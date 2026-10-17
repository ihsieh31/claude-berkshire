import tempfile, sys
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, "/Users/zongen/Downloads/claude code/berkshire/trader")
from trader.config import load_config
from trader.paths import Paths, PKG_ROOT
from trader import db
from trader.scheduler import Scheduler, NotBootstrapped
cfg = load_config("/Users/zongen/Downloads/claude code/berkshire/trader/config.example.yaml")
tmp = Path(tempfile.mkdtemp())
P = Paths(root=PKG_ROOT, state=tmp); P.ensure()
c = db.connect(P.db); db.migrate(c)
db.insert(c, "registry", {"ticker": "OTIS", "status": "WATCH", "data": {}, "updated_at": db.now_iso()})
calls = []
s = Scheduler(cfg, P, runner=lambda a: (calls.append(a) or 0),
              now=lambda: datetime(2026, 11, 24, 23, 0, tzinfo=timezone.utc), log=lambda m: None)
print("沒有待同意：", s.tick() is not None, "runner 呼叫數", len(calls))
db.insert(c, "approvals", {"ticker": "XYZ", "run_id": "r", "asof": "2026-11-20", "job_id": "J1", "job_type": "FULL_A",
                           "status": "pending", "created_at": db.now_iso()})
c.close()
calls.clear()
try:
    s.tick(); print("tick 照常執行")
except NotBootstrapped as e:
    print("有一筆待同意 → tick 直接丟 NotBootstrapped：", e, "；runner 呼叫數", len(calls))
