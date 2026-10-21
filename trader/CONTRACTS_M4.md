# M4 介面契約：觸發檢查（review）

規則來源（不得改動、不得複製公式）：
- `berkshire-deep-merged/scripts/review_due.py`
- `routing/model_routing.json`（`signals.review_due`、`jobs.TRIAGE/PRICE/THESIS`、`state_writes`）
- `routing/prompts/{TRIAGE,PRICE,THESIS}.md`
- `routing/output_schemas.json`

## 0. 共用約定
- 代號一律 `data.universe.normalize_ticker`。
- 價格隔離：TRIAGE、THESIS 沙箱內不得有任何價格（`PRICE_ISOLATED_JOBS`）；PRICE 可有價格。
  - 先確認 `models.PRICE_ISOLATED_JOBS` 內容：TRIAGE、THESIS 必須在內，PRICE 必須不在內。
- 秘密只從 cfg／環境變數取，不寫進程式。
- 測試不連網：SEC 用 `--offline`；Alpaca、FRED 用假物件。

## 1. 工作參數（pipeline 建立；handler 讀取）

### 1.1 基本參數
每個 review 工作的 `params` 都含「基本參數」。基本參數與 `pipelines/analyze.py` 的 DOSSIER params 相同：
- `price`、`price_date`、`rates`、`screen_context`、`portfolio_state`、`largecap_pe`

### 1.2 review 參數
另外再加：

| 鍵 | 內容 |
|---|---|
| `holding` | bool，登錄表 status == HOLDING |
| `status` | 登錄表 status |
| `cik` | CIK |
| `reasons` | due.json 的 reasons |
| `triage` | due.json 的 triage |
| `kill_criteria` | list[str] |
| `kill_criteria_due` | 登錄表原列 |
| `thesis_file` | 絕對路徑字串或 None |
| `last_review_date` | 日期 |
| `base_action` | due.json 的 action：NONE／PRICE／THESIS／FULL |

### 1.3 升級時的參數傳遞
- handler 升級建立新工作時，一律帶上原工作 params 的完整副本：`next_job(job, T, ctx, {**job.params, ...})`。
- 例外：PRICE、THESIS 以外的工作，要拿掉 `created_ts`，由 `next_job` 重設。

## 2. pipelines/review.py（子代理 A）

```python
class ReviewError(Exception): ...

def export_registry(conn, scope: str) -> list[dict]
    # scope: holdings（HOLDING）| watch（WATCH）| all（全部登錄表列）
    # 每列 = registry.data（dict），補 ticker、status（取欄位值）
    # 缺 cik 的列由呼叫端用 sec.cik_for 補；補不到則排除並記在 summary["skipped"]

def run_review(cfg, paths, conn, asof, scope="all", run_id=None, clients=None, router=None,
               offline_filings: Path | None = None, due_only=False) -> dict
```

### 2.1 流程
1. **run_id**：預設為 `review-{asof}-{scope}`。runs 表的 kind 寫 `review`，做法同 screen。
2. **工作目錄**：`out = paths.work / run_id`。
   - `out/registry.json` = `{"schema": "berkshire-deep-merged/registry-v1", "entries": [...]}`
3. **價格**：寫 `out/daily.csv`，欄位 `ticker,date,close`。
   - 來源：`clients["alpaca"].bars(tickers, start=asof−200 曆日, end=asof)`，adjustment 用 raw。
   - date 必須 ≤ asof。
   - 沒有 alpaca 時不給 `--prices`。注意：skill 會改抓 Yahoo，所以這種情況要傳 `--prices` 指向一個只有表頭的空檔，避免連網。
4. **t30**：`clients["fred"].t30(asof)`，回傳 (值, 日期)。
   - 取不到時不傳 `--t30`，並記入 summary。
5. **新聞**：`clients["alpaca"].news(tickers, start=min(last_review_date), end=asof)`。
   - 轉成 `{ticker: [{date, headline, source, url}]}`，寫入 `out/news.json`。
   - 沒有 alpaca 時不傳 `--news`。
6. **跑 skill**：用子程序執行 `review_due.py`。
   - 參數：`--registry`、`--asof`、`--prices`、`[--t30]`、`[--news]`、`[--offline]`、`--out out/due.json`。
   - 環境變數 `SEC_USER_AGENT` 取自 sec client（見 `analysis.dossier_prep._user_agent`）。
   - timeout 1800 秒。非 0 結束碼 → 丟 ReviewError。
7. **讀結果**：讀 due.json，檢查 `signal_version == "review-due-v2"` 且 asof 一致。
8. **`due_only=True`**：到此回傳 summary，不建工作、不呼叫模型。
9. **建工作**：每檔最多一個起始工作，job_id 為 `{run_id}:{T}:{TYPE}:1`。

   | 條件 | 建立的工作 |
   |---|---|
   | `triage` 非空 | TRIAGE（base_action 保留原 action，此時只會是 PRICE／NONE） |
   | action == FULL | DOSSIER（params 同 analyze） |
   | action == THESIS | THESIS |
   | action == PRICE | PRICE；但若 `paths.state/"cap_inputs"/f"{T}.json"` 不存在，改建 DOSSIER，並在 summary 註明 |
   | action == NONE 且 triage 空 | 不建工作 |

10. **組合狀態與 largecap_pe**：沿用 analyze 的 `_portfolio_state`、`get_frozen`。
    - 可把 analyze 中組 params 的程式抽成 `analyze.base_params(...)` 共用；不得改變 analyze 的行為。
11. **跑引擎**：`engine.ctx.sec = clients["sec"]`，然後 `engine.run(run_id)`。
12. **回傳 summary**：`{run_id, asof, scope, counts: {action: n}, jobs: {T: TYPE}, skipped, engine, due_path}`，同時寫到 `out/summary.json`。

### 2.2 CLI（cli.py）
```
trader review --asof D [--scope holdings|watch|all] [--run-id X] [--due-only] [--offline-filings F]
```
寫法比照 `cmd_analyze`。

### 2.3 測試：`tests/test_review_pipeline.py`
- 用假的 alpaca、fred、sec client，並用 `--offline` 申報檔。
- 以登錄表各 status 組合驗證：
  - due_only 輸出
  - 每檔只建一個工作
  - PRICE 缺 cap_inputs 時改建 DOSSIER
  - TRIAGE 帶 base_action
- 用 FakeRunner 跑一次 TRIAGE → THESIS 的整條流程（這部分仰賴子代理 B 的 handler；若尚未完成，用 xfail 標註，由主代理補上）。

## 3. jobs/review_jobs.py ＋ writeback（子代理 B）

`handlers.py` 中原有的 TriageHandler、PriceHandler、ThesisHandler 改為從 `review_jobs.py` 匯入（`HANDLERS` 的鍵不變）。既有的 `tests/test_engine.py` 必須照樣全部通過；若行為改變，改測試並說明理由。

### 3.1 共用
- **論點檔**：`thesis_files(paths, ticker, thesis_file)` 回傳 `[主檔, *addendum 依序]`。
  - 主檔：params 的 `thesis_file` 存在就用它；否則取 `theses/{T}_*.md` 中日期最新、且非 addendum 的那份。
  - addendum：`{主檔名去 .md}_addendum_*.md`。
- **提示佔位符**（`prompt_values`）：`{ticker}`、`{asof}`、`{thesis_file}`（沙箱內路徑）、`{kill_criteria}`（逐行 `- `）、`{triage_items}`（JSON 區塊）、`{last_review_date}`、`{tickers}`、`{pkg}`、`{work}`。
  - 參考 `deep._vals` 的寫法：Claude Code 給絕對沙箱路徑，API 給相對路徑。

### 3.2 TRIAGE
- **輸入**：論點檔，加上 triage 中 type == 8-K 且有 url 的原文。
  - 原文用 `ctx.sec.filing_text(url)` 抓，再經 `drop_price_sections`、`scrub_price_text` 處理。
  - 每份最多 30,000 字元，存成 `sec/8-K_{date}.txt`。這段放在 `pre_script` 裡做。
- **escalate == true** → THESIS。
- **否則**：
  - base_action == PRICE → PRICE；但 cap_inputs 不存在時改建 DOSSIER。
  - 其他情況 → `writeback.touch_review(conn, T, asof)`，只更新 `last_review_date`。
- **on_invalid**：維持現狀（升級 THESIS，deps 為空），params 照 §1 帶完整副本。

### 3.3 THESIS
- **輸入**：論點檔，加上「上次分析後的新申報節錄」。
  - 來源：`ctx.sec.filings(cik, asof, since=last_review_date)` 中 10-Q、10-Q/A、8-K，日期須 > last_review_date 且 < asof。
  - 原文用 `filing_text`，經同樣的去價格處理。
  - 10-Q 只留 MD&A 及其後；找不到時取前 40,000 字。
  - 每份最多 40,000 字元，全部合計最多 150,000 字元，從最新的開始取。
- **post**：呼叫 `writeback.write_thesis_result(conn, paths, T, asof, run_id, result, params)`。
  - 決策日誌的 action：
    - HOLDING：完好或疑似 → HOLD；證偽 → SELL_ALL
    - 非持股 → WATCH
  - `thesis_check` 寫 verdict；`failure_conditions_hit` 寫 status 為「已觸發」的條件。
  - 登錄表只更新 `last_review_date` 與 `review_price`（取 params.price）。
  - 論點增補檔：用 `_write_thesis` 的 addendum 機制，寫入 THESIS 結果的 markdown 摘要。
- **next_jobs**：verdict 為疑似或證偽 → DOSSIER（帶完整 params）。

### 3.4 PRICE（一檔一個工作）
- **pre_script**：
  1. 讀 `paths.state/"cap_inputs"/f"{T}.json"`。
  2. 用 params 更新 `price`、`long_rate`（取 `rates["t30"]`），有值時更新 `long_rate_20y_med`、`largecap_pe_median`。
  3. 以 `checks.run_cap_calc` 計算，結果寫入沙箱的 `{T}/cap_result.json` 與 ticker_dir。
- **輸入**：
  - cap_result.json
  - 上次訊號卡：signals 表中該檔最新一列的 data
  - 登錄表該列
  - 組合狀態
  - 論點檔
- **prompt_suffix**：「本工作只有一檔：輸出單一 JSON 物件（不是陣列）。」
  - engine 也會接受長度 1 的陣列，這部分由主代理改。
- **post（框架覆核，不信模型的數字）**：
  1. `auto_buy_cap`、`sell_review_above`（= IV_base）、`absurd_price_line` 一律以 cap_result 覆寫。
     - 鍵名見 cap_calc 輸出與 `checks._IV_MAP`；荒謬級線的鍵名請到 cap_calc.py 確認。
  2. gate2 由框架重算：price < cap。cap 為 None 時判「無上限」。
  3. gate3 由框架判斷：取該檔最近一次 FULL_B 決策（decisions 表）的 `gate3_pass == true`，且距今 ≤ 365 天。
- **proposed_action：選填、只記日誌，動作由框架算（設計決策 D28；賣出側 D03）**：
  - 模型的 `proposed_action` 不採信，只寫進 `result.model_proposed_action` 與決策日誌的 `framework_notes`。
  - 買進側（`review_jobs._framework_action`）：只看框架三閘門、是否持股、權重與目標權重。

    | 條件 | action |
    |---|---|
    | 三閘門全過，未持股 | BUY 候選 → 交完整分析（近期完整分析已看過可買價位仍未判 BUY → WATCH） |
    | 三閘門全過，持股且權重 < 目標權重（權重或目標權重缺 → 不加碼） | ADD |
    | 閘門未全過 | 未持股 WATCH；持股 HOLD |
    | 持股已達目標權重 | HOLD |

  - 賣出側（`review_jobs._sell_route`，只決定要不要交 HOLD_REVIEW；PRICE 本身不寫 TRIM，`writeback.PRICE_ACTIONS` 不含 TRIM）：

    | 條件（持股） | 結果 |
    |---|---|
    | 現價 ≥ 荒謬級線 | HOLD，交 HOLD_REVIEW（第一年內也送；回寫端再擋價格類賣出） |
    | 現價 ≥ IV_base 且建倉未滿一年 | HOLD，只停止加碼，不送 |
    | 現價 ≥ 新 IV_base 但 < 登錄表上次 `sell_review_above`（P-25） | HOLD，只停止加碼，不送 |
    | 其他現價 ≥ IV_base | HOLD，交 HOLD_REVIEW |

  - `g10_still_valid` 為 false 或 `g9_contrarian` 空白 → `needs_full`，交完整分析，不回寫（P-63）。

- **寫入**：`writeback.write_price_result(...)`。
  - 決策日誌：gate1/2/3、price、`buy_price_cap`、`price_to_cap`。
  - 登錄表更新：`last_review_date`、`review_price`、`t30_at_valuation`、`auto_buy_cap`、`sell_review_above`、`absurd_price_line`。
  - 訊號卡：沿用上一張，更新數字並加 `framework_notes`。
- **needs_full == true** → DOSSIER。

### 3.5 writeback 新增函式
| 函式 | 說明 |
|---|---|
| `touch_review(conn, ticker, asof)` | 只改 data 的 last_review_date 與 updated_at；登錄表無該列時什麼都不做 |
| `write_thesis_result(...)`、`write_price_result(...)` | decision_id 用 `next_decision_id`；以 `_write_rows` 在單一交易內寫入 |

- **修改 `write_full_result`**：把 `tdir/cap_input.json` 複製到 `paths.state/"cap_inputs"/f"{T}.json"`（覆寫；檔案不存在時略過），並在 registry entry 寫入 `cap_input_file`。
- **登錄表一律合併**：先讀既有 `data`，只更新指定欄位，絕不清掉其他欄位。

### 3.6 測試：`tests/test_review_jobs.py`
- 價格隔離：TRIAGE、THESIS 沙箱沒有價格；PRICE 有 cap_result。
- PRICE 覆寫模型的數字、閘門重算、建倉未滿一年不賣。
- THESIS 的 action 對應與登錄表合併。
- 升級鏈 TRIAGE → THESIS → DOSSIER；TRIAGE（未升級）→ PRICE 或 touch。
- `write_full_result` 會保存 cap_input。
