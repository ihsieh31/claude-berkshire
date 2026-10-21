"""RM2-12：複審 M2 收尾程式項。
- sizing.plan_sells：HOLD_REVIEW TRIM 比例 1.0（D03 荒謬級上限）照比例全賣，不再落到「減半」；
  全賣仍先經 writeback._vet_valuation_sell（第一年 HOLD、荒謬級只減一次、3c ≤1/4、普通企業無 G8 → HOLD）。
- M2-RS8-03（框架側）：申報抓取失敗的檔只略過申報類觸發，其他觸發照建工作；申報窗口以登錄表 filings_fail.since
  保留，恢復後交 review_due 的 filings_since（只放寬申報窗口，A3-M2E-03）退回該日重查；連續失敗 3 次開 trigger_fail_streak 異常。
"""
from __future__ import annotations

import json
from datetime import date

import pytest

import test_fix_P24 as P24
import test_review_pipeline as TRP
from test_fix_P43 import DataLayerSec
from test_review_pipeline import env as pipe_env  # noqa: F401 - pytest 夾具
from test_review_pipeline import no_engine  # noqa: F401 - pytest 夾具
from trader.analysis import writeback as W
from trader.pipelines import review as RP
from trader.pipelines.review import run_review
from trader.portfolio import sizing as S

env = P24.env   # (paths, conn)
ASOF = P24.ASOF


# ---------------------------------------------------------------- sizing：TRIM 比例 1.0

def _plan(conn, price=300.0):
    return [p for p in S.plan_sells(conn, ASOF, S.load_decisions(conn, ASOF), {"KO": price}) if p["ticker"] == "KO"]


def test_absurd_trim_fraction_one_sells_all_at_line(env):
    """荒謬級（滿一年、首次）TRIM 1.0：日誌 1.0、last_trim.shares＝全部，plan_sells 全量限價＝線（修前賣一半）。"""
    paths, conn = env
    P24._seed(conn, qty=101, absurd=260.0)
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", P24._hr(1.0), price=300.0, iv_base=100.0)
    log = P24._log(conn, did)
    assert log["action"] == "TRIM" and log["trim_fraction"] == 1.0
    assert P24._reg_data(conn)["last_trim"]["shares"] == 101
    [p] = _plan(conn)
    assert p["qty"] == 101 and p["limit_price"] == 260.0


def test_fraction_below_one_unchanged(env):
    paths, conn = env
    P24._seed(conn, qty=101, absurd=260.0)
    W.write_hold_review_result(conn, paths, "KO", ASOF, "r", P24._hr(0.6), price=300.0, iv_base=100.0)
    assert _plan(conn)[0]["qty"] == 60   # floor(101 × 0.6)


def test_trim_one_still_vetted_by_writeback(env):
    """全賣路徑不繞過 D01／D03：第一年 → HOLD；荒謬級第二次 → HOLD；3c → 夾到 1/4；普通企業（無 G8）→ HOLD。"""
    paths, conn = env
    P24._seed(conn, qty=100, absurd=260.0)
    P24.seed_p08(conn)   # A3-M3D-02：頂級複利型須 P-08 品質優
    # 3c 頂級複利型、未達荒謬級：夾到 0.25
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r1", P24._hr(1.0, P24.Q3C), price=200.0, iv_base=100.0)
    assert P24._log(conn, did)["trim_fraction"] == 0.25
    # 普通企業、未達荒謬級：HOLD
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r2", P24._hr(1.0), price=200.0, iv_base=100.0)
    assert P24._log(conn, did)["action"] == "HOLD"
    # 荒謬級第一次 1.0，第二次（未回線下）HOLD
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r3", P24._hr(1.0), price=300.0, iv_base=100.0)
    assert P24._log(conn, did)["trim_fraction"] == 1.0
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r4", P24._hr(1.0), price=300.0, iv_base=100.0)
    assert P24._log(conn, did)["action"] == "HOLD"


def test_trim_one_first_year_is_hold(env):
    paths, conn = env
    P24._seed(conn, qty=100, absurd=260.0)
    recent = "2026-03-02"
    conn.execute("UPDATE positions SET first_buy_date=?", (recent,))
    d = P24._reg_data(conn)
    d["first_buy_date"] = recent
    conn.execute("UPDATE registry SET data=? WHERE ticker='KO'", (json.dumps(d),))
    conn.commit()
    did = W.write_hold_review_result(conn, paths, "KO", ASOF, "r", P24._hr(1.0), price=300.0, iv_base=100.0)
    assert P24._log(conn, did)["action"] == "HOLD"
    assert _plan(conn) == [] or all(p["qty"] == 0 for p in _plan(conn))


# ---------------------------------------------------------------- M2-RS8-03：申報抓取失敗只略過申報類觸發

PRX = ("WATCH", {"cik": "0000000021", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                 "auto_buy_cap": 110})   # 現價 105 < 上限 → PRICE（價格類觸發，不需申報）
QUIET = ("WATCH", {"cik": "0000000022", "last_review_date": "2026-09-01", "last_full_review_date": "2026-09-01",
                   "auto_buy_cap": 80})  # 無價格觸發
TEN_K = [{"form": "10-K", "filingDate": "2026-10-01", "items": "", "url": ""}]


def _reg(conn, t):
    return json.loads(conn.execute("SELECT data FROM registry WHERE ticker=?", (t,)).fetchone()["data"])


def _cap_inputs(paths, t="PRX"):
    (paths.state / "cap_inputs").mkdir(parents=True, exist_ok=True)
    (paths.state / "cap_inputs" / f"{t}.json").write_text("{}", encoding="utf-8")


def _clients(seed, sec=None):
    return {"alpaca": TRP.FakeAlpaca({t: TRP.CLOSE for t in seed}), "fred": TRP.FakeFred(),
            "sec": sec if sec is not None else TRP.FakeSec({})}


def _streak_anoms(conn, t="PRX"):
    return conn.execute("SELECT detail, pause_trading FROM anomalies WHERE ticker=? AND kind='trigger_fail_streak' "
                        "AND status='open'", (t,)).fetchall()


def test_sec_failure_still_builds_price_job_and_keeps_window(pipe_env, no_engine):
    cfg, paths, conn, tmp = pipe_env
    seed = {"PRX": PRX}
    TRP._seed(conn, seed)
    _cap_inputs(paths)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all",
                   clients=_clients(seed, DataLayerSec({}, {}, fail={"PRX"})))
    assert "error" not in s, s
    assert s["jobs"] == {"PRX": "PRICE"}   # 修前：整檔 skipped、沒有工作
    out = paths.work / s["run_id"]
    assert json.loads((out / "filings.json").read_text(encoding="utf-8")) == {"PRX": None}
    due = json.loads((out / "due.json").read_text(encoding="utf-8"))
    assert due["results"][0]["filings_failed"] is True and due["results"][0]["action"] == "PRICE"
    assert any(x["ticker"] == "PRX" and "只略過申報類" in x["reason"] for x in s["skipped"])
    assert _reg(conn, "PRX")["filings_fail"] == {"count": 1, "since": "2026-09-01", "last": TRP.ASOF}


def test_offline_null_failure_builds_job(pipe_env, no_engine):
    cfg, paths, conn, tmp = pipe_env
    seed = {"PRX": PRX}
    TRP._seed(conn, seed)
    _cap_inputs(paths)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": None}))
    assert "error" not in s, s
    assert s["jobs"] == {"PRX": "PRICE"}
    assert _reg(conn, "PRX")["filings_fail"]["count"] == 1


def test_three_consecutive_failures_open_anomaly_once(pipe_env, no_engine):
    cfg, paths, conn, tmp = pipe_env
    seed = {"PRX": QUIET}
    TRP._seed(conn, seed)
    off = TRP._offline(tmp, {"PRX": None})
    for i, asof in enumerate(("2026-10-16", "2026-10-16", "2026-10-19")):   # 同一決策日重跑不重複計
        run_review(cfg, paths, conn, asof, scope="all", run_id=f"r{i}", clients=_clients(seed), offline_filings=off)
    assert _reg(conn, "PRX")["filings_fail"]["count"] == 2 and not _streak_anoms(conn)
    run_review(cfg, paths, conn, "2026-10-20", scope="all", run_id="r3", clients=_clients(seed), offline_filings=off)
    [a] = _streak_anoms(conn)
    assert a["pause_trading"] == 0 and "申報" in json.loads(a["detail"])["note"]
    run_review(cfg, paths, conn, "2026-10-21", scope="all", run_id="r4", clients=_clients(seed), offline_filings=off)
    assert len(_streak_anoms(conn)) == 1 and _reg(conn, "PRX")["filings_fail"]["count"] == 4
    assert _reg(conn, "PRX")["filings_fail"]["since"] == "2026-09-01"


def test_recovery_rechecks_filings_since_first_failure(pipe_env, no_engine):
    """失敗期間 PRICE 已把 last_review_date 推到 10-10；恢復時交 review_due 的申報窗口（filings_since）退回 since（09-01）
    → 10-01 的 10-K 觸發 FULL（修前 NONE，該年報永久漏掉）；last_review_date 不動（A3-M2E-03）；工作未完成前標記保留。"""
    cfg, paths, conn, tmp = pipe_env
    data = dict(QUIET[1], last_review_date="2026-10-10",
                filings_fail={"count": 2, "since": "2026-09-01", "last": "2026-10-15"})
    seed = {"PRX": ("WATCH", data)}
    TRP._seed(conn, seed)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": TEN_K}))
    assert "error" not in s, s
    out = paths.work / s["run_id"]
    reg = json.loads((out / "registry.json").read_text(encoding="utf-8"))
    assert reg["entries"][0]["filings_since"] == "2026-09-01"
    assert reg["entries"][0]["last_review_date"] == "2026-10-10"   # A3-M2E-03：只放寬申報窗口
    assert s["jobs"] == {"PRX": "DOSSIER"}
    p = json.loads(TRP._jobs(conn, s["run_id"])[0]["params"])
    assert p["last_review_date"] == "2026-10-10"
    assert _reg(conn, "PRX")["filings_fail"]["since"] == "2026-09-01"   # 工作未完成（no_engine）→ 標記保留
    assert _reg(conn, "PRX")["last_review_date"] == "2026-10-10"         # 登錄表本身不被改寫


def test_recovery_with_nothing_due_clears_marker(pipe_env, no_engine):
    cfg, paths, conn, tmp = pipe_env
    data = dict(QUIET[1], last_review_date="2026-10-10",
                filings_fail={"count": 2, "since": "2026-09-01", "last": "2026-10-15"})
    seed = {"PRX": ("WATCH", data)}
    TRP._seed(conn, seed)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": []}))
    assert "error" not in s and s["jobs"] == {}
    assert _reg(conn, "PRX").get("filings_fail") is None


def test_recovery_marker_cleared_after_jobs_done(pipe_env, monkeypatch):
    cfg, paths, conn, tmp = pipe_env
    data = dict(QUIET[1], last_review_date="2026-10-10",
                filings_fail={"count": 1, "since": "2026-09-01", "last": "2026-10-15"})
    seed = {"PRX": ("WATCH", data)}
    TRP._seed(conn, seed)

    def fake_run(self, run_id):   # 模擬引擎跑完：本 run 工作全部 done
        self.conn.execute("UPDATE jobs SET status='done' WHERE run_id=?", (run_id,))
        self.conn.commit()
        return {"status": "done"}
    monkeypatch.setattr(RP.Engine, "run", fake_run)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": TEN_K}))
    assert s["jobs"] == {"PRX": "DOSSIER"}
    assert _reg(conn, "PRX").get("filings_fail") is None


def test_due_only_does_not_touch_registry(pipe_env):
    cfg, paths, conn, tmp = pipe_env
    seed = {"PRX": PRX}
    TRP._seed(conn, seed)
    run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
               offline_filings=TRP._offline(tmp, {"PRX": None}), due_only=True)
    assert "filings_fail" not in _reg(conn, "PRX")


# ---------------------------------------------------------------- RM2-12R：清除條件

def test_recovery_marker_cleared_after_conservative_fallback(pipe_env, monkeypatch):
    """恢復 run 的工作以 conservative_fallback（D43 已處理終態，會推進 last_review_date）收尾 → 標記清除；
    修前只認 done，標記保留 → 之後每天放寬窗口、同一份申報重觸發。"""
    cfg, paths, conn, tmp = pipe_env
    data = dict(QUIET[1], last_review_date="2026-10-10",
                filings_fail={"count": 1, "since": "2026-09-01", "last": "2026-10-15"})
    seed = {"PRX": ("WATCH", data)}
    TRP._seed(conn, seed)

    def fake_run(self, run_id):
        self.conn.execute("UPDATE jobs SET status='conservative_fallback' WHERE run_id=?", (run_id,))
        self.conn.commit()
        return {"status": "done"}
    monkeypatch.setattr(RP.Engine, "run", fake_run)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": TEN_K}))
    assert s["jobs"] == {"PRX": "DOSSIER"}
    assert _reg(conn, "PRX").get("filings_fail") is None


def test_recovery_clears_marker_without_since_and_count_restarts(pipe_env, no_engine):
    """since 為 None（失敗時登錄表缺 last_review_date）：恢復後照樣清標記；之後再失敗從 1 重算，不跨不連續失敗累加。"""
    cfg, paths, conn, tmp = pipe_env
    data = dict(QUIET[1], filings_fail={"count": 1, "since": None, "last": "2026-10-15"})
    seed = {"PRX": ("WATCH", data)}
    TRP._seed(conn, seed)
    s = run_review(cfg, paths, conn, "2026-10-16", scope="all", run_id="ok", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": []}))
    assert "error" not in s and s["jobs"] == {}
    assert _reg(conn, "PRX").get("filings_fail") is None
    run_review(cfg, paths, conn, "2026-10-19", scope="all", run_id="bad", clients=_clients(seed),
               offline_filings=TRP._offline(tmp, {"PRX": None}))
    assert _reg(conn, "PRX")["filings_fail"]["count"] == 1


def test_hold_review_check_accepts_trim_fraction_one():
    """hold_jobs.check：TRIM 1.0（D03 荒謬級上限、writeback hi=1.0）不得被驗證退回（修前 0<f<1 拒絕）；0 與 >1 仍拒。"""
    from test_hold_review import hold
    from trader.jobs.hold_jobs import check
    ok = dict(iv_base=50.0, price=60.0, held_days=1000)
    assert check(hold("TRIM", "估值", 1.0), **ok) == ([], False)
    assert check(hold("TRIM", "估值", 0), **ok)[0]
    assert check(hold("TRIM", "估值", 1.01), **ok)[0]
