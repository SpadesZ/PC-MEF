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
#   2b. POST /projects/select-profile  切換目前 Research Profile
#   3. POST /projects/create     建立新專案
#   4. POST /projects/<id>/archive  封存專案
#   4b. POST /projects/<id>/unarchive 解除封存
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
from pcmef.platform.profiles.registry import ProfileNotFoundError
from pcmef.platform.projects.resolver import ProjectIdError
from pcmef.platform.templates import apply_template, template_choices

__all__ = ["blueprint", "install_project_context", "request_context"]

blueprint = Blueprint("projects", __name__)


def _registry() -> ProjectRegistry:
    """目前 app 的專案登記處。

    root 取自 `PCMEF_WORKSPACE_ROOT`，**不得直接用預設值**：預設值是
    repo 根目錄，於是測試會在開發者真實的 projects/ 底下建立與封存專案。
    """
    from flask import current_app

    return ProjectRegistry(root=current_app.config.get("PCMEF_WORKSPACE_ROOT"))


def _discard_project(registry, project_id: str) -> None:
    """收回建立到一半的專案。**只在 rollback 路徑上使用。**

    刪除本身失敗不再拋出：那會蓋掉原本真正的失敗原因。
    """
    import shutil

    from pcmef.platform.projects.resolver import resolve_paths

    try:
        shutil.rmtree(resolve_paths(project_id, root=registry.root).metadata_root)
    except Exception:  # noqa: BLE001
        pass


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
    from dataclasses import replace as _replace

    from flask import session

    from pcmef.platform.profiles.selection import resolve_profile

    context = current_context(session, registry=_registry())
    profiles = _profile_registry()
    try:
        profiles.ensure_thesis_profile()
    except Exception:  # noqa: BLE001 - 遷移失敗不得讓整站 500
        pass
    # 重讀專案紀錄：ensure_thesis_profile() 可能剛剛才宣告 default_profile_id，
    # 而 context 裡那份是在此之前讀的。用舊的那份解析會得到「尚未選擇」，
    # 且下一次請求才會自己好起來 —— 那種時好時壞最難查。
    try:
        project = _registry().get(context.project_id)
    except Exception:  # noqa: BLE001
        project = context.project
    try:
        selected = resolve_profile(project, session, registry=profiles)
    except Exception:  # noqa: BLE001 - 解析失敗顯示「尚未選擇」，不是 500
        selected = None
    return _replace(context, project=project, selected=selected)


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
        current_profiles=_profile_registry().list_profiles(context.project_id),
        current_profile_id=context.profile_id,
        templates=template_choices(),
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
    active = getattr(context.selected, "profile", None)
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


@blueprint.post("/projects/select-profile")
def select_profile_route():
    """明確切換目前 Research Profile。

    集中在這一個 POST 入口：切換 Profile 會讓 Status、Pipeline、Run 與
    Results 全部跟著換，那是有後果的動作，不該散落在唯讀頁上。
    """
    from pcmef.platform.profiles.selection import select_profile

    target = request.form.get("profile_id", "")
    context = request_context()
    try:
        select_profile(
            session, context.project_id, target, registry=_profile_registry()
        )
    except (ProfileNotFoundError, ProjectIdError, ValueError):
        return redirect(url_for("projects.page", error="profile"))
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

    # template 只給起點：一份 DRAFT 起始 Profile 並宣告為預設。
    # 不複製任何結果或 frozen evidence。
    #
    # 套用失敗就把專案收回。留著一個沒有 Profile、沒有預設的專案，
    # 使用者會看到它出現在清單上、點進去卻什麼都不能做 ——
    # 「看似存在但不能用」比一個乾淨的失敗更難查（audit P1-3）。
    try:
        apply_template(
            template, project_id, projects=registry, profiles=_profile_registry()
        )
    except Exception:  # noqa: BLE001
        _discard_project(registry, project_id)
        return redirect(url_for("projects.page", error="create-incomplete"))
    return redirect(url_for("projects.page"))


@blueprint.post("/projects/<project_id>/archive")
def archive(project_id: str):
    try:
        _registry().archive(project_id)
    except (ProjectNotFoundError, ProjectIdError, ValueError):
        return redirect(url_for("projects.page", error="archive"))
    return redirect(url_for("projects.page"))


@blueprint.post("/projects/<project_id>/unarchive")
def unarchive(project_id: str):
    try:
        _registry().unarchive(project_id)
    except (ProjectNotFoundError, ProjectIdError, ValueError):
        return redirect(url_for("projects.page", error="unarchive"))
    return redirect(url_for("projects.page"))


@blueprint.post("/projects/<project_id>/clone")
def clone(project_id: str):
    registry = _registry()
    new_id = request.form.get("new_project_id", "").strip()
    display_name = request.form.get("new_display_name", "").strip()
    try:
        registry.clone(project_id, new_id, display_name or new_id)
        # Project clone 之後把來源的預設 Profile 也複製過去，成為新專案
        # 的 Development Profile。少了這一步，Clone 出來的專案沒有任何
        # 可看的研究設定，畫面只會說「尚未選擇」—— 那是個死胡同，
        # 而使用者剛按下的按鈕叫「建立副本」。
        _clone_default_profile(project_id, new_id, registry)
    except (ProjectNotFoundError, ProjectExistsError, ProjectIdError, ValueError):
        return redirect(url_for("projects.page", error="clone"))
    return redirect(url_for("projects.page"))


def _clone_default_profile(source_id: str, target_id: str, registry) -> None:
    """把來源專案的預設 Profile 複製成新專案的 Development Profile。

    複製的是**設計**。新 Profile 一律 DRAFT（由 profiles.clone 保證）：
    它還沒有自己的 lock，繼承 FROZEN 會讓一份空設定宣稱自己
    已建立科學身分。
    """
    profiles = _profile_registry()
    source_profile = profiles.default_profile(source_id)
    if source_profile is None:
        return
    new_profile_id = "dev-v1"
    profiles.clone(
        source_id,
        source_profile.profile_id,
        new_profile_id,
        f"{source_profile.display_name}（Development）",
        target_project_id=target_id,
    )
    registry.set_default_profile(target_id, new_profile_id)


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
