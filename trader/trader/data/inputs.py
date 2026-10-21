"""把各資料來源組成 skill 腳本需要的輸入檔（features.csv、prices.csv）與利率（rates.json）。

金鑰與 SEC User-Agent 只從環境變數讀（設定檔存變數名稱）。缺金鑰的來源略過並在摘要中註明。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .. import db
from ..config import Config
from ..paths import Paths
from .alpaca import AlpacaClient
from .fred import FredClient
from .http import atomic_write_text
from .pe_median import compute_pe_median, freeze_monthly
from .prices_adapter import fetch_price_inputs
from .sec import SecClient
from .xbrl_shares import fill_missing_shares

SHARE_CLASS_RATIOS = "share_class_ratios.csv"  # state/：使用者補充的類別比例（覆蓋套件內同代號）
CIK_PREDECESSORS = "cik_predecessors.json"   # state/：使用者補充的前身 CIK 對照（覆蓋套件內同鍵）
from .universe import _parse_override, load_universe, normalize_ticker, universe_sha256

UNIVERSE_FILE = "universe.txt"  # outdir/：本次用的股票池；重跑同一輸出目錄時以它為準（M1-D9-04）
OWN_OUTPUTS = ("features.csv", "prices.csv", "rates.json")
RETIRED_OUTPUTS = ("daily.csv", "corporate_actions.json")   # 舊版產出、無讀者（M4-R3-01）；重跑同目錄時清掉殘檔
RATE_MEAN_YEARS = (5, 10)   # P-58 浮存金成本對照用的多年平均（M4-D5-07；年數推論）
# D29 利率鍵（fetch_rates 產出）：每組要嘛鍵齊，要嘛有 {組}_extra_error；兩者皆無＝修前舊格式（M4-R5-02）
D29_RATE_KEYS = {"t30": tuple(f"t30_mean_{n}y" for n in RATE_MEAN_YEARS),
                 "t3m": ("t3m", "t3m_date") + tuple(f"t3m_mean_{n}y" for n in RATE_MEAN_YEARS)}
STALE_RATE_DAYS = 7  # t30_date 早於 asof 超過此曆日數 → rates 標 stale（M4-D7-5）
MAX_SEC_STALE_RATIO = 0.10  # companyfacts 改用舊快取（SEC 失敗／封鎖）的代號比例高於此 → 不凍結 P/E、初選失敗（A3-M1E-01）
MIN_COVERAGE = 0.9  # 有價格且有財報（years_of_data，無此欄則 net_income 非缺）的代號比例低於此 → 不凍結 P/E、初選失敗（M1-D7-2）


def coverage(feats: pd.DataFrame | None, prices: pd.DataFrame | None, n_tickers: int) -> float:
    """有價格（price>0）且有財報（years_of_data 非缺）的代號數 ÷ n_tickers；任一表缺或 n_tickers=0 → 0。
    沒有 years_of_data 欄 → 改以 net_income 非缺判斷；兩欄都沒有（例如 SEC 全部失敗，只剩 ticker/cik/data_notes）→ 視為都沒有財報。"""
    if feats is None or prices is None or not n_tickers or "price" not in prices or "ticker" not in feats:
        return 0.0
    col = next((c for c in ("years_of_data", "net_income") if c in feats), None)
    if col is None:
        return 0.0
    ok = feats[col].notna()
    has_fin = set(feats.loc[ok, "ticker"].astype(str))
    has_px = set(prices.loc[pd.to_numeric(prices["price"], errors="coerce") > 0, "ticker"].astype(str))
    return len(has_fin & has_px) / n_tickers


def _read_universe_file(path: Path) -> tuple[list[str], str] | None:
    """讀 outdir/universe.txt：第一行 `# source=…`，其餘每行一檔；不存在或無代號回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    tickers = _parse_override(text)
    if not tickers:
        return None
    first = text.splitlines()[0] if text else ""
    src = first.split("source=", 1)[1].strip() if first.startswith("# source=") else "unknown"
    return tickers, src


def make_clients(cfg: Config, paths: Paths) -> dict[str, Any]:
    """依環境變數（金鑰）與設定建立可用的客戶端；缺金鑰者為 None。
    "alpaca"＝交易用（撮合、限價、ADV、日曆），一律 Alpaca；"prices"＝研究用（初選、深度分析取價），
    依 data.research_price_source 選 Alpaca 或 Yahoo（D34）。"""
    key, secret = cfg.secret(cfg.data.alpaca_key_env), cfg.secret(cfg.data.alpaca_secret_env)
    ua = cfg.secret(cfg.data.sec_user_agent_env)
    alpaca = AlpacaClient(key, secret, feed=cfg.data.alpaca_feed) if key and secret else None
    if cfg.data.research_price_source == "yahoo":
        from .yahoo import YahooClient
        prices = YahooClient(paths.cache / "yahoo")
    else:
        prices = alpaca
    return {
        "alpaca": alpaca,
        "prices": prices,
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


def fetch_rates(fred, asof: str) -> dict[str, Any]:
    """rates.json 的內容：30 年期（t30 三鍵）＋D29 的 3 個月國庫券與 5／10 年平均；失敗記 error／{組}_extra_error。"""
    try:
        t30, t30_date = fred.t30(asof)
        rates = {"t30": t30, "t30_date": t30_date, "t30_20y_median": None}
        try:   # A3-M4B-01：中位數失敗不得連 t30 一起丟（只影響 P4′）
            rates["t30_20y_median"] = fred.t30_median_20y(asof)
        except Exception as e:  # noqa: BLE001
            rates["t30_20y_median_error"] = f"30 年期 20 年中位：{type(e).__name__}: {e}"
        lag = (pd.Timestamp(asof[:10]) - pd.Timestamp(t30_date)).days
        if lag > STALE_RATE_DAYS:   # M4-D7-5：FRED 失敗沿用舊快取或序列停更時看得見
            rates.update({"stale": True, "warning": f"DGS30 最後日期 {t30_date} 早於 asof {lag} 天"})
    except Exception as e:  # noqa: BLE001 - 利率缺 → 記錄，由後續流程判 MISSING_DATA
        rates = {"error": f"{type(e).__name__}: {e}"}
    # D29：資料包利率段（給 FULL_A；非個股價格）另需 3 個月國庫券與 5／10 年平均；單項失敗只記該項 error
    for sid, key in (("DGS30", "t30"), ("DGS3MO", "t3m")):
        try:
            if key == "t3m":
                v, d = fred.latest("DGS3MO", asof)
                rates.update({"t3m": v / 100, "t3m_date": d})
            for n in RATE_MEAN_YEARS:
                rates[f"{key}_mean_{n}y"] = fred.mean(sid, asof, n) / 100
        except Exception as e:  # noqa: BLE001
            rates[f"{key}_extra_error"] = f"{type(e).__name__}: {e}"
    if getattr(fred, "warnings", None):
        rates["fred_warnings"] = list(fred.warnings)
    return rates


def fetch_inputs(cfg: Config, paths: Paths, conn, asof: str, outdir: Path,
                 tickers: list[str] | None = None, freeze_pe: bool = False,
                 clients: dict[str, Any] | None = None, refreeze: bool = False) -> dict[str, Any]:
    """產生 outdir/{features.csv, prices.csv, rates.json}，回傳摘要。

    tickers=None → 用股票池（S&P 500），名單寫入 outdir/universe.txt；該檔已存在（重跑）就沿用它（M1-D9-04）。
    凍結 P/E 中位另需：非 override 名單（M1-D7-5）、資料覆蓋率 ≥ MIN_COVERAGE（M1-D7-2）。freeze_pe=True 時以本次全體資料凍結當月大型股 P/E 中位（已凍結則沿用）；
    refreeze=True（隱含 freeze_pe）→ 重算並取代當月凍結值，舊檔改名保留（M1-D4-04）。
    features.csv／prices.csv 各只寫一次且以暫存檔替換，中斷不留半成品（M1-D9-02）。
    """
    freeze_pe = freeze_pe or refreeze
    outdir.mkdir(parents=True, exist_ok=True)
    for name in OWN_OUTPUTS + RETIRED_OUTPUTS:   # 某來源本次略過時不得沿用上次的檔（新 features＋舊 prices；M4-D9-05）。universe.txt 例外（M1-D9-04）
        (outdir / name).unlink(missing_ok=True)
    cl = clients if clients is not None else make_clients(cfg, paths)
    summary: dict[str, Any] = {"asof": asof, "outdir": str(outdir), "skipped": []}
    whole_universe = tickers is None
    if whole_universe:
        pinned = _read_universe_file(outdir / UNIVERSE_FILE)
        if pinned is not None:  # 重跑：沿用上次記下的名單，不重新載入（M1-D9-04）
            tickers, src = pinned[0], f"pinned:{pinned[1]}"
        else:
            tickers, src = load_universe(paths, conn, asof=asof)
            atomic_write_text(outdir / UNIVERSE_FILE, f"# source={src}\n" + "\n".join(tickers) + "\n")
        summary["universe_source"] = src
        summary["universe_sha256"] = universe_sha256([normalize_ticker(t) for t in tickers])
    tickers = sorted({normalize_ticker(t) for t in tickers if t.strip()})
    summary["n_tickers"] = len(tickers)

    feats = prices = None
    if cl.get("sec") is not None:
        n_stale0 = len(getattr(cl["sec"], "stale", None) or [])   # 只算本次 features 的舊快取（呼叫端可能共用客戶端）
        feats = cl["sec"].features(tickers, asof)
        summary["features"] = len(feats)
        if "years_of_data" in feats:
            summary["features_no_data"] = int(feats["years_of_data"].isna().sum())
        if cl["sec"].stale:
            summary["sec_stale_cache"] = sorted(set(cl["sec"].stale))
        # A3-M1E-01：SEC 失敗／封鎖時 _cached_json 退回舊快取不拋例外，覆蓋率照算仍高；以 companyfacts 舊快取比例把關
        facts_stale = {n for n in cl["sec"].stale[n_stale0:] if re.fullmatch(r"CIK\d{10}\.json", n)}
        summary["sec_stale_ratio"] = round(len(facts_stale) / len(tickers), 4) if tickers else 0.0
        if getattr(getattr(cl["sec"], "http", None), "blocked", None):
            summary["sec_blocked"] = True
    else:
        summary["skipped"].append(f"SEC（未設定含 @ 的 {cfg.data.sec_user_agent_env}）")

    pc = cl.get("prices", cl.get("alpaca"))   # 研究用價格來源（D34）；舊呼叫端只給 "alpaca" 時沿用它
    if pc is not None:
        px = fetch_price_inputs(pc, tickers, asof)
        prices = px["prices"]
        feed = getattr(pc, "feed", None)   # 價格檔來源，與 prices.csv 的 price_src 同口徑（alpaca／yahoo）
        summary["price_source"] = "yahoo" if feed == "yahoo" else (f"alpaca({feed})" if feed else "alpaca")
        summary["price_notes"] = px.get("notes") or []
        hum = sorted({f"{a['ticker']} {a['ex_date']}：{a['raw']['needs_human']}"
                      for a in px["actions"] if (a.get("raw") or {}).get("needs_human")})
        if hum:
            summary["needs_human"] = hum
        atomic_write_text(outdir / "prices.csv", prices.to_csv(index=False))
        summary["prices_ok"] = int(prices["price_src"].isin(["alpaca", "yahoo"]).sum())
    else:
        summary["skipped"].append(f"價格（research_price_source={cfg.data.research_price_source}；"
                                  f"Alpaca 需設定 {cfg.data.alpaca_key_env}／{cfg.data.alpaca_secret_env}）")

    if feats is not None and prices is not None:
        # 多類股公司：companyfacts 無分類別股數 → 由最新 10-K／10-Q 的 XBRL 依 EPS 比例換算（失敗則維持缺值）
        feats, rep = fill_missing_shares(feats, prices, cl["sec"], asof, paths.state / SHARE_CLASS_RATIOS)
        if rep:
            summary["shares_fill"] = rep
    if feats is not None:
        atomic_write_text(outdir / "features.csv", feats.to_csv(index=False))  # 補完多類股股數後才寫
        sus = detect_successions(cl["sec"], feats, asof, conn)
        if sus:
            summary["cik_succession_suspects"] = sus
    if cl.get("sec") is not None and cl["sec"].warnings:
        summary["sec_warnings"] = list(cl["sec"].warnings)

    rates = fetch_rates(cl["fred"], asof)
    (outdir / "rates.json").write_text(json.dumps(rates, ensure_ascii=False, indent=1), encoding="utf-8")
    summary["rates"] = rates

    if feats is not None and prices is not None:
        summary["coverage"] = round(coverage(feats, prices, summary["n_tickers"]), 4)
    if freeze_pe:
        if not whole_universe:
            summary["pe_median"] = "略過：P/E 中位只能用整個股票池凍結（不可用部分代號，否則整月錯值）"
        elif str(summary.get("universe_source")).endswith("override"):
            summary["pe_median"] = f"略過：universe_override 名單不凍結 P/E 中位（刪除 override，並刪除本輸出目錄的 {UNIVERSE_FILE}（初選為 inputs/{UNIVERSE_FILE}），否則重跑會沿用舊名單）"
        elif feats is None or prices is None:
            summary["pe_median"] = "略過：需要 SEC 與 Alpaca 兩者資料"
        elif summary.get("sec_stale_ratio", 0) > MAX_SEC_STALE_RATIO or summary.get("sec_blocked"):
            summary["pe_median"] = (f"略過：SEC 舊快取比例 {summary.get('sec_stale_ratio', 0):.1%}"
                                    f"{'（SEC 封鎖中）' if summary.get('sec_blocked') else ''}，不以舊資料凍結（A3-M1E-01）")
        elif summary["coverage"] < MIN_COVERAGE:
            summary["pe_median"] = (f"略過：資料覆蓋率 {summary['coverage']:.1%} < {MIN_COVERAGE:.0%}"
                                    "（SEC 或價格大量失敗），不以殘缺樣本凍結")
        else:
            def compute():
                from ..analysis.writeback import skill_hashes_file  # 延遲匯入：凍結時才需要
                return {**compute_pe_median(feats, prices, asof), "n_tickers": summary["n_tickers"],
                        "coverage": summary["coverage"], "universe_source": summary.get("universe_source"),
                        "skill_hashes": skill_hashes_file(paths)}
            summary["pe_median"] = freeze_monthly(paths, asof[:7], compute, refreeze=refreeze)
    return summary
