# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.cli 的 admin 子指令匯入；本檔不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/admin/__init__.py
# 產生時間: 2026-08-26 23:50 +08:00
# 版本: v0.1.0
# 功能說明: 標示 admin 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞。
# 主要責任:
#   1. 使 pcmef.admin 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得在此 import app 或 routes_llm；那會讓沒安裝 Flask 的環境
#     連 import pcmef.admin 都失敗，而 formal run 根本不需要 Flask。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.admin; print('ok')"
# ------------------------------------------------------------
