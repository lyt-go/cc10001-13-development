"""诊断记录业务规则：报告登记、跨模块联动与结案结论保护。

诊断报告可以由告警闭环引擎在告警单进入「待处理」时自动生成，也可以人工登记。
报告一旦进入「已结案/已归档」，其处理结论即固化，后续任何重复告警或规则恢复
都不允许覆盖结论（告警恢复与诊断结论冲突时，以已落库告警单当前状态为准，
诊断结论原样保留）。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "diagnose"
REQUIRED_FIELDS = ["报告编号", "关联任务", "异常类型"]
ALL_FIELDS = [
    "报告编号",
    "关联任务",
    "异常类型",
    "异常堆栈",
    "发生时间",
    "影响行数",
    "根因分类",
    "修复建议",
    "关联告警单",
    "处理结论",
]
STATUS_ORDER = ["草稿", "分析中", "已结案", "已归档"]
# 提交结论与标记结案都落到「已结案」；状态只允许沿序列单向推进。
ACTION_RULES = {"开始分析": "分析中", "标记结案": "已结案", "提交结论": "已结案", "归档报告": "已归档"}
FINAL_STATUSES = {"已结案", "已归档"}
NEGATIVE_ACTIONS = []


class DiagnoseService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        linked_alert: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if linked_alert:
            rows = [row for row in rows if str(row.get("关联告警单", "")) == linked_alert]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_by_code(self, report_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("报告编号", "")) == report_code:
                return row
        return None

    def next_report_code(self) -> str:
        """生成下一个诊断报告编号（DIAG-序号），序号取现有编号最大后缀 +1。"""
        serial = 0
        for row in store.rows(MODULE):
            code = str(row.get("报告编号", ""))
            if code.startswith("DIAG-"):
                try:
                    serial = max(serial, int(code.rsplit("-", 1)[1]))
                except ValueError:
                    continue
        return f"DIAG-{serial + 1:03d}"

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        report_code = str(values.get("报告编号") or "").strip()
        if any(str(row.get("报告编号", "")) == report_code for row in rows):
            return None, [f"报告编号 {report_code} 已存在"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in ALL_FIELDS:
            entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def create_for_alert(self, values: dict[str, Any]) -> dict[str, Any]:
        """告警闭环引擎专用：告警单进入待处理时生成「分析中」的关联诊断报告。

        调用方（闭环引擎）已持有全局写锁，这里不再重复加锁。
        """
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in ALL_FIELDS:
            entry[field] = values.get(field)
        entry.setdefault("报告编号", self.next_report_code())
        entry["status"] = "分析中"
        entry["pending"] = True
        entry["abnormal"] = True
        rows.append(entry)
        return entry

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        conclusion: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"诊断报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于诊断记录可执行范围"
        target = ACTION_RULES[action]
        current_index = STATUS_ORDER.index(entry["status"]) if entry.get("status") in STATUS_ORDER else -1
        target_index = STATUS_ORDER.index(target)
        if target_index <= current_index:
            return None, (
                f"报告当前为「{entry['status']}」，状态只能单向向前流转，"
                f"不能执行「{action}」"
            )
        if action == "提交结论":
            text = str(conclusion or "").strip()
            if not text:
                return None, "提交结论时必须填写处理结论"
            entry["处理结论"] = text
        elif target == "已结案" and not str(entry.get("处理结论") or "").strip():
            # 直接走「标记结案」也要落一句结论，保证闭环后报告始终可追溯。
            entry["处理结论"] = f"{entry.get('报告编号', '报告')} 已完成分析并结案"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"诊断报告已{action}"

    def is_finalized(self, entry: dict[str, Any]) -> bool:
        """报告是否已固化：固化后的结论不允许被重复告警或恢复动作覆盖。"""
        return entry.get("status") in FINAL_STATUSES
