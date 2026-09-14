"""
工作台基础核心类定义，包含工具包声明类 ToolSpec 以及动态加载工具的反射方法。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field

# 导航分类默认选中字段，用于展示全局所有爬虫工具
ALL_CATEGORY = "全部"


def load_object(dotted_path: str):
    """
    根据给定的点分字符串路径动态载入对应的模块和对象。
    例如传入 "src.core.xlsx.XlsxRowWriter" 将反射加载导入该类。

    Args:
        dotted_path: 点分符号对象导入路径

    Returns:
        Any: 导入完成的对象或方法类
    """
    module_path, object_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, object_name)


@dataclass(frozen=True)
class ToolSpec:
    """
    爬虫工具描述符类，承载从各平台 *.manifest.json 清单文件中读取出的参数定义。
    """
    tool_id: str             # 唯一的工具标识 ID
    name: str                # 界面显示的工具标题
    category: str            # 工具类型归属（如：YouTube / 抖音）
    summary: str             # 简短的工具功能介绍
    entrypoint: str          # 对应 Python 执行函数入口的点分路径
    implementation_path: str = ""  # 选填，实现文件路径
    tags: tuple[str, ...] = field(default_factory=tuple)  # 自定义过滤标签列表

    def matches(self, query: str, category: str) -> bool:
        """
        判断当前工具是否与用户输入的检索关键字和侧边分类栏相匹配。

        Args:
            query: 检索关键词
            category: 选中的标签分类

        Returns:
            bool: 匹配结果
        """
        # 分类校验匹配
        if category != ALL_CATEGORY and self.category != category:
            return False
        if not query:
            return True
        # 搜索过滤串：合并名称、分类、概述和标签，做大小写不敏感匹配
        haystack = " ".join([self.name, self.category, self.summary, " ".join(self.tags)]).lower()
        return query.lower() in haystack


