"""诊断记录接口：维护诊断报告，覆盖开始分析、提交结论、标记结案、归档报告等动作。

告警闭环引擎创建的报告会带 ``关联告警单`` 字段；工作台可按该字段反查一张告警单
对应的诊断结论。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.diagnose import DiagnoseService

router = APIRouter(prefix="/api/diagnose", tags=["诊断记录"])

service = DiagnoseService()

LIST_FIELDS = ["报告编号", "关联任务", "异常类型", "异常堆栈", "发生时间", "影响行数", "根因分类", "修复建议", "关联告警单", "处理结论"]
STATUSES = ["草稿", "分析中", "已结案", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报告编号检索"),
    status: str | None = Query(default=None, description="草稿、分析中、已结案、已归档"),
    linked_alert: str | None = Query(default=None, description="按关联告警单号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按报告编号、状态与关联告警单过滤诊断记录列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        linked_alert=linked_alert,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条诊断报告明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"诊断报告 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条诊断报告，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段或字段不合法：{'、'.join(missing)}")
    return ActionResult(ok=True, message="诊断报告已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条诊断报告执行开始分析、提交结论、标记结案、归档报告；不允许的动作会被拦下。"""
    action = str(payload.values.get("action") or "").strip()
    conclusion = payload.values.get("conclusion") or payload.remark
    entry, message = service.run_action(entry_id, action, conclusion=conclusion)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出诊断记录清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "diagnose", "total": total, "items": items}
