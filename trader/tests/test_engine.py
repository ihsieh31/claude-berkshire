"""工作引擎與處理器的測試。全部使用 FakeRunner，不連網、不呼叫真的 claude。

設定檔用 load_config 讀 tmp 目錄寫的 YAML；三個 tier 都是 fake backend。
"""
from __future__ import annotations

import json

import pytest
from pathlib import Path

from trader import db
from trader.config import load_config
from trader.jobs.engine import Engine
from trader.llm.base import TransportError
from trader.llm.fake import FakeRunner
from trader.llm.router import Router
from trader.models import JobSpec
from trader.paths import get_paths

ASOF = "2026-10-16"
RUN = "r1"


# ---------------------------------------------------------------- 測試環境

def _write_config(tmp_path: Path, max_calls: int = 200, approval: list | None = None) -> Path:
    """approval：需要同意的等級；預設 []（不經同意閘門，閘門另有 test_approvals.py）。"""
    p = tmp_path / "config.yaml"
    p.write_text(
        f"approval: {{require_for_tiers: {json.dumps(approval or [])}}}\n"
        "models:\n"
        "  strong: {backend: fake, model: fake, concurrency: 2}\n"
        "  medium: {backend: fake, model: fake, concurrency: 2}\n"
        "  weak: {backend: fake, model: fake, concurrency: 2}\n"
        "budget:\n"
        f"  max_llm_calls_per_run: {max_calls}\n"
        "  max_input_tokens_per_run: 0\n",
        encoding="utf-8",
    )
    return p


class Env:
    def __init__(self, tmp_path: Path, responses: dict, max_calls: int = 200, approval: list | None = None):
        self.config = load_config(_write_config(tmp_path, max_calls, approval), env_file=tmp_path / "none.env")
        self.paths = get_paths(state_dir=tmp_path / "state").ensure()
        self.conn = db.connect(self.paths.db)
        db.migrate(self.conn)
        self.fake = FakeRunner(responses=responses)
        self.router = Router(self.config, self.paths.routing_json, dry_run=True, fake=self.fake)
        self.engine = Engine(self.conn, self.router, self.paths, self.config, sleep=lambda s: None)
        self.engine.start_run(RUN, "daily", ASOF)

    def job(self, ticker: str, job_type: str, n: int, params: dict | None = None,
            deps: list[str] | None = None, run: str = RUN) -> JobSpec:
        return JobSpec(job_id=f"{run}:{ticker}:{job_type}:{n}", run_id=run, job_type=job_type,
                       ticker=ticker, asof=ASOF, deps=deps or [], params=params or {})

    def add(self, spec: JobSpec) -> str:
        assert self.engine.add_job(spec)
        return spec.job_id

    def rows(self, run: str = RUN) -> list:
        return self.conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY rowid", (run,)).fetchall()

    def by_type(self, job_type: str, run: str = RUN) -> list:
        return [r for r in self.rows(run) if r["job_type"] == job_type]

    def price_file(self, ticker: str = "KO", run: str = RUN) -> Path:
        """模擬 DOSSIER 前置已完成：資料包（不含價格）、價格檔、SEC 原文目錄（前置看到已存在就跳過）。"""
        d = self.paths.work / run / ticker
        (d / "dossier").mkdir(parents=True, exist_ok=True)
        (d / "sec").mkdir(parents=True, exist_ok=True)
        (d / "sec" / "10-K_2026-02-20_000001.txt").write_text("# 10-K\nItem 1. Business\nDrinks.\n", encoding="utf-8")
        (d / "dossier" / f"{ticker}.md").write_text(f"# 資料包：{ticker}\n", encoding="utf-8")
        p = d / "dossier" / f"{ticker}_price.json"
        p.write_text(json.dumps({"price": 60.5, "price_date": "2026-10-15"}), encoding="utf-8")
        return p


def make_env(tmp_path: Path, responses: dict, max_calls: int = 200, approval: list | None = None) -> Env:
    return Env(tmp_path, responses, max_calls, approval)


# ---------------------------------------------------------------- 合格輸出（格式照 output_schemas.json）

CAP_INPUT = json.loads((get_paths().skill_deep / "assets" / "cap_calc_example_input.json").read_text(encoding="utf-8"))


def gate3_pass_answers() -> dict:
    """依問卷每題取第一個通過選項，附理由與決策日前證據 → gate3_check 會通過。"""
    q = json.loads((get_paths().skill_deep / "assets" / "gate3_questionnaire.json").read_text(encoding="utf-8"))
    return {"ticker": "KO", "decision_date": ASOF, "answered_by": "agent", "answered_at": "2026-10-16 09:00 UTC",
            "price_seen_before_answering": False, "price_file_opened_at": "",
            "answers": {it["id"]: {"answer": it["pass_if"][0], "reason": "測試理由",
                                   "evidence": [{"source": "10-K", "date": "2026-02-20"}]}
                        for it in q["questions"]}}


def consistent_card(signal: str) -> dict:
    """訊號卡上限與 IV 取自 cap_calc 實算 → 覆核一致。"""
    from trader.analysis.checks import run_cap_calc
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "c.json"
        f.write_text(json.dumps(CAP_INPUT), encoding="utf-8")
        cap = run_cap_calc(f)
    return {"ticker": "KO", "signal": signal, "conclusion": "買" if signal == "BUY" else "觀察",
            "reason_code": "PASS_ALL" if signal == "BUY" else "TOO_HARD",
            "auto_buy_cap": cap.get("auto_buy_cap"), "buy_price_cap": cap.get("cap"),
            "value_per_share": {"conservative": cap.get("IV_cons"), "base": cap.get("IV_base"),
                                "optimistic": cap.get("IV_opt"), "zero_growth_IV0": cap.get("IV0")}}


def dossier_ok() -> dict:
    return {
        "ticker": "KO", "decision_date": ASOF, "sections_appended": ["業務"],
        "dq_checks": [{"warning": "無", "result": "已核對：原因成立"}],
        "missing": [], "contains_price": False,
        "files": {"dossier/KO.md": "# KO 資料卷宗\n## 年報文字節錄\n業務。\n"},
    }


def triage(escalate: bool) -> dict:
    return {
        "ticker": "KO",
        "items": [{"ref": "8-K 2026-10-01 2.02", "answer": "是" if escalate else "否",
                   "touches": "營收" if escalate else "", "reason": "測試"}],
        "escalate": escalate,
    }


def thesis(verdict: str) -> dict:
    return {
        "ticker": "KO", "verdict": verdict,
        "kill_criteria_status": [{"criterion": "毛利率連兩季下滑", "status": "未觸發", "evidence": "10-Q 2026-10-01"}],
        "r_list_status": [{"item": "R1", "status": "無變化", "evidence": "測試"}], "new_facts": [], "reason": "測試",
    }


def full_a(stopped: bool = False) -> dict:
    return {
        "ticker": "KO", "decision_date": ASOF, "stopped": stopped,
        "stopped_at_gate": "G3" if stopped else None,
        "reason_code": "REJECT" if stopped else None,
        "asset_class": "非金融", "starting_grade": None if stopped else "A",
        "downgrades": [],
        "moat_tests": {"pricing_power": "強", "attack_copy": "中", "price_cut": "強"},
        "moat_evidence": {"pricing_power": "足夠", "attack_copy": "缺資料", "price_cut": "足夠"},
        "red_flags": {"exclusion": 0, "warning": 0},
        "capital_allocation_net": 0, "tail_loss_after_tax_pct": 30.0,
        "normalized_inputs": {"oe_per_share": 3.2, "g10": None, "g5": 0.05,
                              "excluded_shock_years": [], "roe_std_ex_shock_pp": 2.1},
        "thesis_draft": {"one_liner": "測試論點", "key_assumptions": ["假設 A", "假設 B", "假設 C"],
                         "kill_criteria": [{"text": "毛利率連兩季下滑", "due": "2027-06-30"}]},
        "missing": [], "inferred_thresholds_used": [],
        "files": {"KO/gate3_answers.json": json.dumps(gate3_pass_answers(), ensure_ascii=False),
                  "KO/report_A.md": "# A 報告\n"},
    }


G8_NONE = {"self_adjusted": None, "best_holding": None, "best_holding_adjusted": None,
           "weakest_holding_adjusted": None, "best_watch": None, "best_watch_adjusted": None,
           "treasury": None, "market": None, "winner": "未比較"}


def full_b(signal: str = "BUY") -> dict:
    return {
        "ticker": "KO", "decision_id": "KO-20261016-1", "signal": signal,
        "reason_code": "PASS_ALL" if signal == "BUY" else "TOO_HARD",
        "stopped_at_gate": None, "certainty_grade": "B",
        "auto_buy_cap": 12.5, "target_weight": 0.05, "first_tranche_weight": 0.025,
        "g8": G8_NONE,
        "files": {"KO/cap_input.json": json.dumps(CAP_INPUT), "KO/report.md": "# 報告\n",
                  "KO/signal_card.json": json.dumps(consistent_card(signal), ensure_ascii=False),
                  "KO/registry_entry.json": json.dumps({"ticker": "KO", "status": "WATCH"}),
                  "KO/decision_log.json": json.dumps({"ticker": "KO", "action": signal}),
                  "KO/thesis.md": "# 論點\n"},
    }


def red_team(fatal: bool) -> dict:
    return {
        "ticker": "KO", "fatal": fatal,
        "fatal_paths": [{"path": "需求崩潰", "probability_note": "測試", "covered_by_kill_criteria": False}]
        if fatal else [],
        "number_errors": [], "rule_violations": [],
        "verdict": "退回" if fatal else "維持",
    }


def full_responses() -> dict:
    return {"DOSSIER": dossier_ok(), "FULL_A": full_a(), "FULL_B": full_b("BUY"), "RED_TEAM": red_team(False)}


# ---------------------------------------------------------------- 1. 完整 DAG

def test_full_dag_runs_to_done(tmp_path):
    env = make_env(tmp_path, full_responses())
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")  # FULL_B 才會開的價格檔

    result = env.engine.run(RUN)

    assert result["status"] == "done"
    assert result["done"] == 4 and result["failed"] == 0
    order = [(r["job_type"], r["status"]) for r in env.rows()]
    assert order == [("DOSSIER", "done"), ("FULL_A", "done"), ("FULL_B", "done"), ("RED_TEAM", "done")]
    calls = env.conn.execute("SELECT COUNT(*) FROM llm_calls WHERE run_id=?", (RUN,)).fetchone()[0]
    assert calls == 4
    run_status = env.conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN,)).fetchone()[0]
    assert run_status == "done"

    # FULL_B 沙箱：有價格檔與開檔時間紀錄
    fb_sandbox = Path(env.by_type("FULL_B")[0]["sandbox"])
    assert (fb_sandbox / "dossier" / "KO_price.json").is_file()
    assert (fb_sandbox / "dossier" / "price_file_opened_at.txt").is_file()
    # 終點回寫：決策、訊號、登錄表、論點檔
    dec = env.conn.execute("SELECT decision_id, signal FROM decisions").fetchall()
    assert [(r["decision_id"], r["signal"]) for r in dec] == [("KO-20261016-1", "BUY")]
    assert (env.paths.theses / f"KO_{ASOF}.md").is_file()
    # DOSSIER／FULL_A 沙箱：沒有任何 *_price.json
    for jt in ("DOSSIER", "FULL_A"):
        sb = Path(env.by_type(jt)[0]["sandbox"])
        assert list(sb.rglob("*_price.json")) == []
    # G8 持股比較檔只進 FULL_B；訊號卡的 G8 調整後預期報酬由腳本重算：(oe ÷ 價格 ＋ 保守 g0) × B 0.75
    assert (fb_sandbox / "portfolio" / "holdings_g8.json").is_file()
    for jt in ("DOSSIER", "FULL_A", "RED_TEAM"):
        assert list(Path(env.by_type(jt)[0]["sandbox"]).rglob("holdings_g8.json")) == []
    card = json.loads(env.conn.execute("SELECT data FROM signals").fetchone()[0])
    assert card["g8_adjusted_expected_return"] == pytest.approx((5.0 / 60.0 + 0.045) * 0.75, abs=1e-4)
    # gate3 答覆檔由 FULL_A 收集到同檔輸出區，且早於 FULL_B 建立時間
    td = env.paths.work / RUN / "KO"
    gate3 = td / "gate3_answers.json"
    assert gate3.is_file()
    fb_created = json.loads(env.by_type("FULL_B")[0]["params"])["created_ts"]
    assert gate3.stat().st_mtime < fb_created


def _g8_view(env, best_adj: float) -> None:
    from trader.portfolio.holdings_view import view_path
    f = view_path(env.paths, RUN)
    f.parent.mkdir(parents=True, exist_ok=True)
    row = {"ticker": "PEP", "grade": "A", "expected_return": best_adj / 0.9, "adjusted_expected_return": best_adj,
           "addable": True}
    f.write_text(json.dumps({"n_holdings": 1, "holdings": [row], "watchlist": [],
                             "benchmarks": {"treasury": {"adjusted": 0.04}, "market": {"adjusted": 0.016}}}),
                 encoding="utf-8")


def test_g8_clear_loss_downgrades_buy_to_watch(tmp_path):
    """最佳持股高出本標的 ≥1pp → BUY 改 WATCH（停在 G8），不跑 RED_TEAM。本標的 (5/60+0.045)×0.75≈0.0963。"""
    env = make_env(tmp_path, full_responses())
    _g8_view(env, 0.12)
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()] == ["DOSSIER", "FULL_A", "FULL_B"]
    dec = env.conn.execute("SELECT signal, data FROM decisions").fetchone()
    assert dec["signal"] == "WATCH"
    assert any("G8 機會成本未勝出" in n and "PEP" in n for n in json.loads(dec["data"])["framework_notes"])
    card = json.loads(env.conn.execute("SELECT data FROM signals").fetchone()[0])
    assert card["stopped_at_gate"] == "G8" and card["signal"] == "WATCH"


def test_g8_tie_within_1pp_keeps_buy(tmp_path):
    env = make_env(tmp_path, full_responses())
    _g8_view(env, 0.10)
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    env.engine.run(RUN)
    assert "RED_TEAM" in [r["job_type"] for r in env.rows()]
    assert env.conn.execute("SELECT signal FROM decisions").fetchone()[0] == "BUY"


# ---------------------------------------------------------------- 2. FULL_A 停止

def test_full_a_stopped_does_not_create_full_b(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True)})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))

    result = env.engine.run(RUN)

    assert result["status"] == "done"
    assert [r["job_type"] for r in env.rows()] == ["FULL_A"]
    dec = env.conn.execute("SELECT signal, reason_code FROM decisions").fetchall()
    assert [(r["signal"], r["reason_code"]) for r in dec] == [("NO_ACTION", "REJECT")]
    assert env.by_type("FULL_A")[0]["status"] == "done"


# ---------------------------------------------------------------- 3. TRIAGE → THESIS → 證偽 → DOSSIER

def test_triage_escalates_to_thesis_then_dossier(tmp_path):
    responses = {"TRIAGE": triage(True), "THESIS": thesis("證偽"), **full_responses()}
    env = make_env(tmp_path, responses)
    tri_id = env.add(env.job("KO", "TRIAGE", 1))
    env.price_file("KO")

    env.engine.run(RUN)

    thesis_rows = env.by_type("THESIS")
    assert len(thesis_rows) == 1 and thesis_rows[0]["status"] == "done"
    assert json.loads(thesis_rows[0]["deps"]) == [tri_id]
    dossier_rows = env.by_type("DOSSIER")
    assert len(dossier_rows) == 1
    assert json.loads(dossier_rows[0]["deps"]) == [thesis_rows[0]["job_id"]]
    assert dossier_rows[0]["status"] == "done"


def test_thesis_intact_does_not_create_dossier(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(True), "THESIS": thesis("完好")})
    env.add(env.job("KO", "TRIAGE", 1))
    env.engine.run(RUN)
    assert env.by_type("DOSSIER") == []


# ---------------------------------------------------------------- 4. 輸出不合格

def test_invalid_then_valid_retries_with_feedback(tmp_path):
    bad = {"ticker": "KO"}  # 缺必填欄位
    env = make_env(tmp_path, {"TRIAGE": [bad, triage(False)]})
    env.add(env.job("KO", "TRIAGE", 1))

    result = env.engine.run(RUN)

    assert result["status"] == "done"
    row = env.by_type("TRIAGE")[0]
    assert row["status"] == "done"
    assert row["attempts"] == 2
    assert len(env.fake.calls) == 2
    assert "未通過格式檢查" in env.fake.calls[1].prompt_text
    calls = env.engine.conn.execute("SELECT attempt, error FROM llm_calls ORDER BY rowid").fetchall()
    assert calls[0]["error"].startswith("invalid_output") and calls[1]["error"] is None


def test_single_element_array_accepted(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    env.add(env.job("KO", "TRIAGE", 1))
    assert env.engine.run(RUN)["status"] == "done"
    assert env.by_type("TRIAGE")[0]["attempts"] == 1


def test_invalid_twice_fails_and_logs_decision(tmp_path):
    env = make_env(tmp_path, {"PRICE": [{"ticker": "KO"}, {"ticker": "KO"}]})
    # PRICE 需要 state/cap_inputs（由 DOSSIER 的 FULL_B 寫入）；沒有時 pre_script 會直接失敗
    cap_dir = env.paths.state / "cap_inputs"
    cap_dir.mkdir(parents=True, exist_ok=True)
    (cap_dir / "KO.json").write_text(json.dumps(CAP_INPUT), encoding="utf-8")
    env.add(env.job("KO", "PRICE", 1, params={"price": 60.5, "price_date": ASOF}))

    env.engine.run(RUN)

    row = env.by_type("PRICE")[0]
    assert row["status"] == "failed"
    assert row["error"] == "invalid_output"
    assert row["attempts"] == 2
    dec = env.conn.execute("SELECT signal, reason_code FROM decisions WHERE ticker='KO'").fetchall()
    assert len(dec) == 1
    assert (dec[0]["signal"], dec[0]["reason_code"]) == ("NO_ACTION", "MISSING_DATA")


# ---------------------------------------------------------------- 5. 依賴失敗

def test_dependency_failure_skips_dependents_only(tmp_path):
    # 沒有 DOSSIER 的回應 → FakeRunner 拋 FatalLLMError → DOSSIER 失敗
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    dos = env.add(env.job("KO", "DOSSIER", 1))
    env.add(env.job("KO", "FULL_A", 1, deps=[dos]))
    env.add(env.job("MSFT", "TRIAGE", 1))  # 獨立工作不受影響

    result = env.engine.run(RUN)

    assert result["status"] == "failed"  # L2：有失敗工作不得標 done
    assert env.by_type("DOSSIER")[0]["status"] == "failed"
    fa = env.by_type("FULL_A")[0]
    assert fa["status"] == "skipped" and dos in fa["error"]
    assert env.by_type("TRIAGE")[0]["status"] == "done"


# ---------------------------------------------------------------- 6. 預算與斷點續跑

def test_budget_pauses_then_resumes_without_rerunning_done(tmp_path):
    env = make_env(tmp_path, full_responses(), max_calls=1)
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")

    first = env.engine.run(RUN)
    assert first["status"] == "paused_budget"
    assert len(env.fake.calls) == 1
    assert [r["status"] for r in env.by_type("DOSSIER")] == ["done"]
    assert env.by_type("FULL_A")[0]["status"] == "pending"

    # 提高預算後續跑
    env.config.budget.max_llm_calls_per_run = 100
    second = env.engine.run(RUN)

    assert second["status"] == "done"
    assert len(env.fake.calls) == 4  # DOSSIER 沒有重跑
    assert sum(1 for c in env.fake.calls if c.job_type == "DOSSIER") == 1


def test_budget_cost_cap_pauses_and_token_count_unlimited_by_default(tmp_path):
    env = make_env(tmp_path, full_responses())
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    # 先記一筆既有費用與大量快取 token（模擬代理式 DOSSIER）
    env.conn.execute(
        "INSERT INTO llm_calls(job_id, run_id, tier, input_tokens, output_tokens, cost_usd, ok, created_at) "
        "VALUES('x', ?, 'weak', 90000000, 0, 2.0, 1, '2026-10-16T00:00:00Z')", (RUN,))
    env.config.budget.max_cost_usd_per_run = 1.0
    first = env.engine.run(RUN)
    assert first["status"] == "paused_budget"
    assert env.fake.calls == []

    # 費用上限放寬後續跑；token 上限預設 0＝不限，9000 萬 token 不擋
    env.config.budget.max_cost_usd_per_run = 100.0
    assert env.config.budget.max_input_tokens_per_run == 0
    second = env.engine.run(RUN)
    assert second["status"] == "done"


# ---------------------------------------------------------------- 7. 冪等

def test_add_job_is_idempotent(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    spec = env.job("KO", "TRIAGE", 1)
    assert env.engine.add_job(spec) is True
    assert env.engine.add_job(spec) is False
    assert len(env.rows()) == 1


def test_residual_running_job_is_reset_and_rerun(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    jid = env.add(env.job("KO", "TRIAGE", 1))
    # 模擬上次中斷：狀態殘留 running
    env.conn.execute("UPDATE jobs SET status='running' WHERE job_id=?", (jid,))
    env.conn.commit()

    env.engine.run(RUN)

    assert [c.job_id for c in env.fake.calls] == [jid]
    assert env.by_type("TRIAGE")[0]["status"] == "done"


# ---------------------------------------------------------------- 8. 價格隔離

def test_full_a_gets_no_price_even_when_available(tmp_path):
    """同檔輸出區已有價格檔、screen_context 含 price_context：FULL_A 沙箱與提示都不得出現價格。"""
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True)})
    env.price_file("KO")
    sc = {"ticker": "KO", "screen_decision": "PASS", "price_context": {"price": 60.5, "market_cap": 2.6e11}}
    env.add(env.job("KO", "FULL_A", 1, params={"screen_context": sc}))

    env.engine.run(RUN)

    sb = Path(env.by_type("FULL_A")[0]["sandbox"])
    assert list(sb.rglob("*_price.json")) == []
    prompt = env.fake.calls[0].prompt_text
    assert "price_context" not in prompt and "60.5" not in prompt and "screen_decision" in prompt


def test_dossier_with_price_text_is_scrubbed_before_full_a(tmp_path):
    resp = dossier_ok()
    resp["files"] = {"dossier/KO.md": "# KO\n業務說明。\nThe closing price was $60.50.\n"}
    env = make_env(tmp_path, {"DOSSIER": resp, "FULL_A": full_a(stopped=True)})
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1))
    env.engine.run(RUN)
    md = (env.paths.work / RUN / "KO" / "dossier" / "KO.md").read_text(encoding="utf-8")
    assert "業務說明" in md and "60.50" not in md


# ---------------------------------------------------------------- 9. 傳輸錯誤

def test_transport_error_exhausts_retries_and_others_continue(tmp_path):
    env = make_env(tmp_path, {"DOSSIER": TransportError("連線失敗"), "TRIAGE": triage(False)})
    env.price_file("KO")
    env.add(env.job("KO", "DOSSIER", 1))
    env.add(env.job("MSFT", "TRIAGE", 1))

    env.engine.run(RUN)

    dos = env.by_type("DOSSIER")[0]
    assert dos["status"] == "failed"
    assert "TransportError" in dos["error"]
    assert sum(1 for c in env.fake.calls if c.job_type == "DOSSIER") == 4  # 1 + 3 次重試
    assert env.by_type("TRIAGE")[0]["status"] == "done"


# ---------------------------------------------------------------- 10. 紅隊升級與上限

def test_red_team_fatal_twice_ends_as_watch(tmp_path):
    responses = {**full_responses(), "RED_TEAM": red_team(True)}  # 兩輪都 fatal
    env = make_env(tmp_path, responses)
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")

    result = env.engine.run(RUN)

    assert result["status"] == "done"
    fb = env.by_type("FULL_B")
    assert len(fb) == 2  # 原始一次 + 第一輪 fatal 後退回一次
    assert json.loads(fb[1]["params"])["redteam_round"] == 1
    rt = env.by_type("RED_TEAM")
    assert len(rt) == 2
    assert json.loads(rt[-1]["result"])["final_signal"] == "WATCH"
    assert "final_signal" not in json.loads(rt[0]["result"])


# ---------------------------------------------------------------- 不合格輸出的保守處理

def test_triage_invalid_escalates_to_thesis(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": [{"ticker": "KO"}, {"ticker": "KO"}], "THESIS": thesis("完好")})
    env.add(env.job("KO", "TRIAGE", 1, params={"holding": True}))

    env.engine.run(RUN)

    assert env.by_type("TRIAGE")[0]["status"] == "failed"
    th = env.by_type("THESIS")
    assert len(th) == 1 and th[0]["status"] == "done"
    assert json.loads(th[0]["params"])["triage_invalid"] is True
    assert th[0]["tier"] == "medium"  # 持股 → 中模型


def test_corp_action_invalid_writes_anomaly_and_pauses(tmp_path):
    env = make_env(tmp_path, {"CORP_ACTION": [{"ticker": "KO"}, {"ticker": "KO"}]})
    env.add(env.job("KO", "CORP_ACTION", 1))

    env.engine.run(RUN)

    assert env.by_type("CORP_ACTION")[0]["status"] == "failed"
    an = env.conn.execute("SELECT kind, pause_trading, status FROM anomalies WHERE ticker='KO'").fetchall()
    assert [tuple(r) for r in an] == [("corp_action_invalid_output", 1, "open")]
    assert env.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_dossier_params_reach_full_b(tmp_path):
    """組合狀態與大型股 P/E 中位要從 DOSSIER 一路傳到 FULL_B（2026-10-16 OTIS 實測時曾在 DOSSIER→FULL_A 遺失）。"""
    env = make_env(tmp_path, full_responses())
    ps = {"cash": 100000, "holdings": []}
    pe = {"pe_norm_median": 26.57}
    env.add(env.job("KO", "DOSSIER", 1, params={"portfolio_state": ps, "largecap_pe": pe}))
    env.price_file("KO")
    env.engine.run(RUN)
    for jt in ("FULL_A", "FULL_B"):
        p = json.loads(env.by_type(jt)[0]["params"])
        assert p["portfolio_state"] == ps and p["largecap_pe"] == pe


# ---------------------------------------------------------------- L2：run 狀態與失敗異常、M3：jobs retry

def _run_status(env) -> str:
    return env.conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN,)).fetchone()[0]


def test_run_with_failed_job_is_failed_and_writes_anomaly_once(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    dos = env.add(env.job("KO", "DOSSIER", 1))  # 沒有回應 → 失敗
    env.add(env.job("KO", "FULL_A", 1, deps=[dos]))
    env.add(env.job("MSFT", "TRIAGE", 1))

    assert env.engine.run(RUN)["status"] == "failed"
    assert _run_status(env) == "failed"
    an = env.conn.execute("SELECT ticker, kind, pause_trading, status, detail FROM anomalies").fetchall()
    assert [(r["ticker"], r["kind"], r["pause_trading"], r["status"]) for r in an] == [("KO", "job_failed", 0, "open")]
    d = json.loads(an[0]["detail"])
    assert d["run_id"] == RUN and d["job_id"] == dos and d["error"]

    env.engine.run(RUN)  # 重跑不重複寫
    assert env.conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == 1


def test_run_all_done_is_done_and_no_anomaly(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    env.add(env.job("KO", "TRIAGE", 1))
    assert env.engine.run(RUN)["status"] == "done"
    assert env.conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == 0


def test_failed_corp_action_gets_no_generic_anomaly(tmp_path):
    env = make_env(tmp_path, {})  # CORP_ACTION 無回應 → 失敗；暫停異常由 daily 寫，引擎不重複
    env.add(env.job("KO", "CORP_ACTION", 1))
    assert env.engine.run(RUN)["status"] == "failed"
    assert env.conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='job_failed'").fetchone()[0] == 0


def test_killed_run_stays_running_not_done(tmp_path):
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    jid = env.add(env.job("KO", "TRIAGE", 1))
    env.engine._begin(RUN)
    env.engine._update(jid, status="running")  # 模擬行程被強殺：run 與工作都停在 running
    assert _run_status(env) == "running"
    env.engine._begin(RUN)  # 下次啟動
    assert env.by_type("TRIAGE")[0]["status"] == "pending" and _run_status(env) == "running"
    assert env.engine.run(RUN)["status"] == "done"


def test_retry_failed_resets_and_reruns_keeping_call_history(tmp_path):
    from trader.jobs.engine import retry_failed
    env = make_env(tmp_path, {"TRIAGE": TransportError("暫時性錯誤")})
    env.add(env.job("KO", "TRIAGE", 1))
    env.add(env.job("MSFT", "TRIAGE", 1))
    env.engine.run(RUN)
    env.fake.responses["TRIAGE"] = triage(False)
    failed = [r["job_id"] for r in env.rows() if r["status"] == "failed"]
    assert failed
    calls = env.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]

    ids = retry_failed(env.conn, RUN, ticker="KO")
    assert ids == [f"{RUN}:KO:TRIAGE:1"]
    row = env.conn.execute("SELECT status, error, finished_at FROM jobs WHERE job_id=?", (ids[0],)).fetchone()
    assert (row["status"], row["error"], row["finished_at"]) == ("pending", None, None)
    assert env.conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == calls  # 歷史呼叫保留
    assert env.conn.execute("SELECT status FROM anomalies WHERE kind='job_failed'").fetchone()[0] == "resolved"
    assert env.engine.run(RUN)["status"] == "failed"  # MSFT 未重設，仍是失敗
    assert [r["status"] for r in env.rows()] == ["done", "failed"]


def test_retry_skips_corp_action_and_reset_dependents(tmp_path):
    from trader.jobs.engine import retry_failed
    env = make_env(tmp_path, {"TRIAGE": triage(False)})
    dos = env.add(env.job("KO", "DOSSIER", 1))
    env.add(env.job("KO", "FULL_A", 1, deps=[dos]))
    env.add(env.job("KO", "CORP_ACTION", 1))
    env.engine.run(RUN)
    ids = retry_failed(env.conn, RUN, job_id=dos)
    assert set(ids) == {dos, f"{RUN}:KO:FULL_A:1"}
    assert env.by_type("CORP_ACTION")[0]["status"] == "failed"
