"""RM2-9（M2-D5-09 複審 PARTIAL；canonical M6-D5-01／P45）：停止卡的規則編號與一句話理由。

真實流程：FULL_A 輸出（照 output_schemas.json 契約）→ validate.check → FullAHandler.post → write_stop_result。\n修前：契約沒有 rule_ids，停止卡 rule_ids 恆為 null；card.one_liner 未填。\n調度決定（M3 複審 notes）：漏填 → 退回 FULL_A 一次；再缺 → 保留模型停止結論，rule_ids＝[]、framework_notes 記警示。
"""
import json

import pytest

from trader import db
from trader.analysis import writeback as W
from trader.jobs import validate
from trader.paths import PKG_ROOT, get_paths

ASOF = "2026-10-16"
SCHEMAS = validate.load_schemas(PKG_ROOT / "routing" / "output_schemas.json")


def _stopped(**kw) -> dict:
    d = {"ticker": "KO", "decision_date": ASOF, "stopped": True, "stopped_at_gate": "G1",
         "reason_code": "TOO_HARD", "asset_class": "非金融", "starting_grade": None, "downgrades": [],
         "moat_tests": {"pricing_power": None, "attack_copy": None, "price_cut": None},
         "moat_evidence": {"pricing_power": None, "attack_copy": None, "price_cut": None},
         "red_flags": {"exclusion": None, "warning": None}, "capital_allocation_net": None,
         "tail_loss_after_tax_pct": None,
         "normalized_inputs": {"oe_per_share": None, "g10": None, "g5": None, "excluded_shock_years": [],
                               "roe_std_ex_shock_pp": None, "norm_eps": None, "roe": None, "eps_cagr5": None,
                               "incr_roic_gt_rd": False,
                               "insurance": {"tbv_ps": None, "float_ps": None, "float_cost": None}},
         "thesis_draft": {"one_liner": "產品快速變動，十年後樣貌無法判斷", "key_assumptions": [], "kill_criteria": []},
         "missing": [], "inferred_thresholds_used": [], "files": [], "rule_ids": ["SEL-061"]}
    d.update(kw)
    return d


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield conn, paths
    conn.close()


def test_contract_documents_rule_ids():
    assert "rule_ids" in SCHEMAS["FULL_A"]["_說明"]
    prompt = (PKG_ROOT / "routing" / "prompts" / "FULL_A.md").read_text(encoding="utf-8")
    assert "`rule_ids`" in prompt


@pytest.mark.parametrize("bad", [None, [], [""], ["G0-1"], "SEL-061", [61]])
def test_stop_rule_ids_rejects_missing_or_malformed(bad):
    obj = _stopped()
    if bad is None:
        del obj["rule_ids"]
    else:
        obj["rule_ids"] = bad
    assert validate.stop_rule_ids(obj) is None, bad
    # 漏填不是格式錯：validate 不擋（不走引擎重試 → MISSING_DATA），由處理器退回
    assert validate.check("FULL_A", obj, SCHEMAS)[1] == []


@pytest.mark.parametrize("ok", [["SEL-061"], ["P-26a", "SEL-B3"], ["SEL-076b"], ["RISK-12.1"], [" PROC-08 "]])
def test_stop_rule_ids_accepts_rulebook_ids(ok):
    assert validate.stop_rule_ids(_stopped(rule_ids=ok)) == [r.strip() for r in ok]
    assert validate.check("FULL_A", _stopped(rule_ids=ok), SCHEMAS)[1] == []


def test_writeback_stop_card_has_rule_ids_and_one_liner(env):
    conn, paths = env
    out, errors = validate.check("FULL_A", _stopped(), SCHEMAS)
    assert errors == []
    did = W.write_stop_result(conn, paths, "KO", ASOF, "r1", out)
    card = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()[0])
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()[0])
    assert card["rule_ids"] == ["SEL-061"] and log["rule_ids"] == ["SEL-061"]
    assert card["one_liner"] == "產品快速變動，十年後樣貌無法判斷" == log["reason_one_liner"]


def test_stop_card_one_liner_fallback(env):
    conn, paths = env
    did = W.write_stop_result(conn, paths, "KO", ASOF, "r1",
                              {"stopped": True, "stopped_at_gate": "G2", "reason_code": "REJECT"})
    card = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()[0])
    assert card["one_liner"] == "停止於 G2，停止碼 REJECT"


# ---------------------------------------------------------------- 引擎實跑：漏填 → 退回一次；再缺 → 保留停止結論

def _engine_stop(tmp_path, first_has: bool, second_has: bool):
    from test_engine import RUN, full_a, make_env
    def resp(job):
        a = full_a(stopped=True)   # G3／REJECT
        has = second_has if job.params.get("rule_ids_retry") else first_has
        a["rule_ids"] = ["SEL-061"] if has else []
        return a
    env = make_env(tmp_path, {"FULL_A": resp})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    assert env.engine.run(RUN)["status"] == "done"
    rows = env.conn.execute("SELECT d.data, s.data AS card FROM decisions d JOIN signals s USING(decision_id)").fetchall()
    return env, [(json.loads(r["data"]), json.loads(r["card"])) for r in rows]


def test_engine_missing_rule_ids_retried_once_then_filled(tmp_path):
    env, rows = _engine_stop(tmp_path, first_has=False, second_has=True)
    assert len(env.by_type("FULL_A")) == 2 and len(rows) == 1
    log, card = rows[0]
    assert (log["action"], card["reason_code"]) == ("NO_ACTION", "REJECT")
    assert card["rule_ids"] == ["SEL-061"] == log["rule_ids"]
    assert not any("rule_ids 缺" in n for n in log.get("framework_notes") or [])


def test_engine_rule_ids_still_missing_keeps_stop_conclusion(tmp_path):
    env, rows = _engine_stop(tmp_path, first_has=False, second_has=False)
    assert len(env.by_type("FULL_A")) == 2 and len(rows) == 1   # 只退回一次
    log, card = rows[0]
    assert card["reason_code"] == "REJECT" and card["stopped_at_gate"] == "G3"   # 不落成 MISSING_DATA
    assert card["rule_ids"] == [] and log["rule_ids"] == []
    assert any("rule_ids 缺" in n for n in log["framework_notes"])


def test_engine_rule_ids_present_no_retry(tmp_path):
    env, rows = _engine_stop(tmp_path, first_has=True, second_has=True)
    assert len(env.by_type("FULL_A")) == 1 and rows[0][1]["rule_ids"] == ["SEL-061"]


# RM2-9R：rule_index 實際使用的 MKT-／EVID-／CR- 前綴與 MKT-06B 這類數字後大寫尾碼也要收
@pytest.mark.parametrize("ok", [["MKT-061"], ["MKT-06B"], ["EVID-002"], ["SEL-092", "MKT-061"], ["CR-01"]])
def test_stop_rule_ids_accepts_mkt_evid_ids(ok):
    assert validate.stop_rule_ids(_stopped(rule_ids=ok)) == ok


@pytest.mark.parametrize("bad", [["MKT-06BB"], ["mkt-061"], ["EVID002"], ["MKT-"], ["GAP-X03"], ["D-14"],
                                 ["SEL-092 MKT-061"], ["SEL-092", "MKT-06BX"]])
def test_stop_rule_ids_still_rejects_malformed_after_prefix_fix(bad):
    assert validate.stop_rule_ids(_stopped(rule_ids=bad)) is None


# RM2-13（check-RM2-9 缺陷 2）：停止缺編號 → 退回後未停止但 D26 比對不符 → 比對退回後又停止缺編號：編號只退回一次
def test_rule_ids_retry_survives_oe_retry(tmp_path, monkeypatch):
    from test_engine import RUN, full_a, make_env
    from trader.jobs import deep
    seq = [True, False, True, True]   # 各次 FULL_A 是否停止；停止時都漏填 rule_ids
    calls = []

    def resp(job):
        calls.append(dict(job.params))
        a = full_a(stopped=seq[min(len(calls) - 1, len(seq) - 1)])
        a["rule_ids"] = []
        return a
    monkeypatch.setattr(deep, "_oe_check", lambda job, td, pa: {"diffs": ["E 差 20%"],
                                                                 "retried": bool(job.params.get("oe_retry"))})
    env = make_env(tmp_path, {"FULL_A": resp})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    assert env.engine.run(RUN)["status"] == "done"
    assert len(env.by_type("FULL_A")) == 3   # 修前 4 次（rule_ids 被退回第二次）
    assert calls[2].get("oe_retry") == 1 and calls[2].get("rule_ids_retry") == 1
    rows = env.conn.execute("SELECT s.data AS card FROM decisions d JOIN signals s USING(decision_id)").fetchall()
    assert len(rows) == 1 and json.loads(rows[0]["card"])["rule_ids"] == []
