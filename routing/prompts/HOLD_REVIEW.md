# 工作：HOLD_REVIEW 持股完整複查的賣出決策（{ticker}，決策日 {asof}）

本檔是**持股**。剛完成的完整分析是用「買進」閘門做的；買進閘門不通過本身不是賣出理由（rulebook H0：轉換或複查不得產生賣出訊號，除非依 Q2 確認論點證偽）。你的工作是依 H2 賣出決策樹，決定這筆持股要持有、減碼或全清。
規則書與 SKILL 不一致處，**本系統採 SKILL §6**（設計決策 D01），下面逐條標出。H0 引述：本系統採 SKILL §6——賣出只走：論點證偽（Q2）、現價 ≥ IV_base 且有 G8／P-53 證明的更好用途（Q3／Q6）、荒謬級（建倉滿一年）、制度類硬上限（Q1；減碼由框架下單時自動產生，不在本工作決定）與收購要約（Q1b）；權重因上漲超過等級上限、買進閘門不過都不是賣出理由。
若「完整分析」寫「論點檢查判證偽但框架核對未過，已降為疑似…」（或寫「缺」且觸發的論點檢查帶 `falsify_downgrade`）：這是論點檢查判證偽、但框架核對未通過（或屬經濟特性／護城河惡化這類需判斷的理由）後直接交來的複查（D02），本次**沒有完整分析結論、無 IV_base**：Q3 的 answer 一律填「不適用」，evidence 寫「無 IV，完整分析缺」；依 Q2 決定全清、降級續抱（HOLD＋stop_adding）或觀察（P-50）；完整分析另行排程。

輸入：
- 持股：{position}
- 價格：{price}（本工作對共通規則第 1 條的例外：框架提供的價格與 G8 比較檔是決策日收盤，可直接使用）
- 完整分析：{full_status}
- 估值輸入：{valuation_inputs}
- 上次估值減碼（登錄表 last_trim）：{last_trim}
- 觸發本次複查的論點檢查：
{thesis_result}
- 觸發理由（框架提供；數值已遮）：
{trigger}
- 檔案：{files}

H2 賣出決策樹（rulebook §9 原文照錄）：
{sell_tree}

細則：`{pkg}/berkshire-deep-merged/references/rule_index/04_hold_sell.md`（SELL-020～083）。

要做：
0. Q1 制度類只指宿主／使用者設定的單檔權重硬上限、法律或監管強制門檻：只減到解除限制（本系統採 SKILL §6；D01）。這筆減碼由框架下單時依硬上限自動產生（數量＝超出部分），**不在本工作決定**：Q1 成立時該列 answer 填「是」，evidence 寫「框架自動減到解除限制」與依據，本工作不因 Q1 填 TRIM／SELL_ALL，也不填 sell_category＝制度（填了會轉人工、停止加碼並記異常），接著照常作答 Q1b 之後各題；其餘題目都沒有動作時 action＝HOLD。
   權重因上漲超過等級上限（A 15%／B 10%）**不是**制度類、也不是賣出理由：本系統採 SKILL §6——不賣，只停止加碼；> 25% 另排完整複查。
1. 依 Q0 → Q6 的順序逐題作答，寫進 path（每題一列：answer 只填 是／否／不適用 三者之一，不加括號或任何說明；說明與證據一律寫 evidence）。走到第一個產生動作的題目就停，後面的題目答「不適用」。
2. 理由只能來自企業成績、論點與失效條件、會計與誠信事實、估值數字（IV_base、正常化盈餘倍數）。股價漲跌、持有期、成本、新聞、總經不是理由（Q0）。
3. Q2「論點被證偽」要具名：哪一條關鍵假設或失效條件，被哪一個事實否定（寫在 falsified_assumption）。只是接近、方向不利、證據未完整 → 「只是疑似」→ HOLD、stop_adding＝true、observe_until 填 P-50 期限（兩次季報或 6 個月，取先到者）。
   P-50（rulebook 原文：「到期必須結案」）：觸發理由有「P-50 觀察期到期」時本次必須結案——證偽 → SELL_ALL；否則解除觀察，observe_until 填 null（stop_adding 只依其他理由）。框架不接受續期：觀察期已到期時不採用新的 observe_until，觀察中不得延後原期限，新期限最長決策日起 6 個月（超過夾回並記日誌）。
   決策樹走到「持有、確定性降一級、停止加碼」時，另填 `grade_after`（降級後的等級 A／B／C／D）；沒有降級就不填或填 null。框架只採用比登錄表現有等級更保守的值（只降不升）。
4. 會計警訊級（RISK-061 W 類）在持有中屬「完整複查 → Q2」：確認是會計問題（SELL-027(A)）才全清；只是警訊未確認 → 疑似。
5. Q3 只能用「完整分析」給的 IV_base 與 cap_calc 數字；完整分析未走到估值（停在 G7 之前）→ Q3 的 answer 一律填「不適用」，evidence 寫「無 IV」。
   Q3 門檻：本系統採 SKILL §6(2)——現價 ≥ IV_base 時，只有 G8／P-53 證明有更好用途（換股或資金用途）才減碼或換股；**本系統目前不自動執行 SKILL §6(2) 的 G8 數量估值減碼**（沒有 G8 數量來源）：有更好用途 → action＝HOLD、stop_adding＝true，在 reason 寫明 G8 比較（哪一列、高出幾 pp）；需要換股 → action 填 TRIM 或 SELL_ALL、sell_category 填 SWITCH（框架轉人工：HOLD＋停止加碼＋異常，不由本工作下單）。荒謬級與 Q3c 以外的 TRIM＋估值，框架一律改 HOLD＋停止加碼；
   **不採** rulebook 3b「普通／良好企業價格 ≥ IV_base → 全清」（3b 在本系統 → 沒有更好用途就 HOLD＋stop_adding）。3a（V 且結構性 F）屬業務類，維持全清（記「估值＋業務」）。
   P-53：本系統採 SKILL §6——「通過 P-53 的換入標的或資金用途」只能以下列 G8 比較檔的數字證明，寫不出數字就是沒有更好用途。P-53 只適用可交易證券（換入標的須為可在市場買賣的證券）。
   Q3 要判斷「有明顯更好的用途」時，只能用 `portfolio/holdings_g8.json` 的數字比較：`self` 是本持股以決策日收盤重算的調整後預期報酬，對照 `best_addable`（加碼其他持股）、`best_watch`（觀察名單今天可買者）、`benchmarks.treasury`、`benchmarks.market`。門檻照 P-53（rulebook：價格／價值比差 ≥0.3 且 5 年稅後年化報酬差 ≥5pp；任一算不出 → 不換）：換入列的 `price_to_conservative`（取 `holdings`／`watchlist` 中同代號那列）須比 `self` 低 ≥0.3，**且** `adjusted_expected_return` 須比 `self` 高 ≥5pp（以 adjusted_expected_return 代替「5 年稅後年化報酬」是本系統的推論）；確定性換股（價格／價值比相近、換入標的確定性高一級，須寫明理解度差異）報酬差門檻減半（≥2.5pp）：確定性換股（價格／價值比相近、確定性高一級）不要求價格／價值比差 ≥0.3，報酬差門檻 ≥2.5pp。任一項算不出（比較檔缺本持股、某列缺 price_to_conservative 或 adjusted_expected_return）→ 不換，該列不得當作換股理由。`benchmarks.treasury`、`benchmarks.market` 兩列沒有價格／價值比，不能單獨當作更好用途。未達門檻＝沒有更好用途。成本與買進價不得當理由。
6. 業務類不得只賣一部分（TRIM 不可用於業務類）。TRIM 範圍（本系統採 SKILL §6；D01）：只用於 Q3c 的頂級複利型減碼（每次 ≤1/4）、荒謬級（至少一半），
   trim_fraction 寫比例。SKILL §6(2) 的 G8 數量估值減碼本系統目前不執行（見第 5 點：有更好用途 → HOLD＋stop_adding，換股填 SWITCH 交人工）。
   本系統採設計決策 D03（框架回寫時檢查，超出範圍會夾回並記日誌）：只有本工作能決定 TRIM（PRICE 只把荒謬級、≥ IV_base 交來，不下單）；
   荒謬級在價格回到線下之前只減一次（看上方「上次估值減碼」）；Q3c 累計保留 ≥ 本持有期峰值股數 2/3；
   其他估值減碼（SKILL §6(2)）目前一律 HOLD＋stop_adding（框架無 G8 數量來源）。建倉未滿一年（建倉日缺視同未滿）不得以估值類賣出（本系統採 SKILL §6「第一年不因價格因素賣出」；D01）：
   第一年內碰到荒謬級 → **不機械減半**，HOLD＋stop_adding＝true（停止加碼並完整複查）；滿一年後才依 P-48 至少賣一半。論點證偽（業務類）不受一年限制。
   頂級複利型（3a、3c）的「品質優」（設計決策 ASK-3＝B、D77）：先看本次完整分析 report.md 的「P-08 最終」行，寫「品質 優」才算；報告沒有這行（未走完 FULL_B，含 PRICE 觸發的複查）→ 改看 `last_full_signal_card.json` 的 `p08_final.quality`，為「優」才算；兩者都沒有（或欄為 null）→ 不算品質優、不是頂級複利型。
7. 建倉未滿 2 年（Q5）：寫不出「哪條假設被哪個事實否定」→ 持有。
8. 有疑義時：不賣，HOLD＋stop_adding＝true。
9. P-31 外部變數：重新檢視本持股的失效條件，凡描述外部變數者以 `{pkg}/berkshire-deep-merged/assets/external_drivers.json` 的 id 標進 `external_drivers`（沒有 → 空陣列）。本檔既有判定：{prior_external_drivers}。與既有判定不同時，必須在 `external_drivers_change_reason` 寫出新事實（來源、日期），否則框架維持既有判定。P-32 群組共同情境 >10% 的減碼由框架產生建議，不在本工作決定（rulebook：「本句不擋 P-32 群組共同情境超限」）。

輸出：HOLD_REVIEW 格式 JSON。
