# -*- coding: utf-8 -*-
"""YouTube 热度快照自动调度器。

通过本地持久化 JSON 文件保存待处理的快照任务。
在主采集程序启动时检测是否有快照任务到期（如 3天/7天），并自动在后台触发。
"""

from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path

from src.core import log_line, log_error, log_warn
from src.core.output import get_output_root

_jobs_lock = threading.Lock()


def _get_jobs_file() -> Path:
    return get_output_root() / "youtube" / "snapshot_jobs.json"


def _get_legacy_jobs_file() -> Path:
    return get_output_root() / "youtube_snapshot_jobs.json"


def _resolve_jobs_file_for_read() -> Path:
    jobs_file = _get_jobs_file()
    legacy_jobs_file = _get_legacy_jobs_file()
    if jobs_file.exists() or not legacy_jobs_file.exists():
        return jobs_file

    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(legacy_jobs_file, jobs_file)
        return jobs_file
    except OSError:
        return legacy_jobs_file


def _load_jobs() -> dict:
    jobs_file = _resolve_jobs_file_for_read()
    if not jobs_file.exists():
        return {"jobs": []}
    try:
        with _jobs_lock:
            with open(jobs_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return {"jobs": []}


def _save_jobs(data: dict):
    jobs_file = _get_jobs_file()
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path = jobs_file.with_suffix(".tmp")
    with _jobs_lock:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.replace(temp_path, jobs_file)
        except OSError:
            pass # Fallback or silent ignore, better than losing data


def register_job(xlsx_path: str, target_days: list[int]):
    """将新采集的 Excel 文件注册为快照任务。"""
    if not target_days:
        return
        
    data = _load_jobs()
    
    # 检查是否已存在
    for job in data.get("jobs", []):
        if job.get("xlsx_path") == xlsx_path:
            # 合并 target_days
            existing = set(job.get("target_days", []))
            existing.update(target_days)
            job["target_days"] = sorted(list(existing))
            _save_jobs(data)
            return

    new_job = {
        "xlsx_path": xlsx_path,
        "created_timestamp": int(time.time()),
        "target_days": sorted(list(set(target_days))),
        "completed_days": []
    }
    data.setdefault("jobs", []).append(new_job)
    _save_jobs(data)


def process_due_jobs(api_keys: list[str], log_callback, stop_event=None, pause_event=None):
    """扫描并执行已到期的快照任务。"""
    data = _load_jobs()
    jobs = data.get("jobs", [])
    if not jobs:
        return

    from src.platforms.youtube.keyword_snapshot import run_youtube_snapshot
    
    current_ts = int(time.time())
    jobs_modified = False
    
    # 过滤无效任务，同时处理有效任务
    active_jobs = []
    
    for job in jobs:
        xlsx_path = job.get("xlsx_path", "")
        created_ts = job.get("created_timestamp", 0)
        target_days = job.get("target_days", [])
        completed_days = job.get("completed_days", [])
        
        # 如果文件已被移动或删除，记录并抛弃此任务
        if not os.path.isfile(xlsx_path):
            log_warn(log_callback, f"⚠️ 自动快照取消：文件不存在 (可能已被删除或移动) -> {xlsx_path}")
            jobs_modified = True
            continue
            
        pending_days = [d for d in target_days if d not in completed_days]
        if not pending_days:
            # 任务已全部完成
            jobs_modified = True
            continue
            
        processed_actual_days = set()
        
        # 检查是否有天数已经到期
        for day in pending_days:
            # created_ts + day * 86400 <= current_ts
            if current_ts >= created_ts + day * 86400:
                # 动态计算真实流逝的天数（四舍五入到整天）
                actual_days = round((current_ts - created_ts) / 86400)
                
                if actual_days in processed_actual_days:
                    job.setdefault("completed_days", []).append(day)
                    jobs_modified = True
                    continue
                
                log_line(log_callback, f"\n=== 🚀 发现到期的 {day}日 快照任务，正在自动执行 ===")
                if actual_days != day:
                    log_warn(log_callback, f"⚠️ 注意: 任务由于程序未运行等原因发生滞后，实际距今已 {actual_days} 天。快照标签将自动更正为 {actual_days}日。")
                    
                log_line(log_callback, f"原文件: {xlsx_path}")
                
                new_path_box = []
                try:
                    # 使用内部 dummy callback，避免向顶层 UI 抛出任务结束的信号
                    def dummy_finish(path):
                        if path:
                            new_path_box.append(path)
                        
                    run_youtube_snapshot(
                        api_keys=api_keys,
                        xlsx_path=job.get("xlsx_path", xlsx_path),
                        snapshot_days=f"{actual_days}日",
                        log_callback=log_callback,
                        finish_callback=dummy_finish,
                        stop_event=stop_event,
                        pause_event=pause_event
                    )
                    
                    job.setdefault("completed_days", []).append(day)
                    if new_path_box:
                        job["xlsx_path"] = new_path_box[0]
                    processed_actual_days.add(actual_days)
                    jobs_modified = True
                    log_line(log_callback, f"✅ 自动快照 ({actual_days}日) 执行成功。\n")
                except Exception as e:
                    log_error(log_callback, f"❌ 自动快照 ({actual_days}日) 执行失败: {e}\n")
                    
        active_jobs.append(job)
        
    if jobs_modified:
        data["jobs"] = active_jobs
        _save_jobs(data)
