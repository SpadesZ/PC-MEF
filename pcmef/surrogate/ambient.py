# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate.single_acquisition 呼叫；輸入 features.TransientObservables
#         與 calibration.SurrogateCalibration；輸出 Ambient Rate (MCPS)，
#         成為四特徵觀測的第 1 欄（canonical 順序見 core.constants.TOF_SCHEMA）。
# 檔案路徑: pcmef/surrogate/ambient.py
# 產生時間: 2026-08-26 12:15 +08:00
# 版本: v0.1.0
# 功能說明: 把主回波以外的背景能量換算成環境光速率，並加上逐次量測的抖動 ——
#           真實環境光每次讀值都會跳動，不是一個固定數字。
# 模組定位: 四特徵的 Ambient Rate 映射。它「不是」每類一個常數 ——
#           抖動是這個量的本質特徵，不是可省略的裝飾。
# 主要責任:
#   1. map_ambient_rate() 完成 背景能量 -> MCPS 的換算並施加必要的抖動
# 維護提醒:
#   - 不得讓每類使用固定常數而無 jitter；SRC-SAI §10 明列此為禁止做法。
#     沒有抖動的 Ambient 會讓 reliability 的品質特徵失去鑑別力，
#     四類之間變成可由單一數值完美分開的假訊號。
#   - 不得把 ambient_jitter_relative 設為 0 來讓測試更好過；
#     本模組會拒絕非正的抖動設定。
#   - v0.1.0 新增：首版 Ambient Rate 映射。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -k ambient -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np

from pcmef.surrogate.calibration import CalibrationError, SurrogateCalibration
from pcmef.surrogate.features import TransientObservables

__all__ = ["map_ambient_rate"]


def map_ambient_rate(
    observables: TransientObservables,
    calibration: SurrogateCalibration,
    rng: np.random.Generator,
) -> float:
    """把背景能量換算成 Ambient Rate (MCPS)，並施加逐次抖動。"""
    scale = calibration.ambient_energy_to_mcps.value
    if scale <= 0:
        raise CalibrationError(
            f"ambient_energy_to_mcps must be positive, got {scale}"
        )

    jitter = calibration.ambient_jitter_relative.value
    if jitter <= 0:
        raise CalibrationError(
            "ambient_jitter_relative must be positive: a constant ambient rate per "
            "class is an explicitly forbidden surrogate behaviour (SRC-SAI §10). "
            "Without jitter the four classes become separable by a single noiseless "
            "number, which is an artefact of the surrogate rather than physics."
        )

    ambient_mcps = observables.background_energy * scale
    ambient_mcps *= 1.0 + float(rng.normal(0.0, jitter))
    return float(max(ambient_mcps, 0.0))
