from __future__ import annotations

from pathlib import Path

from src.judge_aigc.config import config
from src.judge_aigc.judge_AIGC import run_judge


def judge(
    input_path: str | Path,
    output_path: str | Path,
    row_limit: int | None = None,
    max_workers: int = 3,
    save_every_batches: int | None = None,
    log_callback=None,
    stop_event=None,
    pause_event=None,
    config_overrides: dict | None = None,
) -> str:
    if config_overrides:
        if "temperature" in config_overrides:
            config.TEMPERATURE = float(config_overrides["temperature"])
        if "sleep_seconds" in config_overrides:
            config.SLEEP_SECONDS = float(config_overrides["sleep_seconds"])
        if "trust_local_negative_aigc" in config_overrides:
            config.TRUST_LOCAL_NEGATIVE_AIGC = bool(config_overrides["trust_local_negative_aigc"])
    row_limit = config.ROW_LIMIT if row_limit is None else row_limit
    save_every_batches = config.SAVE_EVERY_BATCHES if save_every_batches is None else save_every_batches
    return run_judge(
        input_txt_path=str(input_path),
        output_excel_path=str(output_path),
        row_limit=row_limit,
        max_workers=max_workers,
        save_every_batches=save_every_batches,
        log_callback=log_callback,
        stop_event=stop_event,
        pause_event=pause_event,
    )

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="判断标题是否为 AIGC 内容并识别主要语言")
    parser.add_argument("input_txt", help="输入 TXT 文件")
    parser.add_argument("output_xlsx", nargs="?", default="AI判断结果.xlsx", help="输出 XLSX 文件")
    parser.add_argument("--row-limit", type=int, default=config.ROW_LIMIT, help="每批处理行数")
    parser.add_argument("--max-workers", type=int, default=3, help="当前批 AI 并发数")
    parser.add_argument("--save-every-batches", type=int, default=config.SAVE_EVERY_BATCHES, help="每几批保存一次")
    args = parser.parse_args(argv)
    judge(
        args.input_txt,
        args.output_xlsx,
        row_limit=args.row_limit,
        max_workers=args.max_workers,
        save_every_batches=args.save_every_batches,
    )


if __name__ == "__main__":
    main()
