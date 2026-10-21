"""2026-10-18 Hark 修正 第 4、5 項：護城河三測一致性（schema、validate、writeback、gate3 問卷）；未評估欄位用 null。"""
import json

import pytest

from trader.analysis import writeback as W
from trader.jobs.validate import check, load_schemas
from trader.paths import get_paths
from trader.portfolio import sizing as S

PATHS = get_paths()
SCHEMAS = load_schemas(PATHS.schemas_json)
Q = json.loads((PATHS.skill_deep / "assets" / "gate3_questionnaire.json").read_text(encoding="utf-8"))


def _full_a(**kw):
    d = {"ticker": "KO", "decision_date": "2026-10-15", "stopped": True, "stopped_at_gate": "G1",
         "reason_code": "TOO_HARD", "asset_class": "非金融", "starting_grade": None, "downgrades": [],
         "moat_tests": {"pricing_power": None, "attack_copy": None, "price_cut": None},
         "moat_evidence": {"pricing_power": None, "attack_copy": None, "price_cut": None},
         "red_flags": {"exclusion": None, "warning": None}, "capital_allocation_net": None,
         "tail_loss_after_tax_pct": None,
         "normalized_inputs": {"oe_per_share": None, "g10": None, "g5": None, "excluded_shock_years": [],
                               "roe_std_ex_shock_pp": None, "norm_eps": None, "roe": None, "eps_cagr5": None,
                               "incr_roic_gt_rd": False, "insurance": {"tbv_ps": None, "float_ps": None, "float_cost": None}},   # P20
         "thesis_draft": {"one_liner": "", "key_assumptions": [], "kill_criteria": []},
         "missing": [], "inferred_thresholds_used": [], "files": [],
         "rule_ids": ["SEL-061"]}   # RM2-9：停止時必填
    d.update(kw)
    return d


# ---------------------------------------------------------------- 第 4 項：三測一致

def test_moat_three_tests_consistent_across_schema_writeback_and_gate3():
    fa = SCHEMAS["FULL_A"]
    assert tuple(fa["moat_tests"]) == W._MOAT_KEYS == tuple(fa["moat_evidence"])
    assert len(W._MOAT_KEYS) == 3 and "price_cut" in W._MOAT_KEYS
    ids = Q["pass_rule"]["min_strong_among"]["ids"]
    assert ids == ["M1", "M2", "M3"] and Q["pass_rule"]["min_strong_among"]["min"] == 2
    texts = {q["id"]: q["text"] for q in Q["questions"]}
    # 問卷三題與 schema 三鍵一一對應：定價權、攻擊／複製、砍價
    assert "定價權" in texts["M1"] and "攻擊" in texts["M2"] and "砍價" in texts["M3"]


def test_full_a_missing_price_cut_is_rejected_by_validate():
    bad = _full_a(moat_tests={"pricing_power": "強", "attack_copy": "強"})
    _, errors = check("FULL_A", bad, SCHEMAS)
    assert any("moat_tests.price_cut" in e and "缺少必填鍵" in e for e in errors)


def test_reject_basis_counts_all_three_tests():
    pa = {"reason_code": "REJECT", "stopped_at_gate": "G3",
          "moat_tests": {"pricing_power": "弱", "attack_copy": "弱", "price_cut": "強"},
          "moat_evidence": {"pricing_power": "缺資料", "attack_copy": "足夠", "price_cut": "足夠"}}
    assert W.reject_basis(pa) == "證據不足"          # 強 1（price_cut）＋缺資料 1 → ≥2
    pa["moat_tests"]["price_cut"] = "弱"
    assert W.reject_basis(pa) == "有反證"


# ---------------------------------------------------------------- 第 5 項：未評估 → null

def test_full_a_accepts_null_for_unevaluated_fields():
    out, errors = check("FULL_A", _full_a(), SCHEMAS)
    assert errors == []
    assert out["red_flags"]["exclusion"] is None and out["capital_allocation_net"] is None
    assert out["moat_tests"]["price_cut"] is None
    # 已評且為零仍可填 0
    _, errors = check("FULL_A", _full_a(red_flags={"exclusion": 0, "warning": 0}, capital_allocation_net=0), SCHEMAS)
    assert errors == []
    # 亂填字串仍被擋
    _, errors = check("FULL_A", _full_a(capital_allocation_net="未評"), SCHEMAS)
    assert errors


def test_full_b_accepts_null_target_weight():
    fb = {"ticker": "KO", "decision_id": "KO-20261015-1", "signal": "NO_ACTION", "reason_code": "MISSING_DATA",
          "stopped_at_gate": "G7", "certainty_grade": None, "auto_buy_cap": None, "target_weight": None,
          "first_tranche_weight": None, "intended_weight": {"level": None, "reason": None},
          "external_drivers": [], "external_drivers_change_reason": None,
          "g8": {"self_adjusted": None, "best_holding": None, "best_holding_adjusted": None,
                 "weakest_holding_adjusted": None, "best_watch": None, "best_watch_adjusted": None,
                 "treasury": None, "market": None, "winner": "未比較"}, "files": []}
    _, errors = check("FULL_B", fb, SCHEMAS)
    assert errors == []


def test_reject_basis_and_sizing_handle_nulls():
    pa = _full_a(stopped_at_gate="G3", reason_code="REJECT")
    assert W.reject_basis(pa) is None                         # moat_evidence 全 null（未走到 G3）→ 無依據
    t, notes = S.target_weight({"certainty_grade": "B", "target_weight_pct": None,
                                "max_weight_pct": {"tail": None, "correlated_group": None}}, None)
    assert t == 0.0 and "缺目標權重" in notes[0]


def test_signal_card_schema_uses_null_for_unevaluated_counts():
    sc = json.loads((PATHS.skill_deep / "assets" / "signal_card_schema.json").read_text(encoding="utf-8"))
    assert sc["bias_count"]["pro_trade"] is None and sc["bias_count"]["pro_sell_or_wait"] is None
    assert sc["target_weight_pct"] is None and sc["external_drivers"] is None


def test_write_stop_result_leaves_unevaluated_as_null(state_dir):
    from trader import db
    paths = get_paths()
    paths.state.mkdir(parents=True, exist_ok=True)
    conn = db.connect(paths.state / "t.db")
    db.migrate(conn)
    did = W.write_stop_result(conn, paths, "KO", "2026-10-15", "r1", _full_a())
    card = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()["data"])
    assert card["bias_count"] is None and card["target_weight_pct"] is None and card["external_drivers"] is None
    conn.close()


def test_mask_buyback_table_with_zero_width_cells():
    from trader.analysis.dossier_prep import mask_buyback_shares, BUYBACK_MASK
    txt = "\n".join(["Shares repurchased", "\u200b", "", "\u200b", "1,439", "\u200b", "\u200b", "1,231",
                     "Total cost of shares repurchased, including excise tax", "\u200b", "$", "798,633"])
    out, n = mask_buyback_shares(txt)
    assert "1,439" not in out and "1,231" not in out and n >= 2
    assert BUYBACK_MASK in out


def test_price_cut_not_scrubbed_from_schema():
    from trader.jobs.sandbox import scrub_prices, is_price_key
    assert not is_price_key("price_cut") and is_price_key("price") and is_price_key("avg_buyback_price_by_fy")
    out = scrub_prices({"moat_tests": {"pricing_power": "x", "price_cut": "y"}, "price": 1})
    assert out == {"moat_tests": {"pricing_power": "x", "price_cut": "y"}}
