# 工作：FULL_A 深度分析前半（{ticker}，決策日 {asof}）

**你看不到任何價格，也不得推測價格。** 本階段的判斷必須在不知道價格的情況下成立。

讀：`{pkg}/berkshire-deep-merged/SKILL.md`（全文），再依 §2 表「細則／思考背景」欄，每一步只讀該步指定的 references 段落。
輸入：`{work}/dossier/{ticker}.md`、screen_context：
{screen_context}

要做：
1. 依 SKILL §2 步 0–8 依序走（STEP0、G0、G1、G2、G3、步 4A〔screen_decision＝PASS-困境或有重大壞消息時必做〕、G4、G5、G6、產業檢查）。非通過即停：停下時 stopped=true，填停止碼，後面不做。
   護城河三測另填 `moat_evidence`（每測一項）：評「強」，或有反證支持「中／弱」（例如漲價後量掉、份額流失、對手砍價搶到客戶）→ `足夠`；評不到「強」只因為資料包沒有判定所需的數字（留存／續約率、壓力事件後使用量、同業比較等），現有資料既不支持也不否定「強」→ `缺資料`。有疑問時填 `足夠`。未走到 G3 護城河 → 三項都填 null。這一欄只做紀錄，不改變評分或停止碼。
2. 若走完步 8：作答閘門 3 問卷（`{pkg}/berkshire-deep-merged/assets/gate3_questionnaire.json`），寫 `{work}/{ticker}/gate3_answers.json`；`price_seen_before_answering`＝false，`price_file_opened_at` 留空。
3. 為後半準備正常化輸入（normalized_inputs）：業主盈餘每股依 SKILL §3 單一公式；剔除產業共同衝擊年依 §3 演算法。資料包同業段若有「判定說明（交分析在誠實標示寫明）」：照判定剔除，並在 report_A.md 的誠實標示逐年寫明本公司當年是否同受衝擊（本公司 ROE、營收、營業利益對照同業中位）與剔除對業主盈餘的方向；不得因同業組成不理想而自行改判。
4. 擬論點一句話、3–5 個關鍵假設（保守價值區間的計算前提，每條要能被日後的申報數字否定；持有後每季依 SELL-011 R1 對照）與 2–4 條失效條件（可觀察、有期限）。
5. 報告前半寫入 `{work}/{ticker}/report_A.md`（模板 `assets/analysis_report_template.md` 對應段）。

輸出：報告等檔案照上面寫到 `{work}/{ticker}/`；最後的回覆只輸出 FULL_A 格式的 JSON 物件本身（框架會存成 `phaseA.json`，不要自己寫這個檔），前後不加任何說明文字。
