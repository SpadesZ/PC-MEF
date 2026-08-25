# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate.distance / signal_rate / ambient / sigma 四個映射模組共用；
#         輸入為 mitransient_adapter 產出的 (H,W,bins,C) 張量與時間軸，
#         輸出的物理量供各模組各自做校準映射。
# 檔案路徑: pcmef/surrogate/features.py
# 產生時間: 2026-08-26 11:10 +08:00
# 版本: v0.2.0
# 功能說明: 從一次光傳播模擬中，量出感測器「看得到」的幾件事 —— 回波最強是在哪個
#           時間點、主回波有多少能量、背景有多少、回波有多寬、有沒有多重反射。
#           這一層純物理，不含任何需要校準的比例常數。
# 模組定位: 四個輸出模組的共用前置。它「不是」感測器模型 ——
#           它不產生 Distance/Signal/Ambient/Sigma，只產生它們各自的原始依據。
# 主要責任:
#   1. collapse_to_waveform() 把 (H,W,bins,C) 空間積分成單一時序波形
#   2. TransientObservables 保存一次 acquisition 的全部物理量
#   3. extract_observables() 計算峰值時間、能量重心、主/背景能量、FWHM、SNR、多路徑擴散
# 維護提醒:
#   - 不得在此加入任何比例常數把物理量轉成 MCPS 或 mm；那是校準，
#     必須留在各輸出模組並受 formal-blocking 管制。
#   - 不得把 bins 當成 measurement 取樣點；本模組的時間軸是 optical transient time，
#     一次 acquisition 內部的光飛行歷程（SRC-D03）。
#   - 不得在波形全零時回傳「合理的預設值」；全零代表模擬時間窗設錯，
#     必須讓它以例外浮現。
#   - v0.2.0 修正：多路徑改用無因次突起度；原本的時間離散度會被平坦背景主導，
#     且與 FWHM 反比，導致「回波越寬 sigma 越小」的反物理結果。
#   - v0.1.0 新增：首版物理量抽取。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -k "observable or waveform or fwhm" -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "TransientFeatureError",
    "TransientObservables",
    "collapse_to_waveform",
    "extract_observables",
]


class TransientFeatureError(ValueError):
    """transient 張量形狀不符，或波形不含任何能量。"""


@dataclass(frozen=True)
class TransientObservables:
    """一次 acquisition 的物理觀測量。全部為模擬單位，尚未校準。"""

    peak_time_s: float
    centroid_time_s: float
    main_energy: float
    background_energy: float
    total_energy: float
    fwhm_s: float
    snr: float
    # 無因次的次要回波突起度，值域約 [0,1]。刻意不用「主窗外能量的時間離散度」：
    # 平坦背景本身就會產生很大的離散度，且該量與 FWHM 成反比，
    # 會讓「回波越寬不確定度越大」這條基本物理被反轉（v0.2.0 修正）。
    multipath_prominence: float
    main_window: tuple[int, int]

    def to_dict(self) -> dict[str, float]:
        return {
            "peak_time_s": self.peak_time_s,
            "centroid_time_s": self.centroid_time_s,
            "main_energy": self.main_energy,
            "background_energy": self.background_energy,
            "total_energy": self.total_energy,
            "fwhm_s": self.fwhm_s,
            "snr": self.snr,
            "multipath_prominence": self.multipath_prominence,
        }


def collapse_to_waveform(transient: np.ndarray) -> np.ndarray:
    """把 (H,W,bins,C) 積分成 (bins,) 的單一時序波形。

    VL53L0X 是單一 SPAD 陣列，在其視野內把光子計數加總成一條時序曲線；
    對整個像面與色彩通道求和是這件事的模擬對應物。
    """
    array = np.asarray(transient, dtype=np.float64)
    if array.ndim == 1:
        waveform = array
    elif array.ndim == 4:
        waveform = array.sum(axis=(0, 1, 3))
    elif array.ndim == 3:
        waveform = array.sum(axis=(0, 1))
    else:
        raise TransientFeatureError(
            f"transient must have 1, 3 or 4 dimensions, got shape {array.shape}"
        )
    if not np.all(np.isfinite(waveform)):
        raise TransientFeatureError("transient waveform contains NaN or Inf")
    return waveform


def _fwhm_seconds(
    waveform: np.ndarray, time_axis: np.ndarray, peak_index: int
) -> float:
    """以半高全寬量測主回波寬度。

    採線性內插而非直接數 bin 數：bin 寬在本研究約 9 ps，
    直接數 bin 會讓 FWHM 量化成幾個離散值，之後的 Sigma 映射跟著階梯化。
    """
    peak_value = float(waveform[peak_index])
    if peak_value <= 0:
        return 0.0
    half = peak_value / 2.0

    def _crossing(indices: range) -> float | None:
        previous = peak_index
        for index in indices:
            if waveform[index] <= half:
                lower, upper = sorted((index, previous))
                span = waveform[upper] - waveform[lower]
                if span == 0:
                    return float(time_axis[index])
                ratio = (half - waveform[lower]) / span
                return float(
                    time_axis[lower] + ratio * (time_axis[upper] - time_axis[lower])
                )
            previous = index
        return None

    left = _crossing(range(peak_index - 1, -1, -1))
    right = _crossing(range(peak_index + 1, waveform.size))
    if left is None or right is None:
        # 回波被時間窗截斷，寬度不可信；回報 0 讓上層判定為異常。
        return 0.0
    return max(right - left, 0.0)


def extract_observables(
    transient: np.ndarray,
    time_axis_s: np.ndarray,
    main_window_halfwidth_bins: int = 8,
) -> TransientObservables:
    """從一次 transient 抽出全部物理觀測量。

    main_window_halfwidth_bins 決定「主回波」的界定範圍。它是**分析參數**
    而非校準常數：換一個值會改變 Signal/Ambient 的切分，因此必須寫入
    surrogate freeze manifest，但它不需要教授裁決，因為它不是研究主張的一部分。
    """
    waveform = collapse_to_waveform(transient)
    axis = np.asarray(time_axis_s, dtype=np.float64).ravel()
    if axis.size != waveform.size:
        raise TransientFeatureError(
            f"time axis length {axis.size} does not match transient bins {waveform.size}"
        )
    total = float(waveform.sum())
    if total <= 0:
        raise TransientFeatureError(
            "transient waveform carries no energy; the simulation time window is "
            "placed outside the actual light arrival and must be fixed rather than "
            "defaulted"
        )

    peak_index = int(np.argmax(waveform))
    peak_time = float(axis[peak_index])
    centroid_time = float(np.sum(axis * waveform) / total)

    low = max(peak_index - main_window_halfwidth_bins, 0)
    high = min(peak_index + main_window_halfwidth_bins + 1, waveform.size)
    main_energy = float(waveform[low:high].sum())
    background_energy = float(total - main_energy)

    background_bins = waveform.size - (high - low)
    background_mean = background_energy / background_bins if background_bins > 0 else 0.0
    peak_value = float(waveform[peak_index])
    snr = float(peak_value / background_mean) if background_mean > 0 else float("inf")

    fwhm = _fwhm_seconds(waveform, axis, peak_index)

    # 多路徑：量主回波之外「有沒有另一個明顯的峰」，而不是量背景鋪得多開。
    # 以中位數為基線（對單一次要回波穩健），取最大突起相對主峰的比例。
    # 平坦背景 -> 接近 0；瓶壁或液面的二次反射 -> 明顯大於 0。
    outside = np.concatenate([waveform[:low], waveform[high:]])
    if outside.size > 0 and peak_value > 0:
        baseline = float(np.median(outside))
        contrast = peak_value - baseline
        multipath = (
            float((float(outside.max()) - baseline) / contrast) if contrast > 0 else 0.0
        )
        multipath = float(np.clip(multipath, 0.0, 1.0))
    else:
        multipath = 0.0

    return TransientObservables(
        peak_time_s=peak_time,
        centroid_time_s=centroid_time,
        main_energy=main_energy,
        background_energy=background_energy,
        total_energy=total,
        fwhm_s=fwhm,
        snr=snr,
        multipath_prominence=multipath,
        main_window=(low, high),
    )
