# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；操作 provenance.sigma 與 provenance.timing；
#         全部使用合成觀測值，不依賴 data/raw_real/ 是否存在。
# 檔案路徑: tests/provenance/test_sigma_and_timing.py
# 產生時間: 2026-08-26 04:05 +08:00
# 版本: v0.1.0
# 功能說明: 驗證兩件事不會發生 —— 系統在缺採集程式碼證據時擅自認定 Sigma
#           暫存器，以及把文件記載或腳本設定的取樣間隔當成實測值使用。
# 模組定位: SRC-D01/D02/D03 證據產生器的驗收測試。它不驗證真實資料的內容，
#           只驗證判定邏輯在各種證據組合下的行為。
# 主要責任:
#   1. scaling 假設在值域、16-bit 上限與整數性下的判定
#   2. 缺採集證據時 status 必須為 UNRESOLVED
#   3. 有採集證據但無出處時仍不得 RESOLVED
#   4. 時間軸推導的穩定度與非單調拒絕
#   5. 三來源偏差計算與雙時間軸分離敘述
# 維護提醒:
#   - 不得放寬「缺 evidence 即 UNRESOLVED」那幾條；它們是 E1-G08 的唯一自動防線。
#   - 不得加入以整數性判定 scaling 的測試；來源 CSV 已四捨五入，該檢定必然誤判。
#   - v0.1.0 新增：首版 provenance 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/provenance/test_sigma_and_timing.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import SIGMA_RAW_SCALE_DIVISOR, SIGMA_REGISTER_CANDIDATES
from pcmef.provenance.sigma import (
    SigmaProvenanceError,
    resolve_sigma,
    evaluate_scaling_hypotheses,
)
from pcmef.provenance.timing import (
    EvidenceLevel,
    TimingProvenanceError,
    audit_timing,
    measure_interval,
)

# 與真實資料同量級的合成 Sigma 觀測值（實測範圍約 0.356 - 0.664 mm）。
REAL_LIKE_SIGMA = np.linspace(0.356, 0.664, 500)


# ---------------------------------------------------------------------------
# Sigma scaling
# ---------------------------------------------------------------------------


def test_sub_millimetre_values_are_consistent_only_with_65536():
    hypotheses = {h.divisor: h for h in evaluate_scaling_hypotheses(REAL_LIKE_SIGMA)}
    assert hypotheses[SIGMA_RAW_SCALE_DIVISOR].plausible
    assert hypotheses[SIGMA_RAW_SCALE_DIVISOR].fits_16bit
    # 未縮放假設要求觀測值為整數，實際為小數，必須被排除。
    assert not hypotheses[1.0].plausible
    # /128 會讓隱含原始值只佔滿量程的極小角落。
    assert not hypotheses[128.0].plausible


def test_values_exceeding_16bit_under_a_divisor_are_rejected():
    huge = np.full(10, 2.0)
    hypotheses = {h.divisor: h for h in evaluate_scaling_hypotheses(huge)}
    assert not hypotheses[SIGMA_RAW_SCALE_DIVISOR].fits_16bit
    assert not hypotheses[SIGMA_RAW_SCALE_DIVISOR].plausible


def test_integer_observations_are_consistent_with_no_scaling():
    integers = np.array([12.0, 48.0, 130.0, 900.0])
    hypotheses = {h.divisor: h for h in evaluate_scaling_hypotheses(integers)}
    assert hypotheses[1.0].plausible


def test_empty_observations_are_rejected():
    with pytest.raises(SigmaProvenanceError):
        evaluate_scaling_hypotheses(np.array([np.nan, np.inf]))


# ---------------------------------------------------------------------------
# Sigma 整體判定
# ---------------------------------------------------------------------------


#: 已排除「sigma 欄即測距值」的檢定結果，對應 channel_semantics CONFIRMED。
RULED_OUT_TEST = {
    "hypothesis": "sigma column was read from the register holding the range",
    "match_ratio": 0.0,
    "correlation": -0.476,
    "magnitude_ratio": 298.5,
    "ruled_out": True,
}


def test_register_address_stays_conflict_without_acquisition_code():
    """AMD-001：位址無法由數值反推，缺 rank-2 採集程式碼即 CONFLICT。"""
    resolution = resolve_sigma(REAL_LIKE_SIGMA, range_register_test=RULED_OUT_TEST)
    assert resolution.facets["register_address"].status == "CONFLICT"
    assert resolution.facets["original_acquisition_method"].status == "UNKNOWN"


def test_channel_and_scale_confirmed_resolve_the_gate_despite_register_conflict():
    """AMD-001 的核心行為：位址 CONFLICT **不得**擋下 E1-G08。"""
    resolution = resolve_sigma(REAL_LIKE_SIGMA, range_register_test=RULED_OUT_TEST)
    assert resolution.facets["channel_semantics"].status == "CONFIRMED"
    assert resolution.facets["numeric_scale"].status == "CONFIRMED"
    assert resolution.facets["register_address"].status == "CONFLICT"
    assert resolution.status == "RESOLVED"
    assert resolution.blocking_reasons == ()


def test_channel_semantics_unknown_blocks_the_gate():
    """沒有排除檢定就沒有 channel semantics —— 這時 G08 必須擋下。

    這條與上一條成對存在。少了它，AMD-001 就只是把 gate 變成橡皮圖章。
    """
    resolution = resolve_sigma(REAL_LIKE_SIGMA)
    assert resolution.facets["channel_semantics"].status == "UNKNOWN"
    assert resolution.status == "UNRESOLVED"


def test_channel_semantics_unknown_when_range_hypothesis_survives():
    """排除檢定跑了但沒排除掉，等同未證成，不得算 CONFIRMED。"""
    survived = {**RULED_OUT_TEST, "ruled_out": False, "match_ratio": 0.99}
    resolution = resolve_sigma(REAL_LIKE_SIGMA, range_register_test=survived)
    assert resolution.facets["channel_semantics"].status == "UNKNOWN"
    assert resolution.status == "UNRESOLVED"


def test_unresolvable_scale_blocks_the_gate():
    """沒有任何除數與觀測相容時，numeric_scale 不得為 CONFIRMED。

    值為非整數（排除 /1），且 x128 後已超出 16-bit（連帶排除 /65536），
    三個候選全滅 —— 此時必須誠實回報 UNKNOWN 並擋下 gate。
    """
    impossible = np.linspace(0.5, 600.0, 500)
    resolution = resolve_sigma(impossible, range_register_test=RULED_OUT_TEST)
    assert resolution.facets["numeric_scale"].status == "UNKNOWN"
    assert resolution.resolved_divisor is None
    assert resolution.status == "UNRESOLVED"
    assert any("no scaling hypothesis" in r for r in resolution.blocking_reasons)


def test_acquisition_register_without_evidence_is_still_blocked():
    """只填一個數字不算證據；必須記錄它出自哪份檔案哪一行。"""
    resolution = resolve_sigma(
        REAL_LIKE_SIGMA, acquisition_register=0x1E, range_register_test=RULED_OUT_TEST
    )
    assert resolution.status == "UNRESOLVED"
    assert any("without evidence" in r for r in resolution.blocking_reasons)


def test_acquisition_code_confirms_the_register_at_rank_two():
    """取得 rank-2 採集程式碼後，位址才可能 CONFIRMED。"""
    resolution = resolve_sigma(
        REAL_LIKE_SIGMA,
        acquisition_register=0x1E,
        acquisition_evidence="collect_dataset.py:31 REG_RESULT_SIGMA_MM = 0x1E",
        range_register_test=RULED_OUT_TEST,
    )
    facet = resolution.facets["register_address"]
    assert facet.status == "CONFIRMED"
    assert facet.value == "0x1e"
    assert facet.evidence_rank == 2
    assert resolution.status == "RESOLVED"


def test_artifact_carries_facets_contract_version_and_amendment():
    artifact = resolve_sigma(
        REAL_LIKE_SIGMA, range_register_test=RULED_OUT_TEST
    ).to_artifact()
    assert artifact["gate"] == "E1-G08"
    assert artifact["contract_version"] == "v2"
    assert artifact["amendment"] == "AMD-001"
    assert artifact["required_facets"] == ["channel_semantics", "numeric_scale"]
    assert artifact["facets"]["numeric_scale"]["value"] == SIGMA_RAW_SCALE_DIVISOR
    assert artifact["facets"]["register_address"]["status"] == "CONFLICT"
    # 位址不在 required 內 —— 這是 AMD-001 的重點，寫成斷言以免日後被悄悄加回去。
    assert "register_address" not in artifact["required_facets"]


def test_reconstructed_does_not_satisfy_the_gate():
    """RECONSTRUCTED 是推論不是證據，不得滿足 gate（NOTE-028 維護邊界）。"""
    from pcmef.core.constants import PROVENANCE_GATE_SATISFYING

    assert "RECONSTRUCTED" not in PROVENANCE_GATE_SATISFYING
    assert PROVENANCE_GATE_SATISFYING == ("CONFIRMED",)


# ---------------------------------------------------------------------------
# 時間軸
# ---------------------------------------------------------------------------


def test_measured_interval_matches_a_regular_axis():
    axis = np.arange(500) * 0.0624
    evidence = measure_interval(axis)
    assert evidence.level is EvidenceLevel.MEASURED
    assert evidence.value_s == pytest.approx(0.0624, rel=1e-9)
    assert evidence.detail["stable"] is True
    assert evidence.detail["duration_s"] == pytest.approx(499 * 0.0624, rel=1e-6)


def test_unstable_axis_is_reported_as_unstable_not_averaged_away():
    rng = np.random.default_rng(5)
    axis = np.cumsum(rng.uniform(0.001, 0.4, size=400))
    evidence = measure_interval(axis)
    assert evidence.detail["stable"] is False


def test_non_monotonic_axis_is_rejected():
    axis = np.array([0.0, 0.1, 0.05, 0.2])
    with pytest.raises(TimingProvenanceError, match="strictly increasing"):
        measure_interval(axis)


def test_too_short_axis_is_rejected():
    with pytest.raises(TimingProvenanceError):
        measure_interval(np.array([0.0]))


def test_documented_value_discrepancy_is_quantified():
    """0.082 對上實測 0.0624 約 +31%，正是硬編常數會造成的失真幅度。"""
    measured = measure_interval(np.arange(500) * 0.0624)
    provenance = audit_timing(measured, documented_s=0.082, script_nominal_s=0.02)
    assert provenance.discrepancies["documented"] == pytest.approx(31.4, abs=0.5)
    assert provenance.discrepancies["script_nominal"] == pytest.approx(-67.9, abs=0.5)


def test_artifact_keeps_the_two_axes_separate():
    measured = measure_interval(np.arange(100) * 0.0624)
    artifact = audit_timing(measured, documented_s=0.082).to_artifact()
    assert set(artifact["axes"]) == {"optical_transient_time", "measurement_time"}
    assert "不得直接切成" in artifact["axes"]["optical_transient_time"]["forbidden"]
    assert artifact["measured"]["level"] == "measured"


def test_audit_without_measurement_reports_no_discrepancies():
    """沒有實測值時不得憑文件值計算偏差，那只是兩個未經驗證的數字相減。"""
    provenance = audit_timing(None, documented_s=0.082, script_nominal_s=0.02)
    assert provenance.measured is None
    assert provenance.discrepancies == {}
