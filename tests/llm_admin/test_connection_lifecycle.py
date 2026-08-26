# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 conftest 的臨時 registry 與離線 stub adapter
#         驅動 pcmef.llm.registry 的 Lifecycle 與 pcmef.admin.services 的
#         Fetch / Select / Test / Connect 四步；不對外連線。
# 檔案路徑: tests/llm_admin/test_connection_lifecycle.py
# 產生時間: 2026-08-27 07:20 +08:00
# 版本: v0.1.0
# 功能說明: 驗證線路的四段設定流程照 roothinks LAVA setup 的規則走 ——
#           沒抓模型不能測、沒測過不能鎖、沒鎖定不能綁、鎖定後不能偷換模型。
# 模組定位: NOTE-023 的可執行防線。它不驗證 UI 版型（那在 test_admin_page.py）。
# 主要責任:
#   1. test_transitions_* 窮舉合法與非法的狀態轉移
#   2. test_test_requires_a_selected_model 驗證未選模型不能測
#   3. test_lock_requires_a_passing_test 驗證未通過不能鎖
#   4. test_only_locked_lines_are_bindable 驗證「僅顯示 Locked」
#   5. test_a_locked_line_offers_exactly_the_model_it_was_checked_with
#   6. test_changing_the_model_drops_back_to_fetched 驗證換模型會作廢先前的 Test
#   7. test_unlocking_a_bound_line_is_refused 驗證解鎖不會留下孤兒綁定
# 維護提醒:
#   - 不得為了方便而允許 draft 直接跳到 locked；那等於把 Test 變成裝飾品。
#   - 不得讓「換了模型」保留 connected 狀態；Connect 按鈕會對著一個
#     沒測過的模型亮著，而畫面上看不出差別。
#   - v0.1.0 新增：首版，對應 NOTE-023。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_connection_lifecycle.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.admin.services import AdminServiceError
from pcmef.agents.provider import Capability
from pcmef.llm.registry import DependencyError, Lifecycle, RegistryError
from pcmef.llm.capabilities import (
    MINIMUM_BINDABLE_CAPABILITIES,
    required_capabilities,
    roles_blocked,
    roles_servable,
)


# ---------------------------------------------------------------------------
# 狀態轉移
# ---------------------------------------------------------------------------


def test_a_new_connection_starts_as_draft(seeded):
    connection = seeded.registry.get_connection(seeded.connection_id)
    assert connection.lifecycle == Lifecycle.DRAFT
    assert connection.can_fetch is True
    assert connection.can_test is False
    assert connection.can_lock is False
    assert connection.bindable is False


@pytest.mark.parametrize(
    "current, target, allowed",
    [
        (Lifecycle.DRAFT, Lifecycle.FETCHED, True),
        (Lifecycle.DRAFT, Lifecycle.CONNECTED, False),
        (Lifecycle.DRAFT, Lifecycle.LOCKED, False),
        (Lifecycle.FETCHED, Lifecycle.CONNECTED, True),
        (Lifecycle.FETCHED, Lifecycle.LOCKED, False),
        (Lifecycle.CONNECTED, Lifecycle.LOCKED, True),
        (Lifecycle.CONNECTED, Lifecycle.FETCHED, True),
        (Lifecycle.LOCKED, Lifecycle.CONNECTED, True),
        (Lifecycle.LOCKED, Lifecycle.DRAFT, False),
    ],
)
def test_transitions_follow_the_declared_table(seeded, current, target, allowed):
    """窮舉轉移表。跳步（例如 draft 直接到 locked）等於讓 Test 變成裝飾品。"""
    registry = seeded.registry
    registry.select_model(seeded.connection_id, seeded.full_model_id)
    # 沿著 draft → fetched → connected → locked 推進到 current 為止。
    order = [Lifecycle.DRAFT, Lifecycle.FETCHED, Lifecycle.CONNECTED, Lifecycle.LOCKED]
    for step in order[1 : order.index(current) + 1]:
        registry.set_lifecycle(seeded.connection_id, step)
    assert registry.get_connection(seeded.connection_id).lifecycle == current

    if allowed:
        assert registry.set_lifecycle(seeded.connection_id, target).lifecycle == target
    else:
        with pytest.raises(RegistryError, match="cannot move to"):
            registry.set_lifecycle(seeded.connection_id, target)


def test_an_unknown_lifecycle_is_refused(seeded):
    with pytest.raises(RegistryError, match="unknown lifecycle"):
        seeded.registry.set_lifecycle(seeded.connection_id, "whatever")


# ---------------------------------------------------------------------------
# 四步流程
# ---------------------------------------------------------------------------


def test_fetch_then_select_moves_to_fetched(admin_service, seeded):
    admin_service.fetch_models(seeded.connection_id)
    view = admin_service.select_model(seeded.connection_id, seeded.full_model_id)

    assert view.lifecycle == Lifecycle.FETCHED
    assert view.selected_model_id == "full-model"
    assert view.can_test is True
    assert view.can_lock is False


def test_test_requires_a_selected_model(admin_service, seeded):
    admin_service.fetch_models(seeded.connection_id)
    with pytest.raises(AdminServiceError, match="select a model first"):
        admin_service.test_connection(seeded.connection_id)


def test_a_passing_test_moves_to_connected(admin_service, seeded):
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, seeded.full_model_id)

    outcome = admin_service.test_connection(seeded.connection_id)

    assert outcome.failed() == ()
    view = next(
        v for v in admin_service.connection_views()
        if v.connection_id == seeded.connection_id
    )
    assert view.lifecycle == Lifecycle.CONNECTED
    assert view.can_lock is True


def test_a_failing_test_stays_put_and_records_why(admin_service, seeded):
    """chat-only 模型連最低要求（chat + structured_json）都不到，一個角色都服務不了。"""
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, seeded.chat_only_id)

    outcome = admin_service.test_connection(seeded.connection_id)

    assert set(outcome.failed()) == {
        Capability.STRUCTURED_JSON, Capability.VISION
    }
    assert roles_servable(outcome.verified) == ()
    view = next(
        v for v in admin_service.connection_views()
        if v.connection_id == seeded.connection_id
    )
    assert view.lifecycle == Lifecycle.FETCHED
    assert view.can_lock is False
    assert "structured_json" in view.last_error


def test_a_vision_less_model_can_still_be_locked_and_serve_two_roles(
    admin_service, seeded, stub_adapter
):
    """§45 是逐 role 的要求表：physics/arbitration 不需要 vision。

    把三項全過當成鎖定門檻，會讓一個純文字強模型永遠鎖不起來，
    連帶把它能勝任的兩個角色一併排除。
    """
    stub_adapter.catalogue["text-strong"] = (
        Capability.CHAT, Capability.STRUCTURED_JSON,
    )
    admin_service.fetch_models(seeded.connection_id)
    profile = next(
        m for m in seeded.registry.list_models(seeded.connection_id)
        if m.model_id == "text-strong"
    )
    admin_service.select_model(seeded.connection_id, profile.model_profile_id)

    outcome = admin_service.test_connection(seeded.connection_id)

    assert outcome.failed() == (Capability.VISION,)
    assert set(roles_servable(outcome.verified)) == {
        "physics_agent", "arbitration_agent"
    }
    # 關鍵：vision 失敗不擋鎖定。
    view = admin_service.lock_connection(seeded.connection_id)
    assert view.lifecycle == Lifecycle.LOCKED
    assert set(view.servable_roles) == {"physics_agent", "arbitration_agent"}

    # 而 vision 的缺口在 task 層擋下，不在線路層。
    assert admin_service.bind_task("physics_agent", profile.model_profile_id)
    assert admin_service.bind_task("arbitration_agent", profile.model_profile_id)
    with pytest.raises(RegistryError, match="not probe-verified"):
        admin_service.bind_task("observation_agent", profile.model_profile_id)


def test_the_minimum_bindable_set_is_derived_not_hand_written():
    """改了 §45 的能力矩陣，下限要自動跟著變。"""
    assert MINIMUM_BINDABLE_CAPABILITIES == {
        Capability.CHAT, Capability.STRUCTURED_JSON
    }
    assert Capability.VISION not in MINIMUM_BINDABLE_CAPABILITIES
    # 下限必須真的是某個 role 的完整要求，而不是隨手取的交集。
    assert any(
        set(required_capabilities(task)) == MINIMUM_BINDABLE_CAPABILITIES
        for task in ("physics_agent", "arbitration_agent")
    )


def test_roles_blocked_names_the_missing_capability(seeded):
    blocked = roles_blocked((Capability.CHAT, Capability.STRUCTURED_JSON))
    assert set(blocked) == {"observation_agent", "visual_semantic_agent"}
    assert all(missing == (Capability.VISION,) for missing in blocked.values())


def test_lock_requires_a_passing_test(admin_service, seeded):
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, seeded.full_model_id)

    with pytest.raises(AdminServiceError, match="run Test successfully"):
        admin_service.lock_connection(seeded.connection_id)


def test_locking_freezes_the_line(admin_service, seeded):
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, seeded.full_model_id)
    admin_service.test_connection(seeded.connection_id)

    view = admin_service.lock_connection(seeded.connection_id)

    assert view.lifecycle == Lifecycle.LOCKED
    assert view.can_edit is False
    assert view.can_fetch is False
    assert view.can_test is False
    assert view.bindable is True


def test_changing_the_model_drops_back_to_fetched(admin_service, seeded):
    """換了模型就等於這條線路還沒被測過。"""
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, seeded.full_model_id)
    admin_service.test_connection(seeded.connection_id)

    view = admin_service.select_model(seeded.connection_id, seeded.chat_only_id)

    assert view.lifecycle == Lifecycle.FETCHED
    assert view.can_lock is False


def test_a_locked_line_refuses_a_model_change(admin_service, seeded):
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, seeded.full_model_id)
    admin_service.test_connection(seeded.connection_id)
    admin_service.lock_connection(seeded.connection_id)

    with pytest.raises(RegistryError, match="locked"):
        admin_service.select_model(seeded.connection_id, seeded.chat_only_id)


def test_a_locked_line_refuses_a_refetch(admin_service, seeded):
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, seeded.full_model_id)
    admin_service.test_connection(seeded.connection_id)
    admin_service.lock_connection(seeded.connection_id)

    with pytest.raises(AdminServiceError, match="locked"):
        admin_service.fetch_models(seeded.connection_id)


# ---------------------------------------------------------------------------
# 只有 Locked 能綁
# ---------------------------------------------------------------------------


def _walk_to_locked(admin_service, seeded, model_profile_id):
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, model_profile_id)
    admin_service.test_connection(seeded.connection_id)
    return admin_service.lock_connection(seeded.connection_id)


def test_only_locked_lines_are_bindable(admin_service, seeded):
    """roothinks LAVA 的 `僅顯示 Locked`。"""
    admin_service.fetch_models(seeded.connection_id)
    admin_service.select_model(seeded.connection_id, seeded.full_model_id)
    admin_service.test_connection(seeded.connection_id)

    before = {v.task_code: v.options for v in admin_service.binding_views()}
    assert all(options == () for options in before.values())

    admin_service.lock_connection(seeded.connection_id)

    after = {v.task_code: v.options for v in admin_service.binding_views()}
    assert all(len(options) == 1 for options in after.values())


def test_a_locked_line_offers_exactly_the_model_it_was_checked_with(
    admin_service, seeded
):
    """同一把 key 底下其他沒測過的模型不算數。"""
    _walk_to_locked(admin_service, seeded, seeded.full_model_id)

    offered = {
        option.model_profile_id
        for view in admin_service.binding_views()
        for option in view.options
    }

    assert offered == {seeded.full_model_id}
    assert seeded.chat_only_id not in offered
    assert seeded.embed_only_id not in offered


def test_binding_a_non_selected_model_of_a_locked_line_is_refused(
    admin_service, seeded
):
    _walk_to_locked(admin_service, seeded, seeded.full_model_id)
    with pytest.raises(RegistryError, match="different model"):
        admin_service.bind_task("physics_agent", seeded.chat_only_id)


def test_unlocking_a_bound_line_is_refused(admin_service, seeded):
    """解鎖若放行，會留下一個綁著未鎖線路的 task。"""
    _walk_to_locked(admin_service, seeded, seeded.full_model_id)
    admin_service.bind_task("physics_agent", seeded.full_model_id)

    with pytest.raises(DependencyError) as excinfo:
        admin_service.unlock_connection(seeded.connection_id)

    assert excinfo.value.tasks == ("physics_agent",)


def test_unlocking_an_unbound_line_returns_to_connected(admin_service, seeded):
    _walk_to_locked(admin_service, seeded, seeded.full_model_id)
    view = admin_service.unlock_connection(seeded.connection_id)
    assert view.lifecycle == Lifecycle.CONNECTED
    assert view.can_edit is True


# ---------------------------------------------------------------------------
# draft binding 的確認鎖
# ---------------------------------------------------------------------------


def test_a_locked_binding_cannot_be_rebound(admin_service, seeded):
    _walk_to_locked(admin_service, seeded, seeded.full_model_id)
    admin_service.bind_task("physics_agent", seeded.full_model_id)
    admin_service.set_binding_locked("physics_agent", True)

    with pytest.raises(RegistryError, match="binding for 'physics_agent' is locked"):
        admin_service.bind_task("physics_agent", seeded.full_model_id)

    admin_service.set_binding_locked("physics_agent", False)
    assert admin_service.bind_task("physics_agent", seeded.full_model_id)


def test_binding_lock_is_draft_level_not_a_formal_lock(admin_service, seeded, tmp_path):
    """draft 的確認鎖不得產生任何 formal identity（§52 結語）。"""
    _walk_to_locked(admin_service, seeded, seeded.full_model_id)
    admin_service.bind_task("arbitration_agent", seeded.full_model_id)
    admin_service.set_binding_locked("arbitration_agent", True)

    assert not (tmp_path / "freeze" / "llm_runtime.lock.json").exists()
    assert admin_service.snapshot_view().resolved_hash == ""
