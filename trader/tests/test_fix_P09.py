"""P09（M3-A 路由契約與驗證器）的回歸測試。

每個測試對應一條稽核條目（見 hark/audit/fixes/P09.md）。
"""
from __future__ import annotations

import copy
import json

from trader.jobs import validate
from trader.jobs.context import build_prompt, render_template
from trader.jobs.hold_jobs import check as hold_check
from trader.models import JobSpec
from trader.paths import PKG_ROOT

SCHEMAS = validate.load_schemas(PKG_ROOT / "routing" / "output_schemas.json")
PROMPTS = PKG_ROOT / "routing" / "prompts"
R10 = [f"R{i}" for i in range(1, 11)]


def _full_a(**kw) -> dict:
    d = {
        "ticker": "KO", "decision_date": "2026-10-15", "stopped": False,
        "stopped_at_gate": None, "reason_code": None, "asset_class": "非金融", "starting_grade": "A",
        "downgrades": [],
        "moat_tests": {"pricing_power": "強", "attack_copy": "強", "price_cut": "中"},
        "moat_evidence": {"pricing_power": "足夠", "attack_copy": "足夠", "price_cut": "足夠"},
        "red_flags": {"exclusion": 0, "warning": 0}, "capital_allocation_net": 1,
        "tail_loss_after_tax_pct": 20.0,
        "normalized_inputs": {"oe_per_share": 2.5, "g10": 0.05, "g5": 0.04,
                              "excluded_shock_years": [], "roe_std_ex_shock_pp": 3.0,
                              "norm_eps": None, "roe": None, "eps_cagr5": None, "incr_roic_gt_rd": False,   # P20
                              "insurance": {"tbv_ps": None, "float_ps": None, "float_cost": None}},
        "thesis_draft": {"one_liner": "x", "key_assumptions": ["A", "B", "C"],
                         "kill_criteria": [{"text": "k1", "due": "2027-06-30"},
                                           {"text": "k2", "due": "2027-12-31"}]},
        "missing": [], "inferred_thresholds_used": [], "files": ["report_A.md", "gate3_answers.json"],
        "rule_ids": ["SEL-061"],   # RM2-9：停止時必填
    }
    d.update(kw)
    return d


def _thesis(items=R10) -> dict:
    return {"ticker": "KO", "verdict": "完好",
            "kill_criteria_status": [{"criterion": "k1", "status": "未觸發", "evidence": "10-Q"}],
            "r_list_status": [{"item": i, "status": "無變化", "evidence": "e"} for i in items],
            "new_facts": [], "reason": "r"}


def _hold_review(**kw) -> dict:
    d = {"ticker": "KO", "decision_date": "2026-10-15", "path": [], "action": "SELL_ALL",
         "sell_category": "業務", "trim_fraction": None, "stop_adding": True, "observe_until": None,
         "falsified_assumption": None, "external_drivers": [], "external_drivers_change_reason": None,
         "reason": "r"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- M3_D1-05（P0）

def test_D1_05_dossier_sections_appended_is_free_text():
    """產業補充序列與模型自取的節名不得讓 DOSSIER 兩次不合格 → MISSING_DATA。"""
    d = {"ticker": "HON", "decision_date": "2026-10-15",
         "sections_appended": ["產業補充序列", "會計師報告", "委託書", "重大8-K", "最近一季10-Q", "業務"],
         "dq_checks": [], "missing": []}
    _, errors = validate.check("DOSSIER", d, SCHEMAS)
    assert errors == []


# ---------------------------------------------------------------- M3_D6-07／M3_D9-03

def test_D6_07_dossier_contains_price_removed_and_scrub_keeps_dossier_keys():
    from trader.jobs.sandbox import scrub_prices
    assert "contains_price" not in SCHEMAS["DOSSIER"]
    scrubbed = scrub_prices(copy.deepcopy(SCHEMAS))
    # 價格隔離沙箱裡的 schema 副本與驗證器的 DOSSIER 必填鍵一致
    assert list(scrubbed["DOSSIER"]) == list(SCHEMAS["DOSSIER"])


def test_D9_03_common_points_to_prompt_output_section_not_schema_copy():
    common = (PROMPTS / "_COMMON.md").read_text(encoding="utf-8")
    assert "routing/output_schemas.json" not in common
    assert "輸出格式" in common


# ---------------------------------------------------------------- M3_D1-03

def test_D1_03_string_null_normalized_for_nullable_rules():
    hr = _hold_review(falsified_assumption="null", external_drivers_change_reason=" None ",
                      observe_until="null", trim_fraction="null")
    out, errors = validate.check("HOLD_REVIEW", hr, SCHEMAS)
    assert errors == []
    assert out["falsified_assumption"] is None
    assert out["external_drivers_change_reason"] is None
    assert out["observe_until"] is None
    assert out["trim_fraction"] is None
    # 列舉允許 null 時，"None"／"NULL" 也視同 null
    out, errors = validate.check("HOLD_REVIEW", _hold_review(action="HOLD", sell_category="None"), SCHEMAS)
    assert errors == [] and out["sell_category"] is None


def test_D1_03_hold_check_placeholder_is_not_named():
    ok = dict(iv_base=50.0, price=60.0, held_days=1000)
    for placeholder in ("null", "NULL", "無", "缺", " none "):
        errs, _ = hold_check(_hold_review(falsified_assumption=placeholder), **ok)
        assert errs, placeholder
    assert hold_check(_hold_review(falsified_assumption="假設 A 被 10-Q 否定"), **ok) == ([], False)


def test_D1_03_business_sell_all_with_string_null_rejected_end_to_end():
    out, errors = validate.check("HOLD_REVIEW", _hold_review(falsified_assumption="null"), SCHEMAS)
    assert errors == []
    errs, _ = hold_check(out, iv_base=None, price=None, held_days=1000)
    assert any("具名" in e for e in errs)


# ---------------------------------------------------------------- M3_D1-04

def test_D1_04_full_a_stopped_consistency():
    _, e = validate.check("FULL_A", _full_a(stopped=False, reason_code="REJECT"), SCHEMAS)
    assert e
    _, e = validate.check("FULL_A", _full_a(stopped=False, stopped_at_gate="G3"), SCHEMAS)
    assert e
    _, e = validate.check("FULL_A", _full_a(stopped=True, stopped_at_gate="G3", reason_code=None), SCHEMAS)
    assert e
    _, e = validate.check("FULL_A", _full_a(stopped="true", stopped_at_gate="G3", reason_code="REJECT"), SCHEMAS)
    assert e == []
    _, e = validate.check("FULL_A", _full_a(), SCHEMAS)
    assert e == []


# ---------------------------------------------------------------- M3_D4-04（validate 部分）

def test_D4_04_thesis_r_list_must_be_r1_to_r10_once_each():
    assert validate.check("THESIS", _thesis(), SCHEMAS)[1] == []
    assert validate.check("THESIS", _thesis(R10[:9]), SCHEMAS)[1]                 # 只 9 列
    assert validate.check("THESIS", _thesis(R10[:9] + ["R1"]), SCHEMAS)[1]        # 重複
    assert validate.check("THESIS", _thesis(list(reversed(R10))), SCHEMAS)[1] == []   # 順序不拘


def test_D4_04_full_a_not_stopped_counts():
    bad = _full_a()
    bad["thesis_draft"]["key_assumptions"] = ["A"]
    assert validate.check("FULL_A", bad, SCHEMAS)[1]
    bad = _full_a()
    bad["thesis_draft"]["kill_criteria"] = bad["thesis_draft"]["kill_criteria"][:1]
    assert validate.check("FULL_A", bad, SCHEMAS)[1]
    # 停止時不要求論點草稿
    stopped = _full_a(stopped=True, stopped_at_gate="G1", reason_code="TOO_HARD",
                      thesis_draft={"one_liner": "", "key_assumptions": [], "kill_criteria": []})
    assert validate.check("FULL_A", stopped, SCHEMAS)[1] == []


# ---------------------------------------------------------------- M3-D8-04

def test_D8_04_nan_and_infinity_rejected():
    c = json.loads('{"ticker":"X","action_type":"拆股","effective_date":"2026-10-15","pay_date":null,'
                   '"ratio_new_per_old":NaN,"cash_per_share":Infinity,"fractional_handling":"不明",'
                   '"new_ticker":null,"needs_human":false,"evidence":"e","reason":"r"}')
    _, errors = validate.check("CORP_ACTION", c, SCHEMAS)
    assert len(errors) == 2 and all("NaN" in e for e in errors)
    fa = _full_a(tail_loss_after_tax_pct=float("-inf"))
    assert validate.check("FULL_A", fa, SCHEMAS)[1]


# ---------------------------------------------------------------- M3_D9-04

def test_D9_04_error_feedback_forbids_changing_substance():
    msg = validate.error_feedback(["FULL_A.red_flags.warning：應為數字或 null"])
    assert "不得改變" in msg and "不得填 0" in msg


# ---------------------------------------------------------------- M3_D4-07

def test_D4_07_framework_owned_fields_are_lenient():
    from test_jobs_io import GOOD
    fb = copy.deepcopy(GOOD["FULL_B"])
    fb["decision_id"] = "X"
    assert validate.check("FULL_B", fb, SCHEMAS)[1] == []
    fb["decision_id"] = None
    assert validate.check("FULL_B", fb, SCHEMAS)[1] == []
    pr = copy.deepcopy(GOOD["PRICE"])
    pr["price"] = None
    pr["gates"] = {"gate1_screen": None, "gate2_price_lt_cap": "通過（價格 < 上限）", "gate3_on_file": None}
    assert validate.check("PRICE", pr, SCHEMAS)[1] == []


# ---------------------------------------------------------------- M3_D1-06／M3-D5-01

def test_D1_06_thesis_prompt_renders_kill_criteria():
    t = (PROMPTS / "THESIS.md").read_text(encoding="utf-8")
    out = render_template(t, {"kill_criteria": "毛利率連兩季 <30%（期限 2027-06-30）"})
    assert "毛利率連兩季 <30%" in out and "{kill_criteria}" not in out


def test_D5_01_thesis_prompt_scopes_r4_r6_r10():
    t = (PROMPTS / "THESIS.md").read_text(encoding="utf-8")
    assert "R4、R6、R10" in t and "不得因此判疑似" in t


# ---------------------------------------------------------------- M3-D5-02／D5-04／D7-08／D2-10

def test_price_prompt_contract():
    t = (PROMPTS / "PRICE.md").read_text(encoding="utf-8")
    assert "陣列" not in t and "單一 JSON 物件" in t                              # D7-08
    # D5-02；F3-GLUE（D90）：cap_calc 不再輸出 conclusion_test，PRICE.md 的 flips 條文已刪 → 改斷言閘門 2 寫明 0.95 已併入上限
    assert "新的 notes 警示" not in t and "grade_used" in t and "flips" not in t and "D90" in t
    assert "無上限（不得買）" in t                                                # D5-04
    assert "共通規則第 1 條的例外" in t                                           # D2-10


def test_D2_10_price_exception_in_full_b_and_hold_review():
    for name in ("FULL_B.md", "HOLD_REVIEW.md"):
        assert "共通規則第 1 條的例外" in (PROMPTS / name).read_text(encoding="utf-8"), name


# ---------------------------------------------------------------- M3_D2-09

def test_D2_09_first_tranche_not_model_owned():
    fb = SCHEMAS["FULL_B"]
    assert "不採用" in fb["_first_tranche_weight"] and "0.10" in fb["_target_weight"]   # A3-M3B-03 改選填說明鍵
    t = (PROMPTS / "FULL_B.md").read_text(encoding="utf-8")
    assert "first_tranche_pct_of_target" in t and "50" in t and "70%" in t


# ---------------------------------------------------------------- M3_D1-09／M3-D10-05

def test_D1_09_dossier_output_limit_matches_prompt():
    r = json.loads((PKG_ROOT / "routing" / "model_routing.json").read_text(encoding="utf-8"))
    assert "40k" in r["jobs"]["DOSSIER"]["output_limit"]
    assert "40k" in (PROMPTS / "DOSSIER.md").read_text(encoding="utf-8")


def test_D10_05_routing_docs_match_framework():
    r = json.loads((PKG_ROOT / "routing" / "model_routing.json").read_text(encoding="utf-8"))
    sw = json.dumps(r["state_writes"], ensure_ascii=False)
    assert "registry.json" not in sw and "decision_log.jsonl" not in sw and "SQLite" in sw
    assert "price_file_opened_at.txt" in r["jobs"]["FULL_B"]["pre_check"]
    readme = (PKG_ROOT / "routing" / "README.md").read_text(encoding="utf-8")
    assert "price_file_opened_at.txt" in readme


# ---------------------------------------------------------------- M3-D10-04（＋DUPLICATE M3_D1-07）

def test_D10_04_corp_action_prompt_has_no_project_root(tmp_path):
    from trader.jobs.corp_jobs import CorpActionHandler

    class _Ctx:
        conn = None

    job = JobSpec(job_id="r:X:CORP_ACTION:1", run_id="r", job_type="CORP_ACTION", ticker="X",
                  asof="2026-10-15", params={})
    import trader.jobs.corp_jobs as cj
    orig = (cj.ca.position_of, cj.ca.open_orders_of, cj._registered)
    cj.ca.position_of = lambda conn, t: None
    cj.ca.open_orders_of = lambda conn, t: []
    cj._registered = lambda conn, t, d: "無"
    try:
        vals = CorpActionHandler().prompt_values(job, _Ctx(), "claude_code")
    finally:
        cj.ca.position_of, cj.ca.open_orders_of, cj._registered = orig
    text = build_prompt(job, "openai_compat", 10**9, PKG_ROOT, sections=[], values=vals)
    assert str(PKG_ROOT) not in text
    common = (PROMPTS / "_COMMON.md").read_text(encoding="utf-8")
    assert "沒有提供" in common
