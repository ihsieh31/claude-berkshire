"""季度初選 pipeline：以 skill 自測的合成資料跑真的 skill screen.py（不連網）。"""
import importlib.util
import json

import pytest

from trader import db
from trader.jobs.sandbox import _has_price_key
from trader.paths import get_paths
from trader.pipelines import screen as SP

ASOF = "2026-10-15"   # 與 skill test_screen.py 的合成資料同一決策日


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
    F, P = _skill_make()
    F.to_csv(inputs / "features.csv", index=False)
    P.to_csv(inputs / "prices.csv", index=False)
    (inputs / "universe.txt").write_text("# source=test\n" + "\n".join(F["ticker"]) + "\n", encoding="utf-8")
    yield paths, conn, inputs   # 有 universe.txt：股票池來源已知，不觸發 M1-R6-04 警告
    conn.close()


def _reg(conn, ticker, **data):
    db.upsert(conn, "registry", {"ticker": ticker, "status": data.get("status"), "data": data,
                                 "updated_at": db.now_iso()}, key=["ticker"])


def test_needs_full():
    assert SP.needs_full(None, ASOF)[0]
    assert SP.needs_full({"status": "WATCH"}, ASOF)[0]
    assert SP.needs_full({"last_full_review_date": "2025-10-01"}, ASOF)[0]          # > 365 天
    assert not SP.needs_full({"last_full_review_date": "2026-06-01"}, ASOF)[0]
    assert SP.needs_full({"status": "MISSING_DATA", "last_full_review_date": "2026-06-01"}, ASOF)[0]
    assert SP.needs_full({"last_full_review_date": "2026-06-01", "next_full_review_due": "2026-10-15"}, ASOF)[0]


def test_build_exclusions(state_dir):
    paths = get_paths().ensure()
    (paths.state / "exclusions.csv").write_text("ticker,excl_competence,excl_fraud\nbrk-b,1,\nXOM,false,TRUE\n")
    reg = {"AAA": {"status": "REJECT", "last_full_review_date": "2026-07-01"},
           "BBB": {"status": "TOO_HARD", "last_full_review_date": "2025-09-01"},   # 超過 12 個月 → 不排除
           "CCC": {"status": "WATCH", "last_full_review_date": "2026-07-01"}}
    ex = SP.build_exclusions(paths, reg, ASOF).set_index("ticker")
    assert set(ex.index) == {"AAA", "BRK.B", "XOM"}
    assert ex.at["AAA", "excl_recent_reject"] and not ex.at["AAA", "excl_competence"]
    assert ex.at["BRK.B", "excl_competence"] and not ex.at["BRK.B", "excl_fraud"]
    assert ex.at["XOM", "excl_fraud"] and not ex.at["XOM", "excl_competence"]


def test_run_screen_end_to_end(env):
    paths, conn, inputs = env
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s
    assert len(s["shortlist"]) == 15 and "warning" not in s
    assert [x["ticker"] for x in s["to_analyze"]] == s["shortlist"]        # 登錄表空 → 全部新進
    out = paths.state / "screens" / ASOF
    hand = json.loads((out / "screen_handoff.json").read_text())
    assert _has_price_key(hand)                                             # 原交接檔含 price_context
    nop = json.loads((out / "handoff_noprice.json").read_text())
    assert not _has_price_key(nop)
    assert [r["ticker"] for r in nop["core_shortlist"]] == s["shortlist"]
    row = conn.execute("SELECT status, kind FROM runs WHERE run_id=?", (f"screen-{ASOF}",)).fetchone()
    assert (row["status"], row["kind"]) == ("done", "quarterly_screen")


def test_registry_x5_backfills_and_recent_skipped(env):
    paths, conn, inputs = env
    first = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)["shortlist"]
    rejected, recent = first[0], first[1]
    _reg(conn, rejected, status="REJECT", last_full_review_date="2026-08-01")
    _reg(conn, recent, status="WATCH", last_full_review_date="2026-08-01")
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs, force=True)  # 同一 run_id 重跑須 force（M1-D9-01）
    assert rejected not in s["shortlist"] and len(s["shortlist"]) == 15     # X5 排除後自動遞補
    assert s["exclusions"]["excl_recent_reject"] == 1
    assert recent in s["shortlist"]
    assert [x["ticker"] for x in s["skipped_recent"]] == [recent]
    assert recent not in [x["ticker"] for x in s["to_analyze"]]


def test_missing_inputs_marks_run_failed(env, tmp_path):
    paths, conn, _ = env
    empty = tmp_path / "empty"
    empty.mkdir()
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=empty)
    assert "缺輸入" in s["error"]
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "failed"


def test_dedupe_share_classes_keeps_one_row_per_cik(tmp_path):
    import pandas as pd
    from trader.pipelines.screen import dedupe_share_classes
    f = tmp_path / "features.csv"
    pd.DataFrame({"ticker": ["GOOG", "GOOGL", "FOX", "FOXA", "KO"],
                  "cik": ["0001652044", "0001652044", "0001754301", "0001754301", "0000021344"]}).to_csv(f, index=False)
    out, dropped = dedupe_share_classes(f, tmp_path, {"FOX": {}})
    kept = list(pd.read_csv(out)["ticker"])
    assert sorted(kept) == ["FOX", "GOOGL", "KO"]                       # 登錄表優先；否則字母序最後
    assert {d["ticker"] for d in dropped} == {"GOOG", "FOXA"}
    f2 = tmp_path / "f2.csv"
    pd.DataFrame({"ticker": ["KO"], "cik": ["0000021344"]}).to_csv(f2, index=False)
    assert dedupe_share_classes(f2, tmp_path, {}) == (f2, [])
