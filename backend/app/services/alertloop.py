"""告警处置闭环引擎：周期判定、告警单去重、静默抑制、恢复流转与诊断联动。

设计口径（与需求逐条对应）：

1. 指标连续 ``持续周期`` 个周期命中比较条件才触发，只越阈一两个周期不产生告警单；
   越阈后立即恢复（命中中断）会把连续计数清零，不会留下任何告警单。
2. 同一任务 + 同一指标 + 同一异常类型在恢复前只保留一张告警单，重复上报只更新
   「最后发生时间 / 触发次数 / 连续周期数 / 影响行数」，绝不新建第二张。
3. 告警单进入「待处理」的同时，在诊断工作台生成关联报告（含影响行数、异常堆栈、
   修复建议），两边通过告警单号互相引用，实现跨模块联动。
4. 指标恢复后告警单向「已关闭」做单向流转，诊断报告保留处理结论，不被下一次重复
   告警覆盖；恢复与诊断结论冲突时，以已落库告警单当前状态为准。
5. 静默时段内再次触发：只更新未关闭告警单的最后发生时间，不发新通知、不建新单；
   若告警单已在静默窗口内关闭，重新越阈也必须等静默结束才允许开新单。
6. 并发采集同时上报：进程内用可重入锁串行化判定，并以事件幂等键去重，同一事件
   重放不会产生第二张告警单。
7. 告警规则被关闭后不再参与任何判定；规则关闭前已经存在的告警单保持原状态，
   只能人工处置，不会被规则关闭连带自动恢复。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.services.alertrule import AlertruleService
from app.services.diagnose import DiagnoseService
from app.store import store

TICKET_MODULE = "alertticket"
TICKET_STATUSES = ["待处理", "处理中", "已关闭"]
# 告警单单向状态机：待处理 -> 处理中 -> 已关闭，不允许回退。
TICKET_ACTIONS = {"认领处置": "处理中", "关闭告警单": "已关闭"}
OPEN_STATUSES = {"待处理", "处理中"}

COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "大于": lambda value, threshold: value > threshold,
    "大于等于": lambda value, threshold: value >= threshold,
    "小于": lambda value, threshold: value < threshold,
    "小于等于": lambda value, threshold: value <= threshold,
    "等于": lambda value, threshold: value == threshold,
    "不等于": lambda value, threshold: value != threshold,
}

# 常见异常类型的默认修复建议；上报事件里带了修复建议时优先用事件里的。
DEFAULT_ADVICE: dict[str, str] = {
    "HTTP 503": "目标服务暂不可用，切换备用代理节点并对失败批次执行重试，必要时联系上游确认恢复时间",
    "HTTP 500": "上游服务内部错误，降低采集频率并对失败批次重试，持续出现时联系站点维护方",
    "HTTP 429": "触发限流，调大限速策略的冷却秒数并切换出口 IP，待限流窗口结束后恢复",
    "TIMEOUT": "请求超时，检查代理链路与目标站点负载，适当调大超时秒数后重试失败批次",
}


def _now_text(clock: Callable[[], datetime]) -> str:
    return clock().strftime("%Y-%m-%d %H:%M:%S")


class _MetricState:
    """单个「任务+指标+异常类型」维度的运行态计数（不入库，进程内状态）。"""

    def __init__(self) -> None:
        self.streak: int = 0          # 当前连续命中周期数
        self.open_ticket_id: int | None = None  # 未关闭告警单的行 id
        self.last_closed_at: datetime | None = None  # 最近一次关单时间（静默窗口用）


class AlertLoopEngine:
    """闭环引擎单例：所有指标上报都经过这里，保证判定与落库串行一致。"""

    def __init__(
        self,
        *,
        rule_service: AlertruleService | None = None,
        diagnose_service: DiagnoseService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._rule_service = rule_service or AlertruleService()
        self._diagnose_service = diagnose_service or DiagnoseService()
        self._clock: Callable[[], datetime] = clock or datetime.now
        self._states: dict[tuple[str, str, str], _MetricState] = {}
        # 已处理过的事件幂等键：事件重放时直接返回原结果，不重复落库。
        self._processed_events: dict[str, dict[str, Any]] = {}
        self._rebuild_state()

    # ------------------------------------------------------------------ 内部工具
    def _rebuild_state(self) -> None:
        """服务重启后从已落库告警单重建运行态，未关闭的单子继续被去重逻辑接管。"""
        for row in store.rows(TICKET_MODULE):
            key = (
                str(row.get("关联任务", "")),
                str(row.get("监控指标", "")),
                str(row.get("异常类型", "")),
            )
            state = self._states.setdefault(key, _MetricState())
            if row.get("status") in OPEN_STATUSES:
                state.open_ticket_id = int(row["id"])
                state.streak = int(row.get("连续周期数", 0) or 0)

    def reset_runtime(self) -> None:
        """重置引擎运行态并按当前仓库内容重建：给测试与 store.reset 配套使用。"""
        with self._lock:
            self._states.clear()
            self._processed_events.clear()
            self._rebuild_state()

    @property
    def write_lock(self) -> threading.RLock:
        """闭环写锁：人工处置服务与采集上报共用，保证同一张单子不会被并发改写。"""
        return self._lock

    def current_time(self) -> datetime:
        """引擎当前时钟：静默窗口与关单时间统一从这里取，测试可注入虚拟时钟。"""
        return self._clock()

    def mark_ticket_closed(self, task_code: str, metric: str, exception_type: str) -> None:
        """人工关单后同步引擎运行态：解除未关闭引用并开始静默计时。"""
        state = self._states.get(self._state_key(task_code, metric, exception_type))
        if state is not None:
            state.open_ticket_id = None
            state.last_closed_at = self._clock()

    @staticmethod
    def _state_key(task_code: str, metric: str, exception_type: str) -> tuple[str, str, str]:
        return task_code, metric, exception_type

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def _next_ticket_code(self, clock: Callable[[], datetime], rule_code: str) -> str:
        """生成告警单号：规则编号-yyyymmdd-序号（序号在同规则当日已有单号上递增）。"""
        day = clock().strftime("%Y%m%d")
        prefix = f"{rule_code}-{day}-"
        serial = 0
        for row in store.rows(TICKET_MODULE):
            code = str(row.get("告警单号", ""))
            if code.startswith(prefix):
                try:
                    serial = max(serial, int(code.rsplit("-", 1)[-1]))
                except ValueError:
                    continue
        return f"{prefix}{serial + 1:03d}"

    def find_open_ticket(
        self,
        task_code: str,
        metric: str,
        exception_type: str,
    ) -> dict[str, Any] | None:
        """以已落库数据为准找未关闭告警单（冲突裁决的唯一事实来源）。"""
        for row in store.rows(TICKET_MODULE):
            if (
                row.get("status") in OPEN_STATUSES
                and str(row.get("关联任务", "")) == task_code
                and str(row.get("监控指标", "")) == metric
                and str(row.get("异常类型", "")) == exception_type
            ):
                return row
        return None

    @staticmethod
    def _is_breached(rule: dict[str, Any], value: float) -> bool:
        comparator = COMPARATORS.get(str(rule.get("比较运算符", "")))
        if comparator is None:
            return False
        threshold = float(rule.get("阈值数值", 0))
        return comparator(value, threshold)

    def _within_silence(self, state: _MetricState, silence_minutes: int) -> bool:
        if silence_minutes <= 0 or state.last_closed_at is None:
            return False
        delta = (self._clock() - state.last_closed_at).total_seconds()
        return delta < silence_minutes * 60

    def _create_ticket_and_diagnosis(
        self,
        *,
        rule: dict[str, Any],
        event: dict[str, Any],
        state: _MetricState,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """命中触发：落一张告警单，同时在诊断工作台生成关联报告。"""
        now = _now_text(self._clock)
        ticket_rows = store.rows(TICKET_MODULE)
        ticket = {
            "id": max((int(row.get("id", 0)) for row in ticket_rows), default=0) + 1,
            "status": "待处理",
            "pending": True,
            "abnormal": True,
            "告警单号": self._next_ticket_code(self._clock, str(rule.get("告警编号", ""))),
            "规则编号": str(rule.get("告警编号", "")),
            "关联任务": str(event.get("task_code", "")),
            "监控指标": str(rule.get("监控指标", "")),
            "异常类型": str(event.get("exception_type", "未分类异常")),
            "告警级别": str(rule.get("告警级别", "普通")),
            "通知渠道": str(rule.get("通知渠道", "")),
            "首次发生时间": now,
            "最后发生时间": now,
            "恢复时间": None,
            "连续周期数": state.streak,
            # 触发次数把凑满持续周期过程中的命中周期一并计入，之后每次重复上报再 +1。
            "触发次数": state.streak,
            "影响行数": self._as_int(event.get("affected_rows")),
            "关联诊断报告": None,
            "处理结论": None,
        }
        ticket_rows.append(ticket)

        advice = str(
            event.get("repair_advice")
            or DEFAULT_ADVICE.get(str(event.get("exception_type", "")), "排查采集链路并对失败批次重试")
        )
        report = self._diagnose_service.create_for_alert({
            "报告编号": self._diagnose_service.next_report_code(),
            "关联任务": ticket["关联任务"],
            "异常类型": ticket["异常类型"],
            "异常堆栈": str(event.get("stack_trace", "未采集到异常堆栈")),
            "发生时间": now,
            "影响行数": ticket["影响行数"],
            "根因分类": str(event.get("root_cause", "待分析")),
            "修复建议": advice,
            "关联告警单": ticket["告警单号"],
            "处理结论": None,
        })
        ticket["关联诊断报告"] = report["报告编号"]
        state.open_ticket_id = int(ticket["id"])
        return ticket, report

    # ------------------------------------------------------------------ 对外能力
    def report_metric(self, event: dict[str, Any]) -> dict[str, Any]:
        """采集任务上报一个周期的指标值，引擎据此完成触发/去重/恢复全闭环。

        事件字段：event_id（幂等键）、task_code、metric、value、exception_type、
        affected_rows、stack_trace、root_cause、repair_advice。
        """
        event_id = str(event.get("event_id") or "").strip()
        with self._lock:
            if event_id and event_id in self._processed_events:
                # 并发/重试导致的同一事件重放：幂等返回，绝不重复落库。
                cached = dict(self._processed_events[event_id])
                cached["deduplicated"] = True
                return cached

            task_code = str(event.get("task_code") or "").strip()
            metric = str(event.get("metric") or "").strip()
            exception_type = str(event.get("exception_type") or "未分类异常").strip()
            if not task_code or not metric:
                return {"ok": False, "action": "rejected", "message": "缺少 task_code 或 metric"}
            try:
                value = float(event.get("value", 0))
            except (TypeError, ValueError):
                return {"ok": False, "action": "rejected", "message": "指标值 value 不是合法数字"}

            key = self._state_key(task_code, metric, exception_type)
            state = self._states.setdefault(key, _MetricState())
            rule = self._rule_service.find_active_rule(metric)
            if rule is None:
                # 规则被关闭或从未配置：不做任何告警动作，也不改动已有告警单。
                return {
                    "ok": True,
                    "action": "rule_inactive",
                    "message": f"指标「{metric}」没有生效中的告警规则，已忽略本次上报",
                }

            duration = max(self._as_int(rule.get("持续周期"), 1), 1)
            silence_minutes = max(self._as_int(rule.get("静默时段"), 0), 0)
            breached = self._is_breached(rule, value)
            now = _now_text(self._clock)
            open_ticket = self.find_open_ticket(task_code, metric, exception_type)

            if not breached:
                # 指标恢复：连续计数清零；若存在未关闭告警单则单向流转到已关闭。
                state.streak = 0
                if open_ticket is None:
                    # 越阈后立即恢复、从未触发过：什么都不落，保持干净。
                    result = {
                        "ok": True,
                        "action": "recovered_without_ticket",
                        "message": "指标已回落且无未关闭告警单，无需处置",
                    }
                else:
                    open_ticket["status"] = "已关闭"
                    open_ticket["pending"] = False
                    open_ticket["abnormal"] = False
                    open_ticket["恢复时间"] = now
                    open_ticket["处理结论"] = open_ticket.get("处理结论") or "指标恢复正常，系统自动关闭告警单"
                    state.open_ticket_id = None
                    state.last_closed_at = self._clock()
                    result = {
                        "ok": True,
                        "action": "auto_closed",
                        "message": "指标已恢复，告警单单向流转为已关闭，诊断报告结论保留",
                        "ticket": dict(open_ticket),
                    }
                if event_id:
                    self._processed_events[event_id] = dict(result)
                return result

            # 越阈：累计连续周期。
            state.streak += 1
            if open_ticket is not None:
                # 恢复前同任务/指标/异常类型只保留一张单：重复触发只更新最后发生时间。
                open_ticket["最后发生时间"] = now
                open_ticket["触发次数"] = int(open_ticket.get("触发次数", 0) or 0) + 1
                open_ticket["连续周期数"] = state.streak
                latest_rows = self._as_int(event.get("affected_rows"))
                if latest_rows:
                    open_ticket["影响行数"] = latest_rows
                result = {
                    "ok": True,
                    "action": "suppressed_duplicate",
                    "message": "告警单尚未恢复，已更新最后发生时间，不新建第二张告警单",
                    "ticket": dict(open_ticket),
                }
                if event_id:
                    self._processed_events[event_id] = dict(result)
                return result

            # 没有未关闭单子：先看持续周期是否满足，再看静默窗口。
            if state.streak < duration:
                result = {
                    "ok": True,
                    "action": "accumulating",
                    "message": f"已连续命中 {state.streak}/{duration} 个周期，暂不触发",
                    "streak": state.streak,
                    "duration": duration,
                }
                if event_id:
                    self._processed_events[event_id] = dict(result)
                return result

            if self._within_silence(state, silence_minutes):
                result = {
                    "ok": True,
                    "action": "silenced",
                    "message": f"处于静默时段（{silence_minutes} 分钟）内，本次触发被抑制，不新建告警单",
                    "streak": state.streak,
                }
                if event_id:
                    self._processed_events[event_id] = dict(result)
                return result

            ticket, report = self._create_ticket_and_diagnosis(rule=rule, event=event, state=state)
            result = {
                "ok": True,
                "action": "triggered",
                "message": "持续周期已满足，生成告警单并联动创建诊断报告",
                "ticket": dict(ticket),
                "diagnosis": dict(report),
            }
            if event_id:
                self._processed_events[event_id] = dict(result)
            return result

    def ticket_state(self, task_code: str, metric: str, exception_type: str) -> dict[str, Any]:
        """查询某维度的连续计数与未关闭告警单，便于前端展示闭环进度。"""
        with self._lock:
            state = self._states.get(self._state_key(task_code, metric, exception_type))
            ticket = self.find_open_ticket(task_code, metric, exception_type)
            return {
                "task_code": task_code,
                "metric": metric,
                "exception_type": exception_type,
                "streak": state.streak if state else 0,
                "open_ticket": dict(ticket) if ticket else None,
            }


# 进程内单例：路由层与测试都通过它访问闭环能力，锁与运行态因此全局唯一。
engine = AlertLoopEngine()
