"""skill 既有自測（腳本形式）納入 pytest：以子程序執行，回傳碼 0 即通過。"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("script", [
    "berkshire-screen-merged/scripts/test_screen.py",
    "berkshire-deep-merged/scripts/test_review_due.py",
])
def test_skill_selftest(script):
    p = ROOT / script
    r = subprocess.run([sys.executable, str(p)], cwd=p.parent, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
