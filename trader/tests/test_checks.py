"""trader.analysis.checks 的測試：閘門 3、cap_calc 匯入與子程序一致、訊號卡一致性、閘門降級。"""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from trader.analysis.checks import (
    CheckError,
    absurd_line,
    apply_cap_override,
    cap_signal_by_gates,
    compare_signal_with_cap,
    run_cap_calc,
    run_gate3,
    worse_grade,
)
from trader.paths import get_paths

PATHS = get_paths()
CAP_SCRIPT = PATHS.skill_deep / "scripts" / "cap_calc.py"
CAP_EXAMPLE = PATHS.skill_deep / "assets" / "cap_calc_example_input.json"


# ---------------------------------------------------------------- 閘門 3

def _passing_answers() -> dict:
    """11 題全部通過、護城河三測 M1–M3 中 2 項為強、作答前未看價格。"""
    answers = {
        "C1": "10年寫得出", "C2": "是", "C3": "是", "C4": "是", "C5": "不屬於",
        "M1": "強", "M2": "強", "M3": "弱", "M4": "是", "M5": "變寬", "M6": "偉大",
    }
    return {
        "ticker": "TEST",
        "decision_date": "2026-10-16",
        "answered_by": "agent",
        "answered_at": "2026-10-01 10:00 Asia/Taipei",
        "price_seen_before_answering": False,
        "price_file_opened_at": "2026-10-01 11:00 Asia/Taipei",
        "answers": {
            qid: {
                "answer": ans,
                "reason": "測試理由",
                "evidence": [{"source": "10-K 第 1 頁", "date": "2026-09-30"}],
            }
            for qid, ans in answers.items()
        },
    }


def _write_json(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


def test_run_gate3_passing_answers(tmp_path):
    r = run_gate3(_write_json(tmp_path / "a.json", _passing_answers()))
    assert r["gate3_pass"] is True
    assert r["failures"] == []
    assert r["strong_count"] == 2


def test_run_gate3_missing_question_fails(tmp_path):
    ans = _passing_answers()
    del ans["answers"]["M6"]
    r = run_gate3(_write_json(tmp_path / "a.json", ans))
    assert r["gate3_pass"] is False
    assert any("M6 未作答" in f for f in r["failures"])


def test_run_gate3_price_seen_fails(tmp_path):
    ans = _passing_answers()
    ans["price_seen_before_answering"] = True
    r = run_gate3(_write_json(tmp_path / "a.json", ans))
    assert r["gate3_pass"] is False


def test_run_gate3_missing_file_does_not_raise(tmp_path):
    r = run_gate3(tmp_path / "nope.json")
    assert r["gate3_pass"] is False
    assert r["failures"] and "不存在" in r["failures"][0]


def test_run_gate3_bad_json_does_not_raise(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    r = run_gate3(p)
    assert r["gate3_pass"] is False
    assert r["failures"]


def test_run_gate3_non_object_json_does_not_raise(tmp_path):
    r = run_gate3(_write_json(tmp_path / "list.json", [1, 2, 3]))
    assert r["gate3_pass"] is False


def test_run_gate3_malformed_content_fails_closed(tmp_path):
    # answers 不是 dict：evaluate 會在內部拋例外，必須轉為未通過
    ans = _passing_answers()
    ans["answers"] = ["not", "a", "dict"]
    r = run_gate3(_write_json(tmp_path / "a.json", ans))
    assert r["gate3_pass"] is False


# ---------------------------------------------------------------- cap_calc

def test_run_cap_calc_matches_cli_output():
    """匯入的 calc 與 `python cap_calc.py 該檔` 的 stdout（JSON）數值完全一致。"""
    proc = subprocess.run(
        [sys.executable, str(CAP_SCRIPT), str(CAP_EXAMPLE)],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(proc.stdout) == run_cap_calc(CAP_EXAMPLE)


def test_run_cap_calc_example_has_contract_keys():
    res = run_cap_calc(CAP_EXAMPLE)
    for key in ("IV_cons", "IV_base", "IV_opt", "IV0", "cap", "auto_buy_cap", "r_d", "W_PE", "conclusion_hint"):
        assert key in res
    assert res["r_d"] == pytest.approx(0.10)  # max(0.10, 0.047 + 0.03)


def test_run_cap_calc_rounds_to_four_places():
    res = run_cap_calc(CAP_EXAMPLE)
    for v in (res["IV_base"], res["IV0"]):
        assert v is None or round(v, 4) == v


def test_run_cap_calc_missing_file_raises(tmp_path):
    with pytest.raises(CheckError):
        run_cap_calc(tmp_path / "nope.json")


def test_run_cap_calc_bad_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{", encoding="utf-8")
    with pytest.raises(CheckError):
        run_cap_calc(p)


def test_run_cap_calc_missing_required_key_raises(tmp_path):
    x = json.loads(CAP_EXAMPLE.read_text(encoding="utf-8"))
    del x["grade"]
    with pytest.raises(CheckError):
        run_cap_calc(_write_json(tmp_path / "in.json", x))


# ---------------------------------------------------------------- cap_calc 人工算例（期望值由公式手算，不取自程式）

def _cap(tmp_path, **kw):
    x = {"oe": 5.0, "g10": 0.07, "g5": 0.06, "grade": "B", "long_rate": 0.04, "price": 40.0, **kw}
    return run_cap_calc(_write_json(tmp_path / "in.json", x))


def test_golden_declining_history_caps_at_k_times_iv0(tmp_path):
    # r_d＝max(10%, 4%＋3pp)＝10%；IV0＝5÷0.10＝50；B 級 ×0.75 → 37.5（P2′ 與 IV_base≈51.1 都較高）
    r = _cap(tmp_path, g10=-0.05, g5=-0.02)
    assert r["r_d"] == pytest.approx(0.10)
    assert r["IV0"] == pytest.approx(50.0)
    assert r["cap"] == pytest.approx(37.5) and r["auto_buy_cap"] == pytest.approx(37.5)
    assert r["components"]["IV0_cap"] == pytest.approx(37.5)


def test_golden_zero_growth_conservative_is_perpetuity(tmp_path):
    # g＝0、保守終值 0% → IV_cons＝E÷r_d＝50；歷史成長 ≤0 → 上限 0.75×50＝37.5
    r = _cap(tmp_path, g10=0.0, g5=0.0)
    assert r["IV_cons"] == pytest.approx(50.0, abs=1e-3)
    assert r["auto_buy_cap"] == pytest.approx(37.5)


def test_golden_financial_override_uses_p1(tmp_path):
    # 金融：不做 P2′、無 P/E 中位 → 上限＝P1＝A 級 0.85×IV_cons 40＝34；r_stress＝max(10%, 10%)＝r_d → P3＝34
    r = _cap(tmp_path, financial=True, grade="A", g10=None, g5=None, iv_cons=40.0, iv_base=50.0, iv_opt=60.0, price=30.0)
    assert r["auto_buy_cap"] == pytest.approx(34.0)
    assert r["components"]["P2_prime"] is None
    assert r["components"]["P3_report_only"] == pytest.approx(34.0)


def test_golden_explicit_null_equals_missing(tmp_path):
    a = _cap(tmp_path, base_g0=None, opt_g0=None, iv_cons=None, iv_base=None, iv_opt=None)
    b = _cap(tmp_path)
    assert a["auto_buy_cap"] == b["auto_buy_cap"] and a["IV_base"] == b["IV_base"] is not None


def test_golden_low_rate_start_adds_stress_premium(tmp_path):
    # 30Y 8% < 20 年中位 9% → 低利率起點：r_d＝11%，r_stress＝8%＋6pp＋1pp＝15%；IV0＝5÷0.11
    r = _cap(tmp_path, long_rate=0.08, long_rate_20y_med=0.09)
    assert r["low_rate_start"] is True
    assert r["r_d"] == pytest.approx(0.11) and r["r_stress"] == pytest.approx(0.15)
    assert r["IV0"] == pytest.approx(5 / 0.11, abs=1e-4)


def test_golden_negative_earnings_no_cap(tmp_path):
    r = _cap(tmp_path, oe=-1.0)
    assert r["cap"] is None and "無法估值" in r["conclusion_hint"]
    assert absurd_line({"oe": -1.0}) is None and absurd_line({"oe": 5.0, "norm_eps": 4.0}) == 200.0


@pytest.mark.parametrize("bad,msg", [
    ({"g10": 7.0}, "g10"),                        # 7 而非 0.07
    ({"long_rate": 4.5}, "long_rate"),            # 百分點
    ({"oe": 5e6}, "price/oe"),                    # 以元計的總額
    ({"price": 0}, "price"),
    ({"grade": "B+"}, "grade"),
])
def test_cap_input_out_of_range_raises(tmp_path, bad, msg):
    with pytest.raises(CheckError, match=msg):
        _cap(tmp_path, **bad)


def test_worse_grade():
    assert worse_grade("A", "B") == "B" and worse_grade(None, "C") == "C" and worse_grade("A", None) == "A"


# ---------------------------------------------------------------- 一致性比對

CAP = {
    "auto_buy_cap": 50.0,
    "IV_cons": 40.0,
    "IV_base": 50.0,
    "IV_opt": 70.0,
    "IV0": 30.0,
}
CARD = {
    "auto_buy_cap": 50.0,
    "value_per_share": {"conservative": 40.0, "base": 50.0, "optimistic": 70.0, "zero_growth_IV0": 30.0},
}


def test_compare_identical_is_consistent():
    assert compare_signal_with_cap(CARD, CAP) == []


def test_compare_within_tolerance_is_consistent():
    card = copy.deepcopy(CARD)
    card["auto_buy_cap"] = 50.0 * 1.004  # 相對差約 0.40% < 0.5%
    assert compare_signal_with_cap(card, CAP) == []


def test_compare_beyond_tolerance_reports_field():
    card = copy.deepcopy(CARD)
    card["auto_buy_cap"] = 50.0 * 1.01  # 相對差約 1% > 0.5%
    out = compare_signal_with_cap(card, CAP)
    assert len(out) == 1
    assert "auto_buy_cap" in out[0]


def test_compare_custom_tolerance():
    card = copy.deepcopy(CARD)
    card["value_per_share"]["base"] = 50.0 * 1.01
    assert compare_signal_with_cap(card, CAP, rel_tol=0.02) == []
    assert len(compare_signal_with_cap(card, CAP, rel_tol=0.005)) == 1


def test_compare_both_none_is_consistent():
    card = {"auto_buy_cap": None, "value_per_share": {"conservative": None}}
    cap = {"auto_buy_cap": None, "IV_cons": None}
    assert compare_signal_with_cap(card, cap) == []


def test_compare_one_side_none_is_inconsistent():
    card = copy.deepcopy(CARD)
    card["auto_buy_cap"] = None
    out = compare_signal_with_cap(card, CAP)
    assert any("auto_buy_cap" in s for s in out)

    card = copy.deepcopy(CARD)
    card["value_per_share"]["conservative"] = None  # cap 有值
    out = compare_signal_with_cap(card, CAP)
    assert any("conservative" in s for s in out)


def test_compare_non_numeric_is_inconsistent():
    card = copy.deepcopy(CARD)
    card["auto_buy_cap"] = "50"
    out = compare_signal_with_cap(card, CAP)
    assert any("auto_buy_cap" in s for s in out)


def test_compare_zero_zero_is_consistent():
    assert compare_signal_with_cap({"auto_buy_cap": 0.0}, {"auto_buy_cap": 0.0}) == []


def test_apply_override_then_compare_is_consistent_with_example():
    cap = run_cap_calc(CAP_EXAMPLE)
    card = {"auto_buy_cap": 1.0, "value_per_share": {"conservative": 2.0}, "signal": "WATCH"}
    before = copy.deepcopy(card)
    out = apply_cap_override(card, cap)
    assert card == before  # 不修改傳入的訊號卡
    assert compare_signal_with_cap(out, cap) == []
    assert out["auto_buy_cap"] == cap["auto_buy_cap"]
    assert out["value_per_share"]["base"] == cap["IV_base"]
    assert out["sell_review_above"] == cap["IV_base"]
    assert out["signal"] == "WATCH"  # 其他欄位不動
    notes = out["framework_notes"]
    assert any("auto_buy_cap" in n and "1.0" in n for n in notes)  # 記錄覆寫前後


def test_apply_override_keeps_existing_notes():
    out = apply_cap_override({"framework_notes": ["原有註記"]}, CAP)
    assert out["framework_notes"][0] == "原有註記"
    assert len(out["framework_notes"]) > 1


# ---------------------------------------------------------------- 閘門降級

PASS_GATE = {"gate3_pass": True, "failures": []}
FAIL_GATE = {"gate3_pass": False, "failures": ["X 未作答"]}


def test_buy_with_gate3_failed_becomes_watch():
    sig, reasons = cap_signal_by_gates("BUY", FAIL_GATE, "PASS")
    assert sig == "WATCH"
    assert reasons


def test_buy_with_gate3_missing_becomes_watch():
    sig, reasons = cap_signal_by_gates("BUY", None, "PASS")
    assert sig == "WATCH"
    assert reasons


def test_buy_with_gate3_pass_stays_buy():
    assert cap_signal_by_gates("BUY", PASS_GATE, "PASS") == ("BUY", [])


def test_buy_with_delete_screen_becomes_watch():
    sig, reasons = cap_signal_by_gates("BUY", PASS_GATE, "刪除（低品質）")
    assert sig == "WATCH"
    assert reasons


def test_buy_with_delete_code_in_english_becomes_watch():
    sig, _ = cap_signal_by_gates("BUY", PASS_GATE, "DELETE")
    assert sig == "WATCH"


def test_watch_unchanged():
    assert cap_signal_by_gates("WATCH", PASS_GATE, "PASS") == ("WATCH", [])
    assert cap_signal_by_gates("WATCH", None, "刪除") == ("WATCH", [])


def test_no_action_unchanged():
    assert cap_signal_by_gates("NO_ACTION", None, None) == ("NO_ACTION", [])


def test_pass_screen_is_not_delete():
    assert cap_signal_by_gates("BUY", PASS_GATE, "PASS-困境") == ("BUY", [])


def test_gate3_pass_must_be_true_not_truthy():
    sig, _ = cap_signal_by_gates("BUY", {"gate3_pass": "yes"}, "PASS")
    assert sig == "WATCH"


# ---------------------------------------------------------------- B2 閘門 2 程式核對

def test_buy_with_price_above_cap_becomes_watch():
    cap = {"auto_buy_cap": 50.0, "price": 55.0, "grade_used": "B"}
    sig, reasons = cap_signal_by_gates("BUY", PASS_GATE, "PASS", cap)
    assert sig == "WATCH" and "閘門 2" in reasons[0]


@pytest.mark.parametrize("cap", [
    {"auto_buy_cap": None, "price": 40.0, "grade_used": "B"},
    {"auto_buy_cap": 50.0, "price": 40.0, "grade_used": "C"},
    {"auto_buy_cap": 50.0, "price": 49.0, "grade_used": "A", "conclusion_test": {"flips": True}},
])
def test_buy_gate2_other_failures(cap):
    assert cap_signal_by_gates("BUY", PASS_GATE, "PASS", cap)[0] == "WATCH"


def test_buy_gate2_pass_stays_buy():
    assert cap_signal_by_gates("BUY", PASS_GATE, "PASS", {"auto_buy_cap": 50.0, "price": 40.0, "grade_used": "A"}) == ("BUY", [])


def test_golden_financial_conclusion_test_uses_e_haircut(tmp_path):
    # 金融覆寫 IV_cons＝40、A 級：上限 34；現價 33（距上限 <5%）→ E×0.9 → 0.85×40×0.9＝30.6 < 33 → 翻轉
    r = _cap(tmp_path, financial=True, grade="A", g10=None, g5=None, iv_cons=40.0, iv_base=50.0, iv_opt=60.0, price=33.0)
    ct = r["conclusion_test"]
    assert ct["P2_prime_with_E_x0.9"] == pytest.approx(30.6) and ct["flips"] is True


def test_golden_financial_cap_is_min_of_p1_and_p4(tmp_path):
    # 金融：P1＝0.85×40＝34；P4′＝min(20, 15×1.1)×E 5＝82.5 → 硬上限取 34（P4′ 不可單獨當硬上限）
    r = _cap(tmp_path, financial=True, grade="A", g10=None, g5=None, iv_cons=40.0, iv_base=50.0, iv_opt=60.0,
             price=30.0, largecap_pe_median=20.0, fin_pe_median=15.0)
    assert r["components"]["P4p"] == pytest.approx(82.5)
    assert r["cap"] == pytest.approx(34.0) and r["auto_buy_cap"] == pytest.approx(34.0)
