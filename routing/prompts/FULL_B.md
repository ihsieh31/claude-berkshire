# 工作：FULL_B 深度分析後半（{ticker}，決策日 {asof}）

前半結論與問卷已存檔（不得修改；若你認為前半有錯，寫在報告「反方」段並把結論降為 WATCH，由框架重跑前半）。
現在才提供價格：`{work}/dossier/{ticker}_price.json`。

讀：`{pkg}/berkshire-deep-merged/SKILL.md` §3–§6，與步 9–15 指定的 references 段落。
輸入：phaseA.json、report_A.md、gate3_check 結果、dossier、價格檔、組合狀態：
{portfolio_state}
大型股 P/E 中位：{largecap_pe_median}

要做：
1. 步 9 G7：寫 `{work}/{ticker}/cap_input.json`（格式見 `scripts/cap_calc.py` 說明），數字取自 phaseA.normalized_inputs 與價格檔；不得另造。框架會執行 cap_calc，並以腳本結果為準。
2. 步 10–14：確定性等級、G8 機會成本、G9 反向思考與偏誤（逐條作答）、G10 三件凍結、G11 部位（§5）。
3. 步 15：依 §4 一致性檢查，輸出報告 `report.md`（含前半）、訊號卡、登錄表一列、決策日誌一列、論點檔 `thesis.md`。論點檔的保守價值區間段要列出 phaseA.thesis_draft.key_assumptions（3–5 個關鍵假設，可依後半結果修訂措辭，不得刪減成少於 3 條）。
4. BUY 只在 SKILL §0 條件全成立且自動買進三閘門全過時；否則最多 WATCH，並附上限。

輸出：其餘檔案寫到 `{work}/{ticker}/`；最後的回覆只輸出 FULL_B 格式的 JSON 物件（摘要）本身，前後不加任何說明文字。
