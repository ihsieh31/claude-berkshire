# 對抗式審查報告 — `berkshire`（v3：真實性與過度工程化自我查核）

審查者：Qoder（qwen 執行緒）
日期：2026-10-17（v1 上午交件；v2 12:00 完成第二輪重驗；**v3 於本日晚間完成第三輪：逐條查「是否真實存在＋是否可達」，並重審自己的修正建議**）
專案：`/Users/zongen/Downloads/claude code/berkshire`
程式碼快照：**2026-10-17 09:19 之後**（你在 08:38–08:56 改過 `jobs/validate.py`、`jobs/context.py`、`routing/output_schemas.json`、`trader/models.py`、`trader/config.py`、`llm/claude_code.py`、`web/static/app.js`；`jobs/engine.py` 為 00:42）
審查模式：**唯讀**（未修改專案原始碼、設定、規則檔；報告與測試只落在本目錄 `bug report/qwen/`）。**v3 更正**：v1／v2 這行寫的「未修改任何專案檔案」並不精確——我在專案樹內留下了 pytest 快取與三個探測暫存目錄，明細與自擇不刪除的理由見 §0.3「v3 邊界更正」與 §11 第 10 項。

> v3 的讀法：**先讀 §12**（第三輪裁決表：哪些條目我證明可達、哪些是我用測試狀態造出來的、哪些誇大已撤銷，以及 §9 建議被我砍掉的部分）。§0.4 是 v2 對 v1 的逐條裁決，§3A 是 v2 新增的 12 條；凡 §12 標 `誇大撤銷`／`嚴重度下修`／`降級` 者，以 §12 為準，原文只留作痕跡。v3 另新增 1 條（N-13）。


---

## 0. 方法、證據等級與自證

### 0.1 做法
1. 從第一性原理把系統拆成五層（§1），先問「這一層的失敗會怎麼讓錢變少」，再逐層讀碼。
2. 讀碼順序：`SKILL.md`／`rulebook.md`（規則真值）→ `scripts/cap_calc.py`（錢的算式）→ `trader/`（決策與帳本）→ `routing/`（提示與 schema）→ 初篩。
3. 對每個候選缺陷寫**可執行重現測試**。測試斷言的是「程式實際做了什麼」，因此**綠燈＝缺陷存在**，紅燈才代表我誤判。
4. 第一輪有兩個子代理分別審查「工作引擎／價格隔離」與「skill 規則／提示」，其結論我逐條回讀原始檔與行號後才收錄；未親驗者一律標 `△`。
5. **v2 第二輪**：派 5 組唯讀查核代理（獲利邏輯／框架 fail-open／隔離與閘門 3／資料時點與公司行動／規則文本）把 v1 全部條目逐條對最新程式碼重判，裁決限定為 `仍成立`／`已修`／`行號漂移`／`我讀錯了`／`部分成立`；我再親自查核其中最關鍵的 12 條（`cap_calc` 的 D 級守衛與 `g_term` 下限、`validate` 的自由文字後門、`gate2` 未讀 `g_impl_gt_cap`、`incr_roic_gt_rd` 死輸入、`universe` 無 `asof`、`daily` 公司行動 +60 日窗、`sizing` 的批次卡死與 C/D 級不減碼、`rulebook.md:3-4` 與 8 份 banner、`screen_quant` 的 PIT 標記）。代理與我自己的判斷衝突時，**以我自己重跑出來的測試為準**（因此 v1 有 4 條被我自己推翻或改向，見 §0.4）。

### 0.2 證據等級
- `[T]` — 由本目錄測試實際跑出（v3：**50 支全綠**，其中第三輪 11 支，見 §10）。
- `[V]` — 我親自讀碼／讀檔確認（附 `檔:行`）。
- `△` — 來自子代理審查，我未完整重現（多半需真實執行環境或實模型輸出）；列為「需人工複核」。**v3：原本 5 條 `△` 已全部處理完畢**（P1-5→`[T]` 並降 P2、P1-14→結構確認、S-10→`[V]` 並更正行號、D-6/D-7→下修），報告裡不再留有 `△` 的金錢主張。

### 0.3 產出物
| 檔 | 內容 |
|---|---|
| `test_repro_profit_logic.py` | 15 支：`cap_calc.py`、`gate3_check.py`、`review_due.py`、`screen*.py` |
| `test_repro_framework.py` | 10 支：`daily.py`、`pe_median.py`、`sizing.py`、`writeback.py`、`approvals.py`、`corp_actions.py`、`validate.py`、`sandbox.py` |
| `test_repro_round2.py` | 14 支（v2 新增）：D 級守衛（推翻舊結論）、未知等級借用 C、`g_term` 私自加下限、`incr_roic_gt_rd` 死輸入、`gate2` 未讀 `g_impl_gt_cap`、DOSSIER 自檢欄自由文字、THESIS 交白卷（改版後仍成立）、清空後的賣出線永久靜默、缺上限把建倉批次卡死、C/None 級不做權重減碼、RED_TEAM 未列管隔離、傳輸重試對預算不可見、`load_universe` 無 `asof`、PIT 未受檢卻標已檢 |
| **`test_repro_round3.py`** | **11 支（v3 新增，全部用來查「我自己」）**：FULL 回寫會丟掉三條估值線、該狀態餵進 `review_due` 的靜默與對照組、`review.py:55` 刪 `None` 鍵其實無影響（**撤銷用**）、HOLDING 受空模板豁免（**限定用**）、gate2 缺上限時 fail-closed（**撤銷用**）、`g_impl_gt_cap` 對閘門 2 冗餘（**撤銷用**）、`g_term` 下限的真實幅度（**降級用**）、`universe_versions` 其實有被讀（**更正用**）、`review_jobs` 無法單獨匯入（**新增 N-13**）、`rejected` 同意列的同 run 死巷與跨 run 出口（**降級 P1-5**）、FULL 路徑不與 `cap_result` 比對（**新增 N-1 向量**） |
| `REPORT.md` | 本報告 |

執行：
```
cd "/Users/zongen/Downloads/claude code/berkshire/bug report/qwen"
python3 -m pytest -q          # 50 passed
```
專案自身的 656 支測試在 v2 重跑時仍全部通過（`656 passed`）。換言之，**這些缺陷全部落在現有測試的覆蓋盲區**；其中 `tests/test_pe_median.py:101-113` 甚至把 P0-6 的錯誤行為寫成斷言（寫入 `asof` 較晚的檔仍宣稱可取回），屬「測試固化缺陷」。
**v3 邊界更正（重要）**：v2 號稱「未在任何專案目錄留下寫入」並不精確。實查檔案時間戳：(1) 我在 11:42 跑專案測試時覆寫了既有的 `trader/.pytest_cache/v/cache/nodeids`、`lastfailed`（該目錄 10-16 06:57 就存在，內容可由 pytest 重建）；(2) 第一、二輪我用專案自己的暫存根建了三個探測目錄 `test_runs/probe28ka_xgm0`、`proberbcw5n6i`、`probe325no2bav`（10:31–10:36，後者含一份 state 複本）。兩者都在專案樹內，違反我自己設定的唯讀邊界；v3 未把它們刪除，因為「刪除」對該路徑同樣是不可逆的寫入動作，留給你決定。專案**原始碼、設定、規則檔**我都沒有改過；`state/trader.db` 的時間戳是 09:35，我對它只有唯讀存取（`mode=ro`），無法也不必歸因。詳見 §11。<br>**v3 完備性檢查**（`find -newermt "2026-10-17 09:20"`，扣除 `state/`、`test_runs/`、`__pycache__` 後整個專案只剩四個檔案）：上列兩個 pytest 快取（11:42:15）與 `.DS_Store`（09:44:42，早於我 10:31 的第一個探測目錄，且我沒有呼叫過 Finder）以及 `.workbuddy-ai/memory/2026-10-17.md`（11:44:12，另一個 agent 工具的記憶檔，我從未對該目錄寫入）。**也就是說：除了 pytest 快取與三個 `probe*` 暫存目錄，專案樹內沒有任何我一方的寫入。**

### 0.4 第二輪裁決表（v1 條目 → 最新程式碼）

| v1 ID | 裁決 | 目前的錨點與差異 |
|---|---|---|
| P0-1 基準股利鍵名 | **仍成立** | `daily.py:294` 讀 `a.get("cash")`；`data/alpaca.py:114` 只寫 `cash_per_share`。全案 grep 無任何地方補 `cash` 鍵 |
| P0-2 `if iv_base` 真值洞 | **仍成立** | `cap_calc.py:161` 原樣未動；`:162` 的警告也同樣要 `iv_base` truthy → 缺值時連註記都不發 |
| P0-3 零成長未封 IV0 | **仍成立** | `cap_calc.py:131-135` 仍只 `notes.append`；全檔 `IV0` 只在 `:89` 算、`:99` 輸出，無任何一處把 `cap` 封在 `IV0×k` |
| P0-4a `SELL_REVIEW→HOLD` | **仍成立** | `review_jobs.py:145-146` 原樣；全 repo 無其他處置（`tests/test_review_jobs.py:439-443` 把它固化成斷言） |
| P0-4b 賣出線只在 HOLDING | **仍成立（行號漂移）** | `review_due.py:116-124`（原 116-121） |
| P0-4c 缺價格靜默 | **改向：仍成立但機制不同** | 我 v1 說是「被當模組呼叫時靜默」；實際 `pipelines/review.py:180-199` 用**子程序**叫 `main()`，那條路徑不存在。實質失靈在 `review_due.py:105` `if px is not None and len(px)` → 無價 → 所有價格觸發跳過 → action `NONE` → `review.py:240` `continue` 不建工作；`check()` 本身確實不回傳「未檢查」 |
| P0-4d 缺 `target_weight` | **仍成立** | `review_due.py:123`；但要修正方向：`sizing.py:305-308`／`:83-85` 對「無上限／無等級」是 **fail-closed**（跳過不買），所以這條只關掉「該不該重檢」的觸發器，不會造成亂加碼 |
| P0-5(1) THESIS `on_invalid` | **仍成立** | `handlers.py:121` 的 `data` 仍無 `review_type`；`sizing.py:243-245` 仍 `continue` 回退舊卡。覆寫 `on_invalid` 的仍是 DOSSIER／FULL_A／FULL_B／RED_TEAM／CORP_ACTION／HOLD_REVIEW／TRIAGE，唯獨 `ThesisHandler`（`review_jobs.py:349-419`）與 `PriceHandler` 用基底版 |
| P0-5(2) 交白卷可過 | **仍成立（你今早改過該檔但未修這條）** | `validate.py:98-101`（空陣列不遞迴）、`:86-88`（只查缺鍵）、`:205-209`（`""` 合格）；`output_schemas.json` 的 `THESIS` 段仍只要求 6 鍵 |
| P0-6 凍結 P/E 無 asof 檢查 | **仍成立** | `pe_median.py:111-118` 仍只看檔名存在；呼叫端 `analyze.py:109`／`review.py:219` 也未補檢查 |
| P0-7 隔離不可稽核 | **仍成立（子項更正）** | 沙箱根 `/Users/Shared/berkshire-sandbox-zongen` 現為 `drwx------`(700，因 `isolation.py:71 chmod 700`)，**不是 v1 寫的 1777**（1777 是 `/Users/Shared` 本身）；真價檔 `.../OTIS/_FULL_B_1/dossier/OTIS_price.json`（70.39）仍在。`prepare_root:70` `mkdir(exist_ok=True)`＋`chmod` 仍不檢查同名目錄擁有者 |
| P0-8 Q3b／Q3c 無法表示 | **仍成立（你今早改過 schema 仍未補）** | `output_schemas.json:182` 列舉仍是 `Q0｜Q1｜Q1b｜Q2｜Q3｜Q4｜Q5｜Q6`；`prompts/HOLD_REVIEW.md:24` 仍寫「TRIM 只用於 Q3c」；`hold_jobs.py:81` 的錯誤訊息自己用「Q3c」這個被 schema 禁止的詞 |
| P0-9 公司行動去重鍵 | **仍成立** | `db.py:46` 三鍵唯一 vs `corp_actions.py:241-242/:246` 兩鍵去重、`_ca_exists:70-73` 兩鍵、`:447-448` duplicate 分支；新增：`jobs/corp_jobs.py:84-88` 對 `duplicate` 直接 `return []`，不記異常 |
| P1-1 `router.py:22` 漏 state | **仍成立** | `router.py:22` `deny_roots(extra_deny)`（不帶 state）vs `engine.py:68` 帶 state。這是本質差異，不是 config 問題 |
| P1-2 驗證只查形狀 | **仍成立，且更廣** | 新增：`validate.py:193-198` 只要任一選項含 `…`／`..`／`：`，**整個欄位降為自由文字** → `DOSSIER.dq_checks[].result` 填空字串或任何話都合格（`[T]`） |
| P1-3 空模板覆寫登錄表 | **仍成立（v3 加限定）** | `writeback.py:339-347`→`:356` upsert 整列。**v3**：空模板只在「前一列不是 HOLDING」時套用（`writeback.py:335-337` 保留 HOLDING），所以清掉估值記憶的範圍是 WATCH 階段；`pipelines/review.py:55` 那一環我已在 §12 C-1 撤銷。真正的生產者是 N-1 的 FULL 回寫與 `write_price_result` 的 `None` 覆寫 |
| P1-4 拒絕不擋加碼 | **仍成立** | `approvals.py:208-213` 只認 `pending/deferred/approved` |
| P1-5 拒絕後卡死 | **v3：可達，但降為 P2（且與文件宣示一致）** | `gate():59-61` 見既有 `rejected` 列不建新 pending；`approve():140-141` 只收 `pending/deferred`；`release():179` 只撈 `approved` → 同 run 無解鎖出口。v3 以 `test_repro_round3.py::test_rejected_approval_row_is_a_dead_end_within_the_same_run` **端到端實測**，同時實測「下一個 run 會重建 pending 列」→ 不是永久鎖死；`approvals.py:4` 文件本來就寫「下次觸發再排」。另更正：v2 寫的 `ensure()` 不存在，函式名是 `gate()` |
| P1-6 金融類以 P4′ 當硬上限 | **仍成立（並修正一處）** | `cap_calc.py:142/:153`：金融 `P4 = min(pem, fpm×1.1)×E`（缺 `fin_pe_median` 時 `pem×E`），`capc` 取 `[P4]`。**但 v1/代理推論的「A 級無條件 1.2 倍放大硬上限」不成立**：`k4=1.2`（`:144`）只給非金融，而非金融的 `cap` 走 `P2′` → 1.2 倍只作用在軟上限欄 |
| P1-7 `ivc_at` 丟掉 `oe_` | **仍成立（更強）** | `:116-122` 覆寫分支不用 `oe_`；`:170` 金融走 `k*ivc_at(r_d, oe*0.9)` → `p2_oe ≡ P1`，獲利打九折那條腿**恆等無操作** |
| P1-8 缺 `iv_opt` 免測 P-21 | **仍成立** | `:105-109`；新增根因：全檔用 `x.get(key, default)`，依 `output_schemas.json:96-98` 的「數字或 null」慣例，**顯式 `null` 會殺掉 fallback**（`iv_cons: null` → `:100` 早退；`iv_opt: null` → P-21 失效） |
| P1-9 D 級無守衛 | **我讀錯了（撤銷主訴）** | `cap_calc.py:112-113` 已有 `if grade == "D": no_cap = ...`，`:159-160` 把 `cap` 與 `auto_buy_cap` 都設 None（`[T: test_grade_d_is_already_blocked_my_old_p1_9_was_wrong]`）。殘留的是**另一條**：`GRADE_CAP.get(grade, C)`（`:114`）把拼錯的等級（`"a"`、`None`）無聲當 C 算出上限（`[T]`） |
| P1-10 special_watch 洩價 | **仍成立** | `screen.py:184` 仍無 `split=True`；`:184` 排序還用 `drawdown_3y`（價格衍生欄） |
| P1-11 缺 SIC 的 T5 守衛 | **部分成立（後果改向）** | 守衛 `screen_quant.py:88` 確為死代碼（`sector` 是 NaN，`!= '未分類'` 恆真），但 `groupby` 丟掉 NaN 組 → `T5` **恆不觸發**（靜默不判），不是 v1 暗示的「亂標」 |
| P1-12 只驗 features 的 asof | **仍成立** | `screen.py:229-231`：讀了 `prices.csv` 卻不比對其 `asof` |
| P1-13 重試對預算不可見 | **仍成立** | `engine.py:277` 一次 submit 內 `call_with_retry(retries=TRANSPORT_RETRIES)`，`llm/base.py:103` 實喊 `retries+1` 次；`_on_done` 只記 1 筆 `llm_calls`（`[T]`）。新增：預算只按 `run_id` 累計 → 每日新 run 即可跨越 `max_cost_usd_per_run`；`config.py:42-43` 該值可為 0＝不限，`max_input_tokens_per_run` 預設就是 0 |
| P1-14 post 與 done 非同一交易 | **v3：讀碼確定（崩窗條件下可達）** | `engine.py:318-329` 先執行 `handler.post()`（內部各自提交）、再於 `:329` `_update(status="done")`；`:157` 的 `_begin` 把殘留 `running` 降回 `pending` 重跑。`writeback.next_decision_id` 依既有 decisions 遞增 → 重放產生 `…-2` 的新決策列與第二份論點增補；`registry` 是 upsert 所以帳本不會雙重，下單端另有 `open_buys` 去重。我沒有注入崩潰，屬**結構確認**而非端到端重現 |
| P1-15(a) PIT 缺報日回傳 False | **我讀錯了（改寫）** | 實際是 `screen_quant.py:52-53` `bad = fd.notna() & (fd >= D)` → 空／壞申報日**跳過不驗**，而 `:61` 把整表 `pit_checked` 標 True（`[T: test_point_in_time_marks_unverified_rows_as_checked]`）；且真有前視時 `:55` 丟 `LookAheadError` 中斷整批，不是丟棄單筆 |
| P1-15(b) `_clean` 金融旗標 | **撤銷** | 機制還在（`~astype(bool)`）但唯一生產路徑 `screen.py:74`→`from_live:37` 先 `fillna(False).astype(bool)`，實檔無誤判。僅當有人繞過 `from_live`（如 `deep_quant.py` 直接吃 CSV）才會復發 |
| P2-1 沙箱 schema 被刮穿 | **成立但嚴重度下修** | 鍵確實被刮（`output_schemas.json:18 contains_price`、`:87 moat_tests.price_cut`；`pricing_power`／`attack_copy` 因不含 "price" 倖存）。但 v1 的後果推論錯了：`jobs/context.py:96-101` 把**未刮的完整 schema 內嵌進提示文字**，模型不必靠沙箱副本也能湊齊必填欄 → 不會「必然 invalid_output」，殘留的是**同一工作有兩份不同 schema 真值**的稽核問題 |
| P2-2 日期字串比較／崩潰 | **仍成立** | `gate3_check.py:38` `str(e['date']) >= D`；`e` 非 dict → `AttributeError` 未 try/except；`:48-49 unknown_count` 算了不用 |
| P2-3 問卷宣稱與選項矛盾 | **仍成立（範圍擴大）** | 缺「不知道」的題是 **C1、C5、M5、M6** 四題（v1 只列三題）；且檢查器根本不驗 `answer ∈ choices`，只驗 `∈ pass_if`（`:36`） |
| P2-4 moat 枚舉不同源 | **部分成立（第三源不存在）** | `output_schemas.json:85-87`「強｜中｜弱」vs 問卷 M1–M3「強、弱、不知道」；但 `assets/signal_card_schema.json` **沒有 moat 欄**（grep 0 命中），`procedure/01:53-57` 與報告模板是二元強／弱 → 「中」只活在 routing schema（第四源），不是三處 |
| P2-5 文本三頭真值 | **半修（你已加裁定，但沒覆蓋全部）** | 新增 `rulebook.md:4` 覆寫清單＋8 份 `rule_index/*.md:1` banner（實測 `04_hold_sell.md:1` 已寫「40 倍→50 倍」）。殘留：(a) `inferred_thresholds.md:7`（8%）與 `:50`（40 倍）仍在，而 `rulebook.md:152` 明定推論參數「一律列入 14.2 節」那張表；(b) 保護期 1 年（`SKILL.md:131`、`registry_schema.json:18`、`PRICE.md:17`）vs 2 年（`rulebook.md:229/:384`、`prompts/HOLD_REVIEW.md:25`），**程式站 2 年**（`hold_jobs.py:29 TWO_YEARS_DAYS=730`）；(c) C 級分母 `IV_cons`（`rulebook.md:188`）vs `IV0`（`rule_index/02:278`） |
| P2-6 優先序互指次等 | **半修** | `rulebook.md:3` 現與 `SKILL.md:10` 同鏈（SKILL＞procedure＞rulebook＞rule_index＞knowledge）；但 `rulebook.md:152` 原文未動（「本表即規則書參數真值；與 references 不同時以本表為準」）與自身 `:3` 互斥 |
| P2-7 P1 拘束力四說 | **仍成立** | `rulebook.md:265/:271/:278/:405` 四條未動，且 `:405` 與 `:4`「買價上限只用 P2′」直接抵觸；`SKILL.md:93` 不含 P1 |
| P2-8 懸空引用 | **仍成立** | `assets/templates.md`、`CHANGELOG_規則書.md`、`QA_規則書.md`、`EVIDENCE_MANIFEST.md` 仍全部不存在（`rulebook.md:126/:152/:436`、`rule_index/07:450`、`procedure/11:3` 引用未修）；`X28／GAP-X05-1／X21-10／X30-14` 全案仍無定義表（正則 0 命中） |
| P2-9 鐵律 9／7 自我違反 | **改判部分成立** | 文本全在（行號：`SKILL.md:44`、`procedure/11:8`、`rule_index/02:152/171/440`、`03:113`、`04:362/378`、`inferred_thresholds.md:36`、`knowledge/02:142`、`06:49`、`16:7`、`SKILL_screen:14`、`rulebook.md:318`）。但鐵律 9 禁的是「以個案類比**某個標的**」，芒格條款／knowledge 三處自標「校準用，不是門檻」→ 屬具名與內部統計外洩（`SKILL.md:178`），不是個案類比。鐵律 7 的實質衝突只剩 G8＋P-27（`rulebook.md:115/:199`、`procedure/06:46`）；`:413/:371` 已部分抵銷 P-25／分區記錄 |
| P2-10／11 `E` 口徑與量綱 | **改向（一半是我講反了）** | `cap_calc.py:7` 已明確 `oe`＝每股，`corp_actions.py:32-33` 把 `oe, norm_eps, iv_*, price` 列入每股口徑逐股調整 → 與 `SKILL.md:81/88` 一致。真正的偏差在 `review_jobs.py:253` `e = x.get("norm_eps") or x["oe"]`：**有** `norm_eps` 時用「正常化營業 EPS」而非規則的 E（5 年 OE 中位），**為 0** 時無聲切到 `oe`。全檔 `cap_calc` 仍**零量綱斷言**（只在 `iv_cons<=0` 拒算） |
| P2-12 決策配最新卡 | **仍成立** | `sizing.py:194` `latest_card(conn, t)`、`:238-240` `ORDER BY decision_date DESC, created_at DESC, rowid DESC` |
| P2-13 `_next_free_id` 在交易外 | **仍成立** | `writeback.py:106-112`，`_write_rows` 的 `BEGIN IMMEDIATE` 在 `:129` |
| P2-14 降 tier 繞過同意 | **仍成立，config 目前未啟用** | `config.yaml:46 job_tier_overrides: {}`（今早你改過此檔，目前是空的）；機制仍在（`router.py:43` → `approvals.py:33 require_for_tiers=["strong"]`）。新增：`require_for_tiers: []` 會整扇關閉同意閘門，`config.example.yaml:65` 把 `[] ＝ 不需同意` 寫成文件化用法（屬設計選擇，但無警告、無決策日誌痕跡） |
| P2-15 `_clean`／PIT 軟肋 | 見 P1-15 | 合併 |
| S-1 真值歸屬 | **半修** | 見 P2-5／P2-6。另新增第 4 條：`reason_code` 四處不同源（`output_schemas.json:74` 有 `NOT_THIS_SKILL`、`:126` 沒有、`signal_card_schema.json:11` 有、`rulebook.md:436` 另列 7 種規則書理由碼，其中 `NO_NEW_FACT／THESIS_BROKEN／VALUATION／VALUATION_AND_WEAKENING／SWITCH／FUNDING／INSTITUTIONAL` 在機器格式中不存在） |
| S-2 文本與可執行參數不同源 | **仍成立** | `cap_calc.py:33 G_IMPL_MAX=0.10` 硬編、`review_jobs.py:29 ABSURD_PE=50`；`inferred_thresholds.md` 的 8%／40 仍是給訊號卡抄的表 |
| S-3 加碼閘門形式死路 | **仍成立** | `procedure/12:27/:34` 仍要求「三閘門以當次資料重新全過」，`:15` 仍要求聲明 `price_seen_before_answering: false`；全案無加碼用的閘門 3 變體或豁免（鐵律 2 現為 `SKILL.md:37`） |
| S-4 漏斗接近空集合 | **改判部分成立** | `procedure/06:9`「逐項降一級」表 `:12–:32` 共 21 列，但 `:22`（產業共同衝擊年，自註「不是買進理由」）與 `:24`（等級與結論說明）不觸發降級 → 實際 19 項。起點 A＋1 降＝B（10%）仍可買，所以「單一降級即空集合」的說法過強；但起點 B、金融類、或 `:21` 穩定度 >4pp 直落 C（上限 0）三路徑疊加 `:34`「A 級須稀少」→ 可買集合仍結構性極窄 |
| S-5 提示把模型導到第二級真值 | **仍成立（banner 讀不到）** | `prompts/HOLD_REVIEW.md:16` 仍叫模型讀 `rule_index/04_hold_sell.md`（SELL-020～083）；覆寫 banner 在該檔 `:1`，而 `SKILL.md:10` 明文「不要整份讀」→ 模型 grep 段落時看不到 banner，過期值（40 倍、`04:230` 的「統計便宜型」）對模型仍是本檔真值 |
| S-6 枚舉跨檔不同源 | 見 P2-4 | 合併 |
| S-7 懸空引用 | 見 P2-8 | 合併 |
| S-8 鐵律 7 | 見 P2-9 | 合併 |
| S-9 `PRICE.md:18` 無判準 | **仍成立** | 未動 |
| S-10 引用截斷 | **v3：逐行確認，屬 `[V]`（並把行號更正為 14／15／16／30）** | `references/procedure/11_distress_check.md` 四處：`:14`「（原文：醜聞期間沒有人停用產品，；訪查）」、`:15`「**真轉機，不買**（：好經理碰上壞生意…）」（出處整個不見）、`:16`「…公司約打平，；一年零報酬之後回到高報酬可以接受，；永不依賴陌生人的善意）」、`:30`「（…虧損紀錄：；壓力情境下一年零報酬可接受：）」。共同特徵是「，；」與「：）」——引文或出處被吃掉，只剩懸空標點。v2 我引的「14/16/30」漏了 `:15`。無執行期影響，屬規則文本品質 |
| D-1～D-5／D-8 | **仍成立** | 未動；D-5 需補一句：新查實 `plan_buys` 與 `target_weight` 對缺值是 **fail-closed**，所以「等待同意期間可新建倉」的風險只在閘門紀錄與卡欄齊全時存在 |
| D-6／D-7 | **v3：兩條都下修** | D-6（`hold_jobs.py:55-66` 讀沙箱 `*_price.json` 判估值賣出）：機制確認——該檔只取 `price`、**不比對其日期與 asof**，且 `sorted(glob)` 取最後一檔；但「可能過期」的前提我證明不了，因為 `{T}_price.json` 是由 `dossier_prep.build`（`:305`）在當次沙箱現造，正常流程下就是最新可得收盤 → **降為「缺日期斷言」的穩健性註記**。D-7（`pipelines/approved.py:29-31` 全程持 `pipeline_lock` 跑 `engine.run`）：代碼確認，但那是該檔註解自己宣示的意圖（「等排程中的工作做完再寫帳本」），長任務阻塞每日排程屬**設計取捨**，不是缺陷 → 移到「觀測項」 |



---

## 1. 第一性原理拆解：這個系統真正在賭什麼

把它當成一個資本配置問題，而不是軟體專案：

```
利潤 = Σ [ 部位權重 × (企業內在價值成長 + 價值收斂) ] − 摩擦 − 踩雷損失
```

規則自述的唯一**有實證**的優勢只有一項：初篩的結構性刪除能降低踩雷率（`README.md`、`berkshire-screen-merged/references/evidence_statistical.md`）。其餘全是「假設規則正確＋假設執行忠於規則」的推論鏈。因此系統的正當架構應是：

| 層 | 應然職責 | 失效時的金錢後果 | 必須的失效模式 |
|---|---|---|---|
| L1 資料點對點 | 決策日只看到當時可得的資料 | 回測與紙上交易全部高估報酬，規則被錯誤資料「驗證」 | fail-closed（缺資料就不決策） |
| L2 判準（skill 文本） | 單一無歧義真值 | 模型自由裁量、同case相反決策 | 真值唯一、可 grep |
| L3 算式（`cap_calc.py`） | 把文本的硬條件寫成程式 | 上限過高 → 買貴；上限消失 → **該筆買進被擋**（v3 更正：不是任意價買進，見 §12 C-5）；真正的金錢後果在賣出側 | 每個文本條款都有對應斷言 |
| L4 決策（DAG／驗證／閘門） | 輸出不合格就退讓 | 壞輸出被當好輸出下單 | fail-closed，且有終端狀態 |
| L5 帳本／績效 | 記對錢與基準 | 超額報酬失真 → 錯誤的「系統有效」結論 | 鍵名與口徑可核 |

**核心診斷（本報告的主軸，v2 未變且更硬）**：這個系統在 L3–L5 上把「不確定就別買」的 fail-closed 原則，反過來實作了 **22 條 fail-open 路徑**（§3、§4），v2 再補 **6 條**（N-1、N-2、N-3、N-4、N-8、N-11）→ 共 28 條。（v3 複核：**28 條的計數不變**——這 6 條逐條重驗後都可達；要更正的是其中 2 條的**機制與方向描述**：N-1 的傳導鏈改走「FULL 回寫採用模型自寫的登錄表」（C-1），P0-2 的金錢後果不是「任意價買進」而是「上限變寬＋賣出側失靈」（C-5）。另：v2 的 N-7 整條撤銷、N-5 降 P2、N-10 降文本，但它們本來就不在這 28 條的計數內——它們屬「規則與實作分岔」，不是 fail-open 路徑。）買進側要過 11 關、三閘門、11 題問卷；賣出側與監控側只要任何一欄缺值就靜默不動作。淨效果是一台**難進、更難出**的機器：

- 買進上限可以在關鍵條件缺失時退化為最寬鬆的那條（P0-2），而且模型照 schema 寫 `"iv_cons": null` 就會觸發（N-8）；
- 賣出檢視線在同一批缺失下被寫成 `None`，之後再也不觸發（P0-2、P0-4、N-1）。**v3 更正**：傳導鏈裡沒有 `pipelines/review.py:55` 那一環——`review_due.check()` 對「鍵值為 `None`」與「鍵不存在」行為完全相同（實測見 `test_repro_round3.py::test_none_key_drop_in_review_py_is_a_no_op_for_review_due`），那行只是序列化整潔。唯一的原因是**程式把「缺值」當成「沒觸發」**（`e.get(k) and …` 的假值短路）；
- 最硬的那條賣出線（荒謬級）由框架自己補算，而 `SELL_REVIEW` 在派發時被映射成 `HOLD`（P0-4）；（v3 更正：`review_jobs.py:139-144` 與 `hold_jobs.py:68-88` 確實另有機械式處置路徑，所以「唯一」這個詞我收回，改為「最硬但被映射掩蓋」）
- 兩道「最後防線」（連 N 日高於上限就取消批次、權重超限就減碼）在缺值時**一起被 `continue` 掉**，而且不留日誌痕跡（N-2、N-3）。

一台買進極保守、賣出會靜默失靈的機器，長期期望報酬 ≈ 建倉品質 − 無法退出的尾部風險。這是比任何單一 bug 更嚴重的結構問題。

第二個結構問題在 L2：規則文本對**同一個參數給了 2–3 套真值**，而且優先序自我矛盾（§7）。當文本自相矛盾時，「模型忠於規則」這個假設就不成立了——模型只能在兩條衝突條款裡任選其一，而選錯的那個方向（例如 Q3b 全清 vs 需有更好用途）剛好是虧錢的方向。

---

## 2. 摘要（v2 口徑）

| 等級 | v1 筆數 | v2 裁決後的數 | 一句話 |
|---|---|---|---|
| **P0** | 9 | **9（全數仍成立）** | 報酬數字全錯、買賣閘門可靜默失效、規則硬條款未實作、價格隔離不可稽核；只有 P0-4c 的失靈機制被我寫錯，實際路徑更糟（見 §3A N-1） |
| **P1** | 14 | **12 仍成立＋1 改寫＋1 撤銷** | P1-9（D 級無守衛）主訴撤銷——守衛存在；改成「拼錯的等級無聲當 C 算出上限」。P1-15(b) 撤銷，P1-15(a) 改寫為「未受檢的列被標成已檢」 |
| **P2** | 15 | **13（含 2 條半修、2 條改向）** | P2-5／P2-6 因你今早加的 `rulebook.md:3-4` 與 8 份 banner 而**半修**；P2-1 嚴重度下修（schema 其實有內嵌進提示）；P2-10/11 方向講反了 |
| **v2 新增** | — | **12 → v3 裁決：9 條仍成立（其中 N-1、N-3、N-8、N-11 只更正機制描述或修法）、1 條撤銷（N-7）、2 條降級（N-5→P2、N-10→文本）；另新增 N-13** | 賣出線永久靜默（向量已更正，見 N-1）、建倉批次卡死、C／`None` 級關掉權重超限減碼、驗證器自由文字後門、死輸入（N-6，非失靈）、股票池無點對點版本（資料其實已在表內，修法是便宜的）|
| 設計疑慮 | 8 | **9**（新增 D-9 預算／同意閘門預設不設限）＋3 條「需你確認意圖」 | 不是 bug，但我會改；S-4「漏斗接近空集合」改判部分成立（19 項而非 21 項，A＋1 降仍可買） |

**如果只修五件事**（按「錢的傳播距離」排序；v3 已按 §12 更正第 1、3、5 件）：
1. **登錄表的三條線可能被寫成空值，而缺值＝不檢查**（N-1、N-2、N-3 的共同上游）。v3 更正後的真實向量有兩條，都比 v2 寫的「空模板」更直接：(a) FULL 回寫時登錄表內容**直接採用模型自寫的 `registry_entry.json`**（`writeback.py:195`），框架在 `deep.py:346-357` 自己算出的 `cap` 只覆寫**訊號卡**（`apply_cap_override`），從未與登錄表比對或合併；(b) `writeback.py:561` 把 `cap.get("IV_base")` 的 `None` 直接寫進登錄表。`review.py:55` 那條我撤銷（見 §12 C-1）。
2. `daily.py:294` 的 `a.get("cash")` → `cash_per_share`（所有歷史績效與「系統打敗 SPY」的結論都掛在這一行）。
3. 賣出側改為 fail-closed，並補 N-2／N-3：缺 `sell_review_above`、缺 `auto_buy_cap`、等級不在 A／B 時，必須產生**可見的異常與人工待辦**，而不是 `continue`、`[]` 和 0 日累計。
4. `universe.py:82` 補 `asof`（N-11）＋ `pe_median.py:111-118` 補點對點檢查（P0-6）——這兩條決定「回測與證據檔裡的數字能不能算數」。
5. `validate.py:193-198` 的自由文字降級（N-4）與 THESIS 交白卷（P0-5）：驗證器現在是「形狀檢查器」，模型只要湊齊鍵名就能拿到 `DONE`。

---

## 3. P0 發現（直接影響金錢決策或報酬數字）

### P0-1 基準股利讀了正規化器從不輸出的鍵名 `[T: test_benchmark_dividend_reads_a_key_the_normalizer_never_writes]`

[`trader/trader/pipelines/daily.py:294`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/pipelines/daily.py#L294) 以 `a.get("cash")` 累加基準ETF股利；[`trader/trader/data/alpaca.py:114`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/data/alpaca.py#L114) 寫出的鍵是 `cash_per_share`。

後果鏈：`bench_div` 恆為 `0.0` → `nav_daily.benchmark_dividend`（[`daily.py:297-298`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/pipelines/daily.py#L297)）→ `report/metrics.py:60-65` 的 SPY 序列成爲純價格指數 → 儀表板「SPY 同期（含股利）」與 `excess_pp` 一律偏優。

金額層面：SPY 殖利率約 1.2–1.5%/年，五年就是 6–8pp 的虛超額。**這是全專案最危險的一行**，因為它不影響任何單筆交易，只污染「系統有沒有用」的判斷，而那個判斷正是繼續投入資金的依據。同一份規則要求「每一個數字標來源與日期」（`SKILL.md:40` 鐵律 5），這裡的來源鍵名根本不存在。

修法：改用 `cash_per_share`，並在 `alpaca.py` 出口加一支「正規化器鍵集合 ⊆ 消費端讀取鍵集合」的契約測試。

---

### 【v2：`cap_calc.py` 你未動，本條原樣仍在】P0-2 合併一致性條款會因 `IV_base` 缺失或為 0 而整個消失，並順手抹掉賣出側 `[T: test_auto_buy_cap_survives_when_iv_base_missing, test_zero_iv_base_also_disables_the_clause, test_sell_lines_wiped_to_none_silently_disable_all_sell_checks]`

[`berkshire-deep-merged/scripts/cap_calc.py:161`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L161)：

```python
abc = (min(cap, iv_base) if iv_base else cap) if cap is not None else None
```

`SKILL.md:93` 把 `auto_buy_cap = min(P2′, IV_base)` 定為「合併版一致性條款」，註解還寫「賣出檢視線＝`IV_base`，買價不得高於它」。但 Python 的真值判斷讓 `iv_base in (None, 0)` 時條款整條蒸發，上限直接退化成 `P2′`。

為什麼這常態而非極端：`iv_base` 需要基準情境起始成長 `g0b`；當 `g10`/`g5` 都缺（新上市、分拆、資料包缺欄）時 `g0b=None → iv_base=None`（[`cap_calc.py:96`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L96)）。我的重現案例用 `oe=1, price=80`，實得 `auto_buy_cap == P2′ > IV0 > IV_cons`——也就是**上限被設在比「零成長價值」與「保守價值」都高的位置**，正好是規則最想禁止的那種買價，且 `conclusion_hint` 仍回答「價格 < 自動買進上限」。

同一筆結果經 [`trader/trader/analysis/writeback.py:561`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/analysis/writeback.py#L561) 回寫登錄表：

```python
{"auto_buy_cap": cap.get("auto_buy_cap"), "sell_review_above": cap.get("IV_base"),
 "absurd_price_line": cap.get("absurd_price_line")}
```

`IV_base` 為 `None` 時 `sell_review_above` 被寫成 `None`（合併更新、非留空），而 [`scripts/review_due.py:119`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/review_due.py#L119) 是 `if e.get("sell_review_above") and p >= ...`。**買得比規則允許的更貴，而且此後漲到任何價格都不會被拉去賣出檢視。** 一處真值判斷失誤，同時打穿買進與賣出兩側。

修法：`iv_base` 缺 → 不給上限（回 `MISSING_DATA`），而不是退回 `P2′`；`write_price_result` 禁止把已存在的非空 `sell_review_above` 覆寫為 `None`，並在 `review_due` 對「持有但無賣出線」產生異常而非靜默。

---

### P0-3 零成長／負成長公司的 `IV0` 硬上限完全沒有實作 `[T: test_declining_company_cap_is_not_restricted_to_iv0]`

規則三處一致：
- [`berkshire-deep-merged/SKILL.md`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/SKILL.md#L101)：「歷史成長 ≤0 → 上限只能用 `IV0`（E ÷ `r_d`）×等級係數」
- [`references/rulebook.md:271`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/references/rulebook.md#L271) 與 [`:276`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/references/rulebook.md#L276)：「P2 只能用零成長價值 `IV0`」

[`cap_calc.py:133-134`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L133) 對此只 `notes.append(...)`，`P2` 仍按 `G_IMPL_MAX` 正成長路徑算。我的測試用 `g10=g5=-0.03`，實得 `cap` 明顯高於 `IV0 × 0.85`（A 級上限）。

方向性極糟：這是全系統唯一被規則明令「只能用零成長價值」的族群——也就是本業在衰退的公司。把它們的買價上限抬高，等同對價值陷阱放水。初篩的貢獻（避開壞公司）會被這一條抵消。

---

### P0-4 賣出側四條靜默失靈路徑 `[T: test_missing_price_data_fails_open_for_selling, test_add_trigger_requires_target_weight]` ＋ `[V]`

| # | 位置 | 行為 | 後果 |
|---|---|---|---|
| a | [`trader/trader/jobs/review_jobs.py:145-147`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/review_jobs.py#L145) | `SELL_REVIEW → "HOLD"`，註記「賣出檢視待組合比較（M5）」 | `PRICE.md:15` 依規則要求模型在 `現價 ≥ sell_review_above` 時產出 `SELL_REVIEW`；框架把它變成**無條件不動作**。「有明顯更好用途就換股」這條賣出路徑，目前沒有任何替代實作。系統實際上是只進不出的 |
| b | [`scripts/review_due.py:116-124`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/review_due.py#L116)（v2：行號漂移） | 賣出線檢查全在 `if status == "HOLDING"` 之內 | 登錄表狀態被任何一次 STOP／拒絕覆寫後（見 P1-3、N-1），賣出觸發永久失效且不會自己修好 |
| c | **【v2 更正機制，結論仍成立】** [`review_due.py:105`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/review_due.py#L105) `if px is not None and len(px)`；「無價格資料：價格類觸發未檢查」的警告（`:182-183`）只在 `main()` 附加 | v1 我寫成「被 `trader/` 當模組呼叫時靜默」——實際 [`pipelines/review.py:180-199`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/pipelines/review.py#L180) 是用**子程序**叫 `main()`，那條路徑並不存在 | 但失靈照樣成立：無價 → 全部價格觸發跳過 → action `NONE` → `review.py:240` `continue` 不建工作。`check()` 的回傳契約裡根本沒有「未檢查」這個狀態 |
| d | [`review_due.py:123`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/review_due.py#L123) | `(e.get("weight") or 0) < (e.get("target_weight") or 0)` | 缺 `target_weight` 時右邊為 0 → 條件恆假 → 「跌進買價上限該重檢」這條觸發器靜默關掉。**v2 更正方向**：`sizing.py:305-308`／`:83-85` 對「無上限／無等級」是 fail-closed（跳過不買），所以這條只讓「該不該重檢」消失，不會造成亂加碼 |

b/c/d 三個都是「欄位缺值 → 條件式短路 → 檢查從未執行且不留錯誤」。**在錢的系統裡，缺資料的正確反應是停止交易並報人工，不是回傳「沒有觸發」。** v2 補：這條原則在 `trader/` 端被 `pipelines/review.py:55`（刪除 `None` 鍵）與 `sizing.py:477-479`（`continue`）反覆重演，見 §3A N-1／N-2。**v3 更正**：`review.py:55` 那一半我收回（§12 C-1）——實測 `check()` 對「鍵值為 `None`」與「鍵不存在」輸出相同，那行只是序列化整潔；`sizing.py:477-479` 的 `continue` 仍成立（N-2／N-3）。

---

### 【v2：你今早改過 `jobs/validate.py` 與 `jobs/context.py`，P0-5(2) 這條仍在】P0-5 論點複查（唯一的持有期監控）有兩條 fail-open `[T: test_invalid_thesis_decision_is_invisible_to_add_blocked, test_thesis_check_passes_validation_with_zero_evidence]`

**(1) 驗證失敗的 THESIS 決策對加碼閘門不可見。**
[`trader/trader/jobs/handlers.py:111-124`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/handlers.py#L111) 的基底 `on_invalid` 寫入的決策 `data` 只有 `{"job_type", "errors"}`，**沒有 `review_type`**。而 [`sizing.py:244-249`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/portfolio/sizing.py#L244) 的 `add_blocked` 只認帶 `review_type` 的列，會一路跳過、回退到**更早的那張「完好」卡**。我實際用框架自己的 `Handler().on_invalid(...)` 產生該列，再呼叫 `add_blocked` → 回傳 `None`（可加碼）。

對照組證明這是遺漏而非設計：`DOSSIER`／`FULL_A`／`FULL_B`／`RED_TEAM`／`CORP_ACTION`／`HOLD_REVIEW`／`TRIAGE` 全部覆寫了 `on_invalid`（`deep.py:245/289/383/425`、`corp_jobs.py:91`、`hold_jobs.py:171`、`review_jobs.py:217`），唯獨 `ThesisHandler` 與 `PriceHandler` 用基底版——而 THESIS 正是加碼閘門的資料來源。

**(2) 交白卷的論點複查照樣合格。**
[`validate.py:93-101`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/validate.py#L93)（空陣列不遞迴、故無錯誤）＋ [`:205-209`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/validate.py#L205)（自由文字段接受 `""`）。我送進 `kill_criteria_status: []`、`r_list_status: []`、`new_facts: []`、`reason: ""`、`verdict: "完好"` → `errors == []`；寫回後 `thesis_check="完好"` → `add_blocked` 放行。登錄表明明記著 `kill_criteria: ["市占連續兩季下滑"]`，**複查輸出與失效條件之間沒有任何比對**。

疊加 [`review_jobs.py:358-363`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/review_jobs.py#L358)：`ctx.sec is None` 或查不到 CIK 時 `pre_script` 直接 `return`（不記註、不報缺），模型只拿到論點檔就判「完好」。在決策日誌裡，「沒有新資料所以沒查」與「查過沒有變化」完全同形。

---

### P0-6 凍結 P/E 檔沒有點對點檢查 → 回測吃未來 `[T: test_frozen_pe_median_file_is_returned_even_when_it_looks_ahead, test_frozen_pe_median_prev_month_also_unchecked]`

[`trader/trader/data/pe_median.py:111-118`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/data/pe_median.py#L111) 只判斷「這個月的檔存在嗎」，存在就整包回傳，**不比對檔內 `asof` 是否 ≤ 查詢 `asof`**。我寫入 `cache/pe_median/2026-10.json`（內含 `asof=2026-10-20`），以 `asof=2026-10-03` 查詢 → 拿到 10-20 的數值。退退回上月時同樣不檢查。

這直接違反 [`rulebook.md:405`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/references/rulebook.md#L405)（母體統計以凍結檔提供、訊號卡記檔名與日期）背後的時點紀律。受影響的是 `P4′ = k × 大型股 P/E 中位 × E`（`cap_calc.py`）與 W-PE 分區，而且 **`--asof` 往回跑的任何重播、以及 handoff 提到的 M5 風格模擬都吃得到**——這類前視不會讓測試變紅，只會讓「這套規則有效」的結論變得不值錢。

修法：`get_frozen` 必驗 `stored["asof"] <= asof`，否則往更早的月找；找不到就回 `None`（規則已定義 `P4′ 不適用` 的路徑）。

---

### P0-7 閘門 3 的價格隔離在工程上不可稽核 `[V]`＋ `[T: test_gate3_price_file_timestamp_is_optional]`

自動買進的三閘門裡，只有閘門 3 是靠「模型真的沒看價格」才有意義的。三處實作讓這個前提無法被證明：

1. **隔離採 deny-list，且沙箱根與真價格檔同層共存。**
   [`trader/trader/jobs/isolation.py:18`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/isolation.py#L18) 封鎖家目錄／`/private`／`/tmp`／專案根／state；[`sandbox.py`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/isolation.py#L76) 只對清單內目錄下 `Read/Edit/Write`。沙箱預設根是 `/Users/Shared/berkshire-sandbox-<user>`，**不在封鎖清單內**。我在本機實測該目錄確實存在，且裡面留有真價格檔：
   `/Users/Shared/berkshire-sandbox-zongen/analyze-2026-10-16/OTIS/_FULL_B_1/dossier/OTIS_price.json`
   隔離工作（THESIS／TRIAGE／DOSSIER／FULL_A）只要用絕對路徑讀同根的兄弟目錄就取得到真價格。**v2 更正權限**：沙箱根本身現在是 `drwx------`（700，來自 [`isolation.py:71`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/isolation.py#L71) 的 `chmod 700`），v1 寫的 1777 其實是上一層 `/Users/Shared` 的權限；這不影響結論——同一 uid 下的隔離工作**本來就能讀寫**該目錄，而跨沙箱可寫意味著可以偽造兄弟沙箱的 `gate3_answers.json`（[`claude_code.py:41`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/llm/claude_code.py#L41) 給了 `Write`），[`_snapshot`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/llm/claude_code.py#L15) 只比對自己那個沙箱。另 [`isolation.py:67-73`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/isolation.py#L67) 的 `prepare_root` 用 `mkdir(exist_ok=True)`＋`chmod`，不檢查同名既有目錄的擁有者。
2. **scrub 只認 `.json` 的鍵名，不查值、不查 `.md`/`.txt`。**
   [`sandbox.py:114-122`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/sandbox.py#L114)：非 `.json` 一律 `copyfile`；[`is_price_key`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/sandbox.py#L49) 只看鍵名。資料包 `.md` 裡的收盤價、SEC 節錄裡的成交價、藏在 `"close": "123.45"` 之外任何形式的價格都留得下來。handoff 已把「平均回購價殘留」列為已知問題——同一根因。
3. **開檔閘門只比 mtime，且「有沒有先看價」由模型自報。**
   [`sandbox.py:148-166`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/sandbox.py#L148) 只看 `gate3_answers.json` 的 mtime；`touch` 或複製檔案即可繞過，答案檔也未綁 `run_id`/`job_id`。[`gate3_check.py:26-28`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/gate3_check.py#L26) 的 `if po and at and po <= at` 讓 `price_file_opened_at` 成為**選擇性**欄位（我的測試證明缺欄照過），而 `price_seen_before_answering` 是模型自己寫的布林值（`procedure/12:20` 要求該聲明為 false）。

三閘門是「自動下單」的唯一授權來源。它的第三閘目前只剩「檔案時間戳先後」這種可偽造的痕跡，而規則把它寫成不可穿越的前提（`SKILL.md:38` 鐵律 2）。

---

### 【v2：你今早改過 `routing/output_schemas.json`，Q3b／Q3c 仍缺】P0-8 賣出決策樹的 Q3b／Q3c 在 schema 裡無法表示 `[T: test_hold_review_schema_cannot_record_the_branches_the_prompt_demands]`

[`routing/output_schemas.json`](file:///Users/zongen/Downloads/claude%20code/berkshire/routing/output_schemas.json)（`HOLD_REVIEW.path[].q`）的列舉是 `Q0 | Q1 | Q1b | Q2 | Q3 | Q4 | Q5 | Q6`。缺兩個實際存在的分支：
- `rulebook.md:370` 的 `3b`（「只有 V、普通／良好企業、價格 ≥ `IV_base` → 全清」）
- `rule_index/04_hold_sell.md:231` 的 `Q3c`（頂級複利型分批減碼），而 [`routing/prompts/HOLD_REVIEW.md:24`](file:///Users/zongen/Downloads/claude%20code/berkshire/routing/prompts/HOLD_REVIEW.md#L24) **明確規定「TRIM 只用於 Q3c」**。

實測：`q="Q3c"` 與 `q="Q3b"` 都驗證失敗，`q="Q3"` 才通過。後果有兩種，都壞：
- 模型忠實記錄 → `invalid_output` ×2 → [`hold_jobs.py:171-180`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/hold_jobs.py#L171) 的 `on_invalid` 保守 `HOLD`＋停止加碼。等於**老實寫出售出路徑反而賣不掉**，而且燒掉一次強模型呼叫。
- 模型（或未來的自己）為了過閘把 `Q3c` 寫成 `Q3` → 稽核鏈斷掉，`procedure/13` 的前瞻驗證樣本被汙染。

同一測試也證明 `path: []` ＋ `action: "SELL_ALL"` 完全合格：可以清掉一檔而不留任何決策樹痕跡。

---

### P0-9 公司行動去重鍵與資料庫唯一鍵不一致 `[T: test_detect_drops_a_second_corp_action_on_the_same_effective_date]`

[`trader/trader/db.py:46`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/db.py#L46) 的約束是 `UNIQUE(ticker, action_type, effective_date)`（允許同日不同類型），但 [`ledger/corp_actions.py:241-250`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/ledger/corp_actions.py#L241) 的去重鍵是 `(ticker, effective_date)`（`_ca_exists` 於 [`:70-73`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/ledger/corp_actions.py#L70) 同樣只用兩鍵）。

實測：先記一筆同日「拆股」，餵進同日的現金股利 → `detect()` 回傳空清單；把股利日期錯開一天 → 正常偵測。也就是**同日「拆股＋股利」、「改名＋股利」等組合中，第二種行動永遠不被處理**：該股利的現金不會進帳，除息造成的跳空也不會被解釋，而 `detect` 的靜默沒有異常紀錄。拆股當除息的場景在真實市場並不罕見（例如分季股利與拆股同生效日）。

---

## 3A. 第二輪新增缺陷（依 09:19 之後快照，v1 未收錄）

以下 12 條全部是本輪重驗時新找出或新看清的；標 `[T]` 者有 `test_repro_round2.py` 重現（N-9、N-12 以檔案系統與原始碼檢查為證，無 pytest）。

> **v3 讀法**：本節 12 條的「是否真實存在」已逐條重驗，結果寫在 §12。其中 N-7 整條撤銷、N-5 降為 P2、N-10 降為文本問題、N-1／N-3／N-8／N-11 的機制描述已就地更正，並新增 N-13。

### N-1 `[T]` 登錄表的三條估值線可以合法地變成空值，之後賣出線與加碼線「永久」靜默（v3 已更正傳導鏈與向量）
**v3 更正後的機制**（`test_repro_round3.py::test_full_writeback_drops_sell_lines_when_model_entry_omits_them`、`::test_full_lane_never_reconciles_registry_entry_with_cap`）：
1. FULL 路徑的登錄表內容來自模型自己交回的 `registry_entry.json`（[`writeback.py:195`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/analysis/writeback.py#L195)），框架只額外保留 `cost/first_buy_date/weight/build_plan` 四欄（`:248-251`）；`auto_buy_cap`／`sell_review_above`／`absurd_price_line`／`grade` **既不保留、也不與 cap 比對**（upsert 整列 → 舊值被覆蓋為缺值）。實測：一列 `HOLDING` 回寫後 `cost=70.0` 還在，三條線全變 `None`。
2. 框架其實**手上有正確數字**：[`deep.py:346-357`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/deep.py#L346) 自己跑 `run_cap_calc`，不一致時 `apply_cap_override(card, cap)` 覆寫**訊號卡**並記 `framework_notes`。也就是說規則的「以腳本為準」只落地在卡上，登錄表那一份沒有對應的比對點——兩者可以長期分岔而無任何註記。
3. 之後 [`review_due.py:119-124`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/review_due.py#L119) 用 `e.get(...) and ...` 判斷，缺值＝不觸發。實測（v3 對照組）：同一份輸入、收盤 100→300，補回三條線就出現兩條 `[PRICE]` 賣出檢查；缺線時**一條都沒有**。
4. 缺值是長久的：只有重跑完整分析才會補回，而 `write_full_result` 把 `last_full_review_date=asof`（`:241`）→ 完整複查已被推遲。
（v2 原寫的「空模板 → `review.py:55` 刪 `None` 鍵」這條鏈，第 2 環在 v3 撤銷、空模板一環限定為「非 HOLDING」時才適用，見 §12 C-1／C-3。）

### N-2 `[T]` 缺上限會把建倉批次卡死，而且連「超漲 10 日自動取消」的逃生口一起停擺
[`trader/trader/portfolio/sizing.py:477-479`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/portfolio/sizing.py#L477)：`if px is None or cap is None: continue`——不產生計畫、不寫原因、不記異常；同一條件在 [`:528-530`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/portfolio/sizing.py#L528) 讓 `tick_build_state` 不再累加 `days_above_cap`，於是 [`:474`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/portfolio/sizing.py#L474) 的「連 `CANCEL_ABOVE_CAP_DAYS` 日高於上限 → 取消剩餘批次」永遠不會觸發。實測（`KO`，`auto_buy_cap=None`，報價 999 對建倉價 60）：`plan_tranches` 回 `([], [])`，`tick_build_state` 後 `days_above_cap` 仍 0。**半個建倉計畫無限期懸在那裡，日誌上完全無痕**；而 N-1 正是 `cap=None` 的生產者之一。

### N-3 `[T]` 機械式權重超限減碼對 C／D／`None` 級完全關閉（v3：把「唯一出口」的說法改窄）
[`sizing.py:23`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/portfolio/sizing.py#L23) `GRADE_CAP = {"A": 0.15, "B": 0.10}`、[`sizing.py:414-416`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/portfolio/sizing.py#L414) `cap is None → continue`。等級不在 A／B（含 `grade` 為 `None` 或被降級成 C）時，持股膨到多少都不產生 TRIM 計畫。實測：`PEP` 建倉超過一年、市值對照權益 54%，`plan_sells` 回 `[]`。系統又刻意沒有價格停損（`rulebook.md:316`），所以這條一關掉，剩下的**價格類**出口只有 `review_jobs.py:139-144` 的荒謬級 → TRIM（需要登錄表有 `absurd_price_line`，也就是仍依賴 N-1 那三條線存在）與 LLM 的 HOLD_REVIEW 輸出。**v3 兩處更正**：(1) 我 v2 說「該部位剩下的唯一出口就是 LLM」誇大了——`hold_jobs.py:68-88` 與荒謬線確實是另外兩條機械路徑，但它們都讀同一批可能缺值的線，所以「共同上游仍是 N-1」這個判斷不變；(2) 觸面來源我原本暗示 RED_TEAM 判 D 會直接反映到 `registry.grade`，實查**沒有任何程式把 RED_TEAM 的 `fatal`／`certainty_grade` 同步進登錄表的 `grade`**（`deep.py:406-428` 只設 `final_signal=WATCH`），所以等級是「模型自己寫進 registry_entry 的那個字串」，拼錯時另有 P1-9 改寫那條（無聲當 C 算上限）處理。

### N-4 `[T]` 驗證器有自由文字後門，DOSSIER 的資料品質自我檢查等於沒驗
[`trader/trader/jobs/validate.py:193-198`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/validate.py#L193)：選項字串只要任一項含 `…`／`..`／`：`，**整個欄位降為「任意字串」**。現行 schema 唯一命中的是 `DOSSIER.dq_checks[].result`（spec「已核對：原因… | 數字有誤：正確值…（來源） | 無法核對」）→ 我送 `result: ""` 與 `result: "我根本沒核對：随便寫"`，`errors` 裡**沒有任何 dq_checks 相關錯誤**。DOSSIER 是後續所有工作的輸入來源，這一欄正是「資料有沒有被核過」的憑證。
（同條更正：代理一度主張 `FULL_B.stopped_at_gate`（`G7..G11 | null`）也中招；我重跑後不成立——`_expand_ranges`（`:131-140`）在自由文字判斷**之前**把 `G7..G11` 展開，`..` 消失。只有 `DOSSIER` 那一欄真的漏。）

### N-5 `[T]` → **v3 降為 P2**：`cap_calc` 私自替終值成長加下限（偏差真實，但幅度被我誇大）
規則三處（`SKILL.md:90`、`rulebook.md:276`、`procedure/05`）都是 `g_term = min(3%, 30Y − 1pp)`，**可為負**；[`scripts/cap_calc.py:126`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L126) 寫成 `min(0.03, max(0.0, L - 0.01))`。同一檔裡 `iv_base`／`iv_opt`（`:96-97`）又硬編 `0.03`、保守情境用 `g_term_c` → 一筆輸入三種終值假設。
**v3 實測幅度**（`test_repro_round3.py::test_g_term_floor_effect_is_small_and_regime_dependent`，固定 `r_d = max(0.10, L+0.03)`）：30Y=0.9% → 上限差 **+0.50%**；0.5% → **+2.44%**；0% → **+4.77%**；**30Y ≥ 4%（含目前的利率區間）→ 0%**。所以我 v2 寫的「低利率時**系統性**放寬買進上限」在方向上正確、在幅度上誇大，且只在利率跌回 <2% 時才有意義 → 從 P1 降為 **P2（規則忠實度問題，不是当下的金錢風險）**。「移除 `max(0.0, …)`」仍建議做，但它不該排進前兩輪修補。

### N-6 `[T]` 文件宣示的輸入欄是死代碼：`incr_roic_gt_rd`
[`cap_calc.py:20`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L20) 自陳它是「A 級 P4′ 1.2 倍條件之一」，`assets/cap_calc_example_input.json` 也確實帶這欄；但 `:142/:144` 從不讀它（全 repo grep 只有這兩處文宣與例檔）。實測金融類同一組輸入、旗標 True 對 False：`cap` 完全相同。
**同時修正查核代理的一條推論**：代理稱「A 級無條件拿到 1.2 倍、把硬上限放大」也不正確——`k4=1.2` 只給非金融，而非金融的 `cap` 走 `P2′`（`:153`），1.2 倍只落在軟上限報告欄。真正會把 P4′ 當硬上限的金融類，走的是 `:142`（乘數 1.0 或 `fpm×1.1`），與該旗標無關。缺陷是「文宣與實作分岔」，不是「上限被放大」。

### N-7 **v3 撤銷**：`g_impl_gt_cap` 不是缺陷——它對閘門 2 是冗餘資訊
[`SKILL.md:99`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/SKILL.md#L99)：「`g_impl` 為 null 且 `g_impl_gt_cap`＝true → 閘門 2 不過」。我 v2 據 `review_jobs.py:291-298` 未讀該旗標就判定「規則沒實作」。**v3 反證**（`test_repro_round3.py::test_g_impl_gt_cap_flag_is_redundant_for_gate2`）：掃價 120／150／200／400／1000／4000／40000，凡旗標亮起者 `price ≥ cap` 恆成立（7 個樣本中 5 個亮起），而 gate2 本來就要求 `price < auto_buy_cap`（`auto_buy_cap = min(cap, iv_base) ≤ cap`）→ **這些情形早就過不了閘門 2**，讀不讀旗標結果一樣。所以 §9 第 4 項裡「gate2 補讀 `g_impl_gt_cap`」這条建議我撤銷。
仍然成立的是那一條**邊界誤標**：`solve_g` 上界是 `min(0.5, r_d - 1e-4)`（[`cap_calc.py:50-51`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L50)）＝0.0999，比 `P2′` 的定義成長 `G_IMPL_MAX=0.10`（`:33`）小，「剛好高於 P2′」的窄帶會回傳 `None` 而被標成「隱含成長遠超上限」。這是**標記欄位的邊界瑕疵**（不影響下單，影響報告敘述），降為 P2。

### N-8 `[T]` 顯式 `null` 會殺掉 fallback（規則慣例與 `dict.get` 的衝突）
`cap_calc` 全用 `x.get(key, default)`（`:95-97`），而 `output_schemas.json:96-98` 的慣例是「數字或 null」——模型照規則寫 `"iv_cons": null` 時，`get` 回傳 `None`（不是 default），直接觸發 `:100` 早退（無上限）；`"iv_opt": null` 讓 P-21 區間寬度檢查整條跳過（同 P1-8 的根因）。這是 schema 與實作之間的**格式層矛盾**，不是模型偶爾漏鍵。
**v3 方向更正**（與 §12 C-5 同一件事）：`auto_buy_cap` 因此變 `None` 時，[`review_jobs.py:291-298`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/review_jobs.py#L291) 把 gate2 記成 `"無上限"`，而 `passed` 要求 `gate2 == "通過"` → **買進側是 fail-closed（不買）**。所以 N-8 的真實代價有兩塊：(a) 該檔被無聲地跳過評估（規則想要的是降級而非拒絕）；(b) **賣出側**：同一組 `None` 經 `write_price_result`（`writeback.py:534/:561`）覆寫登錄表，把原本存在的 `sell_review_above`／`absurd_price_line` 蓋成 `None` → 回到 N-1 的靜默狀態。(b) 才是金錢風險，(a) 是機會成本。

### N-9 `[T]`/`[V]` 價格隔離：沙箱彼此可讀、可寫，且框架自己的時間戳沒人在看
1. 沙箱根 `/Users/Shared/berkshire-sandbox-zongen`（[`isolation.py:18`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/isolation.py#L18) 的 deny 清單沒有它，也無法有——沙箱自己要在下面），因此任何隔離工作可用絕對路徑 `Read`／`Glob` 取到**別的 run、別的股**留下的真價檔。實測仍存在：`.../analyze-2026-10-16/OTIS/_FULL_B_1/dossier/OTIS_price.json`（`"price": 70.39`），同 uid 可讀。我**沒有**真的呼叫模型去讀它（那需要跑 pipeline），所以「模型會不會去 glob」仍是推論；「它能讀」是事實。
2. [`llm/claude_code.py:41`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/llm/claude_code.py#L41) `--allowedTools Read,Write,Glob,Grep`：模型在同一根目錄不只可讀，**可寫** → 可以植入／竄改兄弟沙箱的 `gate3_answers.json` 來伪造 mtime 順序，而 [`_snapshot`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/llm/claude_code.py#L15) 只比對「自己這個沙箱」內的檔案，跨沙箱改動不會被發現。
3. 框架自己寫下的權威時間戳 `price_file_opened_at.txt`（[`sandbox.py:33/:153`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/sandbox.py#L153)）**從未被閘門 3 讀過**——`gate3_check.py:26` 讀的是模型自報的 `price_file_opened_at` 欄位。兩套時間從不比對，等於開檔順序由被審查者自己填寫。（專案測試 `test_jobs_io.py:451` 會讀那個 `.txt`，但只驗檔案內容、沒接進 gate3。）

### N-10 `[T]` **v3 降為「規則與機制不相應，不可被利用」**：`RED_TEAM` 未列入價格隔離
[`models.py:13`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/models.py#L13) `PRICE_ISOLATED_JOBS = {DOSSIER, TRIAGE, THESIS, FULL_A}`；而 `routing/output_schemas.json` 的 `RED_TEAM.rule_violations` 範例文字正是「違反 SKILL 鐵律或步驟的地方（例如**用價格作答 G1–G6**、補假設值、用預測估值）」。
**v3 下修**：RED_TEAM 排在 FULL_B **之後**（`model_routing.json:20/33`、`deep.py:366`），此時價格檔依程序已合法開啟，把它加入 `PRICE_ISOLATED_JOBS` 既做不到、也沒有意義——它看的本來就該是含價格的完整報告。所以這裡沒有可利用的洩漏面，剩下的只是**文字與機制的對應問題**：schema 把「用價格作答 G1–G6」列為 RED_TEAM 可舉報的違規，而 G1–G6 的判定發生在 FULL_A。屬 skill 撰寫類（並入 §7），不是安全缺陷。§9 第 8 項裡「把 RED_TEAM 列入隔離」的建議因此撤銷。

### N-11 `[T]` 股票池取用不看 `asof` → 歷史重播都用「今天的成分股」（v3：更正「只寫不讀」，並把修法改為便宜版）
[`trader/trader/data/universe.py:82`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/data/universe.py#L82) `load_universe(paths, conn, http, refresh, max_age_days)` **沒有 `asof` 參數**（`inspect.signature` 斷言），`inputs.py:81` 在 `fetch_inputs(asof=歷史日)` 內直接呼叫它（可達路徑：`cli.py:34`，含 `bootstrap --asof`）；寫進 `universe_versions` 的 `asof` 其實是「今天」（`:59` 用 `datetime.now`）。後果：任何 `--asof` 回跑都以今天的 S&P 500 名單評選歷史標的（倖存者偏差＋前視），並連帶讓 `pe_median` 的母體統計失真。
**v3 兩處更正**：(1) 「該表全專案只寫不讀」是**錯的**——[`universe.py:55`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/data/universe.py#L55) 會讀最新一列的 `sha256` 做變動偵測（見 `test_repro_round3.py::test_universe_versions_table_is_read_not_write_only`）；(2) 更重要的是該表**已經存了完整 `tickers` 清單**（`db.py:53` schema：`asof, source, tickers, sha256`），所以修法不是「新建點對點資料層」，而是 `load_universe` 加一個可選 `asof` 並回查「最後一次變動 ≤ asof」的那份名單（約 6 行）。我 v2/§9 把它寫成資料層級工程是**過度設計**，見 §12 T-3。
仍屬推論的部分：`evidence_*.md` 那批證據（含 D-3 的 +19%）其時點有效性無法由現行資料層證明——我沒有重跑那批證據。

### N-12 `[T]`/`[V]` 公司行動往未來多取 60 天，而且原文整包進提示
[`pipelines/daily.py:41/:198`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/pipelines/daily.py#L198) 用 `end = d + timedelta(days=CA_PROCESS_LAG_DAYS)`（60 曆日）向 Alpaca 取行動，過濾只有 [`corp_actions.py:149`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/ledger/corp_actions.py#L149) 的 `prev_date < d <= date`（比 ex_date），**不比「該行動是否在 asof 前已公布」**；`:160` 又把 `原文 {json.dumps(raw)}` 全量塞進 CORP_ACTION 提示，而 raw 含 `process_date`／`record_date`／`payable_date`（可晚於 asof）→ 模型在決策日看到日後才會確定的資訊。修法需要「公布日 ≤ asof」的條件，以及提示端只給欄位子集。

### N-13 `[T]`（v3 新增）模組層循環匯入：`trader.jobs.review_jobs` 無法單獨匯入
`python3 -c "import trader.jobs.review_jobs"` 在任何目錄都拋 `ImportError: cannot import name 'DossierHandler' from partially initialized module 'trader.jobs.deep'`。鏈是 [`review_jobs.py:21`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/review_jobs.py#L21)→[`deep.py:28`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/deep.py#L28)→`handlers.py`→`handlers.py:143` 尾端 `from .deep import DossierHandler, ...`，此時 `deep` 只執行到第 28 行，那些類別還沒定義。先 `import trader.jobs.handlers` 就正常（實測見 `test_repro_round3.py::test_review_jobs_cannot_be_imported_standalone`，兩個方向都驗）。
專案自己的 `tests/test_review_jobs.py:18` 之所以能跑，是因為 pytest 先匯入了別的模組（`--collect-only` 單檔可過）。**影響**：不是執行期 bug，是「靠匯入順序才成立的架構」——任何新入口（CLI 子命令、單檔除錯、把 handler 拆出去）踩到就直接崩潰，而且崩在建組階段而非測試階段。修法很便宜：把 `handlers.py:143` 那行反向匯入移到需要使用它的函式內部（延後綁定），或把 `deep` 的類別註冊改成由 `jobs/__init__.py` 明確做一次。


### 另外三條（列為需你確認意圖，不自動算 bug）
- `[V]` **預算只按 run 累計**：[`engine.py:212`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/engine.py#L212) `WHERE run_id=?`，每日新 run 即重新計；`config.py:42-43` 允許 0＝不限，且 `max_input_tokens_per_run` 預設就是 0。
- `[V]` **同意閘門可整扇關閉**：`config.py:69`＋`approvals.py:31-33`，`approval.require_for_tiers: []` → `required()` 對所有 tier 回 False；`config.example.yaml:65` 把 `[] ＝ 不需同意` 寫成文件化用法。是設計選擇，但無警告、不在決策日誌留痕。
- `[V]` **提示與驗證可能不同版**：[`context.py:82`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/context.py#L82) 每次組提示重讀 `output_schemas.json`，而 `engine.py:49` 用常駐快取；另外 [`context.py:49-54`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/context.py#L49) 的 `render_template` 對沒提供的佔位符**原樣保留** → 忘帶 `values` 時提示裡留 `{kill_criteria}` 字面文字，模型在無資料下仍會輸出合格 JSON（fail-open）。

---

## 4. P1 發現

| ID | 位置 | 問題 | 等級 |
|---|---|---|---|
| P1-1 | [`llm/router.py:22`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/llm/router.py#L22) | `deny_roots(extra_deny)` 未帶 `state_dir`；`engine.py:68` 卻帶了。用 `--state-dir` 指向專案外時，實際送給 `claude --settings` 的封鎖清單**完全不含 state**（registry、cap_inputs、work 價格檔都在外面）。默認 state 在 `PKG_ROOT/state` 下，所以只有換目錄時才露出——典型的「測試用預設值通過、部署配置失守」 | `[V]` |
| P1-2 | [`jobs/validate.py:86-88,93-101,191,205-209`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/validate.py#L86) | 驗證只查「鍵存在＋列舉值」，不查內容：空陣列、空字串、字串 `"null"` 全合格。對 action／verdict／evidence 這類載荷欄沒有任何 min_length 或覆核。P0-5(2) 的交白卷路徑就建在這裡 | `[T]` |
| P1-3 | [`analysis/writeback.py:333-347`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/analysis/writeback.py#L333) ＋ [`jobs/approvals.py:171`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/approvals.py#L171) | 非 HOLDING（含等待買進的 WATCH）走**空模板覆寫整列**：`auto_buy_cap`／`sell_review_above`／`grade`／`thesis_file`／`kill_criteria`／`t30_at_valuation` 全變 `None`，同時把 `last_full_review_date` 設為 asof（→ 短期內不再重排完整分析）。任何一次拒絕或 STOP 都把該檔的估值記憶清乾淨 | `[T: test_write_stop_result_wipes_a_watch_entrys_valuation]` |
| P1-4 | [`jobs/approvals.py:206-213`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/approvals.py#L206) | `add_block_reason` 只認 `pending/deferred/approved`。**使用者拒絕強模型分析 → 該檔從未取得上限 → 加碼阻擋理由為 `None`**，加碼與否改由可能過期的舊決策決定。同意閘門的反向保護不存在（M6 只設計了「未同意時不給買」，沒設計「拒絕後不給加」） | `[T: test_rejected_approval_does_not_block_adding]` |
| P1-5 | [`jobs/approvals.py:57-68,132-145,179-190`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/approvals.py#L132) `[T]`（v3） | **v3 已端到端實測並降級為 P2**：某 (run_id, ticker) 的列變 `rejected` 後，`gate()` 仍回 False 但**不重建 pending 列**、`approve()` 對 rejected 拋 `ApprovalError`、`release()` 只撈 `approved` → 同一 run 內反悔沒有出口（`test_repro_round3.py::test_rejected_approval_row_is_a_dead_end_within_the_same_run`）。但同一支測試也證明**下一個 run 會重建 pending 列**，而 [`approvals.py:4`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/approvals.py#L4) 的文件本來就寫「rejected：下次觸發再排」→ 這是「同 run 反悔要等下次排程」的可用性缺口，不是鎖死。故從 P1 降 P2，且我 v2 寫的函式名 `ensure()` 是錯的，實際是 `gate()` | `[T]`（v3 實測，含對照組） |
| P1-6 | [`scripts/cap_calc.py:153`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L153) | `capc = [v for v in (P2,) if v is not None] or ([P4] if P4 is not None else [P1])` — 金融類把 **P4′（軟上限，規則明載「超過須在 G9 寫理由」）** 當成硬上限來源；`rulebook.md:405` 說母體統計缺席時上限＝`min(P1,P2,P3)`，與此處的 P4 優先順序相反 | `[V]` |
| P1-7 | [`cap_calc.py:116-122`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L116) | `def ivc_at(r, oe_=oe)` 在 `x["iv_cons"]` 存在或 `insurance` 時**忽略壓力用的 `oe_`**，只換折現率。結論檢查（壓力情境 E×0.9）的獲利腿是無操作。凡模型直接供 `iv_cons` 的case，壓力測試只剩利率一半 | `[T: test_conclusion_test_earnings_leg_ignores_stressed_earnings]` |
| P1-8 | [`cap_calc.py:106-109`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L106) | `width = iv_opt/iv_cons if iv_opt else None`，`P-21`（區間過寬 → 降級／不做）只在 `iv_opt` 存在時評估。缺樂觀情境＝自動免測；而樂觀情境正是模型最容易不寫的那個 | `[T: test_p21_width_check_skipped_when_iv_opt_missing]` |
| P1-9 | **【v2 撤銷主訴＋改寫】** [`cap_calc.py:112-114`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L112) | v1 我寫「`GRADE_CAP` 無 D、代碼不擋 `grade="D"`」——**錯**：`:112-113` 有 `if grade == "D": no_cap = True`，`:159-160` 把 `cap` 與 `auto_buy_cap` 都設 `None`（`[T: test_grade_d_is_already_blocked_my_old_p1_9_was_wrong]`）。真正殘留的是下一行：`GRADE_CAP.get(grade, "C")`（`:114`）對**拼錯或遺失的等級**（`"a"`、`"Ａ"`、`None`、空字串）無聲借用 C 的係數繼續算上限，既不報警也不留註記（`[T: test_unrecognised_grade_is_silently_treated_as_c]`）。規則 P-16「D 不做」已被老實實作；「等級必須是 A/B/C/D 四個字面值之一」沒有被實作 | `[T]`（主訴撤銷，改寫條有綠燈） |
| P1-10 | [`berkshire-screen-merged/scripts/screen.py:184`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-screen-merged/scripts/screen.py#L184) | `"special_watch": records(df[df.lane...])` 沒帶 `split=True`，而主流交棒塊（`:176`）有。特殊情境區塊**原帶 `price`／`market_cap`／`core_rank`／`research_priority`**（`PRICE_COLS`，`:161`）進交接檔。鐵律 2（G1–G6 與閘門 3 作答時資料包不得含股價、市值、本益比、初篩排名）在特殊情境這條路直接被違反——而那正是唯一宣稱有正向證據（+19%）的族群 | `[T: test_screen_price_columns_leak_into_special_watch_block]` |
| P1-11 | [`scripts/screen_quant.py:88`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-screen-merged/scripts/screen_quant.py#L88) ＋ [`screen.py:61`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-screen-merged/scripts/screen.py#L61) | 缺 SIC 時 `sector` 變成 NaN（在本機 pandas 3.0.5 實測；`astype(str)` 後的偽值不再是註解宣稱的 `'未分類'`，`groupby` 直接把該群丟掉）→ `T5_產業恐慌` 對無 SIC 的公司**靜默永不觸發**，`:88` 的 `!= '未分類'` 守衛在此路徑形同虛設。我的對照組（同 6 檔、有 SIC=7370）全部標記 T5，無 SIC 全部不標。<br>*待查*：在 pandas 2.x 下 `<NA>` 會被 `astype(str)` 變成字串 `'<NA>'`，此時群組會以 `'未分類'` 成團 → 反向錯誤（不該標的亂標）。版本相依，兩種行為都不可接受 | `[T]`＋版本面 `△` |
| P1-12 | [`screen.py:230`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-screen-merged/scripts/screen.py#L230) | 只驗 `features.csv` 的 asof，`prices.csv` 的 asof 不驗 → 財報對、價格取到未來日，初篩排名與 52 週回撤全都在吃未來資料 | `[V]` |
| P1-13 | [`jobs/engine.py:275-292`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/engine.py#L275) ＋ [`:211-218`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/engine.py#L211) | `call_with_retry(retries=3)` 在**一次 submit 內**重試，但 `llm_calls` 只在 `_on_done` 記一筆；預算檢查是 `SUM(cost_usd)`／`COUNT(*)` over `llm_calls` → 傳輸重試的成本與呼叫數對預算不可見，$100/run 上限可被靜默突破 4 倍。且 `cost_usd` 允許 `None`（`:354`），SUM 直接跳過——後端不回傳成本時等於沒有預算 | `[V]` |
| P1-14 | [`jobs/engine.py:318-329`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/engine.py#L318) `△` | `handler.post()`（寫 decisions/signals/registry/thesis）與 `_update(status="done")` 不在同一交易；崩窗後 `_begin` 把 running 還 pending 重跑 → 重複寫入。下單端有 `open_buys` 去重、公司行動有 duplicate 守衛，所以未見雙重下單，但決策日誌與論點增補會長出殭屍列 | `△` |
| P1-15 | **【v2 改寫】** [`scripts/screen_quant.py:48-61`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-screen-merged/scripts/screen_quant.py#L48) | v1 我寫成「`check_point_in_time` 在 `fy_last_filed` 缺席時回傳 False 但不記錯誤 → 該檔被丟棄」——**機制講錯了**：`:52-53` 是 `bad = fd.notna() & (fd >= D)`，空／壞申報日**直接被跳過不驗**，而 `:61` 把整表 `pit_checked` 標成 True（`[T: test_point_in_time_marks_unverified_rows_as_checked]`）。真有前視時 `:55` 丟 `LookAheadError` **中斷整批**，不是丟棄單筆。所以後果不是「少一檔」，而是「未受檢的列被當成已受檢」，初篩的時點宣稱失去意義 | `[T]`＋`[V]` |
| P1-15(b) | **【v2 撤銷】** [`screen_quant.py:16-22`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-screen-merged/scripts/screen_quant.py#L16) | `~astype(bool)` 的金融旗標解析確實脆（NaN→True、`"False"`→True），但唯一生產路徑 `screen.py:74` → `from_live:37` 先 `fillna(False).astype(bool)`，實檔無誤判。僅當有人繞過 `from_live`（例如 `deep_quant.py` 直接吃 CSV）才會復發——列為潛在風險，不計入缺陷數 | `△`（已撤銷） |

---

## 5. P2 發現

1. `[T]` **【v2 嚴重度下修】框架把自己的 schema 從沙箱裡刮穿。** `FULL_A` 屬價格隔離工作，`stage_inputs` 對所有 `.json` 做 `scrub_prices`；而 [`deep.py:63`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/deep.py#L63) 把 `routing/output_schemas.json` 複製進沙箱給模型讀。我實測被刮掉的鍵：`FULL_A.moat_tests.price_cut`（鍵名含 `price`）、`DOSSIER.contains_price`（`pricing_power`／`attack_copy` 因鍵名不含 `price` 倖存）。**v1 的後果推論錯了**：[`jobs/context.py:96-101`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/context.py#L96) 把**未刮的完整 schema 內嵌進提示文字**，模型不必靠沙箱副本也能湊齊必填欄，所以不會「必然 `invalid_output`」。殘留的是**同一工作有兩份不同 schema 真值**（提示內嵌版 vs 沙箱刮除版）的稽核問題。規則檔該走白名單。
2. `[T: test_gate3_accepts_non_date_evidence_strings, test_gate3_crashes_on_malformed_evidence_instead_of_failing]` **閘門 3 的證據日期用字串比大小。** [`gate3_check.py:36-39`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/gate3_check.py#L36) 是 `any(str(e['date']) >= D ...)`，不解析日期：`"2020"`、`"2025-12"`、`"0"`、`"1999"` 這類非日期字串被我塞進證據即可通過（`"FY2025"` 反而因大寫排在後面而被判不過——同一行同時過鬆與過嚴）。此外 `e` 不是 dict 時直接 `AttributeError` 崩潰，而不是回傳「閘門 3 不過」。同一檔裡 `unknown_count`（`:48-49`）算出來卻從不使用。
3. `[T: test_questionnaire_claims_every_question_allows_unknown]` **問卷自我矛盾。** `assets/gate3_questionnaire.json` 的 `_說明`：「每題都可答『不知道』…」；實際 `choices` 裡 **C1、C5、M5、M6 四題沒有「不知道」**（v2 擴充：v1 只列三題）。此外檢查器根本不驗 `answer ∈ choices`，只驗 `answer ∈ pass_if`（[`gate3_check.py:36`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/gate3_check.py#L36)），所以「答了非法選項」與「答了不知道」在此處同形。另外 `pass_rule` 的 `all_answered_with_reason_and_evidence`／`every_answer_in_pass_if`／`evidence_dates_before_decision_date` 三個鍵由檢查器**逐字硬編碼**實作，只有 `min_strong_among` 是讀參數的——改參數不會改變前者的行為（檢查器 `:33` 硬比 `== '強'`，未讀 `counts_strong`）。
4. `[V]` **【v2 部分成立：第三源不存在，故為「四處三真值」而非「三處同源」】** **提示與 schema 的枚舉不同源。** `output_schemas.json` 的 `FULL_A.moat_tests` 用「強 \| 中 \| 弱」，問卷 M1–M3 只有「強 \| 弱 \| 不知道」；閘門 3 通過條件是 ≥2 項「強」。於是「中」是合法訊號卡值卻是非法問卷值，「不知道」是合法問卷值卻無法寫進訊號卡。我查 `assets/signal_card_schema.json` **沒有 moat 欄**（grep 0 命中），`procedure/01:53-57` 與報告模板是二元強／弱 → 「中」只活在 `routing/output_schemas.json`（第四源），不是三處。真值來源仍是多個，且沒有映射表。
5. `[V]` **【v2 半修——你今早加了 `rulebook.md:4` 覆寫清單與 8 份 `rule_index/*.md:1` banner，但沒覆蓋全部】文本參數三頭真值（詳見 §7）**：`g_impl` 硬門檻 10% vs `inferred_thresholds.md:6` 預設 8%（`cap_calc.py:33` 硬編 `G_IMPL_MAX = 0.10`，也就是說「參數真值表」裡的預設值在程式裡根本不可調）；荒謬線 50（`SKILL.md:112`、`rulebook.md:228`）vs 40（`rule_index/04_hold_sell.md:163`、`inferred_thresholds.md:50`），而 `HOLD_REVIEW.md:16` 恰好叫模型去整段讀 `rule_index/04`；新部位保護期 1 年（`SKILL.md:131`、`registry_schema.json:18`、`PRICE.md:17`）vs 2 年（`rulebook.md:229` P-49、`:384` Q5、`HOLD_REVIEW.md:25`），**程式站 2 年**（`hold_jobs.py:29 TWO_YEARS_DAYS=730`）。
   - **v2 更正/殘留**：`rule_index/04_hold_sell.md:1` 的 banner 已寫「40 倍→50 倍」（實測），但 `inferred_thresholds.md:7`（8%）與 `:50`（40 倍）原樣未動，而 `rulebook.md:152` 明文規定推論參數「一律列入 14.2 節」那張表 → 覆寫清單自己承認的來源裡仍留舊值。第三條殘留：C 級上限的分母在 `rulebook.md:188` 是 `IV_cons`，在 `rule_index/02:278` 是 `IV0`。
6. `[V]` **【v2 半修】規則優先序自我矛盾。** `SKILL.md:10` 定「本檔 ＞ procedure ＞ rulebook（參數真值）」；`rulebook.md:152` 寫「本表即規則書參數真值；與 references 不同時以本表為準」。兩者互指對方為次等，v2：`rulebook.md:3` 已改成與 `SKILL.md:10` 同一條鏈（SKILL＞procedure＞rulebook＞rule_index＞knowledge），主訴解了一半；**但 `rulebook.md:152` 原文未動**（「本表即規則書參數真值；與 references 不同時以本表為準」）——它與同檔 `:3` 互斥，所以 §7 的數字衝突仍未徹底**無解**的只有 `:152` 這一条。
7. `[V]` **等級上限 P1 的拘束力有三種說法。** `rulebook.md:265`「P1＝報告欄…不進上限」；`:278`「P4′ 寬鬆而 P1–P3 不過 → 不買」（拘束）；`:271` 零成長時「上限只能用 IV0 × 等級上限」（拘束）；`:405` 母體統計缺席時「上限＝min(P1, P2, P3)」（拘束）。同一個量在四條規則裡兩種身份，且與 `SKILL.md:93` 的 `min(P2′, IV_base)`（不含 P1）互斥。
8. `[V]` **溯源鏈斷裂。** `rulebook.md:126/436`、`rule_index/07:450` 引用的 `assets/templates.md` 不存在（實際檔名是 `analysis_report_template.md`／`summary_table_template.md`）；`rulebook.md:152` 引用的 `CHANGELOG_規則書.md`、`QA_規則書.md` 不存在；`procedure/11:3` 引用的 `EVIDENCE_MANIFEST.md` 不存在（皆我實測 `MISSING`）。`inferred_thresholds.md` 出處欄的審查代號 `X28`、`X05-1`、`X21-10`、`X30-14`… 在全專案**找不到任何定義表**（我以正則搜整個 repo：無匹配）。規則書稱它們為證據來源，實際上無法追溯。
9. `[V]` **違反鐵律 9 的具名與個案殘留。** `SKILL.md:44`（鐵律 9）：「報告與 skill 檔都不得用歷史個案類比」。但 `procedure/11:8`（步 4A 必讀）：「巴菲特買進中約兩成在決策時點帶困境訊號…」；另有「芒格條款／芒格變體」作為規則名與 schema 鍵（`rule_index/02:171/440`、`03:113`、`inferred_thresholds.md:36` 的 `munger_switch`）；`knowledge/02:142`、`knowledge/06:49`、`knowledge/16:7` 以波克夏自用組合作校準；`SKILL_screen:14` 的「那位決策者」；`rulebook.md:318` 的「本人決策部位」。
10. `[V]` **【v2 改向：一半是我講反了】符號與單位漂移：E。** `SKILL.md:88` 定義 `E`＝「近 5 年 OE 中位數」（總額口徑的講法），而 [`cap_calc.py:7`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-deep-merged/scripts/cap_calc.py#L7) 要求 `oe` 為**每股**；`SKILL.md:94/112` 又把 `E` 直接當成價格線基礎。框架端 [`review_jobs.py:253`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/jobs/review_jobs.py#L253) `e = x.get("norm_eps") or x["oe"]` 在 `norm_eps` 缺席時**用總額去除以價格口徑的 50 倍**（這不是運算錯，而是把口徑責任丟給手填 `state/cap_inputs/{T}.json` 的人／模型）。**v2 更正**：`cap_calc.py:7` 已明確寫 `oe`＝每股，`corp_actions.py:32-33` 也把 `oe, norm_eps, iv_*, price` 列入「每股口徑」逐股調整 → 與 `SKILL.md:81/88` 一致，v1 暗示的「文本要總額、程式要每股」不成立。真正的偏差在 `review_jobs.py:253`：**有** `norm_eps` 時用的是「正常化營業 EPS」而非規則的 `E`（近 5 年 OE 中位數），**`norm_eps` 為 0／缺** 時無聲切到 `oe`。見 P2-11 的後果。
11. `[V]` **【v2：仍成立；v3 限定措辭】獲利計算的輸入沒有量綱檢查。** `cap_input.json` 的 `oe`、`g10`、`g5`、`grade` 由模型/人工供入（`corp_actions.py:122` 只在拆股時調整），`cap_calc` 只在 `iv_cons <= 0` 時拒算（`:103`）。全檔 `cap_calc` 仍**零量綱斷言**（只在 `iv_cons<=0` 拒算，`:100`），所以把 `oe` 誤填成公司總業主盈餘（例如 12e9），`P2′`、`IV_base`、`IV0` 全部變成天文數字 → `auto_buy_cap` 高到任何市價都低於它，**價格保護等於不存在**。只需一句 `assert 0.1 ≤ price/oe ≤ 400` 就能擋住整類錯誤；目前沒有任何一道。<br>**v3 兩點限定**：（1）這條與 §12 C-5 不衝突——C-5 撤銷的是「上限**消失**（`None`）→ 任意價買進」，那個方向其實是 fail-closed（`gate2 = "無上限"` → 不過）；本條講的是上限**過高**，方向相反、確實 fail-open。（2）但「任意價格自動買進」我說重了：閘門 2 通過仍需 `grade_used ∈ (A,B)` 且無 `flips`，所以準確的說法是「**等級對的時候，價高照買**」，而不是無條件買進。
12. `[V]` **決策與訊號卡以「最新」配對。** [`sizing.py:174-195`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/portfolio/sizing.py#L174) 的 `load_decisions` 為每筆決策附 `latest_card(conn, t)`，不是該決策的卡；`add_blocked`（`:240`）也用 `created_at DESC, rowid DESC` 而不是決策鏈。併發或補寫時會錯配（一張新卡配舊決策）。
13. `[V]` **`_next_free_id` 在寫入交易外計算。** [`writeback.py:104-112`](file:///Users/zongen/Downloads/claude%20code/berkshire/trader/trader/analysis/writeback.py#L104) 以 `COUNT(*)+1` 生成 decision_id，與 `_write_rows` 之間無鎖定 → 兩個併發流程可拿到同一號（`decisions.decision_id` 是主鍵，會直接拋 `IntegrityError`，讓該次分析白跑）。
14. `[V]` **`config.yaml` 的 `job_tier_overrides` 可把 strong 降成 medium**（`router.py:43-45` `△`），M6 opus 同意閘門的觸發條件寫死在 tier 上，降 tier 即繞過同意，且無警告、無決策日誌痕跡。
15. `[V]` **【v2：本條已拆回 §4，主訴改寫、(b) 撤銷】`screen_quant._clean` 的布林解析**：[`screen_quant.py:16-22`](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-screen-merged/scripts/screen_quant.py#L16) 以 `~d['fin'].astype(bool)` 處理金融類旗標，NaN→True、字串 `"False"`→True（非空字串皆真）；配合 [`check_point_in_time`（`:48-56）](file:///Users/zongen/Downloads/claude%20code/berkshire/berkshire-screen-merged/scripts/screen_quant.py#L48) ——`astype(bool)` 那段在生產路徑（`screen.py:74`→`from_live:37` 已 `fillna(False)`）不觸發，**v2 撤銷**；`check_point_in_time` 那段的真實行為是「空申報日跳過不驗＋整表標 `pit_checked=True`」（`[T]`），**v2 改寫見 §4 P1-15**。初篩時點紀律的軟肋在第二處。

---

## 6. 獲利邏輯專章（逐條對規則）

| 規則條款（文本） | 程式實作 | 落差 | 金錢方向 |
|---|---|---|---|
| `auto_buy_cap = min(P2′, IV_base)`（`SKILL.md:93`） | `cap_calc.py:161` | `iv_base` 缺/0 時退化為 `P2′` | **買貴**（P0-2） |
| 歷史成長 ≤0 → 上限只能 `IV0 × 等級係數`（`SKILL.md:101`、`rulebook.md:271/276`） | `cap_calc.py:133-134`（只記 note） | 完全未實作 | **買貴**（P0-3） |
| P-21 區間 >4 不做、>3 降一級（`rulebook.md:192`） | `cap_calc.py:106-109` | 缺 `iv_opt` 即免測 | **該不買卻買**（P1-8） |
| 壓力測試（`r_d`+3/4pp 且獲利打九折） | `cap_calc.py:116-122` | 獲利腿為無操作 | 假通過（P1-7） |
| 金融類 P4′ 是「軟」上限，超過須寫理由（`SKILL.md:94`、`rulebook.md:405`） | `cap_calc.py:153` | 金融類以 `P4′` 作硬上限來源 | **買貴**（P1-6） |
| 賣出檢視線＝`IV_base`、荒謬線＝50×E（`SKILL.md:112`） | `writeback.py:561`、`review_jobs.py:254` | `IV_base` 缺 → `sell_review_above=None`；荒謬線由框架補算、`E` 口徑靠手填 | **不賣**（P0-2、P2-10） |
| `SELL_REVIEW`（`PRICE.md:15`） | `review_jobs.py:145` | 映射成 `HOLD`，標「M5」 | **不賣**（P0-4a） |
| 論點複查＝持有期唯一監控（`procedure/07`、`SKILL.md:130`） | `validate.py:93-101`＋`handlers.py:111` | 交白卷可過；不合格輸出對閘門不可見 | **不賣／亂加**（P0-5） |
| 三閘門作答前不看價（`SKILL.md:38`、`procedure/12:20`） | `sandbox.py:148`、`gate3_check.py:26` | mtime 可偽造、聲明可自填、隔離用 deny-list | **閘門失去意義**（P0-7） |
| 無價格停損（`SKILL.md:41` 鐵律 6、`rulebook.md:316`） | 一致 | — | 正確的設計，但正因為沒有停損，P0-4 的「賣出靜默失靈」才致命 |
| 初篩只在決策日前已申報財報上判定（`procedure/12:12`、`SKILL_screen:34`） | `screen.py:230` 只驗 features 的 asof；`screen_quant.py:52-53` 空申報日跳過不驗、`:61` 卻把整表標 `pit_checked=True` | 未受檢＝已受檢 | 排名失真（P1-12、v2 改寫後的 P1-15） |
| **【v2 新增】** `g_term = min(3%, 30Y − 1pp)`（`SKILL.md:90`、`rulebook.md:276`、`procedure/05`，**可為負**） | `cap_calc.py:126` `min(0.03, max(0.0, L-0.01))` | 私自加 0 下限；同檔 `:96-97` 又硬編 `0.03`、保守情境走 `g_term_c` → 一筆輸入三種終值假設 | **買貴（v3 降 P2，§12 C-4）**：偏差真實但幅度利率依賴——30Y=0.5% 時上限＋2.44%、0% 時＋4.77%、**≥4%（目前利率區間）→ 0%**。「系統性放寬買進上限」我收回 |
| **【v2 新增】** 文件宣示 `incr_roic_gt_rd` 是 A 級 P4′ 1.2 倍條件之一（`cap_calc.py:20`、`assets/cap_calc_example_input.json`） | `:142/:144` 從不讀它 | 死輸入；金融類實測旗標 True/False 的 `cap` 完全相同 | 規則與實作分岔（N-6，`[T]`）；**注意**：v1／代理的「1.2 倍放大硬上限」不成立，`k4` 只落在非金融的軟上限欄 |
| **【v2 新增】** `g_impl` 為 null 且 `g_impl_gt_cap`＝true → 閘門 2 不過（`SKILL.md:99`） | `review_jobs.py:291-298` 的 `gate2` 只看 `price < auto`／`grade_used ∈ (A,B)`／`flips` | 該旗標只被 `checks.py:183`、`writeback.py:534` 寫進登錄表當裝飾，框架**從不讀** | **v3 撤銷「該擋不擋」**（§12 C-6）：掃價 7 檔，旗標亮起的 5 檔全部 `price ≥ cap ≥ auto_buy_cap`，早就過不了 gate2 → 對閘門 2 是**冗餘資訊**。剩下的只有 `solve_g` 上界 0.0999 < `G_IMPL_MAX`=0.10 的窄帶誤標（不下單）→ 降 P2 |
| **【v2 新增】** schema 慣例「數字或 null」（`output_schemas.json:96-98`） | `cap_calc.py:95-97` 全用 `x.get(key, default)` | 顯式 `null` 回傳 `None` 而非 default → `:100` 早退（無上限）、P-21 跳過 | **格式層矛盾**（N-8）：不是模型偶爾漏鍵，是照規則寫也會觸發 fail-open |
| **【v2 新增】** 分批建倉、高於上限連 N 日應取消剩餘批次（`rulebook.md:228` 一帶的建倉紀律） | `sizing.py:477-479` `cap is None → continue`；`:528-530` 同一條件讓 `days_above_cap` 永不累加 | 計畫消失、計時也消失，日誌無痕 | **卡死＋無異常**（N-2，`[T]`） |
| **【v2 新增】** 等級上限 A 15%／B 10%（宿主模式）＋「超限要減碼」 | `sizing.py:23` `GRADE_CAP` 只列 A/B；`:414-416` `cap is None → continue` | C／D／`None`（含被 N-1 清空者）**永不產生 TRIM** | **該賣不賣**（N-3，`[T]`：持股膨到 54% 仍回 `[]`）。系統又刻意無價格停損 → 權重超限這條機械減碼之外，剩下的出口只有 `review_jobs.py:139-144` 的荒謬線 TRIM（讀的仍是同一批可能缺值的線）與 LLM 的 HOLD_REVIEW，後者的可靠性由 P0-8／P0-5 證明不可靠。**v3 措辭更正**：v2 寫的「唯一出口」收回（§12 C-10），荒謬線是第二條機械路徑，但它與本條共用同一個上游缺失，所以結論不變 |
| **【v2 新增】** 點對點（決策日只看當時可得資料，`SKILL.md:40` 鐵律 5） | `universe.py:82` `load_universe(...)` **無 `asof`**；`daily.py:41/:198` 公司行動向未來取 60 日且 `:160` 把 raw 整包進提示 | 歷史重播用「今天的 S&P 500 名單」；`universe_versions` 的 `asof` 實為今天 | **前視＋倖存者偏差**（N-11、N-12）→ `evidence_*.md` 的實證數字時點有效性目前無法由資料層證明。**v3 更正**：「該表只寫不讀」是錯的（`universe.py:55` 讀 `sha256` 做變動偵測），而且表內已存 `tickers` 全清單（`db.py:53`）→ 修法縮為 `load_universe` 加可選 `asof` 回查，約 6 行（§12 C-7／T-3） |

**一句話總結獲利邏輯的審查結論（v3 修訂）**：算式本身（`pv`／`solve_g`／`g_impl`、`r_d`）我逐行核對過，與 `SKILL.md §3` 的定義一致且數學正確；**問題全部在「規則條款的落地覆蓋率」與「缺資料時的預設方向」**。這台機器的估值引擎是好的，但保護它的剎車有四個沒接線（P0-2/3/4/5）。v2 曾再加四條，v3 覆核後只剩兩條成立：**N-2**（缺上限時「取消剩餘批次」與計時一起消失，卡死且無異常）與 **N-3**（C／`None` 級不做權重超限減碼）；**N-5**（`g_term` 夾下限）偏差真但幅度利率依賴、30Y≥4% 時為 0，降 P2；**N-6/N-7**（死輸入／死旗標）屬規則與實作分岔，不是失靈——`incr_roic_gt_rd` 該從文件刪掉，`g_impl_gt_cap` 對閘門 2 冗餘。<br>**方向也要改**：v2 那句「買進側的算式被私自放寬、兩個方向都朝虧損傾斜」我收回一半。v3 實測顯示**買進側一直是 fail-closed**（缺上限→不買，§12 C-5），真正的金錢風險集中在**賣出側與監控側**：線被寫成缺值 → `e.get(k) and …` 當成沒觸發 → 永不再檢查，而框架手上有正確數字（`cap_result.json`）卻從未拿它校驗登錄表。

---

## 7. Skill 撰寫專章

規則文本是我看過組織度很高的一類（分層 SKILL→procedure→rulebook→rule_index→knowledge、每條帶出處與可調帶、有鐵律與「不得做」清單、有前瞻驗證樣本設計）。以下只列會造成**行為分歧**或**無法稽核**的寫作問題，不評風格。

**S-1 真值歸屬未定義（最嚴重）。【v2：半修】** 你今早在 `rulebook.md:3` 加了與 `SKILL.md:10` 同鏈的優先序、在 `:4` 加了覆寫清單、並給 8 份 `rule_index/*.md:1` 加了「已被覆寫」banner——方向正確，但真值歸屬仍未變成「一句可判定的分工」，而且覆寫清單沒覆蓋它自己承認的來源（`inferred_thresholds.md:7` 的 8%、`:50` 的 40 倍原樣未動）。新增第 4 條雙軌：`reason_code` 在四處不同源——`output_schemas.json:74` 有 `NOT_THIS_SKILL`、`:126` 沒有、`signal_card_schema.json:11` 有、`rulebook.md:436` 另列 7 種規則書理由碼，其中 `NO_NEW_FACT／THESIS_BROKEN／VALUATION／VALUATION_AND_WEAKENING／SWITCH／FUNDING／INSTITUTIONAL` 在機器格式中**不存在**（模型寫了會被 `validate` 擋，或反向被自由文字放過，見 N-4）。建議改成一句可判定的分工：「**數字的真值＝rulebook §5.1；條件與流程的真值＝SKILL＋procedure**；兩者重疊時，同一參數只允許在一處定義，其他地方只能引用編號」。目前 `g_impl`、荒謬線、保護期、`g_term`、P1 拘束力、C 級分母（`rulebook.md:188` 用 `IV_cons`；`rule_index/02_valuation.md:278` 用 `IV0`／清算價值）全都是雙軌。

**S-2 文本與可執行參數不同源。** `inferred_thresholds.md` 是給訊號卡抄的「推論參數表」，但 `cap_calc.py:33` 把 `G_IMPL_MAX` 寫死 0.10，表裡預設 8%；`absurd` 表裡 40、程式用 `review_jobs.py:29` 的 `ABSURD_PE = 50`。建議：可執行參數集中成一個 `params.json`，`cap_calc` 與提示都讀它，`inferred_thresholds.md` 由它產生——否則「使用任一項時把鍵寫入訊號卡」這句話無法保證一致性。

**S-3 不可滿足的條款。** `procedure/12 §2` 的加碼許可要求「三閘門以當次資料重新全過」，而閘門 3 要求 `price_seen_before_answering: false`（`SKILL.md:38` 鐵律 2）。持有中要加碼必然已知價格 → 每批加碼必然違反兩條中的一條。建議明寫加碼時的閘門 3 變體（例如「以建倉時的問卷為準，加碼只重過閘門 1／2 ＋ 論點完好」），否則這條是形式死路，實務上會變成永遠只有首批 50%（P-37）。

**S-4 漏斗可能設計成空集合（需人工確認意圖）。【v2 改判部分成立——v1 說法過強】** `procedure/06 §1` 起點由 C1 決定（`grade_link`：C1＝10 年寫得出才可 A），其後 `:12–:32` 共 21 列，但 `:22`（產業共同衝擊年，自註「不是買進理由」）與 `:24`（等級與結論說明）不觸發降級 → **實際 19 項**，而 `P-28`（`rulebook.md:204`）C 級＝0 不開倉，且「金融類降級後為 C → 宿主不開倉」。組合效果：起點 A＋1 降＝B（10%）**仍可買**，所以「單一降級即空集合」是我講過了；但起點 B、金融類（降級後為 C → 宿主不開倉）、以及 `:21` 穩定度 >4pp 直落 C（上限 0）這三條路徑，疊加 `:34`「A 級須稀少」→ **可買集合仍結構性極窄**。這不是 bug，但它使「太難／不動作」從防禦性預設變成**結構性預設**——handoff 記錄的 bootstrap 15 檔全數 NO_ACTION/WATCH 與此吻合。建議在 SKILL 頂部寫明預期通過率並加一條「降級可被具體證據抵銷」的出口，否則 656 支測試全綠的系統實際上沒有任何出口。

**S-5 層級違規成為過期值入口。【v2：仍成立，而且 banner 修不到它】** `SKILL.md:10` 明文「`rule_index/` 只准 grep、不整份讀」；`HOLD_REVIEW.md:16` 卻指示模型去讀 `rule_index/04_hold_sell.md`（SELL-020～083）。你加的覆寫 banner 在該檔 `:1`，而模型是**grep 段落**進去的（`:163` 的 40 倍、`:230` 的「統計便宜型」）→ **它讀不到第 1 行**，過期值對模型仍是本檔真值。要修得動，覆寫必須做在**被 grep 的那一行本身**（例如在 `:163` 就地標「已廢止，見 rulebook P-xx＝50 倍」），或在提示層直接禁止引用 `rule_index/` 的數值。

**S-6 枚舉／欄位跨檔不同源（P2-4）。【v2：第四源而非三源】** moat 三測的取值在問卷（強／弱／不知道）與 `output_schemas.json:85-87`（強｜中｜弱）不同；我查 `assets/signal_card_schema.json` **沒有 moat 欄**（grep 0 命中），`procedure/01:53-57` 與報告模板是二元強／弱 → 「中」只活在 routing schema，是「多處、彼此無映射」而不是「三處同源」。建議單一來源（`gate3_questionnaire.json`）＋其他地方以 `enum_ref` 引用。

**S-7 懸空引用（P2-8）。** 4 個被規則當作依據引用的檔案不存在，X 代碼無定義表。文本自稱「每一條都附出處」，實際上一整類出處無法追溯；這會直接侵蝕 `procedure/13` 前瞻驗屍的可信度。建議加一支 lint：掃所有 `references/...`、`assets/...`、`X\d+`、`P-\d+` 引用是否存在。

**S-8 自相矛盾的禁令（鐵律 7）。** `SKILL.md:42` 不准以總經、利率、大盤時點作買賣理由；但 `rulebook.md:115`（G8 用大盤長期預期報酬 ×0.8 係數）、`:199`（P-27 分區）、`:411`（每季記錄分區）、`:197`（P-25：長債變動 ≥1pp 重算上限）都把大盤與利率放進了必經路徑。其中 P-25（重算）是合理的紀律，但 P-27/G8 是大盤報酬直接進入買進判斷。建議把鐵律 7 改寫為「不得以**預測短期走勢**作理由；長期名目報酬假設與折現率紀律除外」，並在 G8 標明該係數是「機會成本假設」而非「大盤時點判斷」——否則每一次買進都可被判違反鐵律 7。<br>**v2 改判部分成立**：鐵律 9 禁的是「以個案類比**某個標的**」，而芒格條款／knowledge 三處自標「校準用，不是門檻」，屬**具名與內部統計外洩**（`SKILL.md:178`）而非個案類比；鐵律 7 的實質衝突只剩 G8＋P-27（`:115/:199`、`procedure/06:46`），`:413/:371` 已部分抵銷 P-25／分區記錄。

**S-9 `PRICE.md:18`「其餘 → NO_CHANGE 或 HOLD」無判準。** 一句沒有條件的預設，在自動下單系統裡就是隨機裁量。

**S-10 引用截斷。** `references/procedure/11_distress_check.md:14/:15/:16/:30` 四處「原文：…，；…」被截斷、出處消失。**v3 我已逐行親查**（原引的 14/16/30 漏了 `:15`），由 `△` 升為 `[V]`；無執行期影響，屬文本品質，修法就是把四處補回完整引文與出處，不需要建 lint。

---

## 8. 設計層疑慮（不是 bug，但我會改）

- **D-1 只做買進保守、不做退出紀律。** 無價格停損（`rulebook.md:316`）是可辯護的選擇，但搭配 P0-4 的四個靜默失靈，組合層的實際行為是「進得難、出不來」。如果堅持 A 案停損模式，賣出側就必須有「無法判斷 → 人工」的硬出口，而不是「無法判斷 → 不動作」。<br>**v2 加劇**：N-1、N-2（缺上限把建倉批次卡死且逃生口一起停擺）、N-3（C／D／`None` 級不做權重超限減碼）三條把「出不來」從推論變成實測結果。**v3 更正**：N-1 的機制不是「`review.py:55` 把 `None` 鍵整批刪除」（該環節已在 §12 C-1 撤銷），而是**登錄表的三條線可以在合法回寫過程中變成空值**（模型自寫的 `registry_entry.json` 被直接採用、框架的 `cap_result` 只覆寫訊號卡），之後 `review_due` 把缺值當沒觸發。結論不變，上游更正。
- **D-2 買線與賣線同值、零遲滯帶。** `auto_buy_cap = min(P2′, IV_base)` 與 `sell_review_above = IV_base` 是同一個數（`SKILL.md:93/112`），買在 0.99×IV_base 的部位的賣出檢視距離一個跳動為 0；`rulebook.md:370` 的 3b 更寫「價格 ≥ IV_base → 全清」。建議：買進上限改用 `min(P2′, P1)` 讓等級上限真正約束，或把賣出檢視線明確設為 `IV_base × 1.15`，並在兩線之間設觀察帶。
- **D-3 唯一有正向證據的族群預設關閉。** `evidence_statistical.md:88`：被初篩排除的那批實際買進，3 年超額最高（約 +19%）；而 `--special-max` 預設 0（`SKILL_screen:27/73`），開啟條件是大盤已跌逾 20%。這是可辯護的風險取捨，但與 README 自述「只有『初篩降低踩雷』有證據」不相容——那批證據恰好被預設切掉。
- **D-4 `MISSING_DATA` 沒有終端狀態。** `model_routing.json:24` 的跳過規則明文排除 `MISSING_DATA`，`SKILL.md:19` 未設嘗試上限 → 結構性缺欄位的標的每季重跑整條鏈、永遠得不到上限，白花 token。另 `model_routing.json:13` 把「輸出格式不合格」也寫成 `NO_ACTION/MISSING_DATA` 進決策日誌 → 污染 `procedure/13` 那 3 個月紙上交易樣本（系統唯一的流程報酬證據）。建議：格式不合格用獨立 reason_code（例如 `INVALID_OUTPUT`），與資料缺席分開。
- **D-5 加碼阻擋只覆蓋已持有者。** `sizing.py:284-312`（`plan_buys` 對新部位不查 `add_blocked`）與 `daily.py:230-238` 的敘述一致，但風險未標明：等待同意期間該檔可以**新建倉**（只是不能加碼）。<br>**v2 補**：`plan_buys` 與 `target_weight` 對「缺上限／缺等級」是 **fail-closed**（`:305-308`／`:83-85` 跳過不買），所以這條風險只在閘門紀錄與卡欄齊全時才存在——比 v1 寫的窄。
- **D-6 `hold_jobs.py:55-66`**（v3 下修）：機制確認——`valuation_numbers()` 只讀 `*_price.json` 的 `price`、**不比對檔內日期與 `asof`**，且 `sorted(glob)` 的迴圈會用**最後一檔**覆蓋 `price`。但「可能是舊資料日」這個前提我證明不了：`{T}_price.json` 由 `dossier_prep.build`（`:305`）在當次沙箱現造，正常流程下就是最新可得收盤；正常路徑每檔也只有一支價格檔，多檔覆蓋是理論情形。故本條由「用過期價格判賣出」改為**「缺日期斷言」的穩健性註記**（建議：取價時核對 `price_date <= asof`，否則改寫 `framework_notes`）。
- **D-7 `pipelines/approved.py:29-31`**（v3 下修）：整個 `engine.run` 在 `pipeline_lock` 內執行是**該檔註解自己宣示的意圖**（「等排程中的工作做完再寫帳本」）。長任務（多檔 FULL）期間每日排程會被阻塞是設計取捨，不是缺陷；我把它移到觀測項，只在「你實測到排程錯過了當日收盤」時才值得處理。
- **D-9【v2 新增】預算與同意閘門的預設值是「不設限」。** `engine.py:212` 的預算 `WHERE run_id=?` 只按 run 累計（每日新 run 重新計），`config.py:42-43` 允許 0＝不限且 `max_input_tokens_per_run` 預設就是 0；`approvals.py:31-33`＋`config.py:69` 的 `require_for_tiers: []` 可整扇關閉同意閘門，`config.example.yaml:65` 還把「`[]`＝不需同意」寫成文件化用法。兩者都無警告、不在決策日誌留痕——**安全預設應該是「設限」，這裡是反的**（詳細證據見 §3A 末三條）。
- **D-8 fail-closed 疊加無出口 ⇒ 成本隨噪聲上升。** `TRIAGE.md:15`「有疑慮就選不確定」→ THESIS「有疑義一律疑似」→ 升級 FULL；每一層都把不確定推向更貴的強模型，而利潤來源只有一份（避開踩雷）。建議加一條全域預算／升級次數上限與「超過即記 NO_ACTION/TOO_EXPENSIVE 並停止」的終端態。

---

## 9. 修正優先序（v3：已做「過度工程化」修剪，僅建議，本審查未做任何改動）

修剪原則只有一條：**這個系統在跑真錢，修補要能與風險成比例**。一行能擋住的，不要提議改一層架構；能被現有測試蓋住的，不要提議新建工具。v2 的 15 項中有 9 項被砍掉或縮小範圍，v3 依 §12 T-1～T-9 逐條處理；結果仍是 15 項（新增第 11 項給 N-13、移除一項讀取端 API 改造），但其中 13 項是單點 ≤6 行的修改，只有第 4 項（schema 逐欄 `enum`）與第 7 項（點對點兩端）超過，且新建基礎設施 0 項。

| 順序 | 動作（v3 口徑，粗體＝新增或擴大） | 規模 | 覆蓋 |
|---|---|---|---|
| 0 | **登錄表三條線的兩個生產點各加一道守衛**（取代 v2 第 0 項）：(a) `writeback.py:561`（PRICE 覆寫）——新值為 `None` 而舊值存在時**保留舊值並記 `framework_notes`**；(b) `write_full_result` 的 `held` 分支把保留欄從 4 欄擴成含 `auto_buy_cap`／`sell_review_above`／`absurd_price_line`，**並用同目錄的 `cap_result.json` 覆寫**（框架在 `deep.py:346` 已算好，只是沒往登錄表送） | 兩處各 3–6 行 | N-1、N-8(b)、P1-3、P0-2（賣出側） |
| 1 | `daily.py:294` → `cash_per_share`；**測試只補一支針對該鍵的斷言**（v2 提議的「正規化器↔消費端鍵契約測試」取消，見 §12 T-1） | 1 行＋1 測 | P0-1 |
| 2 | `cap_calc.py:161`：`iv_base in (None, 0)` → `auto_buy_cap=None` ＋ `reason_code MISSING_DATA`（不要退化為 `P2′`） | 1 行 | P0-2、N-8 |
| 3 | 賣出／建倉側 fail-closed **只做留痕**：`sizing.py:477-479` 與 `:528-530` 兩處 `continue` 前各寫一筆 `anomalies`＋人工待辦。（v2 在此條附帶的「實作 `SELL_REVIEW` 處置」「非 A/B 級要能觸發 TRIM」屬新增行為設計，v3 移出修 bug 清單，見 §12 T-2） | 2 處各 2 行 | N-2、N-3（僅可觀測性） |
| 4 | `validate.py:193-198` 的自由文字降級：只在 spec 明確標記 free-text 時才降；`DOSSIER.dq_checks[].result` 改逐欄 `enum`＋`pattern`。（v2 附帶的「所有載荷欄位加 min_length 與陣列非空」縮小為「只修有金錢含義的 dq_checks 與 THESIS 兩處」） | 10–20 行 | P0-5、N-4 |
| 5 | `ThesisHandler.on_invalid`／`PriceHandler.on_invalid` 覆寫成保守（`thesis_check="疑似"`＋`stop_adding`） | 各 3–5 行 | P0-5 |
| 6 | 點對點三小刀：`pe_median.get_frozen` 驗 `stored.asof <= asof`；`screen.py:230` 驗 prices asof；`screen_quant.py:52-61` 把未受檢列老實標記；並修正 `tests/test_pe_median.py:101-113` 那支把錯誤行為寫成斷言的測試 | 各 1–5 行 | P0-6、P1-12、P1-15 |
| 7 | `load_universe` 加可選 `asof`，**直接回查 `universe_versions`（該表已存 `tickers`，`db.py:53`）**——v2 把它寫成「資料層點對點工程」是過度設計，實為約 6 行；公司行動端 `corp_actions.py:160` 提示只給欄位子集，加「公布日 ≤ asof」過濾（只在一端過濾即可） | 6 行＋10 行 | N-11、N-12 |
| 8 | 隔離**只做兩件**：`router.py:22` 補 `state_dir`；`isolation.py:18` 的 deny 清單加入 `/Users/Shared/berkshire-sandbox-*` 的**兄弟沙箱**路徑。（v2 提議的 allow-list 全面重寫、每 run 獨立根目錄＋結束清理、`open_price_file` 綁 `run_id/job_id` 交叉核對、scrub 數值掃描、RED_TEAM 入隔離——v3 全部取消或延後，見 §12 T-4） | 2 處小改 | P0-7（部分）、N-9.1 |
| 9 | `HOLD_REVIEW.path[].q` 補 `Q3b`／`Q3c`（或統一改 `Q3`＋子鍵）；`path` 非空才可判 `SELL_ALL`/`TRIM`；`hold_jobs.py:81` 的錯誤訊息不要用 schema 禁止的詞 | 文字＋1 守衛 | P0-8 |
| 10 | `corp_actions.detect` 去重鍵補 `action_type`；`jobs/corp_jobs.py:84-88` 對 `duplicate` 不要靜默 `return []` | 各 1 行 | P0-9 |
| 11 | **（v3 新增）** `handlers.py:143` 的反向匯入下沉到使用處，或交由 `jobs/__init__.py` 明確註冊一次 → `import trader.jobs.review_jobs` 不再需要靠匯入順序僥倖成立 | 1–3 行 | N-13 |
| 12 | 規則文本：修 `procedure/11:14/15/16/30` 的懸空引用、清掉 `inferred_thresholds.md:7/:50` 舊值、統一 `rulebook.md:152` 與 `:3`、補 `reason_code` 單源與 `Q3b/Q3c`。（v2 提議的「引用 lint 納入測試」取消——為 4 處壞引用建工具是倒掛，見 §12 T-5） | 純文字 | S-1、S-2、S-5、S-7、S-10 |
| 13 | `screen.py:184` 加 `split=True`；`screen_quant.py:88` 對 NaN/Null 顯式處理 | 各 1 行 | P1-10、P1-11 |
| 14 | `require_for_tiers` 為空清單時於日誌與儀表板顯示警告（一行）。**v2 的「全期預算」取消**——那是新功能，不是缺陷修正（見 §12 T-6） | 1 行 | D-9（部分） |

**v3 從清單移除（原 v2 第 4、11 項的局部）**：`移除 g_term 的 max(0.0, …)`（N-5 降 P2，且要先把規則文本改成與實作一致才談得上修）、`gate2 補讀 g_impl_gt_cap`（N-7 撤銷）、`incr_roic_gt_rd` 的處理（仍是真問題，但正確動作是**從文件與例檔刪掉那欄**，不是在算式裡補實作——補實作屬規則變更，需你先拍板）、`cap_inputs` 量綱斷言（P2-10/11 降為「缺值時寫 framework_notes」，`oe` 與市值一致性需要額外的公司資料，暫不做）。

**如果只能做一件事**：做第 0 項。它同時堵住 N-1、N-8(b)、P1-3 與 P0-2 的賣出側，而且全部改在框架自己的程式裡，不需要動規則、動模型、動 schema。
---

## 10. 重現測試索引

`python3 -m pytest -q` 全綠＝缺陷存在（斷言的是實際行為）。

### `test_repro_profit_logic.py`（15 支）
| 測試 | 覆蓋 |
|---|---|
| `test_auto_buy_cap_survives_when_iv_base_missing` | P0-2 |
| `test_zero_iv_base_also_disables_the_clause` | P0-2（`iv_base=0` 分支） |
| `test_declining_company_cap_is_not_restricted_to_iv0` | P0-3 |
| `test_p21_width_check_skipped_when_iv_opt_missing` | P1-8 |
| `test_conclusion_test_earnings_leg_ignores_stressed_earnings` | P1-7 |
| `test_gate3_accepts_non_date_evidence_strings` | P2-2 |
| `test_gate3_price_file_timestamp_is_optional` | P0-7(3) |
| `test_gate3_crashes_on_malformed_evidence_instead_of_failing` | P2-2 |
| `test_questionnaire_claims_every_question_allows_unknown` | P2-3 |
| `test_sell_lines_wiped_to_none_silently_disable_all_sell_checks` | P0-2/P0-4 |
| `test_missing_price_data_fails_open_for_selling` | P0-4c |
| `test_add_trigger_requires_target_weight` | P0-4d |
| `test_entry_without_last_review_date_fires_thesis_on_any_historic_move` | 新建倉無 `last_review_date` 時，任何歷史波動都觸發論點檢查（`review_due.py:113`，`last` 預設 `"1900-01-01"`） |
| `test_screen_price_columns_leak_into_special_watch_block` | P1-10 |
| `test_missing_sic_silently_disables_industry_panic_and_makes_guard_dead` | P1-11 |

### `test_repro_framework.py`（10 支）
| 測試 | 覆蓋 |
|---|---|
| `test_benchmark_dividend_reads_a_key_the_normalizer_never_writes` | P0-1 |
| `test_frozen_pe_median_file_is_returned_even_when_it_looks_ahead` | P0-6 |
| `test_frozen_pe_median_prev_month_also_unchecked` | P0-6 |
| `test_invalid_thesis_decision_is_invisible_to_add_blocked` | P0-5(1) |
| `test_thesis_check_passes_validation_with_zero_evidence` | P0-5(2) |
| `test_rejected_approval_does_not_block_adding` | P1-4 |
| `test_write_stop_result_wipes_a_watch_entrys_valuation` | P1-3 |
| `test_detect_drops_a_second_corp_action_on_the_same_effective_date` | P0-9 |
| `test_hold_review_schema_cannot_record_the_branches_the_prompt_demands` | P0-8 |
| `test_full_a_sandbox_schema_loses_the_moat_test_key_it_must_output` | P2-1 |

### `test_repro_round2.py`（14 支，v2 新增）
| 測試 | 覆蓋 |
|---|---|
| `test_grade_d_is_already_blocked_my_old_p1_9_was_wrong` | **推翻我自己的 P1-9 主訴**（D 級確實被擋） |
| `test_unrecognised_grade_is_silently_treated_as_c` | P1-9 改寫條（未知等級借用 C） |
| `test_g_term_floor_raises_cap_when_long_rate_below_one_percent` | N-5 |
| `test_incr_roic_gt_rd_is_a_dead_input_for_a_grade_p4` | N-6 |
| `test_gate2_never_reads_g_impl_gt_cap` | N-7 |
| `test_dossier_dq_checks_result_is_free_text` | N-4 |
| `test_empty_arrays_and_blank_reason_pass_thesis_still_holds` | P0-5(2)（你今早改過 `validate.py` 後仍成立） |
| `test_wiped_valuation_lines_never_trigger_sell_review` | N-1 |
| `test_missing_auto_buy_cap_strands_the_tranche_without_any_record` | N-2 |
| `test_grade_c_or_none_disables_the_only_mechanical_exit` | N-3 |
| `test_red_team_is_not_price_isolated_though_its_schema_forbids_pricing` | N-10 |
| `test_call_with_retry_makes_retries_plus_one_physical_calls` | P1-13 |
| `test_universe_loader_has_no_asof_parameter` | N-11 |
| `test_point_in_time_marks_unverified_rows_as_checked` | P1-15 改寫條 |

註：`test_repro_profit_logic.py::test_sell_lines_wiped_to_none_silently_disable_all_sell_checks` 與 `test_repro_framework.py::test_write_stop_result_wipes_a_watch_entrys_valuation` 是 N-1 的前兩段；N-8（顯式 `null` 殺 fallback）由 `test_auto_buy_cap_survives_when_iv_base_missing`／`test_zero_iv_base_also_disables_the_clause` 覆蓋同一個 `:161`／`:100` 分支，N-9、N-12 無 pytest（以檔案系統與原始碼檢查為證）。

> **v3 對本表的覆核提示**：上表 14 支測試**全部仍能跑綠**，但它們證明的是「程式碼事實」，不保證我 v2 由該事實推出的「金錢後果」成立。已被 §12 撤銷或改向的有五行：`…g_term_floor_raises_cap…`（N-5→P2，C-4）、`…gate2_never_reads_g_impl_gt_cap`（N-7 撤銷，C-6）、`…red_team_is_not_price_isolated…`（N-10 降為文本，C-12）、`…universe_loader_has_no_asof…`（事實對、結論縮小，C-7）、以及測試名稱裡的「`the_only_mechanical_exit`」（「唯一」二字收回，C-10）。這輪的教訓我寫進 §12.4。

### `test_repro_round3.py`（11 支，v3 新增：全部拿來查「我自己」）

這一輪的目標不是找新 bug，而是問兩個問題：**那個狀態正常流程走得到嗎**、**我提的修法是不是過大**。因此同一批測試裡有 5 支是用來**推翻我自己的結論**（下表標 `撤銷／降級用途`）。

| 測試 | 裁決 | 覆蓋 |
|---|---|---|
| `test_full_writeback_drops_sell_lines_when_model_entry_omits_them` | 可達（真實向量） | N-1 |
| `test_full_lane_never_reconciles_registry_entry_with_cap` | 可達（ stronger：框架手上有 cap 卻只覆寫訊號卡） | N-1、§9 第 0 項 |
| `test_review_due_goes_silent_for_that_row_with_real_price_data` | 可達（含「補回三條線就出現兩條 `[PRICE]`」的對照組） | N-1、P0-4 |
| `test_none_key_drop_in_review_py_is_a_no_op_for_review_due` | **撤銷用途**：`None` 鍵與缺鍵行為相同 | §12 C-1（原 v2 的 `review.py:55` 一環） |
| `test_holding_row_is_protected_from_the_blank_template` | **限定用途**：空模板只打非 HOLDING | P1-3、§12 C-3 |
| `test_gate2_is_fail_closed_when_auto_buy_cap_is_none` | **撤銷用途**：缺上限→不買，不是亂買 | §2、§12 C-5 |
| `test_g_impl_gt_cap_flag_is_redundant_for_gate2` | **撤銷用途**：旗標對閘門 2 冗餘 | N-7、§12 C-6 |
| `test_g_term_floor_effect_is_small_and_regime_dependent` | **降級用途**：幅度 ≤＋4.8%，30Y≥4% 時為 0 | N-5、§12 C-4 |
| `test_universe_versions_table_is_read_not_write_only` | **更正用途**：該表有被讀；但 `tickers` 已存 → 修法很便宜 | N-11、§12 C-7／T-3 |
| `test_rejected_approval_row_is_a_dead_end_within_the_same_run` | 可達＋有跨 run 出口 → 降 P2 | P1-5、§12 C-8 |
| `test_review_jobs_cannot_be_imported_standalone` | 新增缺陷（結構性） | N-13、§12 C-9 |

---

## 11. 誠實聲明與目前的不確定（v2 重寫）

這一節是這份報告裡我最不想省略、也最該被你審的部分。

1. **未實際呼叫任何模型。** P0-7／N-9 的「模型會去 glob 兄弟沙箱讀真價檔」是**能力**層面的事實（同 uid 可讀、且有 `Write`），不是**行為**層面的證明。我沒有跑 pipeline，所以「它會不會去讀」仍是推論；可證明的部分只有「隔離無法被稽核」與「跨沙箱可讀寫」。
2. **沒有端到端跑過任一流程。** 所有 `[T]` 都是對單一函式／模組的單元層級重現（含用框架自己的 `db.migrate` 建 tmp DB、用 `Handler().on_invalid(...)` 產生列）。真實執行序（排程 → DAG → writeback → sizing）下的串聯後果，我只用讀碼推論，可能在某些順序下被別的檢查攔住。
3. **回測與證據檔的數字我沒有複算，而且現在更不敢信。** `evidence_*.md`（含 D-3 的 +19%）是這套規則「唯一有實證的優勢」的支柱；N-11 查明 `load_universe` 沒有 `asof`（v3 更正：`universe_versions` **有被讀**，`universe.py:55` 用它做 sha256 變動偵測，且表內已存完整 `tickers`，所以修法很便宜），P0-6 查明凍結 P/E 檔不吃 asof 檢查。**這兩個缺陷都朝「高估規則有效性」的方向偏差**，所以在資料層補上點對點之前，那批證據的時點有效性無法被證明。
4. **（v3 已全部結案）原本停在 `△` 的五條**：P1-5 → `[T]` 端到端實測，並**降為 P2**（同 run 死巷屬文件宣示的設計，跨 run 有出口）；P1-14 → 結構確認（`_write_rows` 內部三表原子，但 `post()` 與 `status="done"` 分屬兩次提交，重放會長出 `-2` 決策列與第二份論點增補；我未注入崩潰）；S-10 → `[V]`，行號更正為 14／15／16／30；D-6 → 下修為「缺 `price_date` 斷言」的穩健性註記；D-7 → 改為設計取捨／觀測項。**報告現在不再有任何以 `△` 支撐的金錢主張。**
5. **三條我判定為「需你確認意圖」而非 bug**（見 §3A 末）：`inferred_thresholds.md` 的 8%／40 是否刻意保留為「人類可調、程式不讀」的建議值；`require_for_tiers: []`（同意閘門可整扇關閉）是否你的常用配置；`g_term` 的 0 下限是否是你對「終值成長不得為負」的私下裁決。如果是刻意的，請寫進規則，否則下一個讀程式碼的人（或模型）會把它當成 bug 改掉。
6. **環境相依的結論**：本機 Python 3.14／pandas 3.0.5。P1-11（缺 SIC 時 `T5` 恆不觸發）在 pandas 2.x 可能是反向錯誤（亂標）；`test_repro_round2.py` 的 NaN 相關斷言也可能隨版本變紅。這些是「兩個行為都不可接受」的那類，不是「我的測試寫錯」。
7. **並行編輯**：你在我審查期間（08:38–09:19）動過 `validate.py`、`context.py`、`output_schemas.json`、`models.py`、`config.py`、`claude_code.py`、`web/static/app.js`（另有 `tests/test_validate.py`）。v2 處置：派 5 組查核代理＋我自己把 v1 全部條目對**改後的檔**重判（§0.4），交付前再把本報告 **91 個 `file:line` 錨點**逐一驗證，並重跑專案自身的 `python3 -m pytest -q`（**656 passed**）與本資料夾的重現測試（v2 39 支、**v3 共 50 支全綠**）。若你之後又改，請以「判定 ID（P0-x／P1-x／N-x）＋函式名」追蹤，行號可能再漂移。
8. **我自己在 v1 犯了 4 類錯**，全部列在 §0.4：主訴撤銷 1（P1-9 的 D 級）、機制講錯 3（P0-4c、P1-15(a)、P2-1 的後果推論）、方向相反 2（P2-10/11、P1-11 的「亂標」）、嚴重度過高 2（P0-4d、D-5）。此外我撤回了代理給我的兩條過度推論（`FULL_B.stopped_at_gate` 自由文字、A 級 1.2 倍放大硬上限）。**這份報告裡任何一條你覺得不對，優先信你自己跑的結果。**
9. **專案測試品質未審**：`trader/dashboard/`、SwiftUI widget、launchd 排程細節、以及 `trader/tests/` 自身的斷言品質。656 支全綠仍漏掉 P0-1 這種一行鍵名錯誤，而且 `tests/test_pe_median.py:101-113` 主動把 P0-6 的錯誤行為寫成斷言（測試固化缺陷）——這本身就值得另做一次審查。
10. **寫入邊界的實測更正**：我聲稱唯讀，但 v3 用檔案時間戳自查後發現**兩處專案樹內的寫入**，都在 `bug report/qwen/` 之外。**專案原始碼、設定、規則檔：一個字都沒動**；v3 全部驗證採取唯讀手法（把模組 import 進我自己的程序、`inspect.getsource`、讀文本、`subprocess` 只跑 `python3 -c "import …"` 且帶 `PYTHONDONTWRITEBYTECODE=1`）：
    - `trader/.pytest_cache/v/cache/nodeids`、`lastfailed`：mtime **11:42:15**，正是我 v2 重跑專案 656 支測試那一次。當時已加 `-p no:cacheprovider`，**但它沒有阻止這兩個檔案被覆寫**，所以我原先寫的「之後未再產生」是錯的，在此更正。該目錄 10-16 06:57 既存、內容可由 pytest 重建；我不刪除它，避免再做一次不可逆動作。
    - 交付前自檢時發現 `test_runs/` 下有三個 `probe*` 目錄（`probe28ka_xgm0`、`proberbcw5n6i`、`probe325no2bav`，mtime 今日 10:31–10:36），內容是合成資料（`KO_price.json`＝210.55、`price_note.txt`、一個一次性 `trader.db`）。這是我在第一輪驗證價格隔離／沙箱行為時、**用專案自己的暫存根**建的探測目錄（我的正式重現測試一律用 pytest 的 `tmp_path`，不寫這裡）。**專案原始檔沒有被動過**——這三個目錄是新增的暫存，可直接刪除；我留著是因為「刪除」對該路徑同樣是寫入，該由你決定。另外 `/Users/Shared/berkshire-sandbox-zongen/analyze-2026-10-16/OTIS/_FULL_B_1/dossier/OTIS_price.json`（現價 70.39）這類真價檔**我刻意沒有刪除或移動**——那是你的資料，刪除不應由唯讀審查者做；請你自行處理（它的存在本身就是 P0-7／N-9 的證據）。
    - **完備性**：§0.3 那條 `find -newermt "2026-10-17 09:20"` 的全樹掃描顯示，扣除 `state/`（你自己的執行產出）與 `test_runs/` 之後，專案樹內 09:20 以後被改過的檔案只有四個——上述兩個 pytest 快取，加 `.DS_Store`（09:44，早於我 10:31 的第一個探測目錄）與 `.workbuddy-ai/memory/2026-10-17.md`（11:44，另一套工具的記憶檔）。兩者都不是我寫的，所以我一方在專案樹內的痕跡就是上面那兩項，沒有第三項。

---

## 12. 第三輪：真實性裁決與過度工程化檢查（v3）

這一輪我只問兩個問題：

1. **那個狀態，正常流程走得到嗎？** 判準很硬：如果某條結論只能靠「測試直接把壞狀態塞進去」才成立，而框架裡找不到通往那個狀態的寫入點，那它不是一個缺陷，只是一個**註記**。
2. **我提的修法，規模與風險成比例嗎？** 凡是「為 4 處壞引用建一支 lint」「為一個鍵名建跨模組契約測試」「為一份已在資料庫表裡的名單重建點對點資料層」這類，一律視為我把工程講大了。

方法：把專案模組 import 進我自己的程序做 `inspect`／簽名檢查；用 `subprocess` 只跑 `python3 -c "import …"`（帶 `PYTHONDONTWRITEBYTECODE=1`）；讀檔案時間戳自查邊界；其餘全部走 `test_repro_round3.py` 的 11 支測試。**專案原始碼一個字都沒動**（我在專案樹內留下的兩類副產品已於 §11 第 10 項實測更正）。

### 12.1 逐條裁決

| ID | 對象 | 裁決 | 依據 |
|---|---|---|---|
| **C-1** | N-1／P0-2／P1-3 傳導鏈裡的 `pipelines/review.py:55`（刪除值為 `None` 的鍵） | **撤銷**（不構成失靈原因） | `test_none_key_drop_in_review_py_is_a_no_op_for_review_due`：`check()` 對「鍵＝None」與「鍵不存在」輸出完全相同。真正的原因只有一個：`e.get(k) and …` 把**缺值當沒觸發**（`review_due.py:119-124`） |
| **C-2** | N-1 的生產點 | **可達，且比我原本寫的更直接** | `test_full_writeback_drops_sell_lines_when_model_entry_omits_them`＋`test_full_lane_never_reconciles_registry_entry_with_cap`：FULL 回寫的登錄表內容＝模型自寫的 `registry_entry.json`（`writeback.py:195`），`held` 只保留 4 欄（`:248-251`）；框架在 `deep.py:346-357` 自己算出 `cap` 卻只用 `apply_cap_override` 覆寫**訊號卡**。另外 `writeback.py:561`（PRICE 覆寫）會把既有線蓋成 `None` |
| **C-3** | P1-3 空模板抹掉估值記憶 | **成立但範圍縮小** | `test_holding_row_is_protected_from_the_blank_template`：`writeback.py:335-337` 對 `prev["status"]=="HOLDING"` 走保留分支 → 只有 WATCH／非持股階段會被清 |
| **C-4** | N-5 `g_term` 私自加 0 下限 | **偏差真實、幅度誇大 → 降 P2** | `test_g_term_floor_effect_is_small_and_regime_dependent`：30Y=0.9%→＋0.50%、0.5%→＋2.44%、0%→＋4.77%、**≥4%（目前利率區間）→ 0%**。「系統性放寬買進上限」這句我收回 |
| **C-5** | §2「上限消失 → 任意價買進」 | **撤銷（方向相反）** | `test_gate2_is_fail_closed_when_auto_buy_cap_is_none`：`auto is None → gate2="無上限"`，`passed` 需要 `gate2=="通過"` → **缺上限是不買**。買進側一直是對的 fail-closed；金錢風險全在賣出側（C-2） |
| **C-6** | N-7「框架不讀 `g_impl_gt_cap`」 | **整條撤銷** | `test_g_impl_gt_cap_flag_is_redundant_for_gate2`：掃價 7 檔、旗標亮起的 5 檔全部 `price ≥ cap ≥ auto_buy_cap` → 早就過不了 gate2。保留的只有 `solve_g` 上界 0.0999 < `G_IMPL_MAX`=0.10 的**窄帶誤標**（不影響下單）→ 降 P2 |
| **C-7** | N-11「`universe_versions` 只寫不讀」 | **事實錯誤，已更正；核心仍成立** | `universe.py:55` 讀該表 sha256 做變動偵測；且 `db.py:53` 顯示該表**已存 `tickers` 全清單** → 修法從「資料層工程」降為 `load_universe` 加可選 `asof` 回查（約 6 行） |
| **C-8** | P1-5「拒絕後卡死」 | **可達（端到端實測）→ 降 P2，並承認與文件一致** | `test_rejected_approval_row_is_a_dead_end_within_the_same_run`：同 run 無出口（`gate()` 不重建、`approve()` 拋錯、`release()` 不撈），但**下一個 run 會重建 pending 列**，而 `approvals.py:4` 本來就寫「下次觸發再排」。另更正函式名（我寫的 `ensure()` 不存在，實際是 `gate()`） |
| **C-9** | **新增 N-13** | 真實、可達（每次單獨匯入必崩） | `test_review_jobs_cannot_be_imported_standalone`：`import trader.jobs.review_jobs` 單獨執行必拋 `partially initialized module 'trader.jobs.deep'`；先 import `handlers` 就好。專案自己的 `tests/test_review_jobs.py:18` 靠別的模組先匯入才存活（`--collect-only` 單檔可過） |
| **C-10** | N-3「唯一的機械式退出」 | **措辭誇大，結論不變** | `review_jobs.py:139-144`（荒謬線→TRIM）與 `hold_jobs.py:68-88` 是另外兩條機械路徑；但它們讀的還是 C-2 那批可能缺值的線 → 「共同上游是 N-1」成立，「唯一」二字收回 |
| **C-11** | N-8 的金錢方向 | **重述**：買進側 fail-closed（機會成本），賣出側才是風險（`write_price_result` 用 `None` 覆寫既有線） | 同 C-5＋C-2 |
| **C-12** | N-10「RED_TEAM 未列隔離」 | **降為文本問題** | `deep.py:366`：RED_TEAM 排在 FULL_B 之後，此時價格檔依程序已合法開啟 → 沒有可利用的洩漏面；剩下的是 schema 把「用價格作答 G1–G6」列為可舉報項、而該判斷發生在 FULL_A 的**對應缺失**（並入 §7） |
| **C-13** | D-6「用可能過期的價格檔判估值賣出」 | **下修為穩健性註記** | 機制確認（只讀 `price`、不比 `price_date` 與 asof；`sorted(glob)` 取最後一檔），但 `{T}_price.json` 由 `dossier_prep.build`（`:305`）當次現造，且每檔正常只有一支 → 「過期」前提無法證明。建議動作降為：取價時核對 `price_date <= asof`（該鍵確實存在，`build_dossier.py:102`） |
| **C-14** | D-7「`approved.py` 全程持 `pipeline_lock`」 | **改為設計取捨／觀測項** | `approved.py:29-31` 的註解自己寫明意圖（「等排程中的工作做完再寫帳本」）。阻塞每日排程只在長任務時發生，屬取捨不是缺陷 |
| **C-15** | S-10 引用截斷 | **`△`→`[V]`，行號更正** | 實查 `procedure/11_distress_check.md:14/:15/:16/:30`（v2 我漏引 `:15`） |
| **C-16** | P1-14「post 與 done 非同一交易」 | **結構確認（未注入崩潰）** | `engine.py:318-329` 先 `post()`（`_write_rows` 本身三表原子，見 `writeback.py:122-127`）再 `:329` 標 done；`:157` 把殘留 `running` 降回 `pending`。重放時 `next_decision_id`（`:115-119`，n＝既有同檔同日數＋1）會產生 `-2` 決策列與第二份論點增補（`_write_addendum`，`:164`） |
| **C-17** | §1「28 條 fail-open」 | **計數不變**（22＋6 逐條重驗仍可達）；其中 2 條的機制／方向描述按 C-1、C-5 更正。v2 的 N-5／N-7／N-10 降級或撤銷，但它們不屬這 28 條（屬規則分岔，非 fail-open） | `test_repro_round3.py` 的三支 N-1 測試＋gate2 fail-closed 對照組 |
| **C-18** | 我 v3 一度懷疑的第三個向量：`book.py:260` 手工重建只改 `registry.status` **欄位**、不改 `data`，可能造成雙軌不一致 | **撤銷（我推論錯了）** | `pipelines/review.py:57` `d["status"] = r["status"] or d.get("status")` 以欄位覆蓋 data → 不一致會被正規化掉；`ledger/broker.py:101-114` 的 `_registry_update` 也是「合併、只改 status/cost/first_buy_date，其餘保留」。**因此 N-1 的生產點只有 C-2 那兩處** |

### 12.2 仍然「不可證」的部分（我把口徑收斂後寫在這裡）

- **模型會不會真的交回缺欄的 `registry_entry.json`：我沒有實際痕跡可引。** 所以 N-1 的主張已從「登錄表的線現在是空的」收斂為「**框架沒有任何比對點**，所以模型交回什麼就是什麼，而它交回的數字可以與自己算出的 `cap_result` 長期分岔且無註記」。前者是不可證的恐慌，後者是可證的設計缺口——**這是本輪最重要的一次改寫**。
- 我沒有跑過任一 pipeline、沒有呼叫過任何模型（§11 第 1、2 項照舊適用）；`evidence_*.md` 的數字我沒有複算（§11 第 3 項）。
- 「沙箱彼此可讀」是可證的事實，「模型會去讀」仍是推論（N-9 原文已如此標）。

### 12.3 過度工程化檢查：我砍掉了什麼

| ID | v2 的建議 | v3 裁決 | 理由 |
|---|---|---|---|
| T-1 | 「加正規化器↔消費端鍵契約測試」 | **取消**，改為對 `cash_per_share` 那一支鍵的單點斷言 | 全域鍵契約要列舉所有生產／消費端，是新建一套測試基礎設施；P0-1 只是一個鍵名，一行修＋一支測試就能鎖死 |
| T-2 | 「實作 `SELL_REVIEW` 的處置」「`GRADE_CAP` 之外等級要能觸發 TRIM 或明示不處理」 | **移出修 bug 清單**（改列為需你拍板的行為設計） | 這是**新增交易行為**（會改變賣出決策本身），不是把錯誤實作改對。v3 只保留「缺值必須寫 `anomalies`」 |
| T-3 | 「資料層點對點：`load_universe` 加 `asof`（或新建版本化股票池）」 | **縮小**為 6 行回查 | `universe_versions` 已經存了 `tickers` 與 `asof`（C-7）。我 v2 因為「只寫不讀」這個錯誤事實，把一個查表動作想像成資料層工程 |
| T-4 | 「隔離改 allow-list＋每 run 獨立根目錄與結束清理＋`open_price_file` 綁 run_id 交叉核對＋scrub 數值掃描涵蓋 `.md`/`.txt`＋RED_TEAM 入隔離」 | **砍到兩件**：`router.py:22` 補 `state_dir`；`isolation.py:18` deny 兄弟沙箱 | 五個子項裡只有兩個是一行級。allow-list 重寫是在改整個執行模型，而目前的威脅是「同 uid 的模型**可以**去 glob」而非「已經發生」；單機單用戶下，代價與風險不成比例。RED_TEAM 那半隨 C-12 撤銷 |
| T-5 | 「加引用 lint（檔名、X 代碼、規則編號存在性檢查納入測試）」 | **取消**，手修 4 處壞引用 | 為 4 行文本建一支持續維護的工具，是把自己的疏失轉成專案的長期負擔。規則文本本該一次清乾淨 |
| T-6 | 「全期（非 per-run）預算」 | **取消**，只留「`require_for_tiers` 空清單時顯示警告」 | 全期預算屬新功能（要動 schema、遷移、儀表板）；警告是一行，且直接對「閘門被整扇關閉卻無痕」這個真實風險 |
| T-7 | 「`review_due.check()` 回傳三元組時附『哪些線未受檢』」 | **取消**，改在寫入端守衛（§9 第 0 項） | 在讀取端補三元組＝改一個 skill 腳本的 API，還要同步改呼叫端與測試；而在寫入端守住那三條線，是同一個目的、一半的接觸面，而且修的是根因 |
| T-8 | 「`validate` 對**所有**載荷欄位加 min_length 與陣列非空」 | **縮小**為 `DOSSIER.dq_checks`＋THESIS 載荷兩處 | 全欄位加嚴會把「格式檢查器」變成「內容檢查器」，附帶大量誤退（模型寫得對但簡短就會被打回）。只有這兩處的金錢含義可證（P0-5／N-4） |
| T-9 | 「`g_term` 下限移除」「gate2 讀 `g_impl_gt_cap`」「`incr_roic_gt_rd` 補實作」 | **移除或延後** | C-4／C-6；而 `incr_roic_gt_rd`（N-6）的正確動作是**從文件與例檔刪掉那欄**，補實作等於我在替規則做決定 |

**規模對比（老實算法）**：v2 的 §9 要動 9 個子系統，其中 3 項是新建基礎設施（契約測試、lint、隔離模型重寫）＋1 項資料層改造；v3 同樣列 15 項，但 **13 項是單點 ≤6 行的修改**（只有第 4 項 schema 逐欄 `enum`、第 7 項點對點兩端超過），新建基礎設施 **0 項**，被砍掉或縮小的建議 9 項（T-1～T-9）。我接受「這樣風險沒有被根治」這個批評——但根治隔離與內容驗證是**另一件工作**（要你先排程與給預算），把它混進「修 bug 優先序」只會讓前 3 項永遠排不到。

### 12.4 淨結論（v3）

- **今天值得動手的只有兩件**：§9 第 0 項（登錄表三條線的兩處守衛，堵住全部賣出側靜默的上游）與第 1 項（`daily.py:294` 的 `cash_per_share`，決定「系統打敗 SPY」這句話能不能信）。其餘都可以排在這兩件後面。
- **我現在認為不必修**：N-5（利率回到 <2% 再談）、N-7（撤銷）、N-10（文本）、D-6／D-7（下修）、P1-5（等下次重構 approvals 一起處理）。
- **我這一輪最大的方法論教訓**：v2 的 6 條「新增缺陷」裡有 2 條（N-5、N-7）是我從「程式與規則不一致」直接推到「所以錢會變少」，中間那一步（幅度、以及閘門是否本來就會攔住）沒有算。第三輪把每一步都變成可執行的對照組之後，2 條倒下、1 條（N-1）換成一個比我原本寫得更嚴重但更可證的形態。**如果你從這份報告只帶走一件事，我建議是 C-2＋C-5 的組合：你的買進側是保守的，你的賣出側在資料缺值時會完全靜默，而且框架明明手上有正確數字（`cap_result.json`）卻從來沒用它檢查登錄表。**
