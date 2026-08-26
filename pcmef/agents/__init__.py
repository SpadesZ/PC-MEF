# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.llm.*、pcmef.admin.* 與未來的 experiments.e2_formal 隱式匯入；
#         本檔不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/agents/__init__.py
# 產生時間: 2026-08-26 21:30 +08:00
# 版本: v0.1.0
# 功能說明: 標示 agents 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞。
# 主要責任:
#   1. 使 pcmef.agents 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得在此 import provider 或 cache；provider 會拉進 httpx，
#     而只需要 cache key 契約的呼叫端不該被迫安裝它。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.agents; print('ok')"
# ------------------------------------------------------------
