# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.platform.scenarios / sensors 定義各自的元件契約時使用，
#         由 pcmef.platform.catalog 組出 registry。不被任何既有 stable module
#         匯入 —— 它是新加的包裝層，不是 scientific core 的一部分。
# 檔案路徑: pcmef/platform/components.py
# 產生時間: 2026-09-27 09:30 +08:00
# 版本: v0.1.1
# 功能說明: Scenario Plugin 與 Sensor Adapter 共用的地基：元件身分與版本、
#           參數規格、嚴格讀取 manifest 欄位、錯誤型別，以及一個決定性、
#           可凍結、重複即拒絕的 registry。
# 模組定位: SAI Phase 3（§37）的第一片。§38.2 的「plugin registry」包裝層；
#           §28 Plugin Governance 要求的 identity / version / author /
#           parameter schema / units / dependency / provenance / known
#           limitations 在這裡成為必填，而不是慣例。
# 主要責任:
#   1. ComponentIdentity：kind + id + semver + author，registry 的唯一鍵
#   2. ParameterSpec / parse_parameters()：參數規格，數值型一律要有單位
#   3. Fields：嚴格讀取一個 mapping —— 缺的、型別錯的、多出來的都算錯
#   4. ComponentRegistry：register / get / versions / components / describe
#   5. ContractViolation / DuplicateComponent / UnknownComponent /
#      AmbiguousVersion / RegistryFrozen：每一種失敗都有名字，沒有退回預設
# 維護提醒:
#   - **不得在這個模組放任何模組層級的 registry。** 「匯入即註冊」讓結果
#     取決於誰先匯入了誰（§49.3 已經被咬過一次）；registry 一律由
#     catalog.discover() 明確組出來，每次呼叫都是新的一份。
#   - 不得讓 get() 在找不到或有多個版本時回傳任何「預設」元件。找不到就是
#     UnknownComponent，多個版本就是 AmbiguousVersion —— 退回 PC-MEF 的
#     預設值，等於讓一個打錯字的 id 默默跑成碩論的場景或感測器。
#   - 不得讓 register() 覆蓋既有的鍵。同一個 kind + id + version 只能有一份。
#   - 不得把「註冊成功」寫成「科學上相容」。registry 只斷言契約形狀正確；
#     相容性是 §10 compatibility matrix 另外的判斷（describe() 會明說）。
#   - v0.1.1 修正：參數的 minimum / maximum 必須是有限值（JSON 的 1e999
#     會被讀成 inf）。對應 Phase 3 第二片。
#   - v0.1.0 新增：首版，對應 SAI Phase 3 第一片（registry / contract 基礎）。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_extension_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import importlib
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "KIND_SCENARIO",
    "KIND_SENSOR",
    "KINDS",
    "NOT_ASSESSED",
    "RESERVED_NAMESPACE",
    "AmbiguousVersion",
    "ComponentIdentity",
    "ComponentRegistry",
    "ContractViolation",
    "DuplicateComponent",
    "Fields",
    "ParameterSpec",
    "Provenance",
    "RegistryFrozen",
    "UnknownComponent",
    "check_entrypoint",
    "content_sha256",
    "parse_parameters",
    "read_identity",
    "resolve_entrypoint",
    "valid_version",
]

KIND_SCENARIO = "scenario_plugin"
KIND_SENSOR = "sensor_adapter"
KINDS: tuple[str, ...] = (KIND_SCENARIO, KIND_SENSOR)

#: 內建元件保留的命名空間。**manifest 不得使用。**
#:
#: 少了這一條，一份丟進 plugin 資料夾的檔案就能以碩論元件的名字出現 ——
#: 甚至以更高的版本號，讓人以為那是碩論感測器的新版（Appendix A 第 1、3 條）。
RESERVED_NAMESPACE = "pcmef."

#: `acme.ir-camera`：小寫、至少一個分隔符（命名空間）、不得有空白。
_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
#: 嚴格的 MAJOR.MINOR.PATCH。`1.0`、`v1`、`01.0.0` 都不收：版本是 registry
#: 的鍵，同一個版本不得有兩種寫法。
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
#: `package.module:attribute`。
_ENTRYPOINT = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$"
)
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

PARAMETER_TYPES: tuple[str, ...] = ("number", "integer", "string", "boolean")

#: 每一個元件 describe() 裡都帶著這一句：**存在不等於相容。**
NOT_ASSESSED: dict[str, Any] = {
    "assessed": False,
    "note": (
        "Registration asserts only that this component's contract is well "
        "formed. Whether it is scientifically compatible with any scenario, "
        "sensor or profile is a separate claim, made by the compatibility "
        "matrix (SAI §10), not by the registry."
    ),
}


class ContractViolation(ValueError):
    """一份元件定義不符合契約。列出**全部**問題，不是只列第一個。"""

    def __init__(self, source: str, problems: Iterable[str]) -> None:
        self.source = source
        self.problems = tuple(problems)
        super().__init__(
            f"{source} does not satisfy its contract: " + "; ".join(self.problems)
        )


class DuplicateComponent(ValueError):
    """同一個 kind + id + version 已經註冊過。"""


class UnknownComponent(LookupError):
    """沒有這個元件。**不會**退回任何預設元件。"""


class AmbiguousVersion(LookupError):
    """同一個 id 有多個版本，而呼叫端沒有指定要哪一個。"""


class RegistryFrozen(RuntimeError):
    """registry 已經凍結，不再接受註冊。"""


@dataclass(frozen=True)
class ComponentIdentity:
    """一個元件是誰。**kind + id + version 是 registry 的唯一鍵。**"""

    kind: str
    component_id: str
    version: str
    author: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.component_id, self.version)

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        major, minor, patch = self.version.split(".")
        return (int(major), int(minor), int(patch))

    def describe(self) -> dict[str, str]:
        return {
            "kind": self.kind, "id": self.component_id,
            "version": self.version, "author": self.author,
        }


@dataclass(frozen=True)
class Provenance:
    """這份定義從哪裡來，以及它的內容指紋。

    `sha256` 取的是**正規化後**的定義（排序鍵、固定分隔符），因此同一份
    內容不論縮排或鍵的順序，指紋都相同；內容一改，指紋就變。
    """

    source: str          # "builtin" 或 "manifest"
    location: str        # 內建：產生它的函式；manifest：檔案路徑
    sha256: str

    def describe(self) -> dict[str, str]:
        return {"source": self.source, "location": self.location, "sha256": self.sha256}


def content_sha256(data: Mapping[str, Any]) -> str:
    import hashlib
    import json

    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParameterSpec:
    """一個可設定的參數。數值型**一定**有單位；無因次寫 `"1"`。"""

    name: str
    type: str
    unit: str | None
    required: bool
    minimum: float | None
    maximum: float | None
    description: str

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "type": self.type, "unit": self.unit,
            "required": self.required, "minimum": self.minimum,
            "maximum": self.maximum, "description": self.description,
        }


def _is_number(value: Any) -> bool:
    # bool 是 int 的子類別：`"minimum": true` 不得被當成 1。
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Fields:
    """嚴格讀取一個 mapping：缺的、型別錯的、**多出來的**都記成問題。

    多出來的也要算：`"capabilites"` 這種拼錯的鍵若被默默忽略，宣告的能力
    就等於沒有宣告，而作者以為有。
    """

    def __init__(self, data: Any, where: str, problems: list[str]) -> None:
        self.where = where
        self.problems = problems
        if not isinstance(data, Mapping):
            problems.append(f"{where} must be an object, got {type(data).__name__}")
            data = {}
        self._data = data
        self._seen: set[str] = set()

    def _type_name(self, types: tuple[type, ...]) -> str:
        return " or ".join("null" if t is type(None) else t.__name__ for t in types)

    def take(self, key: str, types: tuple[type, ...], *, required: bool = True,
             default: Any = None) -> Any:
        self._seen.add(key)
        if key not in self._data:
            if required:
                self.problems.append(f"{self.where} is missing {key!r}")
            return default
        value = self._data[key]
        bool_ok = bool in types
        if isinstance(value, bool) and not bool_ok:
            self.problems.append(
                f"{self.where}.{key} is a bool, expected {self._type_name(types)}"
            )
            return default
        if not isinstance(value, types):
            self.problems.append(
                f"{self.where}.{key} is a {type(value).__name__}, expected "
                f"{self._type_name(types)}"
            )
            return default
        return value

    def text(self, key: str, *, required: bool = True, allow_empty: bool = False) -> str:
        value = self.take(key, (str,), required=required, default=None)
        if value is None:
            return ""
        if not value and not allow_empty:
            self.problems.append(f"{self.where}.{key} must not be empty")
        return value

    def strings(self, key: str, *, required: bool = True) -> tuple[str, ...]:
        value = self.take(key, (list,), required=required, default=[])
        if not all(isinstance(item, str) and item for item in value):
            self.problems.append(f"{self.where}.{key} must be a list of non-empty strings")
            return ()
        return tuple(value)

    def done(self) -> None:
        extra = sorted(set(self._data) - self._seen)
        if extra:
            self.problems.append(
                f"{self.where} has unknown field(s) {extra}; unknown fields are "
                "refused rather than ignored"
            )


def read_identity(fields: Fields, kind: str, *, builtin: bool) -> ComponentIdentity:
    """讀出 id / version / author 並驗證格式與命名空間。"""
    component_id = fields.text("id")
    version = fields.text("version")
    author = fields.text("author")
    if component_id and not _ID.match(component_id):
        fields.problems.append(
            f"id {component_id!r} must be lowercase and namespaced, e.g. 'acme.ir-camera'"
        )
    if component_id.startswith(RESERVED_NAMESPACE) and not builtin:
        fields.problems.append(
            f"id {component_id!r} uses the reserved namespace {RESERVED_NAMESPACE!r}; "
            "only the built-in thesis components may use it"
        )
    if not component_id.startswith(RESERVED_NAMESPACE) and builtin:
        fields.problems.append(
            f"built-in id {component_id!r} must use the {RESERVED_NAMESPACE!r} namespace"
        )
    if version and not _VERSION.match(version):
        fields.problems.append(
            f"version {version!r} must be MAJOR.MINOR.PATCH, e.g. '1.0.0'"
        )
    return ComponentIdentity(kind, component_id, version, author)


def valid_version(text: str) -> bool:
    """元件版本與觀測 schema 版本共用同一條規則。"""
    return bool(_VERSION.match(text))


def check_entrypoint(value: str | None, where: str, problems: list[str]) -> None:
    if value is not None and not _ENTRYPOINT.match(value):
        problems.append(
            f"{where} {value!r} must be 'package.module:attribute'"
        )


def parse_parameters(data: Any, where: str,
                     problems: list[str]) -> tuple[ParameterSpec, ...]:
    """參數規格：名稱 → {type, unit, required, minimum, maximum, description}。"""
    if not isinstance(data, Mapping):
        problems.append(f"{where} must be an object of parameter specs")
        return ()
    specs = []
    for name in sorted(data):
        at = f"{where}.{name}"
        if not isinstance(name, str) or not _NAME.match(name):
            problems.append(f"{at}: parameter names must be lowercase identifiers")
            continue
        fields = Fields(data[name], at, problems)
        kind = fields.text("type")
        unit = fields.take("unit", (str, type(None)), required=False, default=None)
        required = fields.take("required", (bool,), required=False, default=False)
        minimum = fields.take("minimum", (int, float, type(None)), required=False)
        maximum = fields.take("maximum", (int, float, type(None)), required=False)
        description = fields.take("description", (str,), required=False, default="")
        fields.done()
        if kind and kind not in PARAMETER_TYPES:
            problems.append(f"{at}.type {kind!r} is not one of {list(PARAMETER_TYPES)}")
        numeric = kind in ("number", "integer")
        if numeric and not unit:
            problems.append(
                f"{at} is numeric and must state its unit explicitly "
                "(use \"1\" for dimensionless)"
            )
        if not numeric and (minimum is not None or maximum is not None):
            problems.append(f"{at}: minimum / maximum only apply to numeric parameters")
        for bound, value in (("minimum", minimum), ("maximum", maximum)):
            if _is_number(value) and not math.isfinite(value):
                problems.append(f"{at}.{bound} must be a finite number")
        if _is_number(minimum) and _is_number(maximum) and minimum > maximum:
            problems.append(f"{at}: minimum {minimum} exceeds maximum {maximum}")
        specs.append(ParameterSpec(name, kind, unit, required, minimum, maximum,
                                   description))
    return tuple(specs)


def resolve_entrypoint(reference: str) -> Any:
    """**明確地**匯入一個 `module:attribute`。註冊與探索都不呼叫它。

    把「程式碼存在」與「元件已註冊」分開：探索只讀 manifest，不匯入任何
    東西；要確認宣告的實作真的在，是另外一個、呼叫端自己決定要不要做的
    步驟（catalog.verify_implementations）。
    """
    module_name, _, attribute = reference.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in attribute.split("."):
        target = getattr(target, part)
    return target


class ComponentRegistry:
    """元件登記處。鍵是 kind + id + version；**重複即拒絕，凍結後不收。**

    列舉一律依 (id, 版本) 排序，與註冊順序無關 —— 同一組元件，不論從
    哪個資料夾、以什麼順序讀到，看到的都是同一份清單。

    **這份清單的順序不是類別順序。** 碩論的 class order 由
    core.constants.CLASS_ORDER 決定（Appendix A 第 4 條），不得由這裡推導。
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], Any] = {}
        self._frozen = False

    def register(self, component: Any) -> None:
        if self._frozen:
            raise RegistryFrozen(
                "this registry is frozen; build a new one with catalog.discover()"
            )
        identity: ComponentIdentity = component.identity
        if identity.kind not in KINDS:
            raise ContractViolation(identity.component_id,
                                    [f"unknown kind {identity.kind!r}"])
        if identity.key in self._items:
            raise DuplicateComponent(
                f"{identity.kind} {identity.component_id} {identity.version} is "
                "already registered; a registered identity is never replaced"
            )
        self._items[identity.key] = component

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    def _require_kind(self, kind: str) -> None:
        if kind not in KINDS:
            raise UnknownComponent(f"unknown component kind {kind!r}; expected {list(KINDS)}")

    def versions(self, kind: str, component_id: str) -> tuple[str, ...]:
        self._require_kind(kind)
        found = [c.identity for (k, i, _v), c in self._items.items()
                 if k == kind and i == component_id]
        return tuple(i.version for i in sorted(found, key=lambda i: i.version_tuple))

    def get(self, kind: str, component_id: str, version: str | None = None) -> Any:
        """取出一個元件。**找不到或不明確時一律拋出，不回傳預設。**"""
        self._require_kind(kind)
        if version is not None:
            component = self._items.get((kind, component_id, version))
            if component is None:
                raise UnknownComponent(
                    f"no {kind} {component_id} {version} is registered"
                )
            return component
        versions = self.versions(kind, component_id)
        if not versions:
            raise UnknownComponent(f"no {kind} named {component_id!r} is registered")
        if len(versions) > 1:
            raise AmbiguousVersion(
                f"{kind} {component_id!r} is registered in versions {list(versions)}; "
                "name the version explicitly — the registry never picks one"
            )
        return self._items[(kind, component_id, versions[0])]

    def components(self, kind: str) -> tuple[Any, ...]:
        self._require_kind(kind)
        chosen = [c for (k, _i, _v), c in self._items.items() if k == kind]
        return tuple(sorted(
            chosen,
            key=lambda c: (c.identity.component_id, c.identity.version_tuple),
        ))

    def describe(self) -> dict[str, Any]:
        """結構化 metadata，給下一片（compatibility matrix）使用。可 JSON 序列化。"""
        return {
            "frozen": self._frozen,
            KIND_SCENARIO: [c.describe() for c in self.components(KIND_SCENARIO)],
            KIND_SENSOR: [c.describe() for c in self.components(KIND_SENSOR)],
        }
