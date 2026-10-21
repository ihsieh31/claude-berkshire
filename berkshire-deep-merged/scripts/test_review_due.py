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
assert check(dict(base, status="HOLDING"), [], px([100] * 119 + [74]), ASOF, 0.047)[0] == "THESIS"  # 持股單季跌 ≥25%：論點檢查（不直接完整重跑）
assert check(base, [], px([100] * 119 + [74]), ASOF, 0.047)[0] == "PRICE"             # D23：觀察名單不看單季 −25%，跌破上限不被遮蔽
d23 = check(dict(base, auto_buy_cap=70), [], px([100] * 119 + [78]), ASOF, 0.047)
assert d23[0] == "NONE", d23                                                            # D23：「自上次分析跌 ≥20%」已刪；觀察名單不看單日 ±4%
w = check(dict(base, review_price=82), [], px([82 + (i % 2) * 0.5 for i in range(119)] + [79.5]), ASOF, 0.047)
assert w[0] == "PRICE", w                                                               # 觀察名單跌破自動買進上限
h = dict(base, status="HOLDING", cost=100)
assert check(h, [], px([100] * 119 + [140]), ASOF, 0.047)[0] in ("PRICE", "THESIS")   # ≥ sell_review_above（單日 +40% 也觸發論點檢查）
c12 = check(dict(h, last_review_date="2026-09-01"), [], px([100] * 100 + [100 - 0.65 * i for i in range(1, 21)]), ASOF, 0.047)
assert c12[0] == "THESIS" and any("較成本 −12%" in w for w in c12[1]), c12  # 成本 −12%：慢跌到 87（無單日 ±4%、單季 −13%）
assert check(dict(h, last_review_date="2026-09-01", review_price=87), [], px([100] * 60 + [87] * 60), ASOF, 0.047)[0] == "NONE"  # 上次分析時已 −13%：邊緣觸發不重複（M2_D9_01）
hb = dict(base, status="HOLDING", cost=100, weight=0.04, target_weight=0.08, auto_buy_cap=95)
assert check(hb, [], px([100] * 119 + [94]), ASOF, 0.047)[0] == "THESIS"              # 單日 −6% 觸發論點檢查（較重）
assert "加碼" in " ".join(check(hb, [], px([96 - i * 0.01 for i in range(120)]), ASOF, 0.047)[1])  # 未達目標權重且 < 上限
assert check(dict(h, absurd_price_line=120, sell_review_above=None), [], px([119] * 119 + [121]), ASOF, 0.047)[0] == "PRICE"  # 荒謬級線 → PRICE
assert check(dict(h, absurd_price_line=120, sell_review_above=None, review_price=121), [], px([121] * 120), ASOF, 0.047)[0] == "NONE"  # 上次分析時已在線上：不再觸發
assert check(dict(h, absurd_price_line=120, sell_review_above=None, review_price=119), [], px([121] * 120), ASOF, 0.047)[0] == "PRICE"  # 收盤在線上但 review_price 在線下（審查看的是較早收盤）：兩者都滿足才算已成立
assert check(dict(h, sell_review_above=-33, absurd_price_line=0), [], flat, ASOF, 0.047)[0] == "NONE"  # 線 ≤0 視同缺（M2_D9_02）
assert check(base, [{"form": "10-K", "filingDate": base["last_review_date"], "items": ""}], flat, ASOF, 0.047)[0] == "FULL"  # 申報日＝上次分析日（M2_D6_04）
assert check(dict(base, grade="C", review_price=82), [], px([82] * 119 + [79.5]), ASOF, 0.047)[0] == "NONE"  # C 級觀察名單跌破上限不排 PRICE（M2_D2_04）
assert check(dict(base, grade="D"), [], flat, ASOF, 0.058)[0] == "NONE"                # C／D 級觀察名單長債變動 ≥1pp 也不排 PRICE
assert check(dict(base, next_full_review_due="2026-10-10"), [], flat, ASOF, 0.047)[0] == "FULL"  # 已到下次完整複查日（M2_D9_07）
assert check(base, [], flat, ASOF, 0.057)[0] == "PRICE"                               # 0.047→0.057 恰好 1pp
assert check(dict(base, t30_at_valuation=0.042), [], flat, ASOF, 0.052)[0] == "PRICE"  # 0.042→0.052 浮點差 0.00999…（M2_D1_07）
r = dict(base, status="REJECT", last_full_review_date="2025-01-01")
assert check(r, [{"form": "10-K", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "NONE"  # 淘汰類不例行重評
assert check(r, [{"form": "8-K", "filingDate": "2026-10-10", "items": "4.02"}], flat, ASOF, 0.047)[0] == "FULL"
assert check(dict(base, last_full_review_date="2025-10-01"), [], flat, ASOF, 0.047)[0] == "FULL"  # 滿 12 個月
# ---- P53
dd = {}
ap = check(dict(h, absurd_price_line=120, sell_review_above=None), [{"form": "10-Q", "filingDate": "2026-10-10", "items": ""}],
           px([119] * 119 + [121]), ASOF, 0.047, detail=dd)
assert ap[0] == "THESIS" and dd["also_price"] is True, (ap, dd)                         # THESIS 遮蔽 PRICE → also_price（M2_D7_01）
dd = {}
check(base, [{"form": "10-Q", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047, detail=dd)
assert dd["also_price"] is False
dd = {}
st = check(h, [], px([100] * 90, end="2026-09-15"), ASOF, 0.047, detail=dd)
assert dd["price_status"] == "stale" and st[0] == "NONE" and not any("沒有變化" in w for w in st[1]), st  # 最後收盤早 30 天（M2_D9_05）
assert st[1][0].startswith("價格缺／過期"), st
dd = {}
check(h, [], flat, ASOF, 0.047, detail=dd)
assert dd["price_status"] == "ok"
dd = {}
ff = check(base, None, flat, ASOF, 0.047, detail=dd)
assert ff[0] == "NONE" and dd["filings_ok"] is False and any("重大申報未檢查" in w for w in ff[1]), ff  # 抓取失敗不升 THESIS（M2_D9_03）
md = dict(base, status="MISSING_DATA")
assert check(md, [{"form": "10-Q", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "FULL"  # D27：缺資料檔新季報 → FULL
assert check(md, [{"form": "10-K", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "FULL"
assert check(md, [], px([100] * 119 + [70]), ASOF, 0.058)[0] == "NONE"                # 缺資料檔不看價格、利率
na = dict(base, status="NO_ACTION")
assert check(na, [{"form": "10-Q", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "NONE"  # M2-D10-03
assert check(na, [{"form": "10-K", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047)[0] == "FULL"
ir = dict(r, integrity_reject=True)
assert check(r, [{"form": "8-K", "filingDate": "2026-10-12", "items": "4.02"}], flat, ASOF, 0.047)[0] == "FULL"   # 淘汰類重大 8-K
assert check(ir, [{"form": "8-K", "filingDate": "2026-10-12", "items": "4.02"}], flat, ASOF, 0.047)[0] == "NONE"  # 誠信淘汰不重評
assert check(ir, [{"form": "8-K", "filingDate": "2026-10-12", "items": "5.02"}], flat, ASOF, 0.047)[0] == "FULL"  # 決策者異動才重評
assert check(dict(h, observe_until="2026-10-10"), [], flat, ASOF, 0.047)[0] == "FULL"   # P-50 觀察期到期（M2_D4_04）
assert check(dict(h, observe_until="2026-12-10"), [], flat, ASOF, 0.047)[0] == "NONE"   # 未到
dd = {}
check(dict(base, status="HOLDING", cost=100), [{"form": "10-Q", "filingDate": "2026-10-10", "items": ""}], flat, ASOF, 0.047,
      [{"date": "2026-10-11", "headline": "h1", "source": "s"}], detail=dd)
assert [n["headline"] for n in dd["news"]] == ["h1"]                                  # THESIS 時新聞另給（M3-D10-03）
# ---- P24（D03：荒謬級在價格回到線下之前只減一次）
ha = dict(h, absurd_price_line=120, sell_review_above=None, review_price=119, last_review_date="2026-10-01")
lt = {"date": "2026-10-01", "reason_code": "ABSURD", "line": 120, "shares": 50}
dd = {}
a1 = check(dict(ha, last_trim=lt), [], px([121] * 120), ASOF, 0.047, detail=dd)
assert a1[0] == "NONE" and any("只減一次" in u for u in dd["unchecked"]), (a1, dd)   # 減碼後一直在線上：不再觸發
a2 = check(dict(ha, last_trim=lt, review_price=125), [], px([121] * 110 + [119.5] * 5 + [121] * 5), ASOF, 0.047)
assert a2[0] == "PRICE" and any("回到線下" in w for w in a2[1]), a2                     # 曾回到線下又站上：重新觸發
assert check(dict(ha, last_trim=dict(lt, reason_code="VALUATION")), [], px([121] * 120), ASOF, 0.047)[0] == "PRICE"  # 估值類減碼不擋荒謬級
# ---- P24R（D03 邊緣觸發、回到線下證據、只認本持有期）
seq = [121] * 110 + [119.5] * 3 + [121] * 7         # 減碼 10/01 之後 10/06–10/08 跌破、10/09 起站回線上
fired = []
for k in range(3):                                   # 重新觸發後連續 3 天在線上：只有第一天觸發
    asof_k = (date.fromisoformat(ASOF) + timedelta(days=k)).isoformat()
    last_k = "2026-10-01" if k == 0 else (date.fromisoformat(asof_k) - timedelta(days=1)).isoformat()
    dk = {}
    fired.append(check(dict(ha, last_trim=lt, last_review_date=last_k, review_price=125 if k == 0 else 121), [],
                       px(seq + [121] * k, end=asof_k), asof_k, 0.047, detail=dk)[0])
    if k == 0:
        ev = dk["checked"].get("absurd_rearm")
        assert ev and ev["close"] == 119.5 and ev["date"] > "2026-10-01" and ev["line"] == 120, dk   # 證據：第一筆線下收盤
assert fired == ["PRICE", "NONE", "NONE"], fired
# 上次複查時已在線上（10/09 收盤與 review_price 都 121）：複查日之後的線下收盤才算（複查日前的跌破不再重觸發）
assert check(dict(ha, last_trim=lt, last_review_date="2026-10-09", review_price=121), [], px(seq), ASOF, 0.047)[0] == "NONE"
# P24R2：最後一個線下日（10/08）做過複查（review_price 119.5 < 線），隔天站回 → 一般邊緣觸發 PRICE，帶減碼後第一筆線下證據
for asof_k in ("2026-10-09", ASOF):
    dk = {}
    a = check(dict(ha, last_trim=lt, last_review_date="2026-10-08", review_price=119.5), [],
              px(seq[:len(seq) - (date.fromisoformat(ASOF) - date.fromisoformat(asof_k)).days], end=asof_k), asof_k, 0.047, detail=dk)
    assert a[0] == "PRICE" and not any("只減一次" in u for u in dk["unchecked"]), (asof_k, a, dk)
    assert dk["checked"]["absurd_rearm"] == {"date": "2026-10-06", "close": 119.5, "line": 120, "last_trim_date": "2026-10-01"}, dk
# 前一持有期的荒謬級減碼（早於 first_buy_date）不影響新持有期：照一般邊緣觸發
assert check(dict(ha, last_trim=lt, first_buy_date="2026-10-05"), [], px([121] * 120), ASOF, 0.047)[0] == "PRICE"
assert check(dict(ha, last_trim=lt, first_buy_date="2026-09-05"), [], px([121] * 120), ASOF, 0.047)[0] == "NONE"
# ---- RM2-7（M2-D5-01、M2-RS8-03）：狀態別過濾保留「申報未檢查」、丟掉的 triage 進 suppressed、filings_failed
for st_ in ("REJECT", "MISSING_DATA"):
    dd = {}
    rf = check(dict(base, status=st_), None, flat, ASOF, 0.047, detail=dd)
    assert rf[0] == "NONE" and dd["filings_failed"] and "申報抓取失敗：重大申報未檢查" in dd["unchecked"], (rf, dd)
    assert not any("無相關事件" in w for w in rf[1]), rf
dd = {}
check(dict(base, status="REJECT"), [{"form": "8-K", "filingDate": "2026-10-12", "items": "7.01"}], flat, ASOF, 0.047, nw, detail=dd)
assert "[TRIAGE] 8-K 2026-10-12 7.01" in dd["suppressed"] and "[TRIAGE] 新聞 2026-10-11 h1" in dd["suppressed"], dd
# ---- F3-SKb（A3-M2A-01、A3-M2A-06、A3-M2B-07）
wk = pd.DataFrame({"date": [d for d in (date(2026, 6, 1) + timedelta(days=i) for i in range(137)) if d.weekday() < 5]})
wk["close"] = [100.0 if d <= date(2026, 10, 9) else 95.5 for d in wk["date"]]
assert check(dict(h, last_review_date="2026-10-10"), [], wk, ASOF, 0.047)[0] == "THESIS"   # 週六複查：週五→週一 −4.5% 要算
for rc in ("GATE3_FAIL", "RED_LIGHT", "FRAMEWORK_HOLD"):   # FRAMEWORK_HOLD：F3-WB
    assert check(dict(base, last_reason_code=rc), [], flat, ASOF, 0.058)[0] == "NONE"      # 長債 ≥1pp 不排 PRICE
    assert check(dict(base, last_reason_code=rc, review_price=82), [], px([82] * 119 + [79.5]), ASOF, 0.047)[0] == "NONE"
print("全部通過")
