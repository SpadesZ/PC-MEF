# PC-MEF Research System source maintenance contract
# 上下游: 讀 outputs/perception/{ds_v2,gate_validation,gate} 的 frozen 權重、
#         frozen gate rule 與既有 effective-93 stress manifest；
#         產出 outputs/corrective/pre_final_corrective.json。
#         **不重訓、不改溫度、不改 severity、不 render，
#         且完全不觸碰 families 36-43。**
# 檔案路徑: pcmef/experiments/corrective_pass.py
# 產生時間: 2026-09-01 14:05 +08:00
# 版本: v0.1.0
# 功能說明: 把 NOTE-048 的 effective-93 exclusion 補套到 reliability anchors 上，
#           並以相同既定程序在 effective 93 上重跑一次 gate search 作為驗證。
#           產出 before/after 對照供 corrective lineage 引用。
# 模組定位: implementation-defect 的更正層。它「不是」tuning ——
#           演算法、feature、anchor rule、溫度、severity、權重、prompt、模型
#           一律不動，唯一的差別是擬合 pool 由 96 改為已核定的 effective 93。
# 主要責任:
#   1. load_effective_pool() 套用 NOTE-048 exclusion，回傳 93 筆 clean 樣本
#   2. refit_reliability() 以相同 anchor rule 在 93 上重擬合
#   3. rerun_gate_search() 以相同程序在 effective-93 stress set 上重搜門檻
#   4. build_corrective_report() 產出 before/after 與 delta 是否變動的判定
# 維護提醒:
#   - 不得在本檔改動 q-proxy 定義或新增 feature。更正的是**擬合 pool**，
#     不是模型。改了任何一項，這就不再是 defect correction 而是新方法。
#   - 不得因為更正後的數字比較難看就回頭調整。變好變差都必須如實記錄；
#     這正是「更正」與「調參」的唯一分界。
#   - 不得重建 stress set。既有的 effective-93 stress manifest 就是當初
#     gate 擬合所用的那一份，重建會引入新的 stress seed 而讓比較失去意義。
#   - v0.1.0 新增：首版 pre-final corrective pass，決策見 NOTE-051。
# 驗證方式:
#   - py -3.10 -m pcmef.cli corrective reliability-gate
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pcmef.core.hash import hash_object

__all__ = ["build_corrective_report", "EXCLUDED_FAMILIES"]

DS_V2 = Path("outputs/perception/ds_v2")
GATE_VALIDATION = Path("outputs/perception/gate_validation")
GATE = Path("outputs/perception/gate")
STRESS_MANIFEST = GATE / "stress" / "stress_manifest.json"

#: NOTE-048 判定為與 ds_v2 重複身分的 family。以 (class, family_index) 具名，
#: 不以索引推論 —— 規則本身是 hash 相等，這裡只是把當時的判定結果寫死。
EXCLUDED_FAMILIES: frozenset[tuple[str, int]] = frozenset({("Empty", 27)})


def _manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _frozen_rule():
    from pcmef.perception.gate import GateRule

    spec = _manifest(GATE / "gate_rule.json")["gate_rule"]
    return GateRule(
        q_vision_threshold=spec["q_vision_threshold"],
        q_tof_threshold=spec["q_tof_threshold"],
        disagreement_threshold=spec["disagreement_threshold"],
        fusion_weight=spec["fusion_weight"],
        temperature_vision=spec["temperature_vision"],
        temperature_tof=spec["temperature_tof"],
    )


def _signals_for_clean_pool(samples: list[dict[str, Any]]):
    """由 clean gate-validation 樣本算出 p_V / p_T 與 D/U/Q。

    走的是與 llm_real_validation._compute_cases() 完全相同的路徑：
    同一組 frozen 權重、同一組 frozen 溫度、同一組 preprocessing 常數。
    """
    from pcmef.perception.dataset import _load_exr, _rgb_tonemap, _tof_transform
    from pcmef.perception.gate import (
        _logits, duq_signals, load_frozen_models, quality_signals, softmax,
    )

    vision_model, tof_model, preprocessing = load_frozen_models(DS_V2)
    rule = _frozen_rule()

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
    return duq_signals(p_vision, p_tof, quality), rule


def load_effective_pool() -> tuple[list[dict[str, Any]], np.ndarray, dict[str, Any]]:
    """回傳 (全部 96 筆樣本, effective 遮罩, exclusion 記錄)。"""
    manifest = _manifest(GATE_VALIDATION / "dataset_manifest.json")
    samples = manifest["samples"]
    keep = np.array(
        [
            (s["class_label"], s["family_index"]) not in EXCLUDED_FAMILIES
            for s in samples
        ]
    )
    kept = [s for s, k in zip(samples, keep) if k]
    families = {
        f"{s['class_label']}|{s['family_index']}": s["physical_scene_family"]
        for s in kept
    }
    return samples, keep, {
        "excluded_families": sorted(f"{c} f{f}" for c, f in EXCLUDED_FAMILIES),
        "n_samples_original": len(samples),
        "n_samples_effective": len(kept),
        "n_families_effective": len(families),
        "effective_pool_hash": hash_object(dict(sorted(families.items()))),
    }


def refit_reliability(progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """在 effective 93 上以**相同 anchor rule** 重擬合 reliability anchors。"""
    from pcmef.perception.gate import (
        RELIABILITY_MODEL_VERSION, fit_reliability_model, reliability_route,
        reliability_scores,
    )

    say = progress or (lambda _m: None)
    samples, keep, exclusion = load_effective_pool()
    say(f"  clean pool: {len(samples)} original -> {int(keep.sum())} effective")
    signals, rule = _signals_for_clean_pool(samples)

    def fit_on(mask: np.ndarray) -> dict[str, Any]:
        subset = {k: v[mask] for k, v in signals.items()}
        model = fit_reliability_model(
            subset, np.ones(int(mask.sum()), dtype=bool), rule
        )
        q = reliability_scores(signals, model)
        routes = reliability_route(q, signals, rule)
        # 路由計數一律只在 effective 93 上報 —— 那是唯一的 production pool。
        names, counts = np.unique(routes[keep], return_counts=True)
        return {
            "fitted_on_n": int(mask.sum()),
            "anchors": {
                k: v for k, v in model.to_dict().items() if isinstance(v, float)
            },
            "route_counts_over_effective_93": {
                str(n): int(c) for n, c in zip(names, counts)
            },
            "q_vision_over_effective_93": {
                "mean": float(q["q_vision"][keep].mean()),
                "min": float(q["q_vision"][keep].min()),
                "max": float(q["q_vision"][keep].max()),
            },
            "q_tof_over_effective_93": {
                "mean": float(q["q_tof"][keep].mean()),
                "min": float(q["q_tof"][keep].min()),
                "max": float(q["q_tof"][keep].max()),
            },
            "_routes": routes[keep],
        }

    before = fit_on(np.ones(len(samples), dtype=bool))   # 原始 96（有缺陷的那一版）
    after = fit_on(keep)                                  # 更正後 93
    say(f"  before anchors: {before['anchors']}")
    say(f"  after  anchors: {after['anchors']}")

    moved = int((before.pop("_routes") != after.pop("_routes")).sum())
    return {
        "algorithm_version": RELIABILITY_MODEL_VERSION,
        "algorithm_unchanged": True,
        "feature_schema_unchanged": True,
        "anchor_rule_unchanged": True,
        "exclusion": exclusion,
        "before_original_96": before,
        "after_effective_93": after,
        "cases_whose_route_changed": moved,
        "changed": before["anchors"] != after["anchors"],
    }


def rerun_gate_search(progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """以**相同既定程序**在 effective-93 stress set 上重跑一次 gate search。

    不重建 stress set：既有的那一份就是當初擬合所用的 372 列
    （93 base x 4 condition），重建會換掉 stress seed 而讓比較失去意義。
    """
    from pcmef.perception.gate import (
        _logits, _prepare, _quality_batch, duq_signals, fit_gate_rule,
        load_frozen_models, softmax,
    )

    say = progress or (lambda _m: None)
    manifest = _manifest(STRESS_MANIFEST)
    rows = manifest["rows"]
    families = {(r["class_label"], r["family_index"]) for r in rows}
    contaminated = sorted(families & EXCLUDED_FAMILIES)
    if contaminated:
        raise RuntimeError(
            f"the stress manifest still contains excluded families {contaminated}; "
            "the gate search pool is not the effective one"
        )

    vision_model, tof_model, preprocessing = load_frozen_models(DS_V2)
    rule = _frozen_rule()
    say(f"  gate search pool: {len(rows)} rows / {len(families)} families")

    rgb_x, tof_x = _prepare(rows, preprocessing)
    labels = np.asarray([r["class_index"] for r in rows])
    p_vision = softmax(_logits(vision_model, rgb_x), rule.temperature_vision)
    p_tof = softmax(_logits(tof_model, tof_x), rule.temperature_tof)
    signals = duq_signals(p_vision, p_tof, _quality_batch(rows))

    refit, search = fit_gate_rule(
        p_vision, p_tof, signals, labels,
        rule.temperature_vision, rule.temperature_tof, rule.fusion_weight, say,
    )

    frozen = rule.to_dict()
    recomputed = refit.to_dict()
    fields = (
        "q_vision_threshold", "q_tof_threshold", "disagreement_threshold",
        "fusion_weight", "temperature_vision", "temperature_tof",
    )
    differences = {
        f: {"frozen": frozen[f], "recomputed": recomputed[f]}
        for f in fields
        if abs(float(frozen[f]) - float(recomputed[f])) > 1e-12
    }
    return {
        "pool": {
            "n_rows": len(rows),
            "n_families": len(families),
            "conditions": manifest["counts"],
            "severity": manifest["severity"],
            "stress_manifest": STRESS_MANIFEST.as_posix(),
        },
        "procedure_unchanged": True,
        "temperature_unchanged": True,
        "severity_unchanged": True,
        "frozen": {f: frozen[f] for f in fields},
        "recomputed": {f: recomputed[f] for f in fields},
        "differences": differences,
        "reproduces_frozen_gate": not differences,
        "search": {
            "objective": search["objective"],
            "gate_validation_accuracy": search["gate_validation_accuracy"],
            "route_counts": search["route_counts"],
            "escalation_rate": search["escalation_rate"],
        },
        "search_grid_hash": hash_object(search["search_space"]),
    }


def build_corrective_report(
    out_dir: str | Path = "outputs/corrective",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    say = progress or (lambda _m: None)
    say("refitting reliability anchors on the effective 93")
    reliability = refit_reliability(say)
    say("re-running the gate search on the effective-93 stress set")
    gate = rerun_gate_search(say)

    delta_before = gate["frozen"]["disagreement_threshold"]
    delta_after = gate["recomputed"]["disagreement_threshold"]
    delta_changed = abs(delta_before - delta_after) > 1e-12

    document = {
        "report_id": "pre_final_corrective_pass",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scientific_result": False,
        "defect": (
            "NOTE-048's duplicate-identity exclusion was applied by "
            "gate.run_gate_validation (temperature, severity, stress set and the "
            "gate search all ran on the effective 93) but NOT by "
            "llm_real_validation._compute_cases, which fitted the reliability "
            "anchors on the original 96. This pass applies the same exclusion to "
            "the reliability fit."
        ),
        "not_a_tuning_decision": (
            "algorithm, feature schema, anchor rule, temperatures, stress ladder, "
            "severity rule, CNN weights, prompts and LLM model are all unchanged. "
            "The only difference is the fitting pool."
        ),
        "reliability": reliability,
        "gate": gate,
        "conflict_operational_delta": {
            "before": delta_before,
            "after": delta_after,
            "changed": delta_changed,
            "action": (
                "conflict_operational.delta must be resynchronised"
                if delta_changed
                else "unchanged; conflict_operational needs no new identity"
            ),
        },
        "FINAL_E2_36_43_TOUCHED": "NO",
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pre_final_corrective.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document
