# claude-berkshire：波克夏式長期選股與紙上交易

把波克夏式的選股規則寫成兩個 skill，再由 `trader/` 框架自動執行：

1. **初篩**（每季）：S&P 500 → 刪結構性垃圾 → 排除統計高風險 → 研究優先排序 → 前 15 檔。
2. **深度分析**：G0–G11 閘門、價格隔離（前半看不到價格）、閘門 3 問卷、`cap_calc.py` 算買價上限與確定性等級，產出 BUY／WATCH／NO_ACTION 訊號卡；強模型工作要使用者同意才跑。
3. **trader 紙上交易**：每日收盤流程（公司行動、撮合、觸發檢查、組合計畫、淨值）、紙上帳本、儀表板、匯出與檢討報告。

只做紙上交易（Alpaca paper 帳戶的行情＋本機帳本），不對任何券商送出真實委託。模型由框架依 `routing/` 調用（預設 Claude Code `claude -p`）；skill 腳本本身不呼叫模型。

## 目錄結構

| 路徑 | 內容 |
|---|---|
| `berkshire-screen-merged/` | 初篩 skill：`SKILL.md`、`references/`（證據與資料來源）、`scripts/`（`fetch_edgar.py`、`fetch_prices.py`、`screen.py`、`screen_quant.py`、`test_screen.py`） |
| `berkshire-deep-merged/` | 深度分析 skill：`SKILL.md`、`references/`（`rulebook.md`、`procedure/`、`rule_index/`、`knowledge/` 思考背景、`inferred_thresholds.md`）、`assets/`（閘門 3 問卷、訊號卡／登錄表 schema、報告範本）、`scripts/`（`build_dossier.py`、`cap_calc.py`、`gate3_check.py`、`deep_quant.py`、`review_due.py`、`test_review_due.py`） |
| `routing/` | 工作→模型等級（`model_routing.json`）、各工作提示（`prompts/`）、輸出格式（`output_schemas.json`）、說明（`README.md`） |
| `trader/` | 執行框架。`trader/trader/`：`data/`（SEC、Alpaca、FRED、股票池、P/E 中位）、`analysis/`（資料包、同業、內部人、檢查、回寫）、`jobs/`（工作引擎、沙箱隔離、輸出驗證、同意閘門）、`llm/`（模型後端）、`pipelines/`（screen／analyze／review／daily／bootstrap）、`portfolio/`（部位、G8 比較、相關群）、`ledger/`（帳本、撮合、公司行動）、`report/`（指標、匯出、檢討）、`dashboard/`、`web/`、`scheduler.py`、`cli.py`；`tests/` 全部測試；`desktop/` macOS 小儀表板（Swift）；`CONTRACTS*.md` 各里程碑介面契約 |
| `plan.md`、`plan_dashboard.md` | 整體規劃、儀表板規劃 |
| `handoff.md` | 交接紀錄：§0 現況、設計決策、修復總覽、待辦（新對話先讀） |
| `換機匯出與封存程序.md` | 換機重跑、匯出、本機封存舊分析與替換、還原 |
| `bug report/` | 歷次審查報告與重現腳本 |

## 安裝

需求：Python ≥ 3.11；Claude Code CLI（`claude`，已登入）；Alpaca 紙上交易帳戶金鑰；FRED 金鑰；SEC User-Agent（須含聯絡 email）。桌面小儀表板與 `scheduler install`／`desktop install` 用 launchd，只支援 macOS。

```bash
cd trader
python3 -m pip install -e ".[dev]"     # 安裝後可用 `trader` 指令（等同 python3 -m trader.cli）
cp .env.example .env                    # 填 SEC_USER_AGENT、ALPACA_API_KEY、ALPACA_SECRET_KEY、FRED_API_KEY
trader init                             # 建立 state 與資料庫，檢查設定與金鑰
trader doctor                           # 連線測試：SEC、Alpaca、FRED、各級模型（--skip-models 略過模型）
```

## 主要指令

一律在 `trader/` 下執行。共通參數：`--config`（預設 `TRADER_CONFIG` 或 `config.yaml`）、`--state-dir`（預設 `TRADER_STATE_DIR` 或專案根目錄的 `state/`）。日期格式 `YYYY-MM-DD`。會寫 state 的指令先取 `state/.pipeline.lock`，另一個寫入指令正在跑時直接結束（exit 2），等它跑完再執行。

| 指令 | 做什麼 | 常用參數 |
|---|---|---|
| `trader init` | 建立 state、資料庫，檢查設定與金鑰 | |
| `trader doctor` | 連線測試 | `--skip-models`；`--state`（只查本機 state，換機前後用）；`--timeout` |
| `trader fetch` | 抓 features.csv、prices.csv、daily.csv、公司行動、利率 | `--asof`（必填）、`--tickers`、`--out`、`--freeze-pe`、`--refreeze` |
| `trader screen` | 季度初篩：整個股票池 → `screen.py` → 交接檔（`state/screens/{asof}/`） | `--asof`、`--top 15`、`--special-max 0`、`--inputs DIR`（沿用既有輸入不重抓）、`--force` |
| `trader bootstrap` | 初篩＋深度分析（建立初始名單；會呼叫模型） | `--asof`、`--tickers`（略過初篩）、`--top`、`--dry-run`、`--force`、`--mark-screened` |
| `trader analyze` | 深度分析 DOSSIER → FULL_A → FULL_B → RED_TEAM（會呼叫模型） | `--asof`、`--tickers`、`--screen-date`、`--run-id` |
| `trader review` | 觸發檢查：登錄表 → 訊號 → 起始工作 | `--asof`、`--scope holdings｜watch｜all`、`--run-id`、`--due-only`（只產生 due.json，不呼叫模型） |
| `trader approvals list／approve／defer／reject` | 強模型同意閘門（FULL_A、RED_TEAM 預設要同意） | `approve ID… ｜ --all [--run]`；`reject ID… --note` |
| `trader run-approved` | 執行已同意的強模型工作 | `--run-id` |
| `trader daily` | 每日收盤流程：公司行動、撮合、觸發檢查、組合計畫、淨值 | `--date`（必填）、`--dry-run`、`--skip-review`、`--force`（略過紐約當日須已收盤檢查）。較早交易日未完成時不會先跑較晚日 |
| `trader jobs list／retry／resume／abandon` | 工作管理（retry、resume 會呼叫模型） | `--run-id`、`--ticker`、`--job-id`、`--no-run` |
| `trader ledger show／adjust／cancel／pause` | 紙上帳本：摘要、人工調整、撤單、暫停 | `adjust --note`（必填）＋`--cash`／`--deposit`／`--ticker --qty --avg-cost`／`--first-buy-date`／`--resolve ID`／`--unpause T`／`--unpause-all`／`--resolve-corp-action ID`；`pause [T｜--all] --note` |
| `trader trade buy／sell` | 人工下單（換股、相關群減碼等需人工決定者），寫 MANUAL 決策日誌，下一次 daily 撮合 | `--ticker --qty --note`；buy 必填 `--limit`；`--anomaly ID`、`--date` |
| `trader decisions void` | 作廢錯誤決策（不刪列） | `ID… --note`、`--superseded-by` |
| `trader registry retire` | 登錄表標下市／不再追蹤 | `T --note` |
| `trader db backup` | 本機資料庫快照到 `state/backups/`（保留最近 14 份＋每月最早一份） | `--note` |
| `trader dashboard` | 本機儀表板 API 與網頁（只聽 127.0.0.1） | `--port`（預設 8787） |
| `trader desktop build／install／uninstall／status` | macOS 小儀表板與開機自動啟動 | `install --port --no-widget` |
| `trader scheduler run／once／install／uninstall／status／reset／print` | 常駐排程（沒有期限）；登錄表為空、或還有待同意／未完成的分析工作時拒絕 install | `run --interval`、`reset [任務鍵]`、`print --cron` |
| `trader export` | 匯出持股、委託、成交、現金、公司行動、分析、淨值、用量、同意、異常、登錄表 | `--what all｜逗號清單`、`--format csv｜json｜xlsx`、`--out` |
| `trader validate-report` | 前瞻紙上交易檢討（procedure/13 §3） | `--start`、`--end`、`--no-prices`、`--out` |
| `trader run-job` | 單一工作，除錯用 | `JOB_TYPE TICKER --asof [--fake DIR]` |

第一次建立名單：`trader bootstrap --asof 日期` → `trader approvals list` → `trader approvals approve --all --run`。排程安裝前先讀 `handoff.md` §0。

## 設定檔

- `trader/config.yaml`（已附，只有環境變數名稱、沒有金鑰）；完整說明見 `trader/config.example.yaml`。主要區塊：
  - `account`：初始資金、滑價、手續費、現金緩衝、單檔硬上限、基準（SPY）。
  - `schedule`：時區、收盤時間、觀察名單檢查間隔、季度初篩日、每月 P/E 中位凍結日。
  - `models.strong／medium／weak`：後端 `claude_code`｜`openai_compat`｜`fake`（測試用）、模型、`effort`、逾時、併發；各工作用哪一級見 `routing/model_routing.json`，`job_tier_overrides` 可覆寫。
  - `budget`：每次 run 與每日的模型呼叫數、費用、輸入 token 上限（0＝不限，不得為負）。
  - `data`：SEC／Alpaca／FRED 金鑰的環境變數名稱、Alpaca feed、研究取價來源。
  - `valuation.cap_discount_k`：自動買進上限＝min(P2′, k × IV_base)，預設 A 0.95、B 0.85。
  - `dashboard`：host（只限 127.0.0.1）、port。
- `trader/.env`：金鑰（範本 `.env.example`）。設定檔只寫變數名稱；呼叫模型子程序時框架會移除金鑰環境變數。
- `state/` 內可放的使用者檔：`universe_override.txt`（自訂股票池）、`exclusions.csv`（人工排除）、`screen_overrides.csv`（撤銷結構性刪除：ticker,date,reason）、`cik_predecessors.json`、`share_class_ratios.csv`（覆蓋套件內同名對照）。

## 測試

```bash
cd trader && python3 -m pytest -q                         # trader 全套（約 4 分鐘）
python3 berkshire-screen-merged/scripts/test_screen.py     # 初篩自測（在專案根目錄）
python3 berkshire-deep-merged/scripts/test_review_due.py   # 觸發檢查自測
```

目前結果（2026-10-21，本次待推送 commit 的乾淨匯出）：**3195 passed、3 failed、12 skipped**。3 個失敗是已知的環境性失敗，不是程式錯：

- `test_fix_P23::test_lock_held_env_only_honoured_for_parent_pid`：平行執行時偶發，單跑會過。
- `test_fix_P35::test_clean_hark_estimates_tool`：需要 B 機專用工具，本 repo 不含。
- `test_fix_RM1_5::test_state_2026_10_frozen_file_has_ttm_financial_median`：需要凍結的 state 檔（state 不進版本控制）。

## 安全注意

- **只做紙上交易**。任何 BUY 候選在改用真錢前都要人工複核；上線順序是先跑前瞻紙上交易，再依檢討決定（`berkshire-deep-merged/references/procedure/13_forward_validation.md`）。
- **不得提交**：`trader/.env`（金鑰）、`state/`（含 `trader.db`、SEC 快取、分析結果、價格檔）、`test_runs/`、匯出與修補產物。`.gitignore` 已擋，提交前仍要確認。
- 金鑰只放 `.env` 或環境變數；程式與設定檔不得寫死金鑰或 email。
- 儀表板只聽本機，同意等寫入 API 需要 `state/.dashboard_token`（權限 600）並檢查 Host／Origin。
- 價格隔離：DOSSIER、TRIAGE、THESIS、FULL_A 的沙箱不得出現價格檔或價格字句（含可推得價格的數字，例如回購均價、增資發行價、轉換價）；FULL_B 在閘門 3 作答之後才開價格檔。

## 能不能賺錢（照實說）

- 有證據的：初篩**降低踩雷**（3 年大跌機率下降、被刪者壞結果約 3 倍）。
- 沒有證據的：排名提高報酬（逐期模擬不顯著）、整條流程的報酬（深度分析靠判斷，無法完整回測）。
- 合併版自訂的參數（自動買進上限的 k、困境特殊情境、兩週觸發檢查門檻等）都標為推論、未經回測，見 `berkshire-deep-merged/references/inferred_thresholds.md`。

## 已知限制

- 證據來自美國大型股；其他市場、小型股未驗證。
- S&P 500 名單是現有成分股（存活者偏差），只影響回測，不影響往後的紙上交易。
- `fetch_prices.py`（skill 單獨使用時）用 Yahoo 非官方介面；框架的撮合、限價、成交量一律用 Alpaca。
- 多類股公司可能停報封面合計股數，改用申報 XBRL 推算，市值可能略有誤差（報告會標示）。
- 深度分析需要年報文字、附註、委託書；只有數字時正確結論是 `MISSING_DATA`。
- 修復歷程與設計決策見 `handoff.md`；最近一輪見「2026-10-21 第三輪（A3）修復總覽」。
