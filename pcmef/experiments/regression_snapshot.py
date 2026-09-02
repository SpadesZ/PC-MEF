# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.cli 的 `regression capture` / `regression verify` 呼叫；
#         讀 freeze/runs/PFC-001 的 lock、outputs/perception/ds_v2 的
#         checkpoint 與 outputs/perception/gate/stress 的 effective-93
#         stress set；寫 regression/provisional/。
# 檔案路徑: pcmef/experiments/regression_snapshot.py
# 產生時間: 2026-09-02 10:20 +08:00
# 版本: v0.1.0
# 功能說明: 在重構之前把 deterministic scientific behaviour 拍成一份快照，
#           之後每次重構完再比對一次，用來證明「只是搬程式，沒有改行為」。
# 模組定位: SAI v0.6.0 §6 Thesis Profile Golden Regression 的 **provisional**
#           前身。它「不是」canonical Golden Baseline —— 見 CANONICAL_BLOCKERS。
#           canonical baseline 必須等 AMD-007、llm_runtime、formal_config 與
#           **最終 paid runtime identity** 都確定後才能取，否則會把
#           「code=v2 / lock=v1」的不一致固化成黃金標準。
# 主要責任:
#   1. capture_snapshot() 產生 TGR-01~08 / 10 / 12 的 deterministic 指紋
#   2. verify_snapshot() 逐項比對並回報第一個不一致的欄位
#   3. 明確宣告 canonical=false 與尚未涵蓋的 TGR 項目及其原因
#   4. 全程只讀 already-opened 的 effective-93，不觸碰 families 36-43
# 維護提醒:
#   - 不得把本檔的產出稱為 Golden Baseline。名稱本身就是治理承諾：
#     叫它 Golden 等於宣稱 canonical identity 已確定，而目前尚未確定。
#   - 不得為了讓 verify 通過而放寬比對。指紋不合就是行為變了，
#     要嘛回頭修重構，要嘛先說明為什麼這個改變是預期的並重新 capture。
#   - 不得改成重建 stress set。必須沿用既有 stress manifest：重建會換掉
#     stress seed，於是比對的就不是同一份資料（與 e2_executor_validation
#     同一個理由）。
#   - 不得把 families 36-43 納入。snapshot 是開發期工具，final partition
#     在 Formal Entry Gate 通過前一律 sealed。
#   - v0.1.0 新增：首版，對應 P0-0。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_regression_snapshot.py -v
#   - py -3.10 -m pcmef.cli regression capture
#   - py -3.10 -m pcmef.cli regression verify
# ------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

__all__ = [
    "SnapshotError",
    "CANONICAL_BLOCKERS",
    "COVERED_TGR",
    "UNCOVERED_TGR",
    "DEFAULT_SNAPSHOT_PATH",
    "capture_snapshot",
    "verify_snapshot",
]

DS_V2 = Path("outputs/perception/ds_v2")
GATE_VALIDATION = Path("outputs/perception/gate_validation")
EFFECTIVE_STRESS_MANIFEST = Path("outputs/perception/gate/stress/stress_manifest.json")
ACTIVE_FREEZE_DIR = Path("freeze/runs/PFC-001")
DEFAULT_SNAPSHOT_PATH = Path("regression/provisional/thesis_regression_snapshot.json")

#: 為什麼這份快照還不能叫 Golden。每一條都必須先解除。
CANONICAL_BLOCKERS: tuple[str, ...] = (
    "AMD-007 is written but not frozen: the evidence-contract v2 change has not "
    "been validated against a real provider.",
    "llm_runtime.lock still records the pre-v2 runtime identity. FIXED_SUMMARY is "
    "still the correct representation mode name; what is stale is that the lock "
    "does not yet reflect tof_fixed_summary_v2_channel_preserving, the "
    "role_evidence_contract_v2 additive whitelist, timeout_sec=120, or the new "
    "role connection topology and runtime_config_hash.",
    "formal_config.lock inherits that stale llm_runtime identity.",
    "The paid connection identity for the Final E2 run is not yet fixed. If "
    "secret_ref or role_connection_map change when billing is enabled, the "
    "runtime identity changes again and any baseline taken before that expires.",
)

#: 本快照涵蓋的 TGR 項目（SAI v0.6.0 §6.2）。
COVERED_TGR: dict[str, str] = {
    "TGR-01": "class order",
    "TGR-02": "scenario identity",
    "TGR-03": "sensor observation schema",
    "TGR-04": "ToF channel semantics",
    "TGR-05": "perception prediction",
    "TGR-06": "reliability values",
    "TGR-07": "route decision",
    "TGR-08": "LLM role evidence projection",
    "TGR-10": "statistics resample unit/seed",
    "TGR-12": "truth firewall",
}

#: 尚未涵蓋的 TGR 項目與原因。
UNCOVERED_TGR: dict[str, str] = {
    "TGR-09": (
        "selective escalation result requires executing the four real agents. "
        "The free-tier daily quota is exhausted, and a cached deterministic "
        "substitute would validate the substitute rather than the production path."
    ),
    "TGR-11": (
        "frozen formal config hash semantics / no silent fallback is what the "
        "ACTIVE_LINEAGE resolver introduces in P0-1. There is nothing to fingerprint "
        "until that resolver exists."
    ),
}


class SnapshotError(RuntimeError):
    """快照無法產生或無法比對。一律 fail-closed。"""


# ---------------------------------------------------------------------------
# 指紋
# ---------------------------------------------------------------------------


def _digest_array(array: np.ndarray, decimals: int = 12) -> str:
    """浮點陣列的穩定指紋。

    先 round 再 hash：最後一兩個 ulp 的差異不該被當成「行為改變」，
    但任何實質改變都會在第 12 位小數之前顯現。
    """
    rounded = np.round(np.asarray(array, dtype=np.float64), decimals)
    # +0.0 與 -0.0 的位元表示不同但數值相同，統一掉才不會誤報。
    rounded = rounded + 0.0
    return hashlib.sha256(np.ascontiguousarray(rounded).tobytes()).hexdigest()


def _digest_json(value: Any) -> str:
    """任意 JSON-able 結構的穩定指紋。鍵排序，確保與插入順序無關。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _structure(value: Any) -> Any:
    """只保留結構（鍵與型別），丟掉數值。

    用於 evidence projection：要釘住的是「哪個角色收到哪些欄位」，
    而不是這一筆 case 的數字，否則換一筆 case 就整份不合。
    """
    if isinstance(value, dict):
        return {key: _structure(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_structure(value[0])] if value else []
    return type(value).__name__


# ---------------------------------------------------------------------------
# 各 TGR 項目
# ---------------------------------------------------------------------------


def _tgr_01_class_order() -> dict[str, Any]:
    from pcmef.core.constants import CLASS_ORDER
    from pcmef.agents import pcmef_agents

    # agent 端必須是 import 來的同一個物件，不是自己抄一份字面值。
    same_object = pcmef_agents.CLASS_ORDER is CLASS_ORDER
    return {
        "class_order": list(CLASS_ORDER),
        "source": "pcmef.core.constants.CLASS_ORDER",
        "agents_import_the_same_object": same_object,
        "digest": _digest_json(list(CLASS_ORDER)),
    }


def _tgr_02_scenario_identity(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    identity = [
        {
            "stress_id": r["stress_id"],
            "base_scenario_id": r["base_scenario_id"],
            "physical_scene_family": r["physical_scene_family"],
            "condition": r["condition"],
            "class_index": int(r["class_index"]),
        }
        for r in rows
    ]
    families = sorted({str(r["physical_scene_family"]) for r in rows})
    return {
        "n_rows": len(rows),
        "n_families": len(families),
        "n_base_scenarios": len({r["base_scenario_id"] for r in rows}),
        "conditions": sorted({str(r["condition"]) for r in rows}),
        "empty_f27_present": ("Empty", 27) in {
            (r["class_label"], r["family_index"]) for r in rows
        },
        "digest": _digest_json(identity),
    }


def _tgr_03_observation_schema(preprocessing: Mapping[str, Any]) -> dict[str, Any]:
    from pcmef.core.constants import (
        N_TOF_FEATURES, TOF_RECORDING_POINTS, TOF_RECORDING_SHAPE, TOF_SCHEMA,
    )

    return {
        "tof_schema": list(TOF_SCHEMA),
        "tof_recording_shape": list(TOF_RECORDING_SHAPE),
        "tof_recording_points": TOF_RECORDING_POINTS,
        "n_tof_features": N_TOF_FEATURES,
        "preprocessing_digest": _digest_json(preprocessing),
        "digest": _digest_json(
            {
                "schema": list(TOF_SCHEMA),
                "shape": list(TOF_RECORDING_SHAPE),
                "preprocessing": preprocessing,
            }
        ),
    }


def _flatten_numbers(node: Any, prefix: str = "") -> dict[str, float]:
    """把巢狀 summary 攤成 `channels.distance_mm.mean` 這種點分路徑。

    v2 的輸出是巢狀的（channels/derived 各一層），逐 key 比對必須先攤平，
    否則只會看到最外層那幾個非數值欄位，然後誤以為「沒有數值會變」。
    """
    out: dict[str, float] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).startswith("_"):
                continue
            out.update(_flatten_numbers(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, bool):
        pass
    elif isinstance(node, (int, float)):
        out[prefix] = float(node)
    return out


def _tgr_04_tof_channel_semantics(tof: np.ndarray) -> dict[str, Any]:
    """v2 摘要必須逐 channel 獨立，且只有 temporal_diff_std 對時間順序敏感。

    這兩條是 v2 之所以取代 flatten 的全部理由，所以在快照裡實測而不是引述。
    """
    from pcmef.agents.pcmef_agents import tof_fixed_summary_v2_channel_preserving
    from pcmef.core.constants import TOF_SCHEMA

    summary = tof_fixed_summary_v2_channel_preserving(tof)
    flat = _flatten_numbers(summary)

    # 打亂時間順序：只有 temporal_diff_std 允許改變。
    rng = np.random.default_rng(20260902)
    shuffled_flat = _flatten_numbers(
        tof_fixed_summary_v2_channel_preserving(tof[rng.permutation(tof.shape[0]), :])
    )
    order_sensitive = sorted(
        key
        for key, value in flat.items()
        if abs(value - shuffled_flat.get(key, value)) > 1e-9
    )

    # 只擾動 ambient channel：其他 channel 的統計量必須一個都不動。
    ambient_index = list(TOF_SCHEMA).index("ambient_rate_mcps")
    perturbed = np.asarray(tof, dtype=np.float64).copy()
    perturbed[:, ambient_index] = perturbed[:, ambient_index] * 3.0 + 1.0
    perturbed_flat = _flatten_numbers(
        tof_fixed_summary_v2_channel_preserving(perturbed)
    )
    contaminated = sorted(
        key
        for key, value in flat.items()
        if "ambient_rate_mcps" not in key
        and abs(value - perturbed_flat.get(key, value)) > 1e-9
    )

    return {
        "summary_version": summary.get("summary_version"),
        "channel_order": list(summary.get("channel_order", [])),
        "numeric_paths": sorted(flat),
        "n_numeric_values": len(flat),
        # 唯一允許隨時間順序改變的統計量。
        "order_sensitive_paths": order_sensitive,
        "order_invariant_count": len(flat) - len(order_sensitive),
        # 擾動 ambient 後仍會變的，只能是明確跨 channel 的 derived 量。
        "paths_contaminated_by_ambient_perturbation": contaminated,
        "no_cross_channel_waveform_statistic": not any(
            key in flat for key in ("peak_bin", "centroid_bin", "temporal_spread_bins")
        ),
        "digest": _digest_json(summary),
    }


def _tgr_05_perception_prediction(
    p_vision: np.ndarray, p_tof: np.ndarray
) -> dict[str, Any]:
    return {
        "p_vision_digest": _digest_array(p_vision),
        "p_tof_digest": _digest_array(p_tof),
        "p_vision_argmax_digest": _digest_json(p_vision.argmax(1).tolist()),
        "p_tof_argmax_digest": _digest_json(p_tof.argmax(1).tolist()),
        "shape": list(p_vision.shape),
    }


def _tgr_06_reliability_values(
    q: Mapping[str, np.ndarray], signals: Mapping[str, np.ndarray]
) -> dict[str, Any]:
    return {
        "q_digests": {name: _digest_array(value) for name, value in sorted(q.items())},
        "signal_digests": {
            name: _digest_array(value) for name, value in sorted(signals.items())
        },
    }


def _tgr_07_route_decision(routes: np.ndarray) -> dict[str, Any]:
    names, counts = np.unique(routes, return_counts=True)
    return {
        "counts": {str(n): int(c) for n, c in zip(names, counts)},
        "escalated": int((routes == "escalated").sum()),
        "digest": _digest_json([str(r) for r in routes]),
    }


class _RecordingRunner:
    """攔截 run_pcmef_case 的四次呼叫，記下每個角色**實際**收到什麼。

    不能只呼叫 project_for_role：image routing 根本不由它決定 ——
    `run_pcmef_case` 是用 `images=` 參數逐角色傳的。只驗 projection
    等於驗了契約的宣告，沒驗實作，而漂移正是發生在兩者之間。

    走完整條 run_pcmef_case 還有第二個好處：physics / visual / arbitration
    的 payload 含 observation_brief 與 anonymous_proposals，那是它們在
    正式執行時真正看到的東西。只餵 base evidence 會少掉這一層。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(
        self, spec: Any, payload: Mapping[str, Any], images: Any = None
    ) -> tuple[dict[str, Any], Any]:
        from pcmef.agents.provider import ProviderResponse

        task_code = str(getattr(spec, "task_code", getattr(spec, "name", "")))
        self.calls.append(
            {
                "task_code": task_code,
                "payload": dict(payload),
                "n_images": len(list(images or [])),
                "spec_needs_image": bool(getattr(spec, "needs_image", False)),
            }
        )
        return self._reply(task_code), ProviderResponse(
            text="", model_id="recording", provider="recording",
            request_id=f"recording-{len(self.calls)}",
        )

    @staticmethod
    def _reply(task_code: str) -> dict[str, Any]:
        """符合各 role schema 的最小合法回覆。內容不重要，路徑才重要。"""
        from pcmef.core.constants import CLASS_ORDER

        if task_code == "observation_agent":
            return {
                "visual_description": "recording", "tof_description": "recording",
                "notable_discrepancies": [],
            }
        if task_code in ("physics_agent", "visual_semantic_agent"):
            return {
                "modality": "physics" if task_code == "physics_agent" else "visual_semantic",
                "reasoning": "recording",
                "class_support": {name: 25.0 for name in CLASS_ORDER},
                "confidence": 0.5,
            }
        return {
            "reasoning": "recording",
            "class_support": {name: 25.0 for name in CLASS_ORDER},
            "decisive_evidence": "recording",
        }


def _record_role_payloads(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """跑一次完整的四 agent 流程，回傳每個角色實際收到的 payload。"""
    from pcmef.agents.pcmef_agents import run_pcmef_case

    recorder = _RecordingRunner()
    run_pcmef_case(recorder, evidence)
    return recorder.calls


def _tgr_08_role_evidence_projection(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """四個角色各自實際收到什麼。釘住結構與 image routing，不釘住數值。"""
    from pcmef.agents.pcmef_agents import (
        EVIDENCE_IMAGES_KEY, ROLE_EVIDENCE_CONTRACT_V2,
        ROLE_EVIDENCE_CONTRACT_VERSION,
    )

    roles: dict[str, Any] = {}
    call_order: list[str] = []
    for call in calls:
        task_code = call["task_code"]
        call_order.append(task_code)
        body = {k: v for k, v in call["payload"].items() if k != EVIDENCE_IMAGES_KEY}
        roles[task_code] = {
            "top_level_keys": sorted(body),
            # 實作：run_pcmef_case 真正傳出去的 images=
            "receives_image": call["n_images"] > 0,
            # 宣告一：role_evidence_contract_v2
            "contract_receives_image": bool(
                ROLE_EVIDENCE_CONTRACT_V2[task_code]["receives_image"]
            ),
            # 宣告二：AgentSpec.needs_image（另由 assert_registry_consistent
            # 與 capabilities.TASK_REGISTRY 的 VISION 需求對齊）
            "spec_needs_image": call["spec_needs_image"],
            "structure_digest": _digest_json(_structure(body)),
        }

    # image routing 有三個各自獨立的來源：契約宣告、AgentSpec 宣告、
    # 以及 run_pcmef_case 實際傳的 images=。三者任一漂移都不會有症狀，
    # 因此三者必須逐角色相等。
    routing_mismatches = sorted(
        task_code
        for task_code, item in roles.items()
        if not (
            item["receives_image"]
            == item["contract_receives_image"]
            == item["spec_needs_image"]
        )
    )

    return {
        "contract_version": ROLE_EVIDENCE_CONTRACT_VERSION,
        "call_order": call_order,
        "roles": roles,
        "image_routing": {
            task_code: roles[task_code]["receives_image"] for task_code in sorted(roles)
        },
        "image_routing_declaration_matches_implementation": not routing_mismatches,
        "image_routing_mismatches": routing_mismatches,
        "gate_route_withheld_from_every_role": all(
            "gate_route" not in item["top_level_keys"] for item in roles.values()
        ),
        "digest": _digest_json(roles),
    }


def _tgr_10_statistics(stack: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "resample_unit": stack["resample_unit"],
        "stratification": stack["stratification"],
        "bootstrap_replicates": stack["bootstrap_replicates"],
        "bootstrap_seed": stack["bootstrap_seed"],
        "routing_policy_version": stack["routing_policy_version"],
        "decision_bridge_version": stack["decision_bridge_version"],
        "digest": _digest_json(
            {
                "resample_unit": stack["resample_unit"],
                "stratification": stack["stratification"],
                "replicates": stack["bootstrap_replicates"],
                "seed": stack["bootstrap_seed"],
            }
        ),
    }


#: 任何一個出現在送往 provider 的 payload 裡，都是洩漏。
FORBIDDEN_EVIDENCE_KEYS: frozenset[str] = frozenset(
    {
        "class_label", "class_index", "condition", "severity", "vision_severity",
        "tof_severity", "physical_scene_family", "family_index", "scenario_id",
        "stress_id", "rgb_path", "tof_path", "base_scenario_id", "label", "truth",
        "gate_route",
    }
)

#: `assert_no_forbidden_tokens` 只施用於這些欄位。
#:
#: 它不能拿去掃整個 payload。class space 本來就公開，而且是結構必需：
#: `calibrated_class_probabilities` 與 `class_support` 都是以類別名為鍵的
#: 分布，`class_order` 是類別空間本身，agent 少了它根本無法輸出 class_support。
#: 這些欄位對**每一筆 case 都帶同樣的四個名字**，因此不透露眼前這筆是什麼。
#: 既有的 `check_no_leakage` 只掃 `gate_route` 正是同一個理由。
#:
#: 真正的洩漏風險是「隨 case 變動且對應真值」的東西 —— 那由
#: `_cross_condition_invariance()` 檢驗，比 token 掃描強得多。
TOKEN_SCAN_FIELDS: tuple[str, ...] = ("gate_route",)


def _non_numeric_content(node: Any, prefix: str = "") -> dict[str, str]:
    """抽出 payload 的所有非數值內容（字串、布林、鍵結構）。

    數值當然逐 case 不同 —— p_vision 本來就會變。會洩漏 condition 的是
    **文字**：一句提到 "degraded" 的散文、一個帶 condition 的 id、
    一個隨 severity 改寫的說明。這些在不同 condition 之間必須完全相同。
    """
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for key in sorted(node):
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_non_numeric_content(node[key], path))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            out.update(_non_numeric_content(item, f"{prefix}[{index}]"))
    elif isinstance(node, bool) or not isinstance(node, (int, float)):
        out[prefix] = str(node)
    return out


def _cross_condition_invariance(
    calls_a: list[dict[str, Any]], calls_b: list[dict[str, Any]],
    condition_a: str, condition_b: str,
) -> dict[str, Any]:
    """兩筆**不同 generation condition** 的 case，payload 的文字內容必須相同。

    這是比 token 掃描強的檢驗，也沒有誤報：如果 condition 以任何形式滲進
    payload —— 欄位、id、散文、旗標 —— 兩筆的非數值內容就會不一樣。
    數值不比對，數值本來就該不同。
    """
    from pcmef.agents.pcmef_agents import EVIDENCE_IMAGES_KEY

    by_role_a = {c["task_code"]: c for c in calls_a}
    by_role_b = {c["task_code"]: c for c in calls_b}
    differences: dict[str, list[str]] = {}

    for task_code in sorted(set(by_role_a) & set(by_role_b)):
        def body(call: dict[str, Any]) -> dict[str, Any]:
            return {
                k: v for k, v in call["payload"].items() if k != EVIDENCE_IMAGES_KEY
            }

        text_a = _non_numeric_content(body(by_role_a[task_code]))
        text_b = _non_numeric_content(body(by_role_b[task_code]))
        differing = sorted(
            path
            for path in set(text_a) | set(text_b)
            if text_a.get(path) != text_b.get(path)
        )
        if differing:
            differences[task_code] = differing

    return {
        "condition_a": condition_a,
        "condition_b": condition_b,
        "invariant": not differences,
        "differing_paths_by_role": differences,
        "note": (
            "Numeric values are deliberately not compared: they must differ. What "
            "must not differ is any text, identifier or flag, because that is how a "
            "generation condition would reach the model."
        ),
    }


def _tgr_12_truth_firewall(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """掃描四個角色實際收到的 payload，不是掃描 evidence bundle 本身。

    bundle 裡有什麼不重要，重要的是**真的送出去的**有什麼。因此掃的是
    `run_pcmef_case` 逐角色送出的 payload，含 observation_brief 與
    anonymous_proposals —— 洩漏也可能經由 agent 自己的輸出繞一圈回來。
    """
    from pcmef.agents.pcmef_agents import EVIDENCE_IMAGES_KEY
    from pcmef.core.inference_payload import (
        InferenceFirewallViolation, assert_no_forbidden_tokens,
    )

    found: dict[str, list[str]] = {}
    token_violations: dict[str, str] = {}

    def walk(node: Any, path: str, sink: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key) in FORBIDDEN_EVIDENCE_KEYS:
                    sink.append(f"{path}.{key}")
                walk(value, f"{path}.{key}", sink)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", sink)

    for call in calls:
        task_code = call["task_code"]
        body = {k: v for k, v in call["payload"].items() if k != EVIDENCE_IMAGES_KEY}
        sink: list[str] = []
        walk(body, task_code, sink)
        if sink:
            found[task_code] = sorted(sink)
        # 鍵名乾淨不代表值乾淨：condition 語意也可能藏在字串裡。
        scannable = {k: body.get(k, "") for k in TOKEN_SCAN_FIELDS}
        try:
            assert_no_forbidden_tokens(scannable, task_code)
        except InferenceFirewallViolation as error:
            token_violations[task_code] = str(error)[:200]

    return {
        "forbidden_keys_scanned": sorted(FORBIDDEN_EVIDENCE_KEYS),
        "roles_scanned": sorted(call["task_code"] for call in calls),
        "token_scan_fields": list(TOKEN_SCAN_FIELDS),
        "violations_by_role": found,
        "forbidden_token_violations": token_violations,
        "clean": not found and not token_violations,
    }


# ---------------------------------------------------------------------------
# capture / verify
# ---------------------------------------------------------------------------


def capture_snapshot(
    freeze_dir: str | Path = ACTIVE_FREEZE_DIR,
    *,
    ds_dir: str | Path = DS_V2,
    base_manifest_dir: str | Path = GATE_VALIDATION,
    stress_manifest: str | Path = EFFECTIVE_STRESS_MANIFEST,
    work_dir: str | Path = "outputs/regression",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """取一份 deterministic 行為快照。**不呼叫任何 provider。**"""
    from pcmef.agents.pcmef_agents import build_case_evidence
    from pcmef.experiments.e2_formal import load_frozen_decision_stack, prepare_cases
    from pcmef.perception.gate import load_frozen_models

    say = progress or (lambda _m: None)

    say(f"loading the frozen decision stack from {freeze_dir}")
    stack = load_frozen_decision_stack(freeze_dir)

    say("preparing the already-seen effective-93 stress set (not rebuilt)")
    prepared = prepare_cases(
        base_manifest_dir, stack, Path(work_dir),
        ds_dir=ds_dir, stress_manifest=stress_manifest,
        code_version="regression-snapshot", progress=say,
    )
    rows = prepared["rows"]
    p_vision, p_tof = prepared["p_vision"], prepared["p_tof"]
    q, signals, routes = prepared["q"], prepared["signals"], prepared["routes"]

    if any(int(str(r["family_index"])) >= 36 for r in rows):
        raise SnapshotError(
            "the snapshot pool contains a family index >= 36; families 36-43 are "
            "sealed for the Final E2 and must not be read here"
        )

    _, _, preprocessing = load_frozen_models(ds_dir)

    # 取第一筆 escalated 當代表：那是唯一真的會送出去的 payload 形狀。
    escalated = [i for i, r in enumerate(routes) if str(r) == "escalated"]
    if not escalated:
        raise SnapshotError(
            "no escalated row in the validation pool; the role-projection and "
            "firewall fingerprints would then be taken on a payload that is never built"
        )
    def evidence_for(row_index: int) -> dict[str, Any]:
        row = rows[row_index]
        return build_case_evidence(
            rgb=np.load(row["rgb_path"]),
            tof=np.load(row["tof_path"]),
            p_vision=p_vision[row_index].tolist(),
            p_tof=p_tof[row_index].tolist(),
            q_vision=float(q["q_vision"][row_index]),
            q_tof=float(q["q_tof"][row_index]),
            duq={k: float(v[row_index]) for k, v in signals.items()},
        )

    index = escalated[0]
    tof = np.load(rows[index]["tof_path"])
    evidence = evidence_for(index)

    say("recording what each of the four roles actually receives")
    calls = _record_role_payloads(evidence)

    # 第二筆：不同 generation condition 的 escalated case，用來檢驗
    # condition 沒有以任何文字形式滲進 payload。
    condition_a = str(rows[index]["condition"])
    contrast = next(
        (i for i in escalated if str(rows[i]["condition"]) != condition_a), None
    )
    if contrast is None:
        raise SnapshotError(
            "every escalated row shares one generation condition, so cross-condition "
            "invariance cannot be checked on this pool"
        )
    say(f"contrasting payloads: {condition_a} vs {rows[contrast]['condition']}")
    invariance = _cross_condition_invariance(
        calls, _record_role_payloads(evidence_for(contrast)),
        condition_a, str(rows[contrast]["condition"]),
    )

    say("fingerprinting TGR-01..08 / 10 / 12")
    items = {
        "TGR-01_class_order": _tgr_01_class_order(),
        "TGR-02_scenario_identity": _tgr_02_scenario_identity(rows),
        "TGR-03_observation_schema": _tgr_03_observation_schema(preprocessing),
        "TGR-04_tof_channel_semantics": _tgr_04_tof_channel_semantics(tof),
        "TGR-05_perception_prediction": _tgr_05_perception_prediction(p_vision, p_tof),
        "TGR-06_reliability_values": _tgr_06_reliability_values(q, signals),
        "TGR-07_route_decision": _tgr_07_route_decision(routes),
        "TGR-08_role_evidence_projection": _tgr_08_role_evidence_projection(calls),
        "TGR-10_statistics": _tgr_10_statistics(stack),
        "TGR-12_truth_firewall": {
            **_tgr_12_truth_firewall(calls),
            "cross_condition_invariance": invariance,
        },
    }

    return {
        "report_id": "thesis_provisional_regression_snapshot",
        "canonical": False,
        "canonical_name_is_reserved": (
            "This is NOT the Thesis Profile Golden Regression baseline of SAI "
            "v0.6.0 section 6. Calling it Golden would assert that the canonical "
            "scientific identity is settled, and it is not."
        ),
        "canonical_blockers": list(CANONICAL_BLOCKERS),
        "covered_tgr": dict(COVERED_TGR),
        "uncovered_tgr": dict(UNCOVERED_TGR),
        "purpose": (
            "Behavioural baseline taken before the P0-1..P0-6 refactor so that "
            "'only moved code, did not change behaviour' is checkable rather than "
            "asserted. No accuracy is computed and no research parameter is set."
        ),
        "scientific_result": False,
        "FINAL_E2_36_43_TOUCHED": "NO",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "freeze_dir": Path(freeze_dir).as_posix(),
            "ds_dir": Path(ds_dir).as_posix(),
            "base_manifest_dir": Path(base_manifest_dir).as_posix(),
            "stress_manifest": Path(stress_manifest).as_posix(),
            "stress_set_rebuilt": False,
        },
        "frozen_lock_hashes": stack["lock_hashes"],
        "representative_case": {
            "route": "escalated",
            "row_index": index,
            "note": (
                "the first escalated row: the only route whose payload is actually "
                "sent to a provider, so it is the right shape to fingerprint"
            ),
        },
        "items": items,
    }


def verify_snapshot(
    snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
    freeze_dir: str | Path = ACTIVE_FREEZE_DIR,
    *,
    progress: Callable[[str], None] | None = None,
    **capture_kwargs: Any,
) -> dict[str, Any]:
    """重新取一份快照並與存檔比對。回報**每一個**不一致的欄位。"""
    path = Path(snapshot_path)
    if not path.exists():
        raise SnapshotError(
            f"no snapshot at {path}. Run `pcmef regression capture` first; "
            "there is nothing to compare against."
        )
    stored = json.loads(path.read_text(encoding="utf-8"))
    current = capture_snapshot(freeze_dir, progress=progress, **capture_kwargs)

    differences: list[dict[str, Any]] = []

    def compare(left: Any, right: Any, path_str: str) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                if key not in left:
                    differences.append(
                        {"field": f"{path_str}.{key}", "stored": None, "current": "<added>"}
                    )
                elif key not in right:
                    differences.append(
                        {"field": f"{path_str}.{key}", "stored": "<removed>", "current": None}
                    )
                else:
                    compare(left[key], right[key], f"{path_str}.{key}")
        elif left != right:
            differences.append(
                {
                    "field": path_str,
                    "stored": str(left)[:200],
                    "current": str(right)[:200],
                }
            )

    compare(stored.get("items", {}), current["items"], "items")

    return {
        "report_id": "thesis_provisional_regression_verify",
        "canonical": False,
        "scientific_result": False,
        "FINAL_E2_36_43_TOUCHED": "NO",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_path": path.as_posix(),
        "snapshot_created_at": stored.get("created_at"),
        "identical": not differences,
        "n_differences": len(differences),
        "differences": differences,
        "covered_tgr": dict(COVERED_TGR),
        "uncovered_tgr": dict(UNCOVERED_TGR),
    }


def write_snapshot(document: Mapping[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out
