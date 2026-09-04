# PC-MEF Research System source maintenance contract
# 上下游: 驗 pcmef/agents/cached_case.py 的 CachedArbiter，
#         以及它經由 decide_case(case_arbiter=...) 接進
#         execute_full_pcmef_cases() 之後的行為。
# 檔案路徑: tests/cache/test_cached_case.py
# 產生時間: 2026-09-04 18:30 +08:00
# 版本: v0.1.0
# 功能說明: 確認命中就不呼叫 provider、鑰匙分層正確、預設不介入。
# 模組定位: P3-1 的驗收。這一層**在決策路徑上** ——
#           錯誤的命中會安靜地把另一份證據的答案當成這一筆的答案。
# 主要責任:
#   1. 第二次執行同一批 case 的 provider 呼叫必須是 0（FR-031）
#   2. 七要素任一項改變即為不同鑰匙，必須重問
#   3. 不給 cache 時行為與接線前完全相同
#   4. 命中時 trace 要說是命中，不得看起來像沒有呼叫過 agent
# 維護提醒:
#   - 不得放寬 test_a_second_run_makes_no_provider_calls。resume 不得重複
#     付費是 FR-031 與 LLM-RESUME-01 的要求，不是最佳化。
#   - 不得移除 test_a_different_model_is_a_different_key。少了它，換模型
#     之後仍會命中舊答案，而那不會有任何症狀。
#   - 不得把「命中」與「未 escalate」在 trace 裡混為一談。命中代表這一筆
#     由 agent 決定過，只是這次沒有重問。
# 驗證方式:
#   - py -3.10 -m pytest tests/cache/test_cached_case.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pcmef.agents.cache import AgentArtifactCache
from pcmef.agents.cached_case import CachedArbiter, CacheIdentity
from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.experiments.e2_formal import CountingAdapter, execute_full_pcmef_cases


class _Response:
    text = ""
    request_id = "test"
    latency_ms = 0
    prompt_tokens = 11
    completion_tokens = 7
    thoughts_tokens = 3


class _Adapter:
    def invoke(self, *args, **kwargs):
        return _Response()


class _Runner:
    """決定性的四角色替身。"""

    def __init__(self, support=(40.0, 30.0, 20.0, 10.0), adapter=None):
        self.support = list(support)
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
            reply = {"reasoning": "t",
                     "class_support": dict(zip(CLASS_ORDER, self.support)),
                     "decisive_evidence": "t"}
        return reply, _Response()


IDENTITY = CacheIdentity(
    model_id="test-model", provider_revision="rev-1",
    runtime_config_hash="c" * 64,
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
    """兩筆 escalated、一筆非 escalated。"""
    rng = np.random.default_rng(20260904)
    rows, routes = [], ["escalated", "escalated", "fusion"]
    for index, _route in enumerate(routes):
        rgb = tmp_path / f"rgb_{index}.npy"
        tof = tmp_path / f"tof_{index}.npy"
        # 每一筆的內容不同 —— 否則兩筆 escalated 會是同一把鑰匙，
        # 第一次執行內部就已經命中，測不出「跨 run 才命中」。
        np.save(rgb, rng.random((8, 8, 3)))
        np.save(tof, np.abs(rng.normal(size=(500, len(TOF_SCHEMA)))) + 1.0)
        rows.append({"stress_id": f"case_{index}", "rgb_path": str(rgb),
                     "tof_path": str(tof)})

    n = len(rows)
    p_vision = np.tile(np.array([0.7, 0.1, 0.15, 0.05]), (n, 1))
    p_tof = np.tile(np.array([0.1, 0.6, 0.2, 0.1]), (n, 1))
    q = {"q_vision": np.full(n, 1.5), "q_tof": np.full(n, 12.0)}
    signals = {
        "disagreement": np.full(n, 0.4), "U_vision": np.full(n, 0.9),
        "U_tof": np.full(n, 1.1), "Q_vision": np.full(n, 0.3),
        "Q_tof": np.full(n, 13.0),
    }
    return rows, p_vision, p_tof, q, signals, np.array(routes)


def _run(cases, rule, cache_root, *, support=(40.0, 30.0, 20.0, 10.0),
         identity=IDENTITY, traces=None):
    rows, p_vision, p_tof, q, signals, routes = cases
    counter = CountingAdapter(inner=_Adapter())
    runner = _Runner(support=support, adapter=counter)
    arbiter = None
    if cache_root is not None:
        arbiter = CachedArbiter(
            cache=AgentArtifactCache(root=cache_root), identity=identity
        )
    result = execute_full_pcmef_cases(
        rows, p_vision, p_tof, q, signals, routes, rule,
        agent_runner=runner, formal=True, case_arbiter=arbiter,
        trace_sink=(traces.append if traces is not None else None),
    )
    return result, counter, arbiter


# ---------------------------------------------------------------------------
# 命中就不呼叫 provider
# ---------------------------------------------------------------------------


def test_a_second_run_makes_no_provider_calls(cases, rule, tmp_path):
    """FR-031 / LLM-RESUME-01：resume 不得重複付費。"""
    root = tmp_path / "agents"

    _, first_counter, first = _run(cases, rule, root)
    assert first_counter.calls == 8          # 2 escalated x 4 roles
    assert (first.hits, first.misses) == (0, 2)

    _, second_counter, second = _run(cases, rule, root)
    assert second_counter.calls == 0, "命中之後不得再送任何請求"
    assert (second.hits, second.misses) == (2, 0)


def test_the_cached_decision_is_identical(cases, rule, tmp_path):
    """命中沿用的是同一份 artifact，F(x) 必須逐值相同。"""
    root = tmp_path / "agents"
    first, _, _ = _run(cases, rule, root)
    second, _, _ = _run(cases, rule, root)
    np.testing.assert_array_equal(first["finals"], second["finals"])


def test_without_a_cache_nothing_changes(cases, rule, tmp_path):
    """預設不介入：不給 cache 時每次都實呼。"""
    _, first, arbiter = _run(cases, rule, None)
    _, second, _ = _run(cases, rule, None)
    assert arbiter is None
    assert first.calls == second.calls == 8


# ---------------------------------------------------------------------------
# 鑰匙分層
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "another-model"),
        ("provider_revision", "rev-2"),
        ("runtime_config_hash", "d" * 64),
    ],
)
def test_a_different_identity_is_a_different_key(cases, rule, tmp_path, field, value):
    """七要素任一項變了就必須重問。

    少了這一條，換模型或換 runtime 設定之後仍會命中舊答案 ——
    而那不會有任何症狀，報告上照樣寫著新模型。
    """
    root = tmp_path / "agents"
    _run(cases, rule, root)

    changed = CacheIdentity(**{
        "model_id": IDENTITY.model_id,
        "provider_revision": IDENTITY.provider_revision,
        "runtime_config_hash": IDENTITY.runtime_config_hash,
        field: value,
    })
    _, counter, arbiter = _run(cases, rule, root, identity=changed)
    assert counter.calls == 8, f"{field} 改變後仍然命中"
    assert arbiter.hits == 0


def test_different_evidence_is_a_different_key(cases, rule, tmp_path):
    """兩筆內容不同的 escalated case 不得共用一把鑰匙。"""
    root = tmp_path / "agents"
    _, _, arbiter = _run(cases, rule, root)
    assert arbiter.misses == 2, "兩筆不同證據卻只產生一把鑰匙"


def test_an_identity_missing_the_model_is_refused():
    with pytest.raises(ValueError, match="model_id"):
        CacheIdentity(model_id="", provider_revision="r",
                      runtime_config_hash="c" * 64)


def test_an_identity_missing_the_runtime_hash_is_refused():
    with pytest.raises(ValueError, match="runtime_config_hash"):
        CacheIdentity(model_id="m", provider_revision="r", runtime_config_hash="")


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


def test_a_cache_hit_is_visible_in_the_trace(cases, rule, tmp_path):
    """命中時 call_log 是空的。trace 必須說是命中，
    否則畫面會顯示成「escalated 但沒有呼叫過任何角色」。"""
    from pcmef.experiments.decision_trace import build_case_trace

    root = tmp_path / "agents"
    _run(cases, rule, root)

    traces: list = []
    _run(cases, rule, root, traces=traces)
    escalated = [t for t in traces if t["route"] == "escalated"]
    assert escalated and all(t["cache"]["hit"] for t in escalated)
    assert all(len(t["agent_calls"]) == 0 for t in escalated), (
        "命中卻仍有 call_log 紀錄，代表 provider 被呼叫了"
    )

    payload = escalated[0]
    trace = build_case_trace(
        row={"class_label": "Empty"}, row_index=0, case_id="case_0000",
        p_vision=payload["p_vision"], p_tof=payload["p_tof"],
        signals={"D": 0.4}, q_vision=1.5, q_tof=12.0,
        route="escalated", rule=rule, decision=payload["decision"],
        agent_calls=[], class_order=CLASS_ORDER, arms={},
        rgb_preview=None, tof_shape=None, dry_run=False,
        cache=payload["cache"],
    )
    assert trace.agent_execution["invoked"] is True
    assert trace.agent_execution["reason"] == "cache_hit"
    assert trace.agent_execution["cache_key"] == payload["cache"]["cache_key"]
    assert "no provider call was made" in trace.agent_execution["note"]


def test_a_miss_still_records_the_key(cases, rule, tmp_path):
    """未命中也要記 key —— 下一次 resume 靠它認出這一筆已經問過。"""
    traces: list = []
    _run(cases, rule, tmp_path / "agents", traces=traces)
    escalated = [t for t in traces if t["route"] == "escalated"]
    assert all(not t["cache"]["hit"] for t in escalated)
    assert all(len(t["cache"]["cache_key"]) == 64 for t in escalated)


def test_a_non_escalated_case_has_no_cache_entry(cases, rule, tmp_path):
    """沒進仲裁就沒有鑰匙可言。"""
    traces: list = []
    _run(cases, rule, tmp_path / "agents", traces=traces)
    plain = [t for t in traces if t["route"] != "escalated"]
    assert plain and all(t["cache"] is None for t in plain)


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------


def test_the_summary_reports_the_hit_rate(cases, rule, tmp_path):
    root = tmp_path / "agents"
    _run(cases, rule, root)
    _, _, arbiter = _run(cases, rule, root)
    summary = arbiter.summary()
    assert summary["enabled"] is True
    assert summary["hits"] == 2 and summary["misses"] == 0
    assert summary["hit_rate"] == 1.0
    assert summary["model_id"] == IDENTITY.model_id
