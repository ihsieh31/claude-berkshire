"""F3-LEDGER（A3 round3）：book.adjust（A3-M7A-01／05／06、A3-V3-3-02）、人工指令紐約日與入金守門
（A3-M8A-01＋A3-M8C2-01）、CLI 雜項（A3-M8B2-01、M8B-02、M8B-03、M8B-06、M8E-01）、設定範圍（A3-M8C-02、M8D-06）。
全部離線。"""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import trader.cli as cli
import trader.data.http as http
from trader import db
from trader import scheduler as S
from trader.config import BudgetCfg, ConfigError, _build_config
from trader.jobs.engine import Engine
from trader.ledger import book, broker
from trader.ledger import corp_actions as ca
from trader.ledger.book import LedgerError
from trader.paths import get_paths
from trader.pipelines.approved import read_status, write_status
from trader.report import metrics as M

D0 = "2026-10-01"
EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


@pytest.fixture
def paths(state_dir):
    return get_paths().ensure()


@pytest.fixture
def conn(paths):
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, 100000, D0)
    yield c
    c.close()


def _cli(paths, *argv):
    return cli.main([*argv, "--state-dir", str(paths.state)])


def _hold(conn, t="KO", qty=100, avg=50.0):
    book.adjust(conn, date=D0, ticker=t, set_qty=qty, set_avg_cost=avg, note="seed")


def _bar(px):
    return {"open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 1e6}


# ---------------------------------------------------------------- A3-M7A-01 對帳不改 TRIM 限價

def test_adjust_reconcile_keeps_trim_limit(conn):
    _hold(conn)
    oid = broker.place_order(conn, "KO", "SELL", 30, 90.0, None, "2026-10-14")
    r = book.adjust(conn, date="2026-10-15", ticker="KO", set_qty=150, note="對帳補記")
    o = conn.execute("SELECT qty, limit_price FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert "registry_rescaled" not in r and r["warnings"]
    assert (o["qty"], o["limit_price"]) == (45, 90.0)       # 修前 60.0：可在觸發線 90 以下成交（違 D03）


def test_adjust_split_rescales_trim_limit(conn):
    _hold(conn)
    oid = broker.place_order(conn, "KO", "SELL", 30, 90.0, None, "2026-10-14")
    r = book.adjust(conn, date="2026-10-15", ticker="KO", set_qty=200, set_avg_cost=25.0, note="2:1 拆股")
    o = conn.execute("SELECT qty, limit_price FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert r["registry_rescaled"]["ratio"] == 2.0
    assert (o["qty"], o["limit_price"]) == (60, 45.0)


# ---------------------------------------------------------------- A3-M7A-05 警示不被覆蓋

def test_adjust_unpause_keeps_qty_warning(conn):
    _hold(conn)
    ca._ca_insert(conn, ticker="KO", atype="其他", eff="2026-10-15", ratio=None, cash=None, new_ticker=None,
                  status="pending_human", source="8k", data={})
    conn.commit()
    r = book.adjust(conn, date="2026-10-15", ticker="KO", set_qty=150, unpause="KO", note="對帳")
    assert len(r["warnings"]) == 2
    assert "視為對帳" in r["warnings"][0] and "未結公司行動" in r["warnings"][1]


# ---------------------------------------------------------------- A3-M7A-06 無持股 --qty 0

def test_adjust_qty0_unheld_rejected(conn):
    book.write_row(conn, "registry", {"ticker": "KO", "status": "WATCH", "data": {"auto_buy_cap": 50.0},
                                      "updated_at": db.now_iso()})
    conn.commit()
    with pytest.raises(LedgerError, match="無持股"):
        book.adjust(conn, date="2026-10-16", ticker="KO", set_qty=0, note="打錯代號")
    data = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])
    assert data["auto_buy_cap"] == 50.0 and "sold_out_at" not in data


# ---------------------------------------------------------------- A3-V3-3-02 --unpause 不吞掉工作失敗事件

EV = {"ticker": "KO", "event_id": "KO:alpaca:2026-10-16:a1", "source": "alpaca", "date": "2026-10-16"}


def test_unpause_keeps_failed_ca_event_for_retry(conn):
    _hold(conn, qty=10, avg=100.0)
    ca.record_job_failure(conn, EV, "job1", "timeout")
    assert "KO" in book.paused(conn)
    r = book.adjust(conn, date="2026-10-16", unpause="KO", note="先恢復交易")
    assert r["resolved"] == [] and r["unpaused_only"] and EV["event_id"] in r["warnings"][0]
    assert "KO" not in book.paused(conn)                                            # 已解除暫停
    assert [e["event_id"] for e in ca.unresolved_failed_events(conn)] == [EV["event_id"]]   # 修前 []：永不重派
    a = M.anomalies(conn)["open"]
    assert [x["kind"] for x in a] == ["corp_action_job_failed"] and "--resolve" in a[0]["hint"]


def test_unpause_resolves_manual_pause_but_not_failed_event(conn):
    _hold(conn, qty=10, avg=100.0)
    ca.record_job_failure(conn, EV, "job1", "timeout")
    aid = book.pause(conn, "KO", date="2026-10-16", note="人工")
    r = book.adjust(conn, date="2026-10-16", unpause="KO", note="恢復")
    assert r["resolved"] == [aid] and "KO" not in book.paused(conn)
    assert ca.unresolved_failed_events(conn)


# ---------------------------------------------------------------- A3-M8C2-01 入金守門

def _navs(conn, dates):
    for d in dates:
        book.record_nav(conn, d, {}, None)


def test_deposit_on_last_nav_date_shifted(conn):
    _navs(conn, ["2026-10-15", "2026-10-16"])
    r = book.adjust(conn, date="2026-10-16", deposit=50000, note="daily 後入金")
    assert r["deposit_date_shifted"]["to"] == "2026-10-19"   # 週五 → 下週一
    _navs(conn, ["2026-10-19", "2026-10-20"])
    assert [round(x["account_index"], 6) for x in M.series(conn)] == [1.0] * 4   # 修前 [1, 0.75?…] 永久扭曲


def test_deposit_backfill_rejected(conn):
    _navs(conn, ["2026-10-15", "2026-10-16", "2026-10-19"])
    with pytest.raises(LedgerError, match="早於最後淨值日"):
        book.adjust(conn, date="2026-10-16", deposit=50000, note="回填")
    assert conn.execute("SELECT COUNT(*) FROM cash_ledger WHERE kind='deposit'").fetchone()[0] == 1


def test_deposit_after_last_nav_unchanged(conn):
    _navs(conn, ["2026-10-15"])
    r = book.adjust(conn, date="2026-10-16", deposit=50000, note="daily 前入金")
    assert "deposit_date_shifted" not in r
    _navs(conn, ["2026-10-16"])
    assert [round(x["account_index"], 6) for x in M.series(conn)] == [1.0, 1.0]


# ---------------------------------------------------------------- A3-M8A-01 人工指令用紐約日

def test_cli_manual_dates_ny(paths, conn, monkeypatch):
    monkeypatch.setattr(http, "ny_today", lambda: "2026-10-19")   # 台北 10-20 上午＝紐約 10-19 晚上
    book.write_row(conn, "registry", {"ticker": "OLD", "status": "WATCH", "data": {}, "updated_at": db.now_iso()})
    conn.commit()
    assert _cli(paths, "trade", "buy", "--ticker", "KO", "--qty", "10", "--limit", "70", "--note", "x") == 0
    assert _cli(paths, "ledger", "pause", "PEP", "--note", "x") == 0
    assert _cli(paths, "registry", "retire", "OLD", "--note", "x") == 0
    o = conn.execute("SELECT order_id, created_date FROM orders").fetchone()
    assert o["created_date"] == "2026-10-19"
    assert json.loads(conn.execute("SELECT detail FROM anomalies WHERE kind='manual_pause'").fetchone()[0])["date"] \
        == "2026-10-19"
    assert json.loads(conn.execute("SELECT data FROM registry WHERE ticker='OLD'").fetchone()[0])["retired"]["date"] \
        == "2026-10-19"
    fills = broker.match_orders(conn, "2026-10-20", {"KO": _bar(69.0)}, 0, 0)   # 下一交易日即撮合
    assert [f["order_id"] for f in fills] == [o["order_id"]]


def test_cli_deposit_after_daily_ny_date_shifted(paths, conn, monkeypatch, capsys):
    monkeypatch.setattr(http, "ny_today", lambda: "2026-10-19")
    _navs(conn, ["2026-10-16", "2026-10-19"])
    assert _cli(paths, "ledger", "adjust", "--deposit", "50000", "--note", "入金") == 0
    assert '"to": "2026-10-20"' in capsys.readouterr().out


# ---------------------------------------------------------------- A3-M8B2-01 賣出扣未成交賣單

def test_trade_sell_counts_open_sells(paths, conn, capsys):
    _hold(conn)
    conn.commit()
    assert _cli(paths, "trade", "sell", "--ticker", "KO", "--qty", "30", "--note", "x") == 0
    assert _cli(paths, "trade", "sell", "--ticker", "KO", "--qty", "80", "--note", "x") == 1   # 修前 0
    assert "可賣 70 股" in capsys.readouterr().out
    assert _cli(paths, "trade", "sell", "--ticker", "KO", "--qty", "70", "--note", "x") == 0
    sig = [r[0] for r in conn.execute("SELECT signal FROM decisions WHERE reason_code='MANUAL' ORDER BY decision_id")]
    assert sig == ["TRIM", "SELL_ALL"]


# ---------------------------------------------------------------- A3-M8B-02 寫入鎖

def test_writes_state_reject_defer_fetch_freeze():
    p = cli.build_parser()
    ws = lambda *a: cli._writes_state(p.parse_args(list(a)))   # noqa: E731
    assert ws("approvals", "reject", "1") and ws("approvals", "defer", "1")
    assert ws("approvals", "approve", "1", "--run") and not ws("approvals", "approve", "1")
    assert not ws("approvals", "list")
    assert ws("fetch", "--asof", "2026-10-01", "--freeze-pe") and ws("fetch", "--asof", "2026-10-01", "--refreeze")
    assert not ws("fetch", "--asof", "2026-10-01")
    assert not ws("scheduler", "reset", "daily")   # 依復查：scheduler reset 不加鎖


def test_dashboard_reject_defer_busy_409(paths):
    from trader.dashboard.server import TOKEN_FILE, create_app
    port = 8787
    app = create_app(paths, port, spawn=lambda: None)
    client = TestClient(app, base_url=f"http://127.0.0.1:{port}")
    tok = (paths.state / TOKEN_FILE).read_text().strip()
    j = {"host": f"127.0.0.1:{port}", "content-type": "application/json", "x-dashboard-token": tok}
    with S.pipeline_lock(paths, "trader daily"):
        for ep in ("defer", "reject"):
            r = client.post(f"/api/approvals/{ep}", content=json.dumps({"ids": [1]}), headers=j)
            assert r.status_code == 409 and "執行中" in r.json()["detail"], ep
    r = client.post("/api/approvals/defer", content=json.dumps({"ids": [1]}), headers=j)
    assert r.status_code == 409 and "執行中" not in r.json()["detail"]   # 鎖空：照常處理（找不到編號）


# ---------------------------------------------------------------- A3-M8B-03 cap_inputs 換算失敗

def test_ledger_adjust_rescale_failure_records_anomaly(paths, conn, capsys):
    _hold(conn)
    conn.commit()
    (paths.state / "cap_inputs").mkdir(exist_ok=True)
    (paths.state / "cap_inputs" / "KO.json").write_text("{壞掉", encoding="utf-8")
    rc = _cli(paths, "ledger", "adjust", "--ticker", "KO", "--qty", "200", "--avg-cost", "25", "--note", "2:1 拆股")
    assert rc == 0                                                    # 修前：JSONDecodeError 未攔
    out = json.loads(capsys.readouterr().out)
    assert any("cap_inputs 換算失敗" in w for w in out["warnings"])
    assert "KO" in book.paused(conn)
    assert conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='cap_input_replace_failed' AND status='open'"
                        ).fetchone()[0] == 1
    assert book.positions(conn)["KO"]["qty"] == 200


def test_rescale_cap_after_adjust_ok(paths, conn):
    (paths.state / "cap_inputs").mkdir(exist_ok=True)
    f = paths.state / "cap_inputs" / "KO.json"
    f.write_text(json.dumps({"oe": 4.0, "price": 80.0}), encoding="utf-8")
    assert ca.rescale_cap_after_adjust(conn, paths, "KO", 2.0) is None
    assert json.loads(f.read_text(encoding="utf-8")) == {"oe": 2.0, "price": 40.0}


# ---------------------------------------------------------------- A3-M8B-06 run-approved 被鎖擋下

def test_run_approved_busy_writes_status(paths):
    write_status(paths, {"started_at": db.now_iso(), "pid": os.getpid(), "spawned_by": "dashboard"})
    with S.pipeline_lock(paths, "trader daily"):
        assert _cli(paths, "run-approved") == 2
    st = read_status(paths)
    assert st["finished_at"] and st["busy"] is True and st["exit_code"] == 2


def test_run_approved_busy_leaves_other_status(paths):
    write_status(paths, {"started_at": db.now_iso(), "pid": os.getpid() + 999999})
    with S.pipeline_lock(paths, "trader daily"):
        assert _cli(paths, "run-approved") == 2
    assert "finished_at" not in read_status(paths)


# ---------------------------------------------------------------- A3-M8E-01 doctor 不跟隨轉址

def test_doctor_probe_no_redirect(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.httpx, "get", lambda url, **kw: seen.update(kw) or types.SimpleNamespace(status_code=302))
    assert cli._http_get("https://example.invalid", headers={"APCA-API-KEY-ID": "k"}).status_code == 302
    assert seen["follow_redirects"] is False
    res = []
    monkeypatch.setattr(cli, "_http_get", lambda *a, **k: types.SimpleNamespace(status_code=302))
    cli._probe("Alpaca", "https://example.invalid", {}, 5, [], res)
    assert "302" in json.dumps(res, ensure_ascii=False)


# ---------------------------------------------------------------- A3-M8C-02／A3-M8D-06 設定

def _raw():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_budget_zero_calls_unlimited(conn):
    fake = types.SimpleNamespace(conn=conn, _inflight=set(), _daily_exhausted=lambda rid: False,
                                 config=types.SimpleNamespace(budget=BudgetCfg(max_llm_calls_per_run=0)))
    assert Engine._budget_exhausted(fake, "r1") is False   # 修前 True：每 run 立即 paused_budget
    fake.config.budget = BudgetCfg(max_llm_calls_per_run=1)
    fake._inflight = {"j"}
    assert Engine._budget_exhausted(fake, "r1") is True


def test_config_example_ok():
    _build_config(_raw(), None)


@pytest.mark.parametrize("key", ["max_llm_calls_per_run", "max_cost_usd_per_run", "max_input_tokens_per_run",
                                 "max_llm_calls_per_day", "max_cost_usd_per_day"])
def test_config_rejects_negative_budget(key):
    raw = _raw()
    raw["budget"][key] = -1
    with pytest.raises(ConfigError, match=f"budget.{key}"):
        _build_config(raw, None)


@pytest.mark.parametrize("key,val", [("initial_cash", 0), ("initial_cash", -5), ("slippage_bps", -50),
                                     ("slippage_bps", 20000), ("slippage_bps", 1000), ("commission_per_order", -1)])
def test_config_account_ranges(key, val):
    raw = _raw()
    raw["account"][key] = val
    with pytest.raises(ConfigError, match=f"account.{key}"):
        _build_config(raw, None)


# ---------------------------------------------------------------- A3-V3-3-02 人工拆股＋unpause 後重派不再入帳

EV_S = {**EV, "price_before": 100.0, "price_after": 25.0,
        "alpaca": {"type": "forward_split", "ratio_new_per_old": 4.0}}
RES_S = {"ticker": "KO", "action_type": "拆股", "effective_date": "2026-10-16", "ratio_new_per_old": 4.0,
         "fractional_handling": "無條件捨去", "needs_human": False}


def _qty(conn, t="KO"):
    r = conn.execute("SELECT qty FROM positions WHERE ticker=?", (t,)).fetchone()
    return r["qty"] if r else None


def _redispatch(conn, paths):
    """比照 corp_jobs.CorpActionHandler.post：重派事件 apply，conflict 走 reject，最後結案 job_failed。"""
    evs = ca.unresolved_failed_events(conn)
    outs = []
    for ev in evs:
        out = ca.apply(conn, paths, RES_S, ev, "2026-10-19")
        if out["status"] == "conflict":
            ca.reject(conn, RES_S, ev, out["errors"])
        ca.resolve_job_failed(conn, ev.get("event_id"), out["status"])
        outs.append(out)
    return outs


def test_manual_split_unpause_same_call_resolves_failed_event(conn, paths):
    _hold(conn, qty=10, avg=100.0)
    ca.record_job_failure(conn, EV_S, "job1", "timeout")
    r = book.adjust(conn, date="2026-10-16", ticker="KO", set_qty=40, set_avg_cost=25.0, unpause="KO",
                    note="人工拆股入帳")
    assert r["resolved_job_failed"] and r["resolved"] == r["resolved_job_failed"]
    assert "KO" not in book.paused(conn)
    assert ca.unresolved_failed_events(conn) == []          # 修前仍重派 → apply 再 ×4 成 160 股
    assert _redispatch(conn, paths) == [] and _qty(conn) == 40


def test_manual_split_then_separate_unpause_redispatch_conflicts(conn, paths):
    _hold(conn, qty=10, avg=100.0)
    ca.record_job_failure(conn, EV_S, "job1", "timeout")
    book.adjust(conn, date="2026-10-16", ticker="KO", set_qty=40, set_avg_cost=25.0, note="人工拆股入帳")
    r = book.adjust(conn, date="2026-10-16", unpause="KO", note="恢復交易")
    assert r["unpaused_only"] and "KO" not in book.paused(conn)
    assert [e["event_id"] for e in ca.unresolved_failed_events(conn)] == [EV_S["event_id"]]
    outs = _redispatch(conn, paths)
    assert [o["status"] for o in outs] == ["conflict"] and "人工調整持股" in outs[0]["errors"][0]
    assert _qty(conn) == 40                                   # 修前 160：重複入帳
    assert "KO" in book.paused(conn)                          # 轉人工：再暫停、待人工公司行動列
    assert conn.execute("SELECT status FROM corporate_actions WHERE ticker='KO'").fetchone()["status"] == "anomaly"
    assert ca.unresolved_failed_events(conn) == []            # 不再每天重派


def test_unpause_only_redispatch_still_applies(conn, paths):
    _hold(conn, qty=10, avg=100.0)                            # seed 的 manual_adjust 在失敗之前（同秒）：不擋
    ca.record_job_failure(conn, EV_S, "job1", "timeout")
    book.adjust(conn, date="2026-10-16", unpause="KO", note="先交易")
    outs = _redispatch(conn, paths)
    assert [o["status"] for o in outs] == ["applied"] and _qty(conn) == 40
    assert ca.unresolved_failed_events(conn) == [] and "KO" not in book.paused(conn)


def test_unpause_with_unrelated_ticker_qty_change_keeps_event(conn):
    _hold(conn, qty=10, avg=100.0)
    _hold(conn, t="PEP", qty=5, avg=100.0)
    ca.record_job_failure(conn, EV_S, "job1", "timeout")
    r = book.adjust(conn, date="2026-10-16", ticker="PEP", set_qty=6, unpause="KO", note="別檔對帳")
    assert r["unpaused_only"] and "resolved_job_failed" not in r
    assert ca.unresolved_failed_events(conn)


def test_repeat_failure_records_attempts_without_repausing(conn):
    _hold(conn, qty=10, avg=100.0)
    assert ca.record_job_failure(conn, EV_S, "job1", "timeout") is True
    book.adjust(conn, date="2026-10-16", unpause="KO", note="先交易")
    assert ca.record_job_failure(conn, EV_S, "job2", "quota") is False
    row = conn.execute("SELECT detail, pause_trading FROM anomalies WHERE kind='corp_action_job_failed'").fetchone()
    d = json.loads(row["detail"])
    assert (d["attempts"], d["last_error"], d["last_job_id"], d["error"]) == (2, "quota", "job2", "timeout")
    assert row["pause_trading"] == 0 and "KO" not in book.paused(conn)


def test_ledger_help_and_job_failed_hint_describe_flow(conn):
    _hold(conn, qty=10, avg=100.0)
    ca.record_job_failure(conn, EV_S, "job1", "timeout")
    a = M.anomalies(conn)
    assert "ledger adjust" in a["ledger_help"] and "同一指令" in a["ledger_help"] and "--resolve-corp-action" in a["ledger_help"]
    h = a["open"][0]["hint"]
    assert "--unpause KO" in h and "同一指令改股數" in h and "--resolve" in h

