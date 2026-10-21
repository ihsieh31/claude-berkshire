"""全市場 PE 中位數（P4′ 用）：市值口徑與 skill 的 screen.build 完全一致；每月凍結一次。

凍結檔：`paths.cache/pe_median/{YYYY-MM}.json`，同月內不重算；以暫存檔＋替換寫入，無法解析的檔視同不存在。
上游規則或程式修正後以 `trader fetch --asof D --refreeze` 重凍，D 用原凍結日（舊檔的 asof；不得更晚，否則該月較早日期
查不到凍結值，見 get_frozen）：舊檔複製為 `{YYYY-MM}.json.bak-{時間}` 保留後原子替換。
"""
from __future__ import annotations

import importlib
import json
import math
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from trader.data.http import atomic_write_text
from trader.paths import Paths, get_paths

MIN_SAMPLE = 30  # 樣本數低於此值，該中位數回 None
REIT_SIC = 6798  # REIT 不列入金融 P/E 中位（決策 D20）


def _screen_module():
    """匯入 skill 的 screen.py（只匯入不修改）。"""
    d = str(get_paths().skill_screen / "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module("screen")


def _median_or_none(values: pd.Series, n: int):
    if n < MIN_SAMPLE:
        return None
    return float(values.median())


def compute_pe_median(features: pd.DataFrame, prices: pd.DataFrame, asof: str) -> dict:
    """計算 PE 中位數。

    pe_norm = market_cap / ni5_mean（ni5_mean > 0）；pe_ttm = market_cap / net_income（net_income > 0）。
    fin_pe_norm_median／fin_pe_ttm_median 只取 is_financial == 1 的檔，但 REIT（SIC 6798）不算金融（決策 D20）；
    P4′ 用 pe_ttm_median 與 fin_pe_ttm_median（最近年報口徑，D60、rulebook P/E 以最近年報稀釋 EPS 計）。
    母體＝傳入的 features（fetch_inputs 只以整個 S&P 500 股票池凍結，決策 D70）。
    """
    screen = _screen_module()
    df = screen.build(features, prices, None, asof)
    mc = pd.to_numeric(df["market_cap"], errors="coerce")
    mc_ok = mc.notna() & (mc > 0)

    ni5 = pd.to_numeric(df["ni5_mean"], errors="coerce")
    pe_norm = (mc / ni5).where(mc_ok & (ni5 > 0))
    norm_ok = pe_norm.notna()

    ni = pd.to_numeric(df["net_income"], errors="coerce")
    pe_ttm = (mc / ni).where(mc_ok & (ni > 0))
    ttm_ok = pe_ttm.notna()

    if "is_financial" in df:
        fin = df["is_financial"].fillna(False).astype(str).str.lower().isin(["1", "true", "1.0"])
    else:
        fin = pd.Series(False, index=df.index)
    if "sic" in df:  # REIT 歸非金融（D20）
        fin &= pd.to_numeric(df["sic"], errors="coerce") != REIT_SIC
    fin_ok = norm_ok & fin
    fin_ttm_ok = ttm_ok & fin

    n_norm = int(norm_ok.sum())
    n_ttm = int(ttm_ok.sum())
    n_fin = int(fin_ok.sum())
    n_fin_ttm = int(fin_ttm_ok.sum())
    return {
        "asof": asof,
        "n_norm": n_norm,
        "pe_norm_median": _median_or_none(pe_norm[norm_ok], n_norm),
        "n_ttm": n_ttm,
        "pe_ttm_median": _median_or_none(pe_ttm[ttm_ok], n_ttm),
        "n_fin": n_fin,
        "fin_pe_norm_median": _median_or_none(pe_norm[fin_ok], n_fin),
        "n_fin_ttm": n_fin_ttm,
        "fin_pe_ttm_median": _median_or_none(pe_ttm[fin_ttm_ok], n_fin_ttm),
        "method": (
            "母體＝S&P 500 股票池（整池凍結；override 或部分代號不凍結）；"
            "市值＝price×股數（與 screen.build 相同口徑，含拆股調整）；"
            "pe_norm＝市值/ni5_mean（ni5_mean>0）；pe_ttm＝市值/net_income（net_income>0，最近年報）；"
            f"金融＝is_financial 且 SIC≠{REIT_SIC}（REIT 歸非金融）；"
            f"樣本數 < {MIN_SAMPLE} 之中位數回 None；"
            "多類股（GOOG／GOOGL 等同一公司多列）未去重，含重複列（影響約 0.1%，A3-M1C-04）"
        ),
    }


def freeze_path(paths: Paths, month: str) -> Path:
    """凍結檔路徑；month 為 YYYY-MM。"""
    return paths.cache / "pe_median" / f"{month}.json"


def _clean(obj):
    """把 NaN 換成 None，確保 JSON 合法。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


def _read_frozen(path: Path) -> dict | None:
    """讀凍結檔；不存在或無法解析（例如被殺留下的半截檔）回 None。"""
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError 涵蓋 JSONDecodeError、UnicodeDecodeError
        return None
    return v if isinstance(v, dict) else None


def freeze_monthly(paths: Paths, month: str, compute: Callable[[], dict], refreeze: bool = False) -> dict:
    """檔案已存在且可解析 → 直接讀取（凍結，不呼叫 compute）；否則 compute() 後寫檔再回傳。
    refreeze=True：一律重算；算成功後才把舊檔複製為 {month}.json.bak-{時間}，再原子替換原檔（算或寫失敗舊檔原封不動）。
    重算的 asof 晚於舊檔 asof → ValueError、不寫（M1-R4-01：get_frozen 只收 asof ≤ 查詢日，較晚的 asof 會讓該月較早日期失去凍結值）。"""
    path = freeze_path(paths, month)
    frozen = _read_frozen(path)
    if frozen is not None and not refreeze:
        return frozen
    result = _clean(compute())
    if refreeze and frozen is not None and str(result.get("asof") or "") > str(frozen.get("asof") or ""):
        raise ValueError(f"重凍 asof {result.get('asof')} 晚於原凍結日 {frozen.get('asof')}：請用 --asof {frozen.get('asof')} 重凍")
    path.parent.mkdir(parents=True, exist_ok=True)
    if refreeze and path.exists():  # 複製備份、舊檔留在原處，再原子替換：寫入失敗也不會整月無凍結檔
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%dT%H%M%S')}"))
    atomic_write_text(path, json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    if m == 1:
        return f"{y - 1:04d}-12"
    return f"{y:04d}-{m - 1:02d}"


def get_frozen(paths: Paths, asof: str) -> dict | None:
    """asof 所在月份的凍結值；沒有則退回上一個月；都沒有回 None（P4′ 不適用）。
    凍結檔內的 asof 晚於所求日期（月中才凍結、補跑月初的日子）→ 不用，避免前視。"""
    month = asof[:7]
    for m in (month, _prev_month(month)):
        v = _read_frozen(freeze_path(paths, m))
        if v is not None and str(v.get("asof") or "") <= asof:
            return v
    return None
