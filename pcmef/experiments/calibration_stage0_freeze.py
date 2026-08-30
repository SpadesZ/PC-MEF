# PC-MEF Research System source maintenance contract
# 上下游: 讀 calibration_stage0.execute_stage0() 產出的
#         outputs/calibration/stage_0/stage0_identifiability.json，以及
#         freeze/preregistrations/CAL-PREREG-002.prereg.json 與
#         freeze/initial_simulation.lock.json；由 cli 的
#         calibration stage0 --freeze 呼叫；寫出
#         freeze/stages/CAL-STAGE0-001.stage0.json，不可覆寫。
# 檔案路徑: pcmef/experiments/calibration_stage0_freeze.py
# 產生時間: 2026-08-30 15:20 +08:00
# 版本: v0.1.0
# 功能說明: 把跑完的 stage 0 結果連同它所依賴的每一個雜湊凍成一份不可覆寫的
#           記錄，並在凍結當下**重新驗證**那些雜湊，而不是採信報告裡的字串。
# 模組定位: stage 0 與 stage 1 之間的封條。它決定的不是「結果好不好」，
#           而是「這個結果是不是由宣稱的那一份凍結協定、在宣稱的那一份
#           凍結模型上、於第一次讀取真實資料之前產生的」。
# 主要責任:
#   1. 拒絕覆寫既有的 stage 0 記錄
#   2. 凍結前重新計算 protocol / bounds / registry 雜湊並與報告逐項比對
#   3. 重新讀取兩個 access 帳本，確認仍為 0
#   4. 需要裁決的結局一律拒絕凍結
# 維護提醒:
#   - 不得在 outcome 為 STAGE0_ADJUDICATION_REQUIRED 時凍結；那正是要停下來
#     的情況，凍結它等於把「尚未裁決」寫成「已完成」。
#   - 不得只比對報告裡自帶的雜湊字串；雜湊必須在此重新算一次，
#     否則一份被手改過的報告可以自證清白。
#   - 不得在 calibration_access_count > 0 之後補凍 stage 0；順序反了就是反了。
#   - v0.1.0 新增：首版 stage 0 封存（NOTE-043）。
# 驗證方式:
#   - py -3.10 -m pcmef.cli calibration stage0 --out outputs/calibration/stage_0 --freeze
#   - py -3.10 -m pytest tests/unit/test_calibration_stage0.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.core.hash import hash_object

__all__ = ["STAGE0_ID", "Stage0FreezeError", "freeze_stage0"]

STAGE0_ID = "CAL-STAGE0-001"

#: 凍結記錄必須帶的欄位。這份清單同時是驗證器的比對對象，因此一律是裸識別字。
REQUIRED_PAYLOAD_FIELDS: tuple[str, ...] = (
    "stage_id",
    "outcome",
    "preregistration_id",
    "preregistration_hash",
    "protocol_hash",
    "initial_simulation_lock_hash",
    "parameter_registry",
    "bounds_resolution_hash",
    "resolved_bounds",
    "seeds",
    "sigma_mc",
    "parameters",
    "collinearity",
    "classification",
    "code_version",
    "calibration_access_count",
    "heldout_access_count",
)


class Stage0FreezeError(RuntimeError):
    """stage 0 的結果不可凍結，或凍結前的重新驗證不通過。"""


def freeze_stage0(
    report: dict[str, Any],
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    code_version: str = "",
    code_dirty: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """凍結 stage 0。凍結前重新驗證每一個依賴雜湊。"""
    from pcmef.core.formal_loader import load_formal_lock
    from pcmef.core.parameters import ParameterRegistry
    from pcmef.experiments.calibration_plan import bounds_resolution_hash
    from pcmef.experiments.calibration_prereg import load_protocol

    root = Path(repo_root)
    target_dir = Path(freeze_dir) / "stages"
    target = target_dir / f"{STAGE0_ID}.stage0.json"
    if target.exists():
        raise Stage0FreezeError(
            f"{STAGE0_ID} is already frozen at {target}; stage records are "
            "append-only. Re-running stage 0 after seeing a fit result is exactly "
            "what outcome_is_binding forbids."
        )

    outcome = report.get("outcome")
    if outcome != "STAGE0_COMPLETE":
        raise Stage0FreezeError(
            f"stage 0 outcome is {outcome!r}, not STAGE0_COMPLETE. "
            + "; ".join(report.get("adjudication_blockers") or ["no reason recorded"])
            + ". A result that needs adjudication may not be frozen as if it were "
            "settled."
        )

    # -- 重算而非採信 ------------------------------------------------------
    protocol = load_protocol()
    recomputed_protocol_hash = hash_object(protocol)
    if recomputed_protocol_hash != report["protocol_hash"]:
        raise Stage0FreezeError(
            f"protocol hash recomputed as {recomputed_protocol_hash} but the report "
            f"claims {report['protocol_hash']}; the protocol changed after the run"
        )

    prereg_path = (
        Path(freeze_dir) / "preregistrations" / f"{report['preregistration_id']}.prereg.json"
    )
    frozen = json.loads(prereg_path.read_text(encoding="utf-8"))
    if frozen["payload_hash"] != report["preregistration_hash"]:
        raise Stage0FreezeError(
            "the frozen preregistration hash does not match the one the report was "
            "produced against"
        )
    if frozen["payload"]["protocol_hash"] != recomputed_protocol_hash:
        raise Stage0FreezeError(
            "the working protocol no longer hashes to the frozen protocol_hash"
        )

    resolved = load_formal_lock("initial_simulation", freeze_dir, root)
    if resolved.payload_hash != report["initial_simulation_lock_hash"]:
        raise Stage0FreezeError(
            "initial_simulation.lock hash does not match the report"
        )

    registry = ParameterRegistry.load()
    if registry.parameter_set_hash() != report["parameter_registry"]["parameter_set_hash"]:
        raise Stage0FreezeError(
            "parameter_set_hash changed between the run and the freeze"
        )

    recomputed_bounds = bounds_resolution_hash(protocol, resolved.payload, registry)
    if recomputed_bounds != report["bounds_resolution_hash"]:
        raise Stage0FreezeError(
            f"bounds_resolution_hash recomputed as {recomputed_bounds} but the "
            f"report claims {report['bounds_resolution_hash']}"
        )

    # -- 順序：兩個帳本都必須仍是 0 ----------------------------------------
    ledger_path = root / protocol["data"]["first_access"]["ledger"]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if int(ledger["calibration_access_count"]) != 0:
        raise Stage0FreezeError(
            "calibration_access_count is no longer 0; stage 0 must be frozen "
            "before the first calibration access, not after it"
        )
    split_registry = json.loads(
        (root / "data" / "splits" / "split_registry.json").read_text(encoding="utf-8")
    )
    if int(split_registry["heldout_access_count"]) != 0:
        raise Stage0FreezeError("heldout_access_count is no longer 0")

    payload = dict(report)
    payload["code_version"] = code_version
    payload["code_dirty_at_freeze"] = bool(code_dirty)
    payload["ordering_rule"] = (
        "stage 0 completed and was frozen while calibration_access_count = 0 and "
        "heldout_access_count = 0. Stage 1 is the first legitimate calibration "
        "access; it is a separate decision and is not authorised by this record."
    )
    payload["binding_effect"] = (
        "The admitted / gauge-fixed / not-fitted split recorded here is the "
        "parameter set for stages 1-5. CAL-PREREG-002 outcome_is_binding forbids "
        "re-running stage 0 after seeing any fit result."
    )

    missing = [f for f in REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise Stage0FreezeError(f"stage 0 payload is missing required fields: {missing}")

    document = {
        "stage_id": STAGE0_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload_hash": hash_object(payload),
        "payload": payload,
        "version": 1,
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target, document
