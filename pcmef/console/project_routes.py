# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.app 註冊；讀寫 platform.projects.registry 與
#         context。提供 Workspace 專案清單、切換、建立、封存與 Clone。
#         **不觸發任何研究執行。**
# 檔案路徑: pcmef/console/project_routes.py
# 產生時間: 2026-09-06 16:30 +08:00
# 版本: v0.1.0
# 功能說明: Workspace 層的專案入口，以及供全站版型使用的 context processor。
# 模組定位: 平台化 Phase 1 的前端落點。Project 是五個主入口**之上**的一層，
#           因此它不進導航列，而是由版型頂端的切換器呈現。
# 主要責任:
#   1. GET  /projects            專案清單與切換器
#   1b. GET /projects/design     目前專案的 Research Design（唯讀）
#   2. POST /projects/select     切換目前專案
#   3. POST /projects/create     建立新專案
#   4. POST /projects/<id>/archive  封存專案
#   5. POST /projects/<id>/clone    Clone 專案（不帶科學結果）
#   6. install_project_context() 讓每一頁都拿得到目前專案
# 維護提醒:
#   - 不得在本檔加入任何啟動研究執行的端點。專案層只管「在哪個專案」，
#     准駁與啟動一律回 Run，由 CLI 子行程判定（AMD-008）。
#   - 不得讓 context processor 在失敗時靜默回退。回退原因必須傳到版型，
#     否則使用者會以為自己在看 A，而畫面其實是 B。
#   - 不得為了少一次查詢就把目前專案快取成模組級變數；那會讓兩個
#     開著不同專案的分頁互相污染。
#   - v0.1.0 新增：首版，對應平台化 Phase 1。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_project_routes.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from pcmef.console.navigation import breadcrumb, nav_context
from pcmef.platform.projects.context import current_context, project_switcher
from pcmef.platform.projects.registry import (
    ProjectExistsError,
    ProjectNotFoundError,
    ProjectRegistry,
)
from pcmef.platform.projects.resolver import ProjectIdError

__all__ = ["blueprint", "install_project_context", "request_context"]

blueprint = Blueprint("projects", __name__)


def _registry() -> ProjectRegistry:
    """目前 app 的專案登記處。

    root 取自 `PCMEF_WORKSPACE_ROOT`，**不得直接用預設值**：預設值是
    repo 根目錄，於是測試會在開發者真實的 projects/ 底下建立與封存專案。
    """
    from flask import current_app

    return ProjectRegistry(root=current_app.config.get("PCMEF_WORKSPACE_ROOT"))


def _profile_registry():
    from flask import current_app

    from pcmef.platform.profiles.registry import ProfileRegistry

    return ProfileRegistry(root=current_app.config.get("PCMEF_WORKSPACE_ROOT"))


def request_context():
    """目前 request 的 project context。

    所有 route 一律經由這裡取得目前專案 —— 各自 new 一個 ProjectRegistry()
    會繞過 app 設定的 workspace root。

    順手把 Thesis 的 Frozen Profile 補上：它與 legacy 專案的登記是同一件
    遷移，分成兩個入口會出現「專案在、但研究設計不在」的中間狀態，
    而那個狀態在畫面上看起來像「這個研究還沒有設計」。
    """
    from flask import session

    context = current_context(session, registry=_registry())
    try:
        _profile_registry().ensure_thesis_profile()
    except Exception:  # noqa: BLE001 - 遷移失敗不得讓整站 500
        pass
    return context


@blueprint.get("/projects")
def page():
    """Workspace：有哪些專案、現在在哪一個。"""
    registry = _registry()
    context = request_context()
    return render_template(
        "projects.html",
        **nav_context("run"),
        breadcrumb=breadcrumb(("專案 Projects", None)),
        projects=registry.list_projects(include_archived=True),
        current_project_id=context.project_id,
        paths=context.paths.as_dict(),
    )


@blueprint.get("/projects/design")
def design():
    """目前專案的 Research Design（唯讀）。

    內容出自該 Profile 的 research_design.json，其來源是實驗計畫書；
    畫面上每一欄都標出處，並在有對應 lock 時標出「可執行真相在哪」。
    """
    context = request_context()
    profiles = _profile_registry()
    active = profiles.active_profile(context.project_id)
    research_design = (
        profiles.read_design(context.project_id, active.profile_id)
        if active is not None
        else None
    )
    return render_template(
        "research_design.html",
        **nav_context("status"),
        breadcrumb=breadcrumb(
            ("研究設計 Research Design", None), project_name=context.display_name
        ),
        profile=active,
        all_profiles=profiles.list_profiles(context.project_id),
        design=research_design,
        sections=research_design.ordered_sections() if research_design else [],
    )


@blueprint.post("/projects/select")
def select():
    registry = _registry()
    target = request.form.get("project_id", "")
    try:
        from pcmef.platform.projects.context import select_project

        select_project(session, target, registry=registry)
    except (ProjectNotFoundError, ProjectIdError, ValueError):
        # 切換失敗時不改 session。使用者仍留在原專案，
        # 而不是被靜默丟到某個「預設」專案。
        return redirect(url_for("projects.page", error="select"))
    return redirect(url_for("projects.page"))


@blueprint.post("/projects/create")
def create():
    registry = _registry()
    project_id = request.form.get("project_id", "").strip()
    display_name = request.form.get("display_name", "").strip()
    template = request.form.get("template", "blank").strip() or "blank"
    try:
        registry.create(project_id, display_name or project_id, template=template)
    except (ProjectIdError, ProjectExistsError, ValueError):
        return redirect(url_for("projects.page", error="create"))
    return redirect(url_for("projects.page"))


@blueprint.post("/projects/<project_id>/archive")
def archive(project_id: str):
    try:
        _registry().archive(project_id)
    except (ProjectNotFoundError, ProjectIdError, ValueError):
        return redirect(url_for("projects.page", error="archive"))
    return redirect(url_for("projects.page"))


@blueprint.post("/projects/<project_id>/clone")
def clone(project_id: str):
    registry = _registry()
    new_id = request.form.get("new_project_id", "").strip()
    display_name = request.form.get("new_display_name", "").strip()
    try:
        registry.clone(project_id, new_id, display_name or new_id)
    except (ProjectNotFoundError, ProjectExistsError, ProjectIdError, ValueError):
        return redirect(url_for("projects.page", error="clone"))
    return redirect(url_for("projects.page"))


def install_project_context(app: Any) -> None:
    """讓每一頁的版型都拿得到目前專案與切換器。

    用 context processor 而不是要求每個 route 自己傳：漏傳的那一頁
    會少掉專案標示，而「少一個標示」在畫面上看起來只是比較樸素，
    不像錯誤 —— 那正是最容易看錯專案的情況。
    """

    @app.context_processor
    def _inject_project() -> dict[str, Any]:
        try:
            registry = _registry()
            context = request_context()
            data = project_switcher(context, registry=registry)
            data["current_project_paths"] = context.paths.as_dict()
            return data
        except Exception as error:  # noqa: BLE001 - 版型不得因此 500
            return {
                "current_project": None,
                "project_options": [],
                "project_fell_back": True,
                "project_fallback_reason": f"{type(error).__name__}: {error}",
                "current_project_paths": {},
            }
