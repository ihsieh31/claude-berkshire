# M7 上線：bootstrap 與常駐排程（2026-10-16）

使用者指示：**沒有 3 個月期限**——只要沒有強制中斷就一直跑。「期滿出檢討報告」改為每月自動出一份檢討報告。

## bootstrap

`trader bootstrap --asof D [--tickers X,Y] [--top 15] [--dry-run]`（`pipelines/bootstrap.py`）
- 初選（`run_screen`，整個 S&P 500，會順便凍結當月 P/E 中位）→ 依 skip_rule 得 `to_analyze` → `run_analyze`（每檔 DOSSIER → FULL_A → FULL_B →（擬 BUY）RED_TEAM）。
- `--tickers` 給定則略過初選直接分析。`--dry-run` 只跑初選並列出將分析的代號，不建分析工作、不呼叫模型。
- FULL_A／RED_TEAM 照 M6 同意閘門停在「待同意」，由儀表板或 `trader approvals` 放行；DOSSIER（弱）會直接執行。
- 每季初選也用同一命令（排程呼叫）。

## 常駐排程

`trader/scheduler.py`；`trader scheduler run|once|install|uninstall|status|reset [任務]|print [--cron]`。
（P49 依 H1／M7-D2-02、D08、D09、D10、M8-D5-04、D44 同步；細節以 scheduler.py 模組說明為準。）
- **啟動前提**：`install` 嚴格——登錄表非空，且沒有 pending／deferred 的同意項目、沒有 awaiting_approval 的工作（`bootstrap_block`）。
  `run`／`once`（與每次 tick）只擋登錄表為空（`registry_block`）；待同意不得讓 daily／撮合／淨值／賣出停擺。
- **暫緩（[WAIT]）**：有待同意項目或更早的 daily 未完成時，只暫緩會用「今天」產生新分析的觀察名單與季度初選（[WAIT] 同一原因一天記一次）；
  daily、freeze、report 照跑。run-approved 只在所有 daily 都補完後才排。已同意（approved）不算忙碌，由排程跑 `run-approved`。
- **run-approved 的範圍**（M8-D5-04、D44）：只跑 `approvals.runnable` 的列——只剩 failed 工作的已同意列不排（等 `jobs retry`／`jobs abandon`）；
  runs.status＝paused_budget（預算暫停）的 run 不由「執行已同意」、排程 approved 任務或執行中途的新同意續跑，只能由使用者在儀表板按「續跑」或 `trader jobs resume --run-id R`。
- **上次初選日**（D09）：scheduler.json 的 `last_screen` 與 runs 表「已完成且有 analyze run 的 screen-*」取較晚者。兩者都沒有時不自動初選，
  記 [WAIT] 與一筆 `scheduler_no_last_screen` 異常；`trader bootstrap --asof D`（非 dry-run、未指定 --tickers）初選完成後寫入 last_screen，
  換機重做同一批用 `bootstrap --tickers … --mark-screened`。初選完成但分析中斷時改排 `analyze --asof D` 續跑；analyze-D 為 paused_budget 時不排（D44，記一次 [WAIT]）。
- `install`：launchd 使用者代理 `local.berkshire.scheduler`（開機自動啟動、KeepAlive）。**沒有結束日**；停止只能 `trader scheduler uninstall`（或前景 `run` 時 Ctrl-C）。
- 每 60 秒判斷一次，以紐約時間計。每個交易日 `schedule.daily_close`（17:30）之後依序：
  1. 每月凍結 P/E 中位：當月第 `monthly_freeze` 日之後、當月尚未凍結 → `fetch --asof D --freeze-pe`（當天要初選時略過，由初選凍結）。
  2. 每日收盤 `daily --date D`（公司行動、撮合、持股觸發檢查、組合計畫、淨值）。錯過的交易日依序補跑（一次只排最早未完成的一天），最早補到 `start_date`。成功判準：`runs` 表 `daily-D` 為 done。
  3. 已同意的強模型工作 → `run-approved`（見上）。
  4. 觀察名單：距上次 ≥ 14 天（weekly＝7）→ `review --scope watch --asof D`。
  5. 每季初選＋深度分析：`quarterly_screen` 日期（MM-DD）晚於上次初選日且已過 → `bootstrap --asof D`（任務鍵 `screen:季度日`）。
  6. 每月檢討報告：進入新月份且淨值 ≥ 2 天 → `validate-report --out state/reports/monthly`。
- 每項以子行程執行，輸出 `state/logs/scheduler-tasks.log`；排程本身日誌 `state/logs/scheduler.log`。
- **失敗**：30 分鐘後重試。
  - daily **永不放棄**（D08）：連續 3 次失敗後改成每次新收盤後重試一次（仍依日期順序），並記一筆 `ledger_stalled` 異常（pause_trading＝0，儀表板置頂；未結不重寫，該日 daily 成功即結案）。
  - 其他工作連續 3 次失敗即放棄，記 `scheduler_task_failed`（不暫停交易），其他工作照常。
  - `trader scheduler reset [任務]` 清除失敗次數、重試等待與放棄狀態（含 analyze 續跑標記）。
  - tick 本身連續例外 30 次 → 記一筆 `scheduler_error`（M8-D4-05）。
- daily 成功後寫一份帳本快照到 `state/backups/`（失敗只記錄）。
- 狀態檔 `state/scheduler.json`：`start_date`、`last_watch_review`、`last_screen`、`last_report_month`、`reanalyze`、`failures`、`retry_at`、`gave_up`、`wait_logged`、`last_tick`、`calendar_source`（日曆退回快取／規則時才有，M7-D5-08）。
- 鎖（D10）：`state/.scheduler.lock`（同時只一個排程）；執行工作時（等待）持有 `state/.pipeline.lock`，子行程不再取鎖。
  手動的寫入指令（analyze／review／daily／bootstrap／screen／run-approved／jobs retry…）由 cli 非阻塞取同一把鎖，取不到就提示持鎖者並 exit 2。
- `print --cron`：給 Linux 用的 crontab 行（每 10 分鐘 `scheduler once`）。

## 委託與撮合口徑（P27：設計決策 D03 撮合部分、D04、D05、D06、D07、D15、D50）

- **買單限價**（D04）：限價＝round_down(auto_buy_cap × 0.9999)，嚴格低於上限；收盤只用來算股數（股數＝金額 ÷ 收盤×(1＋滑價)）。下單前「收盤×(1＋滑價) < 上限」的自檢照舊。
- **建倉窗口與委託期限**（D04）：建倉窗口自 `build_plan.started`（首批下單日；舊資料缺時用首次建倉日）起算；買單期限自建單日起算。**兩者都以 21 曆日近似 15 個交易日**（忽略假日，屬近似）。
- **成交價**（D07，紙上交易近似）：典型價 TP＝(高＋低＋收)÷3。市價單 TP±滑價；限價買在最低價 ≤ 限價時成交，價＝min(TP×(1＋滑價), 限價)；限價賣在最高價 ≥ 限價時成交，價＝max(TP×(1−滑價), 限價)。滑價加在 TP 上再以限價夾住屬推論（保守方向）。
- **估值類 TRIM**（D03 撮合部分）：HOLD_REVIEW 的估值 TRIM 掛限價＝觸發線（登錄表 last_trim.line：荒謬級線或 IV_base），當日有效：撮合日最高價未達線即作廢（回落即停），之後由觸發檢查重新判斷；業務類 SELL_ALL 為隔日市價。沒賣成（作廢、撮合前重核取消、下單被拒、計畫股數 0）時，回寫記下的「預計」last_trim（同決策日）標 `unfilled`（原因）、`shares`＝0，峰值與觸發線不動；標了 unfilled／shares＝0 的 last_trim 不算減過，不觸發「荒謬級回到線下前只減一次」（review_due 與 writeback._prior_trim 同口徑，A3-M7A-02）。同一決策已有 open／filled 委託（同日重跑）時不標（A3-M7A-03）。
- **撮合前重核**（D05）：`broker.recheck_pending` 在每日撮合前以前一交易日收盤重核：冷靜單（PRICE 觸發的 BUY／ADD）須收盤×(1＋滑價) < 現行上限；PRICE 觸發（trigger.full_stage＝PRICE）的 HOLD_REVIEW 估值 TRIM 也冷靜一個交易日（D82），以冷靜日收盤照下述估值 TRIM 口徑重核；估值 TRIM（含等同意數日後才完成者）須收盤 ≥ 觸發線；未成交買單另跑 `revalidate_open_buys`（上限、委託後結論、停止加碼）。不符即取消並寫理由。建倉批次（review_type＝TRANCHE）不套冷靜期。
- **暫停交易**（D06）：公司行動待人工的暫停只擋買進與部分減碼；SELL_ALL 照常下單撮合（賣帳上全部股數），分拆新得、未入帳的股票仍由人工處理。
- **現金水位**（D15）：新買可動用現金＝現金 − 計畫水位（5% × `account.cash_plan_buffer`，預設 1.2＝6%）− 已承諾買單；5% 是硬下限；只因持股上漲跌破時只停新買、不賣。
- **觸發檢查失敗**（D50）：只停失敗工作所屬那幾檔的新買與加碼；觸發檢查整體出錯、CORP_ACTION 工作失敗、失敗無法歸屬單檔，或當日失敗檔數 ≥ 3 → 停全組合。同檔連 3 次 daily 失敗記異常 `trigger_fail_streak`（不暫停）。
- **申報清單抓取失敗**（M2-RS8-03，`pipelines/review.py`）：單檔 SEC 申報清單抓不到（含只有舊快取）→ 以 null 交 review_due，只略過申報類觸發，價格類、滿 12 個月等其餘觸發照建工作，並記 summary.skipped。登錄表 `filings_fail`：`count`（連續失敗的觸發檢查次數，同一決策日重跑不重複計）、`since`（首次失敗時的 last_review_date＝申報窗口起點）、`last`（最近失敗的決策日）。恢復成功（資料層取得申報清單，且 review_due 未再回報該檔 filings_ok=false；後者仍算失敗、照上述計次）後，review_due 的 last_review_date 退回 `since`（不早於上次完整分析日）補查窗口；`since` 無效（失敗時登錄表缺 last_review_date）則不放寬窗口，但照樣算恢復。恢復的檔在本 run 無工作、或工作皆已處理（done 或 conservative_fallback，兩者都推進 last_review_date）→ `filings_fail` 清為 null；有未完成或失敗的工作則保留，下次再補查。`trigger_fail_streak` 異常的第二個來源：同檔 `filings_fail.count` ≥ 3（`FILINGS_FAIL_STREAK`）時開一筆（不暫停；已有 open 者不重開）。

## 測試

`tests/test_scheduler.py`：bootstrap 前拒絕、待同意不停 daily 但暫緩觀察與初選、收盤前不做、睡眠後補跑、每月凍結、兩週觀察、每季初選、月報、超過 3 個月照跑、重試與放棄記異常、daily 回 0 但未完成算失敗、bootstrap 串接；daily 永不放棄與 reset 見 `tests/test_fix_P23.py`。
