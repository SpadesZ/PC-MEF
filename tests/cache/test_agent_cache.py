# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；在 tmp_path 建立 artifacts/agents 目錄樹並以
#         計數用的假 producer 驅動 pcmef.agents.cache；
#         cache index 寫進臨時 SQLite。不對外連線。
# 檔案路徑: tests/cache/test_agent_cache.py
# 產生時間: 2026-08-27 04:30 +08:00
# 版本: v0.1.0
# 功能說明: 執行 §51 的 LLM-CACHE-01、LLM-CACHE-02 與 LLM-RESUME-01 ——
#           同樣的輸入在三組 checkpoint pair 只問一次模型、任一 hash 變動就重問、
#           已完成的 case 重跑時只讀既有結果。
# 模組定位: §48 cache contract 與 Appendix J2 共用邊界的可執行防線。
# 主要責任:
#   1. test_llm_cache_01_* 三組 pair 只產生一次 provider call
#   2. test_llm_cache_02_* 七個要素逐一變動皆造成 cache miss
#   3. test_llm_resume_01_* resume 時不再呼叫 provider
#   4. test_pair_specific_quantities_are_refused 對應 Appendix J2 右欄
#   5. test_cache_key_cannot_contain_case_identity 對應 §48 與 NOTE-004
#   6. test_a_half_written_folder_is_not_a_hit 驗證中斷不會變成假命中
# 維護提醒:
#   - 不得把 test_llm_cache_01 改成「呼叫兩次但結果相同」；驗收條件是
#     **只產生 1 次 provider call**，成本與可重現性都靠這一點。
#   - v0.1.0 新增：首版，對應 §51 三條 cache/resume 驗收與 NOTE-021。
# 驗證方式:
#   - py -3.10 -m pytest tests/cache/test_agent_cache.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.agents.cache import (
    AGENT_ARTIFACT_NAMES,
    AgentArtifactCache,
    AgentBundle,
    AgentCacheKey,
    CacheError,
    assert_pair_independent,
)

BASE_COMPONENTS = {
    "evidence_hash": "e" * 64,
    "representation_mode": "FIXED_SUMMARY",
    "provider_model_id": "gemini-probe",
    "provider_revision": "rev-2026-08",
    "prompt_hashes": {"observation": "p1", "arbitration": "p2"},
    "schema_hash": "s" * 64,
    "runtime_config_hash": "r" * 64,
}


def _key(**overrides) -> AgentCacheKey:
    return AgentCacheKey(**{**BASE_COMPONENTS, **overrides})


def _bundle(marker: str = "one") -> AgentBundle:
    return AgentBundle(
        artifacts={
            name: {"schema_version": "v1", "marker": marker, "stage": name}
            for name in AGENT_ARTIFACT_NAMES
        },
        provider_request_id=f"req-{marker}",
        token_usage=1234,
        latency_ms=456,
    )


class _CountingProducer:
    """每被呼叫一次就代表一次真實的 provider 往返。"""

    def __init__(self, marker: str = "one") -> None:
        self.calls = 0
        self.marker = marker

    def __call__(self) -> AgentBundle:
        self.calls += 1
        return _bundle(self.marker)


@pytest.fixture
def cache(tmp_path) -> AgentArtifactCache:
    return AgentArtifactCache(root=tmp_path / "agents")


# ---------------------------------------------------------------------------
# LLM-CACHE-01
# ---------------------------------------------------------------------------


def test_llm_cache_01_three_checkpoint_pairs_produce_one_provider_call(cache):
    """相同 evidence/config 在 3 checkpoint pairs 僅產生 1 次 Agent provider call。

    三組 pair 在此以「同一把 key 被查三次」表示 —— 這正是 §48 的重點：
    cache key 根本不含 pair 身分，所以 pair 的數量對它毫無影響。
    """
    producer = _CountingProducer()
    key = _key()

    results = [cache.resolve(key, producer) for _ in range(3)]

    assert producer.calls == 1
    assert cache.stats.provider_calls == 1
    assert cache.stats.hits == 2
    assert [hit for _, hit in results] == [False, True, True]
    # s_A 的來源（arbitration_validated）三次完全相同。
    arbitration = {
        json.dumps(bundle.artifacts["arbitration_validated"], sort_keys=True)
        for bundle, _ in results
    }
    assert len(arbitration) == 1


def test_a_second_cache_instance_still_hits(tmp_path):
    """resume 之後是新的行程與新的 cache 物件，命中必須來自磁碟而非記憶體。"""
    key = _key()
    first = AgentArtifactCache(root=tmp_path / "agents")
    producer = _CountingProducer()
    first.resolve(key, producer)

    second = AgentArtifactCache(root=tmp_path / "agents")
    bundle, hit = second.resolve(key, producer)

    assert producer.calls == 1
    assert hit is True
    assert bundle.provider_request_id == "req-one"


# ---------------------------------------------------------------------------
# LLM-CACHE-02
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, changed",
    [
        ("evidence_hash", "f" * 64),
        ("representation_mode", "FULL_500x4"),
        ("provider_model_id", "gemini-other"),
        ("provider_revision", "rev-2026-09"),
        ("prompt_hashes", {"observation": "p1", "arbitration": "CHANGED"}),
        ("schema_hash", "t" * 64),
        ("runtime_config_hash", "q" * 64),
    ],
)
def test_llm_cache_02_any_component_change_is_a_miss(cache, field, changed):
    """prompt/schema/model/revision/representation/evidence 任一 hash 變 -> cache miss。"""
    producer = _CountingProducer()
    cache.resolve(_key(), producer)
    assert producer.calls == 1

    _, hit = cache.resolve(_key(**{field: changed}), producer)

    assert hit is False, f"changing {field} must not hit the cache"
    assert producer.calls == 2


def test_the_digest_is_order_sensitive_across_components(cache):
    """§48 寫的是有序串接；換位置就得到相同雜湊會造成誤命中。"""
    swapped = _key(
        provider_model_id=BASE_COMPONENTS["provider_revision"],
        provider_revision=BASE_COMPONENTS["provider_model_id"],
    )
    assert swapped.digest() != _key().digest()


def test_identical_components_produce_an_identical_digest():
    assert _key().digest() == _key().digest()
    assert len(_key().digest()) == 64


# ---------------------------------------------------------------------------
# LLM-RESUME-01
# ---------------------------------------------------------------------------


def test_llm_resume_01_a_completed_case_never_calls_the_provider_again(cache):
    """已完成 formal case resume 時只讀 frozen raw/validated response。"""
    key = _key()
    first_producer = _CountingProducer("original")
    cache.resolve(key, first_producer)

    def must_not_run() -> AgentBundle:
        raise AssertionError(
            "resume re-called the provider; FR-031 and LLM-RESUME-01 forbid it"
        )

    bundle, hit = cache.resolve(key, must_not_run)

    assert hit is True
    assert first_producer.calls == 1
    assert bundle.artifacts["observation_raw"]["marker"] == "original"
    assert bundle.artifacts["arbitration_validated"]["marker"] == "original"


def test_a_half_written_folder_is_not_a_hit(cache, tmp_path):
    """一次中斷的 run 不該悄悄變成完整結果。"""
    key = _key()
    producer = _CountingProducer()
    cache.resolve(key, producer)
    (cache.path_for(key) / "arbitration_validated.json").unlink()

    _, hit = cache.resolve(key, producer)

    assert hit is False
    assert producer.calls == 2


def test_a_bundle_missing_an_artifact_is_refused():
    with pytest.raises(CacheError, match="missing artifact"):
        AgentBundle(artifacts={"observation_raw": {}})


def test_overwriting_an_artifact_with_different_content_is_refused(cache):
    key = _key()
    cache.put(key, _bundle("first"))
    with pytest.raises(CacheError, match="immutable"):
        cache.put(key, _bundle("second"))


def test_writing_identical_content_is_idempotent(cache):
    key = _key()
    assert cache.put(key, _bundle()) == cache.put(key, _bundle())


# ---------------------------------------------------------------------------
# Appendix J2 共用邊界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["p_T", "p_V", "r_T", "g", "F_PCMEF", "predicted_class", "training_pair_id"]
)
def test_pair_specific_quantities_are_refused(field):
    """Appendix J2 右欄的量必須每組 pair 各自重算，不得進共用快取。"""
    with pytest.raises(CacheError, match="checkpoint-pair specific"):
        assert_pair_independent({"observation": {field: 0.5}})


def test_a_nested_pair_specific_field_is_still_caught():
    with pytest.raises(CacheError, match="checkpoint-pair specific"):
        assert_pair_independent({"a": [{"b": {"p_rel": 0.9}}]})


def test_ordinary_agent_content_passes_the_boundary_check():
    assert_pair_independent(
        {
            "schema_version": "arbitration_output_v1",
            "class_support": {"Empty": 10, "Water-filled": 70},
            "conflict_tag": "none",
            "evidence_summary": "text",
        }
    )


def test_cache_key_cannot_contain_case_identity():
    """§48 的 key 是 content-addressed，與 case 身分無關（NOTE-004 維護邊界）。"""
    with pytest.raises(CacheError, match="case or checkpoint attribute"):
        _key(prompt_hashes={"opaque_case_id": "abc"})


def test_empty_components_are_refused():
    with pytest.raises(CacheError, match="evidence_hash"):
        _key(evidence_hash="")
    with pytest.raises(CacheError, match="prompt hash"):
        _key(prompt_hashes={})


# ---------------------------------------------------------------------------
# 費用索引
# ---------------------------------------------------------------------------


def test_the_cache_index_records_one_row_per_key(tmp_path):
    """同一把鑰匙代表同一份輸入，provider 只真的被呼叫過一次，帳也只該有一列。"""
    from pcmef.llm.registry import LLMRegistry

    registry = LLMRegistry(tmp_path / "llm.db")
    cache = AgentArtifactCache(root=tmp_path / "agents", registry=registry)
    producer = _CountingProducer()

    for _ in range(3):
        cache.resolve(_key(), producer)

    entries = registry.cache_entries()
    assert len(entries) == 1
    assert entries[0]["cache_key"] == _key().digest()
    assert entries[0]["token_usage"] == 1234
    assert entries[0]["provider_request_id"] == "req-one"
