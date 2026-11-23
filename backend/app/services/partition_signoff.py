"""分区签认领域服务：只读维护所有方对物理分区具体用途的签认记录。

签认的授予、撤回和冲突标记统一由清理作业领域服务（CleanupJobService）
在删除治理流程内完成，本服务只暴露通用查询骨架，供值班员核对签认台账。
"""
from __future__ import annotations

from app.services.base import BaseModuleService


class PartitionSignoffService(BaseModuleService):
    MODULE = "partition_signoff"
    ENTITY = "分区签认"
    OPERATION_TERM = "核对"
    LIST_FIELDS = [
        "物理分区路径", "所有方", "签认用途", "授权路径",
        "签认时间", "撤回时间", "冲突标记",
    ]
    REQUIRED_FIELDS = ["物理分区ID", "所有方", "签认用途", "授权路径"]
    STATUS_ORDER = ["有效", "已撤回"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []
