"""P32：輸出區歸屬與 FULL/RED_TEAM 輸入完整性（M5-B5 deep 端）＋D30（持股版 FULL_A）＋D45＋D48（API 路徑隔離）。
全部 FakeRunner／tmp_path，不碰正式 state。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_engine import ASOF, RUN, CAP_INPUT, consistent_card, full_a, full_b, make_env, red_team, _g8_view
from test_hold_review import _holding, hold

from trader import db
from trader.jobs import deep
from trader.jobs.context import build_prompt
from trader.jobs.deep import DossierHandler, FullAHandler, FullBHandler, RedTeamHandler, trigger_text
from trader.jobs.handlers import HandlerCtx
from trader.jobs.hold_jobs import HoldReviewHandler
from trader.jobs.review_jobs import ThesisHandler, TriageHandler
from trader.jobs.validate import load_schemas
from trader.models import JobSpec
from trader.paths import get_paths
from trader.portfolio import correlation as corr


@pytest.fixture
def env2(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_STATE_DIR", str(tmp_path / "state"))
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield paths, conn
    conn.close()


def _ctx(paths, conn, sec=None):
    return HandlerCtx(paths=paths, schemas=load_schemas(paths.schemas_json), work_dir=paths.work, conn=conn, sec=sec)


def _job(jt, params=None, n=1):
    return JobSpec(job_id=f"r1:KO:{jt}:{n}", run_id="r1", job_type=jt, ticker="KO", asof=ASOF, params=params or {})


def _files(sb) -> list[str]:
    sb = Path(sb)
    return sorted(p.relative_to(sb).as_posix() for p in sb.rglob("*")
                  if p.is_file() and not p.relative_to(sb).as_posix().startswith("pkg/"))


def _td(paths) -> Path:
    td = paths.work / "r1" / "KO"
    (td / "dossier").mkdir(parents=True, exist_ok=True)
    return td


# ---------------------------------------------------------------- M5-D1-03／D45：只看本次沙箱

def test_fullb_redo_missing_files_not_filled_by_previous_version(tmp_path):
    """FULL_B:2（退回重寫）只寫 report.md → 不得由 FULL_B:1 留在 td 的檔補上，應建 output_retry 的 FULL_B:3。"""
    def fb(job):
        r = full_b("WATCH")
        if job.job_id.endswith(":1"):
            card = consistent_card("WATCH")
            card["auto_buy_cap"] = 999.0   # 與 cap_calc 不一致 → 退回
            r["files"]["KO/signal_card.json"] = json.dumps(card)
            return r
        if job.job_id.endswith(":2"):
            r["files"] = {"KO/report.md": "# 修正版只寫了報告\n"}
        return r
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": fb})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    rows = [r["job_id"].split(":", 2)[2] for r in env.rows()]
    assert rows == ["FULL_A:1", "FULL_B:1", "FULL_B:2", "FULL_B:3"]
    assert json.loads(env.by_type("FULL_B")[-1]["params"]).get("output_retry") == 1
    td = env.paths.work / RUN / "KO"
    assert (td / "previous_2" / "signal_card.json").is_file()   # 上一版移到 previous_{序號}


def test_fulla_retry_missing_gate3_not_filled_by_previous(tmp_path):
    """FULL_A:2（D26 退回）漏寫 gate3_answers.json → 不得沿用 FULL_A:1 的作答（M5-D1-03 FULL_A 比照）。"""
    calls = []

    def fa(job):
        calls.append(job.job_id)
        r = full_a()
        if job.job_id.endswith(":2"):
            r["files"] = {"KO/report_A.md": "# A2\n"}
        return r
    env = make_env(tmp_path, {"FULL_A": fa})
    env.price_file("KO")
    td = env.paths.work / RUN / "KO"
    (td / "gate3_answers.json").write_text("{}", encoding="utf-8")   # 上一版留下的作答
    env.add(env.job("KO", "FULL_A", 2))
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()] == ["FULL_A"]           # 缺檔 → 不進 FULL_B
    assert not (td / "gate3_answers.json").is_file()
    assert (td / "previous_2" / "gate3_answers.json").is_file()


# ---------------------------------------------------------------- M5-D5-01／M5-D5-03：dq 與 RED_TEAM 輸入

def test_dq_json_staged_to_full_a_full_b_red_team_hold_review(env2):
    paths, conn = env2
    td = _td(paths)
    (td / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    (td / "dossier" / "KO_dq.json").write_text(json.dumps({"dq_checks": [{"warning": "w", "result": "數字有誤：11"}],
                                                           "missing": []}, ensure_ascii=False), encoding="utf-8")
    ctx = _ctx(paths, conn)
    j = _job("FULL_A")
    FullAHandler().prepare(j, ctx)
    assert "dossier/KO_dq.json" in _files(j.sandbox)
    for n in ("phaseA.json", "report_A.md", "gate3_result.json", "gate3_answers.json", "report.md",
              "signal_card.json", "cap_input.json", "cap_result.json"):
        (td / n).write_text("{}" if n.endswith(".json") else "x", encoding="utf-8")
    (td / "dossier" / "KO_price.json").write_text(json.dumps({"price": 60.5}), encoding="utf-8")
    for h, jt in ((FullBHandler(), "FULL_B"), (RedTeamHandler(), "RED_TEAM"), (HoldReviewHandler(), "HOLD_REVIEW")):
        j = _job(jt, {"created_ts": 4102444800.0})
        h.prepare(j, ctx)
        assert "dossier/KO_dq.json" in _files(j.sandbox), jt
    files = _files(j.sandbox)
    assert "dossier/KO_dq.json" in files
    vals = HoldReviewHandler().prompt_values(j, ctx, "claude_code")
    assert "KO_dq.json" in vals["files"]


def test_red_team_gets_number_inputs_and_framework_notes(env2):
    paths, conn = env2
    td = _td(paths)
    for n in ("dossier/KO.md", "report.md", "signal_card.json", "cap_input.json", "cap_result.json", "phaseA.json",
              "dossier/KO_price.json", "gate3_result.json", "dossier/KO_dq.json"):
        (td / n).write_text("{}" if n.endswith(".json") else "x", encoding="utf-8")
    j = _job("RED_TEAM", {"notes": ["框架覆寫 cap_input.price：60.0 → 60.5"]})
    RedTeamHandler().prepare(j, _ctx(paths, conn))
    files = _files(j.sandbox)
    for n in ("KO/cap_input.json", "KO/cap_result.json", "KO/phaseA.json", "KO/gate3_result.json",
              "dossier/KO_price.json", "dossier/KO_dq.json"):
        assert n in files
    suffix = RedTeamHandler().prompt_suffix(j)
    assert "框架已調整" in suffix and "60.5" in suffix and "number_errors" in suffix
    assert RedTeamHandler().prompt_suffix(_job("RED_TEAM")) == ""


# ---------------------------------------------------------------- M5-D5-07：觸發原因

def test_trigger_text_scrubs_prices_and_drops_price_category():
    params = {"reasons": ["[THESIS] 上次分析後有新季報（10-Q）",
                          "[THESIS] 單季自高點跌 ≥25%（2026-07-01 高點 70.00 → 2026-10-15 52.00）",
                          "[PRICE] 觀察名單現價 52.00 < 自動買進上限 55.0：快速通道重跑 G7–G11 與三閘門"],
              "thesis_result": {"verdict": "疑似", "reason": "毛利率連兩季下滑"}}
    t = trigger_text(params)
    assert "新季報（10-Q）" in t and "[THESIS]（細節含數字，略）" in t and "疑似" in t and "毛利率" in t
    assert "52.00" not in t and "70.00" not in t and "[PRICE]" not in t
    assert "[PRICE]（細節含數字，略）" in trigger_text(params, price_blind=False)
    assert "不是觸發複查" in trigger_text({})


def test_thesis_and_full_a_prompts_carry_trigger_without_prices(env2):
    paths, conn = env2
    ctx = _ctx(paths, conn)
    params = {"reasons": ["[THESIS] 較成本 −12%（成本 50.00 → 2026-10-15 44.00，-12.0%；論點破裂檢查，不是停損）",
                          "[FULL] 上次分析後有新年報（10-K）"]}
    j = _job("THESIS", params)
    h = ThesisHandler()
    h.prepare(j, ctx)
    prompt = build_prompt(j, "claude_code", 10 ** 9, paths.root, None, values=h.prompt_values(j, ctx, "claude_code"))
    assert "本次為觸發複查" in prompt and "新年報（10-K）" in prompt and "{trigger}" not in prompt
    assert "44.00" not in prompt and "50.00" not in prompt
    td = _td(paths)
    (td / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    j = _job("FULL_A", params)
    h = FullAHandler()
    h.prepare(j, ctx)
    prompt = build_prompt(j, "claude_code", 10 ** 9, paths.root, None, values=h.prompt_values(j, ctx, "claude_code"))
    assert "新年報（10-K）" in prompt and "44.00" not in prompt and "is_holding＝否" in prompt


# ---------------------------------------------------------------- M5_D10-01：TRIAGE 8-K 不進 DOSSIER 沙箱

class _Sec:
    def filing_text(self, url):
        return "During the quarter the Company repurchased 2,000,000 shares of common stock.\nItem 2.02 results.\n"


def test_triage_8k_not_staged_into_dossier(env2, monkeypatch):
    paths, conn = env2
    monkeypatch.setattr("trader.jobs.review_jobs.earnings_exhibit", lambda *a, **k: "")
    ctx = _ctx(paths, conn, sec=_Sec())
    tj = _job("TRIAGE", {"triage": [{"type": "8-K", "url": "u", "date": "2026-10-01", "items": "2.02"}]})
    TriageHandler().pre_script(tj, ctx)
    td = paths.work / "r1" / "KO"
    assert (td / "triage_sec" / "8-K_2026-10-01.txt").is_file() and not (td / "sec").exists()
    TriageHandler().prepare(tj, ctx)
    assert "sec/8-K_2026-10-01.txt" in _files(tj.sandbox)                 # TRIAGE 自己仍看得到
    # DOSSIER 前置完成：自己的原文列在 _index.json；舊版 TRIAGE 留在 sec/ 的檔也不 stage
    (td / "sec").mkdir()
    (td / "sec" / "10-K_2026-02-01_000001.txt").write_text("10-K\n", encoding="utf-8")
    (td / "sec" / "8-K_2026-09-01.txt").write_text("stray\n", encoding="utf-8")
    (td / "sec" / "_index.json").write_text(json.dumps([{"file": "10-K_2026-02-01_000001.txt", "form": "10-K"}]),
                                            encoding="utf-8")
    (td / "dossier").mkdir(exist_ok=True)
    (td / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    dj = _job("DOSSIER")
    DossierHandler().prepare(dj, ctx)
    assert [f for f in _files(dj.sandbox) if f.startswith("sec/")] == ["sec/10-K_2026-02-01_000001.txt"]


# ---------------------------------------------------------------- M5_D10-02：td 訊號卡＝實際回寫版

def test_td_signal_card_matches_written_card_after_downgrade(tmp_path):
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": full_b("BUY")})
    env.price_file("KO")
    _g8_view(env, 0.12)   # 最佳持股明顯勝出 → 框架 BUY → WATCH
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    td = env.paths.work / RUN / "KO"
    card = json.loads((td / "signal_card.json").read_text(encoding="utf-8"))
    model = json.loads((td / "signal_card.model.json").read_text(encoding="utf-8"))
    db_card = json.loads(env.conn.execute("SELECT data FROM signals").fetchone()["data"])
    assert card["signal"] == "WATCH" and model["signal"] == "BUY" and card == db_card


# ---------------------------------------------------------------- M5_D7_06：訊號卡 JSON 壞 → 退回

def test_broken_signal_card_json_retries_full_b(tmp_path):
    def fb(job):
        r = full_b("WATCH")
        if job.job_id.endswith(":1"):
            r["files"]["KO/signal_card.json"] = "{不是 JSON"
        return r
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": fb})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    fbs = env.by_type("FULL_B")
    assert len(fbs) == 2 and json.loads(fbs[1]["params"]).get("output_retry") == 1
    assert all(r["status"] == "done" for r in fbs)


# ---------------------------------------------------------------- M6-D10-04：P-31 判定只做一次、basis 一致

def test_rejected_driver_change_keeps_prior_basis():
    vocab = {"oil_price": "油價", "rates": "利率"}
    prev = {"external_drivers": ["oil_price"], "external_drivers_determined_on": "2026-01-10"}   # 舊列沒有 basis
    for proposal in ({"external_drivers": [{"driver": "rates", "kill_criterion": "十年債 >6%"}]},
                     {"external_drivers": ["oil_price"],   # 舊版 deep 的卡：ids 是既有、basis 是被駁回的提案
                      "external_drivers_basis": [{"driver": "rates", "kill_criterion": "十年債 >6%"}]}):
        entry, notes = {}, []
        corr.apply_drivers_to_entry(entry, prev, proposal, ASOF, vocab, notes)
        assert entry["external_drivers"] == ["oil_price"]
        assert {b["driver"] for b in entry.get("external_drivers_basis") or []} <= {"oil_price"}


def _registry_drivers(env, drivers, basis):
    db.insert(env.conn, "registry", {"ticker": "KO", "status": "WATCH", "updated_at": db.now_iso(), "data": json.dumps(
        {"ticker": "KO", "status": "WATCH", "external_drivers": drivers, "external_drivers_basis": basis,
         "external_drivers_determined_on": "2026-01-10"})})


def test_corr_writeback_rejected_card_basis_matches_registry(tmp_path):
    b = full_b("WATCH")
    b["external_drivers"] = [{"driver": "rates", "kill_criterion": "毛利率連兩季下滑"}]
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": b})
    env.price_file("KO")
    _registry_drivers(env, ["oil_price"], [{"driver": "oil_price", "kill_criterion": "油價 <40"}])
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    card = json.loads(env.conn.execute("SELECT data FROM signals").fetchone()["data"])
    reg = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])
    assert card["external_drivers"] == ["oil_price"] == reg["external_drivers"]
    assert card["external_drivers_basis"] == reg["external_drivers_basis"] == [{"driver": "oil_price",
                                                                                "kill_criterion": "油價 <40"}]


def test_accepted_driver_change_note_only_once(tmp_path):
    b = full_b("WATCH")
    b["external_drivers"] = [{"driver": "rates", "kill_criterion": "毛利率連兩季下滑"}]
    b["external_drivers_change_reason"] = "2026-09 新債務條款改為浮動利率（10-Q 2026-08-01）"
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": b})
    env.price_file("KO")
    _registry_drivers(env, ["oil_price"], [{"driver": "oil_price", "kill_criterion": "油價 <40"}])
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    log = json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()["data"])
    assert sum("外部變數改判" in n for n in log.get("framework_notes") or []) == 1


# ---------------------------------------------------------------- D30：持股版 FULL_A

def test_holding_full_a_gate_fail_continues_to_valuation_without_adding(tmp_path):
    a = full_a()
    a["holding_stop"] = {"gate": "G3", "reason_code": "REJECT", "reason": "護城河轉弱"}
    env = make_env(tmp_path, {"FULL_A": a, "FULL_B": full_b("BUY"), "RED_TEAM": red_team(False),
                              "HOLD_REVIEW": hold()})
    _holding(env)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()] == ["FULL_A", "FULL_B", "HOLD_REVIEW"]   # 不跑 RED_TEAM
    full = env.conn.execute("SELECT signal, data FROM decisions WHERE json_extract(data,'$.review_type') IS NULL"
                            ).fetchone()
    log = json.loads(full["data"])
    assert full["signal"] == "WATCH"
    assert log["hold_flags"] == {"stopped_at_gate": "G3", "reason_code": "REJECT"}
    assert log["gate3_pass"] is False and any("D30" in f for f in log["gate3_failures"])
    hr = json.loads(env.by_type("HOLD_REVIEW")[0]["params"])
    assert hr["full_stage"] == "B"
    td = env.paths.work / RUN / "KO"
    assert (td / "cap_result.json").is_file()   # H2 拿得到重算的 IV_base
    reg = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])
    # D16（P45）：持股完整複查另立新版論點檔，predecessor 指舊檔；舊檔原封不動
    assert reg["thesis_file"] == f"theses/KO_{ASOF}_v2.md"
    assert "predecessor: theses/KO_2024-01-02.md" in (env.paths.state / reg["thesis_file"]).read_text(encoding="utf-8")
    assert (env.paths.theses / "KO_2024-01-02.md").read_text(encoding="utf-8") == "# 原論點\n"


def test_full_a_prompt_has_holding_branch(env2):
    paths, conn = env2
    td = _td(paths)
    (td / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    ctx = _ctx(paths, conn)
    j = _job("FULL_A", {"holding": True})
    h = FullAHandler()
    h.prepare(j, ctx)
    prompt = build_prompt(j, "claude_code", 10 ** 9, paths.root, None, values=h.prompt_values(j, ctx, "claude_code"))
    assert "is_holding＝是" in prompt and "holding_stop" in prompt and "{is_holding}" not in prompt


# ---------------------------------------------------------------- D48：代理式提示（正式路徑）與 API 路徑標示

def test_agentic_prompt_does_not_build_sections(env2, monkeypatch):
    paths, conn = env2
    td = _td(paths)
    (td / "dossier" / "KO.md").write_text("# KO\nBODY_MARKER\n", encoding="utf-8")
    ctx = _ctx(paths, conn)

    def boom(*a, **k):
        raise AssertionError("代理式後端不應組 API 段落")
    monkeypatch.setattr(deep, "_skill_sections", boom)
    j = _job("FULL_A")
    h = FullAHandler()
    secs = h.prepare(j, ctx)
    prompt = build_prompt(j, "claude_code", 10 ** 9, paths.root, secs, values=h.prompt_values(j, ctx, "claude_code"))
    sb = str(j.sandbox)
    assert f"{sb}/dossier/KO.md" in prompt                     # {work} 為沙箱絕對路徑
    assert "- dossier/KO.md" in prompt and "BODY_MARKER" not in prompt
    assert '"KO/report_A.md"' in prompt                         # files 範例帶 {ticker}/（M5_D10-08）
    assert "## 輸出檔案" not in prompt                          # API 的 files 說明不出現


def test_api_prompt_marks_dropped_sections(env2):
    paths, conn = env2
    td = _td(paths)
    (td / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    ctx = _ctx(paths, conn)
    j = _job("FULL_A")
    h = FullAHandler()
    secs = h.prepare(j, ctx)
    vals = h.prompt_values(j, ctx, "openai_compat")
    full = build_prompt(j, "openai_compat", 10 ** 9, paths.root, secs, values=vals)
    assert "已刪段落" not in full and '"KO/report_A.md"' in full
    from trader.jobs.context import estimate_tokens
    knowledge = [s for s in secs() if s.droppable == 2]
    cut = build_prompt(j, "openai_compat", estimate_tokens(full) - estimate_tokens(knowledge[0].text) // 2 - 50,
                       paths.root, secs, values=vals)
    assert "## 已刪段落" in cut and knowledge[0].name in cut.split("## 已刪段落")[1]
