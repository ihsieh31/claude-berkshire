"""P12：價格隔離清洗單一入口（M4-D1-15、M4-D6-1、M4-D6-3、M4-D6-4、M4-D6-5、D38）。"""
from __future__ import annotations

import pytest

from trader.analysis import dossier_prep as dp
from trader.analysis import peers


def _clean(text: str) -> str:
    return dp.sanitize_filing_text(text)[0]


# ---------- D38：每股金額遮蔽（保留句子），EPS／股利／面額等不遮

@pytest.mark.parametrize("src,num,keep", [
    ("We repurchased shares in 2025 (an average of $200.00 per share), funded from cash.", "200.00", "funded from cash"),
    ("Shares were bought at a cost of $150.25 per share.", "150.25", "at a cost of"),
    ("The Company bought shares at prices averaging $88.10 per share.", "88.10", "at prices averaging"),
    ("The weighted average fair value of RSUs granted was $63.21.", "63.21", "fair value of RSUs granted"),
    ("Under the ATM program we sold shares at a weighted average sales price of $57.14.", "57.14", "ATM program"),
    ("We completed an underwritten offering at $35.50 per share.", "35.50", "underwritten offering"),
    ("Each share was converted into the right to receive $54.00 per share in cash.", "54.00", "right to receive"),
    ("PHM repurchased shares at an average price of $119.42 per share.", "119.42", ""),
    ("Weighted average grant date fair value per performance share that were granted$93.10 $97.82 $110.21",
     "97.82", "grant date fair value"),
])
def test_per_share_prices_masked_sentence_kept(src, num, keep):
    out = _clean(src)
    assert num not in out
    if keep:
        assert keep in out
        assert dp.PER_SHARE_MASK in out or dp.BUYBACK_AMOUNT_MASK in out


@pytest.mark.parametrize("src", [
    "Net income was $1.2 billion, or $3.10 per share, compared with $2.90 per share last year.",
    "Diluted earnings per share were $5.20.",
    "Earnings per share, diluted $ 5.20",
    "Dividends paid per share $ 2.00",
    "The Board declared a quarterly cash dividend of $0.26 per share.",
    "We declared $1.10 per share in distributions.",
    "Common stock, par value $0.01 per share; 500 million shares authorized.",
    "Book value per share was $45.12.",
    "FFO was $2.10 per diluted share.",
    "Restructuring charges reduced results by $0.12 per share.",
    "The average price of gasoline was $3.50 per gallon.",
    "Average selling price per home was $512,000.",
    "Pre-tax share-based compensation costs of $53.8 million during 2025 were recognized.",
    "每股盈餘 5.2 美元。",
    "每股股利 2 元。",
])
def test_eps_dividend_par_not_masked(src):
    out, st = dp.sanitize_filing_text(src)
    assert out == src and st["per_share_masked"] == 0


def test_eps_row_uses_previous_line_label():
    """表格列標在上一行（GL：「Insurance underwriting income …」下一行「Per share $ 4.42 $ 3.98」）→ 不遮。"""
    src = "Insurance underwriting income $ 352,405 $ 336,315 5\n\nPer share $ 4.42 $ 3.98 11"
    assert _clean(src) == src


# ---------- M4-D6-1：授予價、股權獎酬表、委託書表格

def test_comp_table_cells_one_per_line_masked():
    """10-K 股權獎酬附註（每格一行）：授予日公允價表頭之後的每股金額格遮蔽，股數格保留。"""
    src = ("Weighted-average grant-date fair value of options granted\n\n(per share)$32.25 $22.03 $18.01\n\n"
           "Shares\n\nWeighted-Average Exercise Price\n\nOutstanding at January 1, 2025\n\n32,523\n\n$\n\n145.32\n\n"
           "Granted\n\n723\n\n$ 160.10\n")
    out = _clean(src)
    for num in ("32.25", "22.03", "18.01", "145.32", "160.10"):
        assert num not in out
    assert "32,523" in out and "723" in out


def test_def14a_equity_award_tables_dropped_toc_entry_only_short():
    src = ("Table of Contents\nSummary Compensation Table\nOutstanding Equity Awards at Fiscal Year-End\n"
           "Option Exercises and Stock Vested\nPotential Payments Upon Termination\n"
           "Compensation Discussion and Analysis\nOur PSUs vest on relative TSR (50% weighting) and ROIC.\n"
           "Outstanding Equity Awards at Fiscal Year-End\nName\nJane Doe\n12,000\n$ 2,345,678\n"
           "Executive Compensation\n8,000\n$ 1,563,785\n"
           "Option Exercises and Stock Vested\nJane Doe\n5,000\n$ 975,500\n"
           "Pension Benefits\nThe plan is frozen.\n"
           "Pay Versus Performance\nValue of initial fixed $100 investment 145.20 120.10\n"
           "CEO Pay Ratio\nThe ratio was 250:1.\n")
    out, st = dp.sanitize_filing_text(src)
    for leak in ("2,345,678", "1,563,785", "975,500", "145.20"):
        assert leak not in out
    assert "Potential Payments Upon Termination" in out          # 目錄的下一個標題還在（只刪到下一個標題）
    assert "relative TSR (50% weighting)" in out                  # 指標名稱與權重保留
    assert "The plan is frozen." in out and "The ratio was 250:1." in out
    assert st["dropped_sections"] >= 4


@pytest.mark.parametrize("src,leak", [
    ("The weighted-average grant-date fair value per share was $55.10.", "55.10"),
    ("Market value of the shares that have not vested $1,234,567", "1,234,567"),
    ("Market or payout value of unearned shares $2,345,678", "2,345,678"),
    ("Value of $100 invested on December 31, 2020 grew to $187.30.", "187.30"),
])
def test_comp_price_sentences_removed(src, leak):
    assert leak not in _clean(src)


# ---------- M4-D6-5：TSR 數值／名次、股價波動率

def test_tsr_rank_and_volatility_removed_but_weights_kept():
    src = ("PSUs are earned on relative TSR (50% weighting) and ROIC. Relative TSR ranked 2 of 9 peers. "
           "The payout was 110% of target.\n"
           "Expected volatility 34.0%\n利率波動加劇。\nBlack-Scholes 模型假設股價波動率 34%。\n相對 TSR 第 2／9。")
    out = _clean(src)
    assert "relative TSR (50% weighting) and ROIC." in out
    assert "ranked 2 of 9" not in out and "34.0%" not in out and "34%" not in out and "第 2／9" not in out
    assert "利率波動加劇。" in out
    assert "The payout was 110% of target." in out                 # 同段其他句子保留（逐句刪）


# ---------- 逐句刪取代整行刪（check-F04：SPG 激勵計畫段整段消失）

def test_sentence_level_scrub_keeps_rest_of_paragraph():
    para = ("The LTIP units vest ratably over three years subject to continued employment. "
            "The stock price on the grant date was $150.00. "
            "Awards are forfeited upon termination for cause.")
    out = _clean(para)
    assert "$150.00" not in out and "stock price" not in out
    assert out == ("The LTIP units vest ratably over three years subject to continued employment. "
                   "Awards are forfeited upon termination for cause.")


# ---------- M4-D1-15、M4-D6-3：回購句

def test_year_with_comma_not_masked_as_share_count():
    out = _clean("In 2025, shares repurchased under the program totaled 2,000,000 shares.")
    assert "In 2025," in out and "2,000,000" not in out


@pytest.mark.parametrize("src,leaks", [
    ("We acquired 5,000,000 shares during the quarter at an average cost of $50.00.", ("5,000,000", "50.00")),
    ("Shares held in treasury increased by 2,000,000 shares.", ("2,000,000",)),
    ("ALL reacquired 1,250,000 shares at an average cost of $89.95.", ("1,250,000", "89.95")),
    ("Shares repurchased 5,138,535 $321.10", ("5,138,535", "321.10")),
    ("During 2025, 2024 and 2023, the Company repurchased 544,716, 792,317 and 1,000,000 shares, respectively.",
     ("544,716", "792,317", "1,000,000")),
    ("The Operating Partnership redeemed 50,000 preferred units for $5.0 million.", ("50,000", "5.0 million")),
    ("Share repurchases (308,183) (300,000)", ("308,183", "300,000")),
    ("Treasury stock acquired— (5,642,036)", ("5,642,036",)),
    ("Purchases of treasury stock (1,815)(2,656)(3,086)", ("1,815", "2,656", "3,086")),
    ("We repurchased 8.6, 3.9 and 12.8 million shares for $1.0 billion, $500 million and $2.3 billion.",
     ("8.6", "3.9", "12.8", "$1.0 billion", "$500 million", "$2.3 billion")),
    ("2023 Repurchases645,753 62,778", ("645,753", "62,778")),
    ("During 2021, we repurchased 20,000,000 shares, or 8.7% of total common shares outstanding as of "
     "December 31, 2020, for $3.26 billion.", ("20,000,000", "$3.26 billion")),
    ("In 2017, we returned $1.9 billion to shareholders through a combination of common share repurchases "
     "and dividends.", ("$1.9 billion",)),
])
def test_buyback_counts_and_amounts_masked(src, leaks):
    out = _clean(src)
    for x in leaks:
        assert x not in out
    assert dp.buyback_leaks(out) == []


def test_buyback_amount_mask_only_near_keyword():
    src = ("The decrease from December 31, 2024, was impacted by $3.4 billion in payments associated with "
           "PFAS-related environmental liabilities and the CAE legal settlement (as discussed in Note 17), "
           "$1.4 billion in purchases of treasury stock and $1.6 billion of dividends.")
    out = _clean(src)
    assert "$3.4 billion in payments associated with PFAS" in out
    assert "$1.4 billion" not in out


@pytest.mark.parametrize("src", [
    "Dilutive effect computed on the treasury stock method 1,234 1,456",
    "In February 2025, 3M's Board of Directors approved a new repurchase program.",
    "Includes $75 million of long-term borrowings related to repurchase agreements, secured by CLO Investments.",
    "Changes in the market value of our shares of Premier amounted to $16 million.",
    "TSR is weighted thirty percent (30%), ROA and EPS Growth are each weighted twenty-five percent (25%).",
    "If relative TSR is in the top quartile (higher than 75th percentile), the units increase by 50%.",
    "HP's stock has faced significant volatility during 2025.",
])
def test_non_leak_text_untouched(src):
    assert _clean(src) == src


# ---------- M4-D6-4：單一清洗入口

def test_sanitize_order_and_stats():
    raw = ("Item 5. Market for Registrant's Common Equity\nHigh $90 Low $70\nItem 6. Reserved\n"
           "We repurchased 1,733,574 shares for $450 million at an average price of $259.60 per share.\n"
           "Our stock price may be volatile.\nRevenue grew 5%.\n")
    out, st = dp.sanitize_filing_text(raw)
    assert set(st) == {"dropped_sections", "buyback_masked", "per_share_masked", "removed_lines"}
    assert st["dropped_sections"] == 1 and st["removed_lines"] == 1 and st["buyback_masked"] >= 2
    for leak in ("$90", "1,733,574", "$450 million", "259.60", "stock price"):
        assert leak not in out
    assert "Revenue grew 5%." in out


def test_peers_uses_single_entry():
    assert peers.sanitize_filing_text is dp.sanitize_filing_text
    for old in ("drop_price_sections", "mask_buyback_shares", "scrub_price_text"):
        assert not hasattr(peers, old)


# ---------- P12R 返工：獎酬表帶日期列、黏字波動率、多欄 EPS 表、住宅售價

@pytest.mark.parametrize("src,leaks,keeps", [
    ("(in thousands)Shares Weighted-Average Grant-Date Fair Value Per Share\n\nNonvested as of January 31, 20251,277 $224.15"
     "\n\nCanceled or forfeited(119)232.46\n\nNonvested as of January 30, 2026 1,355 $ 233.07",      # LOW 型
     ("224.15", "232.46", "233.07"), ("January 30, 2026 1,355", "(119)")),
    ("Shares Weighted-Average Exercise Price\nOutstanding at June 1, 202511,712,352 $222.31\n"
     "Unvested at May 31, 2026420,338 247.03", ("222.31", "247.03"), ("11,712,352",)),            # FDX 型
    ("Shares Weighted-average grant-date fair value\nNon-vested at end of year4,031 $52.45 765 $52.53",
     ("52.45", "52.53"), ("4,031", "765")),                                                         # HIG 型
    ("Number of RSUs\nWeighted-Average\nGrant-Date Fair\nValue Per RSU\nBalance as of September 27, 2025151,574 $189.75\n"
     "Outstanding at beginning of year2,201 $228.23", ("189.75", "228.23"), ("151,574", "2,201")),   # AAPL／HD 型（表頭拆行）
])
def test_comp_table_dated_rows_masked(src, leaks, keeps):
    out = _clean(src)
    for x in leaks:
        assert x not in out
    for x in keeps:
        assert x in out


def test_comp_table_rows_keep_percent_and_eps_rows():
    src = ("Weighted-average grant-date fair value\nRisk-free interest rate4.25 %\nDiluted earnings per share $5.12 $4.98\n"
           "Expected term (in years)5.10 5.12")
    assert _clean(src) == src


@pytest.mark.parametrize("src", [
    "Expected volatility26.1 %24.6 %22.8 %",
    "Weighted average volatility23.0 %17.6 %",
    "Volatility factor29.9 %22.0 %",
    "The fair value of stock awards with market-based condition assumes a risk-free rate of 0.2%, volatility of 29.9% "
    "and a term of 3 years.",                                                                        # ALL 型
    "The significant assumptions used in this simulation model were a volatility rate of 33.95%, a risk-free rate of 4%.",
])
def test_glued_volatility_rows_removed(src):
    out = _clean(src)
    assert "%" not in out


def test_multi_column_eps_cells_not_masked():
    src = "EPS-diluted\n$22.64 per share\n$24.52 per share"
    assert _clean(src) == src


def test_home_selling_price_not_masked():
    src = ("The average unit selling price was $566,000 in 2025, compared with $555,000 in 2024, "
           "and $545,000 in 2023, on 90,000 homes and units closed.")
    assert _clean(src) == src


@pytest.mark.parametrize("src", [
    "The average selling price of homes closed was $566,000 in 2025, on 90,000 homes and units closed.",
    "The average home selling price was $566,000 on 90,000 homes and units closed.",
])
def test_home_selling_price_variants_not_masked(src):   # P12R2
    assert _clean(src) == src


@pytest.mark.parametrize("src", [
    "Shares sold under the ATM program at an average selling price of $57.14.",
    "In 2025 we issued 2.1 million shares under our ATM program at a weighted average selling price of $57.14, "
    "for net proceeds of $118 million.",
])
def test_atm_selling_price_masked(src):   # P12R2：(?<!selling\s) 過寬的回歸
    out, st = dp.sanitize_filing_text(src)
    assert "57.14" not in out and st["per_share_masked"] >= 1
    assert "$118 million" not in out   # D84（A3-M4D-01）：ATM 句的發行總額也遮


@pytest.mark.parametrize("src, amt", [
    ("Each unit consists of one share and one warrant; the offering price of units was $10.00.", "10.00"),
    ("The price of units issued under the ATM program averaged $33.10.", "33.10"),
    ("We sold shares under the ATM program at an average price of $57.14 and received net proceeds of $118 million.",
     "57.14"),
])
def test_unit_price_and_amount_followed_by_a_word_masked(src, amt):   # P12R3
    out, st = dp.sanitize_filing_text(src)
    assert amt not in out and st["per_share_masked"] >= 1


@pytest.mark.parametrize("src", [
    # P12R4：原「Gasoline averaged $3.85 a gallon」無價格字也無股份語境，跑不到尾端判斷（空轉）；改用含 price＋share 的句子
    "The price of steel was $2,400 a ton, which affected our share of costs.",
    "The average unit selling price was $566,000 in 2025, on 90,000 homes and units closed.",
    "The average selling price of homes closed was $566,000 in 2025.",
])
def test_a_gallon_and_home_price_kept(src):   # P12R3
    assert _clean(src) == src


@pytest.mark.parametrize("src, amts", [
    ("The average share prices at the time of vesting of $301.17, $289.52 and $221.26 were used.",
     ["301.17", "289.52", "221.26"]),
    ("The closing stock price on the grant dates of $164.40 and $151.06 was used to value the awards.",
     ["164.40", "151.06"]),
    ("The CPP warrant exercise price was $8.836 and $8.999, respectively, for warrants to purchase common shares.",
     ["8.836", "8.999"]),
    ("RSUs were valued at the fair values of $55.10, and $60.20 on the grant dates.", ["55.10", "60.20"]),
])
def test_listed_prices_after_first_all_masked(src, amts):   # P12R4：只遮第一個價格的漏洞
    # 直接測 mask_per_share_amounts（sanitize 的逐句刪除會把 share/stock price 句整句拿掉，測不到遮蔽本身）
    out, k = dp.mask_per_share_amounts(src)
    assert not [a for a in amts if a in out] and k == len(amts) and out.count(dp.PER_SHARE_MASK) == len(amts)


@pytest.mark.parametrize("src, keep", [
    # D84（A3-M4D-01）：ATM／增資句的發行總額改遮（見 test_fix_F3_C）；量詞金額保留改用非發行句驗
    ("We sold shares to the trust at an average price of $57.14 and $118 million of cash was received.", "$118 million"),
    ("The price of stock awards was $40.00 and $2.10 a gallon was paid for fuel.", "$2.10 a gallon"),
])
def test_listed_amount_with_magnitude_or_unit_kept(src, keep):   # P12R4：量詞／其他單位不算連續價格
    out = _clean(src)
    assert keep in out and dp.PER_SHARE_MASK in out


def test_fair_value_option_not_share_context():   # P12R4：保險公司「fair value option」不是股票選擇權
    src = "The fair value of assets using the fair value option was $125 and $168, respectively."
    assert _clean(src) == src
    # 真正的股票選擇權仍遮
    out = _clean("The fair value of options granted was $12.40 per option.")
    assert "12.40" not in out
