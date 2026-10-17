"""工作引擎（代理 D）：依 DAG 執行工作、驗證輸出、動態升級、預算與斷點續跑。

執行緒模型：主執行緒負責所有資料庫寫入；只有 call_with_retry（模型呼叫）在 ThreadPoolExecutor 中執行。
各 tier 同時執行數不超過 tier_config.concurrency。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Callable

from .. import db
from ..config import Config
from ..llm.base import LLMResult, call_with_retry
from ..llm.router import Router
from ..models import PRICE_ISOLATED_JOBS, JobSpec
from ..paths import Paths
from . import approvals, validate
from . import isolation
from .context import build_prompt
from .handlers import HANDLERS, Handler, HandlerCtx
from .sandbox import purge_stale_sandboxes, remove_sandbox

MAX_ATTEMPTS = 2        # 首次 + 1 次驗證重試（routing defaults.on_invalid_output）
TRANSPORT_RETRIES = 3   # 傳輸錯誤的額外重試次數（call_with_retry）


JOB_FAILED_KIND = "job_failed"


def retry_failed(conn, run_id: str, ticker: str | None = None, job_id: str | None = None) -> list[str]:
    """把 run 內 failed（與因依賴失敗而 skipped）的工作重設為 pending，供 Engine.run 續跑。
    清 error／result／finished_at；llm_calls 與 attempts 保留作歷史。CORP_ACTION 不重設（由 daily 重派事件），
    對應的 job_failed 異常順手結案。回傳被重設的 job_id。"""
    sql = "SELECT job_id, job_type, ticker, status FROM jobs WHERE run_id=? AND status IN ('failed','skipped')"
    args: list[Any] = [run_id]
    if ticker:
        sql += " AND ticker=?"
        args.append(ticker)
    if job_id:
        sql += " AND job_id=?"
        args.append(job_id)
    ids = []
    for r in conn.execute(sql, args).fetchall():
        if r["job_type"] == "CORP_ACTION":
            continue
        ids.append(r["job_id"])
        conn.execute("UPDATE jobs SET status='pending', error=NULL, result=NULL, finished_at=NULL WHERE job_id=?",
                     (r["job_id"],))
    if job_id:  # 指定單一工作時，同檔因它而 skipped 的後續工作一併重設
        for r in conn.execute("SELECT job_id, deps FROM jobs WHERE run_id=? AND status='skipped'", (run_id,)).fetchall():
            if set(json.loads(r["deps"])) & set(ids):
                ids.append(r["job_id"])
                conn.execute("UPDATE jobs SET status='pending', error=NULL, result=NULL, finished_at=NULL WHERE job_id=?",
                             (r["job_id"],))
    for a in conn.execute("SELECT anomaly_id, detail FROM anomalies WHERE kind=? AND status='open'",
                          (JOB_FAILED_KIND,)).fetchall():
        if (json.loads(a["detail"]) if a["detail"] else {}).get("job_id") in ids:
            conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? WHERE anomaly_id=?",
                         (db.now_iso(), "已用 jobs retry 重設重跑", a["anomaly_id"]))
    conn.commit()
    return ids


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


class Engine:
    def __init__(self, conn, router: Router, paths: Paths, config: Config,
                 handlers: dict[str, Handler] | None = None, schemas: dict | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.conn = conn
        self.router = router
        self.paths = paths
        self.config = config
        self.handlers = HANDLERS if handlers is None else handlers
        self.schemas = schemas if schemas is not None else validate.load_schemas(paths.schemas_json)
        self.sleep = sleep
        self.ctx = HandlerCtx(paths=paths, schemas=self.schemas, work_dir=paths.work, conn=conn,
                              sandbox_root=self._sandbox_root(config, paths, router), config=config)
        self._inflight: dict[Future, tuple[JobSpec, str]] = {}
        self._busy: Counter = Counter()
        self._base_prompt: dict[str, str] = {}
        self._pool: ThreadPoolExecutor | None = None
        if self._isolated_root():
            purge_stale_sandboxes(self.ctx.sandbox_root)

    def _isolated_root(self) -> bool:
        root = self.ctx.sandbox_root
        return root is not None and Path(root).resolve() != Path(self.paths.work).resolve()

    def _drop_sandbox(self, spec: JobSpec) -> None:
        """看得到價格的工作結束後刪沙箱（輸出已收回 work）：兄弟沙箱可互讀，留著會被隔離工作讀到價格。"""
        if spec.job_type in PRICE_ISOLATED_JOBS or not spec.sandbox or not self._isolated_root():
            return
        remove_sandbox(Path(spec.sandbox), self.ctx.sandbox_root)

    @staticmethod
    def _sandbox_root(config, paths, router) -> Path:
        """實際會呼叫 claude_code 時，沙箱放在隔離根目錄（家目錄與暫存目錄之外）；否則（含 dry-run）放 state/work。"""
        models = getattr(config, "models", None) or {}
        if getattr(router, "dry_run", False):
            return paths.work
        if not any(getattr(t, "backend", "") == "claude_code" for t in models.values()):
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
        """加入工作；同 job_id 已存在 → False，不覆蓋。"""
        if self.conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (spec.job_id,)).fetchone() is not None:
            return False
        # 先轉成可 JSON 化的值（路徑等以字串存）
        params = json.loads(json.dumps(spec.params, ensure_ascii=False, default=str))
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
        """執行一個 run 直到全部終態、或預算耗盡（paused_budget）。可重複呼叫以斷點續跑。"""
        self._inflight.clear()
        self._busy.clear()
        self._base_prompt.clear()
        self._begin(run_id)
        workers = max(1, sum(t.concurrency for t in self.config.models.values()))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            self._pool = pool
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
            self._pool = None

        blocked = self._budget_exhausted(run_id)
        counts = self._counts(run_id)
        if counts["pending"] and not blocked:
            # 沒有執行中、也不是預算因素：除了（間接）依賴等待同意工作者，剩下的待辦必為循環依賴
            waiting = self._waiting_closure(run_id)
            for row in self._pending_rows(run_id):
                if row["job_id"] in waiting:
                    continue
                self._update(row["job_id"], status="skipped", error="依賴無法滿足（循環？）",
                             finished_at=db.now_iso())
            counts = self._counts(run_id)
        if blocked and counts["pending"]:
            status = "paused_budget"
        elif counts["awaiting_approval"]:
            status = "awaiting_approval"
        elif counts["failed"] or counts["skipped"]:
            status = "failed"  # 有工作失敗或被跳過：不得標 done（done＝全部工作都收到通過驗證的結果）
        else:
            status = "done"
        self._record_failures(run_id)
        self.conn.execute(
            "UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
            (status, db.now_iso(), json.dumps(counts, ensure_ascii=False), run_id),
        )
        self.conn.commit()
        approvals.close_finished(self.conn, run_id)
        return {**counts, "status": status}

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

    # ------------------------------------------------------------ 排程

    def _begin(self, run_id: str) -> None:
        if self.conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
            raise ValueError(f"run {run_id} 不存在，請先 start_run")
        # 上次中斷殘留的 running 改回 pending；done 不動
        self.conn.execute("UPDATE jobs SET status='pending' WHERE run_id=? AND status='running'", (run_id,))
        self.conn.execute(
            "UPDATE runs SET status='running', started_at=COALESCE(started_at, ?) WHERE run_id=?",
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
                if st in ("failed", "skipped"):
                    reason = f"依賴 {d} 狀態為 {st}"
                    break
                if st != "done":
                    all_done = False
            if reason is not None:
                self._update(row["job_id"], status="skipped", error=reason, finished_at=db.now_iso())
                changed = True
            elif all_done:
                ready.append(row)
        return changed, ready

    def _budget_exhausted(self, run_id: str) -> bool:
        n, toks, cost = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id=?",
            (run_id,)).fetchone()
        b = self.config.budget
        return (n >= b.max_llm_calls_per_run
                or (b.max_cost_usd_per_run > 0 and cost >= b.max_cost_usd_per_run)
                or (b.max_input_tokens_per_run > 0 and toks >= b.max_input_tokens_per_run))

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
                self._update(spec.job_id, status="awaiting_approval", tier=tier)  # 不呼叫模型，等使用者同意
                return True
        except Exception as e:  # noqa: BLE001
            self._fail(spec, _err(e))
            return True
        if self._busy[tier] >= self.router.tier_config(tier).concurrency:
            return False
        try:
            handler = self._handler(spec.job_type)
            spec.tier = tier
            spec.attempt = 0
            spec.log_dir = self.paths.llm_calls / _safe(spec.run_id)
            tcfg = self.router.tier_config(tier)
            handler.pre_script(spec, self.ctx)
            sections = handler.prepare(spec, self.ctx)  # 建沙箱、stage 輸入（價格隔離在這裡檢查）
            spec.prompt_text = build_prompt(
                spec, tcfg.backend, self.router.max_context_tokens(spec.job_type, tier),
                self.paths.root, sections, values=handler.prompt_values(spec, self.ctx, tcfg.backend),
            ) + handler.prompt_suffix(spec)
        except Exception as e:  # noqa: BLE001 - 準備失敗只讓這個工作失敗
            self._fail(spec, _err(e))
            return True
        self._base_prompt[spec.job_id] = spec.prompt_text
        self._update(spec.job_id, status="running", tier=tier, model=tcfg.model,
                     sandbox=str(spec.sandbox) if spec.sandbox else None, started_at=db.now_iso())
        self._submit(spec, tier)
        return True

    def _submit(self, spec: JobSpec, tier: str) -> None:
        runner = self.router.runner_for(tier)
        fut = self._pool.submit(call_with_retry, runner, spec, retries=TRANSPORT_RETRIES, sleep=self.sleep)
        self._inflight[fut] = (spec, tier)
        self._busy[tier] += 1

    def _on_done(self, fut: Future) -> None:
        spec, tier = self._inflight.pop(fut)
        self._busy[tier] -= 1
        exc = fut.exception()
        if exc is not None:
            self._record_call(spec, tier, None, ok=False, error=_err(exc))
            self._count_attempt(spec, None)
            self._fail(spec, _err(exc))
            return
        result: LLMResult = fut.result()
        self._record_call(spec, tier, result, ok=True, error=None)
        self._count_attempt(spec, result)

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
            if spec.attempt + 1 < MAX_ATTEMPTS:
                spec.attempt += 1
                spec.prompt_text = self._base_prompt[spec.job_id] + "\n\n" + validate.error_feedback(errors)
                self._submit(spec, tier)
                return
            try:
                for nj in self._handler(spec.job_type).on_invalid(spec, errors, self.ctx) or []:
                    self.add_job(nj)
            except Exception as e:  # noqa: BLE001
                self._fail(spec, _err(e))
                return
            self._fail(spec, "invalid_output", result={"errors": errors})
            return

        try:
            handler = self._handler(spec.job_type)
            new_jobs = handler.post(spec, normalized, self.ctx)
            post_errors = handler.post_script(spec, self.ctx)
            if post_errors:
                raise ValueError("後置檢查失敗：" + "；".join(post_errors))
            for nj in new_jobs:
                self.add_job(nj)
        except Exception as e:  # noqa: BLE001
            self._fail(spec, _err(e))
            return
        self._update(spec.job_id, status="done", result=normalized, error=None, finished_at=db.now_iso())
        self._drop_sandbox(spec)

    def _check(self, spec: JobSpec, result: LLMResult) -> tuple[dict | None, list[str]]:
        parsed = result.parsed_json
        if isinstance(parsed, list) and len(parsed) == 1:  # PRICE 提示要求陣列；一檔一工作時取唯一元素
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            return None, ["輸出不是單一 JSON 物件"]
        return validate.check(spec.job_type, parsed, self.schemas)

    # ------------------------------------------------------------ 記錄

    def _record_call(self, spec: JobSpec, tier: str, result: LLMResult | None, ok: bool, error: str | None) -> None:
        tcfg = self.router.tier_config(tier)
        db.insert(self.conn, "llm_calls", {
            "job_id": spec.job_id,
            "run_id": spec.run_id,
            "tier": tier,
            "backend": tcfg.backend,
            "model": result.model if result and result.model else tcfg.model,
            "attempt": spec.attempt,
            "prompt_sha256": hashlib.sha256(spec.prompt_text.encode("utf-8")).hexdigest(),
            "output_path": str(result.raw_path) if result and result.raw_path else None,
            "input_tokens": result.input_tokens if result else 0,
            "output_tokens": result.output_tokens if result else 0,
            "cost_usd": result.cost_usd if result else None,
            "latency_s": result.latency_s if result else None,
            "ok": 1 if ok else 0,
            "error": error,
            "created_at": db.now_iso(),
        })

    def _count_attempt(self, spec: JobSpec, result: LLMResult | None) -> None:
        self.conn.execute(
            "UPDATE jobs SET attempts=attempts+1, input_tokens=input_tokens+?, output_tokens=output_tokens+?,"
            " cost_usd=COALESCE(cost_usd,0)+?, latency_s=COALESCE(latency_s,0)+? WHERE job_id=?",
            (result.input_tokens if result else 0, result.output_tokens if result else 0,
             (result.cost_usd or 0.0) if result else 0.0, result.latency_s if result else 0.0, spec.job_id),
        )
        self.conn.commit()

    def _fail(self, spec: JobSpec, error: str, result: dict | None = None) -> None:
        fields: dict[str, Any] = {"status": "failed", "error": error, "finished_at": db.now_iso()}
        if result is not None:
            fields["result"] = result
        self._update(spec.job_id, **fields)
        self._drop_sandbox(spec)

    def _update(self, job_id: str, **fields: Any) -> None:
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
        vals.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE job_id=?", vals)
        self.conn.commit()

    def _counts(self, run_id: str) -> dict[str, int]:
        counts = {"done": 0, "failed": 0, "skipped": 0, "pending": 0, "awaiting_approval": 0}
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE run_id=? GROUP BY status", (run_id,)
        ).fetchall()
        for r in rows:
            if r["status"] in counts:
                counts[r["status"]] = int(r["n"])
            elif r["status"] == "running":
                counts["pending"] += int(r["n"])
        return counts
