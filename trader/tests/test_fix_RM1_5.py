"""RM1-5：複審 M1 修復——M1-R4-01（重凍不得晚於原凍結日）、M1-D10-01（金融 P/E 中位改最近年報口徑）、
M1-RS3-01（REIT 改標非金融：訊號卡／登錄表／相關群一致）。合成資料與 FakeRunner，不連網。"""
import argparse
import json

import pytest

from test_engine import CAP_INPUT, RUN, dossier_ok, full_a, full_b, make_env, red_team
from test_fix_P37 import _synth
from trader import cli
from trader.analysis.writeback import _registry_get
from trader.data import pe_median as P
from trader.jobs.deep import _pe_for_cap
from trader.paths import get_paths


@pytest.fixture
def paths(tmp_path):
    p = get_paths(tmp_path / "state", root=tmp_path)
    p.ensure()
    return p


# ---------------------------------------------------------------- M1-R4-01

def test_refreeze_with_later_asof_refused_and_old_file_kept(paths):
    P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-01", "pe_ttm_median": 30.0})
    with pytest.raises(ValueError, match="--asof 2026-10-01"):
        P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-19", "pe_ttm_median": 31.0}, refreeze=True)
    assert P.get_frozen(paths, "2026-10-15")["pe_ttm_median"] == 30.0   # 修前：被覆寫成 10-19 → 10-15 查不到（None）
    assert not list(P.freeze_path(paths, "2026-10").parent.glob("*.bak-*"))


def test_refreeze_with_same_asof_still_works(paths):
    P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-15", "pe_ttm_median": 30.0})
    out = P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-15", "pe_ttm_median": 31.0}, refreeze=True)
    assert out["pe_ttm_median"] == 31.0 and P.get_frozen(paths, "2026-10-15")["pe_ttm_median"] == 31.0


def test_cli_refreeze_later_asof_fails_before_fetch(paths, monkeypatch, capsys):
    P.freeze_monthly(paths, "2026-10", lambda: {"asof": "2026-10-15", "pe_ttm_median": 30.0})
    monkeypatch.setattr(cli, "_load_or_report", lambda *_: object())

    def boom(*a, **k):
        raise AssertionError("不應開始抓資料")
    monkeypatch.setattr("trader.data.inputs.fetch_inputs", boom)
    args = argparse.Namespace(config=None, state_dir=str(paths.state), asof="2026-10-19", refreeze=True,
                              freeze_pe=False, tickers=None, out=None)
    assert cli.cmd_fetch(args) == 1
    assert "--asof 2026-10-15" in capsys.readouterr().out


# ---------------------------------------------------------------- M1-D10-01

def test_fin_pe_ttm_median_computed_excluding_reit():
    f, p = _synth(n=80, fin=50, reit=10)   # pe_ttm 一律 15
    r = P.compute_pe_median(f, p, "2026-10-15")
    assert r["n_fin_ttm"] == 40 and r["fin_pe_ttm_median"] == pytest.approx(15.0)
    f2, p2 = _synth(n=80, fin=35, reit=10)  # 金融 25 檔 < 30
    assert P.compute_pe_median(f2, p2, "2026-10-15")["fin_pe_ttm_median"] is None


def test_pe_for_cap_uses_ttm_financial_median():
    lc, fin, field = _pe_for_cap({"pe_ttm_median": 26.22, "fin_pe_norm_median": 18.11, "fin_pe_ttm_median": 16.41})
    assert (lc, fin) == (26.22, 16.41)
    assert field["fin_pe_median"] == "fin_pe_ttm_median" and "fin_pe_fallback" not in field


def test_pe_for_cap_old_frozen_file_falls_back_and_says_so():
    lc, fin, field = _pe_for_cap({"pe_ttm_median": 26.22, "fin_pe_norm_median": 18.11})
    assert (lc, fin) == (26.22, 18.11)
    assert field["fin_pe_median"] == "fin_pe_norm_median" and "fin_pe_ttm_median" in field["fin_pe_fallback"]


def test_state_2026_10_frozen_file_has_ttm_financial_median():
    v = P._read_frozen(P.freeze_path(get_paths(), "2026-10"))
    assert v is not None and v["asof"] == "2026-10-15"
    assert isinstance(v.get("fin_pe_ttm_median"), float) and v["n_fin_ttm"] >= P.MIN_SAMPLE


# ---------------------------------------------------------------- M1-RS3-01

def _reit_env(tmp_path, fa):
    tmp_path.mkdir(parents=True, exist_ok=True)
    b = full_b("BUY")
    b["files"] = {**b["files"], "KO/cap_input.json": json.dumps({**CAP_INPUT, "financial": True})}
    env = make_env(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": fa, "FULL_B": b, "RED_TEAM": red_team(False)})
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    td = env.paths.work / RUN / "KO"
    (td / "dossier" / "_meta.json").write_text(json.dumps({"sic": "6798", "industry": None}), encoding="utf-8")
    return env, td


def test_reit_relabelled_everywhere(tmp_path):
    a = full_a()
    a["asset_class"] = "其他金融"
    env, td = _reit_env(tmp_path, a)
    assert env.engine.run(RUN)["status"] == "done"
    pa = json.loads((td / "phaseA.json").read_text(encoding="utf-8"))
    assert pa["asset_class"] == "非金融" and pa["asset_class_model"] == "其他金融"
    assert json.loads((td / "cap_input.json").read_text(encoding="utf-8"))["financial"] is False
    card = json.loads((td / "signal_card.json").read_text(encoding="utf-8"))
    assert card["asset_class"] == "非金融"   # 修前：其他金融 → correlation.is_financial True、減損 0.5
    reg = _registry_get(env.conn, "KO")
    assert reg["data"]["asset_class"] == "非金融"
    notes = json.loads(env.conn.execute("SELECT data FROM decisions").fetchone()["data"])["framework_notes"]
    assert any("REIT 歸非金融" in n for n in notes)


def test_reit_relabelled_when_full_a_stops(tmp_path):
    a = full_a(stopped=True)
    a["asset_class"] = "其他金融"
    env, td = _reit_env(tmp_path, a)
    env.engine.run(RUN)
    assert json.loads((td / "phaseA.json").read_text(encoding="utf-8"))["asset_class"] == "非金融"
    assert _registry_get(env.conn, "KO")["data"]["asset_class"] == "非金融"


def test_non_reit_financial_label_untouched(tmp_path):
    a = full_a()
    a["asset_class"] = "其他金融"
    env, td = _reit_env(tmp_path, a)
    (td / "dossier" / "_meta.json").write_text(json.dumps({"sic": "6211", "industry": None}), encoding="utf-8")
    env.engine.run(RUN)
    pa = json.loads((td / "phaseA.json").read_text(encoding="utf-8"))
    assert pa["asset_class"] == "其他金融" and "asset_class_model" not in pa
