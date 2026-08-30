# PC-MEF Research System source maintenance contract
# 上下游: 讀 data/splits/split_registry.json 的 heldout_real **指派**（不讀值）；
#         寫出 freeze/heldout_partition.lock.json 與
#         data/splits/heldout_probe_access_ledger.json；
#         由 cli 的 `split partition-heldout` 凍結，之後由 E1 的開發探針與
#         最終評估各自取用。
# 檔案路徑: pcmef/core/heldout_partition.py
# 產生時間: 2026-08-30 22:40 +08:00
# 版本: v0.1.0
# 功能說明: 依 AMD-005 把 168 筆 heldout_real 在**讀值之前**切成
#           E1_DEVELOPMENT_PROBE（14/class = 56）與 FORMAL_E1_FINAL
#           （28/class = 112），凍結 ID 後才允許任何一側被開啟。
# 模組定位: protected-final-test 設計的分界線。probe 可以在方法開發期被看，
#           final 不行 —— 而這個區別只有在「ID 先於值被凍結」時才成立，
#           否則就變成看過資料之後才決定哪些算最終測試。
# 主要責任:
#   1. plan_partition() 以固定種子做逐類分層抽取，只碰 ID
#   2. freeze_partition() 寫出 lock；已存在即拒絕覆寫
#   3. load_partition() 讀回並驗證 payload hash 與計數
#   4. probe_ids() 取用開發探針，強制記帳
#   5. final_ids() 取用最終測試，要求校準模擬器已凍結且只准一次
# 維護提醒:
#   - 不得在凍結 ID 之前讀取任何一筆 heldout 的值。先看值再決定分割，
#     等於讓最終測試集是挑出來的。
#   - 不得用 probe 的結果去挑參數或改判準；它只是 sanity check。
#     AMD-005 明列「不得對最終測試的逐類均值調參」。
#   - 不得在 calibrated_simulation 凍結之前呼叫 final_ids()；那道前提是
#     protected-final-test 設計唯一的實質保護。
#   - 不得把 probe access 記進 split_registry.heldout_access_count。
#     AMD-005 之後那個計數專指 FORMAL_E1_FINAL，混用會讓它失去意義。
#   - v0.1.0 新增：AMD-005 的 heldout 二次分割。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_heldout_partition.py -v
#   - py -3.10 -m pcmef.cli split partition-heldout --show
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from pcmef.core.constants import CLASS_ORDER
from pcmef.core.hash import hash_object

__all__ = [
    "PARTITION_ID",
    "PARTITION_SEED",
    "PROBE_PER_CLASS",
    "FINAL_PER_CLASS",
    "PROBE_ROLE",
    "FINAL_ROLE",
    "HeldoutPartitionError",
    "plan_partition",
    "freeze_partition",
    "load_partition",
    "probe_ids",
    "final_ids",
    "probe_access_count",
]

PARTITION_ID = "HELDOUT-PART-001"

#: AMD-005 事前選定。與 real split 的 20260826 不同號，因此兩次抽取
#: 不會因為共用種子而產生相關的順序。
PARTITION_SEED = 20260830

PROBE_PER_CLASS = 14
FINAL_PER_CLASS = 28

PROBE_ROLE = "E1_DEVELOPMENT_PROBE"
FINAL_ROLE = "FORMAL_E1_FINAL"

_LOCK_NAME = "heldout_partition.lock.json"
_PROBE_LEDGER = "heldout_probe_access_ledger.json"


class HeldoutPartitionError(RuntimeError):
    """分割不成立，或在不允許的前提下取用某一側。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


# ---------------------------------------------------------------------------
# 規劃（只碰 ID）
# ---------------------------------------------------------------------------


def _heldout_by_class(repo_root: str | Path) -> dict[str, list[str]]:
    """heldout_real 的 measurement key，逐類分組並排序。

    只讀 split_registry 的**指派**。依 AMD-003 的 access 定義，讀 ID 集合
    不是一次 access —— 它是 split 結構，不揭露任何分佈資訊。
    """
    from pcmef.adapters.edge_impulse import EI_LABEL_TO_CLASS

    registry = json.loads(
        (Path(repo_root) / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    by_class: dict[str, list[str]] = {c: [] for c in CLASS_ORDER}
    for key, role in registry["assignments"].items():
        if role != "heldout_real":
            continue
        label = key.split("/", 1)[0]
        class_label = EI_LABEL_TO_CLASS.get(label)
        if class_label is None:
            raise HeldoutPartitionError(
                "SPLIT_REGISTRY_INCONSISTENT",
                f"label {label!r} is not in EI_LABEL_TO_CLASS",
            )
        by_class[class_label].append(key)
    return {c: sorted(ids) for c, ids in by_class.items()}


def plan_partition(repo_root: str | Path = ".") -> dict[str, Any]:
    """逐類分層抽取，產生 probe / final 的 ID 分割。

    每類一條獨立的 rng，且以 class 名稱與筆數參與 seeding —— 與
    `plan_real_split` 同一個理由：共用一條 rng 會讓某一類的筆數變動
    改變其他類的抽取結果。
    """
    by_class = _heldout_by_class(repo_root)
    probe: dict[str, list[str]] = {}
    final: dict[str, list[str]] = {}

    for class_label in CLASS_ORDER:
        ids = by_class[class_label]
        needed = PROBE_PER_CLASS + FINAL_PER_CLASS
        if len(ids) != needed:
            raise HeldoutPartitionError(
                "COUNT_MISMATCH",
                f"{class_label} has {len(ids)} held-out recordings but AMD-005 "
                f"splits {PROBE_PER_CLASS} + {FINAL_PER_CLASS} = {needed}",
            )
        rng = np.random.default_rng(
            [PARTITION_SEED, sum(ord(c) for c in class_label), len(ids)]
        )
        permuted = [str(i) for i in rng.permutation(np.asarray(ids, dtype=object))]
        probe[class_label] = sorted(permuted[:PROBE_PER_CLASS])
        final[class_label] = sorted(permuted[PROBE_PER_CLASS:])

    overlap = set().union(*probe.values()) & set().union(*final.values())
    if overlap:
        raise HeldoutPartitionError(
            "OVERLAP", f"{len(overlap)} id(s) landed in both subsets: {sorted(overlap)[:5]}"
        )
    return {"probe": probe, "final": final}


def _set_hash(mapping: dict[str, list[str]]) -> str:
    return hash_object({k: sorted(v) for k, v in sorted(mapping.items())})


# ---------------------------------------------------------------------------
# 凍結
# ---------------------------------------------------------------------------


def freeze_partition(
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    code_version: str = "",
    amendment_hash: str = "",
) -> tuple[Path, dict[str, Any]]:
    """凍結 ID 分割。已存在即拒絕 —— 分割不可重抽。"""
    target = Path(freeze_dir) / _LOCK_NAME
    if target.exists():
        raise HeldoutPartitionError(
            "ALREADY_FROZEN",
            f"{PARTITION_ID} is already frozen at {target}. Re-drawing the split "
            "after any value has been seen would make the final test set a chosen one.",
        )

    registry = json.loads(
        (Path(repo_root) / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    if int(registry["heldout_access_count"]) != 0:
        raise HeldoutPartitionError(
            "HELDOUT_ALREADY_READ",
            f"heldout_access_count is {registry['heldout_access_count']}, not 0. The "
            "partition must be frozen BEFORE any held-out value is read.",
        )

    plan = plan_partition(repo_root)
    payload = {
        "partition_id": PARTITION_ID,
        "amendment_id": "AMD-005",
        "amendment_hash": amendment_hash,
        "seed": PARTITION_SEED,
        "selection": "deterministic class-stratified permutation, one rng per class",
        "unit": "recording",
        "source_role": "heldout_real",
        "source_set_hash": registry["heldout_real_set_hash"],
        "counts": {
            PROBE_ROLE: {c: len(plan["probe"][c]) for c in CLASS_ORDER},
            FINAL_ROLE: {c: len(plan["final"][c]) for c in CLASS_ORDER},
        },
        "totals": {
            PROBE_ROLE: sum(len(v) for v in plan["probe"].values()),
            FINAL_ROLE: sum(len(v) for v in plan["final"].values()),
        },
        "ids": {PROBE_ROLE: plan["probe"], FINAL_ROLE: plan["final"]},
        "set_hashes": {
            PROBE_ROLE: _set_hash(plan["probe"]),
            FINAL_ROLE: _set_hash(plan["final"]),
        },
        "frozen_before_any_heldout_value_was_read": True,
        "heldout_access_count_at_freeze": 0,
        "code_version": code_version,
        "claim_boundary": (
            "This record freezes recording IDs only. No held-out value was read to "
            "produce it. E1_DEVELOPMENT_PROBE may be inspected during method "
            "development with every access logged; FORMAL_E1_FINAL must stay sealed "
            "until the calibrated simulator and all gate rules are frozen."
        ),
    }
    document = {
        "partition_id": PARTITION_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload_hash": hash_object(payload),
        "payload": payload,
        "version": 1,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _ensure_probe_ledger(repo_root)
    return target, document


def load_partition(
    freeze_dir: str | Path = "freeze", repo_root: str | Path = "."
) -> dict[str, Any]:
    """讀回並驗證。內容被改過、或計數不符即拒絕。"""
    target = Path(freeze_dir) / _LOCK_NAME
    if not target.exists():
        raise HeldoutPartitionError(
            "NOT_FROZEN",
            f"{PARTITION_ID} is not frozen; run `split partition-heldout --freeze` "
            "before touching either subset",
        )
    document = json.loads(target.read_text(encoding="utf-8"))
    payload = document["payload"]
    if hash_object(payload) != document["payload_hash"]:
        raise HeldoutPartitionError(
            "PARTITION_DRIFT", f"{PARTITION_ID} payload hash does not verify"
        )
    for role, per_class in (
        (PROBE_ROLE, PROBE_PER_CLASS),
        (FINAL_ROLE, FINAL_PER_CLASS),
    ):
        for class_label in CLASS_ORDER:
            actual = len(payload["ids"][role][class_label])
            if actual != per_class:
                raise HeldoutPartitionError(
                    "PARTITION_DRIFT",
                    f"{role}/{class_label} holds {actual} ids, expected {per_class}",
                )
    return document


# ---------------------------------------------------------------------------
# 取用
# ---------------------------------------------------------------------------


def _ensure_probe_ledger(repo_root: str | Path) -> Path:
    path = Path(repo_root) / "data" / "splits" / _PROBE_LEDGER
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "ledger": "e1_development_probe_access",
                    "amendment_id": "AMD-005",
                    "probe_access_count": 0,
                    "entries": [],
                    "definition": (
                        "One access = reading the VALUE of any recording in "
                        "E1_DEVELOPMENT_PROBE. Reading the frozen id set does not "
                        "count. FORMAL_E1_FINAL accesses are NOT recorded here; they "
                        "increment split_registry.heldout_access_count."
                    ),
                    "note": (
                        "Append-only. Probe values may be inspected during method "
                        "development, but every inspection leaves a record."
                    ),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return path


def probe_access_count(repo_root: str | Path = ".") -> int:
    path = Path(repo_root) / "data" / "splits" / _PROBE_LEDGER
    if not path.exists():
        return 0
    return int(json.loads(path.read_text(encoding="utf-8"))["probe_access_count"])


def _append_probe_ledger(
    purpose: str, code_version: str, repo_root: str | Path, extra: dict[str, Any]
) -> dict[str, Any]:
    path = _ensure_probe_ledger(repo_root)
    ledger = json.loads(path.read_text(encoding="utf-8"))
    entry = {
        "index": int(ledger["probe_access_count"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "purpose": purpose,
        "code_version": code_version,
        "partition": PROBE_ROLE,
        "reads_formal_e1_final": False,
        **extra,
    }
    ledger["entries"].append(entry)
    ledger["probe_access_count"] = entry["index"]

    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(ledger, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    return entry


def probe_ids(
    purpose: str,
    code_version: str = "",
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """取用 E1_DEVELOPMENT_PROBE 的 ID，並記一次帳。

    記帳發生在**交出 ID 之前**：帳本是「探針被看過幾次」的唯一憑據。
    """
    document = load_partition(freeze_dir, repo_root)
    entry = _append_probe_ledger(
        purpose,
        code_version,
        repo_root,
        {
            "recordings": document["payload"]["totals"][PROBE_ROLE],
            "partition_hash": document["payload_hash"],
            "set_hash": document["payload"]["set_hashes"][PROBE_ROLE],
        },
    )
    ids = {c: list(v) for c, v in document["payload"]["ids"][PROBE_ROLE].items()}
    return ids, entry


def final_ids(
    purpose: str,
    calibrated_lock_hash: str,
    code_version: str = "",
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """取用 FORMAL_E1_FINAL 的 ID。**只准一次，且必須在凍結之後。**

    兩道前提缺一不可：
      * `calibrated_simulation` 必須已凍結 —— 否則「最終測試」只是另一次
        可以回頭調參的評估；
      * `heldout_access_count` 必須仍為 0 —— 開過就不是最終測試了。
    """
    document = load_partition(freeze_dir, repo_root)

    lock_path = Path(freeze_dir) / "calibrated_simulation.lock.json"
    if not lock_path.exists():
        raise HeldoutPartitionError(
            "CALIBRATION_NOT_FROZEN",
            "FORMAL_E1_FINAL must not be opened before "
            f"{lock_path} exists. Until the simulator is frozen, a 'final' "
            "evaluation is just another one you can go back and tune against.",
        )
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if not calibrated_lock_hash or lock["payload_hash"] != calibrated_lock_hash:
        raise HeldoutPartitionError(
            "CALIBRATION_NOT_FROZEN",
            f"calibrated_simulation.lock hashes to {lock['payload_hash']} but the "
            f"caller declared {calibrated_lock_hash!r}",
        )

    registry_path = Path(repo_root) / "data" / "splits" / "split_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if int(registry["heldout_access_count"]) != 0:
        raise HeldoutPartitionError(
            "FINAL_ALREADY_OPENED",
            f"heldout_access_count is {registry['heldout_access_count']}, not 0. "
            "FORMAL_E1_FINAL is opened exactly once; a second opening is not a "
            "final evaluation.",
        )

    entry = {
        "index": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "purpose": purpose,
        "code_version": code_version,
        "partition": FINAL_ROLE,
        "recordings": document["payload"]["totals"][FINAL_ROLE],
        "partition_hash": document["payload_hash"],
        "set_hash": document["payload"]["set_hashes"][FINAL_ROLE],
        "calibrated_simulation_lock_hash": calibrated_lock_hash,
    }
    registry["heldout_access_count"] = 1
    registry.setdefault("heldout_final_access_entries", []).append(entry)

    handle, tmp = tempfile.mkstemp(dir=str(registry_path.parent), suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(registry, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, registry_path)

    ids = {c: list(v) for c, v in document["payload"]["ids"][FINAL_ROLE].items()}
    return ids, entry


def resume_final_ids(
    purpose: str,
    calibrated_lock_hash: str,
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """接續一次**已經記錄在案**的最終取用，計數不再遞增。

    存在的理由：最終評估在開啟資料之後、產出結果之前可能因為實作缺陷崩潰。
    那次取用真的發生了，帳也記了，因此正確處置既不是假裝沒發生（重置計數
    等於竄改帳本），也不是再記一次（那會宣稱開了兩次）。而是**完成**那一次
    取用所授權的評估。

    三項身分必須逐字相符才准接續：calibrated_simulation 的 lock hash、
    partition hash 與 set hash。任何一項不同就代表這不是同一次取用 ——
    例如模擬器在崩潰之後被重新校準過，那麼繼續下去就是拿新模型去用舊授權。
    """
    document = load_partition(freeze_dir, repo_root)
    registry_path = Path(repo_root) / "data" / "splits" / "split_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    count = int(registry["heldout_access_count"])
    if count != 1:
        raise HeldoutPartitionError(
            "NO_RECORDED_ACCESS_TO_RESUME",
            f"heldout_access_count is {count}, not 1; there is no single recorded "
            "final access to continue",
        )
    entries = registry.get("heldout_final_access_entries") or []
    if len(entries) != 1:
        raise HeldoutPartitionError(
            "NO_RECORDED_ACCESS_TO_RESUME",
            f"expected exactly one recorded final access entry, found {len(entries)}",
        )
    entry = entries[0]

    expected = {
        "calibrated_simulation_lock_hash": calibrated_lock_hash,
        "partition_hash": document["payload_hash"],
        "set_hash": document["payload"]["set_hashes"][FINAL_ROLE],
        "purpose": purpose,
    }
    drifted = {
        key: (entry.get(key), value)
        for key, value in expected.items()
        if entry.get(key) != value
    }
    if drifted:
        raise HeldoutPartitionError(
            "RESUMED_ACCESS_IDENTITY_MISMATCH",
            "the recorded final access does not describe this evaluation: "
            + "; ".join(
                f"{key} recorded {was!r} but now {now!r}"
                for key, (was, now) in sorted(drifted.items())
            )
            + ". Continuing would use a new model under an old authorisation.",
        )

    ids = {c: list(v) for c, v in document["payload"]["ids"][FINAL_ROLE].items()}
    return ids, {**entry, "resumed": True}
