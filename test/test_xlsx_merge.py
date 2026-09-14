# -*- coding: utf-8 -*-
import threading
from pathlib import Path
from openpyxl import Workbook
from src.processing.xlsx_merge import merge_xlsx_files, RunStatus
from src.core import generate_run_id

def create_xlsx_file(path: Path, headers: list[str], rows: list[list]):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    if headers:
        ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)

def test_merge_same_headers(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    f2 = tmp_path / "f2.xlsx"
    create_xlsx_file(f1, ["序号", "标题", "播放量"], [[1, "视频1", 100], [2, "视频2", 200]])
    create_xlsx_file(f2, ["序号", "标题", "播放量"], [[1, "视频3", 300]])
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.SUCCEEDED
    assert outcome.stats.success_count == 2
    assert outcome.stats.failed_count == 0
    assert outcome.stats.skipped_count == 0
    
    from openpyxl import load_workbook
    merged_wb = load_workbook(outcome.output_path, read_only=True, data_only=True)
    merged_ws = merged_wb.active
    rows = list(merged_ws.iter_rows(values_only=True))
    assert rows[0] == ("序号", "标题", "播放量")
    assert rows[1] == (1, "视频1", 100)
    assert rows[2] == (2, "视频2", 200)
    assert rows[3] == (3, "视频3", 300)

def test_merge_strict_mismatch(tmp_path):
    f1 = tmp_path / "f1_kw.xlsx"
    f2 = tmp_path / "f2_kw.xlsx"
    create_xlsx_file(f1, ["序号", "标题", "播放量"], [[1, "视频1", 100]])
    create_xlsx_file(f2, ["序号", "标题", "点赞量"], [[1, "视频2", 50]])
    
    outcome = merge_xlsx_files(tmp_path, keyword="kw", platform="test_run", schema_mode="strict")
    assert outcome.status == RunStatus.PARTIAL
    assert outcome.stats.success_count == 1
    assert outcome.stats.failed_count == 1
    assert outcome.stats.skipped_count == 0
    assert len(outcome.errors) == 1
    assert outcome.errors[0].code == "XLSX_SCHEMA_MISMATCH"

def test_merge_union_schema(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    f2 = tmp_path / "f2.xlsx"
    create_xlsx_file(f1, ["序号", "标题", "播放量"], [[1, "视频1", 100]])
    create_xlsx_file(f2, ["序号", "标题", "点赞量"], [[1, "视频2", 50]])
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run", schema_mode="union_schema")
    assert outcome.status == RunStatus.SUCCEEDED
    assert outcome.stats.success_count == 2
    assert outcome.stats.failed_count == 0
    
    from openpyxl import load_workbook
    merged_wb = load_workbook(outcome.output_path, read_only=True, data_only=True)
    merged_ws = merged_wb.active
    rows = list(merged_ws.iter_rows(values_only=True))
    assert rows[0] == ("序号", "标题", "播放量", "点赞量")
    assert rows[1] == (1, "视频1", 100, None)
    assert rows[2] == (2, "视频2", None, 50)

def test_merge_duplicate_headers(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题", "标题"], [[1, "A", "B"]])
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.FAILED
    assert any(e.code == "XLSX_DUPLICATE_HEADER" for e in outcome.errors)

def test_merge_empty_worksheet(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, [], [])
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.FAILED
    assert any(e.code == "XLSX_WORKSHEET_EMPTY" for e in outcome.errors)

def test_merge_corrupted_file(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    with open(f1, "w") as f:
        f.write("corrupted data")
        
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.FAILED
    assert any(e.code == "XLSX_FILE_UNREADABLE" for e in outcome.errors)

def test_merge_formulas(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["序号", "数A", "数B", "和"])
    ws.append([1, 10, 20, "=SUM(B2:C2)"])
    wb.save(f1)
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.SUCCEEDED

def test_merge_cancelled(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "视频1"]])
    
    stop_event = threading.Event()
    stop_event.set()
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run", stop_event=stop_event)
    assert outcome.status == RunStatus.CANCELLED
    assert len(outcome.errors) == 1
    assert outcome.errors[0].code == "RUN_CANCELLED"

def test_merge_concurrency_isolation(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    
    outcomes = []
    # Use unique run_ids to prevent FileExistsError since build_run_output_dir is exclusive creation now!
    def run_merge(rid):
        outcomes.append(merge_xlsx_files(tmp_path, keyword="", platform="test_run", run_id=rid))
        
    t1 = threading.Thread(target=run_merge, args=("run_A_" + generate_run_id(),))
    t2 = threading.Thread(target=run_merge, args=("run_B_" + generate_run_id(),))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    assert len(outcomes) == 2
    assert outcomes[0].status == RunStatus.SUCCEEDED
    assert outcomes[1].status == RunStatus.SUCCEEDED
    assert "run_A" in outcomes[0].output_path or "run_A" in outcomes[1].output_path
    assert "run_B" in outcomes[0].output_path or "run_B" in outcomes[1].output_path

def test_xlsx_merge_counts_files_not_rows(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    f2 = tmp_path / "f2.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"], [2, "B"]])
    create_xlsx_file(f2, ["序号", "标题"], [[1, "C"]])
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.SUCCEEDED
    assert outcome.stats.input_count == 2
    assert outcome.stats.success_count == 2
    assert outcome.stats.failed_count == 0
    assert outcome.stats.extra["merged_row_count"] == 3

def test_xlsx_merge_multi_sheet_file_counts_once(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "S1"
    ws1.append(["序号", "标题"])
    ws1.append([1, "A"])
    ws2 = wb.create_sheet("S2")
    ws2.append(["序号", "标题"])
    ws2.append([1, "B"])
    wb.save(f1)
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.SUCCEEDED
    assert outcome.stats.input_count == 1
    assert outcome.stats.success_count == 1
    assert outcome.stats.extra["merged_row_count"] == 2

def test_xlsx_merge_partial_file_status(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    f2 = tmp_path / "f2.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    # f2 duplicate headers (makes it failed)
    create_xlsx_file(f2, ["序号", "标题", "标题"], [[1, "B", "C"]])
    
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.PARTIAL
    assert outcome.stats.input_count == 2
    assert outcome.stats.success_count == 1
    assert outcome.stats.failed_count == 1
    assert outcome.stats.extra["merged_row_count"] == 1

def test_default_output_is_in_run_directory(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run")
    assert outcome.status == RunStatus.SUCCEEDED
    p = Path(outcome.output_path)
    assert p.name == "test_run_merge.xlsx"
    assert "xlsx_merge" in p.parts
    assert p.parent.parent.name == "xlsx_merge"
    assert p.parent.parent.parent.name == "data"

def test_filename_output_is_in_run_directory(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run", output_file="custom_out.xlsx")
    assert outcome.status == RunStatus.SUCCEEDED
    p = Path(outcome.output_path)
    assert p.name == "custom_out.xlsx"
    assert "xlsx_merge" in p.parts
    assert p.parent.parent.name == "xlsx_merge"
    assert p.parent.parent.parent.name == "data"

def test_absolute_output_path_is_preserved(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    abs_out = tmp_path / "my_custom_abs.xlsx"
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run", output_file=abs_out)
    assert outcome.status == RunStatus.SUCCEEDED
    assert outcome.output_path == str(abs_out)
    assert abs_out.exists()

def test_relative_output_path_with_parent_is_rejected(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run", output_file="sub/custom.xlsx")
    assert outcome.status == RunStatus.FAILED
    assert any(e.code == "XLSX_OUTPUT_INVALID" for e in outcome.errors)

def test_output_write_failure_returns_structured_error(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    # Non-existent drive path to guarantee write failure on Windows
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run", output_file="Z:/invalid_folder_xyz/merged.xlsx")
    assert outcome.status == RunStatus.FAILED
    assert any(e.code == "OUTPUT_WRITE_FAILED" for e in outcome.errors)

def test_xlsx_merge_cancels_during_large_row_stream(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    # Make a file with 500 rows to cross 200 row check block
    create_xlsx_file(f1, ["序号", "标题"], [[i, f"V{i}"] for i in range(500)])
    
    stop_event = threading.Event()
    # Trigger cancellation dynamically
    class CancelStopEvent:
        def __init__(self):
            self.calls = 0
        def is_set(self):
            self.calls += 1
            if self.calls >= 1: # cancel after the first check (at 200 rows)
                return True
            return False
            
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run", stop_event=CancelStopEvent())
    assert outcome.status == RunStatus.CANCELLED
    assert any(e.code == "RUN_CANCELLED" for e in outcome.errors)

def test_xlsx_merge_cancelled_run_has_no_xlsx_artifact(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    stop_event = threading.Event()
    stop_event.set()
    outcome = merge_xlsx_files(tmp_path, keyword="", platform="test_run", stop_event=stop_event)
    assert outcome.status == RunStatus.CANCELLED
    assert outcome.output_path is None
    assert not any(a.label == "合并后的Excel数据" for a in outcome.artifacts)

def test_generated_run_ids_are_unique():
    from src.core.output import generate_run_id
    r1 = generate_run_id()
    r2 = generate_run_id()
    assert r1 != r2
    assert len(r1.split("_")[-1]) == 8 # hex part is 8 chars

def test_build_run_dir_rejects_existing_directory(tmp_path):
    from src.core.output import build_run_output_dir
    import pytest
    rid = "test_run_" + generate_run_id()
    # first create succeeds
    dir1 = build_run_output_dir("test_tool", rid)
    assert dir1.exists()
    
    # second create of same rid fails with FileExistsError
    with pytest.raises(FileExistsError):
        build_run_output_dir("test_tool", rid)

def test_explicit_duplicate_run_id_returns_conflict(tmp_path):
    f1 = tmp_path / "f1.xlsx"
    create_xlsx_file(f1, ["序号", "标题"], [[1, "A"]])
    rid = "rid_" + generate_run_id()
    outcome1 = merge_xlsx_files(tmp_path, keyword="", platform="test_run", run_id=rid)
    assert outcome1.status == RunStatus.SUCCEEDED
    
    # duplicate rid triggers RUN_ID_CONFLICT
    outcome2 = merge_xlsx_files(tmp_path, keyword="", platform="test_run", run_id=rid)
    assert outcome2.status == RunStatus.FAILED
    assert any(e.code == "RUN_ID_CONFLICT" for e in outcome2.errors)

def test_run_id_rejects_path_traversal_and_separators():
    from src.core.output import build_run_output_dir
    import pytest
    with pytest.raises(ValueError):
        build_run_output_dir("tool/name", "run_id")
    with pytest.raises(ValueError):
        build_run_output_dir("tool", "../run_id")
    with pytest.raises(ValueError):
        build_run_output_dir("tool", "run$id")
