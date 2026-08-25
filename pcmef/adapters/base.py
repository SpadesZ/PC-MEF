# PC-MEF Research System source maintenance contract
# 上下游: 由 adapters.legacy_csv、未來的 adapters.real_vl53l0x 與 adapters.simulation 實作；
#         結構體被 cli 的 audit 指令、splits 與 registry 讀取；本檔不做任何 I/O。
# 檔案路徑: pcmef/adapters/base.py
# 產生時間: 2026-08-26 00:20 +08:00
# 版本: v0.1.0
# 功能說明: 定義三種資料來源共用的盤點結構 —— 單一來源檔案的紀錄、對齊後的一次測量、
#           排除帳（哪些資料為什麼不能用），以及五層計數的彙總報告。
# 模組定位: EvidenceSource 的契約層。它不讀檔、不解析 CSV，只規定盤點結果長什麼樣子。
# 主要責任:
#   1. ExclusionReason 列舉所有「資料不可用」的具名原因
#   2. SourceFile 紀錄單一實體檔案的 provenance
#   3. AlignedMeasurement 表示四個 metric 已對齊的一次測量
#   4. ExclusionEntry 紀錄單筆排除及其可稽核細節
#   5. AlignmentReport 彙總對齊結果、排除帳與五層計數
#   6. EvidenceSource 定義 build_inventory / audit_alignment / load_recording 三段介面
# 維護提醒:
#   - 不得新增「其他」這類籠統的排除原因；每個排除都必須有具名且可統計的理由，
#     否則 exclusion ledger 會退化成無法稽核的自由文字。
#   - 不得讓 AlignmentReport 只回報通過的筆數；被排除的筆數與原因同樣是 M0 的產物。
#   - v0.1.0 新增：首版盤點契約，對應 SRC-SAI 7 節與 8 節。
# 驗證方式:
#   - py -3.10 -m pytest tests/adapter/test_legacy_csv.py -k "exclusion or report or counts"
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from pcmef.core.schema import CanonicalCase, CountStatus

__all__ = [
    "ExclusionReason",
    "SourceFile",
    "AlignedMeasurement",
    "ExclusionEntry",
    "AlignmentReport",
    "EvidenceSource",
]


class ExclusionReason(str, Enum):
    """資料無法使用的具名原因。

    刻意做成封閉列舉：SRC-SAI §7.8 要求 M0 產出
    missing/duplicate/NaN/outlier 的 exclusion ledger，
    而「帳」的前提是每個排除都能被歸類與統計。若允許自由文字理由，
    最後會出現十幾種寫法描述同一件事，無法回答「總共有幾筆因為長度不符被排除」。
    """

    MISSING_METRIC = "missing_metric"
    DUPLICATE_MEASUREMENT_KEY = "duplicate_measurement_key"
    UNPARSEABLE_MEASUREMENT_KEY = "unparseable_measurement_key"
    ROW_COUNT_MISMATCH = "row_count_mismatch"
    ROW_COUNT_NOT_EXPECTED = "row_count_not_expected"
    NON_FINITE_VALUE = "non_finite_value"
    EMPTY_FILE = "empty_file"
    UNREADABLE_FILE = "unreadable_file"
    TIMING_PROVENANCE_MISSING = "timing_provenance_missing"
    SIGMA_PROVENANCE_UNRESOLVED = "sigma_provenance_unresolved"


@dataclass(frozen=True)
class SourceFile:
    """單一實體來源檔案的 provenance 紀錄。

    original_path 一律保存**相對於 source root 的原始路徑**，不做正規化改寫：
    M0 的第一條規則是原始檔案只讀封存，路徑本身就是證據
    （SRC-SAI §7.1、§7.2 source_inventory.csv 欄位）。
    """

    original_path: str
    condition: str
    metric: str
    metric_folder: str
    measurement_key: str
    natural_sort_index: int
    row_count: int
    data_column_index: int
    sha256: str

    def to_row(self) -> dict[str, object]:
        """轉成 source_inventory.csv 的一列。"""
        return {
            "original_path": self.original_path,
            "condition": self.condition,
            "metric": self.metric,
            "metric_folder": self.metric_folder,
            "measurement_key": self.measurement_key,
            "natural_sort_index": self.natural_sort_index,
            "row_count": self.row_count,
            "data_column_index": self.data_column_index,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class AlignedMeasurement:
    """四個 metric 已對齊到同一次測量的結果。

    files 以 canonical metric 名稱為鍵，必須四項齊全 —— 缺任何一項就不是
    AlignedMeasurement，而是一筆 MISSING_METRIC 排除。
    """

    condition: str
    class_label: str
    measurement_key: str
    files: dict[str, SourceFile]

    @property
    def row_count(self) -> int:
        return next(iter(self.files.values())).row_count

    def to_alignment_rows(self) -> list[dict[str, object]]:
        """轉成 measurement_alignment.csv 的列（每 metric 一列）。"""
        return [
            {
                "condition": self.condition,
                "class_label": self.class_label,
                "measurement_key": self.measurement_key,
                "metric": metric,
                "original_path": source.original_path,
                "row_count": source.row_count,
                "sha256": source.sha256,
            }
            for metric, source in sorted(self.files.items())
        ]


@dataclass(frozen=True)
class ExclusionEntry:
    """單筆排除紀錄。detail 必須足以讓人重現判斷，而不只是知道被排除了。"""

    scope: str
    identifier: str
    reason: ExclusionReason
    detail: dict[str, object] = field(default_factory=dict)

    def to_row(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "identifier": self.identifier,
            "reason": self.reason.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AlignmentReport:
    """M0 對齊與計數的完整產出。"""

    source_root: str
    key_strategy: str
    aligned: tuple[AlignedMeasurement, ...]
    exclusions: tuple[ExclusionEntry, ...]
    counts: CountStatus
    nominal_source: str

    def exclusion_summary(self) -> dict[str, int]:
        """依原因統計排除筆數，供 audit report 直接引用。"""
        summary: dict[str, int] = {}
        for entry in self.exclusions:
            summary[entry.reason.value] = summary.get(entry.reason.value, 0) + 1
        return dict(sorted(summary.items()))

    def by_class(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for measurement in self.aligned:
            counts[measurement.class_label] = counts.get(measurement.class_label, 0) + 1
        return dict(sorted(counts.items()))


class EvidenceSource(Protocol):
    """三種資料來源的共同介面（SRC-SAI §6 圖 2、§8）。

    三段式而非單一 load()：盤點、對齊稽核與載入是三個必須分開的階段。
    合併成一步會讓「這批資料能不能用」的判斷與「把它讀進來」的動作綁在一起，
    而 M0 的重點正是在載入之前先把可用性講清楚。
    """

    def build_inventory(self, source_root: str) -> tuple[SourceFile, ...]:
        ...

    def audit_alignment(self, inventory: tuple[SourceFile, ...]) -> AlignmentReport:
        ...

    def load_recording(self, recording_id: str) -> CanonicalCase:
        ...
