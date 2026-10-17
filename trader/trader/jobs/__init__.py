"""見 plan.md。"""
# 先載入 handlers：它在檔尾依序載入各工作模組（deep、review_jobs…）。
# 否則單獨 `import trader.jobs.review_jobs` 時會先載 deep → handlers → 回頭載半初始化的 deep，循環匯入失敗。
from . import handlers  # noqa: F401,E402
