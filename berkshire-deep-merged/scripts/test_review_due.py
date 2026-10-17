#!/usr/bin/env python3
"""review_due.check() 自測（不連網）：python test_review_due.py"""
import os, sys
from datetime import date, timedelta
import pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
from review_due import check

ASOF = "2026-10-15"
def px(vals, end=ASOF):
    d0 = date.fromisoformat(end)
    return pd.DataFrame({"date": [d0 - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))], "close": vals})

base = dict(ticker="X", status="WATCH", last_review_date="2026-10-01", last_full_review_date="2026-06-01",
            review_price=100, t30_at_valuation=0.047, auto_buy_cap=80, sell_review_above=130, absurd_price_line=300)
flat = px([100 + (i % 3) * 0.5 for i in range(120)])

assert check(base, [], flat, ASOF, 0.047)[0] == "NONE"                                  # 什麼都沒變 → 不分析
assert check(base, [{"form": "10-Q", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "THESIS"
assert check(base, [{"form": "10-K", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "FULL"
assert check(base, [{"form": "10-K/A", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "THESIS"  # 10-K/A 只到 THESIS
assert check(base, [{"form": "10-K/A", "filingDate": "2026-10-10", "items": ""}, {"form": "10-K", "filingDate": "2026-10-09", "items": ""}], flat, ASOF, 0.047)[0] == "FULL"  # 原始 10-K 仍 FULL
assert check(base, [{"form": "10-K", "filingDate": "2026-09-01", "items": ""}], flat, ASOF, 0.047)[0] == "NONE"  # 上次分析前的申報不算
assert check(base, [{"form": "8-K", "filingDate": "2026-10-12", "items": "5.02,9.01"}], flat, ASOF, 0.047)[0] == "FULL"
k = check(base, [{"form": "8-K", "filingDate": "2026-10-12", "items": "7.01,9.01"}], flat, ASOF, 0.047)
assert k[0] == "NONE" and k[2] and k[2][0]["type"] == "8-K"                                # 非重大 8-K → 不升級，但列入初判
assert not check(base, [{"form": "8-K", "filingDate": "2026-10-12", "items": "5.07,9.01"}], flat, ASOF, 0.047)[2]  # 投票結果不需初判
nw = [{"date": "2026-10-11", "headline": "h1"}, {"date": "2026-09-01", "headline": "舊"}]
k = check(base, [], flat, ASOF, 0.047, nw)
assert k[0] == "NONE" and [x["headline"] for x in k[2]] == ["h1"]                           # 新聞只取上次分析後
assert check(base, [{"form": "10-K", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047, nw)[2] == []  # FULL 不需初判
assert check(dict(base, status="REJECT"), [], flat, ASOF, 0.047, nw)[2] == []               # 淘汰類不初判
assert check(base, [{"form": "10-K", "filingDate": ASOF, "items": ""}], flat, ASOF, 0.047)[0] == "NONE"  # 決策日當天申報不用
assert check(base, [], flat, ASOF, 0.058)[0] == "PRICE"                               # 利率變動 ≥1pp：只重算估值
assert check(base, [], px([100] * 119 + [74]), ASOF, 0.047)[0] == "THESIS"            # 單季跌 ≥25%：論點檢查（不直接完整重跑）
assert check(base, [], px([100] * 119 + [78]), ASOF, 0.047)[0] == "THESIS"            # 自上次分析跌 ≥20%
w = check(dict(base, review_price=82), [], px([82 + (i % 2) * 0.5 for i in range(119)] + [79.5]), ASOF, 0.047)
assert w[0] == "PRICE", w                                                               # 觀察名單跌破自動買進上限
h = dict(base, status="HOLDING", cost=100)
assert check(h, [], px([100] * 119 + [140]), ASOF, 0.047)[0] in ("PRICE", "THESIS")   # ≥ sell_review_above（單日 +40% 也觸發論點檢查）
assert check(dict(h, last_review_date="2026-09-01"), [], px([100] * 60 + [87] * 60), ASOF, 0.047)[0] == "THESIS"  # 成本 −12%
hb = dict(base, status="HOLDING", cost=100, weight=0.04, target_weight=0.08, auto_buy_cap=95)
assert check(hb, [], px([100] * 119 + [94]), ASOF, 0.047)[0] == "THESIS"              # 單日 −6% 觸發論點檢查（較重）
assert "加碼" in " ".join(check(hb, [], px([96 - i * 0.01 for i in range(120)]), ASOF, 0.047)[1])  # 未達目標權重且 < 上限
assert check(dict(h, absurd_price_line=120, sell_review_above=None), [], px([121] * 120), ASOF, 0.047)[0] == "PRICE"  # 荒謬級線 → PRICE
r = dict(base, status="REJECT", last_full_review_date="2025-01-01")
assert check(r, [{"form": "10-K", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "NONE"  # 淘汰類不例行重評
assert check(r, [{"form": "8-K", "filingDate": "2026-10-10", "items": "4.02"}], flat, ASOF, 0.047)[0] == "FULL"
assert check(dict(base, last_full_review_date="2025-10-01"), [], flat, ASOF, 0.047)[0] == "FULL"  # 滿 12 個月
print("全部通過")
