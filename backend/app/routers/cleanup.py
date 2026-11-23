"""清理作业领域路由：物理分区签认权限、清理计划、确认执行与执行记录。

路由只做参数接收和分派，所有业务判断都在 CleanupJobService 内完成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.cleanup_job import CleanupJobService

router = APIRouter(prefix="/api/cleanup_job", tags=["清理作业签认治理"])
service = CleanupJobService()


class SignoffPayload(BaseModel):
    partition_id: int
    owner: str
    purpose: str = "物理删除"
    authorized_path: str


class ConflictPayload(BaseModel):
    remark: str = ""


class PlanPayload(BaseModel):
    confirm: bool = False
    values: dict[str, Any] = Field(default_factory=dict)


@router.get("/{entry_id}/signoff-scope")
def signoff_scope(entry_id: int) -> dict[str, Any]:
    """有效签认范围：逐物理分区核对独占/共同所有方的签认状态。"""
    return service.effective_signoff_scope(entry_id)


@router.get("/{entry_id}/signoffs")
def list_signoffs(entry_id: int) -> dict[str, Any]:
    """列出作业范围内的签认记录与有效签认范围。"""
    return service.list_signoffs(entry_id)


@router.post("/{entry_id}/signoffs")
def grant_signoff(entry_id: int, payload: SignoffPayload) -> dict[str, Any]:
    """新增签认权限：所有方对具体物理分区和用途签认。"""
    return service.grant_signoff(entry_id, payload.model_dump())


@router.post("/{entry_id}/signoffs/{signoff_id}/revoke")
def revoke_signoff(entry_id: int, signoff_id: int) -> dict[str, Any]:
    """撤回签认。"""
    return service.revoke_signoff(entry_id, signoff_id)


@router.post("/{entry_id}/signoffs/{signoff_id}/conflict")
def mark_conflict(entry_id: int, signoff_id: int, payload: ConflictPayload) -> dict[str, Any]:
    """标记预览后发现的授权冲突。"""
    return service.mark_signoff_conflict(entry_id, signoff_id, payload.remark)


@router.get("/{entry_id}/plan")
def get_plan(entry_id: int) -> dict[str, Any]:
    """生成/复查清理计划，已完成的物理分区不会被重复安排。"""
    return service.build_cleanup_plan(entry_id)


@router.get("/{entry_id}/preview")
def preview(entry_id: int) -> dict[str, Any]:
    """预览清理范围与有效签认范围。"""
    return service.preview_cleanup_scope(entry_id)


@router.post("/{entry_id}/execute")
def execute(entry_id: int, payload: PlanPayload) -> dict[str, Any]:
    """确认执行：confirm=true 按有效签认范围删除并写执行记录。"""
    return service.execute_cleanup(entry_id, confirm=payload.confirm)


@router.get("/{entry_id}/execution-records")
def execution_records(entry_id: int) -> dict[str, Any]:
    """复查执行记录：已确认分区与因签认缺失未执行的分区。"""
    return service.get_execution_records(entry_id)


@router.get("/{entry_id}/retention-sync")
def retention_sync(entry_id: int) -> dict[str, Any]:
    """与数据保留策略联动并刷新物理分区清理计划。"""
    return service.sync_with_retention_policy(entry_id)
