"""修復 X14（D73，2026-10-18）：價格檔回購均價加註「推算值，僅供參考」，並鎖住 ±3 年量級檢查對成對概念也生效。
手寫 companyfacts，不連網。"""
import importlib
import sys

import pytest

from trader.paths import get_paths

ASOF = "2026-10-15"


@pytest.fixture(scope="module")
def B():
    for sub in ("berkshire-screen-merged", "berkshire-deep-merged"):
        d = str(get_paths().root / sub / "scripts")
        if d not in sys.path:
            sys.path.insert(0, d)
    importlib.import_module("fetch_edgar")
    return importlib.import_module("build_dossier")


def _cal(y, val):
    return {"form": "10-K", "fp": "FY", "start": f"{y}-01-01", "end": f"{y}-12-31", "filed": f"{y + 1}-02-20", "val": val}


def _calp(y, val):
    return {"form": "10-K", "fp": "FY", "end": f"{y}-12-31", "filed": f"{y + 1}-02-20", "val": val}


def _bb_facts(sh, amt, shares=170e6, assets=13e9):
    yrs = sorted(sh)
    us = {"NetIncomeLoss": [_cal(y, 1e9) for y in yrs],
          "Assets": [_calp(y, assets) for y in yrs],
          "WeightedAverageNumberOfDilutedSharesOutstanding": [_cal(y, shares) for y in yrs],
          "StockRepurchasedAndRetiredDuringPeriodShares": [_cal(y, v) for y, v in sh.items()],
          "StockRepurchasedAndRetiredDuringPeriodValue": [_cal(y, v) for y, v in amt.items()]}
    units = {"WeightedAverageNumberOfDilutedSharesOutstanding": "shares",
             "StockRepurchasedAndRetiredDuringPeriodShares": "shares"}
    return {"cik": 1, "entityName": "TEST",
            "facts": {"us-gaap": {k: {"units": {units.get(k, "USD"): v}} for k, v in us.items()}}}


def test_price_file_has_avg_buyback_note(B):
    """(a) D73：價格檔多 avg_buyback_price_note，緊接 avg_buyback_price_by_fy；措辭含「推算值，僅供參考」與以內在價值為準。"""
    d = B.buyback_price_data(_bb_facts({2024: 5e6}, {2024: 1000e6}), ASOF)
    keys = list(d)
    assert keys.index("avg_buyback_price_note") == keys.index("avg_buyback_price_by_fy") + 1
    note = d["avg_buyback_price_note"]
    assert note == B.BUYBACK_AVG_NOTE
    assert "推算值，僅供參考" in note and "不得以回購均價推論估值" in note and "內在價值" in note
    assert d["avg_buyback_price_by_fy"] == {2024: 200.0}   # 既有數值不動


def test_paired_outlier_within_cap_guard_is_dropped(B):
    """(b) CI 2018 型：成對概念（非現金流量表）FY2024 均價 1,000、鄰年 200–300，隱含市值 1,700 億 <總資產 30 倍
    （絕對值防呆抓不到）→ 只能靠 ±3 年中位數檢查：FY2024 不給均價且警示「前後 3 年」。"""
    sh = {2021: 5e6, 2022: 4e6, 2023: 4e6, 2024: 1e6, 2025: 4e6}
    amt = {2021: 1000e6, 2022: 1000e6, 2023: 1100e6, 2024: 1000e6, 2025: 1200e6}   # 200／250／275／1,000／300
    assert 1000 * 170e6 < B.BUYBACK_MAX_CAP_ASSETS * 13e9
    d = B.buyback_price_data(_bb_facts(sh, amt), ASOF)
    avg, w = d["avg_buyback_price_by_fy"], d["avg_buyback_warnings"]
    assert 2024 not in avg
    assert any(x.startswith("FY2024：") and "前後 3 年" in x and "不給均價" in x for x in w)
    assert avg[2023] == pytest.approx(275) and avg[2025] == pytest.approx(300)
