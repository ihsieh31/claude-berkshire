"""X32：P39／P35 交接小修。不連網。
1. dossier_prep 把 price_meta.screen_price_context（D71 價格類困境碼）寫進 {T}_price.json，不進價格盲 {T}.md。
2. model_routing.json 的 special_handoff 說明改為 FULL_B 看價後做步 4A。
3. （test_hark_queue 斷言改 cost_usd is None，見該檔）
4. hark/export.sh 對匯出副本執行 hark/clean_hark_estimates.py。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from trader import db
from trader.analysis import dossier_prep as dp

from test_dossier_prep import FakeSec, _facts
from test_fix_P36 import EXPORT_SH, ROOT, _seed_state

ASOF = "2026-10-15"
CODES = "T3_股價距52週高跌逾30%"


# ---------- 1. screen_price_context 只進價格檔（D71／P39 交接）

def test_screen_price_context_goes_to_price_json_only(tmp_path):
    out = tmp_path / "out"
    res = dp.build_dossier_base(FakeSec(facts=_facts()), "BRK.B", ASOF, out, price=86.1, price_date="2026-10-14",
                                rates={"error": "x"},
                                price_meta={"source": "yahoo",
                                            "screen_price_context": {"price_distress_codes": CODES}})
    pj = json.loads(res["price_json"].read_text(encoding="utf-8"))
    assert pj["screen_price_context"] == {"price_distress_codes": CODES}     # FULL_B 讀的位置
    assert pj["price_meta"]["source"] == "yahoo"
    # 價格盲檔（{T}.md 與其他前半可見檔）不得含此欄或困境碼
    for f in out.iterdir():
        if f == res["price_json"] or not f.is_file():
            continue
        txt = f.read_text(encoding="utf-8")
        assert "screen_price_context" not in txt and "T3_" not in txt, f.name


def test_no_screen_price_context_key_when_absent(tmp_path):
    res = dp.build_dossier_base(FakeSec(facts=_facts()), "BRK.B", ASOF, tmp_path / "o", price=None,
                                price_date=None, rates={"error": "x"}, price_meta={"source": "yahoo"})
    pj = json.loads(res["price_json"].read_text(encoding="utf-8"))
    assert "screen_price_context" not in pj      # FULL_B：沒有此欄 → 不補做 4A


# ---------- 2. model_routing.json（D71）

def test_model_routing_special_handoff_text_d71():
    j = json.loads((ROOT / "routing" / "model_routing.json").read_text(encoding="utf-8"))
    s = j["signals"]["screen_handoff"]["fields"]["special_handoff[]"]
    assert "FULL_B" in s and "4A" in s
    assert "FULL_A 必做步 4A" not in s


# ---------- 4. export.sh 清 hark_queue 估計值（只動副本）

@pytest.mark.skipif(not EXPORT_SH.exists() or not shutil.which("sqlite3"), reason="hark/export.sh 只在 Hark（B 機）")
def test_export_cleans_hark_estimates_on_copy_only(tmp_path, monkeypatch):
    state, out = tmp_path / "state", tmp_path / "out"
    _seed_state(state, monkeypatch)
    conn = db.connect(state / "trader.db")
    conn.execute("UPDATE llm_calls SET input_tokens=1234, output_tokens=567, cost_usd=0.0")
    conn.execute("UPDATE jobs SET input_tokens=1234, output_tokens=567, cost_usd=0.0")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    env = dict(os.environ, OUT=str(out), EXPORT_SUFFIX="_x32", PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(["bash", str(EXPORT_SH), str(state)], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=110)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "已清除 hark_queue 估計值" in r.stdout
    c = sqlite3.connect(out / "export_state" / "trader.db")
    assert c.execute("SELECT input_tokens, output_tokens, cost_usd FROM llm_calls").fetchall() == [(0, 0, None)]
    assert c.execute("SELECT input_tokens, output_tokens, cost_usd FROM jobs").fetchall() == [(0, 0, None)]
    c.close()
    src = sqlite3.connect(f"file:{state / 'trader.db'}?immutable=1", uri=True)
    assert src.execute("SELECT input_tokens, cost_usd FROM llm_calls").fetchall() == [(1234, 0.0)]  # 原 state 不動
    src.close()
