# PC-MEF Research System source maintenance contract
# 上下游: 呼叫 pcmef.platform.scenarios / sensors 的契約驗證；讀呼叫端明確
#         指定的 manifest 資料夾（只讀 .json）。產出一份凍結的 registry 與
#         被拒絕的清單。**不寫任何檔案，也不匯入任何 plugin 程式碼。**
# 檔案路徑: pcmef/platform/catalog.py
# 產生時間: 2026-09-27 01:38 +08:00（首次提交 d1cf266 的 commit 時間）
# 版本: v0.1.1
# 功能說明: Scenario Plugin 與 Sensor Adapter 的探索：內建的碩論元件，加上
#           明確指定的 manifest 資料夾；任何不合契約、身分衝突、標籤衝突的
#           manifest 都被拒絕並說出原因 —— 不忽略、不退回預設。
# 模組定位: Phase 3 第一片的組裝點。下一片（compatibility matrix）讀的是
#           這裡產出的 registry.describe()，而不是任何寫死的清單。
# 主要責任:
#   1. builtin_components() / builtin_registry()：碩論的四個情境與兩個感測器
#   2. read_manifest()：JSON manifest → 已驗證的元件（重複鍵、NaN 也拒絕）
#   3. discover()：內建 + manifest，決定性、與資料夾順序無關、衝突全拒
#   4. Discovery / Rejection：結果與每一個被拒絕的來源及原因
#   5. verify_implementations()：**另外**、明確地確認宣告的實作真的存在
# 維護提醒:
#   - **不得讓 discover() 匯入任何 plugin 程式碼。** 探索只讀資料；匯入
#     是另一個由呼叫端決定的步驟。否則「程式碼能被匯入」會變成「元件已
#     註冊、可用」，而那正是這一層要分開的兩件事。
#   - 不得讓身分衝突由「誰先被讀到」決定。兩份 manifest 宣告同一個
#     kind + id + version 時**兩份都拒絕** —— 否則結果取決於檔名排序，
#     與匯入順序一樣，是一條看不見的授權路徑。
#   - 不得忽略資料夾裡的非 .json 檔或讀不到的資料夾：它們一律列為拒絕。
#     默默略過一個 `.yaml` manifest，作者會以為它已經註冊了。
#   - 不得在這裡加入任何「找不到就用 PC-MEF 預設」的路徑。
#   - v0.1.1 新增：verify_reference()，一次驗證一個實作指向，供
#     compatibility matrix 分別知道 simulate 與 ingest 是否可用；
#     verify_implementations() 的行為不變。對應 Phase 3 第二片。
#   - v0.1.0 新增：首版，對應 SAI Phase 3 第一片。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_extension_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pcmef.platform import scenarios, sensors
from pcmef.platform.components import (
    KIND_SCENARIO,
    ComponentRegistry,
    ContractViolation,
    DuplicateComponent,
    resolve_entrypoint,
)

__all__ = [
    "Discovery",
    "Rejection",
    "builtin_components",
    "builtin_registry",
    "discover",
    "read_manifest",
    "verify_implementations",
    "verify_reference",
]

_PARSERS = {
    scenarios.CONTRACT: scenarios.parse_scenario,
    sensors.CONTRACT: sensors.parse_sensor,
}


@dataclass(frozen=True)
class Rejection:
    """一個沒有被註冊的來源，以及為什麼。"""

    source: str
    reasons: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        return {"source": self.source, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class Discovery:
    """一次探索的結果：凍結的 registry，與被拒絕的全部來源。"""

    registry: ComponentRegistry
    rejections: tuple[Rejection, ...]

    def raise_for_rejections(self) -> None:
        """嚴格模式：只要有任何一份被拒絕，就整個失敗。"""
        if self.rejections:
            raise ContractViolation(
                "plugin discovery",
                [f"{r.source}: {'; '.join(r.reasons)}" for r in self.rejections],
            )

    def describe(self) -> dict[str, Any]:
        return {
            "registry": self.registry.describe(),
            "rejections": [r.describe() for r in self.rejections],
        }


def builtin_components() -> tuple[Any, ...]:
    """碩論的元件：四個情境（CLASS_ORDER 順序）與 RGB、ToF。每次都重新建立。"""
    return scenarios.thesis_scenarios() + sensors.thesis_sensors()


def builtin_registry() -> ComponentRegistry:
    """只有內建元件的 registry，已凍結。"""
    registry = ComponentRegistry()
    for component in builtin_components():
        registry.register(component)
    registry.freeze()
    return registry


class _ManifestSyntax(ValueError):
    pass


def _refuse_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # JSON 允許重複鍵，而 json.loads 預設「後面的蓋掉前面的」—— 一份
    # manifest 可以在同一個物件裡宣告兩次 capabilities，讀的人只看得到一次。
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise _ManifestSyntax(f"repeats key(s) {repeated} within one object")
    return dict(pairs)


def _refuse_constant(name: str) -> Any:
    raise _ManifestSyntax(f"uses {name}, which is not a JSON number")


def read_manifest(path: str | Path) -> Any:
    """讀一份 JSON manifest 並依它宣告的 contract 驗證。**只讀，不匯入。**"""
    path = Path(path)
    location = path.as_posix()
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractViolation(location, [f"is not UTF-8 ({error.reason})"]) from None
    except OSError as error:
        raise ContractViolation(location, [f"cannot be read ({error})"]) from None
    try:
        data = json.loads(text, object_pairs_hook=_refuse_duplicate_keys,
                          parse_constant=_refuse_constant)
    except _ManifestSyntax as error:
        raise ContractViolation(location, [str(error)]) from None
    except json.JSONDecodeError as error:
        raise ContractViolation(
            location, [f"is not valid JSON ({error.msg}: line {error.lineno}, "
                       f"column {error.colno})"],
        ) from None
    if not isinstance(data, dict):
        raise ContractViolation(location, [f"holds a JSON {type(data).__name__}, "
                                           "not a manifest object"])
    contract = data.get("contract")
    parser = _PARSERS.get(contract)
    if parser is None:
        raise ContractViolation(
            location,
            [f"declares contract {contract!r}; known contracts are {sorted(_PARSERS)}"],
        )
    return parser(data, source="manifest", location=location)


def _read_directories(directories: Iterable[str | Path]):
    candidates, rejections = [], []
    seen: set[Path] = set()
    for directory in directories:
        directory = Path(directory)
        try:
            resolved = directory.resolve()
        except OSError:
            resolved = directory
        if resolved in seen:
            continue  # 同一個資料夾列兩次不是衝突，是同一份來源
        seen.add(resolved)
        if not directory.is_dir():
            rejections.append(Rejection(directory.as_posix(),
                                        ("manifest directory does not exist",)))
            continue
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            if path.name.startswith("."):
                continue
            if path.is_dir() or path.suffix != ".json":
                rejections.append(Rejection(
                    path.as_posix(),
                    ("only .json manifests are read; other entries in a manifest "
                     "directory are reported, not ignored",),
                ))
                continue
            try:
                candidates.append(read_manifest(path))
            except ContractViolation as violation:
                rejections.append(Rejection(path.as_posix(), violation.problems))
    return candidates, rejections


def _conflicts(candidates: list[Any], registry: ComponentRegistry):
    """找出身分衝突與標籤衝突。**衝突的每一方都拒絕。**"""
    reasons: dict[str, list[str]] = {}

    def refuse(component: Any, reason: str) -> None:
        reasons.setdefault(component.provenance.location, []).append(reason)

    by_key: dict[tuple[str, str, str], list[Any]] = {}
    for component in candidates:
        by_key.setdefault(component.identity.key, []).append(component)
    for (kind, component_id, version), group in by_key.items():
        if len(group) > 1:
            where = sorted(c.provenance.location for c in group)
            for component in group:
                refuse(component, f"{kind} {component_id} {version} is declared by "
                                  f"{where}; conflicting identities are all refused")
        elif _registered(registry, group[0]):
            refuse(group[0], f"{kind} {component_id} {version} is already registered")

    by_label: dict[str, set[str]] = {}
    for component in candidates:
        if component.identity.kind == KIND_SCENARIO and component.classification_label:
            by_label.setdefault(component.classification_label, set()).add(
                component.identity.component_id
            )
    for component in candidates:
        if component.identity.kind != KIND_SCENARIO:
            continue
        label = component.classification_label
        if label and len(by_label[label]) > 1:
            refuse(component, f"classification_label {label!r} is claimed by "
                              f"{sorted(by_label[label])}; label collisions are "
                              "all refused")
    return reasons


def _registered(registry: ComponentRegistry, component: Any) -> bool:
    identity = component.identity
    return identity.version in registry.versions(identity.kind, identity.component_id)


def discover(manifest_dirs: Iterable[str | Path] = ()) -> Discovery:
    """內建元件 + 明確指定的 manifest 資料夾 → 凍結的 registry 與拒絕清單。

    **決定性：** 同一組檔案不論資料夾的列出順序、檔案系統的列舉順序，
    結果都相同 —— 衝突的一律全拒，其餘以身分為鍵，與先後無關。
    沒有指定資料夾就只有碩論的內建元件；**沒有任何隱含的搜尋路徑**。
    """
    registry = ComponentRegistry()
    for component in builtin_components():
        registry.register(component)
    candidates, rejections = _read_directories(manifest_dirs)
    refused = _conflicts(candidates, registry)
    for component in sorted(
        candidates,
        key=lambda c: (c.identity.kind, c.identity.component_id,
                       c.identity.version_tuple),
    ):
        location = component.provenance.location
        if location in refused:
            continue
        try:
            registry.register(component)
        except DuplicateComponent as error:  # 防線：上面的衝突檢查漏掉時
            refused.setdefault(location, []).append(str(error))
    for location, reasons in refused.items():
        rejections.append(Rejection(location, tuple(reasons)))
    registry.freeze()
    return Discovery(
        registry,
        tuple(sorted(rejections, key=lambda r: (r.source, r.reasons))),
    )


def verify_implementations(component: Any) -> tuple[str, ...]:
    """**明確地**匯入元件宣告的實作，確認它們存在而且可呼叫。

    探索從不呼叫它。回傳問題清單；空的代表每一個宣告的實作都找得到。
    找得到也**不代表**科學上正確或相容 —— 那是 §28 的 validity review
    與 compatibility matrix 的事。
    """
    if component.identity.kind == KIND_SCENARIO:
        references = {"implementation": component.implementation}
    else:
        references = component.implementations()
    problems = []
    for name, reference in sorted(references.items()):
        if reference is None:
            continue
        problem = verify_reference(reference)
        if problem:
            problems.append(f"{name} {problem}")
    return tuple(problems)


def verify_reference(reference: str) -> str:
    """**明確地**匯入一個 `module:attribute`。空字串代表找得到而且可呼叫。"""
    try:
        target = resolve_entrypoint(reference)
    except Exception as error:  # noqa: BLE001 - 任何匯入失敗都是一個問題
        return f"{reference}: {type(error).__name__}: {error}"
    if not callable(target):
        return f"{reference} is not callable"
    return ""
