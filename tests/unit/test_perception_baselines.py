# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.perception.baselines；以合成張量驗證，不讀資料集、
#         不算圖、不碰 FORMAL_E1_FINAL。由 pytest 收集執行。
# 檔案路徑: tests/unit/test_perception_baselines.py
# 產生時間: 2026-08-31 12:55 +08:00
# 版本: v0.1.0
# 功能說明: 驗證度量算得對、融合權重只由 validation 決定、
#           以及訓練在固定 seed 下可重現。
# 模組定位: perception baseline 層的驗收。它不驗證模型好不好，
#           只驗證報出來的數字是照定義算的、且沒有偷看 test。
# 主要責任:
#   1. test_confusion_and_macro_f1_match_hand_worked_values() 度量正確
#   2. test_fusion_weight_is_selected_on_validation_only() 不看 test
#   3. test_fusion_tie_rule_prefers_equal_weighting() 平手取等權
#   4. test_training_is_reproducible_under_a_fixed_seed() 可重現
#   5. test_models_emit_a_probability_simplex() 輸出是機率
# 維護提醒:
#   - 不得放寬「融合權重只由 validation 決定」這條；用 test 挑權重會讓
#     test 分數不再是一次性的。
#   - 不得把 macro-F1 的空類別記成 nan 再於平均時忽略；那會讓一個
#     從未被預測的類別無聲消失。
#   - v0.1.0 新增：perception baseline 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_perception_baselines.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, N_CLASSES
from pcmef.perception.baselines import (
    ToFCNN,
    VisionCNN,
    confusion_matrix,
    evaluate,
    fit_fusion_weight,
    fuse,
    macro_f1,
    predict_proba,
    train_model,
)

torch = pytest.importorskip("torch")


def test_confusion_and_macro_f1_match_hand_worked_values():
    y_true = np.array([0, 0, 1, 1, 2, 3])
    y_pred = np.array([0, 1, 1, 1, 2, 3])
    matrix = confusion_matrix(y_true, y_pred)
    assert matrix.tolist() == [[1, 1, 0, 0], [0, 2, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

    macro, per_class = macro_f1(matrix)
    # class0: tp1 fp0 fn1 -> 2/(2+0+1)=0.6667 ; class1: tp2 fp1 fn0 -> 4/(4+1+0)=0.8
    assert per_class[0] == pytest.approx(2 / 3)
    assert per_class[1] == pytest.approx(0.8)
    assert per_class[2] == 1.0 and per_class[3] == 1.0
    assert macro == pytest.approx(np.mean(per_class))


def test_macro_f1_scores_an_unpredicted_class_as_zero_not_nan():
    """一個從未被預測也從未出現的類別，其 F1 是 0，不是「未定義」。"""
    matrix = np.zeros((N_CLASSES, N_CLASSES), dtype=int)
    matrix[0, 0] = 5
    macro, per_class = macro_f1(matrix)
    assert per_class[1:] == [0.0, 0.0, 0.0]
    assert np.isfinite(macro)


def test_evaluate_reports_every_required_field():
    rng = np.random.default_rng(0)
    proba = rng.dirichlet(np.ones(N_CLASSES), size=40)
    labels = rng.integers(0, N_CLASSES, size=40)
    report = evaluate("probe", proba, labels)
    for key in (
        "accuracy", "macro_f1", "per_class_f1", "confusion_matrix",
        "mean_probability_of_true_class", "mean_probability_by_true_class",
    ):
        assert key in report
    assert set(report["per_class_f1"]) == set(CLASS_ORDER)
    assert np.array(report["confusion_matrix"]).sum() == 40


# ---------------------------------------------------------------------------
# 融合
# ---------------------------------------------------------------------------


def _one_hot(indices: np.ndarray) -> np.ndarray:
    out = np.full((len(indices), N_CLASSES), 0.01)
    out[np.arange(len(indices)), indices] = 0.97
    return out


def test_fusion_weight_is_selected_on_validation_only():
    """ToF 在 val 上全對、vision 全錯 -> 權重必須偏向 ToF（w 小）。"""
    labels = np.array([0, 1, 2, 3] * 5)
    tof = _one_hot(labels)
    vision = _one_hot((labels + 1) % N_CLASSES)
    result = fit_fusion_weight(vision, tof, labels)
    assert result["selected_on"] == "validation split only"
    assert result["selected_w"] < 0.5
    assert result["val_accuracy_at_selected_w"] == 1.0


def test_fusion_tie_rule_prefers_equal_weighting():
    """兩個模態都全對時每個 w 都一樣好，平手規則必須取 0.5。"""
    labels = np.array([0, 1, 2, 3] * 5)
    perfect = _one_hot(labels)
    result = fit_fusion_weight(perfect, perfect.copy(), labels)
    assert result["selected_w"] == pytest.approx(0.5)
    assert result["n_tied"] > 1


def test_fuse_is_a_convex_combination():
    rng = np.random.default_rng(1)
    a = rng.dirichlet(np.ones(N_CLASSES), size=7)
    b = rng.dirichlet(np.ones(N_CLASSES), size=7)
    blended = fuse(a, b, 0.3)
    assert np.allclose(blended.sum(1), 1.0)
    assert np.all(blended >= 0)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory, shape",
    [(VisionCNN, (6, 3, 64, 64)), (ToFCNN, (6, 4, 500))],
)
def test_models_emit_a_probability_simplex(factory, shape):
    torch.manual_seed(0)
    model = factory()
    model.eval()
    proba = predict_proba(model, np.random.default_rng(0).normal(size=shape).astype(np.float32))
    assert proba.shape == (shape[0], N_CLASSES)
    assert np.allclose(proba.sum(1), 1.0)
    assert np.all(proba >= 0)


def test_training_is_reproducible_under_a_fixed_seed():
    rng = np.random.default_rng(5)
    x = rng.normal(size=(24, 4, 500)).astype(np.float32)
    y = np.tile(np.arange(N_CLASSES), 6).astype(np.int64)
    xv = rng.normal(size=(8, 4, 500)).astype(np.float32)
    yv = np.tile(np.arange(N_CLASSES), 2).astype(np.int64)

    first, history_a = train_model(ToFCNN, x, y, xv, yv, epochs=3, seed=123)
    second, history_b = train_model(ToFCNN, x, y, xv, yv, epochs=3, seed=123)
    assert history_a == history_b
    assert np.allclose(predict_proba(first, xv), predict_proba(second, xv))
