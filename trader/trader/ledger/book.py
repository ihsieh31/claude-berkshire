"""紙上帳本的帳務核心：現金、應收股利、持股、每日淨值與人工調整。

- 金額為美元，現金流以分為單位（round 到 2 位）；股數為整數。
- `transaction` 最外層開交易、成功提交、失敗回滾；巢狀呼叫併入外層交易。
- 本模組與 broker.py 的寫入函式不呼叫 db.insert／db.upsert（它們會自行 commit，會破壞外層交易）。
"""
from __future__ import annotations

import math
from datetime import date as _date
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from trader import db
from trader.data.universe import normalize_ticker


class LedgerError(Exception):
    """帳本規則違反或輸入不合法。"""


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """原子寫入區。已在交易中則併入外層（由外層決定提交或回滾）。"""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def write_row(conn: sqlite3.Connection, table: str, row: dict, key: list[str] | None = None) -> None:
    """在目前交易內寫入一列，不 commit。key 不為 None 時為 upsert（與 db.upsert 相同 SQL）。"""
    db._check_ident(table)
    cols = list(row)
    for c in cols:
        db._check_ident(c)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    if key:
        updates = [c for c in cols if c not in key]
        sql += f" ON CONFLICT({', '.join(key)}) "
        sql += ("DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in updates)) if updates else "DO NOTHING"
    conn.execute(sql, [db._encode(row[c]) for c in cols])


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _money(x) -> float:
    return round(float(x), 2)


def ensure_initial_deposit(conn: sqlite3.Connection, initial_cash: float, date: str) -> None:
    """cash_ledger 沒有任何 deposit 時寫入一筆初始入金；已有則不動。"""
    if not _is_number(initial_cash) or initial_cash <= 0:
        raise LedgerError(f"初始資金須為正數：{initial_cash!r}")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM cash_ledger WHERE kind = 'deposit' LIMIT 1").fetchone() is None:
            write_row(conn, "cash_ledger", {
                "date": date, "amount": _money(initial_cash), "kind": "deposit", "ref": "INIT", "note": "初始入金",
            })


def _sum_cash(conn: sqlite3.Connection, where: str) -> float:
    row = conn.execute(f"SELECT COALESCE(SUM(amount), 0) AS s FROM cash_ledger {where}").fetchone()
    return _money(row["s"])


def cash(conn: sqlite3.Connection) -> float:
    """現金：所有非應收股利的現金流總和。"""
    return _sum_cash(conn, "WHERE kind != 'dividend_receivable'")


def receivables(conn: sqlite3.Connection) -> float:
    """應收股利：除息後尚未發放的金額。"""
    return _sum_cash(conn, "WHERE kind = 'dividend_receivable'")


def positions(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT ticker, qty, avg_cost, first_buy_date FROM positions ORDER BY ticker").fetchall()
    return {
        r["ticker"]: {"qty": int(r["qty"]), "avg_cost": float(r["avg_cost"]), "first_buy_date": r["first_buy_date"]}
        for r in rows
    }


def open_orders(conn: sqlite3.Connection, ticker: str | None = None) -> list[dict]:
    sql = "SELECT * FROM orders WHERE status = 'open'"
    args: tuple = ()
    if ticker is not None:
        sql += " AND ticker = ?"
        args = (ticker,)
    sql += " ORDER BY created_date, order_id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def paused(conn: sqlite3.Connection) -> set[str]:
    """有未結異常且 pause_trading=1 的代號。這些代號不撮合、不下新單。"""
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM anomalies WHERE status = 'open' AND pause_trading = 1 AND ticker IS NOT NULL"
    ).fetchall()
    return {r["ticker"] for r in rows}


def committed_buy_value(conn: sqlite3.Connection) -> float:
    """未成交買單的 qty × limit_price 總和（以委託價計）。"""
    return _money(sum(o["qty"] * o["limit_price"] for o in open_orders(conn) if o["side"] == "BUY"))


def _values(conn: sqlite3.Connection, closes: dict | None) -> tuple[dict[str, dict], list[str]]:
    """每檔持股的市價與市值。缺收盤價時以平均成本估值，並回傳缺價代號清單。"""
    closes = closes or {}
    out: dict[str, dict] = {}
    missing: list[str] = []
    for t, p in positions(conn).items():
        px = closes.get(t)
        if not _is_number(px) or px <= 0:
            missing.append(t)
            px = p["avg_cost"]
        out[t] = {**p, "price": float(px), "value": _money(p["qty"] * float(px))}
    return out, sorted(missing)


def equity(conn: sqlite3.Connection, closes: dict[str, float]) -> float:
    """權益 = 現金 + 應收股利 + Σ 股數 × 收盤價（缺價時用平均成本）。"""
    vals, _ = _values(conn, closes)
    return _money(cash(conn) + receivables(conn) + sum(v["value"] for v in vals.values()))


def record_nav(conn: sqlite3.Connection, date: str, closes: dict[str, float], benchmark_close: float | None,
               benchmark_dividend: float | None = None) -> dict:
    """寫入（或更新）當日淨值。data 含 weights、missing_close、receivables、prices（估值所用每股價）、
    benchmark_dividend（基準當日除息的每股現金股利；儀表板算含股利總報酬用）。"""
    vals, missing = _values(conn, closes)
    c, rec = cash(conn), receivables(conn)
    pos_value = _money(sum(v["value"] for v in vals.values()))
    nav = _money(c + rec + pos_value)
    weights = {t: (round(v["value"] / nav, 6) if nav > 0 else 0.0) for t, v in vals.items()}
    row = {
        "date": date,
        "nav": nav,
        "cash": c,
        "positions_value": pos_value,
        "n_positions": len(vals),
        "benchmark_close": benchmark_close,
        "data": {"weights": weights, "missing_close": missing, "receivables": rec,
                 "prices": {t: v["price"] for t, v in vals.items()},
                 "benchmark_dividend": benchmark_dividend},
    }
    with transaction(conn):
        write_row(conn, "nav_daily", row, ["date"])
    return row


def show(conn: sqlite3.Connection, closes: dict[str, float] | None = None) -> dict:
    """帳本摘要：現金、應收、持股（含權重）、未成交委託、未結異常。"""
    vals, missing = _values(conn, closes)
    c, rec = cash(conn), receivables(conn)
    total = _money(c + rec + sum(v["value"] for v in vals.values()))
    pos_rows = [
        {
            "ticker": t,
            "qty": v["qty"],
            "avg_cost": v["avg_cost"],
            "first_buy_date": v["first_buy_date"],
            "price": v["price"],
            "value": v["value"],
            "weight": (round(v["value"] / total, 6) if total > 0 else None),
        }
        for t, v in sorted(vals.items())
    ]
    anomalies = [
        dict(r) for r in conn.execute("SELECT * FROM anomalies WHERE status = 'open' ORDER BY anomaly_id").fetchall()
    ]
    return {
        "cash": c,
        "receivables": rec,
        "equity": total,
        "positions": pos_rows,
        "open_orders": open_orders(conn),
        "open_anomalies": anomalies,
        "missing_close": missing,
    }


def adjust(conn: sqlite3.Connection, *, date: str, cash_delta: float | None = None, ticker: str | None = None,
           set_qty: int | None = None, set_avg_cost: float | None = None, resolve_anomaly: int | None = None,
           unpause: str | None = None, first_buy_date: str | None = None, note: str) -> dict:
    """人工調整。每次呼叫在單一交易內完成；持股變動另寫一筆已解決的 manual_adjust 異常作為稽核紀錄。

    first_buy_date：分拆、併購換得的新股沿用原持股的建倉日（SKILL §6 一年限制），未指定時新建持股用 date。
    """
    if not isinstance(note, str) or not note.strip():
        raise LedgerError("人工調整必須附 note")
    if ticker is not None:
        ticker = normalize_ticker(ticker)
    if (cash_delta is None and set_qty is None and set_avg_cost is None and first_buy_date is None
            and resolve_anomaly is None and unpause is None):
        raise LedgerError("沒有任何調整項目")
    if cash_delta is not None:
        if not _is_number(cash_delta) or _money(cash_delta) == 0:
            raise LedgerError(f"cash_delta 須為非零有限數：{cash_delta!r}")
        cash_delta = _money(cash_delta)
    if first_buy_date is not None:
        try:
            _date.fromisoformat(first_buy_date)
        except (TypeError, ValueError):
            raise LedgerError(f"first_buy_date 須為 YYYY-MM-DD：{first_buy_date!r}") from None
        if first_buy_date > date:
            raise LedgerError(f"first_buy_date {first_buy_date} 晚於調整日 {date}")
    if set_qty is not None or set_avg_cost is not None or first_buy_date is not None:
        if not ticker:
            raise LedgerError("調整持股需指定 ticker")
    if set_qty is not None and (isinstance(set_qty, bool) or not isinstance(set_qty, int) or set_qty < 0):
        raise LedgerError(f"股數須為非負整數：{set_qty!r}")
    if set_avg_cost is not None and (not _is_number(set_avg_cost) or set_avg_cost <= 0):
        raise LedgerError(f"平均成本須為正數：{set_avg_cost!r}")
    if unpause is not None:
        unpause = normalize_ticker(unpause)

    now = db.now_iso()
    result: dict = {"cash_delta": None, "position": None, "resolved": []}
    with transaction(conn):
        if cash_delta is not None:
            write_row(conn, "cash_ledger", {
                "date": date, "amount": cash_delta, "kind": "adjust", "ref": f"ADJ:{date}", "note": note,
            })
            result["cash_delta"] = cash_delta

        if set_qty is not None or set_avg_cost is not None or first_buy_date is not None:
            cur = conn.execute(
                "SELECT qty, avg_cost, first_buy_date FROM positions WHERE ticker = ?", (ticker,)
            ).fetchone()
            before = dict(cur) if cur else None
            qty = set_qty if set_qty is not None else (cur["qty"] if cur else None)
            if qty is None:
                raise LedgerError(f"{ticker} 無持股，須同時指定 set_qty")
            if qty == 0:
                after = None
                if cur is not None:
                    conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
                # 股數歸零（下市、清算等）：該檔未成交委託一併取消，避免之後成交或占用承諾金額
                cancelled = [r["order_id"] for r in conn.execute(
                    "SELECT order_id FROM orders WHERE ticker = ? AND status = 'open'", (ticker,)).fetchall()]
                conn.execute("UPDATE orders SET status = 'cancelled', cancel_reason = ?, updated_at = ? "
                             "WHERE ticker = ? AND status = 'open'", (f"人工調整股數為 0：{note}", now, ticker))
                result["cancelled_orders"] = cancelled
                conn.execute("UPDATE registry SET status='WATCH', updated_at=? WHERE ticker=? AND status='HOLDING'",
                             (now, ticker))
            else:
                avg = set_avg_cost if set_avg_cost is not None else (cur["avg_cost"] if cur else None)
                if avg is None:
                    raise LedgerError(f"新建 {ticker} 持股須指定 set_avg_cost")
                first = first_buy_date or (cur["first_buy_date"] if cur and cur["first_buy_date"] else date)
                after = {"qty": qty, "avg_cost": avg, "first_buy_date": first}
                write_row(conn, "positions", {
                    "ticker": ticker, "qty": qty, "avg_cost": avg, "first_buy_date": first, "updated_at": now,
                }, ["ticker"])
                conn.execute("UPDATE registry SET status='HOLDING', updated_at=? WHERE ticker=? AND status='WATCH'",
                             (now, ticker))
            if before != after:
                write_row(conn, "anomalies", {
                    "ticker": ticker, "kind": "manual_adjust",
                    "detail": {"before": before, "after": after, "note": note},
                    "status": "resolved", "pause_trading": 0,
                    "created_at": now, "resolved_at": now, "resolution": note,
                })
            result["position"] = {"ticker": ticker, "before": before, "after": after}

        if resolve_anomaly is not None:
            row = conn.execute("SELECT status FROM anomalies WHERE anomaly_id = ?", (resolve_anomaly,)).fetchone()
            if row is None:
                raise LedgerError(f"找不到異常 {resolve_anomaly}")
            if row["status"] != "open":
                raise LedgerError(f"異常 {resolve_anomaly} 已解決")
            conn.execute(
                "UPDATE anomalies SET status = 'resolved', resolved_at = ?, resolution = ? WHERE anomaly_id = ?",
                (now, note, resolve_anomaly),
            )
            result["resolved"].append(resolve_anomaly)

        if unpause is not None:
            ids = [r["anomaly_id"] for r in conn.execute(
                "SELECT anomaly_id FROM anomalies WHERE status = 'open' AND pause_trading = 1 AND ticker = ?",
                (unpause,),
            ).fetchall()]
            for aid in ids:
                conn.execute(
                    "UPDATE anomalies SET status = 'resolved', resolved_at = ?, resolution = ? WHERE anomaly_id = ?",
                    (now, note, aid),
                )
            result["resolved"].extend(ids)
    return result
