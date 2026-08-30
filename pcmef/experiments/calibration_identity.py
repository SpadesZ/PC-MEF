# PC-MEF Research System source maintenance contract
# 上下游: 讀 freeze/preregistrations/CAL-PREREG-003、freeze/stages/CAL-STAGE0-001、
#         freeze/calibration/CAL-SF-001、freeze/initial_simulation.lock.json 與
#         data/splits/{split_registry,calibration_access_ledger}.json；
#         由 pcmef.experiments.calibration_formal 在任何一次評估之前呼叫；
#         不寫出任何 artifact（帳本附加由 calibration_first_access 負責）。
# 檔案路徑: pcmef/experiments/calibration_identity.py
# 產生時間: 2026-08-30 19:05 +08:00
# 版本: v0.1.0
# 功能說明: 把 Formal Calibration 依賴的每一個凍結物讀進來、逐一驗證其
#           payload hash 與已知常數相符，並導出各階段真正的搜尋維度與界線。
#           它是「這次執行到底照著哪一份協定跑」這個問題的唯一答案來源。
# 模組定位: formal calibration 的身分閘門。它**不**做最佳化、不算目標函數、
#           不讀 calibration partition 的數值；它只回答「前提成不成立」。
#           把身分驗證與執行分開，是為了讓漂移在第一次評估之前就中止。
# 主要責任:
#   1. EXPECTED_* 常數把四個凍結雜湊寫死，任何漂移都對得出來
#   2. load_frozen_identity() 驗證全部凍結物並回傳 FrozenIdentity
#   3. stage_dimensions() 由 CAL-STAGE0-001 的 admitted 集合導出各階段維度
#   4. stage_bounds() 以 stage 0 導出的 feasible domain 覆寫登記界線
#   5. stage_declared_cells() 導出各階段被最佳化的 (class, feature) 格子
# 維護提醒:
#   - 不得在此重算 s_f。CAL-SF-001 是唯一來源，重算等於讓 optimizer 有第二個
#     可以移動的正規化尺度（CAL-PREREG-003 normalization.frozen = true）。
#   - 不得放寬任何一個 EXPECTED_* 常數去「讓它過」。雜湊不符代表凍結物或
#     程式已經漂移，正確處置是查明原因，不是改期望值。
#   - 不得在此讀取 heldout_real，也不得組出它的檔案路徑；本模組只讀
#     split_registry 的**計數**，那是 split 結構而非 recording 數值。
#   - 不得用登記界線去跑 noise_relative_sigma。stage 0 已實測其上界不可行，
#     繞過 feasible domain 等於在已知算不出來的區間裡找最小值。
#   - v0.1.0 新增：首版 formal calibration 身分閘門（CAL-PREREG-003 stage 1-5）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_identity.py -v
#   - py -3.10 -m pcmef.cli calibration formal --verify-identity
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.core.hash import hash_object

__all__ = [
    "EXPECTED_PREREG_PAYLOAD_HASH",
    "EXPECTED_STAGE0_PAYLOAD_HASH",
    "EXPECTED_SF_PAYLOAD_HASH",
    "EXPECTED_RAW_CALIBRATION_DATA_HASH",
    "STAGE_ORDER",
    "IdentityError",
    "FrozenIdentity",
    "load_frozen_identity",
    "stage_dimensions",
    "stage_bounds",
    "stage_declared_cells",
    "all_cells",
]


#: 這四個值是本次執行的身分錨點。它們**不是**設定，是斷言的對象。
#: 任何一個對不上，代表凍結物、資料或程式已經漂移，執行必須在讀任何數值
#: 之前中止（fail closed）。
EXPECTED_PREREG_PAYLOAD_HASH = (
    "fb9638d78df974ca90f16546c9aa5367371fe2d82e8284190f5f6c8ad63b70fa"
)
EXPECTED_STAGE0_PAYLOAD_HASH = (
    "810222803e6555e376e1256d6e77fa59cfd8004948af3b77657a69e99d48a0c6"
)
EXPECTED_SF_PAYLOAD_HASH = (
    "8e71171c7ae851c1e575627a78ac107b626f7c228895125b07b864454ee0096e"
)
EXPECTED_RAW_CALIBRATION_DATA_HASH = (
    "926eef729eb3b5b4762fe7a77c139f5ca89375a57367de432508dfa8a8ca1ed8"
)

#: 執行順序即 CAL-PREREG-003 stagewise 的順序，不得調換（AMD-004：
#: physical scene / media 先，final measurement mappings 後）。
STAGE_ORDER: tuple[str, ...] = (
    "SCENE_GEOMETRY_SURFACE_FOIL",
    "SCENE_PARTICIPATING_MEDIA",
    "MAPPING_AMBIENT",
    "MAPPING_SIGNAL",
    "MAPPING_SIGMA",
)


class IdentityError(RuntimeError):
    """凍結物的身分不成立；此時不得進行任何評估。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


@dataclass(frozen=True)
class FrozenIdentity:
    """本次 formal calibration 所依附的全部凍結身分。"""

    prereg: dict[str, Any]
    stage0: dict[str, Any]
    sf: dict[str, Any]
    lock_payload: dict[str, Any]
    prereg_hash: str
    stage0_hash: str
    sf_hash: str
    initial_lock_hash: str
    protocol_hash: str
    bounds_resolution_hash: str
    parameter_set_hash: str
    calibration_set_hash: str
    raw_calibration_data_hash: str
    s_f: dict[str, float]
    initial_values: dict[str, Any]
    heldout_access_count: int
    calibration_access_count: int
    protocol: dict[str, Any] = field(repr=False, default_factory=dict)

    def to_artifact(self) -> dict[str, Any]:
        """寫進每一份 artifact 的身分區塊。"""
        return {
            "preregistration_id": "CAL-PREREG-003",
            "preregistration_hash": self.prereg_hash,
            "protocol_hash": self.protocol_hash,
            "stage0_id": "CAL-STAGE0-001",
            "stage0_hash": self.stage0_hash,
            "sf_record_id": "CAL-SF-001",
            "sf_hash": self.sf_hash,
            "initial_simulation_lock_hash": self.initial_lock_hash,
            "bounds_resolution_hash": self.bounds_resolution_hash,
            "parameter_set_hash": self.parameter_set_hash,
            "calibration_set_hash": self.calibration_set_hash,
            "raw_calibration_data_hash": self.raw_calibration_data_hash,
            "s_f": dict(self.s_f),
            "heldout_access_count": self.heldout_access_count,
        }

    def identity_hash(self) -> str:
        """身分整體的雜湊，供 checkpoint 比對漂移。"""
        return hash_object(self.to_artifact())


def _read_json(path: Path, reason: str) -> dict[str, Any]:
    if not path.exists():
        raise IdentityError(reason, f"{path} does not exist")
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_payload(document: dict[str, Any], expected: str, label: str, reason: str) -> str:
    """payload 自洽 + 與寫死的期望值相符。兩道都要過。"""
    recomputed = hash_object(document["payload"])
    if recomputed != document["payload_hash"]:
        raise IdentityError(
            reason,
            f"{label} does not verify against itself: the stored payload_hash is "
            f"{document['payload_hash']} but the payload hashes to {recomputed}. "
            "The frozen record has been edited in place.",
        )
    if recomputed != expected:
        raise IdentityError(
            reason,
            f"{label} payload hash is {recomputed}, expected {expected}. This is a "
            "different record from the one this runner was written against; the "
            "expected value must not be edited to make the run proceed.",
        )
    return recomputed


def load_frozen_identity(
    freeze_dir: str | Path = "freeze", repo_root: str | Path = "."
) -> FrozenIdentity:
    """讀齊、驗證並回傳全部凍結身分。任何一項不符即 fail closed。"""
    root = Path(repo_root)
    fdir = Path(freeze_dir)

    prereg = _read_json(
        fdir / "preregistrations" / "CAL-PREREG-003.prereg.json", "FROZEN_PROTOCOL_DRIFT"
    )
    prereg_hash = _verify_payload(
        prereg, EXPECTED_PREREG_PAYLOAD_HASH, "CAL-PREREG-003", "FROZEN_PROTOCOL_DRIFT"
    )

    stage0 = _read_json(fdir / "stages" / "CAL-STAGE0-001.stage0.json", "FROZEN_PROTOCOL_DRIFT")
    stage0_hash = _verify_payload(
        stage0, EXPECTED_STAGE0_PAYLOAD_HASH, "CAL-STAGE0-001", "FROZEN_PROTOCOL_DRIFT"
    )
    if stage0["payload"]["outcome"] != "STAGE0_COMPLETE":
        raise IdentityError(
            "FROZEN_PROTOCOL_DRIFT",
            f"stage 0 outcome is {stage0['payload']['outcome']!r}, not STAGE0_COMPLETE",
        )

    sf = _read_json(fdir / "calibration" / "CAL-SF-001.sf.json", "CAL_SF_001_DRIFT")
    sf_hash = _verify_payload(sf, EXPECTED_SF_PAYLOAD_HASH, "CAL-SF-001", "CAL_SF_001_DRIFT")

    sf_payload = sf["payload"]
    if sf_payload["raw_calibration_data_hash"] != EXPECTED_RAW_CALIBRATION_DATA_HASH:
        raise IdentityError(
            "CAL_SF_001_DRIFT",
            "CAL-SF-001 carries raw_calibration_data_hash "
            f"{sf_payload['raw_calibration_data_hash']}, expected "
            f"{EXPECTED_RAW_CALIBRATION_DATA_HASH}",
        )
    s_f = {str(k): float(v) for k, v in sf_payload["s_f"].items()}
    missing = [f for f in TOF_SCHEMA if f not in s_f]
    if missing:
        raise IdentityError("CAL_SF_001_DRIFT", f"CAL-SF-001 has no s_f for {missing}")
    for feature, scale in s_f.items():
        if not (scale > 0.0) or scale != scale or scale in (float("inf"), float("-inf")):
            raise IdentityError(
                "CAL_SF_001_DRIFT",
                f"s_f[{feature}] = {scale!r} is not a finite positive scale",
            )

    # 三份記錄必須互相指認同一條血緣，否則它們描述的是不同的實驗。
    for label, payload, key, expected in (
        ("CAL-SF-001", sf_payload, "preregistration_hash", prereg_hash),
        ("CAL-SF-001", sf_payload, "stage0_hash", stage0_hash),
        ("CAL-STAGE0-001", stage0["payload"], "preregistration_hash", prereg_hash),
    ):
        if payload.get(key) != expected:
            raise IdentityError(
                "FROZEN_PROTOCOL_DRIFT",
                f"{label}.{key} is {payload.get(key)!r} but the live record hashes "
                f"to {expected}; the frozen records disagree about their own lineage",
            )

    # 工作副本的協定必須仍然雜湊成凍結時的值。
    from pcmef.experiments.calibration_prereg import load_protocol, protocol_hash

    protocol = load_protocol()
    live_protocol_hash = protocol_hash()
    if live_protocol_hash != prereg["payload"]["protocol_hash"]:
        raise IdentityError(
            "FROZEN_PROTOCOL_DRIFT",
            f"configs/calibration_preregistration.yaml now hashes to "
            f"{live_protocol_hash}, but CAL-PREREG-003 froze "
            f"{prereg['payload']['protocol_hash']}",
        )

    from pcmef.core.formal_loader import load_formal_lock
    from pcmef.core.parameters import ParameterRegistry
    from pcmef.experiments.calibration_plan import bounds_resolution_hash

    resolved = load_formal_lock("initial_simulation", freeze_dir, root)
    if resolved.payload_hash != prereg["payload"]["initial_simulation"]["lock_hash"]:
        raise IdentityError(
            "FROZEN_PROTOCOL_DRIFT",
            f"initial_simulation.lock hashes to {resolved.payload_hash} but "
            f"CAL-PREREG-003 froze {prereg['payload']['initial_simulation']['lock_hash']}",
        )

    registry = ParameterRegistry.load()
    live_bounds_hash = bounds_resolution_hash(protocol, resolved.payload, registry)
    if live_bounds_hash != prereg["payload"]["bounds_resolution_hash"]:
        raise IdentityError(
            "INVALID_BOUNDS",
            f"bounds_resolution_hash is {live_bounds_hash} but CAL-PREREG-003 froze "
            f"{prereg['payload']['bounds_resolution_hash']}; there is a second, "
            "unfrozen set of bounds",
        )

    split_registry = _read_json(
        root / "data" / "splits" / "split_registry.json", "HELDOUT_ACCESS"
    )
    heldout = int(split_registry["heldout_access_count"])
    if heldout != 0:
        raise IdentityError(
            "HELDOUT_ACCESS",
            f"heldout_access_count is {heldout}, not 0. Held-out must stay sealed "
            "until E1 final; calibration must never open it.",
        )
    if split_registry["calibration_set_hash"] != sf_payload["calibration_set_hash"]:
        raise IdentityError(
            "FROZEN_PROTOCOL_DRIFT",
            "the split registry and CAL-SF-001 disagree about calibration_set_hash",
        )

    ledger = _read_json(
        root / "data" / "splits" / "calibration_access_ledger.json", "FROZEN_PROTOCOL_DRIFT"
    )

    return FrozenIdentity(
        prereg=prereg,
        stage0=stage0,
        sf=sf,
        lock_payload=resolved.payload,
        prereg_hash=prereg_hash,
        stage0_hash=stage0_hash,
        sf_hash=sf_hash,
        initial_lock_hash=resolved.payload_hash,
        protocol_hash=live_protocol_hash,
        bounds_resolution_hash=live_bounds_hash,
        parameter_set_hash=registry.parameter_set_hash(),
        calibration_set_hash=split_registry["calibration_set_hash"],
        raw_calibration_data_hash=EXPECTED_RAW_CALIBRATION_DATA_HASH,
        s_f=s_f,
        initial_values=dict(resolved.payload["initial_parameter_values"]),
        heldout_access_count=heldout,
        calibration_access_count=int(ledger["calibration_access_count"]),
        protocol=protocol,
    )


# ---------------------------------------------------------------------------
# 各階段的搜尋空間
# ---------------------------------------------------------------------------


def stage_dimensions(identity: FrozenIdentity, stage_id: str) -> list[str]:
    """該階段真正進入 optimizer 的維度。

    來源**只有** CAL-STAGE0-001 的 `stage_status[...].admitted`，不是預註冊的
    候選清單 —— 候選是「可能會擬合的」，admitted 才是「stage 0 判定看得見的」。
    用候選清單會讓四個被 gauge-fix 的箔片參數重新變成自由度。
    """
    status = identity.stage0["payload"]["stage_status"]
    if stage_id not in status:
        raise IdentityError(
            "FROZEN_PROTOCOL_DRIFT",
            f"CAL-STAGE0-001 records no stage_status for {stage_id!r}",
        )
    return list(status[stage_id]["admitted"])


def stage_held_parameters(identity: FrozenIdentity, stage_id: str) -> list[str]:
    """該階段被固定在凍結初值的候選參數。"""
    status = identity.stage0["payload"]["stage_status"]
    return list(status[stage_id]["held_at_frozen_initial_value"])


def stage_bounds(
    identity: FrozenIdentity, dimensions: list[str]
) -> dict[str, tuple[float, float]]:
    """維度的數值界線，必要時以 stage 0 導出的 feasible domain 取代。

    `noise_relative_sigma` 的登記上界 0.5 已由 stage 0 實測為不可行，
    導出的可行上界 0.26313476562500004 寫在 CAL-STAGE0-001 裡。這裡**讀那個值**
    而不是抄一個四捨五入的常數：抄來的常數與導出的值只要差一個位元，
    執行用的界線就不在任何凍結物裡了。
    """
    payload = identity.stage0["payload"]
    registered = payload["resolved_bounds"]
    feasible = payload.get("feasible_domains") or {}

    out: dict[str, tuple[float, float]] = {}
    for name in dimensions:
        if name not in registered:
            raise IdentityError(
                "INVALID_BOUNDS", f"{name!r} has no frozen resolved bound"
            )
        lo, hi = (float(v) for v in registered[name])
        derived = feasible.get(name)
        if derived is not None:
            swept = derived.get("swept") or []
            if len(swept) != 2 or any(v is None for v in swept):
                raise IdentityError(
                    "INVALID_BOUNDS",
                    f"{name!r} has an infeasible registered bound but CAL-STAGE0-001 "
                    "records no usable derived domain",
                )
            d_lo, d_hi = float(swept[0]), float(swept[1])
            if d_lo < lo or d_hi > hi:
                raise IdentityError(
                    "INVALID_BOUNDS",
                    f"{name!r} derived domain [{d_lo}, {d_hi}] is not a subset of the "
                    f"registered range [{lo}, {hi}]",
                )
            lo, hi = d_lo, d_hi
        if not (lo < hi):
            raise IdentityError("INVALID_BOUNDS", f"{name!r} bound [{lo}, {hi}] is not lo < hi")
        out[name] = (lo, hi)
    return out


def all_cells() -> list[tuple[str, str]]:
    """全部 16 個 (class, feature) 格子，順序固定為 class -> feature。"""
    return [(c, f) for c in CLASS_ORDER for f in TOF_SCHEMA]


def stage_declared_cells(identity: FrozenIdentity, stage_id: str) -> list[tuple[str, str]]:
    """該階段**最佳化**的格子（CAL-PREREG-003 reporting.optimized_terms）。

    其餘格子仍逐次記錄，但不進該階段的目標函數 —— 那是 monitored_terms，
    也是 regression guard 盯的對象。
    """
    channels = identity.stage0["payload"]["declared_channels"]
    if stage_id not in channels:
        raise IdentityError(
            "FROZEN_PROTOCOL_DRIFT", f"no declared_channels for stage {stage_id!r}"
        )
    spec = channels[stage_id]
    cells = [
        (str(c), str(f)) for c in spec["classes"] for f in spec["features"]
    ]
    unknown = [cell for cell in cells if cell not in set(all_cells())]
    if unknown:
        raise IdentityError(
            "FROZEN_PROTOCOL_DRIFT", f"stage {stage_id!r} declares unknown cells {unknown}"
        )
    return cells
