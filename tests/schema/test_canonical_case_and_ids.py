# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；操作 core.schema、core.ids 與 core.constants；
#         全部使用合成陣列與參數，不需要真實資料，也不做任何 I/O。
# 檔案路徑: tests/schema/test_canonical_case_and_ids.py
# 產生時間: 2026-08-25 21:25 +08:00
# 版本: v0.1.0
# 功能說明: 把規格文件裡五個已知的資料對齊雷點（Sigma 尺度、時間軸混用、欄位順序
#           錯位、真實 RGB/ToF 非同步、計數帳不清）逐條變成建構時就會失敗的斷言，
#           並驗證六種識別碼的格式與文件範例完全一致。
# 模組定位: 資料契約的回歸防線。它不檢查資料內容合不合理，只檢查結構與命名。
# 主要責任:
#   1. 四特徵 canonical 順序與 display 順序的分離
#   2. CanonicalCase 對 500x4 形狀、NaN、取樣點數一致性的拒絕路徑
#   3. 雙時間軸與 measurement_time.source 的必要性
#   4. CountStatus 的單調收斂關係與 physical > logical 的合法情形
#   5. ID 格式與 SRC-SAI 34 節、Appendix A1 範例字串的逐字比對
#   6. Clean condition 不得帶 severity
# 維護提醒:
#   - 不得為了讓某筆資料通過而放寬 shape 或 NaN 檢查；缺值要標 invalid 進
#     exclusion ledger，不是補零。
#   - TOF_SCHEMA 順序若被更動，本檔的 canonical_order 測試必須先失敗，
#     不得反過來配合實作調整期望值。
#   - v0.1.0 新增：首版資料契約與命名回歸測試。
# 驗證方式:
#   - py -3.10 -m pytest tests/schema/test_canonical_case_and_ids.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import (
    CLASS_ORDER,
    LEGACY_CSV_COLUMN_TITLES,
    LEGACY_LABEL_MAP,
    LEGACY_METRIC_FOLDERS,
    SIGMA_RAW_SCALE_DIVISOR,
    SIGMA_REGISTER_CANDIDATES,
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
    MetricAlignment,
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


def _alignment(row_count: int = 500) -> tuple[MetricAlignment, ...]:
    """四個 metric 各自的來源檔案 provenance（SRC-NOTION 的四資料夾結構）。"""
    return tuple(
        MetricAlignment(
            metric=metric,
            source_filename=f"{folder}/measurement_7.csv",
            row_count=row_count,
            source_sha256=f"{index}" * 64,
        )
        for index, (metric, folder) in enumerate(
            zip(TOF_SCHEMA, LEGACY_METRIC_FOLDERS)
        )
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
        metric_alignment=_alignment(),
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
    """legacy handoff 以四個 metric 分檔保存，physical count 因此可大於 logical count。

    SRC-NOTION 的合併程式證實資料組織為 <condition>/<metric>/*.csv，
    因此 4 類 x 140 recordings x 4 metric 檔 = 2240 個實體檔案，
    對應 4 x 140 = 560 個 logical recordings。
    """
    status = CountStatus(560, 2240, 552, 548, 540)
    assert status.physical_source_files > status.nominal_logical_recordings
    assert status.physical_source_files == 560 * len(LEGACY_METRIC_FOLDERS)


# ---------------------------------------------------------------------------
# 與 SRC-NOTION 一手來源的逐字比對
# ---------------------------------------------------------------------------


def test_canonical_order_matches_the_original_feature_flatten_code():
    """SRC-NOTION `extract_features_from_window()` 的 flatten 順序即 canonical 順序。

    原程式碼並附有作者註解「順序：Distance, Ambient, Signal, Sigma /
    根據 Edge Impulse 訓練時的特徵順序一致」，是 NOTE-001 的一手依據。
    """
    source_flatten_order = ("distance", "ambient", "signal", "sigma")
    canonical_prefixes = tuple(
        name.split("_")[0] if not name.startswith("sigma") else "sigma"
        for name in TOF_SCHEMA
    )
    assert canonical_prefixes == source_flatten_order


def test_legacy_csv_titles_align_with_canonical_schema():
    """合併程式的 column_order 與 TOF_SCHEMA 必須逐欄對應。"""
    assert len(LEGACY_CSV_COLUMN_TITLES) == len(TOF_SCHEMA)
    for title, canonical in zip(LEGACY_CSV_COLUMN_TITLES, TOF_SCHEMA):
        head = title.split(" ")[0].lower()
        assert canonical.startswith(head), f"{title!r} does not match {canonical!r}"


def test_metric_folders_align_with_canonical_schema():
    assert len(LEGACY_METRIC_FOLDERS) == len(TOF_SCHEMA)
    for folder, canonical in zip(LEGACY_METRIC_FOLDERS, TOF_SCHEMA):
        assert canonical.startswith(folder)


def test_sigma_register_discrepancy_is_recorded_as_unresolved():
    """SRC-D01：來源同時存在 0x18 與 0x1E，系統不得預設其一為正確。"""
    assert set(SIGMA_REGISTER_CANDIDATES) == {0x18, 0x1E}
    assert SIGMA_RAW_SCALE_DIVISOR == 65536.0


# ---------------------------------------------------------------------------
# Metric alignment 與 Sigma provenance（NOTE-010）
# ---------------------------------------------------------------------------


def test_real_recording_requires_metric_alignment():
    """對齊靠排序位置是靜默錯位的來源，必須逐 metric 保存原始檔名。"""
    with pytest.raises(SchemaViolation, match="metric_alignment"):
        _real_case(metric_alignment=None)


def test_metric_alignment_must_cover_all_four_metrics():
    with pytest.raises(SchemaViolation, match="must cover exactly"):
        _real_case(metric_alignment=_alignment()[:3])


def test_metric_alignment_row_count_disagreement_is_rejected():
    """四個 metric 檔長度不一致代表它們不是同一次測量，不得靜默裁切。"""
    entries = list(_alignment())
    entries[2] = MetricAlignment(
        metric=entries[2].metric,
        source_filename=entries[2].source_filename,
        row_count=498,
        source_sha256=entries[2].source_sha256,
    )
    with pytest.raises(SchemaViolation, match="row counts disagree"):
        _real_case(metric_alignment=tuple(entries))


def test_metric_alignment_requires_the_original_filename():
    with pytest.raises(SchemaViolation, match="original source filename"):
        MetricAlignment(
            metric="distance_mm", source_filename="", row_count=500, source_sha256="a" * 64
        )


def test_metric_alignment_rejects_unknown_metric():
    with pytest.raises(SchemaViolation, match="must be one of"):
        MetricAlignment(
            metric="temperature",
            source_filename="t/measurement_1.csv",
            row_count=500,
            source_sha256="a" * 64,
        )


def test_real_case_requires_explicit_sigma_status():
    """SRC-SAI E1-G08：Sigma provenance 未解析不得計算四特徵 primary。"""
    with pytest.raises(SchemaViolation, match="sigma_status"):
        _real_case(provenance={"source_manifest_hash": "a" * 64})


def test_sigma_status_must_be_a_known_value():
    with pytest.raises(SchemaViolation, match="sigma_status must be one of"):
        _real_case(
            provenance={"source_manifest_hash": "a" * 64, "sigma_status": "probably_ok"}
        )


def test_unresolved_sigma_is_representable_without_blocking_canonicalisation():
    """UNRESOLVED 必須可表達 —— M0 盤點階段本來就還沒解析完 Sigma。

    阻擋點在 E1-G08，不在 canonical 化；若這裡就擋死，M0 根本無法盤點。
    """
    case = _real_case(
        provenance={"source_manifest_hash": "a" * 64, "sigma_status": "UNRESOLVED"}
    )
    assert case.provenance["sigma_status"] == "UNRESOLVED"


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
