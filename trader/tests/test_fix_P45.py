"""P45：紀錄完整性與冪等。M6-D9-03（回寫冪等）、M6-D9-02（cap_inputs 換檔）、M6-D1-04（PRICE HOLD 訊號）、
M6-D5-01／M2-D5-09（停止結果帶前半資訊）、M6-D5-03（PRICE 日誌）、M6-D5-08（HOLD_REVIEW 日誌）、M6-D5-09（PRICE 卡來源）、
M6-D2-10（H2 降級寫回）、M6-D4-02（框架自發委託有決策）、M1-D2-12（NOT_THIS_SKILL → X1）、M8-D2-04（停止結果欄位與缺漏率）、
D16（持股完整複查另立新版論點檔）、D17（無初篩判定時閘門 1 通過但記警示）、作廢列不參與（latest_signal 等）。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader import db
from trader.analysis import writeback as W
from trader.ledger import book
from trader.models import JobSpec
from trader.paths import get_paths
from trader.portfolio import sizing as S

ASOF = "2026-10-16"
CAP = {"auto_buy_cap": 60.0, "IV_base": 80.0, "IV_cons": 65.0, "absurd_price_line": 150.0, "grade_used": "B",
       "cap": 70.0}


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield conn, paths
    conn.close()


def _tdir(tmp_path: Path, name: str = "td", ticker: str = "AAPL", signal: str = "BUY", cap_input: bool = False) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    card = {"ticker": ticker, "as_of": ASOF, "signal": signal, "reason_code": "PASS_ALL", "certainty_grade": "B",
            "auto_buy_cap": 150.0, "value_per_share": {"conservative": 120.0, "base": 160.0}}
    (d / "signal_card.json").write_text(json.dumps(card), encoding="utf-8")
    (d / "registry_entry.json").write_text(json.dumps({"ticker": ticker, "grade": "B"}), encoding="utf-8")
    (d / "decision_log.json").write_text(json.dumps({"action": signal}), encoding="utf-8")
    (d / "thesis.md").write_text(f"論點 {name}", encoding="utf-8")
    if cap_input:
        (d / "cap_input.json").write_text(json.dumps({"oe": 5.0, "price": 100.0, "src": name}), encoding="utf-8")
    return d


def _n(conn, table="decisions") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _reg(conn, t):
    r = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return None if r is None else (r["status"], json.loads(r["data"]))


def _log(conn, did):
    return json.loads(conn.execute("SELECT data FROM decisions WHERE decision_id=?", (did,)).fetchone()[0])


def _card(conn, did):
    return json.loads(conn.execute("SELECT data FROM signals WHERE decision_id=?", (did,)).fetchone()[0])


def _price(conn, paths, action="HOLD", job_id=None, asof="2026-10-20", basis=None):
    return W.write_price_result(conn, paths, "AAPL", asof, "rp", action=action, gate1=True, gate2="不通過",
                                gate3="通過", cap=CAP, price=75.0, price_date=asof, t30=0.046, reason="x", notes=[],
                                basis=basis, job_id=job_id)


# ---------------------------------------------------------------- M6-D9-03：回寫冪等

def test_full_result_replay_same_job_writes_once(env, tmp_path):
    conn, paths = env
    td = _tdir(tmp_path)
    d1 = W.write_full_result(conn, paths, "AAPL", ASOF, "r1", td, job_id="r1:AAPL:FULL_B:1")
    d2 = W.write_full_result(conn, paths, "AAPL", ASOF, "r1", td, job_id="r1:AAPL:FULL_B:1")
    assert d1 == d2 and _n(conn) == 1 and _n(conn, "signals") == 1
    assert sorted(p.name for p in paths.theses.glob("AAPL_*")) == ["AAPL_2026-10-16.md"]
    assert _reg(conn, "AAPL")[1]["thesis_file"].endswith("AAPL_2026-10-16.md")
    assert _log(conn, d1)["source_job_id"] == "r1:AAPL:FULL_B:1"
    assert conn.execute("SELECT job_id FROM decisions").fetchone()[0] is None   # 佔位列語意不變


def test_other_writebacks_replay_same_job_write_once(env, tmp_path):
    conn, paths = env
    W.write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path))
    conn.execute("UPDATE registry SET status='HOLDING'")
    conn.commit()
    pa = {"stopped": True, "stopped_at_gate": "G1", "reason_code": "TOO_HARD"}
    for _ in range(2):
        W.write_stop_result(conn, paths, "AAPL", "2026-10-17", "r2", pa, job_id="j-stop")
        W.write_thesis_result(conn, paths, "AAPL", "2026-10-18", "r3", {"verdict": "完好", "reason": "x"}, {},
                              job_id="j-thesis")
        _price(conn, paths, job_id="j-price")
        W.write_hold_review_result(conn, paths, "AAPL", "2026-10-21", "r5", {"action": "HOLD", "reason": "x"},
                                   job_id="j-hold")
    assert _n(conn) == 5
    assert len(list(paths.theses.glob("AAPL_2026-10-16_addendum_*.md"))) == 2   # THESIS、H2 各一份增補


def test_without_key_still_writes_new_decision(env, tmp_path):
    """on_fail／on_invalid 不帶鍵：jobs retry 後的新結論不被舊列吃掉（反之亦然）。"""
    conn, paths = env
    W.write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path))
    res = {"verdict": "疑似", "reason": "x"}
    W.write_thesis_result(conn, paths, "AAPL", "2026-10-18", "r3", res, {})               # on_fail 的保守結論
    W.write_thesis_result(conn, paths, "AAPL", "2026-10-18", "r3", res, {}, job_id="j")   # retry 後 post
    W.write_thesis_result(conn, paths, "AAPL", "2026-10-18", "r3", res, {})
    assert _n(conn) == 4


# ---------------------------------------------------------------- M6-D9-02：cap_inputs 換檔

def test_cap_input_replace_failure_keeps_thesis_and_records_anomaly(env, tmp_path):
    conn, paths = env
    (paths.state / "cap_inputs" / "AAPL.json").mkdir(parents=True)   # 讓 os.replace 失敗
    did = W.write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, cap_input=True))
    st, data = _reg(conn, "AAPL")
    assert _n(conn) == 1 and (paths.state / data["thesis_file"]).is_file()
    a = conn.execute("SELECT kind, detail FROM anomalies").fetchone()
    assert a["kind"] == "cap_input_replace_failed" and json.loads(a["detail"])["decision_id"] == did


def test_cap_input_replaced_after_commit_and_tmp_removed_on_failure(env, tmp_path, monkeypatch):
    conn, paths = env
    W.write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, "a", cap_input=True))
    dst = paths.state / "cap_inputs" / "AAPL.json"
    assert json.loads(dst.read_text())["src"] == "a" and not dst.with_name("AAPL.json.tmp").exists()

    def boom(*a, **k):
        raise RuntimeError("交易失敗")
    monkeypatch.setattr(W, "_write_rows", boom)
    with pytest.raises(RuntimeError):
        W.write_full_result(conn, paths, "AAPL", "2026-10-17", "r2", _tdir(tmp_path, "b", cap_input=True))
    assert json.loads(dst.read_text())["src"] == "a" and not dst.with_name("AAPL.json.tmp").exists()
    assert not list(paths.theses.glob("AAPL_2026-10-17*"))


# ---------------------------------------------------------------- M6-D1-04、M6-D5-03、M6-D5-09：PRICE

def test_price_hold_after_buy_card_is_not_buy(env, tmp_path):
    conn, paths = env
    full = W.write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path))
    conn.execute("UPDATE registry SET status='HOLDING'")
    conn.commit()
    did = _price(conn, paths, "HOLD")
    row = conn.execute("SELECT signal FROM decisions WHERE decision_id=?", (did,)).fetchone()
    card = _card(conn, did)
    assert row["signal"] == "HOLD" and card["signal"] == "WATCH" and card["conclusion"] == "觀察"
    assert conn.execute("SELECT signal FROM signals WHERE decision_id=?", (did,)).fetchone()[0] == "WATCH"
    assert card["carried_from_decision_id"] == full                      # M6-D5-09
    d2 = _price(conn, paths, "HOLD", asof="2026-10-21")
    assert _card(conn, d2)["carried_from_decision_id"] == full           # 沿用最早的完整分析，不是上一張 PRICE


def test_price_log_has_cap_and_gate3_source(env, tmp_path):
    from trader.jobs.review_jobs import _gate3_info
    conn, paths = env
    full = W.write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path),
                               gates={"gate1": True, "gate2": True, "gate3": True})
    st, src = _gate3_info(conn, "AAPL", "2026-10-20")
    assert (st, src) == ("通過", full)
    did = _price(conn, paths, "WATCH", basis={"gate3_source": src, "weight": 0.05, "target_weight": 0.1})
    log = _log(conn, did)
    assert (log["auto_buy_cap"], log["iv_base"], log["long_rate"], log["buy_price_cap"]) == (60.0, 80.0, 0.046, 70.0)
    assert (log["gate3_source"], log["weight"], log["target_weight"]) == (full, 0.05, 0.1)


# ---------------------------------------------------------------- M6-D5-01、M8-D2-04、M1-D2-12：停止結果

def test_stop_result_keeps_phase_a_info_and_framework_fields(env):
    conn, paths = env
    pa = {"stopped": True, "stopped_at_gate": "STEP0", "reason_code": "NOT_THIS_SKILL", "missing": ["10-K"],
          "rule_ids": ["G0-1"], "inferred_thresholds_used": ["T1"], "asset_class": "保險"}
    did = W.write_stop_result(conn, paths, "AAPL", ASOF, "r1", pa,
                              log_fields={"screen_decision": "保留", "time_zone": "America/New_York",
                                          "flag_codes": ["F1"], "distress_codes": []})
    card, log = _card(conn, did), _log(conn, did)
    assert card["missing_data"] == ["10-K"] and card["rule_ids"] == ["G0-1"] and card["asset_class"] == "保險"
    assert card["inferred_thresholds_used"] == ["T1"] and log["rule_ids"] == ["G0-1"]
    assert (log["screen_decision"], log["time_zone"], log["flag_codes"]) == ("保留", "America/New_York", ["F1"])
    st, data = _reg(conn, "AAPL")
    assert data["asset_class"] == "保險" and data["last_reason_code"] == "NOT_THIS_SKILL"


def test_not_this_skill_becomes_x1_exclusion(env):
    from trader.pipelines.screen import _registry, build_exclusions
    conn, paths = env
    W.write_stop_result(conn, paths, "AAPL", ASOF, "r1",
                        {"stopped": True, "stopped_at_gate": "STEP0", "reason_code": "NOT_THIS_SKILL"})
    W.write_stop_result(conn, paths, "MSFT", ASOF, "r1",
                        {"stopped": True, "stopped_at_gate": "G1", "reason_code": "TOO_HARD"})
    df = build_exclusions(paths, _registry(conn), "2027-01-15").set_index("ticker")
    assert bool(df.loc["AAPL", "excl_asset_class"]) and not bool(df.loc["AAPL", "excl_recent_reject"])
    assert not bool(df.loc["MSFT", "excl_asset_class"]) and bool(df.loc["MSFT", "excl_recent_reject"])
    same_day = build_exclusions(paths, _registry(conn), ASOF)   # 分析日不早於 asof：非時點資訊
    assert "AAPL" not in set(same_day["ticker"])


def test_missing_rate_groups_stop_rows_and_drops_fill_fields():
    from trader.report.validation import _missing_rates
    full = [{"action": "BUY", "screen_decision": "保留", "time_zone": "NY", "price_date": "2026-10-15"},
            {"action": "NO_ACTION", "stop_mode": "A_THESIS_CHECK", "screen_decision": None, "time_zone": "NY"}]
    rates = _missing_rates(full, ["screen_decision", "time_zone", "price_date", "fill_price", "fill_date"])
    assert rates == {"screen_decision": 0.5, "time_zone": 0.0, "price_date": 0.0}   # price_date 停止類不適用


# ---------------------------------------------------------------- M6-D5-08、M6-D2-10：HOLD_REVIEW

def _holding(conn, paths, tmp_path, grade="B"):
    W.write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path))
    st, data = _reg(conn, "AAPL")
    db.upsert(conn, "registry", {"ticker": "AAPL", "status": "HOLDING", "data": {**data, "grade": grade},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def test_hold_review_log_has_price_date_and_trigger(env, tmp_path):
    conn, paths = env
    _holding(conn, paths, tmp_path)
    params = {"full_stage": "B", "final_signal": "WATCH", "stopped_at_gate": None, "price_date": "2026-10-19"}
    did = W.write_hold_review_result(conn, paths, "AAPL", "2026-10-20", "r", {"action": "HOLD", "reason": "x"},
                                     params, price=70.0, iv_base=80.0)
    log = _log(conn, did)
    assert log["price_date"] == "2026-10-19" and (log["price"], log["iv_base"]) == (70.0, 80.0)
    assert log["trigger"] == {"full_stage": "B", "final_signal": "WATCH", "stopped_at_gate": None, "reason_code": None}


def test_hold_review_grade_after_only_lowers(env, tmp_path):
    conn, paths = env
    _holding(conn, paths, tmp_path, grade="B")
    W.write_hold_review_result(conn, paths, "AAPL", "2026-10-20", "r",
                               {"action": "HOLD", "stop_adding": True, "grade_after": "C", "reason": "降一級"})
    assert _reg(conn, "AAPL")[1]["grade"] == "C"
    did = W.write_hold_review_result(conn, paths, "AAPL", "2026-10-21", "r",
                                     {"action": "HOLD", "grade_after": "A", "reason": "x"})
    assert _reg(conn, "AAPL")[1]["grade"] == "C" and _log(conn, did)["grade_after"] == "A"


def test_hold_review_schema_grade_after_optional():
    from trader.jobs import validate
    schemas = validate.load_schemas(get_paths().schemas_json)
    base = {"ticker": "KO", "decision_date": ASOF, "path": [], "action": "HOLD", "sell_category": None,
            "trim_fraction": None, "stop_adding": False, "observe_until": None, "falsified_assumption": None,
            "external_drivers": [], "external_drivers_change_reason": None, "reason": "x"}
    assert validate.check("HOLD_REVIEW", base, schemas)[1] == []
    assert validate.check("HOLD_REVIEW", {**base, "grade_after": "C"}, schemas)[1] == []


# ---------------------------------------------------------------- M6-D4-02：框架自發委託有決策

def test_institutional_trim_order_has_portfolio_decision(tmp_path):
    from trader.pipelines import daily as DP
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    book.ensure_initial_deposit(conn, 100000.0, "2026-10-01")
    db.insert(conn, "positions", {"ticker": "AAA", "qty": 400, "avg_cost": 50.0, "first_buy_date": "2024-01-02",
                                  "updated_at": db.now_iso()})
    db.upsert(conn, "registry", {"ticker": "AAA", "status": "HOLDING", "data": {"grade": "B"},
                                 "updated_at": db.now_iso()}, key=["ticker"])
    plan = S.plan_sells(conn, ASOF, [], {"AAA": 50.0}, user_hard_cap=0.10)[0]
    assert plan["qty"] > 0 and plan["decision_id"] is None
    out = {"run_id": "daily-x", "plans_dry_run": [], "rejected": [], "orders": []}
    oid = DP._place(conn, plan, ASOF, "2026-10-19", True, out)
    did = conn.execute("SELECT decision_id FROM orders WHERE order_id=?", (oid,)).fetchone()[0]
    row = conn.execute("SELECT run_id, signal, data FROM decisions WHERE decision_id=?", (did,)).fetchone()
    log = json.loads(row["data"])
    assert did and plan["decision_id"] == did and out["orders"][0]["decision_id"] == did
    assert (row["run_id"], row["signal"], log["review_type"], log["action"]) == ("daily-x", "TRIM", "PORTFOLIO", "TRIM")
    assert "INSTITUTIONAL" in log["reason_one_liner"]
    conn.close()


# ---------------------------------------------------------------- D16：持股完整複查另立新版論點檔

def test_holding_full_review_writes_new_version_and_keeps_old(env, tmp_path):
    conn, paths = env
    W.write_full_result(conn, paths, "AAPL", "2026-10-01", "r0", _tdir(tmp_path, "a"))
    old = paths.theses / "AAPL_2026-10-01.md"
    old_text = old.read_text(encoding="utf-8")
    corr = old.with_name("AAPL_2026-10-01_addendum_1.md")
    corr.write_text("# 相關群判定（P-31）2026-10-05\n\n- AAPL 與 MSFT 同一相關群\n", encoding="utf-8")
    old.with_name("AAPL_2026-10-01_addendum_2.md").write_text("# AAPL 論點檢查（THESIS）\n", encoding="utf-8")
    conn.execute("UPDATE registry SET status='HOLDING'")
    conn.commit()
    W.write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, "b"))
    new = paths.theses / "AAPL_2026-10-16_v2.md"
    text = new.read_text(encoding="utf-8")
    assert "predecessor: theses/AAPL_2026-10-01.md" in text and "version: v2" in text and "論點 b" in text
    assert "AAPL 與 MSFT 同一相關群" in text and "論點檢查（THESIS）" not in text   # 只沿用相關群增補
    assert old.read_text(encoding="utf-8") == old_text and len(W.addendum_paths(old)) == 2   # 舊檔原封不動
    assert _reg(conn, "AAPL")[1]["thesis_file"] == "theses/AAPL_2026-10-16_v2.md"
    assert W.main_thesis_path(paths, "AAPL") == new                                   # 無指標時也取最新版
    W.write_full_result(conn, paths, "AAPL", "2026-10-20", "r2", _tdir(tmp_path, "c"))
    assert "predecessor: theses/AAPL_2026-10-16_v2.md" in (paths.theses / "AAPL_2026-10-20_v3.md").read_text()


# ---------------------------------------------------------------- D17：無初篩判定 → 閘門 1 通過但記警示

def test_screen_context_only_within_12_months(env):
    from trader.pipelines.screen import screen_context_for
    conn, paths = env
    d = paths.state / "screens" / "2025-09-01"
    d.mkdir(parents=True)
    (d / "decisions.json").write_text(json.dumps({"AAPL": {"screen_decision": "刪除：X"}}), encoding="utf-8")
    (d / "summary.json").write_text("{}", encoding="utf-8")
    assert screen_context_for(paths, "2026-08-01", "AAPL", "觸發")["screen_decision"] == "刪除：X"
    assert screen_context_for(paths, ASOF, "AAPL", "觸發")["screen_decision"] == "未篩"   # 超過 12 個月不算


def test_gate1_warning_when_unscreened(env, tmp_path):
    from trader.jobs.deep import _framework_gates
    conn, paths = env
    ctx = SimpleNamespace(paths=paths, config=SimpleNamespace(schedule={"timezone": "America/New_York"}))
    job = JobSpec(job_id="r:AAPL:FULL_B:1", run_id="r", job_type="FULL_B", ticker="AAPL", asof=ASOF,
                  params={"screen_context": {"screen_decision": "未篩"}})
    g = _framework_gates(job, ctx, tmp_path)
    assert g["gate1"] is True and "D17" in g["gate1_warning"]
    job.params = {"screen_context": {"screen_decision": "刪除：結構性"}}
    g2 = _framework_gates(job, ctx, tmp_path)
    assert g2["gate1"] is False and g2["gate1_warning"] is None
    did = W.write_full_result(conn, paths, "AAPL", ASOF, "r", _tdir(tmp_path), gates=g)
    card, log = _card(conn, did), _log(conn, did)
    assert "D17" in card["gate1_warning"] and "D17" in card["auto_buy_gates"]["gate1_warning"]
    assert log["gate1_not_deleted"] is True and any("D17" in n for n in log["framework_notes"])


# ---------------------------------------------------------------- 作廢列不參與

def _void(conn, did):
    for tb in ("decisions", "signals"):
        r = conn.execute(f"SELECT data FROM {tb} WHERE decision_id=?", (did,)).fetchone()
        if r is not None:
            conn.execute(f"UPDATE {tb} SET data=? WHERE decision_id=?",
                         (json.dumps({**json.loads(r[0]), "voided_at": "2026-10-30T00:00:00Z"}), did))
    conn.commit()


def test_voided_rows_ignored_by_latest_signal_and_newer_state(env, tmp_path):
    conn, paths = env
    d1 = W.write_full_result(conn, paths, "AAPL", "2026-10-01", "r1", _tdir(tmp_path, "a", signal="WATCH"))
    d2 = W.write_full_result(conn, paths, "AAPL", "2026-10-10", "r2", _tdir(tmp_path, "b", signal="BUY"))
    assert W.latest_signal(conn, "AAPL")["data"]["decision_log_id"] == d2
    assert W._newer_state(conn, "AAPL", "2026-10-05") is not None
    _void(conn, d2)
    assert W.latest_signal(conn, "AAPL")["data"]["decision_log_id"] == d1
    assert W._newer_state(conn, "AAPL", "2026-10-05") is None
    assert W._newer_thesis_price(conn, "AAPL", "2026-10-05") is None


def test_voided_price_not_evidence_back_below_line(env):
    conn, paths = env
    db.insert(conn, "decisions", {"decision_id": "AAPL-20261012-1", "ticker": "AAPL", "decision_date": "2026-10-12",
                                  "data": {"price": 90.0}, "created_at": db.now_iso()})
    assert W._back_below_line(conn, "AAPL", "2026-10-01", 100.0, ASOF)
    _void(conn, "AAPL-20261012-1")
    assert not W._back_below_line(conn, "AAPL", "2026-10-01", 100.0, ASOF)
