# PC-MEF Research System source maintenance contract
# 上下游: 由 console 各 route 與 admin.app 匯入；讀 Flask session 決定
#         目前專案，向 registry 與 resolver 取得 Project 與 ProjectPaths。
#         **不寫入任何科學資料。**
# 檔案路徑: pcmef/platform/projects/context.py
# 產生時間: 2026-09-06 15:30 +08:00
# 版本: v0.1.0
# 功能說明: 「目前在哪一個 project context」的單一解答處，以及給版型用的
#           專案切換器資料。
# 模組定位: 平台化 Phase 2。使用者第六節要求「所有 production service
#           都必須知道目前在哪個 project context」—— 本檔就是那個答案，
#           而且是唯一一個。
# 主要責任:
#   1. ProjectContext 綁定 Project 與其 ProjectPaths
#   2. current_context() 由 session 解析目前專案，找不到即回退並標記
#   3. select_project() 切換目前專案
#   4. project_switcher() 產生版型需要的切換器資料
# 維護提醒:
#   - 不得讓回退變成靜默。session 指向一個不存在或已封存的專案時，
#     必須把 `fell_back` 標出來讓畫面顯示；靜默回退會讓使用者以為
#     自己在看 A，而畫面其實是 B —— 那正是本輪要防的結果歸屬錯誤。
#   - 不得在本檔判斷 legacy 特例或自行組裝路徑。一律 resolve_paths()。
#   - 不得把 ProjectContext 快取成模組級變數。它是 per-request 的；
#     模組級快取會讓兩個同時開著不同專案的分頁互相污染。
#   - v0.1.0 新增：首版，對應平台化 Phase 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_context.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pcmef.platform.projects.models import Project, ProjectPaths
from pcmef.platform.projects.registry import ProjectNotFoundError, ProjectRegistry
from pcmef.platform.projects.resolver import resolve_paths

__all__ = [
    "PROJECT_SESSION_KEY",
    "ProjectContext",
    "current_context",
    "project_switcher",
    "select_project",
]

#: session 中記錄目前專案的鍵。
PROJECT_SESSION_KEY = "pcmef_current_project"


@dataclass(frozen=True)
class ProjectContext:
    """目前的 project context。

    `fell_back` 為真時，代表 session 指定的專案無法使用，畫面**必須**
    顯示這件事 —— 否則使用者會以為結果屬於他選的那一個專案。
    """

    project: Project
    paths: ProjectPaths
    fell_back: bool = False
    fallback_reason: str = ""

    @property
    def project_id(self) -> str:
        return self.project.project_id

    @property
    def display_name(self) -> str:
        return self.project.display_name


def _default_context(registry: ProjectRegistry) -> ProjectContext:
    """回退到 legacy Thesis Project，必要時先登記它。"""
    project = registry.ensure_legacy_thesis_project()
    return ProjectContext(
        project=project,
        paths=resolve_paths(project.project_id, root=registry.root),
    )


def current_context(
    session: Any = None, *, registry: ProjectRegistry | None = None
) -> ProjectContext:
    """解析目前的 project context。

    session 未指定、指向不存在的專案、或指向已封存的專案時，一律回退到
    legacy Thesis Project，**並把回退原因記在 context 上**。
    """
    registry = registry or ProjectRegistry()
    if session is None:
        from flask import session as flask_session

        session = flask_session

    selected = session.get(PROJECT_SESSION_KEY) if session is not None else None
    if not selected:
        return _default_context(registry)

    try:
        project = registry.get(selected)
    except (ProjectNotFoundError, ValueError) as error:
        fallback = _default_context(registry)
        return ProjectContext(
            project=fallback.project,
            paths=fallback.paths,
            fell_back=True,
            fallback_reason=(
                f"選定的專案 {selected!r} 無法載入（{type(error).__name__}），"
                f"已回到 {fallback.project.display_name}。"
            ),
        )

    if project.archived:
        fallback = _default_context(registry)
        return ProjectContext(
            project=fallback.project,
            paths=fallback.paths,
            fell_back=True,
            fallback_reason=(
                f"專案「{project.display_name}」已封存，不能作為目前專案，"
                f"已回到 {fallback.project.display_name}。"
            ),
        )

    return ProjectContext(
        project=project,
        paths=resolve_paths(project.project_id, root=registry.root),
    )


def select_project(
    session: Any, project_id: str, *, registry: ProjectRegistry | None = None
) -> Project:
    """切換目前專案。專案不存在或已封存即拒絕，不寫入 session。"""
    registry = registry or ProjectRegistry()
    project = registry.get(project_id)
    if project.archived:
        raise ProjectNotFoundError(
            f"project {project_id!r} is archived and cannot be opened"
        )
    session[PROJECT_SESSION_KEY] = project.project_id
    return project


def project_switcher(
    context: ProjectContext, *, registry: ProjectRegistry | None = None
) -> dict[str, Any]:
    """版型頂端的專案切換器資料。

    清單刻意包含目前專案本身，讓「我在哪」與「我能去哪」在同一個
    控制項裡回答，而不是分成兩個地方。
    """
    registry = registry or ProjectRegistry()
    projects = registry.list_projects()
    if not any(p.project_id == context.project_id for p in projects):
        projects = [context.project, *projects]
    return {
        "current_project": {
            "id": context.project_id,
            "name": context.display_name,
            "state": context.project.state,
            "legacy": context.project.legacy_layout,
        },
        "project_options": [
            {
                "id": p.project_id,
                "name": p.display_name,
                "state": p.state,
                "current": p.project_id == context.project_id,
            }
            for p in projects
        ],
        "project_fell_back": context.fell_back,
        "project_fallback_reason": context.fallback_reason,
    }
