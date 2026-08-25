# PC-MEF Research System source maintenance contract
# 上下游: 被所有 pcmef.* 子模組與 tests 匯入，也被 cli 用來顯示版本；
#         不讀寫任何檔案，唯一輸出是 __version__ 字串。
# 檔案路徑: pcmef/__init__.py
# 產生時間: 2026-08-25 22:22 +08:00
# 版本: v0.1.0
# 功能說明: 宣告套件版本號，除此之外什麼都不做。
# 模組定位: 套件根。它「不是」匯出中樞 —— 子模組一律各自顯式 import。
# 主要責任:
#   1. __version__ 提供供 cli 與 provenance 記錄使用的版本字串
# 維護提醒:
#   - 不得在此 re-export 子模組；那會讓 numpy 以外的重依賴（未來的 tensorflow、
#     mitsuba）在 import pcmef 的當下就被拉進來，拖慢 CLI 啟動與測試收集。
#   - 不得在此載入 config、接觸檔案系統或初始化 logging。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -c "import pcmef; print(pcmef.__version__)"
# ------------------------------------------------------------

__version__ = "0.1.0"
