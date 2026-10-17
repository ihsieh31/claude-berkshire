"""紙上委託與撮合：下單、取消、隔日撮合。

撮合成交時，fills、cash_ledger、positions、registry、orders 在同一交易內寫入；任一步失敗整批回滾。
價格以四位小數（萬分之一美元）記帳，金額以分計算，避免浮點誤差。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from trader import db
from trader.data.universe import normalize_ticker
from trader.ledger.book import (LedgerError, _is_number, cash, paused, positions, transaction, write_row)


class BrokerError(Exception):
    """委託不合法或狀態不允許。"""


_PRICE_UNIT = 10_000  # 價格單位：萬分之一美元


def _check_date(d) -> None:
    try:
        datetime.strptime(str(d), "%Y-%m-%d")
    except ValueError as e:
        raise BrokerError(f"日期須為 YYYY-MM-DD：{d!r}") from e


def _gross_cents(qty: int, price_units: int) -> int:
    """股數 × 價格的金額（分），四捨五入。"""
    return (qty * price_units + 50) // 100


def _max_affordable(avail_cents: int, price_units: int) -> int:
    """在可用現金（分）內最多能買的整數股數。"""
    q = avail_cents * 100 // price_units
    while q > 0 and _gross_cents(q, price_units) > avail_cents:
        q -= 1
    return max(q, 0)


def place_order(conn: sqlite3.Connection, ticker: str, side: str, qty: int, limit_price: float | None,
                decision_id: str | None, created_date: str) -> str:
    """建立 open 委託，回傳 order_id `{T}-{side}-{YYYYMMDD}-{n}`。"""
    t = normalize_ticker(ticker)
    if not t:
        raise BrokerError("代號為空")
    if side not in ("BUY", "SELL"):
        raise BrokerError(f"side 須為 BUY 或 SELL：{side!r}")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise BrokerError(f"股數須為正整數：{qty!r}")
    if side == "BUY":
        if not _is_number(limit_price) or limit_price <= 0:
            raise BrokerError("買單須有限價且大於 0")
    elif limit_price is not None and (not _is_number(limit_price) or limit_price <= 0):
        raise BrokerError(f"賣單限價須為 None 或正數：{limit_price!r}")
    _check_date(created_date)
    if t in paused(conn):
        raise BrokerError(f"{t} 暫停交易中，拒絕下單")
    limit = None if limit_price is None else round(float(limit_price), 4)

    with transaction(conn):
        n = int(conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE ticker = ? AND side = ? AND created_date = ?",
            (t, side, created_date),
        ).fetchone()["c"]) + 1
        day = created_date.replace("-", "")
        while True:
            order_id = f"{t}-{side}-{day}-{n}"
            if conn.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,)).fetchone() is None:
                break
            n += 1
        write_row(conn, "orders", {
            "order_id": order_id, "ticker": t, "side": side, "qty": qty, "limit_price": limit,
            "status": "open", "decision_id": decision_id, "created_date": created_date,
            "cancel_reason": None, "updated_at": db.now_iso(),
        })
    return order_id


def cancel_order(conn: sqlite3.Connection, order_id: str, reason: str, status: str = "cancelled") -> None:
    """取消未成交委託。status 為 cancelled 或 expired。"""
    if status not in ("cancelled", "expired"):
        raise BrokerError(f"取消狀態須為 cancelled 或 expired：{status!r}")
    if not reason or not str(reason).strip():
        raise BrokerError("取消委託需附原因")
    with transaction(conn):
        row = conn.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise BrokerError(f"找不到委託：{order_id}")
        if row["status"] != "open":
            raise BrokerError(f"委託 {order_id} 狀態為 {row['status']}，無法取消")
        conn.execute(
            "UPDATE orders SET status = ?, cancel_reason = ?, updated_at = ? WHERE order_id = ?",
            (status, reason, db.now_iso(), order_id),
        )


def _registry_update(conn, ticker: str, status: str, cost: float | None, first_buy_date: str | None,
                     now: str) -> None:
    """合併更新登錄表：只改 status、data.cost、first_buy_date，其餘 data 欄位保留。登錄表無此代號則不建立。"""
    row = conn.execute("SELECT first_buy_date, data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return
    data = json.loads(row["data"] or "{}")
    if cost is not None:
        data["cost"] = cost
    write_row(conn, "registry", {
        "ticker": ticker, "status": status,
        "first_buy_date": row["first_buy_date"] or first_buy_date,
        "data": data, "updated_at": now,
    }, ["ticker"])


def _apply_buy(conn, ticker: str, qty: int, gross: float, date: str, now: str) -> None:
    cur = conn.execute(
        "SELECT qty, avg_cost, first_buy_date FROM positions WHERE ticker = ?", (ticker,)
    ).fetchone()
    if cur is None:
        new_qty, avg, first = qty, gross / qty, date
    else:
        new_qty = cur["qty"] + qty
        avg = (cur["avg_cost"] * cur["qty"] + gross) / new_qty
        first = cur["first_buy_date"] or date
    avg = round(avg, 6)
    write_row(conn, "positions", {
        "ticker": ticker, "qty": new_qty, "avg_cost": avg, "first_buy_date": first, "updated_at": now,
    }, ["ticker"])
    _registry_update(conn, ticker, "HOLDING", avg, first, now)


def _apply_sell(conn, ticker: str, qty: int, now: str) -> bool:
    """回傳是否賣光。賣出不改平均成本。"""
    cur = conn.execute("SELECT qty FROM positions WHERE ticker = ?", (ticker,)).fetchone()
    if cur is None or cur["qty"] < qty:
        raise BrokerError(f"{ticker} 持股不足，無法賣出 {qty} 股")
    new_qty = cur["qty"] - qty
    if new_qty == 0:
        conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
        _registry_update(conn, ticker, "WATCH", None, None, now)
        return True
    conn.execute("UPDATE positions SET qty = ?, updated_at = ? WHERE ticker = ?", (new_qty, now, ticker))
    return False


def _fill(conn, o: dict, qty: int, price: float, commission: float, date: str) -> dict:
    """成交入帳。須在 transaction 內呼叫。qty 可能小於委託股數（現金或持股不足）。"""
    t, side, oid = o["ticker"], o["side"], o["order_id"]
    pu = round(price * _PRICE_UNIT)
    gross = _gross_cents(qty, pu) / 100
    now = db.now_iso()
    partial = qty < o["qty"]
    note = f"{t} {side} {qty} @ {price:.4f}"

    write_row(conn, "fills", {
        "order_id": oid, "ticker": t, "side": side, "qty": qty, "price": price,
        "commission": commission, "fill_date": date,
    })
    if side == "BUY":
        write_row(conn, "cash_ledger", {"date": date, "amount": -gross, "kind": "buy", "ref": oid, "note": note})
        _apply_buy(conn, t, qty, gross, date, now)
    else:
        write_row(conn, "cash_ledger", {"date": date, "amount": gross, "kind": "sell", "ref": oid, "note": note})
        _apply_sell(conn, t, qty, now)
    if commission > 0:
        write_row(conn, "cash_ledger", {
            "date": date, "amount": -commission, "kind": "commission", "ref": oid, "note": note,
        })
    reason = f"部分成交 {qty}/{o['qty']}（現金或持股不足，其餘股數不再撮合）" if partial else None
    conn.execute(
        "UPDATE orders SET status = 'filled', cancel_reason = ?, updated_at = ? WHERE order_id = ?",
        (reason, now, oid),
    )
    return {
        "order_id": oid, "ticker": t, "side": side, "qty": qty, "price": price, "gross": gross,
        "commission": commission, "fill_date": date, "partial": partial,
    }


def _bar_ok(bar: dict | None) -> bool:
    return bar is not None and all(_is_number(bar.get(k)) for k in ("open", "high", "low"))


def _match_buy(conn, o: dict, bar: dict, bps: float, comm: float, date: str) -> dict | None:
    limit = o["limit_price"]
    if bar["low"] > limit:
        return None
    # 限價單成交價不得劣於限價（買價不得高於上限，SKILL §5）；滑價只加在開盤價上
    price = round(min(bar["open"] * (1 + bps / 1e4), limit), 4)
    avail = round(cash(conn) * 100) - round(comm * 100)
    qty = min(o["qty"], _max_affordable(avail, round(price * _PRICE_UNIT)))
    if qty <= 0:
        return None  # 現金不足且降到 0 股：委託保留
    return _fill(conn, o, qty, price, comm, date)


def _match_sell(conn, o: dict, bar: dict, bps: float, comm: float, date: str) -> dict | None:
    t = o["ticker"]
    held = positions(conn).get(t, {}).get("qty", 0)
    if held <= 0:
        cancel_order(conn, o["order_id"], "無持股可賣", "cancelled")
        return None
    limit = o["limit_price"]
    if limit is not None:
        if bar["high"] < limit:
            return None
        price = round(max(bar["open"] * (1 - bps / 1e4), limit), 4)  # 賣價不低於限價
    else:
        price = round(bar["open"] * (1 - bps / 1e4), 4)
    return _fill(conn, o, min(o["qty"], held), price, comm, date)


def match_orders(conn: sqlite3.Connection, date: str, bars: dict[str, dict], slippage_bps: float,
                 commission: float) -> list[dict]:
    """撮合 created_date < date 的 open 委託（即下一交易日撮合）。整批在單一交易內完成。

    bars：{T: {open, high, low, close}}，只含當日。暫停代號與無當日 bar 的代號不撮合。
    回傳本日所有成交。
    """
    _check_date(date)
    if not _is_number(slippage_bps) or not 0 <= slippage_bps < 10_000:
        raise BrokerError(f"滑價須為 [0, 10000) 的 bps：{slippage_bps!r}")
    if not _is_number(commission) or commission < 0:
        raise BrokerError(f"手續費須為非負數：{commission!r}")
    bps = float(slippage_bps)
    comm = round(float(commission), 2)

    fills: list[dict] = []
    with transaction(conn):
        halted = paused(conn)
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE status = 'open' AND created_date < ? ORDER BY created_date, order_id",
            (date,),
        ).fetchall()]
        for o in orders:
            t = o["ticker"]
            if t in halted:
                continue
            bar = (bars or {}).get(t)
            if not _bar_ok(bar):
                continue
            if o["side"] == "BUY":
                fill = _match_buy(conn, o, bar, bps, comm, date)
            else:
                fill = _match_sell(conn, o, bar, bps, comm, date)
            if fill is not None:
                fills.append(fill)
    return fills
