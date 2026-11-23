"""清理作业领域服务：管理过期分区、临时文件的清理作业调度和执行。

数据链路位置：存储治理层，清理过期数据和孤儿文件释放存储空间。

签认治理规则（物理分区多方签认）：
1. 清理对象以物理分区登记册（physical_partition）为准，区分独占归属和共同归属，
   同一个物理分区在一次清理范围内只计算一次。
2. 有效签认范围按“物理分区 + 用途”逐份判定：签认必须同时满足
   用途获准、授权路径精确覆盖该物理分区、所有方与分区相关、签认未撤回且无冲突。
   一个路径的授权不能把同路径下另一份数据（不同数据集/订单组）一并带走。
3. 独占分区只需其唯一所有方签认；共同分区需要每个相关所有方都对同一用途签认，
   缺任何一份签认，整个物理分区都不得删除。
4. 保留策略创建的清理作业在确认时按物理分区的有效签认范围执行，
   执行记录保留已执行分区和因具体签认缺失而未执行的分区，可逐项核对。
5. 再次生成计划时跳过已经完成（已删除）的物理分区，不重复安排。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 清理作业确认删除时使用的固定用途；签认的用途必须与之一致才有效。
DELETE_PURPOSE = "物理删除"
# 判定过期时使用的当前业务日期（与保留策略服务保持同一天）。
TODAY = datetime(2026, 11, 21)
EXECUTION_TIME = "2026-11-21T08:00:00Z"


class CleanupJobService(BaseModuleService):
    MODULE = "cleanup_job"
    ENTITY = "清理作业"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["作业名称", "目标路径", "清理条件", "预览行数", "最近清理时间"]
    REQUIRED_FIELDS = ["作业名称", "目标路径", "清理条件"]
    STATUS_ORDER = ["草稿", "已启用", "已暂停"]
    ACTION_RULES = {"启用清理": "已启用", "暂停清理": "已暂停"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 物理分区与签认读取
    # ------------------------------------------------------------------
    def _physical_partitions(self) -> list[dict[str, Any]]:
        """读取实际物理分区登记册。"""
        return store.rows("physical_partition")

    def _signoffs(self) -> list[dict[str, Any]]:
        """读取全部签认记录。"""
        return store.rows("partition_signoff")

    @staticmethod
    def _owners(partition: dict[str, Any]) -> list[str]:
        return [owner.strip() for owner in str(partition.get("所有方", "")).split(",") if owner.strip()]

    @staticmethod
    def _allowed_purposes(partition: dict[str, Any]) -> list[str]:
        return [p.strip() for p in str(partition.get("获准用途", "")).split(",") if p.strip()]

    @staticmethod
    def _path_covers(authorized_path: str, partition_path: str) -> bool:
        """授权路径是否精确覆盖物理分区。

        只允许精确相等或授权路径为该分区的父目录，避免对同路径下另一份数据的越权覆盖
        （例如授权到 /data/ods_orders 时，只会按登记册中逐份物理分区核对，
        不会把未登记/未签认的另一份数据带走）。
        """
        authorized_path = (authorized_path or "").rstrip("/")
        partition_path = (partition_path or "").rstrip("/")
        if not authorized_path or not partition_path:
            return False
        if authorized_path == partition_path:
            return True
        return partition_path.startswith(authorized_path + "/")

    def _matching_partitions(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """按作业目标路径与清理条件从登记册筛选物理分区，同一物理分区只算一次。"""
        target_path = (entry.get("目标路径") or "").rstrip("/")
        matched: list[dict[str, Any]] = []
        seen: set[int] = set()
        for partition in self._physical_partitions():
            pid = int(partition["id"])
            if pid in seen:
                continue
            partition_path = (partition.get("物理分区路径") or "").rstrip("/")
            if not self._path_covers(target_path, partition_path):
                continue
            # 清理条件体现到期要求；未到期分区不进入清理计划。
            if partition.get("到期状态") != "已到期":
                continue
            seen.add(pid)
            matched.append(partition)
        return matched

    def _completed_partition_ids(self, entry: dict[str, Any]) -> set[int]:
        """该作业历次执行记录中已经实际删除的物理分区，计划生成时不得重复安排。"""
        completed: set[int] = set()
        for record in entry.get("执行记录", []):
            for item in record.get("executed_partitions", []):
                completed.add(int(item["partition_id"]))
        return completed

    # ------------------------------------------------------------------
    # 有效签认范围：逐物理分区、逐所有方核对
    # ------------------------------------------------------------------
    def effective_signoff_scope(self, entry_id: int) -> dict[str, Any]:
        """确定清理作业覆盖的物理分区的有效签认范围。

        返回每个物理分区的归属类型、相关所有方、逐所有方签认核对结果和缺失签认清单。
        共同分区必须每个相关所有方都对“物理删除”用途留下有效签认；
        独占分区只需其唯一所有方签认。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}

        partitions = self._matching_partitions(entry)
        completed_ids = self._completed_partition_ids(entry)
        scope_partitions: list[dict[str, Any]] = []

        for partition in partitions:
            pid = int(partition["id"])
            owners = self._owners(partition)
            allowed = self._allowed_purposes(partition)
            purpose_allowed = DELETE_PURPOSE in allowed
            owner_checks: list[dict[str, Any]] = []
            missing_signoffs: list[dict[str, str]] = []

            for owner in owners:
                # 只看该所有方对该物理分区、该用途的签认；路径需精确覆盖。
                candidate = [
                    s for s in self._signoffs()
                    if int(s.get("物理分区ID", -1)) == pid
                    and s.get("所有方") == owner
                    and s.get("签认用途") == DELETE_PURPOSE
                    and self._path_covers(str(s.get("授权路径", "")), str(partition.get("物理分区路径", "")))
                ]
                valid = [
                    s for s in candidate
                    if s.get("status") == "有效" and not s.get("冲突标记") and not s.get("撤回时间")
                ]
                check = {
                    "owner": owner,
                    "has_valid_signoff": bool(valid),
                    "signoff_id": int(valid[0]["id"]) if valid else None,
                    "signoff_status": valid[0].get("status") if valid else (
                        candidate[0].get("status") if candidate else "未签认"),
                    "conflict": bool(candidate and candidate[0].get("冲突标记")),
                    "withdrawn": bool(candidate and bool(candidate[0].get("撤回时间"))),
                }
                owner_checks.append(check)
                if not valid:
                    reason = "未签认"
                    if candidate:
                        first = candidate[0]
                        if first.get("撤回时间"):
                            reason = f"{owner} 已撤回签认（签认ID {first['id']}）"
                        elif first.get("冲突标记"):
                            reason = f"{owner} 的授权存在冲突（签认ID {first['id']}）"
                        elif first.get("status") != "有效":
                            reason = f"{owner} 的签认状态为{first.get('status')}（签认ID {first['id']}）"
                    missing_signoffs.append({
                        "partition_id": pid,
                        "partition_path": partition.get("物理分区路径", ""),
                        "owner": owner,
                        "purpose": DELETE_PURPOSE,
                        "reason": reason,
                    })

            if not purpose_allowed:
                missing_signoffs.append({
                    "partition_id": pid,
                    "partition_path": partition.get("物理分区路径", ""),
                    "owner": "、".join(owners),
                    "purpose": DELETE_PURPOSE,
                    "reason": f"物理分区获准用途为「{partition.get('获准用途')}」，不含{DELETE_PURPOSE}",
                })

            all_owners_signed = all(c["has_valid_signoff"] for c in owner_checks)
            in_effective_scope = purpose_allowed and all_owners_signed
            scope_partitions.append({
                "partition_id": pid,
                "partition_path": partition.get("物理分区路径"),
                "dataset": partition.get("数据集"),
                "ownership": partition.get("归属类型"),
                "owners": owners,
                "allowed_purposes": allowed,
                "purpose": DELETE_PURPOSE,
                "purpose_allowed": purpose_allowed,
                "owner_checks": owner_checks,
                "in_effective_scope": in_effective_scope,
                "missing_signoffs": missing_signoffs,
                "already_completed": pid in completed_ids,
            })

        ready = [p for p in scope_partitions if p["in_effective_scope"] and not p["already_completed"]]
        blocked = [p for p in scope_partitions if not p["in_effective_scope"] and not p["already_completed"]]
        return {
            "ok": True,
            "job_id": entry_id,
            "target_path": entry.get("目标路径"),
            "purpose": DELETE_PURPOSE,
            "total_physical_partitions": len(scope_partitions),
            "effective_scope_count": len(ready),
            "blocked_count": len(blocked),
            "completed_count": sum(1 for p in scope_partitions if p["already_completed"]),
            "partitions": scope_partitions,
            "effective_partitions": ready,
            "blocked_partitions": blocked,
        }

    # ------------------------------------------------------------------
    # 清理计划：保留策略生成 + 去重
    # ------------------------------------------------------------------
    def build_cleanup_plan(self, entry_id: int) -> dict[str, Any]:
        """生成清理计划。

        以物理分区登记册为实际分区来源，已完成（已删除）的物理分区不再安排，
        保证再次生成计划不会重复安排已经完成的操作。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}

        scope = self.effective_signoff_scope(entry_id)
        plan_items: list[dict[str, Any]] = []
        for partition in scope["partitions"]:
            if partition["already_completed"]:
                continue
            plan_items.append({
                "partition_id": partition["partition_id"],
                "partition_path": partition["partition_path"],
                "ownership": partition["ownership"],
                "owners": partition["owners"],
                "dataset": partition["dataset"],
                "purpose": DELETE_PURPOSE,
                "in_effective_scope": partition["in_effective_scope"],
                "missing_signoffs": partition["missing_signoffs"],
            })

        plan = {
            "generated_at": EXECUTION_TIME,
            "target_path": entry.get("目标路径"),
            "condition": entry.get("清理条件"),
            "planned_partitions": len(plan_items),
            "executable_partitions": sum(1 for p in plan_items if p["in_effective_scope"]),
            "skipped_completed_partitions": scope["completed_count"],
            "items": plan_items,
        }
        entry["清理计划"] = plan
        return {"ok": True, "job_id": entry_id, "plan": plan}

    def preview_cleanup_scope(self, entry_id: int) -> dict[str, Any]:
        """预览清理范围：基于物理分区登记册和有效签认范围展示待清理分区。

        预览同时输出有效签认范围内可删除的分区，以及因签认缺失被拦截的分区，
        值班员可在确认前逐份核对所有方签认状态。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}

        plan_result = self.build_cleanup_plan(entry_id)
        plan = plan_result["plan"]
        scope = self.effective_signoff_scope(entry_id)

        candidates = []
        total_rows = 0
        total_mb = 0
        partition_by_id = {int(p["id"]): p for p in self._physical_partitions()}
        for item in plan["items"]:
            partition = partition_by_id[item["partition_id"]]
            row_count = int(partition.get("行数", 0))
            size_mb = int(partition.get("大小MB", 0))
            total_rows += row_count
            total_mb += size_mb
            candidates.append({
                "path": item["partition_path"],
                "partition_id": item["partition_id"],
                "partition": partition.get("分区日期"),
                "ownership": item["ownership"],
                "owners": item["owners"],
                "dataset": item["dataset"],
                "row_count": row_count,
                "size_mb": size_mb,
                "matches_condition": True,
                "in_effective_scope": item["in_effective_scope"],
                "missing_signoffs": item["missing_signoffs"],
            })

        entry["预览行数"] = str(total_rows)
        return {
            "ok": True,
            "job_id": entry_id,
            "job_name": entry.get("作业名称"),
            "target_path": entry.get("目标路径"),
            "condition": entry.get("清理条件"),
            "matched_items": len(candidates),
            "total_rows_to_delete": total_rows,
            "total_size_mb": total_mb,
            "total_size_gb": round(total_mb / 1024, 2),
            "candidates": candidates,
            "effective_scope_count": scope["effective_scope_count"],
            "blocked_count": scope["blocked_count"],
            "effective_partitions": scope["effective_partitions"],
            "blocked_partitions": scope["blocked_partitions"],
        }

    # ------------------------------------------------------------------
    # 确认执行：按有效签认范围删除并留执行记录
    # ------------------------------------------------------------------
    def execute_cleanup(self, entry_id: int, confirm: bool = False) -> dict[str, Any]:
        """执行清理作业。

        confirm=False 仅预览；confirm=True 时按物理分区有效签认范围实际删除：
        - 有效签认范围内的物理分区才删除，共同分区缺任何一份签认则整个分区跳过；
        - 执行记录逐项保留已确认（已删除）分区和因哪份签认缺失而未执行的分区；
        - 已完成分区不重复执行。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}
        if not confirm:
            preview = self.preview_cleanup_scope(entry_id)
            preview["awaiting_confirmation"] = True
            return preview
        if entry.get("status") != "已启用":
            return {"ok": False, "message": "只有已启用的清理作业才能执行"}

        scope = self.effective_signoff_scope(entry_id)
        completed_ids = self._completed_partition_ids(entry)
        partition_by_id = {int(p["id"]): p for p in self._physical_partitions()}

        executed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        deleted_rows = 0
        freed_mb = 0

        for partition in scope["partitions"]:
            pid = partition["partition_id"]
            if pid in completed_ids:
                continue
            if partition["in_effective_scope"]:
                physical = partition_by_id[pid]
                deleted_rows += int(physical.get("行数", 0))
                freed_mb += int(physical.get("大小MB", 0))
                executed.append({
                    "partition_id": pid,
                    "partition_path": partition["partition_path"],
                    "ownership": partition["ownership"],
                    "owners": partition["owners"],
                    "purpose": DELETE_PURPOSE,
                    "validated_signoff_ids": [
                        c["signoff_id"] for c in partition["owner_checks"] if c["signoff_id"]
                    ],
                })
            else:
                blocked.append({
                    "partition_id": pid,
                    "partition_path": partition["partition_path"],
                    "ownership": partition["ownership"],
                    "owners": partition["owners"],
                    "purpose": DELETE_PURPOSE,
                    "missing_signoffs": partition["missing_signoffs"],
                })

        record = {
            "execution_id": len(entry.get("执行记录", [])) + 1,
            "executed_at": EXECUTION_TIME,
            "executed_partitions": executed,
            "blocked_partitions": blocked,
            "executed_count": len(executed),
            "blocked_count": len(blocked),
            "deleted_rows": deleted_rows,
            "freed_space_gb": round(freed_mb / 1024, 2),
        }
        entry.setdefault("执行记录", []).append(record)
        entry["最近清理时间"] = EXECUTION_TIME

        # 重新生成计划：已删除分区不再出现，剩余被拦截分区等待补齐签认。
        self.build_cleanup_plan(entry_id)

        return {
            "ok": True,
            "job_id": entry_id,
            "deleted_partitions": len(executed),
            "blocked_partitions": len(blocked),
            "deleted_rows": deleted_rows,
            "freed_space_gb": record["freed_space_gb"],
            "cleanup_time": EXECUTION_TIME,
            "executed_partitions": executed,
            "blocked_partition_details": blocked,
            "execution_record": record,
            "message": (
                f"已清理 {len(executed)} 个物理分区，释放 {record['freed_space_gb']} GB；"
                f"{len(blocked)} 个分区因签认缺失未执行"
            ),
        }

    def get_execution_records(self, entry_id: int) -> dict[str, Any]:
        """复查作业执行记录：核对已确认分区与因签认缺失未执行的分区。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}
        records = entry.get("执行记录", [])
        return {
            "ok": True,
            "job_id": entry_id,
            "job_name": entry.get("作业名称"),
            "execution_count": len(records),
            "records": records,
            "current_plan": entry.get("清理计划"),
        }

    # ------------------------------------------------------------------
    # 签认权限：新增、撤回、冲突检测
    # ------------------------------------------------------------------
    def grant_signoff(self, entry_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """新增签认权限：所有方对作业范围内的物理分区就指定用途签认。

        签认写入 partition_signoff，并校验物理分区存在、所有方相关、用途获准、
        授权路径精确覆盖该物理分区（不允许一份授权带走同路径下另一份数据）。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}

        partition_id = payload.get("partition_id")
        owner = str(payload.get("owner") or "").strip()
        purpose = str(payload.get("purpose") or DELETE_PURPOSE).strip()
        authorized_path = str(payload.get("authorized_path") or "").strip()

        partition = store.find("physical_partition", int(partition_id)) if partition_id is not None else None
        if partition is None:
            return {"ok": False, "message": "物理分区不存在"}
        if owner not in self._owners(partition):
            return {"ok": False, "message": f"{owner} 不是物理分区 {partition_id} 的相关所有方"}
        if purpose not in self._allowed_purposes(partition):
            return {"ok": False, "message": f"用途 {purpose} 不在物理分区获准用途内"}
        if not self._path_covers(authorized_path, str(partition.get("物理分区路径", ""))):
            return {
                "ok": False,
                "message": "授权路径未精确覆盖该物理分区，不能据此带走同路径下的其他数据",
            }

        rows = self._signoffs()
        # 同一所有方对同一物理分区/用途只保留一条有效签认，重复签认刷新状态。
        existing = next(
            (s for s in rows
             if int(s.get("物理分区ID", -1)) == int(partition_id)
             and s.get("所有方") == owner and s.get("签认用途") == purpose),
            None,
        )
        if existing is not None:
            existing.update({
                "status": "有效", "授权路径": authorized_path,
                "签认时间": EXECUTION_TIME, "撤回时间": "", "冲突标记": False,
            })
            signoff_id = int(existing["id"])
        else:
            signoff_id = max((int(s.get("id", 0)) for s in rows), default=0) + 1
            rows.append({
                "id": signoff_id, "status": "有效", "pending": False, "abnormal": False,
                "物理分区ID": str(partition_id),
                "物理分区路径": partition.get("物理分区路径"),
                "所有方": owner, "签认用途": purpose,
                "授权路径": authorized_path, "签认时间": EXECUTION_TIME,
                "撤回时间": "", "冲突标记": False,
            })

        self.build_cleanup_plan(entry_id)
        return {
            "ok": True, "signoff_id": signoff_id, "job_id": entry_id,
            "partition_id": int(partition_id), "owner": owner, "purpose": purpose,
            "message": f"{owner} 已对物理分区 {partition_id} 的{purpose}用途签认",
        }

    def revoke_signoff(self, entry_id: int, signoff_id: int) -> dict[str, Any]:
        """撤回签认：签认立即失效，共同分区若因此缺少所有方签认将被拦截删除。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}
        signoff = store.find("partition_signoff", signoff_id)
        if signoff is None:
            return {"ok": False, "message": "签认记录不存在"}

        signoff["status"] = "已撤回"
        signoff["撤回时间"] = EXECUTION_TIME
        signoff["abnormal"] = True
        self.build_cleanup_plan(entry_id)
        return {
            "ok": True, "signoff_id": signoff_id, "job_id": entry_id,
            "partition_id": int(signoff.get("物理分区ID", 0)),
            "owner": signoff.get("所有方"), "purpose": signoff.get("签认用途"),
            "message": f"{signoff.get('所有方')} 已撤回物理分区 {signoff.get('物理分区ID')} 的签认",
        }

    def mark_signoff_conflict(self, entry_id: int, signoff_id: int, remark: str = "") -> dict[str, Any]:
        """标记预览后发现的授权冲突：冲突签认不再计入有效签认范围。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}
        signoff = store.find("partition_signoff", signoff_id)
        if signoff is None:
            return {"ok": False, "message": "签认记录不存在"}

        signoff["冲突标记"] = True
        signoff["abnormal"] = True
        signoff["冲突说明"] = remark or "预览后所有方授权范围存在冲突"
        self.build_cleanup_plan(entry_id)
        return {
            "ok": True, "signoff_id": signoff_id, "job_id": entry_id,
            "partition_id": int(signoff.get("物理分区ID", 0)),
            "owner": signoff.get("所有方"),
            "message": f"签认 {signoff_id} 已标记授权冲突，不再计入有效签认范围",
        }

    def list_signoffs(self, entry_id: int) -> dict[str, Any]:
        """列出作业范围内物理分区的全部签认及有效签认范围，供值班员办理删除前核对。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}
        scope = self.effective_signoff_scope(entry_id)
        partition_ids = {p["partition_id"] for p in scope["partitions"]}
        signoffs = [
            s for s in self._signoffs()
            if int(s.get("物理分区ID", -1)) in partition_ids
        ]
        return {
            "ok": True, "job_id": entry_id,
            "signoffs": signoffs,
            "effective_scope": {
                "effective_partitions": scope["effective_partitions"],
                "blocked_partitions": scope["blocked_partitions"],
            },
        }

    # ------------------------------------------------------------------
    # 保留策略联动与既有报表能力
    # ------------------------------------------------------------------
    def sync_with_retention_policy(self, entry_id: int) -> dict[str, Any]:
        """与数据保留策略联动生成清理建议，并据此重新生成物理分区清理计划。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "清理作业不存在"}

        retention_rows = store.rows("data_retention")
        target_path = entry.get("目标路径", "")
        dataset_name = target_path.rstrip("/").split("/")[-1] if "/" in target_path else target_path
        related_policies = [
            r for r in retention_rows
            if r.get("目标数据集") == dataset_name and r.get("status") == "已生效"
        ]

        suggestions = []
        for policy in related_policies:
            retention_days = int(policy.get("保留天数", "30"))
            expire_date = TODAY - timedelta(days=retention_days)
            suggestions.append({
                "policy_id": policy["id"],
                "policy_name": policy.get("规则名称"),
                "retention_days": retention_days,
                "expired_before": expire_date.strftime("%Y-%m-%d"),
                "action": "设置清理条件为过期时间早于该日期",
            })

        plan_result = self.build_cleanup_plan(entry_id)
        return {
            "ok": True,
            "job_id": entry_id,
            "matched_retention_policies": len(related_policies),
            "suggestions": suggestions,
            "auto_update_suggestion": suggestions[0] if suggestions else None,
            "cleanup_plan": plan_result["plan"],
        }

    def detect_duplicate_cleanup_jobs(self) -> dict[str, Any]:
        """检测重复的清理作业。

        当多条清理作业针对相同目标路径时产生告警，
        避免重复清理和资源浪费。
        """
        all_jobs = store.rows(self.MODULE)
        path_groups: dict[str, list[dict[str, Any]]] = {}
        for job in all_jobs:
            path = job.get("目标路径", "")
            if path:
                path_groups.setdefault(path, []).append(job)

        duplicates = {k: v for k, v in path_groups.items() if len(v) > 1}
        return {
            "duplicate_groups": len(duplicates),
            "affected_jobs": sum(len(v) for v in duplicates.values()),
            "details": {
                path: [{"job_id": j["id"], "job_name": j.get("作业名称"), "status": j.get("status")} for j in jobs]
                for path, jobs in duplicates.items()
            },
        }

    def cleanup_summary_report(self, days: int = 7) -> dict[str, Any]:
        """生成清理作业的汇总报告：基于执行记录汇总实际删除与签认拦截情况。"""
        all_jobs = store.rows(self.MODULE)
        active = [j for j in all_jobs if j.get("status") == "已启用"]
        executed_total = 0
        blocked_total = 0
        freed_gb_total = 0.0
        for job in all_jobs:
            for record in job.get("执行记录", []):
                executed_total += int(record.get("executed_count", 0))
                blocked_total += int(record.get("blocked_count", 0))
                freed_gb_total += float(record.get("freed_space_gb", 0))
        return {
            "period_days": days,
            "total_jobs": len(all_jobs),
            "active_jobs": len(active),
            "executed_partitions": executed_total,
            "blocked_by_missing_signoff": blocked_total,
            "total_freed_gb": round(freed_gb_total, 2),
            "next_scheduled_cleanup": "2026-11-22T02:00:00Z",
        }
