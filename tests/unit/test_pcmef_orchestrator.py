# PC-MEF Research System source maintenance contract
# 上下游: 測試 pcmef.perception.pcmef_orchestrator；以假的 agent runner 替代
#         provider，不做任何對外連線，也不讀 dataset。
# 檔案路徑: tests/unit/test_pcmef_orchestrator.py
# 產生時間: 2026-09-01 03:20 +08:00
# 版本: v0.1.0
# 功能說明: 逐一驗證四種 route 的最終決策，並釘住兩條紅線：
#           非 escalated 不得呼叫 LLM，formal 模式不得以替身頂替 agent。
# 模組定位: selective_escalation_bridge_v1 的行為回歸線。
# 主要責任:
#   1. 四種 route 各自的 F(x)
#   2. non-escalated 的 provider 呼叫次數必須是 0
#   3. formal 模式缺 agent runner 必須中止
#   4. escalated 時 F 必須等於 s_A
# 維護提醒:
#   - 不得為了方便而允許 formal 模式回退到 deterministic arbiter。
#   - v0.1.0 新增。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_pcmef_orchestrator.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.perception.pcmef_orchestrator import (
    FormalArbiterRequired,
    decide_case,
)

P_V = np.array([0.70, 0.10, 0.10, 0.10])
P_T = np.array([0.10, 0.70, 0.10, 0.10])
W = 0.5


class _CountingRunner:
    """記錄被呼叫幾次的假 runner。真正的 agent 執行由 tests/agents 覆蓋。"""

    def __init__(self, support: dict[str, float]) -> None:
        self.support, self.calls = support, 0
        self.attempts: list = []


def _patch_run_case(monkeypatch, runner):
    import pcmef.agents.pcmef_agents as agents

    class _Bundle:
        def __init__(self, support):
            self.artifacts = {"arbitration_validated": {"class_support": support}}

    def fake_run(runner_arg, evidence):
        runner_arg.calls += 1
        return _Bundle(runner_arg.support)

    monkeypatch.setattr(agents, "run_pcmef_case", fake_run)


@pytest.mark.parametrize("route,expected", [
    ("trust_vision", P_V),
    ("trust_tof", P_T),
    ("fusion", 0.5 * P_V + 0.5 * P_T),
])
def test_each_non_escalated_route_returns_its_traditional_vector(route, expected):
    decision = decide_case(route, P_V, P_T, W)
    assert np.allclose(decision.final, expected, atol=1e-9)
    assert decision.escalated == 0


@pytest.mark.parametrize("route", ["trust_vision", "trust_tof", "fusion"])
def test_non_escalated_never_calls_the_llm(monkeypatch, route):
    """selective escalation 的整個論點就是這一條。"""
    runner = _CountingRunner({"Empty": 25, "Water-filled": 25,
                              "Bubbly": 25, "Misty": 25})
    _patch_run_case(monkeypatch, runner)

    decision = decide_case(route, P_V, P_T, W, evidence={}, agent_runner=runner)

    assert runner.calls == 0, "a non-escalated case must not reach the provider"
    assert decision.llm_called is False


def test_escalated_final_equals_s_a(monkeypatch):
    runner = _CountingRunner({"Empty": 10.0, "Water-filled": 20.0,
                              "Bubbly": 60.0, "Misty": 10.0})
    _patch_run_case(monkeypatch, runner)

    decision = decide_case(
        "escalated", P_V, P_T, W, evidence={"x": 1}, agent_runner=runner
    )

    assert runner.calls == 1
    assert decision.llm_called is True
    assert np.allclose(decision.final, decision.s_a, atol=1e-12)
    # 完全交給 arbitration：傳統向量不得殘留影響。
    assert decision.final.argmax() == 2
    assert not np.allclose(decision.final, P_V, atol=1e-3)


def test_formal_mode_refuses_a_deterministic_substitute():
    """替身頂替會讓 LLM 臂在報告上成立而實際從未執行。"""
    with pytest.raises(FormalArbiterRequired, match="real four-agent arbiter"):
        decide_case("escalated", P_V, P_T, W, formal=True, agent_runner=None)


def test_non_formal_mode_may_use_an_explicit_fallback():
    decision = decide_case(
        "escalated", P_V, P_T, W, formal=False,
        fallback_arbiter=lambda: np.array([0.1, 0.2, 0.3, 0.4]),
    )
    assert decision.llm_called is False
    assert np.allclose(decision.final, [0.1, 0.2, 0.3, 0.4], atol=1e-9)


@pytest.mark.parametrize("route", ["trust_vision", "trust_tof", "fusion", "escalated"])
def test_every_route_returns_a_finite_distribution(monkeypatch, route):
    runner = _CountingRunner({"Empty": 40.0, "Water-filled": 30.0,
                              "Bubbly": 20.0, "Misty": 10.0})
    _patch_run_case(monkeypatch, runner)

    decision = decide_case(
        route, P_V, P_T, W, evidence={"x": 1}, agent_runner=runner
    )
    assert np.all(np.isfinite(decision.final)) and np.all(decision.final >= 0)
    assert np.isclose(decision.final.sum(), 1.0, rtol=0.0, atol=1e-12)
    assert decision.to_trace()["bridge_version"] == "selective_escalation_bridge_v1"
