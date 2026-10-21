"""X20（P42 交接）：觸發檢查取價走研究用 "prices" 客戶端；DGS30 過期時 review 摘要帶警示。"""
import json

import pandas as pd

from test_review_pipeline import (ASOF, CLOSE, SEED, FakeAlpaca, FakeFred, FakeSec, _offline, _seed,  # noqa: F401
                                  env, no_engine)
from trader.pipelines.review import run_review


def test_review_prices_client_only_still_gets_prices(env, no_engine):
    """Hark：沒有 Alpaca 金鑰 → "alpaca" 為 None，研究價（Yahoo）在 "prices"；觸發檢查仍要拿得到價。"""
    cfg, paths, conn, tmp = env
    _seed(conn)
    px = FakeAlpaca({t: CLOSE for t in SEED})
    clients = {"alpaca": None, "prices": px, "fred": FakeFred(), "sec": FakeSec({})}
    s = run_review(cfg, paths, conn, ASOF, clients=clients, offline_filings=_offline(tmp))
    assert "error" not in s, s
    assert px.bar_calls and px.bar_calls[0][3] == "split"
    daily = pd.read_csv(paths.work / s["run_id"] / "daily.csv")
    assert not daily.empty and daily["date"].max() <= ASOF
    assert s["counts"] == {"NONE": 2, "FULL": 1, "THESIS": 1, "PRICE": 2}   # 有價：PRICE 類觸發出現（無價時為 NONE 4）
    assert not (paths.work / s["run_id"] / "news.json").exists()   # 新聞仍只走 Alpaca
    assert px.news_calls == []


def test_review_alpaca_only_unchanged(env, no_engine):
    """舊呼叫端只給 "alpaca"：照舊用它取價。"""
    cfg, paths, conn, tmp = env
    _seed(conn)
    alp = FakeAlpaca({t: CLOSE for t in SEED})
    s = run_review(cfg, paths, conn, ASOF, clients={"alpaca": alp, "fred": FakeFred(), "sec": FakeSec({})},
                   offline_filings=_offline(tmp), due_only=True)
    assert "error" not in s, s
    assert alp.bar_calls and "PRICE" in s["counts"]


class StaleFred(FakeFred):
    warnings = ["DGS30：抓取失敗，改用舊快取"]

    def t30(self, asof):
        return 0.047, "2026-09-30"


def test_review_marks_stale_t30(env, no_engine):
    cfg, paths, conn, tmp = env
    _seed(conn)
    s = run_review(cfg, paths, conn, ASOF, clients={"alpaca": FakeAlpaca({t: CLOSE for t in SEED}),
                                                    "fred": StaleFred(), "sec": FakeSec({})},
                   offline_filings=_offline(tmp), due_only=True)
    assert "error" not in s, s
    t = s["t30"]
    assert t["t30"] == 0.047 and t["stale"] is True and "2026-09-30" in t["warning"]
    assert t["fred_warnings"] == StaleFred.warnings
    saved = json.loads((paths.work / s["run_id"] / "summary.json").read_text(encoding="utf-8"))
    assert saved["t30"]["stale"] is True
    # 新鮮利率（1 天前）不標
    s2 = run_review(cfg, paths, conn, ASOF, run_id="review-fresh", clients={
        "alpaca": FakeAlpaca({t: CLOSE for t in SEED}), "fred": FakeFred(), "sec": FakeSec({})},
        offline_filings=_offline(tmp), due_only=True)
    assert "stale" not in s2["t30"] and "fred_warnings" not in s2["t30"]
