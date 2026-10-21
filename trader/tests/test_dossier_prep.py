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
    "決策日價格 70.39，首批於 72 美元以下買進（P/IV 0.80）。",   # 論點檔寫法（P33／M5_D6-04）
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
    "The average repurchase price was $81.",
    "2025 年平均回購價約 80 美元。",
    "We repurchased shares at an average price of $233.07 per share.",
    "The average cost per share repurchased was $88.10.",
    "Shares were bought at an average of $90 a share.",
    "The Company repurchased shares for $175 million, or $181.59 per share.",
    "Restricted stock was granted at a weighted-average fair value of $276.44 per share.",
    # P12（M4-D6-1、M4-D6-5）
    "The weighted-average grant-date fair value per share was $55.10.",
    "Market value of the shares that have not vested $1,234,567",
    "Market or payout value of unearned shares $2,345,678",
    "Value of $100 invested on December 31, 2020.",
    "Relative TSR at the 55th percentile resulted in a payout of 110%.",
    "Expected volatility 34.0%.",
    "公司三年股東總報酬率 12%。",
]

# P12（D38 數字型遮蔽）：check-F04 的 7 種漏網＋FDX 句型；sanitize_filing_text 後不得留下每股價格數字
PER_SHARE_SAMPLES = [
    ("We repurchased shares in 2025 (an average of $200.00 per share).", "200.00"),
    ("Shares were bought at a cost of $150.25 per share.", "150.25"),
    ("The Company bought shares at prices averaging $88.10 per share.", "88.10"),
    ("The weighted-average grant date fair value per share was $55.10.", "55.10"),
    ("The weighted average fair value of RSUs granted was $63.21.", "63.21"),
    ("Under the ATM program we sold shares at a weighted average sales price of $57.14.", "57.14"),
    ("We completed an underwritten offering at $35.50 per share.", "35.50"),
    ("We repurchased shares at an average price of $233.07 per share.", "233.07"),
    ("Each share was converted into the right to receive $54.00 per share in cash.", "54.00"),
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
    assert first == f"# 10-K 2025-02-20 {tenk['url']} {dp.external_header('10-K', '2025-02-20')}"   # M4-D8-03（P40）
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


# ---------- 2026-10-18 Hark 修正：回購股數遮蔽、保險業歷年 10-K、首次公布版＋最新重編版 ----------

@pytest.mark.parametrize("src", [
    "During 2025, we repurchased 5,138,535 shares at a total cost of $1.6 billion.",
    "We bought back 1.7 million of our shares for $450 million.",
    "repurchased 3.4 million shares of Class A common stock for $278 million",
    "2025 年共回購 16.6 百萬股、13.4 億。",
    "庫藏股回購 226.8M（1,246,190 股）",
    "Q2 回購 793,077 股與 237,618 OP 單位，合計 211.4M",
])
def test_mask_buyback_shares_removes_counts_and_amounts(src):
    out, n = dp.mask_buyback_shares(src)
    assert n >= 1 and (dp.BUYBACK_MASK in out or dp.BUYBACK_ROW_MASK in out)
    assert dp.buyback_leaks(out) == []
    for amt in ("$1.6 billion", "$450 million", "13.4 億", "226.8M", "211.4M", "$278 million"):
        if amt in src:
            assert amt not in out      # 09:42：前半只給級距，回購金額也遮（P12）


def test_mask_buyback_shares_leaves_unrelated_sentences():
    src = ("Revenue grew 5% on 120 million shares outstanding. We repurchased 2,000,000 shares for $90 million.\n"
           "Dividends paid $358 million. 每股股利 2 元，股東 300 人。")
    out, n = dp.mask_buyback_shares(src)
    assert n == 2
    assert "120 million shares outstanding" in out           # 同一行的其他句子不動
    assert "$90 million" not in out and "2,000,000" not in out
    assert "每股股利 2 元，股東 300 人" in out


def test_fetch_sec_texts_masks_buyback_in_10q_and_8k(tmp_path):
    filings = [_f("10-K", "2025-02-20", 5), _f("10-Q", "2025-05-01", 6), _f("8-K", "2025-07-30", 7)]
    body = ("Item 2. Unregistered Sales of Equity Securities\n"
            "During the quarter we repurchased 1,733,574 shares for $450 million under the program.\n"
            "Issuer Purchases of Equity Securities\nApril 1-30 500,000 $260.12\n"
            "Item 3. Defaults\nNone.\n")
    texts = {f["url"]: body for f in filings}
    out = dp.fetch_sec_texts(FakeSec(filings=filings, texts=texts), CIK, "2026-10-15", tmp_path)
    for r in out:
        t = (tmp_path / r["file"]).read_text(encoding="utf-8")
        assert "1,733,574" not in t and "$260.12" not in t and "$450 million" not in t
        assert "under the program" in t
        assert dp.buyback_leaks(t) == []
        assert r["buyback_masked"] == 2


class _InsSec(FakeSec):
    def __init__(self, sic, **kw):
        super().__init__(**kw)
        self.sic = sic
        self.since_seen = []

    def submissions(self, cik):
        return {"sic": self.sic}

    def filings(self, cik, asof, since=None, forms=None):
        self.since_seen.append(since)
        return super().filings(cik, asof, since, forms)


def _ins_fixture():
    filings = [_f("10-K", f"{y}-02-20", y - 2000) for y in range(2013, 2026)]   # 2013…2025 各一份
    filings.append(_f("10-K", "2026-10-16", 99))                               # 決策日後，排除
    filings.append(_f("10-Q", "2025-05-01", 50))
    body = ("Item 1. Business\nWe write auto insurance. Reserve development table.\n"
            "Item 2. Properties\nOffices.\n"
            "Item 5. Market for Registrant's Common Equity\nHigh $90 Low $70\n"
            "Item 7. Management's Discussion and Analysis\nCombined ratio 96.1% 97.4% 99.0%.\n"
            "We repurchased 2,000,000 shares for $300 million.\n"
            "Item 8. Financial Statements\nLoss reserves 10,000.\n"
            "Item 9. Changes in Accountants\nNone.\n")
    return filings, {f["url"]: body for f in filings}


def test_fetch_sec_texts_insurer_adds_history_10ks(tmp_path):
    filings, texts = _ins_fixture()
    sec = _InsSec("6331", filings=filings, texts=texts)
    out = dp.fetch_sec_texts(sec, CIK, "2026-10-15", tmp_path)
    hist = [r for r in out if r.get("history")]
    main = [r for r in out if r["form"] == "10-K" and not r.get("history")]
    assert [r["filingDate"] for r in main] == ["2025-02-20"]
    # 間隔 2 份、至多 4 份：FY−2、FY−4、FY−6、FY−8 的 10-K → 與最新一份合計涵蓋 11 個會計年度
    assert [r["filingDate"] for r in hist] == ["2017-02-20", "2019-02-20", "2021-02-20", "2023-02-20"]
    assert all(r["filingDate"] < "2026-10-15" for r in out)
    assert any(s is not None for s in sec.since_seen)         # 回溯舊分頁
    t = (tmp_path / hist[0]["file"]).read_text(encoding="utf-8")
    assert hist[0]["file"].startswith("10-K-HIST_")
    assert "Combined ratio 96.1%" in t and "Loss reserves" in t and "Reserve development" in t
    assert "Offices." not in t and "$90" not in t and "Changes in Accountants" not in t   # 只留 1、7、7A、8、15
    assert "2,000,000" not in t and "$300 million" not in t
    sec_md = dp.history_section(out, "保險")
    assert "10-K-HIST_2017-02-20" in sec_md and "至少 5 個會計年度" in sec_md


def test_fetch_sec_texts_non_insurer_has_no_history(tmp_path):
    filings, texts = _ins_fixture()
    out = dp.fetch_sec_texts(_InsSec("2080", filings=filings, texts=texts), CIK, "2026-10-15", tmp_path)
    assert not [r for r in out if r.get("history")]
    assert dp.history_section(out, None) == ""


def test_multi_year_industry():
    assert dp.multi_year_industry("6331") == "保險" and dp.multi_year_industry(6311) == "保險"
    assert dp.multi_year_industry("6021") == "銀行"
    assert dp.multi_year_industry("6411") is None and dp.multi_year_industry(None) is None
    assert dp.multi_year_industry("abc") is None


def test_history_10k_keeps_hon_style_statements():
    """HON 型：價格章節後沒有 Item 標題、直接接財報章節；刪價格段不得吃掉財報。"""
    raw = ("Item 5. Market for Registrant's Common Equity\nQ1 $200 $180\n"
           "FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\nNet sales 38,000\n")
    text, dropped, _, _ = dp.history_10k_text(raw)
    assert dropped == 1 and "Net sales 38,000" in text and "$200" not in text


def _restate_facts():
    def dur(fy, val, filed):
        return {"form": "10-K", "fp": "FY", "start": f"{fy}-01-01", "end": f"{fy}-12-31", "filed": filed, "val": val}
    ni = [dur(2021, 100.0, "2022-02-20"), dur(2022, 200.0, "2023-02-20"),
          dur(2022, 180.0, "2024-02-20"),      # FY2022 在 FY2023 10-K 重編
          dur(2023, 300.0, "2024-02-20"),
          dur(2023, 1.0, "2026-10-16"),        # 決策日後的重編：不得使用
          dur(2021, 100.2, "2023-02-20")]      # 差 0.2% < 門檻：不列
    return {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": ni}}}}}


def test_restatement_data_pairs_first_and_latest_before_asof():
    d = dp.restatement_data(_restate_facts(), "2026-10-15")
    assert d["years"][-1] == 2023
    ni = d["series"]["net_income"]
    assert ni["first"][2022] == (200.0, "2023-02-20") and ni["latest"][2022] == (180.0, "2024-02-20")
    assert ni["latest"][2023] == (300.0, "2024-02-20")          # 決策日後的 1.0 不得出現
    assert [(x["fy"], x["first"], x["latest"]) for x in d["diffs"]] == [(2022, 200.0, 180.0)]
    assert d["diffs"][0]["diff_pct"] == -0.1
    md = dp.restatement_section(d)
    assert "最新重編版" in md and "首次公布版" in md and "FY2022 淨利" in md and "2024-02-20" in md
    assert dp.scrub_price_text(md)[1] == 0


def test_build_dossier_base_writes_restatements_and_buyback_to_price_file(tmp_path):
    facts = _facts()
    def dur(fy, val):
        return {"form": "10-K", "fp": "FY", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                "filed": f"{fy + 1}-02-20", "val": val}
    facts["facts"]["us-gaap"]["PaymentsForRepurchaseOfCommonStock"] = {"units": {"USD": [dur(2024, 5e8)]}}
    facts["facts"]["us-gaap"]["StockRepurchasedDuringPeriodShares"] = {"units": {"shares": [dur(2024, 5e6)]}}
    res = dp.build_dossier_base(FakeSec(facts=facts), "BRK.B", "2026-10-15", tmp_path / "out",
                                price=86.1, price_date="2026-10-14", rates=None)
    text = res["md"].read_text(encoding="utf-8")
    assert "首次公布版與最新重編版對照" in text
    assert (tmp_path / "out" / "_restatements.json").is_file()
    assert "5,000,000" not in text and "5e+06" not in text
    data = json.loads(res["price_json"].read_text(encoding="utf-8"))
    assert data["buyback_shares_by_fy"] == {"2024": 5e6}
    assert data["avg_buyback_price_by_fy"] == {"2024": 100.0}
    assert res["industry"] is None


def test_insurance_section_and_float():
    def dur(fy, val):
        return {"form": "10-K", "fp": "FY", "start": f"{fy}-01-01", "end": f"{fy}-12-31",
                "filed": f"{fy + 1}-02-20", "val": val}
    def pt(fy, val):
        return {"form": "10-K", "fp": "FY", "end": f"{fy}-12-31", "filed": f"{fy + 1}-02-20", "val": val}
    us = {"PremiumsEarnedNet": {"units": {"USD": [dur(y, 1000e6) for y in range(2015, 2025)]}},
          "PolicyholderBenefitsAndClaimsIncurredNet": {"units": {"USD": [dur(y, 700e6) for y in range(2015, 2025)]}},
          "LiabilityForClaimsAndClaimsAdjustmentExpense": {"units": {"USD": [pt(y, 2000e6) for y in range(2015, 2025)]}},
          "UnearnedPremiums": {"units": {"USD": [pt(y, 500e6) for y in range(2015, 2025)]}},
          "AmortizationOfDeferredPolicyAcquisitionCosts": {"units": {"USD": [dur(y, 150e6) for y in range(2015, 2025)]}},
          "PremiumsReceivableAtCarryingValue": {"units": {"USD": [pt(y, 100e6) for y in range(2015, 2025)]}},
          "ReinsuranceRecoverables": {"units": {"USD": [pt(y, 200e6) for y in range(2018, 2025)]}}}
    md = dp.insurance_section({"facts": {"us-gaap": us}}, "2026-10-15")
    assert "70.0%" in md and "P-58" in md and "2015" in md and "2024" in md
    flt = next(ln for ln in md.splitlines() if "近似浮存金" in ln)
    cells = [c.strip() for c in flt.split("|")[2:-1]]
    assert cells == ["缺"] * 3 + ["2,200"] * 7                     # 再保應收缺的年度不算浮存金
    cr = next(ln for ln in md.splitlines() if "近似綜合率" in ln)
    assert [c.strip() for c in cr.split("|")[2:-1]] == ["缺"] * 10  # 其他承保費用缺 → 不算（否則嚴重低估）


def test_mask_buyback_tables_one_cell_per_line_wtw_style():
    """WTW 2026-02-25 10-K（HTML 轉文字後每格一行）：股數與均價格各占一行，逐行刪價格句會留下 $321.10。"""
    raw = ("The following table presents specified information about the Company's repurchases of ordinary shares "
           "for the year ended December 31, 2025:\n\nYear ended\n\nDecember 31, 2025\n\nShares repurchased\n\n"
           "5,138,535\n\nAverage price paid per share\n\n$321.10\n\n"
           "Aggregate repurchase cost (excluding broker costs)\n\n$1.6 billion\n\nNet income 1,000\n")
    out, n = dp.mask_buyback_shares(raw)
    out, _ = dp.scrub_price_text(out)
    assert "5,138,535" not in out and "$321.10" not in out and n == 3
    assert "$1.6 billion" not in out and "Net income 1,000" in out


def test_drop_price_sections_accepts_lettered_issuer_heading_and_split_labels(tmp_path):
    """WTW 10-Q：「(c) Issuer Purchases of Equity Securities」帶前綴；表頭「Average price」「paid per share」分兩行。"""
    raw = ("ITEM 2. UNREGISTERED SALES OF EQUITY SECURITIES\nRSUs issued.\n"
           "(c) Issuer Purchases of Equity Securities\nTotal number of shares purchased\nAverage price\npaid per share\n"
           "April 1, 2026 through April 30, 2026\n185,609\n$\n290.80\n"
           "ITEM 3. DEFAULTS UPON SENIOR SECURITIES\nNone.\n")
    out, n = dp.drop_price_sections(raw)
    assert n == 1 and "290.80" not in out and "185,609" not in out and "RSUs issued." in out and "None." in out
    # 標題缺時，表格模式仍遮蔽數字格
    body = raw.replace("(c) Issuer Purchases of Equity Securities\n", "")
    out, k = dp.mask_buyback_shares(body)
    assert "290.80" not in out and "185,609" not in out and k == 3


def test_mask_handles_of_its_outstanding_shares():
    out, n = dp.mask_buyback_shares("the Company repurchased 1,733,574 of its outstanding shares for $450 million.")
    assert n == 2 and "1,733,574" not in out and "$450 million" not in out


@pytest.mark.parametrize("src,num", PER_SHARE_SAMPLES)
def test_sanitize_masks_per_share_prices(src, num):
    out, st = dp.sanitize_filing_text(src)
    assert num not in out
    assert st["per_share_masked"] + st["buyback_masked"] + st["removed_lines"] >= 1


# ---------- 以下兩則自 test_hark_queue.py 移來（框架測試，與 hark_queue 無關；P34／M8-D10-01） ----------
def test_drop_price_sections_stops_at_unnumbered_heading():
    from trader.analysis.dossier_prep import drop_price_sections
    body = "x\n" * 10
    text = ("ITEM 5. Market for Registrant's Common Equity\nIssuer Purchases of Equity Securities\n"
            "Average Price Paid per Share $191.95\n" + body +
            "FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA\nConsolidated Balance Sheet\n" + "y\n" * 50000)
    out, n = drop_price_sections(text)
    assert n >= 1 and "Consolidated Balance Sheet" in out and "$191.95" not in out


def test_drop_price_sections_caps_length():
    from trader.analysis.dossier_prep import MAX_PRICE_SECTION_CHARS, drop_price_sections
    text = "ITEM 5. Market for Registrant's Common Equity\n" + "z" * (MAX_PRICE_SECTION_CHARS * 3)
    out, _ = drop_price_sections(text)
    assert len(out) >= MAX_PRICE_SECTION_CHARS
