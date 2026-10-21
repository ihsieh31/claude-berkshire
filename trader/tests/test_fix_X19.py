"""X19（P26 交接）：FULL_B 驗證過的摘要落檔 phaseB.json、cap_calc 等級不高於 FULL_A 結束等級、
engine 驗證 FULL_B 時帶 phaseA.json（升級 → 不合格走重試）。"""
import json

from test_engine import ASOF, CAP_INPUT, RUN, consistent_card, dossier_ok, full_a, full_b, make_env, red_team


def _phase_a_ends_b() -> dict:
    a = full_a()   # 起點 A，前半降一級 → 結束 B
    a["downgrades"] = [{"step": "G5", "rule": "SEL-093", "driver_id": "資本配置", "reason": "資本配置淨分 <0"}]
    return a


def _full_b(grade: str = "B", cap_grade: str = "B") -> dict:
    b = full_b("BUY")
    b["certainty_grade"] = grade
    b["files"] = dict(b["files"])
    b["files"]["KO/cap_input.json"] = json.dumps({**CAP_INPUT, "grade": cap_grade})
    card = consistent_card("BUY")
    card["certainty_grade"] = cap_grade
    b["files"]["KO/signal_card.json"] = json.dumps(card, ensure_ascii=False)
    return b


def _run(tmp_path, responses):
    env = make_env(tmp_path, responses)
    env.add(env.job("KO", "DOSSIER", 1))
    env.price_file("KO")
    return env, env.engine.run(RUN)


def test_phase_b_summary_written_and_writeback_reads_intended_weight(tmp_path):
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": full_a(),
                               "FULL_B": full_b("BUY"), "RED_TEAM": red_team(False)})
    assert res["status"] == "done"
    td = env.paths.work / RUN / "KO"
    pb = json.loads((td / "phaseB.json").read_text(encoding="utf-8"))
    assert pb["intended_weight"] == {"level": "MEDIUM", "reason": "G8 優勢差距約 1pp、確定性等級 B"}
    sc = json.loads(env.conn.execute("SELECT data FROM signals").fetchone()[0])
    assert sc["intended_weight"]["level"] == "MEDIUM"   # 訊號卡沒寫 → 只能來自 phaseB.json


def test_cap_calc_uses_full_a_end_grade_when_full_b_says_a(tmp_path):
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _phase_a_ends_b(),
                               "FULL_B": _full_b(grade="B", cap_grade="A"), "RED_TEAM": red_team(False)})
    assert res["status"] == "done"
    td = env.paths.work / RUN / "KO"
    assert json.loads((td / "cap_input.json").read_text(encoding="utf-8"))["grade"] == "B"
    cap = json.loads((td / "cap_result.json").read_text(encoding="utf-8"))
    assert cap["grade_used"] == "B" and cap["cap_discount_k"] == 0.85
    card = json.loads((td / "signal_card.json").read_text(encoding="utf-8"))
    assert card["certainty_grade"] == "B"
    assert card["auto_buy_cap"] == cap["auto_buy_cap"]   # 上限線以 B 計，不是 A 的 85.05
    # 框架造成的差異 → 直接以腳本覆寫，不退回重做（FULL_B 只呼叫一次）
    assert len(env.by_type("FULL_B")) == 1


def test_engine_caps_full_b_upgrade_without_retry(tmp_path):
    """A3-M3D-04：FULL_B 等級高於 FULL_A 結束等級 → 驗證不報錯、不退回；摘要壓回 B（原值留 certainty_grade_model）。"""
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _phase_a_ends_b(),
                               "FULL_B": _full_b(grade="A", cap_grade="B"), "RED_TEAM": red_team(False)})
    assert res["status"] == "done"
    row = env.by_type("FULL_B")[0]
    assert row["status"] == "done" and row["attempts"] == 1
    errs = [r["error"] for r in env.conn.execute(
        "SELECT error FROM llm_calls WHERE job_id=? ORDER BY rowid", (row["job_id"],)).fetchall()]
    assert errs == [None]
    pb = json.loads((env.paths.work / RUN / "KO" / "phaseB.json").read_text(encoding="utf-8"))
    assert pb["certainty_grade"] == "B" and pb["certainty_grade_model"] == "A" and pb["grade_notes"]


def test_engine_full_b_upgrade_is_not_missing_data(tmp_path):
    """A3-M3D-04：舊版兩次升級 → 整鏈 NO_ACTION／MISSING_DATA；現在照常走完（擬 BUY 進 RED_TEAM），不落 MISSING_DATA。"""
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": _phase_a_ends_b(),
                               "FULL_B": _full_b(grade="A", cap_grade="B"), "RED_TEAM": red_team(False)})
    assert env.by_type("FULL_B")[0]["status"] == "done"
    dec = env.conn.execute("SELECT signal, reason_code FROM decisions").fetchall()
    assert ("NO_ACTION", "MISSING_DATA") not in [(r["signal"], r["reason_code"]) for r in dec]
    assert ASOF


def _full_b_cap_off() -> dict:
    """等級與 FULL_A 一致、輸入未被框架改動，只有訊號卡 auto_buy_cap 與 cap_calc 不同（真正的 cap 不一致，M5-R3-03）。"""
    b = full_b("BUY")
    b["files"] = dict(b["files"])
    card = consistent_card("BUY")
    card["auto_buy_cap"] = round(float(card["auto_buy_cap"]) * 1.2 + 1, 2)
    b["files"]["KO/signal_card.json"] = json.dumps(card, ensure_ascii=False)
    return b


def test_cap_mismatch_redo_keeps_buy_path(tmp_path):
    """附帶：cap_calc 不一致退回重做時 redteam_round 不得帶 None（舊版 int(None) 讓重做的 FULL_B 失敗）。"""
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": full_a(),
                               "FULL_B": [_full_b_cap_off(), full_b("BUY")],
                               "RED_TEAM": red_team(False)})
    assert res["status"] == "done"
    fb = env.by_type("FULL_B")
    assert [r["status"] for r in fb] == ["done", "done"]
    assert "redteam_round" not in json.loads(fb[1]["params"])
    assert json.loads(fb[1]["params"]).get("cap_retry") == 1
    assert [r["job_type"] for r in env.rows()][-1] == "RED_TEAM"


def test_summary_signal_and_gate_win_over_card(tmp_path):
    """check-P26 D31：訊號卡 BUY、已驗證摘要 WATCH／PASS_ALL／G8 → 以摘要為準（不進 RED_TEAM、不寫回 BUY）。
    WATCH 只配 PASS_ALL（A3-M3C-07），原 WATCH／TOO_HARD 改 PASS_ALL。"""
    b = full_b("WATCH")
    b["stopped_at_gate"] = "G8"
    b["files"] = dict(b["files"])
    b["files"]["KO/signal_card.json"] = json.dumps(consistent_card("BUY"), ensure_ascii=False)
    env, res = _run(tmp_path, {"DOSSIER": dossier_ok(), "FULL_A": full_a(), "FULL_B": b,
                               "RED_TEAM": red_team(False)})
    assert res["status"] == "done"
    assert env.by_type("RED_TEAM") == []
    dec = env.conn.execute("SELECT signal, reason_code FROM decisions").fetchall()
    assert [(r["signal"], r["reason_code"]) for r in dec] == [("WATCH", "PASS_ALL")]
    card = json.loads((env.paths.work / RUN / "KO" / "signal_card.json").read_text(encoding="utf-8"))
    assert card["stopped_at_gate"] == "G8"
