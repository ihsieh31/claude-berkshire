"""執行已同意的強模型工作（M6a）：approvals 表中 approved 的檔 → 等待中的工作改回 pending → 引擎續跑該 run。

同一時間只允許一個執行（state/.run_approved.lock）；儀表板按下同意後由伺服器在背景呼叫 `trader run-approved`。
寫入鎖 state/.pipeline.lock 由 CLI 入口（cli.main）非阻塞取得（D10），這裡不再取，以免同一行程重複 flock 自鎖。
執行中途又有新同意的列：每輪跑完再 release 一次，有新放行的工作就接著跑（最多 MAX_ROUNDS 輪，M8-D2-03）。
上次執行結果寫 state/.run_approved.json（started_at／pid；結束時 finished_at／exit_code／各 run 狀態），儀表板顯示（M8-D5-04）。
"""
from __future__ import annotations

import fcntl
import json
import os
from typing import Any

from .. import db
from ..config import Config
from ..jobs import approvals
from ..jobs.engine import Engine
from ..llm.router import Router
from ..paths import Paths


STATUS_FILE = ".run_approved.json"
LOG_FILE = "state/logs/run_approved.log"
MAX_ROUNDS = 5


def write_status(paths: Paths, data: dict[str, Any]) -> None:
    f = paths.state / STATUS_FILE
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, f)


def read_status(paths: Paths) -> dict[str, Any] | None:
    try:
        return json.loads((paths.state / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
        st: dict[str, Any] = {"started_at": db.now_iso(), "pid": os.getpid(), "log": LOG_FILE}
        write_status(paths, st)
        try:
            out = _run(cfg, paths, conn, run_id, router, clients)
        except BaseException as e:
            write_status(paths, {**st, "finished_at": db.now_iso(), "exit_code": 1, "error": f"{type(e).__name__}: {e}"})
            raise
        runs = {k: {"status": v.get("status"), **{c: v.get(c, 0) for c in ("done", "failed", "skipped", "pending",
                                                                              "awaiting_approval")}}
                for k, v in out["runs"].items()}
        write_status(paths, {**st, "finished_at": db.now_iso(), "exit_code": exit_code(out), "runs": runs,
                             "new_failed": out.get("new_failed", {}), "stale_failed": out.get("stale_failed", {})})
        return out


def _failed_ids(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in conn.execute("SELECT run_id, job_id FROM jobs WHERE status='failed'"):
        out.setdefault(r[0], set()).add(r[1])
    return out


def exit_code(out: dict[str, Any]) -> int:
    """只有本次新出現的 failed 工作才算失敗；同 run 先前就失敗的（stale_failed）不算（M8-R5-04）。"""
    return 1 if any(out.get("new_failed", {}).values()) else 0


def _run(cfg: Config, paths: Paths, conn, run_id: str | None, router: Router | None,
         clients: dict[str, Any] | None) -> dict[str, Any]:
    before = _failed_ids(conn)   # release 前各 run 既有的 failed（M8-R5-04）
    runs = approvals.release(conn, run_id)
    if not runs:
        return {"runs": {}, "new_failed": {}, "stale_failed": {}}
    if clients is None:
        try:
            from ..data.inputs import make_clients
            clients = make_clients(cfg, paths)
        except Exception:  # noqa: BLE001 - 強模型工作本身不需要 SEC；缺了只影響之後可能的 DOSSIER
            clients = {}
    router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
    out: dict[str, Any] = {}
    for _ in range(MAX_ROUNDS):
        for rid in runs:
            engine = Engine(conn, router, paths, cfg)
            engine.ctx.sec = clients.get("sec")
            out[rid] = engine.run(rid)
        runs = approvals.release(conn, run_id, only_new=True)   # 執行中途新同意的（M8-D2-03）
        if not runs:
            break
    after = _failed_ids(conn)
    new = {rid: sorted(after.get(rid, set()) - before.get(rid, set())) for rid in out}
    stale = {rid: sorted(after.get(rid, set()) & before.get(rid, set())) for rid in out}
    return {"runs": out, "new_failed": {k: v for k, v in new.items() if v},
            "stale_failed": {k: v for k, v in stale.items() if v}}
