# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 experiments.e2_formal.execute_full_pcmef_cases
#         —— 正式執行與執行驗證共用的那一段迴圈。用合成 rows 與暫存 .npy，
#         不需要 frozen checkpoint，也不碰任何 dataset。
# 檔案路徑: tests/e2/test_execute_full_pcmef_cases.py
# 產生時間: 2026-09-02 13:10 +08:00
# 版本: v0.1.0
# 功能說明: 釘住 Full PC-MEF 執行迴圈的五條契約：escalated 走 s_A、
#           non-escalated 走 p_trad 且零呼叫、retry 耗盡即中止不 drop、
#           F(x) 有限且和為 1、case 數與呼叫數分開計。
# 模組定位: P0-3 的回歸測試。這段迴圈先前只存在於 run_formal_e2_full 內部，
#           **從未被任何測試或呼叫端執行過**；抽出來的目的就是讓它可被
#           直接驗證，並讓 validator 驗到與正式執行同一段程式。
# 主要責任:
#   1. test_escalated_* 驗證 escalated case 的 F 來自 arbiter
#   2. test_non_escalated_* 驗證零 provider 呼叫（selective escalation 的論點）
#   3. test_retry_exhaustion_* 驗證 ABORT_FORMAL_RUN 且不 drop case
#   4. test_output_is_a_valid_distribution 驗證 finite 與 sum-to-1
#   5. test_counts_separate_cases_from_calls
# 維護提醒:
#   - 不得把 test_non_escalated_cases_make_zero_provider_calls 放寬。
#     selective escalation 的整個論點就是「傳統證據足夠就不叫 LLM」，
#     多叫一次就推翻了它。
#   - 不得把 retry 耗盡改成跳過該 case。少一個 case 就是分母悄悄變小。
#   - v0.1.0 新增：首版，對應 P0-3。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_execute_full_pcmef_cases.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.experiments.e2_formal import (
    CountingAdapter, FormalE2Error, execute_full_pcmef_cases,
)


class _Response:
    text = ""
    request_id = "test"
    latency_ms = 0
    prompt_tokens = 0
    completion_tokens = 0
    thoughts_tokens = 0


class _Adapter:
    def invoke(self, *args, **kwargs):
        return _Response()


class _Runner:
    """決定性的四角色替身。`support` 決定仲裁結果。"""

    def __init__(self, support=(40.0, 30.0, 20.0, 10.0), adapter=None):
        self.support = list(support)
        self.adapter = adapter if adapter is not None else _Adapter()

    def run(self, spec, payload, images=None):
        code = str(getattr(spec, "task_code", ""))
        self.adapter.invoke(None, None, code, payload, None)
        if code == "observation_agent":
            reply = {
                "visual_description": "t", "tof_description": "t",
                "notable_discrepancies": [],
            }
        elif code in ("physics_agent", "visual_semantic_agent"):
            reply = {
                "modality": "physics" if code == "physics_agent" else "visual_semantic",
                "reasoning": "t",
                "class_support": {n: 25.0 for n in CLASS_ORDER},
                "confidence": 0.5,
            }
        else:
            reply = {
                "reasoning": "t",
                "class_support": dict(zip(CLASS_ORDER, self.support)),
                "decisive_evidence": "t",
            }
        return reply, _Response()


class _ExhaustingRunner(_Runner):
    """永遠失敗的 runner，用來觸發 retry 耗盡。"""

    def run(self, spec, payload, images=None):
        from pcmef.agents.pcmef_agents import RetryExhaustedError

        raise RetryExhaustedError(
            f"{getattr(spec, 'task_code', '?')} failed all 2 attempts; last error: test"
        )


@pytest.fixture()
def rule():
    from pcmef.perception.gate import GateRule

    return GateRule(
        q_vision_threshold=1.0, q_tof_threshold=10.0,
        disagreement_threshold=0.5, fusion_weight=0.5,
        temperature_vision=1.0, temperature_tof=1.0,
    )


@pytest.fixture()
def cases(tmp_path: Path):
    """三筆 case：一筆 escalated、兩筆非 escalated。"""
    rng = np.random.default_rng(20260902)
    rows, routes = [], ["escalated", "fusion", "trust_vision"]
    for index, route in enumerate(routes):
        rgb = tmp_path / f"rgb_{index}.npy"
        tof = tmp_path / f"tof_{index}.npy"
        np.save(rgb, rng.random((8, 8, 3)))
        np.save(tof, np.abs(rng.normal(size=(500, len(TOF_SCHEMA)))) + 1.0)
        rows.append(
            {
                "stress_id": f"case_{index}",
                "rgb_path": str(rgb),
                "tof_path": str(tof),
            }
        )

    n = len(rows)
    p_vision = np.tile(np.array([0.7, 0.1, 0.15, 0.05]), (n, 1))
    p_tof = np.tile(np.array([0.1, 0.6, 0.2, 0.1]), (n, 1))
    q = {"q_vision": np.full(n, 1.5), "q_tof": np.full(n, 12.0)}
    signals = {
        "disagreement": np.full(n, 0.4),
        "U_vision": np.full(n, 0.9),
        "U_tof": np.full(n, 1.1),
        "Q_vision": np.full(n, 0.3),
        "Q_tof": np.full(n, 13.0),
    }
    return rows, p_vision, p_tof, q, signals, np.array(routes)


def _run(cases, rule, runner, **kwargs):
    rows, p_vision, p_tof, q, signals, routes = cases
    return execute_full_pcmef_cases(
        rows, p_vision, p_tof, q, signals, routes, rule,
        agent_runner=runner, **kwargs,
    )


# ---------------------------------------------------------------------------
# escalated 走 arbiter；non-escalated 走傳統決策且不呼叫 provider
# ---------------------------------------------------------------------------


def test_escalated_case_final_comes_from_the_arbiter(cases, rule):
    result = _run(cases, rule, _Runner(support=(40.0, 30.0, 20.0, 10.0)))
    final = result["finals"][0]
    # 仲裁 support 正規化後應主導第一類，而 fusion 會給第一類 0.4。
    assert final.argmax() == 0
    assert not np.allclose(final, 0.5 * cases[1][0] + 0.5 * cases[2][0])
    assert result["escalated_cases"] == 1
    assert result["llm_called_cases"] == 1


def test_non_escalated_cases_make_zero_provider_calls(cases, rule):
    """selective escalation 的整個論點；多叫一次就推翻它。"""
    adapter = CountingAdapter(inner=_Adapter())
    runner = _Runner(adapter=adapter)
    result = _run(cases, rule, runner)

    # 三筆裡只有一筆 escalated，四個角色 -> 恰好四次。
    assert adapter.calls == 4
    assert result["escalated_cases"] == 1
    assert result["counter"] is adapter


def test_non_escalated_finals_equal_the_traditional_decision(cases, rule):
    rows, p_vision, p_tof, q, signals, routes = cases
    result = _run(cases, rule, _Runner())
    finals = result["finals"]

    # fusion 列
    assert np.allclose(finals[1], 0.5 * p_vision[1] + 0.5 * p_tof[1])
    # trust_vision 列
    assert np.allclose(finals[2], p_vision[2])


# ---------------------------------------------------------------------------
# retry 耗盡：中止整場，不 drop case
# ---------------------------------------------------------------------------


def test_retry_exhaustion_aborts_the_run_without_dropping_the_case(cases, rule):
    with pytest.raises(FormalE2Error) as error:
        _run(cases, rule, _ExhaustingRunner())
    message = str(error.value)
    assert "ABORT_FORMAL_RUN" in message
    assert "case_0" in message


def test_formal_mode_refuses_to_run_escalated_without_an_arbiter(cases, rule):
    from pcmef.perception.pcmef_orchestrator import FormalArbiterRequired

    with pytest.raises(FormalArbiterRequired):
        _run(cases, rule, None, formal=True)


# ---------------------------------------------------------------------------
# 輸出必須是合法分布
# ---------------------------------------------------------------------------


def test_output_is_finite_and_sums_to_one(cases, rule):
    finals = _run(cases, rule, _Runner())["finals"]
    assert np.all(np.isfinite(finals))
    assert np.allclose(finals.sum(axis=1), 1.0, atol=1e-9)


def test_a_trace_is_emitted_for_every_case(cases, rule):
    result = _run(cases, rule, _Runner())
    traces = result["traces"]
    assert len(traces) == len(cases[0])
    assert [t["stress_id"] for t in traces] == ["case_0", "case_1", "case_2"]


def test_counts_separate_escalated_cases_from_provider_calls(cases, rule):
    """一個 case 呼叫四個角色；把兩者混為一談會讓零呼叫那條無法驗證。"""
    adapter = CountingAdapter(inner=_Adapter())
    result = _run(cases, rule, _Runner(adapter=adapter))
    assert result["escalated_cases"] == 1
    assert adapter.calls == 4
    assert result["calls_before_first_escalation"] == 0
