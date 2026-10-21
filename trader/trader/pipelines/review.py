"""觸發檢查 pipeline（M4）：登錄表 → 價格／利率／新聞檔 → skill review_due.py（子程序，不呼叫模型）
→ 每檔至多一個起始工作（TRIAGE／DOSSIER／THESIS／PRICE）→ 引擎。

輸出目錄：work/{run_id}/（registry.json、daily.csv、news.json、due.json、summary.json）。
規則（公式、閾值、動作判定）全部在 skill 的 review_due.py，這裡不複製。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .. import db
from ..analysis import writeback
from ..analysis.dossier_prep import DossierError, _user_agent, is_price_headline
from ..config import Config
from ..data.http import DataError, ny_today
from ..data.inputs import fetch_rates, make_clients
from ..data.pe_median import get_frozen
from ..data.prices_adapter import daily_closes
from ..data.universe import normalize_ticker
from ..jobs.engine import Engine
from ..llm.router import Router
from ..models import JobSpec, JobStatus
from ..ledger import book
from ..paths import Paths
from ..portfolio.holdings_view import watchlist_tickers, write_view
from ..portfolio.sizing import merge_registry
from .analyze import _portfolio_state, base_params
from .screen import screen_context_for, screen_price_context_for

REGISTRY_SCHEMA = "berkshire-deep-merged/registry-v1"
SIGNAL_VERSION = "review-due-v2"
SCOPES = ("holdings", "watch", "all")
SCOPE_STATUS = {"holdings": "HOLDING", "watch": "WATCH"}
# 兩週觀察名單排程另帶入的狀態（D27）：缺資料檔（新年報／新季報 → FULL）、淘汰類（只看重大 8-K；誠信淘汰另限）。
# 判定規則在 review_due.py；export_registry(conn, "watch") 本身仍只回 WATCH。
WATCH_RUN_EXTRA = ("MISSING_DATA", "TOO_HARD", "REJECT")
PRICE_LOOKBACK_DAYS = 200     # 曆日；skill 需要至少 100 個交易日
NEWS_FALLBACK_DAYS = 30       # 登錄表都沒有 last_review_date 時的新聞起算日
SKILL_TIMEOUT_S = 1800
FILINGS_FAIL_STREAK = 3       # 同檔申報清單連續抓取失敗的觸發檢查次數 → 異常提醒使用者（M2-RS8-03）
MISSING_RETRY_DAYS = 91       # D74：持股完整分析以 MISSING_DATA 停止後，滿 13 週才因時間類觸發再排完整分析
NEW_QUARTERLY = "上次分析後有新季報"   # review_due 新季報理由（D74：缺資料持股有新季報即重做完整分析，同 D27）


class ReviewError(Exception):
    """觸發檢查無法完成（skill 失敗、輸出格式不符、範圍不合法）。"""


# review_due.check 以數字運算的欄位、以日期比較的欄位
_NUM_FIELDS = ("review_price", "t30_at_valuation", "auto_buy_cap", "sell_review_above", "absurd_price_line",
               "cost", "weight", "target_weight")
_DATE_FIELDS = ("last_review_date", "last_full_review_date", "first_buy_date")


def _is_ymd(v: Any) -> bool:
    try:
        date.fromisoformat(str(v)[:10])
        return isinstance(v, str)
    except ValueError:
        return False


def _norm_ymd(v: Any) -> str | None:
    """日期字串正規化為 YYYY-MM-DD：YYYYMMDD 轉成橫線形、帶時間者截斷；不是字串或無法解析 → None（A3-M2E-01：
    review_due 以全字串比較日期，'20261001' 或 '2026-10-01T08:00' 會讓新申報被靜默略過或讓日期解析拋例外）。"""
    if not isinstance(v, str):
        return None
    s = v.strip()
    try:
        if len(s) >= 8 and s[:8].isdigit() and (len(s) == 8 or not s[8].isdigit()):
            return date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat()
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _sanitize_entry(d: dict, notes: list[str]) -> dict:
    """型別不合的欄位丟掉並記註（雙保險：一檔的壞欄位不得讓 review_due.py 崩潰、整批觸發檢查停擺；M6-D1-01）。"""
    t = d.get("ticker")
    for k in _NUM_FIELDS:
        v = d.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v != v):
            notes.append(f"{t}：登錄表 {k}={v!r} 不是數字，本次觸發檢查略過此欄")
            d.pop(k)
    for k in _DATE_FIELDS:
        if k not in d:
            continue
        n = _norm_ymd(d[k])
        if n is None:
            notes.append(f"{t}：登錄表 {k}={d[k]!r} 不是日期，本次觸發檢查略過此欄")
            d.pop(k)
        else:
            d[k] = n   # A3-M2E-01：交 review_due 前正規化（全字串比較）
    kc = d.get("kill_criteria_due")
    if kc is not None:
        ok = [x if x.get("due") is None else {**x, "due": _norm_ymd(x.get("due"))}
              for x in kc if isinstance(x, dict) and (x.get("due") is None or _norm_ymd(x.get("due")))] \
            if isinstance(kc, list) else []
        if not isinstance(kc, list) or len(ok) != len(kc):
            notes.append(f"{t}：登錄表 kill_criteria_due 有 {len(kc) - len(ok) if isinstance(kc, list) else '整欄'}"
                         " 筆格式不合（須為 [{text, due}]），本次略過")
        d["kill_criteria_due"] = ok
    return d


def export_registry(conn, scope: str, notes: list[str] | None = None, extra_statuses: tuple = ()) -> list[dict]:
    """登錄表列 → dict（data 內容＋ticker、status）。scope: holdings（HOLDING）｜watch（WATCH）｜all；
    extra_statuses：另外納入的狀態。notes 給定時收集被略過的壞欄位說明。
    已下架（RETIRED，`trader registry retire`）的一律不匯出，含 scope=all。"""
    if scope not in SCOPES:
        raise ReviewError(f"scope 不合法：{scope!r}（應為 {'、'.join(SCOPES)}）")
    notes = notes if notes is not None else []
    out = []
    for r in conn.execute("SELECT ticker, status, data FROM registry ORDER BY ticker"):
        if scope in SCOPE_STATUS and r["status"] != SCOPE_STATUS[scope] and r["status"] not in extra_statuses:
            continue
        if r["status"] == "RETIRED":
            continue
        d = json.loads(r["data"]) if r["data"] else {}
        # 空欄位（schema 骨架填 None）略過：skill 以 e.get(k, 預設) 取值，None 會讓清單欄位無法迭代
        d = {k: v for k, v in d.items() if v is not None}
        d["ticker"] = normalize_ticker(r["ticker"])
        d["status"] = r["status"] or d.get("status")
        fb = writeback.first_buy_date(conn, r["ticker"])   # review_due 只認本持有期的 last_trim（D03；與回寫端同一來源）
        if fb:
            d["first_buy_date"] = fb
        out.append(_sanitize_entry(d, notes))
    return out


def _missing_data_attempt(conn, ticker: str, asof: str, full_last: Any) -> str | None:
    """D74：持股最近一次完整分析以 MISSING_DATA 停止（使用者拒絕強模型、金融前置缺項、輸出不合格；
    write_stop_result 不推進 last_full_review_date）→ 回傳該次嘗試日；否則 None。"""
    from ..jobs.review_jobs import _last_full_card
    last = _last_full_card(conn, ticker, asof)
    if last and last[1].get("reason_code") == "MISSING_DATA" and last[0] > str(full_last or "")[:10]:
        return last[0]
    return None


CHECK_ERROR_KIND = "review_check_error"


def record_check_errors(conn, rows: list[dict], asof: str, due_path: str | None = None) -> list[dict]:
    """due.json 中 check_error 非空的檔（review_due 單檔 check() 例外，A3-M2E-02）各開一筆異常 kind=review_check_error
    （不暫停交易）。同一檔同一 asof 已有未結者不重複新增（重跑同一決策日）；不同 asof 照開新的一筆。回傳出錯清單。"""
    out = []
    for r in rows:
        err = r.get("check_error")
        if not err:
            continue
        t = r.get("ticker")
        out.append({"ticker": t, "check_error": str(err)})
        dup = False
        for a in conn.execute("SELECT detail FROM anomalies WHERE ticker IS ? AND kind=? AND status='open'",
                              (t, CHECK_ERROR_KIND)):
            try:
                dup = json.loads(a["detail"] or "{}").get("date") == asof
            except ValueError:
                dup = False
            if dup:
                break
        if dup:
            continue
        db.insert(conn, "anomalies", {
            "ticker": t, "kind": CHECK_ERROR_KIND, "status": "open", "pause_trading": 0,
            "detail": json.dumps({"date": asof, "check_error": str(err), "due": due_path,
                                  "note": "review_due 對本檔觸發檢查拋例外：本次全部觸發（申報、價格、論點、新聞）未檢查，"
                                          "沒有建任何工作，不等於「沒有變化」；需人工排查錯誤後結案"},
                                 ensure_ascii=False), "created_at": db.now_iso()})
    return out


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _last_closes(df: pd.DataFrame) -> dict[str, dict]:
    """每檔最後一筆收盤：{ticker: {price, price_date}}。"""
    if df.empty:
        return {}
    last = df.sort_values(["ticker", "date"]).groupby("ticker").tail(1)
    return {r["ticker"]: {"price": float(r["close"]), "price_date": str(r["date"])} for r in last.to_dict("records")}


def _held_closes(conn, prices, asof: str, last_px: dict[str, dict]) -> dict[str, dict]:
    """G8 比較檔用：scope 範圍外的持股與觀察名單另抓決策日收盤；抓不到就缺，比較檔會標註。"""
    out = dict(last_px)
    miss = [t for t in dict.fromkeys(list(book.positions(conn)) + watchlist_tickers(conn)) if t not in out]
    if miss and prices is not None:
        start = (date.fromisoformat(asof) - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
        bars = prices.bars(miss, start=start, end=asof, adjustment="raw")
        if not bars.empty:
            out.update(_last_closes(bars[bars["date"] <= asof][["ticker", "date", "close"]]))
    return out


def run_review(cfg: Config, paths: Paths, conn, asof: str, scope: str = "all", run_id: str | None = None,
               clients: dict[str, Any] | None = None, router: Router | None = None,
               offline_filings: Path | None = None, due_only: bool = False) -> dict[str, Any]:
    if scope not in SCOPES:
        raise ReviewError(f"scope 不合法：{scope!r}")
    run_id = run_id or f"review-{asof}-{scope}"
    cl = clients if clients is not None else make_clients(cfg, paths)
    sec, alpaca, fred = cl.get("sec"), cl.get("alpaca"), cl.get("fred")
    prices = cl.get("prices", alpaca)   # 研究用價格來源（D34）：觸發檢查與比較檔取價；新聞仍走 Alpaca（X20）
    out = paths.work / run_id
    out.mkdir(parents=True, exist_ok=True)
    if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        db.insert(conn, "runs", {"run_id": run_id, "kind": "review", "asof": asof, "status": "running",
                                 "started_at": db.now_iso()})
    else:
        conn.execute("UPDATE runs SET status='running', started_at=?, finished_at=NULL WHERE run_id=?",
                     (db.now_iso(), run_id))
        conn.commit()
    summary: dict[str, Any] = {"run_id": run_id, "asof": asof, "scope": scope, "outdir": str(out)}
    engine_result: dict[str, Any] | None = None
    try:
        engine_result = _execute(cfg, paths, conn, asof, scope, run_id, out, sec, alpaca, prices, fred,
                                 router, offline_filings, due_only, summary)
        status = engine_result["status"] if engine_result else "done"
    except Exception as e:  # noqa: BLE001 - 與 run_screen 相同：錯誤寫進 summary 與 runs 表
        summary["error"] = f"{type(e).__name__}: {e}"
        status = "failed"
    summary["engine"] = engine_result
    _write_json(out / "summary.json", summary)
    conn.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?",
                 (status, db.now_iso(), json.dumps(summary, ensure_ascii=False, default=str), run_id))
    conn.commit()
    return summary


def _execute(cfg, paths, conn, asof, scope, run_id, out, sec, alpaca, prices, fred, router, offline_filings,
             due_only, summary) -> dict[str, Any] | None:
    """流程 1–9；summary 原地補欄位。due_only 或建工作時回傳引擎結果（due_only 回 None）。"""
    # 1–2. 登錄表 → registry.json（缺 cik 由 SEC 對照補；補不到則排除）
    skipped: list[dict] = []
    entries: list[dict] = []
    reg_notes: list[str] = []
    for e in export_registry(conn, scope, reg_notes, WATCH_RUN_EXTRA if scope == "watch" else ()):
        t = e["ticker"]
        if not e.get("cik"):
            cik = sec.cik_for(t) if sec is not None else None
            if not cik:
                skipped.append({"ticker": t, "reason": "缺 CIK 且 SEC 對照無此代號"})
                continue
            e["cik"] = cik
        entries.append(e)
    # 申報清單一律經資料層（SecClient：重試、快取、前身 CIK）取得，以 --offline 交給 skill，
    # 不讓 skill 自己 urllib 連 SEC。單檔抓不到 → 以 null 交 skill：只略過申報類觸發（不升 THESIS 白跑模型；
    # M4-D10-02），價格類、滿 12 個月等其餘觸發照查（M2-RS8-03）。呼叫端給 offline_filings 時以其為準（測試覆寫）。
    failed_now: set[str] = set()   # 本次已知申報抓取失敗的檔
    if offline_filings is None and sec is not None and entries:
        filings: dict[str, list[dict] | None] = {}
        stale = getattr(sec, "stale", None)  # SecClient 抓取失敗改用舊快取時不拋例外，只記在 sec.stale
        tail = "（只略過申報類觸發，其餘觸發照查；申報窗口保留到抓取成功）"
        for e in entries:
            n_stale = len(stale) if stale is not None else 0
            try:
                # max_age_hours=0：當次重抓，不用 20 小時內其他流程刷新過、可能缺最新申報的快取（X13）
                got = sec.offline_filings({e["ticker"]: e["cik"]}, asof, max_age_hours=0)
            except Exception as ex:  # noqa: BLE001 - 單檔失敗不影響其他檔；下次觸發檢查重試
                skipped.append({"ticker": e["ticker"],
                                "reason": f"SEC 申報清單抓取失敗：{type(ex).__name__}: {str(ex)[:200]}{tail}"})
                got = None
            # 只計現行 CIK 的檔：前身（已停止申報）用舊快取不影響完整性，前身失敗照 sec.warnings 處理（X13）
            cur = f"sub_CIK{str(e['cik']).zfill(10)}.json"
            if got is not None and stale is not None and cur in stale[n_stale:]:
                # 舊清單可能缺最新申報：照用會以舊清單判「無新申報」（P43R），視同抓取失敗
                skipped.append({"ticker": e["ticker"], "reason": f"SEC 申報清單抓取失敗，僅有舊快取：{cur}{tail}"})
                got = None
            if got is None:
                failed_now.add(e["ticker"])
                filings[e["ticker"]] = None
            else:
                filings.update(got)
        offline_filings = out / "filings.json"
        _write_json(offline_filings, filings)
    elif offline_filings is not None:
        try:   # 呼叫端給的檔：值為 null ＝該檔抓取失敗；格式不合交 skill 報錯
            off = json.loads(Path(offline_filings).read_text(encoding="utf-8"))
            failed_now = {t for t, v in off.items() if v is None} if isinstance(off, dict) else set()
        except (OSError, ValueError):
            pass
    # 申報抓取曾失敗、本次成功的檔：交 review_due 的 filings_since（只放寬申報窗口）＝首次失敗前的窗口起點
    # （filings_fail.since；不早於上次完整分析日），補查失敗期間其他工作推進窗口而漏掉的申報（M2-RS8-03）。
    # last_review_date 不動：價格、失效條件、新聞等邊緣觸發仍以上次分析日為基準，不重複觸發（A3-M2E-03）。
    # since 無效（失敗時登錄表缺 last_review_date）不放寬，但照樣列入 recovered，run 末清標記（RM2-12R）
    recovered: set[str] = set()
    for e in entries:
        ff = e.get("filings_fail")
        if offline_filings is None or e["ticker"] in failed_now or not isinstance(ff, dict):
            continue
        recovered.add(e["ticker"])
        since = ff.get("since")
        if not _is_ymd(since):
            continue
        w = max(since[:10], str(e.get("last_full_review_date") or since)[:10])
        if w < str(e.get("last_review_date") or "9999-12-31")[:10]:
            e["filings_since"] = w
    # D74：持股完整分析缺資料後的重試節流。13 週內交 review_due 的完整分析基準日改為該次嘗試日
    # （「滿 12 個月」、next_full_review_due、權重 >25% 邊緣觸發不再每兩週重排）；新年報照常 FULL，新季報下方升 FULL
    md_retry: dict[str, str] = {}
    for e in entries:
        if e.get("status") != "HOLDING":
            continue
        att = _missing_data_attempt(conn, e["ticker"], asof, e.get("last_full_review_date"))
        if att is None:
            continue
        md_retry[e["ticker"]] = att
        if (date.fromisoformat(asof) - date.fromisoformat(att)).days < MISSING_RETRY_DAYS:
            e["last_full_review_date"] = att
            if e.get("weight") is not None:
                e["full_review_weight"] = e["weight"]
    if md_retry:
        summary["missing_data_retry"] = md_retry
    summary["skipped"] = skipped
    if reg_notes:
        summary["registry_fields_dropped"] = reg_notes
    tickers = [e["ticker"] for e in entries]
    reg_path = out / "registry.json"
    _write_json(reg_path, {"schema": REGISTRY_SCHEMA, "entries": entries})

    # 3. 日線 → daily.csv（拆股調整價，資料層 daily_closes；只留 ≤ asof）。拆股調整的最後一筆收盤與 raw 相同，
    #    歷史則與公司行動已換算的登錄表每股欄位同一口徑，拆股不再誤觸發單季 −25%／±4%（M7-D1-05、M4-D10-03）。
    #    沒有價格客戶端時寫表頭空檔，避免 skill 自己連網抓價
    px_path = out / "daily.csv"
    if prices is not None and tickers:
        start = (date.fromisoformat(asof) - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
        daily = daily_closes(prices.bars(tickers, start=start, end=asof, adjustment="split"), asof)
    else:
        daily = pd.DataFrame(columns=["ticker", "date", "close"])
    daily.to_csv(px_path, index=False)
    last_px = _last_closes(daily)
    # M4-D4-02：拆股字串（同初篩 prices_adapter：4 年公司行動、只計 ex_date ≤ 價格日）給資料包封面股數與內部人表換算；
    #           抓取失敗或無價格客戶端 → 不帶（None＝拆股未核，資料包照實標示）
    review_splits: dict[str, str] = {}
    # A3-M7D-04：補跑舊 asof 時，asof 之後的拆合股已被套進 split 調整日線，登錄表每股欄位卻要等該日 daily 的 CORP_ACTION
    # 才換算 → 口徑不一。簡化：asof 早於紐約今天時，確定 asof 之後有拆合股的檔，本次略過價格類觸發並記 note
    backfill = asof[:10] < ny_today()
    later_split: set[str] | None = set()
    if prices is not None and tickers and hasattr(prices, "corporate_actions"):
        from ..data.prices_adapter import SPLIT_TYPES, _years_before, splits_string
        try:
            acts = prices.corporate_actions(tickers, _years_before(date.fromisoformat(asof), 4).isoformat(),
                                            max(asof[:10], ny_today()) if backfill else asof)
            review_splits = {t: splits_string(acts, t, (last_px.get(t) or {}).get("price_date") or asof)
                             for t in tickers}
            later_split = {normalize_ticker(a.get("ticker") or "") for a in acts
                           if a.get("type") in SPLIT_TYPES and str(a.get("ex_date") or "")[:10] > asof[:10]}
        except Exception as e:  # noqa: BLE001 - 拆股未核照實標示，不擋觸發檢查
            summary["splits_fetch_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            later_split = None
    skill_daily = daily
    skip = (later_split or set()) & set(tickers)   # 只略過確定 asof 後有拆合股的檔
    if backfill and later_split is None:
        # 公司行動取不到：無法確認口徑，價格觸發照建（略過全部檔會讓補跑日整天沒有價格觸發），只記 note（A3-M7D-04 返工）
        summary["backfill_split_check_unavailable"] = {
            "tickers": sorted(tickers),
            "note": "補跑日：公司行動取不到、無法確認 asof 後有無拆合股；價格類觸發照建，若期間有拆合股，"
                    "拆股調整日線與登錄表每股欄位可能口徑不一，請人工核對觸發價"}
    if backfill and not daily.empty and skip:
        skill_daily = daily[~daily["ticker"].isin(skip)]
        skill_px = out / "daily_skill.csv"
        skill_daily.to_csv(skill_px, index=False)
        summary["backfill_price_triggers_skipped"] = {
            "tickers": sorted(skip),
            "note": "補跑日：asof 之後有拆合股，拆股調整日線與登錄表每股欄位口徑不一，本次略過這些檔的價格類觸發（下次觸發檢查照查）"}
    else:
        skill_px = px_path
    if skill_daily.empty:  # skill 對「只有表頭」的檔會因空日期欄失敗；改給一列永不匹配的哨兵列（僅供 skill 讀取）
        skill_px = out / "prices_sentinel.csv"
        skill_px.write_text(f"ticker,date,close\n_NONE_,{asof},\n", encoding="utf-8")

    # 4. 30 年期公債殖利率（取不到則不傳 --t30）
    rates: dict[str, Any] | None = None
    t30 = None
    if fred is None:
        summary["t30"] = {"error": "未設定 FRED 金鑰"}
    else:
        # A3-M4B-01：與 fetch_inputs 同一份組裝（t30、20 年中位、D29 3 個月國庫券與 5／10 年平均、停更、FRED 警示；
        # 單項失敗只記該項 error，X29）；DGS30 取不到 → rates 只有 error 與其他項，照常執行觸發檢查
        rates = fetch_rates(fred, asof)
        t30 = rates.get("t30")
        if rates.get("error"):
            summary["t30"] = {"error": rates["error"]}
        else:
            summary["t30"] = {"t30": t30, "t30_date": rates.get("t30_date")}
            if rates.get("stale"):
                summary["t30"].update({"stale": True, "warning": rates.get("warning")})
        if rates.get("fred_warnings"):
            summary["t30"]["fred_warnings"] = list(rates["fred_warnings"])

    # 5. 新聞 → news.json（只在有 Alpaca 時）
    news_path: Path | None = None
    if alpaca is not None and tickers:
        dates = [str(e["last_review_date"])[:10] for e in entries if e.get("last_review_date")]
        floor = (date.fromisoformat(asof) - timedelta(days=NEWS_FALLBACK_DAYS)).isoformat()
        # A3-X1-03：起點不早於 asof−NEWS_FALLBACK_DAYS（久未檢查的觀察檔不得把全組合的新聞窗口拉到數月前、分頁暴增）
        start = max(min(dates), floor) if dates else floor
        news: dict[str, list[dict]] = {t: [] for t in tickers}
        dropped = 0
        try:
            got_news = alpaca.news(tickers, start=start, end=asof)
        except DataError as e:   # A3-X1-03：新聞抓不到只略過新聞類觸發（同 SEC／FRED／拆股降級），不讓整個 review 失敗、全組合停買
            got_news = None
            summary["news_error"] = f"{type(e).__name__}: {str(e)[:200]}（本次不帶新聞，新聞類觸發未檢查）"
        for n in got_news or []:
            if is_price_headline(n.get("headline", "")):  # 價格隔離：目標價、評等、漲跌標題不進任何下游
                dropped += 1
                continue
            news.setdefault(n["ticker"], []).append({"date": n["date"], "headline": n["headline"],
                                                     "source": n["source"], "url": n["url"]})
        if got_news is not None:
            summary["news_price_headlines_dropped"] = dropped
            news_path = out / "news.json"
            _write_json(news_path, news)

    # 6. 子程序執行 skill 的 review_due.py（規則以 skill 為準）
    due_path = out / "due.json"
    cmd = [sys.executable, str(paths.skill_deep / "scripts" / "review_due.py"),
           "--registry", str(reg_path), "--asof", asof, "--prices", str(skill_px), "--out", str(due_path)]
    if t30 is not None:
        cmd += ["--t30", repr(float(t30))]
    if news_path is not None:
        cmd += ["--news", str(news_path)]
    if offline_filings is not None:
        cmd += ["--offline", str(offline_filings)]
    env = dict(os.environ)
    if sec is not None:
        try:
            env["SEC_USER_AGENT"] = _user_agent(sec)
        except DossierError:
            pass  # 離線模式不需要；連網模式由 skill 自行檢查並失敗
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=SKILL_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired as e:
        raise ReviewError(f"review_due.py 逾時（{SKILL_TIMEOUT_S} 秒）") from e
    if p.returncode != 0:
        raise ReviewError(f"review_due.py 失敗（{p.returncode}）：{(p.stderr or p.stdout)[-1500:]}")

    # 7. 讀 due.json
    due = json.loads(due_path.read_text(encoding="utf-8"))
    if due.get("signal_version") != SIGNAL_VERSION or due.get("asof") != asof:
        raise ReviewError(f"due.json 版本或決策日不符：{due.get('signal_version')!r} / {due.get('asof')!r}")
    rows = due.get("results") or []
    for r in rows:   # D74（同 D27）：缺資料持股有新季報 → 重做完整分析
        att = md_retry.get(r["ticker"])
        if att and r["action"] == "THESIS" and any(str(x).find(NEW_QUARTERLY) >= 0 for x in r.get("reasons") or []):
            r["action"], r["triage"] = "FULL", []
            r["reasons"] = [f"[FULL] 上次完整分析（{att}）缺資料：有新季報，重做完整分析（D74）"] + list(r.get("reasons") or [])
    summary.update({
        "counts": dict(Counter(r["action"] for r in rows)),
        "due_path": str(due_path),
        "jobs": {},
    })
    # 價格缺／過期的持股與觀察名單：價格類觸發本次未檢查（M2_D9_05）
    bad_px = [{"ticker": r["ticker"], "status": r.get("status"), "price_status": r.get("price_status")}
              for r in rows if r.get("status") in ("HOLDING", "WATCH") and r.get("price_status") not in (None, "ok")]
    if bad_px:
        summary["price_status_not_ok"] = bad_px

    # 8. due_only：只回傳訊號摘要
    if due_only:
        return None

    # 8b. 申報抓取失敗：登錄表記連續次數與窗口起點（建工作前寫，工作推進 last_review_date 也不丟窗口），
    #     連續 FILINGS_FAIL_STREAK 次開不暫停的異常（M2-RS8-03）
    entries_by_t = {e["ticker"]: e for e in entries}
    for r in rows:
        if r.get("filings_ok") is not False:
            continue
        t = r["ticker"]
        recovered.discard(t)   # skill 端仍失敗：不算恢復
        if t not in failed_now:
            skipped.append({"ticker": t, "reason": "申報清單抓取失敗（review_due）：只略過申報類觸發，其餘觸發照查"})
        ff = entries_by_t.get(t, {}).get("filings_fail")
        ff = dict(ff) if isinstance(ff, dict) else {}
        n = int(ff.get("count") or 0) + (ff.get("last") != asof)   # 同一決策日重跑不重複計
        ff = {"count": n, "since": ff.get("since") or entries_by_t.get(t, {}).get("last_review_date"), "last": asof}
        merge_registry(conn, t, {"filings_fail": ff}, asof)
        if n >= FILINGS_FAIL_STREAK and conn.execute(
                "SELECT 1 FROM anomalies WHERE ticker=? AND kind='trigger_fail_streak' AND status='open'",
                (t,)).fetchone() is None:
            db.insert(conn, "anomalies", {
                "ticker": t, "kind": "trigger_fail_streak", "status": "open", "pause_trading": 0,
                "detail": json.dumps({"date": asof, "count": n, "since": ff["since"],
                                      "note": f"SEC 申報清單連續 {n} 次觸發檢查抓取失敗：{ff['since'] or '最早'} 起的"
                                              "新年報／季報／8-K 未檢查（其他觸發照常），需人工排查 CIK 或連線"},
                                     ensure_ascii=False), "created_at": db.now_iso()})

    # 8c. 單檔觸發檢查例外（review_due check_error 非空）：該檔已標 NONE＋全部觸發未檢查，不得當「沒有變化」
    #     → 每檔開一筆不暫停的異常，同 asof 不重複（A3-M2E-02 宿主端）
    cerr = record_check_errors(conn, rows, asof, str(due_path))
    if cerr:
        summary["check_errors"] = cerr

    # 9. 建工作：每檔至多一個起始工作
    pstate = _portfolio_state(cfg, conn)
    write_view(conn, paths, run_id, asof, _held_closes(conn, prices, asof, last_px), rates, fred)
    pe = get_frozen(paths, asof)
    router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
    engine = Engine(conn, router, paths, cfg)
    engine.ctx.sec = sec
    engine.ctx.prices = prices   # M4-R3-02
    engine.start_run(run_id, "review", asof)  # run 已於 run_review 建立，這裡只是保險
    price_no_cap: list[str] = []
    for r in rows:
        t, action = r["ticker"], r["action"]
        # 申報抓取失敗（filings_ok=false）的檔照建：review_due 已只略過申報類觸發（M2-RS8-03）
        triage = r.get("triage") or []
        if triage:
            jtype = "TRIAGE"
        elif action == "FULL":
            jtype = "DOSSIER"
        elif action == "THESIS":
            jtype = "THESIS"
        elif action == "PRICE":
            if (paths.state / "cap_inputs" / f"{t}.json").is_file():
                jtype = "PRICE"
            else:
                jtype = "DOSSIER"
                price_no_cap.append(t)
        else:  # NONE 且無初判項目：不建工作，沿用上次結論
            continue
        e = entries_by_t.get(t, {})
        thesis = r.get("thesis_file")
        # screen_context：最近一次初篩的全池判定（之後初篩改判刪除也傳到觀察名單與持股；M1-D7-4）
        ctx = screen_context_for(paths, asof, t, "觸發檢查：非初選交接")
        px = dict(last_px.get(t) or {})
        if t in review_splits:   # M4-D4-02
            px["splits"] = review_splits[t]
        if r.get("price_status") == "stale":   # M4-R4S4-03：同 review_due STALE_PRICE_TRADING_DAYS 門檻，過期收盤不當現價
            px = {k: v for k, v in px.items() if k == "splits"}   # 拆股字串與價格無關，照帶
            px["price_src"] = (f"過期：最後收盤 {(last_px.get(t) or {}).get('price_date')} 早於決策日超過 5 個交易日"
                               "（review_due price_status=stale），不當現價")
            summary.setdefault("price_stale_not_passed", []).append(t)
        params = base_params(px, rates, ctx, pstate, pe)
        if spc := screen_price_context_for(paths, ctx):   # T3／T5 只進價格檔（D71、M1-R6-02）
            params["price_meta"]["screen_price_context"] = spc
        params.update({
            "holding": r.get("status") == "HOLDING",
            "status": r.get("status"),
            "cik": e.get("cik"),
            "reasons": r.get("reasons") or [],
            "triage": triage,
            "kill_criteria": r.get("kill_criteria") or [],
            "kill_criteria_due": e.get("kill_criteria_due") or [],
            "thesis_file": str(thesis) if thesis else None,
            "last_review_date": r.get("last_review_date"),
            "base_action": action,
            "also_price": bool(r.get("also_price")),   # THESIS 判完好後續接 PRICE（M2_D7_01）
            # 新聞標題（已濾價格標題）給 TRIAGE／THESIS 提示（M3-D10-03）；完整分析 FULL_A／FULL_B 亦經
            # trigger_text(news=True) 看到窗口新聞標題（已遮價格數字；D75，M3-RS3-04）
            "news": r.get("news") or [],
            # D03：review_due 判定的「荒謬級減碼後回到線下」證據；PRICE→HOLD_REVIEW 沿用 params，回寫端認同一份
            "absurd_rearm": r.get("absurd_rearm"),
        })
        spec = JobSpec(job_id=f"{run_id}:{t}:{jtype}:1", run_id=run_id, job_type=jtype, ticker=t, asof=asof,
                       params=params)
        if engine.add_job(spec):
            summary["jobs"][t] = jtype
    summary["price_no_cap_input"] = price_no_cap

    # 10–11. 跑引擎
    result = engine.run(run_id)

    # 申報恢復的檔：本 run 該檔沒有工作、或工作全部已處理（done／conservative_fallback，兩者都推進 last_review_date）
    # → 清掉失敗標記（未完成或失敗則保留，下次再補查；RM2-12R）
    for t in recovered:
        if conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND ticker=? AND status NOT IN (?, ?)",
                        (run_id, t, JobStatus.DONE, JobStatus.CONSERVATIVE_FALLBACK)).fetchone() is None:
            merge_registry(conn, t, {"filings_fail": None}, asof)

    # 12. TRIAGE 判否的決策日誌（D59）改由 TriageHandler 在工作完成時寫（A3-M5B-05）
    return result
