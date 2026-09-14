from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.core import output as core_output
from src.platforms.youtube import snapshot_scheduler
from src.processing.anomaly_detection import run_anomaly_detection


def test_youtube_snapshot_jobs_migrate_to_youtube_dir(tmp_path, monkeypatch):
    output_root = tmp_path / "output"
    legacy_jobs_file = output_root / "youtube_snapshot_jobs.json"
    new_jobs_file = output_root / "youtube" / "snapshot_jobs.json"
    payload = {"jobs": [{"xlsx_path": "old.xlsx", "target_days": [3], "completed_days": []}]}

    output_root.mkdir(parents=True)
    legacy_jobs_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(snapshot_scheduler, "get_output_root", lambda: output_root)

    assert snapshot_scheduler._load_jobs() == payload
    assert new_jobs_file.exists()
    assert not legacy_jobs_file.exists()

    updated_payload = {"jobs": [{"xlsx_path": "new.xlsx", "target_days": [7], "completed_days": []}]}
    snapshot_scheduler._save_jobs(updated_payload)
    assert json.loads(new_jobs_file.read_text(encoding="utf-8")) == updated_payload


def test_anomaly_detection_outputs_under_data_anomaly(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    input_xlsx = tmp_path / "input.xlsx"
    finished_paths: list[str | None] = []

    pd.DataFrame(
        {
            "浏览量": [100],
            "点赞量": [200],
            "评论数": [0],
            "转发量": [0],
        }
    ).to_excel(input_xlsx, index=False)

    monkeypatch.setattr(core_output, "get_workspace_root", lambda: workspace)

    run_anomaly_detection(
        values={"input_xlsx": str(input_xlsx)},
        config={},
        log_callback=lambda _message: None,
        finish_callback=finished_paths.append,
        stop_event=None,
    )

    output_path = Path(finished_paths[-1] or "")
    assert output_path.parent == workspace / "output" / "data" / "anomaly"
    assert "processing" not in output_path.parts
    assert output_path.exists()
