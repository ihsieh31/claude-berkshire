# Berkshire 專案對抗式審查報告（deepseek）

- 審查日期：2026-10-17（第二版：全部發現經實測驗證後修訂）
- 審查範圍：`/Users/zongen/Downloads/claude code/berkshire` 全部程式、skill 文件、prompt、schema、測試
- **本報告與兩支腳本只寫在 `bug report/deepseek/`；專案本體零改動（已用 `find -newermt` 驗證）**
- 腳本：
  - `probe_cap_calc.py`：對 `cap_calc.py` 做參數掃描（只 import，不寫入）
  - `verify_findings.py`：24 項事實查核，用真實 SQLite、真實 `broker`／`sizing`／`writeback`、真實 `screen.statistical()`、真實 `hold_jobs.check()`；暫存檔全在 `tempfile`

---

## 0. 結論摘要

整體架構是我看過的同類系統裡少見地嚴謹：價格隔離、決策日誌、可重跑的 DAG、公司行動覆核、把「推論」與「原文」分開標記，這些都是真本事。**問題不在工程紀律，而在「估值 → 買價 → 下單」這條價值鏈的中段被簡化掉了，以及幾處「框架說以腳本為準、但腳本其實沒管」的缺口。**

本版已對每一條做實測，結果：**21 條確認、2 條已被其他層緩解、1 條我原本高估**（詳見 §0A）。嚴重度已按實測重排。

最該先處理的五件事：

| # | 問題 | 影響 | 嚴重度 |
|---|---|---|---|
| 1 | `cap_input.json` 把 `base_g0`／`opt_g0` 寫成顯式 `null`（而非省略鍵）⇒ `IV_base` 變 `None` ⇒ 買價上限退回 P2′（實測 **+12.9%**）、`sell_review_above` 變 `None`（賣出檢視**永久失效**）、P-21 寬度檢查靜默消失 | 一個 JSON 寫法同時放寬買價、關掉賣出紀律；已用完整鏈路實測 | **高** |
| 2 | `cap_calc` 完全沒有輸入驗證／單位檢查：`g10: 7` 被當成 700% 成長（夾成 8%）、`long_rate: 4.7` 讓 `r_d` 變 **473%** | 計算器是「以腳本為準」的仲裁者，卻對最常見的 LLM 錯誤無防禦，且錯誤方向幾乎都是放寬 | **高** |
| 3 | FULL_B 路徑**不覆核** `absurd_price_line`／`sell_review_above`／`cap_components`（只有 PRICE 快速通道會注入荒謬級線） | 模型可寫 `null`（schema 允許），直接關掉「正常化 P/E ≥50 → 至少賣一半」與「≥ IV_base → 賣出檢視」 | **高** |
| 4 | 加碼批次在「下單」時就扣 `tranches_left`，未成交／逾期不回補 | 實測：下單後 3→2，訂單逾期作廢後仍為 2。三批全落空 ⇒ 建倉靜默停在首批 50% | **中高** |
| 5 | 三個地方讀不同的「等級」：閘門 2 用 `grade_used`、部位上限用 `certainty_grade`、權重減碼用 registry `grade` | 實測：`grade_used=B` 只寫進 decision log，registry 仍是 `A` ⇒ P-21 降級不影響 15%／10% 的部位上限 | **中高** |

以下依「獲利邏輯 → 估值實作 → 交易帳務 → 資料時點 → 治理流程 → skill 撰寫 → 測試 → 過度工程檢視」分節。

---

## 0A. 驗證結果與自我修正

| 代號 | 判定 | 一句話證據 |
|---|---|---|
| A1 | CONFIRMED | 21 組參數中 18 組 `auto_buy_cap == IV_base`；P2′ 只在 30Y < 4% 時 binding；cap÷P1 = 1.73–1.78× |
| A2 | CONFIRMED | 金融股補上 PE 中位 → 上限 45 → 75（**+67%**） |
| A3 | CONFIRMED | g=0/2/5/8/10% 五組**全部** `IV_base == IV_opt`；只有 g=15% 才分開 |
| A4 | CONFIRMED | g10=−5% → 永續仍 3%，永續段佔 IV_base 的 14.5%；`IV0×0.75=37.5` vs `cap=51.15` |
| A5 | CONFIRMED | L=3%、20 年中位 4.5% → `r_stress == r_d == 10%`、`P3 == P1` |
| B1 | CONFIRMED | 完整鏈路：3 項不一致 → 覆寫後 `sell_review_above=None`、`value_per_share.base=None`、上限 +12.9% |
| B2 | CONFIRMED | `long_rate=4.7` → `r_d=473%`、`cap=1.15`；`roe=18` 讓 W-PE 放寬條件失效 |
| B3 | CONFIRMED | 金融股 `P2_prime_with_E_x0.9 = 45.0 = P1`（完全相等） |
| C1 | CONFIRMED | `tranches_left` 3→2，逾期作廢後仍 2（不回補） |
| C2 | CONFIRMED | `match(10-19)` 成交 0 筆、`match(10-20)` 成交 1 筆 → 冷靜期是 2 個交易日 |
| C3 | CONFIRMED | B 級 30% 權重 → 減 1400 股；C 級 30% 權重 → plans 內**完全沒有這檔** |
| C4/D5 | CONFIRMED | `grade_used=B` 只進 decision log，registry 仍 `A`；`registry.weight` 停在 0.05 從不更新 |
| C5 | CONFIRMED | 委託 100 股、現金不足 → 成交 10 股、狀態 `filled`、`cancel_reason` 寫「其餘股數不再撮合」 |
| C6 | CONFIRMED | 唯一會讀 `anomalies` 的行動路徑是 `book.paused()`（只取 `pause_trading=1`），本異常是 0 |
| D1 | CONFIRMED | `scrub_price_text("平均回購價 \| 78.53")` 原樣保留（「股價」那行才被刪） |
| D2 | **MITIGATED** | skill 的 `records(split=False)` 確實保留 `price`，但 trader 的 `scrub_prices` 會清掉且殘留就 `raise` |
| D3 | CONFIRMED | 無 SIC、負債/資產 0.3、ROA 10%（完全不像金融）→ `fin_source='近似（無 SIC）'` |
| D4 | **OVERSTATED** | 我原說「50–70% 每輪觸發」；實算：年化波動 25% 約 **11%**、35% 約 51%、45% 約 82% |
| E1 | CONFIRMED | `compare_signal_with_cap` 只比 4 個 IV；原始碼不含 `absurd`／`sell_review_above`／`cap_components`；`cap_calc` 不輸出荒謬級線 |
| E2 | CONFIRMED | TRIM＋業務 → 被擋；TRIM＋估值＋`fraction=0.9`（一次賣 90%）→ 零錯誤 |
| E3 | **MITIGATED** | 順序確實是 `post()` → `post_script()`，但**全專案 0 個 handler 覆寫 `post_script`** |
| E4 | CONFIRMED | `max_grade_from_C1='A'` 有回傳；`trader/` 內只有 `checks.py` 的失敗預設值提到它，無人使用 |
| E5 | CONFIRMED | 完全不填 `price_file_opened_at` → `gate3_pass=True`；填了早於作答的時間才被抓 |

### 三處自我修正

**(1) A1 從「高（bug）」降為「中高（設計選擇造成的語意衝突）」。** `SKILL.md` §3 最後一條明寫：

> 「倍數、跌幅、自由現金流殖利率、P/B 都不是買進理由；**達到上限即可買，不必等更便宜**。」

所以「安全邊際由 `r_d = max(10%, 30Y+3pp)` 承擔、P1 只是報告欄」是**刻意的**，不是疏漏。但它留下三個真實的內部矛盾（見 A1），仍值得修——性質是「文件與語意」而非「程式錯誤」。這是全報告唯一被我改變「性質」而不只是「嚴重度」的一條。

**(2) D4 從「中（成本）」降為「低（不建議修）」。** 用常態報酬重算 10 個交易日窗口內至少一次 |單日| ≥ 4%：年化波動 20% → 1%、25% → 11%、35% → 51%、45% → 82%。原報告說「多數持股每兩週都會觸發」是**錯的**——一般持股只有約 11%，觸發偏多的是高波動股，而那正是最該檢查的族群。**這條撤回。**

**(3) E3 從「中」降為「低（介面設計債）」。** 順序問題是真的，但 `post_script` 目前沒有任何實作者，現在不會發生。不該排進修復前段。

### 兩處「已被緩解」——不該當 bug 修

- **D2**：trader 讀完交接檔後會 `scrub_prices(hand)`，殘留價格鍵就 `raise ScreenError`（`pipelines/screen.py:146-148`）。skill 端 `special_watch` 沒拆 `price_context` 是真的，但自動化路徑安全。修它只是「讓兩條 lane 一致」，一行 `split=True`。
- **E3**：同上，等真的有人需要 `post_script` 再修。

---

## A. 獲利邏輯（第一性原理拆解）

### A1. 買價上限 ≡ 基準情境合理價，P1 只是報告欄（中高：設計選擇＋語意衝突）

**位置**
- `SKILL.md` §3：「自動買進上限 auto_buy_cap = min(P2′, IV_base)」「P1（報告）= 等級係數 × IV_cons」「達到上限即可買，不必等更便宜」
- `references/procedure/05_valuation_caps.md:51` 同義；`:44` P1 標明「**報告欄**：安全邊際、G8 預期報酬用；**不進 min**」
- `scripts/cap_calc.py:132` `P2 = pv(oe, G_IMPL_MAX=0.10, r_d, g_term_i)`；`:87` `g0b = min(max(hist), 0.10)`；`:153-161` `capc = [P2] or [P4] or [P1]`、`abc = min(cap, iv_base)`

**實測（21 組參數掃描 + 5 個利率點）**

| 歷史成長 | P1（安全邊際價） | IV_cons | IV_base | auto_buy_cap | 上限 ÷ P1 |
|---|---|---|---|---|---|
| 7%／6% | 50.29 | 67.06 | 89.53 | **89.53** | **1.78×** |
| 15%／14% | 57.73 | 76.97 | 101.07 | **101.07**（P2′ 與 IV_base 完全相等） | **1.75×** |
| 2%／1% | 39.46 | 52.61 | 68.37 | **68.37** | **1.73×** |

| L（30Y） | r_d | g_term_impl | P2′ | IV_base | cap |
|---|---|---|---|---|---|
| 2% | 10% | 1% | 87.85 | 89.53 | 87.85 |
| 3% | 10% | 2% | 93.63 | 89.53 | 89.53 |
| 4% | 10% | 3% | 101.07 | 89.53 | 89.53 |
| 7% | 10% | 3% | 101.07 | 89.53 | 89.53 |

**結論**：`P2′ = pv(oe, 10%, r_d, min(3%, L−1pp))`、`IV_base = pv(oe, min(hist,10%), r_d, 3%)`。當 `L ≥ 4%` 時 `g_term_i = 3%`，且 `g0b ≤ 10% = G_IMPL_MAX` ⇒ **P2′ ≥ IV_base 恆成立**，`auto_buy_cap ≡ IV_base`（例外：模型在 `base_g0` 直接給 >10%，此時反由 P2′ 約束——等於讓模型自己決定上限）。**P2′ 只在 30 年期殖利率 < 4% 時才會成為約束。**

**三個仍然成立的內部矛盾**（這才是本條的重點，不是「該不該有安全邊際」）

1. **語意**：`SKILL.md` 把 P1 叫「安全邊際欄」，訊號卡 schema 也保留 `cap_components.P1_report_only`（`assets/signal_card_schema.json:32-37`）。於是卡片上同時有「P1 安全邊際價 50.29」與「自動買進上限 89.53」，而程式只認後者。讀者會以為安全邊際已被執行。
2. **兩把尺**：G8 機會成本用 P1 算預期報酬（`SKILL.md` §2 G8、`05:44`），買進門檻用 `IV_base`。同一檔股票在「排序」與「買不買」用不同價格，G8 勝出與否無法對應到實際買價。
3. **一致性條款退化**：`apply_cap_override` 把 `sell_review_above` 設為 `IV_base`（`checks.py:178`），而買價上限也是 `IV_base` ⇒ 兩者數值相同，「買價不得高於賣出檢視線」的約束只剩 `buy_limit` 的 `cap × 0.9999`（0.01%，`sizing.py:105`）。

**建議（依代價排序）**
- **不改行為、只改文件**（成本最低）：把 P1 從「安全邊際」正名為「G8 預期報酬參考價」，並在報告第 9 段標明「本系統的安全邊際由 `r_d` 承擔」。同時刪掉 `buy_price_cap` 與 `auto_buy_cap` 並存的雙上限（見 B5）。
- **改行為**（需回測）：`auto_buy_cap = min(P2′, IV_base, P1)`。注意這會大幅減少買進機會——先用歷史資料回測「若當時用 P1，會少買幾檔、少賺多少」，再決定係數（A 0.85／B 0.75 是規則書原值，不宜再放寬）。
- 不論走哪條，加一條不變式測試：`auto_buy_cap < sell_review_above`（嚴格小於，差距 ≥ 10%）。

---

### A2. 資料越完整，買價上限越寬鬆（中）

**位置**：`cap_calc.py:153` `capc = [v for v in (P2,) if v is not None] or ([P4] if P4 is not None else [P1])`

**實測**
- 金融股、缺 `largecap_pe_median`：`P2′=None`、`P4′=None` ⇒ `cap = P1 = 45.0`
- 同一組參數補上 `largecap_pe_median=19`：`P4′=79.8` ⇒ `cap = min(79.8, IV_base=75) = 75.0`（**+67%**）

**影響**：`cap` 這個欄位在不同公司代表三種不同東西（P2′／P4′／P1），但對外一律稱「P2′ 硬上限」（`checks.py:177` 的 `buy_price_cap = cap["cap"]`）。而且資料越少越保守、資料越齊越激進——這會誘使「把欄位填滿」而非「把不確定性誠實標成缺」。

**建議**：輸出加 `cap_basis`（`P2′|P4′|P1`），並讓 `buy_price_cap` 只放 P2′（缺就 `null`），不要用 fallback 鏈冒充同一個欄位。這是顯示層修正，不動買價。

---

### A3. 三情境退化：多數股票的 `IV_opt` 等於 `IV_base`（中）

**位置**：`cap_calc.py:87-88`
```python
g0b = x.get("base_g0", min(max(hist), 0.10) if hist else None)
g0o = x.get("opt_g0",  min(0.15, max(hist)) if hist else None)
```

**實測**：g = 0／2／5／8／10% 五組，**全部** `IV_base == IV_opt`（例：g=3% 時兩者皆 73.5714）；只有 g=15% 才分開（101.07 vs 123.32）。

**影響**
1. P-21 的「`IV_opt ÷ IV_cons` > 3 降一級、> 4 不做」實際量到的是 `IV_base ÷ IV_cons`，與規則文字（樂觀 vs 保守）不符；對低成長股這個比值永遠 < 2，檢查形同虛設。
2. 報告與訊號卡會出現「樂觀情境 = 基準情境」，讀者會以為估值沒有不確定性。
3. `05:27` 明說樂觀情境「永不當買進依據」，但程式讓它與基準重合。

**建議**：樂觀情境給獨立假設（如 `max(hist) × 1.25`），或承認只有兩情境並移除 `IV_opt` 欄位。**不要**兩者都做（見 §J）。

---

### A4. 衰退企業硬套 3% 永續成長（中高）

**位置**：`cap_calc.py:96` `iv_base = pv(oe, g0b, r_d, 0.03)`；`:37` `path()`；`:47` 終值式

**實測**：`g10=-5%`、`g5=-2%` ⇒ `g0b=-2%`，10 年全程 −2%，**第 11 年起永續 +3%**。`IV_base = 51.15`，其中永續段佔 **14.5%**（`g_term=0%` 時只剩 43.72）。

**影響**
- 「先萎縮 10 年、再永續成長 3%」在經濟上沒有依據，方向還相反，會系統性**高估衰退企業**。
- `SKILL.md` §3 與 `05:38` 明寫「歷史成長 ≤0 → 上限只能用 `IV0` × 等級係數」，但程式只加一行註記（`cap_calc.py:133-134`），`cap` 完全沒變：實測 `cap=51.15`，而 `IV0×0.75=37.50`。**規則沒實作。**
- 保守情境反而正確地允許 `g_term = 0%`（`:86`），造成「基準比現實更樂觀」的錯位。

**建議**：`g_term_base = min(3%, max(0, g0b))`；並把 SKILL 那條真的寫成 `if max(hist) <= 0.005: cap = min(cap, iv0 * k)`。兩行。

---

### A5. 低利率時壓力價 P3 等於 P1（低）

**位置**：`cap_calc.py:71-73`

**實測**：`L=3%`、20 年中位 4.5%（低利率起點）⇒ `r_stress = max(0.10, 0.03+0.06+0.01) = 0.10 = r_d`；`P3 = P1 = 50.29`。

**影響**：壓力價與等級價完全相同，報告上的「壓力價」不反映任何壓力。但 **P3 只是報告欄**（`05:46`、不進 min），所以不影響下單。嚴重度因此降為低。

**建議**：`r_stress = max(r_d + 0.02, L + 0.06 + ...)`。一行，但優先級低。

---

### A6. `g_impl ≤ 10%` 閘門是冗餘檢查（低）

**位置**：`cap_calc.py:128, 132, 183`

**推論**：`P2′` 就是「`g_impl = 10%` 的價格」，而 `cap = min(P2′, IV_base) ≤ P2′` ⇒ 任何低於 `cap` 的價格其 `g_impl` 必然 < 10%，(a) 閘門恆為真。

**影響**：不造成錯誤，只是文件把它寫成「閘門」（`05:36`）而它不會擋任何東西。真正有用的是 (b)(c)(d) 三個反方問題，但它們「答不出 → 寫入 G9 並停在觀察」（`05:37`），G9 是模型自由文字，程式不檢查。

**建議**：文件改稱「一致性檢查」即可；或把 (b) 升級為硬閘門（`g_impl ≤ 歷史 5 年成長的一半`）。**不建議**同時做兩件事。

---

## B. `cap_calc.py` 實作缺陷（全部可重現）

### B1. 顯式 `null` 讓 `IV_base` 消失，連帶放寬買價、關掉賣出檢視（高）

**位置**：`cap_calc.py:87-88`、`:161`、`:103`；`checks.py:178`；`review_due.py:119`

**實測（同一組數字，只差兩個鍵是否存在）**

| | `"base_g0": null, "opt_g0": null` | 不含這兩個鍵 |
|---|---|---|
| `IV_base` | **None** | 89.5258 |
| `IV_opt` | **None** | 89.5258 |
| `width_opt_over_cons` | **None**（P-21 檢查消失） | 1.3351 |
| `auto_buy_cap` | **101.0664**（退回 P2′） | 89.5258 |

**完整鏈路實測**：`compare_signal_with_cap` 檢出 3 項不一致 → `apply_cap_override` 後 `sell_review_above = None`、`value_per_share.base = None`、上限 +12.9%。

**連鎖效應**
1. 第一次退回重寫；第二次 `apply_cap_override` 把 `sell_review_above` 設成 `cap["IV_base"] = None`（`checks.py:178`）。
2. `write_full_result` / `write_price_result` 把 `None` 寫進 registry（`writeback.py:561`）。
3. `review_due.py:119` `if e.get("sell_review_above") and p >= ...` ⇒ **賣出檢視永遠不觸發**。
4. 買價上限同時從 89.53 升到 101.07。

**為什麼很可能發生**：`routing/prompts/FULL_B.md:12` 只說「格式見 `scripts/cap_calc.py` 說明」，而說明用的是「缺：預設…」這種模糊措辭。LLM 把 schema 所有鍵列出、沒有的填 `null`，是最常見的行為。

**建議**：`x.get("base_g0") or 預設`（或 `if ... is not None`）。另在 `run_cap_calc` 加硬檢查：`hist` 非空但 `IV_base is None` ⇒ `CheckError`，不要產出一張沒有賣出檢視的卡。

---

### B2. 沒有任何輸入驗證或單位檢查（高）

**位置**：`checks.py:85-100`（只檢查能否解析、能否計算）；`cap_calc.py` 全檔無驗證

**實測**

| 誤填 | 後果 |
|---|---|
| `g10: 7, g5: 6` | `g0.cons` 被夾成 0.08（看起來合理）；`cap` 89.53 → **101.07（+12.9%）** |
| `long_rate: 4.7` | `r_d = 473%`、`cap = 1.15` |
| `roe: 18, eps_cagr5: 10` | W-PE 的放寬條件（`>= 0.18`）永遠不成立，門檻從 25 掉回 20 |

**影響**：這是整個系統唯一的「以腳本為準」仲裁者（`jobs/deep.py:357`「cap_calc 覆核兩次不一致，訊號卡上限與 IV 以腳本覆寫」）。它對最常見的 LLM 單位錯誤完全無防禦，而且**錯誤方向幾乎都是放寬**（百分數被夾到上限 ⇒ 估值變高）。

**建議（依成本排序）**
1. 範圍檢查：`g10/g5 ∈ [-1,1]`、`roe ∈ [0,1]`、`eps_cagr5 ∈ [-1,1]`、`long_rate ∈ [0,0.2]`、`oe>0`、`price>0`，超出就 `CheckError` 讓工作退回重寫。約 20 行。
2. 輸出加 `units_echo`（腳本實際解析到的值），讓人工覆核看得到「用了 0.07 還是 7」。
3. **不要**做「自動把 >1.5 當百分數」的靜默正規化——那會讓錯誤更難發現。

---

### B3. 金融股結論測試的「盈餘 ×0.9」是空操作（中）

**位置**：`cap_calc.py:116-122`（override 分支忽略 `oe_`）、`:170`

**實測**：`financial=True`、`iv_cons=60`、`price=44`（距上限 <5% → 觸發結論測試）⇒ `P2_prime_with_E_x0.9 = 45.0`，`P1 = 45.0`，**完全相等**。盈餘減 10% 沒有改變任何東西；`flips=True` 只來自利率敏感度那一條腿。

**影響**：`05:55` 的結論測試要求「以 E×0.9 **或** 敏感度利率重算」——金融股少了一半觸發機會，而金融股正是最需要這類測試的類別。

**建議**：override 分支也套盈餘比例：`x["iv_cons"] * (r_d/r) * (oe_/oe)`。一行。

---

### B4. `calc()` 會改寫呼叫端傳入的 dict（低）

**位置**：`cap_calc.py:93` `x.setdefault("iv_cons", iv_c)`

目前無害（`run_cap_calc` 傳入剛解析的 JSON），但 `PriceHandler.pre_script` 會 `x = _read_json(...)` 後改 `price`／`long_rate` 再寫檔（`review_jobs.py:237-251`），若日後重用同一個 `x`，`iv_cons` 會被上一次的保險公式污染。純函式不純是維護風險，不是現行 bug。

**建議**：函式開頭 `x = dict(x)`。一行。

---

### B5. `cap` 與 `buy_price_cap` 的語意漂移（中）

**位置**：`cap_calc.py:149-161`；`checks.py:177`

**現象**：訊號卡同時有 `buy_price_cap = cap`（可能是 P2′／P4′／P1）與 `auto_buy_cap = min(cap, IV_base)`。以探針 case 2 為例：`buy_price_cap = 101.07`、`auto_buy_cap = 89.53`、`sell_review_above = 89.53`——**卡片上的「硬上限」高於「賣出檢視線」**，直接違反 `SKILL §4`「買價不得高於 `sell_review_above`」。

**影響**：`sizing.plan_buys` 只用 `auto_buy_cap`（正確），但 `review_jobs.PriceHandler.prepare` 會把上一張卡餵給模型（`review_jobs.py:263`），模型可能拿 `buy_price_cap` 當上限。

**建議**：移除 `buy_price_cap`，或改名 `p2_prime_reference` 並在 schema 註明「不得用於下單」。

---

## C. 交易、下單與帳務

### C1. 加碼批次在下單時就扣，未成交不回補（中高）

**位置**：`daily.py:89-94` `_consume`、`:115-121`（`place_order` 成功後立刻扣）、`:281-290`（逾期 `expired`）；`sizing.py:456-458`

**實測**：`build_plan.tranches_left=3` → `_place` 一筆 ADD → **2**；`cancel_order(status="expired")` 後仍為 **2**（未回補）。

**影響**：限價單若因隔日跳空不成交（`broker.py:188` `bar["low"] > limit`），21 天後作廢但批次已扣。三批全落空 ⇒ 建倉停在 50% 目標權重，`out["expired"]` 只記 order_id，**沒有任何告警**。

**建議**：最小改動是在 `cancel_order(status="expired")` 時回補 `tranches_left += 1` 並記 note；較正確的做法是把 `_consume` 移到 `broker._fill` 成功後（同一個交易內更新 registry）。前者 5 行，後者要動 broker。

---

### C2. 冷靜期實際是 2 個交易日（中）

**位置**：`daily.py:101-102`（`created = next_day if cooling else date`）；`broker.py:233-235`（`created_date < date`）

**實測**：`created_date=2026-10-19` 的單，`match_orders(2026-10-19)` 成交 **0** 筆、`match_orders(2026-10-20)` 成交 **1** 筆。

**影響**：所有 PRICE 觸發的下單都晚一天成交。與 `UNFILLED_EXPIRE_DAYS=21`、`BUILD_CAL_DAYS=21` 疊加後，建倉窗口實際只剩 19 個交易日。

**建議**：**不要**新增 `eligible_date` 欄位（那是過度工程）。最小修法：維持 `created_date = date`，在 `plan` 上帶 `cooling=True`，`match_orders` 跳過「當日建立且 cooling」的單；或直接承認 2 日並更正註解與文件。後者 1 行。

---

### C3. 等級 C/D 的持股永不因權重超限減碼，且「僅記錄」是假的（中）

**位置**：`sizing.py:414-416`

**實測**：B 級、實際權重 30% → 產生減碼 **1400 股**；C 級、同樣 30% → `plans` 內**完全沒有這檔**（連 `qty=0` 的記錄都沒有）。

**影響**
1. 被降為 C/D 的持股可以漲到 40% 權重而系統完全不出聲、不減碼、不留痕。
2. 等級降級通常發生在基本面轉弱時，正是最該控制集中度的時候。

**建議**：至少 `plans.append(_plan(..., qty=0, reason="等級 C/D 權重超限，僅記錄"))`；較好是給 C/D 一個上限（如 5%）並允許減至上限。

---

### C4. 三個地方各自讀不同的「等級」，P-21 降級不影響部位（中高）

**位置**
- 閘門 2：`review_jobs.py:293` `grade_ok = cap.get("grade_used") in ("A","B")`
- 目標權重：`sizing.py:82` `grade = card.get("certainty_grade")`
- 權重超限：`sizing.py:414` `GRADE_CAP.get(reg.get("grade"))`
- `apply_cap_override`（`checks.py:156-191`）**不覆寫** `certainty_grade`
- `write_price_result`（`writeback.py:543`）把 `grade_used` 寫進 **decision log**；registry 更新（`:560-562`）不含 `grade`

**實測**：`cap_calc` 的 `grade_used="B"`（P-21 降級）→ decision log `grade="B"`，但 registry `grade` 仍是 **`"A"`** ⇒ `GRADE_CAP` 與 `plan_sells` 都用舊等級，部位上限仍是 15%（A）而非 10%（B）。

**影響**：降級只影響「能不能買」與 P1/P3/P4 係數，不影響「能買多少」——與 P-21「不確定性高 → 更保守」的意圖相反。

**建議**：`apply_cap_override` 同步寫 `certainty_grade = cap["grade_used"]` 並記錄覆寫前後；registry 回寫時同步 `grade`。三處收斂成單一來源（建議一律讀 registry）。

---

### C5. 部分成交直接關單，缺口不回寫建倉計畫（中）

**位置**：`broker.py:171-175`

**實測**：委託 100 股、現金僅 1000 → 成交 **10 股**，訂單 `status='filled'`，`cancel_reason='部分成交 10/100（現金或持股不足，其餘股數不再撮合）'`；`registry.build_plan` 沒有缺口欄位。

**影響**：與 C1 疊加，建倉進度會靜默偏低。另外把 `cancel_reason` 掛在 `filled` 訂單上語意奇怪——儀表板若用 `cancel_reason` 篩異常單會誤判。

**建議**：新增 `filled_qty` 或 `partial` 狀態；缺口寫進 `build_plan.shortfall`，下一輪 `plan_tranches` 補回。

---

### C6. 不在登錄表的持股只寫異常，不排分析（中低）

**位置**：`daily.py:173-182`

**實測**：`anomalies` 在 `trader/` 只有三個消費者——`book.paused()`（只取 `pause_trading=1`，`book.py:107`）、`metrics.anomalies()` 顯示（`report/metrics.py:260`）、web 列表。`unregistered_holding` 以 `pause_trading=0` 寫入 ⇒ 不暫停、不排工作、不通知。

**影響**：透過分拆／併購換股取得的持股，不會被分析、沒有 `auto_buy_cap`、沒有 `sell_review_above`、沒有論點檔，也不受任何賣出紀律約束。補救只能靠人工跑 `cli analyze`（`cli.py:676`），但異常沒有連到任何待辦、文件也沒寫這一步。

**建議**：`unregistered_holding` 直接建一個 `DOSSIER` 工作（或 `pause_trading=1` + 儀表板待辦）。不需要新的排程器。

---

### C7. 公司行動的零股現金了結未調整成本（低，未實測）

**位置**：`corp_actions.py:386-399`

`avg_cost` 更新為 `pos.avg_cost * qty / n`，但 `frac × price_after` 的現金沒有從總成本基礎中扣除。合股時零股比例可能很大，每股成本會被高估，未實現損益失真。**這是純算術推論，我沒有寫測試驗證**（需要構造一個會產生零股的合股事件，成本高於本條價值）。只影響報表，不影響下單。

---

## D. 資料與時點（look-ahead / 隔離）

### D1. dossier 自帶「平均回購價」，繞過價格清洗（中）

**位置**：`build_dossier.py:47` `R["平均回購價"] = T.buyback / T.buyback_shares`；`dossier_prep.py:42` 清洗樣式 `r"股價|市值|本益比|目標價|現價|收盤價?|報價|成交價"`

**實測**：`scrub_price_text("平均回購價 | 78.53\n股價 | 86.10")` → 只刪「股價」那行，「平均回購價」**原樣保留**。

**影響**：DOSSIER 沙箱的鐵律是「不得含任何價格」，而 `build_dossier.py:94` 的檔頭還寫著「**本檔不含任何價格、市值、本益比。**」——自相矛盾。平均回購價是歷史每股均價，通常落在市價 ±30% 內，等於給了模型一個強力錨點。

**建議**：把欄位改名為「回購金額÷回購股數」（不減資訊、切斷價格語意），或在清洗樣式加 `回購價|均價`。前者更好，因為它保留了「管理層資本配置品質」這個有用訊號。

---

### D2. `special_watch` 未做 `price_context` 拆分（低，已緩解）

**位置**：`screen.py:170`（`split=True` 才拆）vs `:184`（`special_watch` 沒拆）

**實測**：`records(df)` 保留 `price=86.1`；`records(df, split=True)` 把它搬進 `price_context`。但 `trader` 的 `scrub_prices(hand)` 之後，`special_watch` 已不含 `price`——**自動化路徑安全**。

**影響**：只在「agent 直接使用 `berkshire-screen-merged` 這個 skill」時才洩漏，而 `SKILL.md` 的價格隔離規則對 agent 同樣適用。

**建議**：`special_watch` 也 `split=True`（一行）。若確實要保留（它是「跌深觀察」清單，排序鍵就是回撤），就在輸出加 `"special_watch_has_prices": true` 並在 SKILL.md 註明。**不要**為此改 trader。

---

### D3. `fin_source` 標記失真（低）

**位置**：`screen.py:88-92`

**實測**：一列 `is_financial=NaN`、`liab_to_assets=0.3`、`ROA=10%`（完全不像金融）⇒ `fin_like=0`，但 `fin_source='近似（無 SIC）'`。有 SIC 的列才是 `'SIC'`。

**影響**：模型與人工會把「沒有 SIC」誤讀成「已用財務比率近似判斷過金融業」，讓「金融股用不同百分位」看起來比實際更可靠。純標記問題，不影響計算。

**建議**：`np.select([nofin & approx, nofin & ~approx], ["近似（無 SIC，財務比率）", "無 SIC，視為非金融"], default="SIC")`。

---

### D4. ±4% 觸發頻率（低，原報告高估，撤回）

**位置**：`review_due.py:113-115`

**實測（常態報酬估算，10 個交易日窗口）**

| 年化波動 | 至少一次 \|單日\| ≥ 4% |
|---|---|
| 20% | 1% |
| 25% | **11%** |
| 35% | 51% |
| 45% | 82% |
| 60% | 97% |

**修正**：原報告說「多數持股每兩週都會觸發」是錯的。一般持股（25%）只有約 11%，一年約 2–3 次；只有高波動股（35%+）才會接近一半——而那正是最該檢查的族群。**因此這條不構成設計缺陷，撤回，不建議修改。**

唯一仍值得記一筆的是：`review_due` 對同一檔沒有「THESIS 冷卻期」，若某檔同時命中「單日 ±4%」與「成本 −12%」，會在同一輪重複列出兩個理由（但只會建一個工作，`review.py:225-241` 每檔至多一個起始工作，所以成本沒有翻倍）。

---

### D5. `weight < target_weight` 用靜態權重（中低）

**位置**：`review_due.py:123`；`review_jobs.py:305`

**實測**：`write_price_result` 更新 registry 的 `auto_buy_cap`／`sell_review_above`／`absurd_price_line`／`review_price`／`t30_at_valuation`，**不含 `weight`**；`registry.weight` 只由模型在分析時寫一次（`registry_schema.json:19`），此後不變。實測 registry `weight` 停在 0.05。

**影響**
1. `review_due` 的「持股未達目標且低於上限」在建倉完成後仍可能成立 ⇒ 反覆排 PRICE（成本）。
2. `_map_action`（`review_jobs.py:136`）用同一個舊 weight 判斷「已達目標 → HOLD」，可能一直回 ADD。
3. **但** `plan_buys` 用即時 `mv/equity` 算 `build_amount`，`amount <= 0` 就跳過（`sizing.py:351-353`）⇒ **不會真的超買**。所以是 churn，不是金額錯誤。

**建議**：`review_due` 與 `_map_action` 改讀即時權重（`daily` 已有 `closes` 與 `book.equity`），或改用 `build_plan.tranches_left > 0` 當條件。**不要**把 weight 寫回 registry（會多一份會過期的狀態）。

---

## E. 治理、閘門與流程

### E1. FULL_B 不覆核 `absurd_price_line` / `sell_review_above` / `cap_components`（高）

**位置**
- `jobs/deep.py:334-357`：只跑 `run_cap_calc` → `compare_signal_with_cap` → 必要時 `apply_cap_override`
- `checks.py:149-153`：只比 `auto_buy_cap` 與四個 IV
- `checks.py:156-191`：`apply_cap_override` 設 `sell_review_above`，但**不設** `absurd_price_line`、不碰 `cap_components`
- `jobs/review_jobs.py:29, 254`：`ABSURD_PE = 50`，只有 `PriceHandler.pre_script` 會算它
- `routing/output_schemas.json:37`：`"absurd_price_line": "數字或 null"`

**實測**：`compare_signal_with_cap` 原始碼不含 `absurd`／`sell_review_above`／`cap_components`；`cap_calc.py` 全檔不含 `absurd_price_line`。

**影響**
1. 初次完整分析時，荒謬級線由模型自己寫，可以是 `null` 或 500×E。寫 `null` ⇒ `review_due.py:121` 不觸發 ⇒ **「至少賣一半」永久失效**，直到下一次 PRICE。
2. `sell_review_above` 若模型寫偏高、而 `auto_buy_cap` 與 IV 都對得上（沒有 diffs），`apply_cap_override` 不會執行 ⇒ 錯誤的賣出檢視線直接入庫。
3. `cap_components`（P1／P2′／P3／P4′）既不比對也不覆寫 ⇒ 卡片上的四個分量可以與 `auto_buy_cap`／`IV_base` 完全脫節。這是「腳本為準」原則的唯一破口。

**買進端被三重覆核，賣出端卻有一條完整路徑完全不覆核，不對稱。**

**建議**
1. 把 `ABSURD_PE × E` 移進 `cap_calc.calc()` 輸出，兩條路徑自動一致（順便修掉 `review_jobs.py:307` 那句不實的註記「荒謬級線以 cap_calc 覆寫」）。
2. `compare_signal_with_cap` 加入 `sell_review_above`（＝`IV_base`）、`absurd_price_line`（＝`50 × eps`）、`cap_components` 四項；`apply_cap_override` 一併覆寫。
3. `run_cap_calc` 對 `norm_eps <= 0` 明確輸出 `absurd_price_line=None` 並記註記。

---

### E2. HOLD_REVIEW 的 TRIM 只認 `sell_category == "估值"`，且無 1/4 上限（中高）

**位置**：`jobs/hold_jobs.py:79-84`

**實測**
- `TRIM` ＋ `sell_category="業務"` ＋ `fraction=0.5` → **被擋**（1 項錯誤）
- `TRIM` ＋ `sell_category="估值"` ＋ `fraction=0.9`（一次賣 90%）→ **零錯誤**

**影響**
1. 若模型把「股價 60 倍 + 業務轉弱」歸類為 `業務` 或 `估值＋業務` 並選 TRIM，會被退回；退回一次仍不過 ⇒ 強制 `HOLD + stop_adding`。而 `SELL_ALL` 對業務類要求 `falsified_assumption`（`:85-86`）⇒ **「價格荒謬但業務沒壞」的持股：TRIM 被類別擋、SELL_ALL 被具名假設擋 ⇒ 系統只能 HOLD**。
2. `trim_fraction` 只檢查 `0 < f < 1`，prompt 寫的「Q3c 每次 ≤1/4」（`routing/prompts/HOLD_REVIEW.md:24`）沒有程式檢查，一次賣 90% 也合格。

**建議**
1. 增加 `sell_rule` 欄位（`Q2/Q3c/P48/SWITCH/FUNDING`），權限判斷改看規則而非類別：`P48` 允許 TRIM 且 `fraction ≥ 0.5`；`Q3c` 允許 TRIM 且 `fraction ≤ 0.25`。
2. 若堅持用類別，至少允許 `估值＋業務` 做 TRIM，並把 `fraction ≤ 0.25` 寫進 `check()`。

---

### E3. `post()` 先寫 DB，`post_script()` 失敗只標 job failed（低，已緩解）

**位置**：`jobs/engine.py:318-329`

**實測**：順序確實是 `post()`（已寫 decisions／signals／registry）→ `post_script()` → 失敗只 `_fail(job)`。但**全專案覆寫 `post_script` 的 handler 有 0 個**（只有 `handlers.py:138` 的基底回傳 `[]`）。

**影響**：現在不會發生。屬介面設計債——未來有人實作 `post_script` 就會踩到「副作用已提交、job 卻 failed、下一次 `load_decisions` 又撿到那筆決策去下單」。

**建議**：**現在不要修**。在 `Handler.post_script` 的 docstring 註明「必須在 post 之前完成所有檢查」，或把介面改名 `pre_commit_check`。

---

### E4. 閘門 3 的等級上限算出來但沒人用（中高）

**位置**：`gate3_check.py:46-51`；`checks.py:54`

**實測**：`evaluate()` 回傳 `max_grade_from_C1='A'`；`trader/` 內只有 `checks.py` 的失敗預設值提到這個鍵，**沒有任何地方用它壓 `certainty_grade`**。

**影響**：`procedure/12:17` 明寫「C1 只到『5 到 10 年』→ 最高 B」。這是等級的**來源約束**，直接決定 `GRADE_CAP`（15% vs 10%）與 P1/P4 係數。現在完全靠模型自律。

**建議**：在 `write_full_result` 或 `cap_calc` 做 `grade = max_grade(grade, gate3["max_grade_from_C1"])`（A > B > C > D），並在訊號卡記「等級被閘門 3 上限壓到 B」。約 10 行。

---

### E5. 閘門 3 的價格隔離只靠自述（中低）

**位置**：`gate3_check.py:22-28`

**實測**：完全不填 `price_file_opened_at` → `gate3_pass=True`；填了一個早於 `answered_at` 的時間 → 才被抓（`'價格檔開啟時間不晚於作答完成時間（價格隔離）'`）。

**影響**：`procedure/12:15` 要求「問卷必須聲明 `price_seen_before_answering: false`」——這條有實作；但「作答完成後才開價格檔」只有自述，沒有框架證據。

**建議**：框架已有更硬的證據可用——`gate3_answers.json` 的 mtime 必須早於 `*_price.json` 被 stage 進沙箱的時間。把它寫成檢查，比自述可靠。優先級低（目前的攻擊面只有模型自己作弊）。

---

## F. Skill 撰寫與文件一致性

### F1. 同一條規則四份文件、數字互相衝突（中）

同一組數字在 `SKILL.md`、`references/rulebook.md`、`references/procedure/*.md`、`references/rule_index/*.md` 各寫一次，靠檔頭「已知被覆寫…以本檔為準」補救。殘留：

- `references/rule_index/04_hold_sell.md:163`：「荒謬級：正常化 P/E ≥**40**（可調 35–50）」，現行是 50（`procedure/07:67` 明說「40 倍找不到原文錨點」）。檔頭有覆寫聲明，內文沒改。
- 八份 `rule_index` 檔頭各貼一次同樣的覆寫聲明（`01:1`、`02:1`、`03:1`、`04:1`、`05:1`、`06:1`、`07:1`、`08:1`）。

**影響**：`rule_index` 就是「grep 查詢用」，模型檢索時會撈到 40 倍；prompt 只說「衝突時以 SKILL.md 為準」，模型未必會比對。文件債會變成行為債。

**建議**：`rule_index` 裡被覆寫的條目直接改數字（它們是索引，不是原文引用），八份檔頭收斂成一份 `references/PRECEDENCE.md`。

---

### F2. 註記與程式不符（低）

- `review_jobs.py:307`：「荒謬級線以 cap_calc 覆寫」——cap_calc 不輸出該鍵（實際來源是 `:254`）。
- `sizing.py:416`：「僅記錄，不因此下單」——沒有記錄（實測見 C3）。
- `daily.py:98`：「次日才可撮合」——實際是次次日（實測見 C2）。
- `CONTRACTS_M4.md:155-156`：「`absurd_price_line` 一律以 cap_result 覆寫」——同上。

**影響**：這類註記是審查者唯一的線索。四處不一致會讓下一個維護者（或下一個 agent）做出錯誤假設。

---

### F3. 輸出 schema 的「範例形狀」契約脆弱（低，潛在）

**位置**：`validate.py:21` `_FREE_MARKERS = ("…", "..", "：")`；`:193`

只要某個列舉欄位的**任一選項**含「…」「..」「：」，整個欄位就退化成任意字串。目前只有 `DOSSIER.dq_checks[0].result` 受影響（那是刻意的自由文字），但未來任何人把選項寫成 `"通過：說明"` 就會靜默關掉該欄位的列舉檢查。

**建議**：等 schema 真的要改時再處理（用顯式 `"自由文字"` 保留字）。**現在不要動**——這是潛在風險，不是現行缺陷。

---

### F4. 分批節奏沒有下限約束（低中）

**位置**：`sizing.py:31` `DEFAULT_TRANCHES = 3`；`sizing.py:439-517`

**現象**：`procedure/12:26` 說「剩餘 0.5×T 在 1–15 個交易日內分批」。程式沒有「兩批之間至少間隔 N 日」的約束，`plan_tranches` 每天排一批 ⇒ **3 個交易日就能把 3 批打完**，建倉第 3 天就 100% 到位。

**影響**：分批的目的是降低時點風險；3 天打完等於沒有分批。實務上仍受 `avail` 與 ADV 上限約束，但那是流動性，不是節奏。

**建議**：`build_plan` 加 `last_tranche_date`，要求間隔 ≥ 5 個交易日。約 5 行。優先級低於 C1（先讓批次別白白消耗）。

---

### F5. 「推論」標記散落，沒有集中清單（低，不建議做）

`21 曆日 ≈ 15 交易日`、`ADV 取 20 筆`、`同日收盤當參考價`、`零成長近似換算利率`（`cap_calc.py:118`）等近似散落在十幾個檔案，每處都標了「推論」。

**我的判斷：這不是問題。** 每一處就地標記，比集中清單更不容易與程式脫節；集中清單會變成第五份需要同步的文件（見 F1 的教訓）。**建議不做。**

---

## G. 測試與可觀測性缺口

現有測試（數百個）覆蓋管線、帳務、公司行動、schema 驗證，品質不差。以下斷言不存在，而它們正是上面高嚴重度問題的防線：

| 缺口 | 對應 |
|---|---|
| `cap_calc` 邊界測試：`base_g0=None`、`g10=7`、`g10<0`、`iv_cons` override、`financial=True` | B1、B2、B3、A4 |
| `auto_buy_cap < sell_review_above` 不變式（差距 ≥ 門檻） | A1 |
| FULL_B 路徑的 `absurd_price_line`／`sell_review_above`／`cap_components` 一致性 | E1 |
| `tranches_left` 在訂單逾期後的行為 | C1 |
| 冷靜期實際撮合日（應為 D+1，目前 D+2） | C2 |
| `max_grade_from_C1` 對 `certainty_grade` 的壓制 | E4 |
| 等級降級對 `target_weight` 的傳導 | C4 |

**可觀測性**：`plan_buys` 的 `qty=0` 計畫帶 `reason`（很好），但 `runs.summary` 只寫 `buys`／`tranches`，跳過原因要翻 `plans_dry_run`。建議把 `qty=0` 的計畫也寫進 summary——這是 5 行的事，卻能省掉大半「為什麼這檔沒買」的追查。

---

## H. 值得肯定的設計

1. **價格隔離做成三層**（sandbox 不含價格鍵 → dossier 文字清洗 → gate3 的 mtime 檢查），而且清洗後仍有價格行就**直接拋錯**（`dossier_prep.build_dossier_base`），不是靜默通過。同樣的「失敗關閉」習慣見於 `run_gate3`（`checks.py:79-80`）與 `_truthy`（`sizing.py:43-49`，placeholder 視為假）。
2. **公司行動「寧可擋下也不能錯入帳」**：`verify()` 三個獨立檢查（Alpaca 比例、價格比例 ≤15%、市值守恆 ≤2%），不過就 `reject` + 暫停該檔；`apply` 的檔案變更在交易內、失敗還原（`corp_actions.py:5, 431-485`）。
3. **確定性計算全部腳本化**：`cap_calc`、`gate3_check`、`review_due`、`screen_quant` 都不呼叫模型。`compare_signal_with_cap` 的「退回一次 → 仍不一致則以腳本覆寫」是不對稱的好設計（模型不能改數字，只能被改）。
4. **決策日誌 + 前視檢查**：`check_point_in_time` 拋 `LookAheadError`、`fetch_edgar.features()` 自檢 `max_filed >= asof`、gate3 證據日期須早於決策日。回測最容易死在前視，這裡有多道防線。
5. **「推論」與「原文」分離**：`rulebook.md` 每條規則都有原文錨點，`04_hold_sell.md:163` 甚至直說「40 倍找不到原文錨點」。這種誠實在投資系統裡比準確更難得。

---

## I. 建議修復順序

依「每單位工時降低的風險」排序，並標明是否改行為：

| 順位 | 項目 | 改動量 | 改行為？ |
|---|---|---|---|
| 1 | **B1** `x.get(...) or 預設` + `IV_base is None` 硬檢查 | 2–5 行 | 否（修錯） |
| 2 | **B2** `run_cap_calc` 範圍檢查 + `units_echo` | ~20 行 | 否（擋錯） |
| 3 | **E1** `absurd_price_line` 移進 `cap_calc`；`compare` 補 3 項 | ~30 行 | 否（一致性） |
| 4 | **C4** 等級單一來源（`apply_cap_override` + registry） | ~10 行 | 是（部位上限會變嚴） |
| 5 | **C1** 逾期回補 `tranches_left` | ~5 行 | 是（會多買） |
| 6 | **E4** 閘門 3 壓等級 | ~10 行 | 是（部位上限會變嚴） |
| 7 | **C3** C/D 權重超限至少留記錄 | ~3 行 | 否 |
| 8 | **E2** `sell_rule` 或 `fraction ≤ 0.25` | ~15 行 | 是（賣出更靈活） |
| 9 | **A4** 負成長 `g_term=0` + `IV0×係數` | ~5 行 | 是（估值變保守） |
| 10 | **D1** 改欄位名或加清洗樣式 | 1 行 | 否 |
| 11 | **A1** 文件正名（P1 → G8 參考價） | 文件 | 否 |
| 12 | 其餘（A2、A3、A5、A6、B3、B4、B5、C2、C5、C6、C7、D2、D3、D5、E3、E5、F1–F4） | — | 排入例行維護 |

**A1 的「改行為」版本（`min(P2′, IV_base, P1)`）刻意不列入上表**：它會大幅改變買進頻率，必須先回測，屬於「投資決策」而不是「修 bug」。這是本報告唯一需要你本人拍板的一項。

---

## J. 過度工程檢視（我自己的建議，哪些不該做）

你問「是否有過度工程化」，我把自己的建議也審了一遍。以下四項**不建議做**：

| 建議 | 為什麼不該做 |
|---|---|
| C2 新增 `eligible_date` 欄位給 `match_orders` | 為了 1 天的延遲改 schema + 撮合邏輯，成本遠大於收益。改成「承認 2 日、更正註解」1 行就夠。 |
| F5 建 `references/INFERENCES.md` 集中清單 | 會變成第五份要同步的文件（F1 的病因），而且就地標記已經足夠。 |
| F3 現在就改 `_FREE_MARKERS` 的推斷方式 | 目前只有 1 個欄位受影響且是刻意的。等 schema 真的要加含冒號的選項再改。 |
| E3 現在就重排 `post`／`post_script` | 0 個實作者，改了也沒有行為差異。留一行 docstring 警告即可。 |

另外三項是**「修一半比不修更糟」**，特別提醒：

- **A3**：不要同時「給樂觀情境新假設」又「移除 IV_opt」。選一個。兩者並存會讓 `cap_calc` 的輸出欄位與 schema 對不上。
- **A4**：`g_term_base = 0` 會讓所有低成長股的 IV_base 下降 10–15%，等於全面收緊買價。要改就一次改到底（連同 `IV0×係數` 那條），不要只改一半。
- **D5**：不要「把即時 weight 寫回 registry」。那會多一份會過期的狀態，正是 C4 的病因。讓 `review_due` 改成由框架傳入即時值。

---

## 附錄：兩支腳本

```bash
cd "/Users/zongen/Downloads/claude code/berkshire"

# 1) cap_calc 參數掃描（10 個 case）
/Users/zongen/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3 "bug report/deepseek/probe_cap_calc.py"

# 2) 24 項事實查核（需要 pandas／numpy／httpx，用系統 Python）
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 "bug report/deepseek/verify_findings.py"
```

`verify_findings.py` 的覆蓋範圍：A1–A5、B1–B3、C1–C6、D1–D5、E1–E5，以及「SKILL 是否明說『達到上限即可買』」的意圖查核。它用真實 DB 與真實函式，不是複製邏輯；所有暫存檔在 `tempfile`，不寫專案。

---

*本報告未對專案做任何修改。所有引用行號以審查當日檔案為準；判定以 `verify_findings.py` 的輸出為準。*
