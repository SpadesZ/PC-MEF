# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 取 /projects/design。
#         一律以 tmp_path 當 workspace root，**不碰真實 projects/**。
# 檔案路徑: tests/console/test_research_design_page.py
# 產生時間: 2026-09-06 22:50 +08:00
# 版本: v0.1.0
# 功能說明: Research Design 頁的渲染、唯讀性與跨專案隔離驗收。
# 模組定位: 平台化 Phase 3 的前端驗收。第一次使用者應在這一頁回答
#           「這個 Project 研究什麼、用哪些感測器、判準是什麼」。
# 主要責任:
#   1. 驗證 Thesis 專案顯示 v1.2.1 的研究設計
#   2. 驗證頁面完全唯讀（無表單）
#   3. 驗證空專案不顯示 PC-MEF 的研究設計
#   4. 驗證出處與 authoritative lock 有出現在畫面上
# 維護提醒:
#   - 不得在本頁加入編輯表單，測試會擋下。已定稿的研究設計要改，
#     一律 Clone 成 Development Profile。
#   - 不得放寬 test_a_blank_project_shows_no_pcmef_design；那是
#     「Blank Project 不得出現 PC-MEF 專屬內容」的守門測試。
#   - v0.1.0 新增：首版，對應平台化 Phase 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_research_design_page.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

flask = pytest.importorskip("flask")


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


def test_the_design_page_renders(client):
    assert client.get("/projects/design").status_code == 200


def test_the_thesis_design_shows_the_plan_content(client):
    body = client.get("/projects/design").get_data(as_text=True)

    assert "PC-MEF_實驗計畫_v1.2.1" in body
    assert "Worst-condition Macro-F1" in body
    for label in ("Empty", "Water-filled", "Bubbly", "Misty"):
        assert label in body
    for channel in ("Distance", "Signal Rate", "Ambient Rate", "Sigma-like"):
        assert channel in body


def test_the_design_page_shows_the_frozen_profile_identity(client):
    body = client.get("/projects/design").get_data(as_text=True)
    assert "FROZEN" in body
    assert "thesis-frozen" in body


def test_the_design_page_cites_sources_and_authoritative_locks(client):
    """畫面必須同時說「計畫書怎麼寫」與「執行時讀哪一份」。"""
    body = client.get("/projects/design").get_data(as_text=True)
    assert "§3.4" in body, "plan citations must be visible"
    assert "gate.lock" in body, "the authoritative lock must be named"


def test_the_design_page_is_read_only(client):
    """已定稿的研究設計不得從畫面上被改；要改一律 Clone。"""
    body = client.get("/projects/design").get_data(as_text=True)
    content = body.split('<div class="page">', 1)[-1]
    for control in ("<form", "<textarea", "<input"):
        assert control not in content, f"the design page must not contain {control}"


def test_a_blank_project_shows_no_pcmef_design(client):
    """Blank Project 不得出現 PC-MEF 專屬的研究內容。"""
    _form(client, "/projects/create", project_id="blank-demo",
          display_name="Blank Demo")
    _form(client, "/projects/select", project_id="blank-demo")

    body = client.get("/projects/design").get_data(as_text=True)
    assert "Blank Demo" in body
    for leaked in ("Worst-condition Macro-F1", "Water-filled", "Sigma-like",
                   "PC-MEF_實驗計畫_v1.2.1"):
        assert leaked not in body, (
            f"{leaked!r} leaked into a blank project's research design page"
        )


def test_a_blank_project_gets_a_starter_profile_with_no_design(client):
    """template 給的是**起點**：一份 DRAFT Profile，但還沒有研究設計。

    「有 Profile、沒有設計」與「連 Profile 都沒有」是兩種狀態，
    畫面必須分得出來 —— 前者是「該來填設計了」，後者是「還沒開始」。
    """
    _form(client, "/projects/create", project_id="blank-demo",
          display_name="Blank Demo", template="blank")
    _form(client, "/projects/select", project_id="blank-demo")

    body = client.get("/projects/design").get_data(as_text=True)
    assert "Development Profile v1" in body
    assert "DRAFT" in body
    assert "尚未寫入 Research Design" in body


def test_a_project_with_no_profile_at_all_says_so(client, tmp_path):
    """沒有起始 Profile 的專案（例如未知 template）要明說。"""
    _form(client, "/projects/create", project_id="no-template-demo",
          display_name="No Template Demo", template="not-a-template")
    _form(client, "/projects/select", project_id="no-template-demo")

    body = client.get("/projects/design").get_data(as_text=True)
    assert "還沒有任何 Research Profile" in body


def test_profile_switching_is_not_offered_on_the_design_page(client):
    """切換 Profile 會讓 Status / Pipeline / Run / Results 全部跟著換。

    那是有後果的動作，入口集中在 Workspace 頁；唯讀頁上不得出現，
    否則使用者會在一個「觀察頁」上改掉全站綁定的對象。
    """
    body = client.get("/projects/design").get_data(as_text=True)
    content = body.split('<div class="page">', 1)[-1]
    assert "select-profile" not in content


def test_the_workspace_page_offers_the_profile_switcher(client):
    """切換入口必須存在且集中 —— 只是不在唯讀頁上。"""
    body = client.get("/projects").get_data(as_text=True)
    assert "select-profile" in body
    assert "thesis-frozen" in body


def test_the_design_page_binds_to_the_selected_profile(client):
    """Research Design 顯示的必須是**選定的**那一份，不是猜的。"""
    _form(client, "/projects/create", project_id="two-profile-demo",
          display_name="Two Profile Demo")
    _form(client, "/projects/select", project_id="two-profile-demo")

    body = client.get("/projects/design").get_data(as_text=True)
    assert "尚未" in body, (
        "a project with no declared default profile must say so rather than "
        "silently showing one"
    )
