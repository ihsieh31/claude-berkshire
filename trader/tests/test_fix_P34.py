"""P34／M8-D10-01（D55）：hark_queue 後端改延遲匯入，框架不依賴 llm/hark_queue.py 存在。"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import trader
from trader.llm.router import build_runner
from trader.models import TierConfig

PKG = Path(trader.__file__).resolve().parent


def test_framework_imports_without_hark_queue_module(tmp_path):
    dst = tmp_path / "trader"
    shutil.copytree(PKG, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dst / "llm" / "hark_queue.py").unlink()
    code = ("import trader, trader.jobs.engine, trader.pipelines.daily, trader.cli\n"
            "from trader.llm.router import build_runner\n"
            "from trader.models import TierConfig\n"
            "assert trader.__file__.startswith(%r), trader.__file__\n"
            "r = build_runner(TierConfig(name='weak', backend='fake', model='m'))\n"
            "print('OK', type(r).__name__)\n") % str(tmp_path)
    env = dict(os.environ, PYTHONPATH=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    assert "OK FakeRunner" in p.stdout


def test_hark_queue_backend_still_builds():
    r = build_runner(TierConfig(name="weak", backend="hark_queue", model="haiku", timeout_s=1, effort="max"))
    assert type(r).__name__ == "HarkQueueRunner" and r.backend == "hark_queue"
