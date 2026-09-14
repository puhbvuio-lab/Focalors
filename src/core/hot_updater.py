"""热更新模块。

下载 GitHub release 源码 zip，解压覆盖后自动重启。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

import requests

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUEST_TIMEOUT = 30
DEPENDENCY_INSTALL_TIMEOUT = 15 * 60

# 这些目录包含用户数据、Git 元数据或当前进程正在使用的运行环境，
# 任何源码更新都不得删除或覆盖。
PRESERVED_TOP_LEVEL_ITEMS = frozenset(
    {
        ".env",
        ".git",
        ".gitignore",
        ".idea",
        ".pixi",
        ".trae",
        ".venv",
        ".vscode",
        "config",
        "env",
        "output",
        "user_data",
        "user_data_edge",
        "venv",
    }
)
REQUIRED_SOURCE_ITEMS = ("main.py", "requirements.txt", "src")


def run_hot_update(tag: str, repo_owner: str, repo_name: str) -> tuple[bool, str]:
    """下载指定 release 源码 zip，解压覆盖并更新版本号。"""
    version = tag.lstrip("v")
    success, msg = _download_and_extract(tag, repo_owner, repo_name)
    if success:
        _write_version(version)
    return (success, msg)


def _write_version(version: str) -> None:
    """将新版本号写入 config/version.json。"""
    version_path = PROJECT_ROOT / "config" / "version.json"
    try:
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_text(
            json.dumps({"version": version}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("版本号已更新为 %s", version)
    except Exception as e:
        logger.warning("写入版本号失败：%s", e)


def _download_and_extract(tag: str, repo_owner: str, repo_name: str) -> tuple[bool, str]:
    """下载、校验并安装 GitHub 源码 zip。"""
    clean_tag = tag.lstrip("v")
    zip_url = f"https://github.com/{repo_owner}/{repo_name}/archive/refs/tags/v{clean_tag}.zip"
    logger.info("正在下载：%s", zip_url)

    try:
        resp = requests.get(zip_url, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
    except Exception as e:
        return (False, f"下载失败：{e}")

    try:
        with tempfile.TemporaryDirectory(prefix="scraper_update_") as tmp_dir:
            temp_root = Path(tmp_dir)
            zip_path = temp_root / "update.zip"
            with zip_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            extract_dir = temp_root / "extracted"
            with zipfile.ZipFile(zip_path, "r") as zf:
                _safe_extract_zip(zf, extract_dir)

            inner_dirs = [path for path in extract_dir.iterdir() if path.is_dir()]
            if len(inner_dirs) != 1:
                raise RuntimeError(f"更新包顶层源码目录数量异常：{len(inner_dirs)}")
            src_dir = inner_dirs[0]

            _validate_source_tree(src_dir)
            _sync_runtime_dependencies(src_dir, PROJECT_ROOT)
            _replace_project(src_dir, PROJECT_ROOT)

        logger.info("更新完成，已切换到 v%s", clean_tag)
        return (True, f"已更新到 v{clean_tag}")
    except Exception as e:
        logger.exception("热更新失败")
        return (False, f"更新失败，已保留原版本：{e}")


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    """拒绝包含绝对路径或目录穿越条目的更新包。"""
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    for member in archive.infolist():
        member_path = (destination / member.filename).resolve()
        try:
            member_path.relative_to(destination_root)
        except ValueError as exc:
            raise RuntimeError(f"更新包包含非法路径：{member.filename}") from exc
    archive.extractall(destination)


def _validate_source_tree(source_root: Path) -> None:
    """在修改现有安装前校验更新包结构和 Python 语法。"""
    missing = [name for name in REQUIRED_SOURCE_ITEMS if not (source_root / name).exists()]
    if missing:
        raise RuntimeError(f"更新包缺少必要文件：{', '.join(missing)}")
    if not (source_root / "src").is_dir():
        raise RuntimeError("更新包中的 src 不是有效目录")

    for python_file in source_root.rglob("*.py"):
        try:
            compile(python_file.read_bytes(), str(python_file), "exec")
        except (OSError, SyntaxError) as exc:
            relative_path = python_file.relative_to(source_root)
            raise RuntimeError(f"更新包 Python 文件校验失败：{relative_path}: {exc}") from exc


def _requirements_changed(source_root: Path, project_root: Path) -> bool:
    staged = source_root / "requirements.txt"
    current = project_root / "requirements.txt"
    if not current.exists():
        return True
    return staged.read_bytes() != current.read_bytes()


def _sync_runtime_dependencies(
    source_root: Path,
    project_root: Path,
    *,
    python_executable: str | None = None,
) -> None:
    """仅在 requirements.txt 变化时，先安装新依赖再替换源码。"""
    if not _requirements_changed(source_root, project_root):
        return

    executable = Path(python_executable or sys.executable)
    if not executable.exists():
        raise RuntimeError(f"当前 Python 解释器不存在：{executable}")

    requirements_path = source_root / "requirements.txt"
    logger.info("检测到运行依赖变化，正在同步 requirements.txt")
    completed = subprocess.run(
        [
            str(executable),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements_path),
        ],
        cwd=str(source_root),
        capture_output=True,
        text=True,
        timeout=DEPENDENCY_INSTALL_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "未知 pip 错误").strip()
        raise RuntimeError(f"依赖安装失败：{details[-2000:]}")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _copy_path(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _restore_backup(
    destination: Path,
    backup_root: Path,
    installed_names: Iterable[str],
) -> list[str]:
    """移除未完成的新版本并恢复备份，返回恢复失败项。"""
    failures: list[str] = []
    for name in reversed(list(installed_names)):
        try:
            _remove_path(destination / name)
        except OSError as exc:
            failures.append(f"移除新文件 {name} 失败：{exc}")

    for backup_item in list(backup_root.iterdir()):
        target = destination / backup_item.name
        try:
            _remove_path(target)
            shutil.move(str(backup_item), str(target))
        except OSError as exc:
            failures.append(f"恢复 {backup_item.name} 失败：{exc}")
    return failures


def _replace_project(src: Path, dst: Path) -> None:
    """事务式替换源码；失败时恢复原文件，并始终保留用户数据和运行环境。"""
    src = src.resolve()
    dst = dst.resolve()
    if not src.is_dir() or not dst.is_dir():
        raise ValueError("源码目录和目标目录都必须存在")
    if src == dst or src in dst.parents or dst in src.parents:
        raise ValueError("源码目录与目标目录不得互相包含")

    preserve = {name.casefold() for name in PRESERVED_TOP_LEVEL_ITEMS}
    backup_root = Path(tempfile.mkdtemp(prefix=f".{dst.name}_update_backup_", dir=str(dst.parent)))
    installed_names: list[str] = []

    try:
        for old_item in list(dst.iterdir()):
            if old_item.name.casefold() in preserve:
                continue
            shutil.move(str(old_item), str(backup_root / old_item.name))

        for source_item in src.iterdir():
            if source_item.name.casefold() in preserve:
                continue
            installed_names.append(source_item.name)
            _copy_path(source_item, dst / source_item.name)
    except Exception as update_error:
        rollback_failures = _restore_backup(dst, backup_root, installed_names)
        if rollback_failures:
            details = "；".join(rollback_failures)
            raise RuntimeError(f"更新失败且自动回滚不完整，备份保留在 {backup_root}：{details}") from update_error
        shutil.rmtree(backup_root)
        raise RuntimeError(f"更新写入失败，已恢复原版本：{update_error}") from update_error
    else:
        try:
            shutil.rmtree(backup_root)
        except OSError as exc:
            logger.warning("更新成功，但旧版本备份目录清理失败：%s (%s)", backup_root, exc)


def restart_app() -> None:
    """启动新进程，不退出当前进程（由调用方在主线程安全退出）。"""
    logger.info("正在重启应用…")
    subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(PROJECT_ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
