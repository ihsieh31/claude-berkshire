"""CLI 測試：init、doctor（注入假的 http_get 與假模型 router）、run-job（注入假的 engine）。

全部不連網、不呼叫真的 claude；環境變數與 PROJECT_DIR 都隔離在 tmp_path。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import httpx
import pytest
import yaml

import trader.cli as cli
import trader.config as config_mod
from trader import db
from trader.llm.base import TransportError
from trader.models import LLMResult

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"
ENV_KEYS = [
    "SEC_USER_AGENT", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FRED_API_KEY",
    "PROVIDER_A_KEY", "PROVIDER_B_KEY", "TRADER_CONFIG", "TRADER_STATE_DIR",
]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.setenv(k, "")  # 先記錄原值，teardown 時還原
        monkeypatch.delenv(k)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(config_mod, "PROJECT_DIR", proj)
    return tmp_path


def _example_config(tmp: Path) -> Path:
    p = tmp / "config.yaml"
    p.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return p


def _mini_config(tmp: Path) -> Path:
    data = {
        "models": {
            "strong": {"backend": "claude_code", "model": "opus"},
            "medium": {"backend": "fake", "model": "fake"},
            "weak": {"backend": "fake", "model": "fake"},
        }
    }
    p = tmp / "mini.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


class _FakeHttp:
    """替換 cli._http_get；記錄呼叫，依 status 回應。"""

    def __init__(self, status: int = 200, exc: Exception | None = None):
        self.calls: list[tuple[str, dict | None]] = []
        self.status = status
        self.exc = exc

    def __call__(self, url, headers=None, timeout=10.0):
        self.calls.append((url, headers))
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status, request=httpx.Request("GET", url))


# ---------------------------------------------------------------- init
def test_init_succeeds_in_tmp_state_and_creates_db(isolated, capsys):
    cfg = _example_config(isolated)
    state = isolated / "state"
    rc = cli.main(["init", "--config", str(cfg), "--state-dir", str(state)])
    assert rc == 0
    db_path = state / "trader.db"
    assert db_path.exists()
    conn = db.connect(db_path)
    try:
        assert db.migrate(conn) == 2
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"jobs", "orders", "corporate_actions", "schema_version"} <= names
    finally:
        conn.close()
    out = capsys.readouterr().out
    assert "schema 版本 2" in out
    assert "WARN" in out  # 範例中的金鑰皆未設定，應只警告不失敗


def test_init_warns_on_sec_user_agent_without_at_sign(isolated, monkeypatch, capsys):
    cfg = _example_config(isolated)
    monkeypatch.setenv("SEC_USER_AGENT", "plain-name-only")
    rc = cli.main(["init", "--config", str(cfg), "--state-dir", str(isolated / "s")])
    assert rc == 0
    assert "格式應為" in capsys.readouterr().out


def test_init_missing_config_returns_1_but_still_creates_db(isolated, capsys):
    state = isolated / "state"
    rc = cli.main(["init", "--config", str(isolated / "nope.yaml"), "--state-dir", str(state)])
    assert rc == 1
    assert (state / "trader.db").exists()
    assert "cp config.example.yaml config.yaml" in capsys.readouterr().out


def test_init_invalid_config_returns_1(isolated, capsys):
    bad = isolated / "bad.yaml"
    bad.write_text(yaml.safe_dump({"models": {"strong": {"backend": "gpt"}}}), encoding="utf-8")
    rc = cli.main(["init", "--config", str(bad), "--state-dir", str(isolated / "s")])
    assert rc == 1
    assert "設定檔有誤" in capsys.readouterr().out


# ---------------------------------------------------------------- doctor
def test_doctor_skip_models_without_any_keys_all_network_skipped(isolated, monkeypatch, capsys):
    cfg = _example_config(isolated)
    fake = _FakeHttp()
    monkeypatch.setattr(cli, "_http_get", fake)
    rc = cli.main(["doctor", "--skip-models", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert fake.calls == []  # 沒有金鑰 → 完全不發請求
    for name in ("SEC", "Alpaca", "FRED"):
        assert f"[SKIP] {name}" in out
    assert "[FAIL]" not in out and "[OK]" not in out


def test_doctor_all_ok_with_keys_and_200(isolated, monkeypatch, capsys):
    cfg = _example_config(isolated)
    monkeypatch.setenv("SEC_USER_AGENT", "trader-test contact-placeholder")
    monkeypatch.setenv("ALPACA_API_KEY", "AK-test-123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK-test-456")
    monkeypatch.setenv("FRED_API_KEY", "FK-test-789")
    fake = _FakeHttp(status=200)
    monkeypatch.setattr(cli, "_http_get", fake)
    rc = cli.main(["doctor", "--skip-models", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert [c[0].split("?")[0] for c in fake.calls] == [cli.SEC_URL, cli.ALPACA_URL, cli.FRED_URL]
    assert fake.calls[0][1] == {"User-Agent": "trader-test contact-placeholder"}
    assert fake.calls[1][1]["APCA-API-KEY-ID"] == "AK-test-123"
    assert out.count("[OK]") == 3


def test_doctor_http_error_returns_1(isolated, monkeypatch, capsys):
    cfg = _example_config(isolated)
    monkeypatch.setenv("ALPACA_API_KEY", "AK-test-123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK-test-456")
    monkeypatch.setattr(cli, "_http_get", _FakeHttp(status=403))
    rc = cli.main(["doctor", "--skip-models", "--config", str(cfg)])
    assert rc == 1
    assert "[FAIL] Alpaca  HTTP 403" in capsys.readouterr().out


def test_doctor_redacts_keys_in_network_errors(isolated, monkeypatch, capsys):
    cfg = _example_config(isolated)
    monkeypatch.setenv("FRED_API_KEY", "FK-secret-value")
    err = httpx.ConnectError("cannot reach https://api.stlouisfed.org/fred/series?api_key=FK-secret-value")
    monkeypatch.setattr(cli, "_http_get", _FakeHttp(exc=err))
    rc = cli.main(["doctor", "--skip-models", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] FRED" in out
    assert "FK-secret-value" not in out


def _install_fake_router(monkeypatch, build):
    mod = types.ModuleType("trader.llm.router")
    mod.build_runner = build
    monkeypatch.setitem(sys.modules, "trader.llm.router", mod)
    return mod


class _StubRunner:
    backend = "stub"

    def __init__(self, reply):
        self.reply = reply
        self.jobs = []

    def run(self, job):
        self.jobs.append(job)
        if isinstance(self.reply, Exception):
            raise self.reply
        return LLMResult(text=self.reply, parsed_json=None, latency_s=0.5)


def test_doctor_model_check_ok_only_for_real_backends(isolated, monkeypatch, capsys):
    cfg = _mini_config(isolated)
    built = []

    def build(tier):
        built.append(tier.name)
        return _StubRunner('{"ok": true}')

    _install_fake_router(monkeypatch, build)
    monkeypatch.setattr(cli, "_http_get", _FakeHttp())
    rc = cli.main(["doctor", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert built == ["strong"]  # fake 後端不建立 runner
    assert "[OK] 模型 strong" in out
    assert "[SKIP] 模型 medium" in out and "[SKIP] 模型 weak" in out


def test_doctor_model_bad_reply_returns_1(isolated, monkeypatch, capsys):
    cfg = _mini_config(isolated)
    _install_fake_router(monkeypatch, lambda tier: _StubRunner("not json at all"))
    rc = cli.main(["doctor", "--config", str(cfg), "--timeout", "1"])
    assert rc == 1
    assert '[FAIL] 模型 strong' in capsys.readouterr().out


def test_doctor_model_transport_error_is_reported(isolated, monkeypatch, capsys):
    cfg = _mini_config(isolated)
    _install_fake_router(monkeypatch, lambda tier: _StubRunner(TransportError("連線逾時")))
    rc = cli.main(["doctor", "--config", str(cfg)])
    assert rc == 1
    assert "TransportError" in capsys.readouterr().out


def test_doctor_missing_config_returns_1(isolated, capsys):
    rc = cli.main(["doctor", "--skip-models", "--config", str(isolated / "none.yaml")])
    assert rc == 1


# ---------------------------------------------------------------- run-job
class _Recorder:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def _install_run_job_fakes(monkeypatch, failed: int = 0):
    state = {}

    class FakeRunner(_Recorder):
        pass

    class Router(_Recorder):
        pass

    class Engine:
        def __init__(self, conn, router, paths, config):
            state["engine_args"] = (conn, router, paths, config)
            self.started = None
            self.jobs = []
            state["engine"] = self

        def start_run(self, run_id, kind, asof):
            self.started = (run_id, kind, asof)

        def add_job(self, spec):
            self.jobs.append(spec)
            return True

        def run(self, run_id):
            state["ran"] = run_id
            return {"done": 1, "failed": failed, "skipped": 0, "pending": 0, "status": "done"}

    fake_mod = types.ModuleType("trader.llm.fake")
    fake_mod.FakeRunner = FakeRunner
    router_mod = types.ModuleType("trader.llm.router")
    router_mod.Router = Router
    engine_mod = types.ModuleType("trader.jobs.engine")
    engine_mod.Engine = Engine
    monkeypatch.setitem(sys.modules, "trader.llm.fake", fake_mod)
    monkeypatch.setitem(sys.modules, "trader.llm.router", router_mod)
    monkeypatch.setitem(sys.modules, "trader.jobs.engine", engine_mod)
    return state


def test_run_job_fake_flow(isolated, monkeypatch, capsys):
    cfg = _example_config(isolated)
    fixtures = isolated / "fixtures"
    fixtures.mkdir()
    state_dir = isolated / "state"
    state = _install_run_job_fakes(monkeypatch)

    rc = cli.main(["run-job", "DOSSIER", "ko", "--asof", "2026-03-01", "--fake", str(fixtures),
                   "--config", str(cfg), "--state-dir", str(state_dir)])
    assert rc == 0
    assert (state_dir / "trader.db").exists()

    engine = state["engine"]
    assert engine.started == ("manual-2026-03-01", "manual", "2026-03-01")
    (spec,) = engine.jobs
    assert spec.job_id == "manual-2026-03-01:KO:DOSSIER:1"
    assert spec.job_type == "DOSSIER" and spec.ticker == "KO" and spec.asof == "2026-03-01"
    assert state["ran"] == "manual-2026-03-01"

    conn, router, paths, config = state["engine_args"]
    assert router.kwargs["dry_run"] is True
    assert router.args[1] == paths.routing_json
    assert router.kwargs["fake"].kwargs["fixtures_dir"] == fixtures
    assert '"done": 1' in capsys.readouterr().out


def test_run_job_returns_1_when_jobs_failed(isolated, monkeypatch):
    cfg = _example_config(isolated)
    fixtures = isolated / "fixtures"
    fixtures.mkdir()
    _install_run_job_fakes(monkeypatch, failed=1)
    rc = cli.main(["run-job", "DOSSIER", "KO", "--asof", "2026-03-01", "--fake", str(fixtures),
                   "--config", str(cfg), "--state-dir", str(isolated / "state")])
    assert rc == 1


def test_run_job_without_fake_returns_2(isolated, capsys):
    cfg = _example_config(isolated)
    rc = cli.main(["run-job", "DOSSIER", "KO", "--asof", "2026-03-01", "--config", str(cfg),
                   "--state-dir", str(isolated / "state")])
    assert rc == 2
    assert "M3 才支援真實工作" in capsys.readouterr().out


def test_run_job_missing_fixture_dir_returns_1(isolated, monkeypatch):
    cfg = _example_config(isolated)
    _install_run_job_fakes(monkeypatch)
    rc = cli.main(["run-job", "DOSSIER", "KO", "--asof", "2026-03-01", "--fake", str(isolated / "nope"),
                   "--config", str(cfg), "--state-dir", str(isolated / "state")])
    assert rc == 1


def test_run_job_rejects_unknown_job_type(isolated):
    with pytest.raises(SystemExit):
        cli.main(["run-job", "NOT_A_JOB", "KO", "--asof", "2026-03-01", "--fake", "x"])


def test_no_subcommand_returns_2(capsys):
    assert cli.main([]) == 2


def test_common_args_accepted_before_subcommand(isolated):
    cfg = _example_config(isolated)
    rc = cli.main(["--config", str(cfg), "--state-dir", str(isolated / "state"), "init"])
    assert rc == 0


@pytest.mark.parametrize("mod", ["trader.jobs.review_jobs", "trader.jobs.deep", "trader.jobs.corp_jobs",
                                 "trader.jobs.hold_jobs", "trader.jobs.engine"])
def test_job_modules_import_standalone(mod):
    """單獨匯入任一工作模組不得因循環匯入失敗（新程序，避免已載入的模組掩蓋問題）。"""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-c", f"import {mod}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]


def test_jobs_retry_no_run_resets_failed_jobs(isolated, capsys):
    cfg = _example_config(isolated)
    state = isolated / "state"
    assert cli.main(["init", "--config", str(cfg), "--state-dir", str(state)]) == 0
    conn = db.connect(state / "trader.db")
    db.insert(conn, "runs", {"run_id": "r1", "kind": "analyze", "asof": "2026-10-16", "status": "failed"})
    for jid, st in (("r1:KO:DOSSIER:1", "failed"), ("r1:MSFT:DOSSIER:1", "done")):
        db.insert(conn, "jobs", {"job_id": jid, "run_id": "r1", "job_type": "DOSSIER", "ticker": jid.split(":")[1],
                                 "asof": "2026-10-16", "status": st, "error": "x" if st == "failed" else None,
                                 "created_at": "2026-10-16T00:00:00"})
    conn.close()
    rc = cli.main(["jobs", "retry", "--run-id", "r1", "--no-run", "--config", str(cfg), "--state-dir", str(state)])
    assert rc == 0 and "r1:KO:DOSSIER:1" in capsys.readouterr().out
    conn = db.connect(state / "trader.db")
    got = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT job_id, status, error FROM jobs")}
    assert got == {"r1:KO:DOSSIER:1": ("pending", None), "r1:MSFT:DOSSIER:1": ("done", None)}
    conn.close()
    assert cli.main(["jobs", "retry", "--run-id", "nope", "--config", str(cfg), "--state-dir", str(state)]) == 1
