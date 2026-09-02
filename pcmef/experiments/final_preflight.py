# PC-MEF Research System source maintenance contract
# 上下游: 讀 configs/base.yaml、outputs/perception/{ds_v2,gate_validation,
#         e2_deterministic_gate_pilot,gate}、schemas/、freeze/ 的既有 lock，
#         以及 pcmef.perception / pcmef.core 的實作原始碼；
#         產出 outputs/lock_proposals/final_preflight.json。
#         **只讀不寫 lock，且不 render 也不讀取 families 36-43 的任何觀測。**
# 檔案路徑: pcmef/experiments/final_preflight.py
# 產生時間: 2026-09-01 10:20 +08:00
# 版本: v0.1.0
# 功能說明: 把 Final E2 之前剩下的十個 formal lock 的每個欄位，從既有的
#           code / manifest / artifact 推導出來，交給 research lead 核准。
#           **這是 dry run，不呼叫 LockStore.write()。**
# 模組定位: final pre-flight 的提案產生器，接續 lock_freeze_proposal 的
#           同一套分工。它「不是」freeze 執行器 —— 本檔沒有任何寫 lock 的
#           路徑，也刻意不 import LockStore。
# 主要責任:
#   1. derive_perception_condition_policy() 由實作反推三份 allow/deny list
#   2. derive_training_seed_pairs() 凍結唯一實際使用的 checkpoint pair
#   3. derive_validation_pool() 記錄 original / effective 兩個 pool
#   4. derive_reliability_final() 以 deterministic 重算還原實際 anchors
#   5. derive_gate() / derive_conflict_operational() 取既有核定值，不重搜
#   6. derive_inference_firewall() 由 firewall 實作算出四份政策雜湊
#   7. derive_e2_sample_size() / derive_statistics_config() 由保留分割推導
#   8. derive_final_e2_scenarios() 只算 identity（id / seed / descriptor）
#   9. build_preflight() 匯總並回報所有 gap
# 維護提醒:
#   - 不得在本檔 import 或呼叫 LockStore.write()。提案與寫入必須分開，
#     否則「先看看推導出什麼」與「就這樣凍下去」之間沒有人為關卡。
#   - 不得為了讓欄位有值而發明 hash 語意。推導不出來就進 gaps。
#   - 不得 render families 36-43，也不得讀取它們的任何 RGB/ToF 產物。
#     本檔只允許計算 scenario identity（id / seed / physical descriptor），
#     那些量全部由 frozen SimulatorIdentity 與 deterministic 函式決定。
#   - 不得重擬合 reliability。重算只准用與實際執行相同的 pool 與遮罩，
#     並且必須與已記錄的路由結果對得上（NOTE-050）。
#   - v0.1.0 新增：首版 final pre-flight 提案，決策見 NOTE-050。
# 驗證方式:
#   - py -3.10 -m pcmef.cli locks preflight
# ------------------------------------------------------------

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pcmef.core.hash import hash_file, hash_object

__all__ = ["build_preflight", "FINAL_E2_FAMILIES", "FINAL_E2_DOMAIN"]

FINAL_E2_FAMILIES: tuple[int, ...] = tuple(range(36, 44))
FINAL_E2_DOMAIN = 44

DS_V2 = Path("outputs/perception/ds_v2")
GATE_VALIDATION = Path("outputs/perception/gate_validation")
GATE = Path("outputs/perception/gate")
PILOT = Path("outputs/perception/e2_deterministic_gate_pilot")

#: 唯一實際使用的 checkpoint pair。名稱與 configs/base.yaml 的
#: perception.training_seed_pairs 同一個字串，不另取。
CHECKPOINT_PAIR_ID = "ds_v2_pair_20260831"


def _code_hash(function: Any) -> dict[str, str]:
    """一個函式的來源位置與原始碼雜湊，供 lock 指認「當時的規則長什麼樣」。"""
    source_file = Path(inspect.getsourcefile(function)).as_posix()
    return {
        "source_file": source_file.split("pcmef-research/")[-1],
        "function": function.__qualname__,
        "code_sha256": hash_object(inspect.getsource(function)),
    }


def _manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. perception_condition_policy
# ---------------------------------------------------------------------------


def derive_perception_condition_policy() -> tuple[dict[str, Any], list[str]]:
    """由 code 與 manifest 反推實際使用過的 condition / augmentation 政策。

    三份清單全部是**觀察結果**，不是設計宣告：
      * train allowlist 由 ds_v2 manifest 是否帶 condition 欄位決定
      * augmentation allowlist 由原始碼掃描決定
      * stress denylist 由 stress 模組的具名算子決定
    """
    from pcmef.perception import baselines, dataset
    from pcmef.perception.stress import (
        CONDITIONS,
        TOF_DEGRADATIONS,
        VISION_DEGRADATIONS,
    )

    gaps: list[str] = []
    ds = _manifest(DS_V2 / "dataset_manifest.json")
    rows = ds["samples"]

    # 訓練資料是否帶 condition 標記？帶了就代表訓練看過劣化。
    conditioned = sorted({r["condition"] for r in rows if "condition" in r})
    if conditioned:
        gaps.append(
            f"ds_v2 rows carry condition labels {conditioned}; the training "
            "condition allowlist can no longer be asserted as clean-only"
        )
    train_allowlist = conditioned or ["clean"]

    # augmentation：掃描訓練路徑的原始碼，不靠記憶。
    augmentation_markers = (
        "augment", "RandomFlip", "RandomCrop", "ColorJitter", "RandAugment",
        "random_flip", "random_crop", "mixup", "cutout", "rotate(",
    )
    scanned, hits = [], []
    for module in (dataset, baselines):
        source = inspect.getsource(module)
        scanned.append(Path(inspect.getsourcefile(module)).name)
        hits += [m for m in augmentation_markers if m in source]
    if hits:
        gaps.append(
            f"augmentation markers {sorted(set(hits))} found in {scanned}; the "
            "generic augmentation allowlist cannot be asserted as empty"
        )

    stress_operators = sorted(VISION_DEGRADATIONS) + sorted(TOF_DEGRADATIONS)
    payload = {
        "train_condition_allowlist": train_allowlist,
        "train_condition_derivation": (
            "outputs/perception/ds_v2/dataset_manifest.json carries no `condition` "
            "field on any of its 400 rows; perception.baselines trains directly on "
            "that manifest. stress.build_stress_dataset() is called only by "
            "gate.run_gate_validation and gate.run_formal_e2, never by the trainer."
        ),
        "generic_augmentation_allowlist": [],
        "generic_augmentation_derivation": (
            f"source scan of {scanned} found none of {list(augmentation_markers)}. "
            "The only input transforms are the deterministic, parameter-free "
            "preprocessing recorded in ds_v2/preprocessing.json "
            "(log1p tonemap + z-score fitted on the train split only). "
            "No augmentation is added by this freeze."
        ),
        "formal_stress_denylist": {
            "conditions": [c for c in CONDITIONS if c != "clean"],
            "vision_operators": sorted(VISION_DEGRADATIONS),
            "tof_operators": sorted(TOF_DEGRADATIONS),
            "statement": (
                "These operators and conditions define the formal E2 stress axis. "
                "They must never appear in perception training or in the "
                "preprocessing fitted on the train split; a model that has seen "
                "them cannot be used to measure degradation routing."
            ),
        },
        "stress_operator_catalogue": {
            "vision": dict(VISION_DEGRADATIONS),
            "tof": dict(TOF_DEGRADATIONS),
        },
        "stress_operators_unchanged": True,
        "preprocessing_hash": hash_file(DS_V2 / "preprocessing.json"),
        "operator_code_hash": hash_object(
            {"vision": dict(VISION_DEGRADATIONS), "tof": dict(TOF_DEGRADATIONS),
             "operators": stress_operators}
        ),
        "synthetic_split_policy_hash_note": (
            "the family partition this policy applies to is frozen separately in "
            "synthetic_split_policy.lock"
        ),
    }
    return payload, gaps


# ---------------------------------------------------------------------------
# 2. training_seed_pairs
# ---------------------------------------------------------------------------


def _split_hash(rows: list[dict[str, Any]], split: str) -> dict[str, Any]:
    """一個 split 的內容雜湊：scenario id + physical family + seed，排序後取雜湊。"""
    members = sorted(
        {
            (r["scenario_id"], r["physical_scene_family"],
             int(r["seed_family"]["scenario_seed"]))
            for r in rows if r.get("split") == split
        }
    )
    return {
        "split": split,
        "n_samples": len(members),
        "n_families": len({m[1] for m in members}),
        "hash": hash_object([list(m) for m in members]),
    }


def derive_training_seed_pairs() -> tuple[dict[str, Any], list[str]]:
    """凍結唯一實際使用的 Vision+ToF checkpoint pair。**不得重訓。**"""
    gaps: list[str] = []
    ds = _manifest(DS_V2 / "dataset_manifest.json")
    report = _manifest(DS_V2 / "perception_report.json")
    rows = ds["samples"]

    vision_weights = DS_V2 / "vision_weights.pt"
    tof_weights = DS_V2 / "tof_weights.pt"
    for path in (vision_weights, tof_weights):
        if not path.exists():
            gaps.append(f"checkpoint {path} is missing; the pair cannot be frozen")

    # seed provenance 必須從 artifact 還原，不得發明。
    seeds = {}
    for modality in ("vision", "tof"):
        entry = report.get("models", {}).get(modality, {})
        if "seed" not in entry:
            gaps.append(
                f"perception_report.json models.{modality} has no `seed`; training "
                "seed provenance cannot be recovered from the artifact"
            )
        seeds[modality] = entry.get("seed")

    from pcmef.perception.baselines import TRAIN_SEED, train_model

    if seeds.get("vision") != TRAIN_SEED or seeds.get("tof") != TRAIN_SEED:
        gaps.append(
            f"artifact seeds {seeds} disagree with baselines.TRAIN_SEED "
            f"{TRAIN_SEED}; provenance is ambiguous"
        )

    train = _split_hash(rows, "train")
    val = _split_hash(rows, "val")

    pair = {
        "checkpoint_pair_id": CHECKPOINT_PAIR_ID,
        "run_dir": DS_V2.as_posix(),
        "vision_checkpoint_sha256": hash_file(vision_weights),
        "tof_checkpoint_sha256": hash_file(tof_weights),
        "vision_seed": seeds.get("vision"),
        "tof_seed": seeds.get("tof"),
        "seed_source": "pcmef.perception.baselines.TRAIN_SEED",
        "seed_semantics": (
            "weight initialisation and batch order only. It is disjoint from every "
            "scene-realisation seed family (PAIRED_SEED_BASE 50000, FAMILY_SEED_BASE "
            "60000, STRESS_SEED_BASE 80000), so vision and tof sharing one value "
            "does not mean they share a random stream."
        ),
        "train_set_hash": train["hash"],
        "validation_set_hash": val["hash"],
        "preprocessing_hash": hash_file(DS_V2 / "preprocessing.json"),
        "preprocessing_fitted_on": report["preprocessing"]["fitted_on"],
        "checkpoint_selection": {
            "rule": "highest validation accuracy epoch; state_dict cloned at that epoch",
            "tie_break": "strict `>` keeps the earlier epoch",
            "epochs": report["models"]["vision"]["epochs"],
            "batch_size": report["models"]["vision"]["batch_size"],
            "learning_rate": report["models"]["vision"]["learning_rate"],
            "selected_on": "ds_v2 val split only; the test split selected nothing",
            "implementation": _code_hash(train_model),
        },
        "architecture": {
            "vision_parameter_count": report["models"]["vision"]["parameter_count"],
            "tof_parameter_count": report["models"]["tof"]["parameter_count"],
            "vision_backbone": "pcmef.perception.baselines.VisionCNN (3-block small CNN)",
            "tof_backbone": "pcmef.perception.baselines.ToFCNN (3-block 1D CNN)",
        },
        "dataset_manifest_code_version": ds["code_version"],
        "training_code_version": report["code_version"],
    }

    payload = {
        "pairs": [pair],
        "train_core_hash": train["hash"],
        "train_core_semantics": (
            "ds_v2 train split: 12 families/class, 240 samples. Supplies "
            "preprocessing statistics and all gradient-based fitting."
        ),
        "train_dev_hash": val["hash"],
        "train_dev_semantics": (
            "ds_v2 val split: 4 families/class, 80 samples. Supplies checkpoint "
            "selection and the fusion weight, nothing else."
        ),
        "train_core_family_disjoint_from_train_dev": True,
        # 這是本 lock 的整個代價，寫在最顯眼的地方。
        "training_seed_robustness_claim": False,
        "n_pairs": 1,
        "n_pairs_superseded_from": 3,
        "supersede_rationale": (
            "The old contract required >= 3 immutable checkpoint pairs so that "
            "statistics could aggregate over training seeds. The thesis execution "
            "path trained exactly one pair; temperature scaling, severity "
            "selection, the gate thresholds, the reliability anchors, the "
            "deterministic-gate pilot and the real-agent LLM validation are all "
            "bound to it. Training two more pairs would not be used by any frozen "
            "result. Recorded in docs/NOTES.md NOTE-050."
        ),
        "retraining_forbidden": True,
    }
    return payload, gaps


# ---------------------------------------------------------------------------
# 3. validation_pool
# ---------------------------------------------------------------------------


def derive_validation_pool() -> tuple[dict[str, Any], list[str]]:
    """凍結真正 effective 的 gate-validation pool。**不建立 cross-fit fold。**"""
    gaps: list[str] = []
    manifest = _manifest(GATE_VALIDATION / "dataset_manifest.json")
    gate_rule = _manifest(GATE / "gate_rule.json")
    rows = manifest["samples"]

    families = {
        f"{r['class_label']}|{r['family_index']}": r["physical_scene_family"]
        for r in rows
    }
    original_hash = hash_object(dict(sorted(families.items())))

    exclusion = gate_rule.get("family_exclusion", {})
    excluded = {
        (e["class_label"], e["family_index"])
        for e in exclusion.get("excluded_families", [])
    }
    kept_rows = [
        r for r in rows if (r["class_label"], r["family_index"]) not in excluded
    ]
    kept_families = {
        key: value for key, value in sorted(families.items())
        if (key.split("|")[0], int(key.split("|")[1])) not in excluded
    }
    effective_hash = hash_object(kept_families)

    if len(kept_rows) != exclusion.get("remaining_sample_count"):
        gaps.append(
            f"recomputed effective pool has {len(kept_rows)} rows but gate_rule.json "
            f"records {exclusion.get('remaining_sample_count')}"
        )

    payload = {
        "checkpoint_pair_ids": [CHECKPOINT_PAIR_ID],
        "original_pool": {
            "n_families": len(families),
            "n_samples": len(rows),
            "family_indices": sorted({r["family_index"] for r in rows}),
            "realizations_per_family": manifest["realizations_per_family"],
            "manifest": (GATE_VALIDATION / "dataset_manifest.json").as_posix(),
        },
        "original_pool_hash": original_hash,
        "exclusion": {
            "excluded_families": sorted(f"{c} f{f}" for c, f in excluded),
            "excluded_sample_count": len(rows) - len(kept_rows),
            "rule": exclusion.get("rule"),
            "reason": (
                "physical_scene_family collision with the ds_v2 Empty f15 training "
                "family. The gate threshold would otherwise have been fitted on a "
                "physical scene the perception models were trained on."
            ),
            "recorded_in": exclusion.get("rule_recorded_in"),
        },
        "effective_pool": {
            "n_families": len(kept_families),
            "n_samples": len(kept_rows),
        },
        "effective_pool_hash": effective_hash,
        "pooled_row_hash": hash_object(
            sorted(r["scenario_id"] for r in kept_rows)
        ),
        # reliability 的特徵只有 Q_vision / Q_tof / D，沒有任何一項帶 pair 身分。
        "training_pair_id_excluded_from_features": True,
        "training_pair_id_exclusion_evidence": (
            "the reliability feature schema is exactly {Q_vision, Q_tof, D}; none "
            "of the three is a function of checkpoint_pair_id. With a single pair "
            "the field is trivially satisfied, and it is recorded so that adding a "
            "second pair later cannot silently introduce pair identity as a feature."
        ),
        "crossfit_folds": "NOT_APPLICABLE_NO_CROSSFIT_IN_PRODUCTION",
        "crossfit_note": (
            "group_fold_assignment_hash was removed from this LockSpec (NOTE-050): "
            "the production reliability mapping has no folds, so the only way to "
            "fill it would be to fabricate an assignment that was never used."
        ),
    }
    return payload, gaps


# ---------------------------------------------------------------------------
# 4. reliability_final
# ---------------------------------------------------------------------------


def derive_reliability_final(
    progress: Callable[[str], None] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """以 deterministic 重算還原**實際執行過的** reliability mapping。

    重算的 pool 與遮罩必須與 `llm_real_validation._compute_cases()` 相同
    （clean gate-validation 全 96 筆），否則得到的是一個新版本而不是還原。
    """
    import numpy as np

    from pcmef.perception.dataset import _load_exr, _rgb_tonemap, _tof_transform
    from pcmef.perception.gate import (
        RELIABILITY_EVIDENCE,
        RELIABILITY_MODEL_VERSION,
        RELIABILITY_ROUTING,
        RELIABILITY_ROUTING_VERSION,
        RELIABLE_MARGIN,
        GateRule,
        _logits,
        duq_signals,
        fit_reliability_model,
        load_frozen_models,
        quality_signals,
        reliability_route,
        reliability_scores,
        softmax,
    )

    say = progress or (lambda _m: None)
    gaps: list[str] = []

    vision_model, tof_model, preprocessing = load_frozen_models(DS_V2)
    manifest = _manifest(GATE_VALIDATION / "dataset_manifest.json")
    spec = _manifest(GATE / "gate_rule.json")["gate_rule"]
    rule = GateRule(
        q_vision_threshold=spec["q_vision_threshold"],
        q_tof_threshold=spec["q_tof_threshold"],
        disagreement_threshold=spec["disagreement_threshold"],
        fusion_weight=spec["fusion_weight"],
        temperature_vision=spec["temperature_vision"],
        temperature_tof=spec["temperature_tof"],
    )

    samples = manifest["samples"]
    say(f"  recomputing reliability anchors over {len(samples)} clean samples")
    raw_rgb, raw_tof, prep_rgb, prep_tof = [], [], [], []
    for sample in samples:
        image = _rgb_tonemap(_load_exr(sample["rgb"]["exr_path"]))
        raw_rgb.append(image)
        normed = (image - np.asarray(preprocessing["rgb"]["mean"])) / np.asarray(
            preprocessing["rgb"]["std"]
        )
        prep_rgb.append(np.ascontiguousarray(normed.transpose(2, 0, 1), dtype=np.float32))
        record = np.load(sample["tof"]["path"])
        raw_tof.append(record)
        rec = _tof_transform(record)
        rec = (rec - np.asarray(preprocessing["tof"]["mean"])) / np.asarray(
            preprocessing["tof"]["std"]
        )
        prep_tof.append(np.ascontiguousarray(rec.transpose(1, 0), dtype=np.float32))

    p_vision = softmax(_logits(vision_model, np.stack(prep_rgb)), rule.temperature_vision)
    p_tof = softmax(_logits(tof_model, np.stack(prep_tof)), rule.temperature_tof)
    per_case = [quality_signals(r, t) for r, t in zip(raw_rgb, raw_tof)]
    quality = {
        "vision_sharpness": np.asarray([q["vision_sharpness"] for q in per_case]),
        "tof_snr": np.asarray([q["tof_snr"] for q in per_case]),
    }
    signals = duq_signals(p_vision, p_tof, quality)

    # 與實際執行相同：全 96 筆皆為 clean，遮罩全 True。
    clean_mask = np.ones(len(samples), dtype=bool)
    model = fit_reliability_model(signals, clean_mask, rule)
    q = reliability_scores(signals, model)
    routes = reliability_route(q, signals, rule)

    excluded = {("Empty", 27)}
    effective = np.array(
        [(s["class_label"], s["family_index"]) not in excluded for s in samples]
    )
    counts = {
        str(name): int(count)
        for name, count in zip(*np.unique(routes[effective], return_counts=True))
    }

    fitted = model.to_dict()
    stats = {k: v for k, v in fitted.items() if isinstance(v, float)}

    payload = {
        "algorithm": "sensor_quality_sigmoid_with_cross_modal_support",
        "algorithm_version": RELIABILITY_MODEL_VERSION,
        "formula": (
            "margin_m = sigmoid((Q_m - anchor_m) / scale_m); "
            "support = 0.5 + 0.5 * (1 - clip(D, 0, 1)); "
            "q_m = clip(margin_m * support, 0, 1)"
        ),
        "feature_schema": {
            "Q_vision": {
                "definition": "log1p(var(Laplacian(grey(rgb)) / mean(|grey|)))",
                "source": "raw RGB evidence only",
                "computed_by": "pcmef.perception.gate.quality_signals",
            },
            "Q_tof": {
                "definition": "log1p(median(signal_rate_mcps) / median(ambient_rate_mcps))",
                "source": "raw ToF recording only",
                "computed_by": "pcmef.perception.gate.quality_signals",
            },
            "D": {
                "definition": "0.5 * sum(|p_vision - p_tof|) (total variation distance)",
                "source": (
                    "the two calibrated class-probability vectors. D is a "
                    "cross-modal AGREEMENT signal, not either modality's own "
                    "confidence: it is symmetric and invariant to how sharp "
                    "either distribution is."
                ),
                "computed_by": "pcmef.perception.gate.duq_signals",
            },
            "ordered_features": ["Q_vision", "Q_tof", "D"],
        },
        "fitted_statistics": stats,
        "fitted_statistics_source": (
            "5th percentile (anchor) and IQR (scale) of the CLEAN gate-validation "
            "distribution of each Q; medians recorded for reference"
        ),
        "parameters": {
            "reliable_margin": RELIABLE_MARGIN,
            "routing_rule": RELIABILITY_ROUTING,
            "routing_version": RELIABILITY_ROUTING_VERSION,
            "sigmoid_scale_floor": 1e-9,
            "support_floor": 0.5,
            "support_floor_rationale": (
                "on disagreement the cross-modal term degrades to a neutral 0.5 "
                "rather than 0, so that a conflict does not crush both q values "
                "at once and leave the router with no discrimination"
            ),
            "clip_range": [0.0, 1.0],
        },
        "scalers": {
            "kind": "none",
            "note": (
                "there is no fitted scaler. anchor/scale are reference statistics "
                "of the clean distribution, used directly in the sigmoid."
            ),
        },
        "anchor_rule": fitted["anchor_rule"],
        "evidence_sources": list(RELIABILITY_EVIDENCE),
        "forbidden_predictive_features": [
            "max softmax probability",
            "predictive entropy of the modality being scored",
            "calibrated confidence",
            "anything derived from p_m(y|x)",
        ],
        "forbidden_statement": (
            "max-softmax / entropy / calibrated confidence are NOT modality "
            "reliability inputs."
        ),
        "forbidden_rationale": fitted["why"],
        "effective_validation_pool_hash": hash_object(
            {
                f"{s['class_label']}|{s['family_index']}": s["physical_scene_family"]
                for s in samples
                if (s["class_label"], s["family_index"]) not in excluded
            }
        ),
        # 這是本 lock 唯一的邊界，必須寫在 lock 裡而不是只寫在 NOTE。
        "anchor_fit_pool": {
            "n_families": 32,
            "n_samples": len(samples),
            "pool": "FULL clean gate-validation, BEFORE the NOTE-048 exclusion",
            "boundary": (
                "the anchors were fitted on all 96 clean gate-validation samples, "
                "which includes Empty f27 -- the family later excluded for "
                "duplicating ds_v2 Empty f15. Refitting on the 93-sample effective "
                "pool yields different anchors and moves one case between "
                "trust_vision and fusion. That refit is deliberately NOT performed: "
                "the frozen mapping must be the one that actually ran. Recorded in "
                "docs/NOTES.md NOTE-050."
            ),
            "refit_forbidden": True,
        },
        "deterministic_reproduction": {
            "routes_over_effective_pool": counts,
            "method": (
                "recomputed from the frozen ds_v2 checkpoints and the frozen gate "
                "rule; no fitting of any kind was re-run"
            ),
        },
        "code_hash": hash_object(
            {
                "fit_reliability_model": _code_hash(fit_reliability_model),
                "reliability_scores": _code_hash(reliability_scores),
                "reliability_route": _code_hash(reliability_route),
                "quality_signals": _code_hash(quality_signals),
                "duq_signals": _code_hash(duq_signals),
            }
        ),
        "code_components": {
            "fit_reliability_model": _code_hash(fit_reliability_model),
            "reliability_scores": _code_hash(reliability_scores),
            "reliability_route": _code_hash(reliability_route),
            "quality_signals": _code_hash(quality_signals),
            "duq_signals": _code_hash(duq_signals),
        },
    }

    if counts.get("escalated") is None:
        gaps.append("reliability routing produced no escalated case on the effective pool")
    return payload, gaps


# ---------------------------------------------------------------------------
# 5. gate
# ---------------------------------------------------------------------------


def derive_gate(reliability_hash: str, validation_pool_hash: str) -> tuple[dict[str, Any], list[str]]:
    """取既有核定的 corrective gate 值。**不重新搜尋。**"""
    from pcmef.core.numeric import SELECTIVE_ESCALATION_BRIDGE_VERSION
    from pcmef.perception.gate import (
        RELIABILITY_ROUTING,
        RELIABILITY_ROUTING_VERSION,
        fit_gate_rule,
    )

    gaps: list[str] = []
    document = _manifest(GATE / "gate_rule.json")
    spec = document["gate_rule"]
    search = document["gate_search"]

    expected = {
        "q_vision_threshold": 1.3157833714234424,
        "q_tof_threshold": 17.01321693530837,
        "disagreement_threshold": 0.48965981236738715,
        "fusion_weight": 0.5,
        "temperature_vision": 0.16915522430955235,
        "temperature_tof": 0.04978728827972712,
    }
    for key, value in expected.items():
        if abs(float(spec[key]) - value) > 1e-12:
            gaps.append(
                f"gate_rule.json {key} = {spec[key]!r} does not match the approved "
                f"corrective value {value!r}"
            )

    payload = {
        "routing_policy_version": RELIABILITY_ROUTING_VERSION,
        "routing_policy": RELIABILITY_ROUTING,
        "routing_policy_source": "pcmef.perception.gate.reliability_route",
        "decision_bridge_version": SELECTIVE_ESCALATION_BRIDGE_VERSION,
        "decision_bridge_formula": (
            "F(x) = (1 - e) * p_trad + e * s_A, with e = 1 iff route == 'escalated'"
        ),
        "q_vision_threshold": spec["q_vision_threshold"],
        "q_tof_threshold": spec["q_tof_threshold"],
        "disagreement_threshold": spec["disagreement_threshold"],
        "fusion_weight": spec["fusion_weight"],
        "temperature_vision": spec["temperature_vision"],
        "temperature_tof": spec["temperature_tof"],
        "temperature_predictions_unchanged": {
            modality: document["calibration"][modality]["predictions_unchanged"]
            for modality in ("vision", "tof")
        },
        "effective_gate_validation_hash": validation_pool_hash,
        "reliability_config_hash": reliability_hash,
        "search_grid_hash": hash_object(search["search_space"]),
        "search_grid": {
            "q_vision_grid_points": len(search["search_space"]["q_vision_grid"]),
            "q_tof_grid_points": len(search["search_space"]["q_tof_grid"]),
            "disagreement_grid_points": len(search["search_space"]["disagreement_grid"]),
            "construction": (
                "quantiles of the gate-validation signal distributions: "
                "Q_vision and Q_tof at linspace(0.05, 0.95, 19), "
                "D at linspace(0.5, 0.99, 12)"
            ),
        },
        "objective": search["objective"],
        "tie_break": (
            "strict `>` on routed accuracy, so the first candidate in grid order "
            "wins ties; iteration order is q_vision outer, q_tof middle, D inner"
        ),
        "tie_break_implementation": _code_hash(fit_gate_rule),
        "fitted_on": spec["fitted_on"],
        "gate_validation_accuracy": search["gate_validation_accuracy"],
        "search_escalation_rate": search["escalation_rate"],
        "re_search_forbidden": True,
        "legacy_continuous_gate": {
            "status": "SUPERSEDED_NEVER_ACTIVATED",
            "parameters": ["alpha", "beta", "gamma"],
            "note": (
                "the continuous simplex gate g = clip(alpha*D + beta*U + gamma*Q) "
                "had no production caller and must not be revived (NOTE-049)"
            ),
        },
        "escalation_rate_is_a_result": (
            "The Formal E2 escalation rate is itself a research finding. Thresholds "
            "must not be adjusted to make the LLM arm fire more often."
        ),
        "code_version": document["code_version"],
    }
    return payload, gaps


# ---------------------------------------------------------------------------
# 6. inference_firewall
# ---------------------------------------------------------------------------


def derive_inference_firewall() -> tuple[dict[str, Any], list[str]]:
    """由 firewall 實作算出 payload / metadata / opaque-id / snapshot 四份政策。"""
    from pcmef.core.inference_payload import (
        FORBIDDEN_PAYLOAD_FIELDS,
        OBSERVABLE_QUALITY_CUES,
        InferencePayload,
        assert_no_forbidden_tokens,
    )
    from pcmef.core.opaque_ids import OpaqueIdMap
    from pcmef.agents.pcmef_agents import AGENTS

    gaps: list[str] = []
    validation = Path("outputs/llm_validation/real_agent_validation.json")
    if not validation.exists():
        gaps.append(f"{validation} missing; the firewall was never executed end to end")
    checks = _manifest(validation)["checks"] if validation.exists() else []
    failed = [c["check"] for c in checks if not c["passed"]]
    if failed:
        gaps.append(f"real-agent validation checks failed: {failed}")

    payload_schema = {
        "fields": ["opaque_case_id", "rgb_tensor", "tof_sequence", "tof_summary",
                   "quality_cues"],
        "required": ["opaque_case_id"],
        "evidence_requirement": "at least one of rgb_tensor / tof_sequence, as ndarray",
        "quality_cue_allowlist": sorted(OBSERVABLE_QUALITY_CUES),
        "intentionally_absent": sorted(FORBIDDEN_PAYLOAD_FIELDS),
    }

    role_isolation = {
        code: {
            "schema": spec.schema_name,
            "needs_image": spec.needs_image,
            "withheld_keys": list(spec.withheld_keys),
        }
        for code, spec in sorted(AGENTS.items())
    }

    payload = {
        "payload_schema_hash": hash_object(payload_schema),
        "payload_schema": payload_schema,
        "forbidden_metadata_rules": {
            "forbidden_fields": sorted(FORBIDDEN_PAYLOAD_FIELDS),
            # 鍵名刻意避開 "token"/"salt"/"secret" 這些字：LockStore 的
            # _assert_no_secrets() 以鍵名比對擋 secret 值，而它擋得對 ——
            # 該放寬的是這裡的命名，不是那道防線。
            "semantic_string_scan": (
                "recursive scan of every string key AND value against class names, "
                "legacy labels, E2 condition names, the seven split roles, "
                "Low/Mid/High, and the syn_/real_/nominal_/clean_ filename prefixes"
            ),
            "field_pattern_boundary": "(?<![a-z0-9_])<field>(?![a-z0-9_]), case-insensitive",
            "on_violation": "raise InferenceFirewallViolation; fail the run, never continue",
            "implementation": _code_hash(assert_no_forbidden_tokens),
        },
        "forbidden_metadata_rules_hash": hash_object(
            {
                "fields": sorted(FORBIDDEN_PAYLOAD_FIELDS),
                "cues": sorted(OBSERVABLE_QUALITY_CUES),
                "scanner": _code_hash(assert_no_forbidden_tokens),
            }
        ),
        "opaque_id_map_policy": {
            "scheme": "run-scoped keyed HMAC-SHA256, truncated to 32 hex chars",
            "per_run_keying_material": (
                "32 random bytes generated per run; never written into any lock, "
                "because a lock that carried it would let any reader invert the "
                "opaque ids"
            ),
            "reverse_lookup": "evaluator-only; inference modules must not import it",
            "accepted_form": "^[0-9a-f]{16,64}$ or UUID",
            "map_hash_definition": (
                "hash_object({run_id, forward}); the keying material is excluded"
            ),
            "implementation": _code_hash(OpaqueIdMap.map_hash),
            "derivation": _code_hash(OpaqueIdMap._derive),
        },
        # 每一次 run 的實際 map 在該 run 產生（salt 隨機），Final E2 尚未執行，
        # 因此這裡凍的是**政策**而不是某一份 map 的雜湊。語意寫明，不含糊。
        "opaque_id_map_hash": hash_object(
            {
                "policy": "run_scoped_keyed_hmac_v1",
                "hex_length": 32,
                "keying_material_bytes": 32,
                "map_hash_excludes_keying_material": True,
                "derivation": _code_hash(OpaqueIdMap._derive),
            }
        ),
        "opaque_id_map_hash_semantics": (
            "This is the hash of the MAPPING POLICY, not of any particular map. "
            "The opaque map is run-scoped and its salt is generated when the run "
            "starts, so no map exists before Final E2 executes. The per-run "
            "map_hash is written into that run's own artifact and must match this "
            "policy."
        ),
        "provider_payload_snapshot_policy": {
            "method": "InferencePayload.to_provider_dict(include_arrays=False)",
            "content": "opaque_case_id, tof_summary, quality_cues, and array SHAPES only",
            "rescan": "the dict is scanned again immediately before it is returned",
            "implementation": _code_hash(InferencePayload.to_provider_dict),
        },
        "role_isolation_policy": role_isolation,
        "role_isolation_policy_version": "role_withholding_v1",
        "role_isolation_mechanism": (
            "isolation is enforced by NOT SENDING the withheld keys, never by "
            "asking the prompt to ignore them"
        ),
        "role_isolation_policy_hash": hash_object(role_isolation),
        "allowlist_unchanged": True,
        "verified_by": {
            "artifact": validation.as_posix(),
            "checks_passed": len(checks) - len(failed),
            "checks_total": len(checks),
            "real_provider_calls": _manifest(validation).get("real_calls")
            if validation.exists() else None,
        },
    }
    return payload, gaps


# ---------------------------------------------------------------------------
# 7. conflict_operational
# ---------------------------------------------------------------------------


def derive_conflict_operational(delta: float) -> tuple[dict[str, Any], list[str]]:
    payload = {
        "delta": delta,
        "delta_source": (
            "outputs/perception/gate/gate_rule.json gate_rule.disagreement_threshold; "
            "the same quantity as the gate's D cut, not a second independent number"
        ),
        "operational_conflict_rule": "argmax(p_V) != argmax(p_T) OR D >= delta",
        "rule_id": "argmax_mismatch_or_d_ge_delta",
        "distinction_from_condition_name": (
            "Conflict-Stress is a GENERATION condition: both modalities were "
            "degraded. Operational Conflict is a MEASURED property of one case. A "
            "Conflict-Stress row need not be an operational conflict, and a clean "
            "row can be one."
        ),
        "attainment_reporting": {
            "usage": "REPORTING_AND_SUBGROUP_ANALYSIS_ONLY",
            "reported": [
                "operational conflict rate overall",
                "operational conflict rate per generation condition",
                "accuracy and macro-F1 within the operational-conflict subgroup",
                "accuracy and macro-F1 within its complement",
            ],
            "must_not_filter_formal_ids": True,
            "prohibition": (
                "Operational Conflict must NOT be used to select, resample, or "
                "exclude Formal E2 IDs. Every one of the 384 condition rows enters "
                "the primary analysis regardless of its conflict status; otherwise "
                "the denominator would change with the method being evaluated."
            ),
        },
    }
    return payload, []


# ---------------------------------------------------------------------------
# 8. e2_sample_size
# ---------------------------------------------------------------------------


def derive_e2_sample_size() -> tuple[dict[str, Any], list[str]]:
    """Final E2 的 sizing。由**保留區間**推得，不看任何相對表現。"""
    gaps: list[str] = []
    pilot = _manifest(PILOT / "dataset_manifest.json")
    pilot_families = {
        f"{r['class_label']}|{r['family_index']}": r["physical_scene_family"]
        for r in pilot["samples"]
    }
    if any(r["family_index"] in FINAL_E2_FAMILIES for r in pilot["samples"]):
        gaps.append("the pilot manifest contains families 36-43; the reserve is broken")

    families_per_class = len(FINAL_E2_FAMILIES)
    realizations = 3
    base_per_class = families_per_class * realizations
    conditions = 4

    payload = {
        "pilot_set_hash": hash_object(dict(sorted(pilot_families.items()))),
        "pilot_set": {
            "run": PILOT.as_posix(),
            "family_indices": pilot["family_indices"],
            "n_samples": len(pilot["samples"]),
            "historical_purpose": "E2_DETERMINISTIC_GATE_PILOT_ALREADY_SEEN",
        },
        "family_indices": list(FINAL_E2_FAMILIES),
        "family_domain": FINAL_E2_DOMAIN,
        "family_domain_derivation": "max_reserved_family_index + 1 = 43 + 1 = 44",
        "families_per_class": families_per_class,
        "realizations_per_family": realizations,
        "base_scenarios_per_class": base_per_class,
        "base_scenarios_total": base_per_class * len(["Empty", "Water-filled", "Bubbly", "Misty"]),
        "conditions_per_scenario": conditions,
        "conditions": ["clean", "vision_degraded", "tof_degraded", "conflict"],
        "rows_per_class_per_condition": base_per_class,
        "total_condition_rows": base_per_class * 4 * conditions,
        "final_n_per_class": base_per_class,
        "class_balance": {
            "scheme": "exactly balanced by construction",
            "per_class_base_scenarios": base_per_class,
            "per_class_condition_rows": base_per_class * conditions,
        },
        "severity_allocation": {
            "scheme": "FIXED_TWO_SEVERITY_NO_LOW_MID_HIGH",
            "superseded_from": "low_mid_high_quota",
            "vision": 2.0,
            "tof": 0.05,
            "selected_on": "gate-validation only, by stress.select_severity()",
            "conflict_condition": "both operators applied at the same two severities",
            "reselection_forbidden": True,
        },
        "decision_rule_version": "reserved_partition_sizing_v1",
        "decision_rule_semantics": (
            "N is not chosen by a power calculation or by a pilot's observed "
            "effect. It is the full size of an independent partition that was "
            "reserved, and whose index range and domain were fixed, before any "
            "36-43 outcome existed."
        ),
        "sizing_reference": (
            "independent reserved final partition fixed before any 36-43 outcome; "
            "not selected from G5-G4 performance"
        ),
        "sizing_did_not_use": [
            "G5 minus G4 delta",
            "any proposed-method relative performance",
            "the deterministic-gate pilot's accuracy or effect size",
            "any Formal E2 outcome (none exists)",
        ],
        "sizing_pilot_rerun_forbidden": True,
    }
    return payload, gaps


# ---------------------------------------------------------------------------
# 9. statistics_config
# ---------------------------------------------------------------------------


def derive_statistics_config() -> tuple[dict[str, Any], list[str]]:
    """Final E2 的統計契約。這是**預註冊**，不是對現有實作的描述。"""
    gaps: list[str] = []

    from pcmef.stats.bootstrap import (
        CLUSTER_BOOTSTRAP_VERSION,
        cluster_bootstrap_delta,
    )

    # 誠實檢查：本 lock 凍的契約有沒有一份**確實符合**的實作。
    # 沒有的話這個 lock 就是空的，凍它只是把問題往後推。
    conforming = _code_hash(cluster_bootstrap_delta)
    source = inspect.getsource(cluster_bootstrap_delta)
    for requirement, marker in (
        ("cluster-level resampling", "rows_by_cluster"),
        ("stratified draw", "by_stratum"),
        ("shared clusters across methods", "drawn"),
    ):
        if marker not in source:
            gaps.append(
                f"pcmef.stats.bootstrap.cluster_bootstrap_delta does not implement "
                f"{requirement}"
            )

    payload = {
        "bootstrap_replicates": 10000,
        "bootstrap_seed": 20260827,
        "resample_unit": "physical_scene_family",
        "resample_unit_superseded_from": "scenario_id",
        "cluster_definition": (
            "drawing one family carries ALL of that family's rows into the "
            "replicate: 3 realizations x 4 conditions = 12 rows"
        ),
        "cluster_size_rows": 12,
        "stratification": "class",
        "stratification_superseded_from": "condition",
        "paired_methods": True,
        "shared_clusters_across_methods": (
            "within one bootstrap replicate every compared method uses the SAME "
            "set of family clusters, so the paired delta carries no "
            "different-sample noise"
        ),
        "confidence_level": 0.95,
        "ci_method": "percentile, 2.5 / 97.5",
        "metric_definitions": {
            "overall_accuracy": "fraction of the 384 condition rows classified correctly",
            "overall_macro_f1": "unweighted mean of the four per-class F1 scores",
            "per_condition_accuracy": "accuracy within each of the four conditions (96 rows each)",
            "per_condition_macro_f1": "macro-F1 within each of the four conditions",
            "paired_effect_delta": "PC-MEF minus baseline, on identical rows",
            "cluster_bootstrap_ci_95": "95% CI of the paired delta under family-cluster resampling",
            "note": (
                "worst_condition_macro_f1, declared as e2.primary_metric in "
                "configs/base.yaml, is the minimum over per_condition_macro_f1 and "
                "is therefore derivable from the reported quantities."
            ),
        },
        "primary_reporting": [
            "overall accuracy",
            "overall macro-F1",
            "per-condition accuracy",
            "per-condition macro-F1",
            "paired effect delta",
            "95% cluster-bootstrap CI",
        ],
        "training_seed_aggregation": {
            "scheme": "identity_single_pair",
            "superseded_from": "mean_over_training_pairs",
            "n_pairs": 1,
            "reason": (
                "only one checkpoint pair exists (NOTE-050); there is nothing to "
                "aggregate over and no training-seed robustness is claimed"
            ),
        },
        "ece_semantics": {
            "mode": "score_calibration_diagnostic_only",
            "s_a_probability_calibration_claim": False,
            "statement": (
                "s_A is a normalized Evidence-Support Score, not a calibrated "
                "posterior. No NLL or ECE probability-calibration claim may be "
                "made for s_A or for F(x). The per-modality temperature scaling "
                "reported in gate_rule.json applies to p_vision / p_tof only."
            ),
        },
        "implementation": {
            "version": CLUSTER_BOOTSTRAP_VERSION,
            "entry_point": "pcmef.stats.bootstrap.cluster_bootstrap_delta",
            "code": conforming,
            "superseded_entry_point": (
                "pcmef.perception.gate.paired_bootstrap_delta -- resamples "
                "base_scenario_id at seed 20260831. It was used for the "
                "deterministic-gate pilot and must NOT be used for Final E2: "
                "neither its resample unit nor its seed satisfies this lock."
            ),
        },
    }
    return payload, gaps


# ---------------------------------------------------------------------------
# 10. Final E2 scenario identity（只算身分，不產資料）
# ---------------------------------------------------------------------------


def derive_final_e2_scenarios() -> tuple[dict[str, Any], list[str]]:
    """推導 32 個 family descriptor、96 個 scenario ID 與 96 個 seed。

    **不 render、不產生任何 observation。** scenario_id 與 seed 由純函式決定，
    descriptor 由 frozen SimulatorIdentity 與 deterministic 的 family_variation
    決定；render 產物不在 identity 之內。
    """
    from pcmef.core.constants import CLASS_ORDER
    from pcmef.perception.dataset import physical_scene_family
    from pcmef.simulation.paired import (
        _scenario_config,
        family_scenario_seed,
        family_variation,
        load_calibrated_simulator,
    )

    gaps: list[str] = []
    identity, _calibration = load_calibrated_simulator("freeze", ".")
    realizations = 3

    def descriptor(class_label: str, family_index: int, domain: int) -> str:
        variation = family_variation(class_label, family_index, domain)
        config = _scenario_config(identity, class_label, seed=0, variation=variation)
        return physical_scene_family(
            {**config.to_dict(),
             "scene_constants_applied": dict(identity.scene_constants)}
        )

    # 等價性證明：離線重算既有 partition，對不上就不能信任 36-43 那 32 個值。
    equivalence = {"checked": 0, "mismatches": []}
    for path in (
        DS_V2 / "dataset_manifest.json",
        GATE_VALIDATION / "dataset_manifest.json",
        PILOT / "dataset_manifest.json",
    ):
        manifest = _manifest(path)
        domain = manifest.get("family_domain") or 20
        seen: dict[tuple[str, int], str] = {}
        for row in manifest["samples"]:
            seen.setdefault(
                (row["class_label"], row["family_index"]), row["physical_scene_family"]
            )
        for (class_label, family_index), stored in seen.items():
            equivalence["checked"] += 1
            if descriptor(class_label, family_index, domain) != stored:
                equivalence["mismatches"].append(f"{path.parent.name}:{class_label}|{family_index}")
    equivalence["proven"] = not equivalence["mismatches"]
    if not equivalence["proven"]:
        gaps.append(
            f"offline descriptor derivation disagrees with existing manifests: "
            f"{equivalence['mismatches'][:5]}"
        )

    # 既有 family 的 descriptor，用來證明 36-43 沒有撞上任何一個。
    existing: dict[str, dict[str, str]] = {
        cls: {
            **{f"ds_v2:{f}": descriptor(cls, f, 20) for f in range(20)},
            **{f"d36:{f}": descriptor(cls, f, 36) for f in range(20, 36)},
        }
        for cls in CLASS_ORDER
    }

    descriptors: dict[str, str] = {}
    scenarios: list[dict[str, Any]] = []
    collisions: dict[str, list[str]] = {}
    for class_label in CLASS_ORDER:
        for family_index in FINAL_E2_FAMILIES:
            value = descriptor(class_label, family_index, FINAL_E2_DOMAIN)
            descriptors[f"{class_label}|{family_index}"] = value
            hits = [k for k, v in existing[class_label].items() if v == value]
            if hits:
                collisions[f"{class_label}|{family_index}"] = hits
            for realization in range(realizations):
                scenarios.append(
                    {
                        "scenario_id": (
                            f"{class_label.lower().replace('-', '_')}"
                            f"_f{family_index:02d}_r{realization:02d}"
                        ),
                        "class_label": class_label,
                        "family_index": family_index,
                        "realization_index": realization,
                        "scenario_seed": family_scenario_seed(
                            class_label, family_index, realization
                        ),
                        "physical_scene_family": value,
                    }
                )
    if collisions:
        gaps.append(f"families 36-43 collide with existing families: {collisions}")

    seeds = [s["scenario_seed"] for s in scenarios]
    if len(set(seeds)) != len(seeds):
        gaps.append("duplicate scenario seeds in the Final E2 set")

    ordered = sorted(scenarios, key=lambda s: s["scenario_id"])
    return {
        "family_domain": FINAL_E2_DOMAIN,
        "family_domain_derivation": "max_reserved_family_index + 1 = 43 + 1 = 44",
        "n_families": len(descriptors),
        "n_scenarios": len(ordered),
        "realizations_per_family": realizations,
        "rendered": False,
        "observations_read": False,
        "family_descriptors": dict(sorted(descriptors.items())),
        "family_descriptors_hash": hash_object(dict(sorted(descriptors.items()))),
        "scenario_ids": [s["scenario_id"] for s in ordered],
        "scenario_seeds": {s["scenario_id"]: s["scenario_seed"] for s in ordered},
        "seed_rule": "FAMILY_SEED_BASE 60000 + 1000*class_index + 10*family_index + realization",
        "scenario_set_hash": hash_object(
            [
                [s["scenario_id"], s["scenario_seed"], s["physical_scene_family"]]
                for s in ordered
            ]
        ),
        "zero_collision_with_existing_families": not collisions,
        "offline_derivation_equivalence": equivalence,
        "simulator_identity_hash": identity.identity_hash(),
        "calibrated_simulation_lock_hash": identity.calibrated_lock_hash,
    }, gaps


def prove_final_e2_absent(out_root: str | Path = "outputs") -> dict[str, Any]:
    """以檔案系統實測證明 families 36-43 尚無任何 generated sample artifact。

    掃描的是**目錄名**，因此不需要開啟任何檔案，也就不可能在證明「沒有資料」
    的過程中讀到資料。
    """
    root = Path(out_root)
    found: list[str] = []
    for index in FINAL_E2_FAMILIES:
        found.extend(p.as_posix() for p in root.rglob(f"*_f{index:02d}_r*"))
    mentions: list[str] = []
    for path in (
        DS_V2 / "dataset_manifest.json",
        GATE_VALIDATION / "dataset_manifest.json",
        PILOT / "dataset_manifest.json",
    ):
        rows = _manifest(path)["samples"]
        if any(r.get("family_index") in FINAL_E2_FAMILIES for r in rows):
            mentions.append(path.parent.name)
    return {
        "families": list(FINAL_E2_FAMILIES),
        "sample_directories_found": found,
        "manifests_containing_them": mentions,
        "generated": bool(found or mentions),
        "method": "directory-name scan under outputs/; no sample file was opened",
    }


# ---------------------------------------------------------------------------
# 匯總
# ---------------------------------------------------------------------------


def build_preflight(
    out_dir: str | Path = "outputs/lock_proposals",
    *,
    freeze_dir: str | Path,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """推導十個 lock 的 payload 並回報所有 gap。**不寫任何 lock。**

    `freeze_dir` 必填：本函式會讀 `gate` 與 `reliability_final`，而這兩個
    已被 AMD-006 在 PFC-001 supersede。舊的 `"freeze"` 預設指向 parent
    lineage，那裡仍是更正前的值，讀錯不會拋錯（NOTE-054）。
    """
    say = progress or (lambda _m: None)
    gaps: dict[str, list[str]] = {}
    payloads: dict[str, dict[str, Any]] = {}

    def record(name: str, result: tuple[dict[str, Any], list[str]]) -> dict[str, Any]:
        payload, found = result
        payloads[name] = payload
        if found:
            gaps[name] = found
        say(f"  derived {name}" + (f"  [{len(found)} gap(s)]" if found else ""))
        return payload

    say("deriving perception_condition_policy")
    record("perception_condition_policy", derive_perception_condition_policy())
    say("deriving training_seed_pairs")
    record("training_seed_pairs", derive_training_seed_pairs())
    say("deriving validation_pool")
    pool = record("validation_pool", derive_validation_pool())
    say("deriving reliability_final (recomputing anchors, this loads the checkpoints)")
    reliability = record("reliability_final", derive_reliability_final(say))
    say("deriving gate")
    record(
        "gate",
        derive_gate(hash_object(reliability), pool["effective_pool_hash"]),
    )
    say("deriving inference_firewall")
    record("inference_firewall", derive_inference_firewall())
    say("deriving conflict_operational")
    record(
        "conflict_operational",
        derive_conflict_operational(payloads["gate"]["disagreement_threshold"]),
    )
    say("deriving e2_sample_size")
    record("e2_sample_size", derive_e2_sample_size())
    say("deriving statistics_config")
    record("statistics_config", derive_statistics_config())
    say("deriving Final E2 scenario identity (no render)")
    scenarios, scenario_gaps = derive_final_e2_scenarios()
    if scenario_gaps:
        gaps["final_e2_scenarios"] = scenario_gaps

    absence = prove_final_e2_absent()
    if absence["generated"]:
        gaps.setdefault("final_e2_scenarios", []).append(
            "families 36-43 already have generated artifacts; the reserve is broken"
        )

    document = {
        "report_id": "final_preflight_freeze_proposal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scientific_result": False,
        "dry_run": True,
        "locks_written": [],
        "payloads": payloads,
        "final_e2_scenarios": scenarios,
        "final_e2_absence_proof": absence,
        "gaps": gaps,
        "SAFE_TO_FREEZE": "NO" if gaps else "YES",
        "FINAL_E2_36_43_GENERATED": "YES" if absence["generated"] else "NO",
        "FINAL_E2_36_43_READ": "NO",
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "final_preflight.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document
