"""P53（M2-B2 框架端＋M3-G＋D27）：also_price 續接 PRICE、THESIS 含當日申報與新聞標題、失效條件 next_due、
P-50 觀察期、紅燈延後、價格過期／申報抓取失敗標示、D27 缺資料與淘汰類範圍。全部離線。"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import test_review_jobs as TJ
import test_review_pipeline as TRP
from test_review_pipeline import env as pipe_env  # noqa: F401 - pytest 夾具
from test_review_pipeline import no_engine  # noqa: F401 - pytest 夾具
from trader import db
from trader.analysis import writeback
from trader.jobs.context import build_prompt
from trader.jobs.review_jobs import ThesisHandler, TriageHandler
from trader.pipelines import review as RP
from trader.pipelines.review import run_review

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("review_due_p53", ROOT / "berkshire-deep-merged" / "scripts" / "review_due.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

ASOF = TJ.ASOF   # 2026-10-16
env = TJ.env     # review_jobs 夾具（paths, conn）


def series(vals, end=ASOF):
    d0 = date.fromisoformat(end)
    return pd.DataFrame({"date": [d0 - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))], "close": vals})


HOLD = {"ticker": "KO", "status": "HOLDING", "last_review_date": "2026-10-01", "last_full_review_date": "2026-06-01",
        "t30_at_valuation": 0.047, "review_price": 100, "cost": 100}
Q = [{"form": "10-Q", "filingDate": "2026-10-05", "items": ""}]


def _check(e, filings, px, t30=0.047, news=None):
    d = {}
    act, why, tri = R.check(e, filings, px, ASOF, t30, news, detail=d)
    return act, why, tri, d


# ---------------------------------------------------------------- M2_D7_01 also_price

def test_review_due_also_price_when_thesis_masks_price():
    act, why, _, d = _check(dict(HOLD, absurd_price_line=120), Q, series([119] * 119 + [121]))
    assert act == "THESIS" and d["also_price"] is True
    assert any(w.startswith("[PRICE]") for w in why)
    # 沒有 PRICE 型觸發 → False；只有 PRICE → action PRICE、also_price False
    assert _check(HOLD, Q, series([100] * 120))[3]["also_price"] is False
    act, _, _, d = _check(dict(HOLD, absurd_price_line=120), [], series([119] * 119 + [121]))
    assert act == "PRICE" and d["also_price"] is False


def _thesis_job(n: int = 1, **kw):
    return TJ._job("THESIS", TJ._price_params(last_review_date="2026-10-01", **kw), n=n)


def test_thesis_intact_with_also_price_continues_price(env):
    paths, conn = env
    TJ._write_cap_input(paths)
    nxt = ThesisHandler().post(_thesis_job(also_price=True, base_action="THESIS"), TJ.thesis_result("完好"),
                               TJ._ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["PRICE"]
    assert nxt[0].params["price"] == TJ.PRICE_NOW   # PRICE 需要的現價、利率沿用原 params


def test_thesis_intact_also_price_without_cap_inputs_goes_dossier(env):
    paths, conn = env
    nxt = ThesisHandler().post(_thesis_job(also_price=True), TJ.thesis_result("完好"), TJ._ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["DOSSIER"]


def test_triage_escalated_thesis_keeps_base_price(env):
    """TRIAGE（base_action=PRICE）升級成 THESIS、判完好 → 原本的 PRICE 不可丟。"""
    paths, conn = env
    TJ._write_cap_input(paths)
    ctx = TJ._ctx(paths, conn)
    tri = TJ._job("TRIAGE", TJ._price_params(base_action="PRICE", last_review_date="2026-10-01"))
    th = TriageHandler().next_jobs(tri, {"escalate": True}, ctx)[0]
    assert th.job_type == "THESIS"
    nxt = ThesisHandler().post(th, TJ.thesis_result("完好"), ctx)
    assert [j.job_type for j in nxt] == ["PRICE"]


def test_thesis_intact_without_flag_no_next_job(env):
    paths, conn = env
    TJ._write_cap_input(paths)
    assert ThesisHandler().post(_thesis_job(base_action="THESIS"), TJ.thesis_result("完好"), TJ._ctx(paths, conn)) == []


# ---------------------------------------------------------------- M2_D6_04（框架端）＋ M3-D10-03 新聞

def test_thesis_includes_same_day_filing_and_news_titles(env):
    paths, conn = env
    q_url = "https://www.sec.gov/Archives/edgar/data/1/q/q.htm"
    sec = TJ.FakeSec(rows=[{"form": "10-Q", "filingDate": "2026-10-01", "url": q_url,
                            "accessionNumber": "0000000001-26-000001"}],
                     texts={q_url: "Item 2. Management's Discussion\nSAMEDAY_MARKER\n"})
    news = [{"type": "news", "date": "2026-10-03", "headline": "KO signs bottling deal", "source": "Wire", "url": "u"},
            {"type": "news", "date": "2026-10-04", "headline": "KO price target raised to $70", "source": "X", "url": "u"}]
    job = _thesis_job(kill_criteria=["毛利率連兩季下滑"], news=news)
    ctx = TJ._ctx(paths, conn, sec)
    h = ThesisHandler()
    h.pre_script(job, ctx)
    sections = h.prepare(job, ctx)
    texts = TJ._sandbox_texts(job.sandbox)
    assert any("SAMEDAY_MARKER" in t for n, t in texts.items() if n.startswith("thesis_sec/")), list(texts)
    TJ._assert_price_free(job.sandbox)
    prompt = build_prompt(job, "openai_compat", 10**9, paths.root, sections,
                          values=h.prompt_values(job, ctx, "openai_compat"))
    assert "KO signs bottling deal" in prompt and "{news}" not in prompt
    assert "price target" not in prompt.lower()          # 價格標題不進價格盲提示
    assert str(TJ.PRICE_NOW) not in prompt                # 觸發理由（含價格）不進提示
    assert "[PRICE]" not in prompt


def test_thesis_news_falls_back_to_triage_items(env):
    paths, conn = env
    job = _thesis_job(triage=[{"type": "news", "date": "2026-10-03", "headline": "H-TRIAGE", "source": "S"},
                              {"type": "8-K", "date": "2026-10-03", "items": "7.01", "url": "u"}])
    v = ThesisHandler().prompt_values(job, TJ._ctx(paths, conn), "openai_compat")
    assert "H-TRIAGE" in v["news"]
    assert ThesisHandler().prompt_values(_thesis_job(), TJ._ctx(paths, conn), "openai_compat")["news"] == "（無）"


# ---------------------------------------------------------------- M2-D5-12 失效條件 next_due

def _kc_result(next_due, status="未觸發", verdict="完好"):
    r = TJ.thesis_result(verdict)
    r["kill_criteria_status"][0].update({"status": status, "next_due": next_due})
    return r


def test_thesis_next_due_moves_registry_due_and_stops_refire(env):
    paths, conn = env
    data = {"cik": "1", "last_review_date": "2026-09-01", "last_full_review_date": "2026-06-01",
            "kill_criteria_due": [{"text": "毛利率連兩季下滑", "due": "2026-10-10"}, {"text": "其他", "due": None}]}
    TJ._seed_registry(conn, "KO", "WATCH", data)
    ThesisHandler().post(_thesis_job(), _kc_result("2027-02-15"), TJ._ctx(paths, conn))
    kc = TJ._reg(conn)["data"]["kill_criteria_due"]
    assert kc[0] == {"text": "毛利率連兩季下滑", "due": "2027-02-15", "checked_on": ASOF, "status": "未觸發"}
    assert kc[1] == {"text": "其他", "due": None}          # 沒檢查到的條目不動
    e = {**TJ._reg(conn)["data"], "ticker": "KO", "status": "WATCH"}
    nxt_day = (date.fromisoformat(ASOF) + timedelta(days=1)).isoformat()
    act, why, _ = R.check(e, [], series([100] * 120, end=nxt_day), nxt_day, 0.047)
    assert act == "NONE", why
    act, why, _ = R.check(e, [], series([100] * 120, end="2027-02-16"), "2027-02-16", 0.047)
    assert any("2027-02-15" in w for w in why)            # 新 due 到期再檢查


def test_thesis_next_due_null_or_past_keeps_due_and_older_asof_ignored(env):
    paths, conn = env
    TJ._seed_registry(conn, "KO", "WATCH", {"kill_criteria_due": [
        {"text": "毛利率連兩季下滑", "due": "2026-10-10", "checked_on": "2026-12-01", "status": "未觸發"}]})
    ThesisHandler().post(_thesis_job(), _kc_result("2027-01-01"), TJ._ctx(paths, conn))
    assert TJ._reg(conn)["data"]["kill_criteria_due"][0]["checked_on"] == "2026-12-01"   # 舊資料日不覆寫
    TJ._seed_registry(conn, "KO", "WATCH", {"kill_criteria_due": [{"text": "毛利率連兩季下滑", "due": "2026-10-10"}]})
    ThesisHandler().post(_thesis_job(n=2), _kc_result(None), TJ._ctx(paths, conn))   # 另一個工作（同 job_id 只回寫一次，M6-D9-03）
    kc = TJ._reg(conn)["data"]["kill_criteria_due"][0]
    assert kc["due"] == "2026-10-10" and kc["checked_on"] == ASOF


# ---------------------------------------------------------------- M2_D4_04 P-50 觀察期

def test_thesis_suspect_sets_observe_until_for_holding_and_review_due_fires(env):
    paths, conn = env
    TJ._seed_registry(conn, "KO", "HOLDING", {"cost": 100.0, "last_full_review_date": "2026-06-01"})
    ThesisHandler().post(_thesis_job(holding=True), TJ.thesis_result("疑似"), TJ._ctx(paths, conn))
    ou = TJ._reg(conn)["data"]["observe_until"]
    assert ou == (date.fromisoformat(ASOF) + timedelta(days=183)).isoformat()
    e = dict(HOLD, observe_until=ou)
    assert R.check(e, [], series([100] * 120, end=ou), ou, 0.047)[0] == "FULL"
    assert R.check(dict(e, last_full_review_date=ou), [], series([100] * 120, end=ou), ou, 0.047)[0] == "NONE"
    # 觀察名單不設；既有未到期的期限不延長
    TJ._seed_registry(conn, "PEP", "WATCH", {})
    ThesisHandler().post(TJ._job("THESIS", TJ._price_params(), ticker="PEP"), TJ.thesis_result("疑似"),
                         TJ._ctx(paths, conn))
    assert "observe_until" not in TJ._reg(conn, "PEP")["data"]
    TJ._seed_registry(conn, "KO", "HOLDING", {"observe_until": "2026-12-31"})
    ThesisHandler().post(_thesis_job(holding=True), TJ.thesis_result("疑似"), TJ._ctx(paths, conn))
    assert TJ._reg(conn)["data"]["observe_until"] == "2026-12-31"


# ---------------------------------------------------------------- M2_D7_08 紅燈延後

def test_red_light_stop_is_watch_with_rerun_due(env):
    paths, conn = env
    writeback.write_stop_result(conn, paths, "KO", ASOF, "r1", {"reason_code": "RED_LIGHT", "stopped_at_gate": "G11"})
    reg = TJ._reg(conn)
    due = (date.fromisoformat(ASOF) + timedelta(days=14)).isoformat()
    assert reg["status"] == "WATCH" and reg["data"]["next_full_review_due"] == due
    card = conn.execute("SELECT data FROM signals WHERE ticker='KO'").fetchone()
    assert json.loads(card["data"])["reason_code"] == "RED_LIGHT"   # 保留紅燈理由
    e = {**{k: v for k, v in reg["data"].items() if v is not None}, "ticker": "KO", "status": "WATCH"}
    assert R.check(e, [], series([100] * 120, end=due), due, 0.047)[0] == "FULL"


# ---------------------------------------------------------------- M2_D9_05、M2_D9_03

def test_stale_price_marked_and_price_triggers_skipped():
    act, why, _, d = _check(dict(HOLD, absurd_price_line=50), [], series([100] * 90, end="2026-09-15"))
    assert d["price_status"] == "stale" and act == "NONE"
    assert why[0].startswith("價格缺／過期") and not any("沒有變化" in w for w in why)
    assert _check(HOLD, [], None)[3]["price_status"] == "missing"


def test_filings_fetch_failure_not_upgraded():
    act, why, _, d = _check(HOLD, None, series([100] * 120))
    assert act == "NONE" and d["filings_ok"] is False
    assert any("重大申報未檢查" in w for w in why) and not any("沒有變化" in w for w in why)


# ---------------------------------------------------------------- D27／M2-D10-03

def test_status_scopes_d27():
    md = dict(HOLD, status="MISSING_DATA")
    assert _check(md, Q, series([100] * 120))[0] == "FULL"
    assert _check(md, [], series([100] * 119 + [60]), t30=0.06)[0] == "NONE"
    na = dict(HOLD, status="NO_ACTION")
    assert _check(na, Q, series([100] * 120))[0] == "NONE"
    rj = dict(HOLD, status="REJECT", last_full_review_date="2026-06-01")
    k402 = [{"form": "8-K", "filingDate": "2026-10-05", "items": "4.02"}]
    k502 = [{"form": "8-K", "filingDate": "2026-10-05", "items": "5.02"}]
    assert _check(rj, k402, series([100] * 120))[0] == "FULL"
    act, why, _, d = _check(dict(rj, integrity_reject=True), k402, series([100] * 120))
    assert act == "NONE" and d["suppressed"] and "誠信" in why[0]
    assert _check(dict(rj, integrity_reject=True), k502, series([100] * 120))[0] == "FULL"


P_SEED = {
    "WAT": ("WATCH", {"cik": "0000000011", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                       "auto_buy_cap": 80}),
    "MDX": ("MISSING_DATA", {"cik": "0000000012", "last_review_date": "2026-09-01",
                             "last_full_review_date": "2026-09-01"}),
    "RJX": ("REJECT", {"cik": "0000000013", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01"}),
    "RJI": ("REJECT", {"cik": "0000000014", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                       "integrity_reject": True}),
    "HLD": ("HOLDING", {"cik": "0000000015", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01"}),
}
P_FILINGS = {"WAT": [], "MDX": [{"form": "10-Q", "filingDate": "2026-10-02", "items": "", "url": ""}],
             "RJX": [{"form": "8-K", "filingDate": "2026-10-02", "items": "4.02", "url": ""}],
             "RJI": [{"form": "8-K", "filingDate": "2026-10-02", "items": "4.02", "url": ""}], "HLD": []}


def test_watch_run_brings_missing_data_and_rejected(pipe_env, no_engine):
    cfg, paths, conn, tmp = pipe_env
    TRP._seed(conn, P_SEED)
    clients = {"alpaca": TRP.FakeAlpaca({t: 105.0 for t in P_SEED}), "fred": TRP.FakeFred(), "sec": TRP.FakeSec({})}
    s = run_review(cfg, paths, conn, ASOF, scope="watch", clients=clients, offline_filings=TRP._offline(tmp, P_FILINGS))
    assert "error" not in s, s
    assert s["jobs"] == {"MDX": "DOSSIER", "RJX": "DOSSIER"}   # HLD 不在 watch；RJI 誠信淘汰不重評；WAT 無觸發
    due = json.loads((paths.work / s["run_id"] / "due.json").read_text(encoding="utf-8"))
    assert sorted(r["ticker"] for r in due["results"]) == ["MDX", "RJI", "RJX", "WAT"]
    assert [e["ticker"] for e in RP.export_registry(conn, "watch")] == ["WAT"]   # export_registry 的 watch 語意不變


def test_pipeline_carries_also_price_news_and_skips_failed_filings(pipe_env, no_engine, monkeypatch):
    """review.py：also_price／news 帶入 params；價格非 ok 記 summary。
    filings_ok=false：申報失敗只略過申報類觸發（review_due 已處理），其餘觸發照建工作並記 skipped（RM2-12／M2-RS8-03）；
    函式名中的 skips_failed_filings 沿用舊名，意涵即「只略過申報類」。"""
    cfg, paths, conn, tmp = pipe_env
    TRP._seed(conn, {"AAA": P_SEED["WAT"], "BBB": P_SEED["WAT"], "CCC": P_SEED["WAT"]})
    real_run = RP.subprocess.run

    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("--out") + 1])
        base = {"cik": None, "status": "WATCH", "reasons": [], "triage": [], "suppressed": [], "unchecked": [],
                "checked": {}, "thesis_file": None, "kill_criteria": [], "last_review_date": "2026-09-01"}
        rows = [dict(base, ticker="AAA", action="THESIS", price_status="ok", filings_ok=True, also_price=True,
                     news=[{"type": "news", "date": "2026-10-01", "headline": "H1", "source": "S", "url": ""}]),
                dict(base, ticker="BBB", action="PRICE", price_status="ok", filings_ok=False, also_price=False, news=[]),
                dict(base, ticker="CCC", action="NONE", price_status="stale", filings_ok=True, also_price=False, news=[])]
        out.write_text(json.dumps({"signal_version": RP.SIGNAL_VERSION, "asof": ASOF, "results": rows}), encoding="utf-8")

        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()

    monkeypatch.setattr(RP.subprocess, "run", fake_run)
    clients = {"alpaca": TRP.FakeAlpaca({t: 105.0 for t in ("AAA", "BBB", "CCC")}), "fred": TRP.FakeFred(),
               "sec": TRP.FakeSec({})}
    s = run_review(cfg, paths, conn, ASOF, scope="watch", clients=clients,
                   offline_filings=TRP._offline(tmp, {"AAA": [], "BBB": [], "CCC": []}))
    monkeypatch.setattr(RP.subprocess, "run", real_run)
    assert "error" not in s, s
    assert s["jobs"] == {"AAA": "THESIS", "BBB": "DOSSIER"}   # BBB 申報失敗仍照建其餘觸發的工作
    assert any(x["ticker"] == "BBB" and "申報" in x["reason"] for x in s["skipped"])
    assert s["price_status_not_ok"] == [{"ticker": "CCC", "status": "WATCH", "price_status": "stale"}]
    p = json.loads(TRP._jobs(conn, s["run_id"])[0]["params"])
    assert p["also_price"] is True and p["news"][0]["headline"] == "H1"


# ---------------------------------------------------------------- P19 交接

def test_price_pe_median_uses_frozen_ttm_and_null_when_missing(env):
    """D60：PRICE 重算與完整分析同口徑——大型股取 pe_ttm_median、金融取 fin_pe_norm_median 並記來源；凍結檔缺 → null。"""
    paths, conn = env
    from trader.jobs.review_jobs import PriceHandler
    TJ._write_cap_input(paths, extra={"largecap_pe_median": 99.0, "fin_pe_median": 88.0})
    ctx = TJ._ctx(paths, conn)
    job = TJ._job("PRICE", TJ._price_params(largecap_pe={"pe_norm_median": 26.9, "pe_ttm_median": 21.5,
                                                         "fin_pe_norm_median": 18.1}))
    PriceHandler().pre_script(job, ctx)
    used = json.loads((ctx.work_dir / job.run_id / "KO" / "cap_input.json").read_text(encoding="utf-8"))
    assert used["largecap_pe_median"] == 21.5 and used["fin_pe_median"] == 18.1
    assert used["pe_field"]["largecap_pe_median"] == "pe_ttm_median"
    assert used["pe_field"]["fin_pe_median"] == "fin_pe_norm_median"
    job2 = TJ._job("PRICE", TJ._price_params(largecap_pe=None), n=2)
    PriceHandler().pre_script(job2, ctx)
    used = json.loads((ctx.work_dir / job2.run_id / "KO" / "cap_input.json").read_text(encoding="utf-8"))
    assert used["largecap_pe_median"] is None and used["fin_pe_median"] is None   # 不沿用 cap_inputs 舊值 99／88


def test_price_card_price_to_cap_is_price_over_auto_buy_cap(env):
    paths, conn = env
    TJ._seed_registry(conn, "KO", "WATCH", {"auto_buy_cap": 80.0})
    cap = {"auto_buy_cap": 80.0, "cap": 100.0, "IV_base": 120.0, "price_to_cap": 0.6, "price_to_auto_buy_cap": 0.75,
           "grade_used": "B"}
    did = writeback.write_price_result(conn, paths, "KO", ASOF, "r1", action="WATCH", gate1=True, gate2="通過",
                                       gate3="通過", cap=cap, price=60.0, price_date="2026-10-15", t30=0.047,
                                       reason="測試", notes=[])
    card = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()["data"])
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])
    assert card["price_to_cap"] == 0.75 and log["price_to_cap"] == 0.75
