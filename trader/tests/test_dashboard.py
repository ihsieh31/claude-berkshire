"""M6b：指標計算與本機 API（權杖、Host、Origin）。不連網。"""
from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from test_approvals import _env, _run_to_gate
from test_engine import dossier_ok, full_responses

from trader import db
from trader.dashboard.server import TOKEN_FILE, create_app, ensure_token
from trader.report import metrics as M

PORT = 8787
H = {"host": f"127.0.0.1:{PORT}"}


def _nav(conn, d, nav, cash, spy, div=None, prices=None):
    db.insert(conn, "nav_daily", {"date": d, "nav": nav, "cash": cash, "positions_value": nav - cash,
                                  "n_positions": len(prices or {}), "benchmark_close": spy,
                                  "data": {"receivables": 0.0, "prices": prices or {}, "benchmark_dividend": div,
                                           "weights": {}, "missing_close": []}})


def _cash(conn, d, amt, kind, ref="X"):
    db.insert(conn, "cash_ledger", {"date": d, "amount": amt, "kind": kind, "ref": ref})


def _book(conn):
    """入金 10 萬；買 KO 100 股 @50、PEP 200 股 @100；KO 股利 30；SPY 500 → 505（除息 2）→ 510。"""
    _cash(conn, "2026-01-02", 100000.0, "deposit", "INIT")
    _cash(conn, "2026-01-05", -5000.0, "buy")
    _cash(conn, "2026-01-05", -20000.0, "buy")
    _cash(conn, "2026-01-07", 30.0, "dividend")
    for t, q, c in (("KO", 100, 50.0), ("PEP", 200, 100.0)):
        db.insert(conn, "positions", {"ticker": t, "qty": q, "avg_cost": c, "first_buy_date": "2026-01-05",
                                      "updated_at": db.now_iso()})
    _nav(conn, "2026-01-02", 100000.0, 100000.0, 500.0)
    _nav(conn, "2026-01-05", 100000.0, 75000.0, 505.0, div=2.0, prices={"KO": 50.0, "PEP": 100.0})
    # KO 60（+1000）、PEP 105（+1000）、股利 30 → 淨值 102,030
    _nav(conn, "2026-01-07", 102030.0, 75030.0, 510.0, prices={"KO": 60.0, "PEP": 105.0})


def test_summary_metrics(tmp_path):
    env = _env(tmp_path)
    _book(env.conn)
    s = M.summary(env.conn)
    assert s["return"] == pytest.approx(0.0203)
    spy = (505 + 2) / 500 * 510 / 505 - 1
    assert s["spy_return"] == pytest.approx(spy, abs=1e-6)
    assert s["excess_pp"] == pytest.approx((0.0203 - spy) * 100, abs=1e-3)
    assert s["pnl"] == {"total": 2030.0, "unrealized": 2000.0, "dividends": 30.0, "realized_other": 0.0}
    assert s["cash_ratio"] == pytest.approx(75030 / 102030, abs=1e-6)
    assert [x["ticker"] for x in s["top3"]] == ["PEP", "KO"]
    assert s["top3"][1]["return"] == pytest.approx(0.2)
    assert s["approvals"]["n_pending"] == 0 and s["approvals"]["label"] is None


def test_twr_ignores_later_deposit(tmp_path):
    env = _env(tmp_path)
    c = env.conn
    _cash(c, "2026-01-02", 100000.0, "deposit")
    _nav(c, "2026-01-02", 100000.0, 100000.0, 500.0)
    _nav(c, "2026-01-05", 110000.0, 100000.0, 500.0)      # +10%
    _cash(c, "2026-01-06", 50000.0, "deposit")
    _nav(c, "2026-01-06", 160000.0, 150000.0, 500.0)      # 入金 5 萬，無損益
    s = M.summary(c)
    assert s["return"] == pytest.approx(0.10)
    assert s["pnl"]["total"] == 10000.0


def test_chart_rebases_window(tmp_path):
    env = _env(tmp_path)
    _book(env.conn)
    pts = M.chart(env.conn, "ALL")["points"]
    assert pts[0] == {"date": "2026-01-02", "account": 100.0, "spy": 100.0}
    assert pts[-1]["account"] == pytest.approx(102.03)
    with pytest.raises(ValueError):
        M.chart(env.conn, "5Y")


def test_empty_summary(tmp_path):
    env = _env(tmp_path)
    s = M.summary(env.conn)
    assert s["empty"] is True and M.chart(env.conn)["points"] == []


# ---------------------------------------------------------------- API

@pytest.fixture
def api(tmp_path):
    # 輸出 ticker 須與工作相符（M3_D1-10）：PEP 的 DOSSIER 回應另給，否則 PEP 停在 conservative_fallback、沒有同意列 #2
    env = _env(tmp_path, {**full_responses(), ("DOSSIER", "PEP"): {**dossier_ok(), "ticker": "PEP"}})
    calls = []
    app = create_app(env.paths, PORT, spawn=lambda: calls.append(1))
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    tok = (env.paths.state / TOKEN_FILE).read_text().strip()
    return env, client, tok, calls


def test_token_file_mode(tmp_path):
    env = _env(tmp_path)
    t1 = ensure_token(env.paths)
    assert ensure_token(env.paths) == t1 and len(t1) >= 32
    assert stat.S_IMODE((env.paths.state / TOKEN_FILE).stat().st_mode) == 0o600


def test_host_guard_and_reads(api):
    env, client, tok, _ = api
    assert client.get("/api/summary", headers={"host": "evil.example:8787"}).status_code == 403
    r = client.get("/api/summary", headers=H)
    assert r.status_code == 200 and r.json()["empty"] is True
    for path in ("/api/nav?range=1M", "/api/holdings", "/api/trades", "/api/analyses", "/api/usage",
                 "/api/anomalies", "/api/approvals"):
        assert client.get(path, headers=H).status_code == 200, path
    assert client.get("/api/nav?range=X", headers=H).status_code == 400
    assert client.get("/api/analyses/NOPE", headers=H).status_code == 404
    page = client.get("/", headers=H)
    assert tok in page.text and page.headers["cache-control"] == "no-store"


def test_approve_requires_token_origin_json(api):
    env, client, tok, calls = api
    _run_to_gate(env)
    body = json.dumps({"ids": [1]})
    j = {**H, "content-type": "application/json"}
    assert client.post("/api/approvals/approve", content=body, headers=j).status_code == 401
    assert client.post("/api/approvals/approve", content=body,
                       headers={**j, "x-dashboard-token": tok, "origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/approvals/approve", content=body,
                       headers={**H, "x-dashboard-token": tok, "content-type": "text/plain"}).status_code == 415
    assert calls == [] and env.conn.execute("SELECT status FROM approvals").fetchone()[0] == "pending"

    # D54：不收 all（同意只涵蓋畫面上的編號）
    assert client.post("/api/approvals/approve", content=json.dumps({"all": True}),
                       headers={**j, "x-dashboard-token": tok}).status_code == 400
    r = client.post("/api/approvals/approve", content=body,
                    headers={**j, "x-dashboard-token": tok, "origin": f"http://127.0.0.1:{PORT}"})
    assert r.status_code == 200 and r.json() == {"approved": [1], "started": True, "note": None}
    assert calls == [1]
    row = env.conn.execute("SELECT status, decided_via FROM approvals").fetchone()
    assert tuple(row) == ("approved", "web")
    # 重複同意 → 409
    r = client.post("/api/approvals/approve", content=json.dumps({"ids": [1]}), headers={**j, "x-dashboard-token": tok})
    assert r.status_code == 409


def test_defer_reject_and_report(api):
    env, client, tok, _ = api
    env.price_file("PEP")
    _run_to_gate(env)
    _run_to_gate(env, ticker="PEP")
    j = {**H, "content-type": "application/json", "x-dashboard-token": tok}
    assert client.post("/api/approvals/defer", content=json.dumps({"ids": [1], "via": "desktop"}),
                       headers=j).json() == {"deferred": [1]}
    assert client.post("/api/approvals/defer", content=json.dumps({"all": True}), headers=j).status_code == 400
    r = client.post("/api/approvals/reject", content=json.dumps({"ids": [2], "note": "額度"}), headers=j)
    did = r.json()["decisions"][0]
    a = client.get("/api/approvals", headers=H).json()
    assert [x["ticker"] for x in a["items"]] == ["KO"] and a["summary"]["n_deferred"] == 1 and a["running"] is False
    rep = client.get(f"/api/analyses/{did}", headers=H).json()
    assert rep["ticker"] == "PEP" and rep["log"]["action"] in ("NO_ACTION", None)
    an = client.get("/api/analyses", headers=H).json()
    assert an[0]["decision_id"] == did and an[0]["reason_code"] == "MISSING_DATA"


def test_holdings_detail(tmp_path):
    env = _env(tmp_path)
    _book(env.conn)
    db.insert(env.conn, "registry", {"ticker": "KO", "status": "HOLDING", "updated_at": db.now_iso(), "data": json.dumps({
        "auto_buy_cap": 55.0, "sell_review_above": 70.0, "absurd_price_line": 150.0, "grade": "B",
        "kill_criteria_due": [{"text": "a", "due": "2027-03-01"}, {"text": "b", "due": "2026-12-01"}],
        "next_full_review_due": "2027-02-20"})})
    h = {x["ticker"]: x for x in M.holdings(env.conn)}
    ko = h["KO"]
    assert ko["price"] == 60.0 and ko["value"] == 6000.0 and ko["return"] == pytest.approx(0.2)
    assert ko["auto_buy_cap"] == 55.0 and ko["next_kill_check"] == "2026-12-01" and ko["paused"] is False
    assert ko["add_blocked"] is None and h["PEP"]["registry_status"] is None
