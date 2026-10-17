# M6 介面契約

## 6a 強模型同意閘門（2026-10-16 完成）

**設定**：`approval.require_for_tiers`（預設 `[strong]`；`[]`＝不經閘門）。目前 strong 工作＝FULL_A、RED_TEAM。

**資料表 `approvals`**（schema 版本 2）：`approval_id, ticker, run_id, asof, job_id, job_type, tier, holding, reason, status, created_at, decided_at, decided_via, note`，`UNIQUE(run_id, ticker)`。

| status | 意義 |
|---|---|
| pending | 待同意 |
| deferred | 延後：留在佇列，不計入一鍵全同意；可逐檔同意或拒絕 |
| approved | 已同意，等 `run-approved` |
| done | 該檔本 run 已無未完成工作 |
| rejected | 拒絕：等待工作改 skipped，記 NO_ACTION/MISSING_DATA（`write_stop_result`；持股不清空登錄表），下次觸發再排 |
| superseded | 同一檔有新觸發（新 run），舊的 pending／deferred 作廢，舊等待工作改 skipped |

**引擎**（`jobs/engine.py` `_launch`）：路由出的等級需要同意且本 run 該檔未同意 → 工作狀態 `awaiting_approval`、不呼叫模型。同意單位是一檔：同 run 該檔之後才產生的 strong 工作（RED_TEAM）直接放行。
- run 結束：有等待工作 → run 狀態 `awaiting_approval`（預算暫停優先）；計數多 `awaiting_approval` 鍵；依賴等待工作的待辦不當循環刪除；已同意且無未完成工作 → done。

**模組 `jobs/approvals.py`**：`required`、`gate`、`list_items`、`summary`（n_pending、n_deferred、n_approved_waiting、est_input_tokens、est_output_tokens、est_minutes、per_ticker）、`label`、`approve(ids|None＝全部 pending)`、`defer`、`reject`、`release`、`close_finished`、`add_block_reason`。
- 估算只算 FULL_A：過去成功 strong FULL_A 工作的平均（輸入、輸出、耗時）；無紀錄用預設 0.9M／30k／330 秒。

**執行**：`pipelines/approved.py run_approved` — `release`（approved 的等待工作改 pending）→ 逐 run `Engine.run`；檔案鎖 `state/.run_approved.lock`，同時只一個。

**暫停加碼**：`sizing.add_blocked` 先查 `add_block_reason`（pending／deferred／approved 未完成 → 擋加碼批次與 ADD）；賣出不受影響。

**每日流程**：
- 觸發檢查 run 狀態 `awaiting_approval` 視同成功（不擋其他檔的買單）。
- `load_decisions(..., late_after=上次 daily_close 完成時間)`：另取決策日 ≤ 前一交易日、但之後才寫入的決策（等待同意數日後才完成的分析，決策日仍是資料日）。

**CLI**：`trader approvals list [--all]`、`approve IDS… | --all [--run]`、`defer IDS…`、`reject IDS… [--note]`；`trader run-approved [--run-id]`。analyze／review 有等待工作時印 `[WAIT]` 提示。

## 6b API 伺服器與指標（2026-10-16 完成）

**啟動**：`trader dashboard [--port]`（uvicorn，只綁 127.0.0.1；config `dashboard.host` 非本機 → 拒絕）。開機自動啟動（launchd）在 6d。

**指標** `trader/report/metrics.py`（算法只在這裡，前端不另算）：
- 報酬率＝TWR：首日 淨值÷首日前累計入金，之後每日 (淨值−當日入金)÷前日淨值。外部資金只認 `deposit`；`adjust`（調帳、公司行動現金）算帳戶內損益。
- SPY 總報酬：I_t＝I_{t−1}×(close_t＋div_t)÷close_{t−1}；`div_t` 取 `nav_daily.data.benchmark_dividend`（每日流程由 Alpaca 公司行動寫入，除息日落在 (前一交易日, 今天]）；收盤缺值沿用前值。超額＝帳戶−SPY（百分點）。
- 收益：總＝淨值−累計入金；未實現＝Σ股數×(價−均價)；股利＝dividend＋dividend_receivable；已實現及其他＝總−未實現−股利（含賣出損益、手續費、調帳）。
- 價格：`nav_daily.data.prices`（`record_nav` 新增；舊列沒有 → 以成本估、標 `price_missing`）。
- 走勢：1M（30 曆日）、3M（91）、YTD、ALL，視窗首日＝100。

**端點**（全部檢查 Host ∈ 127.0.0.1／localhost:埠，否則 403）：

| 方法 | 路徑 | 內容 |
|---|---|---|
| GET | `/api/summary` | 小儀表板全部數據（含 approvals 摘要與按鈕標籤，無待同意時 label＝null） |
| GET | `/api/nav?range=` | 走勢點 `{date, account, spy}` |
| GET | `/api/holdings` | 持股＋登錄表上限、賣出檢視線、荒謬級線、建倉計畫、論點一句話、下次檢查、暫停／停止加碼 |
| GET | `/api/trades?ticker&start&end` | orders、fills、cash（依委託編號或 `CA:代號:` 對應）、corporate_actions |
| GET | `/api/analyses?ticker&limit` ／ `/api/analyses/{id}` | 決策日誌；單筆含 log、訊號卡、report.md（無則 report_A.md） |
| GET | `/api/usage` | llm_calls 每月×等級 |
| GET | `/api/anomalies` | 未結異常、待人工公司行動 |
| GET | `/api/approvals` | items、summary、running（run-approved 鎖是否被持有） |
| POST | `/api/approvals/approve` | `{"ids":[…]}` 或 `{"all":true}`（不含延後）；成功且沒有執行中 → 背景啟動 `trader run-approved`（記錄 `state/logs/run_approved.log`） |
| POST | `/api/approvals/defer`、`/reject` | `{"ids":[…]}`；reject 可附 `note` |
| GET | `/` | 網頁（index.html 內嵌權杖於 `<meta name="dashboard-token">`）；`/static/*` |

**POST 防護**：`X-Dashboard-Token`（`state/.dashboard_token`，600，`hmac.compare_digest`）＋`Content-Type: application/json`（跨站須預檢）＋有 Origin 時須為本機網頁。`via`：`web`（預設）或 `desktop`。狀態不允許 → 409。

## 6c 網頁（2026-10-16 完成）

`trader/web/index.html`＋`static/app.css`、`static/app.js`：原生 HTML/CSS/JS，無建置工具、無外部 CDN；圖表以 SVG 自繪，字型用系統字型。
- 設計（2026-10-16 改版）：看盤軟體版面＋Apple／Anthropic 風格。介面黑白灰，顏色只給漲跌；唯一強調色陶土橘只用於同意等主要動作；數字 SF Pro 等寬數字、標題 New York 襯線；細分隔線取代陰影。總覽＝指標列（淨值／帳戶報酬／SPY／超額／現金比）＋全寬走勢圖（Y 軸在右、線尾標最新值、十字線＋日期標籤、起點 100 虛線；帳戶線依報酬正負用漲／跌色）＋持股報價表＋收益分解／灰階配置條／待辦。
- 頂列切換（只存瀏覽器 localStorage）：主題 自動／淺色／深色（`html[data-theme]`）、漲跌色 綠漲紅跌（預設）／紅漲綠跌（`html[data-colors="tw"]`）。
- 頁面（hash 路由）：`#overview` 總覽、`#holdings` 持股、`#trades` 交易紀錄（委託／成交／現金流水／公司行動，可依代號與日期篩選）、`#analyses` 分析紀錄（決策日誌、模型用量；`#analyses/{決策編號}` 開報告抽屜：一句話結論、可摺疊訊號卡、report.md）、`#approvals` 待同意（全部同意／同意所選／延後／拒絕附理由，皆先確認）、`#anomalies` 異常。
- 安全：所有資料經 `esc()` 跳脫；報告 markdown 先跳脫再轉換；`Content-Security-Policy`：script 只允許本機檔、不允許行內 script；`X-Frame-Options: DENY`。
- 個股走勢彈出視窗（2026-10-16 新增）：任何頁面點代號（`.tk[data-q]`）→ 置中彈出視窗（淡入＋縮放，transform／opacity 動畫），區間 1 天／1 週／3 個月／6 個月／10 年（記住上次選擇）；持股另顯示股數、成本、市值、報酬。API `GET /api/charts?tickers=A,B&ranges=1D,1W,3M,6M,10Y`（`trader/dashboard/charts.py`）：1D＝最近交易日正常盤 5 分鐘線、基準昨收；1W＝近 5 交易日 30 分鐘線；3M／6M 日線；10Y 週線；價格分拆還原；資料源 SIP（免費方案延遲約 15 分鐘；IEX 只有 2020 年後歷史），失敗退回 IEX；同區間多檔合併一次請求；伺服器不快取。預載：網頁開啟／重新整理時預載持股＋待同意標的（存在該頁記憶體，關掉網頁即消失，下次開啟重新抓），每頁渲染後閒置時預載頁面上所有代號，滑過代號也會預載 → 點開與切換不等網路。未設 Alpaca 金鑰回 503。
- 主題預設跟隨系統（淺色米白 #faf9f5、深色近純黑）；窄螢幕（≤760px）頂列換行。

## 6d 桌面小儀表板與開機自動啟動（2026-10-16 完成）

- `trader/desktop/BerkshireWidget/main.swift`＋`build.sh`（只需 Command Line Tools）→ `build/BerkshireWidget.app`（LSUIElement，無 Dock 圖示）。
- 340×340 無邊框純黑圓角視窗（不用毛玻璃），層級在桌面圖示之上、一般視窗之下，所有桌面空間都顯示；拖曳以螢幕絕對座標（`NSEvent.mouseLocation`）計算，避免視窗移動造成座標回饋抽動；移動超過 3pt 才算拖曳，否則算點擊開網頁；位置記在 UserDefaults。
- 版面：帳戶報酬大數字、SPY／超額、3M 走勢線（含 100 基準虛線）、收益／現金／持股數、前三持股（權重條＋報酬）、同意按鈕（陶土橘，顯示檔數與預估 token／時間）。
- 每 60 秒讀 `/api/summary`、`/api/nav?range=3M`；失敗時每 5 秒重試。同意按鈕只在 n_pending > 0 時出現，按下先 NSAlert 確認，再 POST `{"all":true,"via":"desktop"}`（帶權杖、不送 Origin）。
- `trader desktop build|install|uninstall|status`：launchd 使用者代理 `local.berkshire.dashboard`（伺服器，KeepAlive；PATH 含 `claude` 所在目錄、SSL_CERT_FILE＝certifi）與 `local.berkshire.widget`；plist 不含金鑰；日誌在 `state/logs/`。

## 6e 匯出（2026-10-16 完成）

`trader/report/export.py`：資料集 holdings、orders、fills、cash、corporate_actions、analyses、nav、usage、approvals、anomalies（皆取自 metrics，同源）。
- CSV（UTF-8 BOM，Excel 不亂碼；巢狀欄位存 JSON 字串）、JSON（合一檔）、XLSX（每資料集一張工作表）。
- CLI：`trader export [--what all|a,b] [--format csv|json|xlsx] [--out DIR]`（預設 `state/exports`）；API：`/api/export/{資料集}.csv`、`/api/export/all.xlsx`、`/api/export/all.json`。

## 前瞻檢討 validate-report（plan.md 列在 M6；2026-10-16 完成）

`trader/report/validation.py`，格式依 skill procedure/13 §3 事先寫定：
1. 流程合規：日誌筆數 vs 實際決策（run×代號，深度與觸發工作）、完整分析決策的欄位缺漏率（decision_log_schema 欄位）、BUY／ADD 閘門非 true 或缺問卷作答檔、`fy_last_filed ≥ 決策日`、SELL_ALL／TRIM 內容含停損字樣、skill 雜湊種類數。
2. 對大盤：帳戶 vs SPY（儀表板算法）；每筆買進自成交日起 vs SPY（Alpaca `adjustment=all`）。期間內有分拆／併購換股／現金併購／其他 → 標「不可比」不算差（Alpaca 不還原換得的新股與現金）。
3. 對初篩母體：期初（≤ 期初的最近一次）初選 `screen.csv` 依 lane 分核心道、特殊情境、統計排除、刪除，排除已買進者，等權平均與中位；初選日不早於期末 → 註明無可比期間。
4. 閘門診斷：停在各關數、閘門 3 不過題目分布、閘門 3 通過者同期報酬（只記錄）。
5. 誠實標示（固定文字）。
- CLI：`trader validate-report [--start] [--end] [--no-prices] [--out]` → `state/reports/validation_{start}_{end}.md／.json`。
