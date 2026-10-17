# 交接紀錄（handoff.md）

> 最後更新：2026-10-17 深夜（M4 部分處理、資料包補強、換機與封存程序、建立 GitHub 公開 repo `claude-berkshire`）。新對話先讀本檔 §0，再依需要讀 `plan.md`（整體規劃）、`plan_dashboard.md`（M6 計畫）、`trader/CONTRACTS*.md`（各里程碑介面）、`換機匯出與封存程序.md`。

## 0. 新 session 從這裡開始（2026-10-16 交接）

**最新現況（2026-10-17 深夜，優先於下方較舊的敘述）**
- 測試：`cd trader && python3 -m pytest -q` 744 個全過；初篩自測通過。
- **下一步由使用者執行**：在另一台電腦用空的 state 重跑全部分析（`bootstrap --asof 2026-10-15`，或 `--tickers` 指定同一批 15 檔），再依 `換機匯出與封存程序.md` 把整個 state 換回本機。
- **下方 §0 與 §4d 描述的 2026-10-15 第一批 15 檔結果，以及 `test_runs/`，在替換後會封存為樣本**（`~/berkshire-archive/SAMPLE_analyze-2026-10-15_不得採用/`，唯讀、不可變、不刪）。替換完成後，現行結果一律以 `state/trader.db` 為準，不得引用這批舊結論。
- 版本控制：專案已建 git，推到 GitHub 公開 repo `ihsieh31/claude-berkshire`（公開：任何人可見，金鑰與 state 絕不能進 git）。`state/`、`trader/.env`、`test_runs/`、審查前備份、編譯產物不進 git（`.gitignore`）。
- 這一天的改動摘要見本檔末「2026-10-17 M4 處理與資料包補強」。


**現況一句話**：M0–M7 程式完成，708 測試全過；初始 15 檔分析**已完成**（零 BUY，見下）；排程程式完成但**尚未安裝**（使用者 2026-10-17 指示先不裝）；M3／M4／M5 都用真模型實測過（結果都在 `test_runs/`，從未寫入正式 state）；正式 state 目前沒有任何持股或決策。
- **初始 bootstrap 歷程（2026-10-17，已完成，見上）**：`trader bootstrap --asof 2026-10-15`；初選 15 檔（HIG HPQ BBY LOW WTW MAS FDX SPG PHM GL OTIS BALL ALL MO FTV），**DOSSIER 15 檔全部完成**（haiku，共 $8.64），FULL_A 已全部同意並跑完。run `analyze-2026-10-15`；資料 `state/work/analyze-2026-10-15/`；輸出 `state/reports/bootstrap-2026-10-15.json`、`analyze-2026-10-15-resume*.json`。途中修正：預算改 `budget.max_cost_usd_per_run: 100`（token 上限 0＝不限，因含快取讀取被多回合放大）；DOSSIER 輸出格式 sections_appended 列舉補齊提示要求的 13 節、提示加「A | B 擇一、陣列逐格填」讀法、validate 拆開多包一層工作名稱。PHM、OTIS 第一次不合格時被記 NO_ACTION/MISSING_DATA，已重設工作重跑通過；這兩筆過時的決策與登錄表紀錄已依使用者指示刪除（2026-10-17，刪前備份 `state/trader.db.bak-20261017-before-del-OTIS-PHM`）。**effort 統一 medium**（使用者指示 2026-10-17）：`models.<tier>.effort` 預設 medium，claude_code 每次呼叫明確帶 `--effort`，不受環境變數或個人設定影響；15 檔 DOSSIER 當時未指定（推測繼承終端環境 CLAUDE_EFFORT=medium，未驗證）。
- **初始 15 檔分析完成（2026-10-17 晚）**：run `analyze-2026-10-15`，registry／decisions／signals 各 15 筆。結果：**零 BUY**；OTIS＝WATCH（C 級，自動買進上限 62.40，分析時現價 70.39）；LOW＝TOO_HARD（G1，零售預設太難＋W-REV）；其餘 13 檔 REJECT（多數 G3：護城河三測 <2 強；SPG 為利息保障／負債比；MO 見下）。該 run 的 LLM 花費約 $27（含 MO 重做）。`trader scheduler install` 的前提檢查已可通過（`bootstrap_block` 只看 pending／running／awaiting_approval 與待同意項目，jobs 表兩筆 failed 不擋）。**下一步**：使用者決定何時 `trader scheduler install`；建議先抽查「資料缺口被當成弱」是否讓 G3 偏嚴（WTW 為「客戶留存率缺 → 不能評強」）。
- **補寫回 PHM、OTIS（2026-10-17）**：兩檔模型輸出已完成，但寫回時撞 `signals.decision_id` UNIQUE（先前只刪了 decisions／registry，沒刪 signals）。已備份 `state/trader.db.bak-20261017-before-fix-PHM-OTIS-signals`，刪兩筆殘留 signals，直接呼叫 `write_stop_result`（PHM）與 `write_full_result`（OTIS）補寫，沒有重呼叫模型。**jobs 表仍留兩筆 failed**（PHM FULL_A、OTIS FULL_B），純歷史紀錄。教訓：手動刪決策時 decisions、signals、registry 要一起刪；引擎沒有重設工作的指令。
- **`_pick_filings` 修正（2026-10-17）**：原本取「最新 10-K 或 10-K/A」，MO 的 10-K/A（2026-05-27，只有封面 7.6k 字）蓋掉原始 10-K（2026-02-25），資料包缺年報與債務到期表 → 誤判 MISSING_DATA；且 10-K 日期之後的 8-K 篩選以 10-K/A 日期為界，丟掉中間的 8-K。現在：年報主體取最新**原始** 10-K（沒有才退而取 10-K/A）；年報之後的最新 10-K/A 列候選，全文 ≥50,000 字（`MIN_AMENDMENT_CHARS`）才收；10-Q／8-K 的界線用原始 10-K 日期。測試新增 2 支（`tests/test_dossier_prep.py`）。15 檔中只有 MO 受影響（逐檔查過 `sec/_index.json`）。
- **MO 重做（2026-10-17）**：備份 `state/trader.db.bak-20261017-before-redo-MO`；刪 MO 的 jobs、decisions、signals、registry、approvals（run 內）；舊工作目錄移到 session scratchpad；`trader analyze --asof 2026-10-15 --tickers MO --run-id analyze-2026-10-15` → DOSSIER（haiku $0.82，取到完整 10-K 55.6 萬字）→ FULL_A（opus $1.34，經同意閘門）→ **G3 REJECT**（卷煙量年降 8–10%，護城河三測僅 1 強、方向變窄）。LLM 呼叫紀錄表保留舊的兩筆 MO 呼叫。
- **正式 state 的實際位置是 `berkshire/state/`（專案根目錄，`paths.py` 預設），不是 `trader/state/`**；本檔較早段落寫 `trader/state/` 處皆指它。
- **已安裝 launchd（2026-10-16）**：`local.berkshire.dashboard`（http://127.0.0.1:8787/）與 `local.berkshire.widget`（桌面小儀表板）開機自動啟動；`trader desktop status|uninstall`。改了 Python 程式後伺服器要重啟：`launchctl kickstart -k gui/$(id -u)/local.berkshire.dashboard`；改了 Swift：`trader desktop install`（會重新編譯）。

**審查報告修正（2026-10-17，`bug report/SUMMARY_核實匯總.md` 的 A1–A6、N1–N3、過度工程化改小修）**：676 測試全過，未呼叫模型。
- A1/N2 沙箱：看價格的工作（FULL_B、PRICE 等）完成或失敗後刪沙箱；引擎啟動時清超過 24 小時的非隔離沙箱（`jobs/sandbox.py`）。已手動刪掉 `/Users/Shared/berkshire-sandbox-zongen/` 下所有測試殘留，只留正式的 `analyze-2026-10-15`。
- A2（使用者同意改 skill）`cap_calc.py`：歷史成長 ≤0 → 上限夾到 IV0×等級係數；A5 顯式 null 一律視為缺。
- A3 SPY 股利鍵 `cash_per_share`；A4 `write_full_result` 登錄表三條線與等級以 cap_result／cap_input 為準；A5 PRICE 回寫 null 不蓋掉既有賣出線；A6 CORP_ACTION「無公司行動」可填 null 日期。
- N1 PRICE 對非持股判 BUY → 升級完整分析（DOSSIER→FULL_A→FULL_B→RED_TEAM，強模型照樣等同意）；90 天內完整分析已看過可買價位卻沒判 BUY → 不重跑、維持 WATCH。
- B3 排程一次只排最早未完成的 daily；B7 有未成交買單不排下一批加碼；D4 `checks.cap_input_problems` 擋單位錯誤（g、利率、price/oe 1–500）→ FULL_B 重做。
- 文件：rule_index 荒謬級 50 倍、rulebook P1 報告欄一致、procedure/12 標 deep_quant 只供研究；openai_compat 標「未驗證」。

**第二批修正（2026-10-17，同一份審查報告的 B1–B6、§4 小問題、D 系列）**：706 測試全過，未呼叫模型。
- B1 閘門 3 C1 壓等級上限（`deep._cap_grade_by_c1`）；B2 FULL_B 判 BUY 時程式核閘門 2（`checks.gate2_failures`）；B4 THESIS 不合格當「疑似」、PRICE 不合格留「閘門未過」紀錄（停止加碼）；B5 月報接受「通過」字串；B6 公司行動去重鍵含類型。
- §4：等級 C/D 權重超限留紀錄；登錄表 weight 每日隨淨值更新；`ledger adjust --set-qty 0` 取消未成交委託；結論測試 E×0.9 對覆寫 IV／保險生效；plan_buys 上限取訊號卡與登錄表較低者（拆股）；8-K 來源拆合股價格比容差 8%（有 Alpaca 比例維持 15%）；HOLD_REVIEW 路徑列舉補 Q3a–Q3d；月報欄位 skill_hashes、fy_last_filed 由框架填；`trader.jobs` 循環匯入修正；`--state-dir` 進 deny 清單；gate3 查詢加時點上界、P/E 中位凍結檔不得晚於 asof、補跑用當時股票池版本；同 CIK 多類股只留一列。
- D3 持股 PRICE 賣出檢視（或現價 ≥ IV_base）→ HOLD_REVIEW；D5 平均回購價移出資料包（放價格檔），**已手動移除 9 檔待同意的資料包中該兩列**（備份在當次 session scratchpad）；D6 金融硬上限 min(P1, P4′)（SKILL §3 已補）；D7 持股滿 10 檔時新 BUY 記 `switch_candidate` 異常待人工；D8 非金融單純公開盈餘指引不計 W4，改記 G5 誘因警訊。D1 為設計（不改）、D2 併入 N1、D4 已於第一批完成。
- 注意：9 檔待同意工作的 params 帶的 P/E 中位是 10-16 凍結（決策日 10-15，前視 1 天；P4′ 軟上限，影響極小，未重建）。

**M6a 完成（2026-10-16，契約 `trader/CONTRACTS_M6.md`）**：FULL_A／RED_TEAM 預設進 `awaiting_approval`，不再直接呼叫 opus；`trader approvals list/approve/defer/reject`、`trader run-approved`；等待期間暫停加碼；daily 把等待同意視為複查成功、並補處理延後才寫入的決策。測試設定（`tests/test_engine.py _write_config`）預設關閉閘門，閘門測試在 `tests/test_approvals.py`。未用真模型驗（閘門本身不呼叫模型）。

**M6b 完成（2026-10-16，契約 `trader/CONTRACTS_M6.md` §6b）**：`trader dashboard`；指標 `trader/report/metrics.py`；伺服器 `trader/dashboard/server.py`（Host／權杖／Origin／JSON 檢查）；網頁目前只是佔位 `trader/web/index.html`。每日淨值新增 `data.prices` 與 `data.benchmark_dividend`（SPY 總報酬用；舊淨值列沒有，持股以成本估）。已用 M5 模擬帳本副本實際啟動伺服器驗證。

**M6c–e 與 validate-report 完成（2026-10-16，使用者指示「直接把 M6 全部做完」，未停下確認設計稿）**：網頁全部頁面、Swift 小儀表板＋launchd、匯出、前瞻檢討。契約 `trader/CONTRACTS_M6.md`。驗證：展示用帳本副本（scratchpad，M5 模擬＋回填價格）實際截圖檢查各頁淺／深色與窄螢幕；`validate-report` 連真 Alpaca 跑過，發現分拆／併購標的還原權值失真 → 改標「不可比」。

**介面改版（2026-10-16 深夜，使用者要求）**：小儀表板修拖曳抽動、改純黑底；網頁改看盤軟體＋Apple／Anthropic 風格，加主題與漲跌色（綠漲紅跌／紅漲綠跌）切換。細節見 `trader/CONTRACTS_M6.md` 6c／6d。改 Swift 後要 `trader desktop install` 重編並重載。另新增個股走勢彈出視窗（點任何代號；1 天／1 週／3 個月／6 個月／10 年；開頁預載），API `/api/charts`，測試 `tests/test_charts.py`。

**M7 程式完成（2026-10-16，契約 `trader/CONTRACTS_M7.md`）**：`trader bootstrap`、`trader scheduler`；使用者指示**取消 3 個月期限**，排程沒有結束日，直到 `trader scheduler uninstall`。
- **排程目前已停止（使用者要求）**：先完成初始 15 檔分析才能排程。程式已加前提檢查：登錄表為空、或還有待同意／未完成的分析工作時，`scheduler install|run|once` 一律拒絕。期間排程沒有執行任何工作。

**下一步**：與使用者確認後手動 `trader bootstrap --asof 日期`（初選 → 15 檔 DOSSIER（haiku，直接跑）→ FULL_A 等強模型工作停在待同意）→ 使用者同意並跑完 → 檢查結果 → `trader scheduler install`。

**待使用者決定的事**：
1. RED_TEAM 真模型未驗（需要 A／B 級 BUY）。建議等第一個真 BUY 候選（屆時經 M6 同意閘門）再驗；硬測需人造 B 級報告，約 $1.5–2 opus。
2. ~~平均回購價~~ 已移出資料包（2026-10-17），改放價格檔 `avg_buyback_price_by_fy`。
3. `.env` 的 SEC_USER_AGENT 用的 email 與帳號 email 不同（先前提醒過，使用者未改；不影響運作）。
4. 已下市代號（如測試中的 QRVO）會留在登錄表 WATCH，目前沒有刪除登錄表列的命令。
5. ~~W4 公開盈餘指引~~ 已處理（2026-10-17）：非金融只有指引、無湊數證據 → 不計 W4，改記 G5 誘因警訊（procedure/03）。

**今天（2026-10-16）真模型花費約 $11（不含更早的 PHM）**：LOW $1.5、OTIS 兩次 $6.3、M4 小測 $0.04、CORP_ACTION 3 次 $0.12、TRIAGE＋THESIS $0.23、第 4 項 $2.49、HOLD_REVIEW $0.08、FULL_B 假價 $0.56。

**今天最後一輪完成的事**（細節見 §4d 末段）：
- M5 模擬（真行情＋真公司行動）與人工調帳；修 SPY 誤偵測、`ledger adjust --first-buy-date`、調帳同步登錄表狀態、不在登錄表的持股記異常。
- CORP_ACTION 真模型：修提示（已登錄行動、8-K 股利、價格比對範圍、決策日例外）；**分拆／併購後取消剩餘加碼批次**（避免用舊每股上限買進）。
- R 清單定義＝SELL-011 R1–R10；FULL_A 產出 3–5 個關鍵假設；THESIS 逐項 R1–R10。
- **H2 持股賣出路徑**：新工作 HOLD_REVIEW（sonnet），框架覆核賣出理由；持股分析不再清空登錄表、論點檔只增補；停止加碼統一由 `sizing.add_blocked` 判斷。

**做真模型測試的固定做法**（沿用）：
- 一律用 `test_runs/` 或 scratchpad 的獨立 state 副本，不碰 `trader/state/`。
- 用防護路由只放行指定工作（範本 `test_runs/m5-live-corp-action-2026-10-16/guard.py` 的 `GuardedRouter`），避免意外升級到 opus；opus 工作先徵求使用者同意並估價。
- live 指令前 `export SSL_CERT_FILE=$(python3 -c "import certifi;print(certifi.where())")`；macOS 沒有 `timeout`。
- 刪目錄用 `rm -rf "${R:?}"/…`（安全檢查會擋未保護的變數展開）。
- 每次改完：`cd trader && python3 -m pytest -q` → 同步 Drive（§1 指令，另加 `--exclude build`；另複製 `routing/prompts/*.md`、`routing/output_schemas.json`、`routing/model_routing.json`、handoff.md、plan.md、plan_dashboard.md）→ 在 Drive 副本 grep 使用者 email 確認沒有。

## 1. 專案是什麼

波克夏式長期選股的**自動紙上交易系統**。
- 規則在 skill 裡（唯讀，交易系統不改寫規則；衝突以 skill 為準）：
  - `berkshire-screen-merged/`：初選；
  - `berkshire-deep-merged/`：深度分析＋兩週檢查；
  - `routing/`：工作→模型等級、提示、輸出格式。
- 交易系統在 `trader/`：排程、資料、調用模型、驗證、記帳、報表。

主目錄：`/Users/zongen/Downloads/claude code/berkshire/`。
同步備份：`…/GoogleDrive-410110@…/我的雲端硬碟/Berkshire-Data/merged-skill/`，同步指令為
```
rsync -a --exclude __pycache__ --exclude .pytest_cache --exclude state --exclude .env --exclude config.yaml trader/ <Drive>/trader/
```
plan.md、handoff.md 另外複製。

## 2. 使用者的要求（必守）

- **語言**：回覆用繁體中文。
- **額度**：Claude 額度吃緊。耗額度的工作先估算；子代理預設 haiku，每個控制約 100k 上下文；使用者已授權用 haiku 子代理平行開發。
- **安全**：
  - 不得把使用者 email 送到無關服務；程式不得寫死 email 或金鑰。
  - 秘密只放 `trader/.env` 或環境變數；設定檔只存變數名稱。
  - SEC User-Agent 由使用者自行在 `.env` 設定。
- **skill 寫法**：還原選股邏輯，不模仿說話語氣；不得用歷史個案舉例；嚴謹，因為會用於金錢交易。
- **沒問題就說沒有**，不要過度工程化。
- **做完要檢核**：子代理寫完後，主代理逐檔讀程式、找邏輯錯誤、補測試。

## 3. 進度

| 里程碑 | 狀態 | 內容 |
|---|---|---|
| M0 基礎架構 | ✅ 完成 | 設定、SQLite（14 表）、CLI（init／doctor／run-job／fetch）、三種 runner（Claude Code `claude -p`／OpenAI 相容／Fake）＋router、沙箱價格隔離、輸出驗證、提示打包、工作引擎（DAG、併發、重試、預算、續跑、動態升級） |
| M1 資料層 | ✅ 完成（2026-10-16 真資料驗證通過） | Alpaca（日線／公司行動／新聞／日曆）、交易日曆（NYSE 規則備援）、SEC（財報特徵／申報清單／原文轉文字）、FRED DGS30、S&P 500 名單、大型股 P/E 中位（月凍結）、prices_adapter、`trader fetch` |
| M2 初選 | ✅ 完成（2026-10-16 真跑 503 檔，8 分鐘，產出 15 檔） | `trader screen --asof D [--top 15] [--special-max 0] [--inputs DIR]`：`pipelines/screen.py` |
| M3 深度分析 | ✅ 程式完成；真模型實跑 PHM、LOW、OTIS（含同業資料與 EX-99 後 G4 REJECT）；FULL_B＋cap_calc 已驗；RED_TEAM 待第一個真 BUY（見 §0） | `trader analyze --asof D [--tickers X]`；`jobs/deep.py`、`jobs/isolation.py`、`analysis/{dossier_prep,checks,writeback}.py`、`pipelines/analyze.py`；契約 `CONTRACTS_M3.md` |
| M4 觸發檢查 | ✅ 程式完成；TRIAGE、THESIS（含 R 清單、持股版）、PRICE、升級到 FULL 的整條路徑、HOLD_REVIEW（H2）都已真模型實測；注意 M6 同意閘門前升級會直接呼叫 opus | `trader review`，契約見 trader/CONTRACTS_M4.md |
| M5 組合與帳本 | ✅ 程式完成；2026-08-25～10-16 真行情與真公司行動模擬跑通；CORP_ACTION 真模型（haiku）實測通過；人工調帳實測通過 | `trader daily`、`trader ledger`，契約見 trader/CONTRACTS_M5.md |
| M6 儀表板與 opus 同意閘門 | ✅ 完成（2026-10-16）：6a 同意閘門、6b API、6c 網頁、6d Swift 小儀表板＋launchd、6e 匯出、validate-report | 計畫見 `plan_dashboard.md`；6a 同意閘門 → 6b API → 6c 網頁設計稿 → 6d Swift 小儀表板 → 6e 匯出 |
| M7 上線 | 程式完成（2026-10-16）；待初始 bootstrap 完成後啟動排程 | `trader bootstrap`、`trader scheduler`，契約見 trader/CONTRACTS_M7.md |

測試：`cd trader && python3 -m pytest -q` → **643 passed**（含 skill 原有兩支自測）。

已實際驗證：
- `trader init`、`trader doctor`：真打一次 `claude -p`（haiku），3.3 秒回應正確；
- FakeRunner 全流程 DAG；
- FRED 公開 CSV：無金鑰可用；
- S&P 500 名單下載：503 檔。

## 4. 已完成的設定與真資料驗證（2026-10-16）

- `trader/.env`（權限 600，不同步）已填 SEC_USER_AGENT、Alpaca paper 金鑰、FRED 金鑰。
- `trader/config.yaml`（不同步）：強／中／弱＝claude_code 的 opus／sonnet／haiku。
- `trader doctor`：6 項全過（SEC、Alpaca、FRED、三個模型）。
- `trader fetch --asof 2026-10-16 --tickers KO,AAPL,BRK.B,JPM,COST`：
  - features.csv 與 skill `fetch_edgar.py` 逐欄完全一致（0 差異）；
  - prices.csv 與 `fetch_prices.py`（Yahoo）相比：價格差 ≤0.1%（IEX feed 非整合收盤價），vol_36m 差 <1%，回撤差 ≤0.12 個百分點；
  - 兩組輸入跑 `screen.py`，交接檔（去掉價格欄後）完全相同。
- 注意：
  - skill 原腳本用 urllib，python.org 版 Python 需 `SSL_CERT_FILE=$(python3 -c "import certifi;print(certifi.where())")`；
  - `screen.py --out` 是檔名前綴（產生 `{out}_handoff.json`、`{out}.csv`、`{out}.md`），不是目錄。

## 4b. M2 初選（2026-10-16 完成）

- 流程：全股票池 fetch（凍結當月 P/E 中位）→ exclusions.csv（`state/exclusions.csv` 使用者檔＋登錄表 X5：12 個月內完整分析判 TOO_HARD／REJECT）→ 子程序跑 skill `screen.py` → `handoff_noprice.json`（scrub_prices 後自檢無價格鍵）→ 比對登錄表決定 `to_analyze`（新進、從未完整分析、>365 天、或已到 next_full_review_due）→ runs 表與 `state/screens/{asof}/summary.json`。
- 真跑結果（asof 2026-10-16）：503 檔；核心道 257、統計排除 101、特殊情境 117、刪除 15、資料不足 13；短名單 HPQ HIG BBY LOW WTW MAS FDX SPG PHM GL OTIS BALL ALL MO FTV；P/E 中位 26.7（n=474）。
- 真跑結果（第三次，2026-10-16）：核心道 261、資料不足 6（全為近年分拆、財報 <5 年或尚無年報：FDXF HONA Q SNDK SOLV VYLR，依 skill 規則）；短名單不變（HIG HPQ BBY LOW WTW MAS FDX SPG PHM GL OTIS BALL ALL MO FTV）；P/E 中位重凍 26.57（n=478）。
- **資料缺口修正（trader 資料層，不改 skill；每次 fetch／screen 自動套用）**：
  - 前身 CIK：`data/cik_predecessors.json`（套件）＋`state/cik_predecessors.json`（覆蓋）。companyfacts 串接去重後交 skill（同財年取首次申報，時點紀律不變）；filings 也併入前身。已加 XOM 2115436←34088（8-K12B 2026-07-01，Rule 12g-3(a)）。
  - 改組偵測：`inputs.detect_successions`：無年度財報＋有 8-K12B／8-K12G3＋未登記前身 → anomalies（kind=cik_succession，不重複）＋摘要 `cik_succession_suspects`。**不自動猜前身**，由人確認後加對照。
  - 多類股股數 `data/xbrl_shares.py`：shares_cover 與 shares 皆缺者解析最新 10-K/10-Q XBRL：
    - 預設：各類別股數 × **基本** EPS 比例（兩類別法下 Σ 股數×基本 EPS＝淨利；稀釋 EPS 假設已轉換，會失真），EPS 取所有類別共同的同一期間。
    - 基準類別：代號所在類別；代號在無維度時，依 Security12bTitle 的「Class X」對應，否則用唯一 CommonStockMember；EquivalentClassX↔CommonClassX 依字母對應。
    - 類別比例表 `data/share_class_ratios.csv`（套件，每列附申報原文佐證）＋`state/share_class_ratios.csv`（覆蓋同代號）：列入者改用「股數 × 比例」，股數仍每季自動讀申報；表外出現新類別 → 不換算。已填 STZ（Class 1 一比一轉換）、ARES（A＝非表決普通股＝1；B、C、Series B 特別股＝0）。
    - 任何缺值／衝突／EPS≤0 → 不填；P/E 須在 [3, 200]。
  - 結果：V、HSY、BRK.B、XOM、STZ、ERIE、ARES 全部補上，市值合理。

## 4c. M3 深度分析（2026-10-16 程式完成）

- **沙箱隔離（重要）**：實測 `claude -p` 的 Read／Glob／Grep 可讀沙箱外任何檔案 → 價格隔離可被繞過。修法：
  - 沙箱根目錄放家目錄與暫存目錄之外（macOS 預設 `/Users/Shared/berkshire-sandbox-{user}`，權限 700；可設 `sandbox.root`）；
  - `--settings` deny 規則封鎖家目錄、/private、/tmp、/var、/Volumes 等、專案根、state 的 Read／Edit／Write（Read 涵蓋 Glob／Grep）；實測有效；
  - runner 執行前檢查沙箱不在封鎖範圍內，否則拒絕。
- 沙箱佈局：`pkg/`（skill 副本：SKILL.md、references、assets、cap_calc.py、gate3_check.py、output_schemas.json）、`dossier/{T}.md`、`sec/*.txt`（只 DOSSIER）、`dossier/{T}_price.json`（只 FULL_B，gate3 之後）、`{T}/` 前步輸出。
- DOSSIER 前置：skill `build_dossier.py`（以合併前身後的 companyfacts 餵快取，不重複公式）＋ SEC 原文（最新 10-K、其後 10-Q ≤3、DEF 14A、10-K 後 8-K ≤12；只取 primaryDocument，8-K 附件新聞稿未抓）；整段刪 10-K Item 5 與庫藏股買回表、逐行刪價格字句；DOSSIER 輸出的資料包再刪一次價格句。
- FULL_A 後置：gate3_check（skill 函式）→ gate3_result.json；stopped → 框架寫停止結果。
- FULL_B 後置：cap_calc（skill 函式）覆核訊號卡上限與 IV（容差 0.5%），不一致退回重寫一次、再不一致以腳本覆寫；缺輸出檔退回一次；gate3 未過或初篩刪除 → BUY 降 WATCH；BUY → RED_TEAM。
- RED_TEAM：fatal／數字錯／退回 → 退回 FULL_B 一次；仍不過 → WATCH；輸出不合格 → 保守 WATCH。
- 回寫（analysis/writeback.py）：decision_id `代號-YYYYMMDD-序號`；decisions、signals、registry（BUY 尚未成交記 WATCH；既有 HOLDING 不降）；論點檔 `state/theses/{T}_{asof}.md` 凍結（已存在寫 addendum）。
- 初選 skip 規則：MISSING_DATA 不跳過（routing skip_rule）。
- 組合狀態：M5 前以「全額現金、無持股」代入。
- 已知觀察：skill `build_dossier.py` 的「平均回購價」（回購金額÷股數）在資料包內，屬歷史價格水準資訊；這是 skill 設計，未更動，請使用者決定是否要從資料包移除。
- 未驗證：API（openai_compat）後端——使用者目前三級都用 claude_code。

## 4d. 目前狀態與下一步（2026-10-16 記錄）

**真模型實測：MO 改為 LOW（已完成，結果見下）**
- 改動原因（對照 skill 規則）：
  - MO 大機率在 G1 判太難：`knowledge/12` §19 明列菸草法律與立法風險無法預測，放進「太難」籃子。
  - MO 即使過關也幾乎不可能 BUY：近 5 年營收年減約 2%，歷史成長 ≤0 時上限只能用 E÷10%×等級係數，約需盈餘殖利率 ≥13%，現約 5.6%。
  - LOW：與 HD 雙寡占，護城河三測較可能 ≥2 強；盈餘殖利率約 6.0%，接近上限（粗估基準成長 5% 需 ≥6.1%、7% 需 ≥5.6%），可能觸發結論測試，是短名單中唯一較可能出現 BUY、跑到 RED_TEAM 的。權益為負，依 SEL-076b 改看有形資本報酬，不直接排除。
  - 備選：OTIS（護城河強但大概 WATCH，會跑困境檢查）。
- 執行方式：獨立測試資料夾（同 PHM 做法，`test_runs/m3-LOW-2026-10-16/`），**不寫入正式 state**；live 命令需 `SSL_CERT_FILE=$(python3 -c "import certifi;print(certifi.where())")`。
- 預估：全程通過約 8–10M 輸入 token、30–40 分鐘；中途停關則少很多。結論若為 WATCH，RED_TEAM 留待之後真的出現 BUY 再驗。
- 之後依序：TRIAGE／PRICE／THESIS 真模型各一次 → 若 BUY 跑 daily 下單成交流程；CORP_ACTION 真模型尚未驗。

**LOW 實測結果（2026-10-16，結果存於 `test_runs/m3-LOW-2026-10-16/`，未寫入正式 state）**
- NO_ACTION／TOO_HARD，停在 G1：零售屬 SEL-050 預設太難型；SEL-051 第 3 條不成立（W-REV：營收 2020→2025 年複合 −0.75%），第 2 條（非產業共性的可量化個別優勢）也無法證明。判斷符合規則。報告附註：營收下降可能部分來自業務範圍變動（資料包未載），模型未以記憶補充。
- 用量：DOSSIER haiku 2 次共 5.08M 輸入、$0.42、438 秒；FULL_A opus 0.65M 輸入、$1.04、123 秒。價格隔離檢查通過。
- DOSSIER 第一次失敗的根因（PHM 同樣）：提示要模型「把 JSON 寫到檔案」，框架卻讀最後回覆（存檔由 `jobs/deep.py` 負責）。已改 DOSSIER／FULL_A／FULL_B 提示：最後回覆只輸出 JSON 本身、不要自己寫該檔。重試成本約 $0.02，損失不大。
- 教訓：短名單中多檔落在 SEL-050 預設太難型（零售 BBY、LOW；硬體 HPQ；REIT SPG；菸草 MO 依產業手冊）。下一個實測候選：OTIS（維修服務、營收成長為正、未命中預設太難清單）。

**OTIS 實測結果（2026-10-16，`test_runs/m3-OTIS-2026-10-16/`，未寫入正式 state）**
- 流程：DOSSIER（haiku，一次成功）→ FULL_A（opus，2 次）→ FULL_B（sonnet，2 次：cap_calc 覆核退回一次）。
- 結論 NO_ACTION（停在 G7／步 10）：起點 B，三次降級到 D，D 級不做；cap_calc：IV_base 56.39、現價 70.39、g_impl 9.45%。護城河三測：定價權強、攻擊／複製強、砍價弱，閘門 3 通過。
- 用量：DOSSIER 1.55M 輸入 $0.15；FULL_A 1.18M $2.15；FULL_B 0.68M $0.75；合計約 $3.05、約 14 分鐘。
- **實測發現的框架問題（都已修，580 測試過）**：
  1. 三次降級中兩次是框架資料缺漏造成：
     - DOSSIER 提示沒要求摘錄 procedure/08 §1 的「其他附註」（營收認列、股權薪酬、商譽、退休金、租賃、表外）→ 每檔都會自動降一級（PHM 也中）。已在 DOSSIER 提示加「附註摘要」與或有負債、會計師意見、內部人持股、關係人交易、最近一季 10-Q 數字。
     - 資料包缺固定資產淨額、流動資產、流動負債 → SEL-055 有形資本報酬只能用代理口徑（OTIS 被估約 20% 而降級；實際有形資本為負，屬「負營運資金」型）。已在 `dossier_prep.supplement_section` 補表（沿用 skill 的 `fetch_edgar.annual_series` 時點邏輯；未分類資產負債表如建商照實標缺）。
  2. DOSSIER→FULL_A 沒傳 portfolio_state、largecap_pe → FULL_B 拿到 null（P4′ 判不適用、組合狀態空）。已修並加測試。本次結論不受影響（norm P/E 19.8 <20；D 級本來 0%）。
  3. FULL_A 把 null 寫成字串 "null" 被判格式錯 → validate 對允許 null 的列舉把 "null" 視同 null。
- **修正後重跑 OTIS（`test_runs/m3-OTIS-rerun-2026-10-16/`）驗證有效**：
  - DOSSIER 一次成功，資料包含「附註摘要」9 項與補充資產負債表；FULL_A 依補充表判 SEL-055 四型＝偉大（有形資本 6/7 年為負，非代理口徑）。
  - 起點 A；降級 2 次：P-01（缺第三方查證資料：競爭者年報、產業統計、客戶留存率 → A 上限降一級，規則明文）、RISK-061 W1（重組費連續三年）→ C → **WATCH**（PASS_ALL，停 G7）。
  - cap_calc：IV_cons 38.89、IV_base 49.29＝自動買進上限，P2′ 71.96，P4′ 94.59（P/E 中位已正確傳入），現價 70.39。即使 B 級，上限仍是 IV_base，結論穩定。
  - 參數：portfolio_state、largecap_pe 已傳到 FULL_A、FULL_B。
  - 用量：DOSSIER 6.35M 輸入 $0.56（附註摘要使輸入增加）；FULL_A 0.87M $1.95；FULL_B 0.66M $0.69；合計約 $3.20、約 17 分鐘；三步都一次成功。
  - 可選改進（未做）：P-01 的第三方查證資料（美國上市同業 10-K、客戶留存率）由框架抓取放入資料包，可讓 A 級可達；非美國競爭者（KONE、Schindler）無 SEC 申報。

**M4 真模型實測（OTIS，2026-10-16；`test_runs/m4-OTIS-triage-thesis/`、`test_runs/m4-OTIS-price/`）**
- 情境為模擬：在測試副本改登錄表（上次複查日、上限），申報用 `--offline-filings` 注入 OTIS 真實的 2026-07-22 財報 8-K。
- TRIAGE（haiku，$0.005）：財報 8-K 無新聞稿內容 → 判「不確定」；CEO 退休新聞 → 「是」→ 升 THESIS。
- THESIS（haiku，$0.02）：「疑似」——CEO 退休依 SELL-010 須立即完整複查；Service 利益率 H1 23.1% 接近失效線 23.0%。判斷有規則依據 → 升 DOSSIER／FULL。**在進入 opus 前人工停止**（M6 同意閘門尚未實作，目前會直接跑 opus）。
- PRICE（haiku，$0.007）：照抄 cap_calc（上限 49.29，推翻測試假設的 75）、gate2 不過、WATCH；框架回寫登錄表上限與複查日。第一次跑時模型因「登錄表上限與 cap_calc 不同」設 needs_full → 已在 PRICE 提示說明「上限不同是正常重算，以 cap_calc 為準」，重跑正確。
- **實測發現並修正**：
  1. 價格隔離：新聞標題含目標價、分析師評等、股價漲跌 → `is_price_headline` 在來源端（review.py 產 news.json）丟棄，review_due 與所有模型都看不到。
  2. 價格隔離：論點檔「現價（70.39…）」未被刪（清單缺「現價」）→ 補現價、收盤價、報價、成交價、current/trading price；論點檔改逐句刪（`scrub_price_sentences`），不連論點句一起刪。
  3. TRIAGE／THESIS 讀財報 8-K 無新聞稿 → 共用 `earnings_exhibit` 接上 EX-99。
  4. 登錄表 thesis_file 存絕對路徑，state 搬移後失效 → 改存相對 state 路徑；舊絕對路徑失效時取同名檔。

**可選改進已完成（2026-10-16）：同業與第三方查證資料**
- `trader/analysis/peers.py`（DOSSIER 前置自動附到資料包「同業資料」段）：
  - 產業共同衝擊年：SKILL §3 演算法（SIC 前兩碼、股票池內 ≥5 家、中位 ROE ≤0 或 < 前 5 年中位一半）；樣本不足照實標。
  - 主要競爭者：本公司 10-K 競爭段點名且在股票池（S&P 500）者；未點名但 GICS 子產業很窄（含本公司 ≤5 家，`NARROW_GICS`，推論；如 LOW／HD、PHM／DHI／LEN）→ 取營收前 2 家→ 同組視為直接競爭者。附其最新 10-K Item 1、7 節錄（刪價格句）。
  - 同口徑指標表：同 GICS 子產業前 5 家（缺 GICS 用 SIC 四碼）：營收、5 年年複合、營業利益率、ROE（5 年中位）。
- 財報新聞稿：8-K 項目 2.02 自動接上 EX-99（SGML 全文取附件；OTIS 標 EX-99、LOW 標 EX-99.1 都支援）。
- DOSSIER 提示加「競爭與產業關鍵指標」段（點名競爭者、競爭者年報摘要、留存／續約／單位量等 KPI 附出處）。上限放寬到 40k tokens。
- 實測（不呼叫模型）：LOW → 附 HD 10-K 節錄；OTIS → 競爭者皆非美國上市照實標缺，新聞稿含服務有機成長等 KPI。尚未用真模型跑 DOSSIER＋FULL_A 驗證對等級的影響。

**M5 每日流程模擬（2026-10-16，不呼叫模型；`test_runs/m5-sim-2026-08-25_10-16/`）**
- 做法：假 BUY 決策（8/25 APH、ADI；9/22 CTVA、QRVO，B 級、上限＝收盤×1.5）＋ Alpaca 真行情與真公司行動；CORP_ACTION 用 FakeRunner 照規則作答（測模型以外的整段）。腳本 `sim_m5.py`，調帳後續跑 `sim_m5_after.py`。
- 驗證通過：下單→次日成交、分批建倉到約 10%；APH 2:1 拆股（61→122 股、成本 158.97→79.48）；ADI 除息 9/1 依除息前一日持股 21 股記應收 $23.1、9/15 發放沖銷；APH 拆股後股利 0.125×122 記應收、10/14 入帳；CTVA 分拆（VYLR）與 QRVO 併購（0.96 SWKS＋$32.5）暫停該檔、不入帳；QRVO 下市後淨值以成本估並列 missing_close。
- 人工調帳實測：CTVA 成本依 10/1 收盤 12.57／67.21 分配給 CTVA 與 VYLR；QRVO 歸零、SWKS 81 股、現金 +2,812.86（含 0.6 股零股）；解除暫停後淨值回到約 10.19 萬。
- **發現並修正**：
  1. Alpaca 公司行動端點以「處理日」過濾（股利處理日＝發放日），漏掉 ADI 股利與 QRVO 併購 → 查詢區間往後放寬 `CA_PROCESS_LAG_DAYS`（60 日），detect 依除息／生效日過濾。
  2. 每份 8-K（8.01／5.03／3.03）都產生只能選「其他」的 CORP_ACTION → 一律需要人工並暫停 → 新增類型「無公司行動」（只限 8-K 來源、數字欄須為 null，不入帳），並附 8-K 全文與 EX-99 給模型。
  3. 基準 SPY 被當持股偵測公司行動，每季股利都派一個模型工作 → 偵測只涵蓋持股、未成交委託與觀察名單。
  4. 分拆／併購換得的新股建倉日被記成調帳日（影響 SKILL §6 一年限制）→ `ledger adjust --first-buy-date`。
  5. 調帳把持股歸零時登錄表仍為 HOLDING → 歸零轉 WATCH、新建持股 WATCH 轉 HOLDING。
  6. 不在登錄表的持股（VYLR、SWKS）觸發檢查完全涵蓋不到 → 每日流程記一筆不暫停的 `unregistered_holding` 異常與提示，待人工排完整分析（M6 同意閘門完成後可改為自動排入佇列）。
- 已知行為（未改）：需要人工的公司行動在調帳前，淨值會失真（CTVA 分拆後一週少算約 8,600 元）；已下市代號（QRVO）留在登錄表 WATCH，目前沒有刪除登錄表列的命令。

**真模型小測（2026-10-16，使用者同意第 1～3 項；防護路由只放行指定工作，DOSSIER 以後一律擋下）**
- CORP_ACTION（haiku，`test_runs/m5-live-corp-action-2026-10-16/`，同上模擬改用真模型，共跑 3 次，每次約 $0.04、11～13 次呼叫）：
  - 第 1 次多暫停 ADI、APH：(a) 模型拿除息日股價跌 2% 對照 1.1 元股利判「不符」；(b) APH 的 8-K 是拆股事後公告，模型把生效日寫成 9/2（Alpaca 9/3），去重失敗。
  - 發現更嚴重的問題：CTVA 分拆後股價 13 元，加碼仍用分拆前每股上限 120 → 計畫加碼 642 股。已修：建倉期間若有分拆／併購換股／其他（生效日 ≥ 首次建倉日）→ 取消剩餘批次，需重新分析（`CA_INVALIDATES_CAP`）。
  - 提示修正：CORP_ACTION 輸入加「本檔近期已登錄的公司行動」；8-K 所述行動已登錄 → 無公司行動、不比價；8-K 一般股利宣告 → 無公司行動（股利以 Alpaca 除息入帳）；價格比對只用於拆合股與 ≥10% 特別股利；對共通規則第 1 條的例外（事件日可等於決策日、Alpaca 處理日在未來屬預告資料）。
  - 第 3 次結果與預期完全一致：ADI 股利、APH 拆股、APH 股利自動入帳；8-K 全判無公司行動；只暫停 CTVA（9/25 公告分拆即暫停並取消剩餘批次）與 QRVO。
- TRIAGE＋THESIS 持股版（`test_runs/m4-OTIS-holding-triage-thesis/`，假條件：持有 OTIS 120 股、上次複查 7/15）：
  - TRIAGE（haiku，$0.009）：財報 8-K 附 EX-99 後能讀到分部數字，判「是」（Service 利益率 23.2% 貼近 23.0% 失效線、New Equipment 利益年減）；CEO 退休判「是」。
  - THESIS（sonnet，$0.22）：「疑似」，四條失效條件皆「接近」並附 10-Q／EX-99 出處與數字；未誤判證偽。升級 DOSSIER 被防護擋下（未呼叫模型）。
  - THESIS 回報「輸入沒有 R 清單」→ 已接上（2026-10-16，使用者要求「先定義它」）：
    - 定義來源：R 清單＝SELL-011 完整複查清單 R1–R10（`rule_index/04_hold_sell.md`），R1 對照「買進時寫下的 3–5 個關鍵假設」（PSY：保守價值區間附 3–5 個關鍵假設）。
    - FULL_A 的 thesis_draft 新增必填 `key_assumptions`；FULL_B 論點檔的保守價值區間段列出關鍵假設（≥3 條）。
    - THESIS 輸入附 SELL-011 原文（執行時從 rule_index 擷取，`review_jobs.r_list_text`）；輸出 `r_list_status` 逐項 R1–R10：無變化／改善／惡化／無資料／不在本工作範圍。R7、R8 需價格 → 不在本工作範圍（價格隔離，由 PRICE／完整分析處理）。「惡化」只在符合該項惡化訊號時使用，並先依 SELL-012 分週期或結構；任一 R 項惡化 → 至少疑似（rulebook H1 → H2）。
    - 舊論點檔（如 OTIS_2026-10-16）沒有關鍵假設 → R1 照實標無資料，下次完整分析後補上。

**第 4＋5 項真模型實測（2026-10-16，`test_runs/m4m5-OTIS-holding-full-2026-10-16/`，約 $2.49）**
- 路徑：OTIS 持股（假條件：上次複查 7/15；FULL_B 價格改 40.0）→ TRIAGE（haiku $0.009）→ THESIS（sonnet $0.30）→ DOSSIER（haiku $0.52，含同業資料）→ FULL_A（opus $1.51＋格式重試 $0.15）。
- THESIS 附 R 清單後：疑似；R1 無資料（舊論點檔無關鍵假設）、R7／R8 不在範圍、R9 惡化（總債務 +4.8 億、買回＋股利 11.4 億 > 自由現金流 6 億）→ 依 H1 升級。
- FULL_A：G4 REJECT。W1（重組費連三年）＋W4（公開盈餘指引並期中修訂，證據來自新接上的 EX-99）→ P-61 警訊 ≥2 淘汰。規則套用正確；前次 C 級 WATCH 是因當時資料包沒有新聞稿。注意：W4 對多數大型股都會命中，再加任一警訊即淘汰。
- 未進 FULL_B，RED_TEAM 未測（需有 BUY）。
- **發現設計缺口 → 已補（使用者選 1，2026-10-16）**：持股的完整複查原本只跑買進閘門，停在 G4 只記 NO_ACTION，不走 H2。
  - 新工作 `HOLD_REVIEW`（medium，可看價格；`jobs/hold_jobs.py`、提示 `routing/prompts/HOLD_REVIEW.md`，附 rulebook §9 原文）：持股（帳本有股數或登錄表 HOLDING）的完整分析在 FULL_A 停止、FULL_B 非 BUY、RED_TEAM 降 WATCH 時接續；BUY（加碼）不需要。輸出 HOLD／TRIM／SELL_ALL、sell_category、trim_fraction、stop_adding、observe_until、falsified_assumption、Q0–Q6 路徑。
  - 框架覆核：業務類不得 TRIM、須具名被否定的假設；估值類須有 IV_base 且價格 ≥ IV_base；TRIM 只限估值類、比例 0–1；建倉未滿 2 年須具名（Q5）；制度／事件／換股／資金需求類不由模型下單 → HOLD＋停止加碼＋異常 `hold_review_needs_human`。覆核不過退回一次，仍不過或輸出不合格 → HOLD＋停止加碼＋異常。
  - 回寫 `write_hold_review_result`：決策 action＝HOLD／TRIM／SELL_ALL（review_type HOLD_REVIEW）、論點增補（H2 路徑）、登錄表合併更新。持股的 FULL_A 停止不再清空登錄表（保留上限、論點檔、建倉日）；持股的 FULL_B 論點稿寫成原主檔的增補，不換主檔，保留 cost／first_buy_date／build_plan。
  - 下單：TRIM 依 trim_fraction（其他來源仍減半）；`sizing.add_blocked`：最近一筆 THESIS 疑似／證偽或 HOLD_REVIEW stop_adding → 擋加碼批次與 ADD；觀察期滿不自動恢復，須新的複查結論。
  - 參數沿 DOSSIER→FULL_A→FULL_B→RED_TEAM 傳遞（`deep.CARRY`：holding、thesis_result、thesis_file、reasons…）；THESIS 升級時附 thesis_result。
  - 真模型驗證（`test_runs/m6-OTIS-hold-review-2026-10-16/`，sonnet $0.08）：OTIS（FULL_A G4 停止）→ Q0 否、Q2「只是疑似」（失效條件接近未觸發、無會計或誠信問題證據）→ HOLD＋停止加碼、觀察至 2027-04-16；Q3 因無 IV 判不適用。符合規則。

- 第 5 項 RED_TEAM（`test_runs/m5-OTIS-fullb-fakeprice30-2026-10-16/`）：以 OTIS 重跑的 FULL_A 輸出重跑 FULL_B，價格假設 30（< 上限 49.29），sonnet $0.56 → WATCH：C 級只能 WATCH（cap_calc conclusion_hint；BUY 需 A／B 級），未觸發 RED_TEAM。注意模型把 stopped_at_gate 標成 G7（實際是等級），屬標示不精確。RED_TEAM 真模型尚未驗；建議等第一個真正的 BUY 候選出現（M6 同意閘門會讓 opus 工作先經使用者同意）再驗。

**M6 儀表板（計畫已確認，未動工）** — 詳見 `plan_dashboard.md`
- 小儀表板：SwiftUI 浮動視窗、固定桌面層、開機自動啟動（本機無 Xcode，不做 WidgetKit）。顯示報酬率、SPY 同期（含股利總報酬）與超額、前三持股、收益、現金比、走勢圖。
- opus 同意閘門：strong 工作（FULL_A、RED_TEAM）設為 `awaiting_approval`，弱模型前置先跑；同意單位為一檔（含後續 RED_TEAM）。桌面一鍵全同意、網頁逐檔同意／延後／拒絕。延後留在佇列；拒絕判 MISSING_DATA，下次觸發再排。沒有待同意時按鈕完全隱藏；顯示「N 檔 · 約 X M token · 約 Y 分鐘」，按下再確認一次。
- 等待 opus 期間該持股暫停加碼，賣出照常。
- 本機 FastAPI 127.0.0.1:8787 為唯一資料來源；同意 API 需 `state/.dashboard_token`（600）＋Origin 檢查；只限本機。
- 網頁：總覽、持股、交易紀錄、分析紀錄、待同意、異常；原生 HTML/CSS/JS＋uPlot，資源放本機。設計稿（總覽頁、小儀表板外觀）須先給使用者確認。

## 5. 關鍵設計（改動前先懂）

- **模型只在工作引擎裡被調用**；skill 腳本與資料層都是純程式。
- **價格隔離**：
  - DOSSIER、TRIAGE、THESIS、FULL_A 的沙箱不得出現 `*_price.json` 或價格鍵，`stage_inputs` 會清除或拒絕。
  - FULL_B 只有在 `gate3_answers.json` 的 mtime 早於工作建立時間時，才開價格檔。
- **輸出驗證**：`output_schemas.json` 是範例形狀，不是 JSON Schema，由 `jobs/validate.py` 依 7 條規則編譯（CONTRACTS.md）。
- **不合格輸出的處理**（重試 1 次後仍不合格）：
  - 一般工作 → 記 NO_ACTION/MISSING_DATA；
  - TRIAGE → 視同「不確定」，升級 THESIS；
  - CORP_ACTION → 寫 anomalies 並暫停該檔。
- **`claude -p` 隔離旗標**：`--setting-sources project,local --strict-mcp-config --no-session-persistence`，不載入使用者的 hooks、外掛、MCP。
  - 不可用 `--bare`：它需要 ANTHROPIC_API_KEY，無法用訂閱登入。
- **資料時點**：
  - SEC 申報日 < asof；FRED 日期 < asof；價格 ≤ asof。
  - 拆股字串只計 ex_date ≤ 實際收盤價日期，和市值基準一致。
- **代號標準形**：大寫、點號（`BRK.B`）。SEC 對照表兩種寫法都收。
- **重用 skill 計算**：`fetch_edgar.features`、`fetch_prices.feats`、`screen.build` 都直接匯入呼叫，不複製公式。
- **SEC 快取**：
  - 由 `SecClient` 負責過期重抓（20 小時）；抓取失敗時回退舊快取，記在 `stale`。
  - `since` 早於 recent 範圍時，補讀舊分頁。
- **P/E 中位**：
  - 口徑：市值 ÷ 5 年平均淨利（ni5_mean > 0），另有 TTM 與金融類版本。
  - 只能用整個股票池凍結；用部分代號時拒絕凍結，否則整月錯值。
  - 樣本少於 30 → None，此時 P4′ 不適用。

## 6. 已知待辦（依里程碑）

- **M4**（已完成程式）：
  - 論點檔：登錄表 thesis_file，否則取最新 `{T}_{date}.md`；THESIS 結果寫 addendum。
  - PRICE 用 `state/cap_inputs/{T}.json`（FULL_B 回寫時保存）重算；重算輸入寫在工作目錄，不改 state。
  - 匯出登錄表略過 None 欄位（skill 對 None 清單會失敗）。
  - TRIAGE／PRICE／THESIS 真模型已驗（2026-10-16）。
  - THESIS 輸入附 SELL-011 R1–R10；疑似／證偽升級時帶 thesis_result 一路傳到 HOLD_REVIEW。
- **持股完整複查（H2）**：見 §4d「第 4＋5 項」段；`jobs/hold_jobs.py`、`routing/prompts/HOLD_REVIEW.md`、`writeback.write_hold_review_result`、`sizing.add_blocked`。
- **M5**（已完成程式）：
  - 限價單成交價不劣於限價：買 = min(開盤×(1+滑價), 限價)，確保買價不高於上限。
  - 需要人工的公司行動一律不入帳（含現金對價），暫停該檔，人工 `trader ledger adjust`。
  - 拆合股同步調整：持股、未成交委託、登錄表每股欄位、`state/cap_inputs/{T}.json`。
  - 每日流程讀 (前一交易日, 今天] 的決策，週末的分析由下一個交易日接手。
  - 推論近似：15 交易日以 21 曆日近似；缺 G8 預期報酬欄位時以 price_to_conservative 排序。
  - dry-run 只算計畫，不寫入、不跑公司行動解讀。
  - 公司行動偵測只涵蓋持股、未成交委託、觀察名單（不含基準 SPY；M6 的 SPY 股利另抓）。
  - 建倉期間有分拆／併購換股／其他 → 取消剩餘加碼批次（`CA_INVALIDATES_CAP`）。
  - 需人工的公司行動在調帳前淨值會失真（例：CTVA 分拆後一週少算約 8,600）；不在登錄表的持股記 `unregistered_holding` 異常（不暫停），M6 後可改自動排入分析佇列。
- **M6**：6a 完成（strong 工作 FULL_A、RED_TEAM；HOLD_REVIEW 是 medium，不需同意）。已知行為：同意數日後才執行時，分析仍用原資料日（時點紀律），未設同意期限；`unregistered_holding` 尚未自動排入分析佇列。
- **風險**：
  - S&P 500 名單來源是 GitHub datasets 社群 CSV，可用 `state/universe_override.txt` 覆寫。
  - 名單是現有成分股，有存活者偏差，只影響回測，不影響往後的紙上交易。

## 7. 開發方式（沿用）

1. 主代理先寫介面契約（`CONTRACTS*.md`）與共用核心檔。
2. 平行派 haiku 子代理：每個只寫指定檔案、跑全部測試、回報偏離處。
3. 主代理逐檔檢核、修正、補測試，最後同步 Drive。

## 2026-10-17 第三次審查 H1／H2 修正
備份：`bug report/review3/backup_before_fix/`（trader_pkg、tests、handoff.md，修改前）。測試 711 個全過（原 708 + 3）。
- **H1 scheduler**：`tick()`／`run_forever` 改 `check_ready(strict=False)`，只在登錄表為空時擋；`plan()` 在有待同意／未完成工作時只暫緩 `watch` 與季度初選（`screen`），`daily`、`freeze`、`report` 照跑，處理完後自動補上。`check_ready()` 預設 strict，只有 `scheduler install` 用（cli.py）。注意：`run`／`once` 也改成非 strict——cron 模式每 10 分鐘跑一次 `once`、launchd 重啟 `run` 時，若仍嚴格擋待同意，H1 會經由重啟再現。測試：`test_scheduler.py` 新增 `test_pending_approval_does_not_halt_daily_but_defers_watch_and_screen`，並調整 `test_refuses_before_bootstrap`。
- **H2 公司行動**：`ledger/corp_actions.py` 新增 `record_job_failure`、`unresolved_failed_events`（異常 kind=`corp_action_job_failed`，pause_trading=1，detail 存完整事件）。`pipelines/daily.py`：CORP_ACTION 工作非 done／skipped（失敗、預算耗盡、未完成）→ 寫異常暫停該檔；每次 daily 先把未入帳的失敗事件重派（不受 (prev,date] 窗口限制），入帳後自動結案解除暫停；同日重跑換新 job_id（`...r2`）；`run_daily` 新增可選 `sleep` 參數供測試。daily 本身仍標 done（以異常 + notes 呈現）。測試：`test_daily_pipeline.py::test_failed_corp_action_job_pauses_ticker_and_is_redetected_next_day`。
- **退避**：`llm/base.py` 額度類訊息（usage limit／quota／rate limit／credit／overloaded）退避改 60、120、240 秒（共 7 分鐘），其餘仍 2、4、8。不再更長，因訂閱額度重置要數小時，真正的保險是上面的事件重派。測試：`test_llm.py::test_quota_errors_back_off_longer`。
- 已知限制：重派後若該事件反覆失敗，異常會一直開著該檔暫停（含賣出）直到成功或人工處理；M3（失敗工作不重跑）未動。


## 2026-10-17 第三次審查 M1／M2／M3／M5／L2 修正
備份：`bug report/review3/backup_before_fix2/`。測試 `cd trader && python3 -m pytest -q` 722 個全過；`berkshire-deep-merged/scripts/test_review_due.py` 通過。
- M1 `pipelines/analyze.py::_portfolio_state`：有持股時回傳 cash、committed（未成交買單委託金額）、n_holdings，holdings 含 qty／avg_cost／cost_value／cost_weight（成本基礎權重，非市價）／first_buy_date。不含市價；portfolio_state 只進 FULL_B／HOLD 類價格可見工作的提示。
- M2 `ledger/corp_actions.py`：現金股利缺發放日 → 除息日 +30 天（`DEFAULT_PAY_LAG_DAYS`）為推論發放日，data 標 `pay_date_inferred`、現金分錄備註「發放日推論」、`settle_dividends` 回傳也標。選此法理由：不需人工介入就不會永遠卡應收，且比常見 2–4 週發放晚，寧可晚入帳。舊列（data 無 pay_date）同樣處理。若真實發放日不同，需人工用 ledger adjust 對帳。
- M3a `writeback._next_free_id` 同時避開 decisions 與 signals 已用編號。
- M3b 新增 `trader jobs retry --run-id X [--ticker T] [--job-id J] [--no-run]`：failed（及依賴失敗的 skipped）→ pending、清 error／result，保留 llm_calls 與 attempts，對應 job_failed 異常結案後由引擎續跑。CORP_ACTION 不重設（由 daily 重派）。限制：不支援「只重做後處理、不重呼叫模型」，重跑會重新呼叫模型。
- M5 `review_due.py`：10-K/A → THESIS；原始 10-K／20-F／40-F 仍 FULL。測試補在 `test_review_due.py`。
- L2 `jobs/engine.py::run`：有 failed／skipped 工作時 run 狀態為 `failed`（無 CHECK 限制；daily 的 review_ok 判斷 status in done/awaiting_approval 且 failed==0，行為不變；scheduler._daily_done 只認 done）。每個 failed 工作（CORP_ACTION 除外，由 daily 的 `record_job_failure` 寫暫停異常）寫 `job_failed` 異常（pause_trading=0，detail 含 run_id／job_id／job_type／error），同 job 已有未結異常不重複。強殺：runs 停在 running（不是 done），下次 `_begin` 把 running 工作改回 pending，runs 仍為 running 直到跑完。awaiting_approval 優先於 failed（要先能同意）。
- 順手修：`test_engine.py::test_single_element_array_accepted` 原本因 fixture 錯誤（巢狀 list）其實是 failed 卻被舊的 done 狀態遮住，改用正確 fixture；注意 `extract_json` 只回傳 dict，引擎內「單元素陣列」分支在 fake／真實路徑都不會被觸發。
- 未動：M4、L1、L2 其他子項。


## 2026-10-17 M4 處理與資料包補強
測試 744 個全過（原 722 + 22）。未呼叫模型；只對真實 SEC 做唯讀實測（輸出放 session scratchpad，未寫入 state/work）。
- **初篩排序**（`berkshire-screen-merged/scripts/screen.py`）：非金融六項的 `g("rev_cagr_5y", False)` 換成 `g("roe_min5")`；負權益年度為缺值 → 0.5 中性。文件 `SKILL.md` §4、`references/evidence_statistical.md` §2 寫明：改的是優化目標（G3 命中率），**未回測**；2–3 季後 G3 命中率沒改善就回復。試算（10-16 初篩資料）：前 15 名換 5 檔。`test_screen.py` 加 `test_quality_factor`。
- **REJECT 依據**：`routing/output_schemas.json` FULL_A 新增 `moat_evidence`（三測各 `足夠 | 缺資料 | null`），`prompts/FULL_A.md` 補說明；`writeback.reject_basis()` 於 G3 REJECT 算 `有反證／證據不足／其他`，寫入決策紀錄與登錄表。`reason_code`、status、X5、`review_due` 都不變。「證據不足」維持 12 個月排除（使用者決定）。
- **一句話理由 bug**：`write_stop_result` 原讀 `phase_a["reason_one_liner"]`（模型不輸出這欄）→ 改為依序讀 `reason_one_liner`、`thesis_draft.one_liner`、`report_A.md` 的「一句話理由：」。14 筆舊決策（13 REJECT＋LOW）已補回，備份 `state/trader.db.bak-20261017-before-backfill-oneliner`；7 筆取自 report_A（模型 one_liner 為空）。`reject_basis` 舊資料為 null。
- **資料包補強**（`trader/analysis/`、`trader/data/sec.py`、`jobs/deep.py`、`prompts/DOSSIER.md`）：
  - `insiders.py`：Form 4／4/A 近 24 個月，每人公開市場買賣股數（賣出分 10b5-1）、最後直接持股、其他代碼筆數；**不輸出成交價與金額**；上限 400 份，超過標實際涵蓋起日。寫入資料包「內部人交易」段與 `dossier/_insiders.json`。
  - `earnings_exhibit`：財報 8-K 除 EX-99.1 外，再收至多 2 份 EX-99.x（每份 4 萬字上限）；`annual_report_exhibit`：10-K 的 EX-13（6 萬字上限），**另行刪價格**後附在 10-K 文字檔後。`SecClient.exhibit_texts` 略過 PDF／圖檔附件。
  - `peers.py`：10-K 點名但不在 S&P 500 的競爭者也比對（名稱只去法律形式字、至少兩字、SIC 大類相同，最多 4 家）；連字號名稱處理（Sherwin‑Williams 不再誤配 WMB）；衝擊年樣本 <8 家時以 `SecClient.sic_companies`（SEC 公司搜尋，快取 30 天）補本公司與池內同業的 SIC 四碼公司，最多讀 30 家。資料包列出補充公司名，衝擊年含補樣本時附「判定說明」，FULL_A 須在誠實標示逐年說明本公司是否同受衝擊與剔除方向（使用者決定保留補樣本，照 SIC 前兩碼規則）。
  - 實測：WTW 補充資料只有定性「strong client retention」；ALL 有 policies in force 數字；FDX 補樣本後 2020–2021 判為衝擊年（主要是航空客運拉低），交分析在誠實標示說明。
- **換機**：`換機匯出與封存程序.md`（匯出清單與 9 條檢查、`.backup` 打包、本機封存四層隔離、替換、還原）。注意 launchd 的 dashboard／widget 有 KeepAlive，替換前須 `desktop uninstall`。
- **未做**：法說會逐字稿（SEC 沒有）、留存率數字；`cap_input_file` 仍存絕對路徑（使用者決定先不改，目前無程式讀取）。


## 2026-10-18 G8 持股比較檔
測試 755 個全過（原 744 + 11），同日補完後 770 個。未呼叫模型；帳上仍 0 檔持股，真實持倉下未跑過。
- **問題**：FULL_B 的 G8 要比「加碼現有最佳持股」，原本只拿到持股成本（`portfolio_state`），沒有持股的價值、等級、預期報酬；排序欄 `g8_adjusted_expected_return` 也從未有卡寫入。
- **作法**（使用者決定用決策日收盤價，成本權重保留當參考；procedure/06 §2 與 CORE-07 不得以自身買價作門檻）：
  - `trader/portfolio/holdings_view.py`：每檔持股以 `state/cap_inputs/{T}.json`（已隨拆合股換算）換上決策日收盤與當日長債重跑 cap_calc；預期報酬＝oe ÷ 收盤＋保守 g0，×等級係數（A 0.9／B 0.75／C 0.5）。列市價權重、成本權重、可否加碼（異常暫停、`add_blocked`、權重 >15%、等級非 A／B）、`best_addable`、持股 ≥10 檔時的 `weakest`。論點檢查等快速工作的無估值卡跳過。
  - `pipelines/analyze.py`、`review.py`：抓價時含持股（review 的 scope 不含持股時另抓），寫 `work/{run}/_portfolio/holdings_g8.json`；**不進工作參數**（參數會沿用到 FULL_A）。
  - `FullBHandler.prepare`：排除本標的後放進 FULL_B 沙箱 `portfolio/holdings_g8.json`；DOSSIER／FULL_A／RED_TEAM 沒有。舊 run 沒比較檔 → 以帳本重建、收盤價缺並附 `warning`。
  - FULL_B 摘要新增 `g8` 比較表（`output_schemas.json`）；訊號卡新增 `g8_adjusted_expected_return`（`signal_card_schema.json`），FULL_B post 與 PRICE 快速通道**以腳本重算寫入**（模型數字落差只記日誌，不改訊號）。`sizing._rank_key` 從此可用這欄排序。
- **同日補完**（使用者要求直接做完再回報，測試 770 個全過）：
  - **國庫券、大盤兩列**（`portfolio/g8_benchmarks.py`、`data/fred.py` 新增 `latest`、`equity_gdp`）：國庫券＝FRED DGS3MO；大盤＝P-66 衍生數列（NCBEILQ027S ÷ GDP，季初日期 +170 天才算公布；FRED 是修正後數值，回測不可用）分區 → P-27。區內取值與摩擦是推論：低 11%、中 8%→5%、高 5%→3% 線性、極高 3%，扣 1pp，×0.8。10-16 實測：比值 255%、分位 100 → 極高，大盤調整後 1.6%；國庫券 4.28%。
  - **觀察名單次佳**：登錄表 WATCH 且未持有者，以決策日收盤重算；只有 A／B 級且收盤 < 重算的自動買進上限才算可得選項（`best_watch`）。
  - **G8 輸了自動降級**（`FullBHandler.post`）：任一列高出本標的 ≥1pp、持股 ≥10 檔時最弱持股高出 ≥1pp、或本標的算不出 → BUY 改 WATCH（`stopped_at_gate`＝G8），不跑 RED_TEAM；差 <1pp 交模型依品質。比較檔缺列只記日誌，不擋。實際效果：B 級標的預期報酬須 ≥ 約 4.4% 才不會輸給國庫券。
  - **排序**：`plan_buys(cap_dir=…)` 以當日報價＋cap_inputs 重算每張候選卡的 G8（`sizing.live_g8`），新舊卡同一層比；沒有 cap_inputs 才用卡上存的值，再沒有才退回 price_to_conservative。
  - **HOLD_REVIEW**：沙箱也放比較檔（本持股在 `self`），提示 Q3「明顯更好的用途」只能用這份數字比、差 <1pp 不算。
  - 規則文件：SKILL.md G8 行、procedure/06 §2、procedure/08 資料表已註明框架給數字與推論參數。
- **PRICE 加碼補 G8 檢查**（同日，測試 772 個全過）：`review_jobs._g8_check_add` 用本次 run 的比較檔（排除本標的）與 FULL_B 同一判準（`g8_verdict`），明顯輸 → ADD 改 HOLD 並記理由；run 沒有比較檔只記錄不擋。PRICE 的新部位 BUY 本來就升級為完整分析（走 FULL_B 的 G8），不需另補。
- **仍未做**：相關群上限（P-31／P-32）；沒用真模型跑過、帳上 0 檔持股。
