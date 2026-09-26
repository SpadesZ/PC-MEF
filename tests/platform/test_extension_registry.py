# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 pcmef.platform.components / scenarios /
#         sensors / catalog。manifest 範例在 tests/platform/fixtures/extensions，
#         其餘一律寫在 tmp_path。不寫入任何 repo 路徑。
# 檔案路徑: tests/platform/test_extension_registry.py
# 產生時間: 2026-09-27 10:50 +08:00
# 版本: v0.1.1
# 功能說明: SAI Phase 3 第一片的驗收：Scenario Plugin 與 Sensor Adapter 能
#           註冊並被決定性地探索；重複與衝突一律拒絕；不合契約即失敗而不
#           退回預設；碩論的行為與常數不變；registry 提供下一片
#           （compatibility matrix）需要的結構化 metadata。
# 模組定位: Phase 3 第一片的驗收測試。
# 主要責任:
#   1. 內建元件由凍結常數推導，建立 registry 不改變任何常數
#   2. 探索與資料夾順序、檔案列舉順序無關，而且不匯入任何 plugin 程式碼
#   3. 身分衝突、保留命名空間、碩論類別名稱、標籤衝突全部拒絕
#   4. 各種不合契約的 manifest 都被拒絕並說出原因，查詢時沒有預設
#   5. describe() 足以支撐 compatibility matrix，而且明說「未評估相容性」
# 維護提醒:
#   - **不得把畸形 manifest 的測試改成只檢查「有沒有被拒絕」。** 每一條都
#     要斷言原因裡點到那個問題 —— 否則一條因為別的錯而被拒絕的 manifest
#     會讓測試通過，而它要守的那一條規則其實沒人在守。
#   - 不得把 fixtures 目錄裡的範例當成正式 plugin；它們的實作刻意不存在。
#   - v0.1.1 新增：觀測 schema 的維度一致性（layout 與 shape 的維度數、
#     通道維度必須寫明）與非有限數值（JSON 的 1e999、直接傳入的 inf / nan）
#     的對抗測試。對應 Phase 3 第二片。
#   - v0.1.0 新增：首版，對應 SAI Phase 3 第一片。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_extension_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

from pcmef.core import constants
from pcmef.platform import catalog, components, scenarios, sensors
from pcmef.platform.components import (
    KIND_SCENARIO,
    KIND_SENSOR,
    AmbiguousVersion,
    ComponentRegistry,
    ContractViolation,
    DuplicateComponent,
    RegistryFrozen,
    UnknownComponent,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "extensions"
FIXTURE_FILES = ("acme_ir_camera.json", "acme_sand.json", "acme_tof_vl53l1x.json")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _write(directory: Path, name: str, data) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


def _portable(described: dict) -> dict:
    """去掉 provenance.location（檔案路徑隨 tmp 目錄而變），其餘逐字比對。"""
    result = copy.deepcopy(described)
    for kind in (KIND_SCENARIO, KIND_SENSOR):
        for item in result[kind]:
            item["provenance"].pop("location")
    return result


# ---------------------------------------------------------------------------
# 1 內建元件：碩論被描述，不被改寫
# ---------------------------------------------------------------------------


def test_the_builtin_registry_holds_the_thesis_and_nothing_else():
    registry = catalog.builtin_registry()
    assert registry.frozen
    assert [c.identity.component_id for c in registry.components(KIND_SCENARIO)] == [
        "pcmef.scenario.bubbly", "pcmef.scenario.empty",
        "pcmef.scenario.misty", "pcmef.scenario.water-filled",
    ]
    assert [c.identity.component_id for c in registry.components(KIND_SENSOR)] == [
        "pcmef.sensor.rgb-camera", "pcmef.sensor.tof-vl53l0x",
    ]


def test_thesis_scenarios_follow_class_order_and_the_existing_medium_presets():
    from pcmef.simulation.scenario import MediumPreset

    built = scenarios.thesis_scenarios()
    assert [s.classification_label for s in built] == list(constants.CLASS_ORDER)
    for scenario in built:
        preset = MediumPreset.for_class(scenario.classification_label)
        assert scenario.template_id == f"pcmef.medium.{preset.value}"
        assert scenario.simulation_support == scenarios.SUPPORT_TEMPLATE
        assert scenario.parameters == (), (
            "thesis physics parameters are owned by the frozen ScenarioConfig"
        )


def test_the_registry_order_is_never_a_class_order():
    """registry 依 id 排序；類別順序只有 CLASS_ORDER 一個來源。"""
    listed = [c.classification_label
              for c in catalog.builtin_registry().components(KIND_SCENARIO)]
    assert listed != list(constants.CLASS_ORDER)
    assert constants.CLASS_ORDER == ("Empty", "Water-filled", "Bubbly", "Misty")


def test_the_tof_adapter_is_derived_from_the_frozen_constants():
    tof = catalog.builtin_registry().get(KIND_SENSOR, "pcmef.sensor.tof-vl53l0x")
    assert tuple(c.name for c in tof.observation.channels) == constants.TOF_SCHEMA
    assert tof.observation.shape == constants.TOF_RECORDING_SHAPE
    assert all(c.unit for c in tof.observation.channels)
    sigma = next(c for c in tof.observation.channels if c.name == "sigma_like")
    assert "NOT the VL53L0X internal Sigma register" in sigma.description
    assert tof.capabilities == {"simulate": True, "ingest": True}


def test_every_builtin_capability_points_at_code_that_exists():
    """宣告的能力指到真的程式碼（明確匯入驗證）；RGB 沒有 ingest 就不宣告。"""
    for component in catalog.builtin_components():
        assert catalog.verify_implementations(component) == (), component.identity
    rgb = catalog.builtin_registry().get(KIND_SENSOR, "pcmef.sensor.rgb-camera")
    assert rgb.capabilities == {"simulate": True, "ingest": False}


def test_building_registries_changes_no_frozen_constant():
    from pcmef.simulation.scenario import MediumPreset

    before = (constants.CLASS_ORDER, constants.TOF_SCHEMA,
              constants.TOF_RECORDING_SHAPE, tuple(MediumPreset))
    catalog.builtin_registry()
    catalog.discover([FIXTURES])
    after = (constants.CLASS_ORDER, constants.TOF_SCHEMA,
             constants.TOF_RECORDING_SHAPE, tuple(MediumPreset))
    assert after == before
    assert after[0] is before[0] and after[1] is before[1]


def test_there_is_no_module_level_registry_to_depend_on():
    """「匯入即註冊」讓結果取決於誰先匯入了誰（§49.3）。這裡沒有那種狀態。"""
    for module in (components, scenarios, sensors, catalog):
        assert not [name for name, value in vars(module).items()
                    if isinstance(value, ComponentRegistry)], module.__name__
    first, second = catalog.builtin_registry(), catalog.builtin_registry()
    assert first is not second
    assert first.describe() == second.describe()


# ---------------------------------------------------------------------------
# 2 探索：決定性，而且不匯入任何東西
# ---------------------------------------------------------------------------


def test_valid_manifests_register_alongside_the_thesis():
    discovery = catalog.discover([FIXTURES])
    assert discovery.rejections == ()
    registry = discovery.registry
    assert registry.get(KIND_SENSOR, "acme.ir-camera").family == "infrared_camera"
    assert registry.get(KIND_SENSOR, "acme.tof-vl53l1x").capabilities["ingest"]
    sand = registry.get(KIND_SCENARIO, "acme.sand")
    assert sand.simulation_support == scenarios.SUPPORT_REQUIRED
    assert len(registry.components(KIND_SCENARIO)) == 5
    assert len(registry.components(KIND_SENSOR)) == 4


def test_discovery_does_not_depend_on_directory_or_file_order(tmp_path):
    """同一組檔案，分在不同資料夾、以不同順序建立與列出 —— 結果逐字相同。"""
    reference = _portable(catalog.discover([FIXTURES]).registry.describe())

    layouts = [
        ([["acme_sand.json"], ["acme_tof_vl53l1x.json", "acme_ir_camera.json"]], False),
        ([["acme_tof_vl53l1x.json", "acme_ir_camera.json"], ["acme_sand.json"]], True),
        ([list(reversed(FIXTURE_FILES))], False),
    ]
    for index, (groups, reverse_dirs) in enumerate(layouts):
        directories = []
        for group_index, group in enumerate(groups):
            directory = tmp_path / f"layout{index}" / f"dir{group_index}"
            directory.mkdir(parents=True)
            for name in group:  # 建立順序刻意不同
                shutil.copyfile(FIXTURES / name, directory / name)
            directories.append(directory)
        if reverse_dirs:
            directories.reverse()
        discovery = catalog.discover(directories)
        assert discovery.rejections == ()
        assert _portable(discovery.registry.describe()) == reference


def test_discovery_imports_no_plugin_code():
    """manifest 指向的模組在探索時不得被匯入 —— 程式碼存在不等於已註冊。"""
    before = set(sys.modules)
    discovery = catalog.discover([FIXTURES])
    assert not {m for m in set(sys.modules) - before if m.startswith("acme")}

    ir = discovery.registry.get(KIND_SENSOR, "acme.ir-camera")
    problems = catalog.verify_implementations(ir)
    assert problems and "ModuleNotFoundError" in problems[0], (
        "registered is not the same as implemented: the fixture's ingest code "
        "deliberately does not exist, and only an explicit check reveals it"
    )


def test_no_directory_means_only_the_thesis():
    discovery = catalog.discover()
    assert discovery.rejections == ()
    assert discovery.registry.describe() == catalog.builtin_registry().describe()


def test_missing_directories_and_stray_files_are_reported_not_ignored(tmp_path):
    directory = tmp_path / "plugins"
    shutil.copytree(FIXTURES, directory)
    _write(directory, "README.md", "notes")
    _write(directory, "acme_other.yaml", "id: acme.other")
    _write(directory, ".hidden.json", "{")
    discovery = catalog.discover([directory, tmp_path / "does-not-exist"])
    sources = {Path(r.source).name: r.reasons for r in discovery.rejections}
    assert set(sources) == {"README.md", "acme_other.yaml", "does-not-exist"}
    assert "does not exist" in sources["does-not-exist"][0]
    assert "only .json manifests are read" in sources["acme_other.yaml"][0]
    assert discovery.registry.get(KIND_SENSOR, "acme.ir-camera")


def test_listing_one_directory_twice_is_not_a_conflict():
    discovery = catalog.discover([FIXTURES, FIXTURES / "." ])
    assert discovery.rejections == ()


def test_strict_discovery_fails_on_any_rejection(tmp_path):
    _write(tmp_path / "plugins", "broken.json", "{")
    discovery = catalog.discover([tmp_path / "plugins"])
    with pytest.raises(ContractViolation, match="broken.json"):
        discovery.raise_for_rejections()


# ---------------------------------------------------------------------------
# 3 重複與衝突：一律拒絕，不由先後決定
# ---------------------------------------------------------------------------


def test_the_registry_refuses_a_duplicate_identity():
    registry = ComponentRegistry()
    tof = sensors.thesis_sensors()[1]
    registry.register(tof)
    with pytest.raises(DuplicateComponent):
        registry.register(sensors.thesis_sensors()[1])


def test_two_manifests_with_one_identity_are_both_refused(tmp_path):
    first = _fixture("acme_ir_camera.json")
    second = copy.deepcopy(first)
    second["device_model"] = "a different camera"
    _write(tmp_path / "a", "ir.json", first)
    _write(tmp_path / "b", "ir.json", second)

    discovery = catalog.discover([tmp_path / "a", tmp_path / "b"])
    assert len(discovery.rejections) == 2
    assert all("conflicting identities are all refused" in r.reasons[0]
               for r in discovery.rejections)
    with pytest.raises(UnknownComponent):
        discovery.registry.get(KIND_SENSOR, "acme.ir-camera")


def test_a_manifest_cannot_use_the_reserved_thesis_namespace(tmp_path):
    impostor = _fixture("acme_tof_vl53l1x.json")
    impostor["id"], impostor["version"] = "pcmef.sensor.tof-vl53l0x", "2.0.0"
    _write(tmp_path / "p", "impostor.json", impostor)

    discovery = catalog.discover([tmp_path / "p"])
    (rejection,) = discovery.rejections
    assert "reserved namespace" in " ".join(rejection.reasons)
    assert discovery.registry.versions(KIND_SENSOR, "pcmef.sensor.tof-vl53l0x") == ("1.0.0",)


def test_a_plugin_cannot_reuse_a_thesis_class_label(tmp_path):
    empty_again = _fixture("acme_sand.json")
    empty_again["id"] = "acme.empty-again"
    empty_again["label_role"]["classification_label"] = "Empty"
    _write(tmp_path / "p", "empty.json", empty_again)

    (rejection,) = catalog.discover([tmp_path / "p"]).rejections
    assert "frozen thesis class" in " ".join(rejection.reasons)


def test_label_collisions_between_plugins_are_all_refused(tmp_path):
    other = _fixture("acme_sand.json")
    other["id"] = "acme.sand-dry"
    _write(tmp_path / "p", "sand.json", _fixture("acme_sand.json"))
    _write(tmp_path / "p", "sand_dry.json", other)

    discovery = catalog.discover([tmp_path / "p"])
    assert len(discovery.rejections) == 2
    assert all("label collisions are all refused" in r.reasons[0]
               for r in discovery.rejections)


def test_new_versions_coexist_and_must_be_named_explicitly(tmp_path):
    newer = _fixture("acme_ir_camera.json")
    newer["version"] = "1.1.0"
    _write(tmp_path / "p", "ir_1_0.json", _fixture("acme_ir_camera.json"))
    _write(tmp_path / "p", "ir_1_1.json", newer)

    registry = catalog.discover([tmp_path / "p"]).registry
    assert registry.versions(KIND_SENSOR, "acme.ir-camera") == ("1.0.0", "1.1.0")
    with pytest.raises(AmbiguousVersion):
        registry.get(KIND_SENSOR, "acme.ir-camera")
    assert registry.get(KIND_SENSOR, "acme.ir-camera", "1.1.0").identity.version == "1.1.0"


def test_a_frozen_registry_refuses_registration():
    registry = catalog.builtin_registry()
    with pytest.raises(RegistryFrozen):
        registry.register(sensors.thesis_sensors()[0])


# ---------------------------------------------------------------------------
# 4 不合契約：fail closed，而且說出原因
# ---------------------------------------------------------------------------


def _mutate(base: str, change):
    data = _fixture(base)
    change(data)
    return data


def _drop(key):
    return lambda d: d.pop(key)


def _set(path, value):
    def apply(data):
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return apply


SENSOR = "acme_ir_camera.json"
TOF = "acme_tof_vl53l1x.json"
SCENARIO = "acme_sand.json"

MALFORMED = {
    "missing_contract": (SENSOR, _drop("contract"), "known contracts are"),
    "unknown_contract": (SENSOR, _set(["contract"], "pcmef.sensor_adapter/v9"),
                         "known contracts are"),
    "misspelt_field": (SENSOR, lambda d: d.update(capabilites=d["capabilities"]),
                       "unknown field(s) ['capabilites']"),
    "missing_version": (SENSOR, _drop("version"), "missing 'version'"),
    "short_version": (SENSOR, _set(["version"], "1.0"), "MAJOR.MINOR.PATCH"),
    "bad_id": (SENSOR, _set(["id"], "Acme IR"), "lowercase and namespaced"),
    "missing_author": (SENSOR, _drop("author"), "missing 'author'"),
    "unknown_family": (SENSOR, _set(["family"], "lidar"), "family 'lidar'"),
    "claim_without_code": (SENSOR, _set(["implementations", "ingest"], None),
                           "names no implementation"),
    "code_without_claim": (SENSOR, _set(["implementations", "simulate"], "acme.x:y"),
                           "code does not imply a capability"),
    "no_capability": (SENSOR, lambda d: (d["capabilities"].update(ingest=False),
                                         d["implementations"].update(ingest=None)),
                      "simulate or ingest"),
    "bad_entrypoint": (SENSOR, _set(["implementations", "ingest"], "acme.reader.read"),
                       "package.module:attribute"),
    "channel_without_unit": (SENSOR, lambda d: d["observation"]["channels"][0].pop("unit"),
                             "missing 'unit'"),
    "numeric_parameter_without_unit": (
        TOF, lambda d: d["parameters"]["fov_deg"].pop("unit"),
        "must state its unit explicitly"),
    "shape_contradicts_channels": (SENSOR, _set(["observation", "shape"], [None, None, 3]),
                                   "contradicts itself"),
    "quality_signal_is_a_prediction": (
        SENSOR, _set(["quality_signals"], [{"name": "probability", "unit": "1"}]),
        "separate from predictions"),
    "quality_signal_is_a_channel": (
        SENSOR, _set(["quality_signals"], [{"name": "intensity", "unit": "1"}]),
        "also an observation channel"),
    "timing_unexplained": (SENSOR, _set(["observation", "timing_note"], ""),
                           "timing_note must say why"),
    "negative_interval": (TOF, _set(["observation", "sampling_interval_s"], -1),
                          "must be positive"),
    "image_shape_missing_a_dimension": (SENSOR, _set(["observation", "shape"], [None, 1]),
                                        "'image' needs a 3-dimensional shape"),
    "sequence_shape_with_an_extra_dimension": (
        TOF, _set(["observation", "shape"], [None, None, 4]),
        "'sequence' needs a 2-dimensional shape"),
    "vector_layout_with_a_sequence_shape": (
        TOF, _set(["observation", "layout"], "vector"),
        "'vector' needs a 1-dimensional shape"),
    "free_channel_dimension": (TOF, _set(["observation", "shape"], [None, None]),
                               "leaves the channel dimension free"),
    "bool_as_number": (TOF, _set(["parameters", "fov_deg", "minimum"], True),
                       "is a bool"),
    "duplicate_channels": (TOF, lambda d: d["observation"]["channels"][1].update(
        name="distance_mm"), "repeat a name"),
    "unknown_template": (SCENARIO, _set(["physics", "template_id"], "pcmef.medium.sand"),
                         "not a known template"),
    "template_and_code": (SCENARIO, lambda d: d["physics"].update(
        template_id="pcmef.medium.water", implementation="acme.sand:simulate"),
        "both a template and an implementation"),
    "two_roles": (SCENARIO, _set(["label_role", "environment_state"], True),
                  "exactly one of"),
    "no_role": (SCENARIO, _set(["label_role", "classification_label"], None),
                "exactly one of"),
    "unknown_category": (SCENARIO, _set(["category"], "sandy"), "category 'sandy'"),
    "unknown_declared_family": (SCENARIO, _set(["declared_sensor_families"], ["sonar"]),
                                "declared_sensor_families ['sonar']"),
    "missing_limitations": (SCENARIO, _drop("known_limitations"),
                            "missing 'known_limitations'"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_a_malformed_manifest_is_refused_with_its_reason(tmp_path, name):
    base, change, reason = MALFORMED[name]
    data = _mutate(base, change)
    _write(tmp_path / "p", f"{name}.json", data)

    discovery = catalog.discover([tmp_path / "p"])
    (rejection,) = discovery.rejections
    assert reason in " ".join(rejection.reasons), rejection.reasons
    assert len(discovery.registry.components(KIND_SENSOR)) == 2
    assert len(discovery.registry.components(KIND_SCENARIO)) == 4


SYNTAX = {
    "not_json": ("{", "not valid JSON"),
    "array": ("[]", "not a manifest object"),
    "duplicate_key": ('{"contract": "a", "contract": "b"}', "repeats key(s) ['contract']"),
    "nan": ('{"contract": NaN}', "not a JSON number"),
}


@pytest.mark.parametrize("name", sorted(SYNTAX))
def test_unreadable_manifests_are_refused(tmp_path, name):
    text, reason = SYNTAX[name]
    _write(tmp_path / "p", f"{name}.json", text)
    (rejection,) = catalog.discover([tmp_path / "p"]).rejections
    assert reason in " ".join(rejection.reasons)


def test_a_manifest_that_is_not_utf8_is_refused(tmp_path):
    directory = tmp_path / "p"
    directory.mkdir()
    (directory / "latin1.json").write_bytes(b'{"id": "acme.caf\xe9"}')
    (rejection,) = catalog.discover([directory]).rejections
    assert "not UTF-8" in rejection.reasons[0]


#: JSON 沒有 inf，但 `1e999` 是合法的數字字面值，Python 讀成 inf —— 它繞過
#: NaN / Infinity 常數的拒絕，所以要在契約裡另外擋。
OVERFLOW = {
    "interval": (TOF, '"sampling_interval_s": 0.0333', '"sampling_interval_s": 1e999',
                 "observation.sampling_interval_s must be a finite number"),
    "maximum": (TOF, '"maximum": 27', '"maximum": 1e999',
                "parameters.fov_deg.maximum must be a finite number"),
    "minimum": (SCENARIO, '"minimum": 0.05', '"minimum": -1e999',
                "physics.parameters.particle_size_mm.minimum must be a finite number"),
}


@pytest.mark.parametrize("name", sorted(OVERFLOW))
def test_an_overflowing_json_number_is_refused(tmp_path, name):
    base, before, after, reason = OVERFLOW[name]
    text = json.dumps(_fixture(base), ensure_ascii=False)
    assert text.count(before) == 1, "the fixture no longer contains the value to replace"
    _write(tmp_path / "p", f"{name}.json", text.replace(before, after))

    (rejection,) = catalog.discover([tmp_path / "p"]).rejections
    assert reason in " ".join(rejection.reasons), rejection.reasons


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_values_are_refused_even_without_json(value):
    """直接呼叫 parse_*：nan 比較永遠是 False，舊的「<= 0」與「min > max」都擋不住。"""
    tof = _fixture(TOF)
    tof["observation"]["sampling_interval_s"] = value
    with pytest.raises(ContractViolation, match="sampling_interval_s must be a finite"):
        sensors.parse_sensor(tof, source="manifest", location="direct")

    tof = _fixture(TOF)
    tof["parameters"]["fov_deg"]["minimum"] = value
    with pytest.raises(ContractViolation, match="fov_deg.minimum must be a finite"):
        sensors.parse_sensor(tof, source="manifest", location="direct")

    sand = _fixture(SCENARIO)
    sand["physics"]["parameters"]["fill_level"]["maximum"] = value
    with pytest.raises(ContractViolation, match="fill_level.maximum must be a finite"):
        scenarios.parse_scenario(sand, source="manifest", location="direct")


def test_every_shipped_observation_states_its_channel_count():
    """收緊之後，碩論的兩個 adapter 與 fixtures 仍然合契約，而且維度一致。"""
    registry = catalog.discover([FIXTURES]).registry
    ranks = {"image": 3, "sequence": 2, "vector": 1}
    for sensor in registry.components(KIND_SENSOR):
        observation = sensor.observation
        assert len(observation.shape) == ranks[observation.layout], sensor.identity
        assert observation.shape[-1] == len(observation.channels), sensor.identity


def test_unknown_lookups_never_fall_back_to_a_default():
    registry = catalog.discover([FIXTURES]).registry
    with pytest.raises(UnknownComponent):
        registry.get(KIND_SENSOR, "acme.not-installed")
    with pytest.raises(UnknownComponent):
        registry.get(KIND_SENSOR, "pcmef.sensor.tof-vl53l0x", "9.9.9")
    with pytest.raises(UnknownComponent):
        registry.get("model", "pcmef.sensor.tof-vl53l0x")


# ---------------------------------------------------------------------------
# 5 給下一片的 metadata，與「存在 ≠ 相容」
# ---------------------------------------------------------------------------


def test_describe_carries_what_a_compatibility_matrix_needs():
    described = catalog.discover([FIXTURES]).describe()
    json.dumps(described)  # 結構化、可序列化
    registry = described["registry"]
    for scenario in registry[KIND_SCENARIO]:
        assert {"label_role", "category", "physics", "declared_sensor_families",
                "provenance", "compatibility"} <= set(scenario)
        assert scenario["physics"]["support"] in (
            scenarios.SUPPORT_TEMPLATE, scenarios.SUPPORT_PLUGIN,
            scenarios.SUPPORT_REQUIRED)
    for sensor in registry[KIND_SENSOR]:
        assert {"family", "capabilities", "observation", "quality_signals",
                "provenance", "compatibility"} <= set(sensor)
        assert all(c["unit"] for c in sensor["observation"]["channels"])
    for item in registry[KIND_SCENARIO] + registry[KIND_SENSOR]:
        assert item["compatibility"]["assessed"] is False
        assert len(item["provenance"]["sha256"]) == 64


def test_existence_is_not_compatibility():
    """註冊一個 IR adapter 不會讓任何情境「支援 IR」；宣告也不等於能模擬。"""
    thesis_only = catalog.builtin_registry()
    with_plugins = catalog.discover([FIXTURES]).registry
    for scenario in thesis_only.components(KIND_SCENARIO):
        again = with_plugins.get(KIND_SCENARIO, scenario.identity.component_id)
        assert again.declared_sensor_families == ("rgb_camera", "time_of_flight")
    sand = with_plugins.get(KIND_SCENARIO, "acme.sand")
    assert "time_of_flight" in sand.declared_sensor_families
    assert sand.simulation_support == scenarios.SUPPORT_REQUIRED


def test_the_same_family_is_not_equivalence():
    """UAT-03：另一顆 ToF 的通道、形狀、時間軸都要看得出差異。"""
    registry = catalog.discover([FIXTURES]).registry
    thesis = registry.get(KIND_SENSOR, "pcmef.sensor.tof-vl53l0x")
    variant = registry.get(KIND_SENSOR, "acme.tof-vl53l1x")
    assert thesis.family == variant.family
    differences = sensors.compare_observations(thesis, variant)
    joined = " | ".join(differences)
    for expected in ("observation schema", "shape", "channels", "sampling interval"):
        assert expected in joined, differences
    assert sensors.compare_observations(thesis, thesis) == ()
