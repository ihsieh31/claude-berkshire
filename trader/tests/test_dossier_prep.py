"""深度分析前置（dossier_prep）測試：不連網。

build_dossier 測試會真的跑 skill 腳本子程序，但快取命中（手寫 companyfacts），不碰網路。
"""
from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from trader.analysis import dossier_prep as dp
from trader.analysis.dossier_prep import DossierError

UA = "test-agent test@example.com"
CIK = "0001067983"


# ---------- 假 SEC 物件 ----------

class FakeSec:
    """模擬 SecClient 的介面；filings 仍保留 asof 過濾（與真實行為一致），並刻意多塞一筆未來申報驗證過濾。"""

    def __init__(self, facts=None, filings=None, texts=None, user_agent=UA):
        self._facts = facts
        self._filings = filings or []
        self._texts = texts or {}
        self.user_agent = user_agent
        self.text_calls: list[str] = []
        self.filing_calls: list[dict] = []

    def cik_for(self, ticker):
        return CIK if ticker.replace("-", ".").upper() in ("BRK.B",) else None

    def companyfacts(self, cik):
        return self._facts

    def _with_predecessor_facts(self, cik10, facts):
        return facts, [], []

    def filings(self, cik, asof, since=None, forms=None):
        self.filing_calls.append({"asof": asof, "forms": forms})
        out = [f for f in self._filings if f["filingDate"] < asof]
        if forms is not None:
            out = [f for f in out if f["form"] in forms]
        return sorted(out, key=lambda f: f["filingDate"])

    def filing_text(self, url):
        self.text_calls.append(url)
        return self._texts[url]


def _facts(net_income=True):
    """手寫 companyfacts：淨利／營收（期間型）與股東權益（時點型），FY2019–2024 皆於 asof 前申報；
    另放一筆 FY2025 於 asof 當日後申報，必須被排除。"""
    def dur(fy, val, filed):
        return {"form": "10-K", "fp": "FY", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                "filed": filed, "val": val}

    rows_ni = [dur(y, 100 + i, f"{y + 1}-02-20") for i, y in enumerate(range(2019, 2025))]
    rows_ni.append(dur(2025, 999, "2026-10-16"))  # 決策日當天（含）之後申報，排除
    rows_rev = [dur(y, 1000 + i, f"{y + 1}-02-20") for i, y in enumerate(range(2019, 2025))]
    rows_eq = [{"form": "10-K", "fp": "FY", "end": f"{y}-12-31", "filed": f"{y + 1}-02-20", "val": 500 + i}
               for i, y in enumerate(range(2019, 2025))]
    us = {
        "Revenues": {"units": {"USD": rows_rev}},
        "StockholdersEquity": {"units": {"USD": rows_eq}},
    }
    if net_income:
        us["NetIncomeLoss"] = {"units": {"USD": rows_ni}}
    return {"cik": int(CIK), "entityName": "TEST", "facts": {"us-gaap": us}}


def _sec_with_facts(net_income=True, **kw):
    return FakeSec(facts=_facts(net_income), **kw)


# ---------- scrub_price_text ----------

# 每個 pattern 至少一個樣本；樣本都必須被刪除
PRICE_SAMPLES = [
    "The aggregate market value of common stock held by non-affiliates was $1,000 million.",
    "Closing price of the stock on June 30, 2025 was $86.10.",
    "Closing sale price per share was $12.",
    "the market price of our common stock fluctuated significantly.",
    "the market price of the common stock may decline.",
    "Our stock price may be volatile.",
    "The share price of our Class A shares.",
    "Net proceeds at a purchase price per share of $50 for the acquisition.",
    "The exercise price of options outstanding was $30.",
    "High and low sales prices per share for each quarter: $90 $80.",
    "Trailing price-to-earnings ratio 18x.",
    "Price to earnings multiple of 20.",
    "The P/E ratio was 15.",
    "Total market capitalization exceeded $1 trillion.",
    "Consensus price target of $200.",
    "Analyst rating: Buy.",
    "See the Stock Performance Graph below.",
    "Cumulative total shareholder return on $100 invested.",
    "Cumulative total stockholder return since 2020.",
    "Average Price Paid per Share $123.45",
    "Total number of shares purchased and price paid per share.",
    "公司股價大幅波動。",
    "截至年底市值為 1,000 億美元。",
    "本益比偏高。",
    "分析師目標價調升。",
    "The current market price of the shares is $50.",
    "The trading price of our notes may fluctuate.",
    "現價（70.39，2026-10-15）高於上限。",
]


def test_every_price_pattern_has_a_sample_and_is_removed():
    for pat in dp.PRICE_TEXT_PATTERNS:
        assert any(pat.search(s) for s in PRICE_SAMPLES), f"無樣本命中 {pat.pattern}"
    for s in PRICE_SAMPLES:
        out, n = dp.scrub_price_text(s)
        assert n == 1 and out == "", f"未刪除：{s}"


def test_scrub_is_case_insensitive():
    out, n = dp.scrub_price_text("STOCK PRICE moves\nkeep this line")
    assert n == 1 and out == "keep this line"


def test_scrub_keeps_financial_lines_with_dollar_signs():
    text = "\n".join([
        "Net income $ 1,234 million",
        "Total revenues $ 300,000 million",
        "Earnings per share, diluted $ 5.20",
        "Cash paid for interest $ 50",
        "Dividends paid per share $ 2.00",
    ])
    out, n = dp.scrub_price_text(text)
    assert n == 0
    assert out == text


def test_scrub_removes_only_price_lines_and_counts_them():
    text = "Revenue $ 100\nThe aggregate market value of stock was $5 billion.\nNet income $ 10"
    out, n = dp.scrub_price_text(text)
    assert n == 1
    assert out == "Revenue $ 100\nNet income $ 10"


# ---------- split_10k_items ----------

def test_split_items_basic_and_order():
    text = "Item 1. Business\nWe sell things.\nItem 1A. Risk Factors\nRisks here.\nItem 7. MD&A\nDiscussion.\n"
    got = dp.split_10k_items(text)
    assert [c for c, _ in got] == ["1", "1A", "7"]
    assert got[0][1].startswith("Item 1. Business")
    assert "Risks here." in got[1][1]
    assert "Discussion." in got[2][1]


def test_split_items_toc_duplicates_keep_longest():
    toc = "Item 1. Business 3\nItem 1A. Risk Factors 5\n"
    body = "Item 1. Business\n" + "Body text. " * 50 + "\nItem 1A. Risk Factors\nshort\n"
    got = dict(dp.split_10k_items(toc + body))
    assert "Body text." in got["1"]
    assert len(got["1"]) > 200
    assert [c for c, _ in dp.split_10k_items(toc + body)].count("1") == 1


def test_split_items_accepts_uppercase_no_period_and_dash():
    # 無句點、大寫
    text1 = "ITEM 7\nMD&A text one.\nITEM 8 — Financials\nnumbers"
    assert [c for c, _ in dp.split_10k_items(text1)] == ["7", "8"]
    assert "MD&A text one." in dict(dp.split_10k_items(text1))["7"]
    # en dash 寫法
    text2 = "Item 7 – Management's discussion\nMD&A text two.\nItem 8 – Financials\nnumbers"
    assert [c for c, _ in dp.split_10k_items(text2)] == ["7", "8"]
    assert "MD&A text two." in dict(dp.split_10k_items(text2))["7"]


def test_split_items_ignores_out_of_range_numbers_and_inline_mentions():
    text = "Item 99. Nonsense\nItem 2. Properties\nThe company owns buildings.\n"
    assert [c for c, _ in dp.split_10k_items(text)] == ["2"]


def test_split_items_unsplittable_returns_all():
    text = "This document has no item headings at all.\nJust prose."
    assert dp.split_10k_items(text) == [("ALL", text)]


# ---------- fetch_sec_texts ----------

def _f(form, date, acc_suffix, doc=None):
    acc = f"0001067983-{date[2:4]}-{acc_suffix:06d}"
    return {"form": form, "filingDate": date, "reportDate": "", "items": "",
            "accessionNumber": acc, "url": f"https://www.sec.gov/Archives/edgar/data/1067983/{acc.replace('-', '')}/{doc or 'x.htm'}"}


def _fetch_fixture():
    filings = [
        _f("10-K", "2024-02-20", 3),
        _f("10-K", "2025-02-20", 5),          # 最新 10-K
        _f("10-Q", "2025-01-15", 9),          # 10-K 之前，不取
        _f("10-Q", "2025-04-30", 10),
        _f("10-Q", "2025-07-30", 11),
        _f("10-Q", "2025-10-30", 12),
        _f("10-Q", "2026-01-30", 13),         # 最新三份為 2025-04/07/10 之後者，這裡共四份，取後三份
        _f("DEF 14A", "2024-03-01", 20),
        _f("DEF 14A", "2025-03-05", 21),      # 最新一份
        _f("8-K", "2025-02-01", 30),          # 10-K 之前，不取
        _f("8-K", "2025-03-10", 31),
        _f("8-K", "2025-05-10", 32),
        _f("8-K", "2025-08-10", 33),
        _f("8-K", "2025-11-10", 34),
        _f("8-K", "2026-10-16", 35),          # 決策日當天，排除
        _f("10-K", "2026-10-16", 36),         # 決策日當天，排除
    ]
    texts = {}
    for f in filings:
        texts[f["url"]] = (
            "ANNUAL REPORT\n"
            "Aggregate market value of common stock held by non-affiliates: $1,000 million\n"
            "Net income $ 500 million\n"
            "Share price performance was strong.\n"
        )
    return filings, texts


def test_fetch_sec_texts_selection_rules(tmp_path):
    filings, texts = _fetch_fixture()
    sec = FakeSec(filings=filings, texts=texts)
    out = dp.fetch_sec_texts(sec, CIK, "2026-10-15", tmp_path / "docs", max_8k=3)

    by_form = {}
    for r in out:
        by_form.setdefault(r["form"], []).append(r["filingDate"])
    assert by_form["10-K"] == ["2025-02-20"]                                  # 最新一份
    assert by_form["10-Q"] == ["2025-07-30", "2025-10-30", "2026-01-30"]  # 10-K 之後 4 份取最新 3 份
    assert by_form["DEF 14A"] == ["2025-03-05"]                               # 最新一份
    assert by_form["8-K"] == ["2025-05-10", "2025-08-10", "2025-11-10"]      # 10-K 之後最新 3 份
    assert all(r["filingDate"] < "2026-10-15" for r in out)
    assert [r["filingDate"] for r in out] == sorted(r["filingDate"] for r in out)


def test_fetch_sec_texts_max_8k_caps_and_zero(tmp_path):
    filings, texts = _fetch_fixture()
    sec = FakeSec(filings=filings, texts=texts)
    out = dp.fetch_sec_texts(sec, CIK, "2026-10-15", tmp_path / "a", max_8k=12)
    assert len([r for r in out if r["form"] == "8-K"]) == 4  # 10-K（2025-02-20）之後共 4 份，決策日當天的排除
    out0 = dp.fetch_sec_texts(sec, CIK, "2026-10-15", tmp_path / "b", max_8k=0)
    assert not [r for r in out0 if r["form"] == "8-K"]


def test_fetch_sec_texts_file_names_header_and_scrub(tmp_path):
    filings, texts = _fetch_fixture()
    sec = FakeSec(filings=filings, texts=texts)
    outdir = tmp_path / "docs"
    out = dp.fetch_sec_texts(sec, CIK, "2026-10-15", outdir)

    tenk = next(r for r in out if r["form"] == "10-K")
    assert tenk["file"] == "10-K_2025-02-20_000005.txt"
    assert next(r for r in out if r["form"] == "DEF 14A")["file"] == "DEF14A_2025-03-05_000021.txt"
    assert (outdir / tenk["file"]).exists()

    text = (outdir / tenk["file"]).read_text(encoding="utf-8")
    first = text.split("\n", 1)[0]
    assert first == f"# 10-K 2025-02-20 {tenk['url']}"
    assert "Aggregate market value" not in text
    assert "Share price performance" not in text
    assert "Net income $ 500 million" in text
    assert tenk["removed_lines"] == 2
    assert tenk["url"] in sec.text_calls


def test_fetch_sec_texts_empty_when_no_filings(tmp_path):
    assert dp.fetch_sec_texts(FakeSec(filings=[]), CIK, "2026-10-15", tmp_path) == []


# ---------- build_dossier_base（真的跑 skill 子程序，快取命中） ----------

def test_build_dossier_base_happy_path(tmp_path):
    sec = _sec_with_facts()
    rates = {"t30": 0.0475, "t30_date": "2026-10-14", "t30_20y_median": 0.035}
    res = dp.build_dossier_base(sec, "BRK.B", "2026-10-15", tmp_path / "out",
                                price=86.1, price_date="2026-10-14", rates=rates)
    md, pj = res["md"], res["price_json"]
    assert md.name == "BRK.B.md" and pj.name == "BRK.B_price.json"
    assert md.exists() and pj.exists()
    assert not (tmp_path / "out" / "_cache").exists()
    assert res["predecessors_used"] == []

    text = md.read_text(encoding="utf-8")
    assert "資料包：BRK.B" in text
    assert "86.1" not in text                      # 價格隔離
    assert "2026-10-16" not in text                # 決策日當天後申報的 FY2025 不得出現
    assert "2025-02-20" in text                    # FY2024 的 10-K 申報日

    data = json.loads(pj.read_text(encoding="utf-8"))
    assert data["price"] == 86.1
    assert data["price_date"] == "2026-10-14"
    assert data["t30"] == 0.0475
    assert data["t30_date"] == "2026-10-14"
    assert data["t30_20y_median"] == 0.035
    # 平均回購價（歷史價格水準）只在價格檔，資料包不得有，也不得留回購股數讓模型自行相除
    assert "平均回購價" not in text and "buyback_shares" not in text
    assert isinstance(data["avg_buyback_price_by_fy"], dict)
    assert data["decision_date"] == "2026-10-15"


def test_build_dossier_base_without_price_writes_nulls(tmp_path):
    res = dp.build_dossier_base(_sec_with_facts(), "BRK.B", "2026-10-15", tmp_path / "out",
                                price=None, price_date=None, rates=None)
    data = json.loads(res["price_json"].read_text(encoding="utf-8"))
    assert data["price"] is None and data["price_date"] is None and data["t30"] is None


def test_build_dossier_base_no_net_income_raises_and_cleans_cache(tmp_path):
    out = tmp_path / "out"
    with pytest.raises(DossierError) as ei:
        dp.build_dossier_base(_sec_with_facts(net_income=False), "BRK.B", "2026-10-15", out,
                              price=1.0, price_date="2026-10-14", rates=None)
    assert "淨利" in str(ei.value)
    assert not (out / "_cache").exists()
    assert not (out / "BRK.B.md").exists()


def test_build_dossier_base_unknown_ticker_raises(tmp_path):
    with pytest.raises(DossierError):
        dp.build_dossier_base(_sec_with_facts(), "ZZZZ", "2026-10-15", tmp_path / "o",
                              price=None, price_date=None, rates=None)


def test_build_dossier_base_passes_user_agent_via_env(tmp_path, monkeypatch):
    seen = {}
    real_run = subprocess.run

    def spy(cmd, **kw):
        seen["env"] = kw.get("env", {})
        seen["timeout"] = kw.get("timeout")
        seen["cmd"] = cmd
        return real_run(cmd, **kw)

    monkeypatch.setattr(dp.subprocess, "run", spy)
    monkeypatch.setenv("SEC_USER_AGENT", "should-be-overridden")
    dp.build_dossier_base(_sec_with_facts(), "BRK.B", "2026-10-15", tmp_path / "o",
                          price=None, price_date=None, rates=None)
    assert seen["env"]["SEC_USER_AGENT"] == UA
    assert seen["timeout"] == 300
    assert "--cache" in seen["cmd"] and "--asof" in seen["cmd"]


def test_user_agent_fallbacks(monkeypatch):
    class HttpStub:
        def __init__(self, ua):
            self._client = httpx.Client(headers={"User-Agent": ua} if ua else {})  # 無 ua 時 httpx 自帶 python-httpx/…

    class NoAttr:
        def __init__(self, http):
            self.http = http

    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert dp._user_agent(NoAttr(HttpStub("from-http x@y.z"))) == "from-http x@y.z"
    monkeypatch.setenv("SEC_USER_AGENT", "from-env x@y.z")
    assert dp._user_agent(NoAttr(HttpStub(None))) == "from-env x@y.z"  # httpx 預設 UA 不算
    assert dp._user_agent(FakeSec(user_agent="attr x@y.z")) == "attr x@y.z"


def test_user_agent_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    class NoAttr:
        http = None

    with pytest.raises(DossierError) as ei:
        dp._user_agent(NoAttr())
    assert "User-Agent" in str(ei.value)
    with pytest.raises(DossierError):
        dp.build_dossier_base(FakeSec(facts=_facts(), user_agent=""), "BRK.B", "2026-10-15",
                              tmp_path / "o", price=None, price_date=None, rates=None)


def test_predecessor_notes_recorded(tmp_path):
    class PredSec(FakeSec):
        def _with_predecessor_facts(self, cik10, facts):
            return facts, ["0000000001"], ["0000000002"]

    res = dp.build_dossier_base(PredSec(facts=_facts()), "BRK.B", "2026-10-15", tmp_path / "o",
                                price=None, price_date=None, rates=None)
    assert res["predecessors_used"] == ["0000000001"]
    assert any("0000000001" in n for n in res["notes"])
    assert any("0000000002" in n and "失敗" in n for n in res["notes"])


def test_drop_price_sections_removes_item5_and_buyback_table():
    from trader.analysis.dossier_prep import drop_price_sections
    text = ("Item 1. Business\nWe make drinks.\n"
            "Item 5. Market for Registrant's Common Equity\nQ1 2025 $45.20 $40.10\nQ2 2025 $47.00 $41.00\n"
            "Item 7. Management's Discussion\nRevenue grew.\n"
            "Issuer Purchases of Equity Securities\nOct 1-31, 2025 1,234 $123.45\n"
            "Item 3. Defaults\nNone.\n")
    out, n = drop_price_sections(text)
    assert n == 2
    assert "$45.20" not in out and "$123.45" not in out
    assert "We make drinks." in out and "Revenue grew." in out and "None." in out


# ---------- 補充資產負債欄位 ----------

def _bs_facts():
    def inst(tag_rows):
        return {"units": {"USD": tag_rows}}

    def pt(fy, val):
        return {"form": "10-K", "fp": "FY", "end": f"{fy}-12-31", "filed": f"{fy + 1}-02-20", "val": val}

    def dur(fy, val):
        return {"form": "10-K", "fp": "FY", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                "filed": f"{fy + 1}-02-20", "val": val}
    ys = range(2022, 2025)
    us = {
        "NetIncomeLoss": inst([dur(y, 80e6) for y in ys]),
        "OperatingIncomeLoss": inst([dur(y, 100e6) for y in ys]),
        "IncomeTaxExpenseBenefit": inst([dur(y, 20e6) for y in ys]),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
            inst([dur(y, 100e6) for y in ys]),
        "PropertyPlantAndEquipmentNet": inst([pt(y, 200e6) for y in ys]),
        "AssetsCurrent": inst([pt(2022, 500e6), pt(2023, 500e6), pt(2024, 300e6)]),
        "LiabilitiesCurrent": inst([pt(y, 400e6) for y in ys]),
        "CashAndCashEquivalentsAtCarryingValue": inst([pt(y, 100e6) for y in ys]),
        "LongTermDebtCurrent": inst([pt(y, 50e6) for y in ys]),
    }
    return {"facts": {"us-gaap": us}}


def test_supplement_section_tangible_capital_return():
    md = dp.supplement_section(_bs_facts(), "2026-10-15", years=3)
    rows = {ln.split("|")[1].strip(): [c.strip() for c in ln.split("|")[2:-1]]
            for ln in md.splitlines() if ln.startswith("| ") and not ln.startswith("| 百萬美元")}
    # 營運資金 =(500−100)−(400−50)=50；有形資本 = 50+200 = 250；稅後營業利益 = 100×(1−0.2)=80 → 32.0%
    assert rows["有形資本＝營運資金＋固定資產淨額"][:2] == ["250", "250"]
    assert rows["稅後無槓桿有形資本報酬"][:2] == ["32.0%", "32.0%"]
    # 2024：(300−100)−(400−50)=−150；有形資本 50 → 160%
    assert rows["稅後無槓桿有形資本報酬"][2] == "160.0%"
    assert "價" not in md and "市值" not in md


def test_supplement_section_negative_capital_and_unclassified():
    f = _bs_facts()
    f["facts"]["us-gaap"]["LiabilitiesCurrent"]["units"]["USD"] = [
        {"form": "10-K", "fp": "FY", "end": f"{y}-12-31", "filed": f"{y + 1}-02-20", "val": 900e6} for y in range(2022, 2025)]
    md = dp.supplement_section(f, "2026-10-15", years=3)
    assert "有形資本≤0" in md
    del f["facts"]["us-gaap"]["AssetsCurrent"]
    md = dp.supplement_section(f, "2026-10-15", years=3)
    assert "未分類資產負債表" in md


def test_build_dossier_base_appends_supplement_before_text_section(tmp_path):
    res = dp.build_dossier_base(_sec_with_facts(), "BRK.B", "2026-10-15", tmp_path / "out",
                                price=86.1, price_date="2026-10-14", rates=None)
    text = res["md"].read_text(encoding="utf-8")
    i, j = text.find("## 補充資產負債欄位"), text.find("## 年報文字節錄")
    assert 0 < i < j
    assert "86.1" not in text


@pytest.mark.parametrize("h,drop", [
    ("Evercore ISI Group Maintains Outperform on Otis Worldwide, Lowers Price Target to $95", True),
    ("Wells Fargo Initiates Coverage On Otis Worldwide with Underweight Rating", True),
    ("Otis shares fall 6% after guidance cut", True),
    ("Stock hits 52-week low", True),
    ("Otis Worldwide Chair, CEO And President Judy Marks To Retire", False),
    ("Otis Worldwide Q2 Adj. EPS $1.01, Sales $3.859B", False),
    ("Otis To Supply 251 Elevators To Tianjin 117 Tower", False),
])
def test_is_price_headline(h, drop):
    assert dp.is_price_headline(h) is drop


def test_scrub_price_sentences_keeps_other_sentences_on_same_line():
    line = "Otis 以維護組合收取經常性服務費。現價（70.39，2026-10-15）高於自動買進上限 49.29，故只觀察。"
    text = "# 論點\n" + line + "\n- g_impl ≈ 9.4%（現價隱含）\n- 失效條件：服務利益率 <23%"
    out, n = dp.scrub_price_sentences(text)
    assert "70.39" not in out and "現價" not in out
    assert "Otis 以維護組合收取經常性服務費。" in out      # 同一行的論點句保留
    assert "g_impl" not in out                              # 整行都是價格資訊 → 整行刪
    assert "失效條件：服務利益率 <23%" in out
    assert n == 2


class _ExSec:
    def __init__(self, sgml):
        self.sgml = sgml
        self.calls = []

    def exhibit_text(self, cik, acc, ex_type):
        self.calls.append((cik, acc, ex_type))
        return self.sgml


def test_earnings_exhibit_only_for_item_202():
    url = "https://www.sec.gov/Archives/edgar/data/1781335/000178133526000110/otis-20260722.htm"
    sec = _ExSec("Q2 net sales $3.9 billion; organic service growth 9%")
    out = dp.earnings_exhibit(sec, url, "2.02,9.01")
    assert "organic service growth 9%" in out and "EX-99" in out
    assert sec.calls == [("1781335", "0001781335-26-000110", "EX-99")]
    assert dp.earnings_exhibit(sec, url, "5.02") == ""
    assert "缺" in dp.earnings_exhibit(_ExSec(None), url, "2.02")


def test_fetch_sec_texts_prefers_original_10k_over_short_amendment(tmp_path):
    """MO 案例：年報之後的 10-K/A 只有封面，不得取代原始 10-K；其間 8-K 也不能被丟掉。"""
    filings = [
        _f("10-K", "2026-02-25", 5),
        _f("8-K", "2026-04-10", 6),           # 10-K 之後、10-K/A 之前
        _f("10-K/A", "2026-05-27", 7),
        _f("10-Q", "2026-07-30", 8),
    ]
    texts = {f["url"]: "BODY " + f["form"] for f in filings}
    texts[filings[0]["url"]] = "ANNUAL " + "x" * 60_000
    texts[filings[2]["url"]] = "COVER ONLY"
    out = dp.fetch_sec_texts(FakeSec(filings=filings, texts=texts), CIK, "2026-10-15", tmp_path)
    forms = [(r["form"], r["filingDate"]) for r in out]
    assert ("10-K", "2026-02-25") in forms
    assert ("8-K", "2026-04-10") in forms
    assert not [r for r in out if r["form"] == "10-K/A"]


def test_fetch_sec_texts_keeps_long_amendment_and_falls_back(tmp_path):
    filings = [_f("10-K", "2026-02-25", 5), _f("10-K/A", "2026-05-27", 7)]
    texts = {filings[0]["url"]: "ANNUAL " + "x" * 60_000, filings[1]["url"]: "RESTATED " + "y" * 60_000}
    out = dp.fetch_sec_texts(FakeSec(filings=filings, texts=texts), CIK, "2026-10-15", tmp_path)
    assert [r["form"] for r in out] == ["10-K", "10-K/A"]
    # 沒有原始 10-K 時退而取 10-K/A
    only = [_f("10-K/A", "2026-05-27", 7)]
    out2 = dp.fetch_sec_texts(FakeSec(filings=only, texts={only[0]["url"]: "A" * 10}), CIK, "2026-10-15", tmp_path / "b")
    assert [r["form"] for r in out2] == ["10-K/A"]


class _MultiExSec:
    def __init__(self, exs):
        self.exs = exs

    def exhibit_texts(self, cik, acc, ex_type):
        return self.exs.get(ex_type, [])


def test_earnings_exhibit_includes_supplements_capped():
    url = "https://www.sec.gov/Archives/edgar/data/1781335/000178133526000110/otis-20260722.htm"
    big = "Policies in force retention 88.1%\n" + "x" * (dp.SUPPLEMENT_CHARS + 10)
    sec = _MultiExSec({"EX-99": [("EX-99.1", "press release"), ("EX-99.2", big), ("EX-99.3", "deck"),
                                 ("EX-99.4", "dropped")]})
    out = dp.earnings_exhibit(sec, url, "2.02")
    assert "press release" in out and "附件 EX-99.2（財報補充資料／簡報）" in out and "retention 88.1%" in out
    assert "框架截斷" in out and "deck" in out and "dropped" not in out


def test_annual_report_exhibit_scrubbed_separately(tmp_path):
    """EX-13 另行刪價格：EX-13 內的回購表段落不得吃掉後面的致股東信，也不得吃掉主文。"""
    ex13 = ("Dear Shareholders,\nWe raised prices twice and kept customers.\n"
            "Issuer Purchases of Equity Securities\nAverage price paid per share $55\n")
    sec = _MultiExSec({"EX-13": [("EX-13", ex13)]})
    out = dp.annual_report_exhibit(sec, "1", "0000000001-26-000001")
    assert "Dear Shareholders" in out and "致股東年報" in out
    text, _ = dp.drop_price_sections(out)
    text, _ = dp.scrub_price_text(text)
    assert "kept customers" in text and "$55" not in text
    assert dp.annual_report_exhibit(_MultiExSec({}), "1", "x") == ""
