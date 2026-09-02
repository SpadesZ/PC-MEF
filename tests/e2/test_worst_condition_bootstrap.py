# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 stats.bootstrap.cluster_bootstrap_worst_condition_delta
#         以及它與既有 cluster_bootstrap_delta 的關係。純數值，不需資料集。
# 檔案路徑: tests/e2/test_worst_condition_bootstrap.py
# 產生時間: 2026-09-02 21:05 +08:00
# 版本: v0.1.0
# 功能說明: 釘住 primary endpoint 的成對 CI：重抽與 overall estimator 完全
#           相同，但 minimum 在每個 replicate 內取。
# 模組定位: P1-4 / AMD-009 的回歸測試。它同時守住兩件事：新 estimator 的
#           行為，以及**舊 estimator 一個位元都沒動** ——
#           statistics_config.lock 綁定舊函式的原始碼雜湊。
# 主要責任:
#   1. test_the_existing_estimator_is_byte_identical lock 綁定的 hash 不變
#   2. test_it_shares_the_resampling_with_the_overall_estimator
#   3. test_the_minimum_is_taken_inside_each_replicate
#   4. test_the_weakest_condition_varies_across_replicates 這是它存在的理由
#   5. test_overall_and_worst_can_disagree
# 維護提醒:
#   - 不得為了共用程式碼而把 cluster_bootstrap_delta 重構掉。
#     statistics_config.lock 記的是它的 hash_object(inspect.getsource(...))；
#     改動函式本體（連 docstring）都會讓既有 lock 失效（AMD-009）。
#   - 不得改用「取某個固定 condition 的 CI」當作 worst 的 CI。
#     argmin 會隨 replicate 換人，那正是 worst 不確定性的一部分。
#   - v0.1.0 新增：首版，對應 P1-4。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_worst_condition_bootstrap.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect

import numpy as np
import pytest

from pcmef.core.hash import hash_object
from pcmef.stats.bootstrap import (
    BootstrapError,
    WORST_CONDITION_BOOTSTRAP_VERSION,
    cluster_bootstrap_delta,
    cluster_bootstrap_worst_condition_delta,
)

CONDITIONS = ("clean", "vision_degraded", "tof_degraded", "conflict")
CLASSES = ("Empty", "Water-filled", "Bubbly", "Misty")

#: statistics_config.lock 記載的 cluster_bootstrap_delta 原始碼雜湊。
LOCKED_OVERALL_ESTIMATOR_HASH = (
    "1d8c80a65ab83dde4ea80d6ae73926d119a1465688f2ec4a53989d0ada509bb3"
)


def _macro_f1(truth: np.ndarray, predicted: np.ndarray) -> float:
    scores = []
    for klass in range(len(CLASSES)):
        tp = int(((truth == klass) & (predicted == klass)).sum())
        fp = int(((truth != klass) & (predicted == klass)).sum())
        fn = int(((truth == klass) & (predicted != klass)).sum())
        scores.append(0.0 if 2 * tp + fp + fn == 0 else 2 * tp / (2 * tp + fp + fn))
    return float(np.mean(scores))


@pytest.fixture()
def pool():
    """4 class x 4 family x 4 condition x 3 realization = 192 列。"""
    clusters, strata, conditions, truth = [], [], [], []
    for index, klass in enumerate(CLASSES):
        for family in range(4):
            for condition in CONDITIONS:
                for _ in range(3):
                    clusters.append(f"{klass}_f{family}")
                    strata.append(klass)
                    conditions.append(condition)
                    truth.append(index)
    return np.array(truth), clusters, strata, conditions


def _predict(truth, conditions, accuracy_by_condition, seed=1):
    rng = np.random.default_rng(seed)
    predicted = truth.copy()
    for index in range(len(truth)):
        if rng.random() > accuracy_by_condition[conditions[index]]:
            predicted[index] = (predicted[index] + 1) % len(CLASSES)
    return predicted


# ---------------------------------------------------------------------------
# 舊 estimator 必須原封不動
# ---------------------------------------------------------------------------


def test_the_existing_estimator_is_byte_identical():
    """statistics_config.lock 綁定它的原始碼雜湊。

    新增 worst-condition estimator 的整個前提就是「不動舊的」——
    改動它的函式本體，連 docstring，都會讓既有 lock 失效。
    """
    digest = hash_object(inspect.getsource(cluster_bootstrap_delta))
    assert digest == LOCKED_OVERALL_ESTIMATOR_HASH, (
        "cluster_bootstrap_delta changed; statistics_config.lock's code_sha256 "
        "no longer matches and the frozen lineage is invalidated"
    )


# ---------------------------------------------------------------------------
# 與 overall estimator 共用重抽
# ---------------------------------------------------------------------------


def test_it_shares_the_resampling_contract(pool):
    truth, clusters, strata, conditions = pool
    predictions = {
        "a": _predict(truth, conditions, dict.fromkeys(CONDITIONS, 0.9)),
        "b": _predict(truth, conditions, dict.fromkeys(CONDITIONS, 0.7), seed=2),
    }
    overall = cluster_bootstrap_delta(
        truth, predictions, clusters, strata, _macro_f1, "b",
        replicates=200, seed=20260827,
    )
    worst = cluster_bootstrap_worst_condition_delta(
        truth, predictions, clusters, strata, conditions, _macro_f1, "b",
        replicates=200, seed=20260827,
    )
    for field in ("n_clusters", "clusters_per_stratum", "rows_per_cluster",
                  "replicates", "seed", "n_rows", "shared_clusters_across_methods"):
        assert overall[field] == worst[field], field
    assert worst["version"] == WORST_CONDITION_BOOTSTRAP_VERSION
    assert worst["version"] != overall["version"]


def test_identical_methods_give_exactly_zero(pool):
    """成對設計的定義：同一組列上兩個相同的方法沒有差異也沒有變異。"""
    truth, clusters, strata, conditions = pool
    same = _predict(truth, conditions, dict.fromkeys(CONDITIONS, 0.8))
    worst = cluster_bootstrap_worst_condition_delta(
        truth, {"a": same, "b": same.copy()}, clusters, strata, conditions,
        _macro_f1, "b", replicates=200, seed=20260827,
    )
    comparison = worst["comparisons"]["a_vs_b"]
    assert comparison["delta"] == 0.0
    assert comparison["ci_lower"] == 0.0
    assert comparison["ci_upper"] == 0.0


# ---------------------------------------------------------------------------
# 這個 estimator 存在的理由
# ---------------------------------------------------------------------------


def test_the_minimum_is_taken_inside_each_replicate(pool):
    """觀測值必須等於 per-condition 的 min，而不是 overall。"""
    truth, clusters, strata, conditions = pool
    predicted = _predict(
        truth, conditions,
        {"clean": 0.99, "vision_degraded": 0.99, "tof_degraded": 0.99,
         "conflict": 0.10},
    )
    worst = cluster_bootstrap_worst_condition_delta(
        truth, {"a": predicted, "b": predicted.copy()}, clusters, strata,
        conditions, _macro_f1, "b", replicates=50, seed=20260827,
    )
    condition_array = np.asarray(conditions)
    by_condition = {
        name: _macro_f1(truth[condition_array == name], predicted[condition_array == name])
        for name in CONDITIONS
    }
    assert worst["observed"]["a"] == pytest.approx(min(by_condition.values()))
    assert worst["observed_worst_condition"]["a"] == "conflict"
    # 而 overall 明顯高於它 —— 否則這條測試分不出兩者。
    assert _macro_f1(truth, predicted) > worst["observed"]["a"] + 0.3


def test_the_weakest_condition_varies_across_replicates(pool):
    """argmin 會換人 —— 這正是不能拿單一 condition 的 CI 代替的理由。"""
    truth, clusters, strata, conditions = pool
    balanced = _predict(truth, conditions, dict.fromkeys(CONDITIONS, 0.70))
    worst = cluster_bootstrap_worst_condition_delta(
        truth, {"a": balanced, "b": balanced.copy()}, clusters, strata,
        conditions, _macro_f1, "b", replicates=500, seed=20260827,
    )
    counts = worst["reference_worst_condition_counts"]
    assert sum(counts.values()) == 500
    non_zero = [name for name, count in counts.items() if count > 0]
    assert len(non_zero) > 1, (
        "the weakest condition never changed across 500 replicates; if that were "
        "generally true, a fixed-condition CI would suffice and this estimator "
        "would not be needed"
    )


def test_overall_and_worst_can_disagree(pool):
    """一個方法可以在平均上打平、在最弱條件上顯著落後。

    這是 primary endpoint 必須有自己的 CI 的實證：只報 overall 的 CI 會
    讓「最弱條件崩潰」完全看不見。
    """
    truth, clusters, strata, conditions = pool
    flashy = _predict(
        truth, conditions,
        {"clean": 0.99, "vision_degraded": 0.99, "tof_degraded": 0.99,
         "conflict": 0.10},
    )
    steady = _predict(
        truth, conditions,
        {"clean": 0.72, "vision_degraded": 0.70, "tof_degraded": 0.71,
         "conflict": 0.69},
        seed=3,
    )
    predictions = {"flashy": flashy, "steady": steady}
    overall = cluster_bootstrap_delta(
        truth, predictions, clusters, strata, _macro_f1, "steady",
        replicates=500, seed=20260827,
    )["comparisons"]["flashy_vs_steady"]
    worst = cluster_bootstrap_worst_condition_delta(
        truth, predictions, clusters, strata, conditions, _macro_f1, "steady",
        replicates=500, seed=20260827,
    )["comparisons"]["flashy_vs_steady"]

    assert overall["significant_at_95"] is False
    assert worst["significant_at_95"] is True
    assert worst["ci_upper"] < 0.0, "worst-condition must show the collapse"


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------


def test_misaligned_conditions_are_refused(pool):
    truth, clusters, strata, conditions = pool
    predicted = _predict(truth, conditions, dict.fromkeys(CONDITIONS, 0.8))
    with pytest.raises(BootstrapError) as error:
        cluster_bootstrap_worst_condition_delta(
            truth, {"a": predicted, "b": predicted}, clusters, strata,
            conditions[:-1], _macro_f1, "b", replicates=10, seed=1,
        )
    assert "row-aligned" in str(error.value)


def test_zero_replicates_are_refused(pool):
    truth, clusters, strata, conditions = pool
    predicted = _predict(truth, conditions, dict.fromkeys(CONDITIONS, 0.8))
    with pytest.raises(BootstrapError):
        cluster_bootstrap_worst_condition_delta(
            truth, {"a": predicted, "b": predicted}, clusters, strata,
            conditions, _macro_f1, "b", replicates=0, seed=1,
        )


def test_a_cluster_spanning_strata_is_refused(pool):
    """cluster 必須屬於恰好一個 stratum，與 overall estimator 同一條規則。"""
    truth, clusters, strata, conditions = pool
    broken = list(strata)
    broken[0] = "SomethingElse"
    predicted = _predict(truth, conditions, dict.fromkeys(CONDITIONS, 0.8))
    with pytest.raises(BootstrapError) as error:
        cluster_bootstrap_worst_condition_delta(
            truth, {"a": predicted, "b": predicted}, clusters, broken,
            conditions, _macro_f1, "b", replicates=10, seed=1,
        )
    assert "exactly one stratum" in str(error.value)
