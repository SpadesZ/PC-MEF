# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；在 tmp_path 內合成前研究的目錄結構與 CSV，
#         操作 adapters.legacy_csv 與 adapters.base；不觸碰 data/raw_real/。
# 檔案路徑: tests/adapter/test_legacy_csv.py
# 產生時間: 2026-08-26 01:10 +08:00
# 版本: v0.1.0
# 功能說明: 用合成資料重現前研究的資料結構與各種壞掉的情況，驗證 adapter 會把問題
#           記進排除帳而不是靜默吞掉 —— 特別是四個 metric 檔數量不一致時，
#           必須不會發生 legacy 那種「該點之後全部錯位」。
# 模組定位: M0 盤點的驗收測試。它不需要真實資料即可完整執行，
#           真實資料到位後同一批斷言直接適用。
# 主要責任:
#   1. _make_dataset() 合成 <condition>/<metric>/*.csv 結構
#   2. 正常路徑：四 metric 對齊、五層計數、CanonicalCase 組裝
#   3. 對齊防護：缺檔、重複編號、無法解析編號、位置錯位不得發生
#   4. 品質防護：長度不一致、非預期長度、非有限值、空檔、不可讀
#   5. provenance：取樣間隔由時間欄推導、sigma 未解析時 e1_eligible 歸零
# 維護提醒:
#   - 不得把 test_misaligned_file_counts_do_not_shift_subsequent_measurements
#     改成寬鬆斷言；它是 NOTE-010 唯一的自動防線。
#   - 不得在測試中寫入 data/raw_real/；來源檔全程唯讀是 M0 的第一條規則。
#   - v0.1.0 新增：首版 M0 盤點驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/adapter/test_legacy_csv.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pcmef.adapters.base import ExclusionReason
from pcmef.adapters.legacy_csv import (
    LegacyCSVAdapter,
    LegacyCSVConfig,
    LegacyCSVError,
)
from pcmef.core.constants import LEGACY_METRIC_FOLDERS, TOF_SCHEMA
from pcmef.core.schema import SchemaViolation, SplitRole

CONDITIONS = ("nowater", "water", "bubble", "smoke")
INTERVAL = 0.082


def _write_metric_csv(
    path: Path, rows: int = 500, value: float = 1.0, interval: float = INTERVAL,
    finite: bool = True,
) -> None:
    """寫一個 legacy 格式的 metric CSV：第 0 欄時間、第 1 欄數值。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    time_axis = np.arange(rows, dtype=np.float64) * interval
    values = np.full(rows, value, dtype=np.float64) + np.arange(rows) * 1e-3
    if not finite:
        values[rows // 2] = np.nan
    pd.DataFrame({"timestamp": time_axis, "value": values}).to_csv(path, index=False)


def _make_dataset(
    root: Path, per_class: int = 3, rows: int = 500, conditions=CONDITIONS
) -> Path:
    """合成完整且乾淨的來源目錄。"""
    for condition in conditions:
        for metric_index, folder in enumerate(LEGACY_METRIC_FOLDERS):
            for n in range(1, per_class + 1):
                _write_metric_csv(
                    root / condition / folder / f"{folder}_{n:03d}.csv",
                    rows=rows,
                    value=float(metric_index + 1) * 10.0,
                )
    return root


def _audit(root: Path, **config_kwargs):
    adapter = LegacyCSVAdapter(LegacyCSVConfig(**config_kwargs))
    inventory = adapter.build_inventory(root)
    return adapter, inventory, adapter.audit_alignment(inventory)


# ---------------------------------------------------------------------------
# 正常路徑
# ---------------------------------------------------------------------------


def test_clean_dataset_aligns_every_measurement(tmp_path):
    _make_dataset(tmp_path, per_class=3)
    _, inventory, report = _audit(tmp_path, nominal_logical_recordings=12)

    assert len(inventory) == 4 * 4 * 3          # 4 類 x 4 metric x 3 筆
    assert len(report.aligned) == 12
    assert report.counts.physical_source_files == 48
    assert report.counts.canonical_recordings == 12
    assert report.counts.valid_recordings == 12
    assert report.by_class() == {
        "Bubbly": 3, "Empty": 3, "Misty": 3, "Water-filled": 3
    }


def test_each_aligned_measurement_carries_all_four_metric_files(tmp_path):
    _make_dataset(tmp_path, per_class=2)
    _, _, report = _audit(tmp_path)
    for measurement in report.aligned:
        assert sorted(measurement.files) == sorted(TOF_SCHEMA)
        assert len(measurement.to_alignment_rows()) == 4


def test_physical_file_count_is_four_times_the_logical_count(tmp_path):
    """SRC-NOTION 的四資料夾結構：實體檔案數是邏輯 recording 數的四倍。"""
    _make_dataset(tmp_path, per_class=5)
    _, _, report = _audit(tmp_path)
    assert report.counts.physical_source_files == report.counts.canonical_recordings * 4


def test_load_recording_builds_a_canonical_case(tmp_path):
    _make_dataset(tmp_path, per_class=1)
    adapter, _, report = _audit(tmp_path, sigma_status="RESOLVED")
    case = adapter.load_recording(
        report.aligned[0], counts=report.counts, split_role=SplitRole.CALIBRATION, serial=1
    )
    assert case.tof_sequence.shape == (500, 4)
    assert case.metric_alignment is not None and len(case.metric_alignment) == 4
    assert case.provenance["sigma_status"] == "RESOLVED"


def test_loaded_columns_follow_canonical_schema_order(tmp_path):
    """欄位必須依 TOF_SCHEMA 順序組裝，不得依資料夾掃描順序。"""
    _make_dataset(tmp_path, per_class=1)
    adapter, _, report = _audit(tmp_path)
    case = adapter.load_recording(
        report.aligned[0], counts=report.counts, split_role=SplitRole.CALIBRATION, serial=1
    )
    # _make_dataset 讓每個 metric 的基準值 = (index+1)*10，因此欄均值遞增。
    means = case.tof_sequence.mean(axis=0)
    assert list(np.argsort(means)) == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# 對齊防護（NOTE-010）
# ---------------------------------------------------------------------------


def test_misaligned_file_counts_do_not_shift_subsequent_measurements(tmp_path):
    """NOTE-010 的核心防線。

    legacy 合併程式依排序位置配對，因此 sigma 資料夾少了第 2 筆時，
    第 2 筆之後的每一筆都會拿到錯位的 sigma（第 3 筆的 sigma 配到第 2 筆）。
    改以檔名編號配對後，只有第 2 筆被排除，第 1、3 筆完全不受影響。
    """
    _make_dataset(tmp_path, per_class=3, conditions=("nowater",))
    (tmp_path / "nowater" / "sigma" / "sigma_002.csv").unlink()

    _, _, report = _audit(tmp_path)

    aligned_keys = sorted(m.measurement_key for m in report.aligned)
    assert aligned_keys == ["1", "3"], "只有缺檔的那一筆該被排除"

    missing = [
        e for e in report.exclusions if e.reason is ExclusionReason.MISSING_METRIC
    ]
    assert len(missing) == 1
    assert missing[0].identifier == "nowater/2"
    assert missing[0].detail["missing"] == ["sigma_like"]

    # 第 3 筆的四個檔案都必須來自編號 3，沒有任何一個來自編號 2。
    third = next(m for m in report.aligned if m.measurement_key == "3")
    assert all("003" in f.original_path for f in third.files.values())


def test_extra_stray_file_in_one_folder_does_not_corrupt_alignment(tmp_path):
    """多一個暫存檔在 legacy 做法下同樣會整批位移。"""
    _make_dataset(tmp_path, per_class=3, conditions=("water",))
    _write_metric_csv(tmp_path / "water" / "distance" / "distance_999.csv")

    _, _, report = _audit(tmp_path)
    assert sorted(m.measurement_key for m in report.aligned) == ["1", "2", "3"]
    missing = [
        e for e in report.exclusions if e.reason is ExclusionReason.MISSING_METRIC
    ]
    assert [e.identifier for e in missing] == ["water/999"]


def test_duplicate_measurement_key_is_recorded_not_overwritten(tmp_path):
    _make_dataset(tmp_path, per_class=1, conditions=("bubble",))
    _write_metric_csv(tmp_path / "bubble" / "distance" / "copy_of_001.csv")

    _, _, report = _audit(tmp_path)
    duplicates = [
        e for e in report.exclusions
        if e.reason is ExclusionReason.DUPLICATE_MEASUREMENT_KEY
    ]
    assert len(duplicates) == 1
    assert "conflicts_with" in duplicates[0].detail


def test_unparseable_filename_is_excluded_with_the_strategy_recorded(tmp_path):
    _make_dataset(tmp_path, per_class=1, conditions=("smoke",))
    _write_metric_csv(tmp_path / "smoke" / "ambient" / "no_digits_here.csv")

    _, _, report = _audit(tmp_path)
    bad = [
        e for e in report.exclusions
        if e.reason is ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY
    ]
    assert len(bad) == 1
    assert bad[0].detail["key_strategy"] == "trailing_integer"


def test_full_stem_strategy_pairs_identically_named_files(tmp_path):
    """檔名完全一致時，full_stem 策略同樣能配對。"""
    for folder in LEGACY_METRIC_FOLDERS:
        _write_metric_csv(tmp_path / "nowater" / folder / "run_alpha.csv")
    _, _, report = _audit(tmp_path, key_strategy="full_stem")
    assert len(report.aligned) == 1
    assert report.aligned[0].measurement_key == "run_alpha"


def test_unknown_key_strategy_is_rejected():
    with pytest.raises(LegacyCSVError, match="unknown key_strategy"):
        LegacyCSVConfig(key_strategy="positional")


# ---------------------------------------------------------------------------
# 品質防護
# ---------------------------------------------------------------------------


def test_row_count_mismatch_is_excluded_not_cropped(tmp_path):
    _make_dataset(tmp_path, per_class=1, conditions=("nowater",))
    _write_metric_csv(tmp_path / "nowater" / "signal" / "signal_001.csv", rows=498)

    _, _, report = _audit(tmp_path)
    assert report.aligned == ()
    mismatch = [
        e for e in report.exclusions if e.reason is ExclusionReason.ROW_COUNT_MISMATCH
    ]
    assert len(mismatch) == 1
    assert mismatch[0].detail["row_counts"]["signal_rate_mcps"] == 498


def test_formal_mode_refuses_to_continue_on_length_mismatch(tmp_path):
    """SRC-SAI §7.5：formal 模式預設 ERROR，不得靜默裁切。"""
    _make_dataset(tmp_path, per_class=1, conditions=("nowater",))
    _write_metric_csv(tmp_path / "nowater" / "signal" / "signal_001.csv", rows=498)

    with pytest.raises(LegacyCSVError, match="refuses to crop"):
        _audit(tmp_path, formal=True)


def test_non_expected_row_count_is_excluded(tmp_path):
    _make_dataset(tmp_path, per_class=1, conditions=("water",), rows=480)
    _, _, report = _audit(tmp_path)
    assert report.aligned == ()
    bad = [
        e for e in report.exclusions
        if e.reason is ExclusionReason.ROW_COUNT_NOT_EXPECTED
    ]
    assert bad[0].detail == {"row_count": 480, "expected": 500}


def test_empty_file_is_excluded(tmp_path):
    _make_dataset(tmp_path, per_class=1, conditions=("smoke",))
    _write_metric_csv(tmp_path / "smoke" / "sigma" / "sigma_001.csv", rows=0)
    _, _, report = _audit(tmp_path)
    assert any(e.reason is ExclusionReason.EMPTY_FILE for e in report.exclusions)


def test_unparseable_csv_is_recorded_rather_than_crashing(tmp_path):
    """欄數不一致會讓 pandas 丟 ParserError，必須成為可稽核事實而非中斷整批。"""
    _make_dataset(tmp_path, per_class=1, conditions=("bubble",))
    broken = tmp_path / "bubble" / "distance" / "distance_001.csv"
    broken.write_text("col1,col2\n1,2,3\n4,5,6,7\n", encoding="utf-8")

    _, inventory, report = _audit(tmp_path)

    unreadable = [
        e for e in report.exclusions if e.reason is ExclusionReason.UNREADABLE_FILE
    ]
    assert len(unreadable) == 1
    assert unreadable[0].identifier.endswith("distance_001.csv")
    # 該筆測量因此缺 distance，必須另外記為 MISSING_METRIC，不得被當成完整測量。
    assert any(
        e.reason is ExclusionReason.MISSING_METRIC for e in report.exclusions
    )
    assert report.aligned == ()


def test_truncated_garbage_file_is_caught_as_a_length_defect(tmp_path):
    """能被 pandas 讀但內容殘缺的檔案，應落在長度檢查而非靜默通過。"""
    _make_dataset(tmp_path, per_class=1, conditions=("bubble",))
    broken = tmp_path / "bubble" / "distance" / "distance_001.csv"
    broken.write_bytes(b"\x00\x01\x02 not,a,valid\ncsv\x00")

    _, _, report = _audit(tmp_path)
    assert report.aligned == ()
    assert any(
        e.reason is ExclusionReason.ROW_COUNT_MISMATCH for e in report.exclusions
    )


def test_non_finite_values_are_refused_at_load_time(tmp_path):
    """NaN 不得被補值；audit 未攔到時 load 必須拒絕。"""
    _make_dataset(tmp_path, per_class=1, conditions=("nowater",))
    _write_metric_csv(
        tmp_path / "nowater" / "ambient" / "ambient_001.csv", finite=False
    )
    adapter, _, report = _audit(tmp_path)
    with pytest.raises(SchemaViolation, match="non-finite"):
        adapter.load_recording(
            report.aligned[0], counts=report.counts,
            split_role=SplitRole.CALIBRATION, serial=1,
        )


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_sample_interval_is_derived_from_the_recording_not_a_constant(tmp_path):
    """SRC-SAI §6：real interval 必須來自該筆自己的 provenance。"""
    for folder in LEGACY_METRIC_FOLDERS:
        _write_metric_csv(
            tmp_path / "nowater" / folder / f"{folder}_001.csv", interval=0.0413
        )
    adapter, _, report = _audit(tmp_path)
    case = adapter.load_recording(
        report.aligned[0], counts=report.counts, split_role=SplitRole.CALIBRATION, serial=1
    )
    assert case.measurement_time.sample_interval_s == pytest.approx(0.0413, rel=1e-6)
    assert case.measurement_time.source == "derived_from_column_0"


def test_missing_time_axis_blocks_loading(tmp_path):
    """沒有時間欄就沒有 measurement-time provenance，不得沿用全域常數。"""
    for folder in LEGACY_METRIC_FOLDERS:
        path = tmp_path / "nowater" / folder / f"{folder}_001.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"value": np.arange(500, dtype=float)}).to_csv(path, index=False)

    adapter, _, report = _audit(tmp_path)
    with pytest.raises(SchemaViolation, match="measurement-time provenance"):
        adapter.load_recording(
            report.aligned[0], counts=report.counts,
            split_role=SplitRole.CALIBRATION, serial=1,
        )


def test_unstable_time_axis_is_not_accepted_as_provenance(tmp_path):
    for folder in LEGACY_METRIC_FOLDERS:
        path = tmp_path / "water" / folder / f"{folder}_001.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(3)
        axis = np.cumsum(rng.uniform(0.001, 0.5, size=500))
        pd.DataFrame(
            {"timestamp": axis, "value": np.ones(500)}
        ).to_csv(path, index=False)

    adapter, _, report = _audit(tmp_path)
    with pytest.raises(SchemaViolation, match="measurement-time provenance"):
        adapter.load_recording(
            report.aligned[0], counts=report.counts,
            split_role=SplitRole.CALIBRATION, serial=1,
        )


def test_unresolved_sigma_zeroes_e1_eligible_count(tmp_path):
    """SRC-SAI E1-G08：Sigma 未解析時不得有任何 e1-eligible recording。"""
    _make_dataset(tmp_path, per_class=2)
    _, _, report = _audit(tmp_path, sigma_status="UNRESOLVED")
    assert report.counts.valid_recordings == 8
    assert report.counts.e1_eligible_recordings == 0
    assert any(
        e.reason is ExclusionReason.SIGMA_PROVENANCE_UNRESOLVED
        for e in report.exclusions
    )


def test_resolved_sigma_allows_e1_eligibility(tmp_path):
    _make_dataset(tmp_path, per_class=2)
    _, _, report = _audit(tmp_path, sigma_status="RESOLVED")
    assert report.counts.e1_eligible_recordings == 8


def test_invalid_sigma_status_is_rejected():
    with pytest.raises(LegacyCSVError, match="sigma_status"):
        LegacyCSVConfig(sigma_status="probably_fine")


# ---------------------------------------------------------------------------
# 計數帳與報告
# ---------------------------------------------------------------------------


def test_nominal_count_is_reported_separately_from_actual(tmp_path):
    """SRC-SAI §7.9：nominal logical count 不得被當成 usable N。"""
    _make_dataset(tmp_path, per_class=2)
    _, _, report = _audit(
        tmp_path, nominal_logical_recordings=560, nominal_source="configs/base.yaml"
    )
    assert report.counts.nominal_logical_recordings == 560
    assert report.counts.canonical_recordings == 8
    assert report.nominal_source == "configs/base.yaml"


def test_exclusion_summary_counts_every_reason(tmp_path):
    _make_dataset(tmp_path, per_class=2, conditions=("nowater",))
    (tmp_path / "nowater" / "sigma" / "sigma_002.csv").unlink()
    _write_metric_csv(tmp_path / "nowater" / "ambient" / "no_digits.csv")

    _, _, report = _audit(tmp_path)
    summary = report.exclusion_summary()
    assert summary[ExclusionReason.MISSING_METRIC.value] == 1
    assert summary[ExclusionReason.UNPARSEABLE_MEASUREMENT_KEY.value] == 1


def test_empty_source_root_is_rejected(tmp_path):
    with pytest.raises(LegacyCSVError, match="no CSV files"):
        _audit(tmp_path)


def test_missing_source_root_is_rejected(tmp_path):
    with pytest.raises(LegacyCSVError, match="source root not found"):
        _audit(tmp_path / "absent")


def test_source_files_are_never_modified(tmp_path):
    """M0 第一條規則：原始檔案只讀封存。"""
    _make_dataset(tmp_path, per_class=2)
    before = {
        p: p.read_bytes() for p in sorted(tmp_path.rglob("*.csv"))
    }
    adapter, inventory, report = _audit(tmp_path, sigma_status="RESOLVED")
    for measurement in report.aligned:
        adapter.load_recording(
            measurement, counts=report.counts,
            split_role=SplitRole.CALIBRATION, serial=1,
        )
    after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*.csv"))}
    assert before == after
