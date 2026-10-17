"""把各資料來源組成 skill 腳本需要的輸入檔（features.csv、prices.csv、daily.csv）與利率、公司行動。

金鑰與 SEC User-Agent 只從環境變數讀（設定檔存變數名稱）。缺金鑰的來源略過並在摘要中註明。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import db
from ..config import Config
from ..paths import Paths
from .alpaca import AlpacaClient
from .fred import FredClient
from .pe_median import compute_pe_median, freeze_monthly
from .prices_adapter import fetch_price_inputs
from .sec import SecClient
from .xbrl_shares import fill_missing_shares

SHARE_CLASS_RATIOS = "share_class_ratios.csv"  # state/：使用者補充的類別比例（覆蓋套件內同代號）
CIK_PREDECESSORS = "cik_predecessors.json"   # state/：使用者補充的前身 CIK 對照（覆蓋套件內同鍵）
from .universe import load_universe, normalize_ticker


def make_clients(cfg: Config, paths: Paths) -> dict[str, Any]:
    """依環境變數建立可用的客戶端；缺金鑰者為 None。"""
    key, secret = cfg.secret(cfg.data.alpaca_key_env), cfg.secret(cfg.data.alpaca_secret_env)
    ua = cfg.secret(cfg.data.sec_user_agent_env)
    return {
        "alpaca": AlpacaClient(key, secret, feed=cfg.data.alpaca_feed) if key and secret else None,
        "sec": SecClient(ua, paths.cache / "edgar", predecessors_path=paths.state / CIK_PREDECESSORS)
               if ua and "@" in ua else None,
        "fred": FredClient(cfg.secret(cfg.data.fred_key_env), paths.cache / "fred"),
    }


def detect_successions(sec, feats, asof: str, conn=None) -> list[dict]:
    """無年度財報、又有承繼註冊申報（8-K12B 等）且未登記前身的代號 → 疑似改組換 CIK。

    不自動猜前身（錯誤合併會污染財報）；寫入 anomalies（同代號未結案者不重複），由人確認後加入 cik_predecessors。
    """
    out: list[dict] = []
    if "fy_last" not in feats or "cik" not in feats:
        return out
    for _, r in feats[feats["fy_last"].isna() & feats["cik"].notna()].iterrows():
        cik = str(r["cik"]).split(".")[0].zfill(10)
        try:
            if sec.predecessors(cik):
                continue
            hits = sec.succession_filings(cik, asof)
        except Exception:  # noqa: BLE001 - 偵測失敗不影響資料產出
            continue
        if not hits:
            continue
        h = hits[-1]
        item = {"ticker": r["ticker"], "cik": cik, "form": h["form"], "filingDate": h["filingDate"], "url": h["url"]}
        out.append(item)
        if conn is not None and conn.execute(
                "SELECT 1 FROM anomalies WHERE ticker=? AND kind='cik_succession' AND status='open'",
                (r["ticker"],)).fetchone() is None:
            db.insert(conn, "anomalies", {
                "ticker": r["ticker"], "kind": "cik_succession", "pause_trading": 0, "created_at": db.now_iso(),
                "detail": json.dumps({**item, "action": "確認承繼關係後，把舊 CIK 加入 state/cik_predecessors.json"},
                                     ensure_ascii=False)})
    return out


def fetch_inputs(cfg: Config, paths: Paths, conn, asof: str, outdir: Path,
                 tickers: list[str] | None = None, freeze_pe: bool = False,
                 clients: dict[str, Any] | None = None) -> dict[str, Any]:
    """產生 outdir/{features.csv, prices.csv, daily.csv, corporate_actions.json, rates.json}，回傳摘要。

    tickers=None → 用股票池（S&P 500）。freeze_pe=True 時以本次全體資料凍結當月大型股 P/E 中位（已凍結則沿用）。
    """
    outdir.mkdir(parents=True, exist_ok=True)
    cl = clients if clients is not None else make_clients(cfg, paths)
    summary: dict[str, Any] = {"asof": asof, "outdir": str(outdir), "skipped": []}
    whole_universe = tickers is None
    if whole_universe:
        tickers, src = load_universe(paths, conn, asof=asof)
        summary["universe_source"] = src
    tickers = sorted({normalize_ticker(t) for t in tickers if t.strip()})
    summary["n_tickers"] = len(tickers)

    feats = prices = None
    if cl.get("sec") is not None:
        feats = cl["sec"].features(tickers, asof)
        feats.to_csv(outdir / "features.csv", index=False)
        summary["features"] = len(feats)
        if "years_of_data" in feats:
            summary["features_no_data"] = int(feats["years_of_data"].isna().sum())
        if cl["sec"].stale:
            summary["sec_stale_cache"] = sorted(set(cl["sec"].stale))
    else:
        summary["skipped"].append(f"SEC（未設定含 @ 的 {cfg.data.sec_user_agent_env}）")

    if cl.get("alpaca") is not None:
        px = fetch_price_inputs(cl["alpaca"], tickers, asof)
        prices = px["prices"]
        prices.to_csv(outdir / "prices.csv", index=False)
        px["daily"].to_csv(outdir / "daily.csv", index=False)
        (outdir / "corporate_actions.json").write_text(
            json.dumps([{k: v for k, v in a.items() if k != "raw"} for a in px["actions"]],
                       ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        summary["prices_ok"] = int((prices["price_src"] == "alpaca").sum())
    else:
        summary["skipped"].append(f"Alpaca（未設定 {cfg.data.alpaca_key_env}／{cfg.data.alpaca_secret_env}）")

    if feats is not None and prices is not None:
        # 多類股公司：companyfacts 無分類別股數 → 由最新 10-K／10-Q 的 XBRL 依 EPS 比例換算（失敗則維持缺值）
        feats, rep = fill_missing_shares(feats, prices, cl["sec"], asof, paths.state / SHARE_CLASS_RATIOS)
        if rep:
            feats.to_csv(outdir / "features.csv", index=False)
            summary["shares_fill"] = rep
    if feats is not None:
        sus = detect_successions(cl["sec"], feats, asof, conn)
        if sus:
            summary["cik_succession_suspects"] = sus
    if cl.get("sec") is not None and cl["sec"].warnings:
        summary["sec_warnings"] = list(cl["sec"].warnings)

    try:
        t30, t30_date = cl["fred"].t30(asof)
        rates = {"t30": t30, "t30_date": t30_date, "t30_20y_median": cl["fred"].t30_median_20y(asof)}
    except Exception as e:  # noqa: BLE001 - 利率缺 → 記錄，由後續流程判 MISSING_DATA
        rates = {"error": f"{type(e).__name__}: {e}"}
    (outdir / "rates.json").write_text(json.dumps(rates, ensure_ascii=False, indent=1), encoding="utf-8")
    summary["rates"] = rates

    if freeze_pe:
        if not whole_universe:
            summary["pe_median"] = "略過：P/E 中位只能用整個股票池凍結（不可用部分代號，否則整月錯值）"
        elif feats is None or prices is None:
            summary["pe_median"] = "略過：需要 SEC 與 Alpaca 兩者資料"
        else:
            summary["pe_median"] = freeze_monthly(paths, asof[:7], lambda: compute_pe_median(feats, prices, asof))
    return summary
