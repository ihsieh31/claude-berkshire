# 訊號 → 模型 對應（給外部交易框架）

兩個 skill 與所有腳本**都不呼叫模型**，只產出訊號檔與工作所需的資料。外部框架負責：讀訊號 → 建工作 → 代入提示模板 → 調用模型 → 以 `output_schemas.json` 驗證 → 執行後置腳本 → 回寫狀態。

| 檔案 | 內容 |
|---|---|
| `model_routing.json` | 訊號欄位怎麼轉成工作、每個工作用哪個模型、輸入、輸出、上下文上限、升級規則、狀態回寫、成本粗估。程式只讀各工作的模型等級（`model`）與上下文上限（`max_context_tokens`），其餘是說明；代理式後端（claude_code）的上限只估提示本身，模型自讀的沙箱檔不計 |
| `output_schemas.json` | 每個工作的 JSON 輸出格式（框架驗證用） |
| `prompts/_COMMON.md` | 所有工作共用的系統提示前段 |
| `prompts/{工作}.md` | 各工作模板；`{佔位符}` 由框架代入 |

## 流程

```
每季：screen.py → result_handoff.json
  core_shortlist 每檔（登錄表 12 個月內已完整分析且非 MISSING_DATA → 跳過）
    → build_dossier.py → DOSSIER(弱) → FULL_A(強, 無價格)
       → gate3_check.py → [停止？→ 寫結論] → FULL_B(中, 給價格) → cap_calc.py 覆核
       → signal=BUY → RED_TEAM(強) → 退回/維持；修正後仍致命 → NO_ACTION（RED_LIGHT，G9；D24）
  全部完成 → PORTFOLIO（程式，不用模型）→ 下單

每兩週：review_due.py → due.json
  triage 非空 → TRIAGE(弱) → 是/不確定 → 升為 THESIS
  FULL   → 同上 DOSSIER → FULL_A → FULL_B → RED_TEAM
  THESIS → THESIS(持股中／其他弱) → 證偽先經框架核對（D02）：
           持股證偽且核對通過 → SELL_ALL（下一交易日起），不升 FULL；
           持股證偽但核對不過或屬經濟特性惡化 → 疑似（停止加碼）＋同輪 HOLD_REVIEW（中）＋ FULL；
           其他疑似/證偽 → FULL；完好且同期有 PRICE 型觸發 → PRICE
  PRICE  → cap_calc.py（更新現價、長債、P/E 中位）→ PRICE(弱) → 框架判定（動作不採信模型，D28）：
           needs_full（框架亦自行比對：cap_calc 或上次訊號卡等級與登錄表不同）、
           G10 不成立或 G9 空白、閘門 3 已過期、本次長債缺（rates_stale）→ FULL；
           非持股三閘門全過 → FULL（近 90 天完整分析已看過可買價位者維持 WATCH）；
           持股：現價 ≥ 荒謬級線，或現價 ≥ IV_base 且建倉滿一年、不低於上次 sell_review_above（P-25）
           → 寫 PRICE 結論後交 HOLD_REVIEW（中）；其餘寫 PRICE 結論 → PORTFOLIO
  NONE   → 不做
```

## 框架必須以程式保證的事（不要交給模型自律）

1. **價格隔離**：`FULL_A`、`TRIAGE`、`THESIS`、`DOSSIER` 的輸入不得含 `*_price.json`、股價、市值、本益比、初篩排名（交接檔每檔的 `price_context` 整個刪掉）。`FULL_B` 開價格檔前，框架以 `gate3_answers.json` 的 mtime 早於開檔時間為閘（不早於就不開），開檔時間寫進沙箱的 `price_file_opened_at.txt`，並留存在分析目錄、路徑寫進決策日誌 `price_file_opened_at_file`；作答先於看價由此框架保證，作答檔的 `price_file_opened_at` 欄在 FULL_A 留空（M2-D10-04）。
2. **數字以腳本為準**：`FULL_B` 之後跑 `cap_calc.py`，`auto_buy_cap`、IV 與訊號卡不一致 → 以腳本為準並退回。
3. **閘門 3 以腳本為準**：`gate3_check.py`（決策日以工作的 asof 為準）不過 → 訊號最多 WATCH（reason_code `GATE3_FAIL`），無論模型寫什麼；問卷有停止級答案（C1 寫不到5年、C2–C4 否、C5 屬於且加嚴條件未全部成立、M4 否、M5 變窄、M6 可怕，或 M1–M3 強少於 2 項）或答「不知道」→ 回寫取較嚴的 REJECT／TOO_HARD、不給 auto_buy_cap；PRICE 快速通道遇上次結論 `GATE3_FAIL`／`RED_LIGHT`／`FRAMEWORK_HOLD` 不得升成買進（設計決策 D24）。FULL_B 判 BUY、框架降成 WATCH（G8、P-32、G9、D26、價格盲缺口、持股前半、紅隊未過）時 reason_code 由 `PASS_ALL` 改 `FRAMEWORK_HOLD`，閘門值不改（A3-M5B-01）。
4. **組合層用程式**：名額、現金下限、首批 50%、每日新部位上限、權重上限，不交給模型。
5. **輸出不合格**：重試 1 次（重建沙箱），仍不合格 → 框架依工作寫保守結論，不得採用自由文字：DOSSIER／FULL_A／FULL_B → `NO_ACTION/MISSING_DATA`（持股另交 HOLD_REVIEW）；TRIAGE → 升 THESIS；THESIS → 疑似（停止加碼）並升完整分析；PRICE → 閘門未過紀錄；RED_TEAM → WATCH；HOLD_REVIEW → HOLD＋停止加碼；CORP_ACTION → 異常＋暫停該檔。這類工作記 `conservative_fallback`：不算失敗、不擋當日買進、`jobs retry` 不重跑，另記一筆低嚴重度異常。傳輸或準備失敗（非格式失敗）的工作仍記 `failed`：PRICE、THESIS 比照上列寫保守紀錄；RED_TEAM → WATCH；深度鏈（DOSSIER／FULL_A／FULL_B）不寫 `MISSING_DATA`、不動登錄表，持股接 HOLD_REVIEW，其餘交 run 收尾。run 結束時仍沒有任何結論的檔，持股記 HOLD＋停止加碼（該次失敗經 `jobs retry` 修好、異常結案或之後有新結論即解除）、其他記 WATCH（不動登錄表，下次觸發照常重排）。模型輸出失敗（含 FULL_A 缺輸出檔 G6、FULL_B 無法產出 G7）與使用者拒絕強模型的 `MISSING_DATA` 是 D80 非分析性停止：三閘門寫 null（日誌 `non_analytic_stop`）、不撤未成交買單、非持股觀察檔的估值欄與價格線保留（D87）；金融前置檢查（D20）與資料包建立失敗屬資料檢查不過，仍失敗關閉（A3-M5A-01）。
6. **亂序回寫（D86）**：工作啟動（同意之後、呼叫模型之前）與 post／保守回寫前，該檔已有 `decision_date` 晚於工作資料日的決策（PRICE、佔位、run 收尾、TRIAGE 判否、D80 非分析性停止不算）→ 工作改 `skipped`（error `stale_asof…`），不回寫、不寫收尾保守結論、不算失敗，由下次觸發以新資料日重排。各回寫函式的較新狀態守衛保留為第二道防線（A3-M6B-01/02/03、A3-M6D-01/02/03）。

## 合併版推論（未經回測，上線前紙上交易驗證）

- 前後兩段式完整分析（`FULL_A`／`FULL_B`）：把 skill 的「價格最後看」改由框架實體隔離。
- `TRIAGE` 初判：補足 `review_due.py` 看不懂 8-K 內容與新聞的缺口；有疑慮一律升級，寧可多花一次 `THESIS`。
- `PRICE` 工作中「閘門 3 問卷 12 個月內有效」：與完整分析至遲 12 個月重做一致。
- 模型分級（強／中／弱）：FULL_A（每檔完整分析都跑）、RED_TEAM（只在擬 BUY 時跑）用強；FULL_B（只在通過 G6、FULL_A 未停止後才跑）、持股的 THESIS、HOLD_REVIEW 用中；抽取、是非題、機械計算用弱。FULL_A 次數其實多於 FULL_B，維持現行分級是使用者決定（設計決策 D32，考量額度），紙上交易期間再比較。試點只驗證過中模型執行完整分析的價格隔離與 `MISSING_DATA` 停止；FULL_A 用強模型未經驗證。
