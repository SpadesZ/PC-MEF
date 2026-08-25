# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.provenance.* 的使用者隱式匯入；不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/provenance/__init__.py
# 產生時間: 2026-08-26 03:30 +08:00
# 版本: v0.1.0
# 功能說明: 標示 provenance 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞。
# 主要責任:
#   1. 使 pcmef.provenance 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得在此 re-export sigma 或 timing；兩者各自被顯式 import，
#     維持 SRC-D01 與 SRC-D03 的證據路徑彼此獨立可讀。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.provenance; print('ok')"
# ------------------------------------------------------------
