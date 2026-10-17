# 波克夏式長期選股自動交易系統：規劃（plan.md）

> 狀態：M0、M1、M2 完成；M3 程式完成待實跑（423 測試通過）。進度與交接見 `handoff.md`。
> 規則來源：同目錄的 `berkshire-screen-merged/`、`berkshire-deep-merged/`、`routing/`。交易系統**不改寫規則**，只負責排程、資料、調用模型、驗證、記帳、報表。規則與程式衝突時以 skill 為準。

## 0. 已確認的決定

| 項目 | 決定 |
|---|---|
| 執行環境 | 本機 Mac 與雲端 Linux 都要支援；程式不綁平台，排程可用內建常駐 scheduler、launchd 或 cron |
| Claude Code 模式 | `claude -p` headless 子程序 |
| 自訂 API 模式 | OpenAI 相容 `chat/completions`；強／中／弱三級各自設定 base_url、api_key、model，可混搭（例：強＝Claude Code、中＝供應商 A、弱＝供應商 B） |
| 市場資料 | Alpaca：日線（未調整）、公司行動（拆股、合股、股利）、交易日曆、新聞 |
| 交易 | **本機紙上帳本**：下單、成交、拆合股、股利、現金都在本機計算與記錄；Alpaca 只提供資料；公司行動由弱模型解讀、程式覆核後入帳 |
| 股票池 | S&P 500 |
| 新聞 | Alpaca News API（給 TRIAGE） |
| 利率／P/E | FRED `DGS30`；大型股 P/E 中位以 S&P 500 母體價格 ÷ 正常化 EPS 自算，每月凍結 |
| 確認與通知 | 全自動；資料可即時匯出，透過遠端桌面查看交易紀錄與收益（本機唯讀網頁儀表板＋匯出 CSV／JSON／XLSX） |

## 1. 系統總覽

```
                 ┌──────────── scheduler（內建常駐 / launchd / cron）────────────┐
                 │ daily_close   biweekly_review   quarterly_screen   monthly_freeze │
                 └───────┬─────────────┬──────────────────┬───────────────┬─────────┘
                         ▼             ▼                  ▼               ▼
data/（純程式）  Alpaca 價格與公司行動 · SEC EDGAR · FRED · Alpaca News · S&P 500 名單
                         │
pipelines/（純程式）  screen（呼叫 skill 腳本）· review_due（呼叫 skill 腳本）· dossier 骨架
                         │ 產生訊號
                         ▼
jobs/（工作引擎）  依 routing/model_routing.json 建工作 DAG → 沙箱 → 調用模型 → 驗證 → 後置腳本
                         │                          ▲
                         ▼                          │
llm/  ClaudeCodeRunner（claude -p） · OpenAICompatRunner（chat/completions）· FakeRunner（測試）
                         │ 結論（訊號卡、登錄表列、日誌）
                         ▼
portfolio/（純程式）  三閘門核對 · 部位計算 · 加碼許可 · 賣出規則 · 組合限制 → 委託單
                         ▼
ledger/（純程式）  紙上撮合 · 拆合股 · 股利 · 現金 · 每日淨值
                         ▼
report/  SQLite → 唯讀儀表板（localhost）· 匯出 · 前瞻驗證報告（procedure/13）
```

原則：
1. **模型只在工作引擎裡被調用**，其他全部是確定性程式。
2. **規則以程式保證**：價格隔離、數字覆核（`cap_calc.py`）、閘門 3（`gate3_check.py`）、組合限制都由程式執行，不靠模型自律。
3. **可重放**：每次模型調用的輸入、輸出、模型名稱、token 數、耗時都存檔；換模型可以重跑比較。
4. **冪等**：每個 pipeline 以 `(類型, 決策日)` 為 run_id，中斷後重跑只補未完成的工作。

## 2. 目錄結構

```
berkshire/
  berkshire-screen-merged/  berkshire-deep-merged/  routing/   ← 規則（唯讀）
  plan.md
  trader/
    pyproject.toml
    config.example.yaml        .env.example
    trader/
      __init__.py
      cli.py                   # `trader <command>` 入口
      config.py                # 讀 YAML＋環境變數、驗證
      paths.py                 # 套件根目錄、state、work、cache 路徑
      db.py                    # SQLite 連線、migration
      models.py                # 資料類別（dataclass）
      calendar.py              # 交易日（Alpaca calendar，離線時退回 NYSE 規則）
      data/
        alpaca.py              # 日線、公司行動、新聞、日曆
        sec.py                 # 包裝 skill 的 fetch_edgar；下載 10-K/10-Q/DEF 14A/8-K 文字
        fred.py                # DGS30、20 年中位
        universe.py            # S&P 500 成分（快取＋手動覆寫檔）
        pe_median.py           # 大型股 P/E 中位（月凍結）
        prices_adapter.py      # Alpaca → skill 需要的 prices.csv（取代 Yahoo）
      pipelines/
        screen.py              # 季初選
        review.py              # 兩週／每日觸發檢查
        bootstrap.py           # 首次：初選＋15 檔完整分析
        daily.py               # 收盤後：公司行動、撮合、淨值
      jobs/
        engine.py              # DAG、併發、重試、預算
        sandbox.py             # 每個工作的隔離輸入目錄（價格隔離）
        context.py             # API 模式的上下文打包（SKILL 段落＋references＋dossier）
        validate.py            # 依 routing/output_schemas.json 驗證
        handlers.py            # 各工作的前置／後置（build_dossier、cap_calc、gate3_check）
      llm/
        base.py                # Runner 介面、LLMResult
        claude_code.py         # claude -p 子程序
        openai_compat.py       # chat/completions
        fake.py                # 測試用重放
        router.py              # job → tier → runner
      portfolio/
        sizing.py  rules.py  orders.py
      ledger/
        broker.py              # PaperBroker（Broker 介面，預留真券商）
        corporate_actions.py   # 偵測公司行動、套用 CORP_ACTION 結果、覆核（比例、價格比、市值守恆）
        nav.py
      report/
        export.py  dashboard.py  validation.py
      scheduler.py             # 內建常駐排程；另可產生 launchd plist／crontab
    tests/
  state/                       # 執行期資料（不進版控）
    trader.db
    registry.json              # 由 DB 匯出，給 review_due.py 讀
    theses/  work/{run_id}/{ticker}/  llm_calls/  exports/  cache/
```

語言：Python 3.11+。相依盡量少：`pyyaml`、`httpx`、`pandas`、`jsonschema`、`fastapi`＋`uvicorn`（儀表板）、`openpyxl`（XLSX 匯出）。

## 3. 設定（config.yaml＋.env）

```yaml
account:
  initial_cash: 100000          # 美元
  slippage_bps: 10
  commission_per_order: 0
  benchmark: SPY
universe: sp500
schedule:
  timezone: America/New_York
  daily_close: "17:30"          # 收盤後：公司行動、撮合、淨值、持股觸發檢查
  review_holdings: daily        # 持股每天檢查（純程式）
  review_watch: biweekly        # 觀察名單每兩週
  quarterly_screen: ["02-20", "05-20", "08-20", "11-20"]
  monthly_freeze: "01"          # 每月 1 日凍結 P/E 中位
models:
  strong: {backend: claude_code, model: opus}
  medium: {backend: openai_compat, base_url: https://api.provider-a.com/v1, api_key_env: PROVIDER_A_KEY, model: xxx}
  weak:   {backend: openai_compat, base_url: https://api.provider-b.com/v1, api_key_env: PROVIDER_B_KEY, model: yyy}
  # 每級可設 max_output_tokens、temperature、timeout_s、concurrency、json_mode
job_tier_overrides: {}          # 例：{FULL_B: strong}；預設讀 routing/model_routing.json
budget:
  max_llm_calls_per_run: 200
  max_cost_usd_per_run: 100        # 一輪模型費用總和上限（美元）
  max_input_tokens_per_run: 0      # 0＝不限（token 含快取讀取，會被多回合放大）
data:
  sec_user_agent_env: SEC_USER_AGENT
  alpaca_key_env: ALPACA_API_KEY
  alpaca_secret_env: ALPACA_SECRET_KEY
  alpaca_feed: iex              # 免費方案 iex；付費 sip
  fred_key_env: FRED_API_KEY
dashboard: {host: 127.0.0.1, port: 8787}
```

秘密只放 `.env`（或環境變數），設定檔只存環境變數名稱。

## 4. 模型層

### 4.1 介面

```python
class Runner(Protocol):
    def run(self, job: JobSpec) -> LLMResult  # LLMResult: text, parsed_json, input_tokens, output_tokens, model, latency, raw_path
```

`router.py`：`job_type`（＋THESIS 依持股狀態）→ tier（routing 表，可被 `job_tier_overrides` 覆寫）→ 該 tier 的 runner。

### 4.2 兩種後端的差異（關鍵）

| | ClaudeCodeRunner（`claude -p`） | OpenAICompatRunner |
|---|---|---|
| 讀檔 | 模型在沙箱目錄內自己讀 SKILL、references、dossier | 不能讀檔：`context.py` 依工作把需要的內容打包進提示 |
| 寫檔 | 模型直接寫 `report_A.md`、`gate3_answers.json` 等 | 模型回傳單一 JSON，檔案內容放在 `files: {檔名: 內容}`，框架寫檔 |
| 呼叫 | `claude -p --model M --output-format json --allowedTools Read,Write,Glob,Grep --permission-mode acceptEdits`，cwd＝沙箱 | `POST {base_url}/chat/completions`，可選 `response_format: json_object` |
| 隔離 | 沙箱只放允許的檔案；不給 Bash、WebFetch | 打包內容即全部可見內容 |
| 計量 | 解析 `--output-format json` 的 usage | 讀 `usage` 欄位 |

`context.py` 的打包規則：工作類型 → 固定清單（`_COMMON.md`＋工作模板＋SKILL.md 指定節＋該步 references 段落＋輸入檔）；超過 tier 上下文上限（預設 100k）時，先刪 knowledge 背景、再截 references，**從不截 dossier 的財報表與問卷**；仍超過 → 工作失敗並記錄。

### 4.3 驗證與失敗

- 輸出先抽 JSON（容忍 ```json 包裹），用 `routing/output_schemas.json` 驗證列舉值與必填欄。
- 不合格：附錯誤訊息重試 1 次；仍不合格 → 依 routing `on_invalid_output` 記 `NO_ACTION/MISSING_DATA`。
- 子程序或 HTTP 失敗：指數退避重試 3 次；限流（429）依 `Retry-After`。

## 5. 工作引擎

- 工作 DAG（每檔）：`DOSSIER → FULL_A → [gate3_check] → FULL_B → [cap_calc 覆核] → RED_TEAM?`；`TRIAGE → THESIS? → FULL?`；`PRICE → FULL?`。升級在執行期動態加節點。
- 併發：各 tier 獨立上限（設定），各檔之間平行；組合層必須等全部單檔工作結束。
- 狀態存 `jobs` 表：pending／running／done／failed／skipped，含 attempts、tokens、成本估計、輸出路徑。重跑同一 run_id 只補未完成者。
- **價格隔離（sandbox.py）**：DOSSIER、TRIAGE、THESIS、FULL_A 的沙箱不放 `*_price.json`；交接檔每檔的 `price_context` 刪掉。FULL_B 建立時，先確認 `gate3_answers.json` 已存在且 mtime 早於現在，再複製價格檔並寫入 `price_file_opened_at`。
- 後置覆核：FULL_B 後執行 `cap_calc.py`，`auto_buy_cap`／IV 與訊號卡不一致 → 以腳本為準並退回重寫一次；`gate3_check.py` 不過 → 訊號最多 WATCH。
- 預算：超過 `budget` → 停止建立新工作，未完成者留待下次。

## 6. 資料層

| 來源 | 用途 | 備註 |
|---|---|---|
| Alpaca bars（`adjustment=raw`） | 收盤價、36 個月波動、3 年回撤、52 週回撤、撮合 | 拆股基準由公司行動自行調整；`prices_adapter.py` 產出 skill 需要的 `prices.csv` 格式（含 `splits` 欄） |
| Alpaca corporate actions | 拆股、合股、現金股利、代號變更 | 帳本與市值計算都用；與 SEC 封面股數交叉檢查，不一致寫異常 |
| Alpaca news | TRIAGE | 轉成 `review_due.py --news` 的格式 |
| Alpaca calendar | 交易日、提早收盤 | |
| SEC EDGAR | 財報（skill 的 `fetch_edgar.py`）、申報清單、原文 | 原文下載後轉純文字存 `cache/sec/`；遵守 10 req/s |
| FRED | DGS30、20 年中位 | 決策日前最後一個值 |
| S&P 500 名單 | 股票池 | 網路來源抓取＋快取，可用 `universe_override.txt` 手動覆寫；記錄每次名單版本 |

**時點紀律**：所有資料查詢都帶 `asof`，只取 `asof` 之前可得者；skill 腳本本身已檢查前視。

## 7. Pipelines

| pipeline | 何時 | 純程式 | 會產生的模型工作 |
|---|---|---|---|
| `bootstrap` | 手動一次 | 初選 | 15 檔 × DOSSIER、FULL_A、FULL_B、（擬買）RED_TEAM |
| `quarterly_screen` | 每季 | 初選；比對登錄表 | 新進或 12 個月內未完整分析者 × 同上 |
| `review` | 持股每天、觀察名單每兩週 | `review_due.py`（由 DB 匯出 registry.json） | TRIAGE／PRICE／THESIS／FULL（依訊號） |
| `daily_close` | 每個交易日收盤後 | 偵測公司行動、撮合待成交委託、淨值、匯出快照 | 持股或掛單標的有公司行動 → CORP_ACTION（弱） |
| `monthly_freeze` | 每月 | 凍結 P/E 中位 | 無 |

每個 pipeline 結束時：寫回 registry、decision_log、論點檔；跑組合層產生委託；寫 run 摘要。

## 8. 組合層與紙上帳本（純程式）

**組合層**（依 SKILL §5–§6、procedure/06、procedure/12）：
- 買進候選：訊號 BUY（或 PRICE 的 BUY_CANDIDATE）＋三閘門紀錄在案＋價格 < `auto_buy_cap`。
- 目標權重 T：等級上限（A 15％、B 10％）、尾部與單事件限制、使用者上限；T < 3％ 不建倉。
- 首批 0.5T；剩餘批次 1–15 個交易日內，每批先過加碼許可；連 10 日高於上限 → 取消。
- 限制：持股 5–10 檔（最多 12）、現金 ≥ 5％、每日新部位 ≤ 4、單檔 >15％ 不加碼、無槓桿、部位 ≤ 平均日成交額 1％。
- 名額或現金不足：依 G8 調整後預期報酬排序。
- 賣出：論點證偽 → 全清（下一交易日起）；荒謬級線 → 至少一半；≥ `sell_review_above` 且有更好用途 → 減碼；權重超限 → 減至上限；建倉未滿一年不因價格因素賣出。**沒有價格停損。**

**紙上帳本**：
- 委託：限價單（買：限價 = min(`auto_buy_cap`, 參考價 ×(1+滑價))）；在決策日的**下一個交易日**撮合，不用當日已知收盤成交。
- 撮合：買單若當日最低價 ≤ 限價 → 成交價 = min(開盤價 ×(1+滑價), 限價)（限價單不得劣於限價，確保買價不高於上限）；賣單對稱。未成交保留至取消條件。
- **公司行動交由弱模型解讀（CORP_ACTION 工作，預設 haiku 等級）**：
  1. 程式偵測：Alpaca 公司行動、SEC 8-K（項目 3.03、5.03、8.01）、價格跳空（收盤對前收 ≥ ±40% 且無對應事件）→ 為持股、掛單、觀察名單建立工作。
  2. 弱模型輸入：事件原文（Alpaca 欄位、8-K／新聞稿文字）、目前持股數與成本、事件前後收盤價；輸出結構化調整：類型（拆股／合股／現金股利／股票股利／分拆／併購換股／代號變更）、比例、生效日、零股處理方式、現金對價、新代號、是否需要人工。
  3. 程式覆核後才入帳：比例與 Alpaca 數字一致；比例與事件前後價格比相符（誤差 ≤ 15％）；調整前後持股市值守恆（誤差 ≤ 2％，現金對價計入）；股數為整數、零股以收盤價現金了結。任一不符 → 不入帳、寫 `anomalies`、暫停該檔交易，等人工 `trader ledger adjust`。
  4. 入帳時同步調整：持股股數與每股成本、未成交委託的股數與限價、登錄表的每股數字（`auto_buy_cap`、`sell_review_above`、`absurd_price_line`、`cost`、`review_price`）。
  5. 每筆公司行動以 `(代號, 類型, 生效日)` 防重複入帳；模型輸入輸出照常存檔。
- 現金股利：同樣經 CORP_ACTION 解讀；除息日記應收、發放日入帳；不自動再投入。
- 分拆、併購換股：模型解讀後一律標「需要人工」，程式不入帳（含現金對價，避免與未調整股數重複計價）並暫停該檔，由人工 `trader ledger adjust` 一次處理。
- 每日淨值：持股 × 收盤（同一拆股基準）＋現金；對照 SPY 與等權 S&P 500（可選）。

## 9. 資料庫（SQLite，單一事實來源）

| 表 | 內容 |
|---|---|
| `registry` | 每檔一列（欄位同 `assets/registry_schema.json`，另加 `first_buy_date`） |
| `decisions` | 決策日誌（`decision_log_schema.json`），decision_id＝`代號-YYYYMMDD-序號` |
| `signals` | 訊號卡全文（JSON） |
| `runs` | pipeline 執行紀錄 |
| `jobs` | 工作狀態、tier、模型、token、耗時、輸出路徑 |
| `llm_calls` | 每次調用：提示雜湊、輸入／輸出檔路徑、usage |
| `orders` `fills` `positions` `cash_ledger` | 帳本 |
| `corporate_actions` | 已入帳的公司行動（防重複） |
| `nav_daily` | 每日淨值、基準、現金、持股數 |
| `anomalies` | 資料異常、待人工處理事項 |
| `universe_versions` | 每次股票池名單 |

`registry.json`、`exports/` 都由 DB 產生，不手改。

## 10. 報表與遠端查看

- `trader dashboard`：FastAPI 唯讀網頁，預設 `127.0.0.1:8787`（遠端桌面連回本機即可看；要從其他裝置看再改 host 並設 token）。頁面：淨值與基準曲線、持股（成本、市值、權重、上限、賣出檢視線、論點一句話）、委託與成交、決策日誌、觀察名單（距上限）、工作與模型用量、異常。
- `trader export --format csv|json|xlsx [--since]`：全部表或指定表；`daily_close` 後自動更新 `state/exports/latest/`。
- `trader validate-report`：依 procedure/13 產出紙上交易檢討（對大盤、對初選母體、決策結果分組）。

## 11. CLI

```
trader init                         # 建 state/、DB、檢查設定與金鑰、檢查 claude CLI
trader doctor                       # 連線測試：Alpaca、SEC、FRED、各 tier 模型（一次極短呼叫）
trader bootstrap --asof DATE [--tickers X,Y] [--dry-run]
trader screen --asof DATE
trader review [--scope holdings|watch|all] --asof DATE
trader daily --date DATE
trader run-job JOB_TYPE TICKER --asof DATE     # 單一工作，除錯用
trader scheduler [--install launchd|cron|print]
trader dashboard
trader export ...
trader ledger show|adjust ...
```

`--dry-run`：走完整流程但模型換成 FakeRunner、不寫帳本。

## 12. 測試

- 單元：設定載入、router 對應、JSON 抽取與驗證、沙箱價格隔離（斷言沙箱內無價格檔與價格欄）、context 打包上限、撮合與拆合股、組合限制。
- 整合：FakeRunner 重放固定輸出，跑 bootstrap → review → daily 全流程。
- skill 既有測試：`test_screen.py`、`test_review_due.py` 納入 `pytest`。

## 13. 里程碑

| 里程碑 | 內容 | 完成判準 |
|---|---|---|
| **M0 基礎架構** | 專案骨架、設定、DB 與 migration、CLI 骨架、LLM 三種 runner＋router、沙箱、驗證、工作引擎骨架、FakeRunner、測試 | `trader init`、`trader doctor` 可跑；FakeRunner 跑通一個假工作 DAG；pytest 全過 |
| M1 資料層 | Alpaca、SEC 原文、FRED、S&P 500、P/E 中位、prices_adapter | 對 5 檔產生 features.csv、prices.csv，與現有腳本輸出一致 |
| M2 初選 | screen pipeline | S&P 500 跑出 15 檔交接檔 |
| M3 深度分析 | DOSSIER／FULL_A／FULL_B／RED_TEAM handlers、context 打包 | 1 檔分別用 Claude Code 與 API 後端跑通 |
| M4 觸發檢查 | review pipeline、TRIAGE／PRICE／THESIS | 對假登錄表與真資料跑通 |
| M5 組合與帳本 | portfolio、PaperBroker、公司行動、淨值 | 合成情境測試（含拆股、合股、股利） |
| M6 報表 | dashboard、export、validation | 遠端可看、可匯出 |
| M7 上線 | bootstrap 15 檔、排程安裝、持續紙上交易（2026-10-16 使用者改為沒有期限，直到手動停止） | 排程常駐；每月自動出檢討報告 |

## 14. 風險與未決

- **S&P 500 名單來源**：沒有官方免費 API；以網路來源＋快取＋手動覆寫處理，名單變動以季為單位影響不大。
- **Alpaca 免費資料**：IEX feed 的成交量只有部分市場，「日成交額 1％」限制會偏保守；價格本身足夠。
- **Claude Code 額度**：`claude -p` 吃訂閱額度，bootstrap 15 檔的強模型工作建議分批（工作引擎支援暫停與續跑）。
- **API 模式的上下文**：模型不能自己翻 references，打包品質直接影響分析品質；M3 要比較兩種後端的結論差異。
- **公司行動**：拆股、合股、股利由弱模型解讀＋程式覆核後自動入帳；分拆、換股併購由模型解讀但需人工確認。弱模型讀錯比例時由「價格比」與「市值守恆」兩道覆核擋下。
- **紙上撮合樂觀偏差**：用日線最低價判斷限價單成交，實際可能未成交；檢討時列為已知偏差。
