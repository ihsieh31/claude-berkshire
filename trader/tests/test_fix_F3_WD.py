"""F3-WD：round3 G6-docs-handoff／G8-small-docs／G9-web（V3-4）。程式項修前失敗→修後通過；文件項為文字檢查。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_approvals import _env

TESTS = Path(__file__).resolve().parent
TRADER = TESTS.parent
ROOT = TRADER.parent
PORT = 8787
H = {"host": f"127.0.0.1:{PORT}"}


def _doc(rel: str) -> str:
    p = ROOT / rel
    if not p.is_file():
        pytest.skip(f"{rel} 不在此環境")
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- G9-web

def test_summary_has_bench(tmp_path, monkeypatch):
    """A3-M8C2-10：/api/summary 帶設定的基準代號；沒給設定檔 → SPY。"""
    from trader import config as C
    from trader.dashboard.server import create_app
    env = _env(tmp_path)
    monkeypatch.setattr(C, "load_config", lambda *a, **k: SimpleNamespace(account=SimpleNamespace(benchmark="voo")))
    app = create_app(env.paths, PORT, config_path=tmp_path / "config.yaml", spawn=lambda: None)
    r = TestClient(app, base_url=f"http://127.0.0.1:{PORT}").get("/api/summary", headers=H)
    assert r.status_code == 200 and r.json()["bench"] == "VOO"
    app2 = create_app(env.paths, PORT, spawn=lambda: None)
    assert TestClient(app2, base_url=f"http://127.0.0.1:{PORT}").get("/api/summary", headers=H).json()["bench"] == "SPY"


def _js() -> str:
    return (TRADER / "trader" / "web" / "static" / "app.js").read_text(encoding="utf-8")


def test_app_js_no_hardcoded_spy():
    """A3-M8C2-10：app.js 的基準名稱取 s.bench，SPY 只當後備值。"""
    js = _js()
    assert "s.bench" in js
    for m in re.finditer(r"SPY", js):
        assert js[m.start() - 1] == '"' and js[m.start() - 4:m.start() - 1] == "|| ", js[m.start() - 40:m.end() + 5]


def test_app_js_analyses_limit_and_export_labels():
    """A3-M8C2-11：分析頁滿 200 筆標『最近 200 筆』；A3-M8C2-13：匯出按鈕標明不帶篩選（全部）。"""
    js = _js()
    assert "rows.length >= 200" in js and "最近 200 筆決策（完整見匯出）" in js
    for href in ('href="/api/export/${state.tab}.csv">', 'href="/api/export/analyses.csv">'):
        i = js.index(href) + len(href)
        assert js[i:i + 10] == "匯出 CSV（全部）", js[i:i + 12]


def test_install_widget_fail_still_loads_server(tmp_path, monkeypatch, capsys):
    """A3-M8C2-14：小儀表板編譯失敗時，伺服器 plist 仍寫入並 bootstrap，install 不拋例外。"""
    from trader.dashboard import launchd as L
    env = _env(tmp_path)
    calls = []
    monkeypatch.setattr(L, "AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(L, "_launchctl", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))

    def boom():
        raise RuntimeError("小儀表板編譯失敗：swiftc not found")
    monkeypatch.setattr(L, "build_widget", boom)
    done = L.install(env.paths, PORT, None, widget=True)
    assert done == [L.LABEL_SERVER]
    assert (tmp_path / "LaunchAgents" / f"{L.LABEL_SERVER}.plist").is_file()
    assert any(c[0] == "bootstrap" for c in calls)
    assert not (tmp_path / "LaunchAgents" / f"{L.LABEL_WIDGET}.plist").exists()
    err = capsys.readouterr().err
    assert "小儀表板編譯失敗" in err and "--no-widget" in err


def test_install_widget_ok_loads_both(tmp_path, monkeypatch):
    from trader.dashboard import launchd as L
    env = _env(tmp_path)
    monkeypatch.setattr(L, "AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(L, "_launchctl", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(L, "build_widget", lambda: L.APP_BIN)
    assert L.install(env.paths, PORT, None, widget=True) == [L.LABEL_SERVER, L.LABEL_WIDGET]


# ---------------------------------------------------------------- G8-small-docs

def test_charts_docstring_says_no_cache():
    """A3-M8C-04"""
    first = (TRADER / "trader" / "dashboard" / "charts.py").read_text(encoding="utf-8").splitlines()[0]
    assert "記憶體快取" not in first and "不快取" in first


def test_rules_fixes_test_renamed():
    """A3-M8B2-02（M8-D10-01）"""
    assert (TESTS / "test_rules_fixes_20261018.py").is_file()
    assert not (TESTS / "test_hark_fixes_20261018.py").exists()


def test_contracts_db_lists_v2_v3_and_backup():
    """A3-M8B2-03"""
    s = (TRADER / "CONTRACTS.md").read_text(encoding="utf-8")
    sec = s[s.index("## trader/db.py"):s.index("## trader/cli.py")]
    assert "def backup(db_path: Path, note: str | None = None, keep: int = BACKUP_KEEP) -> Path" in sec
    assert "版本 2" in sec and "approvals(" in sec and "版本 3" in sec and "provenance" in sec


# ---------------------------------------------------------------- G6-docs-handoff

def test_sellall_pause_docs_consistent():
    """A3-M8C2-04：人工暫停期間 SELL_ALL 照建委託、解除後撮合，文件不得寫成被拒／要補下。"""
    from trader.ledger import book
    assert "SELL_ALL 全清照建" in book.pause.__doc__ and "不要另補賣單" in book.pause.__doc__
    m5 = (TRADER / "CONTRACTS_M5.md").read_text(encoding="utf-8")
    assert "買賣（含 SELL_ALL）都擋" not in m5 and "連 SELL_ALL 也擋。" not in m5
    h = _doc("handoff.md")
    assert "SELL_ALL 全清照建委託、撮合時擋" in h and "不要另補賣單" in h


def test_handoff_current_state_only():
    """A3-M8B2-08 後續（2026-10-21）：handoff.md 只寫現況，整份覆寫；歷史移到 docs/archive/，不在本檔追加流水帳。"""
    h = _doc("handoff.md")
    for sec in ("## 現況", "## 禁止事項", "## 下一步：r3", "## 兩台機器", "## 仍有效的使用者決策",
                "## 人工操作注意"):
        assert sec in h, sec
    assert "docs/archive/" in h
    for old in ("## 0-歷史", "修復總覽", "§0-歷史"):   # 歷史段不得留在現況檔
        assert old not in h, old


def test_switch_doc_restore_mentions_cap_inputs_theses():
    """A3-M8E-02"""
    s = _doc("換機匯出與封存程序.md")
    sec4 = s[s.index("## 4. 還原"):s.index("## 4.1")]
    assert "cap_inputs" in sec4 and "theses" in sec4 and "decisions void" in sec4


def test_hark_doc_token_cost_wording():
    """A3-M8B2-10：無 meta.json 時 token 0、cost_usd NULL（hark_queue M5-D5-05）。"""
    s = _doc("HARK_執行說明.md")
    row = next(l for l in s.splitlines() if l.startswith("| 模型後端"))
    assert "字數估算" not in row.replace("不以字數估算", "") and "`cost_usd`＝0" not in row
    assert "meta.json" in row and "NULL" in row
