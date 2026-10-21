### 14.2 `inferred_thresholds_used` 表（全部推論門檻；使用任一項時，把「鍵」寫入訊號卡同名欄位；cap_calc 套用的鍵框架會補列）
| 鍵 | 規則 | 預設 | 可調 | 出處（審查代號） |
|---|---|---|---|---|
| rd_spread | P-14 | 長債＋3pp | 2–4pp | X30-14、X28 |
| rd_floor | P-14 | 10% | 9–12% | 舊版|
| grade_caps | P-16 | A 0.85／B 0.75／C 0.40（報告欄 P1、金融類硬上限 min(P1, P4′)、IV0 上限用；不是自動買進上限的折扣） | 0.80–0.90／0.70–0.85／0.33–0.50 | X25、X29、X30-01 |
| gimpl_cap | P-17 | 10%（P2′ 硬條件；舊版 8% 作廢） | 6–10% | 舊版、合併版覆寫|
| gimpl_A_half | P-17 | A 級 ≤ 歷史一半 | — | X29 |
| gimpl_base_rate | P-17 | 歷史躍升比例 <10% | — | GAP-X05-1 |
| stress_add | P-18 | r_stress＝max(10%, 長債 +6pp)；低利率起點 max(10%, 長債 +7pp)（即 r_d 利差 3pp 之上再 +3pp／+4pp；10% 地板使長債 ≤4% 時 r_stress＝r_d；P3 只作報告；cap_calc 實際式，D91） | +2～+4／+3～+5 | X30-06、X28、設計決策 D91 |
| stress_sens | P-18／VAL-045 | r_sens＝max(10%, 長債 +7pp)；低利率起點 +8pp（敏感度欄 sensitivity_cap_P3_plus4pp，只作報告；D90 起不參與上限、不做翻轉測試） | — | X28、X30-10、設計決策 D90、D91 |
| p4_soft_A | P-19 | A 級且增量資本報酬 > r_d → 1.2 倍（缺或不成立 → 1.0） | 1.1–1.3 | X02 |
| p4_fin | P-19／VAL-097 | 金融中位 ×1.1 | 1.0–1.2 | X25 |
| wpe_lines | P-20 | 20／25 倍（警示） | — | X25、X06 |
| range_width | P-21 | >3 降級／>4 不做（只適用非金融；cap_calc 自動套用，輸入等級不得先含此項；降級計入 driver 成長離散度） | 2.5–4／3.5–5 | X29、設計決策 D22 |
| near_cap | P-23／VAL-045 | 距上限 <5%：D90 起明文寫進自動買進上限＝0.95 × min(P2′, k × IV_base)（CONCLUSION_MARGIN；原 E×0.9 翻轉測試退化、已刪） | 3–7% | X30-10、設計決策 D90 |
| capex_coef | P-24 | max（×1.2，中位 ×0.8）；上限 1.6（2.0）；高通膨上限 1.4 | 1.2–2.0 | X21-10、X21-05、X21-07、X04 |
| g0_haircut | VAL-040 | ×0.75（單期 ×0.6） | 0.5–0.9 | X06、X30-09 |
| terminal_cons | VAL-040 | 0%（強護城河 ≤3%） | 0–2% | X19d |
| roe_pct_bonus | P-03／P-04 | 第 50／35 百分位 | 40–60／25–45 | X25、X26 |
| bonus_rate | P-08 | ≥60%／40–60%／<40%（宿主以可計 7 項中有資料者為分母；優另需有資料 ≥4 項，原 <8/13 按比例換算） | 優 50–70%；降級 30–50% | X26；ASK-3＝B |
| min_years | P-10 | 5 個完整年度 | 5–6 | X29、X30-13 |
| rev_volume | P-05 | 實物量年降 2% | — | X08、X09 |
| int_cov | P-06 | 5 年中位 ≥5；連降且 <8 降級 | 窗口 3–7 年 | X21-02、X21-03、X21-07 |
| int_cov_reg | P-06 | 受管制下限 2.5（2.0–2.5 降級） | 2–3.5 | X03、X04、X10、X21-09 |
| de_exclude | P-07 | >3 倍排除 | 2–4 | X29 |
| strict_screen | P-12 | 0.9 倍 | 0.8–1.0 | X26 |
| rank_weights | P-13 | 見 P-13 | 第 50–70 百分位 | X09、X26、X28 |
| host_B_cap | P-28 | 10% | 7.5–12% | 舊版|
| tail_formula | P-29 | 10pp ÷ 最壞相對損失；下限 25% | — | X30-18 |
| exec_loss | P-30 | 權益 2% | 1.5–3% | X27、X30-17 |
| gap_est | P-30 | 近 8 次 8-K 2.02 財報：每次取申報日與次一交易日跌幅較差者，再取最大跌幅；取不到 → null、記「P-30 未檢查」（框架計算） | — | X30-17、M2_D4_01 |
| s_eff | P-30（B 案，未採用） | 掛單 13%／收盤檢查 18% | 12–15%／15–20% | X27 |
| corr_group | P-31 | 2 × 單檔上限；同一外部變數 → 同群 | 1–2 倍 | X30-18 |
| common_loss | P-32 | 5%／10% | 3–7%／8–12% | 舊版|
| munger_switch | P-34 | 超額 ≥10pp、A 級、可承受 −50% | 8–12pp | X19w、X19b |
| a_grade_count | P-01 | A 級 >3 檔重驗（框架目前不檢查、無自動觸發：人工） | 2–4 | X19b |
| cash_buffer | P-35 | 下限 ×1.2 | 1.1–1.3 | GAP-X09-01 |
| first_tranche | P-36 | 自動模式一律 50%（「回落 ≥20% 時 70%」加速條款不用） | 50–70%；回落 −15～−30% | X30-32、X27 |
| build_window | P-37 | 15 日／連 10 日高於上限 | 10–20／5–15 | X30-24 |
| liquidity | P-38 | 日成交額 1%；一般模式可 2–5% | 0.5–2% | X12 |
| illiquid_total | P-38 | 權益 40% | 25–40% | GAP-X11-02 |
| deriv_limits | P-40 | 追繳 ≤1%；到期支付 ≤5% | — | X03、X14 |
| struct_break | P-41 | 破局下檔 50% | — | GAP-X21-09-1 |
| overweight | P-43 | 宿主 15%／25%；一般 40%／50% | 20–30%；35–45% | X30-33、X10、X11 |
| v_rebase | P-45 | 重估後 ≥ IV_base ×0.9 | 0.85–0.95 | X02 |
| v_alarms | P-45 | 建倉 1.5 倍；大盤 1.2 倍；P/B 6 且 ROE <25% | 1.3–1.7；1.1–1.4；20–30% | X25、X13 |
| f_roe_drop | P-46 | 2pp；連 2 年 | 2–5pp；1–3 年 | X01、X12 |
| top_trim | P-47 | ×1.3、警報 ≥3、≤1/4、保留 2/3 | 1.2–1.5 | X05、X02 |
| absurd | P-48 | 正常化 P/E ≥50（價格線＝50 × 正常化 EPS，缺 → E）；賣一半（建倉第一年內只停止加碼＋完整複查） | 40–60 | X05、X28 |
| new_pos_gate | P-49 | 2 年 | 1.5–3 年 | 舊版|
| watch_limit | P-50 | 兩次季報或 6 個月 | 1–3 季 | 舊版|
| switch_gate | P-53 | 0.3；5pp（確定性換股減半） | 0.2–0.4；3–7pp | 舊版、X02 |
| hit_rate_fb | P-54 | 20 筆；<50% 收緊；>65% 維持 | — | 舊版|
| stop_A_trigger | P-55（A） | −12%；±4%；單季 −25%（只適用持股；狀態型觸發為邊緣觸發：上次分析時已成立者不再觸發） | −10～−15%；±4–5% | 宿主計畫 S-8 |
| stop_B_budget | P-55（B 案，未採用） | 12 個月 ≤ 權益 2%；63 日二次觸發等季報 | 1.5–3% | X27、X30-07 |
| turnover_review | P-56 | 非制度周轉 >100% | 50–150% | X03 |
| event_loss_tax | P-57 | 稅後 3%／6% | 2.5–3.5%／5–7% | X02、X16 |
| float_switch | P-58 | 短期利率 4% | 3–5% | X03、X28 |
| bank_stress | P-60 | 2.75%／2.25% | 2.5–3.25% | X02 |
| reeval_gap | P-62 | 12 個月 | 6–24 | 舊版|
| fast_track | P-63 | 12 個月 | 6–18 | 舊版|
| bias_stack | P-64 | 紅燈 3–4；否決 ≥5；快速例外 ≤ 上限 ×0.8 | 3–4；4–6 | X07、X12 |
| mktcap_gdp | P-66 | 原文 80／160／200%；衍生 60／120／150%；分位 75／90 | 70–90%；±10pp | X28、X15 |
| opp_flow | P-68 | 4 季提示；8 季流程檢查 | 6–12 季 | X06、X12、X19w |
| eval_end_buffer | POS-064 | 最後 5 個交易日不開新部位（紙上期無清算日，不適用、框架不實作：procedure/13 §4、設計決策 D94） | 0–5 日 | X30-29 |
| certainty_coef | G8／POS-021 | A 0.9／B 0.75／C 0.5；國庫券 1.0／大盤 0.8 | ±0.1 | X30-15 |
| fast_change_def | G1／SEL-051 | 任一：①新進者 3 年內取得 ≥10% 市占；②核心產品單位價格近 3 個財年中 ≥2 年下降 >15% 且不是商品價格循環；③風險因素具名替代技術且已侵蝕核心營收（泛用樣板句不算）。單位量變動、一般技術替代字樣不算 | — | X30-27、設計決策 D19 |
| overactivity | G9 | 12 個月非制度新建倉 >4 | 3–6 | GAP-X14-04 |
| hq_cost | SEL-097 | 總部費用 >5% 營業盈餘或同業中位 2 倍 | — | GAP-X02-03 |
| great_plus | SEL-055 | ≥7 年增量報酬 ≥20%、再投入 ≥50% | 7–10 年；40–60% | X03 |
| great_line | SEL-055 | 25% | 20–30% | X29 |
| bank_gate | SEL-101 | ROA ≥0.9%；權益 ÷ 資產 ≥ 中位且 ≥5% | 0.8–1.1% | X25、X02 |
| jurisdiction | RISK-090 | 等級上限再減 0.10；機率 5–20%、損失 50–100% | 0.05–0.15 | X07、X06 |
| vie_tail | SEL-023a | 單檔最壞損失 >權益 9% 不買 | — | X19c |
| reserve_agg | RISK-038 | 連 3 年 ≥0.5%；5 年累計 >5% | 3–8% | X15、X16a、X21-05 |
| reserve_seg | RISK-038 | 相對成熟路徑 >3pp；曆年 2 年 ≥2%；分部 <25% 只記 W1 | 2–5pp；1.5–3% | X21-08、X21-10、X21-05 |
| reserve_release | RISK-038 | 增提額 ≥50% 於 3–5 年內釋放 → 排除級 | 30–70% | X03 |
| float_spike | RISK-035 | 扣除後 >5% 連 3 年 | 4–6%；觀察 1–3 年 | X15 |
| release_share | RISK-039 | 前期釋放 >50% 承保利益 | 40–70% | X16 |
| reserve_sens | RISK-040 | 5 年平均稅前營業盈餘 100% | 50–150% | X15 |
| retro_gap | RISK-042 | ≤10%／10–20%／>20%；≥5 份合約 | 8–15%／15–25% | X16c |
| longtail_net | RISK-024 | 5%／10% | 4–6%／8–12% | X16a |
| pension | RISK-024／RISK-061 W5 | 差距 >2pp；費用 5%、缺口 5%、>20% 降級 | 3–10%／15–30% | X17d |
| eq_sens | RISK-044 | 稅後 20%（資本強度 30%）；>30% 一律降 | 25–35%；1/5–1/2 | X17a、X18a、X18w、X21-04 |
| mkt_scenario | RISK-044（GAP-X17c-2） | −50% 情境 >35% 不買 | 30–40% | X17c |
| unreal_gain | RISK-044b | 未實現增值 >50% 權益；前三大 >75% | 40–60%／60–90% | X18w |
| index_tail | RISK-029 | 最大暴險 >20%；同年到期 >50% | 15–25%／40–60% | X16a、X17c |
| premium_growth | RISK-033 | 保費年增 >15% 且綜合率惡化 >3pp | 10–20% | X15、X21-05 |
| claim_share | RISK-076 | 求償總額 ×10%；>5% 降級、>10% 不買 | 5–20% | X21-05、X16a |
| problem_size | RISK-082② | ≤ 權益 50% 且 ≤2 年正常化盈餘 | 1–3 年 | X14 |
| construction_loans | RISK-050 | 同業中位 2 倍或權益 100% | 75–150% | X07 |
| htm_loss | RISK-069 | −15% 降級；−33% 不買 | −10～−25%／−25～−40% | X01 |
| distress_reserve | RISK-092 | 最差累計不利修正率 ×1.0–1.5 | — | X03 |
| soft_market | RISK-093 | 淨準備 ×5% | 3–10% | X15 |
| cam_goodwill | RISK-077b | 預扣 30–50% | 20–50% | X17d |
| inv_income_window | RISK-034 | 10 年平均短期利率 | 7–15 年 | X21-05、X21-10 |
| yield_chase | RISK-034 | 另類＋低評等 3 年升 ≥5pp | — | X04 |
| equity_method | RISK-061 R7 | 帳面高於市值 >20% 連 ≥4 季 | 3–6 季／15–30% | X21-06 |
| growth_addback | VAL-010 | 新增單位價值 ÷ 取得成本 >1.2 | 1.1–1.5 | X08b |
| cyclical_haircut | VAL-012 | 衰退年營業利益 −60～−100% | — | X18b |
| restricted_cash | VAL-113 | 打折 10–30% | — | GAP-CASH-01 |
| control_cross | VAL-112 | ≤0.6 加分；>1.0 重查 | — | GAP-X02-06 |
| credit_spread | MKT-021 | 90 分位 | 85–95 | X28 |
| method_review | MKT-037(b) | 一個完整週期（≥5 年）＋錯誤率 >50% | 5–7 年 | X13 |
| retained_dollar | MKT-061 | 1.2／0.9 | — | X13、X16 |
| idle_cash | POS-053 | 連 4 季、閒置 >25% | 2–8 季 | X30-34 |
| contract_lock | POS-095 | 不超過 P-29 上限一半 | 1/3–2/3 | GAP-X23-01 |
| p25_triggers | P-25 | 利率變動 ≥1pp；單季跌 ≥25% | — | 舊版、X30-19 |
| p26_buyback | P-26 | 等級上限 −0.10 | — | X05、X29 |
| p42_near_stop | P-42（B 案，未採用；A 案已刪） | 距有效停損 ≤3% 不加碼 | — | 舊版|
| accounting_flags | P-61 | 警訊 1 項 → 重編並降一級；≥2 或排除級淘汰 | — | X29 |
| pnl_window_W | RISK-061 W13 | 前置認列 >20%／>40% | 10–30% | X06 |
| capital_alloc | MKT-061～069 | 見各條 | — | 舊版|
| corr_group_worst_loss | P-31 | 群最壞相對損失＝各成員 P-29 最壞損失（1 − 悲觀價值 ÷ 現價，下限 25%）按權重加權平均；成員缺 tail 時用 P-32 減損（金融 50%、非金融 30%）；報表標出 P-31 或 P-32 先綁定（規則書未定義「群」的最壞相對損失；框架計算） | — | 2026-10-18 Hark 修正、設計決策 D11 |
| corr_group_grade_cap | P-31 | 2 × 單檔等級上限：取群內 A／B 級成員與本標的等級上限的最小者 | — | 2026-10-18 Hark 修正 |
| corr_group_by_variable | P-31 | 以受控詞彙 variable 分群，方向不分（油價崩與油價漲同群） | 改為分方向 | 2026-10-18 Hark 修正 |
| corr_buy_keep_le_5pct | P-32 | 單筆買進不得使群損失超過 5%（依「任何動作不得違反」） | — | 2026-10-18 Hark 修正 |
| corr_unknown_fin | P-32 | 缺 asset_class → 視為金融（50%） | — | 2026-10-18 Hark 修正 |
| restate_min_diff | procedure/08 §6.3 | 首次版與最新重編版差 ≥0.5% 才列入差異清單 | 0.1–1% | 2026-10-18 Hark 修正（框架資料包） |
| history_10k | procedure/08 §3、P-58 | 保險 SIC 6300–6399、銀行 6020–6036；歷年 10-K 每隔一份取一份、至多 4 份；各 Item 字數上限 | — | 2026-10-18 Hark 修正（框架資料包） |
| cap_discount_k | SKILL §3／P-16 | 自動買進上限＝0.95 × min(P2′, k × IV_base)（0.95 見 near_cap，D90）：A 0.95／B 0.85（無回測；設定檔 cap_discount_k，框架注入） | 1.0＝不折扣 | 設計決策 ASK-1（使用者 2026-10-18 選 B） |
| abc_consistency | SKILL §3 | 自動買進上限不高於 k × IV_base（賣出檢視線＝IV_base，兩線間留緩衝） | — | 合併版一致性條款 |
| g9_growth_spread | SKILL §3／ASK-1 | 已證明成長取 10 年與 5 年較高者；兩期差 >5pp → G9 必寫理由（g9_growth_reason） | — | 設計決策 ASK-1 |
| near_zero_g | SKILL §3／VAL-050 | 歷史成長 ≤1% 視為近零（或 W-REV）→ 硬上限不高於 IV0 × 等級係數；歷史成長 ≤0 → 保守 g0 不打折、IV_base 永續 0%；此處「歷史成長」＝10 年與 5 年中較低者（只有一期用該期），已證明成長（較高者）只用於基準起始成長 | — | 設計決策 D21 |
| g_term_impl | P-17／SKILL §3 | P2′ 永續成長＝min(3%, 30 年期 −1pp)（下限 0%） | — | 合併版 |
| cons_path_decay | VAL-040／VAL-046 | 保守情境 1–10 年成長採 g_impl 同式遞減至 min(g, 4%) | — | 合併版（cap_calc） |
| fin_hard_cap | SKILL §3／VAL-080～081 | 金融類無 P2′：硬上限＝min(P1, P4′)；金融 IV_base 尚未定義（自動流程的覆寫由框架清空）→ 不設自動買進上限；P-21 不適用 | — | 合併版、設計決策 D20、D22 |
| heavy_asset_def | SKILL §3／P-24 | 重資產＝5 年中位 資本支出 ÷ 營收 ≥8% 且 資本支出 ÷ 折舊 ≥1.2 → K＝max(1.2, 中位 ×0.8)，上限 1.6 | — | 合併版 |
| shock_year_def | SKILL §3 | 同業（SIC 前兩碼）≥5 家；當年中位 ROE ≤0 或 < 前 5 年中位的一半 → 產業共同衝擊年 | — | 合併版 |
| roe_sd_stability | SKILL §2 步 10 | 剔除衝擊年後 ROE 標準差 >4pp → 最高 C | — | 合併版 |
| intent_weight | G11／SKILL §5 | 意願權重 HIGH／MEDIUM／LOW → 100%／75%／50% × 等級上限，再與其他上限取最小 | — | 設計決策 D13 |
| oe_check_tol | SKILL §3（D26） | 模型手算 E、K、g 與機械版相對差 >10%（成長率另需絕對差 >1pp）或剔除年不同 → 退回 FULL_A 一次，仍不同 → 最多 WATCH | — | 設計決策 D26 |
| norm_eps_floor | cap_input 檢查 | norm_eps 有給須 >0 且 norm_eps ÷ oe ≥0.2 | — | 2026-10-18 Hark 修正（M2-D8-01） |
| too_hard_cap | 初篩排序／SEL-050 | 前 15 名中預設太難型產業最多 4 檔，超出往下遞補 | — | 設計決策 D66 |
| quarter_drawdown_window | P-25／P-55 (c) | 單季跌＝近 92 曆日（不早於上次分析日）最高收盤 → 最新收盤 | — | 2026-10-18 Hark 修正（M2-D5-03；procedure/07 §7） |
| material_8k_items | P-55 (e)／P-62 | 重大 8-K＝1.01、1.03、2.01、2.06、3.01、4.01、4.02、5.02 → FULL；9.01、5.07 不初判；其他進 triage | — | 2026-10-18 Hark 修正（M2-D5-03） |
| amendment_level | P-55 (e) | 10-K/A、10-Q/A → THESIS；20-F／40-F 視同年報 → FULL | — | 2026-10-18 Hark 修正（M2-D5-03） |
| triage_cap | SKILL §7 | triage 依日期取最近 20 筆 | — | 2026-10-18 Hark 修正（M2-D5-03） |
| p08_denominator | P-08／SEL-078 | 可計集合 7 項：前半（FULL_A）B3、B4、B8、B12、B13，得暫定通過率；涉價格的 B7、B11 前半記「缺」，由 FULL_B 看價後補評，算最終通過率。需母體百分位或中位的 SEL-B1、B2、B5、B6、B9、B10 無母體檔記「缺」不計分母。有資料 <4 項 → 最高「品質可」；前半 5 項有資料 <3 項且因財報缺 → MISSING_DATA（因母體或價格項記缺者不適用）。暫定 <40% → 前半降一級；最終 <40% 且前半未因 P-08 降級 → FULL_B 降一級；前半已降者維持、最終品質最高「可」（只能更嚴） | 門檻 4、3 為原 8/13、5/13 乘 7 取整 | 2026-10-18 Hark 修正（M2-D5-13、D61）；2026-10-19 ASK-3＝B |
| paper_fill_typical | procedure/13 | 紙上成交價＝隔日典型價（高＋低＋收）÷3 ± 滑價；限價買／賣以限價夾住 | — | 設計決策 D07 |
| review_due_triggers | SKILL §7 | 申報窗口＝申報日 ≥ 上次分析日且 < 決策日；FULL：新年報、滿 12 個月、登錄表 next_full_review_due 到期、重大 8-K、P-50 期滿、權重 >25%；THESIS：新季報（含 10-K/A、10-Q/A）、延遲申報通知（NT 10-K／NT 10-Q／NT 20-F，W14）、失效條件到期、持股 −12%／±4%／單季 −25%；PRICE：跌破或站上價格線、長債變動 ≥1pp | — | 合併版、設計決策 D23 |
| missing_data_retry_weeks | SKILL §7／procedure/07 §7 | 13 週（91 個曆日）：持股完整分析以 MISSING_DATA 停止後，期間內「滿 12 個月」、next_full_review_due、權重 >25% 不重排 FULL；新年報照常 FULL、新季報升 FULL；PRICE／THESIS 照常（推論） | — | 設計決策 D74 |
| HOLD_STOP_RETRY_DAYS | SKILL §7／A3-M3B-01 | 91 個曆日：持股 FULL_A 退回後仍違規停止（沒有估值）→ 不推進 last_full_review_date，next_full_review_due＝決策日＋91 天，約一季後重排年度完整分析（框架常數，非訊號卡鍵） | — | 推論；trader/trader/analysis/writeback.py HOLD_STOP_RETRY_DAYS（jobs/deep.py _stop 傳 hold_unresolved） |
| stale_quote_days | SKILL §7／procedure/07 §7 | 最後收盤早於決策日 >5 個交易日（只扣週末）→ 價格類觸發不檢查；上次分析日前最後收盤早於該日 >5 個曆日 → 資料缺口，不當上次分析基準；申報抓取失敗 → 只略過申報類觸發（不升級、標「重大申報未檢查」），其餘觸發照查；同檔連續 3 次失敗 → 異常 trigger_fail_streak（不暫停交易） | — | 2026-10-18 Hark 修正（M2_D9_05、M2_D9_01、M2-R5-04、M2-RS8-03） |
| g8_framework_values | G8／P-27／procedure/06 §2 | 國庫券列用 3 個月期（FRED DGS3MO，決策日前最後一筆）×1.0；大盤列依 P-66 分區，P-27 區內取值：低 11%、中 8%→5%、高 5%→3%（依比值線性；分位把區別往上推時取該區上緣）、極高 3%，扣摩擦 1pp（下限 0）後 ×0.8；框架計算 | 摩擦 0.5–2pp | 框架 g8_benchmarks（未回測） |
| a_grade_g8_lead | procedure/06 §1 | A 級另須 G8 排序分高於第 2 名至少 2pp；不到 → 不得評 A（模型判斷；框架目前不檢查） | — | procedure/06 §1（推論） |
| w4_guidance_nonfin | procedure/03 §3 W4 | 非金融：只是提供公開盈餘指引、沒有「湊數字」行為證據 → 不計 W4，改記 G5 誘因警訊（同一事實不在 G4、G5 重複計）；金融機構提供盈餘指引即升 A3 | — | procedure/03（推論） |
| director_fee | procedure/02 §4.2／SEL-094 | 董事費占董事本人可見收入比重高（原文量級 20–50%，未定單一切點）或董事自掏腰包持股少 → 視同 SEL-094 第 6 項命中（不重複計）；前半不用價格判斷持股市值，記「缺」 | — | procedure/02（推論） |
| cooling_off | procedure/07 §5／SKILL §5 | 價格或新聞觸發、且不屬論點證偽的決定延後一個交易日；框架對 PRICE 觸發的 BUY／ADD／TRIM 次日才撮合，撮合前以前一交易日收盤重核（買：收盤×(1＋滑價) < 現行上限；估值 TRIM：收盤仍 ≥ 觸發線），不符取消；論點證偽全清與已寫入日誌的建倉批次不適用。避開開盤與收盤前後劇烈時段（紙上交易以隔日典型價記成交，見 paper_fill_typical） | — | procedure/07（推論）、設計決策 D05 |
