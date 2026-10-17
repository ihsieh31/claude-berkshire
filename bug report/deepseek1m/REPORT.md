# 對抗式審查報告（第二版：已逐條複驗）

> 審查日期：2026-10-17　受審版本：`/Users/zongen/Downloads/claude code/berkshire`（M0–M7 完成、656 測試）
> 審查方式：全專案逐檔閱讀（trader 約 24k 行、skill 約 13k 行）＋ 第一性原理拆解 ＋ 13 支可重現探針 ＋ **1 次真實模型呼叫**
> 本報告**未對專案做任何修改**；所有測試程式與暫存 state 都在 `bug report/deepseek1m/`。

---

## 0. 這一版改了什麼

第一版提出 7 個 P0、8 個 P1、9 個 P2、12 個 P3。逐條複驗後：

| 處置 | 數量 | 內容 |
|---|---|---|
| 證實 | 8 | 其中 1 項用真實 `claude -p` 實測（價格隔離可繞過） |
| 降級 | 4 | 兩個 P0 降為 P1（批次扣帳、同日公司行動）、兩個 P1 降為 P2（P2′ 不生效、C 級未擋） |
| 撤回 | 2 | 基準股利重複計入（錯）、adjust 入金算成報酬（是文件明示的設計） |
| 修正措辭 | 2 | 「等級不影響買價」是設計不是 bug；「低利率時買價更低」只對高成長股成立 |
| 新增 | 1 | 賣出＋買進同輪撮合，成交結果取決於代號字母順序 |

**最終分級：5 個 P0、9 個 P1、11 個 P2、9 個 P3。**

一句話結論不變，但理由更精確：**這套系統的核心保證「規則以程式保證、不靠模型自律」在「模型 → 參數」這個介面上沒有成立**——模型仍然決定買價上限、部位參數、登錄表狀態與「我沒看過價格」的自述，而程式的覆核只覆核了模型自己的算術。加上 G4 的警訊設計與買價上限的結構，這套系統在目前參數下**幾乎不可能買進任何一檔大型股**。

---

## 0.5 複驗方法與結果

複驗分三層，全部可重跑：

| 層次 | 工具 | 內容 |
|---|---|---|
| 真實模型 | `verify_sandbox_live.py` | 用與 `ClaudeCodeRunner` 完全相同的旗標跑一次 haiku，測試能否讀到隔壁沙箱的價格檔 |
| 行為探針 | `probe_bugs.py`（10 項） | 建臨時 DB、跑真實函式，斷言結果 |
| 逐條複驗 | `verify_issues.py`（10 項） | 對第一版的每個指控做「證實／誇大／推翻」判定 |

### 0.5.1 真實模型驗證：價格隔離確實可繞過（P0-1）

```
deny 規則 30 條；沙箱根是否被 deny：False
exit 0
result: {"sibling": "{\"ticker\": \"OTIS\", \"price\": 70.39, \"auto_buy_cap\": 49.29}",
         "project": "DENIED"}
cost_usd: 0.00173225
```

- `sibling`（`../../OTIS/_FULL_B_1/dossier/OTIS_price.json`）**讀到了**：`price 70.39`、`auto_buy_cap 49.29`。
- `project`（`.../berkshire/trader/config.yaml`）**被 DENIED**——證明 deny 機制本身有效，只是**沒有涵蓋沙箱根目錄**。

這是本報告唯一一次呼叫模型（haiku、`--effort low`、$0.0017），因為這是唯一需要實證而非推論的項目。

### 0.5.2 逐條判定

| # | 第一版主張 | 判定 | 依據 |
|---|---|---|---|
| P0-1 | 沙箱根殘留可讀到舊價格檔 | **證實（實測）** | 上節 |
| P0-2 | gate3 的價格隔離是模型自述 | **證實** | `FULL_A.md:11` 明令填 false；`gate3_check` 不讀框架寫的時間戳 |
| P0-3 | `cap_input.json` 由模型寫，覆核只覆核算術 | **證實** | `deep.py:346` 讀模型檔；OE 無程式實作 |
| P0-4 | `registry_entry.json` 無驗證，模型決定加碼上限 | **證實** | 訊號卡 60／登錄表 180 |
| P0-5 | `adjust` 歸零不清未成交買單 | **證實** | 次日以 65.065 買回 10 股 |
| P0-6 | 未成交批次被扣掉 | **證實，但降為 P1** | 影響是部位偏小，不是錯誤交易 |
| P0-7 | 同日第二種公司行動被丟棄 | **證實，但降為 P1** | 頻率低；後果是漏記而非錯帳 |
| P1-1 | 利率對買價無影響、且低利率買價更低 | **部分修正** | 平坦是事實；「更低」只對 P2′／高成長股成立，見 §3.4 |
| P1-2 | P2′ 幾乎不生效 | **證實，降為 P2** | 這是冗餘設計，不是錯誤 |
| P1-3 | 歷史成長 ≤0 的規則未實作 | **證實** | 51.15 vs 規則 37.5 |
| P1-4 | C 級 BUY 未被框架擋 | **證實，降為 P2** | 結果仍不會買（sizing 歸零），只是多一筆待同意 |
| P1-5 | 現金不足的 BUY 永久消失 | **證實** | 決策窗口 `(prev, date]` 不含隔日 |
| P1-6 | 滿 10 檔後永久不再買 | **證實（設計限制）** | `sizing.py:325` |
| P1-7 | 排程逐一補跑錯過的交易日 | **證實** | `scheduler.py:186` |
| P1-8 | `bootstrap_block` 被全域 job 卡死 | **證實** | 查詢未帶 run_id；無取消命令 |
| P1-9 | claude_code 模式沒有 100k 上下文控制 | **證實，降為 P2** | 量的是檔名清單；成本影響真實但屬已知 |
| P2-3 | `portfolio_state` 缺現金／檔數 | **證實** | 有持股時只剩 `holdings` |
| P2-7 | 觸發路徑的 gate1 永遠通過 | **證實（影響小）** | 初篩刪除類本來就不會進 WATCH |
| P3-5 | 基準股利補跑重複計入 | **推翻** | 每次重算是冪等；`nav_daily` 是 upsert |
| P3-10 | adjust 入金被算成報酬 | **推翻** | `metrics.py` 開頭明寫「adjust 視為帳戶內損益」 |
| P3-12 | deny 規則的路徑轉義問題 | **撤回** | 未實測，屬臆測 |

### 0.5.3 我哪裡過度解讀

1. **「等級不影響買價 = 安全邊際不存在」**——錯。SKILL §3 把等級係數定義在 `P1`（安全邊際欄與 G8），不進硬上限；安全邊際由部位大小（A 15%／B 10%）與 G8 體現。實測 A/B/C 的 `auto_buy_cap` 都是 82.51，但 `P1` 是 54.92／48.46／25.85——**係數有在用，只是不用在買價上**。這是設計選擇，我把它寫成 bug 是過度。
2. **「8-K 5.02 直接排 opus」**——不精確。DOSSIER（haiku，$0.15–0.56）會自動跑，FULL_A 停在**待同意**；真正的 opus 花費要使用者按下去才發生。問題是「每週多幾次同意」與「不必要的 DOSSIER」，不是自動作業扣款。
3. **「C 級 BUY 會進 RED_TEAM 燒 opus」**——同樣不精確。RED_TEAM 是 strong，會先停在待同意。
4. **「P2′ 幾乎不生效」說成 P1**——它是冗餘，不是錯誤；降為 P2。

---

## 1. 第一性原理拆解：這個系統到底是什麼

把整個系統看成七個轉換，每一個都是必須可信的介面：

```
① 世界 → 資料     Alpaca 日線/公司行動/新聞、SEC XBRL 與原文、FRED、S&P 名單
② 資料 → 訊號     screen.py（初選）、review_due.py（觸發）         ← 純程式，可稽核
③ 訊號 → 判斷     DOSSIER→FULL_A→FULL_B→RED_TEAM、TRIAGE/THESIS/PRICE/HOLD_REVIEW  ← LLM
④ 判斷 → 數字     cap_calc.py、gate3_check.py                      ← 純程式
⑤ 數字 → 部位     sizing.py（T、批次、閘門、現金/名額）             ← 純程式
⑥ 部位 → 帳       broker.py、corp_actions.py、book.py              ← 純程式
⑦ 帳 → 事實       nav_daily、metrics.py                            ← 純程式
```

設計文件宣稱「模型只在③被調用，其餘全是確定性程式，所以規則由程式保證」。但實際上：

- ③ 的輸出**不只有判斷，還包含④的輸入**（`cap_input.json`）與⑤的參數（`registry_entry.json` 的 `auto_buy_cap`、`target_weight`、`grade`）。
- ④ 的「覆核」只比對③自己算的算術結果，不比對事實。
- ⑤ 的規則（尾部風險上限、單事件上限、使用者上限、換股）多數沒有實作。

**所以真正的架構是：模型產生判斷＋參數，程式只負責讓這些參數彼此不矛盾。** 這與文件描述的「程式保證規則」是兩件事——這是本報告多數 P0 的根源。

---

## 2. P0：阻斷級（5 項）

### P0-1　價格隔離可被繞過：沙箱根目錄不在 deny 名單，舊沙箱從不清除

**位置**：`trader/trader/jobs/isolation.py:33-52`（`deny_roots`）、`trader/trader/jobs/sandbox.py:72-80`（`create_sandbox`）

`deny_roots()` 封鎖家目錄、`PKG_ROOT`、`/private`、`/tmp`、`/var`、`/Volumes`…、`state`。**沙箱根 `/Users/Shared/berkshire-sandbox-{user}` 本身不在名單內**，而每個工作的 cwd 就在它底下；`create_sandbox` 只清自己那一個 `_{job_type}_{n}`，其他 run／job 的沙箱永遠留著。

**已用真實模型證實**（§0.5.1）：cwd 在 `.../KO/_FULL_A_1`，用 `Read ../../OTIS/_FULL_B_1/dossier/OTIS_price.json` 讀到 `price 70.39`、`auto_buy_cap 49.29`；同一次呼叫讀專案 `config.yaml` 被 DENIED。目前磁碟上仍有 3 個 OTIS 的價格檔、合計 66 MB 從未清除。

**影響**：價格隔離——整個系統最核心的方法論保證——可以被上一輪自己留下的檔案繞過。而且**同一個代號**的價格檔永遠在同一個位置，跑過一次 FULL_B 之後，每次重跑 FULL_A 都拿得到。

**建議**：把沙箱根目錄加入 deny（只允許 cwd 自己），或每次啟動清空非本次 run 的目錄。

---

### P0-2　閘門 3 的「作答前沒看價格」是提示叫模型填 `false`

**位置**：`routing/prompts/FULL_A.md:11`、`berkshire-deep-merged/scripts/gate3_check.py:22-28`

提示原文：

> 寫 `{work}/{ticker}/gate3_answers.json`；**`price_seen_before_answering`＝false，`price_file_opened_at` 留空**。

`gate3_check.evaluate()` 的檢查是「這個欄位是否為 false」與「兩個時間戳的先後」。真正的開檔時間由 `open_price_file()` 寫在沙箱的 `price_file_opened_at.txt`（`sandbox.py:165`），**全專案沒有任何程式讀它**（只有測試讀）。

**影響**：閘門 3 的 price isolation 條款是儀式。配合 P0-1，模型可以真的看到價格、同時通過閘門 3。

**建議**：`run_gate3` 改讀框架寫的 `job_started_at` 與 `price_file_opened_at.txt`；刪掉 `price_seen_before_answering`（它無法被驗證）。

---

### P0-3　「程式覆核數字」只覆核算術，不覆核輸入

**位置**：`trader/trader/jobs/deep.py:334-357`、`trader/trader/analysis/checks.py:85-153`

FULL_B 的流程是：模型寫 `cap_input.json`（`oe`、`g10`、`g5`、`grade`、`norm_eps`…全是模型算的）→ 框架用 `cap_calc.calc()` 重算 → `compare_signal_with_cap(card, cap)` 比對訊號卡與重算結果。

第 3 步看起來很安全，但它比對的是「模型算的」與「模型提供的輸入經腳本算出的」——**兩者必然一致，除非模型算錯乘法**。如果模型把 `oe` 高估 30%，`cap_calc` 會忠實地算出高 30% 的上限，覆核通過。

而 `oe`（業主盈餘）是整套估值的地基：SKILL §3 用 20 行文字定義它（一次性項目、股權薪酬、維持性資本支出 K 值、5 年有效稅率中位…），**`build_dossier.py` 與 `dossier_prep.supplement_section()` 都沒有實作它**（只算有形資本報酬、資產負債補充）。SEC 原始數字都在手上，但沒有任一行程式算 OE。

**影響**：整個「數字不信任模型」的設計，在最重要的那個數字上完全失效。同理，`gate3_answers.json` 的證據（來源＋日期）也沒有被核對——框架不檢查那份 10-K 是否真的存在、日期是否真的早於決策日。

**建議**：把 OE 公式實作成 `oe_calc.py`（輸入 SEC companyfacts、輸出逐年每股 OE），`cap_input.oe` 改由框架提供；模型若要調整，必須列出調整項與金額，由框架加總。

---

### P0-4　登錄表幾乎完全由模型決定，且無任何結構驗證

**位置**：`trader/trader/analysis/writeback.py:237-271`、`trader/trader/jobs/deep.py:334-343`

`write_full_result()` 只覆寫 6 個欄位（`ticker`、`status`、兩個複查日期、`thesis_file`，持股時保留 `cost/first_buy_date/weight/build_plan`），其餘照抄 `registry_entry.json`：

| 欄位 | 下游用途 | 覆核 |
|---|---|---|
| `auto_buy_cap` | `plan_tranches` 的加碼限價、`review_due` 的加碼觸發 | ❌ |
| `sell_review_above` | `review_due` 的賣出檢視 | ❌ |
| `absurd_price_line` | `review_due` 的「至少賣一半」 | ❌ |
| `grade` | `plan_sells` 的權重上限減碼 | ❌ |
| `target_weight` | 批次金額 `(T×equity−mv)/n` | ❌ |
| `kill_criteria_due` | `review_due` 的到期檢查 | ❌ |
| `next_full_review_due` | 初選的 `needs_full()` | ❌ |

而 `registry_entry.json`／`decision_log.json`／`thesis.md`／`report.md` **沒有經過 `validate.check()`**——`output_schemas.FULL_B` 只把它們列在 `files: [...]`，`collect_outputs()` 直接複製。被驗證的只有最後那個摘要 JSON。

**實測**：訊號卡寫 `auto_buy_cap=60`（已與 cap_calc 對帳），`registry_entry.json` 寫 `180` → 登錄表存 **180**，之後 `plan_tranches` 就用 180 當加碼上限。

**建議**：`registry_entry.json` 加結構驗證；`auto_buy_cap`／`sell_review_above`／`absurd_price_line` 一律由 `cap_result` 覆寫（PRICE 已經這樣做）；`grade` 與 `target_weight` 由框架算，模型只給「意願權重」。

---

### P0-5　人工把持股歸零不會取消未成交買單

**位置**：`trader/trader/ledger/book.py:237-269`

`adjust(set_qty=0)` 刪 `positions`、把 registry 改回 WATCH，但**不動 `orders`**。隔天 `match_orders` 照常撮合舊買單。

**實測**：QRVO 100 股 → 歸零 → 仍有 `QRVO-BUY-20260105-1`（10 股 @70）→ 次日以 65.065 成交 → 持股變 10 股。

這正是 `handoff.md` 記錄的 QRVO 併購歸零操作路徑。**影響**：人工調帳是公司行動的唯一補救手段，而這個手段本身會製造反向交易。

**建議**：`set_qty=0` 或大幅改動股數時，一律取消該代號所有 open 委託並記錄原因。

---

## 3. P1：嚴重（9 項）

### P1-1　未成交的加碼批次仍被扣掉（原 P0-6，降級）

**位置**：`daily.py:89-94`（`_consume`）、`:120-121`（`_place` 對 ADD 呼叫）

`tranches_left` 在**下單成功**時減 1，不是**成交**時。**實測**：目標 10%、現有 100 股、限價 55.05、隔日開盤 58 → 0 成交，`tranches_left` 3→2。窗口 21 天到期後歸零，**部位永久停在目標的 2/3**。ADV 上限與現金不足造成的縮量同樣無法補回。

**影響**：部位系統性小於模型算出的目標（報酬被稀釋），且沒有紀錄說明原因。**不是錯誤交易**，所以不是 P0。

**建議**：改在 `broker._fill()` 成功時依成交量遞減，或每日比對「計畫 vs 成交」後補回。

---

### P1-2　同一天有兩種公司行動時，第二種被靜默丟棄（原 P0-7，降級）

**位置**：`corp_actions.py:70-74`（`_ca_exists` 只看 `(ticker, effective_date)`）、`:447-448`

資料表約束是 `UNIQUE(ticker, action_type, effective_date)`，但去重查詢**沒帶 action_type**。**實測**：拆股 applied 後，同日現金股利回 `duplicate`，應收股利 0.0，**異常筆數 0**。

**影響**：股利永久漏記且事後查不到。頻率低（拆股＋特別股利同日），所以 P1。

**建議**：去重鍵加上 `action_type`，與 DB 約束一致；`duplicate` 寫一筆 resolved 稽核紀錄。

---

### P1-3　賣出＋買進同一輪撮合，成交結果取決於代號字母順序（新發現）

**位置**：`broker.py:233-236`（`ORDER BY created_date, order_id`）、`_match_buy` 用 `cash(conn)`

`order_id = {ticker}-{side}-{date}-{n}`，所以排序實際上是「先代號、後買賣」。同一輪的賣單與買單誰先撮合，取決於代號字母。

**實測**（現金 500、賣 10 股可得 1000、買 10 股需 1000）：

| 組合 | 成交 |
|---|---|
| 買 AAA／賣 ZZZ（買單先撮） | AAA **5 股（部分）**、ZZZ 10 股 |
| 買 ZZZ／賣 AAA（賣單先撮） | AAA 10 股、ZZZ 10 股 |

`_fill` 把部分成交的訂單標成 `filled`（剩餘只寫在 `cancel_reason` 文字裡），**剩下的 5 股永久丟失**。

**影響**：換股／減碼後加碼這種「同一輪一賣一買」的操作，能不能買到取決於代號字母。這是 `sizing.plan_sells` → `plan_buys` 的標準路徑，不是罕見情境。

**建議**：`match_orders` 先撮所有 SELL 再撮 BUY（或先結算賣出 proceeds）；部分成交保留 `open` 並記錄 `filled_qty`。

---

### P1-4　`r_d` 被釘在 10%，利率對買價幾乎沒有影響，且方向不單調

**位置**：`cap_calc.py:71-73`、`:126`

```python
r_d = max(0.10, L + 0.03)
g_term_i = min(0.03, max(0.0, L - 0.01))
```

**實測（g=5%，IV_base 綁定）**：L=1%／3%／5%／7% 的 `auto_buy_cap` 全部是 **82.51**，L=9% 才是 63.76。**1%–7% 完全平坦。**

**實測（g=12%，P2′ 綁定）**：L=1% → **83.22**、2% → 87.85、3% → 93.63、4.2% → **101.07**、6% → 101.07、8% → **87.79**。

也就是說：**買價對利率的敏感度不單調**——先升後降。低利率時 `g_term` 塌成 0%，反而壓低上限。這是實作錯誤（`g_term = L − 1%` 不該隨利率塌陷），不是保守設計。

10% 的下限本身是 SKILL 的明文選擇（「不得直接以公債殖利率折現定價」），**這一半是設計、不是 bug**；但它讓 FRED DGS30、20 年中位、`low_rate_start`、`r_stress`、`r_sens`、P3、利率觸發（≥1pp）整條鏈對最終買價幾乎沒有作用。

**建議**：修 `g_term`（例如固定 3% 或 `min(3%, L)`）；若刻意保留 10% 下限，就把利率相關的資料與觸發一起移除，不要留著讓人以為它有效。

---

### P1-5　歷史成長 ≤0 的規則沒有實作，實際上限比規則寬 36%

**位置**：`cap_calc.py:133-134`（只加一句 note）、`:161`

SKILL §3 明文：「歷史成長 ≤0 → 上限只能用 `IV0`（E ÷ `r_d`）× 等級係數」。

**實測** g=−2%、oe=5、grade B：`IV0 = 50`，規則上限應為 `0.75 × 50 = 37.5`；**程式給 51.15**，寬 36%。

`plan.md` 規定「規則與程式衝突時以 skill 為準」，所以這是實作偏離權威文件。**影響**：對衰退中的公司（最需要保守對待的一群），系統願意付的價格比規則允許的高三分之一。

---

### P1-6　現金或名額不足而跳過的 BUY，沒有重試佇列

**位置**：`daily.py:249-277`、`sizing.py:355-358`

`amount = min(amount, adv×1%, avail)`；`avail` 不夠 → `qty=0` → `skip(...)`。這筆 BUY **不進任何佇列**，而 `load_decisions` 只取 `decision_date ∈ (prev, today]`，隔天就過期。`skip("持股已達 10 檔")` 同理。

配合 P1-3，同一輪「先賣後買」在成交日還可能因撮合順序買不到。

**影響**：SKILL §5「名額或現金不足 → 依 G8 排序」實際上是「先到先得，後到的永遠不做」。

**建議**：把被跳過的 BUY 寫進 `pending_orders` 佇列，隔日重試（有 TTL）。

---

### P1-7　持股滿 10 檔後永久不再買，且沒有換股機制

**位置**：`sizing.py:26`、`:325-326`

`MAX_HOLDINGS=10`，滿檔就 `skip("…需勝最弱持股（G8），留待人工")`。但「留待人工」沒有任何介面：沒有 `trader portfolio compare`、沒有待辦清單、儀表板也沒有這個提示。SKILL §5 說「核心 5–10 檔、最多 12 檔、保留 2 空名額」，程式卻在 10 檔硬停，也不允許 11–12 檔。

**影響**：長期執行後組合會鎖死在最初 10 檔。這是設計限制（程式註解有寫），但沒有配套流程，所以列 P1。

---

### P1-8　排程會把關機期間的每個交易日逐一補跑

**位置**：`scheduler.py:186-188`

```python
for d in cal.trading_days(start, last):
    if not self._daily_done(conn, d):
        tasks.append((f"daily:{d}", ["daily", "--date", d]))
```

筆電關機兩週 → 醒來依序補跑 10 個交易日，每個 `daily` 都跑持股觸發檢查（TRIAGE／THESIS／可能的 DOSSIER→FULL_A），**用兩週前的資料產生今天的決策**，每一輪都可能產生待同意的 opus 工作。

**建議**：只補最後一個交易日，其餘標記 skipped 並記一筆異常。

---

### P1-9　`bootstrap_block()` 會被任何殘留 job 永久卡死，且沒有取消機制

**位置**：`scheduler.py:55-65`

```python
n = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running','awaiting_approval')")
```

`jobs` 是**全域**表，不分 run。任何一次失敗的測試 run、被中斷的 bootstrap、忘記拒絕的同意列，都會讓排程永遠拒絕啟動。CLI **沒有 `trader jobs cancel`**，只能手動改 SQLite。目前正式 state 就有 11 個 `awaiting_approval` 的 FULL_A。

---

## 4. P2：設計與邏輯不一致（11 項）

### P2-1　P2′（隱含成長 10% 上限）對多數公司不生效（原 P1-2，降級）

**實測掃描**（oe=5、r_d=10%）：

| 歷史成長 | P2′ | IV_base | auto_buy_cap |
|---|---|---|---|
| 0% | 101.1 | 59.1 | **59.1** |
| 5% | 101.1 | 82.5 | **82.5** |
| 8% | 101.1 | 93.2 | **93.2** |
| 10% | 101.1 | 101.1 | **101.1** |
| 15% | 101.1 | 101.1 | **101.1** |

P2′ 只在歷史成長 ≥10% 時才是上限（3/13）。對典型的 3–6% 成長股，`auto_buy_cap` **等於** `IV_base`——SKILL 寫的 `min(P2′, IV_base)` 在數學上永遠取後者。

**這是冗餘，不是錯誤**（結果仍然正確），但「隱含成長不得超過 10%」這道閘門對它最想買的公司沒有作用，而它佔了 `cap_calc` 三分之一的程式。

### P2-2　C／D 級「不得買」沒有在框架層擋（原 P1-4，降級）

`cap_signal_by_gates()`（`checks.py:203-217`）只檢查 gate3 與初篩刪除，**不看 `certainty_grade`**。C 級 BUY 會寫進 decisions（signal=BUY）、產生一筆待同意的 RED_TEAM，最後才被 `target_weight()` 的 `GRADE_CAP.get("C", 0)=0` 歸零。

**結果是對的**（不會買），但防線在最後一層：任何人為了讓 C 級能建小部位而把 C 加進 `GRADE_CAP`，這道防線就消失。**建議**：在 `cap_signal_by_gates` 直接降級，並在 `FullBHandler.post` 不為 C/D 建 RED_TEAM（省一次同意）。

### P2-3　claude_code 模式沒有上下文控制機制（原 P1-9，降級）

**位置**：`context.py:99-105`。claude_code 模式只把**檔名**放進提示，`estimate_tokens` 量的是幾百字的清單，永遠不會超限；真正的上下文是模型自己讀的 dossier＋SEC 原文＋SKILL＋references。實測（`handoff.md`）：OTIS DOSSIER 一次 6.35M 輸入 token。

**這可能是有意的**（agentic 模式本來就讓模型自己讀檔），但「每個工作控制在 100k」這句話在主要後端上不成立，成本只剩 `budget.max_cost_usd_per_run` 在管。

### P2-4　`_portfolio_state()` 給 FULL_B 的組合狀態是殘缺的

**位置**：`pipelines/analyze.py:32-37`。**實測**有持股時回傳 `{'holdings': [{'ticker': 'KO', 'qty': 100, 'avg_cost': 50.0}], 'note': ...}`，**缺 `cash`、`n_holdings`、`committed`、`weights`**。SKILL §0 要求「持股、現金、下限、已承諾、檔數」。無持股時反而有 `cash`，行為不一致。G8 的機會成本比較直接受影響。

### P2-5　兩套同名 `GRADE_CAP`，語意完全不同

- `cap_calc.py:29`：`{"A":0.85,"B":0.75,"C":0.40}` → 估值安全邊際係數（進 P1）
- `sizing.py:23`：`{"A":0.15,"B":0.10}` → 部位權重上限

同名、同風格、不同意義。應改為 `MARGIN_OF_SAFETY` / `MAX_WEIGHT_BY_GRADE`。

### P2-6　`cap_calc` 有一半輸出對決策沒有影響

| 輸出 | 是否影響決策 |
|---|---|
| `IV_base` | ✅ 買價上限＋賣出檢視線 |
| `IV_cons` | ⚠️ 只在 `width>3` 時降級 |
| `P2′` | ⚠️ 只在歷史成長 ≥10% 時 |
| `grade_used` | ⚠️ 只影響 C/D |
| `P1`／`P3`／`P4′`／`W_PE` | ❌ 報告欄（SKILL 要求，屬設計） |
| `r_stress`／`r_sens` | ⚠️ 只在距上限 <5% 時 |

P1／P3／P4′ 是 SKILL 要求的報告欄，**不是 bug**；但 `monthly_freeze`（每月跑整個 S&P 500）、`fred.t30_median_20y()`、P4′ 的計算，對交易決策的邊際貢獻是 0。這是「維護成本 vs 行為差異」的取捨，值得確認是否真的有人看那些欄位。

### P2-7　`review_due.py` 把 8-K 5.02（董事／高階經理人異動）當「重大」

**位置**：`review_due.py:37-38`。**實測**：一則例行的 CFO 退休公告 → `action=FULL` → DOSSIER 自動跑（haiku，$0.15–0.56）＋一筆待同意的 FULL_A。大型股每年 5.02 很常見，`1.01 重大合約` 的定義也極寬。

**影響**：真正的重大性判斷（是否動搖論點）被跳過，直接交給最貴的流程；使用者每週要多按幾次同意。

### P2-8　規則書 W4 讓大多數大型股在 G4 就出局

**位置**：`rule_index/05_risk_financials_accounting.md:420`、`:455`

```
W4 | 公開盈餘指引、季度數字常剛好湊整、總部逼季度成長 | 警訊
警訊級合計 ≥ 2 → 不買
```

「公開盈餘指引」在美國大型股是常態。**這意味著多數候選股一進 G4 就已經有 1 個警訊**，再命中任何一個（重組費、稅率下降、現金轉換率 <0.7…）就淘汰。`handoff.md` 已記錄「W4 幾乎所有大型股都命中」。

一個 2 項門檻配上一個命中率 >60% 的項目，等於單項否決。這是「15 檔全部沒買」最直接的原因之一。

### P2-9　SKILL 的賣出規則有三條沒有實作

| SKILL §6 條款 | 狀態 |
|---|---|
| (1) 論點證偽 → 全清 | ✅ |
| (2) 現價 ≥ `sell_review_above` 且有更好用途 → 減碼 | ❌ `_map_action` 對 `SELL_REVIEW` 直接回 `HOLD`（"待組合比較（M5）"，M5 已完成但沒接） |
| (3) 現價 ≥ `absurd_price_line` → 至少賣一半 | ⚠️ 只在滿一年時 TRIM；且該值來自模型 |
| (4) 權重超限 → 減至上限 | ✅ |
| 單檔 >25% → 完整複查 | ❌ 全專案 grep 不到 |
| 建倉未滿一年不因價格賣出 | ✅ |

SKILL §5 的「尾部 10pp ÷ 最壞損失」「單事件 ≤ 權益 2%」「使用者上限」也全部沒實作——`target_weight(card, None)` 的 `user_cap` **永遠是 None**。

### P2-10　觸發檢查路徑的閘門 1 只看舊訊號卡

`review.py` 傳的 `screen_context` 沒有 `screen_decision`，`PriceHandler` 退回訊號卡的舊值；若都沒有，`_is_delete(None)=False` → gate1 直接 True。初篩事後把某檔改判刪除時，這條路徑不會知道（影響小：刪除類本來就不會進 WATCH）。

### P2-11　`pe_median` 的凍結值有前視風險（只影響回測）

`get_frozen(asof)` 回傳「asof 所屬月份」的凍結值，不管凍結日。若 10 月凍結在 10/15 計算，asof=10/03 的分析也會讀到它。前瞻交易不會踩到，回測與 `--screen-date` 補跑會。

---

## 5. 獲利邏輯：為什麼這套系統幾乎不會交易

### 5.1 漏斗（專案自己的數字）

```
503 檔 S&P 500
 → 257 核心道
 → 15 檔短名單
 → 15 檔 DOSSIER 全部完成
 → FULL_A：4 檔已完成並全部停止（TOO_HARD 1、REJECT 3、MISSING_DATA 2，含重跑）
            11 檔仍卡在 awaiting_approval
 → 0 筆 BUY
```

（取自正式 state 的 `runs`／`jobs`／`registry`，2026-10-17。）

三重結構性瓶頸：

1. **G4 的 W4**（P2-8）：公開盈餘指引＝1 個警訊，再加任一項就淘汰。
2. **G1 的產業預設太難**（SEL-050）：零售、硬體、REIT、菸草直接進「太難」。短名單裡 BBY／LOW／HPQ／SPG／MO 全中。
3. **買價上限**：`auto_buy_cap ≈ 11.8–18.6 × OE`（成長 0–8%），而大型股 P/E 中位 26.6。要買得比市場中位便宜 30–55%。

三者相乘，成交機率極低。**一個從不交易的策略無法被紙上交易驗證，也無法產生報酬。**

### 5.2 就算交易了，報酬結構也不對

- 部位上限 15%／10%、最多 10 檔、現金下限 5%、每日新部位 ≤4、批次建倉 21 天 → 實際平均曝險遠低於目標，**多頭市場結構性落後大盤**。
- 沒有停損、沒有再平衡（只有「權重超限且滿一年」才減碼）。
- 成本模型只有 10bps 滑價＋0 手續費，且用 IEX feed（成交量只涵蓋部分市場）。
- `plan_buys` 用當日收盤當參考價、隔日開盤成交：跳空上漲不成交（保守），跳空下跌以更低開盤成交（樂觀）。**沒有記錄「決策價 vs 成交價」的滑價統計**，所以永遠不知道假設是否成立。

### 5.3 驗證機制與策略目標不匹配

`procedure/13` 要求「期滿對大盤與初選母體檢討」。但 0 交易 → 無法檢討選股能力；3–5 筆 → 統計無意義；賺錢 → 可能只是 β。

**建議**：建「影子組合」——每一檔走到 G7 的股票都留下 `auto_buy_cap`、當日價、之後 3/6/12 個月報酬。這是唯一能在「不交易」時累積證據的方法，目前完全沒做。

---

## 6. skill 的撰寫問題

### 6.1 規則文字與程式實作已經漂移

| SKILL 條文 | 實作 | 差異 |
|---|---|---|
| 歷史成長 ≤0 → 上限 = IV0 × 等級係數 | 用 `IV_base` | 寬 36%（P1-5） |
| 保守 g0 = min(8%, 較低者 ×0.75) | 只有一個期間時 ×**0.6** | 文件未提 |
| 上限 = min(P2′, IV_base) | P2′ 對 g<10% 恆大於 IV_base | 實質只有 IV_base（P2-1） |
| 每個工作控制在 100k tokens | claude_code 模式不量測 | 實測 5–6M（P2-3） |

`rulebook.md` 的 P-xx／SEL-xx 編號在程式裡只是註解字串，改了規則不會有任何測試失敗。**建議**：把 `cap_calc` 的每個參數與 `inferred_thresholds.md` 建立對應表，並在 skill 測試裡斷言「文件寫的公式 = 程式算的結果」。

### 6.2 skill 的測試太淺

`test_screen.py`（93 行）、`test_review_due.py`（48 行）是 `assert` 腳本，不是 pytest；沒有測分道順序、`MATERIAL_8K` 清單、`last_review_date` 為空、`filings` 抓取失敗、`cap_calc` 的單調性。`cap_calc.py` 本身沒有單元測試（只有透過 trader 的 `test_checks.py` 間接測）。

### 6.3 提示把「自我證明」寫進規則

除了 P0-2，FULL_A 還要求模型「讀 SKILL.md（全文），再依 §2 表只讀該步指定的 references 段落」。claude_code 模式下**沒有任何機制確認它真的只讀了那些段落**。

### 6.4 `deep_quant.py` 是死碼

`SKILL.md:199` 明寫「逐檔流程不使用」，182 行模組沒有任何程式引用。

---

## 7. P3：小型 bug 與可維運性（9 項）

| # | 位置 | 問題 |
|---|---|---|
| 1 | `sizing.py:333-334` | `first_tranche_pct_of_target` 為 0 時被 `or 50` 變成 50%（實測） |
| 2 | `sizing.py:311` vs `buy_limit` | 兩道「必須低於上限」的檢查重複；第二道在數學上不可能觸發（死碼，無害） |
| 3 | `broker.py:171-175` | 部分成交也標 `filled`；`orders` 沒有 `filled_qty`，事後無法統計 |
| 4 | `book.py:117-128` | 缺價時用 `avg_cost` 估市值 → 下市股的權益被高估，sizing 跟著錯 |
| 5 | `writeback.py:534`、`:561` | `IV_base` 為 None 時會把 registry 的 `sell_review_above` 覆寫成 None → 賣出檢視永久失效 |
| 6 | `corp_actions.py:419-428` | 現金股利應收用「目前持股」而非除息日持股；晚偵測或期間有買賣時會算錯 |
| 7 | `corp_actions.py:405-413` | 拆股調整未成交委託的限價，但沒重算 `build_plan` 已承諾金額 |
| 8 | `review_due.py:131` | `triage[-20:]` 靜默丟棄第 21 筆以後（實測 25→20） |
| 9 | `scheduler.py:182` | 凍結條件用交易日 `>= freeze_day`；1 號是週末時語意變成「1 號之後第一個交易日」 |

**已撤回**：基準股利補跑重複計入（錯）、adjust 入金算成報酬（文件明示的設計）、deny 路徑轉義（未實測）。

---

## 8. 測試與驗證體系

1. **656 個測試全過**（`env -u PYTHONPATH python -m pytest -q`），但**沒有一個測到本報告的 P0**。原因：測試用同一套假設寫（fixture 預設關閉同意閘門、沙箱是乾淨的、沒有同日雙行動案例）。**測試驗證的是「程式照設計跑」，不是「設計是對的」。**
2. 沒有整合測試覆蓋「daily → review → DOSSIER → FULL_A → FULL_B → HOLD_REVIEW → 下單」的完整鏈路；P1-1／P1-3／P1-6 這種跨模組狀態錯誤正好落在接縫。
3. 沒有 property-based test：`cap_calc` 的單調性（利率↑ → 上限↑）用 hypothesis 三行就能抓到 P1-4 的方向錯誤。
4. 沒有「已知答案」的估值回歸測試：3 個標準案例（零成長、5% 成長、衰退）的期望上限寫死，就能防止 P1-5 這類漂移。
5. `validate-report` 從未被真實驗證（0 交易）。

---

## 9. 過度工程化評估

使用者問「是否有過度工程化」。分兩層回答。

### 9.1 我的指控有沒有過度？

有 4 處，已在 §0.5.3 修正：等級與買價、8-K 的 opus 花費、C 級燒 opus、P2′ 的嚴重度。其餘 8 項有實測或程式碼依據。

### 9.2 專案本身有沒有過度工程化？

**核心沒有。** 工作引擎（DAG／併發／預算／續跑）、帳本（交易邊界、檔案回滾、防重複入帳）、公司行動覆核（比例／價格比／市值守恆三道）都是**必要複雜度**——這些地方錯一次就是真的錯帳。656 個測試也合理。

**周邊有 4 組死機械**，加起來是可觀的維護面積：

| 機械 | 為什麼是死的 | 建議 |
|---|---|---|
| `cap_calc` 的 P1／P3／P4′／W_PE／r_stress／r_sens | 只進報告欄，不影響任何決策；P4′ 永遠大於上限 | 保留（SKILL 要求報告），但不要再擴充 |
| `monthly_freeze`（每月跑整個 S&P 500 算 P/E 中位） | 只餵 P4′，而 P4′ 永不生效 | 可降為「有需要再算」 |
| `openai_compat` 後端 | 三級都用 claude_code，API 後端從未驗證（handoff 自承） | 保留但標「未驗證」，或暫時移除 |
| `deep_quant.py` | 沒有任何引用，SKILL 明說不用 | 刪 |

另外兩處「為了未來的複雜度」：

- `approvals` 的 5 種狀態＋supersede 邏輯：對單一使用者、單機，`pending/approved/rejected` 三態就夠；`deferred` 與 `superseded` 增加了狀態機面積，但目前沒有實際使用痕跡。
- `jobs` 表全域、無 run 邊界清理：不是過度工程，是**不足**（P1-9）。

**結論**：這不是一個過度工程化的專案，而是一個**核心做得很紮實、周邊留了幾個沒人用的零件**的專案。真正的問題不是「做太多」，而是「最關鍵的那個介面（模型 → 參數）做得太少」。

---

## 10. 建議的修正順序

**第一批（不修就不建議啟動排程）**
1. 沙箱根目錄加入 deny ＋ 每次啟動清空非本次 run 的目錄（P0-1，已實測可利用）。
2. gate3 改讀框架寫的時間戳，刪掉模型自述欄位（P0-2）。
3. `registry_entry.json` 加結構驗證；三個每股價格欄位一律由 `cap_result` 覆寫（P0-4）。
4. `adjust` 歸零持股時取消所有 open 委託（P0-5）。
5. `match_orders` 先賣後買、部分成交保留 open（P1-3）。
6. `tranches_left` 改在成交時遞減（P1-1）。

**第二批（決定策略是否成立）**
7. 把 OE 公式實作成程式，`cap_input.oe` 由框架提供（P0-3）。
8. 修 `g_term`，讓利率的影響單調（P1-4）；實作「歷史成長 ≤0 → IV0×k」（P1-5）。
9. 重新校準 G4 的 W4（改成「指引＋連續下修」才計分），否則樣本永遠是 0（P2-8）。
10. 建「影子組合」，讓驗證在 0 交易時也能累積證據（§5.3）。

**第三批（可維運性）**
11. 排程只補最後一日（P1-8）；加 `trader jobs cancel`（P1-9）。
12. 把 `MAX_HOLDINGS` 與「勝最弱持股」做成真正的換股流程（P1-7）。
13. 統一 `GRADE_CAP` 命名、刪 `deep_quant.py`、skill 測試改 pytest 並加單調性測試。

---

## 11. 附錄

### A. 檔案

| 檔案 | 內容 |
|---|---|
| `REPORT.md` | 本報告 |
| `probe_bugs.py` | 10 項行為探針（P0-1／2／3／4／5、P1-1／2、P2-4、P2-7、SPY 對齊） |
| `verify_issues.py` | 10 項逐條複驗（證實／誇大／推翻） |
| `verify_sandbox_live.py` | 真實模型驗證價格隔離（1 次 haiku，$0.0017） |
| `cap_probe.py` | 估值參數敏感度掃描 |
| `probe_output.txt` | 探針輸出 |

執行：
```
python probe_bugs.py          # 只寫 tmp/ 下的暫存 state
python verify_issues.py
python verify_sandbox_live.py # 會呼叫 1 次 claude haiku
```

### B. 測試套件

`env -u PYTHONPATH python -m pytest -q` → **656 passed**（與 `handoff.md` 一致）。
註：本審查環境的 Python 被注入 `sitecustomize` 攔截 `mkdir`，需 `env -u PYTHONPATH` 才能跑完；這是審查環境問題，不是專案問題。

### C. 未深入審查（誠實標示）

- `desktop/BerkshireWidget/main.swift`（371 行）只閱讀，未編譯執行。
- `web/static/app.js`（613 行）只檢查 CSP 與 API 契約，未逐頁操作。
- `data/sec.py`、`data/xbrl_shares.py`（613 行）、`analysis/peers.py` 只讀主要路徑，未驗證真實 SEC 資料的邊界案例。
- 真模型輸出品質只引用 `handoff.md` 的既有實測，本次未重跑（除 §0.5.1 那一次）。

---

*本報告的所有結論都可由附錄 A 的程式重現；未對受審專案做任何檔案修改。*
