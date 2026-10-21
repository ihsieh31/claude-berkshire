# 工作：THESIS 論點檢查（{ticker}，決策日 {asof}）

輸入：論點檔 `{thesis_file}`、失效條件、上次分析（{last_review_date}，含當日）後的新申報節錄、TRIAGE 結果（如有）、新聞標題。
新申報節錄期間：{last_review_date}（含）起、決策日之前（10-Q、10-Q/A、10-K/A、8-K、8-K/A；只有 9.01／5.07 項目碼的 8-K 不附）。
申報抓取狀態：{sec_status}
有 `thesis_sec/_status.txt` 表示本次未抓取申報：沒有申報檔不代表沒有新申報，結論至少「疑似」。有 `thesis_sec/_omitted.txt` 表示其中列出的申報因篇幅未附：未讀的申報不得據以判完好。兩者皆無時，沒有申報檔＝該期間查無新申報。

本次觸發原因（價格細節已略）：{trigger}

失效條件（登錄表，凍結論點檔的失效條件；K 編號供 condition_id 引用）：
{kill_criteria}

新聞標題（只有日期、標題與來源，沒有內文；只當線索，證據以申報為準）：
{news}

R 清單（SELL-011 完整複查清單，原文照錄）：
{r_list}

參考：`{pkg}/berkshire-deep-merged/references/procedure/07_conclusion_execution.md` §6、`procedure/09_postmortem_signals.md`。

要做：
1. 逐條失效條件：未觸發／接近／已觸發／無資料，附證據（文件、節、申報日、數字）。`criterion` 照抄失效條件原文，不含 K 編號。
   每列另填 `next_due`：下一次能拿到判斷這條所需證據的檢查日（YYYY-MM-DD，須晚於決策日；例如下一份季報預計申報日之後），
   框架據此更新登錄表的到期日；這條已不需定期檢查（結案）或已觸發 → 填 null。
2. R 清單 R1–R10 逐項（每項一列，共 10 列）：無變化／改善／惡化／無資料／不在本工作範圍，附證據（status 只填列舉值本身，不加括號說明；說明寫 evidence）。
   - R1 對照論點檔的「關鍵假設」逐條；論點檔沒有關鍵假設 → status 填「無資料」，evidence 寫明論點檔未載。
   - R7（價格相對內在價值）、R8（換股）需要價格：本工作看不到價格 → status 填「不在本工作範圍」，evidence 寫「由 PRICE 與完整分析處理」。
   - R4、R6、R10 需要 10 年財報表、r_d 或部位權重：本工作未提供這些資料時 status 填「不在本工作範圍」、evidence 寫「由年度完整分析處理」，不得因此判疑似。
   - 「惡化」只在符合該項「惡化訊號」欄時使用。惡化要先依 SELL-012 分週期性或結構性；只有一兩季的獲利下滑不算結構性，寫在證據裡。
3. 列出與論點有關的新事實。
4. 結論：
   - 證偽：任一失效條件已觸發；或經濟特性、護城河明確惡化；或最高決策者本人誠信問題有證據。
   - 疑似：接近觸發、方向不利但證據未完整、或資料不足以排除；或任一 R 項惡化（rulebook H1：任一 R 清單項目惡化 → 記錄並進 H2，由完整分析處理）。
   - 完好：以上皆非。
   **有疑義一律「疑似」**。價格下跌本身不是證偽或疑似的理由。
5. 判「證偽」時必填（設計決策 D02；不判證偽時三欄填 null）：
   - `falsify_type`：CONDITION_TRIGGERED（失效條件已觸發）｜TOP_MGMT_INTEGRITY（最高決策者本人誠信問題）｜ACCOUNTING（會計問題）｜ECONOMICS_DETERIORATION（經濟特性或護城河惡化）。
   - `condition_id`：對應的失效條件編號（上方清單的 K1、K2…）；CONDITION_TRIGGERED 必填，且該條在 kill_criteria_status 須標「已觸發」。
     TOP_MGMT_INTEGRITY／ACCOUNTING 沒有對應的失效條件時填 null（不要引用不相干的 K 編號），框架改以 evidence 核對；有填則須是清單內的編號。
   - `evidence`：[{"source": 文件與節（申報類型、申報日、URL 或段落）, "date": YYYY-MM-DD（早於決策日）}]，至少一筆。
   框架只在前三類、condition_id 核對通過（CONDITION_TRIGGERED 須在清單內；誠信／會計類可為 null，有填須在清單內）、證據有來源與早於決策日的日期時才直接全清（自下一交易日起執行，不再做完整分析）；
   ECONOMICS_DETERIORATION 或核對不過 → 框架改為「疑似」：停止加碼、同輪交持股複查（HOLD_REVIEW）並升完整分析。

輸出：THESIS 格式 JSON。
