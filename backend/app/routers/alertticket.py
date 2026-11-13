"""告警单接口：告警处置闭环的工作台入口与采集指标上报入口。

- ``POST /ingest``：采集任务每个周期把指标值上报给闭环引擎，引擎负责持续周期判定、
  告警单去重、静默抑制、恢复关单与诊断报告联动。
- 常规列表/明细/动作接口与其他模块保持同一套口径。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alertloop import TICKET_STATUSES, engine
from app.services.alertticket import AlertTicketService

router = APIRouter(prefix="/api/alertticket", tags=["告警处置"])

service = AlertTicketService()

LIST_FIELDS = [
    "告警单号", "规则编号", "关联任务", "监控指标", "异常类型", "告警级别",
    "通知渠道", "首次发生时间", "最后发生时间", "恢复时间", "连续周期数",
    "触发次数", "影响行数", "关联诊断报告", "处理结论",
]
STATUSES = TICKET_STATUSES


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按告警单号或规则编号检索"),
    status: str | None = Query(default=None, description="待处理、处理中、已关闭"),
    task_code: str | None = Query(default=None, description="按关联任务编号过滤"),
    linked_report: str | None = Query(default=None, description="按关联诊断报告编号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按单号、状态、任务、关联报告过滤告警单；没有数据时返回空页。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        task_code=task_code,
        linked_report=linked_report,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/stats")
def ticket_stats() -> dict[str, int]:
    """工作台卡片：各状态告警单数量。"""
    return service.stats()


@router.get("/state")
def ticket_state(
    task_code: str = Query(..., description="任务编号，如 TASK-1042"),
    metric: str = Query(..., description="监控指标，如 失败条数"),
    exception_type: str = Query("未分类异常", description="异常类型，如 HTTP 503"),
) -> dict[str, Any]:
    """查询某任务/指标/异常类型维度的连续周期计数与未关闭告警单。"""
    return engine.ticket_state(task_code, metric, exception_type)


@router.post("/ingest", response_model=ActionResult)
def ingest_metric(payload: EntryPayload) -> ActionResult:
    """采集周期指标上报：驱动持续周期判定、去重、静默与恢复闭环。"""
    values = payload.values
    result = engine.report_metric(values)
    if not result.get("ok"):
        return ActionResult(ok=False, message=str(result.get("message", "上报被拒绝")))
    entry = result.get("ticket")
    return ActionResult(ok=True, message=str(result.get("message", "已处理")), entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单张告警单明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警单 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对告警单执行认领处置、关闭告警单；状态单向流转，已关闭不可回退。"""
    values = payload.values
    action = str(values.get("action") or "").strip()
    conclusion = values.get("conclusion") or payload.remark
    entry, message = service.run_action(entry_id, action, conclusion=conclusion)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出告警单全量清单。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alertticket", "total": total, "items": items}
