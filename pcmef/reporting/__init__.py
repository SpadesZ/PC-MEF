# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.cli 的 `figures export` 呼叫；讀 formal E2 report JSON。
# 檔案路徑: pcmef/reporting/__init__.py
# 產生時間: 2026-09-04 16:20 +08:00
# 版本: v0.1.0
# 功能說明: 論文用圖的產出層。
# 模組定位: 與 `pcmef/console/results.py` 分工明確 —— console 的圖是給人
#           在瀏覽器裡看趨勢的（伺服器端 SVG、零 script、零繪圖相依），
#           本套件產出的是要貼進論文的向量圖。兩者都**只讀 artifact**，
#           不得重算任何科學量。
# 主要責任:
#   1. style 提供 rcParams 與調色盤
#   2. figures 依 formal report 產出五張圖（× 三種格式 = 15 個檔）
# 維護提醒:
#   - 不得在本套件內重算任何指標。畫上去的數字必須逐字來自 report，
#     否則「圖上的」與「凍結的」會分岔，而分岔時沒有人會發現。
#   - 不得讓本套件被 console 或決策路徑 import。matplotlib 是重相依，
#     admin 那組不該因為畫圖而被迫安裝它。
# 驗證方式:
#   - py -3.10 -m pytest tests/reporting/test_figures.py -v
# ------------------------------------------------------------

from __future__ import annotations

__all__ = ["style", "figures"]
