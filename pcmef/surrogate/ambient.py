# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate.single_acquisition 呼叫；輸入 features.TransientObservables
#         與 calibration.SurrogateCalibration；輸出 Ambient Rate (MCPS)，
#         成為四特徵觀測的第 1 欄（canonical 順序見 core.constants.TOF_SCHEMA）。
# 檔案路徑: pcmef/surrogate/ambient.py
# 產生時間: 2026-08-26 12:15 +08:00
# 版本: v0.2.0
# 功能說明: 把主回波以外的背景能量換算成環境光速率，並加上逐次量測的抖動 ——
#           真實環境光每次讀值都會跳動，不是一個固定數字。
# 模組定位: 四特徵的 Ambient Rate 映射。它「不是」每類一個常數 ——
#           抖動是這個量的本質特徵，不是可省略的裝飾。
# 主要責任:
#   1. map_ambient_rate() 完成 背景能量 -> MCPS 的換算並施加必要的抖動
# 維護提醒:
#   - 不得讓每類使用固定常數而無 jitter；SRC-SAI §10 明列此為禁止做法。
#     沒有抖動的 Ambient 會讓 reliability 的品質特徵失去鑑別力，
#     四類之間變成可由單一數值完美分開的假訊號。**這條仍然成立**，
#     只是改由校準目標函數（分佈 W1）承擔，不再由一道會擋住已凍結
#     登記下界的斷言承擔（AMD-004，NOTE-044）。
#   - 不得把 ambient_jitter_relative 設為負值；負的相對抖動沒有物理讀法。
#   - 不得在 jitter = 0 時仍抽一次亂數；那會讓 CRN 在不同 jitter 下
#     走到不同的亂數位置，同階段的評估就不再共用隨機數。
#   - v0.2.0 修正：拒絕條件由 `<= 0` 改為 `< 0`（AMD-004）。
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
    # NOTE(NOTE-044): AMD-004 把「不得為 0」放寬為「不得為負」。
    # jitter = 0 是**已凍結登記範圍 [0.0, 0.5] 的下界**，先前的 `<= 0` 讓
    # stage 0 在原理上無法評估自己的下界，而那個下界不可更改（它在
    # initial_simulation.lock 的 parameter_ranges 裡，屬勘誤禁區）。
    #
    # 原本的擔憂（沒有抖動的 Ambient 讓四類可由單一無雜訊數值完美分開）
    # 現在由**目標函數本身**承擔而非由這道硬性拒絕：J 比的是整個分佈的
    # W1 距離，真實 Ambient 的離散度不為零，因此 jitter -> 0 會讓
    # ambient 那幾項的 W1 變大而被 optimizer 自己排除。用分佈距離擋，
    # 比用一道會擋住合法邊界的斷言擋更精確。
    if jitter < 0:
        raise CalibrationError(
            f"ambient_jitter_relative must not be negative, got {jitter}. "
            "A negative relative jitter has no physical reading."
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
    # jitter = 0 時**不抽亂數**，與 signal_rate / distance / sigma 的
    # `if relative_sigma > 0` 同一個寫法。抽一個標準差為 0 的常態值雖然
    # 也回傳 0.0，但它會消耗一個 rng 抽樣，讓同一組種子在 jitter=0 與
    # jitter>0 之間走到不同的亂數位置 —— CRN 就不再是 CRN。
    if jitter > 0:
        ambient_mcps *= 1.0 + float(rng.normal(0.0, jitter))
    return float(max(ambient_mcps, 0.0))
