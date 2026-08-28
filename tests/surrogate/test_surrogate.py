# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以合成 toy transient 操作 surrogate 全部模組，
#         不依賴 mitsuba 是否安裝，也不讀取 data/raw_real/。
# 檔案路徑: tests/surrogate/test_surrogate.py
# 產生時間: 2026-08-26 13:30 +08:00
# 版本: v0.2.0
# 功能說明: 用可控的假回波驗證感測器替身的每一欄映射是否符合物理直覺，
#           並鎖住三條禁止做法：拿 transient bins 當量測點、預設取樣間隔、
#           只用 Distance 推算 Signal。
# 模組定位: Batch 5 的驗收測試。它不驗證物理校準的正確性 ——
#           校準常數尚未求出，全部標記為 placeholder。
# 主要責任:
#   1. _toy_transient() 合成可控峰值位置與寬度的回波
#   2. 物理量抽取：峰值時間、FWHM、SNR、多路徑
#   3. 四欄映射各自的單調性與拒絕路徑
#   4. 500 點 recording 的形狀、間隔必填與模式差異
#   5. 禁止做法的自動防線
# 維護提醒:
#   - 不得放寬 test_recording_length_is_independent_of_transient_bins；
#     它是「bins 不得當量測點」唯一的自動防線。
#   - 不得為 sample_interval_s 在測試中引入共用預設值；
#     每個測試都應顯式傳入，否則會掩蓋掉必填的設計。
#   - v0.2.0 跟隨 features/sigma 的多路徑指標改名。
#   - v0.1.0 新增：首版 surrogate 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/surrogate/test_surrogate.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import TOF_SCHEMA, tof_index
from pcmef.surrogate.ambient import map_ambient_rate
from pcmef.surrogate.calibration import (
    PLACEHOLDER_SMOKE_CALIBRATION,
    CalibratedScale,
    CalibrationError,
    SurrogateCalibration,
)
from pcmef.surrogate.distance import DistanceEstimator, map_distance
from pcmef.surrogate.features import (
    TransientFeatureError,
    collapse_to_waveform,
    extract_observables,
)
from pcmef.surrogate.signal_rate import map_signal_rate
from pcmef.surrogate.sigma import map_sigma_like
from pcmef.surrogate.single_acquisition import SensorSurrogate
from pcmef.surrogate.temporal_model import (
    RecordingMode,
    TemporalModel,
    TemporalModelError,
)

BINS = 128
BIN_WIDTH_S = 1e-11          # 10 ps，與 Batch 4 實測的 9.43 ps 同量級
START_S = 2.7e-10


def _time_axis(bins: int = BINS) -> np.ndarray:
    return START_S + np.arange(bins) * BIN_WIDTH_S


def _toy_transient(
    peak_bin: int = 40,
    width_bins: float = 4.0,
    amplitude: float = 100.0,
    background: float = 0.5,
    bins: int = BINS,
    secondary_bin: int | None = None,
) -> np.ndarray:
    """合成一個高斯回波，可選加上代表多路徑的次要回波。

    回傳 (H,W,bins,C) 形狀以符合 mitransient 的實際輸出結構。
    """
    index = np.arange(bins, dtype=np.float64)
    waveform = amplitude * np.exp(-0.5 * ((index - peak_bin) / width_bins) ** 2)
    waveform += background
    if secondary_bin is not None:
        waveform += 0.3 * amplitude * np.exp(
            -0.5 * ((index - secondary_bin) / width_bins) ** 2
        )
    return waveform.reshape(1, 1, bins, 1)


def _rng(seed: int = 0) -> np.random.Generator:
    return np.random.default_rng(seed)


def _ambient_pass(level: float = 0.5, bins: int = BINS) -> np.ndarray:
    """合成一個 ambient pass（VCSEL 關閉、只有室內光）。

    NOTE(NOTE-034): Ambient 只能來自這個獨立的 pass。舊測試把 active pass
    的主窗外能量當 ambient，那個量實測 99.97% 是雷射多重反射。
    """
    return np.full((1, 1, bins, 1), level)


# ---------------------------------------------------------------------------
# 物理量抽取
# ---------------------------------------------------------------------------


def test_waveform_collapses_spatial_and_channel_dimensions():
    transient = np.ones((4, 5, BINS, 3))
    waveform = collapse_to_waveform(transient)
    assert waveform.shape == (BINS,)
    assert np.allclose(waveform, 4 * 5 * 3)


def test_peak_time_tracks_the_injected_peak():
    axis = _time_axis()
    for peak_bin in (20, 40, 80):
        observables = extract_observables(_toy_transient(peak_bin=peak_bin), axis)
        assert observables.peak_time_s == pytest.approx(axis[peak_bin], abs=BIN_WIDTH_S)


def test_fwhm_grows_with_injected_width():
    axis = _time_axis()
    narrow = extract_observables(_toy_transient(width_bins=2.0), axis)
    wide = extract_observables(_toy_transient(width_bins=6.0), axis)
    assert wide.fwhm_s > narrow.fwhm_s * 2


def test_snr_falls_as_background_rises():
    axis = _time_axis()
    clean = extract_observables(_toy_transient(background=0.1), axis)
    noisy = extract_observables(_toy_transient(background=5.0), axis)
    assert clean.snr > noisy.snr


def test_multipath_prominence_reacts_to_a_secondary_echo():
    axis = _time_axis()
    single = extract_observables(_toy_transient(), axis)
    multi = extract_observables(_toy_transient(secondary_bin=90), axis)
    assert multi.multipath_prominence > single.multipath_prominence


def test_empty_waveform_is_rejected_rather_than_defaulted():
    """全零代表模擬時間窗設錯，必須浮現而不是回傳合理預設值。"""
    with pytest.raises(TransientFeatureError, match="no energy"):
        extract_observables(np.zeros((1, 1, BINS, 1)), _time_axis())


def test_time_axis_length_mismatch_is_rejected():
    with pytest.raises(TransientFeatureError, match="does not match"):
        extract_observables(_toy_transient(), _time_axis(bins=64))


# ---------------------------------------------------------------------------
# 四欄映射
# ---------------------------------------------------------------------------


def test_distance_increases_with_later_echo():
    """回波越晚到，換算出的距離越遠。這是 Distance 映射的基本物理。"""
    axis = _time_axis()
    calib = PLACEHOLDER_SMOKE_CALIBRATION
    near = map_distance(
        extract_observables(_toy_transient(peak_bin=20), axis), calib, _rng(1)
    )
    far = map_distance(
        extract_observables(_toy_transient(peak_bin=100), axis), calib, _rng(1)
    )
    assert far > near


def test_distance_estimators_differ_under_asymmetric_echo():
    """峰值與能量重心對多路徑的敏感度不同，因此必須顯式選擇。"""
    axis = _time_axis()
    observables = extract_observables(_toy_transient(secondary_bin=100), axis)
    peak = map_distance(
        observables, PLACEHOLDER_SMOKE_CALIBRATION, _rng(2), DistanceEstimator.PEAK
    )
    centroid = map_distance(
        observables,
        PLACEHOLDER_SMOKE_CALIBRATION,
        _rng(2),
        DistanceEstimator.ENERGY_CENTROID,
    )
    assert centroid > peak


def test_signal_rate_increases_with_echo_energy():
    axis = _time_axis()
    weak = map_signal_rate(
        extract_observables(_toy_transient(amplitude=10.0), axis),
        PLACEHOLDER_SMOKE_CALIBRATION, _rng(3),
    )
    strong = map_signal_rate(
        extract_observables(_toy_transient(amplitude=200.0), axis),
        PLACEHOLDER_SMOKE_CALIBRATION, _rng(3),
    )
    assert strong > weak


def test_ambient_rate_tracks_the_ambient_pass_not_the_active_background():
    """NOTE-034：Ambient 隨**室內光**變動，且**不隨** active pass 的背景變動。"""
    axis = _time_axis()
    dim = map_ambient_rate(
        extract_observables(_toy_transient(), axis, ambient_transient=_ambient_pass(0.2)),
        PLACEHOLDER_SMOKE_CALIBRATION, _rng(4),
    )
    bright = map_ambient_rate(
        extract_observables(_toy_transient(), axis, ambient_transient=_ambient_pass(8.0)),
        PLACEHOLDER_SMOKE_CALIBRATION, _rng(4),
    )
    assert bright > dim

    # 反向：active pass 的背景是雷射多重反射，它變動時 Ambient 必須不動。
    same = _ambient_pass(0.2)
    quiet = map_ambient_rate(
        extract_observables(_toy_transient(background=0.2), axis, ambient_transient=same),
        PLACEHOLDER_SMOKE_CALIBRATION, _rng(4),
    )
    noisy = map_ambient_rate(
        extract_observables(_toy_transient(background=8.0), axis, ambient_transient=same),
        PLACEHOLDER_SMOKE_CALIBRATION, _rng(4),
    )
    assert quiet == pytest.approx(noisy)


def test_ambient_must_jitter_between_acquisitions():
    """SRC-SAI §10：每類固定常數無 jitter 是禁止做法。"""
    axis = _time_axis()
    observables = extract_observables(
        _toy_transient(), axis, ambient_transient=_ambient_pass()
    )
    rng = _rng(5)
    samples = [
        map_ambient_rate(observables, PLACEHOLDER_SMOKE_CALIBRATION, rng)
        for _ in range(50)
    ]
    assert len(set(samples)) > 40, "ambient rate must vary between acquisitions"
    assert np.std(samples) > 0


def test_zero_ambient_jitter_is_refused():
    calib = SurrogateCalibration(
        **{
            **{k: v for k, v in vars(PLACEHOLDER_SMOKE_CALIBRATION).items()
               if k not in ("ambient_jitter_relative", "analysis")},
            "ambient_jitter_relative": CalibratedScale(0.0, placeholder=True),
            "analysis": dict(PLACEHOLDER_SMOKE_CALIBRATION.analysis),
        }
    )
    observables = extract_observables(_toy_transient(), _time_axis())
    with pytest.raises(CalibrationError, match="forbidden surrogate behaviour"):
        map_ambient_rate(observables, calib, _rng(6))


def test_sigma_grows_with_width_and_with_multipath():
    axis = _time_axis()
    calib = PLACEHOLDER_SMOKE_CALIBRATION
    narrow = map_sigma_like(
        extract_observables(_toy_transient(width_bins=2.0), axis), calib, _rng(7)
    )
    wide = map_sigma_like(
        extract_observables(_toy_transient(width_bins=6.0), axis), calib, _rng(7)
    )
    multi = map_sigma_like(
        extract_observables(_toy_transient(width_bins=2.0, secondary_bin=95), axis),
        calib, _rng(7),
    )
    assert wide > narrow
    assert multi > narrow


def test_truncated_echo_is_refused_rather_than_reported_as_precise():
    """FWHM=0 代表回波被時間窗截斷，不得偽裝成極高精度。"""
    axis = _time_axis()
    # 峰值貼在最後一個 bin，右半邊無法找到半高點。
    observables = extract_observables(_toy_transient(peak_bin=BINS - 1), axis)
    assert observables.fwhm_s == 0.0
    with pytest.raises(CalibrationError, match="truncated"):
        map_sigma_like(observables, PLACEHOLDER_SMOKE_CALIBRATION, _rng(8))


# ---------------------------------------------------------------------------
# 校準把關
# ---------------------------------------------------------------------------


def test_formal_mode_refuses_placeholder_calibration():
    with pytest.raises(CalibrationError, match="placeholder"):
        SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION, formal=True)


def test_smoke_mode_allows_placeholder_but_records_it():
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    provenance = surrogate.provenance()
    assert provenance["formal"] is False

    # 九個 scale 中有**八**個是 placeholder。少的那一個是
    # optical_path_to_distance —— 它由共置幾何決定（NOTE-026），
    # NOTE-030 起改標 derived 並移出可校準集合，因此不算 placeholder。
    # 這裡把「為什麼是 8 不是 9」寫死成斷言，而不是把數字改小了事。
    placeholders = provenance["calibration"]["placeholders"]
    assert len(placeholders) == 8
    assert "optical_path_to_distance" not in placeholders
    assert len(PLACEHOLDER_SMOKE_CALIBRATION.scales()) == 9
    assert PLACEHOLDER_SMOKE_CALIBRATION.derived_names() == ["optical_path_to_distance"]


def test_non_placeholder_scale_requires_a_source():
    with pytest.raises(CalibrationError, match="record its source"):
        CalibratedScale(value=0.5, placeholder=False)


def test_calibration_hash_is_sensitive_to_every_scale():
    base = PLACEHOLDER_SMOKE_CALIBRATION
    changed = SurrogateCalibration(
        **{
            **{k: v for k, v in vars(base).items()
               if k not in ("signal_energy_to_mcps", "analysis")},
            "signal_energy_to_mcps": CalibratedScale(2.0, placeholder=True),
            "analysis": dict(base.analysis),
        }
    )
    assert base.calibration_hash() != changed.calibration_hash()


# ---------------------------------------------------------------------------
# 單次 acquisition
# ---------------------------------------------------------------------------


def test_single_acquisition_produces_exactly_four_values():
    """SRC-SAI §10：一次 optical transient 對應恰好一筆 observation。"""
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    vector = surrogate.map_single_acquisition(
        _toy_transient(), _time_axis(), _rng(9),
        ambient_transient=_ambient_pass(),
    )
    assert vector.shape == (4,)
    assert np.all(np.isfinite(vector))


def test_single_acquisition_follows_canonical_column_order():
    """欄序必須經 TOF_SCHEMA 取得（NOTE-001），不得依模組宣告順序。"""
    axis = _time_axis()
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    vector = surrogate.map_single_acquisition(
        _toy_transient(), axis, _rng(10), _ambient_pass()
    )
    observables = surrogate.observe(_toy_transient(), axis, _ambient_pass())

    # 背景遠小於主回波，因此 ambient 必然小於 signal；
    # 若欄序被寫反，這條斷言會失敗。
    assert vector[tof_index("ambient_rate_mcps")] < vector[tof_index("signal_rate_mcps")]
    # distance 為毫米量級，sigma 為次毫米到毫米量級。
    assert vector[tof_index("distance_mm")] > vector[tof_index("sigma_like")]
    assert observables.main_energy > observables.out_of_window_energy


def test_signal_is_not_derived_from_distance():
    """SRC-SAI §10：只用 Distance 推算 Signal 是禁止做法。

    固定回波時間、只改振幅：Distance 幾乎不動而 Signal 大幅變化。
    若 Signal 由 Distance 推導，兩者會一起變或一起不變。
    """
    axis = _time_axis()
    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    weak = surrogate.map_single_acquisition(
        _toy_transient(peak_bin=40, amplitude=10.0), axis, _rng(11),
        ambient_transient=_ambient_pass(),
    )
    strong = surrogate.map_single_acquisition(
        _toy_transient(peak_bin=40, amplitude=500.0), axis, _rng(11),
        ambient_transient=_ambient_pass(),
    )
    d = tof_index("distance_mm")
    s = tof_index("signal_rate_mcps")
    assert strong[s] > weak[s] * 10
    assert abs(strong[d] - weak[d]) / weak[d] < 0.05


# ---------------------------------------------------------------------------
# 500 點 recording
# ---------------------------------------------------------------------------


def _model() -> TemporalModel:
    return TemporalModel(SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION))


def test_recording_has_the_canonical_shape():
    recording = _model().generate_recording(
        _toy_transient(), _time_axis(),
        sample_interval_s=0.082, sample_interval_source="frozen_temporal_config",
        seed=42,
        ambient_transient=_ambient_pass(),
    )
    assert recording.values.shape == (500, len(TOF_SCHEMA))
    assert recording.n_samples == 500
    assert recording.duration_s == pytest.approx(41.0, rel=1e-9)


def test_recording_length_is_independent_of_transient_bins():
    """禁止做法的核心防線：n_samples 與 bins 無關。

    `tof_recording = transient_bins[:500]` 是 SRC-SAI §10 明列的禁止實作。
    若有人改成從 bins 取樣，改變 bins 就會改變 recording 長度，本測試即失敗。
    """
    model = _model()
    for bins in (32, 64, 256):
        recording = model.generate_recording(
            _toy_transient(peak_bin=bins // 3, bins=bins), _time_axis(bins),
            sample_interval_s=0.082, sample_interval_source="frozen_temporal_config",
            seed=1, n_samples=500,
            # ambient pass 必須與 active pass 同 binning（NOTE-034）。
            ambient_transient=_ambient_pass(bins=bins),
        )
        assert recording.n_samples == 500


def test_sample_interval_is_mandatory():
    """SRC-SAI §10：禁止 silently default 到 0.082 s。"""
    model = _model()
    with pytest.raises(TypeError):
        model.generate_recording(_toy_transient(), _time_axis(), seed=1)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [0.0, -0.082])
def test_non_positive_interval_is_rejected(bad):
    with pytest.raises(TemporalModelError, match="required and must be positive"):
        _model().generate_recording(
            _toy_transient(), _time_axis(),
            sample_interval_s=bad, sample_interval_source="x", seed=1,
            ambient_transient=_ambient_pass(),
        )


def test_interval_source_is_mandatory():
    with pytest.raises(TemporalModelError, match="origin cannot be audited"):
        _model().generate_recording(
            _toy_transient(), _time_axis(),
            sample_interval_s=0.082, sample_interval_source="", seed=1,
            ambient_transient=_ambient_pass(),
        )


def test_two_datasets_can_carry_different_intervals():
    """NOTE-011：資料集內確實存在 0.082 與 0.0624 兩種取樣率。"""
    model = _model()
    main = model.generate_recording(
        _toy_transient(), _time_axis(),
        sample_interval_s=0.082, sample_interval_source="edge_impulse_12.19512Hz",
        seed=1,
        ambient_transient=_ambient_pass(),
    )
    offset = model.generate_recording(
        _toy_transient(), _time_axis(),
        sample_interval_s=0.0624, sample_interval_source="offset_test_recording",
        seed=1,
        ambient_transient=_ambient_pass(),
    )
    assert main.duration_s != offset.duration_s
    assert main.to_provenance()["sample_interval_source"] != (
        offset.to_provenance()["sample_interval_source"]
    )


def test_recording_samples_vary_between_acquisitions():
    """500 點必須是 500 次獨立觀測，不是同一筆複製 500 次。"""
    recording = _model().generate_recording(
        _toy_transient(), _time_axis(),
        sample_interval_s=0.082, sample_interval_source="frozen_temporal_config",
        seed=7,
        ambient_transient=_ambient_pass(),
    )
    for column in range(len(TOF_SCHEMA)):
        assert np.std(recording.values[:, column]) > 0


def test_same_seed_reproduces_the_recording():
    kwargs = dict(
        sample_interval_s=0.082, sample_interval_source="frozen_temporal_config",
        seed=99, ambient_transient=_ambient_pass(),
    )
    first = _model().generate_recording(_toy_transient(), _time_axis(), **kwargs)
    second = _model().generate_recording(_toy_transient(), _time_axis(), **kwargs)
    assert np.array_equal(first.values, second.values)


def test_realizations_mode_requires_one_transient_per_sample():
    """REALIZATIONS 模式若沿用同一份 transient，那就是 stochastic 模式。"""
    model = _model()
    with pytest.raises(TemporalModelError, match="exactly 8 rendered transients"):
        model.generate_recording(
            [_toy_transient()] * 3, _time_axis(),
            sample_interval_s=0.082, sample_interval_source="x", seed=1,
            n_samples=8, mode=RecordingMode.REALIZATIONS,
            ambient_transient=_ambient_pass(),
        )


def test_realizations_mode_accepts_matching_transients():
    model = _model()
    transients = [_toy_transient(peak_bin=40 + i % 3) for i in range(8)]
    recording = model.generate_recording(
        transients, _time_axis(),
        sample_interval_s=0.082, sample_interval_source="x", seed=1,
        n_samples=8, mode=RecordingMode.REALIZATIONS,
        ambient_transient=_ambient_pass(),
    )
    assert recording.values.shape == (8, 4)
    assert recording.mode is RecordingMode.REALIZATIONS


def test_provenance_marks_the_measurement_time_axis():
    """SRC-D03：這條軸是 measurement time，與 optical transient time 不同。"""
    provenance = _model().generate_recording(
        _toy_transient(), _time_axis(),
        sample_interval_s=0.082, sample_interval_source="frozen_temporal_config",
        seed=3,
        ambient_transient=_ambient_pass(),
    ).to_provenance()
    assert provenance["time_axis"] == "measurement_time"
    assert provenance["tof_schema"] == list(TOF_SCHEMA)
    assert provenance["mode"] == "measurement_time_stochastic_model"
