# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 audit real-data 指令建構；只讀 data/raw_real/ 下的原始 CSV，
#         產出 SourceFile/AlignmentReport 與 CanonicalCase，
#         盤點結果寫入 data/inventory/ 供 splits 與 E1 使用。
# 檔案路徑: pcmef/adapters/legacy_csv.py
# 產生時間: 2026-08-26 00:45 +08:00
# 版本: v0.1.0
# 功能說明: 把前研究留下的 CSV 轉成系統統一的案例格式。它掃描四個 metric 資料夾，
#           依「檔名中的測量編號」而非檔案排序位置把四個特徵配對起來，
#           檢查長度、缺漏與非有限值，並從時間欄推導每筆的實際取樣間隔。
# 模組定位: 三種 EvidenceSource 之一，負責 legacy 來源。它只讀不寫來源檔，
#           也不做任何補值或裁切 —— 有問題的資料一律進 exclusion ledger。
# 主要責任:
#   1. build_inventory() 掃描 <condition>/<metric_folder>/*.csv 並計算每檔 provenance
#   2. _extract_measurement_key() 依設定策略從檔名取出測量編號
#   3. audit_alignment() 以測量編號分組配對四個 metric，產出 AlignmentReport
#   4. _derive_sample_interval() 從時間欄推導實際間隔，失敗即記為 timing provenance 缺失
#   5. load_recording() 組出 (500,4) 陣列與含 MetricAlignment 的 CanonicalCase
# 維護提醒:
#   - 不得改回依排序位置配對四個 metric；那是 legacy 合併程式的做法，
#     任一資料夾多或少一個檔就會讓該點之後全部靜默錯位（NOTE-010）。
#   - 不得靜默補零、補平均或裁切到最短長度；四檔長度不一致一律進 exclusion ledger。
#   - 不得硬編 usable N=560；nominal 只是設定裡的名目值，實際數量由本模組盤點產生。
#   - 不得猜測 Sigma 尺度；sigma_status 由呼叫端傳入，未解析時只影響 e1_eligible 計數。
#   - v0.1.0 新增：首版 legacy adapter，對應 SRC-SAI 7 節與 8 節、NOTE-010。
# 驗證方式:
#   - py -3.10 -m pytest tests/adapter/test_legacy_csv.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from pcmef.adapters.base import (
    AlignedMeasurement,
    AlignmentReport,
    ExclusionEntry,
    ExclusionReason,
    SourceFile,
)
from pcmef.core.constants import (
    LEGACY_LABEL_MAP,
    LEGACY_METRIC_FOLDERS,
    SIGMA_STATUS_RESOLVED,
    SIGMA_STATUS_VALUES,
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
    SchemaViolation,
    SourceRole,
    SplitRole,
    TruthVisibility,
)

__all__ = ["LegacyCSVError", "LegacyCSVConfig", "LegacyCSVAdapter", "KEY_STRATEGIES"]


class LegacyCSVError(RuntimeError):
    """來源目錄結構、設定或對齊策略無法成立。"""


# metric folder -> canonical metric 名稱。順序即 TOF_SCHEMA 順序。
_FOLDER_TO_METRIC = dict(zip(LEGACY_METRIC_FOLDERS, TOF_SCHEMA))

KEY_STRATEGIES: tuple[str, ...] = (
    "trailing_integer",
    "full_stem",
    "stem_without_metric",
)


@dataclass(frozen=True)
class LegacyCSVConfig:
    """LegacyCSVAdapter 的凍結設定。"""

    key_strategy: str = "trailing_integer"
    expected_row_count: int = TOF_RECORDING_POINTS
    sigma_status: str = "UNRESOLVED"
    nominal_logical_recordings: int = 0
    nominal_source: str = "config"
    formal: bool = False
    # 時間欄推導出的間隔容許的相對離散度。超過即視為時間軸不穩定，
    # 不能宣稱該筆有可信的 measurement-time provenance。
    max_interval_cv: float = 0.25
    label_map: dict[str, str] = field(default_factory=lambda: dict(LEGACY_LABEL_MAP))

    def __post_init__(self) -> None:
        if self.key_strategy not in KEY_STRATEGIES:
            raise LegacyCSVError(
                f"unknown key_strategy {self.key_strategy!r}; expected one of "
                f"{KEY_STRATEGIES}"
            )
        if self.sigma_status not in SIGMA_STATUS_VALUES:
            raise LegacyCSVError(
                f"sigma_status must be one of {SIGMA_STATUS_VALUES}, "
                f"got {self.sigma_status!r}"
            )
        if self.expected_row_count <= 0:
            raise LegacyCSVError("expected_row_count must be positive")


def _natural_sort_index(name: str) -> int:
    """回傳檔名中最後一個整數，供 natural sort 對照使用。

    只用於重現 legacy 的排序順序以便交叉比對，**不**用於對齊本身。
    """
    digits = re.findall(r"\d+", name)
    return int(digits[-1]) if digits else -1


class LegacyCSVAdapter:
    """前研究 CSV 的 EvidenceSource 實作。"""

    def __init__(self, config: LegacyCSVConfig | None = None) -> None:
        self.config = config or LegacyCSVConfig()
        self._source_root: Path | None = None

    # -- 測量編號抽取 ------------------------------------------------------

    def _extract_measurement_key(self, stem: str, metric_folder: str) -> str | None:
        """從檔名取出測量編號。取不到回傳 None，由呼叫端記為排除。

        這是取代 legacy 位置配對的關鍵：只要四個資料夾的檔名都帶得出同一組編號，
        配對就與檔案數量、排序結果、是否有多餘暫存檔完全無關。
        """
        strategy = self.config.key_strategy
        if strategy == "full_stem":
            return stem or None
        if strategy == "stem_without_metric":
            cleaned = re.sub(
                rf"{re.escape(metric_folder)}|{re.escape(_FOLDER_TO_METRIC[metric_folder])}",
                "",
                stem,
                flags=re.IGNORECASE,
            )
            cleaned = cleaned.strip("_- .")
            return cleaned or None
        digits = re.findall(r"\d+", stem)
        return str(int(digits[-1])) if digits else None

    # -- 盤點 --------------------------------------------------------------

    def build_inventory(self, source_root: str | Path) -> tuple[SourceFile, ...]:
        """掃描來源目錄，對每個 CSV 計算 provenance。來源檔全程唯讀。"""
        root = Path(source_root)
        if not root.is_dir():
            raise LegacyCSVError(f"source root not found: {root}")
        self._source_root = root

        files: list[SourceFile] = []
        for condition in sorted(self.config.label_map):
            condition_dir = root / condition
            if not condition_dir.is_dir():
                continue
            for metric_folder in LEGACY_METRIC_FOLDERS:
                metric_dir = condition_dir / metric_folder
                if not metric_dir.is_dir():
                    continue
                for path in sorted(metric_dir.glob("*.csv")):
                    files.append(self._describe_file(root, path, condition, metric_folder))
        if not files:
            raise LegacyCSVError(
                f"no CSV files found under {root}; expected layout "
                "<condition>/<metric_folder>/*.csv"
            )
        return tuple(files)

    def _describe_file(
        self, root: Path, path: Path, condition: str, metric_folder: str
    ) -> SourceFile:
        try:
            frame = pd.read_csv(path)
        except Exception as error:  # noqa: BLE001 - 任何讀取失敗都要成為可稽核事實
            return SourceFile(
                original_path=path.relative_to(root).as_posix(),
                condition=condition,
                metric=_FOLDER_TO_METRIC[metric_folder],
                metric_folder=metric_folder,
                measurement_key="",
                natural_sort_index=_natural_sort_index(path.name),
                row_count=-1,
                data_column_index=-1,
                sha256=f"unreadable:{type(error).__name__}",
            )

        # legacy 合併程式的取欄規則：有兩欄以上取第 2 欄，否則取第 1 欄。
        data_column_index = 1 if frame.shape[1] >= 2 else 0
        key = self._extract_measurement_key(path.stem, metric_folder)
        return SourceFile(
            original_path=path.relative_to(root).as_posix(),
            condition=condition,
            metric=_FOLDER_TO_METRIC[metric_folder],
            metric_folder=metric_folder,
            measurement_key=key or "",
            natural_sort_index=_natural_sort_index(path.name),
            row_count=int(frame.shape[0]),
            data_column_index=data_column_index,
            sha256=hash_file(path),
        )

    # -- 對齊稽核 ----------------------------------------------------------

    def audit_alignment(self, inventory: tuple[SourceFile, ...]) -> AlignmentReport:
        """以測量編號配對四個 metric，產出對齊結果、排除帳與五層計數。"""
        exclusions: list[ExclusionEntry] = []
        grouped: dict[tuple[str, str], dict[str, SourceFile]] = {}
        seen: dict[tuple[str, str, str], SourceFile] = {}

        for source in inventory:
            if source.row_count < 0:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=source.original_path,
                        reason=ExclusionReason.UNREADABLE_FILE,
                        detail={"sha256": source.sha256},
                    )
                )
                continue
            if not source.measurement_key:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=source.original_path,
                        reason=ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY,
                        detail={"key_strategy": self.config.key_strategy},
                    )
                )
                continue
            if source.row_count == 0:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=source.original_path,
                        reason=ExclusionReason.EMPTY_FILE,
                        detail={},
                    )
                )
                continue

            identity = (source.condition, source.metric, source.measurement_key)
            if identity in seen:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=source.original_path,
                        reason=ExclusionReason.DUPLICATE_MEASUREMENT_KEY,
                        detail={
                            "measurement_key": source.measurement_key,
                            "conflicts_with": seen[identity].original_path,
                        },
                    )
                )
                continue
            seen[identity] = source
            grouped.setdefault((source.condition, source.measurement_key), {})[
                source.metric
            ] = source

        aligned: list[AlignedMeasurement] = []
        canonical_count = 0

        for (condition, key), metric_files in sorted(grouped.items()):
            identifier = f"{condition}/{key}"
            missing = [metric for metric in TOF_SCHEMA if metric not in metric_files]
            if missing:
                exclusions.append(
                    ExclusionEntry(
                        scope="measurement",
                        identifier=identifier,
                        reason=ExclusionReason.MISSING_METRIC,
                        detail={
                            "missing": missing,
                            "present": sorted(metric_files),
                        },
                    )
                )
                continue

            # 走到這裡代表四個 metric 都有 provenance mapping，即 canonical。
            canonical_count += 1

            row_counts = {
                metric: source.row_count for metric, source in metric_files.items()
            }
            if len(set(row_counts.values())) != 1:
                exclusions.append(
                    ExclusionEntry(
                        scope="measurement",
                        identifier=identifier,
                        reason=ExclusionReason.ROW_COUNT_MISMATCH,
                        detail={"row_counts": row_counts},
                    )
                )
                if self.config.formal:
                    raise LegacyCSVError(
                        f"measurement {identifier} has mismatched metric lengths "
                        f"{row_counts}; formal mode refuses to crop silently "
                        "(SRC-SAI §7.5)"
                    )
                continue

            row_count = next(iter(row_counts.values()))
            if row_count != self.config.expected_row_count:
                exclusions.append(
                    ExclusionEntry(
                        scope="measurement",
                        identifier=identifier,
                        reason=ExclusionReason.ROW_COUNT_NOT_EXPECTED,
                        detail={
                            "row_count": row_count,
                            "expected": self.config.expected_row_count,
                        },
                    )
                )
                continue

            aligned.append(
                AlignedMeasurement(
                    condition=condition,
                    class_label=self.config.label_map[condition],
                    measurement_key=key,
                    files=metric_files,
                )
            )

        valid_count = len(aligned)
        e1_eligible = (
            valid_count if self.config.sigma_status == SIGMA_STATUS_RESOLVED else 0
        )
        if self.config.sigma_status != SIGMA_STATUS_RESOLVED and aligned:
            exclusions.append(
                ExclusionEntry(
                    scope="dataset",
                    identifier="sigma_provenance",
                    reason=ExclusionReason.SIGMA_PROVENANCE_UNRESOLVED,
                    detail={
                        "status": self.config.sigma_status,
                        "affected_recordings": valid_count,
                        "gate": "E1-G08",
                    },
                )
            )

        counts = CountStatus(
            nominal_logical_recordings=self.config.nominal_logical_recordings,
            physical_source_files=len(inventory),
            canonical_recordings=canonical_count,
            valid_recordings=valid_count,
            e1_eligible_recordings=e1_eligible,
        )

        return AlignmentReport(
            source_root=str(self._source_root or ""),
            key_strategy=self.config.key_strategy,
            aligned=tuple(aligned),
            exclusions=tuple(exclusions),
            counts=counts,
            nominal_source=self.config.nominal_source,
        )

    # -- 載入 --------------------------------------------------------------

    def _read_metric_column(self, path: Path, column_index: int) -> np.ndarray:
        frame = pd.read_csv(path)
        return frame.iloc[:, column_index].to_numpy(dtype=np.float64)

    def _derive_sample_interval(
        self, path: Path
    ) -> tuple[float | None, str, dict[str, float]]:
        """從第 0 欄推導實際取樣間隔。

        SRC-SAI §6 要求 real interval 由 recording provenance 提供，
        不得沿用 0.082 或 0.02 這類全域常數。第 0 欄若是單調遞增的數值時間軸，
        就是該筆自己的 provenance；否則回報缺失，由呼叫端決定是否排除。
        """
        frame = pd.read_csv(path)
        if frame.shape[1] < 2:
            return None, "absent:single_column", {}
        try:
            axis = frame.iloc[:, 0].to_numpy(dtype=np.float64)
        except (ValueError, TypeError):
            return None, "absent:non_numeric_first_column", {}
        if axis.size < 2 or not np.all(np.isfinite(axis)):
            return None, "absent:non_finite_axis", {}
        diffs = np.diff(axis)
        if np.any(diffs <= 0):
            return None, "absent:non_monotonic_axis", {}
        median = float(np.median(diffs))
        if median <= 0:
            return None, "absent:non_positive_interval", {}
        cv = float(np.std(diffs) / median)
        stats = {"median_interval_s": median, "interval_cv": cv}
        if cv > self.config.max_interval_cv:
            return None, "absent:unstable_axis", stats
        return median, "derived_from_column_0", stats

    def load_recording(
        self,
        measurement: AlignedMeasurement,
        *,
        counts: CountStatus,
        split_role: SplitRole,
        serial: int,
    ) -> CanonicalCase:
        """把一筆已對齊的測量載入成 CanonicalCase。

        四個 metric 依 TOF_SCHEMA 順序組成 (500,4)；這裡不做任何補值或裁切，
        前面的 audit_alignment() 已經把不合格的筆數排除掉了。
        """
        if self._source_root is None:
            raise LegacyCSVError("build_inventory() must run before load_recording()")

        columns = []
        alignment_entries = []
        for metric in TOF_SCHEMA:
            source = measurement.files[metric]
            path = self._source_root / source.original_path
            columns.append(self._read_metric_column(path, source.data_column_index))
            alignment_entries.append(
                MetricAlignment(
                    metric=metric,
                    source_filename=source.original_path,
                    row_count=source.row_count,
                    source_sha256=source.sha256,
                )
            )

        tof = np.column_stack(columns)
        if not np.all(np.isfinite(tof)):
            raise SchemaViolation(
                f"measurement {measurement.condition}/{measurement.measurement_key} "
                "contains non-finite values; it must be excluded rather than filled"
            )

        reference = measurement.files[TOF_SCHEMA[0]]
        interval, interval_source, interval_stats = self._derive_sample_interval(
            self._source_root / reference.original_path
        )
        if interval is None:
            raise SchemaViolation(
                f"measurement {measurement.condition}/{measurement.measurement_key} "
                f"has no usable measurement-time provenance ({interval_source}); "
                "the sample interval must come from the recording itself, never from "
                "a global constant"
            )

        return CanonicalCase(
            case_id=real_recording_id(measurement.class_label, serial),
            source_role=SourceRole.REAL_ANCHOR,
            split_role=split_role,
            class_label=ClassLabel(measurement.class_label),
            truth_visibility=TruthVisibility.EVALUATOR_ONLY,
            provenance={
                "source_condition": measurement.condition,
                "measurement_key": measurement.measurement_key,
                "key_strategy": self.config.key_strategy,
                "sigma_status": self.config.sigma_status,
                "sample_interval_source": interval_source,
                "sample_interval_stats": interval_stats,
            },
            tof_sequence=tof,
            measurement_time=MeasurementTime(
                sample_interval_s=interval,
                n_samples=int(tof.shape[0]),
                source=interval_source,
            ),
            count_status=counts,
            metric_alignment=tuple(alignment_entries),
        )
