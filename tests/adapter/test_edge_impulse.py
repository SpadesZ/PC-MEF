# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以合成的 Edge Impulse 匯出結構操作
#         adapters.edge_impulse；若 data/raw_real/edge_impulse_export/ 存在
#         則另跑一組真實資料交叉驗證，不存在時自動 skip。
# 檔案路徑: tests/adapter/test_edge_impulse.py
# 產生時間: 2026-08-26 15:50 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 Edge Impulse 匯出的每一筆樣本都被逐筆檢查形狀、欄序與取樣間隔，
#           並確認產出的案例真的滿足 500x4 契約。
# 模組定位: 第三個 EvidenceSource 的驗收測試。它不因為來源是「官方匯出」
#           就放寬驗證 —— 那是 ingest 後再匯出的產物，不是原始 CSV。
# 主要責任:
#   1. _make_export() 合成 Edge Impulse 匯出目錄結構
#   2. 正常路徑：計數、每類數量、CanonicalCase 形狀與 provenance
#   3. 欄序錯置、形狀錯誤、非有限值、重複編號的排除路徑
#   4. sigma 未解析時 e1_eligible 歸零
#   5. 真實資料存在時的交叉驗證
# 維護提醒:
#   - 不得移除欄序逐筆比對；欄序錯置沒有任何症狀，是 SRC-D04 的核心風險。
#   - 不得讓真實資料測試變成必要條件；repo 不含 data/raw_real/。
#   - v0.1.0 新增：首版 Edge Impulse reader 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/adapter/test_edge_impulse.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from pcmef.adapters.base import ExclusionReason
from pcmef.adapters.edge_impulse import EdgeImpulseAdapter, EdgeImpulseError
from pcmef.core.constants import LEGACY_CSV_COLUMN_TITLES, TOF_SCHEMA
from pcmef.core.schema import SplitRole

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_EXPORT = REPO_ROOT / "data" / "raw_real" / "edge_impulse_export"
INTERVAL_MS = 82.00001312000211

LABELS = ("empty", "water-filled", "bubbly", "misty")


def _sample_json(rows: int = 500, sensors=None, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    values = np.column_stack(
        [
            rng.integers(90, 120, rows).astype(float),
            np.round(rng.integers(5, 12, rows) / 128.0, 4),
            np.round(rng.integers(2600, 2900, rows) / 128.0, 4),
            np.round(rng.integers(20000, 40000, rows) / 65536.0, 4),
        ]
    )
    return {
        "protected": {"ver": "v1", "alg": "HS256"},
        "signature": "0" * 8,
        "payload": {
            "device_type": "EDGE_IMPULSE_UPLOADER",
            "interval_ms": INTERVAL_MS,
            "sensors": [
                {"name": name, "units": "N/A"}
                for name in (sensors or LEGACY_CSV_COLUMN_TITLES)
            ],
            "values": values.tolist(),
        },
    }


def _make_export(root: Path, per_class: int = 5, train_ratio: float = 0.8) -> Path:
    """合成 Edge Impulse 匯出結構：training/ 與 testing/ 各若干 JSON。"""
    n_train = int(per_class * train_ratio)
    for label in LABELS:
        for n in range(1, per_class + 1):
            split = "training" if n <= n_train else "testing"
            path = root / split / f"{label}.measurement_{n}.csv.abc{n}.ingestion-x.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_sample_json(seed=n)), encoding="utf-8")
    return root


def _audit(root: Path, **kwargs):
    adapter = EdgeImpulseAdapter(**kwargs)
    inventory = adapter.build_inventory(root)
    return adapter, adapter.audit_alignment(inventory)


# ---------------------------------------------------------------------------
# 正常路徑
# ---------------------------------------------------------------------------


def test_complete_export_yields_one_recording_per_sample(tmp_path):
    _, report = _audit(_make_export(tmp_path, per_class=5))
    assert report.counts.physical_source_files == 20
    assert report.counts.canonical_recordings == 20
    assert report.counts.valid_recordings == 20
    assert report.exclusion_summary() == {"sigma_provenance_unresolved": 1}
    assert report.by_class() == {
        "Bubbly": 5, "Empty": 5, "Misty": 5, "Water-filled": 5
    }


def test_recording_satisfies_the_500x4_contract(tmp_path):
    """NOTE-011 原本判定真實資料無法滿足此契約；Edge Impulse 匯出可以。"""
    adapter, report = _audit(_make_export(tmp_path, per_class=2))
    case = adapter.load_recording(
        report.aligned[0], counts=report.counts,
        split_role=SplitRole.CALIBRATION, serial=1,
    )
    assert case.tof_sequence.shape == (500, len(TOF_SCHEMA))
    assert case.metric_alignment is not None and len(case.metric_alignment) == 4
    assert case.measurement_time.n_samples == 500
    assert case.measurement_time.sample_interval_s == pytest.approx(0.082000013, abs=1e-9)


def test_interval_comes_from_the_sample_not_a_constant(tmp_path):
    """每筆自帶 interval_ms，那就是該筆的 provenance（NOTE-011）。"""
    adapter, report = _audit(_make_export(tmp_path, per_class=2))
    case = adapter.load_recording(
        report.aligned[0], counts=report.counts,
        split_role=SplitRole.CALIBRATION, serial=1,
    )
    assert case.measurement_time.source == "edge_impulse_export:interval_ms"
    assert case.provenance["sample_interval_source"] == "edge_impulse_export:interval_ms"


def test_report_records_the_observed_intervals(tmp_path):
    _, report = _audit(_make_export(tmp_path, per_class=2))
    assert "82.00001312" in report.key_strategy


# ---------------------------------------------------------------------------
# 排除路徑
# ---------------------------------------------------------------------------


def test_wrong_sensor_order_is_rejected(tmp_path):
    """SRC-D04：欄序錯置沒有任何症狀，必須逐筆比對。"""
    root = _make_export(tmp_path, per_class=2)
    victim = next((root / "training").glob("empty.*"))
    swapped = list(LEGACY_CSV_COLUMN_TITLES)
    swapped[1], swapped[2] = swapped[2], swapped[1]
    victim.write_text(json.dumps(_sample_json(sensors=swapped)), encoding="utf-8")

    _, report = _audit(root)
    bad = [e for e in report.exclusions if e.reason is ExclusionReason.MISSING_METRIC]
    assert len(bad) == 1
    assert bad[0].detail["actual_order"] != list(LEGACY_CSV_COLUMN_TITLES)


def test_wrong_row_count_is_rejected(tmp_path):
    root = _make_export(tmp_path, per_class=2)
    victim = next((root / "training").glob("bubbly.*"))
    victim.write_text(json.dumps(_sample_json(rows=480)), encoding="utf-8")

    _, report = _audit(root)
    bad = [
        e for e in report.exclusions
        if e.reason is ExclusionReason.ROW_COUNT_NOT_EXPECTED
    ]
    assert bad and bad[0].detail["shape"] == [480, 4]


def test_non_finite_values_are_rejected(tmp_path):
    root = _make_export(tmp_path, per_class=2)
    victim = next((root / "training").glob("misty.*"))
    payload = _sample_json()
    payload["payload"]["values"][7][2] = float("nan")
    victim.write_text(json.dumps(payload), encoding="utf-8")

    _, report = _audit(root)
    # json.dumps 會把 NaN 寫成裸 NaN，讀回時仍是 float('nan')。
    assert any(
        e.reason is ExclusionReason.NON_FINITE_VALUE for e in report.exclusions
    )


def test_unreadable_sample_is_recorded(tmp_path):
    root = _make_export(tmp_path, per_class=2)
    victim = next((root / "training").glob("water-filled.*"))
    victim.write_text("{ not valid json", encoding="utf-8")

    _, report = _audit(root)
    assert any(
        e.reason is ExclusionReason.UNREADABLE_FILE for e in report.exclusions
    )


def test_missing_root_is_rejected(tmp_path):
    with pytest.raises(EdgeImpulseError, match="not found"):
        EdgeImpulseAdapter().build_inventory(tmp_path / "absent")


def test_directory_without_samples_is_rejected(tmp_path):
    (tmp_path / "empty_dir").mkdir()
    with pytest.raises(EdgeImpulseError, match="no sample JSON"):
        EdgeImpulseAdapter().build_inventory(tmp_path / "empty_dir")


# ---------------------------------------------------------------------------
# Sigma gate
# ---------------------------------------------------------------------------


def test_unresolved_sigma_zeroes_e1_eligibility(tmp_path):
    _, report = _audit(_make_export(tmp_path, per_class=3))
    assert report.counts.valid_recordings == 12
    assert report.counts.e1_eligible_recordings == 0


def test_resolved_sigma_allows_e1_eligibility(tmp_path):
    _, report = _audit(_make_export(tmp_path, per_class=3), sigma_status="RESOLVED")
    assert report.counts.e1_eligible_recordings == 12


# ---------------------------------------------------------------------------
# 真實資料交叉驗證（存在時才跑）
# ---------------------------------------------------------------------------

needs_real = pytest.mark.skipif(
    not REAL_EXPORT.is_dir(), reason="real Edge Impulse export not present in this checkout"
)


@needs_real
def test_real_export_has_560_valid_recordings():
    _, report = _audit(REAL_EXPORT)
    assert report.counts.physical_source_files == 560
    assert report.counts.canonical_recordings == 560
    assert report.counts.valid_recordings == 560
    assert report.by_class() == {
        "Bubbly": 140, "Empty": 140, "Misty": 140, "Water-filled": 140
    }


@needs_real
def test_real_export_uses_a_single_sampling_interval():
    """Edge Impulse 的 12.19512 Hz 對應 1/12.19512 = 0.082 s。"""
    _, report = _audit(REAL_EXPORT)
    assert "82.00001312" in report.key_strategy
    assert 1.0 / (INTERVAL_MS / 1000.0) == pytest.approx(12.19512, abs=1e-5)


@needs_real
def test_real_export_class_means_match_the_documented_anchors():
    """SRC-PLAN §2.1 的距離錨點必須與真實資料相符。"""
    adapter, report = _audit(REAL_EXPORT)
    per_class: dict[str, list[float]] = {}
    for measurement in report.aligned:
        sample = adapter._samples[measurement.files[TOF_SCHEMA[0]].original_path]
        per_class.setdefault(measurement.class_label, []).append(
            float(sample.values[:, 0].mean())
        )
    means = {k: float(np.mean(v)) for k, v in per_class.items()}
    assert means["Empty"] == pytest.approx(100.0, abs=2.0)
    assert means["Water-filled"] == pytest.approx(114.0, abs=2.0)
    assert means["Bubbly"] == pytest.approx(105.5, abs=2.0)
    assert 55.0 <= means["Misty"] <= 90.0
