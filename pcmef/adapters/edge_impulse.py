# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 audit real-data --source-format edge-impulse 建構；
#         只讀 data/raw_real/edge_impulse_export/ 下的 JSON；
#         產出 CanonicalCase 與 AlignmentReport，供 splits 與 E1 使用。
# 檔案路徑: pcmef/adapters/edge_impulse.py
# 產生時間: 2026-08-26 15:20 +08:00
# 版本: v0.1.0
# 功能說明: 讀取 Edge Impulse 官方 dataset export，把每個 JSON 樣本還原成
#           一筆 500x4 的原始量測序列，並帶出它自己的取樣間隔與 train/test 標記。
# 模組定位: 第三個 EvidenceSource reader，與 legacy_csv（逐 metric 分檔）、
#           legacy_kg（窗口統計量）並列。它是目前**唯一**能滿足
#           CanonicalCase (500,4) 契約的真實資料來源。
# 主要責任:
#   1. EdgeImpulseSample 解析單一 JSON 的 payload 與檔名中的 class/measurement
#   2. EdgeImpulseAdapter.build_inventory() 掃描 training/ 與 testing/
#   3. EdgeImpulseAdapter.audit_alignment() 驗證形狀、欄序、間隔一致性並產五層計數
#   4. EdgeImpulseAdapter.load_recording() 組出含 MetricAlignment 的 CanonicalCase
# 維護提醒:
#   - 不得把 sensors 欄位的順序當成理所當然而略過檢查；本 reader 每一筆都比對
#     TOF_SCHEMA，因為欄序錯置是 SRC-D04 的核心風險且不會有任何症狀。
#   - 不得以 payload 以外的來源推定取樣間隔；每個 JSON 自帶 interval_ms，
#     那就是該筆的 provenance（NOTE-011：資料集內確實存在兩種取樣率）。
#   - 不得因為這是「官方 export」就跳過結構驗證；它是 ingest 後再匯出的產物，
#     不是硬碟上 byte-for-byte 的原始 CSV，兩者不可混為一談。
#   - v0.1.0 新增：首版 Edge Impulse export reader。
# 驗證方式:
#   - py -3.10 -m pytest tests/adapter/test_edge_impulse.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np

from pcmef.adapters.base import (
    AlignedMeasurement,
    AlignmentReport,
    ExclusionEntry,
    ExclusionReason,
    SourceFile,
)
from pcmef.core.constants import (
    LEGACY_CSV_COLUMN_TITLES,
    SIGMA_STATUS_RESOLVED,
    TOF_RECORDING_POINTS,
    TOF_SCHEMA,
)
from pcmef.core.hash import hash_file
from pcmef.core.ids import real_recording_id
from pcmef.core.schema import (
    CanonicalCase,
    ClassLabel,
    CountStatus,
    MeasurementTime,
    MetricAlignment,
    SourceRole,
    SplitRole,
    TruthVisibility,
)

__all__ = [
    "EdgeImpulseError",
    "EI_LABEL_TO_CLASS",
    "EdgeImpulseSample",
    "EdgeImpulseAdapter",
]


class EdgeImpulseError(RuntimeError):
    """匯出目錄結構不符，或找不到任何可解析的樣本。"""


# Edge Impulse 的 label -> 本研究 canonical class。
# 與 LEGACY_LABEL_MAP 不同：EI 用的是英文 label 的小寫連字號形式。
EI_LABEL_TO_CLASS = MappingProxyType(
    {
        "empty": "Empty",
        "water-filled": "Water-filled",
        "bubbly": "Bubbly",
        "misty": "Misty",
    }
)

# 檔名形如 bubbly.measurement_42.csv.<id>.ingestion-<pod>.json
_NAME_PATTERN = re.compile(r"^(?P<label>[a-z\-]+)\.measurement_(?P<num>\d+)\.csv\.")

_SPLIT_DIRS = ("training", "testing")


@dataclass(frozen=True)
class EdgeImpulseSample:
    """單一 Edge Impulse 樣本。"""

    original_path: str
    split: str
    label: str
    class_label: str
    measurement: int
    interval_ms: float
    sensor_names: tuple[str, ...]
    values: np.ndarray
    sha256: str

    @property
    def sample_interval_s(self) -> float:
        return self.interval_ms / 1000.0


class EdgeImpulseAdapter:
    """Edge Impulse dataset export 的 EvidenceSource 實作。"""

    def __init__(self, sigma_status: str = "UNRESOLVED") -> None:
        self.sigma_status = sigma_status
        self._source_root: Path | None = None
        self._samples: dict[str, EdgeImpulseSample] = {}

    # -- 盤點 --------------------------------------------------------------

    def build_inventory(self, source_root: str | Path) -> tuple[SourceFile, ...]:
        """掃描 training/ 與 testing/，解析每個 JSON 樣本。"""
        root = Path(source_root)
        if not root.is_dir():
            raise EdgeImpulseError(f"Edge Impulse export root not found: {root}")
        self._source_root = root
        self._samples = {}

        files: list[SourceFile] = []
        for split in _SPLIT_DIRS:
            split_dir = root / split
            if not split_dir.is_dir():
                continue
            for path in sorted(split_dir.glob("*.json")):
                files.append(self._describe(root, path, split))
        if not files:
            raise EdgeImpulseError(
                f"no sample JSON found under {root}; expected training/ and testing/ "
                "subdirectories from an Edge Impulse dataset export"
            )
        return tuple(files)

    def _describe(self, root: Path, path: Path, split: str) -> SourceFile:
        relative = path.relative_to(root).as_posix()
        match = _NAME_PATTERN.match(path.name)
        label = match.group("label") if match else ""
        measurement = int(match.group("num")) if match else -1

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
            values = np.asarray(payload["values"], dtype=np.float64)
            names = tuple(str(s["name"]) for s in payload["sensors"])
            interval_ms = float(payload["interval_ms"])
        except Exception as error:  # noqa: BLE001
            return SourceFile(
                original_path=relative,
                condition=label,
                metric="",
                metric_folder=split,
                measurement_key=str(measurement) if measurement >= 0 else "",
                natural_sort_index=measurement,
                row_count=-1,
                data_column_index=-1,
                sha256=f"unreadable:{type(error).__name__}",
            )

        if label in EI_LABEL_TO_CLASS and measurement >= 0:
            self._samples[relative] = EdgeImpulseSample(
                original_path=relative,
                split=split,
                label=label,
                class_label=EI_LABEL_TO_CLASS[label],
                measurement=measurement,
                interval_ms=interval_ms,
                sensor_names=names,
                values=values,
                sha256=hash_file(path),
            )
        return SourceFile(
            original_path=relative,
            condition=label,
            # 一個檔案同時帶四個 metric，因此 metric 欄留空並在 alignment 階段展開。
            metric="",
            metric_folder=split,
            measurement_key=str(measurement) if measurement >= 0 else "",
            natural_sort_index=measurement,
            row_count=int(values.shape[0]) if values.ndim == 2 else -1,
            data_column_index=0,
            sha256=hash_file(path),
        )

    # -- 對齊稽核 ----------------------------------------------------------

    def audit_alignment(self, inventory: tuple[SourceFile, ...]) -> AlignmentReport:
        """驗證每筆樣本的形狀、欄序與取樣間隔，並產出五層計數。

        本來源不存在 legacy_csv 的位置對齊風險：四個 metric 在同一個 JSON 內，
        由 sensors 欄位顯式標名。但欄序仍必須逐筆比對 —— 欄序錯置沒有任何症狀。
        """
        exclusions: list[ExclusionEntry] = []
        aligned: list[AlignedMeasurement] = []
        canonical = 0
        intervals: set[float] = set()
        seen: set[tuple[str, int]] = set()

        for source in inventory:
            sample = self._samples.get(source.original_path)
            if sample is None:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=source.original_path,
                        reason=(
                            ExclusionReason.UNREADABLE_FILE
                            if source.row_count < 0
                            else ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY
                        ),
                        detail={"condition": source.condition},
                    )
                )
                continue

            identity = (sample.label, sample.measurement)
            if identity in seen:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=sample.original_path,
                        reason=ExclusionReason.DUPLICATE_MEASUREMENT_KEY,
                        detail={"label": sample.label, "measurement": sample.measurement},
                    )
                )
                continue
            seen.add(identity)
            canonical += 1

            if sample.sensor_names != LEGACY_CSV_COLUMN_TITLES:
                exclusions.append(
                    ExclusionEntry(
                        scope="measurement",
                        identifier=f"{sample.label}/{sample.measurement}",
                        reason=ExclusionReason.MISSING_METRIC,
                        detail={
                            "expected_order": list(LEGACY_CSV_COLUMN_TITLES),
                            "actual_order": list(sample.sensor_names),
                        },
                    )
                )
                continue

            if sample.values.shape != (TOF_RECORDING_POINTS, len(TOF_SCHEMA)):
                exclusions.append(
                    ExclusionEntry(
                        scope="measurement",
                        identifier=f"{sample.label}/{sample.measurement}",
                        reason=ExclusionReason.ROW_COUNT_NOT_EXPECTED,
                        detail={
                            "shape": list(sample.values.shape),
                            "expected": [TOF_RECORDING_POINTS, len(TOF_SCHEMA)],
                        },
                    )
                )
                continue

            if not np.all(np.isfinite(sample.values)):
                exclusions.append(
                    ExclusionEntry(
                        scope="measurement",
                        identifier=f"{sample.label}/{sample.measurement}",
                        reason=ExclusionReason.NON_FINITE_VALUE,
                        detail={},
                    )
                )
                continue

            intervals.add(sample.interval_ms)
            aligned.append(
                AlignedMeasurement(
                    condition=sample.label,
                    class_label=sample.class_label,
                    measurement_key=str(sample.measurement),
                    files={
                        metric: SourceFile(
                            original_path=sample.original_path,
                            condition=sample.label,
                            metric=metric,
                            metric_folder=sample.split,
                            measurement_key=str(sample.measurement),
                            natural_sort_index=sample.measurement,
                            row_count=int(sample.values.shape[0]),
                            data_column_index=index,
                            sha256=sample.sha256,
                        )
                        for index, metric in enumerate(TOF_SCHEMA)
                    },
                )
            )

        e1_eligible = (
            len(aligned) if self.sigma_status == SIGMA_STATUS_RESOLVED else 0
        )
        if self.sigma_status != SIGMA_STATUS_RESOLVED and aligned:
            exclusions.append(
                ExclusionEntry(
                    scope="dataset",
                    identifier="sigma_provenance",
                    reason=ExclusionReason.SIGMA_PROVENANCE_UNRESOLVED,
                    detail={
                        "status": self.sigma_status,
                        "affected_recordings": len(aligned),
                        "gate": "E1-G08",
                    },
                )
            )

        counts = CountStatus(
            nominal_logical_recordings=0,
            physical_source_files=len(inventory),
            canonical_recordings=canonical,
            valid_recordings=len(aligned),
            e1_eligible_recordings=e1_eligible,
        )
        return AlignmentReport(
            source_root=str(self._source_root or ""),
            key_strategy=f"edge_impulse_export (intervals={sorted(intervals)})",
            aligned=tuple(aligned),
            exclusions=tuple(exclusions),
            counts=counts,
            nominal_source="edge_impulse_dataset_export",
        )

    # -- 載入 --------------------------------------------------------------

    def load_recording(
        self,
        measurement: AlignedMeasurement,
        *,
        counts: CountStatus,
        split_role: SplitRole,
        serial: int,
    ) -> CanonicalCase:
        """把一筆樣本載入成滿足 (500,4) 契約的 CanonicalCase。"""
        reference = measurement.files[TOF_SCHEMA[0]]
        sample = self._samples[reference.original_path]

        return CanonicalCase(
            case_id=real_recording_id(measurement.class_label, serial),
            source_role=SourceRole.REAL_ANCHOR,
            split_role=split_role,
            class_label=ClassLabel(measurement.class_label),
            truth_visibility=TruthVisibility.EVALUATOR_ONLY,
            provenance={
                "source_format": "edge_impulse_dataset_export",
                "edge_impulse_split": sample.split,
                "edge_impulse_label": sample.label,
                "measurement": sample.measurement,
                "sigma_status": self.sigma_status,
                "sample_interval_source": "edge_impulse_export:interval_ms",
                "sensor_names": list(sample.sensor_names),
            },
            tof_sequence=sample.values,
            measurement_time=MeasurementTime(
                sample_interval_s=sample.sample_interval_s,
                n_samples=int(sample.values.shape[0]),
                source="edge_impulse_export:interval_ms",
            ),
            count_status=counts,
            metric_alignment=tuple(
                MetricAlignment(
                    metric=metric,
                    source_filename=sample.original_path,
                    row_count=int(sample.values.shape[0]),
                    source_sha256=sample.sha256,
                )
                for metric in TOF_SCHEMA
            ),
        )
