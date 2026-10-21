"""P20：FULL_A→cap_input 欄位契約（M2_D4_03、M3_D2-06）與 D20 deep 端（金融前置檢查、REIT 歸非金融）。FakeRunner，不連網。"""
import copy
import json
from pathlib import Path

from test_engine import (CAP_INPUT, RUN, consistent_card, dossier_ok, full_a, full_b, gate3_pass_answers, make_env,
                         red_team)
from trader.jobs import validate
from trader.paths import get_paths

SCHEMAS = validate.load_schemas(get_paths().schemas_json)


def _td(env) -> Path:
    return env.paths.work / RUN / "KO"


def _j(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _notes(env):
    return json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()["data"])["framework_notes"]


def _env(tmp_path, responses, meta=None, index=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = make_env(tmp_path, responses)
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    td = _td(env)
    if meta is not None:
        (td / "dossier" / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if index is not None:
        (td / "sec" / "_index.json").write_text(json.dumps(index), encoding="utf-8")
    return env


def _fb(cap_input):
    b = full_b("BUY")
    b["files"] = {**b["files"], "KO/cap_input.json": json.dumps(cap_input)}
    return b


def _fa(answers=None, **ni):
    a = full_a()
    a["normalized_inputs"] = {**a["normalized_inputs"], **ni}
    if answers:
        g = gate3_pass_answers()
        for q, v in answers.items():
            g["answers"][q]["answer"] = v
        a["files"] = {**a["files"], "KO/gate3_answers.json": json.dumps(g, ensure_ascii=False)}
    return a


# ---------------------------------------------------------------- 契約：FULL_A.normalized_inputs 估值欄必填（可為 null）

def test_validate_requires_cap_fields_in_phase_a_but_allows_null():
    a = full_a()
    a.pop("files", None)
    assert _errors(a) == []
    for k in ("norm_eps", "roe", "eps_cagr5", "incr_roic_gt_rd", "insurance"):
        bad = copy.deepcopy(a)
        bad["normalized_inputs"].pop(k)
        errs = _errors(bad)
        assert any(f"normalized_inputs.{k}" in e and "缺少必填鍵" in e for e in errs), (k, errs)
    ok = copy.deepcopy(a)
    ok["normalized_inputs"].update(norm_eps=None, roe=None, eps_cagr5=None, incr_roic_gt_rd=False,
                                   insurance={"tbv_ps": None, "float_ps": None, "float_cost": None})
    assert _errors(ok) == []
    ok["normalized_inputs"]["insurance"]["float_cost"] = "低"
    assert any("float_cost" in e for e in _errors(ok))


def _errors(obj):
    return [e for e in validate.check("FULL_A", obj, SCHEMAS)[1] if ".files" not in e]


# ---------------------------------------------------------------- M2_D4_03：框架以 phaseA 與問卷覆寫 cap_input

def test_moat_widening_false_when_m5_flat_even_if_model_says_true(tmp_path):
    x = {**CAP_INPUT, "moat_widening": True}
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa({"M5": "持平"}), "FULL_B": _fb(x),
                          "RED_TEAM": red_team(False)})
    assert env.engine.run(RUN)["status"] == "done"
    assert _j(_td(env) / "cap_input.json")["moat_widening"] is False
    assert any("cap_input.moat_widening：True → False" in n for n in _notes(env))


def test_moat_widening_true_only_when_m1_m3_strong_and_m5_widening(tmp_path):
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa({"M1": "強", "M2": "強", "M3": "強", "M5": "變寬"}),
                          "FULL_B": _fb(CAP_INPUT), "RED_TEAM": red_team(False)})
    env.engine.run(RUN)
    assert _j(_td(env) / "cap_input.json")["moat_widening"] is True
    env2 = _env(tmp_path / "b", {"DOSSIER": dossier_ok(), "FULL_A": _fa({"M2": "弱", "M5": "變寬"}),
                                 "FULL_B": _fb({**CAP_INPUT, "moat_widening": True}), "RED_TEAM": red_team(False)})
    env2.engine.run(RUN)
    assert _j(_td(env2) / "cap_input.json")["moat_widening"] is False


def test_valuation_fields_taken_from_phase_a_not_invented_in_full_b(tmp_path):
    x = {**CAP_INPUT, "norm_eps": 9.0, "roe": 0.5, "eps_cagr5": 0.3, "incr_roic_gt_rd": True}
    a = _fa(norm_eps=5.3, roe=0.19, eps_cagr5=None, incr_roic_gt_rd=False)
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": a, "FULL_B": _fb(x), "RED_TEAM": red_team(False)})
    assert env.engine.run(RUN)["status"] == "done"
    ci = _j(_td(env) / "cap_input.json")
    assert (ci["norm_eps"], ci["roe"], ci["eps_cagr5"], ci["incr_roic_gt_rd"]) == (5.3, 0.19, None, False)
    assert ci.get("insurance") is None and ci["financial"] is False
    assert _j(_td(env) / "cap_input_model.json")["norm_eps"] == 9.0
    assert abs(_j(_td(env) / "cap_result.json")["norm_pe"] - 60.5 / 5.3) < 1e-3  # W-PE／荒謬級線用的 E 來自前半
    assert any("框架覆寫 cap_input.norm_eps：9.0 → 5.3" in n for n in _notes(env))
    assert len(env.by_type("FULL_B")) == 1   # 框架造成的差異不退回


# ---------------------------------------------------------------- M3_D2-06／D20：金融欄位

def test_insurer_gets_financial_and_insurance_from_phase_a_and_no_auto_buy_cap(tmp_path):
    a = _fa(insurance={"tbv_ps": 40.0, "float_ps": 30.0, "float_cost": -0.01})
    a["asset_class"] = "保險"
    x = {**CAP_INPUT, "financial": False}   # 模型錯填非金融、也沒填 insurance
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": a, "FULL_B": _fb(x), "RED_TEAM": red_team(False)})
    assert env.engine.run(RUN)["status"] == "done"
    ci = _j(_td(env) / "cap_input.json")
    assert ci["financial"] is True and ci["insurance"] == {"tbv_ps": 40.0, "float_ps": 30.0, "float_cost": -0.01}
    cap = _j(_td(env) / "cap_result.json")
    assert cap["IV_base"] is None and cap["auto_buy_cap"] is None   # D20：金融 IV_base 未定義 → 不自動買
    assert cap["IV_cons"] is not None and cap["IV_cons"] > 40.0      # VAL-081 保險式（有形帳面＋浮存金價值）
    assert not env.by_type("RED_TEAM")                               # 最多 WATCH，不進紅隊


def test_partial_insurance_not_used(tmp_path):
    a = _fa(insurance={"tbv_ps": 40.0, "float_ps": None, "float_cost": 0.0})
    a["asset_class"] = "保險"
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": a, "FULL_B": _fb(CAP_INPUT), "RED_TEAM": red_team(False)})
    assert env.engine.run(RUN)["status"] == "done"
    assert _j(_td(env) / "cap_input.json").get("insurance") is None
    assert any("insurance 三欄不全" in n for n in _notes(env))


def test_reit_sic_6798_is_non_financial_in_cap_input(tmp_path):
    a = full_a()
    a["asset_class"] = "其他金融"
    env = _env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": a, "FULL_B": _fb({**CAP_INPUT, "financial": True}),
                          "RED_TEAM": red_team(False)}, meta={"sic": "6798", "industry": None})
    assert env.engine.run(RUN)["status"] == "done"
    assert _j(_td(env) / "cap_input.json")["financial"] is False
    assert any("REIT 歸非金融" in n for n in _notes(env))


# ---------------------------------------------------------------- D20：DOSSIER 後金融前置檢查

_TENK = {"file": "10-K_2026-02-20_000001.txt", "form": "10-K", "filingDate": "2026-02-20"}
_HIST = {"file": "10-K_2026-02-20_000001.txt", "form": "10-K", "filingDate": "2022-02-20", "history": True}


def _dq(missing):
    d = dossier_ok()
    d["missing"] = missing
    return d


def _stopped_step0(env):
    rec = _j(_td(env) / "decision.json")
    return not env.by_type("FULL_A") and rec["signal"] == "NO_ACTION" and rec["reason_code"] == "MISSING_DATA"


def test_insurer_without_older_10k_stops_at_step0_before_full_a(tmp_path):
    env = _env(tmp_path, {"DOSSIER": _dq([]), "FULL_A": full_a()},
               meta={"sic": "6331", "industry": "保險"}, index=[_TENK])
    assert env.engine.run(RUN)["status"] == "done"
    assert _stopped_step0(env)
    assert any("P-58" in n and "D20" in n for n in _j(_td(env) / "decision.json")["notes"])


def test_insurer_missing_float_cost_stops_but_p59_missing_continues(tmp_path):
    env = _env(tmp_path, {"DOSSIER": _dq(["P-58：浮存金成本只湊到 3 個會計年度"]), "FULL_A": full_a()},
               meta={"sic": "6331", "industry": "保險"}, index=[_TENK, _HIST])
    env.engine.run(RUN)
    assert _stopped_step0(env)
    env2 = _env(tmp_path / "b", {"DOSSIER": _dq(["P-59 綜合率剔除追溯再保後缺 2 年"]), "FULL_A": full_a(stopped=True)},
                meta={"sic": "6331", "industry": "保險"}, index=[_TENK, _HIST])
    env2.engine.run(RUN)
    assert len(env2.by_type("FULL_A")) == 1   # P-59 只是加分項：照樣續行


def test_bank_missing_p60_stops_and_non_financial_ignores_tags(tmp_path):
    env = _env(tmp_path, {"DOSSIER": _dq(["P-60：撥備前稅前盈餘缺"]), "FULL_A": full_a()},
               meta={"sic": "6022", "industry": "銀行"}, index=[_TENK])
    env.engine.run(RUN)
    assert _stopped_step0(env)
    env2 = _env(tmp_path / "b", {"DOSSIER": _dq(["P-58：無"]), "FULL_A": full_a(stopped=True)},
                meta={"sic": "2080", "industry": None}, index=[_TENK])
    env2.engine.run(RUN)
    assert len(env2.by_type("FULL_A")) == 1
