"""采集告警业务规则：规则登记、单向状态流转与阈值口径都收在这里。

规则本身只描述「什么指标、用什么比较符、超过阈值并持续几个周期才告警」，
真正的周期判定、告警单去重与恢复闭环在 ``app.services.alertloop`` 里。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "alertrule"
REQUIRED_FIELDS = ["告警编号", "监控指标", "比较运算符"]
ALL_FIELDS = [
    "告警编号",
    "监控指标",
    "比较运算符",
    "阈值数值",
    "持续周期",
    "告警级别",
    "通知渠道",
    "静默时段",
]
SUPPORTED_OPERATORS = {"大于", "大于等于", "小于", "小于等于", "等于", "不等于"}
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已关闭"]
# 单向状态机：只允许沿状态序列向前流转，不允许回退。
ACTION_RULES = {"提交评审": "规则评审中", "生效规则": "已生效", "关闭规则": "已关闭"}
NEGATIVE_ACTIONS = []


def _to_int(value: Any) -> int | None:
    """把前端传来的字符串/数字统一转成非负整数；无法转换时返回 None。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


class AlertruleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("告警编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_by_code(self, rule_code: str) -> dict[str, Any] | None:
        """按业务编号（如 ALERT-06）找规则，闭环引擎用它做规则匹配。"""
        for row in store.rows(MODULE):
            if str(row.get("告警编号", "")) == rule_code:
                return row
        return None

    def find_active_rule(self, metric: str) -> dict[str, Any] | None:
        """按监控指标找当前仍生效的规则；已关闭的规则不再参与任何告警判定。"""
        for row in store.rows(MODULE):
            if row.get("监控指标") == metric and row.get("status") == "已生效":
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        operator = str(values.get("比较运算符") or "").strip()
        if operator not in SUPPORTED_OPERATORS:
            return None, [f"比较运算符仅支持：{'、'.join(sorted(SUPPORTED_OPERATORS))}"]
        threshold = _to_int(values.get("阈值数值"))
        if threshold is None:
            return None, ["阈值数值必须是整数"]
        duration = _to_int(values.get("持续周期"))
        if duration is None or duration < 1:
            return None, ["持续周期必须是不小于 1 的整数"]
        silence = _to_int(values.get("静默时段"))
        if silence is None or silence < 0:
            return None, ["静默时段必须是不小于 0 的整数（分钟）"]

        rows = store.rows(MODULE)
        rule_code = str(values.get("告警编号") or "").strip()
        if any(str(row.get("告警编号", "")) == rule_code for row in rows):
            return None, [f"告警编号 {rule_code} 已存在"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in ALL_FIELDS:
            entry[field] = values.get(field)
        entry["阈值数值"] = threshold
        entry["持续周期"] = duration
        entry["静默时段"] = silence
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"告警规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集告警可执行范围"
        target = ACTION_RULES[action]
        current_index = STATUS_ORDER.index(entry["status"]) if entry.get("status") in STATUS_ORDER else -1
        target_index = STATUS_ORDER.index(target)
        # 单向流转：目标状态必须在当前状态之后；重复推进或回退一律拒绝。
        if target_index <= current_index:
            return None, (
                f"规则当前为「{entry['status']}」，状态只能单向向前流转，"
                f"不能执行「{action}」"
            )
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"告警规则已{action}"
