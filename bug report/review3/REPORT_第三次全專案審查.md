# 第三次全專案審查（2026-10-17，初始 15 檔分析完成後）

範圍：資料完整性、`trader/` 全部主要執行路徑（帳本、撮合、sizing、daily、review、scheduler、engine、approvals、writeback、checks、corp_actions、screen）、兩個 skill 的腳本（cap_calc、review_due、gate3_check、screen、screen_quant）。
方法：讀程式、對真實 state 跑唯讀檢查、對真實 Alpaca／SEC／FRED 跑 `daily --dry-run` 與 `review --due-only`、用專案自己的測試夾具寫重現。**本次沒有修改任何專案程式碼**；唯一改動的資料是先前已報告的 PHM／OTIS 補寫回與 MO 重做。
沒讀完的部分：`dashboard/web` 前端、`report/metrics.py`／`validation.py`、`data/xbrl_shares.py`、`data/sec.py` 的細節、Swift 小工具。這些沿用先前審查（`bug report/` 下四份）的結論，沒有重新驗證。

## 結論

- 測試 708 個全過（Python 3.13 與 3.14 各跑一次）；SQLite `integrity_check` 為 ok。
- **有 2 個高嚴重度問題，都在「裝排程之後、有持股之後」才會咬人，目前零持股所以還沒造成損失。**
  - H1：排程只要有任何一檔「待同意」就整個停擺，包含每日收盤。
  - H2：公司行動（拆股、股利）的模型工作失敗時，事件被永久漏掉，沒有任何警示。
- 資料大致齊全，但有 4 處小缺口（見 §2）。
- 分析結果本身合理，但**整條流程幾乎不可能產生 BUY**（見 M4），這是設計層面要你決定的事。

## 1. 發現

### H1（高）排程遇到任何待同意項目就整個停擺
- 位置：[scheduler.py:245-247](../../trader/trader/scheduler.py)（`tick()` 第一行呼叫 `check_ready()`）、[scheduler.py:56-68](../../trader/trader/scheduler.py)（`bootstrap_block`）。
- 原因：`bootstrap_block` 把「有 pending／deferred／approved 的同意項目」或「有 pending／running／awaiting_approval 的工作」都視為「初始 bootstrap 未完成」。但 `tick()` 每一輪都先呼叫它，所以任何時候只要有一檔 FULL_A 等同意，`tick()` 直接丟 `NotBootstrapped`，後面的 daily、watch、report、freeze 全部不跑。
- 重現：[repro_H1_scheduler_halts.py](repro_H1_scheduler_halts.py)。沒有待同意時一輪排了 4 個工作；加一筆 pending 同意後，runner 呼叫數 0。
- 後果：
  - 每季初選（02-20、05-20、08-20、11-20）會產生最多 15 筆待同意；你不處理，每日收盤、撮合、賣出、淨值都停。
  - 持股觸發檢查本身升級出的 FULL_A 也一樣會卡住排程。
  - 補跑機制會在解除後補回錯過的交易日，但這段期間的賣出與停損類動作延遲。
  - 與設計文件不一致：`daily` 的註解寫「等待同意視為複查成功」，M6 契約寫「等待期間只暫停該檔加碼」。
- 建議：啟動前提只在「登錄表為空」時擋；執行中不要因為待同意而停止 daily／report／freeze，只讓 `screen`／`watch` 這類會新增 opus 工作的項目在還有未處理同意時暫緩。

### H2（高）公司行動工作失敗 → 拆股被永久漏掉，且沒有任何異常紀錄
- 位置：[daily.py:197-215](../../trader/trader/pipelines/daily.py)（`out["ca_engine"] = engine.run(run_id)` 之後沒有檢查失敗）、[corp_actions.py:218-255](../../trader/trader/ledger/corp_actions.py)（`detect` 只看 `(prev_date, date]`）、[engine.py](../../trader/trader/jobs/engine.py)（`failed` 是終態，重跑不會再執行）。
- 重現：[repro_H2_corp_action_lost.py](repro_H2_corp_action_lost.py)。持有 100 股 AAA，ex-date 當天 Alpaca 有 2 拆 1，模型呼叫回傳「usage limit reached」：
  - CORP_ACTION 工作 `failed`，`daily` 的 run 狀態 `done`，異常 0 筆，`corporate_actions` 0 筆。
  - 隔天重跑，`detect` 回傳空清單；持股仍是 100 股、成本 $50，但市價已是 $25。淨值少算一半，之後的賣出、權重、加碼全部以錯誤股數計算。
- 觸發條件很現實：`claude -p` 任何非零結束碼都視為可重試的傳輸錯誤，退避只有 2、4、8 秒（[llm/base.py:100-110](../../trader/trader/llm/base.py)）。訂閱額度用完時不會等到重置。
- 建議：
  1. `run_daily` 檢查 `ca_engine["failed"] > 0`，對該檔寫 `pause_trading=1` 的異常，並讓 daily 狀態變 failed（或至少不標 done）。
  2. 失敗的 CORP_ACTION 保留「未解決事件」，下次 daily 重新偵測，而不是只看昨天到今天。
  3. 額度類錯誤退避拉長，或讓工作留在 pending 而不是 failed。

### M1（中）有持股時，完整分析拿不到現金與已承諾金額　**【已修（2026-10-17）：portfolio_state 補 cash／committed／n_holdings，holdings 附成本、成本權重，不含市價】**
- 位置：[analyze.py:32-37](../../trader/trader/pipelines/analyze.py)。有持股時 `_portfolio_state` 只回傳持股清單，沒有 `cash`、`committed`、`n_holdings`。
- 後果：FULL_B 的 G8（和最弱持股比較）、目標權重與現金下限（5%）無從計算；模型會自己猜。
- 先前審查（`bug report/step`）的發現 ⑧，仍未修；目前零持股不受影響。

### M2（中）沒有發放日的現金股利永遠不入現金　**【已修（2026-10-17）：缺發放日以除息日後 30 天推論並標 pay_date_inferred，舊列同樣處理】**
- 位置：[corp_actions.py:424-433](../../trader/trader/ledger/corp_actions.py)、[corp_actions.py:500-501](../../trader/trader/ledger/corp_actions.py)。`pay_date` 缺（8-K 來源或 Alpaca 沒給）時應收股利永遠留在 `dividend_receivable`。
- 後果：淨值正確（應收有計入），但可用現金少算，會少買或不買。先前審查發現 ⑪，仍未修。

### M3（中）失敗的工作不會被重跑；寫回失敗會丟掉已付費的模型結果　**【已修（2026-10-17）：next_decision_id 檢查 signals；新增 trader jobs retry；限制：不支援只重做後處理，會重呼叫模型】**
- 位置：[engine.py](../../trader/trader/jobs/engine.py)（`run()` 只處理 pending）、`_on_done` 內 `post()` 例外 → `_fail`。
- 實例：本次 PHM（FULL_A）與 OTIS（FULL_B）模型都跑完了，寫回時撞 `signals.decision_id` UNIQUE 失敗，工作變 `failed`，必須手動補寫回。
- 另外：`next_decision_id` 只數 `decisions` 表，不看 `signals`（[writeback.py:105-120](../../trader/trader/analysis/writeback.py)），這就是撞號的原因。
- 建議：加 `trader jobs retry`（或 `run` 時把可重試的 failed 重設為 pending）；`next_decision_id` 同時檢查 `signals`。

### M4（中，設計）流程幾乎不可能產生 BUY　**【部分處理（2026-10-17）：初選營收成長改中性、改計 5 年最差 ROE（未回測，2–3 季後看 G3 命中率再決定是否保留）；FULL_A 新增 moat_evidence，G3 REJECT 記 reject_basis（有反證／證據不足／其他，不改停止碼與 12 個月排除）；寫回改讀 thesis_draft.one_liner 並以 report_A.md 備援，14 筆舊決策已補回理由。2026-10-17 再補資料：Form 4 內部人交易（24 個月股數彙整，不含價格）、財報 8-K 的 EX-99.2 等補充資料／簡報、10-K 的 EX-13 致股東年報、池外 SEC 申報競爭者（同 SIC 大類、名稱 ≥2 字）、衝擊年樣本不足時以 SEC 同 SIC 四碼補樣本。未做：法說會逐字稿（SEC 沒有）、留存率數字（多數公司只有定性描述）；「強」的標準不放寬，待 reject_basis 累積 2–3 季再議】**
- 15 檔的結果：13 檔 REJECT 於 G3（其中 12 檔是「護城河三測少於 2 項強」，SPG 是利息保障與負債比）、1 檔 LOW 為 TOO_HARD、1 檔 OTIS 通過但被降到 C 級，只能 WATCH。
- 原因之一是證據標準：評「強」要有留存率、壓力事件後的使用量、同業比較等數字，但資料包不提供。15 份 FULL_A 的缺資料清單中，**客戶留存／續約率缺 11 份、同業樣本不足缺 9 份、內部人交易（Form 4）缺 5 份、致股東信／法說缺 4 份**。缺資料被當成「不能評強」，是保守設計，但等於把門檻訂在多數公司拿不到的證據上。
- 初選本身偏好「便宜、低成長、低負債」（營收成長越低排越前面，見 `screen.py` 的 `g("rev_cagr_5y", False)`），而閘門 3 要的是強護城河，兩者方向有張力。
- 影響：每季約 $27 的模型花費，預期只有很少的 BUY；帳戶大多數時間是現金，和 SPY 比會長期落後。這不是 bug，但請你知道這是目前設計的結果。
- OTIS 是 C 級，`cap_calc` 與 PRICE 都規定 C 級「最多觀察」，所以即使價格掉到 62.40 以下也不會被買；兩週一次的 PRICE 檢查對它沒有作用，要等 2027-03-31 的下次完整分析。

### M5（中）任何 10-K/A 都會觸發完整重分析　**【已修（2026-10-17）：10-K/A 降為 THESIS，原始 10-K 仍 FULL】**
- 位置：`review_due.py` 的 `check()`：`f["form"] in ("10-K", "10-K/A", ...)` → FULL。很多 10-K/A 只是補封面或 Part III（本次 MO 就是 7.6k 字）。
- 後果：多一次 opus 完整分析待同意；配合 H1 還會讓排程停擺。建議 10-K/A 降為 THESIS 層級，或先看文件長度。

### L1（低）決策紀錄追溯欄位不齊
- 15 筆決策中，BBY、HIG、HPQ、LOW、MAS、WTW 共 6 筆沒有 `skill_hashes` 與 `fy_last_filed`（它們寫入時，框架填欄位的程式還沒上線）。月報用 `skill_hashes` 判斷規則是否變動，這 6 筆會被當成缺值。
- 14 筆被淘汰的登錄表列沒有 `cik` 與 `next_full_review_due`；不影響運作（`review` 會用 SEC 對照補 CIK；淘汰類 12 個月內不重評）。
- 登錄表 `cap_input_file` 存絕對路徑（[writeback.py:312-316](../../trader/trader/analysis/writeback.py)），搬動 state 目錄後該欄失效（目前沒有程式讀它）。

### L2（低）其他　**【已修（2026-10-17）：run 有 failed／skipped 工作時標 failed 並寫 job_failed 異常（只修 run 狀態那項，其餘 L2 子項未動）】**
- `analyze-2026-10-15` 這個 run 的狀態是 `done`，但有 2 個 failed 工作；儀表板看不出來。
- IEX 行情的成交量只有實際的一小部分（OTIS 日均成交額 IEX 約 $15M），所以 ADV 上限（成交額的 1%）被低估 10 倍以上。以 $100,000 帳戶的單批約 $5,000 來說不會卡到，帳戶變大才會。
- 儀表板首頁把權杖內嵌在 HTML，本機任何行程用 `Host: 127.0.0.1:8787` 就能取得並按同意；網頁、DNS rebinding 都擋住了，只有本機其他行程有這個風險。
- 價格隔離的兩個沙箱（價格可見的 FULL_B 與其他票的 DOSSIER／FULL_A）在強模型併發 1、弱模型併發 4 時可同時存在，理論上 DOSSIER 可讀到兄弟沙箱的價格檔。實際機率低；先前已用「看價格工作結束就刪沙箱」緩解。
- `launchd` 啟動的 `claude -p` 是否能取得登入憑證沒有驗證過（我只在互動環境跑過）。裝排程後第一次要觀察 `state/logs/scheduler-tasks.log`。

## 2. 資料完整性盤點

| 項目 | 狀態 |
|---|---|
| `registry`／`decisions`／`signals` | 各 15 筆，一一對應 |
| `jobs` | 31 筆；done 29、failed 2（PHM FULL_A、OTIS FULL_B，已手動補寫回） |
| `approvals` | 15 筆全部 done |
| `orders`／`fills`／`positions`／`cash_ledger`／`nav_daily`／`corporate_actions`／`anomalies` | 0 筆（符合零持股、排程未啟動） |
| 論點檔、`cap_inputs` | 只有 OTIS（其餘停在 G1／G3，不產生；符合設計） |
| 價格隔離 | 15 份 FULL_A 提示與資料包抽查，沒有價格或排名欄位 |
| 即時連線 | `daily --dry-run`、`review --due-only` 對真實 Alpaca／SEC／FRED 都正常，沒有寫入任何資料 |
| 磁碟 | 可用 28 GiB（87% 已用）；`state/cache` 2.2 GB（SEC 快取），之後會增長 |
| 備份 | `state/trader.db.bak-*` 共 4 份，都在 state 內，沒有異地備份 |

## 3. 查過、沒有問題的部分

- `cap_calc.py`：三情境、P2′、一致性條款、歷史成長 ≤0 的 IV0 上限、結論測試、金融類上限都和文件一致；沒有找到公式錯誤。
- `gate3_check.py`：失敗關閉（缺答、缺證據、前視、價格先看都不通過）。
- 帳本與撮合：價格以萬分之一美元記帳、金額以分計算；買單成交價不會高於限價；現金不足時降股數；整批在單一交易內，失敗回滾。
- `sizing.py`：三閘門、5% 現金下限、單日新部位 4 檔、持股 10 檔上限、1% ADV、權重 15% 不加碼、冷靜期，邏輯與註解一致。
- `daily.py`：交易日判斷、前一／下一交易日、淨值、同日重跑 upsert、持股不在登錄表會記異常。
- 儀表板：Host／Origin／權杖／Content-Type 檢查齊全。
- 先前審查標示已修的項目，抽查 A3（SPY 股利鍵）、A2（IV0 上限）、B2（FULL_B 核閘門 2）、N1（PRICE 判 BUY 升級完整分析）、D4（cap_input 範圍檢查）都在程式裡。

## 4. 建議處理順序

1. **H1、H2 先修，再裝排程。** 這兩個不修，第一次遇到待同意或額度用完就會出問題。
2. M3（可重跑失敗工作、`next_decision_id` 檢查 signals）和 M5（10-K/A）一起做，工作量小。
3. M1、M2 在第一筆真實持股出現之前修掉。
4. M4 需要你決定：要不要補資料（Form 4、法說、留存率）、放寬「強」的證據標準，或接受目前「多半空手」的結果。

## 5. 重現檔

- [repro_H1_scheduler_halts.py](repro_H1_scheduler_halts.py)：`python3 repro_H1_scheduler_halts.py`
- [repro_H2_corp_action_lost.py](repro_H2_corp_action_lost.py)：`python3 -m pytest -s repro_H2_corp_action_lost.py`（用專案自己的 `tests/` 夾具，路徑寫死在檔案開頭）
