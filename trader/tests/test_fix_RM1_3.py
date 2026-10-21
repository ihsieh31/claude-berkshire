"""RM1-3（M1-RS8-01）：同 asof 初選 --force 重跑後，bootstrap 的分析改用新 run_id，真的重做；
同 run 已完成而未重做的代號列入 not_rerun，bootstrap 印 [WARN]。"""
import argparse
import json
import types

from trader import cli, db
from trader.config import load_config
from trader.llm.fake import FakeRunner
from trader.llm.router import Router
from trader.paths import get_paths
from trader.pipelines import bootstrap as B
from trader.pipelines.analyze import run_analyze

import test_analyze_pipeline as TA
import test_engine as TE

ASOF = TE.ASOF
STAMP = "20261019T010000"


def _fake_screen(calls):
    def run_screen(cfg, paths, conn, asof, **kw):
        calls.append(kw.get("force"))
        return {"run_id": f"screen-{asof}", "to_analyze": [{"ticker": "KO"}]}
    return run_screen


def test_bootstrap_run_id_only_new_when_forced(monkeypatch):
    got, scr = [], []
    monkeypatch.setattr(B, "run_screen", _fake_screen(scr))
    monkeypatch.setattr(B, "run_analyze", lambda *a, **kw: got.append(kw.get("run_id")) or {"run_id": kw.get("run_id")})
    monkeypatch.setattr(B, "time", types.SimpleNamespace(strftime=lambda fmt: STAMP), raising=False)
    B.run_bootstrap(None, None, None, ASOF)                        # 一般初選（排程走這條）：預設 analyze-{asof}
    B.run_bootstrap(None, None, None, ASOF, force=True)            # --force 重跑初選：新 run_id
    B.run_bootstrap(None, None, None, ASOF, tickers=["KO"], force=True)   # --tickers 不重跑初選：不變
    assert got == [None, f"analyze-{ASOF}-r{STAMP}", None]
    assert scr == [None, True]


def _prep(paths, run):
    """模擬 DOSSIER 前置已完成（同 test_analyze_pipeline；真的前置需要 SEC 連網）。"""
    td = paths.work / run / "KO"
    (td / "dossier").mkdir(parents=True)
    (td / "sec").mkdir()
    (td / "dossier" / ".prep_done").write_text("ok\n", encoding="utf-8")
    (td / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    (td / "dossier" / "KO_price.json").write_text(json.dumps({"price": 60.5}), encoding="utf-8")


def _after_first_run(tmp_path, monkeypatch):
    TA.test_run_analyze_from_screen(tmp_path, monkeypatch)   # 第一輪（r2）：KO 分析完成
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    return cfg, paths, conn


def test_forced_bootstrap_reanalyzes(tmp_path, monkeypatch):
    """修前：bootstrap --force 沿用 analyze-{asof}，added=[]、模型 0 次、回傳上一輪決策。"""
    cfg, paths, conn = _after_first_run(tmp_path, monkeypatch)
    monkeypatch.setattr(B, "run_screen", _fake_screen([]))
    monkeypatch.setattr(B, "time", types.SimpleNamespace(strftime=lambda fmt: STAMP), raising=False)
    new_run = f"analyze-{ASOF}-r{STAMP}"
    _prep(paths, new_run)
    fake = FakeRunner(responses=TE.full_responses())
    router = Router(cfg, paths.routing_json, dry_run=True, fake=fake)
    out = B.run_bootstrap(cfg, paths, conn, ASOF, force=True, clients={"sec": object()}, router=router)
    ana = out["analyze"]
    assert ana["run_id"] == new_run
    assert ana["added"] == ["KO"] and ana["not_rerun"] == [] and len(fake.calls) > 0
    assert ana["engine"]["failed"] == 0 and ana["decisions"]["KO"] is not None
    # 舊 run 保留不動
    assert conn.execute("SELECT status FROM runs WHERE run_id=?", (f"analyze-{ASOF}",)).fetchone()[0] == "done"
    conn.close()


def test_same_run_reports_not_rerun(tmp_path, monkeypatch):
    """單獨 analyze 同 run_id（續跑語意不變）：已完成的不重做，但列入 not_rerun。"""
    cfg, paths, conn = _after_first_run(tmp_path, monkeypatch)
    fake = FakeRunner(responses=TE.full_responses())
    router = Router(cfg, paths.routing_json, dry_run=True, fake=fake)
    out = run_analyze(cfg, paths, conn, ASOF, clients={"sec": object()}, router=router)
    assert out["run_id"] == f"analyze-{ASOF}" and out["added"] == [] and out["not_rerun"] == ["KO"]
    assert len(fake.calls) == 0
    conn.close()


def test_cli_bootstrap_warns_not_rerun(tmp_path, monkeypatch, capsys):
    import trader.pipelines.bootstrap as mod
    monkeypatch.setattr(cli, "_load_or_report", lambda c: object())
    monkeypatch.setattr(mod, "run_bootstrap", lambda *a, **kw: {
        "asof": ASOF, "tickers": ["KO"],
        "analyze": {"run_id": f"analyze-{ASOF}", "not_rerun": ["KO"], "engine": {"done": 4, "failed": 0}}})
    args = argparse.Namespace(config=None, state_dir=str(tmp_path / "state"), asof=ASOF, tickers="KO", top=15,
                              dry_run=False, force=False, mark_screened=False)
    assert cli.cmd_bootstrap(args) == 0
    o = capsys.readouterr().out
    assert "[WARN]" in o and f"analyze-{ASOF} 已分析過、本次未重做：KO" in o
