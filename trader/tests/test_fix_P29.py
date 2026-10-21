"""P29：CLI／設定／DB 健壯性（M8-B4 其餘）。不連網、不呼叫真的 claude。"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import shlex
import sqlite3
import stat
from pathlib import Path

import pytest
import yaml

import trader.cli as cli
import trader.config as config_mod
from trader import db
from trader.config import ConfigError, load_config, load_dotenv

HEAD = {"models": {"strong": {"backend": "fake", "model": "f"}, "medium": {"backend": "fake", "model": "f"},
                   "weak": {"backend": "fake", "model": "f"}}}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for k in ("TRADER_CONFIG", "TRADER_STATE_DIR", "SEC_USER_AGENT", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
              "FRED_API_KEY", "P29_A", "P29_B", "P29_C"):
        monkeypatch.delenv(k, raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(config_mod, "PROJECT_DIR", proj)
    return tmp_path


def _write(tmp: Path, extra: dict | None = None, name: str = "c.yaml") -> Path:
    data = json.loads(json.dumps(HEAD))
    data.update(extra or {})
    p = tmp / name
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _load(p, tmp):
    return load_config(p, env_file=tmp / "none.env")


# ---------------------------------------------------------------- M8-D1-02：設定檔路徑一律絕對
def test_relative_config_path_resolved_absolute(isolated, monkeypatch):
    sub = isolated / "sub"
    sub.mkdir()
    _write(sub)
    monkeypatch.chdir(isolated)
    cfg = _load("sub/c.yaml", isolated)
    assert cfg.source_path.is_absolute() and cfg.source_path == (sub / "c.yaml").resolve()
    from trader import scheduler as S
    from trader.paths import get_paths
    argv = S._cli(cfg, get_paths(isolated / "st"), "daily")
    assert Path(argv[argv.index("--config") + 1]).is_absolute()
    monkeypatch.setenv("TRADER_CONFIG", "sub/c.yaml")
    assert config_mod.resolve_config_path().is_absolute()


# ---------------------------------------------------------------- M8-D1-08：引號值後接註解
def test_dotenv_quoted_value_with_comment(isolated):
    env = isolated / ".env"
    env.write_text('P29_A="Jane Doe jane@example.com"  # 聯絡人\nP29_B=\'abc123\' # key\nP29_C=plain # c\n',
                   encoding="utf-8")
    got = load_dotenv(env)
    assert got == {"P29_A": "Jane Doe jane@example.com", "P29_B": "abc123", "P29_C": "plain"}


# ---------------------------------------------------------------- M8-D7-07：schedule 載入時驗證
@pytest.mark.parametrize("sched, msg", [
    ("daily_close: 17:30", "請加引號"),                 # YAML 讀成 1050
    ("daily_close: '25:00'", "daily_close"),
    ("timezone: America/NewYork", "timezone"),
    ("quarterly_screen: ['02-30']", "quarterly_screen"),
    ("quarterly_screen: 03-20", "quarterly_screen"),    # 不是清單
    ("monthly_freeze: '31'", "monthly_freeze"),
    ("dialy_close: '17:30'", "未知的鍵"),
])
def test_schedule_validated_at_load(isolated, sched, msg):
    p = _write(isolated)
    p.write_text(p.read_text(encoding="utf-8") + f"schedule:\n  {sched}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=msg):
        _load(p, isolated)


def test_schedule_valid_values_accepted(isolated):
    p = _write(isolated, {"schedule": {"timezone": "America/Chicago", "daily_close": "18:05",
                                       "quarterly_screen": ["03-20"], "monthly_freeze": 3}})
    s = _load(p, isolated).schedule
    assert (s["timezone"], s["daily_close"], s["monthly_freeze"]) == ("America/Chicago", "18:05", 3)
    for f in ("config.yaml", "config.example.yaml"):   # 兩份設定檔本身仍可載入
        _load(Path(__file__).resolve().parent.parent / f, isolated)


# ---------------------------------------------------------------- M8-D9-05：universe 只接受 sp500
def test_universe_only_sp500(isolated):
    assert _load(_write(isolated), isolated).universe == "sp500"
    with pytest.raises(ConfigError, match="universe"):
        _load(_write(isolated, {"universe": "russell"}), isolated)


# ---------------------------------------------------------------- M8-D2-10：設定檔註解與 routing 分級一致
def test_tier_comments_match_routing():
    root = Path(__file__).resolve().parent.parent
    routing = json.loads((root.parent / "routing" / "model_routing.json").read_text(encoding="utf-8"))["jobs"]
    assert (routing["FULL_A"]["model"], routing["RED_TEAM"]["model"], routing["FULL_B"]["model"]) == \
        ("strong", "strong", "medium")
    for f in ("config.yaml", "config.example.yaml"):
        txt = (root / f).read_text(encoding="utf-8")
        assert "強：FULL_B" not in txt and "強：FULL_A、RED_TEAM" in txt and "hark_queue" in txt


# ---------------------------------------------------------------- M8-D1-07：沒有 launchctl 不例外
def test_launchctl_missing_no_traceback(isolated, monkeypatch, capsys):
    from trader.dashboard import launchd as L
    monkeypatch.setattr(L.shutil, "which", lambda name: None)
    assert L._launchctl("print", "x").returncode == 127
    st = L.status()
    assert set(st.values()) == {"未載入"}
    monkeypatch.setattr(L, "AGENTS", isolated / "agents")
    assert L.uninstall() == []


# ---------------------------------------------------------------- M8-D2-01：doctor 沙箱在隔離根下
def test_doctor_claude_code_sandbox_under_isolated_root(isolated, monkeypatch, capsys):
    from trader.jobs import isolation
    monkeypatch.setattr(isolation, "_SYSTEM_DENY", [])   # 測試用 tmp 可能在 /tmp 或 /private/var 下
    mark = isolated / "cwd.txt"
    claude = isolated / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\ncat >/dev/null\npwd > %s\necho '{\"result\": \"{\\\"ok\\\": true}\"}'\n" % mark,
                      encoding="utf-8")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    sb = isolated / "sbroot"
    p = _write(isolated, {"models": {**HEAD["models"], "strong": {"backend": "claude_code", "model": "opus",
                                                                  "claude_bin": str(claude)}},
                          "sandbox": {"root": str(sb)}})
    monkeypatch.setattr(cli, "_http_get", lambda *a, **k: type("R", (), {"status_code": 200})())
    rc = cli.main(["doctor", "--config", str(p), "--state-dir", str(isolated / "st")])
    out = capsys.readouterr().out
    assert "[OK] 模型 strong" in out, out
    used = Path(mark.read_text().strip()).resolve()
    assert sb.resolve() in used.parents and not used.exists()   # 在隔離根下，用完刪除
    assert rc == 0


def test_doctor_reports_isolation_error_as_fail(isolated, monkeypatch, capsys):
    from trader.jobs import isolation

    def boom():
        raise isolation.IsolationError("此平台沒有預設沙箱根目錄")
    monkeypatch.setattr(isolation, "default_sandbox_root", boom)
    p = _write(isolated, {"models": {**HEAD["models"], "strong": {"backend": "claude_code", "model": "opus"}}})
    monkeypatch.setattr(cli, "_http_get", lambda *a, **k: type("R", (), {"status_code": 200})())
    assert cli.main(["doctor", "--config", str(p), "--state-dir", str(isolated / "st")]) == 1
    assert "[FAIL] 模型 strong" in capsys.readouterr().out


# ---------------------------------------------------------------- M8-D7-03：print --cron
def test_print_cron_line(isolated, monkeypatch, capsys):
    st = isolated / "my state%"
    p = _write(isolated)
    assert cli.main(["scheduler", "print", "--cron", "--config", str(p), "--state-dir", str(st)]) == 0
    line = capsys.readouterr().out.strip()
    assert (st / "logs").is_dir()
    toks = shlex.split(line.replace("\\%", "%"))
    assert toks[:5] == ["*/10", "*", "*", "*", "*"] and "PATH=" in line
    assert any(t.startswith("PATH=") for t in toks)
    assert str(st.resolve()) in toks and "%" not in line.replace("\\%", "")
    try:
        import certifi  # noqa: F401
        assert any(t.startswith("SSL_CERT_FILE=") for t in toks)
    except ImportError:
        pass


# ---------------------------------------------------------------- M8-D4-04：doctor --state
def _state(tmp: Path) -> Path:
    st = tmp / "state"
    conn = db.connect(st / "trader.db")
    db.migrate(conn)
    conn.close()
    (st / "scheduler.json").write_text(json.dumps({"last_screen": "2026-08-20", "gave_up": []}), encoding="utf-8")
    return st


def test_doctor_state_pending_job_fails_then_passes(isolated, capsys):
    st = _state(isolated)
    conn = db.connect(st / "trader.db")
    db.insert(conn, "runs", {"run_id": "r1", "kind": "analyze", "asof": "2026-10-01", "status": "running",
                             "started_at": db.now_iso()})
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    row = {"job_id": "j1", "run_id": "r1", "job_type": "DOSSIER", "ticker": "AAA", "status": "pending",
           "asof": "2026-10-01", "created_at": db.now_iso(), "updated_at": db.now_iso(), "attempts": 0}
    db.insert(conn, "jobs", {k: v for k, v in row.items() if k in cols})
    assert cli.main(["doctor", "--state", "--state-dir", str(st)]) == 1   # 不需設定檔
    assert "[FAIL] 待執行工作" in capsys.readouterr().out
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()
    assert cli.main(["doctor", "--state", "--state-dir", str(st)]) == 0
    (st / "scheduler.json").write_text(json.dumps({"gave_up": ["daily:2026-10-01"]}), encoding="utf-8")
    assert cli.main(["doctor", "--state", "--state-dir", str(st)]) == 1
    out = capsys.readouterr().out
    assert "[WARN] scheduler.json last_screen" in out and "[FAIL] scheduler.json gave_up" in out


def test_doctor_state_missing_db(isolated, capsys):
    assert cli.main(["doctor", "--state", "--state-dir", str(isolated / "nope")]) == 1
    assert not (isolated / "nope").exists()   # 唯讀：不建立 state


# ---------------------------------------------------------------- M8-D1-06／M8-D9-07：migrate
def _mig(p, q):
    try:
        q.put(("ok", db.migrate(db.connect(Path(p)))))
    except Exception as e:  # noqa: BLE001
        q.put((type(e).__name__, str(e)))


def test_migrate_concurrent_new_db(isolated):
    ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context()
    for i in range(6):
        p = isolated / f"r{i}.db"
        q = ctx.Queue()
        ps = [ctx.Process(target=_mig, args=(str(p), q)) for _ in range(3)]
        [x.start() for x in ps]
        [x.join(30) for x in ps]
        res = [q.get(timeout=5) for _ in ps]
        assert all(r == ("ok", len(db.MIGRATIONS)) for r in res), res
        conn = sqlite3.connect(p)
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")] == \
            list(range(1, len(db.MIGRATIONS) + 1))
        conn.close()


def test_migrate_newer_db_raises(isolated):
    conn = db.connect(isolated / "n.db")
    n = db.migrate(conn)
    assert db.migrate(conn) == n   # 冪等
    conn.execute("INSERT INTO schema_version VALUES (?, ?)", (n + 1, db.now_iso()))
    conn.commit()
    with pytest.raises(RuntimeError, match="比程式新"):
        db.migrate(conn)


def test_migrate_from_v2_to_current(isolated):
    """與 P36 的 v3 銜接：v2 資料庫升級只套 v3，資料保留。"""
    conn = db.connect(isolated / "v2.db")
    conn.executescript("BEGIN;\n" + db.MIGRATIONS[0] + db.MIGRATIONS[1] +
                       "\nINSERT INTO schema_version VALUES (1,'x');INSERT INTO schema_version VALUES (2,'x');COMMIT;")
    assert db.migrate(conn) == len(db.MIGRATIONS)
    assert "provenance" in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
