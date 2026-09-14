from __future__ import annotations

from src.core import RunStatus, TaskFeedback
from src.platforms.youtube import context as context_module
from src.platforms.youtube import profiles as profiles_module


class _DummyWriter:
    def __init__(self, *_args, **_kwargs):
        self.rows = []
        self.saved = False

    def writerow(self, row):
        self.rows.append(row)

    def writerows(self, rows):
        self.rows.extend(rows)

    def save(self):
        self.saved = True


def test_profiles_reports_item_and_export_progress(monkeypatch, tmp_path):
    input_path = tmp_path / "profiles.txt"
    input_path.write_text("https://www.youtube.com/@channel\n", encoding="utf-8")

    def fake_run_dir(_platform, _channel, run_id):
        path = tmp_path / run_id
        path.mkdir()
        return path

    monkeypatch.setattr(profiles_module, "build_platform_run_output_dir", fake_run_dir)
    monkeypatch.setattr(profiles_module, "YouTubeClientPool", lambda _keys: object())
    monkeypatch.setattr(
        profiles_module,
        "resolve_channel",
        lambda *_args, **_kwargs: {
            "id": "UC1",
            "snippet": {"title": "Channel", "description": "Description"},
            "statistics": {"subscriberCount": "10"},
        },
    )
    monkeypatch.setattr(profiles_module, "XlsxRowWriter", _DummyWriter)
    feedback = TaskFeedback()

    outcome = profiles_module.run_channel_spider(
        ["key"],
        str(input_path),
        lambda _message: None,
        lambda _outcome: None,
        config={"max_parallel_tabs": 1, "_task_feedback": feedback},
        run_id="feedback_profiles",
    )

    assert outcome.status == RunStatus.SUCCEEDED
    assert feedback.snapshot()["completed"] == feedback.snapshot()["total"] == 2
    assert feedback.snapshot()["activity"] == "博主信息导出完成"


def test_context_reports_pair_and_export_progress(monkeypatch, tmp_path):
    output_path = tmp_path / "context.xlsx"
    writer = _DummyWriter()
    monkeypatch.setattr(
        context_module,
        "parse_input_pairs",
        lambda _path: [("video-1", "profile-1"), ("video-2", "profile-2")],
    )
    monkeypatch.setattr(context_module, "build_output_path", lambda *_args, **_kwargs: str(output_path))
    monkeypatch.setattr(context_module, "XlsxRowWriter", lambda *_args, **_kwargs: writer)
    monkeypatch.setattr(context_module, "YouTubeClientPool", lambda _keys: object())
    monkeypatch.setattr(
        context_module,
        "build_pair_rows",
        lambda _pool, target, *_args, **_kwargs: [{"目标视频链接": target}],
    )
    feedback = TaskFeedback()

    context_module.run_youtube_paired_context_spider(
        ["key"],
        "unused.txt",
        lambda _message: None,
        lambda _path: None,
        config={"max_parallel_tabs": 2, "_task_feedback": feedback},
    )

    assert writer.saved is True
    assert feedback.snapshot()["completed"] == feedback.snapshot()["total"] == 3
    assert feedback.snapshot()["activity"] == "视频上下文导出完成"
