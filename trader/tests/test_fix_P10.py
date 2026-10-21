"""P10（M2-B2 review_due 腳本端＋D23）：邊緣觸發、時點邊界、D23 價格觸發範圍、≤0 線、C 級、next_full_review_due、
NONE 說明與輸出追溯、理由字串含值。全部離線。"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "berkshire-deep-merged" / "scripts" / "review_due.py"
_spec = importlib.util.spec_from_file_location("review_due_p10", SCRIPT)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

ASOF = "2026-10-16"


def px(rows):
    return pd.DataFrame([{"ticker": "X", "date": date.fromisoformat(d), "close": c} for d, c in rows])


def series(vals, end=ASOF):
    d0 = date.fromisoformat(end)
    return pd.DataFrame({"date": [d0 - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))], "close": vals})


HOLD = {"ticker": "X", "status": "HOLDING", "last_review_date": "2026-10-15", "last_full_review_date": "2026-06-01",
        "t30_at_valuation": 0.047}
WATCH = dict(HOLD, status="WATCH")


# ---- M2_D9_01（含 M2_D4_05）：狀態型觸發改邊緣觸發
def test_sticky_triggers_do_not_refire_after_review():
    e = dict(HOLD, cost=120, kill_criteria_due=[{"text": "k", "due": "2026-09-01"}],
             sell_review_above=50, absurd_price_line=80)
    p = px([("2026-10-14", 100), ("2026-10-15", 100), ("2026-10-16", 100)])
    assert R.check(e, [], p, ASOF, 0.047)[0] == "NONE"


def test_cost_crossing_after_review_triggers():
    e = dict(HOLD, cost=120)
    p = px([("2026-10-15", 107), ("2026-10-16", 105)])   # 107/120−1＝−10.8% → 105/120−1＝−12.5%；單日 −1.9%
    act, why, _ = R.check(e, [], p, ASOF, 0.047)
    assert act == "THESIS" and any("較成本 −12%" in w and "120.00" in w and "105.00" in w for w in why)


def test_lines_crossing_after_review_trigger_price():
    e = dict(HOLD, sell_review_above=104, absurd_price_line=105)
    p = px([("2026-10-15", 103), ("2026-10-16", 106)])   # +2.9%
    act, why, _ = R.check(e, [], p, ASOF, 0.047)
    assert act == "PRICE"
    assert any("sell_review_above" in w and "104.00" in w for w in why)
    assert any("荒謬級線 105.00" in w for w in why)


def test_lines_missing_close_at_review_falls_back_to_review_price():
    e = dict(HOLD, absurd_price_line=105, review_price=110)   # 上次分析時已在線上（以 review_price 判）
    p = px([("2026-10-16", 106)])
    assert R.check(e, [], p, ASOF, 0.047)[0] == "NONE"
    assert R.check(dict(e, review_price=100), [], p, ASOF, 0.047)[0] == "PRICE"


def test_kill_criteria_due_edge():
    e = dict(HOLD, last_review_date="2026-10-01", kill_criteria_due=[{"text": "毛利率", "due": "2026-10-10"},
                                                                       {"text": "舊", "due": "2026-10-01"}])
    act, why, _ = R.check(e, [], None, ASOF, 0.047)
    assert act == "THESIS"
    assert any("due 2026-10-10" in w and "毛利率" in w for w in why) and not any("舊" in w for w in why)


def test_quarter_high_only_after_review():
    e = dict(HOLD, last_review_date="2026-10-01")   # D23：單季 −25% 只對持股
    old_high = px([("2026-08-01", 140), ("2026-10-01", 100), ("2026-10-16", 99)])
    assert R.check(e, [], old_high, ASOF, 0.047)[0] == "NONE"
    new_drop = px([("2026-10-01", 100), ("2026-10-05", 132), ("2026-10-16", 98)])
    act, why, _ = R.check(e, [], new_drop, ASOF, 0.047)
    assert act == "THESIS" and any("2026-10-05" in w and "132.00" in w for w in why)


# ---- M2_D6_04：申報日／新聞日＝上次分析日
def test_same_day_filing_and_news_counted():
    e = dict(HOLD, last_review_date="2026-10-01")
    assert R.check(e, [{"form": "10-K", "filingDate": "2026-10-01", "items": ""}], None, ASOF, 0.047)[0] == "FULL"
    assert R.check(e, [{"form": "8-K", "filingDate": "2026-10-01", "items": "4.02"}], None, ASOF, 0.047)[0] == "FULL"
    _, _, tri = R.check(e, [], None, ASOF, 0.047, [{"date": "2026-10-01", "headline": "h"}])
    assert [t["headline"] for t in tri] == ["h"]


# ---- M2_D1_07：分析日 → 次一交易日的 ±4%；恰好 1pp
def test_first_day_move_counts_for_holding():
    e = dict(HOLD, last_review_date="2026-10-01")
    p = px([("2026-10-01", 100), ("2026-10-02", 95.5), ("2026-10-05", 95.5), ("2026-10-16", 95.5)])
    act, why, _ = R.check(e, [], p, ASOF, 0.047)
    assert act == "THESIS" and any("單日 ±4%" in w and "2026-10-02" in w and "-4.5%" in w for w in why)


def test_t30_exact_1pp():
    assert R.check(dict(WATCH, t30_at_valuation=0.042), [], None, ASOF, 0.052)[0] == "PRICE"


# ---- D23：±4% 只對持股；−20% 刪除
def test_d23_watch_ignores_daily_move_and_minus20():
    e = dict(WATCH, last_review_date="2026-10-01", review_price=100, auto_buy_cap=95)
    p = px([("2026-10-01", 100), ("2026-10-02", 100), ("2026-10-05", 90), ("2026-10-16", 89)])
    act, why, _ = R.check(e, [], p, ASOF, 0.047)
    assert act == "PRICE", why   # 跌破上限不再被 ±4% 的 THESIS 遮蔽
    assert not any("±4%" in w or "20%" in w for w in why)
    p2 = px([("2026-10-01", 100), ("2026-10-02", 78), ("2026-10-16", 78)])
    assert R.check(dict(e, auto_buy_cap=70), [], p2, ASOF, 0.047)[0] == "NONE"


def test_d23_holding_still_daily_move():
    e = dict(HOLD, last_review_date="2026-10-01")
    p = px([("2026-10-01", 100), ("2026-10-02", 100), ("2026-10-05", 94), ("2026-10-16", 94)])
    assert R.check(e, [], p, ASOF, 0.047)[0] == "THESIS"


# ---- M2_D9_02（review_due 部分）：≤0 線與上限視同缺
def test_nonpositive_lines_ignored():
    p = px([("2026-10-15", 100), ("2026-10-16", 100)])
    det = {}
    act, why, _ = R.check(dict(HOLD, sell_review_above=-33, absurd_price_line=0), [], p, ASOF, 0.047, detail=det)
    assert act == "NONE"
    assert any("sell_review_above" in u for u in det["unchecked"])
    assert R.check(dict(WATCH, auto_buy_cap=-5), [], p, ASOF, 0.047)[0] == "NONE"


# ---- M2_D2_04：C 級觀察名單跌破上限不排 PRICE
def test_watch_grade_c_no_price():
    p = px([("2026-10-15", 100), ("2026-10-16", 100)])
    e = dict(WATCH, auto_buy_cap=150)
    assert R.check(dict(e, grade="C"), [], p, ASOF, 0.047)[0] == "NONE"
    assert R.check(dict(e, grade="B"), [], p, ASOF, 0.047)[0] == "PRICE"
    assert R.check(e, [], p, ASOF, 0.047)[0] == "PRICE"   # 缺等級照舊


# ---- M2_D9_07：next_full_review_due
def test_next_full_review_due():
    act, why, _ = R.check(dict(WATCH, next_full_review_due="2026-10-10"), [], None, ASOF, 0.047)
    assert act == "FULL" and any("2026-10-10" in w for w in why)
    assert R.check(dict(WATCH, next_full_review_due="2026-05-01"), [], None, ASOF, 0.047)[0] == "NONE"   # 早於上次完整分析
    assert R.check(dict(WATCH, next_full_review_due="2026-11-01"), [], None, ASOF, 0.047)[0] == "NONE"


# ---- M2-D5-01：NONE 說明區分 P-62 壓掉、欄位缺、t30 缺
def test_none_explains_suppressed_and_unchecked():
    det = {}
    e = dict(WATCH, status="REJECT", last_full_review_date="2025-09-01")
    act, why, _ = R.check(e, [{"form": "10-K", "filingDate": "2026-10-15", "items": ""}], None, ASOF, 0.047, detail=det)
    assert act == "NONE" and "P-62" in why[0] and "新年報" in why[0]
    assert any("新年報" in s for s in det["suppressed"])
    det = {}
    p = px([("2026-10-15", 100), ("2026-10-16", 100)])
    act, why, _ = R.check(dict(WATCH, t30_at_valuation=None), [], p, ASOF, None, detail=det)
    assert act == "NONE" and "未檢查" in why[0]
    assert any("auto_buy_cap" in u for u in det["unchecked"]) and any("t30" in u for u in det["unchecked"])
    assert det["checked"]["last_close"] == 100 and det["checked"]["last_close_date"] == "2026-10-16"


def test_main_writes_trace_fields(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"entries": [dict(WATCH, ticker="AAA", status="REJECT"), dict(WATCH, ticker="BBB")]}),
                   encoding="utf-8")
    off = tmp_path / "off.json"
    off.write_text(json.dumps({"AAA": [{"form": "10-K", "filingDate": "2026-10-15", "items": ""}]}), encoding="utf-8")
    prices = tmp_path / "daily.csv"
    prices.write_text("ticker,date,close\nBBB,2026-10-15,100\nBBB,2026-10-16,100\n", encoding="utf-8")
    out = tmp_path / "due.json"
    subprocess.run([sys.executable, "-B", str(SCRIPT), "--registry", str(reg), "--asof", ASOF, "--offline", str(off),
                    "--prices", str(prices), "--out", str(out)], check=True, capture_output=True, text=True,
                   env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["signal_version"] == "review-due-v2"   # 框架 pipelines/review.py 以此核對
    rows = {r["ticker"]: r for r in d["results"]}
    assert rows["AAA"]["action"] == "NONE" and rows["AAA"]["suppressed"]
    assert any("t30" in u for u in rows["BBB"]["unchecked"]) and rows["BBB"]["checked"]["last_close"] == 100


# ---- M2-D5-02：理由字串含表單＋申報日、日期與數值
def test_reasons_carry_values():
    e = dict(HOLD, last_review_date="2026-10-01", last_full_review_date="2025-10-01")
    fl = [{"form": "10-Q", "filingDate": "2026-10-05", "items": ""}, {"form": "10-K", "filingDate": "2026-10-06", "items": ""}]
    _, why, _ = R.check(e, fl, None, ASOF, 0.047)
    s = " ".join(why)
    assert "10-Q 2026-10-05" in s and "10-K 2026-10-06" in s and "2025-10-01" in s


# ---- P10R：M2_D9_01 三種漏觸發情境
def test_thesis_and_price_same_period_price_reason_kept():
    """(1) 同期新季報（THESIS）＋剛跨過賣出檢視線：action 只回 THESIS，但 [PRICE] 理由必須留在 reasons，
    讓框架 THESIS 判完好後續接 PRICE（also_price，P53）；否則下一期基準已在線上，PRICE 永久漏掉。"""
    e = dict(HOLD, sell_review_above=104, review_price=103)
    p = px([("2026-10-15", 103), ("2026-10-16", 106)])
    act, why, _ = R.check(e, [{"form": "10-Q", "filingDate": "2026-10-15", "items": ""}], p, ASOF, 0.047)
    assert act == "THESIS"
    assert any(w.startswith("[PRICE]") and "sell_review_above" in w for w in why)


def test_stale_close_at_review_not_used_as_baseline():
    """(2) 複查日前後資料缺口：≤上次分析日的最後一筆收盤是幾週前的舊值（當時在線上），不得當成「上次已成立」。"""
    p = px([("2026-09-01", 110), ("2026-10-16", 106)])   # 9/1 之後到 10/16 之間缺資料
    e = dict(HOLD, absurd_price_line=105, review_price=100)
    act, why, _ = R.check(e, [], p, ASOF, 0.047)
    assert act == "PRICE" and any("荒謬級線" in w for w in why)
    det = {}
    assert R.check(dict(e, review_price=None), [], p, ASOF, 0.047, detail=det)[0] == "PRICE"   # 舊值不用、都缺 → 不成立
    assert det["checked"]["close_at_last_review"] is None
    # 收盤距上次分析日在 STALE_DAYS 內（週末＋休市）仍用：收盤與 review_price 都在線上 → 不再觸發
    p2 = px([("2026-10-10", 110), ("2026-10-16", 106)])
    assert R.check(dict(e, review_price=110), [], p2, ASOF, 0.047)[0] == "NONE"


def test_review_close_not_yet_in_file_uses_review_price():
    """(3) 複查跑時決策日收盤尚未入檔：審查用的是前一日收盤（review_price＝103，線下），之後入檔的決策日收盤 106 已跨線。
    兩者都滿足才算上次已成立 → 仍觸發。"""
    p = px([("2026-10-14", 103), ("2026-10-15", 106), ("2026-10-16", 107)])
    e = dict(HOLD, absurd_price_line=105, review_price=103)
    act, why, _ = R.check(e, [], p, ASOF, 0.047)
    assert act == "PRICE" and any("荒謬級線" in w for w in why)
    cost = dict(HOLD, cost=120, review_price=106)   # 審查看到 106（−11.7%），入檔的決策日收盤 105（−12.5%）
    p3 = px([("2026-10-14", 106), ("2026-10-15", 105), ("2026-10-16", 104.5)])
    act, why, _ = R.check(cost, [], p3, ASOF, 0.047)
    assert act == "THESIS" and any("較成本 −12%" in w for w in why)


# ---- P10R：D23 觀察名單不看單季 −25%
def test_d23_watch_quarter_drop_no_thesis_and_price_not_masked():
    e = dict(WATCH, last_review_date="2026-10-01", review_price=100, auto_buy_cap=80, grade="A")
    p = px([("2026-10-01", 100), ("2026-10-02", 99), ("2026-10-05", 97), ("2026-10-08", 90),
            ("2026-10-12", 85), ("2026-10-14", 78), ("2026-10-16", 70)])   # −30%，跌破上限
    act, why, _ = R.check(e, [], p, ASOF, 0.047)
    assert act == "PRICE" and not any("單季" in w for w in why)
    assert R.check(dict(e, auto_buy_cap=60), [], p, ASOF, 0.047)[0] == "NONE"


# ---- P10R：M2_D2_04 C／D 級觀察名單長債變動也不排 PRICE
def test_watch_grade_cd_t30_no_price():
    for g in ("C", "D"):
        det = {}
        assert R.check(dict(WATCH, grade=g), [], None, ASOF, 0.058, detail=det)[0] == "NONE"
        assert any("長債" in u for u in det["unchecked"])
    assert R.check(dict(WATCH, grade="B"), [], None, ASOF, 0.058)[0] == "PRICE"
    assert R.check(dict(HOLD, grade="C"), [], None, ASOF, 0.058)[0] == "PRICE"   # 持股照舊（只影響估值）


def test_grade_c_cap_note_only_when_below_cap():
    p = px([("2026-10-15", 100), ("2026-10-16", 100)])
    det = {}
    R.check(dict(WATCH, grade="C", auto_buy_cap=90), [], p, ASOF, 0.047, detail=det)
    assert not any("跌破上限" in u for u in det["unchecked"])
    det = {}
    R.check(dict(WATCH, grade="C", auto_buy_cap=150), [], p, ASOF, 0.047, detail=det)
    assert any("跌破上限" in u for u in det["unchecked"])
