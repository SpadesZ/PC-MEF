# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.simulation.paired 與已凍結的 calibrated_simulation.lock /
#         initial_simulation.lock；算圖測試需要 mitsuba，缺了就 skip。
#         **不讀任何真實資料**，不碰 FORMAL_E1_FINAL，不寫 repo 內 artifact
#         （只用 tmp_path）。
# 檔案路徑: tests/simulation/test_paired.py
# 產生時間: 2026-08-31 02:55 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 E1->E2 成對資料橋的三件事 —— 模擬器身分確實是 E1 最終保留
#           狀態、RGB 與 ToF 來自同一個 scenario、以及同一身分可逐位元重現。
# 模組定位: 成對資料橋的驗收。它不驗證 perception 表現（那不是這一階段的事），
#           只驗證「這一張 RGB 與這一筆 ToF 是同一個場景」這個宣稱站得住。
# 主要責任:
#   1. test_ambient_uses_the_accepted_calibrated_values() 兩項校準值到位
#   2. test_inhibited_parameters_are_the_frozen_initial_values() 五項保留值到位
#   3. test_identity_hash_changes_with_any_scientific_input() 身分涵蓋完整
#   4. test_paired_seeds_are_disjoint_from_every_earlier_use() 種子不重用
#   5. test_generate_paired_sample_emits_one_scenario_two_modalities() 成對性
# 維護提醒:
#   - 不得把成對性測成「兩個檔案都存在」。存在不代表同源；必須驗證兩者
#     來自同一個 ScenarioConfig 與同一個 scene_hash。
#   - 不得在本檔重新擬合任何參數，也不得放寬「被抑制參數等於凍結初值」
#     這條斷言 —— 它是 E1 結論在下游還成立的唯一憑據。
#   - v0.1.0 新增：E1->E2 成對資料橋驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/simulation/test_paired.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_RECORDING_POINTS, TOF_SCHEMA
from pcmef.simulation.paired import (
    PAIRED_SEED_BASE,
    load_calibrated_simulator,
    scenario_seed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "freeze" / "calibrated_simulation.lock.json").exists(),
    reason="calibrated_simulation.lock is not frozen in this working copy",
)


@pytest.fixture(scope="module")
def simulator():
    return load_calibrated_simulator(REPO_ROOT / "freeze", REPO_ROOT)


@pytest.fixture(scope="module")
def initial_values():
    return json.loads(
        (REPO_ROOT / "freeze" / "initial_simulation.lock.json").read_text(
            encoding="utf-8"
        )
    )["payload"]["initial_parameter_values"]


# ---------------------------------------------------------------------------
# 模擬器身分就是 E1 最終保留狀態
# ---------------------------------------------------------------------------


def test_ambient_uses_the_accepted_calibrated_values(simulator, initial_values):
    """MAPPING_AMBIENT 是唯一 CONVERGED 的階段，它的兩個值必須真的被套用。"""
    identity, calibration = simulator
    for name in ("ambient_energy_to_mcps", "ambient_jitter_relative"):
        applied = getattr(calibration, name).value
        assert applied == identity.fitted_parameters[name]
        assert applied != float(initial_values[name]), (
            f"{name} still equals its frozen initial value; the accepted "
            "MAPPING_AMBIENT update was not applied"
        )


def test_inhibited_parameters_are_the_frozen_initial_values(simulator, initial_values):
    """被抑制的階段其參數必須逐位元等於凍結初值，不得被重新擬合。"""
    identity, calibration = simulator
    assert set(identity.inhibited_stages) == {
        "SCENE_GEOMETRY_SURFACE_FOIL",
        "MAPPING_SIGNAL",
        "MAPPING_SIGMA",
    }
    for name in (
        "signal_energy_to_mcps",
        "noise_relative_sigma",
        "sigma_width_to_mm",
        "sigma_multipath_weight",
    ):
        assert getattr(calibration, name).value == float(initial_values[name])
    assert identity.scene_constants["sensor.fov_deg"] == float(
        initial_values["sensor.fov_deg"]
    )


def test_every_scale_names_its_provenance(simulator):
    """沒有 placeholder 殘留，且每一項都說得出自己從哪來。"""
    _identity, calibration = simulator
    assert calibration.placeholder_names() == []
    for name, scale in calibration.scales().items():
        if scale.derived:
            continue
        assert scale.source, f"{name} has no recorded source"


def test_simulation_settings_are_the_frozen_not_fitted_values(simulator, initial_values):
    identity, _calibration = simulator
    settings = identity.simulation_settings
    assert settings["spp"] == int(initial_values["spp"])
    assert settings["resolution"] == [int(v) for v in initial_values["resolution"]]
    assert settings["temporal_bins"] == int(initial_values["temporal_bins"])
    assert settings["n_samples_per_recording"] == TOF_RECORDING_POINTS
    assert settings["feature_order"] == list(TOF_SCHEMA)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda i: replace(i, fitted_parameters={**i.fitted_parameters,
                                                    "ambient_energy_to_mcps": 1.0}),
            id="ambient-gain",
        ),
        pytest.param(
            lambda i: replace(i, scene_constants={"sensor.fov_deg": 44.0}),
            id="scene-constant",
        ),
        pytest.param(
            lambda i: replace(i, simulation_settings={**i.simulation_settings,
                                                      "spp": 32}),
            id="spp",
        ),
        pytest.param(
            lambda i: replace(i, surrogate_hash="different"), id="surrogate-hash"
        ),
    ],
)
def test_identity_hash_changes_with_any_scientific_input(simulator, mutate):
    """身分少涵蓋一項，兩組不同的模擬器就會共用一個 id。"""
    identity, _calibration = simulator
    assert mutate(identity).identity_hash() != identity.identity_hash()


# ---------------------------------------------------------------------------
# 種子
# ---------------------------------------------------------------------------


def test_paired_seeds_are_disjoint_from_every_earlier_use():
    """成對資料是新的抽樣，不是既有實現的重播。"""
    from pcmef.experiments.calibration_objective import CRN_SEEDS, VERIFICATION_SEEDS
    from pcmef.experiments.e1_final import scenario_seed_matrix

    used = {
        scenario_seed(c, i) for c in CLASS_ORDER for i in range(50)
    }
    e1 = scenario_seed_matrix()
    earlier = (
        set(CRN_SEEDS.values())
        | set(VERIFICATION_SEEDS.values())
        | set(e1["optical_transport_seeds"].values())
        | set(e1["acquisition_seed_matrix"].values())
    )
    assert not used & earlier
    assert min(used) >= PAIRED_SEED_BASE


def test_scenario_seed_is_deterministic_and_class_separated():
    assert scenario_seed("Empty", 0) == scenario_seed("Empty", 0)
    per_class = {c: {scenario_seed(c, i) for i in range(10)} for c in CLASS_ORDER}
    seen: set[int] = set()
    for ids in per_class.values():
        assert not ids & seen, "two classes share a scenario seed"
        seen |= ids


# ---------------------------------------------------------------------------
# 成對性
# ---------------------------------------------------------------------------


def test_one_scenario_config_serves_both_modalities(simulator):
    """成對性的來源：兩種模態讀同一份 config，因此 scene_hash 只有一個。"""
    from pcmef.simulation.paired import _scenario_config

    identity, _calibration = simulator
    config = _scenario_config(identity, "Bubbly", scenario_seed("Bubbly", 0))
    assert config.render_rgb and config.render_transient
    # 同一份 config 重建一次必須得到同一個 scene_hash。
    again = _scenario_config(identity, "Bubbly", scenario_seed("Bubbly", 0))
    assert config.scene_hash() == again.scene_hash()
    # 不同 class 或不同 seed 必須是不同的場景。
    assert config.scene_hash() != _scenario_config(
        identity, "Misty", scenario_seed("Bubbly", 0)
    ).scene_hash()
    assert config.scene_hash() != _scenario_config(
        identity, "Bubbly", scenario_seed("Bubbly", 1)
    ).scene_hash()


def test_medium_values_are_frozen_scalars_not_placeholders(simulator):
    """介質密度是 NO_FREE_PARAMETERS 的凍結初值，不是暫用值。"""
    from pcmef.simulation.paired import _scenario_config

    identity, _calibration = simulator
    for class_label in CLASS_ORDER:
        config = _scenario_config(identity, class_label, scenario_seed(class_label, 0))
        assert config.placeholder_keys() == set()
        assert not config.uses_placeholder_medium()


def test_generate_paired_sample_emits_one_scenario_two_modalities(simulator, tmp_path):
    """實跑一個 sample：RGB 與 ToF 同時產出、同址、同 scenario_hash。"""
    pytest.importorskip("mitsuba")
    from pcmef.simulation.mitsuba_adapter import require_mitsuba
    from pcmef.simulation.paired import _scenario_config, generate_paired_sample

    require_mitsuba()
    identity, calibration = simulator
    seed = scenario_seed("Empty", 0)
    sample = generate_paired_sample(
        identity, calibration, "empty_probe", "Empty", seed, tmp_path
    )

    assert sample.scenario_hash == _scenario_config(identity, "Empty", seed).scene_hash()
    assert Path(sample.rgb_exr_path).parent == Path(sample.tof_path).parent
    assert set(sample.seed_family.values()) == {seed}
    assert sample.tof_shape == [TOF_RECORDING_POINTS, len(TOF_SCHEMA)]
    assert sample.feature_order == list(TOF_SCHEMA)

    tof = np.load(sample.tof_path)
    assert tof.shape == (TOF_RECORDING_POINTS, len(TOF_SCHEMA))
    assert np.all(np.isfinite(tof))
    assert Path(sample.rgb_exr_path).exists()
    assert Path(sample.rgb_png_path).exists()

    # 同一身分重跑一次必須逐位元相同。
    repeat = generate_paired_sample(
        identity, calibration, "empty_probe_again", "Empty", seed, tmp_path
    )
    assert repeat.tof_array_hash == sample.tof_array_hash
    assert repeat.rgb_exr_sha256 == sample.rgb_exr_sha256


# ---------------------------------------------------------------------------
# P0-1：RGB 必須用支援參與介質的積分器
# ---------------------------------------------------------------------------


def test_rgb_integrator_is_volumetric_exactly_when_a_medium_is_present(simulator):
    """`path` 靜默忽略 interior medium，於是 (Empty, Misty) 與
    (Water-filled, Bubbly) 在 RGB 上長得一樣 —— 因為唯一能區分它們的介質
    根本沒有進入光傳輸。積分器必須依場景內容選，與 transient 側一致。
    """
    pytest.importorskip("mitsuba")
    import mitsuba as mi

    from pcmef.experiments.calibration_objective import scene_overrides
    from pcmef.simulation.mitsuba_adapter import build_scene_dict, require_mitsuba
    from pcmef.simulation.paired import _scenario_config

    require_mitsuba()
    identity, _calibration = simulator
    expected = {
        "Empty": "path",            # 無介質
        "Water-filled": "volpath",
        "Bubbly": "volpath",
        "Misty": "volpath",
    }
    for class_label, integrator in expected.items():
        config = _scenario_config(identity, class_label, scenario_seed(class_label, 0))
        with scene_overrides(identity.scene_constants):
            scene = build_scene_dict(mi, config)
        has_medium = any(
            isinstance(node, dict) and "interior" in node
            for node in scene.values()
            if isinstance(node, dict)
        )
        assert scene["integrator"]["type"] == integrator, (
            f"{class_label}: integrator {scene['integrator']['type']} with "
            f"has_medium={has_medium}"
        )
        assert has_medium == (integrator == "volpath")


# ---------------------------------------------------------------------------
# P1：RGB spp 與 ToF spp 解耦
# ---------------------------------------------------------------------------


def test_rgb_spp_does_not_change_the_physical_scene(simulator):
    """spp 是數值積分精度，不是場景物理狀態。"""
    from pcmef.perception.dataset import physical_scene_family
    from pcmef.simulation.paired import _scenario_config

    identity, _calibration = simulator
    config = _scenario_config(identity, "Empty", scenario_seed("Empty", 0))
    base = {
        **config.to_dict(),
        "scene_constants_applied": dict(identity.scene_constants),
    }
    high_rgb = {**base, "rgb_render_spp": 4096}
    assert physical_scene_family(base) == physical_scene_family(high_rgb)


def test_tof_spp_stays_at_the_e1_frozen_value(simulator):
    """ToF 的 spp 不得被 RGB 的選擇帶著走。"""
    from pcmef.simulation.paired import _scenario_config

    identity, _calibration = simulator
    config = _scenario_config(identity, "Empty", scenario_seed("Empty", 0))
    assert config.spp == int(identity.simulation_settings["spp"]) == 16


# ---------------------------------------------------------------------------
# physical family 變異
# ---------------------------------------------------------------------------


def test_family_variation_is_deterministic_and_inside_preregistered_ranges():
    from pcmef.simulation.paired import NUISANCE_RANGES, family_variation

    lo_off, hi_off = NUISANCE_RANGES["geometry.lateral_offset_mm"]
    lo_irr, hi_irr = NUISANCE_RANGES["lighting.irradiance"]
    for class_label in CLASS_ORDER:
        for index in range(20):
            first = family_variation(class_label, index, 20)
            assert first == family_variation(class_label, index, 20)
            assert lo_off <= first.lateral_offset_mm <= hi_off
            assert lo_irr <= first.irradiance <= hi_irr


def test_families_within_a_class_are_distinct_scenes():
    from pcmef.simulation.paired import family_variation

    for class_label in CLASS_ORDER:
        points = {
            (round(v.lateral_offset_mm, 9), round(v.irradiance, 9))
            for v in (family_variation(class_label, i, 20) for i in range(20))
        }
        assert len(points) == 20, f"{class_label} families collapse onto {len(points)} scenes"


def test_family_seeds_are_unique_and_disjoint_from_earlier_ranges():
    from pcmef.experiments.calibration_objective import CRN_SEEDS, VERIFICATION_SEEDS
    from pcmef.experiments.e1_final import scenario_seed_matrix
    from pcmef.simulation.paired import family_scenario_seed

    seeds = [
        family_scenario_seed(c, f, r)
        for c in CLASS_ORDER for f in range(20) for r in range(5)
    ]
    assert len(set(seeds)) == len(seeds) == 400

    e1 = scenario_seed_matrix()
    earlier = (
        set(CRN_SEEDS.values())
        | set(VERIFICATION_SEEDS.values())
        | set(e1["optical_transport_seeds"].values())
        | set(e1["acquisition_seed_matrix"].values())
        | {scenario_seed(c, i) for c in CLASS_ORDER for i in range(100)}
    )
    assert not set(seeds) & earlier
