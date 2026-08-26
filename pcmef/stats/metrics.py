# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.experiments.e1 呼叫，輸入為 adapters/surrogate 產出的
#         四特徵陣列；輸出的 W1 / NW / trend 判定流向 e1_metrics.csv 與
#         e1_outcome.lock。本檔不讀寫任何檔案，也不決定 PASS/FAIL。
# 檔案路徑: pcmef/stats/metrics.py
# 產生時間: 2026-08-27 13:05 +08:00
# 版本: v0.1.0
# 功能說明: 計算「模擬產生的四特徵分佈，離真實資料有多遠」。距離本身有物理單位
#           （mm、MCPS），因此另外除以一個事先凍結的尺度變成無單位數，
#           跨特徵比較與平均只准用後者。
# 模組定位: SRC-SAI §11 E1 Calibration / Fidelity Engine 的度量層。
#           它「不是」決策層 —— PASS/DEGRADED 由 experiments.e1_outcome 依
#           frozen rule 判定，本檔只產生數字。
# 主要責任:
#   1. wasserstein_w1() 計算單一特徵的 raw W1（primary evidence）
#   2. feature_scale_iqr() 由 calibration-only 真實值算 s_f
#   3. normalized_wasserstein() 產生無單位 NW = W1 / s_f
#   4. mean_sd_error() / temporal_variability() 產生 secondary 指標
#   5. class_distance_ordering() 產生 class 間距排序，供解釋用
#   6. trend_consistency() 判定 ±offset 的方向與相對變化是否維持
# 維護提醒:
#   - 不得直接平均或加總不同特徵的 raw W1。distance 的單位是 mm、
#     signal/ambient 是 MCPS、sigma_like 無單位；把它們平均等於把公尺加上安培
#     （§11 明列的禁止做法）。跨特徵一律先轉 NW。
#   - 不得用 held-out 或 synthetic 的值計算 s_f。Appendix I2 規定
#     s_f 是 calibration-only real 的 pooled IQR，且必須在 Held-out 開啟前凍結；
#     用到 held-out 就等於讓尺度看過答案。
#   - 不得在 s_f <= 0 或非有限時回退到 1.0 或任何預設值。那會讓一個
#     退化的特徵看起來剛好正規化成功，Appendix I2 要求此時 BLOCK。
#   - 不得把 500 個 measurement point 當成獨立樣本做統計推論；
#     推論單位是 recording/scenario（SRC-PLAN §1 的偽重複）。本檔只算描述量，
#     推論由 bootstrap 以 ID 重抽完成。
#   - v0.1.0 新增：首版度量層，決策見 NOTE-024。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1/test_metrics.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

__all__ = [
    "MetricError",
    "FeatureScaleError",
    "wasserstein_w1",
    "feature_scale_iqr",
    "normalized_wasserstein",
    "mean_sd_error",
    "temporal_variability",
    "class_distance_ordering",
    "TrendResult",
    "trend_consistency",
]


class MetricError(ValueError):
    """輸入不符合度量的前提（空陣列、非有限值、形狀不符）。"""


class FeatureScaleError(MetricError):
    """特徵尺度 s_f 無法使用。

    與一般 MetricError 分開：Appendix I2 規定 s_f 必須 finite 且 > 0，
    否則 Formal E1 直接 BLOCK —— 這是研究設計層級的中止，
    不是可以退回預設值的一般錯誤。
    """


def _finite_1d(values, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64).ravel()
    if array.size == 0:
        raise MetricError(f"{name} is empty")
    if not np.all(np.isfinite(array)):
        raise MetricError(f"{name} contains non-finite values")
    return array


# ---------------------------------------------------------------------------
# Primary：Wasserstein
# ---------------------------------------------------------------------------


def wasserstein_w1(real_values, synthetic_values) -> float:
    """兩個一維分佈的 1-Wasserstein 距離，單位與輸入相同。

    保留 raw W1 是 §11 的要求：它是 primary evidence，而且帶物理單位時
    才有「差了幾 mm」這種可解釋性。跨特徵比較另用 NW。
    """
    real = _finite_1d(real_values, "real_values")
    synthetic = _finite_1d(synthetic_values, "synthetic_values")
    from scipy.stats import wasserstein_distance

    return float(wasserstein_distance(real, synthetic))


def feature_scale_iqr(calibration_values) -> float:
    """s_f = calibration-only real values 的 IQR（Appendix I2）。

    刻意不接受「找不到就用 1.0」這種退路：s_f 是把有單位的 W1 轉成無單位 NW
    的除數，退化時 NW 會失去意義，而失去意義的 NW 仍然算得出一個數字 ——
    那正是最危險的情況。因此 s_f <= 0 或非有限一律 BLOCK。
    """
    values = _finite_1d(calibration_values, "calibration_values")
    q75, q25 = np.percentile(values, [75, 25])
    scale = float(q75 - q25)
    if not np.isfinite(scale) or scale <= 0.0:
        raise FeatureScaleError(
            f"feature scale s_f = IQR = {scale!r} is not usable; Appendix I2 requires "
            "a finite positive scale. A degenerate feature must block Formal E1 "
            "rather than be rescued by a default divisor."
        )
    return scale


def normalized_wasserstein(w1: float, scale: float) -> float:
    """NW = W1 / s_f。無單位，因此可以跨特徵比較與平均。"""
    if not np.isfinite(w1) or w1 < 0:
        raise MetricError(f"W1 must be finite and non-negative, got {w1!r}")
    if not np.isfinite(scale) or scale <= 0:
        raise FeatureScaleError(f"feature scale must be finite and positive, got {scale!r}")
    return float(w1 / scale)


# ---------------------------------------------------------------------------
# Secondary
# ---------------------------------------------------------------------------


def mean_sd_error(real_values, synthetic_values) -> dict[str, float]:
    """真實與合成的 mean/SD 差。與 W1 同單位，屬 secondary 解釋用。"""
    real = _finite_1d(real_values, "real_values")
    synthetic = _finite_1d(synthetic_values, "synthetic_values")
    return {
        "real_mean": float(real.mean()),
        "synthetic_mean": float(synthetic.mean()),
        "mean_error": float(synthetic.mean() - real.mean()),
        "real_sd": float(real.std(ddof=1)) if real.size > 1 else 0.0,
        "synthetic_sd": float(synthetic.std(ddof=1)) if synthetic.size > 1 else 0.0,
        "sd_error": float(
            (synthetic.std(ddof=1) if synthetic.size > 1 else 0.0)
            - (real.std(ddof=1) if real.size > 1 else 0.0)
        ),
    }


def temporal_variability(recording) -> dict[str, float]:
    """單筆 (n_samples, n_features) recording 的內部時序變異。

    這是 recording **內部**的描述量，不是跨 recording 的推論；
    §11 註明「統計 inferential unit 仍為 recording/scenario」。
    """
    array = np.asarray(recording, dtype=np.float64)
    if array.ndim != 2 or array.shape[0] < 2:
        raise MetricError(
            f"recording must be 2-D with at least 2 samples, got shape {array.shape}"
        )
    if not np.all(np.isfinite(array)):
        raise MetricError("recording contains non-finite values")
    diffs = np.diff(array, axis=0)
    return {
        "sd_per_feature": [float(v) for v in array.std(axis=0, ddof=1)],
        "mean_abs_step": [float(v) for v in np.abs(diffs).mean(axis=0)],
        "range_per_feature": [
            float(v) for v in (array.max(axis=0) - array.min(axis=0))
        ],
    }


def class_distance_ordering(per_class_values: Mapping[str, Sequence[float]]) -> list[str]:
    """依各 class 的中位數由小到大排序，供 class 間距關係的解釋。

    回傳排序而非數值：§11 把它列為 secondary「用於解釋，不取代 primary」，
    只給順序可以避免它被誤當成一個可以拿去比較的量。
    """
    medians = {
        label: float(np.median(_finite_1d(values, f"class {label}")))
        for label, values in per_class_values.items()
    }
    return [label for label, _ in sorted(medians.items(), key=lambda kv: kv[1])]


# ---------------------------------------------------------------------------
# Primary：Perturbation trend consistency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrendResult:
    """±offset 的趨勢一致性判定。"""

    applicable: bool
    consistent: bool
    real_direction: tuple[int, ...]
    synthetic_direction: tuple[int, ...]
    real_relative: tuple[float, ...]
    synthetic_relative: tuple[float, ...]
    detail: str = ""

    @property
    def degraded(self) -> bool:
        """趨勢不適用時不算降級 —— 那代表這個特徵本來就沒有方向可比。"""
        return self.applicable and not self.consistent


#: 真實側相對變化小到這個程度時，視為「baseline 在此 offset 上沒有變化」，
#: 因為再往下比就是在比量測雜訊。
_NEGLIGIBLE_RELATIVE = 1e-6


def _relative_magnitude_ok(real_rel: float, syn_rel: float, tolerance: float) -> bool:
    """合成的相對變化是否重現了真實的量級。"""
    if not np.isfinite(syn_rel):
        return False
    if abs(real_rel) <= _NEGLIGIBLE_RELATIVE:
        # 真實沒有變化時，合成也不該有 —— 此時比值無意義，改比絕對量。
        return abs(syn_rel) <= _NEGLIGIBLE_RELATIVE
    return abs(syn_rel / real_rel - 1.0) <= tolerance


def trend_consistency(
    real_by_offset: Mapping[str, float],
    synthetic_by_offset: Mapping[str, float],
    baseline_key: str = "baseline",
    relative_tolerance: float = 0.5,
) -> TrendResult:
    """比較真實與合成在 ±offset 上的方向與相對變化（§11 primary）。

    §11 要求「至少檢查方向與相對變化；不可只比單點」，因此這裡同時比：
      方向 —— 相對 baseline 是變大還是變小（sign）
      相對變化 —— (v − baseline) / |baseline|，容許差在 relative_tolerance 內

    只比方向會讓「真實差 7 mm、合成差 0.01 mm」被判成一致；
    只比數值又會讓一個整體偏移但趨勢正確的模擬被誤判成失敗。
    """
    offsets = sorted(set(real_by_offset) & set(synthetic_by_offset) - {baseline_key})
    if baseline_key not in real_by_offset or baseline_key not in synthetic_by_offset:
        return TrendResult(
            applicable=False, consistent=False,
            real_direction=(), synthetic_direction=(),
            real_relative=(), synthetic_relative=(),
            detail=f"no {baseline_key!r} entry; trend is not applicable",
        )
    if not offsets:
        return TrendResult(
            applicable=False, consistent=False,
            real_direction=(), synthetic_direction=(),
            real_relative=(), synthetic_relative=(),
            detail="no offset levels shared by real and synthetic",
        )

    real_base = float(real_by_offset[baseline_key])
    syn_base = float(synthetic_by_offset[baseline_key])
    if not np.isfinite(real_base) or real_base == 0.0:
        return TrendResult(
            applicable=False, consistent=False,
            real_direction=(), synthetic_direction=(),
            real_relative=(), synthetic_relative=(),
            detail=f"real baseline {real_base!r} cannot anchor a relative comparison",
        )

    real_rel = tuple(
        (float(real_by_offset[k]) - real_base) / abs(real_base) for k in offsets
    )
    syn_rel = tuple(
        (float(synthetic_by_offset[k]) - syn_base) / abs(syn_base)
        if syn_base not in (0.0,) and np.isfinite(syn_base)
        else float("nan")
        for k in offsets
    )
    real_dir = tuple(int(np.sign(v)) for v in real_rel)
    syn_dir = tuple(int(np.sign(v)) if np.isfinite(v) else 0 for v in syn_rel)

    direction_ok = real_dir == syn_dir
    # 幅度以**比值**判定，不是相對變化的絕對差。
    # 用絕對差會在相對變化本身很小時失效：真實變化 0.1、容差 0.5，
    # 那麼「合成完全沒變化」的差是 0.1，照樣通過 —— 而那正是最該擋下的情況
    # （方向沒錯只是因為沒有反向）。改用 syn/real 的比值後，
    # 幅度塌到千分之一會得到比值 0.001，必然落在容差外。
    magnitude_ok = all(
        _relative_magnitude_ok(r, s, relative_tolerance)
        for r, s in zip(real_rel, syn_rel)
    )
    detail = (
        f"offsets={offsets} direction={'ok' if direction_ok else 'MISMATCH'} "
        f"relative={'ok' if magnitude_ok else 'OUT OF TOLERANCE'}"
    )
    return TrendResult(
        applicable=True,
        consistent=bool(direction_ok and magnitude_ok),
        real_direction=real_dir, synthetic_direction=syn_dir,
        real_relative=real_rel, synthetic_relative=syn_rel,
        detail=detail,
    )
