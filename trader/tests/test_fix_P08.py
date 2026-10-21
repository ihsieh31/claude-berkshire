"""P08（M1-C）：初選重跑防護與時點。

M1-D6-02 登錄表紀錄不早於 asof → 非時點，不進 X5、要重新分析；
M1-D9-01 同 asof 已完成的初選不得原地覆寫（--force 才重跑，舊目錄改名保留）；
M1-D9-05 失敗／中斷的初選不留舊交接檔，analyze 不得把失敗初選當 0 檔成功；
M1-D9-02 features.csv 只在補股數後寫一次、以暫存檔替換；
M1-D4-04 fetch --refreeze：舊凍結檔改名保留、新凍結檔記 skill_hashes／n_tickers／universe_source。
"""
import argparse
import importlib.util
import json

import pandas as pd
import pytest

from trader import db
from trader.config import load_config
from trader.data import inputs as I
from trader.data import pe_median as P
from trader.paths import get_paths
from trader.pipelines import analyze as A
from trader.pipelines import screen as SP

ASOF = "2026-10-15"


def _skill_make():
    p = get_paths().skill_screen / "scripts" / "test_screen.py"
    spec = importlib.util.spec_from_file_location("skill_test_screen", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.make()


@pytest.fixture
def env(tmp_path, state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    F, Pr = _skill_make()
    F.to_csv(inputs / "features.csv", index=False)
    Pr.to_csv(inputs / "prices.csv", index=False)
    yield paths, conn, inputs
    conn.close()


def _reg(conn, ticker, **data):
    db.upsert(conn, "registry", {"ticker": ticker, "status": data.get("status"), "data": data,
                                 "updated_at": db.now_iso()}, key=["ticker"])


# ---------------------------------------------------------------- M1-D6-02
def test_x5_ignores_review_not_before_asof(state_dir):
    paths = get_paths().ensure()
    reg = {"SAME": {"status": "REJECT", "last_full_review_date": ASOF},          # 同日：非時點
           "LATER": {"status": "TOO_HARD", "last_full_review_date": "2026-10-20"},  # 晚於 asof
           "PRIOR": {"status": "REJECT", "last_full_review_date": "2026-10-05"}}   # asof−10 天 → X5
    ex = SP.build_exclusions(paths, reg, ASOF)
    assert list(ex["ticker"]) == ["PRIOR"] and ex["excl_recent_reject"].all()


def test_needs_full_review_not_before_asof():
    for d in (ASOF, "2026-10-20"):
        need, why = SP.needs_full({"status": "WATCH", "last_full_review_date": d}, ASOF)
        assert need and "不早於" in why
    assert not SP.needs_full({"status": "WATCH", "last_full_review_date": "2026-10-05"}, ASOF)[0]


# ---------------------------------------------------------------- M1-D9-01
def test_second_run_same_asof_refused_and_outputs_untouched(env):
    paths, conn, inputs = env
    first = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in first
    out = paths.state / "screens" / ASOF
    before = {f: (out / f).read_bytes() for f in ("summary.json", "screen_handoff.json", "handoff_noprice.json")}
    # 同日分析完、登錄表改寫（正是 r3 情境）：不 force 不得重跑
    _reg(conn, first["shortlist"][0], status="REJECT", last_full_review_date=ASOF)
    with pytest.raises(SP.ScreenError, match="--force"):
        SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert {f: (out / f).read_bytes() for f in before} == before
    assert conn.execute("SELECT status FROM runs WHERE run_id=?", (f"screen-{ASOF}",)).fetchone()[0] == "done"


def test_force_moves_old_dir_aside_and_same_day_registry_does_not_change_shortlist(env):
    paths, conn, inputs = env
    first = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    for t in first["shortlist"]:
        _reg(conn, t, status="REJECT", last_full_review_date=ASOF)   # 同 asof 的分析結果
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, force=True)
    assert "error" not in s, s
    assert s["shortlist"] == first["shortlist"]                         # 名單不換人
    assert [x["ticker"] for x in s["to_analyze"]] == first["shortlist"]
    baks = sorted(p.name for p in (paths.state / "screens").iterdir() if p.name.startswith(f"{ASOF}.bak-"))
    assert len(baks) == 1
    old = json.loads((paths.state / "screens" / baks[0] / "summary.json").read_text())
    assert old["shortlist"] == first["shortlist"]


def test_force_keeps_inputs_inside_screen_dir(env):
    paths, conn, inputs = env
    out = paths.state / "screens" / ASOF
    (out / "inputs").mkdir(parents=True)
    for f in ("features.csv", "prices.csv"):
        (out / "inputs" / f).write_bytes((inputs / f).read_bytes())
    assert "error" not in SP.run_screen(None, paths, conn, ASOF, inputs_dir=out / "inputs")
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=out / "inputs", force=True)
    assert "error" not in s, s
    assert (out / "inputs" / "prices.csv").is_file()                    # analyze 讀 screens/{asof}/inputs


# ---------------------------------------------------------------- M1-D9-05
def test_failed_rerun_leaves_no_stale_handoff(env, tmp_path):
    paths, conn, inputs = env
    assert "error" not in SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    empty = tmp_path / "empty"
    empty.mkdir()
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=empty, force=True)
    assert "缺輸入" in s["error"]
    out = paths.state / "screens" / ASOF
    assert not (out / "screen_handoff.json").exists() and not (out / "handoff_noprice.json").exists()


def test_interrupted_run_dir_moved_aside_without_force(env):
    paths, conn, inputs = env
    out = paths.state / "screens" / ASOF
    out.mkdir(parents=True)
    (out / "screen_handoff.json").write_text("{}")                      # 前次中斷留下的殘檔（runs 無 done）
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    assert any(p.name.startswith(f"{ASOF}.bak-") for p in (paths.state / "screens").iterdir())


def test_analyze_refuses_failed_screen(env, tmp_path):
    paths, conn, inputs = env
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "error" in SP.run_screen(None, paths, conn, ASOF, inputs_dir=empty)
    with pytest.raises(A.AnalyzeError, match="初選"):
        A.run_analyze(None, paths, conn, ASOF, clients={"sec": None})


def test_analyze_refuses_screen_not_done(env):
    paths, conn, _ = env
    sdir = paths.state / "screens" / ASOF
    sdir.mkdir(parents=True)
    (sdir / "summary.json").write_text(json.dumps({"to_analyze": [{"ticker": "KO"}]}))
    db.insert(conn, "runs", {"run_id": f"screen-{ASOF}", "kind": "quarterly_screen", "asof": ASOF,
                             "status": "running", "started_at": db.now_iso()})
    with pytest.raises(A.AnalyzeError, match="初選"):
        A.run_analyze(None, paths, conn, ASOF, clients={"sec": None})


# ---------------------------------------------------------------- M1-D9-02
class _Sec:
    stale, warnings = [], []

    def features(self, tickers, asof):
        return pd.DataFrame([{"ticker": t, "net_income": 1.0} for t in tickers])


class _Alpaca:
    def bars(self, symbols, start, end, adjustment="raw", timeframe="1Day"):
        return pd.DataFrame([{"ticker": s, "date": ASOF, "open": 10, "high": 10, "low": 10,
                              "close": 10.0, "volume": 1, "vwap": 10} for s in symbols])

    def corporate_actions(self, symbols, start, end, types=None):
        return []


class _Fred:
    def t30(self, asof):
        return 0.047, ASOF

    def t30_median_20y(self, asof):
        return 0.035


def _cfg(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("models:\n  strong: {backend: fake, model: f}\n  medium: {backend: fake, model: f}\n"
                 "  weak: {backend: fake, model: f}\n", encoding="utf-8")
    return load_config(p, env_file=tmp_path / "none.env")


def test_features_not_written_before_share_fill(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "state").ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    out = tmp_path / "out"

    def boom(*a, **k):
        raise RuntimeError("中斷")
    monkeypatch.setattr(I, "fill_missing_shares", boom)
    with pytest.raises(RuntimeError):
        I.fetch_inputs(_cfg(tmp_path), paths, conn, ASOF, out, tickers=["KO"],
                       clients={"sec": _Sec(), "alpaca": _Alpaca(), "fred": _Fred()})
    assert not (out / "features.csv").exists()
    assert not list(out.glob("*.tmp"))


# ---------------------------------------------------------------- M1-D4-04
def test_freeze_monthly_refreeze(state_dir):
    paths = get_paths().ensure()
    calls = []

    def compute(v):
        def f():
            calls.append(v)
            return {"asof": "2026-10-15", "pe_norm_median": v}
        return f
    P.freeze_monthly(paths, "2026-10", compute(30.0))
    assert P.freeze_monthly(paths, "2026-10", compute(99.0))["pe_norm_median"] == 30.0   # 不 refreeze → 不重算
    assert calls == [30.0]
    out = P.freeze_monthly(paths, "2026-10", compute(26.9), refreeze=True)
    assert out["pe_norm_median"] == 26.9
    assert P._read_frozen(P.freeze_path(paths, "2026-10"))["pe_norm_median"] == 26.9
    baks = list((paths.cache / "pe_median").glob("2026-10.json.bak-*"))
    assert len(baks) == 1 and json.loads(baks[0].read_text())["pe_norm_median"] == 30.0


def test_refreeze_failed_compute_keeps_old(state_dir):
    paths = get_paths().ensure()
    P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-15", "pe_norm_median": 30.0})

    def bad():
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        P.freeze_monthly(paths, "2026-10", bad, refreeze=True)
    assert P._read_frozen(P.freeze_path(paths, "2026-10"))["pe_norm_median"] == 30.0


def test_fetch_refreeze_records_provenance(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "state").ensure()
    conn = db.connect(paths.db); db.migrate(conn)
    monkeypatch.setattr(I, "load_universe", lambda paths, conn, asof: (["KO", "PEP"], "test_universe"))
    monkeypatch.setattr(I, "fill_missing_shares", lambda feats, *a, **k: (feats, []))
    monkeypatch.setattr(I, "compute_pe_median", lambda f, p, asof: {"asof": asof, "pe_norm_median": 26.9})
    P.freeze_monthly(paths, "2026-10", lambda: {"asof": ASOF, "pe_norm_median": 30.0})   # RM1-5：重凍 asof 不得晚於原凍結日
    cl = {"sec": _Sec(), "alpaca": _Alpaca(), "fred": _Fred()}
    s = I.fetch_inputs(_cfg(tmp_path), paths, conn, ASOF, tmp_path / "o", freeze_pe=True, clients=cl)
    assert s["pe_median"]["pe_norm_median"] == 30.0                      # 未 refreeze：沿用
    s = I.fetch_inputs(_cfg(tmp_path), paths, conn, ASOF, tmp_path / "o2", freeze_pe=True, refreeze=True,
                       clients=cl)
    fz = P._read_frozen(P.freeze_path(paths, "2026-10"))
    assert fz["pe_norm_median"] == 26.9 and fz["n_tickers"] == 2 and fz["universe_source"] == "test_universe"
    assert (paths.state / fz["skill_hashes"]).is_file()
    assert list((paths.cache / "pe_median").glob("2026-10.json.bak-*"))


def test_cli_flags_registered():
    from trader import cli
    ap = cli.build_parser() if hasattr(cli, "build_parser") else None
    if ap is None:
        pytest.skip("cli 無 build_parser")
    assert ap.parse_args(["fetch", "--asof", ASOF, "--refreeze"]).refreeze
    assert ap.parse_args(["screen", "--asof", ASOF, "--force"]).force
    assert ap.parse_args(["bootstrap", "--asof", ASOF, "--force"]).force


# ---------------------------------------------------------------- P08R 返工
def test_screen_for_ignores_bak_dirs(state_dir):
    """M1-D9-01 回歸：force 重跑後 screens/ 下同時有 D 與 D.bak-…，月報母體要挑 D。"""
    from trader.report import validation as V
    paths = get_paths().ensure()
    for name in (ASOF, f"{ASOF}.bak-20261018T053747", f"{ASOF}.bak-20261018T053747-1"):
        d = paths.state / "screens" / name
        d.mkdir(parents=True)
        (d / "screen.csv").write_text("ticker,lane\n")
    assert V._screen_for(paths, "2026-11-01").name == ASOF
    assert V._screen_for(paths, "2026-10-01").name == ASOF      # 都晚於 start → 最早的純日期目錄


def test_refreeze_write_failure_keeps_frozen_file(state_dir, monkeypatch):
    """M1-D4-04 空窗：重凍寫入失敗時，該月凍結檔仍是舊值（不得整月無檔），舊值另有 .bak 備份。"""
    paths = get_paths().ensure()
    P.freeze_monthly(paths, "2026-10", lambda: {"asof": ASOF, "pe_norm_median": 30.0})   # RM1-5：重凍 asof 不得晚於原凍結日

    def boom(path, text):
        raise OSError("disk full")
    monkeypatch.setattr(P, "atomic_write_text", boom)
    with pytest.raises(OSError):
        P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-15", "pe_norm_median": 26.9}, refreeze=True)
    assert P._read_frozen(P.freeze_path(paths, "2026-10"))["pe_norm_median"] == 30.0
    assert P.get_frozen(paths, "2026-10-20")["pe_norm_median"] == 30.0
