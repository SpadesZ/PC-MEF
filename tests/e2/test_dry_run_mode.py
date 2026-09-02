# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 experiments.e2_formal 的 llm_mode='skip'
#         分支與其治理欄位。用合成 rows 與暫存 .npy，不需要 provider、
#         registry 或 frozen checkpoint。
# 檔案路徑: tests/e2/test_dry_run_mode.py
# 產生時間: 2026-09-02 14:40 +08:00
# 版本: v0.1.0
# 功能說明: 確認零成本預演真的零成本，而且它的產出不可能被誤讀成正式結果。
# 模組定位: P0-5 的回歸測試。dry run 的風險不在「跑不動」，
#           而在「跑得動、看起來像正式結果」—— 這裡守的是後者。
# 主要責任:
#   1. test_dry_run_makes_no_provider_calls 零呼叫
#   2. test_dry_run_does_not_build_evidence escalated 連 payload 都不建
#   3. test_dry_run_keeps_the_real_route_in_the_trace 路由沒有被改寫
#   4. test_dry_run_cannot_be_formal 兩者互斥
#   5. test_unknown_llm_mode_is_refused
# 維護提醒:
#   - 不得讓 dry run 產出 pcmef_full 的數字。那一欄會等於 fixed_fusion，
#     看起來像「PC-MEF 沒有比固定融合好」，而實際上它從未執行。
#   - 不得讓 dry run 與正式結果寫進同一個檔名。同名的話，目錄裡兩者
#     長得一模一樣，唯一的差別藏在 JSON 欄位裡。
#   - 不得允許 llm_mode='skip' 與 formal=True 併用。跳過仲裁器正是
#     formal 模式存在的理由所要拒絕的那件事。
#   - v0.1.0 新增：首版，對應 P0-5。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_dry_run_mode.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pcmef.core.constants import TOF_SCHEMA
from pcmef.experiments.e2_formal import (
    LLM_MODE_EXECUTE, LLM_MODE_SKIP, CountingAdapter, FormalE2Error,
    execute_full_pcmef_cases,
)


class _Adapter:
    def invoke(self, *args, **kwargs):  # pragma: no cover - 不該被呼叫
        raise AssertionError("a dry run must not reach the provider")


class _Runner:
    adapter = _Adapter()

    def run(self, spec, payload, images=None):  # pragma: no cover
        raise AssertionError("a dry run must not invoke an agent")


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
    """四列：兩筆 escalated、兩筆非 escalated。"""
    routes = ["escalated", "fusion", "escalated", "trust_vision"]
    rng = np.random.default_rng(20260902)
    rows = []
    for index in range(len(routes)):
        # 刻意**不建立**任何 .npy：dry run 若嘗試讀取證據就會 FileNotFoundError，
        # 那正是我們要它不做的事。
        rows.append(
            {
                "stress_id": f"case_{index}",
                "rgb_path": str(tmp_path / f"missing_rgb_{index}.npy"),
                "tof_path": str(tmp_path / f"missing_tof_{index}.npy"),
            }
        )
    n = len(rows)
    p_vision = np.tile(np.array([0.7, 0.1, 0.15, 0.05]), (n, 1))
    p_tof = np.tile(np.array([0.1, 0.6, 0.2, 0.1]), (n, 1))
    q = {"q_vision": np.full(n, 1.5), "q_tof": np.full(n, 12.0)}
    signals = {"disagreement": np.full(n, 0.4)}
    return rows, p_vision, p_tof, q, signals, np.array(routes)


def _run(cases, rule, **kwargs):
    rows, p_vision, p_tof, q, signals, routes = cases
    return execute_full_pcmef_cases(
        rows, p_vision, p_tof, q, signals, routes, rule, **kwargs
    )


# ---------------------------------------------------------------------------
# 零成本
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_provider_calls(cases, rule):
    adapter = CountingAdapter(inner=_Adapter())
    runner = _Runner()
    runner.adapter = adapter
    result = _run(
        cases, rule, agent_runner=runner, formal=False, llm_mode=LLM_MODE_SKIP
    )
    assert adapter.calls == 0
    assert result["llm_called_cases"] == 0


def test_dry_run_does_not_build_evidence_for_escalated_cases(cases, rule):
    """證據檔案刻意不存在：只要 dry run 嘗試組裝就會炸開。"""
    result = _run(cases, rule, formal=False, llm_mode=LLM_MODE_SKIP)
    assert result["skipped_escalated_cases"] == 2
    assert result["escalated_cases"] == 2


def test_execute_mode_would_have_needed_the_evidence(cases, rule):
    """對照組：同一份輸入在 execute 模式下必定去讀證據，因而失敗。

    這條讓上一個測試有意義 —— 否則無法分辨「沒讀證據」和「根本沒走到」。
    """
    with pytest.raises((FileNotFoundError, OSError)):
        _run(cases, rule, agent_runner=_Runner(), formal=False,
             llm_mode=LLM_MODE_EXECUTE)


# ---------------------------------------------------------------------------
# 不可被誤讀
# ---------------------------------------------------------------------------


def test_dry_run_keeps_the_real_route_in_the_trace(cases, rule):
    """填位用的 fusion 決策不得改寫 trace 裡的 route。"""
    traces = _run(cases, rule, formal=False, llm_mode=LLM_MODE_SKIP)["traces"]
    escalated = [t for t in traces if t.get("llm_skipped")]
    assert len(escalated) == 2
    for trace in escalated:
        assert trace["route"] == "escalated"
        assert trace["e"] == 1
        assert trace["llm_called"] is False
        assert trace["final_is_placeholder"] is True


def test_non_escalated_traces_are_not_marked_as_skipped(cases, rule):
    traces = _run(cases, rule, formal=False, llm_mode=LLM_MODE_SKIP)["traces"]
    quiet = [t for t in traces if t["route"] in ("fusion", "trust_vision")]
    assert len(quiet) == 2
    assert all("llm_skipped" not in t for t in quiet)


def test_dry_run_still_produces_a_valid_distribution(cases, rule):
    """填位值仍必須是合法分布，否則後續形狀檢查失去意義。"""
    finals = _run(cases, rule, formal=False, llm_mode=LLM_MODE_SKIP)["finals"]
    assert np.all(np.isfinite(finals))
    assert np.allclose(finals.sum(axis=1), 1.0, atol=1e-9)


# ---------------------------------------------------------------------------
# 互斥與未知值
# ---------------------------------------------------------------------------


def test_dry_run_cannot_be_combined_with_formal_mode(cases, rule):
    with pytest.raises(FormalE2Error) as error:
        _run(cases, rule, formal=True, llm_mode=LLM_MODE_SKIP)
    assert "dry run is not a formal run" in str(error.value)


def test_unknown_llm_mode_is_refused(cases, rule):
    with pytest.raises(FormalE2Error) as error:
        _run(cases, rule, formal=False, llm_mode="whatever")
    assert "unknown llm_mode" in str(error.value)


def test_execute_is_the_default_mode(cases, rule):
    """預設必須是正式執行；預設成 skip 會讓忘記加參數的人得到假結果。"""
    import inspect

    default = inspect.signature(execute_full_pcmef_cases).parameters["llm_mode"].default
    assert default == LLM_MODE_EXECUTE
