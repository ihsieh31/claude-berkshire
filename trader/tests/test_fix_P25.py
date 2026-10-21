"""P25：持股賣出規則優先序（設計決策 D01）與論點證偽分流（D02）；M5_D2-01、M6-D4-03。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from test_engine import ASOF, RUN, full_a, full_b, make_env, thesis
from test_hold_review import _holding, hold
from trader import db
from trader.analysis import writeback as W
from trader.ledger import book
from trader.portfolio import sizing as S

ROOT = Path(__file__).resolve().parents[2]
KC = ["毛利率連兩季下滑", "前三大客戶流失"]
EV = [{"source": "10-Q 2026-08-01 MD&A", "date": "2026-08-01"}]


def falsified(ftype, cid="K1", evidence=EV, hit=True) -> dict:
    r = thesis("證偽")
    r["kill_criteria_status"] = [{"criterion": KC[0], "status": "已觸發" if hit else "接近", "evidence": "10-Q"},
                                 {"criterion": KC[1], "status": "未觸發", "evidence": "10-Q"}]
    r.update({"falsify_type": ftype, "condition_id": cid, "evidence": evidence})
    return r


# ---------------------------------------------------------------- D02：證偽核對（四種 falsify_type）

@pytest.mark.parametrize("ftype", ["CONDITION_TRIGGERED", "TOP_MGMT_INTEGRITY", "ACCOUNTING"])
def test_direct_falsify_types_pass_vetting(ftype):
    r = W.vet_falsification(falsified(ftype), KC, ASOF)
    assert r["verdict"] == "證偽" and "falsify_downgrade" not in r


def test_economics_deterioration_becomes_suspect():
    r = W.vet_falsification(falsified("ECONOMICS_DETERIORATION"), KC, ASOF)
    assert r["verdict"] == "疑似" and "判斷" in r["falsify_downgrade"]


@pytest.mark.parametrize("bad", [
    dict(cid="K9"),                                                        # condition_id 不在凍結論點清單
    dict(cid=None),
    dict(evidence=[]),                                                     # 沒有證據
    dict(evidence=[{"source": "10-Q", "date": ASOF}]),                     # 證據日期不早於決策日
    dict(evidence=[{"source": "", "date": "2026-08-01"}]),                 # 沒有來源
    dict(hit=False),                                                       # CONDITION_TRIGGERED 但該條未標已觸發
])
def test_failed_vetting_becomes_suspect(bad):
    r = W.vet_falsification(falsified("CONDITION_TRIGGERED", **bad), KC, ASOF)
    assert r["verdict"] == "疑似" and r["falsify_downgrade"]


def test_missing_falsify_type_becomes_suspect():
    r = W.vet_falsification(thesis("證偽"), KC, ASOF)
    assert r["verdict"] == "疑似" and "falsify_type" in r["falsify_downgrade"]


def _thesis_env(tmp_path, result, extra=None):
    env = make_env(tmp_path, {"THESIS": result, "HOLD_REVIEW": hold(), **(extra or {})})
    _holding(env)
    book.ensure_initial_deposit(env.conn, 1_000_000.0, "2024-01-01")   # 權重遠低於上限
    env.price_file("KO")
    env.add(env.job("KO", "THESIS", 1, params={"holding": True, "kill_criteria": KC}))
    return env


def test_verified_falsify_sells_all_without_full_analysis(tmp_path):
    """D02(a)＋M5_D2-01：核對通過 → SELL_ALL、不升完整分析；plan_sells 產生全賣。"""
    env = _thesis_env(tmp_path, falsified("CONDITION_TRIGGERED"))
    env.engine.run(RUN)
    assert [r["job_type"] for r in env.rows()] == ["THESIS"]           # 不升 DOSSIER／HOLD_REVIEW
    decs = S.load_decisions(env.conn, ASOF)
    assert [d["action"] for d in decs] == ["SELL_ALL"]
    assert decs[0]["log"]["falsify"]["verified"] is True
    plans = S.plan_sells(env.conn, ASOF, decs, {"KO": 60.0})
    assert [(p["action"], p["qty"]) for p in plans] == [("SELL_ALL", 100)]


def test_unverified_falsify_is_suspect_with_same_run_hold_review(tmp_path):
    """D02(b)：經濟特性惡化 → 疑似＋停止加碼；同 run 排 HOLD_REVIEW（依賴 THESIS）並升完整分析。"""
    env = _thesis_env(tmp_path, falsified("ECONOMICS_DETERIORATION"))
    env.engine.run(RUN)
    th = env.by_type("THESIS")[0]
    hr = env.by_type("HOLD_REVIEW")
    assert hr and json.loads(hr[0]["deps"]) == [th["job_id"]] and hr[0]["status"] == "done"
    assert json.loads(hr[0]["params"])["full_stage"] == "THESIS"
    assert env.by_type("DOSSIER")
    first = json.loads(env.conn.execute("SELECT data FROM decisions WHERE json_extract(data,'$.review_type')='THESIS'"
                                        ).fetchone()[0])
    assert first["action"] == "HOLD" and first["thesis_check"] == "疑似"
    assert first["falsify"]["verified"] is False and first["falsify"]["type"] == "ECONOMICS_DETERIORATION"
    assert S.add_blocked(env.conn, "KO", ASOF)                           # 停止加碼
    assert not any(d["action"] == "SELL_ALL" for d in S.load_decisions(env.conn, ASOF))


def test_unknown_condition_id_downgrades_in_writeback(tmp_path):
    """write_thesis_result 自己也核對（不只 ThesisHandler）：condition_id 不存在 → 疑似、HOLD。"""
    env = make_env(tmp_path, {})
    _holding(env)
    W.write_thesis_result(env.conn, env.paths, "KO", ASOF, RUN, falsified("ACCOUNTING", cid="K7"),
                          {"holding": True, "kill_criteria": KC})
    log = json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()[0])
    assert log["action"] == "HOLD" and log["thesis_check"] == "疑似" and "K7" in log["falsify"]["downgrade_reason"]


# ---------------------------------------------------------------- M5_D2-01：之後的結論不取消已寫入的全清

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    book.ensure_initial_deposit(c, 100000.0, "2024-01-01")
    yield c
    c.close()


def _pos(conn, t, qty, first, grade="B"):
    db.insert(conn, "positions", {"ticker": t, "qty": qty, "avg_cost": 50.0, "first_buy_date": first,
                                  "updated_at": db.now_iso()})
    db.upsert(conn, "registry", {"ticker": t, "status": "HOLDING", "data": {"grade": grade},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _it(did, action, review_type=None, **log):
    return {"decision_id": did, "ticker": "KO", "action": action, "review_type": review_type,
            "log": {"action": action, "review_type": review_type, **log}, "card": None}


def test_sell_all_survives_later_stop_and_hold(conn):
    _pos(conn, "KO", 100, "2024-01-02")
    decs = [_it("KO-1", "SELL_ALL", "THESIS"), _it("KO-2", "NO_ACTION", reason_code="MISSING_DATA"),
            _it("KO-3", "HOLD", "HOLD_REVIEW"), _it("KO-4", "TRIM", "HOLD_REVIEW", trim_fraction=0.5)]
    plans = S.plan_sells(conn, ASOF, decs, {"KO": 50.0})
    assert [(p["decision_id"], p["action"], p["qty"]) for p in plans] == [("KO-1", "SELL_ALL", 100)]


# ---------------------------------------------------------------- D01：權重

def test_rise_over_grade_cap_does_not_sell(conn):
    """B 級因上漲到 12%：不產生賣單，只記錄（anomaly weight_over_cap，不暫停）。"""
    _pos(conn, "KO", 272, "2024-01-02")                   # 272×50＝13600／(100000＋13600−0) ≈ 12%
    plans = S.plan_sells(conn, ASOF, [], {"KO": 50.0})
    assert len(plans) == 1 and plans[0]["qty"] == 0 and plans[0]["action"] == "HOLD"
    assert plans[0]["anomaly"]["kind"] == S.WEIGHT_OVER_CAP and plans[0]["anomaly"]["full_review"] is False
    S.record_weight_over_cap(conn, plans, ASOF)
    S.record_weight_over_cap(conn, plans, ASOF)           # 重跑不重複
    rows = conn.execute("SELECT ticker, pause_trading, status FROM anomalies WHERE kind=?", (S.WEIGHT_OVER_CAP,)).fetchall()
    assert [tuple(r) for r in rows] == [("KO", 0, "open")]
    assert "KO" not in book.paused(conn)
    S.record_weight_over_cap(conn, [], "2026-10-17")      # 不再超限 → 結案
    assert conn.execute("SELECT status FROM anomalies WHERE kind=?", (S.WEIGHT_OVER_CAP,)).fetchone()[0] == "resolved"


def test_rise_over_cap_blocks_add(conn):
    """因上漲超過等級上限：ADD 停止加碼（即使還在 15% 以下）。"""
    _pos(conn, "KO", 272, "2024-01-02")
    gates = {"gate1_not_deleted": True, "gate2_price_below_cap": True, "gate3_pass": True}
    card = {"certainty_grade": "B", "target_weight_pct": 10, "auto_buy_cap": 60.0}
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING",
                                 "data": {"grade": "B", "auto_buy_cap": 60.0}, "updated_at": db.now_iso()}, key=["ticker"])
    it = {"decision_id": "KO-9", "ticker": "KO", "action": "ADD", "review_type": "PRICE",
          "log": {**gates, "action": "ADD"}, "card": card}
    plans = S.plan_buys(conn, ASOF, [it], {"KO": 50.0}, {"KO": 1e12}, 10)
    assert plans[0]["qty"] == 0 and "等級上限" in plans[0]["reason"]


def test_user_hard_cap_trims_only_excess_even_in_first_year(conn):
    """制度類（config account.user_hard_cap）：只減超出部分，reason_code INSTITUTIONAL，不受一年限制。"""
    _pos(conn, "KO", 400, "2026-09-01")                   # 20000／120000 ≈ 16.7%
    plans = S.plan_sells(conn, ASOF, [], {"KO": 50.0}, user_hard_cap=0.10)
    assert [(p["action"], p["qty"], p.get("reason_code")) for p in plans] == [("TRIM", 160, "INSTITUTIONAL")]
    assert S.plan_sells(conn, ASOF, [], {"KO": 50.0}, user_hard_cap=0.20)[0]["qty"] == 0   # 未超硬上限：只記錄


def test_first_year_absurd_trim_becomes_hold_and_stop_adding(tmp_path):
    """第一年內荒謬級：HOLD_REVIEW 的估值 TRIM 改 HOLD＋停止加碼，不產生賣單。"""
    env = make_env(tmp_path, {})
    _holding(env, first="2026-06-01")
    W.write_hold_review_result(env.conn, env.paths, "KO", ASOF, RUN, hold("TRIM", "估值", 0.5), {},
                               price=200.0, iv_base=50.0)
    log = json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()[0])
    assert log["action"] == "HOLD" and log["stop_adding"] is True
    assert S.add_blocked(env.conn, "KO", ASOF)
    decs = S.load_decisions(env.conn, ASOF)
    assert [p for p in S.plan_sells(env.conn, ASOF, decs, {"KO": 200.0}) if p["qty"] > 0] == []


# ---------------------------------------------------------------- M6-D4-03：權重 > 25% → FULL（review_due，邊緣觸發）

def _review_due():
    sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
    try:
        import review_due
    finally:
        sys.path.pop(0)
    return review_due


@pytest.mark.parametrize("w,w0,expect", [(0.26, None, "FULL"), (0.26, 0.20, "FULL"), (0.26, 0.27, "NONE"),
                                         (0.24, None, "NONE")])
def test_review_due_weight_over_25pct_full(w, w0, expect):
    e = {"ticker": "KO", "status": "HOLDING", "last_review_date": "2026-10-01", "last_full_review_date": "2026-06-01",
         "weight": w}
    if w0 is not None:
        e["full_review_weight"] = w0
    act, why, _ = _review_due().check(e, [], None, ASOF, None)
    assert act == expect, why
    if expect == "FULL":
        assert any("P-43" in x for x in why)


def test_full_review_stamps_weight(tmp_path):
    """持股完整分析回寫時記 full_review_weight（review_due 邊緣觸發的基準）。持股 FULL_A 違規停止退回後仍停止
    不算完成年度複查、不記（A3-M3B-01，見 test_fix_F3_DP），這裡改走 FULL_B 非 BUY 的完整路徑。"""
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": full_b("WATCH"), "HOLD_REVIEW": hold()})
    _holding(env)
    S.merge_registry(env.conn, "KO", {"weight": 0.3}, ASOF)
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    data = json.loads(env.conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()[0])
    assert data["full_review_weight"] == 0.3


# ---------------------------------------------------------------- 設定 user_hard_cap

def test_config_user_hard_cap(tmp_path, monkeypatch):
    from trader import config as C
    monkeypatch.setattr(C, "PROJECT_DIR", tmp_path)
    base = {"models": {t: {"backend": "fake", "model": "fake"} for t in ("strong", "medium", "weak")}}

    def load(acct):
        p = tmp_path / "c.yaml"
        p.write_text(yaml.safe_dump({**base, "account": acct}), encoding="utf-8")
        return C.load_config(p, env_file=tmp_path / "none.env")
    assert load({}).account.user_hard_cap is None
    assert load({"user_hard_cap": 0.2}).account.user_hard_cap == 0.2
    assert load({"user_hard_cap": None}).account.user_hard_cap is None
    for bad in (1.5, 0, "20%"):
        with pytest.raises(C.ConfigError):
            load({"user_hard_cap": bad})


# ---------------------------------------------------------------- P25R：criterion 帶 K 編號前綴、誠信／會計 condition_id 可為 null

@pytest.mark.parametrize("prefix", ["K1：", "K1:", "K1： ", "K1 : "])
def test_condition_triggered_criterion_with_k_prefix_passes(prefix):
    """模型照抄提示清單「K1：原文」當 criterion：比對先去 K 編號前綴，CONDITION_TRIGGERED 仍通過。"""
    r = falsified("CONDITION_TRIGGERED")
    r["kill_criteria_status"][0]["criterion"] = prefix + KC[0]
    v = W.vet_falsification(r, KC, ASOF)
    assert v["verdict"] == "證偽" and "falsify_downgrade" not in v


def test_thesis_kill_due_matches_criterion_with_k_prefix():
    """_thesis_kill_due 也用同一正規化：criterion 帶「K2：」仍更新 checked_on／status／due。"""
    cur = [{"text": KC[0], "due": "2026-10-01"}, {"text": KC[1], "due": "2026-10-01"}]
    result = {"kill_criteria_status": [
        {"criterion": "K1：" + KC[0], "status": "接近", "next_due": "2027-02-15"},
        {"criterion": "K2: " + KC[1], "status": "未觸發", "next_due": None}]}
    notes: list[str] = []
    out = W._thesis_kill_due(cur, result, ASOF, notes)
    assert out == [{"text": KC[0], "due": "2027-02-15", "checked_on": ASOF, "status": "接近"},
                   {"text": KC[1], "due": "2026-10-01", "checked_on": ASOF, "status": "未觸發"}]


@pytest.mark.parametrize("ftype", ["TOP_MGMT_INTEGRITY", "ACCOUNTING"])
@pytest.mark.parametrize("cid", [None, ""])
def test_integrity_accounting_null_condition_id_passes_with_evidence(ftype, cid):
    """D02 補充：誠信／會計類不要求對應失效條件，condition_id 可為 null，改以 evidence 核對。"""
    v = W.vet_falsification(falsified(ftype, cid=cid, hit=False), KC, ASOF)
    assert v["verdict"] == "證偽" and "falsify_downgrade" not in v


@pytest.mark.parametrize("ftype", ["TOP_MGMT_INTEGRITY", "ACCOUNTING"])
@pytest.mark.parametrize("ev", [
    [],
    [{"source": "8-K Item 4.02", "date": None}],          # 無日期
    [{"source": "8-K Item 4.02", "date": ASOF}],          # 不早於決策日
    [{"source": "", "date": "2026-08-01"}],               # 無來源
])
def test_integrity_accounting_null_id_without_dated_evidence_is_suspect(ftype, ev):
    v = W.vet_falsification(falsified(ftype, cid=None, evidence=ev), KC, ASOF)
    assert v["verdict"] == "疑似" and "evidence" in v["falsify_downgrade"]


@pytest.mark.parametrize("ftype", ["TOP_MGMT_INTEGRITY", "ACCOUNTING", "CONDITION_TRIGGERED"])
def test_condition_id_given_but_not_in_list_is_suspect(ftype):
    v = W.vet_falsification(falsified(ftype, cid="K9"), KC, ASOF)
    assert v["verdict"] == "疑似" and "K9" in v["falsify_downgrade"]


def test_condition_triggered_still_requires_condition_id():
    v = W.vet_falsification(falsified("CONDITION_TRIGGERED", cid=None), KC, ASOF)
    assert v["verdict"] == "疑似" and "condition_id" in v["falsify_downgrade"]


def test_prompt_and_schema_document_rules():
    md = (ROOT / "routing/prompts/THESIS.md").read_text(encoding="utf-8")
    assert "不含 K 編號" in md and "可為 null" in md
    sch = json.loads((ROOT / "routing/output_schemas.json").read_text(encoding="utf-8"))["THESIS"]
    assert "可為 null" in sch["_falsify"] and not any(k in sch for k in ("falsify_type", "condition_id", "evidence"))
