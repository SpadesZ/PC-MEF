# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；只測 pcmef.stats.metrics 的純函式，
#         以手算得出答案的 toy example 驗證；不讀寫檔案、不連線。
#         本檔是 E1-G06 的證據來源（tests/e1_metrics.xml 由 --junitxml 產出）。
# 檔案路徑: tests/e1/test_metrics.py
# 產生時間: 2026-08-27 14:10 +08:00
# 版本: v0.1.0
# 功能說明: 用可以手算的例子驗證四個度量算對了 —— 特別是「不同單位的距離
#           不准直接平均」與「尺度退化時必須中止而不是套預設值」這兩條。
# 模組定位: SRC-SAI E1-G06「metrics unit tests 對 synthetic toy example 正確」
#           的可執行證據。
# 主要責任:
#   1. test_w1_* 以已知答案的分佈驗證 Wasserstein
#   2. test_feature_scale_* 驗證 IQR 與退化時的 BLOCK
#   3. test_normalized_wasserstein_* 驗證無單位化
#   4. test_trend_* 驗證方向與相對變化都要對才算一致
#   5. test_temporal_variability / test_class_distance_ordering 驗證 secondary
# 維護提醒:
#   - 不得把 toy example 換成「跑一次真實資料看看對不對」。這一層要的是
#     可手算的正確性，真實資料只能驗證「跑得動」。
#   - 不得放寬 FeatureScaleError 的斷言；退化特徵必須 BLOCK 而非套預設除數。
#   - v0.1.0 新增：首版，對應 NOTE-024。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1/test_metrics.py -v
#   - py -3.10 -m pytest tests/e1 --junitxml=tests/e1_metrics.xml   # E1-G06 證據
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.stats.metrics import (
    FeatureScaleError,
    MetricError,
    class_distance_ordering,
    feature_scale_iqr,
    mean_sd_error,
    normalized_wasserstein,
    temporal_variability,
    trend_consistency,
    wasserstein_w1,
)


# ---------------------------------------------------------------------------
# Wasserstein
# ---------------------------------------------------------------------------


def test_w1_of_identical_distributions_is_zero():
    values = [1.0, 2.0, 3.0, 4.0]
    assert wasserstein_w1(values, values) == pytest.approx(0.0, abs=1e-12)


def test_w1_of_a_constant_shift_equals_the_shift():
    """整體平移 c 的 W1 恰為 |c| —— 這是可手算的基準情況。"""
    base = [0.0, 1.0, 2.0, 3.0, 4.0]
    shifted = [v + 2.5 for v in base]
    assert wasserstein_w1(base, shifted) == pytest.approx(2.5, rel=1e-9)


def test_w1_is_symmetric():
    a, b = [0.0, 1.0, 2.0], [5.0, 6.0, 7.0]
    assert wasserstein_w1(a, b) == pytest.approx(wasserstein_w1(b, a))


def test_w1_rejects_empty_or_non_finite():
    with pytest.raises(MetricError, match="empty"):
        wasserstein_w1([], [1.0])
    with pytest.raises(MetricError, match="non-finite"):
        wasserstein_w1([1.0, np.nan], [1.0, 2.0])


# ---------------------------------------------------------------------------
# 特徵尺度
# ---------------------------------------------------------------------------


def test_feature_scale_is_the_interquartile_range():
    # 0..100 的均勻整數，IQR = 75 - 25 = 50。
    values = list(range(101))
    assert feature_scale_iqr(values) == pytest.approx(50.0)


def test_a_degenerate_feature_blocks_rather_than_defaults():
    """Appendix I2：s_f 必須 finite 且 > 0，否則 BLOCK。

    退回 1.0 會讓 NW 仍然算得出一個數字 —— 而那個數字沒有意義，
    卻會一路進到 macro 平均與 CI，最後變成論文裡的一格。
    """
    with pytest.raises(FeatureScaleError, match="not usable"):
        feature_scale_iqr([3.0] * 50)          # 全常數 -> IQR = 0


def test_feature_scale_rejects_non_finite_input():
    with pytest.raises(MetricError, match="non-finite"):
        feature_scale_iqr([1.0, 2.0, np.inf])


# ---------------------------------------------------------------------------
# 無單位化
# ---------------------------------------------------------------------------


def test_normalized_wasserstein_divides_by_the_scale():
    assert normalized_wasserstein(5.0, 2.5) == pytest.approx(2.0)


def test_normalized_wasserstein_refuses_a_non_positive_scale():
    with pytest.raises(FeatureScaleError):
        normalized_wasserstein(5.0, 0.0)
    with pytest.raises(FeatureScaleError):
        normalized_wasserstein(5.0, -1.0)


def test_normalization_makes_two_features_comparable():
    """同樣『差了半個 IQR』的兩個特徵，NW 必須相等 —— 即使單位差 1000 倍。

    這正是 §11 禁止直接平均 raw W1 的理由：下面兩個 raw W1 差了三個數量級，
    但它們代表的失真程度其實一樣。
    """
    distance_w1, distance_scale = 5.0, 10.0        # mm
    signal_w1, signal_scale = 0.005, 0.010         # MCPS
    assert normalized_wasserstein(distance_w1, distance_scale) == pytest.approx(
        normalized_wasserstein(signal_w1, signal_scale)
    )
    assert distance_w1 / signal_w1 == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Trend consistency
# ---------------------------------------------------------------------------


def _series(baseline: float, minus: float, plus: float) -> dict[str, float]:
    return {"baseline": baseline, "minus_0p1cm": minus, "plus_0p1cm": plus}


def test_trend_is_consistent_when_direction_and_magnitude_match():
    real = _series(98.61, 91.05, 88.76)
    synthetic = _series(100.0, 92.3, 90.0)
    result = trend_consistency(real, synthetic)
    assert result.applicable and result.consistent and not result.degraded


def test_trend_is_inconsistent_when_direction_flips():
    real = _series(98.61, 91.05, 88.76)      # 兩側都往下
    synthetic = _series(100.0, 108.0, 112.0)  # 兩側都往上
    result = trend_consistency(real, synthetic)
    assert result.applicable and not result.consistent and result.degraded


def test_trend_is_inconsistent_when_direction_matches_but_magnitude_collapses():
    """§11：不可只比單點，也不能只比方向。

    合成的方向對，但幅度只有真實的千分之一 —— 那不是「趨勢一致」，
    只是剛好沒有反向。
    """
    real = _series(98.61, 91.05, 88.76)
    synthetic = _series(100.0, 99.992, 99.990)
    result = trend_consistency(real, synthetic)
    assert result.applicable
    assert not result.consistent


def test_trend_not_applicable_without_a_baseline():
    result = trend_consistency({"plus_0p1cm": 1.0}, {"plus_0p1cm": 1.0})
    assert not result.applicable
    # 不適用不算降級 —— 沒得比與比了不一致是兩回事。
    assert not result.degraded


def test_trend_not_applicable_without_offset_levels():
    result = trend_consistency({"baseline": 1.0}, {"baseline": 1.0})
    assert not result.applicable and not result.degraded


# ---------------------------------------------------------------------------
# Secondary
# ---------------------------------------------------------------------------


def test_mean_sd_error_reports_signed_differences():
    real = [1.0, 2.0, 3.0]
    synthetic = [2.0, 3.0, 4.0]
    errors = mean_sd_error(real, synthetic)
    assert errors["mean_error"] == pytest.approx(1.0)
    assert errors["sd_error"] == pytest.approx(0.0)


def test_temporal_variability_describes_within_recording_behaviour():
    recording = np.array([[0.0, 10.0], [1.0, 10.0], [2.0, 10.0]])
    stats = temporal_variability(recording)
    assert stats["mean_abs_step"] == pytest.approx([1.0, 0.0])
    assert stats["range_per_feature"] == pytest.approx([2.0, 0.0])


def test_temporal_variability_needs_at_least_two_samples():
    with pytest.raises(MetricError, match="at least 2 samples"):
        temporal_variability(np.array([[1.0, 2.0]]))


def test_class_distance_ordering_sorts_by_median():
    ordering = class_distance_ordering({
        "Misty": [79.5, 80.0],
        "Empty": [100.9, 101.0],
        "Water-filled": [113.9, 114.0],
        "Bubbly": [105.6, 105.5],
    })
    # 與 SRC-PLAN §2.1 的錨點順序相符。
    assert ordering == ["Misty", "Empty", "Bubbly", "Water-filled"]
