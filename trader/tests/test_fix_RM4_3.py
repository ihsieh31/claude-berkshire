"""RM4-3：M4-R2-02 封面股數拆股換算＋價格檔拆股註記、M4-D4-02 review 路徑 splits、M4-R3-02 ctx.prices。"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from trader.analysis import dossier_prep as dp


def _cover_facts(rows):
    return {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": rows}}}}}


NVDA_COVER = [{"filed": "2024-05-29", "end": "2024-05-17", "accn": "q1", "form": "10-Q", "val": 2_460_000_000},
              {"filed": "2024-02-21", "end": "2024-02-16", "accn": "k1", "form": "10-K", "val": 2_464_000_000}]


def test_cover_shares_multiplied_by_split_after_filing():
    md, cs = dp.cover_shares_section(_cover_facts(NVDA_COVER), "2024-06-20", splits="2024-06-10:10",
                                     upto="2024-06-18")
    assert cs["shares"] == pytest.approx(24_600_000_000)
    assert cs["shares_as_filed"] == pytest.approx(2_460_000_000) and cs["split_factor"] == pytest.approx(10)
    assert cs["filed"] == "2024-05-29"
    assert "24,600.0 百萬股" in md and "×10" in md


def test_cover_shares_split_after_price_date_not_applied():
    _, cs = dp.cover_shares_section(_cover_facts(NVDA_COVER), "2024-06-20", splits="2024-06-10:10",
                                    upto="2024-06-07")
    assert cs == {"shares": pytest.approx(2_460_000_000), "filed": "2024-05-29"}


def test_cover_shares_splits_none_says_unchecked():
    md, cs = dp.cover_shares_section(_cover_facts(NVDA_COVER), "2024-06-20")
    assert cs == {"shares": pytest.approx(2_460_000_000), "filed": "2024-05-29"}
    assert "拆股未核" in md
    md2, _ = dp.cover_shares_section(_cover_facts(NVDA_COVER), "2024-06-20", splits="")
    assert "拆股未核" not in md2


def test_splits_meta_events_after_last_10k():
    m = dp.splits_meta(_cover_facts(NVDA_COVER), "2024-06-20", "2021-07-20:4;2024-06-10:10", "2024-06-18")
    assert m["checked"] is True and m["last_10k_filed"] == "2024-02-21"
    assert m["events_after_last_10k"] == [{"ex_date": "2024-06-10", "ratio": 10.0}]
    assert "×10" in m["note"]
    assert dp.splits_meta(_cover_facts(NVDA_COVER), "2024-06-20", None, None)["checked"] is False
    assert dp.splits_meta(_cover_facts(NVDA_COVER), "2024-06-20", "", "2024-06-18")["events_after_last_10k"] == []


def test_build_dossier_base_accepts_splits_and_deep_passes_them():
    assert "splits" in inspect.signature(dp.build_dossier_base).parameters
    from trader.jobs import deep
    src = inspect.getsource(deep)
    assert 'defer_assemble=True, splits=p.get("splits")' in src


class _Sec:
    def cik_for(self, t):
        return "0000000001"

    def filings(self, cik, asof, since=None, forms=None):
        return [{"filingDate": "2026-05-01", "form": "8-K", "items": "2.02,9.01"},
                {"filingDate": "2026-08-01", "form": "8-K", "items": "2.02"}]


class _Prices:
    def __init__(self):
        self.calls = 0

    def bars(self, symbols, start, end, adjustment="raw"):
        import pandas as pd
        self.calls += 1
        return pd.DataFrame({"date": ["2026-04-30", "2026-05-01", "2026-10-14"], "close": [10.0, 11.0, 12.0]})


def test_p30_data_uses_ctx_prices_without_config():
    from trader.jobs.deep import _p30_data
    px = _Prices()
    ctx = SimpleNamespace(config=None, paths=None, sec=_Sec(), prices=px)
    dates, closes = _p30_data(ctx, "AAA", "2026-10-15")
    assert dates == ["2026-05-01", "2026-08-01"] and px.calls == 1
    assert closes[-1] == ("2026-10-14", 12.0)


def test_handler_ctx_has_prices_and_pipelines_inject_it():
    from trader.jobs.handlers import HandlerCtx
    assert "prices" in HandlerCtx.__dataclass_fields__
    from trader.pipelines import analyze, review
    assert "engine.ctx.prices" in inspect.getsource(analyze) and "engine.ctx.prices" in inspect.getsource(review)


def test_review_execute_puts_splits_into_params():
    from trader.pipelines import review
    src = inspect.getsource(review)
    assert "review_splits" in src and "corporate_actions(" in src
