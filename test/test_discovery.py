import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.studio.discovery import discover_tools, _load_manifest
from src.studio.base import ToolSpec

def test_discover_all_tools():
    """测试发现所有工具"""
    tools, _ = discover_tools()
    print(f"发现 {len(tools)} 个工具")

    assert len(tools) == 32, f"期望 32 个工具，实际 {len(tools)}"

    # 验证每个工具都有必要字段
    for tool in tools:
        assert isinstance(tool, ToolSpec), f"{tool} 不是 ToolSpec 实例"
        assert tool.tool_id, "tool_id 不能为空"
        assert tool.name, "name 不能为空"
        assert tool.category, "category 不能为空"
        assert tool.entrypoint, "entrypoint 不能为空"

    print("[PASS] 所有工具结构验证通过")

def test_discover_by_category():
    """测试按类别发现工具"""
    tools, _ = discover_tools()

    categories = {}
    for tool in tools:
        categories.setdefault(tool.category, []).append(tool)

    print("按类别统计:")
    for cat, cat_tools in sorted(categories.items()):
        print(f"  {cat}: {len(cat_tools)} 个工具")

    # 验证预期的类别
    expected_categories = {
        "YouTube",
        "X/Twitter",
        "TikTok",
        "Instagram",
        "Facebook",
        "Steam",
        "数据处理",
        "工具",
    }
    actual_categories = set(categories.keys())
    assert actual_categories == expected_categories, f"类别不匹配: {actual_categories}"

    # 验证每个类别的工具数量
    assert len(categories["YouTube"]) == 5, f"YouTube 工具数量错误: {len(categories['YouTube'])}"
    assert len(categories["X/Twitter"]) == 8, f"X/Twitter 工具数量错误: {len(categories['X/Twitter'])}"
    assert len(categories["TikTok"]) == 9, f"TikTok 工具数量错误: {len(categories['TikTok'])}"
    assert len(categories["Instagram"]) == 1, f"Instagram 工具数量错误: {len(categories['Instagram'])}"
    assert len(categories["Facebook"]) == 2, f"Facebook 工具数量错误: {len(categories['Facebook'])}"
    assert len(categories["数据处理"]) == 4, f"数据处理工具数量错误: {len(categories['数据处理'])}"
    assert len(categories.get("工具", [])) == 1, f"工具 数量错误: {len(categories.get('工具', []))}"

    print("[PASS] 类别统计验证通过")

def test_discover_specific_tool():
    """测试发现特定工具"""
    tools, _ = discover_tools()

    # 查找 YouTube 关键词搜索工具
    youtube_keyword = next((t for t in tools if t.tool_id == "youtube_keyword_mining"), None)
    assert youtube_keyword is not None, "未找到 youtube_keyword_mining"
    assert youtube_keyword.name == "YouTube 关键词搜索"
    assert youtube_keyword.category == "YouTube"
    assert "search" in youtube_keyword.tags
    assert youtube_keyword.entrypoint == "src.platforms.youtube.windows.YouTubeKeywordWindow"

    print("[PASS] 特定工具验证通过")

def test_load_manifest_valid():
    """测试加载有效的 manifest 文件"""
    manifest_content = {
        "tool_id": "test_tool",
        "name": "测试工具",
        "category": "测试",
        "summary": "这是一个测试工具",
        "entrypoint": "src.test.TestWindow",
        "tags": ["test", "demo"]
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.manifest.json', delete=False, encoding='utf-8') as f:
        json.dump(manifest_content, f, ensure_ascii=False)
        temp_path = Path(f.name)

    try:
        tool, _ = _load_manifest(temp_path)
        assert tool is not None, "加载 manifest 失败"
        assert tool.tool_id == "test_tool"
        assert tool.name == "测试工具"
        assert tool.category == "测试"
        assert tool.tags == ("test", "demo")
        print("[PASS] 有效 manifest 加载验证通过")
    finally:
        temp_path.unlink()

def test_load_manifest_missing_fields():
    """测试加载缺少必要字段的 manifest"""
    manifest_content = {
        "tool_id": "incomplete_tool",
        "name": "不完整的工具"
        # 缺少 category, summary, entrypoint
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.manifest.json', delete=False, encoding='utf-8') as f:
        json.dump(manifest_content, f, ensure_ascii=False)
        temp_path = Path(f.name)

    try:
        tool, err = _load_manifest(temp_path)
        assert tool is None, "应该返回 None 但返回了工具"
        print("[PASS] 缺少字段的 manifest 验证通过")
    finally:
        temp_path.unlink()

def test_load_manifest_invalid_json():
    """测试加载无效 JSON 的 manifest"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.manifest.json', delete=False, encoding='utf-8') as f:
        f.write("这不是有效的 JSON")
        temp_path = Path(f.name)

    try:
        tool, err = _load_manifest(temp_path)
        assert tool is None, "应该返回 None 但返回了工具"
        print("[PASS] 无效 JSON 的 manifest 验证通过")
    finally:
        temp_path.unlink()

def test_discover_custom_dir():
    """测试从自定义目录发现工具"""
    with tempfile.TemporaryDirectory() as temp_dir:
        # 创建一个测试 manifest
        manifest_content = {
            "tool_id": "custom_tool",
            "name": "自定义工具",
            "category": "自定义",
            "summary": "从自定义目录加载的工具",
            "entrypoint": "src.custom.CustomWindow",
            "tags": ["custom"]
        }

        manifest_path = Path(temp_dir) / "custom.manifest.json"
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest_content, f, ensure_ascii=False)

        # 从自定义目录发现工具
        tools, _ = discover_tools(scan_dirs=[temp_dir])
        assert len(tools) == 1, f"期望 1 个工具，实际 {len(tools)}"
        assert tools[0].tool_id == "custom_tool"

        print("[PASS] 自定义目录发现验证通过")

def test_reload_tools():
    """测试重新加载工具（模拟热重载）"""
    # 第一次加载
    tools1, _ = discover_tools()
    count1 = len(tools1)

    # 第二次加载（应该返回相同结果）
    tools2, _ = discover_tools()
    count2 = len(tools2)

    assert count1 == count2, f"两次加载结果不同: {count1} vs {count2}"

    # 验证工具列表一致性
    ids1 = {t.tool_id for t in tools1}
    ids2 = {t.tool_id for t in tools2}
    assert ids1 == ids2, "两次加载的工具 ID 不一致"

    print("[PASS] 重新加载验证通过")

def test_find_tool_covers_all_discovered():
    """测试 tool_runner.find_tool 能找到所有 discover_tools 发现的工具"""
    try:
        from src.studio.tool_runner import find_tool
    except ModuleNotFoundError as e:
        if "PyQt5" in str(e):
            print("[SKIP] PyQt5 未安装，跳过 find_tool 测试")
            return
        raise

    tools, _ = discover_tools()
    project_root = Path(__file__).resolve().parents[1]

    for tool in tools:
        # find_tool 必须能找到每个已发现的工具
        found = find_tool(tool.tool_id)
        assert found is not None, f"find_tool 找不到 '{tool.tool_id}'"
        assert found.tool_id == tool.tool_id, f"find_tool('{tool.tool_id}') 返回了错误的工具 '{found.tool_id}'"

        # implementation_path 对应的文件必须存在
        if tool.implementation_path:
            script_path = project_root / "src" / tool.implementation_path
            assert script_path.exists(), f"工具 '{tool.tool_id}' 的实现文件不存在: {script_path}"

    print(f"[PASS] find_tool 覆盖全部 {len(tools)} 个工具，实现文件均存在")

def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("开始运行 discovery 模块测试")
    print("=" * 60)

    tests = [
        test_discover_all_tools,
        test_discover_by_category,
        test_discover_specific_tool,
        test_load_manifest_valid,
        test_load_manifest_missing_fields,
        test_load_manifest_invalid_json,
        test_discover_custom_dir,
        test_reload_tools,
        test_find_tool_covers_all_discovered,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            print(f"\n运行: {test.__name__}")
            test()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__} 失败: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"测试完成: {passed} 通过, {failed} 失败")
    print("=" * 60)

    return failed == 0

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
