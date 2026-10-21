"""RM4-2：M4 價格遮蔽群（reaudit M4_S6）。
M4-RS6-01 一格一行股價／市值列（DHI 委託書 Stock Price and Other Data）、M4-RS6-02 折行價格句、
M4-RS6-03 權益薪酬計畫表／選擇權表黏字列（GL $106.34、PRU、HD「years$249.25」、表接表）、
M4-RS6-04 sanitize_text 中文「每股 $X」與英文「for each share」；另：is_price_headline(blind=True)＝FULL_A 濾網（THESIS 用），
sanitize_text 不共用 writeback._VALUATION_RE（只加回歸保護）。全部純字串，不碰 state。"""
from __future__ import annotations

import pytest

from trader.analysis import dossier_prep as dp
from trader.jobs import review_jobs

M = dp.PER_SHARE_MASK

# DHI DEF 14A 2025-12-10「Stock Price and Other Data」表（HTML 轉文字後一格一行；數字取自 r2 sec/ 實檔 :741-776）
DHI_TABLE = """As of and for the Fiscal Year Ended September 30,
2025
2024
2023
2022
2021

Stock Price and Other Data
Common stock price

$169.47

$190.77

$107.47

$67.35

$83.97

(11)%

102%


Equity market capitalization (millions)

$49,905

$61,815

$35,986

$23,165

$29,895

(19)%

67%

Book value per share

$82.15

$78.12

Diluted earnings per share

$11.57

$14.34
"""


def _s(t: str) -> str:
    return dp.sanitize_text(t)[0]


# ---------------------------------------------------------------- M4-RS6-01

def test_dhi_stock_price_and_market_cap_cells_masked():
    out = _s(DHI_TABLE)
    for v in ("169.47", "190.77", "107.47", "67.35", "83.97", "49,905", "61,815", "35,986", "23,165", "29,895"):
        assert v not in out, v
    assert "(11)%" not in out and "102%" not in out   # 股價漲跌幅也遮
    # 年份表頭、每股淨值、稀釋 EPS 照留
    for keep in ("2025", "2021", "Book value per share", "$82.15", "$78.12", "Diluted earnings per share", "$11.57", "$14.34"):
        assert keep in out, keep
    assert "Common stock price" not in out and "market capitalization" not in out   # 列標由 scrub 刪
    assert out.count(M) == 14


def test_sanitize_filing_text_same_on_dhi_table():
    out, st = dp.sanitize_filing_text(DHI_TABLE)
    assert "169.47" not in out and "49,905" not in out and st["per_share_masked"] >= 14


def test_closing_price_cells_without_dollar_and_window_ends_at_text():
    out = _s("Closing stock price\n169.47\n190.77\nRevenue\n1,234\n2,345")
    assert "169.47" not in out and "190.77" not in out
    assert "Revenue\n1,234\n2,345" in out


def test_price_label_does_not_mask_eps_or_prose():
    t = "Market price risk\nWe are exposed to interest rate risk on $1,000 million of debt.\n2.50"
    assert _s(t).endswith("2.50")   # 段落文字已結束視窗
    assert _s("Book value per share\n$82.15") == "Book value per share\n$82.15"


# ---------------------------------------------------------------- M4-RS6-02

HIG = ("The weighted average grant-date fair value per share of performance shares granted during\n"
       "the years ended December 31, 2019, 2018, and 2017 was $54.07, $50.26 and $48.89, respectively.\n"
       "Compensation expense is recognized over the vesting period.")
DHI_WRAP = ("Unvested units are valued based on the closing price of our common stock on\n"
            "September 30, 2025 of $169.47. Mr. Auld's awards vest ratably over three years.")


def test_wrapped_price_sentence_hig():
    out = _s(HIG)
    for v in ("54.07", "50.26", "48.89"):
        assert v not in out
    assert "Compensation expense is recognized over the vesting period." in out


def test_wrapped_price_sentence_dhi_keeps_rest_of_line():
    out = _s(DHI_WRAP)
    assert "169.47" not in out
    assert "awards vest ratably over three years." in out


def test_wrap_not_applied_to_table_rows_or_finished_sentences():
    # 以數字結尾的表格列、以名詞結尾的列標、句號結尾的句子都不延續
    assert "Total revenue 1,234" in _s("Average price paid per share $50.12\nTotal revenue 1,234")
    t = "Aggregate market value of common stock held by non-affiliates: $1,000 million\nNet income $ 500 million"
    assert "Net income $ 500 million" in _s(t)
    t2 = "The closing price of our stock is published daily on the exchange.\nNet sales were $5.2 billion."
    assert "Net sales were $5.2 billion." in _s(t2)


# ---------------------------------------------------------------- M4-RS6-03

GL_ECP = ("ITEM 12. SECURITY OWNERSHIP OF CERTAIN BENEFICIAL OWNERS AND MANAGEMENT AND RELATED STOCKHOLDER MATTERS\n\n"
          "1. Equity Compensation Plan Information as of December 31, 2025\n\n(a)(b)(c)\n\n\n"
          "Equity compensation plans approved by security holders4,303,944 $106.34 2,898,770\n\n"
          "Equity compensation plans not approved by security holders— — —\n\n"
          "Total4,303,944 $106.34 2,898,770\n")
LONG_HEAD = ("Plan CategoryNumber of securities to be issued upon exercise of outstanding options, warrants and rights (a)"
             "Weighted-average exercise price of outstanding options, warrants and rights (b)Number of securities remaining "
             "available for future issuance under equity compensation plans (excluding securities reflected in column (a)) (c)")


def test_gl_equity_comp_plan_rows_masked():
    out = _s(GL_ECP)
    assert "106.34" not in out
    assert "4,303,944" in out and "2,898,770" in out   # 股數保留


def test_long_sticky_header_opens_window_pru():
    assert len(LONG_HEAD) > 200
    out = _s(LONG_HEAD + "\nPrudential Omnibus Plan7,399,810(1)$97.67 (2)12,496,717")
    assert "97.67" not in out and "7,399,810" in out and "12,496,717" in out


def test_long_prose_with_black_scholes_does_not_open_window():
    prose = ("The fair value method requires the use of an option valuation model to value employee stock options. "
             "The Company has elected to use the Black-Scholes valuation model for option expensing. A summary of "
             "assumptions for options granted in each of the three years is as follows:")
    assert len(prose) > 200
    out = _s(prose + "\nExpected term (in years)\n5.71\n5.78")
    assert "5.71" in out and "5.78" in out


def test_years_label_masks_dollar_cells_only_hd():
    out = _s("Weighted-average exercise price\nOutstanding2,156 $272 4.7 years$249.25\nExpected term (in years)5.105.12")
    assert "249.25" not in out
    assert "Expected term (in years)5.105.12" in out


def test_one_cell_per_line_years_label_kept():
    out = _s("Weighted-average exercise price\n$45.12\nExpected term (in years)\n5.71\n5.78")
    assert "45.12" not in out
    assert "5.71" in out and "5.78" in out


def test_second_table_split_header_inside_window_gl():
    # GL 10-K：選擇權餘額表後接選擇權變動表，第二張表的拆行表頭在第一張表的視窗內 → 重開視窗
    head1 = "\n".join(["Weighted-", "Average", "Exercise", "Price"])
    rows1 = "\n".join(f"{i:,} 1.89$92.51 {i:,} $92.51" for i in range(1000, 1010))
    filler = "\n".join(["129", "GL 2025 FORM 10-K", "Table of Contents", "Notes to Consolidated Financial Statements"] * 10)
    head2 = "\n".join(["OptionsWeighted-Average", "Exercise", "Price"])   # 第一張表視窗剩約 7 行時出現
    rows2 = ("\n".join(f"Granted:\n{i:,} 127.43 520,810 128.32 497,060 120.49" for i in range(10))
             + "\nOutstanding—end of year\n4,303,944 $106.34 5,825,957 $99.74 5,940,320 $96.19\n"
             "Exercisable at end of year\n3,230,992 $99.69 4,376,436 $93.73 4,003,028 $91.23")
    out = _s("\n".join([head1, rows1, filler, head2, rows2]))
    for v in ("92.51", "106.34", "99.74", "99.69", "93.73", "91.23"):
        assert v not in out, v


def test_comp_table_eps_cells_one_per_line_kept():
    out = _s("Weighted-average grant date fair value\n$45.12\nDiluted earnings per share\n$11.57")
    assert "45.12" not in out and "$11.57" in out


# ---------------------------------------------------------------- M4-RS6-04

@pytest.mark.parametrize("t,v", [
    ("以每股 $52.00 現金收購。", "52.00"),
    ("RSU 每股 $192.37。", "192.37"),
    ("每股 $52.00 的收購對價", "52.00"),
    ("merger consideration of $52.00 in cash for each share", "52.00"),
    ("Holders will receive $52.00 in cash for each share.", "52.00"),
    ("Holders will receive $52.00 for each share.", "52.00"),
])
def test_writeback_per_share_forms_masked(t, v):
    assert v not in _s(t)


@pytest.mark.parametrize("t", [
    "每股股利 $0.5", "每股盈餘 $5.2，每股淨值 $30。", "A dividend of $0.25 for each share was paid.",
    "Diluted EPS of $1.20 per share.", "Liquidation preference of $25.00 for each share.",
    "Total consideration of $1.2 billion for the acquisition of shares of Acme.",
])
def test_writeback_dividend_eps_kept(t):
    assert _s(t) == t


# ---------------------------------------------------------------- 估值句：不共用 writeback._VALUATION_RE（避免誤殺基本面）

@pytest.mark.parametrize("t", [
    "PART IV Item 15. Exhibits and Financial Statement Schedules",
    "Schedule IV — Reinsurance 2025",
    "退休金計畫資產預期報酬率 6.5%。",
    "可供出售債券公允價值 12,345 百萬美元。",
])
def test_sanitize_text_keeps_fundamental_lines_valuation_words(t):
    assert _s(t) == t


# ---------------------------------------------------------------- 新聞標題：THESIS 與 FULL_A 一致

PRICEY = ["Apple falls on China worries", "Apple slumps after iPhone sales disappoint", "Apple cut to neutral at Barclays",
          "Apple hits new high", "Apple valued at $4T", "Is Apple a buy?", "蘋果創新高", "Apple downgraded to Sell"]
FUND = ["Apple revenue rises 6% to $90 billion", "Apple drops lawsuit against Masimo",
        "Apple announces $90 billion share buyback", "Apple CEO steps down"]


def test_is_price_headline_blind_matches_full_a():
    from trader.jobs.deep import _blind_drop
    for h in PRICEY:
        assert dp.is_price_headline(h, blind=True), h
    for h in FUND:
        assert not dp.is_price_headline(h, blind=True), h
    for h in PRICEY + FUND:
        assert dp.is_price_headline(h, blind=True) == (dp.is_price_headline(h) or _blind_drop(h))
    # 預設（FULL_B、review news.json）不變
    assert not dp.is_price_headline("Apple falls on China worries")
    assert not dp.is_price_headline("Apple cut to neutral at Barclays")


def test_news_text_blind_for_thesis():
    params = {"news": [{"date": "2026-10-05", "headline": h, "source": "s"} for h in PRICEY + FUND]}
    blind = review_jobs._news_text(params, blind=True)
    assert not [h for h in PRICEY if h in blind], blind
    assert all(h in blind for h in FUND)
    assert "Apple falls on China worries" in review_jobs._news_text(params)   # FULL_B 路徑不變


def test_thesis_prompt_uses_blind_news():
    import inspect
    src = inspect.getsource(review_jobs)
    assert '"news": _news_text(job.params, blind=True)' in src
