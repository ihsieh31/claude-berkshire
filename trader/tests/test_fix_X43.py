"""X43：P47 查核小項。
(1) multiple_alarms：cap_calc 解不出 g_impl（g_impl_gt_cap＝true，隱含成長 > solve_g 上界 min(0.5, r_d)）時，
    已證明成長在上界以下 → 「g_impl > 已證明成長」為「是」（原本顯示「缺」）；已證明成長超過上界則無從比較，仍「缺」。
(3) 人工調整（book.adjust）與對帳（book.reconcile_registry）開新持有期／加倉時，登錄表 peak_qty 與成交路徑一致：
    新持有期重設、加倉取 max、減少不降。
（(2) 是 test_fix_P47 的測試資料修正，不在此檔。）
"""
from __future__ import annotations

import json

import pytest

from trader import db
from trader.jobs.hold_jobs import multiple_alarms
from trader.ledger import book
from trader.paths import get_paths

G_ROW = "g_impl > 已證明成長（g10、g5 較高者）："


# ---------------------------------------------------------------- (1) g_impl_gt_cap → 是

def test_g_impl_gt_cap_alarm_is_yes():
    txt = multiple_alarms({"g10": 0.06, "g5": 0.04},
                          {"norm_pe": 40.0, "r_d": 0.09, "g_impl": None, "g_impl_gt_cap": True}, {})
    assert G_ROW + "是" in txt


def test_g_impl_gt_cap_but_history_above_bound_stays_missing():
    # 已證明成長 20% > 上界 r_d 9%：只知 g_impl > 9%，無法與 20% 比 → 仍標缺
    txt = multiple_alarms({"g10": 0.20}, {"norm_pe": 40.0, "r_d": 0.09, "g_impl": None, "g_impl_gt_cap": True}, {})
    assert G_ROW + "缺" in txt


def test_g_impl_none_without_flag_stays_missing():
    txt = multiple_alarms({"g10": 0.06}, {"norm_pe": 40.0, "r_d": 0.09, "g_impl": None, "g_impl_gt_cap": False}, {})
    assert G_ROW + "缺" in txt


# ---------------------------------------------------------------- (3) adjust／reconcile 的 peak_qty

@pytest.fixture
def lconn(state_dir, tmp_path):
    paths = get_paths().ensure()
    c = db.connect(paths.db)
    db.migrate(c)
    book.ensure_initial_deposit(c, 1_000_000, "2026-10-01")
    cin = tmp_path / "KO_cap.json"
    cin.write_text(json.dumps({"norm_eps": 2.5}), encoding="utf-8")
    # 前一持有期留下的峰值 500（賣光時不清，靠新持有期重設）
    db.upsert(c, "registry", {"ticker": "KO", "status": "WATCH", "first_buy_date": None,
                              "data": {"cap_input_file": str(cin), "peak_qty": 500, "entry_norm_pe": 99.0},
                              "updated_at": db.now_iso()}, ["ticker"])
    yield c
    c.close()


def _rd(c):
    return json.loads(c.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])


def test_adjust_new_period_resets_peak_then_max_then_no_decrease(lconn):
    book.adjust(lconn, date="2026-10-02", ticker="KO", set_qty=100, set_avg_cost=50.0, note="人工建倉")
    d = _rd(lconn)
    assert d["peak_qty"] == 100 and d["entry_norm_pe"] == pytest.approx(20.0)   # 舊期 500 不沿用
    book.adjust(lconn, date="2026-10-03", ticker="KO", set_qty=150, set_avg_cost=55.0, note="人工加倉")
    d = _rd(lconn)
    assert d["peak_qty"] == 150 and d["entry_norm_pe"] == pytest.approx(22.0)
    book.adjust(lconn, date="2026-10-04", ticker="KO", set_qty=90, note="人工減碼")
    assert _rd(lconn)["peak_qty"] == 150                                         # 減少不降
    book.adjust(lconn, date="2026-10-05", ticker="KO", set_qty=0, note="人工清倉")
    book.adjust(lconn, date="2026-10-06", ticker="KO", set_qty=10, set_avg_cost=40.0, note="再建倉")
    assert _rd(lconn)["peak_qty"] == 10                                          # 新持有期重新起算


def test_reconcile_new_holding_period_resets_peak(lconn):
    # 帳上有持股、登錄表非 HOLDING（不經 broker／adjust 的 registry_hold，例如先有持股後才登錄）
    with book.transaction(lconn):
        book.write_row(lconn, "positions", {"ticker": "KO", "qty": 80, "avg_cost": 50.0,
                                            "first_buy_date": "2026-10-01", "updated_at": db.now_iso()}, ["ticker"])
    book.reconcile_registry(lconn, "2026-10-02")
    d = _rd(lconn)
    row = lconn.execute("SELECT status FROM registry WHERE ticker='KO'").fetchone()
    assert row["status"] == "HOLDING" and d["peak_qty"] == 80 and d["entry_norm_pe"] == pytest.approx(20.0)
