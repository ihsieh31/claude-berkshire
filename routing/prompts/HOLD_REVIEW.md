# 工作：HOLD_REVIEW 持股完整複查的賣出決策（{ticker}，決策日 {asof}）

本檔是**持股**。剛完成的完整分析是用「買進」閘門做的；買進閘門不通過本身不是賣出理由（rulebook H0：轉換或複查不得產生賣出訊號，除非依 Q2 確認論點證偽）。你的工作是依 H2 賣出決策樹，決定這筆持股要持有、減碼或全清。

輸入：
- 持股：{position}
- 價格：{price}
- 完整分析：{full_status}
- 觸發本次複查的論點檢查：
{thesis_result}
- 檔案：{files}

H2 賣出決策樹（rulebook §9 原文照錄）：
{sell_tree}

細則：`{pkg}/berkshire-deep-merged/references/rule_index/04_hold_sell.md`（SELL-020～083）。

要做：
1. 依 Q0 → Q6 的順序逐題作答，寫進 path（每題一列：是／否／不適用＋證據）。走到第一個產生動作的題目就停，後面的題目答「不適用」。
2. 理由只能來自企業成績、論點與失效條件、會計與誠信事實、估值數字（IV_base、正常化盈餘倍數）。股價漲跌、持有期、成本、新聞、總經不是理由（Q0）。
3. Q2「論點被證偽」要具名：哪一條關鍵假設或失效條件，被哪一個事實否定（寫在 falsified_assumption）。只是接近、方向不利、證據未完整 → 「只是疑似」→ HOLD、stop_adding＝true、observe_until 填 P-50 期限（兩次季報或 6 個月，取先到者）。
4. 會計警訊級（RISK-061 W 類）在持有中屬「完整複查 → Q2」：確認是會計問題（SELL-027(A)）才全清；只是警訊未確認 → 疑似。
5. Q3 只能用「完整分析」給的 IV_base 與 cap_calc 數字；完整分析未走到估值（停在 G7 之前）→ Q3 一律「不適用（無 IV）」。
6. 業務類不得只賣一部分（TRIM 不可用於業務類）。TRIM 只用於 Q3c 的頂級複利型減碼（每次 ≤1/4）或荒謬級（至少一半），trim_fraction 寫比例。
7. 建倉未滿 2 年（Q5）：寫不出「哪條假設被哪個事實否定」→ 持有。
8. 有疑義時：不賣，HOLD＋stop_adding＝true。

輸出：HOLD_REVIEW 格式 JSON。
