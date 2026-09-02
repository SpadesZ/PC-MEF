# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 experiments.e2_formal 的 G4
#         reliability_routing 臂。用合成 rows 與暫存 .npy，不需要 provider、
#         registry 或 frozen checkpoint。
# 檔案路徑: tests/e2/test_reliability_routing_arm.py
# 產生時間: 2026-09-02 19:30 +08:00
# 版本: v0.1.0
# 功能說明: 釘住 G4 的定義：同一組路由、escalated 改用 fixed fusion、
#           整條臂零 provider 呼叫。
# 模組定位: P1-2 的回歸測試。G4 的存在理由是把「路由」與「仲裁」的效果
#           分開量 —— G4 vs G3 是路由，G5 vs G4 是仲裁。少了它，
#           G5 vs G3 會把兩件事混在一起。
# 主要責任:
#   1. test_escalated_rows_fall_back_to_fixed_fusion
#   2. test_non_escalated_rows_match_the_full_arm 兩條臂在傳統路徑上必須相同
#   3. test_the_arm_costs_no_provider_calls
#   4. test_the_arm_is_identical_in_dry_run_and_execute_mode
#   5. test_it_is_a_baseline_in_the_paired_statistics
# 維護提醒:
#   - 不得讓 G4 走任何會呼叫 provider 的分支。它的定義就是「路由但不仲裁」；
#     一旦它叫了 LLM，G5 vs G4 就不再是仲裁的增量。
#   - 不得把 G4 從 dry run 拿掉。它不依賴 LLM，因此在預演中完全有效，
#     那正是零成本預演能給出四個真實 arm 的原因。
#   - v0.1.0 新增：首版，對應 P1-2。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_reliability_routing_arm.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.experiments.e2_formal import (
    LLM_MODE_EXECUTE, LLM_MODE_SKIP, CountingAdapter, execute_full_pcmef_cases,
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
    """決定性的四角色替身，仲裁結果刻意與 fusion 不同。"""

    def __init__(self, adapter=None):
        self.adapter = adapter if adapter is not None else _Adapter()

    def run(self, spec, payload, images=None):
        code = str(getattr(spec, "task_code", ""))
        self.adapter.invoke(None, None, code, payload, None)
        if code == "observation_agent":
            reply = {"visual_description": "t", "tof_description": "t",
                     "notable_discrepancies": []}
        elif code in ("physics_agent", "visual_semantic_agent"):
            reply = {
                "modality": "physics" if code == "physics_agent" else "visual_semantic",
                "reasoning": "t",
                "class_support": {n: 25.0 for n in CLASS_ORDER},
                "confidence": 0.5,
            }
        else:
            # 明顯偏向最後一類，好與 fusion 區分開。
            reply = {
                "reasoning": "t",
                "class_support": dict(zip(CLASS_ORDER, [1.0, 1.0, 1.0, 97.0])),
                "decisive_evidence": "t",
            }
        return reply, _Response()


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
    """五列，涵蓋四種 route，其中兩列 escalated。"""
    routes = ["escalated", "fusion", "trust_vision", "escalated", "trust_tof"]
    rng = np.random.default_rng(20260902)
    rows = []
    for index in range(len(routes)):
        rgb = tmp_path / f"rgb_{index}.npy"
        tof = tmp_path / f"tof_{index}.npy"
        np.save(rgb, rng.random((8, 8, 3)))
        np.save(tof, np.abs(rng.normal(size=(500, len(TOF_SCHEMA)))) + 1.0)
        rows.append({"stress_id": f"case_{index}",
                     "rgb_path": str(rgb), "tof_path": str(tof)})

    n = len(rows)
    p_vision = np.tile(np.array([0.7, 0.1, 0.15, 0.05]), (n, 1))
    p_tof = np.tile(np.array([0.1, 0.6, 0.2, 0.1]), (n, 1))
    q = {"q_vision": np.full(n, 1.5), "q_tof": np.full(n, 12.0)}
    signals = {"disagreement": np.full(n, 0.4)}
    return rows, p_vision, p_tof, q, signals, np.array(routes)


def _g4(finals: np.ndarray, fused: np.ndarray, routes: np.ndarray) -> np.ndarray:
    """G4 的定義，與 run_formal_e2_full 內的算式相同。"""
    mask = np.asarray([str(r) == "escalated" for r in routes])
    return np.where(mask[:, None], fused, finals)


def _fused(p_vision, p_tof, rule):
    return rule.fusion_weight * p_vision + (1.0 - rule.fusion_weight) * p_tof


# ---------------------------------------------------------------------------
# 定義
# ---------------------------------------------------------------------------


def test_escalated_rows_fall_back_to_fixed_fusion(cases, rule):
    rows, p_vision, p_tof, q, signals, routes = cases
    result = execute_full_pcmef_cases(
        rows, p_vision, p_tof, q, signals, routes, rule,
        agent_runner=_Runner(), formal=True,
    )
    fused = _fused(p_vision, p_tof, rule)
    g4 = _g4(result["finals"], fused, routes)

    for index, route in enumerate(routes):
        if str(route) == "escalated":
            assert np.allclose(g4[index], fused[index]), index
            # 而 G5 在同一列必須**不同** —— 否則這條測試什麼都沒證明。
            assert not np.allclose(result["finals"][index], fused[index]), index


def test_non_escalated_rows_match_the_full_arm(cases, rule):
    """傳統路徑上 G4 與 G5 必須逐位元相同：它們走的是同一條路。"""
    rows, p_vision, p_tof, q, signals, routes = cases
    result = execute_full_pcmef_cases(
        rows, p_vision, p_tof, q, signals, routes, rule,
        agent_runner=_Runner(), formal=True,
    )
    g4 = _g4(result["finals"], _fused(p_vision, p_tof, rule), routes)

    for index, route in enumerate(routes):
        if str(route) != "escalated":
            assert np.array_equal(g4[index], result["finals"][index]), index


def test_the_arm_adds_no_provider_calls(cases, rule):
    """G4 完全由既有資訊導出，不得讓呼叫數增加。"""
    rows, p_vision, p_tof, q, signals, routes = cases
    adapter = CountingAdapter(inner=_Adapter())
    result = execute_full_pcmef_cases(
        rows, p_vision, p_tof, q, signals, routes, rule,
        agent_runner=_Runner(adapter=adapter), formal=True,
    )
    before = adapter.calls
    _g4(result["finals"], _fused(p_vision, p_tof, rule), routes)
    assert adapter.calls == before
    # 兩列 escalated x 四個角色。
    assert adapter.calls == 8


def test_the_arm_is_identical_in_dry_run_and_execute_mode(cases, rule):
    """G4 不依賴 LLM，因此預演與正式執行必須給出同一條臂。

    這是它能在零成本預演中提供真實數字的原因。
    """
    rows, p_vision, p_tof, q, signals, routes = cases
    fused = _fused(p_vision, p_tof, rule)

    executed = execute_full_pcmef_cases(
        rows, p_vision, p_tof, q, signals, routes, rule,
        agent_runner=_Runner(), formal=True, llm_mode=LLM_MODE_EXECUTE,
    )
    skipped = execute_full_pcmef_cases(
        rows, p_vision, p_tof, q, signals, routes, rule,
        formal=False, llm_mode=LLM_MODE_SKIP,
    )

    assert np.array_equal(
        _g4(executed["finals"], fused, routes),
        _g4(skipped["finals"], fused, routes),
    )
    # 對照：G5 本身在兩種模式下**必須不同**，否則上面的相等毫無意義。
    assert not np.allclose(executed["finals"], skipped["finals"])


# ---------------------------------------------------------------------------
# 進到報告與統計
# ---------------------------------------------------------------------------


def test_reliability_routing_is_a_paired_statistics_baseline():
    """G5 對 G1-G4 都要有成對比較（實驗計畫 v1.2 §5）。"""
    import inspect

    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    assert '"reliability_routing"' in source
    for baseline in ("vision_only", "tof_only", "fixed_fusion", "reliability_routing"):
        assert f'"{baseline}"' in source, baseline


def test_the_arm_is_documented_in_the_report():
    """報告要自己說明每條臂是什麼，讀者不必回去翻程式。"""
    import inspect

    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    assert "arm_definitions" in source
    assert "G4" in source and "G5" in source


def test_the_arm_survives_a_dry_run_report():
    """dry run 拿掉的是 pcmef_full，不是 reliability_routing。"""
    import inspect

    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    # pcmef_full 只在非 dry-run 時加入；G4 無條件加入。
    assert 'arms["pcmef_full"] = finals' in source
    assert '"reliability_routing": reliability_routing,' in source
