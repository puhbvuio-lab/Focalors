# -*- coding: utf-8 -*-
"""数据任务执行结果契约模型。

定义了标准化任务输出结果、数据统计、错误信息及制品清单的数据模型。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class RunStatus(str, Enum):
    """数据任务执行终态。"""
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class RunError:
    """结构化错误信息。"""
    code: str
    message: str
    item: str | None = None
    extra: dict | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class RunStats:
    """任务执行统计信息。

    注意：基础计数单位在所有工具中必须保持一致，以“输入单元”（Input Unit）统计：
    - 在博主列表采集等工具中，一个输入单元代表一个 URL。
    - 在 Excel 合并等文件处理工具中，一个输入单元代表一个输入文件。
    - 业务指标统计（如合并后总行数、提取词条数）必须放入 extra 属性中，避免与基础输入计数混淆。
    """
    input_count: int = 0      # 用户提交的原始输入单元总数
    success_count: int = 0    # 成功处理的原始输入单元数
    failed_count: int = 0     # 失败的原始输入单元数
    skipped_count: int = 0    # 主动跳过、过滤或取消的原始输入单元数
    extra: dict = field(default_factory=dict) # 包含非输入单元单位的业务自定义指标

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ArtifactRef:
    """输出产物关联引用。"""
    path: str
    label: str
    extra: dict | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class RunOutcome:
    """数据处理/采集任务的标准化完整输出结构。"""
    run_id: str
    tool_id: str
    status: RunStatus
    stats: RunStats = field(default_factory=RunStats)
    errors: list[RunError] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    output_path: str | None = None  # 兼容旧有 UI 期待的单个主输出文件绝对路径

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "tool_id": self.tool_id,
            "status": self.status.value,
            "stats": self.stats.to_dict(),
            "errors": [e.to_dict() for e in self.errors],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "output_path": self.output_path,
        }

    def save_to_json(self, path: str | Path):
        """原子化保存当前任务执行报告为 JSON 文件。"""
        import tempfile
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 在目标目录创建临时文件，以保证原子重命名发生在同一磁盘分区上
        temp_fd, temp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(temp_path, str(p))
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
            raise
