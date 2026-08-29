# PC-MEF Research System source maintenance contract
# 上下游: 讀 core.locks 的 LockStore 與 core.errata 的 ErratumStore；
#         由 cli 的 locks resolve、experiments.* 與任何 formal runner 呼叫，
#         作為讀取已凍結 lock 的**唯一** formal 入口。
# 檔案路徑: pcmef/core/formal_loader.py
# 產生時間: 2026-08-29 10:55 +08:00
# 版本: v0.1.0
# 功能說明: 載入一份 formal lock 時，同時驗證三件事並回傳可追溯的結果 ——
#           原始 lock 未被竄改、指向它的每一份 erratum 都通過範疇與綁定檢查、
#           以及每一份 erratum 的一手證據檔仍然存在且雜湊相符。
# 模組定位: lock 的 formal 讀取閘門。LockStore.load() 只保證「這份檔案沒被改過」，
#           它不知道有勘誤存在；本模組是唯一會把勘誤疊上去的地方，
#           也是唯一有資格回答「這份 lock 現在該被怎麼解讀」的地方。
# 主要責任:
#   1. load_formal_lock() 驗證 lock 完整性並套用已驗證的 errata
#   2. 回傳 ResolvedLock：同時帶原始 payload、更正後 payload、與 provenance
#   3. lock 身分永遠是**原始** payload_hash，勘誤只是疊加層
#   4. 任一驗證失敗即拋錯，不回傳部分套用的結果
# 維護提醒:
#   - 不得讓 formal 程式碼直接呼叫 LockStore.load() 取用 lock 內容；
#     那條路看不到勘誤，會讓兩個地方對同一份 lock 有兩種解讀。
#   - 不得以 corrected payload 重算並宣稱那是 lock 的 hash；
#     lock 的身分是凍結當下那一份，勘誤改變的是解讀而不是身分。
#   - 不得在任一 erratum 驗證失敗時退回「就用原始值」；驗證失敗代表
#     provenance 已經自相矛盾，繼續執行會把矛盾帶進 formal run。
#   - 不得為了讓載入通過而略過證據檔的雜湊比對。
#   - v0.1.0 新增：首版 formal 載入閘門，對應 ERR-001（NOTE-040）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_errata.py -v
#   - py -3.10 -m pcmef.cli locks resolve --lock initial_simulation
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcmef.core.errata import ErratumStore, apply_errata
from pcmef.core.locks import LockStore

__all__ = ["ResolvedLock", "load_formal_lock"]


@dataclass(frozen=True)
class ResolvedLock:
    """一份已解析的 formal lock。

    `payload` 是套用勘誤後、供讀取使用的內容；`original_payload` 是凍結
    當下那一份。兩者都保留是刻意的 —— 只給更正後的內容，後人無法看出
    這份 lock 曾經記錯過什麼；只給原始內容，則等於讓已知的錯誤繼續流通。
    """

    name: str
    payload: dict[str, Any]
    original_payload: dict[str, Any]
    payload_hash: str
    errata_applied: tuple[dict[str, Any], ...]
    errata_hashes: dict[str, str]

    @property
    def corrected(self) -> bool:
        return bool(self.errata_applied)

    def provenance(self) -> dict[str, Any]:
        """可嵌入下游 lock / manifest / artifact 的追溯區塊。

        `payload_hash` 是**原始** lock 的雜湊，不是更正後的 —— 下游引用
        這份 lock 時引用的必須是它被凍結的那個身分。
        """
        return {
            "lock": self.name,
            "payload_hash": self.payload_hash,
            "errata": [
                {
                    "erratum_id": summary["erratum_id"],
                    "erratum_payload_hash": self.errata_hashes[summary["erratum_id"]],
                    "field_path": summary["field_path"],
                    "recorded_value": summary["recorded_value"],
                    "corrected_value": summary["corrected_value"],
                    "evidence": [
                        {"path": e["path"], "sha256": e["sha256"]}
                        for e in summary["evidence_verified"]
                    ],
                }
                for summary in self.errata_applied
            ],
        }


def load_formal_lock(
    name: str,
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
) -> ResolvedLock:
    """載入 lock 並套用經過完整驗證的勘誤。

    三道驗證缺一不可：
      1. **original lock** —— LockStore.load() 重算 payload_hash，
         凍結後被改過即拒絕。
      2. **erratum** —— 每一份都重算自己的 payload_hash、必須指名這一份
         lock 的 hash、必須落在 metadata 白名單內且不在禁區、
         且 scientific_state_changed 必須為 false。
      3. **source evidence** —— 證據檔必須存在、雜湊相符、其指向的欄位
         等於 corrected_value，且 binds 斷言把它綁回**被凍結的那次 run**。
    """
    lock_store = LockStore(freeze_dir)
    original = lock_store.load(name)          # 驗證 1
    payload_hash = lock_store.load_hash(name)

    erratum_store = ErratumStore(freeze_dir)
    entries = erratum_store.for_lock(name)    # 讀回時已重算 erratum 自身的 hash

    payloads = [payload for _, payload in entries]
    corrected, summaries = apply_errata(      # 驗證 2 與 3
        original, payload_hash, payloads, repo_root
    )

    hashes = {
        erratum_id: erratum_store.load_hash(erratum_id) for erratum_id, _ in entries
    }

    return ResolvedLock(
        name=name,
        payload=corrected,
        original_payload=original,
        payload_hash=payload_hash,
        errata_applied=tuple(summaries),
        errata_hashes=hashes,
    )


def resolved_lock_report(resolved: ResolvedLock) -> list[str]:
    """人可讀的解析結果，供 CLI 輸出。"""
    lines = [
        f"lock            : {resolved.name}",
        f"payload_hash    : {resolved.payload_hash}   (original, unchanged)",
        f"errata applied  : {len(resolved.errata_applied)}",
    ]
    for summary in resolved.errata_applied:
        erratum_id = summary["erratum_id"]
        lines.append(
            f"  {erratum_id}  {summary['field_path']}: "
            f"{json.dumps(summary['recorded_value'], ensure_ascii=False)} -> "
            f"{json.dumps(summary['corrected_value'], ensure_ascii=False)}"
        )
        lines.append(f"    erratum hash : {resolved.errata_hashes[erratum_id]}")
        for evidence in summary["evidence_verified"]:
            lines.append(
                f"    evidence     : {evidence['path']}  "
                f"sha256 {evidence['sha256'][:16]}  "
                f"binds {evidence['bindings_checked'] or '[]'}"
            )
    return lines
