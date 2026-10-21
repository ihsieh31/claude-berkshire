"""P33（M5-B5 review 端）：PRICE 利率缺註記與升級、THESIS 申報篇幅分配／未附清單／未抓取狀態與 last_review_date、
失效條件與論點檔價格清洗、HOLD_REVIEW 輸入完整性與登錄表觀察期停止加碼、TRIAGE／CORP_ACTION 截斷標示。"""
from __future__ import annotations

import json
from pathlib import Path

from test_hold_review import hold
from test_review_jobs import (ASOF, FakeSec, _ctx, _job, _price_params, _price_result, _reg, _seed_registry,
                              _write_cap_input, env, thesis_result)  # noqa: F401 - env 為夾具

from trader import db
from trader.analysis.dossier_prep import scrub_price_sentences
from trader.jobs.context import build_prompt
from trader.jobs.corp_jobs import CORP_8K_CHARS, CorpActionHandler
from trader.jobs.hold_jobs import HoldReviewHandler
from trader.jobs.review_jobs import (TRIAGE_8K_CHARS, PriceHandler, ThesisHandler, TriageHandler,
                                     _kill_criteria)


def _sb_files(job) -> list[str]:
    sb = Path(job.sandbox)
    return sorted(p.relative_to(sb).as_posix() for p in sb.rglob("*")
                  if p.is_file() and not p.relative_to(sb).as_posix().startswith("pkg/"))


# ---------------------------------------------------------------- M5-D5-02（PRICE 利率缺）

def test_price_missing_t30_notes_and_escalates_without_writeback(env):
    paths, conn = env
    _write_cap_input(paths, extra={"long_rate": 0.031, "long_rate_20y_med": 0.035})
    _seed_registry(conn, "KO", "WATCH", {"t30_at_valuation": 0.031, "last_review_date": "2026-01-01"})
    job = _job("PRICE", _price_params(rates=None))
    h, ctx = PriceHandler(), _ctx(paths, conn)
    h.pre_script(job, ctx)
    cap = json.loads((paths.work / "r1" / "KO" / "cap_result.json").read_text(encoding="utf-8"))
    assert any(n.startswith("長債缺") and "0.031" in n for n in cap["framework_notes"])
    assert cap["rates_stale"] is True and cap["inputs_used"]["long_rate"] == 0.031
    nxt = h.post(job, _price_result(), ctx)
    assert [j.job_type for j in nxt] == ["DOSSIER"]
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0   # 未回寫 PRICE 結果
    assert _reg(conn)["data"]["last_review_date"] == "2026-01-01"


def test_price_with_rates_exposes_inputs_used(env):
    paths, conn = env
    _write_cap_input(paths)
    job = _job("PRICE", _price_params())
    PriceHandler().pre_script(job, _ctx(paths, conn))
    cap = json.loads((paths.work / "r1" / "KO" / "cap_result.json").read_text(encoding="utf-8"))
    assert cap["inputs_used"]["long_rate"] == 0.047 and cap["inputs_used"]["largecap_pe_median"] == 19.0
    assert "rates_stale" not in cap


# ---------------------------------------------------------------- M5-D5-06（THESIS 篇幅）

def _budget_sec():
    rows = [{"form": "10-Q", "filingDate": "2026-08-01", "url": "u-q", "accessionNumber": "0001-26-000001"}]
    texts = {"u-q": "Item 2. Management's Discussion\nKEY FACT: retention fell.\n" + "a" * 39000}
    for i in range(5):
        u = f"u-8k-{i}"
        rows.append({"form": "8-K", "filingDate": f"2026-09-0{i + 1}", "url": u,
                     "accessionNumber": f"0001-26-00001{i}", "items": "8.01"})
        texts[u] = "routine " + "b" * 39990
    rows.append({"form": "8-K", "filingDate": "2026-09-20", "url": "u-ex", "accessionNumber": "0001-26-000099",
                 "items": "9.01"})
    texts["u-ex"] = "exhibit only"
    return FakeSec(rows, texts)


def test_thesis_budget_prioritises_10q_lists_omitted_and_partially_advances(env):
    paths, conn = env
    _seed_registry(conn, "KO", "HOLDING", {"last_review_date": "2026-07-01"})
    job = _job("THESIS", _price_params(last_review_date="2026-07-01", holding=True))
    h, ctx = ThesisHandler(), _ctx(paths, conn, sec=_budget_sec())
    h.pre_script(job, ctx)
    out = paths.work / "r1" / "KO" / "thesis_sec"
    names = sorted(p.name for p in out.glob("*.txt"))
    assert any(n.startswith("10-Q_2026-08-01") for n in names)
    assert not any("2026-09-20" in n for n in names)            # 只有 9.01 的 8-K 不附
    omitted = (out / "_omitted.txt").read_text(encoding="utf-8")
    assert all(f"u-8k-{i}" in omitted for i in (2, 3, 4)) and "u-8k-1" not in omitted and "u-q" not in omitted
    h.prepare(job, ctx)
    assert "thesis_sec/_omitted.txt" in _sb_files(job)
    vals = h.prompt_values(job, ctx, "claude_code")
    assert "另有 3 份" in vals["sec_status"]
    assert "另有 3 份" in build_prompt(job, "claude_code", 10 ** 9, paths.root, [], values=vals)
    h.post(job, thesis_result("完好"), ctx)
    assert _reg(conn)["data"]["last_review_date"] == "2026-09-03"   # 只推進到最早未附者


def test_thesis_all_attached_advances_to_asof(env):
    paths, conn = env
    _seed_registry(conn, "KO", "HOLDING", {"last_review_date": "2026-07-01"})
    sec = FakeSec([{"form": "10-Q", "filingDate": "2026-08-01", "url": "u", "accessionNumber": "1"}], {"u": "short"})
    job = _job("THESIS", _price_params(last_review_date="2026-07-01", holding=True))
    h, ctx = ThesisHandler(), _ctx(paths, conn, sec=sec)
    h.pre_script(job, ctx)
    assert h.prompt_values(job, ctx, "api")["sec_status"].startswith("已抓取")
    h.post(job, thesis_result("完好"), ctx)
    assert _reg(conn)["data"]["last_review_date"] == ASOF


# ---------------------------------------------------------------- M5_D10-07（未抓取）

def test_thesis_without_sec_writes_status_and_keeps_last_review_date(env):
    paths, conn = env
    _seed_registry(conn, "KO", "HOLDING", {"last_review_date": "2026-07-01"})
    job = _job("THESIS", _price_params(last_review_date="2026-07-01", holding=True))
    h, ctx = ThesisHandler(), _ctx(paths, conn, sec=None)
    h.pre_script(job, ctx)
    h.prepare(job, ctx)
    assert "thesis_sec/_status.txt" in _sb_files(job)
    vals = h.prompt_values(job, ctx, "claude_code")
    assert vals["sec_status"].startswith("未抓取") and "至少「疑似」" in vals["sec_status"]
    h.post(job, thesis_result("完好"), ctx)
    assert _reg(conn)["data"]["last_review_date"] == "2026-07-01"


def test_thesis_without_cik_writes_status(env):
    paths, conn = env

    class NoCik(FakeSec):
        def cik_for(self, ticker):
            return None

    job = _job("THESIS", _price_params(last_review_date="2026-07-01"))
    ThesisHandler().pre_script(job, _ctx(paths, conn, sec=NoCik()))
    assert "CIK" in (paths.work / "r1" / "KO" / "thesis_sec" / "_status.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- M5_D6-04（價格清洗）

def test_kill_criteria_scrubbed_in_triage_and_thesis_prompts(env):
    paths, conn = env
    kc = ["營收連兩年衰退", "價格 70.39 跌破時重估", "Price 70.39 below cap"]
    assert _kill_criteria({"kill_criteria": kc}) == ["營收連兩年衰退", "（含價格數字，已刪）", "（含價格數字，已刪）"]
    for h, jt in ((TriageHandler(), "TRIAGE"), (ThesisHandler(), "THESIS")):
        job = _job(jt, _price_params(kill_criteria=kc))
        assert "70.39" not in h.prompt_values(job, _ctx(paths, conn), "api")["kill_criteria"]


def test_thesis_file_price_phrasings_removed():
    samples = ["決策日價格 70.39，低於自動買進上限 75.10。", "Price 70.39 vs IV_cons 88.0 (price/IV 0.80).",
               "首批於 72 美元以下買進 30%。", "安全邊際 20%（以決策日價格計）。", "買進價 70.39；P/IV＝0.80。", "現價 70.39。"]
    for s in samples:
        out, n = scrub_price_sentences(s)
        assert n >= 1 and "70.39" not in out and "72 美元" not in out, s
    keeps = ["營收成長 5%，營業利益率 20%。",
             # P33R：財務／營運數字不是股價，不得刪（check-P33 在申報語料重掃的誤刪類型）
             "Average selling price $542 (5)% $570", "Total purchase price $1,624,220", "Purchase price $2,270",
             "Price 4 % 2 %", "Revenue grew 6%, driven by price 4% and volume 2%.", "Fixed-price 2,600 2,369",
             "提價能力：價格 5% 調漲後客戶未流失。", "原料成本價格上漲，毛利率承壓。"]
    for keep in keeps:
        assert scrub_price_sentences(keep) == (keep, 0), keep


# ---------------------------------------------------------------- M5-D5-04（HOLD_REVIEW 輸入）

def test_hold_review_after_red_team_gets_red_team_json_and_dossier(env):
    paths, conn = env
    td = paths.work / "r1" / "KO"
    (td / "dossier").mkdir(parents=True)
    for n in ("phaseA.json", "report.md", "signal_card.json", "cap_result.json", "red_team.json", "dossier/KO.md"):
        (td / n).write_text("{}" if n.endswith(".json") else "x", encoding="utf-8")
    (td / "cap_input.json").write_text(json.dumps({"long_rate": 0.047, "largecap_pe_median": 19.0}), encoding="utf-8")
    _seed_registry(conn, "KO", "HOLDING", {"t30_at_valuation": 0.045, "review_price": 60.0})
    job = _job("HOLD_REVIEW", {"full_stage": "B", "final_signal": "WATCH", "holding": True})
    h, ctx = HoldReviewHandler(), _ctx(paths, conn)
    h.prepare(job, ctx)
    files = _sb_files(job)
    assert "KO/red_team.json" in files and "dossier/KO.md" in files
    vals = h.prompt_values(job, ctx, "claude_code")
    assert "紅隊" in vals["full_status"]
    assert "t30 0.047" in vals["valuation_inputs"] and "t30_at_valuation 0.045" in vals["valuation_inputs"]
    assert "P/E 中位 19.0" in vals["valuation_inputs"]


def test_hold_review_price_stage_stages_last_full_card_and_registry(env):
    paths, conn = env
    _seed_registry(conn, "KO", "HOLDING", {"observe_until": None, "t30_at_valuation": 0.045})
    db.insert(conn, "decisions", {"decision_id": "KO-20260601-1", "ticker": "KO", "decision_date": "2026-06-01",
                                  "run_id": "r0", "job_id": None, "signal": "HOLD", "reason_code": None,
                                  "data": {"action": "HOLD"}, "created_at": db.now_iso()})
    db.insert(conn, "signals", {"decision_id": "KO-20260601-1", "ticker": "KO", "signal": "HOLD",
                                "data": {"signal": "HOLD", "iv_base": 80.0}, "created_at": db.now_iso()})
    job = _job("HOLD_REVIEW", {"full_stage": "PRICE", "price": 90.0, "holding": True})
    h, ctx = HoldReviewHandler(), _ctx(paths, conn)
    h.prepare(job, ctx)
    sb = Path(job.sandbox)
    card = json.loads((sb / "last_full_signal_card.json").read_text(encoding="utf-8"))
    assert card["decision_date"] == "2026-06-01" and card["iv_base"] == 80.0
    assert json.loads((sb / "registry_entry.json").read_text(encoding="utf-8"))["t30_at_valuation"] == 0.045
    assert "last_full_signal_card.json" in h.prompt_values(job, ctx, "api")["files"]


# ---------------------------------------------------------------- 附帶 (b)：登錄表觀察期

def _post_hold(paths, conn, monkeypatch, reg_data):
    seen = {}
    from trader.analysis import writeback
    monkeypatch.setattr(writeback, "write_hold_review_result",
                        lambda *a, **k: seen.update(result=a[5], notes=a[7]))
    _seed_registry(conn, "KO", "HOLDING", reg_data)
    r = {**hold(stop=False), "observe_until": None}
    HoldReviewHandler().post(_job("HOLD_REVIEW", {"full_stage": "PRICE"}), r, _ctx(paths, conn))
    return seen


def test_hold_registry_observe_until_in_future_forces_stop_adding(env, monkeypatch):
    paths, conn = env
    seen = _post_hold(paths, conn, monkeypatch, {"observe_until": "2027-01-31"})
    assert seen["result"]["stop_adding"] is True and any("登錄表觀察期" in n for n in seen["notes"])


def test_hold_registry_observe_until_expired_keeps_stop_adding_false(env, monkeypatch):
    paths, conn = env
    seen = _post_hold(paths, conn, monkeypatch, {"observe_until": ASOF})
    assert seen["result"]["stop_adding"] is False


# ---------------------------------------------------------------- TRIAGE／CORP_ACTION 截斷標示

def test_triage_and_corp_truncation_marked(env):
    paths, conn = env
    long = "x" * (TRIAGE_8K_CHARS + CORP_8K_CHARS + 10)
    sec = FakeSec(texts={"u": long})
    job = _job("TRIAGE", _price_params(triage=[{"type": "8-K", "date": "2026-10-10", "items": "8.01", "url": "u"}]))
    TriageHandler().pre_script(job, _ctx(paths, conn, sec=sec))
    txt = (paths.work / "r1" / "KO" / "triage_sec" / "8-K_2026-10-10.txt").read_text(encoding="utf-8")
    assert f"原長 {len(long)} 字" in txt
    cj = _job("CORP_ACTION", {"event": {"ticker": "KO", "source": "8k", "url": "u", "text": "事件"}})
    CorpActionHandler().pre_script(cj, _ctx(paths, conn, sec=sec))
    assert "原文已截斷" in cj.params["event"]["text"]
