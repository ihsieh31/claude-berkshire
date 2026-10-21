"""RM4-2b：dossier_prep 價格遮蔽小缺口（check-RM4-2c 提出）。"""
from trader.analysis import dossier_prep as dp


def _s(t):
    return dp.sanitize_text(t)[0]


def test_wrap_does_not_eat_next_bullet():
    t = ("The weighted average grant-date fair value per share of awards granted during 2025 was $54.07, and\n"
         "• We operate 120 stores in Texas.")
    out = _s(t)
    assert "54.07" not in out and "We operate 120 stores in Texas." in out


def test_wrap_does_not_eat_item_heading():
    t = ("The closing price of our common stock on the last trading day of the year, which was $150.12, and\n"
         "Item 7. Management's Discussion and Analysis")
    out = _s(t)
    assert "150.12" not in out and "Item 7." in out


def test_wrap_still_continues_ordinary_line():
    t = ("The weighted average grant-date fair value per share of performance shares granted during\n"
         "the years ended December 31, 2019 was $54.07.\nCompensation expense is recognized ratably.")
    out = _s(t)
    assert "54.07" not in out and "Compensation expense is recognized ratably." in out


def test_price_row_year_cell_keeps_window():
    out = _s("Common stock price\n2025\n$169.47\n2024\n$190.77\nNet income\n1,234")
    assert "169.47" not in out and "190.77" not in out
    assert "2025" in out and "Net income\n1,234" in out


def test_long_prose_ending_paren_no_window():
    prose = ("We estimate the fair value of options using the Black-Scholes model, which requires assumptions about "
             "volatility, the expected term and the exercise price of each grant; these assumptions are reviewed annually "
             "by management and the compensation committee (see Note 14)")
    assert len(prose) > 200
    out = _s(prose + "\nTotal revenues $12.34")
    assert "12.34" in out


def test_long_sticky_header_still_opens_window():
    head = ("Plan CategoryNumber of securities to be issued upon exercise of outstanding options, warrants and rights (a)"
            "Weighted-average exercise price of outstanding options, warrants and rights (b)Number of securities remaining "
            "available for future issuance under equity compensation plans (excluding securities reflected in column (a)) (c)")
    out = _s(head + "\nPrudential Omnibus Plan7,399,810(1)$97.67 (2)12,496,717")
    assert "97.67" not in out


def test_consideration_total_without_decimals_kept():
    t = "Total consideration of $3,500, consisting of cash and shares of the acquirer."
    assert "3,500" in _s(t)


def test_consideration_per_share_masked():
    for t in ("merger consideration of $52.00 in cash for each share", "consideration of $48 for each share"):
        assert "52.00" not in _s(t) and "$48 " not in _s(t)


def test_num_backtracking_guarded():
    assert "45.00" in _s("A price of $45.00 per unit of product was charged.")
    assert "3.5 billion" in _s("Total cost of $3.5 billion for the new plant.")


def test_sticky_dollar_cell_masked():
    out = _s("Weighted-average exercise price\nTotal1,234,567$106.34987,654")
    assert "106.34" not in out
