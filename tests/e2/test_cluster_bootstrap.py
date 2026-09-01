# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；只測 pcmef.stats.bootstrap.cluster_bootstrap_delta
#         這個純函式，以構造好的 Final E2 形狀（32 family x 12 列）驗證；
#         不讀寫檔案、不連線、不碰 families 36-43 的任何資料。
# 檔案路徑: tests/e2/test_cluster_bootstrap.py
# 產生時間: 2026-09-01 11:05 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 statistics_config.lock 凍結的三條契約真的被實作 ——
#           family 為重抽單位、class 內分層、同一 replicate 跨方法共用 cluster。
# 模組定位: statistics_config.lock 的可執行防線。少了它，把 cluster 改回
#           scenario、或讓各方法各自重抽，都不會有任何測試失敗。
# 主要責任:
#   1. test_cluster_carries_all_twelve_rows 驗證抽一個 family 帶入 12 列
#   2. test_draw_is_stratified_by_class 驗證每個 replicate 的類別組成不變
#   3. test_identical_methods_give_exactly_zero_delta 證明跨方法共用 cluster
#   4. test_scenario_level_resampling_understates_variance 量化為什麼要用 family
#   5. test_cluster_spanning_two_strata_is_refused 擋下錯誤的 cluster 定義
#   6. test_same_seed_reproduces_the_interval 驗證可重現
#   7. test_replicates_have_no_default 驗證不得自行補值
# 維護提醒:
#   - 不得刪除或放寬 test_identical_methods_give_exactly_zero_delta。
#     它是「同一 replicate 跨方法共用 cluster」的唯一機械證據：若各方法
#     各自重抽，兩個完全相同的方法之間會出現非零的 CI。
#   - 不得刪除 test_scenario_level_resampling_understates_variance。
#     它是「為什麼重抽單位是 family 而不是 scenario」的唯一量化證據。
#   - v0.1.0 新增：首版，對應 NOTE-050。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_cluster_bootstrap.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.stats.bootstrap import BootstrapError, cluster_bootstrap_delta

CLASSES = ("Empty", "Water-filled", "Bubbly", "Misty")
FAMILIES = range(36, 44)
REALIZATIONS = 3
CONDITIONS = 4


def _final_e2_shape():
    """Final E2 的實際形狀：4 class x 8 family x 3 realization x 4 condition。"""
    clusters, strata, scenarios, labels = [], [], [], []
    for index, class_label in enumerate(CLASSES):
        for family in FAMILIES:
            for realization in range(REALIZATIONS):
                for _condition in range(CONDITIONS):
                    clusters.append(f"{class_label}|{family}")
                    strata.append(class_label)
                    scenarios.append(f"{class_label}|{family}|{realization}")
                    labels.append(index)
    return clusters, strata, scenarios, np.asarray(labels)


def _accuracy(truth: np.ndarray, predicted: np.ndarray) -> float:
    return float((truth == predicted).mean())


def test_cluster_carries_all_twelve_rows():
    clusters, strata, _scenarios, y = _final_e2_shape()
    result = cluster_bootstrap_delta(
        y, {"a": y, "b": y}, clusters, strata, _accuracy, "b",
        replicates=50, seed=20260827,
    )
    assert result["n_rows"] == 384
    assert result["n_clusters"] == 32
    # 3 realization x 4 condition。若有人把 cluster 改成 scenario，這裡會是 4。
    assert result["rows_per_cluster"] == [REALIZATIONS * CONDITIONS]


def test_draw_is_stratified_by_class():
    clusters, strata, _scenarios, y = _final_e2_shape()
    result = cluster_bootstrap_delta(
        y, {"a": y, "b": y}, clusters, strata, _accuracy, "b",
        replicates=50, seed=20260827,
    )
    assert result["clusters_per_stratum"] == {c: len(FAMILIES) for c in CLASSES}


def test_identical_methods_give_exactly_zero_delta():
    """兩個完全相同的方法之間，Delta 與 CI 必須恰為 0。

    這只有在「同一個 replicate 內所有方法共用同一組 cluster」時才成立。
    改成各方法各自重抽的話，這裡會出現非零的區間。
    """
    clusters, strata, _scenarios, y = _final_e2_shape()
    noisy = y.copy()
    noisy[::5] = (noisy[::5] + 1) % len(CLASSES)
    result = cluster_bootstrap_delta(
        y, {"a": noisy, "b": noisy.copy()}, clusters, strata, _accuracy, "b",
        replicates=200, seed=20260827,
    )
    comparison = result["comparisons"]["a_vs_b"]
    assert comparison["delta"] == 0.0
    assert comparison["ci_lower"] == 0.0
    assert comparison["ci_upper"] == 0.0
    assert comparison["significant_at_95"] is False


def test_scenario_level_resampling_understates_variance():
    """以 scenario 重抽會得到比 family 重抽更窄的 CI —— 那正是被禁止的理由。

    構造一個 family-level 的效果：整個 family 要嘛全對要嘛全錯。以 family
    重抽時變異來自「抽到幾個壞 family」；以 scenario 重抽時同一個 family 的
    三個 realization 被當成獨立樣本，n 虛增三倍而 CI 收窄。
    """
    clusters, strata, scenarios, y = _final_e2_shape()
    rng = np.random.default_rng(7)
    bad_families = set(
        rng.choice(sorted(set(clusters)), size=8, replace=False).tolist()
    )
    predicted = np.asarray(
        [
            (label + 1) % len(CLASSES) if cluster in bad_families else label
            for cluster, label in zip(clusters, y)
        ]
    )

    by_family = cluster_bootstrap_delta(
        y, {"a": predicted, "b": y}, clusters, strata, _accuracy, "b",
        replicates=2000, seed=20260827,
    )["comparisons"]["a_vs_b"]
    by_scenario = cluster_bootstrap_delta(
        y, {"a": predicted, "b": y}, scenarios, strata, _accuracy, "b",
        replicates=2000, seed=20260827,
    )["comparisons"]["a_vs_b"]

    family_width = by_family["ci_upper"] - by_family["ci_lower"]
    scenario_width = by_scenario["ci_upper"] - by_scenario["ci_lower"]
    assert family_width > scenario_width


def test_cluster_spanning_two_strata_is_refused():
    clusters, strata, _scenarios, y = _final_e2_shape()
    with pytest.raises(BootstrapError, match="spans strata"):
        cluster_bootstrap_delta(
            y, {"a": y, "b": y}, ["one"] * len(y), strata, _accuracy, "b",
            replicates=10, seed=20260827,
        )


def test_same_seed_reproduces_the_interval():
    clusters, strata, _scenarios, y = _final_e2_shape()
    # 效果必須是 family-level 的，否則每個 replicate 的準確率都相同、
    # CI 寬度恆為 0，而「換 seed 會得到不同區間」就無從檢驗。
    rng = np.random.default_rng(3)
    bad = set(rng.choice(sorted(set(clusters)), size=6, replace=False).tolist())
    noisy = np.asarray(
        [
            (label + 2) % len(CLASSES) if cluster in bad else label
            for cluster, label in zip(clusters, y)
        ]
    )
    kwargs = dict(
        cluster_ids=clusters, strata=strata, metric=_accuracy, reference="b",
        replicates=300, seed=20260827,
    )
    first = cluster_bootstrap_delta(y, {"a": noisy, "b": y}, **kwargs)
    second = cluster_bootstrap_delta(y, {"a": noisy, "b": y}, **kwargs)
    assert first["comparisons"] == second["comparisons"]

    different = cluster_bootstrap_delta(
        y, {"a": noisy, "b": y}, **{**kwargs, "seed": 20260831}
    )
    assert different["comparisons"] != first["comparisons"]


def test_replicates_have_no_default():
    clusters, strata, _scenarios, y = _final_e2_shape()
    with pytest.raises(BootstrapError, match="statistics_config.lock"):
        cluster_bootstrap_delta(
            y, {"a": y, "b": y}, clusters, strata, _accuracy, "b",
            replicates=0, seed=20260827,
        )


def test_row_alignment_is_enforced():
    clusters, strata, _scenarios, y = _final_e2_shape()
    with pytest.raises(BootstrapError, match="row-aligned"):
        cluster_bootstrap_delta(
            y, {"a": y, "b": y}, clusters[:-1], strata, _accuracy, "b",
            replicates=10, seed=20260827,
        )
