# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.simulation.* 的使用者隱式匯入；不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/simulation/__init__.py
# 產生時間: 2026-08-26 06:05 +08:00
# 版本: v0.1.0
# 功能說明: 標示 simulation 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞。
# 主要責任:
#   1. 使 pcmef.simulation 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得在此 import mitsuba_adapter；那會讓 46 MB 的原生擴充在只需要
#     ScenarioConfig 時就被拉進來，也會讓沒裝 mitsuba 的機器無法收集測試。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.simulation; print('ok')"
# ------------------------------------------------------------
