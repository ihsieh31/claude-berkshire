"""同業資料（peers）測試：衝擊年演算法、競爭者名稱比對、整段產生。全部離線。"""
from __future__ import annotations

import pandas as pd

from trader.analysis import peers as P


def test_shock_year_rules():
    # 6 家同業；2020 中位 ROE 為負 → 衝擊年；2023 中位 5% < 前 5 年中位 20% 的一半 → 衝擊年
    base = {y: 0.20 for y in range(2015, 2026)}
    base[2020] = -0.05
    base[2023] = 0.05
    peer_roe = {f"P{i}": pd.Series(base) for i in range(6)}
    df, shocks = P.shock_years(peer_roe, list(range(2016, 2026)))
    assert shocks == [2020, 2023]
    assert set(df["flag"]) <= {"否", "衝擊年", "前 5 年中位不足 3 年，只判 ≤0"}


def test_shock_year_needs_five_peers():
    peer_roe = {f"P{i}": pd.Series({2020: -0.1, 2021: -0.1}) for i in range(4)}
    df, shocks = P.shock_years(peer_roe, [2020, 2021])
    assert shocks == [] and list(df["flag"]) == ["樣本不足", "樣本不足"]


def test_named_competitors_case_and_common_words():
    titles = {"HD": "HOME DEPOT, INC.", "TGT": "TARGET CORP", "WMT": "Walmart Inc.", "LOW": "LOWES COMPANIES INC"}
    text = ("We compete with The Home Depot and Walmart. Our target customers are DIY homeowners.\n"
            "Lowe's competes on service.")
    got = P.named_competitors(text, titles, ["HD", "TGT", "WMT", "LOW"], "LOW")
    assert got == ["HD", "WMT"]          # Target 是常用字不比對；本公司不列


def test_competition_text_keeps_context_lines():
    tenk = "Item 1. Business\nWe sell tools.\nOther line\nCompetition\nWe compete with Acme Corp.\nEnd\n" \
           "Item 1A. Risk Factors\nRisks."
    t = P.competition_text(tenk)
    assert "Acme" in t and "Risks." not in t


class FakePeerSec:
    def __init__(self):
        def facts(ni, eq, rev, op):
            def dur(y, v):
                return {"form": "10-K", "fp": "FY", "start": f"{y}-01-01", "end": f"{y}-12-31",
                        "filed": f"{y + 1}-02-20", "val": v}

            def pt(y, v):
                return {"form": "10-K", "fp": "FY", "end": f"{y}-12-31", "filed": f"{y + 1}-02-20", "val": v}
            ys = range(2015, 2026)
            return {"facts": {"us-gaap": {
                "NetIncomeLoss": {"units": {"USD": [dur(y, ni) for y in ys]}},
                "StockholdersEquity": {"units": {"USD": [pt(y, eq) for y in ys]}},
                "Revenues": {"units": {"USD": [dur(y, rev) for y in ys]}},
                "OperatingIncomeLoss": {"units": {"USD": [dur(y, op) for y in ys]}},
            }}}
        self.cik = {"SELF": "1", "HD": "2", "A": "3", "B": "4", "C": "5", "D": "6", "E": "7"}
        self.sic = {"1": "5211", "2": "5211", "3": "5200", "4": "5231", "5": "5251", "6": "5261", "7": "3600"}
        self.facts = {c: facts(100e6, 500e6, 1000e6 * int(c), 150e6) for c in self.cik.values()}
        self.text_urls = []

    def cik_for(self, t):
        return self.cik.get(t)

    def submissions(self, cik, max_age_hours=None, asof=None):   # 同真實 SecClient.submissions 介面（A3-M4C-02 起 peers 傳 max_age_hours）
        return {"sic": self.sic[cik]}

    def companyfacts(self, cik):
        return self.facts[cik]

    def merged_companyfacts(self, cik):   # 真實 SecClient 會併入前身 CIK（M4-D2-12）；假物件無前身
        return self.companyfacts(cik)

    def company_titles(self):
        return {"SELF": "SELF CO", "HD": "HOME DEPOT, INC.", "A": "ALPHA STORES", "B": "BETA MART",
                "C": "GAMMA GROCERS", "D": "DELTA AUTO", "E": "EPSILON CHIPS"}

    def filings(self, cik, asof, forms=None, since=None):
        return [{"form": "10-K", "filingDate": "2026-03-18",
                 "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/x/10k.htm"}]

    def filing_text(self, url):
        self.text_urls.append(url)
        return "Item 1. Business\nWe are the largest home improvement retailer.\nItem 7. MD&A\nSales grew.\n" \
               "The closing price of our stock was $300.\n"


def test_build_peer_section_end_to_end(tmp_path):
    sec = FakePeerSec()
    tenk = "Item 1. Business\nCompetition\nWe compete with The Home Depot in every market.\nItem 1A. Risk"
    gics = {"SELF": "Home Improvement Retail", "HD": "Home Improvement Retail", "A": "Other"}
    res = P.build_peer_section(sec, "SELF", "1", "2026-10-16", list(sec.cik), tenk, tmp_path, gics)
    md = res["markdown"]
    assert res["competitors"] == ["HD"]
    assert res["sic"] == "5211"
    assert "SIC 前兩碼 52" in md and "股票池內 5 家" in md    # HD、A、B、C、D（E 為 36）
    assert "| HD | 10-K 競爭段點名 |" in md
    assert res["peer_files"] == ["peer_HD_10-K_2026-03-18.txt"]
    peer = (tmp_path / "peer_HD_10-K_2026-03-18.txt").read_text(encoding="utf-8")
    assert "largest home improvement retailer" in peer
    assert "$300" not in peer and "closing price" not in peer      # 價格隔離
    assert "價" not in md.replace("價值", "")


def test_build_peer_section_narrow_gics_without_named(tmp_path):
    sec = FakePeerSec()
    gics = {"SELF": "Home Improvement Retail", "HD": "Home Improvement Retail"}
    res = P.build_peer_section(sec, "SELF", "1", "2026-10-16", list(sec.cik), "Item 1. Business\nNo names.\n",
                               tmp_path, gics)
    assert res["competitors"] == [] and res["narrow_gics_peers"] == ["HD"]
    assert res["peer_files"] == ["peer_HD_10-K_2026-03-18.txt"]
    assert "GICS 子產業很窄" in res["markdown"]


class OutsideSec(FakePeerSec):
    """池外競爭者（Crown Holdings，非 S&P 500）與 SEC 同 SIC 補樣本。"""
    def __init__(self):
        super().__init__()
        self.cik["CCK"] = "8"
        self.sic["8"] = "5211"
        for c in ("9", "10", "11"):
            self.sic[c] = "5211"
        for c in ("8", "9", "10", "11"):
            self.facts[c] = self.facts["2"]
        self.sic_calls = []

    def company_titles(self):
        return {**super().company_titles(), "CCK": "CROWN HOLDINGS, INC.", "ZZ": "ZETA"}

    def sic_companies(self, sic):
        self.sic_calls.append(sic)
        return ["0000000009", "0000000010", "0000000011", "0000000001"]   # 含本公司 → 排除

    def companyfacts(self, cik):
        return self.facts[str(int(cik))]


def test_outside_competitor_needs_same_sic_division(tmp_path):
    sec = OutsideSec()
    sec.sic["8"] = "6111"     # 融資機構（H 大類）被點名、本公司是零售（G）→ 不收
    tenk = "Item 1. Business\nCompetition\nWe compete with Crown Holdings.\nItem 1A. Risk"
    res = P.build_peer_section(sec, "SELF", "1", "2026-10-16", ["SELF", "HD", "A"], tenk, tmp_path, {})
    assert res["outside_competitors"] == []


def test_outside_named_competitor_and_shock_sample(tmp_path):
    sec = OutsideSec()
    tenk = "Item 1. Business\nCompetition\nWe compete with Crown Holdings and Zeta.\nItem 1A. Risk"
    universe = ["SELF", "HD", "A", "B", "C", "D", "E"]
    res = P.build_peer_section(sec, "SELF", "1", "2026-10-16", universe, tenk, tmp_path, {})
    assert res["outside_competitors"] == ["CCK"]            # Zeta 單字名稱，池外不比對
    assert "CCK" in res["competitors"]
    md = res["markdown"]
    assert "不在股票池但在 SEC 申報：CCK" in md and "| CCK | 10-K 競爭段點名 |" in md
    assert res["peer_files"] == ["peer_CCK_10-K_2026-03-18.txt"]
    assert {k: res["shock_sample"][k] for k in ("pool", "sec_extra")} == {"pool": 5, "sec_extra": 3}
    assert "SEC 補充樣本：" in md
    assert "SEC 同 SIC 四碼補充 3 家" in md
    assert "5211" in sec.sic_calls


def test_sic_division():
    assert P.sic_division("3430") == P.sic_division("2430") == "D"      # MAS 與 Fortune Brands 同為製造業
    assert P.sic_division("1531") == "C" and P.sic_division("6111") == "H"
    assert P.sic_division("") is None and P.sic_division(None) is None


def test_named_competitors_ignores_hyphenated_names():
    titles = {"WMB": "WILLIAMS COMPANIES, INC.", "SHW": "SHERWIN WILLIAMS CO"}
    for dash in ("-", "\u2011"):
        got = P.named_competitors(f"Our paints compete with Sherwin{dash}Williams.", titles, ["WMB", "SHW"], "X")
        assert got == ["SHW"], got


def test_shock_note_when_sec_sample_flags_years(tmp_path):
    sec = OutsideSec()
    bad = sec.facts["2"]["facts"]["us-gaap"]
    for c in ("9", "10", "11", "2", "3", "4", "5", "6"):     # 同業 2020 年虧損 → 衝擊年
        f = {"facts": {"us-gaap": {k: {"units": {"USD": [dict(x) for x in v["units"]["USD"]]}} for k, v in bad.items()}}}
        for x in f["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]:
            if x["end"].startswith("2020"):
                x["val"] = -50e6
        sec.facts[c] = f
    res = P.build_peer_section(sec, "SELF", "1", "2026-10-16", ["SELF", "HD", "A", "B", "C", "D", "E"], None,
                               tmp_path, {})
    assert 2020 in res["shock_years"] and res["shock_sample"]["sec_extra"] == 3
    assert "交分析在誠實標示寫明" in res["markdown"]
