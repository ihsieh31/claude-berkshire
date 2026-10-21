"""工作引擎（代理 D）：依 DAG 執行工作、驗證輸出、動態升級、預算與斷點續跑。

執行緒模型：主執行緒負責所有資料庫寫入；只有 call_with_retry（模型呼叫）在 ThreadPoolExecutor 中執行。
各 tier 同時執行數不超過 tier_config.concurrency。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import db
from ..config import Config
from ..llm.base import LLMResult, TransportError, call_with_retry, latest_raw, write_raw
from ..llm.router import Router
from ..models import AGENTIC_BACKENDS, PRICE_ISOLATED_JOBS, JobSpec, JobStatus, prompt_backend
from ..paths import Paths
from . import approvals, validate
from . import isolation
from .context import build_prompt
from .handlers import HANDLERS, Handler, HandlerCtx
from .sandbox import purge_stale_sandboxes, remove_sandbox, ticker_dir

MAX_ATTEMPTS = 2        # 首次 + 1 次驗證重試（routing defaults.on_invalid_output）
TRANSPORT_RETRIES = 3   # 額度類傳輸錯誤的額外重試次數（call_with_retry；逾時最多 1 次、其他不重試，D47）
MAX_FULL_B_PER_TICKER = 3   # 設計決策 D47：每檔每輪（run）最多執行 3 個 FULL_B 工作
INTERRUPT_WAIT_S = 10.0     # 中斷時等執行中的呼叫收尾（claude 子行程群組已被結束）的秒數
BUDGET_DAY_TZ = ZoneInfo("Asia/Taipei")   # 每日預算的日界（M5_D4-04）
BUDGET_DAY_KIND = "budget_daily_exhausted"
POST_INTERRUPTED = "post 中斷，需人工確認（回寫與後續工作可能已部分寫入，未自動重跑）"


JOB_FAILED_KIND = "job_failed"
FALLBACK = JobStatus.CONSERVATIVE_FALLBACK
FALLBACK_KIND = "job_conservative_fallback"   # D43：低嚴重度（不暫停交易），只供追蹤
# 使用者或同意時效決定不跑而 skipped 的工作（error 開頭）：jobs retry 與同 id 重新加入都不得推翻（M5_D2-02）
USER_CLOSED = ("使用者拒絕", "同意已作廢", "使用者放棄")
# D86（A3-M6B-04）：該檔已有比本工作資料日新的決策 → 工作改 skipped（error 以此開頭）；與使用者關閉同樣不算失敗、
# 不寫收尾保守結論、jobs retry 不重設，由下次觸發以新資料日重排
STALE_ASOF = "stale_asof"
_CLOSED = (*USER_CLOSED, STALE_ASOF)
# 不觸發 stale_asof 的決策種類：PRICE（只更新價格線；等待同意中的完整分析不因期間的 PRICE 作廢，A3-M6D-03 的窄化守衛處理）、
# 佔位與收尾（FALLBACK、TRIAGE 判否）、人工單與框架自發單
STALE_EXEMPT_REVIEW_TYPES = ("PRICE", "FALLBACK", "TRIAGE", "MANUAL", "PORTFOLIO")
STALE_EXEMPT_JOBS = ("CORP_ACTION",)
# daily 建立並標記的 run 種類：不受每日上限（M5-RS4-03）。corp_action＝daily 公司行動 run；
# daily_review＝daily 先建立的持股觸發 run（手動 review 建的 run 種類是 review，不豁免）
DAILY_REVIEW_KIND = "daily_review"
DAILY_BUDGET_EXEMPT_KINDS = ("corp_action", DAILY_REVIEW_KIND)
_RESET = "UPDATE jobs SET status='pending', error=NULL, result=NULL, finished_at=NULL WHERE job_id=?"


def _user_closed(error: Any) -> bool:
    """使用者拒絕／作廢／放棄，或 D86 stale_asof 跳過（都不是失敗、不重設、不寫收尾結論）。"""
    return isinstance(error, str) and error.startswith(_CLOSED)


def stale_asof(conn, ticker: str | None, asof: str | None) -> str | None:
    """D86（A3-M6B-04）：該檔已有 decision_date > asof 的非作廢、非佔位決策 → 理由；沒有回 None。
    佔位＝回寫函式以外寫的列（job_id 非 null：PRICE 不合格、run 收尾）、日誌 fallback、STALE_EXEMPT_REVIEW_TYPES；
    D80 非分析性停止（使用者拒絕強模型、輸出不合格）不是判斷，也不算。"""
    if not ticker or not asof:
        return None
    from ..portfolio.sizing import nonanalytic_stop
    for r in conn.execute(
            "SELECT decision_id, decision_date, reason_code, data FROM decisions WHERE ticker = ? AND job_id IS NULL "
            "AND json_extract(data, '$.voided_at') IS NULL AND substr(decision_date, 1, 10) > ? "
            "ORDER BY decision_date DESC, rowid DESC", (ticker, str(asof)[:10])).fetchall():
        log = json.loads(r["data"]) if r["data"] else {}
        if log.get("review_type") in STALE_EXEMPT_REVIEW_TYPES or log.get("fallback") \
                or nonanalytic_stop(log, r["reason_code"]):
            continue
        return (f"{STALE_ASOF}：{ticker} 已有比資料日 {str(asof)[:10]} 新的決策 {r['decision_id']}"
                f"（{r['decision_date']}，{log.get('review_type') or '完整分析'}），本工作不回寫，由下次觸發以新資料日重排（D86）")
    return None


def _closed_ids(conn, run_id: str) -> set[str]:
    """使用者拒絕／同意作廢／放棄而 skipped 的工作，及（間接）因它們而 skipped 的工作。"""
    rows = conn.execute("SELECT job_id, error, deps FROM jobs WHERE run_id=? AND status='skipped'", (run_id,)).fetchall()
    out = {r["job_id"] for r in rows if _user_closed(r["error"])}
    grew = True
    while grew:
        grew = False
        for r in rows:
            if r["job_id"] not in out and set(json.loads(r["deps"])) & out:
                out.add(r["job_id"])
                grew = True
    return out


def _replaced(result: Any) -> bool:
    """on_fail／on_invalid 已建替代工作的工作（result.replaced_by）：retry 與同 id 重新加入都不重設，
    否則重跑成功後 post 再升級、同 run 同檔出現第二條鏈（M5-RS1-02）。"""
    try:
        res = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return False
    return isinstance(res, dict) and bool(res.get("replaced_by"))


def _replaced_ids(conn, run_id: str) -> set[str]:
    """已有替代工作的 failed／skipped 工作，及（間接）因它們而 skipped 的工作。"""
    rows = conn.execute("SELECT job_id, status, deps, result FROM jobs WHERE run_id=? AND status IN ('failed','skipped')",
                        (run_id,)).fetchall()
    out = {r["job_id"] for r in rows if _replaced(r["result"])}
    grew = True
    while grew:
        grew = False
        for r in rows:
            if r["status"] == "skipped" and r["job_id"] not in out and set(json.loads(r["deps"])) & out:
                out.add(r["job_id"])
                grew = True
    return out


def retry_failed(conn, run_id: str, ticker: str | None = None, job_id: str | None = None) -> list[str]:
    """把 run 內 failed（與因依賴失敗而 skipped）的工作重設為 pending，供 Engine.run 續跑。
    清 error／result／finished_at；llm_calls 與 attempts 保留作歷史。不重設：CORP_ACTION（由 daily 重派事件）、
    conservative_fallback（已保守處理，D43）、使用者拒絕／同意作廢／放棄的工作及其下游（M5_D2-02）、
    失敗時已建替代工作者（result.replaced_by）及其下游（M5-RS1-02）。
    job_failed 異常不在這裡結案：重跑完成後由 Engine.run 結案（M5_D9-05）。回傳被重設的 job_id。"""
    sql = "SELECT job_id, job_type, ticker, status FROM jobs WHERE run_id=? AND status IN ('failed','skipped')"
    args: list[Any] = [run_id]
    if ticker:
        sql += " AND ticker=?"
        args.append(ticker)
    if job_id:
        sql += " AND job_id=?"
        args.append(job_id)
    closed = _closed_ids(conn, run_id) | _replaced_ids(conn, run_id)
    ids = []
    for r in conn.execute(sql, args).fetchall():
        if r["job_type"] == "CORP_ACTION" or r["job_id"] in closed:
            continue
        ids.append(r["job_id"])
        conn.execute(_RESET, (r["job_id"],))
    if job_id:  # 指定單一工作時，同檔因它而 skipped 的後續工作一併重設
        for r in conn.execute("SELECT job_id, deps FROM jobs WHERE run_id=? AND status='skipped'", (run_id,)).fetchall():
            if r["job_id"] not in closed and set(json.loads(r["deps"])) & set(ids):
                ids.append(r["job_id"])
                conn.execute(_RESET, (r["job_id"],))
    conn.commit()
    return ids


def abandon(conn, run_id: str, ticker: str | None = None, note: str | None = None) -> list[str]:
    """使用者放棄（M5_D4-01；D44 的「放棄」）：本 run（或該檔）pending／running／awaiting_approval 與 failed
    （CORP_ACTION 除外，由 daily 重派事件；M5-RS4-01）工作 → skipped
    （error＝「使用者放棄：note」，retry 不會重設）；該 run 的 pending／deferred／approved 同意列 → rejected
    （decided_via='abandon'）；對應的 job_failed 異常結案；整個 run 已無未完成工作時 runs.status='abandoned'。
    不寫任何決策（放棄＝本次沒有結論，持股維持現狀）。回傳被放棄的 job_id。"""
    why = "使用者放棄" + (f"：{note}" if note else "")
    cond, args = "run_id=?", [run_id]
    if ticker:
        cond += " AND ticker=?"
        args.append(ticker)
    ids = [r[0] for r in conn.execute(
        f"SELECT job_id FROM jobs WHERE {cond} AND (status IN ('pending','running','awaiting_approval') "
        f"OR (status='failed' AND job_type<>'CORP_ACTION'))", args)]
    now = db.now_iso()
    for j in ids:
        conn.execute("UPDATE jobs SET status='skipped', error=?, finished_at=? WHERE job_id=?", (why, now, j))
    jobs_here = {r[0] for r in conn.execute(f"SELECT job_id FROM jobs WHERE {cond}", args)}
    for a in conn.execute("SELECT anomaly_id, detail FROM anomalies WHERE kind=? AND status='open'",
                          (JOB_FAILED_KIND,)).fetchall():
        if (json.loads(a["detail"]) if a["detail"] else {}).get("job_id") in jobs_here:
            conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? WHERE anomaly_id=?",
                         (now, why, a["anomaly_id"]))
    conn.commit()
    approvals.abandon(conn, run_id, ticker, why)
    left = conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND status IN ('pending','running','awaiting_approval')",
                        (run_id,)).fetchone()
    if left is None:
        conn.execute("UPDATE runs SET status='abandoned', finished_at=? WHERE run_id=?", (now, run_id))
        conn.commit()
    return ids


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _deferred_retry(row) -> tuple[int, list[str], str | None] | None:
    """延後的驗證重試（_on_done 記在 result：attempt、retry_errors、retry_prev）→ (嘗試序號, 錯誤, 前次輸出)；沒有則 None。"""
    try:
        res = json.loads(row["result"]) if row["result"] else None
    except ValueError:
        return None
    if not isinstance(res, dict) or not res.get("retry_errors"):
        return None
    n = int(res.get("attempt") or 0)
    if not 0 < n < MAX_ATTEMPTS:
        return None
    return n, [str(e) for e in res["retry_errors"]], res.get("retry_prev")


_NO_TRAP = object()


def _trap_sigterm() -> Any:
    """主執行緒：SIGTERM 轉成 KeyboardInterrupt（排程逾時、timeout 指令），讓 run 能收尾。回傳原處理器。"""
    if threading.current_thread() is not threading.main_thread():
        return _NO_TRAP

    def _raise(signum, frame):
        raise KeyboardInterrupt(f"收到信號 {signum}")
    return signal.signal(signal.SIGTERM, _raise)


def _restore_sigterm(prev: Any) -> None:
    if prev is not _NO_TRAP:
        signal.signal(signal.SIGTERM, prev if prev is not None else signal.SIG_DFL)


class Engine:
    def __init__(self, conn, router: Router, paths: Paths, config: Config,
                 handlers: dict[str, Handler] | None = None, schemas: dict | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.conn = conn
        self.router = router
        self.paths = paths
        self.config = config
        self.handlers = HANDLERS if handlers is None else handlers
        # schemas 未注入 → 由 output_schemas.json 載入，並記 mtime：run 中同步 routing 後驗證跟著提示用新 schema（A3-M3E-01）
        self._schemas_path: Path | None = None if schemas is not None else Path(paths.schemas_json)
        self._schemas_mtime: int | None = None
        self.schemas = schemas if schemas is not None else self._load_schemas()
        self.sleep = sleep
        self.ctx = HandlerCtx(paths=paths, schemas=self.schemas, work_dir=paths.work, conn=conn,
                              sandbox_root=self._sandbox_root(config, paths, router), config=config)
        self._inflight: dict[Future, tuple[JobSpec, str]] = {}
        self._busy: Counter = Counter()
        self._base_prompt: dict[str, str] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._stop = threading.Event()      # 中斷時設定：不再重試、不再送出（M5_D9-03）
        self._resume: set[str] = set()      # 上次中斷時仍在執行、可沿用沙箱續跑的工作（M5_D9-02）
        if self._isolated_root():
            # priced 根內非執行中工作留下的看價格沙箱立即清除（不等 24 小時）；執行中的屬其他行程，保留
            running = {r[0] for r in conn.execute(
                "SELECT sandbox FROM jobs WHERE status='running' AND sandbox IS NOT NULL").fetchall()}
            purge_stale_sandboxes(self.ctx.sandbox_root, keep=running)
            root = Path(self.ctx.sandbox_root)
            if isolation.priced_root(root).is_dir():  # 舊版留在隔離根的看價格沙箱也立即清除（非執行中者）
                keep = {str(Path(r).resolve()) for r in running}
                for p in root.glob("*/*/_*"):
                    if p.name[1:].rsplit("_", 1)[0] not in PRICE_ISOLATED_JOBS and str(p.resolve()) not in keep:
                        remove_sandbox(p, root)

    def _load_schemas(self) -> dict:
        p = self._schemas_path
        mtime = p.stat().st_mtime_ns
        out = validate.load_schemas(p)
        self._schemas_mtime = mtime
        return out

    def _current_schemas(self) -> dict:
        """output_schemas.json 改過（mtime 變）就重讀，與 build_prompt 每工作重讀的提示同一版本（A3-M3E-01）。
        重讀失敗（例如同步寫到一半）沿用舊快取，下次再試。注入的 schemas（測試）不重讀。"""
        p = self._schemas_path
        if p is None:
            return self.schemas
        try:
            if p.stat().st_mtime_ns != self._schemas_mtime:
                self.schemas = self.ctx.schemas = self._load_schemas()
        except (OSError, ValueError):
            pass
        return self.schemas

    def _isolated_root(self) -> bool:
        root = self.ctx.sandbox_root
        return root is not None and Path(root).resolve() != Path(self.paths.work).resolve()

    def _drop_sandbox(self, spec: JobSpec) -> None:
        """工作終態後刪沙箱（輸出已收回 work）：看價格的沙箱留著會被隔離工作讀到價格；隔離工作的沙箱也刪，
        不再每檔每 run 累積（A3-M5E-03；續跑沿用的是 running 工作的沙箱，終態工作重跑時 prepare 會重建）。"""
        if not spec.sandbox or not self._isolated_root():
            return
        sb = Path(spec.sandbox)
        try:   # 刪前留清單（相對路徑＋sha256）在 llm_calls 目錄，事後可追溯模型看過哪些檔（M5-D5-08）
            files = {p.relative_to(sb).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(sb.rglob("*")) if p.is_file()}
            write_raw(spec.log_dir, spec, "manifest.json",
                      json.dumps({"sandbox": str(sb), "files": files}, ensure_ascii=False, indent=1))
        except OSError:
            pass
        remove_sandbox(sb, self.ctx.sandbox_root)

    @staticmethod
    def _sandbox_root(config, paths, router) -> Path:
        """實際會呼叫 claude_code 時，沙箱放在隔離根目錄（家目錄與暫存目錄之外）；否則（含 dry-run）放 state/work。"""
        models = getattr(config, "models", None) or {}
        if getattr(router, "dry_run", False):
            return paths.work
        if not any(getattr(t, "backend", "") in AGENTIC_BACKENDS for t in models.values()):
            return paths.work
        sb = config.sandbox
        root = Path(sb.root).expanduser() if sb.root else isolation.default_sandbox_root()
        return isolation.prepare_root(root, isolation.deny_roots(sb.extra_deny, paths.state))

    # ------------------------------------------------------------ 公開介面

    def start_run(self, run_id: str, kind: str, asof: str) -> None:
        """建立 run（已存在則不覆蓋 started_at 等既有欄位）。"""
        if self.conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
            db.insert(self.conn, "runs", {
                "run_id": run_id, "kind": kind, "asof": asof, "status": "pending",
            })

    def add_job(self, spec: JobSpec) -> bool:
        """加入工作。同 job_id 已存在時（M5_D9-01）：既有列 failed／skipped（使用者拒絕、作廢、放棄者除外），
        或 pending 且尚未嘗試、而參數或依賴不同 → 以新 params、deps 覆寫並改回 pending，回傳 True；
        其他情形（done、conservative_fallback、running、awaiting_approval、參數相同、已有替代工作者）不動，回傳 False。"""
        # 先轉成可 JSON 化的值（路徑等以字串存）
        params = json.loads(json.dumps(spec.params, ensure_ascii=False, default=str))
        row = self.conn.execute("SELECT status, attempts, error, params, deps, result FROM jobs WHERE job_id=?",
                                (spec.job_id,)).fetchone()
        if row is not None:
            same = json.loads(row["params"]) == params and json.loads(row["deps"]) == list(spec.deps)
            redo = (row["status"] in ("failed", "skipped") and not _user_closed(row["error"])
                    and not _replaced(row["result"])) or \
                (row["status"] == "pending" and not row["attempts"] and not same)
            if not redo:
                return False
            self.conn.execute(
                "UPDATE jobs SET status='pending', deps=?, params=?, error=NULL, result=NULL, finished_at=NULL "
                "WHERE job_id=?", (json.dumps(list(spec.deps)), json.dumps(params, ensure_ascii=False), spec.job_id))
            self.conn.commit()
            return True
        db.insert(self.conn, "jobs", {
            "job_id": spec.job_id,
            "run_id": spec.run_id,
            "job_type": spec.job_type,
            "ticker": spec.ticker,
            "asof": spec.asof,
            "status": "pending",
            "deps": list(spec.deps),
            "params": params,
            "attempts": 0,
            "created_at": db.now_iso(),
        })
        return True

    def run(self, run_id: str) -> dict[str, Any]:
        """執行一個 run 直到全部終態、或預算耗盡（paused_budget）。可重複呼叫以斷點續跑。
        執行期間 SIGTERM 視同 Ctrl-C（KeyboardInterrupt）；中斷時不再重試、結束執行中的 claude 子行程群組，
        已完成的呼叫照常記帳與處理，被中止的工作改回 pending（M5_D9-03、M5_D4-02），再拋出原例外。"""
        self._inflight.clear()
        self._busy.clear()
        self._base_prompt.clear()
        self._stop = threading.Event()
        was = self.conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        abandoned = was is not None and was["status"] == "abandoned"
        self._begin(run_id)
        workers = max(1, sum(t.concurrency for t in self.config.models.values()))
        pool = ThreadPoolExecutor(max_workers=workers)
        self._pool = pool
        prev = _trap_sigterm()
        try:
            while True:
                changed, ready = self._scan(run_id)
                blocked = self._budget_exhausted(run_id)
                if not blocked:
                    for row in ready:
                        if self._budget_exhausted(run_id):
                            break
                        if self._launch(row):
                            changed = True
                if not self._inflight:
                    if changed:
                        continue
                    break
                done, _ = wait(list(self._inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    self._on_done(fut)
        except BaseException:
            self._interrupted(pool)
            raise
        finally:
            _restore_sigterm(prev)
            self._pool = None
        pool.shutdown(wait=True)

        blocked = self._budget_exhausted(run_id)
        counts = self._counts(run_id)
        if counts["pending"] and not blocked:
            # 沒有執行中、也不是預算因素：除了（間接）依賴等待同意工作者，剩下的待辦必為循環依賴
            waiting = self._waiting_closure(run_id)
            for row in self._pending_rows(run_id):
                if row["job_id"] in waiting:
                    continue
                self._update(row["job_id"], status="skipped", error="依賴無法滿足（循環？）",
                             finished_at=db.now_iso(), result={"decision_mark": self._decision_mark()})
        self.finalize_run(run_id)
        counts = self._counts(run_id)
        if blocked and counts["pending"]:
            status = "paused_budget"
            self._record_daily_budget(run_id)
        elif counts["awaiting_approval"]:
            status = "awaiting_approval"
        elif counts["failed"] or counts["skipped"] > len(_closed_ids(self.conn, run_id)):
            # 有工作失敗或被跳過：不得標 done（done＝全部工作都收到通過驗證的結果）；
            # 使用者拒絕／同意作廢／放棄造成的 skipped 不算失敗（M5-RS2-02）
            status = "failed"
        else:
            status = "done"
        if abandoned and status in ("done", "failed"):
            status = "abandoned"   # 使用者已放棄的 run 不因再跑一次而改狀態（M5-RS4-01）
        self._record_failures(run_id)
        self._resolve_recovered(run_id)
        self.conn.execute(
            "UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
            (status, db.now_iso(), json.dumps(counts, ensure_ascii=False), run_id),
        )
        self.conn.commit()
        approvals.close_finished(self.conn, run_id)
        return {**counts, "status": status}

    def _interrupted(self, pool: ThreadPoolExecutor) -> None:
        """中斷收尾：不再重試、結束子行程、取消未開始者；已完成的照常處理；仍在執行者（例如可續跑後端仍在等候）
        保持 running，下次續跑時處理。"""
        self._stop.set()
        cancel = getattr(self.router, "cancel_all", None)
        if callable(cancel):
            cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        # 可續跑後端（例如等外部代理作答）不等：保持 running，下次續跑接上
        ended = [f for f, (_, tier) in self._inflight.items()
                 if not getattr(self.router.runner_for(tier), "resumable", False)]
        if ended:
            wait(ended, timeout=INTERRUPT_WAIT_S)
        for fut in list(self._inflight):
            if fut.cancelled():
                spec, tier = self._inflight.pop(fut)
                self._busy[tier] -= 1
                self._update(spec.job_id, status="pending", error="中止：行程中斷（尚未送出）")
            elif fut.done():
                spec, tier = self._inflight[fut]
                if fut.exception() is not None and getattr(self.router.runner_for(tier), "resumable", False):
                    # 可續跑後端被 cancel() 結束等候：不算一次呼叫，保持 running，下次續跑接上佇列
                    self._inflight.pop(fut)
                    self._busy[tier] -= 1
                    continue
                self._on_done(fut)

    def _record_failures(self, run_id: str) -> None:
        """每個 failed 工作寫一筆不暫停交易的異常 job_failed（已有同 job 未結異常不重複）。
        CORP_ACTION 由 daily 另寫暫停該檔的異常（corp_actions.record_job_failure），這裡不寫。"""
        rows = self.conn.execute(
            "SELECT job_id, job_type, ticker, error FROM jobs WHERE run_id=? AND status='failed' AND job_type<>'CORP_ACTION'",
            (run_id,)).fetchall()
        if not rows:
            return
        open_ids = set()
        for a in self.conn.execute("SELECT detail FROM anomalies WHERE kind=? AND status='open'", (JOB_FAILED_KIND,)):
            open_ids.add((json.loads(a["detail"]) if a["detail"] else {}).get("job_id"))
        for r in rows:
            if r["job_id"] in open_ids:
                continue
            db.insert(self.conn, "anomalies", {
                "ticker": r["ticker"], "kind": JOB_FAILED_KIND, "status": "open", "pause_trading": 0,
                "detail": {"run_id": run_id, "job_id": r["job_id"], "job_type": r["job_type"], "error": r["error"]},
                "created_at": db.now_iso()})

    def _resolve_recovered(self, run_id: str) -> None:
        """本 run 已完成（done／conservative_fallback）的工作：其未結 job_failed 異常結案（M5_D9-05：
        jobs retry 只重設狀態，確認重跑完成才結案）。failed 但已由替代工作完成收尾者（approvals.replaced_ok）同樣結案（A3-M5B-02）。"""
        ok = {r[0] for r in self.conn.execute(
            "SELECT job_id FROM jobs WHERE run_id=? AND status IN ('done', ?)", (run_id, FALLBACK))}
        replaced = {r[0] for r in self.conn.execute("SELECT job_id FROM jobs WHERE run_id=? AND status='failed'", (run_id,))
                    if approvals.replaced_ok(self.conn, r[0], ok=("done", FALLBACK))}
        for a in self.conn.execute("SELECT anomaly_id, detail FROM anomalies WHERE kind=? AND status='open'",
                                   (JOB_FAILED_KIND,)).fetchall():
            jid = (json.loads(a["detail"]) if a["detail"] else {}).get("job_id")
            if jid in ok or jid in replaced:
                self.conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? WHERE anomaly_id=?",
                                  (db.now_iso(), "工作重跑後已完成" if jid in ok else "失敗已由替代工作完成收尾",
                                   a["anomaly_id"]))

    def finalize_run(self, run_id: str) -> list[str]:
        """設計決策 D46：run 結束時逐檔收尾。該檔本 run 工作都已終態、有 failed 或 skipped（使用者拒絕／作廢／放棄除外；
        CORP_ACTION 由 daily 處理，不算），而最後一次失敗之後沒有任何決策 → 寫一筆保守結論
        （持股 HOLD＋停止加碼、其他 WATCH；writeback.write_fallback_result；日誌加 "fallback": true）。回傳寫入的決策編號。"""
        from ..analysis.writeback import write_fallback_result
        from .hold_jobs import is_holding
        rows = self.conn.execute("SELECT * FROM jobs WHERE run_id=? AND job_type<>'CORP_ACTION' ORDER BY rowid",
                                 (run_id,)).fetchall()
        by_ticker: dict[str, list] = {}
        for r in rows:
            by_ticker.setdefault(r["ticker"], []).append(r)
        out = []
        for t, rs in by_ticker.items():
            if any(r["status"] in ("pending", "running", "awaiting_approval") for r in rs):
                continue   # 還沒結束（等待同意、預算暫停）：下次續跑結束時再收尾
            bad = [r for r in rs if r["status"] == "failed"
                   or (r["status"] == "skipped" and not _user_closed(r["error"]))]
            if not bad:
                continue
            mark = max(int((json.loads(r["result"]) if r["result"] else {}).get("decision_mark") or 0) for r in bad)
            if self.conn.execute("SELECT 1 FROM decisions WHERE run_id=? AND ticker=? AND rowid>?",
                                 (run_id, t, mark)).fetchone() is not None:
                continue   # 失敗之後已有結論（on_fail、替代工作或後續步驟寫的）
            asof = rs[-1]["asof"]
            holding = any(json.loads(r["params"] or "{}").get("holding") for r in rs) or is_holding(self.conn, t)
            why = "；".join(f"{r['job_type']} {r['status']}：{(r['error'] or '')[:200]}" for r in bad)
            try:
                # 收尾決策的 "fallback": true 由 write_fallback_result 同交易寫入（A3-M6B-05）：
                # sizing.add_blocked 只在本 run 該檔失敗未結時擋加碼（失敗修好即解除）
                did = write_fallback_result(self.conn, self.paths, t, asof, run_id, bad[-1]["job_id"], holding, why)
                out.append(did)
            except Exception as e:  # noqa: BLE001 - 收尾失敗不得讓 run 中斷；失敗紀錄（job_failed）仍在
                self.conn.rollback()
                self._update(bad[-1]["job_id"], error=f"{bad[-1]['error']}；收尾保守結論寫入失敗：{_err(e)}")
        return out

    def _decision_mark(self) -> int:
        """目前 decisions 的最大 rowid：記在失敗工作的 result，收尾時據此判斷失敗之後有沒有結論。"""
        return int(self.conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM decisions").fetchone()[0])

    def _on_fail(self, spec: JobSpec, error: str) -> None:
        """非格式失敗（路由、準備、傳輸、後置例外、依賴跳過）：交處理器 on_fail 寫保守紀錄（M5_D4-06）。
        回傳的替代工作不依賴本工作（M5-D1-01）。on_fail 本身出錯只記在本工作的 error，不中斷 run。"""
        handler = self.handlers.get(spec.job_type)
        if handler is None:
            return
        try:
            ids = []
            for nj in handler.on_fail(spec, error, self.ctx) or []:
                nj.deps = [d for d in nj.deps if d != spec.job_id]
                self.add_job(nj)
                ids.append(nj.job_id)
            if ids:   # 記下替代工作：retry／add_job 不再重設本工作（M5-RS1-02）
                self._mark_replaced(spec.job_id, ids)
        except Exception as e:  # noqa: BLE001
            if self.conn.in_transaction:
                self.conn.rollback()
            self._update(spec.job_id, error=f"{error}；on_fail 失敗：{_err(e)}")

    def _mark_replaced(self, job_id: str, ids: list[str]) -> None:
        row = self.conn.execute("SELECT result FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        res = json.loads(row["result"]) if row is not None and row["result"] else {}
        res = res if isinstance(res, dict) else {}
        self._update(job_id, result={**res, "replaced_by": list(ids)})

    # ------------------------------------------------------------ 排程

    def _begin(self, run_id: str) -> None:
        if self.conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
            raise ValueError(f"run {run_id} 不存在，請先 start_run")
        # 上次中斷殘留的 running：post 中途被中斷者（回寫可能已部分完成）改 failed、不重跑 post（M5-D1-02），
        # decision_mark 用進入 post 前的值，收尾時看得到 post 已寫的決策；其餘改回 pending，有沙箱者記下供可續跑後端沿用（M5_D9-02）
        self._resume = set()
        for r in self.conn.execute("SELECT job_id, sandbox, result FROM jobs WHERE run_id=? AND status='running'",
                                   (run_id,)).fetchall():
            res = json.loads(r["result"]) if r["result"] else None
            if isinstance(res, dict) and res.get("posting"):
                self.conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=?, result=? WHERE job_id=?",
                                  (POST_INTERRUPTED, db.now_iso(),
                                   json.dumps({"decision_mark": int(res.get("decision_mark") or 0),
                                               **({"replaced_by": res["replaced_by"]} if res.get("replaced_by") else {})}),
                                   r["job_id"]))
            elif r["sandbox"]:
                self._resume.add(r["job_id"])
        self.conn.execute("UPDATE jobs SET status='pending' WHERE run_id=? AND status='running'", (run_id,))
        self.conn.execute(
            "UPDATE runs SET status=CASE WHEN status='abandoned' THEN status ELSE 'running' END, "
            "started_at=COALESCE(started_at, ?) WHERE run_id=?",
            (db.now_iso(), run_id),
        )
        self.conn.commit()

    def _pending_rows(self, run_id: str) -> list:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE run_id=? AND status='pending' ORDER BY rowid", (run_id,)
        ).fetchall()

    def _waiting_closure(self, run_id: str) -> set[str]:
        """等待同意的工作，及（間接）依賴它們的待辦工作。"""
        rows = self.conn.execute("SELECT job_id, status, deps FROM jobs WHERE run_id=?", (run_id,)).fetchall()
        out = {r["job_id"] for r in rows if r["status"] == "awaiting_approval"}
        grew = True
        while grew:
            grew = False
            for r in rows:
                if r["status"] == "pending" and r["job_id"] not in out and set(json.loads(r["deps"])) & out:
                    out.add(r["job_id"])
                    grew = True
        return out

    def _dep_status(self, dep_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM jobs WHERE job_id=?", (dep_id,)).fetchone()
        return None if row is None else row["status"]

    def _dep_error(self, dep_id: str) -> str | None:
        row = self.conn.execute("SELECT error FROM jobs WHERE job_id=?", (dep_id,)).fetchone()
        return None if row is None else row["error"]

    def _skip_if_stale(self, spec: JobSpec) -> bool:
        """D86（A3-M6B-04）：工作啟動與 post／on_invalid 回寫前檢查；該檔已有較新的決策 → 改 skipped（stale_asof），
        不呼叫模型、不回寫、不寫收尾保守結論。回傳是否已跳過。"""
        if spec.job_type in STALE_EXEMPT_JOBS:
            return False
        why = stale_asof(self.conn, spec.ticker, spec.asof)
        if why is None:
            return False
        self._update(spec.job_id, status="skipped", error=why, finished_at=db.now_iso(),
                     result={"decision_mark": self._decision_mark()})
        self._drop_sandbox(spec)
        return True

    def _scan(self, run_id: str) -> tuple[bool, list]:
        """依賴失敗者標為 skipped；回傳（是否有狀態變動、可執行的 pending 列）。"""
        changed = False
        ready = []
        for row in self._pending_rows(run_id):
            deps = json.loads(row["deps"])
            reason = None
            all_done = True
            for d in deps:
                st = self._dep_status(d)
                if st is None:
                    reason = f"依賴不存在：{d}"
                    break
                if st in ("failed", "skipped", FALLBACK):
                    reason = f"依賴 {d} 狀態為 {st}"
                    if st == "skipped" and str(self._dep_error(d) or "").startswith(STALE_ASOF):
                        reason = f"{STALE_ASOF}：依賴 {d} 因資料日較舊而跳過（D86）"
                    break
                if st != "done":
                    all_done = False
            if reason is not None:
                self._update(row["job_id"], status="skipped", error=reason, finished_at=db.now_iso(),
                             result={"decision_mark": self._decision_mark()})
                if not reason.startswith(STALE_ASOF):   # D86：資料日較舊的跳過不寫保守紀錄
                    self._on_fail(self._spec(row), reason)
                changed = True
            elif all_done:
                ready.append(row)
        return changed, ready

    def _budget_exhausted(self, run_id: str) -> bool:
        """每 run 上限（送出中的呼叫也算一次，M5-D1-07）或每日上限（台北日界，所有 run 合計，M5_D4-04）已到。"""
        n, toks, cost = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id=?",
            (run_id,)).fetchone()
        n += len(self._inflight)
        b = self.config.budget
        return ((b.max_llm_calls_per_run > 0 and n >= b.max_llm_calls_per_run)   # 0＝不限（A3-M8C-02）
                or (b.max_cost_usd_per_run > 0 and cost >= b.max_cost_usd_per_run)
                or (b.max_input_tokens_per_run > 0 and toks >= b.max_input_tokens_per_run)
                or self._daily_exhausted(run_id))

    def _daily_exhausted(self, run_id: str | None = None) -> bool:
        b = self.config.budget
        if b.max_llm_calls_per_day <= 0 and b.max_cost_usd_per_day <= 0:
            return False
        if run_id is not None:   # daily 建立並標記的公司行動／持股觸發 run 不受每日上限（M5-RS4-03）
            k = self.conn.execute("SELECT kind FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if k is not None and k["kind"] in DAILY_BUDGET_EXEMPT_KINDS:
                return False
        start = datetime.now(BUDGET_DAY_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        n, cost = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE created_at>=?",
            (start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),)).fetchone()
        n += len(self._inflight)
        return ((b.max_llm_calls_per_day > 0 and n >= b.max_llm_calls_per_day)
                or (b.max_cost_usd_per_day > 0 and cost >= b.max_cost_usd_per_day))

    def _record_daily_budget(self, run_id: str) -> None:
        """每日上限造成的 paused_budget：記一筆不暫停交易的異常（同一 run 未結者不重複）。"""
        if not self._daily_exhausted(run_id):
            return
        for a in self.conn.execute("SELECT detail FROM anomalies WHERE kind=? AND status='open'", (BUDGET_DAY_KIND,)):
            if (json.loads(a["detail"]) if a["detail"] else {}).get("run_id") == run_id:
                return
        b = self.config.budget
        db.insert(self.conn, "anomalies", {
            "kind": BUDGET_DAY_KIND, "status": "open", "pause_trading": 0,
            "detail": {"run_id": run_id, "max_llm_calls_per_day": b.max_llm_calls_per_day,
                       "max_cost_usd_per_day": b.max_cost_usd_per_day, "day_tz": str(BUDGET_DAY_TZ)},
            "created_at": db.now_iso()})

    def _spec(self, row) -> JobSpec:
        return JobSpec(
            job_id=row["job_id"],
            run_id=row["run_id"],
            job_type=row["job_type"],
            ticker=row["ticker"],
            asof=row["asof"],
            deps=json.loads(row["deps"]),
            params=json.loads(row["params"]),
        )

    def _handler(self, job_type: str) -> Handler:
        try:
            return self.handlers[job_type]
        except KeyError:
            raise ValueError(f"沒有 {job_type} 的處理器") from None

    def _launch(self, row) -> bool:
        """準備並送出一個工作。回傳 True 表示狀態已變動（已送出或已失敗）；False 表示因 tier 併發上限而延後。"""
        spec = self._spec(row)
        try:
            tier = self.router.tier_for(spec)
        except Exception as e:  # noqa: BLE001 - 任何路由錯誤都只影響這個工作
            self._fail(spec, _err(e))
            return True
        try:
            if approvals.required(self.config, tier) and not approvals.gate(self.conn, spec, tier):
                if self._dep_status(spec.job_id) != "skipped":   # gate 依已拒絕／作廢的同意列改 skipped 者不再等待
                    self._update(spec.job_id, status="awaiting_approval", tier=tier)  # 不呼叫模型，等使用者同意
                return True
        except Exception as e:  # noqa: BLE001
            self._fail(spec, _err(e))
            return True
        if self._busy[tier] >= self.router.tier_config(tier).concurrency:
            return False
        if self._skip_if_stale(spec):   # D86：工作啟動前（同意之後、呼叫模型之前）
            return True
        if spec.job_type == "FULL_B" and self._full_b_count(spec) >= MAX_FULL_B_PER_TICKER:
            self._fail(spec, f"本輪 {spec.ticker} 已執行 {MAX_FULL_B_PER_TICKER} 個 FULL_B，不再執行（設計決策 D47）")
            return True
        resumed = False
        try:
            handler = self._handler(spec.job_type)
            spec.tier = tier
            spec.attempt = 0
            spec.log_dir = self.paths.llm_calls / _safe(spec.run_id)
            tcfg = self.router.tier_config(tier)
            prompt = self._resume_prompt(spec, row, tier)
            if prompt is not None:   # 可續跑後端：沿用原沙箱與原提示，不重跑前置腳本、不重建沙箱（M5_D9-02）
                spec.sandbox = Path(row["sandbox"])
                spec.prompt_text = base = prompt
                resumed = True
            else:
                handler.pre_script(spec, self.ctx)
                sections = handler.prepare(spec, self.ctx)  # 建沙箱、stage 輸入（價格隔離在這裡檢查）
                pb = prompt_backend(tcfg.backend)  # hark_queue 與 claude_code 同為「自己讀沙箱檔」的代理式後端
                spec.prompt_text = base = build_prompt(
                    spec, pb, self.router.max_context_tokens(spec.job_type, tier),
                    self.paths.root, sections, values=handler.prompt_values(spec, self.ctx, pb),
                ) + handler.prompt_suffix(spec)
                deferred = _deferred_retry(row)
                if deferred is not None:   # 驗證重試曾因中斷／預算延後：接著那次嘗試、帶錯誤回饋送出（A3-M5C-06）
                    spec.attempt, errors, prev = deferred
                    spec.prompt_text = base + "\n\n" + validate.error_feedback(errors, prev)
        except Exception as e:  # noqa: BLE001 - 準備失敗只讓這個工作失敗
            self._fail(spec, _err(e))
            return True
        if spec.job_id not in self._base_prompt or not resumed:
            self._base_prompt[spec.job_id] = base
        if resumed:
            self._update(spec.job_id, status="running", error=None)
        else:
            spec.params = {**spec.params, **self._rules_stamp()}
            self._update(spec.job_id, status="running", tier=tier, model=tcfg.model, params=spec.params,
                         sandbox=str(spec.sandbox) if spec.sandbox else None, started_at=db.now_iso())
        self._submit(spec, tier)
        return True

    def _rules_stamp(self) -> dict[str, Any]:
        """送出時的規則版本（skill 兩目錄＋routing 逐檔雜湊檔，與決策日誌 skill_hashes 同格式）與設定的模型訓練截止日；
        記進 jobs.params，writeback 比對同鏈是否換過規則、補跑日期是否早於截止日（M5_D4-03、D49、D25）。"""
        from ..analysis.writeback import skill_hashes_file
        try:
            rules = skill_hashes_file(self.paths)
        except OSError:
            rules = None
        return {"rules_hash": rules, "model_cutoff": getattr(self.config, "model_cutoff", "") or None}

    def _full_b_count(self, spec: JobSpec) -> int:
        """本 run 本檔其他已執行過（有呼叫紀錄）的 FULL_B 工作數。"""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE run_id=? AND ticker=? AND job_type='FULL_B' AND job_id<>? AND attempts>0",
            (spec.run_id, spec.ticker, spec.job_id)).fetchone()[0])

    def _resume_prompt(self, spec: JobSpec, row, tier: str) -> str | None:
        """上次中斷時仍在執行、Runner 可續跑（resumable）、沙箱還在、且本次執行寫過原始提示 → 回傳該提示；否則 None。
        沿用中斷時的嘗試序號（驗證重試送出時記在 result.attempt）與該次提示，不從 attempt 0 重播（M5-RS8-01）；
        attempt>0 時另把 attempt 0 的提示記為基底（再驗證重試組回饋用）。"""
        if spec.job_id not in self._resume:
            return None
        self._resume.discard(spec.job_id)
        if not getattr(self.router.runner_for(tier), "resumable", False):
            return None
        if not row["sandbox"] or not Path(row["sandbox"]).is_dir():
            return None
        try:
            res = json.loads(row["result"]) if row["result"] else {}
        except ValueError:
            res = {}
        n = int(res.get("attempt") or 0) if isinstance(res, dict) else 0
        p = latest_raw(spec.log_dir, spec, "prompt.txt", attempt=n)
        if p is None:
            return None
        started = row["started_at"]
        if started:   # 只認本次執行（started_at 之後）寫的提示，避免沿用 jobs retry 之前的舊提示
            t0 = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if p.stat().st_mtime < t0 - 1:
                return None
        if n:
            p0 = latest_raw(spec.log_dir, spec, "prompt.txt", attempt=0)
            self._base_prompt[spec.job_id] = p0.read_text(encoding="utf-8") if p0 is not None else p.read_text(encoding="utf-8")
        spec.attempt = n
        return p.read_text(encoding="utf-8")

    def _submit(self, spec: JobSpec, tier: str) -> None:
        runner = self.router.runner_for(tier)
        failed: list[TransportError] = []   # 已重試過去的傳輸錯誤：完成後逐次記入 llm_calls（M5-D1-07）
        before = None if getattr(runner, "resumable", False) else (lambda: self._before_transport_retry(spec))
        fut = self._pool.submit(call_with_retry, runner, spec, retries=TRANSPORT_RETRIES, sleep=self.sleep,
                                failed=failed, stop=self._stop, before_retry=before)
        fut.failed_calls = failed
        self._inflight[fut] = (spec, tier)
        self._busy[tier] += 1

    def _before_transport_retry(self, spec: JobSpec) -> None:
        """傳輸重試（同一沙箱）前，在工作執行緒執行：DOSSIER 會在沙箱 dossier/{T}.md 追加，逾時被殺後重試
        不得在半截檔上再追加 → 還原為輸出區版本（M5-D1-09）。只做檔案複製（prepare 可能讀資料庫，不能在工作執行緒跑）。"""
        if spec.job_type != "DOSSIER" or not spec.sandbox:
            return
        from .deep import dossier_base   # 前置版底稿，不是上次 post 覆寫的模型版（A3-M5E-01）
        src = dossier_base(ticker_dir(self.ctx.work_dir, spec), spec.ticker)
        dst = Path(spec.sandbox) / "dossier" / f"{spec.ticker}.md"
        if src.is_file() and dst.parent.is_dir():
            shutil.copyfile(src, dst)

    def _on_done(self, fut: Future) -> None:
        spec, tier = self._inflight.pop(fut)
        self._busy[tier] -= 1
        retried = list(getattr(fut, "failed_calls", []))
        for e in retried:
            self._record_call(spec, tier, None, ok=False, error=_err(e), exc=e)
        exc = fut.exception()
        if exc is not None:
            self._record_call(spec, tier, None, ok=False, error=_err(exc), exc=exc)
            self._count_attempt(spec, None, [*retried, exc])
            if self._stop.is_set():   # 中斷造成（子行程被結束）：不算失敗，下次續跑重做
                self._update(spec.job_id, status="pending", error=f"中止：行程中斷（{_err(exc)[:200]}）")
                return
            self._fail(spec, _err(exc))
            return
        result: LLMResult = fut.result()
        self._record_call(spec, tier, result, ok=True, error=None)
        self._count_attempt(spec, result, retried)

        try:
            normalized, errors = self._check(spec, result)
        except Exception as e:  # noqa: BLE001
            self._fail(spec, _err(e))
            return
        if errors:
            self.conn.execute(  # 把驗證錯誤記到本次呼叫，事後可查重試原因
                "UPDATE llm_calls SET error=? WHERE rowid=(SELECT max(rowid) FROM llm_calls WHERE job_id=? AND attempt=?)",
                ("invalid_output: " + "；".join(errors)[:2000], spec.job_id, spec.attempt))
            self.conn.commit()
            prev = (result.text or "")[:validate.PREVIOUS_OUTPUT_CHARS + 1]   # 前次輸出附進回饋（A3-M5A-02）
            if spec.attempt + 1 < MAX_ATTEMPTS:
                if self._stop.is_set() or self._budget_exhausted(spec.run_id):   # 驗證重試也受預算限制（M5-D1-07）
                    # 記下一次嘗試序號與錯誤回饋：續跑時接著重試，不從 attempt 0 重送原提示（可續跑後端會重播舊答案，A3-M5C-06）
                    self._update(spec.job_id, status="pending",
                                 error="驗證重試延後：" + ("行程中斷" if self._stop.is_set() else "預算已用完"),
                                 result={"attempt": spec.attempt + 1, "retry_errors": list(errors), "retry_prev": prev})
                    return
                spec.attempt += 1
                try:   # 重建沙箱（輸入皆來自同檔輸出區，可重放；不重跑 pre_script）：前次嘗試寫進沙箱的檔不得殘留（M5-D1-09）
                    self._handler(spec.job_type).prepare(spec, self.ctx)
                except Exception as e:  # noqa: BLE001
                    self._fail(spec, _err(e))
                    return
                spec.prompt_text = self._base_prompt[spec.job_id] + "\n\n" + validate.error_feedback(errors, prev)
                self._update(spec.job_id, result={"attempt": spec.attempt})   # 中斷後續跑沿用此嘗試序號（M5-RS8-01）
                self._submit(spec, tier)
                return
            if self._skip_if_stale(spec):   # D86：保守回寫前再檢查一次
                return
            # 比照 post 先標記（M5-RS1-01）：on_invalid 寫回之後、標 conservative_fallback 之前被中斷時，
            # 續跑由 _begin 改 failed、不重跑 on_invalid（避免重複決策與第二條替代鏈）
            mark = self._decision_mark()
            self._update(spec.job_id, result={"posting": True, "decision_mark": mark})
            try:
                ids = []
                for nj in self._handler(spec.job_type).on_invalid(spec, errors, self.ctx) or []:
                    nj.deps = [d for d in nj.deps if d != spec.job_id]   # 替代工作不依賴本工作（M5-D1-01）
                    self.add_job(nj)
                    ids.append(nj.job_id)
                if ids:
                    self._update(spec.job_id, result={"posting": True, "decision_mark": mark, "replaced_by": ids})
            except Exception as e:  # noqa: BLE001 - 沒有保守處理（或處理失敗）：真正的失敗
                if self.conn.in_transaction:
                    self.conn.rollback()
                self._fail(spec, _err(e), result={"errors": errors}, on_fail=False)
                return
            # D43：已依規則寫了保守結論（或建了替代工作）→ 終態 conservative_fallback，不算失敗；另記低嚴重度異常
            self._update(spec.job_id, status=FALLBACK, error="invalid_output", result={"errors": errors},
                         finished_at=db.now_iso())
            self._record_fallback(spec, errors)
            self._drop_sandbox(spec)
            return

        if self._skip_if_stale(spec):   # D86：模型執行期間（等待同意數日等）該檔已有較新決策 → post 前跳過
            return
        # 進入 post 前先標記（M5-D1-02）：post 中途被中斷時，續跑不重跑 post（避免重複決策、重複後續工作）
        self._update(spec.job_id, result={"posting": True, "decision_mark": self._decision_mark()})
        try:
            handler = self._handler(spec.job_type)
            new_jobs = handler.post(spec, normalized, self.ctx)
            for nj in new_jobs:
                self.add_job(nj)
        except Exception as e:  # noqa: BLE001
            self._fail(spec, _err(e))
            return
        self._update(spec.job_id, status="done", result=normalized, error=None, finished_at=db.now_iso())
        self._drop_sandbox(spec)

    def _check(self, spec: JobSpec, result: LLMResult) -> tuple[dict | None, list[str]]:
        parsed = result.parsed_json   # extract_json 只回 dict 或 None
        if not isinstance(parsed, dict):
            return None, ["輸出不是單一 JSON 物件"]
        ref = None
        if spec.job_type == "FULL_B":   # D29：帶前半結果驗證，FULL_B 升級等級 → 不合格、走驗證重試
            pa = ticker_dir(self.ctx.work_dir, spec) / "phaseA.json"
            ref = json.loads(pa.read_text(encoding="utf-8")) if pa.is_file() else None
        return validate.check(spec.job_type, parsed, self._current_schemas(), ref=ref,
                              expect={"ticker": spec.ticker, "decision_date": spec.asof})

    # ------------------------------------------------------------ 記錄

    def _record_call(self, spec: JobSpec, tier: str, result: LLMResult | None, ok: bool, error: str | None,
                     exc: BaseException | None = None) -> None:
        """一次模型呼叫一列。失敗呼叫的用量與原始回應取自 TransportError（M5-D1-07）；input_path＝本工作本次嘗試
        最新的原始提示檔（M3_D9-01）。"""
        tcfg = self.router.tier_config(tier)
        prompt = latest_raw(spec.log_dir, spec, "prompt.txt")
        out = result.raw_path if result is not None else getattr(exc, "raw_path", None)
        db.insert(self.conn, "llm_calls", {
            "job_id": spec.job_id,
            "run_id": spec.run_id,
            "tier": tier,
            "backend": tcfg.backend,
            "model": result.model if result and result.model else tcfg.model,
            "attempt": spec.attempt,
            "prompt_sha256": hashlib.sha256(spec.prompt_text.encode("utf-8")).hexdigest(),
            "input_path": str(prompt) if prompt else None,
            "output_path": str(out) if out else None,
            "input_tokens": result.input_tokens if result else int(getattr(exc, "input_tokens", 0) or 0),
            "output_tokens": result.output_tokens if result else int(getattr(exc, "output_tokens", 0) or 0),
            "cost_usd": result.cost_usd if result else getattr(exc, "cost_usd", None),
            "latency_s": result.latency_s if result else None,
            "ok": 1 if ok else 0,
            "error": error,
            "created_at": db.now_iso(),
        })

    def _count_attempt(self, spec: JobSpec, result: LLMResult | None, failed: list | None = None) -> None:
        """attempts 每次送出 +1；token／費用另加上失敗呼叫（TransportError 帶回的）用量。"""
        objs = [o for o in (*(failed or []), result) if o is not None]
        self.conn.execute(
            "UPDATE jobs SET attempts=attempts+1, input_tokens=input_tokens+?, output_tokens=output_tokens+?,"
            " cost_usd=COALESCE(cost_usd,0)+?, latency_s=COALESCE(latency_s,0)+? WHERE job_id=?",
            (sum(int(getattr(o, "input_tokens", 0) or 0) for o in objs),
             sum(int(getattr(o, "output_tokens", 0) or 0) for o in objs),
             sum(float(getattr(o, "cost_usd", 0) or 0.0) for o in objs),
             result.latency_s if result else 0.0, spec.job_id),
        )
        self.conn.commit()

    def _fail(self, spec: JobSpec, error: str, result: dict | None = None, on_fail: bool = True) -> None:
        fields: dict[str, Any] = {"status": "failed", "error": error, "finished_at": db.now_iso(),
                                  "result": {**(result or {}), "decision_mark": self._decision_mark()}}
        self._update(spec.job_id, **fields)
        self._drop_sandbox(spec)
        if on_fail:
            self._on_fail(spec, error)

    def _record_fallback(self, spec: JobSpec, errors: list[str]) -> None:
        """D43：conservative_fallback 另記一筆低嚴重度異常（不暫停交易），方便追蹤不合格輸出的頻率。"""
        db.insert(self.conn, "anomalies", {
            "ticker": spec.ticker, "kind": FALLBACK_KIND, "status": "open", "pause_trading": 0,
            "detail": {"severity": "low", "run_id": spec.run_id, "job_id": spec.job_id, "job_type": spec.job_type,
                       "errors": list(errors)[:10]},
            "created_at": db.now_iso()})

    def _update(self, job_id: str, **fields: Any) -> None:
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
        vals.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE job_id=?", vals)
        self.conn.commit()

    def _counts(self, run_id: str) -> dict[str, int]:
        counts = {"done": 0, "failed": 0, "skipped": 0, "pending": 0, "awaiting_approval": 0, FALLBACK: 0}
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE run_id=? GROUP BY status", (run_id,)
        ).fetchall()
        for r in rows:
            if r["status"] in counts:
                counts[r["status"]] = int(r["n"])
            elif r["status"] == "running":
                counts["pending"] += int(r["n"])
        return counts
