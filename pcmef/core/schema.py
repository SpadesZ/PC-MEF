# PC-MEF Research System source maintenance contract
# 上下游: 由 adapters.legacy_csv、adapters.real_vl53l0x、adapters.simulation 建構，
#         讀取來源檔案後轉成本結構；供 splits、experiments、registry 與 evaluator
#         使用；經 core.inference_payload 去除 truth 後才會流向 provider。
# 檔案路徑: pcmef/core/schema.py
# 產生時間: 2026-08-25 20:50 +08:00
# 版本: v0.1.0
# 功能說明: 定義一筆「案例」在系統裡長什麼樣子 —— 不論它來自前研究的 CSV、
#           實體 VL53L0X 感測器還是 Mitsuba 模擬，最後都收斂成同一個 CanonicalCase，
#           並在建構當下就檢查 500x4 形狀、四特徵順序、雙時間軸與五層計數帳。
# 模組定位: 三種資料來源共同的收斂契約。它可以帶 ground truth（供 evaluator 使用），
#           但它「不是」可以送給 provider 的東西 —— 那必須先轉成 InferencePayload。
# 主要責任:
#   1. SourceRole / SplitRole / ClassLabel / TruthVisibility 四個列舉定義角色空間
#   2. MeasurementTime 保存 measurement-time 軸，與 optical transient 時間軸分離
#   3. CountStatus 保存 nominal/physical/canonical/valid/e1_eligible 五層計數
#   4. CanonicalCase._check_tof() 驗證形狀、非有限值與取樣點數一致性
#   5. CanonicalCase._check_source_role_requirements() 依來源角色檢查必要 artifact
#   6. CanonicalCase.to_json() 產生可做 canonical hashing 的 dict
# 維護提醒:
#   - 不得把 CanonicalCase 整份傳給 provider；轉換一律經由 core.inference_payload。
#   - 不得用補零或補平均值處理缺漏的 ToF 欄位；缺值必須標為 invalid 進 exclusion ledger。
#   - 不得 hard-code usable N=560；那是 nominal logical count，實際可用數必須由
#     M0 盤點產生的 count_status 決定。
#   - 新增欄位時必須同步決定它屬於 evaluator-only 還是 inference-visible，
#     預設一律 evaluator-only。
#   - v0.1.0 新增：首版 CanonicalCase，涵蓋 SRC-D01..D05 五個 provenance 雷點。
# 驗證方式:
#   - py -3.10 -m pytest tests/schema/test_canonical_case_and_ids.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from pcmef.core.constants import (
    CLASS_ORDER,
    SPLIT_ROLES,
    TOF_RECORDING_SHAPE,
    TOF_SCHEMA,
)

__all__ = [
    "SchemaViolation",
    "SourceRole",
    "SplitRole",
    "ClassLabel",
    "TruthVisibility",
    "MeasurementTime",
    "CountStatus",
    "CanonicalCase",
]


class SchemaViolation(ValueError):
    """CanonicalCase 或其子結構違反契約。一律 fail-fast，不做欄位補值。"""


# ---------------------------------------------------------------------------
# 列舉
# ---------------------------------------------------------------------------


class SourceRole(str, Enum):
    """資料來源角色（SRC-SAI §6）。禁止只看檔案路徑推定。"""

    REAL_ANCHOR = "real_anchor"
    SYNTHETIC = "synthetic"
    REAL_LIVE = "real_live"


class SplitRole(str, Enum):
    """七類資料角色（SRC-PLAN §3.1、SRC-SAI FR-007）。由 SplitRegistry 控制。"""

    CALIBRATION = "calibration"
    PERCEPTION_TRAIN = "perception_train"
    MODEL_GATE_VALIDATION = "model_gate_validation"
    E2_PILOT = "e2_pilot"
    HELDOUT_REAL = "heldout_real"
    FORMAL_E2 = "formal_e2"
    EXTENSION = "extension"


class ClassLabel(str, Enum):
    """四類固定 label space。順序意義由 constants.CLASS_ORDER 決定。"""

    EMPTY = "Empty"
    WATER_FILLED = "Water-filled"
    BUBBLY = "Bubbly"
    MISTY = "Misty"


class TruthVisibility(str, Enum):
    """本 case 的 truth 是否允許被下游看見。

    CanonicalCase 可以帶 truth，但 InferencePayload 必須 truth-free
    （SRC-SAI §6 truth_visibility 欄位說明）。這個欄位讓 evaluator 與
    inference orchestrator 之間的邊界成為顯式狀態，而不是靠呼叫端記得。
    """

    EVALUATOR_ONLY = "evaluator_only"
    TRUTH_FREE = "truth_free"


# 內部一致性檢查：列舉與凍結常數必須完全對齊，否則其中一份被改動時會靜默分歧。
if tuple(role.value for role in SplitRole) != SPLIT_ROLES:
    raise SchemaViolation("SplitRole enum diverged from constants.SPLIT_ROLES")
if tuple(label.value for label in ClassLabel) != CLASS_ORDER:
    raise SchemaViolation("ClassLabel enum diverged from constants.CLASS_ORDER")


# ---------------------------------------------------------------------------
# 子結構
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementTime:
    """500 點 recording 的 measurement-time 軸（SRC-SAI §6）。

    這是與 optical_transient_time 完全分離的第二條時間軸（SRC-D03）。
    optical transient 描述單次 acquisition 內部的光子飛行時間，
    measurement time 描述連續 acquisition 的取樣時間軸。
    兩者不得互相換算或彼此頂替。
    """

    sample_interval_s: float
    n_samples: int
    source: str

    def __post_init__(self) -> None:
        if not np.isfinite(self.sample_interval_s) or self.sample_interval_s <= 0:
            raise SchemaViolation(
                f"sample_interval_s must be finite and positive, "
                f"got {self.sample_interval_s!r}"
            )
        if self.n_samples <= 0:
            raise SchemaViolation(f"n_samples must be positive, got {self.n_samples!r}")
        if not self.source:
            raise SchemaViolation(
                "measurement_time.source is required; real interval must come from "
                "recording provenance and synthetic interval from the frozen "
                "measurement-time config"
            )


@dataclass(frozen=True)
class CountStatus:
    """real 資料的五層計數帳（SRC-PLAN §3.1、SRC-SAI §7）。

    五個數字必須分開回報。nominal logical count（4x140=560）只是計算上的數字，
    不等於 final usable N；SRC-SAI §7.9 audit rule 明文禁止 hard-code usable N=560。
    """

    nominal_logical_recordings: int
    physical_source_files: int
    canonical_recordings: int
    valid_recordings: int
    e1_eligible_recordings: int

    def __post_init__(self) -> None:
        for name in (
            "nominal_logical_recordings",
            "physical_source_files",
            "canonical_recordings",
            "valid_recordings",
            "e1_eligible_recordings",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SchemaViolation(
                    f"count_status.{name} must be a non-negative int, got {value!r}"
                )
        # 單調收斂關係：canonical >= valid >= e1_eligible。
        # 不檢查 nominal 與 physical 的大小關係 —— legacy handoff 可能以四個 metric
        # 分檔保存，physical count 因此可以大於 logical count（SRC-PLAN §2.1 M0 rule）。
        if not (
            self.canonical_recordings
            >= self.valid_recordings
            >= self.e1_eligible_recordings
        ):
            raise SchemaViolation(
                "count_status must satisfy canonical >= valid >= e1_eligible, got "
                f"{self.canonical_recordings} / {self.valid_recordings} / "
                f"{self.e1_eligible_recordings}"
            )


# ---------------------------------------------------------------------------
# CanonicalCase
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalCase:
    """三種 EvidenceSource adapter 收斂後的統一 case 契約（SRC-SAI §6、Appendix A1）。

    重要邊界：本結構帶有 class_label 等 evaluator-only 欄位，
    **永遠不得整份傳給 provider**。要送進 Multi-Agent 之前必須經由
    core.inference_payload.to_inference_payload() 轉換成 truth-free 的
    InferencePayload（SRC-SAI Appendix G2 / I1）。
    """

    case_id: str
    source_role: SourceRole
    split_role: SplitRole
    class_label: ClassLabel
    truth_visibility: TruthVisibility
    provenance: dict[str, Any]

    rgb_path: str | None = None
    tof_sequence: np.ndarray | None = None
    tof_sequence_path: str | None = None
    tof_summary: dict[str, Any] | None = None
    tof_summary_path: str | None = None
    measurement_time: MeasurementTime | None = None
    optical_transient_path: str | None = None
    optical_transient_time_axis_path: str | None = None
    scenario_params: dict[str, Any] | None = None
    count_status: CountStatus | None = None
    tof_schema: tuple[str, ...] = field(default=TOF_SCHEMA)

    def __post_init__(self) -> None:
        if not self.case_id:
            raise SchemaViolation("case_id is required")
        if not isinstance(self.provenance, dict) or not self.provenance:
            raise SchemaViolation(
                "provenance is required and must record software/version/source "
                "filenames/hash/timing/register notes"
            )
        if tuple(self.tof_schema) != TOF_SCHEMA:
            raise SchemaViolation(
                f"tof_schema must equal the canonical order {TOF_SCHEMA}, "
                f"got {tuple(self.tof_schema)}"
            )

        self._check_tof()
        self._check_modality_presence()
        self._check_source_role_requirements()

    # -- 欄位檢查 ---------------------------------------------------------

    def _check_tof(self) -> None:
        if self.tof_sequence is None:
            return
        array = self.tof_sequence
        if not isinstance(array, np.ndarray):
            raise SchemaViolation(
                f"tof_sequence must be an ndarray, got {type(array).__name__}"
            )
        if array.shape != TOF_RECORDING_SHAPE:
            raise SchemaViolation(
                f"tof_sequence must have shape {TOF_RECORDING_SHAPE}, got {array.shape}"
            )
        if not np.all(np.isfinite(array)):
            raise SchemaViolation(
                "tof_sequence contains NaN or Inf; missing cells must be marked "
                "invalid in the exclusion ledger, never zero-filled"
            )
        if self.measurement_time is None:
            raise SchemaViolation(
                "measurement_time is required whenever tof_sequence is present"
            )
        if self.measurement_time.n_samples != array.shape[0]:
            raise SchemaViolation(
                f"measurement_time.n_samples ({self.measurement_time.n_samples}) "
                f"must match tof_sequence rows ({array.shape[0]})"
            )

    def _check_modality_presence(self) -> None:
        has_tof = self.tof_sequence is not None or self.tof_sequence_path is not None
        has_rgb = self.rgb_path is not None
        if not has_tof and not has_rgb:
            raise SchemaViolation(
                f"case {self.case_id} carries neither ToF nor RGB evidence"
            )

    def _check_source_role_requirements(self) -> None:
        if self.source_role is SourceRole.SYNTHETIC:
            if self.optical_transient_path is None:
                raise SchemaViolation(
                    "synthetic cases require optical_transient_path; the surrogate "
                    "must map one optical transient to one sensor observation"
                )
            if self.optical_transient_time_axis_path is None:
                raise SchemaViolation(
                    "synthetic cases require optical_transient_time_axis_path so the "
                    "transient time axis stays separable from measurement time"
                )
            if not self.scenario_params:
                raise SchemaViolation(
                    "synthetic cases require scenario_params "
                    "(geometry/medium/light/degradation/seed)"
                )
        if self.source_role is SourceRole.REAL_ANCHOR and self.count_status is None:
            raise SchemaViolation(
                "real cases require count_status; usable N must be traceable to the "
                "M0 inventory audit and must never be hard-coded"
            )

    # -- 匯出 -------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """轉成可 canonical-hash 的 dict。

        tof_sequence 這類大型 ndarray 不進 JSON，只留路徑；
        內容摘要由 core.hash.hash_array 另行計算後寫入 provenance。
        """
        payload = asdict(self)
        payload.pop("tof_sequence", None)
        payload["source_role"] = self.source_role.value
        payload["split_role"] = self.split_role.value
        payload["class_label"] = self.class_label.value
        payload["truth_visibility"] = self.truth_visibility.value
        payload["tof_schema"] = list(self.tof_schema)
        return payload
