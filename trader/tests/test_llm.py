"""M0 模型層測試。不連網、不呼叫真的 claude；金鑰在執行期隨機產生，不寫死在程式中。"""
from __future__ import annotations

import json
import secrets
import subprocess
import types
from pathlib import Path

import httpx
import pytest

from trader.llm.base import (
    FatalLLMError,
    LLMError,
    TransportError,
    call_with_retry,
    extract_json,
    materialize_files,
    write_raw,
)
from trader.jobs import isolation
from trader.llm.claude_code import ClaudeCodeRunner
from trader.llm.fake import FakeRunner
from trader.llm.openai_compat import OpenAICompatRunner
from trader.llm.router import Router, build_runner
from trader.models import JobSpec, LLMResult, TierConfig


# ---------- 共用輔助 ----------

def make_job(tmp_path: Path, job_type: str = "DOSSIER", ticker: str = "AAPL", attempt: int = 0,
             sandbox: bool = True, prompt: str = "提示文字", params: dict | None = None) -> JobSpec:
    sb = tmp_path / "sandbox"
    if sandbox:
        sb.mkdir(parents=True, exist_ok=True)
    return JobSpec(
        job_id=f"run1:{ticker}:{job_type}:1",
        run_id="run1",
        job_type=job_type,
        ticker=ticker,
        asof="2026-01-02",
        params=params or {},
        sandbox=sb if sandbox else None,
        log_dir=tmp_path / "logs",
        prompt_text=prompt,
        attempt=attempt,
    )


# ---------- extract_json ----------

def test_extract_pure_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_fenced_json():
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_extract_json_with_surrounding_noise():
    text = '好的，結果如下：\n{"a": 3, "b": {"c": 1}}\n以上，謝謝。'
    assert extract_json(text) == {"a": 3, "b": {"c": 1}}


def test_extract_returns_none_when_not_an_object():
    assert extract_json("") is None
    assert extract_json("[1, 2]") is None
    assert extract_json("沒有任何 JSON") is None


# ---------- materialize_files ----------

def test_materialize_writes_files_and_replaces_list(tmp_path):
    sb = tmp_path / "sb"
    sb.mkdir()
    parsed = {"ok": True, "files": {"out/a.md": "# 標題", "out/b.json": {"x": 1}}}
    names = materialize_files(parsed, sb)
    assert names == ["out/a.md", "out/b.json"]
    assert parsed["files"] == names
    assert (sb / "out" / "a.md").read_text(encoding="utf-8") == "# 標題"
    assert json.loads((sb / "out" / "b.json").read_text(encoding="utf-8")) == {"x": 1}


@pytest.mark.parametrize("bad", ["../x", "/abs/x", "out/../../x"])
def test_materialize_rejects_unsafe_names(tmp_path, bad):
    sb = tmp_path / "sb"
    sb.mkdir()
    with pytest.raises(ValueError):
        materialize_files({"files": {bad: "內容"}}, sb)
    assert not (tmp_path / "x").exists()


def test_materialize_without_files_is_noop():
    assert materialize_files({"a": 1}, None) == []
    assert materialize_files(None, None) == []


# ---------- call_with_retry ----------

class ScriptedRunner:
    """依序回傳或拋出預先設定的結果。"""

    backend = "fake"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(self, job):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


def _ok() -> LLMResult:
    return LLMResult(text="{}", parsed_json={})


def test_retry_transport_error_then_success(tmp_path):
    # D47：逾時最多重試 1 次（退避 base_delay）；非額度類的其他傳輸錯誤不重試
    from trader.llm.base import LLMTimeout
    sleeps: list[float] = []
    runner = ScriptedRunner([LLMTimeout("逾時"), _ok()])
    res = call_with_retry(runner, make_job(tmp_path, sandbox=False), retries=3, base_delay=1.5, sleep=sleeps.append)
    assert res.text == "{}"
    assert runner.calls == 2
    assert sleeps == [1.5]
    runner = ScriptedRunner([TransportError("斷線"), _ok()])
    with pytest.raises(TransportError):
        call_with_retry(runner, make_job(tmp_path, sandbox=False), retries=3, base_delay=1.5, sleep=sleeps.append)
    assert runner.calls == 1


def test_quota_errors_back_off_longer(tmp_path):
    sleeps: list[float] = []
    runner = ScriptedRunner([TransportError("claude 結束碼 1：usage limit reached"), TransportError("usage limit reached"), _ok()])
    call_with_retry(runner, make_job(tmp_path, sandbox=False), retries=3, sleep=sleeps.append)
    assert sleeps == [60.0, 120.0]


def test_retry_after_is_used_for_sleep(tmp_path):
    sleeps: list[float] = []
    runner = ScriptedRunner([TransportError("429", retry_after=7.0), _ok()])
    call_with_retry(runner, make_job(tmp_path, sandbox=False), retries=2, base_delay=1.0, sleep=sleeps.append)
    assert sleeps == [7.0]


def test_fatal_error_is_not_retried(tmp_path):
    sleeps: list[float] = []
    runner = ScriptedRunner([FatalLLMError("金鑰錯誤"), _ok()])
    with pytest.raises(FatalLLMError):
        call_with_retry(runner, make_job(tmp_path, sandbox=False), retries=3, sleep=sleeps.append)
    assert runner.calls == 1
    assert sleeps == []


def test_transport_error_raised_after_exhaustion(tmp_path):
    sleeps: list[float] = []
    runner = ScriptedRunner([TransportError("rate limit a"), TransportError("rate limit b"), TransportError("rate limit c")])
    with pytest.raises(TransportError):   # 額度類錯誤才會用完 retries（D47）
        call_with_retry(runner, make_job(tmp_path, sandbox=False), retries=2, sleep=sleeps.append)
    assert runner.calls == 3
    assert len(sleeps) == 2


# ---------- FakeRunner ----------

def test_fake_lookup_order(tmp_path):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "TRIAGE__AAPL.json").write_text('{"src": "fixture-tk"}', encoding="utf-8")
    (fixtures / "TRIAGE.json").write_text('{"src": "fixture-jt"}', encoding="utf-8")
    runner = FakeRunner(
        responses={
            ("DOSSIER", "AAPL"): {"src": "resp-tk"},
            "DOSSIER": {"src": "resp-jt"},
            "TRIAGE": {"src": "resp-triage"},
            "*": {"src": "star"},
        },
        fixtures_dir=fixtures,
    )
    # 1) (job_type, ticker) 優先於 job_type
    assert runner.run(make_job(tmp_path, "DOSSIER", "AAPL")).parsed_json == {"src": "resp-tk"}
    # 2) job_type 次之
    assert runner.run(make_job(tmp_path, "DOSSIER", "MSFT")).parsed_json == {"src": "resp-jt"}
    # 3) responses 的 job_type 仍優先於 fixtures
    assert runner.run(make_job(tmp_path, "TRIAGE", "AAPL")).parsed_json == {"src": "resp-triage"}


def test_fake_fixtures_then_star(tmp_path):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "TRIAGE__AAPL.json").write_text('{"src": "fixture-tk"}', encoding="utf-8")
    (fixtures / "THESIS.json").write_text('{"src": "fixture-jt"}', encoding="utf-8")
    runner = FakeRunner(responses={"*": {"src": "star"}}, fixtures_dir=fixtures)
    assert runner.run(make_job(tmp_path, "TRIAGE", "AAPL")).parsed_json == {"src": "fixture-tk"}
    assert runner.run(make_job(tmp_path, "THESIS", "MSFT")).parsed_json == {"src": "fixture-jt"}
    assert runner.run(make_job(tmp_path, "PRICE", "MSFT")).parsed_json == {"src": "star"}


def test_fake_missing_response_is_fatal(tmp_path):
    runner = FakeRunner(responses={"DOSSIER": {}})
    with pytest.raises(FatalLLMError):
        runner.run(make_job(tmp_path, "PRICE", "AAPL"))


def test_fake_list_indexed_by_attempt(tmp_path):
    runner = FakeRunner(responses={"DOSSIER": [{"n": 0}, {"n": 1}]})
    assert runner.run(make_job(tmp_path, attempt=0)).parsed_json == {"n": 0}
    assert runner.run(make_job(tmp_path, attempt=1)).parsed_json == {"n": 1}
    assert runner.run(make_job(tmp_path, attempt=5)).parsed_json == {"n": 1}  # 超過取最後一個


def test_fake_callable_receives_job(tmp_path):
    runner = FakeRunner(responses={"DOSSIER": lambda job: {"ticker_seen": job.ticker}})
    assert runner.run(make_job(tmp_path, ticker="NVDA")).parsed_json == {"ticker_seen": "NVDA"}


def test_fake_raises_transport_error_value(tmp_path):
    runner = FakeRunner(responses={"DOSSIER": TransportError("測試用斷線")})
    with pytest.raises(TransportError):
        runner.run(make_job(tmp_path))


def test_fake_writes_files_into_sandbox(tmp_path):
    runner = FakeRunner(responses={"DOSSIER": {"files": {"out/d.json": {"k": 1}}}})
    job = make_job(tmp_path)
    res = runner.run(job)
    assert res.files_written == ["out/d.json"]
    assert res.parsed_json["files"] == ["out/d.json"]
    assert json.loads((job.sandbox / "out" / "d.json").read_text(encoding="utf-8")) == {"k": 1}


def test_fake_records_calls_and_tokens(tmp_path):
    runner = FakeRunner(responses={"*": {"ok": True}})
    job = make_job(tmp_path, prompt="一二三四五六七八")  # 8 字元
    res = runner.run(job)
    assert len(runner.calls) == 1
    assert runner.calls[0].job_id == job.job_id
    assert res.input_tokens == len(job.prompt_text) // 4
    assert res.output_tokens == len(res.text) // 4
    assert res.raw_path is not None and res.raw_path.exists()


def test_write_raw_none_log_dir_returns_none(tmp_path):
    assert write_raw(None, make_job(tmp_path), "txt", "x") is None


# ---------- ClaudeCodeRunner ----------

CC_EXPECTED_CMD = [
    "/opt/claude", "-p",
    "--model", "model-x",
    "--effort", "medium",
    "--output-format", "json",
    "--allowedTools", "Read,Write,Glob,Grep",
    "--disallowedTools", "Bash,WebFetch,WebSearch",
    "--permission-mode", "acceptEdits",
    "--setting-sources", "project,local",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--settings", isolation.settings_json([]),  # 無封鎖目錄：只剩 disableAllHooks 與設定檔寫入禁止
]


def cc_tier() -> TierConfig:
    return TierConfig(name="strong", backend="claude_code", model="model-x", claude_bin="/opt/claude", timeout_s=123)


def cc_stdout(result_text: str = '{"a": 1}', usage: dict | None = None, cost: float | None = 0.0123,
              is_error: bool = False) -> str:
    if usage is None:
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 3,
                 "output_tokens": 7}
    return json.dumps({"type": "result", "is_error": is_error, "result": result_text,
                       "usage": usage, "total_cost_usd": cost})


class FakeClaude:
    """取代 subprocess.run；可在 cwd 內模擬 claude 寫檔，或拋出例外。"""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0,
                 exc: BaseException | None = None, on_run=None):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.exc, self.on_run = exc, on_run
        self.cmd = None
        self.kwargs = None

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        if self.exc is not None:
            raise self.exc
        if self.on_run is not None:
            self.on_run(kwargs["cwd"])
        return subprocess.CompletedProcess(args=cmd, returncode=self.returncode,
                                           stdout=self.stdout, stderr=self.stderr)


def test_claude_command_and_stdin_and_cwd(tmp_path):
    fake = FakeClaude(stdout=cc_stdout())
    runner = ClaudeCodeRunner(cc_tier(), runner_fn=fake, denies=[])
    job = make_job(tmp_path, prompt="這是要送進 stdin 的提示")
    runner.run(job)
    assert fake.cmd == CC_EXPECTED_CMD
    assert fake.kwargs["input"] == "這是要送進 stdin 的提示"
    assert "這是要送進 stdin 的提示" not in fake.cmd  # 提示不走參數
    assert fake.kwargs["cwd"] == str(job.sandbox)
    assert fake.kwargs["timeout"] == 123
    assert fake.kwargs["capture_output"] is True
    assert fake.kwargs["text"] is True
    assert isinstance(fake.kwargs["env"], dict)  # 子程序環境有明確傳入（已去除秘密變數）


def test_claude_parses_usage_with_cache_and_cost(tmp_path):
    fake = FakeClaude(stdout=cc_stdout('```json\n{"a": 1}\n```'))
    res = ClaudeCodeRunner(cc_tier(), runner_fn=fake, denies=[]).run(make_job(tmp_path))
    assert res.parsed_json == {"a": 1}
    assert res.input_tokens == 10 + 5 + 3
    assert res.output_tokens == 7
    assert res.cost_usd == pytest.approx(0.0123)
    assert res.model == "model-x"


def test_claude_nonzero_returncode_is_transport_error(tmp_path):
    fake = FakeClaude(stderr="rate limit exceeded", returncode=1)
    with pytest.raises(TransportError):
        ClaudeCodeRunner(cc_tier(), runner_fn=fake, denies=[]).run(make_job(tmp_path))


def test_claude_missing_binary_is_fatal(tmp_path):
    fake = FakeClaude(exc=FileNotFoundError("claude"))
    with pytest.raises(FatalLLMError):
        ClaudeCodeRunner(cc_tier(), runner_fn=fake, denies=[]).run(make_job(tmp_path))


def test_claude_timeout_is_transport_error(tmp_path):
    fake = FakeClaude(exc=subprocess.TimeoutExpired(cmd="claude", timeout=123))
    with pytest.raises(TransportError):
        ClaudeCodeRunner(cc_tier(), runner_fn=fake, denies=[]).run(make_job(tmp_path))


def test_claude_is_error_flag_is_transport_error(tmp_path):
    fake = FakeClaude(stdout=cc_stdout(result_text="出錯了", is_error=True))
    with pytest.raises(TransportError):
        ClaudeCodeRunner(cc_tier(), runner_fn=fake, denies=[]).run(make_job(tmp_path))


def test_claude_without_sandbox_is_fatal(tmp_path):
    fake = FakeClaude(stdout=cc_stdout())
    with pytest.raises(FatalLLMError):
        ClaudeCodeRunner(cc_tier(), runner_fn=fake, denies=[]).run(make_job(tmp_path, sandbox=False))
    assert fake.cmd is None  # 根本沒有呼叫子程序


def test_claude_detects_new_and_modified_files(tmp_path):
    job = make_job(tmp_path)
    (job.sandbox / "old.txt").write_text("old", encoding="utf-8")
    (job.sandbox / "same.txt").write_text("unchanged", encoding="utf-8")

    def write_files(cwd: str):
        root = Path(cwd)
        (root / "new.json").write_text('{"n": 1}', encoding="utf-8")
        (root / "old.txt").write_text("old-changed-longer", encoding="utf-8")  # 大小改變

    fake = FakeClaude(stdout=cc_stdout(), on_run=write_files)
    res = ClaudeCodeRunner(cc_tier(), runner_fn=fake, denies=[]).run(job)
    assert res.files_written == ["new.json", "old.txt"]


# ---------- OpenAICompatRunner ----------

KEY = "tk-" + secrets.token_hex(8)  # 執行期產生的假金鑰
KEY_ENV = "TEST_LLM_KEY"


def oa_tier(**kw) -> TierConfig:
    base = dict(name="weak", backend="openai_compat", model="m-1", base_url="https://api.example.test/v1/",
                api_key_env=KEY_ENV, max_output_tokens=500, temperature=0.2, json_mode=True)
    base.update(kw)
    return TierConfig(**base)


def oa_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def oa_ok_response(content: str = '{"ok": true}') -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
    })


def test_openai_request_body_and_auth(tmp_path):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.content)
        return oa_ok_response()

    runner = OpenAICompatRunner(oa_tier(), client=oa_client(handler), env={KEY_ENV: KEY})
    res = runner.run(make_job(tmp_path, prompt="請回答"))
    assert seen["url"] == "https://api.example.test/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    assert body["model"] == "m-1"
    assert body["messages"] == [{"role": "user", "content": "請回答"}]
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 500
    assert body["response_format"] == {"type": "json_object"}
    assert res.parsed_json == {"ok": True}
    assert (res.input_tokens, res.output_tokens) == (11, 4)


def test_openai_no_json_mode_omits_response_format(tmp_path):
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return oa_ok_response()

    OpenAICompatRunner(oa_tier(json_mode=False), client=oa_client(handler), env={KEY_ENV: KEY}).run(make_job(tmp_path))
    assert "response_format" not in seen["body"]


def test_openai_429_carries_retry_after(tmp_path):
    def handler(req):
        return httpx.Response(429, headers={"Retry-After": "7"}, text="慢一點")

    runner = OpenAICompatRunner(oa_tier(), client=oa_client(handler), env={KEY_ENV: KEY})
    with pytest.raises(TransportError) as ei:
        runner.run(make_job(tmp_path))
    assert ei.value.retry_after == 7.0


def test_openai_500_is_transport_error(tmp_path):
    def handler(req):
        return httpx.Response(500, text="internal")

    runner = OpenAICompatRunner(oa_tier(), client=oa_client(handler), env={KEY_ENV: KEY})
    with pytest.raises(TransportError) as ei:
        runner.run(make_job(tmp_path))
    assert ei.value.retry_after is None
    assert not isinstance(ei.value, FatalLLMError)


def test_openai_401_is_fatal_and_message_has_no_key(tmp_path):
    def handler(req):
        # 模擬伺服器錯誤訊息意外回顯金鑰：仍不得出現在例外訊息中
        return httpx.Response(401, text=f"invalid key {KEY}")

    runner = OpenAICompatRunner(oa_tier(), client=oa_client(handler), env={KEY_ENV: KEY})
    with pytest.raises(FatalLLMError) as ei:
        runner.run(make_job(tmp_path))
    assert "401" in str(ei.value)
    assert KEY not in str(ei.value)


def test_openai_missing_key_is_fatal_without_request(tmp_path):
    called = {"n": 0}

    def handler(req):
        called["n"] += 1
        return oa_ok_response()

    runner = OpenAICompatRunner(oa_tier(), client=oa_client(handler), env={})
    with pytest.raises(FatalLLMError):
        runner.run(make_job(tmp_path))
    assert called["n"] == 0


def test_openai_stored_files_do_not_contain_key(tmp_path):
    def handler(req):
        return oa_ok_response('{"ok": true, "files": {"note.md": "內容"}}')

    job = make_job(tmp_path)
    OpenAICompatRunner(oa_tier(), client=oa_client(handler), env={KEY_ENV: KEY}).run(job)
    stored = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert stored, "應該有存檔"
    for p in stored:
        assert KEY not in p.read_text(encoding="utf-8"), p
    assert (job.sandbox / "note.md").read_text(encoding="utf-8") == "內容"


# ---------- Router ----------

ROUTING = {
    "defaults": {"max_context_tokens": 100000},
    "jobs": {
        "DOSSIER": {"model": "weak", "max_context_tokens": 100000},
        "TRIAGE": {"model": "weak", "max_context_tokens": 30000},
        "FULL_A": {"model": "strong"},
        "FULL_B": {"model": "medium"},
        "THESIS": {"model": {"HOLDING": "medium", "其他": "weak"}, "max_context_tokens": 60000},
        "PORTFOLIO": {"model": "none"},
    },
}


@pytest.fixture
def routing_path(tmp_path) -> Path:
    p = tmp_path / "model_routing.json"
    p.write_text(json.dumps(ROUTING, ensure_ascii=False), encoding="utf-8")
    return p


def fake_config(overrides: dict | None = None, caps: dict | None = None):
    caps = caps or {}
    models = {
        n: TierConfig(name=n, backend="fake", model=f"{n}-m", max_context_tokens=caps.get(n, 100000))
        for n in ("strong", "medium", "weak")
    }
    return types.SimpleNamespace(models=models, job_tier_overrides=dict(overrides or {}))


def test_tier_for_follows_routing(routing_path):
    r = Router(fake_config(), routing_path)
    assert r.tier_for(make_job_spec("DOSSIER")) == "weak"
    assert r.tier_for(make_job_spec("TRIAGE")) == "weak"
    assert r.tier_for(make_job_spec("FULL_A")) == "strong"
    assert r.tier_for(make_job_spec("FULL_B")) == "medium"


def make_job_spec(job_type: str, params: dict | None = None, ticker: str = "AAPL") -> JobSpec:
    return JobSpec(job_id=f"r:{ticker}:{job_type}:1", run_id="r", job_type=job_type, ticker=ticker,
                   asof="2026-01-02", params=params or {})


def test_thesis_tier_depends_on_holding(routing_path):
    r = Router(fake_config(), routing_path)
    assert r.tier_for(make_job_spec("THESIS", {"holding": True})) == "medium"
    assert r.tier_for(make_job_spec("THESIS", {"holding": False})) == "weak"
    assert r.tier_for(make_job_spec("THESIS")) == "weak"


def test_override_wins_over_routing(routing_path):
    r = Router(fake_config(overrides={"FULL_A": "medium", "THESIS": "strong"}), routing_path)
    assert r.tier_for(make_job_spec("FULL_A")) == "medium"
    assert r.tier_for(make_job_spec("THESIS", {"holding": True})) == "strong"
    assert r.tier_for(make_job_spec("DOSSIER")) == "weak"  # 無覆寫者照表


def test_none_tier_is_value_error(routing_path):
    r = Router(fake_config(), routing_path)
    with pytest.raises(ValueError):
        r.tier_for(make_job_spec("PORTFOLIO"))


def test_dry_run_returns_fake(routing_path):
    r = Router(fake_config(), routing_path, dry_run=True)
    a = r.runner_for("strong")
    assert isinstance(a, FakeRunner)
    assert r.runner_for("weak") is a

    own = FakeRunner(responses={"*": {}})
    r2 = Router(fake_config(), routing_path, dry_run=True, fake=own)
    assert r2.runner_for("medium") is own


def test_max_context_tokens_takes_minimum(routing_path):
    # DOSSIER 工作層 100000，weak 等級上限 20000 → 取 20000
    r = Router(fake_config(caps={"weak": 20000}), routing_path)
    assert r.max_context_tokens("DOSSIER") == 20000
    # TRIAGE 工作層 30000，weak 等級 100000 → 取 30000
    r2 = Router(fake_config(caps={"weak": 100000}), routing_path)
    assert r2.max_context_tokens("TRIAGE") == 30000
    # 指定等級時只看該等級
    assert r.max_context_tokens("DOSSIER", tier="strong") == 100000


def test_max_context_tokens_defaults_when_job_has_no_limit(routing_path):
    r = Router(fake_config(caps={"strong": 50000}), routing_path)
    # FULL_A 無工作層設定 → 預設 100000，strong 上限 50000 → 50000
    assert r.max_context_tokens("FULL_A") == 50000


def test_runner_is_cached_per_tier(routing_path):
    r = Router(fake_config(), routing_path)
    a = r.runner_for("weak")
    assert r.runner_for("weak") is a
    assert r.runner_for("strong") is not a


def test_build_runner_dispatch():
    assert isinstance(build_runner(TierConfig(name="s", backend="claude_code", model="m")), ClaudeCodeRunner)
    assert isinstance(build_runner(TierConfig(name="s", backend="fake", model="m")), FakeRunner)
    tier = TierConfig(name="s", backend="openai_compat", model="m", base_url="https://x.test/v1", api_key_env=KEY_ENV)
    assert isinstance(build_runner(tier), OpenAICompatRunner)
    with pytest.raises(ValueError):
        build_runner(TierConfig(name="s", backend="nope", model="m"))


def test_llm_error_hierarchy():
    assert issubclass(TransportError, LLMError)
    assert issubclass(FatalLLMError, LLMError)
