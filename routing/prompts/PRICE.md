# 工作：PRICE 快速通道（{tickers}，決策日 {asof}）

這是機械工作。每檔輸入：cap_calc 新輸出、上次訊號卡、登錄表該列、論點檔、組合狀態。

每檔：
1. 照抄 cap_calc 的 `auto_buy_cap`、`IV_base`（＝sell_review_above）、荒謬級線；不得自行改數字。
2. 閘門：
   - gate1：登錄表或上次訊號卡的初篩結果不是刪除 → 通過。
   - gate2：現價 **<** auto_buy_cap → 通過；auto_buy_cap 為 null → 無上限。
   - gate3：上次閘門 3 問卷通過且距今 ≤ 12 個月 → 通過；超過 → 已過期。
3. proposed_action：
   - WATCH 且三閘門全過 → BUY_CANDIDATE（部位與組合比較由框架做）
   - HOLDING、權重 < 目標權重、三閘門全過 → BUY_CANDIDATE（加碼；加碼許可由框架核對）
   - HOLDING 且現價 ≥ 荒謬級線 → SELL_HALF
   - HOLDING 且現價 ≥ sell_review_above → SELL_REVIEW（只有出現明顯更好用途才減碼，由框架比較）
   - 現價 ≥ sell_review_above 只是因為長債上升使 IV_base 下修（價格本身未超過上次的 sell_review_above）→ 只停止加碼，改 HOLD（規則書 P-25）
   - 建倉未滿一年（first_buy_date）→ 不提 SELL_REVIEW／SELL_HALF，改 HOLD（SKILL §6：第一年不因價格因素賣出）
   - 其餘 → NO_CHANGE 或 HOLD
4. needs_full＝true：cap_calc 出現新的 notes 警示、等級可能需要變動、gate3 已過期、或任何需要判斷才能回答的情況。
   - 登錄表或上次訊號卡的上限、IV 與 cap_calc 新輸出不同是正常的（本工作就是用新價格與新長債重算），一律以 cap_calc 為準，**不算**需要判斷，不因此設 needs_full。

輸出：每檔一個 PRICE 格式 JSON，放在一個 JSON 陣列中。
