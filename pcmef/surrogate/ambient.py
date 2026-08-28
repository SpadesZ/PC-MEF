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

    # NOTE(NOTE-034): 來源必須是**獨立 ambient pass**（VCSEL 關閉、室內光開啟）。
    # 先前用的是 active pass 主窗以外的能量，實測那個量 99.97% 是感測器自己
    # 打出去的光造成的多重反射，室內光只佔 0.033% —— 那不是 Ambient。
    # 缺 ambient pass 時直接拒絕，不退回舊行為：一個算得出來但量錯東西的
    # Ambient，比一個算不出來的 Ambient 危險得多。
    if observables.ambient_energy is None:
        raise CalibrationError(
            "Ambient Rate requires a dedicated ambient pass (VCSEL off, room light "
            "on); no ambient_transient was provided. It must not fall back to the "
            "active pass's out-of-window energy: that quantity was measured to be "
            "99.97% laser multipath and only 0.033% room light (NOTE-034)."
        )

    ambient_mcps = observables.ambient_energy * scale
    ambient_mcps *= 1.0 + float(rng.normal(0.0, jitter))
    return float(max(ambient_mcps, 0.0))
