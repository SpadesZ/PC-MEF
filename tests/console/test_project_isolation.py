# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 同時操作三個專案。
#         一律以 tmp_path 當 workspace root，**不碰真實 projects/**。
# 檔案路徑: tests/console/test_project_isolation.py
# 產生時間: 2026-09-07 20:10 +08:00
# 版本: v0.1.0
# 功能說明: 三專案並存下的 namespace / Profile / Status / Pipeline /
#           Results / artifact 隔離驗收。
# 模組定位: 平台化的總體隔離驗收。A = PC-MEF Thesis、
#           B = Thesis 的 development clone、C = minimal dummy。
#           重點是「B 與 C 不得看到 A 的科學內容」。
# 主要責任:
#   1. 驗證三個專案同時存在且各自獨立
#   2. 驗證 Status / Pipeline / Research Design 綁定各自的 Profile
#   3. 驗證 C 完全不出現 PC-MEF 字樣，B 不出現 A 自己的 lineage
#   4. 驗證 pipeline 節點數隨專案改變
#   5. 驗證 archive 不影響其他專案
# 維護提醒:
#   - **不得放寬 FORBIDDEN_IN_BLANK。** 那一組是這篇論文科學內容的
#     指紋；出現在 Blank Project 上就是平台把 A 的東西漏給了不相干的專案。
#   - **不得把 FORBIDDEN_EVERYWHERE_BUT_THE_THESIS 併進上面那一組。**
#     兩者問的是不同的問題：前者問「這個專案有沒有繼承 PC-MEF 的
#     設計語彙」（clone 有，是對的），後者問「有沒有看到 A 自己的
#     身分」（誰都不該有）。合併會逼人放寬其中一條。
#   - 不得讓本檔在真實 workspace 執行。
#   - v0.1.0 新增：首版，對應平台化總體驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_project_isolation.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

flask = pytest.importorskip("flask")

from pcmef.platform.projects.resolver import (  # noqa: E402
    LEGACY_THESIS_PROJECT_ID,
)

#: 這篇論文的**科學內容**。一個 Blank Project 出現其中任何一項，
#: 就代表平台把 PC-MEF 的東西洩漏給了不相干的專案。
FORBIDDEN_IN_BLANK = (
    "PFC-001",
    "AMD-007",
    "Water-filled",
    "Sigma-like",
    "Worst-condition Macro-F1",
    "VL53L0X",
    "PC-MEF_實驗計畫_v1.2.1",
)

#: A 這個專案**自己的狀態**。任何專案顯示它就是跨專案串接。
#:
#: 與上面那組刻意分開：Thesis 的 development clone 明確繼承了
#: pcmef-thesis template，因此它出現 VL53L0X（流程用語）或
#: AMD-007（它自己那份尚未凍結的 amendment）是正確的 ——
#: 使用者第「除非它自己的 design/template 明確定義」那一條。
#: 但它**不得**顯示 A 已解析出來的 lineage：那是 A 的身分，
#: 而身分是不能共用的。
FORBIDDEN_EVERYWHERE_BUT_THE_THESIS = (
    "PFC-001",
    "PC-MEF_實驗計畫_v1.2.1",
)

OBSERVATION_PAGES = ("/status", "/pipeline", "/projects/design", "/results")


@pytest.fixture()
def client(tmp_path):
    from pcmef.admin.app import create_app

    app = create_app(
        registry_path=tmp_path / "registry.db",
        vault_path=tmp_path / "vault",
        console_run_root=tmp_path / "runs",
        workspace_root=tmp_path / "workspace",
    )
    app.config["TESTING"] = True
    return app.test_client()


def _form(client, path, **fields):
    return client.post(path, data=fields, follow_redirects=True)


def _open(client, project_id):
    _form(client, "/projects/select", project_id=project_id)


@pytest.fixture()
def three_projects(client):
    """A = Thesis（遷移而來）、B = Thesis 的 clone、C = minimal dummy。"""
    client.get("/projects")  # 觸發 Thesis 遷移
    _form(client, "/projects/pcmef-thesis/clone",
          new_project_id="thesis-dev", new_display_name="Thesis Development")
    _form(client, "/projects/create", project_id="tiny-dummy",
          display_name="Tiny Dummy", template="blank")
    return client


# ---------------------------------------------------------------------------
# 三專案並存
# ---------------------------------------------------------------------------


def test_all_three_projects_exist(three_projects):
    body = three_projects.get("/projects").get_data(as_text=True)
    for name in ("PC-MEF Thesis", "Thesis Development", "Tiny Dummy"):
        assert name in body


def test_each_project_reports_its_own_paths(three_projects, tmp_path):
    for project_id in ("thesis-dev", "tiny-dummy"):
        _open(three_projects, project_id)
        body = three_projects.get("/projects").get_data(as_text=True)
        assert f"projects/{project_id}/freeze" in body


def test_the_thesis_keeps_the_legacy_layout(three_projects):
    _open(three_projects, LEGACY_THESIS_PROJECT_ID)
    body = three_projects.get("/projects").get_data(as_text=True)
    assert "legacy layout" in body


# ---------------------------------------------------------------------------
# 科學內容不得外洩 —— 本節是總體隔離的核心
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", OBSERVATION_PAGES)
def test_a_blank_project_shows_no_thesis_science_at_all(three_projects, page):
    """C：完全不相干的專案，一個字都不該漏。"""
    _open(three_projects, "tiny-dummy")
    body = three_projects.get(page).get_data(as_text=True)

    leaked = [s for s in FORBIDDEN_IN_BLANK if s in body]
    assert not leaked, (
        f"{page} leaked {leaked} into a blank project; the project boundary "
        "is a namespace, not a UI filter"
    )


@pytest.mark.parametrize("project_id", ["thesis-dev", "tiny-dummy"])
@pytest.mark.parametrize("page", OBSERVATION_PAGES)
def test_no_other_project_shows_the_thesis_own_identity(
    three_projects, project_id, page
):
    """B 與 C：都不得顯示 A 已解析出來的 lineage 或其定稿計畫書。

    B 繼承了 pcmef-thesis template，因此它出現流程用語與 gate 名稱是
    正確的；但 A 的 lineage 是 A 的身分，身分不能共用。
    """
    _open(three_projects, project_id)
    body = three_projects.get(page).get_data(as_text=True)

    leaked = [s for s in FORBIDDEN_EVERYWHERE_BUT_THE_THESIS if s in body]
    assert not leaked, (
        f"{page} leaked {leaked} into project {project_id!r}; that is the "
        "thesis's own scientific identity, not something a project inherits"
    )


def test_the_clone_resolves_its_own_freeze_directory(three_projects):
    """B 的 Status 必須去讀**自己的** freeze，不是 A 的。"""
    import re

    _open(three_projects, "thesis-dev")
    body = three_projects.get("/status").get_data(as_text=True)
    match = re.search(r"no active lineage declaration at ([^<\"]+?)ACTIVE_LINEAGE", body)
    assert match, "Status must report which lineage it tried to resolve"
    assert "projects/thesis-dev/freeze" in match.group(1).replace("\\", "/")


def test_the_thesis_itself_still_shows_its_science(three_projects):
    """對照組：否則上面的斷言只是證明了「每一頁都是空的」。"""
    _open(three_projects, LEGACY_THESIS_PROJECT_ID)
    design = three_projects.get("/projects/design").get_data(as_text=True)
    status = three_projects.get("/status").get_data(as_text=True)

    assert "Water-filled" in design
    assert "Worst-condition Macro-F1" in design
    assert "AMD-007" in status


# ---------------------------------------------------------------------------
# Pipeline / Status 隨專案改變
# ---------------------------------------------------------------------------


def test_pipeline_length_follows_the_project(three_projects):
    _open(three_projects, LEGACY_THESIS_PROJECT_ID)
    thesis = three_projects.get("/pipeline").get_data(as_text=True)
    _open(three_projects, "tiny-dummy")
    dummy = three_projects.get("/pipeline").get_data(as_text=True)

    assert "共 7 個節點" in thesis
    assert "共 3 個節點" in dummy


def test_the_dummy_project_shows_generic_lifecycle_stages(three_projects):
    _open(three_projects, "tiny-dummy")
    body = three_projects.get("/status").get_data(as_text=True)

    assert "研究定義 Project Definition" in body
    assert "正式前凍結 Pre-Final Freeze" in body
    assert "E1-G" not in body


def test_the_clone_starts_without_the_thesis_frozen_profile(three_projects):
    """Clone 複製設計，不繼承已凍結的科學身分。"""
    _open(three_projects, "thesis-dev")
    body = three_projects.get("/projects/design").get_data(as_text=True)
    assert "thesis-frozen" not in body


# ---------------------------------------------------------------------------
# Profile 隔離
# ---------------------------------------------------------------------------


def test_profile_selection_does_not_leak_between_projects(three_projects):
    _open(three_projects, LEGACY_THESIS_PROJECT_ID)
    assert "Frozen Thesis Profile" in three_projects.get(
        "/projects/design"
    ).get_data(as_text=True)

    _open(three_projects, "tiny-dummy")
    body = three_projects.get("/projects/design").get_data(as_text=True)
    assert "Frozen Thesis Profile" not in body


def test_switching_back_restores_the_previous_profile(three_projects):
    _open(three_projects, "tiny-dummy")
    _open(three_projects, LEGACY_THESIS_PROJECT_ID)
    body = three_projects.get("/projects/design").get_data(as_text=True)
    assert "Frozen Thesis Profile" in body


# ---------------------------------------------------------------------------
# Archive 不影響其他專案
# ---------------------------------------------------------------------------


def test_archiving_one_project_leaves_the_others_alone(three_projects):
    _form(three_projects, "/projects/tiny-dummy/archive")
    body = three_projects.get("/projects").get_data(as_text=True)

    assert "已封存" in body
    assert "PC-MEF Thesis" in body
    assert "Thesis Development" in body


def test_an_archived_project_cannot_be_opened(three_projects):
    _form(three_projects, "/projects/tiny-dummy/archive")
    _open(three_projects, "tiny-dummy")

    body = three_projects.get("/status").get_data(as_text=True)
    assert "Tiny Dummy" not in body, (
        "an archived project must not become the current project"
    )
