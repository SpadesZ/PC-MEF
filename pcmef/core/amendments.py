# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 amendment freeze / amendment status 呼叫；寫出並讀回
#         freeze/amendments/<id>.amendment.json；被 audit.e1_gates 引用以確認
#         被修訂的 gate 契約版本與已凍結的 amendment 一致。
# 檔案路徑: pcmef/core/amendments.py
# 產生時間: 2026-08-27 23:40 +08:00
# 版本: v0.1.0
# 功能說明: 記錄「協定本身被修改過」這件事 —— 誰改了哪一條 gate、為什麼改、
#           改之前 held-out 有沒有被動過，並把整份記錄雜湊後凍結成不可覆寫的檔案。
# 模組定位: 預註冊修訂的證據層。它「不是」lock —— lock 凍結的是實驗參數，
#           amendment 凍結的是「判準本身的變更」。兩者必須分開，否則
#           「改了規則」會被混在「設定了參數」裡看不出來。
# 主要責任:
#   1. ProtocolAmendment 描述一次修訂的完整內容與前提證據
#   2. AmendmentStore.freeze() 寫入且拒絕覆寫
#   3. AmendmentStore.load() / list_ids() 供稽核與 CLI 讀回
#   4. payload_hash 讓事後可驗證這份記錄未被竄改
# 維護提醒:
#   - 不得允許覆寫已凍結的 amendment。判準改了兩次就必須有兩份記錄，
#     否則「後來又偷偷改回去」不會留下痕跡。
#   - 不得在 held-out 已被取用後才凍結放寬型 amendment；
#     precondition_evidence 必須如實記下當下的 access_count，
#     那正是這份記錄唯一能證明「不是看到結果才改規則」的東西。
#   - 不得把 amendment 當成補預設值的通道；它只描述判準結構的變更，
#     未核定數值一律仍走 !required（NOTE-005）。
#   - v0.1.0 新增：首版 amendment 凍結機制，對應 AMD-001（NOTE-028）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_amendments.py -v
#   - py -3.10 -m pcmef.cli amendment status
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.core.hash import hash_object

__all__ = [
    "AmendmentError",
    "ProtocolAmendment",
    "AmendmentStore",
]


class AmendmentError(RuntimeError):
    """amendment 不存在、格式不符，或試圖覆寫。"""


@dataclass(frozen=True)
class ProtocolAmendment:
    """一次協定修訂的完整記錄。"""

    amendment_id: str
    title: str
    supersedes: str
    rationale: str
    changed_contracts: dict[str, dict[str, str]]
    precondition_evidence: dict[str, Any]
    invariants_preserved: dict[str, Any]
    authority: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def payload(self) -> dict[str, Any]:
        return {
            "amendment_id": self.amendment_id,
            "title": self.title,
            "supersedes": self.supersedes,
            "rationale": self.rationale,
            "changed_contracts": self.changed_contracts,
            "precondition_evidence": self.precondition_evidence,
            "invariants_preserved": self.invariants_preserved,
            "authority": self.authority,
            "created_at": self.created_at,
        }

    def to_document(self) -> dict[str, Any]:
        payload = self.payload()
        return {
            "amendment_id": self.amendment_id,
            "payload": payload,
            "payload_hash": hash_object(payload),
            "version": 1,
        }


class AmendmentStore:
    """freeze/amendments/ 的讀寫閘門。"""

    def __init__(self, freeze_dir: str | Path) -> None:
        self.dir = Path(freeze_dir) / "amendments"

    def path_for(self, amendment_id: str) -> Path:
        return self.dir / f"{amendment_id}.amendment.json"

    def exists(self, amendment_id: str) -> bool:
        return self.path_for(amendment_id).exists()

    def list_ids(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.name.split(".")[0] for p in self.dir.glob("*.amendment.json"))

    def freeze(self, amendment: ProtocolAmendment) -> Path:
        """寫入 amendment。已存在即拒絕 —— 判準變更不可覆寫。"""
        path = self.path_for(amendment.amendment_id)
        if path.exists():
            raise AmendmentError(
                f"amendment {amendment.amendment_id!r} is already frozen at {path}; "
                "protocol changes are append-only, issue a new amendment id instead"
            )
        self.dir.mkdir(parents=True, exist_ok=True)
        document = amendment.to_document()
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def load(self, amendment_id: str) -> dict[str, Any]:
        """讀回並驗證雜湊。內容被改過即拋錯。"""
        path = self.path_for(amendment_id)
        if not path.exists():
            raise AmendmentError(f"amendment {amendment_id!r} is not frozen")
        document = json.loads(path.read_text(encoding="utf-8"))
        payload = document.get("payload")
        if payload is None:
            raise AmendmentError(f"amendment {amendment_id!r} has no payload")
        actual = hash_object(payload)
        recorded = document.get("payload_hash")
        if actual != recorded:
            raise AmendmentError(
                f"amendment {amendment_id!r} payload_hash mismatch: "
                f"recorded {recorded}, actual {actual}; the record has been modified"
            )
        return payload
