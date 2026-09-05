# PC-MEF Research System source maintenance contract
# 上下游: 平台層的根套件；由 console、cli 與 admin 匯入其下的 projects 子套件。
#         **本檔不含邏輯，僅宣告平台層邊界。**
# 檔案路徑: pcmef/platform/__init__.py
# 產生時間: 2026-09-06 14:10 +08:00
# 版本: v0.1.0
# 功能說明: SAI v0.6.0 §39 建議的 platform 層落點，承載 Project 抽象。
# 模組定位: 科學核心（simulation / perception / agents / stats / experiments）
#           與平台組織層的分界線。平台層只處理「這是哪一個研究專案、
#           它的東西放哪裡」，不處理任何科學決策。
# 主要責任:
#   1. 宣告 pcmef.platform 為平台層根套件
#   2. 讓 projects 子套件可被匯入
# 維護提醒:
#   - 不得在平台層放入任何科學計算、門檻、公式或決策邏輯。
#     平台層改的是組織與 namespace；科學身分由 freeze/ 的 lock 決定。
#     一旦平台層開始「順便」算東西，Thesis Profile 的 scientific identity
#     就會有第二個來源，而兩個來源必然漂移。
#   - v0.1.0 新增：首版，對應平台化 Phase 1。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_paths.py -v
# ------------------------------------------------------------

from __future__ import annotations

__all__: list[str] = []
