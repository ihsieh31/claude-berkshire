"""G8 持股比較檔（portfolio/holdings_view.py）：決策日收盤重算預期報酬、可否加碼、最佳／最弱持股、排除本標的。"""
import json

import pandas as pd
import pytest

from trader import db
from trader.ledger import book
from trader.paths import get_paths
from trader.portfolio import g8_benchmarks as B
from trader.portfolio import holdings_view as H

ASOF = "2026-10-16"
CAP = {"oe": 5.0, "g10": 0.07, "g5": 0.06, "grade": "B", "long_rate": 0.047, "long_rate_20y_med": 0.035,
       "price": 60.0, "norm_eps": 5.2, "largecap_pe_median": 19.0, "financial": False,
       "incr_roic_gt_rd": True, "roe": 0.22, "eps_cagr5": 0.06}
G0 = 0.045   # min(8%, min(7%, 6%) × 0.75)


@pytest.fixture
def env(tmp_path):
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100_000.0, "2026-10-01")
    yield conn, paths
    conn.close()


def _hold(conn, paths, t, qty, avg, grade="B", oe=5.0, card=None):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": "2026-10-02",
                                  "updated_at": db.now_iso()})
    d = paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{t}.json").write_text(json.dumps({**CAP, "grade": grade, "oe": oe}), encoding="utf-8")
    if card is not None:
        db.insert(conn, "signals", {"decision_id": f"{t}-1", "ticker": t, "signal": "BUY", "data": card,
                                    "created_at": "2026-10-02T00:00:00Z"})


def _px(**kw):
    return {t: {"price": p, "price_date": ASOF} for t, p in kw.items()}


def _row(view, t):
    return next(r for r in view["holdings"] if r["ticker"] == t)


def test_empty_book_has_no_rows(env):
    conn, paths = env
    v = H.holdings_g8(conn, paths, ASOF, {}, None)
    assert v["holdings"] == [] and v["best_addable"] is None and v["weakest"] is None
    assert "無持股" in v["note"]


def test_expected_return_uses_close_not_cost_and_picks_best(env):
    conn, paths = env
    _hold(conn, paths, "AAA", 100, 10.0, grade="B")      # 買得很便宜：成本不得影響預期報酬（CORE-07）
    _hold(conn, paths, "BBB", 50, 100.0, grade="A")
    v = H.holdings_g8(conn, paths, ASOF, _px(AAA=50.0, BBB=100.0), None)
    a, b = _row(v, "AAA"), _row(v, "BBB")
    assert a["expected_return"] == pytest.approx(5 / 50 + G0, abs=1e-4)
    assert a["adjusted_expected_return"] == pytest.approx((5 / 50 + G0) * 0.75, abs=1e-4)
    assert b["adjusted_expected_return"] == pytest.approx((5 / 100 + G0) * 0.9, abs=1e-4)
    assert v["best_addable"]["ticker"] == "AAA"
    assert v["weakest"] is None                          # 持股 <10 檔不比最弱
    equity = 100_000 + 100 * 50 + 50 * 100            # 測試直接寫持股、未扣現金
    assert a["market_weight"] == round(100 * 50 / equity, 4)
    assert a["cost_weight"] == round(100 * 10 / (100_000 + 100 * 10 + 50 * 100), 4)
    assert a["addable"] and a["iv_cons"] and a["price_to_conservative"] == round(50 / a["iv_cons"], 4)


def test_skips_valuationless_card_for_card_date(env):
    conn, paths = env
    _hold(conn, paths, "AAA", 10, 50.0, card={"as_of": "2026-09-01", "value_per_share": {"conservative": 40.0}})
    db.insert(conn, "signals", {"decision_id": "AAA-2", "ticker": "AAA", "signal": "NO_ACTION",
                                "data": {"as_of": "2026-10-10", "value_per_share": None},
                                "created_at": "2026-10-10T00:00:00Z"})
    v = H.holdings_g8(conn, paths, ASOF, _px(AAA=50.0), None)
    assert _row(v, "AAA")["card_date"] == "2026-09-01"


def test_not_addable_reasons_exclude_from_best(env):
    conn, paths = env
    _hold(conn, paths, "BIG", 400, 50.0)                 # 400 × 50 = 20,000 → 權重 20% > 15%
    _hold(conn, paths, "CCC", 10, 50.0, grade="C")
    _hold(conn, paths, "SUS", 10, 50.0)
    db.insert(conn, "decisions", {"decision_id": "SUS-T", "ticker": "SUS", "decision_date": "2026-10-10",
                                  "signal": None, "data": {"review_type": "THESIS", "thesis_check": "疑似"},
                                  "created_at": db.now_iso()})
    v = H.holdings_g8(conn, paths, ASOF, _px(BIG=50.0, CCC=50.0, SUS=50.0), None)
    assert any("15%" in x for x in _row(v, "BIG")["not_addable"])
    assert any("A／B" in x for x in _row(v, "CCC")["not_addable"])
    assert _row(v, "SUS")["not_addable"] == ["論點疑似或證偽"]
    assert v["best_addable"] is None
    assert _row(v, "CCC")["adjusted_expected_return"] == pytest.approx((0.1 + G0) * 0.5, abs=1e-4)


def test_missing_close_or_cap_inputs_is_noted(env):
    conn, paths = env
    _hold(conn, paths, "AAA", 10, 50.0)
    db.insert(conn, "positions", {"ticker": "NOC", "qty": 5, "avg_cost": 20.0, "first_buy_date": "2026-10-02",
                                  "updated_at": db.now_iso()})
    v = H.holdings_g8(conn, paths, ASOF, {"AAA": {"price": float("nan"), "price_date": float("nan")}}, None)
    a, n = _row(v, "AAA"), _row(v, "NOC")
    assert a["close"] is None and a["adjusted_expected_return"] is None and "收盤價" in a["notes"][0]
    assert n["adjusted_expected_return"] is None and "cap_inputs" in n["notes"][0]
    assert v["best_addable"] is None


def test_rates_update_long_rate_for_iv(env):
    conn, paths = env
    _hold(conn, paths, "AAA", 10, 50.0)
    lo = _row(H.holdings_g8(conn, paths, ASOF, _px(AAA=50.0), None), "AAA")
    hi = _row(H.holdings_g8(conn, paths, ASOF, _px(AAA=50.0), {"t30": 0.12, "t30_20y_median": 0.035}), "AAA")
    assert hi["iv_cons"] < lo["iv_cons"]
    assert hi["expected_return"] == lo["expected_return"]   # 預期報酬不含折現率


def test_weakest_only_at_max_holdings(env):
    conn, paths = env
    for i in range(10):
        _hold(conn, paths, f"T{i}", 1, 50.0, oe=1.0 + i)
    v = H.holdings_g8(conn, paths, ASOF, _px(**{f"T{i}": 50.0 for i in range(10)}), None)
    assert v["weakest"]["ticker"] == "T0" and v["best_addable"]["ticker"] == "T9"


def test_for_ticker_excludes_self(env):
    conn, paths = env
    _hold(conn, paths, "AAA", 10, 50.0)
    _hold(conn, paths, "BBB", 10, 50.0, oe=2.0)
    v = H.holdings_g8(conn, paths, ASOF, _px(AAA=50.0, BBB=50.0), None)
    f = H.for_ticker(v, "AAA")
    assert [r["ticker"] for r in f["holdings"]] == ["BBB"]
    assert f["best_addable"]["ticker"] == "BBB" and f["excluded_self"] == "AAA"
    assert "excluded_self" not in H.for_ticker(v, "ZZZ")


def test_write_view_path(env):
    conn, paths = env
    v = H.write_view(conn, paths, "analyze-x", ASOF, {}, None)
    assert json.loads(H.view_path(paths, "analyze-x").read_text(encoding="utf-8")) == v


def test_self_adjusted_and_notes():
    cap = {"g0": {"cons": G0}, "grade_used": "B"}
    adj = H.self_adjusted({**CAP, "price": 50.0}, cap)
    assert adj == pytest.approx((0.1 + G0) * 0.75, abs=1e-4)
    assert H.self_adjusted({**CAP, "price": 50.0}, {"g0": {"cons": None}, "grade_used": "B"}) is None
    assert H.self_adjusted({**CAP, "price": 50.0}, {"g0": {"cons": G0}, "grade_used": "D"}) is None
    staged = {"best_addable": {"ticker": "BBB", "adjusted_expected_return": adj + 0.02}}
    notes = H.g8_notes({"self_adjusted": adj + 0.01, "best_holding": "BBB",
                        "best_holding_adjusted": adj + 0.02, "winner": "本標的"}, adj, staged)
    assert len(notes) == 2 and "腳本" in notes[0] and "高 ≥1pp" in notes[1]
    assert H.g8_notes({"self_adjusted": adj, "best_holding": None, "best_holding_adjusted": None,
                       "winner": "本標的"}, adj, {}) == []
    assert H.g8_notes(None, adj, staged) == []


# ---------------------------------------------------------------- 觀察名單、G8 勝負

def _watch(conn, paths, t, grade="B", oe=5.0):
    db.insert(conn, "registry", {"ticker": t, "status": "WATCH", "data": "{}", "updated_at": db.now_iso()})
    d = paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{t}.json").write_text(json.dumps({**CAP, "grade": grade, "oe": oe}), encoding="utf-8")


def test_watchlist_only_buyable_counts(env):
    conn, paths = env
    _watch(conn, paths, "CHEAP")              # 收盤 40 < 自動買進上限 → 可買
    _watch(conn, paths, "DEAR")               # 收盤 500 ≥ 上限 → 不算可得選項
    _watch(conn, paths, "GRC", grade="C")
    _hold(conn, paths, "HELD", 10, 50.0)
    db.insert(conn, "registry", {"ticker": "HELD", "status": "WATCH", "data": "{}", "updated_at": db.now_iso()})
    assert H.watchlist_tickers(conn) == ["CHEAP", "DEAR", "GRC"]   # 已持有者不算觀察名單
    v = H.holdings_g8(conn, paths, ASOF, _px(CHEAP=40.0, DEAR=500.0, GRC=40.0, HELD=50.0), None)
    w = {r["ticker"]: r for r in v["watchlist"]}
    assert w["CHEAP"]["buyable"] and not w["DEAR"]["buyable"] and not w["GRC"]["buyable"]
    assert "上限" in w["DEAR"]["not_buyable"][0]
    assert v["best_watch"]["ticker"] == "CHEAP"
    assert H.for_ticker(v, "CHEAP")["best_watch"] is None
    assert H.for_ticker(v, "CHEAP")["self"]["ticker"] == "CHEAP"


def _view(best=None, watch=None, weakest=None, treasury=None, market=None):
    r = lambda t, a: {"ticker": t, "adjusted_expected_return": a} if a is not None else None
    return {"best_addable": r("PEP", best), "best_watch": r("WMT", watch), "weakest": r("T", weakest),
            "benchmarks": {"treasury": {"adjusted": treasury} if treasury is not None else {"error": "x"},
                           "market": {"adjusted": market} if market is not None else {"error": "y"}}}


def test_g8_verdict_loss_tie_and_missing():
    assert H.g8_verdict(0.10, _view(best=0.105, treasury=0.042, market=0.016)) == (False, [])   # <1pp 同分
    lost, why = H.g8_verdict(0.10, _view(best=0.08, watch=0.12))
    assert lost and "觀察名單次佳 WMT" in why[0]
    lost, why = H.g8_verdict(0.03, _view(treasury=0.042))
    assert lost and "國庫券" in why[0]
    lost, why = H.g8_verdict(0.10, _view(weakest=0.115))
    assert lost and "最弱持股 T" in why[0]
    assert H.g8_verdict(None, _view())[0] is True
    assert H.g8_verdict(0.10, {})[0] is False                      # 無比較檔：不擋（只有缺列註記）
    assert len(H.missing_rows(_view())) == 2 and H.missing_rows(_view(treasury=0.04, market=0.02)) == []


# ---------------------------------------------------------------- 國庫券與大盤


def _ratio_series(vals):
    return pd.Series(vals, index=[f"{1950 + i}-01-01" for i in range(len(vals))], dtype=float)


@pytest.mark.parametrize("ratio,zone,nominal", [
    (0.50, "低", 0.11), (0.60, "低", 0.11), (0.90, "中", 0.065), (1.20, "中", 0.05),
    (1.35, "高", 0.04), (1.50, "高", 0.03), (2.55, "極高", 0.03),
])
def test_market_zone_by_absolute_cut(ratio, zone, nominal):
    s = _ratio_series([3.0] * 20 + [ratio])        # 歷史都更高 → 分位低，只看絕對切點
    m = B.market_row(s)
    assert m["zone"] == zone and m["p27_nominal"] == pytest.approx(nominal, abs=1e-4)
    assert m["expected_return"] == pytest.approx(max(nominal - 0.01, 0), abs=1e-4)
    assert m["adjusted"] == pytest.approx(m["expected_return"] * 0.8, abs=1e-4)


def test_market_percentile_pushes_zone_up():
    s = _ratio_series([0.3] * 20 + [0.9])          # 絕對 中、分位 100 → 極高
    m = B.market_row(s)
    assert (m["zone_absolute"], m["zone_percentile"], m["zone"]) == ("中", "極高", "極高")
    assert m["p27_nominal"] == 0.03
    s = _ratio_series([0.3] * 8 + [2.0] * 2 + [0.9])   # 分位 ≈ 81.8 → 高；比值不在高區 → 取高區上緣 5%
    m = B.market_row(s)
    assert m["zone"] == "高" and m["p27_nominal"] == 0.05


class FakeFred:
    def __init__(self, fail=False):
        self.fail = fail

    def latest(self, sid, asof):
        if self.fail:
            raise RuntimeError("down")
        assert sid == "DGS3MO"
        return 4.28, "2026-10-15"

    def equity_gdp(self, asof):
        if self.fail:
            raise RuntimeError("down")
        return _ratio_series([1.0] * 10 + [2.55])


def test_benchmarks_rows_and_errors():
    b = B.benchmarks(FakeFred(), ASOF)
    assert b["treasury"]["adjusted"] == 0.0428 and b["treasury"]["date"] == "2026-10-15"
    assert b["market"]["zone"] == "極高" and b["market"]["adjusted"] == pytest.approx(0.016)
    assert "error" in B.benchmarks(FakeFred(fail=True), ASOF)["market"]
    assert "未設定" in B.benchmarks(None, ASOF)["treasury"]["error"]


def test_fred_equity_gdp_respects_release_lag(tmp_path):
    from trader.data.fred import FredClient
    f = FredClient(None, tmp_path)
    data = {"NCBEILQ027S": pd.Series([50_000_000.0, 60_000_000.0], index=["2026-01-01", "2026-04-01"]),
            "GDP": pd.Series([25_000.0, 30_000.0], index=["2026-01-01", "2026-04-01"]),
            "DGS3MO": pd.Series([4.2, 4.3], index=["2026-10-14", "2026-10-16"])}
    f.series = lambda sid="DGS30": data[sid]
    s = f.equity_gdp("2026-09-01")                 # 4/1 + 170 天 = 9/18 → 該季尚未公布
    assert list(s.index) == ["2026-01-01"] and s.iloc[-1] == pytest.approx(2.0)
    assert f.equity_gdp("2026-10-16").iloc[-1] == pytest.approx(2.0)   # 60,000,000 百萬 ÷ 1000 ÷ 30,000 十億
    assert f.latest("DGS3MO", "2026-10-16") == (4.2, "2026-10-14")     # 嚴格早於決策日


# ---------------------------------------------------------------- 排序：當日報價重算

def test_rank_uses_live_g8_for_old_and_new_cards(env):
    from trader.portfolio import sizing as S
    conn, paths = env
    d = paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "OLD.json").write_text(json.dumps({**CAP, "oe": 8.0}), encoding="utf-8")
    (d / "NEW.json").write_text(json.dumps({**CAP, "oe": 4.0}), encoding="utf-8")
    old_card = {"value_per_share": {"conservative": 60.0}}               # 舊卡：沒有 g8 欄
    new_card = {"g8_adjusted_expected_return": 0.50}                     # 新卡：分析日存的值（已過時）
    k_old = S._rank_key(old_card, 50.0, S.live_g8(d, "OLD", 50.0))
    k_new = S._rank_key(new_card, 50.0, S.live_g8(d, "NEW", 50.0))
    assert k_old[0] == k_new[0] == 0 and k_old < k_new                  # 同一層比數值：OLD 盈餘殖利率較高
    assert S.live_g8(d, "MISSING", 50.0) is None and S.live_g8(d, "OLD", None) is None
    assert S._rank_key(new_card, 50.0, None) == (0, -0.50)              # 沒有 cap_inputs → 用卡上存的值
