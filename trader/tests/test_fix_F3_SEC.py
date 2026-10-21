"""F3-SEC：V3-1 FG-M1-sec-fetch（A3-M1C-02、A3-M1E-01）、FG-M2-review_pipeline（A3-M2E-01、A3-M2E-03）。
V3-2 G5-sec-fetch（A3-M4C-01／02、A3-M4E-02）已由 F3-C 修畢，測試在 test_fix_F3_C.py。不連網。"""
from __future__ import annotations

import json
import sys

import pandas as pd
import pytest

import test_fix_P37 as P37
import test_review_pipeline as TRP
from test_fix_P37 import fenv, senv  # noqa: F401 - pytest 夾具
from test_fix_RM2_12 import QUIET, TEN_K, _clients
from test_review_pipeline import env as pipe_env  # noqa: F401 - pytest 夾具
from test_review_pipeline import no_engine  # noqa: F401 - pytest 夾具
from test_sec import CIK, FACTS, SUBS, TICKERS, UA, age_file, make_http
from trader.data import inputs as I
from trader.data import pe_median as P
from trader.data import sec
from trader.data.http import SecBlocked
from trader.pipelines import review as RP
from trader.pipelines import screen as SP
from trader.pipelines.review import _sanitize_entry, run_review


# ============================================================ A3-M1C-02：前視自檢失敗必須中止整批
def test_m1c02_lookahead_aborts_host_features(tmp_path, monkeypatch):
    fe = sec._load_fetch_edgar()
    assert issubclass(fe.LookaheadError, RuntimeError)

    def boom(*a, **k):
        raise fe.LookaheadError("前視：BRK.B 用到 2026-10-16 申報資料，決策日 2026-10-16")

    monkeypatch.setattr(fe, "features", boom)
    routes = {"https://www.sec.gov/files/company_tickers.json": TICKERS,
              f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json": FACTS,
              f"https://data.sec.gov/submissions/CIK{CIK}.json": SUBS}
    c = sec.SecClient(UA, tmp_path, http=make_http(routes, []))
    with pytest.raises(fe.LookaheadError):
        c.features(["BRK-B"], asof="2026-10-16")


def test_m1c02_other_errors_note_type_and_message(tmp_path, monkeypatch):
    fe = sec._load_fetch_edgar()

    def boom(*a, **k):
        raise ValueError("壞欄位 X" + "y" * 300)

    monkeypatch.setattr(fe, "features", boom)
    routes = {"https://www.sec.gov/files/company_tickers.json": TICKERS,
              f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json": FACTS,
              f"https://data.sec.gov/submissions/CIK{CIK}.json": SUBS}
    c = sec.SecClient(UA, tmp_path, http=make_http(routes, []))
    note = c.features(["BRK-B"], asof="2026-10-16").loc[0, "data_notes"]
    assert note.startswith("抓取失敗：ValueError: 壞欄位 X") and len(note) <= len("抓取失敗：ValueError: ") + 120


def test_m1c02_lookahead_aborts_skill_main(tmp_path, monkeypatch):
    fe = sec._load_fetch_edgar()

    def fake_cached(path, url, asof=None, notes=None):
        if "company_tickers" in url:
            return {"0": {"cik_str": 1, "ticker": "AAA", "title": "A"}}
        return {"facts": {}, "sic": ""}

    def boom(*a, **k):
        raise fe.LookaheadError("前視")

    monkeypatch.setattr(fe, "cached", fake_cached)
    monkeypatch.setattr(fe, "features", boom)
    (tmp_path / "t.txt").write_text("AAA\n", encoding="utf-8")
    (tmp_path / "pre.json").write_text('{"map": {}}', encoding="utf-8")
    out = tmp_path / "f.csv"
    monkeypatch.setattr(sys, "argv", ["fetch_edgar.py", "--tickers", str(tmp_path / "t.txt"), "--out", str(out),
                                      "--cache", str(tmp_path / "c"), "--asof", "2026-10-16", "--ua", UA,
                                      "--predecessors", str(tmp_path / "pre.json")])
    with pytest.raises(fe.LookaheadError):
        fe.main()
    assert not out.exists()   # 不寫半份輸出


# ============================================================ A3-M1E-01：SEC 封鎖＋舊快取不得照常凍結／產出名單
class _StaleSec(P37._Sec):
    """features 期間 n 檔 companyfacts 改用舊快取（同 SecClient._cached_json 記 stale）。"""

    def __init__(self, n_stale, blocked=None):
        super().__init__()
        self.stale = []
        self.warnings = []
        self.n = n_stale
        self.http = type("H", (), {"blocked": blocked})()

    def features(self, tickers, asof):
        for i, _ in enumerate(tickers[:self.n]):
            self.stale += [f"CIK{i + 1:010d}.json", f"sub_CIK{i + 1:010d}.json"]
        return super().features(tickers, asof)


def _fetch_stale(tmp_path, paths, conn, sec_client):
    return I.fetch_inputs(P37._cfg(tmp_path), paths, conn, P37.ASOF, tmp_path / "o", freeze_pe=True,
                          clients={"sec": sec_client, "alpaca": P37._Alpaca(), "fred": P37._Fred()})


def test_m1e01_stale_cache_ratio_blocks_pe_freeze(tmp_path, fenv):
    paths, conn = fenv
    s = _fetch_stale(tmp_path, paths, conn, _StaleSec(10))    # 10／40＝25% 舊快取
    assert s["sec_stale_ratio"] == 0.25
    assert isinstance(s["pe_median"], str) and "舊快取" in s["pe_median"]
    assert P._read_frozen(P.freeze_path(paths, "2026-10")) is None


def test_m1e01_sec_blocked_blocks_pe_freeze(tmp_path, fenv):
    paths, conn = fenv
    s = _fetch_stale(tmp_path, paths, conn, _StaleSec(0, blocked="SEC 封鎖"))
    assert s["sec_blocked"] is True and "略過" in s["pe_median"]
    assert P._read_frozen(P.freeze_path(paths, "2026-10")) is None


def test_m1e01_small_stale_ratio_still_freezes(tmp_path, fenv):
    paths, conn = fenv
    s = _fetch_stale(tmp_path, paths, conn, _StaleSec(2))     # 5% ≤ 10%：照常凍結
    assert s["sec_stale_ratio"] == 0.05
    assert P._read_frozen(P.freeze_path(paths, "2026-10"))["pe_norm_median"] == 26.9


def test_m1e01_real_client_blocked_with_stale_cache_counts(tmp_path, monkeypatch):
    """SecClient 封鎖中、全部有舊快取：features 不拋例外（舊快取備援），但 sec_stale_ratio＝1、sec_blocked。"""
    cache = tmp_path / "edgar"
    cache.mkdir()
    for name, body in (("company_tickers.json", TICKERS), (f"CIK{CIK}.json", FACTS), (f"sub_CIK{CIK}.json", SUBS)):
        (cache / name).write_text(json.dumps(body), encoding="utf-8")
        age_file(cache / name, 24 * 40)
    http = make_http({}, [])
    http.blocked = "SEC 封鎖（測試）"
    c = sec.SecClient(UA, cache, http=http)
    monkeypatch.setattr(I, "fill_missing_shares", lambda feats, *a, **k: (feats, []))
    paths = P37.get_paths(tmp_path / "state").ensure()
    conn = P37.db.connect(paths.db); P37.db.migrate(conn)
    s = I.fetch_inputs(P37._cfg(tmp_path), paths, conn, P37.ASOF, tmp_path / "o", tickers=["BRK.B"],
                       clients={"sec": c, "alpaca": P37._Alpaca(), "fred": P37._Fred()})
    conn.close()
    assert s["sec_stale_ratio"] == 1.0 and s["sec_blocked"] is True


def test_m1e01_secblocked_without_cache_still_raises(tmp_path):
    """沒有舊快取時 SEC 封鎖照舊往上拋（不得吞掉）。"""
    http = make_http({}, [])
    http.blocked = "SEC 封鎖（測試）"
    c = sec.SecClient(UA, tmp_path, http=http)
    with pytest.raises(SecBlocked):
        c.features(["BRK-B"], asof="2026-10-16")


def test_m1e01_screen_fails_when_sec_blocked_with_stale_cache(tmp_path, senv, monkeypatch):
    paths, conn = senv

    def fake_fetch(cfg, paths, conn, asof, inputs_dir, **k):
        P37._write_inputs(inputs_dir)
        return {"universe_source": "download", "sec_stale_ratio": 0.25, "sec_stale_cache": ["CIK0000000001.json"]}

    monkeypatch.setattr(SP, "fetch_inputs", fake_fetch)
    s = SP.run_screen(None, paths, conn, P37.ASOF)
    assert "舊快取" in s.get("error", "") and "shortlist" not in s
    assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "failed"


# ============================================================ A3-M2E-01：登錄表日期欄正規化
def test_m2e01_sanitize_normalizes_dates():
    notes: list[str] = []
    d = _sanitize_entry({"ticker": "X", "last_review_date": "20261001",
                         "last_full_review_date": "2026-10-01T08:00:00", "first_buy_date": "2026-01-02",
                         "kill_criteria_due": [{"text": "a", "due": "20261101"}, {"text": "b", "due": None}]}, notes)
    assert d["last_review_date"] == "2026-10-01"
    assert d["last_full_review_date"] == "2026-10-01"
    assert d["first_buy_date"] == "2026-01-02"
    assert d["kill_criteria_due"] == [{"text": "a", "due": "2026-11-01"}, {"text": "b", "due": None}]
    assert notes == []
    notes2: list[str] = []
    d2 = _sanitize_entry({"ticker": "Y", "last_review_date": "garbage", "first_buy_date": 20261001}, notes2)
    assert "last_review_date" not in d2 and "first_buy_date" not in d2 and len(notes2) == 2


def test_m2e01_yyyymmdd_last_review_still_sees_new_10k(pipe_env, no_engine):
    """last_review_date='20260901'（YYYYMMDD）時，10-01 的 10-K 仍觸發 FULL（修前全字串比較 → NONE，年報被略過）。"""
    cfg, paths, conn, tmp = pipe_env
    seed = {"PRX": ("WATCH", dict(QUIET[1], last_review_date="20260901", last_full_review_date="20260901"))}
    TRP._seed(conn, seed)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": TEN_K}))
    assert "error" not in s, s
    assert s["jobs"] == {"PRX": "DOSSIER"}


# ============================================================ A3-M2E-03：申報恢復只放寬申報窗口
def test_m2e03_recovery_does_not_rewind_last_review_date(pipe_env, no_engine):
    """恢復時 last_review_date 不退回 since：09-15 到期、上次分析（10-10）前已處理過的失效條件不再觸發 THESIS。"""
    cfg, paths, conn, tmp = pipe_env
    data = dict(QUIET[1], last_review_date="2026-10-10",
                kill_criteria_due=[{"text": "毛利率跌破 30%", "due": "2026-09-15"}],
                filings_fail={"count": 2, "since": "2026-09-01", "last": "2026-10-15"})
    seed = {"PRX": ("WATCH", data)}
    TRP._seed(conn, seed)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": []}))
    assert "error" not in s, s
    assert s["jobs"] == {}   # 修前：窗口退回 09-01 → 失效條件 09-15 重觸發 THESIS
    reg = json.loads((paths.work / s["run_id"] / "registry.json").read_text(encoding="utf-8"))
    e = reg["entries"][0]
    assert e["last_review_date"] == "2026-10-10" and e["filings_since"] == "2026-09-01"


def test_m2e03_recovery_still_rechecks_filings_window(pipe_env, no_engine):
    """申報窗口仍放寬到 since：10-01 的 10-K（早於 last_review_date 10-10）照樣觸發 FULL。"""
    cfg, paths, conn, tmp = pipe_env
    data = dict(QUIET[1], last_review_date="2026-10-10",
                filings_fail={"count": 2, "since": "2026-09-01", "last": "2026-10-15"})
    seed = {"PRX": ("WATCH", data)}
    TRP._seed(conn, seed)
    s = run_review(cfg, paths, conn, TRP.ASOF, scope="all", clients=_clients(seed),
                   offline_filings=TRP._offline(tmp, {"PRX": TEN_K}))
    assert s["jobs"] == {"PRX": "DOSSIER"}
    p = json.loads(TRP._jobs(conn, s["run_id"])[0]["params"])
    assert p["last_review_date"] == "2026-10-10"


def test_m2e03_review_due_filings_since_only_widens_filings():
    """review_due.check：filings_since 只影響申報窗口；晚於 last_review_date 的 filings_since 無效。"""
    import importlib.util
    from trader.paths import get_paths
    spec = importlib.util.spec_from_file_location("rd_f3sec", get_paths().skill_deep / "scripts" / "review_due.py")
    rd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rd)
    e = {"ticker": "X", "status": "WATCH", "last_review_date": "2026-10-10", "last_full_review_date": "2026-10-10"}
    act, _, _ = rd.check(dict(e), TEN_K, None, "2026-10-16", 0.047)
    assert act == "NONE"
    act, why, _ = rd.check(dict(e, filings_since="2026-09-01"), TEN_K, None, "2026-10-16", 0.047)
    assert act == "FULL" and any("10-K" in str(w) for w in why)
    act, _, _ = rd.check(dict(e, filings_since="2026-10-12"), TEN_K, None, "2026-10-16", 0.047)
    assert act == "NONE"
