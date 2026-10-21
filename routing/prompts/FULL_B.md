# 工作：FULL_B 深度分析後半（{ticker}，決策日 {asof}）

前半結論與問卷已存檔（不得修改）。確定性等級與失效條件以前半為準，後半只能更嚴：等級不得高於前半結束時的等級（phaseA.starting_grade 依 phaseA.downgrades 逐項降一級，同一 driver_id 只降一次），只能再降；後半再降級也依獨立驅動因素計：phaseA.downgrades 已因同一件事（同一 driver_id）降過的，不再降（設計決策 D18）；論點檔與登錄表的失效條件必須逐字包含 phaseA.thesis_draft.kill_criteria 全部條件，只能新增、不得刪改。若你認為前半有錯，寫在報告「反方」段並把結論降為 WATCH；前半不會重跑，框架一律以較嚴的結論寫回。摘要等級高於前半結束等級（或高於下面 P-26a 應再降的等級）時，框架直接壓回並記註，不退回重做。
現在才提供價格：`{work}/dossier/{ticker}_price.json`。
本工作對共通規則第 1 條的例外：框架提供的價格檔與 G8 比較檔（`portfolio/holdings_g8.json`）是決策日收盤，可直接使用。
價格檔另有：`price_meta`（價格來源與距決策日天數）、`rates_meta`（利率序列與錯誤）、`cover_shares`（決策日前最新封面流通股數與申報日；缺時以 10 年表稀釋股數代用並在報告寫明）、`not_provided`（框架未提供：月價格、同業估值）。`not_provided` 所列項目依 procedure/08 §2 在 G4／G7 降級並寫缺資料，這是框架沒給，不是公司本身資料不足。
拆股（M4-R2-02）：`cover_shares.shares` 已乘封面申報日後至價格日的拆股（有換算時另列 `shares_as_filed`、`split_factor`）；`splits_meta.events_after_last_10k` 非空時，10 年表稀釋股數／EPS 是該 10-K 的拆股前基準，股數乘、每股數字除以合計比例後才與價格比；`splits_meta.checked` 為 false＝拆股未核，市值與每股價值須在報告註明此風險。

讀：`{pkg}/berkshire-deep-merged/SKILL.md` §3–§6，與步 9–15 指定的 references 段落。
輸入：phaseA.json、report_A.md、gate3_check 結果、dossier、資料包核對結果 `{work}/dossier/{ticker}_dq.json`（有才讀）、價格檔、持股比較檔 `{work}/portfolio/holdings_g8.json`、組合狀態：
{portfolio_state}
大型股 P/E 中位（月凍結檔）：{largecap_pe_median}
P4′ 只採用其中兩個數字（最近年報稀釋 EPS 口徑，設計決策 D60）：大型股＝`pe_ttm_median`、金融＝`fin_pe_ttm_median`（凍結檔沒有此欄才用 `fin_pe_norm_median`）。cap_input 與報告只引用這兩個數字；其餘欄（`pe_norm_median` 等）不是 P4′ 採用值，不得引用為大型股 P/E 中位。
`{ticker}_dq.json` 的 dq_checks 標「數字有誤」者以更正值為準，並在報告誠實標示寫明；missing 各項依 procedure/08 降級。
phaseA.holding_stop 非 null（持股年度複查，前半有閘門未過；D30）：照常做完 G7 估值與其後各步，但結論最多 WATCH（框架亦會降級），報告寫明未過的閘門。
價格類困境碼（D71）：價格檔 `screen_price_context.price_distress_codes` 非空（初篩的 T3／T5 價格類困境碼，或 `S_特殊情境交棒`），而前半沒有做步 4A（report_A.md 無 4A 段）→ 看價後、步 9 之前依 SKILL §2 步 4A（procedure/11）補做困境專門檢查，報告寫 4A 段：三問全過 → 不因困境降級；任一不過 → 等級降一級；需要整個商業模式翻轉 → signal＝NO_ACTION、reason_code＝REJECT、stopped_at_gate 填 4A（報告寫明停在看價後補做的步 4A）；⑤ 在步 2 的 G9 判定。困境與跌幅都不是買進理由。價格檔沒有此欄或為空 → 不補做。

本次觸發：{trigger}

要做：
1. 步 9 G7：寫 `{work}/{ticker}/cap_input.json`（格式見 `scripts/cap_calc.py` 說明），數字取自 phaseA.normalized_inputs 與價格檔；不得另造。框架會執行 cap_calc，並以腳本結果為準。各欄來源：
   - phaseA.normalized_inputs：`oe`＝oe_per_share、`g10`、`g5`、`norm_eps`、`roe`、`eps_cagr5`、`incr_roic_gt_rd`、`insurance`（三欄皆 null → 填 null）；`grade`＝步 10 完成後的最終確定性等級（看價後補判的降級——SEL-034 例外、P-26a、P-08 最終、步 4A——也要先套用；不得先含 P-21 降級：P-21 只適用非金融，由 cap_calc 自動套用、結果在 `grade_used`，降級計入 driver 成長離散度；設計決策 D22）。框架會以摘要與訊號卡 `certainty_grade` 較嚴者壓低 `grade`（摘要等級已含 cap_calc 的 P-21 降級時不重複降），再執行 cap_calc。
   - 框架覆寫（照填亦可，以框架為準）：`price`、`long_rate`、`long_rate_20y_med`（價格檔）、`largecap_pe_median`、`fin_pe_median`（凍結檔）、`financial`（phaseA.asset_class；REIT 屬非金融）、`moat_widening`（問卷 M1–M3 全強且 M5＝變寬）、`cap_discount_k`（設定），以及上一項 phaseA 的各估值欄。
   - `iv_cons`／`iv_base`／`iv_opt`、`base_g0`／`opt_g0` 一律不填（填了框架也清為 null）：金融類 IV_base／IV_opt 目前未定義 → 自動買進上限為 null，最多 WATCH（保險 IV_cons 用 insurance 三欄公式；設計決策 D20）；基準／樂觀起始成長由 cap_calc 依已證明成長（g10、g5）的預設口徑計算（D21）。
   - `w_rev`（P-05 W-REV）：非金融時框架依資料包 10 年財報營收判定——最近年度對 3 年前或 5 年前的年複合任一 ≤0 → 框架設 true（上限只用 IV0×等級係數），你不得改為 false；你依前半結論認為成立時可填 true（只能從嚴），否則省略。
2. 步 10–14：確定性等級、G8 機會成本、G9 反向思考與偏誤（逐條作答）、G10 三件凍結、G11 部位（§5）。摘要與訊號卡 `certainty_grade` 寫最終等級：須含 cap_calc 的 P-21 降級（若適用，即 `grade_used`）。
   看價後補判（前半價格盲而留下的項目；前半結論不改寫，只能更嚴）：report_A.md 寫「SEL-034 待 G7 判例外」→ 以價格檔判 SEL-034 例外（稅後盈餘殖利率約 ≥10% 且折讓遠大於一般標的）：成立 → 等級降一級續行，不成立 → signal＝NO_ACTION、reason_code＝REJECT、停止點 stopped_at_gate＝G7（procedure/01 §2）。P-26a 回購價格面：讀價格檔的回購均價與金額，依 procedure/02 §2 補評（回購均價 `avg_buyback_basis` 標「未比對」或 `avg_buyback_warnings` 有該年警示的年度，不作 P-26a 扣分依據），補評後的資本配置淨分寫在摘要 `capital_allocation_net_after_price`（數字；未補評或無回購資料填 null，並在報告寫明原因）；補評後 <0 而 phaseA.downgrades 沒有資本配置 driver → 等級再降一級，已有則不重複降（D18；漏降時框架依此欄壓回）。
   P-08 最終（procedure/01 §6；設計決策 ASK-3＝B）：補評 B7（回購均價對 cap_calc `IV_cons`，口徑同 P-26a；均價標「未比對」或帶警示的年度不計）與 B11（保留盈餘兩段檢驗），資料不足記缺；連同 report_A.md 的「P-08 暫定」5 項，以可計 7 項中有資料者算最終通過率，report.md 寫一行「P-08 最終：有資料 n/7、通過 m、通過率 x%、品質 優｜可｜降級」。品質優＝通過率 ≥60% 且有資料 ≥4 項；40–60% 或有資料 <4 項 → 可；<40% → 降級。只能更嚴：最終 <40% 而 phaseA.downgrades 沒有「品質加分」driver → 等級再降一級；前半已因 P-08 降級者不重複降、品質最高「可」。後半不因 P-08 停 MISSING_DATA。訊號卡另填選填欄 `p08_final`（設計決策 D77；格式見 signal_card_schema）：`items_with_data`＝n、`passed`＝m、`rate`＝x（百分數）、`quality`＝優｜可｜降級，四值須與 report.md「P-08 最終」行一致；停止未算出最終分數時填 null。
   步 4A ⑤（鐵律 7）：前半有 4A 段或有困境標記（T1–T5）者，G9 刪掉理由中所有「跌很多、大家都怕、創新低」類句子後重跑 G7；⑤ 不成立 → 輸出 WATCH，stopped_at_gate G9、reason_code 填 PASS_ALL（不另降級、不用停止碼；procedure/11 ⑤、SKILL §0）。
   G9 已證明成長：cap_calc `g9_reason_required`＝true（10 年與 5 年成長差 >5pp，已證明成長取較高者）時，訊號卡 `g9_growth_reason` 必填一句理由（同 G9 反方所寫）；缺 → 框架退回一次，仍缺 → BUY 改 WATCH（停在 G9）。不需要時填 null。
   G8 比較表各列照抄 `{work}/portfolio/holdings_g8.json`（框架已用決策日收盤價與 cap_calc 重算，不得另算或改用買進成本）：加碼現有最佳持股＝`best_addable`、觀察名單次佳＝`best_watch`（只含今天收盤低於自動買進上限的 A／B 級）、短期國庫券＝`benchmarks.treasury.adjusted`、大盤＝`benchmarks.market.adjusted`（P-66 分區與 P-27 取值寫在該列）；持股 ≥10 檔時本標的另須勝過 `weakest`。本標的的調整後預期報酬＝(cap_input 的 oe ÷ 價格＋cap_calc 保守 g0)×等級係數，框架以腳本重算寫入訊號卡 `g8_adjusted_expected_return`。比較表填進摘要的 `g8`；停在 G7 未走到 G8 時數字填 null、winner 填 `未比較`。某列有 `error` 或缺值時在報告寫明，該列不得當作輸給或勝過的依據。
   框架覆核：任一列高出本標的 ≥1pp（或持股 ≥10 檔時最弱持股高出 ≥1pp），或本標的算不出調整後預期報酬 → BUY 一律改 WATCH（停在 G8）；差 <1pp 由你依品質判斷。
3. 步 15：依 §4 一致性檢查，輸出報告 `report.md`（含前半）、訊號卡、登錄表一列、決策日誌一列、論點檔 `thesis.md`。論點檔的保守價值區間段要列出 phaseA.thesis_draft.key_assumptions（3–5 個關鍵假設，可依後半結果修訂措辭，不得刪減成少於 3 條）。登錄表一列須含 `kill_criteria_due`（[{text, due}]，逐字沿用 phaseA.thesis_draft.kill_criteria 的 text 與 due，只能新增；漏寫時框架以 phaseA 補回）。論點檔的估值數字（IV、買價上限、安全邊際、P/E、殖利率、預期報酬）寫成獨立的行，不要和論點或失效條件寫在同一行：價格隔離工作讀論點檔時，含這類字詞與數字的行整行刪除。
   G11 意願權重：在摘要 `intended_weight` 給 `level`（HIGH | MEDIUM | LOW）與一句 `reason`，理由必須引用 G8 優勢差距與確定性等級（沒有理由或不提 G8 → 輸出不合格）。框架換算 HIGH 100%／MEDIUM 75%／LOW 50% × 等級上限（A 15%／B 10%），再與訊號卡 `max_weight_pct` 各項（P-29 尾部、P-30 單事件）與宿主上限取最小，<3% 不建倉（`max_weight_pct.single_event` 由框架依近 8 次財報日跌幅計算覆寫，你可填 null；取不到時框架寫 null 並記「P-30 未檢查」）。P-29 尾部：在摘要填 `tail_worst_loss_pct`（數字或 null，百分數：合理最壞損失＝(1 − 悲觀價值 ÷ 現價)×100，例如 40＝40%），框架以 10pp ÷ max(25%, 該值) 寫入並覆寫訊號卡 `max_weight_pct.tail`（P-31 群最壞損失同用此值）；缺或不是數字時框架記警示，取訊號卡值與 P-32 減損（金融 50%、非金融 30%）口徑較嚴者；以此覆寫訊號卡 `target_weight_pct`。未走到 G11 → `level`、`reason` 填 null；BUY 必填。
   權重單位：訊號卡 `target_weight_pct` 是百分數（10＝10%），摘要 `target_weight` 是小數（0.10＝10%），兩者只供報告。首批比例由框架固定為目標的 50%：訊號卡 `first_tranche_pct_of_target` 填 50，不得套用 procedure/06 的 70% 加速條款（自動模式不用）。
4. BUY 只在 SKILL §0 條件全成立且自動買進三閘門全過時；否則最多 WATCH，並附上限。reason_code：BUY、WATCH 一律填 PASS_ALL（WATCH 停在哪一步寫 stopped_at_gate），停止碼（TOO_HARD | REJECT | MISSING_DATA | RED_LIGHT）只配 NO_ACTION；NO_ACTION 不得填 PASS_ALL。閘門 3 問卷不過時框架寫回最多 WATCH（reason_code GATE3_FAIL）；問卷有停止級答案（C1 寫不到5年、C2–C4 否、C5 屬於且加嚴條件未全部成立、M4 否、M5 變窄、M6 可怕，或 M1–M3 強少於 2 項）或答「不知道」時，框架以較嚴的 REJECT／TOO_HARD 寫回（設計決策 D24）。
5. P-31／P-32 相關群（G11）：
   - 逐條看論點檔的失效條件，凡描述外部變數者（油價、利率、消費信用、新屋開工…），以受控詞彙 `{pkg}/berkshire-deep-merged/assets/external_drivers.json` 的 id 標進摘要 `external_drivers`（每列 driver＋該條失效條件原文；一條可標多個 id；沒有任何一條描述外部變數 → 空陣列）。詞彙沒有合適 id 時可自訂小寫 id（框架只以完全相同字串分群並記警示）。
   - 本檔既有判定：{prior_external_drivers}
   - 框架依共同變數分群（連通分量），算群上限 min(2×單檔等級上限, 10pp÷群最壞相對損失) 與 P-32 共同情境損失（Σ權重×減損；金融 50%、非金融 30%），框架把同群與剩餘額度寫進訊號卡 `correlated_group`（報告用）；訊號卡 `max_weight_pct.correlated_group` 框架不採用，同群剩餘額度在下單當時依持股即時限制；該群 >5% → BUY 一律改 WATCH（停在 G11）。持股現況見 `portfolio/holdings_g8.json` 的 `correlation`。報告第 ⑥ 段寫出同群持股與剩餘額度。
6. 未評估的數值欄填 null，不得填 0：停在 G11 之前 → 摘要 `target_weight`、`first_tranche_weight`（兩者選填，框架不採用，可省略）與訊號卡 `target_weight_pct` 填 null；未走到 G9 → 訊號卡 `bias_count` 兩項 null。0 只代表已評且為零（例如 G11 算出目標權重 <3% 不建倉）。

輸出：其餘檔案寫到 `{work}/{ticker}/`；最後的回覆只輸出 FULL_B 格式的 JSON 物件（摘要）本身，前後不加任何說明文字。
