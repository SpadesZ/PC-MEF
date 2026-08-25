# PC-MEF Research System source maintenance contract
# 上下游: 被所有 pcmef.core.* 的使用者隱式匯入；不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/core/__init__.py
# 產生時間: 2026-08-25 22:23 +08:00
# 版本: v0.1.0
# 功能說明: 標示 core 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞。
# 主要責任:
#   1. 使 pcmef.core 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得寫 from .schema import *；那會建立第二條匯入路徑，讓 NOTE-006 的
#     「唯一 stabilize_prob」全樹掃描出現誤判。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.core; print('ok')"
# ------------------------------------------------------------
