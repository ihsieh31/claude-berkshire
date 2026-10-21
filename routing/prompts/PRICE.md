# 工作：PRICE 快速通道（{tickers}，決策日 {asof}）

這是機械工作。每檔輸入：cap_calc 新輸出、上次訊號卡、登錄表該列、論點檔、組合狀態。
本工作對共通規則第 1 條的例外：框架提供的價格（cap_calc 的 price）是決策日收盤，可直接使用。

每檔：
1. 照抄 cap_calc 的 `auto_buy_cap`、`IV_base`（＝sell_review_above）、荒謬級線；不得自行改數字。
2. 閘門：
   - gate1：登錄表或上次訊號卡的初篩結果不是刪除 → 通過。
   - gate2：cap_result.grade_used ∈ {A, B}、且現價 **<** auto_buy_cap → 通過；auto_buy_cap 為 null → 無上限（不得買）。auto_buy_cap 已是 0.95 × 原始上限（VAL-045 結論測試明文化，設計決策 D90），沒有另外的翻轉測試。
   - gate3：由框架依決策日誌（最近一次完整分析的問卷結果與 12 個月期限）計算，本工作寫 null。三閘門最後都由框架重算並覆寫。
3. 動作不由本工作決定（設計決策 D28）：買進候選、加碼、持有、交 HOLD_REVIEW 賣出檢視，一律由框架依 cap_calc、三閘門與賣出規則（SKILL §6、規則書 P-25、P-48；設計決策 D01／D03）算出。
   - 本工作不得決定賣出：荒謬級、≥ sell_review_above 都只由框架交 HOLD_REVIEW；減碼（TRIM）只有 HOLD_REVIEW 能決定。
   - `proposed_action`（BUY_CANDIDATE | HOLD | SELL_REVIEW | SELL_HALF | NO_CHANGE）選填，只記日誌、框架不採信。
4. G9、G10 不得跳過（P-63）：
   - `g9_contrarian`：一到兩句，以本次 cap_calc 數字寫反方最強論點的簡答。
   - `g10_still_valid`：G10 三件（一句話理由、保守價值區間、失效條件）以本次數字看是否仍成立；任一不成立 → false（框架交完整分析）。
5. needs_full＝true：cap_result.grade_used 與 registry_entry.grade 不同（框架亦自行檢查，模型漏設也會交完整分析）。
   - 閘門 3 已過期、長債缺由框架自行交完整分析，不必設。
   - 登錄表或上次訊號卡的上限、IV 與 cap_calc 新輸出不同是正常的（本工作就是用新價格與新長債重算），一律以 cap_calc 為準，**不算**需要判斷，不因此設 needs_full。

輸出：PRICE 格式的單一 JSON 物件。
