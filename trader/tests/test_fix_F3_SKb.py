"""F3-SKb：V3-1 FG-M2-review_due／cap_calc／gate3／assets／build_dossier（deep skill 腳本與 assets）。
每個測試對應一個 A3 項目；修前失敗、修後通過。設計決策：D90（A3-M2B-01）、D91（A3-M2B-02）、D97（A3-M2A-02）。"""
import copy
import importlib.util
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
DEEP = ROOT / "berkshire-deep-merged"
SCRIPTS = DEEP / "scripts"


def _mod(name):
    for d in (SCRIPTS, ROOT / "berkshire-screen-merged" / "scripts"):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    spec = importlib.util.spec_from_file_location(f"skb_{name}", SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


RD = _mod("review_due")
CC = _mod("cap_calc")
G3 = _mod("gate3_check")
ASOF = "2026-10-15"   # 週四


def _bdays(vals_by_date):
    return pd.DataFrame({"date": list(vals_by_date), "close": list(vals_by_date.values())})


def _px_weekdays(start="2026-06-01", end=ASOF, close=lambda d: 100.0):
    d, out = date.fromisoformat(start), {}
    while d <= date.fromisoformat(end):
        if d.weekday() < 5:
            out[d] = close(d)
        d += timedelta(days=1)
    return _bdays(out)


BASE = dict(ticker="X", status="WATCH", last_review_date="2026-10-01", last_full_review_date="2026-06-01",
            review_price=100, t30_at_valuation=0.047, auto_buy_cap=80, sell_review_above=130, absurd_price_line=300)


# ---------------------------------------------------------------- review_due

def test_A3_M2A_01_single_day_move_counts_from_last_close_before_weekend_review():
    """上次分析日是週六：週五收盤 100 → 週一 95.5（−4.5%）必須算進單日 ±4%（修前從週一起算，漏掉）。"""
    h = dict(BASE, status="HOLDING", cost=100, last_review_date="2026-10-10")   # 2026-10-10 週六
    px = _px_weekdays(close=lambda d: 100.0 if d <= date(2026, 10, 9) else 95.5)
    dd = {}
    act, why, _ = RD.check(h, [], px, ASOF, 0.047, detail=dd)
    assert act == "THESIS" and any("單日 ±4%" in w for w in why), (act, why)
    assert dd["checked"]["close_at_last_review_date"] == "2026-10-09"
    # 交易日做的複查：起點即該日，照舊（當天收盤與次日比）
    h2 = dict(h, last_review_date="2026-10-12")
    assert RD.check(h2, [], px, ASOF, 0.047)[0] == "NONE"


@pytest.mark.parametrize("rc", ["GATE3_FAIL", "RED_LIGHT"])
def test_A3_M2A_06_long_rate_move_not_price_for_gate3_fail_or_red_light_watch(rc):
    flat = _px_weekdays()
    assert RD.check(BASE, [], flat, ASOF, 0.058)[0] == "PRICE"            # 對照：一般觀察名單照排
    dd = {}
    act, why, _ = RD.check(dict(BASE, last_reason_code=rc), [], flat, ASOF, 0.058, detail=dd)
    assert act == "NONE", (act, why)
    assert any(rc in u and "長債" in u for u in dd["unchecked"]), dd["unchecked"]
    # 持股不受影響（只擋觀察名單）
    hold = dict(BASE, status="HOLDING", cost=100, last_reason_code=rc)
    assert RD.check(hold, [], flat, ASOF, 0.058)[0] == "PRICE"


def test_A3_M2B_07_red_light_watch_below_cap_not_price():
    px = pd.DataFrame({"date": [date(2026, 10, 15) - timedelta(days=119 - i) for i in range(120)],
                       "close": [82 + (i % 2) * 0.5 for i in range(119)] + [79.5]})
    w = dict(BASE, review_price=82)
    assert RD.check(w, [], px, ASOF, 0.047)[0] == "PRICE"
    dd = {}
    act, _, _ = RD.check(dict(w, last_reason_code="RED_LIGHT"), [], px, ASOF, 0.047, detail=dd)
    assert act == "NONE" and any("RED_LIGHT" in u and "不排 PRICE" in u for u in dd["unchecked"]), dd
    assert RD.check(dict(w, last_reason_code="GATE3_FAIL"), [], px, ASOF, 0.047)[0] == "NONE"   # 原行為保留


def test_A3_M2E_02_main_isolates_per_ticker_check_errors(tmp_path):
    reg = {"entries": [dict(BASE, ticker="GOOD", cik=1, last_full_review_date="2025-01-01"),
                       dict(BASE, ticker="BAD", cik=2, last_full_review_date="2026-13-45")]}   # 壞日期 → check() 例外
    (tmp_path / "reg.json").write_text(json.dumps(reg), encoding="utf-8")
    (tmp_path / "off.json").write_text(json.dumps({"GOOD": [], "BAD": []}), encoding="utf-8")
    out = tmp_path / "due.json"
    r = subprocess.run([sys.executable, str(SCRIPTS / "review_due.py"), "--registry", str(tmp_path / "reg.json"),
                        "--asof", ASOF, "--offline", str(tmp_path / "off.json"), "--out", str(out)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and out.is_file(), r.stderr[-1500:]
    res = {x["ticker"]: x for x in json.loads(out.read_text(encoding="utf-8"))["results"]}
    assert res["GOOD"]["action"] == "FULL" and res["GOOD"]["check_error"] is None
    bad = res["BAD"]
    assert bad["action"] == "NONE" and bad["check_error"].startswith("check_error: ValueError")
    assert bad["reasons"][0].startswith("check_error") and any("全部觸發未檢查" in u for u in bad["unchecked"])


# ---------------------------------------------------------------- cap_calc

CAPX = dict(oe=5.0, g10=0.10, g5=0.08, grade="B", long_rate=0.045, long_rate_20y_med=0.04, price=60.0)


def test_A3_M2A_03_grade_a_half_threshold_only_for_positive_g5():
    r_d, gti = 0.10, 0.03
    price = CC.pv(5.0, -0.03, r_d, gti)        # 使 g_impl ≈ −3%
    for grade, key in (("A", "b_le_hist5y_half"), ("B", "b_le_hist5y")):
        r = CC.calc(dict(oe=5.0, g10=-0.02, g5=-0.04, grade=grade, long_rate=0.047, long_rate_20y_med=0.04, price=price))
        assert r["g_impl"] == pytest.approx(-0.03, abs=1e-6)
        assert r["g_impl_checks"][key] is False, (grade, r["g_impl_checks"])   # −3% > g5 −4%：A、B 皆不過
    # g5 > 0 時 A 級仍取一半
    r = CC.calc(dict(CAPX, grade="A", price=CC.pv(5.0, 0.05, r_d, gti)))
    assert r["g_impl_checks"]["b_le_hist5y_half"] is False                   # 5% > 8%/2


def test_A3_M2A_02_incr_roic_false_caps_at_iv0_null_unchanged():
    t, f, n = (CC.calc(dict(CAPX, incr_roic_gt_rd=v)) for v in (True, False, None))
    iv0_cap = 0.75 * t["IV0"]
    assert f["cap"] == pytest.approx(iv0_cap) and f["components"]["IV0_cap"] == pytest.approx(iv0_cap)
    assert f["auto_buy_cap"] <= CC.CONCLUSION_MARGIN * iv0_cap + 1e-9
    assert any("D97" in s and "≤ r_d" in s for s in f["notes"])
    assert n["cap"] == t["cap"] > iv0_cap and n["auto_buy_cap"] == t["auto_buy_cap"]
    assert any("未判定" in s and "D97" in s for s in n["notes"])
    assert n["incr_roic_gt_rd"] is None and f["incr_roic_gt_rd"] is False


def test_A3_M2B_01_auto_buy_cap_is_095_of_raw_and_no_degenerate_flip_test():
    r = CC.calc(CAPX)
    raw = r["auto_buy_cap_raw"]
    assert raw == pytest.approx(min(r["cap"], r["cap_discount_k"] * r["IV_base"]))
    assert r["auto_buy_cap"] == pytest.approx(0.95 * raw) == r["abc_effective"]
    assert r["conclusion_margin"] == 0.95
    # 行為不變：原結論測試區（距原始上限 <5%）→ WATCH；之下 → 可買；不再輸出退化的 E×0.9 翻轉測試
    near = CC.calc(dict(CAPX, price=raw * 0.97))
    assert "price_ge_cap" in near["watch_reasons"] and "conclusion_test_flip" not in near["watch_reasons"]
    assert "conclusion_test" not in near
    below = CC.calc(dict(CAPX, price=raw * 0.94))
    assert below["watch_reasons"] == [] and below["price_to_auto_buy_cap"] < 1
    # 金融類（P1／P4′ 綁定）同樣 ×0.95
    fx = dict(oe=6, g10=0.05, g5=0.05, grade="B", long_rate=0.045, financial=True, iv_cons=120, iv_base=200,
              iv_opt=220, largecap_pe_median=15, fin_pe_median=12, price=76.82)
    rf = CC.calc(fx)
    assert rf["auto_buy_cap_raw"] == pytest.approx(79.2) and rf["auto_buy_cap"] == pytest.approx(0.95 * 79.2)
    assert "price_ge_cap" in rf["watch_reasons"]


def test_A3_M2B_02_docs_use_actual_r_stress_r_sens_formula():
    doc = (DEEP / "references" / "rule_index" / "02_valuation.md").read_text(encoding="utf-8")
    val045 = next(ln for ln in doc.splitlines() if ln.startswith("- **結論測試**"))
    assert "（+4pp，低利率起點 +5pp）" not in val045
    assert "max（10%，30 年期 +7pp；低利率起點 +8pp）" in val045 and "D90" in val045
    p3 = next(ln for ln in doc.splitlines() if ln.strip().startswith("- P3 壓力價"))
    assert "＋3pp＋3pp" not in p3 and "max（10%，30 年期 +6pp；低利率起點 +7pp）" in p3
    # 文件寫的實際式與程式一致：非低利率起點且 30 年期 ≤4% 時 r_stress＝r_d＝10%
    for L, low, want in ((0.047, False, 0.107), (0.04, False, 0.10), (0.035, True, 0.105), (0.03, True, 0.10), (0.05, True, 0.12)):
        r = CC.calc(dict(CAPX, long_rate=L, long_rate_20y_med=(L + 0.01) if low else (L - 0.01)))
        assert r["r_stress"] == pytest.approx(want), (L, low, r["r_stress"])
    sk = (DEEP / "SKILL.md").read_text(encoding="utf-8")
    assert "`r_stress`＝max(10%, 30 年期 +6pp；低利率起點 +7pp)" in sk


# ---------------------------------------------------------------- gate3

def _q():
    return json.loads((DEEP / "assets" / "gate3_questionnaire.json").read_text(encoding="utf-8"))


def _all_pass(**kw):
    ev = [{"source": "10-K", "date": "2026-02-20"}]
    ans = {}
    for it in _q()["questions"]:
        a = "強" if it["id"] in ("M1", "M2", "M3") else it["pass_if"][0]
        ans[it["id"]] = {"answer": a, "reason": "r", "evidence": ev}
    d = {"ticker": "X", "decision_date": ASOF, "price_seen_before_answering": False, "answers": ans}
    d.update(kw)
    return d


def test_A3_M2A_07_questionnaire_note_matches_choices():
    q = _q()
    note = q["_說明"]
    assert "每題都可答「不知道」" not in note
    no_unknown = [it["id"] for it in q["questions"] if "不知道" not in it["choices"]]
    assert set(no_unknown) == {"C1", "C5", "M5", "M6"}
    assert "C1、C5、M5、M6 沒有「不知道」選項" in note and "最保守" in note
    sk = (DEEP / "SKILL.md").read_text(encoding="utf-8")
    assert "C1、C5、M5、M6" in sk.split("## 0A.")[1].split("## 1.")[0]
    assert "C1、C5、M5、M6" in sk.split("## 0.")[1].split("## 0A.")[0]


def test_A3_M2A_08_timing_compared_as_datetimes_with_offsets():
    assert G3.evaluate(_all_pass(), asof=ASOF)["gate3_pass"] is True
    # 作答 10:00 +08:00；開價格檔 03:30 UTC＝11:30 +08:00（較晚）：字串比較會誤判為「不晚於」
    ok = G3.evaluate(_all_pass(answered_at="2026-10-14 10:00 +08:00", price_file_opened_at="2026-10-14 03:30 Z"), asof=ASOF)
    assert ok["gate3_pass"] is True and ok["timing_checked"] is True, ok
    # 反向：字串看起來較晚、實際較早（09:00 +08:00 ＝ 01:00 UTC，早於作答 02:00 UTC）
    bad = G3.evaluate(_all_pass(answered_at="2026-10-14 02:00 UTC", price_file_opened_at="2026-10-14 09:00 +08:00"), asof=ASOF)
    assert bad["gate3_pass"] is False and any("價格隔離" in f for f in bad["failures"])
    # 無時區視為 Asia/Taipei
    naive = G3.evaluate(_all_pass(answered_at="2026-10-14 10:00", price_file_opened_at="2026-10-14 02:30 Z"), asof=ASOF)
    assert naive["gate3_pass"] is True and naive["timing_checked"] is True
    # 解析不了 → 不通過（無法確認價格隔離）
    junk = G3.evaluate(_all_pass(answered_at="昨天下午", price_file_opened_at="2026-10-14 10:00"), asof=ASOF)
    assert junk["gate3_pass"] is False and junk["timing_checked"] is False and any("時間格式錯" in f for f in junk["failures"])


def test_A3_M2A_08_non_dict_answer_reports_format_error():
    d = _all_pass()
    d["answers"]["C1"] = "10年寫得出"
    d["answers"]["M2"]["evidence"] = ["10-K 2026-02-20"]
    r = G3.evaluate(d, asof=ASOF)   # 修前 AttributeError
    assert r["gate3_pass"] is False
    assert any(f.startswith("C1 作答格式錯") for f in r["failures"]) and any(f.startswith("M2 證據格式錯") for f in r["failures"])
    r2 = G3.evaluate(_all_pass(answers=["C1"]), asof=ASOF)
    assert r2["gate3_pass"] is False and any("answers 不是" in f for f in r2["failures"])


def test_A3_M2D_01_evidence_date_defined_as_filing_date():
    assert "filingDate" in _q()["_說明"] and "期末日" in _q()["_說明"]
    assert "filingDate" in G3.__doc__ and "資料包已收錄" in G3.__doc__
    assert "filingDate" in (DEEP / "SKILL.md").read_text(encoding="utf-8").split("## 0A.")[1].split("## 1.")[0]


# ---------------------------------------------------------------- assets

def test_A3_M2A_11_registry_schema_documents_framework_keys_without_new_blank_keys():
    s = json.loads((DEEP / "assets" / "registry_schema.json").read_text(encoding="utf-8"))
    e = s["entries"][0]
    assert "RETIRED" in e["status"]
    ff = e["_framework_fields"]
    for k in ("last_reason_code", "filings_fail", "build_plan", "sold_out_at"):
        assert k in ff, k
        assert k not in e   # 只登錄於底線區塊：writeback 依 schema 建空白列時不多寫 null
    assert "0.95" in e["auto_buy_cap"] and "D90" in e["auto_buy_cap"]


def test_A3_M2A_12_report_template_lists_all_normalization_bases():
    t = (DEEP / "assets" / "analysis_report_template.md").read_text(encoding="utf-8")
    line = next(ln for ln in t.splitlines() if "正常化盈餘口徑" in ln)
    for k in ("ex_shock_median", "5y_median", "5y_mean"):
        assert k in line, k
    sc = json.loads((DEEP / "assets" / "signal_card_schema.json").read_text(encoding="utf-8"))
    assert "ex_shock_median | 5y_median | 5y_mean" in sc["distress_check"]["earnings_normalization"]


def test_A3_M2C_09_signal_card_schema_registers_hold_side_fields():
    sc = json.loads((DEEP / "assets" / "signal_card_schema.json").read_text(encoding="utf-8"))
    hs = sc["_hold_side_fields"]
    assert "sell_flags" in hs and "trim_to_weight_pct" in hs and "HOLD_REVIEW" in hs["_說明"]
    assert "sell_flags" not in sc and "trim_to_weight_pct" not in sc   # 選填：不進空白卡


# ---------------------------------------------------------------- build_dossier

def test_A3_M2A_13_buyback_amount_is_absolute():
    import test_fix_F04 as F4
    B = _mod("build_dossier")
    f = F4._dossier_facts()
    pos = B.buyback_price_data(copy.deepcopy(f), F4.ASOF)["buyback_amount_by_fy"]
    assert pos and all(v > 0 for v in pos.values())
    g = f["facts"]["us-gaap"]
    for t in B.BUYBACK_CF_TAGS + [v for _, v in B.BUYBACK_PAIRS]:
        for rows in (g.get(t) or {}).get("units", {}).values():
            for r in rows:
                r["val"] = -abs(r["val"])
    neg = B.buyback_price_data(f, F4.ASOF)
    assert neg["buyback_amount_by_fy"] == pos, (neg["buyback_amount_by_fy"], pos)
    assert "絕對值" in neg["buyback_amount_note"]


def test_A3_M2C_07_build_dossier_documents_rates_section_owner():
    doc = _mod("build_dossier").__doc__
    assert "skill 單跑不產生資料包 md 的利率段" in doc and "rates_section" in doc and "3 個月" in doc
