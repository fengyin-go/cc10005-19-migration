"""登录凭证接口：维护凭证配置，覆盖登记、提交绑定、启用、失效与站点转交。

绑定/转交/解绑是站点与凭证关系的正式写入，服务端在单事务里提交两边修改；
版本过期或写锁竞争时返回 409 冲突回执，前端可凭 current_version 刷新重试。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db import ConflictError
from app.schemas import ActionResult, BindingPayload, EntryPayload, PageResult
from app.services.auth import AuthService

router = APIRouter(prefix="/api/auth", tags=["登录凭证"])

service = AuthService()

LIST_FIELDS = ["凭证编号", "目标站点", "认证方式", "账号标识", "凭据有效天数", "会话保持", "刷新触发", "失效告警"]
STATUSES = ["草稿", "待绑定", "已生效", "已失效"]


def _conflict(exc: ConflictError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"message": exc.message, "current_version": exc.current_version},
    )


# 静态路径放在 /{entry_id} 之前，避免被当成凭证 id 解析。
@router.get("/migration-issues")
def migration_issues() -> dict[str, Any]:
    """迁移遗留问题：重复业务编号、无法匹配目标站点的凭证都在这里可查。"""
    return {"items": service.migration_issues()}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出登录凭证清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "auth", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按凭证编号检索"),
    status: str | None = Query(default=None, description="草稿、待绑定、已生效、已失效"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按凭证编号与状态过滤登录凭证列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条凭证配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"凭证配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条凭证配置，缺字段或编号重复时说明原因而不是静默丢弃。"""
    try:
        entry, missing = service.create_entry(payload.values)
    except ConflictError as exc:
        raise _conflict(exc) from exc
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="凭证配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条凭证配置执行提交绑定、启用凭证、标记失效；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(
            entry_id, action, expected_version=payload.version
        )
    except ConflictError as exc:
        raise _conflict(exc) from exc
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/bindings", response_model=ActionResult, status_code=200)
def bind_entry(entry_id: int, payload: BindingPayload) -> ActionResult:
    """把凭证绑定到目标站点；已绑定其他站点时按转交处理，冲突时返回 409。"""
    try:
        entry, message = service.bind_entry(
            entry_id, payload.site_id, expected_version=payload.version
        )
    except ConflictError as exc:
        raise _conflict(exc) from exc
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=message,
        entry=entry,
        current_version=entry.get("version"),
    )


@router.delete("/{entry_id}/bindings", response_model=ActionResult)
def unbind_entry(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """解除凭证与站点的绑定，凭证回到草稿；版本过期时返回 409。"""
    expected_version = payload.version if payload is not None else None
    try:
        entry, message = service.unbind_entry(
            entry_id, expected_version=expected_version
        )
    except ConflictError as exc:
        raise _conflict(exc) from exc
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
