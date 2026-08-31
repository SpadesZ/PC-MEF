# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；使用 tests/llm_admin/conftest.py 的臨時 registry 與
#         離線 stub adapter，驗證 pcmef.llm.registry 的寫入守衛與相依性規則；
#         不對外連線，也不觸碰真實 registry/llm_admin.db。
# 檔案路徑: tests/llm_admin/test_registry.py
# 產生時間: 2026-08-26 23:15 +08:00
# 版本: v0.1.0
# 功能說明: 驗證這份可變的操作紀錄守得住四件事 —— 明文金鑰進不來、
#           沒驗證過的能力綁不上去、還在用的東西刪不掉、以及改綁一定留下歷程。
# 模組定位: §46 Delete dependency、§44 verified-only binding 與 §49 SQLite
#           禁止事項的可執行防線。它不驗證 probe 本身是否正確。
# 主要責任:
#   1. test_migration_is_idempotent 驗證重複 migrate 不重建表
#   2. test_add_connection_rejects_* 驗證明文 key 與 session ref 都被擋
#   3. test_no_table_stores_anything_resembling_the_key 掃描整份 DB 檔
#   4. test_binding_requires_probe_verified_capabilities 驗證 declared 不算數
#   5. test_deleting_a_bound_* 驗證 409 語意與 dependency 清單正確
#   6. test_rebinding_writes_an_audit_row_with_before_and_after_hash 驗證歷程
#   7. test_verified_capabilities_follow_the_latest_probe 驗證能力會退場
# 維護提醒:
#   - 不得放寬 test_no_table_stores_anything_resembling_the_key 的片段長度；
#     只比對完整字串會讓「存了一半的 key」通過。
#   - 不得為了讓刪除測試好寫而改成軟刪除；§46 要求的是拒絕，不是標記。
#   - v0.1.0 新增：首版，對應 NOTE-018。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import sqlite3

import pytest

from pcmef.agents.provider import Capability, ProbeResult, StubOfflineAdapter
from pcmef.llm.capabilities import required_capabilities
from pcmef.llm.registry import (
    SCHEMA_VERSION,
    ConnectionStatus,
    Lifecycle,
    DependencyError,
    LLMRegistry,
    RegistryError,
)
from pcmef.secrets.vault import SecretError

STUB_PROVIDER = StubOfflineAdapter.provider


def _ok(capability: Capability) -> ProbeResult:
    return ProbeResult(capability=capability, success=True, detail="stub ok")


def _fail(capability: Capability) -> ProbeResult:
    return ProbeResult(
        capability=capability, success=False, error_sanitized="stub says no"
    )


def _verify_all(registry, model_profile_id, capabilities):
    for capability in capabilities:
        registry.record_verification(model_profile_id, _ok(capability))


def _ready(registry, connection_id, model_profile_id, capabilities):
    """驗證能力並把線路鎖定 —— 綁定的兩個前提（NOTE-023）。"""
    _verify_all(registry, model_profile_id, capabilities)
    registry.select_model(connection_id, model_profile_id)
    registry.set_lifecycle(connection_id, Lifecycle.FETCHED)
    registry.set_lifecycle(connection_id, Lifecycle.CONNECTED)
    registry.set_lifecycle(connection_id, Lifecycle.LOCKED)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_creates_all_six_tables(registry):
    with registry.connect() as db:
        names = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "llm_connections", "llm_models", "llm_task_bindings",
        "llm_verification_logs", "llm_binding_audit_logs", "llm_cache_index",
    } <= names


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "llm.db"
    first = LLMRegistry(path)
    first.add_connection("A", STUB_PROVIDER, "env:PCMEF_TEST_KEY")

    second = LLMRegistry(path)

    assert second.migrate() == SCHEMA_VERSION
    assert [c.name for c in second.list_connections()] == ["A"]


# ---------------------------------------------------------------------------
# Secret 邊界
# ---------------------------------------------------------------------------


def test_add_connection_rejects_a_plaintext_key(registry, fake_key):
    with pytest.raises(SecretError, match="plaintext API key"):
        registry.add_connection("Bad", STUB_PROVIDER, fake_key)


def test_add_connection_rejects_a_session_ref(registry):
    """session ref 重啟後必然解析不到，寫進 DB 等於製造一個壞掉的設定。"""
    with pytest.raises(RegistryError, match="process memory"):
        registry.add_connection("Bad", STUB_PROVIDER, "session:abc123")


def test_no_table_stores_anything_resembling_the_key(registry, fake_key):
    registry.add_connection("Good", STUB_PROVIDER, "env:PCMEF_TEST_KEY")

    raw = registry.path.read_bytes()

    assert fake_key.encode() not in raw
    for start in range(0, len(fake_key) - 10):
        assert fake_key[start : start + 10].encode() not in raw
    # 參考本身則必須存得下來，否則 formal 無從解析憑證。
    assert b"env:PCMEF_TEST_KEY" in raw


def test_duplicate_connection_names_are_refused(registry):
    registry.add_connection("Same", STUB_PROVIDER, "env:PCMEF_TEST_KEY")
    with pytest.raises(RegistryError, match="already exists"):
        registry.add_connection("Same", STUB_PROVIDER, "env:PCMEF_TEST_KEY")


# ---------------------------------------------------------------------------
# 能力與綁定
# ---------------------------------------------------------------------------


def test_fetching_models_records_declared_but_not_verified_capabilities(seeded):
    model = seeded.registry.get_model_profile(seeded.full_model_id)

    assert Capability.CHAT in model.declared_capabilities
    assert model.verified_capabilities == ()


def test_binding_requires_a_locked_line_before_anything_else(seeded):
    """未鎖定時先講「還沒鎖定」——那才是操作者接下來要做的事（NOTE-023）。"""
    required = required_capabilities("arbitration_agent")

    with pytest.raises(RegistryError, match="not locked"):
        seeded.registry.set_binding(
            "arbitration_agent", seeded.full_model_id, required
        )


def test_binding_requires_probe_verified_capabilities(seeded):
    """§44：provider metadata 只能作提示，不能取代正式 probe。

    先把線路鎖定但**不**驗證能力，讓拒絕理由必然來自能力而非 lifecycle ——
    否則這條測試會被外層的鎖定檢查搶答，測不到它要測的東西。
    """
    required = required_capabilities("arbitration_agent")
    registry = seeded.registry
    registry.select_model(seeded.connection_id, seeded.full_model_id)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.FETCHED)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.CONNECTED)
    registry.set_lifecycle(seeded.connection_id, Lifecycle.LOCKED)

    with pytest.raises(RegistryError, match="not probe-verified"):
        registry.set_binding("arbitration_agent", seeded.full_model_id, required)

    _verify_all(registry, seeded.full_model_id, required)
    binding = seeded.registry.set_binding(
        "arbitration_agent", seeded.full_model_id, required, actor="tester"
    )

    assert binding.model_profile_id == seeded.full_model_id
    assert binding.binding_version == 1


def test_binding_is_refused_when_the_connection_is_disabled(seeded):
    required = required_capabilities("physics_agent")
    _ready(seeded.registry, seeded.connection_id, seeded.full_model_id, required)
    seeded.registry.set_connection_enabled(seeded.connection_id, False)

    with pytest.raises(RegistryError, match="forbids binding"):
        seeded.registry.set_binding("physics_agent", seeded.full_model_id, required)


def test_verified_capabilities_follow_the_latest_probe(seeded):
    """模型被降級後 probe 會開始失敗；沿用歷史成功結果會讓綁定指著壞掉的能力。"""
    registry = seeded.registry
    registry.record_verification(seeded.full_model_id, _ok(Capability.CHAT))
    assert registry.verified_capabilities(seeded.full_model_id) == (Capability.CHAT,)

    registry.record_verification(seeded.full_model_id, _fail(Capability.CHAT))

    assert registry.verified_capabilities(seeded.full_model_id) == ()
    assert registry.recompute_verified_capabilities(seeded.full_model_id) == ()


def test_verified_cache_matches_what_the_logs_imply(seeded):
    registry = seeded.registry
    for capability in (Capability.CHAT, Capability.STRUCTURED_JSON):
        registry.record_verification(seeded.full_model_id, _ok(capability))
    registry.record_verification(seeded.full_model_id, _fail(Capability.VISION))

    assert set(registry.verified_capabilities(seeded.full_model_id)) == set(
        registry.recompute_verified_capabilities(seeded.full_model_id)
    )


def test_a_failed_probe_does_not_condemn_the_whole_connection(seeded):
    """模型缺某項能力與連線故障是兩回事。

    若在此把連線標成 degraded，之後 Bind 被拒時給出的理由會指向連線狀態，
    而真正的原因是能力不足 —— 操作者會照著錯的方向去修。
    連線層級的故障由 fetch_models 的失敗標記，見 test_admin_services.py。
    """
    seeded.registry.record_verification(seeded.full_model_id, _fail(Capability.CHAT))

    connection = seeded.registry.get_connection(seeded.connection_id)
    assert connection.status == ConnectionStatus.PENDING
    # healthy 講的是憑證與端點通不通，與「設定進度走到哪」是兩欄。
    assert connection.healthy is True
    assert connection.last_verified_at is not None


def test_a_successful_probe_marks_the_connection_active(seeded):
    seeded.registry.record_verification(seeded.full_model_id, _ok(Capability.CHAT))
    assert seeded.registry.get_connection(seeded.connection_id).status == (
        ConnectionStatus.ACTIVE
    )


# ---------------------------------------------------------------------------
# 相依性與刪除
# ---------------------------------------------------------------------------


def test_deleting_a_bound_model_profile_is_refused_with_the_dependency_list(seeded):
    """§46 Delete dependency：必須拒絕並列出 dependent tasks。"""
    registry = seeded.registry
    required = required_capabilities("physics_agent")
    _ready(registry, seeded.connection_id, seeded.full_model_id, required)
    registry.set_binding("physics_agent", seeded.full_model_id, required)
    registry.set_binding("arbitration_agent", seeded.full_model_id, required)

    with pytest.raises(DependencyError) as excinfo:
        registry.delete_model_profile(seeded.full_model_id)

    assert excinfo.value.tasks == ("arbitration_agent", "physics_agent")
    assert registry.get_model_profile(seeded.full_model_id) is not None


def test_deleting_a_bound_connection_is_refused(seeded):
    registry = seeded.registry
    required = required_capabilities("physics_agent")
    _ready(registry, seeded.connection_id, seeded.full_model_id, required)
    registry.set_binding("physics_agent", seeded.full_model_id, required)

    with pytest.raises(DependencyError) as excinfo:
        registry.delete_connection(seeded.connection_id)

    assert excinfo.value.tasks == ("physics_agent",)


def test_an_unbound_model_profile_can_be_deleted(seeded):
    seeded.registry.delete_model_profile(seeded.embed_only_id)
    remaining = {m.model_profile_id for m in seeded.registry.list_models()}
    assert seeded.embed_only_id not in remaining


def test_a_probed_but_unbound_connection_can_still_be_deleted(seeded):
    """按過 Test 之後仍然要刪得掉。

    llm_verification_logs 以 FK 指向 llm_models，因此一次 probe 就足以讓
    `DELETE FROM llm_models` 撞上外鍵。舊版沒有一併清掉 log，於是
    sqlite3.IntegrityError 一路冒到 Flask，UI 得到一個沒有任何訊息的 500 ——
    而使用者做的只是「試了連線，然後想把它刪掉」這件再正常不過的事。
    """
    registry = seeded.registry
    _verify_all(registry, seeded.full_model_id, required_capabilities("physics_agent"))
    assert registry.verification_logs(seeded.full_model_id), "前提：probe 有留下記錄"

    registry.delete_connection(seeded.connection_id)

    assert seeded.connection_id not in {
        c.connection_id for c in registry.list_connections()
    }
    assert seeded.full_model_id not in {
        m.model_profile_id for m in registry.list_models()
    }




# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_rebinding_writes_an_audit_row_with_before_and_after_hash(seeded):
    registry = seeded.registry
    required = required_capabilities("physics_agent")
    _verify_all(registry, seeded.chat_only_id, required)
    _ready(registry, seeded.connection_id, seeded.full_model_id, required)

    registry.set_binding(
        "physics_agent", seeded.full_model_id, required, actor="a", reason="first"
    )

    # 一條鎖定的線路只提供它被檢查過的那個模型，因此換模型必須先解鎖再重鎖。
    registry.set_lifecycle(seeded.connection_id, Lifecycle.CONNECTED)
    _ready(registry, seeded.connection_id, seeded.chat_only_id, required)
    binding = registry.set_binding(
        "physics_agent", seeded.chat_only_id, required, actor="b", reason="switch"
    )

    rows = registry.binding_audit_log("physics_agent")
    assert len(rows) == 2
    assert rows[0]["before_hash"] == ""
    assert rows[1]["before_hash"] == rows[0]["after_hash"]
    assert rows[1]["after_hash"] != rows[1]["before_hash"]
    assert rows[1]["actor"] == "b" and rows[1]["reason"] == "switch"
    assert binding.binding_version == 2


def test_binding_identity_excludes_the_human_readable_alias(seeded):
    """§43：name 是管理者可讀 alias，不得參與 scientific hash。"""
    registry = seeded.registry
    identity = registry.binding_identity(seeded.full_model_id)
    assert set(identity) == {
        "connection_id", "provider", "model_id", "provider_revision"
    }
    assert "Stub Formal" not in str(identity)


def test_foreign_keys_are_enforced(seeded):
    """外鍵仍然強制執行，但以 RegistryError 呈現而非原始 sqlite 例外。

    型別會變是刻意的：外鍵違反的意思是「這個操作在目前狀態下不合法」，
    不是伺服器壞掉。讓 sqlite3.IntegrityError 一路冒到 Flask，UI 只會得到
    一個沒有任何訊息的 HTTP 500 —— 實測發生過：刪除一條按過 Test 的
    connection 就是這樣，而使用者完全看不出該怎麼辦。
    訊息仍必須說得出是外鍵擋下來的。
    """
    with pytest.raises(RegistryError, match="FOREIGN KEY"):
        with seeded.registry.connect() as db:
            db.execute(
                "INSERT INTO llm_task_bindings (task_code, model_profile_id, "
                "binding_version, status, updated_at, updated_by) "
                "VALUES ('ghost_agent','no-such-profile',1,'draft','now','')"
            )
