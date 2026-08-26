# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；直接呼叫 pcmef.admin.services.AdminService，
#         背後是 conftest 的臨時 registry / vault / 離線 stub adapter；
#         不經過 HTTP 層，也不對外連線。
# 檔案路徑: tests/llm_admin/test_admin_services.py
# 產生時間: 2026-08-27 03:30 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 UI 與 CLI 共用的那一層守得住 secret 邊界 —— 憑證只以指紋外露、
#           換 key 不換身分、手動加入的模型不會自帶能力宣稱，
#           以及連線層級的故障與模型能力不足被分開標記。
# 模組定位: §46 與 §43 在服務層的可執行防線。它不驗證 HTTP 狀態碼（那在
#           test_admin_page.py），也不驗證快照語意（那在 test_snapshot.py）。
# 主要責任:
#   1. test_add_connection_requires_exactly_one_credential_source
#   2. test_no_view_ever_carries_the_secret_value 掃描所有 *View 的欄位
#   3. test_fetch_models_marks_the_connection_error_when_the_provider_fails
#   4. test_manual_model_declares_no_capabilities 對應 §42 的 Manual Model ID
#   5. test_rotate_secret_keeps_the_reference_and_the_binding_identity
#   6. test_why_not_bindable_names_the_actual_reason 驗證拒絕理由可讀
# 維護提醒:
#   - 不得為了讓 UI 顯示方便而在任何 *View 加入可還原 secret 的欄位。
#   - v0.1.0 新增：首版，對應 NOTE-019。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_admin_services.py -v
# ------------------------------------------------------------

from __future__ import annotations

import dataclasses

import pytest

from pcmef.admin.services import UNRESOLVABLE_FINGERPRINT, AdminServiceError
from pcmef.agents.provider import Capability, ProbeResult, ProviderError
from pcmef.llm.registry import ConnectionStatus


def _verify_all(registry, model_profile_id):
    for capability in (
        Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON
    ):
        registry.record_verification(
            model_profile_id, ProbeResult(capability=capability, success=True)
        )


# ---------------------------------------------------------------------------
# 憑證來源
# ---------------------------------------------------------------------------


def test_add_connection_requires_exactly_one_credential_source(admin_service, fake_key):
    with pytest.raises(AdminServiceError, match="exactly one"):
        admin_service.add_connection(name="A", provider="google")
    with pytest.raises(AdminServiceError, match="exactly one"):
        admin_service.add_connection(
            name="A", provider="google",
            api_key=fake_key, secret_ref="env:PCMEF_TEST_KEY",
        )


def test_an_api_key_becomes_a_vault_reference(admin_service):
    view = admin_service.add_connection(
        name="Vaulted", provider="google", api_key="sk-" + "VaultedKeyBits00" * 2
    )
    assert view.secret_ref.startswith("vault:")
    assert view.secret_fingerprint.startswith("****")


def test_no_view_ever_carries_the_secret_value(admin_service, fake_key):
    """掃過每個 *View 的所有欄位，確認沒有任何一格是 secret 本身。"""
    views = admin_service.connection_views()
    assert views, "the seeded fixture should have produced a connection"
    for view in views:
        for value in dataclasses.asdict(view).values():
            assert fake_key not in str(value)


def test_an_unresolvable_credential_is_shown_rather_than_crashing_the_page(
    admin_service
):
    """列表正是用來發現「設定了但解不開」的畫面，不該因此整頁掛掉。"""
    admin_service.add_connection(
        name="Missing Env", provider="google", secret_ref="env:NEVER_SET_THIS"
    )
    shown = {v.name: v.secret_fingerprint for v in admin_service.connection_views()}
    assert shown["Missing Env"] == UNRESOLVABLE_FINGERPRINT


# ---------------------------------------------------------------------------
# 連線故障 vs 能力不足
# ---------------------------------------------------------------------------


def test_fetch_models_marks_the_connection_error_when_the_provider_fails(
    seeded, vault, decided_config, tmp_path
):
    """連線層級的故障只有 list_models 判定得出來，因此 error 狀態只在那裡標。"""
    from pcmef.admin.services import AdminService

    def exploding_factory(provider, resolve_secret):
        class Boom:
            def list_models(self, connection):
                raise ProviderError("google: HTTP 401")

        return Boom()

    service = AdminService(
        registry=seeded.registry, vault=vault, adapter_factory=exploding_factory,
        config=decided_config, freeze_dir=tmp_path / "freeze",
    )

    with pytest.raises(ProviderError):
        service.fetch_models(seeded.connection_id)

    connection = seeded.registry.get_connection(seeded.connection_id)
    assert connection.status == ConnectionStatus.ERROR
    assert connection.bindable is False


def test_a_capability_gap_leaves_the_connection_usable(admin_service, seeded):
    """chat-only 模型缺 structured_json，但那不是連線的問題。"""
    admin_service.verify_model(seeded.chat_only_id)

    connection = seeded.registry.get_connection(seeded.connection_id)
    assert connection.status == ConnectionStatus.ACTIVE
    # healthy 是連線本身通不通；bindable 另外還要求已 Locked（NOTE-023）。
    assert connection.healthy is True


# ---------------------------------------------------------------------------
# Manual model 與 rotation
# ---------------------------------------------------------------------------


def test_manual_model_declares_no_capabilities(admin_service, seeded):
    """§42 區塊 A 允許 Manual Model ID，但手打一個名字不構成任何能力宣稱。"""
    view = admin_service.add_manual_model(seeded.connection_id, "hand-typed-model")

    assert view.model_id == "hand-typed-model"
    assert view.declared == ()
    assert view.verified == ()


def test_rotate_secret_keeps_the_reference_and_the_binding_identity(admin_service):
    """§46：只換 credential 而 identity 不變時，不必 invalidate scientific lock。"""
    original = admin_service.add_connection(
        name="Rotatable", provider="google", api_key="sk-" + "OriginalKeyBits0" * 2
    )

    rotated = admin_service.rotate_secret(
        original.connection_id, "sk-" + "ReplacementKey00" * 2
    )

    assert rotated.secret_ref == original.secret_ref
    assert rotated.secret_fingerprint != original.secret_fingerprint
    assert admin_service.vault.secret_version(rotated.secret_ref) == 2


def test_rotating_an_env_backed_connection_is_refused(admin_service, seeded):
    with pytest.raises(AdminServiceError, match="environment variable"):
        admin_service.rotate_secret(seeded.connection_id, "sk-" + "Nope0" * 4)


# ---------------------------------------------------------------------------
# 拒絕理由
# ---------------------------------------------------------------------------


def test_why_not_bindable_names_the_actual_reason(admin_service, seeded):
    reasons = admin_service.why_not_bindable("arbitration_agent", seeded.chat_only_id)
    assert any("structured_json" in reason for reason in reasons)

    _verify_all(seeded.registry, seeded.full_model_id)
    assert admin_service.why_not_bindable(
        "arbitration_agent", seeded.full_model_id
    ) == ["connection is not locked (draft)"]


def test_why_not_bindable_reports_a_disabled_connection_separately(
    admin_service, seeded
):
    _verify_all(seeded.registry, seeded.full_model_id)
    admin_service.set_enabled(seeded.connection_id, False)

    reasons = admin_service.why_not_bindable("physics_agent", seeded.full_model_id)

    assert any("enabled=False" in reason for reason in reasons)


def test_binding_an_unknown_task_code_is_refused(admin_service, seeded):
    """Appendix J3：額外的 task 未經核定不得默默進入 formal。"""
    _verify_all(seeded.registry, seeded.full_model_id)
    with pytest.raises(AdminServiceError, match="unknown task_code"):
        admin_service.bind_task("rag_retrieval_agent", seeded.full_model_id)
