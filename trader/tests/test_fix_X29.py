"""X29（P34/P40 交接）：review 觸發的資料包利率段補 3 個月國庫券與多年平均；test_fix_P42 在 A 機無 config.hark.yaml 時不失敗。"""
import json
import shutil

import pytest

import test_fix_P42 as P42
from test_review_pipeline import ASOF, FakeFred, _clients, _jobs, _offline, _seed, env, no_engine  # noqa: F401
from trader.analysis.dossier_prep import rates_section
from trader.config import PROJECT_DIR
from trader.pipelines.review import run_review


class _Fred(FakeFred):
    def latest(self, sid, asof):
        assert sid == "DGS3MO" and asof == ASOF
        return 3.9, "2026-10-14"

    def mean(self, sid, asof, years):
        return {("DGS30", 5): 4.0, ("DGS30", 10): 3.3, ("DGS3MO", 5): 2.5, ("DGS3MO", 10): 1.5}[(sid, years)]


class _NoT30(_Fred):
    def t30(self, asof):
        raise RuntimeError("FRED 暫時不可用")


class _No3m(_Fred):
    def latest(self, sid, asof):
        raise RuntimeError("DGS3MO 無資料")


def _rates(conn, s, ticker="FUL"):
    return json.loads([r for r in _jobs(conn, s["run_id"]) if r["ticker"] == ticker][0]["params"])["rates"]


def test_review_rates_have_t3m_and_means(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(fred=_Fred()), offline_filings=_offline(tmp))
    r = _rates(conn, s)
    assert r["t30"] == 0.047 and r["t30_20y_median"] == 0.034            # 原有三鍵不變
    assert r["t3m"] == pytest.approx(0.039) and r["t3m_date"] == "2026-10-14"
    assert r["t30_mean_5y"] == pytest.approx(0.04) and r["t30_mean_10y"] == pytest.approx(0.033)
    assert r["t3m_mean_5y"] == pytest.approx(0.025) and r["t3m_mean_10y"] == pytest.approx(0.015)
    sec = rates_section(r)
    assert not any(x in sec for x in ("：缺", " 缺", "（缺）", "- 缺")) and "3.90%" in sec and "1.50%" in sec
    assert s["t30"] == {"t30": 0.047, "t30_date": "2026-10-15"}           # summary 口徑不變


def test_review_t30_failure_still_carries_t3m(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(fred=_NoT30()), offline_filings=_offline(tmp))
    assert "FRED" in s["t30"]["error"] and "error" not in s
    r = _rates(conn, s)
    assert "t30" not in r and "FRED" in r["error"]
    assert r["t3m"] == pytest.approx(0.039) and r["t30_mean_5y"] == pytest.approx(0.04)
    sec = rates_section(r)
    assert "3.90%" in sec and "缺：RuntimeError: FRED" in sec


def test_review_extra_failure_only_marks_that_item(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients=_clients(fred=_No3m()), offline_filings=_offline(tmp))
    r = _rates(conn, s)
    assert r["t30"] == 0.047 and r["t30_mean_10y"] == pytest.approx(0.033) and "t30_extra_error" not in r
    assert "t3m" not in r and "DGS3MO 無資料" in r["t3m_extra_error"]
    assert "缺：RuntimeError: DGS3MO 無資料" in rates_section(r)


def test_p42_config_test_passes_without_hark_yaml(tmp_path, monkeypatch):
    """A 機：config.hark.yaml 被 .gitignore 排除；P42 的設定測試應照跑其餘斷言而不失敗。"""
    for name in ("config.yaml", "config.example.yaml"):
        shutil.copy(PROJECT_DIR / name, tmp_path / name)
    monkeypatch.setattr(P42, "PROJECT_DIR", tmp_path)
    (tmp_path / "t").mkdir()
    P42.test_config_research_price_source_defaults_and_validation(tmp_path / "t")


def test_p42_config_test_still_strict_when_hark_yaml_present(tmp_path, monkeypatch):
    for name in ("config.yaml", "config.example.yaml"):
        shutil.copy(PROJECT_DIR / name, tmp_path / name)
    shutil.copy(PROJECT_DIR / "config.example.yaml", tmp_path / "config.hark.yaml")   # 有檔但不是 yahoo → 仍須失敗
    monkeypatch.setattr(P42, "PROJECT_DIR", tmp_path)
    (tmp_path / "t").mkdir()
    with pytest.raises(AssertionError):
        P42.test_config_research_price_source_defaults_and_validation(tmp_path / "t")
