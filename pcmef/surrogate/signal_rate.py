# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate.single_acquisition 呼叫；輸入 features.TransientObservables
#         與 calibration.SurrogateCalibration；輸出 Signal Rate (MCPS)，
#         成為四特徵觀測的第 2 欄（canonical 順序見 core.constants.TOF_SCHEMA）。
# 檔案路徑: pcmef/surrogate/signal_rate.py
# 產生時間: 2026-08-26 12:10 +08:00
# 版本: v0.1.0
# 功能說明: 把主回波時間窗內的能量換算成訊號速率。能量多代表回波強，
#           對應感測器讀到的較高 MCPS。
# 模組定位: 四特徵的 Signal Rate 映射。它「不是」由 Distance 推導的衍生量 ——
#           兩者必須各自從光傳播結果獨立算出。
# 主要責任:
#   1. map_signal_rate() 完成 主回波能量 -> MCPS 的換算並施加雜訊
# 維護提醒:
#   - 不得只用 Distance 推算 Signal；SRC-SAI §10 明列此為禁止做法。
#     兩者若由同一個量導出，reliability 的 D（跨模態分歧）會被人為壓低。
#   - 不得回傳負值；MCPS 是速率，負值代表校準係數為負，屬設定錯誤。
#   - v0.1.0 新增：首版 Signal Rate 映射。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -k signal -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np

from pcmef.surrogate.calibration import CalibrationError, SurrogateCalibration
from pcmef.surrogate.features import TransientObservables

__all__ = ["map_signal_rate"]


def map_signal_rate(
    observables: TransientObservables,
    calibration: SurrogateCalibration,
    rng: np.random.Generator,
) -> float:
    """把主回波能量換算成 Signal Rate (MCPS)。"""
    scale = calibration.signal_energy_to_mcps.value
    if scale <= 0:
        raise CalibrationError(
            f"signal_energy_to_mcps must be positive, got {scale}"
        )

    signal_mcps = observables.main_energy * scale

    relative_sigma = calibration.noise_relative_sigma.value
    if relative_sigma > 0:
        signal_mcps *= 1.0 + float(rng.normal(0.0, relative_sigma))

    # 速率不可為負。雜訊把它推到負值代表 noise_relative_sigma 相對訊號過大，
    # 這裡截到 0 而非拋例外：那是物理上「沒收到光子」的合法狀態。
    return float(max(signal_mcps, 0.0))
