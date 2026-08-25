# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 audit real-data 與 provenance 指令建構；只讀 data/raw_real/
#         tof_aggregated/KG_all 下的 32 個彙總 CSV；產出 AggregatedRecording
#         與 AlignmentReport，供 provenance.sigma / provenance.anchors 使用。
# 檔案路徑: pcmef/adapters/legacy_kg.py
# 產生時間: 2026-08-26 03:10 +08:00
# 版本: v0.1.0
# 功能說明: 讀取前研究實際留存下來的 ToF 資料 —— 每個類別每個特徵各有一份
#           平均值與一份標準差的矩陣，橫軸是 140 筆 recording、縱軸是 99 個
#           滑動窗口。它把這 32 個矩陣重新組織成「每筆 recording 一組四特徵」。
# 模組定位: legacy_csv 的姊妹 reader，處理「只剩衍生統計量」的來源格式。
#           它「不是」500 點原始序列的替代品，產出的不是 CanonicalCase。
# 主要責任:
#   1. KG_FILE_PATTERN 解析 KG_<class>_<Metric>_<Stat>.csv 檔名
#   2. build_inventory() 掃描並驗證 32 個矩陣的完整性與形狀一致性
#   3. audit_alignment() 以 recording 欄位（No.N）為單位配對四特徵與兩統計量
#   4. recording_matrix() 取出單筆 recording 的 (99, 4) 平均值矩陣
#   5. metric_values() 供 provenance 分析取用整個類別的某個特徵
# 維護提醒:
#   - 不得把本模組的輸出宣稱為 500 點 recording；它是窗口統計量，
#     任何以此計算的 temporal 指標都必須註明是 window-level（NOTE-011）。
#   - 不得從這裡推導 sample_interval；這批檔案沒有時間欄，
#     猜一個間隔等於偽造 measurement-time provenance。
#   - 不得在缺任一 metric 或統計量時補零或以另一類別代替；一律進 exclusion ledger。
#   - v0.1.0 新增：首版彙總格式 reader，對應 NOTE-011。
# 驗證方式:
#   - py -3.10 -m pytest tests/adapter/test_legacy_kg.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd

from pcmef.adapters.base import ExclusionEntry, ExclusionReason
from pcmef.core.constants import LEGACY_LABEL_MAP, TOF_SCHEMA
from pcmef.core.hash import hash_file
from pcmef.core.schema import CountStatus

__all__ = [
    "LegacyKGError",
    "KG_METRIC_TO_CANONICAL",
    "AggregatedRecording",
    "KGInventory",
    "LegacyKGAdapter",
]


class LegacyKGError(RuntimeError):
    """彙總來源目錄結構不完整或矩陣形狀不一致。"""


# KG 檔名中的 metric 名稱 -> canonical TOF_SCHEMA 名稱。
KG_METRIC_TO_CANONICAL = MappingProxyType(
    {
        "Distance_mm": "distance_mm",
        "Ambient_Rate_MCPS": "ambient_rate_mcps",
        "Signal_Rate_MCPS": "signal_rate_mcps",
        "Sigma_mm": "sigma_like",
    }
)

KG_STATS: tuple[str, ...] = ("Mean", "Std")

_KG_FILE_PATTERN = re.compile(
    r"^KG_(?P<condition>[a-z]+)_(?P<metric>.+)_(?P<stat>Mean|Std)\.csv$"
)

# 欄位標題形如 No.1 ... No.140，代表第 N 筆 recording。
_RECORDING_COLUMN = re.compile(r"^No\.(\d+)$")


@dataclass(frozen=True)
class AggregatedRecording:
    """單筆 recording 的窗口統計量。

    mean/std 皆為 shape (n_windows, 4)，欄序固定為 TOF_SCHEMA。
    這裡刻意不提供 tof_sequence 屬性 —— 它不是 500 點序列，
    命名上就不該讓下游誤用（NOTE-011）。
    """

    condition: str
    class_label: str
    recording_index: int
    window_mean: np.ndarray
    window_std: np.ndarray

    @property
    def n_windows(self) -> int:
        return int(self.window_mean.shape[0])


@dataclass(frozen=True)
class KGMatrix:
    """單一 KG 矩陣檔的 provenance 與內容。"""

    original_path: str
    condition: str
    metric: str
    stat: str
    n_windows: int
    recording_columns: tuple[int, ...]
    sha256: str
    values: np.ndarray


@dataclass(frozen=True)
class KGInventory:
    """彙總來源的完整盤點結果。"""

    source_root: str
    matrices: tuple[KGMatrix, ...]
    exclusions: tuple[ExclusionEntry, ...]

    def by_key(self) -> dict[tuple[str, str, str], KGMatrix]:
        return {(m.condition, m.metric, m.stat): m for m in self.matrices}


class LegacyKGAdapter:
    """前研究彙總 ToF 統計量的 reader。"""

    def __init__(self, label_map: dict[str, str] | None = None) -> None:
        self.label_map = dict(label_map or LEGACY_LABEL_MAP)
        self._source_root: Path | None = None

    # -- 盤點 --------------------------------------------------------------

    def build_inventory(self, source_root: str | Path) -> KGInventory:
        """掃描 KG_all 目錄，解析並驗證每個矩陣檔。"""
        root = Path(source_root)
        if not root.is_dir():
            raise LegacyKGError(f"KG source root not found: {root}")
        self._source_root = root

        matrices: list[KGMatrix] = []
        exclusions: list[ExclusionEntry] = []

        for path in sorted(root.glob("KG_*.csv")):
            match = _KG_FILE_PATTERN.match(path.name)
            if not match:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=path.name,
                        reason=ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY,
                        detail={"expected": "KG_<class>_<Metric>_<Mean|Std>.csv"},
                    )
                )
                continue

            condition = match.group("condition")
            metric_raw = match.group("metric")
            stat = match.group("stat")

            if condition not in self.label_map:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=path.name,
                        reason=ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY,
                        detail={"unknown_condition": condition},
                    )
                )
                continue
            if metric_raw not in KG_METRIC_TO_CANONICAL:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=path.name,
                        reason=ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY,
                        detail={"unknown_metric": metric_raw},
                    )
                )
                continue

            try:
                frame = pd.read_csv(path)
            except Exception as error:  # noqa: BLE001
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=path.name,
                        reason=ExclusionReason.UNREADABLE_FILE,
                        detail={"error": type(error).__name__},
                    )
                )
                continue

            columns = []
            for name in frame.columns:
                column_match = _RECORDING_COLUMN.match(str(name).strip())
                if column_match:
                    columns.append(int(column_match.group(1)))
            if len(columns) != frame.shape[1]:
                exclusions.append(
                    ExclusionEntry(
                        scope="file",
                        identifier=path.name,
                        reason=ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY,
                        detail={
                            "expected_columns": "No.<n>",
                            "actual_first": [str(c) for c in list(frame.columns)[:3]],
                        },
                    )
                )
                continue

            matrices.append(
                KGMatrix(
                    original_path=path.relative_to(root).as_posix(),
                    condition=condition,
                    metric=KG_METRIC_TO_CANONICAL[metric_raw],
                    stat=stat,
                    n_windows=int(frame.shape[0]),
                    recording_columns=tuple(columns),
                    sha256=hash_file(path),
                    values=frame.to_numpy(dtype=np.float64),
                )
            )

        if not matrices:
            raise LegacyKGError(
                f"no parsable KG matrices under {root}; expected files named "
                "KG_<class>_<Metric>_<Mean|Std>.csv"
            )
        return KGInventory(
            source_root=str(root),
            matrices=tuple(matrices),
            exclusions=tuple(exclusions),
        )

    # -- 對齊 --------------------------------------------------------------

    def audit_alignment(
        self, inventory: KGInventory
    ) -> tuple[tuple[AggregatedRecording, ...], tuple[ExclusionEntry, ...], CountStatus]:
        """以 recording 欄位為單位，把四特徵 × 兩統計量配對成完整記錄。"""
        lookup = inventory.by_key()
        exclusions: list[ExclusionEntry] = list(inventory.exclusions)
        recordings: list[AggregatedRecording] = []
        canonical_count = 0

        for condition in sorted(self.label_map):
            required = [
                (condition, metric, stat)
                for metric in TOF_SCHEMA
                for stat in KG_STATS
            ]
            missing = [key for key in required if key not in lookup]
            if missing:
                exclusions.append(
                    ExclusionEntry(
                        scope="condition",
                        identifier=condition,
                        reason=ExclusionReason.MISSING_METRIC,
                        detail={"missing": [f"{m}/{s}" for _, m, s in missing]},
                    )
                )
                continue

            present = [lookup[key] for key in required]
            window_counts = {m.n_windows for m in present}
            if len(window_counts) != 1:
                exclusions.append(
                    ExclusionEntry(
                        scope="condition",
                        identifier=condition,
                        reason=ExclusionReason.ROW_COUNT_MISMATCH,
                        detail={
                            "window_counts": {
                                f"{m.metric}/{m.stat}": m.n_windows for m in present
                            }
                        },
                    )
                )
                continue

            column_sets = {m.recording_columns for m in present}
            if len(column_sets) != 1:
                exclusions.append(
                    ExclusionEntry(
                        scope="condition",
                        identifier=condition,
                        reason=ExclusionReason.MISSING_METRIC,
                        detail={
                            "recording_column_mismatch": {
                                f"{m.metric}/{m.stat}": len(m.recording_columns)
                                for m in present
                            }
                        },
                    )
                )
                continue

            columns = present[0].recording_columns
            n_windows = present[0].n_windows

            for position, recording_index in enumerate(columns):
                canonical_count += 1
                mean = np.column_stack(
                    [
                        lookup[(condition, metric, "Mean")].values[:, position]
                        for metric in TOF_SCHEMA
                    ]
                )
                std = np.column_stack(
                    [
                        lookup[(condition, metric, "Std")].values[:, position]
                        for metric in TOF_SCHEMA
                    ]
                )
                if not (np.all(np.isfinite(mean)) and np.all(np.isfinite(std))):
                    exclusions.append(
                        ExclusionEntry(
                            scope="recording",
                            identifier=f"{condition}/No.{recording_index}",
                            reason=ExclusionReason.NON_FINITE_VALUE,
                            detail={"n_windows": n_windows},
                        )
                    )
                    continue
                recordings.append(
                    AggregatedRecording(
                        condition=condition,
                        class_label=self.label_map[condition],
                        recording_index=recording_index,
                        window_mean=mean,
                        window_std=std,
                    )
                )

        counts = CountStatus(
            nominal_logical_recordings=0,
            physical_source_files=len(inventory.matrices),
            canonical_recordings=canonical_count,
            valid_recordings=len(recordings),
            # 彙總資料永遠不是 e1-eligible：E1 的四特徵 primary 需要
            # Sigma provenance RESOLVED，而且此格式不含 500 點序列（NOTE-011）。
            e1_eligible_recordings=0,
        )
        return tuple(recordings), tuple(exclusions), counts

    # -- 取值 --------------------------------------------------------------

    @staticmethod
    def metric_values(
        inventory: KGInventory, metric: str, stat: str = "Mean"
    ) -> dict[str, np.ndarray]:
        """取出某個特徵在各 condition 的所有窗口值，供 provenance 分析使用。"""
        if metric not in TOF_SCHEMA:
            raise LegacyKGError(f"unknown metric {metric!r}; expected one of {TOF_SCHEMA}")
        if stat not in KG_STATS:
            raise LegacyKGError(f"unknown stat {stat!r}; expected one of {KG_STATS}")
        out: dict[str, np.ndarray] = {}
        for matrix in inventory.matrices:
            if matrix.metric == metric and matrix.stat == stat:
                values = matrix.values.ravel()
                out[matrix.condition] = values[np.isfinite(values)]
        return dict(sorted(out.items()))
