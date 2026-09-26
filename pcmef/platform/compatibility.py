# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.platform.catalog 產出的凍結 registry、呼叫端明確提供的
#         實作驗證結果與驗證紀錄；產出 Sensor × Scenario 的相容性判斷。
#         不寫任何東西，不修改 registry，也不被任何 stable module 匯入。
# 檔案路徑: pcmef/platform/compatibility.py
# 產生時間: 2026-09-27 02:05 +08:00
# 版本: v0.1.0
# 功能說明: SAI §10 的 Sensor × Scenario Compatibility Matrix。每一格說出
#           六種狀態之一，並保留涉及的元件身分與版本、以及一條一條可解釋
#           的理由與證據，讓之後的 Profile Builder 與 Wizard 說得出「為什麼」。
# 模組定位: Phase 3 第二片。**只消費** registry 的 metadata，不取代它、
#           不複製碩論的科學常數，也不因為元件存在或家族相同就推論相容。
# 主要責任:
#   1. STATUSES / PURPOSES：六種狀態（SAI §10）與兩種用途（模擬 / 量測）
#   2. ValidationRecord / thesis_validations()：驗證紀錄；碩論配對為迴歸錨點
#   3. ImplementationReport：實作驗證**明確地**做或明確地不做
#   4. evaluate_pair() / build_matrix()：逐條理由，取最嚴格的狀態
#   5. Cell / UnservedFamily / CompatibilityMatrix：結構化、可 JSON 序列化
# 維護提醒:
#   - **READY 只有一條路：** 完全相同的 scenario@版本 × sensor@版本 × 用途
#     有驗證紀錄，而且這次評估中實作驗證通過。不得再開第二條。
#   - 不得讓 declared_sensor_families 決定相容。它是作者的宣告 —— 有宣告
#     只是不扣分，沒有宣告則降為 NEEDS_VALIDATION。
#   - 不得讓 plugin_required 的情境在模擬用途上高於 PLUGIN_REQUIRED，不論
#     有沒有驗證紀錄、有多少相容的感測器。
#   - 不得把「同一家族」當成等價。未經驗證的 adapter 一律與同情境、同用途
#     已驗證的參考 adapter 比對觀測，差異逐項列入理由。
#   - 不得在實作驗證失敗時給出 BLOCKED 以外的狀態；未做驗證時不得給 READY。
#     ImplementationReport 只能由 verify() 或 not_performed() 產生 —— 一份
#     手寫的「已驗證」等於沒有驗證。
#   - 不得在查不到身分時退回碩論的預設元件 —— 一律 UnknownComponent /
#     AmbiguousVersion。驗證紀錄必須寫明確切版本。
#   - v0.1.0 新增：首版，對應 SAI Phase 3 第二片。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_compatibility_matrix.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from pcmef.core.constants import CLASS_ORDER
from pcmef.platform.catalog import verify_reference
from pcmef.platform.components import (
    KIND_SCENARIO,
    KIND_SENSOR,
    ComponentIdentity,
    ComponentRegistry,
    ContractViolation,
    UnknownComponent,
    valid_version,
)
from pcmef.platform.scenarios import SUPPORT_PLUGIN, SUPPORT_REQUIRED, SUPPORT_TEMPLATE
from pcmef.platform.sensors import compare_observations

__all__ = [
    "BLOCKED",
    "NEEDS_VALIDATION",
    "PLUGIN_REQUIRED",
    "PURPOSES",
    "PURPOSE_MEASUREMENT",
    "PURPOSE_SIMULATION",
    "READY",
    "STATUSES",
    "TEMPLATE_SUPPORTED",
    "UNSUPPORTED",
    "Cell",
    "CompatibilityMatrix",
    "ImplementationReport",
    "Reason",
    "UnservedFamily",
    "ValidationRecord",
    "build_matrix",
    "evaluate_pair",
    "thesis_validations",
]

READY = "READY"
TEMPLATE_SUPPORTED = "TEMPLATE_SUPPORTED"
NEEDS_VALIDATION = "NEEDS_VALIDATION"
PLUGIN_REQUIRED = "PLUGIN_REQUIRED"
UNSUPPORTED = "UNSUPPORTED"
BLOCKED = "BLOCKED"

#: SAI §10 的六種狀態，依 SAI 列出的順序。
STATUSES: tuple[str, ...] = (
    READY, TEMPLATE_SUPPORTED, NEEDS_VALIDATION, PLUGIN_REQUIRED, UNSUPPORTED, BLOCKED,
)

#: 一格的狀態取所有理由中**最嚴格**的那一個。數字越大越嚴格。
#:
#: BLOCKED（宣告的東西壞了）> UNSUPPORTED（這條路根本不存在）>
#: PLUGIN_REQUIRED（缺一個 plugin 才走得通）> NEEDS_VALIDATION >
#: TEMPLATE_SUPPORTED > READY。UNSUPPORTED 排在 PLUGIN_REQUIRED 之前：
#: 一個不能模擬的感測器，補了情境的 physics plugin 也還是不能模擬。
_SEVERITY: dict[str, int] = {
    READY: 0, TEMPLATE_SUPPORTED: 1, NEEDS_VALIDATION: 2,
    PLUGIN_REQUIRED: 3, UNSUPPORTED: 4, BLOCKED: 5,
}

PURPOSE_SIMULATION = "simulation"    # 以模擬產生這個情境的觀測
PURPOSE_MEASUREMENT = "measurement"  # 以真實量測讀入這個情境的觀測
PURPOSES: tuple[str, ...] = (PURPOSE_SIMULATION, PURPOSE_MEASUREMENT)

#: 每種用途需要感測器的哪一項能力。
_CAPABILITY: dict[str, str] = {PURPOSE_SIMULATION: "simulate",
                               PURPOSE_MEASUREMENT: "ingest"}


# ---------------------------------------------------------------------------
# 理由、驗證紀錄、實作驗證
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reason:
    """一條理由。`limit` 是它允許這一格**最多**到達的狀態。"""

    code: str
    limit: str
    detail: str
    evidence: tuple[tuple[str, Any], ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "code": self.code, "limit": self.limit, "detail": self.detail,
            "evidence": {key: list(value) if isinstance(value, tuple) else value
                         for key, value in self.evidence},
        }


@dataclass(frozen=True)
class ValidationRecord:
    """一次**已完成**的相容性驗證：哪個情境版本 × 哪個感測器版本 × 哪個用途。

    綁定到確切的版本與觀測 schema：元件升版不會繼承舊版的驗證
    （Appendix A 第 7 條）。
    """

    purpose: str
    scenario: tuple[str, str]      # (id, version)
    sensor: tuple[str, str]        # (id, version)
    observation: tuple[str, str]   # (schema_id, version) —— 驗證時的觀測
    evidence: str
    source: str

    def key(self) -> tuple[str, tuple[str, str, str], tuple[str, str, str]]:
        return (self.purpose, (KIND_SCENARIO, *self.scenario),
                (KIND_SENSOR, *self.sensor))

    def describe(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "scenario": {"id": self.scenario[0], "version": self.scenario[1]},
            "sensor": {"id": self.sensor[0], "version": self.sensor[1]},
            "observation": {"schema_id": self.observation[0],
                            "version": self.observation[1]},
            "evidence": self.evidence, "source": self.source,
        }


_THESIS_SIMULATION_EVIDENCE = (
    "thesis-frozen pairing: the PC-MEF thesis generates paired RGB and ToF "
    "observations of every CLASS_ORDER class from one scene (research design: "
    "modalities; E2 paired benchmark)"
)
_THESIS_MEASUREMENT_EVIDENCE = (
    "thesis-frozen pairing: real VL53L0X recordings of every CLASS_ORDER class "
    "are the reference of the E1 physics-calibration validation (research "
    "design: RQ1)"
)

_THESIS_RGB = ("pcmef.sensor.rgb-camera", "1.0.0")
_THESIS_TOF = ("pcmef.sensor.tof-vl53l0x", "1.0.0")
_RGB_OBSERVATION = ("pcmef.rgb.image", "1.0.0")
_TOF_OBSERVATION = ("pcmef.tof.recording", "1.0.0")


def thesis_validations() -> tuple[ValidationRecord, ...]:
    """碩論既有的組合：**迴歸錨點**，不是新的科學主張。

    四個類別 × {RGB 模擬、ToF 模擬、ToF 真實量測}。RGB 沒有真實量測路徑，
    所以沒有 RGB 量測的紀錄。只列身分與版本，不複製任何科學數值；情境 id
    的推導與 scenarios.thesis_scenarios() 相同，類別只有 CLASS_ORDER 一個來源。
    """
    records = []
    for label in CLASS_ORDER:
        scenario = (f"pcmef.scenario.{label.lower()}", "1.0.0")
        records.append(ValidationRecord(
            PURPOSE_SIMULATION, scenario, _THESIS_RGB, _RGB_OBSERVATION,
            _THESIS_SIMULATION_EVIDENCE, "thesis"))
        records.append(ValidationRecord(
            PURPOSE_SIMULATION, scenario, _THESIS_TOF, _TOF_OBSERVATION,
            _THESIS_SIMULATION_EVIDENCE, "thesis"))
        records.append(ValidationRecord(
            PURPOSE_MEASUREMENT, scenario, _THESIS_TOF, _TOF_OBSERVATION,
            _THESIS_MEASUREMENT_EVIDENCE, "thesis"))
    return tuple(records)


VERIFIED = "verified"
FAILED = "failed"
NOT_VERIFIED = "not_verified"

#: 只有這個模組的兩個工廠方法拿得到。手寫一份「已驗證」的報告會被拒絕。
_ISSUED = object()


@dataclass(frozen=True)
class ImplementationReport:
    """實作驗證的結果。**要嘛明確地做了，要嘛明確地沒做。**

    `verify()` 會匯入每一個宣告的實作 —— 那是呼叫端決定要付的代價，
    不是探索或矩陣偷偷做的事。`not_performed()` 讓不想匯入程式碼的
    呼叫端也能建矩陣，代價是沒有任何一格能到 READY。

    結果以「身分 + 能力」為鍵，並記下當時驗證的實作指向：同一個身分
    若換了指向，舊的結果不算數。
    """

    performed: bool
    results: Mapping[tuple[tuple[str, str, str], str], tuple[str, str]]
    _issued: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issued is not _ISSUED:
            raise TypeError(
                "an ImplementationReport comes only from "
                "ImplementationReport.verify() or .not_performed()"
            )

    @classmethod
    def verify(cls, registry: ComponentRegistry) -> "ImplementationReport":
        results: dict[tuple[tuple[str, str, str], str], tuple[str, str]] = {}
        for scenario in registry.components(KIND_SCENARIO):
            if scenario.implementation is not None:
                results[(scenario.identity.key, "implementation")] = (
                    scenario.implementation, verify_reference(scenario.implementation))
        for sensor in registry.components(KIND_SENSOR):
            for name, reference in sensor.implementations().items():
                if reference is not None:
                    results[(sensor.identity.key, name)] = (
                        reference, verify_reference(reference))
        return cls(True, MappingProxyType(results), _ISSUED)

    @classmethod
    def not_performed(cls) -> "ImplementationReport":
        return cls(False, MappingProxyType({}), _ISSUED)

    def state(self, component: Any, name: str, reference: str) -> tuple[str, str]:
        if not self.performed:
            return NOT_VERIFIED, "implementation verification was not performed"
        found = self.results.get((component.identity.key, name))
        if found is None:
            return NOT_VERIFIED, "this component was not part of the verified registry"
        verified_reference, problem = found
        if verified_reference != reference:
            return NOT_VERIFIED, (f"the verification checked {verified_reference}, "
                                  f"not {reference}")
        return (FAILED, problem) if problem else (VERIFIED, "")

    def describe(self) -> dict[str, Any]:
        return {
            "performed": self.performed,
            "results": [
                {"kind": key[0], "id": key[1], "version": key[2], "name": name,
                 "reference": reference, "problem": problem or None}
                for (key, name), (reference, problem) in sorted(self.results.items())
            ],
        }


# ---------------------------------------------------------------------------
# 矩陣
# ---------------------------------------------------------------------------


def _worst(reasons: Iterable[Reason]) -> str:
    return max((r.limit for r in reasons), key=_SEVERITY.__getitem__, default=READY)


@dataclass(frozen=True)
class Cell:
    """一個情境 × 一個感測器 × 一種用途的判斷，以及全部理由。"""

    purpose: str
    scenario: ComponentIdentity
    sensor: ComponentIdentity
    status: str
    reasons: tuple[Reason, ...]

    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.reasons)

    def reason(self, code: str) -> Reason:
        for reason in self.reasons:
            if reason.code == code:
                return reason
        raise KeyError(code)

    def describe(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "scenario": self.scenario.describe(),
            "sensor": self.sensor.describe(),
            "status": self.status,
            "reasons": [r.describe() for r in self.reasons],
        }


@dataclass(frozen=True)
class UnservedFamily:
    """情境宣告了某個感測家族，而 registry 裡沒有任何那個家族的 adapter。"""

    scenario: ComponentIdentity
    family: str
    status: str
    detail: str

    def describe(self) -> dict[str, Any]:
        return {"scenario": self.scenario.describe(), "family": self.family,
                "status": self.status, "detail": self.detail}


def _index(registry: ComponentRegistry,
           validations: Iterable[ValidationRecord]) -> dict[tuple, ValidationRecord]:
    """驗證紀錄 → 以確切身分為鍵。**參照不到、互相矛盾、重複的紀錄一律拋出。**"""
    index: dict[tuple, ValidationRecord] = {}
    for record in validations:
        problems = []
        if record.purpose not in PURPOSES:
            problems.append(f"unknown purpose {record.purpose!r}")
        for role, (component_id, version) in (("scenario", record.scenario),
                                              ("sensor", record.sensor)):
            if not isinstance(version, str) or not valid_version(version):
                # 沒寫版本就讓 registry 挑一個，等於紀錄會自動跟著升版。
                problems.append(f"{role} {component_id!r} must name an exact version, "
                                f"got {version!r}")
        if problems:
            raise ContractViolation("validation record", problems)
        # 參照不到就是 UnknownComponent —— 不得當成「這筆不適用」而略過。
        registry.get(KIND_SCENARIO, *record.scenario)
        sensor = registry.get(KIND_SENSOR, *record.sensor)
        produced = (sensor.observation.schema_id, sensor.observation.version)
        if tuple(record.observation) != produced:
            problems.append(
                f"{record.sensor[0]} {record.sensor[1]} produces {produced}, but the "
                f"record claims it was validated on {tuple(record.observation)}")
        capability = _CAPABILITY[record.purpose]
        if not sensor.capabilities[capability]:
            problems.append(
                f"{record.sensor[0]} {record.sensor[1]} cannot {capability}, so it "
                f"cannot have been validated for {record.purpose}")
        key = record.key()
        if key in index:
            problems.append(f"duplicate record for {key}")
        if problems:
            raise ContractViolation("validation record", problems)
        index[key] = record
    return index


def _implementation_reason(implementations: ImplementationReport, component: Any,
                           name: str, reference: str, subject: str,
                           prefix: str) -> Reason:
    state, detail = implementations.state(component, name, reference)
    if state == FAILED:
        return Reason(
            f"{prefix}_implementation_unverifiable", BLOCKED,
            f"{subject} is declared but its implementation cannot be verified; a "
            "declaration without working code is not a capability",
            (("reference", reference), ("problem", detail)))
    if state == NOT_VERIFIED:
        return Reason(
            f"{prefix}_implementation_not_verified", NEEDS_VALIDATION,
            f"{subject} was not verified in this evaluation ({detail})",
            (("reference", reference),))
    return Reason(f"{prefix}_implementation_verified", READY,
                  f"{subject} resolves to callable code", (("reference", reference),))


def _evaluate(registry: ComponentRegistry, scenario: Any, sensor: Any, purpose: str,
              implementations: ImplementationReport,
              index: Mapping[tuple, ValidationRecord]) -> Cell:
    reasons: list[Reason] = []
    capability = _CAPABILITY[purpose]
    sensor_name = f"{sensor.identity.component_id} {sensor.identity.version}"

    # 1 感測器走不走得了這條路（註冊 ≠ 實作存在）
    if not sensor.capabilities[capability]:
        reasons.append(Reason(
            f"sensor_cannot_{capability}", UNSUPPORTED,
            f"{sensor_name} declares no {capability!r} capability, so it cannot be "
            f"used for {purpose}"))
    else:
        reasons.append(_implementation_reason(
            implementations, sensor, capability, sensor.implementations()[capability],
            f"{sensor_name}'s {capability!r}", "sensor"))

    # 2 情境的 physics（只有模擬會用到）
    if purpose == PURPOSE_SIMULATION:
        support = scenario.simulation_support
        if support == SUPPORT_REQUIRED:
            reasons.append(Reason(
                "scenario_physics_missing", PLUGIN_REQUIRED,
                "the scenario has neither a physics template nor an implementation; "
                "no sensor can simulate what has no physics (SAI §7.3)"))
        elif support == SUPPORT_PLUGIN:
            reasons.append(_implementation_reason(
                implementations, scenario, "implementation", scenario.implementation,
                "the scenario's physics implementation", "scenario"))
        else:
            reasons.append(Reason(
                "scenario_template_backed", READY,
                "the scenario is represented by an existing physics template",
                (("template_id", scenario.template_id),)))
    else:
        reasons.append(Reason(
            "physics_not_used_for_measurement", READY,
            "a real measurement does not run the scenario's simulated physics",
            (("physics_support", scenario.simulation_support),)))

    # 3 作者的宣告：輸入，不是判決
    if sensor.family in scenario.declared_sensor_families:
        reasons.append(Reason(
            "family_declared_by_scenario", READY,
            f"the scenario author declares {sensor.family!r}; a declaration is "
            "evidence, not a compatibility verdict",
            (("declared_sensor_families", scenario.declared_sensor_families),)))
    else:
        reasons.append(Reason(
            "family_not_declared_by_scenario", NEEDS_VALIDATION,
            f"the scenario does not declare {sensor.family!r}; nobody has claimed "
            "this family can observe it",
            (("declared_sensor_families", scenario.declared_sensor_families),)))

    # 4 驗證紀錄：READY 的唯一來源
    record = index.get((purpose, scenario.identity.key, sensor.identity.key))
    if record is not None:
        reasons.append(Reason(
            "validated_pairing", READY, record.evidence,
            (("source", record.source), ("observation", tuple(record.observation)))))
        return Cell(purpose, scenario.identity, sensor.identity, _worst(reasons),
                    tuple(reasons))

    if purpose == PURPOSE_SIMULATION and scenario.simulation_support == SUPPORT_TEMPLATE:
        reasons.append(Reason(
            "pairing_not_validated_template", TEMPLATE_SUPPORTED,
            "an existing template can represent this scenario, but this exact "
            "pairing has no validation record"))
    else:
        reasons.append(Reason(
            "pairing_not_validated", NEEDS_VALIDATION,
            f"this exact pairing has no validation record for {purpose}"))

    # 5 同一家族不等於等價：與同情境、同用途、同家族的已驗證參考比對
    references = sorted(
        (r for r in index.values()
         if r.purpose == purpose
         and (KIND_SCENARIO, *r.scenario) == scenario.identity.key),
        key=lambda r: r.sensor,
    )
    for reference in references:
        reference_sensor = registry.get(KIND_SENSOR, *reference.sensor)
        if reference_sensor.family != sensor.family:
            continue
        differences = compare_observations(reference_sensor, sensor)
        referenced = f"{reference.sensor[0]} {reference.sensor[1]}"
        if differences:
            reasons.append(Reason(
                "observation_differs_from_validated_reference", NEEDS_VALIDATION,
                f"same family as the validated {referenced}, but the observation "
                "differs; the family alone does not make them interchangeable",
                (("reference", tuple(reference.sensor)), ("differences", differences))))
        else:
            reasons.append(Reason(
                "observation_matches_validated_reference", READY,
                f"the observation schema matches the validated {referenced}; the "
                "pairing itself is still unvalidated",
                (("reference", tuple(reference.sensor)),)))

    return Cell(purpose, scenario.identity, sensor.identity, _worst(reasons),
                tuple(reasons))


def _require(registry: ComponentRegistry, purpose: str,
             implementations: ImplementationReport) -> None:
    problems = []
    if not registry.frozen:
        problems.append("the registry is not frozen; build it with catalog.discover()")
    if purpose not in PURPOSES:
        problems.append(f"unknown purpose {purpose!r}; expected {list(PURPOSES)}")
    if not isinstance(implementations, ImplementationReport):
        problems.append(
            "implementations must be an ImplementationReport from verify() or "
            "not_performed()")
    if problems:
        raise ContractViolation("compatibility matrix", problems)


def evaluate_pair(registry: ComponentRegistry, scenario_id: str, sensor_id: str, *,
                  purpose: str, implementations: ImplementationReport,
                  validations: Iterable[ValidationRecord],
                  scenario_version: str | None = None,
                  sensor_version: str | None = None) -> Cell:
    """評估一格。身分查不到或不明確時**拋出**，從不退回碩論的預設元件。"""
    _require(registry, purpose, implementations)
    scenario = registry.get(KIND_SCENARIO, scenario_id, scenario_version)
    sensor = registry.get(KIND_SENSOR, sensor_id, sensor_version)
    return _evaluate(registry, scenario, sensor, purpose, implementations,
                     _index(registry, validations))


@dataclass(frozen=True)
class CompatibilityMatrix:
    """一種用途下，每一個情境 × 每一個感測器的判斷。"""

    purpose: str
    verification_performed: bool
    cells: tuple[Cell, ...]
    unserved: tuple[UnservedFamily, ...]
    registry: ComponentRegistry = field(repr=False, compare=False)

    def cell(self, scenario_id: str, sensor_id: str, *,
             scenario_version: str | None = None,
             sensor_version: str | None = None) -> Cell:
        """取出一格。身分的解析與 registry 相同：查不到、不明確都拋出。"""
        scenario = self.registry.get(KIND_SCENARIO, scenario_id, scenario_version)
        sensor = self.registry.get(KIND_SENSOR, sensor_id, sensor_version)
        for cell in self.cells:
            if cell.scenario == scenario.identity and cell.sensor == sensor.identity:
                return cell
        raise UnknownComponent(
            f"no cell for {scenario.identity.key} × {sensor.identity.key}")

    def table(self) -> dict[str, dict[str, str]]:
        """`{scenario id@version: {sensor id@version: status}}` —— 給畫面用。"""
        table: dict[str, dict[str, str]] = {}
        for cell in self.cells:
            row = f"{cell.scenario.component_id}@{cell.scenario.version}"
            column = f"{cell.sensor.component_id}@{cell.sensor.version}"
            table.setdefault(row, {})[column] = cell.status
        return table

    def describe(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "statuses": list(STATUSES),
            "verification_performed": self.verification_performed,
            "cells": [c.describe() for c in self.cells],
            "unserved_families": [u.describe() for u in self.unserved],
        }


def build_matrix(registry: ComponentRegistry, *, purpose: str,
                 implementations: ImplementationReport,
                 validations: Iterable[ValidationRecord]) -> CompatibilityMatrix:
    """每一個已註冊的情境 × 每一個已註冊的感測器，依身分排序。

    `implementations` 與 `validations` 都必須由呼叫端明確給出：矩陣不會
    自己去匯入程式碼，也不會自己假設碩論的組合已經驗證過。
    """
    _require(registry, purpose, implementations)
    index = _index(registry, validations)
    scenarios = registry.components(KIND_SCENARIO)
    sensors = registry.components(KIND_SENSOR)
    cells = tuple(
        _evaluate(registry, scenario, sensor, purpose, implementations, index)
        for scenario in scenarios for sensor in sensors
    )
    families = {sensor.family for sensor in sensors}
    unserved = tuple(
        UnservedFamily(
            scenario.identity, family, PLUGIN_REQUIRED,
            f"the scenario declares {family!r}, but no adapter of that family is "
            "registered; a sensor adapter plugin is required")
        for scenario in scenarios
        for family in scenario.declared_sensor_families
        if family not in families
    )
    return CompatibilityMatrix(purpose, implementations.performed, cells, unserved,
                               registry)
