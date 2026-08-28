# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate 的四個輸出模組與 single_acquisition 讀取；
#         值來自 configs/surrogate/*.yaml 或 E1 calibration 的輸出；
#         其 hash 進 initial_simulation.lock 與 calibrated_simulation.lock。
# 檔案路徑: pcmef/surrogate/calibration.py
# 產生時間: 2026-08-26 11:35 +08:00
# 版本: v0.2.0
# 功能說明: 存放把模擬單位換算成感測器單位的比例常數（例如能量→MCPS、飛行時間→mm），
#           並強制區分「已校準的值」與「暫用的佔位值」，後者在 formal 模式無法通過。
# 模組定位: surrogate 的校準參數容器。它「不是」校準演算法 ——
#           求出這些值是 E1 calibration 的工作，本模組只負責保管與把關。
# 主要責任:
#   1. CalibratedScale 表示單一常數及其是否為佔位值
#   2. SurrogateCalibration 匯集四個輸出所需的全部常數
#   3. SurrogateCalibration.assert_formal_ready() 在 formal 模式拒絕佔位值，
#      並轉呼叫 core.parameters 檢查場景側的其餘建模常數
#   4. SurrogateCalibration.with_calibrated() 套入校準結果並拒絕覆寫 derived
#   5. SurrogateCalibration.calibration_hash() 供 freeze 引用
# 維護提醒:
#   - 不得為任何常數填入「看起來合理」的值而不標 placeholder；
#     未校準的比例常數一旦被當成已校準，E1 的 fidelity 結論就失去意義。
#   - 不得在 formal 模式放行任何 placeholder；這是 surrogate 層唯一的自動防線。
#   - 不得把 distance_offset_mm 當成可調參數去湊合真實均值；
#     SRC-SAI §10 明列「直接用 class label 填前研究均值」為禁止做法。
#   - 不得把 optical_path_to_distance 改回 placeholder 或放進校準集合；
#     它由共置幾何決定（NOTE-026），改它等於改幾何假設。
#   - 不得用 check_registry=False 讓 formal 路徑略過 registry 檢查；
#     那個參數只為單元測試而存在（NOTE-030）。
#   - v0.1.0 新增：首版校準容器。
#   - v0.2.0 optical_path_to_distance 改標 derived 並移出可校準集合；
#     assert_formal_ready() 併入 parameter registry 防線；
#     新增 with_calibrated() 作為 derived 常數的實質保護（NOTE-030）。
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
    "OPTICAL_PATH_TO_DISTANCE",
    "PLACEHOLDER_SMOKE_CALIBRATION",
]


class CalibrationError(ValueError):
    """校準常數缺失、非有限，或在 formal 模式仍為佔位值。"""


@dataclass(frozen=True)
class CalibratedScale:
    """單一校準常數。

    三種狀態必須在型別上就分得開，不必靠註解或記憶：

    | placeholder | derived | 意義 |
    |---|---|---|
    | True  | False | 只是為了讓管線跑得動，formal 模式擋下 |
    | False | False | 已由 E1 calibration 求得 |
    | False | True  | **由幾何/物理推導而來，不是自由參數** |

    derived 不是「已校準」的別名：已校準的值可以被下一次 calibration 改寫，
    derived 的值不行 —— 改它等於改幾何假設（NOTE-030）。
    """

    value: float
    placeholder: bool = False
    source: str = ""
    derived: bool = False

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
        if self.derived and self.placeholder:
            raise CalibrationError(
                "a scale cannot be both derived and placeholder: derived means its "
                "value follows from the geometry, which is either true or not"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "placeholder": self.placeholder,
            "source": self.source,
            "derived": self.derived,
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

    def derived_names(self) -> list[str]:
        return sorted(name for name, scale in self.scales().items() if scale.derived)

    def calibratable_names(self) -> list[str]:
        """calibration 允許求解的常數。derived 不在其中（NOTE-030）。"""
        return sorted(
            name for name, scale in self.scales().items() if not scale.derived
        )

    def with_calibrated(self, **values: float) -> "SurrogateCalibration":
        """把 calibration 求得的值套進來，回傳新的容器。

        拒絕覆寫 derived 常數。這是 optical_path_to_distance 唯一的實質防線 ——
        SRC-SAI §10 禁止把它當旋鈕去逼近真實均值，而一句註解攔不住任何人。
        """
        from dataclasses import replace

        derived = set(self.derived_names())
        forbidden = sorted(set(values) & derived)
        if forbidden:
            raise CalibrationError(
                f"{forbidden} are derived from the scene geometry and must not be "
                "fitted. Changing them means changing the geometry assumption, which "
                "has to happen in the scene first (SRC-SAI §10, NOTE-026)."
            )
        unknown = sorted(set(values) - set(self.scales()))
        if unknown:
            raise CalibrationError(f"unknown calibration scale(s): {unknown}")
        updates = {
            name: CalibratedScale(
                value=float(value), placeholder=False, source="E1 calibration"
            )
            for name, value in values.items()
        }
        return replace(self, **updates)

    def assert_formal_ready(self, check_registry: bool = True) -> None:
        """formal 模式下拒絕任何佔位值，並一併檢查整組參數 registry。

        只檢查自己這九個是不夠的：場景側還有二十幾個未校準建模常數，
        它們一樣會決定四特徵，卻長期在這條防線之外（NOTE-030）。
        """
        pending = self.placeholder_names()
        if pending:
            raise CalibrationError(
                "surrogate calibration still contains placeholder scales and cannot "
                "be used in formal mode; these must come from E1 calibration, not "
                f"from defaults: {pending}"
            )
        if check_registry:
            from pcmef.core.parameters import (
                ParameterRegistryError,
                assert_formal_ready as assert_parameters_formal_ready,
            )

            try:
                assert_parameters_formal_ready()
            except ParameterRegistryError as error:
                raise CalibrationError(str(error)) from error

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


#: 光程長 -> 單程距離。**derived，不是 placeholder。**
#: 光源與相機共置（monostatic），光程恰為單程距離的兩倍，故係數由幾何得 0.5
#: （NOTE-026）。它不該出現在任何 calibration 搜尋集合裡；with_calibrated()
#: 會拒絕覆寫它。
OPTICAL_PATH_TO_DISTANCE = CalibratedScale(
    value=0.5,
    placeholder=False,
    derived=True,
    source=(
        "derived from monostatic sensor/emitter geometry (NOTE-026): the optical "
        "path is exactly twice the one-way range, so the factor is 1/2"
    ),
)


# 僅供 smoke / toy test 使用。除 derived 的那一項外每一項都標記為 placeholder，
# 因此任何 formal 路徑載入它都會被 assert_formal_ready() 擋下。
PLACEHOLDER_SMOKE_CALIBRATION = SurrogateCalibration(
    optical_path_to_distance=OPTICAL_PATH_TO_DISTANCE,
    distance_offset_mm=_placeholder(0.0),
    signal_energy_to_mcps=_placeholder(1.0),
    ambient_energy_to_mcps=_placeholder(1.0),
    sigma_width_to_mm=_placeholder(1.0),
    sigma_snr_weight=_placeholder(1.0),
    sigma_multipath_weight=_placeholder(1.0),
    noise_relative_sigma=_placeholder(0.01),
    ambient_jitter_relative=_placeholder(0.05),
)
