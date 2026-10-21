"""RM6-1（M6-RS1-01）：誠信淘汰（NEVER-04(a)）寫進登錄表 integrity_reject，review_due 只對 8-K 5.02 排 FULL（D27）。"""
import json
import sys

import pytest

from trader import db
from trader.analysis import writeback as W
from trader.ledger import book
from trader.paths import get_paths
from trader.pipelines.review import export_registry

ASOF = "2026-10-16"
GATES_OK = {"gate1": True, "gate2": True, "gate3": True, "gate3_failures": [], "gate2_failures": [],
            "questionnaire_version": "q-v1", "gate3_answers_file": "work/r/AAA/gate3_answers.json",
            "screen_decision": "PASS", "flag_codes": [], "distress_codes": [], "price_date": ASOF,
            "time_zone": "America/New_York"}


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100000.0, "2026-10-01")
    yield conn, paths
    conn.close()


def _reg(conn, t):
    r = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return r["status"], json.loads(r["data"])


def _review_due():
    sys.path.insert(0, str(get_paths().skill_deep / "scripts"))
    import review_due
    return review_due


def _8k(items):
    return [{"form": "8-K", "filingDate": "2026-10-20", "items": items}]


def _td(tmp_path, name, reason):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    sig = "WATCH" if reason == "PASS_ALL" else "NO_ACTION"
    for n, o in (("signal_card.json", {"signal": sig, "reason_code": reason, "certainty_grade": "B"}),
                 ("registry_entry.json", {}), ("decision_log.json", {"action": sig})):
        (d / n).write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text("# 論點\n", encoding="utf-8")
    return d


def test_never04_stop_sets_flag_and_review_due_only_502(env):
    conn, paths = env
    did = W.write_stop_result(conn, paths, "AAA", ASOF, "r", {"reason_code": "REJECT", "stopped_at_gate": "G5",
                                                             "rule_ids": ["NEVER-04", "SEL-091"]})
    st, data = _reg(conn, "AAA")
    assert st == "REJECT" and data["integrity_reject"] is True
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])
    assert any("integrity_reject" in n for n in log["framework_notes"])
    rd = _review_due()
    e = export_registry(conn, "all")[0]
    assert e["integrity_reject"] is True
    assert rd.check(e, _8k("5.02"), None, "2026-10-21", None)[0] == "FULL"
    assert rd.check(e, _8k("1.01"), None, "2026-10-21", None)[0] == "NONE"
    assert rd.check(e, _8k("2.01"), None, "2026-10-21", None)[0] == "NONE"


def test_non_integrity_reject_no_flag(env):
    conn, paths = env
    W.write_stop_result(conn, paths, "BBB", ASOF, "r", {"reason_code": "REJECT", "stopped_at_gate": "G3",
                                                       "rule_ids": ["SEL-048"]})
    _, data = _reg(conn, "BBB")
    assert not data.get("integrity_reject")
    e = export_registry(conn, "all")[0]
    assert _review_due().check(e, _8k("2.01"), None, "2026-10-21", None)[0] == "FULL"   # 一般淘汰：重大 8-K 照排


def test_never04_without_reject_no_flag(env):
    conn, paths = env   # NEVER-04 (b) 只降級；停止碼非 REJECT 不標
    W.write_stop_result(conn, paths, "CCC", ASOF, "r", {"reason_code": "TOO_HARD", "stopped_at_gate": "G5",
                                                       "rule_ids": ["NEVER-04"]})
    assert not _reg(conn, "CCC")[1].get("integrity_reject")


def test_later_reject_keeps_flag_then_pass_clears(env, tmp_path):
    conn, paths = env
    W.write_stop_result(conn, paths, "AAA", "2026-10-10", "r1", {"reason_code": "REJECT", "stopped_at_gate": "G5",
                                                                "rule_ids": ["NEVER-04a"]})
    assert _reg(conn, "AAA")[1]["integrity_reject"] is True
    W.write_stop_result(conn, paths, "AAA", "2026-10-12", "r2", {"reason_code": "REJECT", "stopped_at_gate": "G3",
                                                                "rule_ids": ["SEL-048"]})
    assert _reg(conn, "AAA")[1]["integrity_reject"] is True       # 後續 REJECT 保留
    W.write_full_result(conn, paths, "AAA", "2026-10-14", "r3", _td(tmp_path, "rej", "REJECT"), gates=GATES_OK)
    assert _reg(conn, "AAA")[1]["integrity_reject"] is True       # 完整分析仍 REJECT：保留
    W.write_full_result(conn, paths, "AAA", ASOF, "r4", _td(tmp_path, "ok", "PASS_ALL"), gates=GATES_OK)
    st, data = _reg(conn, "AAA")
    assert st == "WATCH" and not data.get("integrity_reject")     # 完整分析通過才清
