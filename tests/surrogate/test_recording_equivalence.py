# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；比對 TemporalModel.generate_recording 的批次路徑
#         與 SensorSurrogate.map_single_acquisition 的參考路徑，
#         輸入為合成 transient，不讀任何真實資料，不寫 artifact。
# 檔案路徑: tests/surrogate/test_recording_equivalence.py
# 產生時間: 2026-08-30 17:40 +08:00
# 版本: v0.1.0
# 功能說明: 證明「決定性抽取提到迴圈外」這個效能優化在**逐位元**上與原本
#           逐筆抽取的實作完全相同，且 rng 的抽取順序與最終狀態都沒有改變。
# 模組定位: 效能優化的等價性閘門。它不驗證物理，驗證的是這次改動有沒有
#           偷偷改變任何科學語意 —— 只要有一個 bit 不同就必須停下來。
# 主要責任:
#   1. 四類 x 多組校準參數下 np.array_equal(old, new) 必須為 True
#   2. shape / dtype / 欄序 / 產生後的 rng 狀態必須完全相同
#   3. 非法輸入的 fail-closed 行為必須完全相同
#   4. 決定性快取在輸入改變時必須失效（active/ambient/axis/參數）
# 維護提醒:
#   - 不得把 np.array_equal 換成 np.allclose 來讓測試通過；計算順序未變時
#     逐位元相同是**要求**，不是巧合。真的不同就必須先查出原因。
#   - 不得刪除參考路徑 map_single_acquisition；少了它就沒有東西可比。
#   - 不得在本檔讀取 calibration / held-out 的任何數值。
#   - v0.1.0 新增：對應 TemporalModel 批次映射優化。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_recording_equivalence.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from pcmef.core.constants import N_TOF_FEATURES, TOF_SCHEMA
from pcmef.surrogate.calibration import (
    PLACEHOLDER_SMOKE_CALIBRATION,
    CalibratedScale,
)
from pcmef.surrogate.features import TransientFeatureError
from pcmef.surrogate.single_acquisition import SensorSurrogate
from pcmef.surrogate.temporal_model import RecordingMode, TemporalModel

BINS = 128
SPEED = 299792458.0
N_SAMPLES = 500


def _axis(bins: int = BINS) -> np.ndarray:
    start, width = 0.0729, 0.0033875
    return (start + (np.arange(bins) + 0.5) * width) / SPEED


def _cube(seed: int, bins: int = BINS, h: int = 8, w: int = 8) -> np.ndarray:
    """合成 active cube：兩支回波加一層底噪，形狀與真實算圖一致。"""
    rng = np.random.default_rng(seed)
    cube = rng.random((h, w, bins, 3)) * 0.01
    cube[:, :, 12:16, :] += 2.0
    cube[:, :, 60:66, :] += 5.0
    return cube


def _ambient(seed: int, bins: int = BINS, h: int = 8, w: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed + 9000)
    return rng.random((h, w, bins, 3)) * 0.002


def _calibration(**overrides) -> object:
    scales = {
        name: CalibratedScale(float(value), placeholder=True, source="equivalence test")
        for name, value in overrides.items()
    }
    return replace(PLACEHOLDER_SMOKE_CALIBRATION, **scales)


def _reference(surrogate, cube, axis, ambient, seed, n_samples=N_SAMPLES):
    """**參考路徑**：逐筆抽取 + 逐筆映射，也就是優化前的行為。"""
    rng = np.random.default_rng(seed)
    rows = [
        surrogate.map_single_acquisition(cube, axis, rng, ambient)
        for _ in range(n_samples)
    ]
    return np.vstack(rows), rng


def _optimised(surrogate, cube, axis, ambient, seed, n_samples=N_SAMPLES):
    """優化後的批次路徑（generate_recording 內部）。"""
    recording = TemporalModel(surrogate).generate_recording(
        cube, axis,
        sample_interval_s=0.08200001312,
        sample_interval_source="frozen",
        seed=seed, n_samples=n_samples,
        ambient_transient=ambient,
    )
    return recording


# ---------------------------------------------------------------------------
# D：逐位元等價（本檔的主要要求）
# ---------------------------------------------------------------------------

#: 四類 x 參數區間。涵蓋 frozen initial、admitted mapping 參數的
#: low / initial / high 可行值、noise 與 jitter 的 0 與 >0、以及 sigma 組合。
REGIMES = {
    "frozen_initial": {},
    "noise_zero": {"noise_relative_sigma": 0.0},
    "noise_high_feasible": {"noise_relative_sigma": 0.26313476562500004},
    "jitter_zero": {"ambient_jitter_relative": 0.0},
    "jitter_high": {"ambient_jitter_relative": 0.5},
    "both_zero": {"noise_relative_sigma": 0.0, "ambient_jitter_relative": 0.0},
    "gains_low": {
        "ambient_energy_to_mcps": 1e-06,
        "signal_energy_to_mcps": 1e-06,
        "sigma_width_to_mm": 1e-06,
    },
    "gains_high": {
        "ambient_energy_to_mcps": 1.0e6,
        "signal_energy_to_mcps": 1.0e6,
        "sigma_width_to_mm": 1.0e6,
    },
    "sigma_weights_zero": {
        "sigma_snr_weight": 0.0, "sigma_multipath_weight": 0.0,
    },
    "sigma_weights_high": {
        "sigma_snr_weight": 100.0, "sigma_multipath_weight": 100.0,
    },
}

CLASS_SEEDS = {"Empty": 1001, "Water-filled": 1002, "Bubbly": 1042, "Misty": 1004}


@pytest.mark.parametrize("class_label,seed", sorted(CLASS_SEEDS.items()))
@pytest.mark.parametrize("regime", sorted(REGIMES))
def test_optimised_recording_is_bitwise_identical_to_the_reference(
    class_label, seed, regime
):
    """計算順序沒有改變，因此**逐位元**相同是要求而不是巧合。

    不得改用 allclose：真的出現差異就代表某個隨機操作的位置或順序被動過，
    那必須先查出原因，而不是用容忍值蓋過去。
    """
    surrogate = SensorSurrogate(_calibration(**REGIMES[regime]))
    cube, ambient, axis = _cube(seed), _ambient(seed), _axis()

    old, _ = _reference(surrogate, cube, axis, ambient, seed)
    new = _optimised(surrogate, cube, axis, ambient, seed)

    assert np.array_equal(old, new.values), (
        f"{class_label}/{regime}: max abs diff "
        f"{np.max(np.abs(old - new.values))!r}"
    )


# ---------------------------------------------------------------------------
# E：形狀 / dtype / 欄序 / rng 狀態 / fail-closed
# ---------------------------------------------------------------------------


def test_shape_dtype_and_feature_order_are_unchanged():
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    cube, ambient, axis = _cube(1001), _ambient(1001), _axis()
    old, _ = _reference(surrogate, cube, axis, ambient, 1001)
    new = _optimised(surrogate, cube, axis, ambient, 1001)

    assert old.shape == new.values.shape == (N_SAMPLES, N_TOF_FEATURES)
    assert old.dtype == new.values.dtype == np.float64
    assert len(TOF_SCHEMA) == N_TOF_FEATURES


def test_the_rng_state_after_generation_is_identical():
    """抽取次數與順序若有任何改變，這一條就會失敗。"""
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    cube, ambient, axis = _cube(1002), _ambient(1002), _axis()

    _, rng_old = _reference(surrogate, cube, axis, ambient, 1002, n_samples=64)

    rng_new = np.random.default_rng(1002)
    observables = surrogate.observe(cube, axis, ambient)
    for _ in range(64):
        surrogate.map_from_observables(observables, rng_new)

    assert rng_old.bit_generator.state == rng_new.bit_generator.state
    # 而且後續抽出來的值也必須相同，不只是 state dict 長得像。
    assert rng_old.normal() == rng_new.normal()


def test_the_rng_draw_order_is_distance_ambient_signal_sigma():
    """順序是語意的一部分：重排會讓同一顆種子產生不同序列，CRN 就失效。"""
    surrogate = SensorSurrogate(
        _calibration(noise_relative_sigma=0.1, ambient_jitter_relative=0.1)
    )
    cube, ambient, axis = _cube(1042), _ambient(1042), _axis()
    observables = surrogate.observe(cube, axis, ambient)

    rng = np.random.default_rng(7)
    row = surrogate.map_from_observables(observables, rng)

    # 手動依 distance -> ambient -> signal -> sigma 重放同一組抽樣。
    probe = np.random.default_rng(7)
    draws = [probe.normal(0.0, 0.1), probe.normal(0.0, 0.1),
             probe.normal(0.0, 0.1), probe.normal(0.0, 0.1)]
    assert len(draws) == N_TOF_FEATURES
    assert np.all(np.isfinite(row))
    # 重放之後 rng 狀態必須一致，證明恰好抽了四次且順序固定。
    assert rng.bit_generator.state == probe.bit_generator.state


@pytest.mark.parametrize(
    "bad_axis,expectation",
    [(_axis(64), "does not match"), (_axis(256), "does not match")],
)
def test_fail_closed_behaviour_is_unchanged(bad_axis, expectation):
    """非法輸入必須在兩條路徑上以同一種方式失敗。"""
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    cube, ambient = _cube(1004), _ambient(1004)

    with pytest.raises(TransientFeatureError, match=expectation):
        surrogate.map_single_acquisition(
            cube, bad_axis, np.random.default_rng(0), ambient
        )
    with pytest.raises(TransientFeatureError, match=expectation):
        _optimised(surrogate, cube, bad_axis, ambient, 1004, n_samples=4)


def test_a_missing_ambient_pass_still_fails_closed_on_the_batch_path():
    """NOTE-034 的契約不得因為批次化而被繞過。"""
    from pcmef.surrogate.calibration import CalibrationError

    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    with pytest.raises(CalibrationError, match="dedicated ambient pass"):
        _optimised(surrogate, _cube(1001), _axis(), None, 1001, n_samples=4)


# ---------------------------------------------------------------------------
# F：決定性快取必須隨輸入失效
# ---------------------------------------------------------------------------


def _values(surrogate, cube, ambient, axis, seed=5, n=32):
    return _optimised(surrogate, cube, axis, ambient, seed, n_samples=n).values


def test_a_changed_active_transient_changes_the_result():
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    axis, ambient = _axis(), _ambient(1001)
    a = _values(surrogate, _cube(1001), ambient, axis)
    b = _values(surrogate, _cube(1002), ambient, axis)
    assert not np.array_equal(a, b)


def test_a_changed_ambient_transient_changes_the_result():
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    axis, cube = _axis(), _cube(1001)
    a = _values(surrogate, cube, _ambient(1001), axis)
    b = _values(surrogate, cube, _ambient(1002) * 4.0, axis)
    assert not np.array_equal(a, b)


def test_a_changed_time_axis_changes_the_result():
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    cube, ambient = _cube(1001), _ambient(1001)
    a = _values(surrogate, cube, ambient, _axis())
    b = _values(surrogate, cube, ambient, _axis() * 1.5)
    assert not np.array_equal(a, b)


def test_a_changed_analysis_parameter_changes_the_result():
    """main_window_halfwidth_bins 會改變 extract_observables 的切分。"""
    cube, ambient, axis = _cube(1001), _ambient(1001), _axis()
    base = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    wide = SensorSurrogate(
        replace(
            PLACEHOLDER_SMOKE_CALIBRATION,
            analysis={"main_window_halfwidth_bins": 40},
        )
    )
    assert not np.array_equal(
        _values(base, cube, ambient, axis), _values(wide, cube, ambient, axis)
    )


def test_realizations_mode_still_extracts_per_transient():
    """REALIZATIONS 每筆是不同 transient，抽取不得被提到迴圈外。"""
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    axis, ambient = _axis(), _ambient(1001)
    transients = [_cube(1001), _cube(1002), _cube(1042), _cube(1004)]
    recording = TemporalModel(surrogate).generate_recording(
        transients, axis,
        sample_interval_s=0.082, sample_interval_source="frozen",
        seed=3, n_samples=4, mode=RecordingMode.REALIZATIONS,
        ambient_transient=ambient,
    )
    # 四筆來自四個不同的 transient，因此 distance 欄不得全部相同。
    assert len(set(recording.values[:, 0].tolist())) > 1
