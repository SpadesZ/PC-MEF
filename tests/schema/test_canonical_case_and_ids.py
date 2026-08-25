# 檔案路徑: tests/schema/test_canonical_case_and_ids.py
# 模組定位: CanonicalCase 資料契約與 SRC-SAI §34 ID 命名規範的可執行版本。
# 功能說明: 驗證 500x4 shape、四特徵 canonical 順序、雙時間軸分離、五層計數帳與 ID 格式。
# 主要責任: 讓 SRC-D01..D05 五個已知 provenance 雷點在型別層就無法被忽略。
# 呼叫來源: pytest。
# 輸入契約: 合成陣列與參數，不需要真實資料。
# 輸出契約: 測試通過與否。
# 安全邊界: 無 I/O。
# 維護提醒: TOF_SCHEMA 順序若被更動，本檔的 test_canonical_order_is_distance_ambient_signal_sigma 必須先失敗。
# 版本: v0.1.0 / 2026-08-25
# ----------------------------------------------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import (
    CLASS_ORDER,
    LEGACY_LABEL_MAP,
    TOF_DISPLAY_ORDER,
    TOF_RECORDING_SHAPE,
    TOF_SCHEMA,
    class_index,
    tof_index,
)
from pcmef.core.ids import (
    InvalidIdentifier,
    artifact_path,
    is_canonical_id,
    model_id,
    real_image_id,
    real_recording_id,
    run_id,
    synthetic_scenario_id,
)
from pcmef.core.schema import (
    CanonicalCase,
    ClassLabel,
    CountStatus,
    MeasurementTime,
    SchemaViolation,
    SourceRole,
    SplitRole,
    TruthVisibility,
)


def _tof(rows: int = 500, cols: int = 4) -> np.ndarray:
    rng = np.random.default_rng(11)
    return rng.uniform(1.0, 100.0, size=(rows, cols))


def _measurement_time(n_samples: int = 500) -> MeasurementTime:
    return MeasurementTime(
        sample_interval_s=0.082, n_samples=n_samples, source="recording_provenance"
    )


def _real_case(**overrides) -> CanonicalCase:
    defaults = dict(
        case_id=real_recording_id("Bubbly", 7),
        source_role=SourceRole.REAL_ANCHOR,
        split_role=SplitRole.CALIBRATION,
        class_label=ClassLabel.BUBBLY,
        truth_visibility=TruthVisibility.EVALUATOR_ONLY,
        provenance={"source_manifest_hash": "a" * 64, "sigma_status": "RESOLVED"},
        tof_sequence=_tof(),
        measurement_time=_measurement_time(),
        count_status=CountStatus(560, 2240, 552, 548, 540),
    )
    defaults.update(overrides)
    return CanonicalCase(**defaults)


# ---------------------------------------------------------------------------
# 四特徵 canonical 順序（SRC-D04）
# ---------------------------------------------------------------------------


def test_canonical_order_is_distance_ambient_signal_sigma():
    """NOTE-001：canonical order 採 Notion 推論程式順序，不是 SRC-PLAN 正文敘述順序。"""
    assert TOF_SCHEMA == (
        "distance_mm",
        "ambient_rate_mcps",
        "signal_rate_mcps",
        "sigma_like",
    )
    assert tof_index("ambient_rate_mcps") == 1
    assert tof_index("signal_rate_mcps") == 2


def test_display_order_differs_from_array_order_and_is_not_used_for_indexing():
    """NOTE-002：display order 與 array index 必須是兩個獨立概念。"""
    assert TOF_DISPLAY_ORDER != TOF_SCHEMA
    assert set(TOF_DISPLAY_ORDER) == set(TOF_SCHEMA)
    # 若有人誤用 display order 取 index，signal 與 ambient 會靜默對調。
    assert TOF_DISPLAY_ORDER.index("signal_rate_mcps") != tof_index("signal_rate_mcps")


def test_unknown_feature_name_fails_fast():
    with pytest.raises(ValueError):
        tof_index("sigma_mm")


def test_class_order_and_index_are_frozen():
    assert CLASS_ORDER == ("Empty", "Water-filled", "Bubbly", "Misty")
    assert [class_index(c) for c in CLASS_ORDER] == [0, 1, 2, 3]


def test_legacy_label_map_is_fixed():
    """SRC-SAI FR-002：固定映射，不得由模型自行推定。"""
    assert dict(LEGACY_LABEL_MAP) == {
        "nowater": "Empty",
        "water": "Water-filled",
        "bubble": "Bubbly",
        "smoke": "Misty",
    }
    assert set(LEGACY_LABEL_MAP.values()) == set(CLASS_ORDER)


# ---------------------------------------------------------------------------
# CanonicalCase 契約
# ---------------------------------------------------------------------------


def test_valid_real_case_is_accepted():
    case = _real_case()
    assert case.tof_sequence.shape == TOF_RECORDING_SHAPE
    assert case.tof_schema == TOF_SCHEMA


@pytest.mark.parametrize("shape", [(499, 4), (500, 3), (500, 5), (501, 4)])
def test_wrong_tof_shape_is_rejected(shape):
    with pytest.raises(SchemaViolation, match="shape"):
        _real_case(
            tof_sequence=_tof(*shape), measurement_time=_measurement_time(shape[0])
        )


def test_nan_in_tof_is_rejected_rather_than_zero_filled():
    """SRC-SAI §7：缺欄位標 invalid，不補零。"""
    broken = _tof()
    broken[17, 2] = np.nan
    with pytest.raises(SchemaViolation, match="NaN"):
        _real_case(tof_sequence=broken)


def test_measurement_time_is_required_with_tof():
    with pytest.raises(SchemaViolation, match="measurement_time"):
        _real_case(measurement_time=None)


def test_measurement_time_sample_count_must_match_the_recording():
    with pytest.raises(SchemaViolation, match="n_samples"):
        _real_case(measurement_time=_measurement_time(250))


def test_measurement_time_requires_an_explicit_source():
    """SRC-SAI §6：real interval 由 recording provenance 保存，synthetic 由 frozen config 明示。"""
    with pytest.raises(SchemaViolation, match="source"):
        MeasurementTime(sample_interval_s=0.082, n_samples=500, source="")


def test_tof_schema_cannot_be_reordered_per_case():
    with pytest.raises(SchemaViolation, match="canonical order"):
        _real_case(
            tof_schema=("distance_mm", "signal_rate_mcps", "ambient_rate_mcps", "sigma_like")
        )


def test_real_case_requires_count_status():
    """SRC-SAI §7.9：usable N 必須可追溯到 M0 盤點，不得 hard-code。"""
    with pytest.raises(SchemaViolation, match="count_status"):
        _real_case(count_status=None)


def test_count_status_must_be_monotonically_narrowing():
    with pytest.raises(SchemaViolation, match="canonical >= valid >= e1_eligible"):
        CountStatus(560, 2240, 500, 520, 480)


def test_count_status_allows_more_physical_files_than_logical_recordings():
    """legacy handoff 可能以四個 metric 分檔保存，physical count 因此可大於 logical count。"""
    status = CountStatus(560, 2240, 552, 548, 540)
    assert status.physical_source_files > status.nominal_logical_recordings


def test_synthetic_case_requires_both_transient_artifacts_and_scenario_params():
    """SRC-D03：optical transient time 與 measurement time 必須分開保存。"""
    with pytest.raises(SchemaViolation, match="optical_transient_path"):
        CanonicalCase(
            case_id=synthetic_scenario_id("Clean", "Empty", 1),
            source_role=SourceRole.SYNTHETIC,
            split_role=SplitRole.FORMAL_E2,
            class_label=ClassLabel.EMPTY,
            truth_visibility=TruthVisibility.EVALUATOR_ONLY,
            provenance={"mitsuba_version": "3.5.0"},
            tof_sequence=_tof(),
            measurement_time=_measurement_time(),
            scenario_params={"seed": 1042},
        )


def test_case_without_any_evidence_is_rejected():
    with pytest.raises(SchemaViolation, match="neither ToF nor RGB"):
        CanonicalCase(
            case_id=real_recording_id("Empty", 1),
            source_role=SourceRole.REAL_ANCHOR,
            split_role=SplitRole.CALIBRATION,
            class_label=ClassLabel.EMPTY,
            truth_visibility=TruthVisibility.EVALUATOR_ONLY,
            provenance={"source_manifest_hash": "a" * 64},
            count_status=CountStatus(560, 2240, 552, 548, 540),
        )


def test_provenance_is_mandatory():
    with pytest.raises(SchemaViolation, match="provenance"):
        _real_case(provenance={})


def test_to_json_drops_the_array_and_keeps_enum_values():
    payload = _real_case().to_json()
    assert "tof_sequence" not in payload
    assert payload["class_label"] == "Bubbly"
    assert payload["split_role"] == "calibration"
    assert payload["tof_schema"] == list(TOF_SCHEMA)


# ---------------------------------------------------------------------------
# ID 命名規範（SRC-SAI §34）
# ---------------------------------------------------------------------------


def test_id_formats_match_the_specification_examples():
    assert real_recording_id("Bubbly", 7) == "real_tof_bubbly_0007"
    assert real_image_id("Misty", 211) == "real_rgb_misty_0211"
    assert (
        synthetic_scenario_id("Conflict-Stress", "Bubbly", 42)
        == "syn_conflictstress_bubbly_0042"
    )
    assert model_id("tof_1dcnn", 2, 3) == "tof_1dcnn_s02_r03"
    assert run_id("e2_formal", "20260825", 1) == "e2_formal_20260825_r01"
    assert (
        artifact_path("syn_clean_empty_0001", "rgb", "png")
        == "syn_clean_empty_0001/rgb.png"
    )


def test_severity_bearing_scenario_id():
    assert (
        synthetic_scenario_id("ToF-degraded", "Water-filled", 42, severity="Mid")
        == "syn_tofdegraded_water_mid_0042"
    )


def test_clean_condition_rejects_severity():
    """SRC-SAI §13：Clean 使用 seed strata 而非 severity。"""
    with pytest.raises(InvalidIdentifier, match="seed strata"):
        synthetic_scenario_id("Clean", "Empty", 1, severity="Low")


@pytest.mark.parametrize(
    "call",
    [
        lambda: real_recording_id("Unknown", 1),
        lambda: synthetic_scenario_id("Nonexistent", "Empty", 1),
        lambda: real_recording_id("Empty", -1),
        lambda: real_recording_id("Empty", 10000),
        lambda: model_id("ToF CNN", 1, 1),
        lambda: run_id("e2_formal", "2026-08-25", 1),
    ],
)
def test_invalid_identifier_inputs_fail_fast(call):
    with pytest.raises(InvalidIdentifier):
        call()


def test_canonical_ids_are_recognised_for_leakage_scanning():
    for identifier in (
        "real_tof_bubbly_0007",
        "real_rgb_misty_0211",
        "syn_conflictstress_bubbly_0042",
        "syn_tofdegraded_water_mid_0042",
        "tof_1dcnn_s02_r03",
        "e2_formal_20260825_r01",
    ):
        assert is_canonical_id(identifier), identifier


def test_opaque_hex_is_not_mistaken_for_a_canonical_id():
    assert not is_canonical_id("9f2c41ab7d0e5386bc194af027d3e5a1")
