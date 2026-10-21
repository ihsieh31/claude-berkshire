"""F3-HS（V3-1 FG-M1-host-screen）：初篩宿主修復。每條修前失敗、修後通過。不連網。"""
import argparse
import json

import pandas as pd
import pytest

from trader import cli, db
from trader.paths import get_paths
from trader.pipelines import analyze as AN
from trader.pipelines import bootstrap as B
from trader.pipelines import screen as SP

from test_screen_pipeline import ASOF, _reg, env  # noqa: F401


def _paths(state_dir):
    return get_paths().ensure()


# ---------------------------------------------------------------- A3-M1B-01 誠信淘汰
def test_integrity_reject_excluded_beyond_12m(state_dir):
    paths = _paths(state_dir)
    reg = {"FRD": {"status": "REJECT", "last_full_review_date": "2025-09-01", "integrity_reject": True},
           "HRK": {"status": "REJECT", "last_full_review_date": "2025-09-01", "integrity_reject": True,
                   "origin": "hark_b"}}
    ex = SP.build_exclusions(paths, reg, ASOF).set_index("ticker")
    assert "FRD" in ex.index and ex.at["FRD", "excl_fraud"] and not ex.at["FRD", "excl_recent_reject"]
    assert "HRK" not in ex.index                       # 他機結論不鎖死（ASK-2）
    need, why = SP.needs_full(reg["FRD"], ASOF)
    assert need is False and "NEVER-04" in why
    assert SP.needs_full({**reg["FRD"], "status": "MISSING_DATA"}, ASOF)[0] is False   # D87 保留標記
    assert SP.needs_full(reg["HRK"], ASOF)[0] is True


def test_integrity_reject_in_summary(env):
    paths, conn, inputs = env
    _reg(conn, "ZZZ", status="REJECT", last_full_review_date="2025-01-01", integrity_reject=True)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    assert [x["ticker"] for x in s["x4_integrity"]] == ["ZZZ"]
    ex = pd.read_csv(paths.state / "screens" / ASOF / "exclusions.csv").set_index("ticker")
    assert bool(ex.at["ZZZ", "excl_fraud"])


# ---------------------------------------------------------------- A3-M1B-02 撤銷檔
def _ov(paths, rows):
    (paths.state / SP.USER_OVERRIDES).write_text("ticker,date,reason\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_overrides_order_independent(state_dir):
    paths = _paths(state_dir)
    _ov(paths, ["AAA,2026-09-01,new", "AAA,2025-06-01,old"])     # 較新列在前
    o = SP.load_overrides(paths, ASOF, [])
    assert list(o["reason"]) == ["new"] and list(o.columns) == ["ticker", "date", "reason"]


def test_overrides_blank_date_reported(state_dir):
    paths = _paths(state_dir)
    _ov(paths, ["AAA,,blank", "BBB,2026-13-40,bad", "CCC,2026-12-01,future", "DDD,2026-01-01,ok"])
    ign = []
    o = SP.load_overrides(paths, ASOF, [], ign)
    assert list(o["ticker"]) == ["DDD"]
    got = {x["ticker"]: x["ignored"] for x in ign}
    assert set(got) == {"AAA", "BBB", "CCC"}
    assert "空白" in got["AAA"] and "格式" in got["BBB"] and "晚於" in got["CCC"]


# ---------------------------------------------------------------- A3-M1A-03 往較早初篩找
def _screen_dir(paths, day, decisions):
    d = paths.state / "screens" / day
    d.mkdir(parents=True)
    (d / SP.DECISIONS).write_text(json.dumps(decisions), encoding="utf-8")
    (d / "summary.json").write_text(json.dumps({"asof": day}), encoding="utf-8")


def test_screen_context_falls_back_to_earlier_delete(state_dir):
    paths = _paths(state_dir)
    _screen_dir(paths, "2026-05-20", {"XYZ": {"screen_decision": "DELETE（J1）", "delete_codes": "J1"}})
    _screen_dir(paths, "2026-08-20", {"OTHER": {"screen_decision": "PASS"}})
    c = SP.screen_context_for(paths, "2026-09-10", "XYZ", "手動")
    assert c["screen_decision"] == "DELETE（J1）" and c["screen_asof"] == "2026-05-20"
    assert "2026-08-20" in c["note"]
    c2 = SP.screen_context_for(paths, "2026-09-10", "NONE", "手動")
    assert c2["screen_decision"] == SP.UNSCREENED and "2026-08-20" in c2["note"]


# ---------------------------------------------------------------- A3-M1E-02 dry-run 後正式跑
def test_dry_run_then_real_bootstrap(env):
    paths, conn, inputs = env
    s1 = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, dry_run=True)
    assert "error" not in s1 and s1["dry_run"] is True
    s2 = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)      # 不需 --force
    assert "error" not in s2 and s2["dry_run"] is False
    assert list(paths.state.joinpath("screens").glob(f"{ASOF}.bak-*"))  # dry-run 結果改名保留
    with pytest.raises(SP.ScreenError):                                  # 正式完成後仍擋重跑
        SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)


def test_bootstrap_error_does_not_mark_screened(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(cli, "_load_or_report", lambda c: object())

    class Sch:
        def __init__(self, cfg, paths): pass
        def load_state(self): return {}
        def save_state(self, st): saved.append(st)
    import trader.scheduler as S
    monkeypatch.setattr(S, "Scheduler", Sch)

    def fake(cfg, paths, conn, asof, **kw):   # 前次 dry-run 留下 runs done
        db.insert(conn, "runs", {"run_id": f"screen-{asof}", "kind": "bootstrap", "asof": asof, "status": "done",
                                 "started_at": db.now_iso(), "summary": json.dumps({"dry_run": True})})
        return {"asof": asof, "error": "初選未執行：已完成"}
    monkeypatch.setattr(B, "run_bootstrap", fake)
    args = argparse.Namespace(config=None, state_dir=str(tmp_path / "state"), asof=ASOF, tickers=None, top=15,
                              dry_run=False, force=False, mark_screened=False)
    assert cli.cmd_bootstrap(args) == 1
    assert saved == []


# ---------------------------------------------------------------- A3-M1E-04 override 名單不釘選
def test_force_rerun_does_not_pin_override_universe(state_dir, monkeypatch):
    paths = _paths(state_dir)
    conn = db.connect(paths.db); db.migrate(conn)
    out = paths.state / "screens" / ASOF
    (out / "inputs").mkdir(parents=True)
    (out / "inputs" / "universe.txt").write_text("# source=override\nAAA\n", encoding="utf-8")
    seen = []

    def fake_fetch(cfg, paths, conn, asof, d, **kw):
        seen.append((d / "universe.txt").exists())
        raise SP.ScreenError("stop")
    monkeypatch.setattr(SP, "fetch_inputs", fake_fetch)
    s = SP.run_screen(None, paths, conn, ASOF, force=True)
    assert seen == [False] and "override" in s["universe_pin_dropped"]
    conn.close()


def test_override_universe_warns(env):
    paths, conn, inputs = env
    txt = (inputs / "universe.txt").read_text().replace("# source=test", "# source=pinned:override")
    (inputs / "universe.txt").write_text(txt)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s and "override 名單" in s["warning"]


# ---------------------------------------------------------------- A3-M1C-03 X1 清單
def test_summary_lists_x1_registry(env):
    paths, conn, inputs = env
    _reg(conn, "QQQ", status="WATCH", last_reason_code="NOT_THIS_SKILL", last_full_review_date="2026-06-01")
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert s["x1_registry"] == [{"ticker": "QQQ", "last_reason_code": "NOT_THIS_SKILL",
                                 "last_full_review_date": "2026-06-01", "expires": "2027-06-01"}]


# ---------------------------------------------------------------- A3-M1C-06 待人工項目進異常
def test_screen_review_anomaly_once_per_asof(env):
    paths, conn, inputs = env
    s0 = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)   # 合成資料無待人工項目 → 不記
    assert not s0.get("lacking") and not s0.get("deletions_to_review") and "warning" not in s0
    assert conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='screen_review'").fetchone()[0] == 0
    _ov(paths, ["AAA,,沒寫日期"])                                      # → overrides_ignored
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, force=True)
    assert "error" not in s and s["overrides_ignored"]
    rows = conn.execute("SELECT detail FROM anomalies WHERE kind='screen_review' AND status='open'").fetchall()
    assert len(rows) == 1 and json.loads(rows[0]["detail"])["asof"] == ASOF
    SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, force=True)
    n = conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='screen_review' AND status='open'").fetchone()[0]
    assert n == 1


def test_dry_run_no_screen_review_anomaly(env):
    paths, conn, inputs = env
    SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, dry_run=True)
    assert conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='screen_review'").fetchone()[0] == 0


# ---------------------------------------------------------------- A3-M1C-11 NOTE_COLS
def test_decisions_carry_filing_dates(env):
    paths, conn, inputs = env
    SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    d = json.loads((paths.state / "screens" / ASOF / SP.DECISIONS).read_text())
    any_row = next(iter(d.values()))
    for k in ("fy_last_filed", "max_filed_used", "mcap_shares_source"):
        assert k in any_row
    from trader.jobs.sandbox import _has_price_key
    assert not _has_price_key(d)


# ---------------------------------------------------------------- D95 撤銷不保證進短名單
def test_override_not_in_to_analyze_warns(env, monkeypatch):
    paths, conn, inputs = env
    real = SP.run_skill_screen

    def patched(*a, **kw):
        real(*a, **kw)
        out = paths.state / "screens" / ASOF / "screen_handoff.json"
        h = json.loads(out.read_text())
        h["overrides"] = [{"ticker": "NOTPICKED", "note": "人工撤銷"}]
        out.write_text(json.dumps(h))
    monkeypatch.setattr(SP, "run_skill_screen", patched)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "撤銷不保證進短名單，要分析請手動 trader analyze --tickers" in s["warning"] and "NOTPICKED" in s["warning"]


# ---------------------------------------------------------------- A3-M1C-04 method 註記
def test_pe_median_method_notes_share_class_duplicates():
    from trader.data import pe_median as P
    import inspect
    assert "多類股" in inspect.getsource(P.compute_pe_median)


# ---------------------------------------------------------------- A3-M1E-03 analyze_run_id
def test_bootstrap_backfills_analyze_run_id_and_analyze_uses_it(state_dir, monkeypatch):
    paths = _paths(state_dir)
    conn = db.connect(paths.db); db.migrate(conn)
    sd = paths.state / "screens" / ASOF
    sd.mkdir(parents=True)
    (sd / "summary.json").write_text(json.dumps({"asof": ASOF, "analyze_run_id": f"analyze-{ASOF}",
                                                 "to_analyze": [{"ticker": "KO"}]}))
    db.insert(conn, "runs", {"run_id": f"screen-{ASOF}", "kind": "bootstrap", "asof": ASOF, "status": "done",
                             "started_at": db.now_iso()})
    monkeypatch.setattr(B, "run_screen", lambda *a, **kw: {"run_id": f"screen-{ASOF}", "to_analyze": [{"ticker": "KO"}]})
    monkeypatch.setattr(B, "time", type("T", (), {"strftime": staticmethod(lambda f: "20261019T010000")}))
    monkeypatch.setattr(B, "run_analyze", lambda *a, **kw: {"run_id": kw.get("run_id")})
    B.run_bootstrap(None, paths, conn, ASOF, force=True)
    new = f"analyze-{ASOF}-r20261019T010000"
    assert json.loads((sd / "summary.json").read_text())["analyze_run_id"] == new

    rs = conn.execute("SELECT summary FROM runs WHERE run_id=?", (f"screen-{ASOF}",)).fetchone()[0]
    assert json.loads(rs)["analyze_run_id"] == new
    conn.close()


def test_run_analyze_resolves_backfilled_run_id(state_dir, monkeypatch):
    paths = _paths(state_dir)
    conn = db.connect(paths.db); db.migrate(conn)
    sd = paths.state / "screens" / ASOF
    sd.mkdir(parents=True)
    rid = f"analyze-{ASOF}-rX"
    (sd / "summary.json").write_text(json.dumps({"asof": ASOF, "analyze_run_id": rid, "to_analyze": []}))
    seen = []

    class Stop(Exception):
        pass

    def fake_fetch(cfg, paths, conn, asof, d, **kw):
        seen.append(str(d))
        raise Stop()
    monkeypatch.setattr(AN, "fetch_inputs", fake_fetch)
    monkeypatch.setattr(AN, "watchlist_tickers", lambda c: [])
    with pytest.raises(Stop):
        AN.run_analyze(None, paths, conn, ASOF, tickers=["KO"], clients={"sec": object()})
    assert rid in seen[0]
    conn.close()


def test_cli_analyze_warns_not_rerun(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_load_or_report", lambda c: object())
    monkeypatch.setattr(AN, "run_analyze", lambda *a, **kw: {
        "run_id": f"analyze-{ASOF}", "not_rerun": ["KO"], "engine": {"done": 1, "failed": 0}})
    args = argparse.Namespace(config=None, state_dir=str(tmp_path / "state"), asof=ASOF, tickers="KO",
                              screen_date=None, run_id=None)
    assert cli.cmd_analyze(args) == 0
    assert f"[WARN] analyze-{ASOF} 已分析過、本次未重做：KO" in capsys.readouterr().out
