"""P24：PRICE 動作權與估值類減碼（設計決策 D28、D03 非撮合部分；M5_D7_02〔＝M3_D7-01〕、M5_D2-07、M5_D2-08、M5_D2-10、
M7-D1-03 的 PRICE 建倉日部分）。

- D28：PRICE 的動作由框架依三閘門與賣出規則算出，proposed_action 只記日誌；G10 不再成立或 G9 空白 → 完整分析。
- D03：PRICE 不寫 TRIM，荒謬級／≥ IV_base 只交 HOLD_REVIEW；荒謬級回到線下前只減一次；3c 累計保留 ≥ 峰值 2/3。
- M5_D2-07：第一年估值類賣出由覆核擋下；P-25（只因 IV_base 下修）只停止加碼。
- M5_D2-08：FULL_A 停止（未走到估值）不取、不給 cap_result.json。
- M5_D2-10／M7-D1-03：建倉日帳本優先、登錄表欄位與 data 次之；都缺視為未滿一年／兩年。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import test_review_jobs as TR
from test_engine import RUN, full_a, make_env
from test_hold_review import _holding, hold

from trader import db
from trader.analysis import writeback as W
from trader.analysis.checks import run_cap_calc
from trader.jobs import validate as V
from trader.jobs.hold_jobs import _input_files, _last_trim_text, _position_text, check, valuation_numbers
from trader.paths import get_paths
from trader.portfolio import sizing as S

ASOF = TR.ASOF


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield paths, conn
    conn.close()


def _cap(extra: dict | None = None) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "c.json"
        f.write_text(json.dumps({**TR.CAP_INPUT, **(extra or {})}), encoding="utf-8")
        return run_cap_calc(f)


def _iv() -> float:
    return float(_cap()["IV_base"])


def _days_ago(n: int) -> str:
    return (date.fromisoformat(ASOF) - timedelta(days=n)).isoformat()


def _types(nxt) -> list[str]:
    return [j.job_type for j in nxt]


def _actions(conn) -> list[str]:
    return [d["data"]["action"] for d in TR._decisions(conn)]


def _set_absurd_line(paths, line: float) -> None:
    """cap_inputs 的 norm_eps 調成讓荒謬級線（50×E）＝line。"""
    TR._write_cap_input(paths, extra={"norm_eps": line / 50})


# ---------------------------------------------------------------- M5_D7_02／D03：PRICE 的 SELL_HALF 不下單

def test_price_sell_half_below_absurd_line_is_hold_without_trim(env):
    """復查重現 r2：現價 60.5 遠低於荒謬級線，模型回 SELL_HALF → 修前記 TRIM（次日賣半）；修後 HOLD、不送 HOLD_REVIEW。"""
    paths, conn = env
    TR._write_cap_input(paths)
    TR._seed_registry(conn, "KO", "HOLDING", {"first_buy_date": "2020-01-01", "weight": 0.05})
    result = TR._price_result(proposed_action="SELL_HALF")
    _, nxt = TR._run_price(paths, conn, TR._price_params(), result)
    assert TR.PRICE_NOW < _iv()   # 夾具前提：現價低於 IV_base 與荒謬級線
    assert _actions(conn) == ["HOLD"] and nxt == []
    assert result["model_proposed_action"] == "SELL_HALF" and result["framework_action"] == "HOLD"
    assert any("只記日誌" in n for n in TR._decisions(conn)[-1]["data"]["framework_notes"])
    assert S.plan_sells(conn, ASOF, S.load_decisions(conn, ASOF), {"KO": TR.PRICE_NOW}) == []


@pytest.mark.parametrize("proposed", ["SELL_HALF", "NO_CHANGE"])
def test_price_at_absurd_line_goes_to_hold_review_not_trim(env, proposed):
    """現價 ≥ 荒謬級線、滿一年：不論模型怎麼提，PRICE 只記 HOLD 並交 HOLD_REVIEW（D03：只有 HOLD_REVIEW 能決定 TRIM）。"""
    paths, conn = env
    _set_absurd_line(paths, 50.0)
    TR._seed_registry(conn, "KO", "HOLDING", {"first_buy_date": "2020-01-01"})
    _, nxt = TR._run_price(paths, conn, TR._price_params(), TR._price_result(proposed_action=proposed))
    assert _actions(conn) == ["HOLD"]
    assert _types(nxt) == ["HOLD_REVIEW"] and nxt[0].params["full_stage"] == "PRICE"
    assert any("荒謬級" in n for n in TR._decisions(conn)[-1]["data"]["framework_notes"])


def test_price_absurd_in_first_year_still_reviews_but_never_trims(env):
    """D01(2)：第一年內荒謬級 → 停止加碼＋完整複查（HOLD_REVIEW），不機械減半。"""
    paths, conn = env
    _set_absurd_line(paths, 50.0)
    TR._seed_registry(conn, "KO", "HOLDING", {"first_buy_date": _days_ago(100)})
    _, nxt = TR._run_price(paths, conn, TR._price_params(), TR._price_result(proposed_action="SELL_HALF"))
    assert _actions(conn) == ["HOLD"] and _types(nxt) == ["HOLD_REVIEW"]


def test_write_price_result_rejects_trim(env):
    paths, conn = env
    with pytest.raises(W.WritebackError):
        W.write_price_result(conn, paths, "KO", ASOF, "r", action="TRIM", gate1=True, gate2="通過", gate3="通過",
                             cap={}, price=1.0, price_date=None, t30=None, reason="", notes=[])


# ---------------------------------------------------------------- D28：動作由框架算、只採信 needs_full／G9／G10

def test_price_framework_action_ignores_model_proposal(env):
    """觀察名單三閘門全過：模型回 NO_CHANGE 也照框架算出 BUY 候選 → 完整分析；三閘門不過時模型 BUY_CANDIDATE 不採信。"""
    paths, conn = env
    TR._write_cap_input(paths)
    TR._seed_registry(conn, "KO", "WATCH", {"grade": "B"})
    TR._gate3_row(conn)
    cap = TR._auto_cap()
    _, nxt = TR._run_price(paths, conn, TR._price_params(price=cap * 0.8), TR._price_result(proposed_action="NO_CHANGE"))
    assert _types(nxt) == ["DOSSIER"]


def test_price_holding_add_needs_known_weights(env):
    paths, conn = env
    TR._write_cap_input(paths)
    TR._seed_registry(conn, "KO", "HOLDING", {"first_buy_date": "2020-01-01"})   # 權重、目標權重缺
    TR._gate3_row(conn)
    TR._run_price(paths, conn, TR._price_params(price=TR._auto_cap() * 0.8),
                  TR._price_result(proposed_action="BUY_CANDIDATE"))
    assert _actions(conn)[-1] == "HOLD"


@pytest.mark.parametrize("kw", [{"g10_still_valid": False}, {"g9_contrarian": "  "}])
def test_price_g10_invalid_or_g9_blank_goes_full(env, kw):
    paths, conn = env
    TR._write_cap_input(paths)
    TR._seed_registry(conn, "KO", "WATCH", {})
    before = len(TR._decisions(conn))
    result = TR._price_result(**{"g9_contrarian": "反方：利率續升", "g10_still_valid": True, **kw})
    _, nxt = TR._run_price(paths, conn, TR._price_params(), result)
    assert _types(nxt) == ["DOSSIER"] and len(TR._decisions(conn)) == before
    assert "P-63" in result["framework_g9g10"]


def test_price_schema_proposed_action_optional_g9_g10_required():
    schemas = V.load_schemas(get_paths().schemas_json)
    ok = {"ticker": "KO", "price": None, "auto_buy_cap": None, "sell_review_above": None, "absurd_price_line": None,
          "gates": {"gate1_screen": None, "gate2_price_lt_cap": None, "gate3_on_file": None},
          "needs_full": False, "g9_contrarian": "反方：估值已高", "g10_still_valid": True, "reason": "測試"}
    assert V.check("PRICE", ok, schemas)[1] == []
    errs = V.check("PRICE", {k: v for k, v in ok.items() if k != "g10_still_valid"}, schemas)[1]
    assert any("g10_still_valid" in e for e in errs)


# ---------------------------------------------------------------- M5_D2-07：第一年與 P-25

def test_price_above_iv_first_year_only_stops_adding(env):
    paths, conn = env
    TR._write_cap_input(paths)
    TR._seed_registry(conn, "KO", "HOLDING", {"first_buy_date": _days_ago(200)})
    _, nxt = TR._run_price(paths, conn, TR._price_params(price=_iv() * 1.05), TR._price_result())
    assert nxt == [] and _actions(conn) == ["HOLD"]
    assert any("未滿一年" in n for n in TR._decisions(conn)[-1]["data"]["framework_notes"])


def test_price_above_iv_only_because_iv_fell_is_p25(env):
    """P-25：現價 ≥ 新 IV_base 但 < 上次 sell_review_above（只因長債使 IV_base 下修）→ 只停止加碼，不送賣出檢視。"""
    paths, conn = env
    TR._write_cap_input(paths)
    iv = _iv()
    TR._seed_registry(conn, "KO", "HOLDING", {"first_buy_date": "2020-01-01", "sell_review_above": iv * 1.2})
    _, nxt = TR._run_price(paths, conn, TR._price_params(price=iv * 1.05), TR._price_result())
    assert nxt == [] and _actions(conn) == ["HOLD"]
    assert any("P-25" in n for n in TR._decisions(conn)[-1]["data"]["framework_notes"])


def test_check_first_year_valuation_sell_rejected():
    """復查情境：建倉 200 日、IV_base 58、現價 60，HOLD_REVIEW 回估值類 SELL_ALL（具名）→ 覆核錯誤。"""
    errs, human = check(hold("SELL_ALL", "估值", named="IV 下修"), iv_base=58.0, price=60.0, held_days=200)
    assert any("未滿一年" in e for e in errs) and not human
    assert not any("未滿一年" in e for e in check(hold("SELL_ALL", "估值＋業務", named="假設 A 被否定"),
                                                  iv_base=58.0, price=60.0, held_days=200)[0])   # 3a 屬業務類（D01）
    assert check(hold("TRIM", "估值", 0.25), iv_base=50.0, price=60.0, held_days=1000) == ([], False)


# ---------------------------------------------------------------- M5_D2-10／M7-D1-03：建倉日來源

def test_check_unknown_held_days_fails_closed():
    errs, _ = check(hold("SELL_ALL", "業務"), iv_base=None, price=None, held_days=None)
    assert any("Q5" in e for e in errs)
    assert any("未滿一年" in e for e in check(hold("SELL_ALL", "估值", named="x"), iv_base=50.0, price=60.0,
                                                held_days=None)[0])


def test_first_buy_date_ledger_then_registry_column_then_data(env):
    paths, conn = env
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "first_buy_date": "2021-01-04",
                                 "data": {"first_buy_date": "2022-02-01"}, "updated_at": db.now_iso()}, ["ticker"])
    assert W.first_buy_date(conn, "KO") == "2021-01-04"          # broker 只寫的登錄表欄位（M7-D1-03）
    db.insert(conn, "positions", {"ticker": "KO", "qty": 10, "avg_cost": 50.0, "first_buy_date": None,
                                  "updated_at": db.now_iso()})
    text, days = _position_text(conn, "KO", ASOF)
    assert days == (date.fromisoformat(ASOF) - date(2021, 1, 4)).days and "2021-01-04" in text   # 帳本缺 → 登錄表
    conn.execute("UPDATE positions SET first_buy_date='2023-03-01'")
    conn.commit()
    assert W.first_buy_date(conn, "KO") == "2023-03-01"          # 帳本優先


def test_price_uses_registry_column_first_buy_date(env):
    """M7-D1-03：PRICE 讀登錄表欄位的建倉日（broker 寫的）→ 滿一年、現價 ≥ IV_base → 交 HOLD_REVIEW；都缺 → 視為未滿一年。"""
    paths, conn = env
    TR._write_cap_input(paths)
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "first_buy_date": "2020-01-02", "data": {},
                                 "updated_at": db.now_iso()}, ["ticker"])
    _, nxt = TR._run_price(paths, conn, TR._price_params(price=_iv() * 1.05), TR._price_result())
    assert _types(nxt) == ["HOLD_REVIEW"]
    conn.execute("UPDATE registry SET first_buy_date = NULL")
    conn.commit()
    _, nxt = TR._run_price(paths, conn, TR._price_params(price=_iv() * 1.05), TR._price_result())
    assert nxt == []


# ---------------------------------------------------------------- M5_D2-08：FULL_A 停止不取 cap_result

def test_stage_a_ignores_and_does_not_stage_cap_result(tmp_path):
    (tmp_path / "cap_result.json").write_text(json.dumps({"IV_base": 50.0}), encoding="utf-8")
    assert valuation_numbers(tmp_path, {"full_stage": "A", "price": 60.0}) == (None, 60.0)
    assert "cap_result.json" not in _input_files(tmp_path, {"full_stage": "A"})
    assert "cap_result.json" in _input_files(tmp_path, {"full_stage": "B"})
    assert valuation_numbers(tmp_path, {"full_stage": "B", "price": 60.0}) == (50.0, 60.0)


def test_full_a_stop_valuation_sell_with_price_cap_result_becomes_hold(tmp_path):
    """td 有同 run PRICE 留下的 cap_result.json、FULL_A 停止、模型回估值類 SELL_ALL → 覆核錯誤（未走到估值）→ HOLD。"""
    env = make_env(tmp_path, {"FULL_A": full_a(stopped=True), "HOLD_REVIEW": hold("SELL_ALL", "估值")})
    _holding(env)
    env.price_file("KO")
    (env.paths.work / RUN / "KO").mkdir(parents=True, exist_ok=True)
    (env.paths.work / RUN / "KO" / "cap_result.json").write_text(json.dumps({"IV_base": 50.0}), encoding="utf-8")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    log = json.loads(env.conn.execute("SELECT data FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()["data"])
    assert log["action"] == "HOLD" and "未走到估值" in log["framework_notes"][0]


# ---------------------------------------------------------------- D03 回寫：荒謬級只減一次、3c 累計保留 2/3

Q3C = [{"q": "Q3c", "answer": "是", "evidence": "頂級複利型"}]


def _seed(conn, qty=100, absurd=260.0, last_trim=None):
    db.insert(conn, "positions", {"ticker": "KO", "qty": qty, "avg_cost": 50.0, "first_buy_date": "2024-01-02",
                                  "updated_at": db.now_iso()})
    data = {"first_buy_date": "2024-01-02", "absurd_price_line": absurd, "grade": "B"}
    if last_trim:
        data["last_trim"] = last_trim
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "data": data, "updated_at": db.now_iso()},
              ["ticker"])


def seed_p08(conn, quality="優"):
    """A3-M3D-02（F3-DP）：Q3c 頂級複利型須最近一次完整分析訊號卡 p08_final.quality＝優。已有 KO 訊號卡 → 全部補上；
    沒有 → 補一筆 2026-10-01 的完整分析卡。"""
    p08 = json.dumps({"items_with_data": 5, "passed": 4, "rate": 80.0, "quality": quality})
    if conn.execute("SELECT 1 FROM signals WHERE ticker='KO'").fetchone():
        conn.execute("UPDATE signals SET data=json_set(data, '$.p08_final', json(?)) WHERE ticker='KO'", (p08,))
        conn.commit()
        return
    db.insert(conn, "decisions", {"decision_id": "KO-20261001-1", "ticker": "KO", "decision_date": "2026-10-01",
                                  "run_id": "x", "job_id": None, "signal": "WATCH", "reason_code": "TOO_HARD",
                                  "data": {"action": "WATCH"}, "created_at": db.now_iso()})
    db.insert(conn, "signals", {"decision_id": "KO-20261001-1", "ticker": "KO", "signal": "WATCH",
                                "data": {"p08_final": json.loads(p08)}, "created_at": db.now_iso()})


def _hr(frac, path=()):
    return {"ticker": "KO", "decision_date": ASOF, "path": list(path), "action": "TRIM", "sell_category": "估值",
            "trim_fraction": frac, "stop_adding": True, "observe_until": None, "falsified_assumption": None,
            "external_drivers": [], "external_drivers_change_reason": None, "reason": "測試"}


def _log(conn, did):
    return json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()["data"])


def _reg_data(conn):
    return json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])


def test_absurd_trim_only_once_until_back_below_line(env):
    """連兩天荒謬級只減一次；中間有低於線的價格紀錄後才可再減。"""
    paths, conn = env
    d1 = "2026-10-15"
    _seed(conn, last_trim={"date": d1, "reason_code": "ABSURD", "line": 260.0, "fraction": 0.5, "shares": 50,
                           "peak_shares": 100})
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _hr(0.5), price=300.0, iv_base=60.0)
    log = _log(conn, did)
    assert log["action"] == "HOLD" and log["stop_adding"] is True
    assert any("只減一次" in n for n in log["framework_notes"])
    assert _reg_data(conn)["last_trim"]["date"] == d1   # 沒有新減碼紀錄
    # 之後有一筆價格低於線的決策紀錄（回到線下）→ 再站上線可再減
    db.insert(conn, "decisions", {"decision_id": "KO-20261016-9", "ticker": "KO", "decision_date": ASOF, "run_id": "x",
                                  "job_id": None, "signal": "HOLD", "reason_code": None,
                                  "data": {"action": "HOLD", "review_type": "PRICE", "price": 250.0},
                                  "created_at": db.now_iso()})
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r2", _hr(0.5), price=300.0, iv_base=60.0)
    assert _log(conn, did)["action"] == "TRIM" and _reg_data(conn)["last_trim"]["date"] == ASOF


def test_absurd_trim_from_previous_holding_period_does_not_block(env):
    paths, conn = env
    _seed(conn, last_trim={"date": "2023-05-01", "reason_code": "ABSURD", "line": 260.0, "shares": 50})
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _hr(0.5), price=300.0, iv_base=60.0)
    assert _log(conn, did)["action"] == "TRIM"


def test_3c_cumulative_keeps_two_thirds_of_peak(env):
    paths, conn = env
    # 第一次：100 股（峰值 100）→ 至多 1/4，記 peak_shares
    _seed(conn, qty=100)
    seed_p08(conn)   # A3-M3D-02：頂級複利型須 P-08 品質優
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", _hr(0.25, Q3C), price=80.0, iv_base=60.0)
    assert _log(conn, did)["trim_fraction"] == 0.25 and _reg_data(conn)["last_trim"]["peak_shares"] == 100
    # 已減到 75 股：保留 ≥ 66.67 股 → 本次至多 1 − 66.67/75 ≈ 0.111
    conn.execute("UPDATE positions SET qty = 75")
    conn.commit()
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r2", _hr(0.25, Q3C), price=80.0, iv_base=60.0)
    log = _log(conn, did)
    assert log["action"] == "TRIM" and log["trim_fraction"] == pytest.approx(1 - (2 / 3) * 100 / 75)
    assert any("2/3" in n for n in log["framework_notes"])
    # 已在 2/3 以下：不再減
    conn.execute("UPDATE positions SET qty = 66")
    conn.commit()
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r3", _hr(0.25, Q3C), price=80.0, iv_base=60.0)
    assert _log(conn, did)["action"] == "HOLD"


def test_hold_review_input_carries_last_trim(env):
    paths, conn = env
    _seed(conn, last_trim={"date": "2026-10-01", "reason_code": "ABSURD", "line": 260.0, "shares": 50})
    assert '"ABSURD"' in _last_trim_text(conn, "KO")
    conn.execute("DELETE FROM positions")
    conn.execute("UPDATE registry SET data = ?", (json.dumps({"first_buy_date": "2024-01-02"}),))
    assert "無" in _last_trim_text(conn, "KO")
    tmpl = (get_paths().root / "routing" / "prompts" / "HOLD_REVIEW.md").read_text(encoding="utf-8")
    assert "{last_trim}" in tmpl
