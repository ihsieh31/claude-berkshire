# M7 上線：bootstrap 與常駐排程（2026-10-16）

使用者指示：**沒有 3 個月期限**——只要沒有強制中斷就一直跑。「期滿出檢討報告」改為每月自動出一份檢討報告。

## bootstrap

`trader bootstrap --asof D [--tickers X,Y] [--top 15] [--dry-run]`（`pipelines/bootstrap.py`）
- 初選（`run_screen`，整個 S&P 500，會順便凍結當月 P/E 中位）→ 依 skip_rule 得 `to_analyze` → `run_analyze`（每檔 DOSSIER → FULL_A → FULL_B →（擬 BUY）RED_TEAM）。
- `--tickers` 給定則略過初選直接分析。`--dry-run` 只跑初選並列出將分析的代號，不建分析工作、不呼叫模型。
- FULL_A／RED_TEAM 照 M6 同意閘門停在「待同意」，由儀表板或 `trader approvals` 放行；DOSSIER（弱）會直接執行。
- 每季初選也用同一命令（排程呼叫）。

## 常駐排程

`trader/scheduler.py`；`trader scheduler run|once|install|uninstall|status|print [--cron]`。
- **啟動前提**：初始 bootstrap 已完成——登錄表非空，且沒有 pending／deferred／approved 的同意項目、沒有 pending／running／awaiting_approval 的工作；否則 `install|run|once` 拒絕（常駐中途不符則記 [WAIT] 不做事）。`trader bootstrap`（非 dry-run、未指定 --tickers）完成後把 asof 寫入 `last_screen`。
- `install`：launchd 使用者代理 `local.berkshire.scheduler`（開機自動啟動、KeepAlive）。**沒有結束日**；停止只能 `trader scheduler uninstall`（或前景 `run` 時 Ctrl-C）。
- 每 60 秒判斷一次，以紐約時間計。每個交易日 `schedule.daily_close`（17:30）之後依序：
  1. 每月凍結 P/E 中位：當月第 `monthly_freeze` 日之後、當月尚未凍結 → `fetch --asof D --freeze-pe`（當天要初選時略過，由初選凍結）。
  2. 每日收盤 `daily --date D`（公司行動、撮合、持股觸發檢查、組合計畫、淨值）。錯過的交易日（關機、睡眠）醒來後依序補跑，最早補到排程第一次啟動日 `start_date`。成功判準：`runs` 表 `daily-D` 為 done。
  3. 觀察名單：距上次 ≥ 14 天 → `review --scope watch --asof D`。
  4. 每季初選＋深度分析：`quarterly_screen` 日期（MM-DD）晚於 `last_screen` 且已過 → `bootstrap --asof D`。
  5. 每月檢討報告：進入新月份且淨值 ≥ 2 天 → `validate-report --out state/reports/monthly`。
- 每項以子行程執行，輸出 `state/logs/scheduler-tasks.log`；排程本身日誌 `state/logs/scheduler.log`。
- 失敗：30 分鐘後重試；連續 3 次失敗即放棄該項，記異常 `scheduler_task_failed`（不暫停交易），其他工作照常。
- 狀態檔 `state/scheduler.json`：`start_date`、`last_watch_review`、`last_screen`、`last_report_month`、`failures`、`retry_at`、`gave_up`、`last_tick`。
- 鎖：`state/.scheduler.lock`（同時只一個排程）；執行工作時持有 `state/.pipeline.lock`，`run-approved` 進入時也會等這把鎖 → 排程工作與同意後的強模型工作不會同時寫帳本。
- `print --cron`：給 Linux 用的 crontab 行（每 10 分鐘 `scheduler once`）。

## 測試

`tests/test_scheduler.py`：收盤前不做、首次啟動即 bootstrap、睡眠後補跑、每月凍結、兩週觀察、每季初選、月報、超過 3 個月照跑、重試與放棄記異常、daily 回 0 但未完成算失敗、bootstrap 串接。
