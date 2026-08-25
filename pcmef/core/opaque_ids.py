# 檔案路徑: pcmef/core/opaque_ids.py
# 模組定位: canonical case ID 與 opaque inference ID 之間的 evaluator-only 隱藏映射。
# 功能說明: 以 run-scoped keyed HMAC 產生非語意 opaque_case_id，並保存只有 evaluator 可讀的反查表。
# 主要責任: 讓 inference 端拿得到穩定識別碼，同時讓該識別碼無法被解碼回 class/condition/severity。
# 呼叫來源: experiments.e2_formal、agents.provider、stats 的 evaluator join、tests.leakage。
# 輸入契約: canonical case ID 集合；salt 由建立時隨機產生，之後只從 map 檔載入。
# 輸出契約: 小寫 hex opaque id 與 evaluator-only 的 OpaqueIdMap；map 檔含 salt，屬機密等級。
# 安全邊界: map 檔與 salt 絕不得進入 provider payload、manifest 可讀欄位或任何 report。
# 維護提醒: salt 外洩等同 opaque id 可被逆推，須視為研究完整性事故並重新產生整個 run 的映射。
# 版本: v0.1.0 / 2026-08-25
# ----------------------------------------------------------------------------------------------------

from __future__ import annotations

import hmac
import json
import os
import secrets
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from pcmef.core.hash import hash_object

__all__ = ["OpaqueIdError", "OpaqueIdMap"]

_OPAQUE_HEX_LENGTH = 32
_SALT_BYTES = 32


class OpaqueIdError(RuntimeError):
    """opaque map 缺失、損毀，或被要求做不允許的反查/覆寫。"""


@dataclass
class OpaqueIdMap:
    """evaluator-only 的 opaque id 映射表。

    NOTE(NOTE-004): 採 run-scoped keyed HMAC 而非全域隨機 UUID。
    同一 run 內 deterministic 讓 resume 與 artifact 對齊安全；跨 run 不同
    讓 opaque id 不會退化成半永久語意標籤。

    SRC-SAI Appendix I1：opaque_case_id 必須 random/non-semantic，
    僅能透過 evaluator-only hidden map 反查；ground truth 與 benchmark metadata
    只能在 inference 完成後透過這個映射 join 回來。
    """

    run_id: str
    salt: bytes
    forward: dict[str, str]
    reverse: dict[str, str]

    # -- 建立 / 載入 -------------------------------------------------------

    @classmethod
    def create(cls, run_id: str, canonical_case_ids: list[str]) -> "OpaqueIdMap":
        """為一個 run 建立全新映射。salt 隨機產生，不可重現。"""
        if not run_id:
            raise OpaqueIdError("run_id is required")
        unique_ids = list(dict.fromkeys(canonical_case_ids))
        if len(unique_ids) != len(canonical_case_ids):
            duplicates = sorted(
                {cid for cid in canonical_case_ids if canonical_case_ids.count(cid) > 1}
            )
            raise OpaqueIdError(f"duplicate canonical case ids: {duplicates}")

        salt = secrets.token_bytes(_SALT_BYTES)
        forward: dict[str, str] = {}
        reverse: dict[str, str] = {}
        for case_id in unique_ids:
            opaque = cls._derive(salt, case_id)
            if opaque in reverse:
                # HMAC-SHA256 截斷到 128 bit，碰撞機率在本研究規模下可忽略；
                # 但真的發生時必須中止，不能靜默覆蓋掉另一個 case 的映射。
                raise OpaqueIdError(
                    f"opaque id collision between {reverse[opaque]!r} and {case_id!r}"
                )
            forward[case_id] = opaque
            reverse[opaque] = case_id
        return cls(run_id=run_id, salt=salt, forward=forward, reverse=reverse)

    @staticmethod
    def _derive(salt: bytes, canonical_case_id: str) -> str:
        digest = hmac.new(salt, canonical_case_id.encode("utf-8"), sha256).hexdigest()
        return digest[:_OPAQUE_HEX_LENGTH]

    # -- 查詢 -------------------------------------------------------------

    def to_opaque(self, canonical_case_id: str) -> str:
        try:
            return self.forward[canonical_case_id]
        except KeyError:
            raise OpaqueIdError(
                f"canonical case {canonical_case_id!r} is not registered in the "
                f"opaque map for run {self.run_id!r}"
            ) from None

    def to_canonical(self, opaque_case_id: str) -> str:
        """反查 canonical ID。只有 evaluator 端可以呼叫。

        inference 路徑上的任何模組呼叫這個方法都是設計錯誤：
        agent、reliability、gate、fusion 都不該知道 canonical 身分
        （SRC-SAI FR-021）。這裡無法在執行期分辨呼叫者，
        因此以 tests/leakage 靜態掃描確保 inference 模組不 import 本模組。
        """
        try:
            return self.reverse[opaque_case_id]
        except KeyError:
            raise OpaqueIdError(
                f"opaque id {opaque_case_id!r} is not registered in the opaque map "
                f"for run {self.run_id!r}"
            ) from None

    # -- 持久化 -----------------------------------------------------------

    def map_hash(self) -> str:
        """回傳可寫入 inference_firewall.lock 的映射 hash。

        刻意只 hash forward 映射與 run_id，**不含 salt**：
        lock 檔是 formal provenance 的一部分，會被檢視與流通，
        salt 進了 lock 就等於 opaque id 可被任何拿到 lock 的人逆推。
        """
        return hash_object({"run_id": self.run_id, "forward": self.forward})

    def save(self, path: str | Path) -> Path:
        """寫入 evaluator-only map 檔。含 salt，屬機密等級。

        拒絕覆寫既有檔案：映射一旦用於任何 provider 呼叫就已固定，
        重寫會讓既有 artifact 的 opaque id 對不回 canonical case。
        """
        target = Path(path)
        if target.exists():
            raise OpaqueIdError(
                f"opaque map {target} already exists; the mapping is immutable once "
                "any provider call has used it"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "salt_hex": self.salt.hex(),
            "forward": self.forward,
            "map_hash": self.map_hash(),
            "_warning": "EVALUATOR ONLY. Contains the opaque-id salt. "
            "Never ship, log, or include in any provider payload or report.",
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(target, 0o600)
        except (OSError, NotImplementedError):
            # Windows 上 chmod 語意有限；權限保護改由存放位置負責，不因此中斷。
            pass
        return target

    @classmethod
    def load(cls, path: str | Path) -> "OpaqueIdMap":
        source = Path(path)
        if not source.exists():
            raise OpaqueIdError(f"opaque map {source} not found")
        data: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
        for key in ("run_id", "salt_hex", "forward", "map_hash"):
            if key not in data:
                raise OpaqueIdError(f"opaque map {source} is missing {key!r}")

        forward = dict(data["forward"])
        instance = cls(
            run_id=str(data["run_id"]),
            salt=bytes.fromhex(str(data["salt_hex"])),
            forward=forward,
            reverse={opaque: case for case, opaque in forward.items()},
        )
        if instance.map_hash() != data["map_hash"]:
            raise OpaqueIdError(
                f"opaque map {source} failed integrity check; stored map_hash does "
                "not match its contents"
            )
        # 重新以 salt 推導一次，確認 forward 表沒有被手動改動成任意對應。
        for case_id, opaque in forward.items():
            if cls._derive(instance.salt, case_id) != opaque:
                raise OpaqueIdError(
                    f"opaque map {source} entry for {case_id!r} is inconsistent with "
                    "the stored salt; the mapping has been tampered with"
                )
        return instance
