#!/usr/bin/env python3
"""第三組驗證：沙箱價格隔離的實際可讀範圍、閘門 3 的時點外洩、以及估值公式有無獨立數值防線。

唯讀：本檔只讀專案原始碼與 state/ 沙箱，不寫入專案任何檔案；暫存一律落在本目錄或系统 tmp。
用法：python3 verify_isolation.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent.parent / "trader"          # berkshire/trader
REPO = HERE.parent.parent                     # berkshire/
SANDBOX_ROOT = Path("/Users/Shared/berkshire-sandbox-zongen")
sys.path.insert(0, str(PKG))

from trader.jobs import isolation as ISO      # noqa: E402
from trader.jobs.sandbox import assert_price_free, PriceLeakError  # noqa: E402
from trader.jobs import handlers  # noqa: E402,F401  先載入，避開 deep↔handlers 循環匯入
from trader.jobs.review_jobs import _gate3_status  # noqa: E402
from trader import db  # noqa: E402


def head(t: str) -> None:
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


# ---------------------------------------------------------------- W1 沙箱根不在封鎖清單
head("W1  deny_roots 是否封住沙箱根（模型可用絕對路徑讀到兄弟沙箱的價格檔？）")
denies = ISO.deny_roots()
price_file = SANDBOX_ROOT / "analyze-2026-10-16/OTIS/_FULL_B_1/dossier/OTIS_price.json"
print(f"封鎖目錄 {len(denies)} 個：")
for d in denies:
    print("   ", d)
covered = [d for d in denies if price_file == d or d in price_file.parents]
print(f"\n價格檔：{price_file}")
print(f"存在：{price_file.is_file()}　被任一封鎖目錄覆蓋：{bool(covered)}　→ {covered}")
rules = json.loads(ISO.settings_json(denies))["permissions"]["deny"]
print(f"\n--settings 規則筆數：{len(rules)}；前 3 條：")
for r in rules[:3]:
    print("   ", r)
print("\n【觀察 1】沙箱根本身不在封鎖清單（isolation.check_sandbox 反而要求它在清單之外）。")
print("【觀察 2】規則字串是 `Read(//Users/...` 雙斜線：settings_json 用 f\"{{t}}(/{d.as_posix()}/**)\",")
print("          而 d 已是絕對路徑。tests/test_isolation.py:32 直接把雙斜線寫成期望值，")
print("          所以這串字從未被真正的 claude 匹配器驗證過（該測試明寫『不呼叫 claude』）。")


# ---------------------------------------------------------------- W2 兄弟沙箱的價格檔可直讀
head("W2  從隔離沙箱的相對路徑，直接打開同 run 另一工作的現價檔")
fa = SANDBOX_ROOT / "analyze-2026-10-16/OTIS/_FULL_A_1"
rel = "../_FULL_B_1/dossier/OTIS_price.json"
if fa.is_dir():
    own = sorted(p.relative_to(fa).as_posix() for p in fa.rglob("*") if p.name.endswith("_price.json"))
    print(f"  FULL_A 沙箱內的價格檔（stage_inputs 的保證）：{own or '無'}")
    p = (fa / rel).resolve()
    txt = p.read_text(encoding="utf-8")           # 等效於模型在沙箱裡用 Read 打開這個路徑
    d = json.loads(txt)
    print(f"\n  以 cwd={fa} 相對路徑 {rel} 打開 → 成功")
    print(f"  內容：price={d.get('price')} price_date={d.get('price_date')} t30={d.get('t30')}")
    print(f"       該檔自註：{d.get('note')}")
    sib = sorted(q for q in SANDBOX_ROOT.rglob("*_price.json") if q.is_file())
    print(f"\n  同一沙箱根下的價格檔共 {len(sib)} 個：")
    for q in sib:
        print("   ", q)
    print("\n【後果】價格隔離由 stage_inputs（只複製白名單檔）+ assert_price_free（只掃自己這一格）")
    print("        構成；而 claude 的 Read/Glob/Grep 走絕對或 .. 相對路徑，不受這兩者管。")
    print("        能擋住它的只有 --settings 的 deny 清單，而沙箱根刻意不在清單內")
    print("        （isolation.check_sandbox 要求沙箱不得位於任何封鎖目錄之下）。")
    print("        → DOSSIER／TRIAGE／THESIS／FULL_A 的『看不到價格』在實作上只剩提示詞約束。")
else:
    print(f"（找不到 {fa}，跳過）")



# ---------------------------------------------------------------- W3 估值公式無獨立數值防線
head("W3  改動 cap_calc 的取數方向，花錢的數字立刻變，但框架內無任何獨立期望值")
calc = REPO / "berkshire-deep-merged" / "scripts" / "cap_calc.py"
example = REPO / "berkshire-deep-merged" / "assets" / "cap_calc_example_input.json"
src = calc.read_text(encoding="utf-8")
MUTANTS = [
    ("cap 取 min → max（上限改挑最寬）", "min(capc)", "max(capc)"),
    ("auto_buy_cap 取 min → max", "abc = (min(cap, iv_base)", "abc = (max(cap, iv_base)"),
    ("歷史兩期時的 g0c 折減 0.75 → 1.0", "min(hist) * (0.75 if len(hist) == 2 else 0.6)", "min(hist) * 1.0"),
    ("貼現率下限 10% 拿掉", "max(0.10,", "max(0.00,"),
]
KEYS = ("cap", "auto_buy_cap", "IV_base", "IV_cons", "g0_conservative")


def run(script: Path) -> dict:
    r = subprocess.run([sys.executable, str(script), str(example)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[:200])
    return json.loads(r.stdout)


base = run(calc)
print(f"skill 自己附的範例輸入，原始輸出：")
for k in KEYS:
    print(f"   {k:18s} {base.get(k)!s:>12}")
print(f"   → 範例裡 auto_buy_cap == IV_base == {base.get('auto_buy_cap')}（實例即等號成立）\n")
with tempfile.TemporaryDirectory(dir=HERE) as td:
    for i, (label, old, new) in enumerate(MUTANTS):
        if old not in src:
            print(f"  [{i}] {label}：找不完整字串 {old!r}，跳過")
            continue
        mut = Path(td) / f"cap_calc_m{i}.py"
        mut.write_text(src.replace(old, new), encoding="utf-8")
        try:
            got = run(mut)
        except Exception as e:
            print(f"  [{i}] {label}：變體無法執行（{e}）")
            continue
        ch = {k: (base.get(k), got.get(k)) for k in KEYS if base.get(k) != got.get(k)}
        print(f"  [{i}] {label}")
        print(f"       改變的欄位：{', '.join(f'{k} {a}→{b}' for k, (a, b) in ch.items()) or '無'}")
print("\n【觀察】tests 端對 auto_buy_cap 的斷言全部拿同一份 cap_calc 的輸出當期望值：")
print("        test_checks.py:113（比對 cap_calc 自己 CLI 的 stdout）、:225-226（比對同一算式產物）、")
print("        test_review_jobs.py:101/243（_auto_cap() 就是 run_cap_calc()）。")
print("        上述每個變體都讓這三個斷言繼續成立 → 花錢的那個數字沒有 golden value。")


# ---------------------------------------------------------------- W4 產出端從不掃價格
head("W4  價格隔離只在『送進沙箱前』掃一次；模型寫出來的東西從不回首")
fa = SANDBOX_ROOT / "analyze-2026-10-16/OTIS/_FULL_A_1"
if fa.is_dir():
    try:
        assert_price_free(fa)
        print("  assert_price_free(FULL_A 沙箱) → 通過")
    except PriceLeakError as e:
        print(f"  事後對同一沙箱跑 assert_price_free → PriceLeakError：{e}")
        print("  觸發者是模型自己寫的 gate3_answers.json 裡兩個布林／空字串欄：")
        print("    price_seen_before_answering=false、price_file_opened_at=\"\"（根本沒有價格值）")
        print("  → 反證：這支掃描從沒被接在模型之後跑；接上去 FULL_A 每次必死。")
    print("  files_written（claude_code.py:107 已算出沙箱內新增或修改的檔名）在 trader/ 全專案")
    print("  只有赋值與欄位宣告，沒有任何讀取端 → 輸出端的價格稽核並不存在。")




# ---------------------------------------------------------------- W5 閘門 3 的時點外洩
head("W5  _gate3_status 無 asof 上界：回填舊交易日會讀到『未來』的 gate3 決定")
tmpdb = HERE / "_w4_gate3.sqlite"
if tmpdb.exists():
    tmpdb.unlink()
conn = sqlite3.connect(str(tmpdb))
conn.row_factory = sqlite3.Row
db.migrate(conn)
ASOF = "2026-10-05"          # 假設這是 scheduler 回填的舊交易日
for dd, passed, tag in [("2026-10-16", True, "未來才做的 gate3 問卷（通過）"),
                        ("2026-09-01", False, "asof 之前的舊問卷（不通過）")]:
    did = f"KO-{dd.replace('-', '')}-1"
    conn.execute("INSERT INTO decisions(decision_id, ticker, decision_date, run_id, signal, reason_code, data, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (did, "KO", dd, f"r-{dd}", "WATCH", "TOO_HARD",
                  json.dumps({"gate3_pass": passed}, ensure_ascii=False), f"{dd}T12:00:00Z"))
conn.commit()
got = _gate3_status(conn, "KO", ASOF)
print(f"  asof = {ASOF}")
print(f"  僅有的 asof 之前紀錄 = 2026-09-01 gate3_pass=False")
print(f"  asof 之後的紀錄      = 2026-10-16 gate3_pass=True")
print(f"  _gate3_status() 回傳 = {got!r}   ← 應為『不通過』")
print("  來源：review_jobs.py:94-97 的 SELECT 沒有 `decision_date <= ?`；")
print("        比對 sizing.py:221/239 與 validation.py:35 都有上界，只有這條沒有。")
conn.close()
tmpdb.unlink()

head("完成")
print("本腳本未寫入專案任何檔案（僅在本目錄與系統暫存建立暫時檔，且已清除）。")
