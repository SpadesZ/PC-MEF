# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.experiments.e1 / e1_outcome 與未來的 e2_formal 匯入；
#         本檔不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/stats/__init__.py
# 產生時間: 2026-08-27 13:00 +08:00
# 版本: v0.1.0
# 功能說明: 標示 stats 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞。
# 主要責任:
#   1. 使 pcmef.stats 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得在此 import metrics 或 bootstrap；那會讓只需要型別的呼叫端
#     連帶拉進 scipy。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.stats; print('ok')"
# ------------------------------------------------------------
