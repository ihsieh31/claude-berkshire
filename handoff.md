# 交接（handoff.md）

> 最後更新：2026-10-21。新 session 先讀本檔；完整歷史（各輪修復清單、設計決策 D1–D99、實測紀錄）在 [`docs/archive/handoff-2026-10-21.md`](docs/archive/handoff-2026-10-21.md)，只供追溯，裡面的舊指令不得照做。
> 維護規則：本檔只寫**現況**。每次 session 結束時整份覆寫，舊內容移到 `docs/archive/`，不在這裡追加流水帳。

## 現況

- 程式：main 已含第三輪（A3）全部修復，以及 2026-10-21 合併後的補修（測試環境隔離、A3-M1E-01 SEC 封鎖時初篩失敗、文件鍵名）。
- 測試：`cd trader && python3 -m pytest -q` → 3205 passed、2 failed、4 skipped（本機 A 機）。2 個失敗是環境差異，不是程式錯：
  - `test_fix_P35`：需要 B 機專用的 `hark/` 工具（刻意不進 repo）。
  - `test_fix_RM1_5`：讀本機 `state/` 的凍結 P/E 檔，A 機是 10-16 凍結，測試寫死 B 機的 10-15。
- 資料：A 機 `state/` 只有 2026-10-15 的舊樣本（15 筆決策、0 持股、0 委託），schema v2；新程式第一次連線時自動升到 v3（只新增 `decisions.provenance` 欄）。
- 排程：未安裝（使用者 2026-10-17 指示先不裝）。

## 禁止事項

- 不得對現行 `state/` 跑 `trader analyze`／`bootstrap`／`daily`／`run-approved`，等 r3 換機後再說。
- r1、r2（B 機 2026-10-18 的兩批結果）只當樣本：不換到 A 機、不當結論引用、不進 X5。r2 樣本包 `berkshire_state_B_20261018_r2.tar.gz`（＋`.sha256`）不得覆蓋或刪除。
- B 機專用的隔離手段（`hark/`、`hark_env.sh`、`config.hark.yaml`）不得進 `trader/`、`routing/`、skill，上傳或產 patch 時排除。

## 下一步：r3

1. **名單與 run-id**：待使用者決定（使用者 2026-10-21：先處理完其他問題再說）。預定沿用 r2 的 15 檔：HIG PHM BBY WTW ULTA SPG HPQ GL FDX ALL UHS DHI LOW CF HON。定案後寫進本節，並改《換機匯出與封存程序.md》§1 第 4 步的名單。
2. **前置**：
   - `exclusions.csv`、`universe_override.txt`：已答（使用者 2026-10-21），A 機沒有這兩個檔，不建立、不複製；r3 用完整 S&P 500、不做人工排除。
   - `cik_predecessors.json`、`share_class_ratios.csv`：由 Hark 對照 S&P 500／SEC 補齊。
   - inputs 沒有 `features.csv`，抓資料要加 `--force`。
3. **流程**（使用者 2026-10-18 ASK-2＝A）：
   1. B 機（Hark）跑 r3 → QA。
   2. B 機匯出：每筆決策／訊號卡／登錄表標 `origin=hark_b`，`provenance` 記 origin／backend／effort／價格來源（D51）。B 機產生的 BUY／ADD 在匯出副本降為 WATCH、保留原訊號（D89，由 B 機匯出工具處理，不在本 repo）。
   3. A 機照《換機匯出與封存程序.md》整包替換，直接當初始資料；A 機舊樣本照 §3.3 封存。
   4. **換機後檢查**：匯出包裡 B 機的 BUY／ADD 是否都已降為 WATCH。daily 不看 origin，漏降會直接下單。
   5. 被降級的檔**不會自動重跑**，要在 A 機手動跑：`trader analyze --asof <日期> --tickers <代號> --run-id <新 run-id>`，仍判 BUY 才可下單。
   6. B 機的 REJECT／TOO_HARD 不進 X5 的 12 個月排除，下季初篩再選到就在 A 機重新完整分析。
4. **之後**：排程分析一律在 A 機用 `claude_code`；任何 BUY 下單前在 A 機複核。

## 兩台機器

| | A 機（使用者電腦） | B 機（Hark） |
|---|---|---|
| 用途 | 正式資料、排程、紙上帳本 | 一次性重跑 r3 |
| 模型後端 | `claude_code` | `hark_queue` |
| effort | `medium` | `max`（D51 明確例外，不要互相對齊；以 `provenance` 分辨） |
| 研究取價 | Alpaca | Yahoo |

換機前兩台程式必須一致（《換機匯出與封存程序.md》§0）。claude_code 沙箱的金鑰外洩風險（M5-D8-01／02）已修：每次呼叫前清沙箱 `.claude/`、`--settings` 帶 `disableAllHooks`、子程序剔除秘密環境變數。

## 仍有效的使用者決策

- **2026-10-18 09:42**：回購前半只給粗分級（回購／業主盈餘級距、淨股數變化級距），精確金額與股數到 PRICE 可見階段才給；價格盲工作期間，含價格的檔案不得留在模型可讀位置。
- **2026-10-18 12:14 ASK-1＝B**：自動買進上限＝min(P2′, k × IV_base)，k：A 級 0.95、B 級 0.85（`valuation.cap_discount_k`）；A3 起再乘 0.95（D90），即 `auto_buy_cap = 0.95 × min(P2′, k × IV_base)`。
- **2026-10-18 12:14 ASK-2＝A**：見上方 r3 流程。
- **修復政策**：審查結果 CONFIRMED 全修、DUP 隨主項修、REJECTED／OVERENGINEERED 不修、不過度工程化；DESIGN 題由使用者決定。

## 使用者的要求（必守）

- 回覆用繁體中文。
- Claude 額度吃緊：耗額度的工作先估算；子代理預設 haiku，每個控制在約 100k 上下文。
- 秘密只放 `trader/.env` 或環境變數，設定檔只存變數名稱；程式不得寫死 email 或金鑰；不得把使用者 email 送到無關服務。
- skill：還原選股邏輯，不模仿說話語氣，不用歷史個案舉例；嚴謹，因為會用於金錢交易。
- 沒問題就說沒有，不要過度工程化。
- 做完要檢核：子代理寫完後，主代理逐檔讀程式、找邏輯錯誤、補測試。

## 人工操作注意

- **作廢錯誤決策後重跑**：`trader decisions void <decision_id…> --note 理由`（只標記作廢、取消它的未成交委託），再用**新的** `--run-id` 重跑 `trader analyze`；同一 run-id 重跑不支援，舊 run 殘留工作用 `jobs abandon`。作廢不還原已回寫進登錄表的價格線／等級，由重跑的新決策覆寫。細節見《換機匯出與封存程序.md》§4.1。
- **人工暫停期間的系統賣單**：`ledger pause` 期間買進與 TRIM 減碼拒絕下單，解除後不會自動重下，需要時用 `trader trade sell --ticker T --qty N --note …` 人工補下；SELL_ALL 全清照建委託、撮合時擋，解除（`ledger adjust --unpause T`／`--unpause-all`）後次日自動撮合，**不要另補賣單**（會重複賣）。

## 刻意不修／後續觀察

- A3-M1B-04（D96）：初篩對 MISSING_DATA 不套節流；r3 後視重排頻率再議。
- `_kill_criteria` 的估值型失效條件目前在 THESIS 可見，是否要從 THESIS 輸入移出，建議另立一題。
- 紅隊判「數字錯」時訊號仍保留 PASS_ALL 的缺口，建議另立一題。
