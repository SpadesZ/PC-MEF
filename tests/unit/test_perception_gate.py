# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.perception.stress 與 pcmef.perception.gate；以合成張量與
#         tmp_path 驗證，不算圖、不讀真實資料、不碰 FORMAL_E1_FINAL、
#         不讀 Formal E2 的結果。由 pytest 收集執行。
# 檔案路徑: tests/unit/test_perception_gate.py
# 產生時間: 2026-08-31 14:50 +08:00
# 版本: v0.1.0
# 功能說明: 驗證劣化算子是決定性且方向正確的、溫度校準不改變預測、
#           品質訊號看不到標籤，以及 gate 路由與 severity 選定照宣告的規則走。
# 模組定位: gate 決策層的驗收。它不驗證 PC-MEF 贏不贏，只驗證
#           「若有結論，產生它的規則沒有偷看不該看的東西」。
# 主要責任:
#   1. test_degradations_are_deterministic() 同 seed 同結果
#   2. test_degradations_move_quality_in_the_expected_direction() 方向正確
#   3. test_temperature_scaling_never_changes_predictions() 只動信心
#   4. test_quality_signals_cannot_see_labels() 介面上就看不到
#   5. test_select_severity_follows_the_declared_rule() 不是事後挑的
# 維護提醒:
#   - 不得放寬「溫度校準不改變 argmax」這條。改變預測的縮放不是校準，
#     是在校準的名義下換一個模型。
#   - 不得讓品質訊號拿到標籤或 condition；那會讓 gate 變成作弊而不是偵測。
#   - 不得在本檔引用 Formal E2 的任何輸出。
#   - v0.1.0 新增：D/U/Q gate 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_perception_gate.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect

import numpy as np
import pytest

from pcmef.core.constants import N_CLASSES, TOF_RECORDING_POINTS, TOF_SCHEMA
from pcmef.perception.gate import (
    GateRule,
    apply_gate,
    calibration_metrics,
    confidence_weighted_arbiter,
    duq_signals,
    fit_temperature,
    paired_bootstrap_delta,
    quality_signals,
    softmax,
)
from pcmef.perception.stress import (
    DEGRADED_TARGET_BAND,
    SEVERITY_LADDER,
    UNAFFECTED_MINIMUM,
    StressError,
    degrade_tof,
    degrade_vision,
    select_severity,
)


def _image(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.gamma(2.0, 5.0, size=(32, 32, 3))
    base[10:20, 10:20] *= 40.0          # 高光，模擬 VCSEL specular
    return base


def _recording(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = np.empty((TOF_RECORDING_POINTS, len(TOF_SCHEMA)))
    out[:, TOF_SCHEMA.index("distance_mm")] = rng.normal(45.0, 0.5, TOF_RECORDING_POINTS)
    out[:, TOF_SCHEMA.index("ambient_rate_mcps")] = rng.normal(0.08, 0.005, TOF_RECORDING_POINTS)
    out[:, TOF_SCHEMA.index("signal_rate_mcps")] = rng.normal(2.0e5, 1e3, TOF_RECORDING_POINTS)
    out[:, TOF_SCHEMA.index("sigma_like")] = rng.normal(3.0, 0.1, TOF_RECORDING_POINTS)
    return np.clip(out, 0.0, None)


# ---------------------------------------------------------------------------
# 劣化算子
# ---------------------------------------------------------------------------


def test_degradations_are_deterministic():
    image, recording = _image(), _recording()
    a, _ = degrade_vision(image, 1.0, 7)
    b, _ = degrade_vision(image, 1.0, 7)
    assert np.array_equal(a, b)
    c, _ = degrade_tof(recording, 1.0, 7)
    d, _ = degrade_tof(recording, 1.0, 7)
    assert np.array_equal(c, d)


def test_degradations_change_with_severity():
    image = _image()
    mild, _ = degrade_vision(image, 0.25, 1)
    harsh, _ = degrade_vision(image, 3.0, 1)
    assert not np.allclose(mild, harsh)


def test_degradations_reject_a_negative_severity():
    for fn, payload in ((degrade_vision, _image()), (degrade_tof, _recording())):
        with pytest.raises(StressError) as excinfo:
            fn(payload, -1.0, 0)
        assert excinfo.value.reason == "BAD_SEVERITY"


def test_degradations_move_quality_in_the_expected_direction():
    """失焦必須降低銳利度；ambient flood + 衰減必須降低 SNR。

    這一條是 gate 能運作的前提：品質訊號若對劣化沒有反應，
    路由就只是在猜。
    """
    image, recording = _image(), _recording()
    clean = quality_signals(image, recording)

    blurred, _ = degrade_vision(image, 2.0, 3)
    assert quality_signals(blurred, recording)["vision_sharpness"] < clean["vision_sharpness"]

    flooded, _ = degrade_tof(recording, 2.0, 3)
    assert quality_signals(image, flooded)["tof_snr"] < clean["tof_snr"]


def test_vision_degradation_leaves_tof_quality_alone():
    """劣化必須只作用在自己的模態上，否則 condition 的語意就混了。"""
    image, recording = _image(), _recording()
    blurred, _ = degrade_vision(image, 2.0, 5)
    assert quality_signals(image, recording)["tof_snr"] == pytest.approx(
        quality_signals(blurred, recording)["tof_snr"]
    )


def test_tof_degradation_leaves_vision_quality_alone():
    image, recording = _image(), _recording()
    flooded, _ = degrade_tof(recording, 2.0, 5)
    assert quality_signals(image, recording)["vision_sharpness"] == pytest.approx(
        quality_signals(image, flooded)["vision_sharpness"]
    )


# ---------------------------------------------------------------------------
# 校準
# ---------------------------------------------------------------------------


def test_temperature_scaling_never_changes_predictions():
    """T > 0 的縮放是單調的，因此 argmax 不動。改變預測的不叫校準。"""
    rng = np.random.default_rng(0)
    logits = rng.normal(size=(200, N_CLASSES)) * 3.0
    base = softmax(logits, 1.0).argmax(1)
    for temperature in (0.2, 0.5, 1.0, 2.0, 8.0):
        assert np.array_equal(softmax(logits, temperature).argmax(1), base)


def test_fit_temperature_reduces_nll_on_overconfident_logits():
    rng = np.random.default_rng(1)
    labels = rng.integers(0, N_CLASSES, size=400)
    logits = rng.normal(size=(400, N_CLASSES))
    logits[np.arange(400), labels] += 1.0
    logits *= 6.0                                   # 刻意過度自信
    temperature = fit_temperature(logits, labels)
    before = calibration_metrics(softmax(logits, 1.0), labels)
    after = calibration_metrics(softmax(logits, temperature), labels)
    assert temperature > 1.0
    assert after["nll"] < before["nll"]
    assert after["ece"] < before["ece"]
    assert after["accuracy"] == before["accuracy"]


def test_calibration_metrics_on_a_perfectly_calibrated_case():
    """完全確定且全對 -> NLL 與 Brier 皆為 0，ECE 為 0。"""
    labels = np.array([0, 1, 2, 3])
    proba = np.eye(N_CLASSES)
    metrics = calibration_metrics(proba, labels)
    assert metrics["nll"] == pytest.approx(0.0, abs=1e-9)
    assert metrics["brier"] == pytest.approx(0.0, abs=1e-12)
    assert metrics["ece"] == pytest.approx(0.0, abs=1e-12)


def test_quality_signals_cannot_see_labels():
    """介面上就拿不到標籤 —— 這比寫一句「不要看」可靠。"""
    parameters = list(inspect.signature(quality_signals).parameters)
    assert parameters == ["rgb", "tof"]


# ---------------------------------------------------------------------------
# severity 選定
# ---------------------------------------------------------------------------


def test_select_severity_follows_the_declared_rule():
    lo, hi = DEGRADED_TARGET_BAND
    ladder = {
        0.25: {"vision": 0.95, "tof": 0.99},          # 還沒壞
        0.5: {"vision": 0.80, "tof": 0.99},           # 還在帶外
        1.0: {"vision": 0.50, "tof": 0.99},           # 命中
        2.0: {"vision": 0.20, "tof": 0.99},           # 太低
        3.0: {"vision": 0.10, "tof": 0.60},           # 另一邊也壞了
    }
    result = select_severity(ladder, "vision")
    assert result["selected_severity"] == 1.0
    assert result["rule_satisfied"] is True
    assert lo <= 0.50 <= hi


def test_select_severity_reports_a_fallback_instead_of_pretending():
    """沒有任何一格滿足規則時必須具名記錄，不得假裝規則過了。"""
    ladder = {s: {"vision": 0.99, "tof": 0.99} for s in SEVERITY_LADDER}
    result = select_severity(ladder, "vision")
    assert result["rule_satisfied"] is False
    assert result["fallback_used"] is True


def test_select_severity_requires_the_other_modality_to_survive():
    """被劣化的模態落進帶內，但另一邊也壞了 -> 不算滿足規則。"""
    ladder = {s: {"vision": 0.50, "tof": 0.40} for s in SEVERITY_LADDER}
    result = select_severity(ladder, "vision")
    assert result["rule_satisfied"] is False
    assert UNAFFECTED_MINIMUM > 0.40


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def _rule(**overrides) -> GateRule:
    base = dict(
        q_vision_threshold=0.0, q_tof_threshold=0.0, disagreement_threshold=0.5,
        fusion_weight=0.5, temperature_vision=1.0, temperature_tof=1.0,
    )
    base.update(overrides)
    return GateRule(**base)


def _signals(d, qv, qt, uv=0.1, ut=0.1):
    return {
        "D": np.asarray(d), "Q_vision": np.asarray(qv), "Q_tof": np.asarray(qt),
        "U_vision": np.asarray(uv) * np.ones(len(d)),
        "U_tof": np.asarray(ut) * np.ones(len(d)),
    }


def test_gate_trusts_the_intact_modality_when_one_is_degraded():
    v = np.array([[0.9, 0.1, 0.0, 0.0]])
    t = np.array([[0.0, 0.0, 0.1, 0.9]])
    # vision 品質低於門檻、tof 正常 -> 應該相信 tof
    proba, decisions = apply_gate(
        _rule(q_vision_threshold=1.0), v, t, _signals([0.9], [0.0], [5.0])
    )
    assert decisions["route"][0] == "trust_tof"
    assert np.array_equal(proba[0], t[0])

    proba, decisions = apply_gate(
        _rule(q_tof_threshold=1.0), v, t, _signals([0.9], [5.0], [0.0])
    )
    assert decisions["route"][0] == "trust_vision"
    assert np.array_equal(proba[0], v[0])


def test_gate_escalates_only_when_both_are_healthy_and_they_disagree():
    v = np.array([[0.9, 0.1, 0.0, 0.0]])
    t = np.array([[0.0, 0.0, 0.1, 0.9]])
    _, decisions = apply_gate(_rule(), v, t, _signals([0.9], [5.0], [5.0]))
    assert decisions["route"][0] == "escalated"
    assert bool(decisions["escalated"][0])

    # 一致時不升級。
    _, agree = apply_gate(_rule(), v, v.copy(), _signals([0.0], [5.0], [5.0]))
    assert agree["route"][0] == "fusion"
    assert not bool(agree["escalated"][0])


def test_gate_falls_back_to_fusion_by_default():
    v = np.array([[0.7, 0.3, 0.0, 0.0]])
    t = np.array([[0.6, 0.4, 0.0, 0.0]])
    proba, decisions = apply_gate(_rule(), v, t, _signals([0.1], [5.0], [5.0]))
    assert decisions["route"][0] == "fusion"
    assert proba[0] == pytest.approx(0.5 * v[0] + 0.5 * t[0])


def test_arbiter_favours_the_more_confident_modality():
    v = np.array([0.97, 0.01, 0.01, 0.01])
    t = np.array([0.28, 0.24, 0.24, 0.24])
    out = confidence_weighted_arbiter(0, v, t, {"U_vision": 0.05, "U_tof": 0.98})
    assert out.argmax() == 0
    assert out.sum() == pytest.approx(1.0)


def test_duq_disagreement_is_zero_for_identical_distributions():
    p = np.array([[0.7, 0.1, 0.1, 0.1]])
    signals = duq_signals(p, p.copy(), {"vision_sharpness": np.array([1.0]),
                                        "tof_snr": np.array([1.0])})
    assert signals["D"][0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------


def test_paired_bootstrap_resamples_scenarios_not_rows():
    """重抽單位是 scenario；同一場景的四個 condition 不是獨立樣本。"""
    units = np.repeat([f"s{i}" for i in range(20)], 4)
    a = np.ones(80, dtype=bool)
    b = np.zeros(80, dtype=bool)
    result = paired_bootstrap_delta(a, b, units, replicates=500)
    assert result["n_units"] == 20
    assert result["delta_accuracy"] == pytest.approx(1.0)
    assert result["significant_at_95"] is True


def test_paired_bootstrap_reports_no_difference_when_there_is_none():
    units = np.repeat([f"s{i}" for i in range(20)], 4)
    rng = np.random.default_rng(0)
    shared = rng.random(80) > 0.5
    result = paired_bootstrap_delta(shared, shared.copy(), units, replicates=500)
    assert result["delta_accuracy"] == pytest.approx(0.0)
    assert result["ci_lower"] == pytest.approx(0.0)
    assert result["ci_upper"] == pytest.approx(0.0)
    assert result["significant_at_95"] is False
