"""XBRL 分類別股數換算測試：全部使用假 sec 物件與手寫 instance，不連網。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trader.data import xbrl_shares as xs


# ---------------------------------------------------------------------------
# 假 sec 與假 HTTP
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, text: str = "", data=None):
        self.text = text
        self._data = data

    def json(self):
        return self._data


class FakeHttp:
    """routes：{完整網址: JSON dict | 字串}；未知網址拋 RuntimeError 模擬失敗。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None):
        self.calls.append(url)
        if url not in self.routes:
            raise RuntimeError(f"no route: {url}")
        body = self.routes[url]
        if isinstance(body, str):
            return FakeResp(text=body)
        return FakeResp(data=body)


class FakeSec:
    def __init__(self, cache_dir, filings: dict, routes: dict):
        self.cache_dir = cache_dir
        self._filings = filings  # cik(int 字串) → 申報清單
        self.http = FakeHttp(routes)

    def filings(self, cik, asof, since=None, forms=None):
        out = []
        for f in self._filings.get(str(int(cik)), []):
            if f["filingDate"] >= asof:
                continue
            if forms is not None and f["form"] not in forms:
                continue
            out.append(dict(f))
        return sorted(out, key=lambda f: f["filingDate"])


# ---------------------------------------------------------------------------
# instance 建構器
# ---------------------------------------------------------------------------
def _seg(dims) -> str:
    if not dims:
        return ""
    mem = "".join(
        f'<xbrldi:explicitMember dimension="{ax}">{m}</xbrldi:explicitMember>' for ax, m in dims
    )
    return f"<xbrli:segment>{mem}</xbrli:segment>"


def dur_ctx(cid, start, end, dims=()) -> str:
    return (f'<xbrli:context id="{cid}"><xbrli:entity>'
            f'<xbrli:identifier scheme="http://www.sec.gov/CIK">0001067983</xbrli:identifier>'
            f'{_seg(dims)}</xbrli:entity><xbrli:period>'
            f'<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>'
            f'</xbrli:period></xbrli:context>')


def inst_ctx(cid, date, dims=()) -> str:
    return (f'<xbrli:context id="{cid}"><xbrli:entity>'
            f'<xbrli:identifier scheme="http://www.sec.gov/CIK">0001067983</xbrli:identifier>'
            f'{_seg(dims)}</xbrli:entity><xbrli:period>'
            f'<xbrli:instant>{date}</xbrli:instant></xbrli:period></xbrli:context>')


def fact(tag, cref, value, extra="") -> str:
    return f'<{tag} contextRef="{cref}" unitRef="u" decimals="0"{extra}>{value}</{tag}>'


CLS = "usg:StatementClassOfStockAxis"


def instance(contexts, facts, dei="2025", gaap="2025") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        f'xmlns:dei="http://xbrl.sec.gov/dei/{dei}" '
        f'xmlns:us-gaap="http://fasb.org/us-gaap/{gaap}" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:usg="http://fasb.org/us-gaap/2025">'
        + "".join(contexts) + "".join(facts) + "</xbrli:xbrl>"
    )


def two_class_xml(dei="2025", gaap="2025", eps_a="3000", eps_b="2", dup_shares=False) -> str:
    """A 類 500 股、EPS 3000；B 類 1,000,000 股、EPS 2。"""
    ctxs = [
        inst_ctx("iA", "2026-10-10", [(CLS, "brk:ClassAMember")]),
        inst_ctx("iB", "2026-10-10", [(CLS, "brk:ClassBMember")]),
        dur_ctx("dA", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassAMember")]),
        dur_ctx("dB", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassBMember")]),
    ]
    facts = [
        fact("dei:EntityCommonStockSharesOutstanding", "iA", "500"),
        fact("dei:TradingSymbol", "iA", "BRK.A"),
        fact("dei:EntityCommonStockSharesOutstanding", "iB", "1000000"),
        fact("dei:TradingSymbol", "iB", "BRK.B"),
        fact("us-gaap:EarningsPerShareDiluted", "dA", eps_a),
        fact("us-gaap:EarningsPerShareDiluted", "dB", eps_b),
    ]
    if dup_shares:  # inline 常見：同 context＋概念重複出現（值相同）
        facts.append(fact("dei:EntityCommonStockSharesOutstanding", "iA", "500"))
    return instance(ctxs, facts, dei=dei, gaap=gaap)


def parsed_two_class(**kw) -> dict:
    return xs.parse_instance(two_class_xml(**kw))


# ---------------------------------------------------------------------------
# parse_instance / equivalent_shares
# ---------------------------------------------------------------------------
def test_parse_two_class_fields():
    p = parsed_two_class()
    assert set(p) == {"ClassAMember", "ClassBMember"}
    assert p["ClassAMember"]["symbol"] == "BRK.A"
    assert p["ClassAMember"]["shares"] == 500
    assert p["ClassAMember"]["eps"] == 3000
    assert p["ClassAMember"]["eps_concept"] == "EarningsPerShareDiluted"
    assert p["ClassBMember"]["shares"] == 1_000_000
    assert p["ClassBMember"]["eps"] == 2


def test_two_class_equivalent_shares():
    total, desc = xs.equivalent_shares(parsed_two_class(), "BRK.B")
    assert total == 500 * 1500 + 1_000_000  # 1,750,000
    assert "BRK.B" in desc


@pytest.mark.parametrize("ticker", ["BRK.B", "BRK-B", "BRK/B", "brk b", " brk.b "])
def test_ticker_forms_are_equivalent(ticker):
    total, _ = xs.equivalent_shares(parsed_two_class(), ticker)
    assert total == 1_750_000


def test_base_class_a_gives_its_own_equivalent():
    # 以 A 類為基準：B 股數 × (2/3000) + A 股數
    total, _ = xs.equivalent_shares(parsed_two_class(), "BRK.A")
    assert total == pytest.approx(500 + 1_000_000 * 2 / 3000)


def test_single_class_no_dims_returns_shares_directly():
    ctx = [inst_ctx("i", "2026-10-10")]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "i", "1234")]
    p = xs.parse_instance(instance(ctx, facts))
    assert list(p) == [None]
    total, desc = xs.equivalent_shares(p, "ANYTHING")
    assert total == 1234.0
    assert "比例 1" in desc


def test_single_class_with_matching_symbol():
    ctx = [inst_ctx("i", "2026-10-10")]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "i", "77"),
             fact("dei:TradingSymbol", "i", "XYZ")]
    p = xs.parse_instance(instance(ctx, facts))
    assert xs.equivalent_shares(p, "xyz")[0] == 77


def test_single_class_symbol_mismatch_is_none():
    ctx = [inst_ctx("i", "2026-10-10")]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "i", "77"),
             fact("dei:TradingSymbol", "i", "OTHER")]
    p = xs.parse_instance(instance(ctx, facts))
    assert xs.equivalent_shares(p, "XYZ") is None


def test_missing_eps_in_a_class_is_none():
    ctxs = [inst_ctx("iA", "2026-10-10", [(CLS, "brk:ClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "brk:ClassBMember")]),
            dur_ctx("dB", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "iA", "500"),
             fact("dei:TradingSymbol", "iA", "BRK.A"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "1000000"),
             fact("dei:TradingSymbol", "iB", "BRK.B"),
             fact("us-gaap:EarningsPerShareDiluted", "dB", "2")]  # A 類沒有 EPS
    p = xs.parse_instance(instance(ctxs, facts))
    assert p["ClassAMember"]["eps"] is None
    assert xs.equivalent_shares(p, "BRK.B") is None


@pytest.mark.parametrize("eps_a,eps_b", [("-3000", "2"), ("3000", "0"), ("3000", "-2")])
def test_nonpositive_eps_is_none(eps_a, eps_b):
    assert xs.equivalent_shares(parsed_two_class(eps_a=eps_a, eps_b=eps_b), "BRK.B") is None


def test_nonpositive_eps_in_non_base_class_is_none():
    # 基準 B 的 EPS 正常，但 A 類 EPS 為負（虧損年）→ 不換算
    assert xs.equivalent_shares(parsed_two_class(eps_a="-100", eps_b="2"), "BRK.B") is None


def test_duplicate_context_fact_counted_once():
    p = xs.parse_instance(two_class_xml(dup_shares=True))
    assert p["ClassAMember"]["shares"] == 500
    assert xs.equivalent_shares(p, "BRK.B")[0] == 1_750_000


def test_conflicting_duplicate_is_none():
    xml = two_class_xml().replace(
        "</xbrli:xbrl>",
        fact("dei:EntityCommonStockSharesOutstanding", "iA", "999") + "</xbrli:xbrl>",
    )
    p = xs.parse_instance(xml)
    assert p["ClassAMember"]["shares"] is None
    assert xs.equivalent_shares(p, "BRK.B") is None


@pytest.mark.parametrize("dei,gaap", [("2024", "2024"), ("2025", "2025"), ("2024", "2025")])
def test_namespace_year_variants_parse(dei, gaap):
    total, _ = xs.equivalent_shares(parsed_two_class(dei=dei, gaap=gaap), "BRK.B")
    assert total == 1_750_000


def test_diluted_falls_back_to_basic():
    ctxs = [inst_ctx("iA", "2026-10-10", [(CLS, "brk:ClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "brk:ClassBMember")]),
            dur_ctx("dA", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassAMember")]),
            dur_ctx("dB", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "iA", "500"),
             fact("dei:TradingSymbol", "iA", "BRK.A"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "1000000"),
             fact("dei:TradingSymbol", "iB", "BRK.B"),
             fact("us-gaap:EarningsPerShareBasic", "dA", "3000"),
             fact("us-gaap:EarningsPerShareBasic", "dB", "2")]
    p = xs.parse_instance(instance(ctxs, facts))
    assert p["ClassAMember"]["eps_concept"] == "EarningsPerShareBasic"
    assert xs.equivalent_shares(p, "BRK.B")[0] == 1_750_000


def test_eps_picks_latest_end_then_longest_period():
    ctxs = [inst_ctx("iA", "2026-10-10", [(CLS, "brk:ClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "brk:ClassBMember")]),
            dur_ctx("fy", "2025-01-01", "2025-12-31", [(CLS, "brk:ClassAMember")]),
            dur_ctx("ytd", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassAMember")]),
            dur_ctx("q3", "2026-07-01", "2026-09-30", [(CLS, "brk:ClassAMember")]),
            dur_ctx("dB", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "iA", "500"),
             fact("dei:TradingSymbol", "iA", "BRK.A"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "1000000"),
             fact("dei:TradingSymbol", "iB", "BRK.B"),
             fact("us-gaap:EarningsPerShareDiluted", "fy", "10"),
             fact("us-gaap:EarningsPerShareDiluted", "ytd", "7"),
             fact("us-gaap:EarningsPerShareDiluted", "q3", "2.5"),
             fact("us-gaap:EarningsPerShareDiluted", "dB", "2")]
    p = xs.parse_instance(instance(ctxs, facts))
    assert p["ClassAMember"]["eps"] == 7  # 結束日最新 2026-09-30，其中期間最長的 YTD
    assert p["ClassAMember"]["eps_days"] == 272  # 2026-01-01 至 2026-09-30 的差值


def test_instant_shares_picks_latest_date():
    ctxs = [inst_ctx("old", "2026-02-01"), inst_ctx("new", "2026-10-10")]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "old", "400"),
             fact("dei:EntityCommonStockSharesOutstanding", "new", "500")]
    assert xs.parse_instance(instance(ctxs, facts))[None]["shares"] == 500


def test_eps_context_with_extra_dimension_ignored():
    ctxs = [inst_ctx("iA", "2026-10-10", [(CLS, "brk:ClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "brk:ClassBMember")]),
            dur_ctx("dA", "2026-01-01", "2026-09-30",
                    [(CLS, "brk:ClassAMember"), ("usg:SegmentAxis", "seg:SegX")]),
            dur_ctx("dB", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "iA", "500"),
             fact("dei:TradingSymbol", "iA", "BRK.A"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "1000000"),
             fact("dei:TradingSymbol", "iB", "BRK.B"),
             fact("us-gaap:EarningsPerShareDiluted", "dA", "3000"),  # 多維度，不採用
             fact("us-gaap:EarningsPerShareDiluted", "dB", "2")]
    p = xs.parse_instance(instance(ctxs, facts))
    assert p["ClassAMember"]["eps"] is None
    assert xs.equivalent_shares(p, "BRK.B") is None


def test_no_base_equal_eps_sums_directly():
    xml = two_class_xml(eps_a="2.0", eps_b="2.01")  # 差 0.5%
    p = xs.parse_instance(xml)
    total, _ = xs.equivalent_shares(p, "ZZZ")
    assert total == 1_000_500


def test_no_base_different_eps_is_none():
    xml = two_class_xml(eps_a="2.0", eps_b="2.05")  # 差 2.5%
    assert xs.equivalent_shares(xs.parse_instance(xml), "ZZZ") is None


def test_typed_member_context_ignored():
    ctxs = [
        '<xbrli:context id="t"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
        '<xbrli:segment><xbrldi:typedMember dimension="usg:SomeAxis"><x>1</x></xbrldi:typedMember>'
        '</xbrli:segment></xbrli:entity><xbrli:period><xbrli:instant>2026-10-10</xbrli:instant>'
        '</xbrli:period></xbrli:context>'
    ]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "t", "42")]
    assert xs.parse_instance(instance(ctxs, facts)) == {}


def test_scale_and_sign_attributes_applied():
    ctx = [inst_ctx("i", "2026-10-10")]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "i", "1.5", ' scale="6"')]
    assert xs.parse_instance(instance(ctx, facts))[None]["shares"] == 1_500_000


def test_malformed_xml_raises():
    with pytest.raises(Exception):
        xs.parse_instance("<xbrli:xbrl><unclosed>")


# ---------------------------------------------------------------------------
# find_instance_url / fetch_instance（快取）
# ---------------------------------------------------------------------------
IDX = "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000010/index.json"


def _idx(*names):
    return {"directory": {"item": [{"name": n} for n in names]}}


def test_find_instance_prefers_htm_xml(tmp_path):
    sec = FakeSec(tmp_path, {}, {IDX: _idx("FilingSummary.xml", "b_pre.xml", "brk-20260930.xml",
                                           "brk-20260930_htm.xml", "q.htm")})
    url = xs.find_instance_url(sec, "1067983", "0001067983-26-000010")
    assert url == "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000010/brk-20260930_htm.xml"


def test_find_instance_falls_back_to_plain_xml(tmp_path):
    sec = FakeSec(tmp_path, {}, {IDX: _idx("FilingSummary.xml", "b_cal.xml", "b_def.xml",
                                           "b_lab.xml", "b_pre.xml", "brk-20260930.xml")})
    assert xs.find_instance_url(sec, "1067983", "0001067983-26-000010").endswith("/brk-20260930.xml")


def test_find_instance_none_when_no_instance(tmp_path):
    sec = FakeSec(tmp_path, {}, {IDX: _idx("FilingSummary.xml", "b_pre.xml", "q.htm")})
    assert xs.find_instance_url(sec, "1067983", "0001067983-26-000010") is None


def test_fetch_instance_caches_and_skips_network(tmp_path):
    acc = "0001067983-26-000010"
    inst_url = "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000010/x_htm.xml"
    sec = FakeSec(tmp_path, {}, {IDX: _idx("x_htm.xml"), inst_url: "<xbrli:xbrl/>"})
    filing = {"accessionNumber": acc}
    assert xs.fetch_instance(sec, "1067983", filing) == "<xbrli:xbrl/>"
    assert (tmp_path / "xbrl" / f"{acc}.xml").exists()
    n = len(sec.http.calls)
    assert xs.fetch_instance(sec, "1067983", filing) == "<xbrli:xbrl/>"
    assert len(sec.http.calls) == n  # 第二次走快取


def test_fetch_instance_missing_instance_raises(tmp_path):
    sec = FakeSec(tmp_path, {}, {IDX: _idx("q.htm")})
    with pytest.raises(xs.XbrlError):
        xs.fetch_instance(sec, "1067983", {"accessionNumber": "0001067983-26-000010"})


def test_only_sec_urls_allowed():
    with pytest.raises(ValueError):
        xs._require_sec_url("https://evil.example.com/x.xml")
    xs._require_sec_url("https://www.sec.gov/Archives/x.xml")


# ---------------------------------------------------------------------------
# fill_missing_shares
# ---------------------------------------------------------------------------
B_CIK = "1067983"
B_ACC_10Q = "0001067983-26-000011"
B_ACC_10K = "0001067983-26-000005"
ASOF = "2026-10-16"


def _routes_for(cik: str, acc: str, xml: str, inst_name="brk_htm.xml") -> dict:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
    return {base + "index.json": _idx(inst_name), base + inst_name: xml}


def _filings():
    return {B_CIK: [
        {"form": "10-K", "filingDate": "2026-02-20", "accessionNumber": B_ACC_10K,
         "primaryDocument": "k.htm"},
        {"form": "10-Q", "filingDate": "2026-08-05", "accessionNumber": B_ACC_10Q,
         "primaryDocument": "q.htm"},
        # asof 之後的申報必須被排除
        {"form": "10-Q", "filingDate": "2026-10-20", "accessionNumber": "0001067983-26-000099",
         "primaryDocument": "future.htm"},
    ]}


SINGLE_CIK = "1403161"
SINGLE_ACC = "0000014031-26-000007"


def _single_xml() -> str:
    """單一類別（無維度）公司：1,000,000 股。"""
    return instance([inst_ctx("i", "2026-07-20")],
                     [fact("dei:EntityCommonStockSharesOutstanding", "i", "1000000")])


def _make_sec(tmp_path, extra_routes=None, extra_filings=None):
    routes = {}
    routes.update(_routes_for(B_CIK, B_ACC_10Q, two_class_xml()))
    routes.update(_routes_for(B_CIK, B_ACC_10K, two_class_xml(eps_a="3000", eps_b="2")))
    routes.update(_routes_for(SINGLE_CIK, SINGLE_ACC, _single_xml()))
    routes.update(extra_routes or {})
    filings = _filings()
    filings[SINGLE_CIK] = [{"form": "10-Q", "filingDate": "2026-07-30",
                            "accessionNumber": SINGLE_ACC, "primaryDocument": "s.htm"}]
    filings.update(extra_filings or {})
    return FakeSec(tmp_path, filings, routes)


def _feats(rows):
    return pd.DataFrame(rows)


def test_fill_filled_and_rejected_and_skips(tmp_path):
    feats = _feats([
        {"ticker": "BRK.B", "cik": B_CIK, "shares_cover": np.nan, "shares": np.nan,
         "net_income": 1e10, "data_notes": "原備註"},
        # 同一公司、價格拉高 → P/E 超出 200 → rejected 不寫值
        {"ticker": "HIGH", "cik": SINGLE_CIK, "shares_cover": np.nan, "shares": np.nan,
         "net_income": 1e10, "data_notes": np.nan},
        # 已有股數 → 不處理（不出現在報告）
        {"ticker": "HAS", "cik": B_CIK, "shares_cover": 5.0, "shares": np.nan,
         "net_income": 1e10, "data_notes": ""},
        # 無 cik → 不處理
        {"ticker": "NOCIK", "cik": np.nan, "shares_cover": np.nan, "shares": np.nan,
         "net_income": 1e10, "data_notes": ""},
        # 淨利為負 → 不檢查 P/E，仍需換算成功
        {"ticker": "LOSS", "cik": SINGLE_CIK, "shares_cover": np.nan, "shares": np.nan,
         "net_income": -5e8, "data_notes": ""},
    ])
    # HIGH：1,000,000 股 × 價 1,000 萬 ÷ 淨利 100 億 = P/E 1000 → 超出範圍
    prices = pd.DataFrame({"ticker": ["BRK.B", "HIGH", "HAS", "LOSS"],
                           "price": [100_000.0, 10_000_000.0, 1.0, 100_000.0]})
    snapshot = feats.copy(deep=True)
    sec = _make_sec(tmp_path)
    out, report = xs.fill_missing_shares(feats, prices, sec, ASOF)

    # 不改原物件
    pd.testing.assert_frame_equal(feats, snapshot)

    by = {r["ticker"]: r for r in report}
    assert set(by) == {"BRK.B", "HIGH", "LOSS"}
    assert by["BRK.B"]["result"] == "filled"
    assert "P/E" in by["BRK.B"]["detail"]
    assert by["HIGH"]["result"] == "rejected"
    assert "P/E" in by["HIGH"]["detail"]
    assert by["LOSS"]["result"] == "filled"
    assert "未檢查 P/E" in by["LOSS"]["detail"]

    row = out.set_index("ticker")
    assert row.loc["BRK.B", "shares_cover"] == pytest.approx(1_750_000)
    assert row.loc["BRK.B", "shares_cover_filed"] == "2026-08-05"  # 未採用 asof 之後的申報
    assert row.loc["BRK.B", "data_notes"] == "原備註；股數＝XBRL 分類別換算（EPS 比例；10-Q 2026-08-05）"
    assert "1000" in by["HIGH"]["detail"]  # P/E 寫在說明中
    assert pd.isna(row.loc["HIGH", "shares_cover"])
    assert pd.isna(row.loc["HIGH", "shares_cover_filed"])
    assert row.loc["HIGH", "data_notes"] == "多類股股數換算被拒：換算結果未通過合理性檢查"  # 寫原因、不含 P/E（P39）
    assert row.loc["LOSS", "shares_cover"] == 1_000_000
    assert row.loc["HAS", "shares_cover"] == 5.0
    assert "NOCIK" in set(out["ticker"])


def test_pe_out_of_range_low_rejected(tmp_path):
    feats = _feats([{"ticker": "BRK.B", "cik": B_CIK, "shares_cover": np.nan, "shares": np.nan,
                     "net_income": 1e12, "data_notes": ""}])
    prices = pd.DataFrame({"ticker": ["BRK.B"], "price": [100.0]})  # P/E ≈ 0.00018
    out, report = xs.fill_missing_shares(feats, prices, _make_sec(tmp_path), ASOF)
    assert report[0]["result"] == "rejected"
    assert pd.isna(out.loc[0, "shares_cover"])


def _brk_feats():
    return _feats([{"ticker": "BRK.B", "cik": B_CIK, "shares_cover": np.nan, "shares": np.nan,
                    "net_income": 1e10, "data_notes": ""}])


def test_ratio_table_used_when_listed(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("ticker,member,ratio,evidence\nBRK.B,ClassAMember,1000,測試\nBRK.B,ClassBMember,1,測試\n",
                 encoding="utf-8")
    prices = pd.DataFrame({"ticker": ["BRK.B"], "price": [100_000.0]})
    out, report = xs.fill_missing_shares(_brk_feats(), prices, _make_sec(tmp_path), ASOF, f)
    assert report[0]["result"] == "ratio"
    assert out.loc[0, "shares_cover"] == 500 * 1000 + 1_000_000          # 用比例表，不用 EPS 比 1500
    assert "類別比例表" in out.loc[0, "data_notes"]


def test_ratio_table_missing_class_rejected(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("ticker,member,ratio,evidence\nBRK.B,ClassBMember,1,只列一類\n", encoding="utf-8")
    prices = pd.DataFrame({"ticker": ["BRK.B"], "price": [100_000.0]})
    out, report = xs.fill_missing_shares(_brk_feats(), prices, _make_sec(tmp_path), ASOF, f)
    assert report[0]["result"] == "rejected"                              # 表外新類別 → 不換算
    assert pd.isna(out.loc[0, "shares_cover"])


def test_ratio_table_requires_evidence_and_valid_ratio(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("ticker,member,ratio,evidence\nBRK.B,ClassBMember,1,\n", encoding="utf-8")
    with pytest.raises(ValueError):
        xs.load_ratios(f)
    f.write_text("ticker,member,ratio,evidence\nBRK.B,ClassBMember,-1,x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        xs.load_ratios(f)


def test_packaged_ratio_table_loads_and_user_file_replaces_ticker(tmp_path):
    base = xs.load_ratios()
    assert base["ARES"]["CommonClassBMember"] == 0 and base["STZ"]["ConvertibleCommonStockMember"] == 1
    f = tmp_path / "r.csv"
    f.write_text("ticker,member,ratio,evidence\nSTZ,CommonClassAMember,1,x\n", encoding="utf-8")
    merged = xs.load_ratios(f)
    assert merged["STZ"] == {"CommonClassAMember": 1.0} and "ARES" in merged


def test_one_failure_does_not_stop_others(tmp_path):
    feats = _feats([
        {"ticker": "BROKEN", "cik": "9999999", "shares_cover": np.nan, "shares": np.nan,
         "net_income": 1e10, "data_notes": ""},   # 無申報、無路由 → 例外或無申報
        {"ticker": "BRK.B", "cik": B_CIK, "shares_cover": np.nan, "shares": np.nan,
         "net_income": 1e10, "data_notes": ""},
    ])
    prices = pd.DataFrame({"ticker": ["BRK.B", "BROKEN"], "price": [100_000.0, 1.0]})
    out, report = xs.fill_missing_shares(feats, prices, _make_sec(tmp_path), ASOF)
    by = {r["ticker"]: r for r in report}
    assert by["BROKEN"]["result"] == "failed"
    assert by["BRK.B"]["result"] == "filled"
    assert out.set_index("ticker").loc["BRK.B", "shares_cover"] == pytest.approx(1_750_000)


def test_missing_price_with_positive_income_is_rejected(tmp_path):
    feats = _feats([{"ticker": "BRK.B", "cik": B_CIK, "shares_cover": np.nan, "shares": np.nan,
                     "net_income": 1e10, "data_notes": ""}])
    out, report = xs.fill_missing_shares(feats, pd.DataFrame({"ticker": [], "price": []}),
                                         _make_sec(tmp_path), ASOF)
    assert report[0]["result"] == "rejected"
    assert pd.isna(out.loc[0, "shares_cover"])


def test_unconvertible_is_rejected_not_filled(tmp_path):
    # 兩類 EPS 不同，且找不到基準 → equivalent_shares 回 None → rejected
    xml = two_class_xml(eps_a="3000", eps_b="2").replace("TradingSymbol", "TradingSymbolX")
    routes = _routes_for(B_CIK, B_ACC_10Q, xml)
    sec = FakeSec(tmp_path, _filings(), routes)
    feats = _feats([{"ticker": "ZZZ", "cik": B_CIK, "shares_cover": np.nan, "shares": np.nan,
                     "net_income": 1e10, "data_notes": ""}])
    out, report = xs.fill_missing_shares(feats, pd.DataFrame({"ticker": ["ZZZ"], "price": [1.0]}),
                                         sec, ASOF)
    assert report[0]["result"] == "rejected"
    assert pd.isna(out.loc[0, "shares_cover"])


def test_no_override_file_is_fine(tmp_path):
    feats = _feats([{"ticker": "BRK.B", "cik": B_CIK, "shares_cover": np.nan, "shares": np.nan,
                     "net_income": 1e10, "data_notes": ""}])
    prices = pd.DataFrame({"ticker": ["BRK.B"], "price": [100_000.0]})
    _, report = xs.fill_missing_shares(feats, prices, _make_sec(tmp_path), ASOF,
                                       tmp_path / "nope.csv")
    assert report[0]["result"] == "filled"


def test_math_helpers_sanity():
    assert xs._norm("brk/b") == "BRK.B"
    assert xs._norm("BRK-B") == xs._norm("BRK.B") == xs._norm("BRK B") == "BRK.B"
    assert math.isfinite(xs._to_float("1.5"))
    assert xs._to_float("nan") is None


def test_class_missing_shares_while_other_has_shares_is_none():
    # A 類有代號與 EPS 但無股數、B 類有股數：總數會少算 A 類 → 拒絕
    ctxs = [inst_ctx("iB", "2026-10-10", [(CLS, "brk:ClassBMember")]),
            dur_ctx("dA", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassAMember")]),
            dur_ctx("dB", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "iB", "1000000"),
             fact("dei:TradingSymbol", "iB", "BRK.B"),
             fact("dei:TradingSymbol", "dA", "BRK.A"),
             fact("us-gaap:EarningsPerShareDiluted", "dA", "3000"),
             fact("us-gaap:EarningsPerShareDiluted", "dB", "2")]
    p = xs.parse_instance(instance(ctxs, facts))
    assert p["ClassAMember"]["shares"] is None
    assert xs.equivalent_shares(p, "BRK.B") is None


def test_conflicting_shares_in_one_class_blocks_other_class_only_result():
    # 回歸測試：A 類股數衝突時，不可退化成只回 B 類股數
    p = parsed_two_class()
    p["ClassAMember"]["conflict"] = ["shares"]
    p["ClassAMember"]["shares"] = None
    assert xs.equivalent_shares(p, "BRK.B") is None


def test_total_plus_class_breakdown_uses_classes_only():
    # 同時有無維度總數與各類別時，以類別為準，不重複加總
    ctxs = [inst_ctx("tot", "2026-10-10"),
            inst_ctx("iA", "2026-10-10", [(CLS, "brk:ClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "brk:ClassBMember")]),
            dur_ctx("dA", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassAMember")]),
            dur_ctx("dB", "2026-01-01", "2026-09-30", [(CLS, "brk:ClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "tot", "1750000"),
             fact("dei:EntityCommonStockSharesOutstanding", "iA", "500"),
             fact("dei:TradingSymbol", "iA", "BRK.A"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "1000000"),
             fact("dei:TradingSymbol", "iB", "BRK.B"),
             fact("us-gaap:EarningsPerShareDiluted", "dA", "3000"),
             fact("us-gaap:EarningsPerShareDiluted", "dB", "2")]
    p = xs.parse_instance(instance(ctxs, facts))
    assert xs.equivalent_shares(p, "BRK.B")[0] == 1_750_000


def test_symbol_only_debt_member_is_not_a_class():
    # 回歸測試（實測 V）：債券 context 也帶類別軸且有代號，但沒有股數與 EPS，不可視為類別
    xml = two_class_xml().replace(
        "</xbrli:xbrl>",
        inst_ctx("iN", "2026-10-10", [(CLS, "brk:Notes29Member")])
        + fact("dei:TradingSymbol", "iN", "BRK29") + "</xbrli:xbrl>",
    )
    p = xs.parse_instance(xml)
    assert p["Notes29Member"]["shares"] is None and p["Notes29Member"]["eps"] is None
    assert xs.equivalent_shares(p, "BRK.B")[0] == 1_750_000


# ---------------------------------------------------------------------------
# 主代理檢核補充：Equivalent／Common 類別對應、無維度代號、同期間 EPS
# ---------------------------------------------------------------------------
def test_equivalent_member_eps_maps_to_common_member_shares():
    """EPS 掛 EquivalentClassX、股數與代號掛 CommonClassX → 依類別字母對應。"""
    ctxs = [inst_ctx("iA", "2026-10-10", [(CLS, "brk:CommonClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "brk:CommonClassBMember")]),
            dur_ctx("dA", "2026-01-01", "2026-06-30", [(CLS, "brk:EquivalentClassAMember")]),
            dur_ctx("dB", "2026-01-01", "2026-06-30", [(CLS, "brk:EquivalentClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "iA", "500"),
             fact("dei:TradingSymbol", "iA", "BRK.A"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "1000000"),
             fact("dei:TradingSymbol", "iB", "BRK.B"),
             fact("us-gaap:EarningsPerShareDiluted", "dA", "3000"),
             fact("us-gaap:EarningsPerShareDiluted", "dB", "2")]
    total, _ = xs.equivalent_shares(xs.parse_instance(instance(ctxs, facts)), "BRK.B")
    assert total == 500 * 1500 + 1_000_000


def _undim_symbol_xml(generic="us:CommonStockMember"):
    ctxs = [inst_ctx("i0", "2026-10-10"),
            inst_ctx("iC", "2026-10-10", [(CLS, generic)]),
            inst_ctx("iB", "2026-10-10", [(CLS, "us:CommonClassBMember")]),
            dur_ctx("dC", "2026-01-01", "2026-06-30", [(CLS, generic)]),
            dur_ctx("dB", "2026-01-01", "2026-06-30", [(CLS, "us:CommonClassBMember")])]
    facts = [fact("dei:TradingSymbol", "i0", "HSY"),
             fact("dei:EntityCommonStockSharesOutstanding", "iC", "1000"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "400"),
             fact("us-gaap:EarningsPerShareDiluted", "dC", "4"),
             fact("us-gaap:EarningsPerShareDiluted", "dB", "3")]
    return instance(ctxs, facts)


def test_undimensioned_symbol_uses_generic_common_stock_as_base():
    total, _ = xs.equivalent_shares(xs.parse_instance(_undim_symbol_xml()), "HSY")
    assert total == 1000 + 400 * 3 / 4


def test_undimensioned_symbol_without_generic_member_rejected():
    assert xs.equivalent_shares(xs.parse_instance(_undim_symbol_xml("us:CommonClassAMember")), "HSY") is None


def test_eps_ratio_uses_common_period_only():
    """A 類有 Q3 YTD 與 H1，B 類只有 H1 → 比例須用共同的 H1，不可拿 A 的 Q3 對 B 的 H1。"""
    ctxs = [inst_ctx("iA", "2026-10-10", [(CLS, "x:ClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "x:ClassBMember")]),
            dur_ctx("hA", "2026-01-01", "2026-06-30", [(CLS, "x:ClassAMember")]),
            dur_ctx("qA", "2026-01-01", "2026-09-30", [(CLS, "x:ClassAMember")]),
            dur_ctx("hB", "2026-01-01", "2026-06-30", [(CLS, "x:ClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "iA", "10"),
             fact("dei:TradingSymbol", "iA", "X.A"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "100"),
             fact("dei:TradingSymbol", "iB", "X.B"),
             fact("us-gaap:EarningsPerShareDiluted", "hA", "20"),
             fact("us-gaap:EarningsPerShareDiluted", "qA", "33"),
             fact("us-gaap:EarningsPerShareDiluted", "hB", "2")]
    total, desc = xs.equivalent_shares(xs.parse_instance(instance(ctxs, facts)), "X.B")
    assert total == 10 * 10 + 100 and "2026-06-30" in desc


def test_no_common_eps_period_rejected():
    ctxs = [inst_ctx("iA", "2026-10-10", [(CLS, "x:ClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "x:ClassBMember")]),
            dur_ctx("qA", "2026-01-01", "2026-09-30", [(CLS, "x:ClassAMember")]),
            dur_ctx("hB", "2026-01-01", "2026-06-30", [(CLS, "x:ClassBMember")])]
    facts = [fact("dei:EntityCommonStockSharesOutstanding", "iA", "10"),
             fact("dei:TradingSymbol", "iA", "X.A"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "100"),
             fact("dei:TradingSymbol", "iB", "X.B"),
             fact("us-gaap:EarningsPerShareDiluted", "qA", "33"),
             fact("us-gaap:EarningsPerShareDiluted", "hB", "2")]
    assert xs.equivalent_shares(xs.parse_instance(instance(ctxs, facts)), "X.B") is None


def test_title_maps_undimensioned_symbol_to_class():
    """代號在無維度 context、證券名稱寫 Class A、無一般普通股成員 → 以 ClassA 成員為基準（基本 EPS 比例）。"""
    ctxs = [inst_ctx("i0", "2026-10-10"),
            inst_ctx("iA", "2026-10-10", [(CLS, "x:CommonClassAMember")]),
            inst_ctx("iB", "2026-10-10", [(CLS, "x:CommonClassBMember")]),
            dur_ctx("dA", "2026-01-01", "2026-06-30", [(CLS, "x:CommonClassAMember")]),
            dur_ctx("dB", "2026-01-01", "2026-06-30", [(CLS, "x:CommonClassBMember")])]
    facts = [fact("dei:TradingSymbol", "i0", "ERIE"),
             fact("dei:Security12bTitle", "i0", "Class A common stock, stated value $0.0292 per share"),
             fact("dei:EntityCommonStockSharesOutstanding", "iA", "1000"),
             fact("dei:EntityCommonStockSharesOutstanding", "iB", "2"),
             fact("us-gaap:EarningsPerShareBasic", "dA", "6"),
             fact("us-gaap:EarningsPerShareBasic", "dB", "900"),
             fact("us-gaap:EarningsPerShareDiluted", "dA", "5"),     # 稀釋假設 B 已轉換，不可用於比例
             fact("us-gaap:EarningsPerShareDiluted", "dB", "899")]
    total, _ = xs.equivalent_shares(xs.parse_instance(instance(ctxs, facts)), "ERIE")
    assert total == 1000 + 2 * 900 / 6


def test_detect_successions_writes_anomaly_once(state_dir):
    from trader import db
    from trader.data.inputs import detect_successions
    from trader.paths import get_paths

    class S:
        def predecessors(self, cik):
            return ["0000000001"] if cik == "0000000002" else []

        def succession_filings(self, cik, asof):
            return [{"form": "8-K12B", "filingDate": "2026-07-01", "url": "https://www.sec.gov/x"}] \
                if cik in ("0000000003", "0000000002") else []

    feats = pd.DataFrame([
        {"ticker": "NEW", "cik": "3", "fy_last": np.nan},       # 疑似改組 → 報
        {"ticker": "DONE", "cik": "2", "fy_last": np.nan},      # 已登記前身 → 不報
        {"ticker": "SPIN", "cik": "4", "fy_last": np.nan},      # 無承繼申報 → 不報
        {"ticker": "OK", "cik": "3", "fy_last": 2025},          # 有財報 → 不報
    ])
    conn = db.connect(get_paths().ensure().db)
    db.migrate(conn)
    assert [x["ticker"] for x in detect_successions(S(), feats, ASOF, conn)] == ["NEW"]
    detect_successions(S(), feats, ASOF, conn)
    rows = conn.execute("SELECT ticker, kind FROM anomalies").fetchall()
    assert [(r["ticker"], r["kind"]) for r in rows] == [("NEW", "cik_succession")]
