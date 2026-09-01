# PC-MEF Research System source maintenance contract
# 上下游: 讀原 freeze/ 的 22 個 lock 與 amendments/errata/preregistrations/stages/
#         calibration/ 子目錄，讀 experiments.corrective_pass 的更正結果；
#         寫出 freeze/runs/<run_id>/ 的完整 formal run 目錄與
#         corrective_lineage.json。**不刪除、不覆寫原 freeze/ 的任何檔案。**
# 檔案路徑: pcmef/experiments/corrective_run.py
# 產生時間: 2026-09-01 16:40 +08:00
# 版本: v0.1.0
# 功能說明: 建立 pre-final corrective formal run —— 未變動的 lock 逐位元延用
#           （雜湊不變即為「未重新裁決」的機器證據），只有真正受影響的
#           identity 以新內容寫入。
# 模組定位: 「開新 run」機制的實作。它「不是」erratum（勘誤層硬性拒絕
#           parameter 變更），也「不是」覆寫 —— 原 lineage 完整保留在 freeze/。
# 主要責任:
#   1. carry_forward() 逐位元複製未受影響的 lock 並驗證雜湊不變
#   2. write_corrected_locks() 只寫 reliability_final 與 gate
#   3. build_lineage() 記錄 parent / superseded / carried-forward 與兩條理由
#   4. freeze_corrected_formal_config() 在 executor 就緒後最後凍結
# 維護提醒:
#   - 不得把未受影響的 lock 以「重算」的方式寫進新 run。必須是複製，
#     且複製後雜湊必須與 parent 相同 —— 那是「沒有被重新裁決」唯一的證據。
#   - 不得在本檔刪除或改動 parent freeze/ 的任何檔案。
#   - 不得在 executor 尚未通過驗證時凍結 formal_config（AMD-006 reason B）。
#   - v0.1.0 新增：首版 corrective run，決策見 NOTE-051 與 AMD-006。
# 驗證方式:
#   - py -3.10 -m pcmef.cli corrective open-run
#   - py -3.10 -m pcmef.cli locks status --freeze-dir freeze/runs/PFC-001
# ------------------------------------------------------------

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pcmef.core.hash import hash_object
from pcmef.core.locks import LOCK_SPECS, LockError, LockStore

__all__ = [
    "CorrectiveRunError",
    "RUN_ID",
    "SUPERSEDED",
    "open_corrective_run",
    "freeze_corrected_formal_config",
]

#: 本次更正 run 的識別。PFC = Pre-Final Corrective。
RUN_ID = "PFC-001"

#: 真正受影響、必須以新內容重寫的 lock。其餘一律逐位元延用。
#:
#: gate 在此不是因為任何 gate 數值改變（重跑同一套搜尋在 effective 93 上
#: 逐位元重現了原值），而是因為它的 payload 交叉引用 reliability_config_hash。
SUPERSEDED: tuple[str, ...] = ("reliability_final", "gate", "formal_config")

#: 隨 run 一起延用的證據子目錄。它們是 append-only 記錄，複製不改變身分。
EVIDENCE_DIRS: tuple[str, ...] = (
    "amendments", "errata", "preregistrations", "stages", "calibration",
)

CORRECTIVE_AMENDMENT_ID = "AMD-006"


class CorrectiveRunError(RuntimeError):
    """更正 run 的前提不成立。"""


def _run_dir(freeze_dir: str | Path, run_id: str) -> Path:
    return Path(freeze_dir) / "runs" / run_id


def carry_forward(
    parent: Path, target: Path, progress: Callable[[str], None] | None = None
) -> dict[str, str]:
    """逐位元複製未受影響的 lock，並驗證雜湊與 parent 相同。"""
    say = progress or (lambda _m: None)
    parent_store, target_store = LockStore(parent), LockStore(target)
    carried: dict[str, str] = {}
    for name in LOCK_SPECS:
        if name in SUPERSEDED:
            continue
        source = parent_store.path_for(name)
        if not source.exists():
            raise CorrectiveRunError(
                f"parent lineage is incomplete: {name} is missing at {source}"
            )
        before = parent_store.load_hash(name)
        shutil.copy2(source, target_store.path_for(name))
        after = target_store.load_hash(name)
        if before != after:
            raise CorrectiveRunError(
                f"{name} changed hash while being carried forward "
                f"({before} -> {after}); a carried-forward lock must be identical"
            )
        carried[name] = after
    say(f"  carried forward {len(carried)} lock(s) with unchanged hashes")
    return carried


def _corrected_reliability(
    parent: Path, corrective: dict[str, Any]
) -> dict[str, Any]:
    """以更正後的 anchors 重建 reliability_final payload。

    只換 fitted_statistics 與描述擬合 pool 的欄位；演算法、feature schema、
    禁止特徵、code hash 一律沿用 parent —— 那些本來就沒有改變。
    """
    payload = dict(LockStore(parent).load("reliability_final"))
    after = corrective["reliability"]["after_effective_93"]
    exclusion = corrective["reliability"]["exclusion"]

    payload["fitted_statistics"] = after["anchors"]
    payload["anchor_fit_pool"] = {
        "n_families": exclusion["n_families_effective"],
        "n_samples": exclusion["n_samples_effective"],
        "pool": "effective clean gate-validation, AFTER the NOTE-048 exclusion",
        "excluded_families": exclusion["excluded_families"],
        "effective_pool_hash": exclusion["effective_pool_hash"],
        "boundary": (
            "the anchors are now fitted on the same effective pool that the "
            "temperature scaling, the severity ladder, the stress set and the "
            "gate threshold search already used. The previous lineage fitted "
            "them on all 96 samples including Empty f27; that defect is "
            "superseded by AMD-006, not hidden."
        ),
        "refit_forbidden": True,
    }
    payload["deterministic_reproduction"] = {
        "routes_over_effective_pool": after["route_counts_over_effective_93"],
        "method": (
            "recomputed from the frozen ds_v2 checkpoints and the frozen gate "
            "rule; only the fitting pool changed"
        ),
    }
    payload["corrective_lineage"] = {
        "run_id": RUN_ID,
        "amendment": CORRECTIVE_AMENDMENT_ID,
        "supersedes_hash": LockStore(parent).load_hash("reliability_final"),
        "previous_anchor_fit_pool_n_samples":
            corrective["reliability"]["before_original_96"]["fitted_on_n"],
        "previous_anchors":
            corrective["reliability"]["before_original_96"]["anchors"],
        "previous_route_counts":
            corrective["reliability"]["before_original_96"][
                "route_counts_over_effective_93"
            ],
        "cases_whose_route_changed": corrective["reliability"][
            "cases_whose_route_changed"
        ],
        "reason": (
            "NOTE-048's duplicate-identity exclusion was not applied to the "
            "reliability fit (AMD-006 reason A). Same algorithm, same features, "
            "same anchor rule, different pool."
        ),
    }
    return payload


def _corrected_gate(
    parent: Path, corrective: dict[str, Any], reliability_payload: dict[str, Any]
) -> dict[str, Any]:
    """gate payload 只換 reliability_config_hash 與 lineage；門檻一律不動。"""
    store = LockStore(parent)
    payload = dict(store.load("gate"))
    recomputed = corrective["gate"]["recomputed"]
    for field, value in recomputed.items():
        if abs(float(payload[field]) - float(value)) > 1e-12:
            raise CorrectiveRunError(
                f"gate.{field} moved during the corrective pass "
                f"({payload[field]} -> {value}); this correction must not change "
                "any gate value"
            )
    payload["reliability_config_hash"] = hash_object(reliability_payload)
    payload["corrective_lineage"] = {
        "run_id": RUN_ID,
        "amendment": CORRECTIVE_AMENDMENT_ID,
        "supersedes_hash": store.load_hash("gate"),
        "reason": (
            "no gate value changed. Re-running the same search procedure on the "
            "effective-93 stress set reproduced every threshold exactly, because "
            "the gate search never consumed the reliability anchors. This lock "
            "takes a new identity only because it cross-references "
            "reliability_config_hash, which did change."
        ),
        "values_unchanged": True,
        "search_reproduced_frozen_gate": corrective["gate"]["reproduces_frozen_gate"],
    }
    return payload


def open_corrective_run(
    parent_freeze_dir: str | Path = "freeze",
    run_id: str = RUN_ID,
    corrective_report: str | Path = "outputs/corrective/pre_final_corrective.json",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """建立更正 run：延用未變動的 lock，寫入更正後的 reliability_final 與 gate。

    **不寫 formal_config** —— 依 AMD-006 reason B，它必須等 executor 就緒。
    """
    say = progress or (lambda _m: None)
    parent = Path(parent_freeze_dir)
    target = _run_dir(parent, run_id)

    corrective = json.loads(Path(corrective_report).read_text(encoding="utf-8"))
    if corrective["FINAL_E2_36_43_TOUCHED"] != "NO":
        raise CorrectiveRunError(
            "the corrective report says families 36-43 were touched; a correction "
            "made after seeing the final partition is not a correction"
        )
    if corrective["conflict_operational_delta"]["changed"]:
        raise CorrectiveRunError(
            "delta changed, so conflict_operational must also be superseded; "
            "update SUPERSEDED before continuing"
        )

    if target.exists() and any(target.glob("*.lock.json")):
        raise CorrectiveRunError(
            f"corrective run {run_id} already has locks at {target}; runs are "
            "immutable -- open a new run id instead of reusing this one"
        )
    target.mkdir(parents=True, exist_ok=True)

    for name in EVIDENCE_DIRS:
        source = parent / name
        if source.exists():
            shutil.copytree(source, target / name, dirs_exist_ok=True)
    say(f"  copied {len(EVIDENCE_DIRS)} evidence director(ies)")

    amendment_path = target / "amendments" / f"{CORRECTIVE_AMENDMENT_ID}.amendment.json"
    if not amendment_path.exists():
        raise CorrectiveRunError(
            f"{CORRECTIVE_AMENDMENT_ID} must be frozen before the corrective run "
            f"is opened; expected it at {parent / 'amendments'}"
        )

    carried = carry_forward(parent, target, say)

    reliability_payload = _corrected_reliability(parent, corrective)
    gate_payload = _corrected_gate(parent, corrective, reliability_payload)

    store = LockStore(target)
    superseded_hashes, new_hashes = {}, {}
    parent_store = LockStore(parent)
    for name, payload in (
        ("reliability_final", reliability_payload), ("gate", gate_payload)
    ):
        superseded_hashes[name] = parent_store.load_hash(name)
        store.write(name, payload)
        new_hashes[name] = store.load_hash(name)
        say(f"  superseded {name}: {superseded_hashes[name][:16]} -> {new_hashes[name][:16]}")
    superseded_hashes["formal_config"] = parent_store.load_hash("formal_config")

    lineage = {
        "run_id": run_id,
        "run_kind": "PRE_FINAL_CORRECTIVE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parent_freeze_dir": parent.as_posix(),
        "parent_lineage_retained": True,
        "amendment": CORRECTIVE_AMENDMENT_ID,
        "reasons": {
            "A": (
                "NOTE-048's duplicate-identity exclusion was applied by "
                "run_gate_validation but not by the reliability fit, so the "
                "anchors were fitted on 96 samples including Empty f27 while "
                "every other stage used the effective 93."
            ),
            "B": (
                "formal_config was frozen before any executor implemented the "
                "decision path named in gate.lock; the only runner available "
                "used a deterministic arbiter substitute and self-reported "
                "llm_arm_evaluated: false."
            ),
        },
        "correction_did_not_use_final_e2": True,
        "final_e2_36_43_generated": "NO",
        "final_e2_36_43_read": "NO",
        "superseded_lock_hashes": superseded_hashes,
        "new_lock_hashes": new_hashes,
        "carried_forward_lock_hashes": carried,
        "carried_forward_semantics": (
            "byte-identical copies of the parent lock files. Their hashes are "
            "unchanged, which is the machine-checkable form of 'not re-decided'."
        ),
        "formal_config_status": "DEFERRED_UNTIL_EXECUTOR_VALIDATED",
    }
    (target / "corrective_lineage.json").write_text(
        json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return lineage


def freeze_corrected_formal_config(
    freeze_dir: str | Path,
    code_revision: str,
    executor_validation: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """在 executor 驗證通過後，最後凍結更正後的 formal_config。"""
    from pcmef.core.amendments import amendment_provenance
    from pcmef.core.hash import hash_file
    from pcmef.experiments.final_preflight import derive_final_e2_scenarios

    say = progress or (lambda _m: None)
    target = Path(freeze_dir)
    store = LockStore(target)
    lineage = json.loads(
        (target / "corrective_lineage.json").read_text(encoding="utf-8")
    )

    if not executor_validation.get("all_passed"):
        raise CorrectiveRunError(
            "refusing to freeze formal_config: the executor validation did not "
            f"pass ({executor_validation.get('failed')})"
        )

    scenarios, gaps = derive_final_e2_scenarios()
    if gaps:
        raise CorrectiveRunError(f"Final E2 identity derivation reported gaps: {gaps}")

    parent = Path(lineage["parent_freeze_dir"])
    previous = dict(LockStore(parent).load("formal_config"))
    payload = dict(previous)
    payload.update(
        {
            "resolved_config_sha256": hash_file(Path("configs/base.yaml")),
            "code_revision": code_revision,
            "code_revision_dirty": False,
            "reliability_hash": store.load_hash("reliability_final"),
            "gate": {
                "lock_hash": store.load_hash("gate"),
                "decision_bridge_version": store.load("gate")["decision_bridge_version"],
                "routing_policy_version": store.load("gate")["routing_policy_version"],
            },
            "scenario_set_hash": scenarios["scenario_set_hash"],
            "executor": {
                "entry_point": "pcmef.experiments.e2_formal.run_formal_e2_full",
                "validated": True,
                "validation": executor_validation,
                "superseded_entry_point": (
                    "pcmef.perception.gate.run_formal_e2 -- deterministic arbiter "
                    "substitute, llm_arm_evaluated false; must not be used for "
                    "Full PC-MEF"
                ),
            },
            "corrective_lineage": {
                "run_id": lineage["run_id"],
                "amendment": lineage["amendment"],
                "parent_freeze_dir": lineage["parent_freeze_dir"],
                "supersedes_hash": lineage["superseded_lock_hashes"]["formal_config"],
                "superseded_lock_hashes": lineage["superseded_lock_hashes"],
                "reasons": lineage["reasons"],
            },
            "FINAL_E2_36_43_GENERATED": "NO",
            "FINAL_E2_36_43_READ": "NO",
            **amendment_provenance(target),
        }
    )
    store.write("formal_config", payload)
    say(f"  frozen formal_config {store.load_hash('formal_config')[:16]}")

    before = store.load_hash("formal_config")
    store.write("formal_config", payload)
    if store.load_hash("formal_config") != before:
        raise CorrectiveRunError("formal_config is not idempotent")

    lineage["formal_config_status"] = "FROZEN"
    lineage["new_lock_hashes"]["formal_config"] = before
    lineage["code_revision"] = code_revision
    (target / "corrective_lineage.json").write_text(
        json.dumps(lineage, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {"formal_config_hash": before, "lineage": lineage}
