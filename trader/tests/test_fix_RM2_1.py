"""RM2-1：FULL_B 看價後的 cap_input 欄位（M2-RS2-01 等級回寫、M2-RS3-02／M2_D4_03／M2-RS7-03 金融 iv_* 與
base_g0／opt_g0、M2-R4-01／M2-RS7-02 W-REV 由框架判定）。FakeRunner，不連網。"""
import json
from pathlib import Path

from test_engine import CAP_INPUT, RUN, dossier_ok, full_b, red_team
from test_fix_P20 import _env, _fa, _fb, _j, _notes, _td



def _cap(env):
    return _j(_td(env) / "cap_result.json")


def _ci(env):
    return _j(_td(env) / "cap_input.json")


# ---------------------------------------------------------------- M2-RS2-01：看價後降級回寫 cap_input.grade

def test_summary_downgrade_reaches_cap_input_and_state(tmp_path):
    """FULL_B 步 1 寫 grade A，步 10 因 SEL-034 例外／P-26a 降成 B（摘要 B）→ cap_calc 以 B 算、state/cap_inputs 存 B。"""
    b = _fb({**CAP_INPUT, "grade": "A"})   # 摘要 certainty_grade＝B（full_b 預設）
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa(), "FULL_B": b, "RED_TEAM": red_team(False)})
    assert env.engine.run(RUN)["status"] == "done"
    ci, cap = _ci(env), _cap(env)
    assert ci["grade"] == "B" and cap["grade_used"] == "B"
    assert abs(cap["auto_buy_cap"] - 0.95 * min(cap["cap"], 0.85 * cap["IV_base"])) < 1e-3   # D90
    assert any("看價後" in n and "A → B" in n for n in _notes(env))
    st = env.paths.state / "cap_inputs" / "KO.json"
    assert _j(st)["grade"] == "B"   # PRICE 快速通道沿用的版本也是 B
    assert len(env.by_type("FULL_B")) == 1   # 框架改等級造成的差異不退回


def test_card_grade_stricter_than_summary_also_used(tmp_path):
    b = _fb({**CAP_INPUT, "grade": "A"})
    b["certainty_grade"] = "A"
    card = json.loads(b["files"]["KO/signal_card.json"])
    card["certainty_grade"] = "B"
    b["files"] = {**b["files"], "KO/signal_card.json": json.dumps(card, ensure_ascii=False)}
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa(), "FULL_B": b, "RED_TEAM": red_team(False)})
    env.engine.run(RUN)
    assert _ci(env)["grade"] == "B" and _cap(env)["grade_used"] == "B"


def _p21_case(tmp_path, summary_grade):
    """g10 15%、g5 −6.5%：IV_opt／IV_cons≈3.8（問卷通過→moat_widening） → 非金融 P-21 由 cap_calc 自 A 降 B。"""
    x = {**CAP_INPUT, "grade": "A", "g10": 0.15, "g5": -0.065}
    b = _fb(x)
    b["certainty_grade"] = summary_grade
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa(g10=0.15, g5=-0.065), "FULL_B": b,
                          "RED_TEAM": red_team(False)})
    env.engine.run(RUN)
    return env


def test_summary_grade_that_already_contains_p21_is_not_downgraded_twice(tmp_path):
    env = _p21_case(tmp_path, "B")   # 摘要 B 只是 cap_calc 的 P-21 結果
    assert _ci(env)["grade"] == "A" and _cap(env)["grade_used"] == "B"   # D22：同一原因不重複降


def test_summary_grade_below_p21_result_lowers_input_once(tmp_path):
    env = _p21_case(tmp_path, "C")   # 看價後補判再降一級（另一驅動因素）
    assert _ci(env)["grade"] == "B" and _cap(env)["grade_used"] == "C"


# ---------------------------------------------------------------- M2_D4_03：base_g0／opt_g0 不採 FULL_B 自填

def test_base_and_opt_g0_from_full_b_are_dropped(tmp_path):
    x = {**CAP_INPUT, "base_g0": 0.25, "opt_g0": 0.02}
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa(), "FULL_B": _fb(x), "RED_TEAM": red_team(False)})
    assert env.engine.run(RUN)["status"] == "done"
    ci, cap = _ci(env), _cap(env)
    assert ci.get("base_g0") is None and ci.get("opt_g0") is None
    assert abs(cap["g0"]["base"] - min(max(CAP_INPUT["g10"], CAP_INPUT["g5"]), 0.10)) < 1e-9   # cap_calc 預設口徑
    assert abs(cap["g0"]["opt"] - min(0.15, max(CAP_INPUT["g10"], CAP_INPUT["g5"]))) < 1e-9
    assert any("cap_input.base_g0：0.25 → None" in n for n in _notes(env))


# ---------------------------------------------------------------- M2-RS3-02／M2-RS7-03：金融 iv_* 一律 null（D20）

def _fin_case(tmp_path, asset_class, **ni):
    a = _fa(**ni)
    a["asset_class"] = asset_class
    x = {**CAP_INPUT, "financial": True, "iv_cons": 70.0, "iv_base": 120.0, "iv_opt": 150.0}
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": a, "FULL_B": _fb(x), "RED_TEAM": red_team(False)})
    assert env.engine.run(RUN)["status"] == "done"
    return env


def test_bank_iv_overrides_from_full_b_are_cleared_no_auto_buy(tmp_path):
    env = _fin_case(tmp_path, "銀行")
    ci, cap = _ci(env), _cap(env)
    assert (ci.get("iv_cons"), ci.get("iv_base"), ci.get("iv_opt")) == (None, None, None)
    assert cap["IV_base"] is None and cap["auto_buy_cap"] is None
    assert not env.by_type("RED_TEAM")


def test_insurer_iv_base_override_cleared_insurance_iv_cons_kept(tmp_path):
    env = _fin_case(tmp_path, "保險", insurance={"tbv_ps": 40.0, "float_ps": 120.0, "float_cost": -0.01})
    cap = _cap(env)
    assert cap["auto_buy_cap"] is None and cap["IV_base"] is None
    assert cap["IV_cons"] is not None and cap["IV_cons"] != 70.0   # 保險式 VAL-081，不是覆寫值


# ---------------------------------------------------------------- M2-R4-01／M2-RS7-02：W-REV 由資料包營收判定

def _dossier_rev(revs):
    d = dossier_ok()
    years = list(range(2016, 2016 + len(revs)))
    md = ("# KO 資料卷宗\n## 10 年財報\n| 項目 | " + " | ".join(f"FY{y}" for y in years) + " |\n"
          + "|---" * (len(revs) + 1) + "|\n| 營收 | " + " | ".join(str(v) for v in revs) + " |\n## 其他\n")
    d["files"] = {"dossier/KO.md": md}
    return d


def _wrev_case(tmp_path, revs, model_w_rev):
    x = {**CAP_INPUT}
    if model_w_rev is not None:
        x["w_rev"] = model_w_rev
    env = _env(tmp_path, {"DOSSIER": _dossier_rev(revs), "FULL_A": _fa(), "FULL_B": _fb(x),
                          "RED_TEAM": red_team(False)})
    assert env.engine.run(RUN)["status"] == "done"
    return env


def test_w_rev_forced_true_when_3y_revenue_cagr_not_positive(tmp_path):
    # 5 年 100→104 為正，3 年 106→104 ≤0 → 任一 ≤0 即成立
    env = _wrev_case(tmp_path, [80, 85, 90, 95, 100, 102, 106, 105, 104, 104], False)
    ci, cap = _ci(env), _cap(env)
    assert ci["w_rev"] is True and cap["w_rev"] is True
    assert abs(cap["cap"] - 0.75 * cap["IV0"]) < 1e-3   # 上限只用 IV0×等級係數
    assert any("W-REV" in n for n in _notes(env))


def test_w_rev_forced_true_when_model_omits_and_5y_declines(tmp_path):
    env = _wrev_case(tmp_path, [80, 85, 90, 95, 120, 110, 100, 101, 102, 103], None)
    assert _ci(env)["w_rev"] is True


def test_w_rev_growing_revenue_model_true_kept_false_kept(tmp_path):
    rev = [80, 85, 90, 95, 100, 102, 106, 110, 114, 118]
    assert _ci(_wrev_case(tmp_path / "a", rev, True))["w_rev"] is True     # 模型從嚴可保留
    assert _ci(_wrev_case(tmp_path / "b", rev, None)).get("w_rev") is None  # 資料為正：不由框架打開


def test_w_rev_no_revenue_table_leaves_model_value(tmp_path):
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa(), "FULL_B": _fb({**CAP_INPUT, "w_rev": False}),
                          "RED_TEAM": red_team(False)})
    env.engine.run(RUN)
    assert _ci(env)["w_rev"] is False
