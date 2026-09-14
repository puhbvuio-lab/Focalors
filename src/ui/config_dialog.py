"""
配置对话框定义模块。
提供通用的 GUI 弹窗以供用户查看、调优底层高级策略参数（如延迟、超时、最大限制等），并支持多套命名参数方案（Profile）的存取与删除。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


@dataclass
class ConfigParam:
    """
    高级配置参数的声明规范类。
    """
    key: str                    # 参数键名
    label: str                  # 参数显示名称
    kind: str = "int"          # 参数渲染类型：int | float | combo | bool | text
    default: Any = 0            # 默认值
    minimum: float = 0          # 数字上限
    maximum: float = 999999
    step: float = 1            # 递增步长
    decimals: int = 1          # 浮点数保留小数位
    options: tuple[str, ...] = ()  # combo 模式选项
    tooltip: str = ""           # 浮动工具栏提示说明文本


class ConfigDialog(QDialog):
    """
    高级策略参数配置弹窗。
    提供参数值调整、恢复默认值以及“多方案 Profile 另存为/加载/删除”的一套完整管理界面。
    """

    def __init__(
        self,
        title: str,
        params: list[ConfigParam],
        current_values: dict[str, Any] | None = None,
        parent: QWidget | None = None,
        tool_id: str = "",
        current_profile: str | None = None,
    ) -> None:
        """
        Args:
            title: 弹窗标题
            params: 字段参数声明列表
            current_values: 传入的当前参数字典
            parent: 父级窗口指针
            tool_id: 工具 ID，用于落盘读写
            current_profile: 当前激活的配置方案名
        """
        super().__init__(parent)
        self.setWindowTitle(f"{title} — 参数配置")
        self.resize(580, 460)
        self.params = params
        self.tool_id = tool_id
        self._profile = current_profile
        self._defaults = {p.key: p.default for p in params}
        self._current = dict(current_values or {})
        self._widgets: dict[str, Any] = {}
        self._profile_combo: QComboBox | None = None
        self._delete_btn: QPushButton | None = None
        self._build_ui()
        if self._profile:
            from src.core.config_store import load_config
            self._current = load_config(self.tool_id, self._defaults, self._profile)
        self._apply_current_or_defaults()

    def _build_ui(self) -> None:
        """动态绘制配置对话框布局，渲染方案栏与字段区域。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        header = QLabel("调整爬取行为参数。可选择已保存的配置方案，或另存为新方案。")
        header.setWordWrap(True)
        header.setStyleSheet("color: #667085; font-size: 9pt;")
        root.addWidget(header)

        # 如果工具声明了 ID，则渲染顶部方案方案栏
        if self.tool_id:
            profile_row = QHBoxLayout()
            profile_row.setSpacing(8)
            profile_row.addWidget(QLabel("配置方案："))
            self._profile_combo = QComboBox()
            self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
            self._refresh_profile_list()
            profile_row.addWidget(self._profile_combo, 1)
            save_as_btn = QPushButton("另存为…")
            save_as_btn.clicked.connect(self._on_save_as)
            profile_row.addWidget(save_as_btn)
            self._delete_btn = QPushButton("删除")
            self._delete_btn.clicked.connect(self._on_delete_profile)
            self._delete_btn.setEnabled(self._profile is not None)
            profile_row.addWidget(self._delete_btn)
            root.addLayout(profile_row)

        # 中部滚动区域，防止配置字段过多溢出窗口
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        root.addWidget(scroll, 1)

        form_widget = QWidget()
        scroll.setWidget(form_widget)
        form = QFormLayout(form_widget)
        form.setLabelAlignment(Qt.AlignRight)
        form.setVerticalSpacing(8)
        form.setHorizontalSpacing(10)

        # 遍历动态渲染
        for param in self.params:
            widget = self._create_widget(param)
            label = QLabel(param.label)
            if param.tooltip:
                label.setToolTip(param.tooltip)
            form.addRow(label, widget)
            self._widgets[param.key] = widget

        # 底部确定/恢复默认/取消按钮栏
        buttons = QHBoxLayout()
        restore_btn = QPushButton("恢复默认值")
        restore_btn.clicked.connect(self._apply_defaults)
        buttons.addWidget(restore_btn)
        buttons.addStretch(1)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryButton")
        save_btn.clicked.connect(self._on_save)
        buttons.addWidget(save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        root.addLayout(buttons)

        self.setStyleSheet("""
            QDialog {
                background: #f6f8fb;
                color: #172033;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 9pt;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
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
            #primaryButton {
                background: #2563eb;
                border-color: #2563eb;
                color: white;
            }
            #primaryButton:hover {
                background: #1d4ed8;
            }
            QScrollArea {
                border: 1px solid #e4eaf2;
                border-radius: 6px;
                background: #ffffff;
            }
        """)

    def _create_widget(self, param: ConfigParam):
        """根据 ConfigParam.kind 类型构建对应的输入控件。"""
        if param.kind == "int":
            widget = QSpinBox()
            widget.setRange(int(param.minimum), int(param.maximum))
            widget.setValue(int(param.default))
            widget.setSingleStep(max(1, int(param.step)))
        elif param.kind == "float":
            widget = QDoubleSpinBox()
            widget.setRange(float(param.minimum), float(param.maximum))
            widget.setValue(float(param.default))
            widget.setSingleStep(max(0.1, float(param.step)))
            widget.setDecimals(max(1, int(param.decimals)))
        elif param.kind == "combo":
            widget = QComboBox()
            widget.addItems(param.options)
            if str(param.default) in param.options:
                widget.setCurrentText(str(param.default))
        elif param.kind == "bool":
            widget = QCheckBox()
            widget.setChecked(bool(param.default))
        elif param.kind == "text":
            widget = QLineEdit()
            widget.setText(str(param.default or ""))
        else:
            widget = QSpinBox()
            widget.setRange(int(param.minimum), int(param.maximum))
            widget.setValue(int(param.default))
        widget.installEventFilter(self)
        if param.tooltip:
            widget.setToolTip(param.tooltip)
        return widget

    def eventFilter(self, obj, event):
        """阻止高级参数组件响应滚轮滚动，防止修改参数时误操作。"""
        if event.type() == QEvent.Wheel:
            return True
        return super().eventFilter(obj, event)

    def _apply_current_or_defaults(self) -> None:
        """从缓存字典中取值并应用回 GUI 输入框，如缺失则采用默认配置参数。"""
        for param in self.params:
            widget = self._widgets.get(param.key)
            if widget is None:
                continue
            value = self._current.get(param.key, param.default)
            if param.kind == "int":
                widget.setValue(int(value))
            elif param.kind == "float":
                widget.setValue(float(value))
            elif param.kind == "combo" and str(value) in param.options:
                widget.setCurrentText(str(value))
            elif param.kind == "bool":
                widget.setChecked(bool(value))
            elif param.kind == "text":
                widget.setText(str(value or ""))

    def _on_save(self) -> None:
        """确认保存。强行同步各微调框中用户手动键入未按回车的文本。"""
        for widget in self._widgets.values():
            if hasattr(widget, "interpretText"):
                widget.interpretText()
        self.accept()

    def _apply_defaults(self) -> None:
        """将页面所有的输入控件恢复为出厂配置（仅重设显示，点保存才真正生效）。"""
        for param in self.params:
            widget = self._widgets.get(param.key)
            if widget is None:
                continue
            if param.kind == "int":
                widget.setValue(int(param.default))
            elif param.kind == "float":
                widget.setValue(float(param.default))
            elif param.kind == "combo" and str(param.default) in param.options:
                widget.setCurrentText(str(param.default))
            elif param.kind == "bool":
                widget.setChecked(bool(param.default))
            elif param.kind == "text":
                widget.setText(str(param.default or ""))

    def get_values(self) -> dict[str, Any]:
        """
        导出配置对话框当前界面的所有最新键值对配置数据。
        """
        result: dict[str, Any] = {}
        for param in self.params:
            widget = self._widgets.get(param.key)
            if widget is None:
                continue
            if param.kind in ("int", "float"):
                widget.interpretText()
            if param.kind == "int":
                result[param.key] = widget.value()
            elif param.kind == "float":
                result[param.key] = widget.value()
            elif param.kind == "combo":
                result[param.key] = widget.currentText()
            elif param.kind == "bool":
                result[param.key] = widget.isChecked()
            elif param.kind == "text":
                result[param.key] = widget.text()
        return result

    def get_selected_profile(self) -> str | None:
        """返回用户最终选择的方案名，None 表示默认方案。"""
        return self._profile

    def _refresh_profile_list(self) -> None:
        """刷新下拉配置方案列表。"""
        if self._profile_combo is None:
            return
        from src.core.config_store import list_profiles

        # blockSignals(True) 暂时屏蔽事件，防止下拉框清除重建过程中
        # 频繁触发 currentIndexChanged 槽函数去重复加载 IO，导致界面抖动甚至死锁
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        profiles = list_profiles(self.tool_id)
        for display_name, profile_key in profiles:
            self._profile_combo.addItem(display_name, profile_key)
        idx = self._profile_combo.findData(self._profile)
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.blockSignals(False)

    def _on_profile_changed(self, _index: int) -> None:
        """方案切换回调：加载新方案对应的配置文件并刷入界面。"""
        if self._profile_combo is None:
            return
        self._profile = self._profile_combo.currentData()
        if self._delete_btn is not None:
            self._delete_btn.setEnabled(self._profile is not None)
        from src.core.config_store import load_config

        values = load_config(self.tool_id, self._defaults, self._profile)
        self._current = values
        self._apply_current_or_defaults()

    def _on_delete_profile(self) -> None:
        """删除当前激活的自定义方案文件并切回默认参数配置。"""
        if self._profile is None:
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除配置方案「{self._profile}」吗？此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        from src.core.config_store import delete_profile

        delete_profile(self.tool_id, self._profile)
        self._profile = None
        self._refresh_profile_list()
        from src.core.config_store import load_config

        self._current = load_config(self.tool_id, self._defaults, None)
        self._apply_current_or_defaults()

    def _on_save_as(self) -> None:
        """另存为新方案命名配置。"""
        name, ok = QInputDialog.getText(self, "另存为新方案", "请输入新方案名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        from src.core.config_store import save_config

        values = self.get_values()
        save_config(self.tool_id, values, self._defaults, name)
        self._profile = name
        self._refresh_profile_list()
        self.accept()

