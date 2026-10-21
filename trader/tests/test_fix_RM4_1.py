"""RM4-1／RM4-1R（M4 複審，資料包資料面）：M4-RS1-01、M4-RS1-02、M4-R5-01、M4-D5-04、M4-R4S4-01、M4-R2-01、M4-D1-02；
RM4-1R：D1-02 改「最近 2 份一致否則全歷史眾數」（CRWD 回歸）、_pick_filings 傳 since。
手寫 companyfacts／申報清單不連網；另以 repo 的 state/cache/edgar 快取離線驗證（快取不在時 skip）。"""
import json
from pathlib import Path

import pytest

from trader.analysis import dossier_prep as dp

from test_dossier_prep import CIK, FakeSec, _f
from test_fix_F04 import B, F, cal, calp, facts, inst  # noqa: F401（fixture）

ASOF = "2026-10-15"
EDGAR = Path(__file__).resolve().parents[2] / "state" / "cache" / "edgar"


def _rows(md):
    return {ln.split("|")[1].strip(): [c.strip() for c in ln.split("|")[2:-1]]
            for ln in md.splitlines() if ln.startswith("| ") and not ln.startswith("| 百萬美元")}


def _cached(cik):
    p = EDGAR / f"CIK{cik}.json"
    if not p.exists():
        pytest.skip(f"無快取 {p}")
    return json.loads(p.read_text())


# ---------------------------------------------------------------- M4-RS1-01：一年內到期債務缺 → 0†

def _bs(debt_cur=False, cash=True):
    ys = (2023, 2024)
    us = {"NetIncomeLoss": [cal(y, 80e6) for y in ys], "OperatingIncomeLoss": [cal(y, 100e6) for y in ys],
          "IncomeTaxExpenseBenefit": [cal(y, 20e6) for y in ys],
          "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
              [cal(y, 100e6) for y in ys],
          "AssetsCurrent": [calp(y, 500e6) for y in ys], "LiabilitiesCurrent": [calp(y, 400e6) for y in ys],
          "PropertyPlantAndEquipmentNet": [calp(y, 200e6) for y in ys]}
    if cash:
        us["CashAndCashEquivalentsAtCarryingValue"] = [calp(y, 100e6) for y in ys]
    if debt_cur:
        us["LongTermDebtCurrent"] = [calp(y, 50e6) for y in ys]
    return facts(us)


def test_supplement_missing_debt_cur_counts_as_zero_with_dagger():
    md = dp.supplement_section(_bs(), ASOF, years=2)
    r = _rows(md)
    assert r["其中一年內到期債務（一年內到期長期債＋短期借款）"] == ["0†", "0†"]
    # 營運資金＝(500−100)−(400−0)=0；有形資本＝200；稅後營業利益 80 → 40.0%
    assert r["有形資本＝營運資金＋固定資產淨額"] == ["200", "200"]
    assert r["稅後無槓桿有形資本報酬"] == ["40.0%", "40.0%"]
    assert "† 2023、2024：一年內到期債務" in md and "以 0 計" in md


def test_supplement_debt_cur_present_no_dagger_and_cash_missing_still_missing():
    md = dp.supplement_section(_bs(debt_cur=True), ASOF, years=2)
    assert "†" not in md.split("SEL-055")[1].split("\n", 1)[1]   # 說明文字之後沒有 † 格與註腳
    assert _rows(md)["稅後無槓桿有形資本報酬"] == ["32.0%", "32.0%"]
    md = dp.supplement_section(_bs(cash=False), ASOF, years=2)
    r = _rows(md)
    assert r["稅後無槓桿有形資本報酬"] == ["缺", "缺"] and "0†" not in md


@pytest.mark.parametrize("cik", ["0000866787", "0001403568", "0001324404"])   # AZO、ULTA、CF
def test_supplement_offline_azo_ulta_cf_latest_year_has_return(cik):
    md = dp.supplement_section(_cached(cik), ASOF)
    ret = _rows(md)["稅後無槓桿有形資本報酬"]
    assert ret[-1] != "缺", md
    assert "0†" in md and "以 0 計" in md


# ---------------------------------------------------------------- M4-RS1-02：有債無利息 → 警示

def test_debt_without_interest_warns(B):
    us = {"NetIncomeLoss": [cal(y, 10e6) for y in (2024, 2025)],
          "LongTermDebtNoncurrent": [calp(y, 500e6) for y in (2024, 2025)]}
    T, (R, filed, last, warn, bands) = B.build(facts(us), "T", ASOF)
    assert any("FY2024、FY2025：有有息負債但利息費用 XBRL 缺" in w for w in warn), warn
    assert any(w.startswith("利息費用：10 年中缺") for w in warn)


def test_interest_expense_operating_tag_counts(F, B):
    us = {"NetIncomeLoss": [cal(2025, 10e6)], "LongTermDebtNoncurrent": [calp(2025, 500e6)],
          "InterestExpenseOperating": [cal(2025, 20e6)]}
    assert F.annual_series(facts(us), F.TAGS["interest"], ASOF)[2025] == 20e6
    T, (R, filed, last, warn, bands) = B.build(facts(us), "T", ASOF)
    assert not any("利息費用 XBRL 缺" in w for w in warn)


# ---------------------------------------------------------------- M4-R2-01：拆股跳動年不給增減級距

def test_share_jump_year_band_is_split_basis(B):
    us = {"NetIncomeLoss": [cal(y, 10e6) for y in (2023, 2024, 2025)],
          "WeightedAverageNumberOfDilutedSharesOutstanding": [cal(2023, 100e6), cal(2024, 400e6), cal(2025, 399e6)]}
    T, (R, filed, last, warn, bands) = B.build(facts(us, {"WeightedAverageNumberOfDilutedSharesOutstanding": "shares"}),
                                               "T", ASOF)
    col = bands["稀釋股數年變化 級距"]
    assert col[2024] == "疑似拆股基準（見警示）" and col[2025] == "±1% 內"
    assert any("FY2024：稀釋股數為前一年的 4.00 倍" in w for w in warn)


# ---------------------------------------------------------------- M4-D1-02：最近 2 份 10-K 一致用它，否則全歷史眾數

def _fy_facts(inst, diffs):
    """依申報先後給各份 10-K 的 fy−_fy_base 差（1 月期末）。"""
    return [inst(f"{2010 + i}-01-30", 1, f"{2010 + i}-03-30", fy=2009 + i + d, accn=f"a{i}") for i, d in enumerate(diffs)]


def test_fiscal_year_offset_votes_recent_five_only(F):
    """TJX 型：舊 11 份 10-K 稱前一年（差 0），近 5 份改稱期末年（差 1）→ 最近 2 份一致，依現在的自稱 offset 1。"""
    A = [inst(f"{y}-01-30", 1, f"{y}-03-30", fy=y - 1, accn=f"o{y}") for y in range(2015, 2019)]
    A += [inst(f"{y}-01-30", 1, f"{y}-03-30", fy=y, accn=f"n{y}") for y in range(2019, 2024)]
    A += [inst(f"{y}-01-30", 1, f"{y}-03-30", fy=y - 1, accn=f"p{y}") for y in range(2008, 2015)]   # 更舊 7 份
    assert F.fiscal_year_offset(facts({"Assets": A})) == 1


def test_fiscal_year_offset_crwd_type_falls_back_to_full_history(F):
    """CrowdStrike 型 [1,1,1,0,0,0,1]：最近 2 份不一致 → 全歷史眾數 1（最近 5 份眾數 0 會少一年）。"""
    assert F.fiscal_year_offset(facts({"Assets": _fy_facts(inst, [1, 1, 1, 0, 0, 0, 1])})) == 1


def test_fiscal_year_offset_single_bad_latest_falls_back(F):
    """Salesforce 型：最新一份 fy 欄填錯（差 0），其餘皆 1 → 1；最近 2 份一致的少數派仍以最近為準（TJX 型）。"""
    assert F.fiscal_year_offset(facts({"Assets": _fy_facts(inst, [1, 1, 1, 1, 1, 0])})) == 1
    assert F.fiscal_year_offset(facts({"Assets": _fy_facts(inst, [0, 0, 0, 0, 0, 1, 1])})) == 1


@pytest.mark.parametrize("cik,end,fy", [("0001535527", "2026-01-31", 2026),    # CRWD：fiscal 2026（RM4-1 回歸）
                                        ("0000109198", "2026-01-31", 2026),    # TJX：fiscal 2026
                                        ("0000016918", "2026-02-28", 2026),    # STZ：fiscal 2026
                                        ("0000060667", "2026-01-30", 2025)])   # LOW：fiscal 2025（不變）
def test_fiscal_year_label_offline_tjx_stz_low(F, cik, end, fy):
    s = F.annual_series(_cached(cik), F.TAGS["net_income"], ASOF)
    assert int(s.index.max()) == fy and s.attrs["end"][fy] == end


# ---------------------------------------------------------------- M4-R5-01／M4-D5-04：委託書挑選與 Part III 註記

def test_def14c_does_not_displace_annual_def14a(tmp_path):
    """DASH 情境：2026-04-20 年度 DEF 14A，2026-08-27 公司行動 DEF 14C → 收 DEF 14A，DEF 14C 略過留痕。"""
    fl = [_f("10-K", "2026-02-20", 5), _f("DEF 14A", "2026-04-20", 7), _f("DEF 14C", "2026-08-27", 8)]
    texts = {fl[0]["url"]: "ANNUAL", fl[1]["url"]: "Summary Compensation Table", fl[2]["url"]: "INFORMATION STATEMENT"}
    notes: list = []
    out = dp.fetch_sec_texts(FakeSec(filings=fl, texts=texts), CIK, ASOF, tmp_path, notes=notes)
    forms = [r["form"] for r in out]
    assert "DEF 14A" in forms and "DEF 14C" not in forms
    assert any(n.startswith("略過：DEF 14C 2026-08-27") for n in notes), notes


def test_def14c_used_when_no_recent_def14a(tmp_path):
    """ERIE 型：15 個月內無 DEF 14A（只有很舊的）→ 退用最新 DEF 14C。"""
    fl = [_f("10-K", "2026-02-20", 5), _f("DEF 14A", "2024-03-01", 7), _f("DEF 14C", "2026-03-10", 8)]
    texts = {f["url"]: "X" for f in fl}
    got = dp._pick_filings(FakeSec(filings=fl, texts=texts), CIK, ASOF, 3)
    assert [f["form"] for f in got["DEF 14A"]] == ["DEF 14C"] and got["proxy_skipped"] == []


_PART3 = ("PART III\nItem 10. Directors\nItem 11. Executive Compensation\nItem 12. Security Ownership\n"
          "Item 13. Relationships\nItem 14. Accountant Fees\n")


def test_part3_only_note(tmp_path):
    """PSKY 情境：只有 Part III 10-K/A、無委託書 → 不寫『缺』，改指 10-K/A。"""
    fl = [_f("10-K", "2026-02-20", 5), _f("10-K/A", "2026-04-24", 6)]
    texts = {fl[0]["url"]: "ANNUAL", fl[1]["url"]: _PART3}
    notes: list = []
    out = dp.fetch_sec_texts(FakeSec(filings=fl, texts=texts), CIK, ASOF, tmp_path, notes=notes)
    assert "10-K/A" in [r["form"] for r in out]
    assert not any("委託書" in n and n.startswith("缺") for n in notes), notes
    assert any("Part III" in n and "10-K/A 2026-04-24" in n for n in notes), notes


def test_no_proxy_no_part3_still_missing(tmp_path):
    fl = [_f("10-K", "2026-02-20", 5)]
    notes: list = []
    dp.fetch_sec_texts(FakeSec(filings=fl, texts={fl[0]["url"]: "ANNUAL"}), CIK, ASOF, tmp_path, notes=notes)
    assert "缺：決策日前無 DEF 14A／DEF 14C 委託書" in notes


def test_stale_proxy_points_to_newer_part3(tmp_path):
    fl = [_f("10-K", "2026-02-20", 5), _f("DEF 14A", "2025-03-01", 7), _f("10-K/A", "2026-04-24", 6)]
    texts = {fl[0]["url"]: "ANNUAL", fl[1]["url"]: "PROXY", fl[2]["url"]: _PART3}
    notes: list = []
    dp.fetch_sec_texts(FakeSec(filings=fl, texts=texts), CIK, ASOF, tmp_path, notes=notes)
    n = next(n for n in notes if "15 個月" in n)
    assert "10-K/A 2026-04-24" in n and "可能缺最新年度薪酬" not in n


# ---------------------------------------------------------------- M4-R4S4-01：申報時點段傳 since

class _SinceSec(FakeSec):
    def filings(self, cik, asof, since=None, forms=None):
        self.filing_calls.append({"asof": asof, "since": since, "forms": forms})
        return [f for f in super().filings(cik, asof, forms=forms) if since is None or f["filingDate"] > since]


def test_filing_history_passes_since_36_months():
    sec = _SinceSec(filings=[_f("10-K", "2025-02-20", 5), _f("NT 10-Q", "2024-05-01", 6)])
    md = dp.filing_history_section(sec, CIK, ASOF)
    assert sec.filing_calls[0]["since"] == "2023-10-15"
    assert "最新 10-K：2025-02-20" in md and "2024-05-01 NT 10-Q" in md
    sec = _SinceSec(filings=[_f("10-K", "2022-02-20", 5)])
    assert "缺：決策日前 3 年內無 10-K" in dp.filing_history_section(sec, CIK, ASOF)


class _BankSec(_SinceSec):
    """大銀行型：recent 只涵蓋約 12 個月；沒傳 since（不補讀舊分頁）就看不到更早的申報。"""
    def filings(self, cik, asof, since=None, forms=None):
        rows = super().filings(cik, asof, since=since, forms=forms)
        return rows if since is not None else [f for f in rows if f["filingDate"] > "2025-10-14"]


def test_pick_filings_passes_since_for_old_pages():
    """RM4-1R：_pick_filings 各查詢傳 since＝決策日−36 個月（同 M4-R4S4-01），recent 只有 12 個月時仍抓到 10-K 與 DEF 14A。"""
    fl = [_f("10-K", "2025-02-20", 5), _f("DEF 14A", "2025-03-15", 6), _f("10-Q", "2025-11-01", 7),
          _f("8-K", "2025-12-01", 8)]
    sec = _BankSec(filings=fl)
    got = dp._pick_filings(sec, CIK, "2026-01-30", 3)
    calls = [c for c in sec.filing_calls if "since" in c]   # FakeSec 本身也記一筆（無 since 鍵），只看 _SinceSec 的 4 次查詢
    assert len(calls) == 4 and {c["since"] for c in calls} == {"2023-01-28"}
    assert [f["filingDate"] for f in got["10-K"]] == ["2025-02-20"]
    assert [f["filingDate"] for f in got["DEF 14A"]] == ["2025-03-15"]
    assert [f["filingDate"] for f in got["10-Q"]] == ["2025-11-01"] and len(got["8-K"]) == 1
