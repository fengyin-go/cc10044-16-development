"""数据保留策略领域服务：管理数据保留规则、合规重叠检查和按策略触发清理。

数据链路位置：数据生命周期层，控制数据的保留期和自动清理。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataRetentionService(BaseModuleService):
    MODULE = "data_retention"
    ENTITY = "保留规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则名称", "目标数据集", "保留天数", "清理方式", "最近执行时间"]
    REQUIRED_FIELDS = ["规则名称", "目标数据集", "保留天数"]
    STATUS_ORDER = ["待生效", "已生效", "已过期"]
    ACTION_RULES = {"启用规则": "已生效", "停用规则": "已过期"}
    NEGATIVE_ACTIONS = ["停用规则"]

    CLEANUP_METHODS = frozenset({"物理删除", "移至归档存储", "降冷", "仅标记过期"})

    def evaluate_retention_status(self, entry_id: int) -> dict[str, Any]:
        """评估数据保留策略的执行状态。

        检查数据集当前年龄是否超出保留天数，
        输出需要清理的数据清单和期限。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "保留规则不存在"}

        dataset = entry.get("目标数据集", "")
        retention_days = int(entry.get("保留天数", "30"))
        cleanup_method = entry.get("清理方式", "移至归档存储")

        today = datetime(2026, 11, 21)
        retention_cutoff = today - timedelta(days=retention_days)
        expired_partitions = []
        for i in range(5):
            d = retention_cutoff - timedelta(days=i)
            expired_partitions.append({
                "dataset": dataset,
                "partition": f"dt={d.strftime('%Y-%m-%d')}",
                "age_days": (today - d).days,
                "action": cleanup_method,
            })

        return {
            "policy_id": entry_id,
            "policy_name": entry.get("规则名称"),
            "dataset": dataset,
            "retention_days": retention_days,
            "cutoff_date": retention_cutoff.strftime("%Y-%m-%d"),
            "cleanup_method": cleanup_method,
            "expired_partitions": len(expired_partitions),
            "partitions": expired_partitions,
            "estimated_cleanup_size_gb": len(expired_partitions) * 0.35,
            "needs_action": len(expired_partitions) > 0,
        }

    def check_compliance_overlap(self, entry_id: int) -> dict[str, Any]:
        """检查合规保留重叠。

        当同一数据集上存在多条保留策略时检测冲突，
        合规要求保留天数大于业务保留天数时优先合规要求。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "保留规则不存在"}

        dataset = entry.get("目标数据集", "")
        all_rules = store.rows(self.MODULE)
        overlapping = [
            r for r in all_rules
            if r.get("目标数据集") == dataset and r["id"] != entry_id
            and r.get("status") == "已生效"
        ]
        conflicts = []
        for r in overlapping:
            r_days = int(r.get("保留天数", "0"))
            entry_days = int(entry.get("保留天数", "0"))
            if r_days > entry_days:
                conflicts.append({
                    "other_policy_id": r["id"],
                    "other_policy_name": r.get("规则名称"),
                    "other_retention_days": r_days,
                    "conflict": f"合规要求保留 {r_days} 天，业务设定为 {entry_days} 天",
                    "resolution": "以较长保留期为准",
                })

        return {
            "policy_id": entry_id,
            "dataset": dataset,
            "overlapping_policies": len(overlapping),
            "conflicts": conflicts,
            "effective_retention_days": max(
                [int(entry.get("保留天数", "0"))] +
                [int(r.get("保留天数", "0")) for r in overlapping]
            ) if conflicts else int(entry.get("保留天数", "0")),
            "recommendation": "需要合并策略" if conflicts else "无冲突",
        }

    def trigger_cleanup_by_policy(self, entry_id: int) -> dict[str, Any]:
        """根据保留策略触发清理作业。

        联动 cleanup_job 模块，自动生成或更新对应数据集的清理作业配置。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "保留规则不存在"}
        if entry.get("status") != "已生效":
            return {"ok": False, "message": "只有已生效的规则才能触发清理"}

        eval_result = self.evaluate_retention_status(entry_id)
        if not eval_result["needs_action"]:
            return {"ok": True, "message": "无需清理，所有数据在保留期内"}

        cleanup_rows = store.rows("cleanup_job")
        new_job_id = max((int(r.get("id", 0)) for r in cleanup_rows), default=0) + 1
        new_job = {
            "id": new_job_id,
            "status": "已启用",
            "作业名称": f"auto_cleanup_{entry.get('规则名称')}",
            "目标路径": f"/data/{entry.get('目标数据集')}",
            "清理条件": f"分区日期早于 {eval_result['cutoff_date']}",
            "预览行数": str(sum(p["age_days"] * 500 for p in eval_result["partitions"])),
            "最近清理时间": "",
            "pending": False,
            "abnormal": False,
            "来源保留策略ID": entry_id,
            "执行记录": [],
        }
        cleanup_rows.append(new_job)

        # 保留策略创建的清理作业立即按物理分区有效签认范围生成计划，
        # 值班员确认时只删除有效签认范围内的物理分区。
        from app.services.cleanup_job import CleanupJobService
        plan_result = CleanupJobService().build_cleanup_plan(new_job_id)

        entry["最近执行时间"] = "2026-11-21T08:00:00Z"
        return {
            "ok": True,
            "policy_id": entry_id,
            "created_cleanup_job_id": new_job_id,
            "cleanup_method": eval_result["cleanup_method"],
            "expired_partitions": eval_result["expired_partitions"],
            "cleanup_plan": plan_result["plan"],
            "message": f"已创建清理作业 {new_job_id}",
        }

    def retention_policy_report(self) -> dict[str, Any]:
        """生成保留策略全平台汇总报告。

        统计所有数据集的保留状态、即将过期的数据集列表。
        """
        all_rules = store.rows(self.MODULE)
        active = [r for r in all_rules if r.get("status") == "已生效"]
        expiring_soon: list[dict[str, Any]] = []
        for r in active:
            retention = int(r.get("保留天数", "30"))
            if retention <= 7:
                expiring_soon.append({
                    "policy_id": r["id"],
                    "policy_name": r.get("规则名称"),
                    "dataset": r.get("目标数据集"),
                    "retention_days": retention,
                    "cleanup_method": r.get("清理方式"),
                })

        return {
            "total_policies": len(all_rules),
            "active_policies": len(active),
            "total_datasets_managed": len({r.get("目标数据集") for r in active}),
            "expiring_soon_count": len(expiring_soon),
            "expiring_soon": expiring_soon,
            "avg_retention_days": round(
                sum(int(r.get("保留天数", "30")) for r in active) / max(len(active), 1), 1
            ),
            "next_scheduled_cleanup": "2026-11-22T02:00:00Z",
        }