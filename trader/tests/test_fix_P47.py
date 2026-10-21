"""P47：持有側資料與 P-30（M2_D4_01〔＝M2-D10-01〕、M2_D4_06）。
- M2_D4_01：P-30 單事件上限由框架算（sizing.single_event_cap、deep._p30_writeback），取不到 → null 並註記「P-30 未檢查」。
- M2_D4_06：broker 買進成交記 peak_qty／entry_norm_pe（拆股時 corp_actions 換算峰值）；HOLD_REVIEW 輸入帶峰值與
  P-45 倍數警報中能機械算的項目；TRIM 比例（Q3c ≤1/4、荒謬級 ≥1/2）由 D03 在 writeback 夾到範圍內（P24 已做，這裡只回歸）。
"""
from __future__ import annotations

import json

import pytest

import test_fix_P24 as P24
import test_fix_P15 as P15
from test_engine import RUN, full_responses, make_env
from trader import db
from trader.analysis import writeback as W
from trader.jobs import deep
from trader.jobs.hold_jobs import _position_text, multiple_alarms
from trader.ledger import book, broker, corp_actions
from trader.paths import get_paths
from trader.portfolio import sizing as S

env = P24.env


def _closes(rets: dict[str, float]) -> list[tuple[str, float]]:
    """連續交易日 2024-01-01 起；rets：{日期: 該日報酬}，其餘 0。"""
    from datetime import date, timedelta
    out, px, d = [], 100.0, date(2024, 1, 1)
    for _ in range(800):
        ds = d.isoformat()
        px *= 1 + rets.get(ds, 0.0)
        out.append((ds, px))
        d += timedelta(days=1)
    return out


EIGHT = ["2024-02-01", "2024-05-01", "2024-08-01", "2024-11-01",
         "2025-02-01", "2025-05-01", "2025-08-01", "2025-11-01"]


# ---------------------------------------------------------------- M2_D4_01：P-30

def test_single_event_cap_eight_reports_worst_next_day_drop_20pct():
    cap, why = S.single_event_cap(EIGHT, _closes({"2025-05-02": -0.20, "2024-08-02": -0.05}))
    assert cap == pytest.approx(10.0) and "2025-05-01" in why     # 2% ÷ 20%


def test_single_event_cap_same_day_drop_counts_and_only_last_eight():
    # 盤前公布：申報日當天跌 -25% 也算（取當日與次日較差者）
    assert S.single_event_cap(EIGHT, _closes({"2024-11-01": -0.25}))[0] == pytest.approx(8.0)
    # 第 9 次（最早的）不在近 8 次內：其前補一根日線，否則它落在第一根 K 線本來就被略過、抓不到 [-8:] 被拿掉（X43）
    old = ["2023-01-02"] + EIGHT
    c = _closes({})
    c = [("2022-12-30", 100.0), ("2023-01-02", 100.0), ("2023-01-03", 50.0)] + c
    assert S.single_event_cap(old, c)[0] == 100.0                 # 近 8 次都沒跌 → 不綁定


def test_single_event_cap_without_data_is_none():
    cap, why = S.single_event_cap(EIGHT, [])
    assert cap is None and "P-30 未檢查" in why


def test_p30_writeback_overrides_model_value(monkeypatch):
    monkeypatch.setattr(deep, "_p30_data", lambda ctx, t, a: (EIGHT, _closes({"2025-05-02": -0.20})))
    card, notes = {"max_weight_pct": {"tail": None, "single_event": 15}}, []
    deep._p30_writeback(None, type("J", (), {"ticker": "KO", "asof": "2026-10-16"})(), card, notes)
    assert card["max_weight_pct"] == {"tail": None, "single_event": 10.0}
    assert "以框架值覆寫" in notes[0]
    assert S.target_weight({**P15._card(target_weight_pct=15, certainty_grade="A"), **card}, None)[0] == \
        pytest.approx(0.10)


def test_p30_writeback_failure_writes_null_and_note(monkeypatch):
    def boom(ctx, t, a):
        raise RuntimeError("缺 SEC 或價格客戶端")
    monkeypatch.setattr(deep, "_p30_data", boom)
    card, notes = {"max_weight_pct": {"single_event": 5}}, []
    deep._p30_writeback(None, type("J", (), {"ticker": "KO", "asof": "2026-10-16"})(), card, notes)
    assert card["max_weight_pct"]["single_event"] is None and "P-30 未檢查" in notes[0]


def test_plan_buys_notes_p30_unchecked(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    book.ensure_initial_deposit(c, 100000.0, "2026-10-01")
    p = S.plan_buys(c, P15.ASOF, [P15._item("AAA-1", "AAA")], {"AAA": 50.0}, {"AAA": P15.BIG_ADV}, 10)[0]
    assert p["qty"] > 0 and any("P-30 未檢查" in n for n in p["notes"])
    card = P15._card(max_weight_pct={"host": 15, "single_event": 12.0})
    p = S.plan_buys(c, P15.ASOF, [P15._item("BBB-1", "BBB", card=card)], {"BBB": 50.0}, {"BBB": P15.BIG_ADV}, 10)[0]
    assert not any("P-30" in n for n in p["notes"])
    c.close()


def test_full_b_flow_writes_framework_single_event(tmp_path, monkeypatch):
    monkeypatch.setattr(deep, "_p30_data", lambda ctx, t, a: (EIGHT, _closes({"2025-05-02": -0.40})))
    e = make_env(tmp_path, full_responses())
    e.add(e.job("KO", "DOSSIER", 1))
    e.price_file("KO")
    assert e.engine.run(RUN)["status"] == "done"
    card = json.loads(e.conn.execute("SELECT data FROM signals").fetchone()[0])
    assert card["max_weight_pct"]["single_event"] == pytest.approx(5.0)
    log = json.loads(e.conn.execute("SELECT data FROM decisions").fetchone()[0])
    assert any("P-30" in n and "5.0%" in n for n in log.get("framework_notes") or [])


def test_full_b_flow_without_clients_marks_unchecked(tmp_path):
    e = make_env(tmp_path, full_responses())   # 測試設定沒有金鑰：價格客戶端為 None
    e.add(e.job("KO", "DOSSIER", 1))
    e.price_file("KO")
    assert e.engine.run(RUN)["status"] == "done"
    card = json.loads(e.conn.execute("SELECT data FROM signals").fetchone()[0])
    assert card["max_weight_pct"]["single_event"] is None
    log = json.loads(e.conn.execute("SELECT data FROM decisions").fetchone()[0])
    assert any("P-30 未檢查" in n for n in log.get("framework_notes") or [])


# ---------------------------------------------------------------- M2_D4_06：peak_qty／entry_norm_pe

@pytest.fixture
def lconn(state_dir, tmp_path):
    paths = get_paths().ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, 1_000_000, "2026-10-01")
    cin = tmp_path / "KO_cap.json"
    cin.write_text(json.dumps({"norm_eps": 2.5, "oe": 3.0}), encoding="utf-8")
    db.upsert(c, "registry", {"ticker": "KO", "status": "WATCH", "first_buy_date": None,
                              "data": {"grade": "B", "cap_input_file": str(cin)}, "updated_at": db.now_iso()},
              ["ticker"])
    yield c
    c.close()


def _bar(px):
    return {"open": px, "high": px, "low": px, "close": px}


def _trade(c, side, qty, px, created, date):
    broker.place_order(c, "KO", side, qty, px + 1 if side == "BUY" else None, None, created)
    return broker.match_orders(c, date, {"KO": _bar(px)}, 0.0, 0.0)


def _rd(c):
    return json.loads(c.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])


def test_broker_records_peak_qty_and_entry_norm_pe(lconn):
    _trade(lconn, "BUY", 100, 50.0, "2026-10-01", "2026-10-02")
    d = _rd(lconn)
    assert d["peak_qty"] == 100 and d["entry_norm_pe"] == pytest.approx(20.0)   # 50 ÷ 2.5
    _trade(lconn, "BUY", 50, 65.0, "2026-10-02", "2026-10-05")
    d = _rd(lconn)
    assert d["peak_qty"] == 150 and d["entry_norm_pe"] == pytest.approx(55.0 / 2.5)   # 均價 55
    _trade(lconn, "SELL", 60, 70.0, "2026-10-05", "2026-10-06")   # 系統減碼（如硬上限）不改峰值
    assert _rd(lconn)["peak_qty"] == 150
    _trade(lconn, "SELL", 90, 70.0, "2026-10-06", "2026-10-07")   # 賣光
    _trade(lconn, "BUY", 10, 40.0, "2026-10-07", "2026-10-08")    # 新持有期重新起算
    d = _rd(lconn)
    assert d["peak_qty"] == 10 and d["entry_norm_pe"] == pytest.approx(16.0)


def test_split_scales_peak_qty_keeps_entry_pe(lconn):
    _trade(lconn, "BUY", 100, 50.0, "2026-10-01", "2026-10-02")
    corp_actions._scale_registry(lconn, "KO", 2.0)
    d = _rd(lconn)
    assert d["peak_qty"] == 200 and d["entry_norm_pe"] == pytest.approx(20.0)


def test_hold_review_position_text_shows_peak(lconn):
    _trade(lconn, "BUY", 100, 50.0, "2026-10-01", "2026-10-02")
    assert "峰值股數 100" in _position_text(lconn, "KO", "2026-10-16")[0]


def test_multiple_alarms_mechanical_items():
    txt = multiple_alarms({"largecap_pe_median": 20.0, "long_rate": 0.05, "g10": 0.06, "g5": 0.08},
                          {"norm_pe": 30.0, "r_d": 0.10, "g_impl": 0.09}, {"entry_norm_pe": 18.0})
    assert "正常化 P/E 30.00 ≥25：是" in txt
    assert "建倉時 P/E 18.00，現值 ≥ 1.5 倍：是" in txt
    assert "≥ 大型股中位 1.2 倍：是" in txt and "本系統不提供自身 5 年 P/E 中位" in txt
    assert "盈餘殖利率 < 長債一半：否" in txt          # 3.33% vs 2.5%
    assert "盈餘殖利率 < r_d：是" in txt and "g_impl > 已證明成長（g10、g5 較高者）：是" in txt
    assert "建倉時 P/E 缺，現值 ≥ 1.5 倍：缺" in multiple_alarms({}, {"norm_pe": 30.0}, {})


# ---------------------------------------------------------------- M2_D4_06：TRIM 比例（D03 回寫夾範圍；回歸）

def test_q3c_trim_04_clamped_to_quarter(env):
    paths, conn = env
    P24._seed(conn, qty=100)
    P24.seed_p08(conn)   # A3-M3D-02
    did = W.write_hold_review_result(conn, paths, "KO", P24.ASOF, "r", P24._hr(0.4, P24.Q3C), price=80.0, iv_base=60.0)
    assert P24._log(conn, did)["trim_fraction"] == pytest.approx(0.25)


def test_absurd_trim_03_raised_to_half(env):
    paths, conn = env
    P24._seed(conn, qty=100)
    did = W.write_hold_review_result(conn, paths, "KO", P24.ASOF, "r", P24._hr(0.3), price=300.0, iv_base=60.0)
    assert P24._log(conn, did)["trim_fraction"] == pytest.approx(0.5)
