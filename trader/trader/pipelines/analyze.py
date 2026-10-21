"""深度分析 pipeline：對指定代號（或初選 to_analyze）跑 DOSSIER → FULL_A → FULL_B → RED_TEAM?，終點回寫 DB。

輸入來源：
- 初選結果 state/screens/{screen_asof}/：handoff_noprice.json（screen_context）、summary.json（to_analyze）、
  inputs/prices.csv 與 rates.json（價格與利率；只寫進價格檔，FULL_B 才開）、screen_handoff.json 的
  price_context.price_distress_codes（T3／T5、特殊交棒標記；經 price_meta 只進價格檔，D71）。
- 非短名單代號（手動 --tickers）：screen_context 取最近一次初篩的全池判定 decisions.json，查不到標「未篩」（M1-D7-4）。
- 不在初選輸入裡的代號：以 fetch_inputs 抓該檔價格與利率到 run 目錄。
- 組合狀態：M5 帳本未啟用前，以初始資金、無持股代入（明確標註）。
- G8 持股比較檔：work/{run_id}/_portfolio/holdings_g8.json（持股與觀察名單以決策日收盤重算預期報酬，加國庫券與大盤；只給 FULL_B、HOLD_REVIEW）。
- 大型股 P/E 中位：當月凍結值（沒有則上月；都沒有 → 缺，P4′ 不適用）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import Config
from ..data.inputs import D29_RATE_KEYS, fetch_inputs, fetch_rates, make_clients
from ..data.pe_median import get_frozen
from ..data.universe import normalize_ticker
from ..jobs.engine import Engine
from ..ledger import book
from ..llm.router import Router
from ..models import JobSpec
from ..paths import Paths
from ..portfolio.holdings_view import watchlist_tickers, write_view
from .screen import screen_context_for, screen_price_context_for


class AnalyzeError(Exception):
    """無法開始深度分析（缺初選結果、缺 SEC 設定等）。"""


def _portfolio_state(cfg: Config, conn) -> dict:
    pos = book.positions(conn)
    # 帳本有任何現金分錄就以帳本為準（含無持股：首批買單未成交、清倉後的現金；M7-D9-04）；完全沒有分錄才用初始資金
    if pos or conn.execute("SELECT 1 FROM cash_ledger LIMIT 1").fetchone() is not None:
        # 價格隔離：只放成本，不放市價（FULL_B 自己有價格檔；其他隔離工作看不到 portfolio_state）
        cash = book.cash(conn)
        basis = cash + book.receivables(conn) + sum(p["qty"] * p["avg_cost"] for p in pos.values())
        holdings = [{"ticker": t, "qty": p["qty"], "avg_cost": p["avg_cost"],
                     "cost_value": round(p["qty"] * p["avg_cost"], 2),
                     "cost_weight": round(p["qty"] * p["avg_cost"] / basis, 4) if basis > 0 else None,
                     "first_buy_date": p["first_buy_date"]} for t, p in pos.items()]
        return {"cash": cash, "holdings": holdings, "n_holdings": len(holdings),
                "committed": book.committed_buy_value(conn),
                "note": "持股取自帳本；cost_weight 為成本基礎權重（成本 ÷ 現金＋應收股利＋持股成本），不是市價權重；"
                        "committed 為未成交買單以委託價計的金額"}
    return {"cash": cfg.account.initial_cash, "holdings": [], "n_holdings": 0, "committed": 0,
            "note": "紙上帳本尚未建倉：全額現金、無持股、無已承諾部位"}


def _screen_dir(paths: Paths, screen_asof: str | None, asof: str) -> Path | None:
    d = paths.state / "screens" / (screen_asof or asof)
    return d if d.is_dir() else None


def _screen_price_distress(sdir: Path | None) -> dict[str, str]:
    """短名單／特殊交棒每檔的價格類困境碼（price_context.price_distress_codes）；只給價格檔（FULL_B 看價後做步 4A）。"""
    if sdir is None or not (sdir / "screen_handoff.json").is_file():
        return {}
    h = json.loads((sdir / "screen_handoff.json").read_text(encoding="utf-8"))
    return {normalize_ticker(r["ticker"]): str((r.get("price_context") or {}).get("price_distress_codes") or "")
            for lane in ("core_shortlist", "special_handoff") for r in h.get(lane) or []}


def _screen_contexts(sdir: Path | None) -> dict[str, dict]:
    if sdir is None or not (sdir / "handoff_noprice.json").is_file():
        return {}
    h = json.loads((sdir / "handoff_noprice.json").read_text(encoding="utf-8"))
    out = {}
    for lane in ("core_shortlist", "special_handoff"):
        for r in h.get(lane) or []:
            out[normalize_ticker(r["ticker"])] = r
    return out


# 利率檔傳給 DOSSIER 的欄位：30 年期（價格檔、FULL_B）與資料包利率段（3 個月國庫券、5／10 年平均；D29）＋錯誤與停更註記
RATE_KEYS = ("t30", "t30_date", "t30_20y_median", "t30_mean_5y", "t30_mean_10y", "t3m", "t3m_date", "t3m_mean_5y",
             "t3m_mean_10y", "error", "stale", "warning", "t30_extra_error", "t3m_extra_error",
             "t30_20y_median_error", "fred_warnings")   # A3-M4B-01：中位數錯誤與 FRED 警示進資料包利率段


def base_params(px: dict, rates: dict | None, screen_context: dict, portfolio_state: dict,
                largecap_pe: Any) -> dict[str, Any]:
    """DOSSIER 基本參數（analyze 與 review 共用）。px：{price, price_date}；rates：利率檔原始內容或 None。"""
    price = px.get("price")
    src = px.get("price_src")
    # 拆股字串（prices.csv splits 欄，'YYYY-MM-DD:比例;…'）給內部人表換算股數（M4-D4-02）：
    # 列上沒有 splits 欄（無價格列、review 的收盤 dict）→ None＝未取得；read_csv 的 NaN → ""＝確定無拆股
    sp = px.get("splits")
    splits = None if "splits" not in px else (sp if isinstance(sp, str) else "")
    return {
        "price": None if price is None or pd.isna(price) else float(price),
        "price_date": px.get("price_date") if isinstance(px.get("price_date"), str) else None,
        # 價格來源（alpaca／yahoo 或「失敗：…」）只進價格檔（M4-D2-09）
        "price_meta": {"source": src if isinstance(src, str) and src else None},
        "rates": {k: rates.get(k) for k in RATE_KEYS if k in rates} if rates else None,
        "screen_context": screen_context,
        "portfolio_state": portfolio_state,
        "largecap_pe": largecap_pe,
        "splits": splits,
    }


def _backfill_rates(rates: dict, fred, asof: str) -> list[str]:
    """修前舊格式 rates.json（某組 D29 鍵不全且無 {組}_extra_error）→ 以 FRED 補抓缺鍵，t30 三鍵沿用原檔（M4-R5-02）。
    補不到（無 FRED 客戶端或抓取失敗）→ 該組記 {組}_extra_error，資料包利率段明寫缺。原地修改，回傳補上的鍵。"""
    stale = {g: [k for k in ks if k not in rates] for g, ks in D29_RATE_KEYS.items()
             if f"{g}_extra_error" not in rates and any(k not in rates for k in ks)}
    if not stale:
        return []
    new = fetch_rates(fred, asof) if fred is not None else {}
    got = []
    for g, miss in stale.items():
        if all(k in new for k in miss):
            rates.update({k: new[k] for k in miss})
            got += miss
        else:
            rates[f"{g}_extra_error"] = new.get(f"{g}_extra_error") or \
                f"初選 rates.json 為舊格式，缺 {'、'.join(miss)}，且無 FRED 客戶端可補抓"
    return got


def _price_rows(csv: Path) -> dict[str, dict]:
    if not csv.is_file():
        return {}
    df = pd.read_csv(csv)
    return {normalize_ticker(r["ticker"]): r for r in df.to_dict("records")}


def run_analyze(cfg: Config, paths: Paths, conn, asof: str, tickers: list[str] | None = None,
                screen_asof: str | None = None, run_id: str | None = None,
                clients: dict[str, Any] | None = None, router: Router | None = None) -> dict[str, Any]:
    sdir = _screen_dir(paths, screen_asof, asof)
    if tickers is None:
        if sdir is None or not (sdir / "summary.json").is_file():
            raise AnalyzeError(f"沒有初選結果（state/screens/{screen_asof or asof}/）；請先 trader screen 或指定 --tickers")
        summ = json.loads((sdir / "summary.json").read_text(encoding="utf-8"))
        run = conn.execute("SELECT status FROM runs WHERE run_id=?", (f"screen-{sdir.name}",)).fetchone()
        if "error" in summ or (run is not None and run["status"] != "done"):
            # 失敗或未跑完的初選不得當成「0 檔要分析」（M1-D9-05）
            raise AnalyzeError(f"初選 {sdir.name} 未成功完成（{summ.get('error') or run['status']}）；"
                               "請先修好並重跑 trader screen，或指定 --tickers")
        tickers = [x["ticker"] for x in summ.get("to_analyze", [])]
    if screen_asof and screen_asof > asof:  # 初選日晚於決策日 → 會用到決策日之後的價格（M7-D6-05）
        raise AnalyzeError(f"初選日 {screen_asof} 晚於決策日 {asof}：不可用未來的初選價格")
    tickers = [normalize_ticker(t) for t in tickers if str(t).strip()]
    if run_id is None and sdir is not None and sdir.name == asof and (sdir / "summary.json").is_file():
        # 同日初選記下的分析 run（--force 重跑後為 analyze-{asof}-r…；A3-M1E-03）
        run_id = json.loads((sdir / "summary.json").read_text(encoding="utf-8")).get("analyze_run_id")
    run_id = run_id or f"analyze-{asof}"
    cl = clients if clients is not None else make_clients(cfg, paths)
    if cl.get("sec") is None:
        raise AnalyzeError("深度分析需要 SEC（.env 的 SEC_USER_AGENT 須含 email）")

    contexts = _screen_contexts(sdir)
    price_distress = _screen_price_distress(sdir)
    # 價格與利率只用決策日當天的初選輸入；初選日較早時一律以 asof 重抓，不把舊價當現價（M7-D6-05）
    same_day = sdir is not None and sdir.name == asof
    prices = _price_rows(sdir / "inputs" / "prices.csv") if same_day else {}
    rates_file = (sdir / "inputs" / "rates.json") if same_day else None
    rates = json.loads(rates_file.read_text(encoding="utf-8")) if rates_file and rates_file.is_file() else None
    others = list(book.positions(conn)) + watchlist_tickers(conn)   # G8 比較檔要持股與觀察名單的決策日收盤
    need = [t for t in dict.fromkeys(tickers + others) if t not in prices]
    if need or rates is None or "error" in (rates or {}):
        extra = paths.work / run_id / "_inputs"
        fetch_inputs(cfg, paths, conn, asof, extra, tickers=need or tickers[:1], clients=cl)
        prices.update(_price_rows(extra / "prices.csv"))
        if rates is None or "error" in rates:
            rates = json.loads((extra / "rates.json").read_text(encoding="utf-8"))
    rates_backfilled = _backfill_rates(rates, cl.get("fred"), asof)

    router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
    engine = Engine(conn, router, paths, cfg)
    engine.ctx.sec = cl["sec"]
    engine.ctx.prices = cl.get("prices", cl.get("alpaca"))   # M4-R3-02：P-30 日線用，不每工作重建客戶端
    engine.start_run(run_id, "analyze", asof)
    pstate = _portfolio_state(cfg, conn)
    write_view(conn, paths, run_id, asof, prices, rates, cl.get("fred"))
    pe = get_frozen(paths, asof)
    added, not_rerun = [], []
    for t in tickers:
        ctx = contexts.get(t) or screen_context_for(paths, asof, t, "非本次初選交接（手動指定）")
        params = base_params(prices.get(t) or {}, rates, ctx, pstate, pe)
        if t in price_distress:   # 價格類困境碼只進價格檔（price_meta 由 DOSSIER 寫入 {T}_price.json；D71）
            params["price_meta"]["screen_price_context"] = {"price_distress_codes": price_distress[t]}
        elif spc := screen_price_context_for(paths, ctx):   # 非短名單代號的 T3／T5（M1-R6-02）
            params["price_meta"]["screen_price_context"] = spc
        spec = JobSpec(job_id=f"{run_id}:{t}:DOSSIER:1", run_id=run_id, job_type="DOSSIER", ticker=t, asof=asof,
                       params=params)
        if engine.add_job(spec):
            added.append(t)
        elif (r := conn.execute("SELECT status FROM jobs WHERE job_id=?", (spec.job_id,)).fetchone()) \
                and r["status"] in ("done", "conservative_fallback"):
            not_rerun.append(t)   # 同 run 已分析過，不會重做（M1-RS8-01；重做需新的 run_id）
    result = engine.run(run_id)

    decisions = {}
    for t in tickers:
        f = paths.work / run_id / t / "decision.json"
        decisions[t] = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
    return {"run_id": run_id, "asof": asof, "tickers": tickers, "added": added, "not_rerun": not_rerun,
            "rates_backfilled": rates_backfilled, "engine": result, "decisions": decisions}
