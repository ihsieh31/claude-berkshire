"""M7 常駐排程：補跑錯過的交易日、每月凍結、觀察名單、每季初選、月報、失敗重試與放棄、沒有期限。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from test_engine import make_env

from trader import db
from trader.calendar import TradingCalendar
from trader.data.pe_median import freeze_path
from trader.scheduler import MAX_FAILURES, Scheduler

NY = ZoneInfo("America/New_York")


class Clock:
    def __init__(self, s):
        self.t = datetime.fromisoformat(s).replace(tzinfo=NY)

    def __call__(self):
        return self.t

    def set(self, s):
        self.t = datetime.fromisoformat(s).replace(tzinfo=NY)


class FakeRunner:
    """模擬子行程：daily 寫 runs 與淨值、fetch --freeze-pe／bootstrap 寫凍結檔。"""

    def __init__(self, env, fail=()):
        self.env, self.fail, self.calls = env, set(fail), []

    def __call__(self, argv):
        args = argv[3:argv.index("--state-dir")]
        self.calls.append(" ".join(args))
        if args[0] in self.fail:
            return 1
        if args[0] == "daily":
            d = args[2]
            db.insert(self.env.conn, "runs", {"run_id": f"daily-{d}", "kind": "daily_close", "asof": d, "status": "done",
                                              "started_at": db.now_iso(), "finished_at": db.now_iso()})
            db.insert(self.env.conn, "nav_daily", {"date": d, "nav": 100000.0, "cash": 100000.0, "positions_value": 0.0,
                                                   "n_positions": 0, "data": "{}"})
            self.env.conn.commit()
        if args[0] in ("fetch", "bootstrap"):                     # 初選也會凍結當月 P/E 中位
            p = freeze_path(self.env.paths, args[2][:7])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
        return 0


def _sched(tmp_path, now, fail=(), bootstrapped=True):
    env = make_env(tmp_path, {})
    if bootstrapped:
        db.insert(env.conn, "registry", {"ticker": "KO", "status": "WATCH", "data": "{}", "updated_at": db.now_iso()})
        env.conn.commit()
    clock = Clock(now)
    run = FakeRunner(env, fail)
    s = Scheduler(env.config, env.paths, runner=run, now=clock, calendar=lambda a, b: TradingCalendar(source="rules"),
                  log=lambda m: None)
    return env, s, clock, run


def test_refuses_before_bootstrap(tmp_path):
    import pytest
    from trader.scheduler import NotBootstrapped
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00", bootstrapped=False)
    with pytest.raises(NotBootstrapped):
        s.once()
    assert run.calls == []
    db.insert(env.conn, "registry", {"ticker": "KO", "status": "WATCH", "data": "{}", "updated_at": db.now_iso()})
    db.insert(env.conn, "approvals", {"ticker": "KO", "run_id": "analyze-2026-10-16", "asof": "2026-10-16",
                                      "job_id": "J1", "job_type": "FULL_A", "status": "pending", "created_at": db.now_iso()})
    env.conn.commit()
    with pytest.raises(NotBootstrapped, match="待同意"):
        s.check_ready()          # 安裝前提（strict）仍擋待同意
    s.check_ready(strict=False)  # 執行中只擋登錄表為空


def test_pending_approval_does_not_halt_daily_but_defers_watch_and_screen(tmp_path):
    """H1：有待同意時 daily／report／freeze 照跑，只暫緩 watch 與季度初選；解除後補上。"""
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    db.insert(env.conn, "approvals", {"ticker": "KO", "run_id": "analyze-2026-10-16", "asof": "2026-10-16",
                                      "job_id": "J1", "job_type": "FULL_A", "status": "pending", "created_at": db.now_iso()})
    env.conn.commit()
    done = s.once()
    assert "daily:2026-10-16" in done
    assert not any(c.startswith("bootstrap") or c.startswith("review") for c in run.calls)
    clock.set("2026-11-20T17:45")                                  # 季度初選日
    run.calls.clear()
    s.once()
    assert any(c.startswith("daily") for c in run.calls)
    assert any(c.startswith("fetch") and "--freeze-pe" in c for c in run.calls)
    assert not any(c.startswith("bootstrap") or c.startswith("review") for c in run.calls)
    env.conn.execute("UPDATE approvals SET status='rejected'")
    env.conn.commit()
    run.calls.clear()
    s.once()
    assert "bootstrap --asof 2026-11-20" in run.calls


def test_first_start_waits_for_close_then_bootstraps(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T10:00")   # 週五盤中
    assert s.once() == []                                          # 收盤前、前一交易日早於啟動日 → 不補
    assert s.load_state()["start_date"] == "2026-10-16"
    clock.set("2026-10-16T17:31")
    s.once()
    assert run.calls == ["daily --date 2026-10-16", "review --scope watch --asof 2026-10-16", "bootstrap --asof 2026-10-16"]
    st = s.load_state()
    assert st["last_screen"] == st["last_watch_review"] == "2026-10-16"
    assert s.once() == []                                          # 同一天不重做


def test_catch_up_freeze_watch_quarter_and_report(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    s.once()
    run.calls.clear()
    clock.set("2026-11-04T09:00")                                  # 睡了兩週多（中間有週末）
    s.once()
    dailies = [c for c in run.calls if c.startswith("daily")]
    assert dailies[0] == "daily --date 2026-10-19" and dailies[-1] == "daily --date 2026-11-03"
    assert len(dailies) == 12
    assert run.calls[0] == "fetch --asof 2026-11-03 --freeze-pe"  # 11 月凍結先做
    assert "review --scope watch --asof 2026-11-03" in run.calls    # 距上次 ≥ 14 天
    assert not any(c.startswith("bootstrap") for c in run.calls)   # 11-20 還沒到
    assert any(c.startswith("validate-report") for c in run.calls) # 進入 11 月 → 月報
    run.calls.clear()
    clock.set("2026-11-20T17:45")
    s.once()
    assert "bootstrap --asof 2026-11-20" in run.calls              # 每季初選
    assert not any(c.startswith("fetch") for c in run.calls)


def test_no_end_date(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    s.once()
    clock.set("2027-02-01T17:40")                                  # 超過 3 個月照跑
    s.once()
    assert "daily --date 2027-02-01" in run.calls


def test_retry_then_give_up_records_anomaly(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00", fail={"review"})
    s.once()
    assert s.load_state()["failures"]["watch:2026-10-16"] == 1
    assert "bootstrap --asof 2026-10-16" in run.calls             # 其他工作照做
    n = len(run.calls)
    s.once()
    assert len(run.calls) == n                                    # 30 分鐘內不重試
    for k in range(MAX_FAILURES - 1):
        clock.set(f"2026-10-16T{19 + k}:00")
        s.once()
    st = s.load_state()
    assert "watch:2026-10-16" in st["gave_up"]
    a = env.conn.execute("SELECT kind, pause_trading FROM anomalies").fetchall()
    assert [tuple(r) for r in a] == [("scheduler_task_failed", 0)]


def test_daily_exit_zero_but_not_done_counts_as_failure(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    run.__class__ = type("NoWrite", (FakeRunner,), {"__call__": lambda self, argv: (self.calls.append(argv[3]), 0)[1]})
    s.once()
    assert s.load_state()["failures"]["daily:2026-10-16"] == 1


def test_failed_daily_blocks_later_days(tmp_path):
    """前一天 daily 失敗、在重試等待中 → 後面的交易日不能先跑（淨值與撮合依帳本順序）。"""
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    s.once()
    clock.set("2026-10-21T18:00")
    orig = run.__class__.__call__
    first = {"n": 0}

    def flaky(self, argv):
        if argv[3] == "daily" and argv[5] == "2026-10-19" and first["n"] == 0:
            first["n"] += 1
            self.calls.append("daily --date 2026-10-19 (fail)")
            return 1
        return orig(self, argv)
    run.__class__ = type("Flaky", (FakeRunner,), {"__call__": flaky})
    run.calls.clear()
    s.once()
    assert [c for c in run.calls if c.startswith("daily")] == ["daily --date 2026-10-19 (fail)"]
    clock.set("2026-10-21T18:40")                                  # 重試時間到 → 依序補完
    run.calls.clear()
    s.once()
    assert [c for c in run.calls if c.startswith("daily")] == [
        "daily --date 2026-10-19", "daily --date 2026-10-20", "daily --date 2026-10-21"]


# ---------------------------------------------------------------- bootstrap
def test_bootstrap_screen_then_analyze(tmp_path, monkeypatch):
    from trader.pipelines import bootstrap as B
    env = make_env(tmp_path, {})
    seen = {}
    monkeypatch.setattr(B, "run_screen", lambda cfg, paths, conn, asof, top, kind, clients: {
        "run_id": f"screen-{asof}", "shortlist": ["AAA", "BBB"], "to_analyze": [{"ticker": "AAA"}], "skipped_recent": [{"ticker": "BBB"}]})
    monkeypatch.setattr(B, "run_analyze", lambda cfg, paths, conn, asof, tickers, screen_asof, clients, router:
                        seen.setdefault("a", (tickers, screen_asof)) and {"engine": {"failed": 0}})
    r = B.run_bootstrap(env.config, env.paths, env.conn, "2026-10-16")
    assert seen["a"] == (["AAA"], "2026-10-16") and r["tickers"] == ["AAA"]
    seen.clear()
    r = B.run_bootstrap(env.config, env.paths, env.conn, "2026-10-16", dry_run=True)
    assert "a" not in seen and r["tickers"] == ["AAA"]
    monkeypatch.setattr(B, "run_screen", lambda *a, **k: {"error": "SEC 失敗"})
    assert "error" in B.run_bootstrap(env.config, env.paths, env.conn, "2026-10-16")
