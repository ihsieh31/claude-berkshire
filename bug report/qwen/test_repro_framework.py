"""框架層（trader/）重現測試：只讀專案，所有寫入都落在本目錄的 _scratch。

執行：
    cd "/Users/zongen/Downloads/claude code/berkshire/bug report/qwen"
    python3 -m pytest -q test_repro_framework.py

每支測試都針對一個「程式碼與規則不一致」或「fail-open」的具體位置；
斷言的是實際行為，不是預期行為——綠燈即代表缺陷存在。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path("/Users/zongen/Downloads/claude code/berkshire")
sys.path.insert(0, str(ROOT / "trader"))

from trader import db  # noqa: E402
from trader.data.alpaca import _normalize_action  # noqa: E402
from trader.data.pe_median import freeze_path, get_frozen  # noqa: E402
from trader.jobs.approvals import add_block_reason  # noqa: E402
from trader.jobs.handlers import Handler, HandlerCtx  # noqa: E402
from trader.ledger.corp_actions import detect  # noqa: E402
from trader.models import JobSpec  # noqa: E402
from trader.paths import PKG_ROOT, Paths  # noqa: E402
from trader.portfolio.sizing import add_blocked  # noqa: E402


@pytest.fixture()
def conn(tmp_path_factory):
    d = tmp_path_factory.mktemp("scratch") / "trader.db"
    c = db.connect(d)
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture()
def paths(tmp_path_factory):
    st = tmp_path_factory.mktemp("scratch2")
    return Paths(root=PKG_ROOT, state=st)


# ---------------------------------------------------------------- 1 基準股利欄位名

def test_benchmark_dividend_reads_a_key_the_normalizer_never_writes():
    """daily.py:294 以 a["cash"] 累加基準股利；_normalize_action 寫的是 "cash_per_share"。

    結果：nav_daily.benchmark_close 的股利成分恆為 0，report/metrics 的 SPY 為純價格指數，
    儀表板的「SPY 同期（含股利）」與超額報酬因此一律偏向好看。
    """
    action = _normalize_action("cash_dividends", {
        "symbol": "SPY", "ex_date": "2026-03-20", "rate": "1.50", "id": 1,
    })
    assert action["type"] == "cash_dividend"
    assert action["cash_per_share"] == 1.5
    assert "cash" not in action, "正規化器若改了欄位名，本測試需同步更新（此為缺陷所在）"

    # 逐字重現 daily.py:294 的表示式
    bench_div = sum(a.get("cash") or 0.0 for a in [action]
                    if a.get("type") == "cash_dividend"
                    and "2026-03-19" < str(a.get("ex_date") or "") <= "2026-03-20")
    correct = sum(a.get("cash_per_share") or 0.0 for a in [action]
                  if a.get("type") == "cash_dividend")
    assert bench_div == 0.0
    assert correct == 1.5, "同一筆資料用對欄位名就有值——純鍵名錯誤"


# ---------------------------------------------------------------- 2 凍結 P/E 的前視

def test_frozen_pe_median_file_is_returned_even_when_it_looks_ahead(paths):
    """pe_median.get_frozen 只看「月份檔存在」，不比對檔內 asof ≤ 查詢 asof。

    重播／回測（--asof 往回跑）時，同月稍晚算出的凍結值會被當成決策當時的資料，
    P4′（依賴全市場 P/E 中位數）與 W-PE 分區都吃進未來資訊。
    """
    p = freeze_path(paths, "2026-10")
    p.parent.mkdir(parents=True, exist_ok=True)
    future = {"asof": "2026-10-20", "pe_norm_median": 21.4, "pe_ttm_median": 24.0,
              "fin_pe_norm_median": 12.0, "n_norm": 300, "n_ttm": 280, "n_fin": 60}
    p.write_text(json.dumps(future), encoding="utf-8")

    got = get_frozen(paths, "2026-10-03")  # 決策日在資料產生日之前 17 天
    assert got == future, "get_frozen 未做任何點對點檢查，直接交出未來值"
    assert got["asof"] > "2026-10-03"


def test_frozen_pe_median_prev_month_also_unchecked(paths):
    """退回上個月時同樣不比對 asof：整個 9 月的任意時點凍結值都能用於 10 月初之前。"""
    p = freeze_path(paths, "2026-09")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"asof": "2026-09-30", "pe_norm_median": 20.0}), encoding="utf-8")
    got = get_frozen(paths, "2026-09-01")
    assert got["asof"] == "2026-09-30", "月內任何一天都取到月末值（前視一個月內的部分區間）"


# ---------------------------------------------------------------- 3 THESIS 驗證失敗＝fail-open

def test_invalid_thesis_decision_is_invisible_to_add_blocked(conn, paths, tmp_path):
    """THESIS 輸出不合格時，Handler.on_invalid 寫入的決策沒有 review_type；

    add_blocked 只認有 review_type 的列，於是回退到更早的「完好」結論 →
    論點複查失敗（最該停止加碼的時刻）反而放行加碼。HOLD_REVIEW/RED_TEAM 有覆寫成保守值，
    THESIS/PRICE 沒有——同一框架內的 fallback 不一致。
    """
    ticker = "TEST1"
    db.insert(conn, "registry", {
        "ticker": ticker, "status": "HOLDING", "first_buy_date": "2024-01-02",
        "data": {"ticker": ticker, "status": "HOLDING", "target_weight": 0.10},
        "updated_at": db.now_iso(),
    })
    # 1) 一次完好的論點複查
    db.insert(conn, "decisions", {
        "decision_id": "D1", "ticker": ticker, "decision_date": "2026-06-01",
        "signal": "HOLD", "reason_code": None,
        "data": {"review_type": "THESIS", "thesis_check": "完好"},
        "created_at": "2026-06-01T00:00:00Z",
    })
    ctx = HandlerCtx(paths=paths, schemas={}, work_dir=tmp_path, conn=conn)
    job = JobSpec(job_id="R1:TEST1:THESIS:1", run_id="R1", job_type="THESIS",
                  ticker=ticker, asof="2026-10-15")
    # 2) 用框架自己的 on_invalid 產生「輸出不合格」的決策列
    Handler().on_invalid(job, ["缺少 thesis_check"], ctx)
    row = conn.execute("SELECT data FROM decisions WHERE decision_id = ?", (job.job_id,)).fetchone()
    log = json.loads(row["data"])
    assert "review_type" not in log, log
    assert log["job_type"] == "THESIS"

    assert add_blocked(conn, ticker, "2026-10-20") is None, \
        "缺陷：論點複查壞掉＝視而不見，加碼仍然放行"


# ---------------------------------------------------------------- 4 拒絕同意後仍可加碼

def test_rejected_approval_does_not_block_adding(conn):
    """approvals.add_block_reason 只認 pending/deferred/approved；

    使用者拒絕強模型分析 → 該檔從未取得上限，卻落回「無阻擋理由」。
    加碼與否轉由（可能過期的）舊決策決定，同意閘門的反向保護不存在。
    """
    ticker, run_id = "TEST2", "R2"
    db.insert(conn, "approvals", {
        "ticker": ticker, "run_id": run_id, "asof": "2026-10-15",
        "job_id": f"{run_id}:{ticker}:FULL_A:1", "job_type": "FULL_A", "tier": "strong",
        "holding": 1, "reason": "持股年複查", "status": "rejected",
        "created_at": db.now_iso(), "decided_at": db.now_iso(), "decided_via": "cli",
    })
    assert add_block_reason(conn, ticker) is None
    # 同檔若無其他決策，add_blocked 亦回 None（=可加碼）
    assert add_blocked(conn, ticker, "2026-10-16") is None


# ---------------------------------------------------------------- 5 拒絕＝抹掉登錄表

def test_write_stop_result_wipes_a_watch_entrys_valuation(conn, paths):
    """非 HOLDING（例如等待買進的 WATCH）走空模板覆寫整列：

    auto_buy_cap／sell_review_above／grade／thesis_file／kill_criteria 全變 None，
    同時把 last_full_review_date 設為 asof。之後價格跌進原上限、或利率跳升，
    review_due 與 size 側都查無可比值 → 靜默不再觸發，且 12 個月內不會重排完整分析。
    """
    from trader.analysis.writeback import write_stop_result
    ticker = "TEST3"
    entry = {"ticker": ticker, "status": "WATCH", "grade": "B",
             "auto_buy_cap": 120.0, "sell_review_above": 150.0, "absurd_price_line": 400.0,
             "review_price": 118.0, "t30_at_valuation": 0.042,
             "first_buy_date": None, "last_review_date": "2026-08-01",
             "last_full_review_date": "2026-08-01", "thesis_file": "theses/TEST3.md",
             "kill_criteria": ["市占連續兩季下滑"]}
    db.insert(conn, "registry", {"ticker": ticker, "status": "WATCH",
                                 "data": entry, "updated_at": db.now_iso()})

    write_stop_result(conn, paths, ticker, "2026-10-15", "R3",
                      {"ticker": ticker, "decision_date": "2026-10-15", "stopped": True,
                       "stopped_at_gate": "STEP0", "reason_code": "MISSING_DATA"},
                      ["使用者拒絕以強模型分析"])

    after = json.loads(conn.execute("SELECT data FROM registry WHERE ticker = ?",
                                   (ticker,)).fetchone()["data"])
    for k in ("auto_buy_cap", "sell_review_above", "grade", "thesis_file",
              "kill_criteria", "t30_at_valuation", "absurd_price_line"):
        assert after.get(k) is None, f"{k} 應被保留，實得 {after.get(k)!r}"
    assert after["last_full_review_date"] == "2026-10-15"


# ---------------------------------------------------------------- 6 空證據的論點複查


def test_thesis_check_passes_validation_with_zero_evidence(conn):
    """validate 只查「鍵存在」：kill_criteria_status／r_list_status／new_facts 給空陣列、
    reason 給空字串，照樣合格；寫回後 thesis_check＝完好 → add_blocked 放行。

    「持有期間唯一主動監控論點」的機制，最低成本合格解＝什麼都不查。
    搭配 ThesisHandler.pre_script 在 SEC 客戶端缺席或查不到 cik 時直接 return（不記註），
    「沒有新資料」與「查過沒有變化」在決策日誌裡完全同形。
    """
    from trader.jobs import validate
    schemas = validate.load_schemas(ROOT / "routing" / "output_schemas.json")
    empty = {"ticker": "TEST5", "verdict": "完好", "kill_criteria_status": [],
             "r_list_status": [], "new_facts": [], "reason": ""}
    norm, errors = validate.check("THESIS", empty, schemas)
    assert errors == [], f"空證據本應被擋下：{errors}"
    assert norm["verdict"] == "完好"

    ticker = "TEST5"
    db.insert(conn, "registry", {
        "ticker": ticker, "status": "HOLDING", "first_buy_date": "2024-01-02",
        "data": {"ticker": ticker, "status": "HOLDING",
                 "kill_criteria": ["市占連續兩季下滑"], "auto_buy_cap": 100.0},
        "updated_at": db.now_iso(),
    })
    db.insert(conn, "decisions", {
        "decision_id": "D5", "ticker": ticker, "decision_date": "2026-10-15",
        "signal": "HOLD", "reason_code": None,
        "data": {"review_type": "THESIS", "thesis_check": norm["verdict"],
                 "failure_conditions_hit": [], "reason_one_liner": norm["reason"]},
        "created_at": db.now_iso(),
    })
    assert add_blocked(conn, ticker, "2026-10-20") is None
    # 登錄表記了失效條件，複查卻可交白卷——兩者沒有任何比對
    assert json.loads(conn.execute("SELECT data FROM registry WHERE ticker=?", (ticker,))
                      .fetchone()["data"])["kill_criteria"] == ["市占連續兩季下滑"]


# ---------------------------------------------------------------- 7 同生效日的第二種公司行動

def test_detect_drops_a_second_corp_action_on_the_same_effective_date(conn):
    """detect 的去重鍵是 (ticker, effective_date)，但 DB 的 UNIQUE 是 (ticker, action_type, effective_date)。

    同日「拆股 + 現金股利」（或任何兩種行動）時，第一筆入帳後第二筆永不再被偵測：
    股利不會進現金帳，跳空也不會被解釋。
    """
    ticker, eff = "TEST4", "2026-03-02"
    # 資料庫允許同代號同生效日的不同類型（唯一鍵含 action_type）
    db.insert(conn, "corporate_actions", {
        "ticker": ticker, "action_type": "拆股", "effective_date": eff,
        "ratio_new_per_old": 2.0, "cash_per_share": None, "status": "applied",
        "source": "alpaca", "data": {}, "created_at": db.now_iso(),
    })
    dividend = _normalize_action("cash_dividends", {
        "symbol": ticker, "ex_date": eff, "rate": "0.25", "id": 77,
    })
    events = detect(conn, date="2026-03-02", prev_date="2026-02-27",
                    tickers=[ticker], alpaca_actions=[dividend],
                    filings_8k={}, bars_prev={ticker: {"close": 50.0}},
                    bars_today={ticker: {"close": 24.9}})
    assert events == [], "缺陷：同日第二種公司行動被 (ticker, date) 去重鍵無聲吃掉"

    # 對照組：錯開一天就偵測得到，證明原因只有日期
    events2 = detect(conn, date="2026-03-04", prev_date="2026-02-27",
                     tickers=[ticker], alpaca_actions=[
                         _normalize_action("cash_dividends",
                                           {"symbol": ticker, "ex_date": "2026-03-04",
                                            "rate": "0.25", "id": 78})],
                     filings_8k={}, bars_prev={ticker: {"close": 50.0}},
                     bars_today={ticker: {"close": 24.9}})
    assert len(events2) == 1


# ---------------------------------------------------------------- 8 賣出決策樹的 Q3b／Q3c 無法表示

def test_hold_review_schema_cannot_record_the_branches_the_prompt_demands():
    """HOLD_REVIEW.path[].q 的列舉只有 Q0｜Q1｜Q1b｜Q2｜Q3｜Q4｜Q5｜Q6：

    - prompts/HOLD_REVIEW.md:24 與 rule_index/04_hold_sell.md:231 要求「TRIM 只用於 Q3c 的頂級複利型減碼」；
    - rulebook.md:370 的 3b「只有 V、價格 ≥ IV_base → 全清」同樣不在列舉內。
    模型若如實記錄路徑 → 驗證不合格 → HoldReviewHandler.on_invalid 走保守 HOLD＋停止加碼，
    等於「老實寫出賣出路徑反而賣不掉」；反之把 Q3c 寫成 Q3 才能通過，稽核鏈就此斷掉。
    同場加映：path 給空陣列＋SELL_ALL 照樣合格（validate 只遞迴既有元素）。
    """
    from trader.jobs import validate
    schemas = validate.load_schemas(ROOT / "routing" / "output_schemas.json")

    def card(q, action="TRIM", path=None):
        return {"ticker": "TEST6", "decision_date": "2026-10-15",
                "path": path if path is not None else [{"q": q, "answer": "是", "evidence": "P-46"}],
                "action": action, "sell_category": "估值", "trim_fraction": 0.25,
                "stop_adding": False, "observe_until": None,
                "falsified_assumption": None, "reason": "頂級複利型估值減碼"}

    _, errs_c = validate.check("HOLD_REVIEW", card("Q3c"), schemas)
    assert any("Q3c" in e for e in errs_c), f"Q3c 竟被接受：{errs_c}"
    _, errs_b = validate.check("HOLD_REVIEW", card("Q3b"), schemas)
    assert any("Q3b" in e for e in errs_b), f"Q3b 竟被接受：{errs_b}"
    _, errs_ok = validate.check("HOLD_REVIEW", card("Q3"), schemas)
    assert errs_ok == [], "寫成 Q3（不忠實）才能通過——缺陷的對照組"

    blank = card("Q3", action="SELL_ALL", path=[])
    _, errs_blank = validate.check("HOLD_REVIEW", blank, schemas)
    assert errs_blank == [], f"無任何決策樹痕跡的 SELL_ALL 竟合格：{errs_blank}"


def test_full_a_sandbox_schema_loses_the_moat_test_key_it_must_output():
    """FULL_A 為價格隔離工作，stage_inputs 會 scrub 所有 .json 的「價格鍵」；

    連帶把沙箱內 `pkg/routing/output_schemas.json` 的 `moat_tests.price_cut` 鍵刪除
    （鍵名含 price）。claude_code 後端的提示只列檔名，模型必須自己讀schema 才能湊齊欄位——
    它讀到的是被自己框架刮掉的版本，輸出必然缺 `price_cut` → 驗證失敗 → 重試／退回。
    同一機制也把 DOSSIER 的 `contains_price` 自報欄位從 schema 副本中抹除。
    """
    from trader.jobs import validate
    from trader.jobs.sandbox import scrub_prices
    schemas = validate.load_schemas(ROOT / "routing" / "output_schemas.json")
    inside = scrub_prices(schemas["FULL_A"])
    assert "price_cut" in schemas["FULL_A"]["moat_tests"]
    assert "price_cut" not in inside["moat_tests"], "隔離沙箱看到的 schema 少了必填鍵"

    # 模型照沙箱內的 schema 作答 → 框架用完整版判定 → 一定不合格
    answer = {"ticker": "TEST7", "decision_date": "2026-10-15", "stopped": False,
              "stopped_at_gate": None, "reason_code": None, "asset_class": "非金融",
              "starting_grade": "B", "downgrades": [],
              "moat_tests": {k: "強" for k in inside["moat_tests"]},
              "red_flags": {"exclusion": 0, "warning": 0}, "capital_allocation_net": 0,
              "tail_loss_after_tax_pct": None,
              "normalized_inputs": {"oe_per_share": None, "g10": None, "g5": None,
                                    "excluded_shock_years": [], "roe_std_ex_shock_pp": None},
              "thesis_draft": {"one_liner": "", "key_assumptions": [], "kill_criteria": []},
              "missing": [], "inferred_thresholds_used": [],
              "files": ["report_A.md", "gate3_answers.json"]}
    _, errs = validate.check("FULL_A", answer, schemas)
    assert any("price_cut" in e for e in errs), f"預期因缺 price_cut 而不合格：{errs}"
