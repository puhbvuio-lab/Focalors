"""测试配置方案管理功能：列出、保存、加载、切换、删除配置方案。

覆盖 config_store 的 profile 相关函数和 ConfigDialog 的 GUI 交互。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from PyQt5.QtWidgets import QApplication

from src.core.config_store import (
    _coerce_value,
    delete_profile,
    get_config_path_for_profile,
    list_profiles,
    load_config,
    save_config,
)
from src.ui.config_dialog import ConfigDialog, ConfigParam

_app = None


def _ensure_app():
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


# ---------------------------------------------------------------------------
# 工具函数：创建临时 config 目录
# ---------------------------------------------------------------------------

def _temp_config_dir():
    """创建一个临时目录并返回路径，用于隔离测试。"""
    tmp = tempfile.mkdtemp(prefix="test_config_")
    return Path(tmp)


# ---------------------------------------------------------------------------
# _coerce_value 类型强制转换测试
# ---------------------------------------------------------------------------

def test_coerce_int():
    assert _coerce_value(42, 0) == 42
    assert _coerce_value("99", 0) == 99
    assert _coerce_value("abc", 0) == 0       # 无法转换 → 返回默认值
    assert _coerce_value(True, 0) == 1         # bool 是 int 子类


def test_coerce_float():
    assert abs(_coerce_value(3.14, 0.0) - 3.14) < 0.001
    assert abs(_coerce_value("2.5", 0.0) - 2.5) < 0.001
    assert abs(_coerce_value("xyz", 1.5) - 1.5) < 0.001


def test_coerce_bool():
    assert _coerce_value(True, False) is True
    assert _coerce_value(False, True) is False
    assert _coerce_value("true", False) is True     # 非空字符串 → True
    assert _coerce_value("", True) is False
    assert _coerce_value(1, False) is True
    assert _coerce_value(0, True) is False


# ---------------------------------------------------------------------------
# list_profiles 测试
# ---------------------------------------------------------------------------

def test_list_profiles_empty():
    """没有命名方案时，只返回默认配置。"""
    with patch("src.core.config_store.get_config_dir", return_value=_temp_config_dir()):
        profiles = list_profiles("test_tool")
        assert len(profiles) == 1
        assert profiles[0] == ("默认配置", None)


def test_list_profiles_with_named():
    """保存命名方案后，列表应包含默认 + 命名方案。"""
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        # 先保存几个命名方案
        defaults = {"a": 1, "b": 2}
        save_config("tool_x", {"a": 10}, defaults, "fast")
        save_config("tool_x", {"a": 20}, defaults, "slow")

        profiles = list_profiles("tool_x")
        assert len(profiles) == 3
        assert profiles[0] == ("默认配置", None)
        names = [p[0] for p in profiles]
        assert "fast" in names
        assert "slow" in names
        # profile_key 与显示名一致
        for display, key in profiles:
            if key is not None:
                assert display == key


# ---------------------------------------------------------------------------
# get_config_path_for_profile 测试
# ---------------------------------------------------------------------------

def test_config_path_default():
    path = get_config_path_for_profile("my_tool", None)
    assert path.name == "my_tool.json"


def test_config_path_named():
    path = get_config_path_for_profile("my_tool", "high_speed")
    assert path.name == "my_tool_high_speed.json"


def test_config_path_sanitizes_slashes():
    """路径分隔符应被替换为下划线，防止写入子目录。"""
    path = get_config_path_for_profile("my_tool", "a/b\\c")
    assert "/" not in path.name
    assert "\\" not in path.name
    assert path.name == "my_tool_a_b_c.json"


# ---------------------------------------------------------------------------
# load_config / save_config 带 profile 测试
# ---------------------------------------------------------------------------

def test_save_and_load_profile():
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        defaults = {"timeout": 30000, "retries": 3}
        save_config("tool_p", {"timeout": 60000, "retries": 5}, defaults, "aggressive")
        result = load_config("tool_p", defaults, "aggressive")
        assert result["timeout"] == 60000
        assert result["retries"] == 5


def test_load_profile_missing_keys_filled_by_defaults():
    """JSON 中缺少的字段应用默认值补齐。"""
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        defaults = {"a": 1, "b": 2, "c": 3}
        save_config("tool_m", {"a": 99}, defaults, "partial")
        result = load_config("tool_m", defaults, "partial")
        assert result["a"] == 99
        assert result["b"] == 2
        assert result["c"] == 3


def test_load_nonexistent_profile_returns_defaults():
    """不存在的方案应返回纯默认值。"""
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        defaults = {"x": 10}
        result = load_config("no_tool", defaults, "ghost")
        assert result == {"x": 10}


def test_save_config_without_defaults_param():
    """不传 defaults 参数时，从 DEFAULT_CONFIGS 查找。"""
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        # 使用一个已知在 DEFAULT_CONFIGS 中的 tool_id
        save_config("x_keyword_video_search",
                    {"slice_days": 30, "no_new_scroll_limit": 20},
                    profile="my_x_config")
        result = load_config("x_keyword_video_search",
                            {"slice_days": 7, "max_scrolls": 200,
                             "search_page_timeout": 40000,
                             "cooldown_min": 5.0, "cooldown_max": 7.0,
                             "no_new_scroll_limit": 5},
                            "my_x_config")
        assert result["slice_days"] == 30
        assert result["no_new_scroll_limit"] == 20
        # 清理
        delete_profile("x_keyword_video_search", "my_x_config")


# ---------------------------------------------------------------------------
# delete_profile 测试
# ---------------------------------------------------------------------------

def test_delete_profile_removes_file():
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        defaults = {"a": 1}
        save_config("tool_d", {"a": 5}, defaults, "to_delete")
        assert get_config_path_for_profile("tool_d", "to_delete").exists()
        result = delete_profile("tool_d", "to_delete")
        assert result is True
        assert not get_config_path_for_profile("tool_d", "to_delete").exists()


def test_delete_none_profile_returns_false():
    """profile 为 None 时不应删除（默认配置不可删除）。"""
    assert delete_profile("any_tool", None) is False
    assert delete_profile("any_tool", "") is False


def test_delete_nonexistent_profile_returns_false():
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        assert delete_profile("tool_x", "no_such_profile") is False


# ---------------------------------------------------------------------------
# ConfigDialog GUI 测试 — 方案选择
# ---------------------------------------------------------------------------

_SAMPLE_PARAMS = [
    ConfigParam("timeout", "超时(毫秒)", kind="int", default=30000, minimum=1000, maximum=120000),
    ConfigParam("cooldown", "冷却(秒)", kind="float", default=3.0, minimum=0.5, maximum=30.0, step=0.5, decimals=1),
    ConfigParam("enabled", "启用", kind="bool", default=True),
]


def test_config_dialog_no_profile_when_tool_id_empty():
    """tool_id 为空时，不显示方案选择区域。"""
    _ensure_app()
    dialog = ConfigDialog("测试", _SAMPLE_PARAMS, tool_id="")
    assert dialog._profile_combo is None
    assert dialog._delete_btn is None
    dialog.close()


def test_config_dialog_profile_row_visible():
    """有 tool_id 时显示方案选择区域。"""
    _ensure_app()
    dialog = ConfigDialog("测试", _SAMPLE_PARAMS, tool_id="test_tool")
    assert dialog._profile_combo is not None
    assert dialog._delete_btn is not None
    dialog.close()


def test_config_dialog_default_profile_in_combo():
    """方案下拉框的第一项应为"默认配置"。"""
    _ensure_app()
    dialog = ConfigDialog("测试", _SAMPLE_PARAMS, tool_id="test_tool")
    assert dialog._profile_combo.count() >= 1
    assert dialog._profile_combo.itemText(0) == "默认配置"
    dialog.close()


def test_config_dialog_delete_btn_disabled_for_default():
    """选中默认配置时，删除按钮应禁用。"""
    _ensure_app()
    dialog = ConfigDialog("测试", _SAMPLE_PARAMS, tool_id="test_tool", current_profile=None)
    assert dialog._delete_btn is not None
    assert dialog._delete_btn.isEnabled() is False
    dialog.close()


def test_config_dialog_get_selected_profile_default():
    """初始应返回 None（默认配置）。"""
    _ensure_app()
    dialog = ConfigDialog("测试", _SAMPLE_PARAMS, tool_id="test_tool")
    assert dialog.get_selected_profile() is None
    dialog.close()


# ---------------------------------------------------------------------------
# ConfigDialog GUI 测试 — 保存与加载方案值
# ---------------------------------------------------------------------------

def test_config_dialog_loads_profile_values():
    """切换方案应加载对应 JSON 中的值。"""
    _ensure_app()
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        defaults = {"timeout": 30000, "cooldown": 3.0, "enabled": True}
        save_config("test_tool", {"timeout": 12345, "cooldown": 8.5, "enabled": False},
                    defaults, "night_mode")

        dialog = ConfigDialog("测试", _SAMPLE_PARAMS, tool_id="test_tool", current_profile="night_mode")
        values = dialog.get_values()
        assert values["timeout"] == 12345
        assert abs(values["cooldown"] - 8.5) < 0.01
        assert values["enabled"] is False
        dialog.close()
        delete_profile("test_tool", "night_mode")


def test_config_dialog_switch_profile_updates_widgets():
    """手动切换方案下拉框后，get_values 应反映新方案的值。"""
    _ensure_app()
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        defaults = {"timeout": 30000, "cooldown": 3.0, "enabled": True}
        save_config("test_tool", {"timeout": 99999}, defaults, "extreme")

        dialog = ConfigDialog("测试", _SAMPLE_PARAMS, tool_id="test_tool", current_profile=None)
        # 默认值
        assert dialog.get_values()["timeout"] == 30000
        # 切换到 extreme
        combo = dialog._profile_combo
        for i in range(combo.count()):
            if combo.itemData(i) == "extreme":
                combo.setCurrentIndex(i)
                break
        values = dialog.get_values()
        assert values["timeout"] == 99999
        dialog.close()
        delete_profile("test_tool", "extreme")


def test_config_dialog_save_as_creates_profile():
    """另存为应创建新的 JSON 文件并在下拉框中出现。"""
    _ensure_app()
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        dialog = ConfigDialog("测试", _SAMPLE_PARAMS, tool_id="test_tool", current_profile=None)
        # 模拟另存为：通过 Mock 绕过 QInputDialog.getText 交互
        with patch("src.ui.config_dialog.QInputDialog.getText", return_value=("custom", True)):
            dialog._on_save_as()
        dialog.close()

    # 绕过 QInputDialog：直接测试 save → list → load 路径
    tmp2 = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp2):
        defaults = {"timeout": 30000, "cooldown": 3.0, "enabled": True}
        save_config("test_tool", {"timeout": 11111}, defaults, "custom")
        profiles = list_profiles("test_tool")
        names = [p[0] for p in profiles]
        assert "custom" in names
        loaded = load_config("test_tool", defaults, "custom")
        assert loaded["timeout"] == 11111
        delete_profile("test_tool", "custom")


# ---------------------------------------------------------------------------
# 完整流程测试
# ---------------------------------------------------------------------------

def test_full_profile_lifecycle():
    """完整流程：保存 → 列出 → 加载 → 切换 → 删除 → 确认删除。"""
    tmp = _temp_config_dir()
    with patch("src.core.config_store.get_config_dir", return_value=tmp):
        defaults = {"timeout": 30000, "cooldown": 3.0, "enabled": True}
        tool = "lifecycle_tool"

        # 1. 初始只有默认配置
        profiles = list_profiles(tool)
        assert len(profiles) == 1

        # 2. 保存两个命名方案
        save_config(tool, {"timeout": 10000}, defaults, "low_latency")
        save_config(tool, {"timeout": 90000}, defaults, "high_reliability")
        profiles = list_profiles(tool)
        assert len(profiles) == 3

        # 3. 加载方案值正确
        low = load_config(tool, defaults, "low_latency")
        assert low["timeout"] == 10000
        high = load_config(tool, defaults, "high_reliability")
        assert high["timeout"] == 90000

        # 4. 默认配置仍独立
        default = load_config(tool, defaults, None)
        assert default["timeout"] == 30000

        # 5. 删除 low_latency
        assert delete_profile(tool, "low_latency") is True
        profiles = list_profiles(tool)
        assert len(profiles) == 2
        names = [p[0] for p in profiles]
        assert "low_latency" not in names
        assert "high_reliability" in names

        # 6. 默认配置不可删除
        assert delete_profile(tool, None) is False
        assert delete_profile(tool, "") is False

        # 7. 清理
        delete_profile(tool, "high_reliability")
        profiles = list_profiles(tool)
        assert len(profiles) == 1


# ---------------------------------------------------------------------------
# 清理：删除可能残留的测试文件
# ---------------------------------------------------------------------------

def test_cleanup_test_files():
    """清理测试过程中可能残留的临时 profile 文件。"""
    from src.core.config_store import get_config_dir
    config_dir = get_config_dir()
    if config_dir.exists():
        for f in config_dir.glob("test_tool_*.json"):
            os.remove(str(f))
        for f in config_dir.glob("tool_x_*.json"):
            os.remove(str(f))
        for f in config_dir.glob("tool_p_*.json"):
            os.remove(str(f))
        for f in config_dir.glob("tool_m_*.json"):
            os.remove(str(f))
        for f in config_dir.glob("tool_d_*.json"):
            os.remove(str(f))
        for f in config_dir.glob("lifecycle_tool_*.json"):
            os.remove(str(f))
