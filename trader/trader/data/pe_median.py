"""全市場 PE 中位數（P4′ 用）：市值口徑與 skill 的 screen.build 完全一致；每月凍結一次。

凍結檔：`paths.cache/pe_median/{YYYY-MM}.json`，同月內不重算。
"""
from __future__ import annotations

import importlib
import json
import math
import sys
from pathlib import Path
from typing import Callable

import pandas as pd

from trader.paths import Paths, get_paths

MIN_SAMPLE = 30  # 樣本數低於此值，該中位數回 None


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
    fin_pe_norm_median 只取 is_financial == 1 的檔。
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
    fin_ok = norm_ok & fin

    n_norm = int(norm_ok.sum())
    n_ttm = int(ttm_ok.sum())
    n_fin = int(fin_ok.sum())
    return {
        "asof": asof,
        "n_norm": n_norm,
        "pe_norm_median": _median_or_none(pe_norm[norm_ok], n_norm),
        "n_ttm": n_ttm,
        "pe_ttm_median": _median_or_none(pe_ttm[ttm_ok], n_ttm),
        "n_fin": n_fin,
        "fin_pe_norm_median": _median_or_none(pe_norm[fin_ok], n_fin),
        "method": (
            "市值＝price×股數（與 screen.build 相同口徑，含拆股調整）；"
            "pe_norm＝市值/ni5_mean（ni5_mean>0）；pe_ttm＝市值/net_income（net_income>0）；"
            f"樣本數 < {MIN_SAMPLE} 之中位數回 None"
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


def freeze_monthly(paths: Paths, month: str, compute: Callable[[], dict]) -> dict:
    """檔案已存在 → 直接讀取（凍結，不呼叫 compute）；否則 compute() 後寫檔再回傳。"""
    path = freeze_path(paths, month)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    result = _clean(compute())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
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
        path = freeze_path(paths, m)
        if path.exists():
            v = json.loads(path.read_text(encoding="utf-8"))
            if str(v.get("asof") or "") <= asof:
                return v
    return None
