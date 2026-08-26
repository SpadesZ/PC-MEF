# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；只讀 pcmef.llm.capabilities 的常數與純函式，
#         另用 conftest 的 seeded registry 驗證 dropdown 過濾；不讀寫檔案、不連線。
# 檔案路徑: tests/llm_admin/test_capabilities.py
# 產生時間: 2026-08-26 23:25 +08:00
# 版本: v0.1.0
# 功能說明: 驗證四個 Agent 角色的必要能力與規格逐字相符，且 embedding 沒有偷偷
#           變成 Thesis Core 的必要條件；同時驗證下拉選單只列得出綁得上去的選項。
# 模組定位: §45 task registry 與 Appendix J3 task 邊界的逐字比對。
#           它不驗證 probe 行為，只驗證「誰需要什麼」這張表沒有被改動。
# 主要責任:
#   1. test_task_registry_matches_the_specification 逐 task 比對必要能力
#   2. test_only_four_formal_task_codes_exist 擋下未經核定的 task 擴充
#   3. test_embedding_is_never_required_by_thesis_core 對應 §41
#   4. test_probe_order_puts_structured_json_before_vision 對應 §52 步驟 3
#   5. test_compatible_models_excludes_* 對應 LLM-UI-02 的過濾語意
# 維護提醒:
#   - 不得為了支援新用途而在 TASK_REGISTRY 增列 task_code；Appendix J3 規定
#     額外的 RAG/embedding/content-generation task 只能列 extension。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_capabilities.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.agents.provider import Capability, ProbeResult
from pcmef.llm.capabilities import (
    FORMAL_TASK_CODES,
    PROBE_ORDER,
    TASK_REGISTRY,
    CapabilityError,
    compatible_models,
    missing_capabilities,
    required_capabilities,
)

CHAT = Capability.CHAT
VISION = Capability.VISION
JSON = Capability.STRUCTURED_JSON

# SRC-SAI §45 的 Required Capabilities 欄，逐列抄錄。
SPEC_REQUIREMENTS = {
    "observation_agent": {CHAT, VISION, JSON},
    "physics_agent": {CHAT, JSON},
    "visual_semantic_agent": {CHAT, VISION, JSON},
    "arbitration_agent": {CHAT, JSON},
}


def test_task_registry_matches_the_specification():
    assert set(TASK_REGISTRY) == set(SPEC_REQUIREMENTS)
    for task_code, expected in SPEC_REQUIREMENTS.items():
        assert set(required_capabilities(task_code)) == expected, task_code


def test_only_four_formal_task_codes_exist():
    """Appendix J3：Thesis Core 的正式 Agent task codes 只有這四個。"""
    assert len(FORMAL_TASK_CODES) == 4
    assert set(FORMAL_TASK_CODES) == set(TASK_REGISTRY)


def test_embedding_is_never_required_by_thesis_core():
    """§41：PC-MEF Thesis Core 不需要 embedding，registry 支援它只為未來擴充。"""
    for task_code in FORMAL_TASK_CODES:
        assert Capability.EMBEDDING not in required_capabilities(task_code)


def test_every_task_declares_an_output_schema():
    for task_code, spec in TASK_REGISTRY.items():
        assert spec.output_schema.endswith("_v1"), task_code
        assert spec.ui_name, task_code


def test_unknown_task_code_fails_fast():
    with pytest.raises(CapabilityError, match="unknown task_code"):
        required_capabilities("rag_retrieval_agent")


def test_probe_order_puts_structured_json_before_vision():
    """§52 步驟 3：先 chat + structured_json，再 vision；embedding 只作 optional。"""
    order = list(PROBE_ORDER)
    assert order.index(CHAT) < order.index(JSON) < order.index(VISION)
    assert order[-1] is Capability.EMBEDDING


def test_missing_capabilities_reports_exactly_what_is_absent():
    assert missing_capabilities("physics_agent", [CHAT]) == (JSON,)
    assert missing_capabilities("physics_agent", [CHAT, JSON]) == ()


# ---------------------------------------------------------------------------
# Dropdown 過濾（LLM-UI-02 的核心語意）
# ---------------------------------------------------------------------------


def _verify(registry, model_profile_id, capabilities):
    for capability in capabilities:
        registry.record_verification(
            model_profile_id, ProbeResult(capability=capability, success=True)
        )


def _lock_line(registry, connection_id, model_profile_id):
    """dropdown 只列已 Locked 的線路，因此測試也必須先鎖（NOTE-023）。"""
    from pcmef.llm.registry import Lifecycle

    registry.select_model(connection_id, model_profile_id)
    registry.set_lifecycle(connection_id, Lifecycle.FETCHED)
    registry.set_lifecycle(connection_id, Lifecycle.CONNECTED)
    registry.set_lifecycle(connection_id, Lifecycle.LOCKED)


def test_compatible_models_excludes_an_embedding_only_model(seeded):
    registry = seeded.registry
    _verify(registry, seeded.full_model_id, (CHAT, VISION, JSON))
    _verify(registry, seeded.embed_only_id, (Capability.EMBEDDING,))
    _lock_line(registry, seeded.connection_id, seeded.full_model_id)

    connections = {c.connection_id: c for c in registry.list_connections()}
    for task_code in ("observation_agent", "arbitration_agent"):
        offered = compatible_models(task_code, registry.list_models(), connections)
        ids = {m.model_profile_id for m in offered}
        assert seeded.embed_only_id not in ids, task_code
        assert seeded.full_model_id in ids, task_code


def test_compatible_models_excludes_a_model_missing_one_capability(seeded):
    registry = seeded.registry
    _verify(registry, seeded.chat_only_id, (CHAT,))

    connections = {c.connection_id: c for c in registry.list_connections()}
    offered = compatible_models("arbitration_agent", registry.list_models(), connections)

    assert seeded.chat_only_id not in {m.model_profile_id for m in offered}


def test_compatible_models_excludes_a_disabled_connection(seeded):
    registry = seeded.registry
    _verify(registry, seeded.full_model_id, (CHAT, VISION, JSON))
    _lock_line(registry, seeded.connection_id, seeded.full_model_id)
    registry.set_connection_enabled(seeded.connection_id, False)

    connections = {c.connection_id: c for c in registry.list_connections()}
    offered = compatible_models("observation_agent", registry.list_models(), connections)

    assert offered == []
