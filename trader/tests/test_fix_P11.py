"""P11：cap_calc 公式與輸入正規化（M2-B3 腳本端）＋ASK-1(B)＋D21＋D22。

條目：M2_D9_02、M2_D9_04、M2_D7_04（含 M2_D1_03）、M2_D1_06、M2_D1_04、M2_D9_10、M2-D5-10；
決策：ASK-1＝B（auto_buy_cap＝min(P2′, k×IV_base)）、D21（成長率口徑明顯錯誤）、D22（P-21 只適用非金融）、
D20 的 cap_calc 部分（金融類 IV_base 缺 → 無自動買進上限）、M2-D8-01 (b)（覆寫只限金融類）。
"""
import json
import math

import pytest

from trader.analysis.checks import (CheckError, _skill_module, apply_cap_override, cap_input_problems,
                                    gate2_failures, run_cap_calc, set_cap_report_fields)
from trader.paths import get_paths

C = _skill_module("cap_calc")
G3 = _skill_module("gate3_check")

BASE = {"long_rate": 0.047, "long_rate_20y_med": 0.035, "price": 1.0, "grade": "B", "oe": 1.0, "g10": 0.05, "g5": 0.05}


def calc(**kw):
    return C.calc({**BASE, **kw})


def _run(tmp_path, **kw):
    p = tmp_path / "in.json"
    p.write_text(json.dumps({**BASE, **kw}, allow_nan=True), encoding="utf-8")
    return run_cap_calc(p)


# ---------------------------------------------------------------- M2_D9_02 負值不得當價格線

def test_negative_oe_outputs_no_negative_lines_and_fixed_keys():
    r = calc(oe=-2.0, price=100.0)
    for k in ("IV_cons", "IV_base", "IV_opt", "IV0", "cap", "auto_buy_cap", "g_impl", "P1", "price_to_cons"):
        assert r[k] is None, k
    assert r["grade_used"] == "B" and r["price"] == 100.0
    assert r["no_value_reason"] == "iv_cons_le_0" and "無法估值" in r["conclusion_hint"]
    assert gate2_failures(r)  # 閘門 2 不過


def test_missing_growth_reason():
    r = calc(g10=None, g5=None)
    assert r["no_value_reason"] == "missing_growth" and r["auto_buy_cap"] is None and r["IV_base"] is None


# ---------------------------------------------------------------- M2_D9_04 輸入正規化

def test_string_bools_equal_json_bools():
    assert calc(financial="false", price=10.0) == calc(financial=False, price=10.0)
    assert calc(moat_widening="false")["IV_cons"] == calc(moat_widening=False)["IV_cons"]
    assert calc(moat_widening="true")["IV_cons"] == calc(moat_widening=True)["IV_cons"]


def test_bad_bool_and_nan_raise_check_error(tmp_path):
    with pytest.raises(CheckError, match="financial"):
        _run(tmp_path, financial="no")
    with pytest.raises(CheckError, match="oe"):
        _run(tmp_path, oe=float("nan"))
    with pytest.raises(ValueError):
        calc(oe=float("nan"))          # 必要欄非有限 → 缺 → 報錯（不得全 NaN 輸出）
    with pytest.raises(ValueError):
        calc(financial=1)


def test_optional_nan_is_missing_in_cap_calc_and_flagged_by_checks():
    assert calc(long_rate_20y_med=float("nan"))["low_rate_start"] is False
    probs = cap_input_problems({**BASE, "roe": float("inf"), "moat_widening": "yes", "price": float("nan")})
    assert any("roe" in p for p in probs) and any("moat_widening" in p for p in probs)
    assert any("price" in p for p in probs)
    assert cap_input_problems({**BASE, "financial": "false"}) == []


# ---------------------------------------------------------------- M2_D7_04／M2_D1_03 結論測試重算實際上限

def test_conclusion_test_recomputes_binding_auto_buy_cap():
    # D90：E×0.9 腿（實際綁定上限 ×0.9）在距上限 <5% 時必翻轉 → 明文化為 auto_buy_cap＝0.95×原始上限
    raw = calc()["auto_buy_cap_raw"]
    r = calc(price=raw * 0.97)
    assert r["auto_buy_cap"] == pytest.approx(raw * 0.95, rel=1e-9)
    assert "price_ge_cap" in r["watch_reasons"] and "conclusion_test" not in r
    assert gate2_failures(r)


def test_conclusion_test_financial_p4_binding_flips():
    x = dict(oe=6, g10=0.05, g5=0.05, financial=True, iv_cons=120, iv_base=200, iv_opt=220,
             largecap_pe_median=15, fin_pe_median=12, price=76.82)
    r = calc(**x)
    assert r["auto_buy_cap_raw"] == pytest.approx(79.2)
    assert r["auto_buy_cap"] == pytest.approx(79.2 * 0.95) and "price_ge_cap" in r["watch_reasons"]   # D90


# ---------------------------------------------------------------- M2_D1_06 保險＋覆寫

def test_insurance_with_override_uses_override_for_p3():
    r = calc(oe=6, financial=True, iv_cons=70, insurance={"tbv_ps": 50, "float_ps": 60, "float_cost": -0.02},
             largecap_pe_median=15, fin_pe_median=12)
    assert r["IV_cons"] == 70
    assert r["components"]["P3_report_only"] == pytest.approx(0.75 * 70 * r["r_d"] / r["r_stress"])


# ---------------------------------------------------------------- M2_D1_04 P4′ 1.2 倍條件

@pytest.mark.parametrize("v,mult", [(None, 1.0), (False, 1.0), (True, 1.2)])
def test_p4_multiplier_requires_incr_roic(v, mult):
    r = calc(grade="A", largecap_pe_median=20, incr_roic_gt_rd=v)
    assert r["P4_multiplier"] == mult and r["components"]["P4p"] == pytest.approx(mult * 20)


# ---------------------------------------------------------------- M2_D9_10 docstring 口徑

def test_docstring_oe_definition_matches_skill():
    doc = C.__doc__
    assert "GAAP 淨利已含股權薪酬費用，不加回也不再扣" in doc
    assert "已扣維持性資本支出差額與股權薪酬" not in doc


# ---------------------------------------------------------------- M2-D5-10 原因欄

def test_watch_reasons_and_gate3_timing_checked():
    r = calc(price=100.0, grade="C")
    assert "price_ge_cap" in r["watch_reasons"] and "grade_not_AB" in r["watch_reasons"]
    assert r["no_value_reason"] is None
    assert calc(price=0.5)["watch_reasons"] == []
    ans = {"decision_date": "2026-10-01", "answers": {}}
    assert G3.evaluate(ans)["timing_checked"] is False
    ans.update(answered_at="2026-10-01 10:00", price_file_opened_at="2026-10-01 11:00")
    assert G3.evaluate(ans)["timing_checked"] is True


# ---------------------------------------------------------------- ASK-1＝B

def test_auto_buy_cap_is_min_p2_and_k_times_iv_base():
    b, a = calc(grade="B"), calc(grade="A")
    m = 0.95   # D90：auto_buy_cap＝0.95 × min(P2′, k×IV_base)
    assert b["auto_buy_cap"] == pytest.approx(m * min(b["components"]["P2_prime"], 0.85 * b["IV_base"]))
    assert a["auto_buy_cap"] == pytest.approx(m * min(a["components"]["P2_prime"], 0.95 * a["IV_base"]))
    assert b["cap_discount_k"] == 0.85 and a["cap_discount_k"] == 0.95
    # 選 A（k＝1.0）回到舊式 min(P2′, IV_base)
    assert calc(cap_discount_k=1.0)["auto_buy_cap_raw"] == pytest.approx(b["IV_base"])
    assert calc(cap_discount_k={"B": 0.8})["auto_buy_cap"] == pytest.approx(m * 0.8 * b["IV_base"])
    # 買賣線之間留緩衝：自動買進上限 < 賣出檢視線（IV_base）
    assert b["auto_buy_cap"] < b["IV_base"]


def test_bad_cap_discount_k_rejected(tmp_path):
    for bad in (0, 1.5, "0.9", {"Z": 0.9}):
        assert any("cap_discount_k" in p for p in cap_input_problems({**BASE, "cap_discount_k": bad}))
        with pytest.raises(CheckError):
            _run(tmp_path, cap_discount_k=bad)


def test_g9_reason_required_when_periods_diverge():
    assert calc(g10=0.12, g5=0.05)["g9_reason_required"] is True
    assert calc(g10=0.08, g5=0.05)["g9_reason_required"] is False
    assert calc(g10=None, g5=0.05)["g9_reason_required"] is False
    # 已證明成長仍取較高者
    assert calc(g10=0.12, g5=0.05)["g0"]["base"] == pytest.approx(0.10)


def test_p1_and_price_to_cons_listed_on_result_and_card():
    r = calc(price=10.0)
    assert r["P1"] == pytest.approx(0.75 * r["IV_cons"]) == pytest.approx(r["components"]["P1_report_only"])
    assert r["price_to_cons"] == pytest.approx(10.0 / r["IV_cons"])
    card = set_cap_report_fields({"cap_components": {"P1_report_only": 1.0}}, r)
    assert card["cap_components"]["P1_report_only"] == r["P1"]
    assert card["cap_components"]["P2_prime_g_impl_10"] == r["components"]["P2_prime"]
    assert card["price_to_conservative"] == r["price_to_cons"]
    assert any("P1_report_only" in n for n in card["framework_notes"])
    assert apply_cap_override({}, r)["cap_components"]["P1_report_only"] == r["P1"]


# ---------------------------------------------------------------- M2_D9_04（P11R）insurance 空物件

@pytest.mark.parametrize("ins", [{}, {"tbv_ps": None, "float_ps": None, "float_cost": None}])
def test_empty_insurance_same_as_absent_for_non_financial(tmp_path, ins):
    base = calc(financial=False)
    r = calc(financial=False, insurance=ins)
    assert r == base  # 含 notes：不出現「忽略 insurance」
    assert _run(tmp_path, financial=False, insurance=ins)["auto_buy_cap"] == pytest.approx(base["auto_buy_cap"], abs=1e-4)


def test_non_financial_skips_insurance_validation():
    r = calc(financial=False, insurance={"tbv_ps": 1})  # 部分填寫：非金融照舊忽略、不報錯
    assert r["auto_buy_cap"] == pytest.approx(calc()["auto_buy_cap"])
    assert any("忽略" in n and "insurance" in n for n in r["notes"])


def test_financial_empty_insurance_is_missing_partial_raises(tmp_path):
    kw = dict(oe=6, financial=True, g10=0.05, g5=0.05, price=10.0)
    assert calc(insurance={}, **kw) == calc(**kw)
    with pytest.raises(ValueError):
        calc(insurance={"tbv_ps": 50, "float_ps": None, "float_cost": None}, **kw)
    with pytest.raises(CheckError):
        _run(tmp_path, insurance={"tbv_ps": 50}, **kw)


# ---------------------------------------------------------------- D21

def test_zero_and_near_zero_growth_caps_are_continuous():
    z, nz = calc(g10=0.0, g5=0.0), calc(g10=0.001, g5=0.001)
    assert z["auto_buy_cap"] == pytest.approx(nz["auto_buy_cap"], rel=0.02)   # 修前 7.5 → 11.9（+59%）
    assert nz["auto_buy_cap"] == pytest.approx(0.95 * 0.75 * nz["IV0"])   # D90
    assert calc(g10=0.01, g5=0.01)["components"]["IV0_cap"] is not None        # ≤1% 視為近零
    assert calc(g10=0.011, g5=0.011)["components"]["IV0_cap"] is None


def test_w_rev_uses_iv0_cap():
    r = calc(g10=0.08, g5=0.08, w_rev=True)
    assert r["components"]["IV0_cap"] == pytest.approx(0.75 * r["IV0"])
    assert r["auto_buy_cap"] <= 0.75 * r["IV0"] + 1e-12


def test_negative_growth_not_more_optimistic_than_zero():
    z, n2, n1 = calc(g10=0.0, g5=0.0), calc(g10=-0.04, g5=-0.04), calc(g10=None, g5=-0.04)
    assert n2["IV_cons"] <= z["IV_cons"] and n1["IV_cons"] <= z["IV_cons"]
    assert n1["IV_cons"] <= calc(g10=None, g5=-0.024)["IV_cons"]
    assert n2["g0"]["cons"] == pytest.approx(-0.04)  # 不乘 0.75
    assert n2["auto_buy_cap"] <= z["auto_buy_cap"]


def test_non_positive_history_iv_base_terminal_zero():
    r = calc(g10=0.0, g5=0.0)
    assert r["IV_base"] == pytest.approx(r["IV0"], rel=1e-9)  # g＝0 且永續 0% → E÷r_d
    assert calc(g10=-0.05, g5=-0.03)["IV_base"] < r["IV0"]


# ---------------------------------------------------------------- D22／D20

def test_p21_not_applied_to_financial_and_iv_base_required():
    ins = {"tbv_ps": 30, "float_ps": 60, "float_cost": -0.02}
    r = calc(oe=12, g10=0.15, g5=0.12, grade="A", financial=True, insurance=ins, largecap_pe_median=20, price=10.0)
    assert r["p21_applicable"] is False and r["grade_used"] == "A"
    assert r["IV_base"] is None and r["auto_buy_cap"] is None and "no_auto_buy_cap" in r["watch_reasons"]
    r2 = calc(oe=12, grade="A", financial=True, iv_cons=10, iv_opt=50, iv_base=20, price=5.0)  # 區間 5 倍
    assert r2["p21_applicable"] is False and r2["grade_used"] == "A" and r2["auto_buy_cap"] is not None
    assert calc()["p21_applicable"] is True


def test_overrides_ignored_for_non_financial():
    r = calc(iv_base=1.0, iv_cons=1e9)
    assert r["IV_base"] == pytest.approx(calc()["IV_base"]) and r["IV_cons"] == pytest.approx(calc()["IV_cons"])
    assert any("忽略" in n for n in r["notes"])


def test_example_input_still_buyable_and_cli_matches():
    ex = json.loads((get_paths().skill_deep / "assets" / "cap_calc_example_input.json").read_text(encoding="utf-8"))
    r = C.calc(ex)
    assert r["watch_reasons"] == [] and math.isfinite(r["auto_buy_cap"])
