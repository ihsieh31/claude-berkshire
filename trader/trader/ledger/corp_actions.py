"""公司行動：偵測、覆核、入帳、股利發放與拒收（CONTRACTS_M5 §B）。

原則：覆核寧可擋下也不能錯入帳。覆核不過 → 不動股數與現金，寫異常並暫停該檔。
每筆入帳在單一交易（BEGIN IMMEDIATE）內完成；檔案變更（cap_inputs）在交易內寫入，
交易失敗時還原檔案。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any, Iterator

from trader import db
from trader.data.universe import normalize_ticker
from trader.paths import Paths


class CorpActionError(Exception):
    """交易狀態或輸入不合法。"""


RATIO_TYPES = ("拆股", "合股", "股票股利")       # 有比例、依比例調整股數與每股欄位
PRICE_CHECK_TYPES = RATIO_TYPES                  # 覆核 #3：比例與前後價格比
HUMAN_TYPES = ("分拆", "併購換股", "其他")        # 一律需要人工
NONE_TYPE = "無公司行動"                           # 只限 8-K 事件：不入帳、不暫停
KNOWN_TYPES = (*RATIO_TYPES, "現金股利", "分拆", "併購換股", "代號變更", "其他", NONE_TYPE)
PER_SHARE_REGISTRY = ("auto_buy_cap", "sell_review_above", "absurd_price_line", "cost", "review_price")
PER_SHARE_CAP = ("oe", "norm_eps", "iv_cons", "iv_base", "iv_opt", "price")
PER_SHARE_CAP_NESTED = (("insurance", "tbv_ps"), ("insurance", "float_ps"))
TOL_RATIO_ALPACA = 0.001
TOL_PRICE_RATIO = 0.15        # 有 Alpaca 比例可交叉核對（0.1%）時：只防明顯錯誤，容許當日行情波動
TOL_PRICE_RATIO_8K = 0.08     # 只有 8-K 來源：價格比是唯一比對；8% 可抓到 3 拆 2 與 4 拆 3（差 12.5%）之類的混淆（推論）
TOL_MV = 0.02
DEFAULT_PAY_LAG_DAYS = 30     # 現金股利缺發放日時，以除息日後這麼多天推論發放日（M2）


# ---------------------------------------------------------------- 小工具

def _num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _whole(x: float) -> int:
    """股數取整數下限；先四捨五入到 1e-6，避免浮點誤差把 3.0 變成 2。"""
    return math.floor(round(x, 6))


def _frac(x: float) -> float:
    return round(x - _whole(x), 6)


def _nt(t: Any) -> str:
    return normalize_ticker(str(t))


def _needs_human(result: dict) -> bool:
    return result.get("needs_human") is True or result.get("action_type") in HUMAN_TYPES or \
        result.get("action_type") not in KNOWN_TYPES


def _ca_exists(conn: sqlite3.Connection, ticker: str, eff: str, atype: str) -> bool:
    """去重鍵與資料表唯一鍵一致：(代號, 類型, 生效日)；同日拆股＋股利是兩筆。"""
    return conn.execute(
        "SELECT 1 FROM corporate_actions WHERE ticker=? AND action_type=? AND effective_date=?", (ticker, atype, eff)
    ).fetchone() is not None


def _now() -> str:
    return db.now_iso()


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    """單一寫入交易：正常結束提交，任何例外回滾。"""
    if conn.in_transaction:
        raise CorpActionError("連線已有未提交的交易，拒絕寫入以免部分提交")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class _Files:
    """交易內的檔案變更；交易失敗時依逆序還原。"""

    def __init__(self) -> None:
        self._undo: list[tuple[Path, bytes | None]] = []

    def write_json(self, path: Path, obj: dict) -> None:
        self._undo.append((path, path.read_bytes() if path.exists() else None))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def move(self, src: Path, dst: Path) -> None:
        self._undo.append((src, src.read_bytes()))
        self._undo.append((dst, dst.read_bytes() if dst.exists() else None))
        os.replace(src, dst)

    def restore(self) -> None:
        for path, orig in reversed(self._undo):
            if orig is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(orig)
        self._undo.clear()


def _cap_path(paths: Paths, ticker: str) -> Path:
    return paths.state / "cap_inputs" / f"{ticker}.json"


def position_of(conn: sqlite3.Connection, ticker: str) -> dict | None:
    row = conn.execute("SELECT qty, avg_cost, first_buy_date FROM positions WHERE ticker=?", (ticker,)).fetchone()
    if row is None or int(row["qty"]) <= 0:
        return None
    return {"qty": int(row["qty"]), "avg_cost": float(row["avg_cost"]), "first_buy_date": row["first_buy_date"]}


def open_orders_of(conn: sqlite3.Connection, ticker: str) -> list[dict]:
    rows = conn.execute(
        "SELECT order_id, side, qty, limit_price FROM orders WHERE ticker=? AND status='open' ORDER BY order_id",
        (ticker,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 偵測

def _alpaca_events(alpaca_actions: list[dict], want: set[str], prev_date: str, date: str,
                   bars_prev: dict, bars_today: dict) -> list[dict]:
    out: list[dict] = []
    counter: dict[tuple[str, str], int] = {}
    for a in alpaca_actions:
        T = _nt(a.get("ticker") or "")
        d = a.get("ex_date") or a.get("effective_date")
        if not T or T not in want or not d or not (prev_date < d <= date):
            continue
        raw = a.get("raw") or {}
        rid = raw.get("id")
        if rid is None:
            key = (T, d)
            counter[key] = counter.get(key, 0) + 1
            rid = counter[key]
        out.append(_event(
            T, "alpaca", d, rid, prev_date, date, bars_prev, bars_today,
            text=f"Alpaca 公司行動：類型 {a.get('type')}，代號 {T}，日期 {d}，比例 {a.get('ratio_new_per_old')}，"
                 f"每股現金 {a.get('cash_per_share')}，新代號 {a.get('new_ticker')}，原文 {json.dumps(raw, ensure_ascii=False, default=str)}",
            alpaca=a, url=None,
        ))
    return out


def _8k_events(filings_8k: dict[str, list[dict]], want: set[str], prev_date: str, date: str,
               bars_prev: dict, bars_today: dict) -> list[dict]:
    out: list[dict] = []
    for raw_t, filings in (filings_8k or {}).items():
        T = _nt(raw_t)
        if T not in want:
            continue
        for f in filings or []:
            fd = f.get("filingDate")
            form = f.get("form")
            if not fd or not (prev_date < fd <= date):
                continue
            if form not in (None, "8-K", "8-K/A"):
                continue
            items = [x for x in re.split(r"[,\s;]+", str(f.get("items") or "")) if x]
            if not any(i in ("3.03", "5.03", "8.01") for i in items):
                continue
            acc = f.get("accessionNumber") or f"{len(out) + 1}"
            out.append(_event(
                T, "8k", fd, acc, prev_date, date, bars_prev, bars_today,
                text=f"8-K 項目 {','.join(items)}，申報日 {fd}，申報編號 {acc}",
                alpaca=None, url=f.get("url"),
            ))
    return out


def _close(bars: dict, T: str) -> float | None:
    b = (bars or {}).get(T)
    if isinstance(b, dict):
        return _num(b.get("close"))
    return _num(b)


def _event(T: str, source: str, d: str, seq: Any, prev_date: str, date: str,
           bars_prev: dict, bars_today: dict, *, text: str, alpaca: dict | None, url: str | None) -> dict:
    return {
        "event_id": f"{T}:{source}:{d}:{seq}",
        "ticker": T,
        "source": source,
        "date": d,
        "text": text,
        "alpaca": alpaca,
        "url": url,
        "price_before": _close(bars_prev, T),
        "date_before": prev_date,
        "price_after": _close(bars_today, T),
        "date_after": date,
    }


def detect(conn: sqlite3.Connection, date: str, prev_date: str, tickers: list[str],
           alpaca_actions: list[dict], filings_8k: dict[str, list[dict]],
           bars_prev: dict, bars_today: dict) -> list[dict]:
    """偵測 (prev_date, date] 內的公司行動事件。

    來源：Alpaca 公司行動、8-K（3.03／5.03／8.01）、無事件的價格跳空 ≥ 40%。
    已在 corporate_actions 登錄（同代號同生效日）或同批重複 event_id 的略過。
    """
    want = {_nt(t) for t in tickers}
    known = _alpaca_events(alpaca_actions, want, prev_date, date, bars_prev, bars_today)
    known += _8k_events(filings_8k, want, prev_date, date, bars_prev, bars_today)

    events = list(known)
    have_known = {e["ticker"] for e in known}
    for T in sorted(want - have_known):
        pb, pa = _close(bars_prev, T), _close(bars_today, T)
        if not pb or pa is None or pb <= 0:
            continue
        if abs(pa / pb - 1.0) >= 0.40:
            events.append(_event(
                T, "gap", date, 1, prev_date, date, bars_prev, bars_today,
                text=f"無公司行動事件的價格跳空：{prev_date} 收盤 {pb} → {date} 收盤 {pa}，變動 {pa / pb - 1.0:+.1%}",
                alpaca=None, url=None,
            ))

    rows = conn.execute("SELECT ticker, effective_date, data FROM corporate_actions").fetchall()
    registered = {(r["ticker"], r["effective_date"]) for r in rows}
    registered_ids = {((json.loads(r["data"]) if r["data"] else {}).get("event") or {}).get("event_id") for r in rows}
    out: list[dict] = []
    seen: set[str] = set()
    for e in events:
        # Alpaca 事件逐筆比對來源事件（同日可有多筆不同類型）；8-K 與跳空：同日已登錄任一行動即視為已解釋
        done = e["event_id"] in registered_ids if e["source"] == "alpaca" else (e["ticker"], e["date"]) in registered
        if done or e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        out.append(e)
    return out


# ---------------------------------------------------------------- 工作失敗的未解決事件（H2）

JOB_FAILED_KIND = "corp_action_job_failed"


def _is_registered(conn: sqlite3.Connection, e: dict) -> bool:
    """事件是否已入 corporate_actions（判斷方式與 detect 相同）。"""
    rows = conn.execute("SELECT ticker, effective_date, data FROM corporate_actions").fetchall()
    if e.get("source") == "alpaca":
        return any(((json.loads(r["data"]) if r["data"] else {}).get("event") or {}).get("event_id") == e.get("event_id")
                   for r in rows)
    return any(r["ticker"] == e.get("ticker") and r["effective_date"] == e.get("date") for r in rows)


def record_job_failure(conn: sqlite3.Connection, event: dict, job_id: str, error: str | None) -> bool:
    """CORP_ACTION 工作失敗（傳輸錯誤、額度用完、預算耗盡…）：寫暫停該檔的異常，並把事件留到下次 daily 重派。
    同一事件已有未結異常則不重複寫。回傳是否新寫。"""
    T = _nt(event["ticker"])
    for r in conn.execute("SELECT detail FROM anomalies WHERE ticker=? AND kind=? AND status='open'", (T, JOB_FAILED_KIND)):
        if ((json.loads(r["detail"]) if r["detail"] else {}).get("event") or {}).get("event_id") == event.get("event_id"):
            return False
    detail = {"event": event, "job_id": job_id, "error": error}
    with _tx(conn):
        conn.execute(
            "INSERT INTO anomalies(ticker, kind, detail, status, pause_trading, created_at) VALUES (?,?,?,?,1,?)",
            (T, JOB_FAILED_KIND, json.dumps(detail, ensure_ascii=False, default=str), "open", _now()))
    return True


def unresolved_failed_events(conn: sqlite3.Connection) -> list[dict]:
    """先前工作失敗、至今仍未入帳的事件（不受 (prev_date, date] 窗口限制）。已入帳者順便結案（解除暫停）。"""
    out: list[dict] = []
    rows = conn.execute("SELECT anomaly_id, detail FROM anomalies WHERE kind=? AND status='open' ORDER BY anomaly_id",
                        (JOB_FAILED_KIND,)).fetchall()
    for r in rows:
        ev = (json.loads(r["detail"]) if r["detail"] else {}).get("event")
        if not isinstance(ev, dict) or not ev.get("ticker"):
            continue
        if _is_registered(conn, ev):
            with _tx(conn):
                conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? WHERE anomaly_id=?",
                             (_now(), "事件已由後續公司行動工作入帳或轉人工（另有異常）", r["anomaly_id"]))
        else:
            out.append(ev)
    return out


# ---------------------------------------------------------------- 覆核

def verify(result: dict, event: dict, position: dict | None) -> list[str]:
    """回傳錯誤清單；空清單代表通過。position 為 {qty, avg_cost} 或 None。"""
    if _needs_human(result):
        return [f"需要人工：類型 {result.get('action_type')}，needs_human={result.get('needs_human')}"]

    errs: list[str] = []
    T = _nt(event.get("ticker") or "")
    if _nt(result.get("ticker") or "") != T:
        errs.append(f"代號與事件不符：結果 {result.get('ticker')}，事件 {T}")

    atype = result.get("action_type")
    if atype == NONE_TYPE:
        # 只有 8-K 事件可判「無公司行動」；Alpaca 已登錄的行動或不明跳空判無 → 與資料矛盾，轉人工
        if event.get("source") != "8k":
            errs.append(f"事件來源 {event.get('source')} 已顯示有公司行動或異常跳空，不可判為無公司行動")
        if any(_num(result.get(k)) for k in ("ratio_new_per_old", "cash_per_share")) or result.get("new_ticker"):
            errs.append("判為無公司行動卻填了比例、現金或新代號（自相矛盾）")
        return errs
    eff = result.get("effective_date")
    r = _num(result.get("ratio_new_per_old"))
    cps = _num(result.get("cash_per_share"))
    fh = result.get("fractional_handling")
    alp = event.get("alpaca") or {}
    pb, pa = _num(event.get("price_before")), _num(event.get("price_after"))
    qty = int(position["qty"]) if position and int(position.get("qty") or 0) > 0 else 0

    if not eff:
        errs.append("缺生效日")

    # 1 類型與必要欄位
    if atype in RATIO_TYPES and (r is None or r <= 0):
        errs.append("缺比例或比例不合理")
    if atype == "現金股利" and (cps is None or cps <= 0):
        errs.append("現金股利缺每股現金或不為正")
    if atype == "代號變更" and not result.get("new_ticker"):
        errs.append("代號變更缺新代號")

    # 2 與 Alpaca 一致
    ar = _num(alp.get("ratio_new_per_old"))
    if ar is not None and ar > 0:
        if r is None or abs(r / ar - 1.0) > TOL_RATIO_ALPACA:
            errs.append(f"比例與 Alpaca 不一致：結果 {r}，Alpaca {ar}")
    acash = _num(alp.get("cash_per_share"))
    if acash is not None and acash > 0:
        if cps is None or abs(cps / acash - 1.0) > TOL_RATIO_ALPACA:
            errs.append(f"現金與 Alpaca 不一致：結果 {cps}，Alpaca {acash}")

    # 3 比例與前後價格比
    if atype in PRICE_CHECK_TYPES and r is not None and r > 0:
        if not pb or not pa or pb <= 0 or pa <= 0:
            errs.append("缺前後價格，無法覆核")
        elif abs((pb / pa) / r - 1.0) > (TOL_PRICE_RATIO if _num(alp.get("ratio_new_per_old")) else TOL_PRICE_RATIO_8K):
            errs.append(f"比例與前後價格比不符：比例 {r}，價格比 {pb / pa:.4f}")

    # 零股處理：有零股時只接受明確的處理方式
    frac_exists = atype in RATIO_TYPES and r and qty > 0 and _frac(qty * r) > 1e-6
    if frac_exists and fh not in ("現金了結", "無條件捨去"):
        errs.append(f"零股處理不明（{fh}），無法覆核")

    # 4 市值守恆（只限比例類；現金股利的除息價變動不在此檢查）
    if atype in RATIO_TYPES and r and qty > 0:
        if not pb or pb <= 0:
            errs.append("缺前後價格，無法覆核")
        else:
            pre = qty * pb
            cash_frac = 0.0
            if fh == "現金了結" and _frac(qty * r) > 0:
                if not pa or pa <= 0:
                    errs.append("缺前後價格，無法覆核")
                else:
                    cash_frac = _frac(qty * r) * pa
            post = _whole(qty * r) * (pb / r) + cash_frac + qty * (cps or 0.0)
            if pre > 0 and abs(post / pre - 1.0) > TOL_MV:
                errs.append(f"市值不守恆：調整前 {pre:.2f}，調整後 {post:.2f}")

    return errs


# ---------------------------------------------------------------- 入帳

def _cash(conn: sqlite3.Connection, date: str, amount: float, kind: str, ref: str | None, note: str | None) -> None:
    conn.execute("INSERT INTO cash_ledger(date, amount, kind, ref, note) VALUES (?,?,?,?,?)",
                 (date, round(amount, 2), kind, ref, note))


def _ca_insert(conn: sqlite3.Connection, *, ticker: str, atype: str, eff: str, ratio: float | None,
               cash: float | None, new_ticker: str | None, status: str, source: str | None, data: dict) -> None:
    conn.execute(
        "INSERT INTO corporate_actions(ticker, action_type, effective_date, ratio_new_per_old, cash_per_share, "
        "new_ticker, status, source, data, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ticker, atype, eff, ratio, cash, new_ticker, status, source, json.dumps(data, ensure_ascii=False, default=str), _now()),
    )


def _scale_registry(conn: sqlite3.Connection, ticker: str, r: float) -> None:
    row = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (ticker,)).fetchone()
    if row is None:
        return
    data = json.loads(row["data"] or "{}")
    for k in PER_SHARE_REGISTRY:
        v = _num(data.get(k))
        if v is not None:
            data[k] = round(v / r, 6)
    conn.execute("UPDATE registry SET data=?, updated_at=? WHERE ticker=?",
                 (json.dumps(data, ensure_ascii=False), _now(), ticker))


def _scale_cap(paths: Paths, files: _Files, ticker: str, r: float) -> None:
    p = _cap_path(paths, ticker)
    if not p.is_file():
        return
    x = json.loads(p.read_text(encoding="utf-8"))
    for k in PER_SHARE_CAP:
        v = _num(x.get(k))
        if v is not None:
            x[k] = round(v / r, 6)
    for outer, k in PER_SHARE_CAP_NESTED:
        sub = x.get(outer)
        if isinstance(sub, dict):
            v = _num(sub.get(k))
            if v is not None:
                sub[k] = round(v / r, 6)
    files.write_json(p, x)


def _apply_ratio(conn: sqlite3.Connection, paths: Paths, files: _Files, result: dict, event: dict,
                 T: str, eff: str, r: float, cps: float | None) -> None:
    """拆股、合股、股票股利：股數、成本、委託、登錄表、cap_inputs，以及零股與現金對價。"""
    pa = _num(event.get("price_after"))
    fh = result.get("fractional_handling")
    pos = position_of(conn, T)
    if pos is not None:
        qty = pos["qty"]
        n = _whole(qty * r)
        frac = _frac(qty * r)
        ref = f"CA:{T}:{result['action_type']}:{eff}"
        if fh == "現金了結" and frac > 0:
            if not pa or pa <= 0:
                raise CorpActionError("現金了結零股需要 price_after")
            _cash(conn, eff, frac * pa, "corp_action_cash", ref, "零股現金了結")
        if cps:
            _cash(conn, eff, qty * cps, "corp_action_cash", ref, "現金對價")
        if n > 0:
            conn.execute("UPDATE positions SET qty=?, avg_cost=?, updated_at=? WHERE ticker=?",
                         (n, pos["avg_cost"] * qty / n, _now(), T))
        else:
            conn.execute("DELETE FROM positions WHERE ticker=?", (T,))
            conn.execute("UPDATE registry SET status='WATCH', updated_at=? WHERE ticker=? AND status='HOLDING'",
                         (_now(), T))

    for o in open_orders_of(conn, T):
        nq = _whole(o["qty"] * r)
        if nq <= 0:
            conn.execute("UPDATE orders SET status='cancelled', cancel_reason=?, updated_at=? WHERE order_id=?",
                         ("公司行動後股數為零", _now(), o["order_id"]))
        else:
            lp = round(o["limit_price"] / r, 4) if o["limit_price"] is not None else None
            conn.execute("UPDATE orders SET qty=?, limit_price=?, updated_at=? WHERE order_id=?",
                         (nq, lp, _now(), o["order_id"]))

    _scale_registry(conn, T, r)
    _scale_cap(paths, files, T, r)


def _apply_cash_dividend(conn: sqlite3.Connection, result: dict, event: dict, T: str, eff: str,
                         cps: float) -> dict:
    pos = position_of(conn, T)
    amount = round(pos["qty"] * cps, 2) if pos else 0.0
    alp = event.get("alpaca") or {}
    pay = result.get("pay_date") or alp.get("payable_date")
    inferred = not pay
    if inferred:  # 保守：晚於常見的 2–4 週發放，寧可晚入帳也不憑空提早；標記「推論」供人工核對
        pay = (_date.fromisoformat(eff) + timedelta(days=DEFAULT_PAY_LAG_DAYS)).isoformat()
    ref = f"CA:{T}:{result['action_type']}:{eff}"
    if amount > 0:
        _cash(conn, eff, amount, "dividend_receivable", ref, "除息應收" + ("（發放日推論）" if inferred else ""))
    return {"pay_date": pay, "pay_date_inferred": inferred, "amount": amount, "paid": False, "ref": ref}


def apply(conn: sqlite3.Connection, paths: Paths, result: dict, event: dict, date: str) -> dict:
    """覆核通過後入帳。回傳 {status: applied|duplicate|conflict, ...}。

    conflict（代號變更時新代號已存在）不寫任何東西，由呼叫端改走 reject。
    """
    T = _nt(event["ticker"])
    eff = result["effective_date"]
    atype = result["action_type"]
    if atype == NONE_TYPE:  # 不寫 corporate_actions（避免占用同代號同日的去重鍵），只回報
        return {"status": "no_action", "ticker": T, "effective_date": eff}
    r = _num(result.get("ratio_new_per_old"))
    cps = _num(result.get("cash_per_share"))
    new_t = _nt(result["new_ticker"]) if result.get("new_ticker") else None
    files = _Files()
    try:
        with _tx(conn):
            if _ca_exists(conn, T, eff, atype):
                return {"status": "duplicate", "ticker": T, "effective_date": eff}

            data: dict[str, Any] = {"result": result, "event": event}
            if atype in RATIO_TYPES:
                if not r or r <= 0:
                    raise CorpActionError("比例類公司行動缺比例")
                _apply_ratio(conn, paths, files, result, event, T, eff, r, cps)
                ratio, cash, nt_ = r, cps, None
            elif atype == "現金股利":
                data.update(_apply_cash_dividend(conn, result, event, T, eff, cps))
                ratio, cash, nt_ = None, cps, None
            elif atype == "代號變更":
                if new_t is None:
                    raise CorpActionError("代號變更缺新代號")
                if (conn.execute("SELECT 1 FROM registry WHERE ticker=?", (new_t,)).fetchone()
                        or conn.execute("SELECT 1 FROM positions WHERE ticker=?", (new_t,)).fetchone()
                        or _cap_path(paths, new_t).exists()):
                    return {"status": "conflict", "ticker": T,
                            "errors": [f"新代號 {new_t} 已存在於登錄表、持股或 cap_inputs，不入帳"]}
                conn.execute("UPDATE positions SET ticker=?, updated_at=? WHERE ticker=?", (new_t, _now(), T))
                conn.execute("UPDATE orders SET ticker=?, updated_at=? WHERE ticker=? AND status='open'",
                             (new_t, _now(), T))
                conn.execute("UPDATE registry SET ticker=?, updated_at=? WHERE ticker=?", (new_t, _now(), T))
                src = _cap_path(paths, T)
                if src.is_file():
                    files.move(src, _cap_path(paths, new_t))
                ratio, cash, nt_ = None, None, new_t
            else:
                raise CorpActionError(f"此類型不可自動入帳：{atype}")

            _ca_insert(conn, ticker=T, atype=atype, eff=eff, ratio=ratio, cash=cash, new_ticker=nt_,
                       status="applied", source=event.get("source"), data=data)
    except BaseException:
        files.restore()
        raise
    return {"status": "applied", "ticker": T, "effective_date": eff, "action_type": atype}


def settle_dividends(conn: sqlite3.Connection, date: str) -> list[dict]:
    """發放到期（pay_date ≤ date）且尚未發放的現金股利：dividend +X、dividend_receivable −X。每列一筆交易。"""
    rows = conn.execute(
        "SELECT ca_id, ticker, effective_date, data FROM corporate_actions "
        "WHERE status='applied' AND action_type='現金股利' ORDER BY ca_id"
    ).fetchall()
    paid: list[dict] = []
    for row in rows:
        data = json.loads(row["data"] or "{}")
        pay = data.get("pay_date")
        if data.get("paid"):
            continue
        if not pay:  # 舊列（M2 修正前入帳）沒有發放日：同樣以推論日期處理
            pay = (_date.fromisoformat(row["effective_date"]) + timedelta(days=DEFAULT_PAY_LAG_DAYS)).isoformat()
            data["pay_date"], data["pay_date_inferred"] = pay, True
        if pay > date:
            continue
        amount = float(data.get("amount") or 0.0)
        T, eff = row["ticker"], row["effective_date"]
        with _tx(conn):
            if amount > 0:
                ref = data.get("ref") or f"CA:{T}:現金股利:{eff}"
                _cash(conn, date, amount, "dividend", ref, "股利發放" + ("（發放日推論）" if data.get("pay_date_inferred") else ""))
                _cash(conn, date, -amount, "dividend_receivable", ref, "股利發放沖銷應收")
            data["paid"] = True
            conn.execute("UPDATE corporate_actions SET data=? WHERE ca_id=?",
                         (json.dumps(data, ensure_ascii=False, default=str), row["ca_id"]))
        paid.append({"ticker": T, "effective_date": eff, "pay_date": pay, "amount": amount,
                     **({"pay_date_inferred": True} if data.get("pay_date_inferred") else {})})
    return paid


def reject(conn: sqlite3.Connection, result: dict | None, event: dict, errors: list[str], *,
           kind: str = "corp_action_check_failed", extra: dict | None = None) -> None:
    """拒收：寫異常（暫停該檔）與公司行動列。不動股數與現金；需要人工者狀態為 pending_human。"""
    T = _nt(event["ticker"])
    eff = (result or {}).get("effective_date") or event.get("date")
    atype = (result or {}).get("action_type") or "未知"
    human = bool(result) and _needs_human(result)
    status = "pending_human" if human else "anomaly"
    ratio = _num((result or {}).get("ratio_new_per_old"))
    cash = _num((result or {}).get("cash_per_share"))
    nt_ = _nt(result["new_ticker"]) if (result or {}).get("new_ticker") else None
    detail = {"event": event, "result": result, "errors": list(errors), **(extra or {})}
    with _tx(conn):
        conn.execute(
            "INSERT INTO anomalies(ticker, kind, detail, status, pause_trading, created_at) VALUES (?,?,?,?,1,?)",
            (T, kind, json.dumps(detail, ensure_ascii=False, default=str), "open", _now()),
        )
        if eff and not _ca_exists(conn, T, eff, atype):
            _ca_insert(conn, ticker=T, atype=atype, eff=eff, ratio=ratio, cash=cash, new_ticker=nt_,
                       status=status, source=event.get("source"), data={"result": result, "event": event,
                                                                        "errors": list(errors)})
            # 需要人工者一律不自動入帳（含現金對價）：股數未調整前先入現金會重複計價；
            # 該檔已暫停，現金對價記在異常 detail，由人工 `trader ledger adjust` 一次入帳


def on_invalid(conn: sqlite3.Connection, job_id: str, ticker: str, asof: str, event: dict | None,
               errors: list[str]) -> None:
    """LLM 輸出重試後仍不合格：寫異常並暫停，公司行動列 status=anomaly（鍵用 event.date）。"""
    ev = {"ticker": ticker, "date": asof, **(event or {})}
    reject(conn, None, ev, errors, kind="corp_action_invalid_output", extra={"job_id": job_id})
