# PC-MEF Research System source maintenance contract
# 上下游: 由 E1 pipeline 與 scenario_generator 呼叫；輸入 SensorSurrogate 與
#         一或多份 transient；輸出 (500,4) TofRecording 與其 measurement-time
#         provenance，後者直接餵給 core.schema.MeasurementTime。
# 檔案路徑: pcmef/surrogate/temporal_model.py
# 產生時間: 2026-08-26 13:05 +08:00
# 版本: v0.1.0
# 功能說明: 把單次觀測擴展成一整筆連續量測 —— 500 個時間點、每點四個數值。
#           兩種產生方式：真的算 500 次光傳播，或用一次光傳播加上經校準的
#           量測時間隨機模型。兩者都必須明示取樣間隔，不得預設。
# 模組定位: measurement-time 這條時間軸的產生器。它與 optical transient time
#           是兩條不同的軸，**禁止互相換算**（SRC-D03）。
# 主要責任:
#   1. RecordingMode 區分 realizations 與 stochastic 兩種產生方式
#   2. TemporalModel.generate_recording() 產生 (n_samples, 4) recording
#   3. 強制要求 sample_interval_s，並拒絕以任何全域常數頂替
#   4. recording_provenance() 保存模式、seed、間隔來源供 freeze 引用
# 維護提醒:
#   - 不得把 transient bins 切成 measurement points；
#     `tof_recording = transient_bins[:500]` 是 SRC-SAI §10 明列的禁止做法。
#     本模組刻意不接受「從 transient 直接取樣」的路徑，n_samples 與 bins 無關。
#   - 不得為 sample_interval_s 提供預設值；SRC-SAI §10 明列
#     「silently default every recording to exactly 0.082 s」為禁止做法，
#     且實測已證實資料集內存在 0.082 與 0.0624 兩種取樣率（NOTE-011）。
#   - 不得把 sensor-noise 與 latent-dynamics 混在同一個參數；SRC-SAI §10 要求
#     兩者分開凍結，本模組只實作前者，後者若要加入必須另立顯式模型並獨立驗證。
#   - v0.1.0 新增：首版 500 點 temporal model。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -k "recording or temporal or interval" -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

import numpy as np

from pcmef.core.constants import N_TOF_FEATURES, TOF_RECORDING_POINTS, TOF_SCHEMA
from pcmef.surrogate.single_acquisition import SensorSurrogate

__all__ = ["TemporalModelError", "RecordingMode", "TofRecording", "TemporalModel"]


class TemporalModelError(ValueError):
    """取樣間隔缺失、realization 數量不符，或產生的 recording 形狀錯誤。"""


class RecordingMode(str, Enum):
    """500 點 recording 的兩種產生方式（SRC-SAI §10）。"""

    # 真的算 500 次光傳播。物理上最直接，但成本是單次的 500 倍。
    REALIZATIONS = "acquisition_realizations"
    # 一次光傳播 + 經 E1 校準的量測時間隨機模型。
    STOCHASTIC = "measurement_time_stochastic_model"


@dataclass(frozen=True)
class TofRecording:
    """一筆 (n_samples, 4) 量測序列與其 provenance。"""

    values: np.ndarray
    sample_interval_s: float
    sample_interval_source: str
    mode: RecordingMode
    seed: int

    @property
    def n_samples(self) -> int:
        return int(self.values.shape[0])

    @property
    def duration_s(self) -> float:
        return self.n_samples * self.sample_interval_s

    def to_provenance(self) -> dict[str, Any]:
        return {
            "shape": list(self.values.shape),
            "tof_schema": list(TOF_SCHEMA),
            "sample_interval_s": self.sample_interval_s,
            "sample_interval_source": self.sample_interval_source,
            "duration_s": self.duration_s,
            "mode": self.mode.value,
            "seed": self.seed,
            "time_axis": "measurement_time",
        }


@dataclass(frozen=True)
class TemporalModel:
    """把單次觀測擴展成 500 點 recording。"""

    surrogate: SensorSurrogate

    def generate_recording(
        self,
        transient: np.ndarray | Sequence[np.ndarray],
        time_axis_s: np.ndarray,
        *,
        sample_interval_s: float,
        sample_interval_source: str,
        seed: int,
        n_samples: int = TOF_RECORDING_POINTS,
        mode: RecordingMode = RecordingMode.STOCHASTIC,
        ambient_transient: np.ndarray | None = None,
    ) -> TofRecording:
        """產生 (n_samples, 4) recording。

        sample_interval_s 與其來源皆為必填關鍵字參數。這是刻意的：
        SRC-SAI §10 禁止「silently default every recording to exactly 0.082 s」，
        而實測已證實資料集內同時存在 0.082 與 0.0624 兩種取樣率（NOTE-011），
        任何預設值都必然對其中一批是錯的。

        n_samples 與 transient 的 bin 數**完全無關**。若兩者有關聯，
        就代表實作把 optical transient time 誤當成 measurement time。
        """
        if not sample_interval_s or sample_interval_s <= 0:
            raise TemporalModelError(
                "sample_interval_s is required and must be positive; it must come "
                "from the recording's own provenance or a frozen temporal config, "
                "never from a global constant"
            )
        if not sample_interval_source:
            raise TemporalModelError(
                "sample_interval_source is required; an interval without a stated "
                "origin cannot be audited"
            )
        if n_samples <= 0:
            raise TemporalModelError(f"n_samples must be positive, got {n_samples}")

        rng = np.random.default_rng(seed)

        if mode is RecordingMode.REALIZATIONS:
            transients = list(transient) if isinstance(transient, Sequence) else None
            if transients is None:
                raise TemporalModelError(
                    "REALIZATIONS mode requires a sequence of independently rendered "
                    "transients, one per acquisition"
                )
            if len(transients) != n_samples:
                raise TemporalModelError(
                    f"REALIZATIONS mode needs exactly {n_samples} rendered transients, "
                    f"got {len(transients)}. Reusing one transient for every sample "
                    "would be the stochastic model, not independent realizations."
                )
            rows = [
                self.surrogate.map_single_acquisition(
                    item, time_axis_s, rng, ambient_transient
                )
                for item in transients
            ]
        else:
            if isinstance(transient, Sequence) and not isinstance(
                transient, np.ndarray
            ):
                raise TemporalModelError(
                    "STOCHASTIC mode takes a single transient; pass one array"
                )
            # 每次 acquisition 都重跑一次映射：噪聲在映射層施加，
            # 因此同一份 transient 會產生互不相同但同分佈的觀測。
            rows = [
                self.surrogate.map_single_acquisition(
                    transient, time_axis_s, rng, ambient_transient
                )
                for _ in range(n_samples)
            ]

        values = np.vstack(rows)
        if values.shape != (n_samples, N_TOF_FEATURES):
            raise TemporalModelError(
                f"recording must have shape ({n_samples}, {N_TOF_FEATURES}), "
                f"got {values.shape}"
            )
        if not np.all(np.isfinite(values)):
            raise TemporalModelError("recording contains non-finite values")

        return TofRecording(
            values=values,
            sample_interval_s=float(sample_interval_s),
            sample_interval_source=str(sample_interval_source),
            mode=mode,
            seed=int(seed),
        )
