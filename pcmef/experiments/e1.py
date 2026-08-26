# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 experiment e1 呼叫；讀 freeze/ 的 e1_candidates、
#         e1_evaluation_design、e1_scientific_rule 三個 lock 與
#         data/splits 的 calibration/heldout 名單；輸入為 adapters 提供的真實
#         四特徵與 surrogate 提供的兩組合成四特徵；
#         輸出 e1_metrics.csv 與 e1_result.json，供 e1_outcome 判定。
# 檔案路徑: pcmef/experiments/e1.py
# 產生時間: 2026-08-27 13:35 +08:00
# 版本: v0.1.0
# 功能說明: 把「校準前」與「校準後」兩組模擬，各自與真實資料比距離，再看校準
#           有沒有真的把距離拉近。距離帶物理單位，因此另外除以一個事先凍結的
#           尺度變成無單位數，跨特徵的彙整只用後者。
# 模組定位: SRC-SAI §11 的執行層。它「不是」判定層 —— PASS/DEGRADED 由
#           experiments.e1_outcome 依 frozen rule 判，本檔只產生數字與證據。
# 主要責任:
#   1. FeatureScales.from_calibration() 由 calibration-only 真實值凍結 s_f
#   2. E1Engine.evaluate() 逐 class×feature 算 raw W1 與 NW，並得出 Delta
#   3. E1Engine._assert_matched_design() 檢查兩候選共用 base scenarios 與 seed
#   4. E1Engine._assert_locks_before_heldout() 確認三個 lock 早於 held-out 開啟
#   5. E1Result.macro_mean_delta() 只對無單位 NW 取巨集平均
#   6. E1Result.to_rows() 產出 e1_metrics.csv 的逐 cell 列
# 維護提醒:
#   - 不得在 e1_scientific_rule.lock 之前開啟 held-out。Appendix H2 規定
#     scale 與 pass/degraded 規則都是 calibration-only 且必須先凍結；
#     順序反過來就是「看過答案才定規則」。
#   - 不得對 raw W1 跨特徵取平均（§11 禁止做法）；跨特徵一律先轉 NW。
#   - 不得在 Initial 與 Calibrated 之間使用不同的 base scenarios 或 seed matrix。
#     Appendix B 的 e1_evaluation_design checker 要求兩者完全相同，
#     否則 Delta 會混入 random realization 的差異。
#   - 不得因為結果不理想就重跑一次 E1 final。held-out 只能開一次
#     （splits.heldout_ids 會記錄取用次數），重跑等於用完了還再用。
#   - v0.1.0 新增：首版 E1 引擎，決策見 NOTE-024。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1/test_e1_engine.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.core.hash import hash_object
from pcmef.core.locks import LockError, LockStore
from pcmef.stats.bootstrap import BootstrapResult, paired_bootstrap_ci
from pcmef.stats.metrics import (
    FeatureScaleError,
    TrendResult,
    feature_scale_iqr,
    mean_sd_error,
    normalized_wasserstein,
    trend_consistency,
    wasserstein_w1,
)

__all__ = [
    "E1Error",
    "E1DesignError",
    "FeatureScales",
    "CandidateObservations",
    "CellMetric",
    "E1Result",
    "E1Engine",
    "HELDOUT_PURPOSE",
]

#: held-out 唯一允許的用途，與 core.splits 的常數相同。
HELDOUT_PURPOSE = "e1_final_evaluation"

#: e1_scientific_rule.lock 必須早於 held-out 開啟的三個前置 lock。
REQUIRED_LOCKS: tuple[str, ...] = (
    "e1_candidates",
    "e1_evaluation_design",
    "e1_scientific_rule",
)


class E1Error(RuntimeError):
    """E1 的前提不成立或輸入不完整。"""


class E1DesignError(E1Error):
    """matched evaluation design 被破壞。

    與一般 E1Error 分開：這代表 Initial 與 Calibrated 不是在同一組隨機實現
    上比較，Delta 因此混入 random realization 的差異 —— 是研究設計事故，
    不是可重試的錯誤。
    """


# ---------------------------------------------------------------------------
# 特徵尺度
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureScales:
    """calibration-only pooled IQR，逐特徵一個 s_f（Appendix I2）。"""

    scales: Mapping[str, float]

    def __post_init__(self) -> None:
        missing = [f for f in TOF_SCHEMA if f not in self.scales]
        if missing:
            raise FeatureScaleError(f"feature scales missing for {missing}")

    def __getitem__(self, feature: str) -> float:
        return float(self.scales[feature])

    @classmethod
    def from_calibration(
        cls, calibration_values: Mapping[str, Sequence[float]]
    ) -> FeatureScales:
        """由 calibration split 的真實值 pooled across classes 算出 s_f。

        pooled 而非逐 class：Appendix I2 明訂「pooled across eligible
        calibration recordings/classes」。逐 class 各自一個尺度會讓
        class 間的 NW 不可比，而 macro 平均正是跨 class 取的。
        """
        return cls(
            scales={
                feature: feature_scale_iqr(calibration_values[feature])
                for feature in TOF_SCHEMA
            }
        )

    def hash(self) -> str:
        return hash_object({k: self.scales[k] for k in sorted(self.scales)})

    def to_artifact(self) -> dict[str, float]:
        return {k: float(v) for k, v in sorted(self.scales.items())}


# ---------------------------------------------------------------------------
# 候選觀測
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateObservations:
    """一個候選（Initial 或 Calibrated）的合成觀測。

    values[class_label][feature] -> 該 class 在該特徵上的合成值序列；
    per_unit[class_label][feature][scenario_id] -> 逐 scenario 的代表值，
    供 paired bootstrap 以 scenario 為單位重抽。
    """

    name: str
    values: Mapping[str, Mapping[str, Sequence[float]]]
    per_unit: Mapping[str, Mapping[str, Mapping[str, float]]]
    base_scenario_ids: tuple[str, ...]
    seed_matrix_hash: str

    def unit_ids(self, class_label: str, feature: str) -> tuple[str, ...]:
        return tuple(sorted(self.per_unit[class_label][feature]))


# ---------------------------------------------------------------------------
# 結果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellMetric:
    """一個 class x feature 格子的完整度量。"""

    class_label: str
    feature: str
    w1_initial: float
    w1_calibrated: float
    scale: float
    nw_initial: float
    nw_calibrated: float
    delta: float
    mean_sd_initial: Mapping[str, float]
    mean_sd_calibrated: Mapping[str, float]

    def to_row(self) -> dict[str, Any]:
        return {
            "class_label": self.class_label,
            "feature": self.feature,
            "w1_initial": self.w1_initial,
            "w1_calibrated": self.w1_calibrated,
            "feature_scale_s_f": self.scale,
            "nw_initial": self.nw_initial,
            "nw_calibrated": self.nw_calibrated,
            "delta_nw": self.delta,
            "mean_error_initial": self.mean_sd_initial["mean_error"],
            "mean_error_calibrated": self.mean_sd_calibrated["mean_error"],
            "sd_error_initial": self.mean_sd_initial["sd_error"],
            "sd_error_calibrated": self.mean_sd_calibrated["sd_error"],
        }


@dataclass(frozen=True)
class E1Result:
    """一次 E1 評估的完整結果。尚未判定 PASS/DEGRADED。"""

    cells: tuple[CellMetric, ...]
    scales: FeatureScales
    bootstrap: BootstrapResult
    trend: TrendResult
    heldout_access_count: int
    design_hash: str
    created_at: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    # -- 彙整 -------------------------------------------------------------

    def macro_mean_delta(self) -> float:
        """對所有 class x feature 的無單位 Delta 取平均。

        只對 NW 取平均：raw W1 帶不同物理單位，平均它們等於把 mm 加上 MCPS
        （§11 明列的禁止做法）。
        """
        return float(np.mean([c.delta for c in self.cells]))

    def per_feature_class_macro_delta(self) -> dict[str, float]:
        """逐特徵、跨 class 取平均，供「每個特徵都不得退步」的規則使用。"""
        return {
            feature: float(
                np.mean([c.delta for c in self.cells if c.feature == feature])
            )
            for feature in TOF_SCHEMA
        }

    def to_rows(self) -> list[dict[str, Any]]:
        return [cell.to_row() for cell in self.cells]

    def to_artifact(self) -> dict[str, Any]:
        return {
            "macro_mean_delta": self.macro_mean_delta(),
            "per_feature_class_macro_delta": self.per_feature_class_macro_delta(),
            "bootstrap": self.bootstrap.to_artifact(),
            "trend": {
                "applicable": self.trend.applicable,
                "consistent": self.trend.consistent,
                "degraded": self.trend.degraded,
                "detail": self.trend.detail,
            },
            "feature_scales": self.scales.to_artifact(),
            "feature_scales_hash": self.scales.hash(),
            "design_hash": self.design_hash,
            "heldout_access_count": self.heldout_access_count,
            "cells": self.to_rows(),
            **dict(self.extra),
        }

    def result_hash(self) -> str:
        return hash_object(self.to_artifact())


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class E1Engine:
    """E1 fidelity 評估。只負責產生數字，不判 PASS/DEGRADED。"""

    def __init__(self, store: LockStore) -> None:
        self.store = store

    # -- 前置檢查 ---------------------------------------------------------

    def assert_ready(self) -> dict[str, str]:
        """三個 lock 必須都已凍結。回傳它們的 payload hash 供結果引用。

        Appendix H2：scale 與 pass/degraded 規則都是 calibration-only 且
        必須在 Held-out 開啟前凍結。這個檢查放在最前面，
        是為了讓「還沒鎖就想跑 final」在碰到 held-out 之前就停下來。
        """
        missing = [name for name in REQUIRED_LOCKS if not self.store.exists(name)]
        if missing:
            raise E1Error(
                f"E1 final evaluation requires these locks first: {missing}. "
                "Appendix H2 forbids opening held-out before the scale and the "
                "pass/degraded rule are frozen — otherwise the rule is chosen "
                "after seeing the answer."
            )
        try:
            return {name: self.store.load_hash(name) for name in REQUIRED_LOCKS}
        except LockError as error:
            raise E1Error(f"a required E1 lock failed its integrity check: {error}")

    @staticmethod
    def _assert_matched_design(
        initial: CandidateObservations, calibrated: CandidateObservations
    ) -> str:
        """Initial 與 Calibrated 必須共用 base scenarios 與 seed matrix。"""
        if initial.base_scenario_ids != calibrated.base_scenario_ids:
            only_initial = set(initial.base_scenario_ids) - set(
                calibrated.base_scenario_ids
            )
            only_calibrated = set(calibrated.base_scenario_ids) - set(
                initial.base_scenario_ids
            )
            raise E1DesignError(
                "Initial and Calibrated must evaluate the same base scenarios "
                f"(only-initial={sorted(only_initial)[:3]}, "
                f"only-calibrated={sorted(only_calibrated)[:3]}). Without common "
                "random numbers the Delta also contains the difference between "
                "two random realizations (SRC-SAI Appendix B)."
            )
        if initial.seed_matrix_hash != calibrated.seed_matrix_hash:
            raise E1DesignError(
                "Initial and Calibrated used different optical/acquisition seed "
                f"matrices ({initial.seed_matrix_hash[:16]} vs "
                f"{calibrated.seed_matrix_hash[:16]}); "
                "e1_evaluation_design requires an identical matrix."
            )
        return hash_object(
            {
                "base_scenario_ids": list(initial.base_scenario_ids),
                "seed_matrix_hash": initial.seed_matrix_hash,
            }
        )

    # -- 評估 -------------------------------------------------------------

    def evaluate(
        self,
        real_heldout: Mapping[str, Mapping[str, Sequence[float]]],
        initial: CandidateObservations,
        calibrated: CandidateObservations,
        scales: FeatureScales,
        replicates: int,
        seed: int,
        confidence_level: float = 0.95,
        trend_real: Mapping[str, float] | None = None,
        trend_initial: Mapping[str, float] | None = None,
        trend_calibrated: Mapping[str, float] | None = None,
        heldout_access_count: int = 1,
    ) -> E1Result:
        """逐 class x feature 算 W1 / NW / Delta，並以成對重抽求 CI。

        real_heldout 必須是**已經透過 heldout_ids(purpose) 取出**的資料；
        本方法不自行開啟 held-out，因此不可能繞過那道用途守門。
        """
        lock_hashes = self.assert_ready()
        design_hash = self._assert_matched_design(initial, calibrated)

        cells: list[CellMetric] = []
        for class_label in CLASS_ORDER:
            if class_label not in real_heldout:
                raise E1Error(
                    f"held-out real data has no class {class_label!r}; "
                    "E1 must evaluate every class or the macro mean is undefined"
                )
            for feature in TOF_SCHEMA:
                real_values = real_heldout[class_label][feature]
                w1_i = wasserstein_w1(real_values, initial.values[class_label][feature])
                w1_c = wasserstein_w1(
                    real_values, calibrated.values[class_label][feature]
                )
                scale = scales[feature]
                nw_i = normalized_wasserstein(w1_i, scale)
                nw_c = normalized_wasserstein(w1_c, scale)
                cells.append(
                    CellMetric(
                        class_label=class_label,
                        feature=feature,
                        w1_initial=w1_i,
                        w1_calibrated=w1_c,
                        scale=scale,
                        nw_initial=nw_i,
                        nw_calibrated=nw_c,
                        # Delta = NW_initial - NW_calibrated：正值代表校準把
                        # 距離拉近了（§11 的定義方向）。
                        delta=nw_i - nw_c,
                        mean_sd_initial=mean_sd_error(
                            real_values, initial.values[class_label][feature]
                        ),
                        mean_sd_calibrated=mean_sd_error(
                            real_values, calibrated.values[class_label][feature]
                        ),
                    )
                )

        bootstrap = self._bootstrap(
            initial, calibrated, scales, replicates, seed, confidence_level
        )
        trend = (
            trend_consistency(trend_real, trend_calibrated)
            if trend_real and trend_calibrated
            else TrendResult(
                applicable=False, consistent=False,
                real_direction=(), synthetic_direction=(),
                real_relative=(), synthetic_relative=(),
                detail="no offset series supplied; trend not applicable",
            )
        )

        return E1Result(
            cells=tuple(cells),
            scales=scales,
            bootstrap=bootstrap,
            trend=trend,
            heldout_access_count=heldout_access_count,
            design_hash=design_hash,
            extra={"lock_hashes": lock_hashes},
        )

    @staticmethod
    def _bootstrap(
        initial: CandidateObservations,
        calibrated: CandidateObservations,
        scales: FeatureScales,
        replicates: int,
        seed: int,
        confidence_level: float,
    ) -> BootstrapResult:
        """以 scenario ID 為單位對 macro-mean Delta 做成對重抽。

        每個 scenario 先在所有 class x feature 上折算成一個無單位的
        cell-wise Delta 平均，再以 scenario 為單位重抽 —— 推論單位因此
        是 scenario，不是 measurement point（SRC-PLAN §1）。
        """
        unit_ids = sorted(
            set(initial.per_unit[CLASS_ORDER[0]][TOF_SCHEMA[0]])
            & set(calibrated.per_unit[CLASS_ORDER[0]][TOF_SCHEMA[0]])
        )
        if len(unit_ids) < 2:
            raise E1Error(
                f"paired bootstrap needs at least 2 scenarios, got {len(unit_ids)}"
            )

        def scenario_mean(candidate: CandidateObservations) -> dict[str, float]:
            per_scenario: dict[str, list[float]] = {i: [] for i in unit_ids}
            for class_label in CLASS_ORDER:
                for feature in TOF_SCHEMA:
                    scale = scales[feature]
                    table = candidate.per_unit[class_label][feature]
                    for identifier in unit_ids:
                        per_scenario[identifier].append(table[identifier] / scale)
            return {i: float(np.mean(v)) for i, v in per_scenario.items()}

        return paired_bootstrap_ci(
            unit_ids=unit_ids,
            initial=scenario_mean(initial),
            calibrated=scenario_mean(calibrated),
            replicates=replicates,
            seed=seed,
            confidence_level=confidence_level,
        )
