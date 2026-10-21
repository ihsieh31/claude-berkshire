#!/usr/bin/env python3
"""資料包骨架：從 SEC companyfacts 產生 10 年財報表（只用決策日前已申報者），價格另存一檔（價格隔離）。

  SEC_USER_AGENT="名稱 email" python build_dossier.py --ticker KO --asof 2026-10-15 --outdir dossier \
      [--cache edgar_cache] [--price 86.1 --price-date 2026-10-14] [--t30 0.0475 --t30-date 2026-10-14] [--t30-20y-med 0.035]

輸出：
  dossier/代號.md         10 年財報表＋衍生比率＋申報日；**不含任何價格**。年報文字節錄由 DOSSIER_TEXT 工作接著追加。
                          回購只給級距（回購÷業主盈餘近似、淨股數年變化），不給精確回購金額與回購股數（procedure/08 §1）。
  dossier/代號_price.json  價格、利率、精確回購金額／股數與平均回購價（作答閘門 3 問卷並存檔後才可開啟）。
                          本腳本把兩檔寫在同一目錄；價格隔離由呼叫端負責：前半（步 0–8、問卷）的工作環境不得放入或可讀
                          此檔（外部框架以分開的工作與沙箱，只在 FULL_B 提供）。skill 單跑時請自行移到前半讀不到的位置。
利率段：--t30／--t30-date／--t30-20y-med 只寫進價格檔；**skill 單跑不產生資料包 md 的利率段**（30 年期公債與 3 個月國庫券
  殖利率，SKILL §0 前半可讀），由宿主 trader/analysis/dossier_prep.py 的 rates_section 補進資料包。skill 單跑時請自行在 md 末加
  「## 利率」段（決策日前最後一個 30 年期與 3 個月殖利率，各附日期；不是個股價格）（A3-M2C-07）。
需要 ../../berkshire-screen-merged/scripts/fetch_edgar.py（共用抓取、標籤與時點邏輯；改標籤須兩邊驗收）。
"""
import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "berkshire-screen-merged", "scripts"))
import fetch_edgar as F  # noqa: E402

EXTRA = {"dna": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet"],
         "sbc": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
         "tax": ["IncomeTaxExpenseBenefit"], "gross": ["GrossProfit"], "goodwill": ["Goodwill"],
         "dividends": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
         "equity_income": ["IncomeLossFromEquityMethodInvestments"], "cash": ["CashAndCashEquivalentsAtCarryingValue"]}
# 回購（只進價格檔與級距計算，不進資料包表格）
BUYBACK_CF_TAGS = ["PaymentsForRepurchaseOfCommonStock"]                                   # 現金流量表回購金額
# 同表（權益變動表／庫藏股）的「股數, 金額」成對概念；均價優先用同一對算
BUYBACK_PAIRS = [("TreasuryStockSharesAcquired", "TreasuryStockValueAcquiredCostMethod"),
                 ("StockRepurchasedAndRetiredDuringPeriodShares", "StockRepurchasedAndRetiredDuringPeriodValue"),
                 ("StockRepurchasedDuringPeriodShares", "StockRepurchasedDuringPeriodValue")]
BUYBACK_SHARE_MISMATCH = 1.5   # 推論：無同表金額時，兩個股數概念相差超過 1.5 倍 → 該年不給均價（口徑不明）
SHARES_JUMP = (0.55, 1.8)      # 推論：相鄰年度稀釋股數比超出此範圍 → 疑似拆股基準未追溯或合併
INTEREST_LOW = 0.005           # 推論：利息費用 < 有息負債 0.5% → 疑似利息資本化（建商）或標籤錯
DEBT_TINY = 0.01               # 推論：有息負債 < 總負債 1% 且有利息費用 → 疑似只取到一部分債務標籤（PHM 型）
BUYBACK_CF_RANGE = 3.0         # 推論：均價與前後 3 年內他年均價中位數差 >3 倍 → 量級異常，不給（所有算法）
BUYBACK_MIN_PRICE = 0.5        # 推論：均價 <0.5 美元 → 單位錯（EQT 2016＝0.13），不給
BUYBACK_MAX_CAP_ASSETS = 30    # 推論：均價×稀釋股數（隱含市值）>總資產 30 倍 → 單位或口徑錯，不給（ADP 2011–18 約 75 萬倍、
                               # PSA 2015–21、MSI 2017–25、ZTS 2022–24）。2026-10-15 快取全池合理值最高約 24 倍（NVDA FY2025），
                               # 其餘 ≤21。用總資產而非每股淨值／EPS：後兩者可能 ≤0 或趨近 0
BUYBACK_AVG_NOTE = "推算值，僅供參考（回購金額 ÷ 回購股數，少數年度可能高估數倍）；不得以回購均價推論估值，估值以內在價值為準"   # D73
BALANCE_TOL = 0.005            # 推論：總資產與負債＋權益合計差 >0.5% → 標籤或版本不一致
ROW_ORDER = ["revenue", "gross", "operating_income", "pretax", "tax", "net_income", "eps", "shares", "dna", "sbc",
             "ocf", "capex", "dividends", "equity_income", "assets", "liabilities", "equity", "goodwill", "cash",
             "debt_lt", "debt_cur", "interest"]
LABEL = {"revenue": "營收", "gross": "毛利", "operating_income": "營業利益", "pretax": "稅前盈餘", "tax": "所得稅",
         "net_income": "淨利（歸屬母公司）", "eps": "EPS（稀釋，美元）", "shares": "稀釋加權股數（百萬股）",
         "dna": "折舊攤銷", "sbc": "股權薪酬", "ocf": "營業現金流", "capex": "資本支出", "dividends": "普通股股利",
         "equity_income": "權益法投資損益", "assets": "總資產", "liabilities": "總負債", "equity": "股東權益（歸屬母公司）",
         "goodwill": "商譽", "cash": "現金及約當現金", "debt_lt": "長期債（含租賃；部分標籤含一年內到期）",
         "debt_cur": "一年內到期債務＋短期借款", "interest": "利息費用", "debt": "有息負債合計"}
NA_EQ = "不適用（權益≤0）"


def _bucket_buyback(b, oe):
    """回購金額 ÷ 業主盈餘近似 的級距；不輸出精確比例。"""
    if pd.isna(b):
        return "缺（無回購標籤）"
    b = abs(b)   # 申報端常以負號表示回購（GE FY2016 現金流量表 −22.58B）：與價格檔同口徑取絕對值（M2-RS1-02）
    if b == 0:
        return "無回購"
    if pd.isna(oe):
        return "缺（業主盈餘近似缺）"
    if oe <= 0:
        return "業主盈餘近似≤0"
    r = b / oe
    return "<25%" if r < 0.25 else "25–50%" if r < 0.5 else "50–100%" if r <= 1.0 else ">100%"


def _bucket_shares(c):
    if pd.isna(c):
        return "缺"
    if c <= -0.03:
        return "減 >3%"
    if c <= -0.01:
        return "減 1–3%"
    if c < 0.01:
        return "±1% 內"
    return "增 1–3%" if c < 0.03 else "增 >3%"


def build(facts, tk, asof):
    tags = dict(F.TAGS); tags.update(EXTRA)
    S = {k: F.annual_series(facts, v, asof, k in ("shares", "eps")) for k, v in tags.items()}
    if S["net_income"].empty:
        return None, None
    D = F.debt_series(facts, asof)
    S.update(debt_lt=D["debt_lt"], debt_cur=D["debt_cur"], debt=D["debt"])
    S["buyback"] = F.annual_series(facts, BUYBACK_CF_TAGS + [v for _, v in BUYBACK_PAIRS], asof)   # 只供級距計算
    last = int(S["net_income"].index.max()); yrs = range(last - 9, last + 1)
    T = pd.DataFrame({k: v.reindex(yrs) for k, v in S.items()})
    pos_eq = T.equity.where(T.equity > 0)
    avg_eq = (pos_eq + pos_eq.shift(1)) / 2   # 期初或期末權益 ≤0 → 不計（平均值為正也不算：M2-D5-06）
    R = pd.DataFrame(index=T.index)
    R["營業利益率"] = T.operating_income / T.revenue
    R["ROE（期初期末平均；期初或期末權益≤0 不適用）"] = T.net_income / avg_eq
    R["營業現金流÷淨利"] = T.ocf / T.net_income
    R["資本支出÷折舊"] = T.capex.abs() / T.dna
    R["資本支出÷營收"] = T.capex.abs() / T.revenue
    R["有效稅率"] = T.tax / T.pretax
    R["有息負債÷權益（權益≤0 不適用）"] = T.debt / pos_eq
    R["利息保障（稅前息前÷利息）"] = (T.pretax + T.interest) / T.interest.where(T.interest > 0)
    # 回購級距（09:42 決策：前半只給級距，精確金額與股數在價格檔）
    oe = T.net_income + T.dna - T.capex.abs()
    bb_band = [_bucket_buyback(T.buyback.get(y), oe.get(y)) for y in T.index]
    # 股數跳動年（checks 警示疑似拆股基準未追溯）不給增減級距，避免把拆股當稀釋（M4-R2-01）
    sh_band = ["疑似拆股基準（見警示）" if pd.notna(c) and not SHARES_JUMP[0] <= c <= SHARES_JUMP[1]
               else _bucket_shares(c - 1) for c in (T.shares / T.shares.shift(1))]
    bands = pd.DataFrame({"回購÷業主盈餘近似 級距": bb_band, "稀釋股數年變化 級距": sh_band}, index=T.index)
    latest_ni = F.annual_series(facts, F.TAGS["net_income"], asof, latest=True).reindex(yrs)
    lse = F.annual_series(facts, ["LiabilitiesAndStockholdersEquity"], asof).reindex(yrs)
    filed = {k: v.attrs.get("filed", {}) for k, v in S.items()}
    return T, (R, filed, last, checks(T, S, latest_ni, lse), bands)


def checks(T, S=None, latest_ni=None, lse=None):
    """資料品質警示：只標示，不改數字。"""
    W = []
    G = {"neg_eq": [], "no_debt": [], "no_int": [], "low_int": [], "tiny_debt": []}   # 多年同類警示合併成一行
    S = S or {}
    lni = latest_ni if latest_ni is not None else pd.Series(dtype=float)
    for y, r in T.iterrows():
        ni = lni.get(y) if pd.notna(lni.get(y)) else r.net_income   # EPS、股數是最新申報版 → 淨利也用同版本比
        if pd.notna(r.eps) and pd.notna(r.shares) and r.shares > 0 and pd.notna(ni) and r.eps != 0:
            d = (ni / r.shares) / r.eps - 1
            if abs(d) > 0.05:
                W.append(f"FY{y}：淨利÷稀釋股數 與 EPS 差 {d:+.0%}（特別股股利、少數股權、拆股基準或單位問題，需查年報）")
        if pd.notna(r.eps) and r.eps == round(r.eps) and r.eps != 0:
            W.append(f"FY{y}：EPS 為整數 {r.eps:g}，疑似四捨五入或單位錯誤，需對年報")
        if pd.notna(r.ocf) and pd.notna(r.net_income) and r.net_income > 0 and r.ocf / r.net_income < 0.5:
            W.append(f"FY{y}：營業現金流不到淨利一半（盈餘品質關需作答）")
        if pd.notna(lni.get(y)) and pd.notna(r.net_income) and r.net_income != 0 and \
                abs(lni.get(y) / r.net_income - 1) >= 0.005:
            W.append(f"FY{y}：淨利已重編（首次版與最新版差 {lni.get(y) / r.net_income - 1:+.1%}；本表為首次版，"
                     "見「首次公布版與最新重編版對照」）")
        if "equity" in T and pd.notna(r.equity) and r.equity <= 0:
            G["neg_eq"].append(y)
        if "debt" in T:
            if pd.isna(r.debt):
                G["no_debt"].append(y)
            elif r.debt > 0 and "interest" in T and pd.isna(r.interest):
                G["no_int"].append(y)
            elif r.debt > 0 and pd.notna(r.interest) and r.interest / r.debt < INTEREST_LOW:
                G["low_int"].append(y)
            if pd.notna(r.debt) and pd.notna(r.get("liabilities")) and r.liabilities > 0 and \
                    r.debt < DEBT_TINY * r.liabilities and pd.notna(r.interest) and r.interest > 0:
                G["tiny_debt"].append(y)
        if lse is not None and pd.notna(lse.get(y)) and pd.notna(r.assets) and r.assets > 0 and \
                abs(lse.get(y) / r.assets - 1) > BALANCE_TOL:
            W.append(f"FY{y}：總資產與負債＋權益合計不符（差 {lse.get(y) / r.assets - 1:+.1%}），標籤或版本不一致")
    yl = lambda ys: "、".join(f"FY{y}" for y in ys)
    if G["neg_eq"]:
        W.append(f"{yl(G['neg_eq'])}：股東權益 ≤0，ROE（含次一年：期初權益 ≤0）與有息負債÷權益（P-07）不適用；"
                 "依 SEL-076b 改看有形資本報酬（SEL-055，見補充資產負債欄位），生存性看利息保障（P-06）")
    if G["no_debt"]:
        W.append(f"{yl(G['no_debt'])}：有息負債 XBRL 缺（不得當 0），須由債務附註補")
    if G["no_int"]:
        W.append(f"{yl(G['no_int'])}：有有息負債但利息費用 XBRL 缺（不得當無利息），利息保障（P-06）須由損益表或債務附註補")
    if G["tiny_debt"]:
        W.append(f"{yl(G['tiny_debt'])}：有息負債不到總負債的 {DEBT_TINY:.0%} 但有利息費用（可能只取到一部分債務標籤），"
                 "有息負債需對債務附註")
    if G["low_int"]:
        W.append(f"{yl(G['low_int'])}：利息費用不到有息負債的 {INTEREST_LOW:.1%}（可能利息資本化或標籤錯），利息保障需對附註")
    if "shares" in T:
        for y, c in (T.shares / T.shares.shift(1)).items():
            if pd.notna(c) and not SHARES_JUMP[0] <= c <= SHARES_JUMP[1]:
                W.append(f"FY{y}：稀釋股數為前一年的 {c:.2f} 倍（疑似拆股基準未追溯、合併或單位錯誤；以後一年 10-K 為準核對）")
    for k, s in S.items():   # 逐年換標籤（口徑可能斷點）、期末月份變動、同財年兩個期末
        if k in ("debt_lt", "debt_cur", "buyback"):   # 有息負債三欄同一組標籤，只在 debt 報一次；回購不在表上
            continue
        tg = s.attrs.get("tag", {})
        ys = sorted(y for y in tg if y in T.index)
        sw = [f"FY{b} 起 {tg[a]}→{tg[b]}" for a, b in zip(ys, ys[1:]) if tg[a] != tg[b]]
        if sw:
            W.append(f"{LABEL.get(k, k)}：換標籤（口徑可能斷點，對原文）：" + "；".join(sw))
        for w in s.attrs.get("warn", []):
            W.append(w)
    end = S.get("net_income").attrs.get("end", {}) if S.get("net_income") is not None else {}
    ys = sorted(y for y in end if y in T.index)
    for a, b in zip(ys, ys[1:]):
        if abs((pd.Timestamp(end[b]) - pd.Timestamp(end[a])).days - 365) > 20:
            W.append(f"FY{b}：期末日 {end[a]}→{end[b]}（會計年度變更或期間不足一年，跨年比較需調整）")
    for c in ("revenue", "net_income", "equity", "ocf", "capex", "dna", "shares", "debt", "interest"):
        n = int(T[c].isna().sum()) if c in T else len(T)
        if n:
            W.append(f"{LABEL.get(c, c)}：10 年中缺 {n} 年")
    return list(dict.fromkeys(W))


def _by_filing(facts, tag, asof):
    """單一標籤的 10-K 全年值，依（財年, 申報日）索引：{(fy, filed): val}（同一份申報的股數與金額才能配對）。"""
    g = facts.get("facts", {}).get("us-gaap", {})
    if tag not in g:
        return {}
    off = F.fiscal_year_offset(facts, asof)
    df, _ = F._tag_rows(g, tag, asof, off, F.fiscal_year_ends(g, asof, off))   # 過渡年與 annual_series 同一期末
    return {(int(r.fy), r.filed): float(r.val) for r in df.itertuples()}


def buyback_price_data(facts, asof):
    """價格檔用：精確回購金額、回購股數、平均回購價。
    - 均價的股數與金額取「同一份 10-K」（同時有兩者的最新一份），不混拆股基準。
      優先同表成對概念（BUYBACK_PAIRS）；沒有成對時股數配同表其他金額概念，再缺才用現金流量表金額。
    - 拆股基準：所用申報的稀釋股數與最新版稀釋股數比超出 SHARES_JUMP → 該年是舊基準，不給；最新版稀釋股數序列在
      某年跳動（10-K 只追溯 3 年，更早年度仍是舊基準）→ 跳動年之前一律不給。
    - 量級防呆（所有算法）：均價 <BUYBACK_MIN_PRICE、或隱含市值（均價×稀釋股數）>總資產 BUYBACK_MAX_CAP_ASSETS 倍
      → 單位錯，不給；其餘與前後 3 年內其他年度均價中位數差 >BUYBACK_CF_RANGE 倍 → 量級異常，不給。都寫警示。
      前後 3 年內沒有他年均價可比（孤立年度）→ 照給，但寫警示並在 avg_buyback_basis 標「未比對」（M2-RS1-01：
      總資產上限對銀行、REIT 等資產重的公司抓不到單位錯，RF 2022＝317、KIM 2018＝270 型）。
    - 金額或股數為負（權益變動表常以負號表示減少）→ 取絕對值；為 0 或缺 → 該組不用。
    - 現金流量表金額給的年度也寫警示。無成對金額且兩股數概念差 >1.5 倍 → 不給。
    - 會計年度變更過渡年：與資料包同一期末（fiscal_year_ends），較早期末的數字不用，警示寫進 avg_buyback_warnings。
    avg_buyback_basis 記每年所用標籤與申報日；avg_buyback_warnings 記不給的原因與現金流量表備援。
    avg_buyback_price_note（BUYBACK_AVG_NOTE，D73）：均價是推算值，僅供參考，不得據以推論估值。"""
    latest = lambda t: F.annual_series(facts, [t], asof, latest=True)
    share_tags = [s for s, _ in BUYBACK_PAIRS]
    amt_tags = [v for _, v in BUYBACK_PAIRS] + BUYBACK_CF_TAGS
    Q = {t: _by_filing(facts, t, asof) for t in share_tags + amt_tags + F.TAGS["shares"]}
    dil = F.annual_series(facts, F.TAGS["shares"], asof, latest=True)
    assets = F.annual_series(facts, ["Assets"], asof)
    q = lambda t, k: abs(Q[t].get(k, 0))   # 負號（權益變動表的減項）取絕對值
    ratio = dil / dil.shift(1)
    jumps = [int(y) for y, c in ratio.items() if pd.notna(c) and not SHARES_JUMP[0] <= c <= SHARES_JUMP[1]]
    first_ok = max(jumps) if jumps else None
    years = sorted({fy for t in share_tags for fy, _ in Q[t]})
    cand, warn = {}, list(dil.attrs.get("warn", []))   # 會計年度變更過渡年：較早期末的回購數字不用（同資料包）
    for y in years:
        if first_ok is not None and y < first_ok:
            if any(q(t, k) > 0 for t in share_tags for k in Q[t] if k[0] == y):
                warn.append(f"FY{y}：在 FY{first_ok} 稀釋股數跳動（疑似拆股）之前，回購股數為舊基準，不給均價")
            continue
        combos = [(s, v) for s, v in BUYBACK_PAIRS] + [(s, v) for s in share_tags for v in amt_tags]
        pick = None
        for s, v in combos:   # 成對優先；同一組內取同時有股數與金額的最新一份申報
            fl = sorted((k[1] for k in Q[s] if k[0] == y and q(s, k) > 0 and q(v, k) > 0), reverse=True)
            if fl:
                pick = (s, v, fl[0]); break
        if pick is None:
            if any(q(t, k) > 0 for t in share_tags for k in Q[t] if k[0] == y):
                warn.append(f"FY{y}：同一份申報中沒有回購股數與回購金額（或其一為 0），不給均價")
            continue
        s, v, fd = pick
        n, amt = q(s, (y, fd)), q(v, (y, fd))
        paired = (s, v) in BUYBACK_PAIRS
        if not paired:
            pos = [q(t, (y, fd)) for t in share_tags if q(t, (y, fd)) > 0]
            if len(pos) >= 2 and max(pos) / min(pos) > BUYBACK_SHARE_MISMATCH:
                warn.append(f"FY{y}：兩個回購股數概念相差 {max(pos) / min(pos):.1f} 倍且無成對金額，不給均價")
                continue
        d_f = next((Q[t][(y, fd)] for t in F.TAGS["shares"] if (y, fd) in Q[t]), None)
        if d_f and pd.notna(dil.get(y)) and not SHARES_JUMP[0] <= dil[y] / d_f <= SHARES_JUMP[1]:
            warn.append(f"FY{y}：所用申報（{fd}）的股數為拆股前基準（稀釋股數為最新版的 {d_f / dil[y]:.2f} 倍），不給均價")
            continue
        p, sh = amt / n, d_f or dil.get(y)
        if p < BUYBACK_MIN_PRICE or (pd.notna(sh) and pd.notna(assets.get(y)) and assets[y] > 0
                                     and p * sh > BUYBACK_MAX_CAP_ASSETS * assets[y]):
            warn.append(f"FY{y}：均價 {p:,.2f}（{v} ÷ {s}）量級不合理（<{BUYBACK_MIN_PRICE:g} 美元或隱含市值 >總資產 "
                        f"{BUYBACK_MAX_CAP_ASSETS} 倍，疑似單位錯），不給均價")
            continue
        cand[y] = (n, amt, f"{v} ÷ {s}（{fd} 申報）", v in BUYBACK_CF_TAGS)
    avg, basis, shares = {}, {}, {}
    for y, (n, amt, bs, is_cf) in cand.items():
        p = amt / n
        others = [a / m for x, (m, a, _, _) in cand.items() if x != y and abs(x - y) <= 3]
        med = float(pd.Series(others).median()) if others else None
        if med and not 1 / BUYBACK_CF_RANGE <= p / med <= BUYBACK_CF_RANGE:
            warn.append(f"FY{y}：均價 {p:,.2f}（{bs}）與前後 3 年均價中位數 {med:,.2f} 差 >{BUYBACK_CF_RANGE:g} 倍（量級異常，"
                        "可能單位錯、含扣稅股或口徑不同），不給均價")
            continue
        if is_cf:
            warn.append(f"FY{y}：均價用現金流量表回購金額（可能含扣稅股或結算時差），需對原文")
            bs += "（現金流量表金額）"
        if med is None:   # 孤立年度：所有算法都警示（M2-RS1-01）
            warn.append(f"FY{y}：均價 {p:,.2f}（{bs}）前後 3 年無他年均價可比對量級（未比對，量級未驗證；"
                        "可能單位錯），需對原文，不得單獨作為扣分依據")
            bs += "（未比對）"
        shares[int(y)], avg[int(y)], basis[int(y)] = float(n), round(float(p), 4), bs
    cf = latest(BUYBACK_CF_TAGS[0])
    amount = cf
    for _, v in BUYBACK_PAIRS:
        amount = amount.combine_first(latest(v))
    return {"avg_buyback_price_by_fy": avg, "avg_buyback_price_note": BUYBACK_AVG_NOTE, "buyback_shares_by_fy": shares,
            # 金額取絕對值：權益變動表常以負號表示減少，與上面的均價、級距同口徑（A3-M2A-13）
            "buyback_amount_by_fy": {int(y): abs(float(v)) for y, v in amount.items() if v == v},
            "buyback_amount_note": "回購金額取絕對值（申報以負號表示減少者已轉正）",
            "avg_buyback_basis": basis, "avg_buyback_warnings": warn}


def _fmt_cell(k, v):
    if pd.isna(v):
        return "nan"
    if k == "eps":
        return f"{v:,.2f}"
    if k == "shares":
        return f"{v / 1e6:,.1f}"
    return f"{v / 1e6:,.0f}"


def _md_table(rows, cols):
    head = "| | " + " | ".join(cols) + " |"
    return "\n".join([head, "|:--|" + "--:|" * len(cols)] + ["| " + name + " | " + " | ".join(cells) + " |"
                                                              for name, cells in rows])


def render(T, R, bands, filed, ends):
    cols = [f"FY{y}" + (f"（{ends[y]}）" if ends.get(y) else "") for y in T.index]
    rows = [(LABEL[k], [_fmt_cell(k, T[k].get(y)) for y in T.index]) for k in ROW_ORDER if k in T]
    eq = T.equity
    neg = eq.notna() & (eq <= 0)
    na_eq = {"ROE": neg | neg.shift(1, fill_value=False), "權益": neg}   # ROE 用期初期末平均：前一年權益 ≤0 也不適用
    rrows = []
    for c in R.columns:
        cells = []
        for y in T.index:
            v = R[c].get(y)
            m = na_eq["ROE"] if "ROE" in c else na_eq["權益"] if "權益" in c else None
            if pd.isna(v) and m is not None and bool(m.get(y)):
                cells.append(NA_EQ)
            else:
                cells.append("nan" if pd.isna(v) else f"{v:.3f}")
        rrows.append((c, cells))
    brows = [(c, [str(bands[c].get(y)) for y in T.index]) for c in bands.columns]
    return _md_table(rows, cols), _md_table(rrows + brows, cols)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True); ap.add_argument("--asof", required=True)
    ap.add_argument("--outdir", default="dossier"); ap.add_argument("--cache", default="edgar_cache")
    ap.add_argument("--ua", default=None)
    ap.add_argument("--price", type=float); ap.add_argument("--price-date")
    ap.add_argument("--t30", type=float); ap.add_argument("--t30-date"); ap.add_argument("--t30-20y-med", type=float)
    a = ap.parse_args()
    F.UA = a.ua or F.UA
    os.makedirs(a.outdir, exist_ok=True); os.makedirs(a.cache, exist_ok=True)
    m = F.cached(os.path.join(a.cache, "company_tickers.json"), "https://www.sec.gov/files/company_tickers.json")
    t2c = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in m.values()}
    tk = a.ticker.upper(); cik = t2c.get(tk) or t2c.get(tk.replace(".", "-"))
    if not cik:
        sys.exit(f"找不到 {tk} 的 CIK")
    facts = F.cached(os.path.join(a.cache, f"CIK{cik}.json"), f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    T, extra = build(facts, tk, a.asof)
    if T is None:
        sys.exit(f"{tk}：決策日前沒有可用的年度淨利 → MISSING_DATA")
    R, filed, last, warn, bands = extra
    ends = F.annual_series(facts, F.TAGS["net_income"], a.asof).attrs.get("end", {})
    tbl, rtbl = render(T, R, bands, filed, ends)
    L = [f"# 資料包：{tk}（CIK {cik}；決策日 {a.asof}）", "",
         "來源：SEC XBRL companyfacts，10-K 全年值；財年標籤：7–12 月期末＝期末年，1–6 月期末依多數 10-K 的 "
         "DocumentFiscalYearFocus（少數公司可能與自稱差一年，以欄頭括號的期末日為準）；同財年取首次申報版"
         "（股數、EPS 取決策日前最新申報版）；只用申報日早於決策日者。",
         "**本檔不含任何價格、市值、本益比。** 缺值為 nan，不得補假設值。", "",
         "## 10 年財報（百萬美元；EPS 每股美元；股數百萬股）", "", tbl, "",
         "## 衍生比率（由上表計算）", "",
         "回購只給級距（procedure/08 §1，使用者 2026-10-18 決定）：回購金額 ÷ 業主盈餘近似（淨利＋折舊攤銷−資本支出；"
         "框架機械近似，推論，非 SKILL §3 正式業主盈餘）；精確回購金額與回購股數在價格檔，G7 之後才開。", "",
         rtbl, "",
         "## 資料品質警示（只標示、未改數字；DOSSIER_TEXT 工作逐項到原文核對並寫結果）", "",
         *(["- " + w for w in warn] or ["- 無"]), "",
         "## 各財年淨利的 10-K 申報日", "", json.dumps({int(k): v for k, v in filed["net_income"].items()}, ensure_ascii=False), "",
         "## 年報文字節錄（DOSSIER_TEXT 工作追加：業務、分部、風險因素、管理層討論、債務到期表、會計師關鍵查核事項、委託書薪酬；每段標文件、節、申報日）", ""]
    open(os.path.join(a.outdir, f"{tk}.md"), "w", encoding="utf-8").write("\n".join(L))
    # 精確回購金額、回購股數、平均回購價（歷史價格水準）：不放資料包，放價格檔，G7 之後才可比對內在價值
    json.dump({"ticker": tk, "decision_date": a.asof, "price": a.price, "price_date": a.price_date,
               "t30": a.t30, "t30_date": a.t30_date, "t30_20y_median": a.t30_20y_med,
               **buyback_price_data(facts, a.asof),
               "note": "價格隔離：閘門 3 問卷與 G1–G6 存檔後才可開啟本檔"},
              open(os.path.join(a.outdir, f"{tk}_price.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{tk}：財報至 FY{last} → {a.outdir}/{tk}.md、{tk}_price.json")


if __name__ == "__main__":
    main()
