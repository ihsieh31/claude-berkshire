#!/usr/bin/env python3
"""兩週例行檢查：判斷登錄表中每一檔「這次要不要重新分析、做到多深」。論點沒動 → 不分析，沿用上次結論。

  SEC_USER_AGENT="名稱 email" python review_due.py --registry state/registry.json --asof 2026-10-15 \
      [--prices daily.csv] [--t30 0.047] [--news news.json] [--offline filings.json] [--out due.json]

本腳本不呼叫任何模型，只輸出訊號檔；由外部框架讀訊號、依 ../../routing/model_routing.json 調用模型。
各觸發的操作定義（92 曆日視窗、重大 8-K 項目碼、10-K/A 分級、20-F／40-F、triage 20 筆）見
../references/procedure/07_conclusion_execution.md §7 與 ../references/inferred_thresholds.md（review_due_triggers 等鍵）。

- registry.json：深度分析每次結束時回寫（格式見 ../assets/registry_schema.json）。
- daily.csv：ticker,date,close（近 100 個交易日以上的日收盤）；缺 → 以 Yahoo 日線抓（非官方）。
- 申報：SEC submissions（10-K、10-Q、8-K 與 8-K 項目碼），只看「上次分析日 ≤ 申報日 < 決策日」者（含上次分析日當天：
  上次分析只用申報日 < 當時決策日的文件，當天申報者上次未讀，本次要算；M2_D6_04、M2-R5-04）。
  登錄表列可另帶 filings_since（框架在申報抓取失敗後恢復時給）：只把申報窗口起點放寬到它，其他觸發仍以
  last_review_date 為基準（A3-M2E-03）。
- news.json（選用，由外部框架提供）：{ticker: [{"date","headline","source","url"}]}；窗口同申報（上次分析日 ≤ 日期 < 決策日）。
- --offline：直接給 {ticker: [{"form","filingDate","items"}]}，不連網；某檔的值為 null ＝該檔申報抓取失敗
  （照 filings=None 處理；缺鍵＝沒有申報）。

輸出每檔一個 action（由重到輕；同一檔取最重者）：
  FULL      完整重跑深度分析（G0–G11）       新年報；滿 12 個月；已到登錄表 next_full_review_due；重大 8-K；
                                               持股 observe_until（P-50 觀察期）到期；持股權重 >25%（P-43；上次完整
                                               分析時權重已 >25% 者不再觸發）
  THESIS    論點破裂檢查（H1 輕複查＋H2 Q2）  新季報；失效條件到期；持股：成本 −12%、單日 ±4%、單季自高點跌 ≥25%
                                               （跌價只觸發論點檢查；觀察名單只看跌破上限 → PRICE 與申報，D23）
                                               延遲申報通知（NT 10-K／NT 10-Q／NT 20-F 及 /A；W14 延遲申報→W，補齊前只觀察）
  PRICE     只重跑 G7–G11（P-63 快速通道）     觀察名單（A／B 級）現價 < 自動買進上限；持股未達目標權重且現價 < 上限（加碼檢查）；
                                               持股現價 ≥ sell_review_above（賣出檢視）或 ≥ 荒謬級線；長債較上次估值變動 ≥1pp
                                               （只影響估值；C／D 級觀察名單不排）
  NONE      不分析，沿用上次結論
狀態型觸發（失效條件到期、成本 −12%、兩條價格線、單季高點）為邊緣觸發：上次分析時已成立者不再觸發（見 check()）。
  注意：PRICE 若與 THESIS 同期成立，action 只回 THESIS（[PRICE] 理由仍在 reasons），並輸出 also_price=true：
  框架在 THESIS 判完好後續接 PRICE（M2_D7_01），否則被遮蔽的買點／賣出檢視／荒謬級檢查會漏掉。
價格線、上限 ≤0 視同缺。每列另輸出 suppressed（P-62 等壓掉的理由）、unchecked（欄位或輸入缺而未檢查的觸發）、
checked（判定所用的值）、price_status（ok｜missing｜stale：最後收盤早於決策日超過 5 個交易日 → 價格類觸發不檢查）、
filings_ok／filings_failed（申報清單抓取成功與否；失敗時只略過申報類觸發——新年報、新季報、8-K——不升級 action，
標「重大申報未檢查」；價格類、滿 12 個月、next_full_review_due、失效條件等其餘觸發照查（M2-RS8-03）。
連續失敗次數本腳本不記（無狀態），由框架依 filings_failed 累計）、
news（窗口內新聞標題，供 THESIS 提示；M3-D10-03；標題可能含股價或漲跌幅，交給價格盲工作前須先濾價格標題）、
check_error（單檔 check() 例外時的訊息；其餘為 null。該檔 action＝NONE、price_status＝unchecked、unchecked 標「全部觸發未檢查」，
整批照常輸出；框架須對 check_error 非空者開異常，不得當成「沒有變化」；A3-M2E-02）。
觀察名單上次完整分析停在 GATE3_FAIL 或 RED_LIGHT：跌破上限與長債 ≥1pp 都不排 PRICE（PRICE 不能升成 BUY），記入 unchecked，
等 next_full_review_due 的完整分析（D24；A3-M2A-06、A3-M2B-07）。

另輸出 triage（初判清單）：腳本無法判斷輕重的事件——非重大項目碼的 8-K、新聞標題。只在 action 為 PRICE／NONE 時列出
（FULL／THESIS 本來就會讀全部新事實）。框架規則：triage 非空 → 先做 TRIAGE 初判；任一項回答「是」或「不確定」→ action 升為 THESIS。
狀態別（D27、M2-D10-03）：
  MISSING_DATA  只看新年報／新季報 → FULL（資料可能補齊）；其餘觸發不看
  NO_ACTION     只保留 FULL 類理由
  TOO_HARD／REJECT  淘汰類不例行重評（P-62）：只有重大 8-K 才 FULL；integrity_reject（NEVER-04(a) 誠信淘汰）
                在現任決策者在位期間不重評，只有 8-K 5.02（董事或高階經理人異動）才 FULL
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, timedelta

import numpy as np
import pandas as pd

UA = os.environ.get("SEC_USER_AGENT", "")
MATERIAL_8K = {"1.03": "破產或接管", "2.06": "重大減損", "4.01": "更換會計師", "4.02": "財報不可依賴（重編）",
               "5.02": "董事或高階經理人異動", "2.01": "完成重大收購或處分", "1.01": "重大合約", "3.01": "下市或不符上市規定"}
IGNORE_8K = {"9.01", "5.07"}  # 只附展品、股東會投票結果：不需初判
RANK = {"NONE": 0, "PRICE": 1, "THESIS": 2, "FULL": 3}
SIGNAL_VERSION = "review-due-v2"
# 上次分析日（含）前最後一筆收盤早於上次分析日超過此日曆日數，視為資料缺口、不當上次分析時的價格（M2_D9_01）。
# 上次分析日應是交易日；5 個日曆日容得下週末＋一個休市日（例如週一休市後的週二分析，最後收盤是上週五＝4 天），
# 超過就表示缺了好幾個交易日的資料。
STALE_DAYS = 5
# 最後收盤早於決策日超過此交易日數（只扣週末，不扣假日；推論）→ 價格過期，價格類觸發不檢查（M2_D9_05）
STALE_PRICE_TRADING_DAYS = 5
WEIGHT_FULL_REVIEW = 0.25   # P-43：單檔市值權重 >25% → 完整複查（M6-D4-03；設計決策 D01）
# 觀察名單上次完整分析的停止碼：PRICE 快速通道不得把它升成 BUY → 跌破上限、長債變動都不排 PRICE，只等完整分析重跑
# （GATE3_FAIL：D24；RED_LIGHT：紅燈延後，下個決策週期以反方重跑 G9，deep._red_light「PRICE 不得升買」；A3-M2A-06、A3-M2B-07）
NO_PRICE_UPGRADE = {"GATE3_FAIL": "閘門 3 問卷不過", "RED_LIGHT": "紅隊致命紅燈",
                    "FRAMEWORK_HOLD": "FULL_B 判 BUY 被框架降為觀察"}   # FRAMEWORK_HOLD：A3-M5B-01（F3-OO），PRICE 不得升成買進


def _get(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as r:
        time.sleep(0.21)
        return json.loads(r.read().decode("utf-8"))


def sec_filings(cik, ua):
    d = _get(f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json", ua)["filings"]["recent"]
    n = len(d["form"]); c = int(cik)
    acc, doc, items = d.get("accessionNumber", [""] * n), d.get("primaryDocument", [""] * n), d.get("items", [""] * n)
    return [{"form": d["form"][i], "filingDate": d["filingDate"][i], "items": items[i],
             "url": f"https://www.sec.gov/Archives/edgar/data/{c}/{acc[i].replace('-', '')}/{doc[i]}" if acc[i] and doc[i] else ""}
            for i in range(n)]


def yahoo_daily(tk):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{tk.replace('.', '-')}?range=6mo&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research script)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]
    return pd.DataFrame({"ticker": tk, "date": pd.to_datetime(d["timestamp"], unit="s").date,
                         "close": d["indicators"]["quote"][0]["close"]}).dropna()


def _is_ymd(v):
    try:
        date.fromisoformat(str(v)[:10])
        return isinstance(v, str)
    except ValueError:
        return False


def _pos(v):
    """價格線、上限 ≤0 或非數字 → None（視同缺；M2_D9_02：負 IV 寫成的線不得讓「現價 ≥ 線」恆真）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def check(e, filings, px, asof, t30, news=None, detail=None):
    """回傳 (action, reasons, triage)。e：登錄表一列；filings：該檔申報清單（None＝抓取失敗：申報觸發未檢查，
    不因此升級；M2_D9_03）；px：該檔日收盤（date 升冪，≤ asof）；news：該檔新聞 [{"date","headline","source","url"}]（選用）。
    detail（選用 dict）：填入 suppressed（依狀態別壓掉的理由）、unchecked（因欄位或輸入缺而未檢查的觸發）、
    checked（判定所用的值）、price_status、filings_ok、also_price、news，只供輸出與框架續接，不影響 action。

    狀態型觸發一律「邊緣觸發」（M2_D9_01）：以上次分析時的狀態為基準，上次分析時已成立者不再觸發——
    失效條件只看 last < due ≤ asof；成本 −12%、≥ sell_review_above、≥ 荒謬級線只在「上次分析時不滿足、現價滿足」時觸發；
    單季高點只取上次分析日（含）之後的收盤。
    荒謬級線另看登錄表 last_trim（設計決策 D03：回到線下之前只減一次；只認本持有期＝date ≥ first_buy_date 的減碼）：
    線＝last_trim.line（缺則現行線）。上次減碼後沒有任何線下收盤，或上次分析時已在線上且上次分析日之後沒有線下收盤
    → 不觸發；上次分析時已在線上、之後又有線下收盤 → 現價在線上即觸發；上次分析時在線下 → 照一般邊緣觸發。
    (上次減碼日, 決策日] 的第一筆線下收盤當證據輸出為 absurd_rearm（經觸發參數帶進 PRICE／HOLD_REVIEW，回寫端認同一份證據）。
    「上次分析時已滿足」＝上次分析日（含）前最後一筆收盤與 review_price 兩者都滿足（缺一者只看另一者；都缺視為不滿足）；
    該收盤早於上次分析日超過 STALE_DAYS 個日曆日（資料缺口）時不用，只看 review_price。要兩者都滿足，是為了防
    「複查時決策日收盤尚未入檔、審查看到的是較早收盤」：那時 review_price 是審查實際用的價格，與入檔後的收盤不同時
    寧可多觸發一次，也不漏掉。"""
    last = e.get("last_review_date") or "1900-01-01"
    full_last = e.get("last_full_review_date") or last
    status = e.get("status", "WATCH")
    reasons, triage, unchecked, news_hits = [], [], [], []
    checked = {"t30": t30, "t30_at_valuation": e.get("t30_at_valuation"), "review_price": e.get("review_price")}

    def add(level, why):
        reasons.append((level, why))
        return reasons[-1]

    filings_ok = filings is not None
    if not filings_ok:
        unchecked.append("申報抓取失敗：重大申報未檢查")
    # 申報日＝上次分析日的文件不在上次分析內（上次只用 filed < asof），本次要算（M2_D6_04）
    # filings_since（選填，框架給）：申報抓取失敗後恢復時只放寬申報窗口，last_review_date 與其他觸發基準不動（A3-M2E-03）
    fs = e.get("filings_since")
    lo = fs[:10] if _is_ymd(fs) and fs[:10] < last else last
    new = [f for f in filings or [] if lo <= f["filingDate"] < asof]

    def forms(fs):
        return "、".join(f"{f['form']} {f['filingDate']}" for f in fs)

    ann = [f for f in new if f["form"] in ("10-K", "20-F", "40-F")]
    r_filed = []      # 新年報／新季報的理由（MISSING_DATA 只看這兩種；D27）
    r_8k = []         # 重大 8-K 的 (理由, 項目碼)（淘汰類只看這些；P-62）
    if ann:
        r_filed.append(add("FULL", f"上次分析後有新年報（{forms(ann)}）"))
    # 10-K/A 多半只補封面或 Part III：只到 THESIS（原始 10-K 仍是 FULL）
    qs = [f for f in new if f["form"] in ("10-Q", "10-Q/A", "10-K/A")]
    if qs:
        r_filed.append(add("THESIS", f"上次分析後有新季報（{forms(qs)}）"))
    # 延遲申報通知（NT 10-K／NT 10-Q 等）：規則書 W14＝延遲申報→W，補齊前只觀察；不算新年報／新季報（M4-R4S4-02）
    nts = [f for f in new if f["form"] in ("NT 10-K", "NT 10-K/A", "NT 10-Q", "NT 10-Q/A", "NT 20-F", "NT 20-F/A")]
    if nts:
        add("THESIS", f"上次分析後有延遲申報通知（{forms(nts)}；W14 補齊前只觀察）")
    for f in new:
        if f["form"].startswith("8-K"):
            hit = [MATERIAL_8K[i.strip()] for i in str(f.get("items") or "").split(",") if i.strip() in MATERIAL_8K]
            codes = {i.strip() for i in str(f.get("items") or "").split(",") if i.strip()}
            if hit:
                r_8k.append((add("FULL", f"8-K（{f['filingDate']}）：" + "、".join(hit)), codes))
            elif codes - IGNORE_8K or not codes:
                triage.append({"type": "8-K", "date": f["filingDate"], "items": ",".join(sorted(codes)), "url": f.get("url", "")})
    for n in news or []:
        if last <= str(n.get("date", ""))[:10] < asof:
            item = {"type": "news", "date": str(n["date"])[:10], "headline": n.get("headline", ""),
                    "source": n.get("source", ""), "url": n.get("url", "")}
            triage.append(item)
            news_hits.append(item)
    if (date.fromisoformat(asof) - date.fromisoformat(full_last)).days >= 365:
        add("FULL", f"距上次完整分析（{full_last}）已滿 12 個月")
    nf = str(e.get("next_full_review_due") or "")[:10]
    if nf and full_last < nf <= asof:   # 模型寫的較早完整複查日（M2_D9_07）
        add("FULL", f"已到下次完整複查日（next_full_review_due {nf}）")
    ou = str(e.get("observe_until") or "")[:10]
    if status == "HOLDING" and ou and full_last < ou <= asof:   # P-50 觀察期到期必須結案（M2_D4_04）
        add("FULL", f"P-50 觀察期到期（observe_until {ou}）：必須結案")
    w, w0 = e.get("weight"), e.get("full_review_weight")   # w0：上次完整分析時的權重（框架回寫時記）
    checked.update({"weight": w, "full_review_weight": w0})
    if status == "HOLDING" and isinstance(w, (int, float)) and w > WEIGHT_FULL_REVIEW \
            and not (isinstance(w0, (int, float)) and w0 > WEIGHT_FULL_REVIEW):   # 邊緣觸發，不每天重排
        add("FULL", f"單檔權重 {w:.1%} > {WEIGHT_FULL_REVIEW:.0%}（P-43）：完整複查（因上漲超限不賣，設計決策 D01）")
    if status == "WATCH" and e.get("grade") not in (None, "A", "B"):   # C／D 級跑 PRICE 必過不了閘門 2（M2_D2_04）
        unchecked.append(f"等級 {e.get('grade')}：觀察名單長債變動不排 PRICE（只有 A／B 級有自動買進上限）")
    elif t30 is None:
        unchecked.append("未提供 t30：長債觸發未檢查")
    elif e.get("t30_at_valuation") is None:
        unchecked.append("登錄表缺 t30_at_valuation：長債觸發未檢查")
    elif round(abs(t30 - e["t30_at_valuation"]), 6) >= 0.01:   # 恰好 1pp 不因浮點誤差漏掉（M2_D1_07）
        if status == "WATCH" and e.get("last_reason_code") in NO_PRICE_UPGRADE:
            # PRICE 不能把 GATE3_FAIL／RED_LIGHT 升成 BUY（D24、紅燈延後），排了只會白跑一次（A3-M2A-06）
            unchecked.append(f"上次完整分析 {e.get('last_reason_code')}（{NO_PRICE_UPGRADE[e.get('last_reason_code')]}）："
                             f"長債 {e['t30_at_valuation']:.2%} → {t30:.2%} 變動 ≥1pp 不排 PRICE，待完整分析重跑")
        else:
            add("PRICE", f"30 年期殖利率較上次估值變動 ≥1pp（{e['t30_at_valuation']:.2%} → {t30:.2%}）")
    for kc in e.get("kill_criteria_due", []):
        due = str(kc.get("due") or "")[:10]
        if due and last < due <= asof:
            add("THESIS", f"失效條件到期檢查（due {due}）：{kc.get('text', '')}")

    price_status = "missing"
    if px is None or not len(px):
        unchecked.append("無價格資料：價格類觸發未檢查")
    else:
        lag = int(np.busday_count(np.datetime64(str(px["date"].iloc[-1])[:10]), np.datetime64(asof)))
        checked.update({"last_close": float(px["close"].iloc[-1]), "last_close_date": str(px["date"].iloc[-1])})
        if lag > STALE_PRICE_TRADING_DAYS:   # 下市／停牌／資料中斷：不拿舊價判斷（M2_D9_05）
            price_status = "stale"
            unchecked.append(f"最後收盤 {px['date'].iloc[-1]} 早於決策日 {lag} 個交易日（過期）：價格類觸發未檢查")
        else:
            price_status = "ok"
    if price_status == "ok":
        p, p_date = float(px["close"].iloc[-1]), px["date"].iloc[-1]
        last_d = date.fromisoformat(last)
        before = px[px["date"] <= last_d]
        c0 = c0_date = c0_d = None
        if len(before) and (last_d - before["date"].iloc[-1]).days <= STALE_DAYS:   # 太舊＝資料缺口，不用
            c0_d = before["date"].iloc[-1]
            c0, c0_date = float(before["close"].iloc[-1]), str(c0_d)
        p0s = [v for v in (c0, _pos(e.get("review_price"))) if v is not None]   # 上次分析時的價格（收盤、review_price）

        def was(cond):   # 上次分析時已成立：可用的基準價都滿足（都缺 → 不成立）
            return bool(p0s) and all(cond(v) for v in p0s)

        checked.update({"last_close": p, "last_close_date": str(p_date), "close_at_last_review": c0,
                        "close_at_last_review_date": c0_date})
        if status == "HOLDING":
            # 單季自高點 −25% 只對持股（D23：觀察名單只留跌破上限 → PRICE 與申報觸發）
            q = px[px["date"] >= max(date.fromisoformat(asof) - timedelta(days=92), last_d)].reset_index(drop=True)
            if len(q) and q["close"].max() > 0 and p / q["close"].max() - 1 <= -0.25:
                i = int(q["close"].idxmax())
                add("THESIS", f"單季自高點跌 ≥25%（{q['date'][i]} 高點 {q['close'][i]:.2f} → {p_date} {p:.2f}，"
                              f"{p / q['close'][i] - 1:.1%}；查新事實；跌價本身不是賣出或調高價值的理由）")
            # 單日 ±4% 只對持股（D23；P-55 屬 H1 持有複核）；含「上次分析日 → 次一交易日」（M2_D1_07）。
            # 起點用上次分析日（含）前最後一筆收盤（c0_date）：上次分析日是週末／休市日時，「分析前最後收盤 → 分析後
            # 第一個交易日」那一天也要算（A3-M2A-01）；c0 因資料缺口不用時退回上次分析日
            r = px[px["date"] >= (c0_d if c0_d is not None else last_d)].reset_index(drop=True)
            ch = r["close"].pct_change().iloc[1:]
            big = ch[ch.abs().round(6) >= 0.04]
            if len(big):
                j = int(big.abs().idxmax())
                add("THESIS", f"上次分析後出現單日 ±4%（{len(big)} 日；最大 {r['date'][j]} {ch[j]:+.1%}）")
            cost = _pos(e.get("cost"))
            checked["cost"] = cost
            if cost is None:
                unchecked.append("登錄表缺 cost：成本 −12% 未檢查")
            elif p / cost - 1 <= -0.12 and not was(lambda v: v / cost - 1 <= -0.12):
                add("THESIS", f"較成本 −12%（成本 {cost:.2f} → {p_date} {p:.2f}，{p / cost - 1:.1%}；論點破裂檢查，不是停損）")
            for key, name, lv_why in (("sell_review_above", "賣出檢視線（sell_review_above）", "H2 Q3 賣出檢視"),
                                      ("absurd_price_line", "荒謬級線", "正常化 P/E ≥50：荒謬級減碼檢查（是否減、減多少由 HOLD_REVIEW 決定，D03）")):
                line = _pos(e.get(key))
                checked[key] = line
                if line is None:
                    unchecked.append(f"登錄表缺 {key}（或 ≤0）：{name}觸發未檢查")
                    continue
                # 荒謬級在價格回到線下之前只減一次（設計決策 D03）：只認本持有期的減碼
                lt = e.get("last_trim") if key == "absurd_price_line" else None
                fb = str(e.get("first_buy_date") or "")[:10]
                blocked = rearmed = False
                # 沒賣成（broker／daily 標 unfilled、shares＝0）不算減過（A3-M7A-02；與 writeback._prior_trim 同口徑）
                if isinstance(lt, dict) and lt.get("unfilled") or isinstance(lt, dict) and lt.get("shares") == 0:
                    lt = None
                if isinstance(lt, dict) and lt.get("reason_code") == "ABSURD" and _is_ymd(lt.get("date")) \
                        and not (_is_ymd(fb) and str(lt["date"])[:10] < fb):
                    rl = _pos(lt.get("line")) or line   # 與回寫端同一條線（writeback._vet_valuation_sell）
                    below = px[(px["date"] > date.fromisoformat(str(lt["date"])[:10])) & (px["close"] < rl)]
                    rearmed = bool((below["date"] > last_d).any())   # 上次分析日之後又跌破
                    # 減碼後從未跌破；或上次分析時已在線上、之後沒再跌破 → 擋。上次分析時在線下 → 交給一般邊緣觸發
                    blocked = not len(below) or (was(lambda v: v >= rl) and not rearmed)
                    checked["last_trim"] = lt
                    if len(below):   # 回到線下的證據：減碼後第一筆線下收盤（review.py 帶進 PRICE／HOLD_REVIEW 的 params）
                        checked["absurd_rearm"] = {"date": str(below["date"].iloc[0])[:10], "close": float(below["close"].iloc[0]),
                                                   "line": rl, "last_trim_date": str(lt["date"])[:10]}
                if p >= line and blocked:
                    unchecked.append(f"荒謬級線：{str(lt['date'])[:10]} 已依荒謬級減碼，之後（上次減碼／分析日後）未回到線下，"
                                     "不再觸發（D03 只減一次）")
                elif p >= line and (rearmed or not was(lambda v: v >= line)):
                    rr = checked.get("absurd_rearm") if key == "absurd_price_line" else None
                    add("PRICE", f"現價 {p:.2f}（{p_date}）≥ {name} {line:.2f}：{lv_why}"
                        + (f"（上次荒謬級減碼後曾回到線下：{rr['date']} 收盤 {rr['close']:.2f}）" if rr else ""))
            cap = _pos(e.get("auto_buy_cap"))
            checked["auto_buy_cap"] = cap
            if cap is None:
                unchecked.append("登錄表缺 auto_buy_cap（或 ≤0）：加碼檢查未做")
            elif p < cap and (e.get("weight") or 0) < (e.get("target_weight") or 0):
                add("PRICE", f"持股權重未達目標且現價 {p:.2f} < 自動買進上限 {cap}：加碼許可檢查")
        if status == "WATCH":
            cap = _pos(e.get("auto_buy_cap"))
            checked["auto_buy_cap"] = cap
            if cap is None:
                unchecked.append("登錄表缺 auto_buy_cap（或 ≤0）：觀察名單跌破上限未檢查")
            elif e.get("grade") not in (None, "A", "B"):   # 只有 A／B 級有自動買進上限（M2_D2_04）
                if p < cap:
                    unchecked.append(f"等級 {e.get('grade')}：觀察名單跌破上限不排 PRICE（只有 A／B 級有自動買進上限）")
            elif p < cap and e.get("last_reason_code") in NO_PRICE_UPGRADE:   # PRICE 不能把它升成 BUY（D24），只等完整分析重跑
                rc = e.get("last_reason_code")
                unchecked.append(f"上次完整分析{NO_PRICE_UPGRADE[rc]}（{rc}）：現價 {p:.2f} < 上限 {cap} 不排 PRICE，"
                                 "待 next_full_review_due 重跑完整分析（M2-RS2-03、A3-M2B-07）")
            elif p < cap:
                add("PRICE", f"觀察名單現價 {p:.2f} < 自動買進上限 {cap}：快速通道重跑 G7–G11 與三閘門")

    suppressed, label = [], None
    if status in ("TOO_HARD", "REJECT", "MISSING_DATA", "NO_ACTION"):
        if status == "MISSING_DATA":
            # 缺資料檔：有新年報或新季報就 FULL（資料可能已補齊）；其餘觸發不看（D27、M2-D10-03）
            keep = [("FULL", f"缺資料檔{w}：重做完整分析（D27）") for _, w in r_filed]
            drop = [r for r in reasons if r not in r_filed]
            label = "缺資料檔（D27）只看新年報／新季報"
        elif status == "NO_ACTION":
            keep = [r for r in reasons if r[0] == "FULL"]
            drop = [r for r in reasons if r[0] != "FULL"]
            label = "NO_ACTION 只看 FULL 類事件"
        elif e.get("integrity_reject"):
            # 誠信淘汰（NEVER-04(a)）：現任決策者在位期間不重評；只有董事或高階經理人異動（8-K 5.02）才 FULL（D27）
            keep = [r for r, codes in r_8k if "5.02" in codes]
            drop = [r for r in reasons if r not in keep]
            label = "誠信淘汰（NEVER-04(a)）只看 8-K 5.02 董事或高階經理人異動"
        else:
            # 淘汰類不例行重評（P-62）：只有重大 8-K 才重評；滿 12 個月後是否再研究，由初篩是否再次選入決定
            keep = [r for r, _ in r_8k]
            drop = [r for r in reasons if r not in keep]
            label = "淘汰類（P-62）只看重大 8-K"
        suppressed = [f"[{lv}] {w}" for lv, w in drop]
        # 被丟掉的初判項目也列進 suppressed（M2-D5-01）
        suppressed += [f"[TRIAGE] 8-K {t['date']} {t['items']}" if t["type"] == "8-K"
                       else f"[TRIAGE] 新聞 {t['date']} {t['headline']}" for t in sorted(triage, key=lambda t: t["date"])[-20:]]
        # 這些狀態仍看申報：抓取失敗的「未檢查」要保留，不得寫成「無相關事件」（M2-D5-01）
        reasons, triage, news_hits = keep, [], []
        unchecked = [u for u in unchecked if u.startswith("申報抓取失敗")]
    triage = sorted(triage, key=lambda t: t["date"])[-20:]
    notes = [f"（{u}）" for u in unchecked]
    top = max(reasons, key=lambda r: RANK[r[0]])[0] if reasons else "NONE"
    if detail is not None:
        detail.update({"suppressed": suppressed, "unchecked": unchecked, "checked": checked,
                       "price_status": price_status, "filings_ok": filings_ok, "filings_failed": not filings_ok,
                       # THESIS 遮蔽了同期 PRICE 型觸發：框架在 THESIS 判完好後續接 PRICE（M2_D7_01）
                       "also_price": top == "THESIS" and any(lv == "PRICE" for lv, _ in reasons),
                       "news": sorted(news_hits, key=lambda t: t["date"])[-20:]})
    if not reasons:
        if label:
            parts = ([f"本期 " + "；".join(suppressed) + " 均不重評"] if suppressed else []) \
                + ([] if filings_ok else ["申報抓取失敗：重大申報未檢查"])
            why = f"{label}：" + "；".join(parts or ["本期無相關事件"]) + "，沿用上次結論"
        else:
            gaps = ([] if price_status == "ok" or status not in ("HOLDING", "WATCH")
                    else ["價格缺／過期：價格類觸發未檢查"]) + ([] if filings_ok else ["申報抓取失敗：重大申報未檢查"])
            if gaps:   # 不得說「沒有變化」（M2_D9_05）
                why = "；".join(gaps) + "；其餘觸發無變化，沿用上次結論"
            else:
                why = "論點相關事實與價格區間都沒有變化：沿用上次結論" + ("（部分觸發未檢查，見下）" if unchecked else "")
        return "NONE", [why] + notes, triage
    return top, [f"[{lv}] {w}" for lv, w in reasons] + notes, (triage if RANK[top] < RANK["THESIS"] else [])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--asof", default=date.today().isoformat())
    ap.add_argument("--prices")
    ap.add_argument("--t30", type=float, help="決策日前最後一個 30 年期公債殖利率（小數）")
    ap.add_argument("--news", help="新聞標題 JSON（外部框架提供）：{ticker: [{date, headline, source, url}]}")
    ap.add_argument("--offline", help="測試用申報 JSON")
    ap.add_argument("--ua", default=None)
    ap.add_argument("--out", default="due.json")
    a = ap.parse_args()
    ua = a.ua or UA
    reg = json.load(open(a.registry, encoding="utf-8"))
    entries = reg["entries"] if isinstance(reg, dict) else reg
    off = json.load(open(a.offline, encoding="utf-8")) if a.offline else None
    if off is None and "@" not in ua:
        sys.exit("SEC 要求 User-Agent 含 email：設定 SEC_USER_AGENT 或 --ua")
    news_all = json.load(open(a.news, encoding="utf-8")) if a.news else None
    px_all = pd.read_csv(a.prices, parse_dates=["date"]) if a.prices else None
    if px_all is not None:
        px_all["date"] = px_all["date"].dt.date
    out = []
    for e in entries:
        tk = e["ticker"]
        try:
            fl = off.get(tk, []) if off is not None else sec_filings(e["cik"], ua)
        except Exception as ex:  # 抓不到申報：不升 THESIS（論點檢查不讀年報、完好後窗口前移會漏掉申報），標未檢查（M2_D9_03）
            fl, err = None, f"申報抓取失敗：{type(ex).__name__}: {str(ex)[:200]}"
        else:
            err = None
        if px_all is not None:
            px = px_all[(px_all.ticker == tk)]
        elif off is None:
            try:
                px = yahoo_daily(tk)
            except Exception:
                px = None
        else:
            px = None
        if px is not None:
            px = px[px["date"] <= date.fromisoformat(a.asof)].sort_values("date")
        det = {}
        try:
            act, why, tri = check(e, fl, px, a.asof, a.t30, (news_all or {}).get(tk), detail=det)
        except Exception as ex:   # 單檔例外不得讓整批沒有 due.json（A3-M2E-02）：該檔 NONE、標 check_error，由框架開異常
            cerr = f"check_error: {type(ex).__name__}: {str(ex)[:200]}"
            act, why, tri = "NONE", [cerr, "（本檔觸發檢查失敗：全部觸發未檢查，不得視為「沒有變化」）"], []
            det = {"suppressed": [], "unchecked": [f"{cerr}：全部觸發未檢查"], "checked": {},
                   "price_status": "unchecked", "filings_ok": fl is not None, "filings_failed": fl is None,
                   "also_price": False, "news": [], "check_error": cerr}
        if err:
            why.append(f"（{err}）")
        if news_all is None:
            why.append("（未提供新聞：新聞類初判未檢查）")
        out.append({"ticker": tk, "cik": e.get("cik"), "status": e.get("status"), "action": act, "reasons": why,
                    "triage": tri, "suppressed": det["suppressed"], "unchecked": det["unchecked"],
                    "checked": det["checked"], "price_status": det["price_status"], "filings_ok": det["filings_ok"],
                    "filings_failed": det["filings_failed"],
                    "also_price": det["also_price"], "news": det["news"], "thesis_file": e.get("thesis_file"),
                    "absurd_rearm": det["checked"].get("absurd_rearm"),   # D03 回到線下證據（review.py 帶進觸發參數）
                    "kill_criteria": [k.get("text", "") for k in e.get("kill_criteria_due", [])],
                    "last_review_date": e.get("last_review_date"),
                    "check_error": det.get("check_error")})
    out.sort(key=lambda r: -RANK[r["action"]])
    json.dump({"signal_version": SIGNAL_VERSION, "asof": a.asof,
               "triage_rule": "triage 非空 → 先做 TRIAGE；任一項「是」或「不確定」→ action 升為 THESIS",
               "results": out}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    cnt = pd.Series([r["action"] for r in out]).value_counts().to_dict()
    nt = sum(1 for r in out if r["triage"])
    print(f"{len(out)} 檔：" + "，".join(f"{k} {v}" for k, v in cnt.items()) + f"；需初判 {nt} 檔 → {a.out}")


if __name__ == "__main__":
    main()
