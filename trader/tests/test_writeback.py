"""trader.analysis.writeback 的測試：決策編號、三表寫入、論點檔凍結、停止結果。"""
import json
from pathlib import Path

import pytest

from trader import db
from trader.analysis import writeback
from trader.analysis.writeback import WritebackError, next_decision_id, write_full_result, write_stop_result
from trader.paths import get_paths

ASOF = "2026-10-16"
SCHEMA_DIR = get_paths().skill_deep / "assets"


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield conn, paths
    conn.close()


def _tdir(tmp_path: Path, name: str = "tdir", ticker: str = "AAPL", signal: str = "BUY",
          reason: str = "PASS_ALL", thesis: str = "論點內容 A") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    card = {
        "schema": "berkshire-deep-merged/signal-v1",
        "ticker": ticker,
        "as_of": ASOF,
        "signal": signal,
        "reason_code": reason,
        "conclusion": {"BUY": "買", "WATCH": "觀察", "NO_ACTION": "不買"}[signal],
        "auto_buy_cap": 150.0,
        "value_per_share": {"conservative": 120.0, "base": 160.0},
        "red_light": False,
        "decision_log_id": "",
    }
    entry = {"ticker": ticker, "cik": "0000320193", "status": signal, "grade": "B", "auto_buy_cap": 150.0}
    log = {
        "decision_id": "",
        "decision_date": ASOF,
        "ticker": ticker,
        "action": signal,
        "screen_decision": "PASS",
        "gate3_pass": True,
        "reason_one_liner": "測試",
    }
    (d / "signal_card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    (d / "registry_entry.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    (d / "decision_log.json").write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text(thesis, encoding="utf-8")
    return d


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def _reg(conn, ticker: str):
    row = conn.execute("SELECT * FROM registry WHERE ticker = ?", (ticker,)).fetchone()
    return None if row is None else dict(row)


# ---------------------------------------------------------------- 決策編號

def test_next_decision_id_first_is_one(env):
    conn, _ = env
    assert next_decision_id(conn, "AAPL", ASOF) == "AAPL-20261016-1"


def test_next_decision_id_increments_same_day(env, tmp_path):
    conn, paths = env
    write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, "a"))
    assert next_decision_id(conn, "AAPL", ASOF) == "AAPL-20261016-2"
    write_full_result(conn, paths, "AAPL", ASOF, "r2", _tdir(tmp_path, "b"))
    assert next_decision_id(conn, "AAPL", ASOF) == "AAPL-20261016-3"


def test_next_decision_id_skips_ids_already_in_signals(env):
    """M3：decisions 缺列但 signals 仍占著該編號時不得撞號。"""
    conn, _ = env
    conn.execute("INSERT INTO signals(decision_id, ticker, signal, data, created_at) VALUES (?,?,?,?,?)",
                 ("AAPL-20261016-1", "AAPL", "WATCH", "{}", "2026-10-16T00:00:00"))
    conn.commit()
    assert next_decision_id(conn, "AAPL", ASOF) == "AAPL-20261016-2"


def test_next_decision_id_is_per_ticker_and_day(env, tmp_path):
    conn, paths = env
    write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, "a"))
    assert next_decision_id(conn, "MSFT", ASOF) == "MSFT-20261016-1"
    assert next_decision_id(conn, "AAPL", "2026-10-17") == "AAPL-20261017-1"


def test_next_decision_id_normalizes_ticker(env):
    conn, _ = env
    assert next_decision_id(conn, "brk-b", ASOF) == "BRK.B-20261016-1"


def test_next_decision_id_rejects_bad_asof(env):
    conn, _ = env
    with pytest.raises(WritebackError):
        next_decision_id(conn, "AAPL", "2026/10/16")


# ---------------------------------------------------------------- write_full_result

def test_full_result_writes_three_tables(env, tmp_path):
    conn, paths = env
    did = write_full_result(conn, paths, "AAPL", ASOF, "run-1", _tdir(tmp_path))
    assert did == "AAPL-20261016-1"

    d = conn.execute("SELECT * FROM decisions WHERE decision_id = ?", (did,)).fetchone()
    assert d["ticker"] == "AAPL" and d["decision_date"] == ASOF and d["run_id"] == "run-1"
    assert d["signal"] == "BUY" and d["reason_code"] == "PASS_ALL"
    log = json.loads(d["data"])
    assert log["decision_id"] == did and log["action"] == "BUY"

    s = conn.execute("SELECT * FROM signals WHERE decision_id = ?", (did,)).fetchone()
    assert s["signal"] == "BUY"
    card = json.loads(s["data"])
    assert card["decision_log_id"] == did
    assert card["auto_buy_cap"] == 150.0

    reg = _reg(conn, "AAPL")
    assert reg["status"] == "WATCH"  # BUY 尚未成交
    entry = json.loads(reg["data"])
    assert entry["last_review_date"] == ASOF
    assert entry["last_full_review_date"] == ASOF
    assert entry["thesis_file"] == f"theses/AAPL_{ASOF}.md"          # 相對 state，可隨 state 搬移
    assert writeback.main_thesis_path(paths, "AAPL", entry["thesis_file"]) == paths.state / f"theses/AAPL_{ASOF}.md"
    # 舊紀錄的絕對路徑已失效 → 取同名檔
    assert writeback.main_thesis_path(paths, "AAPL", f"/nonexistent/old/theses/AAPL_{ASOF}.md") == \
        paths.theses / f"AAPL_{ASOF}.md"
    assert reg["updated_at"]


def test_thesis_written_and_frozen_with_addendum(env, tmp_path):
    conn, paths = env
    write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, "a", thesis="第一版"))
    base = paths.theses / f"AAPL_{ASOF}.md"
    assert base.read_text(encoding="utf-8") == "第一版"

    write_full_result(conn, paths, "AAPL", ASOF, "r2", _tdir(tmp_path, "b", thesis="第二版"))
    assert base.read_text(encoding="utf-8") == "第一版"  # 凍結，不覆寫
    add1 = paths.theses / f"AAPL_{ASOF}_addendum_1.md"
    assert add1.read_text(encoding="utf-8") == "第二版"

    write_full_result(conn, paths, "AAPL", ASOF, "r3", _tdir(tmp_path, "c", thesis="第三版"))
    assert (paths.theses / f"AAPL_{ASOF}_addendum_2.md").read_text(encoding="utf-8") == "第三版"
    assert base.read_text(encoding="utf-8") == "第一版"


@pytest.mark.parametrize("signal,reason,status", [
    ("BUY", "PASS_ALL", "WATCH"),
    ("WATCH", "PASS_ALL", "WATCH"),
    ("NO_ACTION", "MISSING_DATA", "MISSING_DATA"),
    ("NO_ACTION", "TOO_HARD", "TOO_HARD"),
    ("NO_ACTION", "REJECT", "REJECT"),
    ("NO_ACTION", "RED_LIGHT", "WATCH"),   # M2_D7_08：紅燈＝延後，留觀察名單（P53）
    ("NO_ACTION", "NOT_THIS_SKILL", "NO_ACTION"),
])
def test_registry_status_mapping(env, tmp_path, signal, reason, status):
    conn, paths = env
    ticker = f"T{abs(hash((signal, reason))) % 10000}"
    write_full_result(conn, paths, ticker, ASOF, "r", _tdir(tmp_path, ticker, ticker=ticker,
                                                             signal=signal, reason=reason))
    assert _reg(conn, ticker)["status"] == status


def test_final_signal_override_downgrades_everywhere(env, tmp_path):
    conn, paths = env
    did = write_full_result(conn, paths, "AAPL", ASOF, "r", _tdir(tmp_path),
                            final_signal="WATCH", notes=["閘門 3 未過"])
    d = conn.execute("SELECT * FROM decisions WHERE decision_id = ?", (did,)).fetchone()
    assert d["signal"] == "WATCH"
    log = json.loads(d["data"])
    assert log["action"] == "WATCH"
    assert any("閘門 3 未過" in n for n in log["framework_notes"])
    assert any("BUY" in n and "WATCH" in n for n in log["framework_notes"])

    s = conn.execute("SELECT * FROM signals WHERE decision_id = ?", (did,)).fetchone()
    assert s["signal"] == "WATCH"
    card = json.loads(s["data"])
    assert card["signal"] == "WATCH" and card["conclusion"] == "觀察"
    assert _reg(conn, "AAPL")["status"] == "WATCH"


def test_final_signal_invalid_raises_and_writes_nothing(env, tmp_path):
    conn, paths = env
    with pytest.raises(WritebackError):
        write_full_result(conn, paths, "AAPL", ASOF, "r", _tdir(tmp_path), final_signal="HOLD")
    assert _count(conn, "decisions") == 0
    assert not (paths.theses / f"AAPL_{ASOF}.md").exists()


def test_holding_status_is_not_downgraded(env, tmp_path):
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAPL", "status": "HOLDING", "data": {"ticker": "AAPL"},
                                 "updated_at": db.now_iso(), "first_buy_date": "2026-01-02"}, ["ticker"])
    did = write_full_result(conn, paths, "AAPL", ASOF, "r", _tdir(tmp_path))
    reg = _reg(conn, "AAPL")
    assert reg["status"] == "HOLDING"
    assert reg["first_buy_date"] == "2026-01-02"
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id = ?", (did,)).fetchone()["data"])
    assert any("HOLDING" in n for n in log["framework_notes"])


def test_missing_file_raises_and_writes_nothing(env, tmp_path):
    conn, paths = env
    d = _tdir(tmp_path)
    (d / "thesis.md").unlink()
    with pytest.raises(WritebackError):
        write_full_result(conn, paths, "AAPL", ASOF, "r", d)
    assert _count(conn, "decisions") == 0
    assert _count(conn, "signals") == 0
    assert _reg(conn, "AAPL") is None
    assert not (paths.theses / f"AAPL_{ASOF}.md").exists()


def test_missing_signal_card_raises(env, tmp_path):
    conn, paths = env
    d = _tdir(tmp_path)
    (d / "signal_card.json").unlink()
    with pytest.raises(WritebackError):
        write_full_result(conn, paths, "AAPL", ASOF, "r", d)


def test_db_failure_removes_new_thesis_file(env, tmp_path, monkeypatch):
    conn, paths = env

    def boom(*_a, **_k):
        raise RuntimeError("模擬寫入失敗")

    monkeypatch.setattr(writeback, "_write_rows", boom)
    with pytest.raises(RuntimeError):
        write_full_result(conn, paths, "AAPL", ASOF, "r", _tdir(tmp_path))
    assert not (paths.theses / f"AAPL_{ASOF}.md").exists()


def test_ticker_normalized_in_rows(env, tmp_path):
    conn, paths = env
    did = write_full_result(conn, paths, "brk-b", ASOF, "r", _tdir(tmp_path, ticker="BRK.B"))
    assert did == "BRK.B-20261016-1"
    assert _reg(conn, "BRK.B") is not None
    assert (paths.theses / f"BRK.B_{ASOF}.md").exists()


# ---------------------------------------------------------------- write_stop_result

def _schema_keys(name: str) -> set[str]:
    """三個 schema 的欄位名：訊號卡為頂層鍵；決策日誌為 fields；登錄表為 entries[0]。"""
    obj = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    if name == "decision_log_schema.json":
        obj = obj["fields"]
    elif name == "registry_schema.json":
        obj = obj["entries"][0]
    return {k for k in obj if not k.startswith("_")}


def test_stop_result_minimal_triplet(env):
    conn, paths = env
    did = write_stop_result(conn, paths, "AAPL", ASOF, "r", {"stopped": True, "reason_code": "MISSING_DATA",
                                                             "stopped_at_gate": "G0"})
    assert did == "AAPL-20261016-1"

    d = conn.execute("SELECT * FROM decisions WHERE decision_id = ?", (did,)).fetchone()
    assert d["signal"] == "NO_ACTION" and d["reason_code"] == "MISSING_DATA"
    log = json.loads(d["data"])
    assert log["stopped_at_gate"] == "G0" and log["action"] == "NO_ACTION"
    assert set(log) >= _schema_keys("decision_log_schema.json")

    s = conn.execute("SELECT * FROM signals WHERE decision_id = ?", (did,)).fetchone()
    card = json.loads(s["data"])
    assert set(card) >= _schema_keys("signal_card_schema.json")
    assert card["signal"] == "NO_ACTION"
    assert card["reason_code"] == "MISSING_DATA"
    assert card["stopped_at_gate"] == "G0"
    assert card["certainty_grade"] is None
    assert card["stop_mode"] == "A_THESIS_CHECK"
    assert card["conclusion"] == "觀察（缺資料）"
    assert card["auto_buy_cap"] is None  # 無法得出的欄位為 null

    reg = _reg(conn, "AAPL")
    assert reg["status"] == "MISSING_DATA"
    entry = json.loads(reg["data"])
    assert set(entry) >= _schema_keys("registry_schema.json")
    assert entry["last_full_review_date"] == ASOF
    assert entry["last_review_date"] == ASOF

    assert not list(paths.theses.glob("AAPL_*"))  # 停止不寫論點檔


def test_stop_result_default_reason_is_missing_data(env):
    conn, paths = env
    did = write_stop_result(conn, paths, "AAPL", ASOF, "r", {"stopped": True})
    assert conn.execute("SELECT reason_code FROM decisions WHERE decision_id = ?", (did,)).fetchone()[0] == "MISSING_DATA"
    assert _reg(conn, "AAPL")["status"] == "MISSING_DATA"


def test_stop_result_too_hard_status(env):
    conn, paths = env
    write_stop_result(conn, paths, "AAPL", ASOF, "r", {"stopped": True, "reason_code": "TOO_HARD",
                                                       "stopped_at_gate": "G1"})
    assert _reg(conn, "AAPL")["status"] == "TOO_HARD"


def test_stop_result_id_continues_after_full_result(env, tmp_path):
    conn, paths = env
    write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path))
    did = write_stop_result(conn, paths, "AAPL", ASOF, "r2", {"reason_code": "REJECT", "stopped_at_gate": "G2"})
    assert did == "AAPL-20261016-2"


def test_stop_result_does_not_downgrade_holding(env):
    conn, paths = env
    db.upsert(conn, "registry", {"ticker": "AAPL", "status": "HOLDING", "data": {}, "updated_at": db.now_iso()},
              ["ticker"])
    write_stop_result(conn, paths, "AAPL", ASOF, "r", {"reason_code": "MISSING_DATA"})
    assert _reg(conn, "AAPL")["status"] == "HOLDING"


def _g3(tests, ev):
    keys = ("pricing_power", "attack_copy", "price_cut")
    return {"stopped": True, "reason_code": "REJECT", "stopped_at_gate": "G3",
            "moat_tests": dict(zip(keys, tests)), "moat_evidence": dict(zip(keys, ev)) if ev else None}


@pytest.mark.parametrize("tests,ev,want", [
    (("中", "弱", "弱"), ("足夠", "足夠", "足夠"), "有反證"),
    (("強", "中", "弱"), ("足夠", "缺資料", "足夠"), "證據不足"),
    (("中", "中", "弱"), ("缺資料", "缺資料", "足夠"), "證據不足"),
    (("中", "中", "弱"), ("缺資料", "足夠", "足夠"), "有反證"),
    (("強", "強", "弱"), ("足夠", "足夠", "足夠"), "其他"),
    (("中", "弱", "弱"), None, None),                       # 舊格式沒有 moat_evidence
])
def test_reject_basis(tests, ev, want):
    assert writeback.reject_basis(_g3(tests, ev)) == want


def test_reject_basis_only_for_g3_reject():
    p = _g3(("中", "弱", "弱"), ("缺資料", "缺資料", "缺資料"))
    assert writeback.reject_basis({**p, "stopped_at_gate": "G1"}) is None
    assert writeback.reject_basis({**p, "reason_code": "TOO_HARD"}) is None


def test_stop_result_records_basis_and_one_liner(env):
    conn, paths = env
    p = _g3(("中", "中", "弱"), ("缺資料", "缺資料", "足夠"))
    p["thesis_draft"] = {"one_liner": "客戶留存率缺，三測無法評強", "key_assumptions": [], "kill_criteria": []}
    did = write_stop_result(conn, paths, "AAPL", ASOF, "r", p)
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id = ?", (did,)).fetchone()[0])
    assert log["reject_basis"] == "證據不足"
    assert log["reason_one_liner"] == "客戶留存率缺，三測無法評強"
    reg = _reg(conn, "AAPL")
    assert reg["status"] == "REJECT"  # 停止碼與 status 不變
    assert json.loads(reg["data"])["reject_basis"] == "證據不足"


def test_stop_result_one_liner_falls_back_to_report(env, tmp_path):
    conn, paths = env
    td = tmp_path / "AAPL"; td.mkdir()
    (td / "report_A.md").write_text("## ① 結論卡\n\n一句話理由：三測 0 項強，方向變窄。\n停在哪一關：G3\n", encoding="utf-8")
    p = {**_g3(("弱", "弱", "中"), ("足夠", "足夠", "足夠")), "thesis_draft": {"one_liner": ""}}
    did = write_stop_result(conn, paths, "AAPL", ASOF, "r", p, tdir=td)
    log = json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id = ?", (did,)).fetchone()[0])
    assert log["reason_one_liner"] == "三測 0 項強，方向變窄。"
    assert log["reject_basis"] == "有反證"


def test_stop_result_bad_asof_raises(env):
    conn, paths = env
    with pytest.raises(WritebackError):
        write_stop_result(conn, paths, "AAPL", "bad", "r", {})


def test_full_result_framework_fills_skill_hashes_and_fy_last_filed(env, tmp_path):
    """月報「決策日後申報」「skill 雜湊」讀的欄位由框架填，不靠模型。"""
    conn, paths = env
    d = _tdir(tmp_path)
    (d / "sec").mkdir()
    (d / "dossier").mkdir(parents=True, exist_ok=True)
    (d / "dossier" / ".prep_done").write_text("ok\n", encoding="utf-8")  # 前置完成標記
    (d / "sec" / "_index.json").write_text(json.dumps([
        {"form": "10-K", "filingDate": "2025-02-20"}, {"form": "10-K", "filingDate": "2026-02-19"},
        {"form": "10-Q", "filingDate": "2026-07-30"}]), encoding="utf-8")
    write_full_result(conn, paths, "AAPL", ASOF, "r1", d)
    log = json.loads(conn.execute("SELECT data FROM decisions").fetchone()["data"])
    assert log["fy_last_filed"] == "2026-02-19"
    assert (paths.state / log["skill_hashes"]).is_file()
    assert writeback.skill_hashes_file(paths) == log["skill_hashes"]   # 內容不變 → 同一檔


# 自 test_hark_queue.py 移來（框架測試，與 hark_queue 無關；P34／M8-D10-01）
def test_stop_one_liner_joins_continuation(tmp_path):
    from trader.analysis.writeback import _stop_one_liner
    (tmp_path / "report_A.md").write_text("結論：觀察\n一句話理由：甲乙丙，\n            丁戊己。\n停在哪一關：步 0\n", encoding="utf-8")
    assert _stop_one_liner({}, tmp_path) == "甲乙丙， 丁戊己。"
