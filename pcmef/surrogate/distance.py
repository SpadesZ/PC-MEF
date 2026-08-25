# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate.single_acquisition 呼叫；輸入 features.TransientObservables
#         與 calibration.SurrogateCalibration；輸出一個 Distance (mm) 純量，
#         成為四特徵觀測的第 0 欄。
# 檔案路徑: pcmef/surrogate/distance.py
# 產生時間: 2026-08-26 12:00 +08:00
# 版本: v0.1.0
# 功能說明: 把回波的飛行時間換算成距離。用峰值或能量重心的時間乘光速得到光程長，
#           再依校準係數折算成感測器讀到的毫米值，最後加上量測雜訊。
# 模組定位: 四特徵的 Distance 映射。它「不是」真值標註 ——
#           距離必須由光傳播結果算出，不得由類別標籤查表。
# 主要責任:
#   1. DistanceEstimator 列舉峰值/重心兩種時間取法
#   2. map_distance() 完成 飛行時間 -> 光程長 -> 毫米 的換算並施加雜訊
# 維護提醒:
#   - 不得用 class label 去填前研究的均值（Empty~100、Water~114 等）；
#     SRC-SAI §10 明列此為禁止做法，那會讓 E1 變成自我實現的預言。
#   - 不得把 optical_path_to_distance 當可調旋鈕去逼近真實均值；
#     它是校準結果，不是擬合參數。
#   - 不得回傳負距離；出現負值代表校準 offset 不合理，必須讓它浮現。
#   - v0.1.0 新增：首版 Distance 映射。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -k distance -v
# ------------------------------------------------------------

from __future__ import annotations

from enum import Enum

import numpy as np

from pcmef.surrogate.calibration import CalibrationError, SurrogateCalibration
from pcmef.surrogate.features import TransientObservables

__all__ = ["SPEED_OF_LIGHT_M_PER_S", "DistanceEstimator", "map_distance"]

SPEED_OF_LIGHT_M_PER_S: float = 299_792_458.0
_M_TO_MM = 1000.0


class DistanceEstimator(str, Enum):
    """飛行時間的取法。兩者對多路徑的敏感度不同，必須顯式選擇並凍結。"""

    PEAK = "peak"
    ENERGY_CENTROID = "energy_centroid"


def map_distance(
    observables: TransientObservables,
    calibration: SurrogateCalibration,
    rng: np.random.Generator,
    estimator: DistanceEstimator = DistanceEstimator.PEAK,
) -> float:
    """把飛行時間換算成 Distance (mm)。

    光程長 = c x t。乘上 optical_path_to_distance 折算成單程距離
    （共置收發時為 0.5；本研究場景光源與相機非共置，實際係數由校準決定），
    再換成毫米並加上幾何 offset。
    """
    time_s = (
        observables.peak_time_s
        if estimator is DistanceEstimator.PEAK
        else observables.centroid_time_s
    )
    if time_s <= 0:
        raise CalibrationError(
            f"non-positive time of flight ({time_s}); the transient window is placed "
            "before any light arrives"
        )

    optical_path_m = SPEED_OF_LIGHT_M_PER_S * time_s
    distance_mm = (
        optical_path_m * calibration.optical_path_to_distance.value * _M_TO_MM
        + calibration.distance_offset_mm.value
    )

    relative_sigma = calibration.noise_relative_sigma.value
    if relative_sigma > 0:
        distance_mm *= 1.0 + float(rng.normal(0.0, relative_sigma))

    if distance_mm < 0:
        raise CalibrationError(
            f"distance mapped to a negative value ({distance_mm:.3f} mm); the "
            "calibration offset is inconsistent with the scene geometry"
        )
    return float(distance_mm)
