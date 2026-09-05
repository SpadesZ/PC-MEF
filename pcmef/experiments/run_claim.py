# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.experiments.formal_service 的 pre-flight 查詢、
#         由 pcmef.experiments.e2_formal 在執行前後推進狀態；
#         狀態檔落在 <formal_root>/RUN_CLAIM.json。
#         **不讀 lock、不執行任何 case、不呼叫 provider。**
# 檔案路徑: pcmef/experiments/run_claim.py
# 產生時間: 2026-09-05 14:20 +08:00
# 版本: v0.1.0
# 功能說明: Formal E2 的一次性由一份 claim 檔保證，而不是由「報告檔在不在」
#           推論。claim 以 O_EXCL 建立，因此兩個同時發出的請求只有一個成功。
# 模組定位: one-shot 的**狀態機**。它「不是」pre-flight 的一部分 ——
#           pre-flight 唯讀地問它，執行器才推進它。
# 主要責任:
#   1. RunIdentity 定義「同一場實驗」的機械判準（lineage + lock + 資料 + 程式碼）
#   2. reserve() 以 O_EXCL 原子地取得 claim，競態時只有一個贏
#   3. mark_running / mark_complete / mark_interrupted 推進狀態
#   4. read_claim() 唯讀查詢，供畫面與 pre-flight 使用
#   5. COMPLETE 永久封閉；INTERRUPTED_RESUMABLE 只允許相同 identity resume
# 維護提醒:
#   - 不得用「先檢查再建立」取代 O_EXCL。兩個請求可以同時通過檢查，
#     而 formal run 是一次性的 —— 那時兩個行程會同時寫同一份正式結果。
#   - 不得讓 COMPLETE 可以被覆寫或刪除成可重跑。一次性的意思就是它不可回頭；
#     要重跑必須是新的 lineage（新的 freeze dir），那會是另一個 identity。
#   - 不得在 identity 裡放時間戳、run_id、主機名或 pid。identity 回答的是
#     「這是不是同一場實驗」，而那四者每次都不同 —— 放進去等於任何 resume
#     都會被判成不同實驗。
#   - 不得把 claim 檔寫進 run-specific 目錄。它保護的是 canonical formal
#     root，而那個位置整個專案只有一個。
#   - v0.1.0 新增：首版，對應 P0-2。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_run_claim.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pcmef.core.hash import hash_object

__all__ = [
    "ClaimError",
    "CLAIM_FILENAME",
    "STATE_RESERVED",
    "STATE_RUNNING",
    "STATE_COMPLETE",
    "STATE_INTERRUPTED",
    "CLAIM_STATES",
    "RunIdentity",
    "reserve",
    "mark_running",
    "mark_complete",
    "mark_interrupted",
    "read_claim",
    "claim_path",
]

CLAIM_FILENAME = "RUN_CLAIM.json"
CLAIM_SCHEMA_VERSION = "formal_run_claim_v1"

#: 已取得 claim，尚未開始跑。競態的贏家在這個狀態。
STATE_RESERVED = "RESERVED"
#: 正在跑。
STATE_RUNNING = "RUNNING"
#: 跑完了。**永久封閉** —— 這就是 one-shot。
STATE_COMPLETE = "COMPLETE"
#: 中斷。可以用**相同 identity** resume，不可以 fresh restart。
STATE_INTERRUPTED = "INTERRUPTED_RESUMABLE"

CLAIM_STATES: tuple[str, ...] = (
    STATE_RESERVED, STATE_RUNNING, STATE_COMPLETE, STATE_INTERRUPTED,
)


class ClaimError(RuntimeError):
    """claim 無法取得，或狀態轉移不合法。一律 fail-closed。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim_path(formal_root: str | Path) -> Path:
    return Path(formal_root) / CLAIM_FILENAME


@dataclass(frozen=True)
class RunIdentity:
    """「這是不是同一場實驗」的機械判準。

    四個要素，缺一都會讓 resume 變成一件說不清楚的事：

      * `freeze_dir` / `lock_hashes` —— 決策堆疊。換 lineage 就是換實驗。
      * `base_manifest_hash` —— 吃進去的資料。換 partition 就是換實驗。
      * `code_version` —— 執行的程式碼。

    **刻意不含** run_id、時間戳、pid 與主機名：那些每次都不同，放進來會讓
    任何一次 resume 都被判成另一場實驗，於是 INTERRUPTED_RESUMABLE 這個
    狀態永遠無法被使用。
    """

    freeze_dir: str
    lock_hashes: Mapping[str, str]
    base_manifest_hash: str
    code_version: str

    def to_json(self) -> dict[str, Any]:
        return {
            "freeze_dir": str(self.freeze_dir),
            "lock_hashes": dict(sorted(self.lock_hashes.items())),
            "base_manifest_hash": str(self.base_manifest_hash),
            "code_version": str(self.code_version),
        }

    def digest(self) -> str:
        return hash_object(self.to_json())


def read_claim(formal_root: str | Path) -> dict[str, Any] | None:
    """唯讀地看現在的狀態。沒有 claim 回 None。

    讀不回來（檔案損毀）**不當成沒有 claim**：那會讓一次壞掉的寫入變成
    「可以重新開始」。回一個明確的 corrupt 狀態，讓上層 fail-closed。
    """
    path = claim_path(formal_root)
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        return {
            "state": "CORRUPT",
            "error": f"{type(error).__name__}: {error}",
            "path": path.as_posix(),
        }
    document.setdefault("path", path.as_posix())
    return document


def _write(path: Path, document: Mapping[str, Any]) -> None:
    """原子性覆寫既有 claim：先寫暫存再 replace。

    只用於**推進**既有 claim。建立 claim 走 `_create_exclusive()`，
    那一條不能用 replace —— replace 會蓋掉別人的 claim。
    """
    staging = path.with_suffix(".json.partial")
    staging.write_text(
        json.dumps(dict(document), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(staging, path)


def _create_exclusive(path: Path, document: Mapping[str, Any]) -> None:
    """以 O_EXCL 建立 claim。已存在即 FileExistsError。

    **不得改成先 `exists()` 再寫。** 兩個行程可以同時通過那個檢查，
    而 formal run 是一次性的 —— 那時兩份正式結果會同時被寫出來，
    而且沒有任何一方知道對方存在。O_EXCL 把「檢查」與「建立」合成一個
    由作業系統保證的動作。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(
            handle,
            json.dumps(
                dict(document), ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8"),
        )
    finally:
        os.close(handle)


def reserve(
    formal_root: str | Path,
    identity: RunIdentity,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    """取得跑一次 Formal E2 的權利。

    回傳新的 claim document。取不到就拋 `ClaimError` —— 呼叫端一律
    fail-closed，不得降級成「那就跑吧」。

    狀態機（現況 → 動作）：

    ==========================  ================================================
    現況                        結果
    ==========================  ================================================
    無 claim                    建立 RESERVED
    RESERVED / RUNNING          拒絕。另一個請求已經持有它
    COMPLETE                    拒絕。**永久封閉**，這就是 one-shot
    INTERRUPTED_RESUMABLE       只有 `resume=True` 且 identity 相符才放行
    CORRUPT                     拒絕。狀態不明時不得假設可以重新開始
    ==========================  ================================================

    「第一次正式開封後禁止 fresh restart」就落在這張表上：claim 一旦存在，
    `resume=False` 在任何狀態下都拿不到它。
    """
    root = Path(formal_root)
    path = claim_path(root)
    existing = read_claim(root)

    if existing is not None:
        state = str(existing.get("state", ""))
        if state == STATE_COMPLETE:
            raise ClaimError(
                f"Formal E2 is already COMPLETE at {path.as_posix()} "
                f"(finished {existing.get('finished_at')}). It is one-shot: the "
                "result is the conclusion, and a second run would produce a "
                "second, contradicting official result. Re-running requires a "
                "new corrective lineage, which is a different identity."
            )
        if state in (STATE_RESERVED, STATE_RUNNING):
            raise ClaimError(
                f"Formal E2 is already {state} at {path.as_posix()} "
                f"(claim {existing.get('claim_id')}, reserved "
                f"{existing.get('reserved_at')} by pid {existing.get('pid')} on "
                f"{existing.get('host')}). Only one run may hold the claim. If "
                "that run died, mark it interrupted before resuming."
            )
        if state == STATE_INTERRUPTED:
            if not resume:
                raise ClaimError(
                    f"Formal E2 was interrupted at {path.as_posix()} and the "
                    "final partition is already unsealed. A fresh restart is "
                    "refused; resume the same identity instead "
                    "(resume=True / --resume)."
                )
            recorded = str((existing.get("identity") or {}).get("digest", ""))
            if recorded != identity.digest():
                raise ClaimError(
                    "refusing to resume under a different identity: the claim "
                    f"records {recorded[:16]} and this run computes "
                    f"{identity.digest()[:16]}. Resume means continuing the same "
                    "experiment; a changed lineage, dataset or code revision is "
                    "a different one and must not inherit the claim."
                )
            document = dict(existing)
            document.update(
                state=STATE_RESERVED,
                resumed_at=_now(),
                resume_count=int(existing.get("resume_count", 0)) + 1,
                pid=os.getpid(),
                host=socket.gethostname(),
            )
            _write(path, document)
            return document
        raise ClaimError(
            f"Formal E2 claim at {path.as_posix()} is in an unusable state "
            f"{state!r}: {existing.get('error', '')}. Refusing to start; a claim "
            "whose state cannot be read must not be assumed to mean 'free'."
        )

    document = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "state": STATE_RESERVED,
        "claim_id": os.urandom(8).hex(),
        "identity": {**identity.to_json(), "digest": identity.digest()},
        "reserved_at": _now(),
        "started_at": None,
        "finished_at": None,
        "resume_count": 0,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "note": (
            "Formal E2 是一次性的。這份 claim 是它的狀態機：COMPLETE 之後"
            "永久封閉，中斷之後只能以相同 identity resume，不能重新開始。"
        ),
    }
    try:
        _create_exclusive(path, document)
    except FileExistsError:
        # 競態：在 read_claim() 與這裡之間有人先建立了。這正是 O_EXCL 要
        # 抓的那一格 —— 誠實地報告輸掉，而不是覆寫對方。
        raise ClaimError(
            f"lost the race for the Formal E2 claim at {path.as_posix()}: "
            "another request created it first. Only one run may hold it."
        ) from None
    return document


def _advance(
    formal_root: str | Path, claim_id: str, expected: tuple[str, ...],
    **changes: Any,
) -> dict[str, Any]:
    """推進既有 claim。claim_id 與現況都必須對得上。"""
    root = Path(formal_root)
    existing = read_claim(root)
    if existing is None:
        raise ClaimError(
            f"no Formal E2 claim at {claim_path(root).as_posix()}; nothing to "
            "advance. A run must reserve before it can report progress."
        )
    if str(existing.get("claim_id")) != str(claim_id):
        raise ClaimError(
            f"claim id mismatch: the file holds {existing.get('claim_id')!r} but "
            f"this run holds {claim_id!r}. Another run owns the claim."
        )
    state = str(existing.get("state", ""))
    if state not in expected:
        raise ClaimError(
            f"illegal transition from {state!r}; expected one of {list(expected)}"
        )
    document = dict(existing)
    document.update(changes)
    _write(claim_path(root), document)
    return document


def mark_running(formal_root: str | Path, claim_id: str) -> dict[str, Any]:
    return _advance(
        formal_root, claim_id, (STATE_RESERVED,),
        state=STATE_RUNNING, started_at=_now(),
    )


def mark_complete(
    formal_root: str | Path, claim_id: str, report_path: str = ""
) -> dict[str, Any]:
    """跑完了。**這一步之後 one-shot 永久封閉。**"""
    return _advance(
        formal_root, claim_id, (STATE_RUNNING, STATE_RESERVED),
        state=STATE_COMPLETE, finished_at=_now(), report_path=str(report_path),
    )


def mark_interrupted(
    formal_root: str | Path, claim_id: str, reason: str = ""
) -> dict[str, Any]:
    """跑到一半失敗。

    狀態留成 INTERRUPTED_RESUMABLE 而不是刪掉 claim：final partition 一旦
    生成就是開封，刪掉 claim 等於讓下一次以為那是第一次。
    """
    return _advance(
        formal_root, claim_id, (STATE_RESERVED, STATE_RUNNING),
        state=STATE_INTERRUPTED, interrupted_at=_now(), reason=str(reason)[:400],
    )
