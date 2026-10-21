"""P44（M6:B5 其餘＋D11＋D14）：G8 比較檔可加碼條件、grade_used 係數、相關群 P-31 改用 P-29 最壞損失與 binding、
外部變數未知 warning、改判新事實保存、corr_group_trim／switch_candidate 更新與自動結案。不連網、不下單。"""
import json

import pytest

from trader import db
from trader.ledger import book
from trader.paths import get_paths
from trader.pipelines import daily as Dly
from trader.portfolio import correlation as C
from trader.portfolio import holdings_view as H
from trader.portfolio import sizing as S

ASOF = "2026-10-16"
CAP = {"oe": 5.0, "g10": 0.07, "g5": 0.06, "grade": "B", "long_rate": 0.047, "long_rate_20y_med": 0.035,
       "price": 60.0, "norm_eps": 5.2, "largecap_pe_median": 19.0, "financial": False,
       "incr_roic_gt_rd": True, "roe": 0.22, "eps_cagr5": 0.06}
VOCAB = C.load_vocab()


@pytest.fixture
def env(tmp_path):
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100_000.0, "2026-10-01")
    yield conn, paths
    conn.close()


def _hold(conn, paths, t, qty, avg, grade="B", oe=5.0):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": avg, "first_buy_date": "2026-10-02",
                                  "updated_at": db.now_iso()})
    d = paths.state / "cap_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{t}.json").write_text(json.dumps({**CAP, "grade": grade, "oe": oe}), encoding="utf-8")


def _reg(conn, t, status, **data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"ticker": t, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _sig(conn, t, card, n=1):
    db.insert(conn, "signals", {"decision_id": f"{t}-{n}", "ticker": t, "signal": "BUY", "data": card,
                                "created_at": f"Fri 2026-10-0{n} 8:00 AM CST (UTC+08:00)"})


def _px(**kw):
    return {t: {"price": p, "price_date": ASOF} for t, p in kw.items()}


def _row(view, t):
    return next(r for r in view["holdings"] if r["ticker"] == t)


# ---------------------------------------------------------------- M6_D7_11：係數用 cap_calc grade_used

def test_holding_coef_uses_grade_used_after_p21_downgrade(env, monkeypatch):
    conn, paths = env
    _hold(conn, paths, "AAA", 10, 50.0, grade="A")

    class Fake:   # cap_inputs 為 A 級，cap_calc 因 P-21 區間 >3 降為 B
        @staticmethod
        def calc(x):
            return {"grade_used": "B", "g0": {"cons": 0.045}, "IV_cons": 80.0, "auto_buy_cap": 70.0}
    monkeypatch.setattr(H, "_skill_module", lambda name: Fake)
    r = _row(H.holdings_g8(conn, paths, ASOF, _px(AAA=50.0), None), "AAA")
    assert r["grade"] == "B" and r["certainty_coef"] == 0.75
    assert r["adjusted_expected_return"] == pytest.approx((5 / 50 + 0.045) * 0.75, abs=1e-4)


# ---------------------------------------------------------------- M6-D2-05：收盤 ≥ 自動買進上限不可加碼

def test_close_at_or_above_auto_buy_cap_is_not_best_addable(env):
    conn, paths = env
    _hold(conn, paths, "AAA", 10, 50.0)              # oe 5 → 上限約 76 > 50
    _hold(conn, paths, "HOT", 10, 50.0, oe=2.0)      # oe 2 → 上限約 30 ≤ 50
    v = H.holdings_g8(conn, paths, ASOF, _px(AAA=50.0, HOT=50.0), None)
    hot = _row(v, "HOT")
    assert hot["auto_buy_cap"] < 50 and "收盤 ≥ 自動買進上限" in hot["not_addable"] and not hot["addable"]
    f = H.for_ticker(v, "AAA")                       # 重看 AAA 時，HOT 不是可得選項
    assert f["best_addable"] is None
    assert v["best_addable"]["ticker"] == "AAA"


# ---------------------------------------------------------------- D14：剩餘加碼空間

def test_holding_without_room_below_target_or_grade_cap_not_addable(env):
    conn, paths = env
    _hold(conn, paths, "TGT", 100, 50.0)             # 權重約 4.5% ≥ 目標 T 3%
    _reg(conn, "TGT", "HOLDING", grade="B", target_weight=0.03)
    _hold(conn, paths, "BIG", 250, 50.0)             # 權重約 10.7% ≥ B 級上限 10%（未超過 15%）
    _hold(conn, paths, "OK", 20, 50.0)               # 權重約 0.9%，T 6%
    _reg(conn, "OK", "HOLDING", grade="B", target_weight=0.06)
    _hold(conn, paths, "BP", 20, 50.0)               # 登錄表無 T → 取建倉計畫 T 0.5%
    _reg(conn, "BP", "HOLDING", grade="B", build_plan={"target_weight": 0.005, "tranches_left": 1})
    v = H.holdings_g8(conn, paths, ASOF, _px(TGT=50.0, BIG=50.0, OK=50.0, BP=50.0), None)
    assert any("目標權重 T" in x for x in _row(v, "TGT")["not_addable"])
    assert any("B 級上限" in x for x in _row(v, "BIG")["not_addable"])
    assert any("目標權重 T" in x for x in _row(v, "BP")["not_addable"])
    assert _row(v, "OK")["addable"] and v["best_addable"]["ticker"] == "OK"


# ---------------------------------------------------------------- D11：P-31 用 P-29 最壞損失、binding

def test_tail_worst_loss_from_card_with_floor():
    assert C.tail_worst_loss({"max_weight_pct": {"tail": 10 / 0.7}}) == pytest.approx(0.7)
    assert C.tail_worst_loss({"max_weight_pct": {"tail": 50}}) == 0.25          # 20% → 下限 25%
    assert C.tail_worst_loss({"max_weight_pct": {"tail": None}}) is None
    assert C.tail_worst_loss({}) is None


def test_high_leverage_group_bound_by_p31():
    mem = [{"ticker": t, "impairment": 0.30, "worst_loss": 0.70, "grade": "B"} for t in ("A", "B")]
    met = C.group_metrics(mem, {"A": 0.04, "B": 0.04})
    assert met["worst_relative_loss"] == pytest.approx(0.70)
    assert met["cap"] == pytest.approx(0.10 / 0.70, abs=1e-6)   # 14.3% < P-32 換算 5%÷30%＝16.7%
    assert met["binding"] == "P31" and met["worst_basis"] == {"A": "P-29", "B": "P-29"}


def test_missing_tail_falls_back_to_p32_impairment():
    mem = [{"ticker": t, "impairment": 0.30, "grade": "B"} for t in ("A", "B")]
    met = C.group_metrics(mem, {"A": 0.04, "B": 0.04})
    assert met["worst_relative_loss"] == pytest.approx(0.30) and met["cap"] == pytest.approx(0.20)
    assert met["binding"] == "P32" and met["worst_basis"]["A"] == "P-32 減損"
    mixed = [{"ticker": "A", "impairment": 0.30, "worst_loss": 0.10, "grade": "B"},   # 下限 25%
             {"ticker": "B", "impairment": 0.50, "grade": "B"}]
    met = C.group_metrics(mixed, {"A": 0.06, "B": 0.02})
    assert met["worst_relative_loss"] == pytest.approx((0.06 * 0.25 + 0.02 * 0.50) / 0.08, abs=1e-4)


def test_buy_room_uses_card_tail_and_reports_p31_binding(env):
    conn, _ = env
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 40, "avg_cost": 100.0, "first_buy_date": "2026-10-02",
                                  "updated_at": db.now_iso()})                   # 4,000／104,000 ≈ 3.85%
    for t in ("AAA", "NEW"):
        _reg(conn, t, "HOLDING" if t == "AAA" else "WATCH", grade="B", asset_class="非金融",
             external_drivers=["housing_starts"])
        _sig(conn, t, {"max_weight_pct": {"tail": round(10 / 0.7, 4)}})
    room = C.buy_room(conn, "NEW", {"AAA": 100.0}, VOCAB)
    w = 4000 / 104000
    assert room["status"] == "ok" and room["binding"] == "P31"
    assert room["room"] == pytest.approx(0.10 / 0.70 - w, abs=1e-4)
    assert "P-31 綁定" in room["reason"]


# ---------------------------------------------------------------- M6-D5-05：持股外部變數未知 → warning

def test_buy_room_warns_when_holding_drivers_unknown(env):
    conn, _ = env
    db.insert(conn, "positions", {"ticker": "OLD", "qty": 40, "avg_cost": 100.0, "first_buy_date": "2026-10-02",
                                  "updated_at": db.now_iso()})
    _reg(conn, "OLD", "HOLDING", grade="B")                                      # 舊卡沒有判定
    _reg(conn, "NEW", "WATCH", grade="B", asset_class="非金融", external_drivers=["housing_starts"])
    room = C.buy_room(conn, "NEW", {"OLD": 100.0}, VOCAB)
    assert room["status"] == "none" and "OLD" in room["warning"] and "未納入分群" in room["warning"]


# ---------------------------------------------------------------- M6-D5-06：改判新事實存登錄表與增補

def test_change_reason_saved_and_in_addendum(env, state_dir):
    conn, _ = env
    entry: dict = {}
    prev = {"external_drivers": ["oil_price_down"], "external_drivers_determined_on": "2026-01-02"}
    res = {"external_drivers": [{"driver": "housing_starts", "kill_criterion": "開工年減 >25%"}],
           "external_drivers_change_reason": " 2026-09-30 8-K：出售油氣部門 "}
    C.apply_drivers_to_entry(entry, prev, res, ASOF, VOCAB, [])
    assert entry["external_drivers"] == ["housing_starts"]
    assert entry["external_drivers_change_reason"] == "2026-09-30 8-K：出售油氣部門"
    e2: dict = {}                                        # 首次判定不算改判
    C.apply_drivers_to_entry(e2, None, res, ASOF, VOCAB, [])
    assert e2["external_drivers_change_reason"] is None

    paths = get_paths()
    paths.theses.mkdir(parents=True, exist_ok=True)
    for t in ("AAA", "NEW"):
        (paths.theses / f"{t}_2026-01-02.md").write_text("# 論點\n", encoding="utf-8")
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 10, "avg_cost": 100.0, "first_buy_date": "2026-10-02",
                                  "updated_at": db.now_iso()})
    _reg(conn, "AAA", "HOLDING", grade="B", thesis_file="theses/AAA_2026-01-02.md",
         external_drivers=["housing_starts"])
    _reg(conn, "NEW", "WATCH", grade="B", thesis_file="theses/NEW_2026-01-02.md", **entry)
    ch = C.record_group_links(conn, paths, "NEW", ASOF)
    assert ch and ch[0]["change_reason"] == "2026-09-30 8-K：出售油氣部門"
    for t in ("AAA", "NEW"):
        add = (paths.theses / f"{t}_2026-01-02_addendum_1.md").read_text(encoding="utf-8")
        assert "出售油氣部門" in add


# ---------------------------------------------------------------- M6-D4-04：異常更新與自動結案

def _trim_report(conn, b1_qty):
    db.upsert(conn, "positions", {"ticker": "B1", "qty": b1_qty, "avg_cost": 100.0, "first_buy_date": "2026-09-01",
                                  "updated_at": db.now_iso()}, key=["ticker"])
    return C.portfolio_groups(conn, {"B1": 100.0, "B2": 100.0}, VOCAB)


def test_corr_group_trim_updates_then_resolves_when_loss_back_under_ten(env):
    conn, _ = env
    for t in ("B1", "B2"):
        _reg(conn, t, "HOLDING", grade="B", asset_class="銀行", external_drivers=["consumer_credit"])
    db.insert(conn, "positions", {"ticker": "B2", "qty": 150, "avg_cost": 100.0, "first_buy_date": "2026-09-01",
                                  "updated_at": db.now_iso()})
    rep = _trim_report(conn, 150)                       # 30% 金融 → 損失約 11.5% > 10%
    assert rep["trim_recommendations"]
    assert C.record_trim_anomalies(conn, rep, "2026-10-15") == len(rep["trim_recommendations"])
    t0 = rep["trim_recommendations"][0]["ticker"]
    rep2 = _trim_report(conn, 160)                      # 再加重：同檔未結 → 更新數字與日期
    assert C.record_trim_anomalies(conn, rep2, ASOF) == 0
    d = json.loads(conn.execute("SELECT detail FROM anomalies WHERE kind='corr_group_trim' AND ticker=? "
                                "AND status='open'", (t0,)).fetchone()["detail"])
    assert d["date"] == ASOF and d["first_date"] == "2026-10-15"
    new_sw = next(r["sell_weight"] for r in rep2["trim_recommendations"] if r["ticker"] == t0)
    assert d["sell_weight"] == new_sw
    rep3 = _trim_report(conn, 20)                       # 群損失降回 ≤10% → 自動結案
    assert not rep3["trim_recommendations"]
    C.record_trim_anomalies(conn, rep3, "2026-10-17")
    rows = conn.execute("SELECT status, resolution FROM anomalies WHERE kind='corr_group_trim'").fetchall()
    assert rows and all(r["status"] == "resolved" and "條件已消失" in r["resolution"] for r in rows)


def test_switch_candidate_updates_then_resolves(env):
    conn, _ = env
    full = f"持股已達 {S.MAX_HOLDINGS} 檔，不建新部位：需勝最弱持股（G8），留待人工"
    for i in range(S.MAX_HOLDINGS):
        db.insert(conn, "positions", {"ticker": f"H{i}", "qty": 1, "avg_cost": 10.0, "first_buy_date": "2026-09-01",
                                      "updated_at": db.now_iso()})
    Dly._switch_candidates(conn, [{"ticker": "NEW", "qty": 0, "reason": full, "decision_id": "NEW-1"}], "2026-10-15")
    Dly._switch_candidates(conn, [{"ticker": "NEW", "qty": 0, "reason": full, "decision_id": "NEW-2"}], ASOF)
    rows = conn.execute("SELECT status, detail FROM anomalies WHERE kind='switch_candidate'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "open"
    d = json.loads(rows[0]["detail"])
    assert d["decision_id"] == "NEW-2" and d["date"] == ASOF and d["first_date"] == "2026-10-15"
    Dly._switch_candidates(conn, [], "2026-10-17")      # 仍滿：維持未結
    assert conn.execute("SELECT status FROM anomalies WHERE kind='switch_candidate'").fetchone()["status"] == "open"
    conn.execute("DELETE FROM positions WHERE ticker='H0'")
    Dly._switch_candidates(conn, [], "2026-10-18")      # 持股不滿 → 條件已消失
    r = conn.execute("SELECT status, resolution FROM anomalies WHERE kind='switch_candidate'").fetchone()
    assert r["status"] == "resolved" and "條件已消失" in r["resolution"]
