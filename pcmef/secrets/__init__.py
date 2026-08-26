# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.llm.*、pcmef.admin.* 與 pcmef.agents.provider 隱式匯入；
#         本檔不讀寫任何資料，無輸出。
# 檔案路徑: pcmef/secrets/__init__.py
# 產生時間: 2026-08-26 21:05 +08:00
# 版本: v0.1.0
# 功能說明: 標示 secrets 為 Python 套件，內容刻意留空。
# 模組定位: 命名空間宣告。它「不是」匯出中樞，也不持有任何 secret 值。
# 主要責任:
#   1. 使 pcmef.secrets 成為可匯入的套件，不執行任何 side effect
# 維護提醒:
#   - 不得在此 import vault 或 crypto；crypto 會拉進 cryptography 套件，
#     而只需要解析 secret_ref 字串的呼叫端不該被迫安裝它。
#   - 不得在本套件內 import 名為 secrets 的標準函式庫模組時使用相對匯入；
#     Python 3 預設絕對匯入，寫成 import secrets 取得的是標準函式庫那一份。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef.secrets; print('ok')"
# ------------------------------------------------------------
