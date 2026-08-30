# PC-MEF Research System source maintenance contract
# 上下游: 讀 data/splits/split_registry.json 的 calibration 指派、
#         data/raw_real/edge_impulse_export 的對應樣本、
#         freeze/preregregistrations/CAL-PREREG-003 與 freeze/stages/CAL-STAGE0-001；
#         由 cli 的 calibration first-access 呼叫；
#         附加 data/splits/calibration_access_ledger.json 並寫出
#         freeze/calibration/CAL-SF-001.sf.json。
# 檔案路徑: pcmef/experiments/calibration_first_access.py
# 產生時間: 2026-08-30 18:30 +08:00
# 版本: v0.1.0
# 功能說明: 執行本研究**第一次合法讀取 calibration partition**：先驗證全部
#           前提，再原子性地記帳，然後只讀 calibration 那 392 筆、算出
#           canonical raw data hash 與四個 feature 的 pooled s_f，並凍結它。
# 模組定位: calibration 的資料閘門。它是唯一被授權開啟 calibration partition
#           的入口；held-out 在這裡連路徑都不會被組出來。
# 主要責任:
#   1. assert_preconditions() 驗證 stage 0 已凍、held-out 未開、雜湊未漂移
#   2. append_ledger_entry() 在讀值**之前**原子性記帳
#   3. load_calibration_partition() 只載入 role == calibration 的樣本
#   4. canonical_raw_data_hash() 對讀到的值算出可重現的雜湊
#   5. compute_pooled_s_f() 逐 feature 池化 IQR，只算一次
# 維護提醒:
#   - 不得在本模組讀取 heldout_real 的任何一筆；它連檔名都不該被解析。
#     `_calibration_keys()` 是唯一的來源，且只回傳 role == calibration。
#   - 不得在 s_f 算出來之後重算它；逐階段重算會讓 optimizer 靠放大模擬
#     離散度稀釋自己的誤差（CAL-PREREG-003 normalization.frozen = true）。
#   - 不得在記帳之前讀值。帳本是「第一次 access 發生在什麼時候」的唯一憑據，
#     先讀後記等於那份憑據是事後補的。
#   - 不得讓 s_f <= 0 或非有限值通過；feature_scale_iqr 已 fail-closed，
#     本模組不得加上任何退路。
#   - v0.1.0 新增：首版 calibration first access（CAL-PREREG-003 stage 1 前置）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_first_access.py -v
#   - py -3.10 -m pcmef.cli calibration first-access --dry-run
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.core.hash import hash_object

__all__ = [
    "SF_RECORD_ID",
    "CalibrationAccessError",
    "assert_preconditions",
    "calibration_keys",
    "load_calibration_partition",
    "canonical_raw_data_hash",
    "compute_pooled_s_f",
    "append_ledger_entry",
    "run_first_access",
]

SF_RECORD_ID = "CAL-SF-001"

EXPECTED_TOTAL = 392
EXPECTED_PER_CLASS = 98


class CalibrationAccessError(RuntimeError):
    """前提未滿足，或 calibration partition 與凍結的身分不符。"""


# ---------------------------------------------------------------------------
# 前提
# ---------------------------------------------------------------------------


def assert_preconditions(
    freeze_dir: str | Path = "freeze", repo_root: str | Path = "."
) -> dict[str, Any]:
    """讀任何一筆 calibration 之前必須全部成立的條件。"""
    root = Path(repo_root)
    fdir = Path(freeze_dir)

    stage0_path = fdir / "stages" / "CAL-STAGE0-001.stage0.json"
    if not stage0_path.exists():
        raise CalibrationAccessError(
            "CAL-STAGE0-001 is not frozen. CAL-PREREG-003 requires stage 0 to "
            "complete and freeze BEFORE the first calibration access."
        )
    stage0 = json.loads(stage0_path.read_text(encoding="utf-8"))
    if stage0["payload"]["outcome"] != "STAGE0_COMPLETE":
        raise CalibrationAccessError(
            f"stage 0 outcome is {stage0['payload']['outcome']!r}, not STAGE0_COMPLETE"
        )
    if hash_object(stage0["payload"]) != stage0["payload_hash"]:
        raise CalibrationAccessError("CAL-STAGE0-001 payload hash does not verify")

    prereg_path = fdir / "preregistrations" / "CAL-PREREG-003.prereg.json"
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    if hash_object(prereg["payload"]) != prereg["payload_hash"]:
        raise CalibrationAccessError("CAL-PREREG-003 payload hash does not verify")

    from pcmef.experiments.calibration_prereg import protocol_hash

    live = protocol_hash()
    if live != prereg["payload"]["protocol_hash"]:
        raise CalibrationAccessError(
            f"the working protocol hashes to {live}, but CAL-PREREG-003 froze "
            f"{prereg['payload']['protocol_hash']}. The protocol drifted after freezing."
        )

    registry = json.loads(
        (root / "data" / "splits" / "split_registry.json").read_text(encoding="utf-8")
    )
    heldout = int(registry["heldout_access_count"])
    if heldout != 0:
        raise CalibrationAccessError(
            f"heldout_access_count is {heldout}, not 0. Held-out must stay sealed "
            "until E1 final."
        )

    return {
        "stage0_hash": stage0["payload_hash"],
        "prereg_hash": prereg["payload_hash"],
        "protocol_hash": live,
        "initial_lock_hash": prereg["payload"]["initial_simulation"]["lock_hash"],
        "parameter_set_hash": prereg["payload"]["registry"]["parameter_set_hash"],
        "calibration_set_hash": registry["calibration_set_hash"],
        "heldout_access_count": heldout,
    }


# ---------------------------------------------------------------------------
# 只取 calibration
# ---------------------------------------------------------------------------


def calibration_keys(repo_root: str | Path = ".") -> list[str]:
    """calibration partition 的 measurement key，**排序後**回傳。

    這是本模組唯一的樣本來源。任何 role != "calibration" 的指派在這裡就被
    濾掉，因此 held-out 的檔案路徑不會被組出來，更不會被開啟。
    """
    registry = json.loads(
        (Path(repo_root) / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    keys = sorted(k for k, role in registry["assignments"].items() if role == "calibration")
    if len(keys) != EXPECTED_TOTAL:
        raise CalibrationAccessError(
            f"expected {EXPECTED_TOTAL} calibration recordings, got {len(keys)}"
        )
    return keys


def load_calibration_partition(
    source_root: str | Path, repo_root: str | Path = "."
) -> dict[str, list[np.ndarray]]:
    """載入 calibration partition 的 (500,4) 序列，逐類分組。

    **只開啟 calibration 那 392 個檔案。** 刻意不呼叫
    `EdgeImpulseAdapter.build_inventory()`：那個方法會掃 training/ 與
    testing/ 底下**每一個** JSON 並把值讀進記憶體，也就是會一併讀到
    held-out 的 168 筆。對本模組而言那是洩漏，不是效能問題 ——
    這裡改為由 calibration key 直接組出檔名再逐一開啟。

    **recording 是統計單位**：回傳的是每筆 recording 一個陣列，不是一個
    攤平的大矩陣 —— 攤平之後就沒有人分得出「一筆」在哪裡結束。
    """
    # 類別映射取自 adapter 的單一來源，不得在此另立一套（NOTE-001）。
    from pcmef.adapters.edge_impulse import EI_LABEL_TO_CLASS

    root = Path(source_root)
    wanted = calibration_keys(repo_root)
    by_class: dict[str, list[np.ndarray]] = {c: [] for c in CLASS_ORDER}
    seen: list[str] = []

    for key in wanted:
        label, measurement = key.split("/", 1)
        class_label = EI_LABEL_TO_CLASS.get(label)
        if class_label is None:
            raise CalibrationAccessError(
                f"label {label!r} is not in EI_LABEL_TO_CLASS; the split registry and "
                "the adapter disagree about class naming"
            )
        matches = sorted(
            path
            for split in ("training", "testing")
            for path in (root / split).glob(f"{label}.{measurement}.csv.*.json")
        )
        if len(matches) != 1:
            raise CalibrationAccessError(
                f"{key} resolved to {len(matches)} files; expected exactly one"
            )
        payload = json.loads(matches[0].read_text(encoding="utf-8"))["payload"]
        values = np.asarray(payload["values"], dtype=np.float64)
        if values.shape != (500, len(TOF_SCHEMA)):
            raise CalibrationAccessError(
                f"{key} has shape {values.shape}; expected (500, {len(TOF_SCHEMA)})"
            )
        by_class[class_label].append(values)
        seen.append(key)

    if len(seen) != EXPECTED_TOTAL:
        raise CalibrationAccessError(
            f"loaded {len(seen)} calibration recordings, expected {EXPECTED_TOTAL}"
        )
    for class_label, blocks in by_class.items():
        if len(blocks) != EXPECTED_PER_CLASS:
            raise CalibrationAccessError(
                f"{class_label} has {len(blocks)} calibration recordings, "
                f"expected {EXPECTED_PER_CLASS}"
            )
    return by_class


def canonical_raw_data_hash(by_class: dict[str, list[np.ndarray]]) -> str:
    """對實際讀到的**數值**算出可重現的雜湊。

    以 canonical class 順序、每類內以逐筆陣列的內容雜湊排序後組合，
    因此與檔案系統的列舉順序無關。
    """
    from pcmef.core.hash import hash_array

    per_class = {
        class_label: sorted(hash_array(block) for block in blocks)
        for class_label, blocks in by_class.items()
    }
    return hash_object(
        {
            "schema": list(TOF_SCHEMA),
            "class_order": list(CLASS_ORDER),
            "recordings_per_class": {k: len(v) for k, v in per_class.items()},
            "recording_hashes": per_class,
        }
    )


def compute_pooled_s_f(by_class: dict[str, list[np.ndarray]]) -> dict[str, float]:
    """s_f = 逐 feature、**跨類池化**的 calibration 真實值 IQR。

    只算一次，全部階段共用（CAL-PREREG-003 normalization.frozen = true）。
    """
    from pcmef.stats.metrics import feature_scale_iqr

    out: dict[str, float] = {}
    for index, feature in enumerate(TOF_SCHEMA):
        pooled = np.concatenate(
            [block[:, index] for blocks in by_class.values() for block in blocks]
        )
        out[feature] = feature_scale_iqr(pooled)
    return out


# ---------------------------------------------------------------------------
# 帳本（先記帳，後讀值）
# ---------------------------------------------------------------------------


def append_ledger_entry(
    purpose: str,
    code_version: str,
    repo_root: str | Path = ".",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """原子性地附加一筆 access 記錄並遞增計數。

    先寫暫存檔再 os.replace：中途失敗時帳本要嘛是舊的、要嘛是新的，
    不會出現一個「計數加了但條目沒寫進去」的中間狀態。
    """
    path = Path(repo_root) / "data" / "splits" / "calibration_access_ledger.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    entry = {
        "index": int(ledger["calibration_access_count"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "purpose": purpose,
        "code_version": code_version,
        **(extra or {}),
    }
    ledger["entries"].append(entry)
    ledger["calibration_access_count"] = entry["index"]

    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(ledger, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    return entry


# ---------------------------------------------------------------------------
# 第一次合法讀取
# ---------------------------------------------------------------------------


def run_first_access(
    source_root: str | Path = "data/raw_real/edge_impulse_export",
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    code_version: str = "",
) -> tuple[Path, dict[str, Any]]:
    """驗證前提 -> 記帳 -> 讀值 -> 算 s_f -> 凍結。順序不得調換。"""
    target_dir = Path(freeze_dir) / "calibration"
    target = target_dir / f"{SF_RECORD_ID}.sf.json"
    if target.exists():
        raise CalibrationAccessError(
            f"{SF_RECORD_ID} is already frozen at {target}. s_f is computed ONCE and "
            "shared by every stage; recomputing it would let the optimizer dilute its "
            "own error by inflating simulated dispersion."
        )

    evidence = assert_preconditions(freeze_dir, repo_root)

    # **記帳在讀值之前。** 帳本是「第一次 access 何時發生」的唯一憑據；
    # 先讀後記等於那份憑據是事後補的。
    entry = append_ledger_entry(
        purpose="CAL-PREREG-003 stage 1 start: raw_data_hash + pooled s_f",
        code_version=code_version,
        repo_root=repo_root,
        extra={
            "partition": "calibration",
            "recordings": EXPECTED_TOTAL,
            "reads_heldout": False,
            "stage0_hash": evidence["stage0_hash"],
            "preregistration_hash": evidence["prereg_hash"],
        },
    )

    by_class = load_calibration_partition(source_root, repo_root)
    raw_hash = canonical_raw_data_hash(by_class)
    s_f = compute_pooled_s_f(by_class)

    from pcmef.core.hash import hash_file

    payload = {
        "record_id": SF_RECORD_ID,
        "purpose": "frozen pooled feature scale s_f for CAL-PREREG-003 calibration",
        "preregistration_id": "CAL-PREREG-003",
        "preregistration_hash": evidence["prereg_hash"],
        "protocol_hash": evidence["protocol_hash"],
        "stage0_hash": evidence["stage0_hash"],
        "initial_simulation_lock_hash": evidence["initial_lock_hash"],
        "parameter_set_hash": evidence["parameter_set_hash"],
        "calibration_set_hash": evidence["calibration_set_hash"],
        "raw_calibration_data_hash": raw_hash,
        "s_f": s_f,
        "s_f_definition": (
            "pooled over classes, per feature, IQR of the calibration partition's "
            "REAL values; computed exactly once and shared by every stage "
            "(CAL-PREREG-003 normalization.frozen = true)"
        ),
        "implementation": "pcmef.stats.metrics.feature_scale_iqr",
        "implementation_hash": hash_file(
            Path(repo_root) / "pcmef" / "stats" / "metrics.py"
        ),
        "loader_hash": hash_file(
            Path(repo_root) / "pcmef" / "experiments" / "calibration_first_access.py"
        ),
        "recordings_total": EXPECTED_TOTAL,
        "recordings_per_class": {k: len(v) for k, v in sorted(by_class.items())},
        "statistical_unit": "recording",
        "code_version": code_version,
        "ledger_entry": entry,
        "heldout_access_count": evidence["heldout_access_count"],
        "claim_boundary": (
            "This record freezes the normalisation scale only. It contains no "
            "per-class statistic and no class mean; it must not be used to infer "
            "anything about the real class distributions beyond their pooled spread."
        ),
    }
    document = {
        "record_id": SF_RECORD_ID,
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
