# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 操作 /projects 系列端點。
#         一律以 tmp_path 當 workspace root，**不碰真實 projects/ 或 freeze/**。
# 檔案路徑: tests/console/test_project_routes.py
# 產生時間: 2026-09-06 17:40 +08:00
# 版本: v0.1.0
# 功能說明: Workspace 專案入口的路由驗收：清單、切換、建立、封存、Clone，
#           以及切換之後 Status 是否真的換了一組資料。
# 模組定位: 平台化 Phase 1/2 的端到端驗收。最重要的一條是
#           test_switching_project_changes_which_freeze_directory_status_reads
#           —— 它證明 Project 是 namespace 邊界，而不是 UI filter。
# 主要責任:
#   1. 驗證 /projects 渲染並列出專案
#   2. 驗證切換專案後版型與 breadcrumb 都改變
#   3. 驗證切換後 Status 讀的是另一個專案的 lineage
#   4. 驗證非法／不存在的專案不會改動目前選擇
#   5. 驗證 app 的 workspace root 不落在真實 repo 上
# 維護提醒:
#   - 不得移除 workspace_root=tmp_path。少了它，跑一次測試就會在
#     開發者的 projects/ 底下留下專案，而封存與 Clone 還會改動它。
#   - 不得把切換測試改成只比對畫面文字。它要證明的是**資料來源**換了，
#     不是標題換了；標題相同與資料相同在畫面上分不出來。
#   - 不得在隔離 workspace 裡斷言「PFC-001 沒有外洩」。兩個專案都沒有
#     lineage 時那種斷言恆真，看起來卻像通過。
#   - v0.1.0 新增：首版，對應平台化 Phase 1。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_project_routes.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re

import pytest

flask = pytest.importorskip("flask")

from pcmef.platform.projects.resolver import (  # noqa: E402
    LEGACY_THESIS_PROJECT_ID,
)


@pytest.fixture()
def client(tmp_path):
    from pcmef.admin.app import create_app

    app = create_app(
        registry_path=tmp_path / "registry.db",
        vault_path=tmp_path / "vault",
        console_run_root=tmp_path / "runs",
        # workspace root 指向 tmp_path：測試建立與封存的專案落在這裡，
        # 不會動到開發者真實的 projects/。
        workspace_root=tmp_path / "workspace",
    )
    app.config["TESTING"] = True
    return app.test_client()


def _form(client, path, **fields):
    return client.post(path, data=fields, follow_redirects=True)


# ---------------------------------------------------------------------------
# 清單與遷移
# ---------------------------------------------------------------------------


def test_the_projects_page_renders(client):
    assert client.get("/projects").status_code == 200


def test_the_legacy_thesis_appears_without_being_created_by_hand(client):
    """第一次開啟就該看到既有 PC-MEF，而不是一個空 workspace。"""
    body = client.get("/projects").get_data(as_text=True)
    assert LEGACY_THESIS_PROJECT_ID in body
    assert "legacy layout" in body


def test_a_created_project_shows_up_in_the_list(client):
    _form(client, "/projects/create", project_id="sand-extension",
          display_name="Sand Extension", template="blank")
    body = client.get("/projects").get_data(as_text=True)
    assert "Sand Extension" in body


def test_an_illegal_project_id_is_rejected(client):
    _form(client, "/projects/create", project_id="../escape", display_name="Bad")
    body = client.get("/projects").get_data(as_text=True)
    assert "escape" not in body


# ---------------------------------------------------------------------------
# 切換 —— Project 是 namespace，不是 filter
# ---------------------------------------------------------------------------


def test_switching_project_changes_the_workspace_bar_and_breadcrumb(client):
    _form(client, "/projects/create", project_id="demo-minimal",
          display_name="Demo Minimal")
    _form(client, "/projects/select", project_id="demo-minimal")

    body = client.get("/status").get_data(as_text=True)
    assert "Demo Minimal" in body
    assert "Workspace" in body


def _lineage_target(body: str) -> str:
    """Status 頁上「這一頁去讀了哪個 lineage」的那一段。

    專案沒有 lineage 時，訊息裡帶的就是它**去找過**的路徑 ——
    那正好是最直接的資料來源證據。
    """
    match = re.search(r"no active lineage declaration at ([^<\"]+?)ACTIVE_LINEAGE", body)
    assert match, "Status did not report which lineage it resolved"
    return match.group(1).replace("\\", "/")


def test_switching_project_changes_which_freeze_directory_status_reads(client):
    """切換專案後，Status 必須去讀**另一個 freeze 目錄**。

    這一條是本輪的核心。若 Project 只是畫面上的篩選器，兩個專案的
    Status 會讀同一組 lock，只是標題不同 —— 而標題相同與資料相同
    在畫面上分不出來，所以這裡比對的是資料來源，不是文字。

    刻意不斷言「PFC-001 沒有外洩」：在隔離的測試 workspace 裡
    兩個專案都沒有 lineage，那種斷言會**恆真**而看起來像通過。
    """
    thesis_target = _lineage_target(client.get("/status").get_data(as_text=True))

    _form(client, "/projects/create", project_id="demo-minimal",
          display_name="Demo Minimal")
    _form(client, "/projects/select", project_id="demo-minimal")
    other_target = _lineage_target(client.get("/status").get_data(as_text=True))

    assert thesis_target != other_target, (
        "both projects resolved to the same freeze directory; the project "
        "boundary is a UI filter, not a namespace"
    )
    # legacy 專案用 repo 根目錄佈局，因此它的 freeze 不在 projects/ 底下。
    assert "/projects/" not in thesis_target
    assert thesis_target.endswith("/freeze/")
    # 一般專案完全收在自己的 namespace 裡。
    assert other_target.endswith("/projects/demo-minimal/freeze/")


def test_returning_to_the_thesis_restores_its_own_lineage(client):
    """對照組：切回去要拿回原本那一組，而不是停在最後一次選的。"""
    before = _lineage_target(client.get("/status").get_data(as_text=True))
    _form(client, "/projects/create", project_id="demo-minimal",
          display_name="Demo Minimal")
    _form(client, "/projects/select", project_id="demo-minimal")
    _form(client, "/projects/select", project_id=LEGACY_THESIS_PROJECT_ID)

    assert _lineage_target(client.get("/status").get_data(as_text=True)) == before


def test_selecting_a_missing_project_leaves_the_current_one_alone(client):
    _form(client, "/projects/create", project_id="keep-me", display_name="Keep Me")
    _form(client, "/projects/select", project_id="keep-me")

    _form(client, "/projects/select", project_id="does-not-exist")
    body = client.get("/status").get_data(as_text=True)
    assert "Keep Me" in body, (
        "a failed switch must not silently drop the user into another project"
    )


# ---------------------------------------------------------------------------
# 封存與 Clone
# ---------------------------------------------------------------------------


def test_archiving_keeps_the_project_visible_but_marked(client):
    _form(client, "/projects/create", project_id="old-work", display_name="Old Work")
    _form(client, "/projects/old-work/archive")

    body = client.get("/projects").get_data(as_text=True)
    assert "Old Work" in body, "archived is not deleted; hiding it misleads"
    assert "已封存" in body


def test_cloning_creates_a_draft_child(client):
    _form(client, "/projects/create", project_id="origin-one", display_name="Origin One")
    _form(client, "/projects/origin-one/clone",
          new_project_id="copy-one", new_display_name="Copy One")

    body = client.get("/projects").get_data(as_text=True)
    assert "Copy One" in body
    assert "origin-one" in body


def test_the_workspace_root_is_not_the_real_repo(client, tmp_path):
    """守門：測試若寫進真實 repo，這條會先失敗。"""
    from pcmef.admin.app import create_app  # noqa: F401

    _form(client, "/projects/create", project_id="scratch-only",
          display_name="Scratch Only")
    assert (tmp_path / "workspace" / "projects" / "scratch-only").exists()
