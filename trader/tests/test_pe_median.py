"""PE 中位數測試：合成 features／prices，不連網。"""
import pandas as pd
import pytest

from trader.data import pe_median as P
from trader.paths import get_paths


def _synth(n: int, neg: int = 0, fin: int = 0):
    """n 檔正常股＋neg 檔負盈餘＋前 fin 檔標記為金融。

    第 i 檔：市值 = 價格 × 1,000,000 股；pe_ttm = 10+i；pe_norm = 20+i。
    負盈餘檔：net_income 與 ni5_mean 皆為負。
    """
    rows, px = [], []
    for i in range(n + neg):
        price = 10.0 + i
        mc = price * 1_000_000
        if i < n:
            ttm, norm = 10.0 + i, 20.0 + i
        else:
            ttm, norm = -5.0, -5.0
        rows.append({
            "ticker": f"T{i:03d}",
            "shares_cover": 1_000_000.0, "shares_cover_filed": "2025-01-01",
            "shares": 1_000_000.0, "shares_filed_date": "2025-01-01",
            "net_income": mc / ttm, "ni5_mean": mc / norm, "fcf": mc / 15.0,
            "is_financial": 1 if i < fin else 0,
        })
        px.append({"ticker": f"T{i:03d}", "price": price, "price_date": "2026-10-15"})
    return pd.DataFrame(rows), pd.DataFrame(px)


ASOF = "2026-10-15"


def test_median_over_40_names():
    f, p = _synth(40)
    r = P.compute_pe_median(f, p, ASOF)
    assert r["n_norm"] == 40 and r["n_ttm"] == 40
    # pe_norm 為 20..59 → 中位數 39.5；pe_ttm 為 10..49 → 中位數 29.5
    assert r["pe_norm_median"] == pytest.approx(39.5)
    assert r["pe_ttm_median"] == pytest.approx(29.5)
    assert r["asof"] == ASOF and r["method"]


def test_negative_earnings_excluded():
    f, p = _synth(35, neg=5)
    r = P.compute_pe_median(f, p, ASOF)
    assert r["n_norm"] == 35
    assert r["n_ttm"] == 35
    # 負盈餘檔不得進入中位數：仍為 20..54 → 37.0
    assert r["pe_norm_median"] == pytest.approx(37.0)
    assert r["pe_ttm_median"] == pytest.approx(27.0)


def test_small_sample_returns_none():
    f, p = _synth(29)
    r = P.compute_pe_median(f, p, ASOF)
    assert r["n_norm"] == 29
    assert r["pe_norm_median"] is None
    assert r["pe_ttm_median"] is None


def test_financial_subset_threshold():
    f, p = _synth(40, fin=10)
    r = P.compute_pe_median(f, p, ASOF)
    assert r["n_fin"] == 10
    assert r["fin_pe_norm_median"] is None  # 金融樣本 < 30
    f2, p2 = _synth(40, fin=40)
    r2 = P.compute_pe_median(f2, p2, ASOF)
    assert r2["fin_pe_norm_median"] == pytest.approx(39.5)


@pytest.fixture
def paths(tmp_path):
    p = get_paths(tmp_path / "state", root=tmp_path)
    p.ensure()
    return p


def test_freeze_does_not_recompute(paths):
    calls = []

    def compute():
        calls.append(1)
        return {"asof": "2026-10-15", "pe_norm_median": 31.0}

    a = P.freeze_monthly(paths, "2026-10", compute)
    b = P.freeze_monthly(paths, "2026-10", compute)
    assert len(calls) == 1
    assert a == b == {"asof": "2026-10-15", "pe_norm_median": 31.0}
    assert P.freeze_path(paths, "2026-10").exists()


def test_freeze_writes_nan_as_null(paths):
    out = P.freeze_monthly(paths, "2026-03", lambda: {"pe_norm_median": float("nan")})
    assert out["pe_norm_median"] is None


def test_get_frozen_same_month_then_previous(paths):
    assert P.get_frozen(paths, "2026-10-15") is None
    P.freeze_monthly(paths, "2026-09", lambda: {"asof": "2026-09-01", "pe_norm_median": 25.0})
    got = P.get_frozen(paths, "2026-10-15")  # 10 月沒有 → 退回 9 月
    assert got["pe_norm_median"] == 25.0
    P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-01", "pe_norm_median": 30.0})
    assert P.get_frozen(paths, "2026-10-15")["pe_norm_median"] == 30.0


def test_get_frozen_skips_file_frozen_after_asof(paths):
    """10-16 才凍結的 10 月檔不可用於 10-15 的分析（前視）→ 退回 9 月。"""
    P.freeze_monthly(paths, "2026-09", lambda: {"asof": "2026-09-01", "pe_norm_median": 25.0})
    P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-16", "pe_norm_median": 30.0})
    assert P.get_frozen(paths, "2026-10-15")["pe_norm_median"] == 25.0
    assert P.get_frozen(paths, "2026-10-16")["pe_norm_median"] == 30.0


def test_get_frozen_january_falls_back_to_prior_december(paths):
    P.freeze_monthly(paths, "2025-12", lambda: {"pe_norm_median": 22.0})
    assert P.get_frozen(paths, "2026-01-10")["pe_norm_median"] == 22.0
    assert P.get_frozen(paths, "2026-02-01") is None  # 2 月與 1 月皆無 → None
