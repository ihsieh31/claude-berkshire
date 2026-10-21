"""P22：所有寫 state 的指令共用 state/.pipeline.lock（D10、M8-D1-01、M7-D7-04）。不連網、不呼叫模型。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trader import cli, db
from trader import scheduler as S
from trader.paths import get_paths
from trader.pipelines import approved as AP
from trader.portfolio import sizing as Z

from test_daily_pipeline import world  # noqa: F401 - fixture

TRADER_DIR = Path(__file__).resolve().parents[1]
HELD = "TRADER_PIPELINE_LOCK_HELD"

WRITE_ARGVS = [
    ["analyze", "--asof", "2026-10-15"],
    ["review", "--asof", "2026-10-15"],
    ["daily", "--date", "2026-10-15"],
    ["bootstrap", "--asof", "2026-10-15"],
    ["screen", "--asof", "2026-10-15"],
    ["jobs", "retry", "--run-id", "r1"],
    ["run-approved"],
    ["ledger", "adjust", "--cash", "1", "--note", "x"],
    ["approvals", "approve", "1", "--run"],
    ["jobs", "abandon", "--run-id", "r1"],          # X25（P23）：abandon 也取鎖
]


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.delenv(HELD, raising=False)
    p = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(p.db)
    db.migrate(conn)
    conn.close()
    return p


def _argv(a, paths):
    return [*a, "--state-dir", str(paths.state)]


@pytest.mark.parametrize("argv", WRITE_ARGVS, ids=lambda a: " ".join(a[:2]))
def test_write_commands_exit_2_when_lock_held(paths, argv, capsys):
    with S.pipeline_lock(paths, "排程 daily:2026-10-15"):
        assert cli.main(_argv(argv, paths)) == 2
    out = capsys.readouterr().out
    assert "[WAIT]" in out and "排程 daily:2026-10-15" in out   # 印出持鎖指令


def test_analyze_blocked_leaves_jobs_untouched(paths):
    """M8-D1-01：另一行程執行中（running）時手動 analyze 不得進引擎把它改回 pending。"""
    conn = db.connect(paths.db)
    db.insert(conn, "runs", {"run_id": "analyze-2026-10-15", "kind": "analyze", "asof": "2026-10-15",
                             "status": "running", "started_at": db.now_iso()})
    db.insert(conn, "jobs", {"job_id": "j1", "run_id": "analyze-2026-10-15", "job_type": "FULL_A", "ticker": "KO",
                             "asof": "2026-10-15", "status": "running", "created_at": db.now_iso()})
    with S.pipeline_lock(paths, "trader run-approved"):
        assert cli.main(_argv(["analyze", "--asof", "2026-10-15"], paths)) == 2
    assert conn.execute("SELECT status FROM jobs WHERE job_id='j1'").fetchone()[0] == "running"
    conn.close()


@pytest.mark.parametrize("argv", [["daily", "--date", "2026-10-15", "--dry-run"], ["ledger", "show"],
                                  ["approvals", "list"], ["approvals", "approve", "1"],
                                  ["scheduler", "status"]])
def test_read_only_commands_do_not_take_lock(argv):
    args, _ = cli.build_parser().parse_known_args(argv)
    assert cli._writes_state(args) is False


def test_lock_held_env_skips_lock_and_lock_is_held_during_command(paths, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_analyze", lambda a: seen.append(S.pipeline_holder(paths)) or 0)
    # 未持鎖：指令執行期間由本行程持鎖，持鎖者為此指令
    assert cli.main(_argv(["analyze", "--asof", "2026-10-15"], paths)) == 0
    assert seen[-1].startswith("trader analyze --asof 2026-10-15") and f"pid {os.getpid()}" in seen[-1]
    assert S.pipeline_holder(paths) is None                     # 結束即釋放
    # 排程父行程持鎖、子行程帶 TRADER_PIPELINE_LOCK_HELD=<父行程 pid>：照常執行、不自鎖（X25）
    monkeypatch.setenv(HELD, str(os.getppid()))
    with S.pipeline_lock(paths, "排程 daily:2026-10-15"):
        assert cli.main(_argv(["analyze", "--asof", "2026-10-15"], paths)) == 0
    assert len(seen) == 2


def test_lock_released_when_command_raises(paths, monkeypatch):
    def boom(a):
        raise RuntimeError("x")
    monkeypatch.setattr(cli, "cmd_review", boom)
    with pytest.raises(RuntimeError):
        cli.main(_argv(["review", "--asof", "2026-10-15"], paths))
    assert S.pipeline_holder(paths) is None


def test_subprocess_runner_marks_child_lock_held(paths):
    code = "import os,sys; sys.exit(0 if os.environ.get('TRADER_PIPELINE_LOCK_HELD')==str(os.getppid()) else 3)"
    assert S.subprocess_runner(paths)([sys.executable, "-c", code]) == 0


def _cli_proc(paths, env_extra, *args):
    env = {k: v for k, v in os.environ.items() if k != HELD}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", **env_extra})
    return subprocess.run([sys.executable, "-B", "-m", "trader.cli", *args, "--state-dir", str(paths.state)],
                          cwd=str(TRADER_DIR), env=env, capture_output=True, text=True, timeout=60)


def test_real_cli_subprocess_blocked_unless_lock_held_env(paths):
    """跨行程：排程（父）持鎖時，手動 ledger adjust 回 2（含匯出舊值 HELD=1）；排程子行程（HELD=父 pid）照常寫入。"""
    adj = ("ledger", "adjust", "--date", "2026-10-15", "--cash", "5", "--note", "P22")
    with S.pipeline_lock(paths, "排程 daily:2026-10-15"):
        r = _cli_proc(paths, {}, *adj)
        assert r.returncode == 2 and "排程 daily:2026-10-15" in r.stdout, r.stdout + r.stderr
        r = _cli_proc(paths, {HELD: "1"}, *adj)
        assert r.returncode == 2, r.stdout + r.stderr            # X25：不是父行程 pid 不能繞過鎖
        r = _cli_proc(paths, {HELD: str(os.getpid())}, *adj)    # 本測試行程＝子行程的直接父行程（持鎖者）
        assert r.returncode == 0, r.stdout + r.stderr
    conn = db.connect(paths.db)
    n = conn.execute("SELECT COUNT(*) FROM cash_ledger WHERE amount=5").fetchone()[0]
    conn.close()
    assert n == 1


HOLDER = textwrap.dedent("""
    import subprocess, sys, time
    from trader import scheduler as S
    from trader.paths import get_paths
    paths = get_paths(state_dir=sys.argv[1])
    with S.pipeline_lock(paths, "holder"):
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        print(child.pid, flush=True)
        time.sleep(60)
""")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_lock_released_when_holder_killed_and_not_inherited_by_child(paths, sig):
    """與 P31 的 SIGTERM／killpg 互動：持鎖行程被中斷即釋放；它另開群組的子行程（如 claude）不繼承鎖。"""
    p = subprocess.Popen([sys.executable, "-B", "-c", HOLDER, str(paths.state)], cwd=str(TRADER_DIR),
                         stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    child = int(p.stdout.readline())
    try:
        assert S.pipeline_holder(paths).startswith("holder")
        os.kill(p.pid, sig)
        p.wait(timeout=10)
        os.kill(child, 0)                                 # 子行程仍活著
        assert S.pipeline_holder(paths) is None           # 但鎖已釋放
        with S.pipeline_lock(paths, "next", wait=False):
            pass
    finally:
        for pid in (p.pid, child):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        p.wait(timeout=10)


def test_run_approved_does_not_self_lock_under_cli_lock(paths, monkeypatch):
    """修前 run_approved 內部再以阻塞方式取 .pipeline.lock：CLI 已持鎖時同一行程會永久卡住。"""
    monkeypatch.setattr(AP, "_run", lambda *a, **k: {"runs": {}})
    res = []
    with S.pipeline_lock(paths):
        t = threading.Thread(target=lambda: res.append(AP.run_approved(None, paths, None)), daemon=True)
        t.start()
        t.join(timeout=5)
    assert res == [{"runs": {}}]


def test_dashboard_does_not_spawn_when_pipeline_locked(tmp_path):
    from test_approvals import _env, _run_to_gate
    from trader.dashboard.server import TOKEN_FILE, create_app
    env = _env(tmp_path)
    _run_to_gate(env)
    calls = []
    client = TestClient(create_app(env.paths, 8787, spawn=lambda: calls.append(1)), base_url="http://127.0.0.1:8787")
    tok = (env.paths.state / TOKEN_FILE).read_text().strip()
    h = {"host": "127.0.0.1:8787", "content-type": "application/json", "x-dashboard-token": tok}
    with S.pipeline_lock(env.paths, "排程 daily:2026-10-15"):
        r = client.post("/api/approvals/approve", content=json.dumps({"ids": [1]}), headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] == [1] and body["started"] is False and "排程 daily:2026-10-15" in body["note"]
    assert calls == []


def test_decision_written_after_daily_read_is_loaded_next_daily(world, monkeypatch):
    """M7-D7-04：daily 讀完決策後、finished_at 前寫入的決策，下一次 daily 以 read_at 為界仍讀得到並下單。"""
    import test_daily_pipeline as T
    cfg, p, conn = world
    clock = iter(range(10**6))   # 每次取時戳前進 1 秒：模擬 daily 跑數分鐘、讀決策與 finished_at 不同秒
    monkeypatch.setattr(db, "now_iso", lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                             time.gmtime(1_792_000_000 + next(clock))))
    T._reg(conn, "AAA", "WATCH", {"cik": "1", "auto_buy_cap": 60.0, "grade": "B"})
    orig = Z.plan_tranches

    def inject(*a, **k):    # 決策日 D1、在 daily D1 讀完決策之後才寫入（例：同時完成的分析／儀表板動作）
        T._dec(conn, "AAA-20261015-9", "AAA", T.D1, "BUY", card=T.CARD)
        return orig(*a, **k)
    monkeypatch.setattr(Z, "plan_tranches", inject)
    alp = T.FakeAlpaca(T._bars())
    s1 = T._run(cfg, p, conn, T.D1, alp)
    monkeypatch.setattr(Z, "plan_tranches", orig)
    assert "error" not in s1 and s1["orders"] == []
    s2 = T._run(cfg, p, conn, T.D2, alp)
    assert "error" not in s2, s2
    assert [o["ticker"] for o in s2["orders"]] == ["AAA"]
    assert s1["read_at"] < s1_finished(conn, T.D1)          # 讀決策時刻早於完成時刻，摘要有記


def s1_finished(conn, d):
    return conn.execute("SELECT finished_at FROM runs WHERE run_id=?", (f"daily-{d}",)).fetchone()[0]
