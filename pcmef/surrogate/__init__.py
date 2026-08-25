# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.surrogate.* 的使用者隱式匯入；不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/surrogate/__init__.py
# 產生時間: 2026-08-26 11:05 +08:00
# 版本: v0.1.0
# 功能說明: 標示 surrogate 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞。
# 主要責任:
#   1. 使 pcmef.surrogate 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得在此 re-export 四個輸出模組；它們刻意分檔，
#     是為了讓每一欄的映射各自可稽核（SRC-SAI §31 目錄結構）。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.surrogate; print('ok')"
# ------------------------------------------------------------
