from __future__ import annotations

import threading

from src.core.task_feedback import TaskFeedback, feedback_from_config


def test_task_feedback_reports_clamped_progress_and_activity():
    snapshots = []
    feedback = TaskFeedback(snapshots.append)

    feedback.set_total(4, phase="测试阶段")
    feedback.advance(2, activity="正在请求", stop_reason="等待请求返回")
    feedback.advance(99)

    snapshot = feedback.snapshot()
    assert snapshot["completed"] == 4
    assert snapshot["total"] == 4
    assert snapshot["percent"] == 100.0
    assert snapshot["phase"] == "测试阶段"
    assert snapshot["activity"] == "正在请求"
    assert snapshot["stop_reason"] == "等待请求返回"
    assert snapshots[-1] == snapshot


def test_task_feedback_advance_is_thread_safe():
    feedback = TaskFeedback()
    feedback.set_total(1000)

    threads = [threading.Thread(target=lambda: [feedback.advance() for _ in range(100)]) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert feedback.snapshot()["completed"] == 1000


def test_feedback_from_config_ignores_non_runtime_values():
    feedback = TaskFeedback()
    assert feedback_from_config({"_task_feedback": feedback}) is feedback
    assert feedback_from_config({"_task_feedback": {}}) is None
    assert feedback_from_config(None) is None
