"""第三輪自我查核（2026-10-17）：驗證 v2 各條的**真實性與可達性**，並用同一批測試推翻我自己誇大的部分。

規則與 v2 相同：斷言的是程式「實際做了什麼」，綠燈＝該裁決成立。
本檔的任務有兩類：
  (A) 證明我 v2 的缺陷鏈在正常流程裡**真的可達**（不靠測試直接塞 None）。
  (B) 證明我 v2 誇大的部分**不成立**（因此必須降級或撤銷）。
"""
from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
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
from trader import db  # noqa: E402
from trader.models import JobSpec  # noqa: E402
from trader.analysis import writeback  # noqa: E402
from trader.analysis import checks  # noqa: E402
from trader.paths import PKG_ROOT, Paths  # noqa: E402
from trader.data import universe  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def paths(tmp_path):
    p = Paths(root=PKG_ROOT, state=tmp_path / "state")
    p.ensure()
    (p.state / "cap_inputs").mkdir(parents=True, exist_ok=True)
    return p


def _held_row(conn, ticker, **over):
    entry = {"ticker": ticker, "status": "HOLDING", "grade": "B",
             "auto_buy_cap": 90.0, "sell_review_above": 100.0, "absurd_price_line": 300.0,
             "review_price": 80.0, "t30_at_valuation": 0.042, "cost": 70.0,
             "weight": 0.06, "target_weight": 0.10, "first_buy_date": "2025-01-01",
             "last_review_date": "2026-08-01", "last_full_review_date": "2026-08-01",
             "thesis_file": f"theses/{ticker}.md", "kill_criteria": ["市占連續兩季下滑"]}
    entry.update(over)
    db.insert(conn, "registry", {"ticker": ticker, "status": "HOLDING",
                                 "data": entry, "updated_at": db.now_iso()})
    return entry


# ---------------------------------------------------------------- (A) 可達性：FULL 回寫
def test_full_writeback_drops_sell_lines_when_model_entry_omits_them(conn, paths, tmp_path):
    """正常流程可達的向量：**不是**空模板，而是 FULL 回寫時登錄表內容直接採用模型自寫的
    `registry_entry.json`；框架只保留 cost/first_buy_date/weight/build_plan 四欄，
    `sell_review_above`／`auto_buy_cap`／`absurd_price_line`／`grade` 一律不保留、也不與 cap_result 比對。
    """
    t = "KO"
    _held_row(conn, t)
    tdir = tmp_path / "tdir"
    tdir.mkdir()
    (tdir / "signal_card.json").write_text(json.dumps(
        {"ticker": t, "signal": "BUY", "reason_code": "PASS_ALL", "certainty_grade": "B"}),
        encoding="utf-8")
    # 模型交回的登錄表只寫了必要欄，估值線全缺
    (tdir / "registry_entry.json").write_text(json.dumps(
        {"ticker": t, "status": "HOLDING", "thesis": "x"}), encoding="utf-8")
    (tdir / "decision_log.json").write_text(json.dumps(
        {"ticker": t, "decision_date": "2026-10-17", "action": "BUY"}), encoding="utf-8")
    (tdir / "thesis.md").write_text("# 論點\n", encoding="utf-8")

    writeback.write_full_result(conn, paths, t, "2026-10-17", "R9", tdir)

    after = json.loads(conn.execute("SELECT data FROM registry WHERE ticker = ?", (t,)).fetchone()["data"])
    assert after.get("sell_review_above") is None
    assert after.get("auto_buy_cap") is None
    assert after.get("absurd_price_line") is None
    assert after.get("cost") == 70.0, "cost 被保留 → 證明只有這四欄有守衛，估值線沒有"
    assert after["status"] == "HOLDING", "狀態仍是 HOLDING，也就是說這檔「持有中但沒有賣出線」"


def test_review_due_goes_silent_for_that_row_with_real_price_data():
    """接在上條之後：那筆真實登錄表狀態喂進 review_due，價格漲到 3 倍也不產生任何「賣出線」觸發。
    對照組：同一份輸入把三條線補回來，立即出現 [PRICE] 賣出檢視與荒謬級線。
    """
    base = {"ticker": "KO", "cost": 70.0, "weight": 0.06, "target_weight": 0.10,
            "review_price": 80.0, "t30_at_valuation": 0.042,
            "last_review_date": "2026-08-01", "last_full_review_date": "2026-08-01"}
    px = pd.DataFrame({"date": [date(2026, 10, 1), date(2026, 10, 16)], "close": [100.0, 300.0]})
    bare = dict(base, status="HOLDING")
    action, reasons, _ = review_due.check(bare, [], px, "2026-10-17", 0.042, news=[])
    assert not any("sell_review_above" in r or "荒謬" in r for r in reasons), reasons
    assert action != "PRICE", f"三條線都沒有的 HOLDING 列不應觸發 PRICE，實得 {action}／{reasons}"

    armed = dict(bare, sell_review_above=100.0, absurd_price_line=300.0, auto_buy_cap=90.0)
    a2, r2, _ = review_due.check(armed, [], px, "2026-10-17", 0.042, news=[])
    hits = [r for r in r2 if "sell_review_above" in r or "荒謬" in r]
    assert len(hits) == 2 and all(r.startswith("[PRICE]") for r in hits), r2


# ---------------------------------------------------------------- (B) 撤銷：None 鍵刪除不是原因
def test_none_key_drop_in_review_py_is_a_no_op_for_review_due():
    """v2 我把 `pipelines/review.py:55`（刪除值為 None 的鍵）寫成缺陷鏈的一環。
    實測：`review_due.check()` 對「鍵值為 None」與「鍵不存在」行為完全相同（都是 `e.get(k) and …` 的假值短路），
    所以那行只是序列化整潔，**不構成失靈原因**——原因只有一個：值缺了，而程式把「缺值」當「沒觸發」。
    """
    px = pd.DataFrame({"date": [date(2026, 10, 1), date(2026, 10, 16)], "close": [100.0, 3000.0]})
    base = {"ticker": "KO", "status": "HOLDING", "cost": 10.0, "weight": 0.06, "target_weight": 0.10,
            "last_review_date": "2026-08-01", "last_full_review_date": "2026-08-01"}
    with_none = dict(base, sell_review_above=None, absurd_price_line=None, auto_buy_cap=None)
    with_missing = dict(base)
    a1, r1, _ = review_due.check(with_none, [], px, "2026-10-17", 0.042, news=[])
    a2, r2, _ = review_due.check(with_missing, [], px, "2026-10-17", 0.042, news=[])
    assert (a1, sorted(r1)) == (a2, sorted(r2))
    assert a1 != "PRICE"


def test_holding_row_is_protected_from_the_blank_template():
    """v2/P1-3 的寫法需要限定條件：空模板只在「前一列不是 HOLDING」時套用。
    HOLDING 走保留分支（writeback.py:335-337），所以「任何一次拒絕或 STOP 都把持股的估值記憶清乾淨」這句話
    對 HOLDING **不成立**，只有 WATCH 階段成立。
    """
    src = inspect.getsource(writeback.write_stop_result)
    assert 'prev["status"] == "HOLDING"' in src and "保留既有登錄表資料" in src


# ---------------------------------------------------------------- (B) 撤銷：gate2 的兩個誇大
def test_gate2_is_fail_closed_when_auto_buy_cap_is_none():
    """我在 §2 寫「上限消失 → 任意價買進」。實查 gate2 原始碼：`auto is None → gate2 = "無上限"`，
    而 `passed` 需要 `gate2 == "通過"` → **缺上限不會亂買，是不買**。P0-2 的真實金錢方向只剩
    「iv_base 缺、cap 仍有值（退化為 P2′）」那一支。
    """
    body = (ROOT / "trader" / "trader" / "jobs" / "review_jobs.py").read_text(encoding="utf-8")
    assert 'gate2 = "無上限"' in body
    assert 'gate2 == "通過"' in body


def test_review_jobs_cannot_be_imported_standalone():
    """新發現（第三輪）：`python3 -c "import trader.jobs.review_jobs"` 一定炸。
    鏈是 review_jobs → deep → handlers →（handlers 尾端）from .deep import DossierHandler，
    而此時 deep 只執行到第 28 行，還沒有 DossierHandler。能跑起來只因為別處先匯入 handlers。
    """
    r = subprocess.run([sys.executable, "-c", "import trader.jobs.review_jobs"],
                       cwd=str(ROOT / "trader"), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                       capture_output=True, text=True)
    assert r.returncode != 0 and "partially initialized module 'trader.jobs.deep'" in r.stderr, r.stderr[-300:]
    ok = subprocess.run([sys.executable, "-c", "import trader.jobs.handlers, trader.jobs.review_jobs"],
                        cwd=str(ROOT / "trader"), env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr[-300:]


def test_full_lane_never_reconciles_registry_entry_with_cap():
    """把 N-1 講精確：FULL_B 的 post 會自己跑 cap_calc（deep.py:346-357），
    不一致時 `apply_cap_override` 只覆寫**訊號卡**；登錄表那一列完全採用模型自寫的
    `registry_entry.json`，框架手上的 cap 數字從未與之比對或合併。
    → 只要模型寫的數字與 cap_result 不同，登錄表就長期偏離「以腳本為準」的鐵律，而且無註記。
    """
    deep_src = (ROOT / "trader" / "trader" / "jobs" / "deep.py").read_text(encoding="utf-8")
    assert "card = apply_cap_override(card, cap)" in deep_src
    full_src = inspect.getsource(writeback.write_full_result)
    assert "cap_result" not in full_src and "apply_cap_override" not in full_src
    assert 'for k in ("cost", "first_buy_date", "weight", "build_plan")' in full_src
    iv_keys = [k for k in ("sell_review_above", "auto_buy_cap", "absurd_price_line") if k in full_src]
    assert iv_keys == [], f"write_full_result 若出現這些鍵，本裁決就要改：{iv_keys}"
    card_only = inspect.getsource(checks.apply_cap_override)
    assert "signal_card" in card_only and "registry" not in card_only


def test_g_impl_gt_cap_flag_is_redundant_for_gate2():
    """v2/N-7 我主張「規則寫進閘門 2 的旗標，框架從不讀」是缺陷。
    實測掃價：旗標亮起（g_impl 解不出来）的每一檔，`price ≥ cap ≥ auto_buy_cap` 恆成立，
    而 gate2 要求 `price < auto_buy_cap` → **早就過了不了閘門 2**。旗標是冗餘資訊，不是漏洞。
    """
    base = json.loads((ROOT / "berkshire-deep-merged" / "assets" / "cap_calc_example_input.json")
                      .read_text(encoding="utf-8"))
    n = 0
    for price in [120, 150, 200, 400, 1000, 4000, 40000]:
        x = dict(base, price=float(price))
        r = cap_calc.calc(x)
        if r.get("g_impl_gt_cap"):
            n += 1
            assert price >= r["cap"], f"price={price} cap={r['cap']}"
            assert r["auto_buy_cap"] is None or price >= r["auto_buy_cap"]
    assert n >= 5, "掃價應讓多數高價情形亮起旗標，否則本測試沒有意義"


# ---------------------------------------------------------------- (B) 降級：g_term 下限
def test_g_term_floor_effect_is_small_and_regime_dependent():
    """v2/N-5 我寫成「系統性放寬買進上限」。實測差異只有：
    30Y=0.9% → +0.5%；0.5% → +2.4%；0% → +4.8%。而 30Y ≥ 4%（近十年常態）時兩者同為 3%，**零影響**。
    偏差是真的（規則 `min(3%, 30Y−1pp)` 可為負，程式夾 0），但嚴重度是 P2，不是 P0/P1。
    """
    oe = 5.0
    for L, maxdelta in ((0.009, 0.01), (0.005, 0.03), (0.0, 0.06), (0.045, 0.0)):
        r_d = max(0.10, L + 0.03)
        rule = cap_calc.pv(oe, 0.10, r_d, min(0.03, L - 0.01))
        code = cap_calc.pv(oe, 0.10, r_d, min(0.03, max(0.0, L - 0.01)))
        delta = code / rule - 1
        assert -1e-12 <= delta <= maxdelta, f"L={L} 差異 {delta:.3%} 超過宣稱的幅度"


# ---------------------------------------------------------------- (B) 更正：universe_versions 有被讀
def test_universe_versions_table_is_read_not_write_only():
    """v2/N-11 我寫「該表全專案只寫不讀」——錯。`universe.py:55` 會讀最新一列的 sha256 做變動偵測。
    仍成立的是：**沒有 asof 版本的股票池**，所以歷史 asof 重播一律用今天的成分股。
    """
    src = inspect.getsource(universe)
    assert "SELECT sha256 FROM universe_versions ORDER BY version_id DESC LIMIT 1" in src
    assert "asof" not in inspect.signature(universe.load_universe).parameters


# ---------------------------------------------------------------- (B) 降級：P1-5 是同 run 的死巷，且與文件宣示一致
def test_rejected_approval_row_is_a_dead_end_within_the_same_run(tmp_path):
    """實測：某 (run_id, ticker) 的同意列變 `rejected` 後，`gate()` 仍回 False 但**不再建 pending 列**
    （approvals.py:59-61 「已有列則沿用」），`approve()` 對 rejected 直接抛錯（:140-141），
    `release()` 只撈 approved（:179）→ 同一 run 內反悔沒有出口。
    但 approvals.py:4 的文件就寫著「rejected：下次觸發再排」——新的 run 用新 run_id 會重建 pending 列，
    所以這是**同 run 的可用性缺口**，不是永久鎖死；v2 把它列 P1 偏高，降為 P2。
    """
    from trader.jobs import approvals
    c = db.connect(tmp_path / "a.db")
    db.migrate(c)
    spec = JobSpec(job_id="J1", run_id="R1", job_type="FULL_A", ticker="KO", asof="2026-10-17",
                   prompt_text="x", sandbox=tmp_path)
    assert approvals.gate(c, spec, "strong") is False
    row = approvals._row(c, "R1", "KO")
    assert row["status"] == "pending"
    approvals._close(c, row["approval_id"], "rejected", "test")   # 與 reject() 用的同一個原語
    assert approvals.gate(c, spec, "strong") is False
    assert approvals._row(c, "R1", "KO")["status"] == "rejected", "同一 run 不會重建 pending 列"
    with pytest.raises(approvals.ApprovalError):
        approvals.approve(c, ids=[row["approval_id"]])
    assert approvals.release(c, "R1") == [], "rejected 列不會被 release 撈回"
    # 出口在下一個 run：
    spec2 = JobSpec(job_id="J2", run_id="R2", job_type="FULL_A", ticker="KO", asof="2026-10-17",
                    prompt_text="x", sandbox=tmp_path)
    assert approvals.gate(c, spec2, "strong") is False
    assert approvals._row(c, "R2", "KO")["status"] == "pending"
    c.close()
