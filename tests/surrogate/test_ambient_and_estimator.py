# PC-MEF Research System source maintenance contract
# 上下游: 測 surrogate.features 的雙 pass 觀測量、surrogate.ambient 的來源限制、
#         surrogate.distance 的兩個新 estimator，以及 estimator 預註冊檔與
#         DistanceEstimator 列舉之間的一致性。
# 檔案路徑: tests/surrogate/test_ambient_and_estimator.py
# 產生時間: 2026-08-28 17:20 +08:00
# 版本: v0.1.0
# 功能說明: 用合成波形驗證「Ambient 只能來自 ambient pass」與兩個 gated
#           estimator 的分段行為，不需要算圖，因此每次 CI 都會跑到。
# 模組定位: NOTE-034／NOTE-035 的快速反向驗收。它「不是」物理驗收 ——
#           四個照明條件的實測在 sim ambient-check，那需要真的算圖。
# 主要責任:
#   1. 缺 ambient pass 時 Ambient 映射必須 FAIL，不得退回舊行為
#   2. 兩個 pass 的 bin 數不一致必須 FAIL
#   3. find_returns 的分段與門檻行為
#   4. 預註冊的候選集合必須與 DistanceEstimator 列舉一致
# 維護提醒:
#   - 不得把 test_ambient_requires_a_dedicated_pass 改成 warning；
#     退回舊行為正是 NOTE-032 那一輪的病灶。
#   - 不得在預註冊檔之外新增 DistanceEstimator 成員；本檔會失敗。
#   - v0.1.0 新增：首版（NOTE-034、NOTE-035）。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_ambient_and_estimator.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.surrogate.ambient import map_ambient_rate
from pcmef.surrogate.calibration import PLACEHOLDER_SMOKE_CALIBRATION, CalibrationError
from pcmef.surrogate.distance import (
    DistanceEstimator,
    find_returns,
    map_distance,
)
from pcmef.surrogate.estimator_selection import (
    load_preregistration,
    preregistration_hash,
)
from pcmef.surrogate.features import TransientFeatureError, extract_observables

SPEED = 299792458.0


def _synthetic(bins: int = 64):
    """一條有兩支回波的 active 波形：近端小、遠端大。"""
    waveform = np.full(bins, 0.01)
    waveform[10:14] = [0.5, 2.0, 1.5, 0.4]     # 近端回波
    waveform[40:45] = [1.0, 6.0, 8.0, 3.0, 0.8]  # 遠端（較強）
    cube = waveform.reshape(1, 1, bins, 1)
    axis = (0.05 + (np.arange(bins) + 0.5) * 0.004) / SPEED
    return cube, axis


def _ambient_cube(bins: int = 64, level: float = 0.002):
    return np.full((1, 1, bins, 1), level)


# ---------------------------------------------------------------------------
# Ambient 只能來自 ambient pass
# ---------------------------------------------------------------------------


def test_ambient_requires_a_dedicated_pass():
    cube, axis = _synthetic()
    observables = extract_observables(cube, axis, 8)  # 沒有 ambient pass
    assert observables.ambient_energy is None

    with pytest.raises(CalibrationError) as error:
        map_ambient_rate(
            observables, PLACEHOLDER_SMOKE_CALIBRATION, np.random.default_rng(0)
        )
    message = str(error.value)
    assert "dedicated ambient pass" in message
    # 訊息必須帶著那個把上一輪誤導掉的數字，否則下一個人會再試一次。
    assert "99.97%" in message


def test_ambient_comes_from_the_ambient_pass_not_the_active_one():
    cube, axis = _synthetic()
    ambient = _ambient_cube(level=0.002)
    observables = extract_observables(cube, axis, 8, ambient_transient=ambient)

    expected = float(ambient.sum())
    assert observables.ambient_energy == pytest.approx(expected)
    # active pass 的主窗外能量必須是**另一個**數字，兩者不得互相取代。
    assert observables.out_of_window_energy != pytest.approx(expected)

    value = map_ambient_rate(
        observables, PLACEHOLDER_SMOKE_CALIBRATION, np.random.default_rng(0)
    )
    assert value > 0


def test_ambient_scales_with_the_ambient_pass_only():
    cube, axis = _synthetic()
    weak = extract_observables(cube, axis, 8, ambient_transient=_ambient_cube(level=0.001))
    strong = extract_observables(cube, axis, 8, ambient_transient=_ambient_cube(level=0.004))
    assert strong.ambient_energy == pytest.approx(4 * weak.ambient_energy)
    # active pass 沒變，所以 Signal 的來源不該跟著動。
    assert strong.main_energy == pytest.approx(weak.main_energy)


def test_mismatched_pass_binning_is_rejected():
    cube, axis = _synthetic(64)
    with pytest.raises(TransientFeatureError, match="share one binning"):
        extract_observables(cube, axis, 8, ambient_transient=_ambient_cube(32))


# ---------------------------------------------------------------------------
# gated estimators
# ---------------------------------------------------------------------------


def test_find_returns_segments_and_respects_min_length():
    waveform = np.array([0, 0, 5, 5, 5, 0, 0, 9, 0, 0, 4, 4, 0], dtype=float)
    assert find_returns(waveform, threshold=1.0, min_return_bins=1) == [
        (2, 5), (7, 8), (10, 12)
    ]
    # 單一 bin 的突起在 min_return_bins=2 時不算一段 return。
    assert find_returns(waveform, threshold=1.0, min_return_bins=2) == [(2, 5), (10, 12)]


def test_leading_edge_takes_the_first_return_not_the_strongest():
    cube, axis = _synthetic()
    observables = extract_observables(
        cube, axis, 8, ambient_transient=_ambient_cube(level=0.002)
    )
    rng = np.random.default_rng(0)
    leading = map_distance(
        observables, PLACEHOLDER_SMOKE_CALIBRATION, rng, DistanceEstimator.LEADING_EDGE
    )
    peak = map_distance(
        observables, PLACEHOLDER_SMOKE_CALIBRATION, rng, DistanceEstimator.PEAK
    )
    # 近端回波在 bin 10，遠端較強的在 bin 40 -> leading edge 必須明顯較近。
    assert leading < peak


def test_strongest_return_centroid_stays_inside_one_return():
    cube, axis = _synthetic()
    observables = extract_observables(
        cube, axis, 8, ambient_transient=_ambient_cube(level=0.002)
    )
    value = map_distance(
        observables,
        PLACEHOLDER_SMOKE_CALIBRATION,
        np.random.default_rng(0),
        DistanceEstimator.STRONGEST_RETURN_CENTROID,
    )
    # 遠端 return 佔 bin 40..44；其重心必須落在該段之內，
    # 不得被近端回波拉過去（那正是 ENERGY_CENTROID 的行為）。
    lo = float(axis[40] * SPEED * 0.5 * 1000.0)
    hi = float(axis[44] * SPEED * 0.5 * 1000.0)
    assert lo <= value <= hi


def test_gated_estimators_refuse_without_an_ambient_noise_reference():
    """門檻以量到的雜訊為單位；沒有 ambient pass 就沒有單位可用。"""
    cube, axis = _synthetic()
    observables = extract_observables(cube, axis, 8)
    for estimator in (
        DistanceEstimator.LEADING_EDGE,
        DistanceEstimator.STRONGEST_RETURN_CENTROID,
    ):
        with pytest.raises(CalibrationError, match="ambient pass"):
            map_distance(
                observables,
                PLACEHOLDER_SMOKE_CALIBRATION,
                np.random.default_rng(0),
                estimator,
            )


def test_no_return_above_threshold_fails_rather_than_falling_back():
    cube, axis = _synthetic()
    observables = extract_observables(
        cube, axis, 8, ambient_transient=_ambient_cube(level=100.0)
    )
    with pytest.raises(CalibrationError, match="must not silently fall back"):
        map_distance(
            observables,
            PLACEHOLDER_SMOKE_CALIBRATION,
            np.random.default_rng(0),
            DistanceEstimator.LEADING_EDGE,
        )


# ---------------------------------------------------------------------------
# 預註冊與實作必須一致
# ---------------------------------------------------------------------------


def test_preregistered_candidates_match_the_implemented_enum():
    """在預註冊之外新增 estimator，等於繞過整個先註冊後比較的機制。"""
    prereg = load_preregistration()
    registered = {c["id"] for c in prereg["candidates"]}
    implemented = {e.name for e in DistanceEstimator}
    assert registered == implemented, (
        f"preregistration and DistanceEstimator disagree: "
        f"only in yaml {registered - implemented}, only in code {implemented - registered}"
    )


def test_preregistered_tunable_parameters_are_all_shared():
    prereg = load_preregistration()
    for parameter in prereg["tunable_parameters"]:
        assert parameter["class_scope"] == "shared", (
            f"{parameter['name']} is not shared; a class-specific estimator "
            "parameter is an explicitly forbidden leak channel"
        )


def test_real_class_means_are_listed_as_forbidden_inputs():
    prereg = load_preregistration()
    forbidden = " ".join(prereg["forbidden_selection_inputs"])
    for value in ("100.91", "113.87", "105.57", "79.51"):
        assert value in forbidden, (
            "the four real class means must be named explicitly as forbidden "
            "selection inputs; naming them is what makes the rule checkable"
        )


def test_preregistration_hash_is_stable():
    assert preregistration_hash() == preregistration_hash()
    assert len(preregistration_hash()) == 64
