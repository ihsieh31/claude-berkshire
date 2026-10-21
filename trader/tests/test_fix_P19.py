"""P19：FULL_B 後置覆核——cap_input 由框架覆寫與驗證（M2-B3 框架端、M6-B3、M3-C）、D60（P/E 欄位）、D26（機械版業主盈餘比對）、
ASK-1 框架端（cap_discount_k 設定注入、G9 成長取高理由）。全部用 FakeRunner，不連網。"""
import json
import math
import tempfile
from pathlib import Path

import pytest

from test_engine import (ASOF, CAP_INPUT, RUN, consistent_card, dossier_ok, full_a, full_b, gate3_pass_answers, make_env,
                         red_team)
from trader.analysis import checks as C
from trader.analysis.checks import _skill_module, absurd_line, apply_cap_override, cap_input_problems, run_cap_calc
from trader.config import ConfigError, load_config


def _run(tmp_path, responses, params=None):
    env = make_env(tmp_path, responses)
    env.add(env.job("KO", "DOSSIER", 1, params=params))
    env.price_file("KO")
    return env, env.engine.run(RUN)


def _td(env):
    return env.paths.work / RUN / "KO"


def _j(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _notes(env):
    return json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()["data"])["framework_notes"]


def _fb(cap_input=None, card=None, signal="BUY"):
    b = full_b(signal)
    b["files"] = dict(b["files"])
    if cap_input is not None:
        b["files"]["KO/cap_input.json"] = json.dumps(cap_input)
    if card is not None:
        b["files"]["KO/signal_card.json"] = json.dumps(card, ensure_ascii=False)
    return b


def _card_for(x: dict, signal="BUY") -> dict:
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "c.json"
        f.write_text(json.dumps(x), encoding="utf-8")
        cap = run_cap_calc(f)
    return {"ticker": "KO", "signal": signal, "conclusion": "買", "reason_code": "PASS_ALL",
            "auto_buy_cap": cap["auto_buy_cap"], "buy_price_cap": cap["cap"],
            "value_per_share": {"conservative": cap["IV_cons"], "base": cap["IV_base"],
                                "optimistic": cap["IV_opt"], "zero_growth_IV0": cap["IV0"]}}


def _base(**kw):
    return {"oe": 5.0, "g10": 0.06, "g5": 0.07, "grade": "A", "long_rate": 0.045, "long_rate_20y_med": 0.035,
            "price": 80.0, "norm_eps": 5.5, **kw}


# ---------------------------------------------------------------- checks（M2-D8-01 (c)、M6-D8-01、M6-D8-02）

def test_iv_overrides_bounded_by_50x_oe_and_ordered():
    assert any("50×oe" in p for p in cap_input_problems(_base(financial=True, iv_cons=1e9)))
    assert any("iv_cons ≤ iv_base ≤ iv_opt" in p for p in
               cap_input_problems(_base(financial=True, iv_cons=90, iv_base=80, iv_opt=100)))
    assert any("必須是正數" in p for p in cap_input_problems(_base(financial=True, iv_cons=-1)))
    assert cap_input_problems(_base(financial=True, iv_cons=60, iv_base=80, iv_opt=100)) == []


def test_norm_eps_unit_errors_rejected():
    assert any("norm_eps/oe" in p for p in cap_input_problems(_base(norm_eps=0.2)))   # 荒謬級線會立刻觸發
    assert any("norm_eps=" in p and "正數" in p for p in cap_input_problems(_base(norm_eps=-1.0)))
    assert any("price/norm_eps" in p for p in cap_input_problems(_base(norm_eps=500.0, oe=5.0)))
    assert cap_input_problems(_base(norm_eps=30.0, oe=5.0, price=150.0)) == []   # 重資產 OE 遠低於 EPS：不擋


def test_absurd_line_never_nan():
    assert absurd_line({"oe": 5.0, "norm_eps": float("nan")}) is None
    assert absurd_line({"oe": float("inf")}) is None


def test_apply_cap_override_script_fields_even_when_consistent():
    """M2_D9_06：上限與 IV 一致，但 implied_growth／price_to_cap 等模型值錯 → 仍以腳本覆寫；只記有變動者。"""
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "c.json"
        f.write_text(json.dumps(CAP_INPUT), encoding="utf-8")
        cap = run_cap_calc(f)
    card = {**consistent_card("BUY"), "implied_growth": 0.01, "price_to_cap": 0.1}
    out = apply_cap_override(card, cap)
    assert out["implied_growth"] == cap["g_impl"]
    assert out["price_to_cap"] == cap["price_to_auto_buy_cap"]
    assert out["grade_used"] == cap["grade_used"] and out["conclusion_test_flips"] is False
    assert "IV0_cap" in out["cap_components"]
    notes = out["framework_notes"]
    assert any("implied_growth" in n for n in notes)
    assert not any("value_per_share.base" in n for n in notes)   # 沒變的不記


# ---------------------------------------------------------------- D26：機械版業主盈餘

def _table_md(ni, dna, capex, rev, shares, years=range(2016, 2026)):
    rows = [("營收", rev), ("淨利（歸屬母公司）", ni), ("稀釋加權股數（百萬股）", shares), ("折舊攤銷", dna),
            ("資本支出", capex)]
    head = "| | " + " | ".join(f"FY{y}（{y}-12-31）" for y in years) + " |"
    sep = "|:--|" + "--:|" * len(list(years))
    body = ["| " + n + " | " + " | ".join(f"{v:,.1f}" for v in vals) + " |" for n, vals in rows]
    return "\n".join(["# 資料包：KO", "", "## 10 年財報（百萬美元；EPS 每股美元；股數百萬股）", "", head, sep, *body, "",
                      "## 衍生比率（由上表計算）", ""])


def test_mechanical_oe_heavy_asset_k_and_shock_exclusion():
    dq = _skill_module("deep_quant")
    n = 10
    md = _table_md(ni=[100.0] * 9 + [300.0], dna=[50.0] * n, capex=[100.0] * n, rev=[1000.0] * n, shares=[10.0] * n)
    m = dq.mechanical_oe(dq.dossier_table(md), shock_years=[2025])
    assert m["K"] == pytest.approx(1.6)   # capex/rev 10% ≥8%、capex/dna 2.0 → max(1.2, 1.6)，上限 1.6
    assert m["excluded_shock_years"] == [2025] and m["years_used"] == [2021, 2022, 2023, 2024]
    assert m["E"] == pytest.approx((100 + 50 - 1.6 * 50) / 10)   # 衝擊年 2025 的 300 不進中位
    assert m["g10"] == pytest.approx(((300 - 30) / (100 - 30)) ** (1 / 9) - 1)
    light = dq.mechanical_oe(dq.dossier_table(_table_md([100.0] * n, [50.0] * n, [40.0] * n, [10000.0] * n,
                                                        [10.0] * n)))
    assert light["K"] == 1.0 and light["E"] == pytest.approx(10.0) and light["g5"] == pytest.approx(0.0)
    assert dq.mechanical_oe(dq.dossier_table("# 沒有表\n")) is None


def test_oe_check_flags_e_g_and_shock_year_differences():
    md = _table_md([100.0] * 10, [50.0] * 10, [40.0] * 10, [10000.0] * 10, [10.0] * 10)   # E=10、g=0
    ok = C.oe_check(md, [], {"oe_per_share": 10.5, "g10": 0.005, "g5": None, "excluded_shock_years": []})
    assert ok["diffs"] == []   # E 差 5%、g 差 <1pp
    bad = C.oe_check(md, [2020], {"oe_per_share": 12.0, "g10": 0.05, "excluded_shock_years": []})
    assert any(d.startswith("E：") for d in bad["diffs"]) and any(d.startswith("g10：") for d in bad["diffs"])
    assert any("剔除年" in d for d in bad["diffs"])
    assert C.oe_check(md, None, {"oe_per_share": 10.0, "excluded_shock_years": [2019]})["diffs"] == []   # 缺同業不比剔除年
    assert C.oe_check("# 無表", [], {"oe_per_share": 1.0}) is None


def _dossier_with_table():
    d = dossier_ok()
    d["files"] = {"dossier/KO.md": _table_md([400.0] * 10, [50.0] * 10, [40.0] * 10, [10000.0 * 1.05 ** i for i in range(10)], [100.0] * 10)
                  + "\n## 年報文字節錄\n業務。\n"}
    return d


def test_d26_mismatch_redoes_full_a_once_then_caps_buy_at_watch(tmp_path):
    """機械版 E＝4.0、g＝0；FULL_A 寫 E＝5.0、g＝7%／6% → 退回 FULL_A 一次（提示含差異）→ 仍不同 → BUY 最多 WATCH，日誌有對照。"""
    env, res = _run(tmp_path, {"DOSSIER": _dossier_with_table(), "FULL_A": full_a(), "FULL_B": full_b("BUY"),
                               "RED_TEAM": red_team(False)})
    assert res["status"] == "done"
    fa = env.by_type("FULL_A")
    assert len(fa) == 2 and json.loads(fa[1]["params"])["oe_retry"] == 1
    assert "機械版業主盈餘" in [c for c in env.fake.calls if c.job_type == "FULL_A"][1].prompt_text
    chk = _j(_td(env) / "oe_check.json")
    assert chk["retried"] is True and chk["mechanical"]["E"] == pytest.approx(4.0)
    assert env.by_type("RED_TEAM") == []
    dec = env.conn.execute("SELECT signal FROM decisions").fetchone()
    assert dec["signal"] == "WATCH"
    notes = _notes(env)
    assert any(n.startswith("D26 機械版業主盈餘對照") for n in notes)
    assert any("BUY → WATCH" in n and "D26" in n for n in notes)


def test_d26_match_keeps_buy(tmp_path):
    a = full_a()
    a["normalized_inputs"] = {**a["normalized_inputs"], "oe_per_share": 4.0, "g10": 0.0, "g5": 0.0}
    x = {**CAP_INPUT, "oe": 4.0, "g10": 0.0, "g5": 0.0, "norm_eps": 4.0}
    fb = _fb(cap_input=x, card=_card_for({**x, "price": 60.5}))
    env, res = _run(tmp_path, {"DOSSIER": _dossier_with_table(), "FULL_A": a, "FULL_B": fb, "RED_TEAM": red_team(False)})
    assert res["status"] == "done" and len(env.by_type("FULL_A")) == 1
    assert _j(_td(env) / "oe_check.json")["diffs"] == []
    assert not any("D26 與機械版不同" in n for n in _notes(env))


# ---------------------------------------------------------------- 框架覆寫 cap_input（M3_D6-05、M6-D8-05、D60、M2-D8-01 (a)、ASK-1）

def _fa_flat():
    """P20：問卷 M5＝持平 → 框架設 moat_widening=false，框架修正後的輸入與 CAP_INPUT（訊號卡所據）同口徑。"""
    a = full_a()
    g = gate3_pass_answers()
    g["answers"]["M5"]["answer"] = "持平"
    a["files"] = {**a["files"], "KO/gate3_answers.json": json.dumps(g, ensure_ascii=False)}
    return a


def test_price_rates_pe_financial_and_k_overridden_by_framework(tmp_path):
    lp = {"pe_norm_median": 26.97, "pe_ttm_median": 26.22, "fin_pe_norm_median": 18.11}
    x = {**CAP_INPUT, "financial": True, "iv_cons": 50.0, "largecap_pe_median": 99.0}
    env = make_env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa_flat(), "FULL_B": _fb(cap_input=x),
                              "RED_TEAM": red_team(False)})
    env.add(env.job("KO", "DOSSIER", 1, params={"largecap_pe": lp}))
    pf = env.price_file("KO")
    pf.write_text(json.dumps({"price": 61.0, "price_date": "2026-10-15", "t30": 0.046, "t30_20y_median": 0.036}),
                  encoding="utf-8")
    assert env.engine.run(RUN)["status"] == "done"
    ci = _j(_td(env) / "cap_input.json")
    assert (ci["price"], ci["long_rate"], ci["long_rate_20y_med"]) == (61.0, 0.046, 0.036)
    assert ci["largecap_pe_median"] == 26.22 and ci["fin_pe_median"] == 18.11   # D60：母體＝最近年報口徑
    assert ci["pe_field"]["largecap_pe_median"] == "pe_ttm_median" and ci["pe_field"]["company"] == "norm_eps"
    assert ci["financial"] is False   # phaseA.asset_class＝非金融 → 模型標的 financial=true 被改回
    assert ci["cap_discount_k"] == {"A": 0.95, "B": 0.85}   # 設定注入（寫回 state/cap_inputs 供 PRICE、holdings_view）
    assert _j(_td(env) / "cap_input_model.json")["price"] == 60.0   # 模型原檔保留
    cap = _j(_td(env) / "cap_result.json")
    assert cap["price"] == 61.0 and cap["IV_base"] is not None   # iv_cons 覆寫不被採用（非金融）
    assert _j(env.paths.state / "cap_inputs" / "KO.json")["cap_discount_k"] == {"A": 0.95, "B": 0.85}
    notes = _notes(env)
    assert any("框架覆寫 cap_input.price：60.0 → 61.0" in n for n in notes)
    assert any("框架覆寫 cap_input.financial" in n for n in notes)
    assert len(env.by_type("FULL_B")) == 1   # 框架造成的差異不退回


def test_no_pe_file_sets_p4_inputs_null_and_insurer_is_financial(tmp_path):
    a = full_a()
    a["asset_class"] = "保險"
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": a, "FULL_B": full_b("BUY"),
                               "RED_TEAM": red_team(False)})
    ci = _j(_td(env) / "cap_input.json")
    assert ci["largecap_pe_median"] is None and ci.get("fin_pe_median") is None and ci["financial"] is True


def test_config_cap_discount_k_validated_and_injected(tmp_path):
    p = tmp_path / "c.yaml"
    base = ("models:\n  strong: {backend: fake, model: f}\n  medium: {backend: fake, model: f}\n"
            "  weak: {backend: fake, model: f}\n")
    p.write_text(base + "valuation: {cap_discount_k: {A: 0.9, B: 0.8}}\n", encoding="utf-8")
    assert load_config(p, env_file=tmp_path / "x.env").valuation.cap_discount_k == {"A": 0.9, "B": 0.8}
    p.write_text(base + "valuation: {cap_discount_k: {A: 1.5}}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p, env_file=tmp_path / "x.env")


# ---------------------------------------------------------------- M3_D6-01：cap_input 須沿用 phaseA

def test_cap_input_differs_from_phase_a_redo_then_phase_a_wins(tmp_path):
    x = {**CAP_INPUT, "oe": 6.0}   # phaseA oe_per_share＝5.0
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _fa_flat(), "FULL_B": _fb(cap_input=x),
                               "RED_TEAM": red_team(False)})
    assert res["status"] == "done"
    fb = env.by_type("FULL_B")
    assert len(fb) == 2 and json.loads(fb[1]["params"])["input_retry"] == 1
    assert "phaseA" in [c for c in env.fake.calls if c.job_type == "FULL_B"][1].prompt_text
    assert _j(_td(env) / "cap_input.json")["oe"] == 5.0
    assert any("以價格盲的 phaseA 值覆寫" in n for n in _notes(env))


# ---------------------------------------------------------------- M3_D7-02：訊號卡數值欄正規化

def test_card_percent_strings_normalized_without_redo(tmp_path):
    card = {**consistent_card("BUY"), "target_weight_pct": "10%", "max_weight_pct": {"tail": "12.5", "grade": 10},
            "position_plan": {"target_weight_pct": "10 %", "first_tranche_pct_of_target": 50, "add_requires": []}}
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": full_a(), "FULL_B": _fb(card=card),
                               "RED_TEAM": red_team(False)})
    assert res["status"] == "done" and len(env.by_type("FULL_B")) == 1
    sc = json.loads(env.conn.execute("SELECT data FROM signals").fetchone()[0])
    assert sc["max_weight_pct"]["tail"] == 12.5   # target_weight_pct 本身由回寫依 intended_weight 重算（D13）
    assert any("position_plan.target_weight_pct='10 %' 轉為數字 10.0" in n for n in _notes(env))
    assert any("target_weight_pct='10%' 轉為數字 10.0" in n for n in _notes(env))


def test_card_non_numeric_redo_once_then_null(tmp_path):
    card = {**consistent_card("BUY"), "max_weight_pct": {"single_event": "N/A"}}
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": full_a(), "FULL_B": _fb(card=card),
                               "RED_TEAM": red_team(False)})
    assert res["status"] == "done"
    fb = env.by_type("FULL_B")
    assert len(fb) == 2 and "single_event" in [c for c in env.fake.calls if c.job_type == "FULL_B"][1].prompt_text
    sc = json.loads(env.conn.execute("SELECT data FROM signals").fetchone()[0])
    assert sc["max_weight_pct"]["single_event"] is None
    assert any("single_event" in n and "設為 null" in n for n in _notes(env))


# ---------------------------------------------------------------- ASK-1：G9 成長取高理由

def _g9_fb(reason):
    x = {**CAP_INPUT, "g10": 0.12, "g5": 0.05}
    card = _card_for({**x, "price": 60.5})
    if reason:
        card["g9_growth_reason"] = reason
    return x, _fb(cap_input=x, card=card)


def _g9_phase_a():
    a = full_a()
    a["normalized_inputs"] = {**a["normalized_inputs"], "g10": 0.12, "g5": 0.05}
    return a


def test_g9_reason_missing_redo_then_watch(tmp_path):
    _, fb = _g9_fb(None)
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _g9_phase_a(), "FULL_B": fb,
                               "RED_TEAM": red_team(False)})
    assert res["status"] == "done" and len(env.by_type("FULL_B")) == 2
    assert "g9_growth_reason" in [c for c in env.fake.calls if c.job_type == "FULL_B"][1].prompt_text
    assert env.by_type("RED_TEAM") == []
    assert env.conn.execute("SELECT signal FROM decisions").fetchone()["signal"] == "WATCH"
    assert json.loads(env.conn.execute("SELECT data FROM signals").fetchone()[0])["stopped_at_gate"] == "G9"


def test_g9_reason_given_keeps_buy(tmp_path):
    _, fb = _g9_fb("近 5 年受一次性重組拖累，10 年成長較能代表")
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _g9_phase_a(), "FULL_B": fb,
                               "RED_TEAM": red_team(False)})
    assert res["status"] == "done" and len(env.by_type("FULL_B")) == 1
    assert env.by_type("RED_TEAM")


# ---------------------------------------------------------------- M3_D4-03：紅隊違規

def test_red_team_rule_violation_sends_back_once(tmp_path):
    rt = {**red_team(False), "rule_violations": ["前半用到價格"], "verdict": "維持"}
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": full_a(), "FULL_B": full_b("BUY"), "RED_TEAM": rt})
    assert res["status"] == "done"
    fb = env.by_type("FULL_B")
    assert len(fb) == 2 and json.loads(fb[1]["params"])["redteam_round"] == 1
    assert env.conn.execute("SELECT signal FROM decisions").fetchone()["signal"] == "WATCH"
