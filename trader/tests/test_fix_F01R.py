"""F01R／F01R2：來源標記 origin=hark_b 的 REJECT／TOO_HARD 不進 X5，再被選入即本機重新完整分析（使用者 2026-10-18 12:14 ASK-2=A）。

匯出副本的登錄表 data 帶 origin（hark/export.sh 以 json_set 寫入 registry.data），_registry 會把 data 原樣帶進 dict。
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from trader.pipelines import screen as SP

ASOF = "2026-11-20"
NO_USER_FILE = SimpleNamespace(state=Path("/nonexistent-F01R-state"))  # 沒有 exclusions.csv，不建立任何檔


def test_hark_b_reject_not_in_x5():
    reg = {"ALL": {"status": "REJECT", "last_full_review_date": "2026-10-15", "origin": "hark_b"},
           "HPQ": {"status": "TOO_HARD", "last_full_review_date": "2026-10-15", "origin": "hark_b"},
           "BBY": {"status": "REJECT", "last_full_review_date": "2026-10-15"}}           # 本機自己的判定照舊排除
    ex = SP.build_exclusions(NO_USER_FILE, reg, ASOF).set_index("ticker")
    assert set(ex.index) == {"BBY"}
    assert ex.at["BBY", "excl_recent_reject"]


def test_hark_b_via_registry_table():
    # 模擬匯出副本：registry.data 經 json_set 帶 origin，status 欄另存
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE registry (ticker TEXT, status TEXT, data TEXT)")
    for t, st, origin in (("GL", "REJECT", "hark_b"), ("FDX", "REJECT", None)):
        d = {"last_full_review_date": "2026-10-15"}
        if origin:
            d["origin"] = origin
        conn.execute("INSERT INTO registry VALUES (?,?,?)", (t, st, json.dumps(d)))
    ex = SP.build_exclusions(NO_USER_FILE, SP._registry(conn), ASOF)
    assert list(ex["ticker"]) == ["FDX"]


def test_hark_b_reject_needs_full_again():
    # F01R2（調度者裁定 ASK-2）：他機淘汰結論不鎖死，再被選入就在本機重新完整分析
    for st in ("REJECT", "TOO_HARD"):
        need, why = SP.needs_full({"status": st, "last_full_review_date": "2026-10-15", "origin": "hark_b"}, ASOF)
        assert need and why == "他機淘汰結論，本機重新完整分析"
    # 本機自己的淘汰、他機的 WATCH／BUY／HOLDING：照舊（365 天內不重做）
    assert not SP.needs_full({"status": "REJECT", "last_full_review_date": "2026-10-15"}, ASOF)[0]
    for st in ("WATCH", "BUY", "HOLDING"):
        assert not SP.needs_full({"status": st, "last_full_review_date": "2026-10-15", "origin": "hark_b"}, ASOF)[0]


def test_hark_b_reject_needs_full_via_registry_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE registry (ticker TEXT, status TEXT, data TEXT)")
    conn.execute("INSERT INTO registry VALUES ('GL','REJECT',?)",
                 (json.dumps({"last_full_review_date": "2026-10-15", "origin": "hark_b"}),))
    assert SP.needs_full(SP._registry(conn)["GL"], ASOF)[0]
