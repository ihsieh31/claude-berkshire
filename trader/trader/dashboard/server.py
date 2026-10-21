"""本機 API 伺服器（M6b）：網頁與 Swift 小儀表板的唯一資料來源。只綁 127.0.0.1。

安全：
- 每個請求檢查 Host（只接受 127.0.0.1／localhost:埠）→ 擋 DNS rebinding 讀資料。
- 會改狀態的 POST：須帶 X-Dashboard-Token（state/.dashboard_token，權限 600）、Content-Type 為 JSON；
  若有 Origin 必須是本機網頁本身（Swift 不送 Origin）→ 擋其他網頁偷送「同意」。
- 網頁的權杖由伺服器在 index.html 內嵌（只在通過 Host 檢查時提供）。
- 同意只收明確的 ids（D54：前端送出畫面上那批編號；all: true 回 400）。
- 預算暫停的 run 不自動續跑（D44）：/api/runs/resume 背景 `trader jobs resume`，/api/runs/abandon 同 `trader jobs abandon`。
"""
from __future__ import annotations

import fcntl
import json
import re
import hmac
import os
import secrets
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import db
from ..jobs import approvals as A
from ..paths import PROJECT_DIR, Paths
from ..report import export as X
from ..report import metrics as M
from .charts import RANGES as CHART_RANGES, ChartService

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TOKEN_FILE = ".dashboard_token"
TOKEN_PLACEHOLDER = "{{DASHBOARD_TOKEN}}"
LOOPBACK = ("127.0.0.1", "localhost")
TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
# 只允許本機資源；script 不允許行內（擋報告內容注入），樣式允許行內 style 屬性
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def ensure_token(paths: Paths) -> str:
    """讀取或建立權杖檔（600）。"""
    f = paths.state / TOKEN_FILE
    if f.is_file():
        tok = f.read_text(encoding="utf-8").strip()
        if tok:
            os.chmod(f, 0o600)
            return tok
    tok = secrets.token_urlsafe(32)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(tok)
    os.chmod(f, 0o600)
    return tok


def spawn_cli(config_path: Path | None, paths: Paths, args: list[str], log_name: str) -> subprocess.Popen:
    """背景啟動 `trader <args>`（獨立行程群組；輸出附加到 state/logs/<log_name>）。"""
    log_dir = paths.state / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "trader.cli", *args, "--state-dir", str(paths.state)]
    if config_path:
        cmd += ["--config", str(config_path)]
    with open(log_dir / log_name, "a", encoding="utf-8") as log:
        return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=str(PROJECT_DIR), start_new_session=True)


def spawn_run_approved(config_path: Path | None, paths: Paths) -> None:
    """背景啟動 `trader run-approved`；先記 started_at／pid（行程一啟動就失敗時，網頁才看得到沒有結果，M8-D5-04）。"""
    from ..pipelines.approved import LOG_FILE, write_status
    proc = spawn_cli(config_path, paths, ["run-approved"], "run_approved.log")
    write_status(paths, {"started_at": db.now_iso(), "pid": proc.pid, "log": LOG_FILE, "spawned_by": "dashboard"})


def last_run(paths: Paths, running: bool) -> dict | None:
    """上次 run-approved 的結果（state/.run_approved.json）。沒有 finished_at、也不在執行 → 行程已結束卻沒寫結果。"""
    from ..pipelines.approved import read_status
    st = read_status(paths)
    if st and not st.get("finished_at") and not running:
        st["ended_without_result"] = True
    return st


def run_approved_busy(paths: Paths) -> bool:
    f = paths.state / ".run_approved.lock"
    if not f.exists():
        return False
    with open(f, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def _benchmark(config_path: Path | None) -> str:
    """設定檔 account.benchmark（網頁顯示用基準代號，A3-M8C2-10）；沒給設定檔或讀不到 → SPY。"""
    if config_path is None:
        return "SPY"
    try:
        from ..config import load_config
        return str(load_config(config_path).account.benchmark or "SPY").upper()
    except Exception:  # noqa: BLE001 - 只是顯示名稱，讀不到用預設
        return "SPY"


def create_app(paths: Paths, port: int, config_path: Path | None = None,
               spawn: Callable[[], None] | None = None, extra_hosts: tuple[str, ...] = (),
               charts: ChartService | None = None, spawn_cmd: Callable[[list[str]], Any] | None = None,
               budget: dict | None = None) -> FastAPI:
    token = ensure_token(paths)
    hosts = {f"{h}:{port}" for h in LOOPBACK} | set(extra_hosts)
    origins = {f"http://{h}" for h in hosts}
    spawn = spawn or (lambda: spawn_run_approved(config_path, paths))
    spawn_cmd = spawn_cmd or (lambda args: spawn_cli(config_path, paths, args, "dashboard_jobs.log"))
    app = FastAPI(title="berkshire-trader dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    bench = _benchmark(config_path)

    @contextmanager
    def conn():
        c = db.connect(paths.db)
        try:
            db.migrate(c)
            yield c
        finally:
            c.close()

    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        if request.headers.get("host", "") not in hosts:
            return JSONResponse({"error": "host 不允許"}, status_code=403)
        return await call_next(request)

    def guard_write(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            raise HTTPException(403, "origin 不允許")
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "須為 application/json")
        if not hmac.compare_digest(request.headers.get("x-dashboard-token", ""), token):
            raise HTTPException(401, "權杖錯誤")

    # ------------------------------------------------------------ 讀取
    @app.get("/api/summary")
    def api_summary() -> dict:
        with conn() as c:
            return {**M.summary(c, paths.state), "bench": bench}

    @app.get("/api/watchlist")
    def api_watchlist() -> list:
        with conn() as c:
            return M.watchlist(c)

    @app.get("/api/nav")
    def api_nav(range: str = "ALL") -> dict:  # noqa: A002 - 查詢參數名稱
        if range not in M.RANGES:
            raise HTTPException(400, f"range 須為 {'、'.join(M.RANGES)}")
        with conn() as c:
            return M.chart(c, range)

    @app.get("/api/holdings")
    def api_holdings() -> list:
        with conn() as c:
            return M.holdings(c)

    @app.get("/api/charts")
    def api_charts(tickers: str, ranges: str = ",".join(CHART_RANGES)) -> dict:
        """個股走勢（彈出視窗）。tickers 逗號分隔；ranges 為 1D,1W,3M,6M,10Y 的子集。"""
        if charts is None:
            raise HTTPException(503, "未設定 Alpaca 金鑰，無法取得走勢")
        tks = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        rgs = [r.strip().upper() for r in ranges.split(",") if r.strip()]
        if not tks or any(not TICKER_RE.fullmatch(t) for t in tks):
            raise HTTPException(400, "代號格式不符")
        if not rgs or any(r not in CHART_RANGES for r in rgs):
            raise HTTPException(400, f"ranges 須為 {'、'.join(CHART_RANGES)} 的子集")
        return charts.get(tks, rgs)

    @app.get("/api/trades")
    def api_trades(ticker: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        with conn() as c:
            return M.trades(c, ticker.upper() if ticker else None, start, end)

    @app.get("/api/analyses")
    def api_analyses(ticker: str | None = None, limit: int = 200) -> list:
        with conn() as c:
            return M.analyses(c, ticker.upper() if ticker else None, max(1, min(limit, 1000)))

    @app.get("/api/analyses/{decision_id}")
    def api_report(decision_id: str) -> dict:
        with conn() as c:
            r = M.report(c, paths.work, decision_id)
        if r is None:
            raise HTTPException(404, "沒有這筆決策")
        return r

    @app.get("/api/usage")
    def api_usage() -> list:
        with conn() as c:
            return M.usage(c)

    @app.get("/api/anomalies")
    def api_anomalies() -> dict:
        with conn() as c:
            return M.anomalies(c)

    @app.get("/api/approvals")
    def api_approvals() -> dict:
        running = run_approved_busy(paths)
        with conn() as c:
            s = A.summary(c)
            paused = [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in c.execute(
                "SELECT run_id, kind, asof, status, finished_at, summary FROM runs WHERE status='paused_budget' "
                "ORDER BY asof, run_id")]
            return {"items": A.list_items(c), "summary": {**s, "label": A.label(s) if s["n_pending"] else None},
                    "running": running, "last_run": last_run(paths, running),
                    "paused_runs": paused, "budget": budget}   # D44：預算暫停的 run 由使用者按續跑／放棄

    @app.get("/api/export/{name}")
    def api_export(name: str) -> Response:
        """{資料集}.csv，或 all.xlsx／all.json。"""
        stem, _, fmt = name.rpartition(".")
        with conn() as c:
            try:
                if stem == "all" and fmt in ("xlsx", "json"):
                    data = {"meta": [X.meta(c)], **{n: X.dataset(c, n) for n in X.DATASETS}}
                    if fmt == "xlsx":
                        body, mt = X.to_xlsx(data), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    else:
                        body, mt = X.to_json(data), "application/json"
                elif fmt == "csv" and stem in X.DATASETS:
                    body, mt = X.to_csv(X.dataset(c, stem)), "text/csv; charset=utf-8"
                else:
                    raise HTTPException(404, "匯出格式：{資料集}.csv、all.xlsx、all.json")
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        return Response(body, media_type=mt, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------ 同意（需權杖）
    async def _body(request: Request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "內容不是 JSON") from None
        if not isinstance(b, dict):
            raise HTTPException(400, "內容須為 JSON 物件")
        return b

    def _ids(b: dict) -> list[int]:
        """只收明確的編號（D54：同意只涵蓋畫面上看到的那批；all: true 一律 400）。"""
        ids = b.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise HTTPException(400, "須給 ids（整數陣列）")
        return ids

    def _via(b: dict) -> str:
        v = b.get("via")
        return v if v in ("web", "desktop") else "web"

    def _start_run_approved() -> tuple[bool, str | None]:
        """有已同意項目時背景啟動 run-approved。回傳 (是否啟動, 沒啟動的說明)。"""
        from ..scheduler import pipeline_holder
        if run_approved_busy(paths):   # 執行中的 run-approved 每輪結束會再放行新同意的（M8-D2-03）
            return False, "已排入目前執行，結束前會一併處理"
        holder = pipeline_holder(paths)   # 背景 run-approved 也要取同一把寫入鎖（D10）：被占用就不啟動
        if holder is not None:
            return False, (f"排程或其他指令執行中（{holder}）；排程會在其後執行已同意項目，"
                           "或稍後在待同意頁按「執行」、或執行 trader run-approved")
        spawn()
        return True, None

    @app.post("/api/approvals/approve")
    async def api_approve(request: Request) -> dict:
        guard_write(request)
        b = await _body(request)
        ids = _ids(b)   # D54：只同意畫面上那批編號，不收 all
        with conn() as c:
            try:
                done = A.approve(c, ids, via=_via(b))
            except A.ApprovalError as e:
                raise HTTPException(409, str(e)) from None
            q = ",".join("?" * len(done))
            paused = [r[0] for r in c.execute(
                f"SELECT DISTINCT a.run_id FROM approvals a JOIN runs r ON r.run_id=a.run_id "
                f"WHERE a.approval_id IN ({q}) AND r.status='paused_budget' ORDER BY a.run_id", done)] if done else []
            idle = bool(paused) and not A.runnable(c)   # 只同意了預算暫停 run 裡的項目：run-approved 不會跑（D44）
        hint = f"{'、'.join(paused)} 預算暫停，需按「續跑」才會執行" if paused else None
        if idle:
            return {"approved": done, "started": False, "note": f"已同意；{hint}"}
        started, note = _start_run_approved() if done else (False, None)
        note = "；".join(x for x in (note, hint) if x) or None
        return {"approved": done, "started": started, "note": note}

    @app.post("/api/approvals/run")
    async def api_run_approved(request: Request) -> dict:
        """已同意但沒在執行的項目：背景啟動 run-approved（M8-D2-03）。"""
        guard_write(request)
        await _body(request)
        with conn() as c:
            if not A.runnable(c):   # 只剩失敗工作的已同意列不算（M8-D5-04：retry／abandon 處理）
                raise HTTPException(409, "沒有已同意待執行的項目（有失敗工作的請 trader jobs retry 或 abandon）")
        started, note = _start_run_approved()
        if not started:
            raise HTTPException(409, note)
        return {"started": True}

    def _paused_run(b: dict) -> str:
        rid = b.get("run_id")
        if not isinstance(rid, str) or not rid:
            raise HTTPException(400, "須給 run_id")
        with conn() as c:
            r = c.execute("SELECT status FROM runs WHERE run_id=?", (rid,)).fetchone()
        if r is None or r[0] != "paused_budget":
            raise HTTPException(409, f"run {rid} 不是預算暫停狀態")
        return rid

    @app.post("/api/runs/resume")
    async def api_resume(request: Request) -> dict:
        """D44 續跑：背景 `trader jobs resume --run-id X`（會呼叫模型；仍受預算約束）。"""
        guard_write(request)
        rid = _paused_run(await _body(request))
        from ..scheduler import pipeline_holder
        holder = pipeline_holder(paths)
        if holder is not None:
            raise HTTPException(409, f"排程或其他指令執行中（{holder}），稍後再按")
        spawn_cmd(["jobs", "resume", "--run-id", rid])
        return {"started": True, "run_id": rid, "log": "state/logs/dashboard_jobs.log"}

    @app.post("/api/runs/abandon")
    async def api_abandon(request: Request) -> dict:
        """D44 放棄：同 `trader jobs abandon --run-id X`（不呼叫模型、不寫決策）。取寫入鎖，被占用就不做。"""
        guard_write(request)
        b = await _body(request)
        rid = _paused_run(b)
        note = b.get("note") if isinstance(b.get("note"), str) and b.get("note").strip() else None
        from ..jobs.engine import abandon
        from ..scheduler import PipelineBusy, pipeline_lock
        try:
            with pipeline_lock(paths, "儀表板 放棄 " + rid, wait=False), conn() as c:
                if c.execute("SELECT status FROM runs WHERE run_id=?", (rid,)).fetchone()[0] != "paused_budget":
                    raise HTTPException(409, f"run {rid} 已不是預算暫停狀態")   # 取鎖前剛續跑完：不把 done 蓋成 abandoned
                ids = abandon(c, rid, note=note)
                st = c.execute("SELECT status FROM runs WHERE run_id=?", (rid,)).fetchone()[0]
        except PipelineBusy as e:
            raise HTTPException(409, f"排程或其他指令執行中（{e}），稍後再按") from None
        return {"abandoned": ids, "run_id": rid, "status": st}

    @contextmanager
    def write_lock(what: str):
        """同 CLI 的寫入鎖（D10）：daily／run-approved 執行中就回 409，不與其並行寫（A3-M8B-02）。"""
        from ..scheduler import PipelineBusy, pipeline_lock
        try:
            with pipeline_lock(paths, "儀表板 " + what, wait=False):
                yield
        except PipelineBusy as e:
            raise HTTPException(409, f"排程或其他指令執行中（{e}），稍後再按") from None

    @app.post("/api/approvals/defer")
    async def api_defer(request: Request) -> dict:
        guard_write(request)
        b = await _body(request)
        ids = _ids(b)
        with write_lock("延後"), conn() as c:
            try:
                return {"deferred": A.defer(c, ids, via=_via(b))}
            except A.ApprovalError as e:
                raise HTTPException(409, str(e)) from None

    @app.post("/api/approvals/reject")
    async def api_reject(request: Request) -> dict:
        guard_write(request)
        b = await _body(request)
        ids = _ids(b)
        note = b.get("note") if isinstance(b.get("note"), str) else None
        with write_lock("拒絕"), conn() as c:
            try:
                return {"decisions": A.reject(c, paths, ids, via=_via(b), note=note,
                                              time_zone=A.config_timezone(config_path))}
            except A.ApprovalError as e:
                raise HTTPException(409, str(e)) from None

    # ------------------------------------------------------------ 網頁
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Referrer-Policy": "no-referrer", "Content-Security-Policy": CSP})

    static = WEB_DIR / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=str(static)), name="static")
    return app


def serve(paths: Paths, host: str, port: int, config_path: Path | None = None,
          charts: ChartService | None = None, budget: dict | None = None) -> None:
    if host not in LOOPBACK:
        raise ValueError(f"儀表板只能綁本機（{'／'.join(LOOPBACK)}），目前設定 {host!r}")
    import uvicorn
    uvicorn.run(create_app(paths, port, config_path, charts=charts, budget=budget), host="127.0.0.1", port=port,
                log_level="warning")
