"""修復 F04（2026-10-18 審查）：XBRL 取數口徑（fetch_edgar）、資料包 10 年表與回購級距（build_dossier）、
框架補充表（dossier_prep）。全部用手寫 companyfacts，不連網。

涵蓋：M1-D1-01／D1-02／D1-04／D2-08（Q-IMPUTE）、M4-D1-01～08、M4-D1-16、M4-D2-01／02／03／05／07／11、
M4-D4-03、M4-D5-02、M4-D10-01、M2_D1_01、M2_D6_01、M2_D9_08、M2-D5-06。
"""
import importlib
import json
import sys

import pandas as pd
import pytest

from trader.analysis import dossier_prep as dp
from trader.paths import get_paths

ASOF = "2026-10-15"


def _skill(module: str, sub: str):
    d = str(get_paths().root / sub / "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module(module)


@pytest.fixture(scope="module")
def F():
    return _skill("fetch_edgar", "berkshire-screen-merged")


@pytest.fixture(scope="module")
def B():
    _skill("fetch_edgar", "berkshire-screen-merged")
    return _skill("build_dossier", "berkshire-deep-merged")


def dur(start, end, val, filed, fy=None, fp="FY", form="10-K", accn=None):
    r = {"form": form, "fp": fp, "start": start, "end": end, "filed": filed, "val": val}
    if fy is not None:
        r["fy"] = fy
    if accn:
        r["accn"] = accn
    return r


def inst(end, val, filed, fy=None, fp="FY", accn=None):
    r = {"form": "10-K", "fp": fp, "end": end, "filed": filed, "val": val}
    if fy is not None:
        r["fy"] = fy
    if accn:
        r["accn"] = accn
    return r


def cal(y, val, filed=None, **kw):
    return dur(f"{y}-01-01", f"{y}-12-31", val, filed or f"{y + 1}-02-20", **kw)


def calp(y, val, filed=None, **kw):
    return inst(f"{y}-12-31", val, filed or f"{y + 1}-02-20", **kw)


def facts(us: dict, unit_map: dict | None = None):
    unit_map = unit_map or {}
    return {"cik": 1, "entityName": "TEST",
            "facts": {"us-gaap": {k: {"units": {unit_map.get(k, "USD"): v}} for k, v in us.items()}}}


# ---------------------------------------------------------------- fetch_edgar：年度判定與標籤（M4-D1-01／02／03、M1-D1-04）

def test_10k_with_fp_q4_is_kept_and_first_version_used(F):
    """HIG 型：年報 fp 標成 Q4 也要收；同財年取首次申報。"""
    f = facts({"Revenues": [cal(2017, 16974, "2018-02-23", fp="Q4"), cal(2017, 17162, "2020-02-21", fp="FY")]})
    s = F.annual_series(f, ["Revenues"], ASOF)
    assert s[2017] == 16974 and s.attrs["filed"][2017] == "2018-02-23"


def test_52_53_week_year_ends_do_not_collide(F):
    ends = ["2022-07-01", "2023-06-30", "2024-06-28", "2025-06-27", "2026-07-03"]
    rows, prev = [], "2021-07-02"
    for i, e in enumerate(ends):
        rows.append(dur(prev, e, 100 + i, f"{e[:4]}-08-20"))
        prev = e
    s = F.annual_series(facts({"Revenues": rows}), ["Revenues"], ASOF)
    assert len(s) == 5 and list(s.values) == [100, 101, 102, 103, 104]


def test_fiscal_year_label_follows_company_fy(F):
    """5/31 期末公司自稱 FY2026（DocumentFiscalYearFocus＝2026）→ 標 2026；1 月底零售商（自稱 FY2025）標籤不變。"""
    fdx = facts({"Revenues": [dur("2025-06-01", "2026-05-31", 94720, "2026-07-20", fy=2026, accn="a26"),
                              dur("2024-06-01", "2025-05-31", 87926, "2025-07-20", fy=2025, accn="a25")],
                 "Assets": [inst("2026-05-31", 1, "2026-07-20", fy=2026, accn="a26"),
                            inst("2025-05-31", 1, "2025-07-20", fy=2025, accn="a25")]})
    s = F.annual_series(fdx, ["Revenues"], ASOF)
    assert list(s.index) == [2025, 2026] and s[2026] == 94720
    low = facts({"Revenues": [dur("2025-02-01", "2026-01-30", 86286, "2026-03-23", fy=2025, accn="b")],
                 "Assets": [inst("2026-01-30", 1, "2026-03-23", fy=2025, accn="b")]})
    assert list(F.annual_series(low, ["Revenues"], ASOF).index) == [2025]


def test_fiscal_year_offset_ignores_single_bad_fy_field(F):
    """STX 型：一份 10-K 的 fy 欄填錯（2027），以眾數為準。"""
    A = [inst(f"{y}-06-28", 1, f"{y}-08-01", fy=y, accn=f"x{y}") for y in (2022, 2023, 2024, 2026)]
    A.append(inst("2025-06-27", 1, "2025-08-01", fy=2027, accn="x2025"))
    assert F.fiscal_year_offset(facts({"Assets": A})) == 1


def test_quarter_end_instant_in_10k_is_not_an_annual_value(F):
    rows = [calp(2024, 500), inst("2024-09-30", 999, "2025-02-20"), calp(2023, 400, "2025-02-20")]
    s = F.annual_series(facts({"StockholdersEquity": rows}), ["StockholdersEquity"], ASOF)
    assert dict(s) == {2023: 400, 2024: 500}


def test_first_version_across_tags_uses_earliest_filing(F):
    """MSFT 型：FY2017 原始年報用 SalesRevenueNet；次年以新標籤重編比較期。首次版取原始年報。"""
    f = facts({"SalesRevenueNet": [cal(2017, 89950, "2018-02-20")],
               "RevenueFromContractWithCustomerExcludingAssessedTax": [cal(2017, 96571, "2019-02-20"),
                                                                      cal(2018, 110360, "2019-02-20")]})
    tags = F.TAGS["revenue"]
    first = F.annual_series(f, tags, ASOF)
    latest = F.annual_series(f, tags, ASOF, latest=True)
    assert first[2017] == 89950 and first.attrs["tag"][2017] == "SalesRevenueNet"
    assert latest[2017] == 96571 and first[2018] == 110360


# ---------------------------------------------------------------- fetch_edgar：標籤清單（M1-D1-01、M4-D1-04～07）

def test_net_income_prefers_attributable_over_profitloss(F):
    """CF 型：無 NetIncomeLoss，有 ProfitLoss（含少數股權）與歸屬普通股 → 用歸屬普通股。"""
    f = facts({"ProfitLoss": [cal(2025, 1798)], "NetIncomeLossAvailableToCommonStockholdersBasic": [cal(2025, 1455)]})
    assert F.annual_series(f, F.TAGS["net_income"], ASOF)[2025] == 1455


def test_equity_does_not_fall_back_to_nci_total(F):
    f = facts({"StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": [calp(2017, 6492)]})
    assert F.annual_series(f, F.TAGS["equity"], ASOF).empty


def test_interest_uses_income_statement_not_cash_paid(F):
    f = facts({"InterestPaidNet": [cal(2025, 881)], "InterestExpenseNonoperating": [cal(2025, 970)]})
    assert F.annual_series(f, F.TAGS["interest"], ASOF)[2025] == 970
    assert F.annual_series(facts({"InterestPaidNet": [cal(2025, 881)]}), F.TAGS["interest"], ASOF).empty


def test_debt_series_sums_groups_without_double_count(F):
    # FDX 型：非流動長期債＋一年內到期（含租賃標籤）＋短期借款
    f = facts({"LongTermDebtNoncurrent": [calp(2025, 23293)], "LongTermDebt": [calp(2025, 23455)],
               "LongTermDebtAndCapitalLeaseObligationsCurrent": [calp(2025, 1676)],
               "ShortTermBorrowings": [calp(2025, 745)]})
    d = F.debt_series(f, ASOF)
    assert d["debt_lt"][2025] == 23293 and d["debt_cur"][2025] == 1676 + 745 and d["debt"][2025] == 23293 + 1676 + 745
    # PHM 型：合資債務 LongTermDebt 很小，自家 SeniorNotes 才是主債務
    f = facts({"LongTermDebt": [calp(2025, 44)], "SeniorNotes": [calp(2025, 1584)]})
    assert F.debt_series(f, ASOF)["debt"][2025] == 1584
    # 只有 LongTermDebt（已含一年內到期）→ 不再加一年內到期長期債，只加短期借款
    f = facts({"LongTermDebt": [calp(2025, 1000)], "LongTermDebtCurrent": [calp(2025, 100)],
               "ShortTermBorrowings": [calp(2025, 50)]})
    assert F.debt_series(f, ASOF)["debt"][2025] == 1050
    # PGR／GS 型：只有長短期合計
    f = facts({"LongTermDebtCurrent": [calp(2025, 0)], "DebtLongtermAndShorttermCombinedAmount": [calp(2025, 6897)]})
    assert F.debt_series(f, ASOF)["debt"][2025] == 6897


def test_debt_missing_year_is_nan_not_zero(F):
    f = facts({"LongTermDebtNoncurrent": [calp(2023, 500)]})
    d = F.debt_series(f, ASOF)["debt"]
    assert d[2023] == 500 and 2024 not in d.index


def _feat_facts(debt_last=True):
    us = {"NetIncomeLoss": [cal(y, 100 + y - 2016) for y in range(2016, 2026)],
          "Revenues": [cal(y, 1000) for y in range(2016, 2026)],
          "StockholdersEquity": [calp(y, 1000) for y in range(2016, 2026)],
          "Assets": [calp(y, 3000) for y in range(2016, 2026)],
          "LongTermDebtNoncurrent": [calp(y, 300) for y in range(2016, 2026 if debt_last else 2025)],
          "WeightedAverageNumberOfDilutedSharesOutstanding": [cal(y, 1e6) for y in range(2016, 2026)]}
    return facts(us, {"WeightedAverageNumberOfDilutedSharesOutstanding": "shares"})


def test_features_latest_year_debt_missing_gives_nan_de(F):
    """Q-IMPUTE（使用者選 1）：最近一年有息負債標籤缺 → D/E 缺值（計中性），不是 0（最好）。"""
    r = F.features("T", "1", _feat_facts(debt_last=False), "3500", ASOF, False)
    assert pd.isna(r["debt_to_equity"]) and "最近一年有息負債缺" in r["data_notes"]
    r = F.features("T", "1", _feat_facts(), "3500", ASOF, False)
    assert r["debt_to_equity"] == pytest.approx(0.3)
    assert r["shares_3y_ago"] == 1e6 and r["shares_3y_filed"] == "2023-02-20"


# ---------------------------------------------------------------- build_dossier：10 年表、回購級距、警示

def _dossier_facts():
    years = range(2016, 2026)
    us = {"NetIncomeLoss": [cal(y, 1000e6) for y in years],
          "Revenues": [cal(y, 10000e6) for y in years],
          "StockholdersEquity": [calp(y, (500e6 if y < 2022 else -800e6)) for y in years],
          "Assets": [calp(y, 20000e6) for y in years],
          "DepreciationDepletionAndAmortization": [cal(y, 300e6) for y in years],
          "PaymentsToAcquirePropertyPlantAndEquipment": [cal(y, 300e6) for y in years],
          "PaymentsForRepurchaseOfCommonStock": [cal(y, 400e6) for y in years],
          "TreasuryStockSharesAcquired": [cal(2022, 1830022)],
          "TreasuryStockValueAcquiredCostMethod": [cal(2022, 180387000)],
          "StockRepurchasedDuringPeriodShares": [cal(2022, 46555), cal(2023, 2e6), cal(2023, 2e6, "2025-02-20"),
                                                 cal(2024, 0)],
          "EarningsPerShareDiluted": [cal(y, 1.01) for y in years],
          "WeightedAverageNumberOfDilutedSharesOutstanding": [cal(y, 990e6 if y != 2020 else 9900e6) for y in years],
          "InterestExpense": [cal(y, 50e6) for y in years],
          "LongTermDebtNoncurrent": [calp(y, 1000e6) for y in years if y != 2019]}
    return facts(us, {"TreasuryStockSharesAcquired": "shares", "StockRepurchasedDuringPeriodShares": "shares",
                      "WeightedAverageNumberOfDilutedSharesOutstanding": "shares",
                      "EarningsPerShareDiluted": "USD/shares"})


def test_dossier_table_has_no_buyback_amounts_but_has_bands(B):
    """09:42 決策：10 年表不列回購金額／股數；只給回購÷業主盈餘近似級距與股數變化級距。"""
    T, (R, filed, last, warn, bands) = B.build(_dossier_facts(), "T", ASOF)
    ends = {}
    tbl, rtbl = B.render(T, R, bands, filed, ends)
    assert "400" not in tbl and "buyback" not in tbl and "回購" not in tbl
    assert "回購÷業主盈餘近似 級距" in rtbl and "25–50%" in rtbl     # 400 ÷（1000＋300−300）＝40%
    assert "稀釋股數年變化 級距" in rtbl
    assert "e+" not in tbl and "e+" not in rtbl                       # 不用科學記號
    assert "1,000" in tbl and "1.01" in tbl                            # 金額百萬美元、EPS 兩位小數


def test_dossier_negative_equity_and_missing_debt(B):
    """M4-D2-11／M2-D5-06：權益 ≤0 年度 ROE、有息負債÷權益印「不適用」；M4-D2-05：有息負債缺不當 0。"""
    T, (R, filed, last, warn, bands) = B.build(_dossier_facts(), "T", ASOF)
    _, rtbl = B.render(T, R, bands, filed, {})
    de = next(ln for ln in rtbl.splitlines() if ln.startswith("| 有息負債÷權益"))
    assert de.count(B.NA_EQ) == 4                                      # FY2022–2025
    roe = next(ln for ln in rtbl.splitlines() if ln.startswith("| ROE"))
    assert B.NA_EQ in roe and "-" not in roe.split("|")[-2]
    assert pd.isna(T.debt[2019]) and pd.isna(R["有息負債÷權益（權益≤0 不適用）"][2019])
    assert any("FY2019：有息負債 XBRL 缺" in w for w in warn)
    assert any("股東權益 ≤0" in w and "FY2022" in w for w in warn)


def test_dossier_share_jump_warning(B):
    T, (R, filed, last, warn, bands) = B.build(_dossier_facts(), "T", ASOF)
    assert any(w.startswith("FY2020：稀釋股數為前一年的 10.00 倍") for w in warn)


def test_dossier_eps_check_uses_same_version_net_income(B):
    """M2_D9_08：EPS、股數是最新版；淨利重編時用最新版淨利比對，不誤報。"""
    f = _dossier_facts()
    f["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].append(cal(2021, 1400e6, "2023-02-20"))
    f["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"].append(cal(2021, 1.414, "2023-02-20"))
    T, (R, filed, last, warn, bands) = B.build(f, "T", ASOF)
    assert T.net_income[2021] == 1000e6                                   # 表為首次版
    assert not any(w.startswith("FY2021：淨利÷稀釋股數") for w in warn)
    assert any(w.startswith("FY2021：淨利已重編") for w in warn)


def test_buyback_price_data_pairs_and_guards(B):
    """M2_D1_01：同表成對（庫藏股股數÷庫藏股成本）；股數 0 不給；無成對金額且兩股數概念差 >1.5 倍不給。"""
    f = _dossier_facts()
    f["facts"]["us-gaap"]["TreasuryStockSharesAcquired"]["units"]["shares"].append(cal(2023, 4e6))
    d = B.buyback_price_data(f, ASOF)
    assert d["avg_buyback_price_by_fy"][2022] == pytest.approx(98.5709, abs=1e-3)
    assert "TreasuryStockValueAcquiredCostMethod" in d["avg_buyback_basis"][2022]
    assert 2023 not in d["avg_buyback_price_by_fy"] and any("FY2023" in w for w in d["avg_buyback_warnings"])
    assert 2024 not in d["avg_buyback_price_by_fy"]
    assert d["buyback_amount_by_fy"][2024] == 400e6


def test_dossier_tag_switch_warning(B):
    """M4-D4-03：同列換標籤要標（GL 型 debt_cur 2024 換標籤）。"""
    f = _dossier_facts()
    us = f["facts"]["us-gaap"]
    us["LongTermDebtCurrent"] = {"units": {"USD": [calp(y, 10e6) for y in range(2016, 2024)]}}
    us["ShortTermBorrowings"] = {"units": {"USD": [calp(y, 5e6) for y in range(2016, 2026)]}}
    T, (R, filed, last, warn, bands) = B.build(f, "T", ASOF)
    assert any(w.startswith("有息負債合計：換標籤") and "FY2024" in w for w in warn)


# ---------------------------------------------------------------- dossier_prep：補充表、重編表、保險

def test_supplement_years_follow_dossier_table_when_netincomeloss_stops(F):
    """M4-D2-01：CF 型 NetIncomeLoss 只到 2011、之後只有歸屬普通股淨利 → 補充表年度與 10 年表同為 2016–2025。"""
    us = {"NetIncomeLoss": [cal(y, 10) for y in range(2005, 2012)],
          "NetIncomeLossAvailableToCommonStockholdersBasic": [cal(y, 10) for y in range(2012, 2026)],
          "AssetsCurrent": [calp(y, 100) for y in range(2016, 2026)]}
    md = dp.supplement_section(facts(us), ASOF)
    head = next(ln for ln in md.splitlines() if ln.startswith("| 百萬美元"))
    assert "2016" in head and "2025" in head and "2011" not in head


def test_supplement_missing_cash_is_not_zero():
    """M4-D1-08：現金缺的年度，營運資金與報酬記缺；現金標籤缺改用含受限現金標籤並標 *。"""
    us = {"NetIncomeLoss": [cal(y, 10e6) for y in (2023, 2024)],
          "AssetsCurrent": [calp(y, 500e6) for y in (2023, 2024)],
          "LiabilitiesCurrent": [calp(y, 300e6) for y in (2023, 2024)],
          "PropertyPlantAndEquipmentNet": [calp(y, 1000e6) for y in (2023, 2024)],
          "LongTermDebtCurrent": [calp(y, 50e6) for y in (2023, 2024)],
          "OperatingIncomeLoss": [cal(y, 200e6) for y in (2023, 2024)],
          "CashAndCashEquivalentsAtCarryingValue": [calp(2024, 100e6)]}
    md = dp.supplement_section(facts(us), ASOF, years=2)
    ret = next(ln for ln in md.splitlines() if ln.startswith("| 稅後無槓桿有形資本報酬"))
    assert [c.strip() for c in ret.split("|")[2:-1]][0] == "缺"
    us["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"] = [calp(2023, 80e6)]
    md = dp.supplement_section(facts(us), ASOF, years=2)
    cash = next(ln for ln in md.splitlines() if ln.startswith("| 現金及約當現金"))
    assert "80*" in cash


def test_supplement_debt_cur_matches_dossier_debt_cur(F):
    """M4-D10-01：補充表「其中一年內到期債務」與 10 年表 debt_cur 同一組合函式。"""
    us = {"NetIncomeLoss": [cal(2025, 1e6)], "LongTermDebtAndCapitalLeaseObligationsCurrent": [calp(2025, 1676e6)],
          "ShortTermBorrowings": [calp(2025, 745e6)]}
    md = dp.supplement_section(facts(us), ASOF, years=1)
    row = next(ln for ln in md.splitlines() if ln.startswith("| 其中一年內到期債務"))
    assert "2,421" in row and F.debt_series(facts(us), ASOF)["debt_cur"][2025] == 2421e6


def test_restatement_uses_skill_tags_and_more_rows(F):
    """M4-D10-01／M4-D5-02：重編表用 skill 的同一份標籤（淨利優先 NetIncomeLoss），並含折舊攤銷、稅、債務。"""
    us = {"NetIncomeLoss": [cal(2024, 100), cal(2024, 90, "2026-02-20")], "ProfitLoss": [cal(2024, 120)],
          "DepreciationDepletionAndAmortization": [cal(2024, 30)], "IncomeTaxExpenseBenefit": [cal(2024, 20)],
          "LongTermDebtNoncurrent": [calp(2024, 500)]}
    d = dp.restatement_data(facts(us), ASOF)
    assert d["series"]["net_income"]["first"][2024][0] == 100
    assert {"dna", "tax", "debt_lt"} <= set(d["series"])
    assert [(x["metric"], x["fy"]) for x in d["diffs"]] == [("net_income", 2024)]
    assert "buyback" not in d["series"]


def test_insurance_float_net_reserves_do_not_subtract_reinsurance():
    """M4-D1-16：只有淨額準備金的年度不扣再保應收；毛額組優先；兩組不混用。"""
    us = {"NetIncomeLoss": [cal(y, 1) for y in (2023, 2024)],
          "LiabilityForUnpaidClaimsAndClaimsAdjustmentExpenseNet": [calp(y, 1800e6) for y in (2023, 2024)],
          "LiabilityForClaimsAndClaimsAdjustmentExpense": [calp(2024, 2000e6)],
          "UnearnedPremiums": [calp(y, 500e6) for y in (2023, 2024)],
          "PremiumsReceivableAtCarryingValue": [calp(y, 100e6) for y in (2023, 2024)],
          "ReinsuranceRecoverables": [calp(y, 200e6) for y in (2023, 2024)]}
    md = dp.insurance_section(facts(us), ASOF, years=2)
    flt = next(ln for ln in md.splitlines() if "近似浮存金" in ln)
    assert [c.strip() for c in flt.split("|")[2:-1]] == ["2,200", "2,200"]   # 2023 淨額 1800＋500−100；2024 毛額 2000＋500−100−200
    basis = next(ln for ln in md.splitlines() if ln.startswith("| 浮存金所用組別"))
    assert [c.strip() for c in basis.split("|")[2:-1]] == ["淨額", "毛額"]


def test_build_dossier_base_price_file_from_single_source(tmp_path):
    """M4-D2-07／M2_D6_01：資料包無回購數字列；價格檔由 skill 一處寫入回購金額、股數、均價與依據。"""
    from test_dossier_prep import FakeSec, _facts   # noqa: PLC0415
    f = _facts()
    f["facts"]["us-gaap"]["PaymentsForRepurchaseOfCommonStock"] = {"units": {"USD": [cal(2024, 5e8)]}}
    f["facts"]["us-gaap"]["StockRepurchasedDuringPeriodShares"] = {"units": {"shares": [cal(2024, 5e6)]}}
    res = dp.build_dossier_base(FakeSec(facts=f), "BRK.B", ASOF, tmp_path / "out", price=86.1,
                                price_date="2026-10-14", rates=None)
    text = res["md"].read_text(encoding="utf-8")
    assert "500" not in text.split("## 衍生比率")[0].split("## 10 年財報")[1]
    assert "回購÷業主盈餘近似 級距" in text
    data = json.loads(res["price_json"].read_text(encoding="utf-8"))
    assert data["avg_buyback_price_by_fy"] == {"2024": 100.0}
    assert data["buyback_amount_by_fy"] == {"2024": 5e8} and data["buyback_shares_by_fy"] == {"2024": 5e6}
    assert "PaymentsForRepurchaseOfCommonStock" in data["avg_buyback_basis"]["2024"]
    assert not hasattr(dp, "_move_buyback_to_price_file")


# ---------------------------------------------------------------- 價格隔離補洞（調度者追加；check-F07 發現 FDX 10-K 漏網句型）

@pytest.mark.parametrize("src,amt", [
    ("During fiscal year 2026, we repurchased 3.3 million shares of FedEx common stock under ASR or open market "
     "transactions at an average price of $233.07 per share for a total of $776 million.", "233.07"),
    ("we repurchased approximately 0.3 million shares at an average cost of $9.15 per share.", "9.15"),
    ("at an aggregate cost of approximately $320.3 million (average price of approximately $169.43 per share)", "169.43"),
    ("aggregate purchase price of $120.3 million (weighted average price of $19.79 per share)", "19.79"),
    ("The average cost per share repurchased was $88.10.", "88.10"),
    ("average price paid of $12.10 per share", "12.10"),
    ("shares at an average of $90 a share", "90"),
    ("the Company repurchased 1.0 million shares of its common stock for approximately $175 million, "
     "or $181.59 per share.", "181.59"),
    ("restricted stock granted with a weighted-average fair value of $276.44 per share.", "276.44"),
    ("at a grant date fair market value of $186.53 per share", "186.53"),
    ("每股平均成本為 50 元", "50"), ("平均每股價格 120 美元", "120"),
])
def test_scrub_removes_average_price_per_share_sentences(src, amt):
    assert dp.scrub_price_text(src)[1] == 1
    # P12R：資料包實際走 sanitize_filing_text（D38 先遮每股金額、保留句子），不再要求句子被刪，只要求金額不在
    out, _ = dp.sanitize_filing_text(src)
    assert amt not in out
    assert dp.buyback_leaks(out) == []


@pytest.mark.parametrize("src", [
    "Diluted earnings per share were $5.12.",
    "Dividends of $1.38 per share were declared.",
    "an 18% increase in our quarterly dividends from $0.22 to $0.26 per share",
    "the Company may repurchase shares of its common stock, par value $0.01 per share, with no expiration",
    "The weighted average cost of debt was 4.2% on $2.1 billion of borrowings.",
    "Average cost of borrowings was $1.2 billion.",
    "Average selling price per unit increased to $45,000.",
    "average price of oil fell. Diluted EPS was $5 per share.",
    "Share repurchases had a benefit of $0.21 per diluted share in 2026.",
])
def test_scrub_keeps_eps_dividend_and_non_price_averages(src):
    assert dp.scrub_price_text(src)[1] == 0


# ---------------------------------------------------------------- F04R 返工（check-F04 的 PARTIAL）

def test_fiscal_year_offset_only_for_jan_to_jun_year_ends(F):
    """M1-D1-04／M4-D1-02 LHX 型：多年 6 月期末（自稱 FY＝期末年，offset 1）後改成曆年；
    曆年 10-K 不得 +1（FY2025 期末 2026-01-02 仍標 2025）。"""
    rev, A = [], []
    for y in range(2009, 2019):                              # Harris：6 月期末 10 份
        e = f"{y}-06-29"
        rev.append(dur(f"{y - 1}-06-30", e, y, f"{y}-08-20", fy=y, accn=f"h{y}"))
        A.append(inst(e, 1, f"{y}-08-20", fy=y, accn=f"h{y}"))
    for y in range(2020, 2026):                              # L3Harris：曆年 6 份
        e = "2026-01-02" if y == 2025 else f"{y}-12-31"
        rev.append(dur(f"{y}-01-01", e, y, f"{y + 1}-02-20", fy=y, accn=f"l{y}"))
        A.append(inst(e, 1, f"{y + 1}-02-20", fy=y, accn=f"l{y}"))
    s = F.annual_series(facts({"Revenues": rev, "Assets": A}), ["Revenues"], ASOF)
    assert all(s[y] == y for y in range(2009, 2019))         # 6 月期末：標期末年（offset 1）
    assert all(s[y] == y for y in range(2020, 2026))         # 曆年：不加 offset
    assert s.index.max() == 2025


def test_debt_lt_takes_max_of_tags_that_include_current(F):
    """M4-D1-06 WELL 型：SeniorNotes 只是一類債，LongTermDebt 才是總額 → 取最大值；PHM 型（反向）仍取 SeniorNotes。"""
    f = facts({"SeniorNotes": [calp(2025, 745)], "LongTermDebt": [calp(2025, 19197)]})
    d = F.debt_series(f, ASOF)
    assert d["debt"][2025] == 19197 and d["debt"].attrs["tag"][2025] == "LongTermDebt"
    f = facts({"NotesPayable": [calp(2021, 0)], "LongTermDebt": [calp(2021, 6000)]})      # ESS 2021＝0 型
    assert F.debt_series(f, ASOF)["debt"][2021] == 6000
    f = facts({"LongTermDebt": [calp(2025, 44)], "SeniorNotes": [calp(2025, 1584)]})
    assert F.debt_series(f, ASOF)["debt"][2025] == 1584


def test_dossier_tiny_debt_with_interest_warning(B):
    """M4-D1-06 min_fix：有息負債 <總負債 1% 且利息費用 >0 → 警示。"""
    f = _dossier_facts()
    f["facts"]["us-gaap"]["Liabilities"] = {"units": {"USD": [calp(y, 200000e6) for y in range(2016, 2026)]}}
    T, (R, filed, last, warn, bands) = B.build(f, "T", ASOF)
    assert any("有息負債不到總負債的 1%" in w and "FY2025" in w for w in warn)
    T, (R, filed, last, warn, bands) = B.build(_dossier_facts(), "T", ASOF)   # 無總負債 → 不誤報
    assert not any("有息負債不到總負債" in w for w in warn)


def _nvda_buyback_facts():
    """NVDA 型：FY2025 中 10:1 拆股。FY2024 10-K（拆股前，2024-02-21）報 FY2022–24；FY2025 10-K（2025-02-26）追溯 FY2023–25
    的稀釋股數，但權益變動表只重報 FY2024–25 的回購股數；FY2023 回購股數只有拆股前那份。"""
    d, sh, amt = [], [], []
    for y, n in ((2022, 2.5e9), (2023, 2.5e9), (2024, 2.5e9)):
        d.append(cal(y, n, "2024-02-21"))
    for y, n in ((2023, 25e9), (2024, 25e9), (2025, 24.8e9)):
        d.append(cal(y, n, "2025-02-26"))
    sh += [cal(2022, 30e6, "2024-02-21"), cal(2023, 63e6, "2024-02-21"), cal(2024, 21e6, "2024-02-21"),
           cal(2024, 210e6, "2025-02-26"), cal(2025, 310e6, "2025-02-26")]
    amt += [cal(2022, 4e9, "2024-02-21"), cal(2023, 10039e6, "2024-02-21"), cal(2024, 9533e6, "2024-02-21"),
            cal(2023, 10039e6, "2025-02-26"), cal(2024, 9533e6, "2025-02-26"), cal(2025, 33706e6, "2025-02-26")]
    return facts({"NetIncomeLoss": [cal(y, 1e9) for y in range(2022, 2026)],
                  "WeightedAverageNumberOfDilutedSharesOutstanding": d,
                  "StockRepurchasedAndRetiredDuringPeriodShares": sh,
                  "StockRepurchasedAndRetiredDuringPeriodValue": amt},
                 {"WeightedAverageNumberOfDilutedSharesOutstanding": "shares",
                  "StockRepurchasedAndRetiredDuringPeriodShares": "shares"})


def test_buyback_avg_same_filing_and_split_basis(B):
    """M2_D1_01：股數與金額同一份申報；拆股前基準的年度不給並警示（不得出現 FY2023 159 vs FY2024 45 混基準）。"""
    d = B.buyback_price_data(_nvda_buyback_facts(), ASOF)
    avg = d["avg_buyback_price_by_fy"]
    assert avg[2024] == pytest.approx(9533e6 / 210e6, rel=1e-6) and "2025-02-26" in d["avg_buyback_basis"][2024]
    assert avg[2025] == pytest.approx(33706e6 / 310e6, rel=1e-6)
    assert 2023 not in avg and any(w.startswith("FY2023：") and "拆股前基準" in w for w in d["avg_buyback_warnings"])
    assert 2022 not in avg and any(w.startswith("FY2022：") and "舊基準" in w for w in d["avg_buyback_warnings"])


def test_buyback_cash_flow_fallback_magnitude_guard(B):
    """M2_D1_01 ALL 型：無成對金額時用現金流量表金額；FY2024 278k 股 vs $2M（均價 7.2）與前後年差一個量級 → 不給並警示；
    其他現金流量表備援年度給值但標警示。"""
    tsa = [cal(2022, 20e6), cal(2023, 3e6), cal(2024, 278e3), cal(2025, 6e6)]
    cf = [cal(2022, 2520e6), cal(2023, 335e6), cal(2024, 2e6), cal(2025, 1233e6)]
    f = facts({"NetIncomeLoss": [cal(y, 1e9) for y in range(2022, 2026)],
               "WeightedAverageNumberOfDilutedSharesOutstanding": [cal(y, 265e6) for y in range(2022, 2026)],
               "TreasuryStockSharesAcquired": tsa, "PaymentsForRepurchaseOfCommonStock": cf},
              {"WeightedAverageNumberOfDilutedSharesOutstanding": "shares", "TreasuryStockSharesAcquired": "shares"})
    d = B.buyback_price_data(f, ASOF)
    avg, w = d["avg_buyback_price_by_fy"], d["avg_buyback_warnings"]
    assert 2024 not in avg and any(x.startswith("FY2024：") and "量級異常" in x for x in w)
    assert avg[2023] == pytest.approx(335 / 3, rel=1e-6) and any(x.startswith("FY2023：均價用現金流量表") for x in w)
    assert avg[2025] == pytest.approx(1233 / 6, rel=1e-6)



# ---------------------------------------------------------------- F04R2 第二次返工（check-F04R 的 PARTIAL）

def _lhx_transition_facts():
    """LHX 型過渡年：6 月期末（自稱 FY＝期末年）到 FY2019（2019-06-28），10-KT（accn t2019）改報曆年：
    營收、資產、股數有 12 個月至 2020-01-03 的數字，淨利只有 2019-06-28 那年（舊年報）。之後曆年。"""
    rev, ni, A, sh = [], [], [], []
    for y in range(2014, 2020):
        e, f, a = f"{y}-06-28", f"{y}-08-20", f"h{y}"
        rev.append(dur(f"{y - 1}-06-29", e, 100 + y, f, fy=y, accn=a))
        ni.append(dur(f"{y - 1}-06-29", e, 10 + y, f, fy=y, accn=a))
        A.append(inst(e, 1000 + y, f, fy=y, accn=a))
        sh.append(dur(f"{y - 1}-06-29", e, 120, f, fy=y, accn=a))
    rev.append(dur("2018-12-29", "2020-01-03", 12856, "2020-03-02", fy=2019, accn="t2019"))
    A.append(inst("2020-01-03", 38336, "2020-03-02", fy=2019, accn="t2019"))
    sh.append(dur("2018-12-29", "2020-01-03", 169, "2020-03-02", fy=2019, accn="t2019"))
    for y in (2020, 2021):
        rev.append(cal(y, 18000 + y, fy=y, accn=f"l{y}"))
        ni.append(cal(y, 1100 + y, fy=y, accn=f"l{y}"))
        A.append(calp(y, 37000 + y, fy=y, accn=f"l{y}"))
        sh.append(cal(y, 210, fy=y, accn=f"l{y}"))
    return facts({"Revenues": rev, "NetIncomeLoss": ni, "Assets": A,
                  "WeightedAverageNumberOfDilutedSharesOutstanding": sh},
                 {"WeightedAverageNumberOfDilutedSharesOutstanding": "shares"})


def test_transition_year_uses_one_period_end_and_warns(F):
    """M1-D1-04／M4-D1-02／M4-D1-03：1–6 月期末改 7–12 月期末的過渡年，同一財年各欄期末一致（取有全年損益的最晚期末），
    較早期末的淨利不得混進 FY2019，且必須有警示；前後年度不受影響。"""
    f = _lhx_transition_facts()
    S = {k: F.annual_series(f, F.TAGS[k], ASOF) for k in ("revenue", "net_income", "assets", "shares")}
    ends = {k: s.attrs["end"].get(2019) for k, s in S.items()}
    assert {e for e in ends.values() if e} == {"2020-01-03"}           # 不混期間
    assert ends["net_income"] is None and 2019 not in S["net_income"]   # 6 月期淨利不冒充 FY2019
    assert S["revenue"][2019] == 12856 and S["shares"][2019] == 169
    for s in S.values():
        assert any(w.startswith("FY2019：") and "2019-06-28" in w and "2020-01-03" in w for w in s.attrs["warn"])
    assert S["net_income"][2018] == 2028 and S["net_income"][2020] == 3120 and S["revenue"][2018] == 2118


def test_transition_10kt_without_full_year_keeps_old_year_end(F):
    """MOS FY2013／FERG FY2025 型：過渡期 10-KT 只有 7 個月損益（無 12 個月數字），資產負債表日不算年度期末：
    FY2013 各欄（含總資產）取 2013-05-31，不得總資產用 2013-12-31、淨利用 2013-05-31；且有警示。"""
    ni, A = [], []
    for y in (2011, 2012, 2013):
        ni.append(dur(f"{y - 1}-06-01", f"{y}-05-31", y, f"{y}-07-20", fy=y, accn=f"m{y}"))
        A.append(inst(f"{y}-05-31", 100 + y, f"{y}-07-20", fy=y, accn=f"m{y}"))
    ni.append(dur("2013-06-01", "2013-12-31", 999, "2014-02-20", fy=2013, accn="t2013"))   # 7 個月：不算全年
    A.append(inst("2013-12-31", 5555, "2014-02-20", fy=2013, accn="t2013"))
    ni.append(cal(2014, 2014, accn="c2014")); A.append(calp(2014, 2114, accn="c2014"))
    f = facts({"NetIncomeLoss": ni, "Assets": A})
    a, n = F.annual_series(f, ["Assets"], ASOF), F.annual_series(f, ["NetIncomeLoss"], ASOF)
    assert a.attrs["end"][2013] == n.attrs["end"][2013] == "2013-05-31"
    assert a[2013] == 2113 and n[2013] == 2013 and a[2014] == 2114
    assert any(w.startswith("FY2013：") and "2013-12-31" in w for w in a.attrs["warn"])


def test_52_53_week_year_ends_do_not_trigger_transition_warning(F):
    """52/53 週擺動（≤14 天）不是過渡年：不警示、不丟年。"""
    ends = ["2020-01-03", "2021-01-01", "2021-12-31", "2022-12-30", "2023-12-29"]
    fl = lambda e: str((pd.Timestamp(e) + pd.Timedelta(days=60)).date())
    rev = [dur(str((pd.Timestamp(e) - pd.Timedelta(days=364)).date()), e, i, fl(e)) for i, e in enumerate(ends)]
    A = [inst(e, 1, fl(e)) for e in ends]
    s = F.annual_series(facts({"Revenues": rev, "Assets": A}), ["Revenues"], ASOF)
    assert list(s.index) == [2019, 2020, 2021, 2022, 2023] and s.attrs["warn"] == []


def test_dossier_and_buyback_transition_year(B):
    """過渡年在資料包有警示（且無假的「淨利已重編」）；價格檔的回購不用較早期末的數字（GPN FY2016 型）。"""
    f = _lhx_transition_facts()
    g = f["facts"]["us-gaap"]
    g["StockRepurchasedAndRetiredDuringPeriodShares"] = {"units": {"shares": [
        dur("2018-06-30", "2019-06-28", 1e6, "2019-08-20", accn="h2019"),
        dur("2020-01-04", "2021-01-01", 2e6, "2021-03-01", accn="l2020")]}}
    g["StockRepurchasedAndRetiredDuringPeriodValue"] = {"units": {"USD": [
        dur("2018-06-30", "2019-06-28", 2000e6, "2019-08-20", accn="h2019"),
        dur("2020-01-04", "2021-01-01", 4000e6, "2021-03-01", accn="l2020")]}}
    T, (R, filed, last, warn, bands) = B.build(f, "T", ASOF)
    assert any(w.startswith("FY2019：同財年有兩個年度期末") for w in warn)
    assert not any(w.startswith("FY2019：淨利已重編") for w in warn)
    d = B.buyback_price_data(f, ASOF)
    assert 2019 not in d["avg_buyback_price_by_fy"] and d["avg_buyback_price_by_fy"][2020] == 2000
    assert any(w.startswith("FY2019：同財年有兩個年度期末") for w in d["avg_buyback_warnings"])


def _bb_facts(pairs_sh, pairs_amt, shares=170e6, assets=13e9):
    yrs = sorted(pairs_sh)
    return facts({"NetIncomeLoss": [cal(y, 1e9) for y in yrs],
                  "Assets": [calp(y, assets) for y in yrs],
                  "WeightedAverageNumberOfDilutedSharesOutstanding": [cal(y, shares) for y in yrs],
                  "StockRepurchasedAndRetiredDuringPeriodShares": [cal(y, v) for y, v in pairs_sh.items()],
                  "StockRepurchasedAndRetiredDuringPeriodValue": [cal(y, v) for y, v in pairs_amt.items()]},
                 {"WeightedAverageNumberOfDilutedSharesOutstanding": "shares",
                  "StockRepurchasedAndRetiredDuringPeriodShares": "shares"})


def test_buyback_paired_magnitude_and_absolute_guards(B):
    """M2_D1_01：成對概念也做 ±3 年量級檢查（MSI 2024＝26,333 型）；絕對值防呆：隱含市值 >總資產 30 倍
    （ADP 型整串單位錯，鄰年中位數抓不到）或均價 <0.5 美元（EQT 0.13 型）→ 不給並警示。"""
    sh = {2021: 5e6, 2022: 5e6, 2023: 4e6, 2024: 3e3, 2025: 4e6}       # FY2024 股數少三個 0
    amt = {2021: 1000e6, 2022: 1100e6, 2023: 1000e6, 2024: 79e6, 2025: 1200e6}
    d = B.buyback_price_data(_bb_facts(sh, amt), ASOF)
    avg, w = d["avg_buyback_price_by_fy"], d["avg_buyback_warnings"]
    assert 2024 not in avg and any(x.startswith("FY2024：") and "不給均價" in x for x in w)
    assert avg[2023] == pytest.approx(250) and avg[2025] == pytest.approx(300)
    # ADP 型：每年股數單位都錯 → 均價約 5,000 萬，鄰年一樣錯
    d = B.buyback_price_data(_bb_facts({y: 20 for y in range(2016, 2019)}, {y: 1e9 for y in range(2016, 2019)}), ASOF)
    assert d["avg_buyback_price_by_fy"] == {} and sum("量級不合理" in x for x in d["avg_buyback_warnings"]) == 3
    # EQT 型：均價 0.13
    d = B.buyback_price_data(_bb_facts({2016: 1e7}, {2016: 1.3e6}), ASOF)
    assert d["avg_buyback_price_by_fy"] == {} and any("量級不合理" in x for x in d["avg_buyback_warnings"])


def test_buyback_negative_paired_amount_uses_absolute_value(B):
    """M2_D1_01 EME 2011 型：首次申報的權益變動表成對金額為負號 → 取絕對值，不退到別的概念（不得變成 1.05）。"""
    f = facts({"NetIncomeLoss": [cal(2011, 1e8)], "Assets": [calp(2011, 3e9)],
               "WeightedAverageNumberOfDilutedSharesOutstanding": [cal(2011, 67e6)],
               "TreasuryStockSharesAcquired": [cal(2011, 1.0e6)],
               "TreasuryStockValueAcquiredCostMethod": [cal(2011, -22.9e6)],
               "PaymentsForRepurchaseOfCommonStock": [cal(2011, 1.05e6)]},
              {"WeightedAverageNumberOfDilutedSharesOutstanding": "shares", "TreasuryStockSharesAcquired": "shares"})
    d = B.buyback_price_data(f, ASOF)
    assert d["avg_buyback_price_by_fy"][2011] == pytest.approx(22.9)
    assert "TreasuryStockValueAcquiredCostMethod" in d["avg_buyback_basis"][2011]
