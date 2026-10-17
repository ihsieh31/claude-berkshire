#!/usr/bin/env python3
"""兩週例行檢查：判斷登錄表中每一檔「這次要不要重新分析、做到多深」。論點沒動 → 不分析，沿用上次結論。

  SEC_USER_AGENT="名稱 email" python review_due.py --registry state/registry.json --asof 2026-10-15 \
      [--prices daily.csv] [--t30 0.047] [--news news.json] [--offline filings.json] [--out due.json]

本腳本不呼叫任何模型，只輸出訊號檔；由外部框架讀訊號、依 ../../routing/model_routing.json 調用模型。

- registry.json：深度分析每次結束時回寫（格式見 ../assets/registry_schema.json）。
- daily.csv：ticker,date,close（近 100 個交易日以上的日收盤）；缺 → 以 Yahoo 日線抓（非官方）。
- 申報：SEC submissions（10-K、10-Q、8-K 與 8-K 項目碼），只看「上次分析日之後、決策日之前」申報者。
- news.json（選用，由外部框架提供）：{ticker: [{"date","headline","source","url"}]}；只取上次分析日之後、決策日之前者。
- --offline：測試用，直接給 {ticker: [{"form","filingDate","items"}]}，不連網。

輸出每檔一個 action（由重到輕；同一檔取最重者）：
  FULL      完整重跑深度分析（G0–G11）       新年報；滿 12 個月；重大 8-K
  THESIS    論點破裂檢查（H1 輕複查＋H2 Q2）  新季報；成本 −12%；單日 ±4%；自上次分析跌 ≥20%；單季自高點跌 ≥25%（跌價只觸發論點檢查）
  PRICE     只重跑 G7–G11（P-63 快速通道）     觀察名單現價 < 自動買進上限；持股未達目標權重且現價 < 上限（加碼檢查）；
                                               持股現價 ≥ sell_review_above（賣出檢視）或 ≥ 荒謬級線；長債較上次估值變動 ≥1pp（只影響估值）
  NONE      不分析，沿用上次結論

另輸出 triage（初判清單）：腳本無法判斷輕重的事件——非重大項目碼的 8-K、新聞標題。只在 action 為 PRICE／NONE 時列出
（FULL／THESIS 本來就會讀全部新事實）。框架規則：triage 非空 → 先做 TRIAGE 初判；任一項回答「是」或「不確定」→ action 升為 THESIS。
淘汰類（TOO_HARD／REJECT）12 個月內只有 FULL 類重大事件才重評（P-62）。
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, timedelta

import pandas as pd

UA = os.environ.get("SEC_USER_AGENT", "")
MATERIAL_8K = {"1.03": "破產或接管", "2.06": "重大減損", "4.01": "更換會計師", "4.02": "財報不可依賴（重編）",
               "5.02": "董事或高階經理人異動", "2.01": "完成重大收購或處分", "1.01": "重大合約", "3.01": "下市或不符上市規定"}
IGNORE_8K = {"9.01", "5.07"}  # 只附展品、股東會投票結果：不需初判
RANK = {"NONE": 0, "PRICE": 1, "THESIS": 2, "FULL": 3}
SIGNAL_VERSION = "review-due-v2"


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


def check(e, filings, px, asof, t30, news=None):
    """回傳 (action, reasons, triage)。e：登錄表一列；filings：該檔申報清單；px：該檔日收盤（date 升冪，≤ asof）；
    news：該檔新聞 [{"date","headline","source","url"}]（選用）。"""
    last = e.get("last_review_date") or "1900-01-01"
    full_last = e.get("last_full_review_date") or last
    status = e.get("status", "WATCH")
    reasons, triage = [], []

    def add(level, why):
        reasons.append((level, why))

    new = [f for f in filings if last < f["filingDate"] < asof]
    if any(f["form"] in ("10-K", "20-F", "40-F") for f in new):
        add("FULL", "上次分析後有新年報")
    # 10-K/A 多半只補封面或 Part III：只到 THESIS（原始 10-K 仍是 FULL）
    if any(f["form"] in ("10-Q", "10-Q/A", "10-K/A") for f in new):
        add("THESIS", "上次分析後有新季報")
    for f in new:
        if f["form"].startswith("8-K"):
            hit = [MATERIAL_8K[i.strip()] for i in str(f.get("items") or "").split(",") if i.strip() in MATERIAL_8K]
            codes = {i.strip() for i in str(f.get("items") or "").split(",") if i.strip()}
            if hit:
                add("FULL", f"8-K（{f['filingDate']}）：" + "、".join(hit))
            elif codes - IGNORE_8K or not codes:
                triage.append({"type": "8-K", "date": f["filingDate"], "items": ",".join(sorted(codes)), "url": f.get("url", "")})
    for n in news or []:
        if last < str(n.get("date", ""))[:10] < asof:
            triage.append({"type": "news", "date": str(n["date"])[:10], "headline": n.get("headline", ""),
                           "source": n.get("source", ""), "url": n.get("url", "")})
    if (date.fromisoformat(asof) - date.fromisoformat(full_last)).days >= 365:
        add("FULL", "距上次完整分析已滿 12 個月")
    if t30 is not None and e.get("t30_at_valuation") is not None and abs(t30 - e["t30_at_valuation"]) >= 0.01:
        add("PRICE", f"30 年期殖利率較上次估值變動 ≥1pp（{e['t30_at_valuation']:.2%} → {t30:.2%}）")
    for kc in e.get("kill_criteria_due", []):
        if kc.get("due") and kc["due"] <= asof:
            add("THESIS", f"失效條件到期檢查：{kc.get('text', '')}")

    if px is not None and len(px):
        p = float(px["close"].iloc[-1])
        rp = e.get("review_price")
        q = px[px["date"] >= (date.fromisoformat(asof) - timedelta(days=92))]
        if len(q) and q["close"].max() > 0 and p / q["close"].max() - 1 <= -0.25:
            add("THESIS", "單季自高點跌 ≥25%（查新事實；跌價本身不是賣出或調高價值的理由）")
        if rp and p / rp - 1 <= -0.20:
            add("THESIS", f"自上次分析價格跌 ≥20%（{rp} → {p:.2f}）")
        recent = px[px["date"] > date.fromisoformat(last)]
        if len(recent) >= 2 and (recent["close"].pct_change().abs() >= 0.04).any():
            add("THESIS", "上次分析後出現單日 ±4%")
        if status == "HOLDING":
            if e.get("cost") and p / e["cost"] - 1 <= -0.12:
                add("THESIS", "較成本 −12%（論點破裂檢查，不是停損）")
            if e.get("sell_review_above") and p >= e["sell_review_above"]:
                add("PRICE", "現價 ≥ sell_review_above：H2 Q3 賣出檢視")
            if e.get("absurd_price_line") and p >= e["absurd_price_line"]:
                add("PRICE", "現價 ≥ 荒謬級線（正常化 P/E ≥50）：至少賣一半的檢查")
            if e.get("auto_buy_cap") and p < e["auto_buy_cap"] and (e.get("weight") or 0) < (e.get("target_weight") or 0):
                add("PRICE", f"持股權重未達目標且現價 {p:.2f} < 自動買進上限 {e['auto_buy_cap']}：加碼許可檢查")
        if status == "WATCH" and e.get("auto_buy_cap") and p < e["auto_buy_cap"]:
            add("PRICE", f"觀察名單現價 {p:.2f} < 自動買進上限 {e['auto_buy_cap']}：快速通道重跑 G7–G11 與三閘門")

    if status in ("TOO_HARD", "REJECT"):
        # 淘汰類不例行重評（P-62）：只有重大 8-K 才重評；滿 12 個月後是否再研究，由初篩是否再次選入決定
        reasons, triage = [r for r in reasons if r[1].startswith("8-K")], []
    triage = sorted(triage, key=lambda t: t["date"])[-20:]
    if not reasons:
        return "NONE", ["論點相關事實與價格區間都沒有變化：沿用上次結論"], triage
    top = max(reasons, key=lambda r: RANK[r[0]])[0]
    return top, [f"[{lv}] {w}" for lv, w in reasons], (triage if RANK[top] < RANK["THESIS"] else [])


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
        except Exception as ex:  # 抓不到申報 → 保守處理：要求論點檢查
            fl, err = [], f"申報抓取失敗：{type(ex).__name__}"
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
        act, why, tri = check(e, fl, px, a.asof, a.t30, (news_all or {}).get(tk))
        if err:
            act, why, tri = max(act, "THESIS", key=lambda x: RANK[x]), why + [f"[THESIS] {err}"], []
        if px is None or not len(px):
            why.append("（無價格資料：價格類觸發未檢查）")
        if news_all is None:
            why.append("（未提供新聞：新聞類初判未檢查）")
        out.append({"ticker": tk, "cik": e.get("cik"), "status": e.get("status"), "action": act, "reasons": why,
                    "triage": tri, "thesis_file": e.get("thesis_file"),
                    "kill_criteria": [k.get("text", "") for k in e.get("kill_criteria_due", [])],
                    "last_review_date": e.get("last_review_date")})
    out.sort(key=lambda r: -RANK[r["action"]])
    json.dump({"signal_version": SIGNAL_VERSION, "asof": a.asof,
               "triage_rule": "triage 非空 → 先做 TRIAGE；任一項「是」或「不確定」→ action 升為 THESIS",
               "results": out}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    cnt = pd.Series([r["action"] for r in out]).value_counts().to_dict()
    nt = sum(1 for r in out if r["triage"])
    print(f"{len(out)} 檔：" + "，".join(f"{k} {v}" for k, v in cnt.items()) + f"；需初判 {nt} 檔 → {a.out}")


if __name__ == "__main__":
    main()
