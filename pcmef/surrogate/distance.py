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
    """飛行時間的取法。四者對多路徑的敏感度不同，必須顯式選擇並凍結。

    候選集合與判準在 configs/estimator_preregistration.yaml **預先**凍結
    （NOTE-035）；本列舉不得在比較之後新增成員。
    """

    PEAK = "peak"
    ENERGY_CENTROID = "energy_centroid"
    LEADING_EDGE = "leading_edge"
    STRONGEST_RETURN_CENTROID = "strongest_return_centroid"


#: 預註冊的可調參數預設值。**全類共用**，不得逐類設定（NOTE-035）。
DEFAULT_DETECTION_THRESHOLD_SIGMA = 5.0
DEFAULT_MIN_RETURN_BINS = 2

#: 依預註冊判準選定的 estimator（NOTE-037）。
#: **不是**在這裡挑的 —— 它是 outputs/estimator_select/estimator_selection.json
#: 的結論，preregistration v0.2.0（含 AMD-002 的 S4）。
#: 改這一行而不重跑 selection，會讓程式與選定證據分家；
#: tests 會比對兩者是否一致。
SELECTED_ESTIMATOR = DistanceEstimator.LEADING_EDGE


def find_returns(
    waveform: np.ndarray,
    threshold: float,
    min_return_bins: int,
) -> list[tuple[int, int]]:
    """把波形切成「連續超過門檻」的數段 return，回傳 [(start, stop), ...]。

    刻意用「相對波形自身雜訊水準的門檻」而不是絕對距離視窗：距離視窗會把
    「感測器看到什麼」變成「我們允許它看到什麼」，那正是紅線禁止的偷渡。
    """
    above = np.asarray(waveform) > threshold
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(above):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if index - start >= min_return_bins:
                segments.append((start, index))
            start = None
    if start is not None and above.size - start >= min_return_bins:
        segments.append((start, int(above.size)))
    return segments


def _gated_time(
    observables: TransientObservables,
    estimator: DistanceEstimator,
    threshold_sigma: float,
    min_return_bins: int,
) -> float:
    """LEADING_EDGE / STRONGEST_RETURN_CENTROID 共用的 return 分段。"""
    waveform = observables.waveform
    axis = observables.time_axis_s
    noise = observables.ambient_noise_per_bin
    if waveform is None or axis is None:
        raise CalibrationError(
            f"{estimator.value} needs the full waveform; the observables were built "
            "without one. Re-run extract_observables on the active transient."
        )
    if noise is None:
        raise CalibrationError(
            f"{estimator.value} defines its detection threshold in units of the "
            "measured ambient noise level, so it requires a dedicated ambient pass "
            "(NOTE-034). Without one the threshold would be an invented constant."
        )

    threshold = noise * threshold_sigma
    segments = find_returns(waveform, threshold, min_return_bins)
    if not segments:
        raise CalibrationError(
            f"no return crossed the detection threshold "
            f"({threshold_sigma} x ambient noise {noise:.6g} = {threshold:.6g}); "
            "the estimator must not silently fall back to the global peak"
        )

    if estimator is DistanceEstimator.LEADING_EDGE:
        start, _ = segments[0]
        return float(axis[start])

    # STRONGEST_RETURN_CENTROID：取能量最大的那一段，在**該段之內**求重心。
    start, stop = max(segments, key=lambda s: float(waveform[s[0] : s[1]].sum()))
    window = waveform[start:stop]
    total = float(window.sum())
    if total <= 0:
        return float(axis[start])
    return float(np.sum(axis[start:stop] * window) / total)


def map_distance(
    observables: TransientObservables,
    calibration: SurrogateCalibration,
    rng: np.random.Generator,
    estimator: DistanceEstimator = DistanceEstimator.PEAK,
    threshold_sigma: float = DEFAULT_DETECTION_THRESHOLD_SIGMA,
    min_return_bins: int = DEFAULT_MIN_RETURN_BINS,
) -> float:
    """把飛行時間換算成 Distance (mm)。

    光程長 = c x t。乘上 optical_path_to_distance 折算成單程距離
    （共置收發，係數由幾何得 0.5，NOTE-026），再換成毫米並加上幾何 offset。
    """
    if estimator is DistanceEstimator.PEAK:
        time_s = observables.peak_time_s
    elif estimator is DistanceEstimator.ENERGY_CENTROID:
        time_s = observables.centroid_time_s
    else:
        time_s = _gated_time(
            observables, estimator, threshold_sigma, min_return_bins
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
