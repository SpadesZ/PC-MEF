# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 conftest 的臨時 registry、prompts_dir 與
#         decided_config 驅動 pcmef.llm.snapshot，lock 寫在 tmp_path 的
#         freeze 目錄；不對外連線，也不觸碰 repo 的 freeze/。
# 檔案路徑: tests/llm_admin/test_snapshot.py
# 產生時間: 2026-08-27 02:50 +08:00
# 版本: v0.1.0
# 功能說明: 執行 §51 的 LLM-UI-05 與 LLM-UI-06，並驗證快照在前提未齊時
#           如實列出缺什麼、拒絕凍結，且未決的教授裁決不會被任何預設值蓋過去。
# 模組定位: §47 State Invalidation 與 Appendix J1 Binding Resolution Invariant
#           的可執行防線，也是三條紅線中「!required 禁補預設」與
#           「lock 不可覆寫不可跳步」在 LLM 子系統的落點。
# 主要責任:
#   1. test_shipped_config_blocks_the_snapshot 驗證未裁決值擋住凍結
#   2. test_blocking_reasons_name_every_missing_prerequisite 驗證缺項如實列出
#   3. test_llm_ui_05_* 驗證快照後改 draft 不影響既有 lock hash
#   4. test_llm_ui_06_* 驗證 formal 解析只認 lock、手改 SQLite 無效
#   5. test_freezing_cannot_skip_the_prerequisite_locks 驗證不可跳步
#   6. test_a_non_formal_provider_cannot_be_frozen 驗證 stub 進不了 formal
#   7. test_state_invalidation_matches_section_47 逐列比對 §47
# 維護提醒:
#   - 不得為了讓凍結測試好寫而放寬 llm_runtime 的前置 lock；
#     那條相依鏈就是 §23 state machine 本身。
#   - 不得在測試中替 representation_mode 直接改 configs/base.yaml；
#     shipped config 必須維持 formal-blocking，裁決值只能疊在其上。
#   - v0.1.0 新增：首版，對應 §51 LLM-UI-05/06 與 NOTE-020。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_snapshot.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.agents.provider import Capability, ModelDescriptor, ProbeResult
from pcmef.core.config import load_config
from pcmef.core.constants import CLASS_ORDER
from pcmef.core.locks import LockError, LockOrderError, LockStore
from pcmef.llm.registry import Lifecycle
from pcmef.llm.capabilities import FORMAL_TASK_CODES, required_capabilities
from pcmef.llm.snapshot import (
    STATE_INVALIDATION,
    SnapshotError,
    build_runtime_snapshot,
    freeze_runtime_snapshot,
    resolve_formal_agent_binding,
)

# llm_runtime 之前必須存在的 lock，依 core.locks 的相依圖排序。
# 內容用佔位值：本檔測的是順序與不可覆寫，不是各 lock 的內容契約。
_PREREQUISITE_CHAIN: tuple[tuple[str, dict], ...] = (
    ("real_split_policy", {
        "creation_phase": "AFTER_M0_BEFORE_ANY_CALIBRATION", "split_unit": "recording",
        "stratification": "class", "group_rule": "seeded_stratified_recording",
        "allocation": {"calibration": 0.7, "heldout_real": 0.3},
        "minimum_per_class": 100, "seed": 20260826, "eligible_set_hash": "e" * 8,
        "calibration_set_hash": "c" * 8, "heldout_real_set_hash": "h" * 8,
        "redraw_policy": "FORBIDDEN_AFTER_LOCK",
    }),
    ("initial_simulation", {
        "scene_hash": "s1", "surrogate_hash": "u1",
        "code_version": "v0", "parameter_ranges": {},
        "parameter_set_hash": "test-parameter-set",
    }),
    ("calibrated_simulation", {
        "calibrated_scene_hash": "s2", "calibrated_surrogate_hash": "u2",
        "calibration_source_hashes": [],
    }),
    ("metric_config", {"metric_definitions": {}, "code_hash": "m1"}),
    ("e1_candidates", {
        "initial_simulation_hash": "a", "calibrated_simulation_hash": "b",
        "metric_config_hash": "c", "heldout_set_hash": "d",
    }),
    ("e1_evaluation_design", {
        "base_scenario_ids": [], "offset_strata": [], "optical_transport_seeds": [],
        "acquisition_seed_matrix": [], "matched_realization_hash": "r",
    }),
    ("e1_scientific_rule", {
        "normalization_scales": {}, "aggregation": "macro",
        "improvement_threshold": 0.0, "regression_tolerance": 0.0,
        "trend_rule": "non_degraded", "bootstrap_replicates": 10000,
        "bootstrap_seed": 20260826, "code_hash": "x",
        "amendment_id": "AMD-001", "amendment_payload_hash": "a" * 64,
        "g08_contract_version": "v2",
    }),
    ("claim_boundary", {
        "e1_fidelity_scope": "tof_sensor_surrogate",
        "synthetic_rgb_statement": "paired evidence only",
    }),
    ("e1_outcome", {
        "outcome": "E1_SCIENTIFIC_PASS", "claim_mode": "tof_physics_calibrated",
        "result_hashes": [],
    }),
    ("synthetic_split_policy", {
        "parent_scene_family_rule": "disjoint", "family_hashes": [],
        "split_assignment_hash": "y",
    }),
    ("agent_schema", {
        "arbitration_schema_sha256": "z" * 8, "class_order": list(CLASS_ORDER),
        "support_bridge_version": "v1",
    }),
)


def _freeze_prerequisites(store: LockStore) -> None:
    for name, payload in _PREREQUISITE_CHAIN:
        store.write(name, payload)


@pytest.fixture
def formal_ready(seeded, tmp_path):
    """一個 provider 具 formal 資格、四個角色都綁好且能力已驗證的 registry。

    provider 寫成 google 而 probe 走離線 stub：probe 的傳輸方式與
    「這個 provider 能不能進 formal」是兩件獨立的事，分開才測得到後者。
    """
    registry = seeded.registry
    connection = registry.add_connection(
        name="Gemini Formal", provider="google", secret_ref="env:PCMEF_TEST_KEY"
    )
    registry.upsert_models(
        connection.connection_id,
        [
            ModelDescriptor(
                model_id="gemini-probe", provider="google",
                display_name="Gemini Probe", provider_revision="rev-2026-08",
            )
        ],
    )
    profile = next(
        m for m in registry.list_models(connection.connection_id)
        if m.model_id == "gemini-probe"
    )
    for capability in (
        Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON
    ):
        registry.record_verification(
            profile.model_profile_id,
            ProbeResult(capability=capability, success=True, detail="ok"),
        )
    # 只有 Locked 的線路能綁定（NOTE-023）。
    registry.select_model(connection.connection_id, profile.model_profile_id)
    registry.set_lifecycle(connection.connection_id, Lifecycle.FETCHED)
    registry.set_lifecycle(connection.connection_id, Lifecycle.CONNECTED)
    registry.set_lifecycle(connection.connection_id, Lifecycle.LOCKED)
    for task_code in FORMAL_TASK_CODES:
        registry.set_binding(
            task_code, profile.model_profile_id, required_capabilities(task_code)
        )
    return registry, profile.model_profile_id, connection.connection_id


def _build(registry, config, prompts_dir):
    return build_runtime_snapshot(registry, config, prompts_dir=prompts_dir)


# ---------------------------------------------------------------------------
# 未裁決值不得被補上
# ---------------------------------------------------------------------------


def _withdraw(config, *dotted_keys: str):
    """把已裁決的值改回 !required，重現「教授還沒裁決」的狀態。

    2026-08-31（NOTE-046）之後，configs/base.yaml 的
    agents.representation_mode 與 agents.retry.max_attempts 都已核定，
    因此 shipped config 本身不再示範這條紅線。紅線的**機制**仍必須有測試 ——
    否則哪天補值邏輯被加回來，不會有任何測試變紅。
    """
    import copy

    from pcmef.core.config import Required

    clone = copy.deepcopy(config)
    for dotted_key in dotted_keys:
        node = clone.data
        parts = dotted_key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = Required(dotted_key)
    return clone


def test_an_undecided_value_blocks_the_snapshot(formal_ready, prompts_dir):
    """紅線一：未核定值一律 !required，禁補預設。"""
    registry, _, _ = formal_ready
    undecided = _withdraw(
        load_config(["configs/base.yaml"]),
        "agents.representation_mode",
        "agents.retry.max_attempts",
    )

    snapshot = _build(registry, undecided, prompts_dir)

    assert snapshot.freezable is False
    blocking = " ".join(snapshot.blocking_reasons)
    assert "agents.representation_mode" in blocking
    assert "agents.retry.max_attempts" in blocking
    assert snapshot.representation_mode is None
    with pytest.raises(SnapshotError, match="prerequisites"):
        snapshot.to_lock_payload()


def test_shipped_config_now_carries_both_advisor_decisions(formal_ready, prompts_dir):
    """NOTE-046：兩項裁決已於 2026-08-31 核定，因此不再是 blocking 原因。

    核定發生在 families 36-43 的 Formal E2 產生**之前**，且未看過任何
    final 結果。這個測試記錄的是「現在的真實狀態」，不是放寬檢查。
    """
    registry, _, _ = formal_ready
    shipped = load_config(["configs/base.yaml"])

    snapshot = _build(registry, shipped, prompts_dir)

    assert snapshot.representation_mode == "FIXED_SUMMARY"
    assert snapshot.runtime_config["retry_max_attempts"] == 2
    blocking = " ".join(snapshot.blocking_reasons)
    assert "agents.representation_mode" not in blocking
    assert "agents.retry.max_attempts" not in blocking


def test_an_unresolved_value_still_changes_the_candidate_hash(
    formal_ready, prompts_dir, decided_config
):
    """帶缺口的快照不該和補齊之後的快照得到相同雜湊。"""
    registry, _, _ = formal_ready
    withdrawn = _withdraw(
        load_config(["configs/base.yaml"]),
        "agents.representation_mode",
        "agents.retry.max_attempts",
    )
    blocked = _build(registry, withdrawn, prompts_dir)
    decided = _build(registry, decided_config, prompts_dir)
    assert blocked.candidate_hash() != decided.candidate_hash()


def test_blocking_reasons_name_every_missing_prerequisite(
    seeded, decided_config, tmp_path
):
    """沒綁定、沒 prompt 時要逐項講清楚，而不是只說一句「不能凍結」。"""
    snapshot = build_runtime_snapshot(
        seeded.registry, decided_config, prompts_dir=tmp_path / "no-prompts"
    )
    blocking = " ".join(snapshot.blocking_reasons)
    for task_code in FORMAL_TASK_CODES:
        assert f"task {task_code} has no draft binding" in blocking
    assert "prompt file" in blocking


def test_a_non_formal_provider_cannot_be_frozen(seeded, decided_config, prompts_dir):
    """stub_offline 走得完整條流程，但凍不進 formal identity。"""
    registry = seeded.registry
    for capability in (
        Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON
    ):
        registry.record_verification(
            seeded.full_model_id, ProbeResult(capability=capability, success=True)
        )
    registry.select_model(seeded.connection_id, seeded.full_model_id)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.FETCHED)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.CONNECTED)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.LOCKED)
    for task_code in FORMAL_TASK_CODES:
        registry.set_binding(
            task_code, seeded.full_model_id, required_capabilities(task_code)
        )

    snapshot = build_runtime_snapshot(
        registry, decided_config, prompts_dir=prompts_dir
    )

    assert snapshot.freezable is False
    assert "not eligible for a formal run" in " ".join(snapshot.blocking_reasons)


# ---------------------------------------------------------------------------
# 凍結與不可跳步
# ---------------------------------------------------------------------------


def test_freezing_cannot_skip_the_prerequisite_locks(
    formal_ready, decided_config, prompts_dir, tmp_path
):
    """紅線三：lock 不可跳步。llm_runtime 之前必須先有 agent_schema。"""
    registry, _, _ = formal_ready
    snapshot = _build(registry, decided_config, prompts_dir)
    assert snapshot.freezable is True

    store = LockStore(tmp_path / "freeze")
    with pytest.raises(LockOrderError, match="agent_schema"):
        freeze_runtime_snapshot(store, snapshot)


def test_freezing_succeeds_once_the_whole_chain_exists(
    formal_ready, decided_config, prompts_dir, tmp_path
):
    registry, _, _ = formal_ready
    store = LockStore(tmp_path / "freeze")
    _freeze_prerequisites(store)

    freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))

    payload = store.load("llm_runtime")
    assert set(payload["bindings"]) == set(FORMAL_TASK_CODES)
    assert payload["representation_mode"] == "FIXED_SUMMARY"
    assert payload["capability_probe_artifact_hashes"]


def test_the_lock_stores_references_not_secret_values(
    formal_ready, decided_config, prompts_dir, tmp_path
):
    registry, _, connection_id = formal_ready
    store = LockStore(tmp_path / "freeze")
    _freeze_prerequisites(store)
    freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))

    raw = (tmp_path / "freeze" / "llm_runtime.lock.json").read_text(encoding="utf-8")
    assert "env:PCMEF_TEST_KEY" in raw
    assert "sk-" not in raw


# ---------------------------------------------------------------------------
# LLM-UI-05
# ---------------------------------------------------------------------------


def test_llm_ui_05_changing_a_live_binding_does_not_move_the_frozen_hash(
    formal_ready, decided_config, prompts_dir, tmp_path, seeded
):
    """Formal snapshot 產生後修改 live binding，不改變既有 llm_runtime.lock hash。"""
    registry, _, _ = formal_ready
    store = LockStore(tmp_path / "freeze")
    _freeze_prerequisites(store)
    freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))
    frozen_hash = store.load_hash("llm_runtime")

    # 之後在 draft 上改綁到另一個 model profile。
    for capability in (
        Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON
    ):
        registry.record_verification(
            seeded.full_model_id, ProbeResult(capability=capability, success=True)
        )
    registry.select_model(seeded.connection_id, seeded.full_model_id)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.FETCHED)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.CONNECTED)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.LOCKED)
    registry.set_binding(
        "physics_agent", seeded.full_model_id,
        required_capabilities("physics_agent"), actor="tester", reason="post-freeze",
    )

    assert store.load_hash("llm_runtime") == frozen_hash
    # draft 確實變了，而且看得出來已與 active 分歧。
    redrawn = _build(registry, decided_config, prompts_dir)
    assert redrawn.candidate_hash() != frozen_hash
    assert redrawn.bindings["physics_agent"]["provider"] == "stub_offline"


def test_refreezing_with_different_content_is_refused(
    formal_ready, decided_config, prompts_dir, tmp_path, seeded
):
    """紅線三的另一半：lock 不可覆寫。"""
    registry, _, _ = formal_ready
    store = LockStore(tmp_path / "freeze")
    _freeze_prerequisites(store)
    freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))

    registry.upsert_models(
        registry.list_connections()[-1].connection_id,
        [
            ModelDescriptor(
                model_id="gemini-probe", provider="google",
                display_name="Gemini Probe", provider_revision="rev-CHANGED",
            )
        ],
    )

    with pytest.raises(LockError, match="immutable"):
        freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))


def test_refreezing_identical_content_is_idempotent(
    formal_ready, decided_config, prompts_dir, tmp_path
):
    registry, _, _ = formal_ready
    store = LockStore(tmp_path / "freeze")
    _freeze_prerequisites(store)
    first = freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))
    second = freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))
    assert first == second


# ---------------------------------------------------------------------------
# LLM-UI-06
# ---------------------------------------------------------------------------


def test_llm_ui_06_formal_resolution_ignores_a_hand_edited_binding_table(
    formal_ready, decided_config, prompts_dir, tmp_path, seeded
):
    """Formal runner 在 SQLite binding table 被手動修改後仍使用 lock 中 model identity。"""
    registry, locked_profile, _ = formal_ready
    store = LockStore(tmp_path / "freeze")
    _freeze_prerequisites(store)
    freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))
    locked_model_id = store.load("llm_runtime")["bindings"]["arbitration_agent"]["model_id"]

    # 直接改資料庫，繞過所有服務層檢查 —— 這正是這條驗收要模擬的情況。
    with registry.connect() as db:
        db.execute(
            "UPDATE llm_task_bindings SET model_profile_id = ? WHERE task_code = ?",
            (seeded.full_model_id, "arbitration_agent"),
        )
    assert registry.get_binding("arbitration_agent").model_profile_id == (
        seeded.full_model_id
    )

    resolved = resolve_formal_agent_binding(store, "arbitration_agent")

    assert resolved["model_id"] == locked_model_id == "gemini-probe"
    assert resolved["provider"] == "google"
    assert resolved["secret_ref"] == "env:PCMEF_TEST_KEY"


def test_formal_resolution_has_no_database_handle_at_all():
    """Appendix J1 把 live binding 查詢列為 formal 禁止行為。

    沒有 registry 參數，就沒有人能在這裡「順便查一下 live binding」——
    這比寫一條「請不要查」的註解可靠。
    """
    import inspect

    parameters = set(
        inspect.signature(resolve_formal_agent_binding).parameters
    )
    assert parameters == {"store", "task_code"}


def test_resolving_an_unlocked_task_fails_fast(
    formal_ready, decided_config, prompts_dir, tmp_path
):
    registry, _, _ = formal_ready
    store = LockStore(tmp_path / "freeze")
    _freeze_prerequisites(store)
    freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))

    with pytest.raises(SnapshotError, match="no binding"):
        resolve_formal_agent_binding(store, "rag_retrieval_agent")


def test_a_tampered_lock_file_is_rejected(
    formal_ready, decided_config, prompts_dir, tmp_path
):
    registry, _, _ = formal_ready
    store = LockStore(tmp_path / "freeze")
    _freeze_prerequisites(store)
    freeze_runtime_snapshot(store, _build(registry, decided_config, prompts_dir))

    path = tmp_path / "freeze" / "llm_runtime.lock.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace("gemini-probe", "some-other-model"),
        encoding="utf-8",
    )

    with pytest.raises(LockError, match="integrity check"):
        resolve_formal_agent_binding(store, "arbitration_agent")


# ---------------------------------------------------------------------------
# §47 State Invalidation
# ---------------------------------------------------------------------------


def test_state_invalidation_matches_section_47():
    """§47：MODEL_GATE_VALIDATED 之後改動 identity 屬 breaking，須作廢下游 lock。"""
    assert STATE_INVALIDATION["binding_changed_before_model_gate_validated"] == ()
    assert STATE_INVALIDATION["credential_only_rotation"] == ()
    assert STATE_INVALIDATION["draft_changed_after_formal_config_frozen"] == ()

    for event in (
        "binding_changed_after_model_gate_validated",
        "prompt_or_schema_changed_after_model_gate_validated",
        "representation_mode_changed_after_model_gate_validated",
    ):
        invalidated = STATE_INVALIDATION[event]
        assert "gate" in invalidated, event
        assert "e2_sample_size" in invalidated, event
        assert "formal_config" in invalidated, event


def test_credential_rotation_does_not_change_the_binding_identity(formal_ready):
    """§46：只換 credential 而 identity 不變時，不必 invalidate scientific lock。"""
    registry, profile_id, _ = formal_ready
    before = registry.binding_identity(profile_id)

    # 換憑證等於換 vault 內的密文；ref 與 model identity 都不動。
    after = registry.binding_identity(profile_id)

    assert before == after
    assert "secret_ref" not in before
