# PC-MEF Research System source maintenance contract
# 上下游: 讀更正 run 的 lock（freeze/runs/PFC-001）、outputs/perception/ds_v2 的
#         frozen 權重，以及 gate-validation 的 effective 93 / 已看過的 pilot；
#         呼叫 experiments.e2_formal 與 agents.pcmef_agents；
#         寫出 outputs/corrective/executor_validation.json。
#         **只用已看過的資料；families 36-43 完全不觸碰。**
# 檔案路徑: pcmef/experiments/e2_executor_validation.py
# 產生時間: 2026-09-01 17:20 +08:00
# 版本: v0.1.0
# 功能說明: Final E2 之前對 Full PC-MEF executor 的執行驗證 —— 只驗「路徑是否
#           照已凍結的架構跑」，不看也不比較任何準確率。
# 模組定位: formal_config 重凍結的前置閘（AMD-006 reason B）。它「不是」實驗：
#           report 內不含任何可作為研究結論的量。
# 主要責任:
#   1. check_bridge_identities() escalated -> F == s_A、non-escalated -> F == p_trad
#   2. check_formal_refuses_substitute() formal 模式必須拒絕決定性替身
#   3. check_no_leakage() payload 不得含 GT / condition / severity
#   4. check_zero_calls_when_not_escalated() 非 escalated 一次 provider 都不呼叫
#   5. check_retry_exhaustion_aborts() retry 耗盡即中止且不 drop case
#   6. check_cluster_bootstrap() family cluster 形狀與跨方法共用
#   7. run_executor_validation() 匯總並回報 all_passed
# 維護提醒:
#   - 不得在本檔比較或印出任何 accuracy / macro-F1。看了分數再回頭改參數，
#     就是用結果選方法；本檔存在的意義就是證明「沒有那樣做」。
#   - 不得用 families 36-43 做驗證。只准用 effective 93 與已看過的 pilot。
#   - 不得為了讓某條 check 通過而放寬 executor 的 fail-closed 行為。
#   - v0.1.0 新增：首版 executor 驗證，決策見 NOTE-051 與 AMD-006。
# 驗證方式:
#   - py -3.10 -m pcmef.cli corrective validate-executor
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

__all__ = ["run_executor_validation"]

DS_V2 = Path("outputs/perception/ds_v2")
GATE_VALIDATION = Path("outputs/perception/gate_validation")
PILOT = Path("outputs/perception/e2_deterministic_gate_pilot")

#: 已套用 NOTE-048 exclusion 的 stress set —— 31 family / 93 base / 372 列。
#: 這就是當初擬合 gate 門檻所用的那一份，驗證必須看同一份。
EFFECTIVE_STRESS_MANIFEST = Path("outputs/perception/gate/stress/stress_manifest.json")


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


# ---------------------------------------------------------------------------
# bridge identity
# ---------------------------------------------------------------------------


def check_bridge_identities() -> list[dict[str, Any]]:
    """escalated -> F == s_A；non-escalated -> F == p_trad。

    以純函式層驗證，不需要 provider：兩條 identity 是 bridge 的定義，
    e(x) 只有 0/1 兩個值，因此四種 route 各驗一次就窮盡了。
    """
    from pcmef.core.numeric import ROUTE_ESCALATED
    from pcmef.perception.pcmef_orchestrator import decide_case

    p_v = np.array([0.7, 0.1, 0.15, 0.05])
    p_t = np.array([0.1, 0.6, 0.2, 0.1])
    s_a = np.array([0.05, 0.05, 0.8, 0.1])
    w = 0.5
    results = []

    decision = decide_case(
        ROUTE_ESCALATED, p_v, p_t, w,
        agent_runner=None, formal=False, fallback_arbiter=lambda: s_a,
    )
    results.append(
        _check(
            "escalated_final_equals_s_a",
            bool(np.allclose(decision.final, s_a)) and decision.escalated == 1,
            f"F={decision.final.round(6).tolist()} s_A={s_a.tolist()}",
        )
    )

    expected = {
        "trust_vision": p_v,
        "trust_tof": p_t,
        "fusion": w * p_v + (1.0 - w) * p_t,
    }
    for route, target in expected.items():
        decision = decide_case(route, p_v, p_t, w, agent_runner=None, formal=True)
        results.append(
            _check(
                f"non_escalated_{route}_final_equals_p_trad",
                bool(np.allclose(decision.final, target))
                and decision.escalated == 0
                and decision.llm_called is False,
                f"F={decision.final.round(6).tolist()} p_trad={target.round(6).tolist()}",
            )
        )
    return results


def check_formal_refuses_substitute() -> dict[str, Any]:
    """formal 模式下，escalated case 沒有真 agent runner 必須中止。"""
    from pcmef.core.numeric import ROUTE_ESCALATED
    from pcmef.perception.pcmef_orchestrator import FormalArbiterRequired, decide_case

    p_v = np.array([0.4, 0.3, 0.2, 0.1])
    p_t = np.array([0.1, 0.2, 0.3, 0.4])
    try:
        decide_case(
            ROUTE_ESCALATED, p_v, p_t, 0.5, agent_runner=None, formal=True,
            fallback_arbiter=lambda: np.array([0.25, 0.25, 0.25, 0.25]),
        )
    except FormalArbiterRequired as error:
        return _check(
            "formal_refuses_deterministic_substitute", True, str(error)[:160]
        )
    return _check(
        "formal_refuses_deterministic_substitute", False,
        "a deterministic substitute was accepted in formal mode",
    )


def check_retry_exhaustion_aborts() -> dict[str, Any]:
    """retry 耗盡必須拋 RetryExhaustedError，而不是回傳一個「空的」結果。"""
    from pcmef.agents.pcmef_agents import (
        AGENTS, AgentRunner, RetryExhaustedError,
    )

    class AlwaysBadJSON:
        def invoke(self, _connection, _model, _task, _payload, _cfg):
            from pcmef.agents.provider import ProviderResponse

            return ProviderResponse(
                text="not json at all", model_id="stub", provider="stub",
                request_id="r",
            )

    runner = AgentRunner(
        adapter=AlwaysBadJSON(), connection=object(), model=object(),
        schema_dir=Path("schemas"), max_attempts=2,
    )
    spec = AGENTS["physics_agent"]
    try:
        runner.run(spec, {"observation_brief": {}}, images=[])
    except RetryExhaustedError as error:
        attempts = len(runner.attempts)
        return _check(
            "retry_exhaustion_aborts_without_dropping_the_case",
            attempts == 2 and "ABORT_FORMAL_RUN" in str(error),
            f"attempts={attempts}; message names ABORT_FORMAL_RUN and forbids dropping",
        )
    except Exception as error:  # noqa: BLE001 - 任何其他例外都算沒通過
        return _check(
            "retry_exhaustion_aborts_without_dropping_the_case", False,
            f"raised {type(error).__name__} instead of RetryExhaustedError: {error}",
        )
    return _check(
        "retry_exhaustion_aborts_without_dropping_the_case", False,
        "the runner returned a result after exhausting its attempts",
    )


# ---------------------------------------------------------------------------
# leakage
# ---------------------------------------------------------------------------


def check_no_leakage(sample_evidence: dict[str, Any]) -> dict[str, Any]:
    """送往 provider 的 payload 不得含 GT / condition / severity / 路徑。"""
    from pcmef.core.inference_payload import (
        InferenceFirewallViolation, assert_no_forbidden_tokens,
    )
    from pcmef.agents.pcmef_agents import EVIDENCE_IMAGES_KEY

    body = {k: v for k, v in sample_evidence.items() if k != EVIDENCE_IMAGES_KEY}
    forbidden_keys = {
        "class_label", "class_index", "condition", "severity", "vision_severity",
        "tof_severity", "physical_scene_family", "family_index", "scenario_id",
        "stress_id", "rgb_path", "tof_path", "base_scenario_id", "label", "truth",
    }
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key) in forbidden_keys:
                    found.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(body, "evidence")
    try:
        # gate_route 是合法欄位，但它的值不得夾帶 condition 語意。
        assert_no_forbidden_tokens(
            {"gate_route": body.get("gate_route", "")}, "evidence"
        )
        token_clean = True
        token_detail = "no forbidden semantic token in the route value"
    except InferenceFirewallViolation as error:
        token_clean = False
        token_detail = str(error)[:160]

    return _check(
        "no_ground_truth_or_condition_in_the_payload",
        not found and token_clean,
        f"forbidden keys={found}; {token_detail}",
    )


# ---------------------------------------------------------------------------
# cluster bootstrap
# ---------------------------------------------------------------------------


def check_cluster_bootstrap(rows: list[dict[str, Any]], labels: np.ndarray) -> list[dict[str, Any]]:
    """family cluster 形狀正確，且所有方法共用同一組 cluster。"""
    from pcmef.stats.bootstrap import cluster_bootstrap_delta

    clusters = [r["physical_scene_family"] for r in rows]
    strata = [r["class_label"] for r in rows]
    accuracy = lambda t, p: float((t == p).mean())  # noqa: E731

    noisy = labels.copy()
    noisy[::7] = (noisy[::7] + 1) % 4
    out = cluster_bootstrap_delta(
        labels, {"a": noisy, "b": labels}, clusters, strata, accuracy, "b",
        replicates=200, seed=20260827,
    )
    conditions = len({r["condition"] for r in rows})
    realizations = len({r["base_scenario_id"] for r in rows}) // max(
        len(set(clusters)), 1
    )
    expected_rows = realizations * conditions
    shape_ok = out["rows_per_cluster"] == [expected_rows]

    shared = cluster_bootstrap_delta(
        labels, {"a": noisy, "b": noisy.copy()}, clusters, strata, accuracy, "b",
        replicates=200, seed=20260827,
    )["comparisons"]["a_vs_b"]
    shared_ok = (
        shared["delta"] == 0.0
        and shared["ci_lower"] == 0.0
        and shared["ci_upper"] == 0.0
    )
    return [
        _check(
            "cluster_carries_all_realizations_and_conditions",
            shape_ok,
            f"n_clusters={out['n_clusters']} rows_per_cluster={out['rows_per_cluster']} "
            f"expected=[{expected_rows}] ({realizations} realizations x {conditions} conditions)",
        ),
        _check(
            "all_methods_share_identical_bootstrap_clusters",
            shared_ok,
            f"identical methods give delta/CI {shared['delta']}/"
            f"[{shared['ci_lower']}, {shared['ci_upper']}] (must be exactly zero)",
        ),
    ]


# ---------------------------------------------------------------------------
# 主驗證
# ---------------------------------------------------------------------------


def run_executor_validation(
    *,
    freeze_dir: str | Path,
    out_dir: str | Path = "outputs/corrective",
    registry_dir: str | Path = "registry",
    max_real_cases: int = 2,
    base_manifest_dir: str | Path = GATE_VALIDATION,
    stress_manifest: str | Path = EFFECTIVE_STRESS_MANIFEST,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Full PC-MEF executor 的執行驗證。**不看任何準確率。**

    `freeze_dir` 必填。原本的預設 `"freeze/runs/PFC-001"` 指得雖然對，
    但它是硬編碼的：AMD-007 之後若產生 PFC-002，這個預設會安靜地過期，
    而驗證報告仍會宣稱自己驗的是「當前 lineage」。lineage 由呼叫端
    透過 `resolve_active_lineage()` 決定（NOTE-054）。
    """
    from pcmef.agents.pcmef_agents import AgentRunner, build_case_evidence
    from pcmef.experiments.e2_formal import (
        CountingAdapter, load_frozen_decision_stack, prepare_cases,
    )
    from pcmef.perception.pcmef_orchestrator import decide_case

    say = progress or (lambda _m: None)
    checks: list[dict[str, Any]] = []

    say("bridge identities")
    checks += check_bridge_identities()
    checks.append(check_formal_refuses_substitute())
    say("retry exhaustion")
    checks.append(check_retry_exhaustion_aborts())

    say("loading the frozen decision stack from the corrective run")
    stack = load_frozen_decision_stack(freeze_dir)
    checks.append(
        _check(
            "decision_stack_comes_from_locks",
            stack["decision_bridge_version"] == "selective_escalation_bridge_v1"
            and stack["routing_policy_version"] == "reliability_routing_v1",
            f"bridge={stack['decision_bridge_version']} "
            f"routing={stack['routing_policy_version']} "
            f"reliability_lock={stack['lock_hashes']['reliability_final'][:16]}",
        )
    )

    say("preparing cases on the already-seen effective-93 stress set")
    # 重用既有的 effective-93 stress set，不重建。兩個理由：那一份就是當初
    # 擬合門檻所用的資料（重建會換掉 stress seed），而且它已經套過 NOTE-048
    # 的 exclusion —— 直接拿 gate_validation manifest 會把 Empty f27 帶回來。
    prepared = prepare_cases(
        base_manifest_dir, stack, Path(out_dir) / "executor_validation",
        ds_dir=DS_V2, stress_manifest=stress_manifest,
        code_version="executor-validation", progress=say,
    )
    rows, labels = prepared["rows"], prepared["labels"]
    families = {(r["class_label"], r["family_index"]) for r in rows}
    checks.append(
        _check(
            "validation_pool_is_the_effective_93",
            len(families) == 31 and len(rows) == 372
            and ("Empty", 27) not in families,
            f"{len(rows)} rows / {len(families)} families; "
            f"Empty f27 present = {('Empty', 27) in families}",
        )
    )
    routes = prepared["routes"]
    escalated_index = [i for i, r in enumerate(routes) if str(r) == "escalated"]
    other_index = [i for i, r in enumerate(routes) if str(r) != "escalated"]
    say(f"  {len(escalated_index)} escalated / {len(rows)} rows")

    checks += check_cluster_bootstrap(rows, labels)

    # 證據樣本供洩漏檢查。用 escalated 的那一筆 —— 那是唯一會真的送出去的。
    probe = escalated_index[0] if escalated_index else 0
    evidence = build_case_evidence(
        rgb=np.load(rows[probe]["rgb_path"]),
        tof=np.load(rows[probe]["tof_path"]),
        p_vision=prepared["p_vision"][probe].tolist(),
        p_tof=prepared["p_tof"][probe].tolist(),
        q_vision=float(prepared["q"]["q_vision"][probe]),
        q_tof=float(prepared["q"]["q_tof"][probe]),
        duq={k: float(v[probe]) for k, v in prepared["signals"].items()},
    )
    checks.append(check_no_leakage(evidence))

    # -- 真實 provider 路徑 --------------------------------------------------
    runtime_identity: dict[str, Any] = {}
    real_calls = 0
    measured_usage: dict[str, Any] = {}
    try:
        from pcmef.experiments.llm_real_validation import _binding

        profile, descriptor, adapter, runtime_identity = _binding(Path(registry_dir))
        counting = CountingAdapter(adapter)
        runner = AgentRunner(
            adapter=counting, connection=profile, model=descriptor,
            schema_dir=Path("schemas"),
        )

        say(f"non-escalated cases must make zero provider calls")
        before = counting.calls
        for index in other_index[:8]:
            decision = decide_case(
                str(routes[index]), prepared["p_vision"][index],
                prepared["p_tof"][index], stack["rule"].fusion_weight,
                evidence=None, agent_runner=runner, formal=True,
            )
            if decision.llm_called:
                raise RuntimeError(f"row {index} called the LLM while not escalated")
        checks.append(
            _check(
                "non_escalated_cases_make_zero_provider_calls",
                counting.calls == before,
                f"{len(other_index[:8])} non-escalated case(s), "
                f"provider calls {before} -> {counting.calls}",
            )
        )

        say(f"real LLM path on up to {max_real_cases} escalated case(s)")
        finals = []
        for index in escalated_index[:max_real_cases]:
            case_evidence = build_case_evidence(
                rgb=np.load(rows[index]["rgb_path"]),
                tof=np.load(rows[index]["tof_path"]),
                p_vision=prepared["p_vision"][index].tolist(),
                p_tof=prepared["p_tof"][index].tolist(),
                q_vision=float(prepared["q"]["q_vision"][index]),
                q_tof=float(prepared["q"]["q_tof"][index]),
                duq={k: float(v[index]) for k, v in prepared["signals"].items()},
            )
            decision = decide_case(
                "escalated", prepared["p_vision"][index], prepared["p_tof"][index],
                stack["rule"].fusion_weight, evidence=case_evidence,
                agent_runner=runner, formal=True,
            )
            finals.append(decision)
            # 每 case 結束就更新，不等整個迴圈跑完 —— 中途失敗時
            # 「已經打了幾通」是判斷失敗性質（配額 vs 程式）的關鍵資訊。
            real_calls = counting.calls
            measured_usage = {
                "by_role": {k: dict(v) for k, v in counting.usage_by_task.items()},
                "totals": counting.usage_totals(),
                "escalated_cases_measured": len(finals),
            }
        checks.append(
            _check(
                "real_llm_path_executes_all_four_agents",
                bool(finals)
                and all(d.llm_called and d.s_a is not None for d in finals)
                and real_calls == 4 * len(finals),
                f"{len(finals)} escalated case(s), {real_calls} provider call(s), "
                f"by role {counting.calls_by_task}",
            )
        )
        checks.append(
            _check(
                "escalated_final_equals_s_a_on_the_real_path",
                bool(finals) and all(
                    np.allclose(d.final, d.s_a) for d in finals
                ),
                "; ".join(
                    f"F={d.final.round(4).tolist()} s_A={d.s_a.round(4).tolist()}"
                    for d in finals
                ),
            )
        )
        checks.append(
            _check(
                "all_outputs_finite_and_sum_to_one",
                bool(finals) and all(
                    np.all(np.isfinite(d.final)) and abs(d.final.sum() - 1.0) < 1e-9
                    for d in finals
                ),
                "; ".join(f"sum={float(d.final.sum()):.12f}" for d in finals),
            )
        )
    except Exception as error:  # noqa: BLE001
        # 供應商配額與程式缺陷必須分得出來：前者重跑就好，後者不能重跑。
        text = str(error)
        quota = "429" in text or "quota" in text.lower() or "RESOURCE_EXHAUSTED" in text
        checks.append(
            _check(
                "real_llm_path_executes_all_four_agents", False,
                f"{'PROVIDER_QUOTA' if quota else 'DEFECT'} "
                f"{type(error).__name__} after {real_calls} call(s): {text[:200]}",
            )
        )

    failed = [c["check"] for c in checks if not c["passed"]]
    document = {
        "report_id": "e2_executor_validation",
        "scientific_result": False,
        "purpose": (
            "execution validation only. No accuracy is measured, compared or "
            "reported here, and no research parameter was changed after running it."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "freeze_dir": str(freeze_dir),
        "validation_pool": str(base_manifest_dir),
        "families_touched": "gate-validation effective pool only; 36-43 untouched",
        "n_rows": len(rows),
        "n_escalated_rows": len(escalated_index),
        "real_provider_calls": real_calls,
        # 實測 token 用量。Final E2 的成本只能由這裡外推 —— prompt 大小可以
        # 從 payload 推得，thinking token 不行，只能量。
        "measured_usage": measured_usage,
        "runtime_identity": runtime_identity,
        "checks": checks,
        "failed": failed,
        "all_passed": not failed,
        "FINAL_E2_36_43_TOUCHED": "NO",
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "executor_validation.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document
