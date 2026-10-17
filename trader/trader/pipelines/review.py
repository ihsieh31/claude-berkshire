"""觸發檢查 pipeline（M4）：登錄表 → 價格／利率／新聞檔 → skill review_due.py（子程序，不呼叫模型）
→ 每檔至多一個起始工作（TRIAGE／DOSSIER／THESIS／PRICE）→ 引擎。

輸出目錄：work/{run_id}/（registry.json、daily.csv、news.json、due.json、summary.json）。
規則（公式、閾值、動作判定）全部在 skill 的 review_due.py，這裡不複製。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .. import db
from ..analysis.dossier_prep import DossierError, _user_agent, is_price_headline
from ..config import Config
from ..data.inputs import make_clients
from ..data.pe_median import get_frozen
from ..data.universe import normalize_ticker
from ..jobs.engine import Engine
from ..llm.router import Router
from ..models import JobSpec
from ..ledger import book
from ..paths import Paths
from ..portfolio.holdings_view import watchlist_tickers, write_view
from .analyze import _portfolio_state, base_params

REGISTRY_SCHEMA = "berkshire-deep-merged/registry-v1"
SIGNAL_VERSION = "review-due-v2"
SCOPES = ("holdings", "watch", "all")
SCOPE_STATUS = {"holdings": "HOLDING", "watch": "WATCH"}
PRICE_LOOKBACK_DAYS = 200     # 曆日；skill 需要至少 100 個交易日
NEWS_FALLBACK_DAYS = 30       # 登錄表都沒有 last_review_date 時的新聞起算日
SKILL_TIMEOUT_S = 1800


class ReviewError(Exception):
    """觸發檢查無法完成（skill 失敗、輸出格式不符、範圍不合法）。"""


def export_registry(conn, scope: str) -> list[dict]:
    """登錄表列 → dict（data 內容＋ticker、status）。scope: holdings（HOLDING）｜watch（WATCH）｜all。"""
    if scope not in SCOPES:
        raise ReviewError(f"scope 不合法：{scope!r}（應為 {'、'.join(SCOPES)}）")
    out = []
    for r in conn.execute("SELECT ticker, status, data FROM registry ORDER BY ticker"):
        if scope in SCOPE_STATUS and r["status"] != SCOPE_STATUS[scope]:
            continue
        d = json.loads(r["data"]) if r["data"] else {}
        # 空欄位（schema 骨架填 None）略過：skill 以 e.get(k, 預設) 取值，None 會讓清單欄位無法迭代
        d = {k: v for k, v in d.items() if v is not None}
        d["ticker"] = normalize_ticker(r["ticker"])
        d["status"] = r["status"] or d.get("status")
        out.append(d)
    return out


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _last_closes(df: pd.DataFrame) -> dict[str, dict]:
    """每檔最後一筆收盤：{ticker: {price, price_date}}。"""
    if df.empty:
        return {}
    last = df.sort_values(["ticker", "date"]).groupby("ticker").tail(1)
    return {r["ticker"]: {"price": float(r["close"]), "price_date": str(r["date"])} for r in last.to_dict("records")}


def _held_closes(conn, alpaca, asof: str, last_px: dict[str, dict]) -> dict[str, dict]:
    """G8 比較檔用：scope 範圍外的持股與觀察名單另抓決策日收盤；抓不到就缺，比較檔會標註。"""
    out = dict(last_px)
    miss = [t for t in dict.fromkeys(list(book.positions(conn)) + watchlist_tickers(conn)) if t not in out]
    if miss and alpaca is not None:
        start = (date.fromisoformat(asof) - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
        bars = alpaca.bars(miss, start=start, end=asof, adjustment="raw")
        if not bars.empty:
            out.update(_last_closes(bars[bars["date"] <= asof][["ticker", "date", "close"]]))
    return out


def run_review(cfg: Config, paths: Paths, conn, asof: str, scope: str = "all", run_id: str | None = None,
               clients: dict[str, Any] | None = None, router: Router | None = None,
               offline_filings: Path | None = None, due_only: bool = False) -> dict[str, Any]:
    if scope not in SCOPES:
        raise ReviewError(f"scope 不合法：{scope!r}")
    run_id = run_id or f"review-{asof}-{scope}"
    cl = clients if clients is not None else make_clients(cfg, paths)
    sec, alpaca, fred = cl.get("sec"), cl.get("alpaca"), cl.get("fred")
    out = paths.work / run_id
    out.mkdir(parents=True, exist_ok=True)
    if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        db.insert(conn, "runs", {"run_id": run_id, "kind": "review", "asof": asof, "status": "running",
                                 "started_at": db.now_iso()})
    else:
        conn.execute("UPDATE runs SET status='running', started_at=?, finished_at=NULL WHERE run_id=?",
                     (db.now_iso(), run_id))
        conn.commit()
    summary: dict[str, Any] = {"run_id": run_id, "asof": asof, "scope": scope, "outdir": str(out)}
    engine_result: dict[str, Any] | None = None
    try:
        engine_result = _execute(cfg, paths, conn, asof, scope, run_id, out, sec, alpaca, fred, router,
                                 offline_filings, due_only, summary)
        status = engine_result["status"] if engine_result else "done"
    except Exception as e:  # noqa: BLE001 - 與 run_screen 相同：錯誤寫進 summary 與 runs 表
        summary["error"] = f"{type(e).__name__}: {e}"
        status = "failed"
    summary["engine"] = engine_result
    _write_json(out / "summary.json", summary)
    conn.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
                 (status, db.now_iso(), json.dumps(summary, ensure_ascii=False, default=str), run_id))
    conn.commit()
    return summary


def _execute(cfg, paths, conn, asof, scope, run_id, out, sec, alpaca, fred, router, offline_filings,
             due_only, summary) -> dict[str, Any] | None:
    """流程 1–9；summary 原地補欄位。due_only 或建工作時回傳引擎結果（due_only 回 None）。"""
    # 1–2. 登錄表 → registry.json（缺 cik 由 SEC 對照補；補不到則排除）
    skipped: list[dict] = []
    entries: list[dict] = []
    for e in export_registry(conn, scope):
        t = e["ticker"]
        if not e.get("cik"):
            cik = sec.cik_for(t) if sec is not None else None
            if not cik:
                skipped.append({"ticker": t, "reason": "缺 CIK 且 SEC 對照無此代號"})
                continue
            e["cik"] = cik
        entries.append(e)
    summary["skipped"] = skipped
    tickers = [e["ticker"] for e in entries]
    reg_path = out / "registry.json"
    _write_json(reg_path, {"schema": REGISTRY_SCHEMA, "entries": entries})

    # 3. 日線 → daily.csv（raw 價；只留 ≤ asof）。沒有 Alpaca 時寫表頭空檔，避免 skill 改抓 Yahoo 連網
    px_path = out / "daily.csv"
    if alpaca is not None and tickers:
        start = (date.fromisoformat(asof) - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
        bars = alpaca.bars(tickers, start=start, end=asof, adjustment="raw")
        daily = bars[bars["date"] <= asof][["ticker", "date", "close"]].reset_index(drop=True)
    else:
        daily = pd.DataFrame(columns=["ticker", "date", "close"])
    daily.to_csv(px_path, index=False)
    last_px = _last_closes(daily)
    skill_px = px_path
    if daily.empty:  # skill 對「只有表頭」的檔會因空日期欄失敗；改給一列永不匹配的哨兵列（僅供 skill 讀取）
        skill_px = out / "prices_sentinel.csv"
        skill_px.write_text(f"ticker,date,close\n_NONE_,{asof},\n", encoding="utf-8")

    # 4. 30 年期公債殖利率（取不到則不傳 --t30）
    rates: dict[str, Any] | None = None
    t30 = None
    if fred is None:
        summary["t30"] = {"error": "未設定 FRED 金鑰"}
    else:
        try:
            t30, t30_date = fred.t30(asof)
            rates = {"t30": t30, "t30_date": t30_date, "t30_20y_median": None}
            try:
                rates["t30_20y_median"] = fred.t30_median_20y(asof)
            except Exception:  # noqa: BLE001 - 中位數缺只影響 P4′，不擋觸發檢查
                pass
            summary["t30"] = {"t30": t30, "t30_date": t30_date}
        except Exception as e:  # noqa: BLE001 - 利率取不到照常執行，記入 summary
            t30 = None
            summary["t30"] = {"error": f"{type(e).__name__}: {e}"}

    # 5. 新聞 → news.json（只在有 Alpaca 時）
    news_path: Path | None = None
    if alpaca is not None and tickers:
        dates = [str(e["last_review_date"])[:10] for e in entries if e.get("last_review_date")]
        start = min(dates) if dates else (date.fromisoformat(asof) - timedelta(days=NEWS_FALLBACK_DAYS)).isoformat()
        news: dict[str, list[dict]] = {t: [] for t in tickers}
        dropped = 0
        for n in alpaca.news(tickers, start=start, end=asof):
            if is_price_headline(n.get("headline", "")):  # 價格隔離：目標價、評等、漲跌標題不進任何下游
                dropped += 1
                continue
            news.setdefault(n["ticker"], []).append({"date": n["date"], "headline": n["headline"],
                                                     "source": n["source"], "url": n["url"]})
        summary["news_price_headlines_dropped"] = dropped
        news_path = out / "news.json"
        _write_json(news_path, news)

    # 6. 子程序執行 skill 的 review_due.py（規則以 skill 為準）
    due_path = out / "due.json"
    cmd = [sys.executable, str(paths.skill_deep / "scripts" / "review_due.py"),
           "--registry", str(reg_path), "--asof", asof, "--prices", str(skill_px), "--out", str(due_path)]
    if t30 is not None:
        cmd += ["--t30", repr(float(t30))]
    if news_path is not None:
        cmd += ["--news", str(news_path)]
    if offline_filings is not None:
        cmd += ["--offline", str(offline_filings)]
    env = dict(os.environ)
    if sec is not None:
        try:
            env["SEC_USER_AGENT"] = _user_agent(sec)
        except DossierError:
            pass  # 離線模式不需要；連網模式由 skill 自行檢查並失敗
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=SKILL_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired as e:
        raise ReviewError(f"review_due.py 逾時（{SKILL_TIMEOUT_S} 秒）") from e
    if p.returncode != 0:
        raise ReviewError(f"review_due.py 失敗（{p.returncode}）：{(p.stderr or p.stdout)[-1500:]}")

    # 7. 讀 due.json
    due = json.loads(due_path.read_text(encoding="utf-8"))
    if due.get("signal_version") != SIGNAL_VERSION or due.get("asof") != asof:
        raise ReviewError(f"due.json 版本或決策日不符：{due.get('signal_version')!r} / {due.get('asof')!r}")
    rows = due.get("results") or []
    summary.update({
        "counts": dict(Counter(r["action"] for r in rows)),
        "due_path": str(due_path),
        "jobs": {},
    })

    # 8. due_only：只回傳訊號摘要
    if due_only:
        return None

    # 9. 建工作：每檔至多一個起始工作
    entries_by_t = {e["ticker"]: e for e in entries}
    pstate = _portfolio_state(cfg, conn)
    write_view(conn, paths, run_id, asof, _held_closes(conn, alpaca, asof, last_px), rates, fred)
    pe = get_frozen(paths, asof)
    router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
    engine = Engine(conn, router, paths, cfg)
    engine.ctx.sec = sec
    engine.start_run(run_id, "review", asof)  # run 已於 run_review 建立，這裡只是保險
    price_no_cap: list[str] = []
    for r in rows:
        t, action = r["ticker"], r["action"]
        triage = r.get("triage") or []
        if triage:
            jtype = "TRIAGE"
        elif action == "FULL":
            jtype = "DOSSIER"
        elif action == "THESIS":
            jtype = "THESIS"
        elif action == "PRICE":
            if (paths.state / "cap_inputs" / f"{t}.json").is_file():
                jtype = "PRICE"
            else:
                jtype = "DOSSIER"
                price_no_cap.append(t)
        else:  # NONE 且無初判項目：不建工作，沿用上次結論
            continue
        e = entries_by_t.get(t, {})
        thesis = r.get("thesis_file")
        params = base_params(last_px.get(t) or {}, rates,
                             {"ticker": t, "note": "觸發檢查：非初選交接"}, pstate, pe)
        params.update({
            "holding": r.get("status") == "HOLDING",
            "status": r.get("status"),
            "cik": e.get("cik"),
            "reasons": r.get("reasons") or [],
            "triage": triage,
            "kill_criteria": r.get("kill_criteria") or [],
            "kill_criteria_due": e.get("kill_criteria_due") or [],
            "thesis_file": str(thesis) if thesis else None,
            "last_review_date": r.get("last_review_date"),
            "base_action": action,
        })
        spec = JobSpec(job_id=f"{run_id}:{t}:{jtype}:1", run_id=run_id, job_type=jtype, ticker=t, asof=asof,
                       params=params)
        if engine.add_job(spec):
            summary["jobs"][t] = jtype
    summary["price_no_cap_input"] = price_no_cap

    # 10–11. 跑引擎
    return engine.run(run_id)
