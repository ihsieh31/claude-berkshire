"""P38：初篩交接與報告（M1-E1）＋D20（REIT 非金融）＋D66（太難型上限）＋D71（T3／T5 移到 price_context）。全離線。"""
from __future__ import annotations

import importlib
import json
import sys

import numpy as np
import pytest

from trader import db
from trader.jobs.sandbox import _has_price_key
from trader.paths import get_paths
from trader.pipelines import screen as SP

ASOF = "2026-10-15"


def _skill(module: str):
    d = str(get_paths().skill_screen / "scripts")
    if d not in sys.path:
        sys.path.insert(0, d)
    return importlib.import_module(module)


@pytest.fixture(scope="module")
def S():
    return _skill("screen")


@pytest.fixture(scope="module")
def T():
    return _skill("test_screen")


def _run(S, F, P, top=15, special_max=0, **kw):
    df = S.structural(S.build(F, P, None, ASOF), ASOF)
    return S.statistical(df, top, special_max, **kw)[0]


# ---------------------------------------------------------------- D20：REIT 歸非金融組

def test_reit_counts_as_non_financial(S, T):
    F, P = T.make()
    F.loc[90, ["sic", "is_financial"]] = [6798, True]     # REIT
    F.loc[91, ["sic", "is_financial"]] = [6022, True]     # 銀行
    df = _run(S, F, P).set_index("ticker")
    assert df.loc["T090", "fin_like"] == 0 and df.loc["T091", "fin_like"] == 1


# ---------------------------------------------------------------- D66：前 15 名太難型最多 4 檔

def test_too_hard_type_mapping(S):
    assert S.too_hard_type(5311) == "零售" and S.too_hard_type("6798") == "REIT" and S.too_hard_type(3674.0)
    assert S.too_hard_type(5812) == "" and S.too_hard_type(1531) == "" and S.too_hard_type(None) == ""
    assert S.too_hard_type(6022) == "" and S.too_hard_type(2834) == ""


def test_too_hard_cap_backfills(S, T):
    F, P = T.make()
    base = _run(S, F, P).sort_values("core_rank")
    hard = list(base.ticker.head(6))                       # 原前 6 名都改成零售
    F.loc[F.ticker.isin(hard), "sic"] = 5311
    df = _run(S, F, P).set_index("ticker")
    sl = df[df.shortlist]
    assert len(sl) == 15 and (sl.sel050_type != "").sum() == 4
    assert [t for t in hard if df.loc[t, "shortlist"]] == hard[:4]          # 名次高的先保留
    assert df.loc[hard[4], "core_rank"] == 16 and df.loc[hard[5], "core_rank"] == 17
    assert sorted(df.core_rank.dropna()) == list(range(1, int(df.core_rank.max()) + 1))
    nocap = _run(S, F, P, too_hard_cap=99).set_index("ticker")             # 不設上限 → 原名單
    assert set(nocap[nocap.shortlist].index) == set(base.ticker.head(15))


# ---------------------------------------------------------------- D71：T3／T5 只放 price_context

def test_price_distress_codes_moved_to_price_context(S, T, tmp_path):
    F, P = T.make()
    t = _run(S, F, P).sort_values("core_rank").ticker.iloc[0]
    P.loc[P.ticker == t, "dd52"] = -0.4                    # 只有 T3（股價距 52 週高跌逾 30%）
    df = _run(S, F, P)
    assert "T3_" in df.set_index("ticker").loc[t, "distress_codes"]          # 全池 csv 照留
    S.handoff(df, ASOF, str(tmp_path / "r"))
    h = json.loads((tmp_path / "r_handoff.json").read_text(encoding="utf-8"))
    x = next(r for r in h["core_shortlist"] if r["ticker"] == t)
    assert x["distress_codes"] == "" and x["screen_decision"] == "PASS"
    assert "T3_" in x["price_context"]["price_distress_codes"]
    assert h["formula_version"] == S.FORMULA_VERSION


# ---------------------------------------------------------------- check-P13 建議：同日有／無係數的分拆事件排序

def test_spinoff_events_same_day_with_and_without_factor(S):
    ev = S.spinoff_events("2026-06-01:1.241", "2026-06-01")
    assert ev == [("2026-06-01", None), ("2026-06-01", "1.241")]


# ---------------------------------------------------------------- M1-D5-05／D66：pipeline summary

def test_pipeline_summary_has_deleted_lacking_formula(tmp_path, state_dir, S, T):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    F, P = T.make()
    t = _run(S, F, P).sort_values("core_rank").ticker.iloc[0]
    P.loc[P.ticker == t, "vol_36m"] = np.nan
    F.to_csv(inputs / "features.csv", index=False)
    P.to_csv(inputs / "prices.csv", index=False)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    conn.close()
    assert "error" not in s, s
    assert s["formula_version"]
    assert {"ticker": "T000", "codes": "J1_持續虧損;J7_連虧且累虧"} in s["deleted"]
    assert s["lacking"] == [{"ticker": t, "missing": ["vol_36m"], "data_notes": "", "dq_note": ""}]   # RM1-9：lacking 帶 dq_note
    nop = json.loads((paths.state / "screens" / ASOF / "handoff_noprice.json").read_text(encoding="utf-8"))
    assert not _has_price_key(nop)
    assert all("unrated_fields" in r and "data_notes" in r for r in nop["core_shortlist"])
