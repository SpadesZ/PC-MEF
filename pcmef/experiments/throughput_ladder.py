# PC-MEF Research System source maintenance contract
# 上下游: 讀更正 run 的 lock 與已看過的 effective-93 stress set；
#         經 experiments.e2_formal 與 agents.pcmef_agents 呼叫真實 provider；
#         寫出 outputs/corrective/throughput_ladder.json。
#         **只用已看過的資料；families 36-43 完全不觸碰。**
# 檔案路徑: pcmef/experiments/throughput_ladder.py
# 產生時間: 2026-09-01 23:10 +08:00
# 版本: v0.1.0
# 功能說明: 逐級量測「這把 key 在配額用完之前能跑完幾個 escalated case」。
#           這是 operational test，scientific_result 永遠是 false。
# 模組定位: 免費層吞吐的量測工具。它「不是」實驗 —— 產出的是配額與時間，
#           不含任何 accuracy，也不得影響任何研究參數。
# 主要責任:
#   1. LADDER 固定的級距，一級 PASS 才進下一級
#   2. run_level() 每一級從乾淨 transport session 開始
#   3. 記錄 429 出現在第幾個 request、retry 次數、wall time 與 token
#   4. 第一次穩定 429 就停止擴張，不用免費 key 硬跑 500+ calls
# 維護提醒:
#   - 不得為了讓某一級通過而調 prompt、gate、model 或 severity。
#     本模組量的是配額，調那些等於改研究設定去遷就營運限制。
#   - 不得在配額耗盡後自動換 key 續跑。換 key 會讓「這把 key 能跑多少」
#     這個量失去意義；要量多把就分別跑。
#   - 不得把本檔的輸出寫進任何 lock。配額不是研究結論。
#   - v0.1.0 新增：首版吞吐量測，決策見 NOTE-052。
# 驗證方式:
#   - py -3.10 -m pcmef.cli corrective throughput-ladder --max-level 2
# ------------------------------------------------------------

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

__all__ = ["LADDER", "run_ladder"]

#: 固定級距（escalated case 數）。每個 case = 4 個 provider 呼叫。
LADDER: tuple[int, ...] = (2, 4, 8, 16, 32)

DS_V2 = Path("outputs/perception/ds_v2")
GATE_VALIDATION = Path("outputs/perception/gate_validation")
EFFECTIVE_STRESS = Path("outputs/perception/gate/stress/stress_manifest.json")


def _quota_error(error: Exception) -> bool:
    text = str(error)
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower()


def run_level(
    n_cases: int,
    prepared: dict[str, Any],
    escalated_index: list[int],
    stack: dict[str, Any],
    registry_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """跑一級。**每一級都重新建立 transport session 與計數器。**"""
    from pcmef.agents.pcmef_agents import AgentRunner, build_case_evidence
    from pcmef.experiments.e2_formal import CountingAdapter
    from pcmef.experiments.llm_real_validation import _binding
    from pcmef.perception.pcmef_orchestrator import decide_case

    say = progress or (lambda _m: None)
    # 乾淨 session：重新解析 binding 與 adapter，不沿用上一級的連線。
    profile, descriptor, adapter, identity = _binding(registry_dir)
    role_bindings = identity.get("role_bindings") or {}
    counting = CountingAdapter(adapter)
    counted_roles = {
        code: (CountingAdapter(a), c, m) for code, (a, c, m) in role_bindings.items()
    }
    runner = AgentRunner(
        adapter=counting, connection=profile, model=descriptor,
        schema_dir=Path("schemas"), role_bindings=counted_roles,
    )
    runner.assert_single_model_identity()

    def total_calls() -> int:
        if counted_roles:
            return sum(a.calls for a, _c, _m in counted_roles.values())
        return counting.calls

    def merged_usage() -> dict[str, Any]:
        buckets: dict[str, dict[str, int]] = {}
        sources = (
            [a for a, _c, _m in counted_roles.values()] if counted_roles else [counting]
        )
        for source in sources:
            for role, bucket in source.usage_by_task.items():
                target = buckets.setdefault(
                    role, {"calls": 0, "prompt_tokens": 0,
                           "completion_tokens": 0, "thoughts_tokens": 0}
                )
                for key in target:
                    target[key] += bucket[key]
        totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                  "thoughts_tokens": 0}
        for bucket in buckets.values():
            for key in totals:
                totals[key] += bucket[key]
        totals["billable_output_tokens"] = (
            totals["completion_tokens"] + totals["thoughts_tokens"]
        )
        return {"by_role": buckets, "totals": totals}

    rows = prepared["rows"]
    started = time.monotonic()
    completed = 0
    first_429_at: int | None = None
    aborted_reason = ""
    say(f"  level {n_cases}: target {n_cases} case(s) = {n_cases * 4} call(s)")

    for case_number, index in enumerate(escalated_index[:n_cases], start=1):
        evidence = build_case_evidence(
            rgb=np.load(rows[index]["rgb_path"]),
            tof=np.load(rows[index]["tof_path"]),
            p_vision=prepared["p_vision"][index].tolist(),
            p_tof=prepared["p_tof"][index].tolist(),
            q_vision=float(prepared["q"]["q_vision"][index]),
            q_tof=float(prepared["q"]["q_tof"][index]),
            duq={k: float(v[index]) for k, v in prepared["signals"].items()},
        )
        try:
            decide_case(
                "escalated", prepared["p_vision"][index], prepared["p_tof"][index],
                stack["rule"].fusion_weight, evidence=evidence,
                agent_runner=runner, formal=True,
            )
        except Exception as error:  # noqa: BLE001
            if first_429_at is None and _quota_error(error):
                # 已完成的呼叫數 + 1 = 撞到配額的那一個 request 的序號。
                first_429_at = total_calls() + 1
            aborted_reason = f"{type(error).__name__}: {str(error)[:220]}"
            say(f"    aborted on case {case_number}: {aborted_reason[:120]}")
            break
        completed += 1
        if completed % 4 == 0:
            say(f"    {completed}/{n_cases} case(s), {total_calls()} call(s)")

    wall = time.monotonic() - started
    usage = merged_usage()
    retries = sum(1 for a in runner.attempts if not a.ok)
    return {
        "level_cases": n_cases,
        "target_calls": n_cases * 4,
        "successful_cases": completed,
        "total_api_requests": total_calls(),
        "first_429_request_index": first_429_at,
        "retries": retries,
        "attempts_logged": len(runner.attempts),
        "outcome": "SUCCESS" if completed == n_cases else "ABORTED",
        "abort_reason": aborted_reason,
        "wall_time_sec": round(wall, 2),
        "sec_per_case": round(wall / completed, 2) if completed else None,
        "usage": usage,
        "calls_by_connection": (
            {
                str(c.connection_id): a.calls
                for _code, (a, c, _m) in counted_roles.items()
            }
            if counted_roles
            else {str(profile.connection_id): counting.calls}
        ),
        "role_connection_map": identity.get("role_connection_map", {}),
    }


def run_ladder(
    freeze_dir: str | Path = "freeze/runs/PFC-001",
    registry_dir: str | Path = "registry",
    out_dir: str | Path = "outputs/corrective",
    max_level: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """逐級量測直到第一次穩定 429。"""
    from pcmef.experiments.e2_formal import load_frozen_decision_stack, prepare_cases

    say = progress or (lambda _m: None)
    stack = load_frozen_decision_stack(freeze_dir)
    prepared = prepare_cases(
        GATE_VALIDATION, stack, Path(out_dir) / "throughput",
        ds_dir=DS_V2, stress_manifest=EFFECTIVE_STRESS,
        code_version="throughput-ladder", progress=say,
    )
    escalated = [i for i, r in enumerate(prepared["routes"]) if str(r) == "escalated"]
    say(f"{len(escalated)} escalated case(s) available on the already-seen pool")

    levels: list[dict[str, Any]] = []
    ladder = LADDER if max_level is None else LADDER[:max_level]
    for n_cases in ladder:
        if n_cases > len(escalated):
            say(f"  level {n_cases} skipped: only {len(escalated)} escalated cases exist")
            break
        result = run_level(
            n_cases, prepared, escalated, stack, Path(registry_dir), say
        )
        levels.append(result)
        say(
            f"  level {n_cases}: {result['outcome']} "
            f"{result['successful_cases']}/{n_cases} case(s), "
            f"{result['total_api_requests']} request(s), {result['wall_time_sec']}s"
        )
        if result["outcome"] != "SUCCESS":
            say("  stopping: a level did not complete, so the next one cannot either")
            break

    passed = [lv for lv in levels if lv["outcome"] == "SUCCESS"]
    best = passed[-1] if passed else None
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
              "thoughts_tokens": 0}
    for level in levels:
        for key in totals:
            totals[key] += level["usage"]["totals"][key]
    totals["billable_output_tokens"] = (
        totals["completion_tokens"] + totals["thoughts_tokens"]
    )

    document = {
        "report_id": "free_tier_throughput_ladder",
        "scientific_result": False,
        "purpose": (
            "operational measurement of provider quota headroom. No accuracy is "
            "computed and no research parameter was changed."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ladder": list(ladder),
        "levels": levels,
        "max_successful_escalated_cases": best["successful_cases"] if best else 0,
        "max_successful_requests": best["total_api_requests"] if best else 0,
        "first_429_request_index": next(
            (lv["first_429_request_index"] for lv in levels
             if lv["first_429_request_index"] is not None), None
        ),
        "cumulative_usage": totals,
        "families_touched": "gate-validation effective-93 stress only",
        "FINAL_E2_36_43_TOUCHED": "NO",
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "throughput_ladder.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return document
