# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.experiments.e2_formal 與
#         pcmef.perception.pcmef_orchestrator 的 fail-closed 行為，
#         以 stub adapter 取代 provider；不連線、不讀 dataset、
#         不觸碰 families 36-43。
# 檔案路徑: tests/e2/test_formal_executor.py
# 產生時間: 2026-09-01 18:05 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 Full PC-MEF executor 的四條不可協商行為 ——
#           formal 拒絕決定性替身、非 escalated 零呼叫、retry 耗盡即中止、
#           以及呼叫次數與 case 數分開計。
# 模組定位: e2_formal 的可執行防線。少了它，把 formal 的 fail-closed 改成
#           warning、或讓非 escalated 也帶 evidence，都不會有測試失敗。
# 主要責任:
#   1. test_formal_mode_refuses_a_deterministic_substitute
#   2. test_non_escalated_routes_make_zero_provider_calls
#   3. test_counting_adapter_separates_calls_from_cases
#   4. test_retry_exhaustion_raises_and_names_abort_formal_run
#   5. test_bridge_identities_hold_for_every_route
# 維護提醒:
#   - 不得把 test_formal_mode_refuses_a_deterministic_substitute 改成 warning。
#     它是「LLM 臂真的跑過」與「報告上寫著跑過」之間唯一的機械差別。
#   - 不得放寬 test_non_escalated_routes_make_zero_provider_calls。
#     selective escalation 的整個論點就是它。
#   - v0.1.0 新增：首版，對應 NOTE-051 與 AMD-006。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_formal_executor.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.numeric import ROUTE_ESCALATED
from pcmef.experiments.e2_formal import CountingAdapter
from pcmef.perception.pcmef_orchestrator import FormalArbiterRequired, decide_case

P_VISION = np.array([0.70, 0.10, 0.15, 0.05])
P_TOF = np.array([0.10, 0.60, 0.20, 0.10])
S_A = np.array([0.05, 0.05, 0.80, 0.10])
FUSION_WEIGHT = 0.5


class _RecordingAdapter:
    """最小 adapter 替身。只記錄有沒有被呼叫。"""

    def __init__(self) -> None:
        self.invocations: list[str] = []

    def invoke(self, _connection, _model, task_code, _payload, _cfg):
        self.invocations.append(str(task_code))
        raise AssertionError(
            "the provider must not be reached in these tests; a non-escalated "
            "route reaching it is exactly the defect under test"
        )


def test_formal_mode_refuses_a_deterministic_substitute():
    """formal 模式的 escalated case 沒有真 agent runner 必須中止。

    fallback_arbiter 刻意有值：它在 non-formal 模式是合法的，因此這個測試
    證明的是「formal 模式不看它」，而不是「沒有東西可以頂替」。
    """
    with pytest.raises(FormalArbiterRequired, match="four-agent arbiter"):
        decide_case(
            ROUTE_ESCALATED, P_VISION, P_TOF, FUSION_WEIGHT,
            agent_runner=None, formal=True, fallback_arbiter=lambda: S_A,
        )


def test_non_formal_mode_may_use_an_explicit_fallback():
    """對照組：pilot / 診斷路徑仍可用具名的替身，且結果就是 s_A。"""
    decision = decide_case(
        ROUTE_ESCALATED, P_VISION, P_TOF, FUSION_WEIGHT,
        agent_runner=None, formal=False, fallback_arbiter=lambda: S_A,
    )
    assert decision.escalated == 1
    assert decision.llm_called is False
    assert np.allclose(decision.final, S_A)


@pytest.mark.parametrize(
    "route,expected",
    [
        ("trust_vision", P_VISION),
        ("trust_tof", P_TOF),
        ("fusion", FUSION_WEIGHT * P_VISION + (1 - FUSION_WEIGHT) * P_TOF),
    ],
)
def test_bridge_identities_hold_for_every_non_escalated_route(route, expected):
    decision = decide_case(route, P_VISION, P_TOF, FUSION_WEIGHT, formal=True)
    assert decision.escalated == 0
    assert np.allclose(decision.final, expected)
    assert abs(float(decision.final.sum()) - 1.0) < 1e-12


def test_non_escalated_routes_make_zero_provider_calls():
    """非 escalated 分支連 agent_runner 都不得碰。

    _RecordingAdapter.invoke 會直接 assert 失敗，所以「有沒有呼叫」不是靠
    事後數數字，而是呼叫本身就會讓測試爆炸。
    """
    from pcmef.agents.pcmef_agents import AgentRunner

    adapter = _RecordingAdapter()
    runner = AgentRunner(adapter=adapter, connection=object(), model=object())
    for route in ("trust_vision", "trust_tof", "fusion"):
        decision = decide_case(
            route, P_VISION, P_TOF, FUSION_WEIGHT,
            evidence={"anything": 1}, agent_runner=runner, formal=True,
        )
        assert decision.llm_called is False
    assert adapter.invocations == []


def test_counting_adapter_separates_calls_from_cases():
    """一個 escalated case 會產生多次呼叫，兩個數字必須分開量。"""

    class _Ok:
        def invoke(self, _connection, _model, task_code, _payload, _cfg):
            return f"response-for-{task_code}"

        def other_method(self) -> str:
            return "forwarded"

    counter = CountingAdapter(_Ok())
    for task in ("observation_agent", "physics_agent", "visual_semantic_agent",
                 "arbitration_agent"):
        counter.invoke(None, None, task, {}, {})
    counter.invoke(None, None, "physics_agent", {}, {})   # 模擬一次 retry

    assert counter.calls == 5
    assert counter.calls_by_task["physics_agent"] == 2
    assert counter.calls_by_task["observation_agent"] == 1
    # 其餘 adapter 介面必須原樣轉發，否則包一層就換掉了 provider 的行為。
    assert counter.other_method() == "forwarded"


def test_retry_exhaustion_raises_and_names_abort_formal_run():
    """retry 耗盡必須拋錯並明講不得 drop case。"""
    from pathlib import Path

    from pcmef.agents.pcmef_agents import AGENTS, AgentRunner, RetryExhaustedError
    from pcmef.agents.provider import ProviderResponse

    class _BadJSON:
        def invoke(self, _connection, _model, _task, _payload, _cfg):
            return ProviderResponse(
                text="definitely not json", model_id="stub", provider="stub",
                request_id="r",
            )

    runner = AgentRunner(
        adapter=_BadJSON(), connection=object(), model=object(),
        schema_dir=Path("schemas"), max_attempts=2,
    )
    with pytest.raises(RetryExhaustedError) as error:
        runner.run(AGENTS["physics_agent"], {"observation_brief": {}}, images=[])

    assert "ABORT_FORMAL_RUN" in str(error.value)
    assert "Do NOT drop this case" in str(error.value)
    assert len(runner.attempts) == 2
    assert all(not a.ok for a in runner.attempts)


def test_escalated_case_without_evidence_is_refused():
    """有 runner 但沒有 evidence 也必須中止，不得送一個空 payload 出去。"""

    class _Unused:
        def invoke(self, *_args, **_kwargs):
            raise AssertionError("must not be reached without evidence")

    from pcmef.agents.pcmef_agents import AgentRunner

    runner = AgentRunner(adapter=_Unused(), connection=object(), model=object())
    with pytest.raises(FormalArbiterRequired, match="evidence payload"):
        decide_case(
            ROUTE_ESCALATED, P_VISION, P_TOF, FUSION_WEIGHT,
            evidence=None, agent_runner=runner, formal=True,
        )
