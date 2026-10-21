"""P54／P54R：論點檔價格隔離（M6-D6-01、M6-D6-02、M3_D6-03）、完整複查日期（M3-D10-01）、
失效條件期限（M3-D10-02）、skill_hashes 含 routing（M3_D9-02）。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader import db
from trader.analysis import writeback
from trader.analysis.writeback import write_full_result, write_hold_review_result, write_stop_result
from trader.jobs.review_jobs import _stage_thesis
from trader.paths import Paths, get_paths

ASOF = "2026-10-16"
OLD_FULL = "2025-10-01"


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield conn, paths
    conn.close()


def _holding(conn, paths, t="KO"):
    paths.theses.mkdir(parents=True, exist_ok=True)
    (paths.theses / f"{t}_2025-10-01.md").write_text("# 原論點\n", encoding="utf-8")
    data = {"ticker": t, "status": "HOLDING", "grade": "B", "thesis_file": f"theses/{t}_2025-10-01.md",
            "last_review_date": OLD_FULL, "last_full_review_date": OLD_FULL}
    db.upsert(conn, "registry", {"ticker": t, "status": "HOLDING", "data": data, "updated_at": db.now_iso()},
              key=["ticker"])
    db.insert(conn, "positions", {"ticker": t, "qty": 100, "avg_cost": 50.0, "first_buy_date": "2024-01-02",
                                  "updated_at": db.now_iso()})


def _reg(conn, t="KO"):
    return json.loads(conn.execute("SELECT data FROM registry WHERE ticker=?", (t,)).fetchone()["data"])


def _hold(evidence="非價格理由"):
    return {"ticker": "KO", "decision_date": ASOF, "path": [{"q": "Q3", "answer": "否", "evidence": evidence}],
            "action": "HOLD", "sell_category": None, "trim_fraction": None, "stop_adding": True,
            "observe_until": None, "falsified_assumption": None, "external_drivers": [],
            "external_drivers_change_reason": None, "reason": "持有"}


# ---------------------------------------------------------------- M6-D6-01／M3_D6-03

def test_stage_thesis_drops_valuation_lines(env, tmp_path):
    conn, paths = env
    paths.theses.mkdir(parents=True, exist_ok=True)
    (paths.theses / "KO_2025-10-01.md").write_text(
        "# KO 論點\n品牌與通路護城河仍在。\n安全邊際 35%，買價上限 89.5\nIV_base 120，P/E 22\n"
        "## 保守價值區間\n失效條件：2027-06-30 前有機營收連兩年負成長\n", encoding="utf-8")
    sb = tmp_path / "sb"
    job = SimpleNamespace(ticker="KO", params={"thesis_file": "theses/KO_2025-10-01.md"})
    names = _stage_thesis(sb, job, paths)
    text = (sb / names[0]).read_text(encoding="utf-8")
    assert "89.5" not in text and "35%" not in text and "IV_base" not in text and "22" not in text
    assert "品牌與通路護城河仍在" in text and "2027-06-30 前有機營收連兩年負成長" in text
    assert "## 保守價值區間" in text


# P54R：只刪股價／估值寫法；業務論述裡的「價格／上限／殖利率」要保留（含數字也一樣）。
@pytest.mark.parametrize("line", [
    "定價權：強——每年提高產品價格 4–6%",
    "1. 價格競爭加劇：對手降價 10%",
    "價格接受者：產品無差異，2024 年毛利率 18%",
    "無上限侵權責任：2025 年和解 3 億美元",
    "等級上限 B（代理口徑，SEL-055）",
    "產能上限 120 萬噸，2027 年擴產完成",
    "失效條件：30 年期公債殖利率 4.5% 以上連續 2 季",
    "Talkspace 購買價格分攤缺 2024 年資料",
])
def test_scrub_valuation_keeps_business_lines(line):
    assert writeback.scrub_valuation_lines(line) == line


@pytest.mark.parametrize("line", [
    "自動買進上限：95.0",
    "買進上限 89.5",
    "價格上限 100",
    "價格 45.0，IV_base 50.0",
    "價格：$45.2",
    "股利殖利率 3.2%",
    "FCF殖利率 6%",
    "內在價值 120（保守）",
    "安全邊際 35%",
    "預期報酬 9%",
    "P/E 22",
    "本益比 22 倍",
    "IV0 80",
])
def test_scrub_valuation_drops_valuation_lines(line):
    assert writeback.scrub_valuation_lines("論點\n" + line + "\n結尾") == "論點\n結尾"


# ---------------------------------------------------------------- M6-D6-02

def test_hold_review_addendum_has_no_price_or_iv(env):
    conn, paths = env
    _holding(conn, paths)
    notes = ["覆核兩次未通過：估值類賣出須價格 ≥ IV_base（價格 45.0，IV_base 50.0）；模型原動作 SELL_ALL，改為 HOLD"]
    write_hold_review_result(conn, paths, "KO", ASOF, "r1", _hold("價格 45 低於 IV_base 50"),
                             {"full_stage": "B"}, notes, price=45.0, iv_base=50.0)
    add = (paths.theses / "KO_2025-10-01_addendum_1.md").read_text(encoding="utf-8")
    assert "45" not in add and "IV_base" not in add and "價格" not in add
    assert "持股完整複查 H2" in add
    log = json.loads(conn.execute("SELECT data FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()["data"])
    assert log["framework_notes"] == notes          # 註記仍完整進決策日誌


# ---------------------------------------------------------------- M3-D10-01

@pytest.mark.parametrize("params,advanced", [
    ({"full_stage": "PRICE", "price": 60.0}, False),
    ({"full_stage": "FAIL", "reason_code": "MISSING_DATA"}, False),
    ({"full_stage": "A", "reason_code": "MISSING_DATA"}, False),
    ({"full_stage": "A", "reason_code": "MOAT_FAIL"}, True),
    ({"full_stage": "B"}, True),
])
def test_hold_review_full_date_only_after_real_full_review(env, params, advanced):
    conn, paths = env
    _holding(conn, paths)
    write_hold_review_result(conn, paths, "KO", ASOF, "r1", _hold(), params)
    d = _reg(conn)
    assert d["last_review_date"] == ASOF
    assert d["last_full_review_date"] == (ASOF if advanced else OLD_FULL)


@pytest.mark.parametrize("reason,advanced", [("MISSING_DATA", False), ("MOAT_FAIL", True)])
def test_stop_result_holding_missing_data_keeps_full_date(env, reason, advanced):
    conn, paths = env
    _holding(conn, paths)
    write_stop_result(conn, paths, "KO", ASOF, "r1", {"stopped": True, "stopped_at_gate": "G2",
                                                      "reason_code": reason})
    d = _reg(conn)
    assert d["last_review_date"] == ASOF
    assert d["last_full_review_date"] == (ASOF if advanced else OLD_FULL)


# ---------------------------------------------------------------- M3-D10-02（F05 已修，本測試為回歸）

def test_kill_criteria_due_filled_from_phase_a(env, tmp_path):
    conn, paths = env
    d = tmp_path / "td"
    d.mkdir()
    card = {"ticker": "AAPL", "as_of": ASOF, "signal": "WATCH", "reason_code": "PRICE_ABOVE_CAP",
            "conclusion": "觀察", "red_light": False, "decision_log_id": ""}
    entry = {"ticker": "AAPL", "cik": "0000320193", "status": "WATCH"}     # 模型漏寫 kill_criteria_due
    log = {"decision_id": "", "decision_date": ASOF, "ticker": "AAPL", "action": "WATCH", "reason_one_liner": "測試"}
    pa = {"thesis_draft": {"kill_criteria": [{"text": "服務營收連兩季衰退", "due": "2027-06-30"},
                                             {"text": "毛利率跌破 40%", "due": "2027-12-31"}]}}
    for n, v in (("signal_card.json", card), ("registry_entry.json", entry), ("decision_log.json", log),
                 ("phaseA.json", pa)):
        (d / n).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text("論點", encoding="utf-8")
    write_full_result(conn, paths, "AAPL", ASOF, "r1", d)
    assert _reg(conn, "AAPL")["kill_criteria_due"] == [{"text": "服務營收連兩季衰退", "due": "2027-06-30"},
                                                       {"text": "毛利率跌破 40%", "due": "2027-12-31"}]


# ---------------------------------------------------------------- M3_D9-02

def test_skill_hashes_include_routing(env, tmp_path):
    _, real = env
    root = tmp_path / "pkg"
    (root / "routing" / "prompts").mkdir(parents=True)
    (root / "routing" / "prompts" / "FULL_B.md").write_text("v1", encoding="utf-8")
    p = Paths(root=root, state=real.state)
    h1 = writeback.skill_hashes_file(p)
    files = json.loads((p.state / h1).read_text(encoding="utf-8"))
    assert "routing/prompts/FULL_B.md" in files
    (root / "routing" / "prompts" / "FULL_B.md").write_text("v2", encoding="utf-8")
    assert writeback.skill_hashes_file(p) != h1
