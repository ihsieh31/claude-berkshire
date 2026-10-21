"""RM6-3：M6 複審 S5 三條。全部 FakeRunner／本地檔，不連網。

1. M6-D5-08（PARTIAL）：完整分析鏈交 HOLD_REVIEW，日誌 price_date 取 params（null）。修：valuation_numbers(with_date=True)
   由同一價格檔回傳價格日，post／on_invalid 以 price_date= 傳給 write_hold_review_result（優先於 params）。
2. M6-RS5-01：非持股停止回寫帶入 external_drivers_change_reason（_STOP_CARRY_KEYS）。
3. M6-RS5-02：新增追溯欄以 "_framework_fields" 登錄於 decision_log_schema／signal_card_schema，標「框架填，模型不填」。
"""
from __future__ import annotations

import json

import pytest

from trader import db
from trader.analysis import writeback as W
from trader.jobs import deep
from trader.jobs.hold_jobs import HoldReviewHandler, valuation_numbers
from trader.ledger import book
from trader.paths import get_paths

from test_engine import ASOF, make_env
from test_hold_review import _holding, _last_log, hold


def _parent(env, job_type="FULL_B", n=1, extra=None):
    params = {"holding": True, "screen_context": {"screen_decision": "PASS"}, "reasons": ["持股定期完整複查"],
              **(extra or {})}
    return env.job("KO", job_type, n, params=params)


def _hold_job(env, parent, specs):
    assert len(specs) == 1 and specs[0].job_type == "HOLD_REVIEW"
    hr = specs[0]
    assert "price_date" not in hr.params and "price" not in hr.params
    return hr


def test_full_b_chain_hold_review_log_has_price_date_from_price_file(tmp_path):
    env = make_env(tmp_path, {})
    _holding(env)
    env.price_file("KO")
    ctx = env.engine.ctx
    parent = _parent(env)
    hr = _hold_job(env, parent, deep._hold_review_after(parent, ctx, "WATCH"))
    assert hr.params["full_stage"] == "B"
    HoldReviewHandler().post(hr, hold("HOLD"), ctx)
    log = _last_log(env)
    assert log["review_type"] == "HOLD_REVIEW"
    assert log["price"] == 60.5 and log["price_date"] == "2026-10-15"


def test_full_a_chain_hold_review_log_has_price_date(tmp_path):
    env = make_env(tmp_path, {})
    _holding(env)
    env.price_file("KO")
    ctx = env.engine.ctx
    parent = _parent(env, "FULL_A")
    params = deep._carry(parent, {"full_stage": "A", "stopped_at_gate": "G3", "reason_code": "TOO_HARD"})
    hr = _hold_job(env, parent, [deep.next_job(parent, "HOLD_REVIEW", ctx, params)])
    HoldReviewHandler().post(hr, hold("HOLD"), ctx)
    log = _last_log(env)
    assert log["price"] == 60.5 and log["price_date"] == "2026-10-15"


def test_fail_chain_on_invalid_log_has_price_date(tmp_path):
    env = make_env(tmp_path, {})
    _holding(env)
    env.price_file("KO")
    ctx = env.engine.ctx
    parent = _parent(env)
    hr = _hold_job(env, parent, deep._hold_after_fail(parent, ctx, "FULL_B 輸出不合格", "G7"))
    assert hr.params["full_stage"] == "FAIL"
    HoldReviewHandler().on_invalid(hr, ["缺欄位 action"], ctx)
    log = _last_log(env)
    assert log["action"] == "HOLD" and log["price"] == 60.5 and log["price_date"] == "2026-10-15"


def test_valuation_numbers_price_date_same_source(tmp_path):
    td = tmp_path / "td"
    (td / "dossier").mkdir(parents=True)
    assert valuation_numbers(td, {"price": 50.0}) == (None, 50.0)
    assert valuation_numbers(td, {"price": 50.0, "price_date": "2026-10-14"}, with_date=True) == \
        (None, 50.0, "2026-10-14")
    assert valuation_numbers(td, {}, with_date=True) == (None, None, None)
    pf = td / "dossier" / "KO_price.json"
    pf.write_text(json.dumps({"price": 60.5, "price_date": "2026-10-15"}), encoding="utf-8")
    assert valuation_numbers(td, {"price": 50.0, "price_date": "2026-10-14"}, with_date=True) == \
        (None, 60.5, "2026-10-15")
    pf.write_text(json.dumps({"price": 60.5, "price_meta": {"price_date": "2026-10-13"}}), encoding="utf-8")
    assert valuation_numbers(td, {}, with_date=True) == (None, 60.5, "2026-10-13")
    pf.write_text(json.dumps({"price": 60.5}), encoding="utf-8")
    # 價格取自價格檔但檔內無價格日：不配工作參數的日期（可能不同日；check-RM6-3）
    assert valuation_numbers(td, {"price_date": "2026-10-12"}, with_date=True) == (None, 60.5, None)
    assert valuation_numbers(td, {}, with_date=True) == (None, 60.5, None)


def test_write_hold_review_result_price_date_param_overrides_params(tmp_path):
    env = make_env(tmp_path, {})
    _holding(env)
    W.write_hold_review_result(env.conn, env.paths, "KO", ASOF, "r1", hold("HOLD"),
                               {"full_stage": "PRICE", "price_date": "2026-10-01"}, price=60.0,
                               price_date="2026-10-15")
    assert _last_log(env)["price_date"] == "2026-10-15"
    W.write_hold_review_result(env.conn, env.paths, "KO", ASOF, "r1", hold("HOLD"),
                               {"full_stage": "PRICE", "price_date": "2026-10-01"}, price=60.0)
    assert _last_log(env)["price_date"] == "2026-10-01"


@pytest.fixture
def wenv(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100000.0, "2026-10-01")
    yield conn, paths
    conn.close()


def test_stop_keeps_drivers_change_reason_for_non_holding(wenv):
    conn, paths = wenv
    why = "2026-09-30 8-K：出售油氣部門"
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "WATCH", "data": {
        "cik": "123", "external_drivers": ["oil"], "external_drivers_determined_on": "2026-09-01",
        "external_drivers_basis": [{"driver": "oil"}], "external_drivers_change_reason": why,
        "thesis_file": "theses/AAA_2026-09-01.md"}, "updated_at": db.now_iso()}, key=["ticker"])
    W.write_stop_result(conn, paths, "AAA", ASOF, "r", {"reason_code": "TOO_HARD", "stopped_at_gate": "G1"})
    data = json.loads(conn.execute("SELECT data FROM registry WHERE ticker='AAA'").fetchone()["data"])
    assert data["external_drivers"] == ["oil"]
    assert data["external_drivers_change_reason"] == why
    assert "external_drivers_change_reason" in W._STOP_CARRY_KEYS


LOG_KEYS = ("auto_buy_cap", "iv_base", "long_rate", "gate3_source", "weight", "trigger", "grade_after",
            "source_job_id", "hold_flags")
CARD_KEYS = ("carried_from_decision_id", "gate1_warning")


def _schema(name):
    return json.loads((get_paths().skill_deep / "assets" / name).read_text(encoding="utf-8"))


def test_decision_log_schema_registers_framework_fields():
    s = _schema("decision_log_schema.json")
    ff = s.get("_framework_fields")
    assert isinstance(ff, dict)
    assert "框架填，模型不填" in ff.get("_說明", "")
    for k in LOG_KEYS:
        assert k in ff and isinstance(ff[k], str) and ff[k], k
        assert k not in s["fields"], k
    txt = json.dumps(s, ensure_ascii=False)
    assert "price_to_cap＝現價÷auto_buy_cap" in txt and "buy_price_cap＝P2′" in txt


def test_signal_card_schema_registers_framework_fields_not_blank_card_keys():
    s = _schema("signal_card_schema.json")
    ff = s.get("_framework_fields")
    assert isinstance(ff, dict) and "框架填，模型不填" in ff.get("_說明", "")
    for k in CARD_KEYS:
        assert k in ff and isinstance(ff[k], str) and ff[k], k
    blank = W._blank_from_fields(s)
    assert "_framework_fields" not in blank and not any(k in blank for k in CARD_KEYS)


def test_missing_rates_field_list_unchanged():
    s = _schema("decision_log_schema.json")
    fields = [k for k in s.get("fields", {}) if not k.startswith("_")]
    assert not set(fields) & set(LOG_KEYS)
