"""執行已同意的強模型工作（M6a）：approvals 表中 approved 的檔 → 等待中的工作改回 pending → 引擎續跑該 run。

同一時間只允許一個執行（state/.run_approved.lock）；儀表板按下同意後由伺服器在背景呼叫 `trader run-approved`。
"""
from __future__ import annotations

import fcntl
from typing import Any

from ..config import Config
from ..jobs import approvals
from ..jobs.engine import Engine
from ..llm.router import Router
from ..paths import Paths


class RunApprovedBusy(Exception):
    """已有另一個 run-approved 在執行。"""


def run_approved(cfg: Config, paths: Paths, conn, run_id: str | None = None, router: Router | None = None,
                 clients: dict[str, Any] | None = None) -> dict[str, Any]:
    lock_path = paths.state / ".run_approved.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunApprovedBusy("已有另一個 run-approved 在執行") from None
        from ..scheduler import pipeline_lock
        with pipeline_lock(paths):  # 等排程中的工作（每日收盤等）做完再寫帳本
            return _run(cfg, paths, conn, run_id, router, clients)


def _run(cfg: Config, paths: Paths, conn, run_id: str | None, router: Router | None,
         clients: dict[str, Any] | None) -> dict[str, Any]:
    runs = approvals.release(conn, run_id)
    if not runs:
        return {"runs": {}}
    if clients is None:
        try:
            from ..data.inputs import make_clients
            clients = make_clients(cfg, paths)
        except Exception:  # noqa: BLE001 - 強模型工作本身不需要 SEC；缺了只影響之後可能的 DOSSIER
            clients = {}
    router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
    out: dict[str, Any] = {}
    for rid in runs:
        engine = Engine(conn, router, paths, cfg)
        engine.ctx.sec = clients.get("sec")
        out[rid] = engine.run(rid)
    return {"runs": out}
