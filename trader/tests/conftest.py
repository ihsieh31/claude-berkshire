"""測試共用：讓 `import trader` 不需安裝；每個測試用獨立 state 目錄。"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


@pytest.fixture(autouse=True)
def _restore_environ():
    """每個測試結束後還原 os.environ：CLI 測試走 load_config() 會把本機 trader/.env 的金鑰寫進環境，
    不還原會外溢到後續「測試設定沒有金鑰」的測試（例 P19／P47 的 single_event 變成有值）。"""
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("TRADER_STATE_DIR", str(d))
    return d
