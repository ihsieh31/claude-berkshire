"""RM6-4：M6_S6-NF-1（舊資料日停止結果）、NF-2（最新卡依資料日）、NF-3／D79（估值字句濾網）、
M6_S7_N01／D80（STEP0 缺資料停止不否決先前閘門）、write_full_result 舊資料日註記。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trader import db
from trader.analysis import writeback
from trader.analysis.writeback import write_full_result, write_stop_result
from trader.jobs.review_jobs import _gate3_info, _stage_thesis
from trader.paths import get_paths
from trader.portfolio import correlation
from trader.portfolio.holdings_view import _valued_card
from trader.portfolio.sizing import _latest_gate_decision

ASOF = "2026-10-16"
NEWER = "2026-10-18"
OLD = "2026-10-01"


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield conn, paths
    conn.close()


def _reg_set(conn, t, status, data):
    db.upsert(conn, "registry", {"ticker": t, "status": status, "data": {"ticker": t, "status": status, **data},
                                 "updated_at": db.now_iso()}, key=["ticker"])


def _reg(conn, t):
    r = conn.execute("SELECT status, data FROM registry WHERE ticker=?", (t,)).fetchone()
    return r["status"], json.loads(r["data"])


def _decision(conn, t, d, did, data, signal="WATCH"):
    db.insert(conn, "decisions", {"decision_id": did, "ticker": t, "decision_date": d, "run_id": "r0",
                                  "job_id": None, "signal": signal, "reason_code": "X", "data": data,
                                  "created_at": db.now_iso()})


def _open_buy(conn, t, oid="RM64-o1"):
    row = {"order_id": oid, "ticker": t, "side": "BUY", "status": "open"}
    for c in conn.execute("PRAGMA table_info(orders)").fetchall():
        name, typ, notnull, dflt, pk = c[1], (c[2] or "").upper(), c[3], c[4], c[5]
        if name in row or not notnull or dflt is not None or pk:
            continue
        row[name] = db.now_iso() if name.endswith("_at") else (
            1 if any(x in typ for x in ("INT", "REAL", "NUM", "FLOA", "DOUB")) else "x")
    db.insert(conn, "orders", row)
    return oid


def _order_status(conn, oid):
    return conn.execute("SELECT status FROM orders WHERE order_id=?", (oid,)).fetchone()["status"]


def _last_log(conn, t):
    return json.loads(conn.execute("SELECT data FROM decisions WHERE ticker=? ORDER BY rowid DESC LIMIT 1",
                                   (t,)).fetchone()["data"])


BP = {"tranches_left": 2, "target_weight": 0.1}


# ---------------------------------------------------------------- M6_S6-NF-1

def test_old_asof_stop_keeps_newer_state_holding(env):
    conn, paths = env
    _reg_set(conn, "KO", "HOLDING", {"grade": "A", "auto_buy_cap": 100.0, "target_weight": 0.1, "build_plan": BP,
                                     "last_reason_code": "OK_PRIOR", "last_review_date": NEWER,
                                     "last_full_review_date": OLD})
    _decision(conn, "KO", NEWER, "KO-20261018-1", {"review_type": None, "action": "BUY"}, "BUY")
    oid = _open_buy(conn, "KO")
    write_stop_result(conn, paths, "KO", ASOF, "r1", {"stopped": True, "stopped_at_gate": "G2",
                                                      "reason_code": "MOAT_FAIL"})
    status, d = _reg(conn, "KO")
    assert status == "HOLDING"
    assert d["grade"] == "A" and d["auto_buy_cap"] == 100.0 and d["target_weight"] == 0.1
    assert d["build_plan"] == BP                       # 剩餘批次不歸 0
    assert d["last_reason_code"] == "OK_PRIOR"
    assert d["last_full_review_date"] == ASOF and d["last_review_date"] == NEWER
    assert _order_status(conn, oid) == "open"          # 未成交買單屬較新結論，不取消
    notes = _last_log(conn, "KO")["framework_notes"]
    assert any("M6_S6-NF-1" in n for n in notes)
    assert not any("取消未成交買單" in n or "歸 0" in n for n in notes)


def test_old_asof_stop_keeps_newer_state_non_holding(env):
    conn, paths = env
    _reg_set(conn, "PEP", "WATCH", {"grade": "B", "auto_buy_cap": 90.0, "last_reason_code": "PRICE_ABOVE_CAP",
                                    "last_full_review_date": NEWER, "last_review_date": NEWER})
    _decision(conn, "PEP", NEWER, "PEP-20261018-1", {"review_type": "PRICE", "action": "WATCH"})
    write_stop_result(conn, paths, "PEP", ASOF, "r1", {"stopped": True, "stopped_at_gate": "G3",
                                                       "reason_code": "REJECT", "rule_ids": ["NEVER-04a"]})
    status, d = _reg(conn, "PEP")
    assert status == "WATCH"
    assert d["grade"] == "B" and d["auto_buy_cap"] == 90.0 and d["last_reason_code"] == "PRICE_ABOVE_CAP"
    assert "integrity_reject" not in d                 # 誠信淘汰寫入一併捨棄
    assert d["last_full_review_date"] == NEWER         # 不往回改
    notes = _last_log(conn, "PEP")["framework_notes"]
    assert not any("integrity_reject" in n for n in notes)


def test_current_stop_still_overwrites(env):
    conn, paths = env
    _reg_set(conn, "PEP", "WATCH", {"grade": "B", "auto_buy_cap": 90.0})
    _decision(conn, "PEP", OLD, "PEP-20261001-1", {"review_type": None, "action": "WATCH"})
    write_stop_result(conn, paths, "PEP", ASOF, "r1", {"stopped": True, "stopped_at_gate": "G2",
                                                       "reason_code": "MOAT_FAIL"})
    _, d = _reg(conn, "PEP")
    assert d["grade"] is None and d["last_reason_code"] == "MOAT_FAIL"


# ---------------------------------------------------------------- M6_S7_N01／D80

def test_step0_missing_data_does_not_veto_prior_gates(env):
    conn, paths = env
    _reg_set(conn, "KO", "HOLDING", {"grade": "A", "auto_buy_cap": 100.0, "build_plan": BP,
                                     "last_full_review_date": OLD})
    _decision(conn, "KO", OLD, "KO-20261001-1", {"review_type": None, "action": "BUY", "gate1_not_deleted": True,
                                                 "gate2_price_below_cap": True, "gate3_pass": True,
                                                 "gate3_failures": []}, "BUY")
    oid = _open_buy(conn, "KO")
    did = write_stop_result(conn, paths, "KO", ASOF, "r1", {"stopped": True, "stopped_at_gate": "STEP0",
                                                            "reason_code": "MISSING_DATA"})
    log = _last_log(conn, "KO")
    assert log["gate1_not_deleted"] is None and log["gate2_price_below_cap"] is None and log["gate3_pass"] is None
    assert log["gate3_failures"] == []
    assert any("D80" in n for n in log["framework_notes"])
    assert _order_status(conn, oid) == "open"
    _, d = _reg(conn, "KO")
    assert d["build_plan"] == BP and d["last_full_review_date"] == OLD
    g = _latest_gate_decision(conn, "KO", ASOF)
    assert g is not None and g["decision_id"] == "KO-20261001-1" and g["decision_id"] != did
    assert _gate3_info(conn, "KO", ASOF) == ("通過", "KO-20261001-1")


def test_analytic_missing_data_stop_still_fails_closed(env):
    conn, paths = env
    _reg_set(conn, "KO", "HOLDING", {"grade": "A", "build_plan": BP})
    oid = _open_buy(conn, "KO")
    write_stop_result(conn, paths, "KO", ASOF, "r1", {"stopped": True, "stopped_at_gate": "G2",
                                                      "reason_code": "MISSING_DATA"})
    log = _last_log(conn, "KO")
    assert log["gate2_price_below_cap"] is False and log["gate3_pass"] is False
    assert _order_status(conn, oid) == "cancelled"
    assert _reg(conn, "KO")[1]["build_plan"]["tranches_left"] == 0


# ---------------------------------------------------------------- M6_S6-NF-2

def _signal(conn, did, as_of, created, data):
    db.insert(conn, "signals", {"decision_id": did, "ticker": "KO", "signal": "WATCH",
                                "data": {"as_of": as_of, **data}, "created_at": created})


def test_latest_cards_ordered_by_as_of(env):
    conn, _ = env
    _signal(conn, "KO-NEW", NEWER, "2026-10-18T10:00:00+00:00", {"value_per_share": {"conservative": 80.0}})
    _signal(conn, "KO-OLD", OLD, "2026-10-19T10:00:00+00:00",   # 舊資料日、較晚寫入
            {"value_per_share": {"conservative": 60.0}})
    assert _valued_card(conn, "KO")["as_of"] == NEWER
    first = conn.execute(correlation._SIGNALS_SQL, ("KO",)).fetchone()
    assert json.loads(first["data"])["as_of"] == NEWER


# ---------------------------------------------------------------- M6_S6-NF-3／D79

DROP = ["保守價值 85", "基準價值 100 美元", "悲觀價值 60", "樂觀價值 130", "殖利率 5.2%", "殖利率：4%",
        "預期年化報酬率 12%", "預期報酬率 9%", "P／E 18", "P / E 18", "PE 18 倍", "荒謬價格線 300", "荒謬線 300",
        "荒謬價 300", "Intrinsic value 120", "margin of safety 30%", "buy cap 95", "FCF yield 6%",
        "fair value estimate 90"]
KEEP = ["失效條件：30 年期公債殖利率 4.5% 以上連續 2 季", "10 年期美債殖利率 4.2%", "國庫券殖利率 5%",
        "債券殖利率 3% 的保險資金", "fair value 85 million of investments", "EPS 年增 8%", "PEG 為 1.2",
        "上檔空間有限，管理層保守", "價值區間 2025–30 年", "公允價值 85 百萬美元"]


@pytest.mark.parametrize("line", DROP)
def test_d79_valuation_words_dropped(line):
    assert writeback.scrub_valuation_lines("論點\n" + line + "\n結尾") == "論點\n結尾"


@pytest.mark.parametrize("line", KEEP)
def test_d79_business_lines_kept(line):
    assert writeback.scrub_valuation_lines(line) == line


def test_drop_valuation_sections_md_heading():
    text = ("# KO\n## 估值\n情境 A 85，情境 B 100\n| 情境 | 數值 |\n| A | 85 |\n說明文字沒有數字\n"
            "失效條件：2027 年前營收衰退\n## 護城河\n市占率 45%")
    assert writeback.drop_valuation_sections(text) == (
        "# KO\n## 估值\n| 情境 | 數值 |\n說明文字沒有數字\n失效條件：2027 年前營收衰退\n## 護城河\n市占率 45%")


def test_drop_valuation_sections_bold_and_numbered():
    text = "**三、保守價值區間**\n情境 85–95\n**護城河**\n毛利率 60%\n一、估值\n數字 1\n二、競爭\n對手 3 家"
    assert writeback.drop_valuation_sections(text) == \
        "**三、保守價值區間**\n**護城河**\n毛利率 60%\n一、估值\n二、競爭\n對手 3 家"


@pytest.mark.parametrize("text,gone,kept", [
    ("## 保守價值區間\n### 熊市情境\n每股 85 美元\n### 基準情境\n情境值 100\n## 護城河\n市占率 45%",
     ["85", "100"], ["### 熊市情境", "市占率 45%"]),
    ("## 估值\n**熊市**\n情境 85\n## 護城河\n毛利率 60%", ["85"], ["**熊市**", "毛利率 60%"]),
    ("## 估值\n一、保守情境 85\n說明\n## 競爭\n對手 3 家", ["85"], ["說明", "對手 3 家"]),
    ("### 3. 價值評估\n情境 85\n### 4. 管理層\n任期 12 年", ["情境 85"], ["### 3. 價值評估", "任期 12 年"]),
])
def test_drop_valuation_sections_subheadings(text, gone, kept):
    out = writeback.drop_valuation_sections(text)
    assert all(g not in out for g in gone) and all(k in out for k in kept)


def test_stage_thesis_drops_valuation_section_numbers(env, tmp_path):
    conn, paths = env
    paths.theses.mkdir(parents=True, exist_ok=True)
    (paths.theses / "KO_2025-10-01.md").write_text(
        "# KO 論點\n## 保守價值區間\n情境 A：85、情境 B：100\n失效條件：2027-06-30 前有機營收連兩年負成長\n"
        "## 護城河\n毛利率 60–62%，護城河來自品牌\n", encoding="utf-8")
    sb = tmp_path / "sb"
    job = SimpleNamespace(ticker="KO", params={"thesis_file": "theses/KO_2025-10-01.md"})
    text = (sb / _stage_thesis(sb, job, paths)[0]).read_text(encoding="utf-8")
    assert "情境 A" not in text and "85" not in text
    assert "## 保守價值區間" in text and "2027-06-30 前有機營收連兩年負成長" in text
    assert "毛利率 60–62%，護城河來自品牌" in text


# ---------------------------------------------------------------- 尾巴：write_full_result 舊資料日註記

def test_full_result_old_asof_notes_match_actions(env, tmp_path):
    conn, paths = env
    _reg_set(conn, "AAPL", "WATCH", {"grade": "B", "build_plan": BP, "integrity_reject": True,
                                     "last_full_review_date": NEWER})
    _decision(conn, "AAPL", NEWER, "AAPL-20261018-1", {"review_type": None, "action": "BUY"}, "BUY")
    oid = _open_buy(conn, "AAPL")
    d = tmp_path / "td"
    d.mkdir()
    card = {"ticker": "AAPL", "as_of": ASOF, "signal": "WATCH", "reason_code": "PRICE_ABOVE_CAP",
            "conclusion": "觀察", "red_light": False, "decision_log_id": ""}
    entry = {"ticker": "AAPL", "cik": "0000320193", "status": "WATCH"}
    log = {"decision_id": "", "decision_date": ASOF, "ticker": "AAPL", "action": "WATCH", "reason_one_liner": "測試"}
    for n, v in (("signal_card.json", card), ("registry_entry.json", entry), ("decision_log.json", log)):
        (d / n).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text("論點", encoding="utf-8")
    write_full_result(conn, paths, "AAPL", ASOF, "r1", d)
    assert _order_status(conn, oid) == "open"
    _, reg = _reg(conn, "AAPL")
    assert reg["build_plan"] == BP and reg["integrity_reject"] is True
    notes = _last_log(conn, "AAPL")["framework_notes"]
    assert not any("取消未成交買單" in n for n in notes)
    assert not any("剩餘批次歸 0" in n or "以新框架 T 為上限" in n for n in notes)
    assert not any("清除誠信淘汰標記" in n for n in notes)
    assert any("保留、未清除" in n for n in notes)
