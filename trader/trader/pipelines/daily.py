"""每日收盤流程（M5）：交易日判斷 → 入金 → 行情與 ADV → 公司行動偵測與解讀 → 股利發放 → 撮合
→ 觸發檢查（持股）→ 組合層計畫（賣出、加碼、新買）→ 過期委託 → 淨值 → runs 表與摘要。

約定與近似（均在程式註解標明）：
- 「下一交易日」與「前一交易日」由 Alpaca 交易日曆取得（精確）。
- 買單 15 交易日過期、建倉窗口 15 交易日：以 21 曆日近似（推論）。
- 參考價以當日收盤代替（推論）；實際成交依 broker.match_orders 於下一交易日開盤撮合。
- 平均日成交額 ADV 以 40 曆日的資料取末 20 筆近似 20 交易日（推論）。
- 8-K 只抓持股；filingDate 以 asof=次日 取得「≤ 當日」的申報（SEC 客戶端的 asof 為嚴格小於）。

dry_run：不寫入任何資料（不入金、不跑公司行動、不撮合、不跑觸發檢查、不下單、不寫淨值），
只依現有資料算出賣出與買進計畫並回傳，標示 placed=False。
（未採用 FakeRunner＋SAVEPOINT 回滾：db.insert 與 writeback 會自行 commit，SAVEPOINT 無法回滾已提交的寫入。）
"""
from __future__ import annotations

import json
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .. import db
from ..config import Config
from ..data.inputs import make_clients
from ..data.universe import normalize_ticker
from ..jobs.engine import Engine
from ..ledger import book, broker, corp_actions
from ..llm.router import Router
from ..models import JobSpec
from ..paths import Paths
from ..portfolio import sizing as Z
from .review import ReviewError, run_review

BARS_LOOKBACK_DAYS = 40        # 曆日；ADV 取末 20 筆（推論：近似 20 交易日）
ADV_ROWS = 20
CAL_SEARCH_DAYS = 10          # 找前一／下一交易日的曆日範圍
UNFILLED_EXPIRE_DAYS = 21     # 推論：15 交易日的曆日近似
SEC_FORMS = ["8-K"]
CA_PROCESS_LAG_DAYS = 60      # 公司行動處理日最晚約晚於除息日數週（推論，取 60 曆日）


class DailyError(Exception):
    """每日流程無法進行（缺必要客戶端、交易日曆無資料）。"""


def _cal(alpaca, start: str, end: str) -> list[str]:
    return [str(r["date"])[:10] for r in alpaca.calendar(start, end) if r.get("date")]


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


def _place(conn, plan: dict, date: str, next_day: str, write: bool, out: dict) -> str | None:
    """依計畫下單（write=False 只記錄「將下單」）。冷靜期：PRICE 觸發的 BUY／ADD／TRIM 次日才可撮合。"""
    if plan["qty"] <= 0:
        return None
    cooling = plan.get("review_type") == Z.PRICE_TRIGGER and plan["action"] in ("BUY", "ADD", "TRIM")
    created = next_day if cooling else date
    rec = {**_tp(plan), "created_date": created, "placed": write}
    if not write:
        out["plans_dry_run"].append(rec)
        return None
    try:
        oid = broker.place_order(conn, plan["ticker"], plan["side"], plan["qty"], plan["limit_price"],
                                 plan["decision_id"], created)
    except broker.BrokerError as e:
        out["rejected"].append({**rec, "error": str(e)})
        return None
    rec["order_id"] = oid
    out["orders"].append(rec)
    if plan["side"] == "BUY":
        if plan["action"] == "BUY" and plan.get("target_weight"):
            Z.merge_registry(conn, plan["ticker"], {"build_plan": {
                "target_weight": plan["target_weight"], "tranches_left": Z.DEFAULT_TRANCHES,
                "days_above_cap": 0, "started": date}}, date)
        elif plan["action"] == "ADD":
            _consume(conn, plan["ticker"], date)
    return oid


def run_daily(cfg: Config, paths: Paths, conn, date: str, clients: dict[str, Any] | None = None,
              router: Router | None = None, skip_review: bool = False, dry_run: bool = False, sleep=None) -> dict:
    d = _date.fromisoformat(date)
    cl = clients if clients is not None else make_clients(cfg, paths)
    alpaca, sec = cl.get("alpaca"), cl.get("sec")
    if alpaca is None:
        raise DailyError("每日收盤需要 Alpaca 金鑰（交易日曆與行情）")
    if sec is None:
        raise DailyError("每日收盤需要 SEC 設定（SEC_USER_AGENT 須含 email；8-K 偵測）")

    today_days = _cal(alpaca, date, date)
    if date not in today_days:
        return {"skipped": "非交易日", "date": date}
    prev_days = [x for x in _cal(alpaca, (d - timedelta(days=CAL_SEARCH_DAYS)).isoformat(), date) if x < date]
    next_days = [x for x in _cal(alpaca, (d + timedelta(days=1)).isoformat(),
                                 (d + timedelta(days=CAL_SEARCH_DAYS)).isoformat()) if x > date]
    if not prev_days or not next_days:
        raise DailyError("交易日曆缺少前一或下一交易日")
    prev, nxt = prev_days[-1], next_days[0]

    run_id = f"daily-{date}"
    write = not dry_run
    out: dict[str, Any] = {"date": date, "run_id": run_id, "dry_run": dry_run, "prev_date": prev,
                           "next_date": nxt, "orders": [], "plans_dry_run": [], "rejected": [],
                           "corp_events": [], "settled": [], "fills": [], "notes": []}
    if write:
        book.ensure_initial_deposit(conn, cfg.account.initial_cash, date)
        if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
            db.insert(conn, "runs", {"run_id": run_id, "kind": "daily_close", "asof": date, "status": "running",
                                     "started_at": db.now_iso()})
        else:
            conn.execute("UPDATE runs SET status='running', started_at=?, finished_at=NULL WHERE run_id=?",
                         (db.now_iso(), run_id))
            conn.commit()

    try:
        positions = book.positions(conn)
        open_tickers = {o["ticker"] for o in book.open_orders(conn)}
        watch = {r["ticker"] for r in conn.execute("SELECT ticker FROM registry WHERE status = 'WATCH'")}
        bench = normalize_ticker(cfg.account.benchmark)
        universe = sorted(set(positions) | open_tickers | watch | {bench})
        bars = alpaca.bars(universe, start=(d - timedelta(days=BARS_LOOKBACK_DAYS)).isoformat(), end=date,
                           adjustment="raw") if universe else pd.DataFrame()
        bars_today, bars_prev, adv = _bars_maps(bars, date, prev)
        closes = {t: b["close"] for t, b in bars_today.items() if b.get("close") is not None}
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
        holdings = sorted(positions)
        filings_8k: dict[str, list[dict]] = {}
        for t in holdings:
            cik = (Z._reg_data(conn, t) or {}).get("cik") or sec.cik_for(t)
            if not cik:
                out["notes"].append(f"{t} 缺 CIK，略過 8-K 偵測")
                continue
            # SEC 的 asof 為嚴格小於：用次日取得「≤ 當日」的申報
            fl = sec.filings(cik, asof=(d + timedelta(days=1)).isoformat(), since=prev, forms=SEC_FORMS)
            filings_8k[t] = [f for f in fl if str(f.get("form", "")).upper() == "8-K"]
        # Alpaca 端點以「處理日」過濾（股利的處理日＝發放日，常晚於除息日數週）：查詢區間往後放寬，
        # 再由 detect 依除息／生效日落在 (prev, date] 過濾
        alpaca_actions = alpaca.corporate_actions(
            universe, start=prev, end=(d + timedelta(days=CA_PROCESS_LAG_DAYS)).isoformat()) if universe else []
        # 基準（SPY）只用於比較，不持有也不在觀察名單時不偵測：避免每季股利都派一個 CORP_ACTION 模型工作
        ca_tickers = sorted(set(positions) | open_tickers | watch)
        events = corp_actions.detect(conn, date, prev, ca_tickers, alpaca_actions, filings_8k, bars_prev,
                                     bars_today) if write else []
        # 先前工作失敗而未入帳的事件：不受 (prev, date] 窗口限制，重新派工（H2）
        if write:
            have = {e["event_id"] for e in events}
            retried = [e for e in corp_actions.unresolved_failed_events(conn) if e["event_id"] not in have]
            events = events + retried
            if retried:
                out["notes"].append("重派先前失敗的公司行動事件：" + "、".join(e["event_id"] for e in retried))
        out["corp_events"] = [{k: e.get(k) for k in ("event_id", "ticker", "source", "date")} for e in events]
        if events:
            router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
            engine = Engine(conn, router, paths, cfg, **({"sleep": sleep} if sleep else {}))
            engine.ctx.sec = sec
            engine.start_run(run_id, "daily_close", date)
            added: list[tuple[str, dict]] = []
            for i, ev in enumerate(events, 1):
                t = ev["ticker"]
                jid = f"{run_id}:{t}:CORP_ACTION:{i}"
                n = 1
                while conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (jid,)).fetchone():  # 同日重跑／重派：換新 id
                    n += 1
                    jid = f"{run_id}:{t}:CORP_ACTION:{i}r{n}"
                engine.add_job(JobSpec(
                    job_id=jid, run_id=run_id, job_type="CORP_ACTION", ticker=t,
                    asof=date, params={"event": ev, "position": corp_actions.position_of(conn, t),
                                       "open_orders": corp_actions.open_orders_of(conn, t)}))
                added.append((jid, ev))
            out["ca_engine"] = engine.run(run_id)
            # 工作沒有成功完成（失敗、預算耗盡等）：事件不得被當成已處理 → 異常暫停該檔，下次 daily 重派
            for jid, ev in added:
                r = conn.execute("SELECT status, error FROM jobs WHERE job_id=?", (jid,)).fetchone()
                if r and r["status"] not in ("done", "skipped"):
                    if r["status"] in ("pending", "running"):
                        conn.execute("UPDATE jobs SET status='failed', error=? WHERE job_id=?",
                                     ("daily 結束時未完成，事件改由下次 daily 重派", jid))
                        conn.commit()
                    corp_actions.record_job_failure(conn, ev, jid, r["error"])
                    out["notes"].append(f"{ev['ticker']} 公司行動工作未完成（{ev['event_id']}）：已暫停該檔，下次 daily 重派")

        # 5. 股利發放、6. 撮合（下一交易日撮合：只撮合 created_date < date）
        out["settled"] = corp_actions.settle_dividends(conn, date) if write else []
        if write:
            out["fills"] = broker.match_orders(conn, date, bars_today, cfg.account.slippage_bps,
                                               cfg.account.commission_per_order)

        # 7. 觸發檢查（持股）
        review_ok = True
        if write and not skip_review:
            router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
            rs = run_review(cfg, paths, conn, date, scope="holdings", run_id=f"review-{date}-holdings",
                            clients=cl, router=router)
            eng = rs.get("engine") or {}
            # 等待同意（強模型）不算失敗：該檔由 add_blocked 暫停加碼，其他照常
            review_ok = ("error" not in rs and eng.get("status", "done") in ("done", "awaiting_approval")
                         and eng.get("failed", 0) == 0)
            out["review"] = {"ok": review_ok, "run_id": rs.get("run_id"), "counts": rs.get("counts"),
                             "error": rs.get("error")}
        elif skip_review:
            out["notes"].append("--skip-review：未跑觸發檢查，沿用既有決策")
        if not review_ok:
            out["notes"].append("觸發檢查未成功：今日不下新買單與加碼，賣出照常")

        # 8. 建倉計畫的報價狀態（同一日只計一次）
        if write:
            Z.tick_build_state(conn, date, closes)

        # 9. 當日決策 → 組合計畫：賣出 → 加碼（與取消）→ 新買
        last_daily = conn.execute(
            "SELECT MAX(finished_at) FROM runs WHERE kind='daily_close' AND status='done' AND run_id<>?",
            (run_id,)).fetchone()[0]
        # 含前一交易日之後非交易日的決策，以及上次每日流程後才寫入的較早決策（等待同意後才完成的分析）
        decisions = Z.load_decisions(conn, date, since=prev, late_after=last_daily)
        sell_items = [x for x in decisions if x.get("action") in ("SELL_ALL", "TRIM")]
        buy_items = [x for x in decisions if x.get("action") in ("BUY", "ADD")]

        sells = Z.plan_sells(conn, date, sell_items, closes)
        for p in sells:
            if write:
                for oid in p.get("cancel_buys", []):
                    broker.cancel_order(conn, oid, reason="賣出前取消未成交買單")
            _place(conn, p, date, nxt, write, out)
        out["sells"] = [_tp(p) for p in sells]

        tranches, cancels = Z.plan_tranches(conn, date, closes, adv, cfg.account.slippage_bps)
        for c in cancels:
            if write:
                for o in book.open_orders(conn, c["ticker"]):
                    if o["side"] == "BUY":
                        broker.cancel_order(conn, o["order_id"], reason=c["reason"])
                _stop_build(conn, c["ticker"], date)
        out["tranche_cancels"] = cancels
        out["tranches"] = [_tp(p) for p in tranches]
        if review_ok:
            for p in tranches:
                _place(conn, p, date, nxt, write, out)

            buys = Z.plan_buys(conn, date, buy_items, closes, adv, cfg.account.slippage_bps)
            for p in buys:
                _place(conn, p, date, nxt, write, out)
            out["buys"] = [_tp(p) for p in buys]
            # 持股已滿：新 BUY 不建倉，換股由人工決定（記不暫停的異常，儀表板看得到；同檔未結時不重複記）
            for p in buys if write else []:
                if p["qty"] == 0 and str(p.get("reason") or "").startswith(f"持股已達 {Z.MAX_HOLDINGS} 檔"):
                    if conn.execute("SELECT 1 FROM anomalies WHERE ticker=? AND kind='switch_candidate' "
                                    "AND status='open'", (p["ticker"],)).fetchone() is None:
                        db.insert(conn, "anomalies", {
                            "ticker": p["ticker"], "kind": "switch_candidate",
                            "detail": json.dumps({"date": date, "decision_id": p.get("decision_id"),
                                                  "note": "持股已滿：須勝最弱持股（G8）才換股，由人工決定"},
                                                 ensure_ascii=False),
                            "status": "open", "pause_trading": 0, "created_at": db.now_iso()})
        else:
            out["buys"] = []

        # 10. 過期：超過約 15 交易日（21 曆日近似）仍未成交的買單
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
        if write:
            bench_div = sum(a.get("cash_per_share") or 0.0 for a in alpaca_actions
                            if a.get("type") == "cash_dividend" and normalize_ticker(a.get("ticker") or "") == bench
                            and prev < str(a.get("ex_date") or "") <= date)
            out["nav"] = book.record_nav(conn, date, closes, bars_today.get(bench, {}).get("close"),
                                         benchmark_dividend=round(bench_div, 6))
            # 登錄表 weight 跟著每日淨值更新：觸發檢查與 PRICE 的「低於目標權重 → 加碼」才不會用分析當時的舊值
            for t, w in out["nav"]["data"]["weights"].items():
                Z.merge_registry(conn, t, {"weight": w}, date)
    except Exception as e:  # noqa: BLE001 - 與 run_review 相同：錯誤寫進摘要與 runs 表
        out["error"] = f"{type(e).__name__}: {e}"

    if write:
        status = "failed" if "error" in out else "done"
        conn.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
                     (status, db.now_iso(), json.dumps(out, ensure_ascii=False, default=str), run_id))
        conn.commit()
        out_dir = paths.work / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                                              encoding="utf-8")
    return out
