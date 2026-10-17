# 訊號 → 模型 對應（給外部交易框架）

兩個 skill 與所有腳本**都不呼叫模型**，只產出訊號檔與工作所需的資料。外部框架負責：讀訊號 → 建工作 → 代入提示模板 → 調用模型 → 以 `output_schemas.json` 驗證 → 執行後置腳本 → 回寫狀態。

| 檔案 | 內容 |
|---|---|
| `model_routing.json` | 訊號欄位怎麼轉成工作、每個工作用哪個模型、輸入、輸出、上下文上限、升級規則、狀態回寫、成本粗估 |
| `output_schemas.json` | 每個工作的 JSON 輸出格式（框架驗證用） |
| `prompts/_COMMON.md` | 所有工作共用的系統提示前段 |
| `prompts/{工作}.md` | 各工作模板；`{佔位符}` 由框架代入 |

## 流程

```
每季：screen.py → result_handoff.json
  core_shortlist 每檔（登錄表 12 個月內已完整分析且非 MISSING_DATA → 跳過）
    → build_dossier.py → DOSSIER(弱) → FULL_A(強, 無價格)
       → gate3_check.py → [停止？→ 寫結論] → FULL_B(中, 給價格) → cap_calc.py 覆核
       → signal=BUY → RED_TEAM(強) → 退回/維持
  全部完成 → PORTFOLIO（程式，不用模型）→ 下單

每兩週：review_due.py → due.json
  triage 非空 → TRIAGE(弱) → 是/不確定 → 升為 THESIS
  FULL   → 同上 DOSSIER → FULL_A → FULL_B → RED_TEAM
  THESIS → THESIS(持股中／其他弱) → 疑似/證偽 → FULL
  PRICE  → cap_calc.py → PRICE(弱) → needs_full → FULL；BUY_CANDIDATE/SELL_* → PORTFOLIO
  NONE   → 不做
```

## 框架必須以程式保證的事（不要交給模型自律）

1. **價格隔離**：`FULL_A`、`TRIAGE`、`THESIS`、`DOSSIER` 的輸入不得含 `*_price.json`、股價、市值、本益比、初篩排名（交接檔每檔的 `price_context` 整個刪掉）。`FULL_B` 建立前確認 `gate3_answers.json` 已存檔，並由框架填入 `price_file_opened_at`。
2. **數字以腳本為準**：`FULL_B` 之後跑 `cap_calc.py`，`auto_buy_cap`、IV 與訊號卡不一致 → 以腳本為準並退回。
3. **閘門 3 以腳本為準**：`gate3_check.py` 不過 → 訊號最多 WATCH，無論模型寫什麼。
4. **組合層用程式**：名額、現金下限、首批 50%、每日新部位上限、權重上限，不交給模型。
5. **輸出不合格**：重試 1 次，仍不合格 → `NO_ACTION/MISSING_DATA` 並寫日誌；不得採用自由文字。

## 合併版推論（未經回測，上線前紙上交易驗證）

- 前後兩段式完整分析（`FULL_A`／`FULL_B`）：把 skill 的「價格最後看」改由框架實體隔離。
- `TRIAGE` 初判：補足 `review_due.py` 看不懂 8-K 內容與新聞的缺口；有疑慮一律升級，寧可多花一次 `THESIS`。
- `PRICE` 工作中「閘門 3 問卷 12 個月內有效」：與完整分析至遲 12 個月重做一致。
- 模型分級（強／中／弱）：核心判斷且次數少（FULL_A、RED_TEAM）用強；需判斷但次數多（FULL_B、持股的 THESIS）用中；抽取、是非題、機械計算用弱。試點只驗證過中模型執行完整分析的價格隔離與 `MISSING_DATA` 停止；FULL_A 用強模型未經驗證。
