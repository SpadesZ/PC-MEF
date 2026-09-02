# PC-MEF Research System source maintenance contract
# 上下游: 由 experiments.e2_formal、e2_executor_validation 與 cli 的 formal
#         入口呼叫；讀 freeze/ACTIVE_LINEAGE.json 並解析出實際的 freeze 目錄；
#         解析結果與 lock 雜湊由呼叫端寫進 run manifest。
# 檔案路徑: pcmef/core/active_lineage.py
# 產生時間: 2026-09-02 11:40 +08:00
# 版本: v0.1.0
# 功能說明: 回答「這次 formal run 該讀哪一組 lock」，並且在答不出來時
#           拒絕執行，而不是預設回到 freeze/ 根目錄。
# 模組定位: SAI v0.6.0 §18 fail-closed 與 ACC-FML-02（wrong lineage -> refuse）
#           在程式層的落點。**pointer 是 resolver，不是 scientific identity**
#           —— 真正要保存進 run manifest 的是解析出來的目標與其 lock 雜湊。
# 主要責任:
#   1. resolve() 讀取 pointer、解析目錄、驗證預期的 lock 全部存在
#   2. 三種 fail-closed：pointer 不存在、解析目標與預期不符、指向 superseded root
#   3. to_manifest() 產出要寫進 run record 的 resolved lineage 與雜湊
#   4. 讓「新增 PFC-002 之後 pointer 沒更新」變成一個會失敗的狀態
# 維護提醒:
#   - 不得為 freeze_dir 加回預設值。整個模組的存在理由就是「沒有明確
#     lineage 就不准跑」；一個預設值會讓 superseded 的 freeze/ 根目錄
#     在沒有任何錯誤訊息的情況下被讀進 Formal E2。
#   - 不得把 pointer 本身當成 identity 記進 lock 或 run manifest。
#     pointer 會隨新的 corrective run 改變；要記的是它**當下解析到什麼**。
#   - 不得允許 active_freeze_dir 指向 supersedes 清單裡的目錄。
#     那代表 pointer 與它自己的 supersede 宣告互相矛盾。
#   - v0.1.0 新增：首版，對應 P0-1。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_active_lineage.py -v
#   - py -3.10 -m pcmef.cli locks active-lineage
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "ActiveLineageError",
    "ACTIVE_LINEAGE_FILENAME",
    "ACTIVE_LINEAGE_SCHEMA",
    "REQUIRED_FORMAL_LOCKS",
    "ResolvedLineage",
    "resolve_active_lineage",
]

ACTIVE_LINEAGE_FILENAME = "ACTIVE_LINEAGE.json"
ACTIVE_LINEAGE_SCHEMA = "active_lineage_v1"

#: Formal E2 的決策路徑實際會讀到的 lock。解析時全部必須存在。
#:
#: 這不是「全部 22 個 lock」—— 列進來的是**這條執行路徑會讀的**，
#: 少一個就代表指到的 lineage 不完整，那時候失敗遠比讀到一半好。
REQUIRED_FORMAL_LOCKS: tuple[str, ...] = (
    "gate",
    "reliability_final",
    "statistics_config",
    "validation_pool",
    "conflict_operational",
    "e2_sample_size",
    "llm_runtime",
    "agent_schema",
    "inference_firewall",
    "formal_config",
)


class ActiveLineageError(RuntimeError):
    """無法確定該讀哪一組 lock。**一律拒絕執行，不得回退到預設目錄。**"""


@dataclass(frozen=True)
class ResolvedLineage:
    """解析結果。要寫進 run manifest 的是這個，不是 pointer 檔本身。"""

    #: pointer 檔的位置（僅供追溯；它會隨新的 corrective run 改變）
    pointer_path: Path
    #: 解析出來的實際 freeze 目錄
    freeze_dir: Path
    #: pointer 宣告被此 lineage 取代的目錄
    supersedes: tuple[str, ...]
    status: str
    reason: str
    lock_hashes: dict[str, str] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        """寫進 run record 的形狀。

        `resolved_freeze_dir` 與 `lock_hashes` 才是身分證明：pointer 之後
        改指到 PFC-002 時，這份紀錄仍能說明當初實際讀的是哪一組 lock。
        """
        return {
            "schema_version": ACTIVE_LINEAGE_SCHEMA,
            "pointer_path": self.pointer_path.as_posix(),
            "resolved_freeze_dir": self.freeze_dir.as_posix(),
            "supersedes": list(self.supersedes),
            "status": self.status,
            "reason": self.reason,
            "lock_hashes": dict(self.lock_hashes),
            "note": (
                "The pointer is a resolver, not an identity. What identifies this "
                "run is resolved_freeze_dir together with lock_hashes; the pointer "
                "will move when the next corrective lineage is created."
            ),
        }


def _read_pointer(pointer_path: Path) -> Mapping[str, Any]:
    if not pointer_path.exists():
        raise ActiveLineageError(
            f"no active lineage declaration at {pointer_path}. A formal run must "
            "name the lineage it reads; there is deliberately no default, because "
            "defaulting would silently read the superseded root freeze/ directory."
        )
    try:
        document = json.loads(pointer_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ActiveLineageError(
            f"{pointer_path} is not valid JSON: {error}"
        ) from None
    if not isinstance(document, dict):
        raise ActiveLineageError(f"{pointer_path} must contain a JSON object")

    schema = str(document.get("schema_version", ""))
    if schema != ACTIVE_LINEAGE_SCHEMA:
        raise ActiveLineageError(
            f"{pointer_path} declares schema_version={schema!r}; this build "
            f"understands {ACTIVE_LINEAGE_SCHEMA!r} only"
        )
    return document


def resolve_active_lineage(
    root: str | Path = "freeze",
    *,
    expected_freeze_dir: str | Path | None = None,
    required_locks: Sequence[str] = REQUIRED_FORMAL_LOCKS,
) -> ResolvedLineage:
    """讀 pointer、解析目錄、驗證 lock 齊全。任一條不成立即 raise。

    `expected_freeze_dir` 給定時額外比對：呼叫端已經知道自己要讀哪一組，
    解析結果不同就是 identity mismatch，必須拒絕而不是照著 pointer 走。
    """
    from pcmef.core.locks import LockStore

    root_path = Path(root)
    pointer_path = root_path / ACTIVE_LINEAGE_FILENAME
    document = _read_pointer(pointer_path)

    status = str(document.get("status", ""))
    if status != "ACTIVE":
        raise ActiveLineageError(
            f"{pointer_path} declares status={status!r}; a formal run requires "
            "status 'ACTIVE'. A lineage that is not active must not be executed."
        )

    declared = str(document.get("active_freeze_dir", "")).strip()
    if not declared:
        raise ActiveLineageError(
            f"{pointer_path} does not name active_freeze_dir"
        )
    freeze_dir = Path(declared)
    supersedes = tuple(str(item) for item in document.get("supersedes", ()))

    # pointer 不得指向它自己宣告已被取代的目錄。
    superseded_paths = {Path(item).as_posix() for item in supersedes}
    if freeze_dir.as_posix() in superseded_paths:
        raise ActiveLineageError(
            f"{pointer_path} names active_freeze_dir={freeze_dir.as_posix()!r} "
            f"while also listing it in supersedes={sorted(superseded_paths)}. "
            "A lineage cannot supersede itself; one of the two is wrong."
        )

    if not freeze_dir.is_dir():
        raise ActiveLineageError(
            f"active lineage points at {freeze_dir.as_posix()!r}, which is not a "
            "directory. Refusing rather than falling back."
        )

    if expected_freeze_dir is not None:
        expected = Path(expected_freeze_dir)
        if expected.as_posix() != freeze_dir.as_posix():
            raise ActiveLineageError(
                f"lineage mismatch: the caller expects "
                f"{expected.as_posix()!r} but the active lineage resolves to "
                f"{freeze_dir.as_posix()!r}. Refusing to run against a different "
                "set of locks than the one that was asked for."
            )

    store = LockStore(freeze_dir)
    missing = [name for name in required_locks if not store.exists(name)]
    if missing:
        raise ActiveLineageError(
            f"active lineage {freeze_dir.as_posix()!r} is missing required "
            f"lock(s) {missing}. A partial lineage must not be executed: the run "
            "would read some decisions from here and silently need others elsewhere."
        )

    return ResolvedLineage(
        pointer_path=pointer_path,
        freeze_dir=freeze_dir,
        supersedes=supersedes,
        status=status,
        reason=str(document.get("reason", "")),
        lock_hashes={name: store.load_hash(name) for name in required_locks},
    )
