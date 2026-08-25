# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.adapters.* 的使用者隱式匯入；不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/adapters/__init__.py
# 產生時間: 2026-08-26 00:15 +08:00
# 版本: v0.1.0
# 功能說明: 標示 adapters 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞。
# 主要責任:
#   1. 使 pcmef.adapters 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得在此 import legacy_csv 或 simulation；那會讓 pandas 與未來的 mitsuba
#     在只需要 base 契約時就被拉進來。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.adapters; print('ok')"
# ------------------------------------------------------------
