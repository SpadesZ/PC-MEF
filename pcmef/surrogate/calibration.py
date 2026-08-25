# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate 的四個輸出模組與 single_acquisition 讀取；
#         值來自 configs/surrogate/*.yaml 或 E1 calibration 的輸出；
#         其 hash 進 initial_simulation.lock 與 calibrated_simulation.lock。
# 檔案路徑: pcmef/surrogate/calibration.py
# 產生時間: 2026-08-26 11:35 +08:00
# 版本: v0.1.0
# 功能說明: 存放把模擬單位換算成感測器單位的比例常數（例如能量→MCPS、飛行時間→mm），
#           並強制區分「已校準的值」與「暫用的佔位值」，後者在 formal 模式無法通過。
# 模組定位: surrogate 的校準參數容器。它「不是」校準演算法 ——
#           求出這些值是 E1 calibration 的工作，本模組只負責保管與把關。
# 主要責任:
#   1. CalibratedScale 表示單一常數及其是否為佔位值
#   2. SurrogateCalibration 匯集四個輸出所需的全部常數
#   3. SurrogateCalibration.assert_formal_ready() 在 formal 模式拒絕佔位值
#   4. SurrogateCalibration.calibration_hash() 供 freeze 引用
# 維護提醒:
#   - 不得為任何常數填入「看起來合理」的值而不標 placeholder；
#     未校準的比例常數一旦被當成已校準，E1 的 fidelity 結論就失去意義。
#   - 不得在 formal 模式放行任何 placeholder；這是 surrogate 層唯一的自動防線。
#   - 不得把 distance_offset_mm 當成可調參數去湊合真實均值；
#     SRC-SAI §10 明列「直接用 class label 填前研究均值」為禁止做法。
#   - v0.1.0 新增：首版校準容器。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -k "calibration or placeholder" -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pcmef.core.hash import hash_object

__all__ = [
    "CalibrationError",
    "CalibratedScale",
    "SurrogateCalibration",
    "PLACEHOLDER_SMOKE_CALIBRATION",
]


class CalibrationError(ValueError):
    """校準常數缺失、非有限，或在 formal 模式仍為佔位值。"""


@dataclass(frozen=True)
class CalibratedScale:
    """單一校準常數。

    placeholder=True 代表「這個值只是為了讓管線跑得動」，
    與「已由 calibration 求得」在型別上就分得開，不必靠註解或記憶。
    """

    value: float
    placeholder: bool = False
    source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            raise CalibrationError(f"scale value must be numeric, got {self.value!r}")
        if self.value != self.value or self.value in (float("inf"), float("-inf")):
            raise CalibrationError(f"scale value must be finite, got {self.value!r}")
        if not self.placeholder and not self.source:
            raise CalibrationError(
                "a non-placeholder scale must record its source; an unattributed "
                "constant is indistinguishable from a guess"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "placeholder": self.placeholder,
            "source": self.source,
        }


@dataclass(frozen=True)
class SurrogateCalibration:
    """四個輸出所需的全部校準常數。"""

    # Distance：光程長 -> 距離。monostatic 時為 0.5；本研究場景的
    # 光源與相機非共置，實際係數必須由 calibration 求得。
    optical_path_to_distance: CalibratedScale
    distance_offset_mm: CalibratedScale
    # Signal / Ambient：模擬能量 -> MCPS。
    signal_energy_to_mcps: CalibratedScale
    ambient_energy_to_mcps: CalibratedScale
    # Sigma-like：波形寬度與 SNR 的組合 -> mm 尺度。
    sigma_width_to_mm: CalibratedScale
    sigma_snr_weight: CalibratedScale
    sigma_multipath_weight: CalibratedScale
    # 量測雜訊過程。SRC-SAI §10 要求 sensor-noise 與任何 latent-dynamics
    # 分開凍結，因此雜訊參數獨立成欄而非混進上面的尺度。
    noise_relative_sigma: CalibratedScale
    ambient_jitter_relative: CalibratedScale
    analysis: dict[str, Any] = field(
        default_factory=lambda: {"main_window_halfwidth_bins": 8}
    )

    def scales(self) -> dict[str, CalibratedScale]:
        return {
            name: value
            for name, value in vars(self).items()
            if isinstance(value, CalibratedScale)
        }

    def placeholder_names(self) -> list[str]:
        return sorted(
            name for name, scale in self.scales().items() if scale.placeholder
        )

    def assert_formal_ready(self) -> None:
        """formal 模式下拒絕任何佔位值。"""
        pending = self.placeholder_names()
        if pending:
            raise CalibrationError(
                "surrogate calibration still contains placeholder scales and cannot "
                "be used in formal mode; these must come from E1 calibration, not "
                f"from defaults: {pending}"
            )

    def calibration_hash(self) -> str:
        payload = {
            name: scale.to_dict() for name, scale in sorted(self.scales().items())
        }
        payload["analysis"] = dict(self.analysis)
        return hash_object(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scales": {
                name: scale.to_dict() for name, scale in sorted(self.scales().items())
            },
            "analysis": dict(self.analysis),
            "placeholders": self.placeholder_names(),
            "calibration_hash": self.calibration_hash(),
        }


def _placeholder(value: float) -> CalibratedScale:
    return CalibratedScale(value=value, placeholder=True, source="smoke placeholder")


# 僅供 smoke / toy test 使用。每一項都標記為 placeholder，
# 因此任何 formal 路徑載入它都會被 assert_formal_ready() 擋下。
PLACEHOLDER_SMOKE_CALIBRATION = SurrogateCalibration(
    optical_path_to_distance=_placeholder(0.5),
    distance_offset_mm=_placeholder(0.0),
    signal_energy_to_mcps=_placeholder(1.0),
    ambient_energy_to_mcps=_placeholder(1.0),
    sigma_width_to_mm=_placeholder(1.0),
    sigma_snr_weight=_placeholder(1.0),
    sigma_multipath_weight=_placeholder(1.0),
    noise_relative_sigma=_placeholder(0.01),
    ambient_jitter_relative=_placeholder(0.05),
)
