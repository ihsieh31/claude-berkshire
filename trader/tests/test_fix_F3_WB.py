"""F3-WB 回歸測試：V3-2 G14-writeback-misc（A3-M5E-06、A3-M6B-05、A3-M6C-03、A3-M6C-07、A3-M6E-02）、
V3-4 G5-writeback-origin（A3-M8B-07）、A3-M7A-02 writeback 端、A3-M5C-01、review_due NO_PRICE_UPGRADE 加 FRAMEWORK_HOLD。全部離線。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader import db
from trader.analysis import writeback as W
from trader.analysis.writeback import write_fallback_result, write_full_result, write_stop_result
from trader.jobs import deep as D
from trader.jobs import review_jobs as RJ
from trader.paths import get_paths
from trader.portfolio import correlation as corr

ROOT = Path(__file__).resolve().parents[2]
ASOF = "2026-10-16"
NOW = "2026-10-16T08:00:00Z"


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield conn, paths
    conn.close()


def _reg_set(conn, t, status, data, first_buy=None):
    row = {"ticker": t, "status": status, "data": {"ticker": t, "status": status, **data}, "updated_at": NOW}
    if first_buy:
        row["first_buy_date"] = first_buy
    db.upsert(conn, "registry", row, key=["ticker"])


def _reg(conn, t):
    return json.loads(conn.execute("SELECT data FROM registry WHERE ticker=?", (t,)).fetchone()["data"])


def _log(conn, did):
    return json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])


def _decision(conn, t, d, did, data, signal="WATCH", job_id=None):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": d, "run_id": "r0",
                                  "job_id": job_id, "signal": signal, "reason_code": "X",
                                  "data": {"decision_date": d, **data}, "created_at": NOW})


def _tdir(tmp_path: Path, ticker="AAPL", signal="WATCH", reason="PASS_ALL", cap_input=False, name="td") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    card = {"schema": "berkshire-deep-merged/signal-v1", "ticker": ticker, "as_of": ASOF, "signal": signal,
            "reason_code": reason, "conclusion": {"BUY": "買", "WATCH": "觀察", "NO_ACTION": "不買"}[signal],
            "auto_buy_cap": 150.0, "certainty_grade": "B", "value_per_share": {"conservative": 120.0, "base": 160.0},
            "red_light": False, "decision_log_id": ""}
    entry = {"ticker": ticker, "cik": "0000320193", "status": signal, "grade": "B", "auto_buy_cap": 150.0}
    log = {"decision_id": "", "decision_date": ASOF, "ticker": ticker, "action": signal,
           "screen_decision": "PASS", "gate3_pass": True, "reason_one_liner": "測試"}
    for n, v in (("signal_card.json", card), ("registry_entry.json", entry), ("decision_log.json", log)):
        (d / n).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
    if cap_input:
        (d / "cap_input.json").write_text(json.dumps({"price": 100.0, "long_rate": 0.047}), encoding="utf-8")
    (d / "thesis.md").write_text("論點內容", encoding="utf-8")
    return d


# ================================================================ A3-M5E-06
def test_hold_review_post_replay_no_dup_anomaly(env, tmp_path):
    """同一 HOLD_REVIEW 工作已回寫 → 重跑 post 不再記異常、不覆寫 td/hold_review.json，回 []。"""
    from trader.jobs.hold_jobs import HoldReviewHandler
    conn, paths = env
    _reg_set(conn, "KO", "HOLDING", {})
    _decision(conn, "KO", ASOF, "KO-20261016-1", {"review_type": "HOLD_REVIEW", "action": "HOLD",
                                                  "source_job_id": "J-HR-1"}, signal="HOLD")
    job = SimpleNamespace(job_id="J-HR-1", ticker="KO", asof=ASOF, run_id="r1", params={}, job_type="HOLD_REVIEW")
    ctx = SimpleNamespace(conn=conn, paths=paths)
    n_anom = conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
    n_dec = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    out = HoldReviewHandler().post(job, {"action": "SELL_ALL", "sell_category": "論點", "path": []}, ctx)
    assert out == []
    assert conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == n_anom
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == n_dec
    assert not list(paths.state.rglob("hold_review.json"))


# ================================================================ A3-M6B-05
@pytest.mark.parametrize("holding", [True, False])
def test_fallback_flag_written_atomically(env, holding):
    """write_fallback_result 自己就寫 "fallback": true（不靠 engine 第二筆 UPDATE）。"""
    conn, paths = env
    did = write_fallback_result(conn, paths, "KO", ASOF, "r1", "J-1", holding, "TRIAGE failed：x")
    assert _log(conn, did)["fallback"] is True


def test_engine_finalize_no_second_update():
    """engine.finalize_run 不再另以 UPDATE 補寫 fallback 旗標。"""
    import inspect
    from trader.jobs import engine as E
    src = inspect.getsource(E.Engine.finalize_run) if hasattr(E, "Engine") else inspect.getsource(E)
    assert '"fallback": True}' not in src


# ================================================================ A3-M6C-03
def test_full_saw_buyable_ignores_cap_not_applied(env):
    conn, _ = env
    db.insert(conn, "decisions", {"decision_id": "KO-20261001-1", "ticker": "KO", "decision_date": "2026-10-01",
                                  "run_id": "r", "job_id": None, "signal": "WATCH", "reason_code": "PASS_ALL",
                                  "data": {"action": "WATCH", "price": 70.0, "auto_buy_cap": 80.0,
                                           "cap_not_applied": True}, "created_at": NOW})
    db.insert(conn, "signals", {"decision_id": "KO-20261001-1", "ticker": "KO", "signal": "WATCH",
                                "data": {"signal": "WATCH"}, "created_at": NOW})
    assert RJ._full_already_saw_buyable(conn, "KO", ASOF) is None


def test_full_saw_buyable_ignores_stale_sold(env, tmp_path):
    """賣光日不早於資料日（stale_sold）的完整分析：日誌標 cap_not_applied → 不當『已見可買仍非 BUY』節流。"""
    conn, paths = env
    _reg_set(conn, "AAPL", "WATCH", {"sold_out_at": "2026-10-20"})
    did = write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, signal="BUY"))
    assert _log(conn, did).get("cap_not_applied") is True


def test_full_result_normal_has_no_cap_not_applied(env, tmp_path):
    conn, paths = env
    did = write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path))
    assert "cap_not_applied" not in _log(conn, did)


# ================================================================ A3-M6C-07
def test_back_below_line_after_reverse_split(env):
    """1 合 10（r=0.1）後 last_trim.line 已放大 10 倍；合股前的決策價 40（換算 400）不得當成『回到線 300 下』。"""
    conn, _ = env
    db.insert(conn, "corporate_actions", {"ticker": "KO", "action_type": "合股", "effective_date": "2026-09-20",
                                          "ratio_new_per_old": 0.1, "status": "applied", "data": "{}",
                                          "created_at": NOW})
    _decision(conn, "KO", "2026-09-10", "KO-20260910-1", {"review_type": "PRICE", "price": 40.0})
    _decision(conn, "KO", "2026-10-01", "KO-20261001-1", {"review_type": "PRICE", "price": 310.0})
    assert W._back_below_line(conn, "KO", "2026-09-01", 300.0, ASOF) is False
    _decision(conn, "KO", "2026-10-05", "KO-20261005-1", {"review_type": "PRICE", "price": 290.0})
    assert W._back_below_line(conn, "KO", "2026-09-01", 300.0, ASOF) is True   # 合股後真的跌回線下仍算


def test_back_below_line_pre_split_price_converted_below(env):
    """合股前價 25（換算 250 < 300）仍算回到線下。"""
    conn, _ = env
    db.insert(conn, "corporate_actions", {"ticker": "KO", "action_type": "合股", "effective_date": "2026-09-20",
                                          "ratio_new_per_old": 0.1, "status": "applied", "data": "{}",
                                          "created_at": NOW})
    _decision(conn, "KO", "2026-09-10", "KO-20260910-1", {"review_type": "PRICE", "price": 25.0})
    assert W._back_below_line(conn, "KO", "2026-09-01", 300.0, ASOF) is True


# ================================================================ A3-M6E-02
def test_full_result_replay_finishes_post_commit(env, tmp_path, monkeypatch):
    """提交後、os.replace 前被中斷（SIGTERM）→ jobs retry 重跑：cap_inputs 換上、.tmp 清掉、同群判定補做。"""
    conn, paths = env
    td = _tdir(tmp_path, cap_input=True)
    real = W.os.replace

    def boom(src, dst):
        raise KeyboardInterrupt("SIGTERM")
    monkeypatch.setattr(W.os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        write_full_result(conn, paths, "AAPL", ASOF, "r1", td, job_id="J-FULL-1")
    monkeypatch.setattr(W.os, "replace", real)
    dst = paths.state / "cap_inputs" / "AAPL.json"
    tmp = dst.with_name("AAPL.json.tmp")
    assert tmp.is_file() and not dst.exists()
    did = conn.execute("SELECT decision_id FROM decisions WHERE ticker='AAPL'").fetchone()[0]
    calls = []
    monkeypatch.setattr(W, "_record_links_safely", lambda c, p, t, a: calls.append((t, a)))
    assert write_full_result(conn, paths, "AAPL", ASOF, "r1", td, job_id="J-FULL-1") == did
    assert dst.is_file() and not tmp.exists()
    assert json.loads(dst.read_text())["price"] == 100.0
    assert calls == [("AAPL", ASOF)]
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE ticker='AAPL'").fetchone()[0] == 1


def test_full_result_replay_skips_tmp_when_newer_full(env, tmp_path):
    """重跑時已有較新的完整分析 → 不把舊 .tmp 換上。"""
    conn, paths = env
    did = write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, cap_input=True), job_id="J-FULL-1")
    dst = paths.state / "cap_inputs" / "AAPL.json"
    tmp = dst.with_name("AAPL.json.tmp")
    tmp.write_text(json.dumps({"price": 1.0}))
    _decision(conn, "AAPL", "2026-10-17", "AAPL-20261017-1", {"action": "WATCH"})
    assert write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, cap_input=True, name="td2"),
                             job_id="J-FULL-1") == did
    assert json.loads(dst.read_text())["price"] == 100.0


def test_hold_review_replay_records_links(env, monkeypatch):
    conn, paths = env
    _reg_set(conn, "KO", "HOLDING", {})
    _decision(conn, "KO", ASOF, "KO-20261016-1", {"review_type": "HOLD_REVIEW", "action": "HOLD",
                                                  "source_job_id": "J-HR-1"}, signal="HOLD")
    calls = []
    monkeypatch.setattr(W, "_record_links_safely", lambda c, p, t, a: calls.append(t))
    assert W.write_hold_review_result(conn, paths, "KO", ASOF, "r1", {"action": "HOLD"},
                                      job_id="J-HR-1") == "KO-20261016-1"
    assert calls == ["KO"]


# ================================================================ A3-M8B-07
def test_stop_holding_drops_origin(env):
    conn, paths = env
    _reg_set(conn, "KO", "HOLDING", {"origin": "hark_b", "grade": "B"})
    write_stop_result(conn, paths, "KO", ASOF, "r1", {"stopped": True, "stopped_at_gate": "G3",
                                                      "reason_code": "TOO_HARD"})
    d = _reg(conn, "KO")
    assert "origin" not in d and d["grade"] == "B"


def test_stop_holding_non_analytic_keeps_origin(env):
    conn, paths = env
    _reg_set(conn, "KO", "HOLDING", {"origin": "hark_b"})
    write_stop_result(conn, paths, "KO", ASOF, "r1", {"stopped": True, "stopped_at_gate": "STEP0",
                                                      "reason_code": "MISSING_DATA"}, non_analytic=True)
    assert _reg(conn, "KO").get("origin") == "hark_b"


# ================================================================ A3-M7A-02（writeback 端）
@pytest.mark.parametrize("mark", [{"unfilled": "2026-10-01 下單被拒：KO 暫停交易中", "shares": 0},
                                  {"shares": 0}, {"unfilled": "未成交", "shares": 50}])
def test_absurd_rejected_trim_can_retrim(env, mark):
    """上次荒謬級減碼被拒（unfilled／shares＝0）→ 不算減過，下次荒謬級 HOLD_REVIEW 仍可 TRIM（不是『只減一次』HOLD）。"""
    conn, _ = env
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?)", ("KO", 100, 60.0, "2025-01-02", NOW))
    conn.commit()
    lt = {"date": "2026-10-01", "reason_code": "ABSURD", "line": 100.0, "fraction": 0.5, "peak_shares": 100, **mark}
    data = {"absurd_price_line": 100.0, "last_trim": lt}
    _reg_set(conn, "KO", "HOLDING", data, first_buy="2025-01-02")
    assert W._prior_trim(conn, "KO", {"data": data}) is None
    notes: list[str] = []
    act, frac, new_lt = W._vet_valuation_sell(conn, "KO", "2026-11-02",
                                              {"action": "TRIM", "trim_fraction": 0.5, "sell_category": "估值",
                                               "path": []}, {"data": data}, 120.0, 80.0, notes)
    assert act == "TRIM" and frac >= 0.5 and new_lt["reason_code"] == "ABSURD", notes
    assert not any("只減一次" in n for n in notes)


def test_filled_absurd_trim_still_blocks(env):
    conn, _ = env
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?)", ("KO", 50, 60.0, "2025-01-02", NOW))
    conn.commit()
    lt = {"date": "2026-10-01", "reason_code": "ABSURD", "line": 100.0, "fraction": 0.5, "shares": 50,
          "peak_shares": 100}
    data = {"absurd_price_line": 100.0, "last_trim": lt}
    _reg_set(conn, "KO", "HOLDING", data, first_buy="2025-01-02")
    act, _, _ = W._vet_valuation_sell(conn, "KO", "2026-11-02", {"action": "TRIM", "trim_fraction": 0.5,
                                                                "sell_category": "估值", "path": []},
                                      {"data": data}, 120.0, 80.0, [])
    assert act == "HOLD"


# ================================================================ A3-M5C-01
def _p32_tail(asset_class):
    imp = corr.impairment(corr.is_financial(asset_class))
    return round(corr.GROUP_LOSS_BUDGET / max(corr.TAIL_FLOOR, imp) * 100.0, 4)


@pytest.mark.parametrize("ac", ["非金融", "金融"])
def test_p29_decimal_tail_conservative(ac):
    card = {"max_weight_pct": {"tail": 40.0}}
    notes: list[str] = []
    D._p29_writeback(card, {"tail_worst_loss_pct": 0.6}, {"asset_class": ac}, notes)
    assert card["max_weight_pct"]["tail"] <= _p32_tail(ac)
    assert any("疑似把小數填成百分比" in n for n in notes)


def test_p29_percent_tail_unchanged():
    card = {"max_weight_pct": {"tail": 40.0}}
    D._p29_writeback(card, {"tail_worst_loss_pct": 60}, {"asset_class": "非金融"}, [])
    assert card["max_weight_pct"]["tail"] == round(10 / 60 * 100, 4)


# ================================================================ review_due FRAMEWORK_HOLD
def _review_due():
    sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
    try:
        import review_due
    finally:
        sys.path.pop(0)
    return review_due


def test_review_due_framework_hold_no_price_upgrade():
    import pandas as pd
    from datetime import date, timedelta
    rd = _review_due()
    assert "FRAMEWORK_HOLD" in rd.NO_PRICE_UPGRADE
    asof = "2026-10-15"
    d0 = date.fromisoformat(asof)
    vals = [82.0] * 119 + [79.5]
    px = pd.DataFrame({"date": [d0 - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))], "close": vals})
    base = dict(ticker="X", status="WATCH", last_review_date="2026-10-01", last_full_review_date="2026-06-01",
                review_price=82, t30_at_valuation=0.047, auto_buy_cap=80, sell_review_above=130,
                absurd_price_line=300, grade="B", last_reason_code="FRAMEWORK_HOLD")
    assert rd.check(base, [], px, asof, 0.047)[0] == "NONE"            # 跌破上限不排 PRICE
    assert rd.check(dict(base, review_price=100), [], px.assign(close=100.0), asof, 0.058)[0] == "NONE"   # 長債 ≥1pp
    assert rd.check(dict(base, last_reason_code="PASS_ALL"), [], px, asof, 0.047)[0] == "PRICE"   # 對照
