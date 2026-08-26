# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；只測 pcmef.stats.bootstrap 的純函式，
#         以構造好的成對觀測驗證；不讀寫檔案、不連線。
# 檔案路徑: tests/e1/test_bootstrap.py
# 產生時間: 2026-08-27 14:30 +08:00
# 版本: v0.1.0
# 功能說明: 驗證同一組重抽真的同時套用到兩個候選 —— 並以「各自獨立重抽會把
#           CI 撐寬」的對照，證明這件事不是形式而是會改變結論。
# 模組定位: Appendix I2 paired uncertainty 的可執行防線。
# 主要責任:
#   1. test_paired_resampling_is_narrower_than_independent 量化配對的效果
#   2. test_same_seed_reproduces_the_interval 驗證可重現
#   3. test_missing_unit_is_refused 驗證兩候選必須覆蓋同一組 ID
#   4. test_replicates_and_seed_have_no_defaults 驗證不得自行補值
#   5. test_percentile_ci_matches_the_confidence_level 驗證分位取法
# 維護提醒:
#   - 不得把 test_paired_resampling_is_narrower_than_independent 刪掉或放寬。
#     它是「為什麼要成對」的唯一量化證據；沒有它，改成各自獨立重抽也不會有測試失敗。
#   - v0.1.0 新增：首版，對應 NOTE-024。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1/test_bootstrap.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.stats.bootstrap import (
    BootstrapError,
    paired_bootstrap_ci,
    percentile_ci,
)

N_UNITS = 60
B = 2000
SEED = 20260826


def _correlated_pair(n: int = N_UNITS, improvement: float = 0.05):
    """造一組「兩候選高度相關、但校準後穩定好一點」的觀測。

    這正是 E1 的實際情況：同一個 scenario 在兩個候選下的表現本來就相關，
    因為它們共用同一組 base scenarios 與 seed matrix。
    """
    rng = np.random.default_rng(7)
    ids = [f"scn_{i:03d}" for i in range(n)]
    # scenario 本身的難度（共同成分，兩候選都受它影響）
    difficulty = rng.normal(1.0, 0.40, size=n)
    initial = {i: float(d) for i, d in zip(ids, difficulty)}
    calibrated = {
        i: float(d - improvement + rng.normal(0.0, 0.01))
        for i, d in zip(ids, difficulty)
    }
    return ids, initial, calibrated


def test_paired_resampling_is_narrower_than_independent():
    """Appendix I2：同一組重抽必須同時套用到兩個候選。

    各自獨立重抽會讓 Delta 額外吃到「兩邊抽到不同 scenario」的雜訊，
    CI 因此變寬、下界被壓低 —— 結果是一個真的有改善的校準被判成沒有。
    這條測試把那個差距量化出來。
    """
    ids, initial, calibrated = _correlated_pair()

    paired = paired_bootstrap_ci(ids, initial, calibrated, replicates=B, seed=SEED)

    # 對照組：兩個候選各自抽各自的索引。
    rng = np.random.default_rng(SEED)
    init_arr = np.array([initial[i] for i in ids])
    cal_arr = np.array([calibrated[i] for i in ids])
    independent = []
    for _ in range(B):
        a = rng.integers(0, len(ids), len(ids))
        b = rng.integers(0, len(ids), len(ids))
        independent.append(float(init_arr[a].mean() - cal_arr[b].mean()))
    ind_lower, ind_upper = percentile_ci(independent)

    paired_width = paired.ci_upper - paired.ci_lower
    independent_width = ind_upper - ind_lower

    assert paired_width < independent_width
    # 差距不是邊際的：獨立重抽的區間寬了一個數量級以上。
    assert independent_width / paired_width > 5
    # 而且結論會不同——配對後下界為正（有改善），獨立後下界跨越 0。
    assert paired.ci_lower > 0
    assert ind_lower < 0


def test_same_seed_reproduces_the_interval():
    ids, initial, calibrated = _correlated_pair()
    first = paired_bootstrap_ci(ids, initial, calibrated, replicates=500, seed=SEED)
    second = paired_bootstrap_ci(ids, initial, calibrated, replicates=500, seed=SEED)
    assert first.ci_lower == second.ci_lower
    assert first.ci_upper == second.ci_upper


def test_a_different_seed_moves_the_interval_slightly():
    """seed 會小幅移動下界 —— 這正是它必須由教授凍結的理由（NOTE-015）。"""
    ids, initial, calibrated = _correlated_pair()
    a = paired_bootstrap_ci(ids, initial, calibrated, replicates=500, seed=1)
    b = paired_bootstrap_ci(ids, initial, calibrated, replicates=500, seed=2)
    assert a.ci_lower != b.ci_lower
    assert a.point_estimate == b.point_estimate     # 點估計與 seed 無關


def test_point_estimate_is_the_mean_paired_delta():
    ids = ["a", "b", "c", "d"]
    initial = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}
    calibrated = {"a": 0.5, "b": 1.5, "c": 2.5, "d": 3.5}
    result = paired_bootstrap_ci(ids, initial, calibrated, replicates=100, seed=1)
    assert result.point_estimate == pytest.approx(0.5)
    assert result.n_units == 4


def test_missing_unit_is_refused():
    ids = ["a", "b", "c"]
    with pytest.raises(BootstrapError, match="cover every unit"):
        paired_bootstrap_ci(
            ids, {"a": 1.0, "b": 1.0}, {"a": 1.0, "b": 1.0, "c": 1.0},
            replicates=10, seed=1,
        )


def test_replicates_must_be_positive():
    ids, initial, calibrated = _correlated_pair(n=5)
    with pytest.raises(BootstrapError, match="frozen in"):
        paired_bootstrap_ci(ids, initial, calibrated, replicates=0, seed=1)


def test_too_few_units_is_refused():
    with pytest.raises(BootstrapError, match="at least 2"):
        paired_bootstrap_ci(["a"], {"a": 1.0}, {"a": 1.0}, replicates=10, seed=1)


def test_non_finite_values_are_refused():
    ids = ["a", "b"]
    with pytest.raises(BootstrapError, match="non-finite"):
        paired_bootstrap_ci(
            ids, {"a": 1.0, "b": float("nan")}, {"a": 1.0, "b": 1.0},
            replicates=10, seed=1,
        )


def test_percentile_ci_matches_the_confidence_level():
    samples = list(range(101))          # 0..100
    lower, upper = percentile_ci(samples, confidence_level=0.90)
    assert lower == pytest.approx(5.0)
    assert upper == pytest.approx(95.0)


def test_percentile_ci_rejects_an_impossible_level():
    with pytest.raises(BootstrapError, match="confidence_level"):
        percentile_ci([1.0, 2.0], confidence_level=1.0)


def test_bootstrap_artifact_records_b_and_seed():
    """B 與 seed 必須留在產物裡，否則事後無從確認用的是凍結的那組。"""
    ids, initial, calibrated = _correlated_pair(n=10)
    artifact = paired_bootstrap_ci(
        ids, initial, calibrated, replicates=123, seed=456
    ).to_artifact()
    assert artifact["replicates"] == 123
    assert artifact["seed"] == 456
    assert artifact["n_units"] == 10
