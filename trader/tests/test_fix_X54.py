"""X54（設計決策 D77）：訊號卡選填欄 p08_final。

FULL_B 在訊號卡填 p08_final（與 report.md「P-08 最終」行一致）；PRICE 觸發的 HOLD_REVIEW 讀
last_full_signal_card.json 的 p08_final.quality 判頂級複利型的品質優；欄缺＝不算優。舊卡（無此欄）仍合法。"""
import json
from pathlib import Path

import pytest

from trader import db
from trader.analysis import writeback
from trader.analysis.writeback import write_full_result, write_stop_result
from trader.jobs import deep
from trader.jobs.review_jobs import _last_full_card
from trader.paths import get_paths

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "berkshire-deep-merged" / "assets" / "signal_card_schema.json"
PROMPTS = ROOT / "routing" / "prompts"
ASOF = "2026-10-16"
P08 = {"items_with_data": 5, "passed": 3, "rate": 60, "quality": "優"}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.fixture
def env(state_dir):
    paths = get_paths().ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    yield conn, paths
    conn.close()


def _tdir(tmp_path: Path, name: str, p08: dict | None) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    card = {"schema": "berkshire-deep-merged/signal-v1", "ticker": "AAPL", "as_of": ASOF, "signal": "WATCH",
            "reason_code": "PASS_ALL", "conclusion": "觀察", "auto_buy_cap": 150.0,
            "value_per_share": {"conservative": 120.0, "base": 160.0}, "red_light": False, "decision_log_id": ""}
    if p08 is not None:
        card["p08_final"] = dict(p08)
    files = {
        "signal_card.json": card,
        "registry_entry.json": {"ticker": "AAPL", "cik": "0000320193", "status": "WATCH", "grade": "B",
                                "auto_buy_cap": 150.0},
        "decision_log.json": {"decision_id": "", "decision_date": ASOF, "ticker": "AAPL", "action": "WATCH",
                              "screen_decision": "PASS", "gate3_pass": True, "reason_one_liner": "測試"},
    }
    for n, obj in files.items():
        (d / n).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    (d / "thesis.md").write_text("論點內容", encoding="utf-8")
    return d


def test_schema_has_optional_p08_final():
    sc = json.loads(_read(SCHEMA))
    p = sc["p08_final"]
    assert set(k for k in p if not k.startswith("_")) == {"items_with_data", "passed", "rate", "quality"}
    assert [o.strip() for o in p["quality"].split("|")] == ["優", "可", "降級"]
    assert "選填" in p["_說明"] and "D77" in p["_說明"] and "P-08 最終" in p["_說明"]
    assert "required" not in sc and "additionalProperties" not in sc   # 範本式 schema，無必填清單
    # 停止卡以 schema 欄名建空白列：p08_final＝null（未評）
    assert writeback._blank_from_fields(sc)["p08_final"] is None


@pytest.mark.parametrize("p08", [None, P08])
def test_card_number_checks_accept_with_and_without_p08_final(p08):
    card = {"target_weight_pct": 5, "max_weight_pct": {"tail": 10}}
    if p08 is not None:
        card["p08_final"] = dict(p08)
    notes: list[str] = []
    assert deep._norm_card_numbers(card, False, notes) == []
    writeback._card_numbers(card, notes)
    assert notes == []
    assert card.get("p08_final") == p08


@pytest.mark.parametrize("p08", [None, P08])
def test_writeback_keeps_p08_final_for_price_hold_review(env, tmp_path, p08):
    conn, paths = env
    did = write_full_result(conn, paths, "AAPL", ASOF, "r1", _tdir(tmp_path, "a", p08))
    card = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id = ?", (did,)).fetchone()["data"])
    assert card.get("p08_final") == p08
    last = _last_full_card(conn, "AAPL", ASOF)          # PRICE 觸發的 HOLD_REVIEW 附這張卡
    assert last is not None and last[1].get("p08_final") == p08


def test_stop_card_has_null_p08_final(env):
    conn, paths = env
    did = write_stop_result(conn, paths, "AAPL", ASOF, "r", {"stopped": True, "reason_code": "MISSING_DATA",
                                                             "stopped_at_gate": "G0"})
    card = json.loads(conn.execute("SELECT data FROM signals WHERE decision_id = ?", (did,)).fetchone()["data"])
    assert "p08_final" in card and card["p08_final"] is None


def test_full_b_requires_p08_final_matching_report():
    lines = [ln for ln in _read(PROMPTS / "FULL_B.md").splitlines() if "p08_final" in ln]
    assert len(lines) == 1
    ln = lines[0]
    for k in ("items_with_data", "passed", "rate", "quality", "D77", "report.md", "P-08 最終", "一致"):
        assert k in ln, k
    assert "{" not in ln and "}" not in ln


def test_hold_review_reads_report_then_last_full_card():
    hr = _read(PROMPTS / "HOLD_REVIEW.md")
    lines = [ln for ln in hr.splitlines() if "頂級複利型（3a、3c）的「品質優」" in ln]
    assert len(lines) == 1
    ln = lines[0]
    assert ln.index("P-08 最終") < ln.index("last_full_signal_card.json") < ln.index("p08_final.quality")
    assert "D77" in ln and "不算品質優" in ln and "PRICE" in ln
    assert "{" not in ln and "}" not in ln
    # check-X51：該句不得切斷第 6 點「TRIM 範圍…荒謬級（至少一半），」與「trim_fraction 寫比例」
    rows = hr.splitlines()
    i = next(k for k, r in enumerate(rows) if "TRIM 範圍" in r)
    assert rows[i].endswith("荒謬級（至少一半），") and rows[i + 1].lstrip().startswith("trim_fraction 寫比例")
    j = rows.index(ln)
    assert rows[j - 1].lstrip().startswith("第一年內碰到荒謬級") and rows[j + 1].startswith("7. ")
