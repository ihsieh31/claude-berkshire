# 工作：FULL_A 深度分析前半（{ticker}，決策日 {asof}）

**你看不到任何價格，也不得推測價格。** 本階段的判斷必須在不知道價格的情況下成立。

讀：`{pkg}/berkshire-deep-merged/SKILL.md`（全文），再依 §2 表「細則／思考背景」欄，每一步只讀該步指定的 references 段落。
輸入：`{work}/dossier/{ticker}.md`、資料包核對結果 `{work}/dossier/{ticker}_dq.json`（有才讀）、screen_context：
{screen_context}
`{ticker}_dq.json` 的 dq_checks 標「數字有誤」者以更正值為準，並在 report_A.md 誠實標示寫明；missing 各項依 procedure/08 降級。

本次觸發：{trigger}

持股分支（is_holding＝{is_holding}）：
- is_holding＝是（本檔為持股，這是年度完整複查）：G0–G6、4A、產業檢查任一不通過**不停止**——stopped＝false、stopped_at_gate 與 reason_code 填 null，第一個不通過處記在 `holding_stop`＝{"gate": 閘門, "reason_code": 停止碼, "reason": 一句}（物件，不得寫成字串；gate 填 G0–G6、4A 或 產業，reason_code 填 MISSING_DATA | TOO_HARD | REJECT | NOT_THIS_SKILL），其餘步驟（問卷、normalized_inputs、論點稿、report_A.md）照常做完，讓後半做到 G7 估值供持有側 H2 使用；買進側結論框架一律不加碼。只有 STEP0 資料缺到無法分析時照常停止（stopped＝true）。全部通過 → `holding_stop`＝null。
- is_holding＝否：照下面第 1 條「非通過即停」，`holding_stop` 填 null。

要做：
1. 依 SKILL §2 步 0–8 依序走（STEP0、G0、G1、G2、G3、步 4A〔screen_decision＝PASS-困境或有重大壞消息時必做；PASS-困境 只由非價格困境碼產生，只因價格類訊號需要的困境檢查由後半看價後做，前半不推測〕、G4、G5、G6、產業檢查）。非通過即停（持股分支例外，見上）：停下時 stopped=true，填停止碼，並在 `rule_ids` 列出停止所依據的規則編號（字串陣列，照 rulebook 編號，例如 ["SEL-061"]；不得為空），後面不做。
   護城河三測另填 `moat_evidence`（每測一項）：評「強」，或有反證支持「中／弱」（例如漲價後量掉、份額流失、對手砍價搶到客戶）→ `足夠`；評不到「強」只因為資料包沒有判定所需的數字（留存／續約率、壓力事件後使用量、同業比較等），現有資料既不支持也不否定「強」→ `缺資料`。有疑問時填 `足夠`。未走到 G3 護城河 → 三項都填 null。這一欄只做紀錄，不改變評分或停止碼。
   G1 預設太難類的「快速持續變動」只依規則書 G1 操作定義（設計決策 D19，推論），符合任一才算：①主要競爭者近 3 年有新進入者取得 ≥10% 市占；②核心產品單位價格近 3 個財年中 ≥2 年下降 >15%，且不是商品價格循環；③年報風險因素具名某替代技術，且該技術已在侵蝕核心營收（泛用樣板句不算）。單位量變動、一般技術替代字樣不算；商品循環依 SEL-050 清單與 SEL-051 第 4 點。問卷 C5 用同一定義。
   宿主不提供母體統計（同年同類母體的中位、百分位）：P-03／P-04 加分項與 P-13 中需要母體中位的項目一律記 MISSING_DATA（不阻擋、不當作不通過；P-08 以有資料的項目計，不計入分母）。
   P-08（procedure/01 §6；設計決策 ASK-3＝B）：前半只評 B3、B4、B8、B12、B13（B7、B11 涉價格記缺，由後半補評；需母體的 6 項記缺），在 report_A.md 寫「P-08 暫定：有資料 n/5、通過 m、通過率 x%」。有資料 <3 項且不足是因財報缺 → 停止、reason_code MISSING_DATA；暫定通過率 <40% → `downgrades` 列一列（driver_id「品質加分」）；前半不判品質優。
   資料包的利率段（30 年期公債、3 個月國庫券殖利率；不是個股價格）可用於 G3 門檻與 P-58；資料包沒有這段時照規則記缺。
   `downgrades` 每列代表實際降一級；「最高 B／C」類上限型規則（例如盈餘不穩定最高 C、優勢在人最高 B），若當時等級已不高於上限就不列，列入時另填 `max_grade`（該規則的上限等級）：這種列不逐級降，框架以其餘列逐 driver 降級後與 max_grade 取較嚴（一般列省略 max_grade）。每列必填非空的 `driver_id`（觸發這次降級的獨立驅動因素，例如「營運資本」「資本配置」），`reason` 寫觸發的事實。框架依 driver_id 計級：同一 driver 只降一級，不得因同一件事觸發多條規則而列多列（同一 driver_id 出現兩次即不合格；設計決策 D18）；代理口徑只在報告標註，不列降級。
   **未評估的欄位一律填 null，不得填 0 或補一個評等**：未走到 G3 → `moat_tests` 三項 null；未走到 G4 → `red_flags` 兩項 null；未走到 G5 → `capital_allocation_net` null。0 只代表「已評且為零」。
2. 若走完步 8：作答閘門 3 問卷（`{pkg}/berkshire-deep-merged/assets/gate3_questionnaire.json`），寫 `{work}/{ticker}/gate3_answers.json`；`price_seen_before_answering`＝false，`price_file_opened_at` 留空。每題 `evidence` 只能引用資料包已收錄的文件（`{work}/dossier/{ticker}.md` 與其附的 SEC 原文；不得引用資料包以外或憑記憶的文件）；`evidence.date` 一律填該文件的 SEC 申報日 `filingDate`（非 SEC 文件填發布日），不得填財報期末日——例如 FY2025 年報填其 10-K 申報日，不是 2025-12-31（框架以此日期做前視檢查，A3-M2D-01）。
3. 為後半準備正常化輸入（normalized_inputs）：業主盈餘每股依 SKILL §3 單一公式；剔除產業共同衝擊年依 §3 演算法。資料包同業段若有「判定說明（交分析在誠實標示寫明）」：照判定剔除，並在 report_A.md 的誠實標示逐年寫明本公司當年是否同受衝擊（本公司 ROE、營收、營業利益對照同業中位）與剔除對業主盈餘的方向；不得因同業組成不理想而自行改判。框架會用資料包 10 年財報表另算機械版 E、K、g10／g5 與這裡比對（設計決策 D26）：相差 >10%（成長率另需差 >1pp）或剔除年與同業衝擊年不同 → 退回 FULL_A 重做一次；仍不同 → 照記並最多 WATCH。差異有理由（例如已扣除的一次性項目）就在 report_A.md 寫明。g10／g5 若依 VAL-013（基期失真）或 procedure/05（收購期剔除收購貢獻）改算、不是首尾年複合，在 normalized_inputs 填 `g_adjustment_reason`（一句理由與改算方法）：框架對 g 的差異只記註、不退回（設計決策 D93），E、K 與剔除年照核。只有做了步 4A 且困境三問全過才在 normalized_inputs 填 `distress_pass`＝true，否則省略或填 false（A3-M2C-06）。`excluded_shock_years` 與 `one_time_adjustments` 的 fy 一律填整數財年（例如 2020，不寫 "FY2020"）。
   同時填 normalized_inputs 的估值欄（與 cap_calc 輸入同名，後半不得另造，框架會以這裡的值覆寫 cap_input）：`norm_eps`（正常化營業 EPS）、`roe`（正常化 ROE）、`eps_cagr5`、`incr_roic_gt_rd`（三值：確定高於 r_d 填 true；確定 ≤ r_d 填 false〔成長不加價值，框架會把硬上限壓到不高於 IV0×等級係數〕；資料不足、未判定填 null，不得以 false 代替；D97）；`asset_class`＝保險時另填 `insurance` 的每股有形帳面 `tbv_ps`、每股平均浮存金 `float_ps`、P-58 浮存金加權成本 `float_cost`（10 年窗，不足時至少 5 年），非保險三欄填 null。金融與否由 `asset_class` 決定（框架據以設定 cap_input.financial；REIT 屬非金融），不另填。護城河是否「變寬」由框架依問卷 M1–M3 全強且 M5＝變寬判定，不必填。
4. 擬論點一句話、3–5 個關鍵假設（保守價值區間的計算前提，每條要能被日後的申報數字否定；持有後每季依 SELL-011 R1 對照）與 2–4 條失效條件（可觀察、有期限；每條 `due` 須晚於決策日 {asof}，過去或當天的期限永遠不會觸發檢查，validate 會退回）。
5. 報告前半寫入 `{work}/{ticker}/report_A.md`（模板 `assets/analysis_report_template.md` 對應段）。

輸出：你自己寫的檔只有上面第 2、5 條的 `{work}/{ticker}/gate3_answers.json` 與 `{work}/{ticker}/report_A.md`。路由表的輸出清單另列 `{work}/{ticker}/phaseA.json`：那是框架把你最後的回覆存成的檔，不要自己寫。最後的回覆只輸出 FULL_A 格式的 JSON 物件本身，前後不加任何說明文字。
