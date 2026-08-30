# PC-MEF Research System source maintenance contract
# 上下游: 匯出 pcmef.perception 的資料層與 baseline 層；資料一律來自
#         pcmef.simulation.paired 的成對生成器，**不讀任何真實資料**，
#         也不碰 FORMAL_E1_FINAL。
# 檔案路徑: pcmef/perception/__init__.py
# 產生時間: 2026-08-31 11:15 +08:00
# 版本: v0.1.0
# 功能說明: M4 perception 套件的入口。只含合成資料上的單模態 baseline
#           與傳統融合，不含 LLM、Reliability Agent、D/U/Q gate 或 E2。
# 模組定位: 套件宣告層。真正的內容在 dataset.py（資料與 leakage 稽核）
#           與 baselines.py（模型、度量、融合）。
# 主要責任:
#   1. 宣告 pcmef.perception 為套件，供 dataset / baselines 匯入
#   2. 以檔頭載明本套件的資料邊界（synthetic-only）
# 維護提醒:
#   - 不得在本套件內讀取 data/raw_real 的任何一筆，也不得取用
#     FORMAL_E1_FINAL；perception 的輸入只有成對生成器的合成資料。
#   - 不得在此加入 gate / E2 / LLM 的任何部分；那些是後續階段，
#     混進來會讓「這一階段只證明了什麼」變得說不清楚。
#   - v0.1.0 新增：M4 perception 套件。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_perception_dataset.py -v
#   - py -3.10 -m pytest tests/unit/test_perception_baselines.py -v
# ------------------------------------------------------------

"""Perception baselines (M4). Synthetic-only; never touches real data."""
