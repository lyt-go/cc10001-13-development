"""告警单/诊断/规则的服务级与接口级测试：单向流转、结论固化、冲突裁决与 HTTP 口径。"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.services.alertrule import AlertruleService
from app.services.alertloop import engine
from app.services.alertticket import AlertTicketService
from app.services.diagnose import DiagnoseService
from app.store import store
from tests.helpers import reset_loop_tables


class AlertTicketServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_loop_tables()
        self.rules = AlertruleService()
        self.tickets = AlertTicketService()
        self.reports = DiagnoseService()

    def _trigger_ticket(self, task: str = "TASK-SVC") -> dict:
        for idx in range(3):
            engine.report_metric({
                "event_id": f"{task}-{idx}",
                "task_code": task,
                "metric": "失败条数",
                "value": 60 + idx,
                "exception_type": "HTTP 503",
                "affected_rows": 318,
                "stack_trace": "HTTP 503 Service Unavailable",
            })
        return [r for r in store.rows("alertticket") if r["关联任务"] == task][0]

    def test_ticket_one_way_transitions(self) -> None:
        """告警单只能 待处理 -> 处理中 -> 已关闭，回退与重复动作被拒绝。"""
        ticket = self._trigger_ticket()
        entry, msg = self.tickets.run_action(int(ticket["id"]), "认领处置")
        self.assertEqual(entry["status"], "处理中")
        self.assertTrue(entry["pending"])

        bad, err = self.tickets.run_action(int(ticket["id"]), "认领处置")
        self.assertIsNone(bad)
        self.assertIn("单向", err)

        closed, msg = self.tickets.run_action(
            int(ticket["id"]), "关闭告警单", conclusion="已切换备用代理，失败批次重跑完成"
        )
        self.assertEqual(closed["status"], "已关闭")
        self.assertFalse(closed["pending"])
        self.assertEqual(closed["处理结论"], "已切换备用代理，失败批次重跑完成")
        self.assertIsNotNone(closed["恢复时间"])

        again, err = self.tickets.run_action(int(ticket["id"]), "关闭告警单")
        self.assertIsNone(again)
        self.assertIn("单向", err)

    def test_diagnosis_finalized_and_conflict_resolution(self) -> None:
        """诊断结论固化；恢复与诊断结论冲突时以告警单当前状态为准，结论原样保留。"""
        ticket = self._trigger_ticket("TASK-CFL")
        report = self.reports.get_by_code(str(ticket["关联诊断报告"]))

        # 诊断侧先结案并固化结论
        finalized, _ = self.reports.run_action(
            int(report["id"]), "提交结论", conclusion="根因为上游 503，等待站点恢复"
        )
        self.assertEqual(finalized["status"], "已结案")
        self.assertTrue(self.reports.is_finalized(finalized))
        # 已结案报告不允许再提交结论（单向 + 固化）
        again, err = self.reports.run_action(int(report["id"]), "提交结论", conclusion="试图覆盖")
        self.assertIsNone(again)
        self.assertIn("单向", err)

        # 指标恢复驱动关单：告警单状态以落库事实变为已关闭；诊断结论保持不变
        engine.report_metric({
            "event_id": "TASK-CFL-rec",
            "task_code": "TASK-CFL",
            "metric": "失败条数",
            "value": 0,
            "exception_type": "HTTP 503",
        })
        stored_ticket = store.find("alertticket", int(ticket["id"]))
        self.assertEqual(stored_ticket["status"], "已关闭")
        stored_report = store.find("diagnose", int(report["id"]))
        self.assertEqual(stored_report["处理结论"], "根因为上游 503，等待站点恢复")
        self.assertEqual(stored_report["status"], "已结案")

        # 静默窗口后重新越阈：开的是新单，旧报告不会被新告警覆盖（本用例关闭静默窗口）
        rule = self.rules.get_by_code("ALERT-06")
        rule["静默时段"] = 0
        for idx in range(3):
            engine.report_metric({
                "event_id": f"TASK-CFL-new{idx}",
                "task_code": "TASK-CFL",
                "metric": "失败条数",
                "value": 90,
                "exception_type": "HTTP 503",
                "affected_rows": 50,
            })
        old_report = store.find("diagnose", int(report["id"]))
        self.assertEqual(old_report["处理结论"], "根因为上游 503，等待站点恢复")
        self.assertEqual(len([r for r in store.rows("alertticket") if r["关联任务"] == "TASK-CFL"]), 2)

    def test_closing_ticket_syncs_open_report_conclusion(self) -> None:
        """人工关单带结论时，未结案的关联报告会同步结论；已结案报告保持原结论。"""
        ticket = self._trigger_ticket("TASK-SYNC")
        entry, message = self.tickets.run_action(
            int(ticket["id"]), "关闭告警单", conclusion="人工确认已恢复"
        )
        self.assertEqual(entry["status"], "已关闭")
        report = self.reports.get_by_code(str(ticket["关联诊断报告"]))
        self.assertEqual(report["处理结论"], "人工确认已恢复")
        self.assertIn("DIAG", message)


class AlertruleServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_loop_tables()
        self.service = AlertruleService()

    def test_create_validation(self) -> None:
        ok, missing = self.service.create_entry({
            "告警编号": "ALERT-X1",
            "监控指标": "失败条数",
            "比较运算符": "大于",
            "阈值数值": "50",
            "持续周期": "3",
            "静默时段": "10",
        })
        self.assertIsNotNone(ok)
        self.assertEqual(ok["阈值数值"], 50)
        self.assertEqual(ok["持续周期"], 3)
        self.assertEqual(ok["静默时段"], 10)

        bad, errors = self.service.create_entry({
            "告警编号": "ALERT-X2",
            "监控指标": "失败条数",
            "比较运算符": "介于",
            "阈值数值": "x",
            "持续周期": "0",
            "静默时段": "-1",
        })
        self.assertIsNone(bad)
        self.assertTrue(errors)

        dup, errors = self.service.create_entry({
            "告警编号": "ALERT-06",
            "监控指标": "失败条数",
            "比较运算符": "大于",
            "阈值数值": 50,
            "持续周期": 3,
            "静默时段": 5,
        })
        self.assertIsNone(dup)
        self.assertTrue(any("已存在" in e for e in errors))

    def test_rule_one_way_transitions(self) -> None:
        rule = [r for r in store.rows("alertrule") if r["告警编号"] == "ALERT-06"][0]
        entry, _ = self.service.run_action(int(rule["id"]), "关闭规则")
        self.assertEqual(entry["status"], "已关闭")
        again, err = self.service.run_action(int(rule["id"]), "生效规则")
        self.assertIsNone(again)
        self.assertIn("单向", err)


class AlertTicketApiTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_loop_tables()
        self.client = TestClient(app)

    def test_ingest_to_close_flow_over_http(self) -> None:
        """端到端 HTTP：三周期触发 -> 重复抑制 -> 恢复关单 -> 诊断联动可查。"""
        for idx in range(3):
            resp = self.client.post("/api/alertticket/ingest", json={"values": {
                "event_id": f"api-{idx}",
                "task_code": "TASK-1042",
                "metric": "失败条数",
                "value": 60 + idx,
                "exception_type": "HTTP 503",
                "affected_rows": 318,
                "stack_trace": "HTTP 503 Service Unavailable",
            }})
            self.assertEqual(resp.status_code, 200)
        listing = self.client.get("/api/alertticket", params={"task_code": "TASK-1042"})
        items = listing.json()["items"]
        self.assertEqual(len(items), 1)
        ticket_id = items[0]["id"]
        self.assertEqual(items[0]["status"], "待处理")
        self.assertEqual(items[0]["关联诊断报告"], "DIAG-004")

        # 重复上报不建新单
        dup = self.client.post("/api/alertticket/ingest", json={"values": {
            "event_id": "api-dup", "task_code": "TASK-1042", "metric": "失败条数",
            "value": 99, "exception_type": "HTTP 503", "affected_rows": 400,
        }})
        self.assertIn("不新建第二张", dup.json()["message"])

        # 同事件重放幂等
        replay = self.client.post("/api/alertticket/ingest", json={"values": {
            "event_id": "api-2", "task_code": "TASK-1042", "metric": "失败条数",
            "value": 62, "exception_type": "HTTP 503",
        }})
        self.assertTrue(replay.json()["ok"])

        # 工作台动作：认领 -> 关闭
        claim = self.client.post(f"/api/alertticket/{ticket_id}/actions",
                                 json={"values": {"action": "认领处置"}})
        self.assertEqual(claim.json()["entry"]["status"], "处理中")
        close = self.client.post(f"/api/alertticket/{ticket_id}/actions", json={
            "values": {"action": "关闭告警单", "conclusion": "链路恢复，告警闭环完成"}
        })
        self.assertEqual(close.json()["entry"]["status"], "已关闭")

        # 诊断报告按关联告警单反查，结论已同步
        diag = self.client.get("/api/diagnose", params={
            "linked_alert": items[0]["告警单号"]
        })
        diag_items = diag.json()["items"]
        self.assertEqual(len(diag_items), 1)
        self.assertEqual(diag_items[0]["处理结论"], "链路恢复，告警闭环完成")

        stats = self.client.get("/api/alertticket/stats").json()
        self.assertEqual(stats["closed"], 1)

    def test_invalid_ingest_rejected(self) -> None:
        resp = self.client.post("/api/alertticket/ingest", json={"values": {
            "task_code": "", "metric": "失败条数", "value": 1,
        }})
        body = resp.json()
        self.assertFalse(body["ok"])

    def test_seed_example_visible(self) -> None:
        """种子里的 ALERT-06 / DIAG-220 闭环示例可直接在接口看到（本用例不做清表）。"""
        store.reset()
        engine.reset_runtime()
        tickets = self.client.get("/api/alertticket").json()
        self.assertTrue(any(i["告警单号"] == "ALERT-06-20261113-001" for i in tickets["items"]))
        diag = self.client.get("/api/diagnose", params={"keyword": "DIAG-220"}).json()
        self.assertEqual(diag["items"][0]["影响行数"], 318)
        self.assertEqual(diag["items"][0]["关联任务"], "TASK-1042")


if __name__ == "__main__":
    unittest.main()
