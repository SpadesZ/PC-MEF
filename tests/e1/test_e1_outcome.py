# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 tests/e1/conftest.py 的 lock 鏈驅動
#         pcmef.experiments.e1_outcome；lock 寫在 tmp_path。
# 檔案路徑: tests/e1/test_e1_outcome.py
# 產生時間: 2026-08-27 15:25 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 PASS/DEGRADED 的三條判定是 AND 關係、規則只能來自已凍結的
#           lock、以及降級後下游口徑會被強制改掉。
# 模組定位: §12.1 E1 Scientific Outcome Gate 的可執行防線。
# 主要責任:
#   1. test_all_three_conditions_must_hold 逐條驗證 AND 關係
#   2. test_rule_comes_only_from_the_lock 擋下「看過結果再換門檻」
#   3. test_outcome_lock_is_immutable 驗證不可重判
#   4. test_claim_mode_* 驗證降級強制改口徑
#   5. test_a_degraded_outcome_still_freezes 驗證失敗也要留下紀錄
# 維護提醒:
#   - 不得把 DEGRADED 測成「錯誤」。它是合法且必須被凍結的結論，
#     §12.1 規定它只是降級 claim，不是流程失敗。
#   - v0.1.0 新增：首版，對應 NOTE-024。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1/test_e1_outcome.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.core.locks import LockError
from pcmef.experiments.e1 import E1Engine
from pcmef.experiments.e1_outcome import (
    CLAIM_MODE,
    E1_SCIENTIFIC_DEGRADED,
    E1_SCIENTIFIC_PASS,
    OutcomeError,
    ScientificRule,
    assert_claim_mode,
    evaluate_outcome,
    freeze_outcome,
)


def _result(store, real_heldout, pair, scales, **kwargs):
    initial, calibrated = pair
    return E1Engine(store).evaluate(
        real_heldout=real_heldout, initial=initial, calibrated=calibrated,
        scales=scales, replicates=kwargs.pop("replicates", 400),
        seed=kwargs.pop("seed", 20260826), **kwargs,
    )


# ---------------------------------------------------------------------------
# 規則來源
# ---------------------------------------------------------------------------


def test_rule_comes_only_from_the_lock(ready_store):
    rule = ScientificRule.from_lock(ready_store)
    assert rule.macro_mean_delta_ci_lower_bound_gt == 0.0
    assert rule.per_feature_class_macro_delta_gte == 0.0
    assert rule.distance_trend_consistency == "non_degraded"
    assert rule.bootstrap_replicates == 500
    assert rule.bootstrap_seed == 20260826


def test_a_rule_lock_without_aggregation_is_refused(lock_chain):
    lock_chain.write("e1_scientific_rule", {
        "normalization_scales": "x", "aggregation": "not-a-mapping",
        "improvement_threshold": 0.0, "regression_tolerance": 0.0,
        "trend_rule": "non_degraded", "bootstrap_replicates": 10,
        "bootstrap_seed": 1, "code_hash": "h",
        "amendment_id": "AMD-001",
        "amendment_payload_hash": "a" * 64,
        "g08_contract_version": "v2",
    })
    with pytest.raises(OutcomeError, match="aggregation mapping"):
        ScientificRule.from_lock(lock_chain)


def test_a_rule_lock_missing_a_condition_is_refused(lock_chain):
    lock_chain.write("e1_scientific_rule", {
        "normalization_scales": "x",
        "aggregation": {"macro_mean_delta_ci_lower_bound_gt": 0.0},
        "improvement_threshold": 0.0, "regression_tolerance": 0.0,
        "trend_rule": "non_degraded", "bootstrap_replicates": 10,
        "bootstrap_seed": 1, "code_hash": "h",
        "amendment_id": "AMD-001",
        "amendment_payload_hash": "a" * 64,
        "g08_contract_version": "v2",
    })
    with pytest.raises(OutcomeError, match="missing PASS condition"):
        ScientificRule.from_lock(lock_chain)


# ---------------------------------------------------------------------------
# 三條 AND
# ---------------------------------------------------------------------------


def test_a_clear_improvement_passes(ready_store, real_heldout, improving_pair, scales):
    result = _result(ready_store, real_heldout, improving_pair, scales)
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))

    assert decision.outcome == E1_SCIENTIFIC_PASS
    assert decision.claim_mode == "tof_physics_calibrated"
    assert decision.failed_conditions() == ()


def test_a_regression_degrades(ready_store, real_heldout, regressing_pair, scales):
    result = _result(ready_store, real_heldout, regressing_pair, scales)
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))

    assert decision.outcome == E1_SCIENTIFIC_DEGRADED
    assert decision.claim_mode == "synthetic_testbed"
    assert "macro_mean_delta_ci_lower_bound_gt" in decision.failed_conditions()


def test_a_single_regressed_feature_blocks_the_pass(
    ready_store, real_heldout, improving_pair, scales, make_candidate
):
    """整體變好但某個特徵變差，是拿一個特徵換另一個 —— 不算 fidelity 提升。"""
    initial, calibrated = improving_pair
    # 讓 calibrated 在 distance_mm 上刻意變遠。
    broken_values = {
        cls: dict(features) for cls, features in calibrated.values.items()
    }
    for cls in broken_values:
        broken_values[cls]["distance_mm"] = [
            v + 40.0 for v in broken_values[cls]["distance_mm"]
        ]
    from dataclasses import replace

    result = _result(
        ready_store, real_heldout,
        (initial, replace(calibrated, values=broken_values)), scales,
    )
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))

    assert "per_feature_class_macro_delta_gte" in decision.failed_conditions()
    assert decision.outcome == E1_SCIENTIFIC_DEGRADED


def test_a_degraded_trend_blocks_the_pass(
    ready_store, real_heldout, improving_pair, scales
):
    result = _result(
        ready_store, real_heldout, improving_pair, scales,
        trend_real={"baseline": 98.61, "minus_0p1cm": 91.05, "plus_0p1cm": 88.76},
        trend_calibrated={"baseline": 100.0, "minus_0p1cm": 108.0, "plus_0p1cm": 112.0},
    )
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))

    assert "distance_trend_consistency" in decision.failed_conditions()
    assert decision.outcome == E1_SCIENTIFIC_DEGRADED


def test_an_inapplicable_trend_does_not_block(
    ready_store, real_heldout, improving_pair, scales
):
    """沒有 offset 資料時趨勢不適用 —— 那與「比了但不一致」是兩回事。"""
    result = _result(ready_store, real_heldout, improving_pair, scales)
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))
    assert not result.trend.applicable
    assert "distance_trend_consistency" not in decision.failed_conditions()


def test_every_condition_is_reported_with_a_reason(
    ready_store, real_heldout, improving_pair, scales
):
    result = _result(ready_store, real_heldout, improving_pair, scales)
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))
    assert len(decision.conditions) == 3
    assert all(detail for _, _, detail in decision.conditions)
    assert len(decision.lines()) == 3


# ---------------------------------------------------------------------------
# 凍結與 claim mode
# ---------------------------------------------------------------------------


def test_outcome_is_frozen_with_the_result_hashes(
    ready_store, real_heldout, improving_pair, scales
):
    result = _result(ready_store, real_heldout, improving_pair, scales)
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))
    ready_store.write("claim_boundary", {
        "e1_fidelity_scope": "tof_sensor_surrogate",
        "synthetic_rgb_statement": "paired evidence only",
    })

    payload_hash = freeze_outcome(ready_store, decision, result)

    payload = ready_store.load("e1_outcome")
    assert payload["outcome"] == E1_SCIENTIFIC_PASS
    assert payload["claim_mode"] == "tof_physics_calibrated"
    assert payload["result_hashes"]["e1_result"] == result.result_hash()
    assert len(payload_hash) == 64


def test_a_degraded_outcome_still_freezes(
    ready_store, real_heldout, regressing_pair, scales
):
    """失敗也要留下紀錄 —— 不凍結就等於這次評估沒發生過。"""
    result = _result(ready_store, real_heldout, regressing_pair, scales)
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))
    ready_store.write("claim_boundary", {
        "e1_fidelity_scope": "tof_sensor_surrogate",
        "synthetic_rgb_statement": "paired evidence only",
    })

    freeze_outcome(ready_store, decision, result)

    assert ready_store.load("e1_outcome")["claim_mode"] == "synthetic_testbed"


def test_outcome_cannot_be_refrozen_with_a_different_verdict(
    ready_store, real_heldout, improving_pair, regressing_pair, scales
):
    """結果不理想就重跑一次 final，會在這一步被擋下。"""
    ready_store.write("claim_boundary", {
        "e1_fidelity_scope": "tof_sensor_surrogate",
        "synthetic_rgb_statement": "paired evidence only",
    })
    good = _result(ready_store, real_heldout, improving_pair, scales)
    freeze_outcome(ready_store, evaluate_outcome(good, ScientificRule.from_lock(ready_store)), good)

    bad = _result(ready_store, real_heldout, regressing_pair, scales)
    with pytest.raises(LockError, match="immutable"):
        freeze_outcome(
            ready_store, evaluate_outcome(bad, ScientificRule.from_lock(ready_store)), bad
        )


def test_outcome_requires_the_claim_boundary_lock_first(
    ready_store, real_heldout, improving_pair, scales
):
    """§23 state machine：e1_outcome 的前置是 e1_scientific_rule + claim_boundary。"""
    result = _result(ready_store, real_heldout, improving_pair, scales)
    decision = evaluate_outcome(result, ScientificRule.from_lock(ready_store))
    with pytest.raises(LockError, match="claim_boundary"):
        freeze_outcome(ready_store, decision, result)


def test_claim_mode_mapping_matches_the_specification():
    assert CLAIM_MODE[E1_SCIENTIFIC_PASS] == "tof_physics_calibrated"
    assert CLAIM_MODE[E1_SCIENTIFIC_DEGRADED] == "synthetic_testbed"


def test_downstream_must_inherit_the_locked_claim_mode(
    ready_store, real_heldout, regressing_pair, scales
):
    """降級後報告層不得再用未限定範圍的 physics-calibrated 措辭。"""
    ready_store.write("claim_boundary", {
        "e1_fidelity_scope": "tof_sensor_surrogate",
        "synthetic_rgb_statement": "paired evidence only",
    })
    result = _result(ready_store, real_heldout, regressing_pair, scales)
    freeze_outcome(
        ready_store, evaluate_outcome(result, ScientificRule.from_lock(ready_store)), result
    )

    assert_claim_mode(ready_store, "synthetic_testbed")
    with pytest.raises(OutcomeError, match="must inherit"):
        assert_claim_mode(ready_store, "tof_physics_calibrated")
