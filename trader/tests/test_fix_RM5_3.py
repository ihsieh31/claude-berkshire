"""RM5-3：review_jobs／writeback 群。

- M5-RS7-01：持股 PRICE 判 ADD 也套 90 天節流（近期完整分析已見低於上限價卻未判 BUY → HOLD）。
- M5-RS7-02：節流讀框架寫的決策日誌 price／auto_buy_cap，不讀模型填的訊號卡 price（D31）。
- D76：完整分析判 BUY 即 P-50 結案，清除 observe_until；非 BUY 維持。
- M5-R3-01／M5-R5-02：THESIS 不合格／失敗（_suspect）不推進 last_review_date。
- M5-R5-01：機械原因的疑似（申報未抓取／未全附、不合格、失敗）不開 P-50 觀察期，仍交完整分析。
- M5-D5-02：訊號卡 long_rate 值與日期同步；20 年中位沿用的框架註記進決策日誌。
（M3-RS5-01 返工的 PRICE.md／routing 斷言在 test_prompt_contract.py。）
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import test_review_jobs as TR
import test_writeback as TW
from trader import db
from trader.analysis import writeback as W
from trader.jobs import review_jobs as RJ
from trader.jobs.review_jobs import ThesisHandler, _reconcile_thesis

env = TR.env
ASOF = TR.ASOF
_HOLD_ADD = {"grade": "B", "weight": 0.02, "target_weight": 0.05, "first_buy_date": "2020-01-01"}


def _types(nxt):
    return [j.job_type for j in nxt]


def _full(conn, days_ago: int, *, log_price, log_cap, card_price=None, signal="WATCH"):
    """完整分析：決策日誌（框架寫的 price／auto_buy_cap）與訊號卡（模型填的 price）分開給。"""
    d = (date.fromisoformat(ASOF) - timedelta(days=days_ago)).isoformat()
    did = f"KO:{d}:full"
    db.insert(conn, "decisions", {"decision_id": did, "ticker": "KO", "decision_date": d, "run_id": "r", "job_id": None,
                                  "signal": signal, "reason_code": "X",
                                  "data": {"action": signal, "price": log_price, "auto_buy_cap": log_cap},
                                  "created_at": d + "T00:00:00Z"})
    db.insert(conn, "signals", {"decision_id": did, "ticker": "KO", "signal": signal,
                                "data": {"signal": signal, "price": card_price, "auto_buy_cap": log_cap},
                                "created_at": d + "T00:00:00Z"})


def _price_setup(env, status, extra):
    paths, conn = env
    TR._write_cap_input(paths)
    TR._seed_registry(conn, "KO", status, extra)
    TR._gate3_row(conn)
    return paths, conn, TR._auto_cap()


# ---------------------------------------------------------------- M5-RS7-01

def test_holding_add_throttled_when_recent_full_saw_buyable(env):
    """FULL_B 框架降級成 WATCH（日誌閘門全過、T>0）30 天後 PRICE 三閘門全過：不得 ADD，改 HOLD 並註明。"""
    paths, conn, cap = _price_setup(env, "HOLDING", _HOLD_ADD)
    _full(conn, 30, log_price=cap * 0.9, log_cap=cap, card_price=cap * 0.9)
    result = TR._price_result()
    _, nxt = TR._run_price(paths, conn, TR._price_params(price=cap * 0.8), result)
    d = TR._decisions(conn)[-1]["data"]
    assert nxt == [] and d["action"] == "HOLD" and result["framework_action"] == "HOLD"
    assert any("PRICE 不加碼" in n for n in d["framework_notes"])


def test_holding_add_allowed_after_90_days_or_after_full_buy(env):
    paths, conn, cap = _price_setup(env, "HOLDING", _HOLD_ADD)
    _full(conn, 120, log_price=cap * 0.9, log_cap=cap)
    TR._run_price(paths, conn, TR._price_params(price=cap * 0.8), TR._price_result())
    assert TR._decisions(conn)[-1]["data"]["action"] == "ADD"
    _full(conn, 10, log_price=cap * 0.9, log_cap=cap, signal="BUY")
    TR._run_price(paths, conn, TR._price_params(price=cap * 0.8), TR._price_result(), n=2)
    assert TR._decisions(conn)[-1]["data"]["action"] == "ADD"


# ---------------------------------------------------------------- M5-RS7-02

def test_throttle_reads_log_price_not_model_card_price(env):
    """模型訊號卡 price 缺：仍依日誌 price 節流（修前節流失效、每次交完整分析）。"""
    paths, conn, cap = _price_setup(env, "WATCH", {"grade": "B"})
    _full(conn, 30, log_price=cap * 0.9, log_cap=cap, card_price=None)
    _, nxt = TR._run_price(paths, conn, TR._price_params(price=cap * 0.8), TR._price_result())
    assert nxt == [] and TR._decisions(conn)[-1]["data"]["action"] == "WATCH"


def test_wrong_model_card_price_does_not_throttle(env):
    """模型訊號卡 price 錯寫成低於上限、框架日誌 price 其實高於上限：不得誤壓，照常交完整分析。"""
    paths, conn, cap = _price_setup(env, "WATCH", {"grade": "B"})
    _full(conn, 30, log_price=cap * 1.1, log_cap=cap, card_price=cap * 0.5)
    _, nxt = TR._run_price(paths, conn, TR._price_params(price=cap * 0.8), TR._price_result())
    assert _types(nxt) == ["DOSSIER"]


# ---------------------------------------------------------------- D76

def _seed_observing(conn):
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "updated_at": db.now_iso(),
                                 "data": {"cost": 100.0, "observe_until": "2026-12-01",
                                          "last_full_review_date": "2026-06-01"}}, ["ticker"])


def test_full_buy_closes_p50_observation(env, tmp_path):
    paths, conn = env
    _seed_observing(conn)
    did = W.write_full_result(conn, paths, "KO", ASOF, "r1", TW._tdir(tmp_path, "full", "KO", "BUY"))
    reg = TR._reg(conn)["data"]
    assert "observe_until" not in reg
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id = ?", (did,)).fetchone()["data"])
    assert any("D76" in n for n in log["framework_notes"])


def test_full_non_buy_keeps_p50_observation(env, tmp_path):
    paths, conn = env
    _seed_observing(conn)
    W.write_full_result(conn, paths, "KO", ASOF, "r1", TW._tdir(tmp_path, "full", "KO", "WATCH"))
    assert TR._reg(conn)["data"]["observe_until"] == "2026-12-01"


# ---------------------------------------------------------------- M5-R3-01／M5-R5-02／M5-R5-01

def _thesis_job():
    return TR._job("THESIS", {**TR._price_params(), "holding": True, "last_review_date": "2026-07-01"})


def _seed_holding(conn):
    TR._seed_registry(conn, "KO", "HOLDING", {"cost": 100.0, "last_review_date": "2026-07-01",
                                             "last_full_review_date": "2026-06-01"})


def test_thesis_on_fail_does_not_advance_review_or_open_observation(env):
    paths, conn = env
    _seed_holding(conn)
    nxt = ThesisHandler().on_fail(_thesis_job(), "claude 結束碼 1：overloaded", TR._ctx(paths, conn))
    reg = TR._reg(conn)["data"]
    assert _types(nxt) == ["DOSSIER"]                       # 仍交完整分析
    assert reg["last_review_date"] == "2026-07-01"          # 修前推進到 ASOF
    assert "observe_until" not in reg                       # 修前開 6 個月觀察期
    assert TR._decisions(conn)[-1]["data"]["thesis_check"] == "疑似"


def test_thesis_on_invalid_does_not_advance_review(env):
    paths, conn = env
    _seed_holding(conn)
    ThesisHandler().on_invalid(_thesis_job(), ["verdict 缺"], TR._ctx(paths, conn))
    reg = TR._reg(conn)["data"]
    assert reg["last_review_date"] == "2026-07-01" and "observe_until" not in reg


def test_thesis_filings_not_fetched_intact_becomes_suspect_without_observation(env):
    """申報未抓取（_status.txt）、模型判完好 → 框架改疑似：不開觀察期、不推進、交完整分析。"""
    paths, conn = env
    _seed_holding(conn)
    job, ctx = _thesis_job(), TR._ctx(paths, conn)        # sec=None → pre_script 寫 _status.txt
    h = ThesisHandler()
    h.pre_script(job, ctx)
    nxt = h.post(job, TR.thesis_result("完好"), ctx)
    reg = TR._reg(conn)["data"]
    assert _types(nxt) == ["DOSSIER"] and TR._decisions(conn)[-1]["data"]["thesis_check"] == "疑似"
    assert "observe_until" not in reg and reg["last_review_date"] == "2026-07-01"


def test_reconcile_flags_only_pure_mechanical_suspect():
    ok = TR.thesis_result("完好")
    assert _reconcile_thesis(ok, 1, False).get("framework_suspect") is True
    assert _reconcile_thesis(ok, 1, "2026-09-03").get("framework_suspect") is True
    near = {**ok, "kill_criteria_status": [{"criterion": "x", "status": "接近", "evidence": "e"}]}
    assert "framework_suspect" not in _reconcile_thesis(near, 1, False)    # 另有實質疑點 → 照開觀察期
    assert "framework_suspect" not in _reconcile_thesis(TR.thesis_result("疑似"), 1, False)   # 模型自判疑似


def test_model_suspect_still_opens_observation(env):
    paths, conn = env
    _seed_holding(conn)
    ThesisHandler().post(_thesis_job(), TR.thesis_result("疑似"), TR._ctx(paths, conn))
    assert TR._reg(conn)["data"]["observe_until"] == (date.fromisoformat(ASOF) + timedelta(days=183)).isoformat()


# ---------------------------------------------------------------- M5-D5-02

def test_price_long_rate_date_synced_and_20y_stale_note_logged(env):
    paths, conn, cap = _price_setup(env, "WATCH", {"grade": "B"})
    db.insert(conn, "decisions", {"decision_id": "KO:old", "ticker": "KO", "decision_date": "2026-01-02", "run_id": "r",
                                  "job_id": None, "signal": "WATCH", "reason_code": "X",
                                  "data": {"action": "WATCH", "review_type": "PRICE"}, "created_at": "2026-01-02T00:00:00Z"})
    db.insert(conn, "signals", {"decision_id": "KO:old", "ticker": "KO", "signal": "WATCH",
                                "data": {"signal": "WATCH", "long_rate": {"tenor": "30Y", "value": 0.04, "date": "2026-01-02"}},
                                "created_at": "2026-01-02T00:00:00Z"})
    params = TR._price_params(price=cap * 1.2, rates={"t30": 0.049, "t30_date": "2026-10-15"})   # 20 年中位缺
    _, nxt = TR._run_price(paths, conn, params, TR._price_result())
    assert nxt == []
    card = TR.latest_signal(conn)["data"]
    assert card["long_rate"]["value"] == 0.049 and card["long_rate"]["date"] == "2026-10-15"
    notes = TR._decisions(conn)[-1]["data"]["framework_notes"]
    assert any("rates.t30_20y_median" in n for n in notes)
