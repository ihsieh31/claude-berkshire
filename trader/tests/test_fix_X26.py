"""X26：DOSSIER 提示寫明 missing 何時才以「P-58」／「P-60」開頭（D20 financial_precheck 以此開頭判 STEP0），與 output_schemas 範例一致。"""
from __future__ import annotations

import json
import re

from trader.jobs.deep import financial_precheck
from trader.paths import PKG_ROOT

PROMPT = PKG_ROOT / "routing" / "prompts" / "DOSSIER.md"
SCHEMAS = PKG_ROOT / "routing" / "output_schemas.json"


def _rule_line() -> str:
    lines = [ln for ln in PROMPT.read_text(encoding="utf-8").splitlines() if "「P-58」／「P-60」開頭" in ln]
    assert len(lines) == 1, "DOSSIER.md 應有且只有一行 P-58／P-60 開頭規則"
    return lines[0]


def _segments(line: str) -> tuple[str, str]:
    """規則行依產業分開：「保險——…」「銀行——…」（X26R）。"""
    assert "保險——" in line and "銀行——" in line
    ins = line.split("保險——", 1)[1].split("銀行——", 1)[0]
    bank = line.split("銀行——", 1)[1].split("框架見到此開頭", 1)[0]
    return ins, bank


def test_dossier_prompt_states_p58_p60_prefix_rule():
    line = _rule_line()
    ins, bank = _segments(line)
    # 保險：不足 5 年確實算不出 P-58 才開頭；可算但不滿 10 年不得
    assert "不足 5 個會計年度" in ins and "確實算不出 P-58" in ins and "以「P-58」開頭" in ins
    assert "年數不滿 10 年" in ins and "不得以「P-58」開頭" in ins
    # 銀行：P-60 壓力測試輸入缺到算不出才開頭；不綁年數條件，其他序列或年數少但可算不得
    for s in ("撥備前稅前利益", "平均放款", "權益／資產", "確實算不出 P-60", "以「P-60」開頭", "P-60 仍可算", "不得以「P-60」開頭"):
        assert s in bank, s
    assert "5 個會計年度" not in bank and "10 年" not in bank
    assert "STEP0" in line and "MISSING_DATA" in line
    assert "{" not in line and "}" not in line  # 不干擾模板代入


def test_prompt_rule_consistent_with_schema_example():
    ex = json.loads(SCHEMAS.read_text(encoding="utf-8"))["DOSSIER"]["missing"]
    ex = ex if isinstance(ex, str) else json.dumps(ex, ensure_ascii=False)
    ex_ins, ex_bank = ex.split("銀行", 1)
    ex_bank = ex_bank.split("框架見到", 1)[0]
    ins, bank = _segments(_rule_line())
    # 保險：兩邊都以 P-58 開頭、都用 5 個會計年度門檻
    assert "以「P-58」開頭" in ex_ins and "至少 5 個會計年度" in ex_ins
    assert "以「P-58」開頭" in ins and "不足 5 個會計年度" in ins
    # 銀行：兩邊都以「算不出 P-60」為條件、都不帶年數門檻，且列同樣的壓力輸入
    assert "算不出 P-60" in ex_bank and "以「P-60」開頭" in ex_bank and "會計年度" not in ex_bank
    assert "算不出 P-60" in bank and "以「P-60」開頭" in bank and "會計年度" not in bank
    for k in ("撥備前稅前", "平均放款", "權益"):
        assert k in ex_bank and k in bank, k
    # 共同：STEP0 MISSING_DATA、P-59 不以 P-58 開頭
    line = _rule_line()
    for s in ("STEP0 MISSING_DATA", "P-59"):
        assert s in ex and s in line, s
    assert "P-59 綜合率缺照常列，不以 P-58 開頭" in line


def _td(tmp_path, industry: str, sic: str):
    td = tmp_path / industry
    (td / "dossier").mkdir(parents=True)
    (td / "sec").mkdir()
    (td / "sec" / "_index.json").write_text(json.dumps([{"history": True}]), encoding="utf-8")
    (td / "dossier" / "_meta.json").write_text(json.dumps({"sic": sic, "industry": industry}, ensure_ascii=False), encoding="utf-8")
    return td


def test_prompt_example_triggers_precheck_and_non_prefixed_does_not(tmp_path):
    line = _rule_line()
    m = re.search(r"例：「(P-58[^」]*)」", line)
    assert m, "規則行應附 P-58 開頭的例子"
    td = _td(tmp_path, "保險", "6331")
    assert financial_precheck(td, {"missing": [m.group(1)]})
    assert financial_precheck(td, {"missing": ["浮存金序列只有 7 年（P-58 仍可算，未滿 10 年）"]}) is None
    # 銀行（X26R）：例句觸發；其他序列／年數少但 P-60 可算的寫法不觸發
    b = re.search(r"例：「(P-60[^」]*)」", line)
    assert b, "規則行應附 P-60 開頭的例子"
    tb = _td(tmp_path, "銀行", "6021")
    assert financial_precheck(tb, {"missing": [b.group(1)]})
    assert financial_precheck(tb, {"missing": ["逾放與轉銷序列只有 3 個會計年度（P-60 仍可算）"]}) is None
