# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 provenance audit-timing 呼叫；讀含時間欄的原始 recording
#         與設定中的文件記載值；寫出 provenance/timing_provenance.json，
#         供 E1 的 temporal 指標與 measurement_time 契約引用。
# 檔案路徑: pcmef/provenance/timing.py
# 產生時間: 2026-08-26 03:50 +08:00
# 版本: v0.2.0
# 功能說明: 把「一筆 recording 的取樣間隔到底是多少」這件事變成有證據的結論。
#           它並列記錄文件記載值、腳本設定值與實測值三者，指出彼此差多少，
#           並強制區分光子飛行時間軸與量測時間軸這兩條不同的時間軸。
# 模組定位: SRC-D03 的證據產生器。它「不會」挑一個代表值 ——
#           每筆 recording 的間隔一律由該筆自己的時間欄推導。
# 主要責任:
#   1. TimeAxis 列舉兩條互不換算的時間軸及其允許用途
#   2. IntervalEvidence 描述單一來源的間隔值與其證據等級
#   3. measure_interval() 從真實時間欄推導間隔與穩定度
#   4. audit_timing() 併陳各來源並計算相對於 baseline 的偏差
#   5. TimingProvenance.to_artifact() 產生雙時間軸 provenance artifact
# 維護提醒:
#   - 不得把文件記載的 0.082 或腳本的 0.02 當成任何 recording 的實際間隔；
#     兩者都不是該筆 recording 的實測值。
#   - 不得以 0.0624 s 描述 Edge Impulse 那 560 筆。那個值來自偏移測試副產物
#     CSV，是**另一次採集**；560 筆的 rank-1 provenance 是資料集自帶的
#     interval_ms = 82.00001312 ms（NOTE-028）。
#   - 不得把 optical transient bins 當成 measurement-time 取樣點；
#     兩條時間軸物理意義不同，禁止互相換算（SRC-D03）。
#   - 不得在時間軸不穩定時仍回報一個「代表間隔」；不穩定就是結論本身。
#   - v0.2.0 新增 EvidenceLevel.DATASET_PRIMARY（rank-1）並改以它為偏差 baseline；
#     先前以偏移測試的實測值為 baseline，會把 rank-1 證據寫成偏差方（NOTE-028）。
#   - v0.1.0 新增：首版雙時間軸 provenance，對應 NOTE-011。
# 驗證方式:
#   - py -3.10 -m pytest tests/provenance/test_sigma_and_timing.py -k "interval or timing or axis" -v
#   - py -3.10 -m pcmef.cli provenance audit-timing --recording <csv> --time-column Timestamp
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

__all__ = [
    "TimingProvenanceError",
    "TimeAxis",
    "EvidenceLevel",
    "IntervalEvidence",
    "TimingProvenance",
    "measure_interval",
    "audit_timing",
]


class TimingProvenanceError(ValueError):
    """時間軸資料不足以推導間隔。"""


class TimeAxis(str, Enum):
    """兩條物理意義不同、禁止互相換算的時間軸（SRC-PLAN §2.2.1 表）。"""

    OPTICAL_TRANSIENT = "optical_transient_time"
    MEASUREMENT = "measurement_time"


class EvidenceLevel(str, Enum):
    """間隔值的證據等級，由強到弱（SRC-HANDOFF §8）。

    NOTE(NOTE-028): `DATASET_PRIMARY` 是 rank-1 —— 資料集自帶的 `interval_ms`。
    它由產生資料的那次採集寫入，是唯一能回答「這 560 筆是以什麼間隔錄的」的來源。

    `MEASURED` 是「從某個時間欄實測而得」，強度取決於**量的是哪一批資料**。
    偏移測試副產物 CSV 的實測值不能拿來描述 Edge Impulse 那 560 筆，
    兩者是不同次採集。分成兩級就是為了讓這件事無法被混淆。
    """

    DATASET_PRIMARY = "dataset_primary"
    MEASURED = "measured"
    SCRIPT_NOMINAL = "script_nominal"
    DOCUMENTED = "documented"


@dataclass(frozen=True)
class IntervalEvidence:
    """單一來源提出的取樣間隔。"""

    level: EvidenceLevel
    value_s: float
    source: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "value_s": self.value_s,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TimingProvenance:
    """雙時間軸 provenance 的完整結論。"""

    evidence: tuple[IntervalEvidence, ...]
    measured: IntervalEvidence | None
    axis_separation_note: str
    discrepancies: dict[str, float]
    baseline: IntervalEvidence | None = None

    def to_artifact(self) -> dict[str, Any]:
        return {
            "axes": {
                TimeAxis.OPTICAL_TRANSIENT.value: {
                    "meaning": "單次 acquisition 內部的光子飛行時間 / transient bins",
                    "allowed_use": "推導單一 sensor observation 的 peak/width/SNR",
                    "forbidden": "不得直接切成 500 點 measurement sequence",
                },
                TimeAxis.MEASUREMENT.value: {
                    "meaning": "連續 acquisition 的取樣時間軸",
                    "allowed_use": "形成 500x4 TofRecording、計算 temporal variability",
                    "forbidden": "不得以 deployment 設定值覆蓋原始 dataset provenance",
                },
            },
            "axis_separation_note": self.axis_separation_note,
            "evidence": [e.to_dict() for e in self.evidence],
            "measured": self.measured.to_dict() if self.measured else None,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "discrepancies_vs_baseline": {
                k: round(v, 4) for k, v in sorted(self.discrepancies.items())
            },
        }


def measure_interval(
    time_axis: np.ndarray, max_cv: float = 0.25, source: str = "recording_time_column"
) -> IntervalEvidence:
    """從真實時間欄推導取樣間隔中位數與穩定度。

    回傳的 detail 一律包含變異係數；穩定度是結論的一部分，
    不是可以省略的附註 —— 一個不穩定的時間軸不該被當成單一間隔使用。
    """
    axis = np.asarray(time_axis, dtype=np.float64).ravel()
    axis = axis[np.isfinite(axis)]
    if axis.size < 2:
        raise TimingProvenanceError("time axis needs at least two finite samples")

    diffs = np.diff(axis)
    if np.any(diffs <= 0):
        raise TimingProvenanceError(
            "time axis is not strictly increasing; it cannot be a measurement-time axis"
        )

    median = float(np.median(diffs))
    cv = float(np.std(diffs) / median) if median > 0 else float("inf")
    return IntervalEvidence(
        level=EvidenceLevel.MEASURED,
        value_s=median,
        source=source,
        detail={
            "n_samples": int(axis.size),
            "duration_s": round(float(axis[-1] - axis[0]), 4),
            "interval_cv": round(cv, 6),
            "stable": bool(cv <= max_cv),
        },
    )


def audit_timing(
    measured: IntervalEvidence | None,
    documented_s: float | None = None,
    script_nominal_s: float | None = None,
    documented_source: str = "SRC-PLAN §2.1",
    script_source: str = "SRC-NOTION SAMPLE_INTERVAL",
    dataset_primary_s: float | None = None,
    dataset_primary_source: str = "edge_impulse_export:payload.interval_ms",
    dataset_primary_detail: dict[str, Any] | None = None,
) -> TimingProvenance:
    """併陳各來源的間隔值，並計算其餘來源相對於 **baseline** 的偏差。

    NOTE(NOTE-028): baseline 為 `dataset_primary`（若提供），否則才退回 `measured`。
    先前一律以 `measured` 當 baseline，而那個 measured 來自偏移測試副產物 CSV
    （0.0624 s），於是報告會把 dataset 自帶的 0.082 描述成「偏離 31%」——
    把 rank-1 證據寫成偏差方。偏差以相對百分比表示，因為「差幾毫秒」
    在不同量級下意義差很多。
    """
    evidence: list[IntervalEvidence] = []
    if dataset_primary_s is not None:
        evidence.append(
            IntervalEvidence(
                level=EvidenceLevel.DATASET_PRIMARY,
                value_s=float(dataset_primary_s),
                source=dataset_primary_source,
                detail=dataset_primary_detail
                or {
                    "note": "資料集自帶的 interval_ms，由產生這批資料的採集寫入；"
                    "rank-1，不得被 deployment 設定值或他次採集的實測值覆蓋"
                },
            )
        )
    if documented_s is not None:
        evidence.append(
            IntervalEvidence(
                level=EvidenceLevel.DOCUMENTED,
                value_s=float(documented_s),
                source=documented_source,
                detail={"note": "歷史概述值，非任一 recording 的實測結果"},
            )
        )
    if script_nominal_s is not None:
        evidence.append(
            IntervalEvidence(
                level=EvidenceLevel.SCRIPT_NOMINAL,
                value_s=float(script_nominal_s),
                source=script_source,
                detail={
                    "note": "deployment 迴圈的 sleep 設定值；迴圈內另有 sleep 與 I²C "
                    "開銷，實際達成間隔必然更大"
                },
            )
        )
    if measured is not None:
        evidence.append(measured)

    baseline = next(
        (e for e in evidence if e.level is EvidenceLevel.DATASET_PRIMARY), measured
    )
    discrepancies: dict[str, float] = {}
    if baseline is not None and baseline.value_s > 0:
        for item in evidence:
            if item.level is baseline.level:
                continue
            discrepancies[item.level.value] = (
                (item.value_s - baseline.value_s) / baseline.value_s * 100.0
            )

    return TimingProvenance(
        evidence=tuple(evidence),
        measured=measured,
        axis_separation_note=(
            "optical_transient_time 與 measurement_time 為兩條獨立時間軸，"
            "不得互相換算；temporal E1 僅在 measurement_time 上計算。"
        ),
        discrepancies=discrepancies,
        baseline=baseline,
    )
