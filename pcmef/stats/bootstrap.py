# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.experiments.e1 呼叫；輸入為以 recording/scenario ID 為鍵的
#         逐 candidate 觀測值，輸出信賴區間供 e1_outcome 判定 PASS/DEGRADED。
#         B 與 seed 由 e1_scientific_rule.lock 提供，本檔不自行選值。
# 檔案路徑: pcmef/stats/bootstrap.py
# 產生時間: 2026-08-27 13:20 +08:00
# 版本: v0.2.0
# 功能說明: 以重抽 recording/scenario 編號的方式估計信賴區間，
#           而且同一次重抽會同時套用到 Initial 與 Calibrated 兩個候選，
#           讓兩者的差異不含「抽到不同樣本」造成的雜訊。
# 模組定位: SRC-SAI Appendix I2 paired uncertainty 的實作。
#           它「不是」通用 bootstrap 工具 —— 簽章刻意要求成對輸入。
# 主要責任:
#   1. paired_bootstrap_ci() 對兩個候選套用同一組重抽索引
#   2. _resample_indices() 以 lock 提供的 seed 產生可重現的重抽
#   3. BootstrapResult 保存下界、上界、點估計與實際使用的 B/seed
#   4. percentile_ci() 依 2.5/97.5 分位取區間
#   5. cluster_bootstrap_delta() Final E2 的 family-cluster 成對重抽
# 維護提醒:
#   - cluster_bootstrap_delta() 的 cluster 必須是 physical_scene_family，
#     不是 scenario：同一個 family 的 3 realization x 4 condition 是同一個
#     物理場景的 12 個觀測，以 scenario 重抽會把它們當成獨立樣本而低估變異
#     （statistics_config.lock，NOTE-050）。
#   - 同一個 replicate 內所有比較方法必須共用同一組 cluster；各自重抽會讓
#     Delta 混入抽樣雜訊，那正是成對設計要消掉的東西。
#   - 不得對兩個候選各自獨立重抽。Appendix I2 明訂「E1 bootstrap resamples
#     recording/scenario IDs and applies the same resample to both candidates」；
#     各自重抽會讓 Delta 的變異灌入抽樣雜訊，CI 變寬而下界被壓低 ——
#     結果是一個真的有改善的校準被判成沒有。
#   - 不得以 500 個 measurement point 為重抽單位。推論單位是
#     recording/scenario（SRC-PLAN §1 的偽重複）；以點重抽會讓 n 虛增 500 倍，
#     CI 窄到幾乎必然「顯著」。
#   - 不得在此提供 B 或 seed 的預設值。兩者由教授核定並凍結在
#     e1_scientific_rule.lock（NOTE-015）；「換個 seed 看看」不是除錯手段。
#   - v0.2.0 新增：cluster_bootstrap_delta()，實作 statistics_config.lock 凍結的
#     family-cluster / class-stratified / 跨方法共用 cluster 契約（NOTE-050）。
#   - v0.1.0 新增：首版 paired bootstrap，決策見 NOTE-024。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1/test_bootstrap.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

import numpy as np

__all__ = [
    "BootstrapError",
    "BootstrapResult",
    "percentile_ci",
    "paired_bootstrap_ci",
    "cluster_bootstrap_delta",
]


class BootstrapError(ValueError):
    """重抽的前提不成立（ID 不成對、B/seed 缺失、樣本太少）。"""


@dataclass(frozen=True)
class BootstrapResult:
    """一次 paired bootstrap 的結果。"""

    point_estimate: float
    ci_lower: float
    ci_upper: float
    replicates: int
    seed: int
    confidence_level: float
    n_units: int

    def to_artifact(self) -> dict[str, object]:
        return {
            "point_estimate": self.point_estimate,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "replicates": self.replicates,
            "seed": self.seed,
            "confidence_level": self.confidence_level,
            "n_units": self.n_units,
        }


def percentile_ci(
    samples: Sequence[float], confidence_level: float = 0.95
) -> tuple[float, float]:
    """以百分位法取信賴區間。"""
    array = np.asarray(samples, dtype=np.float64)
    if array.size == 0:
        raise BootstrapError("no bootstrap replicates to summarise")
    if not 0.0 < confidence_level < 1.0:
        raise BootstrapError(
            f"confidence_level must lie in (0,1), got {confidence_level!r}"
        )
    tail = (1.0 - confidence_level) / 2.0 * 100.0
    lower, upper = np.percentile(array, [tail, 100.0 - tail])
    return float(lower), float(upper)


def _resample_indices(n: int, replicates: int, seed: int) -> np.ndarray:
    """產生 (replicates, n) 的重抽索引矩陣。

    一次產生整個矩陣而非逐次抽：同一個 seed 在任何機器上都得到同一個矩陣，
    而 E1 的 PASS/FAIL 直接由這個矩陣決定（NOTE-015）。
    """
    rng = np.random.default_rng(seed)
    return rng.integers(0, n, size=(replicates, n))


def paired_bootstrap_ci(
    unit_ids: Sequence[str],
    initial: Mapping[str, float],
    calibrated: Mapping[str, float],
    replicates: int,
    seed: int,
    confidence_level: float = 0.95,
    statistic: Callable[[np.ndarray], float] | None = None,
) -> BootstrapResult:
    """對 Delta = initial − calibrated 做成對重抽。

    unit_ids 是 recording/scenario 編號 —— 統計推論單位。每個 replicate 抽一次
    索引，**同一組索引同時套用到兩個候選**，因此 Delta 只反映校準造成的差異，
    不含「兩邊抽到不同樣本」的雜訊（Appendix I2）。

    statistic 預設取平均；傳入其他函式時仍是對同一組重抽後的 Delta 作用。
    """
    if replicates <= 0:
        raise BootstrapError(
            f"replicates must be positive, got {replicates!r}; B is frozen in "
            "e1_scientific_rule.lock and must not be defaulted here"
        )
    ids = list(unit_ids)
    if len(ids) < 2:
        raise BootstrapError(
            f"need at least 2 inferential units to bootstrap, got {len(ids)}"
        )
    missing_initial = [i for i in ids if i not in initial]
    missing_calibrated = [i for i in ids if i not in calibrated]
    if missing_initial or missing_calibrated:
        raise BootstrapError(
            "paired bootstrap requires both candidates to cover every unit; "
            f"missing from initial={missing_initial[:3]} "
            f"calibrated={missing_calibrated[:3]}"
        )

    initial_values = np.asarray([initial[i] for i in ids], dtype=np.float64)
    calibrated_values = np.asarray([calibrated[i] for i in ids], dtype=np.float64)
    if not (np.all(np.isfinite(initial_values)) and np.all(np.isfinite(calibrated_values))):
        raise BootstrapError("candidate values contain non-finite entries")

    deltas = initial_values - calibrated_values
    reducer = statistic or (lambda values: float(np.mean(values)))

    indices = _resample_indices(len(ids), replicates, seed)
    # 同一組索引套用到已配對好的 delta —— 這就是「same resample to both
    # candidates」的具體形式：delta 本身已經是逐 unit 配對的差。
    replicate_stats = np.asarray(
        [reducer(deltas[row]) for row in indices], dtype=np.float64
    )
    lower, upper = percentile_ci(replicate_stats, confidence_level)
    return BootstrapResult(
        point_estimate=float(reducer(deltas)),
        ci_lower=lower,
        ci_upper=upper,
        replicates=int(replicates),
        seed=int(seed),
        confidence_level=float(confidence_level),
        n_units=len(ids),
    )


#: cluster bootstrap 的版本識別。statistics_config.lock 記下這個字串，
#: 因此它必須存在於程式碼而不是只存在於 lock（同 NOTE-050 的做法）。
CLUSTER_BOOTSTRAP_VERSION = "family_cluster_bootstrap_v1"


def cluster_bootstrap_delta(
    y_true: Sequence[int],
    predictions: Mapping[str, Sequence[int]],
    cluster_ids: Sequence[str],
    strata: Sequence[str],
    metric: Callable[[np.ndarray, np.ndarray], float],
    reference: str,
    replicates: int,
    seed: int,
    confidence_level: float = 0.95,
) -> dict[str, object]:
    """Final E2 的成對 cluster bootstrap。

    重抽單位是 `cluster_ids`（Final E2 為 physical_scene_family）。抽到一個
    cluster 就把它的**全部**列一起帶入 —— 3 realization x 4 condition = 12 列。
    重抽在 `strata` 內分層（Final E2 為 class），因此每個 replicate 的類別
    組成與原樣本相同。

    每個 replicate 只抽一次 cluster，然後**所有方法共用**那一組列：
    方法之間的 Delta 因此只反映方法差異，不含「抽到不同樣本」的雜訊。

    `metric` 對 (y_true_subset, y_pred_subset) 作用，因此 accuracy 與
    macro-F1 走同一條路徑，不需要兩個函式。
    """
    if replicates <= 0:
        raise BootstrapError(
            f"replicates must be positive, got {replicates!r}; B is frozen in "
            "statistics_config.lock and must not be defaulted here"
        )
    truth = np.asarray(y_true)
    clusters = np.asarray([str(c) for c in cluster_ids])
    stratum = np.asarray([str(s) for s in strata])
    n = len(truth)
    if not (len(clusters) == len(stratum) == n):
        raise BootstrapError(
            f"y_true ({n}), cluster_ids ({len(clusters)}) and strata "
            f"({len(stratum)}) must be row-aligned"
        )
    if reference not in predictions:
        raise BootstrapError(f"reference method {reference!r} is not in predictions")
    for name, predicted in predictions.items():
        if len(predicted) != n:
            raise BootstrapError(
                f"predictions[{name!r}] has {len(predicted)} rows, expected {n}"
            )

    # 一個 cluster 只能屬於一個 stratum。Final E2 的 family 是類別專屬的，
    # 若不成立就代表 cluster 定義錯了，分層重抽會靜默地混類。
    rows_by_cluster: dict[str, np.ndarray] = {}
    stratum_of: dict[str, str] = {}
    for cluster in np.unique(clusters):
        mask = clusters == cluster
        rows_by_cluster[cluster] = np.flatnonzero(mask)
        owners = set(stratum[mask].tolist())
        if len(owners) != 1:
            raise BootstrapError(
                f"cluster {cluster!r} spans strata {sorted(owners)}; a resample "
                "unit must belong to exactly one stratum"
            )
        stratum_of[cluster] = owners.pop()

    by_stratum: dict[str, list[str]] = {}
    for cluster, owner in sorted(stratum_of.items()):
        by_stratum.setdefault(owner, []).append(cluster)
    if len(rows_by_cluster) < 2:
        raise BootstrapError(
            f"need at least 2 clusters to bootstrap, got {len(rows_by_cluster)}"
        )

    predicted = {name: np.asarray(values) for name, values in predictions.items()}
    observed = {
        name: float(metric(truth, values)) for name, values in predicted.items()
    }

    rng = np.random.default_rng(seed)
    others = [name for name in predicted if name != reference]
    draws: dict[str, np.ndarray] = {name: np.empty(replicates) for name in others}
    for replicate in range(replicates):
        # 一次抽定，所有方法共用。
        drawn: list[np.ndarray] = []
        for owner in sorted(by_stratum):
            pool = by_stratum[owner]
            picked = rng.choice(pool, size=len(pool), replace=True)
            drawn.extend(rows_by_cluster[cluster] for cluster in picked)
        rows = np.concatenate(drawn)
        truth_rows = truth[rows]
        base = float(metric(truth_rows, predicted[reference][rows]))
        for name in others:
            draws[name][replicate] = float(metric(truth_rows, predicted[name][rows])) - base

    comparisons = {}
    for name in others:
        lower, upper = percentile_ci(draws[name], confidence_level)
        comparisons[f"{name}_vs_{reference}"] = {
            "delta": observed[name] - observed[reference],
            "ci_lower": lower,
            "ci_upper": upper,
            "significant_at_95": bool(lower > 0.0 or upper < 0.0),
        }

    return {
        "version": CLUSTER_BOOTSTRAP_VERSION,
        "resample_unit": "cluster",
        "stratification": "stratum",
        "shared_clusters_across_methods": True,
        "replicates": int(replicates),
        "seed": int(seed),
        "confidence_level": float(confidence_level),
        "n_rows": int(n),
        "n_clusters": len(rows_by_cluster),
        "clusters_per_stratum": {k: len(v) for k, v in sorted(by_stratum.items())},
        "rows_per_cluster": sorted({len(v) for v in rows_by_cluster.values()}),
        "reference": reference,
        "observed": observed,
        "comparisons": comparisons,
    }
