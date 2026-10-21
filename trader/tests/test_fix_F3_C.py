"""F3-C（A3 第三輪 V3-2／V3-3）修正的回歸測試：每條行為修正至少一個「修前失敗→修後通過」案例。
G5 價格盲（M4D-01..06）、G5 SEC 抓取（M4C-01／02、M4E-02）、G7 資料包組裝（M4A-05..08、M4B-01、M4C-03／04）、
G8 同業（M4A-01..04、M4B-02、M4E-01）、G-review-backfill（M7D-04、X1-03）。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from trader.analysis import dossier_prep as dp
from trader.analysis import insiders as I
from trader.analysis import peers as P
from trader.data import sec as S
from trader.data.http import DataError, SecBlocked

from test_dossier_prep import CIK, FakeSec, _f, _facts
from test_insiders import FakeSec as InsSec
from test_peers import FakePeerSec
from test_review_pipeline import ASOF as R_ASOF
from test_review_pipeline import FakeAlpaca, _clients, _offline, _seed, env  # noqa: F401（fixture）
from test_sec import UA, make_http

ASOF = "2026-10-15"
PEER_ASOF = "2026-10-16"


def _clean(src: str) -> str:
    return dp.sanitize_filing_text(src)[0]


class _KwPeerSec(FakePeerSec):
    """同真實 SecClient.submissions 介面（max_age_hours、asof）；記錄每次呼叫。"""
    def __init__(self):
        super().__init__()
        self.sub_calls: list[tuple] = []

    def submissions(self, cik, max_age_hours=None, asof=None):
        self.sub_calls.append((cik, max_age_hours))
        return {"sic": self.sic[cik]}


# ============================================================ G5 價格盲（dossier_prep 價格句／每股金額／增資遮蔽）

def test_m4d01_offering_proceeds_masked_shares_kept():
    """A3-M4D-01／D84：ATM／增資句的發行總額遮蔽（總額÷股數＝發行價），股數保留。"""
    src = "During 2025 we sold 2,000,000 shares of common stock under our at-the-market program for net proceeds of $118 million."
    out = _clean(src)
    assert "$118 million" not in out and "118" not in out
    assert "2,000,000 shares" in out and dp.OFFERING_AMOUNT_MASK in out


def test_m4d01_debt_offering_without_shares_untouched():
    """純債券公開發行（句中無股份字樣）不遮：發行價無從推得。"""
    src = "In March 2025 we completed a public offering of $500 million of 5.25% senior notes due 2035."
    assert "$500 million" in _clean(src)


def test_m4d01_offering_sentence_recorded_in_price_json(tmp_path):
    """D84：被遮的增資句原文（含總額）只寫進同工作的價格檔 offering_proceeds。"""
    sent = "In 2025 we issued 3,000,000 shares in a registered direct offering for gross proceeds of $75.0 million."
    fl = [_f("10-K", "2026-02-20", 5)]
    (tmp_path / "dossier").mkdir()
    pj = tmp_path / "dossier" / "BRK.B_price.json"
    pj.write_text(json.dumps({"price": 1.0}), encoding="utf-8")
    dp.fetch_sec_texts(FakeSec(filings=fl, texts={fl[0]["url"]: "Item 7. MD&A\n" + sent + "\n"}), CIK, ASOF,
                       tmp_path / "sec", notes=[])
    got = json.loads(pj.read_text(encoding="utf-8"))
    assert got["price"] == 1.0
    assert [r["sentence"] for r in got["offering_proceeds"]] == [sent]
    assert all("$75.0 million" not in p.read_text(encoding="utf-8") for p in (tmp_path / "sec").glob("*.txt"))


@pytest.mark.parametrize("head, nums", [
    ("MARKET PRICE OF COMMON STOCK AND DIVIDENDS", ["45.10", "40.20", "47.30", "41.15"]),
    ("Stock Price Information", ["45.10", "40.20", "47.30", "41.15"]),
])
def test_m4d02_ex13_quarterly_price_table_masked(head, nums):
    """A3-M4D-02：EX-13 無 Item 5 標題的季度股價表（含／不含股利欄）所有金額遮蔽，不受 PER_SHARE_KEEP 否決。"""
    src = (f"{head}\nQuarter\tHigh\tLow\tDividends\nFirst\t$45.10\t$40.20\t$0.50\nSecond\t47.30\t41.15\t0.50\n\n"
           "Net sales increased 5% in 2025 to $1,234 million.")
    out = _clean(src)
    assert all(n not in out for n in nums), out
    assert "$1,234 million" in out      # 表外的一般金額不動


@pytest.mark.parametrize("src, gone", [
    ("On February 10, 2026, our common stock closed at $123.45 on the New York Stock Exchange.", "123.45"),
    ("Our Class A shares traded at $50.25 on the record date.", "50.25"),
    ("The last reported sale price of our common stock on the Nasdaq on February 1, 2026 was 88.10.", "88.10"),
    ("本公司普通股收在 123.4 美元。", "123.4"),
])
def test_m4d03_close_traded_last_sale_sentences_removed(src, gone):
    """A3-M4D-03：closed／traded at＋金額、last reported sale price、收在 X 美元。"""
    assert gone not in _clean(src)


@pytest.mark.parametrize("src, gone", [
    ("The total grant-date fair value of RSUs granted in 2025 was $12.3 million.", "12.3"),
    ("The total fair value of shares vested during 2025 was $45.6 million.", "45.6"),
    ("2025 年既得股份之公允價值為 3.2 億美元。", "3.2"),
])
def test_m4d04_total_grant_and_vested_fair_value_removed(src, gone):
    """A3-M4D-04：授予／既得總公允價值（÷股數＝股價）。"""
    assert gone not in _clean(src)


def test_m4d04_fair_value_policy_sentence_kept():
    src = "Grant-date fair value is recognized as expense over the vesting period."
    assert "vesting period" in _clean(src)


def test_m4d05_conversion_rate_removed():
    """A3-M4D-05：轉換比率（每 $1,000 面額換 N 股 → 轉換價＝1000/N）。"""
    src = ("The notes are convertible at an initial conversion rate of 12.3456 shares of common stock "
           "per $1,000 principal amount of notes.")
    assert "12.3456" not in _clean(src)


@pytest.mark.parametrize("src, gone", [
    ("Our dividend yield2.1% compares favorably with peers.", "yield2.1%"),
    ("Free cash flow yield7.5% for 2025.", "yield7.5%"),
    ("In 2025 we withheld 12,345 shares with a value of $1.2 million to satisfy tax withholding obligations.", "$1.2 million"),
    ("Each outstanding share was converted into the right to receive $52.00 in cash.", "52.00"),
])
def test_m4d06_glued_yield_withheld_and_right_to_receive(src, gone):
    """A3-M4D-06：yield 黏字、扣股繳稅股數＋價值、併購每股對價 right to receive $X。"""
    assert gone not in _clean(src)


# ============================================================ G5 SEC 抓取

ACC = "0001067983-26-000005"
FOLDER = "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000005/"
INDEX_URL = FOLDER + ACC + "-index.htm"
SGML_URL = FOLDER + ACC + ".txt"


def _index(rows):
    trs = "".join(f'<tr><td>{i}</td><td>{d}</td><td><a href="/Archives/edgar/data/1067983/000106798326000005/{doc}">'
                  f"{doc}</a></td><td>{typ}</td><td>100</td></tr>" for i, (d, doc, typ) in enumerate(rows, 1))
    return f"<table><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>{trs}</table>"


def test_m4c01_no_ex13_in_index_no_full_sgml(tmp_path):
    """A3-M4C-01：索引頁沒有 EX-13 → 回空，不下載整份 SGML。"""
    calls: list[str] = []
    routes = {INDEX_URL: _index([("10-K", "k.htm", "10-K"), ("Subsidiaries", "ex21.htm", "EX-21")])}
    c = S.SecClient(UA, tmp_path, http=make_http(routes, calls))
    assert c.exhibit_texts(CIK, ACC, "EX-13") == []
    assert SGML_URL not in calls and calls == [INDEX_URL]
    assert c.exhibit_texts(CIK, ACC, "EX-13") == [] and calls == [INDEX_URL]   # 索引頁快取


def test_m4c01_ex13_fetched_alone_via_index(tmp_path):
    calls: list[str] = []
    routes = {INDEX_URL: _index([("10-K", "k.htm", "10-K"), ("Annual report", "ex13.htm", "EX-13")]),
              FOLDER + "ex13.htm": "<html><p>Management discussion body</p></html>"}
    c = S.SecClient(UA, tmp_path, http=make_http(routes, calls))
    got = c.exhibit_texts(CIK, ACC, "EX-13")
    assert [t for t, _ in got] == ["EX-13"] and "Management discussion body" in got[0][1]
    assert SGML_URL not in calls and FOLDER + "k.htm" not in calls


def test_m4c02_peers_sic_uses_long_max_age():
    """A3-M4C-02：同業取 SIC 的 submissions 用長新鮮期（不隨每個新決策日重抓約 500 家）。"""
    sec = _KwPeerSec()
    P.build_peer_section(sec, "SELF", "1", PEER_ASOF, list(sec.cik), None, "/tmp/_unused_peer_dir", {})
    pool = [(c, a) for c, a in sec.sub_calls if c != "1"]
    assert pool and all(a == 24 * 30 for _, a in pool), sec.sub_calls


def test_m4c02_old_submission_pages_not_refetched_per_asof(tmp_path):
    """A3-M4C-02：submissions 舊分頁（封存的歷史申報）快取不隨新決策日重抓。"""
    subs_url = f"https://data.sec.gov/submissions/CIK{CIK}.json"
    page = f"CIK{CIK}-submissions-001.json"
    page_url = f"https://data.sec.gov/submissions/{page}"
    cols = {"reportDate": [""], "items": [""], "primaryDocument": ["k.htm"]}
    routes = {subs_url: {"sic": "6331", "filings": {
        "recent": {"form": ["10-K"], "filingDate": ["2025-02-20"], "accessionNumber": ["0001067983-25-000003"], **cols},
        "files": [{"name": page, "filingTo": "2024-12-31"}]}},
        page_url: {"form": ["10-K"], "filingDate": ["2024-02-20"], "accessionNumber": ["0001067983-24-000002"], **cols}}
    calls: list[str] = []
    c = S.SecClient(UA, tmp_path, http=make_http(routes, calls))
    assert len(c.filings(CIK, "2026-10-01", since="2020-01-01")) == 2
    (tmp_path / f"{page}.meta").write_text(json.dumps({"fetched_ny_date": "2026-01-01"}), encoding="utf-8")
    calls.clear()
    assert len(c.filings(CIK, "2026-10-08", since="2020-01-01")) == 2
    assert page_url not in calls, calls


def test_m4e02_xbrl_shares_goes_through_sec_request(tmp_path):
    """A3-M4E-02：xbrl_shares 經 SecClient._request（SEC 封鎖起算冷卻），不直接 http.get。"""
    from trader.data import xbrl_shares as X

    class Resp:
        def json(self):
            return {"directory": {"item": [{"name": "abc_htm.xml"}]}}

    class Http:
        def get(self, url):
            return Resp()

    class Sec:
        cache_dir = tmp_path
        http = Http()
        req: list[str] = []

        def _request(self, fn, url):
            self.req.append(url)
            return fn(url)

    s = Sec()
    url = X.find_instance_url(s, 1067983, ACC)
    assert url and url.endswith("abc_htm.xml")
    assert s.req == [FOLDER + "index.json"]


# ============================================================ G7 資料包組裝

def test_m4a05_cover_shares_missing_in_status(tmp_path):
    """A3-M4A-05：封面股數缺 → 資料狀態不得寫「正常」。"""
    res = dp.build_dossier_base(FakeSec(facts=_facts()), "BRK.B", ASOF, tmp_path / "o", price=None,
                                price_date=None, rates={"error": "DataError: x"})
    head = res["md"].read_text(encoding="utf-8").split("## 10 年財報")[0]
    assert "缺：最新封面流通股數（決策日前 400 天內無封面股數）" in head and "正常：" not in head


def test_m4a05_cover_shares_exception_in_status(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(dp, "cover_shares_section", boom)
    res = dp.build_dossier_base(FakeSec(facts=_facts()), "BRK.B", ASOF, tmp_path / "o", price=None,
                                price_date=None, rates={"error": "DataError: x"})
    head = res["md"].read_text(encoding="utf-8").split("## 10 年財報")[0]
    assert "缺：最新封面流通股數（框架讀取失敗，RuntimeError）" in head


def test_m4a06_old_def14a_beats_later_corporate_action_14c():
    """A3-M4A-06：15 個月內無 DEF 14A（年度委託書晚了），稍舊的 DEF 14A 不被其後的公司行動 DEF 14C 擠掉。
    （ERIE 型：14C 申報時 DEF 14A 已逾 15 個月 → 仍取 14C，見 test_fix_RM4_1）"""
    fl = [_f("10-K", "2026-02-20", 5), _f("DEF 14A", "2025-06-01", 7), _f("DEF 14C", "2026-03-01", 8)]
    got = dp._pick_filings(FakeSec(filings=fl, texts={f["url"]: "X" for f in fl}), CIK, ASOF, 3)
    assert [f["form"] for f in got["DEF 14A"]] == ["DEF 14A"]
    assert [f["form"] for f in got["proxy_skipped"]] == ["DEF 14C"]


def test_m4a07_stock_dividend_in_splits_string():
    """A3-M4A-07：Alpaca 股票股利（stock_dividend）與 Yahoo 同一事件（1.1 判為拆股）口徑一致，進拆股字串。"""
    from trader.data.prices_adapter import splits_string
    acts = [{"ticker": "XYZ", "type": "stock_dividend", "ex_date": "2026-05-01", "ratio_new_per_old": 1.1}]
    assert splits_string(acts, "XYZ", ASOF) == "2026-05-01:1.1"


def test_m4a07_yahoo_small_stock_dividend_needs_human():
    """A3-M4A-07：Yahoo 係數介於 1 與 1.1 且非簡單比例（5% 股票股利 1.05）→ 標待人工核對、不乘股數。"""
    from test_fix_P13 import _ts
    from trader.data.yahoo import YahooClient
    days = ["2026-05-29", "2026-06-01"]
    close = [100.0, 100.0]
    c = YahooClient(cache_dir=None)
    c._mem["XYZ"] = {"timestamp": [_ts(d) for d in days],
                     "indicators": {"quote": [{"open": close, "high": close, "low": close, "close": close,
                                               "volume": [1000] * 2}], "adjclose": [{"adjclose": close}]},
                     "events": {"splits": {"a": {"date": _ts("2026-06-01"), "numerator": 21.0, "denominator": 20.0,
                                                 "splitRatio": "21:20"}}}}
    acts = [a for a in c.corporate_actions(["XYZ"], "2026-01-01", ASOF) if a["ex_date"] == "2026-06-01"]
    assert acts and acts[0]["ratio_new_per_old"] is None and "needs_human" in acts[0]["raw"]


def test_m4a08_failed_8k_text_only_noted(tmp_path):
    """A3-M4A-08：單份 8-K 原文抓取失敗只記缺，不讓整個 DOSSIER 前置失敗；最新 10-K 失敗照拋。"""
    fl = [_f("10-K", "2026-02-20", 5), _f("8-K", "2026-05-01", 9)]
    notes: list = []
    out = dp.fetch_sec_texts(FakeSec(filings=fl, texts={fl[0]["url"]: "ANNUAL"}), CIK, ASOF, tmp_path / "s", notes=notes)
    assert [r["form"] for r in out] == ["10-K"]
    assert any(n.startswith("缺：8-K 2026-05-01 原文抓取失敗") for n in notes), notes
    with pytest.raises(KeyError):
        dp.fetch_sec_texts(FakeSec(filings=fl, texts={fl[1]["url"]: "8K"}), CIK, ASOF, tmp_path / "s2", notes=[])


class _MedianDownFred:
    warnings = ["DGS30：抓取失敗，沿用舊快取"]

    def t30(self, asof):
        return 0.047, "2026-10-14"

    def t30_median_20y(self, asof):
        raise RuntimeError("median down")

    def latest(self, sid, asof):
        return 3.9, "2026-10-14"

    def mean(self, sid, asof, years):
        return 4.0


def test_m4b01_median_failure_keeps_t30_and_warnings_reach_dossier():
    """A3-M4B-01：20 年中位失敗不連 t30 一起丟；中位錯誤與 FRED 警示進 RATE_KEYS 與資料包利率段。"""
    from trader.data.inputs import fetch_rates
    from trader.pipelines.analyze import RATE_KEYS
    r = fetch_rates(_MedianDownFred(), ASOF)
    assert r["t30"] == 0.047 and "error" not in r and "median down" in r["t30_20y_median_error"]
    assert {"t30_20y_median_error", "fred_warnings"} <= set(RATE_KEYS)
    md = dp.rates_section({k: r[k] for k in RATE_KEYS if k in r})
    assert "缺：30 年期 20 年中位" in md and "注意：DGS30：抓取失敗，沿用舊快取" in md


def test_m4c03_last_10k_ignores_part3_amendment():
    """A3-M4C-03：Part III 10-K/A 晚於原 10-K 時，拆股基準仍以原 10-K 申報日。"""
    facts = {"facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"form": "10-K", "filed": "2026-02-20", "val": 1}, {"form": "10-K/A", "filed": "2026-04-24", "val": 1}]}}}}}
    assert dp._last_10k_filed(facts, ASOF) == "2026-02-20"
    only_a = {"facts": {"dei": {"X": {"units": {"shares": [{"form": "10-K/A", "filed": "2026-04-24", "val": 1}]}}}}}
    assert dp._last_10k_filed(only_a, ASOF) == "2026-04-24"


def test_m4c04_exact_max_filings_not_truncated():
    """A3-M4C-04：恰好 max_filings 份不算截斷；多一份才算。"""
    _, st = I.collect(InsSec({"a1": None, "a2": None}), "1", PEER_ASOF, max_filings=2)
    assert st["truncated"] is False
    _, st = I.collect(InsSec({"a1": None, "a2": None, "a3": None}), "1", PEER_ASOF, max_filings=2)
    assert st["truncated"] is True and st["filings"] == 2


# ============================================================ G8 同業

def test_m4a01_narrow_gics_counts_named_competitors(tmp_path):
    """A3-M4A-01：GICS 組大小含點名者；組內 6 家（含本公司、點名 HD）不得因扣掉點名者而看似 ≤5 家。"""
    sec = _KwPeerSec()
    gics = {t: "Home Improvement Retail" for t in ("SELF", "HD", "A", "B", "C", "D")}
    tenk = "Item 1. Business\nCompetition\nWe compete with The Home Depot in every market.\nItem 1A. Risk"
    res = P.build_peer_section(sec, "SELF", "1", PEER_ASOF, list(sec.cik), tenk, tmp_path, gics)
    assert res["competitors"] == ["HD"] and res["narrow_gics_peers"] == []
    assert "GICS 子產業很窄" not in res["markdown"]


def test_m4a02_named_empty_wording_distinguishes_no_competition_text(tmp_path):
    """A3-M4A-02：沒有 10-K 競爭段 → 寫缺，不寫「點名公司都不在 SEC 名單」；無 GICS 時寫 SIC 四碼。"""
    sec = _KwPeerSec()
    md = P.build_peer_section(sec, "SELF", "1", PEER_ASOF, list(sec.cik), None, tmp_path, {})["markdown"]
    assert "缺：本公司 10-K 未取得或無競爭段" in md and "都不在 SEC 申報名單" not in md
    assert "同 SIC 5211 四碼涵蓋多種業務" in md and "同 GICS 子產業涵蓋" not in md


def test_m4a03_peer_metrics_missing_last_revenue():
    """A3-M4A-03：最新財年營收缺 → 營收與 5 年 CAGR 記缺，不退用舊年。"""
    ys = list(range(2016, 2026))
    df = pd.DataFrame({"net_income": [10.0] * 10, "equity": [100.0] * 10, "operating_income": [5.0] * 10,
                       "revenue": [100.0] * 9 + [float("nan")]}, index=ys)
    m = P._metrics(df, 2025)
    assert pd.isna(m["revenue"]) and pd.isna(m["rev_cagr5"]) and m["fy"] == 2025


def test_m4a04_roe_series_negative_end_equity():
    """A3-M4A-04：期初或期末權益 ≤0 → ROE 不計（與資料包 10 年表同式），平均權益 >0 也一樣。"""
    df = pd.DataFrame({"net_income": [10.0, 10.0, 10.0], "equity": [200.0, -50.0, 100.0]}, index=[2023, 2024, 2025])
    r = P.roe_series(df)
    assert pd.isna(r[2024]) and pd.isna(r[2025]) and pd.isna(r[2023])


def test_m4b02_universe_note_sources_and_header(tmp_path):
    """A3-M4B-02：override／cache_stale 不得寫成「決策日（近日）抓取的名單」；標頭不寫死 S&P 500。"""
    assert "使用者指定名單" in P.universe_note("override")
    assert "舊快取" in P.universe_note("cache_stale")
    assert "舊快取" in P.universe_note("cache_stale_not_pit")
    assert "決策日（近日）抓取" not in P.universe_note("override") + P.universe_note("cache_stale")
    sec = _KwPeerSec()
    md = P.build_peer_section(sec, "SELF", "1", PEER_ASOF, list(sec.cik), None, tmp_path, {},
                              universe_source="override")["markdown"]
    assert "S&P 500" not in md.split("\n")[1]


def test_m4e01_peers_secblocked_recorded(tmp_path):
    """A3-M4E-01：同業母體因 SEC 封鎖縮水要留痕（missing → 資料狀態）。"""
    class Blocked(_KwPeerSec):
        def submissions(self, cik, max_age_hours=None, asof=None):
            if cik in ("3", "4"):
                raise SecBlocked("blocked")
            return super().submissions(cik, max_age_hours, asof)
    sec = Blocked()
    res = P.build_peer_section(sec, "SELF", "1", PEER_ASOF, list(sec.cik), None, tmp_path, {})
    assert "缺：同業母體因 SEC 封鎖縮水 2 家" in res["missing"]
    assert "缺：同業母體因 SEC 封鎖縮水 2 家" in res["markdown"]


def test_m4e01_insiders_secblocked_not_swallowed():
    """A3-M4E-01：內部人 Form 4 抓取遇 SEC 封鎖往上拋（呼叫端寫「缺：內部人交易彙整失敗（SecBlocked）」），不吞成單份失敗。"""
    class Blk(InsSec):
        def full_submission_text(self, cik, acc):
            raise SecBlocked("blocked")
    with pytest.raises(SecBlocked):
        I.collect(Blk({"a1": "x"}), "1", PEER_ASOF)


# ============================================================ G-review-backfill（pipelines/review.py）

class _SplitAlpaca(FakeAlpaca):
    def __init__(self, closes, ex_date):
        super().__init__(closes)
        self.ex_date = ex_date
        self.ca_calls: list = []

    def corporate_actions(self, symbols, start, end, types=None):
        self.ca_calls.append((start, end))
        return [{"ticker": "TRI", "type": "forward_split", "ex_date": self.ex_date, "ratio_new_per_old": 2.0}]


def _split_clients(ex_date):
    cl = _clients()
    cl["alpaca"] = _SplitAlpaca({t: 105.0 for t in ("HLD", "FUL", "THS", "TRI", "PRC", "NON", "NOC")}, ex_date)
    return cl


def test_m7d04_backfill_skips_price_triggers_after_later_split(env, monkeypatch):
    """A3-M7D-04：補跑舊 asof、asof 之後有拆合股的檔，本次略過價格類觸發並記 note。"""
    from trader.pipelines import review as RP
    monkeypatch.setattr(RP, "ny_today", lambda: "2026-10-30", raising=False)
    cfg, paths, conn, tmp = env
    _seed(conn)
    cl = _split_clients("2026-10-20")
    s = RP.run_review(cfg, paths, conn, R_ASOF, scope="all", clients=cl, offline_filings=_offline(tmp), due_only=True)
    assert "error" not in s, s
    assert s["backfill_price_triggers_skipped"]["tickers"] == ["TRI"]
    skill_csv = pd.read_csv(paths.work / s["run_id"] / "daily_skill.csv")
    assert "TRI" not in set(skill_csv["ticker"]) and "PRC" in set(skill_csv["ticker"])


def test_m7d04_live_run_not_skipped(env, monkeypatch):
    from trader.pipelines import review as RP
    monkeypatch.setattr(RP, "ny_today", lambda: R_ASOF, raising=False)
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = RP.run_review(cfg, paths, conn, R_ASOF, scope="all", clients=_split_clients("2026-10-10"),
                      offline_filings=_offline(tmp), due_only=True)
    assert "error" not in s and "backfill_price_triggers_skipped" not in s


class _NewsDown(FakeAlpaca):
    def news(self, symbols, start, end):
        self.news_calls.append((list(symbols), start, end))
        raise DataError("alpaca news 503")


def test_x103_news_failure_degrades_not_errors(env):
    """A3-X1-03：Alpaca 新聞抓取失敗只略過新聞類觸發，不讓 review 失敗（否則全組合停買）。"""
    from trader.pipelines.review import run_review
    cfg, paths, conn, tmp = env
    _seed(conn)
    cl = _clients()
    cl["alpaca"] = _NewsDown({t: 105.0 for t in ("HLD", "FUL", "THS", "TRI", "PRC", "NON", "NOC")})
    s = run_review(cfg, paths, conn, R_ASOF, scope="all", clients=cl, offline_filings=_offline(tmp), due_only=True)
    assert "error" not in s, s
    assert "alpaca news 503" in s["news_error"]
    assert s["counts"]["FULL"] == 1 and not (paths.work / s["run_id"] / "news.json").exists()


def test_x103_news_window_floor(env):
    """A3-X1-03：新聞起點不早於 asof−NEWS_FALLBACK_DAYS（久未檢查的檔不得把窗口拉到數月前）。"""
    from datetime import date, timedelta
    from trader.pipelines import review as RP
    cfg, paths, conn, tmp = env
    _seed(conn)
    cl = _clients()
    RP.run_review(cfg, paths, conn, R_ASOF, scope="all", clients=cl, offline_filings=_offline(tmp), due_only=True)
    floor = (date.fromisoformat(R_ASOF) - timedelta(days=RP.NEWS_FALLBACK_DAYS)).isoformat()
    assert floor > "2026-09-01" and cl["alpaca"].news_calls[0][1] == floor


# ============================================================ F3-C-R 返工（A3-M4D-01..04、A3-M7D-04）

def test_m4d02r_price_table_followed_by_financial_table_kept():
    """A3-M4D-02 返工：html_to_text 一列一行、空行相隔；股價表後緊接五年財報表時，財報表數字不被外溢遮掉。"""
    src = ("MARKET PRICE OF COMMON STOCK AND DIVIDENDS\n\nQuarter\n\nHigh\n\nLow\n\nDividends\n\n"
           "First\t$45.10\t$40.20\t$0.50\n\nSecond\t47.30\t41.15\t0.50\n\n"
           "FIVE-YEAR SELECTED FINANCIAL DATA\n\n(in millions, except per share)\n\n"
           "Net sales\t$12,345\t$11,234\n\nDiluted earnings per share\t4.12\t3.87\n\nTotal assets\t$45,678\t$43,210\n")
    out = _clean(src)
    assert all(n not in out for n in ("45.10", "40.20", "47.30", "41.15")), out
    assert "$12,345" in out and "4.12" in out and "$45,678" in out, out


def test_m4d02r_price_table_row_label_ends_window():
    """股價表後直接接帶金額的財報列（無空白標題行）：列標非股價表欄名 → 視窗結束。"""
    src = ("Stock Price Information\nQuarter\tHigh\tLow\nFirst Quarter\t$45.10\t$40.20\nFourth Quarter\t$47.30\t$41.15\n"
           "Net sales\t$12,345\t$11,234\nDiluted earnings per share\t4.12\t3.87\n")
    out = _clean(src)
    assert "45.10" not in out and "41.15" not in out
    assert "$12,345" in out and "4.12" in out, out


def test_m4d02r_toc_line_does_not_open_window():
    """目錄行（表頭字樣＋結尾頁碼）不開視窗，下方金額保留。"""
    src = ("Market Price of Common Stock and Dividends 23\nSelected Financial Data 24\n\n"
           "Net sales\t$12,345\t$11,234\nDiluted earnings per share\t4.12\t3.87\n")
    out = _clean(src)
    assert "$12,345" in out and "4.12" in out, out


@pytest.mark.parametrize("src, gone, kept", [
    ("In May 2025 we completed an underwritten public offering of 10,000,000 shares of common stock. "
     "Net proceeds were approximately $1,480 million.", "1,480", "10,000,000 shares"),
    ("In 2024 we entered into an equity distribution agreement with several banks. "
     "During 2025, we issued 4,000,000 shares under the agreement for net proceeds of $212.0 million.",
     "212.0", "4,000,000 shares"),
])
def test_m4d01r_offering_proceeds_in_next_sentence_masked(src, gone, kept, tmp_path):
    """A3-M4D-01 返工：發行字樣與總額分在同段兩句 → 行層判斷，總額遮蔽並記入 offering_proceeds。"""
    out = _clean(src)
    assert gone not in out and kept in out and dp.OFFERING_AMOUNT_MASK in out, out
    sents = dp.offering_sentences(src)
    assert any(gone in s for s in sents), sents


def test_m4d01r_shares_at_price_masked():
    src = "In March 2025 we sold 5,000,000 shares of common stock at $45.00 in a registered direct offering."
    out = _clean(src)
    assert "45.00" not in out and "5,000,000 shares" in out, out


def test_m4d01r_unrelated_paragraph_proceeds_kept():
    """同段無發行字樣（只有出售業務所得）不動。"""
    src = "We sold our European business in 2025. Proceeds from the sale were $2.1 billion."
    assert "$2.1 billion" in _clean(src)


@pytest.mark.parametrize("src", [
    "Stock-based compensation expense, measured at grant-date fair value, was $1.2 billion in 2025.",
    "Compensation cost is based on the grant-date fair value and totaled $310 million for the year.",
])
def test_m4d04r_total_expense_at_grant_date_fair_value_kept(src):
    """A3-M4D-04 返工：總費用句（量測基礎是授予日公允價）保留。"""
    out = _clean(src)
    assert ("$1.2 billion" in out) or ("$310 million" in out), out


def test_m4d04r_grant_date_fair_value_of_awards_granted_removed():
    src = "The grant-date fair value of RSUs granted in 2025 was $98.76 per unit on a weighted-average basis."
    assert "98.76" not in _clean(src)


def test_m4d03r_closed_at_billion_kept():
    """A3-M4D-03 返工：closed at＋金額帶量詞（交易總額）不是股價。"""
    src = "The transaction closed at $2.1 billion, funded with cash on hand."
    assert "$2.1 billion" in _clean(src)


class _CaDownAlpaca(FakeAlpaca):
    def corporate_actions(self, symbols, start, end, types=None):
        raise DataError("alpaca corporate actions 503")


def test_m7d04r_backfill_ca_failure_keeps_price_triggers(env, monkeypatch):
    """A3-M7D-04 返工：補跑日公司行動取不到 → 價格觸發照建（不略過全部檔），只記 note。"""
    from trader.pipelines import review as RP
    monkeypatch.setattr(RP, "ny_today", lambda: "2026-10-30", raising=False)
    cfg, paths, conn, tmp = env
    _seed(conn)
    cl = _clients()
    cl["alpaca"] = _CaDownAlpaca({t: 105.0 for t in ("HLD", "FUL", "THS", "TRI", "PRC", "NON", "NOC")})
    s = RP.run_review(cfg, paths, conn, R_ASOF, scope="all", clients=cl, offline_filings=_offline(tmp), due_only=True)
    assert "error" not in s, s
    assert "backfill_price_triggers_skipped" not in s
    assert s["backfill_split_check_unavailable"]["note"] and s.get("splits_fetch_error")
    assert not (paths.work / s["run_id"] / "daily_skill.csv").exists()
    assert s["counts"].get("PRICE", 0) >= 1, s["counts"]


def test_m4d06r_tax_withheld_on_distributions_kept():
    """withheld 後無股數（股利扣繳稅款總額）不是扣股繳稅，保留。"""
    src = "Income taxes withheld on distributions to holders of our shares totaled $3 million."
    assert "$3 million" in _clean(src)
