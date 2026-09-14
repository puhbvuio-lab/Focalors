from __future__ import annotations

import time
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QAbstractItemView, QLabel, QListWidget, QListWidgetItem

from src.core import build_output_path
from src.judge_aigc.config import config as aigc_config
from src.processing.keyword_candidate_validator import run_keyword_candidate_validator, validate_collection_headers
from src.processing.xlsx_schema import extract_sheet_headers, extract_sheet_names, resolve_target_columns
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


class JudgeAIGCWindow(SimpleToolWindow):
    tool_id = "judge_aigc"

    def __init__(self) -> None:
        super().__init__(
            "AIGC 内容判断",
            [
                FieldSpec("input_path", "输入内容，每行一条", kind="text_or_file", required=True, placeholder="序号 标题"),
                FieldSpec("row_limit", "每批行数", kind="int", default=aigc_config.ROW_LIMIT, minimum=1, maximum=100000),
                FieldSpec("max_workers", "当前批 AI 并发数", kind="int", default=3, minimum=1, maximum=100),
                FieldSpec("save_every_batches", "每几批保存一次", kind="int", default=aigc_config.SAVE_EVERY_BATCHES, minimum=1, maximum=100000),
            ],
            height=600,
        )

    def tool_config_params(self):
        return [
            ConfigParam("temperature", "AI 温度 (0-2)", kind="float", default=aigc_config.TEMPERATURE, minimum=0.0, maximum=2.0, step=0.1, decimals=1),
            ConfigParam("sleep_seconds", "批次间隔(秒)", kind="float", default=aigc_config.SLEEP_SECONDS, minimum=0.1, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("trust_local_negative_aigc", "信任本地非 AIGC 判断", kind="bool", default=aigc_config.TRUST_LOCAL_NEGATIVE_AIGC),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.processing.judge_aigc import judge

        output_path = build_output_path("data", f"judge_aigc_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        log_callback(f"输出文件：{output_path}")
        if stop_event and stop_event.is_set():
            finish_callback(None)
            return None
        config = {k: v for k, v in values.items() if k in ("temperature", "sleep_seconds", "trust_local_negative_aigc")}
        judge(
            self._text_to_tempfile(values["input_path"], prefix="aigc_input"),
            output_path,
            row_limit=int(values["row_limit"]),
            max_workers=int(values["max_workers"]),
            save_every_batches=int(values["save_every_batches"]),
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
            config_overrides=config,
        )
        log_callback(f"完成，已保存：{output_path}")
        finish_callback(output_path)
        return output_path


class KeywordCandidateValidatorWindow(SimpleToolWindow):
    tool_id = "keyword_candidate_validator"

    def __init__(self) -> None:
        super().__init__(
            "关键词候选词验证",
            [
                FieldSpec("input_xlsx", "输入 Excel 文件", kind="file", required=True, placeholder="选择标准化采集 .xlsx 文件"),
                FieldSpec("sheet_name", "目标 Sheet", kind="combo", required=True, options=()),
                FieldSpec("row_limit", "每批行数", kind="int", default=100, minimum=1, maximum=100000),
                FieldSpec("max_workers", "当前批 AI 并发数", kind="int", default=3, minimum=1, maximum=100),
                FieldSpec("save_every_batches", "每几批保存一次", kind="int", default=5, minimum=1, maximum=100000),
            ],
            height=720,
        )
        self._column_load_error: str | None = None
        self.column_list = QListWidget()
        self.column_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.column_list.setMinimumHeight(140)
        self.form_layout.insertRow(2, QLabel("判定列"), self.column_list)
        self.widgets["target_columns"] = self.column_list

        self.widgets["input_xlsx"].path_edit.textChanged.connect(self._reload_sheet_names)
        self.widgets["sheet_name"].currentTextChanged.connect(self._reload_columns)
        self._reload_sheet_names(self.widgets["input_xlsx"].path_edit.text().strip())

    def tool_config_params(self):
        return [
            ConfigParam("temperature", "AI 温度 (0-2)", kind="float", default=aigc_config.TEMPERATURE, minimum=0.0, maximum=2.0, step=0.1, decimals=1),
            ConfigParam("sleep_seconds", "批次间隔(秒)", kind="float", default=aigc_config.SLEEP_SECONDS, minimum=0.1, maximum=10.0, step=0.1, decimals=1),
        ]

    def _checked_columns(self) -> list[str]:
        selected: list[str] = []
        for index in range(self.column_list.count()):
            item = self.column_list.item(index)
            if item.checkState() == Qt.Checked:
                selected.append(item.text())
        return selected

    def _reload_sheet_names(self, file_path: str) -> None:
        combo = self.widgets["sheet_name"]
        previous_sheet = combo.currentText().strip()
        combo.blockSignals(True)
        combo.clear()
        combo.blockSignals(False)
        self.column_list.clear()
        self._column_load_error = None

        path = Path(file_path or "")
        if not path.is_file():
            return

        try:
            sheet_names = extract_sheet_names(path)
        except Exception as exc:
            self._column_load_error = str(exc)
            return

        combo.blockSignals(True)
        combo.addItems(sheet_names)
        if previous_sheet and previous_sheet in sheet_names:
            combo.setCurrentText(previous_sheet)
        elif sheet_names:
            combo.setCurrentIndex(0)
        combo.blockSignals(False)
        self._reload_columns(combo.currentText())

    def _reload_columns(self, sheet_name: str) -> None:
        checked = set(self._checked_columns())
        self.column_list.clear()
        self._column_load_error = None

        input_path = Path(self.widgets["input_xlsx"].path_edit.text().strip())
        if not input_path.is_file() or not sheet_name:
            return

        try:
            headers = extract_sheet_headers(input_path, sheet_name)
            validate_collection_headers(headers)
        except Exception as exc:
            self._column_load_error = str(exc)
            return

        default_checked = {"标题", "简介"}
        for header in headers:
            item = QListWidgetItem(header)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            should_check = header in checked or (not checked and header in default_checked)
            item.setCheckState(Qt.Checked if should_check else Qt.Unchecked)
            self.column_list.addItem(item)

    def collect_values(self):
        values = super().collect_values()
        if values is None:
            return None
        values["target_columns"] = self._checked_columns()
        return values

    def validate_values(self, values):
        input_path = Path(values["input_xlsx"])
        if not input_path.is_file():
            raise ValueError("输入 Excel 文件不存在。")
        if input_path.suffix.lower() != ".xlsx":
            raise ValueError("当前工具仅支持 .xlsx 文件。")
        if self._column_load_error:
            raise ValueError(self._column_load_error)
        if not values["sheet_name"]:
            raise ValueError("请选择目标 Sheet。")
        headers = extract_sheet_headers(input_path, values["sheet_name"])
        validate_collection_headers(headers)
        values["target_columns"] = resolve_target_columns(headers, values.get("target_columns", []))

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        output_path = build_output_path("data", f"keyword_candidate_validator_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        log_callback(f"输出文件：{output_path}")
        if stop_event and stop_event.is_set():
            finish_callback(None)
            return None

        run_keyword_candidate_validator(
            values["input_xlsx"],
            output_path,
            sheet_name=values["sheet_name"],
            target_columns=list(values["target_columns"]),
            row_limit=int(values["row_limit"]),
            max_workers=int(values["max_workers"]),
            save_every_batches=int(values["save_every_batches"]),
            temperature=float(values["temperature"]),
            sleep_seconds=float(values["sleep_seconds"]),
            log_callback=log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
        )
        if stop_event and stop_event.is_set():
            log_callback(f"任务已停止，部分结果已保存：{output_path}")
            finish_callback(output_path)
            return output_path
        log_callback(f"完成，已保存：{output_path}")
        finish_callback(output_path)
        return output_path


class XlsxMergeWindow(SimpleToolWindow):
    def __init__(self) -> None:
        super().__init__(
            "XLSX 文件合并",
            [
                FieldSpec("folder", "XLSX 文件夹", kind="folder", required=True),
                FieldSpec("platform", "平台前缀", kind="combo", default="tiktok", options=("youtube", "tiktok", "x")),
                FieldSpec("keyword", "文件名包含", default="keyword", required=True),
                FieldSpec("schema_mode", "合并模式", kind="combo", default="union_schema", options=("union_schema", "strict")),
            ],
            height=600,
        )

    def validate_values(self, values):
        if not Path(values["folder"]).exists():
            raise ValueError("文件夹不存在。")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.processing.xlsx_merge import merge_xlsx_files

        log_callback(f"合并文件夹：{values['folder']}")
        log_callback(f"平台前缀：{values['platform']}")
        log_callback(f"文件名关键词：{values['keyword']}")
        log_callback(f"合并表头模式：{values['schema_mode']}")
        
        if stop_event and stop_event.is_set():
            finish_callback(None)
            return None
            
        outcome = merge_xlsx_files(
            folder=values["folder"],
            keyword=values["keyword"],
            platform=values["platform"],
            schema_mode=values["schema_mode"],
            stop_event=stop_event,
            pause_event=pause_event
        )
        
        log_callback(f"合并结果状态：{outcome.status.value}")
        stats = outcome.stats
        log_callback(f"成功合并数据文件数：{stats.success_count}，失败数：{stats.failed_count}，跳过数：{stats.skipped_count}")
        if outcome.output_path:
            log_callback(f"合并后输出文件路径：{outcome.output_path}")
            
        finish_callback(outcome)
        return outcome


class AnomalyDetectionWindow(SimpleToolWindow):
    tool_id = "processing_anomaly_detection"

    def __init__(self) -> None:
        super().__init__(
            "数据异常分析检测",
            [
                FieldSpec("input_xlsx", "输入 Excel 文件", kind="file", required=True, placeholder="选择需检测的 .xlsx 文件"),
            ],
            height=600,
        )

    def tool_config_params(self):
        return [
            ConfigParam("high_view_threshold", "高浏览量阈值", kind="int", default=1000, minimum=1, maximum=100000000),
            ConfigParam("high_like_threshold", "高点赞阈值", kind="int", default=50, minimum=1, maximum=100000000),
            ConfigParam("abnormal_ratio_multiplier", "失调倍数", kind="float", default=2.0, minimum=1.0, maximum=100.0, step=0.1, decimals=1),
            ConfigParam("abnormal_ratio_min_trigger", "失调最小触发数", kind="int", default=5, minimum=1, maximum=10000),
            ConfigParam("strict_zero_check", "严格 0 值矛盾检测", kind="bool", default=True),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.processing.anomaly_detection import run_anomaly_detection

        return run_anomaly_detection(values, self.config_values, log_callback, finish_callback, stop_event, pause_event)
