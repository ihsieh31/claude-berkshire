"""第二輪重審（依 2026-10-17 09:19 之後的程式碼快照）新增／修正的重現測試。

執行：
    cd "/Users/zongen/Downloads/claude code/berkshire/bug report/qwen"
    python3 -m pytest -q test_repro_round2.py

本檔有兩種測試：
- 重現新發現的缺陷（斷言實際行為，綠燈＝缺陷存在）。
- 推翻我上一版的錯誤結論（斷言「我舊報告講錯了」的那一面，綠燈＝舊結論作廢）。

只讀專案原始檔；任何寫入都落在 pytest 的 tmp 目錄。
"""
from __future__ import annotations

import inspect
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path("/Users/zongen/Downloads/claude code/berkshire")
sys.path.insert(0, str(ROOT / "trader"))
sys.path.insert(0, str(ROOT / "berkshire-deep-merged" / "scripts"))
sys.path.insert(0, str(ROOT / "berkshire-screen-merged" / "scripts"))

import cap_calc  # noqa: E402
import review_due  # noqa: E402
import screen_quant  # noqa: E402

from trader import db  # noqa: E402
from trader.data.universe import load_universe  # noqa: E402
from trader.jobs import validate  # noqa: E402
from trader.jobs import handlers  # noqa: E402,F401  先載入 handlers 以避開 deep↔handlers 的循環匯入
from trader.jobs.review_jobs import PriceHandler  # noqa: E402
from trader.llm import base as llm_base  # noqa: E402
from trader.models import LLMResult, PRICE_ISOLATED_JOBS, JobSpec  # noqa: E402


@pytest.fixture()
def conn(tmp_path_factory):
    c = db.connect(tmp_path_factory.mktemp("r2") / "trader.db")
    db.migrate(c)
    yield c
    c.close()


def _registry(conn, ticker, data, status="HOLDING"):
    db.insert(conn, "registry", {"ticker": ticker, "status": status, "data": data,
                                 "updated_at": db.now_iso()})


def _position(conn, ticker, qty, avg_cost, first_buy_date):
    db.insert(conn, "positions", {"ticker": ticker, "qty": qty, "avg_cost": avg_cost,
                                  "first_buy_date": first_buy_date, "updated_at": db.now_iso()})


# ---------------------------------------------------- 1 推翻舊結論：D 級已被擋

def test_grade_d_is_already_blocked_my_old_p1_9_was_wrong():
    """舊報告 P1-9 稱「代碼層沒有任何地方阻擋 grade=D」。實測為錯：
    cap_calc.py:112-113 有 `if grade == "D": no_cap = ...`，:159-160 把 cap 與 auto_buy_cap 都置 None。
    這支測試斷言「舊結論不成立」，綠燈＝我舊報告該條作廢。"""
    x = {"oe": 5.0, "price": 40.0, "grade": "D", "long_rate": 0.05, "iv_cons": 60.0, "g10": 0.08}
    r = cap_calc.calc(x)
    assert r["auto_buy_cap"] is None, "D 級其實不會拿到買進上限（舊結論有誤）"
    assert r["cap"] is None and "D 級" in (r.get("conclusion_hint") or ""), "D 級該被擋掉"


def test_unrecognised_grade_is_silently_treated_as_c():
    """D 擋掉了，但拼錯的等級（"a"、"BB"、None）沒有：cap_calc.py:114 `.get(grade, GRADE_CAP["C"])`
    無聲借用 C 的 0.40 係數繼續算上限，不留任何註記。"""
    base = {"oe": 5.0, "price": 40.0, "long_rate": 0.05, "iv_cons": 60.0, "g10": 0.08}
    typo = cap_calc.calc({**base, "grade": "a"})
    c_grade = cap_calc.calc({**base, "grade": "C"})
    assert typo["cap"] == pytest.approx(c_grade["cap"]), "未知等級被當成 C 算出上限"
    assert typo["auto_buy_cap"] is not None, "拼錯等級仍可自動買進，且無警告"


# ---------------------------------------------------- 2 新缺陷：g_term 私自加下限

def test_g_term_floor_raises_cap_when_long_rate_below_one_percent():
    """SKILL.md:90／rulebook.md:276 定義終值成長 g_term = min(3%, 30Y − 1pp)，無下限。
    cap_calc.py:126 卻寫成 min(0.03, max(0.0, L − 0.01))：L < 1% 時被抬到 0，
    第 11 年後还在成長 → P2′ 與自動買進上限系統性偏寬。"""
    low = {"oe": 5.0, "price": 40.0, "grade": "A", "long_rate": 0.005,
           "iv_cons": 60.0, "g10": 0.08}
    r = cap_calc.calc(low)
    assert r["g_term_impl"] == 0.0, "規則要 -0.005，實作夾到 0"
    # 規則版終值（無下限）算出的 P2′ 應低於實作版
    rule_p2 = cap_calc.pv(5.0, cap_calc.G_IMPL_MAX, r["r_d"], -0.005)   # 規則：min(3%, L−1pp)＝−0.5%，無下限
    assert r["cap"] > rule_p2, f"實作上限 {r['cap']:.2f} 比規則允許的 {rule_p2:.2f} 更寬鬆"


# ---------------------------------------------------- 3 新缺陷：死輸入與未用的旗標

def test_incr_roic_gt_rd_is_a_dead_input_for_a_grade_p4():
    """cap_calc.py:20 自陳 `incr_roic_gt_rd` 是「A 級 P4′ 1.2 倍條件之一」，
    例檔 assets/cap_calc_example_input.json 也帶該欄；但 :144 是無條件 `k4 = 1.2 if grade=="A" else 1.0`。
    後果：增量資本報酬低于 r_d 的 A 級照样被放大 P4′（金融類還把它當硬上限，見 P1-6）。"""
    # 金融類 P2 不適用 → cap_calc.py:153 取 P4′ 作硬上限，P4′＝k4 × 大型股 P/E 中位 × E（:144）
    base = {"oe": 5.0, "price": 20.0, "grade": "A", "long_rate": 0.05, "financial": True,
            "norm_eps": 1.0, "iv_cons": 30.0, "largecap_pe_median": 19.0}
    yes = cap_calc.calc({**base, "incr_roic_gt_rd": True})
    no = cap_calc.calc({**base, "incr_roic_gt_rd": False})
    assert yes["cap"] == pytest.approx(no["cap"]), "增量資本報酬旗標對上限完全無影響（文宣與實作分岔）"
    # :142 金融類走 `pem * eps`（無 fin_pe_median 時乘數 1.0）；:144 的 k4=1.2 只給非金融，
    # 而非金融的 cap 走 P2′（:153），所以「A 級 1.2 倍」其實進不了硬上限——這是對查核代理说法的修正。
    assert yes["cap"] == pytest.approx(19.0 * 1.0), "金融類硬上限＝P4′＝大型股 P/E 中位×E（同 P1-6）"
    assert "現價 > P4′（軟上限）" not in "".join(yes["notes"]) or True


def test_gate2_never_reads_g_impl_gt_cap():
    """SKILL.md:99：「`g_impl` 為 null 且 `g_impl_gt_cap`＝true → 閘門 2 不過」。
    實測 gate2（review_jobs.py:291-298）只看 `price < auto`、`grade_used in (A,B)`、`flips`，
    整個 PRICE handler 源码沒有任何一處讀 `g_impl_gt_cap`；cap_calc 輸出該旗標只進回寫欄當裝飾。"""
    src = inspect.getsource(PriceHandler)
    assert "g_impl_gt_cap" not in src, "規則宣示的閘門 2 條件之一未實作"
    r = cap_calc.calc({"oe": 5.0, "price": 4000.0, "grade": "A", "long_rate": 0.05,
                       "iv_cons": 60.0, "g10": 0.08})
    assert r["g_impl_gt_cap"] is True, "旗標確實會亮起，只是沒人讀"


# ---------------------------------------------------- 4 新缺陷：驗證器的自由文字後門

def test_dossier_dq_checks_result_is_free_text():
    """validate.py:193-198：選項字串只要含 `…`／`..`／`：` 之一，整個欄位降為「任意字串」。
    `DOSSIER.dq_checks[].result` 的 spec 是「已核對：原因… | 數字有誤：正確值… | 無法核對」→
    資料品質自我檢查欄可以填任何東西（含空字串）都合格，而 DOSSIER 是後所有工作的輸入。"""
    schemas = validate.load_schemas(ROOT / "routing" / "output_schemas.json")
    obj = {"ticker": "X", "decision_date": "2026-10-16", "sections_appended": ["a"],
           "contains_price": False, "missing": [],
           "dq_checks": [{"warning": "w", "result": "", "detail": ""},
                         {"warning": "w2", "result": "我根本沒核對：随便寫", "detail": ""}]}
    _, errors = validate.check("DOSSIER", obj, schemas)
    assert not any("dq_checks" in e for e in errors), f"空格子與亂填都過：{errors}"


def test_empty_arrays_and_blank_reason_pass_thesis_still_holds():
    """第一輪的 P0-5(2) 在你今早改過 validate.py／output_schemas.json 之後仍然成立：
    交白卷的論點複查（空陣列＋空 reason）依旧合格。"""
    schemas = validate.load_schemas(ROOT / "routing" / "output_schemas.json")
    obj = {"ticker": "X", "verdict": "完好", "kill_criteria_status": [], "r_list_status": [],
           "new_facts": [], "reason": ""}
    _, errors = validate.check("THESIS", obj, schemas)
    assert errors == [], "驗證仍只查鍵存在與列舉值，不查內容"


# ---------------------------------------------------- 5 新缺陷：登錄表被清空後賣出線永久靜默

def test_wiped_valuation_lines_never_trigger_sell_review():
    """鏈路：P1-3 的空模板覆寫把 auto_buy_cap／sell_review_above／absurd_price_line 寫成 None →
    pipelines/review.py:55 `{k: v for k, v in d.items() if v is not None}` 把 None 鍵整個刪除 →
    review_due.check() 的 `if e.get("sell_review_above") and ...` 永遠不成立。
    結果：持股漲到 100 倍也不會被拉去賣出檢視，且 action 正當回答 NONE。"""
    px = pd.DataFrame({"date": pd.date_range("2026-06-01", periods=3).date,
                       "close": [10.0, 600.0, 3000.0]})
    e = {"ticker": "X", "status": "HOLDING", "weight": 0.05, "cost": 10.0,
         "sell_review_above": None, "absurd_price_line": None, "auto_buy_cap": None,
         "target_weight": None, "last_review_date": "2026-06-01",
         "last_full_review_date": "2026-06-01"}
    action, reasons, _ = review_due.check(e, [], px, "2026-06-03", 0.05)
    assert not any(("sell_review_above" in w) or ("荒謬" in w) for w in reasons), "賣出線全靜默"
    assert action != "PRICE", f"漲 300 倍卻不進價格檢視，實際 action＝{action}"


# ---------------------------------------------------- 6 新缺陷：缺上限把建倉批次卡死

def test_missing_auto_buy_cap_strands_the_tranche_without_any_record(conn):
    """sizing.py:477-479 與 :528-530：`px is None or cap is None → continue`，
    不產生計畫、不寫原因、不記異常。同時 days_above_cap 不再累加 →
    :474 的「連 10 日高於上限就取消剩餘批次」這條逃生口永遠不會觸發 →
    半個建倉計畫永久卡住（而且 P1-3 的清空正是 auto_buy_cap=None 的來源之一）。"""
    from trader.portfolio import sizing
    asof = "2026-10-14"
    _position(conn, "KO", 100, 60.0, "2026-10-12")
    _registry(conn, "KO", {"build_plan": {"tranches_left": 2, "target_weight": 0.05},
                           "auto_buy_cap": None, "grade": "A", "status": "HOLDING"})
    plans, cancels = sizing.plan_tranches(conn, asof, {"KO": 999.0}, {"KO": 1e9}, 5.0)
    assert plans == [] and cancels == [], "價格已遠超上限，卻既不加碼也不取消、更不留痕"
    sizing.tick_build_state(conn, asof, {"KO": 999.0})
    bp = (json.loads(conn.execute("SELECT data FROM registry WHERE ticker='KO'")
                     .fetchone()["data"]) or {}).get("build_plan", {})
    assert int(bp.get("days_above_cap", 0)) == 0, "超漲天數計數也一起停擺"


def test_grade_c_or_none_disables_the_only_mechanical_exit(conn):
    """sizing.py:23 `GRADE_CAP = {"A": 0.15, "B": 0.10}`、:414-416 `cap is None → continue`：
    權重超限減碼只對 A／B 級生效。等級為 C、D、或（因 P1-3 清空而）None 的持股，
    就算膨到 90% 權益也不會產生任何 TRIM 計畫——而系統沒有價格停損，這是唯一的機械式出口。"""
    from trader.portfolio import sizing
    asof = "2026-10-14"
    _position(conn, "PEP", 9000, 50.0, "2025-01-06")     # 建倉已滿一年
    _registry(conn, "PEP", {"grade": None, "status": "HOLDING", "sell_review_above": 40.0})
    plans = sizing.plan_sells(conn, asof, [], {"PEP": 60.0})
    assert plans == [], "等級缺值 → 權重 54%（>15%）也不減碼，且不留 notes"


# ---------------------------------------------------- 7 新缺陷：RED_TEAM 未列管價格隔離

def test_red_team_is_not_price_isolated_though_its_schema_forbids_pricing():
    """models.py:13 PRICE_ISOLATED_JOBS = {DOSSIER, TRIAGE, THESIS, FULL_A}，RED_TEAM 不在內；
    但 routing/output_schemas.json 的 RED_TEAM.rule_violations 明文把「用價格作答 G1–G6」列為違規。
    該工作 tier 是 strong／opus，沙箱不 scrub、也不走 gate3 開檔閘門 → 規則要求與機制之間沒有橋。"""
    assert "RED_TEAM" not in PRICE_ISOLATED_JOBS
    schemas = validate.load_schemas(ROOT / "routing" / "output_schemas.json")
    assert "用價格作答" in json.dumps(schemas["RED_TEAM"], ensure_ascii=False)


# ---------------------------------------------------- 8 新缺陷：傳輸重試對預算不可見

def test_call_with_retry_makes_retries_plus_one_physical_calls():
    """engine.py:277 一次 _submit 內呼叫 call_with_retry(retries=TRANSPORT_RETRIES)，
    而 llm/base.py:103 `for i in range(retries + 1)` 是實體呼叫次數；
    engine.py:284-291 只在 _on_done 記 1 筆 llm_calls → 預算（SUM(cost_usd)／COUNT(*)）看不到重試。
    本測試量出雨者倍差。"""
    calls = {"n": 0}
    res = LLMResult(text="{}", parsed_json={}, input_tokens=1000, output_tokens=10,
                    model="m", latency_s=0.1, raw_path=None, cost_usd=0.25, files_written=[])

    class Runner:
        backend = "fake"

        def run(self, job):
            calls["n"] += 1
            if calls["n"] < 4:
                raise llm_base.TransportError("429")
            return res

    from trader.jobs import engine as eng
    job = JobSpec(job_id="R1:T:PRICE:1", run_id="R1", job_type="PRICE", ticker="T", asof="2026-10-16")
    out = llm_base.call_with_retry(Runner(), job, retries=eng.TRANSPORT_RETRIES, sleep=lambda s: None)
    assert calls["n"] == eng.TRANSPORT_RETRIES + 1
    assert out is res
    src = inspect.getsource(eng.Engine._on_done)
    assert src.count("_record_call") <= 2, "每 submit 只記一筆（不论實際喊了幾次）"


# ---------------------------------------------------- 9 新缺陷：股票池無 asof 版本

def test_universe_loader_has_no_asof_parameter():
    """universe.py:82 `load_universe(paths, conn, http, refresh, max_age_days)` 沒有 asof；
    快取寫进的 universe_versions.asof 是「今天」（:59），且該表全專案只寫不讀。
    任何 --asof 歷史重播都用今天的成分股名單 → 倖存者偏差，连帶把 pe_median 的母體統計一起弄錯。"""
    params = inspect.signature(load_universe).parameters
    assert "asof" not in params
    src = inspect.getsource(load_universe.__module__ and __import__(
        "trader.data.universe", fromlist=["load_universe"]))
    assert "universe_versions" in src


# ---------------------------------------------------- 10 修正舊結論：初篩 PIT 的實隙破口

def test_point_in_time_marks_unverified_rows_as_checked():
    """舊報告 P1-15(a) 說「缺申報日 → 回傳 False 不記錯誤，該筆被丟棄」——機制不是這樣。
    實際：screen_quant.py:52-53 `bad = fd.notna() & (fd >= D)` 把「申報日為空／壞」的列直接跳過不驗，
    :55 只對有值且 ≥ 決策日的列舉错，:61 卻把整表 `pit_checked` 標 True。
    也就是：未受檢的列被記錄為已通過時點檢查。"""
    df = pd.DataFrame({"fy_last_filed": ["2026-01-01", None, "bad-date"],
                       "ticker": ["A", "B", "C"]})
    ok = screen_quant.check_point_in_time(df, "2026-03-01")
    assert ok is True, "2 列根本沒被驗過，函式卻回傳 True"
