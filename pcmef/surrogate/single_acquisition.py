# PC-MEF Research System source maintenance contract
# 上下游: 由 surrogate.temporal_model 與未來的 E1 pipeline 呼叫；
#         輸入 mitransient 產出的 transient 與時間軸、以及校準常數；
#         輸出一筆 float64[4] 觀測，欄序固定為 core.constants.TOF_SCHEMA。
# 檔案路徑: pcmef/surrogate/single_acquisition.py
# 產生時間: 2026-08-26 12:45 +08:00
# 版本: v0.1.0
# 功能說明: 把一次光傳播模擬變成感測器讀到的一筆四欄數值
#           （距離、環境光速率、訊號速率、不確定度）。
# 模組定位: SRC-SAI §10 的核心研究元件。一次 optical transient 對應
#           **恰好一筆** observation —— 不是 500 筆。
# 主要責任:
#   1. SensorSurrogate.map_single_acquisition() 產生一筆 [4] 觀測
#   2. 四個輸出各自由獨立模組映射，避免任一欄由另一欄推導
#   3. 依 TOF_SCHEMA 順序組裝，不依模組宣告順序
# 維護提醒:
#   - 不得把 transient bins 直接切成 500 個 measurement points；
#     SRC-SAI §10 明列 `tof_recording = transient_bins[:500]` 為禁止做法。
#     一次 acquisition 只產生一筆觀測，500 筆需要 500 次 realization
#     或經校準的 measurement-time 隨機模型（見 temporal_model）。
#   - 不得改變組裝順序；欄序必須經 TOF_SCHEMA 取得而非硬寫（NOTE-001）。
#   - 不得在 formal 模式使用含 placeholder 的校準；本類建構時即檢查。
#   - v0.1.0 新增：首版 single-acquisition surrogate。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -k "single or acquisition or order" -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from pcmef.core.constants import N_TOF_FEATURES, TOF_SCHEMA
from pcmef.surrogate.ambient import map_ambient_rate
from pcmef.surrogate.calibration import SurrogateCalibration
from pcmef.surrogate.distance import (
    SELECTED_ESTIMATOR,
    DistanceEstimator,
    map_distance,
)
from pcmef.surrogate.features import TransientObservables, extract_observables
from pcmef.surrogate.signal_rate import map_signal_rate
from pcmef.surrogate.sigma import map_sigma_like

__all__ = ["SensorSurrogate"]


@dataclass(frozen=True)
class SensorSurrogate:
    """VL53L0X-like 感測器替身。"""

    calibration: SurrogateCalibration
    # NOTE(NOTE-037): 預設值由預註冊選定程序決定，不是實作偏好。
    estimator: DistanceEstimator = SELECTED_ESTIMATOR
    formal: bool = False

    def __post_init__(self) -> None:
        if self.formal:
            self.calibration.assert_formal_ready()

    def observe(
        self,
        transient: np.ndarray,
        time_axis_s: np.ndarray,
        ambient_transient: np.ndarray | None = None,
    ) -> TransientObservables:
        """只抽物理量，不做校準映射。供診斷與 calibration 使用。

        `transient` 為 active pass、`ambient_transient` 為 ambient pass
        （NOTE-034）。
        """
        return extract_observables(
            transient,
            time_axis_s,
            main_window_halfwidth_bins=int(
                self.calibration.analysis.get("main_window_halfwidth_bins", 8)
            ),
            ambient_transient=ambient_transient,
        )

    def map_single_acquisition(
        self,
        transient: np.ndarray,
        time_axis_s: np.ndarray,
        rng: np.random.Generator,
        ambient_transient: np.ndarray | None = None,
    ) -> np.ndarray:
        """一次 optical transient -> 恰好一筆 [Distance, Ambient, Signal, Sigma-like]。

        四個欄位各自由獨立模組從物理量映射而來，彼此不互相推導 ——
        SRC-SAI §10 明令禁止「只用 Distance 推算 Signal」這類做法，
        因為那會讓兩個模態的分歧度 D 被人為壓低。

        本方法把「決定性抽取」與「隨機映射」串在一起，是**參考路徑**：
        它一次只處理一筆，因此同一份 transient 重複呼叫 N 次就會重跑 N 次
        決定性抽取。批次路徑見 `map_from_observables()`。
        """
        observables = self.observe(transient, time_axis_s, ambient_transient)
        return self.map_from_observables(observables, rng)

    def map_from_observables(
        self,
        observables: TransientObservables,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """已抽好的物理量 -> 一筆 [Distance, Ambient, Signal, Sigma-like]。

        **這是唯一消耗亂數的地方，且抽取順序就是下面 dict 的字面順序：**
        distance -> ambient -> signal -> sigma。Python 依字面順序求值 dict 的
        value，因此這個順序是語意的一部分，不得重排 —— 重排會讓同一顆種子
        產生不同的序列，而 CRN 正是靠這個順序成立的。

        把它與 `observe()` 分開的唯一理由是效能：`extract_observables()` 是
        **決定性**的（不碰 rng），同一組 (active, ambient, axis) 重算 500 次
        會得到完全相同的結果，卻要把整個 (H,W,bins,C) cube 折疊 500 次。
        分開之後可以折一次、映射 500 次，數學上完全等價。
        """
        mapped: dict[str, float] = {
            "distance_mm": map_distance(
                observables, self.calibration, rng, self.estimator
            ),
            "ambient_rate_mcps": map_ambient_rate(observables, self.calibration, rng),
            "signal_rate_mcps": map_signal_rate(observables, self.calibration, rng),
            "sigma_like": map_sigma_like(observables, self.calibration, rng),
        }

        # 依 canonical 順序組裝，不依上面 dict 的宣告順序（NOTE-001）。
        vector = np.asarray(
            [mapped[name] for name in TOF_SCHEMA], dtype=np.float64
        )
        if vector.shape != (N_TOF_FEATURES,):
            raise ValueError(
                f"a single acquisition must produce exactly {N_TOF_FEATURES} values, "
                f"got shape {vector.shape}"
            )
        if not np.all(np.isfinite(vector)):
            raise ValueError(f"single acquisition produced non-finite values: {vector}")
        return vector

    def provenance(self) -> dict[str, Any]:
        """供 surrogate freeze manifest 引用。"""
        return {
            "estimator": self.estimator.value,
            "tof_schema": list(TOF_SCHEMA),
            "calibration": self.calibration.to_dict(),
            "formal": self.formal,
        }
