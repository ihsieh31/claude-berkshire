"""儀表板指標（M6b，plan_dashboard.md §3）：網頁與小儀表板共用，算法只在這裡。全部只讀資料庫，不連網。

- 報酬率：時間加權（TWR）。首日以「淨值 ÷ 首日前累計入金」起算，之後每日 (淨值 − 當日入金) ÷ 前日淨值。
  只有初始入金時等於「淨值 ÷ 累計入金 − 1」。外部資金只認 cash_ledger 的 deposit；adjust（人工調帳、
  公司行動現金）視為帳戶內損益。
- SPY 同期：首個淨值日起的總報酬，股利於除息日以收盤價再投入：I_t = I_{t−1} × (close_t + div_t) ÷ close_{t−1}。
  股利取自 nav_daily.data.benchmark_dividend（每日流程由 Alpaca 公司行動寫入）；基準收盤缺值的日子沿用前值。
- 收益：總收益 = 淨值 − 已入帳入金（最後淨值日以前；之後的入金列 pending_deposits，A3-M8C2-02） = 未實現 + 股利（已入帳＋應收）+ 已實現及其他（賣出損益、手續費、調帳）。
- 現金比：現金 ÷ 淨值（應收股利不算現金）。
- 相關群：持股（不含未成交買單）依外部變數分群，取 ≥2 檔群中合計權重最大者（correlation.portfolio_groups，M8-D10-07）。
- 排程健康（summary 給 state 目錄時）：scheduler.json 的 last_tick、放棄清單、最後完成的 daily 與落後交易日數、
  暫停交易的代號數、pending_work（M8-D4-05）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..jobs import approvals as A
from ..ledger import book

NOT_VOIDED = "json_extract(data, '$.voided_at') IS NULL"   # 已作廢的決策不列（`trader decisions void`，M8-D4-02）

RANGES = ("1M", "3M", "YTD", "ALL")


def _r(x: float | None, n: int = 6) -> float | None:
    return None if x is None else round(float(x), n)


def _nav_rows(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT date, nav, cash, positions_value, n_positions, benchmark_close, data "
                          "FROM nav_daily ORDER BY date").fetchall():
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d["data"] else {}
        out.append(d)
    return out


def _deposits(conn) -> list[tuple[str, float]]:
    return [(r["date"], float(r["amount"])) for r in
            conn.execute("SELECT date, amount FROM cash_ledger WHERE kind='deposit' ORDER BY date, entry_id")]


def series(conn) -> list[dict]:
    """每個淨值日：{date, nav, account_index, spy_index}（index 以首日為 1；SPY 在首個有收盤的日子起算）。"""
    rows = _nav_rows(conn)
    if not rows:
        return []
    deps = _deposits(conn)
    out: list[dict] = []
    base = sum(a for d, a in deps if d <= rows[0]["date"])
    acct = rows[0]["nav"] / base if base > 0 else 1.0
    spy = None
    prev_close = None
    for i, r in enumerate(rows):
        if i > 0:
            flow = sum(a for d, a in deps if rows[i - 1]["date"] < d <= r["date"])
            prev_nav = rows[i - 1]["nav"]
            if prev_nav > 0:
                acct *= (r["nav"] - flow) / prev_nav
        close = r["benchmark_close"]
        div = float(r["data"].get("benchmark_dividend") or 0.0)
        if close is not None and close > 0:
            if prev_close is None:
                spy = 1.0
            else:
                spy *= (close + div) / prev_close
            prev_close = close
        out.append({"date": r["date"], "nav": r["nav"], "account_index": acct, "spy_index": spy})
    return out


def chart(conn, rng: str = "ALL") -> dict:
    """走勢圖：視窗內帳戶與 SPY 都換算成視窗首日 = 100。"""
    if rng not in RANGES:
        raise ValueError(f"區間須為 {'、'.join(RANGES)}")
    s = series(conn)
    if not s:
        return {"range": rng, "points": []}
    last = date.fromisoformat(s[-1]["date"])
    start = {"1M": last - timedelta(days=30), "3M": last - timedelta(days=91),
             "YTD": date(last.year, 1, 1), "ALL": date.min}[rng].isoformat()
    w = [x for x in s if x["date"] >= start] or s[-1:]
    a0 = w[0]["account_index"]
    s0 = next((x["spy_index"] for x in w if x["spy_index"] is not None), None)
    pts = [{"date": x["date"],
            "account": _r(x["account_index"] / a0 * 100, 4) if a0 else None,
            "spy": _r(x["spy_index"] / s0 * 100, 4) if (s0 and x["spy_index"] is not None) else None}
           for x in w]
    return {"range": rng, "points": pts}


def _positions(conn, prices: dict, nav: float) -> list[dict]:
    out = []
    for r in conn.execute("SELECT ticker, qty, avg_cost, first_buy_date FROM positions WHERE qty > 0 ORDER BY ticker"):
        px = prices.get(r["ticker"])
        priced = isinstance(px, (int, float)) and px > 0
        px = float(px) if priced else float(r["avg_cost"])
        value = r["qty"] * px
        out.append({"ticker": r["ticker"], "qty": int(r["qty"]), "avg_cost": float(r["avg_cost"]), "price": px,
                    "price_missing": not priced, "value": round(value, 2),
                    "cost_value": round(r["qty"] * float(r["avg_cost"]), 2),
                    "weight": _r(value / nav) if nav > 0 else None,
                    "return": _r(px / r["avg_cost"] - 1) if r["avg_cost"] else None,
                    "first_buy_date": r["first_buy_date"]})
    return out


def health(conn, state: Path) -> dict[str, Any]:
    """排程健康（M8-D4-05）。落後交易日數＝最後完成的 daily 之後、到紐約昨天為止的交易日數（規則日曆，不連網）。
    warn：落後 ≥2 交易日、有放棄的排程工作、有暫停交易的代號，或較早的每日收盤未完成而較晚日期已完成（跳日，
    daily_gap；A3-V3-3-03：既有資料或 --force 造成，MAX(asof) 看不出來）時給一行警示。全域人工暫停另起一句；
    已入金或有淨值但從未完成 daily 也警示（A3-M8C2-12）。"""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    from ..calendar import TradingCalendar
    from ..scheduler import STATE_FILE, pending_work
    try:
        st = json.loads((Path(state) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    gave = [k for k in st.get("gave_up") or [] if not str(k).startswith("daily:")]   # daily 永不放棄（D08）
    last = conn.execute("SELECT MAX(asof) FROM runs WHERE kind IN ('daily_close', 'daily_skip') "
                        "AND status='done'").fetchone()[0]   # daily-D:ca 等子工作不算（M7-S2-N01）
    lag = None
    if last:
        y = (datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=1)).isoformat()
        start = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
        lag = len(TradingCalendar().trading_days(start, y)) if start <= y else 0
    gap = conn.execute("SELECT MIN(asof) FROM runs WHERE kind='daily_close' AND status<>'done' AND asof<?",
                       (last,)).fetchone()[0] if last else None
    n_paused = conn.execute("SELECT COUNT(DISTINCT COALESCE(ticker, '*')) FROM anomalies "
                            "WHERE status='open' AND pause_trading=1").fetchone()[0]
    # 全域人工暫停（ticker IS NULL）另起一句，不算成「1 檔」；有入金／淨值卻從未完成 daily 也要警示（A3-M8C2-12）
    global_pause = conn.execute("SELECT 1 FROM anomalies WHERE status='open' AND pause_trading=1 AND ticker IS NULL "
                                "AND kind=? LIMIT 1", (book.MANUAL_PAUSE,)).fetchone() is not None
    n_paused_t = conn.execute("SELECT COUNT(DISTINCT ticker) FROM anomalies WHERE status='open' AND pause_trading=1 "
                              "AND ticker IS NOT NULL").fetchone()[0]
    never_ran = not last and conn.execute(
        "SELECT EXISTS (SELECT 1 FROM nav_daily) OR EXISTS (SELECT 1 FROM cash_ledger WHERE kind='deposit')").fetchone()[0]
    out = {"last_tick": st.get("last_tick"), "last_daily": last, "daily_lag_days": lag, "gave_up": gave,
           "n_paused": n_paused, "pending_work": pending_work(conn), "daily_gap": gap}
    warn = []
    if never_ran:
        warn.append("尚無完成的 daily（帳本已入金或有淨值，但每日收盤從未完成）")
    if gap:
        warn.append(f"每日收盤 {gap} 未完成（後面日期 {last} 已完成）：帳本跳過了這天，需人工處理")
    if lag is not None and lag >= 2:
        warn.append(f"每日收盤落後 {lag} 個交易日（最後完成 {last}）")
    if gave:
        warn.append(f"排程已放棄：{'、'.join(gave)}（`trader scheduler reset 任務` 重來）")
    if global_pause:
        warn.append("全部代號人工暫停中（`trader ledger adjust --unpause-all --note 理由` 解除）")
    if n_paused_t:
        warn.append(f"{n_paused_t} 檔暫停交易（見異常頁）")
    out["warning"] = "；".join(warn) or None
    return out


def _max_group(conn, prices: dict) -> dict[str, Any]:
    """持股的最大相關群（≥2 檔）合計權重與成員；外部變數未知的持股（M8-D10-07）。算不出來回空值，不讓總覽失敗。"""
    from ..portfolio import correlation as C
    try:
        g = C.portfolio_groups(conn, {t: float(v) for t, v in prices.items() if isinstance(v, (int, float))},
                               include_committed=False)
    except Exception:  # noqa: BLE001 - 詞彙檔缺等：只少這一欄
        return {"max_group_weight": None, "max_group": None, "unknown_drivers": None}
    multi = [x for x in g["groups"] if len(x["members"]) >= 2]
    top = max(multi, key=lambda x: x["weight"], default=None)
    return {"max_group_weight": _r(g["max_group_weight"]) if multi else None,
            "max_group": {"members": top["members"], "variables": top["variables"]} if top else None,
            "unknown_drivers": g["unknown_drivers"]}


def summary(conn, state: Path | None = None) -> dict[str, Any]:
    """小儀表板全部數據（網頁總覽共用）。state：給了才算排程健康（health）。"""
    rows = _nav_rows(conn)
    appr = A.summary(conn)
    approvals = {**appr, "label": A.label(appr) if appr["n_pending"] else None}
    n_anom = conn.execute("SELECT COUNT(*) FROM anomalies WHERE status='open'").fetchone()[0]
    extra = {"health": health(conn, state)} if state is not None else {}
    if not rows:
        return {"empty": True, "as_of": None, "approvals": approvals, "open_anomalies": n_anom, **extra}
    last = rows[-1]
    nav = float(last["nav"])
    s = series(conn)
    acct_ret = s[-1]["account_index"] - 1
    spy_ret = (s[-1]["spy_index"] - 1) if s[-1]["spy_index"] is not None else None
    # 只計已入帳（≤ 最後淨值日）的入金：daily 後、下一 daily 前的入金尚未進淨值，另列待入帳（A3-M8C2-02）
    deps = _deposits(conn)
    deposits = sum(a for d, a in deps if d <= last["date"])
    pending_deposits = sum(a for d, a in deps if d > last["date"])
    pos = _positions(conn, last["data"].get("prices") or {}, nav)
    unreal = sum(p["value"] - p["cost_value"] for p in pos)
    divs = conn.execute("SELECT COALESCE(SUM(amount),0) FROM cash_ledger WHERE kind IN "
                        "('dividend','dividend_receivable')").fetchone()[0]
    total = nav - deposits
    top = sorted(pos, key=lambda p: p["value"], reverse=True)[:3]
    return {
        "empty": False,
        "as_of": last["date"],
        "first_date": rows[0]["date"],
        "nav": nav,
        "deposits": round(deposits, 2),
        "pending_deposits": round(pending_deposits, 2),
        "return": _r(acct_ret),
        "spy_return": _r(spy_ret),
        "excess_pp": _r((acct_ret - spy_ret) * 100, 4) if spy_ret is not None else None,
        "pnl": {"total": round(total, 2), "unrealized": round(unreal, 2), "dividends": round(float(divs), 2),
                "realized_other": round(total - unreal - float(divs), 2)},
        "cash": float(last["cash"]),
        "receivables": float(last["data"].get("receivables") or 0.0),
        "cash_ratio": _r(last["cash"] / nav) if nav > 0 else None,
        "n_positions": len(pos),
        "top3": [{k: p[k] for k in ("ticker", "weight", "return")} for p in top],
        "missing_close": last["data"].get("missing_close") or [],
        **_max_group(conn, last["data"].get("prices") or {}),
        "approvals": approvals,
        "open_anomalies": n_anom,
        **extra,
    }


# ---------------------------------------------------------------- 明細頁

def _latest_log(conn, ticker: str) -> dict:
    r = conn.execute(f"SELECT data FROM decisions WHERE ticker=? AND {NOT_VOIDED} AND "
                     "COALESCE(json_extract(data, '$.review_type'), '') NOT IN ('MANUAL', 'PORTFOLIO') "
                     "ORDER BY decision_date DESC, created_at DESC, rowid DESC LIMIT 1", (ticker,)).fetchone()
    return json.loads(r["data"]) if r and r["data"] else {}


def holdings(conn) -> list[dict]:
    from ..portfolio.sizing import add_blocked
    rows = _nav_rows(conn)
    last = rows[-1] if rows else {"nav": 0.0, "data": {}, "date": None}
    paused = book.paused(conn)   # 含人工全域暫停（M8-D4-03）
    out = []
    for p in _positions(conn, last["data"].get("prices") or {}, float(last["nav"] or 0.0)):
        t = p["ticker"]
        rr = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
        reg = json.loads(rr["data"]) if rr and rr["data"] else {}
        kills = [k.get("due") for k in reg.get("kill_criteria_due") or [] if isinstance(k, dict) and k.get("due")]
        log = _latest_log(conn, t)
        out.append({**p,
                    "registry_status": rr["status"] if rr else None,
                    "grade": reg.get("grade"),
                    "auto_buy_cap": reg.get("auto_buy_cap"),
                    "sell_review_above": reg.get("sell_review_above"),
                    "absurd_price_line": reg.get("absurd_price_line"),
                    "target_weight": reg.get("target_weight"),
                    "build_plan": reg.get("build_plan"),
                    "thesis_one_liner": log.get("reason_one_liner") or log.get("reason"),
                    "next_full_review_due": reg.get("next_full_review_due"),
                    "next_kill_check": min(kills) if kills else None,
                    "paused": t in paused,
                    "pause_reasons": [f"#{a['anomaly_id']} {a['kind']}" for a in conn.execute(
                        "SELECT anomaly_id, kind FROM anomalies WHERE status='open' AND pause_trading=1 "
                        "AND (ticker=? OR ticker IS NULL) ORDER BY anomaly_id", (t,))] if t in paused else [],
                    "add_blocked": add_blocked(conn, t, last["date"]) if last["date"] else None})
    return out


def trades(conn, ticker: str | None = None, start: str | None = None, end: str | None = None) -> dict:
    def q(sql: str, dcol: str, tcol: str | None = "ticker") -> list[dict]:
        cond, args = [], []
        if ticker and tcol:
            cond.append(f"{tcol}=?")
            args.append(ticker)
        if start:
            cond.append(f"{dcol}>=?")
            args.append(start)
        if end:
            cond.append(f"{dcol}<=?")
            args.append(end)
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        return [dict(r) for r in conn.execute(sql.format(where=where), args)]
    oids = {r["order_id"] for r in conn.execute("SELECT order_id FROM orders WHERE ticker=?", (ticker,))} if ticker else set()
    # 人工下單（D12，`trader trade`）：委託與成交標 manual
    manual = {r[0] for r in conn.execute(
        "SELECT o.order_id FROM orders o JOIN decisions d ON d.decision_id = o.decision_id "
        "WHERE json_extract(d.data, '$.review_type') = 'MANUAL'")}
    return {
        "orders": [{**o, "manual": o["order_id"] in manual}
                   for o in q("SELECT * FROM orders{where} ORDER BY created_date DESC, order_id DESC", "created_date")],
        "fills": [{**f, "manual": f["order_id"] in manual}
                  for f in q("SELECT * FROM fills{where} ORDER BY fill_date DESC, fill_id DESC", "fill_date")],
        "cash": [c for c in q("SELECT * FROM cash_ledger{where} ORDER BY date DESC, entry_id DESC", "date", None)
                 if not ticker or c["ref"] in oids or str(c["ref"] or "").startswith(f"CA:{ticker}:")],
        "corporate_actions": q("SELECT * FROM corporate_actions{where} ORDER BY effective_date DESC", "effective_date"),
    }


def _provenance(v: str | None) -> Any:
    if v is None:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return v


def analyses(conn, ticker: str | None = None, limit: int = 200) -> list[dict]:
    sql = ("SELECT decision_id, ticker, decision_date, run_id, signal, reason_code, data, provenance FROM decisions "
           f"WHERE {NOT_VOIDED}")
    args: list = []
    if ticker:
        sql += " AND ticker=?"
        args.append(ticker)
    sql += " ORDER BY decision_date DESC, created_at DESC, rowid DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(sql, args):
        log = json.loads(r["data"]) if r["data"] else {}
        out.append({"decision_id": r["decision_id"], "ticker": r["ticker"], "date": r["decision_date"],
                    "run_id": r["run_id"], "action": log.get("action") or r["signal"],
                    "review_type": log.get("review_type"), "reason_code": r["reason_code"],
                    "stopped_at_gate": log.get("stopped_at_gate"), "grade": log.get("grade"),
                    "reason": log.get("reason_one_liner") or log.get("reason"),
                    "framework_notes": log.get("framework_notes") or [],   # 框架改寫結論的理由（M8-D5-01）
                    "provenance": _provenance(r["provenance"])})   # NULL＝A 機原生；他機匯入由 hark/export.sh 填（D51，A3-M8B-05）
    return out


EXCLUDE_DAYS = 365   # REJECT／TOO_HARD 自最後完整分析日起排除 12 個月（初選 X5）


def watchlist(conn) -> list[dict]:
    """登錄表非 HOLDING 列（觀察名單與排除名單，M8-D5-02）。現價沿用最後一筆淨值的收盤，不連網：先取
    watch_prices（daily 記的觀察名單收盤，不計淨值；A3-M8C2-03），再取持股估值價。"""
    rows = _nav_rows(conn)
    d = rows[-1]["data"] if rows else {}
    prices = {**(d.get("prices") or {}), **(d.get("watch_prices") or {})}
    out = []
    for r in conn.execute("SELECT ticker, status, data FROM registry WHERE status <> 'HOLDING' ORDER BY status, ticker"):
        reg = json.loads(r["data"]) if r["data"] else {}
        last = reg.get("last_full_review_date")
        excl, excl_note = None, None
        # 與初選 X5／X1 同口徑（M8-R5-01）：REJECT／TOO_HARD 或 NOT_THIS_SKILL；他機（hark_b）結論不排除。
        # exclude_until＝排除期滿日（當天起可再被初選選入）
        if (r["status"] in ("REJECT", "TOO_HARD") or reg.get("last_reason_code") == "NOT_THIS_SKILL") \
                and isinstance(last, str):
            if reg.get("origin") == "hark_b":
                excl_note = "他機（Hark）結論，初選不排除；再被選入即重新完整分析"
            else:
                try:
                    excl = (date.fromisoformat(last[:10]) + timedelta(days=EXCLUDE_DAYS)).isoformat()
                except ValueError:
                    excl = None
        px = prices.get(r["ticker"])
        out.append({"ticker": r["ticker"], "status": r["status"], "grade": reg.get("grade"),
                    "auto_buy_cap": reg.get("auto_buy_cap"), "sell_review_above": reg.get("sell_review_above"),
                    "price": float(px) if isinstance(px, (int, float)) else None,
                    "last_full_review_date": last, "exclude_until": excl, "exclude_note": excl_note,
                    "next_full_review_due": reg.get("next_full_review_due"),
                    "reject_basis": reg.get("reject_basis")})
    return out


REPORT_FILES = ("report.md", "report_A.md")


def report(conn, work: Path, decision_id: str) -> dict | None:
    """單筆決策的報告（work/{run_id}/{ticker}/report.md，沒有則 report_A.md）與訊號卡。找不到決策 → None。"""
    r = conn.execute("SELECT ticker, run_id, data FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
    if r is None:
        return None
    text, name = None, None
    if r["run_id"]:
        base = Path(work).resolve()
        td = (base / r["run_id"] / r["ticker"]).resolve()
        if td.is_relative_to(base):  # 路徑來自資料庫；仍確保不跳出 work
            for n in REPORT_FILES:
                if (td / n).is_file():
                    text, name = (td / n).read_text(encoding="utf-8"), n
                    break
    sig = conn.execute("SELECT data FROM signals WHERE decision_id=?", (decision_id,)).fetchone()
    return {"decision_id": decision_id, "ticker": r["ticker"], "log": json.loads(r["data"]) if r["data"] else {},
            "signal_card": json.loads(sig["data"]) if sig and sig["data"] else None,
            "report_file": name, "report_md": text}


def usage(conn) -> list[dict]:
    """模型用量：每月 × 等級 × 後端（M8-D5-07）。estimated：後端不回報實際 token／費用（A.MEASURED_BACKENDS 以外，
    例如 hark_queue、fake），數字是估算。failed：ok=0 的呼叫數。"""
    out = [dict(r) for r in conn.execute(
        "SELECT substr(created_at,1,7) AS month, tier, COALESCE(backend,'') AS backend, COUNT(*) AS calls,"
        " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failed, COALESCE(SUM(input_tokens),0) AS input_tokens,"
        " COALESCE(SUM(output_tokens),0) AS output_tokens, ROUND(COALESCE(SUM(cost_usd),0),4) AS cost_usd,"
        " ROUND(COALESCE(SUM(latency_s),0),1) AS latency_s FROM llm_calls GROUP BY month, tier, backend"
        " ORDER BY month DESC, tier, backend")]
    for r in out:
        r["estimated"] = r["backend"] not in A.MEASURED_BACKENDS
    return out


# 異常類型 → (中文名, 意思)（M8-D5-05）。沒列的類型網頁顯示原碼，處理方式見通用說明。
ANOMALY_KINDS = {
    "ledger_stalled": ("帳本停擺", "每日收盤連續失敗：撮合、賣出、淨值都停在那天，排程每次新收盤後重試"),
    "scheduler_error": ("排程本身出錯", "排程連續出錯，所有排程工作都沒在跑"),
    "scheduler_task_failed": ("排程工作連續失敗", "排程工作連續失敗 3 次：觀察名單檢查（watch）下一個觀察週期自動再試、"
                              "成功即結案（D81）；其他工作排程不再自動重試這項"),
    "scheduler_no_last_screen": ("找不到上次初選日", "排程不會自動做季度初選"),
    "job_failed": ("分析工作失敗", "模型工作失敗（傳輸、額度、輸出驗證等），該檔本輪沒有完整結論"),
    "job_conservative_fallback": ("保守處理", "工作失敗後已寫保守結論（WATCH／HOLD＋停止加碼），只供追蹤"),
    "budget_daily_exhausted": ("每日預算用完", "分析暫停在預算上限，不會自動續跑（D44）"),
    "switch_candidate": ("換股候選", "持股已滿，有新的買進候選：換不換由你決定"),
    "corr_group_trim": ("相關群減碼建議", "同群共同情境損失超過 10%：系統只建議、不自動賣"),
    "hold_review_needs_human": ("持股複查需人工", "模型判賣出，框架覆核兩次未通過改 HOLD＋停止加碼"),
    "trigger_fail_streak": ("觸發檢查連續失敗", "同檔連 3 次觸發檢查失敗（daily 觸發檢查失敗，或 SEC 申報清單抓取失敗）；此提醒本身不暫停交易，原因見明細，需人工排查"),
    "weight_over_cap": ("權重超過等級上限", "因上漲超限：不賣、停止加碼"),
    "buy_deferred_review_failed": ("買進延後", "當日觸發檢查或公司行動工作失敗，買進／加碼延後到下一次每日收盤重核（原因見明細）；之後重核完成即自動結案"),
    "unregistered_holding": ("持股不在登錄表", "帳上有股數但登錄表沒有這檔"),
    "cik_succession": ("CIK 疑似更替", "SEC 代號對應的公司可能已更換（併購、改名）"),
    "corr_link_failed": ("相關群連結失敗", "回寫外部變數／相關群時出錯"),
    "cap_input_replace_failed": ("上限輸入替換失敗", "買價上限的輸入值無法替換，上限可能沿用舊值"),
    "corp_action_job_failed": ("公司行動工作失敗", "公司行動解析失敗，事件尚未入帳；未解除前該檔暫停交易（--unpause 後只解除暫停、"
                               "仍未結案）。下次 daily 會重派，入帳後自動結案"),
    "screen_review": ("初篩待複核", "初篩有待人工看的項目（待複核刪除、過期或被忽略的撤銷、資料不足、警告），"
                      "只寫在 summary.json；看完後結案（A3-M1C-06）"),
    "review_check_error": ("觸發檢查出錯", "觸發檢查程式對這檔拋例外：本次全部觸發未檢查、沒建工作，不等於沒有變化；"
                           "原因見明細，需人工排查（A3-M2E-02）"),
    book.MANUAL_PAUSE: ("人工暫停", "你手動暫停了交易"),
}
LEDGER_HELP = ("帳本類：人工核對後用一個指令完成 `trader ledger adjust --ticker 代號 …（--qty／--avg-cost／--cash） "
               "--note 理由`，同一指令加 `--resolve ID`（結案該異常）或 `--unpause 代號`（解除該檔暫停並結案暫停異常；"
               "公司行動工作失敗只解除暫停、照樣重派，除非同一指令改了該檔股數＝已人工入帳才結案）；"
               "有待人工公司行動列再加 `--resolve-corp-action 編號`")


def anomaly_hint(a: dict) -> str | None:
    """異常的下一步指令（D12、M8-D4-03、M7-D4-04、M8-D5-05）；沒有專屬說明的類型回 None（網頁顯示通用帳本說明）。"""
    i, t, k = a.get("anomaly_id"), a.get("ticker") or "", a.get("kind")
    try:
        d = a.get("detail")
        d = json.loads(d) if isinstance(d, str) else (d or {})
        d = d if isinstance(d, dict) else {}
    except ValueError:
        d = {}
    resolve = f"`trader ledger adjust --resolve {i} --note 理由`"
    if k == "job_failed":
        rid, jid = d.get("run_id") or "<run>", d.get("job_id") or "<job>"
        return (f"重跑（會呼叫模型，完成後自動結案）：`trader jobs retry --run-id {rid} --job-id {jid}`；"
                f"不要了：`trader jobs abandon --run-id {rid}" + (f" --ticker {t}" if t else "") + " --note 理由`。"
                f"不要只用 --resolve：那只關掉異常，工作仍是失敗")
    if k == "scheduler_task_failed":
        task = d.get("task") or "<任務>"
        if str(task).startswith("watch:"):   # D81：watch 不永久放棄（A3-M7A-08）
            return (f"觀察名單檢查連續失敗：排程會在下一個觀察週期自動再試，成功即自動結案。可先看 "
                    f"{d.get('log') or 'state/logs/scheduler-tasks.log'} 修好原因；要立刻重試用 `trader scheduler reset {task}`")
        return (f"看 {d.get('log') or 'state/logs/scheduler-tasks.log'} 修好原因後 `trader scheduler reset {task}`，"
                f"排程才會重試；再 {resolve} 結案（只 --resolve 不會恢復排程）")
    if k == "ledger_stalled":
        task = f" {d['task']}" if d.get("task") else ""
        return (f"看 {d.get('log') or 'state/logs/scheduler-tasks.log'} 修好原因後 `trader scheduler reset{task}` 立即重試；"
                "該日 daily 成功即自動結案")
    if k == "scheduler_error":
        return f"看 {d.get('log') or 'state/logs/scheduler.log'}；修好、排程恢復後 {resolve}"
    if k == "budget_daily_exhausted":
        rid = d.get("run_id") or "<run>"
        return (f"待同意頁按「續跑」或 `trader jobs resume --run-id {rid}`（會呼叫模型）；不要了 "
                f"`trader jobs abandon --run-id {rid}`；之後 {resolve}")
    if k == "hold_review_needs_human":
        return (f"看分析頁該檔報告後決定。要賣：`trader trade sell --ticker {t} --qty 股數 --anomaly {i} --note 理由`；"
                f"不賣：{resolve}")
    if k == "corp_action_job_failed":   # A3-V3-3-02：--unpause 只解除暫停、不結案；--resolve 結案後不再重派
        return ("等下次 daily 重派（成功入帳即自動結案）。急著交易：" +
                (f"`trader ledger adjust --unpause {t} --note 理由`" if t else "`--unpause 代號`") +
                f"（只解除暫停，事件照樣重派）；已人工入帳：同一指令改股數＋`--unpause {t or '代號'}` 會一併結案，"
                f"或另用 {resolve}（結案後不再重派）。已改股數卻沒結案時，重派不自動入帳、改轉人工")
    if k == "job_conservative_fallback":
        return f"只供追蹤；看過後 {resolve}"
    if k == "scheduler_no_last_screen":
        return "`trader bootstrap --asof 日期`，或換機重做同一批 `--tickers … --mark-screened`；設定後自動結案"
    if k == "switch_candidate":
        return (f"持股已滿：換股由你決定。要換：先 `trader trade sell --ticker 最弱持股 --qty 股數 --anomaly {i} --note 理由`，"
                f"成交後 `trader trade buy --ticker {t} --qty 股數 --limit 價格 --anomaly {i} --note 理由`；"
                f"不換：`trader ledger adjust --resolve {i} --note 理由`")
    if k == "corr_group_trim":
        return (f"P-32 相關群減碼建議（系統不自動賣）。要減：`trader trade sell --ticker {t} --qty 股數 --anomaly {i} "
                f"--note 理由`；不減：`trader ledger adjust --resolve {i} --note 理由`")
    if k == book.MANUAL_PAUSE:
        return ("解除：" + (f"`trader ledger adjust --unpause {t} --note 理由`" if t else
                            "`trader ledger adjust --unpause-all --note 理由`"))
    return None


def _meaning(r: dict) -> str | None:
    """異常的意思；公司行動工作失敗已用 --unpause 解除暫停（pause_trading＝0）時改寫，不再說「該檔暫停交易」。"""
    if r.get("kind") == "corp_action_job_failed" and not r.get("pause_trading"):
        return "公司行動解析失敗，事件尚未入帳；已解除暫停（--unpause），可照常交易。下次 daily 會重派，入帳後自動結案"
    return (ANOMALY_KINDS.get(r.get("kind")) or (None, None))[1]


def anomalies(conn) -> dict:
    """未結異常（需人工的附 hint）與待人工公司行動（附結案指令 hint）。"""
    ca = [dict(r) for r in conn.execute(
        "SELECT * FROM corporate_actions WHERE status IN ('pending_human','anomaly') ORDER BY effective_date")]
    for c in ca:
        c["hint"] = (f"人工調帳（`trader ledger adjust --ticker {c['ticker']} …`）完成後，同一指令加 "
                     f"`--unpause {c['ticker']} --resolve-corp-action {c['ca_id']}` 結案")
    rows = [dict(r) for r in conn.execute("SELECT * FROM anomalies WHERE status='open' ORDER BY anomaly_id")]
    rows.sort(key=lambda r: r["kind"] not in ("ledger_stalled", "scheduler_error"))   # 帳本／排程停擺置頂（D08）
    return {"open": [{**r, "kind_name": (ANOMALY_KINDS.get(r["kind"]) or (None, None))[0],
                      "meaning": _meaning(r), "hint": anomaly_hint(r)}
                     for r in rows],
            "pending_corp_actions": ca, "ledger_help": LEDGER_HELP}
