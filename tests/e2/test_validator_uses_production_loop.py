# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 experiments.e2_executor_validation._execute_subset
#         的切片對齊，並以 AST 確認驗證器沒有自己重建執行迴圈。
#         不需要 provider、registry 或 frozen checkpoint。
# 檔案路徑: tests/e2/test_validator_uses_production_loop.py
# 產生時間: 2026-09-02 13:50 +08:00
# 版本: v0.1.0
# 功能說明: 確認「驗證過的」與「要跑的」是同一段程式，而且餵進去的子集
#           沒有錯位。
# 模組定位: P0-4 的回歸測試。這裡守的是 NOTE-055 的結論：
#           驗證器一旦自己重建迴圈，它證明的就只是「另一份很像的實作能跑」。
# 主要責任:
#   1. test_subset_rows_stay_aligned_* 切片錯位不會拋錯，只能靠測試抓
#   2. test_validator_calls_the_production_loop monkeypatch 確認呼叫關係
#   3. test_validator_module_does_not_rebuild_the_decision_loop 以 AST 擋回歸
# 維護提醒:
#   - 不得放寬 test_validator_module_does_not_rebuild_the_decision_loop。
#     驗證器可以呼叫 decide_case 做**純函式層**的 bridge identity 檢查
#     （那不碰資料集），但不得在 run_executor_validation 內逐 row 迴圈呼叫它。
#   - 切片測試刻意用 non-escalated route：那條不呼叫 provider，
#     因此對齊性可以在沒有任何憑證的環境下驗。
#   - v0.1.0 新增：首版，對應 P0-4。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_validator_uses_production_loop.py -v
# ------------------------------------------------------------

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import numpy as np
import pytest

from pcmef.experiments import e2_executor_validation as validation

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def rule():
    from pcmef.perception.gate import GateRule

    return GateRule(
        q_vision_threshold=1.0, q_tof_threshold=10.0,
        disagreement_threshold=0.5, fusion_weight=0.5,
        temperature_vision=1.0, temperature_tof=1.0,
    )


@pytest.fixture()
def prepared():
    """六列，每列的 p_vision 都不同，錯位就會被看出來。"""
    n = 6
    p_vision = np.array(
        [[0.9, 0.04, 0.03, 0.03],
         [0.04, 0.9, 0.03, 0.03],
         [0.03, 0.04, 0.9, 0.03],
         [0.03, 0.03, 0.04, 0.9],
         [0.5, 0.3, 0.15, 0.05],
         [0.25, 0.25, 0.25, 0.25]]
    )
    p_tof = np.tile(np.array([0.1, 0.2, 0.3, 0.4]), (n, 1))
    return {
        "rows": [{"stress_id": f"row_{i}"} for i in range(n)],
        "p_vision": p_vision,
        "p_tof": p_tof,
        "q": {"q_vision": np.arange(n, dtype=float), "q_tof": np.arange(n, dtype=float)},
        "signals": {"disagreement": np.arange(n, dtype=float)},
        # 全部 trust_vision：不呼叫 provider，因此不需要憑證。
        "routes": np.array(["trust_vision"] * n),
    }


# ---------------------------------------------------------------------------
# 切片對齊
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("indices", [[0], [3], [1, 4], [5, 2, 0], [0, 1, 2, 3, 4, 5]])
def test_subset_rows_stay_aligned_with_their_probabilities(prepared, rule, indices):
    """trust_vision 的 F 必須等於**該列自己的** p_vision。

    切片錯位不會拋任何錯誤 —— 它只會讓驗證在錯的證據上通過。
    """
    result = validation._execute_subset(
        prepared, indices, {"rule": rule}, None, lambda _m: None
    )
    finals = result["finals"]
    assert finals.shape == (len(indices), 4)
    for position, source in enumerate(indices):
        assert np.allclose(finals[position], prepared["p_vision"][source]), (
            f"row {position} of the subset carries the probabilities of source row "
            f"{source}, but the output does not match"
        )


def test_subset_traces_carry_the_right_stress_ids(prepared, rule):
    result = validation._execute_subset(
        prepared, [4, 1], {"rule": rule}, None, lambda _m: None
    )
    assert [t["stress_id"] for t in result["traces"]] == ["row_4", "row_1"]


def test_subset_makes_no_provider_calls_for_non_escalated_routes(prepared, rule):
    result = validation._execute_subset(
        prepared, [0, 1, 2], {"rule": rule}, None, lambda _m: None
    )
    assert result["llm_called_cases"] == 0
    assert result["escalated_cases"] == 0


# ---------------------------------------------------------------------------
# 確實走 production loop
# ---------------------------------------------------------------------------


def test_subset_calls_the_production_loop(prepared, rule, monkeypatch):
    """釘住呼叫關係本身，而不只是結果長得像。"""
    import pcmef.experiments.e2_formal as formal

    seen: dict[str, object] = {}
    original = formal.execute_full_pcmef_cases

    def spy(*args, **kwargs):
        seen["called"] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(formal, "execute_full_pcmef_cases", spy)
    validation._execute_subset(prepared, [0], {"rule": rule}, None, lambda _m: None)
    assert seen.get("called") is True


def test_validator_does_not_rebuild_the_decision_loop():
    """`run_executor_validation` 內不得有逐 row 呼叫 decide_case 的迴圈。

    純函式層的 bridge identity 檢查仍可呼叫 decide_case —— 那些不碰資料集，
    且正是它們證明了 escalated/non-escalated 兩條 identity。這裡擋的是
    在主驗證流程裡重建一份等價執行迴圈。
    """
    source = inspect.getsource(validation.run_executor_validation)
    tree = ast.parse(source.lstrip())

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While)):
            continue
        body = ast.dump(node)
        if "decide_case" in body:
            offenders.append(f"line {node.lineno}")

    assert not offenders, (
        "run_executor_validation rebuilds a per-row decide_case loop at "
        f"{offenders}. Validation must go through "
        "e2_formal.execute_full_pcmef_cases, otherwise it proves that a second, "
        "very similar implementation works rather than that the production entry "
        "point works (NOTE-055)."
    )


def test_validator_reports_that_it_used_the_production_loop():
    """報告裡要有一條可稽核的宣告，否則讀報告的人無從得知。"""
    source = inspect.getsource(validation.run_executor_validation)
    assert "validation_runs_the_production_execution_loop" in source
    assert "execute_full_pcmef_cases" in source
