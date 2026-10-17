# 工作：FULL_B 深度分析後半（{ticker}，決策日 {asof}）

前半結論與問卷已存檔（不得修改；若你認為前半有錯，寫在報告「反方」段並把結論降為 WATCH，由框架重跑前半）。
現在才提供價格：`{work}/dossier/{ticker}_price.json`。

讀：`{pkg}/berkshire-deep-merged/SKILL.md` §3–§6，與步 9–15 指定的 references 段落。
輸入：phaseA.json、report_A.md、gate3_check 結果、dossier、價格檔、持股比較檔 `{work}/portfolio/holdings_g8.json`、組合狀態：
{portfolio_state}
大型股 P/E 中位：{largecap_pe_median}

要做：
1. 步 9 G7：寫 `{work}/{ticker}/cap_input.json`（格式見 `scripts/cap_calc.py` 說明），數字取自 phaseA.normalized_inputs 與價格檔；不得另造。框架會執行 cap_calc，並以腳本結果為準。
2. 步 10–14：確定性等級、G8 機會成本、G9 反向思考與偏誤（逐條作答）、G10 三件凍結、G11 部位（§5）。
   G8 比較表各列照抄 `{work}/portfolio/holdings_g8.json`（框架已用決策日收盤價與 cap_calc 重算，不得另算或改用買進成本）：加碼現有最佳持股＝`best_addable`、觀察名單次佳＝`best_watch`（只含今天收盤低於自動買進上限的 A／B 級）、短期國庫券＝`benchmarks.treasury.adjusted`、大盤＝`benchmarks.market.adjusted`（P-66 分區與 P-27 取值寫在該列）；持股 ≥10 檔時本標的另須勝過 `weakest`。本標的的調整後預期報酬＝(cap_input 的 oe ÷ 價格＋cap_calc 保守 g0)×等級係數，框架以腳本重算寫入訊號卡 `g8_adjusted_expected_return`。比較表填進摘要的 `g8`；停在 G7 未走到 G8 時數字填 null、winner 填 `未比較`。某列有 `error` 或缺值時在報告寫明，該列不得當作輸給或勝過的依據。
   框架覆核：任一列高出本標的 ≥1pp（或持股 ≥10 檔時最弱持股高出 ≥1pp），或本標的算不出調整後預期報酬 → BUY 一律改 WATCH（停在 G8）；差 <1pp 由你依品質判斷。
3. 步 15：依 §4 一致性檢查，輸出報告 `report.md`（含前半）、訊號卡、登錄表一列、決策日誌一列、論點檔 `thesis.md`。論點檔的保守價值區間段要列出 phaseA.thesis_draft.key_assumptions（3–5 個關鍵假設，可依後半結果修訂措辭，不得刪減成少於 3 條）。
4. BUY 只在 SKILL §0 條件全成立且自動買進三閘門全過時；否則最多 WATCH，並附上限。

輸出：其餘檔案寫到 `{work}/{ticker}/`；最後的回覆只輸出 FULL_B 格式的 JSON 物件（摘要）本身，前後不加任何說明文字。
