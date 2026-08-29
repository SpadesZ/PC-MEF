# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 erratum freeze / erratum status 呼叫；寫出並讀回
#         freeze/errata/<id>.erratum.json；被 core.formal_loader 在載入任何
#         formal lock 時強制套用與驗證。
# 檔案路徑: pcmef/core/errata.py
# 產生時間: 2026-08-29 10:40 +08:00
# 版本: v0.1.0
# 功能說明: 記錄「已凍結的 lock 有一個**事實錯誤的 metadata 欄位**」——
#           原值是什麼、正確值是什麼、憑什麼說它錯、以及這件事有沒有改變
#           任何科學結論。lock 不可覆寫，因此更正只能以 append-only 的
#           勘誤層存在，原 lock 一個位元都不動。
# 模組定位: 不可覆寫 lock 的**勘誤層**。它「不是」lock，也「不是」amendment：
#           lock 凍結實驗參數，amendment 凍結判準的變更，erratum 只更正
#           **描述執行環境的 metadata 記錄錯誤**。三者混用會讓「改了科學」
#           被藏在「修了筆誤」裡看不出來，因此本模組對可更正的欄位採白名單，
#           且對 parameter / estimator / seed / scene / config 硬性拒絕。
# 主要責任:
#   1. Erratum 描述一次更正的完整內容與其來源證據
#   2. ErratumStore.freeze() 寫入且拒絕覆寫，寫入前跑完整驗證
#   3. verify_erratum() 驗 original lock + erratum + source evidence 三者一致
#   4. CORRECTABLE_PATHS / FORBIDDEN_PATH_PREFIXES 界定可更正範圍
#   5. apply_errata() 產生更正後的 payload，但**不改變 lock 身分雜湊**
# 維護提醒:
#   - 不得允許覆寫已凍結的 erratum；更正錯了就再開一個新 id，
#     否則「後來又偷偷改回去」不會留下痕跡。
#   - 不得放寬 FORBIDDEN_PATH_PREFIXES。它與 CORRECTABLE_PATHS 是**成對鎖**：
#     只把某個欄位加進白名單並不會放行，禁區檢查先跑且不看白名單。
#     一份能改 parameter 的「勘誤」就是在不重跑的情況下改科學。
#   - 不得讓 scientific_state_changed=True 的記錄凍結成 erratum；
#     那代表它根本不是勘誤，該走 amendment 或開新 run。
#   - 不得以 erratum 更正的值取代 lock 的 payload_hash；lock 身分永遠是
#     原始那一份，勘誤只是疊在它上面的一層，兩者必須同時出現在 provenance。
#   - 不得在證據檔不存在或雜湊不符時仍然套用更正；沒有證據的更正
#     與憑記憶改數字無法區分。
#   - v0.1.0 新增：首版勘誤機制，對應 ERR-001（NOTE-040）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_errata.py -v
#   - py -3.10 -m pcmef.cli erratum status
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.core.hash import hash_file, hash_object

__all__ = [
    "ErratumError",
    "ErratumScopeError",
    "EvidenceRef",
    "Erratum",
    "ErratumStore",
    "CORRECTABLE_PATHS",
    "FORBIDDEN_PATH_PREFIXES",
    "verify_erratum",
    "apply_errata",
]


# ---------------------------------------------------------------------------
# 可更正範圍
# ---------------------------------------------------------------------------
# 白名單：只有「描述執行環境的記錄」可以被勘誤。這些欄位不參與任何計算，
# 它們的用途是讓後人**重建環境**；記錯了會讓 lock 的重建指示失效,
# 但改正它不會使任何已算出的數字改變。
CORRECTABLE_PATHS: frozenset[str] = frozenset(
    {
        "environment.mitransient",
        "environment.mitsuba",
        "environment.drjit",
        "environment.python",
        "environment.platform",
        "environment.variant",
    }
)

# 禁區：即使有人把這些路徑加進 CORRECTABLE_PATHS，仍然拒絕。
# 兩層是刻意的 —— 放寬白名單是一行改動，而這一行不該足以讓
# 「改參數」偽裝成「修筆誤」。禁區檢查先跑，且完全不看白名單。
FORBIDDEN_PATH_PREFIXES: tuple[str, ...] = (
    "parameter_ranges",
    "parameter_set_hash",
    "initial_parameter_values",
    "registry",
    "estimator",
    "surrogate",
    "surrogate_hash",
    "scene_hash",
    "scene_topology",
    "code_version",
    "code_dirty_at_freeze",
    "readiness",
    "reproducibility",
    "amendments",
    "claim_boundary",
    "calibration",
    "seed",
    "config",
    "split",
    "objective",
    "optimizer",
)


class ErratumError(RuntimeError):
    """erratum 不存在、格式不符、驗證失敗，或試圖覆寫。"""


class ErratumScopeError(ErratumError):
    """erratum 試圖更正**不屬於 metadata** 的欄位。"""


# ---------------------------------------------------------------------------
# 路徑工具
# ---------------------------------------------------------------------------


def _split_path(path: str) -> list[str]:
    if not isinstance(path, str) or not path.strip():
        raise ErratumError("field_path must be a non-empty dotted string")
    return path.split(".")


def _resolve(payload: Any, path: str) -> Any:
    """依 dotted path 取值。純數字段視為 list index。

    取不到即拋錯，不回傳 None 蒙混 —— 一個解析不到的證據指標若安靜地
    回傳 None，而 corrected_value 剛好也是 None，驗證就會假性通過。
    """
    parts = _split_path(path)
    current = payload
    for index, part in enumerate(parts):
        reached = ".".join(parts[:index]) or "<root>"
        if isinstance(current, list):
            if not part.lstrip("-").isdigit():
                raise ErratumError(
                    f"path {path!r} indexes a list with non-integer segment "
                    f"{part!r} at {reached}"
                )
            position = int(part)
            if not -len(current) <= position < len(current):
                raise ErratumError(
                    f"path {path!r} index {position} is out of range at {reached} "
                    f"(length {len(current)})"
                )
            current = current[position]
            continue
        if not isinstance(current, dict) or part not in current:
            raise ErratumError(
                f"path {path!r} does not resolve in the target document; "
                f"traversal stopped at {reached}"
            )
        current = current[part]
    return current


def _assign(payload: dict[str, Any], path: str, value: Any) -> None:
    parts = _split_path(path)
    current: Any = payload
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value


def assert_path_is_correctable(path: str) -> None:
    """禁區優先，白名單其次。順序有意義，見檔頭維護提醒。"""
    head = _split_path(path)[0]
    for prefix in FORBIDDEN_PATH_PREFIXES:
        if head == prefix or path.startswith(prefix + "."):
            raise ErratumScopeError(
                f"field_path {path!r} is inside the forbidden region {prefix!r}. "
                "An erratum may only correct environment metadata that records how "
                "the frozen run was executed. Parameters, estimator, seeds, scene "
                "and config are the experiment itself: changing them requires a new "
                "run, not a correction note."
            )
    if path not in CORRECTABLE_PATHS:
        raise ErratumScopeError(
            f"field_path {path!r} is not in CORRECTABLE_PATHS. Errata are "
            f"allowlisted; permitted paths are {sorted(CORRECTABLE_PATHS)}."
        )


# ---------------------------------------------------------------------------
# 證據
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """一份支持更正的一手證據。

    `json_pointer` 指向證據檔內**應該等於 corrected_value** 的那個欄位，
    `binds` 則是把這份證據綁回被更正的 lock 的斷言（例如證據的
    content_hash 必須等於 lock 的 scene_hash）。少了 binds，一份任意
    manifest 都能拿來當證據 —— 它證明得了「某次 run 用了 1.3.0」，
    證明不了「**被凍結的那次** run 用了 1.3.0」。
    """

    path: str
    sha256: str
    json_pointer: str
    description: str = ""
    binds: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "json_pointer": self.json_pointer,
            "description": self.description,
            "binds": dict(self.binds),
        }


# ---------------------------------------------------------------------------
# Erratum
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Erratum:
    """一次 metadata 更正的完整記錄。"""

    erratum_id: str
    target_lock: str
    target_payload_hash: str
    field_path: str
    recorded_value: Any
    corrected_value: Any
    defect_class: str
    rationale: str
    evidence: tuple[EvidenceRef, ...]
    authority: str
    scientific_state_changed: bool = False
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def payload(self) -> dict[str, Any]:
        return {
            "erratum_id": self.erratum_id,
            "target_lock": self.target_lock,
            "target_payload_hash": self.target_payload_hash,
            "field_path": self.field_path,
            "recorded_value": self.recorded_value,
            "corrected_value": self.corrected_value,
            "defect_class": self.defect_class,
            "rationale": self.rationale,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "authority": self.authority,
            "scientific_state_changed": self.scientific_state_changed,
            "created_at": self.created_at,
        }

    def to_document(self) -> dict[str, Any]:
        payload = self.payload()
        return {
            "erratum_id": self.erratum_id,
            "payload": payload,
            "payload_hash": hash_object(payload),
            "version": 1,
        }


# ---------------------------------------------------------------------------
# 驗證
# ---------------------------------------------------------------------------


def verify_erratum(
    payload: dict[str, Any],
    lock_payload: dict[str, Any],
    lock_payload_hash: str,
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    """驗 original lock + erratum + source evidence 三者一致。

    任何一項不符即拋錯。回傳可嵌入 provenance 的驗證摘要。
    """
    root = Path(repo_root)
    erratum_id = payload.get("erratum_id", "<unknown>")

    # -- 1. 範疇：禁區優先，白名單其次 -----------------------------------
    field_path = payload.get("field_path")
    if not isinstance(field_path, str):
        raise ErratumError(f"erratum {erratum_id!r} has no field_path")
    assert_path_is_correctable(field_path)

    # -- 2. 科學狀態不得改變 ---------------------------------------------
    if payload.get("scientific_state_changed") is not False:
        raise ErratumScopeError(
            f"erratum {erratum_id!r} declares scientific_state_changed="
            f"{payload.get('scientific_state_changed')!r}. An erratum that changes "
            "the scientific state is not an erratum: open a protocol amendment or "
            "start a new run instead."
        )

    # -- 3. 綁定：必須指名它更正的是**哪一份** lock -----------------------
    recorded_target = payload.get("target_payload_hash")
    if recorded_target != lock_payload_hash:
        raise ErratumError(
            f"erratum {erratum_id!r} targets lock payload_hash {recorded_target!r} "
            f"but the lock on disk hashes to {lock_payload_hash!r}. An erratum is "
            "bound to one exact frozen document; it must never float onto another."
        )

    # -- 4. 原值必須真的在 lock 裡 ----------------------------------------
    actual_recorded = _resolve(lock_payload, field_path)
    if actual_recorded != payload.get("recorded_value"):
        raise ErratumError(
            f"erratum {erratum_id!r} claims the lock records "
            f"{payload.get('recorded_value')!r} at {field_path}, but the lock "
            f"actually records {actual_recorded!r}. The erratum does not describe "
            "this document."
        )

    if payload.get("corrected_value") == actual_recorded:
        raise ErratumError(
            f"erratum {erratum_id!r} corrects {field_path} to the value it already "
            "has; an erratum that changes nothing only adds noise to provenance"
        )

    # -- 5. 證據：存在、雜湊相符、指向的值等於 corrected_value ------------
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ErratumError(
            f"erratum {erratum_id!r} carries no evidence. A correction without a "
            "first-party source is indistinguishable from changing a number from "
            "memory."
        )

    verified: list[dict[str, Any]] = []
    for index, ref in enumerate(evidence):
        location = f"{erratum_id}.evidence[{index}]"
        source = root / str(ref.get("path", ""))
        if not source.exists():
            raise ErratumError(f"{location}: evidence file not found at {source}")

        actual_sha = hash_file(source)
        if actual_sha != ref.get("sha256"):
            raise ErratumError(
                f"{location}: evidence file {source} hashes to {actual_sha}, "
                f"but the erratum records {ref.get('sha256')}. The evidence has "
                "changed since the erratum was frozen."
            )

        document = json.loads(source.read_text(encoding="utf-8"))
        pointed = _resolve(document, str(ref.get("json_pointer", "")))
        if pointed != payload.get("corrected_value"):
            raise ErratumError(
                f"{location}: {ref.get('json_pointer')} in {source} is {pointed!r}, "
                f"but the erratum asserts the corrected value is "
                f"{payload.get('corrected_value')!r}."
            )

        # binds 把證據綁回**這一份** lock，而不只是綁到某次 run。
        for evidence_path, lock_path in dict(ref.get("binds") or {}).items():
            left = _resolve(document, evidence_path)
            right = _resolve(lock_payload, lock_path)
            if left != right:
                raise ErratumError(
                    f"{location}: binding {evidence_path} == lock.{lock_path} "
                    f"failed ({left!r} != {right!r}). This evidence does not come "
                    "from the run that was frozen."
                )

        verified.append(
            {
                "path": str(ref.get("path")),
                "sha256": actual_sha,
                "json_pointer": str(ref.get("json_pointer")),
                "bindings_checked": sorted(dict(ref.get("binds") or {})),
            }
        )

    return {
        "erratum_id": erratum_id,
        "target_lock": payload.get("target_lock"),
        "target_payload_hash": lock_payload_hash,
        "field_path": field_path,
        "recorded_value": payload.get("recorded_value"),
        "corrected_value": payload.get("corrected_value"),
        "scientific_state_changed": False,
        "evidence_verified": verified,
    }


def apply_errata(
    lock_payload: dict[str, Any],
    lock_payload_hash: str,
    errata: list[dict[str, Any]],
    repo_root: str | Path = ".",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """回傳 (更正後 payload, 驗證摘要)。原 payload 不被修改。

    **更正後的 payload 沒有新的身分雜湊。** lock 的身分永遠是原始那一份；
    呼叫端必須同時保存 original hash 與 erratum hash，否則勘誤層會被
    誤當成「一份新的 lock」。
    """
    import copy

    corrected = copy.deepcopy(lock_payload)
    summaries: list[dict[str, Any]] = []
    seen: dict[str, str] = {}

    for payload in errata:
        summary = verify_erratum(payload, lock_payload, lock_payload_hash, repo_root)
        path = summary["field_path"]
        if path in seen:
            raise ErratumError(
                f"two errata correct the same field {path!r} "
                f"({seen[path]} and {summary['erratum_id']}); the resolved value "
                "would depend on application order, which is not a defensible "
                "provenance record"
            )
        seen[path] = str(summary["erratum_id"])
        _assign(corrected, path, payload["corrected_value"])
        summaries.append(summary)

    return corrected, summaries


# ---------------------------------------------------------------------------
# ErratumStore
# ---------------------------------------------------------------------------


class ErratumStore:
    """freeze/errata/ 的讀寫閘門。append-only。"""

    def __init__(self, freeze_dir: str | Path) -> None:
        self.dir = Path(freeze_dir) / "errata"

    def path_for(self, erratum_id: str) -> Path:
        return self.dir / f"{erratum_id}.erratum.json"

    def exists(self, erratum_id: str) -> bool:
        return self.path_for(erratum_id).exists()

    def list_ids(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.name.split(".")[0] for p in self.dir.glob("*.erratum.json"))

    def freeze(
        self,
        erratum: Erratum,
        lock_payload: dict[str, Any],
        lock_payload_hash: str,
        repo_root: str | Path = ".",
    ) -> Path:
        """驗證通過才寫入。已存在即拒絕 —— 勘誤同樣不可覆寫。"""
        path = self.path_for(erratum.erratum_id)
        if path.exists():
            raise ErratumError(
                f"erratum {erratum.erratum_id!r} is already frozen at {path}; "
                "errata are append-only, issue a new erratum id instead"
            )
        # 寫入前先驗，避免留下一份自己都通不過的記錄。
        verify_erratum(erratum.payload(), lock_payload, lock_payload_hash, repo_root)
        self.dir.mkdir(parents=True, exist_ok=True)
        document = erratum.to_document()
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def load(self, erratum_id: str) -> dict[str, Any]:
        """讀回並驗證雜湊。內容被改過即拋錯。"""
        path = self.path_for(erratum_id)
        if not path.exists():
            raise ErratumError(f"erratum {erratum_id!r} is not frozen")
        document = json.loads(path.read_text(encoding="utf-8"))
        payload = document.get("payload")
        if payload is None:
            raise ErratumError(f"erratum {erratum_id!r} has no payload")
        actual = hash_object(payload)
        recorded = document.get("payload_hash")
        if actual != recorded:
            raise ErratumError(
                f"erratum {erratum_id!r} payload_hash mismatch: recorded "
                f"{recorded}, actual {actual}; the record has been modified"
            )
        return payload

    def load_hash(self, erratum_id: str) -> str:
        self.load(erratum_id)
        document = json.loads(self.path_for(erratum_id).read_text(encoding="utf-8"))
        return str(document["payload_hash"])

    def for_lock(self, lock_name: str) -> list[tuple[str, dict[str, Any]]]:
        """取出所有指向某個 lock 的 erratum，依 id 排序。"""
        found: list[tuple[str, dict[str, Any]]] = []
        for erratum_id in self.list_ids():
            payload = self.load(erratum_id)
            if payload.get("target_lock") == lock_name:
                found.append((erratum_id, payload))
        return found
