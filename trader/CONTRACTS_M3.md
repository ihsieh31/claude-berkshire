# M3 介面契約（深度分析：DOSSIER → FULL_A → FULL_B → RED_TEAM）

規則來源：`berkshire-deep-merged/SKILL.md`、`routing/model_routing.json`、`routing/prompts/*.md`、`routing/output_schemas.json`。
skill 檔案唯讀；skill 的計算一律匯入或子程序呼叫，不複製公式。

## 0. 共通

- 代號標準形：大寫、點號（`BRK.B`）。檔名中的代號照標準形（`BRK.B.md`）。
- `ticker_dir`＝`state/work/{run_id}/{ticker}`：同一檔各工作的正式輸出區（框架寫、框架讀）。
- 時點：只用申報日 **<** asof 的 SEC 文件；價格 ≤ asof。
- 單檔失敗不影響其他檔；錯誤以例外拋出，由呼叫端（handler／引擎）記錄。
- 不得寫死 email、金鑰。SEC 存取一律經 `trader.data.sec.SecClient`。

## 1. `trader/analysis/dossier_prep.py`（代理 1）

```python
PRICE_TEXT_PATTERNS: list[re.Pattern]   # 見 scrub_price_text

def build_dossier_base(sec, ticker: str, asof: str, outdir: Path,
                       price: float | None, price_date: str | None,
                       rates: dict | None) -> dict
```
- 以 `sec.cik_for(ticker)` 取 CIK；用 `sec.companyfacts(cik)` 與前身合併（`sec._with_predecessor_facts(cik10, facts)` 回傳 (facts, used, failed)）。
- 在 `outdir/_cache/` 寫入 `company_tickers.json`（只需 `{"0": {"cik_str": int, "ticker": 代號（點號與連字號兩種都寫一筆）, "title": ""}}`）與 `CIK{cik10}.json`（合併後 facts），
  再以子程序執行 skill `berkshire-deep-merged/scripts/build_dossier.py --ticker T --asof D --outdir outdir --cache outdir/_cache`
  ＋ `--price/--price-date`（有才給）＋ `--t30/--t30-date/--t30-20y-med`（rates 的 `t30`、`t30_date`、`t30_20y_median`，有才給）。
  子程序環境要帶 `SEC_USER_AGENT`（從 `sec.user_agent`，若無此屬性就從 os.environ），快取命中時不連網。
- 產出 `outdir/{T}.md`、`outdir/{T}_price.json`；結束後刪除 `outdir/_cache/`。
- 檢查 `{T}.md` 經 `scrub_price_text` 後無變化（腳本本來就不含價格）；有變化 → 拋 `DossierError`。
- 子程序失敗（含「決策日前沒有可用的年度淨利」）→ 拋 `DossierError(訊息)`。
- 回傳 `{"md": Path, "price_json": Path, "predecessors_used": [...], "notes": [...]}`。

```python
def fetch_sec_texts(sec, cik: str, asof: str, outdir: Path, max_8k: int = 12) -> list[dict]
```
- 取申報日 < asof 的：最新一份 10-K（或 10-K/A 若較新且為完整修正——一律取最新 10-K 類）、其後所有 10-Q（最多 3 份）、最新一份 DEF 14A、最新 10-K 申報日之後的 8-K（最多 max_8k 份，取最新者）。
- 每份 `sec.filing_text(url)` → `scrub_price_text` → 寫 `outdir/{form去斜線}_{filingDate}_{accession後6碼}.txt`；檔首加一行 `# {form} {filingDate} {url}`。
- 回傳 `[{"file": 檔名, "form", "filingDate", "url", "chars", "removed_lines"}]`，依申報日排序。

```python
def scrub_price_text(text: str) -> tuple[str, int]
```
- 逐行（以換行切）刪除含價格資訊的行，回傳 (新文字, 刪除行數)。至少涵蓋（不分大小寫）：
  `aggregate market value`、`closing (sale )?price`、`market price of (our|the) (common )?stock`、`stock price`、`share price`、
  `price per share`（但 `exercise price`、`purchase price per share` of acquisitions 也刪，寧可多刪）、`high and low (sales )?prices`、
  `price[- ]to[- ]earnings`、`P/E ratio`、`market capitali[sz]ation`、`price target`、`analyst rating`、
  `stock performance graph`、`cumulative total (shareholder|stockholder) return`、中文 `股價|市值|本益比|目標價`。
- 不刪財報數字行（不得因為含 `$` 就刪）。

```python
def split_10k_items(text: str) -> list[tuple[str, str]]
```
- 以行首 `Item 1.`、`Item 1A.`、`Item 1B.`、`Item 1C.`、`Item 2.` … `Item 16.`（大小寫不拘、允許 `ITEM 7` 無句點、允許 `Item 7 –`）切段，回傳 [(項目代號如 "1A", 段落文字)]，依出現順序。
- 目錄（Table of Contents）中的重複 Item 標題：同一代號出現多次時取**內容最長**的一段。
- 切不出來 → 回傳 `[("ALL", text)]`。

`class DossierError(Exception)`。

## 2. `trader/analysis/checks.py`（代理 2）

```python
def run_gate3(answers_path: Path) -> dict
```
- 匯入 skill `berkshire-deep-merged/scripts/gate3_check.py` 的 `evaluate(ans)`（sys.path 加入 scripts 目錄），回傳其結果 dict。檔案不存在或 JSON 壞 → `{"gate3_pass": False, "failures": ["…"], ...}`（不拋）。

```python
def run_cap_calc(cap_input_path: Path) -> dict
```
- 匯入 skill `cap_calc.calc(x)`，回傳結果（數值以 cap_calc main 的 rnd 方式四捨五入到 4 位）。輸入壞 → 拋 `CheckError`。

```python
def compare_signal_with_cap(signal_card: dict, cap: dict, rel_tol: float = 0.005) -> list[str]
```
- 比對訊號卡的 `auto_buy_cap` 與 IV（訊號卡欄名依 `assets/signal_card_schema.json`；cap 結果欄名依 cap_calc 輸出），兩邊皆 None 視為一致；一邊 None 一邊有值 → 不一致；相對差 > rel_tol → 不一致。回傳不一致說明清單（空＝一致）。

```python
def apply_cap_override(signal_card: dict, cap: dict) -> dict
```
- 回傳新 dict：以 cap_calc 的數字覆寫訊號卡的上限與 IV 欄位，並在訊號卡加 `"framework_notes": [...]` 記錄覆寫前後。

```python
def cap_signal_by_gates(signal: str, gate3: dict | None, screen_decision: str | None) -> tuple[str, list[str]]
```
- 自動買進三閘門（SKILL §0A）中程式可判的兩個：gate3 未過（或缺）→ BUY 降為 WATCH；screen_decision 為刪除類（以 "刪除" 開頭或含 "DELETE"）→ BUY 降為 WATCH。回傳 (新訊號, 理由清單)。非 BUY 原樣回傳。

`class CheckError(Exception)`。

## 3. `trader/analysis/writeback.py`（代理 2）

讀 skill 的 `assets/signal_card_schema.json`、`registry_schema.json`、`decision_log_schema.json` 確認欄位名。

```python
def next_decision_id(conn, ticker: str, asof: str) -> str
```
- 格式 `{TICKER}-{YYYYMMDD}-{n}`，n 從 1 起，依 decisions 表同代號同日已有數量 +1。

```python
def write_full_result(conn, paths, ticker: str, asof: str, run_id: str, tdir: Path,
                      final_signal: str | None = None, notes: list[str] | None = None) -> str
```
- FULL_B 完成後（RED_TEAM 若有也完成）呼叫。讀 `tdir/signal_card.json`、`registry_entry.json`、`decision_log.json`、`thesis.md`。
- `final_signal` 給定時覆寫訊號卡與日誌的 signal（RED_TEAM 降級、閘門降級），notes 併入日誌（欄位不存在就放 `framework_notes`）。
- 寫入：
  - `decisions`：decision_id＝`next_decision_id`（覆寫模型寫的 id），signal、reason_code、data＝日誌 JSON；
  - `signals`：同 decision_id，data＝訊號卡 JSON；
  - `registry`：upsert（ticker 主鍵），status 依訊號（BUY→WATCH〔尚未成交；成交後由帳本改 HOLDING〕、WATCH→WATCH、NO_ACTION→reason_code〔MISSING_DATA／TOO_HARD／REJECT〕、RED_LIGHT→REJECT），data＝registry_entry（補 `last_review_date`＝`last_full_review_date`＝asof、`thesis_file`）；
  - 論點檔：`paths.theses / f"{ticker}_{asof}.md"`；**已存在則不覆寫**（凍結），改寫 `{ticker}_{asof}_addendum_{n}.md`。
- 回傳 decision_id。必要檔缺 → 拋 `WritebackError`。

```python
def write_stop_result(conn, paths, ticker: str, asof: str, run_id: str, phase_a: dict,
                      notes: list[str] | None = None) -> str
```
- FULL_A `stopped=true`（或 DOSSIER／FULL_A 無法完成）時，框架依 phaseA 的停止碼寫：訊號 NO_ACTION、reason_code＝phase_a["reason_code"]（缺→MISSING_DATA）、stopped_at_gate、certainty_grade＝null；
  依三個 schema 的欄位產生最小訊號卡／日誌／登錄表列（只填得出的欄位，其餘 null），寫入 decisions、signals、registry（last_full_review_date＝asof，status＝reason_code）。回傳 decision_id。

`class WritebackError(Exception)`。

## 4. 主代理負責（不要動）

- `trader/llm/claude_code.py`：沙箱外讀寫封鎖（`--settings` deny 規則）。
- `trader/config.py`、`trader/jobs/*.py`：沙箱根目錄、提示佔位符、各工作的沙箱佈局與前後置串接。
- `trader/pipelines/analyze.py`、CLI `trader analyze`。
