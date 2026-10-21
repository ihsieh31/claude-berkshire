#!/usr/bin/env python3
"""結構性刪除（多年虧損／結構性損害）的可量化實作：判定每檔 DELETE（附理由碼）或 PASS（含 PASS-困境、PASS-資料不足）；
另輸出困境標記 T1–T5 與會計旗標 F5。排序由 screen.py 負責。規則見 ../SKILL.md 第 3 節。

時點：所有財報欄位必須是決策日前「已申報」者（申報日 < 決策日）。若輸入帶 fy_last_filed（最近財年申報日）與決策日，
screen() 會檢查並拒絕任何申報日 ≥ 決策日的列（前視）。

輸入欄位（決策時點可得；財報＝決策日前最近已申報財年，價格＝決策日前）：
  Y, sector, fin, util, net_income, ni_prev, shareholders_equity, eq_prev, loss_yrs5, n_ni5, ni_sum5,
  operating_cash_flow, ocf_sum5, ocf_pos5, n_ocf5, eps_g1, roe, roe_med5, dd52；可選 fy_last_filed
用法：
  import screen_quant as S; out = S.screen(S.from_live(features_df), decision_date='2026-10-12')
"""
import numpy as np, pandas as pd

def _clean(df):
    d = df.copy()
    # 資料品質：營業現金流為 0 視為缺值（開發集發現 0 代表未抽到）
    d['ocf_sum5_c'] = d['ocf_sum5'].where(d['ocf_sum5'] != 0)
    d['operating_cash_flow_c'] = d['operating_cash_flow'].where(d['operating_cash_flow'] != 0)
    d['nonfin'] = ~d['fin'].astype(bool)
    return d



LIVE_MAP = {'is_financial': 'fin', 'debt_to_equity': 'de', 'share_change_3y': 'sh_chg3', 'rev_cagr_5y': 'rev_cagr5'}


def from_live(df, asof_year=None):
    """把 fetch_edgar.py＋fetch_prices.py 的輸出轉成本模組的標準欄位。"""
    d = df.rename(columns={k: v for k, v in LIVE_MAP.items() if k in df.columns}).copy()
    if 'Y' not in d: d['Y'] = asof_year or 0
    if 'sector' not in d: d['sector'] = d.get('sector_name', pd.Series(['未分類'] * len(d)))
    for c in ['loss_yrs5', 'ni_sum5', 'n_ni5', 'net_income', 'ni_prev', 'shareholders_equity', 'eq_prev', 'ocf_sum5', 'ocf_pos5',
              'n_ocf5', 'eps_g1', 'roe', 'roe_med5', 'dd52', 'operating_cash_flow', 'fin', 'util']:
        if c not in d: d[c] = np.nan if c not in ('fin', 'util') else False
    d['fin'] = d['fin'].fillna(False).astype(bool); d['util'] = d['util'].fillna(False).astype(bool)
    return d

DELETE_RULES = ['J1_持續虧損', 'J3_權益為負且虧損', 'J4_虧損腰斬權益', 'J7_連虧且累虧']
FLAG_RULES = ['F5_盈餘無現金']


class LookAheadError(ValueError):
    """財報申報日不早於決策日（前視）。"""


def check_point_in_time(d, decision_date):
    """申報日必須早於決策日（decision date > filed）。缺 fy_last_filed 欄位時無法檢查，回傳 False 並由呼叫端在輸出註記。"""
    if decision_date is None or 'fy_last_filed' not in d:
        return False
    fd = pd.to_datetime(d['fy_last_filed'], errors='coerce'); D = pd.Timestamp(decision_date)
    bad = fd.notna() & (fd >= D)
    if bad.any():
        raise LookAheadError(f'{int(bad.sum())} 列財報申報日 ≥ 決策日 {D.date()}（前視）；請以 fetch_edgar.py --asof 決策日 重抓')
    return True


def screen(df, decision_date=None):
    d = _clean(df)
    d['pit_checked'] = check_point_in_time(d, decision_date)
    nf = d.nonfin
    codes = {
        # J1 經濟本質已毀／持續無法獲利：5 年中 ≥3 年虧損且 5 年淨利合計 < 0
        'J1_持續虧損': (d.loss_yrs5 >= 3) & (d.ni_sum5 < 0),
        # J3 資本結構無法存活：權益 ≤0 且最近一年虧損
        'J3_權益為負且虧損': (d.shareholders_equity <= 0) & (d.net_income < 0),
        # J4 一年虧損吃掉一半以上權益（獲利能力無法吸收損失＝非暫時性）
        # 例外：此前經濟力佳（5 年虧損 ≤1 年且 5 年 ROE 中位 ≥12%）→ 不刪，改走困境通道；
        # 例外所需的歷史缺值 → 無法判定，也不刪（缺資料永遠不是刪除理由；虧損本身已標 T1）
        'J4_虧損腰斬權益': (d.net_income < 0) & (d.eq_prev > 0) & (d.shareholders_equity < 0.5 * d.eq_prev)
                         & ~(((d.loss_yrs5 <= 1) & (d.roe_med5 >= 0.12)) | d.loss_yrs5.isna() | d.roe_med5.isna()),
        # J7 連兩年虧損且 5 年淨利合計 < 0
        'J7_連虧且累虧': (d.net_income < 0) & (d.ni_prev < 0) & (d.ni_sum5 < 0),
    }
    M = pd.DataFrame({k: v.fillna(False).astype(bool) for k, v in codes.items()})
    # 旗標（不刪；交深度審查 G4 盈餘品質逐項作答）
    flags = {
        # F5 會計品質：5 年報告淨利為正但 5 年營業現金流合計為負（非金融；版的 J5，改為旗標）
        'F5_盈餘無現金': nf & (d.ni_sum5 > 0) & (d.ocf_sum5_c < 0) & (d.n_ocf5 >= 3),
    }
    FM = pd.DataFrame({k: v.fillna(False).astype(bool) for k, v in flags.items()})
    d['flag_codes'] = FM.apply(lambda r: ';'.join([k for k, v in r.items() if v]), axis=1)
    d['delete_codes'] = M.apply(lambda r: ';'.join([k for k, v in r.items() if v]), axis=1)
    dele = M.any(axis=1)
    # 暫時性困境標記（不刪，送深度審查專門檢查）
    sec_dd = d.groupby(['Y', 'sector'])['dd52'].transform('median')
    sec_n = d.groupby(['Y', 'sector'])['dd52'].transform('count')
    sec_dd = sec_dd.where((sec_n >= 5) & (d['sector'] != '未分類'))   # 同產業 ≥5 檔才判產業恐慌
    dist = {
        'T1_去年虧損': d.net_income < 0,
        'T2_EPS跌逾25%': d.eps_g1 < -0.25,
        'T3_股價距52週高跌逾30%': d.dd52 <= -0.30,
        'T4_ROE低於5年中位一半': (d.roe < 0.5 * d.roe_med5) & (d.roe_med5 > 0),
        'T5_產業恐慌': sec_dd <= -0.20,
    }
    T = pd.DataFrame({k: v.fillna(False).astype(bool) for k, v in dist.items()})
    d['distress_codes'] = T.apply(lambda r: ';'.join([k for k, v in r.items() if v]), axis=1)
    has_core = d.net_income.notna() & d.shareholders_equity.notna()
    d['screen_decision'] = np.where(dele, 'DELETE', np.where(~has_core, 'PASS-資料不足', np.where(T.any(axis=1), 'PASS-困境', 'PASS')))
    return d
