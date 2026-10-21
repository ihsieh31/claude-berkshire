"""RM1-9：初選 summary 的三處小修（RM1-1 的 needs_other）。M1-R5-02 資料不足清單帶 dq_note；M1-RS2-03
deletions_to_review 只列含 J* 的刪除（screen_overrides 只撤銷 J*）；M1-R4-03 summary 帶 overrides_expired。"""
from __future__ import annotations

from test_screen_pipeline import ASOF, SP, _skill_make, env  # noqa: F401（env 為 fixture）


def test_summary_lacking_dq_review_j_only_and_overrides_expired(env):
    paths, conn, inputs = env
    F, P = _skill_make()
    F.loc[32, ["shares_cover", "roa"]] = [1e4, 0.12]        # 單位錯誤 → 殖利率設缺、dq_note → 資料不足道
    P.loc[32, ["vol_36m", "drawdown_3y"]] = [0.15, -0.05]   # 不落入統計排除（同 skill test_dq_note_…）
    F.loc[40, "shares_cover"] = 1e4                         # 同上，且持續虧損 → J1 刪除＋資料品質註記
    F.loc[40, ["net_income", "ni_prev", "loss_yrs5", "ni_sum5", "ni5_mean", "roe", "roe_med5"]] = \
        [-5e8, -3e8, 4, -1e9, -2e8, -0.1, -0.05]
    F.to_csv(inputs / "features.csv", index=False)
    P.to_csv(inputs / "prices.csv", index=False)
    (paths.state / "exclusions.csv").write_text("ticker,excl_fraud\nT005,1\n")          # 只有 X、歷史經濟力佳
    (paths.state / SP.USER_OVERRIDES).write_text("ticker,date,reason\nT000,2025-10-15,一次性\n")  # 滿 365 天
    s = SP.run_screen(None, paths, conn, ASOF, inputs_dir=inputs)
    assert "error" not in s, s

    lk = {x["ticker"]: x for x in s["lacking"]}
    assert "股數或單位錯誤" in lk["T032"]["dq_note"], s["lacking"]                       # M1-R5-02

    deleted = {d["ticker"]: d["codes"] for d in s["deleted"]}
    assert deleted["T005"].startswith("X") and "J" not in deleted["T005"]
    rv = {x["ticker"]: x for x in s["deletions_to_review"]}
    assert "T005" not in rv                                                             # M1-RS2-03
    assert "資料品質註記" in rv["T040"]["why"] and "J1" in rv["T040"]["codes"]          # 含 J* 照列

    assert [x["ticker"] for x in s["overrides_expired"]] == ["T000"]                    # M1-R4-03
    assert "T000" in deleted and not s["overrides"]
