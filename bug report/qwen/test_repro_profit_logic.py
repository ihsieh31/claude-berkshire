"""對抗式審查用的重現測試（只讀專案原始檔，不寫入專案任何目錄）。

執行：
    cd "/Users/zongen/Downloads/claude code/berkshire/bug report/qwen"
    python3 -m pytest -q test_repro_profit_logic.py

每個測試都在重現一個「已從程式碼讀出」的具體行為；斷言失敗代表該行為已不存在（修正或我讀錯）。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path("/Users/zongen/Downloads/claude code/berkshire")
sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
sys.path.insert(0, str(ROOT / "berkshire-screen-merged" / "scripts"))

import cap_calc  # noqa: E402
import gate3_check  # noqa: E402
import review_due  # noqa: E402

DEEP = ROOT / "berkshire-deep-merged"


def _px(closes, start=date(2026, 6, 1)) -> pd.DataFrame:
    days = pd.date_range(start, periods=len(closes), freq="D").date
    return pd.DataFrame({"ticker": "X", "date": days, "close": list(closes)})


# ---------------------------------------------------------------- cap_calc：上限與賣出線的綁定

def test_auto_buy_cap_survives_when_iv_base_missing():
    """SKILL §3／README：auto_buy_cap＝min(P2′, IV_base)（買價不得高於賣出檢視線）。
    但 cap_calc.py:161 用 `if iv_base` 判斷；IV_base 缺（無 g10/g5 且無 base_g0）時直接退回 P2′，
    一致性條款無聲消失，輸出仍是「可買」的 auto_buy_cap，而 sell_review_above（＝IV_base）為 None。"""
    x = {"oe": 5.0, "price": 80.0, "grade": "A", "long_rate": 0.05,
        "largecap_pe_median": 19.0, "iv_cons": 60.0}          # norm_eps 缺 → eps＝oe
    r = cap_calc.calc(x)
    assert r["IV_base"] is None, "前提：無歷史成長期間 → IV_base 未算出"
    assert r["auto_buy_cap"] == pytest.approx(r["cap"]), "一致性條款失效：auto_buy_cap 退回 P2′"
    assert r["auto_buy_cap"] > r["IV0"], f"買價上限高於零成長價值 IV0（{r['IV0']:.1f}）"
    assert r["auto_buy_cap"] > r["IV_cons"], "上限高於保守內在價值 68%（P2′ 只約束隱含成長）"
    assert "價格 < 自動買進上限" in r["conclusion_hint"], "缺輸入卻回報『可買』語氣"
    assert r["conclusion_hint"] != "WATCH"
    # 下游：sell_review_above＝cap["IV_base"]（writeback.py:561、review_jobs.py:311）→ None → 賣出檢視線消失
    assert r["IV_base"] is None


def test_zero_iv_base_also_disables_the_clause():
    """同一真值判斷：IV_base 恰為 0（或極小）時也跳過 min()，買進上限完全不受賣出線約束。"""
    x = {"oe": 5.0, "price": 90.0, "grade": "A", "long_rate": 0.05,
         "iv_cons": 60.0, "iv_base": 0.0}
    r = cap_calc.calc(x)
    assert r["auto_buy_cap"] == pytest.approx(r["cap"])
    assert r["auto_buy_cap"] > 0, "上限仍可買，但賣出檢視線為 0 → 一買進就已超過賣出線"


def test_declining_company_cap_is_not_restricted_to_iv0():
    """SKILL §3：「歷史成長 ≤0 → 上限只能用 IV0（E÷r_d）×等級係數」。
    cap_calc.py:133-134 只加註記，cap/auto_buy_cap 仍由 P2′（隱含成長 10%）與 IV_base
    （基準情境終值 +3%）決定，未套用 IV0×等級係數。"""
    x = {"oe": 5.0, "price": 40.0, "grade": "A", "long_rate": 0.05, "g10": -0.02, "g5": -0.01}
    r = cap_calc.calc(x)
    iv0_grade = r["IV0"] * 0.85                      # 規則要求：A 級上限 = IV0 × 0.85
    assert any("歷史成長 ≤0" in n for n in r["notes"]), "規則只在註記裡出現"
    assert r["auto_buy_cap"] > iv0_grade, (
        f"收縮型公司上限 {r['auto_buy_cap']:.1f} > 規則允許的 IV0×0.85 = {iv0_grade:.1f}")


def test_p21_width_check_skipped_when_iv_opt_missing():
    """P-21（IV_opt/IV_cons >3 降級、>4 不做）只在 iv_opt 存在時評估；
    缺樂觀情境（模型未給 opt_g0/g10/g5）→ 降級與「不做」都自動跳過，上限照給。"""
    base = {"oe": 5.0, "price": 100.0, "grade": "A", "long_rate": 0.05, "iv_cons": 20.0}
    r = cap_calc.calc(dict(base))
    assert r["width_opt_over_cons"] is None
    assert "P-21" not in " ".join(r["notes"])
    assert r["auto_buy_cap"] is not None, "區間寬度無法評估時，規則應視為缺資料而非放行上限"


def test_conclusion_test_earnings_leg_ignores_stressed_earnings():
    """結論測試要求「E×0.9 重算 P2′」；iv_cons 為覆寫值時（銀行等另算者）
    cap_calc.py:116-122 的 ivc_at() 完全忽略 oe_ 參數 → E×0.9 那一條腿等於沒測。"""
    x = {"oe": 5.0, "price": 97.0, "grade": "A", "long_rate": 0.05, "iv_cons": 100.0,
         "financial": True, "largecap_pe_median": 20.0}
    r = cap_calc.calc(x)
    ct = r.get("conclusion_test") or {}
    assert ct["P2_prime_with_E_x0.9"] == pytest.approx(r["components"]["P1_report_only"]), (
        "E×0.9 的結果與未壓力的 P1 相同：壓力測試形同虛設（語意與規則不同，方向偏保守）")


# ---------------------------------------------------------------- gate3_check：可稽核性

def _good_answers():
    q = __import__("json").load(open(DEEP / "assets" / "gate3_questionnaire.json", encoding="utf-8"))
    ans = {}
    for item in q["questions"]:
        ans[item["id"]] = {"answer": item["pass_if"][0], "reason": "r",
                           "evidence": [{"source": "10-K p.31", "date": "2026-03-01"}]}
    # 護城河三測要兩項以上「強」
    ans["M1"]["answer"] = ans["M2"]["answer"] = "強"
    return {"ticker": "X", "decision_date": "2026-10-15", "answered_at": "2026-10-15 10:00",
            "price_seen_before_answering": False, "answers": ans}


def test_gate3_accepts_non_date_evidence_strings():
    """gate3_check.py:38 只做字串比較，不驗證日期格式：
    「2020」「2025-12」「0」這類不完整／非日期字串都比決策日「小」，前視檢查形同裝飾。"""
    a = _good_answers()
    for junk in ("2020", "2025-12", "0", "1999"):
        a["answers"]["C2"]["evidence"] = [{"source": "年報", "date": junk}]
        r = gate3_check.evaluate(a)
        assert r["gate3_pass"], f"非 ISO 日期字串 {junk!r} 仍通過前視檢查"


def test_gate3_price_file_timestamp_is_optional():
    """價格隔離靠模型自報 price_file_opened_at；欄位留空時 gate3_check.py:26-28 的檢查直接跳過，
    問卷仍可 gate3_pass＝True。"""
    a = _good_answers()
    assert "price_file_opened_at" not in a or a.get("price_file_opened_at") is None
    r = gate3_check.evaluate(a)
    assert r["gate3_pass"] and "price_file_opened_at" not in a


def test_gate3_crashes_on_malformed_evidence_instead_of_failing():
    """evidence 若為字串清單（模型常見輸出），.get() 拋 AttributeError →
    驗證器當機而不是回傳 gate3_pass=False。"""
    a = _good_answers()
    a["answers"]["C2"]["evidence"] = ["10-K p.31, 2026-03-01"]
    with pytest.raises(AttributeError):
        gate3_check.evaluate(a)


def test_questionnaire_claims_every_question_allows_unknown():
    """gate3_questionnaire.json 的 _說明宣稱「每題都可答不知道」，
    但 C1／M5／M6 的 choices 沒有「不知道」；文件與資料不一致（模型若照說明知而選非選項，
    只是 fail；真正問題是規則文件不可稽核）。"""
    import json
    q = json.load(open(DEEP / "assets" / "gate3_questionnaire.json", encoding="utf-8"))
    missing = [i["id"] for i in q["questions"] if "不知道" not in (i.get("choices") or [])]
    assert "不知道" in q["_說明"]
    assert set(missing) >= {"C1", "M5", "M6"}, missing


# ---------------------------------------------------------------- review_due：賣出路徑的 fail-open

def _reg(**kw):
    base = {"ticker": "X", "status": "HOLDING", "last_review_date": "2026-09-01",
            "last_full_review_date": "2026-09-01", "cost": 50.0,
            "sell_review_above": None, "absurd_price_line": None,
            "auto_buy_cap": None, "weight": 0.05, "target_weight": None}
    return {**base, **kw}


def test_sell_lines_wiped_to_none_silently_disable_all_sell_checks():
    """write_price_result（writeback.py:560-562）把 sell_review_above 覆寫成 cap["IV_base"]、
    absurd_price_line 覆寫成 50×E；當 cap_calc 回傳 None 時登錄表欄位被清空，
    review_due 之後再也不觸發任何賣出檢查——即使股價已漲到成本的 6 倍。"""
    px = _px([50 + i for i in range(60)])          # 一路漲到 109
    e = _reg()
    act, why, tri = review_due.check(e, [], px, "2026-10-15", 0.05)
    assert act == "NONE", f"賣出線為 None 時完全無賣出觸發：{why}"
    # 對照組：同一價格，登錄表仍有賣出線 → PRICE（H2 Q3 賣出檢視）
    e2 = _reg(sell_review_above=60.0)
    act2, why2, _ = review_due.check(e2, [], px, "2026-10-15", 0.05)
    assert act2 == "PRICE" and any("sell_review_above" in w for w in why2)


def test_missing_price_data_fails_open_for_selling():
    """review_due.check() 在 px 為 None/空時，所有價格類觸發（含荒謬級線、成本 −12%）都不評估，
    而且 check() 本身不回傳任何警告（警告只在 main() 附加）。
    物價缺資料＝不賣出：對沒有價格的持股（下市、抓取失敗）永遠不會走到賣出檢查。"""
    e = _reg(sell_review_above=60.0, absurd_price_line=100.0, cost=50.0)
    act, why, _ = review_due.check(e, [], None, "2026-10-15", 0.05)
    assert act == "NONE"
    assert not any("無價格" in w for w in why), "check() 不回報『價格類觸發未檢查』"


def test_add_trigger_requires_target_weight():
    """加碼檢查（review_due.py:123）用 `(weight or 0) < (target_weight or 0)`；
    登錄表若沒有 target_weight（None 視同 0），持股永遠不會因『低於上限且未達目標』排入 PRICE。"""
    px = _px([40.0] * 40)
    e = _reg(cost=40.0, auto_buy_cap=60.0, weight=0.05, target_weight=None)
    act, why, _ = review_due.check(e, [], px, "2026-10-15", 0.05)
    assert act == "NONE", why
    e2 = _reg(cost=40.0, auto_buy_cap=60.0, weight=0.05, target_weight=0.10)
    act2, why2, _ = review_due.check(e2, [], px, "2026-10-15", 0.05)
    assert act2 == "PRICE" and any("加碼" in w for w in why2)


def test_entry_without_last_review_date_fires_thesis_on_any_historic_move():
    """last_review_date 缺 → 以 1900-01-01 為基準，單日 ±4% 檢查會掃到整個價格歷史；
    任何一檔只要過去曾有過一次 ±4% 波動就永久 THESIS（分析成本失控）。"""
    closes = [100.0] * 30 + [90.0] + [90.0] * 30      # 一次 10% 單日跌幅，發生在很久以前
    px = _px(closes)
    e = _reg(last_review_date=None, sell_review_above=None)
    act, why, _ = review_due.check(e, [], px, "2026-10-15", 0.05)
    assert "上次分析後出現單日 ±4%" in " ".join(why)
    assert act == "THESIS"


def test_screen_price_columns_leak_into_special_watch_block():
    """screen.py:184 的 special_watch 走 records(...) 不經 split=True，
    因此 handoff JSON 的 special_watch 區塊帶著 price／market_cap／earn_yield／core_rank 等
    「G1–G6 不得看到」的欄位，而深度分析读的正是同一份 handoff 檔。"""
    sys.path.insert(0, str(ROOT / "berkshire-screen-merged" / "scripts"))
    import screen
    cols = [c for c in screen.HANDOFF_COLS]
    assert "price" in cols and "price_context" not in cols
    split = screen.records.__doc__ or ""
    assert split == ""                                     # 無文件說明 split 的必要性（可讀性）
    # 直接驗證：split=False 時價格欄位在頂層，未被移到 price_context
    df = pd.DataFrame([{"ticker": "X", "price": 10.0, "market_cap": 1e9, "core_rank": 1,
                        "lane": "特殊情境（深度回撤）", "drawdown_3y": -0.5}])
    rows = screen.records(df, split=False)
    assert rows[0]["price"] == 10.0 and "price_context" not in rows[0]
    rows2 = screen.records(df, split=True)
    assert "price" not in rows2[0] and rows2[0]["price_context"]["price"] == 10.0


def test_missing_sic_silently_disables_industry_panic_and_makes_guard_dead():
    """screen.py:61 用 astype("Int64").astype(str)：缺 SIC 者變成「float NaN 混在 str 欄位」，
    於是 (a) screen_quant.py:88 的 `sector != '未分類'` 守衛永遠不會命中（死代碼），
    (b) 這些公司在 groupby 中被丟棄 → T5「產業恐慌」對它們永不觸發（困境標記靜默消失，
        而 T5 是特殊情境道與步 4A 的入口之一）。
    同產業有 SIC 時正常觸發（對照組），證明是缺 SIC 本身造成。"""
    import screen_quant as SQ

    def frame(sics, n=6):
        live = pd.DataFrame({
            "ticker": [f"T{i}" for i in range(n)],
            "net_income": [1.0] * n, "shareholders_equity": [10.0] * n, "ni_prev": [1.0] * n,
            "loss_yrs5": [0] * n, "ni_sum5": [5.0] * n, "roe_med5": [0.2] * n, "roe": [0.2] * n,
            "eps_g1": [0.0] * n, "dd52": [-0.25] * n, "is_financial": [False] * n,
        })
        live["sector"] = pd.to_numeric(pd.Series(sics), errors="coerce").floordiv(100).astype("Int64").astype(str)
        return live

    # dd52 = -0.25：低於 T3 門檻（-0.30），所以唯一可能的困境標記就是 T5 產業恐慌
    no_sic = SQ.screen(SQ.from_live(frame([None] * 6), 2026), decision_date="2026-10-15")
    assert (no_sic["distress_codes"] == "").all(), f"缺 SIC 者應完全無標記：{no_sic['distress_codes'].tolist()}"

    with_sic = SQ.screen(SQ.from_live(frame(["7370"] * 6), 2026), decision_date="2026-10-15")
    assert (with_sic["distress_codes"] == "T5_產業恐慌").all(), "同產業 6 家同跌應標記產業恐慌（對照組）"

    # 欄位被污染成 str／NaN 混合，screen_quant 的 '未分類' 守衛在此路徑形同虛設
    assert "未分類" not in set(frame([None, None], 2)["sector"].astype(str))
