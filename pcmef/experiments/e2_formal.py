# PC-MEF Research System source maintenance contract
# 上下游: 讀 freeze/ 的 gate.lock / reliability_final.lock / statistics_config.lock，
#         outputs/perception/ds_v2 的 frozen 權重與 preprocessing，
#         以及一份 clean paired dataset manifest；
#         呼叫 perception.stress、perception.gate、perception.pcmef_orchestrator
#         與 agents.pcmef_agents；寫出 <out>/formal_e2_report.json。
#         **不重訓、不重搜門檻、不改溫度、不改 severity。**
# 檔案路徑: pcmef/experiments/e2_formal.py
# 產生時間: 2026-09-01 15:30 +08:00
# 版本: v0.1.0
# 功能說明: Full PC-MEF 的 Formal E2 執行器。完整走已凍結的決策架構 ——
#           frozen CNN -> frozen 溫度 -> D/U/Q 與獨立 q_v/q_t ->
#           reliability_route -> 四個真 agent -> arbitration_support_bridge ->
#           selective_escalation_bridge。
# 模組定位: 已凍結架構的**唯一** formal 執行入口。它「不是」gate.run_formal_e2
#           的改良版 —— 那一條走 apply_gate 加決定性替身仲裁，
#           自報 llm_arm_evaluated=False，不得用於 Full PC-MEF。
# 主要責任:
#   1. load_frozen_decision_stack() 由 lock 還原門檻、溫度與 reliability anchors
#   2. CountingAdapter 把「實際 provider 呼叫次數」與「escalated case 數」分開量
#   3. prepare_cases() 產生 stress 列並算出 p_V / p_T / D-U-Q / q_m / route
#   4. run_formal_e2_full() 逐 case 執行 selective escalation 並彙總
#   5. _cluster_statistics() 以 family cluster bootstrap 產出成對比較
# 維護提醒:
#   - 不得在本檔重新擬合任何東西。門檻、溫度、anchors 一律由 lock 還原；
#     refit 一次就等於 Formal E2 自己決定了自己的判準。
#   - 不得在 formal 模式讓 deterministic arbiter 頂替 LLM。那會讓
#     「PC-MEF 的 LLM 臂」在報告上成立而實際從未執行。
#   - 不得 drop 任何 case。retry 耗盡就是 ABORT_FORMAL_RUN；
#     少一個 case 就是分母悄悄變小，那正是 bounded retry 要防的事。
#   - 不得對 non-escalated case 呼叫 provider。selective escalation 的整個
#     論點就是「傳統證據足夠就不叫 LLM」，多叫一次就推翻了它。
#   - 不得改用 scenario-level bootstrap。statistics_config.lock 凍的是
#     physical_scene_family cluster，且所有方法共用同一組 cluster。
#   - v0.1.0 新增：首版 Full PC-MEF formal executor，決策見 NOTE-051。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_formal_executor.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from pcmef.core.constants import CLASS_ORDER

__all__ = [
    "FormalE2Error",
    "CountingAdapter",
    "load_frozen_decision_stack",
    "prepare_cases",
    "run_formal_e2_full",
]

DS_V2 = Path("outputs/perception/ds_v2")


class FormalE2Error(RuntimeError):
    """Formal E2 的前提不成立。一律 fail-closed，不得降級繼續。"""


# ---------------------------------------------------------------------------
# provider 呼叫計數
# ---------------------------------------------------------------------------


@dataclass
class CountingAdapter:
    """包住 provider adapter，數**實際送出的請求**。

    escalated case 數與實際呼叫次數必須分開量：一個 case 會呼叫四個角色，
    而 retry 會讓某個角色被呼叫不只一次。把兩者混為一談的話，
    「non-escalated 不呼叫 provider」這條就無法被獨立驗證。
    """

    inner: Any
    calls: int = 0
    calls_by_task: dict[str, int] = field(default_factory=dict)

    def invoke(self, connection, model, task_code, payload, runtime_cfg):
        self.calls += 1
        key = str(task_code)
        self.calls_by_task[key] = self.calls_by_task.get(key, 0) + 1
        return self.inner.invoke(connection, model, task_code, payload, runtime_cfg)

    def __getattr__(self, name: str) -> Any:
        # 其餘 adapter 介面原樣轉發；只有 invoke 需要被計數。
        return getattr(self.inner, name)


# ---------------------------------------------------------------------------
# 由 lock 還原決策堆疊
# ---------------------------------------------------------------------------


def load_frozen_decision_stack(freeze_dir: str | Path = "freeze") -> dict[str, Any]:
    """由 lock 還原 GateRule、ReliabilityModel 與統計設定。**不重新擬合。**"""
    from pcmef.core.locks import LockStore
    from pcmef.perception.gate import GateRule, ReliabilityModel

    store = LockStore(freeze_dir)
    store.require("gate", "reliability_final", "statistics_config")
    gate = store.load("gate")
    reliability = store.load("reliability_final")
    statistics = store.load("statistics_config")

    rule = GateRule(
        q_vision_threshold=gate["q_vision_threshold"],
        q_tof_threshold=gate["q_tof_threshold"],
        disagreement_threshold=gate["disagreement_threshold"],
        fusion_weight=gate["fusion_weight"],
        temperature_vision=gate["temperature_vision"],
        temperature_tof=gate["temperature_tof"],
    )
    stats = reliability["fitted_statistics"]
    model = ReliabilityModel(
        q_vision_clean_median=stats["q_vision_clean_median"],
        q_tof_clean_median=stats["q_tof_clean_median"],
        q_vision_degraded_anchor=stats["q_vision_degraded_anchor"],
        q_tof_degraded_anchor=stats["q_tof_degraded_anchor"],
        q_vision_scale=stats["q_vision_scale"],
        q_tof_scale=stats["q_tof_scale"],
    )
    return {
        "rule": rule,
        "reliability_model": model,
        "bootstrap_replicates": statistics["bootstrap_replicates"],
        "bootstrap_seed": statistics["bootstrap_seed"],
        "resample_unit": statistics["resample_unit"],
        "stratification": statistics["stratification"],
        "routing_policy_version": gate["routing_policy_version"],
        "decision_bridge_version": gate["decision_bridge_version"],
        "lock_hashes": {
            name: store.load_hash(name)
            for name in ("gate", "reliability_final", "statistics_config",
                         "validation_pool", "conflict_operational",
                         "e2_sample_size", "llm_runtime", "agent_schema",
                         "inference_firewall")
            if store.exists(name)
        },
    }


# ---------------------------------------------------------------------------
# case 準備
# ---------------------------------------------------------------------------


def prepare_cases(
    base_manifest_dir: str | Path,
    stack: dict[str, Any],
    out_dir: str | Path,
    ds_dir: str | Path = DS_V2,
    severity: dict[str, float] | None = None,
    code_version: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """由 clean base manifest 產生四 condition 的 stress 列並算出決策訊號。"""
    from pcmef.perception.gate import (
        _logits, _prepare, _quality_batch, duq_signals, load_frozen_models,
        reliability_route, reliability_scores, softmax,
    )
    from pcmef.perception.stress import build_stress_dataset

    say = progress or (lambda _m: None)
    rule, model = stack["rule"], stack["reliability_model"]
    vision_model, tof_model, preprocessing = load_frozen_models(ds_dir)

    base = json.loads(
        (Path(base_manifest_dir) / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    if severity is None:
        raise FormalE2Error(
            "severity must come from the frozen gate-validation selection; "
            "this executor must not select it"
        )
    say(f"building the stress set at severity {severity}")
    stress = build_stress_dataset(
        base, severity["vision"], severity["tof"],
        Path(out_dir) / "stress", code_version, say,
    )
    rows = stress["rows"]

    rgb_x, tof_x = _prepare(rows, preprocessing)
    labels = np.asarray([r["class_index"] for r in rows])
    p_vision = softmax(_logits(vision_model, rgb_x), rule.temperature_vision)
    p_tof = softmax(_logits(tof_model, tof_x), rule.temperature_tof)
    signals = duq_signals(p_vision, p_tof, _quality_batch(rows))
    q = reliability_scores(signals, model)
    routes = reliability_route(q, signals, rule)

    say(f"  {len(rows)} rows; routes " + json.dumps(
        {str(k): int(v) for k, v in zip(*np.unique(routes, return_counts=True))}
    ))
    return {
        "rows": rows,
        "labels": labels,
        "p_vision": p_vision,
        "p_tof": p_tof,
        "signals": signals,
        "q": q,
        "routes": routes,
        "stress": stress,
        "base_manifest": base,
    }


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------


def _macro_f1(truth: np.ndarray, predicted: np.ndarray) -> float:
    from pcmef.perception.baselines import confusion_matrix, macro_f1

    return macro_f1(confusion_matrix(truth, predicted))[0]


def _accuracy(truth: np.ndarray, predicted: np.ndarray) -> float:
    return float((truth == predicted).mean())


def _cluster_statistics(
    labels: np.ndarray,
    predictions: dict[str, np.ndarray],
    rows: Sequence[dict[str, Any]],
    stack: dict[str, Any],
    reference: str,
) -> dict[str, Any]:
    """physical_scene_family cluster bootstrap。所有方法共用同一組 cluster。"""
    from pcmef.stats.bootstrap import cluster_bootstrap_delta

    clusters = [r["physical_scene_family"] for r in rows]
    strata = [r["class_label"] for r in rows]
    out = {}
    for metric_name, metric in (("accuracy", _accuracy), ("macro_f1", _macro_f1)):
        out[metric_name] = cluster_bootstrap_delta(
            labels, predictions, clusters, strata, metric, reference,
            replicates=stack["bootstrap_replicates"],
            seed=stack["bootstrap_seed"],
        )
    return out


# ---------------------------------------------------------------------------
# 主執行器
# ---------------------------------------------------------------------------


def run_formal_e2_full(
    base_manifest_dir: str | Path,
    out_dir: str | Path,
    *,
    freeze_dir: str | Path = "freeze",
    ds_dir: str | Path = DS_V2,
    severity: dict[str, float] | None = None,
    agent_runner: Any = None,
    formal: bool = True,
    code_version: str = "",
    is_final_formal_e2: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Full PC-MEF 的 Formal E2。

    formal=True 時 escalated case **必須**有真正的四 agent 仲裁器；
    沒有就中止，不接受決定性替身。
    """
    from pcmef.agents.pcmef_agents import (
        RetryExhaustedError, build_case_evidence,
    )
    from pcmef.perception.pcmef_orchestrator import decide_case

    say = progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    stack = load_frozen_decision_stack(freeze_dir)
    rule = stack["rule"]
    say(
        f"frozen decision stack: bridge={stack['decision_bridge_version']} "
        f"routing={stack['routing_policy_version']} D>{rule.disagreement_threshold:.6f}"
    )

    prepared = prepare_cases(
        base_manifest_dir, stack, out, ds_dir=ds_dir, severity=severity,
        code_version=code_version, progress=say,
    )
    rows = prepared["rows"]
    labels = prepared["labels"]
    p_vision, p_tof = prepared["p_vision"], prepared["p_tof"]
    signals, q, routes = prepared["signals"], prepared["q"], prepared["routes"]

    counter: CountingAdapter | None = None
    if agent_runner is not None and isinstance(agent_runner.adapter, CountingAdapter):
        counter = agent_runner.adapter

    escalated_cases = 0
    llm_called_cases = 0
    calls_before_first_escalation: int | None = None
    finals = np.empty_like(p_vision)
    traces: list[dict[str, Any]] = []

    say(f"deciding {len(rows)} cases")
    for index, row in enumerate(rows):
        route = str(routes[index])
        evidence = None
        if route == "escalated":
            escalated_cases += 1
            if calls_before_first_escalation is None and counter is not None:
                calls_before_first_escalation = counter.calls
            # 證據只在 escalated 分支組裝：non-escalated 連 payload 都不建，
            # 因此不可能不小心送出去。
            evidence = build_case_evidence(
                rgb=np.load(row["rgb_path"]),
                tof=np.load(row["tof_path"]),
                p_vision=p_vision[index].tolist(),
                p_tof=p_tof[index].tolist(),
                q_vision=float(q["q_vision"][index]),
                q_tof=float(q["q_tof"][index]),
                duq={k: float(v[index]) for k, v in signals.items()},
                route=route,
            )
        try:
            decision = decide_case(
                route, p_vision[index], p_tof[index], rule.fusion_weight,
                evidence=evidence, agent_runner=agent_runner, formal=formal,
            )
        except RetryExhaustedError as error:
            # 明確不 drop：整場 run 中止。
            raise FormalE2Error(
                f"ABORT_FORMAL_RUN at row {index} ({row['stress_id']}): {error}"
            ) from None
        finals[index] = decision.final
        if decision.llm_called:
            llm_called_cases += 1
        traces.append({"stress_id": row["stress_id"], **decision.to_trace()})
        if (index + 1) % 48 == 0:
            say(f"  {index + 1}/{len(rows)} cases")

    if not np.all(np.isfinite(finals)):
        raise FormalE2Error("F(x) contains non-finite entries")
    sums = finals.sum(axis=1)
    if not np.allclose(sums, 1.0, atol=1e-9):
        raise FormalE2Error(
            f"F(x) rows do not sum to 1 (min {sums.min()}, max {sums.max()})"
        )

    fused = rule.fusion_weight * p_vision + (1.0 - rule.fusion_weight) * p_tof
    arms = {
        "vision_only": p_vision,
        "tof_only": p_tof,
        "fixed_fusion": fused,
        "pcmef_full": finals,
    }
    predictions = {name: proba.argmax(1) for name, proba in arms.items()}

    conditions = np.array([r["condition"] for r in rows])
    results = {
        name: {
            "accuracy": _accuracy(labels, predicted),
            "macro_f1": _macro_f1(labels, predicted),
            "n": int(len(labels)),
        }
        for name, predicted in predictions.items()
    }
    per_condition = {
        name: {
            condition: {
                "n": int((conditions == condition).sum()),
                "accuracy": _accuracy(
                    labels[conditions == condition], predicted[conditions == condition]
                ),
                "macro_f1": _macro_f1(
                    labels[conditions == condition], predicted[conditions == condition]
                ),
            }
            for condition in sorted(set(conditions.tolist()))
        }
        for name, predicted in predictions.items()
    }

    say("cluster bootstrap")
    statistics = {
        f"pcmef_full_vs_{baseline}": {
            metric: value["comparisons"][f"pcmef_full_vs_{baseline}"]
            for metric, value in _cluster_statistics(
                labels, predictions, rows, stack, baseline
            ).items()
        }
        for baseline in ("vision_only", "tof_only", "fixed_fusion")
    }
    cluster_shape = _cluster_statistics(
        labels, predictions, rows, stack, "vision_only"
    )["accuracy"]

    route_names, route_counts = np.unique(routes, return_counts=True)
    document = {
        "report_id": "formal_e2_full_pcmef",
        "scientific_result": bool(is_final_formal_e2),
        "is_final_formal_e2": is_final_formal_e2,
        "one_shot": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "formal_mode": formal,
        "llm_arm_evaluated": llm_called_cases > 0 or escalated_cases == 0,
        "architecture": {
            "routing_policy_version": stack["routing_policy_version"],
            "decision_bridge_version": stack["decision_bridge_version"],
            "path": (
                "RGB/ToF -> frozen CNNs -> frozen temperatures -> D/U/Q + "
                "independent q_v/q_t -> reliability_route -> "
                "{trust_vision: p_V | trust_tof: p_T | fusion: 0.5 p_V + 0.5 p_T | "
                "escalated: observation -> physics + visual_semantic -> arbitration "
                "-> arbitration_support_bridge_v1 -> s_A} -> "
                "selective_escalation_bridge_v1"
            ),
            "deterministic_substitute_used": False,
        },
        "frozen_lock_hashes": stack["lock_hashes"],
        "gate_rule": rule.to_dict(),
        "severity": severity,
        "severity_source": "frozen gate-validation selection; not re-selected here",
        "dataset": {
            "base": Path(base_manifest_dir).as_posix(),
            "family_indices": prepared["base_manifest"].get("family_indices"),
            "total_rows": len(rows),
            "counts": prepared["stress"]["counts"],
        },
        "routing": {
            "counts": {str(n): int(c) for n, c in zip(route_names, route_counts)},
            "escalated_cases": escalated_cases,
            "escalation_rate": float(escalated_cases / len(rows)),
            # 兩個數字刻意分開：一個 case 會呼叫四個角色，retry 還會再加。
            "llm_called_cases": llm_called_cases,
            "actual_provider_calls": None if counter is None else counter.calls,
            "actual_provider_calls_by_role": (
                None if counter is None else dict(counter.calls_by_task)
            ),
            "provider_calls_before_first_escalation": calls_before_first_escalation,
            "note": (
                "escalated_cases counts cases that routed to the arbiter; "
                "actual_provider_calls counts requests actually sent, which is "
                "four per escalated case plus any retry. Non-escalated cases make "
                "zero calls by construction: their evidence payload is never built."
            ),
        },
        "results": results,
        "per_condition": per_condition,
        "statistics": statistics,
        "statistics_config": {
            "replicates": stack["bootstrap_replicates"],
            "seed": stack["bootstrap_seed"],
            "resample_unit": stack["resample_unit"],
            "stratification": stack["stratification"],
            "n_clusters": cluster_shape["n_clusters"],
            "rows_per_cluster": cluster_shape["rows_per_cluster"],
            "clusters_per_stratum": cluster_shape["clusters_per_stratum"],
            "shared_clusters_across_methods": True,
        },
        "class_order": list(CLASS_ORDER),
        "traces": traces,
        "claim_boundary": (
            "Synthetic data only. s_A is a normalized Evidence-Support Score, not a "
            "calibrated posterior: no NLL or ECE calibration claim is made for s_A "
            "or F(x). The escalation rate is a finding, not a tuning target."
        ),
    }
    (out / "formal_e2_report.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document
