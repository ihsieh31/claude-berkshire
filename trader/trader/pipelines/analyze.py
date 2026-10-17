"""深度分析 pipeline：對指定代號（或初選 to_analyze）跑 DOSSIER → FULL_A → FULL_B → RED_TEAM?，終點回寫 DB。

輸入來源：
- 初選結果 state/screens/{screen_asof}/：handoff_noprice.json（screen_context）、summary.json（to_analyze）、
  inputs/prices.csv 與 rates.json（價格與利率；只寫進價格檔，FULL_B 才開）。
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
from ..data.inputs import fetch_inputs, make_clients
from ..data.pe_median import get_frozen
from ..data.universe import normalize_ticker
from ..jobs.engine import Engine
from ..ledger import book
from ..llm.router import Router
from ..models import JobSpec
from ..paths import Paths
from ..portfolio.holdings_view import watchlist_tickers, write_view


class AnalyzeError(Exception):
    """無法開始深度分析（缺初選結果、缺 SEC 設定等）。"""


def _portfolio_state(cfg: Config, conn) -> dict:
    pos = book.positions(conn)
    if pos:
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


def _screen_contexts(sdir: Path | None) -> dict[str, dict]:
    if sdir is None or not (sdir / "handoff_noprice.json").is_file():
        return {}
    h = json.loads((sdir / "handoff_noprice.json").read_text(encoding="utf-8"))
    out = {}
    for lane in ("core_shortlist", "special_handoff"):
        for r in h.get(lane) or []:
            out[normalize_ticker(r["ticker"])] = r
    return out


def base_params(px: dict, rates: dict | None, screen_context: dict, portfolio_state: dict,
                largecap_pe: Any) -> dict[str, Any]:
    """DOSSIER 基本參數（analyze 與 review 共用）。px：{price, price_date}；rates：利率檔原始內容或 None。"""
    price = px.get("price")
    return {
        "price": None if price is None or pd.isna(price) else float(price),
        "price_date": px.get("price_date") if isinstance(px.get("price_date"), str) else None,
        "rates": {k: rates.get(k) for k in ("t30", "t30_date", "t30_20y_median")} if rates else None,
        "screen_context": screen_context,
        "portfolio_state": portfolio_state,
        "largecap_pe": largecap_pe,
    }


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
        tickers = [x["ticker"] for x in summ.get("to_analyze", [])]
    tickers = [normalize_ticker(t) for t in tickers if str(t).strip()]
    run_id = run_id or f"analyze-{asof}"
    cl = clients if clients is not None else make_clients(cfg, paths)
    if cl.get("sec") is None:
        raise AnalyzeError("深度分析需要 SEC（.env 的 SEC_USER_AGENT 須含 email）")

    contexts = _screen_contexts(sdir)
    prices = _price_rows(sdir / "inputs" / "prices.csv") if sdir else {}
    rates_file = (sdir / "inputs" / "rates.json") if sdir else None
    rates = json.loads(rates_file.read_text(encoding="utf-8")) if rates_file and rates_file.is_file() else None
    others = list(book.positions(conn)) + watchlist_tickers(conn)   # G8 比較檔要持股與觀察名單的決策日收盤
    need = [t for t in dict.fromkeys(tickers + others) if t not in prices]
    if need or rates is None or "error" in (rates or {}):
        extra = paths.work / run_id / "_inputs"
        fetch_inputs(cfg, paths, conn, asof, extra, tickers=need or tickers[:1], clients=cl)
        prices.update(_price_rows(extra / "prices.csv"))
        if rates is None or "error" in rates:
            rates = json.loads((extra / "rates.json").read_text(encoding="utf-8"))

    router = router or Router(cfg, paths.routing_json, state_dir=paths.state)
    engine = Engine(conn, router, paths, cfg)
    engine.ctx.sec = cl["sec"]
    engine.start_run(run_id, "analyze", asof)
    pstate = _portfolio_state(cfg, conn)
    write_view(conn, paths, run_id, asof, prices, rates, cl.get("fred"))
    pe = get_frozen(paths, asof)
    added = []
    for t in tickers:
        params = base_params(prices.get(t) or {}, rates,
                             contexts.get(t) or {"ticker": t, "note": "非本次初選交接（手動指定）"}, pstate, pe)
        spec = JobSpec(job_id=f"{run_id}:{t}:DOSSIER:1", run_id=run_id, job_type="DOSSIER", ticker=t, asof=asof,
                       params=params)
        if engine.add_job(spec):
            added.append(t)
    result = engine.run(run_id)

    decisions = {}
    for t in tickers:
        f = paths.work / run_id / t / "decision.json"
        decisions[t] = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
    return {"run_id": run_id, "asof": asof, "tickers": tickers, "added": added, "engine": result,
            "decisions": decisions}
