"""RM5-5：X53 返工（FULL_A 新聞標題價格盲）、output_schemas 括號、M5-D3-05（_redo 帶 output_retry＋D47 上限前保守收尾）、
M5-R3-02（FAIL／THESIS 的 HOLD_REVIEW 不給未覆核的 FULL_B 原版）、M5-RS1-03（驗證重試提示告知檔案已清除）。
RM5-5R：弱動詞無數字、價位、市值、評等／買賣建議、中文新高也擋，回購／增資放行；紅隊第一輪遇 D47 上限照第二輪處理；
X55：P-43 觸發理由在價格盲工作只留類別。
RM5-5R2：營收等破紀錄／創新高放行，補一批常見股價／評等寫法（D78）。全部 FakeRunner／tmp_path，不碰正式 state。"""
from __future__ import annotations

import json

from test_engine import RUN, consistent_card, full_a, full_b, make_env, red_team
from test_fix_P32 import _ctx, _files, _job, _td, env2  # noqa: F401  (env2 是 fixture)

from trader.jobs import validate
from trader.jobs.deep import FullAHandler, _blind_drop, trigger_text
from trader.jobs.hold_jobs import HoldReviewHandler
from trader.paths import get_paths

# check-X53 實驗的典型行情／評等／估值標題（主詞不限、過去式），FULL_A 一則都不得出現
PRICEY = [
    "Apple shares plunge after earnings miss", "Analyst raises price target on Apple to $250",
    "Apple downgraded to Sell at Morgan Stanley", "Morgan Stanley upgraded Apple to Buy", "Apple hits 52-week high",
    "Apple stock tumbles 8% on weak guidance", "Apple market value tops $3 trillion", "Apple sheds $200 billion in value",
    "Apple slumps after iPhone sales disappoint", "Apple rallies 5% as investors cheer buyback", "Why Apple is a buy right now",
    "Apple's selloff deepens", "Apple soars", "Apple sinks 7%", "Apple up 12% year to date", "Apple trades at 30 times earnings",
    "蘋果股價重挫 8%", "蘋果目標價上調至 250 美元", "Apple tanks on DOJ suit", "Apple's worst day since 2020",
    "Apple loses 9% in a week", "Investors dump Apple", "Apple outperforms S&P 500", "Apple yields 0.5%",
    "Apple gains 3%", "蘋果大跌",
]
# RM5-5R（check-RM5-5 另擬的常見標題）：弱動詞無數字、價位、市值、評等／買賣建議、中文新高
PRICEY_R = [
    "Apple falls on China worries", "Apple rises after earnings beat", "Apple jumps as iPhone demand surges",
    "Apple drops after report", "Apple slips", "Apple dips on tariff fears", "Apple climbs on AI hopes",
    "Apple gains after upgrade", "Apple down sharply", "Apple extends losses", "Apple pares gains",
    "Apple edges higher", "Apple hits record", "Apple hits new high", "Apple hits lowest level since 2023", "蘋果創新高",
    "Apple valued at $4T", "Apple worth $4 trillion", "Apple becomes first $4 trillion company",
    "Apple cut to neutral at Barclays", "Is Apple a buy?", "Buy Apple before earnings", "Buy the dip in Apple",
    "Apple looks overbought", "Apple added to Conviction List",
    # RM5-5R2 補擋
    "Apple tops $4 trillion", "Apple nears $5 trillion", "Apple sets record close", "Apple hits 3-month low",
    "Nasdaq enters correction territory as Apple slides", "Should you buy Apple?", "Time to sell Apple?",
    "3 reasons to buy Apple", "Apple is a great buy", "Sell Apple before earnings", "Apple trades higher",
    "Apple moves lower", "Apple edges lower", "Apple outpaces Nasdaq", "Apple lags Dow", "Apple drags S&P lower",
    "Apple among top gainers", "Apple leads losers", "Apple best performer in Dow",
    "Barclays sets Apple price objective", "Apple PT raised at Citi", "Apple at 35x earnings", "蘋果收紅", "台積電收黑",
    "外資買超台積電", "外資賣超鴻海", "鴻海漲停", "聯發科跌停", "Apple retreats from record", "Apple recovers losses",
    "Apple wipes out 2026 gains", "Apple bounces back", "Apple flat after earnings", "Apple hits all-time high",
    "Apple hits a record high", "Apple reaches new high", "Apple downgraded to neutral", "Apple at neutral",
    "Apple neutral rating reiterated", "Apple hits fresh low", "蘋果創歷史新高", "Apple hits highest since 2021",
    # RM5-5R3
    "Strong iPhone sales lift Apple to record high", "蘋果股價創新高", "Apple drags Dow lower", "PT raised to $300",
]
# 基本面標題（行情動詞前接營收等，或非行情用語）要保留；回購／增資放行（RM5-5R）
FUNDAMENTAL = [
    "Apple revenue rises 6% to $90 billion", "iPhone sales slump in China", "Apple earnings fall 10% on weak demand",
    "Apple to buy AI startup for $2 billion", "Apple drops lawsuit against Masimo", "Apple CEO steps down",
    "Apple gains market share in India", "台積電營收大漲 30%",
    "Apple announces $90 billion share buyback", "Apple to repurchase $10 billion of shares",
    "Apple prices $5 billion stock offering", "蘋果宣布股票回購", "Apple gains approval for device",
    "Apple extends partnership with Broadcom", "Apple agrees to buy startup",
    # RM5-5R2：基本面破紀錄與易誤殺寫法
    "Apple revenue hits record", "Apple quarterly revenue hits all-time high", "iPhone sales hit record in India",
    "蘋果營收創新高", "台積電 9 月營收創歷史新高", "台積電毛利率創新高",
    "Apple extends deal with Qualcomm", "Apple loses patent case", "Apple raises prices", "Apple revenue tops estimates",
    "Apple pledges carbon neutral by 2030", "Buy now, pay later firm Affirm partners with Apple",
    "Apple launches Buy Now, Pay Later", "Apple faces new low-cost rival", "鴻海宣布回購", "台積電辦理現金增資",
    "Services revenue tops $25 billion", "iPhone sales bounce back in China", "Apple sales again hit record",
    # RM5-5R3（check-RM5-5R2 誤殺）
    "中華電宣布調漲資費", "中鋼調漲盤價", "台積電宣布漲價", "台積電營收連續3個月創新高", "鴻海營收年增20%創新高",
    "台積電Q3營收季增15%、創新高", "Production drags down earnings", "Revenue rose to a record high",
    "Sales reached a record high", "Apple revenue hits an all-time high", "Revenue hits fresh record high",
    "Sales of iPhone top $50 billion", "SoftBank unveils $1 trillion investment plan", "Coca-Cola wins top performer award",
    "Tesla hits new milestone with 5 millionth car", "PT Telkom signs deal with Microsoft",
]
KEYS = {"downgraded", "upgraded", "slumps", "market value", "times earnings", "sheds", "soars", "sinks", "rallies",
        "selloff", "year to date", "tanks", "worst day", "in a week", "dump", "outperforms", "yields", "is a buy",
        "重挫", "大跌",
        "falls on", "rises after", "Apple hits record", "valued at", "to neutral", "a buy?", "Buy the dip", "overbought",
        "蘋果創新高", "top gainers", "收紅", "漲停", "price objective", "trades higher", "bounces back"}


def _news(hs):
    return {"news": [{"date": "2026-10-05", "headline": h, "source": "src"} for h in hs]}


def test_blind_filter_drops_price_headlines_keeps_fundamentals():
    assert [h for h in PRICEY + PRICEY_R if not _blind_drop(h)] == []
    assert [h for h in FUNDAMENTAL if _blind_drop(h)] == []
    t = trigger_text(_news(PRICEY + PRICEY_R + FUNDAMENTAL), news=True)
    assert not [k for k in KEYS if k in t.replace("sales slump", "")], t
    for h in ("Apple revenue rises", "iPhone sales slump in China", "Apple drops lawsuit against Masimo", "營收大漲",
              "share buyback", "stock offering", "Apple gains approval",
              "Apple revenue hits record", "蘋果營收創新高", "毛利率創新高", "carbon neutral", "new low-cost rival",
              "Buy now, pay later", "Apple revenue tops estimates", "iPhone sales bounce back"):
        assert h in t, h


def test_buyback_with_price_move_still_dropped():
    # 回購只放行 shares／stock 字眼；同一則有漲跌動詞照擋
    for h in ("Apple shares jump on buyback", "Apple rises after share buyback", "Apple stock buyback lifts shares"):
        assert _blind_drop(h), h


def test_all_price_headlines_no_news_section_in_full_a():
    assert "新聞標題" not in trigger_text(_news(PRICEY + PRICEY_R), news=True)


def test_full_b_keeps_existing_filter():
    # FULL_B 看得到價格：維持 is_price_headline（評等過去式等仍可見），不受價格盲濾網影響
    t = trigger_text(_news(["Apple slumps after iPhone sales disappoint", "Apple shares plunge after earnings miss",
                            "Apple falls on China worries", "Apple cut to neutral at Barclays"]),
                     price_blind=False, news=True)
    assert "Apple slumps" in t and "shares plunge" not in t
    assert "Apple falls on China worries" in t and "neutral" in t


def test_full_a_prompt_has_no_price_headlines(env2):  # noqa: F811
    paths, conn = env2
    ctx = _ctx(paths, conn)
    (_td(paths) / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    j = _job("FULL_A", _news(PRICEY + PRICEY_R + FUNDAMENTAL))
    h = FullAHandler()
    h.prepare(j, ctx)
    trig = h.prompt_values(j, ctx, "claude_code")["trigger"]
    for k in ("downgraded", "slumps after", "market value", "times earnings", "upgraded", "falls on", "Apple hits record",
              "valued at", "to neutral", "收紅"):
        assert k not in trig, k
    assert "營收創新高" in trig
    assert "Apple revenue rises" in trig


# ---------------------------------------------------------------- X55：P-43

P43 = "[FULL] 單檔權重 27.3% > 25%（P-43）：完整複查（因上漲超限不賣，設計決策 D01）"


def test_p43_reason_blind_only_category():
    t = trigger_text({"reasons": [P43, "[FULL] 上次分析後有新年報（10-K 2026-09-30）"]})
    assert "[FULL]（制度性完整複查，細節略）" in t
    assert not [k for k in ("上漲", "權重", "P-43", "超限") if k in t], t
    assert "10-K 2026-09-30" in t   # 其他理由照舊
    tb = trigger_text({"reasons": [P43]}, price_blind=False)   # FULL_B／HOLD_REVIEW 看得到價格：不變
    assert "P-43" in tb and "因上漲超限不賣" in tb


# ---------------------------------------------------------------- output_schemas 全形括號

def test_output_schemas_full_b_note_parens_balanced():
    d = json.loads(get_paths().schemas_json.read_text(encoding="utf-8"))
    t = d["FULL_B"]["_說明"]
    assert t.count("（") == t.count("）")
    assert "§2）；tail_worst_loss_pct" in t


# ---------------------------------------------------------------- M5-D3-05

def _missing_files(r):
    r["files"] = {"KO/report.md": "# 只寫了報告\n"}
    return r


def _cap_mismatch(r, signal="WATCH"):
    card = consistent_card(signal)
    card["auto_buy_cap"] = 999.0
    r["files"]["KO/signal_card.json"] = json.dumps(card)
    return r


def _bad_num(r, signal="WATCH"):
    card = consistent_card(signal)
    card["target_weight_pct"] = "abc"   # 數值欄不是數字 → input_retry
    r["files"]["KO/signal_card.json"] = json.dumps(card)
    return r


def _decs(env):
    return [tuple(r) for r in env.conn.execute(
        "SELECT signal, reason_code FROM decisions WHERE ticker='KO' ORDER BY rowid")]


def _last_data(env):
    return env.conn.execute("SELECT data FROM decisions WHERE ticker='KO' ORDER BY rowid DESC").fetchone()[0]


def test_missing_cap_missing_ends_missing_data_without_4th_full_b(tmp_path):
    """缺檔 → cap 不一致 → 缺檔：FULL_B:3 帶 output_retry → _missing_hold 寫 MISSING_DATA，不建第 4 個 FULL_B。"""
    def fb(job):
        n = job.job_id.rsplit(":", 1)[-1]
        r = full_b("WATCH")
        return _missing_files(r) if n in ("1", "3") else _cap_mismatch(r)
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": fb})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    rows = env.by_type("FULL_B")
    assert len(rows) == 3 and all(r["status"] != "failed" for r in rows)
    assert json.loads(rows[2]["params"]).get("output_retry") == 1
    assert ("NO_ACTION", "MISSING_DATA") in _decs(env)


def test_input_missing_cap_chain_stops_at_d47_limit(tmp_path):
    """數值欄壞 → 缺檔 → cap 不一致：第 3 個 FULL_B 已達 D47 上限 → 不再退回，以腳本覆寫收尾（無 failed 的第 4 個）；
    cap 註記寫「已達 D47 上限」，不寫「框架改了輸入或等級」（RM5-5R）。"""
    def fb(job):
        n = job.job_id.rsplit(":", 1)[-1]
        r = full_b("WATCH")
        if n == "1":
            return _bad_num(r)
        return _missing_files(r) if n == "2" else _cap_mismatch(r)
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": fb})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    rows = env.by_type("FULL_B")
    assert len(rows) == 3 and [r["status"] for r in rows] == ["done"] * 3
    assert _decs(env) and _decs(env)[-1][0] == "WATCH"
    d = _last_data(env)
    assert "已達 D47 上限，不再退回" in d and "框架改了輸入或等級" not in d


def test_red_team_fatal_at_d47_limit_is_red_light(tmp_path):
    """數值錯 → cap 不一致 → FULL_B:3 BUY → 紅隊判致命：FULL_B 已達 D47 上限，紅隊不再退回，
    照第二輪處理 → NO_ACTION／RED_LIGHT；沒有 failed 的 FULL_B、沒有 job_failed 異常（RM5-5R）。"""
    def fb(job):
        n = job.job_id.rsplit(":", 1)[-1]
        r = full_b("BUY")
        if n == "1":
            return _bad_num(r, "BUY")
        return _cap_mismatch(r, "BUY") if n == "2" else r
    env = make_env(tmp_path, {"FULL_A": full_a(), "FULL_B": fb, "RED_TEAM": red_team(True)})
    env.price_file("KO")
    env.add(env.job("KO", "FULL_A", 1))
    env.engine.run(RUN)
    rows = env.by_type("FULL_B")
    assert len(rows) == 3 and all(r["status"] != "failed" for r in rows), [r["status"] for r in rows]
    assert len(env.by_type("RED_TEAM")) == 1
    assert _decs(env)[-1] == ("NO_ACTION", "RED_LIGHT"), _decs(env)
    assert "紅隊退回改按第二輪處理" in _last_data(env)
    assert env.conn.execute("SELECT COUNT(*) FROM anomalies WHERE kind='job_failed'").fetchone()[0] == 0


# ---------------------------------------------------------------- M5-R3-02

def test_hold_review_fail_stage_gets_only_first_half(env2):  # noqa: F811
    paths, conn = env2
    ctx = _ctx(paths, conn)
    td = _td(paths)
    for n in ("phaseA.json", "report_A.md", "report.md", "signal_card.json", "cap_result.json", "red_team.json"):
        (td / n).write_text("{}" if n.endswith(".json") else "x", encoding="utf-8")
    for stage in ("FAIL", "THESIS"):
        j = _job("HOLD_REVIEW", {"full_stage": stage, "fail_reason": "測試"})
        HoldReviewHandler().prepare(j, ctx)
        got = {f for f in _files(j.sandbox) if f.startswith("KO/")}
        assert got == {"KO/phaseA.json", "KO/report_A.md"}, stage
        vals = HoldReviewHandler().prompt_values(j, ctx, "claude_code")
        assert "signal_card.json" not in vals["files"] and "KO/report.md" not in vals["files"]
    j = _job("HOLD_REVIEW", {"full_stage": "B", "final_signal": "WATCH"}, n=3)
    HoldReviewHandler().prepare(j, ctx)
    assert {"KO/signal_card.json", "KO/report.md", "KO/cap_result.json"} <= set(_files(j.sandbox))


# ---------------------------------------------------------------- M5-RS1-03

def test_error_feedback_says_files_cleared_and_rewrite_all():
    msg = validate.error_feedback(["FULL_A.stopped：應為布林"])
    assert "已全部清除" in msg and "重新產出全部必要輸出檔" in msg and "report_A.md" in msg
    assert "不得改變" in msg   # 原有約束保留（test_fix_P09）
