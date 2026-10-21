"""紙上帳本的帳務核心：現金、應收股利、持股、每日淨值與人工調整。

- 金額為美元，現金流以分為單位（round 到 2 位）；股數為整數。
- `transaction` 最外層開交易、成功提交、失敗回滾；巢狀呼叫併入外層交易。
- 本模組與 broker.py 的寫入函式不呼叫 db.insert／db.upsert（它們會自行 commit，會破壞外層交易）。
"""
from __future__ import annotations

import json
import math
from datetime import date as _date
import sqlite3
from contextlib import contextmanager
from pathlib import Path
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
    """cash_ledger 沒有 ref='INIT' 的入金時寫入初始入金；已有則不動。首次 daily 前用 --deposit 先入金也不會吞掉初始資金
    （M8-R1-01）。"""
    if not _is_number(initial_cash) or initial_cash <= 0:
        raise LedgerError(f"初始資金須為正數：{initial_cash!r}")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM cash_ledger WHERE kind = 'deposit' AND ref = 'INIT' LIMIT 1").fetchone() is None:
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


MANUAL_PAUSE = "manual_pause"
CA_JOB_FAILED = "corp_action_job_failed"   # 同 corp_actions.JOB_FAILED_KIND（corp_actions 匯入本模組，不反向匯入）
GLOBAL = "*"   # 人工全域暫停（anomalies.ticker 為 NULL）；`ledger pause --all`／`ledger adjust --unpause-all`


class Paused(set):
    """book.paused 的回傳：暫停代號集合。有未結的人工全域暫停時任何代號都算在內（`t in paused(conn)`）；
    hard(t)：人工暫停（manual_pause，含全域）撮合時連 SELL_ALL 全清也擋（SELL_ALL 委託照建、解除後次日撮合；D06 的全清照常撮合只適用於公司行動等系統暫停，A3-M8C2-04）。"""

    def __init__(self, tickers=(), all_: bool = False, manual=()) -> None:
        super().__init__(tickers)
        self.all = all_
        self.manual = set(manual)

    def __contains__(self, t) -> bool:
        return self.all or set.__contains__(self, t)

    def hard(self, t) -> bool:
        return self.all or t in self.manual


def paused(conn: sqlite3.Connection) -> Paused:
    """有未結異常且 pause_trading=1 的代號（含人工全域暫停，M8-D4-03）。這些代號不撮合、不下新單。"""
    rows = conn.execute("SELECT DISTINCT ticker, kind FROM anomalies WHERE status = 'open' AND pause_trading = 1").fetchall()
    return Paused({r["ticker"] for r in rows if r["ticker"] is not None},
                  any(r["ticker"] is None and r["kind"] == MANUAL_PAUSE for r in rows),
                  {r["ticker"] for r in rows if r["ticker"] is not None and r["kind"] == MANUAL_PAUSE})


def pause(conn: sqlite3.Connection, ticker: str | None, *, date: str, note: str) -> int:
    """人工暫停（M8-D4-03）：寫一筆 pause_trading=1 的 manual_pause 異常；ticker=None＝全部代號。
    暫停中該檔（或全部）不撮合；買進與 TRIM 減碼拒絕下單（rejected，需要的話解除後人工補下），SELL_ALL 全清照建
    委託、撮合時擋，解除後次日自動撮合（不要另補賣單，A3-M8C2-04）；解除用 adjust(unpause=代號或 GLOBAL)。
    回傳異常編號。"""
    if not isinstance(note, str) or not note.strip():
        raise LedgerError("人工暫停必須附 note")
    t = normalize_ticker(ticker) if ticker is not None else None
    if ticker is not None and not t:
        raise LedgerError("代號為空")
    with transaction(conn):
        cur = conn.execute("INSERT INTO anomalies(ticker, kind, detail, status, pause_trading, created_at) "
                           "VALUES (?, ?, ?, 'open', 1, ?)",
                           (t, MANUAL_PAUSE, json.dumps({"date": date, "note": note, "scope": t or "ALL"},
                                                        ensure_ascii=False), db.now_iso()))
    return int(cur.lastrowid)


def registry_hold(conn: sqlite3.Connection, ticker: str, avg_cost: float, first_buy_date: str | None,
                  now: str) -> None:
    """帳上有持股（成交、人工調整、對帳）：登錄表有列就改 HOLDING、data.cost＝平均成本，first_buy_date 欄缺才補。
    其餘 data 欄位保留；登錄表無此代號則不建立。須在 transaction 內呼叫。"""
    row = conn.execute("SELECT first_buy_date, data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return
    data = json.loads(row["data"] or "{}")
    data["cost"] = avg_cost
    conn.execute("UPDATE registry SET status = 'HOLDING', first_buy_date = ?, data = ?, updated_at = ? WHERE ticker = ?",
                 (row["first_buy_date"] or first_buy_date, json.dumps(data, ensure_ascii=False), now, ticker))


def registry_sold_out(conn: sqlite3.Connection, ticker: str, date: str, now: str) -> None:
    """持股歸零（賣光、人工調整為 0、公司行動後無整股）：HOLDING → WATCH，並清掉 auto_buy_cap、build_plan 與
    t30_at_valuation、記 sold_out_at。舊上限是持有期間的分析結果，保留會讓剛賣掉（常因論點證偽）的檔價格一跌就進
    PRICE 快速通道被買回；t30_at_valuation 是未持有列唯一還會排 PRICE 的欄位（殖利率變動 ≥1pp，review_due），
    PRICE 會用舊 cap_inputs 回寫 auto_buy_cap。之後要有新的完整分析重寫這些欄位才會再進 PRICE。
    其他狀態（REJECT／TOO_HARD 等）不改。"""
    row = conn.execute("SELECT status, data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return
    data = json.loads(row["data"] or "{}")
    data.update({"auto_buy_cap": None, "build_plan": None, "t30_at_valuation": None, "sold_out_at": date})
    status = "WATCH" if row["status"] == "HOLDING" else row["status"]
    conn.execute("UPDATE registry SET status = ?, data = ?, updated_at = ? WHERE ticker = ?",
                 (status, json.dumps(data, ensure_ascii=False), now, ticker))


def _cap_input_path(conn, cap_input_file) -> Path | None:
    """cap_input_file 解析：相對路徑以 state（DB 所在目錄）為基準；舊列的他機絕對路徑不存在時退回
    <state>/cap_inputs/<檔名>（M8-R4-01）。"""
    if not cap_input_file:
        return None
    p = Path(str(cap_input_file))
    if p.is_absolute() and p.is_file():
        return p
    row = conn.execute("PRAGMA database_list").fetchone() if conn is not None else None
    state = Path(row[2]).parent if row and row[2] else None
    if state is None:
        return p
    return state / p if not p.is_absolute() else state / "cap_inputs" / p.name


def _norm_eps(cap_input_file, conn=None) -> float | None:
    """登錄表 cap_input_file（最近一次完整分析的 cap_calc 輸入）之 norm_eps，缺用 oe；讀不到或 ≤0 → None。"""
    try:
        p = _cap_input_path(conn, cap_input_file)
        x = json.loads(p.read_text(encoding="utf-8")) if p else {}
    except (OSError, ValueError):
        return None
    for k in ("norm_eps", "oe"):
        v = x.get(k) if isinstance(x, dict) else None
        if _is_number(v) and v > 0:
            return float(v)
    return None


def _holding_stats(conn, ticker: str, new_qty: int, avg_cost: float, new_period: bool, now: str) -> None:
    """持有側紀錄（M2_D4_06；P-45 倍數警報、P-47 累計保留 ≥ 峰值 2/3）：持股增加時寫進登錄表 data
    （broker 買進成交、人工調整建倉／加倉、對帳開新持有期，同一寫法；減少不呼叫＝不降峰值；X43）。
    - peak_qty：本持有期峰值股數＝max(舊峰值, 成交後股數)；新持有期（增加前無持股）重新起算。拆合股由 corp_actions 換算。
    - entry_norm_pe：持有成本的正常化 P/E＝成交後平均成本 ÷ 當時 cap_input 的 norm_eps（缺用 oe）；每次增加時更新
      （建倉多批時即為建倉均價的 P/E；推論）。norm_eps 取不到 → 新持有期寫 None，否則保留舊值。"""
    row = conn.execute("SELECT data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return
    data = json.loads(row["data"] or "{}")
    old_peak = None if new_period else data.get("peak_qty")
    data["peak_qty"] = max(new_qty, int(old_peak) if _is_number(old_peak) else 0)
    eps = _norm_eps(data.get("cap_input_file"), conn)
    if eps is not None:
        data["entry_norm_pe"] = round(avg_cost / eps, 4)
    elif new_period:
        data["entry_norm_pe"] = None
    conn.execute("UPDATE registry SET data = ?, updated_at = ? WHERE ticker = ?",
                 (json.dumps(data, ensure_ascii=False), now, ticker))


def reconcile_registry(conn: sqlite3.Connection, date: str) -> list[str]:
    """帳本（positions）為持有與否的準：有股數而登錄表非 HOLDING → 改 HOLDING（成本同步）；
    登錄表 HOLDING 而無股數 → 依 registry_sold_out 改 WATCH；登錄表已有列的持股，其未結 unregistered_holding 異常結案。
    回傳變動說明（供每日摘要）。"""
    now = db.now_iso()
    notes: list[str] = []
    with transaction(conn):
        pos = positions(conn)
        reg = {r["ticker"]: r["status"] for r in conn.execute("SELECT ticker, status FROM registry")}
        for t, p in pos.items():
            if t not in reg:
                continue
            if reg[t] != "HOLDING":
                registry_hold(conn, t, p["avg_cost"], p["first_buy_date"], now)
                _holding_stats(conn, t, p["qty"], p["avg_cost"], True, now)   # 登錄表的新持有期：舊峰值不沿用
                notes.append(f"{t} 帳上持有 {p['qty']} 股，登錄表 {reg[t]} → HOLDING")
            n = conn.execute("UPDATE anomalies SET status = 'resolved', resolved_at = ?, resolution = ? "
                             "WHERE ticker = ? AND kind = 'unregistered_holding' AND status = 'open'",
                             (now, f"{date} 對帳：登錄表已有此代號", t)).rowcount
            if n:
                notes.append(f"{t} 已在登錄表：持股未登錄異常結案")
        for t, st in reg.items():
            if st == "HOLDING" and t not in pos:
                registry_sold_out(conn, t, date, now)
                notes.append(f"{t} 登錄表 HOLDING 但帳上無持股 → WATCH（清除自動買進上限）")
    return notes


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
               benchmark_dividend: float | None = None, watch_closes: dict[str, float] | None = None) -> dict:
    """寫入（或更新）當日淨值。data 含 weights、missing_close、receivables、prices（估值所用每股價）、
    benchmark_dividend（基準當日除息的每股現金股利；儀表板算含股利總報酬用）、watch_prices（觀察名單當日收盤，
    不計淨值；網頁登錄表現價用，A3-M8C2-03）。"""
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
                 "benchmark_dividend": benchmark_dividend,
                 "watch_prices": {t: float(p) for t, p in (watch_closes or {}).items()
                                  if _is_number(p) and p > 0 and t not in vals}},
    }
    with transaction(conn):
        write_row(conn, "nav_daily", row, ["date"])
    return row


def show(conn: sqlite3.Connection, closes: dict[str, float] | None = None) -> dict:
    """帳本摘要：現金、應收、持股（含權重）、未成交委託、未結異常。
    closes 為 None：以最後一筆淨值的估值價（nav_daily.data.prices，與儀表板同一來源）估值，缺價才用平均成本
    （M8-D5-10）；valuation＝nav（有淨值）或 cost（尚無淨值），valuation_date＝該淨值日。"""
    valuation, vdate = "closes", None
    if closes is None:
        last = conn.execute("SELECT date, data FROM nav_daily ORDER BY date DESC LIMIT 1").fetchone()
        valuation = "nav" if last is not None else "cost"
        if last is not None:
            vdate = last["date"]
            closes = (json.loads(last["data"]) if last["data"] else {}).get("prices") or {}
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
        "valuation": valuation,
        "valuation_date": vdate,
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
           unpause: str | None = None, first_buy_date: str | None = None, deposit: float | None = None,
           resolve_corp_action: int | None = None, note: str) -> dict:
    """人工調整。每次呼叫在單一交易內完成；持股變動另寫一筆已解決的 manual_adjust 異常作為稽核紀錄。

    cash_delta：調帳（kind=adjust，報酬計算視為帳戶內損益）；deposit：外部入金／出金（kind=deposit，正入負出，
    報酬計算視為外部資金，M8-D1-04）。
    first_buy_date：分拆、併購換得的新股沿用原持股的建倉日（SKILL §6 一年限制），未指定時新建持股用 date。
    unpause：解除該代號所有暫停異常；GLOBAL（"*"）＝解除人工全域暫停。該檔仍有未結公司行動時回 warnings。
    公司行動工作失敗（corp_action_job_failed）只解除暫停（pause_trading→0）、不結案：事件仍留給下次 daily 重派
    入帳（A3-V3-3-02）；但同一次呼叫改了該檔（ticker＝unpause）股數，或帶 --ticker 該檔＋cash_delta（人工入帳股利、
    現金對價）→ 視為已人工入帳，一併結案、不再重派（現金調帳不寫 manual_adjust，重派的 conflict 守門看不到，會重複入帳）。
    （分開兩次呼叫時，重派的 apply 看到失敗後有 manual_adjust 會回 conflict 轉人工，不會重複入帳。）
    deposit 的日期須晚於最後淨值日（該日淨值已含當時全部現金）：等於最後淨值日 → 改記下一交易日並回
    deposit_date_shifted；早於 → 拒絕（回填會永久扭曲報酬指數，A3-M8C2-01）。
    resolve_corp_action：把 pending_human／anomaly 的公司行動列結案（status→applied，data.manual 記 note、日期；M7-D4-04）。
    """
    if not isinstance(note, str) or not note.strip():
        raise LedgerError("人工調整必須附 note")
    if ticker is not None:
        ticker = normalize_ticker(ticker)
    if (cash_delta is None and set_qty is None and set_avg_cost is None and first_buy_date is None
            and resolve_anomaly is None and unpause is None and deposit is None and resolve_corp_action is None):
        raise LedgerError("沒有任何調整項目")
    if cash_delta is not None:
        if not _is_number(cash_delta) or _money(cash_delta) == 0:
            raise LedgerError(f"cash_delta 須為非零有限數：{cash_delta!r}")
        cash_delta = _money(cash_delta)
    if deposit is not None:
        if not _is_number(deposit) or _money(deposit) == 0:
            raise LedgerError(f"deposit 須為非零有限數：{deposit!r}")
        deposit = _money(deposit)
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
    if unpause is not None and unpause != GLOBAL:
        unpause = normalize_ticker(unpause)

    now = db.now_iso()
    result: dict = {"cash_delta": None, "position": None, "resolved": []}
    with transaction(conn):
        if cash_delta is not None:
            write_row(conn, "cash_ledger", {
                "date": date, "amount": cash_delta, "kind": "adjust", "ref": f"ADJ:{date}", "note": note,
            })
            result["cash_delta"] = cash_delta
        if deposit is not None:
            # 報酬指數把入金歸在 (前一淨值日, 本淨值日]，淨值卻用當下全部現金：入金日 ≤ 最後淨值日會被當成損益
            # （A3-M8C2-01）。等於最後淨值日（daily 後才入金）→ 記到下一交易日；早於（回填）→ 拒絕
            dep_date = date
            last_nav = conn.execute("SELECT MAX(date) FROM nav_daily").fetchone()[0]
            if last_nav is not None and date < last_nav:
                raise LedgerError(f"入金／出金日 {date} 早於最後淨值日 {last_nav}：不能回填（會永久扭曲報酬指數）；"
                                  f"請用 {last_nav} 之後的日期，或省略 --date")
            if last_nav is not None and date == last_nav:
                from trader.calendar import TradingCalendar   # 規則日曆，不連網
                dep_date = TradingCalendar().next_trading_day(last_nav)
                result["deposit_date_shifted"] = {
                    "from": date, "to": dep_date,
                    "reason": f"{date} 的淨值已寫入，入金記到下一交易日，報酬計算才不會把它當成 {date} 的損益"}
            write_row(conn, "cash_ledger", {
                "date": dep_date, "amount": deposit, "kind": "deposit", "ref": f"DEP:{dep_date}", "note": note,
            })
            result["deposit"] = deposit

        if set_qty is not None or set_avg_cost is not None or first_buy_date is not None:
            cur = conn.execute(
                "SELECT qty, avg_cost, first_buy_date FROM positions WHERE ticker = ?", (ticker,)
            ).fetchone()
            before = dict(cur) if cur else None
            qty = set_qty if set_qty is not None else (cur["qty"] if cur else None)
            if qty is None:
                raise LedgerError(f"{ticker} 無持股，須同時指定 set_qty")
            if qty == 0 and cur is None:   # 打錯代號不得清登錄表上限、記 sold_out（A3-M7A-06）
                raise LedgerError(f"{ticker} 無持股，--qty 0 無作用（代號是否打錯？）")
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
                registry_sold_out(conn, ticker, date, now)
            else:
                avg = set_avg_cost if set_avg_cost is not None else (cur["avg_cost"] if cur else None)
                if avg is None:
                    raise LedgerError(f"新建 {ticker} 持股須指定 set_avg_cost")
                first = first_buy_date or (cur["first_buy_date"] if cur and cur["first_buy_date"] else date)
                after = {"qty": qty, "avg_cost": avg, "first_buy_date": first}
                write_row(conn, "positions", {
                    "ticker": ticker, "qty": qty, "avg_cost": avg, "first_buy_date": first, "updated_at": now,
                }, ["ticker"])
                if cur is not None and cur["qty"] > 0 and qty != cur["qty"]:
                    # 拆合股證據＝同時指定新均價且總成本不變（±1%）：登錄表每股欄位、peak_qty、last_trim 依比例換算
                    # （M7-RS7-01）；先換算再 _holding_stats，峰值才不會被重複放大。cap_inputs 由 cli 換算。
                    # 無此證據（對帳、補記成交）不動估值線，只回警示（RM7-1R）
                    base = cur["qty"] * cur["avg_cost"]
                    if set_avg_cost is not None and base > 0 and abs(qty * avg - base) <= 0.01 * base:
                        from .corp_actions import _scale_registry   # 延遲匯入：corp_actions 匯入本模組
                        _scale_registry(conn, ticker, qty / cur["qty"])
                        result["registry_rescaled"] = {"ticker": ticker, "ratio": qty / cur["qty"]}
                    else:
                        result.setdefault("warnings", []).append(
                            f"{ticker} 股數 {cur['qty']}→{qty} 但總成本有變或未給新均價：視為對帳／補記，登錄表價格線、"
                            "cap_inputs 與未成交賣單限價未換算；若是拆合股，請同時以 --avg-cost 給換算後均價")
                registry_hold(conn, ticker, avg, first, now)
                if cur is None or qty > cur["qty"]:   # 與 broker 買進成交同：新持有期重設、加倉取 max；減少不動
                    _holding_stats(conn, ticker, qty, avg, cur is None, now)
                if cur is not None and cur["qty"] > 0 and qty != cur["qty"]:
                    # 股數改變（多為需人工的拆合股）：買單的限價與股數是舊股數下規劃的 → 取消、隔日重新規劃；
                    # 賣單不取消（避免丟掉論點證偽的全清），股數依新舊股數比例換算；限價只在有拆合股證據
                    # （registry_rescaled）時換算，對帳／補記保留原限價（TRIM 限價＝觸發線，D03；A3-M7A-01）
                    split = "registry_rescaled" in result
                    rescaled = []
                    for o in open_orders(conn, ticker):
                        if o["side"] == "BUY":
                            conn.execute("UPDATE orders SET status = 'cancelled', cancel_reason = ?, updated_at = ? "
                                         "WHERE order_id = ?", (f"人工調整股數，需重新規劃：{note}", now, o["order_id"]))
                            result.setdefault("cancelled_orders", []).append(o["order_id"])
                            continue
                        nq = o["qty"] * qty // cur["qty"]
                        if nq <= 0:
                            conn.execute("UPDATE orders SET status = 'cancelled', cancel_reason = ?, updated_at = ? "
                                         "WHERE order_id = ?", (f"人工調整股數後賣單股數為 0：{note}", now, o["order_id"]))
                            result.setdefault("cancelled_orders", []).append(o["order_id"])
                            continue
                        lp = (round(o["limit_price"] * cur["qty"] / qty, 4)
                              if split and o["limit_price"] is not None else o["limit_price"])
                        conn.execute("UPDATE orders SET qty = ?, limit_price = ?, updated_at = ? WHERE order_id = ?",
                                     (nq, lp, now, o["order_id"]))
                        rescaled.append({"order_id": o["order_id"], "qty_before": o["qty"], "qty_after": nq,
                                         "limit_before": o["limit_price"], "limit_after": lp})
                    if rescaled:
                        result["rescaled_sell_orders"] = rescaled
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

        if resolve_corp_action is not None:
            row = conn.execute("SELECT status, data FROM corporate_actions WHERE ca_id = ?",
                               (resolve_corp_action,)).fetchone()
            if row is None:
                raise LedgerError(f"找不到公司行動 ca_id={resolve_corp_action}")
            if row["status"] not in ("pending_human", "anomaly"):
                raise LedgerError(f"公司行動 ca_id={resolve_corp_action} 狀態為 {row['status']}，不需結案")
            data = json.loads(row["data"] or "{}")
            data["manual"] = {"note": note, "date": date, "resolved_at": now, "status_before": row["status"]}
            conn.execute("UPDATE corporate_actions SET status = 'applied', data = ? WHERE ca_id = ?",
                         (json.dumps(data, ensure_ascii=False, default=str), resolve_corp_action))
            result["resolved_corp_action"] = resolve_corp_action

        if unpause is not None:
            rows = conn.execute(
                "SELECT anomaly_id, kind, detail FROM anomalies WHERE status = 'open' AND pause_trading = 1 AND "
                + ("ticker IS NULL AND kind = ?" if unpause == GLOBAL else "ticker = ?"),
                (MANUAL_PAUSE if unpause == GLOBAL else unpause,),
            ).fetchall()
            # 公司行動工作失敗：結案會讓事件永不重派、拆合股永不入帳（detect／unresolved_failed_events 只認 open）
            # → 只解除暫停、保留 open 等重派結案（A3-V3-3-02）
            failed = [r for r in rows if r["kind"] == CA_JOB_FAILED]
            ids = [r["anomaly_id"] for r in rows if r["kind"] != CA_JOB_FAILED]
            pos = result["position"]
            qty_changed = bool(pos and pos["ticker"] == unpause and
                               ((pos["before"] or {}).get("qty") or 0) != ((pos["after"] or {}).get("qty") or 0))
            cash_booked = cash_delta is not None and ticker is not None and ticker == unpause
            if failed and (qty_changed or cash_booked):
                # 同一指令改股數或調該檔現金＋解除暫停＝人工已入帳（例：人工拆股、人工入帳股利）→ 結案，
                # 免得下次 daily 重派再入帳一次
                ids += [r["anomaly_id"] for r in failed]
                result["resolved_job_failed"] = [r["anomaly_id"] for r in failed]
                failed = []
            for aid in ids:
                conn.execute(
                    "UPDATE anomalies SET status = 'resolved', resolved_at = ?, resolution = ? WHERE anomaly_id = ?",
                    (now, note, aid),
                )
            result["resolved"].extend(ids)
            if failed:
                conn.executemany("UPDATE anomalies SET pause_trading = 0 WHERE anomaly_id = ?",
                                 [(r["anomaly_id"],) for r in failed])
                result["unpaused_only"] = [r["anomaly_id"] for r in failed]
                evs = [((json.loads(r["detail"]) if r["detail"] else {}).get("event") or {}).get("event_id") or "?"
                       for r in failed]
                result.setdefault("warnings", []).append(
                    f"{unpause} 公司行動工作失敗事件 {'、'.join(evs)} 尚未入帳：已解除暫停但不結案，下次 daily 仍會重派；"
                    f"若已人工調帳，請以 --resolve {'／'.join(str(r['anomaly_id']) for r in failed)} 結案（結案後不再重派；"
                    "未結案時，重派看到之後的人工改股數會轉人工、不自動入帳）")
            if unpause != GLOBAL:   # 只警示、不擋（M7-D4-04）
                left = [r["ca_id"] for r in conn.execute(
                    "SELECT ca_id FROM corporate_actions WHERE ticker = ? AND status IN ('pending_human', 'anomaly') "
                    "ORDER BY ca_id", (unpause,))]
                if left:   # 附加、不覆蓋前面的股數／工作失敗警示（A3-M7A-05）
                    result.setdefault("warnings", []).append(
                        f"{unpause} 仍有未結公司行動 ca_id={'、'.join(map(str, left))}，"
                        "人工調帳完成後請以 --resolve-corp-action 結案")
    return result
