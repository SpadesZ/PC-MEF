# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 tests/e1/conftest.py 的 lock 鏈與合成候選
#         驅動 pcmef.experiments.e1；lock 寫在 tmp_path，不碰 repo 的 freeze/。
# 檔案路徑: tests/e1/test_e1_engine.py
# 產生時間: 2026-08-27 15:10 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 E1 引擎守得住三件事 —— 沒鎖規則不准碰 held-out、
#           兩個候選必須跑在同一組隨機實現上、跨特徵只准平均無單位量。
# 模組定位: Appendix H2 與 Appendix B 在 E1 執行層的可執行防線。
# 主要責任:
#   1. test_evaluation_requires_the_locks_first 對應 Appendix H2 的順序要求
#   2. test_mismatched_* 對應 matched evaluation design 的兩種破壞
#   3. test_delta_direction 驗證 Delta 的正負號語意
#   4. test_macro_mean_uses_only_dimensionless 驗證不平均 raw W1
#   5. test_degenerate_feature_blocks 驗證 s_f 退化時中止
# 維護提醒:
#   - 不得為了讓測試好寫而讓 evaluate() 自己去開 held-out；
#     它只接受已取出的資料，那道用途守門留在 core.splits。
#   - v0.1.0 新增：首版，對應 NOTE-024。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1/test_e1_engine.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.experiments.e1 import (
    E1DesignError,
    E1Engine,
    E1Error,
    FeatureScales,
)
from pcmef.stats.metrics import FeatureScaleError


def _evaluate(store, real_heldout, pair, scales, **kwargs):
    initial, calibrated = pair
    return E1Engine(store).evaluate(
        real_heldout=real_heldout, initial=initial, calibrated=calibrated,
        scales=scales, replicates=kwargs.pop("replicates", 400),
        seed=kwargs.pop("seed", 20260826), **kwargs,
    )


# ---------------------------------------------------------------------------
# 前置順序（Appendix H2）
# ---------------------------------------------------------------------------


def test_evaluation_requires_the_locks_first(lock_chain, real_heldout, improving_pair, scales):
    """規則必須早於 held-out 開啟；順序反過來就是看過答案才定規則。"""
    with pytest.raises(E1Error, match="requires these locks first"):
        _evaluate(lock_chain, real_heldout, improving_pair, scales)


def test_evaluation_runs_once_every_lock_is_frozen(ready_store, real_heldout, improving_pair, scales):
    result = _evaluate(ready_store, real_heldout, improving_pair, scales)
    assert len(result.cells) == len(CLASS_ORDER) * len(TOF_SCHEMA)
    assert set(result.extra["lock_hashes"]) == {
        "e1_candidates", "e1_evaluation_design", "e1_scientific_rule"
    }


def test_a_tampered_lock_stops_the_evaluation(ready_store, real_heldout, improving_pair, scales):
    path = ready_store.path_for("e1_scientific_rule")
    path.write_text(
        path.read_text(encoding="utf-8").replace("20260826", "99999"), encoding="utf-8"
    )
    with pytest.raises(E1Error, match="integrity check"):
        _evaluate(ready_store, real_heldout, improving_pair, scales)


# ---------------------------------------------------------------------------
# Matched evaluation design（Appendix B）
# ---------------------------------------------------------------------------


def test_mismatched_base_scenarios_are_refused(
    ready_store, real_heldout, scales, make_candidate
):
    """兩候選跑不同場景時，Delta 會混入 random realization 的差異。"""
    initial = make_candidate("initial", 8.0, 21)
    calibrated = make_candidate(
        "calibrated", 1.0, 22, scenarios=tuple(f"other_{i:04d}" for i in range(12))
    )
    with pytest.raises(E1DesignError, match="same base scenarios"):
        _evaluate(ready_store, real_heldout, (initial, calibrated), scales)


def test_mismatched_seed_matrix_is_refused(
    ready_store, real_heldout, scales, make_candidate
):
    initial = make_candidate("initial", 8.0, 21)
    calibrated = make_candidate("calibrated", 1.0, 22, seed_matrix="different-matrix")
    with pytest.raises(E1DesignError, match="seed"):
        _evaluate(ready_store, real_heldout, (initial, calibrated), scales)


def test_design_hash_covers_scenarios_and_seed_matrix(
    ready_store, real_heldout, improving_pair, scales
):
    result = _evaluate(ready_store, real_heldout, improving_pair, scales)
    assert len(result.design_hash) == 64


# ---------------------------------------------------------------------------
# Delta 語意與彙整
# ---------------------------------------------------------------------------


def test_delta_is_positive_when_calibration_moves_closer(
    ready_store, real_heldout, improving_pair, scales
):
    """Delta = NW_initial - NW_calibrated；正值代表校準把距離拉近。"""
    result = _evaluate(ready_store, real_heldout, improving_pair, scales)
    assert result.macro_mean_delta() > 0
    assert all(cell.nw_calibrated < cell.nw_initial for cell in result.cells)


def test_delta_is_negative_when_calibration_moves_away(
    ready_store, real_heldout, regressing_pair, scales
):
    result = _evaluate(ready_store, real_heldout, regressing_pair, scales)
    assert result.macro_mean_delta() < 0


def test_macro_mean_uses_only_dimensionless_quantities(
    ready_store, real_heldout, improving_pair, scales
):
    """§11 禁止平均不同物理單位的 raw W1；macro 平均必須來自 NW。"""
    result = _evaluate(ready_store, real_heldout, improving_pair, scales)
    expected = float(np.mean([c.nw_initial - c.nw_calibrated for c in result.cells]))
    assert result.macro_mean_delta() == pytest.approx(expected)
    # 而以 raw W1 算出的平均是另一個數 —— 兩者不該相等。
    raw_mean = float(np.mean([c.w1_initial - c.w1_calibrated for c in result.cells]))
    assert raw_mean != pytest.approx(result.macro_mean_delta())


def test_per_feature_macro_covers_every_feature(
    ready_store, real_heldout, improving_pair, scales
):
    per_feature = _evaluate(
        ready_store, real_heldout, improving_pair, scales
    ).per_feature_class_macro_delta()
    assert set(per_feature) == set(TOF_SCHEMA)


def test_nw_equals_w1_over_the_frozen_scale(
    ready_store, real_heldout, improving_pair, scales
):
    result = _evaluate(ready_store, real_heldout, improving_pair, scales)
    cell = result.cells[0]
    assert cell.nw_initial == pytest.approx(cell.w1_initial / cell.scale)
    assert cell.scale == 10.0


# ---------------------------------------------------------------------------
# 退化與缺項
# ---------------------------------------------------------------------------


def test_a_degenerate_feature_scale_blocks(ready_store, real_heldout, improving_pair):
    with pytest.raises(FeatureScaleError):
        FeatureScales.from_calibration({f: [1.0] * 40 for f in TOF_SCHEMA})


def test_missing_class_in_heldout_is_refused(
    ready_store, real_heldout, improving_pair, scales
):
    """少一類的話 macro 平均沒有定義。"""
    partial = {k: v for k, v in real_heldout.items() if k != "Misty"}
    with pytest.raises(E1Error, match="has no class"):
        _evaluate(ready_store, partial, improving_pair, scales)


def test_feature_scales_require_every_feature():
    with pytest.raises(FeatureScaleError, match="missing"):
        FeatureScales(scales={"distance_mm": 1.0})


def test_scales_from_calibration_uses_pooled_iqr():
    values = {f: list(range(101)) for f in TOF_SCHEMA}
    scales = FeatureScales.from_calibration(values)
    assert all(scales[f] == pytest.approx(50.0) for f in TOF_SCHEMA)


# ---------------------------------------------------------------------------
# 產物
# ---------------------------------------------------------------------------


def test_result_artifact_carries_everything_needed_to_audit(
    ready_store, real_heldout, improving_pair, scales
):
    result = _evaluate(ready_store, real_heldout, improving_pair, scales)
    artifact = result.to_artifact()
    for key in (
        "macro_mean_delta", "per_feature_class_macro_delta", "bootstrap",
        "trend", "feature_scales", "feature_scales_hash", "design_hash", "cells",
    ):
        assert key in artifact, key
    assert artifact["bootstrap"]["replicates"] == 400
    assert len(result.result_hash()) == 64


def test_metric_rows_are_flat_and_named(ready_store, real_heldout, improving_pair, scales):
    rows = _evaluate(ready_store, real_heldout, improving_pair, scales).to_rows()
    assert len(rows) == 16
    assert set(rows[0]) >= {
        "class_label", "feature", "w1_initial", "w1_calibrated",
        "feature_scale_s_f", "nw_initial", "nw_calibrated", "delta_nw",
    }
