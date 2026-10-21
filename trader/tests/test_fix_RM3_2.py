"""RM3-2：M3-R8-02（超大整數不得讓驗證器拋例外）、M3-RS4-03（紅隊「無違規」佔位不得當成違規）。"""
from __future__ import annotations

import pytest

from trader.jobs import validate


# ---------------------------------------------------------------- M3-R8-02

def test_is_num_huge_int_is_invalid_not_exception():
    assert validate._is_num(10 ** 400) is False
    assert validate._is_num(-(10 ** 400)) is False
    assert validate._is_num(10 ** 300) is True


def test_check_huge_int_returns_error_for_retry():
    # 數字欄位收到超大整數：回傳格式錯誤（走重試），不拋 OverflowError
    schemas = {"X": {"n": 1, "m": "數字或 null", "k": "小數或 0"}}
    big = int("1" + "0" * 400)
    _, errors = validate.check("X", {"n": big, "m": big, "k": big}, schemas)
    assert len(errors) == 3
    _, errors = validate.check("X", {"n": 2, "m": None, "k": 0.5}, schemas)
    assert errors == []


# ---------------------------------------------------------------- M3-RS4-03

@pytest.mark.parametrize("v", [None, [], [""], ["  "], ["無"], ["none"], ["None"], ["N/A"], ["n/a"],
                               ["沒有"], ["無。"], ["null"], ["-"], ["無", ""],
                               [{"where": "", "claimed": "", "correct": "", "source": ""}],
                               [{"where": "無", "claimed": None, "correct": "", "source": ""}]])
def test_real_items_placeholders_are_empty(v):
    assert validate.real_items(v) == []


@pytest.mark.parametrize("v", [["用價格作答 G3"], ["無", "補假設值"], ["沒有引用來源就估值"],
                               [{"where": "§3", "claimed": "ROE 30%", "correct": "ROE 20%", "source": "10-K"}],
                               [{"where": "", "claimed": "", "correct": "12", "source": ""}]])
def test_real_items_keeps_real_entries(v):
    assert validate.real_items(v) != []


def _run_red_team(result, tmp_path, monkeypatch):
    from trader.jobs import deep
    calls = {}
    monkeypatch.setattr(deep, "_td", lambda job, ctx: tmp_path)
    monkeypatch.setattr(deep, "_finish", lambda job, ctx, sig, *a, **k: calls.setdefault("signal", sig))
    monkeypatch.setattr(deep, "_hold_review_after", lambda job, ctx, sig: [])

    class _Redo:
        @staticmethod
        def _last(job, ctx):   # RedTeamHandler 以 FullBHandler._last 判斷已是最後一輪
            return False

        def _redo(self, job, ctx, fb, params):
            calls["redo"] = fb
            return ["redo"]
    monkeypatch.setattr(deep, "FullBHandler", _Redo)

    class _Job:
        params = {}
        job_id = "j1"
    deep.RedTeamHandler().post(_Job(), dict(result), None)
    return calls


@pytest.mark.parametrize("viol,nerr", [(["無"], []), ([""], []), (["none"], ["無"]),
                                       ([], [{"where": "", "claimed": "", "correct": "", "source": ""}])])
def test_red_team_placeholder_passes_buy(viol, nerr, tmp_path, monkeypatch):
    res = {"ticker": "X", "fatal": False, "fatal_paths": [], "number_errors": nerr,
           "rule_violations": viol, "verdict": "維持"}
    calls = _run_red_team(res, tmp_path, monkeypatch)
    assert calls.get("signal") == "BUY" and "redo" not in calls


def test_red_team_real_violation_still_sent_back(tmp_path, monkeypatch):
    res = {"ticker": "X", "fatal": False, "fatal_paths": [], "number_errors": [],
           "rule_violations": ["無", "用價格作答 G2"], "verdict": "維持"}
    calls = _run_red_team(res, tmp_path, monkeypatch)
    assert "redo" in calls and "signal" not in calls


def test_real_items_non_list_fail_closed():
    assert validate.real_items("用價格作答 G3") == ["用價格作答 G3"]
    assert validate.real_items("無") == []
