# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.profiles.registry。
#         全部在 tmp_path 上進行，**不碰真實 projects/ 或 freeze/**。
# 檔案路徑: tests/platform/test_profile_registry.py
# 產生時間: 2026-09-06 22:30 +08:00
# 版本: v0.1.0
# 功能說明: Profile 建立、清單、Clone、Research Design 讀寫與 Thesis
#           遷移的行為驗收。
# 模組定位: 平台化 Phase 3 的驗收。最重要的兩條是
#           test_a_frozen_profile_refuses_an_in_place_design_edit 與
#           test_clone_of_a_frozen_profile_starts_as_draft ——
#           它們是 SAI §23/§24 immutable 政策的守門測試。
# 主要責任:
#   1. 驗證 Profile 建立的 O_EXCL 語意
#   2. 驗證 FROZEN Profile 拒絕原地改寫設計
#   3. 驗證 Clone 產生 DRAFT 並帶走設計
#   4. 驗證 ensure_thesis_profile 冪等且建立 FROZEN 設計
#   5. 驗證 active_profile 優先 Frozen
# 維護提醒:
#   - 不得放寬 FROZEN 的原地改寫禁令。已凍結設計若能被就地改掉，
#     已發表結果對應的設計就會悄悄變成另一份。
#   - 不得讓 Profile 測試共用真實 workspace root。
#   - v0.1.0 新增：首版，對應平台化 Phase 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_profile_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.platform.profiles.design import DesignField, DesignSection, ResearchDesign
from pcmef.platform.profiles.registry import (
    THESIS_PROFILE_ID,
    FrozenProfileError,
    ProfileExistsError,
    ProfileNotFoundError,
    ProfileRegistry,
)
from pcmef.platform.projects.registry import ProjectRegistry
from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID


@pytest.fixture()
def workspace(tmp_path):
    projects = ProjectRegistry(root=tmp_path)
    projects.create("demo-project", "Demo Project")
    return ProfileRegistry(root=tmp_path)


def _design(title: str = "design") -> ResearchDesign:
    return ResearchDesign(
        source_document="plan",
        title=title,
        sections=(
            DesignSection(
                key="objective", title="O", summary="",
                fields=(DesignField("goal", title, "§1"),),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 建立與清單
# ---------------------------------------------------------------------------


def test_create_and_get_round_trip(workspace):
    created = workspace.create("demo-project", "dev-one", "Development v1")
    assert workspace.get("demo-project", "dev-one") == created
    assert created.state == "DRAFT"


def test_creating_the_same_profile_twice_is_refused(workspace):
    workspace.create("demo-project", "dev-one", "Development v1")
    with pytest.raises(ProfileExistsError):
        workspace.create("demo-project", "dev-one", "Impostor")


def test_get_on_a_missing_profile_raises(workspace):
    with pytest.raises(ProfileNotFoundError):
        workspace.get("demo-project", "never-made")


def test_profiles_are_scoped_to_their_project(workspace, tmp_path):
    """一個 Project 的 Profile 不得出現在另一個 Project 底下。"""
    ProjectRegistry(root=tmp_path).create("other-project", "Other")
    workspace.create("demo-project", "dev-one", "Development v1")

    assert [p.profile_id for p in workspace.list_profiles("demo-project")] == ["dev-one"]
    assert workspace.list_profiles("other-project") == []


def test_frozen_profiles_sort_before_development_ones(workspace):
    workspace.create("demo-project", "dev-one", "Development v1")
    workspace.create("demo-project", "frozen-one", "Frozen One", state="FROZEN")

    assert [p.profile_id for p in workspace.list_profiles("demo-project")][0] == "frozen-one"


def test_active_profile_prefers_the_frozen_one(workspace):
    workspace.create("demo-project", "dev-one", "Development v1")
    workspace.create("demo-project", "frozen-one", "Frozen One", state="FROZEN")
    assert workspace.active_profile("demo-project").profile_id == "frozen-one"


def test_active_profile_is_none_when_there_are_no_profiles(workspace):
    assert workspace.active_profile("demo-project") is None


# ---------------------------------------------------------------------------
# Research Design 與 immutability
# ---------------------------------------------------------------------------


def test_design_round_trips_through_disk(workspace):
    workspace.create("demo-project", "dev-one", "Development v1")
    workspace.write_design("demo-project", "dev-one", _design("first"))

    loaded = workspace.read_design("demo-project", "dev-one")
    assert loaded is not None
    assert loaded.title == "first"


def test_reading_a_design_that_was_never_written_returns_none(workspace):
    workspace.create("demo-project", "dev-one", "Development v1")
    assert workspace.read_design("demo-project", "dev-one") is None


def test_a_frozen_profile_refuses_an_in_place_design_edit(workspace):
    """已凍結的設計若能就地改掉，已發表結果對應的設計會悄悄變成另一份。"""
    workspace.create("demo-project", "frozen-one", "Frozen One", state="FROZEN")

    with pytest.raises(FrozenProfileError, match="cannot be edited in place"):
        workspace.write_design("demo-project", "frozen-one", _design("tampered"))


@pytest.mark.parametrize(
    "state", ["FROZEN", "FORMAL_READY", "FORMAL_RUNNING", "FORMAL_COMPLETE"]
)
def test_every_post_freeze_state_refuses_an_edit(workspace, state):
    # profile id 與 project id 共用同一組字元規則：底線不合法。
    profile_id = f"p-{state.lower().replace('_', '-')}"
    workspace.create("demo-project", profile_id, state, state=state)
    with pytest.raises(FrozenProfileError):
        workspace.write_design("demo-project", profile_id, _design())


def test_a_draft_profile_accepts_an_edit(workspace):
    workspace.create("demo-project", "dev-one", "Development v1")
    workspace.write_design("demo-project", "dev-one", _design("v1"))
    workspace.write_design("demo-project", "dev-one", _design("v2"))
    assert workspace.read_design("demo-project", "dev-one").title == "v2"


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


def test_clone_of_a_frozen_profile_starts_as_draft(workspace):
    """Clone 出來的設定還沒有自己的 lock，不得繼承 FROZEN。"""
    workspace.create("demo-project", "frozen-one", "Frozen One", state="FROZEN")
    clone = workspace.clone("demo-project", "frozen-one", "dev-two", "Development v2")

    assert clone.state == "DRAFT"
    assert clone.parent_profile_id == "frozen-one"


def test_clone_carries_the_design_across(workspace):
    workspace.create("demo-project", "dev-one", "Development v1")
    workspace.write_design("demo-project", "dev-one", _design("carried"))

    workspace.clone("demo-project", "dev-one", "dev-two", "Development v2")
    assert workspace.read_design("demo-project", "dev-two").title == "carried"


def test_the_clone_is_editable_even_though_the_source_was_frozen(workspace):
    """Clone 的用途就是讓已凍結的設計能被繼續發展。"""
    workspace.create("demo-project", "frozen-one", "Frozen One", state="FROZEN")
    workspace.clone("demo-project", "frozen-one", "dev-two", "Development v2")

    workspace.write_design("demo-project", "dev-two", _design("evolved"))
    assert workspace.read_design("demo-project", "dev-two").title == "evolved"


# ---------------------------------------------------------------------------
# Thesis 遷移
# ---------------------------------------------------------------------------


def test_thesis_profile_is_created_frozen_with_its_design(tmp_path):
    registry = ProfileRegistry(root=tmp_path)
    profile = registry.ensure_thesis_profile()

    assert profile.profile_id == THESIS_PROFILE_ID
    assert profile.project_id == LEGACY_THESIS_PROJECT_ID
    assert profile.state == "FROZEN"
    assert profile.source_document == "PC-MEF_實驗計畫_v1.2.1"

    design = registry.read_design(LEGACY_THESIS_PROJECT_ID, THESIS_PROFILE_ID)
    assert design is not None
    assert design.section("primary_endpoint") is not None


def test_thesis_migration_is_idempotent(tmp_path):
    registry = ProfileRegistry(root=tmp_path)
    assert registry.ensure_thesis_profile() == registry.ensure_thesis_profile()


def test_the_migrated_thesis_design_cannot_be_edited_in_place(tmp_path):
    registry = ProfileRegistry(root=tmp_path)
    registry.ensure_thesis_profile()

    with pytest.raises(FrozenProfileError):
        registry.write_design(
            LEGACY_THESIS_PROJECT_ID, THESIS_PROFILE_ID, _design("tampered")
        )
