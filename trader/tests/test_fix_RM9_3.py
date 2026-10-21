"""RM9-3／M7-RS6-01：初選日晚於決策日 → AnalyzeError（M7-D6-05，analyze.py），不寫任何工作。"""
import json

import pytest

from trader import db
from trader.config import load_config
from trader.paths import get_paths
from trader.pipelines.analyze import AnalyzeError, run_analyze

import test_engine as TE


def test_screen_asof_after_decision_date_rejected(tmp_path):
    cfg = load_config(TE._write_config(tmp_path), env_file=tmp_path / "none.env")
    paths = get_paths(state_dir=tmp_path / "state").ensure()
    conn = db.connect(paths.db)
    db.migrate(conn)
    sd = paths.state / "screens" / "2026-10-20"
    sd.mkdir(parents=True)
    (sd / "summary.json").write_text(json.dumps({"to_analyze": [{"ticker": "KO"}]}), encoding="utf-8")
    with pytest.raises(AnalyzeError, match="晚於決策日"):
        run_analyze(cfg, paths, conn, "2026-10-15", screen_asof="2026-10-20", clients={"sec": object()})
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
