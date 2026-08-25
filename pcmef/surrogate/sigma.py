# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate.single_acquisition 呼叫；輸入 features.TransientObservables
#         與 calibration.SurrogateCalibration；輸出 Sigma-like (mm)，
#         成為四特徵觀測的第 3 欄（canonical 順序見 core.constants.TOF_SCHEMA）。
# 檔案路徑: pcmef/surrogate/sigma.py
# 產生時間: 2026-08-26 12:25 +08:00
# 版本: v0.2.0
# 功能說明: 估計測距的不確定度。回波越寬、訊噪比越差、多重反射越明顯，
#           量到的距離就越不可靠，這個值就越大。
# 模組定位: 四特徵的 Sigma-like 映射。它是 surrogate 自建的不確定度指標，
#           **不是** VL53L0X 內部真正的 Sigma 暫存器值。
# 主要責任:
#   1. map_sigma_like() 由 FWHM、SNR 與多路徑擴散組合出 mm 尺度的不確定度
# 維護提醒:
#   - 不得把本模組的輸出稱為「真實 VL53L0X internal Sigma」；
#     SRC-SAI §10 明列此為禁止措辭。真實暫存器讀哪一個位址至今未定（NOTE-010），
#     本值只是同名的替代量。
#   - 不得在 FWHM 為 0 時回傳 0；那代表回波被時間窗截斷，寬度不可信，
#     必須讓它以例外浮現而非偽裝成「非常精準」。
#   - 不得移除 SNR 與多路徑項來簡化；三者缺一，Sigma 就退化成 FWHM 的線性縮放，
#     四類之間將失去 SRC-SAI §10 要求的「類別分布、異常波動」鑑別度。
#   - v0.2.0 修正：多路徑改用無因次突起度；原本的時間離散度會被平坦背景主導，
#     且與 FWHM 反比，導致「回波越寬 sigma 越小」的反物理結果。
#   - v0.1.0 新增：首版 Sigma-like 映射。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -k sigma -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np

from pcmef.surrogate.calibration import CalibrationError, SurrogateCalibration
from pcmef.surrogate.distance import SPEED_OF_LIGHT_M_PER_S
from pcmef.surrogate.features import TransientObservables

__all__ = ["map_sigma_like"]

_M_TO_MM = 1000.0


def map_sigma_like(
    observables: TransientObservables,
    calibration: SurrogateCalibration,
    rng: np.random.Generator,
) -> float:
    """由波形寬度、SNR 與多路徑擴散組合出 Sigma-like (mm)。

    三個成分各自對應一種讓測距變不準的物理原因：
      - 回波寬度：脈衝越寬，峰值位置越難定準
      - SNR：背景越強，峰值越容易被雜訊推移
      - 多路徑：瓶壁與液面的多次反射會在主回波之外堆出額外能量
    """
    if observables.fwhm_s <= 0:
        raise CalibrationError(
            "FWHM is zero, which means the echo is truncated by the transient time "
            "window. Returning a small sigma here would misrepresent a broken "
            "simulation as an unusually precise measurement."
        )

    # 脈衝寬度換算成等效距離展寬。
    width_mm = (
        observables.fwhm_s
        * SPEED_OF_LIGHT_M_PER_S
        * calibration.optical_path_to_distance.value
        * _M_TO_MM
    )

    snr = observables.snr
    snr_term = 1.0 + calibration.sigma_snr_weight.value / max(snr, 1e-9)

    # 直接用無因次的突起度，不再除以 FWHM。除以 FWHM 會讓多路徑項與寬度項
    # 反向抵銷，使「回波越寬不確定度越大」這條基本物理被反轉（v0.2.0 修正）。
    multipath_term = (
        1.0 + calibration.sigma_multipath_weight.value * observables.multipath_prominence
    )

    sigma_mm = (
        calibration.sigma_width_to_mm.value * width_mm * snr_term * multipath_term
    )

    relative_sigma = calibration.noise_relative_sigma.value
    if relative_sigma > 0:
        sigma_mm *= 1.0 + float(rng.normal(0.0, relative_sigma))

    return float(max(sigma_mm, 0.0))
