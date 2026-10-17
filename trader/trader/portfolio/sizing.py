"""組合層部位規模：只回傳計畫（dict 列表），不下單、不寫委託。下單與寫入由 pipelines/daily.py 負責。

規則來源：SKILL §5–§6；procedure/06（P-28、P-33、P-36、P-37、P-38、P-43、P-44）；
procedure/12（三閘門、加碼許可）；plan.md §8。

計畫格式：{ticker, side, qty, limit_price, decision_id, reason, notes, action, review_type, ...}。
qty 為 0 的項目是「跳過」，附理由於 reason／notes，不會下單（方便追溯為何沒買或沒賣）。

標示規則：
- 「推論」＝本實作的近似或補強，規則文字沒有直接給出，程式註解與回報中都要說明。
- 「近似」＝以曆日代替交易日等。
"""
from __future__ import annotations

import json
import math
from datetime import date as _date
from pathlib import Path
from typing import Any

from .. import db
from ..ledger import book

GRADE_CAP = {"A": 0.15, "B": 0.10}   # P-28 宿主上限；C、D 不開倉（上限視為 0）
MIN_T = 0.03                          # P-33：目標權重 < 3% 不建倉
CA_INVALIDATES_CAP = ("分拆", "併購換股", "其他")   # 這些公司行動之後，每股上限不能沿用
MAX_HOLDINGS = 10                     # P-34：≥10 檔時不建新部位（上限 12，保留 2 空名額）
MAX_NEW_PER_DAY = 4                   # P-44
CASH_FLOOR = 0.05                     # P-35：現金下限 5% 權益
ADV_FRAC = 0.01                       # P-38：部位金額 ≤ 平均日成交額 1%
BUILD_CAL_DAYS = 21                   # P-37：1–15 交易日窗口；推論：以 21 曆日近似 15 交易日（忽略假日）
DEFAULT_TRANCHES = 3                  # 剩餘批數預設（plan.md §8 與 build_plan.tranches_left）
NO_ADD_ABOVE = 0.15                   # 權重 > 15% 不加碼（P-43）
CANCEL_ABOVE_CAP_DAYS = 10            # 連 10 日高於上限 → 取消剩餘批次（P-37）
ONE_YEAR_DAYS = 365                   # 建倉滿一年（曆日）
GATE_LOOKBACK_DAYS = 365              # 加碼許可：三閘門紀錄需在 365 天內
ADV_DAYS = 20                         # 平均日成交額取 20 個交易日（呼叫端以 40 曆日抓資料後取末 20 筆）
GATE_KEYS = ("gate1_not_deleted", "gate2_price_below_cap", "gate3_pass")
PRICE_TRIGGER = "PRICE"


# ---------------------------------------------------------------- 純函式（不讀資料庫）

def _truthy(v: Any) -> bool:
    """閘門值是否為真。字串只接受明確的「真」字樣；placeholder（如 'true | false'）視為假。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "通過", "yes", "1")
    return False


def gate_failures(log: dict | None, card: dict | None) -> list[str]:
    """三閘門（procedure/12 §1）全過回傳空清單。以決策日誌為準；日誌缺欄位時退回訊號卡的 auto_buy_gates。"""
    log = log or {}
    ag = (card or {}).get("auto_buy_gates") or {}
    checks = (
        ("gate1_not_deleted", "gate1_not_deleted", "閘門1（初篩未刪）"),
        ("gate2_price_below_cap", "gate2_price_below_cap", "閘門2（現價低於上限）"),
        ("gate3_pass", "gate3_questionnaire_pass", "閘門3（能力圈問卷）"),
    )
    out = []
    for log_key, card_key, name in checks:
        v = log.get(log_key)
        if v is None:
            v = ag.get(card_key)
        if not _truthy(v):
            out.append(f"{name}未過")
    return out


def target_weight(card: dict, user_cap: float | None) -> tuple[float, list[str]]:
    """T = min(目標權重, 等級上限, 非空的 max_weight_pct 各項, 使用者上限)；T < 3% → 0。

    目標權重取訊號卡 target_weight_pct，缺時退回 position_plan.target_weight_pct。
    """
    notes: list[str] = []
    raw = card.get("target_weight_pct")
    if raw is None:
        raw = (card.get("position_plan") or {}).get("target_weight_pct")
    if raw is None:
        return 0.0, ["缺目標權重（target_weight_pct）"]
    grade = card.get("certainty_grade")
    caps = [float(raw) / 100.0, GRADE_CAP.get(grade, 0.0)]
    if grade not in GRADE_CAP:
        notes.append(f"等級 {grade!r} 不開倉（上限 0）")
    for v in (card.get("max_weight_pct") or {}).values():
        if v is not None:
            caps.append(float(v) / 100.0)
    if user_cap is not None:
        caps.append(float(user_cap))
    t = min(caps)
    if t < MIN_T:
        notes.append(f"目標權重 {t:.2%} < 3%，不建倉（P-33）")
        return 0.0, notes
    return t, notes


def buy_limit(quote: float | None, cap: float | None, slippage_bps: float) -> float | None:
    """限價 = min(上限 × 0.9999, 報價 ×（1＋滑價）)，無條件捨去到分。

    推論：規則寫 round(…, 2)，但四捨五入可能進位到等於上限；此處改用捨去，確保嚴格低於上限。
    """
    if quote is None or cap is None or quote <= 0 or cap <= 0:
        return None
    raw = min(cap * 0.9999, quote * (1 + slippage_bps / 1e4))
    lim = math.floor(raw * 100 + 1e-9) / 100
    if lim <= 0 or lim >= cap:
        return None
    return lim


def build_amount(t: float, equity: float, mv: float, tranches_left: int) -> float:
    """加碼批次金額 = (T × 權益 − 目前市值) ÷ 剩餘批數（plan.md §8）。"""
    if tranches_left <= 0:
        return 0.0
    return max(0.0, (t * equity - mv) / tranches_left)


def live_g8(cap_dir: Path | None, ticker: str, quote: float | None) -> float | None:
    """以 cap_inputs 換上當日報價重算 G8 調整後預期報酬（與 FULL_B、比較檔同一算法）；缺檔、缺價或算不出 → None。"""
    if cap_dir is None or not quote:
        return None
    f = Path(cap_dir) / f"{ticker}.json"
    if not f.is_file():
        return None
    from ..analysis.checks import _round, _skill_module, cap_input_problems
    from .holdings_view import self_adjusted
    try:
        x = {**json.loads(f.read_text(encoding="utf-8")), "price": float(quote)}
        if cap_input_problems(x):
            return None
        return self_adjusted(x, _round(_skill_module("cap_calc").calc(x)))
    except Exception:  # noqa: BLE001 - 算不出就退回下一層排序
        return None


def _rank_key(card: dict, quote: float | None, live: float | None = None) -> tuple:
    """排序（名額或現金不足時使用）：G8 調整後預期報酬由高到低。

    同一層才可比：第 0 層用當日報價重算的值（live_g8；新舊卡都有 cap_inputs 就都在這層）；
    沒有 cap_inputs 時退回訊號卡存的 g8_adjusted_expected_return（分析日價格）；
    都沒有 → 第 1 層，price_to_conservative 升冪（推論：價格相對保守價值越低越優先；即時報價 ÷ IV_cons 優先）。
    """
    g8 = live if live is not None else card.get("g8_adjusted_expected_return")
    if g8 is not None:
        return (0, -float(g8))
    cons = (card.get("value_per_share") or {}).get("conservative")
    if quote and cons:
        return (1, quote / float(cons))
    p = card.get("price_to_conservative")
    return (1, float(p) if p is not None else math.inf)


def _has_g8(card: dict) -> bool:
    return card.get("g8_adjusted_expected_return") is not None


def _plan(ticker, side, qty, limit, decision_id, action, reason, notes=None, review_type=None,
          cancel_buys=None) -> dict:
    return {"ticker": ticker, "side": side, "qty": int(qty), "limit_price": limit, "decision_id": decision_id,
            "action": action, "reason": reason, "notes": list(notes or []), "review_type": review_type,
            "cancel_buys": list(cancel_buys or [])}


# ---------------------------------------------------------------- 讀資料庫的輔助

def _reg_data(conn, ticker: str) -> dict | None:
    row = conn.execute("SELECT data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return None
    return json.loads(row["data"]) if row["data"] else {}


def merge_registry(conn, ticker: str, patch: dict, date: str) -> bool:
    """合併登錄表 data（不清掉其他欄位）。列不存在回傳 False。"""
    row = conn.execute("SELECT status, data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return False
    data = {**(json.loads(row["data"]) if row["data"] else {}), **patch}
    with book.transaction(conn):
        conn.execute("UPDATE registry SET data = ?, updated_at = ? WHERE ticker = ?",
                     (json.dumps(data, ensure_ascii=False), db.now_iso(), ticker))
    return True


def latest_card(conn, ticker: str) -> dict | None:
    row = conn.execute("SELECT data FROM signals WHERE ticker = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                       (ticker,)).fetchone()
    return json.loads(row["data"]) if row else None


def load_decisions(conn, asof: str, since: str | None = None, late_after: str | None = None) -> list[dict]:
    """決策日 ∈ (since, asof] 的所有決策（since 為 None 時只取 asof 當日），附各檔最新訊號卡。

    since 用前一交易日：非交易日（週末、假日）做出的決策由下一個交易日的收盤流程接手。
    late_after（UTC 時戳，上次每日流程完成時間）：另取決策日 ≤ since、但在該時戳之後才寫入的決策
    （例：等待同意數日後才跑完的完整分析，決策日仍是分析的資料日）。
    每筆：{decision_id, ticker, action, review_type, log, card}。
    """
    lo = since if since is not None else _prev_day(asof)
    sql = "SELECT decision_id, ticker, data FROM decisions WHERE (decision_date > ? AND decision_date <= ?)"
    args: list = [lo, asof]
    if late_after:
        sql += " OR (decision_date <= ? AND created_at > ?)"
        args += [lo, late_after]
    rows = conn.execute(sql + " ORDER BY decision_date, created_at, rowid", args).fetchall()
    out = []
    for r in rows:
        log = json.loads(r["data"]) if r["data"] else {}
        t = r["ticker"]
        out.append({"decision_id": r["decision_id"], "ticker": t, "action": log.get("action"),
                    "review_type": log.get("review_type"), "log": log, "card": latest_card(conn, t)})
    return out


def _last_per_ticker(items: list[dict]) -> list[dict]:
    last: dict[str, dict] = {}
    for it in items:
        last[it["ticker"]] = it
    return list(last.values())


def _open_buys_by_ticker(conn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for o in book.open_orders(conn):
        if o["side"] == "BUY":
            out.setdefault(o["ticker"], []).append(o["order_id"])
    return out


def _avail_cash(conn, equity: float) -> float:
    """可動用現金 = 現金 − 5% 權益 − 已承諾買單（P-35）。"""
    return book.cash(conn) - CASH_FLOOR * equity - book.committed_buy_value(conn)


def _latest_gate_decision(conn, ticker: str, asof: str) -> dict | None:
    """最近一次「有閘門紀錄」的決策（閘門值為 bool 或字串即視為有紀錄）。"""
    rows = conn.execute(
        "SELECT decision_id, decision_date, data FROM decisions WHERE ticker = ? AND decision_date <= ? "
        "ORDER BY decision_date DESC, created_at DESC, rowid DESC", (ticker, asof)).fetchall()
    for r in rows:
        log = json.loads(r["data"]) if r["data"] else {}
        if any(isinstance(log.get(k), (bool, str)) for k in GATE_KEYS):
            return {"decision_id": r["decision_id"], "decision_date": r["decision_date"], "log": log}
    return None


def add_blocked(conn, ticker: str, asof: str) -> str | None:
    """停止加碼（procedure/07 §6、rulebook §9：疑似 → P-50 觀察、停止加碼）：取最近一筆 THESIS 或 HOLD_REVIEW 決策。
    THESIS 疑似／證偽，或 HOLD_REVIEW 的 stop_adding 為真 → 回傳理由；否則 None。
    另：該檔有未結的強模型同意列（M6：待同意、延後、已同意未完成）→ 暫停加碼。"""
    from ..jobs.approvals import add_block_reason
    why = add_block_reason(conn, ticker)
    if why:
        return why
    rows = conn.execute(
        "SELECT data FROM decisions WHERE ticker = ? AND decision_date <= ? "
        "ORDER BY decision_date DESC, created_at DESC, rowid DESC", (ticker, asof)).fetchall()
    for r in rows:
        log = json.loads(r["data"]) if r["data"] else {}
        rt = log.get("review_type")
        if rt == "THESIS":
            return "論點疑似或證偽" if log.get("thesis_check") in ("疑似", "證偽") else None
        if rt == "HOLD_REVIEW":
            if not log.get("stop_adding"):
                return None
            # 觀察期滿也不自動恢復：須有新的複查結論（THESIS 完好或 HOLD_REVIEW 不停止加碼）
            return f"持股複查決定停止加碼（觀察至 {log.get('observe_until') or '未定'}）"
    return None


def _prev_day(d: str) -> str:
    from datetime import timedelta
    return (_date.fromisoformat(d) - timedelta(days=1)).isoformat()


def _days_between(later: str, earlier: str) -> int:
    return (_date.fromisoformat(later) - _date.fromisoformat(earlier)).days


# ---------------------------------------------------------------- 對外：計畫

def plan_buys(conn, asof, decisions: list[dict], quotes: dict[str, float],
              adv: dict[str, float], slippage_bps: float, cap_dir: Path | None = None) -> list[dict]:
    """BUY／ADD 的計畫。decisions 為 load_decisions 的結果（可含其他 action，會被忽略）。

    quotes：當日收盤（參考報價；推論：實際下單參考價以收盤代替）。adv：20 日平均成交額（美元）。
    新部位（未持有）的金額 = 首批比例 × T × 權益；ADD（已持有）依建倉計畫的批次公式。
    cap_dir：state/cap_inputs（排序用當日報價重算 G8；None 時只用訊號卡存的值）。
    """
    closes = {t: float(p) for t, p in quotes.items() if p is not None}
    equity = book.equity(conn, closes)
    avail = _avail_cash(conn, equity)
    held = book.positions(conn)
    paused = book.paused(conn)
    open_buys = _open_buys_by_ticker(conn)

    cands = [d for d in _last_per_ticker(decisions) if d.get("action") in ("BUY", "ADD")]
    cands.sort(key=lambda d: _rank_key(d.get("card") or {}, quotes.get(d["ticker"]),
                                       live_g8(cap_dir, d["ticker"], quotes.get(d["ticker"]))))
    plans: list[dict] = []
    n_new = 0

    for d in cands:
        t, card, log = d["ticker"], d.get("card") or {}, d.get("log") or {}
        is_new = t not in held
        act = "BUY" if is_new else "ADD"
        def skip(why: str) -> None:
            plans.append(_plan(t, "BUY", 0, None, d["decision_id"], act, why, review_type=d.get("review_type")))


        if t in paused:
            skip("暫停交易，拒單"); continue
        if t in open_buys:
            skip("已有未成交買單，不重複下單"); continue
        fails = gate_failures(log, card)
        if fails:
            skip("三閘門未全過：" + "、".join(fails)); continue
        if d.get("action") == "ADD" and is_new:
            skip("ADD 但未持有，拒絕（應以 BUY 處理）"); continue
        if not is_new:
            why = add_blocked(conn, t, asof)
            if why:
                skip(f"停止加碼：{why}"); continue
        cap = card.get("auto_buy_cap")
        quote = quotes.get(t)
        if cap is None:
            skip("訊號卡缺 auto_buy_cap"); continue
        # 拆合股只換算登錄表與 cap_inputs（訊號卡是當時的紀錄）：兩者取低，避免用拆股前的每股上限
        rcap = (_reg_data(conn, t) or {}).get("auto_buy_cap")
        if isinstance(rcap, (int, float)) and rcap < cap:
            cap = rcap
        if quote is None or quote <= 0:
            skip("缺當日報價"); continue
        if not quote * (1 + slippage_bps / 1e4) < cap:
            skip("報價（含滑價）未低於自動買進上限"); continue
        limit = buy_limit(quote, cap, slippage_bps)
        if limit is None:
            skip("無法產生合法限價（需嚴格低於上限）"); continue
        a = adv.get(t)
        if not a or a <= 0:
            skip("缺 20 日平均成交額，無法檢查流動性（P-38）"); continue

        notes: list[str] = []
        if not _has_g8(card):
            notes.append("推論：缺 G8 欄位，改以 price_to_conservative 排序")

        if is_new:
            if len(held) + n_new >= MAX_HOLDINGS:
                skip(f"持股已達 {MAX_HOLDINGS} 檔，不建新部位：需勝最弱持股（G8），留待人工"); continue
            if n_new >= MAX_NEW_PER_DAY:
                skip(f"當日新部位已達 {MAX_NEW_PER_DAY} 檔（P-44）"); continue
            t_w, tnotes = target_weight(card, None)
            notes += tnotes
            if t_w <= 0:
                skip("目標權重為 0，不建倉"); continue
            pct = (card.get("position_plan") or {}).get("first_tranche_pct_of_target") or \
                card.get("first_tranche_pct_of_target") or 50
            amount = float(pct) / 100.0 * t_w * equity
            target_w = t_w
        else:
            reg = _reg_data(conn, t) or {}
            bp = reg.get("build_plan") or {}
            mv = held[t]["qty"] * (quote or 0)
            if equity <= 0 or mv / equity > NO_ADD_ABOVE:
                skip("現有權重超過 15%，不加碼（P-43）"); continue
            tl = int(bp.get("tranches_left", 0))
            if bp.get("target_weight") and tl > 0:
                target_w = float(bp["target_weight"])
            else:
                # 建倉期已結束（PRICE 判定權重未達目標的加碼）：目標取訊號卡，缺口分 DEFAULT_TRANCHES 批（推論）
                target_w, tnotes = target_weight(card, None)
                notes += tnotes + ["推論：建倉期外加碼，缺口分批補足"]
                tl = DEFAULT_TRANCHES
            amount = build_amount(target_w, equity, mv, tl)
            if amount <= 0:
                skip("建倉計畫已完成或批次為 0"); continue

        amount = min(amount, a * ADV_FRAC, avail)
        qty = math.floor(amount / limit) if amount > 0 else 0
        if qty <= 0:
            skip("可買金額不足一股（受流動性或可動用現金限制）"); continue
        plans.append(_plan(t, "BUY", qty, limit, d["decision_id"], act,
                           f"{act} {qty} 股，限價 {limit}（上限 {cap}）",
                           notes=notes, review_type=d.get("review_type")))
        plans[-1]["target_weight"] = target_w   # 新部位據此建立建倉計畫（build_plan）
        avail -= qty * limit
        if is_new:
            n_new += 1
    return plans


def plan_sells(conn, asof, decisions: list[dict], quotes: dict) -> list[dict]:
    """SELL_ALL（全賣，論點證偽，不受一年限制）、TRIM（減半），以及權重超限的減碼。

    權重超限：權重 > 等級上限，且建倉滿一年 → 減至上限；未滿一年只記在 notes，不賣（SKILL §6）。
    每筆賣出計畫的 cancel_buys 列出須先取消的未成交買單。
    """
    positions = book.positions(conn)
    open_buys = _open_buys_by_ticker(conn)
    closes = {t: float(p) for t, p in quotes.items() if p is not None}
    plans: list[dict] = []
    handled: set[str] = set()

    for d in _last_per_ticker(decisions):
        if d.get("action") not in ("SELL_ALL", "TRIM"):
            continue
        t, act = d["ticker"], d["action"]
        pos = positions.get(t)
        if pos is None:
            plans.append(_plan(t, "SELL", 0, None, d["decision_id"], act, "未持有，無可賣股數",
                               review_type=d.get("review_type")))
            continue
        frac = (d.get("log") or {}).get("trim_fraction")   # HOLD_REVIEW 的 TRIM 帶比例；其他來源沿用減半
        if act == "SELL_ALL":
            qty = pos["qty"]
        elif isinstance(frac, (int, float)) and not isinstance(frac, bool) and 0 < frac < 1:
            qty = math.floor(pos["qty"] * frac)
        else:
            qty = pos["qty"] // 2
        handled.add(t)
        if qty <= 0:
            plans.append(_plan(t, "SELL", 0, None, d["decision_id"], act,
                               "股數不足以依比例減碼", review_type=d.get("review_type")))
            continue
        plans.append(_plan(t, "SELL", qty, None, d["decision_id"], act,
                           f"{act}：賣出 {qty} 股（市價，開盤成交）", review_type=d.get("review_type"),
                           cancel_buys=open_buys.get(t, [])))

    equity = book.equity(conn, closes)
    for t, pos in positions.items():
        if t in handled or equity <= 0:
            continue
        reg = _reg_data(conn, t) or {}
        px = closes.get(t)
        if px is None:
            continue
        cap = GRADE_CAP.get(reg.get("grade"))
        w = pos["qty"] * px / equity
        if cap is None:
            # 推論：等級不在 A/B（降級或缺）時沒有權重上限可依，不因此下單；超過最高等級上限時留紀錄供複查
            top = max(GRADE_CAP.values())
            if w > top:
                plans.append(_plan(t, "SELL", 0, None, None, "TRIM",
                                   f"等級 {reg.get('grade') or '缺'} 不在 A／B，權重 {w:.1%} 超過最高等級上限 {top:.0%}：只記錄，需複查",
                                   notes=["權重超限僅記錄（等級不在 A／B）"]))
            continue
        if w <= cap:
            continue
        if not pos["first_buy_date"]:
            plans.append(_plan(t, "SELL", 0, None, None, "TRIM", "權重超限但缺首次建倉日，不賣",
                               notes=[f"權重 {w:.1%} > 上限 {cap:.0%}"]))
            continue
        held_days = _days_between(asof, pos["first_buy_date"])
        if held_days < ONE_YEAR_DAYS:
            plans.append(_plan(t, "SELL", 0, None, None, "TRIM",
                               f"權重 {w:.1%} 超過上限 {cap:.0%}，建倉未滿一年，只記錄不賣",
                               notes=["SKILL §6：未滿一年不因價格因素賣出"]))
            continue
        target_qty = math.floor(cap * equity / px)
        sell = pos["qty"] - target_qty
        if sell <= 0:
            continue
        plans.append(_plan(t, "SELL", sell, None, None, "TRIM", f"權重 {w:.1%} 超過上限 {cap:.0%}，減至上限",
                           cancel_buys=open_buys.get(t, [])))
    return plans


def plan_tranches(conn, asof, quotes, adv, slippage_bps) -> tuple[list[dict], list[dict]]:
    """持股的剩餘批次加碼。回傳 (加碼計畫, 取消清單)。

    窗口：建倉起算 ≤ 21 曆日（推論：15 交易日的曆日近似）；超過或連續 10 日高於上限 → 取消剩餘批次。
    每批允許條件（procedure/12 §2 加碼許可）：最近 THESIS 非疑似／證偽；最近一次有閘門紀錄的決策三閘門全過且在 365 天內；
    報價 < 上限；權重 ≤ 15%。批次金額 = (T × 權益 − 市值) ÷ 剩餘批數。
    """
    closes = {t: float(p) for t, p in quotes.items() if p is not None}
    equity = book.equity(conn, closes)
    avail = _avail_cash(conn, equity)
    positions = book.positions(conn)
    open_buys = _open_buys_by_ticker(conn)
    plans: list[dict] = []
    cancels: list[dict] = []

    for t, pos in positions.items():
        reg = _reg_data(conn, t) or {}
        bp = reg.get("build_plan")
        if not bp or int(bp.get("tranches_left", DEFAULT_TRANCHES)) <= 0:
            continue
        tl = int(bp.get("tranches_left", DEFAULT_TRANCHES))
        if not pos["first_buy_date"]:
            cancels.append({"ticker": t, "reason": "缺首次建倉日，無法計算窗口，取消剩餘批次"})
            continue
        if _days_between(asof, pos["first_buy_date"]) > BUILD_CAL_DAYS:
            cancels.append({"ticker": t, "reason": "建倉窗口到期（21 曆日近似），取消剩餘批次"})
            continue
        # 分拆、併購、其他類公司行動：每股上限與原分析都不再適用（只有拆合股會自動換算上限）
        ca = conn.execute(
            "SELECT action_type, effective_date FROM corporate_actions WHERE ticker = ? AND action_type IN "
            f"({','.join('?' * len(CA_INVALIDATES_CAP))}) AND effective_date >= ? ORDER BY effective_date LIMIT 1",
            (t, *CA_INVALIDATES_CAP, pos["first_buy_date"])).fetchone()
        if ca is not None:
            cancels.append({"ticker": t, "reason": f"建倉期間有{ca['action_type']}（{ca['effective_date']}），"
                                                   "每股上限失效，取消剩餘批次，需重新分析"})
            continue
        if int(bp.get("days_above_cap", 0)) >= CANCEL_ABOVE_CAP_DAYS:
            cancels.append({"ticker": t, "reason": f"連 {CANCEL_ABOVE_CAP_DAYS} 日高於上限，取消剩餘批次"})
            continue
        if open_buys.get(t):  # 前一批還沒成交：不重複掛單（批次在下單時已扣）
            plans.append(_plan(t, "BUY", 0, None, None, "ADD", "前一批買單尚未成交，不排下一批"))
            continue
        px, cap = quotes.get(t), reg.get("auto_buy_cap")
        if px is None or cap is None:
            continue

        fails: list[str] = []
        why = add_blocked(conn, t, asof)
        if why:
            fails.append(why)
        g = _latest_gate_decision(conn, t, asof)
        if g is None or _days_between(asof, g["decision_date"]) > GATE_LOOKBACK_DAYS:
            fails.append("三閘門紀錄不在 365 天內")
        else:
            gf = gate_failures(g["log"], None)
            if gf:
                fails.append("最近閘門紀錄未全過：" + "、".join(gf))
        if not px < cap:
            fails.append("報價未低於上限")
        mv = pos["qty"] * px
        if equity <= 0 or mv / equity > NO_ADD_ABOVE:
            fails.append("權重超過 15%")
        if fails:
            plans.append(_plan(t, "BUY", 0, None, g["decision_id"] if g else None, "ADD",
                               "加碼許可未通過：" + "、".join(fails)))
            continue

        a = adv.get(t)
        if not a or a <= 0:
            plans.append(_plan(t, "BUY", 0, None, None, "ADD", "缺 20 日平均成交額，無法加碼"))
            continue
        amount = build_amount(float(bp.get("target_weight") or 0), equity, mv, tl)
        amount = min(amount, a * ADV_FRAC, avail)
        limit = buy_limit(px, cap, slippage_bps)
        qty = math.floor(amount / limit) if (limit and amount > 0) else 0
        if qty <= 0:
            plans.append(_plan(t, "BUY", 0, None, g["decision_id"], "ADD", "加碼金額不足一股或無合法限價"))
            continue
        plans.append(_plan(t, "BUY", qty, limit, g["decision_id"], "ADD",
                           f"加碼批次（剩餘 {tl} 批）：{qty} 股，限價 {limit}",
                           review_type=g["log"].get("review_type")))
        avail -= qty * limit
    return plans, cancels


def tick_build_state(conn, asof: str, quotes: dict[str, float]) -> None:
    """每日一次：持股的建倉計畫若報價高於上限，days_above_cap 加 1，否則歸 0（同一日重跑不重複計）。"""
    closes = {t: float(p) for t, p in quotes.items() if p is not None}
    for t in book.positions(conn):
        reg = _reg_data(conn, t) or {}
        bp = reg.get("build_plan")
        if not bp or bp.get("last_tick") == asof:
            continue
        px, cap = closes.get(t), reg.get("auto_buy_cap")
        if px is None or cap is None:
            continue
        bp = dict(bp)
        bp["days_above_cap"] = int(bp.get("days_above_cap", 0)) + 1 if px > cap else 0
        bp["last_tick"] = asof
        merge_registry(conn, t, {"build_plan": bp}, asof)
