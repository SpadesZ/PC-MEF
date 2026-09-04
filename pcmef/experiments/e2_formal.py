# PC-MEF Research System source maintenance contract
# 上下游: 讀 freeze/ 的 gate.lock / reliability_final.lock / statistics_config.lock，
#         outputs/perception/ds_v2 的 frozen 權重與 preprocessing，
#         以及一份 clean paired dataset manifest；
#         呼叫 perception.stress、perception.gate、perception.pcmef_orchestrator
#         與 agents.pcmef_agents；寫出 <out>/formal_e2_report.json。
#         **不重訓、不重搜門檻、不改溫度、不改 severity。**
# 檔案路徑: pcmef/experiments/e2_formal.py
# 產生時間: 2026-09-01 15:30 +08:00
# 版本: v0.7.0
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
#   4. execute_full_pcmef_cases() 是**唯一**的執行迴圈，正式執行與驗證共用
#   5. run_formal_e2_full() 組裝 formal identity、呼叫上者、彙總並寫報告
#   6. G1-G5 五條臂；G4 由既有 route 與 fused 導出，零 provider 呼叫
#   7. worst_condition_macro_f1() primary endpoint 的點估計與最弱 condition
#   8. _cluster_statistics() 以 family cluster bootstrap 產出成對比較
#   9. _worst_condition_statistics() primary endpoint 的成對 CI（AMD-009）
#  10. emit_trace() 旁路寫出 decision trace，決策完成後才呼叫（NOTE-064）
# 維護提醒:
#   - 不得在本檔重新擬合任何東西。門檻、溫度、anchors 一律由 lock 還原；
#     refit 一次就等於 Formal E2 自己決定了自己的判準。
#   - 不得在 formal 模式讓 deterministic arbiter 頂替 LLM。那會讓
#     「PC-MEF 的 LLM 臂」在報告上成立而實際從未執行。
#   - 不得 drop 任何 case。retry 耗盡就是 ABORT_FORMAL_RUN；
#     少一個 case 就是分母悄悄變小，那正是 bounded retry 要防的事。
#   - 不得對 non-escalated case 呼叫 provider。selective escalation 的整個
#     論點就是「傳統證據足夠就不叫 LLM」，多叫一次就推翻了它。
#   - 不得讓 G4 reliability_routing 走任何會呼叫 provider 的分支。它的定義
#     是「路由但不仲裁」；一旦它叫了 LLM，G5 vs G4 就不再是仲裁的增量
#     （NOTE-061）。也不得把它從 dry run 拿掉 —— 它不依賴 LLM。
#   - 不得讓 dry run 輸出 pcmef_full 的數字。那一欄會等於 fixed_fusion，
#     讀起來像「PC-MEF 沒有比固定融合好」，而它其實從未執行（NOTE-056）。
#   - 不得只報 worst-condition 的最小值而不報它落在哪個 condition。
#     不同方法的最弱條件可能不同，那個對比本身就是結果（NOTE-062）。
#   - 不得讓 decision trace 的任何回傳值進入決策路徑。它在決策完成之後
#     才被呼叫，例外一律吞掉 —— 寫不出 trace 只損失可讀性，不該讓一次
#     formal run 失敗（NOTE-064）。
#   - 不得改用 scenario-level bootstrap。statistics_config.lock 凍的是
#     physical_scene_family cluster，且所有方法共用同一組 cluster。
#   - 不得在別處另寫一份等價的執行迴圈。execute_full_pcmef_cases() 抽出來
#     就是為了讓驗證跑到與正式執行同一段程式；再複製一份，驗到的又會是
#     「另一份很像的實作」（NOTE-055）。
#   - v0.1.0 新增：首版 Full PC-MEF formal executor，決策見 NOTE-051。
#   - v0.2.0 新增：抽出 execute_full_pcmef_cases()，freeze_dir 改必填。
#   - v0.3.0 新增：llm_mode dry run；dry run 不輸出 pcmef_full 臂（NOTE-056）。
#   - v0.4.0 新增：G4 reliability_routing 臂與其成對統計（NOTE-061）。
#   - v0.5.0 新增：worst-condition macro-F1 點估計與 primary_endpoint（NOTE-062）。
#   - v0.6.0 新增：primary endpoint 的成對 CI（NOTE-063 / AMD-009）。
#   - v0.7.0 新增：decision trace 旁路輸出（NOTE-064）。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_formal_executor.py -v
#   - py -3.10 -m pytest tests/e2/test_execute_full_pcmef_cases.py -v
#   - py -3.10 -m pytest tests/e2/test_dry_run_mode.py -v
#   - py -3.10 -m pytest tests/e2/test_reliability_routing_arm.py -v
#   - py -3.10 -m pytest tests/e2/test_worst_condition_endpoint.py -v
#   - py -3.10 -m pytest tests/e2/test_worst_condition_bootstrap.py -v
#   - py -3.10 -m pytest tests/e2/test_decision_trace.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from pcmef.core.constants import CLASS_ORDER

__all__ = [
    "FormalE2Error",
    "CountingAdapter",
    "load_frozen_decision_stack",
    "prepare_cases",
    "execute_full_pcmef_cases",
    "worst_condition_macro_f1",
    "run_formal_e2_full",
]

DS_V2 = Path("outputs/perception/ds_v2")

#: 正式執行：escalated case 呼叫四個真 agent。
LLM_MODE_EXECUTE = "execute"
#: 零成本預演：資料生成、routing 與統計形狀照跑，但完全不呼叫 provider。
#: **不是**降級的正式執行 —— pcmef_full 整條臂會被排除在報告之外。
LLM_MODE_SKIP = "skip"
LLM_MODES = frozenset({LLM_MODE_EXECUTE, LLM_MODE_SKIP})


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
    #: 逐角色累計的實際 token 用量。thinking token 單獨記 ——
    #: 它照 output 計費卻不在 candidatesTokenCount 裡，是成本估算誤差最大的一項。
    usage_by_task: dict[str, dict[str, int]] = field(default_factory=dict)

    def invoke(self, connection, model, task_code, payload, runtime_cfg):
        self.calls += 1
        key = str(task_code)
        self.calls_by_task[key] = self.calls_by_task.get(key, 0) + 1
        response = self.inner.invoke(connection, model, task_code, payload, runtime_cfg)
        bucket = self.usage_by_task.setdefault(
            key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                  "thoughts_tokens": 0}
        )
        bucket["calls"] += 1
        bucket["prompt_tokens"] += int(getattr(response, "prompt_tokens", 0) or 0)
        bucket["completion_tokens"] += int(getattr(response, "completion_tokens", 0) or 0)
        bucket["thoughts_tokens"] += int(getattr(response, "thoughts_tokens", 0) or 0)
        return response

    def usage_totals(self) -> dict[str, int]:
        total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "thoughts_tokens": 0}
        for bucket in self.usage_by_task.values():
            for key in total:
                total[key] += bucket[key]
        total["billable_output_tokens"] = (
            total["completion_tokens"] + total["thoughts_tokens"]
        )
        return total

    def __getattr__(self, name: str) -> Any:
        # 其餘 adapter 介面原樣轉發；只有 invoke 需要被計數。
        return getattr(self.inner, name)


# ---------------------------------------------------------------------------
# 由 lock 還原決策堆疊
# ---------------------------------------------------------------------------


def load_frozen_decision_stack(freeze_dir: str | Path) -> dict[str, Any]:
    """由 lock 還原 GateRule、ReliabilityModel 與統計設定。**不重新擬合。**

    `freeze_dir` **必填，刻意沒有預設值**。原本的預設是 `"freeze"`，
    而那正是 superseded 的 parent lineage —— 兩份 lineage 的檔名與 schema
    完全相同，讀錯不會拋任何錯誤，只會安靜地用被 AMD-006 更正掉的
    anchors 與門檻跑完整場。呼叫端要嘛明確指定，要嘛先經
    `resolve_active_lineage()` 解析（NOTE-054）。
    """
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
    stress_manifest: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """由 clean base manifest 產生四 condition 的 stress 列並算出決策訊號。

    `stress_manifest` 給定時改為**讀回既有的** stress set 而不是重建。
    驗證階段必須走這條：重建會換掉 stress seed，於是驗證看到的就不是
    當初擬合門檻的那一份資料。Final E2 則相反 —— 那時 stress set 尚未存在，
    必須由本函式產生。
    """
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
    if stress_manifest is not None:
        stress = json.loads(Path(stress_manifest).read_text(encoding="utf-8"))
        say(f"reusing the existing stress set at {stress_manifest}")
    else:
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


def worst_condition_macro_f1(
    per_condition: Mapping[str, Mapping[str, Mapping[str, float]]],
) -> tuple[dict[str, float], dict[str, str]]:
    """E2 的 primary robustness endpoint：每個方法在四個 condition 中的最低
    macro-F1，以及那個 condition 是哪一個。

    它可由 per-condition 表格推導，但算出來報出去有兩個理由：讀者不必自己
    取 min，而且**哪一個 condition 是最弱的本身就是結果的一部分** ——
    不同方法的最弱條件可能不同，那件事在表格裡看得到卻很容易被略過。

    平手時取字典序最小的 condition，讓輸出不依賴 dict 的插入順序。
    """
    worst: dict[str, float] = {}
    where: dict[str, str] = {}
    for name, block in per_condition.items():
        if not block:
            raise FormalE2Error(
                f"method {name!r} has no per-condition breakdown; the worst-condition "
                "endpoint is undefined without one"
            )
        chosen = min(
            sorted(block), key=lambda condition: block[condition]["macro_f1"]
        )
        worst[name] = float(block[chosen]["macro_f1"])
        where[name] = chosen
    return worst, where


def _worst_condition_statistics(
    labels: np.ndarray,
    predictions: Mapping[str, np.ndarray],
    rows: Sequence[Mapping[str, Any]],
    stack: Mapping[str, Any],
    reference: str,
) -> dict[str, Any]:
    """Primary endpoint 的成對 CI。重抽與 `_cluster_statistics` 完全相同。

    分成兩個入口不是重複：worst-condition 必須在**每個 replicate 內**先分
    condition 再取 min，而 `cluster_bootstrap_delta` 的 metric 拿不到列的
    condition。詳見 `cluster_bootstrap_worst_condition_delta` 的說明與 AMD-009。
    """
    from pcmef.stats.bootstrap import cluster_bootstrap_worst_condition_delta

    return cluster_bootstrap_worst_condition_delta(
        labels,
        predictions,
        [r["physical_scene_family"] for r in rows],
        [r["class_label"] for r in rows],
        [r["condition"] for r in rows],
        _macro_f1,
        reference,
        replicates=stack["bootstrap_replicates"],
        seed=stack["bootstrap_seed"],
    )


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


def execute_full_pcmef_cases(
    rows: Sequence[Mapping[str, Any]],
    p_vision: np.ndarray,
    p_tof: np.ndarray,
    q: Mapping[str, np.ndarray],
    signals: Mapping[str, np.ndarray],
    routes: np.ndarray,
    rule: Any,
    *,
    agent_runner: Any = None,
    formal: bool = True,
    llm_mode: str = LLM_MODE_EXECUTE,
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """**唯一**的 Full PC-MEF 執行迴圈。逐 case 決策並回傳 F(x) 與 trace。

    這個函式從 `run_formal_e2_full` 抽出來，目的只有一個：讓
    **正式執行與執行驗證跑的是同一段程式**。先前 validator 自己重建了一份
    等價迴圈，於是它證明的是「另一份很像的實作能跑」，而不是
    「正式入口能跑」—— 兩者之間的差異正好是最不會有症狀的那種。

    切在這裡而不是切整個 `run_formal_e2_full`，是為了讓 validator 能用
    **已開封的 effective-93** 驗證同一條執行路徑，而不必碰 families 36-43。
    final partition 一旦生成就是開封，沒有預演的餘地。

    `formal=True` 時 escalated case 必須有真正的四 agent 仲裁器；
    `decide_case` 會拒絕決定性替身。retry 耗盡一律中止整場 run，
    **不得 drop case** —— drop 會讓分母隨方法而變。

    `llm_mode="skip"` 是零成本的預演：資料生成、routing 與統計形狀全部照跑，
    但 escalated case **不呼叫 provider**。這一列的 F(x) 以傳統決策填位，
    純粹為了讓後續的 sum-to-1 與 cluster 形狀檢查仍有意義；
    **呼叫端必須據 `skipped_escalated_cases > 0` 把 pcmef_full 整條臂排除**，
    否則那一欄會等於 fixed_fusion，看起來像「PC-MEF 沒有比固定融合好」——
    一個從未執行過的方法不該有數字（見 `run_formal_e2_full` 的處理）。
    """
    from pcmef.agents.pcmef_agents import RetryExhaustedError, build_case_evidence
    from pcmef.perception.pcmef_orchestrator import decide_case

    if llm_mode not in LLM_MODES:
        raise FormalE2Error(
            f"unknown llm_mode {llm_mode!r}; expected one of {sorted(LLM_MODES)}"
        )
    if llm_mode == LLM_MODE_SKIP and formal:
        raise FormalE2Error(
            "llm_mode='skip' cannot be combined with formal=True: skipping the "
            "arbiter is exactly the deterministic substitute that formal mode "
            "exists to refuse. A dry run is not a formal run."
        )

    say = progress or (lambda _m: None)

    counter: CountingAdapter | None = None
    if agent_runner is not None and isinstance(agent_runner.adapter, CountingAdapter):
        counter = agent_runner.adapter

    escalated_cases = 0
    llm_called_cases = 0
    skipped_escalated_cases = 0
    calls_before_first_escalation: int | None = None
    finals = np.empty_like(p_vision)
    traces: list[dict[str, Any]] = []

    say(f"deciding {len(rows)} cases" + (" (dry run: no provider calls)"
                                         if llm_mode == LLM_MODE_SKIP else ""))
    for index, row in enumerate(rows):
        route = str(routes[index])
        escalated = route == "escalated"
        skipping = escalated and llm_mode == LLM_MODE_SKIP
        evidence = None

        if escalated:
            escalated_cases += 1
            if calls_before_first_escalation is None and counter is not None:
                calls_before_first_escalation = counter.calls
        # 這個 case 開始前 call_log 的長度，供旁路 trace 取出「本 case 新增的」。
        calls_before = (
            len(agent_runner.call_log)
            if agent_runner is not None and hasattr(agent_runner, "call_log")
            else 0
        )

        if escalated and not skipping:
            # 證據只在真的要送出去時才組裝：non-escalated 與 dry run 連 payload
            # 都不建，因此不可能不小心送出去。
            evidence = build_case_evidence(
                rgb=np.load(row["rgb_path"]),
                tof=np.load(row["tof_path"]),
                p_vision=p_vision[index].tolist(),
                p_tof=p_tof[index].tolist(),
                q_vision=float(q["q_vision"][index]),
                q_tof=float(q["q_tof"][index]),
                duq={k: float(v[index]) for k, v in signals.items()},
            )

        try:
            decision = decide_case(
                # dry run 把這一列當成 fusion 走，只是為了取得一個合法分布填位。
                "fusion" if skipping else route,
                p_vision[index], p_tof[index], rule.fusion_weight,
                evidence=evidence,
                agent_runner=None if skipping else agent_runner,
                formal=formal,
            )
        except RetryExhaustedError as error:
            # 明確不 drop：整場 run 中止。
            raise FormalE2Error(
                f"ABORT_FORMAL_RUN at row {index} ({row['stress_id']}): {error}"
            ) from None

        finals[index] = decision.final
        if decision.llm_called:
            llm_called_cases += 1

        # --- 旁路觀測。決策**已經完成**，以下任何失敗都不得回頭影響它。---
        if trace_sink is not None:
            try:
                trace_sink(
                    {
                        "row_index": index,
                        "row": row,
                        "p_vision": p_vision[index],
                        "p_tof": p_tof[index],
                        "signals": {k: float(v[index]) for k, v in signals.items()},
                        "q_vision": float(q["q_vision"][index]),
                        "q_tof": float(q["q_tof"][index]),
                        "route": route,
                        "rule": rule,
                        "decision": decision,
                        # 只取這個 case 新增的幾筆 —— call_log 是 runner 級的，
                        # 跨 case 累積；用長度差而不是清空它，避免動到 runner 狀態。
                        "agent_calls": (
                            list(agent_runner.call_log[calls_before:])
                            if agent_runner is not None
                            and hasattr(agent_runner, "call_log")
                            else []
                        ),
                        "dry_run": llm_mode == LLM_MODE_SKIP,
                    }
                )
            except Exception as error:  # noqa: BLE001
                # trace 是觀測，不是決策。寫不出來只損失可讀性，
                # 不該讓一次 formal run 失敗（NOTE-064）。
                say(f"  warning: trace for row {index} failed: {type(error).__name__}")

        trace = {"stress_id": row["stress_id"], **decision.to_trace()}
        if skipping:
            skipped_escalated_cases += 1
            # trace 保留這一列**真正的** route，否則 dry run 的紀錄會看起來
            # 像是路由本身變了，而路由完全沒有改變。
            trace["route"] = route
            trace["e"] = 1
            trace["llm_skipped"] = True
            trace["final_is_placeholder"] = True
        traces.append(trace)
        if (index + 1) % 48 == 0:
            say(f"  {index + 1}/{len(rows)} cases")

    if not np.all(np.isfinite(finals)):
        raise FormalE2Error("F(x) contains non-finite entries")
    sums = finals.sum(axis=1)
    if not np.allclose(sums, 1.0, atol=1e-9):
        raise FormalE2Error(
            f"F(x) rows do not sum to 1 (min {sums.min()}, max {sums.max()})"
        )

    return {
        "finals": finals,
        "traces": traces,
        "escalated_cases": escalated_cases,
        "llm_called_cases": llm_called_cases,
        "skipped_escalated_cases": skipped_escalated_cases,
        "llm_mode": llm_mode,
        "calls_before_first_escalation": calls_before_first_escalation,
        "counter": counter,
    }


def run_formal_e2_full(
    base_manifest_dir: str | Path,
    out_dir: str | Path,
    *,
    freeze_dir: str | Path,
    ds_dir: str | Path = DS_V2,
    severity: dict[str, float] | None = None,
    agent_runner: Any = None,
    formal: bool = True,
    llm_mode: str = LLM_MODE_EXECUTE,
    code_version: str = "",
    is_final_formal_e2: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Full PC-MEF 的 Formal E2。

    formal=True 時 escalated case **必須**有真正的四 agent 仲裁器；
    沒有就中止，不接受決定性替身。

    `llm_mode="skip"` 產出一份 **dry-run 報告**：三個 baseline 臂的數字完整
    有效（它們本來就不需要 LLM），而 `pcmef_full` 整條臂連同它的成對統計
    一律不輸出。理由是 dry run 的 F(x) 只是填位值；把它當成 PC-MEF 的結果
    報出去，就會得到「PC-MEF 恰好等於 fixed fusion」這個看似真實、
    實際上只反映「從未執行」的結論。

    `freeze_dir` 為 required keyword-only：見 `load_frozen_decision_stack`
    的說明，這裡不得回復預設值。
    """
    say = progress or (lambda _m: None)
    dry_run = llm_mode == LLM_MODE_SKIP
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

    # --- decision trace（旁路觀測，NOTE-064）-------------------------------
    from pcmef.experiments.decision_trace import TraceWriter, build_case_trace

    fused_all = rule.fusion_weight * p_vision + (1.0 - rule.fusion_weight) * p_tof
    writer = TraceWriter(
        out, run_id=Path(out_dir).name, code_revision=code_version,
        runtime_identity=stack["lock_hashes"], dry_run=dry_run,
    )

    def emit_trace(payload: dict[str, Any]) -> None:
        """把一個 case 的決策落盤。由 executor 在決策完成後呼叫。

        這個函式的例外由呼叫端吞掉 —— 它是觀測，不是決策路徑的一環。
        """
        index = payload["row_index"]
        row = payload["row"]
        case_id = f"case_{index:04d}"

        # preview 由**這次 run 記憶體裡**的 RGB 產生，不讓 Web 之後回頭讀
        # dataset：那會多開一條對正式資料集的存取路徑。
        rgb_array = np.load(row["rgb_path"])
        preview_path, shape = writer.write_preview(case_id, rgb_array)
        from pcmef.agents.pcmef_agents import rgb_to_png_bytes
        from pcmef.core.hash import hash_object
        import base64 as _b64

        digest = hash_object(
            _b64.b64encode(rgb_to_png_bytes(rgb_array)).decode("ascii")
        )
        tof_shape = list(np.load(row["tof_path"]).shape)

        writer.write_case(
            build_case_trace(
                row=row, row_index=index, case_id=case_id,
                p_vision=payload["p_vision"], p_tof=payload["p_tof"],
                signals=payload["signals"],
                q_vision=payload["q_vision"], q_tof=payload["q_tof"],
                route=payload["route"], rule=payload["rule"],
                decision=payload["decision"], agent_calls=payload["agent_calls"],
                class_order=CLASS_ORDER,
                arms={
                    "vision_only": p_vision[index],
                    "tof_only": p_tof[index],
                    "fixed_fusion": fused_all[index],
                },
                rgb_preview={
                    "sha256": digest,
                    "original_shape": shape,
                    "preview_path": preview_path,
                },
                tof_shape=tof_shape,
                dry_run=payload["dry_run"],
            )
        )

    try:
        executed = execute_full_pcmef_cases(
            rows, p_vision, p_tof, q, signals, routes, rule,
            agent_runner=agent_runner, formal=formal, llm_mode=llm_mode,
            trace_sink=emit_trace, progress=say,
        )
    except BaseException as error:
        # run 中止時仍要留下已寫的 case，但 index 必須明說它不完整 ——
        # 一份看起來完整的 index 比沒有 index 更糟。
        writer.abort(f"{type(error).__name__}: {error}")
        raise
    finals = executed["finals"]
    traces = executed["traces"]
    escalated_cases = executed["escalated_cases"]
    llm_called_cases = executed["llm_called_cases"]
    skipped_escalated = executed["skipped_escalated_cases"]
    calls_before_first_escalation = executed["calls_before_first_escalation"]
    counter = executed["counter"]

    fused = rule.fusion_weight * p_vision + (1.0 - rule.fusion_weight) * p_tof
    escalated_mask = np.asarray([str(r) == "escalated" for r in routes])

    # G4：可靠度路由，但**不呼叫 LLM**。三條傳統路徑照常輸出，
    # escalated 改用 fixed fusion。這條臂把「路由本身」與「Multi-Agent
    # 仲裁」的效果乾淨隔開：G4 vs G3 量的是路由，G5 vs G4 量的是仲裁。
    #
    # non-escalated 的列直接沿用 finals —— 那時 F(x) 依定義等於 p_trad
    # （見 decide_case 的 non-escalated 分支），所以這裡不是近似，是等式。
    # 整條 G4 的 provider 呼叫數為 0，因為它一個 case 都沒有新跑。
    reliability_routing = np.where(escalated_mask[:, None], fused, finals)

    arms = {
        "vision_only": p_vision,
        "tof_only": p_tof,
        "fixed_fusion": fused,
        "reliability_routing": reliability_routing,
    }
    if dry_run:
        # pcmef_full 刻意缺席。dry run 的 F(x) 是填位值，報出去會變成
        # 「PC-MEF 恰好等於 fixed fusion」—— 一個從未執行的方法不該有數字。
        #
        # G4 則相反，在 dry run 下**完全有效**：它的定義就是 escalated 用
        # fused，而那正是 dry run 對 escalated 列填的值。
        say(
            f"dry run: {skipped_escalated} escalated case(s) skipped; "
            "the pcmef_full arm is omitted from the report "
            "(reliability_routing remains valid: it never calls an LLM)"
        )
    else:
        arms["pcmef_full"] = finals
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

    worst_condition, worst_condition_at = worst_condition_macro_f1(per_condition)

    say("cluster bootstrap")
    # G5 對 G1-G4 都要有成對比較（實驗計畫 v1.2 §5）。少了 G4，
    # 「LLM 仲裁的增量」就沒有對照 —— G5 vs G3 混著路由與仲裁兩件事。
    baselines = ("vision_only", "tof_only", "fixed_fusion", "reliability_routing")
    if dry_run:
        # 成對統計全部是 pcmef_full vs baseline；沒有 pcmef_full 就沒有比較。
        # cluster 形狀仍然算 —— 驗證 12 列 / cluster 與分層是 dry run 的重點之一。
        statistics: dict[str, Any] = {}
        worst_condition_statistics: dict[str, Any] = {}
    else:
        statistics = {
            f"pcmef_full_vs_{baseline}": {
                metric: value["comparisons"][f"pcmef_full_vs_{baseline}"]
                for metric, value in _cluster_statistics(
                    labels, predictions, rows, stack, baseline
                ).items()
            }
            for baseline in baselines
        }
        # Primary endpoint 的 CI。**必須**用專用 estimator：worst-condition
        # 的 CI 不等於任何單一 condition 的 CI，因為每個 replicate 的
        # argmin condition 可能不同（NOTE-063 / AMD-009）。
        say("worst-condition bootstrap (primary endpoint)")
        worst_runs = {
            baseline: _worst_condition_statistics(
                labels, predictions, rows, stack, baseline
            )
            for baseline in baselines
        }
        worst_condition_statistics = {
            "estimator": next(iter(worst_runs.values()))["version"],
            "comparisons": {
                f"pcmef_full_vs_{baseline}":
                    run["comparisons"][f"pcmef_full_vs_{baseline}"]
                for baseline, run in worst_runs.items()
            },
            # 最弱 condition 在 replicate 之間會換人，而那正是不能用單一
            # condition 的 CI 代替的原因。數出來讓那句話有證據。
            "reference_worst_condition_counts": {
                baseline: run["reference_worst_condition_counts"]
                for baseline, run in worst_runs.items()
            },
        }
    cluster_shape = _cluster_statistics(
        labels, predictions, rows, stack, "vision_only"
    )["accuracy"]

    route_names, route_counts = np.unique(routes, return_counts=True)
    document = {
        "report_id": "formal_e2_dry_run" if dry_run else "formal_e2_full_pcmef",
        # dry run 永遠不是科學結果，即使呼叫端把 is_final_formal_e2 設成 True。
        "scientific_result": bool(is_final_formal_e2) and not dry_run,
        "is_final_formal_e2": is_final_formal_e2 and not dry_run,
        "one_shot": not dry_run,
        "llm_mode": llm_mode,
        "dry_run": dry_run,
        "dry_run_meaning": (
            None if not dry_run else
            "Data generation, routing, reliability and cluster shape were exercised "
            "with zero provider calls. The pcmef_full arm and every paired statistic "
            "are absent by construction: a method that never ran must not have a "
            "number. The other four arms -- including reliability_routing -- are "
            "complete and valid, because none of them ever calls an LLM."
        ),
        "arm_definitions": {
            "vision_only": "G1. argmax p_V.",
            "tof_only": "G2. argmax p_T.",
            "fixed_fusion": (
                f"G3. {rule.fusion_weight} p_V + {1.0 - rule.fusion_weight} p_T "
                "on every row, regardless of route."
            ),
            "reliability_routing": (
                "G4. The same q_V / q_T / D and the same route as G5, but the "
                "escalated route falls back to fixed fusion instead of calling the "
                "arbiter. Zero provider calls by construction. G4 vs G3 isolates "
                "the routing; G5 vs G4 isolates the multi-agent arbitration."
            ),
            "pcmef_full": (
                "G5. Reliability routing plus selective multi-agent arbitration; "
                "escalated rows carry s_A."
            ),
        },
        "skipped_escalated_cases": skipped_escalated,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "formal_mode": formal,
        "llm_arm_evaluated": (
            False if dry_run else (llm_called_cases > 0 or escalated_cases == 0)
        ),
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
        # 實驗計畫 v1.2 §5 的 primary robustness metric。
        "worst_condition_macro_f1": worst_condition,
        "worst_condition_at": worst_condition_at,
        "primary_endpoint": {
            "metric": "worst_condition_macro_f1",
            "definition": (
                "min over the four generation conditions of that method's "
                "per-condition macro-F1"
            ),
            "conditions": sorted(set(conditions.tolist())),
            "rationale": (
                "A method that lifts the average while the weakest condition "
                "still collapses has not shown robustness, which is what E2 asks "
                "about. The per-condition table stays in the report because which "
                "condition is weakest is itself a finding."
            ),
            "paired_ci_estimator": (
                "pcmef.stats.bootstrap.cluster_bootstrap_worst_condition_delta -- "
                "same clusters, strata, seed and shared-resample contract as the "
                "overall estimator, but the minimum is taken inside each replicate. "
                "The worst-condition CI is not the CI of any single condition "
                "because the argmin condition varies across replicates (AMD-009)."
            ),
        },
        "worst_condition_statistics": worst_condition_statistics,
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
    # 檔名分開：dry run 不得寫進 formal report 的位置。同名會讓一份預演
    # 在目錄裡與正式結果長得一模一樣，而唯一的差別藏在 JSON 欄位裡。
    filename = "formal_e2_dry_run.json" if dry_run else "formal_e2_report.json"
    (out / filename).write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    document["report_path"] = (out / filename).as_posix()

    # trace 的 index 最後才寫，而且只在 run 真的跑完之後。中途失敗時
    # 上面的 except 已經寫過一份 aborted index。
    document["trace_index_path"] = writer.finalise().as_posix()
    return document
