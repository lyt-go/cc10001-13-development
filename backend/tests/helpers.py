"""测试公共夹具：清掉种子里预置的闭环示例，保证用例从空告警单开始。"""
from __future__ import annotations

from app.services.alertloop import engine
from app.store import store


def reset_loop_tables() -> None:
    """重置内存仓库并移除种子中的示例告警单与关联诊断，再重建引擎运行态。"""
    store.reset()
    store._tables["alertticket"] = []
    diagnose_rows = store._tables["diagnose"]
    store._tables["diagnose"] = [
        row for row in diagnose_rows if row.get("报告编号") != "DIAG-220"
    ]
    engine.reset_runtime()
