"""2026-10-18 Hark 修正 第 6 項：相關群上限 P-31／共同情境損失 P-32（portfolio/correlation.py、sizing 擋單與縮量、
FULL_B 降級、判定保存與論點檔增補、>10% 減碼建議）。不連網、不下單。"""
import json

import pytest

from trader import db
from trader.ledger import book
from trader.paths import get_paths
from trader.portfolio import correlation as C
from trader.portfolio import sizing as S

ASOF = "2026-10-16"
GATES = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}
VOCAB = C.load_vocab()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    book.ensure_initial_deposit(c, 90000.0, "2026-10-01")   # 現金 9 萬＋下面 1 萬持股 → 權益 10 萬
    yield c
    c.close()


def _pos(conn, t, qty, avg, first="2026-09-01"):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": first,
                                  "updated_at": db.now_iso()})


def _reg(conn, t, status, **data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"ticker": t, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _card(drivers=("interest_rate_up",), first_pct=50, **kw):
    c = {"target_weight_pct": 10, "certainty_grade": "B", "auto_buy_cap": 60.0, "price_to_conservative": 0.8,
         "value_per_share": {"conservative": 60.0}, "max_weight_pct": {"host": 15},
         "position_plan": {"first_tranche_pct_of_target": first_pct},
         "external_drivers": list(drivers) if drivers is not None else None}
    c.update(kw)
    return c


def _item(did, t, card):
    return {"decision_id": did, "ticker": t, "action": "BUY", "review_type": None,
            "log": {**GATES, "action": "BUY"}, "card": card}


def _cand(conn, t, card, fin=False):
    _reg(conn, t, "WATCH", grade="B", asset_class="保險" if fin else "非金融",
         external_drivers=card.get("external_drivers"))
    return _item(f"{t}-20261016-1", t, card)


# ---------------------------------------------------------------- 純函式

def test_vocab_has_user_examples_and_variables():
    for i in ("oil_price_down", "oil_price_up", "interest_rate_up", "consumer_credit", "housing_starts"):
        assert i in VOCAB
    assert VOCAB["interest_rate_up"] == VOCAB["interest_rate_down"] == "interest_rate"


def test_normalize_drivers_unknown_and_missing():
    ids, basis, warns = C.normalize_drivers(
        [{"driver": "Oil_Price_Down", "kill_criterion": "WTI < 50 連兩季"}, {"driver": "moon_phase"}], VOCAB)
    assert ids == ["oil_price_down", "moon_phase"] and basis[0]["kill_criterion"] == "WTI < 50 連兩季"
    assert len(warns) == 1 and "moon_phase" in warns[0]
    assert C.normalize_drivers(None, VOCAB) == (None, [], [])          # 舊卡：未知
    assert C.normalize_drivers([], VOCAB)[0] == []                     # 已判定：沒有外部變數


def test_build_groups_connected_components_and_unknown():
    m = {"A": {"variables": ["oil_price"]}, "B": {"variables": ["oil_price", "interest_rate"]},
         "C": {"variables": ["interest_rate"]}, "D": {"variables": ["tech_capex"]}, "E": {"variables": None},
         "F": {"variables": []}}
    groups, unknown = C.build_groups(m)
    assert groups[0]["members"] == ["A", "B", "C"]                     # A–B 共享油價、B–C 共享利率 → 同群
    assert set(groups[0]["variables"]) == {"oil_price", "interest_rate"}
    assert ["D"] in [g["members"] for g in groups] and unknown == ["E"]


def test_group_metrics_cap_and_p32_thresholds():
    mem = [{"ticker": t, "impairment": 0.30, "grade": "B"} for t in ("A", "B")]
    met = C.group_metrics(mem, {"A": 0.10, "B": 0.10})
    assert met["loss"] == pytest.approx(0.06) and met["stop_new"] and not met["trim"]
    assert met["cap"] == pytest.approx(0.20)                           # min(2×10%, 10pp÷30%＝33%)
    fin = [{"ticker": t, "impairment": 0.50, "grade": "A"} for t in ("A", "B")]
    met = C.group_metrics(fin, {"A": 0.05, "B": 0.04})
    assert met["cap"] == pytest.approx(0.20) and not met["stop_new"]  # min(2×15%, 10pp÷50%＝20%)


def test_trim_recommendations_weakest_first_until_ten_percent():
    mem = [{"ticker": "STRONG", "impairment": 0.5, "grade": "A", "g8": 0.09},
           {"ticker": "MID", "impairment": 0.5, "grade": "B", "g8": 0.08},
           {"ticker": "WEAK", "impairment": 0.5, "grade": "B", "g8": 0.05}]
    w = {"STRONG": 0.15, "MID": 0.10, "WEAK": 0.05}                    # 損失 15%
    recs = C.trim_recommendations(mem, w, {"WEAK": 10.0, "MID": 20.0}, 100000)
    assert [r["ticker"] for r in recs] == ["WEAK", "MID"]               # 同為 B 級：G8 低者先；A 級最後
    assert recs[0]["sell_weight"] == pytest.approx(0.05) and recs[1]["sell_weight"] == pytest.approx(0.05)
    assert recs[0]["sell_qty_approx"] == 500
    left = sum((w[r["ticker"]] - r["sell_weight"]) * 0.5 for r in recs) + 0.15 * 0.5
    assert left == pytest.approx(0.10)
    assert C.trim_recommendations(mem, {"STRONG": 0.1, "MID": 0.05}) == []


def test_decide_drivers_only_new_facts_change():
    assert C.decide_drivers(None, ["oil_price_down"], None) == (["oil_price_down"], None)
    kept, note = C.decide_drivers(["oil_price_down"], ["housing_starts"], None)
    assert kept == ["oil_price_down"] and "只能因新事實改判" in note
    kept, note = C.decide_drivers(["oil_price_down"], ["housing_starts"], "2026-09 10-Q：油氣業務已出售")
    assert kept == ["housing_starts"] and "新事實" in note
    assert C.decide_drivers(["a"], None, None) == (["a"], None)


# ---------------------------------------------------------------- 三檔同變數陸續買進

def test_three_same_driver_stocks_second_is_trimmed_third_blocked(conn):
    """AAA 持有 10%（怕利率大升）；BBB、CCC 同怕利率大升。BBB T 10% 先被 P-32 剩餘額度夾到 6.67%（M6-D2-07），
    首批固定 50%（P-36，卡上 100% 不採用）→ 3.33%；同日 CCC 看到 BBB 已排的 3.3% → 剩餘約 3.37%，T 夾到該值、首批一半。"""
    _pos(conn, "AAA", 100, 100.0)
    _reg(conn, "AAA", "HOLDING", grade="B", asset_class="非金融", external_drivers=["interest_rate_up"])
    b = _cand(conn, "BBB", _card(first_pct=100, g8_adjusted_expected_return=0.09))
    c = _cand(conn, "CCC", _card(first_pct=100, g8_adjusted_expected_return=0.08))
    quotes = {"AAA": 100.0, "BBB": 50.0, "CCC": 50.0}
    plans = S.plan_buys(conn, ASOF, [b, c], quotes, {"BBB": 1e9, "CCC": 1e9}, 0)
    pb = next(p for p in plans if p["ticker"] == "BBB")
    pc = next(p for p in plans if p["ticker"] == "CCC")
    assert pb["qty"] == 66 and pb["target_weight"] == pytest.approx(0.0667, abs=1e-3)
    assert any("P-31／P-32 目標權重" in n for n in pb["notes"]) and pb["corr_group"] == ["AAA", "BBB"]
    assert any("首批比例固定 50%" in n for n in pb["notes"])
    assert pc["qty"] == 33 and pc["target_weight"] == pytest.approx(0.0337, abs=1e-3)


def test_third_buy_blocked_when_group_loss_above_five_percent(conn):
    _pos(conn, "AAA", 100, 100.0)       # 權益 9 萬現金＋1 萬＋1.5 萬＝11.5 萬
    _pos(conn, "BBB", 300, 50.0)        # 群合計 21.7% → 損失 6.52% > 5%
    for t in ("AAA", "BBB"):
        _reg(conn, t, "HOLDING", grade="B", asset_class="非金融", external_drivers=["interest_rate_up"])
    c = _cand(conn, "CCC", _card(drivers=("interest_rate_down",)))   # 同變數（利率）不同方向：照 P-31 字面同群
    plans = S.plan_buys(conn, ASOF, [c], {"AAA": 100.0, "BBB": 50.0, "CCC": 50.0}, {"CCC": 1e9}, 0)
    assert plans[0]["qty"] == 0 and "停止新買" in plans[0]["reason"] and "6.52%" in plans[0]["reason"]
    # 加碼（ADD）同樣被擋：BBB 已持有
    add = {"decision_id": "BBB-20261016-1", "ticker": "BBB", "action": "ADD", "review_type": None,
           "log": {**GATES, "action": "ADD"}, "card": _card(target_weight_pct=15, certainty_grade="A")}
    plans = S.plan_buys(conn, ASOF, [add], {"AAA": 100.0, "BBB": 50.0}, {"BBB": 1e9}, 0)
    assert plans[0]["qty"] == 0 and "停止新買" in plans[0]["reason"]


def test_unrelated_and_unknown_drivers_not_blocked(conn):
    _pos(conn, "AAA", 100, 100.0)
    _pos(conn, "BBB", 140, 50.0)
    for t in ("AAA", "BBB"):
        _reg(conn, t, "HOLDING", grade="B", asset_class="非金融", external_drivers=["interest_rate_up"])
    other = _cand(conn, "OIL", _card(drivers=("oil_price_down",)))
    old = _cand(conn, "OLD", _card(drivers=None))                      # 舊卡沒有 external_drivers
    plans = S.plan_buys(conn, ASOF, [other, old], {"AAA": 100.0, "BBB": 50.0, "OIL": 50.0, "OLD": 50.0},
                        {"OIL": 1e9, "OLD": 1e9}, 0)
    po = next(p for p in plans if p["ticker"] == "OIL")
    pd_ = next(p for p in plans if p["ticker"] == "OLD")
    assert po["qty"] > 0 and not any("P-31" in n for n in po["notes"])
    assert pd_["qty"] > 0 and any("沒有外部變數判定" in n for n in pd_["notes"])   # 不擋，但記 warning


def test_financial_group_uses_fifty_percent(conn):
    _pos(conn, "INS1", 100, 100.0)      # 10% 金融
    _reg(conn, "INS1", "HOLDING", grade="B", asset_class="保險", external_drivers=["catastrophe_losses"])
    c = _cand(conn, "INS2", _card(drivers=("catastrophe_losses",)), fin=True)
    plans = S.plan_buys(conn, ASOF, [c], {"INS1": 100.0, "INS2": 50.0}, {"INS2": 1e9}, 0)
    assert plans[0]["qty"] == 0 and "停止新買" not in plans[0]["reason"]   # 損失恰 5%：不大於 5%，但額度 0
    assert "剩餘額度 0.00%" in plans[0]["reason"]


def test_portfolio_groups_report_and_trim_anomalies(conn):
    for t, q in (("B1", 150), ("B2", 100), ("B3", 50)):   # 金融 15%＋10%＋5%＝30% → 損失 15%
        _pos(conn, t, q, 100.0)
    book.ensure_initial_deposit(conn, 90000.0, "2026-10-01")
    _reg(conn, "B1", "HOLDING", grade="A", asset_class="銀行", external_drivers=["consumer_credit"])
    _reg(conn, "B2", "HOLDING", grade="B", asset_class="銀行", external_drivers=["consumer_credit", "interest_rate_down"])
    _reg(conn, "B3", "HOLDING", grade="B", asset_class="銀行", external_drivers=["interest_rate_up"])
    _pos(conn, "OLD", 10, 100.0)
    _reg(conn, "OLD", "HOLDING", grade="B")                 # 沒有判定 → 列入報表
    rep = C.portfolio_groups(conn, {"B1": 100.0, "B2": 100.0, "B3": 100.0, "OLD": 100.0})
    g = rep["groups"][0]
    assert g["members"] == ["B1", "B2", "B3"] and g["trim"]
    assert rep["unknown_drivers"] == ["OLD"] and rep["warnings"]
    recs = rep["trim_recommendations"]
    assert recs and recs[0]["ticker"] in ("B2", "B3") and all(r["ticker"] != "B1" for r in recs[:1])
    assert C.record_trim_anomalies(conn, rep, ASOF) == len(recs)
    assert C.record_trim_anomalies(conn, rep, ASOF) == 0           # 未結時不重複
    row = conn.execute("SELECT pause_trading FROM anomalies WHERE kind='corr_group_trim'").fetchone()
    assert row["pause_trading"] == 0                                 # 只建議，不暫停、不下單
    assert not book.open_orders(conn)


# ---------------------------------------------------------------- 判定保存：兩檔論點檔＋登錄表

def test_record_group_links_writes_both_theses_once(conn, state_dir):
    paths = get_paths()
    paths.theses.mkdir(parents=True, exist_ok=True)
    for t in ("AAA", "NEW"):
        (paths.theses / f"{t}_2026-01-02.md").write_text("# 論點\n", encoding="utf-8")
    _pos(conn, "AAA", 100, 100.0)
    _reg(conn, "AAA", "HOLDING", grade="B", thesis_file="theses/AAA_2026-01-02.md",
         external_drivers=["oil_price_down"],
         external_drivers_basis=[{"driver": "oil_price_down", "kill_criterion": "WTI 連兩季 < 50"}])
    _reg(conn, "NEW", "WATCH", grade="B", thesis_file="theses/NEW_2026-01-02.md",
         external_drivers=["oil_price_up"],
         external_drivers_basis=[{"driver": "oil_price_up", "kill_criterion": "航油成本占營收 > 35%"}])
    ch = C.record_group_links(conn, paths, "NEW", ASOF)
    assert [(x["a"], x["b"], x["variables"]) for x in ch] == [("NEW", "AAA", ["oil_price"])]
    for t, other in (("AAA", "NEW"), ("NEW", "AAA")):
        add = (paths.theses / f"{t}_2026-01-02_addendum_1.md").read_text(encoding="utf-8")
        assert "相關群判定（P-31）" in add and other in add and "WTI 連兩季 < 50" in add
        reg = json.loads(conn.execute("SELECT data FROM registry WHERE ticker=?", (t,)).fetchone()["data"])
        assert reg["corr_links"][other]["variables"] == ["oil_price"]
    assert C.record_group_links(conn, paths, "NEW", ASOF) == []      # 已判定、未變：不重寫
    assert not (paths.theses / "AAA_2026-01-02_addendum_2.md").exists()


def test_hold_review_writeback_keeps_drivers_without_new_fact(conn, state_dir):
    from trader.analysis.writeback import write_hold_review_result
    paths = get_paths()
    paths.theses.mkdir(parents=True, exist_ok=True)
    (paths.theses / "AAA_2026-01-02.md").write_text("# 論點\n", encoding="utf-8")
    _pos(conn, "AAA", 100, 100.0)
    _reg(conn, "AAA", "HOLDING", grade="B", thesis_file="theses/AAA_2026-01-02.md",
         external_drivers=["oil_price_down"], external_drivers_determined_on="2026-01-02")
    res = {"ticker": "AAA", "decision_date": ASOF, "path": [], "action": "HOLD", "sell_category": None,
           "trim_fraction": None, "stop_adding": False, "observe_until": None, "falsified_assumption": None,
           "external_drivers": [{"driver": "housing_starts", "kill_criterion": "開工連兩季 -20%"}],
           "external_drivers_change_reason": None, "reason": "持有"}
    write_hold_review_result(conn, paths, "AAA", ASOF, "r1", res)
    reg = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])
    assert reg["external_drivers"] == ["oil_price_down"] and reg["external_drivers_determined_on"] == "2026-01-02"
    log = json.loads(conn.execute("SELECT data FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()["data"])
    assert any("只能因新事實改判" in n for n in log["framework_notes"])
    res["external_drivers_change_reason"] = "2026-09-30 8-K：出售油氣部門，改為營建材料"
    write_hold_review_result(conn, paths, "AAA", ASOF, "r2", res)
    reg = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])
    assert reg["external_drivers"] == ["housing_starts"] and reg["external_drivers_determined_on"] == ASOF


def test_full_b_corr_writeback_downgrades_buy_when_group_over_five_percent(conn, state_dir, tmp_path):
    """FULL_B 後置：同群持股已使 P-32 >5% → 回傳停止理由（呼叫端把 BUY 改 WATCH、停在 G11）；未超過時只寫 card.correlated_group.room，不動 max_weight_pct（M6-D2-02）。"""
    from types import SimpleNamespace
    from trader.jobs.deep import _corr_writeback
    paths = get_paths()
    for t in ("AAA", "BBB"):
        _reg(conn, t, "HOLDING", grade="B", asset_class="非金融", external_drivers=["housing_starts"])
    td = tmp_path / "td"
    td.mkdir()
    (td / "phaseA.json").write_text(json.dumps({"asset_class": "非金融"}), encoding="utf-8")
    view = {"holdings": [{"ticker": "AAA", "market_weight": 0.10}, {"ticker": "BBB", "market_weight": 0.08}]}
    (td / "holdings_g8.json").write_text(json.dumps(view), encoding="utf-8")
    ctx = SimpleNamespace(conn=conn, paths=paths)
    card = {"certainty_grade": "B", "max_weight_pct": {"correlated_group": None}}
    notes: list[str] = []
    why = _corr_writeback(ctx, "NEW", td, card, {"external_drivers": [
        {"driver": "housing_starts", "kill_criterion": "新屋開工年減 >25%"}], "external_drivers_change_reason": None},
        notes)
    assert why and "停止新買" in why
    saved = json.loads((td / "signal_card.json").read_text(encoding="utf-8"))
    assert saved["external_drivers"] == ["housing_starts"] and saved["correlated_group"]["status"] == "blocked"
    assert saved["max_weight_pct"] == {"correlated_group": None} and saved["correlated_group"]["room"] == 0.0
    # 只有 AAA 4%：損失 1.2%，剩餘 min(20%−4%, (5%−1.2%)÷30%＝12.67%) → 12.67
    (td / "holdings_g8.json").write_text(json.dumps({"holdings": [{"ticker": "AAA", "market_weight": 0.04}]}),
                                         encoding="utf-8")
    card = {"certainty_grade": "B", "max_weight_pct": {"correlated_group": 15}}
    assert _corr_writeback(ctx, "NEW", td, card, {"external_drivers": [{"driver": "housing_starts"}]}, []) is None
    saved = json.loads((td / "signal_card.json").read_text(encoding="utf-8"))
    assert saved["correlated_group"]["room"] == pytest.approx(0.1267, abs=1e-4)
    assert saved["max_weight_pct"] == {"correlated_group": 15}   # 剩餘增量不寫進總權重上限


def test_tranche_add_blocked_by_p32(conn):
    _pos(conn, "AAA", 100, 100.0, first="2026-10-10")
    _pos(conn, "BBB", 300, 50.0, first="2026-10-10")
    _reg(conn, "AAA", "HOLDING", grade="B", asset_class="非金融", external_drivers=["housing_starts"])
    _reg(conn, "BBB", "HOLDING", grade="B", asset_class="非金融", external_drivers=["housing_starts"],
         auto_buy_cap=60.0, build_plan={"target_weight": 0.15, "tranches_left": 2, "days_above_cap": 0})
    db.insert(conn, "decisions", {"decision_id": "BBB-20261010-1", "ticker": "BBB", "decision_date": "2026-10-10",
                                  "signal": "BUY", "data": {**GATES, "action": "BUY"}, "created_at": db.now_iso()})
    plans, cancels = S.plan_tranches(conn, ASOF, {"AAA": 100.0, "BBB": 50.0}, {"BBB": 1e9}, 0)
    assert not cancels
    assert plans[0]["qty"] == 0 and "停止新買" in plans[0]["reason"]
