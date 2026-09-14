# -*- coding: utf-8 -*-
"""UI 任务产出归一化测试。"""

import pytest

pytest.importorskip("PyQt5")

from src.core.outcome import RunOutcome, RunStatus
from src.ui.base import _record_task_output


def test_record_task_output_preserves_run_outcome_and_dedupes_output_path():
    outcome = RunOutcome(
        run_id="run_001",
        tool_id="xlsx_merge",
        status=RunStatus.SUCCEEDED,
        output_path="C:/tmp/youtube_merge.xlsx",
    )
    result = {"path": None, "paths": [], "outcome": None}

    _record_task_output(result, outcome)
    _record_task_output(result, outcome)

    assert result["outcome"] is outcome
    assert result["path"] == "C:/tmp/youtube_merge.xlsx"
    assert result["paths"] == ["C:/tmp/youtube_merge.xlsx"]
    assert "RunOutcome" not in result["paths"][0]
