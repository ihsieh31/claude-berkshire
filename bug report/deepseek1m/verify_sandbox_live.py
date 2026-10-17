#!/usr/bin/env python3
"""驗證 P0-1：用真實 claude CLI 測「同一個沙箱根目錄裡的舊價格檔」是否讀得到。

只建立 /Users/Shared/berkshire-sandbox-zongen/verify-leak/ 下的測試檔，跑完即刪。
成本：一次 haiku、極短提示（約 $0.01）。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/zongen/Downloads/claude code/berkshire")
sys.path.insert(0, str(ROOT / "trader"))
from trader.jobs.isolation import deny_roots, settings_json   # noqa: E402

SB = Path("/Users/Shared/berkshire-sandbox-zongen/verify-leak")
SIBLING = SB / "OTIS" / "_FULL_B_1" / "dossier"
CWD = SB / "KO" / "_FULL_A_1"

PROMPT = (
    "只做一件事：用 Read 工具讀取下列兩個檔案，把讀到的內容放進 JSON。\n"
    "檔案1（相對路徑）：../../OTIS/_FULL_B_1/dossier/OTIS_price.json\n"
    "檔案2（絕對路徑）：/Users/zongen/Downloads/claude code/berkshire/trader/config.yaml\n"
    "若某個檔案讀不到（被權限拒絕或不存在），該欄位填字串 DENIED。\n"
    '只輸出一個 JSON 物件，不要說明：{"sibling": "...", "project": "..."}'
)


def main():
    if SB.exists():
        shutil.rmtree(SB)
    SIBLING.mkdir(parents=True)
    CWD.mkdir(parents=True)
    (SIBLING / "OTIS_price.json").write_text(json.dumps({"ticker": "OTIS", "price": 70.39,
                                                         "auto_buy_cap": 49.29}), encoding="utf-8")

    denies = deny_roots([], ROOT / "state")
    settings = settings_json(denies)
    print(f"deny 規則 {len(json.loads(settings)['permissions']['deny'])} 條；"
          f"沙箱根是否被 deny：{any(str(SB) in r for r in json.loads(settings)['permissions']['deny'])}")

    cmd = [
        "claude", "-p",
        "--model", "haiku",
        "--effort", "low",
        "--output-format", "json",
        "--allowedTools", "Read,Write,Glob,Grep",
        "--disallowedTools", "Bash,WebFetch,WebSearch",
        "--permission-mode", "acceptEdits",
        "--setting-sources", "project,local",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--settings", settings,
    ]
    proc = subprocess.run(cmd, input=PROMPT, cwd=str(CWD), capture_output=True, text=True, timeout=180)
    print("exit", proc.returncode)
    if proc.returncode != 0:
        print("stderr:", (proc.stderr or "")[:800])
        return
    data = json.loads(proc.stdout)
    print("result:", str(data.get("result"))[:600])
    print("usage:", json.dumps(data.get("usage") or {}, ensure_ascii=False))
    print("cost_usd:", data.get("total_cost_usd"))
    shutil.rmtree(SB, ignore_errors=True)


if __name__ == "__main__":
    main()
