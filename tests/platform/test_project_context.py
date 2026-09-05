# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.projects.context 的解析與回退。
#         以 dict 代替 Flask session，**不啟動 app、不碰真實 projects/**。
# 檔案路徑: tests/platform/test_project_context.py
# 產生時間: 2026-09-06 15:45 +08:00
# 版本: v0.1.0
# 功能說明: 目前專案的解析、失效回退、切換拒絕條件與切換器資料驗收。
# 模組定位: 平台化 Phase 2 的驗收。重點在「回退必須看得見」——
#           使用者第十三節要求避免「以為在看 A、其實資料來自 B」。
# 主要責任:
#   1. 驗證未選擇時回退到 legacy Thesis Project
#   2. 驗證選定專案可被正確解析
#   3. 驗證指向不存在／已封存專案時回退並標記 fell_back
#   4. 驗證 select_project 拒絕不存在與已封存的專案
#   5. 驗證 project_switcher 一定包含目前專案
# 維護提醒:
#   - 不得把回退測試改成「靜默回退也可接受」。fell_back 旗標是這一層
#     唯一能讓錯誤歸屬浮上畫面的機制。
#   - v0.1.0 新增：首版，對應平台化 Phase 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_context.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.platform.projects.context import (
    PROJECT_SESSION_KEY,
    current_context,
    project_switcher,
    select_project,
)
from pcmef.platform.projects.registry import ProjectNotFoundError, ProjectRegistry
from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID


@pytest.fixture()
def registry(tmp_path):
    return ProjectRegistry(root=tmp_path)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_no_selection_falls_back_to_the_legacy_thesis_project(registry):
    context = current_context({}, registry=registry)
    assert context.project_id == LEGACY_THESIS_PROJECT_ID
    assert context.fell_back is False, "an unset selection is not a failure"


def test_a_selected_project_is_resolved(registry):
    registry.create("sand-extension", "Sand Extension")
    session = {PROJECT_SESSION_KEY: "sand-extension"}

    context = current_context(session, registry=registry)
    assert context.project_id == "sand-extension"
    assert context.display_name == "Sand Extension"
    assert context.fell_back is False


def test_the_context_carries_that_projects_paths(registry, tmp_path):
    registry.create("sand-extension", "Sand Extension")
    context = current_context(
        {PROJECT_SESSION_KEY: "sand-extension"}, registry=registry
    )
    assert context.paths.freeze == tmp_path / "projects" / "sand-extension" / "freeze"


# ---------------------------------------------------------------------------
# 回退必須看得見
# ---------------------------------------------------------------------------


def test_a_missing_project_falls_back_visibly(registry):
    context = current_context({PROJECT_SESSION_KEY: "gone-away"}, registry=registry)

    assert context.project_id == LEGACY_THESIS_PROJECT_ID
    assert context.fell_back is True
    assert "gone-away" in context.fallback_reason, (
        "the reason must name the project that failed, otherwise the user "
        "cannot tell which selection was dropped"
    )


def test_an_archived_project_falls_back_visibly(registry):
    registry.create("archived-one", "Archived One")
    registry.archive("archived-one")

    context = current_context(
        {PROJECT_SESSION_KEY: "archived-one"}, registry=registry
    )
    assert context.project_id == LEGACY_THESIS_PROJECT_ID
    assert context.fell_back is True
    assert "Archived One" in context.fallback_reason


def test_an_illegal_project_id_in_session_falls_back_instead_of_raising(registry):
    """session 被竄改成 `../../etc` 時不得爆掉，也不得照著解析。"""
    context = current_context({PROJECT_SESSION_KEY: "../../etc"}, registry=registry)
    assert context.project_id == LEGACY_THESIS_PROJECT_ID
    assert context.fell_back is True


# ---------------------------------------------------------------------------
# 切換
# ---------------------------------------------------------------------------


def test_select_project_writes_the_session(registry):
    registry.create("target-project", "Target")
    session: dict = {}

    select_project(session, "target-project", registry=registry)
    assert session[PROJECT_SESSION_KEY] == "target-project"


def test_select_refuses_a_missing_project_and_leaves_the_session_alone(registry):
    session = {PROJECT_SESSION_KEY: "unchanged"}
    with pytest.raises(ProjectNotFoundError):
        select_project(session, "not-there", registry=registry)
    assert session[PROJECT_SESSION_KEY] == "unchanged"


def test_select_refuses_an_archived_project(registry):
    registry.create("archived-target", "Archived Target")
    registry.archive("archived-target")
    session: dict = {}

    with pytest.raises(ProjectNotFoundError, match="archived"):
        select_project(session, "archived-target", registry=registry)
    assert PROJECT_SESSION_KEY not in session


# ---------------------------------------------------------------------------
# 切換器
# ---------------------------------------------------------------------------


def test_switcher_marks_exactly_one_current_option(registry):
    registry.create("alpha-project", "Alpha")
    registry.create("beta-project", "Beta")
    context = current_context({PROJECT_SESSION_KEY: "beta-project"}, registry=registry)

    switcher = project_switcher(context, registry=registry)
    current = [o for o in switcher["project_options"] if o["current"]]
    assert len(current) == 1
    assert current[0]["id"] == "beta-project"


def test_switcher_always_contains_the_current_project(registry):
    """legacy 專案剛遷移時可能還不在清單裡，切換器仍必須顯示它。"""
    context = current_context({}, registry=registry)
    switcher = project_switcher(context, registry=registry)
    ids = {o["id"] for o in switcher["project_options"]}
    assert context.project_id in ids


def test_switcher_surfaces_the_fallback_reason(registry):
    context = current_context({PROJECT_SESSION_KEY: "vanished"}, registry=registry)
    switcher = project_switcher(context, registry=registry)
    assert switcher["project_fell_back"] is True
    assert switcher["project_fallback_reason"]
