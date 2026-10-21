"""2026-10-18 Hark 修正 X35：讀最新訊號卡時排除已作廢卡（data.voided_at，`trader decisions void`，M8-D4-02；
與 portfolio.sizing.NOT_VOIDED 同口徑）。holdings_view._valued_card（G8 比較檔）與 correlation.member_info
（external_drivers、g8、P-29 尾部上限）修前會讀到作廢卡。不連網、不下單。"""
import json

import pytest

from trader import db
from trader.ledger import book
from trader.paths import get_paths
from trader.portfolio import correlation as C
from trader.portfolio import holdings_view as H

ASOF = "2026-10-16"
OLD = "Fri 2026-10-02 8:00 AM CST (UTC+08:00)"
NEW = "Sat 2026-10-10 8:00 AM CST (UTC+08:00)"
VOID = {"voided_at": "Sun 2026-10-11 9:00 AM CST (UTC+08:00)", "voided_note": "測試", "superseded_by": None}
CAP = {"oe": 5.0, "g10": 0.07, "g5": 0.06, "grade": "B", "long_rate": 0.047, "long_rate_20y_med": 0.035,
       "price": 60.0, "norm_eps": 5.2, "largecap_pe_median": 19.0, "financial": False,
       "incr_roic_gt_rd": True, "roe": 0.22, "eps_cagr5": 0.06}


@pytest.fixture
def env(tmp_path):
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100_000.0, "2026-10-01")
    yield conn, paths
    conn.close()


def _sig(conn, did, t, data, created, signal="BUY"):
    db.insert(conn, "signals", {"decision_id": did, "ticker": t, "signal": signal, "data": data, "created_at": created})


def _hold(conn, paths, t):
    db.insert(conn, "positions", {"ticker": t, "qty": 10, "avg_cost": 50.0, "first_buy_date": "2026-10-02",
                                  "updated_at": db.now_iso()})
    d = paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{t}.json").write_text(json.dumps(CAP), encoding="utf-8")


def _row(view, t):
    return next(r for r in view["holdings"] if r["ticker"] == t)


# ---------------------------------------------------------------- holdings_view

def test_valued_card_skips_voided(env):
    conn, paths = env
    _hold(conn, paths, "AAA")
    _sig(conn, "AAA-1", "AAA", {"as_of": "2026-10-02", "signal": "BUY", "value_per_share": {"conservative": 40.0}}, OLD)
    _sig(conn, "AAA-2", "AAA", {"as_of": "2026-10-10", "signal": "SELL_ALL", "certainty_grade": "C",
                                "value_per_share": {"conservative": 80.0}, **VOID}, NEW, signal="SELL_ALL")
    assert H._valued_card(conn, "AAA")["as_of"] == "2026-10-02"
    r = _row(H.holdings_g8(conn, paths, ASOF, {"AAA": {"price": 50.0, "price_date": ASOF}}, None), "AAA")
    assert r["card_date"] == "2026-10-02" and r["card_signal"] == "BUY"


def test_valued_card_fallback_skips_voided(env):
    """全部都沒有估值時退回最新卡，也不得退回作廢卡（writeback.latest_signal 本身未過濾）。"""
    conn, paths = env
    _sig(conn, "AAA-1", "AAA", {"as_of": "2026-10-02", "signal": "NO_ACTION", "value_per_share": None}, OLD,
         signal="NO_ACTION")
    _sig(conn, "AAA-2", "AAA", {"as_of": "2026-10-10", "signal": "SELL_ALL", **VOID}, NEW, signal="SELL_ALL")
    assert H._valued_card(conn, "AAA").get("as_of") == "2026-10-02"
    conn.execute("DELETE FROM signals WHERE decision_id='AAA-1'")
    assert H._valued_card(conn, "AAA") == {}        # 只剩作廢卡 → 視同沒有卡


# ---------------------------------------------------------------- correlation

def test_member_info_skips_voided_cards(env):
    conn, _ = env
    db.upsert(conn, "registry", {"ticker": "BBB", "status": "HOLDING",
                                 "data": {"ticker": "BBB", "grade": "B", "asset_class": "非金融"},
                                 "updated_at": db.now_iso()}, key=["ticker"])   # 登錄表無 external_drivers → 讀卡
    good = {"external_drivers": ["interest_rate_up"], "g8_adjusted_expected_return": 0.08,
            "max_weight_pct": {"tail": 20}}
    bad = {"external_drivers": ["oil_price_up"], "g8_adjusted_expected_return": 0.30,
           "max_weight_pct": {"tail": 40}, **VOID}
    _sig(conn, "BBB-1", "BBB", good, OLD)
    _sig(conn, "BBB-2", "BBB", bad, NEW)
    m = C.member_info(conn, "BBB", C.load_vocab())
    assert m["drivers"] == ["interest_rate_up"]
    assert m["g8"] == 0.08
    assert m["worst_loss"] == C.tail_worst_loss(good) != C.tail_worst_loss(bad)


def test_member_info_only_voided_means_unknown(env):
    conn, _ = env
    _sig(conn, "CCC-1", "CCC", {"external_drivers": ["oil_price_up"], "g8_adjusted_expected_return": 0.3,
                                "max_weight_pct": {"tail": 40}, **VOID}, NEW)
    m = C.member_info(conn, "CCC", C.load_vocab())
    assert m["drivers"] is None and m["variables"] is None and m["g8"] is None and m["worst_loss"] is None
