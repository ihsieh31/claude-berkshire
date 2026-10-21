"""組合層部位規模：只回傳計畫（dict 列表），不下單、不寫委託。下單與寫入由 pipelines/daily.py 負責。

規則來源：SKILL §5–§6；procedure/06（P-28、P-31、P-32、P-33、P-36、P-37、P-38、P-43、P-44）；
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
from . import correlation

GRADE_CAP = {"A": 0.15, "B": 0.10}   # P-28 宿主上限；C、D 不開倉（上限視為 0）
# 意願權重（設計決策 D13）：模型給高／中／低，框架換算成等級上限的比例。推論（規則只寫「依優勢×確定性排序、不等權」）
INTENT_FRAC = {"HIGH": 1.0, "MEDIUM": 0.75, "LOW": 0.5}
MIN_T = 0.03                          # P-33：目標權重 < 3% 不建倉
CA_INVALIDATES_CAP = ("分拆", "併購換股", "其他")   # 這些公司行動之後，每股上限不能沿用
MAX_HOLDINGS = 10                     # P-34：≥10 檔時不建新部位（上限 12，保留 2 空名額）
MAX_NEW_PER_DAY = 4                   # P-44
CASH_FLOOR = 0.05                     # P-35：現金下限 5% 權益（硬下限）
CASH_PLAN_BUFFER = 1.2                # P-35／設計決策 D15：新買以計畫水位 下限×1.2（6%）計可動用現金；config account.cash_plan_buffer
ADV_FRAC = 0.01                       # P-38：部位金額 ≤ 平均日成交額 1%
BUILD_CAL_DAYS = 21                   # P-37：1–15 交易日窗口，自 build_plan.started（首批下單日）起算；以 21 曆日近似 15 交易日（D04）
DEFAULT_TRANCHES = 3                  # 剩餘批數預設（plan.md §8 與 build_plan.tranches_left）
FIRST_TRANCHE_PCT = 50.0              # P-36：自動模式首批一律 T 的 50%（程式保證，不採模型值；M6_D7_08）
NO_ADD_ABOVE = 0.15                   # 權重 > 15% 不加碼（P-43）
FULL_REVIEW_ABOVE = 0.25              # 權重 > 25% → 完整複查（P-43；觸發在 review_due，M6-D4-03）
WEIGHT_OVER_CAP = "weight_over_cap"   # 因上漲超過等級上限：不賣、停止加碼，記不暫停的異常（設計決策 D01）
CANCEL_ABOVE_CAP_DAYS = 10            # 連 10 日高於上限 → 取消剩餘批次（P-37）
ONE_YEAR_DAYS = 365                   # 建倉滿一年（曆日）
GATE_LOOKBACK_DAYS = 365              # 加碼許可：三閘門紀錄需在 365 天內
ADV_DAYS = 20                         # 平均日成交額取 20 個交易日（呼叫端以 40 曆日抓資料後取末 20 筆）
GATE_KEYS = ("gate1_not_deleted", "gate2_price_below_cap", "gate3_pass")
PRICE_TRIGGER = "PRICE"
TRANCHE = "TRANCHE"                   # 建倉批次計畫的 review_type：不套冷靜期（procedure/07:59；M7-D10-03）
NOT_VOIDED = "json_extract(data, '$.voided_at') IS NULL"   # 已作廢的決策／訊號卡不參與（`trader decisions void`，M8-D4-02）


# ---------------------------------------------------------------- 純函式（不讀資料庫）

def _truthy(v: Any) -> bool:
    """閘門值是否為真。字串只接受明確的「真」字樣；placeholder（如 'true | false'）視為假。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "通過", "yes", "1")
    return False


def gate_failures(log: dict | None, card: dict | None = None) -> list[str]:
    """三閘門（procedure/12 §1）全過回傳空清單。只看決策日誌（完整分析由框架判定後寫回，PRICE 由框架計算）；
    日誌缺欄位＝未過。不再退回模型寫的訊號卡 auto_buy_gates（M6-D8-04）；card 參數保留只為相容呼叫端。"""
    log = log or {}
    checks = (
        ("gate1_not_deleted", "閘門1（初篩未刪）"),
        ("gate2_price_below_cap", "閘門2（現價低於上限）"),
        ("gate3_pass", "閘門3（能力圈問卷）"),
    )
    return [f"{name}未過" for key, name in checks if not _truthy(log.get(key))]


def _num(v: Any) -> float | None:
    """有限的數字 → float；bool、字串、NaN／inf、其他型別 → None（訊號卡不經格式驗證，M6-D1-02）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def target_weight(card: dict, user_cap: float | None) -> tuple[float, list[str]]:
    """T = min(意願權重, 等級上限, 非空的 max_weight_pct 各項, 使用者上限)；T < 3% → 0。

    意願權重（D13）：訊號卡 intended_weight.level（回寫時只取驗證過的 FULL_B 摘要）→ INTENT_FRAC × 等級上限。
    舊訊號卡沒有 intended_weight：退回 target_weight_pct（缺時 position_plan.target_weight_pct，單位：百分比）；
    不是數字 → 0；0 < 值 ≤ 1 視為小數誤寫 → 0（M6_D7_08）。max_weight_pct 不是物件、或某項不是數字 → 略過並記註。
    max_weight_pct.correlated_group 不參與：相關群額度是下單當時的剩餘增量，由 correlation.buy_room 即時限制（M6-D2-02）。
    """
    notes: list[str] = []
    grade = card.get("certainty_grade")
    iw = card.get("intended_weight")
    level = iw.get("level") if isinstance(iw, dict) else None
    if level in INTENT_FRAC:
        want = INTENT_FRAC[level] * GRADE_CAP.get(grade, 0.0)
    else:
        pp = card.get("position_plan") if isinstance(card.get("position_plan"), dict) else {}
        raw = card.get("target_weight_pct")
        if raw is None:
            raw = pp.get("target_weight_pct")
        if raw is None:
            return 0.0, ["缺目標權重（target_weight_pct；且無 intended_weight）"]
        v = _num(raw)
        if v is None:
            return 0.0, [f"目標權重不是數字：{raw!r}"]
        if 0 < v <= 1:
            return 0.0, [f"目標權重疑似小數（應為百分比）：{raw!r}"]
        want = v / 100.0
    caps = [want, GRADE_CAP.get(grade, 0.0)]
    if grade not in GRADE_CAP:
        notes.append(f"等級 {grade!r} 不開倉（上限 0）")
    mw = card.get("max_weight_pct")
    if mw is not None and not isinstance(mw, dict):
        notes.append(f"max_weight_pct 不是物件，略過：{mw!r}")
        mw = {}
    for k, x in (mw or {}).items():
        if x is None or k == "correlated_group":
            continue
        if _num(x) is None:
            notes.append(f"max_weight_pct.{k} 不是數字，略過：{x!r}")
            continue
        caps.append(_num(x) / 100.0)
    if _num(user_cap) is not None:
        caps.append(float(user_cap))
    t = min(caps)
    if t < MIN_T:
        notes.append(f"目標權重 {t:.2%} < 3%，不建倉（P-33）")
        return 0.0, notes
    return t, notes


P30_EQUITY_LOSS = 2.0   # P-30（stop_mode A，s＝0）：權重 × 近 8 次財報後最大單日跌幅 ≤ 權益 2%（百分比單位）
P30_EVENTS = 8


def single_event_cap(report_dates: list[str], closes: list[tuple[str, float]]) -> tuple[float | None, str]:
    """P-30 單事件上限（百分比，同 max_weight_pct 單位）＝2 ÷ 近 8 次財報後最大單日跌幅（小數）。純函式（M2_D4_01）。
    report_dates：財報公布日（8-K item 2.02 申報日）；closes：拆股調整日收盤 [(YYYY-MM-DD, close)]，只含決策日之前。
    每次事件取「申報日當日」與「次一交易日」報酬較差者：申報時刻不明（盤前公布反應在當日、盤後在次日），取保守（推論）。
    申報日不是交易日 → 以之前最近交易日為當日。近 8 次都沒有跌 → 100（不綁定）。取不到任何事件 → (None, 原因)。"""
    px = sorted((str(d)[:10], float(c)) for d, c in closes if _num(c) is not None and c > 0)
    days = [d for d, _ in px]
    drops: list[tuple[str, float]] = []
    for rd in sorted({str(d)[:10] for d in report_dates})[-P30_EVENTS:]:
        i = max((k for k, d in enumerate(days) if d <= rd), default=None)
        if i is None or i == 0:
            continue
        rets = [px[j][1] / px[j - 1][1] - 1 for j in (i, i + 1) if j < len(px)]
        drops.append((rd, min(rets)))
    if not drops:
        return None, "P-30 未檢查：取不到財報日前後的日線"
    rd, worst = min(drops, key=lambda x: x[1])
    if worst >= 0:
        return 100.0, f"P-30：近 {len(drops)} 次財報後沒有單日下跌，不綁定"
    cap = round(P30_EQUITY_LOSS / abs(worst), 2)
    return cap, f"P-30：近 {len(drops)} 次財報後最大單日跌幅 {worst:.2%}（{rd}）→ 單事件上限 {cap}%"


def single_event_note(card: dict) -> str | None:
    """訊號卡沒有框架算的 P-30 上限（max_weight_pct.single_event 缺或不是數字）→ 註記，不靜默略過（M2_D4_01）。"""
    mw = card.get("max_weight_pct") if isinstance(card.get("max_weight_pct"), dict) else {}
    return None if _num(mw.get("single_event")) is not None else "P-30 未檢查（訊號卡 max_weight_pct.single_event 缺）"


def buy_limit(quote: float | None, cap: float | None, slippage_bps: float) -> float | None:
    """限價 = round_down(上限 × 0.9999)（設計決策 D04：嚴格低於上限；收盤只用來算股數，見 ref_price）。
    報價仍須有值（呼叫端另做「報價×(1＋滑價) < 上限」自檢）。捨去到分，確保不會進位到等於上限。
    """
    if quote is None or cap is None or quote <= 0 or cap <= 0:
        return None
    lim = math.floor(cap * 0.9999 * 100 + 1e-9) / 100
    if lim <= 0 or lim >= cap:
        return None
    return lim


def ref_price(quote: float, slippage_bps: float) -> float:
    """算股數用的參考價＝收盤×(1＋滑價)（D04：限價改掛上限下緣後，股數仍依收盤，不因限價較高而少買）。"""
    return quote * (1 + slippage_bps / 1e4)


def build_amount(t: float, equity: float, mv: float, tranches_left: int) -> float:
    """加碼批次金額 = (T × 權益 − 目前市值) ÷ 剩餘批數（plan.md §8）。"""
    if tranches_left <= 0:
        return 0.0
    return max(0.0, (t * equity - mv) / tranches_left)


def live_g8(cap_dir: Path | None, ticker: str, quote: float | None, reg_grade: str | None = None) -> float | None:
    """以 cap_inputs 換上當日報價重算 G8 調整後預期報酬（與 FULL_B、比較檔同一算法）；缺檔、缺價或算不出 → None。
    reg_grade：登錄表等級；取與 cap_inputs 較保守者重算（HOLD_REVIEW 降等只改登錄表；與比較檔同口徑，A3-M6A-04）。"""
    if cap_dir is None or not quote:
        return None
    f = Path(cap_dir) / f"{ticker}.json"
    if not f.is_file():
        return None
    from ..analysis.checks import _round, _skill_module, cap_input_problems, worse_grade
    from .holdings_view import self_adjusted
    try:
        x = {**json.loads(f.read_text(encoding="utf-8")), "price": float(quote)}
        g = worse_grade(x.get("grade"), reg_grade)
        if g is not None:
            x["grade"] = g
        if cap_input_problems(x):
            return None
        return self_adjusted(x, _round(_skill_module("cap_calc").calc(x)))
    except Exception:  # noqa: BLE001 - 算不出就退回下一層排序
        return None


def _rank_key(card: dict, quote: float | None, live: float | None = None, scale: float = 1.0) -> tuple:
    """排序（名額或現金不足時使用）：G8 調整後預期報酬由高到低。

    同一層才可比：第 0 層用當日報價重算的值（live_g8；新舊卡都有 cap_inputs 就都在這層）；
    沒有 cap_inputs 時退回訊號卡存的 g8_adjusted_expected_return（分析日價格）；
    都沒有 → 第 1 層，price_to_conservative 升冪（推論：價格相對保守價值越低越優先；即時報價 ÷ IV_cons 優先）。
    scale：拆合股換算倍數（登錄表上限 ÷ 訊號卡上限；訊號卡每股值不隨拆合股換算，M6-D10-03）。
    """
    g8 = _num(live) if live is not None else _num(card.get("g8_adjusted_expected_return"))
    if g8 is not None:
        return (0, -g8)
    vps = card.get("value_per_share") if isinstance(card.get("value_per_share"), dict) else {}
    cons = _num(vps.get("conservative"))
    if quote and cons:
        return (1, quote / (cons * scale))
    p = _num(card.get("price_to_conservative"))
    return (1, p if p is not None else math.inf)


def _has_g8(card: dict) -> bool:
    return _num(card.get("g8_adjusted_expected_return")) is not None


def _caps(card: dict, reg: dict) -> tuple[float | None, float]:
    """(買進上限, 拆合股換算倍數)。登錄表 auto_buy_cap 為數字時以它為準（回寫與 PRICE 時與卡同步，且隨拆合股換算；
    訊號卡是當時紀錄、不換算），否則用卡值（M6-D10-03）。倍數＝登錄表上限 ÷ 卡上限（兩者皆有），供排序換算。"""
    ccap, rcap = _num(card.get("auto_buy_cap")), _num(reg.get("auto_buy_cap"))
    if rcap is None:
        return ccap, 1.0
    return rcap, (rcap / ccap if ccap and ccap > 0 and rcap > 0 else 1.0)


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


def latest_card(conn, ticker: str, asof: str | None = None) -> dict | None:
    """最近一張訊號卡：先依資料日（as_of 前 10 字），再依寫入時間（M6-D6-07：舊資料日的完整分析晚寫入時不得變最新）。
    已作廢（data.voided_at，`trader decisions void`，M8-D4-02）的不算。asof：資料日上界（A3-M6B-03）。
    與 writeback.latest_signal 同一份查詢（writeback.signal_rows）。"""
    from ..analysis.writeback import latest_signal
    row = latest_signal(conn, ticker, asof)
    return row["data"] if row else None


def _own_card(conn, decision_id: str, ticker: str) -> dict | None:
    """該決策自己的訊號卡（A3-M6D-03：決策附卡優先取同 decision_id 者，不拿較新決策的卡拼成「舊決策閘門＋新卡 T」）；
    沒有（THESIS／HOLD_REVIEW 不寫卡）才用最新卡。"""
    r = conn.execute(f"SELECT data FROM signals WHERE decision_id = ? AND {NOT_VOIDED} ORDER BY rowid DESC LIMIT 1",
                     (decision_id,)).fetchone()
    return json.loads(r["data"]) if r and r["data"] else latest_card(conn, ticker)


def load_decisions(conn, asof: str, since: str | None = None, late_after: str | None = None) -> list[dict]:
    """決策日 ∈ (since, asof] 的所有決策（since 為 None 時只取 asof 當日），附該決策自己的訊號卡
    （沒有才用該檔最新卡；A3-M6D-03）。

    since 用前一交易日：非交易日（週末、假日）做出的決策由下一個交易日的收盤流程接手。
    late_after（UTC 時戳，上次每日流程完成時間）：另取決策日 ≤ since、但在該時戳之後才寫入的決策
    （例：等待同意數日後才跑完的完整分析，決策日仍是分析的資料日）。
    每筆：{decision_id, ticker, action, review_type, log, card}。
    略過已作廢的決策（M8-D4-02）與人工下單（review_type＝MANUAL，委託已由 `trader trade` 建立，D12）。
    """
    lo = since if since is not None else _prev_day(asof)
    sql = ("SELECT decision_id, ticker, reason_code, data FROM decisions "
           "WHERE ((decision_date > ? AND decision_date <= ?)")
    args: list = [lo, asof]
    if late_after:
        sql += " OR (decision_date <= ? AND created_at > ?)"
        args += [lo, late_after]
    sql += (f") AND {NOT_VOIDED} AND COALESCE(json_extract(data, '$.review_type'), '') <> 'MANUAL'")
    rows = conn.execute(sql + " ORDER BY decision_date, created_at, rowid", args).fetchall()
    out = []
    for r in rows:
        log = json.loads(r["data"]) if r["data"] else {}
        t = r["ticker"]
        out.append({"decision_id": r["decision_id"], "ticker": t, "action": log.get("action"),
                    "review_type": log.get("review_type"), "reason_code": r["reason_code"], "log": log,
                    "card": _own_card(conn, r["decision_id"], t)})
    return out


def _last_per_ticker(items: list[dict]) -> list[dict]:
    last: dict[str, dict] = {}
    for it in items:
        last[it["ticker"]] = it
    return list(last.values())


def last_buys(items: list[dict]) -> list[dict]:
    """每檔最後一筆 BUY／ADD，且其後沒有 supersedes_buy 為真的決策（A3-M7D-02、A3-V3-3-01）：TRIAGE 日誌（D59）、
    THESIS 完好、PRICE、D80 非分析性停止不取消窗口內未下單的買進；較新的完整分析非買進結論、THESIS 疑似／證偽、
    HOLD_REVIEW 減碼／停止結論才取消（與 revalidate_open_buys 同口徑）。items 依決策時間排序（load_decisions）。"""
    last: dict[str, dict] = {}
    for it in items:
        t = it["ticker"]
        if it.get("action") in ("BUY", "ADD"):
            last[t] = it
        elif t in last and supersedes_buy(it.get("log") or {}, it.get("reason_code")):
            del last[t]
    return list(last.values())


def _sell_per_ticker(items: list[dict]) -> list[dict]:
    """每檔要執行的賣出決策：窗口內有 SELL_ALL 取最後一筆 SELL_ALL，否則取最後一筆 TRIM；其他動作不看。
    已寫入的全清不被之後的分析推翻（設計決策 D02：深度鏈停止、缺資料、HOLD 都不取消；要買回走新建倉流程；M5_D2-01）。"""
    last: dict[str, dict] = {}
    for it in items:
        act, prev = it.get("action"), last.get(it["ticker"])
        if act == "SELL_ALL" or (act == "TRIM" and (prev is None or prev.get("action") != "SELL_ALL")):
            last[it["ticker"]] = it
    return list(last.values())


def _sell_orders(conn) -> tuple[dict[str, int], set[str]]:
    """({代號: 未成交賣單股數}, 已有有效委託的決策 id)。有效＝open 或 filled（取消、逾期不算）（M6_D7_03）；
    人工撤單（manual_cancel，`trader ledger cancel`）的決策也算已處理：同日重跑不得把使用者撤掉的單重掛（A3-X1-02）。"""
    open_t: dict[str, int] = {}
    for o in book.open_orders(conn):
        if o["side"] == "SELL":
            open_t[o["ticker"]] = open_t.get(o["ticker"], 0) + int(o["qty"])
    used = {r[0] for r in conn.execute("SELECT DISTINCT decision_id FROM orders WHERE decision_id IS NOT NULL "
                                       "AND status IN ('open', 'filled')").fetchall()}
    used |= {r[0] for r in conn.execute("SELECT json_extract(detail, '$.decision_id') FROM anomalies "
                                        "WHERE kind = 'manual_cancel'").fetchall() if r[0]}
    return open_t, used


def selling_all(conn) -> set[str]:
    """有未成交 SELL_ALL（全清）賣單的代號（A3-X1-01）：全清中不再排買進／加碼批次，買回走新建倉（D02）。"""
    out: set[str] = set()
    for o in book.open_orders(conn):
        if o["side"] == "SELL" and o["decision_id"] and _decision_action(conn, o["decision_id"]) == "SELL_ALL":
            out.add(o["ticker"])
    return out


def _decision_action(conn, decision_id: str) -> str | None:
    r = conn.execute("SELECT json_extract(data, '$.action') FROM decisions WHERE decision_id = ?",
                     (decision_id,)).fetchone()
    return r[0] if r else None


def _open_buys_by_ticker(conn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for o in book.open_orders(conn):
        if o["side"] == "BUY":
            out.setdefault(o["ticker"], []).append(o["order_id"])
    return out


def _avail_cash(conn, equity: float) -> float:
    """新買可動用現金＝現金 − 計畫水位（5% 下限 × CASH_PLAN_BUFFER＝6% 權益）− 已承諾買單（P-35；設計決策 D15）。
    5% 是硬下限；只因持股上漲跌破計畫水位時，可動用 ≤ 0 → 只停新買，不因此賣出。建倉剩餘批次的預留另見 _build_reserve。"""
    return book.cash(conn) - CASH_FLOOR * CASH_PLAN_BUFFER * equity - book.committed_buy_value(conn)


def _plan_margin(equity: float, cash_plan_buffer: float) -> float:
    """config account.cash_plan_buffer 與預設 CASH_PLAN_BUFFER 不同時的差額（預設值時為 0）。"""
    return CASH_FLOOR * (float(cash_plan_buffer) - CASH_PLAN_BUFFER) * equity


def _build_reserve(conn, equity: float, closes: dict[str, float]) -> dict[str, float]:
    """建倉中尚未下單的剩餘批次（POS-051、procedure/06：可動用＝現金−下限−已承諾）：持股或有未成交買單、
    build_plan 剩餘批次 > 0 的檔，預留 max(0, T×權益 − 市值 − 未成交買單)（M6-D2-03）。缺收盤以平均成本估市值。"""
    if equity <= 0:
        return {}
    pos = book.positions(conn)
    ob: dict[str, float] = {}
    for o in book.open_orders(conn):
        if o["side"] == "BUY":
            ob[o["ticker"]] = ob.get(o["ticker"], 0.0) + o["qty"] * (o["limit_price"] or 0)
    out: dict[str, float] = {}
    for t in set(pos) | set(ob):
        bp = (_reg_data(conn, t) or {}).get("build_plan")
        bp = bp if isinstance(bp, dict) else {}
        tw, tl = _num(bp.get("target_weight")), _num(bp.get("tranches_left"))
        if not tw or not tl or tl <= 0:
            continue
        p = pos.get(t)
        mv = p["qty"] * (closes.get(t) or p["avg_cost"]) if p else 0.0
        r = tw * equity - mv - ob.get(t, 0.0)
        if r > 0:
            out[t] = r
    return out


def _spend(reserve: dict[str, float], t: str, amount: float) -> float:
    """t 下單 amount：先用 t 自己的預留，回傳要從一般可動用現金扣的部分。"""
    use = min(amount, reserve.get(t, 0.0))
    if use > 0:
        reserve[t] -= use
    return amount - use


def _target_cap(t_w: float, adv: float, equity: float, notes: list[str]) -> float:
    """P-38：目標部位金額 ≤ 平均日成交額 1%（整個部位，不是每筆委託；M6-D2-09）。"""
    lim = ADV_FRAC * adv / equity if equity > 0 else 0.0
    if t_w > lim:
        notes.append(f"P-38 流動性：目標權重 {t_w:.2%} → {lim:.2%}（平均日成交額 1%）")
        return lim
    return t_w


def _add_cap(reg: dict, hard: float | None) -> float:
    """加碼門檻／建倉目標上限＝min(等級上限, 15%〔P-43〕, 使用者硬上限〔D01 制度類；None 不參與〕)（X38）。
    登錄表有等級但不是 A／B（C、D）→ 0：P-28「降級後為 C → 既有部位持有、不加碼」，與 target_weight 同口徑（RM3-5b）；
    缺等級（舊資料）維持只受 15% 限制。"""
    g = reg.get("grade")
    cap = min(GRADE_CAP.get(g, 0.0) if g else NO_ADD_ABOVE, NO_ADD_ABOVE)
    return min(cap, hard) if hard is not None else cap


def _latest_gate_decision(conn, ticker: str, asof: str) -> dict | None:
    """最近一次「有閘門紀錄」的決策（閘門值為 bool 或字串即視為有紀錄）。"""
    rows = conn.execute(
        f"SELECT decision_id, decision_date, data FROM decisions WHERE ticker = ? AND decision_date <= ? AND {NOT_VOIDED} "
        "ORDER BY decision_date DESC, created_at DESC, rowid DESC", (ticker, asof)).fetchall()
    for r in rows:
        log = json.loads(r["data"]) if r["data"] else {}
        if any(isinstance(log.get(k), (bool, str)) for k in GATE_KEYS):
            return {"decision_id": r["decision_id"], "decision_date": r["decision_date"], "log": log}
    return None


def add_blocked(conn, ticker: str, asof: str) -> str | None:
    """停止加碼（procedure/07 §6、rulebook §9：疑似 → P-50 觀察、停止加碼）：取最近一筆 THESIS 或 HOLD_REVIEW 決策。
    THESIS 疑似／證偽，或 HOLD_REVIEW 的 stop_adding 為真 → 回傳理由；THESIS 完好／HOLD_REVIEW 不停止加碼時，
    登錄表 P-50 觀察期（observe_until）未到期仍擋；否則 None。
    run 收尾（D46，日誌 fallback 為真）的 HOLD＋停止加碼：只在該 run 該檔仍有未結 job_failed 異常、且之後沒有
    該檔較新的決策（任何類型）時才擋；jobs retry 成功、異常結案或之後有新結論即解除，解除後該收尾列視同不存在
    （繼續往更早的決策找，不蓋掉更早真正的疑似／停止加碼）。
    較新的完整分析 BUY（review_type 為空）→ 不擋（取代舊的疑似；M6_D7_06）；完整分析非 BUY 不解除，繼續往前找。
    另：該檔有未結的強模型同意列（M6：待同意、延後、已同意未完成）→ 暫停加碼。"""
    from ..jobs.approvals import add_block_reason
    why = add_block_reason(conn, ticker)
    if why:
        return why
    rows = conn.execute(
        f"SELECT run_id, data FROM decisions WHERE ticker = ? AND decision_date <= ? AND {NOT_VOIDED} "
        "ORDER BY decision_date DESC, created_at DESC, rowid DESC", (ticker, asof)).fetchall()
    for i, r in enumerate(rows):
        log = json.loads(r["data"]) if r["data"] else {}
        rt = log.get("review_type")
        if rt is None and log.get("action") == "BUY":
            return None
        if rt == "THESIS":
            return "論點疑似或證偽" if log.get("thesis_check") in ("疑似", "證偽") else _registry_observing(conn, ticker, asof)
        if rt == "HOLD_REVIEW":
            if not log.get("stop_adding"):
                return _registry_observing(conn, ticker, asof)
            if log.get("fallback") and (i > 0 or not _open_job_failure(conn, ticker, r["run_id"])):
                continue
            # 觀察期滿也不自動恢復：須有新的複查結論（THESIS 完好或 HOLD_REVIEW 不停止加碼，且登錄表觀察期已過）
            return f"持股複查決定停止加碼（觀察至 {log.get('observe_until') or '未定'}）"
    return None


def _registry_observing(conn, ticker: str, asof: str) -> str | None:
    """登錄表 observe_until 晚於 asof（P-50 觀察中）→ 理由；THESIS 完好不提前解除，到期由完整分析結案
    （與 hold_jobs._registry_observing 同口徑；RM3-5b／M3-RS4-01）。"""
    row = conn.execute("SELECT data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    ou = str((json.loads(row["data"]) if row and row["data"] else {}).get("observe_until") or "")[:10]
    return f"P-50 觀察期至 {ou}（到期結案前不加碼）" if ou > asof else None


def _open_job_failure(conn, ticker: str, run_id: str | None) -> bool:
    """該 run 該檔有未結的 job_failed 異常（engine._record_failures 寫、_resolve_recovered／abandon 結案）。"""
    for a in conn.execute("SELECT detail FROM anomalies WHERE kind='job_failed' AND status='open' AND ticker=?",
                          (ticker,)).fetchall():
        if (json.loads(a["detail"]) if a["detail"] else {}).get("run_id") == run_id:
            return True
    return False


def _prev_day(d: str) -> str:
    from datetime import timedelta
    return (_date.fromisoformat(d) - timedelta(days=1)).isoformat()


def _days_between(later: str, earlier: str) -> int:
    return (_date.fromisoformat(later) - _date.fromisoformat(earlier)).days


# ---------------------------------------------------------------- 對外：計畫

def plan_buys(conn, asof, decisions: list[dict], quotes: dict[str, float],
              adv: dict[str, float], slippage_bps: float, cap_dir: Path | None = None,
              cash_plan_buffer: float = CASH_PLAN_BUFFER, user_hard_cap: float | None = None,
              selling: set[str] | None = None) -> list[dict]:
    """BUY／ADD 的計畫。decisions 為 load_decisions 的結果（可含其他 action，會被忽略；每檔只看最後一筆）。

    quotes：當日收盤（參考報價；推論：實際下單參考價以收盤代替）。adv：20 日平均成交額（美元）。
    新部位（未持有）：T 先受相關群剩餘額度與 P-38（部位 ≤ 1% ADV）夾住，首批固定 50% × T（P-36）。
    ADD（已持有）依建倉計畫的批次公式；建倉期外一次補足缺口。
    cap_dir：state/cap_inputs（排序用當日報價重算 G8；None 時只用訊號卡存的值）。
    限價＝上限×0.9999 捨去（D04），股數＝金額 ÷ ref_price（收盤×(1＋滑價)）；可動用現金以計畫水位計（D15）。
    單檔計算出錯 → 該檔 skip，不拖垮整個 daily（M6-D1-02）。
    user_hard_cap（config account.user_hard_cap，制度類硬上限，D01）：新建倉 T、加碼門檻與建倉目標都再取 min(…, 硬上限)，
    免得先買到等級上限、隔天再被 plan_sells 以 INSTITUTIONAL 減回（X38）；None → 行為不變。
    selling：當日計畫全清（SELL_ALL）的代號；與未成交 SELL_ALL 賣單的代號一起跳過（全清中，買回走新建倉；A3-X1-01）。
    """
    closes = {t: float(p) for t, p in quotes.items() if p is not None}
    equity = book.equity(conn, closes)
    avail = _avail_cash(conn, equity) - _plan_margin(equity, cash_plan_buffer)
    reserve = _build_reserve(conn, equity, closes)
    avail -= sum(reserve.values())
    hard = _num(user_hard_cap)
    held = book.positions(conn)
    selling = set(selling or ()) | selling_all(conn)
    paused = book.paused(conn)
    open_buys = _open_buys_by_ticker(conn)
    n_pending_new = len({t for t in open_buys if t not in held})   # 未成交的新部位也占名額（M6_D7_07）
    used = _sell_orders(conn)[1]   # 已有有效（open／filled）委託的決策：不再下第二次（首次 daily 失敗後重跑，check-P16）

    def key(d):
        q = quotes.get(d["ticker"])
        try:
            scale = _caps(d.get("card") or {}, _reg_data(conn, d["ticker"]) or {})[1]
            return _rank_key(d.get("card") or {}, q, live_g8(cap_dir, d["ticker"], q,
                                                              (_reg_data(conn, d["ticker"]) or {}).get("grade")), scale)
        except Exception:  # noqa: BLE001 - 排序資料壞掉排最後，計畫階段再 skip
            return (2, 0.0)

    cands = last_buys(decisions)   # 與 daily 的 buy_items 同口徑（A3-M7D-02）
    cands.sort(key=key)
    plans: list[dict] = []
    n_new = 0
    vocab = correlation.load_vocab()
    pending: dict[str, float] = {}   # 當日已排的買進（權重），後面的候選要算進相關群（P-31／P-32）

    for d in cands:
        t, card, log = d["ticker"], d.get("card") or {}, d.get("log") or {}
        is_new = t not in held
        act = "BUY" if is_new else "ADD"

        def skip(why: str, notes: list[str] | None = None, add_target: float | None = None) -> None:
            plans.append(_plan(t, "BUY", 0, None, d["decision_id"], act, why, notes=notes,
                               review_type=d.get("review_type")))
            if add_target is not None and not is_new and add_target > 0:
                # 加碼因框架限制買不了：可達的目標權重，daily 據此下修登錄表 T（review_due 不再天天排加碼 PRICE；M6-D10-01）
                plans[-1]["add_target"] = add_target

        try:
            if t in paused:
                skip("暫停交易，拒單"); continue
            if t in selling:
                skip("全清中（SELL_ALL），買回走新建倉（D02）"); continue
            if d["decision_id"] in used:
                skip("此決策已有有效委託（open／filled），不重複下單"); continue
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
            reg = _reg_data(conn, t) or {}
            cap, _ = _caps(card, reg)
            quote = quotes.get(t)
            if cap is None:
                skip("訊號卡與登錄表都缺 auto_buy_cap"); continue
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
            if equity <= 0:
                skip("權益非正，無法計算部位"); continue

            notes: list[str] = []
            group_cut = None
            if not _has_g8(card):
                notes.append("推論：缺 G8 欄位，改以 price_to_conservative 排序")
            if is_new:
                if len(held) + n_pending_new + n_new >= MAX_HOLDINGS:
                    skip(f"持股已達 {MAX_HOLDINGS} 檔（含未成交新部位），不建新部位：需勝最弱持股（G8），留待人工"); continue
                if n_new >= MAX_NEW_PER_DAY:
                    skip(f"當日新部位已達 {MAX_NEW_PER_DAY} 檔（P-44）"); continue

            # P-31 相關群上限／P-32 共同情境損失（rulebook 第 147 行：任何動作不得違反）
            room = correlation.buy_room(conn, t, closes, vocab, pending)
            if room["status"] == "blocked":
                skip(room["reason"], add_target=None if is_new else held[t]["qty"] * quote / equity); continue
            if room.get("warning"):   # unknown，或 none／ok 時持股外部變數未知（M6-D5-05）
                notes.append(room["warning"])
            rr = room.get("room")

            if is_new:
                t_w, tnotes = target_weight(card, hard)
                notes += tnotes + [n for n in (single_event_note(card),) if n]
                if t_w <= 0:
                    skip("目標權重為 0，不建倉" + ("：" + "；".join(tnotes) if tnotes else ""), notes); continue
                if rr is not None and rr < MIN_T:
                    skip(f"P-31／P-32 剩餘額度 {rr:.2%} < 3%，不建倉（P-33）：{room['reason']}", notes); continue
                if rr is not None and t_w > rr:   # 相關群額度先夾 T，首批與建倉計畫都依夾後的 T（M6-D2-07）
                    notes.append(f"P-31／P-32 目標權重 {t_w:.2%} → {rr:.2%}：{room['reason']}")
                    t_w = rr
                t_w = _target_cap(t_w, a, equity, notes)
                if t_w < MIN_T:
                    skip(f"受相關群額度或流動性限制後目標權重 {t_w:.2%} < 3%，不建倉（P-33）", notes); continue
                pp = card.get("position_plan") if isinstance(card.get("position_plan"), dict) else {}
                mp = pp.get("first_tranche_pct_of_target", card.get("first_tranche_pct_of_target"))
                if mp is not None and _num(mp) != FIRST_TRANCHE_PCT:
                    notes.append(f"首批比例固定 {FIRST_TRANCHE_PCT:.0f}%（P-36），不採模型值 {mp!r}")
                amount = FIRST_TRANCHE_PCT / 100.0 * t_w * equity
                target_w = t_w
            else:
                bp = reg.get("build_plan") if isinstance(reg.get("build_plan"), dict) else {}
                mv = held[t]["qty"] * quote
                add_cap = _add_cap(reg, hard)
                if mv / equity > add_cap:   # 因上漲超過等級上限（或使用者硬上限）：停止加碼（設計決策 D01）
                    skip(f"現有權重超過 {add_cap:.0%}（等級上限／P-43／使用者硬上限），不加碼", add_target=add_cap); continue
                tl = int(_num(bp.get("tranches_left")) or 0)
                if _num(bp.get("target_weight")) and tl > 0:
                    target_w = min(_num(bp["target_weight"]), add_cap)
                else:
                    # 建倉期已結束（PRICE 判定權重未達目標的加碼）：目標取訊號卡，一次補足缺口（M6-D2-04）
                    target_w, tnotes = target_weight(card, hard)
                    reg_t = _num(reg.get("target_weight"))   # PRICE 以登錄表 T 判 ADD：兩者取嚴（A3-M6B-08）
                    if reg_t is not None and 0 <= reg_t < target_w:
                        tnotes = tnotes + [f"建倉期外目標取登錄表 target_weight {reg_t:.2%}（訊號卡 {target_w:.2%}）"]
                        target_w = reg_t
                    target_w = min(target_w, add_cap)   # 與建倉期內同口徑：HOLD_REVIEW 降等只改登錄表等級（M6-R2-01）
                    notes += tnotes + [n for n in (single_event_note(card),) if n] + ["建倉期外加碼一次補足缺口"]
                    tl = 1
                target_w = _target_cap(target_w, a, equity, notes)
                amount = build_amount(target_w, equity, mv, tl)
                if amount <= 0:
                    skip("建倉計畫已完成或批次為 0", notes, add_target=target_w); continue
                if rr is not None and amount > rr * equity:
                    notes.append(f"P-31／P-32 縮量：{room['reason']}（金額 {amount:,.0f} → {rr * equity:,.0f}）")
                    amount = rr * equity
                    group_cut = mv / equity + rr   # 相關群額度綁定時可達的權重（不足一股時據此抑制加碼）
            usable = avail + reserve.get(t, 0.0)
            px_q = ref_price(quote, slippage_bps)
            if amount > usable:
                notes.append(f"可動用現金縮量（已扣計畫水位、未成交買單與建倉預留）：{amount:,.0f} → {max(usable, 0):,.0f}")
                amount = usable
                px_q = max(px_q, limit)   # 現金綁定：以限價（最壞成交、預扣口徑）算股數，承諾不超過可用現金（A3-M6C-05）
            qty = math.floor(amount / px_q) if amount > 0 else 0
            if qty <= 0:
                skip("可買金額不足一股（受流動性、可動用現金或相關群額度限制）", notes,
                     add_target=group_cut if amount < usable else None); continue   # 現金不足不抑制
        except Exception as e:  # noqa: BLE001 - 單檔資料壞掉不拖垮整個 daily（M6-D1-02）
            skip(f"計畫計算失敗：{type(e).__name__}: {e}"); continue
        plans.append(_plan(t, "BUY", qty, limit, d["decision_id"], act,
                           f"{act} {qty} 股，限價 {limit}（上限 {cap}）",
                           notes=notes, review_type=d.get("review_type")))
        plans[-1]["target_weight"] = target_w   # 新部位據此建立建倉計畫（build_plan）
        plans[-1]["corr_group"] = room.get("group")
        avail -= _spend(reserve, t, qty * limit)   # 現金以限價（最壞成交）預扣
        # 相關群的當日已排權重以算股數的參考價（收盤×(1＋滑價)，D04）計，與持股以收盤計同口徑；
        # 不以限價（D04 後＝上限×0.9999，可遠高於收盤）計，否則同群後一檔的額度被高估扣掉（X38）
        pending[t] = pending.get(t, 0.0) + qty * ref_price(quote, slippage_bps) / equity
        if is_new:
            n_new += 1
    return plans


def plan_sells(conn, asof, decisions: list[dict], quotes: dict, user_hard_cap: float | None = None) -> list[dict]:
    """SELL_ALL（全賣，論點證偽，不受一年限制）、TRIM（減半），以及制度類硬上限的減碼。

    每檔的賣出決策見 _sell_per_ticker：SELL_ALL 優先，之後的非賣出結論不取消它（設計決策 D02；M5_D2-01）。
    權重（設計決策 D01）：
    - 權重 > user_hard_cap（config account.user_hard_cap，宿主／使用者硬上限，rulebook §9 Q1 制度類）→ TRIM，
      數量＝超出部分（只減到解除限制），reason_code＝INSTITUTIONAL，不受一年限制。
    - 因上漲使權重 > 等級上限 → 不賣：qty 0 的紀錄（action＝HOLD）並附 anomaly（weight_over_cap，不暫停交易），
      由 record_weight_over_cap 寫入；加碼由 plan_buys／plan_tranches 的等級上限檢查擋下；> 25% 的完整複查由 review_due 觸發。
    每筆賣出計畫的 cancel_buys 列出須先取消的未成交買單。
    估值類 TRIM（HOLD_REVIEW、sell_category＝估值）掛限價＝觸發線（登錄表 last_trim.line：荒謬級線或 IV_base），
    當日有效（broker 未觸及即作廢，回落即停）；其他賣出為市價（設計決策 D03 撮合部分）。
    """
    positions = book.positions(conn)
    open_buys = _open_buys_by_ticker(conn)
    open_sells, used = _sell_orders(conn)
    closes = {t: float(p) for t, p in quotes.items() if p is not None}
    plans: list[dict] = []
    handled: set[str] = set()

    for d in _sell_per_ticker(decisions):
        t, act = d["ticker"], d["action"]
        pos = positions.get(t)
        if pos is None:
            plans.append(_plan(t, "SELL", 0, None, d["decision_id"], act, "未持有，無可賣股數",
                               review_type=d.get("review_type")))
            continue
        # 同日重跑、冷靜期次日、跨日 late_after 重讀同一決策都不得重複掛單（M6_D7_03）
        if d["decision_id"] in used:
            handled.add(t)
            plans.append(_plan(t, "SELL", 0, None, d["decision_id"], act, "此決策已下過單",
                               review_type=d.get("review_type")))
            continue
        if open_sells.get(t) and act == "TRIM":
            handled.add(t)
            plans.append(_plan(t, "SELL", 0, None, d["decision_id"], act, "已有未成交賣單，不重複下單",
                               review_type=d.get("review_type")))
            continue
        frac = (d.get("log") or {}).get("trim_fraction")   # HOLD_REVIEW 的 TRIM 帶比例；其他來源沿用減半
        if act == "SELL_ALL":   # 全賣一律掛全量：暫停代號只撮合 SELL_ALL 決策的賣單（D06），先前的 TRIM 可能不成交；
            qty = pos["qty"]    # 不會重複賣：broker 以 min(qty, 持股) 成交，持股歸零後其餘賣單以無持股可賣取消（P15R）
        elif isinstance(frac, (int, float)) and not isinstance(frac, bool) and 0 < frac <= 1:
            qty = math.floor(pos["qty"] * frac)   # 1.0＝荒謬級上限全賣（D03）；比例已經 writeback._vet_valuation_sell 核過
        else:
            qty = pos["qty"] // 2
        handled.add(t)
        if qty <= 0:
            plans.append(_plan(t, "SELL", 0, None, d["decision_id"], act,
                               "股數不足以依比例減碼", review_type=d.get("review_type")))
            continue
        line = _valuation_trim_line(conn, d) if act == "TRIM" else None
        if line is False:
            plans.append(_plan(t, "SELL", 0, None, d["decision_id"], act,
                               "估值減碼找不到本次觸發線（登錄表 last_trim），不下單", review_type=d.get("review_type")))
            continue
        how = f"限價＝觸發線 {line}，當日有效" if line else "市價，隔日典型價成交"
        plans.append(_plan(t, "SELL", qty, line or None, d["decision_id"], act,
                           f"{act}：賣出 {qty} 股（{how}）", review_type=d.get("review_type"),
                           cancel_buys=open_buys.get(t, [])))

    equity = book.equity(conn, closes)
    hard = _num(user_hard_cap)
    # 本日沒有檢查等級超限的持股（當日有賣出決策、缺收盤、權益非正、制度類減碼中）：未結 weight_over_cap 異常維持原狀，
    # 不得被 record_weight_over_cap 當成「已不超限」結案（X38；否則隔天以新 first_date 重開）
    for p in plans:
        if p["ticker"] in handled:
            p["weight_unchecked"] = True
    open_over = {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM anomalies WHERE kind=? AND status='open'",
                                            (WEIGHT_OVER_CAP,)).fetchall()}
    for t, pos in positions.items():
        if t in handled:
            continue
        reg = _reg_data(conn, t) or {}
        px = closes.get(t)
        if px is None or equity <= 0:
            if t in open_over:
                plans.append(_plan(t, "SELL", 0, None, None, "HOLD",
                                   "缺收盤價（或權益非正），本日未檢查權重；未結的等級超限異常維持原狀"))
                plans[-1]["weight_unchecked"] = True
            continue
        w = pos["qty"] * px / equity
        if hard is not None and w > hard:   # 制度類硬上限（D01）：只減到解除限制
            if open_sells.get(t):
                plans.append(_plan(t, "SELL", 0, None, None, "TRIM", "已有未成交賣單，不重複下單"))
                plans[-1]["weight_unchecked"] = True
                continue
            sell = pos["qty"] - math.floor(hard * equity / px)
            if sell > 0:
                plans.append(_plan(t, "SELL", sell, None, None, "TRIM",
                                   f"INSTITUTIONAL：權重 {w:.1%} 超過使用者硬上限 {hard:.0%}，只減到上限（賣 {sell} 股）",
                                   notes=["制度類（rulebook §9 Q1；設計決策 D01）：數量＝超出部分"],
                                   cancel_buys=open_buys.get(t, [])))
                plans[-1]["reason_code"] = "INSTITUTIONAL"
                plans[-1]["weight_unchecked"] = True   # 減碼中：等級超限次日再判
                continue
        grade = reg.get("grade")
        cap = GRADE_CAP.get(grade, max(GRADE_CAP.values()))   # 不在 A／B（降級或缺）：以最高等級上限記錄
        if w <= cap:
            continue
        why = f"權重 {w:.1%} 超過等級 {grade or '缺'} 上限 {cap:.0%}：因上漲超限不賣，只記錄並停止加碼（設計決策 D01）"
        if w > FULL_REVIEW_ABOVE:
            why += f"；> {FULL_REVIEW_ABOVE:.0%} 由觸發檢查排完整複查（P-43）"
        plans.append(_plan(t, "SELL", 0, None, None, "HOLD", why))
        plans[-1]["anomaly"] = {"kind": WEIGHT_OVER_CAP, "date": asof, "weight": round(w, 6), "cap": cap,
                                "grade": grade, "full_review": w > FULL_REVIEW_ABOVE}
    return plans


def record_weight_over_cap(conn, plans: list[dict], asof: str) -> None:
    """（寫入模式的 daily 呼叫）plan_sells 標出的 weight_over_cap → 不暫停交易的異常（設計決策 D01）：
    同檔未結者更新 detail（保留 first_date），否則新增；未結但本次確認不再超限（或已無持股）的結案。
    plan_sells 標 weight_unchecked 的檔（當日有賣出決策、缺收盤、制度類減碼中）本日沒有檢查，維持原狀（X38）。"""
    with book.transaction(conn):   # 自成一筆交易，不留隱式交易給後續寫入（A3-M6E-03）
        over = {p["ticker"]: p["anomaly"] for p in plans if (p.get("anomaly") or {}).get("kind") == WEIGHT_OVER_CAP}
        unchecked = {p["ticker"] for p in plans if p.get("weight_unchecked")}
        held = book.positions(conn)
        for a in conn.execute("SELECT anomaly_id, ticker, detail FROM anomalies WHERE kind=? AND status='open'",
                              (WEIGHT_OVER_CAP,)).fetchall():
            d = over.pop(a["ticker"], None)
            if d is None and a["ticker"] in unchecked and a["ticker"] in held:
                continue
            if d is None:
                conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? WHERE anomaly_id=?",
                             (db.now_iso(), f"{asof} 權重已不超過等級上限（或已賣出）", a["anomaly_id"]))
                continue
            first = (json.loads(a["detail"]) if a["detail"] else {}).get("first_date") or asof
            conn.execute("UPDATE anomalies SET detail=? WHERE anomaly_id=?",
                         (json.dumps({**d, "first_date": first}, ensure_ascii=False), a["anomaly_id"]))
        for t, d in over.items():
            book.write_row(conn, "anomalies", {"ticker": t, "kind": WEIGHT_OVER_CAP,
                                               "detail": json.dumps({**d, "first_date": asof}, ensure_ascii=False),
                                               "status": "open", "pause_trading": 0, "created_at": db.now_iso()})


def _valuation_trim_line(conn, d: dict) -> float | None | bool:
    """估值類 TRIM 的觸發線（D03）：HOLD_REVIEW 回寫的 last_trim（同決策日）之 line。非估值類 → None（市價）；
    估值類但找不到線 → False（不下單，有疑義時不賣）。"""
    log = d.get("log") or {}
    if log.get("review_type") != "HOLD_REVIEW" or log.get("sell_category") != "估值":
        return None
    lt = (_reg_data(conn, d["ticker"]) or {}).get("last_trim")
    line = _num(lt.get("line")) if isinstance(lt, dict) and str(lt.get("date") or "")[:10] == str(
        log.get("decision_date") or "")[:10] else None
    return math.ceil(line * 100 - 1e-9) / 100 if line and line > 0 else False   # 進位到分：賣價不低於線


# 委託後出現的這些結論代表買進依據已失效（M7-D4-01）；PRICE／FALLBACK 等不算（冷靜或價格類、保守收尾）
_REVAL_REVIEW_TYPES = (None, "FULL", "THESIS", "HOLD_REVIEW")   # 完整分析的 review_type 為 None
_REVAL_STOP_ACTIONS = ("SELL_ALL", "TRIM", "WATCH", "REJECT", "TOO_HARD", "NO_ACTION")


def nonanalytic_stop(log: dict, reason_code: str | None = None) -> bool:
    """D80：STEP0 MISSING_DATA 停止（使用者拒絕強模型、模型輸出不合格）不是對公司的判斷。日誌不帶 reason_code 時以
    決策列的 reason_code 欄判斷；兩者皆缺時，STEP0＋stop_mode 且三閘門皆 null（writeback D80 分支的寫法）亦視為此類。"""
    if isinstance(log, dict) and log.get("non_analytic_stop") is True:   # writeback 明示（G6／G7 輸出失敗等，A3-M5A-01）
        return True
    if not isinstance(log, dict) or log.get("stopped_at_gate") != "STEP0":
        return False
    hf = log.get("hold_flags") if isinstance(log.get("hold_flags"), dict) else {}
    rc = log.get("reason_code") or hf.get("reason_code") or reason_code
    if rc:
        return rc == "MISSING_DATA"
    return bool(log.get("stop_mode")) and all(log.get(k) is None for k in GATE_KEYS)


def supersedes_buy(log: dict, reason_code: str | None = None) -> bool:
    """較新的這筆決策是否取消先前（未下單或未成交）的買進（A3-M7D-02、A3-V3-3-01）：review_type 須為完整分析（None／FULL）、
    THESIS 或 HOLD_REVIEW（TRIAGE、PRICE、FALLBACK、MANUAL、PORTFOLIO 等不算）；THESIS 只看 thesis_check 疑似／證偽
    （未持有時 writeback 對完好也寫 WATCH，P17R）；其他看 action ∈ _REVAL_STOP_ACTIONS；D80 非分析性停止不算。
    daily 的 buy_items、_deferred_buys、_switch_candidates 與 revalidate_open_buys 共用。"""
    if not isinstance(log, dict):
        return False
    rt = log.get("review_type")
    if rt not in _REVAL_REVIEW_TYPES or nonanalytic_stop(log, reason_code):
        return False
    if rt == "THESIS":
        return log.get("thesis_check") in ("疑似", "證偽")
    return log.get("action") in _REVAL_STOP_ACTIONS or log.get("thesis_check") in ("疑似", "證偽")


def _manual_order(conn, decision_id: str | None) -> bool:
    r = conn.execute("SELECT json_extract(data, '$.review_type') FROM decisions WHERE decision_id = ?",
                     (decision_id,)).fetchone() if decision_id else None
    return r is not None and r[0] == "MANUAL"


def revalidate_open_buys(conn, asof: str) -> list[dict]:
    """未成交買單逐日重核（M7-D4-01；SKILL §2 G2、§3、procedure/07 §6）。回傳 [{order_id, ticker, reason}]，由呼叫端撤單。
    (1) 現行上限（_caps：登錄表優先，與 plan_buys 同）缺或 限價 ≥ 上限；(2) 委託後（決策日晚於 created_date，或寫入時間
    晚於委託最後更新）該檔最新一筆完整分析／THESIS／HOLD_REVIEW 決策為非買進結論或論點疑似／證偽；(3) 已持有（加碼批次）
    且 add_blocked。暫停交易的檔不動（撮合本就略過）。不動 build_plan：下次許可通過再排批次。"""
    paused = book.paused(conn)
    held = book.positions(conn)
    out: list[dict] = []
    for o in book.open_orders(conn):
        t = o["ticker"]
        if o["side"] != "BUY" or t in paused or _manual_order(conn, o["decision_id"]):
            continue   # 人工下單（D12）不做自動重核
        cap, _ = _caps(latest_card(conn, t) or {}, _reg_data(conn, t) or {})
        why = None
        if cap is None or o["limit_price"] is None or float(o["limit_price"]) >= cap:
            why = f"現行自動買進上限 {cap} 已不高於限價 {o['limit_price']}"
        if why is None:
            for r in conn.execute(
                    f"SELECT decision_id, reason_code, data FROM decisions WHERE ticker = ? AND decision_date <= ? "
                    f"AND {NOT_VOIDED} AND (decision_date > ? OR created_at > ?) "
                    "ORDER BY decision_date DESC, created_at DESC, rowid DESC",
                    (t, asof, o["created_date"], o["updated_at"])).fetchall():
                log = json.loads(r["data"]) if r["data"] else {}
                if r["decision_id"] == o["decision_id"] or log.get("review_type") not in _REVAL_REVIEW_TYPES \
                        or nonanalytic_stop(log, r["reason_code"]):   # D80 停止不是結論，往前找（A3-V3-3-01）
                    continue
                # THESIS 只看 thesis_check：未持有時 writeback 對完好也寫 action=WATCH（P17R），不能當停止結論
                if supersedes_buy(log, r["reason_code"]):
                    why = f"委託後論點／分析結論已改變（{r['decision_id']}：{log.get('action')}）"
                break   # 只看最新一筆相關結論（較新的完好／BUY 蓋過較舊的疑似）
        if why is None and t in held:
            b = add_blocked(conn, t, asof)
            if b:
                why = f"停止加碼：{b}"
        if why:
            out.append({"order_id": o["order_id"], "ticker": t, "reason": f"買單重核撤單：{why}"})
    return out


def plan_tranches(conn, asof, quotes, adv, slippage_bps,
                  cash_plan_buffer: float = CASH_PLAN_BUFFER,
                  user_hard_cap: float | None = None,
                  selling: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """持股的剩餘批次加碼。回傳 (加碼計畫, 取消清單)。

    窗口：自 build_plan.started（首批下單日；舊資料缺則用首次建倉日）起算 ≤ 21 曆日（以 21 曆日近似 15 交易日，D04）；
    超過或連續 10 日高於上限 → 取消剩餘批次。限價＝上限×0.9999 捨去、股數依收盤（D04）；批次計畫的 review_type＝TRANCHE
    （已寫入日誌的建倉批次不套冷靜期，procedure/07；M7-D10-03）。
    每批允許條件（procedure/12 §2 加碼許可）：最近 THESIS 非疑似／證偽；最近一次有閘門紀錄的決策三閘門全過且在 365 天內；
    報價×(1＋滑價) < 上限（與 plan_buys 同口徑，撮合前重核才不會以同一收盤撤單）；權重 ≤ 15%。批次金額 = (T × 權益 − 市值) ÷ 剩餘批數；T 再以等級上限、15% 與 P-38（部位 ≤ 1% ADV）夾住。
    單檔計算出錯 → 該檔 skip（M6-D1-02）。
    user_hard_cap：門檻與 T 再取 min(…, 硬上限)（D01；X38）；None → 行為不變。
    selling：同 plan_buys，全清中的持股不排批次（A3-X1-01）。
    """
    closes = {t: float(p) for t, p in quotes.items() if p is not None}
    equity = book.equity(conn, closes)
    avail = _avail_cash(conn, equity) - _plan_margin(equity, cash_plan_buffer)
    reserve = _build_reserve(conn, equity, closes)
    avail -= sum(reserve.values())
    hard = _num(user_hard_cap)
    positions = book.positions(conn)
    open_buys = _open_buys_by_ticker(conn)
    selling = set(selling or ()) | selling_all(conn)
    plans: list[dict] = []
    cancels: list[dict] = []
    vocab = correlation.load_vocab()
    pending: dict[str, float] = {}

    for t, pos in positions.items():
        try:
            reg = _reg_data(conn, t) or {}
            bp = reg.get("build_plan")
            if not isinstance(bp, dict) or int(_num(bp.get("tranches_left", DEFAULT_TRANCHES)) or 0) <= 0:
                continue
            tl = int(_num(bp.get("tranches_left", DEFAULT_TRANCHES)))
            start = str(bp.get("started") or pos["first_buy_date"] or "")[:10]   # 首批下單日起算（D04）
            if not start or not pos["first_buy_date"]:
                cancels.append({"ticker": t, "reason": "缺首次建倉日，無法計算窗口，取消剩餘批次"})
                continue
            if _days_between(asof, start) > BUILD_CAL_DAYS:
                cancels.append({"ticker": t, "reason": "建倉窗口到期（自首批下單日 21 曆日近似 15 交易日），取消剩餘批次"})
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
            if int(_num(bp.get("days_above_cap", 0)) or 0) >= CANCEL_ABOVE_CAP_DAYS:
                cancels.append({"ticker": t, "reason": f"連 {CANCEL_ABOVE_CAP_DAYS} 日高於上限，取消剩餘批次"})
                continue
            if t in selling:   # 全清中：不再加碼（A3-X1-01）；全清成交後 build_plan 由賣光清掉
                plans.append(_plan(t, "BUY", 0, None, None, "ADD", "全清中（SELL_ALL），不排加碼批次"))
                continue
            if open_buys.get(t):  # 前一批還沒成交：不重複掛單（批次在下單時已扣）
                plans.append(_plan(t, "BUY", 0, None, None, "ADD", "前一批買單尚未成交，不排下一批"))
                continue
            px, cap = quotes.get(t), _num(reg.get("auto_buy_cap"))
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
            if not ref_price(px, slippage_bps) < cap:   # 與 plan_buys、D04 同口徑：收盤×(1＋滑價) < 上限（P27R）
                fails.append("報價（含滑價）未低於上限")
            mv = pos["qty"] * px
            add_cap = _add_cap(reg, hard)
            if equity <= 0 or mv / equity > add_cap:   # 因上漲超過等級上限（或使用者硬上限）：停止加碼（設計決策 D01）
                fails.append(f"權重超過 {add_cap:.0%}（等級上限／P-43／使用者硬上限）")
            room = correlation.buy_room(conn, t, closes, vocab, pending)
            if room["status"] == "blocked":
                fails.append(room["reason"])
            if fails:
                plans.append(_plan(t, "BUY", 0, None, g["decision_id"] if g else None, "ADD",
                                   "加碼許可未通過：" + "、".join(fails)))
                continue

            a = adv.get(t)
            if not a or a <= 0:
                plans.append(_plan(t, "BUY", 0, None, None, "ADD", "缺 20 日平均成交額，無法加碼"))
                continue
            tnotes: list[str] = []
            # 建倉計畫目標讀出時夾限（等級上限、15%、使用者硬上限；M6_D7_08、X38）與 P-38（部位 ≤ 1% ADV；M6-D2-09）
            tw = min(_num(bp.get("target_weight")) or 0.0, add_cap)
            tw = _target_cap(tw, a, equity, tnotes)
            amount = build_amount(tw, equity, mv, tl)
            if room.get("warning"):
                tnotes.append(room["warning"])
            if room.get("room") is not None and amount > room["room"] * equity:
                tnotes.append(f"P-31／P-32 縮量：{room['reason']}")
                amount = room["room"] * equity
            usable = avail + reserve.get(t, 0.0)
            cash_bound = amount > usable
            amount = min(amount, usable)
            limit = buy_limit(px, cap, slippage_bps)
            px_q = max(ref_price(px, slippage_bps), limit or 0) if cash_bound else ref_price(px, slippage_bps)
            qty = math.floor(amount / px_q) if (limit and amount > 0) else 0   # 現金綁定以限價算（A3-M6C-05）
            if qty <= 0:
                plans.append(_plan(t, "BUY", 0, None, g["decision_id"], "ADD", "加碼金額不足一股或無合法限價"
                                   + ("（相關群額度用盡）" if room.get("room") == 0 else ""), notes=tnotes))
                continue
        except Exception as e:  # noqa: BLE001 - 單檔資料壞掉不拖垮整個 daily（M6-D1-02）
            plans.append(_plan(t, "BUY", 0, None, None, "ADD", f"批次計畫計算失敗：{type(e).__name__}: {e}"))
            continue
        plans.append(_plan(t, "BUY", qty, limit, g["decision_id"], "ADD",
                           f"加碼批次（剩餘 {tl} 批）：{qty} 股，限價 {limit}",
                           notes=tnotes, review_type=TRANCHE))
        avail -= _spend(reserve, t, qty * limit)
        pending[t] = pending.get(t, 0.0) + qty * ref_price(px, slippage_bps) / equity   # 同 plan_buys（X38）
    return plans, cancels


def tick_build_state(conn, asof: str, quotes: dict[str, float], slippage_bps: float = 0.0) -> None:
    """每日一次：持股的建倉計畫若報價（含滑價，ref_price）不低於上限，days_above_cap 加 1，否則歸 0（同一日重跑不重複計）。
    與 plan_tranches／plan_buys 的加碼許可同口徑：收盤×(1＋滑價) < 上限才算「低於上限」（A3-X2-03）。"""
    closes = {t: float(p) for t, p in quotes.items() if p is not None}
    for t in book.positions(conn):
        reg = _reg_data(conn, t) or {}
        bp = reg.get("build_plan")
        if not bp or bp.get("last_tick") == asof:
            continue
        px, cap = closes.get(t), _num(reg.get("auto_buy_cap"))
        if px is None or cap is None:
            continue
        bp = dict(bp)
        bp["days_above_cap"] = int(bp.get("days_above_cap", 0)) + 1 if not ref_price(px, slippage_bps) < cap else 0
        bp["last_tick"] = asof
        merge_registry(conn, t, {"build_plan": bp}, asof)
