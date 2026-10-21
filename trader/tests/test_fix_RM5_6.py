"""RM5-6：M5-RS6-01（符號連結沙箱根）與 M5_D6-04（估值區間／目標價寫法進價格盲提示）。"""
import fnmatch
import json
from pathlib import Path

import pytest

from trader.analysis import writeback
from trader.analysis.dossier_prep import scrub_price_sentences
from trader.jobs import isolation as iso
from trader.jobs import review_jobs
from trader.jobs.sandbox import create_sandbox
from trader.llm.claude_code import ClaudeCodeRunner
from trader.models import JobSpec, TierConfig


def _job(job_type: str, n: int) -> JobSpec:
    return JobSpec(job_id=f"r:KO:{job_type}:{n}", run_id="r", job_type=job_type, ticker="KO", asof="2026-10-16")


def _read_denied(rules: list[str], path: Path) -> list[str]:
    s = "/" + path.as_posix()
    return [r for r in rules if r.startswith("Read(") and fnmatch.fnmatch(s, r[5:-1])]


# ---------------------------------------------------------------- M5-RS6-01

def test_symlinked_root_priced_sandbox_lands_beside_real_root_and_is_denied(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "sb"
    link.symlink_to(real)
    ret = iso.prepare_root(link, [])
    assert ret == real.resolve()
    assert (tmp_path / ("real" + iso.PRICED_SUFFIX)).is_dir()            # priced 根建在實際路徑旁
    assert not (tmp_path / ("sb" + iso.PRICED_SUFFIX)).exists()
    sb_b = create_sandbox(ret, _job("FULL_B", 1))
    sb_a = create_sandbox(ret, _job("FULL_A", 2))
    assert iso.priced_root(ret) in sb_b.parents                           # 看價沙箱不回到隔離根
    assert ret in sb_a.parents
    price = sb_b / "dossier" / "KO_price.json"
    price.parent.mkdir(parents=True)
    price.write_text('{"price": 70.39}')
    tier = TierConfig(name="weak", backend="claude_code", model="haiku")
    cmd = ClaudeCodeRunner(tier, runner_fn=lambda *a, **k: None, denies=[]).command(sb_a, "FULL_A")
    rules = json.loads(cmd[cmd.index("--settings") + 1])["permissions"]["deny"]
    assert _read_denied(rules, price)                                     # FULL_A 封鎖涵蓋兄弟價格檔
    assert not _read_denied(rules, sb_a / "dossier" / "KO.md")


# ---------------------------------------------------------------- M5_D6-04

LEAKS = ["保守價值區間：每股 85–95 美元", "賣出檢視線 130", "建倉區間 60–70", "目標賣出區 130–140 美元",
         "在 60 至 70 美元之間建倉", "合理價 90 美元", "上檔空間到 130", "目標買進區 60–70",
         "保守價值區間：每股 85–95 美元。護城河來自品牌。",
         # RM5-6R：原本漏網的估值寫法
         "目標價 120 美元", "公允價值 85–95 美元", "合理估值區間 85 到 95 美元", "買進區間 60–70", "目標賣出價 130",
         "估值 90 美元/股", "股價在 60 至 70 美元之間", "上檔空間 30%",
         # RM5-6RR：RM5-6R 收窄後回歸漏網的寫法（含千元股 19xx–20xx 美元價位區間）
         "合理價位 90 美元", "合理價值約 90 美元", "價值區間落在 80–100", "建倉區間設定為 55–60", "賣出檢視線設在 150",
         "目標買進區落在 60–70", "上檔空間約 30%", "上檔空間還有 30%", "價值區間 1950–2050 美元", "目標買進區 1900–2100 美元",
         "目標賣出價位 130", "公允價值估計 90 美元",
         # RM5-6R3
         "上檔空間有 30%", "上檔空間達 30%", "上檔空間為 30%", "上檔空間高達 30%"]
KEEP = ["營收區間 85–95 億美元", "資本支出 60–70 億", "營收在 60 至 70 億美元之間", "以合理價格買進優秀公司",
        "毛利率 60–62%，護城河來自品牌", "上檔空間有限，管理層保守",
        # RM5-6R：RM5-6 版（詞後 12 字內任一數字＋整行刪）誤殺的基本面句
        "以合理價格買進優秀公司，過去 10 年營收成長 8%", "我們以合理價格收購了 3 家裝瓶廠",
        "合理價格策略使市占率 3 年內升至 45%", "上檔空間有限，毛利率維持 60%",
        "上檔空間有限，但 2025 年自由現金流 95 億美元", "每股盈餘在 3 至 4 美元之間", "每股股利 1.9 至 2.0 美元之間",
        "2027 年每股盈餘介於 2.8 到 3.0 美元之間", "平均客單價在 5 至 6 美元之間", "每箱售價 5 至 6 美元之間",
        "時薪在 15 到 20 美元之間", "檢視線上通路占比 25%", "重新檢視線下門市 300 家的租約",
        "建倉區間（新廠建設期）2026–2028", "建倉區間 2026–2028", "目標買進區域市場：東南亞 3 國",
        "公允價值變動損益 3 億美元",
        # RM5-6RR：帶 百萬／兆／billion／B／千 單位的會計公允價值句
        "公允價值 85 百萬美元", "投資組合公允價值約 1.2 兆日圓", "公允價值 3.5 billion 美元的投資組合",
        "公允價值 2,100 百萬美元", "公允價值 $2.1B", "公允價值 850 千美元",
        # RM5-6R3
        "建倉區間 2026–27 年", "價值區間 2025–30 年", "建倉區間 2026-27", "公允價值約 85 bn"]


@pytest.mark.parametrize("s", LEAKS)
def test_valuation_range_lines_scrubbed_on_thesis_path(s):
    text = f"護城河來自品牌與通路。\n{s}\n管理層資本配置穩健。"
    out = writeback.scrub_valuation_lines(scrub_price_sentences(text)[0])
    assert s not in out
    assert "護城河來自品牌與通路。" in out and "管理層資本配置穩健。" in out


@pytest.mark.parametrize("s", LEAKS)
def test_valuation_range_kill_criteria_scrubbed(s):
    assert review_jobs._kill_criteria({"kill_criteria": [s]}) == ["（含價格數字，已刪）"]


@pytest.mark.parametrize("s", KEEP)
def test_fundamental_ranges_not_scrubbed(s):
    assert writeback.scrub_valuation_lines(scrub_price_sentences(s)[0]) == s
    assert review_jobs._kill_criteria({"kill_criteria": [s]}) == [s]
