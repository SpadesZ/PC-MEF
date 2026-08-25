# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 provenance resolve-sigma 呼叫；讀 adapters.legacy_kg 取得的
#         真實 Sigma 觀測值與（若有）採集程式碼證據；寫出
#         provenance/sigma_resolution.json，供 E1-G08 判定與 e1_candidates.lock 引用。
# 檔案路徑: pcmef/provenance/sigma.py
# 產生時間: 2026-08-26 03:35 +08:00
# 版本: v0.1.0
# 功能說明: 判斷前研究的 Sigma 數值到底是怎麼算出來的 —— 用觀測到的數值範圍
#           反推它除以了多少（scaling），並記錄它讀自哪個暫存器仍未確定；
#           兩者都確定才允許四特徵 E1 把 Sigma 當 primary 證據。
# 模組定位: SRC-D01/D02 的證據產生器。它「不會」替未決的事項挑一個答案 ——
#           暫存器身分無法從數值反推，缺採集程式碼時一律回報 UNRESOLVED。
# 主要責任:
#   1. ScalingHypothesis 描述單一 scaling 候選及其相容性判定
#   2. evaluate_scaling_hypotheses() 以觀測值域反推各候選除數的隱含原始碼值
#      （刻意不以 test_ 開頭命名：pytest 會把任何 test_ 開頭的可呼叫物件
#      當成測試案例收集，即使它是從產品模組 import 進來的）
#   3. resolve_sigma() 綜合 scaling 與暫存器證據產生 SigmaResolution
#   4. SigmaResolution.to_artifact() 產生 E1-G08 的證據 artifact
# 維護提醒:
#   - 不得因為「四參數腳本用 0x1E」就把 register 判為 RESOLVED；
#     資料集由當時的採集腳本產生，那份腳本不在已知的三份之中（NOTE-010）。
#   - 不得在 scaling 相容但 register 未定時回傳 RESOLVED；E1-G08 要求兩者皆定。
#   - 不得用整數性檢定判斷 scaling；來源 CSV 是四捨五入後的窗口平均值，
#     小數位已被截斷，整數性檢定必然失敗且會給出錯誤結論。
#   - v0.1.0 新增：首版 Sigma provenance resolver，對應 NOTE-011。
# 驗證方式:
#   - py -3.10 -m pytest tests/provenance/test_sigma_and_timing.py -k sigma -v
#   - py -3.10 -m pcmef.cli provenance resolve-sigma
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pcmef.core.constants import (
    RATE_RAW_SCALE_DIVISOR,
    SIGMA_RAW_SCALE_DIVISOR,
    SIGMA_REGISTER_CANDIDATES,
    SIGMA_STATUS_RESOLVED,
    SIGMA_STATUS_UNRESOLVED,
)

__all__ = [
    "SigmaProvenanceError",
    "ScalingHypothesis",
    "SigmaResolution",
    "evaluate_scaling_hypotheses",
    "resolve_sigma",
]

# VL53L0X 的 result 欄位是 16-bit 無號整數。
_RAW_MIN = 0
_RAW_MAX = 65535

# 若某個 scaling 假設下，隱含原始值只佔滿量程的極小比例，
# 代表該假設要求感測器全程只用到量程邊角，實務上不合理。
_MIN_PLAUSIBLE_RANGE_FRACTION = 1e-3


class SigmaProvenanceError(ValueError):
    """觀測值不足以進行 scaling 判定。"""


@dataclass(frozen=True)
class ScalingHypothesis:
    """單一 scaling 候選的判定結果。"""

    divisor: float
    implied_raw_min: float
    implied_raw_max: float
    fits_16bit: bool
    range_fraction: float
    plausible: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "divisor": self.divisor,
            "implied_raw_min": round(self.implied_raw_min, 4),
            "implied_raw_max": round(self.implied_raw_max, 4),
            "fits_16bit": self.fits_16bit,
            "range_fraction": round(self.range_fraction, 8),
            "plausible": self.plausible,
            "note": self.note,
        }


@dataclass(frozen=True)
class SigmaResolution:
    """Sigma provenance 的完整判定。"""

    status: str
    observed_min: float
    observed_max: float
    observed_median: float
    n_observations: int
    scaling_hypotheses: tuple[ScalingHypothesis, ...]
    resolved_divisor: float | None
    register_candidates: tuple[int, ...]
    resolved_register: int | None
    blocking_reasons: tuple[str, ...] = field(default=())

    def to_artifact(self) -> dict[str, Any]:
        """產生 provenance/sigma_resolution.json 的內容（E1-G08 證據）。"""
        return {
            "status": self.status,
            "gate": "E1-G08",
            "observation": {
                "n": self.n_observations,
                "min": round(self.observed_min, 6),
                "median": round(self.observed_median, 6),
                "max": round(self.observed_max, 6),
            },
            "scaling": {
                "resolved_divisor": self.resolved_divisor,
                "hypotheses": [h.to_dict() for h in self.scaling_hypotheses],
            },
            "register": {
                "candidates": [hex(c) for c in self.register_candidates],
                "resolved": hex(self.resolved_register)
                if self.resolved_register is not None
                else None,
            },
            "blocking_reasons": list(self.blocking_reasons),
        }


def evaluate_scaling_hypotheses(
    values: np.ndarray, divisors: tuple[float, ...] | None = None
) -> tuple[ScalingHypothesis, ...]:
    """對每個候選除數，反推隱含的原始暫存器值域並判定相容性。

    判定只用值域，不用整數性：來源 CSV 是四捨五入過的窗口平均值，
    小數位已被截斷，任何整數性檢定都會失敗並導向錯誤結論。
    """
    finite = np.asarray(values, dtype=np.float64).ravel()
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise SigmaProvenanceError("no finite Sigma observations to analyse")

    candidates = divisors or (1.0, RATE_RAW_SCALE_DIVISOR, SIGMA_RAW_SCALE_DIVISOR)
    observed_min = float(finite.min())
    observed_max = float(finite.max())

    hypotheses: list[ScalingHypothesis] = []
    for divisor in candidates:
        raw_min = observed_min * divisor
        raw_max = observed_max * divisor
        fits = _RAW_MIN <= raw_min and raw_max <= _RAW_MAX
        fraction = (raw_max - raw_min) / (_RAW_MAX - _RAW_MIN)

        if not fits:
            note = (
                f"implied raw values exceed the 16-bit range "
                f"[{_RAW_MIN}, {_RAW_MAX}]"
            )
            plausible = False
        elif divisor == 1.0:
            # 未縮放時原始值必為整數；觀測到非整數即排除。
            non_integer = float(np.mean(np.abs(finite - np.round(finite)) > 1e-9))
            plausible = non_integer == 0.0
            note = (
                "raw register reads are integers; "
                f"{non_integer:.0%} of observations are non-integer"
            )
        elif fraction < _MIN_PLAUSIBLE_RANGE_FRACTION:
            plausible = False
            note = (
                f"implied raw span is only {fraction:.2e} of full scale, which would "
                "require the sensor to use a negligible corner of its range"
            )
        else:
            plausible = True
            note = "implied raw values occupy a plausible portion of the 16-bit range"

        hypotheses.append(
            ScalingHypothesis(
                divisor=float(divisor),
                implied_raw_min=raw_min,
                implied_raw_max=raw_max,
                fits_16bit=fits,
                range_fraction=float(fraction),
                plausible=plausible,
                note=note,
            )
        )
    return tuple(hypotheses)


def resolve_sigma(
    values: np.ndarray,
    acquisition_register: int | None = None,
    acquisition_evidence: str = "",
) -> SigmaResolution:
    """綜合觀測值與採集程式碼證據，判定 Sigma provenance 狀態。

    acquisition_register 必須來自**產生這批資料的採集腳本**，
    不能拿三份推論腳本中的任一份代替（NOTE-010）。缺此證據時
    register 保持未決，整體狀態為 UNRESOLVED，四特徵 E1 primary 被 E1-G08 擋下。
    """
    finite = np.asarray(values, dtype=np.float64).ravel()
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise SigmaProvenanceError("no finite Sigma observations to analyse")

    hypotheses = evaluate_scaling_hypotheses(finite)
    plausible = [h for h in hypotheses if h.plausible]
    blocking: list[str] = []

    if len(plausible) == 1:
        resolved_divisor: float | None = plausible[0].divisor
    else:
        resolved_divisor = None
        if not plausible:
            blocking.append("no scaling hypothesis is consistent with the observations")
        else:
            blocking.append(
                "multiple scaling hypotheses remain consistent: "
                + ", ".join(str(h.divisor) for h in plausible)
            )

    resolved_register: int | None = None
    if acquisition_register is None:
        blocking.append(
            "the Sigma register is not determinable from values alone; both "
            f"{[hex(c) for c in SIGMA_REGISTER_CANDIDATES]} are 16-bit reads sharing "
            "the same divisor. Evidence from the acquisition script that produced "
            "this dataset is required."
        )
    elif acquisition_register not in SIGMA_REGISTER_CANDIDATES:
        blocking.append(
            f"acquisition register {hex(acquisition_register)} is not among the "
            f"observed candidates {[hex(c) for c in SIGMA_REGISTER_CANDIDATES]}; "
            "record the discrepancy before proceeding"
        )
    elif not acquisition_evidence:
        blocking.append(
            "acquisition_register was supplied without evidence; record the source "
            "file and line that establishes it"
        )
    else:
        resolved_register = acquisition_register

    status = (
        SIGMA_STATUS_RESOLVED
        if resolved_divisor is not None and resolved_register is not None
        else SIGMA_STATUS_UNRESOLVED
    )

    return SigmaResolution(
        status=status,
        observed_min=float(finite.min()),
        observed_max=float(finite.max()),
        observed_median=float(np.median(finite)),
        n_observations=int(finite.size),
        scaling_hypotheses=hypotheses,
        resolved_divisor=resolved_divisor,
        register_candidates=tuple(SIGMA_REGISTER_CANDIDATES),
        resolved_register=resolved_register,
        blocking_reasons=tuple(blocking),
    )
