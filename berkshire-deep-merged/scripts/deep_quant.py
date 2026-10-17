#!/usr/bin/env python3
"""深度審查（決定買）的可量化部分（X100 版＋X102 自動買進三閘門與部位）：排除級紅旗 → 困境三問（可量化代理）→ 跨循環確定性等級 → 利率感知估值上限 → 買／觀察／不買。
這只是深度審查的數字骨架；能力圈、護城河、管理層、產業與困境的質性判斷照 ../SKILL.md 逐步做，質性不通過即不買。

輸入：berkshire-screen-merged/scripts/screen_quant.screen() 的輸出，另需跨循環欄位（由 tc_features() 從 10 年財報算出）：
  n_tc, loss_tc, loss_tc_ex, roe_med_tc_ex, roe_sd_ex, roe_min_ex, ni_norm_ex5, t30（決策日前最後一個 30 年期公債殖利率，小數）
以及 X99 欄位 ni5_mean、ni5_med、roe_sd5、roe_min5、roe_med5、n_roe5、loss_yrs5、eps_cagr5、de、pe、mcap、ocf_sum5_c、nonfin、util、screen_decision。

規則出處見 ../references/05_valuation_caps.md、06_certainty_position.md、11_distress_check.md 與 10_sources_and_merge.md（X100 節）。
X102：auto_buy_gates() 把「自動買進」限定為三閘門全過（閘門 1 初篩未刪、閘門 2 現價 < 買價上限、閘門 3 固定問卷通過，見 gate3_check.py）；
initial_position()、add_allowed() 依 v3.1 P-28、P-33、P-36、P-42、P-43 算首批與加碼（references/12_auto_buy_gates.md）。
"""
import numpy as np, pandas as pd

# ---- 參數（X100 固定預設；選法見 references/10_sources_and_merge.md X100 節，網格與目標事先寫定於測試規格） ----
STAB_SD = 0.04        # 跨循環 ROE 標準差上限（已剔除產業共同衝擊年）
G_MAX = 0.10          # 隱含成長絕對上限
G_NOM_SPREAD = -0.01  # 名目經濟成長代理＝30 年期殖利率＋此值（推論）
R_FLOOR = 0.10        # r_d 下限（「至少 10% 稅前」）
R_SPREAD = 0.03       # r_d＝max(下限, 30 年期＋3pp)
SHOCK_RATIO = 0.5     # 產業共同衝擊年：該年產業中位 ROE ≤0 或 < 前 5 年產業中位的一半


def _pv(g0, r, gl=0.04, gt=None):
    t = np.arange(1, 11); gend = min(g0, gl); gs = g0 + (gend - g0) * (t - 1) / 9
    cf = np.cumprod(1 + gs); disc = (1 + r) ** t; g_term = 0.0 if gt is None else gt
    return (cf / disc).sum() + cf[-1] * (1 + g_term) / (r - g_term) / (1 + r) ** 10


def _g_impl(mult, r, gt=0.03):
    """市價隱含的前 10 年平均成長（10 年遞減至 4%，之後永續 gt）。"""
    if not (mult > 0) or mult > 200: return np.nan
    lo, hi = -0.5, 1.0; f = lambda g: _pv(g, r, 0.04, gt)
    if f(lo) > mult: return lo
    if f(hi) < mult: return hi
    for _ in range(50):
        m = (lo + hi) / 2
        if f(m) < mult: lo = m
        else: hi = m
    return (lo + hi) / 2


def shock_years(F, sector_of):
    """F：多家公司年度財報（cik, fy, net_income, shareholders_equity）。回傳 {(產業, fy): 是否共同衝擊年}。
    共同衝擊＝同產業 ≥5 家的中位 ROE ≤0 或 < 前 5 年（至少 3 年）產業中位的一半——整個產業同時受創，不是個別公司的結構問題。"""
    F = F.sort_values(['cik', 'fy']).copy()
    F['eq_prev'] = F.groupby('cik').shareholders_equity.shift(1)
    avg = (F.shareholders_equity + F.eq_prev) / 2
    F['roe_f'] = np.where(avg > 0, F.net_income / avg, np.where(F.shareholders_equity > 0, F.net_income / F.shareholders_equity, np.nan))
    F['sector'] = F.cik.map(sector_of)
    med = F.groupby(['sector', 'fy']).agg(m=('roe_f', 'median'), n=('roe_f', 'count')).reset_index()
    med = med[med.n >= 5].sort_values(['sector', 'fy'])
    med['prior'] = med.groupby('sector').m.transform(lambda s: s.shift(1).rolling(5, min_periods=3).median())
    med['shock'] = (med.m <= 0) | (med.m < SHOCK_RATIO * med.prior)
    return {(s, f): bool(v) for s, f, v in zip(med.sector, med.fy, med.shock)}


def company_tc(hist, sector, shock, fy_last):
    """單一公司跨循環欄位。hist：該公司年度財報（fy, net_income, shareholders_equity）；fy_last＝決策年−1（最近一個已申報財年）。
    窗口＝fy_last−9…fy_last；shock：shock_years() 的結果。回傳 dict（欄位同 deep() 需要的跨循環欄位）。"""
    h = hist.sort_values('fy').drop_duplicates('fy', keep='last').copy()
    h['eq_prev'] = h.shareholders_equity.shift(1); avg = (h.shareholders_equity + h.eq_prev) / 2
    h['roe_f'] = np.where(avg > 0, h.net_income / avg, np.where(h.shareholders_equity > 0, h.net_income / h.shareholders_equity, np.nan))
    fy = h.fy.values; ni = h.net_income.values.astype(float); roe = h.roe_f.values
    sh = np.array([shock.get((sector, f), False) for f in fy])
    m = (fy >= fy_last - 9) & (fy <= fy_last) & ~np.isnan(ni); mx = m & ~sh
    r_ex = roe[mx]; r_ex = r_ex[~np.isnan(r_ex)]
    m5 = (fy >= fy_last - 4) & (fy <= fy_last) & ~np.isnan(ni) & ~sh
    return dict(n_tc=int(m.sum()), loss_tc=int((ni[m] < 0).sum()), loss_tc_ex=int((ni[mx] < 0).sum()),
                roe_med_tc_ex=float(np.median(r_ex)) if len(r_ex) else np.nan,
                roe_sd_ex=float(np.std(r_ex, ddof=1)) if len(r_ex) >= 4 else np.nan,
                roe_min_ex=float(np.min(r_ex)) if len(r_ex) else np.nan,
                ni_norm_ex5=float(np.median(ni[m5])) if m5.sum() >= 3 else np.nan)


def deep(sdf, STAB_SD=STAB_SD, G_MAX=G_MAX, G_NOM_SPREAD=G_NOM_SPREAD, R_FLOOR=R_FLOOR, R_SPREAD=R_SPREAD):
    d = sdf.copy()
    for c in ['n_tc', 'loss_tc', 'loss_tc_ex', 'roe_med_tc_ex', 'roe_sd_ex', 'roe_min_ex', 'ni_norm_ex5', 't30']:
        if c not in d: d[c] = np.nan          # 缺跨循環欄位 → 退回 5 年口徑；缺利率 → r_d 取下限、g_term 取 3%（訊號卡註記）
    d['mcap'] = d['mcap'].where(d['mcap'].notna(), d['pe'] * d['net_income'])
    nf = d.nonfin
    distress = d.screen_decision.eq('PASS-困境')
    conv = d.ocf_sum5_c / d.ni_sum5
    rf = pd.DataFrame({
        'R1_權益非正': d.shareholders_equity <= 0,
        'R2_現金轉換<0.5': nf & (d.ni_sum5 > 0) & (conv < 0.5),
        'R3_有息負債權益>3': nf & ~d.util.astype(bool) & (d.de > 3),
    }).fillna(False)
    d['red_flags'] = rf.apply(lambda r: ';'.join([k for k, v in r.items() if v]), axis=1)
    red = rf.any(axis=1)
    has_tc = d.n_tc.fillna(0) >= 5
    # 跨循環欄位（剔除產業共同衝擊年）；10 年資料不足 5 年 → 退回 5 年口徑
    loss_ex = np.where(has_tc, d.loss_tc_ex, d.loss_yrs5)
    loss_all = np.where(has_tc, d.loss_tc, d.loss_yrs5)
    roe_med = np.where(has_tc, d.roe_med_tc_ex, d.roe_med5)
    roe_min = np.where(has_tc, d.roe_min_ex, d.roe_min5)
    sd = np.where(has_tc & d.roe_sd_ex.notna(), d.roe_sd_ex, d.roe_sd5)
    loss_ex = pd.Series(loss_ex, index=d.index).astype(float); roe_med = pd.Series(roe_med, index=d.index).astype(float)
    # 困境三問（可量化部分）：跨循環經濟力佳（非衝擊年虧損 ≤1、非衝擊年 ROE 中位 ≥12%）、無紅旗
    hist_good = (loss_ex.fillna(9) <= 1) & (roe_med >= 0.12)
    d['dist_ok'] = distress & hist_good & ~red
    # 正常化盈餘：近 5 年非衝擊年淨利中位（≥3 年）；不足則 5 年平均（困境三問通過者用 5 年中位）
    e_fb = np.where(d.dist_ok, d.ni5_med, d.ni5_mean)
    d['E_norm'] = np.where(d.ni_norm_ex5.notna(), d.ni_norm_ex5, e_fb)
    stab = (pd.Series(sd, index=d.index) <= STAB_SD) | d.dist_ok
    A = (loss_ex == 0) & (pd.Series(loss_all, index=d.index) <= 1) & ((d.n_tc.fillna(0) >= 5) | (d.n_roe5 >= 5)) & (pd.Series(roe_min, index=d.index) >= 0.10) & stab
    Bg = (loss_ex <= 1) & (roe_med >= 0.10) & stab
    C = (loss_ex <= 2) & (d.E_norm > 0)
    g = np.where(A, 'A', np.where(Bg, 'B', np.where(C, 'C', 'D')))
    warn = (nf & (d.ni_sum5 > 0) & (conv >= 0.5) & (conv < 0.8)).fillna(False)
    down = {'A': 'B', 'B': 'C', 'C': 'D', 'D': 'D'}
    g = np.where(warn, [down[x] for x in g], g)
    g = np.where(distress & ~d.dist_ok, [down[x] for x in g], g)
    d['grade'] = g
    # 估值：r_d 與利率連動（有下限）；永續成長＝min(3%, 名目經濟成長代理＝30 年期＋G_NOM_SPREAD)（推論）；
    # 隱含成長（前 10 年）上限 G_MAX。低利率年份永續成長下修 → 同一價格的隱含成長變高 → 上限自動變嚴。
    t30 = d.t30.astype(float)
    d['r_d'] = np.maximum(R_FLOOR, (t30 + R_SPREAD).fillna(R_FLOOR))
    d['g_term'] = np.clip((t30 + G_NOM_SPREAD).fillna(0.03), 0.0, 0.03)
    mult = d.mcap / d.E_norm
    d['g_impl'] = [(_g_impl(m, r, gt) if m == m else np.nan) for m, r, gt in zip(mult, d.r_d, d.g_term)]
    d['g_cap'] = G_MAX
    elig = pd.Series(g).isin(['A', 'B']).values & ~red & (d.E_norm > 0) & ~d.screen_decision.eq('DELETE')
    ok = elig & d.mcap.notna() & (d.g_impl <= d.g_cap)
    watch = ~ok & (pd.Series(g).isin(['A', 'B', 'C']).values) & ~red & (d.E_norm > 0) & ~d.screen_decision.eq('DELETE')
    d['deep_decision'] = np.where(ok, '買', np.where(watch, '觀察', '不買'))
    # 報告欄（不是硬上限）：P1＝等級係數 × 保守價值；P4′＝相對倍數
    GF = {'A': 0.85, 'B': 0.75, 'C': 0.40}
    g0 = (0.75 * np.fmin(d.eps_cagr5, d.rev_cagr5)).clip(-0.05, 0.08).fillna(0.0)
    d['IV_cons'] = d.E_norm * np.array([_pv(x, r, 0.04, None) for x, r in zip(g0, d.r_d)])
    d['P1_mcap'] = pd.Series(g).map(GF).values * d.IV_cons
    d['P4_mcap'] = np.where(g == 'A', 1.2, 1.0) * d.groupby('Y')['pe'].transform('median') * d.E_norm
    return d


# ---- X102 自動買進三閘門與部位（references/12_auto_buy_gates.md；數字全部沿用 v3.1，不新設上限） ----
FIRST_TRANCHE = 0.50            # P-36 首批＝目標的 50%；自動模式不用 70% 加速條款
MIN_TARGET = 0.03               # P-33 開倉目標 ≥ 權益 3%
GRADE_CAP = {'A': 0.15, 'B': 0.10}   # P-28 宿主單檔建倉成本上限
ADD_STOP_WEIGHT = 0.15          # P-43 市值權重 >15% 不加碼


def auto_buy_gates(d, gate3_col='gate3_pass'):
    """d：deep() 的輸出。gate3_col：每列問卷驗證結果（gate3_check.evaluate()['gate3_pass']）；缺 → 視為未通過。
    閘門 2＝價格 < 買價上限：上限＝使 g_impl ＝ g_cap 的價格，價格與 g_impl 同向，所以等價於 g_impl < g_cap（且等級 A／B、無紅旗、E>0）。"""
    d = d.copy()
    d['gate1_not_deleted'] = ~d.screen_decision.eq('DELETE')
    d['gate2_price_below_cap'] = (d.grade.isin(['A', 'B']) & (d.red_flags.fillna('') == '') & (d.E_norm > 0)
                                  & d.mcap.notna() & (d.g_impl < d.g_cap)).fillna(False)
    g3 = d[gate3_col] if gate3_col in d else pd.Series(False, index=d.index)
    d['gate3_questionnaire'] = g3.fillna(False).astype(bool)
    d['auto_buy'] = d.gate1_not_deleted & d.gate2_price_below_cap & d.gate3_questionnaire
    return d


def initial_position(target_weight, grade):
    """回傳 (目標權重, 首批權重) 或 None。目標先受 P-28 等級上限約束；<3% 不建倉（P-33，不試水溫）。
    target_weight 由 G11 公式（意願、P-29 尾部、P-30 單事件、P-31／P-32 相關群、宿主上限）先算好再傳入。"""
    if grade not in GRADE_CAP or not (target_weight == target_weight):
        return None
    tgt = min(float(target_weight), GRADE_CAP[grade])
    if tgt < MIN_TARGET:
        return None
    return tgt, FIRST_TRANCHE * tgt


def add_allowed(current_weight, target_weight, grade, thesis_intact, gates_repassed, price, cap):
    """每次複查的加碼許可（P-37 剩餘批次與 P-42 之後的加碼都走這裡）。回傳 (可否, 理由)。
    thesis_intact：本次論點檢查（H1／H2 Q2）結論為「完好」；疑似或證偽 → 不可（P-50、P-55）。
    gates_repassed：三閘門以本次資料重新全過（P-42 加碼門檻＝新建倉門檻）。"""
    if not thesis_intact:
        return False, '論點檢查未通過（疑似或證偽）→ 停止加碼'
    if not gates_repassed:
        return False, '三閘門未全部重新通過（P-42）'
    if not (price == price and cap == cap) or price >= cap:
        return False, '價格 ≥ 買價上限（NEVER-11）'
    if current_weight > ADD_STOP_WEIGHT:
        return False, '權重 >15%（P-43）'
    lim = min(target_weight, GRADE_CAP.get(grade, 0.0))
    if current_weight >= lim:
        return False, '已達目標或等級上限（P-28）'
    return True, f'可加至 {lim:.1%}'
