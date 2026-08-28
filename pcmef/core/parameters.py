# PC-MEF Research System source maintenance contract
# 上下游: 讀 configs/parameter_registry.yaml；被 simulation.scenario 的 formal 建構、
#         surrogate.calibration.assert_formal_ready() 與 cli 的 params audit 呼叫；
#         產出的 parameter_set_hash 進 simulation manifest 與 initial_simulation.lock。
# 檔案路徑: pcmef/core/parameters.py
# 產生時間: 2026-08-28 09:40 +08:00
# 版本: v0.1.0
# 功能說明: 把 parameter registry 由一份說明文件變成會擋人的防線 —— 載入並驗證
#           registry、算出 parameter_set_hash、比對 registry 宣稱的值與程式碼裡
#           真正在用的值，並在 formal 模式下逐條列出為什麼不能放行。
# 模組定位: 參數層的 formal firewall。它「不是」設定載入器 ——
#           它不供應數值給模擬，只負責判定「這組參數現在可不可以進 formal」。
#           數值仍各自留在模組常數與 scenario yaml。
# 主要責任:
#   1. ParameterRegistry.load() 載入並驗證 registry 結構
#   2. parameter_set_hash() 對整組參數身分取 canonical SHA-256
#   3. live_value_drift() 比對 registry 宣稱值與程式碼實際值
#   4. assert_formal_ready() 在 formal 模式逐條攔截並回報全部理由
# 維護提醒:
#   - 不得把 RECONSTRUCTED / PLACEHOLDER / UNKNOWN 視為可進 formal；
#     只有 CONFIRMED 與 FIXED_SPEC 過得了。放寬這個集合等同拆掉本模組。
#   - 不得在 assert_formal_ready() 遇到第一個問題就 return；一次只擋一條會讓
#     交接的人以為只剩一項，實際還有二十幾項（NOTE-030）。
#   - 不得為了讓 live_value_drift() 變空而修改 registry 的 value 欄；
#     drift 代表程式與登記不一致，要查的是哪一邊錯，不是把記錄改成跟程式一樣。
#   - 不得把 confounded group 的 decision 直接寫成 RESOLVED 而不填 rationale；
#     schema 會拒絕，這是刻意的（NOTE-031）。
#   - v0.1.0 新增：首版 registry firewall 與 parameter_set_hash（NOTE-030）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_parameter_registry.py -v
#   - py -3.10 -m pcmef.cli params audit
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcmef.core.hash import canonical_json, hash_object

__all__ = [
    "ParameterRegistryError",
    "FORMAL_ELIGIBLE_STATUSES",
    "PROVENANCE_STATUSES",
    "PARAMETER_KINDS",
    "DEFAULT_REGISTRY_PATH",
    "RegistryParameter",
    "ConfoundedGroup",
    "ParameterRegistry",
    "live_values",
    "live_value_drift",
    "assert_formal_ready",
]


class ParameterRegistryError(ValueError):
    """registry 缺檔、結構不合法，或這組參數不得進入 formal。"""


#: 只有這兩種 provenance 狀態能進 formal。RECONSTRUCTED 刻意排除 ——
#: 「由既有資訊推得」與「有一手證據」是兩件事，NOTE-028 已在 E1-G08
#: 用同一條界線區分過，這裡沿用同一個標準。
FORMAL_ELIGIBLE_STATUSES = frozenset({"CONFIRMED", "FIXED_SPEC"})

#: gauge / 正規化慣例。用於一個**在原理上不可辨識**的自由度：它沒有物理真值，
#: 固定它不是「假裝知道」，而是選一個座標系。
#:
#: 這個狀態只在極窄的條件下能進 formal（見 _gauge_violations）：
#: 必須是某個 **RESOLVED** confounded group 的 `fixed` 成員，且該 group 必須
#: 寫明 claim_boundary。單獨把一個 PLACEHOLDER 改標 CONVENTION 不會放行 ——
#: 否則這個狀態就會變成繞過防線的萬用鑰匙（NOTE-031）。
GAUGE_STATUS = "CONVENTION"

PROVENANCE_STATUSES = frozenset(
    {"CONFIRMED", "FIXED_SPEC", GAUGE_STATUS, "RECONSTRUCTED", "PLACEHOLDER", "UNKNOWN"}
)
PARAMETER_KINDS = frozenset({"fixed", "derived", "calibration_only", "topology"})
CLASS_SCOPES = frozenset({"shared", "class_specific", "n/a"})

#: confounded group 的裁決狀態。BLOCKED 是合法且必要的終局狀態 ——
#: 「還沒有足夠依據」必須說得出口，否則唯一的出路就是偷偷給一個 default。
GROUP_DECISION_STATUSES = frozenset({"RESOLVED", "BLOCKED"})

DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "parameter_registry.yaml"
)

#: 進 parameter_set_hash 的欄位。刻意排除 role / note 這類散文欄位：
#: 修一個錯字不該讓所有既有 lock 失效。但 source 有進 —— 一個參數換了出處
#: 就是換了身分，即使數值沒變。
_HASHED_PARAMETER_FIELDS = (
    "name",
    "source",
    "value",
    "provenance_status",
    "kind",
    "class_scope",
    "formal_blocking",
    "allowed_range",
    "confounded_with",
)


@dataclass(frozen=True)
class RegistryParameter:
    """registry 中的單一參數。"""

    name: str
    source: str
    role: str
    value: Any
    provenance_status: str
    kind: str
    class_scope: str
    formal_blocking: bool
    allowed_range: Any = None
    confounded_with: tuple[str, ...] = ()
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.provenance_status in FORMAL_ELIGIBLE_STATUSES

    def hash_payload(self) -> dict[str, Any]:
        payload = {field: getattr(self, field) for field in _HASHED_PARAMETER_FIELDS}
        payload["confounded_with"] = sorted(self.confounded_with)
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload = self.hash_payload()
        payload["role"] = self.role
        payload["note"] = self.note
        payload["resolved"] = self.resolved
        return payload


@dataclass(frozen=True)
class ConfoundedGroup:
    """一組共同決定同一物理量、只有乘積可辨識的參數。"""

    group_id: str
    physical_quantity: str
    members: tuple[str, ...]
    relation: str
    decision: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        """UNDECIDED / RESOLVED / BLOCKED。UNDECIDED 代表連 BLOCKED 都還沒宣告。"""
        if not self.decision:
            return "UNDECIDED"
        return str(self.decision.get("status", "UNDECIDED"))

    @property
    def decided(self) -> bool:
        """是否已有明示裁決。BLOCKED 也算已裁決 —— 它是一個結論，不是空白。"""
        return self.status in GROUP_DECISION_STATUSES

    @property
    def formal_ready(self) -> bool:
        """只有 RESOLVED 能進 formal；BLOCKED 已裁決但仍擋 formal。"""
        return self.status == "RESOLVED"

    def hash_payload(self) -> dict[str, Any]:
        return {
            "id": self.group_id,
            "members": sorted(self.members),
            "relation": self.relation,
            "decision": self.decision or None,
        }


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ParameterRegistryError(f"{context}: required key {key!r} is missing")
    return mapping[key]


def _validate_decision(group_id: str, decision: dict[str, Any], members: set[str]) -> None:
    """裁決區塊的 schema。刻意嚴格：一個沒有理由的 RESOLVED 比沒裁決更危險。"""
    context = f"confounded group {group_id!r} decision"
    if not isinstance(decision, dict):
        raise ParameterRegistryError(f"{context} must be a mapping")

    status = _require(decision, "status", context)
    if status not in GROUP_DECISION_STATUSES:
        raise ParameterRegistryError(
            f"{context}: status must be one of {sorted(GROUP_DECISION_STATUSES)}, "
            f"got {status!r}"
        )
    for key in ("rationale", "decided_on"):
        value = _require(decision, key, context)
        if not isinstance(value, str) or not value.strip():
            raise ParameterRegistryError(f"{context}: {key} must be a non-empty string")

    if status == "RESOLVED":
        fixed = _require(decision, "fixed", context)
        calibrated = _require(decision, "calibrated", context)
        boundary = _require(decision, "claim_boundary", context)
        if not isinstance(boundary, str) or not boundary.strip():
            raise ParameterRegistryError(
                f"{context}: a RESOLVED group must state claim_boundary -- fixing one "
                "member is a choice of gauge, and the price of that choice is that "
                "the remaining members lose their absolute interpretation. If that "
                "price cannot be written down, the group is not resolved."
            )
        if not isinstance(fixed, list) or not fixed:
            raise ParameterRegistryError(
                f"{context}: a RESOLVED group must fix at least one member; "
                "fitting every member of a confounded group has infinitely many "
                "equivalent solutions"
            )
        if not isinstance(calibrated, list):
            raise ParameterRegistryError(f"{context}: calibrated must be a list")
        unknown = (set(fixed) | set(calibrated)) - members
        if unknown:
            raise ParameterRegistryError(
                f"{context}: fixed/calibrated name(s) {sorted(unknown)} are not "
                f"members of the group"
            )
        overlap = set(fixed) & set(calibrated)
        if overlap:
            raise ParameterRegistryError(
                f"{context}: {sorted(overlap)} appear as both fixed and calibrated"
            )
        uncovered = members - (set(fixed) | set(calibrated))
        if uncovered:
            raise ParameterRegistryError(
                f"{context}: member(s) {sorted(uncovered)} are neither fixed nor "
                "calibrated; every member must be assigned or the group is still "
                "under-determined"
            )
    else:  # BLOCKED
        for key in ("blocked_reason", "unblock_requires"):
            value = _require(decision, key, context)
            if not value:
                raise ParameterRegistryError(
                    f"{context}: a BLOCKED group must state {key}; "
                    "'blocked' without a route out is indistinguishable from neglect"
                )


@dataclass(frozen=True)
class ParameterRegistry:
    """整份 registry。"""

    registry_version: str
    parameters: tuple[RegistryParameter, ...]
    groups: tuple[ConfoundedGroup, ...]
    source_path: Path | None = None

    # -- 查詢 ---------------------------------------------------------------

    def by_name(self) -> dict[str, RegistryParameter]:
        return {parameter.name: parameter for parameter in self.parameters}

    def sanctioned_gauges(self) -> set[str]:
        """被某個 RESOLVED group 正式指定為固定項、且已寫明 claim_boundary 的參數。"""
        sanctioned: set[str] = set()
        for group in self.groups:
            if not group.formal_ready:
                continue
            decision = group.decision or {}
            if not str(decision.get("claim_boundary", "")).strip():
                continue
            sanctioned.update(decision.get("fixed", []) or [])
        return sanctioned

    def unresolved(self) -> list[RegistryParameter]:
        sanctioned = self.sanctioned_gauges()
        return [
            p
            for p in self.parameters
            if not p.resolved
            and not (p.provenance_status == GAUGE_STATUS and p.name in sanctioned)
        ]

    def formal_blockers(self) -> list[RegistryParameter]:
        """formal_blocking 且尚未解決的參數 —— 擋住 formal 的實際清單。"""
        unresolved = {p.name for p in self.unresolved()}
        return [p for p in self.parameters if p.formal_blocking and p.name in unresolved]

    def gauge_violations(self) -> list[str]:
        """標了 CONVENTION 卻沒有被任何 RESOLVED group 授權的參數。"""
        sanctioned = self.sanctioned_gauges()
        return [
            f"{p.name} is marked {GAUGE_STATUS} but is not the fixed member of any "
            "RESOLVED confounded group carrying a claim_boundary; a gauge is only "
            "legitimate as the fixed end of a declared degeneracy"
            for p in self.parameters
            if p.provenance_status == GAUGE_STATUS and p.name not in sanctioned
        ]

    def counts(self) -> dict[str, int]:
        by_kind: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for parameter in self.parameters:
            by_kind[parameter.kind] = by_kind.get(parameter.kind, 0) + 1
            by_status[parameter.provenance_status] = (
                by_status.get(parameter.provenance_status, 0) + 1
            )
        return {
            "total": len(self.parameters),
            "unresolved": len(self.unresolved()),
            "formal_blocking": sum(1 for p in self.parameters if p.formal_blocking),
            "formal_blockers": len(self.formal_blockers()),
            "class_specific": sum(
                1 for p in self.parameters if p.class_scope == "class_specific"
            ),
            "confounded_groups": len(self.groups),
            "groups_resolved": sum(1 for g in self.groups if g.formal_ready),
            "groups_blocked": sum(1 for g in self.groups if g.status == "BLOCKED"),
            "groups_undecided": sum(1 for g in self.groups if not g.decided),
            **{f"kind.{k}": v for k, v in sorted(by_kind.items())},
            **{f"status.{k}": v for k, v in sorted(by_status.items())},
        }

    # -- 身分 ---------------------------------------------------------------

    def parameter_set_hash(self) -> str:
        """整組參數身分的 canonical SHA-256。

        涵蓋 registry_version、每個參數的 _HASHED_PARAMETER_FIELDS，以及每一組
        confounded group 的成員與裁決。**不涵蓋** role/note 等散文欄位。

        「裁決」進 hash 是刻意的：同樣的數值配上不同的「固定哪一項」，
        是兩組不同的實驗設定，不該共用同一個身分。
        """
        payload = {
            "registry_version": self.registry_version,
            "parameters": [
                p.hash_payload() for p in sorted(self.parameters, key=lambda x: x.name)
            ],
            "confounded_groups": [
                g.hash_payload() for g in sorted(self.groups, key=lambda x: x.group_id)
            ],
        }
        return hash_object(payload)

    def summary(self) -> dict[str, Any]:
        """進 formal artifact 的摘要區塊。"""
        return {
            "registry_version": self.registry_version,
            "parameter_set_hash": self.parameter_set_hash(),
            "counts": self.counts(),
            "formal_blockers": sorted(p.name for p in self.formal_blockers()),
            "confounded_groups": {
                group.group_id: {
                    "status": group.status,
                    "members": sorted(group.members),
                    "fixed": sorted((group.decision or {}).get("fixed", []) or []),
                    "calibrated": sorted(
                        (group.decision or {}).get("calibrated", []) or []
                    ),
                }
                for group in sorted(self.groups, key=lambda g: g.group_id)
            },
        }

    # -- 載入 ---------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ParameterRegistry":
        import yaml

        registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
        if not registry_path.exists():
            raise ParameterRegistryError(
                f"parameter registry not found at {registry_path}. Formal mode cannot "
                "proceed without it: with no registry there is no answer to 'how many "
                "uncalibrated parameters are in this run', and an unanswered question "
                "is not the same as zero."
            )
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        registry = cls.from_mapping(raw)
        return cls(
            registry_version=registry.registry_version,
            parameters=registry.parameters,
            groups=registry.groups,
            source_path=registry_path,
        )

    @classmethod
    def from_mapping(cls, raw: Any) -> "ParameterRegistry":
        if not isinstance(raw, dict):
            raise ParameterRegistryError("parameter registry must be a mapping")

        version = raw.get("registry_version")
        if not isinstance(version, str) or not version:
            raise ParameterRegistryError("registry_version is required")

        entries = raw.get("parameters")
        if not isinstance(entries, list) or not entries:
            raise ParameterRegistryError("registry must declare a non-empty parameters list")

        parameters: list[RegistryParameter] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries, start=1):
            context = f"parameter #{index}"
            if not isinstance(entry, dict):
                raise ParameterRegistryError(f"{context} must be a mapping")
            name = str(_require(entry, "name", context))
            if name in seen:
                raise ParameterRegistryError(f"duplicate parameter name {name!r}")
            seen.add(name)

            status = str(_require(entry, "provenance_status", context))
            if status not in PROVENANCE_STATUSES:
                raise ParameterRegistryError(
                    f"{name}: provenance_status must be one of "
                    f"{sorted(PROVENANCE_STATUSES)}, got {status!r}"
                )
            kind = str(_require(entry, "kind", context))
            if kind not in PARAMETER_KINDS:
                raise ParameterRegistryError(
                    f"{name}: kind must be one of {sorted(PARAMETER_KINDS)}, got {kind!r}"
                )
            scope = str(_require(entry, "class_scope", context))
            if scope not in CLASS_SCOPES:
                raise ParameterRegistryError(
                    f"{name}: class_scope must be one of {sorted(CLASS_SCOPES)}, "
                    f"got {scope!r}"
                )
            blocking = _require(entry, "formal_blocking", context)
            if not isinstance(blocking, bool):
                raise ParameterRegistryError(
                    f"{name}: formal_blocking must be a boolean, got {blocking!r}"
                )
            if "value" not in entry:
                raise ParameterRegistryError(f"{name}: required key 'value' is missing")

            # calibration_only 必須帶 allowed_range，否則 calibration 的搜尋邊界
            # 就落在實作者手上。允許顯式 null，但那必須是「尚未參數化」的已知缺口。
            if kind == "calibration_only" and "allowed_range" not in entry:
                raise ParameterRegistryError(
                    f"{name}: a calibration_only parameter must declare allowed_range "
                    "(explicit null is allowed only for a not-yet-parameterised gap)"
                )

            parameters.append(
                RegistryParameter(
                    name=name,
                    source=str(entry.get("source", "")),
                    role=str(entry.get("role", "")),
                    value=entry["value"],
                    provenance_status=status,
                    kind=kind,
                    class_scope=scope,
                    formal_blocking=blocking,
                    allowed_range=entry.get("allowed_range"),
                    confounded_with=tuple(entry.get("confounded_with") or ()),
                    note=str(entry.get("note", "")),
                )
            )

        known = {p.name for p in parameters}
        groups: list[ConfoundedGroup] = []
        for index, entry in enumerate(raw.get("confounded_groups") or [], start=1):
            context = f"confounded group #{index}"
            if not isinstance(entry, dict):
                raise ParameterRegistryError(f"{context} must be a mapping")
            group_id = str(_require(entry, "id", context))
            members = tuple(str(m) for m in _require(entry, "members", context))
            unknown = set(members) - known
            if unknown:
                raise ParameterRegistryError(
                    f"{group_id}: member(s) {sorted(unknown)} are not registered "
                    "parameters"
                )
            if len(members) < 2:
                raise ParameterRegistryError(
                    f"{group_id}: a confounded group needs at least two members"
                )
            decision = entry.get("decision")
            if decision is not None:
                _validate_decision(group_id, decision, set(members))
            groups.append(
                ConfoundedGroup(
                    group_id=group_id,
                    physical_quantity=str(entry.get("physical_quantity", "")),
                    members=members,
                    relation=str(entry.get("relation", "")),
                    decision=decision,
                )
            )

        return cls(
            registry_version=version,
            parameters=tuple(parameters),
            groups=tuple(groups),
        )


# ---------------------------------------------------------------------------
# registry 宣稱值 vs 程式碼實際值
# ---------------------------------------------------------------------------
# 一份沒有被拿去對照程式的 registry 只是第二份文件；它會漂移，而且漂移時
# 沒有任何症狀。下面把每個「值住在程式碼裡」的參數綁到實際來源。
#
# 三種綁定狀態必須分開，不得混為一談：
#   code   —— 值在模組常數裡，可靜態比對
#   config —— 值在 scenario yaml 裡，隨 run 而異，靜態比對沒有意義
#   unbound—— 尚未參數化或尚未接線，**這是缺口，不是通過**

_CONFIG_SUPPLIED = frozenset(
    {
        "sample_interval_s",
        "temporal_bins",
        "spp",
        "resolution",
        "lighting.irradiance",
        "medium.turbidity",
        "medium.bubble_density",
        "medium.mist_density",
    }
)


def live_values() -> dict[str, Any]:
    """讀出程式碼裡此刻真正在用的值。

    刻意不 import mitsuba：mitsuba_adapter 的 mitsuba 載入是延遲的，
    只讀模組常數不會把 46 MB 的原生擴充拉進來，本函式因此可在任何測試裡呼叫。
    """
    from pcmef.core.constants import TOF_RECORDING_POINTS
    from pcmef.simulation import mitransient_adapter as mta
    from pcmef.simulation import mitsuba_adapter as ma
    from pcmef.simulation.scenario import Geometry
    from pcmef.surrogate.calibration import PLACEHOLDER_SMOKE_CALIBRATION

    geometry = Geometry()
    calibration = PLACEHOLDER_SMOKE_CALIBRATION

    values: dict[str, Any] = {
        "SPEED_OF_LIGHT_M_PER_S": mta.SPEED_OF_LIGHT_M_PER_S,
        "TOF_RECORDING_POINTS": TOF_RECORDING_POINTS,
        "geometry.sensor_to_bottle_mm": geometry.sensor_to_bottle_mm,
        "geometry.bottle_diameter_mm": geometry.bottle_diameter_mm,
        "geometry.wall_thickness_mm": geometry.wall_thickness_mm,
        "geometry.lateral_offset_mm": geometry.lateral_offset_mm,
        "_INTERIOR_BASE_IOR": ma._INTERIOR_BASE_IOR,
        "_ROOM_LIGHT_RATIO": ma._ROOM_LIGHT_RATIO,
        "_BOTTLE_SURFACE_ALPHA": ma._BOTTLE_SURFACE_ALPHA,
        "_FOIL_GAP_TO_BOTTLE_RATIO": ma._FOIL_GAP_TO_BOTTLE_RATIO,
        "_FOIL_SIZE_TO_DIAMETER_RATIO": ma._FOIL_SIZE_TO_DIAMETER_RATIO,
        "_FOIL_REFLECTANCE_940NM": ma._FOIL_REFLECTANCE_940NM,
        "_FOIL_SURFACE_ALPHA": ma._FOIL_SURFACE_ALPHA,
        "foil_orientation": ma._FOIL_ORIENTATION,
        "_SIGMA_T_REFERENCE_PER_M": ma._SIGMA_T_REFERENCE_PER_M,
        "_ALBEDO_BY_PRESET": ma._ALBEDO_BY_PRESET,
        "sensor.fov_deg": ma._SENSOR_FOV_DEG,
        "light.cutoff_angle_deg": ma._LIGHT_CUTOFF_ANGLE_DEG,
        "max_depth": ma._MAX_DEPTH,
        "_LEADING_MARGIN_BINS": mta._LEADING_MARGIN_BINS,
        "bounce_budget": mta._DEFAULT_BOUNCE_BUDGET,
    }
    for name, scale in calibration.scales().items():
        values[name] = scale.value
    return values


def live_value_drift(registry: ParameterRegistry) -> list[str]:
    """回傳 registry 宣稱值與程式實際值不符的說明；相符時回傳空清單。"""
    actual = live_values()
    drift: list[str] = []
    for parameter in registry.parameters:
        if parameter.name not in actual:
            continue
        declared = canonical_json(parameter.value)
        observed = canonical_json(actual[parameter.name])
        if declared != observed:
            drift.append(
                f"{parameter.name}: registry says {declared}, code uses {observed} "
                f"(source: {parameter.source})"
            )
    return drift


def binding_coverage(registry: ParameterRegistry) -> dict[str, list[str]]:
    """誠實回報綁定涵蓋率：哪些對得上、哪些屬設定檔、哪些根本沒接線。"""
    actual = set(live_values())
    code, config, unbound = [], [], []
    for parameter in registry.parameters:
        if parameter.name in actual:
            code.append(parameter.name)
        elif parameter.name in _CONFIG_SUPPLIED:
            config.append(parameter.name)
        else:
            unbound.append(parameter.name)
    return {
        "code": sorted(code),
        "config_supplied": sorted(config),
        "unbound": sorted(unbound),
    }


# ---------------------------------------------------------------------------
# formal firewall
# ---------------------------------------------------------------------------


def formal_blocking_reasons(
    registry: ParameterRegistry | None = None,
    expected_hash: str | None = None,
    check_live: bool = True,
) -> list[str]:
    """列出**全部**不得進 formal 的理由。沒有理由時回傳空清單。

    刻意一次收齊而不是遇到第一條就返回：只擋一條會讓人以為修掉它就過了，
    而現在的實際狀況是二十幾條（NOTE-030）。
    """
    reasons: list[str] = []
    if registry is None:
        try:
            registry = ParameterRegistry.load()
        except ParameterRegistryError as error:
            return [str(error)]

    reasons.extend(registry.gauge_violations())

    blockers = registry.formal_blockers()
    if blockers:
        reasons.append(
            f"{len(blockers)} formal-blocking parameter(s) are unresolved "
            f"(only {sorted(FORMAL_ELIGIBLE_STATUSES)} may enter formal): "
            + ", ".join(f"{p.name}[{p.provenance_status}]" for p in blockers)
        )

    undecided = [g for g in registry.groups if not g.decided]
    if undecided:
        reasons.append(
            "confounded group(s) with no recorded decision: "
            + ", ".join(g.group_id for g in undecided)
            + " -- each group must record which member is fixed and which are "
            "calibrated, or be explicitly BLOCKED"
        )
    blocked = [g for g in registry.groups if g.status == "BLOCKED"]
    if blocked:
        reasons.append(
            "confounded group(s) explicitly BLOCKED: "
            + ", ".join(
                f"{g.group_id} ({(g.decision or {}).get('blocked_reason', '')})"
                for g in blocked
            )
        )

    if check_live:
        try:
            drift = live_value_drift(registry)
        except AttributeError as error:
            drift = [
                f"a registry parameter is not bound to any code constant: {error}"
            ]
        if drift:
            reasons.append(
                "registry value(s) disagree with the code actually in use: "
                + "; ".join(drift)
            )

    if expected_hash is not None:
        actual_hash = registry.parameter_set_hash()
        if actual_hash != expected_hash:
            reasons.append(
                f"parameter_set_hash mismatch: lock expects {expected_hash}, "
                f"current registry is {actual_hash}. The frozen run used a different "
                "parameter set; open a new run rather than reusing the lock."
            )
    return reasons


def assert_formal_ready(
    registry: ParameterRegistry | None = None,
    expected_hash: str | None = None,
    check_live: bool = True,
) -> None:
    """formal 模式的參數防線。任一條不合即中斷，並列出全部理由。"""
    reasons = formal_blocking_reasons(registry, expected_hash, check_live)
    if reasons:
        raise ParameterRegistryError(
            "the parameter set is not formal-ready:\n  - " + "\n  - ".join(reasons)
        )
