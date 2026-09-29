# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 pcmef.platform.compatibility，registry 一律
#         由 pcmef.platform.catalog.discover() 組出。manifest 範例在
#         tests/platform/fixtures/extensions，變體一律寫在 tmp_path。
#         不寫入任何 repo 路徑。
# 檔案路徑: tests/platform/test_compatibility_matrix.py
# 產生時間: 2026-09-27 02:34 +08:00（首次提交 fd75d28 的 commit 時間）
# 版本: v0.1.1
# 功能說明: SAI Phase 3 第二片的驗收：Sensor × Scenario Compatibility Matrix
#           的六種狀態、每一格的理由與證據、以及工作單列出的每一條邊界 ——
#           註冊 ≠ 實作存在、實作存在 ≠ 科學相容、同家族 ≠ 可互換、宣告 ≠
#           判決、plugin_required 不因感測器相容而可模擬、身分不明一律失敗。
# 模組定位: Phase 3 第二片的驗收測試。
# 主要責任:
#   1. 碩論 Scenario × RGB / ToF 維持現有的支援語意（迴歸錨點）
#   2. READY 只來自「明確的實作驗證 + 確切版本的驗證紀錄」
#   3. fixtures 的實作刻意不存在：宣告了也到不了 READY，一律 BLOCKED
#   4. alt-ToF：家族相同不夠，差異看得見，狀態是待驗證而不是等價
#   5. Sand 在 plugin_required 時不能模擬；身分不明或不明確一律拋出
#   6. 驗證紀錄屬於被驗證的那一份定義：同 id、同版本而內容改了，舊紀錄
#      一律拒絕，說出哪一個元件漂移，不可能因此到 READY
# 維護提醒:
#   - **不得把狀態斷言改成只檢查「不是 READY」。** 每一格都斷言確切狀態與
#     造成它的理由代碼 —— 否則一格因為別的理由而降級，測試照樣通過，而它
#     要守的那條邊界其實沒人在守。
#   - 不得在這裡手寫 ImplementationReport；它只能由 verify() 或
#     not_performed() 產生（手寫本身就是一條測試）。
#   - 不得把漂移測試的「定義 A 到得了 READY」那一步拿掉：少了它，紀錄被拒
#     也可能是別的原因，而漂移那條規則其實沒人在守。
#   - v0.1.1 新增：過期證據（感測器與情境定義同版本漂移、碩論錨點漂移、
#     格式不同但定義相同）與 UNSUPPORTED 高於 BLOCKED 的對抗測試。
#     對應 P3-2 獨立審查。
#   - v0.1.0 新增：首版，對應 SAI Phase 3 第二片。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_compatibility_matrix.py -v
# ------------------------------------------------------------

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from pcmef.core import constants
from pcmef.platform import catalog, compatibility, scenarios, sensors
from pcmef.platform.compatibility import (
    BLOCKED,
    NEEDS_VALIDATION,
    PLUGIN_REQUIRED,
    PURPOSE_MEASUREMENT,
    PURPOSE_SIMULATION,
    READY,
    STATUSES,
    TEMPLATE_SUPPORTED,
    UNSUPPORTED,
    ImplementationReport,
    ValidationRecord,
    build_matrix,
    evaluate_pair,
    thesis_validations,
)
from pcmef.platform.components import (
    KIND_SCENARIO,
    KIND_SENSOR,
    AmbiguousVersion,
    ComponentRegistry,
    ContractViolation,
    UnknownComponent,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "extensions"

THESIS_SCENARIOS = tuple(f"pcmef.scenario.{label.lower()}"
                         for label in constants.CLASS_ORDER)
RGB = "pcmef.sensor.rgb-camera"
TOF = "pcmef.sensor.tof-vl53l0x"
ALT_TOF = "acme.tof-vl53l1x"
IR = "acme.ir-camera"
SAND = "acme.sand"

#: 解析得到而且可呼叫的實作指向，給「程式碼存在」的變體用。
RESOLVABLE = "json:loads"

#: 由寬到嚴。寫在這裡而不是讀模組的常數：嚴格順序是這一片的決策，
#: 測試要自己說出來。
PRECEDENCE = (READY, TEMPLATE_SUPPORTED, NEEDS_VALIDATION, PLUGIN_REQUIRED,
              BLOCKED, UNSUPPORTED)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _variant(directory: Path, base: str, change=None, *, name: str | None = None) -> Path:
    """把一個 fixture 改過後寫進 `directory`。"""
    data = _fixture(base)
    if change is not None:
        change(data)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or base)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return directory


def _registry(*directories: Path) -> ComponentRegistry:
    discovery = catalog.discover(list(directories))
    assert discovery.rejections == (), [r.describe() for r in discovery.rejections]
    return discovery.registry


def _record(registry: ComponentRegistry, purpose: str, scenario: str, sensor: str,
            *, scenario_version: str | None = None,
            sensor_version: str | None = None) -> ValidationRecord:
    """為 registry 裡**現在這一份**情境定義與感測器定義寫一筆驗證。"""
    return ValidationRecord.of(
        purpose, registry.get(KIND_SCENARIO, scenario, scenario_version),
        registry.get(KIND_SENSOR, sensor, sensor_version),
        evidence="test fixture validation", source="test")


def _matrix(registry: ComponentRegistry, purpose: str, *, verify: bool = True,
            validations=None):
    report = (ImplementationReport.verify(registry) if verify
              else ImplementationReport.not_performed())
    return build_matrix(
        registry, purpose=purpose, implementations=report,
        validations=thesis_validations() if validations is None else validations)


def _ingest(reference):
    return lambda d: d["implementations"].update(ingest=reference)


# ---------------------------------------------------------------------------
# 1 六種狀態與碩論的迴歸錨點
# ---------------------------------------------------------------------------


def test_the_six_statuses_are_those_of_sai_section_10():
    assert STATUSES == ("READY", "TEMPLATE_SUPPORTED", "NEEDS_VALIDATION",
                        "PLUGIN_REQUIRED", "UNSUPPORTED", "BLOCKED")


def test_the_thesis_anchors_are_exact_identities_derived_from_class_order():
    records = thesis_validations()
    assert len(records) == 12
    registry = catalog.builtin_registry()
    scenario_ids = {s.identity.component_id for s in registry.components(KIND_SCENARIO)}
    assert {r.scenario[0] for r in records} == scenario_ids == set(THESIS_SCENARIOS)
    assert {(r.purpose, r.sensor) for r in records} == {
        (PURPOSE_SIMULATION, (RGB, "1.0.0")),
        (PURPOSE_SIMULATION, (TOF, "1.0.0")),
        (PURPOSE_MEASUREMENT, (TOF, "1.0.0")),
    }
    assert all(r.scenario[1] == "1.0.0" for r in records)
    # 錨點寫死的是**現在這一份**內建定義：與每次重新建出的內建元件逐一相同。
    for record in records:
        scenario = registry.get(KIND_SCENARIO, *record.scenario)
        sensor = registry.get(KIND_SENSOR, *record.sensor)
        assert record == ValidationRecord.of(record.purpose, scenario, sensor,
                                             evidence=record.evidence, source="thesis")
    # 每一筆都參照得到、定義一致 —— 否則 build_matrix 會拋出。
    _matrix(registry, PURPOSE_SIMULATION)
    _matrix(registry, PURPOSE_MEASUREMENT)


@pytest.mark.parametrize("scenario", THESIS_SCENARIOS)
@pytest.mark.parametrize("sensor", [RGB, TOF])
def test_thesis_simulation_pairings_stay_ready(scenario, sensor):
    matrix = _matrix(_registry(FIXTURES), PURPOSE_SIMULATION)
    cell = matrix.cell(scenario, sensor)
    assert cell.status == READY, cell.describe()
    assert {"sensor_implementation_verified", "scenario_template_backed",
            "family_declared_by_scenario", "validated_pairing"} == set(cell.codes())
    assert cell.reason("validated_pairing").evidence[0] == ("source", "thesis")
    assert (cell.scenario.version, cell.sensor.version) == ("1.0.0", "1.0.0")


@pytest.mark.parametrize("scenario", THESIS_SCENARIOS)
def test_thesis_measurement_is_ready_for_tof_and_unsupported_for_rgb(scenario):
    matrix = _matrix(_registry(FIXTURES), PURPOSE_MEASUREMENT)
    tof = matrix.cell(scenario, TOF)
    assert tof.status == READY, tof.describe()
    assert tof.reason("sensor_implementation_verified").evidence == (
        ("reference", "pcmef.adapters.edge_impulse:EdgeImpulseAdapter"),)
    rgb = matrix.cell(scenario, RGB)
    assert rgb.status == UNSUPPORTED
    assert rgb.reason("sensor_cannot_ingest").limit == UNSUPPORTED


# ---------------------------------------------------------------------------
# 2 READY 只有一條路
# ---------------------------------------------------------------------------


def test_without_implementation_verification_nothing_is_ready():
    registry = _registry(FIXTURES)
    for purpose in (PURPOSE_SIMULATION, PURPOSE_MEASUREMENT):
        matrix = _matrix(registry, purpose, verify=False)
        assert matrix.verification_performed is False
        assert READY not in {c.status for c in matrix.cells}
    cell = _matrix(registry, PURPOSE_SIMULATION, verify=False).cell(THESIS_SCENARIOS[0], TOF)
    assert cell.status == NEEDS_VALIDATION
    assert "sensor_implementation_not_verified" in cell.codes()
    assert "validated_pairing" in cell.codes(), "the record alone does not make it READY"


def test_without_a_validation_record_nothing_is_ready():
    registry = _registry(FIXTURES)
    simulation = _matrix(registry, PURPOSE_SIMULATION, validations=())
    measurement = _matrix(registry, PURPOSE_MEASUREMENT, validations=())
    assert READY not in {c.status for c in simulation.cells + measurement.cells}
    for scenario in THESIS_SCENARIOS:
        for sensor in (RGB, TOF):
            cell = simulation.cell(scenario, sensor)
            assert cell.status == TEMPLATE_SUPPORTED
            assert "pairing_not_validated_template" in cell.codes()
        cell = measurement.cell(scenario, TOF)
        assert cell.status == NEEDS_VALIDATION
        assert "pairing_not_validated" in cell.codes()


@pytest.mark.parametrize("verify", [True, False])
@pytest.mark.parametrize("validations", [None, ()])
def test_every_ready_cell_has_verified_code_and_an_exact_record(verify, validations):
    registry = _registry(FIXTURES)
    for purpose in (PURPOSE_SIMULATION, PURPOSE_MEASUREMENT):
        for cell in _matrix(registry, purpose, verify=verify,
                            validations=validations).cells:
            if cell.status == READY:
                assert "validated_pairing" in cell.codes()
                assert "sensor_implementation_verified" in cell.codes()
            # 狀態就是最嚴格的那條理由，不多也不少。
            worst = max(cell.reasons, key=lambda r: PRECEDENCE.index(r.limit))
            assert cell.status == worst.limit, cell.describe()


def test_a_hand_written_report_is_refused():
    with pytest.raises(TypeError, match="comes only from"):
        ImplementationReport(True, {})
    registry = _registry(FIXTURES)
    with pytest.raises(ContractViolation, match="must be an ImplementationReport"):
        build_matrix(registry, purpose=PURPOSE_SIMULATION, implementations=None,
                     validations=thesis_validations())


def test_the_matrix_imports_no_code_unless_asked(monkeypatch):
    def refuse(reference):
        raise AssertionError(f"imported {reference} without being asked")

    monkeypatch.setattr(compatibility, "verify_reference", refuse)
    registry = _registry(FIXTURES)
    report = ImplementationReport.not_performed()
    for purpose in (PURPOSE_SIMULATION, PURPOSE_MEASUREMENT):
        build_matrix(registry, purpose=purpose, implementations=report,
                     validations=thesis_validations())
    with pytest.raises(AssertionError, match="without being asked"):
        ImplementationReport.verify(registry)


def test_a_stale_report_does_not_verify_a_changed_reference(tmp_path):
    resolvable = _registry(_variant(tmp_path / "a", "acme_tof_vl53l1x.json",
                                    _ingest(RESOLVABLE)))
    stale = ImplementationReport.verify(resolvable)
    shipped = _registry(FIXTURES)          # 同一個身分，指向不存在的程式碼
    cell = evaluate_pair(shipped, THESIS_SCENARIOS[0], ALT_TOF,
                         purpose=PURPOSE_MEASUREMENT, implementations=stale,
                         validations=thesis_validations())
    reason = cell.reason("sensor_implementation_not_verified")
    assert RESOLVABLE in reason.detail
    assert cell.status == NEEDS_VALIDATION


# ---------------------------------------------------------------------------
# 3 宣告了但程式碼不存在：BLOCKED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sensor", [IR, ALT_TOF])
def test_fixture_adapters_with_missing_code_are_blocked(sensor):
    matrix = _matrix(_registry(FIXTURES), PURPOSE_MEASUREMENT)
    for scenario in THESIS_SCENARIOS + (SAND,):
        cell = matrix.cell(scenario, sensor)
        assert cell.status == BLOCKED, cell.describe()
        evidence = dict(cell.reason("sensor_implementation_unverifiable").evidence)
        assert "ModuleNotFoundError" in evidence["problem"]
        # 被擋下的理由之外，其他理由照樣記下來。
        expected = ("family_not_declared_by_scenario" if sensor == IR
                    else "family_declared_by_scenario")
        assert expected in cell.codes()


def test_a_validation_record_cannot_rescue_missing_code():
    registry = _registry(FIXTURES)
    records = thesis_validations() + (
        _record(registry, PURPOSE_MEASUREMENT, THESIS_SCENARIOS[0], ALT_TOF),)
    cell = _matrix(registry, PURPOSE_MEASUREMENT, validations=records).cell(
        THESIS_SCENARIOS[0], ALT_TOF)
    assert "validated_pairing" in cell.codes()
    assert cell.status == BLOCKED


@pytest.mark.parametrize("sensor", [IR, ALT_TOF])
def test_ingest_only_adapters_cannot_simulate(sensor):
    matrix = _matrix(_registry(FIXTURES), PURPOSE_SIMULATION)
    for scenario in THESIS_SCENARIOS + (SAND,):
        cell = matrix.cell(scenario, sensor)
        assert cell.status == UNSUPPORTED, cell.describe()
        assert "sensor_cannot_simulate" in cell.codes()


# ---------------------------------------------------------------------------
# 4 同一家族不等於等價；宣告不等於判決
# ---------------------------------------------------------------------------


def test_alt_tof_shows_its_differences_even_while_blocked():
    cell = _matrix(_registry(FIXTURES), PURPOSE_MEASUREMENT).cell(
        THESIS_SCENARIOS[0], ALT_TOF)
    evidence = dict(cell.reason("observation_differs_from_validated_reference").evidence)
    assert evidence["reference"] == (TOF, "1.0.0")
    joined = " | ".join(evidence["differences"])
    for expected in ("observation schema", "shape", "channels", "sampling interval"):
        assert expected in joined, evidence["differences"]


@pytest.mark.parametrize("scenario", THESIS_SCENARIOS)
def test_the_tof_family_alone_is_not_enough(tmp_path, scenario):
    """alt-ToF 的程式碼存在、家族也被情境宣告，仍然只是待驗證。"""
    registry = _registry(_variant(tmp_path / "p", "acme_tof_vl53l1x.json",
                                  _ingest(RESOLVABLE)))
    cell = _matrix(registry, PURPOSE_MEASUREMENT).cell(scenario, ALT_TOF)
    assert cell.status == NEEDS_VALIDATION, cell.describe()
    assert {"sensor_implementation_verified", "family_declared_by_scenario",
            "pairing_not_validated",
            "observation_differs_from_validated_reference"} <= set(cell.codes())
    assert cell.reason("observation_differs_from_validated_reference").limit == (
        NEEDS_VALIDATION)


def test_an_identical_observation_is_still_not_a_validated_pairing(tmp_path):
    thesis_observation = catalog.builtin_registry().get(KIND_SENSOR, TOF).observation

    def clone(data):
        _ingest(RESOLVABLE)(data)
        data["observation"] = thesis_observation.describe()
        data["quality_signals"] = []

    registry = _registry(_variant(tmp_path / "p", "acme_tof_vl53l1x.json", clone))
    cell = _matrix(registry, PURPOSE_MEASUREMENT).cell(THESIS_SCENARIOS[0], ALT_TOF)
    assert "observation_matches_validated_reference" in cell.codes()
    assert "pairing_not_validated" in cell.codes()
    assert cell.status == NEEDS_VALIDATION


def test_an_undeclared_family_needs_validation(tmp_path):
    registry = _registry(_variant(tmp_path / "p", "acme_ir_camera.json",
                                  _ingest(RESOLVABLE)))
    records = thesis_validations() + (
        _record(registry, PURPOSE_MEASUREMENT, THESIS_SCENARIOS[0], IR),)
    matrix = _matrix(registry, PURPOSE_MEASUREMENT, validations=records)
    cell = matrix.cell(THESIS_SCENARIOS[0], IR)
    assert "sensor_implementation_verified" in cell.codes()
    assert "validated_pairing" in cell.codes()
    assert cell.reason("family_not_declared_by_scenario").limit == NEEDS_VALIDATION
    assert cell.status == NEEDS_VALIDATION
    assert matrix.cell(THESIS_SCENARIOS[1], IR).status == NEEDS_VALIDATION


# ---------------------------------------------------------------------------
# 5 情境的 physics：plugin_required 不能模擬
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sensor", [RGB, TOF])
def test_sand_cannot_simulate_while_plugin_required(sensor):
    registry = _registry(FIXTURES)
    # 連驗證紀錄都給了，感測器也能模擬、實作也驗證過、家族也宣告了。
    records = thesis_validations() + (
        _record(registry, PURPOSE_SIMULATION, SAND, sensor),)
    cell = _matrix(registry, PURPOSE_SIMULATION, validations=records).cell(SAND, sensor)
    assert cell.status == PLUGIN_REQUIRED, cell.describe()
    assert {"sensor_implementation_verified", "scenario_physics_missing",
            "family_declared_by_scenario", "validated_pairing"} == set(cell.codes())


def test_sand_with_a_non_simulating_sensor_is_unsupported():
    cell = _matrix(_registry(FIXTURES), PURPOSE_SIMULATION).cell(SAND, IR)
    assert cell.status == UNSUPPORTED
    assert {"sensor_cannot_simulate", "scenario_physics_missing"} <= set(cell.codes())


def test_sand_can_be_measured_but_is_not_validated():
    matrix = _matrix(_registry(FIXTURES), PURPOSE_MEASUREMENT)
    cell = matrix.cell(SAND, TOF)
    assert cell.status == NEEDS_VALIDATION
    assert dict(cell.reason("physics_not_used_for_measurement").evidence) == {
        "physics_support": "plugin_required"}
    assert "pairing_not_validated" in cell.codes()
    assert matrix.cell(SAND, RGB).status == UNSUPPORTED


def _sand_physics(reference):
    return lambda d: d["physics"].update(implementation=reference)


def test_a_plugin_implemented_scenario_needs_verified_code_and_a_record(tmp_path):
    registry = _registry(_variant(tmp_path / "ok", "acme_sand.json",
                                  _sand_physics(RESOLVABLE)))
    unvalidated = _matrix(registry, PURPOSE_SIMULATION).cell(SAND, TOF)
    assert unvalidated.status == NEEDS_VALIDATION
    assert {"scenario_implementation_verified", "pairing_not_validated"} <= set(
        unvalidated.codes())

    records = thesis_validations() + (_record(registry, PURPOSE_SIMULATION, SAND, TOF),)
    assert _matrix(registry, PURPOSE_SIMULATION, validations=records).cell(
        SAND, TOF).status == READY
    assert _matrix(registry, PURPOSE_SIMULATION, verify=False, validations=records).cell(
        SAND, TOF).status == NEEDS_VALIDATION

    broken = _registry(_variant(tmp_path / "broken", "acme_sand.json",
                                _sand_physics("acme_sand.physics:simulate")))
    records = thesis_validations() + (_record(broken, PURPOSE_SIMULATION, SAND, TOF),)
    cell = _matrix(broken, PURPOSE_SIMULATION, validations=records).cell(SAND, TOF)
    assert cell.status == BLOCKED
    assert "scenario_implementation_unverifiable" in cell.codes()


@pytest.mark.parametrize("sensor", [IR, ALT_TOF])
def test_a_missing_capability_outranks_a_broken_scenario(tmp_path, sensor):
    """感測器不能 simulate + 情境的 physics 實作壞了：這條路本來就不存在。

    修好情境的實作也不會讓一個不能模擬的感測器能模擬，所以最上層是
    UNSUPPORTED；壞掉的實作仍然記成理由，沒有被吞掉。
    """
    directory = tmp_path / "p"
    _variant(directory, "acme_ir_camera.json")
    _variant(directory, "acme_tof_vl53l1x.json")
    registry = _registry(_variant(directory, "acme_sand.json",
                                  _sand_physics("acme_sand.physics:simulate")))
    records = thesis_validations() + (_record(registry, PURPOSE_SIMULATION, SAND, TOF),)
    matrix = _matrix(registry, PURPOSE_SIMULATION, validations=records)
    cell = matrix.cell(SAND, sensor)
    assert cell.reason("sensor_cannot_simulate").limit == UNSUPPORTED
    assert cell.reason("scenario_implementation_unverifiable").limit == BLOCKED
    assert cell.status == UNSUPPORTED, cell.describe()
    # 同一個壞掉的情境，配上能模擬的感測器就是 BLOCKED。
    assert matrix.cell(SAND, TOF).status == BLOCKED


def test_a_broken_sensor_outranks_missing_physics(tmp_path):
    """能模擬但程式碼不存在的感測器 × plugin_required 的情境：先修壞掉的。"""
    def simulating(data):
        data["capabilities"]["simulate"] = True
        data["implementations"]["simulate"] = "acme_tof.simulator:simulate"

    directory = tmp_path / "p"
    _variant(directory, "acme_sand.json")
    registry = _registry(_variant(directory, "acme_tof_vl53l1x.json", simulating))
    cell = _matrix(registry, PURPOSE_SIMULATION).cell(SAND, ALT_TOF)
    assert cell.reason("sensor_implementation_unverifiable").limit == BLOCKED
    assert cell.reason("scenario_physics_missing").limit == PLUGIN_REQUIRED
    assert cell.status == BLOCKED, cell.describe()


# ---------------------------------------------------------------------------
# 6 身分：查不到、不明確、版本不同，一律明說
# ---------------------------------------------------------------------------


def _two_versions(tmp_path: Path) -> ComponentRegistry:
    directory = tmp_path / "p"
    _variant(directory, "acme_tof_vl53l1x.json", _ingest(RESOLVABLE), name="a.json")
    _variant(directory, "acme_tof_vl53l1x.json",
             lambda d: (_ingest(RESOLVABLE)(d), d.update(version="1.1.0")),
             name="b.json")
    return _registry(directory)


def test_unknown_identities_never_fall_back_to_the_thesis():
    registry = _registry(FIXTURES)
    kwargs = dict(purpose=PURPOSE_MEASUREMENT,
                  implementations=ImplementationReport.not_performed(),
                  validations=thesis_validations())
    for scenario, sensor in [("acme.not-installed", TOF),
                             (THESIS_SCENARIOS[0], "pcmef.sensor.tof-vl53l0"),
                             (THESIS_SCENARIOS[0], "acme.not-installed")]:
        with pytest.raises(UnknownComponent):
            evaluate_pair(registry, scenario, sensor, **kwargs)
    with pytest.raises(UnknownComponent):
        evaluate_pair(registry, THESIS_SCENARIOS[0], TOF, sensor_version="9.9.9",
                      **kwargs)
    matrix = _matrix(registry, PURPOSE_MEASUREMENT)
    with pytest.raises(UnknownComponent):
        matrix.cell("acme.not-installed", TOF)
    with pytest.raises(UnknownComponent):
        matrix.cell(THESIS_SCENARIOS[0], TOF, sensor_version="2.0.0")


def test_an_ambiguous_identity_must_name_its_version(tmp_path):
    registry = _two_versions(tmp_path)
    matrix = _matrix(registry, PURPOSE_MEASUREMENT)
    with pytest.raises(AmbiguousVersion):
        matrix.cell(THESIS_SCENARIOS[0], ALT_TOF)
    with pytest.raises(AmbiguousVersion):
        evaluate_pair(registry, THESIS_SCENARIOS[0], ALT_TOF, purpose=PURPOSE_MEASUREMENT,
                      implementations=ImplementationReport.verify(registry),
                      validations=thesis_validations())
    old = matrix.cell(THESIS_SCENARIOS[0], ALT_TOF, sensor_version="1.0.0")
    new = matrix.cell(THESIS_SCENARIOS[0], ALT_TOF, sensor_version="1.1.0")
    assert (old.sensor.version, new.sensor.version) == ("1.0.0", "1.1.0")


def test_a_validation_does_not_carry_over_to_a_new_version(tmp_path):
    registry = _two_versions(tmp_path)
    records = thesis_validations() + (_record(
        registry, PURPOSE_MEASUREMENT, THESIS_SCENARIOS[0], ALT_TOF,
        sensor_version="1.0.0"),)
    matrix = _matrix(registry, PURPOSE_MEASUREMENT, validations=records)
    validated = matrix.cell(THESIS_SCENARIOS[0], ALT_TOF, sensor_version="1.0.0")
    assert validated.status == READY, validated.describe()
    successor = matrix.cell(THESIS_SCENARIOS[0], ALT_TOF, sensor_version="1.1.0")
    assert successor.status == NEEDS_VALIDATION
    assert "validated_pairing" not in successor.codes()


def _bad_records(registry):
    good = _record(registry, PURPOSE_MEASUREMENT, THESIS_SCENARIOS[0], TOF)
    replace = dataclasses.replace
    return {
        "unknown_sensor_version": (replace(good, sensor=(TOF, "9.9.9")),
                                   UnknownComponent, "9.9.9"),
        "unknown_scenario": (replace(good, scenario=("acme.lava", "1.0.0")),
                             UnknownComponent, "acme.lava"),
        "no_version": (replace(good, sensor=(TOF, None)),
                       ContractViolation, "must name an exact version"),
        "wrong_observation": (replace(good, observation=("acme.tof.recording", "1.0.0")),
                              ContractViolation, "but the record claims"),
        "impossible_capability": (
            _record(registry, PURPOSE_MEASUREMENT, THESIS_SCENARIOS[0], RGB),
            ContractViolation, "cannot ingest"),
        "unknown_purpose": (replace(good, purpose="training"),
                            ContractViolation, "unknown purpose"),
        "duplicate": (good, ContractViolation, "duplicate record"),
        "no_definition_hash": (replace(good, sensor_sha256=None),
                               ContractViolation, "sensor_sha256 must be the sha256"),
        "abbreviated_definition_hash": (
            replace(good, scenario_sha256=good.scenario_sha256[:16]),
            ContractViolation, "scenario_sha256 must be the sha256"),
        "stale_scenario_definition": (
            replace(good, scenario_sha256="0" * 64),
            ContractViolation,
            re.escape(f"scenario {THESIS_SCENARIOS[0]} 1.0.0 drifted")),
        "stale_sensor_definition": (
            replace(good, sensor_sha256="0" * 64),
            ContractViolation, re.escape(f"sensor {TOF} 1.0.0 drifted")),
        "observation_fingerprint_contradicts_the_sensor": (
            replace(good, observation_sha256="0" * 64),
            ContractViolation, "is not the observation of the validated"),
    }


@pytest.mark.parametrize("name", ["unknown_sensor_version", "unknown_scenario",
                                  "no_version", "wrong_observation",
                                  "impossible_capability", "unknown_purpose",
                                  "duplicate", "no_definition_hash",
                                  "abbreviated_definition_hash",
                                  "stale_scenario_definition", "stale_sensor_definition",
                                  "observation_fingerprint_contradicts_the_sensor"])
def test_a_bad_validation_record_is_refused_not_skipped(name):
    registry = _registry(FIXTURES)
    record, error, reason = _bad_records(registry)[name]
    with pytest.raises(error, match=reason):
        _matrix(registry, PURPOSE_MEASUREMENT,
                validations=thesis_validations() + (record,))


def test_the_registry_must_be_frozen_and_the_purpose_known():
    registry = ComponentRegistry()
    for component in catalog.builtin_components():
        registry.register(component)
    with pytest.raises(ContractViolation, match="not frozen"):
        build_matrix(registry, purpose=PURPOSE_SIMULATION,
                     implementations=ImplementationReport.not_performed(),
                     validations=())
    with pytest.raises(ContractViolation, match="unknown purpose"):
        _matrix(_registry(FIXTURES), "training")


# ---------------------------------------------------------------------------
# 7 形狀：每一格都在、可解釋、可序列化、決定性
# ---------------------------------------------------------------------------


def test_unserved_declared_families_are_reported_as_plugin_required(tmp_path):
    assert _matrix(_registry(FIXTURES), PURPOSE_SIMULATION).unserved == ()
    registry = _registry(_variant(
        tmp_path / "p", "acme_sand.json",
        lambda d: d.update(declared_sensor_families=["thermal_camera", "time_of_flight"])))
    (unserved,) = _matrix(registry, PURPOSE_SIMULATION).unserved
    assert (unserved.scenario.component_id, unserved.family, unserved.status) == (
        SAND, "thermal_camera", PLUGIN_REQUIRED)


def test_the_matrix_covers_every_pair_and_describes_itself():
    registry = _registry(FIXTURES)
    matrix = _matrix(registry, PURPOSE_MEASUREMENT)
    scenario_ids = [s.identity.component_id for s in registry.components(KIND_SCENARIO)]
    sensor_ids = [s.identity.component_id for s in registry.components(KIND_SENSOR)]
    assert [(c.scenario.component_id, c.sensor.component_id) for c in matrix.cells] == [
        (s, a) for s in scenario_ids for a in sensor_ids]
    assert len(matrix.cells) == 5 * 4
    described = json.loads(json.dumps(matrix.describe()))
    assert described["purpose"] == PURPOSE_MEASUREMENT
    assert described["statuses"] == list(STATUSES)
    for cell in described["cells"]:
        assert cell["status"] in STATUSES
        assert {"id", "version", "author", "kind"} <= set(cell["scenario"])
        assert {"id", "version", "author", "kind"} <= set(cell["sensor"])
        assert cell["reasons"], "every cell explains itself"
        for reason in cell["reasons"]:
            assert set(reason) == {"code", "limit", "detail", "evidence"}
            assert reason["limit"] in STATUSES and reason["detail"]
    table = matrix.table()
    assert table["pcmef.scenario.empty@1.0.0"]["pcmef.sensor.tof-vl53l0x@1.0.0"] == READY
    assert table["acme.sand@0.1.0"]["acme.ir-camera@1.0.0"] == BLOCKED
    json.dumps(ImplementationReport.verify(registry).describe())


def test_the_matrix_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for name in ("acme_ir_camera.json", "acme_sand.json"):
        _variant(first, name)
    _variant(second, "acme_tof_vl53l1x.json")
    a = catalog.discover([first, second]).registry
    b = catalog.discover([second, first]).registry
    for purpose in (PURPOSE_SIMULATION, PURPOSE_MEASUREMENT):
        assert (_matrix(a, purpose).describe()
                == _matrix(b, purpose).describe()
                == _matrix(a, purpose).describe())


def test_building_a_matrix_changes_nothing():
    registry = _registry(FIXTURES)
    before = registry.describe()
    frozen = (constants.CLASS_ORDER, constants.TOF_SCHEMA, constants.TOF_RECORDING_SHAPE)
    for purpose in (PURPOSE_SIMULATION, PURPOSE_MEASUREMENT):
        _matrix(registry, purpose)
        _matrix(registry, purpose, verify=False)
    assert registry.describe() == before
    assert (constants.CLASS_ORDER, constants.TOF_SCHEMA,
            constants.TOF_RECORDING_SHAPE) == frozen
    for item in before[KIND_SCENARIO] + before[KIND_SENSOR]:
        assert item["compatibility"]["assessed"] is False, (
            "the registry never claims compatibility; the matrix does")


# ---------------------------------------------------------------------------
# 8 過期的證據：同一個 id 與版本，定義卻變了
# ---------------------------------------------------------------------------


def _drifted(role: str, component_id: str, version: str) -> str:
    return re.escape(f"{role} {component_id} {version} drifted")


def _swap_first_two_channels(data):
    channels = data["observation"]["channels"]
    channels[0], channels[1] = channels[1], channels[0]


#: 感測器定義的漂移：id、版本、觀測 schema 的 id 與版本全部不變。
#: 值是（改法, 觀測定義是否也跟著變）。
SENSOR_DRIFT = {
    "channel_unit": (lambda d: d["observation"]["channels"][3].update(unit="um"), True),
    "channel_order": (_swap_first_two_channels, True),
    "sampling_interval": (lambda d: d["observation"].update(sampling_interval_s=0.05),
                          True),
    "quality_signal": (lambda d: d["quality_signals"][0].update(
        unit="code", description="vendor status code, remapped"), False),
    "parameter_bounds": (lambda d: d["parameters"]["fov_deg"].update(maximum=25), False),
    "device_model": (lambda d: d.update(device_model="VL53L1CB"), False),
}

#: 情境定義的漂移：id 與版本不變。
SCENARIO_DRIFT = {
    "declared_families": lambda d: d["declared_sensor_families"].append(
        "infrared_camera"),
    "physics_parameter_unit": lambda d: d["physics"]["parameters"]["fill_level"].update(
        unit="%", maximum=100),
    "physics_parameter_bounds": lambda d: d["physics"]["parameters"][
        "particle_size_mm"].update(maximum=5),
    "physics_implementation": lambda d: d["physics"].update(implementation="json:dumps"),
    "category": lambda d: d.update(category="custom"),
}


def _alt_tof_definition(directory: Path, change=None) -> ComponentRegistry:
    def build(data):
        _ingest(RESOLVABLE)(data)
        if change is not None:
            change(data)
    return _registry(_variant(directory, "acme_tof_vl53l1x.json", build))


def _sand_definition(directory: Path, change=None) -> ComponentRegistry:
    def build(data):
        _sand_physics(RESOLVABLE)(data)
        if change is not None:
            change(data)
    return _registry(_variant(directory, "acme_sand.json", build))


@pytest.mark.parametrize("drift", sorted(SENSOR_DRIFT))
def test_a_sensor_that_drifted_under_the_same_version_loses_its_validation(
        tmp_path, drift):
    change, observation_changed = SENSOR_DRIFT[drift]
    scenario = THESIS_SCENARIOS[0]
    # 定義 A：實作找得到、紀錄成立、到得了 READY —— 下面被擋，擋的就是漂移。
    registry_a = _alt_tof_definition(tmp_path / "A")
    records = thesis_validations() + (
        _record(registry_a, PURPOSE_MEASUREMENT, scenario, ALT_TOF),)
    assert _matrix(registry_a, PURPOSE_MEASUREMENT, validations=records).cell(
        scenario, ALT_TOF).status == READY

    # 定義 B：同一個 id、版本、作者、觀測 schema id 與版本，內容改了。
    registry_b = _alt_tof_definition(tmp_path / "B", change)
    a = registry_a.get(KIND_SENSOR, ALT_TOF)
    b = registry_b.get(KIND_SENSOR, ALT_TOF)
    assert a.identity == b.identity
    assert (a.observation.schema_id, a.observation.version) == (
        b.observation.schema_id, b.observation.version)
    assert a.provenance.sha256 != b.provenance.sha256
    # B 的實作一樣驗證得過：能擋下 READY 的只剩這筆紀錄。
    report = ImplementationReport.verify(registry_b)
    assert report.state(b, "ingest", RESOLVABLE) == (compatibility.VERIFIED, "")

    for purpose in (PURPOSE_MEASUREMENT, PURPOSE_SIMULATION):
        with pytest.raises(ContractViolation,
                           match=_drifted("sensor", ALT_TOF, "1.0.0")) as caught:
            build_matrix(registry_b, purpose=purpose, implementations=report,
                         validations=records)
        message = str(caught.value)
        assert a.provenance.sha256 in message and b.provenance.sha256 in message
        assert ("changed too" if observation_changed else "is unchanged") in message
    with pytest.raises(ContractViolation, match=_drifted("sensor", ALT_TOF, "1.0.0")):
        evaluate_pair(registry_b, scenario, ALT_TOF, purpose=PURPOSE_MEASUREMENT,
                      implementations=report, validations=records)

    # B 本身不是不能用：為 B 重新驗證之後，才是 READY。
    revalidated = thesis_validations() + (
        _record(registry_b, PURPOSE_MEASUREMENT, scenario, ALT_TOF),)
    assert _matrix(registry_b, PURPOSE_MEASUREMENT, validations=revalidated).cell(
        scenario, ALT_TOF).status == READY


@pytest.mark.parametrize("drift", sorted(SCENARIO_DRIFT))
def test_a_scenario_that_drifted_under_the_same_version_loses_its_validation(
        tmp_path, drift):
    registry_a = _sand_definition(tmp_path / "A")
    records = thesis_validations() + (_record(registry_a, PURPOSE_SIMULATION, SAND, TOF),)
    assert _matrix(registry_a, PURPOSE_SIMULATION, validations=records).cell(
        SAND, TOF).status == READY

    registry_b = _sand_definition(tmp_path / "B", SCENARIO_DRIFT[drift])
    a = registry_a.get(KIND_SCENARIO, SAND)
    b = registry_b.get(KIND_SCENARIO, SAND)
    assert a.identity == b.identity
    assert a.provenance.sha256 != b.provenance.sha256
    report = ImplementationReport.verify(registry_b)
    assert report.state(b, "implementation", b.implementation)[0] == compatibility.VERIFIED

    for purpose in (PURPOSE_SIMULATION, PURPOSE_MEASUREMENT):
        with pytest.raises(ContractViolation,
                           match=_drifted("scenario", SAND, "0.1.0")) as caught:
            build_matrix(registry_b, purpose=purpose, implementations=report,
                         validations=records)
        assert a.provenance.sha256 in str(caught.value)
        assert b.provenance.sha256 in str(caught.value)

    revalidated = thesis_validations() + (
        _record(registry_b, PURPOSE_SIMULATION, SAND, TOF),)
    assert _matrix(registry_b, PURPOSE_SIMULATION, validations=revalidated).cell(
        SAND, TOF).status == READY


def test_the_thesis_anchors_do_not_bless_a_changed_builtin(monkeypatch):
    """碩論錨點是寫死的定義 sha256：內建描述一改，錨點就不替它背書。"""
    assert _matrix(catalog.builtin_registry(), PURPOSE_MEASUREMENT).cell(
        THESIS_SCENARIOS[0], TOF).status == READY

    monkeypatch.setitem(sensors._TOF_CHANNEL_UNITS, "sigma_like",
                        ("um", "surrogate-built ranging uncertainty"))
    with pytest.raises(ContractViolation, match=_drifted("sensor", TOF, "1.0.0")) as caught:
        _matrix(catalog.builtin_registry(), PURPOSE_MEASUREMENT)
    assert "changed too" in str(caught.value)

    monkeypatch.undo()
    monkeypatch.setitem(scenarios._CATEGORY_BY_CLASS, "Misty", "custom")
    with pytest.raises(ContractViolation,
                       match=_drifted("scenario", "pcmef.scenario.misty", "1.0.0")):
        _matrix(catalog.builtin_registry(), PURPOSE_SIMULATION)


def test_an_unchanged_definition_keeps_its_validation(tmp_path):
    """同一份定義換個檔名、資料夾、縮排與鍵的順序：還是被驗證過的那一個。"""
    registry_a = _alt_tof_definition(tmp_path / "A")
    records = thesis_validations() + (
        _record(registry_a, PURPOSE_MEASUREMENT, THESIS_SCENARIOS[0], ALT_TOF),)

    data = _fixture("acme_tof_vl53l1x.json")
    _ingest(RESOLVABLE)(data)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "renamed.json").write_text(
        json.dumps(dict(reversed(list(data.items()))), indent=4, ensure_ascii=False),
        encoding="utf-8")
    registry_c = _registry(elsewhere)
    a = registry_a.get(KIND_SENSOR, ALT_TOF)
    c = registry_c.get(KIND_SENSOR, ALT_TOF)
    assert a.provenance.location != c.provenance.location
    assert a.provenance.sha256 == c.provenance.sha256

    cell = _matrix(registry_c, PURPOSE_MEASUREMENT, validations=records).cell(
        THESIS_SCENARIOS[0], ALT_TOF)
    assert cell.status == READY, cell.describe()
    evidence = dict(cell.reason("validated_pairing").evidence)
    assert evidence["sensor_sha256"] == c.provenance.sha256
    assert evidence["scenario_sha256"] == registry_c.get(
        KIND_SCENARIO, THESIS_SCENARIOS[0]).provenance.sha256
