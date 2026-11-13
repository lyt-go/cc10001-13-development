"""告警单业务规则：告警单的查询、单向状态流转与跨模块联动写回。

告警单本身由 ``app.services.alertloop`` 引擎在指标命中时自动创建，本服务负责
工作台侧的列表筛选与人工处置动作（认领、关闭），并把处置结论同步给关联的诊断
报告；诊断报告一旦结案即固化，任何动作都不会回写覆盖它。
"""
from __future__ import annotations

from typing import Any

from app.services.alertloop import TICKET_ACTIONS, TICKET_STATUSES, engine as default_engine
from app.services.diagnose import DiagnoseService
from app.store import store

MODULE = "alertticket"
FINAL_STATUS = "已关闭"


class AlertTicketService:
    def __init__(self, loop_engine=None) -> None:
        # 允许注入闭环引擎（测试用虚拟时钟）；默认走进程内单例。
        self._engine = loop_engine or default_engine
        self._diagnose = DiagnoseService()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        task_code: str | None = None,
        linked_report: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("告警单号", ""))
                or keyword in str(row.get("规则编号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if task_code:
            rows = [row for row in rows if str(row.get("关联任务", "")) == task_code]
        if linked_report:
            rows = [row for row in rows if str(row.get("关联诊断报告", "")) == linked_report]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def stats(self) -> dict[str, int]:
        rows = store.rows(MODULE)
        return {
            "total": len(rows),
            "pending": sum(1 for row in rows if row.get("status") == "待处理"),
            "processing": sum(1 for row in rows if row.get("status") == "处理中"),
            "closed": sum(1 for row in rows if row.get("status") == FINAL_STATUS),
        }

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        conclusion: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        # 与闭环引擎共用同一把锁，避免人工处置和采集上报并发写同一张单子。
        with self._engine.write_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"告警单 {entry_id} 不存在或已归档"
            if action not in TICKET_ACTIONS:
                return None, f"动作「{action}」不属于告警单可执行范围"
            target = TICKET_ACTIONS[action]
            current_index = (
                TICKET_STATUSES.index(entry["status"])
                if entry.get("status") in TICKET_STATUSES
                else -1
            )
            target_index = TICKET_STATUSES.index(target)
            # 单向状态机：已关闭的单子不允许任何回退或重复关闭。
            if target_index <= current_index:
                return None, (
                    f"告警单当前为「{entry['status']}」，状态只能单向向前流转，"
                    f"不能执行「{action}」"
                )

            entry["status"] = target
            entry["pending"] = target != FINAL_STATUS
            if target == FINAL_STATUS:
                entry["abnormal"] = False
                entry["恢复时间"] = entry.get("恢复时间") or self._engine.current_time().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                entry["处理结论"] = (
                    str(conclusion or "").strip()
                    or str(entry.get("处理结论") or "").strip()
                    or "人工关闭告警单"
                )
                # 关单后同步引擎运行态：静默窗口从这一刻开始计时。
                self._engine.mark_ticket_closed(
                    str(entry.get("关联任务", "")),
                    str(entry.get("监控指标", "")),
                    str(entry.get("异常类型", "")),
                )

            # 跨模块联动：把处置结论带给关联诊断报告；已结案/已归档的报告结论固化不动。
            report_code = entry.get("关联诊断报告")
            report = self._diagnose.get_by_code(str(report_code or "")) if report_code else None
            report_note = "诊断报告已保留原结论"
            if report is not None and not self._diagnose.is_finalized(report):
                if target == FINAL_STATUS and str(conclusion or "").strip():
                    report["处理结论"] = str(conclusion).strip()
                    report_note = "处置结论已同步到诊断报告"
                elif action == "认领处置":
                    report["status"] = "分析中"
                    report_note = "诊断报告保持分析中，等待结论"
            message = f"告警单已{action}"
            if report is not None:
                message = f"{message}；{report_note}（{report.get('报告编号')}）"
            return entry, message
