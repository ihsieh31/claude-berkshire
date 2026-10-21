"""每日收盤流程（M5）：交易日判斷 → 入金 → 行情與 ADV → 公司行動偵測與解讀 → 股利發放 → 撮合
→ 觸發檢查（持股）→ 組合層計畫（賣出、加碼、新買）→ 過期委託 → 淨值 → runs 表與摘要。

約定與近似（均在程式註解標明）：
- 「下一交易日」與「前一交易日」由 Alpaca 交易日曆取得（精確）。
- 買單 15 交易日過期、建倉窗口 15 交易日（自 build_plan.started 起算）：以 21 曆日近似（D04）。
- 參考價以當日收盤代替（推論）；實際成交依 broker.match_orders 於下一交易日以典型價撮合（D07，紙上交易近似）。
- 撮合前 broker.recheck_pending 以前一交易日收盤重核冷靜單、估值 TRIM 與未成交買單（D05）。
- 觸發檢查失敗只停失敗那幾檔的新買與加碼；組合層失敗（觸發檢查整體出錯、CORP_ACTION 工作失敗、無法歸屬到單檔的
  工作失敗）或當日失敗檔數 ≥ 3 才停全組合；同檔連 3 次 daily 失敗記 trigger_fail_streak（D50）。
- 平均日成交額 ADV 以 40 曆日的資料取末 20 筆近似 20 交易日（推論）。
- 8-K 只抓持股；filingDate 以 asof=次日 取得「≤ 當日」的申報（SEC 客戶端的 asof 為嚴格小於）。
  申報清單當次重抓（不吃 20 小時快取，否則窗口滑過就永久漏掉當天 8-K）；含 8-K/A；SEC 失敗改用舊快取時寫進 notes（M4-D7-3）。
- 價格一律 Alpaca（Yahoo 只供研究，D34／M4-D7-1）；ADV 的 feed 由 data.adv_feed 決定並寫進 notes（M4-D7-2）。
- 下單紀錄（摘要 orders／plans_dry_run）附 research_px（登錄表 review_price，研究來源）與 research_gap＝限價÷研究價−1（D34）。
- 寫入模式只跑已收盤的日子：schedule.timezone（紐約）的 D 須已過 schedule.daily_close（--force 略過）；
  當日完全沒有日線 → 失敗不標 done（M7-D7-05）。已有較晚日期完成時不可回頭重跑（M7-D6-02）；較早交易日未完成時
  也不可先跑較晚日（A3-M7A-04，--force 略過）。
- runs['daily-D'] 只由 run_daily 寫（CORP_ACTION 引擎用 daily-D:ca，M7-D2-01）；非交易日寫 kind=daily_skip 的 done 列（M7-D1-09）。

dry_run：不寫入任何資料（不入金、不跑公司行動、不撮合、不跑觸發檢查、不下單、不寫淨值），
只依現有資料算出賣出與買進計畫並回傳，標示 placed=False。
（未採用 FakeRunner＋SAVEPOINT 回滾：db.insert 與 writeback 會自行 commit，SAVEPOINT 無法回滾已提交的寫入。）
"""
from __future__ import annotations

import json
import math
import traceback
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .. import db
from ..config import Config
from ..data.http import DataError
from ..data.inputs import make_clients
from ..data.universe import normalize_ticker
from ..jobs import approvals
from ..calendar import TradingCalendar
from ..jobs.engine import DAILY_REVIEW_KIND, Engine, _closed_ids, _restore_sigterm, _trap_sigterm
from ..ledger import book, broker, corp_actions
from ..llm.router import Router
from ..models import JobSpec, JobStatus
from ..paths import Paths
from ..portfolio import sizing as Z
from .review import ReviewError, run_review

BARS_LOOKBACK_DAYS = 40        # 曆日；ADV 取末 20 筆（推論：近似 20 交易日）
ADV_ROWS = 20
CAL_SEARCH_DAYS = 10          # 找前一／下一交易日的曆日範圍
UNFILLED_EXPIRE_DAYS = 21     # 推論：15 交易日的曆日近似
SEC_FORMS = ["8-K", "8-K/A"]
CA_PROCESS_LAG_DAYS = 60      # 公司行動處理日最晚約晚於除息日數週（推論，取 60 曆日）
FIRST_DAILY_SINCE = "1900-01-01"   # 首次 daily 讀全部歷史決策（bootstrap 決策日可能遠早於安裝日，M8-D7-01、M8-R7-01）


class DailyError(Exception):
    """每日流程無法進行（缺必要客戶端、交易日曆無資料）。"""


def _cal(alpaca, start: str, end: str) -> list[str]:
    try:   # 網路／Alpaca 暫時故障 → DailyError（CLI 乾淨回報，排程照重試規則）（M8-D7-02）
        return [str(r["date"])[:10] for r in alpaca.calendar(start, end) if r.get("date")]
    except (DataError, RuntimeError, OSError) as e:
        raise DailyError(f"交易日曆取得失敗：{type(e).__name__}: {e}") from e


def _bars_maps(df: pd.DataFrame, date: str, prev: str) -> tuple[dict, dict, dict]:
    """回傳 (bars_today, bars_prev, adv)。adv 以 close×volume 的 20 筆平均；缺 volume 欄位時 adv 為空。"""
    if df is None or df.empty:
        return {}, {}, {}
    df = df.copy()
    df["ticker"] = df["ticker"].map(normalize_ticker)
    df["date"] = df["date"].astype(str).str[:10]
    df = df[df["date"] <= date]
    today, pre = {}, {}
    for r in df.to_dict("records"):
        bar = {k: (None if pd.isna(r.get(k)) else float(r[k])) for k in ("open", "high", "low", "close")}
        if r["date"] == date:
            today[r["ticker"]] = bar
        elif r["date"] == prev:
            pre[r["ticker"]] = bar
    adv: dict[str, float] = {}
    if "volume" in df.columns:
        d2 = df.dropna(subset=["close", "volume"]).sort_values(["ticker", "date"])
        for t, g in d2.groupby("ticker"):
            g = g.tail(ADV_ROWS)
            adv[t] = float((g["close"].astype(float) * g["volume"].astype(float)).mean())
    return today, pre, adv


def _last_closes(df: pd.DataFrame, date: str) -> dict[str, float]:
    """每檔 ≤ date 最近一筆收盤（持股缺當日 bar 時估淨值用，M7-D1-10）。"""
    if df is None or df.empty:
        return {}
    df = df.dropna(subset=["close"]).copy()
    df["ticker"] = df["ticker"].map(normalize_ticker)
    df["date"] = df["date"].astype(str).str[:10]
    df = df[df["date"] <= date].sort_values(["ticker", "date"])
    return {t: float(g["close"].iloc[-1]) for t, g in df.groupby("ticker")}


_DECISIONS_DESC = ("SELECT decision_id, reason_code, data FROM decisions WHERE ticker=? AND decision_date<=? "
                   f"AND {Z.NOT_VOIDED} AND COALESCE(json_extract(data, '$.review_type'), '') "
                   "NOT IN ('MANUAL', 'PORTFOLIO') ORDER BY decision_date DESC, created_at DESC, rowid DESC")


def _latest_decision(conn, ticker: str, date: str):
    """該檔決策日 ≤ date 的最新分析決策列（略過已作廢、MANUAL 人工單與 PORTFOLIO 框架自發單；M6-RS4-03）。"""
    return conn.execute(_DECISIONS_DESC + " LIMIT 1", (ticker, date)).fetchone()


def _newer_than(conn, ticker: str, date: str, decision_id: str) -> list | None:
    """該檔比 decision_id 新的分析決策列（新到舊；口徑同 _latest_decision）。decision_id 不在其中（已作廢等）→ None。"""
    newer = []
    for r in conn.execute(_DECISIONS_DESC, (ticker, date)).fetchall():
        if r["decision_id"] == decision_id:
            return newer
        newer.append(r)
    return None


def _card_for(conn, decision_id: str, ticker: str) -> dict | None:
    """該決策自己的訊號卡（A3-M7D-03：延後的買進以它當時的卡重核，不拿之後 PRICE 等較新的卡）；沒有才用最新卡。"""
    r = conn.execute(f"SELECT data FROM signals WHERE decision_id=? AND {Z.NOT_VOIDED} ORDER BY rowid DESC LIMIT 1",
                     (decision_id,)).fetchone()
    return json.loads(r["data"]) if r and r["data"] else Z.latest_card(conn, ticker)


def _sec_8k_since(conn, date: str, prev: str) -> str:
    """8-K 窗口下界：最近一次 SEC 抓取成功（摘要無 sec_8k_stale）的完成 daily 日；之間抓取失敗的日子不推進窗口，
    其 8-K 由今天補抓（R7-01）。已登錄事件由 detect 以代號＋生效日去重。"""
    since = prev
    for r in conn.execute("SELECT asof, summary FROM runs WHERE kind='daily_close' AND status='done' AND asof<? "
                          "ORDER BY asof DESC LIMIT 30", (date,)).fetchall():
        try:
            s = json.loads(r["summary"] or "{}") or {}
        except (TypeError, ValueError):
            s = {}
        since = min(since, r["asof"])
        if not s.get("sec_8k_stale"):
            return since
    return since


def _deferred_buys(conn, date: str, run_id: str, have: set[str]) -> list[dict]:
    """上一次完成（done）的 daily 因觸發檢查失敗而延後的 BUY／ADD 決策（M7-D5-03）：之後沒有較新的 BUY／ADD、也沒有
    取消買進的結論（sizing.supersedes_buy：TRIAGE 日誌、THESIS 完好、PRICE、D80 停止不算，A3-M7D-02）才併回，
    格式同 load_decisions；價格、閘門由 plan_buys 照常重核。決策口徑同 load_decisions 略過已作廢、MANUAL 人工單
    與 PORTFOLIO 框架自發單（M6-RS4-03）。卡片取該決策自己的（A3-M7D-03）。"""
    r = conn.execute("SELECT summary FROM runs WHERE kind='daily_close' AND status='done' AND asof<? AND run_id<>? "
                     "ORDER BY asof DESC LIMIT 1", (date, run_id)).fetchone()
    try:
        items = (json.loads(r["summary"]) or {}).get("deferred_buys") or [] if r and r["summary"] else []
    except ValueError:
        items = []
    out = []
    for it in items:
        did, t = it.get("decision_id"), it.get("ticker")
        if not did or did in have:
            continue
        newer = _newer_than(conn, t, date, did)
        if newer is None:
            continue
        if any(lg.get("action") in ("BUY", "ADD") or Z.supersedes_buy(lg, r["reason_code"])
               for r, lg in ((r, json.loads(r["data"]) if r["data"] else {}) for r in newer)):
            continue
        row = conn.execute("SELECT reason_code, data FROM decisions WHERE decision_id=?", (did,)).fetchone()
        log = json.loads(row["data"]) if row and row["data"] else {}
        out.append({"decision_id": did, "ticker": t, "action": log.get("action"),
                    "review_type": log.get("review_type"), "reason_code": row["reason_code"] if row else None,
                    "log": log, "card": _card_for(conn, did, t)})
    return out


def _late_superseded(conn, date: str, since: str, items: list[dict]) -> tuple[list[dict], list[str]]:
    """晚到（決策日 ≤ since、上次 daily 之後才寫入；late_after）的舊資料日 BUY／ADD（A3-M6D-01，D86 第二道防線）：
    窗口外已有較新的 BUY／ADD 或取消買進的結論（sizing.supersedes_buy，與 _deferred_buys 同口徑）→ 不收。回傳（保留, 註記）。"""
    keep, notes = [], []
    for it in items:
        did, t = it.get("decision_id"), it.get("ticker")
        d = str((it.get("log") or {}).get("decision_date") or "")[:10]
        if not did or not d or d > since:
            keep.append(it)
            continue
        newer = _newer_than(conn, t, date, did)
        hit = next((r for r, lg in ((r, json.loads(r["data"]) if r["data"] else {}) for r in newer or [])
                    if lg.get("action") in ("BUY", "ADD") or Z.supersedes_buy(lg, r["reason_code"])), None)
        if hit is not None:
            notes.append(f"{t} 晚到的舊資料日買進 {did}（{d}）已被較新決策 {hit['decision_id']} 取代，不下單（D86、A3-M6D-01）")
            continue
        keep.append(it)
    return keep, notes


TRIGGER_FAIL_STREAK = 3   # 同檔觸發檢查連續失敗的 daily 次數 → 醒目異常（D50）


def _trigger_fail_streak(conn, date: str, run_id: str, failed: set[str]) -> None:
    """同檔觸發檢查連 3 次 daily 失敗（本次＋前 2 次 daily 摘要的 review_failed_tickers）→ 不暫停的異常 trigger_fail_streak
    （未結者不重複記）。"""
    if not failed:
        return
    prior = []
    for r in conn.execute("SELECT summary FROM runs WHERE kind='daily_close' AND asof<? AND run_id<>? "
                          "ORDER BY asof DESC LIMIT ?", (date, run_id, TRIGGER_FAIL_STREAK - 1)).fetchall():
        try:
            prior.append(set((json.loads(r["summary"] or "{}") or {}).get("review_failed_tickers") or []))
        except ValueError:
            prior.append(set())
    if len(prior) < TRIGGER_FAIL_STREAK - 1:
        return
    for t in sorted(failed):
        if all(t in p for p in prior) and conn.execute(
                "SELECT 1 FROM anomalies WHERE ticker=? AND kind='trigger_fail_streak' AND status='open'",
                (t,)).fetchone() is None:
            db.insert(conn, "anomalies", {
                "ticker": t, "kind": "trigger_fail_streak", "status": "open", "pause_trading": 0,
                "detail": json.dumps({"date": date, "days": TRIGGER_FAIL_STREAK,
                                      "note": f"觸發檢查連續 {TRIGGER_FAIL_STREAK} 次 daily 失敗，該檔停買中，需人工排查"},
                                     ensure_ascii=False), "created_at": db.now_iso()})


def _tp(plan: dict) -> dict:
    """計畫轉成 JSON 安全的 dict（供摘要輸出）。"""
    return json.loads(json.dumps(plan, ensure_ascii=False, default=str))


def _stop_build(conn, ticker: str, date: str) -> None:
    reg = Z._reg_data(conn, ticker) or {}
    bp = dict(reg.get("build_plan") or {})
    if bp:
        bp["tranches_left"] = 0
        Z.merge_registry(conn, ticker, {"build_plan": bp}, date)


def _consume(conn, ticker: str, date: str) -> None:
    reg = Z._reg_data(conn, ticker) or {}
    bp = dict(reg.get("build_plan") or {})
    if bp:
        bp["tranches_left"] = max(0, int(bp.get("tranches_left", Z.DEFAULT_TRANCHES)) - 1)
        Z.merge_registry(conn, ticker, {"build_plan": bp}, date)


def _switch_candidates(conn, buys: list[dict], date: str) -> None:
    """持股已滿而新 BUY 不建倉 → 不暫停的異常 switch_candidate（換股由人工決定）。同檔未結 → 更新 detail
    （日期、決策，保留 first_date）；已持有或持股（含未成交新買單）已不滿 → 未結者結案（條件已消失）。"""
    for p in buys:
        if not (p["qty"] == 0 and str(p.get("reason") or "").startswith(f"持股已達 {Z.MAX_HOLDINGS} 檔")):
            continue
        old = conn.execute("SELECT anomaly_id, detail FROM anomalies WHERE ticker=? AND kind='switch_candidate' "
                           "AND status='open' ORDER BY anomaly_id", (p["ticker"],)).fetchone()
        od = (json.loads(old["detail"]) if old and old["detail"] else {}) if old else {}
        first = od.get("first_date") or od.get("date")   # 舊版異常沒有 first_date：取它當時記的日期，不是今天
        detail = json.dumps({"date": date, "first_date": first or date, "decision_id": p.get("decision_id"),
                             "note": "持股已滿：須勝最弱持股（G8）才換股，由人工決定"}, ensure_ascii=False)
        if old:
            conn.execute("UPDATE anomalies SET detail=? WHERE anomaly_id=?", (detail, old["anomaly_id"]))
        else:
            db.insert(conn, "anomalies", {"ticker": p["ticker"], "kind": "switch_candidate", "detail": detail,
                                          "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
    held = set(book.positions(conn)) | {o["ticker"] for o in book.open_orders(conn) if o["side"] == "BUY"}
    for a in conn.execute("SELECT anomaly_id, ticker FROM anomalies WHERE kind='switch_candidate' AND status='open'"
                          ).fetchall():
        why = ("已持有或已有買單" if a["ticker"] in held
               else f"持股 {len(held)} 檔，未達 {Z.MAX_HOLDINGS} 檔" if len(held) < Z.MAX_HOLDINGS else None)
        if why is None:   # 該檔最新分析結論已不是 BUY／ADD：不再是換股候選（M6-D4-04）
            # 只認取消買進的結論（sizing.supersedes_buy）或 PRICE 的非買進（價格已不在買點，M6-D4-04）；
            # TRIAGE 日誌、THESIS 完好、D80 停止不是結論，略過往前找（A3-M7B-05）
            for r in conn.execute(_DECISIONS_DESC, (a["ticker"], date)).fetchall():   # 查無決策列（資料不全）不據以結案
                lg = json.loads(r["data"]) if r["data"] else {}
                if lg.get("action") in ("BUY", "ADD"):
                    break
                if Z.supersedes_buy(lg, r["reason_code"]) or lg.get("review_type") == Z.PRICE_TRIGGER:
                    why = f"最新分析結論為 {lg.get('action') or '無'}，非 BUY／ADD"
                    break
        if why:
            conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? WHERE anomaly_id=?",
                         (db.now_iso(), f"{date} 條件已消失：{why}", a["anomaly_id"]))


PORTFOLIO = "PORTFOLIO"   # 框架自發委託（制度類硬上限減碼等，計畫無決策）的決策列 review_type（M6-D4-02）


def _portfolio_decision(conn, plan: dict, date: str, run_id: str | None) -> str:
    """框架自發委託先寫一筆決策（review_type＝PORTFOLIO；理由與註記進日誌），前瞻驗證才看得到（rulebook:302 宿主下單前自檢、
    M6-D4-02）。在呼叫端的交易內寫入、不提交（與委託同進退）。回傳決策編號。"""
    from ..analysis.writeback import next_decision_id
    did = next_decision_id(conn, plan["ticker"], date)
    log = {"decision_id": did, "decision_date": date, "ticker": plan["ticker"], "action": plan["action"],
           "review_type": PORTFOLIO, "side": plan["side"], "qty": plan["qty"], "limit": plan.get("limit_price"),
           "reason_code": plan.get("reason_code"), "reason_one_liner": plan.get("reason") or "",
           "framework_notes": list(plan.get("notes") or [])}
    book.write_row(conn, "decisions", {"decision_id": did, "ticker": plan["ticker"], "decision_date": date,
                                       "run_id": run_id, "job_id": None, "signal": plan["action"],
                                       "reason_code": plan.get("reason_code") or PORTFOLIO, "data": log,
                                       "created_at": db.now_iso()})
    return did


def _price_triggered(conn, decision_id) -> bool:
    """HOLD_REVIEW 決策是否由 PRICE 觸發（log.trigger.full_stage＝PRICE）：其估值 TRIM 冷靜一個交易日（D82）。"""
    if not decision_id:
        return False
    r = conn.execute("SELECT json_extract(data, '$.trigger.full_stage') FROM decisions WHERE decision_id = ?",
                     (decision_id,)).fetchone()
    return bool(r) and r[0] == Z.PRICE_TRIGGER


def _trim_not_placed(conn, plan: dict, why: str) -> None:
    """HOLD_REVIEW 估值 TRIM 沒下成單（拒單、股數 0）：回寫時記的「預計」last_trim 標未成交，免得「只減一次」
    把之後的減碼永久擋掉（M7-RS3-01）。非 TRIM、無決策或非 HOLD_REVIEW 由 _trim_unfilled 自行略過。"""
    if plan.get("action") != "TRIM" or not plan.get("decision_id"):
        return
    if conn.execute("SELECT 1 FROM orders WHERE decision_id = ? AND status IN ('open', 'filled') LIMIT 1",
                    (plan["decision_id"],)).fetchone():
        return   # 此決策已有有效委託（同日重跑 plan_sells 回 qty 0「此決策已下過單」）：不是沒下成（A3-M7A-03）
    broker._trim_unfilled(conn, {"ticker": plan["ticker"], "decision_id": plan["decision_id"]}, why)
    conn.commit()


def _place(conn, plan: dict, date: str, next_day: str, write: bool, out: dict) -> str | None:
    """依計畫下單（write=False 只記錄「將下單」）。冷靜期：PRICE 觸發的 BUY／ADD／TRIM 次日才可撮合。
    計畫沒有決策（框架自發）時先寫一筆 PORTFOLIO 決策，再以其編號下單（M6-D4-02）。"""
    if plan["qty"] <= 0:
        if write:
            _trim_not_placed(conn, plan, f"{date} 計畫股數 0：{plan.get('reason') or ''}")
        return None
    cooling = plan["action"] in ("BUY", "ADD", "TRIM") and (
        plan.get("review_type") == Z.PRICE_TRIGGER
        or (plan["action"] == "TRIM" and _price_triggered(conn, plan.get("decision_id"))))   # D82
    created = next_day if cooling else date
    rec = {**_tp(plan), "created_date": created, "placed": write}
    rp = (Z._reg_data(conn, plan["ticker"]) or {}).get("review_price")   # 研究價 vs 下單價（D34）
    if isinstance(rp, (int, float)) and rp > 0:
        rec["research_px"] = rp
        if plan.get("limit_price"):
            rec["research_gap"] = round(float(plan["limit_price"]) / rp - 1, 4)
    if not write:
        out["plans_dry_run"].append(rec)
        return None
    if conn.in_transaction:   # 先提交先前的寫入：委託與登錄表更新自成一筆可整體回滾的交易（M7-D9-06）
        conn.commit()
    try:
        with book.transaction(conn):
            did = plan["decision_id"] or _portfolio_decision(conn, plan, date, out.get("run_id"))
            oid = broker.place_order(conn, plan["ticker"], plan["side"], plan["qty"], plan["limit_price"],
                                     did, created)
            _after_place(conn, plan, date)
    except broker.BrokerError as e:
        out["rejected"].append({**rec, "error": str(e)})
        _trim_not_placed(conn, plan, f"{date} 下單被拒：{e}")
        return None
    plan["decision_id"] = rec["decision_id"] = did
    rec["order_id"] = oid
    out["orders"].append(rec)
    return oid


def _suppress_adds(conn, buys: list[dict], date: str) -> None:
    """ADD 因框架限制（相關群額度、等級／硬上限、目標已達）買不了（qty 0、計畫附 add_target）：登錄表 target_weight 下修到
    可達值並記 target_weight_basis.add_suppressed，review_due 與 PRICE 不再天天排加碼；只下修不上調，下次完整分析重寫 T 時
    解除（M6-D10-01）。現金不足不抑制（plan_buys 不附 add_target）。"""
    for p in buys:
        at = p.get("add_target")
        if p.get("qty") or p.get("action") != "ADD" or isinstance(at, bool) or not isinstance(at, (int, float)):
            continue
        reg = Z._reg_data(conn, p["ticker"]) or {}
        rt = reg.get("target_weight")
        if isinstance(rt, bool) or not isinstance(rt, (int, float)) or at >= rt - 1e-9:
            continue
        basis = dict(reg["target_weight_basis"]) if isinstance(reg.get("target_weight_basis"), dict) else {}
        basis["add_suppressed"] = {"date": date, "from": rt, "reason": p.get("reason")}
        Z.merge_registry(conn, p["ticker"], {"target_weight": math.floor(float(at) * 1e6) / 1e6,
                                             "target_weight_basis": basis}, date)


def _after_place(conn, plan: dict, date: str) -> None:
    """下單後的登錄表更新（新買建 build_plan、加碼消耗一批）；與委託同一交易。"""
    if plan["side"] == "BUY":
        if plan["action"] == "BUY" and plan.get("target_weight"):
            # build_plan.decision_id＝觸發這次下單的決策（可能是 PRICE）；target_weight_basis＝決定目標權重 T 的
            # 分析（M6-D10-01）。PRICE 回寫複製上一張卡並換 decision_log_id，但 T 的輸入仍是完整分析的，故優先沿用
            # 完整分析寫在登錄表的 basis（登錄表 T 與本次計畫 T 相同時）；否則才依最新訊號卡（plan_buys 的 T 來源）。
            reg = Z._reg_data(conn, plan["ticker"]) or {}
            basis, reg_t = reg.get("target_weight_basis"), reg.get("target_weight")
            if not (isinstance(basis, dict) and isinstance(reg_t, (int, float))
                    and math.isclose(float(reg_t), float(plan["target_weight"]))):
                card = Z.latest_card(conn, plan["ticker"]) or {}
                basis = {"decision_id": card.get("decision_log_id"), "grade": card.get("certainty_grade"),
                         "notes": Z.target_weight(card, None)[1]}
            upd: dict = {"build_plan": {
                "target_weight": plan["target_weight"], "tranches_left": Z.DEFAULT_TRANCHES,
                "days_above_cap": 0, "started": date, "decision_id": plan["decision_id"],
                "target_weight_basis": basis}}
            pt = float(plan["target_weight"])
            if not isinstance(reg_t, bool) and isinstance(reg_t, (int, float)) and pt < float(reg_t) - 1e-9:
                # 計畫 T 受相關群 P-31／P-32、流動性 P-38 或使用者硬上限夾過：登錄表 T 同步為計畫 T，review_due／PRICE／
                # 持股檢視才不以夾限前的 T 在建倉完成後天天排加碼（M6-D10-01）；下次完整分析重寫 T 時解除
                upd["target_weight"] = pt
                upd["target_weight_basis"] = {**(basis if isinstance(basis, dict) else {}),
                                              "clamp": {"date": date, "from": reg_t, "notes": list(plan.get("notes") or [])}}
            Z.merge_registry(conn, plan["ticker"], upd, date)
        elif plan["action"] == "ADD":
            _consume(conn, plan["ticker"], date)


def run_daily(cfg: Config, paths: Paths, conn, date: str, clients: dict[str, Any] | None = None,
              router: Router | None = None, skip_review: bool = False, dry_run: bool = False, sleep=None,
              force: bool = False, now: datetime | None = None) -> dict:
    d = _date.fromisoformat(date)
    cl = clients if clients is not None else make_clients(cfg, paths)
    alpaca, sec = cl.get("alpaca"), cl.get("sec")
    if alpaca is None:
        raise DailyError("每日收盤需要 Alpaca 金鑰（交易日曆與行情）")
    if getattr(alpaca, "feed", "") == "yahoo":
        raise DailyError("daily 撮合需要 Alpaca，Yahoo 只供初選／分析（M4-D7-1）")
    if sec is None:
        raise DailyError("每日收盤需要 SEC 設定（SEC_USER_AGENT 須含 email；8-K 偵測）")

    if not dry_run and not force:   # 紐約 D 須已收盤：台北手動跑「今天」時紐約多半未開盤或盤中（M7-D7-05）
        tz = ZoneInfo(cfg.schedule.get("timezone") or "America/New_York")
        local = (now or datetime.now(timezone.utc)).astimezone(tz)
        h, m = (int(x) for x in str(cfg.schedule.get("daily_close") or "17:30").split(":"))
        if date > local.date().isoformat() or (date == local.date().isoformat() and (local.hour, local.minute) < (h, m)):
            raise DailyError(f"紐約 {date} 尚未收盤（{tz.key} 現在 {local:%Y-%m-%d %H:%M}，須過 {h:02d}:{m:02d}；"
                             "台北比紐約快 12–13 小時）。確定要跑請加 --force")

    today_days = _cal(alpaca, date, date)
    if date not in today_days:
        if not _cal(alpaca, (d - timedelta(days=7)).isoformat(), (d + timedelta(days=7)).isoformat()):
            # 前後 7 天都沒交易日＝日曆暫時回空，不是休市；不可記 daily_skip done 永久跳過（M7S1-N01）
            raise DailyError(f"交易日曆 {date}±7 天無任何交易日（資料來源暫時回空），不記非交易日，交排程重試")
        skipped = {"skipped": "非交易日", "date": date}
        if not dry_run:   # 寫 done 列，排程才不會把它當失敗重試到放棄（M7-D1-09）；kind 不同，不影響 late_after
            ts = db.now_iso()
            db.upsert(conn, "runs", {"run_id": f"daily-{date}", "kind": "daily_skip", "asof": date, "status": "done",
                                     "started_at": ts, "finished_at": ts, "summary": skipped}, key=["run_id"])
        return skipped
    if not dry_run and conn.execute("SELECT 1 FROM runs WHERE kind='daily_close' AND status='done' AND asof>?",
                                    (date,)).fetchone():
        raise DailyError(f"已有晚於 {date} 的每日收盤，不可回頭重跑（會以較新帳本改寫舊日淨值）")
    prev_days = [x for x in _cal(alpaca, (d - timedelta(days=CAL_SEARCH_DAYS)).isoformat(), date) if x < date]
    next_days = [x for x in _cal(alpaca, (d + timedelta(days=1)).isoformat(),
                                 (d + timedelta(days=CAL_SEARCH_DAYS)).isoformat()) if x > date]
    if not prev_days or not next_days:
        raise DailyError("交易日曆缺少前一或下一交易日")
    prev, nxt = prev_days[-1], next_days[0]
    if not dry_run and not force:
        # 較早交易日未完成不可先跑較晚日（A3-M7A-04）：否則較晚日 done 後，較早日被上面的「不可回頭」永久擋住、排程死鎖
        # 只看交易日的 daily_close：非交易日的 daily_skip（例：週六）晚於前一交易日，不能拿來當「已補到」的界線
        last_ok = conn.execute("SELECT MAX(asof) FROM runs WHERE kind='daily_close' AND status='done' AND asof<?",
                               (date,)).fetchone()[0]
        if last_ok and last_ok < prev:
            span = prev_days if prev_days[0] <= last_ok else _cal(
                alpaca, (_date.fromisoformat(last_ok) + timedelta(days=1)).isoformat(), date)
            gap = [x for x in span if last_ok < x < date and conn.execute(
                "SELECT 1 FROM runs WHERE run_id=? AND status='done'", (f"daily-{x}",)).fetchone() is None]
            if gap:
                raise DailyError(f"較早的交易日 {'、'.join(gap[:5])}{' 等' if len(gap) > 5 else ''} 每日收盤尚未完成："
                                 f"先補 {gap[0]}（淨值與撮合依帳本順序；確定要跳過請加 --force）")

    run_id = f"daily-{date}"
    write = not dry_run
    out: dict[str, Any] = {"date": date, "run_id": run_id, "dry_run": dry_run, "prev_date": prev,
                           "research_price_source": cfg.data.research_price_source,
                           "next_date": nxt, "orders": [], "plans_dry_run": [], "rejected": [],
                           "corp_events": [], "settled": [], "fills": [], "notes": []}
    if write:
        book.ensure_initial_deposit(conn, cfg.account.initial_cash, date)
        if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
            db.insert(conn, "runs", {"run_id": run_id, "kind": "daily_close", "asof": date, "status": "running",
                                     "started_at": db.now_iso()})
        else:
            conn.execute("UPDATE runs SET kind='daily_close', status='running', started_at=?, finished_at=NULL "
                         "WHERE run_id=?", (db.now_iso(), run_id))   # 先前記過 daily_skip 的列（M7S1-N02）
            conn.commit()

    stage = "bars"   # 目前步驟：失敗時寫進摘要 error_stage，判斷停在哪一步（M7-D5-08）
    # 排程 stop_group 的 SIGTERM 轉 KeyboardInterrupt：非引擎階段收到也能記 failed、寫摘要（A3-M7E-03）
    sig_prev = _trap_sigterm() if write else None
    try:
        positions = book.positions(conn)
        open_tickers = {o["ticker"] for o in book.open_orders(conn)}
        watch = {r["ticker"] for r in conn.execute("SELECT ticker FROM registry WHERE status = 'WATCH'")}
        bench = normalize_ticker(cfg.account.benchmark)
        universe = sorted(set(positions) | open_tickers | watch | {bench})
        bars = alpaca.bars(universe, start=(d - timedelta(days=BARS_LOOKBACK_DAYS)).isoformat(), end=date,
                           adjustment="raw") if universe else pd.DataFrame()
        bars_today, bars_prev, adv = _bars_maps(bars, date, prev)
        adv_feed = cfg.data.adv_feed
        if universe and adv_feed != getattr(alpaca, "feed", adv_feed):   # ADV 另用 adv_feed 的成交量（M4-D7-2）
            adv = _bars_maps(alpaca.bars(universe, start=(d - timedelta(days=BARS_LOOKBACK_DAYS)).isoformat(),
                                         end=date, adjustment="raw", feed=adv_feed), date, prev)[2]
        out["notes"].append(f"ADV 來源＝{adv_feed}")
        if write and not bars_today:   # 當日日線未到：不撮合、不以成本估值標 done，交給重試（M7-D7-05）
            raise DailyError(f"{date} 沒有任何當日日線（行情未到或尚未收盤）")
        closes = {t: b["close"] for t, b in bars_today.items() if b.get("close") is not None}
        # 參考價留底（M7-D5-01）：新買單的參考收盤、價格日與來源
        out["quotes"], out["price_date"] = closes, date
        out["price_source"] = f"{type(alpaca).__name__}:{getattr(alpaca, 'feed', None)}"
        if not adv:
            out["notes"].append("缺 volume 欄位，無 ADV：新買與加碼一律跳過（流動性無法檢查）")

        # 持股不在登錄表（分拆、併購換得或人工補登）：觸發檢查涵蓋不到，記一筆不暫停的異常待人工排完整分析
        if write:
            registered = {r["ticker"] for r in conn.execute("SELECT ticker FROM registry")}
            for t in sorted(set(positions) - registered):
                out["notes"].append(f"{t} 持股不在登錄表：觸發檢查不涵蓋，需排完整分析")
                if conn.execute("SELECT 1 FROM anomalies WHERE ticker=? AND kind='unregistered_holding' "
                                "AND status='open'", (t,)).fetchone() is None:
                    db.insert(conn, "anomalies", {
                        "ticker": t, "kind": "unregistered_holding", "detail": json.dumps({"date": date}),
                        "status": "open", "pause_trading": 0, "created_at": db.now_iso()})

        # 4. 公司行動偵測 → CORP_ACTION 工作（run_id = daily-DATE）
        stage = "ca"
        ca_failed = False
        holdings = sorted(positions)
        filings_8k: dict[str, list[dict]] = {}
        since_8k = _sec_8k_since(conn, date, prev)   # R7-01：上次 SEC 抓取失敗 → 窗口從更早成功日起
        if since_8k < prev:
            out["notes"].append(f"先前 SEC 抓取失敗：8-K 偵測窗口放寬為 ({since_8k}, {date}]")
        for t in holdings:
            cik = (Z._reg_data(conn, t) or {}).get("cik") or sec.cik_for(t)
            if not cik:
                out["notes"].append(f"{t} 缺 CIK，略過 8-K 偵測")
                continue
            # SEC 的 asof 為嚴格小於：用次日取得「≤ 當日」的申報
            fl = sec.filings(cik, asof=(d + timedelta(days=1)).isoformat(), since=since_8k, forms=SEC_FORMS,
                             max_age_hours=0)   # 當次重抓（M4-D7-3）
            filings_8k[t] = [f for f in fl if str(f.get("form", "")).upper() in SEC_FORMS]
        if getattr(sec, "stale", None):
            out["notes"].append("SEC 抓取失敗改用舊快取，可能漏掉最新 8-K：" + "、".join(sorted(set(sec.stale))))
            out["sec_8k_stale"] = sorted(set(sec.stale))   # 下次 daily 由此日之前的成功日重抓（R7-01）
        # Alpaca 端點以「處理日」過濾（股利的處理日＝發放日，常晚於除息日數週）：查詢區間往後放寬，
        # 起點回看 ALPACA_LOOKBACK_DAYS 曆日，再由 detect 依除息／生效日落在 (date−14, date] 過濾（M7-D9-02）
        alpaca_actions = alpaca.corporate_actions(
            universe, start=(d - timedelta(days=corp_actions.ALPACA_LOOKBACK_DAYS)).isoformat(),
            end=(d + timedelta(days=CA_PROCESS_LAG_DAYS)).isoformat()) if universe else []
        # 基準（SPY）只用於比較，不持有也不在觀察名單時不偵測：避免每季股利都派一個 CORP_ACTION 模型工作
        ca_tickers = sorted(set(positions) | open_tickers | watch)
        events = corp_actions.detect(conn, date, prev, ca_tickers, alpaca_actions, filings_8k, bars_prev,
                                     bars_today, since_8k=since_8k) if write else []
        # 觀察名單（無持股、無委託）的 Alpaca 現金股利：帳本無可入帳，不派模型工作、也不因失敗停全組合（A3-M7C-06）；
        # 拆合股／代號變更等仍派（登錄表與 cap_inputs 換算，D83）
        live = set(positions) | open_tickers
        events = [e for e in events if e["ticker"] in live or not (
            e.get("source") == "alpaca" and (e.get("alpaca") or {}).get("type") == "cash_dividend")]
        # 先前工作失敗而未入帳的事件：不受 (prev, date] 窗口限制，重新派工（H2）
        if write:
            have = {e["event_id"] for e in events}
            retried = [e for e in corp_actions.unresolved_failed_events(conn) if e["event_id"] not in have]
            events = events + retried
            if retried:
                out["notes"].append("重派先前失敗的公司行動事件：" + "、".join(e["event_id"] for e in retried))
        # 中斷（Ctrl-C／SIGTERM）遺留的 pending／running CORP_ACTION 工作（A3-M7E-01）：detect 不看 jobs 表，同事件會再派一個，
        # 引擎又會把同 run 的孤兒一起跑 → 重複入帳。同一 ca run 的 pending 孤兒改為只重跑它；其他的標 skipped 後今天重派一個
        ca_run = f"{run_id}:ca"   # 引擎結束會把自己的 run 標 done：不可與 daily-D 共用（M7-D2-01）
        orphans: dict[str, list] = {}
        if write:
            for r in conn.execute("SELECT job_id, run_id, status, params FROM jobs WHERE job_type='CORP_ACTION' "
                                  "AND status IN ('pending', 'running') ORDER BY created_at, job_id").fetchall():
                ev0 = (json.loads(r["params"] or "{}") or {}).get("event") or {}
                if ev0.get("event_id") and ev0.get("ticker"):
                    orphans.setdefault(ev0["event_id"], []).append((r["job_id"], r["run_id"], r["status"], ev0))
            have = {e["event_id"] for e in events}
            for eid, lst in orphans.items():   # 本 ca run 的 pending 孤兒即使今天沒再偵測到也要跑完並檢查結果
                own = [x for x in lst if x[1] == ca_run and x[2] == "pending"]
                if eid not in have and own:
                    events.append(own[0][3])
                    have.add(eid)
        out["corp_events"] = [{k: e.get(k) for k in ("event_id", "ticker", "source", "date")} for e in events]
        if events:
            router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
            engine = Engine(conn, router, paths, cfg, **({"sleep": sleep} if sleep else {}))
            engine.ctx.sec = sec
            engine.start_run(ca_run, "corp_action", date)
            added: list[tuple[str, dict]] = []
            for i, ev in enumerate(events, 1):
                t = ev["ticker"]
                lst = orphans.get(ev["event_id"], [])
                reuse = next((x[0] for x in lst if x[1] == ca_run and x[2] == "pending"), None)
                for jid0, _, _, _ in lst:
                    if jid0 != reuse:
                        conn.execute("UPDATE jobs SET status='skipped', error=?, finished_at=? WHERE job_id=? "
                                     "AND status IN ('pending', 'running')",
                                     (f"中斷遺留的公司行動工作：同事件改由 {run_id} 處理（A3-M7E-01）", db.now_iso(), jid0))
                if reuse:   # 只重跑孤兒，參數換成現況持股與委託
                    conn.execute("UPDATE jobs SET params=? WHERE job_id=?", (json.dumps(
                        {"event": ev, "position": corp_actions.position_of(conn, t),
                         "open_orders": corp_actions.open_orders_of(conn, t)}, ensure_ascii=False, default=str), reuse))
                    conn.commit()
                    added.append((reuse, ev))
                    continue
                conn.commit()
                jid = f"{run_id}:{t}:CORP_ACTION:{i}"
                n = 1
                while conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (jid,)).fetchone():  # 同日重跑／重派：換新 id
                    n += 1
                    jid = f"{run_id}:{t}:CORP_ACTION:{i}r{n}"
                engine.add_job(JobSpec(
                    job_id=jid, run_id=ca_run, job_type="CORP_ACTION", ticker=t,
                    asof=date, params={"event": ev, "position": corp_actions.position_of(conn, t),
                                       "open_orders": corp_actions.open_orders_of(conn, t)}))
                added.append((jid, ev))
            out["ca_engine"] = engine.run(ca_run)
            # 工作沒有成功完成（失敗、預算耗盡等）：事件不得被當成已處理 → 異常暫停該檔，下次 daily 重派
            # 組合層工作失敗 → 今日停全組合新買與加碼（D50）
            for jid, ev in added:
                r = conn.execute("SELECT status, error FROM jobs WHERE job_id=?", (jid,)).fetchone()
                if r and r["status"] not in ("done", "skipped", JobStatus.CONSERVATIVE_FALLBACK):  # 已保守處理不重複記（D43）
                    if r["status"] in ("pending", "running"):
                        conn.execute("UPDATE jobs SET status='failed', error=? WHERE job_id=?",
                                     ("daily 結束時未完成，事件改由下次 daily 重派", jid))
                        conn.commit()
                    corp_actions.record_job_failure(conn, ev, jid, r["error"])
                    ca_failed = True
                    out["notes"].append(f"{ev['ticker']} 公司行動工作未完成（{ev['event_id']}）：已暫停該檔，下次 daily 重派")

        # 5. 股利發放、6. 撮合（下一交易日撮合：只撮合 created_date < date）
        stage = "settle"
        out["settled"] = corp_actions.settle_dividends(conn, date) if write else []
        stage = "match"
        if write:
            # 6a. 撮合前重核（D05）：以前一交易日收盤重核冷靜單、估值 TRIM；未成交買單的結論重核也提前到撮合前（check-P17）
            prev_closes = {t: b["close"] for t, b in bars_prev.items() if b.get("close") is not None}
            out["recheck_cancels"] = broker.recheck_pending(conn, date, prev_closes, cfg.account.slippage_bps)
            out["fills"] = broker.match_orders(conn, date, bars_today, cfg.account.slippage_bps,
                                               cfg.account.commission_per_order)
            # 6b. 登錄表與帳本對帳（持有與否以 positions 為準）：觸發檢查的 holdings 範圍才不會漏掉
            # 人工 adjust 建倉後被分析寫成 WATCH 的持股，也不會把已賣光的檔留在 HOLDING
            out["notes"] += book.reconcile_registry(conn, date)

        # 7. 觸發檢查（持股）
        stage = "review"
        review_ok = True          # False＝停全組合新買與加碼（系統性失敗，D50）
        failed_t: set[str] = set()   # 觸發檢查失敗的檔：只停這幾檔（D50）
        if write:   # D42：未決同意逾 5 個交易日自動作廢（舊資料日不得下單）
            # 交易日以 Alpaca 日曆計（含臨時休市；A3-M7B-06）
            exp_cal = TradingCalendar(_cal(alpaca, (d - timedelta(days=20)).isoformat(), date), source="alpaca")
            expired = approvals.expire_stale(conn, date, cal=exp_cal)
            if expired:
                out["notes"].append(f"同意逾 {approvals.EXPIRE_TRADING_DAYS} 個交易日未決，已作廢：{expired}")
        if write and not skip_review:
            router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
            hrun = f"review-{date}-holdings"
            if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (hrun,)).fetchone() is None:
                # daily 先建立並標記持股觸發 run（kind=daily_review）：不受每日上限（M5-RS4-03）；run_review 沿用既有列
                db.insert(conn, "runs", {"run_id": hrun, "kind": DAILY_REVIEW_KIND, "asof": date, "status": "pending"})
            rs = run_review(cfg, paths, conn, date, scope="holdings", run_id=hrun,
                            clients=cl, router=router)
            eng = rs.get("engine") or {}
            # 等待同意（強模型）不算失敗：該檔由 add_blocked 暫停加碼，其他照常
            # 使用者拒絕／同意作廢／放棄造成的 skipped 不算失敗（M5-RS2-02）
            closed = _closed_ids(conn, rs.get("run_id")) if rs.get("run_id") else set()
            bad = [r for r in conn.execute("SELECT job_id, ticker FROM jobs WHERE run_id=? AND status IN ('failed', 'skipped')",
                                           (rs.get("run_id"),)).fetchall() if r["job_id"] not in closed]
            failed_t = {normalize_ticker(r["ticker"]) for r in bad if r["ticker"]}
            # 系統性（停全組合）：整體出錯、預算暫停等非常態狀態、失敗工作無法歸屬到單檔、失敗檔數 ≥ 3
            unattributed = any(not r["ticker"] for r in bad) or (
                eng.get("failed", 0) + eng.get("skipped", 0) - len(closed) > 0 and not failed_t)
            review_ok = ("error" not in rs
                         and eng.get("status", "done") in ("done", "awaiting_approval", "failed", "abandoned")
                         and not unattributed and len(failed_t) < 3)
            out["review"] = {"ok": review_ok, "run_id": rs.get("run_id"), "counts": rs.get("counts"),
                             "error": rs.get("error"), "skipped": rs.get("skipped") or [],
                             "failed_tickers": sorted(failed_t)}
            out["review_failed_tickers"] = sorted(failed_t)
            if write:
                _trigger_fail_streak(conn, date, run_id, failed_t)
        elif skip_review:
            out["notes"].append("--skip-review：未跑觸發檢查，沿用既有決策")
        if ca_failed:
            review_ok = False
            out["notes"].append("公司行動工作失敗（組合層）：今日不下新買單與加碼，賣出照常")
        elif not review_ok:
            out["notes"].append("觸發檢查未成功：今日不下新買單與加碼，賣出照常")
        elif failed_t:
            out["notes"].append("觸發檢查失敗的檔今日不下新買單與加碼（其他照常）：" + "、".join(sorted(failed_t)))

        # 8. 建倉計畫的報價狀態（同一日只計一次）
        if write:
            Z.tick_build_state(conn, date, closes, cfg.account.slippage_bps)   # 含滑價同加碼許可（A3-X2-03）

        # 9. 當日決策 → 組合計畫：賣出 → 加碼（與取消）→ 新買
        stage = "plan_sells"
        last = conn.execute(
            "SELECT finished_at, summary FROM runs WHERE kind='daily_close' AND status='done' AND run_id<>? "
            "ORDER BY finished_at DESC LIMIT 1", (run_id,)).fetchone()
        # late_after 取上次 daily「讀決策」的時刻（缺則退回 finished_at）：上次讀完之後才寫入的決策不會被漏掉（M7-D7-04）
        last_daily = (json.loads(last[1] or "{}").get("read_at") or last[0]) if last else None
        # 含前一交易日之後非交易日的決策，以及上次每日流程後才寫入的較早決策（等待同意後才完成的分析）
        # 首次 daily（從無 done 的 daily_close）：讀全部歷史決策（M8-D7-01、M8-R7-01）；
        # 每檔取最後一筆、plan_buys 以當日報價重核上限與閘門，不會用舊價下單
        since = prev if last_daily else FIRST_DAILY_SINCE
        out["read_at"] = db.now_iso()
        decisions = [x for x in Z.load_decisions(conn, date, since=since, late_after=last_daily)
                     if x.get("review_type") != PORTFOLIO]   # 框架自發委託的決策列已下過單，不再當計畫輸入（M6-D4-02）
        # 上次 daily 觸發檢查失敗而延後的買進（M7-D5-03）：放前面，窗口內同檔較新的決策會蓋過
        decisions = _deferred_buys(conn, date, run_id, {x["decision_id"] for x in decisions}) + decisions
        sell_items = [x for x in decisions if x.get("action") in ("SELL_ALL", "TRIM")]
        if not last_daily:   # 首次 daily 讀全部歷史：持有期開始前的舊賣出決策不適用於本次持股（check-RM8）
            fbd = {t: str(p["first_buy_date"] or "")[:10] for t, p in book.positions(conn).items()}
            sell_items = [x for x in sell_items
                          if str((x.get("log") or {}).get("decision_date") or "")[:10] >= fbd.get(x.get("ticker"), "")]
        # 每檔最後一筆 BUY／ADD；其後有取消買進的結論（完整分析非買進、THESIS 疑似／證偽、HOLD_REVIEW 停止等）才丟
        # （M6_D7_09）；TRIAGE 日誌、THESIS 完好、PRICE、D80 停止不吞單，與 revalidate_open_buys 同口徑（A3-M7D-02、V3-3-01）
        buy_items = Z.last_buys(decisions)
        buy_items, late_notes = _late_superseded(conn, date, since, buy_items)   # A3-M6D-01
        out["notes"].extend(late_notes)

        # D01：制度類硬上限（config account.user_hard_cap）才減碼；因上漲超過等級上限只記 weight_over_cap 異常（不暫停）
        sells = Z.plan_sells(conn, date, sell_items, closes, user_hard_cap=cfg.account.user_hard_cap)
        if write:
            Z.record_weight_over_cap(conn, sells, date)
        for p in sells:
            if write:
                for oid in p.get("cancel_buys", []):
                    broker.cancel_order(conn, oid, reason="賣出前取消未成交買單")
            _place(conn, p, date, nxt, write, out)
        out["sells"] = [_tp(p) for p in sells]
        # 9a. 未成交買單重核（M7-D4-01）：上限已降到限價以下、委託後論點／分析結論改變、加碼許可失效 → 撤單
        out["revalidated_cancels"] = Z.revalidate_open_buys(conn, date)
        if write:
            for c in out["revalidated_cancels"]:
                broker.cancel_order(conn, c["order_id"], reason=c["reason"])

        stage = "tranches"
        # 全清中（今日計畫的 SELL_ALL 或未成交 SELL_ALL 賣單）的代號不排加碼批次與買進：買回走新建倉（D02；A3-X1-01）
        selling = {p["ticker"] for p in sells if p.get("action") == "SELL_ALL" and p.get("qty", 0) > 0}
        tranches, cancels = Z.plan_tranches(conn, date, closes, adv, cfg.account.slippage_bps,
                                            cash_plan_buffer=cfg.account.cash_plan_buffer,
                                            user_hard_cap=cfg.account.user_hard_cap,   # D01：加碼目標不超過硬上限（X38）
                                            selling=selling)
        for c in cancels:
            if write:
                for o in book.open_orders(conn, c["ticker"]):
                    if o["side"] == "BUY":
                        broker.cancel_order(conn, o["order_id"], reason=c["reason"])
                _stop_build(conn, c["ticker"], date)
        out["tranche_cancels"] = cancels

        def blocked(t: str) -> bool:   # 觸發檢查失敗：系統性 → 全部；否則只擋失敗的檔（D50）
            return not review_ok or t in failed_t

        block_reason = "公司行動工作失敗" if ca_failed else "觸發檢查失敗"   # M7-S5-N2
        out["tranches"] = [{**_tp(p), "placed": False, "skip_reason": block_reason} if blocked(p["ticker"])
                           else _tp(p) for p in tranches]   # M7-D5-06
        for p in tranches:
            if not blocked(p["ticker"]):
                _place(conn, p, date, nxt, write, out)

        stage = "buys"
        deferred = [x for x in buy_items if blocked(x["ticker"])]
        buys: list[dict] = []
        if review_ok:
            buys = Z.plan_buys(conn, date, [x for x in buy_items if not blocked(x["ticker"])], closes, adv,
                               cfg.account.slippage_bps, cap_dir=paths.state / "cap_inputs",
                               cash_plan_buffer=cfg.account.cash_plan_buffer,
                               user_hard_cap=cfg.account.user_hard_cap,   # D01：新建倉／加碼目標不超過硬上限（X38）
                               selling=selling)
            for p in buys:
                _place(conn, p, date, nxt, write, out)
        out["buys"] = [_tp(p) for p in buys]
        if deferred:
            # 延後到下一次 daily 重核（M7-D5-03），不靜默丟單
            out["deferred_buys"] = [{k: x.get(k) for k in ("decision_id", "ticker", "action")} for x in deferred]
            if write:
                detail = json.dumps({"date": date, "reason": block_reason,
                                     "decision_ids": [x["decision_id"] for x in deferred]}, ensure_ascii=False)
                same = conn.execute("SELECT anomaly_id FROM anomalies WHERE kind='buy_deferred_review_failed' "
                                    "AND status='open' AND json_extract(detail, '$.date') = ?", (date,)).fetchone()
                if same:   # 同日重跑：更新，不重複插入（M7-S5-N1）
                    conn.execute("UPDATE anomalies SET detail = ? WHERE anomaly_id = ?", (detail, same[0]))
                else:
                    db.insert(conn, "anomalies", {
                        "ticker": None, "kind": "buy_deferred_review_failed", "status": "open", "pause_trading": 0,
                        "detail": detail, "created_at": db.now_iso()})
        if write:   # 先前延後的買單已不再延後（今日已重核）→ 結案（M7-S5-N1）
            still = {x["decision_id"] for x in deferred}
            for a in conn.execute("SELECT anomaly_id, detail FROM anomalies WHERE kind='buy_deferred_review_failed' "
                                  "AND status='open' AND COALESCE(json_extract(detail, '$.date'), '') <> ?",
                                  (date,)).fetchall():
                ids = set((json.loads(a[1] or "{}") or {}).get("decision_ids") or [])
                if not (ids & still):
                    conn.execute("UPDATE anomalies SET status='resolved', resolved_at=?, resolution=? "
                                 "WHERE anomaly_id=?", (db.now_iso(), f"{date} 已重核、不再延後", a[0]))
        if write:   # 持股已滿：換股由人工決定（不暫停的異常；未結時更新、條件消失時結案）
            _switch_candidates(conn, buys, date)
            _suppress_adds(conn, buys, date)   # 加碼買不了：登錄表 T 下修，不再天天排加碼 PRICE（M6-D10-01）

        # 9b. 相關群（P-31／P-32）組合摘要：>10% 從最弱者減碼的建議只記錄（異常，不暫停、不下單）；外部變數未知者列入報表
        try:
            from ..portfolio.correlation import portfolio_groups, record_trim_anomalies
            cg = portfolio_groups(conn, closes)
            out["corr_groups"] = cg
            if cg["trim_recommendations"]:
                out["notes"].append("P-32 共同情境損失 >10%，減碼建議（未下單）：" + "、".join(
                    f"{r['ticker']} 減 {r['sell_weight']:.2%}" for r in cg["trim_recommendations"]))
            if write:   # 無建議時也要呼叫：把條件已消失的未結 corr_group_trim 結案
                record_trim_anomalies(conn, cg, date)
            out["notes"] += cg["warnings"]
        except Exception as e:  # noqa: BLE001
            out["notes"].append(f"相關群計算失敗：{type(e).__name__}: {e}")

        # 10. 過期：超過約 15 交易日（21 曆日近似）仍未成交的買單
        stage = "expire"
        expired = []
        if write:
            cutoff = (d - timedelta(days=UNFILLED_EXPIRE_DAYS)).isoformat()
            for o in book.open_orders(conn):
                if o["side"] == "BUY" and o["created_date"] < cutoff:
                    broker.cancel_order(conn, o["order_id"], reason="逾期未成交（21 曆日近似 15 交易日）",
                                        status="expired")
                    expired.append(o["order_id"])
        out["expired"] = expired

        # 11. 淨值（同日重跑為 upsert）
        stage = "nav"
        if write:
            bench_div = sum(a.get("cash_per_share") or 0.0 for a in alpaca_actions
                            if a.get("type") == "cash_dividend" and normalize_ticker(a.get("ticker") or "") == bench
                            and prev < str(a.get("ex_date") or "") <= date)
            # 持股只缺當日 bar（停牌、IEX 無成交）：以最近一筆收盤估值，仍列 missing_close（M7-D1-10）
            stale = {t: c for t, c in _last_closes(bars, date).items() if t in book.positions(conn) and t not in closes}
            out["nav"] = book.record_nav(conn, date, {**stale, **closes}, bars_today.get(bench, {}).get("close"),
                                         benchmark_dividend=round(bench_div, 6),
                                         watch_closes={t: closes[t] for t in watch if t in closes})   # A3-M8C2-03
            if stale:
                nd = out["nav"]["data"]
                nd["missing_close"] = sorted(set(nd["missing_close"]) | set(stale))
                with book.transaction(conn):
                    book.write_row(conn, "nav_daily", out["nav"], ["date"])
                out["notes"].append("缺當日收盤、以最近收盤估值：" + "、".join(sorted(stale)))
            # 登錄表 weight 跟著每日淨值更新：觸發檢查與 PRICE 的「低於目標權重 → 加碼」才不會用分析當時的舊值
            for t, w in out["nav"]["data"]["weights"].items():
                Z.merge_registry(conn, t, {"weight": w}, date)
    except BaseException as e:  # noqa: BLE001 - 與 run_review 相同：錯誤寫進摘要與 runs 表
        out["error"] = f"{type(e).__name__}: {e}"
        out["error_stage"] = stage
        out["traceback"] = "".join(traceback.format_exception(e)).splitlines()[-20:]
        if not isinstance(e, Exception):   # Ctrl-C／SystemExit／SIGTERM：先記 failed 再中斷（M7-D2-01）
            try:
                _finish(conn, paths, run_id, out, write)
            finally:
                if write:
                    _restore_sigterm(sig_prev)
            raise
    if write:
        _restore_sigterm(sig_prev)
    _finish(conn, paths, run_id, out, write)
    return out


def _finish(conn, paths: Paths, run_id: str, out: dict, write: bool) -> None:
    if write:
        status = "failed" if "error" in out else "done"
        conn.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
                     (status, db.now_iso(), json.dumps(out, ensure_ascii=False, default=str), run_id))
        conn.commit()
        out_dir = paths.work / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                              encoding="utf-8")
