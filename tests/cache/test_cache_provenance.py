# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.agents.cache 的 producer trace 寫入、
#         pcmef.agents.cached_case 的 CachedArbiter 在未命中時記錄，
#         以及 pcmef.console.run_view.cache_entry_view 把它讀成畫面資料。
#         以 stub cache 與 stub runner 取代 provider；不連線。
# 檔案路徑: tests/cache/test_cache_provenance.py
# 產生時間: 2026-09-04 22:20 +08:00
# 版本: v0.1.0
# 功能說明: cache hit 之後仍然說得出「答案最初來自哪一次 Agent execution」。
# 模組定位: NOTE-074 的可執行防線。§48 的 cache 只存六份答案，不存
#           role-projected payload；沒有 producer trace 的話，一次命中就
#           永久失去「四個角色當時各看到什麼」這件事。
# 主要責任:
#   1. test_a_miss_writes_a_producer_trace
#   2. test_a_hit_does_not_overwrite_it
#   3. test_the_trace_carries_every_required_field
#   4. test_the_trace_carries_no_case_identity 不得破壞內容定址
#   5. test_the_producer_trace_is_not_part_of_the_hit_contract
#   6. test_cache_entry_view_reconstructs_the_isolation_matrix
#   7. test_an_old_entry_says_why_it_cannot_be_traced
# 維護提醒:
#   - 不得把 producer_trace 放進 AGENT_ARTIFACT_NAMES。命中的判準是六份
#     齊全；加進去會讓所有既有目錄一夜之間變成未命中。
#   - 不得把 case_id / class_label / condition / severity 寫進 producer
#     trace。它們是 §48 明列不得進入快取的欄位。
#   - 不得讓寫入失敗中止決策。答案已經拿到，六份 artifact 已經落盤。
#   - v0.1.0 新增：首版，對應 P1-2 / NOTE-074。
# 驗證方式:
#   - py -3.10 -m pytest tests/cache/test_cache_provenance.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.agents.cache import (
    AGENT_ARTIFACT_NAMES, PRODUCER_TRACE_NAME, AgentArtifactCache, AgentBundle,
    AgentCacheKey,
)
from pcmef.agents.cached_case import CachedArbiter, CacheIdentity
from pcmef.agents.pcmef_agents import AgentCallRecord

CLASSES = ["Empty", "Water-filled", "Bubbly", "Misty"]

#: 四個角色實際收到的 payload，形狀取自 project_for_role() 的輸出。
ROLE_PAYLOADS = {
    "observation_agent": {
        "role": "observation_agent", "class_order": CLASSES,
        "evidence_contract_version": "v2", "representation_mode": "structured",
        "tof_summary": {"peak_bin": 41, "ambient": 0.12},
    },
    "physics_agent": {
        "role": "physics_agent", "class_order": CLASSES,
        "evidence_contract_version": "v2", "representation_mode": "structured",
        "observation_brief": {"summary": "…"},
        "tof_summary": {"peak_bin": 41, "ambient": 0.12},
        "calibrated_class_probabilities": {"tof": [0.1, 0.6, 0.2, 0.1]},
    },
    "visual_semantic_agent": {
        "role": "visual_semantic_agent", "class_order": CLASSES,
        "evidence_contract_version": "v2", "representation_mode": "structured",
        "observation_brief": {"summary": "…"},
        "calibrated_class_probabilities": {"vision": [0.7, 0.1, 0.15, 0.05]},
    },
    "arbitration_agent": {
        "role": "arbitration_agent", "class_order": CLASSES,
        "evidence_contract_version": "v2", "representation_mode": "structured",
        "observation_brief": {"summary": "…"},
        "anonymous_proposals": [{"modality": "physics"}, {"modality": "visual_semantic"}],
        "cross_modal": {"D": 0.63},
    },
}

RAW_SUPPORT = {"Empty": 5.0, "Water-filled": 10.0, "Bubbly": 70.0, "Misty": 14.0}


class _Runner:
    """最小 AgentRunner 替身：只提供 call_log。"""

    def __init__(self) -> None:
        self.call_log: list[AgentCallRecord] = []

    def produce(self) -> AgentBundle:
        """模擬 run_pcmef_case：填 call_log 並回傳六份 artifact。"""
        for role, payload in ROLE_PAYLOADS.items():
            self.call_log.append(
                AgentCallRecord(
                    role=role,
                    input_payload=dict(payload),
                    image_digests=["a" * 64] if role != "physics_agent" else [],
                    raw_output={"class_support": RAW_SUPPORT} if role == "arbitration_agent"
                    else {"modality": role.replace("_agent", "")},
                    validated_output={
                        "class_support": RAW_SUPPORT,
                        "support_sum_before_normalisation": 99.0,
                        "support_sum_within_tolerance": True,
                        "conflict_tag": "none",
                    } if role == "arbitration_agent" else {"modality": "x"},
                    attempt_count=1,
                )
            )
        artifacts = {name: {"stub": name} for name in AGENT_ARTIFACT_NAMES}
        artifacts["arbitration_raw"] = {"class_support": RAW_SUPPORT}
        artifacts["arbitration_validated"] = {"class_support": RAW_SUPPORT}
        return AgentBundle(
            artifacts=artifacts, provider_request_id="req-123",
            token_usage=4321, latency_ms=987,
        )


def _key() -> AgentCacheKey:
    return AgentCacheKey(
        evidence_hash="e" * 64, representation_mode="structured",
        provider_model_id="stub-model-1", provider_revision="2026-09",
        prompt_hashes={"observation_agent": "p" * 64},
        schema_hash="s" * 64, runtime_config_hash="r" * 64,
    )


@pytest.fixture()
def cache(tmp_path):
    return AgentArtifactCache(root=tmp_path / "agents")


@pytest.fixture()
def arbiter(cache):
    return CachedArbiter(
        cache=cache,
        identity=CacheIdentity(
            model_id="stub-model-1", provider_revision="2026-09",
            runtime_config_hash="r" * 64,
        ),
        run_identity={"run_id": "e2_final", "code_version": "cafebabe1234",
                      "freeze_dir": "freeze/runs/PFC-001"},
    )


def _install_stub_key(monkeypatch, runner):
    """把 case_cache_key / run_pcmef_case 換成本檔的替身。

    CachedArbiter 在函式內部 import，所以要 patch 來源模組。
    """
    import pcmef.agents.pcmef_agents as agents

    monkeypatch.setattr(agents, "case_cache_key", lambda *a, **k: _key())
    monkeypatch.setattr(agents, "run_pcmef_case", lambda r, e: r.produce())


# ---------------------------------------------------------------------------
# 未命中時寫、命中時不覆寫
# ---------------------------------------------------------------------------


def test_a_miss_writes_a_producer_trace(monkeypatch, arbiter, cache):
    runner = _Runner()
    _install_stub_key(monkeypatch, runner)
    arbiter.case_index = 42
    arbiter(runner, {"evidence": True})

    assert arbiter.last.hit is False
    path = cache.path_for(_key()) / f"{PRODUCER_TRACE_NAME}.json"
    assert path.exists()


def test_a_hit_does_not_overwrite_it(monkeypatch, arbiter, cache):
    """第二次是命中：不呼叫 producer，也不動 producer trace。"""
    runner = _Runner()
    _install_stub_key(monkeypatch, runner)
    arbiter.case_index = 0
    arbiter(runner, {"evidence": True})
    first = cache.read_provenance(_key().digest())

    second_runner = _Runner()
    arbiter.case_index = 7
    arbiter(second_runner, {"evidence": True})

    assert arbiter.last.hit is True
    assert second_runner.call_log == []          # 命中就沒有任何角色投影
    assert cache.read_provenance(_key().digest()) == first
    assert first["producer_row_index"] == 0      # 仍是**最初**那一次


def test_the_hit_avoids_four_provider_calls(monkeypatch, arbiter, cache):
    runner = _Runner()
    _install_stub_key(monkeypatch, runner)
    arbiter(runner, {"evidence": True})
    arbiter(_Runner(), {"evidence": True})
    assert arbiter.summary()["hits"] == 1
    assert arbiter.summary()["provider_calls_avoided"] == 4


# ---------------------------------------------------------------------------
# 內容
# ---------------------------------------------------------------------------


def test_the_trace_carries_every_required_field(monkeypatch, arbiter, cache):
    runner = _Runner()
    _install_stub_key(monkeypatch, runner)
    arbiter.case_index = 11
    arbiter(runner, {"evidence": True})

    trace = cache.read_provenance(_key().digest())
    assert trace["cache_key"] == _key().digest()
    assert trace["evidence_hash"] == "e" * 64
    assert trace["producer_run"]["run_id"] == "e2_final"
    assert trace["producer_run"]["code_version"] == "cafebabe1234"
    assert trace["producer_row_index"] == 11
    assert trace["model_id"] == "stub-model-1"
    assert trace["provider_revision"] == "2026-09"
    assert trace["runtime_config_hash"] == "r" * 64
    assert trace["provider_request_id"] == "req-123"
    assert trace["n_roles"] == 4
    assert trace["total_attempts"] == 4
    # 鑰匙的七個組成也要在，否則看不出「為什麼是這把鑰匙」。
    assert set(trace["key_components"]) >= {
        "evidence_hash", "representation_mode", "provider_model_id",
        "provider_revision", "prompt_hashes", "schema_hash",
        "runtime_config_hash",
    }

    roles = {a["role"] for a in trace["artifacts"]}
    assert roles == set(ROLE_PAYLOADS)
    for artifact in trace["artifacts"]:
        assert "input_payload" in artifact
        assert "received_image" in artifact
        assert "image_digests" in artifact
        assert "raw_output" in artifact
        assert "validated_output" in artifact
        assert "attempt_count" in artifact


def test_the_trace_carries_no_case_identity(monkeypatch, arbiter, cache):
    """§48：case 身分不得進入快取目錄。

    `producer_row_index` 是**這次 run 之內**的序號，不是 canonical case id；
    它不參與鑰匙，也認不出是哪一個場景。
    """
    runner = _Runner()
    _install_stub_key(monkeypatch, runner)
    arbiter(runner, {"evidence": True})

    text = json.dumps(cache.read_provenance(_key().digest()), ensure_ascii=False)
    for forbidden in ("case_id", "canonical_case_id", "class_label",
                      "condition", "severity", "parent_scene_family"):
        assert forbidden not in text


def test_the_producer_trace_is_not_part_of_the_hit_contract(monkeypatch, cache):
    """六份齊全就是命中，與 producer trace 在不在無關。

    反過來也一樣：只有 producer trace 而缺 artifact 不算命中。
    """
    assert PRODUCER_TRACE_NAME not in AGENT_ARTIFACT_NAMES

    key = _key()
    bundle = AgentBundle(artifacts={n: {"x": n} for n in AGENT_ARTIFACT_NAMES})
    cache.put(key, bundle)
    assert cache.get(key) is not None            # 沒有 producer trace 也命中

    cache.write_provenance(key, {"cache_key": key.digest()})
    assert cache.get(key) is not None            # 有了也仍然命中


def test_write_provenance_is_write_once(cache):
    key = _key()
    cache.put(key, AgentBundle(artifacts={n: {"x": n} for n in AGENT_ARTIFACT_NAMES}))
    cache.write_provenance(key, {"cache_key": key.digest(), "n_roles": 4})
    cache.write_provenance(key, {"cache_key": key.digest(), "n_roles": 999})
    assert cache.read_provenance(key.digest())["n_roles"] == 4


def test_a_provenance_write_failure_does_not_break_the_decision(
    monkeypatch, arbiter, cache
):
    """寫不進去只損失日後的解釋能力，不得讓這一筆 case 失敗。"""
    runner = _Runner()
    _install_stub_key(monkeypatch, runner)

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(cache, "write_provenance", _boom)
    bundle = arbiter(runner, {"evidence": True})

    assert bundle is not None
    assert arbiter.summary()["provenance_errors"]
    assert "OSError" in arbiter.summary()["provenance_errors"][0]


# ---------------------------------------------------------------------------
# 畫面：從 cache entry 追回原始執行
# ---------------------------------------------------------------------------


def test_cache_entry_view_reconstructs_the_isolation_matrix(
    monkeypatch, arbiter, cache, tmp_path
):
    from pcmef.console import run_view

    runner = _Runner()
    _install_stub_key(monkeypatch, runner)
    arbiter(runner, {"evidence": True})

    entry = run_view.cache_entry_view(cache.root, _key().digest())
    assert entry is not None
    assert entry["producer"]["n_roles"] == 4

    agents = entry["producer_agents"]
    assert agents["isolation"]["roles"] == list(ROLE_PAYLOADS)
    rows = {r["field"]: r["cells"] for r in agents["isolation"]["rows"]}

    # 隔離的實質：physics 看不到圖，arbitration 看得到匿名意見，
    # 而 gate_route 誰都收不到。每一格都由實際 payload 算出。
    order = list(ROLE_PAYLOADS)
    assert rows["image"][order.index("physics_agent")] == "absent"
    assert rows["image"][order.index("visual_semantic_agent")] == "present"
    assert rows["anonymous_proposals"][order.index("observation_agent")] == "absent"
    assert rows["anonymous_proposals"][order.index("arbitration_agent")] == "present"
    assert set(rows["gate_route"]) == {"absent"}
    # 模態限定的欄位要說出它帶的是哪一側。
    assert rows["calibrated_class_probabilities"][order.index("physics_agent")] == "tof"
    assert rows["calibrated_class_probabilities"][
        order.index("visual_semantic_agent")] == "vision"


def test_cache_entry_view_shows_the_support_chain(monkeypatch, arbiter, cache):
    from pcmef.console import run_view

    runner = _Runner()
    _install_stub_key(monkeypatch, runner)
    arbiter(runner, {"evidence": True})

    support = run_view.cache_entry_view(cache.root, _key().digest())[
        "producer_agents"]["support"]
    assert support["raw"] == RAW_SUPPORT
    assert support["raw_sum"] == 99.0
    assert support["within_tolerance"] is True
    # s_A 的最後一步在 decision bridge，不在快取裡：這裡必須留白而不是編值。
    assert support["s_a"] == {}


def test_an_old_entry_says_why_it_cannot_be_traced(cache):
    """2026-09-04 之前寫入的目錄沒有 producer trace。畫面要說明原因。"""
    from pcmef.console import run_view

    key = _key()
    cache.put(key, AgentBundle(artifacts={n: {"x": n} for n in AGENT_ARTIFACT_NAMES}))

    entry = run_view.cache_entry_view(cache.root, key.digest())
    assert entry["producer"] is None
    assert entry["producer_agents"] is None
    assert "producer_trace.json" in entry["producer_missing_reason"]
