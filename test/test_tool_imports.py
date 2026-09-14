import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS
from src.studio.tool_runner import check_tool_import


def assert_tool_importable_in_clean_process(tool_spec) -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252:strict"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.studio.tool_runner",
            "--tool-id",
            tool_spec.tool_id,
            "--check-import",
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.stdout.strip(), f"{tool_spec.tool_id} 未输出导入检查结果: {completed.stderr}"
    result = json.loads(completed.stdout)
    assert result["tool_id"] == tool_spec.tool_id
    assert result["script_exists"], f"{tool_spec.tool_id} 缺少实现文件"
    assert completed.returncode == 0, (
        f"{tool_spec.tool_id} 在标准安装中不可导入: "
        f"{result['error_type']} / {result['error_message']} / {completed.stderr}"
    )
    assert result["available"] is True
    assert result["error_type"] is None
    assert result["missing_dependencies"] == []


def test_manifest_tools_are_importable_in_standard_install():
    tools, errors = discover_tools()
    assert not errors, f"工具发现阶段存在错误: {errors}"
    assert tools, "未发现任何 manifest 工具"

    for tool in tools:
        assert_tool_importable_in_clean_process(tool)


def test_missing_dependency_is_reported_structurally():
    missing = ModuleNotFoundError("No module named 'optional_demo'")
    missing.name = "optional_demo"
    with patch("src.studio.tool_runner.load_object", side_effect=missing):
        result = check_tool_import("keyword_coverage_calibration")
    assert result["available"] is False
    assert result["error_type"] == "dependency_missing"
    assert result["missing_dependencies"] == ["optional_demo"]


def test_static_registry_implementation_paths_exist():
    project_root = Path(__file__).resolve().parents[1]
    missing = [
        tool.tool_id
        for tool in TOOLS
        if tool.implementation_path and not (project_root / "src" / tool.implementation_path).exists()
    ]
    assert not missing, f"static registry still contains tools with missing implementation files: {missing}"
