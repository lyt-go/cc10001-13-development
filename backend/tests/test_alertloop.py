"""告警处置闭环的引擎级测试：持续周期、去重、静默、并发、幂等与恢复边界。"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta

from app.services.alertloop import AlertLoopEngine
from app.services.alertrule import AlertruleService
from app.services.alertticket import AlertTicketService
from app.services.diagnose import DiagnoseService
from app.store import store
from tests.helpers import reset_loop_tables


class MutableClock:
    """可手动拨快的时钟：让静默窗口等时间相关边界可以确定性测试。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 11, 13, 9, 0, 0)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


class AlertLoopEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_loop_tables()
        self.clock = MutableClock()
        self.engine = AlertLoopEngine(
            rule_service=AlertruleService(),
            diagnose_service=DiagnoseService(),
            clock=self.clock,
        )

    def _ingest(
        self,
        task: str,
        *,
        value: float,
        event_id: str | None = None,
        exception: str = "HTTP 503",
        rows: int = 100,
        metric: str = "失败条数",
    ) -> dict:
        return self.engine.report_metric({
            "event_id": event_id,
            "task_code": task,
            "metric": metric,
            "value": value,
            "exception_type": exception,
            "affected_rows": rows,
            "stack_trace": "HTTP 503 Service Unavailable",
        })

    def test_triggers_only_after_consecutive_duration(self) -> None:
        """ALERT-06：失败条数大于 50、持续 3 个周期才触发；前两期只累计。"""
        r1 = self._ingest("TASK-DUR-1", value=51, event_id="e1")
        r2 = self._ingest("TASK-DUR-1", value=60, event_id="e2")
        self.assertEqual(r1["action"], "accumulating")
        self.assertEqual(r2["action"], "accumulating")
        self.assertEqual(store.rows("alertticket"), [])  # 还没到持续周期，无单子

        r3 = self._ingest("TASK-DUR-1", value=80, event_id="e3", rows=318)
        self.assertEqual(r3["action"], "triggered")
        ticket = r3["ticket"]
        self.assertEqual(ticket["规则编号"], "ALERT-06")
        self.assertEqual(ticket["关联任务"], "TASK-DUR-1")
        self.assertEqual(ticket["异常类型"], "HTTP 503")
        self.assertEqual(ticket["影响行数"], 318)
        self.assertEqual(ticket["status"], "待处理")
        # 触发同时联动诊断报告，记录影响行数、堆栈与修复建议
        report = r3["diagnosis"]
        self.assertEqual(report["关联任务"], "TASK-DUR-1")
        self.assertEqual(report["异常类型"], "HTTP 503")
        self.assertEqual(report["影响行数"], 318)
        self.assertIn("HTTP 503", report["异常堆栈"])
        self.assertTrue(report["修复建议"])
        self.assertEqual(report["关联告警单"], ticket["告警单号"])
        self.assertEqual(ticket["关联诊断报告"], report["报告编号"])

    def test_duplicate_reports_keep_single_ticket_and_update_last_seen(self) -> None:
        """恢复前同任务/指标/异常类型只有一张单，重复触发只更新最后发生时间。"""
        for idx in range(3):
            self._ingest("TASK-1042", value=51 + idx, event_id=f"d{idx}")
        before = [r for r in store.rows("alertticket") if r["关联任务"] == "TASK-1042"]
        self.assertEqual(len(before), 1)
        first_seen = before[0]["首次发生时间"]
        last_seen = before[0]["最后发生时间"]

        self.clock.advance(10)
        dup = self._ingest("TASK-1042", value=99, event_id="d3", rows=400)
        self.assertEqual(dup["action"], "suppressed_duplicate")
        after = [r for r in store.rows("alertticket") if r["关联任务"] == "TASK-1042"]
        self.assertEqual(len(after), 1)  # 没有第二张单
        self.assertEqual(after[0]["触发次数"], 4)
        self.assertEqual(after[0]["影响行数"], 400)
        self.assertEqual(after[0]["首次发生时间"], first_seen)  # 首次时间不变
        self.assertGreater(after[0]["最后发生时间"], last_seen)  # 最后发生时间被更新
        # 诊断报告仍然只有一张，且不被重复告警覆盖
        reports = [r for r in store.rows("diagnose") if r.get("关联告警单") == after[0]["告警单号"]]
        self.assertEqual(len(reports), 1)

    def test_immediate_recovery_resets_streak_without_ticket(self) -> None:
        """越阈后立即恢复：连续计数清零，不产生任何告警单；重新越阈要再凑满周期。"""
        self.assertEqual(self._ingest("TASK-FLAP", value=51, event_id="f1")["action"], "accumulating")
        self.assertEqual(self._ingest("TASK-FLAP", value=10, event_id="f2")["action"],
                         "recovered_without_ticket")
        self.assertEqual(store.rows("alertticket"), [])

        # 重新越阈：计数从头开始，两期不够触发
        self.assertEqual(self._ingest("TASK-FLAP", value=60, event_id="f3")["action"], "accumulating")
        self.assertEqual(self._ingest("TASK-FLAP", value=70, event_id="f4")["action"], "accumulating")
        self.assertEqual([r for r in store.rows("alertticket") if r["关联任务"] == "TASK-FLAP"], [])
        self.assertEqual(self._ingest("TASK-FLAP", value=80, event_id="f5")["action"], "triggered")

    def test_recovery_closes_ticket_and_preserves_diagnosis(self) -> None:
        """恢复后告警单单向流转到已关闭；诊断报告与结论保留，不被覆盖。"""
        for idx in range(3):
            self._ingest("TASK-REC", value=60, event_id=f"r{idx}")
        ticket = self.engine.find_open_ticket("TASK-REC", "失败条数", "HTTP 503")
        self.assertIsNotNone(ticket)
        report = DiagnoseService().get_by_code(str(ticket["关联诊断报告"]))
        self.assertEqual(report["status"], "分析中")

        self.clock.advance(5)
        closed = self._ingest("TASK-REC", value=5, event_id="r9")
        self.assertEqual(closed["action"], "auto_closed")
        self.assertEqual(ticket["status"], "已关闭")
        self.assertIsNotNone(ticket["恢复时间"])
        self.assertFalse(ticket["pending"])

        # 再次越阈三周期：生成的是一张全新告警单 + 全新诊断报告，旧报告原样保留
        self.clock.advance(30)  # 跳过静默窗口
        for idx in range(3):
            self._ingest("TASK-REC", value=70, event_id=f"r2{idx}")
        open_rows = [r for r in store.rows("alertticket") if r["关联任务"] == "TASK-REC"]
        self.assertEqual(len(open_rows), 2)
        old_report = DiagnoseService().get_by_code(report["报告编号"])
        self.assertEqual(old_report["关联告警单"], ticket["告警单号"])  # 旧关联未被改写
        self.assertEqual(old_report["异常堆栈"], report["异常堆栈"])

    def test_silence_window_suppresses_new_ticket(self) -> None:
        """静默时段内再次触发被抑制：不建新单；窗口结束后才能开新单。"""
        for idx in range(3):
            self._ingest("TASK-SIL", value=60, event_id=f"s{idx}")
        ticket = self.engine.find_open_ticket("TASK-SIL", "失败条数", "HTTP 503")
        # 走真实人工关单路径：同步引擎静默计时起点，不触碰内部状态
        closed, _ = AlertTicketService(self.engine).run_action(
            int(ticket["id"]), "关闭告警单", conclusion="人工确认恢复"
        )
        self.assertEqual(closed["status"], "已关闭")

        # 关单后立刻连续越阈：静默 15 分钟内全部被抑制
        for idx in range(4):
            self.clock.advance(1)
            result = self._ingest("TASK-SIL", value=90, event_id=f"sq{idx}")
            self.assertEqual(result["action"], "silenced")
        sil_rows = [r for r in store.rows("alertticket") if r["关联任务"] == "TASK-SIL"]
        self.assertEqual(len(sil_rows), 1)  # 仍然只有原来那张已关闭的单

        self.clock.advance(15)
        triggered = self._ingest("TASK-SIL", value=90, event_id="sq9")
        self.assertEqual(triggered["action"], "triggered")
        self.assertEqual(len([r for r in store.rows("alertticket") if r["关联任务"] == "TASK-SIL"]), 2)

    def test_closed_rule_stops_evaluation_but_keeps_existing_ticket(self) -> None:
        """规则被关闭：新上报不再判定/关单；已存在的告警单保持原状等人工处置。"""
        for idx in range(3):
            self._ingest("TASK-RC", value=60, event_id=f"c{idx}")
        ticket = self.engine.find_open_ticket("TASK-RC", "失败条数", "HTTP 503")
        self.assertIsNotNone(ticket)

        rule = AlertruleService().get_by_code("ALERT-06")
        rule["status"] = "已关闭"
        # 规则关闭后即使指标恢复，也不会自动关掉已有单子
        result = self._ingest("TASK-RC", value=1, event_id="c9")
        self.assertEqual(result["action"], "rule_inactive")
        self.assertEqual(ticket["status"], "待处理")

        # 重新生效后，恢复上报才能驱动关单
        rule["status"] = "已生效"
        recovered = self._ingest("TASK-RC", value=1, event_id="c10")
        self.assertEqual(recovered["action"], "auto_closed")
        self.assertEqual(ticket["status"], "已关闭")

    def test_concurrent_reports_serialized_with_single_ticket(self) -> None:
        """并发采集同时上报：锁串行化 + 幂等键去重，最终只落一张告警单。"""
        errors: list[Exception] = []

        def worker(seq: int) -> None:
            try:
                self._ingest("TASK-CONC", value=60 + seq, event_id=f"conc-{seq}")
            except Exception as exc:  # pragma: no cover - 并发异常需要显式暴露
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rows = [r for r in store.rows("alertticket") if r["关联任务"] == "TASK-CONC"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["触发次数"], 6)

    def test_same_event_replayed_is_idempotent(self) -> None:
        """同一 event_id 重放（网络重试）：幂等返回，不重复落库。"""
        first = self._ingest("TASK-IDEM", value=70, event_id="same-id")
        for _ in range(2):
            self._ingest("TASK-IDEM", value=70, event_id="same-id")
        self.assertEqual(first["action"], "accumulating")
        rows = [r for r in store.rows("alertticket") if r["关联任务"] == "TASK-IDEM"]
        self.assertEqual(rows, [])
        # 凑满另外两期，第三期正常触发
        self._ingest("TASK-IDEM", value=70, event_id="idem-2")
        triggered = self._ingest("TASK-IDEM", value=70, event_id="idem-3")
        self.assertEqual(triggered["action"], "triggered")
        replay = self._ingest("TASK-IDEM", value=70, event_id="idem-3")
        self.assertTrue(replay.get("deduplicated"))
        rows = [r for r in store.rows("alertticket") if r["关联任务"] == "TASK-IDEM"]
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
