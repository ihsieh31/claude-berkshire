"""P43：review 取申報與日線邊界（M4-D10-02、M4-D10-03／M7-D1-05）、analyze 輸入（M7-D6-05、M7-D9-04）、D59 TRIAGE 日誌。"""
import json

import pandas as pd
import pytest

import test_engine as TE
import test_review_pipeline as RPT
from trader import db
from trader.config import load_config
from trader.jobs.engine import Engine
from trader.ledger import book, broker
from trader.llm.fake import FakeRunner
from trader.llm.router import Router
from trader.paths import get_paths
from trader.pipelines import analyze as AP
from trader.pipelines.review import run_review
from trader.report import validation as V

ASOF = TE.ASOF  # 2026-10-16
env = RPT.env


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """skill 子程序若嘗試連 SEC／Yahoo，經不存在的代理立即失敗（測試不得上網）。"""
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    for k in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- M7-D1-05／M4-D10-03：拆股調整日線

class SplitAlpaca(RPT.FakeAlpaca):
    """2:1 拆股（2026-10-05 生效）：raw 之前 100、之後 50；split 調整後全段 50。"""
    SPLIT = "2026-10-05"

    def bars(self, symbols, start, end, adjustment="raw"):
        self.bar_calls.append((list(symbols), start, end, adjustment))
        rows = []
        for t in symbols:
            for d in pd.bdate_range(end=end, periods=120):
                ds = d.strftime("%Y-%m-%d")
                px = 50.0 if (adjustment != "raw" or ds >= self.SPLIT) else 100.0
                rows.append({"ticker": t, "date": ds, "close": px})
        return pd.DataFrame(rows, columns=["ticker", "date", "close"])


SPL = {"SPL": ("HOLDING", {"cik": "0000000009", "last_review_date": "2026-10-01", "last_full_review_date": "2026-09-01",
                           "review_price": 50, "cost": 50, "auto_buy_cap": 40, "sell_review_above": 300,
                           "absurd_price_line": 500, "weight": 0.05, "target_weight": 0.05,
                           "first_buy_date": "2025-01-01", "t30_at_valuation": 0.047})}


def test_split_history_does_not_trigger_thesis(env):
    """登錄表已依拆股換算（review_price／cost 50）；日線若用 raw，100→50 會觸發 ±4% 與單季 −25% → THESIS。"""
    cfg, paths, conn, tmp = env
    RPT._seed(conn, SPL)
    alp = SplitAlpaca({})
    s = run_review(cfg, paths, conn, ASOF, scope="holdings",
                   clients={"alpaca": alp, "fred": RPT.FakeFred(), "sec": RPT.FakeSec({})},
                   offline_filings=RPT._offline(tmp, {"SPL": []}), due_only=True)
    assert "error" not in s, s
    assert alp.bar_calls[0][3] == "split"
    assert s["counts"] == {"NONE": 1}, s
    daily = pd.read_csv(paths.work / s["run_id"] / "daily.csv")
    assert set(daily["close"]) == {50.0} and daily["date"].max() <= ASOF


# ---------------------------------------------------------------- M4-D10-02：申報清單經資料層

class DataLayerSec(RPT.FakeSec):
    def __init__(self, ciks, filings, fail=()):
        super().__init__(ciks)
        self.data = filings
        self.fail = set(fail)
        self.calls = []

    def offline_filings(self, ciks, asof, max_age_hours=None):
        self.calls.append((dict(ciks), asof))
        self.max_ages = getattr(self, "max_ages", []) + [max_age_hours]
        out = {}
        for t in ciks:
            if t in self.fail:
                raise RuntimeError("HTTP 503")
            out[t] = self.data.get(t, [])
        return out


def test_filings_come_from_data_layer_and_failure_skips_ticker(env):
    cfg, paths, conn, tmp = env
    seed = {t: RPT.SEED[t] for t in ("FUL", "THS", "HLD")}
    RPT._seed(conn, seed)
    sec = DataLayerSec({}, RPT.FILINGS, fail={"THS"})
    s = run_review(cfg, paths, conn, ASOF, scope="all",
                   clients={"alpaca": RPT.FakeAlpaca({t: RPT.CLOSE for t in seed}), "fred": RPT.FakeFred(), "sec": sec},
                   due_only=True)
    assert "error" not in s, s
    out = paths.work / s["run_id"]
    assert json.loads((out / "filings.json").read_text(encoding="utf-8")) == {"FUL": RPT.FILINGS["FUL"], "HLD": [], "THS": None}
    assert {c[1] for c in sec.calls} == {ASOF}
    # THS 申報抓取失敗：以 null 交 review_due，只略過申報類觸發（不升 THESIS），記在 skipped（RM2-12）
    assert [x["ticker"] for x in s["skipped"]] == ["THS"] and "HTTP 503" in s["skipped"][0]["reason"]
    due = json.loads((out / "due.json").read_text(encoding="utf-8"))
    acts = {r["ticker"]: r["action"] for r in due["results"]}
    assert acts == {"FUL": "FULL", "HLD": "NONE", "THS": "NONE"}   # THS 其餘觸發照查：本例無 → NONE
    # 申報失敗只標在失敗檔（filings_ok=false＋提示），其他檔不受牽連（RM2-12）
    assert {r["ticker"] for r in due["results"] if any("申報抓取失敗" in w for w in r["reasons"])} == {"THS"}
    assert {r["ticker"] for r in due["results"] if r.get("filings_ok") is False} == {"THS"}


class StaleSec(DataLayerSec):
    """模擬 SecClient：SEC 失敗但有舊快取 → 不拋例外、回傳舊清單，只記在 stale（P43R）。"""
    def __init__(self, ciks, filings, stale=(), stale_pre=()):
        super().__init__(ciks, filings)
        self.stale_tickers = set(stale)
        self.stale_pre = set(stale_pre)  # 只有前身 CIK 用了舊快取的代號
        self.stale = ["company_tickers.json"]  # 先前其他呼叫已用過舊快取：不應牽連本次各檔

    def offline_filings(self, ciks, asof, max_age_hours=None):
        out = super().offline_filings(ciks, asof, max_age_hours)
        for t, cik in ciks.items():
            if t in self.stale_tickers:
                self.stale.append(f"sub_CIK{str(cik).zfill(10)}.json")
            if t in self.stale_pre:
                self.stale.append("sub_CIK0000099999.json")
        return out


def test_stale_cache_filings_skip_ticker(env):
    cfg, paths, conn, tmp = env
    seed = {t: RPT.SEED[t] for t in ("FUL", "HLD")}
    RPT._seed(conn, seed)
    # FUL 的舊快取缺最新 10-K（回傳空清單）；若照用，FUL 會以舊清單判定而非跳過
    sec = StaleSec({}, {"FUL": [], "HLD": []}, stale={"FUL"})
    s = run_review(cfg, paths, conn, ASOF, scope="all",
                   clients={"alpaca": RPT.FakeAlpaca({t: RPT.CLOSE for t in seed}), "fred": RPT.FakeFred(), "sec": sec},
                   due_only=True)
    assert "error" not in s, s
    out = paths.work / s["run_id"]
    assert [x["ticker"] for x in s["skipped"]] == ["FUL"] and "舊快取" in s["skipped"][0]["reason"]
    assert json.loads((out / "filings.json").read_text(encoding="utf-8")) == {"FUL": None, "HLD": []}
    due = json.loads((out / "due.json").read_text(encoding="utf-8"))
    assert {r["ticker"] for r in due["results"]} == {"FUL", "HLD"}   # 失敗檔仍進 review_due，只略過申報類（RM2-12）


def test_predecessor_stale_cache_does_not_skip_ticker(env):
    """X13：只有前身 CIK 用了舊快取（前身端點長期失敗）不應讓該檔被跳過；只計現行 CIK 的 sub 檔。"""
    cfg, paths, conn, tmp = env
    seed = {t: RPT.SEED[t] for t in ("FUL", "HLD")}
    RPT._seed(conn, seed)
    sec = StaleSec({}, {"FUL": RPT.FILINGS["FUL"], "HLD": []}, stale_pre={"FUL"})
    s = run_review(cfg, paths, conn, ASOF, scope="all",
                   clients={"alpaca": RPT.FakeAlpaca({t: RPT.CLOSE for t in seed}), "fred": RPT.FakeFred(), "sec": sec},
                   due_only=True)
    assert "error" not in s, s
    assert s["skipped"] == []
    due = json.loads((paths.work / s["run_id"] / "due.json").read_text(encoding="utf-8"))
    assert {r["ticker"]: r["action"] for r in due["results"]} == {"FUL": "FULL", "HLD": "NONE"}


def test_review_requests_forced_refetch(env):
    """X13：觸發檢查取申報清單要求當次重抓（max_age_hours=0），不吃 20 小時新鮮快取。"""
    cfg, paths, conn, tmp = env
    seed = {t: RPT.SEED[t] for t in ("FUL", "HLD")}
    RPT._seed(conn, seed)
    sec = DataLayerSec({}, RPT.FILINGS)
    s = run_review(cfg, paths, conn, ASOF, scope="all",
                   clients={"alpaca": RPT.FakeAlpaca({t: RPT.CLOSE for t in seed}), "fred": RPT.FakeFred(), "sec": sec},
                   due_only=True)
    assert "error" not in s, s
    assert sec.max_ages and set(sec.max_ages) == {0}


# ---- 真 SecClient：max_age_hours 覆寫只影響要求它的呼叫

def _sec_client(tmp_path, routes):
    import test_sec as TS
    from trader.data import sec as S
    return S.SecClient(TS.UA, tmp_path, http=TS.make_http(routes, [])), TS


def _subs(filing_date):
    return {"sic": "6331", "filings": {"recent": {
        "form": ["8-K"], "filingDate": [filing_date], "reportDate": [""], "items": ["1.01"],
        "accessionNumber": ["0001067983-26-000099"], "primaryDocument": ["e.htm"]}}}


def test_offline_filings_max_age_zero_refetches_fresh_cache(tmp_path):
    import os
    import time
    import test_sec as TS
    old, new = _subs("2026-09-01"), _subs("2026-10-15")
    path = tmp_path / f"sub_CIK{TS.CIK}.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    t = time.time() - 3600  # 1 小時前別的流程刷新過：預設 20 小時內算新鮮
    os.utime(path, (t, t))
    c, _ = _sec_client(tmp_path, {TS.SUBS_URL: new})
    # 其他呼叫端（預設）照舊用新鮮快取
    assert [f["filingDate"] for f in c.filings(TS.CIK, ASOF)] == ["2026-09-01"]
    assert [f["filingDate"] for f in c.offline_filings({"BRK.B": TS.CIK}, ASOF)["BRK.B"]] == ["2026-09-01"]
    # 強制重抓：拿到最新申報並寫回快取
    got = c.offline_filings({"BRK.B": TS.CIK}, ASOF, max_age_hours=0)
    assert [f["filingDate"] for f in got["BRK.B"]] == ["2026-10-15"] and c.stale == []
    assert json.loads(path.read_text(encoding="utf-8")) == new


def test_offline_filings_forced_refetch_failure_falls_back_to_cache(tmp_path):
    import httpx
    import test_sec as TS
    path = tmp_path / f"sub_CIK{TS.CIK}.json"
    path.write_text(json.dumps(_subs("2026-09-01")), encoding="utf-8")  # 剛寫入＝新鮮
    c, _ = _sec_client(tmp_path, {TS.SUBS_URL: httpx.Response(503)})
    got = c.offline_filings({"BRK.B": TS.CIK}, ASOF, max_age_hours=0)
    assert [f["filingDate"] for f in got["BRK.B"]] == ["2026-09-01"]
    assert c.stale == [f"sub_CIK{TS.CIK}.json"]  # review.py 據此跳過該檔（既有路徑）


def test_explicit_offline_filings_still_overrides(env):
    cfg, paths, conn, tmp = env
    RPT._seed(conn, {"FUL": RPT.SEED["FUL"]})
    sec = DataLayerSec({}, {})
    s = run_review(cfg, paths, conn, ASOF, clients={"alpaca": RPT.FakeAlpaca({"FUL": RPT.CLOSE}),
                                                    "fred": RPT.FakeFred(), "sec": sec},
                   offline_filings=RPT._offline(tmp), due_only=True)
    assert sec.calls == [] and s["counts"] == {"FULL": 1}


# ---------------------------------------------------------------- M7-D9-04：無持股時組合狀態讀帳本

def _cfg_conn(tmp_path):
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    return cfg, paths, conn


def test_portfolio_state_reads_ledger_without_positions(tmp_path):
    cfg, paths, conn = _cfg_conn(tmp_path)
    assert AP._portfolio_state(cfg, conn)["cash"] == cfg.account.initial_cash   # 帳本全空 → 初始資金
    book.ensure_initial_deposit(conn, 100000.0, "2026-10-01")
    book.adjust(conn, date="2026-10-10", cash_delta=-7000.0, note="清倉後的已實現虧損")
    broker.place_order(conn, "AAA", "BUY", 100, 50.0, "d1", "2026-10-15")
    ps = AP._portfolio_state(cfg, conn)
    assert (ps["cash"], ps["committed"], ps["holdings"], ps["n_holdings"]) == (93000.0, 5000.0, [], 0)


# ---------------------------------------------------------------- M7-D6-05：初選日與決策日

def _screen(paths, day, price, price_date):
    sd = paths.state / "screens" / day
    (sd / "inputs").mkdir(parents=True)
    (sd / "summary.json").write_text(json.dumps({"to_analyze": [{"ticker": "KO"}]}), encoding="utf-8")
    (sd / "handoff_noprice.json").write_text(json.dumps(
        {"core_shortlist": [{"ticker": "KO", "screen_decision": "PASS"}], "special_handoff": []}), encoding="utf-8")
    pd.DataFrame([{"ticker": "KO", "price": price, "price_date": price_date}]).to_csv(sd / "inputs" / "prices.csv",
                                                                                      index=False)
    (sd / "inputs" / "rates.json").write_text(json.dumps({"t30": 0.05, "t30_date": price_date,
                                                         "t30_20y_median": 0.034}), encoding="utf-8")


def test_screen_date_after_asof_refused(tmp_path):
    cfg, paths, conn = _cfg_conn(tmp_path)
    _screen(paths, "2026-10-20", 70.0, "2026-10-19")
    with pytest.raises(AP.AnalyzeError, match="晚於決策日"):
        AP.run_analyze(cfg, paths, conn, ASOF, screen_asof="2026-10-20", clients={"sec": object()})


def test_older_screen_prices_refetched_at_asof(tmp_path, monkeypatch):
    cfg, paths, conn = _cfg_conn(tmp_path)
    _screen(paths, "2026-10-01", 40.0, "2026-09-30")
    calls = []

    def fake_fetch(cfg, paths, conn, asof, outdir, tickers=None, clients=None, **kw):
        calls.append((asof, list(tickers)))
        outdir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"ticker": "KO", "price": 61.0, "price_date": "2026-10-16"}]).to_csv(outdir / "prices.csv",
                                                                                          index=False)
        (outdir / "rates.json").write_text(json.dumps({"t30": 0.048, "t30_date": "2026-10-15",
                                                      "t30_20y_median": 0.034}), encoding="utf-8")
        return {}

    monkeypatch.setattr(AP, "fetch_inputs", fake_fetch)
    monkeypatch.setattr(Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})
    out = AP.run_analyze(cfg, paths, conn, ASOF, screen_asof="2026-10-01", clients={"sec": object()})
    assert out["tickers"] == ["KO"]
    assert calls == [(ASOF, ["KO"])]
    p = json.loads(conn.execute("SELECT params FROM jobs WHERE job_type='DOSSIER'").fetchone()["params"])
    assert (p["price"], p["price_date"], p["rates"]["t30"]) == (61.0, "2026-10-16", 0.048)
    assert p["screen_context"]["screen_decision"] == "PASS"   # 交接內容（無價格）仍沿用初選


# ---------------------------------------------------------------- D59：TRIAGE 判否寫決策日誌

def _triage_no(env, status):
    cfg, paths, conn, tmp = env
    data = dict(RPT.SEED["TRI"][1], auto_buy_cap=80)   # 現價 105 > 上限 → 無價格觸發；只有 8-K 初判
    if status == "HOLDING":
        data.update(cost=105, review_price=105, sell_review_above=300, absurd_price_line=500,
                    first_buy_date="2025-01-01", weight=0.05, target_weight=0.05, t30_at_valuation=0.047)
    RPT._seed(conn, {"TRI": (status, data)})
    fake = FakeRunner(responses={"TRIAGE": {**TE.triage(False), "ticker": "TRI"}})   # 輸出 ticker 須與工作相符（M3_D1-10）
    router = Router(cfg, paths.routing_json, dry_run=True, fake=fake)
    s = run_review(cfg, paths, conn, ASOF, clients=RPT._clients(), router=router,
                   offline_filings=RPT._offline(tmp, {"TRI": RPT.FILINGS["TRI"]}))
    assert "error" not in s and s["jobs"] == {"TRI": "TRIAGE"}, s
    return s, cfg, paths, conn


@pytest.mark.parametrize("status,action", [("WATCH", "WATCH"), ("HOLDING", "HOLD")])
def test_triage_no_writes_decision_log(env, status, action):
    s, cfg, paths, conn = _triage_no(env, status)
    rows = conn.execute("SELECT decision_id, run_id, job_id, signal, data FROM decisions WHERE ticker='TRI'").fetchall()
    assert len(rows) == 1
    r = rows[0]
    log = json.loads(r["data"])
    assert (r["run_id"], r["job_id"], r["signal"]) == (s["run_id"], None, action)
    assert (log["action"], log["review_type"], log["decision_date"], log["thesis_check"]) == (action, "TRIAGE", ASOF, "不適用")
    assert log["review_trigger_hit"] and "不需深入" in log["reason_one_liner"]
    reg = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='TRI'").fetchone()["data"])
    assert reg["last_review_date"] == ASOF          # touch_review 照舊
    # 月報合規段：這次 (run, 代號) 有日誌，另計觸發初判筆數
    cm = V.compliance(conn, paths, ASOF, ASOF, V._logs(conn, ASOF, ASOF))
    assert cm["without_decision"] == [] and cm["n_triage"] == 1 and cm["n_full"] == 0
    assert "觸發初判判否 1" in V.render({"start": ASOF, "end": ASOF, "compliance": cm,
                                     "market": {"account": None, "spy": None, "excess_pp": None, "buys": []},
                                     "population": {"note": "x"}, "gates": V.gates([], {}),
                                     "honest": V.HONEST})


def test_triage_log_does_not_block_add_or_mask_gates(env):
    """TRIAGE 日誌不是 THESIS／HOLD_REVIEW：不擋加碼、不被當成閘門紀錄。"""
    s, cfg, paths, conn = _triage_no(env, "HOLDING")
    from trader.portfolio.sizing import _latest_gate_decision, add_blocked
    assert add_blocked(conn, "TRI", ASOF) is None
    assert _latest_gate_decision(conn, "TRI", ASOF) is None


def test_compliance_unfinished_jobs_not_counted_as_missing_log(tmp_path):
    cfg, paths, conn = _cfg_conn(tmp_path)
    for jid, t, jt, st in (("r1:AAA:DOSSIER:1", "AAA", "DOSSIER", "done"),
                           ("r1:AAA:FULL_A:1", "AAA", "FULL_A", "awaiting_approval"),
                           ("r1:BBB:TRIAGE:1", "BBB", "TRIAGE", "done")):
        db.insert(conn, "jobs", {"job_id": jid, "run_id": "r1", "job_type": jt, "ticker": t, "asof": ASOF,
                                 "status": st, "created_at": db.now_iso()})
    cm = V.compliance(conn, paths, ASOF, ASOF, [])
    assert cm["without_decision"] == ["r1:BBB"] and cm["in_progress"] == ["r1:AAA"] and cm["n_actual"] == 1
