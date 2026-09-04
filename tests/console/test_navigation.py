# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 取六個頁面，
#         驗證共用導航、breadcrumb 與資訊架構的分工。不啟動真實伺服器。
# 檔案路徑: tests/console/test_navigation.py
# 產生時間: 2026-09-03 11:20 +08:00
# 版本: v0.1.0
# 功能說明: 確認五個主入口在每一頁都存在且一致，且各頁只回答自己的問題。
# 模組定位: P2-2 的回歸測試。它守的是資訊架構本身 ——
#           先前所有功能都往 Run 首頁堆，於是首頁誰都不負責。
# 主要責任:
#   1. test_every_page_carries_the_same_five_entries
#   2. test_each_page_highlights_itself
#   3. test_status_and_pipeline_are_read_only 這兩頁不得有表單
#   4. test_run_page_no_longer_owns_gates_or_history 內容確實搬走了
#   5. test_breadcrumb_shows_the_hierarchy
# 維護提醒:
#   - 不得把導航項目加成第六個。五個入口是刻意的收斂；新功能要歸進
#     其中一個，否則首頁會重新開始堆疊。
#   - 不得讓 Status 或 Pipeline 出現表單。它們是觀測面；
#     Results 唯一允許的寫入是刪除執行紀錄。
#   - v0.1.0 新增：首版，對應 P2-2。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_navigation.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re

import pytest

flask = pytest.importorskip("flask")

from pcmef.console.navigation import NAV_ITEMS, breadcrumb  # noqa: E402

#: 五個主入口與它們各自回答的問題。
PAGES = {
    "/console": "run",
    "/pipeline": "pipeline",
    "/results": "results",
    "/status": "status",
    "/admin/llm-setup": "llm",
}


@pytest.fixture()
def client(tmp_path):
    from pcmef.admin.app import create_app

    app = create_app(
        registry_path=tmp_path / "registry.db",
        vault_path=tmp_path / "vault",
        console_run_root=tmp_path / "runs",
    )
    app.config["TESTING"] = True
    return app.test_client()


def _nav_labels(body: str) -> list[str]:
    nav = re.search(r'<nav class="topnav">.*?</nav>', body, re.S)
    assert nav, "the page has no top navigation"
    return re.findall(r'<a href="[^"]*"\s*\n?\s*class="[^"]*"[^>]*>([^<]+)</a>', nav.group())


# ---------------------------------------------------------------------------
# 導航
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(PAGES))
def test_every_page_renders(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", sorted(PAGES))
def test_every_page_carries_the_same_five_entries(client, path):
    body = client.get(path).get_data(as_text=True)
    labels = [item.label for item in NAV_ITEMS]
    for label in labels:
        assert label in body, f"{path} is missing the {label!r} entry"
    assert len(NAV_ITEMS) == 5, (
        "five entries is a deliberate convergence; a sixth means some feature "
        "was not classified into an existing question"
    )


@pytest.mark.parametrize("path, key", sorted(PAGES.items()))
def test_each_page_highlights_itself(client, path, key):
    body = client.get(path).get_data(as_text=True)
    label = next(item.label for item in NAV_ITEMS if item.key == key)
    # active 的那一項要帶 class="active"，且只有一項。
    active = re.findall(r'class="active"[^>]*>([^<]+)</a>', body)
    assert active == [label], f"{path} highlights {active}, expected [{label!r}]"


def test_the_formal_workspace_lives_under_run(client):
    """Formal Workspace 是「開始跑」的一部分，導航應高亮 Run。"""
    body = client.get("/formal").get_data(as_text=True)
    active = re.findall(r'class="active"[^>]*>([^<]+)</a>', body)
    assert active == ["實驗 Run"]


# ---------------------------------------------------------------------------
# 各頁只回答自己的問題
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/status", "/pipeline"])
def test_status_and_pipeline_are_read_only(client, path):
    """這兩頁是觀測面。要開始跑什麼一律回 Run。"""
    body = client.get(path).get_data(as_text=True).lower()
    for tag in ("<form", "<button", "<input", "<textarea"):
        assert tag not in body, f"{path} contains {tag}"


def test_the_run_page_no_longer_owns_the_gates(client):
    """E1 十二道 gate 搬到 Status —— Run 首頁不該再同時回答兩個問題。"""
    run = client.get("/console").get_data(as_text=True)
    status = client.get("/status").get_data(as_text=True)
    assert "gate-grid" not in run
    assert "E1 關卡" in status


def test_the_run_page_no_longer_owns_the_history(client):
    """執行紀錄表格搬到 Results。

    斷言用區塊 id 與表頭而不是「執行紀錄」四個字：Run 頁的 snapshot 卡片
    仍會提到「會留下一筆執行紀錄」，那是說明文字，不是那張表。
    以文字比對會把兩者混為一談。
    """
    run = client.get("/console").get_data(as_text=True)
    results = client.get("/results").get_data(as_text=True)
    assert 'id="run-history"' not in run
    assert "<th>執行編號</th>" not in run
    # Results 在沒有紀錄時不渲染表格，因此比對區塊標題而不是表頭 ——
    # 否則這條測試會在空目錄下失敗，而那是正常狀態。
    assert "<h2>執行紀錄" in results


def test_the_run_page_still_owns_starting_things(client):
    """搬走的是觀測，不是動作：Run 仍然回答「我要跑什麼」。"""
    body = client.get("/console").get_data(as_text=True)
    assert "跑一次模擬" in body
    assert "Formal Research Workspace" in body


# ---------------------------------------------------------------------------
# breadcrumb
# ---------------------------------------------------------------------------


def test_breadcrumb_starts_from_a_single_root():
    crumbs = breadcrumb(("結果 Results", "/results"), ("run-1", None))
    assert crumbs[0]["label"] == "PC-MEF"
    assert [c["label"] for c in crumbs] == ["PC-MEF", "結果 Results", "run-1"]


def test_the_formal_page_breadcrumb_shows_two_levels(client):
    body = client.get("/formal").get_data(as_text=True)
    crumb = re.search(r'<nav class="breadcrumb".*?</nav>', body, re.S)
    assert crumb
    text = crumb.group()
    assert "PC-MEF" in text and "實驗 Run" in text
    assert "Formal Research Workspace" in text


def test_an_unknown_nav_key_is_refused():
    from pcmef.console.navigation import nav_context

    with pytest.raises(ValueError):
        nav_context("nope")
