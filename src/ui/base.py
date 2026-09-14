"""
UI 基础窗口定义模块，提供通用的多媒体爬虫任务配置界面基类 SimpleToolWindow，支持多线程控制及自动参数校验。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.app_logging import get_logger
from src.core.task_feedback import TaskFeedback

from PyQt5.QtCore import QEvent, QObject, QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class FieldSpec:
    """
    表单字段描述规范，用于动态生成工具配置界面对应的输入组件。
    """
    name: str                   # 字段键名
    label: str                  # 界面显示的标签名称
    kind: str = "text"          # 字段类型：text, multiline, int, combo, file, folder, text_or_file
    default: str | int = ""     # 默认初值
    required: bool = False      # 是否为必填项
    minimum: int = 1            # 数字类型最小值上限
    maximum: int = 999999       # 数字类型最大值上限
    options: tuple[str, ...] = ()  # combo 下拉框的选项列表
    placeholder: str = ""       # 输入框占位符提示
    tooltip: str = ""           # 悬停提示信息


class WorkerSignals(QObject):
    """
    多线程工作信号器。
    由于 PyQt 不允许在子线程直接操作 GUI 组件，子线程需要通过 emit 信号的方式
    通知主进程刷新界面、追加日志或结束任务。
    """
    log = pyqtSignal(str)          # 子线程发出日志消息
    finished = pyqtSignal(object)   # 子线程顺利结束（回传文件导出路径）
    failed = pyqtSignal(str)       # 子线程运行异常（回传错误消息）
    progress = pyqtSignal(object)  # 结构化进度及当前活动状态


def _record_task_output(result: dict[str, Any], output: Any) -> None:
    """把任务产出归一化到 result，保留结构化 RunOutcome，避免把对象 repr 当路径。"""
    if output is None:
        return

    # 本地导入以防止循环依赖
    from src.core.outcome import RunOutcome

    if isinstance(output, RunOutcome):
        result["outcome"] = output
        if output.output_path:
            _record_task_output(result, output.output_path)
        return

    if isinstance(output, (list, tuple)):
        for item in output:
            _record_task_output(result, item)
        return

    path = str(output)
    if not path:
        return
    if path not in result["paths"]:
        result["paths"].append(path)
    result["path"] = path


class SimpleToolWindow(QWidget):
    """
    通用爬虫工具窗口基类。
    支持自动渲染表单、方案选择、输入校验、后台线程运行（避免界面假死）、日志显示及暂停/停止状态控制。
    """
    tool_id: str = ""
    current_profile: str | None = None
    task_feedback_enabled: bool = False

    def __init__(self, title: str, fields: list[FieldSpec], *, width: int = 720, height: int = 680, form_stretch: int = 0) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self.resize(width, height)
        self.fields = fields
        self._form_stretch = form_stretch
        self.widgets: dict[str, Any] = {}
        
        # 线程同步与状态机通信事件
        self.stop_event = threading.Event()    # 标记用户是否按下了停止按钮
        self.pause_event = threading.Event()   # 标记任务是否处于暂停挂起状态
        self.worker_thread: threading.Thread | None = None

        self.logger = get_logger(self.__class__.__name__)
        
        # 实例化信号接收绑定
        self.signals = WorkerSignals(self)
        self.signals.log.connect(self.append_log)
        self.signals.finished.connect(self._finish_success)
        self.signals.failed.connect(self._finish_error)
        self.signals.progress.connect(self._update_progress)

        self.task_feedback = TaskFeedback(self.signals.progress.emit)
        self._stop_requested_at: float | None = None
        self._last_stop_reason = ""
        self._stop_wait_box: QMessageBox | None = None
        self._stop_wait_timer = QTimer(self)
        self._stop_wait_timer.setInterval(250)
        self._stop_wait_timer.timeout.connect(self._check_stop_wait)
        
        self.form_layout: QFormLayout | None = None
        self.config_values: dict[str, Any] = {}
        self._build_ui()
        self._load_persisted_config()
        self._load_last_inputs()

    def _build_ui(self) -> None:
        """构建基础布局、动态字段表单、控制按钮以及只读日志文本框。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(6)

        # 滚动区域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.NoFrame)
        
        # 表单容器
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setLabelAlignment(Qt.AlignRight)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(5)
        
        scroll_area.setWidget(form_widget)
        root.addWidget(scroll_area, self._form_stretch)
        self.form_layout = form

        # 根据配置动态组装表单行
        for field in self.fields:
            widget = self._create_field_widget(field)
            form.addRow(QLabel(field.label), widget)

        # 操作按钮区
        buttons = QHBoxLayout()
        self.config_button = QPushButton("参数配置")
        self.config_button.clicked.connect(self._open_config)
        buttons.addWidget(self.config_button)
        buttons.addStretch(1)
        
        self.action_button = QPushButton("开始")
        self.action_button.clicked.connect(self._on_action_button)
        buttons.addWidget(self.action_button)
        
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button)
        root.addLayout(buttons)

        # 任务进度区：使用结构化状态更新，不依赖日志文本解析。
        self.progress_label = QLabel("尚未开始")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_label.setVisible(self.task_feedback_enabled)
        self.progress_bar.setVisible(self.task_feedback_enabled)
        root.addWidget(self.progress_label)
        root.addWidget(self.progress_bar)

        # 日志文本输出区
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("运行日志")
        # 限制最大保存 5000 行，防长时间抓取日志量过大撑爆内存
        self.log_text.document().setMaximumBlockCount(5000)
        root.addWidget(self.log_text, 1)
        
        # 统一全局 UI 样式表
        self.setStyleSheet(
            """
            QWidget {
                background: #f6f8fb;
                color: #172033;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 9pt;
            }
            QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {
                background: #ffffff;
                border: 1px solid #d8e0eb;
                border-radius: 6px;
                padding: 6px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #cfd8e6;
                border-radius: 6px;
                padding: 7px 16px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #edf4ff;
            }
            QPushButton:disabled {
                color: #98a2b3;
                background: #eef2f7;
            }
            """
        )

    def _create_field_widget(self, field: FieldSpec):
        """
        工厂方法：根据 FieldSpec.kind 类型实例化对应的 PyQt 组件并完成事件监听绑定。
        """
        if field.kind == "multiline":
            widget = QPlainTextEdit()
            widget.setPlainText(str(field.default or ""))
            widget.setPlaceholderText(field.placeholder)
            widget.setMinimumHeight(64)
        elif field.kind == "int":
            widget = QSpinBox()
            widget.setRange(field.minimum, field.maximum)
            widget.setValue(int(field.default or field.minimum))
        elif field.kind == "combo":
            widget = QComboBox()
            widget.addItems(field.options)
            if field.default in field.options:
                widget.setCurrentText(str(field.default))
        elif field.kind in {"file", "folder"}:
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(str(field.default or ""))
            edit.setPlaceholderText(field.placeholder)
            button = QPushButton("选择")
            button.clicked.connect(lambda _=False, f=field, e=edit: self._select_path(f, e))
            layout.addWidget(edit, 1)
            layout.addWidget(button)
            widget = container
            widget.path_edit = edit
        elif field.kind == "text_or_file":
            # 复合组件：支持下拉切换输入来源（直接在框里写，或从外部 txt 读）
            widget = QWidget()
            vbox = QVBoxLayout(widget)
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(4)
            mode_combo = QComboBox()
            mode_combo.installEventFilter(self)
            mode_combo.addItems(["直接输入", "TXT 文件"])
            vbox.addWidget(mode_combo)
            text_edit = QPlainTextEdit()
            text_edit.setPlainText(str(field.default or ""))
            text_edit.setPlaceholderText(field.placeholder or "每行一条")
            text_edit.setMinimumHeight(48)
            vbox.addWidget(text_edit)
            file_row = QWidget()
            fl = QHBoxLayout(file_row)
            fl.setContentsMargins(0, 0, 0, 0)
            file_edit = QLineEdit()
            file_edit.setPlaceholderText("选择 TXT 文件...")
            file_btn = QPushButton("选择")
            file_btn.clicked.connect(lambda _=False, e=file_edit: self._select_text_file(e))
            fl.addWidget(file_edit, 1)
            fl.addWidget(file_btn)
            vbox.addWidget(file_row)
            file_row.hide()
            # 切换下拉框时，显隐切换对应的输入控件
            mode_combo.currentTextChanged.connect(
                lambda t: (text_edit.show(), file_row.hide()) if t == "直接输入" else (text_edit.hide(), file_row.show())
            )
            widget.mode_combo = mode_combo
            widget.text_edit = text_edit
            widget.file_edit = file_edit
        else:
            widget = QLineEdit(str(field.default or ""))
            widget.setPlaceholderText(field.placeholder)
            
        self.widgets[field.name] = widget
        if field.tooltip:
            widget.setToolTip(field.tooltip)
        # 对数字微调框和下拉框安装轮播拦截器，禁止在此类组件上误触鼠标滚轮切换选项
        if isinstance(widget, (QSpinBox, QComboBox)):
            widget.installEventFilter(self)
        return widget

    def _select_path(self, field: FieldSpec, edit: QLineEdit) -> None:
        """调用系统文件对话框选择保存文件或文件夹目录。"""
        if field.kind == "folder":
            path = QFileDialog.getExistingDirectory(self, "选择文件夹")
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择文件", str(Path.cwd()), "Text Files (*.txt);;Excel Files (*.xlsx);;All Files (*.*)"
            )
        if path:
            edit.setText(path)
            self.raise_()
            self.activateWindow()

    def _select_text_file(self, edit: QLineEdit) -> None:
        """特定于文本源字段的文件选择。"""
        path, _ = QFileDialog.getOpenFileName(self, "选择 TXT 文件", str(Path.cwd()), "Text Files (*.txt);;All Files (*.*)")
        if path:
            edit.setText(path)
            self.raise_()
            self.activateWindow()

    def collect_values(self) -> dict[str, Any] | None:
        """
        收集当前界面表单中所有的用户输入值，做必填项检查，并在此处自动加载文件模式下的内容。

        Returns:
            dict[str, Any] | None: 各个参数的名称和实际输入值的字典映射，如遇校验失败则弹出警告并返回 None。
        """
        values: dict[str, Any] = {}
        for field in self.fields:
            widget = self.widgets[field.name]
            if field.kind == "multiline":
                value = widget.toPlainText().strip()
            elif field.kind == "int":
                widget.interpretText()
                value = widget.value()
            elif field.kind == "combo":
                value = widget.currentText().strip()
            elif field.kind in {"file", "folder"}:
                value = widget.path_edit.text().strip()
            elif field.kind == "text_or_file":
                if widget.mode_combo.currentText() == "TXT 文件":
                    file_path = widget.file_edit.text().strip()
                    if not file_path:
                        QMessageBox.warning(self, "提示", f"请选择或输入：{field.label}")
                        return None
                    try:
                        # 自动解析加载 TXT 文件数据
                        value = Path(file_path).read_text(encoding="utf-8").strip()
                    except Exception as exc:
                        QMessageBox.warning(self, "提示", f"无法读取文件：{exc}")
                        return None
                else:
                    value = widget.text_edit.toPlainText().strip()
            else:
                value = widget.text().strip()
                
            if field.required and not value and widget.isVisible():
                QMessageBox.warning(self, "提示", f"请填写：{field.label}")
                return None
            values[field.name] = value
        return values

    def _load_last_inputs(self) -> None:
        """Restore the last successfully submitted form values for this tool."""
        if not self.tool_id:
            return
        try:
            from src.core.task_checkpoint import load_tool_inputs

            values = load_tool_inputs(self.tool_id)
        except Exception:
            return
        if values:
            self._apply_field_values(values)

    def _apply_field_values(self, values: dict[str, Any]) -> None:
        for field in self.fields:
            if field.name not in values or field.name not in self.widgets:
                continue
            value = values.get(field.name)
            widget = self.widgets[field.name]
            try:
                if field.kind == "multiline":
                    widget.setPlainText(str(value or ""))
                elif field.kind == "int":
                    widget.setValue(int(value or field.minimum))
                elif field.kind == "combo":
                    text = str(value or "")
                    index = widget.findText(text)
                    if index >= 0:
                        widget.setCurrentIndex(index)
                elif field.kind in {"file", "folder"}:
                    widget.path_edit.setText(str(value or ""))
                elif field.kind == "text_or_file":
                    widget.mode_combo.setCurrentText("直接输入")
                    widget.text_edit.setPlainText(str(value or ""))
                    widget.text_edit.show()
                    widget.file_edit.parentWidget().hide()
                else:
                    widget.setText(str(value or ""))
            except Exception:
                continue

    def set_field_visible(self, field_name: str, visible: bool) -> None:
        """设定某个字段的可见性，联动隐藏其 Label 标签。"""
        widget = self.widgets.get(field_name)
        if not widget:
            return
        widget.setVisible(visible)
        form = self.form_layout
        if form is None:
            return
        label = form.labelForField(widget)
        if label:
            label.setVisible(visible)

    def bind_field_visibility(self, trigger_field: str, trigger_value: str, target_fields: list[str]) -> None:
        """
        联动逻辑绑定：当 trigger_field 下拉框的值等于 trigger_value 时，显示 target_fields 列表中的字段，否则隐藏它们。
        """
        combo = self.widgets.get(trigger_field)
        if not isinstance(combo, QComboBox):
            self.logger.warning("bind_field_visibility expects a QComboBox for %s", trigger_field)
            return

        def on_changed(text: str):
            visible = (text == trigger_value)
            for target in target_fields:
                self.set_field_visible(target, visible)

        combo.currentTextChanged.connect(on_changed)
        # 初始化调用，确保初始显隐状态正确
        on_changed(combo.currentText())

    def _on_action_button(self) -> None:
        """按钮文本驱动状态机。利用按钮文本的显隐控制状态转化。"""
        text = self.action_button.text()
        if text == "开始":
            self._do_start()
        elif text == "暂停":
            self._toggle_pause()
        elif text == "继续":
            self._toggle_pause()

    def _do_start(self) -> None:
        """从主线程采集参数，执行定制验证，随后启动后台子线程执行真正的业务逻辑，保证 GUI 窗口流畅响应。"""
        values = self.collect_values()
        if values is None:
            return
        try:
            self.validate_values(values)
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        values = self.prepare_run_values(values)
        if values is None:
            return
        if self.tool_id:
            try:
                from src.core.task_checkpoint import save_tool_inputs

                save_tool_inputs(self.tool_id, {key: value for key, value in values.items() if not key.startswith("_")})
            except Exception:
                pass
        
        self.log_text.clear()
        self.stop_event.clear()
        self.pause_event.clear()
        self._cleanup_stop_feedback()
        if self.task_feedback_enabled:
            self.task_feedback.reset(phase="准备任务", activity="正在初始化")
            values["_task_feedback"] = self.task_feedback
        
        self._set_state("running")
        self.logger.info("Task starting: %s", self.windowTitle())
        
        # 启动后台守护工作线程
        self.worker_thread = threading.Thread(target=self._run_worker, args=(values,), daemon=False)
        self.worker_thread.start()

    def _toggle_pause(self) -> None:
        """切换暂停状态。通过控制 pause_event 阻塞或唤醒子线程。"""
        if self.pause_event.is_set():
            self.pause_event.clear()
            self._set_state("running")
            self.append_log("继续运行...")
        else:
            self.pause_event.set()
            self._set_state("paused")
            self.append_log("已暂停，点击「继续」恢复运行。")

    def stop(self) -> None:
        """发出停止事件。在子线程循环内部会检查 stop_event 决定是否快速终止退出。"""
        self.stop_event.set()
        self.pause_event.clear()  # 防止因处于暂停导致线程死锁无法退出
        self.logger.info("Stop requested: %s", self.windowTitle())
        self.append_log("正在停止，请稍候...")
        self.action_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        if self.task_feedback_enabled:
            self._stop_requested_at = time.monotonic()
            self._last_stop_reason = ""
            self._stop_wait_timer.start()

    def _update_progress(self, snapshot: dict[str, Any]) -> None:
        """主线程槽：刷新进度条和当前阶段说明。"""

        total = max(0, int(snapshot.get("total", 0) or 0))
        completed = max(0, int(snapshot.get("completed", 0) or 0))
        phase = str(snapshot.get("phase", "") or "")
        activity = str(snapshot.get("activity", "") or "")
        if total > 0:
            percent = 100 if completed >= total else min(99, max(0, int(completed * 100 / total)))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(percent)
            parts = [f"{completed}/{total}（{percent}%）"]
        else:
            self.progress_bar.setRange(0, 0)
            parts = []
        if phase:
            parts.insert(0, phase)
        if activity:
            parts.append(activity)
        self.progress_label.setText(" · ".join(parts) or "任务进行中")

    def _check_stop_wait(self) -> None:
        """停止未及时完成时，展示任务报告的安全中断等待原因。"""

        thread = self.worker_thread
        if thread is None or not thread.is_alive():
            self._cleanup_stop_feedback()
            return
        requested_at = self._stop_requested_at
        if requested_at is None or time.monotonic() - requested_at < 0.8:
            return

        snapshot = self.task_feedback.snapshot()
        reason = str(snapshot.get("stop_reason", "") or "").strip()
        if not reason:
            activity = str(snapshot.get("activity", "") or "").strip()
            reason = f"当前仍在{activity}，需要等待安全中断点。" if activity else "正在等待当前步骤到达安全中断点。"
        if reason != self._last_stop_reason:
            self._last_stop_reason = reason
            self.append_log(f"停止等待原因：{reason}")

        try:
            box = self._stop_wait_box
            if box is None:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Information)
                box.setWindowTitle("正在安全停止")
                box.setText("停止请求已发送，但任务尚未退出。")
                box.setStandardButtons(QMessageBox.Close)
                box.setModal(False)
                self._stop_wait_box = box
            box.setInformativeText(f"原因：{reason}")
            if not box.isVisible():
                box.show()
        except RuntimeError:
            self._stop_wait_box = None

    def _cleanup_stop_feedback(self) -> None:
        self._stop_wait_timer.stop()
        self._stop_requested_at = None
        self._last_stop_reason = ""
        box = self._stop_wait_box
        self._stop_wait_box = None
        if box is not None:
            try:
                box.close()
            except RuntimeError:
                pass

    def _text_to_tempfile(self, text: str, prefix: str = "input") -> str:
        """工具方法：将大输入框中粘贴的内容临时保存为 txt 文本，以便后续流式读取。"""
        from src.core import build_output_path

        path = build_output_path("temp", f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.txt", organize=False)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
        return path

    def validate_values(self, values: dict[str, Any]) -> None:
        """子类窗口重写该方法，用于在执行任务前对输入合法性进行二次拦截校验（如判断文件是否存在、数值限制）。"""
        return None

    def prepare_run_values(self, values: dict[str, Any]) -> dict[str, Any] | None:
        """Optional main-thread preflight hook used before the worker starts."""

        return values

    def tool_config_params(self) -> list[Any]:
        """子类窗口提供配置方案的字段定义列表（如延迟、次数限制等底层策略值）。"""
        return []

    def _load_persisted_config(self) -> None:
        """自动从磁盘上加载配置参数值，优先合并全局配置。"""
        from src.core.config_store import (
            GLOBAL_ALIAS_MAP,
            GLOBAL_CONFIG_DEFAULTS,
            GLOBAL_TOOL_ID,
            load_config,
        )

        defaults = {p.key: p.default for p in self.tool_config_params()}
        merged = dict(defaults)

        if self.tool_id:
            # 第一步：注入全局参数（无条件，覆盖工具默认值）
            global_values = load_config(GLOBAL_TOOL_ID, GLOBAL_CONFIG_DEFAULTS, None)
            for gk, gv in global_values.items():
                merged[gk] = gv

            # 第二步：加载工具自身 JSON 配置
            tool_values = load_config(self.tool_id, defaults, self.current_profile)
            merged.update(tool_values)

            # 第三步：别名映射 —— 全局标准名值复制到工具的别名 key
            # 仅当工具中该别名的当前值等于 ConfigParam 默认值时才注入，
            # 如果用户已在工具对话框显式修改过，则保留工具值（用户选择优先）
            for std_name, alias_names in GLOBAL_ALIAS_MAP.items():
                if std_name in global_values:
                    for alias in alias_names:
                        if alias in defaults:
                            if tool_values.get(alias) == defaults.get(alias):
                                merged[alias] = global_values[std_name]

        self.config_values = merged

    def _open_config(self) -> None:
        """打开方案及配置参数弹窗，保存用户新调整的参数到配置类并落盘。"""
        from src.ui.config_dialog import ConfigDialog
        from src.core.config_store import save_config

        params = self.tool_config_params()
        if not params:
            QMessageBox.information(self, "提示", "此工具没有可配置的参数。")
            return
        dialog = ConfigDialog(
            self.windowTitle(), params, self.config_values, self,
            tool_id=self.tool_id, current_profile=self.current_profile,
        )
        if dialog.exec_() == ConfigDialog.Accepted:
            # 用 .update() 而非 = 赋值，保留 config_values 中的全局注入参数
            self.config_values.update(dialog.get_values())
            self.current_profile = dialog.get_selected_profile()
            if self.tool_id:
                defaults = {p.key: p.default for p in params}
                save_config(self.tool_id, self.config_values, defaults, self.current_profile)

    def run_task(self, values: dict[str, Any], log_callback, finish_callback, stop_event, pause_event) -> Any:
        """
        子类窗口核心任务入口，在子线程中执行。具体业务代码实现此接口。
        """
        raise NotImplementedError

    def _run_worker(self, values: dict[str, Any]) -> None:
        """运行在子线程的包装器，处理异常捕获并通过 PyQt 信号向主线程汇报。"""
        result = {"path": None, "paths": [], "outcome": None}
        # 将参数配置值追加合并到主输入字典中
        for key, val in self.config_values.items():
            values[key] = val

        def log_callback(message: str) -> None:
            self.signals.log.emit(str(message))

        def _record_path(path) -> None:
            """统一收口：把单一路径或路径列表并入 result，并维护代表路径。"""
            _record_task_output(result, path)

        def finish_callback(path=None) -> None:
            _record_path(path)

        try:
            returned = self.run_task(values, log_callback, finish_callback, self.stop_event, self.pause_event)
            # run_task 的返回值同样视为产出路径，并入收集结果
            _record_path(returned)

            # 单次任务产出多个文件时，自动汇总到一个工作簿（每文件一 sheet）
            if result["outcome"] is None and len(result["paths"]) > 1:
                from src.core import summarize_outputs

                summary_path = summarize_outputs(result["paths"], log_callback)
                if summary_path:
                    result["path"] = summary_path
                else:
                    # 汇总未生成（如有效文件不足），回退展示最后一个分文件路径
                    for p in result["paths"]:
                        log_callback(f"  分文件：{p}")

            finished_payload = result["outcome"] or result["path"]
            self.logger.info("Task finished: %s output=%s", self.windowTitle(), result["path"] or "")
            self.signals.finished.emit(finished_payload)
        except Exception as exc:
            self.logger.error("Task failed: %s\n%s", self.windowTitle(), traceback.format_exc())
            self.signals.failed.emit(str(exc))

    def append_log(self, message: str) -> None:
        """主线程槽：向日志界面追加一行最新消息，并强制滚动条下移到底部。"""
        self.log_text.append(str(message))
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def _set_state(self, state: str) -> None:
        """状态变化时，切换“开始/暂停/继续/停止”按钮的置灰可用状态。"""
        if state == "running":
            self.action_button.setText("暂停")
            self.action_button.setEnabled(True)
            self.stop_button.setEnabled(True)
        elif state == "paused":
            self.action_button.setText("继续")
            self.action_button.setEnabled(True)
            self.stop_button.setEnabled(True)
        else:
            self.action_button.setText("开始")
            self.action_button.setEnabled(True)
            self.stop_button.setEnabled(False)

    def _finish_success(self, output_path) -> None:
        """主线程槽：处理线程完美退出，恢复按钮状态并弹窗通告。"""
        self._cleanup_stop_feedback()
        self._set_state("idle")
        
        # 本地导入以防止循环依赖
        from src.core.outcome import RunOutcome
        
        if isinstance(output_path, RunOutcome):
            outcome = output_path
            if outcome.status == "cancelled" or self.stop_event.is_set():
                resumable = next((artifact for artifact in outcome.artifacts if "恢复" in artifact.label), None)
                message = "任务已停止，未标记为导出完成。"
                if resumable:
                    message += "\n\n采集进度已保留；再次启动相同任务可继续或导出当前数据。"
                self.append_log(f"已取消：{message.replace(chr(10), ' ')}")
                QMessageBox.warning(self, "已取消", message)
                return

            main_xlsx = outcome.output_path
            stats = outcome.stats
            
            merged_detail = ""
            if stats.extra and stats.extra.get("merged_row_count") is not None:
                merged_detail = f"\n已合并 {stats.extra['merged_row_count']} 行数据。"

            if outcome.status == "succeeded":
                msg = f"成功：全部输入已完成。\n\n结果已保存到：\n{main_xlsx}" if main_xlsx else "成功：全部输入已完成。"
                msg += merged_detail
                self.append_log("任务执行成功。")
                QMessageBox.information(self, "完成", msg)
            elif outcome.status == "partial":
                if stats.input_count > 0:
                    msg = f"部分成功：{stats.success_count}/{stats.input_count} 个输入有效，详见报告。\n\n"
                else:
                    msg = "部分成功，详见报告。\n\n"
                
                msg += f"成功 {stats.success_count}，失败 {stats.failed_count}，跳过 {stats.skipped_count}\n\n"
                msg += merged_detail
                
                if main_xlsx:
                    msg += f"\n部分结果已保存到：\n{main_xlsx}\n\n"
                else:
                    resumable = next((artifact for artifact in outcome.artifacts if "恢复" in artifact.label), None)
                    if resumable:
                        msg += "\n采集进度已保留；再次启动相同任务可继续或导出当前数据。\n\n"
                
                report_ref = next((a for a in outcome.artifacts if "report" in a.path.lower() or "报告" in a.label), None)
                if report_ref:
                    msg += f"报告已保存到：\n{report_ref.path}"
                self.append_log(f"任务部分成功：{stats.success_count} 成功, {stats.failed_count} 失败, {stats.skipped_count} 跳过")
                QMessageBox.warning(self, "部分成功", msg)
            elif outcome.status == "failed":
                err_msg = "\n".join([f"- {e.code}: {e.message}" for e in outcome.errors[:5]])
                msg = f"失败：未生成有效结果，详见错误报告。\n\n错误摘要：\n{err_msg}"
                self.append_log("任务执行失败，无有效结果输出。")
                QMessageBox.critical(self, "失败", msg)
            return

        if self.stop_event.is_set():
            self.append_log("任务已停止。")
            return
        if output_path:
            QMessageBox.information(self, "完成", f"结果已保存到：\n{output_path}")
        else:
            self.append_log("任务完成。")

    @staticmethod
    def _friendly_error_message(message: str) -> str:
        text = str(message or "")
        if "PlaywrightContextManager" in text and "_playwright" in text:
            return (
                f"{text}\n\n"
                "这通常是当前虚拟环境的 Python 版本与 Playwright 不兼容导致的。"
                "请安装 Python 3.13 后，重新运行 install_or_update.bat 重建环境。"
            )
        return text

    def _finish_error(self, message: str) -> None:
        """主线程槽：处理任务崩溃，恢复按钮状态，追加日志并弹出报错详情。"""
        self._cleanup_stop_feedback()
        self._set_state("idle")
        display_message = self._friendly_error_message(message)
        self.append_log(f"运行失败：{display_message}")
        QMessageBox.critical(self, "运行失败", display_message)

    def eventFilter(self, obj, event):
        """轮播过滤器，阻止下拉列表/数字组件响应滚轮滚动，防止操作界面发生意外滚动。"""
        if event.type() == QEvent.Wheel:
            return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:
        """
        拦截窗口关闭行为。
        如果子线程还在运行，则弹窗挽留并触发优雅的停止和 join 机制，防止子线程变成僵尸线程在后台死锁挂起。
        """
        if self.worker_thread and self.worker_thread.is_alive():
            reply = QMessageBox.question(
                self,
                "确认关闭",
                "关闭该工具窗口会停止当前任务，确定关闭吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            # 立即触发中断信号
            self.stop_event.set()
            self.pause_event.clear()
            # 最多等待子线程退出 5.0 秒，如果 5 秒内未退出也强制断开 PyQt 关联并直接放行关闭（保护主应用生命周期）
            self.worker_thread.join(timeout=5)
            try:
                # 注销信号关联防止内存泄漏或回调空指针错误
                self.signals.log.disconnect()
                self.signals.finished.disconnect()
                self.signals.failed.disconnect()
                self.signals.progress.disconnect()
            except TypeError:
                pass
        else:
            reply = QMessageBox.question(
                self,
                "确认关闭",
                "确定关闭该工具窗口吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        self._cleanup_stop_feedback()
        event.accept()
