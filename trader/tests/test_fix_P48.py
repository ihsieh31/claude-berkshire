"""P48：報表與儀表板（M8-B8）＋D44（預算暫停續跑／放棄）＋D54（同意只收畫面上的編號）。不連網、不呼叫真模型。"""
from __future__ import annotations

import fcntl
import json
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from test_approvals import _env, _run_to_gate
from test_dashboard import H, PORT, _book, _cash, _nav
from test_engine import ASOF, RUN

from trader import cli, db
from trader import scheduler as S
from trader.dashboard.server import TOKEN_FILE, create_app
from trader.jobs import approvals as A
from trader.pipelines import approved as AP
from trader.portfolio import correlation as C
from trader.report import export as X
from trader.report import metrics as M
from trader.report import validation as V

J = {**H, "content-type": "application/json"}


def _client(env, **kw):
    calls = {"spawn": 0, "cmd": []}

    def spawn():
        calls["spawn"] += 1

    app = create_app(env.paths, PORT, spawn=spawn, spawn_cmd=lambda a: calls["cmd"].append(a), **kw)
    c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    tok = (env.paths.state / TOKEN_FILE).read_text().strip()
    return c, {**J, "x-dashboard-token": tok}, calls


# ---------------------------------------------------------------- M8-D2-03：run-approved 執行中途新同意的也跑
def test_run_approved_releases_new_approvals_midrun(tmp_path, monkeypatch):
    env = _env(tmp_path)
    _run_to_gate(env)                                   # #1 KO（RUN）
    A.approve(env.conn, [1])
    # 另一個 run 的 PEP 正在等同意（#2）
    db.insert(env.conn, "runs", {"run_id": "r2", "kind": "review", "asof": ASOF, "status": "awaiting_approval"})
    db.insert(env.conn, "jobs", {"job_id": "r2:PEP:FULL_A:1", "run_id": "r2", "job_type": "FULL_A", "ticker": "PEP",
                                 "asof": ASOF, "status": "awaiting_approval", "created_at": db.now_iso()})
    db.insert(env.conn, "approvals", {"ticker": "PEP", "run_id": "r2", "asof": ASOF, "job_id": "r2:PEP:FULL_A:1",
                                      "job_type": "FULL_A", "tier": "strong", "status": "pending",
                                      "created_at": db.now_iso()})
    env.conn.commit()
    ran = []

    class FakeEngine:
        def __init__(self, conn, *a):
            self.conn, self.ctx = conn, SimpleNamespace(sec=None)

        def run(self, rid):
            ran.append(rid)
            if len(ran) == 1:
                A.approve(self.conn, [2], via="web")    # 使用者在執行中途又同意了一檔
            return {"status": "done", "done": 1, "failed": 0}

    monkeypatch.setattr(AP, "Engine", FakeEngine)
    out = AP.run_approved(env.config, env.paths, env.conn, router=env.router, clients={})
    assert ran == [RUN, "r2"] and set(out["runs"]) == {RUN, "r2"}
    assert env.conn.execute("SELECT status FROM jobs WHERE job_id='r2:PEP:FULL_A:1'").fetchone()[0] == "pending"
    st = AP.read_status(env.paths)
    assert st["exit_code"] == 0 and st["finished_at"] and set(st["runs"]) == {RUN, "r2"}


def test_approve_while_running_note_and_run_endpoint(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env)
    c, h, calls = _client(env)
    with open(env.paths.state / ".run_approved.lock", "w") as lk:      # 已有 run-approved 在跑
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = c.post("/api/approvals/approve", content=json.dumps({"ids": [1]}), headers=h).json()
        assert r["started"] is False and "已排入目前執行" in r["note"]
        assert c.post("/api/approvals/run", content="{}", headers=h).status_code == 409
    assert calls["spawn"] == 0
    # 權杖／Origin／JSON 檢查
    assert c.post("/api/approvals/run", content="{}", headers=J).status_code == 401
    assert c.post("/api/approvals/run", content="{}", headers={**h, "origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/approvals/run", content="{}", headers={**h, "content-type": "text/plain"}).status_code == 415
    # 鎖已放開：已同意待執行 → 啟動
    assert c.post("/api/approvals/run", content="{}", headers=h).json() == {"started": True}
    assert calls["spawn"] == 1


def test_run_endpoint_409_without_approved(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env)
    c, h, calls = _client(env)
    assert c.post("/api/approvals/run", content="{}", headers=h).status_code == 409 and calls["spawn"] == 0


# ---------------------------------------------------------------- D54：一鍵同意要送編號
def test_d54_all_rejected_and_summary_has_ids(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env)
    c, h, calls = _client(env)
    assert c.post("/api/approvals/approve", content=json.dumps({"all": True, "via": "desktop"}), headers=h).status_code == 400
    assert env.conn.execute("SELECT status FROM approvals").fetchone()[0] == "pending" and calls["spawn"] == 0
    assert c.get("/api/summary", headers=H).json()["approvals"]["ids"] == [1]
    js = (M.Path(__file__).resolve().parents[1] / "trader" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "all: true" not in js and "ids: pend.map" in js


# ---------------------------------------------------------------- M8-D5-04：失敗不設 done、上次執行結果
def test_close_finished_keeps_failed_approved_and_scheduler_skips(tmp_path):
    env = _env(tmp_path)
    _run_to_gate(env)
    A.approve(env.conn, [1])
    env.conn.execute("UPDATE jobs SET status='failed', error='TransportError' WHERE job_type='FULL_A'")
    env.conn.commit()
    A.close_finished(env.conn, RUN)
    row = env.conn.execute("SELECT status, note FROM approvals").fetchone()
    assert tuple(row) == ("approved", A.FAILED_NOTE)
    assert [x["approval_id"] for x in A.list_items(env.conn)] == [1]       # 仍在待同意頁
    # 排程不為只剩失敗工作的已同意列每天空跑 run-approved
    from test_scheduler import _sched
    from test_fix_P23 import _daily_ok
    (tmp_path / "s").mkdir()
    env2, s, clock, run = _sched(tmp_path / "s", "2026-10-16T18:00")
    _daily_ok(env2, "2026-10-16")
    for st in ("failed", "pending"):
        env2.conn.execute("DELETE FROM approvals")
        env2.conn.execute("DELETE FROM jobs")
        db.insert(env2.conn, "jobs", {"job_id": "a:KO:FULL_A:1", "run_id": "a", "job_type": "FULL_A", "ticker": "KO",
                                      "asof": "2026-10-16", "status": st, "created_at": db.now_iso()})
        db.insert(env2.conn, "approvals", {"ticker": "KO", "run_id": "a", "asof": "2026-10-16", "job_id": "a:KO:FULL_A:1",
                                           "job_type": "FULL_A", "status": "approved", "created_at": db.now_iso()})
        env2.conn.commit()
        todo = [k for k, _ in s.plan(env2.conn, {"start_date": "2026-10-16", "last_screen": "2026-08-20",
                                                  "last_watch_review": "2026-10-16"})]
        assert ("approved:2026-10-16" in todo) is (st == "pending"), st


def test_api_approvals_last_run(tmp_path):
    env = _env(tmp_path)
    c, h, _ = _client(env)
    assert c.get("/api/approvals", headers=H).json()["last_run"] is None
    AP.write_status(env.paths, {"started_at": "t0", "pid": 1, "log": AP.LOG_FILE})
    lr = c.get("/api/approvals", headers=H).json()["last_run"]
    assert lr["ended_without_result"] is True                         # 沒在跑、也沒寫結果
    _run_to_gate(env)
    A.approve(env.conn, [1])
    AP.run_approved(env.config, env.paths, env.conn, router=env.router, clients={})
    lr = c.get("/api/approvals", headers=H).json()["last_run"]
    assert lr["exit_code"] == 0 and lr["runs"][RUN]["status"] == "done" and "ended_without_result" not in lr


def test_cli_list_all_prints_note(tmp_path, capsys):
    env = _env(tmp_path)
    _run_to_gate(env)
    A.reject(env.conn, env.paths, [1], note="額度不足")
    assert cli.main(["approvals", "list", "--all", "--state-dir", str(env.paths.state)]) == 0
    out = capsys.readouterr().out
    assert "註：額度不足" in out and "cli" in out


# ---------------------------------------------------------------- D44：預算暫停 → 續跑／放棄
def _paused(env, rid="p1"):
    db.insert(env.conn, "runs", {"run_id": rid, "kind": "analyze", "asof": ASOF, "status": "paused_budget",
                                 "summary": json.dumps({"done": 1, "pending": 1})})
    db.insert(env.conn, "jobs", {"job_id": f"{rid}:KO:DOSSIER:1", "run_id": rid, "job_type": "DOSSIER", "ticker": "KO",
                                 "asof": ASOF, "status": "pending", "created_at": db.now_iso()})
    env.conn.commit()


def test_d44_dashboard_resume_and_abandon(tmp_path):
    env = _env(tmp_path)
    _paused(env)
    c, h, calls = _client(env, budget={"max_llm_calls_per_run": 200})
    a = c.get("/api/approvals", headers=H).json()
    assert [r["run_id"] for r in a["paused_runs"]] == ["p1"] and a["paused_runs"][0]["summary"]["pending"] == 1
    assert a["budget"] == {"max_llm_calls_per_run": 200}
    assert c.post("/api/runs/resume", content=json.dumps({"run_id": "p1"}), headers=J).status_code == 401
    assert c.post("/api/runs/resume", content=json.dumps({"run_id": "nope"}), headers=h).status_code == 409
    r = c.post("/api/runs/resume", content=json.dumps({"run_id": "p1"}), headers=h)
    assert r.status_code == 200 and calls["cmd"] == [["jobs", "resume", "--run-id", "p1"]]
    assert env.conn.execute("SELECT status FROM jobs").fetchone()[0] == "pending"   # 伺服器本身不轉狀態、不呼叫模型
    r = c.post("/api/runs/abandon", content=json.dumps({"run_id": "p1", "note": "不要了"}), headers=h).json()
    assert r == {"abandoned": ["p1:KO:DOSSIER:1"], "run_id": "p1", "status": "abandoned"}
    assert env.conn.execute("SELECT status, error FROM jobs").fetchone()[:] == ("skipped", "使用者放棄：不要了")
    assert c.post("/api/runs/abandon", content=json.dumps({"run_id": "p1"}), headers=h).status_code == 409


def test_d44_abandon_refused_when_pipeline_busy(tmp_path):
    env = _env(tmp_path)
    _paused(env)
    c, h, calls = _client(env)
    with S.pipeline_lock(env.paths, "測試佔用", wait=False):
        assert c.post("/api/runs/abandon", content=json.dumps({"run_id": "p1"}), headers=h).status_code == 409
        assert c.post("/api/runs/resume", content=json.dumps({"run_id": "p1"}), headers=h).status_code == 409
    assert calls["cmd"] == [] and env.conn.execute("SELECT status FROM jobs").fetchone()[0] == "pending"


def test_cli_jobs_resume_runs_engine_without_reset(tmp_path, monkeypatch, capsys):
    from test_cli import _example_config
    import trader.jobs.engine as E
    import trader.llm.router as R
    env = _env(tmp_path)
    _paused(env)
    db.insert(env.conn, "jobs", {"job_id": "p1:PEP:DOSSIER:1", "run_id": "p1", "job_type": "DOSSIER", "ticker": "PEP",
                                 "asof": ASOF, "status": "failed", "error": "x", "created_at": db.now_iso()})
    env.conn.commit()
    ran = []

    class FakeEngine:
        def __init__(self, conn, *a):
            self.ctx = SimpleNamespace(sec=None)

        def run(self, rid):
            ran.append(rid)
            return {"status": "done", "failed": 0}

    monkeypatch.setattr(E, "Engine", FakeEngine)
    monkeypatch.setattr(R, "Router", lambda *a, **k: None)
    cfg = _example_config(tmp_path)
    st = ["--config", str(cfg), "--state-dir", str(env.paths.state)]
    assert cli.main(["jobs", "resume", "--run-id", "p1", *st]) == 0 and ran == ["p1"]
    assert env.conn.execute("SELECT status FROM jobs WHERE job_id='p1:PEP:DOSSIER:1'").fetchone()[0] == "failed"  # 不重設
    env.conn.execute("UPDATE jobs SET status='done' WHERE job_id='p1:KO:DOSSIER:1'")
    env.conn.commit()
    assert cli.main(["jobs", "resume", "--run-id", "p1", *st]) == 0 and ran == ["p1"]
    assert "不需續跑" in capsys.readouterr().out
    assert cli._writes_state(cli.build_parser().parse_args(["jobs", "resume", "--run-id", "p1"]))


# ---------------------------------------------------------------- M8-D5-05：異常說明
def test_anomaly_kinds_and_hints(tmp_path):
    env = _env(tmp_path)
    c = env.conn
    for kind, det in (("job_failed", {"run_id": "analyze-2026-10-16", "job_id": "analyze-2026-10-16:KO:FULL_A:1"}),
                      ("scheduler_task_failed", {"task": "watch:2026-09-01", "log": "state/logs/scheduler-tasks.log"}),
                      ("switch_candidate", {}), ("corr_group_trim", {}), ("hold_review_needs_human", {}),
                      ("ledger_stalled", {"task": "daily:2026-10-19"}), ("weird_kind", {})):
        db.insert(c, "anomalies", {"ticker": "KO", "kind": kind, "detail": json.dumps(det), "status": "open",
                                   "pause_trading": 0, "created_at": db.now_iso()})
    c.commit()
    a = M.anomalies(c)
    by = {x["kind"]: x for x in a["open"]}
    assert a["open"][0]["kind"] == "ledger_stalled"                      # 帳本停擺置頂（D08）
    for k in ("job_failed", "scheduler_task_failed", "switch_candidate", "corr_group_trim", "hold_review_needs_human"):
        assert by[k]["kind_name"] and by[k]["meaning"] and by[k]["hint"], k
    assert "trader jobs retry --run-id analyze-2026-10-16 --job-id analyze-2026-10-16:KO:FULL_A:1" in by["job_failed"]["hint"]
    assert "trader scheduler reset watch:2026-09-01" in by["scheduler_task_failed"]["hint"]
    assert "trader scheduler reset daily:2026-10-19" in by["ledger_stalled"]["hint"]
    assert by["weird_kind"]["hint"] is None and "ledger adjust" in a["ledger_help"]


def test_holdings_pause_reasons(tmp_path):
    env = _env(tmp_path)
    _book(env.conn)
    db.insert(env.conn, "anomalies", {"ticker": "KO", "kind": "corp_action_job_failed", "detail": "{}", "status": "open",
                                      "pause_trading": 1, "created_at": db.now_iso()})
    env.conn.commit()
    h = {x["ticker"]: x for x in M.holdings(env.conn)}
    assert h["KO"]["paused"] and h["KO"]["pause_reasons"] == ["#1 corp_action_job_failed"] and h["PEP"]["pause_reasons"] == []


# ---------------------------------------------------------------- M8-D5-01／02／07／08
def test_analyses_framework_notes_and_export(tmp_path):
    env = _env(tmp_path)
    log = {"action": "WATCH", "reason_one_liner": "建議買進", "framework_notes": ["G8 機會成本未勝出，BUY → WATCH"]}
    db.insert(env.conn, "decisions", {"decision_id": "XYZ-1", "ticker": "XYZ", "decision_date": ASOF, "run_id": "r",
                                      "signal": "WATCH", "data": json.dumps(log, ensure_ascii=False), "created_at": db.now_iso()})
    env.conn.commit()
    assert M.analyses(env.conn)[0]["framework_notes"] == log["framework_notes"]
    assert "G8 機會成本未勝出" in X.to_csv(X.dataset(env.conn, "analyses"))


def test_watchlist_and_api(tmp_path):
    env = _env(tmp_path)
    _book(env.conn)
    for t, st, d in (("OTIS", "WATCH", {"grade": "C", "auto_buy_cap": 62.4, "next_full_review_due": "2027-10-15"}),
                     ("HIG", "REJECT", {"last_full_review_date": "2026-10-15", "reject_basis": "G1"}),
                     ("KO", "HOLDING", {})):
        db.insert(env.conn, "registry", {"ticker": t, "status": st, "data": json.dumps(d), "updated_at": db.now_iso()})
    env.conn.commit()
    w = {x["ticker"]: x for x in M.watchlist(env.conn)}
    assert set(w) == {"OTIS", "HIG"}
    assert w["HIG"]["exclude_until"] == "2027-10-15" and w["OTIS"]["exclude_until"] is None
    assert w["OTIS"]["auto_buy_cap"] == 62.4 and w["HIG"]["reject_basis"] == "G1"
    c, h, _ = _client(env)
    r = c.get("/api/watchlist", headers=H)
    assert r.status_code == 200 and {x["ticker"] for x in r.json()} == {"OTIS", "HIG"}
    assert c.get("/api/export/registry.csv", headers=H).status_code == 200


def test_usage_by_backend_and_estimate_backends(tmp_path):
    env = _env(tmp_path)
    now = db.now_iso()
    env.conn.execute("INSERT INTO llm_calls(job_id,run_id,tier,backend,model,attempt,input_tokens,output_tokens,cost_usd,"
                     "latency_s,ok,created_at) VALUES ('j','r','strong','hark_queue','opus',0,2000,300,0,280,1,?)", (now,))
    env.conn.execute("INSERT INTO llm_calls(job_id,run_id,tier,backend,model,attempt,input_tokens,output_tokens,cost_usd,"
                     "latency_s,ok,error,created_at) VALUES ('j2','r','strong','claude_code','opus',0,900000,30000,6.2,330,0,"
                     "'TransportError',?)", (now,))
    env.conn.commit()
    u = {x["backend"]: x for x in M.usage(env.conn)}
    assert set(u) == {"hark_queue", "claude_code"}
    assert u["claude_code"]["failed"] == 1 and u["claude_code"]["cost_usd"] == 6.2 and u["claude_code"]["estimated"] is False
    assert u["hark_queue"]["estimated"] is True
    env.conn.execute("INSERT INTO jobs(job_id, run_id, job_type, ticker, asof, tier, status, input_tokens, output_tokens, "
                     "latency_s, created_at) VALUES ('j3','x','FULL_A','Z',?,'strong','done',1000000,1,60,'now')", (ASOF,))
    env.conn.execute("INSERT INTO llm_calls(job_id,run_id,tier,backend,model,attempt,input_tokens,output_tokens,cost_usd,"
                     "latency_s,ok,created_at) VALUES ('j3','x','strong','claude_code','opus',0,1000000,1,1,60,1,?)", (now,))
    env.conn.commit()
    assert A.estimate_per_ticker(env.conn)["backends"] == ["claude_code"]


def test_export_anomalies_all_and_meta(tmp_path):
    env = _env(tmp_path)
    _book(env.conn)
    db.insert(env.conn, "anomalies", {"ticker": "KO", "kind": "manual_adjust", "detail": "{}", "status": "resolved",
                                      "pause_trading": 0, "created_at": db.now_iso(), "resolved_at": db.now_iso(),
                                      "resolution": "人工"})
    env.conn.commit()
    rows = X.dataset(env.conn, "anomalies")
    assert len(rows) == 1 and rows[0]["status"] == "resolved" and rows[0]["resolution"] == "人工"
    j = json.loads(X.export(env.conn, list(X.DATASETS), "json", tmp_path / "ex", "d")[0].read_text())
    assert j["meta"][0]["schema_version"] == X.SCHEMA_VERSION and j["meta"][0]["nav_as_of"] == "2026-01-07"
    c, h, _ = _client(env)
    assert "meta" in c.get("/api/export/all.json", headers=H).json()


# ---------------------------------------------------------------- M8-D4-05：排程健康
def test_summary_health_and_scheduler_error(tmp_path, monkeypatch):
    env = _env(tmp_path)
    (env.paths.state / S.STATE_FILE).write_text(json.dumps(
        {"last_tick": "2026-10-20T23:00:00+00:00", "gave_up": ["watch:2026-09-01", "daily:2026-10-01"]}))
    db.insert(env.conn, "runs", {"run_id": "daily-2026-01-02", "kind": "daily_close", "asof": "2026-01-02", "status": "done"})
    db.insert(env.conn, "anomalies", {"ticker": None, "kind": "manual_pause", "detail": "{}", "status": "open",
                                      "pause_trading": 1, "created_at": db.now_iso()})
    env.conn.commit()
    c, h, _ = _client(env)
    hl = c.get("/api/summary", headers=H).json()["health"]
    assert hl["gave_up"] == ["watch:2026-09-01"] and hl["last_tick"].startswith("2026-10-20")
    assert hl["last_daily"] == "2026-01-02" and hl["daily_lag_days"] >= 2 and hl["n_paused"] == 1
    assert "落後" in hl["warning"] and "watch:2026-09-01" in hl["warning"]
    assert "health" not in M.summary(env.conn)                          # 不給 state 時不算（舊呼叫端）

    # tick 連續例外 30 次 → 一筆 scheduler_error（未結不重寫）
    s = S.Scheduler(env.config, env.paths, log=lambda m: None)
    n = {"tick": 0}

    def boom():
        n["tick"] += 1
        raise RuntimeError("壞了")

    class Stop(Exception):
        pass

    def sleep(_):
        if n["tick"] >= 35:
            raise Stop
    monkeypatch.setattr(s, "tick", boom)
    monkeypatch.setattr(s, "check_ready", lambda strict=True: None)
    monkeypatch.setattr(S.time, "sleep", sleep)
    with pytest.raises(Stop):
        s.run_forever(interval_s=0)
    rows = env.conn.execute("SELECT status, detail FROM anomalies WHERE kind='scheduler_error'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "open" and "壞了" in rows[0]["detail"]


# ---------------------------------------------------------------- M8-D10-07：最大相關群
def test_summary_max_group_weight(tmp_path):
    env = _env(tmp_path)
    _book(env.conn)
    for t in ("KO", "PEP"):
        db.insert(env.conn, "registry", {"ticker": t, "status": "HOLDING", "updated_at": db.now_iso(),
                                         "data": json.dumps({"external_drivers": ["consumer_staples_demand"], "grade": "B"})})
    env.conn.commit()
    s = M.summary(env.conn)
    g = C.portfolio_groups(env.conn, {"KO": 60.0, "PEP": 105.0}, include_committed=False)
    assert s["max_group_weight"] == pytest.approx(g["max_group_weight"]) and s["max_group_weight"] > 0
    assert s["max_group"]["members"] == ["KO", "PEP"]
    r = V.build(env.conn, env.paths, None, None)
    assert r["market"]["max_group_weight"] == pytest.approx(g["max_group_weight"])
    assert "最大相關群合計權重" in V.render(r)


# ---------------------------------------------------------------- validation（M8-D1-05、D10-04、D2-07、D2-11、D5-09）
class FakeAlpaca:
    def __init__(self, closes):
        self.closes = closes

    def bars(self, symbols, start, end, adjustment="raw"):
        rows = []
        for t in symbols:
            if t in self.closes:
                a, b = self.closes[t]
                rows += [{"ticker": t, "date": start, "close": a}, {"ticker": t, "date": end, "close": b}]
        return pd.DataFrame(rows, columns=["ticker", "date", "close"])


def test_market_uses_period(tmp_path):
    env = _env(tmp_path)
    c = env.conn
    _cash(c, "2026-01-02", 100000.0, "deposit")
    for d, nav, spy in (("2026-01-02", 100000, 500), ("2026-06-30", 150000, 500), ("2026-12-31", 150000, 600)):
        _nav(c, d, nav, nav, spy)
    m = V.market(c, None, "2026-06-30", "2026-12-31", "SPY")
    assert m["account"] == 0.0 and m["spy"] == pytest.approx(0.2) and m["excess_pp"] == pytest.approx(-20.0)
    full = V.market(c, None, "2026-01-02", "2026-12-31", "SPY")
    assert full["account"] == pytest.approx(0.5)


def test_gate3_returns_from_decision_date(tmp_path):
    env = _env(tmp_path)
    _book(env.conn)
    db.insert(env.conn, "decisions", {"decision_id": "PEP-9", "ticker": "PEP", "decision_date": "2026-01-05", "run_id": "r",
                                      "signal": "WATCH", "data": json.dumps({"action": "WATCH", "gate3_pass": True}),
                                      "created_at": db.now_iso()})
    env.conn.commit()
    seen = []

    class A2(FakeAlpaca):
        def bars(self, symbols, start, end, adjustment="raw"):
            seen.append((tuple(symbols), start))
            return super().bars(symbols, start, end, adjustment)
    r = V.build(env.conn, env.paths, "2026-01-02", "2026-01-07", alpaca=A2({"PEP": (100, 110), "SPY": (500, 510)}))
    assert (("PEP",), "2026-01-05") in seen
    assert r["gates"]["gate3_passed"] == [{"ticker": "PEP", "decision_id": "PEP-9", "return": pytest.approx(0.1),
                                           "noncomparable": None}]


def test_compliance_uses_sizing_and_audits_batch_orders(tmp_path):
    env = _env(tmp_path)
    c = env.conn
    # 日誌缺閘門欄：與 daily 下單同一判定（全部未過）
    logs = [{"action": "BUY", "_id": "X-1", "_ticker": "X", "_date": "2026-10-15", "_run": "r", "_action": "BUY"}]
    bb = V.compliance(c, env.paths, "2026-10-01", "2026-10-31", logs)["bad_buys"]
    from trader.portfolio.sizing import gate_failures
    assert bb[0]["problems"][:3] == gate_failures(logs[0])
    # 建倉批次 ADD：買單指向 PRICE 的 HOLD 閘門決策（閘門 3 未過）→ 列入；人工下單不列
    gate_log = {"action": "HOLD", "review_type": "PRICE", "gate1_not_deleted": True, "gate2_price_below_cap": "通過",
                "gate3_pass": False}
    for did, log in (("X-2", gate_log), ("X-M", {"action": "BUY", "review_type": "MANUAL"})):
        db.insert(c, "decisions", {"decision_id": did, "ticker": "X", "decision_date": "2026-10-20", "run_id": "r2",
                                   "signal": log["action"], "data": json.dumps(log), "created_at": db.now_iso()})
    for oid, did in (("o1", "X-2"), ("o2", "X-M")):
        db.insert(c, "orders", {"order_id": oid, "ticker": "X", "side": "BUY", "qty": 1, "limit_price": 1.0,
                                "status": "open", "decision_id": did, "created_date": "2026-10-21", "updated_at": db.now_iso()})
    c.commit()
    logs2 = V._logs(c, "2026-10-01", "2026-10-31")
    bb = V.compliance(c, env.paths, "2026-10-01", "2026-10-31", logs2)["bad_buys"]
    assert bb == [{"decision_id": "X-2", "ticker": "X", "order_id": "o1", "problems": ["閘門3（能力圈問卷）未過"]}]


def test_stop_words_structured_fields_only():
    base = {"_id": "D", "_ticker": "X", "_date": "2026-10-15", "_run": "r", "_action": "SELL_ALL", "review_type": "HOLD_REVIEW"}
    assert not V._stop_sell({**base, "h2_path": [{"a": "trailing twelve-month FCF 轉負"}], "reason_one_liner": "業務惡化"})
    assert not V._stop_sell({**base, "reason_one_liner": "這是非價格停損：護城河受損"})
    assert V._stop_sell({**base, "reason_one_liner": "觸發移動停損"})
    assert V._stop_sell({**base, "reason": "跌破停損線"})
    assert V._stop_sell({**base, "reason_one_liner": "trailing stop hit"})


def test_render_period_bench_hashes_and_missing(tmp_path):
    env = _env(tmp_path)
    c = env.conn
    _cash(c, "2026-10-16", 100000.0, "deposit")
    _nav(c, "2026-10-16", 100000.0, 100000.0, 200.0)
    _nav(c, "2027-01-15", 101000.0, 100000.0, 210.0)
    for i, (d, h) in enumerate((("2026-10-16", "h1"), ("2026-11-01", "h1"), ("2026-12-01", "h2"))):
        db.insert(c, "decisions", {"decision_id": f"K-{i}", "ticker": "K", "decision_date": d, "run_id": "r",
                                   "signal": "WATCH", "data": json.dumps({"action": "WATCH", "skill_hashes": h}),
                                   "created_at": db.now_iso()})
    db.insert(c, "orders", {"order_id": "o1", "ticker": "NODATA", "side": "BUY", "qty": 1, "limit_price": 1.0,
                            "status": "filled", "decision_id": "K-0", "created_date": "2026-10-20", "updated_at": db.now_iso()})
    db.insert(c, "fills", {"order_id": "o1", "ticker": "NODATA", "side": "BUY", "qty": 1, "price": 1.0, "fill_date": "2026-10-21"})
    c.commit()
    r = V.build(c, env.paths, None, None, alpaca=FakeAlpaca({"VTI": (200, 210)}), bench="VTI")
    md = V.render(r)
    assert "SPY" not in md and "基準 VTI" in md and "VTI（含股利）" in md
    assert "約 3 個月" in md and "使用者指定" not in md
    assert "- h1：2 筆決策，2026-10-16 → 2026-11-01" in md and "- h2：1 筆決策，2026-12-01 → 2026-12-01" in md
    assert V.MISSING_PRICE in md and "產出時間" in md


# ---------------------------------------------------------------- P48R：M8-D5-04 回歸（只剩失敗工作的已同意列）
def _stale_failed(env):
    """KO 的 FULL_A failed → run 結束，close_finished 把同意列留 approved（M8-D5-04）。"""
    _run_to_gate(env)
    A.approve(env.conn, [1])
    env.conn.execute("UPDATE jobs SET status='failed', error='TransportError' WHERE job_type='FULL_A'")
    env.conn.commit()
    env.engine.run(RUN)
    assert tuple(env.conn.execute("SELECT status, note FROM approvals").fetchone()) == ("approved", A.FAILED_NOTE)


def test_p48r_stale_failed_not_rerun_by_run_approved(tmp_path):
    env = _env(tmp_path)
    _stale_failed(env)
    assert A.runnable(env.conn) == [] and A.release(env.conn) == []
    out = AP.run_approved(env.config, env.paths, env.conn, router=env.router, clients={})
    assert out["runs"] == {}
    assert AP.read_status(env.paths)["exit_code"] == 0
    assert cli._run_approved(env.config, env.paths, env.conn) == 0
    assert A.list_items(env.conn)[0]["status"] == "approved"            # 仍留在待同意頁等 retry／abandon


def test_p48r_stale_failed_does_not_block_other_run(tmp_path):
    env = _env(tmp_path)
    _stale_failed(env)
    db.insert(env.conn, "jobs", {"job_id": "r2:PEP:FULL_A:1", "run_id": "r2", "job_type": "FULL_A", "ticker": "PEP",
                                 "asof": ASOF, "status": "awaiting_approval", "created_at": db.now_iso()})
    db.insert(env.conn, "approvals", {"ticker": "PEP", "run_id": "r2", "asof": ASOF, "job_id": "r2:PEP:FULL_A:1",
                                      "job_type": "FULL_A", "tier": "strong", "status": "approved",
                                      "created_at": db.now_iso()})
    # 已同意、工作都跑完卻沒結案（例如 close_finished 前中斷）：仍算要跑，讓 run-approved 結案（與排程舊條件相同）
    db.insert(env.conn, "jobs", {"job_id": "r3:HIG:FULL_A:1", "run_id": "r3", "job_type": "FULL_A", "ticker": "HIG",
                                 "asof": ASOF, "status": "done", "created_at": db.now_iso()})
    db.insert(env.conn, "approvals", {"ticker": "HIG", "run_id": "r3", "asof": ASOF, "job_id": "r3:HIG:FULL_A:1",
                                      "job_type": "FULL_A", "tier": "strong", "status": "approved",
                                      "created_at": db.now_iso()})
    env.conn.commit()
    assert [x["ticker"] for x in A.runnable(env.conn)] == ["PEP", "HIG"]
    assert A.summary(env.conn)["n_runnable"] == 2
    assert A.release(env.conn) == ["r2", "r3"]                            # 不含只剩失敗的 RUN


def test_p48r_run_button_and_endpoint_ignore_stale_failed(tmp_path):
    env = _env(tmp_path)
    _stale_failed(env)
    c, h, calls = _client(env)
    a = c.get("/api/approvals", headers=H).json()
    assert a["summary"]["n_approved_waiting"] == 1 and a["summary"]["n_runnable"] == 0
    assert c.post("/api/approvals/run", content="{}", headers=h).status_code == 409
    assert calls["spawn"] == 0


def test_p48r_abandon_rechecks_status_inside_lock(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    env = _env(tmp_path)
    _paused(env)
    c, h, calls = _client(env)
    real = S.pipeline_lock

    @contextmanager
    def racing_lock(paths, who, wait=True):
        with real(paths, who, wait=wait):   # 取鎖前一刻 resume 剛跑完（伺服器在別的執行緒，另開連線）
            c2 = sqlite3.connect(paths.db)
            c2.execute("UPDATE runs SET status='done' WHERE run_id='p1'")
            c2.execute("UPDATE jobs SET status='done' WHERE run_id='p1'")
            c2.commit()
            c2.close()
            yield

    monkeypatch.setattr(S, "pipeline_lock", racing_lock)
    assert c.post("/api/runs/abandon", content=json.dumps({"run_id": "p1"}), headers=h).status_code == 409
    assert env.conn.execute("SELECT status FROM runs WHERE run_id='p1'").fetchone()[0] == "done"
    assert env.conn.execute("SELECT status FROM jobs WHERE run_id='p1'").fetchone()[0] == "done"


def test_p48r_stop_words_no_stop_negation():
    base = {"_id": "D", "_ticker": "X", "_date": "2026-10-15", "_run": "r", "_action": "SELL_ALL", "review_type": "HOLD_REVIEW"}
    assert not V._stop_sell({**base, "reason_one_liner": "我們沒有停損，賣出是因為護城河受損"})
    assert V._stop_sell({**base, "reason_one_liner": "跌破停損線"})
