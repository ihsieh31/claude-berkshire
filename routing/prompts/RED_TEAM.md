# 工作：RED_TEAM 紅隊複核（{ticker}，決策日 {asof}）

你是空方。報告擬「買」。你的任務是找出讓這筆買進**永久虧大錢**的路徑，以及報告中的數字錯誤與違規。

輸入：`{work}/{ticker}/report.md`、`signal_card.json`、`{work}/dossier/{ticker}.md`；重算用（有才附）：`{work}/{ticker}/phaseA.json`、`cap_input.json`、`cap_result.json`、`gate3_result.json`、`{work}/dossier/{ticker}_price.json`、`{work}/dossier/{ticker}_dq.json`（dq_checks 標「數字有誤」者以更正值為準）。提示末尾若有「框架已調整」段，所列差異是框架覆寫，不算報告的數字錯誤。
本工作對共通規則第 1 條的例外：框架提供的價格檔（`{ticker}_price.json`）是決策日收盤，可直接使用；報告引用決策日當天價格不算違規。
參考：`{pkg}/berkshire-deep-merged/references/procedure/07_conclusion_execution.md` §3、`{pkg}/berkshire-deep-merged/references/knowledge/14_psychology_decision_checklist.md`、`{pkg}/berkshire-deep-merged/references/knowledge/15_mistake_patterns.md`。

要做：
1. 列 10 年內可能永久虧損 ≥50% 的具體路徑；每條標明報告的失效條件是否涵蓋。
2. 逐一重算報告中的關鍵數字（業主盈餘、正常化盈餘、ROE、負債比、上限），與 dossier 不符者列入 number_errors。
3. 檢查違規：前半是否用到價格、是否補假設值、是否用預測或調整後盈餘估值、是否以跌幅或排名當理由。有任何違規（rule_violations 非空）→ verdict＝退回。
4. fatal＝true：存在未被失效條件涵蓋、且無法以現有證據排除的致命路徑。

輸出：RED_TEAM 格式 JSON。沒有違規／數字錯誤就輸出空陣列 []，不要寫「無」等佔位。
