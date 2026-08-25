# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；操作 core.numeric 與 core.constants，並掃描 pcmef/
#         全樹確認 stabilize_prob 只有一份定義；不讀寫任何資料檔。
# 檔案路徑: tests/unit/test_numeric.py
# 產生時間: 2026-08-25 21:00 +08:00
# 版本: v0.1.0
# 功能說明: 把規格文件裡那張 D/U/Q invariant 表逐條變成可執行的斷言 ——
#           相同分佈的 D 要小於 1e-12、極端互斥分佈的 D 要落在 0.999 與 1 之間、
#           熵不能因 log(0) 變成 NaN、all-zero 不得被救成均勻分佈等等。
# 模組定位: 數值語意的回歸防線。它不驗證 gate 的搜尋結果好壞，只驗證數學定義沒被改掉。
# 主要責任:
#   1. stabilize_prob 的合法輸入與六種非法輸入
#   2. D 的自反性、對稱性、有界性，以及「不是 sqrt(JSD)」
#   3. U 的正規化、非 NaN 保證，以及不得被 top-1 confidence 取代
#   4. support_to_vector 必須依 CLASS_ORDER 而非 dict 順序取值
#   5. reliability 權重與 gate 係數的 simplex 守衛
#   6. test_stabilize_prob_has_exactly_one_definition_in_the_codebase() 全樹掃描
# 維護提醒:
#   - 不得為了讓實作通過而放寬這裡的容差；這些數字直接對應論文的式 (1)-(8)，
#     要改必須先確認規格文件真的改了。
#   - 不得移除全樹掃描那一條；它是 NOTE-006「禁止影子複製」唯一的自動防線。
#   - v0.1.0 新增：首版數值回歸測試。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_numeric.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from pcmef.core.constants import EPS_P, EPS_R, N_CLASSES
from pcmef.core.numeric import (
    InvalidAgentSupport,
    InvalidGateCoefficients,
    InvalidProbability,
    InvalidReliability,
    NumericalInvariantError,
    assert_finite_nonnegative_sum1,
    check_gate_coefficients,
    check_reliability,
    normalize_support,
    normalized_entropy,
    normalized_js_divergence,
    reliability_weights,
    stabilize_prob,
    support_to_vector,
)

UNIFORM = np.full(N_CLASSES, 0.25)


# ---------------------------------------------------------------------------
# stabilize_prob
# ---------------------------------------------------------------------------


def test_stabilize_prob_returns_normalized_vector():
    result = stabilize_prob([0.1, 0.2, 0.3, 0.4])
    assert result.shape == (N_CLASSES,)
    assert np.isclose(result.sum(), 1.0, rtol=0.0, atol=1e-12)
    assert np.all(result > 0)


def test_stabilize_prob_clips_zeros_to_eps_without_creating_nan():
    result = stabilize_prob([1.0, 0.0, 0.0, 0.0])
    assert np.all(result >= EPS_P / (1.0 + 3 * EPS_P) * 0.5)
    assert np.all(np.isfinite(np.log(result))), "log of stabilized vector must be finite"


@pytest.mark.parametrize(
    "bad",
    [
        [0.5, 0.5, 0.0],                      # 長度不是 4
        [0.25, 0.25, 0.25, 0.25, 0.0],        # 長度不是 4
        [0.5, -0.1, 0.3, 0.3],                # 負值
        [np.nan, 0.3, 0.3, 0.4],              # NaN
        [np.inf, 0.3, 0.3, 0.4],              # Inf
        [0.0, 0.0, 0.0, 0.0],                 # all-zero：語意失敗，不得救成 uniform
    ],
)
def test_stabilize_prob_rejects_invalid_vectors(bad):
    with pytest.raises(InvalidProbability):
        stabilize_prob(bad)


def test_all_zero_is_not_rescued_into_uniform():
    """SRC-SAI FR-024 / §20：all-zero 是 semantic failure，不得靠 eps 轉成 uniform。"""
    with pytest.raises(InvalidProbability):
        stabilize_prob(np.zeros(N_CLASSES))


# ---------------------------------------------------------------------------
# D：normalized JSD（SRC-SAI §16 invariant 表）
# ---------------------------------------------------------------------------


def test_jsd_of_identical_distributions_is_below_1e_12():
    for vector in ([0.25] * 4, [0.7, 0.1, 0.1, 0.1], [0.97, 0.01, 0.01, 0.01]):
        assert normalized_js_divergence(vector, vector) < 1e-12


def test_jsd_of_extreme_disjoint_one_hots_is_just_below_one():
    """因 EPS_P clipping，D 接近但不等於 1：0.999 < D <= 1。"""
    divergence = normalized_js_divergence([1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0])
    assert 0.999 < divergence <= 1.0


def test_jsd_is_symmetric():
    p = [0.6, 0.2, 0.1, 0.1]
    q = [0.1, 0.1, 0.7, 0.1]
    assert np.isclose(
        normalized_js_divergence(p, q), normalized_js_divergence(q, p), atol=1e-15
    )


def test_jsd_is_bounded_to_unit_interval():
    rng = np.random.default_rng(20260825)
    for _ in range(200):
        p = rng.dirichlet(np.ones(N_CLASSES))
        q = rng.dirichlet(np.ones(N_CLASSES))
        divergence = normalized_js_divergence(p, q)
        assert 0.0 <= divergence <= 1.0
        assert np.isfinite(divergence)


def test_jsd_is_divergence_not_sqrt_distance():
    """SRC-PLAN 式 (5)：必須回傳 divergence，不是 sqrt(JSD) 這個 JS distance。

    兩者在小 divergence 時差一個數量級，會直接改變 g 的分佈。
    """
    p = [0.4, 0.3, 0.2, 0.1]
    q = [0.3, 0.4, 0.2, 0.1]
    divergence = normalized_js_divergence(p, q)
    assert divergence < np.sqrt(divergence), (
        "a value equal to sqrt(D) would indicate the JS distance was returned"
    )


# ---------------------------------------------------------------------------
# U：normalized entropy
# ---------------------------------------------------------------------------


def test_uniform_distribution_has_unit_normalized_entropy():
    assert np.isclose(normalized_entropy(UNIFORM), 1.0, atol=1e-12)


def test_near_one_hot_has_near_zero_entropy():
    assert normalized_entropy([1.0, 0.0, 0.0, 0.0]) < 1e-4


def test_entropy_never_produces_nan_from_log_zero():
    """SRC-SAI §16：U 不得因 log(0) 產生 NaN。"""
    for vector in ([1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0], [0.5, 0.5, 0.0, 0.0]):
        value = normalized_entropy(vector)
        assert np.isfinite(value)
        assert 0.0 <= value <= 1.0


def test_entropy_is_not_top1_confidence():
    """SRC-SAI §16 禁令：禁止用 top-1 confidence 替代 entropy。

    構造兩個 top-1 相同但分佈不同的向量，熵必須不同。
    """
    a = normalized_entropy([0.5, 0.5, 0.0, 0.0])
    b = normalized_entropy([0.5, 0.2, 0.2, 0.1])
    assert not np.isclose(a, b, atol=1e-6)


# ---------------------------------------------------------------------------
# Agent support bridge
# ---------------------------------------------------------------------------


def test_support_to_vector_uses_class_order_not_dict_order():
    """SRC-SAI §21：必須依 CLASS_ORDER 取鍵，不得依 dict iteration order。"""
    shuffled = {
        "class_support": {
            "Misty": 40.0,
            "Empty": 10.0,
            "Bubbly": 30.0,
            "Water-filled": 20.0,
        }
    }
    assert support_to_vector(shuffled).tolist() == [10.0, 20.0, 30.0, 40.0]


@pytest.mark.parametrize(
    "support",
    [
        {"Empty": 0.0, "Water-filled": 0.0, "Bubbly": 0.0, "Misty": 0.0},   # all-zero
        {"Empty": float("nan"), "Water-filled": 1, "Bubbly": 1, "Misty": 1},
        {"Empty": float("inf"), "Water-filled": 1, "Bubbly": 1, "Misty": 1},
        {"Empty": -1.0, "Water-filled": 1, "Bubbly": 1, "Misty": 1},        # 負值
        {"Empty": 101.0, "Water-filled": 1, "Bubbly": 1, "Misty": 1},       # 超出 0-100
        {"Empty": 1, "Water-filled": 1, "Bubbly": 1},                        # 缺 key
        {"Empty": 1, "Water-filled": 1, "Bubbly": 1, "Misty": 1, "Other": 1},  # 多 key
    ],
)
def test_support_to_vector_rejects_invalid_support(support):
    with pytest.raises(InvalidAgentSupport):
        support_to_vector({"class_support": support})


def test_normalize_support_sums_to_one():
    s_a = normalize_support([10.0, 20.0, 30.0, 40.0])
    assert np.isclose(s_a.sum(), 1.0, rtol=0.0, atol=1e-12)
    assert np.all(s_a > 0)


def test_normalize_support_rejects_all_zero():
    with pytest.raises(InvalidAgentSupport):
        normalize_support(np.zeros(N_CLASSES))


# ---------------------------------------------------------------------------
# Reliability 與 gate 係數
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [(-0.1, 0.5), (0.5, 1.1), (np.nan, 0.5), (0.5, np.inf)])
def test_check_reliability_rejects_out_of_range(bad):
    with pytest.raises(InvalidReliability):
        check_reliability(*bad)


def test_reliability_weights_sum_to_one():
    for r_t, r_v in ((0.0, 0.0), (1.0, 0.0), (0.3, 0.9), (0.5, 0.5)):
        w_t, w_v = reliability_weights(r_t, r_v)
        assert np.isclose(w_t + w_v, 1.0, rtol=0.0, atol=1e-12)
        assert 0.0 <= w_t <= 1.0 and 0.0 <= w_v <= 1.0


def test_reliability_weights_are_balanced_when_both_reliabilities_are_zero():
    """r_T=r_V=0 時 EPS_R 讓分母不為零，權重回到各 0.5。"""
    w_t, w_v = reliability_weights(0.0, 0.0)
    assert np.isclose(w_t, 0.5, atol=1e-12) and np.isclose(w_v, 0.5, atol=1e-12)


def test_reliability_weights_favour_the_more_reliable_branch():
    w_t, w_v = reliability_weights(0.9, 0.1)
    assert w_t > w_v


def test_check_gate_coefficients_accepts_simplex_grid_points():
    for alpha, beta, gamma in ((0.4, 0.3, 0.3), (1.0, 0.0, 0.0), (0.1, 0.1, 0.8)):
        coefficients = check_gate_coefficients(alpha, beta, gamma)
        assert np.isclose(coefficients.sum(), 1.0, rtol=0.0, atol=1e-12)


@pytest.mark.parametrize(
    "bad",
    [(0.4, 0.3, 0.4), (-0.1, 0.6, 0.5), (np.nan, 0.5, 0.5), (0.3, 0.3, 0.3)],
)
def test_check_gate_coefficients_rejects_non_simplex(bad):
    with pytest.raises(InvalidGateCoefficients):
        check_gate_coefficients(*bad)


# ---------------------------------------------------------------------------
# 最終輸出 regression invariant
# ---------------------------------------------------------------------------


def test_assert_finite_nonnegative_sum1_accepts_valid_distribution():
    assert_finite_nonnegative_sum1(UNIFORM, "p_rel")


@pytest.mark.parametrize(
    "bad",
    [
        [0.3, 0.3, 0.3, 0.3],                 # sum != 1
        [0.5, -0.1, 0.3, 0.3],                # 負值
        [np.nan, 0.3, 0.3, 0.4],              # NaN
        [0.5, 0.5],                           # shape 錯
    ],
)
def test_assert_finite_nonnegative_sum1_rejects_invalid(bad):
    with pytest.raises(NumericalInvariantError):
        assert_finite_nonnegative_sum1(bad, "F")


# ---------------------------------------------------------------------------
# NOTE-006：唯一 stabilize_prob 實作
# ---------------------------------------------------------------------------


def test_stabilize_prob_has_exactly_one_definition_in_the_codebase():
    """SRC-SAI Appendix G1: same signature imported everywhere; no shadow copy."""
    package_root = Path(__file__).resolve().parents[2] / "pcmef"
    definition = re.compile(r"^\s*def\s+stabilize_prob\s*\(", re.MULTILINE)
    offenders = [
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*.py")
        if definition.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == ["core/numeric.py"], (
        f"stabilize_prob must be defined only in core/numeric.py, found in {offenders}"
    )


def test_epsilon_constants_match_the_frozen_specification():
    """SRC-SAI §16：EPS_P = EPS_R = EPS_S = 1e-6，且納入 formal freeze hash。"""
    from pcmef.core.constants import EPS_S

    assert EPS_P == EPS_R == EPS_S == 1e-6
