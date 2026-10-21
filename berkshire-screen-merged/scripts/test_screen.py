#!/usr/bin/env python3
"""初篩邏輯自測（合成資料，不連網）：python test_screen.py"""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import screen as S  # noqa: E402
import screen_quant as SQ  # noqa: E402

ASOF = "2026-10-15"
rng = np.random.default_rng(7)


def base(i):
    ni = float(rng.uniform(5e8, 3e9))
    return dict(ticker=f"T{i:03d}", name=f"Co{i}", sic=3500 + (i % 7) * 100, is_financial=False, fy_last=2025,
                net_income=ni, ni_prev=ni * 0.95, ni5_mean=ni * 0.9, ni_sum5=ni * 4.5, ni5_med=ni * 0.9, n_ni5=5,
                loss_yrs5=0, shareholders_equity=ni * 6, eq_prev=ni * 5.8, roe=0.16 + rng.normal(0, .04),
                roe_med5=0.16, roe_min5=0.12, roa=0.07 + rng.normal(0, .02), fcf=ni * 0.9,
                operating_cash_flow=ni * 1.2, ocf_sum5=ni * 5.5, ocf_pos5=5, n_ocf5=5, eps_g1=0.05,
                rev_cagr_5y=0.05 + rng.normal(0, .03), op_margin_5y_std=abs(rng.normal(.02, .01)),
                debt_to_equity=abs(rng.normal(.6, .3)), share_change_3y=rng.normal(-.02, .03),
                liab_to_assets=0.5, shares=1e9, shares_cover=1e9, shares_cover_filed="2026-08-01",
                shares_filed_date="2026-02-15", fy_last_filed="2026-02-15", max_filed_used="2026-08-01", asof=ASOF)


def price(i, vol=0.2, dd=-0.1, px=None):
    return dict(ticker=f"T{i:03d}", price=px if px else float(rng.uniform(20, 40)), price_date="2026-10-01",
                vol_36m=vol + rng.normal(0, .02), drawdown_3y=dd, dd52=dd / 2, splits="")


def make():
    F = [base(i) for i in range(120)]
    P = [price(i) for i in range(120)]
    # T000：持續虧損 → J1 刪除
    F[0].update(net_income=-5e8, ni_prev=-3e8, loss_yrs5=4, ni_sum5=-1e9, ni5_mean=-2e8, roe=-0.1, roe_med5=-0.05)
    # T001：此前經濟力佳、單年巨虧吃掉半數權益 → J4 例外：不刪、標困境；統計排除後進「歷史經濟力佳」特殊道
    F[1].update(net_income=-4e9, ni_prev=2e9, loss_yrs5=1, ni_sum5=4e9, shareholders_equity=5e9, eq_prev=1.2e10,
                roe=-0.4, roe_med5=0.2, roe_min5=0.15)
    # T002：波動最高 → 統計排除
    P[2].update(vol_36m=0.9)
    # T003：拆股（封面股數申報後 1 拆 4）→ 市值須乘 4
    P[3].update(price=25.0, splits="2026-09-01:4")
    F[3].update(shares_cover=1e9)
    return pd.DataFrame(F), pd.DataFrame(P)


def run(F, P, special_max=0):
    df = S.build(F, P, None, ASOF)
    df = S.structural(df, ASOF)
    return S.statistical(df, 15, special_max)[0]


def test_all():
    F, P = make()
    df = run(F, P, special_max=3).set_index("ticker")
    assert "J1_持續虧損" in df.loc["T000", "delete_codes"], df.loc["T000", "delete_codes"]
    assert df.loc["T000", "lane"].startswith("刪除")
    assert df.loc["T001", "delete_codes"] == "", "J4 例外不應刪除"
    assert "T1_去年虧損" in df.loc["T001", "distress_codes"]
    assert df.loc["T001", "lane"] == "特殊情境（困境中但歷史經濟力佳）", df.loc["T001", "lane"]
    assert bool(df.loc["T001", "special_handoff"])
    assert "波動" in df.loc["T002", "exclude_reasons"] and not df.loc["T002", "shortlist"]
    assert abs(df.loc["T003", "market_cap"] - 25.0 * 4e9) < 1, df.loc["T003", "market_cap"]
    sl = df[df.shortlist]
    assert len(sl) == 15 and (sl.lane == "核心道").all() and (sl.delete_codes == "").all()
    assert sl.core_rank.max() == 15
    # 預設不交特殊情境
    assert run(F, P).special_handoff.sum() == 0
    # 前視：申報日 ≥ 決策日 → 中止
    F2 = F.copy(); F2.loc[5, "fy_last_filed"] = ASOF
    try:
        run(F2, P); raise AssertionError("應因前視中止")
    except SQ.LookAheadError:
        pass
    # 端到端：寫檔
    with tempfile.TemporaryDirectory() as d:
        df2 = run(F, P)
        S.handoff(df2, ASOF, os.path.join(d, "r")); S.report(df2, True, os.path.join(d, "r"), 15, ASOF)
        import json
        h = json.load(open(os.path.join(d, "r_handoff.json")))
        assert len(h["core_shortlist"]) == 15 and all("screen_decision" in x for x in h["core_shortlist"])
        x = h["core_shortlist"][0]
        assert "price" not in x and "earn_yield" not in x and "core_rank" not in x and "price" in x["price_context"]


def test_quality_factor():
    """非金融研究優先：5 年最差 ROE 高者排前；營收成長不影響分數（2026-10-17 修訂）。"""
    F, P = make()
    F.loc[10, "roe_min5"], F.loc[11, "roe_min5"] = 0.40, 0.01
    for c in ("fcf", "ni5_mean", "op_margin_5y_std", "debt_to_equity", "share_change_3y"):
        F.loc[11, c] = F.loc[10, c]
    P.loc[11, "price"] = P.loc[10, "price"]
    df = run(F, P).set_index("ticker")
    assert df.loc["T010", "research_priority"] > df.loc["T011", "research_priority"]
    F2 = F.copy(); F2["rev_cagr_5y"] = -F2["rev_cagr_5y"]
    df2 = run(F2, P).set_index("ticker")
    assert (df["research_priority"].fillna(-1) == df2["research_priority"].fillna(-1)).all(), "營收成長不應影響分數"



def test_share_change_split_adjusted():
    """股數 3 年變化：fy−3 股數取自較舊 10-K（拆股前基準）→ 用其申報日到最新股數申報日之間的拆股調整（M1-D1-02）；
    調整後 |變化|>50% 註記；> +900%（單位錯誤）才設缺值，真實大幅稀釋保留原值（M1-D4-01、M1-R4-04）。"""
    F, P = make()
    F["shares_3y_ago"] = F["shares"] * 1.02
    F["shares_3y_filed"] = "2023-02-15"
    # T020：2024-06 一拆十，舊年報股數為拆股前基準 → 調整後約 −2%，不是 +880%
    F.loc[20, ["shares", "shares_3y_ago"]] = [1e9, 1.02e8]
    P.loc[20, "splits"] = "2024-06-10:10"
    # T021：拆股發生在舊年報申報之前（已追溯）→ 不再調整
    F.loc[21, ["shares", "shares_3y_ago"]] = [1e9, 1.02e9]
    P.loc[21, "splits"] = "2022-06-10:10"
    # T022：單位錯誤（+95,000%）→ 缺值＋註記；T023：+60%（合併）→ 只註記
    F.loc[22, ["shares", "shares_3y_ago"]] = [3.8e8, 4e5]
    F.loc[23, ["shares", "shares_3y_ago"]] = [1.6e9, 1e9]
    # T025：+150%（真實合併稀釋）→ 保留原值、只註記，不得設缺值拿中性分（M1-R4-04）
    F.loc[25, ["shares", "shares_3y_ago"]] = [2.5e9, 1e9]
    df = S.build(F, P, None, ASOF).set_index("ticker")
    assert abs(df.loc["T020", "share_change_3y"] - (1 / 1.02 - 1)) < 1e-9, df.loc["T020", "share_change_3y"]
    assert abs(df.loc["T021", "share_change_3y"] - (1 / 1.02 - 1)) < 1e-9
    assert pd.isna(df.loc["T022", "share_change_3y"]) and "已設缺值" in df.loc["T022", "dq_note"]
    assert abs(df.loc["T023", "share_change_3y"] - 0.6) < 1e-9 and "先查拆股" in df.loc["T023", "dq_note"]
    assert df.loc["T024", "dq_note"] == ""
    assert abs(df.loc["T025", "share_change_3y"] - 1.5) < 1e-9
    assert "先查拆股" in df.loc["T025", "dq_note"] and "設缺值" not in df.loc["T025", "dq_note"]
    assert S.split_factor("2024-06-10:10;2026-09-01:2", "2023-02-15", "2026-02-15") == 10
    assert S.split_factor("2024-06-10:10;2026-09-01:2", "2023-02-15") == 20


def test_big_dilution_scores_monotonic():
    """股數 3 年增加越多，研究優先分數不得越高：+150% 不可比 +60% 分數高（M1-R4-04：舊版 >+100% 設缺計 0.5 中性）。"""
    F, P = make()
    F["shares_3y_ago"] = F["shares"] / (1 + F["share_change_3y"])
    F["shares_3y_filed"] = "2023-02-15"
    F.loc[30, "shares_3y_ago"] = F.loc[30, "shares"] / 1.6
    F.loc[31, "shares_3y_ago"] = F.loc[31, "shares"] / 2.5
    F.loc[31, [c for c in F.columns if c not in ("ticker", "name", "shares", "shares_3y_ago")]] = \
        F.loc[30, [c for c in F.columns if c not in ("ticker", "name", "shares", "shares_3y_ago")]].values
    P.loc[31, ["price", "vol_36m", "drawdown_3y", "dd52"]] = P.loc[30, ["price", "vol_36m", "drawdown_3y", "dd52"]].values
    df = run(F, P).set_index("ticker")
    assert df.loc["T031", "research_priority_raw"] < df.loc["T030", "research_priority_raw"]


def test_reit_f5_and_no_eps_cagr5():
    """REIT 歸非金融組（D20），F5 也照評（M1-RS2-02）；未拆股調整的 eps_cagr5 不帶進輸出（M1-RS1-01）；
    企業技術服務 737x 列入 SEL-050 太難型（D72），7370 不列。"""
    F, P = make()
    F["eps_cagr5"] = 0.05
    for i, sic in ((40, 6798), (41, 6022)):   # REIT、銀行；皆 5 年淨利 >0、營業現金流 <0
        F.loc[i, ["sic", "is_financial", "ocf_sum5", "n_ocf5"]] = [sic, True, -1e9, 5]
    df = run(F, P).set_index("ticker")
    assert "F5_盈餘無現金" in df.loc["T040", "flag_codes"] and df.loc["T041", "flag_codes"] == ""
    assert "eps_cagr5" not in df.columns
    assert S.too_hard_type(7373) == "企業技術服務" and S.too_hard_type(7372) == "企業技術服務"
    assert S.too_hard_type(7370) == "" and S.too_hard_type(7812) == "電影內容製作" and S.too_hard_type(6770) == "殼公司"


def test_spinoff_after_fiscal_year_end():
    """財年後分拆（M1-D1-03／D69 保守）：市值已是分拆後、年報仍含分出事業 → 殖利率設缺、落入資料不足、dq_note 有字；
    分拆調整係數（非整數比例）不當拆股乘到股數（M1-D1-05）。"""
    F, P = make()
    F["fy_last_end"] = "2025-12-31"
    P["spinoffs"] = ""
    # T030：期末後分拆，事件在 spinoffs 欄；T031：Yahoo 係數混在 splits（skill fetch_prices 路徑）且在封面股數申報前
    P.loc[30, "spinoffs"] = "2026-06-29:1.907"
    P.loc[31, "splits"] = "2026-03-01:1.241"
    F.loc[31, "shares_cover_filed"] = "2026-02-01"
    # T032：分拆在財年內 → 照常
    P.loc[32, "spinoffs"] = "2025-10-30:1.061"
    P.loc[[30, 31, 32], "vol_36m"] = 0.15          # 不受其他排除條件影響（隨機資料）
    F.loc[[30, 31, 32], "roa"] = 0.12
    P.loc[[30, 31, 32], "drawdown_3y"] = -0.05
    df = run(F, P).set_index("ticker")
    for t in ("T030", "T031"):
        assert pd.isna(df.loc[t, "earn_yield"]) and df.loc[t, "lane"] == "資料不足", (t, df.loc[t, "lane"])
        assert "財年後分拆" in df.loc[t, "dq_note"]
    assert abs(df.loc["T031", "shares_mcap"] - 1e9) < 1, "分拆係數不得乘到股數"
    assert pd.notna(df.loc["T032", "earn_yield"]) and df.loc["T032", "dq_note"] == ""


def _handoff(df):
    import json
    with tempfile.TemporaryDirectory() as d:
        S.handoff(df, ASOF, os.path.join(d, "r")); S.report(df, True, os.path.join(d, "r"), 15, ASOF)
        return json.load(open(os.path.join(d, "r_handoff.json"))), open(os.path.join(d, "r.md"), encoding="utf-8").read()


def test_handoff_carries_data_notes_and_unrated_fields():
    """交接檔帶 data_notes 與 unrated_fields（被補 0.5 的因子、缺值無法評估的結構性規則）（M1-D5-01／D5-02）。"""
    F, P = make()
    first = run(F, P).sort_values("core_rank").ticker.iloc[0]
    F["data_notes"] = ""
    F.loc[F.ticker == first, "data_notes"] = "有息負債缺"
    F.loc[F.ticker == "T050", ["debt_to_equity", "loss_yrs5"]] = np.nan
    df = run(F, P)
    u = df.set_index("ticker").loc["T050", "unrated_fields"]
    assert "debt_to_equity" in u.split(";") and "J1:loss_yrs5" in u.split(";"), u
    assert df.set_index("ticker").loc["T060", "unrated_fields"] == ""
    h, _ = _handoff(df)
    assert all("data_notes" in x and "unrated_fields" in x for x in h["core_shortlist"])
    assert h["core_shortlist"][0]["ticker"] == first and h["core_shortlist"][0]["data_notes"] == "有息負債缺"


def test_exclusion_flag_float_one():
    """exclusions.csv 只列部分代號、以 1 填 → merge 後成 1.0，仍要認（M1-D1-06）。"""
    F, P = make()
    ex = pd.DataFrame({"ticker": ["T050", "T051"], "excl_fraud": [1, np.nan]})
    df = S.statistical(S.structural(S.build(F, P, ex, ASOF), ASOF), 15, 0)[0].set_index("ticker")
    assert "X4_誠信紅旗" in df.loc["T050", "delete_codes"], df.loc["T050", "delete_codes"]
    assert df.loc["T051", "delete_codes"] == ""


def test_core_rank_ties_stable():
    """研究優先同分：名次不隨輸入列序改變，同分依代號（M1-D1-07）。"""
    F, P = make()
    cols = ["fcf", "ni5_mean", "roe_min5", "op_margin_5y_std", "debt_to_equity", "share_change_3y", "net_income",
            "roe", "roa"]
    for i in range(41, 46):
        F.loc[i, cols] = F.loc[40, cols].values
        P.loc[i, ["price", "vol_36m", "drawdown_3y", "dd52"]] = P.loc[40, ["price", "vol_36m", "drawdown_3y", "dd52"]].values
    a = run(F, P).set_index("ticker")["core_rank"]
    rev = run(F.iloc[::-1].reset_index(drop=True), P.iloc[::-1].reset_index(drop=True)).set_index("ticker")["core_rank"]
    shuf = np.random.default_rng(1).permutation(len(F))
    sh = run(F.iloc[shuf].reset_index(drop=True), P).set_index("ticker")["core_rank"]
    assert a.equals(rev.reindex(a.index)) and a.equals(sh.reindex(a.index))
    tied = [a[f"T{i:03d}"] for i in range(40, 46)]
    assert tied == sorted(tied), tied


def test_j4_missing_history_not_deleted():
    """J4：例外所需的 5 年歷史缺值 → 不刪，只標 T1（M1-D2-11；缺資料永遠不是刪除理由）。"""
    F, P = make()
    F.loc[52, ["net_income", "shareholders_equity", "eq_prev", "roe"]] = [-3e9, 2e9, 6e9, -1.5]
    F.loc[52, "roe_med5"] = np.nan
    df = run(F, P).set_index("ticker")
    assert df.loc["T052", "delete_codes"] == "", df.loc["T052", "delete_codes"]
    assert "T1_去年虧損" in df.loc["T052", "distress_codes"]
    assert "J4:roe_med5" in df.loc["T052", "unrated_fields"]
    F.loc[52, "roe_med5"] = 0.05                     # 有歷史、經濟力不佳 → 照刪
    assert "J4_虧損腰斬權益" in run(F, P).set_index("ticker").loc["T052", "delete_codes"]


def test_mcap_shares_source():
    """缺封面股數 → 市值用年報加權股數，記 mcap_shares_source（M1-D5-03）；不寫 dq_note（不是錯誤、與刪除無關，
    否則被刪的公司會因此列入 deletions_to_review 的「資料品質註記」；M1-R5-01），報告的資料品質表照列。"""
    F, P = make()
    F.loc[60, "shares_cover"] = np.nan
    df = S.build(F, P, None, ASOF).set_index("ticker")
    assert df.loc["T060", "mcap_shares_source"] == "annual_weighted" and df.loc["T060", "mcap_shares_date"] == "2026-02-15"
    assert df.loc["T060", "dq_note"] == ""
    assert df.loc["T061", "mcap_shares_source"] == "cover" and df.loc["T061", "dq_note"] == ""


def test_report_lists_lacking_and_dq():
    """報告列資料不足表、資料品質註記表、刪除表的觸發欄位、特殊情境截斷說明；交接檔帶 deleted／lacking（M1-D5-05）。"""
    F, P = make()
    t = run(F, P).sort_values("core_rank").ticker.iloc[0]          # 核心道第 1 名，拿掉波動 → 資料不足
    P.loc[P.ticker == t, "vol_36m"] = np.nan
    F.loc[60, "shares_cover"] = np.nan
    h, md = _handoff(run(F, P))
    assert "## 資料不足（1 檔" in md and f"| {t} | vol_36m |" in md
    assert "## 資料品質註記" in md and "T060" in md.split("## 資料品質註記")[1]
    assert "| ticker | name | delete_codes | net_income |" in md and "／共 " in md
    assert {"ticker": "T000", "codes": "J1_持續虧損;J7_連虧且累虧"} in h["deleted"]
    assert [x["ticker"] for x in h["lacking"]] == [t] and h["lacking"][0]["missing"] == ["vol_36m"]


def test_special_handoff_always_distress_check():
    """特殊情境交棒每檔都要做困境專門檢查（M1-D2-06）；入道條件含價格，標記放 price_context（D71）。"""
    F, P = make()
    h, _ = _handoff(run(F, P, special_max=3))
    assert h["special_handoff"]
    for x in h["special_handoff"]:
        assert S.SPECIAL_MARK in x["price_context"]["price_distress_codes"]
        assert x["screen_decision"] == ("PASS-困境" if x["distress_codes"] else "PASS")
        assert "T3_" not in x["distress_codes"] and "T5_" not in x["distress_codes"]


def test_dq_note_has_no_price_derived_values():
    """dq_note 進價格盲的 screen_context：不得含分拆係數（兩個股價的比值）或殖利率；係數只在 result.csv 的
    spinoff_factors（不進交接檔）（M1-R6-01）。"""
    import re
    F, P = make()
    F["fy_last_end"] = "2025-12-31"
    P["spinoffs"] = ""
    F["shares_3y_ago"], F["shares_3y_filed"] = F["shares"], "2026-02-15"
    P.loc[30, "spinoffs"] = "2026-06-29:1.272"                                    # 財年後分拆
    P.loc[31, "splits"] = "2026-03-29:0.9535"; F.loc[31, "fy_last_end"] = "2026-06-30"   # 含合股（HON 型），仍在核心道
    F.loc[32, "shares_cover"] = 1e4                                               # 單位錯誤 → 盈餘殖利率 >100%
    P.loc[[30, 31, 32], "vol_36m"] = 0.15
    F.loc[[30, 31, 32], "roa"] = 0.12
    P.loc[[30, 31, 32], "drawdown_3y"] = -0.05
    df = run(F, P, special_max=3)
    d = df.set_index("ticker")
    assert "財年後分拆 2026-06-29" in d.loc["T030", "dq_note"] and "含合股、需人工處理" in d.loc["T031", "dq_note"]
    assert "股數或單位錯誤" in d.loc["T032", "dq_note"]
    for t in ("T030", "T031", "T032"):
        assert not re.search(r"係數|殖利率異常|\d\.\d", d.loc[t, "dq_note"]), d.loc[t, "dq_note"]
    assert d.loc["T030", "spinoff_factors"] == "2026-06-29:1.272" and d.loc["T031", "spinoff_factors"] == "2026-03-29:0.9535"
    df.loc[df.ticker == "T031", ["shortlist", "core_rank"]] = [True, 0]           # 確保在核心短名單
    h, md = _handoff(df)
    rows = h["core_shortlist"] + h["lacking"]
    assert any(x["ticker"] == "T031" for x in h["core_shortlist"])
    for x in rows:
        assert "spinoff_factors" not in x and not re.search(r"係數|\d\.\d{3}", str(x.get("dq_note"))), x
    assert "2026-03-29:0.9535" in md.split("## 資料品質註記")[1]                  # 給人看的報告照列係數


def test_override_marks_distress_and_expires():
    """撤銷結構性刪除 → 補非價格困境碼 R_人工撤銷刪除、screen_decision＝PASS-困境（SKILL §6.2；J1 最近一年獲利者原本
    沒有 T1；M1-R4-02）；有 date 的撤銷 12 個月內有效，過期列入 overrides_expired、不再撤銷（M1-R4-03）。"""
    F, P = make()
    F.loc[0, ["net_income", "ni_prev", "roe"]] = [2e8, -3e8, 0.02]                 # J1、最近一年獲利（BA／HOOD 型）
    o = pd.DataFrame({"ticker": ["T000", "T001", "T005", "T006", "T007", "T007"],
                      "date": ["2025-10-16", "2026-10-20", "2025-10-15", "bad", "2024-01-01", "2026-09-01"],
                      "reason": ["一次性減損", "未生效", "過期", "日期錯", "舊", "新"]})
    ov, exp = S.load_overrides(o, ASOF)
    assert ov == {"T000": "一次性減損", "T007": "新"}, ov                          # 364 天內生效；未來日期、錯日期不生效
    assert exp == [{"ticker": "T005", "date": "2025-10-15", "reason": "過期"}], exp   # 滿 365 天過期；T007 有新列不算
    assert S.load_overrides(pd.DataFrame({"ticker": ["T000"], "reason": ["手動"]}), ASOF)[0] == {"T000": "手動"}
    df = S.statistical(S.structural(S.build(F, P, None, ASOF), ASOF, ov, exp), 15, 0)[0]
    d = df.set_index("ticker")
    assert d.loc["T000", "delete_codes"] == "" and "撤銷 J1_持續虧損" in d.loc["T000", "override_note"]
    # D95：撤銷不保證進短名單——T000（盈餘殖利率低）實際落在統計排除道、不在短名單
    assert d.loc["T000", "lane"] == "排除（統計高風險）", d.loc["T000", "lane"]
    assert not d.loc["T000", "shortlist"]
    assert d.loc["T000", "distress_codes"] == S.OVERRIDE_DISTRESS and not S.OVERRIDE_DISTRESS.startswith(S.PRICE_DISTRESS)
    assert d.loc["T005", "override_expired"].startswith("2025-10-15 起滿 12 個月")
    h0, md0 = _handoff(df)
    assert "T000" not in [x["ticker"] for x in h0["core_shortlist"]]
    assert "撤銷不保證進短名單" in h0["note"] and "trader analyze --tickers" in h0["note"]
    assert "撤銷不保證進短名單" in md0
    # 以下模擬使用者依 D95 手動送深度（trader analyze --tickers）時交接檔的 screen_decision
    df.loc[df.ticker == "T000", ["shortlist", "core_rank"]] = [True, 0]
    h, md = _handoff(df)
    x = next(x for x in h["core_shortlist"] if x["ticker"] == "T000")
    assert x["screen_decision"] == "PASS-困境" and x["distress_codes"] == S.OVERRIDE_DISTRESS
    assert h["overrides_expired"] == [{"ticker": "T005", "note": d.loc["T005", "override_expired"]}]
    assert "## 已過期的撤銷（1 檔" in md
    # 撤銷前就有 T1 的（J3／J4／J7）照樣補上
    F.loc[0, ["net_income", "roe"]] = [-5e8, -0.1]
    d2 = S.structural(S.build(F, P, None, ASOF), ASOF, {"T000": "r"}).set_index("ticker")
    assert d2.loc["T000", "distress_codes"].split(";") == ["T1_去年虧損", S.OVERRIDE_DISTRESS]


def test_report_explains_evaluated_count():
    """md 標題的檔數是評估列數，股票池說明的檔數是去重前名單：寫明差異與差在哪幾檔（M1-R5-03）。"""
    F, P = make()
    df = run(F, P)
    with tempfile.TemporaryDirectory() as d:
        S.report(df, True, os.path.join(d, "r"), 15, ASOF, "股票池：test，122 檔", ["GOOG", "NWS"])
        md = open(os.path.join(d, "r.md"), encoding="utf-8").read()
    assert md.startswith(f"# 初篩結果（決策日 {ASOF}，評估 120 檔）")
    assert "可少於股票池檔數；價格檔有、財報輸入無：GOOG、NWS" in md


# ---- fetch_edgar.py 手動腳本：SEC 403 中止、快取原子寫入、前身 CIK 合併（RM1-7；假回應，不連網） ----
import json  # noqa: E402
import urllib.error  # noqa: E402

import fetch_edgar as FE  # noqa: E402


def _ni_facts(years, name="Old Co"):
    rows = [dict(start=f"{y}-01-01", end=f"{y}-12-31", val=1e9 * (y - 2015), form="10-K", fy=y, fp="FY",
                 filed=f"{y + 1}-02-20", accn=f"0000-{y}") for y in years]
    return {"entityName": name, "facts": {"us-gaap": {"NetIncomeLoss": {"label": "NI", "units": {"USD": rows}}}}}


def _run_main(tmp, tickers, fake_get, extra=()):
    """以假 get 跑 fetch_edgar.main；回傳 (請求網址清單, 輸出 DataFrame 或 None, SystemExit 訊息或 None)。"""
    calls = []
    tk = os.path.join(tmp, "tickers.txt")
    with open(tk, "w") as f:
        f.write("\n".join(tickers) + "\n")
    out = os.path.join(tmp, "features.csv")
    old_get, old_argv = FE.get, sys.argv
    FE.get = lambda url: (calls.append(url), fake_get(url))[1]
    sys.argv = ["fetch_edgar.py", "--tickers", tk, "--out", out, "--cache", os.path.join(tmp, "cache"),
                "--asof", ASOF, "--ua", "test test@example.com", *extra]
    err = None
    try:
        FE.main()
    except SystemExit as e:
        err = str(e.code)
    finally:
        FE.get, sys.argv = old_get, old_argv
    return calls, (pd.read_csv(out) if os.path.exists(out) else None), err


def test_fetch_edgar_403_streak_aborts():
    """SEC 403 連續 MAX_403_STREAK 檔即中止、不再逐檔續打、不寫半份輸出（M1-RS8-02；同 trader sec.py）。"""
    with tempfile.TemporaryDirectory() as tmp:
        tickers = [f"T{i:02d}" for i in range(20)]
        names = {str(i): {"cik_str": 1000 + i, "ticker": t} for i, t in enumerate(tickers)}

        def fake(url):
            if url.endswith("company_tickers.json"):
                return names
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        calls, df, err = _run_main(tmp, tickers, fake)
        assert err and "SEC 拒絕存取" in err and "SEC_USER_AGENT" in err, err
        assert len(calls) == 1 + FE.MAX_403_STREAK, len(calls)   # 代號表＋5 檔，之後不再送出
        assert df is None


def test_fetch_edgar_cache_atomic_and_truncated_refetch():
    """快取寫入中途失敗不留半截檔；既有半截快取視同沒有、重抓並寫回完整檔（M1-RS8-02；同 trader F07／F09）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "CIK0000000001.json")
        old_get = FE.get
        try:
            FE.get = lambda url: {"a": 1, "b": object()}   # json.dump 寫到一半拋 TypeError
            try:
                FE.cached(path, "u")
                raise AssertionError("應拋 TypeError")
            except TypeError:
                pass
            assert os.listdir(tmp) == [], os.listdir(tmp)       # 目標檔與暫存檔都不留
            with open(path, "w") as f:
                f.write('{"facts": {"us-gaap"')                  # 修前被殺留下的半截快取
            n = []
            FE.get = lambda url: (n.append(url), {"ok": True})[1]
            assert FE.cached(path, "u") == {"ok": True} and n == ["u"]
            with open(path) as f:
                assert json.load(f) == {"ok": True}
            assert FE.cached(path, "u") == {"ok": True} and n == ["u"]   # 之後命中快取
        finally:
            FE.get = old_get


def test_fetch_edgar_merges_predecessor_cik():
    """改組後新 CIK 沒有歷史 10-K：依前身對照（格式同 trader cik_predecessors.json）併入舊 CIK（M1-RS1-02）。"""
    new, old = "0002115436", "0000034088"
    with tempfile.TemporaryDirectory() as tmp:
        pre = os.path.join(tmp, "pre.json")
        with open(pre, "w") as f:
            json.dump({"map": {new: [old]}}, f)
        data = {"company_tickers.json": {"0": {"cik_str": int(new), "ticker": "XOM"}},   # submissions 須排在 CIK*.json 前
                f"submissions/CIK{new}.json": {"sic": "2911"},
                f"CIK{new}.json": {"entityName": "ExxonMobil Holdings", "facts": {}},
                f"CIK{old}.json": _ni_facts(range(2016, 2026))}

        def fake(url):
            return next(v for k, v in data.items() if url.endswith(k))
        calls, df, err = _run_main(tmp, ["XOM"], fake, ["--predecessors", pre])
        assert err is None, err
        r = df.iloc[0]
        assert r.fy_last == 2025 and r.net_income == 1e10, r.to_dict()
        assert f"含前身 CIK {old}" in r.data_notes and r["name"] == "ExxonMobil Holdings" and r.sic == 2911, r.to_dict()
    # 預設讀 repo 內 trader 的對照檔（只讀 JSON）；skill 單獨拿出去用時檔案不在，就不合併
    if os.path.exists(FE.PREDECESSORS):
        assert FE.load_predecessors(FE.PREDECESSORS).get(new) == [old]


# ---- A3 V3-1（F3-SK）新增 ----
def test_too_hard_cap_when_core_short():
    """非太難型不足 top 時不拿超額太難型補位：60 檔 SIC 5300（零售）＋10 檔 3500、top=15 → 太難型 ≤4（A3-M1A-01）。"""
    F = [base(i) for i in range(70)]
    P = [price(i) for i in range(70)]
    for i in range(10, 70):
        F[i]["sic"] = 5300
    F, P = pd.DataFrame(F), pd.DataFrame(P)
    df = S.statistical(S.structural(S.build(F, P, None, ASOF), ASOF), 15, 0)[0]
    sl = df[df.shortlist]
    n_hard = int((sl.sel050_type != "").sum())
    n_easy_core = int(((df.lane == "核心道") & (df.sel050_type == "")).sum())
    assert n_hard <= S.TOO_HARD_CAP, n_hard
    assert n_easy_core < 15 - S.TOO_HARD_CAP and len(sl) == n_easy_core + S.TOO_HARD_CAP < 15, (len(sl), n_easy_core)
    assert df.core_rank.notna().sum() == (df.lane == "核心道").sum()          # core_rank 保留給全部核心道
    with tempfile.TemporaryDirectory() as d:
        S.report(df, True, os.path.join(d, "r"), 15, ASOF)
        S.handoff(df, ASOF, os.path.join(d, "r"))
        md = open(os.path.join(d, "r.md"), encoding="utf-8").read()
        import json as _j
        h = _j.load(open(os.path.join(d, "r_handoff.json"), encoding="utf-8"))
    assert f"## 核心短名單（前 15；實得 {len(sl)} 檔" in md, md[:2000]
    assert len(h["core_shortlist"]) == len(sl)


def test_sector_missing_sic_unclassified():
    """無 SIC → sector＝「未分類」（不是 "<NA>" 字串），T5 不把它當產業（A3-M1A-05）。"""
    F, P = make()
    F["sic"] = F["sic"].astype(float)
    F.loc[[5, 6], "sic"] = np.nan
    df = S.build(F, P, None, ASOF).set_index("ticker")
    assert df.loc["T005", "sector"] == "未分類" and df.loc["T006", "sector"] == "未分類", df.loc["T005", "sector"]
    assert df.loc["T007", "sector"] == str(int(F.loc[7, "sic"]) // 100)
    # 無 SIC 的 ≥5 檔、都深跌：不得因「同為未分類」被標 T5 產業恐慌
    F2, P2 = make()
    F2["sic"] = F2["sic"].astype(float)
    F2.loc[range(10, 17), "sic"] = np.nan
    P2.loc[range(10, 17), "dd52"] = -0.6
    d2 = S.structural(S.build(F2, P2, None, ASOF), ASOF).set_index("ticker")
    assert not any("T5_" in str(d2.loc[f"T{i:03d}", "distress_codes"]) for i in range(10, 17))


def test_fiscal_transition_warn_in_data_notes():
    """annual_series 的會計年度變更過渡年警示併入 features() 的 data_notes（A3-M1C-01）。"""
    rows = [dict(start=f"{y - 1}-07-01", end=f"{y}-06-30", val=1e9, form="10-K", fy=y, fp="FY",
                 filed=f"{y}-08-20", accn=f"A-{y}") for y in range(2015, 2020)]
    rows += [dict(start=f"{y}-01-01", end=f"{y}-12-31", val=2e9, form="10-K", fy=y, fp="FY",
                  filed=f"{y + 1}-02-20", accn=f"B-{y}") for y in range(2019, 2026)]
    facts = {"entityName": "Trans Co", "facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": rows}}}}}
    s = FE.annual_series(facts, ["NetIncomeLoss"], ASOF)
    assert s.attrs["warn"], "前提：annual_series 應有過渡年警示"
    out = FE.features("TRN", "0000000001", facts, "3812", ASOF, False)
    assert "FY2019：同財年有兩個年度期末" in out["data_notes"], out["data_notes"]
    assert out["data_notes"].endswith("盈餘＝GAAP 淨利代理")


def test_fetch_prices_drops_asof_month_bar():
    """Yahoo 月線＋歷史 asof：含 asof 的當月 K 線（收盤＝抓取當下）不用（A3-M1C-09）；trader 日線呼叫預設不變。"""
    import fetch_prices as FP
    dates = pd.date_range("2022-01-01", "2026-10-01", freq="MS")
    s = pd.DataFrame({"date": dates, "adj_close": np.arange(len(dates)) + 10.0, "close": np.arange(len(dates)) + 10.0})
    a = FP.feats(s, pd.Timestamp(ASOF))
    assert a["price_date"] == "2026-10-01" and a["price"] == s.close.iloc[-1]
    b = FP.feats(s, pd.Timestamp(ASOF), drop_asof_month=True)
    assert b["price_date"] == "2026-09-01" and b["price"] == s.close.iloc[-2], b
    assert "vol_36m" in b and b["drawdown_3y"] == 0


def test_price_date_after_asof_aborts():
    """prices.csv 的 price_date 晚於 --asof → screen.py 中止（A3-M1E-06）。"""
    F, P = make()
    assert S.check_price_dates(P, ASOF) is None
    P.loc[4, "price_date"] = "2026-10-16"
    msg = S.check_price_dates(P, ASOF)
    assert msg and "T004" in msg and "前視" in msg, msg
    with tempfile.TemporaryDirectory() as d:
        fp, pp = os.path.join(d, "f.csv"), os.path.join(d, "p.csv")
        F.to_csv(fp, index=False); P.to_csv(pp, index=False)
        old = sys.argv
        sys.argv = ["screen.py", "--features", fp, "--prices", pp, "--asof", ASOF, "--out", os.path.join(d, "r")]
        try:
            S.main(); raise AssertionError("應因 price_date > asof 中止")
        except SystemExit as e:
            assert "前視" in str(e.code), e.code
        finally:
            sys.argv = old
        assert not os.path.exists(os.path.join(d, "r.csv"))


def test_fetch_edgar_cache_refetch_when_older_than_asof():
    """cached(asof)：快取最後修改日早於 asof → 重抓；重抓失敗退回舊檔並註記；未給 asof 照舊有檔即用（A3-M1E-05）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "CIK0000000002.json")
        with open(path, "w") as f:
            json.dump({"v": "old"}, f)
        t_old = pd.Timestamp("2026-01-05 12:00").timestamp()
        os.utime(path, (t_old, t_old))
        old_get = FE.get
        try:
            n = []
            FE.get = lambda url: (n.append(url), {"v": "new"})[1]
            assert FE.cached(path, "u") == {"v": "old"} and n == []                   # 未給 asof：不重抓
            assert FE.cached(path, "u", "2026-01-05") == {"v": "old"} and n == []     # 同日不算早於
            os.utime(path, (t_old, t_old))

            def boom(url):
                raise urllib.error.URLError("down")
            FE.get = boom
            notes = []
            assert FE.cached(path, "u", ASOF, notes) == {"v": "old"}
            assert len(notes) == 1 and "沿用舊快取" in notes[0] and "2026-01-05" in notes[0], notes
            FE.get = lambda url: (n.append(url), {"v": "new"})[1]
            notes = []
            assert FE.cached(path, "u", ASOF, notes) == {"v": "new"} and n == ["u"] and notes == []
            with open(path) as f:
                assert json.load(f) == {"v": "new"}
            assert FE.cached(path, "u", ASOF) == {"v": "new"} and n == ["u"]          # 新檔 mtime ≥ asof：命中快取
        finally:
            FE.get = old_get


def test_fetch_edgar_stale_cache_403_still_aborts():
    """快取過期＋重抓一律 403：不退回舊檔吞掉 403，連續 MAX_403_STREAK 檔即中止、不寫輸出（A3-M1E-05）；
    代號對照表重抓 403 → SystemExit 提示 SEC_USER_AGENT。"""
    with tempfile.TemporaryDirectory() as tmp:
        cache = os.path.join(tmp, "cache")
        os.makedirs(cache)
        tickers = [f"T{i:02d}" for i in range(12)]
        names = {str(i): {"cik_str": 1000 + i, "ticker": t} for i, t in enumerate(tickers)}
        with open(os.path.join(cache, "company_tickers.json"), "w") as f:
            json.dump(names, f)
        for i in range(len(tickers)):
            c = str(1000 + i).zfill(10)
            with open(os.path.join(cache, f"CIK{c}.json"), "w") as f:
                json.dump(_ni_facts(range(2016, 2025)), f)
            with open(os.path.join(cache, f"sub_CIK{c}.json"), "w") as f:
                json.dump({"sic": "3500"}, f)
        t_old = pd.Timestamp("2025-01-05 12:00").timestamp()
        for fn in os.listdir(cache):
            os.utime(os.path.join(cache, fn), (t_old, t_old))

        def fake(url):
            if url.endswith("company_tickers.json"):
                return names
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        calls, df, err = _run_main(tmp, tickers, fake)
        assert err and "SEC 拒絕存取" in err and "SEC_USER_AGENT" in err, err
        assert len(calls) == 1 + FE.MAX_403_STREAK, calls   # 代號表＋5 檔（各 1 次 companyfacts），之後不再送出
        assert df is None
        # 代號對照表本身過期且 403
        os.utime(os.path.join(cache, "company_tickers.json"), (t_old, t_old))

        def deny(url):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        calls, df, err = _run_main(tmp, tickers, deny)
        assert err and "代號對照表" in err and "SEC_USER_AGENT" in err, err
        assert len(calls) == 1 and df is None
        # 非 403 的重抓失敗仍退回舊檔（不中止）
        calls, df, err = _run_main(tmp, tickers[:2], lambda url: (_ for _ in ()).throw(urllib.error.URLError("down")))
        assert err is None and df is not None and len(df) == 2
        assert df["data_notes"].fillna("").str.contains("沿用舊快取").all(), df["data_notes"].tolist()


if __name__ == "__main__":
    # 跑本檔全部 test_* 函式（A3-M1A-04：舊版只跑 test_all 串呼叫的那些，漏了兩項）
    _tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    for _n, _f in _tests:
        _f()
    print(f"全部通過（{len(_tests)} 項）")
