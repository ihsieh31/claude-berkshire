"""P30（M5:B3 引擎失敗與重試語義＋設計決策 D42／D43／D46）的回歸測試。全部用 FakeRunner，不連網。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader import db
from trader.jobs import approvals as A
from trader.jobs.engine import abandon, retry_failed
from trader.llm.base import TransportError
from trader.models import JobStatus

from test_engine import ASOF, CAP_INPUT, RUN, dossier_ok, full_a, full_b, full_responses, make_env, thesis, triage
from test_hold_review import _holding, hold

FB = JobStatus.CONSERVATIVE_FALLBACK


def _decs(env, t="KO"):
    return [dict(r) | {"data": json.loads(r["data"])} for r in env.conn.execute(
        "SELECT * FROM decisions WHERE ticker=? ORDER BY rowid", (t,))]


def _anoms(env, kind):
    return env.conn.execute("SELECT * FROM anomalies WHERE kind=?", (kind,)).fetchall()


def _cap_inputs(env, t="KO"):
    d = env.paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{t}.json").write_text(json.dumps(CAP_INPUT), encoding="utf-8")


PRICE_OK = {"ticker": "KO", "price": 999.0, "auto_buy_cap": 999.0, "sell_review_above": 1.0, "absurd_price_line": 1.0,
            "gates": {"gate1_screen": "通過", "gate2_price_lt_cap": "通過", "gate3_on_file": "通過"},
            "proposed_action": "NO_CHANGE", "needs_full": False, "g9_contrarian": "反方：估值已不便宜", "g10_still_valid": True, "reason": "測試"}


# ---------------------------------------------------------------- M5-D1-01（＋M5_D2-04）

def test_redteam_invalid_holding_hold_review_runs(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": full_b("BUY"), "RED_TEAM": "not json",
                              "HOLD_REVIEW": hold()})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    out = env.engine.run(RUN)
    hr = env.by_type("HOLD_REVIEW")
    assert hr and hr[0]["status"] == "done" and json.loads(hr[0]["deps"]) == []
    assert env.by_type("RED_TEAM")[0]["status"] == FB
    assert out["status"] == "done" and out["failed"] == 0 and out[FB] == 1


# ---------------------------------------------------------------- M5-D1-06

def test_full_a_without_gate3_answers_writes_missing_and_hold_review(tmp_path):
    fa = full_a()
    fa["files"] = {"KO/report_A.md": "# A\n"}
    env = make_env(tmp_path, {"FULL_A": fa, "HOLD_REVIEW": hold()})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert not env.by_type("FULL_B")
    d = _decs(env)
    assert d[0]["reason_code"] == "MISSING_DATA" and "gate3_answers.json" in json.dumps(d[0]["data"], ensure_ascii=False)
    assert env.by_type("HOLD_REVIEW")[0]["status"] == "done"


# ---------------------------------------------------------------- M5-D1-08／M5-D8-05／D43

def test_price_invalid_twice_standard_ids_and_fallback_status(tmp_path):
    env = make_env(tmp_path, {"PRICE": [{"ticker": "KO"}, {"ticker": "KO"}]})
    _cap_inputs(env)
    env.add(env.job("KO", "PRICE", 1, params={"price": 60.5, "price_date": ASOF}))
    out = env.engine.run(RUN)
    row = env.by_type("PRICE")[0]
    assert row["status"] == FB and out["status"] == "done" and out["failed"] == 0
    assert retry_failed(env.conn, RUN) == []          # 已保守處理：jobs retry 不重跑
    assert not _anoms(env, "job_failed")
    low = _anoms(env, "job_conservative_fallback")
    assert len(low) == 1 and low[0]["pause_trading"] == 0 and json.loads(low[0]["detail"])["severity"] == "low"
    env.add(env.job("KO", "PRICE", 2, params={"price": 60.5, "price_date": ASOF}))   # 第二輪再不合格
    env.engine.run(RUN)
    ids = [d["decision_id"] for d in _decs(env)]
    assert ids == ["KO-20261016-1", "KO-20261016-2"]
    assert all(d["data"]["review_type"] == "PRICE" and d["data"]["gate3_pass"] is False for d in _decs(env))


def test_base_on_invalid_raises_and_hooks_removed():
    from trader.jobs.handlers import HANDLERS, Handler
    from trader.jobs.review_jobs import PriceHandler
    with pytest.raises(NotImplementedError):
        from trader.models import JobSpec
        Handler().on_invalid(JobSpec("j", RUN, "X", "KO", ASOF), [], None)
    assert not hasattr(Handler, "post_script")
    assert "next_jobs" not in PriceHandler.__dict__
    assert all(type(h).on_invalid is not Handler.on_invalid for h in HANDLERS.values())


def test_corp_action_invalid_is_fallback_without_job_failed(tmp_path):
    env = make_env(tmp_path, {"CORP_ACTION": [{"ticker": "KO"}, {"ticker": "KO"}]})
    env.add(env.job("KO", "CORP_ACTION", 1))
    out = env.engine.run(RUN)
    assert env.by_type("CORP_ACTION")[0]["status"] == FB and out["status"] == "done"
    assert not _anoms(env, "job_failed") and _anoms(env, "corp_action_invalid_output")


# ---------------------------------------------------------------- M5_D2-02（＋M5-D1-05）

def _approval_env(tmp_path):
    env = make_env(tmp_path, full_responses(), approval=["strong"])
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1))
    env.engine.run(RUN)
    return env


def test_reject_then_retry_does_not_reopen(tmp_path):
    env = _approval_env(tmp_path)
    A.reject(env.conn, env.paths, [1], note="不要")
    assert retry_failed(env.conn, RUN) == []
    out = env.engine.run(RUN)
    assert out["status"] != "awaiting_approval" and out["awaiting_approval"] == 0


def test_gate_on_rejected_row_skips_instead_of_waiting(tmp_path):
    env = _approval_env(tmp_path)
    A.reject(env.conn, env.paths, [1])
    fa = env.by_type("FULL_A")[0]["job_id"]
    env.conn.execute("UPDATE jobs SET status='pending', error=NULL WHERE job_id=?", (fa,))   # 手動改回 pending
    env.conn.commit()
    out = env.engine.run(RUN)
    row = env.by_type("FULL_A")[0]
    assert row["status"] == "skipped" and row["error"].startswith("使用者拒絕") and out["awaiting_approval"] == 0


# ---------------------------------------------------------------- M5_D4-01

def test_abandon_clears_paused_and_awaiting_leftovers(tmp_path):
    from trader.scheduler import pending_work
    env = make_env(tmp_path, full_responses(), approval=["strong"])
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1))
    env.engine.start_run("r2", "daily", ASOF)
    env.add(env.job("MSFT", "TRIAGE", 1, run="r2"))
    env.engine.run(RUN)                                   # KO 停在同意閘門
    assert pending_work(env.conn) is not None
    n_dec = len(_decs(env))
    assert abandon(env.conn, RUN, note="不做了") == [f"{RUN}:KO:FULL_A:1"]
    assert abandon(env.conn, "r2") == ["r2:MSFT:TRIAGE:1"]   # 未執行的待辦（例如預算暫停留下的）
    assert pending_work(env.conn) is None and A.list_items(env.conn) == []
    assert env.conn.execute("SELECT status, decided_via FROM approvals").fetchone()[:] == ("rejected", "abandon")
    assert env.conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN,)).fetchone()[0] == "abandoned"
    assert len(_decs(env)) == n_dec
    assert retry_failed(env.conn, RUN) == []


def test_cli_jobs_abandon(tmp_path, capsys, monkeypatch):
    from trader.cli import main
    env = _approval_env(tmp_path)
    rc = main(["jobs", "abandon", "--state-dir", str(env.paths.state), "--run-id", RUN, "--note", "x"])
    assert rc == 0 and "已放棄" in capsys.readouterr().out
    assert env.by_type("FULL_A")[0]["error"] == "使用者放棄：x"


# ---------------------------------------------------------------- M5_D4-06（on_fail）

class _BadSec:
    def cik_for(self, t):
        raise RuntimeError("SEC 掛了")


def test_holding_thesis_pre_script_failure_writes_suspect(tmp_path):
    from trader.portfolio.sizing import add_blocked
    env = make_env(tmp_path, {"THESIS": thesis("完好"), "DOSSIER": dossier_ok()})
    _holding(env)
    env.engine.ctx.sec = _BadSec()
    env.add(env.job("KO", "THESIS", 1, params={"holding": True}))
    env.engine.run(RUN)
    assert env.by_type("THESIS")[0]["status"] == "failed"
    d = [x for x in _decs(env) if x["data"].get("review_type") == "THESIS"]
    assert d and d[0]["data"]["thesis_check"] == "疑似"
    assert add_blocked(env.conn, "KO", ASOF) is not None
    assert json.loads(env.by_type("DOSSIER")[0]["deps"]) == []


def test_holding_full_b_prepare_failure_creates_hold_review(tmp_path):
    """P30R：持股 FULL_B 非格式失敗 → 只接 HOLD_REVIEW（full_stage＝FAIL、fail_reason），不寫 MISSING_DATA 停止紀錄
    （登錄表只由 HOLD_REVIEW 自己的結論更新；hold_jobs 的 FAIL 文字由 P30Tb 提供）。"""
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": full_b("WATCH"), "HOLD_REVIEW": hold()})
    _holding(env)
    env.price_file("KO").unlink()                        # 沒有價格檔 → FULL_B prepare 例外
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert env.by_type("FULL_B")[0]["status"] == "failed"
    hr = env.by_type("HOLD_REVIEW")[0]
    p = json.loads(hr["params"])
    assert hr["status"] == "done" and p["full_stage"] == "FAIL" and "FULL_B 無法完成" in p["fail_reason"]
    assert not any(d["data"].get("review_type") == "FULL" or d["data"].get("stopped") for d in _decs(env))
    assert all(d["data"].get("review_type") == "HOLD_REVIEW" for d in _decs(env))
    reg = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])
    assert reg["status"] == "HOLDING" and reg["grade"] == "B" and reg["auto_buy_cap"] == 55.0


def test_price_pre_script_failure_leaves_failed_gate_record(tmp_path):
    from trader.portfolio.sizing import _latest_gate_decision, gate_failures
    env = make_env(tmp_path, {"PRICE": PRICE_OK})
    _cap_inputs(env)
    env.add(env.job("KO", "PRICE", 1, params={"price": None}))
    env.engine.run(RUN)
    g = _latest_gate_decision(env.conn, "KO", ASOF)
    assert g and gate_failures(g["log"], None) and g["decision_id"] == "KO-20261016-1"


# ---------------------------------------------------------------- M5_D9-01

def test_readd_failed_job_overwrites_params(tmp_path):
    env = make_env(tmp_path, {"PRICE": PRICE_OK})
    _cap_inputs(env)
    # P33：PRICE 缺長債會升級 DOSSIER，夾具須給利率
    spec = lambda px: env.job("KO", "PRICE", 1, params={"price": px, "price_date": ASOF,
                                                         "rates": {"t30": 0.045, "t30_date": ASOF, "t30_20y_median": 0.04}})
    assert env.engine.add_job(spec(None))
    env.engine.run(RUN)
    assert env.by_type("PRICE")[0]["status"] == "failed"
    assert env.engine.add_job(spec(61.2)) is True
    env.engine.run(RUN)
    row = env.by_type("PRICE")[0]
    assert row["status"] == "done" and json.loads(row["params"])["price"] == 61.2
    assert env.engine.add_job(spec(70.0)) is False      # done 不動
    # 同樣是 job_failed 結案（M5_D9-05：重跑完成才結案）
    assert all(a["status"] == "resolved" for a in _anoms(env, "job_failed"))


# ---------------------------------------------------------------- M5_D9-05

def test_retry_keeps_anomaly_open_until_job_done(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": TransportError("暫時")})
    env.add(env.job("KO", "TRIAGE", 1))
    env.engine.run(RUN)
    assert retry_failed(env.conn, RUN) == [f"{RUN}:KO:TRIAGE:1"]
    assert _anoms(env, "job_failed")[0]["status"] == "open"     # --no-run：異常仍在
    env.fake.responses["TRIAGE"] = triage(False)
    assert env.engine.run(RUN)["status"] == "done"
    assert _anoms(env, "job_failed")[0]["status"] == "resolved"


# ---------------------------------------------------------------- M5-D1-09

def test_validation_retry_rebuilds_sandbox(tmp_path):
    seen = []

    def resp(job):
        md = Path(job.sandbox) / "dossier" / "KO.md"
        seen.append(md.read_text(encoding="utf-8"))
        if job.attempt == 0:
            md.write_text(md.read_text(encoding="utf-8") + "\n## 第一次追加\n", encoding="utf-8")
            return "not json"
        return dossier_ok()

    env = make_env(tmp_path, {"DOSSIER": resp, "FULL_A": full_a(stopped=True)})
    env.price_file("KO")
    orig = (env.paths.work / RUN / "KO" / "dossier" / "KO.md").read_text(encoding="utf-8")
    env.add(env.job("KO", "DOSSIER", 1))
    env.engine.run(RUN)
    assert len(seen) == 2 and seen[1] == orig


# ---------------------------------------------------------------- D46 finalize_run

def test_finalize_writes_hold_stop_adding_for_holding_without_conclusion(tmp_path):
    from trader.portfolio.sizing import add_blocked
    env = make_env(tmp_path, {"TRIAGE": TransportError("斷線")})
    _holding(env)
    reg_before = env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0]
    env.add(env.job("KO", "TRIAGE", 1, params={"holding": True}))
    env.engine.run(RUN)
    d = _decs(env)
    assert len(d) == 1 and d[0]["signal"] == "HOLD" and d[0]["data"]["stop_adding"] is True
    assert d[0]["data"]["review_type"] == "HOLD_REVIEW" and d[0]["job_id"] == f"{RUN}:KO:TRIAGE:1"
    assert add_blocked(env.conn, "KO", ASOF) is not None
    assert env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0] == reg_before
    env.engine.run(RUN)                                   # 重跑不重複收尾
    assert len(_decs(env)) == 1


def test_finalize_watch_for_non_holding_and_skips_when_on_fail_concluded(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": TransportError("斷線"), "PRICE": TransportError("斷線")})
    _cap_inputs(env, "MSFT")
    env.add(env.job("KO", "TRIAGE", 1))
    env.add(env.job("MSFT", "PRICE", 1, params={"price": 60.0, "price_date": ASOF}))
    env.engine.run(RUN)
    ko = _decs(env, "KO")
    assert len(ko) == 1 and ko[0]["signal"] == "WATCH" and ko[0]["data"]["review_type"] == "FALLBACK"
    ms = _decs(env, "MSFT")
    assert len(ms) == 1 and ms[0]["data"]["review_type"] == "PRICE"     # on_fail 已寫，不再收尾


def test_finalize_ignores_user_rejected(tmp_path):
    env = _approval_env(tmp_path)
    A.reject(env.conn, env.paths, [1])
    n = len(_decs(env))
    env.engine.run(RUN)
    assert len(_decs(env)) == n


# ---------------------------------------------------------------- D42

def test_expire_stale_after_five_trading_days(tmp_path):
    env = _approval_env(tmp_path)                        # 資料日 2026-10-16（五）
    assert A.expire_stale(env.conn, "2026-10-22") == []  # 19–22：4 個交易日
    assert A.expire_stale(env.conn, "2026-10-23") == [1]
    assert A.list_items(env.conn) == []
    fa = env.by_type("FULL_A")[0]
    assert fa["status"] == "skipped" and fa["error"].startswith("同意已作廢")
    assert retry_failed(env.conn, RUN) == []


def test_new_trigger_supersedes_old_approved_not_running(tmp_path):
    env = _approval_env(tmp_path)
    A.approve(env.conn, [1])
    env.engine.start_run("r2", "daily", ASOF)
    env.price_file("KO", run="r2")
    env.add(env.job("KO", "DOSSIER", 1, run="r2"))
    env.engine.run("r2")
    st = {(r["run_id"], r["status"]) for r in env.conn.execute("SELECT run_id, status FROM approvals")}
    assert st == {(RUN, "superseded"), ("r2", "pending")}
    assert env.by_type("FULL_A")[0]["status"] == "skipped"
    assert A.release(env.conn) == []


def test_daily_pipeline_calls_expire_stale():
    import inspect
    from trader.pipelines import daily
    assert "approvals.expire_stale(conn, date" in inspect.getsource(daily.run_daily)   # F3-DY：另傳 Alpaca 日曆


# ---------------------------------------------------------------- 調度者追加

def test_corr_writeback_passes_worst_loss(tmp_path, monkeypatch):
    from trader.portfolio import correlation as corr
    seen = {}
    real = corr.buy_room

    def spy(conn, ticker, *a, **kw):
        seen.update(kw.get("info") or {})
        return real(conn, ticker, *a, **kw)

    monkeypatch.setattr(corr, "buy_room", spy)
    env = make_env(tmp_path, full_responses())
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert "worst_loss" in seen


def test_switch_candidate_legacy_detail_keeps_original_date(tmp_path):
    from trader.pipelines.daily import _switch_candidates
    from trader.portfolio import sizing as Z
    env = make_env(tmp_path, {})
    db.insert(env.conn, "anomalies", {"ticker": "KO", "kind": "switch_candidate", "status": "open",
                                      "detail": {"date": "2026-10-01"}, "pause_trading": 0, "created_at": db.now_iso()})
    _switch_candidates(env.conn, [{"ticker": "KO", "qty": 0, "reason": f"持股已達 {Z.MAX_HOLDINGS} 檔"}], ASOF)
    d = json.loads(env.conn.execute("SELECT detail FROM anomalies WHERE kind='switch_candidate'").fetchone()[0])
    assert d["first_date"] == "2026-10-01" and d["date"] == ASOF


# ---------------------------------------------------------------- P30R：M5_D4-06 回歸（E4）

def _watch_b(env, t="KO"):
    data = {"ticker": t, "status": "WATCH", "grade": "B", "auto_buy_cap": 55.0,
            "last_review_date": "2026-07-01", "last_full_review_date": "2026-07-01"}
    db.insert(env.conn, "registry", {"ticker": t, "status": "WATCH", "data": json.dumps(data),
                                     "updated_at": db.now_iso()})


def test_watch_dossier_transport_error_leaves_registry_unchanged(tmp_path):
    """E4：WATCH、B 級的 KO，DOSSIER 一次 TransportError → 登錄表不變（不改 MISSING_DATA、不清 grade／cap、
    不推進複查日），收尾寫 FALLBACK WATCH。"""
    env = make_env(tmp_path, {"DOSSIER": TransportError("斷線")})
    _watch_b(env)
    reg_before = env.conn.execute("SELECT status, data FROM registry WHERE ticker='KO'").fetchone()[:]
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1))
    env.engine.run(RUN)
    assert env.by_type("DOSSIER")[0]["status"] == "failed"
    assert env.conn.execute("SELECT status, data FROM registry WHERE ticker='KO'").fetchone()[:] == reg_before
    d = _decs(env)
    assert len(d) == 1 and d[0]["signal"] == "WATCH" and d[0]["data"]["review_type"] == "FALLBACK"
    assert d[0]["data"]["fallback"] is True
    assert not (env.paths.work / RUN / "KO" / "decision.json").exists()


def test_deep_on_invalid_still_writes_missing(tmp_path):
    """on_invalid（模型輸出不完整，D43）維持 _missing_hold：非持股寫 NO_ACTION/MISSING_DATA。"""
    env = make_env(tmp_path, {"DOSSIER": ["not json", "not json"]})
    _watch_b(env)
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1))
    env.engine.run(RUN)
    assert env.by_type("DOSSIER")[0]["status"] == FB
    assert [d["reason_code"] for d in _decs(env)] == ["MISSING_DATA"]


def test_holding_dossier_transport_error_hold_review_fail_stage(tmp_path):
    env = make_env(tmp_path, {"DOSSIER": TransportError("斷線"), "HOLD_REVIEW": hold()})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1, params={"holding": True}))
    env.engine.run(RUN)
    hr = env.by_type("HOLD_REVIEW")
    assert len(hr) == 1 and hr[0]["status"] == "done" and json.loads(hr[0]["deps"]) == []
    p = json.loads(hr[0]["params"])
    assert p["full_stage"] == "FAIL" and "DOSSIER 無法完成" in p["fail_reason"]
    assert not any(d["reason_code"] == "MISSING_DATA" and d["data"].get("stopped") for d in _decs(env))


def test_missing_hold_passes_fail_stage(tmp_path):
    """P30T 交接：_missing_hold（on_invalid、缺檔）帶 full_stage＝FAIL 與 fail_reason；FULL_A 真停在閘門仍為 A。"""
    fa = full_a()
    fa["files"] = {"KO/report_A.md": "# A\n"}
    env = make_env(tmp_path, {"FULL_A": fa, "HOLD_REVIEW": hold()})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    p = json.loads(env.by_type("HOLD_REVIEW")[0]["params"])
    assert p["full_stage"] == "FAIL" and "gate3_answers.json" in p["fail_reason"]
    (tmp_path / "b").mkdir()
    env2 = make_env(tmp_path / "b", {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": hold()})
    _holding(env2)
    env2.price_file("KO")
    env2.add(env2.job("KO", "FULL_A", 1))
    env2.engine.run(RUN)
    assert json.loads(env2.by_type("HOLD_REVIEW")[0]["params"])["full_stage"] == "A"


# ---------------------------------------------------------------- P30R：D46 解除條件（E1）

def test_fallback_hold_unblocks_after_retry_succeeds(tmp_path):
    """E1：持股 TRIAGE 一次 TransportError → 收尾 HOLD＋停止加碼（fallback）；jobs retry 成功後 add_blocked 解除。"""
    from trader.portfolio.sizing import add_blocked
    env = make_env(tmp_path, {"TRIAGE": TransportError("斷線")})
    _holding(env)
    env.add(env.job("KO", "TRIAGE", 1, params={"holding": True}))
    env.engine.run(RUN)
    assert _decs(env)[-1]["data"]["fallback"] is True
    assert add_blocked(env.conn, "KO", ASOF) is not None
    assert retry_failed(env.conn, RUN) == [f"{RUN}:KO:TRIAGE:1"]
    env.fake.responses["TRIAGE"] = triage(False)
    assert env.engine.run(RUN)["status"] == "done"
    assert add_blocked(env.conn, "KO", ASOF) is None


def test_fallback_hold_blocks_only_while_failure_open(tmp_path):
    """沒有較新決策時：job_failed 仍未結 → 擋；結案（例如 abandon、人工）後解除。非 fallback 的停止加碼照擋。"""
    from trader.portfolio.sizing import add_blocked
    env = make_env(tmp_path, {"TRIAGE": TransportError("斷線")})
    _holding(env)
    env.add(env.job("KO", "TRIAGE", 1, params={"holding": True}))
    env.engine.run(RUN)
    assert add_blocked(env.conn, "KO", ASOF) is not None
    env.conn.execute("UPDATE anomalies SET status='resolved' WHERE kind='job_failed'")
    env.conn.commit()
    assert add_blocked(env.conn, "KO", ASOF) is None
    did = _decs(env)[-1]["decision_id"]
    log = _decs(env)[-1]["data"]
    log.pop("fallback")
    env.conn.execute("UPDATE decisions SET data=? WHERE decision_id=?", (json.dumps(log), did))
    env.conn.commit()
    assert add_blocked(env.conn, "KO", ASOF) is not None       # 一般 HOLD_REVIEW 停止加碼不受影響


# ---------------------------------------------------------------- P30R：D42 已開始執行的舊鏈不作廢（E2）

def test_new_trigger_keeps_old_approved_chain_already_started(tmp_path):
    """E2：舊 run 已同意、FULL_A 已 done、FULL_B pending（例如預算暫停）→ 新觸發不作廢，舊鏈跑完。"""
    env = _approval_env(tmp_path)
    A.approve(env.conn, [1])
    A.release(env.conn)
    fa = env.by_type("FULL_A")[0]["job_id"]
    env.conn.execute("UPDATE jobs SET status='done' WHERE job_id=?", (fa,))
    env.conn.commit()
    env.add(env.job("KO", "FULL_B", 1, deps=[fa]))
    env.engine.start_run("r2", "daily", ASOF)
    env.price_file("KO", run="r2")
    env.add(env.job("KO", "DOSSIER", 1, run="r2"))
    env.engine.run("r2")
    st = {(r["run_id"], r["status"]) for r in env.conn.execute("SELECT run_id, status FROM approvals")}
    assert st == {(RUN, "approved"), ("r2", "pending")}
    assert env.by_type("FULL_B")[0]["status"] == "pending"
