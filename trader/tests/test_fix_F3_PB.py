"""F3-PB：價格盲文字遮蔽（V3-2 fix_group G6-price-blind-text：A3-M5B-04、A3-M5D-01、A3-M5D-02、A3-M6D-04）。
每個測試修前失敗、修後通過。不改 analysis/writeback.py：回寫端的原文比對與 H2 增補寫法不動，修在讀取／提示端。"""
from __future__ import annotations

import json

import test_review_jobs as TR
from trader.analysis import dossier_prep as DP
from trader.analysis import writeback as W
from trader.jobs import deep as D
from trader.jobs import review_jobs as RJ
from trader.jobs.review_jobs import ThesisHandler, TriageHandler

env = TR.env   # (paths, conn)
ASOF = TR.ASOF


# ---------------------------------------------------------------- A3-M5B-04：TRIAGE 提示新聞標題價格盲

PRICEY = ["Coca-Cola shares slump after earnings", "KO stock is a buy, says analyst", "可口可樂股價重挫",
          "Coke upgraded to Buy at Goldman"]


def _triage_job(items):
    return TR._job("TRIAGE", TR._price_params(triage=items, kill_criteria=["毛利率連兩季下滑"], base_action="NONE",
                                              thesis_file=None))


def test_triage_items_blind_filtered(env):
    paths, conn = env
    items = [{"type": "news", "date": "2026-10-10", "headline": h, "source": "Reuters"} for h in PRICEY]
    items.append({"type": "news", "date": "2026-10-11", "headline": "Coca-Cola signs bottler deal", "source": "WSJ"})
    job = _triage_job(items)
    ctx = TR._ctx(paths, conn)
    h = TriageHandler()
    h.pre_script(job, ctx)
    h.prepare(job, ctx)
    txt = h.prompt_values(job, ctx, "claude_code")["triage_items"]
    for bad in ("slump", "a buy", "股價", "upgraded", "Goldman"):
        assert bad not in txt, bad
    shown = json.loads(txt.strip("`").removeprefix("json"))
    assert len(shown) == 5                                       # 保留項數：next_jobs 不誤判漏答
    assert sum(x["headline"] == RJ.TRIAGE_HEADLINE_OMITTED for x in shown) == 4
    assert "Coca-Cola signs bottler deal" in txt
    assert job.params["triage"][0]["headline"] == PRICEY[0]      # 參數不改（THESIS 的 _news_text 另濾）


def test_blind_drop_lives_in_dossier_prep():
    """層次：dossier_prep.is_price_headline 不再反向匯入 jobs.deep；deep 沿用同一函式。"""
    import inspect
    assert "trader.jobs" not in inspect.getsource(DP.is_price_headline)
    assert D._blind_drop is DP._blind_drop
    assert DP.is_price_headline("Coca-Cola shares slump after earnings", blind=True)
    assert not DP.is_price_headline("Coca-Cola revenue jumped 5% on volume", blind=True)


# ---------------------------------------------------------------- A3-M5D-01：trigger_text 略過未檢查註記

REVIEW_DUE_NOTES = [   # review_due.py 的真實句型（notes = [f"（{u}）" for u in unchecked]）
    "（荒謬級線：2026-09-01 已依荒謬級減碼，之後（上次減碼／分析日後）未回到線下，不再觸發（D03 只減一次））",
    "（等級 C：觀察名單跌破上限不排 PRICE（只有 A／B 級有自動買進上限））",
    "（等級 C：觀察名單長債變動不排 PRICE（只有 A／B 級有自動買進上限））",
    "（登錄表缺 auto_buy_cap（或 ≤0）：觀察名單跌破上限未檢查）",
    "（最後收盤 2026-10-01 早於決策日 9 個交易日（過期）：價格類觸發未檢查）",
    "（上次完整分析閘門 3 問卷不過（GATE3_FAIL）：現價 95.00 < 上限 100 不排 PRICE，待 next_full_review_due 重跑完整分析（M2-RS2-03））",
]


def test_trigger_text_drops_unchecked_notes():
    rs = ["[THESIS] 新季報 10-Q 2026-09-30：論點檢查", *REVIEW_DUE_NOTES, "（未提供新聞：新聞類初判未檢查）"]
    out = D.trigger_text({"reasons": rs})
    for bad in ("荒謬", "跌破上限", "長債", "收盤", "auto_buy_cap", "GATE3_FAIL", "細節含數字"):
        assert bad not in out, bad
    assert "新季報 10-Q 2026-09-30" in out
    assert "未提供新聞" in out                      # 與價格無關的證據完整度註記保留（不誤殺）
    full_b = D.trigger_text({"reasons": rs}, price_blind=False)   # 看得到價格的 FULL_B 照舊
    assert "荒謬級線" in full_b and "等級 C" in full_b


def test_thesis_prompt_trigger_has_no_unchecked_notes(env):
    paths, conn = env
    job = TR._job("THESIS", TR._price_params(last_review_date="2026-01-01", kill_criteria=["毛利率連兩季下滑"],
                                             reasons=["[THESIS] 新季報 10-Q 2026-09-30：論點檢查", *REVIEW_DUE_NOTES[:2]]))
    ctx = TR._ctx(paths, conn)
    h = ThesisHandler()
    h.pre_script(job, ctx)
    h.prepare(job, ctx)
    trig = h.prompt_values(job, ctx, "claude_code")["trigger"]
    assert "荒謬級線" not in trig and "跌破上限" not in trig and "新季報" in trig


# ---------------------------------------------------------------- A3-M5D-02：失效條件依序號對應回原文

KC = ["毛利率連兩季低於 30%。若股價跌破 $50 則重新評估。",   # 價格盲版只留前句
      "股價跌破 $50 且一年未回升",                          # 整條刪
      "市佔率連三年下滑"]


def _kc_due(due="2026-10-10"):
    return [{"text": t, "due": due} for t in KC]


def test_kill_due_matches_by_index(env):
    paths, conn = env
    shown = RJ._kill_criteria({"kill_criteria": KC})
    assert shown[0] != KC[0] and shown[1] == "（含價格數字，已刪）" and shown[2] == KC[2]
    TR._seed_registry(conn, "KO", "HOLDING", {"cost": 50.0, "kill_criteria_due": _kc_due()})
    status = [{"criterion": shown[0], "status": "接近", "evidence": "10-Q", "next_due": "2027-01-31"},
              {"criterion": shown[1], "status": "無資料", "evidence": "價格條件已刪"},
              {"criterion": "K3：市佔率連三年下滑", "status": "未觸發", "evidence": "10-Q"}]
    res = {**TR.thesis_result("疑似"), "kill_criteria_status": status}
    job = TR._job("THESIS", TR._price_params(kill_criteria=KC, kill_criteria_due=_kc_due(), holding=True))
    ThesisHandler().post(job, res, TR._ctx(paths, conn))
    kc = {x["text"]: x for x in TR._reg(conn)["data"]["kill_criteria_due"]}
    assert kc[KC[0]]["checked_on"] == ASOF and kc[KC[0]]["status"] == "接近" and kc[KC[0]]["due"] == "2027-01-31"
    assert kc[KC[2]]["checked_on"] == ASOF and kc[KC[2]]["status"] == "未觸發"
    assert "checked_on" not in kc[KC[1]]            # THESIS 沒看到的價格型條目不記為已檢查


def test_map_kill_status_positional_and_untouched_input():
    p = {"kill_criteria": KC}
    ks = [{"criterion": "毛利率連兩季低於三成", "status": "未觸發"}, {"criterion": "x", "status": "無資料"},
          {"criterion": "市佔率連三年下滑", "status": "未觸發"}]
    res = {"verdict": "完好", "kill_criteria_status": ks}
    out = RJ.map_kill_status(res, p)
    assert [k["criterion"] for k in out["kill_criteria_status"]] == [KC[0], "x", KC[2]]
    assert ks[0]["criterion"] == "毛利率連兩季低於三成"
    # 條數不同：不依位置猜
    out2 = RJ.map_kill_status({"kill_criteria_status": ks[:1]}, p)
    assert out2["kill_criteria_status"][0]["criterion"] == "毛利率連兩季低於三成"


def test_price_kill_due_hands_to_price(env):
    paths, conn = env
    TR._write_cap_input(paths)
    TR._seed_registry(conn, "KO", "HOLDING", {"cost": 50.0, "kill_criteria_due": _kc_due()})
    reasons = [f"[THESIS] 失效條件到期檢查（due 2026-10-10）：{KC[1]}"]
    job = TR._job("THESIS", TR._price_params(kill_criteria=KC, kill_criteria_due=_kc_due(), holding=True,
                                             reasons=reasons))
    res = {**TR.thesis_result("完好"),
           "kill_criteria_status": [{"criterion": c, "status": "未觸發", "evidence": "10-Q"}
                                    for c in RJ._kill_criteria({"kill_criteria": KC})]}
    nxt = ThesisHandler().post(job, res, TR._ctx(paths, conn))
    assert [j.job_type for j in nxt] == ["PRICE"]
    # 到期的不是價格型條目 → 照舊不續接
    job2 = TR._job("THESIS", TR._price_params(kill_criteria=KC, holding=True,
                                              reasons=[f"[THESIS] 失效條件到期檢查（due 2026-10-10）：{KC[2]}"]), n=2)
    assert ThesisHandler().post(job2, res, TR._ctx(paths, conn)) == []


# ---------------------------------------------------------------- A3-M6D-04：H2 增補白名單

H2_RESULT = {"action": "TRIM", "sell_category": "估值", "stop_adding": True, "observe_until": "2027-04-14",
             "falsified_assumption": "無", "reason": "G8：本檔 self 0.021 vs best_addable 0.083，高出 6.2 pp",
             "path": [{"q": "Q2", "answer": "否", "evidence": "業務與論點一致"},
                      {"q": "Q3", "answer": "是", "evidence": "正常化盈餘倍數 52 倍，遠高於歷史"},
                      {"q": "Q3a", "answer": "是", "evidence": "較成本已上漲 85%"},
                      {"q": "Q3b", "answer": "是", "evidence": "EV/EBIT 約 38 倍、盈餘收益率 1.9%"},
                      {"q": "Q5", "answer": "是", "evidence": "self 0.021 vs best_addable 0.083"}]}
LEAKS = ("6.2 pp", "best_addable", "52 倍", "85%", "EV/EBIT", "1.9%", "類別 估值", "0.021")


def _seed_h2(paths):
    paths.theses.mkdir(parents=True, exist_ok=True)
    (paths.theses / "KO_2026-01-01.md").write_text("# 論點\n護城河：品牌。\n", encoding="utf-8")
    (paths.theses / "KO_2026-01-01_addendum_1.md").write_text(W._hold_review_text("KO", "2026-10-14", H2_RESULT),
                                                              encoding="utf-8")


def test_h2_addendum_whitelist(env, tmp_path):
    paths, conn = env
    _seed_h2(paths)
    for jt in ("THESIS", "TRIAGE"):
        job = TR._job(jt, TR._price_params())
        RJ._stage_thesis(tmp_path / jt, job, paths)
        txt = (tmp_path / jt / "thesis" / "KO_2026-01-01_addendum_1.md").read_text(encoding="utf-8")
        for bad in LEAKS:
            assert bad not in txt, (jt, bad)
        assert "動作：TRIM" in txt and "停止加碼 是" in txt and "觀察至 2027-04-14" in txt
        assert "Q2：否（業務與論點一致）" in txt and "被否定的假設：無" in txt
        assert "護城河：品牌" in (tmp_path / jt / "thesis" / "KO_2026-01-01.md").read_text(encoding="utf-8")


def test_h2_whitelist_only_for_price_blind_jobs(env, tmp_path):
    """看得到價格的 HOLD_REVIEW 不套白名單（不誤殺），H2 以外的檔原樣。"""
    paths, conn = env
    _seed_h2(paths)
    RJ._stage_thesis(tmp_path, TR._job("HOLD_REVIEW", TR._price_params()), paths)
    assert "Q3" in (tmp_path / "thesis" / "KO_2026-01-01_addendum_1.md").read_text(encoding="utf-8")
    assert RJ.h2_whitelist("# KO 論點\n- 理由：x\n") == "# KO 論點\n- 理由：x\n"
