from __future__ import annotations

import json
from pathlib import Path

from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.base import FieldSpec, SimpleToolWindow

DEFAULT_GAMES_DEFINITION = json.dumps(
    [
        {
            "name": "Genshin Impact",
            "tracks": [
                {
                    "platform": "youtube",
                    "language": "en",
                    "official_keywords": ["Genshin Impact"],
                    "candidate_keywords": ["Genshin guide", "Genshin build", "Genshin wish"],
                },
                {
                    "platform": "youtube",
                    "language": "ja",
                    "official_keywords": ["原神"],
                    "candidate_keywords": ["原神 攻略", "原神 キャラ"],
                },
                {
                    "platform": "tiktok",
                    "language": "ja",
                    "official_keywords": ["原神"],
                    "candidate_keywords": ["原神 攻略", "原神 ガチャ"],
                },
            ],
        }
    ],
    ensure_ascii=False,
    indent=2,
)


class CalibrationGamesEditor(QWidget):
    def __init__(self, initial_definition: str = "") -> None:
        super().__init__()
        self._games: list[dict[str, object]] = []
        self._current_game_index = -1
        self._current_track_index = -1
        self._loading = False
        self._build_ui()

        if initial_definition.strip():
            self.setText(initial_definition)
        else:
            self._games = [self._empty_game(1)]
            self._show_game(0, 0)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        hint = QLabel("左侧维护游戏，中间维护该游戏下的 track，右侧编辑当前 track。每个 track 独立绑定平台、语言、官方关键词和候选关键词。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        root.addLayout(body)

        game_panel = QVBoxLayout()
        game_panel.setContentsMargins(0, 0, 0, 0)
        game_panel.setSpacing(6)
        body.addLayout(game_panel, 1)

        game_controls = QHBoxLayout()
        game_controls.setContentsMargins(0, 0, 0, 0)
        game_controls.setSpacing(6)
        self.add_game_button = QPushButton("新增游戏")
        self.remove_game_button = QPushButton("删除游戏")
        self.up_game_button = QPushButton("上移")
        self.down_game_button = QPushButton("下移")
        self.import_button = QPushButton("导入 TXT/JSON")
        self.add_game_button.clicked.connect(self._add_game)
        self.remove_game_button.clicked.connect(self._remove_game)
        self.up_game_button.clicked.connect(lambda: self._move_game(-1))
        self.down_game_button.clicked.connect(lambda: self._move_game(1))
        self.import_button.clicked.connect(self._import_definition)
        for button in (
            self.add_game_button,
            self.remove_game_button,
            self.up_game_button,
            self.down_game_button,
            self.import_button,
        ):
            game_controls.addWidget(button)
        game_panel.addLayout(game_controls)

        self.game_list = QListWidget()
        self.game_list.setMinimumWidth(220)
        self.game_list.currentRowChanged.connect(self._on_current_game_changed)
        game_panel.addWidget(self.game_list, 1)

        track_panel = QVBoxLayout()
        track_panel.setContentsMargins(0, 0, 0, 0)
        track_panel.setSpacing(6)
        body.addLayout(track_panel, 1)

        track_controls = QHBoxLayout()
        track_controls.setContentsMargins(0, 0, 0, 0)
        track_controls.setSpacing(6)
        self.add_track_button = QPushButton("新增 track")
        self.remove_track_button = QPushButton("删除 track")
        self.up_track_button = QPushButton("上移")
        self.down_track_button = QPushButton("下移")
        self.add_track_button.clicked.connect(self._add_track)
        self.remove_track_button.clicked.connect(self._remove_track)
        self.up_track_button.clicked.connect(lambda: self._move_track(-1))
        self.down_track_button.clicked.connect(lambda: self._move_track(1))
        for button in (
            self.add_track_button,
            self.remove_track_button,
            self.up_track_button,
            self.down_track_button,
        ):
            track_controls.addWidget(button)
        track_panel.addLayout(track_controls)

        self.track_list = QListWidget()
        self.track_list.setMinimumWidth(220)
        self.track_list.currentRowChanged.connect(self._on_current_track_changed)
        track_panel.addWidget(self.track_list, 1)

        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(6)
        body.addWidget(editor_panel, 2)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        editor_layout.addLayout(form)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：Genshin Impact")
        self.name_edit.textChanged.connect(self._refresh_current_game_label)
        form.addRow("游戏名称", self.name_edit)

        self.platform_combo = QComboBox()
        self.platform_combo.setEditable(True)
        self.platform_combo.addItems(["youtube", "tiktok", "x_twitter"])
        self.platform_combo.currentTextChanged.connect(self._refresh_current_track_label)
        form.addRow("平台", self.platform_combo)

        self.language_combo = QComboBox()
        self.language_combo.setEditable(True)
        self.language_combo.addItems(["en", "ja", "default"])
        self.language_combo.currentTextChanged.connect(self._refresh_current_track_label)
        form.addRow("语言", self.language_combo)

        self.official_keywords_edit = QPlainTextEdit()
        self.official_keywords_edit.setPlaceholderText("每行一个官方关键词，例如：\nGenshin Impact\n原神")
        self.official_keywords_edit.setMinimumHeight(120)
        editor_layout.addWidget(QLabel("官方关键词"))
        editor_layout.addWidget(self.official_keywords_edit)

        self.candidate_keywords_edit = QPlainTextEdit()
        self.candidate_keywords_edit.setPlaceholderText("每行一个候选关键词，例如：\nGenshin guide\nGenshin build")
        self.candidate_keywords_edit.setMinimumHeight(220)
        editor_layout.addWidget(QLabel("候选关键词"))
        editor_layout.addWidget(self.candidate_keywords_edit, 1)

    def _empty_track(self) -> dict[str, object]:
        return {
            "platform": "youtube",
            "language": "en",
            "official_keywords": [],
            "candidate_keywords": [],
        }

    def _empty_game(self, number: int) -> dict[str, object]:
        return {
            "name": f"游戏 {number}",
            "tracks": [self._empty_track()],
        }

    def _clone_games(self, games: list[dict[str, object]]) -> list[dict[str, object]]:
        cloned: list[dict[str, object]] = []
        for game in games:
            cloned.append(
                {
                    "name": str(game.get("name", "")),
                    "tracks": [
                        {
                            "platform": str(track.get("platform", "")).strip().lower(),
                            "language": str(track.get("language", "")).strip().lower() or "default",
                            "official_keywords": [str(keyword) for keyword in track.get("official_keywords", []) if str(keyword).strip()],
                            "candidate_keywords": [str(keyword) for keyword in track.get("candidate_keywords", []) if str(keyword).strip()],
                        }
                        for track in game.get("tracks", [])
                        if isinstance(track, dict)
                    ],
                }
            )
        return cloned

    def _track_label(self, track: dict[str, object], index: int) -> str:
        platform = str(track.get("platform", "")).strip().lower() or "platform"
        language = str(track.get("language", "")).strip().lower() or "default"
        return f"{index + 1}. {platform} / {language}"

    def _snapshot_current_track(self) -> dict[str, object]:
        from src.tools.calibration import parse_keyword_list_text

        return {
            "platform": self.platform_combo.currentText().strip().lower(),
            "language": self.language_combo.currentText().strip().lower() or "default",
            "official_keywords": parse_keyword_list_text(self.official_keywords_edit.toPlainText()),
            "candidate_keywords": parse_keyword_list_text(self.candidate_keywords_edit.toPlainText()),
        }

    def _persist_current_state(self) -> None:
        if self._loading:
            return
        if not (0 <= self._current_game_index < len(self._games)):
            return

        game = self._games[self._current_game_index]
        game["name"] = self.name_edit.text().strip()
        tracks = game.get("tracks", [])
        if 0 <= self._current_track_index < len(tracks):
            tracks[self._current_track_index] = self._snapshot_current_track()

    def _populate_track_editor(self, track: dict[str, object]) -> None:
        from src.tools.calibration import format_keyword_list_text

        self._loading = True
        self.platform_combo.setCurrentText(str(track.get("platform", "")))
        self.language_combo.setCurrentText(str(track.get("language", "")))
        self.official_keywords_edit.setPlainText(format_keyword_list_text(track.get("official_keywords", [])))
        self.candidate_keywords_edit.setPlainText(format_keyword_list_text(track.get("candidate_keywords", [])))
        self._loading = False

    def _reload_game_list(self) -> None:
        self.game_list.clear()
        for index, game in enumerate(self._games, 1):
            name = str(game.get("name", "")).strip() or f"游戏 {index}"
            self.game_list.addItem(name)

    def _reload_track_list(self, tracks: list[dict[str, object]]) -> None:
        self.track_list.clear()
        for index, track in enumerate(tracks):
            self.track_list.addItem(self._track_label(track, index))

    def _show_game(self, game_index: int, track_index: int) -> None:
        self._loading = True
        self._reload_game_list()

        if not (0 <= game_index < len(self._games)):
            self._current_game_index = -1
            self._current_track_index = -1
            self.name_edit.clear()
            self.track_list.clear()
            self.platform_combo.setCurrentText("youtube")
            self.language_combo.setCurrentText("en")
            self.official_keywords_edit.clear()
            self.candidate_keywords_edit.clear()
            self._loading = False
            return

        game = self._games[game_index]
        tracks = game.get("tracks", [])
        if not tracks:
            tracks.append(self._empty_track())

        track_index = max(0, min(track_index, len(tracks) - 1))
        self._current_game_index = game_index
        self._current_track_index = track_index

        self.game_list.setCurrentRow(game_index)
        self.name_edit.setText(str(game.get("name", "")))
        self._reload_track_list(tracks)
        self.track_list.setCurrentRow(track_index)
        self._loading = False
        self._populate_track_editor(tracks[track_index])

    def _on_current_game_changed(self, row: int) -> None:
        if self._loading:
            return
        self._persist_current_state()
        self._show_game(row, 0)

    def _on_current_track_changed(self, row: int) -> None:
        if self._loading:
            return
        self._persist_current_state()
        if not (0 <= self._current_game_index < len(self._games)):
            return
        tracks = self._games[self._current_game_index].get("tracks", [])
        if not (0 <= row < len(tracks)):
            return
        self._current_track_index = row
        self._populate_track_editor(tracks[row])

    def _refresh_current_game_label(self) -> None:
        if self._loading:
            return
        current_row = self.game_list.currentRow()
        if 0 <= current_row < self.game_list.count():
            label = self.name_edit.text().strip() or f"游戏 {current_row + 1}"
            self.game_list.item(current_row).setText(label)

    def _refresh_current_track_label(self) -> None:
        if self._loading:
            return
        current_row = self.track_list.currentRow()
        if not (0 <= current_row < self.track_list.count()):
            return
        label = (
            f"{current_row + 1}. "
            f"{self.platform_combo.currentText().strip().lower() or 'platform'} / "
            f"{self.language_combo.currentText().strip().lower() or 'default'}"
        )
        self.track_list.item(current_row).setText(label)

    def _add_game(self) -> None:
        self._persist_current_state()
        self._games.append(self._empty_game(len(self._games) + 1))
        self._show_game(len(self._games) - 1, 0)

    def _remove_game(self) -> None:
        self._persist_current_state()
        current_row = self.game_list.currentRow()
        if not (0 <= current_row < len(self._games)):
            return
        if len(self._games) == 1:
            self._games = [self._empty_game(1)]
            self._show_game(0, 0)
            return
        self._games.pop(current_row)
        self._show_game(min(current_row, len(self._games) - 1), 0)

    def _move_game(self, offset: int) -> None:
        self._persist_current_state()
        current_row = self.game_list.currentRow()
        target_row = current_row + offset
        if not (0 <= current_row < len(self._games)):
            return
        if not (0 <= target_row < len(self._games)):
            return
        self._games[current_row], self._games[target_row] = self._games[target_row], self._games[current_row]
        self._show_game(target_row, 0)

    def _add_track(self) -> None:
        self._persist_current_state()
        if not (0 <= self._current_game_index < len(self._games)):
            return
        tracks = self._games[self._current_game_index].setdefault("tracks", [])
        tracks.append(self._empty_track())
        self._show_game(self._current_game_index, len(tracks) - 1)

    def _remove_track(self) -> None:
        self._persist_current_state()
        if not (0 <= self._current_game_index < len(self._games)):
            return
        tracks = self._games[self._current_game_index].get("tracks", [])
        current_row = self.track_list.currentRow()
        if not (0 <= current_row < len(tracks)):
            return
        if len(tracks) == 1:
            self._games[self._current_game_index]["tracks"] = [self._empty_track()]
            self._show_game(self._current_game_index, 0)
            return
        tracks.pop(current_row)
        self._show_game(self._current_game_index, min(current_row, len(tracks) - 1))

    def _move_track(self, offset: int) -> None:
        self._persist_current_state()
        if not (0 <= self._current_game_index < len(self._games)):
            return
        tracks = self._games[self._current_game_index].get("tracks", [])
        current_row = self.track_list.currentRow()
        target_row = current_row + offset
        if not (0 <= current_row < len(tracks)):
            return
        if not (0 <= target_row < len(tracks)):
            return
        tracks[current_row], tracks[target_row] = tracks[target_row], tracks[current_row]
        self._show_game(self._current_game_index, target_row)

    def _import_definition(self) -> None:
        from src.tools.calibration import parse_games_definition

        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入实验配置",
            str(Path.cwd()),
            "Text or JSON Files (*.txt *.json);;All Files (*.*)",
        )
        if not path:
            return

        try:
            games = parse_games_definition(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return

        self._games = self._clone_games(games)
        self._show_game(0, 0)

    def text(self) -> str:
        self._persist_current_state()
        return json.dumps(self._clone_games(self._games), ensure_ascii=False, indent=2)

    def setText(self, raw_definition: str) -> None:
        from src.tools.calibration import parse_games_definition

        self._games = self._clone_games(parse_games_definition(raw_definition))
        self._show_game(0, 0)


class CalibrationToolWindow(SimpleToolWindow):
    tool_id = "keyword_coverage_calibration"

    def __init__(self) -> None:
        super().__init__(
            "关键词实验数据采集",
            [
                FieldSpec(
                    "days",
                    "时间范围（过去多少天）",
                    kind="int",
                    default=7,
                    minimum=1,
                    maximum=365,
                    tooltip="设置采集时间范围。例如填 7，则工具会采集过去 7 天内的数据。",
                ),
                FieldSpec(
                    "platforms",
                    "运行平台（英文逗号分隔）",
                    default="youtube, tiktok, x_twitter",
                    tooltip="可选 youtube、tiktok、x_twitter。留空默认全部。",
                ),
                FieldSpec(
                    "youtube_api_keys",
                    "YouTube API Keys（每行一个）",
                    kind="multiline",
                    placeholder="仅在实际运行 youtube track 时必填",
                    tooltip="建议提供多个 Key 换行分隔，工具会自动轮询。",
                ),
                FieldSpec(
                    "youtube_max_results",
                    "YouTube 每词最大采集数",
                    kind="int",
                    default=10,
                    minimum=1,
                    maximum=5000,
                ),
                FieldSpec(
                    "tiktok_max_videos",
                    "TikTok 每词最大采集数",
                    kind="int",
                    default=10,
                    minimum=1,
                    maximum=5000,
                ),
                FieldSpec(
                    "x_max_scrolls",
                    "X（Twitter）每词最大滚动数",
                    kind="int",
                    default=2,
                    minimum=1,
                    maximum=5000,
                ),
                FieldSpec(
                    "x_search_tab",
                    "X Search Tab",
                    kind="combo",
                    options=("latest", "top"),
                    default="latest",
                    tooltip="默认使用 latest；如需高曝光结果可切换为 top。",
                ),
                FieldSpec(
                    "cdp_url",
                    "CDP 调试地址",
                    default="http://localhost:9222",
                ),
                FieldSpec(
                    "output_path",
                    "输出路径",
                    kind="text",
                    required=True,
                    default="output/calibration",
                    tooltip="输出根目录。若仍传文件路径，会自动按旧版兼容规则落到 run_id 目录。",
                ),
                FieldSpec(
                    "games_definition",
                    "实验配置",
                    kind="games_editor",
                    required=True,
                    default=DEFAULT_GAMES_DEFINITION,
                    tooltip="按游戏维护 track。每个 track 独立配置平台、语言、官方关键词和候选关键词。",
                ),
            ],
            height=900,
            form_stretch=2,
        )
        self._load_saved_form_values()

    def _create_field_widget(self, field: FieldSpec):
        if field.kind == "games_editor":
            widget = CalibrationGamesEditor(str(field.default or ""))
            self.widgets[field.name] = widget
            if field.tooltip:
                widget.setToolTip(field.tooltip)
            return widget
        return super()._create_field_widget(field)

    def _field_defaults(self) -> dict[str, object]:
        defaults: dict[str, object] = {}
        for field in self.fields:
            if field.kind == "int":
                defaults[field.name] = int(field.default or field.minimum)
            else:
                defaults[field.name] = str(field.default or "")
        return defaults

    def _load_saved_form_values(self) -> None:
        from src.core.config_store import load_config

        saved_values = load_config(self.tool_id, self._field_defaults(), self.current_profile)
        self._apply_form_values(saved_values)

    def _apply_form_values(self, values: dict[str, object]) -> None:
        for field in self.fields:
            if field.name not in values:
                continue
            widget = self.widgets.get(field.name)
            if widget is None:
                continue
            value = values[field.name]

            try:
                if field.kind == "multiline":
                    widget.setPlainText(str(value))
                elif field.kind == "int":
                    widget.setValue(int(value))
                elif field.kind == "combo":
                    widget.setCurrentText(str(value))
                elif field.kind == "games_editor":
                    widget.setText(str(value))
                elif field.kind in {"file", "folder"}:
                    widget.path_edit.setText(str(value))
                else:
                    widget.setText(str(value))
            except Exception:
                continue

    def _save_form_values(self, values: dict[str, object]) -> None:
        from src.core.config_store import save_config

        save_config(self.tool_id, values, self._field_defaults(), self.current_profile)

    def tool_config_params(self):
        return []

    def validate_values(self, values):
        from src.tools.calibration import parse_games_definition, parse_platforms, validate_selected_platforms

        platforms = parse_platforms(values.get("platforms", ""))
        games = parse_games_definition(values.get("games_definition", ""))
        validate_selected_platforms(games, platforms)

        active_platforms = {
            track["platform"]
            for game in games
            for track in game["tracks"]
            if track["platform"] in platforms
        }
        if "youtube" in active_platforms and not values.get("youtube_api_keys", "").strip():
            raise ValueError("请至少提供一个 YouTube API Key")

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.tools.calibration import parse_games_definition, parse_platforms, run_calibration_task

        platforms = parse_platforms(values.get("platforms", ""))
        api_keys = [key.strip() for key in values.get("youtube_api_keys", "").splitlines() if key.strip()]
        games_config = parse_games_definition(values.get("games_definition", ""))
        self._save_form_values(values)

        config = {
            "platforms": platforms,
            "time_period": {
                "days": int(values.get("days", 7)),
            },
            "youtube": {
                "api_keys": api_keys,
                "max_results": int(values.get("youtube_max_results", 10)),
            },
            "tiktok": {
                "cdp_url": values.get("cdp_url", "http://localhost:9222"),
                "max_videos": int(values.get("tiktok_max_videos", 10)),
            },
            "x_twitter": {
                "cdp_url": values.get("cdp_url", "http://localhost:9222"),
                "max_scrolls": int(values.get("x_max_scrolls", 2)),
                "x_search_tab": values.get("x_search_tab", "latest"),
            },
            "games": games_config,
        }

        try:
            actual_output = run_calibration_task(config, values["output_path"], log_callback, stop_event, pause_event)
            if not stop_event.is_set():
                finish_callback(actual_output)
        except Exception as exc:
            log_callback(f"执行异常: {exc}")
            raise
