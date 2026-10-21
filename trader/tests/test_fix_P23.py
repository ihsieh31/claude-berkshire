"""P23：排程 daily 永不放棄＋reset（D08、M8-D7-02）、缺上次初選日不自動初選＋--mark-screened（D09）、
初選日由 runs 推導（M8-D10-05）、watch／screen 穩定鍵（M7-D2-03）、待同意定義與 run-approved（M7-D2-02）、
daily 未完成時不跑 watch／screen（M7-D6-01）、review_watch 生效（M7-D2-09）、失敗診斷（M7-D5-08）、
status 列出放棄／重試／暫緩（M8-D5-06）、鎖 pid（X25）、排程 SIGTERM 收子行程。不連網、不呼叫模型。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import test_daily_pipeline as TD
from test_daily_pipeline import CARD, D1, FakeAlpaca, _bars, _dec, _reg, world  # noqa: F401
from test_scheduler import FakeRunner, _sched
from trader import cli, db
from trader import scheduler as S
from trader.config import ConfigError, load_config
from trader.pipelines import daily as DP
from trader.portfolio import sizing as Z

TRADER_DIR = Path(__file__).resolve().parents[1]
HELD = "TRADER_PIPELINE_LOCK_HELD"


def _flaky_daily(run, bad_dates):
    """只有 bad_dates 裡的 daily 失敗（exit 1，不寫 runs），其餘照 FakeRunner。"""
    orig = FakeRunner.__call__

    def call(self, argv):
        if argv[3] == "daily" and argv[5] in bad_dates:
            self.calls.append(f"daily --date {argv[5]} (fail)")
            return 1
        return orig(self, argv)
    run.__class__ = type("Flaky", (FakeRunner,), {"__call__": call})


def _daily_ok(env, *dates):
    for d in dates:
        db.insert(env.conn, "runs", {"run_id": f"daily-{d}", "kind": "daily_close", "asof": d, "status": "done",
                                     "started_at": db.now_iso(), "finished_at": db.now_iso()})
    env.conn.commit()


def _anoms(env, kind):
    return [tuple(r) for r in env.conn.execute(
        "SELECT status, pause_trading FROM anomalies WHERE kind=? ORDER BY anomaly_id", (kind,))]


# ---------------------------------------------------------------- D08／M8-D7-02：daily 永不放棄
def test_daily_never_gives_up_retries_after_next_close_then_resumes(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-19T18:00")
    bad = {"2026-10-19"}
    _flaky_daily(run, bad)
    for h in ("18:00", "18:31", "19:02"):                       # 30 分鐘重試兩次 → 第 3 次失敗
        clock.set(f"2026-10-19T{h}")
        s.once()
    st = s.load_state()
    assert "daily:2026-10-19" not in st.get("gave_up", [])
    assert st["failures"]["daily:2026-10-19"] == 3
    nxt = datetime.fromtimestamp(st["retry_at"]["daily:2026-10-19"], S.ZoneInfo("America/New_York"))
    assert (nxt.date().isoformat(), nxt.hour, nxt.minute) == ("2026-10-20", 17, 30)   # 下一個收盤
    assert _anoms(env, "ledger_stalled") == [("open", 0)]
    detail = json.loads(env.conn.execute("SELECT detail FROM anomalies WHERE kind='ledger_stalled'").fetchone()[0])
    assert detail["started_at"].startswith("2026-10-19T19:02") and detail["failures"] == 3
    run.calls.clear()
    clock.set("2026-10-20T09:00")                               # 下一個收盤前：不再每 30 分鐘重試
    s.once()
    clock.set("2026-10-20T12:00")
    s.once()
    assert not [c for c in run.calls if c.startswith("daily")]
    clock.set("2026-10-20T17:45")                               # 新收盤後再試一次（仍失敗）
    s.once()
    assert [c for c in run.calls if c.startswith("daily")] == ["daily --date 2026-10-19 (fail)"]
    assert _anoms(env, "ledger_stalled") == [("open", 0)]       # 未結不重寫
    assert s.load_state()["failures"]["daily:2026-10-19"] == 4
    bad.clear()                                                 # 網路恢復：下一個收盤依日期順序補完
    run.calls.clear()
    clock.set("2026-10-21T17:45")
    s.once()
    assert [c for c in run.calls if c.startswith("daily")] == [
        "daily --date 2026-10-19", "daily --date 2026-10-20", "daily --date 2026-10-21"]
    assert _anoms(env, "ledger_stalled") == [("resolved", 0)]  # 成功即結案
    assert "daily:2026-10-19" not in s.load_state().get("failures", {})


def test_old_state_daily_gave_up_no_longer_blocks(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-20T18:00")
    s.save_state({"start_date": "2026-10-19", "last_screen": "2026-08-20", "gave_up": ["daily:2026-10-19"]})
    s.once()
    assert [c for c in run.calls if c.startswith("daily")] == ["daily --date 2026-10-19", "daily --date 2026-10-20"]


def test_reset_clears_state_and_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(HELD, raising=False)
    env, s, clock, run = _sched(tmp_path, "2026-10-19T18:00")
    s.save_state({"last_screen": "2026-08-20", "gave_up": ["watch:init", "report:2026-10"],
                  "failures": {"watch:init": 3, "daily:2026-10-19": 3, "report:2026-10": 3},
                  "retry_at": {"daily:2026-10-19": 9e9}})
    assert s.reset("watch:init") == ["watch:init"]
    st = s.load_state()
    assert st["gave_up"] == ["report:2026-10"] and "watch:init" not in st["failures"]
    rc = cli.main(["scheduler", "reset", "--config", str(tmp_path / "config.yaml"),
                   "--state-dir", str(env.paths.state)])
    assert rc == 0 and "daily:2026-10-19" in capsys.readouterr().out
    st = s.load_state()
    assert st["gave_up"] == [] and st["failures"] == {} and st["retry_at"] == {} and st["last_screen"] == "2026-08-20"


# ---------------------------------------------------------------- D09：缺上次初選日
def test_missing_last_screen_waits_and_records_anomaly_once(tmp_path):
    logs = []
    env, s, clock, run = _sched(tmp_path, "2026-10-21T18:00", last_screen=None)
    s.log = logs.append
    s.once()
    clock.set("2026-10-21T19:00")
    s.once()
    assert not any(c.startswith("bootstrap") for c in run.calls)
    assert _anoms(env, "scheduler_no_last_screen") == [("open", 0)]
    assert sum(1 for m in logs if m.startswith("[WAIT] 找不到上次初選日")) == 1   # 同一天只記一次
    # 補上日期（--mark-screened）後異常結案、下一季照常初選
    st = s.load_state()
    st["last_screen"] = "2026-10-15"
    s.save_state(st)
    s.once()
    assert _anoms(env, "scheduler_no_last_screen") == [("resolved", 0)]
    clock.set("2026-11-20T17:45")
    s.once()
    assert "bootstrap --asof 2026-11-20" in run.calls


def _cli_bootstrap(tmp_path, env, *extra):
    return cli.main(["bootstrap", "--asof", "2026-10-15", *extra, "--config", str(tmp_path / "config.yaml"),
                     "--state-dir", str(env.paths.state)])


def test_bootstrap_tickers_mark_screened(tmp_path, monkeypatch):
    from trader.pipelines import bootstrap as B
    monkeypatch.delenv(HELD, raising=False)
    env, s, clock, run = _sched(tmp_path, "2026-10-21T18:00", last_screen=None)
    seen = []
    monkeypatch.setattr(B, "run_bootstrap", lambda cfg, paths, conn, asof, tickers, top, dry_run, force:
                        seen.append((tickers, dry_run)) or {"asof": asof, "tickers": tickers, "dry_run": dry_run})
    assert _cli_bootstrap(tmp_path, env, "--tickers", "KO") == 0
    assert "last_screen" not in s.load_state()                   # --tickers 不加旗標：不記
    assert _cli_bootstrap(tmp_path, env, "--tickers", "KO", "--mark-screened", "--dry-run") == 0
    assert s.load_state()["last_screen"] == "2026-10-15"          # 只補記日期
    assert seen[-1] == (["KO"], True)


# ---------------------------------------------------------------- M8-D10-05：初選日由 runs 推導；分析失敗不算初選失敗
def test_last_screen_derived_from_runs(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-20T18:00", last_screen=None)
    for rid, kind in (("screen-2026-10-15", "bootstrap"), ("analyze-2026-10-15", "analyze")):
        db.insert(env.conn, "runs", {"run_id": rid, "kind": kind, "asof": "2026-10-15", "status": "done",
                                     "started_at": db.now_iso()})
    env.conn.commit()
    _daily_ok(env, "2026-10-20", "2026-11-20")
    clock.set("2026-11-20T18:00")                       # q＝11-20 > 10-15 → 該初選
    st = {"start_date": "2026-11-20", "last_watch_review": "2026-11-20"}
    assert "screen:2026-11-20" in [k for k, _ in s.plan(env.conn, dict(st))]
    clock.set("2026-10-20T18:00")                       # q＝08-20 ≤ 10-15 → 不初選，也不 WAIT
    todo = [k for k, _ in s.plan(env.conn, dict(st, start_date="2026-10-20"))]
    assert not any(k.startswith("screen:") for k in todo) and s.held == []
    # dry-run／單跑 screen（沒有 analyze run）不算
    db.insert(env.conn, "runs", {"run_id": "screen-2026-11-20", "kind": "bootstrap", "asof": "2026-11-20",
                                 "status": "done", "started_at": db.now_iso()})
    env.conn.commit()
    assert s._last_screen(env.conn, {}) == "2026-10-15"


def test_bootstrap_analysis_failures_return_0_and_write_last_screen(tmp_path, monkeypatch, capsys):
    from trader.pipelines import bootstrap as B
    monkeypatch.delenv(HELD, raising=False)
    env, s, clock, run = _sched(tmp_path, "2026-10-21T18:00", last_screen=None)

    def fake(cfg, paths, conn, asof, tickers, top, dry_run, force):
        db.insert(conn, "runs", {"run_id": f"screen-{asof}", "kind": "bootstrap", "asof": asof, "status": "done",
                                 "started_at": db.now_iso()})
        conn.commit()
        return {"asof": asof, "analyze": {"run_id": f"analyze-{asof}", "engine": {"failed": 1, "status": "failed"}}}
    monkeypatch.setattr(B, "run_bootstrap", fake)
    assert _cli_bootstrap(tmp_path, env) == 0
    assert s.load_state()["last_screen"] == "2026-10-15"
    assert "jobs retry --run-id analyze-2026-10-15" in capsys.readouterr().out


def test_screen_done_but_analysis_crashed_retries_with_analyze(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-11-20T18:00")
    orig = FakeRunner.__call__

    def call(self, argv):
        if argv[3] == "bootstrap":                     # 初選完成、分析途中被殺
            self.calls.append("bootstrap (crash)")
            db.insert(self.env.conn, "runs", {"run_id": f"screen-{argv[5]}", "kind": "bootstrap", "asof": argv[5],
                                              "status": "done", "started_at": db.now_iso()})
            self.env.conn.commit()
            return 124
        return orig(self, argv)
    run.__class__ = type("Crash", (FakeRunner,), {"__call__": call})
    s.once()
    st = s.load_state()
    assert st["last_screen"] == "2026-11-20" and "analyze --asof 2026-11-20" in run.calls
    assert "reanalyze" not in st and not st.get("failures")


# ---------------------------------------------------------------- P23R：analyze 續跑不得永久擋住季度初選
def _crash_then_analyze(run, analyze_run_status=None):
    """bootstrap：初選完成後被殺（124）；analyze：一律 exit 1。analyze_run_status 非 None 時先寫 analyze-D run
    （模擬流程跑完、只有個別工作失敗）。"""
    orig = FakeRunner.__call__

    def call(self, argv):
        if argv[3] == "bootstrap":
            self.calls.append("bootstrap (crash)")
            db.insert(self.env.conn, "runs", {"run_id": f"screen-{argv[5]}", "kind": "bootstrap", "asof": argv[5],
                                              "status": "done", "started_at": db.now_iso()})
            self.env.conn.commit()
            return 124
        if argv[3] == "analyze":
            self.calls.append("analyze (fail)")
            if analyze_run_status:
                db.insert(self.env.conn, "runs", {"run_id": f"analyze-{argv[5]}", "kind": "analyze", "asof": argv[5],
                                                  "status": analyze_run_status, "started_at": db.now_iso(),
                                                  "summary": json.dumps({"failed": 1, "done": 14})})
                self.env.conn.commit()
            return 1
        return orig(self, argv)
    run.__class__ = type("CrashAnalyze", (FakeRunner,), {"__call__": call})


def test_analyze_given_up_clears_reanalyze_next_quarter_screens(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-11-20T18:00")
    _crash_then_analyze(run)
    for t in ("2026-11-20T18:00", "2026-11-20T18:31", "2026-11-20T19:02"):
        clock.set(t)
        s.once()
    st = s.load_state()
    assert "analyze:2026-11-20" in st["gave_up"] and "reanalyze" not in st and st["last_screen"] == "2026-11-20"
    an = env.conn.execute("SELECT detail FROM anomalies WHERE kind='scheduler_task_failed'").fetchall()
    assert len(an) == 1 and "analyze --asof 2026-11-20" in json.loads(an[0][0])["note"]
    run.calls.clear()
    clock.set("2027-03-22T18:00")
    s.once()
    assert "bootstrap (crash)" in run.calls   # 下一季初選照常排


def test_analyze_partial_job_failure_counts_as_done(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-11-20T18:00")
    _crash_then_analyze(run, analyze_run_status="failed")
    s.once()
    st = s.load_state()
    assert run.calls.count("analyze (fail)") == 1
    assert "reanalyze" not in st and not st.get("failures") and not st.get("gave_up")


def test_stale_reanalyze_dropped_and_reset_clears_it(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2027-03-22T18:00")
    _daily_ok(env, "2027-03-22")
    st = {"start_date": "2027-03-22", "last_screen": "2026-11-20", "reanalyze": "2026-11-20",
          "last_watch_review": "2027-03-22", "gave_up": ["analyze:2026-11-20"]}
    todo = [k for k, _ in s.plan(env.conn, st)]
    assert "screen:2027-03-20" in todo and "reanalyze" not in st   # 舊狀態檔殘留：新季度到期即丟棄
    s.save_state({"reanalyze": "2026-11-20", "failures": {"analyze:2026-11-20": 2}})
    assert s.reset("analyze:2026-11-20") == ["analyze:2026-11-20"] and "reanalyze" not in s.load_state()
    s.save_state({"reanalyze": "2026-11-20"})
    s.reset()
    assert "reanalyze" not in s.load_state()


# ---------------------------------------------------------------- M7-D2-02：待同意定義、孤兒工作、run-approved
def test_orphan_running_job_does_not_block_and_approved_runs(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    db.insert(env.conn, "jobs", {"job_id": "review-x:KO:PRICE:1", "run_id": "review-x", "job_type": "PRICE",
                                 "ticker": "KO", "asof": "2026-10-16", "status": "running", "created_at": db.now_iso()})
    db.insert(env.conn, "approvals", {"ticker": "KO", "run_id": "analyze-2026-10-16", "asof": "2026-10-16",
                                      "job_id": "J1", "job_type": "FULL_A", "status": "approved",
                                      "created_at": db.now_iso()})
    env.conn.commit()
    _daily_ok(env, "2026-10-16")
    st = {"start_date": "2026-10-16", "last_screen": "2026-08-20", "last_watch_review": "2026-09-01"}
    todo = [k for k, _ in s.plan(env.conn, dict(st))]
    assert "watch:2026-09-01" in todo and "approved:2026-10-16" in todo
    assert S.pending_work(env.conn) is None


# ---------------------------------------------------------------- M7-D2-03：穩定鍵、部分失敗視為完成、freeze 不被壓
def test_watch_key_stable_and_failures_accumulate(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00", fail={"review"})
    s.once()
    clock.set("2026-10-19T18:00")
    s.once()
    clock.set("2026-10-20T18:00")
    s.once()
    st = s.load_state()
    assert "watch:init" not in st.get("gave_up", []) and len([c for c in run.calls if c.startswith("review")]) == 3
    assert st["retry_at"]["watch:init"] > 0   # D81：不永久放棄，下一個觀察週期再試
    clock.set("2026-10-21T18:00")
    n = len(run.calls)
    s.once()
    assert not any(c.startswith("review") for c in run.calls[n:])   # 放棄不再每天重來


def test_watch_partial_job_failure_counts_as_done(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    orig = FakeRunner.__call__

    def call(self, argv):
        if argv[3] == "review":
            asof = argv[argv.index("--asof") + 1]
            db.insert(self.env.conn, "runs", {"run_id": f"review-{asof}-watch", "kind": "review", "asof": asof,
                                              "status": "failed", "started_at": db.now_iso(),
                                              "summary": json.dumps({"engine": {"failed": 1}})})
            self.env.conn.commit()
            self.calls.append("review")
            return 1
        return orig(self, argv)
    run.__class__ = type("Partial", (FakeRunner,), {"__call__": call})
    s.once()
    st = s.load_state()
    assert st["last_watch_review"] == "2026-10-16" and not st.get("failures")


def test_freeze_runs_when_screen_given_up(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-12-01T18:00")
    st = {"start_date": "2026-12-01", "last_screen": "2026-08-20", "last_watch_review": "2026-12-01",
          "gave_up": ["screen:2026-11-20"], "last_report_month": "2026-12"}
    todo = [k for k, _ in s.plan(env.conn, dict(st))]
    assert "freeze:2026-12" in todo and not any(k.startswith("screen:") for k in todo)


# ---------------------------------------------------------------- M7-D6-01：daily 未完成時不跑 watch／screen
def test_pending_daily_holds_watch_and_screen(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-11-20T18:00", fail=("daily",))
    st = s.load_state()
    st["start_date"] = "2026-11-19"
    s.save_state(st)
    s.once()
    assert [c for c in run.calls if not c.startswith("fetch")] == ["daily --date 2026-11-19"]
    assert any("2026-11-19 尚未完成" in w for w in s.held)


# ---------------------------------------------------------------- M7-D2-09：review_watch 生效；設定驗證；初選日
def test_review_watch_weekly(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-23T18:00")
    _daily_ok(env, "2026-10-23")
    s.cfg.schedule["review_watch"] = "weekly"
    st = {"start_date": "2026-10-23", "last_screen": "2026-08-20", "last_watch_review": "2026-10-16"}
    assert "watch:2026-10-16" in [k for k, _ in s.plan(env.conn, dict(st))]
    s.cfg.schedule["review_watch"] = "biweekly"
    assert not any(k.startswith("watch:") for k, _ in s.plan(env.conn, dict(st)))


def test_config_schedule_keys(tmp_path):
    base = (tmp_path / "c.yaml")
    head = ("models:\n  strong: {backend: fake, model: f}\n  medium: {backend: fake, model: f}\n"
            "  weak: {backend: fake, model: f}\n")
    base.write_text(head, encoding="utf-8")
    cfg = load_config(base, env_file=tmp_path / "none.env")
    assert cfg.schedule["quarterly_screen"][0] == "03-20" and "review_holdings" not in cfg.schedule
    base.write_text(head + "schedule: {review_watch: monthly}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="review_watch"):
        load_config(base, env_file=tmp_path / "none.env")
    base.write_text(head + "schedule: {review_holdings: weekly}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="review_holdings"):
        load_config(base, env_file=tmp_path / "none.env")
    base.write_text(head + "schedule: {review_holdings: daily, review_watch: weekly}\n", encoding="utf-8")
    assert load_config(base, env_file=tmp_path / "none.env").schedule["review_watch"] == "weekly"
    for f in ("config.yaml", "config.example.yaml"):
        txt = (TRADER_DIR / f).read_text(encoding="utf-8")
        assert '"03-20", "05-20"' in txt and "review_holdings:" not in txt


# ---------------------------------------------------------------- M7-D5-08：失敗步驟、traceback、日曆來源
def test_daily_error_stage_and_traceback(world, monkeypatch):
    cfg, paths, conn = world
    _reg(conn, "AAA", "WATCH", {"cik": "0000000001", "auto_buy_cap": 60.0, "grade": "B"})
    _dec(conn, "AAA-20261015-1", "AAA", D1, "BUY", card=CARD)

    def boom(*a, **k):
        raise ValueError("plan_buys 壞了")
    monkeypatch.setattr(Z, "plan_buys", boom)
    s = TD._run(cfg, paths, conn, D1, FakeAlpaca(_bars()))
    assert s["error_stage"] == "buys" and "plan_buys 壞了" in s["traceback"][-1]
    summ = json.loads(conn.execute("SELECT summary FROM runs WHERE run_id=?", (f"daily-{D1}",)).fetchone()[0])
    assert summ["error_stage"] == "buys"


def test_daily_calendar_data_error_is_daily_error(world):
    from trader.data.http import DataError
    cfg, paths, conn = world
    alp = FakeAlpaca(_bars())

    def bad(*a, **k):
        raise DataError("連線逾時")
    alp.calendar = bad
    with pytest.raises(DP.DailyError, match="交易日曆取得失敗"):
        TD._run(cfg, paths, conn, D1, alp)


def test_calendar_source_recorded(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    st = {}
    s.plan(env.conn, st)
    assert st["calendar_source"] == "rules"


# ---------------------------------------------------------------- M8-D5-06：status
def test_status_lists_gave_up_retry_and_wait(tmp_path, capsys, monkeypatch):
    from trader.dashboard import launchd as L
    monkeypatch.delenv(HELD, raising=False)
    monkeypatch.setattr(L, "status", lambda labels: {k: "未安裝" for k in labels})
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00", last_screen=None)
    db.insert(env.conn, "approvals", {"ticker": "KO", "run_id": "a", "asof": "2026-10-16", "job_id": "J1",
                                      "job_type": "FULL_A", "status": "pending", "created_at": db.now_iso()})
    env.conn.commit()
    s.save_state({"start_date": "2020-01-01", "gave_up": ["watch:init"], "failures": {"daily:2020-01-02": 3},
                  "retry_at": {"daily:2020-01-02": 9e9}})
    assert cli.main(["scheduler", "status", "--config", str(tmp_path / "config.yaml"),
                     "--state-dir", str(env.paths.state)]) == 0
    out = capsys.readouterr().out
    assert "已放棄" in out and "watch:init" in out
    assert "重試等待：daily:2020-01-02 已失敗 3 次" in out and "帳本停擺" in out
    assert "[WAIT] 找不到上次初選日" in out
    assert "等下一個收盤" not in out


# ---------------------------------------------------------------- X25：鎖 pid
def test_lock_held_env_only_honoured_for_parent_pid(tmp_path, monkeypatch):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    seen = []
    monkeypatch.setattr(cli, "cmd_analyze", lambda a: seen.append(1) or 0)
    argv = ["analyze", "--asof", "2026-10-15", "--state-dir", str(env.paths.state)]
    with S.pipeline_lock(env.paths, "排程 x"):
        monkeypatch.setenv(HELD, "1")                          # 舊值／手動匯出：不能繞過鎖
        assert cli.main(argv) == 2
        monkeypatch.setenv(HELD, str(os.getppid()))            # 直接父行程（＝排程）持鎖
        assert cli.main(argv) == 0
    assert seen == [1]


def test_subprocess_runner_sets_scheduler_pid(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    code = ("import os,sys; sys.exit(0 if os.environ.get('TRADER_PIPELINE_LOCK_HELD')==str(os.getppid()) "
            f"and os.getppid()=={os.getpid()} else 3)")
    assert S.subprocess_runner(env.paths)([sys.executable, "-c", code]) == 0


@pytest.mark.parametrize("argv", [["jobs", "abandon", "--run-id", "r1"], ["jobs", "retry", "--run-id", "r1"],
                                  ["run-job", "DOSSIER", "KO", "--asof", "2026-10-15", "--fake", "x"]])
def test_abandon_and_run_job_take_lock(argv):
    args, _ = cli.build_parser().parse_known_args(argv)
    assert cli._writes_state(args) is True


# ---------------------------------------------------------------- 排程 SIGTERM：先停子行程群組再退出
SIGTERM_SCRIPT = textwrap.dedent("""
    import sys
    from trader import scheduler as S
    from trader.paths import get_paths
    paths = get_paths(state_dir=sys.argv[1]).ensure()
    code = "import os,time; open(%r,'w').write(str(os.getpid())); time.sleep(120)" % sys.argv[2]
    with S._sigterm_exits():
        S.subprocess_runner(paths)([sys.executable, "-c", code])
""")


def test_scheduler_sigterm_stops_child_group(tmp_path):
    pidf = tmp_path / "child.pid"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.Popen([sys.executable, "-B", "-c", SIGTERM_SCRIPT, str(tmp_path / "state"), str(pidf)],
                         cwd=str(TRADER_DIR), env=env)
    try:
        for _ in range(300):
            if pidf.exists() and pidf.read_text():
                break
            time.sleep(0.1)
        child = int(pidf.read_text())
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=60) == 128 + signal.SIGTERM
        for _ in range(100):                                    # 子行程（另一個行程群組）也已結束
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("子行程仍在執行（孤兒）")
    finally:
        if p.poll() is None:
            p.kill()
