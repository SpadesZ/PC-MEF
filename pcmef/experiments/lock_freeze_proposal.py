# PC-MEF Research System source maintenance contract
# 上下游: 讀 outputs/perception/{ds_v2,gate_validation,e2_deterministic_gate_pilot}
#         的 dataset_manifest.json、schemas/arbitration_output_v1.schema.json、
#         pcmef.core.constants 與 pcmef.perception.dataset；
#         產出 outputs/lock_proposals/freeze_proposal.json。
#         **只讀不寫 lock，且完全不觸碰 families 36-43 的任何 artifact。**
# 檔案路徑: pcmef/experiments/lock_freeze_proposal.py
# 產生時間: 2026-09-01 00:40 +08:00
# 版本: v0.1.0
# 功能說明: 把 synthetic_split_policy 與 agent_schema 兩個 immutable lock 的
#           每個欄位，從現有 canonical code / manifest / config 推導出來，
#           交給 research lead 核准。**這是 dry run，不呼叫 LockStore.write()。**
# 模組定位: freeze 前的提案產生器。它「不是」freeze 執行器 —— 本檔沒有任何
#           寫 lock 的路徑，也刻意不 import LockStore。
# 主要責任:
#   1. derive_family_rule() 從實作反推 machine-readable 的 family identity 規則
#   2. collect_family_hashes() 由既有 manifest 取 physical_scene_family，不重生資料
#   3. check_exclusivity() 證明各 partition 的 family identity 互斥
#   4. prove_final_e2_absent() 證明 36-43 尚無任何 generated artifact
#   5. derive_agent_schema() 以正式 hash 函式計算 schema hash 與 class_order
#   6. trace_support_bridge() 追 class_support 進入融合的實際 code path
# 維護提醒:
#   - 不得在本檔 import 或呼叫 LockStore.write()。提案與寫入必須分開，
#     否則「先看看推導出什麼」與「就這樣凍下去」之間沒有人為關卡。
#   - 不得為了讓欄位有值而發明 hash 語意。推導不出來就回報缺口。
#   - 不得讀取 families 36-43 的任何 observation。本檔只允許在**不產生資料**
#     的前提下計算 descriptor，且目前因 family_domain 未定而不計算。
#   - v0.1.0 新增：首版 freeze proposal，決策見 STATUS。
# 驗證方式:
#   - py -3.10 -m pcmef.cli locks propose-agent-locks
# ------------------------------------------------------------

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.core.hash import hash_file, hash_object
from pcmef.core.numeric import ARBITRATION_SUPPORT_BRIDGE_VERSION

__all__ = ["build_proposal", "FINAL_E2_FAMILIES"]

#: 保留給 final Formal E2 的 family 索引。本檔只用它來**證明沒有資料**，
#: 不用它產生任何東西。
FINAL_E2_FAMILIES: tuple[int, ...] = tuple(range(36, 44))

#: 各 partition 的 manifest 與歷史用途。用途字串刻意保留歷史真相 ——
#: ds_v2 的 test 是 diagnostic，families 28-35 是 pilot，兩者都已被看過，
#: 不得改名成 final benchmark。
PARTITIONS: tuple[tuple[str, str, str], ...] = (
    ("ds_v2", "outputs/perception/ds_v2/dataset_manifest.json",
     "PERCEPTION_TRAIN_VAL_DIAGNOSTIC"),
    ("gate_validation", "outputs/perception/gate_validation/dataset_manifest.json",
     "MODEL_GATE_VALIDATION"),
    ("e2_deterministic_gate_pilot",
     "outputs/perception/e2_deterministic_gate_pilot/dataset_manifest.json",
     "E2_DETERMINISTIC_GATE_PILOT_ALREADY_SEEN"),
)

RESERVED_PURPOSE = "RESERVED_UNOPENED_FINAL_E2"

#: 被剔除的重複身分 family 在 mapping 裡的標記（NOTE-048）。
EXCLUDED_PURPOSE = "EXCLUDED_DUPLICATE_IDENTITY"

#: Final E2 的 family_domain（教授裁決 2026-09-01，NOTE-048）：
#: max_reserved_family_index + 1 = 44。由保留區間直接推得，沒有選擇空間。
FINAL_E2_DOMAIN = 44


def _code_hash(function: Any) -> dict[str, str]:
    """一個函式的來源位置與原始碼雜湊，供 lock 指認「當時的規則長什麼樣」。"""
    source_file = Path(inspect.getsourcefile(function)).as_posix()
    repo_relative = source_file.split("pcmef-research/")[-1]
    return {
        "source_file": repo_relative,
        "function": function.__qualname__,
        "code_sha256": hash_object(inspect.getsource(function)),
    }


def derive_family_rule() -> dict[str, Any]:
    """從實作反推 physical family identity 的 machine-readable 規則。"""
    from pcmef.perception.dataset import _PHYSICAL_KEYS, physical_scene_family

    return {
        "rule_id": "physical_scene_family_v1",
        "statement": (
            "A physical scene family is the SHA-256 (canonical JSON) of an "
            "allow-listed subset of scene_parameters. Two scenarios sharing this "
            "hash are two Monte-Carlo realizations of the SAME physical scene."
        ),
        "included_fields": list(_PHYSICAL_KEYS),
        "excluded_fields_and_why": {
            "seed": "Monte-Carlo realization index, not physical state",
            "spp": "numerical integration setting",
            "tof_render_spp": "numerical integration setting",
            "rgb_render_spp": "numerical integration setting",
            "resolution": "discretisation setting",
            "temporal_bins": "discretisation setting",
            "max_depth": "transport truncation setting",
        },
        "hash_function": "pcmef.core.hash.hash_object (SHA-256 over canonical JSON)",
        "implementation": _code_hash(physical_scene_family),
        "note": (
            "Raising RGB spp leaves the family identity unchanged by construction; "
            "that is the property the allow-list exists to guarantee."
        ),
    }


def collect_family_hashes() -> dict[str, Any]:
    """由既有 manifest 取出每個 (partition, class, family) 的 identity。不重生資料。"""
    partitions: dict[str, Any] = {}
    for name, path, purpose in PARTITIONS:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = manifest.get("samples") or manifest.get("rows") or []
        families: dict[str, str] = {}
        realizations: dict[str, int] = {}
        for row in rows:
            key = f"{row['class_label']}|{row['family_index']}"
            identity = row["physical_scene_family"]
            if key in families and families[key] != identity:
                raise ValueError(
                    f"{name}: {key} has two different physical_scene_family values; "
                    "a family must be one physical scene"
                )
            families[key] = identity
            realizations[key] = realizations.get(key, 0) + 1
        # gate-validation 另記 corrective exclusion 之後的 effective set。
        # 只留原始 32-family hash 會讓 lock 指向一個**不是實際擬合所用**的集合 ——
        # gate threshold 是在 31 family / 93 sample 上定的，那才是它的來源。
        effective: dict[str, Any] | None = None
        if name == "gate_validation":
            gate_rule = json.loads(
                Path("outputs/perception/gate/gate_rule.json").read_text(encoding="utf-8")
            )
            excluded = {
                (e["class_label"], e["family_index"])
                for e in gate_rule.get("family_exclusion", {}).get("excluded_families", [])
            }
            kept = {
                key: identity for key, identity in sorted(families.items())
                if (key.split("|")[0], int(key.split("|")[1])) not in excluded
            }
            kept_samples = sum(
                count for key, count in realizations.items()
                if (key.split("|")[0], int(key.split("|")[1])) not in excluded
            )
            effective = {
                "reason": "corrective exclusion of duplicate identity (NOTE-048)",
                "excluded_families": sorted(
                    f"{c}|{f}" for c, f in excluded
                ),
                "n_families": len(kept),
                "n_samples": kept_samples,
                "families": kept,
                "families_hash": hash_object(kept),
            }

        partitions[name] = {
            "manifest": path,
            "effective_set_after_exclusion": effective,
            "historical_purpose": purpose,
            "family_domain": manifest.get("family_domain"),
            "family_indices": sorted({r["family_index"] for r in rows}),
            "realizations_per_family": sorted(set(realizations.values())),
            "n_samples": len(rows),
            "n_families": len(families),
            "families": dict(sorted(families.items())),
            "families_hash": hash_object(dict(sorted(families.items()))),
        }
    return partitions


def check_exclusivity(partitions: dict[str, Any]) -> dict[str, Any]:
    """各 partition 的 family identity 與 family_index 必須兩兩互斥。"""
    identity_owner: dict[str, list[str]] = {}
    index_owner: dict[int, list[str]] = {}
    for name, data in partitions.items():
        for key, identity in data["families"].items():
            identity_owner.setdefault(identity, []).append(f"{name}:{key}")
        for index in data["family_indices"]:
            index_owner.setdefault(index, []).append(name)

    shared_identity = {k: v for k, v in identity_owner.items() if len(v) > 1}
    shared_index = {
        k: sorted(set(v)) for k, v in index_owner.items() if len(set(v)) > 1
    }
    return {
        "distinct_family_identities": len(identity_owner),
        "identity_collisions_across_partitions": shared_identity,
        "family_index_collisions_across_partitions": shared_index,
        "mutually_exclusive": not shared_identity and not shared_index,
    }


def prove_final_e2_absent(out_root: str | Path = "outputs") -> dict[str, Any]:
    """證明 families 36-43 尚無任何 generated sample artifact。

    以檔案系統實測，不以推論。掃描的是**目錄名**，因此不需要開啟任何檔案，
    也就不可能在證明「沒有資料」的過程中讀到資料。
    """
    root = Path(out_root)
    patterns = [f"*_f{index:02d}_r*" for index in FINAL_E2_FAMILIES]
    found: list[str] = []
    for pattern in patterns:
        found.extend(p.as_posix() for p in root.rglob(pattern))
    manifest_mentions: list[str] = []
    for name, path, _purpose in PARTITIONS:
        indices = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = indices.get("samples") or indices.get("rows") or []
        if any(r.get("family_index") in FINAL_E2_FAMILIES for r in rows):
            manifest_mentions.append(name)
    return {
        "families": list(FINAL_E2_FAMILIES),
        "sample_directories_found": found,
        "manifests_containing_them": manifest_mentions,
        "generated": bool(found or manifest_mentions),
        "method": "directory-name scan under outputs/; no sample file was opened",
    }


def probe_family_domain_aliasing(kwargs_domain: int = 36) -> dict[str, Any]:
    """檢查沿用既有 family_domain 會不會讓 36-43 與既有 family 撞在一起。

    這不是理論疑慮：family_variation 以 `% n_families` 取值，因此
    index >= n_families 會繞回去。
    """
    from pcmef.core.constants import CLASS_ORDER
    from pcmef.simulation.paired import family_variation

    domain = kwargs_domain
    aliases: dict[str, Any] = {}
    for class_label in CLASS_ORDER:
        for index in FINAL_E2_FAMILIES:
            value = family_variation(class_label, index, domain).to_dict()
            collides = [
                existing for existing in range(domain)
                if family_variation(class_label, existing, domain).to_dict() == value
            ]
            if collides:
                aliases[f"{class_label}|{index}"] = collides
    return {
        "tested_family_domain": domain,
        "aliases_onto_existing_families": aliases,
        "safe": not aliases,
        "why_it_matters": (
            "family_variation normalises by `% n_families`, so a family index at "
            "or above the domain wraps around and reproduces an EARLIER family's "
            "nuisance values exactly. Those scenes would then share the physical "
            "family identity of ds_v2 training families, making Formal E2 a "
            "re-run of seen physical scenes under new seeds rather than a "
            "generalisation test."
        ),
    }


def derive_final_e2_descriptors() -> dict[str, Any]:
    """以 domain=44 計算 families 36-43 的 physical-family descriptor。

    **不 render、不產生任何 observation。** scene_parameters 的六個物理欄位
    全部來自 frozen SimulatorIdentity 與 deterministic 的 family_variation，
    render 產物不在 identity 之內，因此 descriptor 算得出來而資料仍是 sealed。

    同時證明離線推導與實際 manifest 等價：拿既有 partition 重算一次比對，
    對不上就不能信任 36-43 那 32 個值。
    """
    from pcmef.core.constants import CLASS_ORDER
    from pcmef.perception.dataset import physical_scene_family
    from pcmef.simulation.paired import (
        _scenario_config, family_variation, load_calibrated_simulator,
    )

    identity, _calibration = load_calibrated_simulator("freeze", ".")

    def descriptor(class_label: str, family_index: int, domain: int) -> str:
        variation = family_variation(class_label, family_index, domain)
        config = _scenario_config(identity, class_label, seed=0, variation=variation)
        return physical_scene_family({
            **config.to_dict(),
            "scene_constants_applied": dict(identity.scene_constants),
        })

    # 等價性證明：離線重算既有 partition，必須逐一對上 manifest。
    equivalence = {"checked": 0, "mismatches": []}
    for name, path, _purpose in PARTITIONS:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        domain = manifest.get("family_domain") or 20
        seen: dict[tuple[str, int], str] = {}
        for row in manifest["samples"]:
            seen.setdefault((row["class_label"], row["family_index"]),
                            row["physical_scene_family"])
        for (class_label, family_index), stored in seen.items():
            equivalence["checked"] += 1
            if descriptor(class_label, family_index, domain) != stored:
                equivalence["mismatches"].append(f"{name}:{class_label}|{family_index}")
    equivalence["proven"] = not equivalence["mismatches"]

    existing = {
        cls: {
            **{f"ds_v2:{f}": descriptor(cls, f, 20) for f in range(20)},
            **{f"d36:{f}": descriptor(cls, f, 36) for f in range(20, 36)},
        }
        for cls in CLASS_ORDER
    }
    descriptors, collisions = {}, {}
    for cls in CLASS_ORDER:
        for family_index in FINAL_E2_FAMILIES:
            value = descriptor(cls, family_index, FINAL_E2_DOMAIN)
            descriptors[f"{cls}|{family_index}"] = value
            hits = [k for k, v in existing[cls].items() if v == value]
            if hits:
                collisions[f"{cls}|{family_index}"] = hits
    return {
        "family_domain": FINAL_E2_DOMAIN,
        "domain_derivation": "max_reserved_family_index + 1 = 43 + 1 = 44",
        "rendered": False,
        "descriptors": dict(sorted(descriptors.items())),
        "descriptors_hash": hash_object(dict(sorted(descriptors.items()))),
        "collisions_with_existing_same_class": collisions,
        "zero_collision": not collisions,
        "offline_derivation_equivalence": equivalence,
        "simulator_identity_hash": identity.identity_hash(),
        "calibrated_simulation_lock_hash": identity.calibrated_lock_hash,
    }


def derive_agent_schema() -> dict[str, Any]:
    from pcmef.core.constants import CLASS_ORDER

    schema_path = Path("schemas/arbitration_output_v1.schema.json")
    return {
        "arbitration_schema_sha256": hash_file(schema_path),
        "arbitration_schema_path": schema_path.as_posix(),
        "hash_function": "pcmef.core.hash.hash_file (SHA-256 over file bytes)",
        "class_order": list(CLASS_ORDER),
        "class_order_source": "pcmef.core.constants.CLASS_ORDER",
        "support_bridge_version": ARBITRATION_SUPPORT_BRIDGE_VERSION,
        "support_bridge_version_source": (
            "pcmef.core.numeric.ARBITRATION_SUPPORT_BRIDGE_VERSION"
        ),
    }


def trace_support_bridge() -> dict[str, Any]:
    """追 class_support 進入 PC-MEF 融合的實際 code path。"""
    from pcmef.core.constants import AGENT_SUPPORT_MAX, AGENT_SUPPORT_MIN, EPS_S
    from pcmef.core.numeric import normalize_support, support_to_vector

    fusion_module_exists = Path("pcmef/fusion").exists()
    return {
        "stage_1_json_to_vector": {
            **_code_hash(support_to_vector),
            "input": "arbitration_output_v1.class_support (keyed object, 0-100)",
            "output": "a_A : float64[4] indexed by CLASS_ORDER",
            "semantics": (
                "explicit key lookup by CLASS_ORDER, never dict iteration order; "
                "rejects all-zero, NaN/Inf and out-of-range as semantic failure"
            ),
            "accepted_range": [AGENT_SUPPORT_MIN, AGENT_SUPPORT_MAX],
        },
        "stage_2_normalisation": {
            **_code_hash(normalize_support),
            "input": "a_A : float64[4]",
            "output": "s_A : Normalized Evidence-Support Score, sums to 1",
            "formula": "s_A = (a_A + EPS_S) / sum(a_A + EPS_S)   [SRC-PLAN eq. (2)]",
            "eps_s": EPS_S,
            "is_it_divide_by_100": False,
            "note": (
                "s_A is explicitly NOT a calibrated posterior. Normalisation is "
                "scale-invariant apart from the EPS_S offset, so dividing by 100 "
                "first is neither required nor equivalent."
            ),
        },
        "stage_3_fusion_with_p_rel": {
            "documented_formula": "F = (1 - g) * p_rel + g * s_A",
            "documented_in": "pcmef/core/inference_payload.py (comment only)",
            "implemented": False,
            "fusion_module_exists": fusion_module_exists,
            "blocking": [
                "no pcmef/fusion package exists; numeric.py's header names "
                "fusion.pcmef and fusion.support_bridge as consumers that were "
                "never written",
                "no function computes F anywhere in the codebase",
                "g depends on gate.alpha / gate.beta / gate.gamma, which are "
                "still unresolved advisor values (config check lists them)",
            ],
        },
        "class_selection": {
            "implemented": False,
            "note": "argmax over F is the intent, but F does not exist yet",
        },
        "existing_version_identifier": None,
        "verdict": "SUPPORT_BRIDGE_UNDEFINED",
        "verdict_reason": (
            "Stages 1 and 2 are explicit, unique and hashable. Stage 3 is not "
            "implemented and is blocked on undecided gate coefficients, and no "
            "version identifier for the bridge exists anywhere in code, config "
            "or spec text. Naming it 'v1' would invent a semantics that the "
            "codebase does not yet have."
        ),
    }


def build_proposal(out_dir: str | Path = "outputs/lock_proposals") -> dict[str, Any]:
    partitions = collect_family_hashes()
    exclusivity = check_exclusivity(partitions)
    absence = prove_final_e2_absent()
    aliasing = probe_family_domain_aliasing(36)   # 沿用舊 domain 的反例
    final_e2 = derive_final_e2_descriptors()      # 核定的 domain=44
    agent_schema = derive_agent_schema()
    bridge = trace_support_bridge()

    assignment: dict[str, str] = {}
    for name, data in partitions.items():
        for key in data["families"]:
            assignment[key] = data["historical_purpose"]
    for index in FINAL_E2_FAMILIES:
        for class_label in agent_schema["class_order"]:
            assignment[f"{class_label}|{index}"] = RESERVED_PURPOSE
    # 被剔除的重複身分保留在 mapping 裡並改標記，不假裝它不存在（NOTE-048）。
    gate_rule = json.loads(
        Path("outputs/perception/gate/gate_rule.json").read_text(encoding="utf-8")
    )
    excluded = gate_rule.get("family_exclusion", {}).get("excluded_families", [])
    for entry in excluded:
        assignment[f"{entry['class_label']}|{entry['family_index']}"] = EXCLUDED_PURPOSE
    ordered_assignment = dict(sorted(assignment.items()))

    # 互斥性判定要把「已剔除」的那一個排除在外：它已經不參與擬合，
    # 但仍留在 mapping 裡當歷史紀錄。
    excluded_keys = {f"{e['class_label']}|{e['family_index']}" for e in excluded}
    live_collisions = {
        h: owners for h, owners in
        exclusivity["identity_collisions_across_partitions"].items()
        if not any(
            owner.split(":", 1)[1] in excluded_keys for owner in owners
        )
    }
    split_ready = (
        not live_collisions
        and not absence["generated"]
        and final_e2["zero_collision"]
        and final_e2["offline_derivation_equivalence"]["proven"]
    )
    document = {
        "report_id": "lock_freeze_proposal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scientific_result": False,
        "dry_run": True,
        "locks_written": [],
        "synthetic_split_policy": {
            "parent_scene_family_rule": derive_family_rule(),
            "family_hashes": {
                "partitions": partitions,
                "final_e2_families": final_e2,
            },
            "split_assignment": ordered_assignment,
            "split_assignment_hash": hash_object(ordered_assignment),
            "exclusivity": exclusivity,
            "excluded_families": excluded,
            "live_identity_collisions_after_exclusion": live_collisions,
            "final_e2_family_descriptors": final_e2,
            "final_e2_absence_proof": absence,
            "family_domain_aliasing_probe": aliasing,
        },
        "agent_schema": {**agent_schema, "support_bridge_semantics": bridge},
        "SAFE_TO_FREEZE_SYNTHETIC_SPLIT_POLICY": "YES" if split_ready else "NO",
        "SAFE_TO_FREEZE_AGENT_SCHEMA": (
            "YES" if agent_schema["support_bridge_version"] else "NO"
        ),
        "FINAL_E2_36_43_GENERATED": "YES" if absence["generated"] else "NO",
        "FINAL_E2_36_43_READ": "NO",
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "freeze_proposal.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document
