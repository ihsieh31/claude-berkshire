"""P46：帳本人工操作（入金、撤單、暫停、估值）、決策作廢、公司行動人工結案、人工下單（D12）、
registry retire、jobs list、db backup，以及 P24 交接（TRIM 限價單作廢、拆股換算 last_trim）。全部離線。"""
from __future__ import annotations

import json
import sqlite3

import pytest

import trader.cli as cli
from trader import db
from trader import scheduler as S
from trader.ledger import book, broker
from trader.ledger import corp_actions as ca
from trader.ledger.book import LedgerError
from trader.ledger.broker import BrokerError
from trader.paths import get_paths
from trader.pipelines.review import export_registry
from trader.portfolio import sizing as Z
from trader.report import metrics as M
from trader.report import validation as V

D0, D1, D2, D3 = "2026-10-01", "2026-10-14", "2026-10-15", "2026-10-16"
NOW = "2026-10-16T00:00:00Z"


@pytest.fixture
def paths(state_dir):
    return get_paths().ensure()


@pytest.fixture
def conn(paths):
    c = db.connect(paths.db)
    db.migrate(c)
    yield c
    c.close()


def _bar(o, h, l, c=None):
    return {"open": o, "high": h, "low": l, "close": c if c is not None else o}


def _hold(conn, t, qty, avg, first="2025-01-02"):
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?)", (t, qty, avg, first, NOW))
    conn.commit()


def _reg(conn, t, status="WATCH", data=None):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "first_buy_date": None,
                                 "data": data or {"grade": "B"}, "updated_at": NOW}, ["ticker"])


def _dec(conn, did, t, date, data, created=NOW, run=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": date, "run_id": run,
                                  "job_id": None, "signal": data.get("action"), "reason_code": None,
                                  "data": data, "created_at": created})


def _cli(state_dir, *argv):
    return cli.main([*argv, "--state-dir", str(state_dir)])


# ---------------------------------------------------------------- M8-D1-04 入金

def test_deposit_is_external_flow_not_return(conn):
    book.ensure_initial_deposit(conn, 100_000, D0)
    book.record_nav(conn, D1, {}, None)
    r = book.adjust(conn, date=D2, deposit=50_000, note="入金")
    assert r["deposit"] == 50_000
    book.record_nav(conn, D2, {}, None)
    s = M.summary(conn)
    assert s["deposits"] == pytest.approx(150_000)
    assert s["return"] == pytest.approx(0.0)
    # --cash 行為不變：kind=adjust，算帳戶內損益
    book.adjust(conn, date=D3, cash_delta=1_500, note="調帳")
    kinds = [r[0] for r in conn.execute("SELECT kind FROM cash_ledger ORDER BY entry_id")]
    assert kinds == ["deposit", "deposit", "adjust"]
    with pytest.raises(LedgerError):
        book.adjust(conn, date=D3, deposit=0, note="x")


def test_cli_ledger_adjust_deposit(state_dir, conn):
    assert _cli(state_dir, "ledger", "adjust", "--deposit", "250", "--note", "入金") == 0
    assert conn.execute("SELECT kind, amount FROM cash_ledger").fetchone()[:] == ("deposit", 250.0)


# ---------------------------------------------------------------- M8-D4-03 撤單、暫停

def test_cli_ledger_cancel_and_match_does_not_fill(state_dir, conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    oid = broker.place_order(conn, "AAPL", "BUY", 10, 100.0, None, D1)
    assert _cli(state_dir, "ledger", "cancel", oid, "--note", "錯單") == 0
    st, why = conn.execute("SELECT status, cancel_reason FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert st == "cancelled" and "錯單" in why
    a = conn.execute("SELECT kind, status, pause_trading FROM anomalies").fetchone()
    assert tuple(a) == ("manual_cancel", "resolved", 0)
    assert broker.match_orders(conn, D2, {"AAPL": _bar(90, 91, 89)}, 0, 0) == []
    assert _cli(state_dir, "ledger", "cancel", oid, "--note", "再撤") == 1   # 已非 open


def test_pause_all_blocks_buy_and_sell_all_until_unpause(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _hold(conn, "MSFT", 5, 50.0)
    _dec(conn, "MSFT-D-1", "MSFT", D1, {"action": "SELL_ALL"})
    aid = book.pause(conn, None, date=D1, note="錯單止血")
    assert "ANY" in book.paused(conn)
    with pytest.raises(BrokerError):
        broker.place_order(conn, "AAPL", "BUY", 1, 10.0, None, D1)
    # 人工暫停：SELL_ALL 全清仍建立委託（決策只讀一次，不可遺失），但不撮合（M7-RS7-02）
    oid = broker.place_order(conn, "MSFT", "SELL", 5, None, "MSFT-D-1", D1)
    assert broker.match_orders(conn, D2, {"MSFT": _bar(50, 51, 49)}, 0, 0) == []
    r = book.adjust(conn, date=D2, unpause=book.GLOBAL, note="恢復")
    assert r["resolved"] == [aid]
    assert [f["order_id"] for f in broker.match_orders(conn, D3, {"MSFT": _bar(50, 51, 49)}, 0, 0)] == [oid]
    broker.place_order(conn, "AAPL", "BUY", 1, 10.0, None, D3)


def test_manual_ticker_pause_blocks_sell_all_but_system_pause_does_not(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _hold(conn, "MSFT", 5, 50.0)
    _dec(conn, "MSFT-D-1", "MSFT", D1, {"action": "SELL_ALL"})
    db.insert(conn, "anomalies", {"ticker": "MSFT", "kind": "corp_action_check_failed", "status": "open",
                                  "pause_trading": 1, "created_at": NOW})
    oid = broker.place_order(conn, "MSFT", "SELL", 5, None, "MSFT-D-1", D1)   # 系統暫停：全清放行（D06）
    book.pause(conn, "MSFT", date=D1, note="人工")
    assert broker.match_orders(conn, D2, {"MSFT": _bar(50, 51, 49)}, 0, 0) == []   # 人工暫停：不撮合
    book.adjust(conn, date=D2, unpause="MSFT", note="恢復")
    assert [f["order_id"] for f in broker.match_orders(conn, D3, {"MSFT": _bar(50, 51, 49)}, 0, 0)] == [oid]


def test_cli_ledger_pause_all_and_unpause_all(state_dir, conn):
    assert _cli(state_dir, "ledger", "pause", "--all", "--note", "停") == 0
    assert "XYZ" in book.paused(conn)
    assert _cli(state_dir, "ledger", "pause", "--note", "缺代號") == 2
    assert _cli(state_dir, "ledger", "adjust", "--unpause-all", "--note", "恢復") == 0
    assert "XYZ" not in book.paused(conn)


# ---------------------------------------------------------------- M8-D5-10 ledger show 估值

def test_ledger_show_uses_last_nav_prices(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _hold(conn, "AAPL", 10, 100.0)
    conn.execute("INSERT INTO cash_ledger(date, amount, kind, ref, note) VALUES (?,?,?,?,?)",
                 (D1, -1000.0, "buy", "x", "x"))
    conn.commit()
    s0 = book.show(conn)
    assert s0["valuation"] == "cost" and s0["valuation_date"] is None
    book.record_nav(conn, D2, {"AAPL": 130.0}, None)
    s = book.show(conn)
    assert s["valuation"] == "nav" and s["valuation_date"] == D2
    assert s["equity"] == pytest.approx(M.summary(conn)["nav"]) == pytest.approx(10_300)


# ---------------------------------------------------------------- M7-D4-04 公司行動人工結案

def _spin_off(conn):
    _hold(conn, "SPN", 100, 50.0)
    ev = {"event_id": "SPN:alpaca:2026-09-10:1", "ticker": "SPN", "source": "alpaca", "date": "2026-09-10",
          "alpaca": {"type": "spin_off"}}
    res = {"ticker": "SPN", "action_type": "分拆", "effective_date": "2026-09-10", "new_ticker": "NEWCO",
           "ratio_new_per_old": 0.5, "cash_per_share": None, "fractional_handling": "不適用", "needs_human": True}
    ca.reject(conn, res, ev, ca.verify(res, ev, ca.position_of(conn, "SPN")))
    return conn.execute("SELECT ca_id FROM corporate_actions WHERE ticker='SPN'").fetchone()[0]


def test_resolve_corp_action_closes_pending_human(conn):
    cid = _spin_off(conn)
    detail = json.loads(conn.execute("SELECT detail FROM anomalies WHERE ticker='SPN'").fetchone()[0])
    assert f"--resolve-corp-action {cid}" in detail["how_to_resolve"]
    r = book.adjust(conn, date="2026-09-12", ticker="SPN", set_avg_cost=40.0, note="分拆母股成本下修", unpause="SPN")
    assert r["warnings"] and str(cid) in r["warnings"][0]          # 未結案：只警示
    assert [c["ca_id"] for c in M.anomalies(conn)["pending_corp_actions"]] == [cid]
    r = book.adjust(conn, date="2026-09-12", ticker="NEWCO", set_qty=50, set_avg_cost=20.0,
                    first_buy_date="2025-01-02", resolve_corp_action=cid, note="分拆新股入帳")
    assert r["resolved_corp_action"] == cid and "warnings" not in r
    st, data = conn.execute("SELECT status, data FROM corporate_actions WHERE ca_id=?", (cid,)).fetchone()
    assert st == "applied" and json.loads(data)["manual"]["note"] == "分拆新股入帳"
    assert M.anomalies(conn)["pending_corp_actions"] == []
    with pytest.raises(LedgerError):
        book.adjust(conn, date="2026-09-12", resolve_corp_action=cid, note="重複")
    with pytest.raises(LedgerError):
        book.adjust(conn, date="2026-09-12", resolve_corp_action=999, note="不存在")


def test_cli_resolve_corp_action(state_dir, conn):
    cid = _spin_off(conn)
    assert _cli(state_dir, "ledger", "adjust", "--unpause", "SPN", "--resolve-corp-action", str(cid),
                "--note", "人工完成") == 0
    assert conn.execute("SELECT status FROM corporate_actions").fetchone()[0] == "applied"
    assert book.paused(conn) == set() and "SPN" not in book.paused(conn)


# ---------------------------------------------------------------- M8-D4-02 作廢決策

def _void_world(conn):
    _dec(conn, "XYZ-20261015-1", "XYZ", D2, {"action": "BUY"}, created="2026-10-16T01:00:00Z", run="analyze-2026-10-15")
    _dec(conn, "XYZ-20261015-2", "XYZ", D2, {"action": "REJECT"}, created="2026-10-17T01:00:00Z",
         run="analyze-2026-10-15-r3")
    db.insert(conn, "signals", {"decision_id": "XYZ-20261015-1", "ticker": "XYZ", "signal": "BUY",
                                "data": {"as_of": D2, "auto_buy_cap": 99}, "created_at": NOW})


def test_cli_decisions_void(state_dir, conn):
    _void_world(conn)
    book.ensure_initial_deposit(conn, 10_000, D0)
    oid = broker.place_order(conn, "XYZ", "BUY", 1, 10.0, "XYZ-20261015-1", D2)
    assert _cli(state_dir, "decisions", "void", "XYZ-20261015-1", "--note", "資料包有錯",
                "--superseded-by", "XYZ-20261015-2") == 0
    assert [a["decision_id"] for a in M.analyses(conn)] == ["XYZ-20261015-2"]
    items = Z.load_decisions(conn, "2026-10-17", since="2026-10-16", late_after="2026-10-15T22:00:00Z")
    assert [x["decision_id"] for x in items if x["action"] in ("BUY", "ADD")] == []
    assert Z.latest_card(conn, "XYZ") is None
    assert conn.execute("SELECT status FROM orders WHERE order_id=?", (oid,)).fetchone()[0] == "cancelled"
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id='XYZ-20261015-1'").fetchone()[0])
    assert log["voided_note"] == "資料包有錯" and log["superseded_by"] == "XYZ-20261015-2" and log["voided_at"]
    assert _cli(state_dir, "decisions", "void", "XYZ-20261015-1", "--note", "again") == 1
    assert _cli(state_dir, "decisions", "void", "NOPE", "--note", "x") == 1


def test_voided_decisions_skipped_in_validation(conn, paths):
    _void_world(conn)
    conn.execute("UPDATE decisions SET data=json_set(data, '$.voided_at', 'x') WHERE decision_id='XYZ-20261015-1'")
    conn.commit()
    assert [x["_id"] for x in V._logs(conn, D0, "2026-10-31")] == ["XYZ-20261015-2"]


# ---------------------------------------------------------------- M8-D4-06 registry retire

def test_cli_registry_retire(state_dir, conn):
    _reg(conn, "QRVO")
    _reg(conn, "HELD", "HOLDING")
    _hold(conn, "HELD", 1, 1.0)
    assert _cli(state_dir, "registry", "retire", "QRVO", "--note", "下市") == 0
    st, data = conn.execute("SELECT status, data FROM registry WHERE ticker='QRVO'").fetchone()
    assert st == "RETIRED" and json.loads(data)["retired"]["status_before"] == "WATCH"
    assert "QRVO" not in [e["ticker"] for e in export_registry(conn, "watch")]
    assert "QRVO" not in {r[0] for r in conn.execute("SELECT ticker FROM registry WHERE status = 'WATCH'")}
    assert _cli(state_dir, "registry", "retire", "HELD", "--note", "x") == 1     # 有持股
    assert _cli(state_dir, "registry", "retire", "QRVO", "--note", "x") == 1     # 已退場


# ---------------------------------------------------------------- M8-D5-03 jobs list

def test_cli_jobs_list_shows_paused_budget_and_skipped(state_dir, conn, capsys):
    for rid, st in (("analyze-2026-10-15", "paused_budget"), ("daily-2026-10-15", "done")):
        db.insert(conn, "runs", {"run_id": rid, "kind": "analyze", "asof": D2, "status": st})
    for jid, jt, st, err in (("j1", "FULL_A", "done", None), ("j2", "FULL_B", "skipped", "依賴失敗 " + "x" * 300),
                             ("j3", "DOSSIER", "pending", None)):
        db.insert(conn, "jobs", {"job_id": jid, "run_id": "analyze-2026-10-15", "job_type": jt, "ticker": "KO",
                                 "asof": D2, "status": st, "error": err, "created_at": NOW})
    assert _cli(state_dir, "jobs", "list") == 0
    out = capsys.readouterr().out
    assert "analyze-2026-10-15" in out and "paused_budget" in out and "daily-2026-10-15" not in out
    assert "j2  KO  FULL_B  skipped" in out and "j3" in out and "j1  " not in out
    assert max(len(line) for line in out.splitlines() if "j2" in line) < 200
    assert _cli(state_dir, "jobs", "list", "--all") == 0 and "daily-2026-10-15" in capsys.readouterr().out
    args, _ = cli.build_parser().parse_known_args(["jobs", "list"])
    assert cli._writes_state(args) is False


def test_paused_budget_hint(capsys):
    cli._awaiting_hint({"status": "paused_budget"}, "analyze-2026-10-15")
    assert "--run-id analyze-2026-10-15" in capsys.readouterr().out


# ---------------------------------------------------------------- M8-D4-01 db backup

def test_db_backup_integrity_and_retention(paths, conn):
    book.ensure_initial_deposit(conn, 1_000, D0)
    bdir = paths.state / "backups"
    bdir.mkdir()
    old = [bdir / f"trader-202609{d:02d}-120000.db" for d in range(1, 21)]   # 9 月 20 份
    for f in old:
        f.write_bytes(b"")
    dest = db.backup(paths.db, note="pre split/check")
    assert dest.name.endswith("-pre_split_check.db")
    c = sqlite3.connect(dest)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT amount FROM cash_ledger").fetchone()[0] == 1_000
    c.close()
    left = sorted(p.name for p in bdir.glob("trader-*.db"))
    assert len(left) == 15                                # 最近 14 份（含新的）＋ 9 月最早一份
    assert old[0].name in left and old[1].name not in left


def test_cli_db_backup(state_dir, paths, conn):
    assert _cli(state_dir, "db", "backup", "--note", "pre") == 0
    assert len(list((paths.state / "backups").glob("trader-*-pre.db"))) == 1


def test_scheduler_daily_done_writes_backup(paths, conn):
    sch = S.Scheduler.__new__(S.Scheduler)
    sch.paths, logs = paths, []
    sch.log = logs.append
    st: dict = {}
    sch._done(conn, st, f"daily:{D2}", ["daily", "--date", D2])
    assert len(list((paths.state / "backups").glob("trader-*.db"))) == 1
    assert any(m.startswith("[BACKUP]") for m in logs)


# ---------------------------------------------------------------- D12 人工下單

def test_cli_trade_buy_writes_manual_log_and_order(state_dir, conn, paths):
    book.ensure_initial_deposit(conn, 10_000, D0)
    db.insert(conn, "anomalies", {"ticker": "NEW", "kind": "switch_candidate", "status": "open",
                                  "pause_trading": 0, "created_at": NOW})
    aid = conn.execute("SELECT anomaly_id FROM anomalies").fetchone()[0]
    assert "trader trade buy --ticker NEW" in M.anomalies(conn)["open"][0]["hint"]
    assert _cli(state_dir, "trade", "buy", "--ticker", "new", "--qty", "10", "--limit", "20",
                "--anomaly", str(aid), "--note", "換股", "--date", D1) == 0
    did, data = conn.execute("SELECT decision_id, data FROM decisions").fetchone()
    log = json.loads(data)
    assert did == "NEW-MANUAL-20261014-1" and log["review_type"] == "MANUAL" and log["anomaly_id"] == aid
    o = conn.execute("SELECT * FROM orders").fetchone()
    assert (o["ticker"], o["side"], o["qty"], o["limit_price"], o["decision_id"], o["order_id"]) == \
        ("NEW", "BUY", 10, 20.0, did, log["order_id"])
    # daily 不把 MANUAL 決策再當買進決策；也不因沒有上限而撤單
    assert Z.load_decisions(conn, D2, since=D0) == []
    assert Z.revalidate_open_buys(conn, D2) == []
    assert broker.recheck_pending(conn, D2, {}, 0) == []
    fills = broker.match_orders(conn, D2, {"NEW": _bar(19, 20, 18, 19)}, 0, 0)
    assert len(fills) == 1
    t = M.trades(conn)
    assert t["orders"][0]["manual"] and t["fills"][0]["manual"]
    rep = V.build(conn, paths, D0, D3)
    assert [x["decision_id"] for x in rep["compliance"]["manual_trades"]] == [did]
    assert rep["compliance"]["bad_buys"] == []
    assert "人工下單（trader trade）：1 筆" in V.render(rep)


def test_cli_trade_sell_checks_holding_and_limit_sell_not_rechecked(state_dir, conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _hold(conn, "KO", 10, 50.0)
    assert _cli(state_dir, "trade", "sell", "--ticker", "KO", "--qty", "11", "--note", "x") == 1
    assert _cli(state_dir, "trade", "sell", "--ticker", "KO", "--qty", "4", "--limit", "60", "--note", "減碼",
                "--date", D1) == 0
    log = json.loads(conn.execute("SELECT data FROM decisions").fetchone()[0])
    assert log["action"] == "TRIM"
    assert broker.recheck_pending(conn, D2, {"KO": 55.0}, 0) == []      # 人工限價賣單不做觸發線重核
    assert broker.match_orders(conn, D2, {"KO": _bar(58, 61, 57)}, 0, 0)[0]["qty"] == 4
    with pytest.raises(SystemExit):   # 買進限價必填
        _cli(state_dir, "trade", "buy", "--ticker", "KO", "--qty", "1", "--note", "缺限價")


def test_trade_buy_rejected_when_paused(state_dir, conn):
    book.pause(conn, None, date=D1, note="停")
    assert _cli(state_dir, "trade", "buy", "--ticker", "AAA", "--qty", "1", "--limit", "5", "--note", "x") == 1
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0   # 下單失敗：日誌一起回滾


@pytest.mark.parametrize("argv,writes", [
    (["ledger", "cancel", "X", "--note", "n"], True), (["ledger", "pause", "--all", "--note", "n"], True),
    (["ledger", "show"], False), (["trade", "buy", "--ticker", "A", "--qty", "1", "--limit", "1", "--note", "n"], True),
    (["decisions", "void", "X", "--note", "n"], True), (["registry", "retire", "X", "--note", "n"], True),
    (["db", "backup"], False), (["jobs", "abandon", "--run-id", "r"], True)])
def test_writes_state_for_new_commands(argv, writes):
    args, _ = cli.build_parser().parse_known_args(argv)
    assert cli._writes_state(args) is writes


# ---------------------------------------------------------------- P24 交接

def test_expired_valuation_trim_marks_last_trim_unfilled(conn):
    book.ensure_initial_deposit(conn, 10_000, D0)
    _hold(conn, "KO", 100, 50.0)
    lt = {"date": D1, "reason_code": "VALUATION", "line": 80.0, "fraction": 0.25, "shares": 25, "peak_shares": 100}
    _reg(conn, "KO", "HOLDING", {"last_trim": lt})
    _dec(conn, "KO-H-1", "KO", D1, {"action": "TRIM", "review_type": "HOLD_REVIEW", "sell_category": "估值"})
    broker.place_order(conn, "KO", "SELL", 25, 80.0, "KO-H-1", D1)
    assert broker.match_orders(conn, D2, {"KO": _bar(75, 79, 74)}, 0, 0) == []
    got = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])["last_trim"]
    assert got["shares"] == 0 and "未達觸發線" in got["unfilled"] and got["peak_shares"] == 100


def test_split_scales_last_trim(conn, paths):
    _hold(conn, "KO", 10, 100.0)
    _reg(conn, "KO", "HOLDING", {"last_trim": {"date": D1, "line": 120.0, "shares": 2, "peak_shares": 12}})
    res = {"ticker": "KO", "action_type": "拆股", "effective_date": D3, "pay_date": None, "ratio_new_per_old": 4,
           "cash_per_share": None, "fractional_handling": "不適用", "new_ticker": None, "needs_human": False}
    ev = {"event_id": "KO:alpaca:x", "ticker": "KO", "source": "alpaca", "date": D3}
    assert ca.apply(conn, paths, res, ev, D3)["status"] == "applied"
    lt = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])["last_trim"]
    assert (lt["line"], lt["shares"], lt["peak_shares"]) == (30.0, 8, 48)
