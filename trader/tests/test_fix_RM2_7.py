"""RM2-7：skill 腳本複審修復（gate3_check、cap_calc、review_due）。

條目：M2-R5-01（gate3 null 當 'None'）、M2-R5-02（oe≤0＋缺成長誤標 MISSING_DATA）、M2-RS8-02（金融 oe≤0 仍輸出
覆寫 IV_base、oe＝0 除零）、M2-D5-01（狀態別過濾清空未檢查、丟掉的 triage 不留）、M2-RS8-03（腳本側 filings_failed）。
"""
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from trader.analysis.checks import _skill_module

C = _skill_module("cap_calc")
G3 = _skill_module("gate3_check")
RD = _skill_module("review_due")

BASE = {"long_rate": 0.047, "long_rate_20y_med": 0.035, "price": 50.0, "grade": "B", "oe": 1.0, "g10": 0.05, "g5": 0.05}
ASOF = "2026-10-15"


# ---------------------------------------------------------------- M2-R5-01

def _answers(**kw):
    q = json.load(open(G3.QPATH, encoding="utf-8"))
    A = {it["id"]: {"answer": "強" if "強" in it["pass_if"] else it["pass_if"][0], "reason": "r",
                    "evidence": [{"source": "10-K", "date": "2026-01-01"}]} for it in q["questions"]}
    return {"decision_date": ASOF, "price_seen_before_answering": False, "answers": A, **kw}


@pytest.mark.parametrize("at", ["2026-10-15 10:00 UTC", None])
def test_gate3_null_price_file_opened_at_is_blank(at):
    r = G3.evaluate(_answers(answered_at=at, price_file_opened_at=None), asof=ASOF)
    assert r["gate3_pass"] is True and r["timing_checked"] is False, r


def test_gate3_timing_still_checked_when_both_filled():
    bad = G3.evaluate(_answers(answered_at="2026-10-15 10:00", price_file_opened_at="2026-10-15 09:00"), asof=ASOF)
    ok = G3.evaluate(_answers(answered_at="2026-10-15 10:00", price_file_opened_at="2026-10-15 11:00"), asof=ASOF)
    assert bad["timing_checked"] and not bad["gate3_pass"] and ok["timing_checked"] and ok["gate3_pass"]


# ---------------------------------------------------------------- M2-R5-02

@pytest.mark.parametrize("oe", [-1.0, 0.0])
def test_oe_nonpositive_missing_growth_is_no_value_not_missing_data(oe):
    r = C.calc({**BASE, "oe": oe, "g10": None, "g5": None})
    assert r["no_value_reason"] == "oe_le_0" and "無法估值" in r["conclusion_hint"]
    assert "MISSING_DATA" not in r["conclusion_hint"] and r["auto_buy_cap"] is None


def test_positive_oe_missing_growth_unchanged():
    r = C.calc({**BASE, "g10": None, "g5": None})
    assert r["no_value_reason"] == "missing_growth" and r["conclusion_hint"].startswith("MISSING_DATA")


# ---------------------------------------------------------------- M2-RS8-02

FIN = dict(BASE, g10=0.06, g5=0.07, financial=True, largecap_pe_median=25, fin_pe_median=12, price=80)


@pytest.mark.parametrize("oe", [0, -1.0])
@pytest.mark.parametrize("ivs", [(1e5, 1e6, 2e6), (0.005, 0.01, 0.02)])
def test_financial_override_oe_nonpositive_no_sell_line_no_zerodiv(oe, ivs):
    r = C.calc(dict(FIN, oe=oe, iv_cons=ivs[0], iv_base=ivs[1], iv_opt=ivs[2]))
    assert r["no_value_reason"] in ("oe_le_0", "iv_cons_le_0")
    assert r["IV_base"] is None and r["IV_opt"] is None and r["auto_buy_cap"] is None and r["cap"] is None


def test_financial_override_positive_oe_unchanged():
    r = C.calc(dict(FIN, oe=2.0, iv_cons=60.0, iv_base=90.0, iv_opt=120.0))
    assert r["no_value_reason"] is None and r["IV_base"] == pytest.approx(90.0) and r["IV_opt"] == pytest.approx(120.0)


# ---------------------------------------------------------------- M2-D5-01／M2-RS8-03（腳本側）

def _px(vals, end=ASOF):
    d0 = date.fromisoformat(end)
    return pd.DataFrame({"date": [d0 - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))], "close": vals})


@pytest.mark.parametrize("status", ["REJECT", "TOO_HARD", "MISSING_DATA", "NO_ACTION"])
def test_status_filter_keeps_filings_unchecked(status):
    d = {}
    act, why, tri = RD.check({"ticker": "X", "status": status, "last_review_date": "2026-10-01",
                              "last_full_review_date": "2026-06-01"}, None, None, ASOF, 0.047, detail=d)
    assert act == "NONE" and d["filings_ok"] is False and d["filings_failed"] is True
    assert "申報抓取失敗：重大申報未檢查" in d["unchecked"]
    assert not any("無相關事件" in w for w in why) and "申報抓取失敗" in why[0], why


def test_status_filter_dropped_triage_goes_to_suppressed():
    d = {}
    act, why, tri = RD.check({"ticker": "X", "status": "REJECT", "last_review_date": "2026-10-01",
                              "last_full_review_date": "2026-06-01"},
                             [{"form": "8-K", "filingDate": "2026-10-05", "items": "7.01,9.01"}], None, ASOF, 0.047,
                             [{"date": "2026-10-06", "headline": "大新聞"}], detail=d)
    assert act == "NONE" and tri == [] and d["unchecked"] == []
    assert "[TRIAGE] 8-K 2026-10-05 7.01,9.01" in d["suppressed"] and "[TRIAGE] 新聞 2026-10-06 大新聞" in d["suppressed"]
    assert not any("無相關事件" in w for w in why)


def test_status_filter_no_events_still_says_none():
    d = {}
    act, why, _ = RD.check({"ticker": "X", "status": "REJECT", "last_review_date": "2026-10-01",
                            "last_full_review_date": "2026-06-01"}, [], None, ASOF, 0.047, detail=d)
    assert act == "NONE" and "本期無相關事件" in why[0] and d["filings_failed"] is False


def test_filings_failure_still_checks_price_and_12_months():
    h = {"ticker": "X", "status": "HOLDING", "last_review_date": "2026-10-01", "last_full_review_date": "2025-09-01",
         "review_price": 100, "cost": 100, "t30_at_valuation": 0.047}
    d = {}
    act, why, _ = RD.check(h, None, _px([100] * 119 + [80]), ASOF, 0.047, detail=d)
    assert act == "FULL" and d["filings_failed"] is True
    assert any("12 個月" in w for w in why) and any("成本 −12%" in w for w in why)
