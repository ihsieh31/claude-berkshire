"""修復 RM2-5（複審 M2）：M2-RS1-01 孤立年度回購均價未比對警示、M2-RS1-02 回購級距取絕對值、
M2-D5-06 權益 ≤0 警示改指向 SEL-076b、M2-RS6-01 價格清洗補殖利率／轉換價／ESPP／授予日公允價／TSR 並提供
DOSSIER 回寫可用的 sanitize_text。手寫 companyfacts 與假文字，不連網。"""
import importlib
import sys

import pytest

from trader.analysis import dossier_prep as dp
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


def _facts(us, share_tags=()):
    units = {t: "shares" for t in ("WeightedAverageNumberOfDilutedSharesOutstanding", *share_tags)}
    return {"cik": 1, "entityName": "TEST",
            "facts": {"us-gaap": {k: {"units": {units.get(k, "USD"): v}} for k, v in us.items()}}}


def _bank(sh, amt, cf=False):
    """RF 型：銀行總資產 1,550 億、稀釋股數 9.5 億；成對概念（或現金流量表金額）。"""
    yrs = sorted(set(sh) | {2016, 2025})
    us = {"NetIncomeLoss": [_cal(y, 2e9) for y in yrs], "Assets": [_calp(y, 155e9) for y in yrs],
          "WeightedAverageNumberOfDilutedSharesOutstanding": [_cal(y, 950e6) for y in yrs],
          "StockRepurchasedAndRetiredDuringPeriodShares": [_cal(y, v) for y, v in sh.items()]}
    us["PaymentsForRepurchaseOfCommonStock" if cf else "StockRepurchasedAndRetiredDuringPeriodValue"] = \
        [_cal(y, v) for y, v in amt.items()]
    return _facts(us, ("StockRepurchasedAndRetiredDuringPeriodShares",))


# ---------------------------------------------------------------- M2-RS1-01

def test_isolated_paired_year_is_flagged_unverified(B):
    """RF 2022 型：成對概念、前後 3 年無他年 → 修前照給且無警示；修後照給但警示「未比對」、basis 標「未比對」。
    隱含市值 317×9.5 億≈3,000 億 <總資產 30 倍（上限抓不到，銀行）。"""
    d = B.buyback_price_data(_bank({2013: 9e6, 2017: 30e6, 2018: 40e6, 2022: 725e3},
                                   {2013: 85e6, 2017: 582e6, 2018: 939e6, 2022: 230e6}), ASOF)
    avg, w, basis = d["avg_buyback_price_by_fy"], d["avg_buyback_warnings"], d["avg_buyback_basis"]
    assert avg[2022] == pytest.approx(230e6 / 725e3, rel=1e-4)
    assert any(x.startswith("FY2022：") and "未比對" in x and "前後 3 年無他年均價" in x for x in w)
    assert basis[2022].endswith("（未比對）")
    assert basis[2013].endswith("（未比對）")                 # FY2013 也孤立
    assert "未比對" not in basis[2017] and "未比對" not in basis[2018]   # 有鄰年可比 → 不標
    assert not any(x.startswith(("FY2017：", "FY2018：")) for x in w)


def test_isolated_cash_flow_year_still_flagged(B):
    """現金流量表金額的孤立年度：仍有現金流量表警示，另有未比對警示（同一口徑）。"""
    d = B.buyback_price_data(_bank({2018: 1e6}, {2018: 270e6}, cf=True), ASOF)
    w, basis = d["avg_buyback_warnings"], d["avg_buyback_basis"]
    assert any(x.startswith("FY2018：均價用現金流量表") for x in w)
    assert any(x.startswith("FY2018：") and "未比對" in x for x in w)
    assert "（現金流量表金額）" in basis[2018] and basis[2018].endswith("（未比對）")


# ---------------------------------------------------------------- M2-RS1-02

def test_negative_buyback_amount_is_banded_by_absolute_value(B):
    """GE FY2016 型：現金流量表回購 −22.58B（負號表示流出）→ 修前「無回購」；修後依絕對值分級距。"""
    assert B._bucket_buyback(-22.58e9, 10e9) == ">100%"
    assert B._bucket_buyback(-2e9, 10e9) == "<25%"
    assert B._bucket_buyback(0.0, 10e9) == "無回購"
    yrs = range(2016, 2026)
    f = _facts({"NetIncomeLoss": [_cal(y, 8e9) for y in yrs],
                "DepreciationDepletionAndAmortization": [_cal(y, 2e9) for y in yrs],
                "PaymentsToAcquirePropertyPlantAndEquipment": [_cal(y, 2e9) for y in yrs],
                "PaymentsForRepurchaseOfCommonStock": [_cal(2016, -22.58e9)] + [_cal(y, 1e9) for y in yrs if y > 2016]})
    T, (R, filed, last, warn, bands) = B.build(f, "GE", ASOF)
    assert bands["回購÷業主盈餘近似 級距"][2016] == ">100%"


# ---------------------------------------------------------------- M2-D5-06（build_dossier 警示）

def test_negative_equity_warning_points_to_sel076b_only(B):
    """權益 ≤0 警示：只指向 SEL-076b 有形資本報酬（SEL-055）與 P-06 利息保障；不再叫模型看沒有門檻的
    「有息負債÷正常化稅前息前盈餘」並把出處標成 SEL-076b。"""
    eq = {y: (500 if y < 2022 else -100) for y in range(2016, 2026)}
    us = {"NetIncomeLoss": [_cal(y, 1e9) for y in eq], "StockholdersEquity": [_calp(y, v * 1e6) for y, v in eq.items()]}
    T, (R, filed, last, warn, bands) = B.build(_facts(us), "T", ASOF)
    w = next(x for x in warn if "股東權益 ≤0" in x)
    assert "SEL-076b" in w and "有形資本報酬" in w and "P-06" in w and "次一年" in w
    assert "正常化稅前息前" not in w


# ---------------------------------------------------------------- M2-RS6-01

# DOSSIER 回寫（模型寫的中文）：每句都不得把價格數字留下
DOSSIER_ZH_LEAKS = [
    ("授予日每股公允價值 187.42 美元", "187.42"),
    ("RSU 加權平均授予日公允價值為每股 187.42 美元。", "187.42"),
    ("可轉債轉換價每股 132.45 美元。", "132.45"),
    ("選擇權行權價 64.10 美元。", "64.10"),
    ("ESPP 員工以 71.20 美元購買股份。", "71.20"),
    ("預期股息殖利率 2.6%。", "2.6"),
    ("公司 TSR 182，同業 150。", "182"),
    ("授予 RSU 52,000 單位，授予日公允價值合計 970 萬美元。", "970"),
    ("Expected dividend yield 2.6%.", "2.6"),
    # RM2-5R：BBY 真實 DOSSIER 句（TSR 位於…第 N 百分位）、strike price、總股東報酬 樣本
    ("FY23 TSR 績效股因 TSR 位於 S&P 500 第 23 百分位而 0% 發放。", "23"),
    ("FY2023 PARSU 實際給付 80.2%（三年 TSR 約第 35 百分位，無修正）。", "35"),
    ("Options were granted with a strike price of $64.10.", "64.10"),
    ("公司三年總股東報酬為 45%。", "45"),
]

# SEC 原文：sanitize_filing_text 後不得留下
FILING_LEAKS = [
    ("Expected dividend yield 2.6%; risk-free rate 4.1%.", "2.6"),
    ("Our dividend yield was 3.1% at year end.", "3.1"),
    ("The dividend represents a yield of 2.1% on our year-end price.", "2.1"),
    ("Company TSR 182; Peer group TSR 150.", "182"),
    ("Relative TSR rank 2 of 9.", "rank 2"),
    ("Granted 52,000 RSUs with an aggregate grant date fair value of $9.7 million.", "9.7"),
    ("The notes have an initial conversion price of $132.45.", "132.45"),
    ("Employees purchased shares under the ESPP at $71.20.", "71.20"),
]

# 不得誤刪（資料包允許的財報與利率內容、薪酬設計）
KEEP = [
    "每股股利 1.84 美元，股利發放率 45%。",
    "每股盈餘（稀釋）5.20 美元。",
    "- 30 年期公債殖利率（DGS30）：4.75%（2026-10-14）；20 年中位 3.50%",
    "- 3 個月國庫券殖利率（DGS3MO）：4.10%（2026-10-14）",
    "| 稀釋加權股數（百萬股） | 1,234.5 | 1,200.0 |",
    "| EPS（稀釋，美元） | 5.20 | 5.61 |",
    "| 普通股股利 | 1,234 | 1,300 |",
    "收入依履約義務認列，交易價格分攤至各履約義務。",
    "ESPP 費用 1,200 萬美元，計入股權薪酬。",
    "Stock-based compensation expense, including ESPP expense of $12.50 million, was recorded.",
    "PSU 指標：相對 TSR 權重 50%、ROIC 權重 50%。",
    "Relative TSR 75th percentile or above earns the maximum payout.",
    "Dividends paid per share $ 2.00",
    # RM2-5R：TSR 權重、年期、年份（薪酬設計，procedure/08:25 允許）；DHI、FDX 真實 DOSSIER 寫法
    "相對 TSR 25%",
    "- 相對 TSR：50%",
    "Relative TSR 50%",
    "(TSR 50% weight)",
    "PSU 由 3 年 ROIC（50%）與相對 TSR 50% 組成。",
    "相對 S&P 500 之 TSR 30%、相對 S&P 500 成份股 ROA 25%。",
    "- **LTI 現金 FY24–FY26**：調整後累計 EPS 50%、ROIC 成長 25%、相對 TSR 25%；最高 200%。",
    "相對 TSR 3 年績效期。",
    "Relative TSR 3-year performance period.",
    "Relative TSR 36-month performance period.",   # RM2-14：英文月數年期
    "TSR 2024–2026 績效期間。",
    # 中文行權價條：無金額的重定價條款保留
    "選擇權行權價於 2020 年未曾重定價。",
]


def test_tsr_result_values_still_removed():
    """RM2-5R 收窄後，TSR 結果值（無 % 的數值、名次）仍刪；權重同句時也只刪結果值所在句。"""
    for src in ("Company TSR 182.", "TSR: 182", "Relative TSR rank 2 of 9.", "公司 TSR 182，同業 150。"):
        assert dp.scrub_price_text(src)[1] == 1, src


@pytest.mark.parametrize("src,num", DOSSIER_ZH_LEAKS)
def test_sanitize_text_removes_dossier_price_leaks(src, num):
    out, st = dp.sanitize_text(src)
    assert num not in out, out
    assert set(st) == {"buyback_masked", "per_share_masked", "removed_lines"}


@pytest.mark.parametrize("src,num", FILING_LEAKS)
def test_sanitize_filing_text_removes_new_patterns(src, num):
    out, _ = dp.sanitize_filing_text(src)
    assert num not in out, out


def test_filing_risk_free_clause_survives_dividend_yield_removal():
    out, _ = dp.sanitize_filing_text("Expected dividend yield 2.6%; risk-free rate 4.1%.")
    assert "2.6" not in out and "risk-free" in out   # 只刪殖利率子句（逐句）


@pytest.mark.parametrize("src", KEEP)
def test_sanitize_keeps_allowed_financial_and_design_text(src):
    assert dp.sanitize_text(src)[0] == src
    assert dp.scrub_price_text(src) == (src, 0)


def test_rates_section_passes_price_fuse():
    """資料包利率段（公債、國庫券殖利率）不得被新殖利率樣式當成價格（assemble 保險絲會擋）。"""
    sec = dp.rates_section({"t30": 0.0475, "t30_date": "2026-10-14", "t30_20y_median": 0.035,
                            "t3m": 0.041, "t3m_date": "2026-10-14"})
    assert dp.scrub_price_text(sec)[1] == 0


def test_sanitize_filing_text_is_drop_sections_plus_sanitize_text():
    raw = "Item 5. Market for Registrant's Common Equity\nHigh $90\nItem 6. Reserved\nRSUs: fair value $55.10 per share."
    out, st = dp.sanitize_filing_text(raw)
    assert set(st) == {"dropped_sections", "buyback_masked", "per_share_masked", "removed_lines"}
    assert st["dropped_sections"] == 1 and "High $90" not in out and "55.10" not in out
