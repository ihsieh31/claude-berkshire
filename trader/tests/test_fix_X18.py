"""X18：排程以「凍結檔可解析」判斷已凍結；被殺留下的半截凍結檔不算已凍結。"""
from __future__ import annotations

from test_scheduler import FakeRunner, _sched

from trader.data.pe_median import freeze_path

TRUNCATED = '{"median_pe": 21.3, "n": 4'   # 寫到一半被殺


def test_truncated_freeze_file_is_not_treated_as_frozen(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    s.once()
    p = freeze_path(env.paths, "2026-11")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TRUNCATED)
    run.calls.clear()
    clock.set("2026-11-04T09:00")
    done = s.once()
    assert "fetch --asof 2026-11-03 --freeze-pe" in run.calls     # 半截檔 → 仍排凍結
    assert "freeze:2026-11" in done


def test_freeze_exit_zero_but_truncated_file_counts_as_failure(tmp_path):
    env, s, clock, run = _sched(tmp_path, "2026-10-16T18:00")
    s.once()

    class Truncating(FakeRunner):
        def __call__(self, argv):
            code = super().__call__(argv)
            args = argv[3:argv.index("--state-dir")]
            if args[0] == "fetch":                                  # 子行程回 0，卻只留下半截凍結檔
                freeze_path(self.env.paths, args[2][:7]).write_text(TRUNCATED)
            return code

    run.__class__ = Truncating
    clock.set("2026-11-04T09:00")
    done = s.once()
    assert "freeze:2026-11" not in done
    assert s.load_state()["failures"]["freeze:2026-11"] == 1
