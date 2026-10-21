#!/usr/bin/env python3
"""合併初篩：結構性垃圾刪除 → 統計排除 → 分道 → 研究優先排序 → 前 N 檔（預設 15）交深度分析。

  python screen.py --features features.csv --prices prices.csv --asof 2026-10-15 \
      [--exclusions exclusions.csv] [--overrides screen_overrides.csv] [--universe-note "股票池：…"] \
      [--top 15] [--special-max 0] [--out result]

輸入：fetch_edgar.py 的 features.csv、fetch_prices.py 的 prices.csv（同一 --asof）、可選的制度類排除檔
（欄位 ticker 加任一 excl_asset_class／excl_competence／excl_unvaluable／excl_fraud／excl_recent_reject 為真）。
可選的撤銷檔（欄位 ticker、reason，可有 date；人工複核後撤銷結構性刪除 J*、改 PASS-困境，制度類 X* 不受影響；
有 date 者 12 個月內有效，SKILL.md 第 6 節）。
輸出：result.csv（全池逐檔）、result.md（給人看）、result_handoff.json（交深度分析）。

規則與證據見 ../SKILL.md 與 ../references/evidence_statistical.md／evidence_structural.md。本腳本只排研究順序，不是買進訊號。
"""
import argparse
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, __import__("os").path.dirname(__file__))
import screen_quant as SQ  # noqa: E402  結構性刪除（J1/J3/J4/J7）、困境標記（T1–T5）、會計旗標（F5）

MIN_UNIVERSE_FOR_PCT = 60
# 名單 < 60 檔時的絕對門檻（大型股母體歷史各期分位數的中位數）
ABS = {"vol_36m": 0.35, "earn_yield": 0.025, "roa": 0.025, "roe_fin": 0.08, "drawdown_3y": -0.35}
EXCL_COLS = {"excl_asset_class": "X1_不適用資產", "excl_competence": "X2_能力圈外", "excl_unvaluable": "X3_無法估值",
             "excl_fraud": "X4_誠信紅旗", "excl_recent_reject": "X5_近期已淘汰"}
STRONG_HISTORY = {"loss_yrs5_max": 1, "roe_med5_min": 0.12}
REIT_SIC = 6798                    # REIT 不算金融組，歸非金融組（決策 D20；與 trader pe_median 一致）
TOO_HARD_CAP = 4                   # 前 N 名中 SEL-050 預設太難型最多幾檔，超出的往下遞補（決策 D66）
FORMULA_VERSION = "2026-10-19：六因子（2026-10-17 修訂）＋REIT 非金融（D20）＋太難型上限 4（D66；SIC 對照 D72）＋股數變化僅 >+900% 設缺"
# SIC → SEL-050 預設太難型（berkshire-deep-merged/references/rule_index/01_selection.md「預設太難型清單」）。
# SIC 粗對應屬推論：只決定初篩名額上限，不是深度分析的判定；住宅建商（1531）不在清單（SEL-050 只列不動產開發與 REIT）。
TOO_HARD_SIC = [((5200, 5999), "零售"), ((6500, 6553), "不動產開發"), ((6798, 6798), "REIT"),
                ((3570, 3579), "硬體"), ((3660, 3679), "通訊設備與半導體"), ((4810, 4829), "電信網路"),
                ((4840, 4899), "電信網路"),   # 483x 廣播電視（FOXA 4833）不是電信網路（X21）
                ((8000, 8099), "醫療服務"), ((6324, 6324), "健康保險"), ((3841, 3845), "醫療技術"),
                ((1000, 1499), "能源與礦業採掘"), ((2911, 2911), "煉油"),
                ((2200, 2299), "紡織"), ((2600, 2699), "造紙"), ((2810, 2829), "基礎化學原料"),
                ((2860, 2879), "化學原料與肥料"), ((3310, 3399), "金屬原料"),
                ((4512, 4512), "航空"), ((4522, 4522), "航空"), ((3711, 3711), "汽車製造"),
                ((6282, 6282), "資產管理"), ((6726, 6726), "資產管理"),
                # D72（M1-RS3-02）補 SEL-050 明列且有明確 SIC 者。7371–7379 企業技術服務（SEL-014：企業端維持太難）：
                # 粗對應，7372 套裝軟體與 7374 資料處理也含消費端或資料／支付型（SEL-050 偏好型），只影響名額上限；
                # 7370（GOOGL、META 等可用消費者行為分析者，SEL-014 可能在圈內）不列
                ((7371, 7379), "企業技術服務"), ((7810, 7819), "電影內容製作"), ((7922, 7922), "劇場製作"),
                ((800, 899), "林業"), ((2411, 2411), "林業"), ((6770, 6770), "殼公司")]
# 未對照（SIC 分不開，留給深度分析 SEL-050，SKILL.md 第 4 節列出）：博彩（X21：EDGAR 7990 是雜項娛樂，DIS 主題樂園也是；
# 賭場多申報 7011，與飯店 HLT、MAR 同碼）、廣播電視（483x）、串流（NFLX 7841 是錄影帶出租碼）、航空租賃（7359 混有一般設備租賃，
# 如 URI）、學生貸款等不熟悉的消費金融（6141 含一般消費金融）、生質燃料、需補貼的基礎建設、對沖基金／私募／私人信貸、新型風險承保。
TOO_HARD_SIC_EXEMPT = {5812}       # 餐廳不算零售


# 股數 3 年變化：|變化|>50% 註記；> +900%（3 年股數成 10 倍以上，如 WRB +95,289% 的千倍單位錯誤）才視為單位錯誤設缺值。
# 真實合併／IPO 稀釋（SW +102%、RDDT +253%）保留原值參與排名（百分位不受極端值影響），只註記（M1-R4-04；data_sources.md §4）
SHARE_CHG_NOTE, SHARE_CHG_MAX = 0.5, 9.0


def is_split_ratio(r):
    """拆股／合股比例判定（唯一一份；trader yahoo.py 也用）：r 或 1/r 乘上 1–10 的分母後與整數相差 ≤0.002，
    即整數或簡單分數（2:1、3:2、4:3、5:4、1:10，股票股利 6:5、7:5、11:10…）。其他非整數（Yahoo 的 1.241、1.057、
    0.9535…，分母 100 以上）是分拆（spin-off）的價格調整係數，不是拆股，不得乘到股數（M1-D1-05／M4-D2-13）。
    分母放寬到 10 時 state/cache/yahoo 全部 134 筆事件的分類不變（X21）。"""
    try:
        r = float(r)
    except (TypeError, ValueError):
        return False
    if not r > 0 or r == float("inf"):
        return False
    x = max(r, 1 / r)
    return any(abs(x * d - round(x * d)) <= 0.002 for d in range(1, 11))


def _events(s):
    """'YYYY-MM-DD:ratio;...'（或只有日期）→ [(日期, ratio 字串或 None)]。"""
    out = []
    for item in (s.split(";") if isinstance(s, str) else []):
        if item.strip():
            d, _, r = item.strip().partition(":")
            out.append((d[:10], r or None))
    return out


def split_factor(splits, after_date, upto=None):
    """after_date 之後（且 ≤ upto，若有給）發生的拆股比例連乘（比例＝新股數/舊股數）。splits 格式 'YYYY-MM-DD:ratio;...'。
    不是拆股比例的項（分拆調整係數，見 is_split_ratio）不乘。"""
    if not isinstance(splits, str) or not splits or not isinstance(after_date, str) or not after_date:
        return 1.0
    hi = upto[:10] if isinstance(upto, str) and upto else None
    f = 1.0
    for d, r in _events(splits):
        if r is not None and is_split_ratio(r) and d > after_date[:10] and (hi is None or d <= hi):
            f *= float(r)
    return f


def spinoff_events(splits, spinoffs):
    """分拆事件 [(日期, 係數或 None)]：spinoffs 欄（trader 價格層已分出的 spin_off）＋ splits 中不是拆股比例的項
    （skill 自己的 fetch_prices.py 仍把 Yahoo 分拆係數混在 splits）。"""
    ev = [(d, r) for d, r in _events(splits) if r is not None and not is_split_ratio(r)] + _events(spinoffs)
    return sorted(set(ev), key=lambda e: (e[0], e[1] or ""))   # 同日一筆有係數、一筆無係數時 None 不能和字串比


def too_hard_type(sic):
    """SIC → SEL-050 預設太難型名稱（不屬於回傳 ""）。"""
    try:
        s = int(float(sic))
    except (TypeError, ValueError):
        return ""
    if s in TOO_HARD_SIC_EXEMPT:
        return ""
    return next((name for (lo, hi), name in TOO_HARD_SIC if lo <= s <= hi), "")


def build(features, prices, exclusions, asof):
    df = features.merge(prices, on="ticker", how="left", suffixes=("", "_px"))
    if exclusions is not None:
        df = df.merge(exclusions, on="ticker", how="left")
    # 市值：close（抓取當下的拆股基準）× 封面股數（調整其申報日之後的拆股）；缺封面股數 → 用年報加權股數
    sh, sh_date = df.get("shares_cover"), df.get("shares_cover_filed")
    if sh is None:
        sh, sh_date = pd.Series(np.nan, index=df.index), pd.Series(None, index=df.index)
    use_cover = sh.notna() & (sh > 0)
    shares = np.where(use_cover, sh, df.get("shares"))
    base_date = np.where(use_cover, sh_date, df.get("shares_filed_date"))
    sp = df["splits"] if "splits" in df else pd.Series("", index=df.index)
    adj = [split_factor(s, d) for s, d in zip(sp, base_date)]
    df["shares_mcap"] = pd.to_numeric(pd.Series(shares, index=df.index), errors="coerce") * adj
    df["mcap_shares_source"] = np.where(use_cover, "cover", np.where(df["shares_mcap"].notna(), "annual_weighted", ""))
    df["mcap_shares_date"] = base_date
    # 股數 3 年變化：fy−3 的股數只在較舊 10-K（其申報日後到最新股數申報日之間的拆股未追溯）→ 先調到最新基準
    if "shares_3y_ago" in df:
        f3 = [split_factor(s, a, b) for s, a, b in zip(sp, df["shares_3y_filed"], df.get("shares_filed_date", pd.Series(None, index=df.index)))]
        old = pd.to_numeric(df["shares_3y_ago"], errors="coerce") * f3
        df["share_change_3y"] = pd.to_numeric(df["shares"], errors="coerce") / old.where(old > 0) - 1
    df["market_cap"] = df["price"] * df["shares_mcap"]
    mc = df["market_cap"].where(df["market_cap"] > 0)
    if "sic" in df:  # 產業恐慌標記（T5）用 SIC 前兩碼當產業
        sec2 = pd.to_numeric(df["sic"], errors="coerce").floordiv(100)
        # 無 SIC → 「未分類」（screen_quant T5 不把未分類當產業；舊寫法得 "<NA>" 字串會被當成同一產業，A3-M1A-05）
        df["sector"] = sec2.astype("Int64").astype(str).where(sec2.notna(), "未分類")
    df["earn_yield"] = df["net_income"] / mc
    df["fcf_yield"] = df["fcf"] / mc
    df["norm_earn_yield"] = df["ni5_mean"] / mc
    # 資料品質：盈餘殖利率絕對值 > 100% 多半是股數或單位錯誤 → 視為缺值並註記
    # dq_note 會進價格盲的 screen_context：只寫事件與性質，不寫殖利率、分拆係數等價格衍生值（M1-R6-01；
    # 係數另放 spinoff_factors，只在 result.csv／.md）。缺封面股數改用年報股數只記 mcap_shares_source，
    # 不寫 dq_note（那不是錯誤、與刪除無關，免得刪除被列入「資料品質註記」複核；M1-R5-01）
    bad = df["earn_yield"].abs() > 1.0
    df.loc[bad, ["earn_yield", "fcf_yield", "norm_earn_yield"]] = np.nan
    notes = pd.Series(np.where(bad, "疑似股數或單位錯誤，殖利率類欄位已設缺值", ""), index=df.index)
    factors = {i: [] for i in df.index}

    def add_factors(i, ev):
        factors[i] += [f"{d}:{r}" for d, r in ev if r and f"{d}:{r}" not in factors[i]]
    # 財年後分拆（D69 保守）：市值已是分拆後、最近年報仍含分出事業 → 殖利率設缺（落入資料不足），等分拆後第一份年報。
    # 基準＝最近財年期末 fy_last_end；舊 features 沒有此欄時退用 fy_last_filed（會漏掉期末到申報之間的分拆）
    so = df["spinoffs"] if "spinoffs" in df else pd.Series("", index=df.index)
    fy_end = df["fy_last_end"] if "fy_last_end" in df else pd.Series(None, index=df.index)
    for i, s, o, e, f in zip(df.index, sp, so, fy_end, df.get("fy_last_filed", pd.Series(None, index=df.index))):
        base = e if isinstance(e, str) and e else f
        late = [(d, r) for d, r in spinoff_events(s, o) if isinstance(base, str) and base[:10] < d <= asof[:10]]
        if late:
            df.loc[i, ["earn_yield", "fcf_yield", "norm_earn_yield"]] = np.nan
            add_factors(i, late)
            t = f"財年後分拆 {'、'.join(d for d, _ in late)}：市值為分拆後、最近年報（期末 {base[:10]}）仍含分出事業，殖利率設缺，等分拆後第一份年報"
            notes[i] = f"{notes[i]}；{t}" if notes[i] else t
    # 分拆係數非簡單比例且 <1（純分拆一定 >1）→ 同日藏了合股（HON 0.9535＝1:2 合股×分拆 1.907），股數未調整：
    # 事件落在所用股數基準日之後、決策日之前就註記需人工（X21）
    s3 = df["shares_3y_filed"] if "shares_3y_filed" in df else pd.Series(None, index=df.index)
    for i, s, o, a, b in zip(df.index, sp, so, s3, base_date):
        lo = min([x[:10] for x in (a, b) if isinstance(x, str) and x] or [""])
        mixed = [(d, r) for d, r in spinoff_events(s, o) if r is not None and lo < d <= asof[:10]
                 and pd.to_numeric(r, errors="coerce") < 1]
        if mixed:
            add_factors(i, mixed)
            t = "、".join(d for d, _ in mixed) + " 分拆事件同日含合股、需人工處理（股數未依合股調整）"
            notes[i] = f"{notes[i]}；{t}" if notes[i] else t
    # 股數 3 年變化（拆股調整後）仍很大：註記先查拆股／合併；> +900% 是單位錯誤 → 設缺值（計中性）
    chg = pd.to_numeric(df.get("share_change_3y", pd.Series(np.nan, index=df.index)), errors="coerce")
    for i in df.index[chg.abs() > SHARE_CHG_NOTE]:
        t = f"股數 3 年變化 {chg[i]:+.0%}：先查拆股／合併" + ("，疑似單位錯誤、已設缺值" if chg[i] > SHARE_CHG_MAX else "")
        notes[i] = f"{notes[i]}；{t}" if notes[i] else t
    df.loc[chg > SHARE_CHG_MAX, "share_change_3y"] = np.nan
    df["dq_note"] = notes
    df["spinoff_factors"] = pd.Series({i: ";".join(v) for i, v in factors.items()})   # 分拆價格調整係數（兩個股價的比值，價格衍生）：不進交接檔
    # fetch_edgar 的 eps_cagr5 未做拆股調整（5 年前 EPS 取自拆股前的舊年報），初篩不用 → 不帶進 result.csv（M1-RS1-01）
    df = df.drop(columns=["eps_cagr5"], errors="ignore")
    if "data_notes" not in df:   # 交接檔每檔都帶 data_notes 鍵（M1-D5-01）
        df["data_notes"] = ""
    return df


OVERRIDE_DISTRESS = "R_人工撤銷刪除"   # 非價格困境碼：撤銷結構性刪除者改 PASS-困境、必走步 4A（SKILL 第 6 節第 2 點；M1-R4-02）
OVERRIDE_MAX_DAYS = 365                # 撤銷自 date 起 12 個月內有效，比照 X5 的 12 個月窗（M1-R4-03）


def load_overrides(o, asof):
    """撤銷檔（ticker、reason，可有 date）→ (生效 {代號: 理由}, 過期 [{ticker, date, reason}])。
    有 date 者只在 0 ≤ 決策日−date < 365 天生效；滿 12 個月列為過期、需重新複核（要續用就寫一列新日期）；
    date 晚於決策日者不生效。沒有 date（單次人工執行）者照用。"""
    active, expired = {}, []
    for r in o.fillna("").astype(str).to_dict("records"):
        t, why, d = r.get("ticker", ""), r.get("reason", ""), r.get("date", "")[:10]
        if not t:
            continue
        age = (pd.Timestamp(asof[:10]) - pd.to_datetime(d, errors="coerce")).days if d else 0
        if pd.isna(age):   # 日期寫錯 → 不生效
            continue
        if age >= OVERRIDE_MAX_DAYS:
            expired.append({"ticker": t, "date": d, "reason": why})
        elif age >= 0:
            active[t] = why
    expired = [x for x in expired if x["ticker"] not in active]   # 同代號有較新的生效列 → 不算過期
    return active, expired


def structural(df, asof, overrides=None, expired=None):
    """結構性刪除（多年虧損／結構性損害）與制度類排除。回傳 delete_codes、distress_codes、flag_codes。
    overrides：{代號: 理由}，人工複核後撤銷結構性刪除（J*），補非價格困境碼 R_人工撤銷刪除；撤銷紀錄寫 override_note（D62）。
    expired：load_overrides 判定過期的撤銷，記在 override_expired（不撤銷，提醒重新複核）。"""
    sq_in = df.copy()   # REIT 歸非金融組（D20），F5 也照非金融評
    if "sic" in sq_in and "is_financial" in sq_in:
        sq_in["is_financial"] = sq_in["is_financial"].where(pd.to_numeric(sq_in["sic"], errors="coerce") != REIT_SIC, False)
    q = SQ.screen(SQ.from_live(sq_in, int(asof[:4])), decision_date=asof)
    for c in ("delete_codes", "distress_codes", "flag_codes", "pit_checked"):
        df[c] = q[c].values
    df["override_note"] = ""
    for i in df.index[df["ticker"].isin(list(overrides or {}))]:
        j = [c for c in str(df.at[i, "delete_codes"] or "").split(";") if c]
        if j:
            df.at[i, "delete_codes"] = ""
            df.at[i, "override_note"] = f"撤銷 {';'.join(j)}：{overrides[df.at[i, 'ticker']]}"
            dc = df.at[i, "distress_codes"]
            df.at[i, "distress_codes"] = ";".join(c for c in (dc if isinstance(dc, str) else "", OVERRIDE_DISTRESS) if c)
    ex = {x["ticker"]: f"{x['date']} 起滿 12 個月：{x['reason']}" for x in (expired or [])}
    df["override_expired"] = df["ticker"].map(ex).fillna("")
    inst = pd.Series("", index=df.index)
    for c, code in EXCL_COLS.items():
        if c in df:
            m = df[c].fillna(False).astype(str).str.lower().isin(["1", "1.0", "true", "yes", "y"])
            inst = inst.where(~m, inst + code + ";")
    df["delete_codes"] = (df["delete_codes"].fillna("") + ";" + inst.str.rstrip(";")).str.strip(";")
    return df


# 研究優先因子（缺值計 0.5）與結構性規則的輸入欄；缺值者記進 unrated_fields（M1-D5-01／D5-02）
FACTORS = {0: ["fcf_yield", "norm_earn_yield", "roe_min5", "op_margin_5y_std", "debt_to_equity", "share_change_3y"],
           1: ["share_change_3y", "roe", "roa"]}
RULE_INPUTS = {"J1": ["loss_yrs5", "ni_sum5"], "J3": ["shareholders_equity", "net_income"],
               "J4": ["net_income", "eq_prev", "shareholders_equity", "loss_yrs5", "roe_med5"],
               "J7": ["net_income", "ni_prev", "ni_sum5"]}


def unrated_fields(df):
    """逐檔列出被補 0.5 的研究優先因子，以及因缺值無法評估的結構性規則（未觸發者，如 J1:loss_yrs5）。只有欄名、不含數值。"""
    out = []
    for i, r in df.iterrows():
        u = [c for c in FACTORS[int(r["fin_like"])] if c not in df or pd.isna(r[c])]
        for rule, cols in RULE_INPUTS.items():
            settled = (rule == "J1" and r.get("loss_yrs5", np.nan) < 3) or (rule != "J1" and r.get("net_income", np.nan) >= 0)
            if rule not in str(r.get("delete_codes", "")) and not settled:   # 已知不會觸發者不列
                u += [f"{rule}:{c}" for c in cols if c not in df or pd.isna(r[c])]
        out.append(";".join(u))
    return pd.Series(out, index=df.index)


def statistical(df, top, special_max, too_hard_cap=TOO_HARD_CAP):
    fin = df["is_financial"].fillna(False).astype(str).str.lower().isin(["1", "true", "1.0"])
    nofin = ~fin & df["is_financial"].isna()
    # 無產業碼時才近似：負債/資產 > 80%、權益為正、ROA < 3%（排除以回購造成負權益或高負債的非金融公司）
    approx = (df["liab_to_assets"].fillna(0) > 0.8) & (df["shareholders_equity"] > 0) & (df["roa"] < 0.03)
    fin = fin | (nofin & approx.fillna(False))
    fin = fin & ~(pd.to_numeric(df.get("sic", pd.Series(np.nan, index=df.index)), errors="coerce") == REIT_SIC)  # D20
    df["fin_source"] = np.where(nofin, "近似（無 SIC）", "SIC")
    df["fin_like"] = fin.astype(int)
    deleted = df["delete_codes"] != ""
    pool = df                                                          # 百分位以全池計（與實證研究的母體定義一致）
    use_pct = len(pool) >= MIN_UNIVERSE_FOR_PCT

    def pct(col, group=False):
        s = pool.groupby("fin_like")[col].rank(pct=True) if group else pool[col].rank(pct=True)
        return s.reindex(df.index)

    reasons = pd.Series("", index=df.index)

    def flag(mask, text):
        nonlocal reasons
        mask = mask.fillna(False) & ~deleted
        reasons = reasons.where(~mask, reasons + text + "；")

    if use_pct:
        flag(pct("vol_36m") >= 0.8, "股價波動屬最高 20%")
        flag((pct("earn_yield") <= 0.2) | (df["earn_yield"] <= 0), "盈餘殖利率屬最低 20% 或虧損")
        flag((df.fin_like == 0) & (pct("roa", True) <= 0.2), "資產報酬率屬非金融最低 20%")
        flag((df.fin_like == 1) & (pct("roe", True) <= 0.2), "ROE 屬金融最低 20%")
        deep = pct("drawdown_3y") <= 0.2
    else:
        flag(df["vol_36m"] > ABS["vol_36m"], f"年化波動 > {ABS['vol_36m']:.0%}")
        flag(df["earn_yield"] < ABS["earn_yield"], f"盈餘殖利率 < {ABS['earn_yield']:.1%} 或虧損")
        flag((df.fin_like == 0) & (df["roa"] < ABS["roa"]), f"ROA < {ABS['roa']:.1%}")
        flag((df.fin_like == 1) & (df["roe"] < ABS["roe_fin"]), f"金融 ROE < {ABS['roe_fin']:.0%}")
        deep = df["drawdown_3y"] < ABS["drawdown_3y"]
    df["exclude_reasons"] = reasons.str.rstrip("；")
    excluded = df["exclude_reasons"] != ""
    deep = deep.fillna(False) & ~deleted

    def g(col, higher=True):
        s = pct(col, True).fillna(0.5)
        return s if higher else 1 - s
    # 2026-10-17：營收成長改中性，換成 5 年最差 ROE（負權益年度為缺值 → 0.5 中性）；理由見 evidence_statistical.md §2
    nonfin = (g("fcf_yield") + g("norm_earn_yield") + g("roe_min5") + g("op_margin_5y_std", False)
              + g("debt_to_equity", False) + g("share_change_3y", False)) / 6
    finsc = (g("share_change_3y", False) + g("roe") + g("roa")) / 3
    raw = pd.Series(np.where(df.fin_like == 1, finsc, nonfin), index=df.index)
    # 排序用未四捨五入值＋代號（穩定排序），顯示用三位小數（M1-D1-07）
    df["research_priority_raw"] = raw.where(~deleted).groupby(df.fin_like).rank(pct=True)
    df["research_priority"] = df["research_priority_raw"].round(3)
    df["unrated_fields"] = unrated_fields(df)

    lacking = df[["vol_36m", "earn_yield"]].isna().any(axis=1)
    strong = (df["loss_yrs5"] <= STRONG_HISTORY["loss_yrs5_max"]) & (df["roe_med5"] >= STRONG_HISTORY["roe_med5_min"])
    df["strong_history"] = strong.fillna(False)
    df["lane"] = np.select(
        [deleted, excluded & deep, excluded & df["strong_history"] & (df["distress_codes"].fillna("") != ""), excluded, lacking, deep],
        ["刪除（結構性損害／制度類）", "特殊情境（被排除且深度回撤）", "特殊情境（困境中但歷史經濟力佳）",
         "排除（統計高風險）", "資料不足", "特殊情境（深度回撤）"],
        default="核心道")
    core = df[df.lane == "核心道"].sort_values(["research_priority_raw", "ticker"], ascending=[False, True],
                                               kind="mergesort")
    # 前 top 名中 SEL-050 預設太難型最多 too_hard_cap 檔，超出的往下遞補（D66）；其餘名次照原順序
    df["sel050_type"] = df["sic"].map(too_hard_type) if "sic" in df else ""
    order, n_hard = [], 0
    for i in core.index:
        if len(order) == top:
            break
        if df.at[i, "sel050_type"]:
            if n_hard >= too_hard_cap:
                continue
            n_hard += 1
        order.append(i)
    picked = set(order)
    order += [i for i in core.index if i not in picked]
    df["core_rank"] = np.nan
    df.loc[order, "core_rank"] = range(1, len(order) + 1)
    # 短名單＝迴圈選中者（A3-M1A-01）：非太難型不足 top 時不拿超額的太難型補位，名單可少於 top（md 註明實得幾檔）
    df["shortlist"] = df.index.isin(picked)
    # 特殊情境交棒（預設 0 檔）：只交「歷史經濟力佳」者（5 年虧損 ≤1、5 年 ROE 中位 ≥12%），依 5 年最差 ROE 由高到低
    sp = df[df.lane.str.startswith("特殊情境") & df["strong_history"]]
    sp = sp.sort_values(["roe_min5", "roe_med5"], ascending=False).head(special_max)
    df["special_handoff"] = df.index.isin(sp.index)
    return df, use_pct


HANDOFF_COLS = ["ticker", "name", "core_rank", "research_priority", "lane", "market_cap", "price", "price_date",
                "earn_yield", "fcf_yield", "norm_earn_yield", "roe", "roe_med5", "roe_min5", "roa", "rev_cagr_5y",
                "op_margin_5y_std", "debt_to_equity", "share_change_3y", "vol_36m", "drawdown_3y", "fin_like",
                "exclude_reasons", "delete_codes", "distress_codes", "flag_codes", "fy_last", "fy_last_filed",
                "max_filed_used", "dq_note", "data_notes", "unrated_fields", "mcap_shares_source"]


# 由價格衍生、或會洩露價格／排名的欄位：交接檔另放 price_context，深度分析前半（G1–G6、閘門 3）不得看到
# lane（特殊情境道名含「深度回撤」）與 exclude_reasons（股價波動、盈餘殖利率）由價格衍生，也只放 price_context（P39）
PRICE_COLS = ["core_rank", "research_priority", "market_cap", "price", "price_date", "earn_yield", "fcf_yield",
              "norm_earn_yield", "vol_36m", "drawdown_3y", "lane", "exclude_reasons"]


def records(d, split=False):
    keep = [c for c in HANDOFF_COLS if c in d.columns]
    rows = json.loads(d[keep].to_json(orient="records", force_ascii=False))
    if split:
        for r in rows:
            r["price_context"] = {c: r.pop(c) for c in PRICE_COLS if c in r}
    return rows


PRICE_DISTRESS = ("T3_", "T5_")   # 價格類困境碼（D71）：只放 price_context，不影響 screen_decision
SPECIAL_MARK = "S_特殊情境交棒"     # 特殊情境交棒必做困境專門檢查（SKILL 第 4 節）；入道條件含價格，標記放 price_context


def lacking_list(df):
    """資料不足道：缺哪個欄位＋原因註記（不含數值）。"""
    d = df[df.lane == "資料不足"]
    return [{"ticker": r.ticker, "missing": [c for c in ("vol_36m", "earn_yield") if pd.isna(getattr(r, c))],
             "data_notes": "" if pd.isna(getattr(r, "data_notes", None)) else str(r.data_notes),
             "dq_note": r.dq_note} for r in d.itertuples()]


def handoff(df, asof, out):
    def ctx(d, special=False):
        r = records(d, split=True)
        for x in r:
            codes = [c for c in str(x.get("distress_codes") or "").split(";") if c]
            px = [c for c in codes if c.startswith(PRICE_DISTRESS)] + ([SPECIAL_MARK] if special else [])
            x["distress_codes"] = ";".join(c for c in codes if not c.startswith(PRICE_DISTRESS))
            x["price_context"]["price_distress_codes"] = ";".join(px)
            x["screen_decision"] = ("PASS-困境" if x["distress_codes"] else "PASS")
        return r
    payload = {
        "asof": asof,
        "formula_version": FORMULA_VERSION,
        "core_shortlist": ctx(df[df.shortlist].sort_values("core_rank")),
        "special_handoff": ctx(df[df.special_handoff], special=True),
        "special_watch": records(df[df.lane.str.startswith("特殊情境")].sort_values("drawdown_3y")),
        "counts": df["lane"].value_counts().to_dict(),
        "deleted": [{"ticker": t, "codes": c} for t, c in zip(df.ticker, df.delete_codes) if c],
        "lacking": lacking_list(df),
        "overrides": [{"ticker": t, "note": n} for t, n in zip(df.ticker, df.get("override_note", [""] * len(df))) if n],
        "overrides_expired": [{"ticker": t, "note": n} for t, n in
                              zip(df.ticker, df.get("override_expired", [""] * len(df))) if n],
        "note": "初篩只降低永久損失機率並排出研究順序，不是買進訊號；排名不得作為任何閘門的理由。"
                "core_shortlist／special_handoff 每檔的 price_context 含價格、排名與價格類困境碼（T3／T5），深度分析前半不得提供。"
                "overrides 只撤銷結構性刪除：撤銷不保證進短名單（撤銷者照常分道與排名）；要深度分析須手動 trader analyze --tickers 送入（D95）。"
                "special_watch 含價格、回撤等價格類資料，只供人工觀察，不得交深度分析前半。",
    }
    with open(out + "_handoff.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def report(df, use_pct, out, top, asof, universe_note=None, not_evaluated=()):
    # 評估檔數＝財報輸入列數；股票池檔數是框架去重前的名單，兩者可差幾檔（M1-R5-03）
    gap = (f"評估檔數以財報輸入為準（同公司多類別股只留一類、缺財報者不評估），可少於股票池檔數"
           + (f"；價格檔有、財報輸入無：{'、'.join(not_evaluated)}" if len(not_evaluated) else "") + "。")
    L = [f"# 初篩結果（決策日 {asof}，評估 {len(df)} 檔）", ""] + ([universe_note, gap, ""] if universe_note else []) + [
         "門檻：" + ("本次名單橫斷面百分位" if use_pct else f"名單少於 {MIN_UNIVERSE_FOR_PCT} 檔，改用絕對門檻（證據較弱）"),
         f"排名公式：{FORMULA_VERSION}", ""]
    L += ["| 分道 | 檔數 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in df["lane"].value_counts().items()]

    def tbl(d, cols):
        cols = [c for c in cols if c in d.columns]
        if not len(d):
            return "（無）"
        rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for _, r in d[cols].iterrows():
            rows.append("| " + " | ".join("" if pd.isna(v) else (f"{v:.3f}" if isinstance(v, float) else str(v)) for v in r) + " |")
        return "\n".join(rows)
    n_sl = int(df["shortlist"].sum())
    short = (f"；實得 {n_sl} 檔：核心道合格者不足，或 SEL-050 預設太難型超過上限 {TOO_HARD_CAP} 檔不補位"
             if n_sl < top else "")
    L += ["", f"## 核心短名單（前 {top}{short}，交深度分析）", "",
          tbl(df[df.shortlist].sort_values("core_rank"), ["ticker", "name", "core_rank", "research_priority", "earn_yield",
              "fcf_yield", "roe", "op_margin_5y_std", "debt_to_equity", "share_change_3y", "distress_codes", "flag_codes",
              "sel050_type", "unrated_fields"])]
    sp = df[df.lane.str.startswith("特殊情境")]
    L += ["", f"## 特殊情境（不是候選；只有寫得出危機論點時才研究；依 3 年回撤列前 {min(25, len(sp))}／共 {len(sp)} 檔）", "",
          tbl(sp.sort_values("drawdown_3y").head(25),
              ["ticker", "name", "lane", "drawdown_3y", "roe_med5", "roe_min5", "strong_history", "special_handoff", "exclude_reasons"])]
    dl = df[df.delete_codes != ""]
    L += ["", f"## 刪除清單（{len(dl)} 檔；每筆都要人工複核，見 SKILL.md 第 6 節）", "",
          tbl(dl, ["ticker", "name", "delete_codes", "net_income", "shareholders_equity", "eq_prev", "loss_yrs5", "roe_med5",
                   "strong_history", "fin_like", "fin_source", "fy_last_filed"])]
    ov = df[df.get("override_note", pd.Series("", index=df.index)).fillna("") != ""]
    if len(ov):
        L += ["", f"## 已撤銷的刪除（{len(ov)} 檔；screen_overrides；撤銷不保證進短名單，要深度分析須手動 trader analyze --tickers）", "", tbl(ov, ["ticker", "name", "override_note", "lane"])]
    oe = df[df.get("override_expired", pd.Series("", index=df.index)).fillna("") != ""]
    if len(oe):
        L += ["", f"## 已過期的撤銷（{len(oe)} 檔；滿 12 個月不再撤銷，重新複核後要續用請寫新日期）", "",
              tbl(oe, ["ticker", "name", "override_expired", "delete_codes"])]
    lk = pd.DataFrame(lacking_list(df))
    if len(lk):
        lk["missing"] = lk["missing"].map("、".join)
    L += ["", f"## 資料不足（{len(lk)} 檔；補資料再判）", "", tbl(lk, ["ticker", "missing", "data_notes", "dq_note"])]
    aw = df.get("mcap_shares_source", pd.Series("", index=df.index)) == "annual_weighted"   # M1-R5-01：不寫 dq_note，這裡列
    dq = df[(df.dq_note.fillna("") != "") | aw]
    L += ["", f"## 資料品質註記（{len(dq)} 檔；逐筆到申報核對，見 SKILL.md 第 6 節）", "",
          tbl(dq, ["ticker", "name", "lane", "dq_note", "mcap_shares_source", "spinoff_factors"])]
    with open(out + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def check_price_dates(px, asof):
    """prices.csv 的 price_date 不得晚於決策日（防前視，A3-M1E-06）；違反回傳錯誤訊息，否則 None。"""
    if "price_date" not in px:
        return None
    d = pd.to_datetime(px["price_date"], errors="coerce").dropna()
    lim = pd.Timestamp(asof).normalize()
    if len(d) and d.max().normalize() > lim:
        bad = px.loc[d[d.dt.normalize() > lim].index, "ticker"].astype(str).tolist()
        return (f"prices.csv 的 price_date 最晚 {d.max().date()} 晚於 --asof {asof}（{'、'.join(bad[:10])}）："
                "前視，請以同一決策日重抓價格")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--features", required=True)
    ap.add_argument("--prices", required=True)
    ap.add_argument("--asof", required=True, help="YYYY-MM-DD；須與抓資料時相同")
    ap.add_argument("--exclusions")
    ap.add_argument("--overrides", help="撤銷結構性刪除的 csv（ticker、reason；有 date 欄者 12 個月內有效）")
    ap.add_argument("--universe-note", help="寫在 md 第一段的股票池來源與日期")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--special-max", type=int, default=0, help="特殊情境道交深度分析的檔數上限（預設 0）")
    ap.add_argument("--out", default="result")
    a = ap.parse_args()
    feat, px = pd.read_csv(a.features), pd.read_csv(a.prices)
    if "asof" in feat and feat["asof"].dropna().astype(str).ne(a.asof).any():
        sys.exit("features.csv 的 asof 與 --asof 不同：請以同一決策日重抓")
    err = check_price_dates(px, a.asof)
    if err:
        sys.exit(err)
    ex = pd.read_csv(a.exclusions) if a.exclusions else None
    df = build(feat, px, ex, a.asof)
    ov, ov_expired = None, None
    if a.overrides:
        ov, ov_expired = load_overrides(pd.read_csv(a.overrides, dtype=str), a.asof)
    df = structural(df, a.asof, ov, ov_expired)
    df, use_pct = statistical(df, a.top, a.special_max)
    df.to_csv(a.out + ".csv", index=False)
    handoff(df, a.asof, a.out)
    report(df, use_pct, a.out, a.top, a.asof, a.universe_note, sorted(set(px["ticker"]) - set(feat["ticker"])))
    print(f"共 {len(df)} 檔；" + "，".join(f"{k}：{v}" for k, v in df["lane"].value_counts().items()))
    print(f"短名單 {int(df.shortlist.sum())} 檔；特殊情境交棒 {int(df.special_handoff.sum())} 檔 → {a.out}_handoff.json")


if __name__ == "__main__":
    main()
