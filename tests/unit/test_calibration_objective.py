# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.experiments.calibration_objective 與 pcmef.stats.metrics；
#         以合成陣列驗證度量與快取，不算圖、不讀 calibration partition，
#         也不碰 held-out。由 pytest 收集執行，不寫出任何 artifact。
# 檔案路徑: tests/unit/test_calibration_objective.py
# 產生時間: 2026-08-30 21:15 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 16 項目標函數的算法、等權加總、場景常數覆寫的還原保證，
#           以及算圖快取的鍵真的涵蓋每一個會改變結果的科學輸入。
# 模組定位: 目標函數層的驗收。它不驗證校準結論，只驗證「同樣的輸入給同樣的
#           數字、不同的輸入給不同的鍵」——這兩件事一旦不成立，
#           optimizer 找到的最小值就沒有意義。
# 主要責任:
#   1. test_objective_terms_are_w1_over_frozen_scale() 逐格對上 W1 / s_f
#   2. test_sum_cells_is_uniform_weighting() 等權且不吸收非有限值
#   3. test_scene_overrides_restore_on_exception() 例外後仍還原
#   4. test_render_cache_key_covers_*() 少一項輸入就會共用結果
#   5. test_render_cache_is_lru_bounded() 逐出只影響時間不影響結果
# 維護提醒:
#   - 不得在本檔用真實 recording 當測試資料；目標函數的正確性與資料無關，
#     用真實資料會讓測試同時依賴一次 calibration access。
#   - 不得放寬 cache 鍵的比對成「差不多就算不同」。鍵相同代表結果被重用，
#     那必須是逐位元等價，不是近似。
#   - v0.1.0 新增：首版目標函數驗收（CAL-PREREG-003 formal calibration）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_objective.py -v
# ------------------------------------------------------------

from __future__ import annotations

import math

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.experiments.calibration_identity import all_cells
from pcmef.experiments.calibration_objective import (
    CRN_SEEDS,
    SCENE_CONSTANT_BY_DIMENSION,
    SURROGATE_DIMENSIONS,
    VERIFICATION_SEEDS,
    RealCalibration,
    RenderCache,
    objective_terms,
    scene_constant_snapshot,
    scene_overrides,
    sum_cells,
)
from pcmef.stats.metrics import wasserstein_w1

S_F = {"distance_mm": 15.75, "ambient_rate_mcps": 0.0156,
       "signal_rate_mcps": 1.2344, "sigma_like": 0.1506}


def _real(seed: int = 7) -> RealCalibration:
    rng = np.random.default_rng(seed)
    values = {
        c: {f: rng.normal(loc=10.0 + i, scale=2.0, size=400)
            for i, f in enumerate(TOF_SCHEMA)}
        for c in CLASS_ORDER
    }
    return RealCalibration(
        values=values, raw_hash="synthetic",
        recordings_per_class={c: 4 for c in CLASS_ORDER},
        ledger_entry={"index": 0},
    )


def _simulated(seed: int = 11) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    return {c: rng.normal(loc=11.0, scale=1.5, size=(500, len(TOF_SCHEMA)))
            for c in CLASS_ORDER}


# ---------------------------------------------------------------------------
# 度量
# ---------------------------------------------------------------------------


def test_objective_terms_are_w1_over_frozen_scale():
    real, simulated = _real(), _simulated()
    nw, raw = objective_terms(simulated, real, S_F)
    assert len(nw) == len(raw) == 16
    for class_label, feature in all_cells():
        key = f"{class_label}|{feature}"
        index = TOF_SCHEMA.index(feature)
        expected = wasserstein_w1(
            real.values[class_label][feature], simulated[class_label][:, index]
        )
        assert raw[key] == expected
        assert nw[key] == expected / S_F[feature]


def test_objective_uses_the_right_feature_column():
    """欄位錯位會給出一個完全算得出來、但沒有意義的目標值。"""
    real = _real()
    simulated = {
        c: np.tile(np.arange(len(TOF_SCHEMA), dtype=float), (500, 1))
        for c in CLASS_ORDER
    }
    _, raw = objective_terms(simulated, real, S_F)
    for class_label in CLASS_ORDER:
        for index, feature in enumerate(TOF_SCHEMA):
            expected = wasserstein_w1(
                real.values[class_label][feature], np.full(500, float(index))
            )
            assert raw[f"{class_label}|{feature}"] == expected


def test_sum_cells_is_uniform_weighting():
    nw = {f"{c}|{f}": 1.5 for c, f in all_cells()}
    assert sum_cells(nw, all_cells()) == pytest.approx(16 * 1.5)
    subset = [("Empty", "distance_mm"), ("Empty", "sigma_like")]
    assert sum_cells(nw, subset) == pytest.approx(2 * 1.5)


def test_sum_cells_propagates_non_finite():
    """一格算不出來就是整個目標算不出來，不得被其餘 15 格稀釋。"""
    nw = {f"{c}|{f}": 1.0 for c, f in all_cells()}
    nw["Misty|sigma_like"] = math.inf
    assert sum_cells(nw, all_cells()) == math.inf
    assert sum_cells(nw, [("Empty", "distance_mm")]) == 1.0


def test_frozen_seed_sets_are_disjoint():
    """CRN 與 verification 必須是不同的種子，否則驗證什麼都沒驗到。"""
    assert set(CRN_SEEDS) == set(VERIFICATION_SEEDS) == set(CLASS_ORDER)
    assert not set(CRN_SEEDS.values()) & set(VERIFICATION_SEEDS.values())


def test_every_calibrated_dimension_has_exactly_one_binding():
    seven = {
        "sensor.fov_deg", "ambient_energy_to_mcps", "ambient_jitter_relative",
        "signal_energy_to_mcps", "noise_relative_sigma", "sigma_width_to_mm",
        "sigma_multipath_weight",
    }
    for name in seven:
        in_scene = name in SCENE_CONSTANT_BY_DIMENSION
        in_surrogate = name in SURROGATE_DIMENSIONS
        assert in_scene != in_surrogate, f"{name} must bind to exactly one side"


# ---------------------------------------------------------------------------
# 場景常數覆寫
# ---------------------------------------------------------------------------


def test_scene_overrides_apply_and_restore():
    from pcmef.simulation import mitsuba_adapter as scene_module

    before = scene_module._SENSOR_FOV_DEG
    with scene_overrides({"sensor.fov_deg": 33.25}):
        assert scene_module._SENSOR_FOV_DEG == 33.25
    assert scene_module._SENSOR_FOV_DEG == before


def test_scene_overrides_restore_on_exception():
    """一次評估拋出例外時若沒有還原，後面每一次都在無人宣告的場景上進行。"""
    from pcmef.simulation import mitsuba_adapter as scene_module

    before = scene_module._SENSOR_FOV_DEG
    with pytest.raises(ZeroDivisionError):
        with scene_overrides({"sensor.fov_deg": 12.5}):
            raise ZeroDivisionError("simulated crash")
    assert scene_module._SENSOR_FOV_DEG == before


def test_scene_overrides_reject_an_unbound_dimension():
    from pcmef.experiments.calibration_identity import IdentityError

    with pytest.raises(IdentityError):
        with scene_overrides({"not_a_scene_constant": 1.0}):
            pass


def test_scene_snapshot_changes_with_every_scene_constant():
    """快照少記一項，就等於宣稱那一項永遠不會變。"""
    from pcmef.simulation import mitsuba_adapter as scene_module

    baseline = scene_constant_snapshot()
    for name, value in list(baseline.items()):
        original = getattr(scene_module, name)
        try:
            if isinstance(original, dict):
                key = next(iter(original))
                setattr(scene_module, name, {**original, key: 12345.0})
            elif isinstance(original, (int, float)):
                setattr(scene_module, name, float(original) + 1.0)
            else:
                setattr(scene_module, name, f"{original}-changed")
            assert scene_constant_snapshot() != baseline, (
                f"{name} is not covered by the snapshot"
            )
        finally:
            setattr(scene_module, name, original)
    assert scene_constant_snapshot() == baseline


# ---------------------------------------------------------------------------
# 算圖快取
# ---------------------------------------------------------------------------


def test_render_cache_returns_the_stored_object():
    cache = RenderCache(capacity=4)
    payload = (np.zeros(2), np.ones(2), np.arange(2))
    assert cache.get("k") is None
    cache.put("k", payload)
    assert cache.get("k") is payload
    stats = cache.stats()
    assert stats["render_cache_hits"] == 1
    assert stats["render_cache_misses"] == 1
    assert stats["renders_executed"] == 1


def test_render_cache_is_lru_bounded():
    cache = RenderCache(capacity=2)
    for key in ("a", "b"):
        cache.put(key, key)
    cache.get("a")           # a 變成最近使用
    cache.put("c", "c")      # 逐出最久未用的 b
    assert cache.get("b") is None
    assert cache.get("a") == "a"
    assert cache.stats()["render_cache_evictions"] == 1


@pytest.mark.parametrize("field, value", [("spp", 32), ("temporal_bins", 64)])
def test_render_cache_key_covers_simulation_settings(field, value):
    """改變任何影響 cube 的設定都必須改變鍵，否則兩組輸入會共用一個結果。"""
    pytest.importorskip("mitsuba")
    from pcmef.experiments.calibration_identity import load_frozen_identity
    from pcmef.experiments.calibration_objective import Simulator
    from pcmef.simulation.mitsuba_adapter import require_mitsuba

    require_mitsuba()
    identity = load_frozen_identity(allow_opened_heldout=True)
    simulator = Simulator(identity=identity)
    baseline = simulator._render_key("Empty", CRN_SEEDS["Empty"])
    setattr(simulator, field, value)
    assert simulator._render_key("Empty", CRN_SEEDS["Empty"]) != baseline


def test_render_cache_key_covers_class_seed_and_scene(monkeypatch):
    pytest.importorskip("mitsuba")
    from pcmef.experiments.calibration_identity import load_frozen_identity
    from pcmef.experiments.calibration_objective import Simulator
    from pcmef.simulation.mitsuba_adapter import require_mitsuba

    require_mitsuba()
    identity = load_frozen_identity(allow_opened_heldout=True)
    simulator = Simulator(identity=identity)
    baseline = simulator._render_key("Empty", CRN_SEEDS["Empty"])

    assert simulator._render_key("Misty", CRN_SEEDS["Empty"]) != baseline
    assert simulator._render_key("Empty", VERIFICATION_SEEDS["Empty"]) != baseline
    with scene_overrides({"sensor.fov_deg": 41.0}):
        assert simulator._render_key("Empty", CRN_SEEDS["Empty"]) != baseline
    assert simulator._render_key("Empty", CRN_SEEDS["Empty"]) == baseline


def test_n_samples_is_outside_the_render_key_but_inside_the_evaluation_identity():
    """`n_samples` 不改變 cube，卻改變評估結果 —— 兩個鍵的分工必須各自正確。

    把它塞進算圖鍵只會讓 cube 白白重算；把它漏出評估身分，則會讓兩次
    **科學上不同**的評估共用一個結果。
    """
    pytest.importorskip("mitsuba")
    from pcmef.experiments.calibration_formal import _Evaluator
    from pcmef.experiments.calibration_identity import load_frozen_identity
    from pcmef.experiments.calibration_objective import Simulator
    from pcmef.simulation.mitsuba_adapter import require_mitsuba

    require_mitsuba()
    identity = load_frozen_identity(allow_opened_heldout=True)
    simulator = Simulator(identity=identity)
    render_key = simulator._render_key("Empty", CRN_SEEDS["Empty"])

    evaluator = _Evaluator(
        identity=identity, simulator=simulator, real=_real(),
        stage_id="MAPPING_AMBIENT", dimensions=["ambient_energy_to_mcps"],
        declared_cells=all_cells(), frozen_parameters={},
        journal=None, budget=None, bounds={"ambient_energy_to_mcps": (0.0, 2.0)},
    )
    values = {"ambient_energy_to_mcps": 1.0}
    before = evaluator.run_identity_hash(values)

    simulator.n_samples = 250
    assert simulator._render_key("Empty", CRN_SEEDS["Empty"]) == render_key
    assert evaluator.run_identity_hash(values) != before


def test_evaluation_identity_covers_seed_mode_and_parameters():
    """換一組種子或換一個參數值，都必須是另一次評估。"""
    pytest.importorskip("mitsuba")
    from pcmef.experiments.calibration_formal import _Evaluator
    from pcmef.experiments.calibration_identity import load_frozen_identity
    from pcmef.experiments.calibration_objective import Simulator
    from pcmef.simulation.mitsuba_adapter import require_mitsuba

    require_mitsuba()
    identity = load_frozen_identity(allow_opened_heldout=True)
    evaluator = _Evaluator(
        identity=identity, simulator=Simulator(identity=identity),
        real=_real(), stage_id="MAPPING_AMBIENT",
        dimensions=["ambient_energy_to_mcps"], declared_cells=all_cells(),
        frozen_parameters={}, journal=None, budget=None,
        bounds={"ambient_energy_to_mcps": (0.0, 2.0)},
    )
    baseline = evaluator.run_identity_hash({"ambient_energy_to_mcps": 1.0})

    assert evaluator.run_identity_hash({"ambient_energy_to_mcps": 1.0}) == baseline
    # 相差一個 ULP 也必須是另一次評估。
    nudged = float(np.nextafter(1.0, 2.0))
    assert evaluator.run_identity_hash({"ambient_energy_to_mcps": nudged}) != baseline

    evaluator.seed_mode = "VERIFICATION"
    assert evaluator.run_identity_hash({"ambient_energy_to_mcps": 1.0}) != baseline
