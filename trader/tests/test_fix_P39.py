"""P39：初篩宿主端排除、回寫與多類股（M1-E2）＋D62（刪除複核與撤銷）＋D71（價格類困境碼分流）。全離線。"""
from __future__ import annotations

import importlib.util
import json

import numpy as np
import pandas as pd
import pytest

from trader import db
from trader.config import load_config
from trader.jobs.engine import Engine
from trader.jobs.sandbox import _has_price_key
from trader.paths import get_paths
from trader.pipelines import screen as SP

import test_engine as TE
import test_xbrl_shares as TX

ASOF = "2026-10-15"   # 與 skill test_screen.py 的合成資料同一決策日


def _skill_make():
    p = get_paths().skill_screen / "scripts" / "test_screen.py"
    spec = importlib.util.spec_from_file_location("skill_test_screen_p39", p)
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
    yield paths, conn, inputs
    conn.close()


def _write(inputs, F, P):
    F.to_csv(inputs / "features.csv", index=False)
    P.to_csv(inputs / "prices.csv", index=False)


def _csv(paths):
    return pd.read_csv(paths.state / "screens" / ASOF / "screen.csv", dtype={"ticker": str}).fillna("").set_index("ticker")


# ---------------------------------------------------------------- M1-D7-1：多類股的排除旗標不得因去重失效

def test_dropped_share_class_exclusion_carries_to_kept_class(env):
    paths, conn, inputs = env
    F, P = _skill_make()
    F["cik"] = [f"{i:010d}" for i in range(len(F))]
    F.loc[F.ticker == "T010", "cik"] = F.loc[F.ticker == "T011", "cik"].iloc[0]   # 同 CIK 兩類：留字母序最後 T011
    _write(inputs, F, P)
    (paths.state / "exclusions.csv").write_text("ticker,excl_fraud\nT010,1\n", encoding="utf-8")
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    assert {"ticker": "T010", "kept": "T011", "cik": F.loc[F.ticker == "T011", "cik"].iloc[0]} in s["share_class_dedup"]
    assert "X4_誠信紅旗" in _csv(paths).loc["T011", "delete_codes"]
    assert "T011" not in s["shortlist"]


def test_build_exclusions_duplicate_rows_or(state_dir):
    paths = get_paths().ensure()
    (paths.state / "exclusions.csv").write_text("ticker,excl_fraud,excl_competence\nAAA,1,\nAAA,,1\n", encoding="utf-8")
    ex = SP.build_exclusions(paths, {}, ASOF).set_index("ticker")
    assert ex.at["AAA", "excl_fraud"] and ex.at["AAA", "excl_competence"]     # 後列不覆寫前列
    merged = SP.merge_share_class_exclusions(ex.reset_index(), [{"ticker": "AAA", "kept": "AAAB"}]).set_index("ticker")
    assert list(merged.index) == ["AAAB"] and merged.at["AAAB", "excl_fraud"]


# ---------------------------------------------------------------- D62：值得複核的刪除＋撤銷檔

def test_overrides_withdraw_structural_delete_and_review_list(env):
    paths, conn, inputs = env
    F, P = _skill_make()
    F.loc[F.ticker == "T000", "is_financial"] = True          # 金融股被刪 → 列入值得複核
    _write(inputs, F, P)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    rv = {r["ticker"]: r for r in s["deletions_to_review"]}
    assert "金融" in rv["T000"]["why"]
    (paths.state / "screen_overrides.csv").write_text(
        "ticker,date,reason\nT000,2026-10-10,人工核對：一次性減損\nT001,2026-10-20,晚於 asof 不生效\n", encoding="utf-8")
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, force=True)
    df = _csv(paths)
    assert df.loc["T000", "delete_codes"] == "" and "撤銷 J1_持續虧損" in df.loc["T000", "override_note"]
    assert s["overrides"] == [{"ticker": "T000", "note": df.loc["T000", "override_note"]}]
    assert "T000" not in {d["ticker"] for d in s["deleted"]}
    md = (paths.state / "screens" / ASOF / "screen.md").read_text(encoding="utf-8")
    assert "已撤銷的刪除（1 檔" in md


# ---------------------------------------------------------------- M1-D5-06／D5-08：summary 溯源欄位與 X5 清單

def test_summary_provenance_and_x5_list(env):
    paths, conn, inputs = env
    F, P = _skill_make()
    _write(inputs, F, P)
    (inputs / "universe.txt").write_text("# source=universe_versions:2026-10-01\n" + "\n".join(F.ticker) + "\n",
                                         encoding="utf-8")
    db.upsert(conn, "registry", {"ticker": "T050", "status": "REJECT", "updated_at": db.now_iso(),
                                 "data": {"status": "REJECT", "last_full_review_date": "2026-10-05"}}, key=["ticker"])
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    assert s["universe_source"] == "universe_versions:2026-10-01" and s["n_universe"] == len(F)
    assert s["top"] == 15 and s["special_max"] == 0
    assert s["skill_hashes"].startswith("skill_hashes/") and (paths.state / s["skill_hashes"]).is_file()
    assert s["x5"] == [{"ticker": "T050", "status": "REJECT", "last_full_review_date": "2026-10-05",
                        "expires": "2027-10-05"}]
    md = (paths.state / "screens" / ASOF / "screen.md").read_text(encoding="utf-8").splitlines()
    assert md[2].startswith("股票池：universe_versions:2026-10-01，120 檔")


def test_no_exclusions_leaves_no_exclusions_csv(env):
    paths, conn, inputs = env
    F, P = _skill_make()
    _write(inputs, F, P)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s and s["x5"] == [] and s["exclusions"] == {}
    assert not (paths.state / "screens" / ASOF / "exclusions.csv").exists()


# ---------------------------------------------------------------- D71：價格類困境碼與道名不進前半

def test_noprice_handoff_has_no_price_distress_lane_or_watch(env):
    paths, conn, inputs = env
    F, P = _skill_make()
    _write(inputs, F, P)
    first = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)["shortlist"][0]
    P.loc[P.ticker == first, "dd52"] = -0.4                 # 只有 T3
    _write(inputs, F, P)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, force=True, special_max=2)
    assert "error" not in s, s
    out = paths.state / "screens" / ASOF
    hand = json.loads((out / "screen_handoff.json").read_text(encoding="utf-8"))
    x = next(r for r in hand["core_shortlist"] if r["ticker"] == first)
    assert "T3_" in x["price_context"]["price_distress_codes"] and x["price_context"]["lane"] == "核心道"
    nop = json.loads((out / "handoff_noprice.json").read_text(encoding="utf-8"))
    assert "special_watch" not in nop and not _has_price_key(nop)
    for r in nop["core_shortlist"] + nop["special_handoff"]:
        assert "lane" not in r and "exclude_reasons" not in r and "T3_" not in r["distress_codes"]
    assert next(r for r in nop["core_shortlist"] if r["ticker"] == first)["screen_decision"] == "PASS"


# ---------------------------------------------------------------- M1-D7-4：非短名單代號的 screen_decision

def test_decisions_json_and_context_for_unlisted_ticker(env):
    paths, conn, inputs = env
    F, P = _skill_make()
    _write(inputs, F, P)
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    dec = json.loads((paths.state / "screens" / ASOF / "decisions.json").read_text(encoding="utf-8"))
    assert not _has_price_key(dec) and len(dec) == len(F)
    assert all(not c.startswith(("T3_", "T5_")) for d in dec.values() for c in d["distress_codes"].split(";") if c)
    ctx = SP.screen_context_for(paths, "2026-10-20", "T000", "手動")
    assert ctx["screen_decision"].startswith("DELETE") and ctx["screen_asof"] == ASOF
    assert SP.screen_context_for(paths, "2026-10-20", "T001", "手動")["screen_decision"] == "PASS-困境"
    assert SP.screen_context_for(paths, "2026-10-20", "ZZZ", "手動")["screen_decision"] == "未篩"
    assert SP.screen_context_for(paths, "2026-10-14", "T000", "手動")["screen_decision"] == "未篩"   # 時點：不用未來初篩
    assert s["shortlist"]


def test_analyze_unlisted_ticker_gets_delete_and_price_distress_goes_to_price_meta(tmp_path, monkeypatch):
    from trader.pipelines.analyze import run_analyze
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    asof = TE.ASOF
    sd = paths.state / "screens" / asof
    (sd / "inputs").mkdir(parents=True)
    (sd / "summary.json").write_text(json.dumps({"to_analyze": [{"ticker": "KO"}]}), encoding="utf-8")
    (sd / "handoff_noprice.json").write_text(json.dumps(
        {"core_shortlist": [{"ticker": "KO", "screen_decision": "PASS", "distress_codes": ""}],
         "special_handoff": []}), encoding="utf-8")
    (sd / "screen_handoff.json").write_text(json.dumps(
        {"core_shortlist": [{"ticker": "KO", "price_context": {"price": 60.5, "price_distress_codes": "T3_股價距52週高跌逾30%"}}],
         "special_handoff": []}, ensure_ascii=False), encoding="utf-8")
    (sd / "decisions.json").write_text(json.dumps(
        {"BAD": {"screen_decision": "DELETE（J1_持續虧損）", "delete_codes": "J1_持續虧損", "distress_codes": "",
                 "flag_codes": ""}}, ensure_ascii=False), encoding="utf-8")
    pd.DataFrame([{"ticker": t, "price": 60.5, "price_date": asof} for t in ("KO", "BAD")]).to_csv(
        sd / "inputs" / "prices.csv", index=False)
    (sd / "inputs" / "rates.json").write_text(json.dumps({"t30": 0.05, "t30_date": "2026-10-14"}), encoding="utf-8")
    monkeypatch.setattr(Engine, "run", lambda self, run_id: {"status": "done", "done": 0, "failed": 0})
    run_analyze(cfg, paths, conn, asof, tickers=["KO", "BAD"], clients={"sec": object(), "fred": None})
    p = {r["ticker"]: json.loads(r["params"]) for r in conn.execute("SELECT ticker, params FROM jobs")}
    conn.close()
    assert p["BAD"]["screen_context"]["screen_decision"].startswith("DELETE")
    assert p["KO"]["screen_context"]["screen_decision"] == "PASS"
    assert "T3_" not in json.dumps(p["KO"]["screen_context"], ensure_ascii=False)
    assert p["KO"]["price_meta"]["screen_price_context"] == {"price_distress_codes": "T3_股價距52週高跌逾30%"}
    assert "screen_price_context" not in p["BAD"]["price_meta"]


def test_x5_is_not_gate1_delete_but_x4_is(env):
    """P39R：X5 只擋初選再選入；被觸發或手動重評的代號 screen_decision 不得因 X5 為 DELETE（delete_codes 仍保留 X5）。"""
    from trader.analysis.checks import cap_signal_by_gates
    paths, conn, inputs = env
    F, P = _skill_make()
    _write(inputs, F, P)
    db.upsert(conn, "registry", {"ticker": "T050", "status": "REJECT", "updated_at": db.now_iso(),
                                 "data": {"status": "REJECT", "last_full_review_date": "2026-10-05"}}, key=["ticker"])
    (paths.state / "exclusions.csv").write_text("ticker,excl_fraud\nT051,1\n", encoding="utf-8")
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    x5 = SP.screen_context_for(paths, "2026-10-20", "T050", "觸發檢查")
    assert "X5_近期已淘汰" in x5["delete_codes"]
    assert not x5["screen_decision"].startswith("DELETE") and "DELETE" not in x5["screen_decision"]
    assert cap_signal_by_gates("BUY", {"gate3_pass": True}, x5["screen_decision"])[0] == "BUY"
    x4 = SP.screen_context_for(paths, "2026-10-20", "T051", "觸發檢查")
    assert x4["screen_decision"].startswith("DELETE") and "X4_誠信紅旗" in x4["screen_decision"]
    assert SP._decision("X5_近期已淘汰;X4_誠信紅旗", "") == "DELETE（X4_誠信紅旗）"
    assert SP._decision("X5_近期已淘汰", "T1_x") == "PASS-困境"


@pytest.mark.parametrize("card_screen,gate1", [("DELETE（J1_持續虧損）", False), ("PASS", True)])
def test_price_review_unscreened_falls_back_to_card(state_dir, card_screen, gate1):
    """review_jobs PRICE：screen_context 標「未篩」時沿用上次訊號卡的 screen_decision（不得因此當作通過閘門 1）。"""
    import test_review_jobs as TRJ
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    TRJ._write_cap_input(paths)
    TRJ._seed_registry(conn, "KO", "WATCH", {"grade": "B"})
    TRJ._gate3_row(conn)
    db.insert(conn, "signals", {"decision_id": "KO:2026-09-01:full", "ticker": "KO", "signal": "WATCH",
                                "data": {"signal": "WATCH", "as_of": "2026-09-01", "screen_decision": card_screen},
                                "created_at": "2026-09-01T00:00:00Z"})
    result = TRJ._price_result(proposed_action="NO_CHANGE")
    TRJ._run_price(paths, conn, TRJ._price_params(price=TRJ._auto_cap() * 0.8,
                                                  screen_context={"screen_decision": "未篩"}), result)
    conn.close()
    assert result["framework_gates"]["gate1"] is gate1


# ---------------------------------------------------------------- M1-D2-04：淘汰類由兩週排程檢查新事實

def test_watch_review_reject_full_on_material_8k_none_on_10k(tmp_path, state_dir):
    import test_review_pipeline as TR
    from trader.pipelines.review import run_review
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=state_dir).ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    seed = {"RJK": ("REJECT", {"cik": "0000000011", "last_review_date": "2026-09-01",
                               "last_full_review_date": "2026-09-01"}),
            "RJQ": ("REJECT", {"cik": "0000000012", "last_review_date": "2026-09-01",
                               "last_full_review_date": "2026-09-01"})}
    TR._seed(conn, seed)
    # D27／P-62：淘汰類只看重大 8-K（2.06 重大減損）；新年報不重評（verified 測試寫 10-K→FULL，以較新的 D27 為準）
    filings = {"RJK": [{"form": "8-K", "filingDate": "2026-10-01", "items": "2.06", "url": "https://example.test/8k"}],
               "RJQ": [{"form": "10-K", "filingDate": "2026-10-02", "items": "", "url": ""}]}
    cl = {"alpaca": TR.FakeAlpaca({t: TR.CLOSE for t in seed}), "fred": TR.FakeFred(), "sec": TR.FakeSec({})}
    s = run_review(cfg, paths, conn, TE.ASOF, scope="watch", clients=cl, offline_filings=TR._offline(tmp_path, filings),
                   due_only=True)
    due = json.loads((paths.work / s["run_id"] / "due.json").read_text(encoding="utf-8"))
    conn.close()
    act = {r["ticker"]: r["action"] for r in due["results"]}
    assert act == {"RJK": "FULL", "RJQ": "NONE"}


# ---------------------------------------------------------------- M1-D7-6／D5-04：多類股股數換算

def _amend_xml_no_eps() -> str:
    """只修正封面（無 EPS、無類別股數）的 10-K/A。"""
    return TX.instance([TX.inst_ctx("i", "2026-09-01")], [TX.fact("dei:DocumentType", "i", "10-K/A")])


def test_latest_amendment_without_eps_falls_back_to_previous_filing(tmp_path):
    acc = "0001067983-26-000020"
    sec = TX._make_sec(tmp_path, extra_routes=TX._routes_for(TX.B_CIK, acc, _amend_xml_no_eps()))
    sec._filings[TX.B_CIK].append({"form": "10-K/A", "filingDate": "2026-09-10", "accessionNumber": acc,
                                   "primaryDocument": "a.htm"})
    prices = pd.DataFrame({"ticker": ["BRK.B"], "price": [100_000.0]})
    out, report = TX.xs.fill_missing_shares(TX._brk_feats(), prices, sec, TX.ASOF)
    assert report[0]["result"] == "filled", report
    assert out.loc[0, "shares_cover_filed"] == "2026-08-05"                    # 次新的 10-Q
    assert "10-Q 2026-08-05" in out.loc[0, "data_notes"]


def test_rejected_and_failed_write_price_free_data_notes(tmp_path):
    feats = TX._feats([{"ticker": "BROKEN", "cik": "9999999", "shares_cover": np.nan, "shares": np.nan,
                        "net_income": 1e10, "data_notes": ""},
                       {"ticker": "BRK.B", "cik": TX.B_CIK, "shares_cover": np.nan, "shares": np.nan,
                        "net_income": 1e12, "data_notes": "原備註"}])
    prices = pd.DataFrame({"ticker": ["BRK.B", "BROKEN"], "price": [100.0, 1.0]})   # BRK.B P/E 過低 → rejected
    out, report = TX.xs.fill_missing_shares(feats, prices, TX._make_sec(tmp_path), TX.ASOF)
    row = out.set_index("ticker")
    assert {r["ticker"]: r["result"] for r in report} == {"BROKEN": "failed", "BRK.B": "rejected"}
    assert row.loc["BRK.B", "data_notes"] == "原備註；多類股股數換算被拒：換算結果未通過合理性檢查"
    assert row.loc["BROKEN", "data_notes"].startswith("多類股股數換算失敗")
    assert "P/E" not in "".join(row["data_notes"].astype(str))
