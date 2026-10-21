"""RM1-2：M1 複審修復。M1-RS2-01（推得 X1 豁免 hark_b、12 個月期限、hark_b 的 NOT_THIS_SKILL 本機重分析）、
M1-R5-04（手動／觸發代號的初篩資訊帶 data_notes／unrated_fields／dq_note）、M1-R6-02（非短名單代號的 T3／T5
進價格檔，D71）、M1-R6-04（--inputs 無 universe.txt → 股票池來源未知警告）。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from trader import db
from trader.config import load_config
from trader.jobs.engine import Engine
from trader.jobs.sandbox import _has_price_key
from trader.paths import get_paths
from trader.pipelines import screen as SP

import test_engine as TE
import test_fix_P39 as P39

ASOF = P39.ASOF


@pytest.fixture
def env(tmp_path, state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    yield paths, conn, inputs
    conn.close()


# ---------------------------------------------------------------- M1-RS2-01

def test_derived_x1_exempts_hark_b_and_expires(state_dir):
    paths = get_paths().ensure()
    reg = {"AAA": {"status": "REJECT", "origin": "hark_b", "last_full_review_date": "2026-10-10"},
           "BBB": {"status": "NO_ACTION", "last_reason_code": "NOT_THIS_SKILL", "origin": "hark_b",
                   "last_full_review_date": "2026-10-10"},
           "CCC": {"status": "NO_ACTION", "last_reason_code": "NOT_THIS_SKILL", "last_full_review_date": "2026-10-10"}}
    x = SP.build_exclusions(paths, reg, "2026-11-20").set_index("ticker")
    assert "AAA" not in x.index and "BBB" not in x.index        # 他機結論不鎖死（ASK-2＝A）
    assert bool(x.loc["CCC", "excl_asset_class"])                 # 本機的 NOT_THIS_SKILL 仍是 X1
    assert "CCC" not in SP.build_exclusions(paths, reg, "2027-10-10")["ticker"].tolist()   # 滿 12 個月失效
    assert "CCC" not in SP.build_exclusions(paths, reg, "2026-10-10")["ticker"].tolist()   # 非時點不設
    need, why = SP.needs_full(reg["BBB"], "2026-11-20")
    assert need and "他機" in why
    assert SP.needs_full(reg["CCC"], "2026-11-20")[0] is False


# ---------------------------------------------------------------- M1-R5-04、M1-R6-02

def _unlisted(paths, s):
    csv = P39._csv(paths)
    listed = set(s["shortlist"]) | set(s["special_handoff"])
    return csv, [t for t in csv.index if t not in listed and not csv.loc[t, "delete_codes"]]


def test_decisions_carry_notes_and_price_distress_split(env):
    paths, conn, inputs = env
    F, P = P39._skill_make()
    P39._write(inputs, F, P)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    _, cand = _unlisted(paths, s)
    t = cand[0]
    F.loc[F.ticker == t, "data_notes"] = "測試註記"
    P.loc[P.ticker == t, "dd52"] = -0.4                      # 只有價格類困境 T3
    P39._write(inputs, F, P)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, force=True)
    assert "error" not in s, s
    csv = P39._csv(paths)
    assert t not in s["shortlist"] and "T3_" in csv.loc[t, "distress_codes"]
    out = paths.state / "screens" / ASOF
    dec = json.loads((out / SP.DECISIONS).read_text(encoding="utf-8"))
    assert not _has_price_key(dec) and "T3_" not in json.dumps(dec, ensure_ascii=False)
    # R5-04：與短名單交接同樣帶資料註記
    for k in ("data_notes", "unrated_fields", "dq_note"):
        assert all(k in d for d in dec.values())
    ctx = SP.screen_context_for(paths, "2026-10-20", t, "手動")
    assert ctx["data_notes"] == "測試註記" and ctx["unrated_fields"] == str(csv.loc[t, "unrated_fields"])
    # R6-02：價格類困境碼另存，只經 screen_price_context_for 給價格檔
    spc = SP.screen_price_context_for(paths, ctx)
    assert spc and "T3_" in spc["price_distress_codes"]
    assert SP.screen_price_context_for(paths, SP.screen_context_for(paths, "2026-10-20", "ZZZ", "手動")) is None


def _seed_screen(paths, asof_day):
    sd = paths.state / "screens" / asof_day
    sd.mkdir(parents=True)
    (sd / "summary.json").write_text("{}", encoding="utf-8")
    (sd / SP.DECISIONS).write_text(json.dumps(
        {"ADI": {"screen_decision": "PASS", "delete_codes": "", "distress_codes": "", "flag_codes": "",
                 "data_notes": "", "unrated_fields": "", "dq_note": ""}}), encoding="utf-8")
    (sd / SP.DECISIONS_PRICE).write_text(json.dumps({"ADI": "T3_股價距52週高跌逾30%"}, ensure_ascii=False),
                                         encoding="utf-8")


def test_analyze_manual_unlisted_price_distress_goes_to_price_meta(tmp_path, monkeypatch):
    from trader.pipelines.analyze import run_analyze
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    asof = TE.ASOF
    _seed_screen(paths, "2026-10-15")
    sd = paths.state / "screens" / asof
    (sd / "inputs").mkdir(parents=True)
    pd.DataFrame([{"ticker": "ADI", "price": 200.0, "price_date": asof}]).to_csv(sd / "inputs" / "prices.csv",
                                                                                   index=False)
    (sd / "inputs" / "rates.json").write_text(json.dumps({"t30": 0.05, "t30_date": "2026-10-14"}), encoding="utf-8")
    monkeypatch.setattr(Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})
    run_analyze(cfg, paths, conn, asof, tickers=["ADI"], clients={"sec": object(), "fred": None})
    p = json.loads(conn.execute("SELECT params FROM jobs WHERE ticker='ADI'").fetchone()[0])
    conn.close()
    assert p["screen_context"]["screen_decision"] == "PASS"
    assert "T3_" not in json.dumps(p["screen_context"], ensure_ascii=False)
    assert p["price_meta"]["screen_price_context"] == {"price_distress_codes": "T3_股價距52週高跌逾30%"}


def test_review_trigger_full_price_distress_goes_to_price_meta(tmp_path, state_dir, monkeypatch):
    import test_review_pipeline as TR
    from trader.pipelines.review import run_review
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=state_dir).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    _seed_screen(paths, "2026-10-15")
    TR._seed(conn, {"ADI": ("REJECT", {"cik": "0000000011", "last_review_date": "2026-09-01",
                                       "last_full_review_date": "2026-09-01"})})
    filings = {"ADI": [{"form": "8-K", "filingDate": "2026-10-01", "items": "2.06", "url": "https://example.test/8k"}]}
    cl = {"alpaca": TR.FakeAlpaca({"ADI": TR.CLOSE}), "fred": TR.FakeFred(), "sec": TR.FakeSec({})}
    monkeypatch.setattr(Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})
    run_review(cfg, paths, conn, TE.ASOF, scope="watch", clients=cl, offline_filings=TR._offline(tmp_path, filings))
    rows = conn.execute("SELECT job_type, params FROM jobs WHERE ticker='ADI'").fetchall()
    conn.close()
    p = next(json.loads(r["params"]) for r in rows if r["job_type"] == "DOSSIER")
    assert "T3_" not in json.dumps(p["screen_context"], ensure_ascii=False)
    assert p["price_meta"]["screen_price_context"] == {"price_distress_codes": "T3_股價距52週高跌逾30%"}


# ---------------------------------------------------------------- M1-R6-04

def test_inputs_without_universe_file_warns_unknown_source(env):
    paths, conn, inputs = env
    F, P = P39._skill_make()
    P39._write(inputs, F, P)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    assert s["universe_source"] is None
    assert "股票池來源未知" in s.get("warning", "")
