from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.xlsx import MultiSheetXlsxWriter, XlsxRowWriter


def _sample_peak_rss(stop_event: threading.Event, result: list[int]) -> None:
    process = psutil.Process()
    peak = process.memory_info().rss
    while not stop_event.wait(0.05):
        peak = max(peak, process.memory_info().rss)
    result.append(max(peak, process.memory_info().rss))


def _row(index: int) -> dict[str, Any]:
    return {
        "id": index,
        "title": f"title-{index}",
        "content": f"benchmark-content-{index}-" + ("x" * 48),
        "count": index * 3,
        "url": f"https://example.invalid/items/{index}",
    }


def run_case(root: Path, row_count: int, interval: int, mode: str) -> dict[str, Any]:
    interval_label = "end-only" if interval <= 0 else str(interval)
    autosave_every = row_count + 1 if interval <= 0 else interval
    output_path = root / f"{mode}_{row_count}_{interval_label}.xlsx"
    fields = ["id", "title", "content", "count", "url"]
    stop_event = threading.Event()
    peak_rss: list[int] = []
    sampler = threading.Thread(target=_sample_peak_rss, args=(stop_event, peak_rss), daemon=True)
    started = time.perf_counter()
    append_started = started
    error = ""

    try:
        sampler.start()
        if mode == "single":
            writer = XlsxRowWriter(str(output_path), fields, autosave_every=autosave_every)
            for index in range(row_count):
                writer.writerow(_row(index))
        else:
            writer = MultiSheetXlsxWriter(
                str(output_path),
                {"even": fields, "odd": fields},
                autosave_every=autosave_every,
            )
            for index in range(row_count):
                writer.writerow("even" if index % 2 == 0 else "odd", _row(index))
        append_seconds = time.perf_counter() - append_started
        writer.save()
        save_count = writer._save_count
    except Exception as exc:
        append_seconds = time.perf_counter() - append_started
        save_count = 0
        error = f"{type(exc).__name__}: {exc}"
    finally:
        stop_event.set()
        if sampler.is_alive():
            sampler.join(timeout=2)

    total_seconds = time.perf_counter() - started
    return {
        "rows": row_count,
        "mode": mode,
        "save_interval": interval_label,
        "total_seconds": round(total_seconds, 4),
        "append_seconds": round(append_seconds, 4),
        "save_count": save_count,
        "peak_rss_mb": round((peak_rss[-1] if peak_rss else 0) / 1024 / 1024, 2),
        "file_size_mb": round(output_path.stat().st_size / 1024 / 1024, 2) if output_path.exists() else 0,
        "seconds_per_1000_rows": round(total_seconds * 1000 / max(1, row_count), 4),
        "exception_rate": 1 if error else 0,
        "error": error,
    }


def _markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "# XLSX 写入性能基线",
        "",
        "| 行数 | 模式 | 保存间隔 | 总耗时(s) | 追加耗时(s) | 保存次数 | 峰值RSS(MB) | 文件(MB) | 每千行(s) | 异常率 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item['rows']} | {item['mode']} | {item['save_interval']} | "
            f"{item['total_seconds']} | {item['append_seconds']} | {item['save_count']} | "
            f"{item['peak_rss_mb']} | {item['file_size_mb']} | "
            f"{item['seconds_per_1000_rows']} | {item['exception_rate']} |"
        )
    comparisons = []
    for mode in ("single", "multi"):
        baseline = next(
            (item for item in results if item["rows"] == 50_000 and item["mode"] == mode and item["save_interval"] == "500"),
            None,
        )
        optimized = next(
            (item for item in results if item["rows"] == 50_000 and item["mode"] == mode and item["save_interval"] == "5000"),
            None,
        )
        if baseline and optimized and optimized["total_seconds"]:
            comparisons.append(
                f"- 50k {mode}：5000 行策略比 500 行策略快 "
                f"{baseline['total_seconds'] / optimized['total_seconds']:.2f} 倍，"
                f"保存次数由 {baseline['save_count']} 次降至 {optimized['save_count']} 次。"
            )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            *comparisons,
            "- 正式策略采用每 5,000 行自动保存，并在完成、取消或异常边界显式保存。",
            "",
            "由 `benchmarks/xlsx_write_benchmark.py` 在 Windows + Python 3.13 环境生成。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark XLSX save intervals.")
    parser.add_argument("--rows", type=int, nargs="+", default=[10_000, 50_000])
    parser.add_argument("--intervals", type=int, nargs="+", default=[500, 5000, 0])
    parser.add_argument("--modes", choices=("single", "multi"), nargs="+", default=["single", "multi"])
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="xlsx-benchmark-") as tmp:
        root = Path(tmp)
        results = [
            run_case(root, row_count, interval, mode)
            for row_count in args.rows
            for mode in args.modes
            for interval in args.intervals
        ]

    payload = json.dumps(results, ensure_ascii=False, indent=2) + "\n"
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(_markdown(results), encoding="utf-8")
    return 1 if any(item["error"] for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
