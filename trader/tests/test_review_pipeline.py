"""觸發檢查 pipeline：真的 skill review_due.py（--offline 申報、假價格／利率／新聞，不連網）。"""
import json

import pandas as pd
import pytest

import test_engine as TE
from trader import cli, db
from trader.config import load_config
from trader.jobs.engine import Engine
from trader.llm.fake import FakeRunner
from trader.llm.router import Router
from trader.paths import get_paths
from trader.pipelines import review as RP
from trader.pipelines.review import ReviewError, export_registry, run_review

ASOF = TE.ASOF  # 2026-10-16
CLOSE = 105.0   # 所有代號的價格平盤，避免單日 ±4% 等價格觸發干擾


class FakeAlpaca:
    def __init__(self, closes, news_rows=None):
        self.closes = closes
        self.news_rows = news_rows or []
        self.bar_calls = []
        self.news_calls = []

    def bars(self, symbols, start, end, adjustment="raw"):
        self.bar_calls.append((list(symbols), start, end, adjustment))
        rows = []
        for t in symbols:
            if t not in self.closes:
                continue
            for d in pd.bdate_range(end=end, periods=30):
                rows.append({"ticker": t, "date": d.strftime("%Y-%m-%d"), "close": self.closes[t]})
            # 決策日之後的一筆：必須被濾掉
            rows.append({"ticker": t, "date": (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                         "close": 999.0})
        return pd.DataFrame(rows, columns=["ticker", "date", "close"])

    def news(self, symbols, start, end):
        self.news_calls.append((list(symbols), start, end))
        return list(self.news_rows)


class FakeFred:
    def t30(self, asof):
        return 0.047, "2026-10-15"

    def t30_median_20y(self, asof):
        return 0.034


class BrokenFred(FakeFred):
    def t30(self, asof):
        raise RuntimeError("FRED 暫時不可用")


class FakeSec:
    user_agent = "Test Runner test@example.com"

    def __init__(self, ciks):
        self.ciks = ciks

    def cik_for(self, ticker):
        return self.ciks.get(ticker)

    def filings(self, cik, asof, since=None, forms=None):
        return []

    def filing_text(self, url):
        return "Item 8.01 Other Events. Routine notice."


# 代號 → (status, data)；HLD 無觸發、FUL 10-K、THS 10-Q、TRI 8-K 非重大項目＋價格觸發、PRC 觀察名單低於上限、
# NON 淘汰類無新事實、NOC 無 CIK 且 SEC 對照無此代號（應被排除）
SEED = {
    "HLD": ("HOLDING", {"cik": "0000000001", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                         "review_price": 100, "cost": 100, "auto_buy_cap": 80, "sell_review_above": 300,
                         "absurd_price_line": 500, "weight": 0.05, "target_weight": 0.05,
                         "first_buy_date": "2025-01-01", "t30_at_valuation": 0.047}),
    "FUL": ("WATCH", {"cik": "0000000002", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                       "auto_buy_cap": 80}),
    "THS": ("WATCH", {"cik": "0000000003", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                       "auto_buy_cap": 80}),
    "TRI": ("WATCH", {"cik": "0000000004", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                       "auto_buy_cap": 110, "kill_criteria_due": [{"text": "毛利率連兩季下滑", "due": "2026-12-31"}]}),
    "PRC": ("WATCH", {"cik": "0000000005", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                       "auto_buy_cap": 110}),
    "NON": ("TOO_HARD", {"cik": "0000000006", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01"}),
    "NOC": ("WATCH", {"last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01"}),
}

FILINGS = {
    "HLD": [],
    "FUL": [{"form": "10-K", "filingDate": "2026-10-01", "items": "", "url": ""}],
    "THS": [{"form": "10-Q", "filingDate": "2026-10-02", "items": "", "url": ""}],
    "TRI": [{"form": "8-K", "filingDate": "2026-10-03", "items": "7.01", "url": "https://example.test/8k"}],
    "PRC": [],
    "NON": [],
}

EXPECTED_JOBS = {  # 代號 → 工作類型（不含 NON、HLD）
    "FUL": "DOSSIER", "THS": "THESIS", "TRI": "TRIAGE", "PRC": "DOSSIER",
}


@pytest.fixture
def env(tmp_path, state_dir):
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=state_dir).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield cfg, paths, conn, tmp_path
    conn.close()


def _seed(conn, seed=None):
    for t, (status, data) in (seed or SEED).items():
        db.upsert(conn, "registry", {"ticker": t, "status": status, "data": data, "updated_at": db.now_iso()},
                  key=["ticker"])


def _offline(tmp_path, filings=None):
    p = tmp_path / "filings.json"
    p.write_text(json.dumps(filings if filings is not None else FILINGS), encoding="utf-8")
    return p


def _clients(alpaca=True, fred=None, sec_ciks=None):
    return {
        "alpaca": FakeAlpaca({t: CLOSE for t in SEED}, news_rows=[]) if alpaca else None,
        "fred": fred or FakeFred(),
        "sec": FakeSec(sec_ciks if sec_ciks is not None else {}),
    }


@pytest.fixture
def no_engine(monkeypatch):
    """只測建工作與訊號，不跑引擎。"""
    monkeypatch.setattr(Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})


def _jobs(conn, run_id):
    return conn.execute("SELECT job_id, ticker, job_type, params FROM jobs WHERE run_id=? ORDER BY ticker",
                        (run_id,)).fetchall()


def test_export_registry_scopes(env):
    cfg, paths, conn, _ = env
    _seed(conn)
    holdings = export_registry(conn, "holdings")
    assert [e["ticker"] for e in holdings] == ["HLD"]
    assert holdings[0]["status"] == "HOLDING" and holdings[0]["cik"] == "0000000001"
    watch = export_registry(conn, "watch")
    assert [e["ticker"] for e in watch] == ["FUL", "NOC", "PRC", "THS", "TRI"]
    assert len(export_registry(conn, "all")) == len(SEED)
    with pytest.raises(ReviewError):
        export_registry(conn, "bogus")


def test_invalid_scope_raises(env):
    cfg, paths, conn, tmp = env
    with pytest.raises(ReviewError):
        run_review(cfg, paths, conn, ASOF, scope="bogus", clients=_clients(), due_only=True)


def test_due_only_writes_signal_and_no_jobs(env):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, scope="all", clients=_clients(sec_ciks={"NOC": "0000000007"}),
                   offline_filings=_offline(tmp), due_only=True)
    assert "error" not in s, s
    assert s["run_id"] == f"review-{ASOF}-all"
    assert s["counts"] == {"NONE": 3, "FULL": 1, "THESIS": 1, "PRICE": 2}   # NOC 此處由 SEC 補到 CIK
    assert s["engine"] is None and s["jobs"] == {}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    due = json.loads((paths.work / s["run_id"] / "due.json").read_text(encoding="utf-8"))
    assert due["signal_version"] == "review-due-v2" and due["asof"] == ASOF
    assert (paths.work / s["run_id"] / "summary.json").is_file()
    row = conn.execute("SELECT kind, status FROM runs WHERE run_id=?", (s["run_id"],)).fetchone()
    assert (row["kind"], row["status"]) == ("review", "done")


def test_missing_cik_resolved_by_sec_or_skipped(env):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(sec_ciks={}), offline_filings=_offline(tmp),
                   due_only=True)
    assert [x["ticker"] for x in s["skipped"]] == ["NOC"]
    reg = json.loads((paths.work / s["run_id"] / "registry.json").read_text(encoding="utf-8"))
    assert reg["schema"] == "berkshire-deep-merged/registry-v1"
    assert "NOC" not in [e["ticker"] for e in reg["entries"]]


def test_one_job_per_ticker_and_types(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(), offline_filings=_offline(tmp))
    assert "error" not in s, s
    jobs = _jobs(conn, s["run_id"])
    got = {r["ticker"]: r["job_type"] for r in jobs}
    assert got == EXPECTED_JOBS                       # HLD、NON 不建工作
    assert len(jobs) == len(got)                      # 每檔只建一個工作
    assert all(r["job_id"] == f"{s['run_id']}:{r['ticker']}:{r['job_type']}:1" for r in jobs)
    assert s["jobs"] == EXPECTED_JOBS
    assert s["price_no_cap_input"] == ["PRC"]         # PRICE 缺 cap_inputs → 改建 DOSSIER


def test_price_with_cap_inputs_stays_price(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    (paths.state / "cap_inputs").mkdir(parents=True)
    (paths.state / "cap_inputs" / "PRC.json").write_text("{}", encoding="utf-8")
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(), offline_filings=_offline(tmp))
    assert s["jobs"]["PRC"] == "PRICE" and s["price_no_cap_input"] == []


def test_triage_carries_base_action_and_params(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(), offline_filings=_offline(tmp))
    row = [r for r in _jobs(conn, s["run_id"]) if r["ticker"] == "TRI"][0]
    p = json.loads(row["params"])
    assert row["job_type"] == "TRIAGE"
    assert p["base_action"] == "PRICE"                # 初判的原 action 保留
    assert p["triage"] and p["triage"][0]["type"] == "8-K"
    assert p["kill_criteria"] == ["毛利率連兩季下滑"]
    assert p["kill_criteria_due"] == [{"text": "毛利率連兩季下滑", "due": "2026-12-31"}]
    assert p["last_review_date"] == "2026-09-01" and p["holding"] is False


def test_params_basic_and_review_fields(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    clients = _clients()
    s = run_review(cfg, paths, conn, ASOF, clients=clients, offline_filings=_offline(tmp))
    p = json.loads([r for r in _jobs(conn, s["run_id"]) if r["ticker"] == "FUL"][0]["params"])
    assert p["price"] == CLOSE and p["price_date"] <= ASOF   # 決策日之後的 999 不得進來
    assert p["rates"] == {"t30": 0.047, "t30_date": "2026-10-15", "t30_20y_median": 0.034}
    assert set(["screen_context", "portfolio_state", "largecap_pe"]) <= set(p)
    assert p["cik"] == "0000000002" and p["base_action"] == "FULL" and p["holding"] is False
    assert p["reasons"] and p["thesis_file"] is None
    # 價格範圍：start = asof − 200 曆日，raw 調整，只到 asof
    _, start, end, adj = clients["alpaca"].bar_calls[0]
    assert end == ASOF and adj == "raw" and start == "2026-03-30"
    daily = pd.read_csv(paths.work / s["run_id"] / "daily.csv")
    assert list(daily.columns) == ["ticker", "date", "close"] and daily["date"].max() <= ASOF
    assert 999.0 not in set(daily["close"])


def test_no_alpaca_gives_empty_prices_and_no_news(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(alpaca=False), offline_filings=_offline(tmp))
    out = paths.work / s["run_id"]
    assert (out / "daily.csv").read_text(encoding="utf-8").strip() == "ticker,date,close"  # 不讓 skill 連 Yahoo
    assert not (out / "news.json").exists()
    assert s["counts"] == {"NONE": 4, "FULL": 1, "THESIS": 1}   # 無價格：PRICE 類觸發不出現；TRI 仍有初判
    assert s["jobs"] == {"FUL": "DOSSIER", "THS": "THESIS", "TRI": "TRIAGE"}


def test_t30_failure_recorded_and_flag_skipped(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(fred=BrokenFred()), offline_filings=_offline(tmp),
                   due_only=True)
    assert "FRED" in s["t30"]["error"] and "error" not in s
    assert s["counts"]["FULL"] == 1


def test_skill_failure_marks_run_failed(env):
    cfg, paths, conn, tmp = env
    _seed(conn)
    bad = tmp / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(), offline_filings=bad, due_only=True)
    assert "review_due.py 失敗" in s["error"]
    assert conn.execute("SELECT status FROM runs WHERE run_id=?", (s["run_id"],)).fetchone()[0] == "failed"


def _triage_flow(env):
    cfg, paths, conn, tmp = env
    _seed(conn, {"TRI": SEED["TRI"]})
    fake = FakeRunner(responses={"TRIAGE": TE.triage(True), "THESIS": TE.thesis("完好")})
    router = Router(cfg, paths.routing_json, dry_run=True, fake=fake)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(), router=router,
                   offline_filings=_offline(tmp, {"TRI": FILINGS["TRI"]}))
    return s, conn


def test_triage_to_thesis_full_flow(env):
    """TRIAGE 初判 escalate → THESIS（FakeRunner，不連網）。"""
    s, conn = _triage_flow(env)
    assert "error" not in s and s["engine"]["failed"] == 0, s
    assert [r["job_type"] for r in _jobs(conn, s["run_id"])] == ["TRIAGE", "THESIS"]
    rows = conn.execute("SELECT job_type, status FROM jobs ORDER BY rowid").fetchall()
    assert [(r["job_type"], r["status"]) for r in rows] == [("TRIAGE", "done"), ("THESIS", "done")]


def test_triage_escalation_carries_full_params(env):
    s, conn = _triage_flow(env)
    thesis = [r for r in _jobs(conn, s["run_id"]) if r["job_type"] == "THESIS"][0]
    p = json.loads(thesis["params"])
    assert p["base_action"] == "PRICE" and p["cik"] == "0000000004" and p["kill_criteria"]


def test_cli_review_due_only(tmp_path, state_dir, monkeypatch, capsys):
    cfg_path = TE._write_config(tmp_path)
    paths = get_paths(state_dir=state_dir).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    _seed(conn)
    conn.close()
    monkeypatch.setattr(RP, "make_clients", lambda cfg, paths: _clients(sec_ciks={}))
    rc = cli.main(["review", "--asof", ASOF, "--scope", "holdings", "--due-only",
                   "--offline-filings", str(_offline(tmp_path)), "--config", str(cfg_path),
                   "--state-dir", str(state_dir)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert '"counts"' in out and '"run_id": "review-2026-10-16-holdings"' in out


def test_export_registry_drops_none_fields(env):
    conn = env[2]
    db.upsert(conn, "registry", {"ticker": "NUL", "status": "REJECT",
                                 "data": {"cik": "1", "kill_criteria_due": None, "grade": None},
                                 "updated_at": db.now_iso()}, key=["ticker"])
    e = [x for x in export_registry(conn, "all") if x["ticker"] == "NUL"][0]
    assert "kill_criteria_due" not in e and "grade" not in e and e["cik"] == "1"
