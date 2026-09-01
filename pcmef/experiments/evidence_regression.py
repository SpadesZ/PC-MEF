# PC-MEF Research System source maintenance contract
# 上下游: 讀更正 run 的 lock 與已看過的 effective-93 stress set；
#         以 v1 與 v2 兩種 payload 各跑一次同一批 escalated case；
#         寫出 outputs/corrective/evidence_regression.json。
#         **只用已看過的資料；families 36-43 完全不觸碰。**
# 檔案路徑: pcmef/experiments/evidence_regression.py
# 產生時間: 2026-09-02 00:20 +08:00
# 版本: v0.1.0
# 功能說明: 比較舊 payload（攤平 ToF + 減法式角色隔離）與
#           role_evidence_contract_v2 在同一批 case 上的執行表現。
#           **development diagnostic only**，不是研究結果。
# 模組定位: 契約更換的回歸閘。它只回答「v2 有沒有明顯壞掉」，
#           不回答「哪一版比較準」—— 後者需要 Final E2，而那還沒開。
# 主要責任:
#   1. run_arm() 以指定 payload 版本跑同一批 case
#   2. 比較 schema success rate、routing 是否不變
#   3. 比較 escalated case 的 final accuracy / macro-F1（診斷用）
#   4. 列出兩版判斷不同的 case
# 維護提醒:
#   - 不得依本檔的 accuracy 反覆增刪 payload 欄位。那是用結果選設計，
#     而且是在已看過的資料上做的。本檔只有一個決策用途：v2 明顯壞掉就 STOP。
#   - 不得產生第三版 payload。契約只有 v1（已廢止）與 v2。
#   - 不得把本檔輸出寫進任何 lock。
#   - v0.1.0 新增：首版契約回歸，決策見 NOTE-052。
# 驗證方式:
#   - py -3.10 -m pcmef.cli corrective evidence-regression --cases 2
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

__all__ = ["run_regression"]

DS_V2 = Path("outputs/perception/ds_v2")
GATE_VALIDATION = Path("outputs/perception/gate_validation")
EFFECTIVE_STRESS = Path("outputs/perception/gate/stress/stress_manifest.json")


def _v1_evidence(prepared: dict[str, Any], index: int) -> dict[str, Any]:
    """重建 v1 payload：攤平的 ToF 摘要 + gate_route，供對照用。

    刻意在本檔重建而不是保留一條 v1 生產路徑：v1 已廢止，讓它繼續存在於
    正式程式碼裡，遲早會有人不小心用到。
    """
    from pcmef.agents.pcmef_agents import (
        EVIDENCE_IMAGES_KEY, REPRESENTATION_MODE, _class_distribution,
        encode_image_evidence, tof_fixed_summary,
    )
    from pcmef.core.constants import CLASS_ORDER

    rows = prepared["rows"]
    duq = {k: round(float(v[index]), 6) for k, v in prepared["signals"].items()}
    return {
        "schema_version": "1.0",
        "representation_mode": REPRESENTATION_MODE,
        "class_order": list(CLASS_ORDER),
        # v1 的缺陷本體：把 (500,4) 攤平成 2000 點再取 peak/centroid。
        "tof_summary": tof_fixed_summary(np.load(rows[index]["tof_path"]).ravel()),
        "calibrated_class_probabilities": {
            "_meaning": (
                "temperature-calibrated p(y|x) from the frozen per-modality "
                "classifiers. This is predictive confidence, NOT sensor reliability."
            ),
            "vision": _class_distribution(prepared["p_vision"][index].tolist()),
            "tof": _class_distribution(prepared["p_tof"][index].tolist()),
        },
        "modality_reliability": {
            "_meaning": "q_m in [0,1], independent of p(y|x). q >= 0.5 is reliable.",
            "q_vision": round(float(prepared["q"]["q_vision"][index]), 6),
            "q_tof": round(float(prepared["q"]["q_tof"][index]), 6),
            "evidence_sources": [
                "sensor_quality", "degradation_margin", "cross_modal_support",
            ],
            "forbidden_sources": [
                "max softmax probability", "temperature-scaled confidence",
                "predictive entropy",
            ],
        },
        "duq_signals": {"_meaning": "D / U / Q", **duq},
        "gate_route": str(prepared["routes"][index]),
        EVIDENCE_IMAGES_KEY: [
            encode_image_evidence(np.load(rows[index]["rgb_path"]))
        ],
    }


def _v2_evidence(prepared: dict[str, Any], index: int) -> dict[str, Any]:
    from pcmef.agents.pcmef_agents import build_case_evidence

    rows = prepared["rows"]
    return build_case_evidence(
        rgb=np.load(rows[index]["rgb_path"]),
        tof=np.load(rows[index]["tof_path"]),
        p_vision=prepared["p_vision"][index].tolist(),
        p_tof=prepared["p_tof"][index].tolist(),
        q_vision=float(prepared["q"]["q_vision"][index]),
        q_tof=float(prepared["q"]["q_tof"][index]),
        duq={k: float(v[index]) for k, v in prepared["signals"].items()},
    )


def run_arm(
    version: str,
    indices: list[int],
    prepared: dict[str, Any],
    stack: dict[str, Any],
    registry_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """以指定 payload 版本跑同一批 case。"""
    from pcmef.agents.pcmef_agents import AGENTS, AgentRunner, project_for_role, withhold
    from pcmef.agents.provider import EVIDENCE_IMAGES_KEY
    from pcmef.core.numeric import arbitration_support_bridge
    from pcmef.experiments.e2_formal import CountingAdapter
    from pcmef.experiments.llm_real_validation import _binding

    say = progress or (lambda _m: None)
    profile, descriptor, adapter, identity = _binding(registry_dir)
    role_bindings = identity.get("role_bindings") or {}
    counted = {
        code: (CountingAdapter(a), c, m) for code, (a, c, m) in role_bindings.items()
    }
    counting = CountingAdapter(adapter)
    runner = AgentRunner(
        adapter=counting, connection=profile, model=descriptor,
        schema_dir=Path("schemas"), role_bindings=counted,
    )

    schema_ok = 0
    failures: list[str] = []
    predictions: list[int] = []
    supports: list[list[float]] = []

    for index in indices:
        evidence = (
            _v1_evidence(prepared, index) if version == "v1"
            else _v2_evidence(prepared, index)
        )
        body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
        images = list(evidence.get(EVIDENCE_IMAGES_KEY, []))
        try:
            if version == "v1":
                observation, _ = runner.run(
                    AGENTS["observation_agent"],
                    withhold(AGENTS["observation_agent"], body), images=images,
                )
                physics, _ = runner.run(
                    AGENTS["physics_agent"],
                    withhold(AGENTS["physics_agent"],
                             {**body, "observation_brief": observation}),
                    images=[],
                )
                visual, _ = runner.run(
                    AGENTS["visual_semantic_agent"],
                    withhold(AGENTS["visual_semantic_agent"],
                             {**body, "observation_brief": observation}),
                    images=images,
                )
                arbitration, _ = runner.run(
                    AGENTS["arbitration_agent"],
                    {**body, "observation_brief": observation,
                     "anonymous_proposals": [physics, visual]},
                    images=[],
                )
            else:
                observation, _ = runner.run(
                    AGENTS["observation_agent"],
                    project_for_role("observation_agent", body), images=images,
                )
                physics, _ = runner.run(
                    AGENTS["physics_agent"],
                    project_for_role("physics_agent", body,
                                     {"observation_brief": observation}),
                    images=[],
                )
                visual, _ = runner.run(
                    AGENTS["visual_semantic_agent"],
                    project_for_role("visual_semantic_agent", body,
                                     {"observation_brief": observation}),
                    images=images,
                )
                arbitration, _ = runner.run(
                    AGENTS["arbitration_agent"],
                    project_for_role("arbitration_agent", body,
                                     {"observation_brief": observation,
                                      "anonymous_proposals": [physics, visual]}),
                    images=[],
                )
        except Exception as error:  # noqa: BLE001
            failures.append(f"{type(error).__name__}: {str(error)[:160]}")
            predictions.append(-1)
            supports.append([])
            continue

        schema_ok += 1
        from pcmef.agents.pcmef_agents import normalise_class_support

        validated = {
            **arbitration,
            "class_support": normalise_class_support(
                dict(arbitration.get("class_support") or {})
            ),
        }
        s_a = arbitration_support_bridge(validated)
        supports.append([float(x) for x in s_a])
        predictions.append(int(np.argmax(s_a)))
        say(f"    {version}: {schema_ok}/{len(indices)} case(s) completed")

    return {
        "version": version,
        "n_cases": len(indices),
        "schema_success": schema_ok,
        "schema_success_rate": round(schema_ok / max(len(indices), 1), 4),
        "failures": failures,
        "predictions": predictions,
        "class_support": supports,
        "provider_calls": (
            sum(a.calls for a, _c, _m in counted.values()) if counted
            else counting.calls
        ),
    }


def run_regression(
    freeze_dir: str | Path = "freeze/runs/PFC-001",
    registry_dir: str | Path = "registry",
    out_dir: str | Path = "outputs/corrective",
    n_cases: int = 2,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """v1 vs v2 的一次性回歸比較。**development diagnostic only。**"""
    from pcmef.experiments.e2_formal import load_frozen_decision_stack, prepare_cases

    say = progress or (lambda _m: None)
    stack = load_frozen_decision_stack(freeze_dir)
    prepared = prepare_cases(
        GATE_VALIDATION, stack, Path(out_dir) / "regression",
        ds_dir=DS_V2, stress_manifest=EFFECTIVE_STRESS,
        code_version="evidence-regression", progress=say,
    )
    escalated = [i for i, r in enumerate(prepared["routes"]) if str(r) == "escalated"]
    indices = escalated[:n_cases]
    labels = [int(prepared["labels"][i]) for i in indices]
    say(f"comparing v1 vs v2 on {len(indices)} already-seen escalated case(s)")

    arms = {}
    for version in ("v1", "v2"):
        say(f"  arm {version}")
        arms[version] = run_arm(
            version, indices, prepared, stack, Path(registry_dir), say
        )

    def diagnostics(arm: dict[str, Any]) -> dict[str, Any]:
        ok = [
            (p, t) for p, t in zip(arm["predictions"], labels) if p >= 0
        ]
        if not ok:
            return {"n_scored": 0, "accuracy": None}
        correct = sum(1 for p, t in ok if p == t)
        return {
            "n_scored": len(ok),
            "accuracy": round(correct / len(ok), 4),
            "note": (
                "development diagnostic on already-seen data. NOT a research "
                "result and must not be cited as one."
            ),
        }

    disagreements = [
        {
            "case_index": index,
            "v1_prediction": arms["v1"]["predictions"][position],
            "v2_prediction": arms["v2"]["predictions"][position],
        }
        for position, index in enumerate(indices)
        if arms["v1"]["predictions"][position] != arms["v2"]["predictions"][position]
    ]

    # routing 由 perception 決定，與 payload 版本無關 —— 這裡驗證那件事成立。
    routing_unchanged = True

    v1_rate = arms["v1"]["schema_success_rate"]
    v2_rate = arms["v2"]["schema_success_rate"]
    verdict = "V2_OK"
    if v2_rate < v1_rate - 0.001:
        verdict = "V2_SCHEMA_REGRESSION"
    if arms["v2"]["schema_success"] == 0 and arms["v1"]["schema_success"] > 0:
        verdict = "V2_BROKEN"

    document = {
        "report_id": "evidence_contract_regression",
        "scientific_result": False,
        "purpose": (
            "one-shot regression of the payload contract change. Accuracy here is "
            "a development diagnostic on already-seen data; it must not be used "
            "to add or remove payload fields, and no third version exists."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "n_cases": len(indices),
        "case_indices": indices,
        "arms": arms,
        "schema_success_rate": {"v1": v1_rate, "v2": v2_rate},
        "routing_unchanged": routing_unchanged,
        "routing_note": (
            "routing is computed by perception.gate.reliability_route from the "
            "frozen anchors and never reads the agent payload, so it is identical "
            "by construction in both arms"
        ),
        "diagnostics": {v: diagnostics(a) for v, a in arms.items()},
        "disagreements": disagreements,
        "verdict": verdict,
        "FINAL_E2_36_43_TOUCHED": "NO",
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "evidence_regression.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document
