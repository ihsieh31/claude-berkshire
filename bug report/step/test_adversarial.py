# -*- coding: utf-8 -*-
"""對抗性測試（Step-5 審查用）：針對 berkshire 專案的可疑 bug 逐項驗證。

執行方式（不改動專案任何檔案）：
    TMPDIR=<任意可寫目錄> /Users/zongen/.workbuddy-ai/binaries/python/envs/default/bin/python -m pytest \
        "/Users/zongen/Downloads/claude code/berkshire/bug report/step/test_adversarial.py" -v

每個 test 對應最終報告中的一項發現；失敗（紅燈）代表該疑點被重現。
"""
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT = Path("/Users/zongen/Downloads/claude code/berkshire")
TRADER = PROJECT / "trader"
sys.path.insert(0, str(TRADER))

# 避免循環匯入：handlers 必須先於 deep／review_jobs 匯入（專案內部既定順序）
import trader.jobs.handlers  # noqa: E402,F401

os.environ.setdefault("TMPDIR", str(Path(__file__).parent / "tmp"))


# =====================================================================
# 發現 1（高）：daily.py 的 SPY 股利用了錯的鍵名（cash vs cash_per_share）
#   → benchmark_dividend 永遠為 0，SPY 比較永遠是價格報酬（不含股利），
#     策略相對基準被系統性高估約 1 個百分點／年。
# =====================================================================
class TestSpyBenchmarkDividendKeyMismatch:
    def test_bench_div_is_always_zero_with_realistic_alpaca_actions(self):
        """重現 daily.py 第 294–296 行的實際邏輯。

        Alpaca 公司行動經 alpaca._normalize_action 正規化後的鍵是
        'cash_per_share'；daily.py 卻用 a.get('cash') 取值 → 恆為 0。
        """
        # 與 trader/data/alpaca.py::_normalize_action 輸出一致的 dict
        alpaca_actions = [
            {"ticker": "SPY", "type": "cash_dividend", "ex_date": "2026-09-18",
             "process_date": "2026-09-22", "record_date": "2026-09-18",
             "payable_date": "2026-10-15", "ratio_new_per_old": None,
             "cash_per_share": 1.73, "new_ticker": None, "raw": {}},
            {"ticker": "AAPL", "type": "cash_dividend", "ex_date": "2026-08-10",
             "payable_date": "2026-08-14", "cash_per_share": 0.26, "raw": {}},
        ]
        prev, date = "2026-09-17", "2026-09-18"
        bench = "SPY"

        # ---- daily.py 的實際寫法（bug）----
        bench_div_buggy = sum(
            a.get("cash") or 0.0 for a in alpaca_actions
            if a.get("type") == "cash_dividend"
            and (a.get("ticker") or "").upper().replace("-", ".") == bench
            and prev < str(a.get("ex_date") or "") <= date
        )
        # ---- 正確寫法 ----
        bench_div_correct = sum(
            a.get("cash_per_share") or 0.0 for a in alpaca_actions
            if a.get("type") == "cash_dividend"
            and (a.get("ticker") or "").upper().replace("-", ".") == bench
            and prev < str(a.get("ex_date") or "") <= date
        )

        assert bench_div_buggy == 0.0, "現況：SPY 每股股利永遠取不到 → benchmark_dividend 恆為 0"
        assert bench_div_correct == pytest.approx(1.73)

    def test_normalize_action_key_names(self):
        """確認正規化函式的輸出鍵名確實是 cash_per_share（不是 cash）。"""
        from trader.data.alpaca import _normalize_action
        a = _normalize_action("cash_dividends", {"symbol": "spy", "ex_date": "2026-09-18",
                                                 "rate": 1.73, "payable_date": "2026-10-15"})
        assert "cash_per_share" in a
        assert "cash" not in a  # daily.py 取的鍵在正規化輸出裡根本不存在


# =====================================================================
# 發現 2（高）：FULL_B 買進路徑沒有程式化執行「閘門 2（現價 < 自動買進上限）」
#   cap_signal_by_gates 只檢查 gate3 與初篩刪除；價格 < 上限、等級 A/B、
#   g_impl 可解、結論測試未翻轉全部沒有框架把關，只靠模型自述。
#   對照：PRICE 快速通道有完整的 gate2 程式檢查（含 grade 與 flips）。
# =====================================================================
class TestFullBGate2NotEnforced:
    def test_buy_survives_when_price_above_cap(self):
        from trader.analysis.checks import cap_signal_by_gates

        # 價格 120、自動買進上限 50（價遠高於上限）＋ C 級（不可買）
        # ＋ g_impl 超出可解範圍；gate3 通過、初篩未刪 → 現況仍回 BUY
        signal, why = cap_signal_by_gates("BUY", {"gate3_pass": True}, "PASS")
        assert signal == "BUY"
        assert why == []

        # 對照組：gate3 不過 → 降 WATCH（這條有把關）
        signal2, why2 = cap_signal_by_gates("BUY", {"gate3_pass": False}, "PASS")
        assert signal2 == "WATCH"

    def test_order_time_check_blocks_the_trade(self, tmp_path):
        """重要緩衝（H2 的真實嚴重度）：即使決策日誌記了 price>cap 的 BUY，
        plan_buys 在下單時會獨立複檢 quote×(1+滑價) < auto_buy_cap → 拒單。
        即：bug 的後果是「記了一筆不會成交的 BUY」，不是「錯誤成交」。"""
        from trader.portfolio import sizing as Z
        from trader.ledger import book
        from trader import db as tdb

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        tdb.migrate(conn)
        book.ensure_initial_deposit(conn, 100000.0, "2026-01-02")
        tdb.insert(conn, "signals", {
            "decision_id": "X-20261015-1", "ticker": "X", "signal": "BUY",
            "data": {"ticker": "X", "signal": "BUY", "auto_buy_cap": 50.0,
                     "certainty_grade": "B", "target_weight_pct": 10.0,
                     "position_plan": {"first_tranche_pct_of_target": 50}},
            "created_at": "2026-10-15T00:00:00Z"})
        tdb.insert(conn, "decisions", {
            "decision_id": "X-20261015-1", "ticker": "X", "decision_date": "2026-10-15",
            "run_id": "analyze-2026-10-15", "signal": "BUY", "reason_code": None,
            "data": {"action": "BUY", "gate1_not_deleted": True,
                     "gate2_price_below_cap": True, "gate3_pass": True},
            "created_at": "2026-10-15T00:00:00Z"})
        tdb.insert(conn, "registry", {
            "ticker": "X", "status": "WATCH", "first_buy_date": None,
            "data": {"ticker": "X", "status": "WATCH"}, "updated_at": "2026-10-15T00:00:00Z"})
        conn.commit()

        decisions = Z.load_decisions(conn, "2026-10-15")
        # 現價 120 > 上限 50：即使日誌三閘門全過、訊號卡是 BUY，也不得產生委託
        plans = Z.plan_buys(conn, "2026-10-15", decisions, quotes={"X": 120.0},
                            adv={"X": 5_000_000.0}, slippage_bps=10)
        assert all(p["qty"] == 0 for p in plans), \
            "下單時的價格複檢必須擋下 price>cap 的 BUY（真實金錢防線）"
        assert any("未低於自動買進上限" in p["reason"] for p in plans)

    def test_price_handler_enforces_gate2_but_full_b_does_not(self):
        """同一套閘門語意，PRICE 工作有 price<cap／grade／flips 三重程式檢查；
        FULL_B 卻只把關 gate1／gate3。"""
        from trader.jobs.review_jobs import _map_action

        # PRICE：閘門未全過 → BUY_CANDIDATE 不會變成 BUY
        action, _ = _map_action("BUY_CANDIDATE", holding=False, passed=False,
                                first_buy=None, asof="2026-10-15", weight=None, target=None)
        assert action == "WATCH"
        action2, _ = _map_action("BUY_CANDIDATE", holding=False, passed=True,
                                 first_buy=None, asof="2026-10-15", weight=None, target=None)
        assert action2 == "BUY"

        from trader.analysis.checks import cap_signal_by_gates
        # FULL_B：沒有 price<cap 檢查的入口（函式只收 gate3 與 screen_decision）
        s, _ = cap_signal_by_gates("BUY", {"gate3_pass": True}, "PASS")
        assert s == "BUY"  # 價格是否低於上限，函式無從判斷、也不檢查


# =====================================================================
# 發現 3（高，設計一致性）：PRICE（弱模型）產生的 BUY 不經 opus 同意閘門、
#   也不經 RED_TEAM 紅隊複核，直接進入組合層下單。
#   FULL 路徑的 BUY 兩者都要。routing/model_routing.json 明寫
#   PRICE 用 weak、BUY_CANDIDATE → PORTFOLIO。
# =====================================================================
class TestPriceBuyBypassesApprovalAndRedTeam:
    def test_price_tier_is_weak_and_buy_needs_no_approval(self):
        routing = json.loads((PROJECT / "routing" / "model_routing.json").read_text(encoding="utf-8"))
        assert routing["jobs"]["PRICE"]["model"] == "weak"
        # 同意閘門只涵蓋 strong 等級（FULL_A、RED_TEAM）
        from trader.jobs import approvals
        class _Cfg:
            class approval:
                require_for_tiers = ["strong"]
        assert approvals.required(_Cfg, "weak") is False
        assert approvals.required(_Cfg, "strong") is True

    def test_price_buy_decision_reaches_plan_buys(self, tmp_path):
        """PRICE 回寫的 BUY 決策會被 daily 的 plan_buys 揀選並產生委託。"""
        from trader.portfolio import sizing as Z
        from trader.ledger import book
        from trader import db as tdb

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        tdb.migrate(conn)
        book.ensure_initial_deposit(conn, 100000.0, "2026-01-02")
        # 模擬 PRICE 回寫：訊號卡 + 決策（三閘門皆過、價格低於上限）
        tdb.insert(conn, "signals", {
            "decision_id": "X-20261015-1", "ticker": "X", "signal": "BUY",
            "data": {"ticker": "X", "signal": "BUY", "auto_buy_cap": 50.0,
                     "certainty_grade": "B", "target_weight_pct": 10.0,
                     "position_plan": {"first_tranche_pct_of_target": 50}},
            "created_at": "2026-10-15T00:00:00Z"})
        tdb.insert(conn, "decisions", {
            "decision_id": "X-20261015-1", "ticker": "X", "decision_date": "2026-10-15",
            "run_id": "review-2026-10-15-all", "signal": "BUY", "reason_code": None,
            "data": {"action": "BUY", "review_type": "PRICE",
                     "gate1_not_deleted": True, "gate2_price_below_cap": "通過", "gate3_pass": True},
            "created_at": "2026-10-15T00:00:00Z"})
        tdb.insert(conn, "registry", {
            "ticker": "X", "status": "WATCH", "first_buy_date": None,
            "data": {"ticker": "X", "status": "WATCH"}, "updated_at": "2026-10-15T00:00:00Z"})
        conn.commit()

        decisions = Z.load_decisions(conn, "2026-10-15")
        plans = Z.plan_buys(conn, "2026-10-15", decisions, quotes={"X": 40.0},
                            adv={"X": 5_000_000.0}, slippage_bps=10)
        assert any(p["qty"] > 0 and p["side"] == "BUY" for p in plans), \
            "PRICE 路徑的 BUY 會直接變成買單——未經任何人工同意或紅隊複核"


# =====================================================================
# 發現 4（中）：screen_quant.J4 在 roe_med5 缺值時照樣刪除，
#   違反 skill「缺欄位 → 條件不成立（不刪）／缺資料永遠不是刪除理由」。
# =====================================================================
class TestJ4DeletesOnMissingData:
    def test_j4_deletes_when_roe_med5_missing(self):
        import pandas as pd
        sys.path.insert(0, str(PROJECT / "berkshire-screen-merged" / "scripts"))
        import screen_quant as SQ

        df = pd.DataFrame([{
            "ticker": "T1", "Y": 2026, "sector": "20", "fin": False, "util": False,
            "net_income": -100.0,     # 最近一年虧損
            "ni_prev": 50.0,
            "shareholders_equity": 40.0,  # 權益從前一年 100 掉到 40（腰斬）
            "eq_prev": 100.0,
            "loss_yrs5": 1,           # 5 年只這 1 年虧損 → 例外條件成立
            "roe_med5": float("nan"),  # 但 roe_med5 缺值 → 例外無法驗證
            "n_ni5": 5, "ni_sum5": 200.0, "roe": float("nan"),
            "eps_g1": float("nan"), "dd52": float("nan"),
            "ocf_sum5": 300.0, "ocf_pos5": 4, "n_ocf5": 5, "operating_cash_flow": 80.0,
        }])
        out = SQ.screen(SQ.from_live(df.copy(), 2026), decision_date="2026-10-15")
        codes = out["delete_codes"].iloc[0]
        # 規則書面：5 年虧損 ≤1 且 roe_med5 ≥12% → 不刪（困境標記）
        # 實務：roe_med5 缺值 → 例外條件為 False → J4 刪除
        assert "J4" in codes, "缺 roe_med5 時 J4 仍刪除——與『缺資料永遠不是刪除理由』衝突"


# =====================================================================
# 發現 5（中，已知但仍在）：資料包含「平均回購價」欄（歷史價格水準），
#   價格隔離的 scrub 清單抓不到它；FULL_A（不許看價格）會看到。
# =====================================================================
class TestBuybackPriceLeakInDossier:
    def test_average_buyback_price_survives_scrub(self):
        sys.path.insert(0, str(PROJECT / "berkshire-deep-merged" / "scripts"))
        import build_dossier as BD
        from trader.analysis.dossier_prep import scrub_price_text

        # 造 10 年財報：回購金額與回購股數都有 → 平均回購價＝歷史股價
        yrs = list(range(2016, 2026))
        T = pd = __import__("pandas")
        df = T.DataFrame({
            "net_income": [100.0] * 10, "eps": [1.0] * 10, "shares": [1000.0] * 10,
            "buyback": [500.0] * 10,        # 每年花 500（百萬）回購
            "buyback_shares": [10.0] * 10,  # 每年回購 10（百萬股）
        }, index=yrs)
        R, filed, last, warn = BD.build.__wrapped__(None, "T", "2026-10-15") if hasattr(BD.build, "__wrapped__") else (None, None, None, None)
        # 直接算欄位（不依賴 build 的快取路徑）
        md_line = "| 平均回購價 | " + " | ".join(str(500.0 / 10.0) for _ in yrs) + " |"
        text, removed = scrub_price_text(md_line)
        assert removed == 0, "平均回購價欄未被價格 scrub 刪除（歷史價格水準進入資料包）"
        assert "50.0" in text

    def test_scrub_list_lacks_buyback_keywords(self):
        from trader.analysis.dossier_prep import PRICE_TEXT_PATTERNS
        hit = any(p.search("平均回購價") for p in PRICE_TEXT_PATTERNS)
        assert hit is False, "價格字句清單不含回購價相關樣式"


# =====================================================================
# 發現 6（中）：價格跳空事件（無 8-K）無法被模型判為「無公司行動」，
#   只能選一個具體行動類型或 needs_human；任何 ≥40% 的單日變動
#   （財報暴漲、軋空）都會暫停該檔直到人工處理。
# =====================================================================
class TestGapEventCannotBeDismissed:
    def test_gap_source_rejects_none_type(self):
        from trader.ledger import corp_actions as ca
        event = {"ticker": "XYZ", "source": "gap", "date": "2026-10-15",
                 "price_before": 50.0, "price_after": 72.0}
        result = {"ticker": "XYZ", "action_type": "無公司行動",
                  "effective_date": "2026-10-15", "ratio_new_per_old": None,
                  "cash_per_share": None, "new_ticker": None, "needs_human": False}
        errs = ca.verify(result, event, {"qty": 100, "avg_cost": 45.0})
        assert errs, "跳空事件判『無公司行動』被拒——只能選具體類型或轉人工（暫停）"

    def test_eightk_source_allows_none_type(self):
        from trader.ledger import corp_actions as ca
        event = {"ticker": "XYZ", "source": "8k", "date": "2026-10-15"}
        result = {"ticker": "XYZ", "action_type": "無公司行動",
                  "effective_date": "2026-10-15", "ratio_new_per_old": None,
                  "cash_per_share": None, "new_ticker": None, "needs_human": False}
        errs = ca.verify(result, event, {"qty": 100, "avg_cost": 45.0})
        assert errs == []

    def test_fabricated_stock_dividend_could_pass_verification_for_gap(self):
        """跳空事件沒有 Alpaca 比例可交叉驗證：模型若把比例寫成
        pb/pa（純粹模仿價格變動），市值守恆與價格比兩道覆核都會通過。"""
        from trader.ledger import corp_actions as ca
        pb, pa = 50.0, 72.0
        r = round(pb / pa, 4)  # 0.6944
        event = {"ticker": "XYZ", "source": "gap", "date": "2026-10-15",
                 "price_before": pb, "price_after": pa}
        result = {"ticker": "XYZ", "action_type": "股票股利", "effective_date": "2026-10-15",
                  "ratio_new_per_old": r, "cash_per_share": None, "new_ticker": None,
                  "needs_human": False, "fractional_handling": "無條件捨去"}
        errs = ca.verify(result, event, {"qty": 100, "avg_cost": 45.0})
        assert errs == [], "以 pb/pa 冒充股票股利比例可通過覆核（跳空事件無外部比例可比對）"


# =====================================================================
# 發現 7（中）：PRICE 的閘門 3 狀態取自「模型自己寫進決策日誌的
#   gate3_pass」，而不是框架算出的 gate3_result.json。
#   模型漏填 → 永遠「不通過」；模型填錯 → 與 gate3_result 不一致也照用。
# =====================================================================
class TestGate3StatusIsModelSelfReported:
    def test_gate3_status_reads_decision_log_not_gate3_result(self, tmp_path):
        from trader.jobs.review_jobs import _gate3_status
        from trader import db as tdb

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        tdb.migrate(conn)
        # 框架算出的 gate3_result（不過）與模型寫進日誌的 gate3_pass（true）互相矛盾
        tdb.insert(conn, "decisions", {
            "decision_id": "X-20261015-1", "ticker": "X", "decision_date": "2026-10-01",
            "run_id": "r", "signal": "WATCH", "reason_code": None,
            "data": {"gate3_pass": True},  # 模型自述通過
            "created_at": "2026-10-01T00:00:00Z"})
        conn.commit()
        # 沒有任何東西把 gate3_result.json 接到這個查詢上——日誌寫什麼就用什麼
        assert _gate3_status(conn, "X", "2026-10-15") == "通過"

    def test_missing_gate3_pass_means_never_passes(self):
        from trader.jobs.review_jobs import _gate3_status
        from trader import db as tdb
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        tdb.migrate(conn)
        tdb.insert(conn, "decisions", {
            "decision_id": "X-20261015-1", "ticker": "X", "decision_date": "2026-10-01",
            "run_id": "r", "signal": "WATCH", "reason_code": None,
            "data": {"action": "WATCH"},  # 沒有 gate3_pass 欄位
            "created_at": "2026-10-01T00:00:00Z"})
        conn.commit()
        assert _gate3_status(conn, "X", "2026-10-15") == "不通過"


# =====================================================================
# 發現 8（中）：帳本啟用後 _portfolio_state 只給持股、不給現金／
#   已承諾／檔數，FULL_B 的 G8 機會成本比較缺少現金與名額資訊。
# =====================================================================
class TestPortfolioStateMissingCash:
    def test_portfolio_state_drops_cash_when_positions_exist(self, tmp_path):
        from trader.pipelines.analyze import _portfolio_state
        from trader import db as tdb
        from trader.ledger import book

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        tdb.migrate(conn)
        book.ensure_initial_deposit(conn, 100000.0, "2026-01-02")
        tdb.insert(conn, "positions", {
            "ticker": "X", "qty": 10, "avg_cost": 40.0, "first_buy_date": "2026-01-05",
            "updated_at": "2026-01-05T00:00:00Z"})
        conn.commit()

        class _Cfg:
            class account:
                initial_cash = 100000.0

        ps = _portfolio_state(_Cfg, conn)
        assert ps["holdings"] and ps["note"] == "持股取自帳本 positions 表"
        assert "cash" not in ps and "committed" not in ps and "n_holdings" not in ps, \
            "有持股時現金、已承諾、檔數全部消失——G8 無從比較"


# =====================================================================
# 發現 9（中低）：PRICE 回寫把 gate2 寫成中文字串（"通過"），
#   但每月檢討報告的 _truthy 只認 bool／"true" → 每個 PRICE 買進
#   都會被 compliance 判為「缺閘門紀錄」。
# =====================================================================
class TestValidationTruthyMismatch:
    def test_price_gate2_string_fails_validation_truthy(self):
        from trader.report import validation as V
        from trader.portfolio import sizing as Z

        gate2 = "通過"
        assert Z._truthy(gate2) is True, "sizing 認得中文字串"
        assert V._truthy(gate2) is False, "validation 不認——PRICE 買進會被列為缺閘門"

    def test_write_price_result_stores_gate2_as_string(self, tmp_path):
        """實際呼叫 write_price_result，確認日誌裡的 gate2_price_below_cap 是字串。"""
        from trader.analysis import writeback
        from trader import db as tdb
        from trader.paths import Paths

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        tdb.migrate(conn)
        paths = Paths(root=PROJECT, state=Path(str(tmp_path)))  # root＝berkshire/（含 skill 規則檔）
        paths.theses.mkdir(parents=True, exist_ok=True)
        writeback.write_price_result(
            conn, paths, "X", "2026-10-15", "r", action="BUY", gate1=True,
            gate2="通過", gate3="通過",
            cap={"auto_buy_cap": 50.0, "IV_base": 60.0, "absurd_price_line": 100.0,
                 "r_d": 0.10, "cap": 55.0, "grade_used": "B", "g_impl": 0.08},
            price=40.0, price_date="2026-10-15", t30=0.05, reason="測試",
            notes=["測試用"])
        row = conn.execute("SELECT data FROM decisions WHERE ticker='X'").fetchone()
        log = json.loads(row["data"])
        assert log["gate2_price_below_cap"] == "通過"  # 字串，非 bool


# =====================================================================
# 發現 10（中低，已知）：skill fetch_prices.feats 在決策日非月末時，
#   最後一根月線含決策日後的價格（Yahoo 月線為不完整月）。
#   README 已列為已知限制；此測試量化其影響。
# =====================================================================
class TestFetchPricesMidMonthLookahead:
    def test_partial_month_bar_uses_prices_after_asof(self):
        import pandas as pd
        sys.path.insert(0, str(PROJECT / "berkshire-screen-merged" / "scripts"))
        import fetch_prices as FP

        # 模擬 Yahoo 月線：10/1 那根的 close 是「抓取當時」的收盤（10/16）
        dates = pd.to_datetime(["2026-08-01", "2026-09-01", "2026-10-01"])
        s = pd.DataFrame({"date": dates, "adj_close": [95.0, 90.0, 99.0],
                          "close": [95.0, 90.0, 99.0]})
        asof = pd.Timestamp("2026-10-15")
        out = FP.feats(s.copy(), asof)
        # 10/16 的收盤 99 出現在「決策日 10/15」的 price 裡
        assert out["price"] == 99.0
        assert str(out["price_date"])[:10] == "2026-10-01"


# =====================================================================
# 發現 11（中低）：現金股利沒有發放日時永遠不會發放入帳
#   （dividend_receivable 永留帳上；NAV 含應收所以不失真，但現金永不回收）。
# =====================================================================
class TestDividendWithoutPayDateNeverSettles:
    def test_no_pay_date_skips_settlement(self, tmp_path):
        from trader.ledger import corp_actions as ca
        from trader import db as tdb

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        tdb.migrate(conn)
        # 除息日先記應收（與 _apply_cash_dividend 一致）
        tdb.insert(conn, "cash_ledger", {
            "date": "2026-09-18", "amount": 50.0, "kind": "dividend_receivable",
            "ref": "CA:X:現金股利:2026-09-18", "note": "除息應收"})
        conn.execute(
            "INSERT INTO corporate_actions(ticker, action_type, effective_date, ratio_new_per_old,"
            " cash_per_share, new_ticker, status, source, data, created_at)"
            " VALUES ('X','現金股利','2026-09-18',NULL,0.5,NULL,'applied','alpaca',?, '2026-09-18T00:00:00Z')",
            (json.dumps({"pay_date": None, "amount": 50.0, "paid": False, "ref": "CA:X:現金股利:2026-09-18"}),))
        conn.commit()
        paid = ca.settle_dividends(conn, "2027-01-01")
        assert paid == [], "沒有發放日的股利永遠不會入帳（應收永留）"
        rec = conn.execute("SELECT COALESCE(SUM(amount),0) FROM cash_ledger"
                           " WHERE kind='dividend_receivable'").fetchone()[0]
        assert rec == pytest.approx(50.0)


# =====================================================================
# 發現 12（低）：PRICE 回寫的荒謬級線 = 50 × (norm_eps or oe)；
#   當 norm_eps=0（無正常化 EPS）時退回 oe，但 oe 為負時
#   50×oe < 0 → 荒謬級線變成負數，永遠不會觸發。
# =====================================================================
class TestAbsurdLineNegativeWhenOeNegative:
    def test_absurd_line_negative(self):
        # 重現 review_jobs.PRICE pre_script 的算式
        x = {"norm_eps": 0.0, "oe": -2.5}   # norm_eps=0 falsy → 用 oe
        e = x.get("norm_eps") or x["oe"]
        absurd = round(50 * float(e), 4)
        assert absurd < 0, "業主盈餘為負時荒謬級線為負——『至少賣一半』的觸發永不成立"


# =====================================================================
# 補充驗證：以 test_runs 真實模型輸出為證——FULL_B 的決策日誌中，
# 模型自行填入的 gate2_price_below_cap 是 bool 且數值正確
# （OTIS：價格 70.39 > 上限 49.29 → False），與框架 gate3_result 一致。
# 即 H2／M4 是「框架沒把關」問題，不是「模型已出錯」問題。
# =====================================================================
class TestRealModelLogsAreConsistent:
    def test_otis_full_b_log_matches_framework_gate3(self):
        base = PROJECT / "test_runs" / "m3-OTIS-rerun-2026-10-16" / "work" / "analyze-2026-10-16" / "OTIS"
        log = json.loads((base / "decision_log.json").read_text(encoding="utf-8"))
        g3 = json.loads((base / "gate3_result.json").read_text(encoding="utf-8"))
        assert log["gate2_price_below_cap"] is False  # 模型自己寫對（價格高於上限）
        assert log["gate3_pass"] is True
        assert log["gate3_pass"] == g3["gate3_pass"]  # 與框架計算一致
        assert isinstance(log["gate3_answers_file"], str) and log["gate3_answers_file"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
