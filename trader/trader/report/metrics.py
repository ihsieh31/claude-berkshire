"""儀表板指標（M6b，plan_dashboard.md §3）：網頁與小儀表板共用，算法只在這裡。全部只讀資料庫，不連網。

- 報酬率：時間加權（TWR）。首日以「淨值 ÷ 首日前累計入金」起算，之後每日 (淨值 − 當日入金) ÷ 前日淨值。
  只有初始入金時等於「淨值 ÷ 累計入金 − 1」。外部資金只認 cash_ledger 的 deposit；adjust（人工調帳、
  公司行動現金）視為帳戶內損益。
- SPY 同期：首個淨值日起的總報酬，股利於除息日以收盤價再投入：I_t = I_{t−1} × (close_t + div_t) ÷ close_{t−1}。
  股利取自 nav_daily.data.benchmark_dividend（每日流程由 Alpaca 公司行動寫入）；基準收盤缺值的日子沿用前值。
- 收益：總收益 = 淨值 − 累計入金 = 未實現 + 股利（已入帳＋應收）+ 已實現及其他（賣出損益、手續費、調帳）。
- 現金比：現金 ÷ 淨值（應收股利不算現金）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..jobs import approvals as A

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


def summary(conn) -> dict[str, Any]:
    """小儀表板全部數據（網頁總覽共用）。"""
    rows = _nav_rows(conn)
    appr = A.summary(conn)
    approvals = {**appr, "label": A.label(appr) if appr["n_pending"] else None}
    n_anom = conn.execute("SELECT COUNT(*) FROM anomalies WHERE status='open'").fetchone()[0]
    if not rows:
        return {"empty": True, "as_of": None, "approvals": approvals, "open_anomalies": n_anom}
    last = rows[-1]
    nav = float(last["nav"])
    s = series(conn)
    acct_ret = s[-1]["account_index"] - 1
    spy_ret = (s[-1]["spy_index"] - 1) if s[-1]["spy_index"] is not None else None
    deposits = sum(a for _, a in _deposits(conn))
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
        "approvals": approvals,
        "open_anomalies": n_anom,
    }


# ---------------------------------------------------------------- 明細頁

def _latest_log(conn, ticker: str) -> dict:
    r = conn.execute("SELECT data FROM decisions WHERE ticker=? ORDER BY decision_date DESC, created_at DESC, "
                     "rowid DESC LIMIT 1", (ticker,)).fetchone()
    return json.loads(r["data"]) if r and r["data"] else {}


def holdings(conn) -> list[dict]:
    from ..portfolio.sizing import add_blocked
    rows = _nav_rows(conn)
    last = rows[-1] if rows else {"nav": 0.0, "data": {}, "date": None}
    paused = {r["ticker"] for r in conn.execute(
        "SELECT DISTINCT ticker FROM anomalies WHERE status='open' AND pause_trading=1 AND ticker IS NOT NULL")}
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
    return {
        "orders": q("SELECT * FROM orders{where} ORDER BY created_date DESC, order_id DESC", "created_date"),
        "fills": q("SELECT * FROM fills{where} ORDER BY fill_date DESC, fill_id DESC", "fill_date"),
        "cash": [c for c in q("SELECT * FROM cash_ledger{where} ORDER BY date DESC, entry_id DESC", "date", None)
                 if not ticker or c["ref"] in oids or str(c["ref"] or "").startswith(f"CA:{ticker}:")],
        "corporate_actions": q("SELECT * FROM corporate_actions{where} ORDER BY effective_date DESC", "effective_date"),
    }


def analyses(conn, ticker: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT decision_id, ticker, decision_date, run_id, signal, reason_code, data FROM decisions"
    args: list = []
    if ticker:
        sql += " WHERE ticker=?"
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
                    "reason": log.get("reason_one_liner") or log.get("reason")})
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
    """模型用量：每月 × 等級。"""
    return [dict(r) for r in conn.execute(
        "SELECT substr(created_at,1,7) AS month, tier, COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens,"
        " COALESCE(SUM(output_tokens),0) AS output_tokens, ROUND(COALESCE(SUM(cost_usd),0),4) AS cost_usd,"
        " ROUND(COALESCE(SUM(latency_s),0),1) AS latency_s FROM llm_calls GROUP BY month, tier ORDER BY month DESC, tier")]


def anomalies(conn) -> dict:
    return {"open": [dict(r) for r in conn.execute("SELECT * FROM anomalies WHERE status='open' ORDER BY anomaly_id")],
            "pending_corp_actions": [dict(r) for r in conn.execute(
                "SELECT * FROM corporate_actions WHERE status IN ('pending_human','anomaly') ORDER BY effective_date")]}
