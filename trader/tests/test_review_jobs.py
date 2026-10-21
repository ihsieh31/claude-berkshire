"""觸發檢查處理器（review_jobs）與 writeback 的 M4 新函式測試。

不連網：SEC 用假物件；提示與沙箱直接以 handler 方法驅動；寫入真的 SQLite（每個測試獨立 state 目錄）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from trader import db
from trader.analysis import writeback
from trader.analysis.checks import run_cap_calc
from trader.jobs.context import build_prompt
from trader.jobs.handlers import HANDLERS, HandlerCtx
from trader.jobs.review_jobs import PriceHandler, ThesisHandler, TriageHandler
from trader.jobs.validate import load_schemas
from trader.models import JobSpec
from trader.paths import get_paths

ASOF = "2026-10-16"
PRICE_NOW = 60.5
CAP_INPUT = json.loads((get_paths().skill_deep / "assets" / "cap_calc_example_input.json").read_text(encoding="utf-8"))
PRICE_KEYS_IN_JSON = ("price", "price_date", "market_cap", "auto_buy_cap", "sell_review_above", "absurd_price_line")


# ---------------------------------------------------------------- 夾具與假物件

class FakeSec:
    """假 SEC 客戶端：申報清單與全文由測試給定；不連網。"""
    user_agent = "Test Runner test@example.com"

    def __init__(self, rows=None, texts=None):
        self.rows = rows or []
        self.texts = texts or {}

    def cik_for(self, ticker):
        return "0000000001"

    def filings(self, cik, asof, since=None, forms=None):
        out = [f for f in self.rows
               if f["filingDate"] < asof and (since is None or f["filingDate"] > since)
               and (forms is None or f["form"] in forms)]
        return sorted(out, key=lambda f: f["filingDate"])

    def filing_text(self, url):
        return self.texts[url]


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield paths, conn
    conn.close()


def _ctx(paths, conn, sec=None) -> HandlerCtx:
    return HandlerCtx(paths=paths, schemas=load_schemas(paths.schemas_json), work_dir=paths.work, conn=conn,
                      sec=sec)


def _job(job_type: str, params: dict | None = None, ticker: str = "KO", n: int = 1) -> JobSpec:
    return JobSpec(job_id=f"r1:{ticker}:{job_type}:{n}", run_id="r1", job_type=job_type, ticker=ticker,
                   asof=ASOF, params=params or {})


def _seed_registry(conn, ticker: str, status: str, data: dict) -> None:
    db.upsert(conn, "registry", {"ticker": ticker, "status": status, "data": data, "updated_at": db.now_iso()},
              ["ticker"])


def _reg(conn, ticker: str = "KO") -> dict:
    row = conn.execute("SELECT status, data FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    return {"status": row["status"], "data": json.loads(row["data"])}


def _decisions(conn, ticker: str = "KO") -> list[dict]:
    rows = conn.execute("SELECT * FROM decisions WHERE ticker = ? ORDER BY rowid", (ticker,)).fetchall()
    return [{**dict(r), "data": json.loads(r["data"])} for r in rows]


def _write_cap_input(paths, ticker: str = "KO", extra: dict | None = None) -> Path:
    d = paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    x = {**CAP_INPUT, **(extra or {})}
    p = d / f"{ticker}.json"
    p.write_text(json.dumps(x, ensure_ascii=False), encoding="utf-8")
    return p


def _auto_cap() -> float:
    """以 cap_calc 實算 KO 的自動買進上限（示例輸入、價格 60）。"""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "c.json"
        p.write_text(json.dumps(CAP_INPUT), encoding="utf-8")
        return float(run_cap_calc(p)["auto_buy_cap"])


def _price_params(**kw) -> dict:
    p = {"price": PRICE_NOW, "price_date": "2026-10-15", "rates": {"t30": 0.047, "t30_20y_median": 0.035},
         "largecap_pe": {"pe_norm_median": 19.0, "pe_ttm_median": 19.0, "fin_pe_norm_median": 15.0},  # D60：取 pe_ttm_median（P19）
         "portfolio_state": {"cash": 100000, "holdings": []}, "screen_context": {"screen_decision": "PASS"}}
    p.update(kw)
    return p


def _gate3_row(conn, decision_date: str = ASOF, passed: bool = True) -> None:
    db.insert(conn, "decisions", {
        "decision_id": f"KO-{decision_date.replace('-', '')}-1", "ticker": "KO", "decision_date": decision_date,
        "run_id": "r0", "job_id": None, "signal": "WATCH", "reason_code": "TOO_HARD",
        "data": {"action": "WATCH", "gate3_pass": passed}, "created_at": db.now_iso()})


def _run_price(paths, conn, params: dict, result: dict, n: int = 1):
    """PRICE 的 pre_script → post（不經引擎）。回傳 (job, 後續工作清單)。同一測試跑第二個 PRICE 工作時給不同的 n：
    同一 job_id 重放 post 只回寫一次（M6-D9-03）。"""
    job = _job("PRICE", params, n=n)
    h, ctx = PriceHandler(), _ctx(paths, conn)
    h.pre_script(job, ctx)
    nxt = h.post(job, result, ctx)
    return job, nxt


def _price_result(**kw) -> dict:
    r = {"ticker": "KO", "price": 999.0, "auto_buy_cap": 999.0, "sell_review_above": 1.0, "absurd_price_line": 1.0,
         "gates": {"gate1_screen": "通過", "gate2_price_lt_cap": "通過", "gate3_on_file": "通過"},
         "proposed_action": "NO_CHANGE", "needs_full": False, "g9_contrarian": "反方：估值已不便宜", "g10_still_valid": True, "reason": "測試"}
    r.update(kw)
    return r


def _sandbox_texts(sb: Path) -> dict[str, str]:
    return {p.relative_to(sb).as_posix(): p.read_text(encoding="utf-8")
            for p in sb.rglob("*") if p.is_file() and p.suffix in (".txt", ".md", ".json")}


def _assert_price_free(sb: Path) -> None:
    """沙箱內（不含 pkg/ 規則副本）不得有價格檔、價格鍵、價格數字或價格字句。"""
    assert list(sb.rglob("*_price.json")) == []
    for name, text in _sandbox_texts(sb).items():
        if name.startswith("pkg/"):  # 規則副本是 skill 原文（描述規則，不含本檔價格）；JSON 鍵另行檢查
            if name.endswith(".json"):
                assert not any(k in PRICE_KEYS_IN_JSON for k in _keys(json.loads(text))), name
            continue
        assert str(PRICE_NOW) not in text, name
        assert "19.0" not in text, name
        low = text.lower()
        assert "closing price" not in low and "share price" not in low and "stock price" not in low, name
        if name.endswith(".json"):
            assert not any(k in PRICE_KEYS_IN_JSON for k in _keys(json.loads(text))), name


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


# ---------------------------------------------------------------- 處理器登錄

def test_handlers_registry_uses_review_classes():
    assert type(HANDLERS["TRIAGE"]) is TriageHandler
    assert type(HANDLERS["PRICE"]) is PriceHandler
    assert type(HANDLERS["THESIS"]) is ThesisHandler


# ---------------------------------------------------------------- 價格隔離

def test_triage_sandbox_has_no_price(env):
    paths, conn = env
    url = "https://www.sec.gov/Archives/edgar/data/1/a/doc.htm"
    sec = FakeSec(texts={url: "Item 8.01 Other events.\nClosing price of $60.5 on Oct 15.\nRevenue rose 5%."})
    params = _price_params(triage=[{"type": "8-K", "date": "2026-10-01", "items": "8.01", "url": url}],
                           kill_criteria=["毛利率連兩季下滑"], base_action="NONE", thesis_file=None)
    job = _job("TRIAGE", params)
    ctx = _ctx(paths, conn, sec)
    h = TriageHandler()
    h.pre_script(job, ctx)
    sections = h.prepare(job, ctx)

    _assert_price_free(job.sandbox)
    texts = _sandbox_texts(job.sandbox)
    assert any(n.startswith("sec/8-K_2026-10-01") for n in texts)
    assert "Revenue rose 5%." in "".join(texts.values())
    prompt = build_prompt(job, "claude_code", 10**9, paths.root, sections,
                          values=h.prompt_values(job, ctx, "claude_code"))
    assert "{triage_items}" not in prompt and "{kill_criteria}" not in prompt
    assert "毛利率連兩季下滑" in prompt
    assert "60.5" not in prompt


def test_thesis_sandbox_has_no_price_and_mdna_only_for_10q(env):
    paths, conn = env
    theses = paths.theses
    theses.mkdir(parents=True, exist_ok=True)
    (theses / "KO_2026-01-01.md").write_text("# 論點\n share price target $60.5 出現在此。\n護城河：品牌。\n",
                                             encoding="utf-8")
    q_url = "https://www.sec.gov/Archives/edgar/data/1/q/q.htm"
    k_url = "https://www.sec.gov/Archives/edgar/data/1/k/k.htm"
    q_text = ("Item 1. Financial Statements\nFINSTMT_MARKER\n"
              "Item 2. Management's Discussion and Analysis\nMDA_MARKER 毛利率下滑。\n"
              "Item 3. Market Risk\nRISK_MARKER\nClosing price $60.5 此行應被刪除。\n")
    sec = FakeSec(rows=[{"form": "10-Q", "filingDate": "2026-10-01", "url": q_url, "accessionNumber": "0000000001-26-000001"},
                        {"form": "8-K", "filingDate": "2026-09-20", "url": k_url, "accessionNumber": "0000000001-26-000002"}],
                  texts={q_url: q_text, k_url: "Item 8.01\nstock price fell.\nNew contract signed.\n"})
    job = _job("THESIS", _price_params(last_review_date="2026-01-01", kill_criteria=["毛利率連兩季下滑"]))
    ctx = _ctx(paths, conn, sec)
    h = ThesisHandler()
    h.pre_script(job, ctx)
    sections = h.prepare(job, ctx)

    _assert_price_free(job.sandbox)
    texts = _sandbox_texts(job.sandbox)
    q_name = next(n for n in texts if n.startswith("thesis_sec/10-Q_2026-10-01"))
    assert "MDA_MARKER" in texts[q_name] and "FINSTMT_MARKER" not in texts[q_name]
    assert "Closing price" not in texts[q_name]
    assert "New contract signed." in "".join(texts.values())
    assert any(n.startswith("thesis/KO_2026-01-01") for n in texts)
    prompt = build_prompt(job, "openai_compat", 10**9, paths.root, sections, values=h.prompt_values(job, ctx, "openai_compat"))
    assert "{thesis_file}" not in prompt and "2026-01-01" in prompt
    assert "60.5" not in prompt


def test_price_sandbox_gets_cap_result_with_recalculated_numbers(env):
    paths, conn = env
    _write_cap_input(paths)
    job = _job("PRICE", _price_params())
    ctx = _ctx(paths, conn)
    h = PriceHandler()
    h.pre_script(job, ctx)
    h.prepare(job, ctx)

    cap = json.loads((job.sandbox / "cap_result.json").read_text(encoding="utf-8"))
    assert cap["price"] == PRICE_NOW
    assert cap["auto_buy_cap"] == pytest.approx(_auto_cap())
    assert cap["absurd_price_line"] == pytest.approx(50 * CAP_INPUT["norm_eps"])
    # 只更新 price、long_rate、PE 中位：寫在工作目錄；state 的原輸入不動
    used = json.loads((ctx.work_dir / job.run_id / "KO" / "cap_input.json").read_text(encoding="utf-8"))
    assert used["long_rate"] == 0.047 and used["long_rate_20y_med"] == 0.035
    assert used["largecap_pe_median"] == 19.0 and used["fin_pe_median"] == 15.0
    assert used["oe"] == CAP_INPUT["oe"] and used["price"] == PRICE_NOW
    assert json.loads((paths.state / "cap_inputs" / "KO.json").read_text(encoding="utf-8")) == CAP_INPUT


# ---------------------------------------------------------------- TRIAGE 升級與回寫

def test_triage_escalates_to_thesis_and_copies_params(env):
    paths, conn = env
    job = _job("TRIAGE", _price_params(holding=True, created_ts=1.0, base_action="PRICE"))
    h = TriageHandler()
    nxt = h.post(job, {"ticker": "KO", "items": [], "escalate": True}, _ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["THESIS"]
    t = nxt[0]
    assert t.deps == [job.job_id]
    assert t.params["holding"] is True and t.params["price"] == PRICE_NOW  # 完整副本
    assert t.params["created_ts"] == 1.0  # THESIS 例外：保留 created_ts


def test_triage_not_escalated_base_price_goes_to_price(env):
    paths, conn = env
    h = TriageHandler()
    job = _job("TRIAGE", _price_params(base_action="PRICE"))
    _write_cap_input(paths)
    nxt = h.post(job, {"escalate": False}, _ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["PRICE"]
    assert nxt[0].params["price"] == PRICE_NOW


def test_triage_base_price_without_cap_inputs_becomes_dossier(env):
    paths, conn = env
    job = _job("TRIAGE", _price_params(base_action="PRICE", created_ts=5.0))
    nxt = TriageHandler().post(job, {"escalate": False}, _ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["DOSSIER"]
    assert nxt[0].params.get("created_ts") != 5.0  # 非 PRICE／THESIS：由 next_job 重設


def test_triage_not_escalated_touches_review_date_only(env):
    paths, conn = env
    _seed_registry(conn, "KO", "WATCH", {"cik": "0000000001", "auto_buy_cap": 80.0, "last_review_date": "2026-01-01",
                                         "thesis_file": "x.md"})
    job = _job("TRIAGE", _price_params(base_action="NONE"))
    assert TriageHandler().post(job, {"escalate": False}, _ctx(paths, conn)) == []
    reg = _reg(conn)
    assert reg["status"] == "WATCH"
    assert reg["data"] == {"cik": "0000000001", "auto_buy_cap": 80.0, "last_review_date": ASOF, "thesis_file": "x.md"}


def test_triage_invalid_escalates_with_no_deps(env):
    paths, conn = env
    job = _job("TRIAGE", _price_params(holding=False))
    nxt = TriageHandler().on_invalid(job, ["缺少必填鍵"], _ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["THESIS"] and nxt[0].deps == []
    assert nxt[0].params["triage_invalid"] is True


def test_upgrade_chain_thesis_to_dossier_resets_created_ts(env):
    paths, conn = env
    ctx = _ctx(paths, conn)
    tri = _job("TRIAGE", {"created_ts": 1.0})
    th = TriageHandler().post(tri, {"escalate": True}, ctx)[0]
    nxt = ThesisHandler().post(th, thesis_result("疑似"), ctx)
    assert [j.job_type for j in nxt] == ["DOSSIER"]
    assert nxt[0].deps == [th.job_id]
    assert nxt[0].params.get("created_ts") != 1.0  # DOSSIER 不保留，由 next_job 重設


# ---------------------------------------------------------------- THESIS 回寫

def thesis_result(verdict: str, *, hit: bool = False, falsify: bool = False) -> dict:
    r = {"ticker": "KO", "verdict": verdict,
         "kill_criteria_status": [{"criterion": "毛利率連兩季下滑",
                                   "status": "已觸發" if hit else "未觸發", "evidence": "10-Q 2026-10-01"}],
         "r_list_status": [{"item": "R1", "status": "無變化", "evidence": "測試"}], "new_facts": ["新事實 A"], "reason": "測試理由"}
    if falsify:   # D02：證偽附理由類型、K 編號與證據（日期早於 ASOF），框架核對才會通過
        r.update(falsify_type="CONDITION_TRIGGERED", condition_id="K1",
                 evidence=[{"source": "10-Q 2026-10-01", "date": "2026-10-01"}])
    return r


def test_thesis_watch_for_non_holding_and_merges_registry(env):
    paths, conn = env
    _seed_registry(conn, "KO", "WATCH", {"cik": "0000000001", "auto_buy_cap": 80.0, "grade": "B",
                                         "last_review_date": "2026-01-01", "review_price": 50.0})
    job = _job("THESIS", _price_params(last_review_date="2026-01-01"))
    ThesisHandler().post(job, thesis_result("完好"), _ctx(paths, conn))

    dec = _decisions(conn)
    assert len(dec) == 1 and dec[0]["data"]["action"] == "WATCH" and dec[0]["data"]["thesis_check"] == "完好"
    reg = _reg(conn)
    assert reg["data"]["auto_buy_cap"] == 80.0 and reg["data"]["grade"] == "B"  # 既有欄位保留
    assert reg["data"]["last_review_date"] == ASOF and reg["data"]["review_price"] == PRICE_NOW
    assert reg["status"] == "WATCH"


@pytest.mark.parametrize("verdict,expected,falsify", [
    pytest.param("完好", "HOLD", False, id="完好-HOLD"), pytest.param("疑似", "HOLD", False, id="疑似-HOLD"),
    # 證偽且核對通過（失效條件 K1 已觸發、附來源與日期）→ SELL_ALL；缺 falsify 欄位 → D02 降為疑似（HOLD）
    pytest.param("證偽", "SELL_ALL", True, id="證偽-SELL_ALL"),
    pytest.param("證偽", "HOLD", False, id="證偽未附核對欄位-HOLD")])
def test_thesis_holding_action_mapping(env, verdict, expected, falsify):
    paths, conn = env
    _seed_registry(conn, "KO", "HOLDING", {"cost": 100.0, "weight": 0.05})
    job = _job("THESIS", _price_params(kill_criteria=["毛利率連兩季下滑"]))
    ThesisHandler().post(job, thesis_result(verdict, hit=falsify, falsify=falsify), _ctx(paths, conn))
    assert _decisions(conn)[0]["data"]["action"] == expected
    assert _decisions(conn)[0]["data"]["thesis_check"] == ("證偽" if expected == "SELL_ALL" else verdict if verdict != "證偽" else "疑似")
    assert _reg(conn)["status"] == "HOLDING"  # 持有狀態由帳本管理，不被論點檢查改寫
    assert _reg(conn)["data"]["cost"] == 100.0


def test_thesis_triggered_criteria_recorded(env):
    paths, conn = env
    ThesisHandler().post(_job("THESIS", _price_params()), thesis_result("證偽", hit=True), _ctx(paths, conn))
    assert _decisions(conn)[0]["data"]["failure_conditions_hit"] == ["毛利率連兩季下滑"]


def test_thesis_addendum_written_next_to_main_file(env):
    paths, conn = env
    paths.theses.mkdir(parents=True, exist_ok=True)
    main = paths.theses / "KO_2026-01-01.md"
    main.write_text("# 主論點\n", encoding="utf-8")
    ThesisHandler().post(_job("THESIS", _price_params()), thesis_result("完好"), _ctx(paths, conn))
    assert main.read_text(encoding="utf-8") == "# 主論點\n"  # 主檔凍結
    add = paths.theses / "KO_2026-01-01_addendum_1.md"
    assert add.is_file() and "判定：完好" in add.read_text(encoding="utf-8")


def test_thesis_suspect_creates_dossier_with_full_params(env):
    paths, conn = env
    job = _job("THESIS", _price_params(holding=True, created_ts=7.0))
    nxt = ThesisHandler().post(job, thesis_result("疑似"), _ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["DOSSIER"]
    assert nxt[0].params["price"] == PRICE_NOW and nxt[0].params["holding"] is True


def test_thesis_intact_creates_no_next_job(env):
    paths, conn = env
    assert ThesisHandler().post(_job("THESIS", _price_params()), thesis_result("完好"), _ctx(paths, conn)) == []


# ---------------------------------------------------------------- PRICE 覆核與動作對應

def test_price_overwrites_model_numbers_and_recomputes_gate2(env):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "WATCH", {"auto_buy_cap": 1.0})
    _gate3_row(conn)
    result = _price_result(proposed_action="BUY_CANDIDATE")
    # 現價高於自動買進上限 → 閘門 2 不過 → WATCH
    cap = _auto_cap()
    job, nxt = _run_price(paths, conn, _price_params(price=cap * 1.5), result)
    assert nxt == []
    assert result["auto_buy_cap"] == pytest.approx(cap) and result["auto_buy_cap"] != 999.0
    assert result["sell_review_above"] != 1.0 and result["absurd_price_line"] != 1.0
    assert result["framework_gates"]["gate2"] == "不通過"
    assert _decisions(conn)[-1]["data"]["action"] == "WATCH"
    assert _reg(conn)["data"]["auto_buy_cap"] == pytest.approx(cap)  # 登錄表合併更新，既有鍵保留


def test_price_buy_when_all_gates_pass_for_watch(env):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "WATCH", {"cost": None, "grade": "B"})
    _gate3_row(conn)
    cap = _auto_cap()
    before = len(_decisions(conn))
    result = _price_result(proposed_action="BUY_CANDIDATE")
    job, nxt = _run_price(paths, conn, _price_params(price=cap * 0.8), result)
    # 新部位不在 PRICE 直接買：交給完整分析（G8–G11、RED_TEAM），此處不寫決策
    assert [j.job_type for j in nxt] == ["DOSSIER"]
    assert len(_decisions(conn)) == before
    assert result["framework_action"] == "BUY_CANDIDATE → 完整分析"


def _full_decision(conn, decision_date: str, price: float, cap: float, signal: str = "WATCH") -> None:
    did = f"KO:{decision_date}:full"
    db.insert(conn, "decisions", {"decision_id": did, "ticker": "KO", "decision_date": decision_date, "run_id": "r",
                                  "job_id": None, "signal": signal, "reason_code": "X",
                                  "data": {"action": signal, "price": price, "auto_buy_cap": cap},
                                  "created_at": decision_date + "T00:00:00Z"})
    db.insert(conn, "signals", {"decision_id": did, "ticker": "KO", "signal": signal,
                                "data": {"signal": signal, "price": price, "auto_buy_cap": cap},
                                "created_at": decision_date + "T00:00:00Z"})


@pytest.mark.parametrize("days,upgrade", [(30, False), (120, True)])
def test_price_buy_skips_rerun_when_recent_full_already_saw_buyable_price(env, days, upgrade):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "WATCH", {"grade": "B"})
    _gate3_row(conn)
    cap = _auto_cap()
    _full_decision(conn, (date.fromisoformat(ASOF) - timedelta(days=days)).isoformat(), price=cap * 0.9, cap=cap)
    _, nxt = _run_price(paths, conn, _price_params(price=cap * 0.8), _price_result(proposed_action="BUY_CANDIDATE"))
    if upgrade:
        assert [j.job_type for j in nxt] == ["DOSSIER"]
    else:
        assert nxt == []
        assert _decisions(conn)[-1]["data"]["action"] == "WATCH"
        assert any("不重跑" in n for n in _decisions(conn)[-1]["data"]["framework_notes"])


def test_price_expired_gate3_forces_full_and_writes_nothing(env):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "WATCH", {})
    _gate3_row(conn, decision_date=(date.fromisoformat(ASOF) - timedelta(days=400)).isoformat())
    before = len(_decisions(conn))
    _, nxt = _run_price(paths, conn, _price_params(price=_auto_cap()), _price_result(proposed_action="NO_CHANGE"))
    assert [j.job_type for j in nxt] == ["DOSSIER"]
    assert len(_decisions(conn)) == before


def test_price_needs_full_from_model_goes_to_dossier(env):
    paths, conn = env
    _write_cap_input(paths)
    _, nxt = _run_price(paths, conn, _price_params(), _price_result(needs_full=True))
    assert [j.job_type for j in nxt] == ["DOSSIER"]


@pytest.mark.parametrize("days", [100, 400])
def test_price_sell_half_respects_one_year_rule(env, days):
    # 設計決策 D03／D28：PRICE 不寫 TRIM；SELL_HALF 只記日誌（現價 60.5 遠低於荒謬級線與 IV_base → HOLD、不交 HOLD_REVIEW）
    paths, conn = env
    _write_cap_input(paths)
    first = (date.fromisoformat(ASOF) - timedelta(days=days)).isoformat()
    _seed_registry(conn, "KO", "HOLDING", {"first_buy_date": first, "weight": 0.05, "target_weight": 0.05})
    _, nxt = _run_price(paths, conn, _price_params(), _price_result(proposed_action="SELL_HALF"))
    assert _decisions(conn)[-1]["data"]["action"] == "HOLD" and nxt == []
    assert all(d["data"]["action"] != "TRIM" for d in _decisions(conn))


def test_price_sell_review_is_hold_with_note(env):
    # 設計決策 D28：模型的 SELL_REVIEW 不採信；由框架依現價 ≥ IV_base 判定是否交 HOLD_REVIEW
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "HOLDING", {"first_buy_date": "2020-01-01"})
    with __import__("tempfile").TemporaryDirectory() as t:
        f = Path(t) / "c.json"
        f.write_text(json.dumps(CAP_INPUT), encoding="utf-8")
        iv = run_cap_calc(f)["IV_base"]
    _, nxt = _run_price(paths, conn, _price_params(), _price_result(proposed_action="SELL_REVIEW"))
    assert PRICE_NOW < iv and nxt == [] and _decisions(conn)[-1]["data"]["action"] == "HOLD"   # 現價 < IV_base：不送
    px = iv * 1.1
    _, nxt = _run_price(paths, conn, _price_params(price=px), _price_result(proposed_action="SELL_REVIEW"), n=2)
    dec = _decisions(conn)[-1]["data"]
    assert dec["action"] == "HOLD"
    assert any("交 HOLD_REVIEW" in n for n in dec["framework_notes"])
    # SKILL §6 第 2 條：交賣出決策樹（HOLD_REVIEW），帶入現價
    assert [j.job_type for j in nxt] == ["HOLD_REVIEW"] and nxt[0].params["full_stage"] == "PRICE"
    assert nxt[0].params["price"] == pytest.approx(px)


def test_price_holding_above_iv_base_goes_to_hold_review_even_without_proposal(env):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "HOLDING", {"first_buy_date": "2020-01-01"})
    _gate3_row(conn)
    with __import__("tempfile").TemporaryDirectory() as t:
        f = Path(t) / "c.json"
        f.write_text(json.dumps(CAP_INPUT), encoding="utf-8")
        iv = run_cap_calc(f)["IV_base"]
    _, nxt = _run_price(paths, conn, _price_params(price=iv * 1.1), _price_result(proposed_action="HOLD"))
    assert [j.job_type for j in nxt] == ["HOLD_REVIEW"]


def test_hold_review_takes_price_from_params_when_no_price_file(tmp_path):
    from trader.jobs.hold_jobs import valuation_numbers
    (tmp_path / "cap_result.json").write_text(json.dumps({"IV_base": 90.0}), encoding="utf-8")
    assert valuation_numbers(tmp_path, {"price": 95.0}) == (90.0, 95.0)


def test_price_holding_below_target_weight_can_add(env):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "HOLDING", {"weight": 0.02, "target_weight": 0.05, "first_buy_date": "2020-01-01"})
    _gate3_row(conn)
    cap = _auto_cap()
    _run_price(paths, conn, _price_params(price=cap * 0.8), _price_result(proposed_action="BUY_CANDIDATE"))
    assert _decisions(conn)[-1]["data"]["action"] == "ADD"


def _write_g8_view(paths, treasury_adj: float) -> None:
    from trader.portfolio.holdings_view import view_path
    v = view_path(paths, "r1")
    v.parent.mkdir(parents=True, exist_ok=True)
    v.write_text(json.dumps({"n_holdings": 1, "holdings": [], "watchlist": [],
                             "benchmarks": {"treasury": {"adjusted": treasury_adj}, "market": {"error": "測試缺"}}}),
                 encoding="utf-8")


@pytest.mark.parametrize("treasury_adj, expected", [(0.9, "HOLD"), (0.0, "ADD")])
def test_price_add_checks_g8_against_view(env, treasury_adj, expected):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "HOLDING", {"weight": 0.02, "target_weight": 0.05, "first_buy_date": "2020-01-01"})
    _gate3_row(conn)
    _write_g8_view(paths, treasury_adj)
    result = _price_result(proposed_action="BUY_CANDIDATE")
    _run_price(paths, conn, _price_params(price=_auto_cap() * 0.8), result)
    d = _decisions(conn)[-1]["data"]
    assert d["action"] == expected and result["framework_action"] == expected
    notes = " ".join(d.get("framework_notes") or [])
    assert "大盤一列缺" in notes
    assert ("G8 機會成本未勝出" in notes) == (expected == "HOLD")


def test_price_mechanical_no_change_stays_watch(env):
    paths, conn = env
    _write_cap_input(paths)
    _seed_registry(conn, "KO", "WATCH", {})
    _run_price(paths, conn, _price_params(), _price_result(proposed_action="NO_CHANGE"))
    assert _decisions(conn)[-1]["data"]["action"] == "WATCH"


def latest_signal(conn) -> dict:
    return writeback.latest_signal(conn, "KO")


# ---------------------------------------------------------------- writeback 新函式

def test_touch_review_merges_and_noop_without_row(env):
    paths, conn = env
    writeback.touch_review(conn, "KO", ASOF)  # 無列：什麼都不做
    assert _reg_or_none(conn) is None
    _seed_registry(conn, "KO", "HOLDING", {"cost": 90.0, "last_review_date": "2026-01-01"})
    writeback.touch_review(conn, "KO", ASOF)
    assert _reg(conn) == {"status": "HOLDING", "data": {"cost": 90.0, "last_review_date": ASOF}}


def _reg_or_none(conn):
    row = conn.execute("SELECT 1 FROM registry WHERE ticker='KO'").fetchone()
    return row


def test_full_result_saves_cap_input(env, tmp_path):
    paths, conn = env
    td = tmp_path / "tdir"
    td.mkdir()
    (td / "signal_card.json").write_text(json.dumps({"signal": "WATCH", "reason_code": "TOO_HARD"}), encoding="utf-8")
    (td / "registry_entry.json").write_text(json.dumps({"ticker": "KO", "status": "WATCH"}), encoding="utf-8")
    (td / "decision_log.json").write_text(json.dumps({"action": "WATCH"}), encoding="utf-8")
    (td / "thesis.md").write_text("論點", encoding="utf-8")
    (td / "cap_input.json").write_text(json.dumps(CAP_INPUT, ensure_ascii=False), encoding="utf-8")

    writeback.write_full_result(conn, paths, "KO", ASOF, "r1", td)

    saved = paths.state / "cap_inputs" / "KO.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == CAP_INPUT
    assert _reg(conn)["data"]["cap_input_file"] == "cap_inputs/KO.json"   # 相對 state（M8-R4-01）
    assert saved.is_file()


def test_full_result_without_cap_input_keeps_existing_state(env, tmp_path):
    paths, conn = env
    existing = _write_cap_input(paths, extra={"price": 1.0})
    td = tmp_path / "tdir2"
    td.mkdir()
    (td / "signal_card.json").write_text(json.dumps({"signal": "WATCH", "reason_code": "TOO_HARD"}), encoding="utf-8")
    (td / "registry_entry.json").write_text(json.dumps({"ticker": "KO", "status": "WATCH"}), encoding="utf-8")
    (td / "decision_log.json").write_text(json.dumps({"action": "WATCH"}), encoding="utf-8")
    (td / "thesis.md").write_text("論點", encoding="utf-8")
    writeback.write_full_result(conn, paths, "KO", ASOF, "r2", td)
    assert json.loads(existing.read_text(encoding="utf-8"))["price"] == 1.0
    assert "cap_input_file" not in _reg(conn)["data"]


def test_full_result_registry_lines_follow_cap_calc(env, tmp_path):
    """登錄表的三條線與等級以 cap_result／cap_input 為準，不照抄模型的 registry_entry。"""
    paths, conn = env
    td = tmp_path / "tdir3"
    td.mkdir()
    (td / "signal_card.json").write_text(json.dumps({"signal": "WATCH", "reason_code": "TOO_HARD",
                                                     "certainty_grade": "B"}), encoding="utf-8")
    (td / "registry_entry.json").write_text(json.dumps({"ticker": "KO", "status": "WATCH", "grade": "A",
                                                        "auto_buy_cap": 999.0, "sell_review_above": 999.0,
                                                        "absurd_price_line": 999.0}), encoding="utf-8")
    (td / "decision_log.json").write_text(json.dumps({"action": "WATCH"}), encoding="utf-8")
    (td / "thesis.md").write_text("論點", encoding="utf-8")
    (td / "cap_input.json").write_text(json.dumps({**CAP_INPUT, "norm_eps": 6.0}), encoding="utf-8")
    (td / "cap_result.json").write_text(json.dumps({"auto_buy_cap": 70.0, "IV_base": 90.0, "grade_used": "C"}),
                                        encoding="utf-8")
    writeback.write_full_result(conn, paths, "KO", ASOF, "r4", td)
    d = _reg(conn)["data"]
    assert (d["auto_buy_cap"], d["sell_review_above"], d["absurd_price_line"], d["grade"]) == (70.0, 90.0, 300.0, "C")
    card = latest_signal(conn)["data"]
    assert card["auto_buy_cap"] == 70.0 and card["sell_review_above"] == 90.0
    assert any("以 cap_calc 為準" in n for n in _decisions(conn)[-1]["data"]["framework_notes"])


def test_price_result_null_lines_keep_existing_sell_lines(env):
    """cap_calc 給不出 IV_base／荒謬級線時，不把登錄表原有的賣出檢視線蓋成 null；買進上限可為 null（不買）。"""
    paths, conn = env
    _seed_registry(conn, "KO", "WATCH", {"auto_buy_cap": 70.0, "sell_review_above": 90.0, "absurd_price_line": 260.0})
    cap = {"auto_buy_cap": None, "cap": None, "IV_base": None, "grade_used": "B"}
    writeback.write_price_result(conn, paths, "KO", ASOF, "r5", action="WATCH", gate1=True, gate2="無上限",
                                 gate3="通過", cap=cap, price=80.0, price_date="2026-10-15", t30=0.047,
                                 reason="測試", notes=[])
    d = _reg(conn)["data"]
    assert d["auto_buy_cap"] is None and d["sell_review_above"] == 90.0 and d["absurd_price_line"] == 260.0


def test_price_result_keeps_registry_fields_and_appends_notes(env):
    paths, conn = env
    _seed_registry(conn, "KO", "WATCH", {"cik": "0000000001", "grade": "B"})
    cap = {"auto_buy_cap": 70.0, "cap": 75.0, "IV_base": 90.0, "IV_cons": 60.0, "IV_opt": 120.0, "IV0": 40.0,
           "r_d": 0.077, "absurd_price_line": 260.0, "grade_used": "B", "price_to_cap": 0.8}
    prior = {"signal": "WATCH", "reason_code": "TOO_HARD", "framework_notes": ["舊註記"]}
    db.insert(conn, "signals", {"decision_id": "KO-20260101-1", "ticker": "KO", "signal": "WATCH",
                                "data": prior, "created_at": "2026-01-01T00:00:00Z"})
    did = writeback.write_price_result(conn, paths, "KO", ASOF, "r3", action="WATCH", gate1=True,
                                       gate2="不通過", gate3="通過", cap=cap, price=80.0, price_date="2026-10-15",
                                       t30=0.047, reason="測試", notes=["測試註記"])
    assert did == "KO-20261016-1"
    card = latest_signal(conn)["data"]
    assert card["framework_notes"][0] == "舊註記" and "測試註記" in card["framework_notes"][-1]
    assert card["sell_review_above"] == 90.0 and card["value_per_share"]["base"] == 90.0
    reg = _reg(conn)["data"]
    assert reg["cik"] == "0000000001" and reg["grade"] == "B"
    assert reg["review_price"] == 80.0 and reg["t30_at_valuation"] == 0.047 and reg["last_review_date"] == ASOF
    assert reg["absurd_price_line"] == 260.0


def test_price_result_rejects_bad_action(env):
    paths, conn = env
    with pytest.raises(writeback.WritebackError):
        writeback.write_price_result(conn, paths, "KO", ASOF, "r4", action="SELL_ALL", gate1=True, gate2="通過",
                                     gate3="通過", cap={}, price=1.0, price_date=None, t30=None, reason="",
                                     notes=[])


def test_thesis_result_rejects_bad_verdict(env):
    paths, conn = env
    with pytest.raises(writeback.WritebackError):
        writeback.write_thesis_result(conn, paths, "KO", ASOF, "r5", {"verdict": "不明"}, {})


def test_r_list_text_extracts_sell_011(tmp_path):
    from trader.jobs.review_jobs import r_list_text
    from trader.paths import get_paths
    txt = r_list_text(get_paths(tmp_path).skill_deep)
    assert txt.startswith("**SELL-011") and "| R1 |" in txt and "| R10 |" in txt and "SELL-012" not in txt
    assert r_list_text(tmp_path / "nope").startswith("（缺")


def test_thesis_addendum_lists_r_items():
    from trader.analysis.writeback import _thesis_addendum_text
    md = _thesis_addendum_text("KO", "2026-10-16", "疑似", "HOLD", {
        "kill_criteria_status": [], "new_facts": [],
        "r_list_status": [{"item": "R4", "status": "惡化", "evidence": "ROIC 連三年下滑"},
                          {"item": "R7", "status": "不在本工作範圍", "evidence": "需價格"}]})
    assert "- R4：惡化（ROIC 連三年下滑）" in md and "R7：不在本工作範圍" in md


# ---------------------------------------------------------------- B1 C1 等級上限

def test_c1_caps_grade_in_cap_input_and_card(tmp_path):
    from trader.jobs.deep import _cap_grade_by_c1
    (tmp_path / "cap_input.json").write_text(json.dumps({**CAP_INPUT, "grade": "A"}), encoding="utf-8")
    card = {"certainty_grade": "A"}
    notes = []
    assert _cap_grade_by_c1(tmp_path, {"max_grade_from_C1": "B"}, card, notes) is True
    assert json.loads((tmp_path / "cap_input.json").read_text())["grade"] == "B" and card["certainty_grade"] == "B"
    assert notes and "C1" in notes[0]
    assert _cap_grade_by_c1(tmp_path, {"max_grade_from_C1": "A"}, card, []) is False  # 已不高於上限


# ---------------------------------------------------------------- B4 不合格輸出不可讓舊結論繼續允許加碼

def test_thesis_invalid_counts_as_suspect_and_blocks_adding(env):
    from trader.portfolio.sizing import add_blocked
    paths, conn = env
    _seed_registry(conn, "KO", "HOLDING", {})
    job = _job("THESIS")
    nxt = ThesisHandler().on_invalid(job, ["缺 verdict"], _ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["DOSSIER"]
    assert _decisions(conn)[-1]["data"]["thesis_check"] == "疑似"
    assert add_blocked(conn, "KO", ASOF) == "論點疑似或證偽"


def test_price_invalid_leaves_failed_gate_record(env):
    from trader.portfolio.sizing import _latest_gate_decision, gate_failures
    paths, conn = env
    PriceHandler().on_invalid(_job("PRICE"), ["格式錯"], _ctx(paths, conn))
    g = _latest_gate_decision(conn, "KO", ASOF)
    assert g is not None and gate_failures(g["log"], None)
