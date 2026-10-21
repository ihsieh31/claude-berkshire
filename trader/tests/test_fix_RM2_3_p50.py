"""RM2-3 P-50 部分（M2_D4_04 複審 PARTIAL）：P-50 觀察期到期必須結案。
(a) HOLD_REVIEW 提示帶觸發理由；(b) 觸發理由只遮數值、保留規則編號與期間（價格盲工作仍不見價格）；
(c) HOLD_REVIEW 回寫：到期結案不接受新期限、觀察中不得延後、新期限最長 6 個月。"""
from __future__ import annotations

import json

import pytest

from trader import db
from trader.analysis import writeback
from trader.jobs import hold_jobs
from trader.jobs.context import build_prompt
from trader.jobs.deep import trigger_text
from trader.jobs.handlers import HANDLERS, HandlerCtx
from trader.jobs.validate import load_schemas
from trader.models import JobSpec
from trader.paths import get_paths

ASOF = "2026-10-16"
P50 = "[FULL] P-50 觀察期到期（observe_until 2026-10-01）：必須結案"
W43 = "[FULL] 單檔權重 26.3% > 25%（P-43）：完整複查（因上漲超限不賣，設計決策 D01）"
M12 = "[FULL] 距上次完整分析（2025-10-01）已滿 12 個月"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADER_STATE_DIR", str(tmp_path / "state"))
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield paths, conn, tmp_path
    conn.close()


def _seed(conn, data):
    db.upsert(conn, "registry", {"ticker": "KO", "status": "HOLDING", "data": data, "updated_at": db.now_iso()},
              ["ticker"])


def _reg(conn):
    return json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'").fetchone()["data"])


def _hold(ou):
    return {"ticker": "KO", "decision_date": ASOF, "path": [], "action": "HOLD", "sell_category": None,
            "trim_fraction": None, "stop_adding": True, "observe_until": ou, "falsified_assumption": None,
            "reason": "仍只是疑似"}


def _write(env, ou, params=None):
    paths, conn, _ = env
    writeback.write_hold_review_result(conn, paths, "KO", ASOF, "r1", _hold(ou),
                                       params or {"full_stage": "B", "final_signal": "WATCH", "reasons": [P50]},
                                       [], price=100.0, iv_base=120.0)
    d = _reg(conn)
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE ticker='KO' ORDER BY rowid DESC").fetchone()[0])
    return d, log


# ---------------------------------------------------------------- (b) 觸發理由

def test_trigger_keeps_rule_ids_and_periods_masks_values():
    for blind in (True, False):
        t = trigger_text({"reasons": [P50, W43, M12]}, price_blind=blind)
        assert "細節含數字" not in t
        assert "P-50 觀察期到期（observe_until 2026-10-01）：必須結案" in t
        if blind:   # X55：價格盲工作的 P-43 理由只留類別（「因上漲超限不賣」洩漏價格方向）
            assert "制度性完整複查，細節略" in t and "26.3" not in t and "上漲" not in t
        else:
            assert "P-43" in t and "權重" in t and "26.3" not in t and "25%" not in t and "〔數值略〕" in t
        assert "已滿 12 個月" in t and "2025-10-01" in t


def test_trigger_price_reasons_stay_hidden():
    r = ["[THESIS] 單季自高點跌 ≥25%（2026-07-01 高點 70.00 → 2026-10-15 52.00）",
         "[THESIS] 較成本 −12%（成本 60.00 → 2026-10-15 52.00，-13.3%；論點破裂檢查，不是停損）",
         "[THESIS] 上次分析後出現單日 ±4%（3 日；最大 2026-10-01 +5.1%）",
         "[PRICE] 觀察名單現價 52.00 < 自動買進上限 55.0：快速通道重跑 G7–G11 與三閘門",
         "[FULL] 估值 $12.5、3.2倍、報酬 8.5%"]
    t = trigger_text({"reasons": r})
    for x in ("70.00", "52.00", "60.00", "13.3", "5.1", "55.0", "12.5", "3.2", "8.5", "[PRICE]", "現價"):
        assert x not in t
    assert t.count("[THESIS]（細節含數字，略）") == 3


# ---------------------------------------------------------------- (a) HOLD_REVIEW 提示

def test_hold_review_prompt_carries_trigger(env):
    paths, conn, tmp = env
    ctx = HandlerCtx(paths=paths, schemas=load_schemas(paths.schemas_json), work_dir=paths.work, conn=conn)
    sb = tmp / "sb"
    sb.mkdir()
    j = JobSpec(job_id="r1:KO:HOLD_REVIEW:1", run_id="r1", job_type="HOLD_REVIEW", ticker="KO", asof=ASOF,
                params={"full_stage": "B", "final_signal": "WATCH", "reasons": [P50, W43]}, sandbox=sb)
    vals = HANDLERS["HOLD_REVIEW"].prompt_values(j, ctx, "claude_code")
    text = build_prompt(j, "claude_code", 10 ** 9, paths.root, [], values=vals)
    assert "P-50 觀察期到期（observe_until 2026-10-01）：必須結案" in text and "P-43" in text
    assert "到期必須結案" in text and "{trigger}" not in text


# ---------------------------------------------------------------- (c) 期限

def test_expired_p50_cannot_renew(env):
    _, conn, _ = env
    _seed(conn, {"cost": 100.0, "observe_until": "2026-10-01", "last_full_review_date": "2026-04-01",
                 "last_review_date": "2026-09-01"})
    d, log = _write(env, "2028-12-31")
    assert d["observe_until"] == "2026-10-01" and log["observe_until"] is None
    assert any("必須結案" in n for n in log["framework_notes"])
    assert not hold_jobs._registry_observing(conn, "KO", ASOF)


def test_observing_cannot_extend_original_deadline(env):
    _, conn, _ = env
    _seed(conn, {"cost": 100.0, "observe_until": "2026-12-01", "last_full_review_date": "2026-06-01"})
    d, _ = _write(env, "2027-09-01", {"full_stage": "B", "final_signal": "WATCH"})
    assert d["observe_until"] == "2026-12-01"


def test_new_observation_capped_at_six_months(env):
    _, conn, _ = env
    _seed(conn, {"cost": 100.0, "last_full_review_date": "2026-06-01"})
    d, log = _write(env, "2028-12-31", {"full_stage": "B", "final_signal": "WATCH"})
    assert d["observe_until"] == "2027-04-17" and log["observe_until"] == "2027-04-17"   # ASOF + 183 天


def test_new_observation_after_closed_one_allowed(env):
    _, conn, _ = env
    # 舊觀察期 2026-01-01 到期後已有完整分析（2026-02-01）結案 → 本次新疑似可開新觀察
    _seed(conn, {"cost": 100.0, "observe_until": "2026-01-01", "last_full_review_date": "2026-02-01"})
    d, _ = _write(env, "2027-01-15", {"full_stage": "B", "final_signal": "WATCH"})
    assert d["observe_until"] == "2027-01-15"


# ---------------------------------------------------------------- RM2-13R：實際流程（FULL 先推進 last_full_review_date）

_EXPIRED = {"cost": 100.0, "observe_until": "2026-10-01", "last_full_review_date": "2026-04-01",
            "last_review_date": "2026-09-01"}


def _assert_closed(conn, log):
    d = _reg(conn)
    assert d["last_full_review_date"] == ASOF          # 前一步確實已推進到決策日
    assert d["observe_until"] == "2026-10-01" and log["observe_until"] is None
    assert any("必須結案" in n for n in log["framework_notes"])
    assert not hold_jobs._registry_observing(conn, "KO", ASOF)


def _stop(env):
    paths, conn, _ = env
    writeback.write_stop_result(conn, paths, "KO", ASOF, "r1", {"ticker": "KO", "decision_date": ASOF, "stopped": True,
                                "stopped_at_gate": "G3", "reason_code": "TOO_HARD", "rule_ids": ["X"]}, [])
    assert _reg(conn)["last_full_review_date"] == ASOF


_STOP_PARAMS = {"full_stage": "A", "stopped_at_gate": "G3", "reason_code": "TOO_HARD"}


def test_stop_then_hold_review_cannot_renew_expired(env):
    _, conn, _ = env
    _seed(conn, dict(_EXPIRED))
    _stop(env)
    _, log = _write(env, "2027-03-01", {**_STOP_PARAMS, "reasons": [P50]})
    _assert_closed(conn, log)


def test_full_then_hold_review_cannot_renew_expired(env):
    # 實際鏈：review_due 的 P-50 理由經 deep._carry 帶到 HOLD_REVIEW；write_full_result 重建登錄表列（不留 observe_until）
    import test_writeback as TW
    paths, conn, tmp = env
    _seed(conn, dict(_EXPIRED))
    writeback.write_full_result(conn, paths, "KO", ASOF, "r1", TW._tdir(tmp, "full", "KO", "WATCH"))
    assert _reg(conn)["last_full_review_date"] == ASOF
    _, log = _write(env, "2027-03-01", {"full_stage": "B", "final_signal": "WATCH", "reasons": [P50]})
    d = _reg(conn)
    assert d.get("observe_until") == "2026-10-01" and log["observe_until"] is None
    assert any("必須結案" in n for n in log["framework_notes"])
    assert not hold_jobs._registry_observing(conn, "KO", ASOF)


def test_unrelated_trigger_after_closed_observation_still_opens_new(env):
    # 舊觀察期到期後已結案（之後有完整分析），本次非 P-50 觸發 → 仍可開新觀察（不放寬也不誤殺）
    _, conn, _ = env
    _seed(conn, {"cost": 100.0, "observe_until": "2026-01-01", "last_full_review_date": "2026-02-01"})
    d, _ = _write(env, "2027-01-15", {"full_stage": "B", "final_signal": "WATCH", "reasons": [W43]})
    assert d["observe_until"] == "2027-01-15"


# ---------------------------------------------------------------- RM2-13R2：端到端（停止／FULL 回寫後再 HOLD_REVIEW）

@pytest.mark.parametrize("closed", [{"observe_until": "2026-01-01", "last_full_review_date": "2026-02-01"},
                                    {"observe_until": "2026-09-01", "last_full_review_date": "2026-09-16"}])
def test_closed_then_stop_then_other_trigger_opens_new(env, closed):
    # 舊觀察期已結案（登錄表留過期 observe_until）→ 他因 FULL_A 停止（last_full 推到決策日）→ HOLD_REVIEW 可新開 ≤183 天
    _, conn, _ = env
    _seed(conn, {"cost": 100.0, **closed})
    _stop(env)
    d, log = _write(env, "2027-01-15", {**_STOP_PARAMS, "reasons": [W43]})
    assert d["observe_until"] == "2027-01-15" and log["observe_until"] == "2027-01-15"
    assert hold_jobs._registry_observing(conn, "KO", ASOF)
    d, _ = _write(env, "2028-12-31", {**_STOP_PARAMS, "reasons": [M12]})
    assert d["observe_until"] == "2027-01-15"   # 已在觀察中：不得延後


def _observing_full(env):
    import test_writeback as TW
    paths, conn, tmp = env
    _seed(conn, {"cost": 100.0, "observe_until": "2026-12-01", "last_full_review_date": "2026-06-01"})
    writeback.write_full_result(conn, paths, "KO", ASOF, "r1", TW._tdir(tmp, "full", "KO", "WATCH"))
    d = _reg(conn)
    assert d["last_full_review_date"] == ASOF and d["observe_until"] == "2026-12-01"   # 完整分析保留觀察期


def test_observing_full_then_hold_review_clamped(env):
    _observing_full(env)
    d, log = _write(env, "2027-04-01", {"full_stage": "B", "final_signal": "WATCH", "reasons": [W43]})
    assert d["observe_until"] == "2026-12-01" and log["observe_until"] == "2026-12-01"
    assert any("原期限不得延後" in n for n in log["framework_notes"])


def test_observing_full_then_hold_review_without_deadline_keeps_original(env):
    _, conn, _ = env
    _observing_full(env)
    d, log = _write(env, None, {"full_stage": "B", "final_signal": "WATCH", "reasons": [W43]})
    assert d["observe_until"] == "2026-12-01" and log["observe_until"] is None
    assert hold_jobs._registry_observing(conn, "KO", ASOF)
