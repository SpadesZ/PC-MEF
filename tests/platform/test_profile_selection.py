# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.profiles.selection。
#         以 dict 代替 Flask session，**不啟動 app、不碰真實 projects/**。
# 檔案路徑: tests/platform/test_profile_selection.py
# 產生時間: 2026-09-07 09:40 +08:00
# 版本: v0.1.0
# 功能說明: 「目前正在看哪一份 Research Profile」的解析、切換與空狀態驗收。
# 模組定位: 平台化 Phase 4 前置的驗收。最重要的一條是
#           test_having_profiles_does_not_imply_having_a_selection ——
#           它擋的是「隨便挑第一筆當成正式設定」。
# 主要責任:
#   1. 驗證未宣告預設時回報「尚未選擇」而非猜一份
#   2. 驗證明確宣告的 default 會被採用
#   3. 驗證 session 的選擇優先於 default
#   4. 驗證每個 Project 各自記住自己的選擇
#   5. 驗證切換不存在的 Profile 會被拒絕且不改 session
# 維護提醒:
#   - 不得把「尚未選擇」改成回退到 profiles[0]。那會讓畫面上出現一份
#     使用者沒選過、卻看起來像正式設定的研究設計。
#   - v0.1.0 新增：首版，對應平台化 Phase 4 前置。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_profile_selection.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.platform.profiles.registry import ProfileNotFoundError, ProfileRegistry
from pcmef.platform.profiles.selection import (
    PROFILE_SESSION_KEY,
    resolve_profile,
    select_profile,
)
from pcmef.platform.projects.registry import ProjectRegistry


@pytest.fixture()
def setup(tmp_path):
    projects = ProjectRegistry(root=tmp_path)
    profiles = ProfileRegistry(root=tmp_path)
    projects.create("demo-project", "Demo Project")
    profiles.create("demo-project", "dev-one", "Development v1")
    profiles.create("demo-project", "frozen-one", "Frozen One", state="FROZEN")
    return projects, profiles


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_having_profiles_does_not_imply_having_a_selection(setup):
    """有 Profile 不等於選了 Profile。**不得猜一份。**"""
    projects, profiles = setup
    selected = resolve_profile(projects.get("demo-project"), {}, registry=profiles)

    assert selected.profile is None
    assert selected.is_selected is False
    assert "尚未選擇" in selected.reason


def test_a_project_with_no_profiles_says_so(tmp_path):
    projects = ProjectRegistry(root=tmp_path)
    profiles = ProfileRegistry(root=tmp_path)
    projects.create("empty-project", "Empty Project")

    selected = resolve_profile(projects.get("empty-project"), {}, registry=profiles)
    assert selected.profile is None
    assert "尚未建立" in selected.reason


def test_the_declared_default_is_used(setup):
    projects, profiles = setup
    projects.set_default_profile("demo-project", "frozen-one")

    selected = resolve_profile(projects.get("demo-project"), {}, registry=profiles)
    assert selected.profile_id == "frozen-one"
    assert selected.origin == "project-default"


def test_the_session_choice_beats_the_default(setup):
    projects, profiles = setup
    projects.set_default_profile("demo-project", "frozen-one")
    session = {PROFILE_SESSION_KEY: {"demo-project": "dev-one"}}

    selected = resolve_profile(projects.get("demo-project"), session, registry=profiles)
    assert selected.profile_id == "dev-one"
    assert selected.origin == "session"


def test_a_stale_session_choice_falls_back_to_the_default(setup):
    """選過的 Profile 被刪掉時回到 default，而不是整頁壞掉。"""
    projects, profiles = setup
    projects.set_default_profile("demo-project", "frozen-one")
    session = {PROFILE_SESSION_KEY: {"demo-project": "deleted-one"}}

    selected = resolve_profile(projects.get("demo-project"), session, registry=profiles)
    assert selected.profile_id == "frozen-one"


def test_a_declared_default_that_does_not_exist_is_reported(setup):
    """宣告了預設卻讀不到，必須說出來，不得靜默改用別份。"""
    projects, profiles = setup
    projects.set_default_profile("demo-project", "ghost-profile")

    selected = resolve_profile(projects.get("demo-project"), {}, registry=profiles)
    assert selected.profile is None
    assert "ghost-profile" in selected.reason


# ---------------------------------------------------------------------------
# 切換
# ---------------------------------------------------------------------------


def test_select_records_the_choice(setup):
    _, profiles = setup
    session: dict = {}
    select_profile(session, "demo-project", "dev-one", registry=profiles)
    assert session[PROFILE_SESSION_KEY]["demo-project"] == "dev-one"


def test_each_project_remembers_its_own_choice(tmp_path):
    """切到 B 再切回 A 時，A 原本看的那一份應該還在。"""
    projects = ProjectRegistry(root=tmp_path)
    profiles = ProfileRegistry(root=tmp_path)
    for pid in ("project-a", "project-b"):
        projects.create(pid, pid)
        profiles.create(pid, "p-one", "One")
        profiles.create(pid, "p-two", "Two")

    session: dict = {}
    select_profile(session, "project-a", "p-two", registry=profiles)
    select_profile(session, "project-b", "p-one", registry=profiles)

    assert resolve_profile(
        projects.get("project-a"), session, registry=profiles
    ).profile_id == "p-two"
    assert resolve_profile(
        projects.get("project-b"), session, registry=profiles
    ).profile_id == "p-one"


def test_selecting_a_missing_profile_is_refused_and_leaves_the_session_alone(setup):
    _, profiles = setup
    session = {PROFILE_SESSION_KEY: {"demo-project": "dev-one"}}

    with pytest.raises(ProfileNotFoundError):
        select_profile(session, "demo-project", "not-there", registry=profiles)
    assert session[PROFILE_SESSION_KEY]["demo-project"] == "dev-one"


def test_a_selection_in_one_project_does_not_leak_into_another(tmp_path):
    projects = ProjectRegistry(root=tmp_path)
    profiles = ProfileRegistry(root=tmp_path)
    projects.create("project-a", "A")
    projects.create("project-b", "B")
    profiles.create("project-a", "only-a", "Only A")

    session: dict = {}
    select_profile(session, "project-a", "only-a", registry=profiles)

    assert resolve_profile(
        projects.get("project-b"), session, registry=profiles
    ).profile is None
