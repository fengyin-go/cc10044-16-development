"""物理分区登记册领域服务：只读维护实际物理分区及其独占/共同归属与获准用途。

数据链路位置：存储治理层，为清理作业的多方签认范围提供实际分区事实来源。
登记册本身的写入由治理流程（分区注册）负责，本服务只暴露通用查询骨架。
"""
from __future__ import annotations

from app.services.base import BaseModuleService


class PhysicalPartitionService(BaseModuleService):
    MODULE = "physical_partition"
    ENTITY = "物理分区"
    OPERATION_TERM = "核对"
    LIST_FIELDS = [
        "物理分区路径", "归属类型", "所有方", "获准用途",
        "数据集", "分区日期", "到期状态", "行数", "大小MB",
    ]
    REQUIRED_FIELDS = ["物理分区路径", "归属类型", "所有方", "获准用途"]
    STATUS_ORDER = ["已登记", "已冻结"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []
