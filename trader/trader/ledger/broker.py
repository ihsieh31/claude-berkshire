"""紙上委託與撮合：下單、取消、隔日撮合。

撮合成交時，fills、cash_ledger、positions、registry、orders 在同一交易內寫入；任一步失敗整批回滾。
價格以四位小數（萬分之一美元）記帳，金額以分計算，避免浮點誤差。

成交口徑（設計決策 D07，紙上交易近似；SKILL §5 避開開盤）：典型價 TP＝(高＋低＋收)÷3。
- 市價單：買 TP×(1＋滑價)、賣 TP×(1−滑價)。
- 限價買：最低價 ≤ 限價才成交，價＝min(TP×(1＋滑價), 限價)；限價賣：最高價 ≥ 限價才成交，價＝max(TP×(1−滑價), 限價)。
  （推論：滑價照市價單一樣加在 TP 上，再以限價夾住，取保守方向。）
- 限價賣單（估值類 TRIM，限價＝觸發線，D03；人工 `trader trade sell --limit`）當日有效：撮合日最高價未達限價即作廢，
  回落即停，不留隔日。
- 人工下單（D12，決策 review_type＝MANUAL）照一般撮合，但不做撮合前的自動重核（上限、冷靜、觸發線；那是使用者的決定）。
- 人工暫停（manual_pause，book.pause）：SELL_ALL 仍建立委託，撮合延到解除後（M7-RS7-02）；其他下單拒絕。
- 公司行動等系統暫停照 D06 放行全清，但 D88 收窄：該檔有未結的比例類／需人工／類型未知（非現金股利）公司行動暫停、
  且事件日 ≤ 撮合日 → SELL_ALL 也不撮合、委託保留，待人工 adjust 換算股數後解除（拆後價×拆前股數會記錯現金）。
- SELL_ALL 成交帳上全部股數（委託股數是下單當時的；之間的人工加買／對帳不得留殘股，A3-X2-02）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from trader import db
from trader.data.universe import normalize_ticker
from trader.ledger.book import (LedgerError, _holding_stats, _is_number, cash, paused, positions, registry_hold,
                                registry_sold_out, transaction, write_row)

MANUAL = "MANUAL"   # 人工下單的決策 review_type（D12，`trader trade`）


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
    # 暫停交易（公司行動待人工）只擋買進與部分減碼；論點證偽的全清照常下單撮合（設計決策 D06，P-52 不等反彈）；
    # 人工暫停（M8-D4-03）擋其他下單；SELL_ALL 仍建立委託、撮合時才擋，解除暫停後次日撮合，全清不遺失（M7-RS7-02）
    halted = paused(conn)
    if t in halted and not (side == "SELL" and _is_sell_all(conn, decision_id)):
        raise BrokerError(f"{t} 暫停交易中，拒絕下單" + ("（人工暫停：只建立 SELL_ALL 全清委託，解除後撮合）"
                                                       if halted.hard(t) else "（只放行 SELL_ALL 全清）"))
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


def _is_sell_all(conn, decision_id: str | None) -> bool:
    """委託對應的決策是否為 SELL_ALL（全清）。"""
    return _decision_log(conn, decision_id).get("action") == "SELL_ALL"


def _is_manual(conn, decision_id: str | None) -> bool:
    return _decision_log(conn, decision_id).get("review_type") == MANUAL


def cancel_manual(conn: sqlite3.Connection, order_id: str, note: str, date: str) -> None:
    """人工撤單（M8-D4-03，`trader ledger cancel`）：取消未成交委託，並寫一筆已結案的 manual_cancel 稽核異常。"""
    if not note or not str(note).strip():
        raise BrokerError("人工撤單需附 note")
    with transaction(conn):
        cancel_order(conn, order_id, f"人工撤單：{note}")
        o = conn.execute("SELECT ticker, side, qty, limit_price, decision_id FROM orders WHERE order_id = ?",
                         (order_id,)).fetchone()
        now = db.now_iso()
        write_row(conn, "anomalies", {
            "ticker": o["ticker"], "kind": "manual_cancel",
            "detail": {"order_id": order_id, "side": o["side"], "qty": o["qty"], "limit_price": o["limit_price"],
                       "decision_id": o["decision_id"], "date": date, "note": note},
            "status": "resolved", "pause_trading": 0, "created_at": now, "resolved_at": now, "resolution": note,
        })


def _trim_unfilled(conn, o: dict, why: str) -> None:
    """估值類 TRIM 限價單未成交即作廢（D03 回落即停）：登錄表 last_trim 是回寫時記下的「預計」減碼，同一決策日的那筆
    標 unfilled、shares 改 0（峰值與觸發線不動），之後的 HOLD_REVIEW 輸入才不會把沒賣成的當成已減（P24 交接）。"""
    d = conn.execute("SELECT decision_date FROM decisions WHERE decision_id = ?", (o["decision_id"],)).fetchone()
    reg = conn.execute("SELECT data FROM registry WHERE ticker = ?", (o["ticker"],)).fetchone()
    if d is None or reg is None or _decision_log(conn, o["decision_id"]).get("review_type") != "HOLD_REVIEW":
        return
    data = json.loads(reg["data"] or "{}")
    lt = data.get("last_trim")
    if not isinstance(lt, dict) or str(lt.get("date") or "")[:10] != str(d["decision_date"])[:10]:
        return
    lt.update({"shares": 0, "unfilled": why})
    conn.execute("UPDATE registry SET data = ?, updated_at = ? WHERE ticker = ?",
                 (json.dumps(data, ensure_ascii=False), db.now_iso(), o["ticker"]))


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
    registry_hold(conn, ticker, avg, first, now)
    _holding_stats(conn, ticker, new_qty, avg, cur is None, now)


def _apply_sell(conn, ticker: str, qty: int, date: str, now: str) -> bool:
    """回傳是否賣光。賣出不改平均成本。"""
    cur = conn.execute("SELECT qty FROM positions WHERE ticker = ?", (ticker,)).fetchone()
    if cur is None or cur["qty"] < qty:
        raise BrokerError(f"{ticker} 持股不足，無法賣出 {qty} 股")
    new_qty = cur["qty"] - qty
    if new_qty == 0:
        conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
        registry_sold_out(conn, ticker, date, now)
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
        _apply_sell(conn, t, qty, date, now)
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
    return bar is not None and all(_is_number(bar.get(k)) for k in ("high", "low", "close"))


def _typical(bar: dict) -> float:
    """典型價 (高＋低＋收)÷3（D07）。"""
    return (bar["high"] + bar["low"] + bar["close"]) / 3


def _match_buy(conn, o: dict, bar: dict, bps: float, comm: float, date: str) -> dict | None:
    limit = o["limit_price"]
    if bar["low"] > limit:
        return None
    # 限價單成交價不得劣於限價（買價不得高於上限，SKILL §5）；典型價加滑價後以限價夾住（D07）
    price = round(min(_typical(bar) * (1 + bps / 1e4), limit), 4)
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
        if bar["high"] < limit:   # 估值 TRIM 限價＝觸發線、當日有效：未觸及即作廢（D03）
            why = f"限價賣單當日有效：最高價 {bar['high']} 未達觸發線 {limit}，回落即停"
            cancel_order(conn, o["order_id"], why, "expired")
            _trim_unfilled(conn, o, f"{date} {why}")
            return None
        price = round(max(_typical(bar) * (1 - bps / 1e4), limit), 4)  # 賣價不低於限價
    else:
        price = round(_typical(bar) * (1 - bps / 1e4), 4)
    # 全清（SELL_ALL）賣帳上全部；其他賣單以 min(委託, 持股)（A3-X2-02）
    qty = held if _is_sell_all(conn, o["decision_id"]) else min(o["qty"], held)
    return _fill(conn, o, qty, price, comm, date)


def _ca_ratio_pending(conn, ticker: str, date: str) -> bool:
    """D88：該檔有未結且暫停中的公司行動異常（corp_action%），類型為比例類／需人工／未知（非現金股利），且事件日 ≤ date。
    類型以模型結果 action_type 為準，缺（工作失敗、輸出不合格）才看 Alpaca 類型；兩者皆缺＝未知 → 擋。
    事件日取結果 effective_date，缺則事件 date；皆缺視為已生效（失敗關閉）。"""
    for r in conn.execute("SELECT detail FROM anomalies WHERE ticker = ? AND status = 'open' AND pause_trading = 1 "
                          "AND kind LIKE 'corp_action%'", (ticker,)).fetchall():
        try:
            d = json.loads(r["detail"]) if r["detail"] else {}
        except (TypeError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        ev = d.get("event") if isinstance(d.get("event"), dict) else {}
        res = d.get("result") if isinstance(d.get("result"), dict) else {}
        atype = res.get("action_type")
        alp = (ev.get("alpaca") or {}).get("type") if isinstance(ev.get("alpaca"), dict) else None
        cash_div = atype == "現金股利" if atype not in (None, "", "未知") else alp == "cash_dividend"
        if cash_div:
            continue
        eff = str(res.get("effective_date") or ev.get("date") or "")[:10]
        if not eff or eff <= date:
            return True
    return False


def _decision_log(conn, decision_id: str | None) -> dict:
    if not decision_id:
        return {}
    row = conn.execute("SELECT data FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
    try:
        log = json.loads(row["data"]) if row is not None and row["data"] else {}
    except (TypeError, ValueError):
        return {}
    return log if isinstance(log, dict) else {}


def recheck_pending(conn: sqlite3.Connection, date: str, closes: dict[str, float], slippage_bps: float) -> list[dict]:
    """撮合前重核（設計決策 D05）：以前一交易日收盤 closes 重核將於 date 撮合的委託，不符者取消並寫理由。
    - 未成交買單：sizing.revalidate_open_buys（上限已不高於限價、委託後結論改變、持股的停止加碼；check-P17：
      要在撮合前擋，否則兩次 daily 之間寫入的結論擋不住當天撮合）。
    - 冷靜單（PRICE 觸發的 BUY／ADD）：收盤×(1＋滑價) < 現行上限，否則取消。委託不記 review_type，閘門決策為 PRICE 的
      建倉批次也會走到這裡；批次下單時已用同一口徑自檢（sizing.plan_tranches，P27R），故撤單理由用中性的「上限重核」。
    - 估值類 TRIM（限價賣單，限價＝觸發線；含等同意數日後才完成者，及 D82 PRICE 觸發而冷靜一日的 HOLD_REVIEW
      估值 TRIM）：收盤仍 ≥ 觸發線，否則取消，並把同決策日的 last_trim 標 unfilled、shares＝0（_trim_unfilled：
      沒賣成不算減過，A3-M7A-02）。
    缺前一日收盤 → 無法重核，取消（有疑義時不動）。暫停交易的檔不動（撮合本就略過）。回傳 [{order_id, ticker, reason}]。"""
    from trader.portfolio import sizing as Z   # 延遲匯入：sizing 依賴 ledger.book
    _check_date(date)
    out = [c for c in Z.revalidate_open_buys(conn, date)]
    seen = {c["order_id"] for c in out}
    halted = paused(conn)
    for r in conn.execute("SELECT * FROM orders WHERE status = 'open' AND created_date < ? ORDER BY order_id",
                          (date,)).fetchall():
        o, t = dict(r), r["ticker"]
        if o["order_id"] in seen or t in halted or _is_manual(conn, o["decision_id"]):
            continue   # 人工下單（D12）不做自動重核
        px = closes.get(t)
        why = None
        if o["side"] == "BUY" and _decision_log(conn, o["decision_id"]).get("review_type") == Z.PRICE_TRIGGER:
            cap = Z._caps(Z.latest_card(conn, t) or {}, Z._reg_data(conn, t) or {})[0]
            if not _is_number(px):
                why = "上限重核：缺前一日收盤"
            elif cap is None or not px * (1 + slippage_bps / 1e4) < cap:
                why = f"上限重核：前一日收盤 {px}（含滑價）已不低於上限 {cap}"
        elif o["side"] == "SELL" and o["limit_price"] is not None:
            if not _is_number(px):
                why = "估值減碼重核：缺前一日收盤"
            elif px < o["limit_price"]:
                why = f"估值減碼重核：前一日收盤 {px} 已回到觸發線 {o['limit_price']} 之下"
        if why:
            out.append({"order_id": o["order_id"], "ticker": t, "reason": why})
    for c in out:
        with transaction(conn):
            cancel_order(conn, c["order_id"], c["reason"])
            if c["reason"].startswith("估值減碼重核"):
                o = dict(conn.execute("SELECT * FROM orders WHERE order_id = ?", (c["order_id"],)).fetchone())
                _trim_unfilled(conn, o, f"{date} {c['reason']}")
    return out


def match_orders(conn: sqlite3.Connection, date: str, bars: dict[str, dict], slippage_bps: float,
                 commission: float) -> list[dict]:
    """撮合 created_date < date 的 open 委託（即下一交易日撮合）。整批在單一交易內完成。

    bars：{T: {open, high, low, close}}，只含當日；成交價依典型價（D07，見檔頭）。
    暫停代號（SELL_ALL 全清除外，D06；D88：比例類公司行動待換算時連全清也不撮合）與缺高／低／收的代號不撮合。
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
            if t in halted and not (o["side"] == "SELL" and not halted.hard(t) and _is_sell_all(conn, o["decision_id"])
                                    and not _ca_ratio_pending(conn, t, date)):   # D88（A3-M7C-01）
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
