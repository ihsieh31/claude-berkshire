"""X53：D75（完整分析看得到觸發窗口新聞標題，M3-RS3-04）＋RM3-4 小瑕疵（_dq.json 空字串不換佔位）。
全部 tmp_path，不碰正式 state。"""
from __future__ import annotations

from test_fix_P32 import _ctx, _job, _td, env2  # noqa: F401  (env2 是 fixture)

from trader.jobs import deep
from trader.jobs.context import build_prompt
from trader.jobs.deep import FullAHandler, FullBHandler, trigger_text
from trader.jobs.review_jobs import ThesisHandler

NEWS = [
    {"date": "2026-10-10T12:00:00Z", "headline": "Coca-Cola to acquire 3 bottlers in Africa, sales up 8%", "source": "Reuters"},
    {"date": "2026-10-11", "headline": "KO shares jump 5% after earnings", "source": "X"},
    {"date": "2026-10-12", "headline": "Analyst raises price target to $75", "source": "Y"},
]
PARAMS = {"reasons": ["[THESIS] 新 8-K（2026-10-10）"], "news": NEWS}


def test_trigger_text_news_masked_and_price_headlines_dropped():
    t = trigger_text(PARAMS, news=True)
    assert "Coca-Cola to acquire 〔數值略〕 bottlers in Africa" in t and "2026-10-10" in t and "Reuters" in t
    assert "8%" not in t and "shares jump" not in t and "price target" not in t and "$75" not in t
    # 只有新聞、沒有 reasons 也要列
    assert "Coca-Cola" in trigger_text({"news": NEWS}, news=True)
    # 預設不附（THESIS 提示另有 {news}，不重複）；新聞全是價格標題 → 不列標題段
    assert "Coca-Cola" not in trigger_text(PARAMS)
    assert "新聞標題" not in trigger_text({"reasons": PARAMS["reasons"], "news": NEWS[1:]}, news=True)


def test_news_carried_from_dossier_to_full_chain():
    assert deep._carry(_job("DOSSIER", {"news": NEWS, "reasons": ["x"]}))["news"] == NEWS


def test_full_a_and_full_b_prompts_show_news_thesis_not_duplicated(env2):  # noqa: F811
    paths, conn = env2
    ctx = _ctx(paths, conn)
    td = _td(paths)
    (td / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    j = _job("FULL_A", dict(PARAMS))
    h = FullAHandler()
    h.prepare(j, ctx)
    prompt = build_prompt(j, "claude_code", 10 ** 9, paths.root, None, values=h.prompt_values(j, ctx, "claude_code"))
    assert "Coca-Cola to acquire 〔數值略〕 bottlers" in prompt and "8%" not in prompt and "price target" not in prompt
    vals = FullBHandler().prompt_values(_job("FULL_B", dict(PARAMS)), ctx, "claude_code")
    assert "Coca-Cola to acquire" in vals["trigger"]
    j = _job("THESIS", dict(PARAMS))
    h = ThesisHandler()
    h.prepare(j, ctx)
    prompt = build_prompt(j, "claude_code", 10 ** 9, paths.root, None, values=h.prompt_values(j, ctx, "claude_code"))
    assert prompt.count("Coca-Cola to acquire") == 1


def test_sanitize_dq_empty_string_not_replaced_by_placeholder():
    out, n = deep._sanitize_dq({"dq_checks": [{"name": "W1", "warning": "", "result": "  "}], "missing": [""]})
    assert out["dq_checks"][0]["warning"] == "" and out["dq_checks"][0]["result"] == "  " and out["missing"] == [""]
    assert n == 0 and "框架已刪" not in str(out)


# ---------------------------------------------------------------- check-X53 返工（RM5-5）：FULL_A 價格盲濾網

PRICEY_X53 = ["Apple downgraded to Sell at Morgan Stanley", "Morgan Stanley upgraded Apple to Buy",
              "Apple slumps after iPhone sales disappoint", "Apple soars", "Apple sinks 7%", "Apple rallies 5%",
              "Apple tanks on DOJ suit", "Apple's selloff deepens", "Apple's worst day since 2020",
              "Apple up 12% year to date", "Apple market value tops $3 trillion", "Apple sheds $200 billion in value",
              "Apple trades at 30 times earnings", "Apple outperforms S&P 500", "Apple yields 0.5%", "Why Apple is a buy right now"]


def test_full_a_drops_rating_move_valuation_headlines_keeps_fundamentals(env2):  # noqa: F811
    paths, conn = env2
    ctx = _ctx(paths, conn)
    (_td(paths) / "dossier" / "KO.md").write_text("# KO\n", encoding="utf-8")
    news = [{"date": "2026-10-05", "headline": h, "source": "s"} for h in PRICEY_X53 + ["Apple revenue rises 6% to $90 billion"]]
    j = _job("FULL_A", {"news": news})
    h = FullAHandler()
    h.prepare(j, ctx)
    prompt = build_prompt(j, "claude_code", 10 ** 9, paths.root, None, values=h.prompt_values(j, ctx, "claude_code"))
    for k in ("downgraded", "upgraded", "slumps", "market value", "times earnings", "soars", "year to date", "outperforms",
              "is a buy", "worst day", "selloff", "yields 〔"):
        assert k not in prompt, k
    assert "Apple revenue rises 〔數值略〕" in prompt
