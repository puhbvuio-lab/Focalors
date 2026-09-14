"""
工作台 GUI 主窗口实现模块。
使用 PyQt5 构建，负责爬虫插件文件的动态发现、列表分类与搜索、子进程双向管道通信管理、以及配置文件系统监听重载。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import QFileSystemWatcher, QProcess, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.app_logging import get_logger, setup_console_logging
from src.studio.discovery import SCAN_DIRS, discover_tools

logger = get_logger(__name__)


# 全部分类常量，用于指示侧边栏拉取全部列表
ALL_CATEGORY = "全部"
# 分类在侧边栏显示时的固定排序权重
CATEGORY_ORDER = [ALL_CATEGORY, "YouTube", "TikTok", "X/Twitter", "Steam", "Instagram", "Facebook", "数据处理"]


class ThreePlatformCrawlerQtApp(QMainWindow):
    """
    爬虫工作台主 GUI 窗口类。
    """

    # 子线程 -> 主线程 更新信号
    _update_available = pyqtSignal(str, str)   # latest_version, url
    _update_error = pyqtSignal(str)            # error message
    _no_update = pyqtSignal()                  # no update available
    _update_failed = pyqtSignal()              # update failed, re-enable UI
    _restart_now = pyqtSignal()                # update success, restart from main thread

    def __init__(self) -> None:
        super().__init__()

        # 连接更新信号到主线程槽
        self._update_available.connect(self._show_update_banner)
        self._update_error.connect(self._show_update_error)
        self._no_update.connect(self._show_no_update)
        self._update_failed.connect(self._on_update_failed)
        self._restart_now.connect(self._on_restart)

        from src.version import __version__
        self.setWindowTitle(f"多平台数据爬取工具 v{__version__}")
        self.resize(1040, 640)
        self.setMinimumSize(860, 560)

        # 扫描并分类发现爬虫工具
        self.tools, _ = discover_tools()
        extra_categories = sorted({tool.category for tool in self.tools} - set(CATEGORY_ORDER))
        self.category_order = [*CATEGORY_ORDER, *extra_categories]
        self.filtered_tools = []
        # 用字典记录所有处于运行状态的 QProcess 子进程，键为 tool_id
        self.processes: dict[str, QProcess] = {}
        self.current_category = ALL_CATEGORY

        self._build_ui()
        self._apply_style()
        self.refresh_tools()
        self._setup_watcher()
        
        # 启动时应用全局代理配置
        from src.core.config_store import apply_global_proxy
        apply_global_proxy()

        # 启动后延迟 500ms 异步检查更新（避免阻塞窗口初始化）
        QTimer.singleShot(500, self._check_for_updates)

    def _build_ui(self) -> None:
        """主界面布局初始化。"""
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 16, 18, 14)
        root_layout.setSpacing(12)

        # 更新提示标签，默认隐藏，位于窗口最上方
        self.update_label = QLabel("")
        self.update_label.setObjectName("updateLabel")
        self.update_label.setVisible(False)
        self.update_label.linkActivated.connect(self._on_update_clicked)
        self.update_label.setWordWrap(True)
        self.update_label.setAlignment(Qt.AlignCenter)
        root_layout.addWidget(self.update_label)

        # 顶部标题栏 + 搜索重载操作栏
        header = QHBoxLayout()
        title_box = QVBoxLayout()

        # 标题行：标题 + 版本号
        title_row = QHBoxLayout()
        self.title_label = QLabel("多平台数据爬取工具")
        self.title_label.setObjectName("titleLabel")
        title_row.addWidget(self.title_label)
        title_row.addStretch(1)

        from src.version import __version__
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("versionLabel")
        title_row.addWidget(self.version_label)

        title_box.addLayout(title_row)
        self.subtitle_label = QLabel("集中启动 YouTube、TikTok、X/Twitter、Instagram、Facebook 采集工具和数据处理工具")
        self.subtitle_label.setObjectName("subtitleLabel")
        title_box.addWidget(self.subtitle_label)
        header.addLayout(title_box, 1)

        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("搜索工具、平台或标签")
        self.search_entry.textChanged.connect(self.refresh_tools)
        header.addWidget(self.search_entry, 0)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_tools)
        header.addWidget(refresh_btn)

        self.reload_btn = QPushButton("重载工具")
        self.reload_btn.setToolTip("重新扫描组件目录，加载新增或修改的工具")
        self.reload_btn.clicked.connect(self.reload_tools)
        header.addWidget(self.reload_btn)

        self.global_config_btn = QPushButton("全局配置")
        self.global_config_btn.setToolTip("配置所有工具共享的爬取参数（超时、滚动、冷却等）")
        self.global_config_btn.clicked.connect(self._open_global_config)
        header.addWidget(self.global_config_btn)

        root_layout.addLayout(header)

        # 水平分割器，左侧分类导航，中间工具表格，右侧详情简介
        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        # 1. 左侧导航分类列表
        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        for category in self.category_order:
            item = QListWidgetItem(self._category_label(category))
            item.setData(Qt.UserRole, category)
            self.nav.addItem(item)
        self.nav.currentItemChanged.connect(self._on_category_changed)
        splitter.addWidget(self.nav)

        # 2. 中间工具数据表格
        center = QFrame()
        center.setObjectName("panel")
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(12, 12, 12, 12)
        center_layout.setSpacing(8)

        list_title = QLabel("工具列表")
        list_title.setObjectName("sectionTitle")
        center_layout.addWidget(list_title)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["工具", "分类"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.update_detail)
        self.table.itemDoubleClicked.connect(lambda *_: self.open_selected_tool())
        center_layout.addWidget(self.table, 1)
        splitter.addWidget(center)

        # 3. 右侧属性详情面板
        detail = QFrame()
        detail.setObjectName("panel")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 14, 14, 14)
        detail_layout.setSpacing(10)

        detail_title = QLabel("工具详情")
        detail_title.setObjectName("sectionTitle")
        detail_layout.addWidget(detail_title)

        self.detail_name = QLabel("未选择工具")
        self.detail_name.setObjectName("detailName")
        self.detail_name.setWordWrap(True)
        detail_layout.addWidget(self.detail_name)

        self.detail_meta = QLabel("")
        self.detail_meta.setObjectName("mutedLabel")
        self.detail_meta.setWordWrap(True)
        detail_layout.addWidget(self.detail_meta)

        self.detail_script = QLabel("")
        self.detail_script.setObjectName("scriptLabel")
        self.detail_script.setWordWrap(True)
        detail_layout.addWidget(self.detail_script)

        self.detail_summary = QTextEdit()
        self.detail_summary.setReadOnly(True)
        self.detail_summary.setObjectName("summaryBox")
        detail_layout.addWidget(self.detail_summary, 1)

        self.open_btn = QPushButton("打开工具")
        self.open_btn.setObjectName("primaryButton")
        self.open_btn.clicked.connect(self.open_selected_tool)
        detail_layout.addWidget(self.open_btn)
        splitter.addWidget(detail)

        # 界面初始化尺寸比例配额
        splitter.setSizes([180, 520, 320])
        self.setCentralWidget(root)
        self.nav.setCurrentRow(0)

        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.close)
        self.addAction(exit_action)

    def _apply_style(self) -> None:
        """全局样式设定表（采用现代化扁平风 CSS 主题）。"""
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #eef2f7;
                color: #172033;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 9pt;
            }
            #titleLabel {
                font-size: 18pt;
                font-weight: 700;
                color: #111827;
            }
            #subtitleLabel, #mutedLabel {
                color: #667085;
            }
            QLineEdit {
                background: #ffffff;
                border: 1px solid #d8e0eb;
                border-radius: 6px;
                padding: 8px 10px;
                min-width: 260px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #d8e0eb;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #edf4ff;
                border-color: #b8cae8;
            }
            #primaryButton {
                background: #2563eb;
                border-color: #2563eb;
                color: white;
                padding: 10px 16px;
            }
            #primaryButton:hover {
                background: #1d4ed8;
            }
            #panel {
                background: #ffffff;
                border: 1px solid #d8e0eb;
                border-radius: 8px;
            }
            #sectionTitle {
                background: transparent;
                font-size: 11pt;
                font-weight: 700;
            }
            #detailName {
                background: transparent;
                font-size: 15pt;
                font-weight: 700;
                color: #111827;
            }
            #scriptLabel {
                color: #475467;
                background: #f8fafc;
                border: 1px solid #e4eaf2;
                border-radius: 6px;
                padding: 7px;
            }
            #summaryBox {
                background: #f8fafc;
                border: 1px solid #e4eaf2;
                border-radius: 6px;
                padding: 8px;
            }
            #navList {
                background: #182033;
                color: #cbd5e1;
                border: 0;
                border-radius: 8px;
                padding: 8px;
                font-weight: 600;
            }
            #navList::item {
                border-radius: 6px;
                padding: 10px;
                margin: 2px 0;
            }
            #navList::item:selected {
                background: #2563eb;
                color: white;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f8fafc;
                border: 1px solid #e4eaf2;
                border-radius: 6px;
                gridline-color: #eef2f7;
                selection-background-color: #dcecff;
                selection-color: #172033;
            }
            QHeaderView::section {
                background: #f8fafc;
                color: #667085;
                border: 0;
                border-bottom: 1px solid #e4eaf2;
                padding: 8px;
                font-weight: 700;
            }
            #versionLabel {
                color: #667085;
                font-size: 11pt;
                font-weight: 500;
            }
            #updateLabel {
                color: #d97706;
                font-size: 12px;
                padding: 4px 10px;
                background: #fef3c7;
                border: 1px solid #f59e0b;
                border-radius: 4px;
            }
            """
        )
        self.table.setAlternatingRowColors(True)
        self.table.setFont(QFont("Microsoft YaHei UI", 9))

    def _category_label(self, category: str) -> str:
        """获取分类按钮的数字统计标签文本，形如 'TikTok 4'。"""
        if category == ALL_CATEGORY:
            return f"全部  {len(self.tools)}"
        count = sum(1 for tool in self.tools if tool.category == category)
        return f"{category}  {count}"

    def _on_category_changed(self, current: QListWidgetItem | None) -> None:
        """侧边分类栏选定槽：记录分类键并触发右侧表格项过滤。"""
        self.current_category = current.data(Qt.UserRole) if current else ALL_CATEGORY
        self.refresh_tools()

    def _setup_watcher(self) -> None:
        """
        初始化文件系统监听服务。
        当开发过程中在 platforms 目录下添加、修改 manifest 配置文件时，主界面会感知并自动静默刷新，简化开发测试流程。
        """
        self.watcher = QFileSystemWatcher(self)
        project_root = Path(__file__).resolve().parents[2]
        
        # 深度扫描并向监视器注册文件夹节点
        for scan_dir in SCAN_DIRS:
            base = project_root / scan_dir
            if base.is_dir():
                self.watcher.addPath(str(base))
                for p in base.rglob('*'):
                    if p.is_dir():
                        self.watcher.addPath(str(p))

        # 定时器防抖设计。当执行 git pull 等大批量文件覆盖操作时，QFileSystemWatcher
        # 会频繁抛出几百次变化信号。设定 500ms 计时器能确保所有写磁盘操作落地稳定后，只执行一次 Manifest 文件解析。
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.setInterval(500)
        self.reload_timer.timeout.connect(self.reload_tools)

        self.watcher.fileChanged.connect(self._on_fs_changed)
        self.watcher.directoryChanged.connect(self._on_fs_changed)

    def _on_fs_changed(self, path: str) -> None:
        # 只在清单发生改写或者文件夹出现增删节点时重置防抖重载
        if path.endswith(".manifest.json") or Path(path).is_dir():
            self.reload_timer.start()

    def reload_tools(self) -> None:
        """
        重新扫描工作目录。保存重载前的 UI 交互状态（选中的行和分类），重新加载完 manifest 后精准恢复原选中态。
        """
        logger.info("Reloading tools from manifests")
        
        # 备份历史聚焦项
        old_category = self.current_category
        old_tool = self.selected_tool()
        old_tool_id = old_tool.tool_id if old_tool else None

        self.tools, errors = discover_tools()
        extra_categories = sorted({tool.category for tool in self.tools} - set(CATEGORY_ORDER))
        self.category_order = [*CATEGORY_ORDER, *extra_categories]

        # 刷新侧边分类树
        self.nav.clear()
        found_category = False
        for i, category in enumerate(self.category_order):
            item = QListWidgetItem(self._category_label(category))
            item.setData(Qt.UserRole, category)
            self.nav.addItem(item)
            if category == old_category:
                self.nav.setCurrentRow(i)
                found_category = True
        
        if not found_category:
            self.nav.setCurrentRow(0)

        self.refresh_tools()
        
        # 恢复表格的高亮聚焦选定行
        if old_tool_id:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item and item.data(Qt.UserRole) == old_tool_id:
                    self.table.selectRow(row)
                    break

        logger.info("Reloaded %d tools", len(self.tools))
        if errors:
            err_msg = "\n".join(errors)
            QMessageBox.warning(self, "工具加载部分失败", f"部分工具配置加载失败：\n\n{err_msg}")
        else:
            self.reload_btn.setText("✓ 重载成功")
            # 1.5 秒后清除成功复位状态按钮文本
            QTimer.singleShot(1500, lambda: self.reload_btn.setText("重载工具"))

    def _open_global_config(self) -> None:
        """打开全局配置对话框，编辑所有工具共享的爬取参数。"""
        from src.ui.config_dialog import ConfigDialog
        from src.core.config_store import (
            GLOBAL_CONFIG_PARAMS,
            GLOBAL_TOOL_ID,
            load_config,
            save_config,
            apply_global_proxy,
        )

        defaults = {p.key: p.default for p in GLOBAL_CONFIG_PARAMS}
        current = load_config(GLOBAL_TOOL_ID, defaults, None)
        dialog = ConfigDialog("全局配置", GLOBAL_CONFIG_PARAMS, current, self, tool_id=GLOBAL_TOOL_ID)
        if dialog.exec_() == ConfigDialog.Accepted:
            values = dialog.get_values()
            save_config(GLOBAL_TOOL_ID, values, defaults, None)
            apply_global_proxy()

    def refresh_tools(self) -> None:
        """
        核心刷新动作：提取检索过滤文本与分类标签，比对 ToolSpec 信息，清空并重新填装 QTableWidget 节点。
        """
        query = self.search_entry.text().strip().lower()
        category = self.current_category

        self.filtered_tools = []
        for tool in self.tools:
            if category != ALL_CATEGORY and tool.category != category:
                continue
            haystack = " ".join([tool.name, tool.category, tool.summary, " ".join(tool.tags)]).lower()
            if query and query not in haystack:
                continue
            self.filtered_tools.append(tool)

        self.table.setRowCount(len(self.filtered_tools))
        for row, tool in enumerate(self.filtered_tools):
            for column, text in enumerate([tool.name, tool.category]):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, tool.tool_id)
                self.table.setItem(row, column, item)

        # 默认高亮选择刷新出的首行数据
        if self.filtered_tools:
            self.table.selectRow(0)
            self.update_detail()
        else:
            self.clear_detail()

    def selected_tool(self):
        """获取当前表格中选中的 ToolSpec 工具对象，如无选中则返回 None。"""
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        row = selected_rows[0].row()
        if row < 0 or row >= len(self.filtered_tools):
            return None
        return self.filtered_tools[row]

    def update_detail(self) -> None:
        """在右侧详情卡片区域渲染显示当前高亮工具的详细说明与文件路径。"""
        tool = self.selected_tool()
        if tool is None:
            self.clear_detail()
            return
        self.detail_name.setText(tool.name)
        tags = " / ".join(tool.tags) if tool.tags else "无标签"
        self.detail_meta.setText(f"{tool.category}    {tags}")
        self.detail_script.setText(tool.implementation_path or tool.entrypoint)
        self.detail_summary.setPlainText(tool.summary)
        self.open_btn.setEnabled(True)
        self.open_btn.setText("打开工具")

    def clear_detail(self) -> None:
        """清空详情展示。"""
        self.detail_name.setText("未选择工具")
        self.detail_meta.setText("")
        self.detail_script.setText("")
        self.detail_summary.setPlainText("请选择左侧工具。")
        self.open_btn.setEnabled(False)

    def open_selected_tool(self) -> None:
        """
        调起选定的爬虫子工具。
        为了防范某些平台爬虫异常崩溃导致主控面板一并闪退挂起，所有子工具窗口均以
        独立的 QProcess 后台子进程方式拉起（拉起 python -m src.studio.tool_runner ），
        从而实现了进程间的沙箱式隔离，保证了主应用的健壮性。
        """
        tool = self.selected_tool()
        if tool is None:
            return
        if self._is_tool_running(tool.tool_id):
            logger.info("Tool already running: %s (%s)", tool.name, tool.tool_id)
            QMessageBox.information(self, "工具已打开", f"{tool.name} 已经打开。")
            return

        logger.info("Launching tool process: %s (%s)", tool.name, tool.tool_id)
        process = QProcess(self)
        # 用当前相同的 python 解释器执行 tool_runner 模块
        process.setProgram(sys.executable)
        
        # 传递当前所有的环境变量，包含系统代理 HTTP_PROXY 等
        from PyQt5.QtCore import QProcessEnvironment
        import os
        env = QProcessEnvironment.systemEnvironment()
        for key, val in os.environ.items():
            env.insert(key, val)
        process.setProcessEnvironment(env)
        
        process.setArguments(["-m", "src.studio.tool_runner", "--tool-id", tool.tool_id])
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        # 开启输出合并管道，将子进程的标准错误和标准输出流合并归纳，方便主应用接收打印
        process.setProcessChannelMode(QProcess.MergedChannels)
        
        # 绑定进程状态销毁监测槽
        process.finished.connect(lambda exit_code, exit_status, tool_id=tool.tool_id: self._tool_finished(tool_id, exit_code, exit_status))
        process.errorOccurred.connect(lambda error, tool_id=tool.tool_id: self._tool_error(tool_id, error))
        process.readyReadStandardOutput.connect(lambda tool_id=tool.tool_id: self._read_tool_output(tool_id))
        
        self.processes[tool.tool_id] = process
        process.start()
        self.refresh_tools()

    def _read_tool_output(self, tool_id: str) -> None:
        """子进程管道输出捕获槽，负责将子窗口的控制台输出转印输出至主进程终端。"""
        process = self.processes.get(tool_id)
        if process is not None:
            text = bytes(process.readAllStandardOutput()).decode(errors="replace")
            if text:
                print(text, end="")
                sys.stdout.flush()

    def _tool_finished(self, tool_id: str, exit_code: int, exit_status) -> None:
        """子进程完美（或非完美）终结后的清理回调。"""
        logger.info("Tool process finished: %s exit_code=%s exit_status=%s", tool_id, exit_code, exit_status)
        if tool_id in self.processes:
            self.processes.pop(tool_id, None)
            self.refresh_tools()

    def _tool_error(self, tool_id: str, error) -> None:
        """子进程启动失败或异常断开后的错误捕获。"""
        logger.error("Tool process error: %s error=%s", tool_id, error)
        if tool_id in self.processes:
            self.processes.pop(tool_id, None)
            self.refresh_tools()

    def _is_tool_running(self, tool_id: str) -> bool:
        """检查指定子窗口进程是否存活。"""
        process = self.processes.get(tool_id)
        return bool(process and process.state() != QProcess.NotRunning)

    # ── 更新检查相关方法 ────────────────────────────────────────

    def _check_for_updates(self) -> None:
        """后台线程检查版本更新，结果通过右上角标签展示。

        有更新：显示可点击的更新提示。
        无更新：标签保持隐藏。
        检查失败：显示失败原因。
        """
        from src.version import __version__
        from src.core.updater import check_for_updates
        import threading

        def _worker() -> None:
            try:
                has_update, latest, url = check_for_updates(__version__)
                if has_update and latest and url:
                    self._update_available.emit(latest, url)
                else:
                    logger.info("当前已是最新版本 %s。", __version__)
                    self._no_update.emit()
            except Exception as e:
                logger.warning("检查更新失败：%s", e)
                err_msg = f"检查更新失败：{e}"
                self._update_error.emit(err_msg)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _show_update_banner(self, latest_version: str, url: str) -> None:
        """在窗口最上方显示新版本更新提示，并记住待更新的 tag。"""
        from src.version import __version__

        self._pending_tag = f"v{latest_version}"
        text = (
            f'<a href="{url}" style="color:#92400e;">'
            f'发现新版本 v{latest_version}，当前版本为 {__version__}，点击更新'
            f'</a>'
        )
        self.update_label.setText(text)
        self.update_label.setStyleSheet(
            "color: #92400e; font-size: 13px; padding: 8px 16px;"
            " background: #fef3c7; border: 1px solid #f59e0b; border-radius: 6px; margin-bottom: 2px;"
        )
        self.update_label.setVisible(True)
        logger.info("发现新版本 v%s，当前版本 %s", latest_version, __version__)

    def _on_update_failed(self) -> None:
        """更新失败时恢复界面操作。"""
        self.setEnabled(True)

    def _on_restart(self) -> None:
        """在主线程中安全退出应用。"""
        QApplication.quit()

    def _show_no_update(self) -> None:
        """显示已是最新版本，3 秒后自动隐藏。"""
        self.update_label.setText("已是最新版本")
        self.update_label.setVisible(True)
        self.update_label.setStyleSheet(
            "color: #065f46; font-size: 13px; padding: 8px 16px;"
            " background: #d1fae5; border: 1px solid #10b981; border-radius: 6px; margin-bottom: 2px;"
        )
        QTimer.singleShot(3000, self.update_label.hide)

    def _on_update_clicked(self, url: str) -> None:
        """点击更新提示后，禁止界面操作，后台更新到 release tag 并自动重启。"""
        # 停掉文件监听，否则更新替换目录时会报错
        if hasattr(self, "watcher") and self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())

        self.setEnabled(False)
        self.update_label.setText("正在更新，请勿关闭窗口…")
        self.update_label.setVisible(True)
        self.update_label.setStyleSheet(
            "color: #1d4ed8; font-size: 14px; padding: 10px 16px;"
            " background: #dbeafe; border: 1px solid #3b82f6; border-radius: 6px; margin-bottom: 2px;"
        )

        tag = getattr(self, "_pending_tag", None)
        if not tag:
            self._update_error.emit("更新失败：无法获取目标版本号")
            self._update_failed.emit()
            return
        import threading

        def _do_update() -> None:
            from src.core.updater import DEFAULT_UPDATE_REPO_NAME, DEFAULT_UPDATE_REPO_OWNER
            from src.core.hot_updater import run_hot_update, restart_app
            success, msg = run_hot_update(tag, DEFAULT_UPDATE_REPO_OWNER, DEFAULT_UPDATE_REPO_NAME)
            if success:
                restart_app()
                self._restart_now.emit()
            else:
                self._update_error.emit(msg)
                self._update_failed.emit()

        t = threading.Thread(target=_do_update, daemon=True)
        t.start()

    def _show_update_error(self, message: str) -> None:
        """在右上角显示检查失败信息。"""
        self.update_label.setText(message)
        self.update_label.setVisible(True)
        self.update_label.setStyleSheet("color: #991b1b; font-size: 13px; padding: 8px 16px; background: #fef2f2; border: 1px solid #ef4444; border-radius: 6px; margin-bottom: 2px;")

    # ── 进程管理与窗口关闭 ────────────────────────────────────────

    def closeEvent(self, event) -> None:
        """
        拦截主窗口退出行为。
        在退出时实施进程级联终结机制：优雅地发出 terminate 退出信号，
        轮询等待子进程处理完善后工作并退出（最长 1.5 秒），对超时进程实施硬杀 kill 动作，防子窗口进程失控遗留在系统后台。
        """
        running = [tool_id for tool_id in self.processes if self._is_tool_running(tool_id)]
        if running:
            message = "关闭主窗口会关闭已打开的工具窗口，确定关闭吗？"
        else:
            message = "确定关闭多平台数据爬取工具吗？"
        reply = QMessageBox.question(self, "确认关闭", message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            event.ignore()
            return
            
        # 1. 尝试优雅发出退出信号
        for process in list(self.processes.values()):
            if process.state() != QProcess.NotRunning:
                process.terminate()
                
        # 2. 循环等待退出以完成磁盘写入等工作，如果超时未退出则执行硬杀
        for process in list(self.processes.values()):
            if process.state() != QProcess.NotRunning:
                process.waitForFinished(1500)
                if process.state() != QProcess.NotRunning:
                    process.kill()
        event.accept()


def main() -> None:
    """GUI 应用主入口函数。"""
    setup_console_logging()
    from src.core.config_store import generate_all_defaults
    try:
        generate_all_defaults()
    except OSError:
        pass
    logger.info("Starting main window")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("多平台数据爬取工具")
    window = ThreePlatformCrawlerQtApp()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
