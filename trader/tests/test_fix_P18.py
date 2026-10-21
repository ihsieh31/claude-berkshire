"""P18（M3-B）：TRIAGE escalate 對帳與不收檔、THESIS 完好對帳與 10-K/A、HOLD_REVIEW 疑似觀察停止加碼。"""
from __future__ import annotations

import json

from test_engine import RUN, full_a, make_env
from test_hold_review import _holding, _last_log, hold
from test_review_jobs import (ASOF, FakeSec, _ctx, _decisions, _job, _price_params, _reg, _seed_registry,
                              env, thesis_result)  # noqa: F401 - env 為夾具

from trader.jobs.hold_jobs import HoldReviewHandler
from trader.jobs.review_jobs import ThesisHandler, TriageHandler
from trader.jobs.sandbox import create_sandbox

TRIAGE_IN = [{"type": "8-K", "date": "2026-10-10", "items": ["8.01"]},
             {"type": "news", "date": "2026-10-11", "headline": "Company signs deal", "source": "X"}]


def _item(ans):
    return {"ref": "8-K 2026-10-10", "answer": ans, "touches": "", "reason": "測試"}


# ---------------------------------------------------------------- M3_D1-01 / M3_D4-04（TRIAGE）

def test_triage_yes_answer_overrides_escalate_false(env):
    paths, conn = env
    _seed_registry(conn, "KO", "WATCH", {"last_review_date": "2026-01-01"})
    job = _job("TRIAGE", _price_params(base_action="NONE", triage=TRIAGE_IN))
    res = {"ticker": "KO", "items": [_item("是"), _item("否")], "escalate": False}
    nxt = TriageHandler().post(job, res, _ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["THESIS"]
    assert "framework_note" in nxt[0].params["triage_result"]
    assert _reg(conn)["data"]["last_review_date"] == "2026-01-01"   # 未被 touch_review 推進


def test_triage_missing_answers_escalate(env):
    paths, conn = env
    job = _job("TRIAGE", _price_params(base_action="NONE", triage=TRIAGE_IN))
    nxt = TriageHandler().post(job, {"ticker": "KO", "items": [_item("否")], "escalate": False}, _ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["THESIS"]


def test_triage_all_no_does_not_escalate(env):
    paths, conn = env
    job = _job("TRIAGE", _price_params(base_action="NONE", triage=TRIAGE_IN))
    res = {"ticker": "KO", "items": [_item("否"), _item("否")], "escalate": False}
    assert TriageHandler().post(job, res, _ctx(paths, conn)) == []
    assert "framework_note" not in res


# ---------------------------------------------------------------- M3-D8-03（TRIAGE 不收檔）

def test_triage_post_collects_no_files(env, tmp_path):
    paths, conn = env
    ctx = _ctx(paths, conn)
    job = _job("TRIAGE", _price_params(base_action="NONE"))
    sb = create_sandbox(tmp_path / "sb", job)
    (sb / "cap_result.json").write_text(json.dumps({"IV_base": 1.0}), encoding="utf-8")
    TriageHandler().post(job, {"ticker": "KO", "items": [], "escalate": False, "files": ["cap_result.json"]}, ctx)
    assert not (paths.work / job.run_id / "KO" / "cap_result.json").exists()


# ---------------------------------------------------------------- M3_D1-02 / M3_D4-04（THESIS）

def test_thesis_intact_but_triggered_becomes_suspect(env):
    paths, conn = env
    nxt = ThesisHandler().post(_job("THESIS", _price_params(holding=True)), thesis_result("完好", hit=True),
                               _ctx(paths, conn))
    d = _decisions(conn)[0]["data"]
    assert d["thesis_check"] == "疑似" and d["action"] == "HOLD" and "框架" in d["reason_one_liner"]
    assert [j.job_type for j in nxt] == ["DOSSIER"]
    assert nxt[0].params["thesis_result"]["verdict"] == "疑似"


def test_thesis_intact_but_r_item_worse_becomes_suspect(env):
    paths, conn = env
    r = thesis_result("完好")
    r["r_list_status"] = [{"item": "R2", "status": "惡化", "evidence": "x"}]
    nxt = ThesisHandler().post(_job("THESIS", _price_params()), r, _ctx(paths, conn))
    assert _decisions(conn)[0]["data"]["thesis_check"] == "疑似" and [j.job_type for j in nxt] == ["DOSSIER"]


def test_thesis_fewer_criteria_rows_than_input_becomes_suspect(env):
    paths, conn = env
    job = _job("THESIS", _price_params(kill_criteria=["毛利率連兩季下滑", "CEO 離職"]))
    ThesisHandler().post(job, thesis_result("完好"), _ctx(paths, conn))
    assert _decisions(conn)[0]["data"]["thesis_check"] == "疑似"


def test_thesis_intact_consistent_stays_intact(env):
    paths, conn = env
    job = _job("THESIS", _price_params(kill_criteria=["毛利率連兩季下滑"]))
    assert ThesisHandler().post(job, thesis_result("完好"), _ctx(paths, conn)) == []
    assert _decisions(conn)[0]["data"]["thesis_check"] == "完好"


# ---------------------------------------------------------------- M3-D5-03（10-K/A、8-K/A）

def test_thesis_stages_10ka_and_8ka(env):
    paths, conn = env
    ka, kk = "https://www.sec.gov/a/ka.htm", "https://www.sec.gov/a/8ka.htm"
    sec = FakeSec(rows=[{"form": "10-K/A", "filingDate": "2026-09-01", "url": ka, "accessionNumber": "1-26-000003"},
                        {"form": "8-K/A", "filingDate": "2026-09-02", "url": kk, "accessionNumber": "1-26-000004"}],
                  texts={ka: "Amended annual report.\n", kk: "Item 2.02\nAmended results.\n"})
    job = _job("THESIS", _price_params(last_review_date="2026-01-01"))
    ThesisHandler().pre_script(job, _ctx(paths, conn, sec))
    names = sorted(p.name for p in (paths.work / job.run_id / "KO" / "thesis_sec").glob("*.txt"))
    assert any(n.startswith("10-KA_2026-09-01") for n in names) and any(n.startswith("8-KA_2026-09-02") for n in names)


# ---------------------------------------------------------------- M3_D4-02（HOLD_REVIEW）

def test_hold_with_observe_until_forces_stop_adding(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": hold(stop=False)})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    log = _last_log(env)
    assert log["action"] == "HOLD" and log["stop_adding"] is True


def test_hold_after_suspect_thesis_forces_stop_adding(env, monkeypatch):
    paths, conn = env
    seen = {}
    from trader.analysis import writeback
    monkeypatch.setattr(writeback, "write_hold_review_result",
                        lambda *a, **k: seen.update(result=a[5], notes=a[7]))
    r = {**hold(stop=False), "observe_until": None}
    job = _job("HOLD_REVIEW", {"full_stage": "B", "thesis_result": {"verdict": "疑似"}})
    assert HoldReviewHandler().post(job, r, _ctx(paths, conn)) == []
    assert seen["result"]["stop_adding"] is True and any("停止加碼" in n for n in seen["notes"])


def test_hold_intact_no_observe_keeps_stop_adding_false(env, monkeypatch):
    paths, conn = env
    seen = {}
    from trader.analysis import writeback
    monkeypatch.setattr(writeback, "write_hold_review_result", lambda *a, **k: seen.update(result=a[5]))
    r = {**hold(stop=False), "observe_until": None}
    HoldReviewHandler().post(_job("HOLD_REVIEW", {"full_stage": "B"}), r, _ctx(paths, conn))
    assert seen["result"]["stop_adding"] is False
