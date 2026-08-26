# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 打 pcmef.admin.routes_llm
#         的端點，背後是 conftest 的臨時 registry / vault / 離線 stub adapter；
#         不開啟真實埠、不對外連線。
# 檔案路徑: tests/llm_admin/test_admin_page.py
# 產生時間: 2026-08-27 02:20 +08:00
# 版本: v0.1.0
# 功能說明: 執行 §51 驗收表的 LLM-UI-01 到 LLM-UI-04，並驗證 §42 的版型鐵則 ——
#           四張 card、內容寬度、以及沒有引入任何前端框架。
# 模組定位: §51 Admin UI Acceptance Tests 的前四條，加上 §42 版型的結構性斷言。
#           它不驗證快照語意（那在 test_snapshot.py）。
# 主要責任:
#   1. test_llm_ui_01_* 驗證回應、HTML 與 log 都找不到完整 API key
#   2. test_llm_ui_02_* 驗證 embedding-only model 不出現在可綁 dropdown
#   3. test_llm_ui_03_* 驗證 structured-json probe FAIL 時 Bind 被拒
#   4. test_llm_ui_04_* 驗證仍被使用的 model profile Delete 回 409 與相依清單
#   5. test_layout_* 驗證四張 card、寬度區間與無前端框架
#   6. test_csrf_is_required_on_every_write_endpoint 驗證寫入端點都受保護
# 維護提醒:
#   - 不得把 LLM-UI-01 放寬成「只檢查完整字串不存在」；部分外洩同樣是外洩，
#     因此比對的是任何 8 字元連續片段。
#   - 不得為了讓測試好寫而在 create_app 開放預設 bind_enabled；
#     預設值本身是 §52 步驟 5 的一部分。
#   - v0.1.0 新增：首版，對應 §51 LLM-UI-01..04。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_admin_page.py -v
# ------------------------------------------------------------

from __future__ import annotations

import logging
import re

import pytest

from pcmef.agents.provider import Capability, ProbeResult
from pcmef.llm.capabilities import required_capabilities


def _verify(registry, model_profile_id, capabilities):
    for capability in capabilities:
        registry.record_verification(
            model_profile_id, ProbeResult(capability=capability, success=True)
        )


def _lock_line(registry, connection_id, model_profile_id):
    """走完 LAVA 的 draft → fetched → connected → locked（NOTE-023）。"""
    from pcmef.llm.registry import Lifecycle

    registry.select_model(connection_id, model_profile_id)
    registry.set_lifecycle(connection_id, Lifecycle.FETCHED)
    registry.set_lifecycle(connection_id, Lifecycle.CONNECTED)
    registry.set_lifecycle(connection_id, Lifecycle.LOCKED)


def _assert_no_fragment(haystack: str, secret: str) -> None:
    """部分外洩也是外洩：任何 8 字元連續片段都不得出現。"""
    assert secret not in haystack
    for start in range(0, len(secret) - 8):
        assert secret[start : start + 8] not in haystack


# ---------------------------------------------------------------------------
# LLM-UI-01
# ---------------------------------------------------------------------------


def test_llm_ui_01_api_key_appears_in_neither_response_html_nor_logs(
    client, csrf, admin_service, caplog, fake_key
):
    """新增 connection 後 response / HTML / logs 均找不到完整 API key。"""
    new_key = "sk-" + "Ui01SecretMaterial" * 2
    caplog.set_level(logging.DEBUG)

    response = client.post(
        "/api/admin/llm/connections",
        json={
            "name": "UI01", "provider": "google",
            "api_key": new_key, "timeout_sec": 30,
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 201
    body = response.get_data(as_text=True)
    _assert_no_fragment(body, new_key)
    # 取而代之的是遮蔽指紋，而指紋本身不是 key 的片段。
    fingerprint = response.get_json()["secret_fingerprint"]
    assert fingerprint.startswith("****")
    assert fingerprint[4:] not in new_key

    page = client.get("/admin/llm-setup").get_data(as_text=True)
    _assert_no_fragment(page, new_key)
    assert fingerprint in page

    _assert_no_fragment(caplog.text, new_key)


def test_the_stored_reference_is_a_vault_ref_not_the_key(client, csrf):
    new_key = "sk-" + "StoredAsReference0" * 2
    response = client.post(
        "/api/admin/llm/connections",
        json={"name": "RefOnly", "provider": "google", "api_key": new_key},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.get_json()["secret_ref"].startswith("vault:")


def test_a_plaintext_key_supplied_as_secret_ref_is_rejected(client, csrf):
    response = client.post(
        "/api/admin/llm/connections",
        json={
            "name": "Bad", "provider": "google",
            "secret_ref": "sk-" + "PastedTheKeyHere0" * 2,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "plaintext API key" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# LLM-UI-02
# ---------------------------------------------------------------------------


def test_llm_ui_02_embedding_only_model_is_absent_from_bindable_dropdowns(
    client, seeded, admin_service
):
    """embedding-only model 不會出現在 observation / arbitration 的可綁 dropdown。"""
    _verify(seeded.registry, seeded.embed_only_id, (Capability.EMBEDDING,))
    _verify(
        seeded.registry, seeded.full_model_id,
        (Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON),
    )
    _lock_line(seeded.registry, seeded.connection_id, seeded.full_model_id)

    views = {view.task_code: view for view in admin_service.binding_views()}

    for task_code in ("observation_agent", "arbitration_agent"):
        offered = {option.model_profile_id for option in views[task_code].options}
        assert seeded.embed_only_id not in offered, task_code
        assert seeded.full_model_id in offered, task_code

    page = client.get("/admin/llm-setup").get_data(as_text=True)
    assert seeded.embed_only_id not in _dropdown_section(page)


def _dropdown_section(html: str) -> str:
    """只取 Task Bindings card 裡的 <select> 區段。

    必須先切到 #task-bindings 再抓 select：Connections card 也有一個
    <select>，那是「這條線路要用哪一個模型」，本來就會列出所有抓回來的模型
    （包含 embedding-only）。兩者是不同用途的選單，混在一起會讓 LLM-UI-02
    在一個它根本不該管的選單上失敗。
    """
    start = html.index('id="task-bindings"')
    end = html.index('id="formal-snapshot"')
    return "".join(re.findall(r"<select[^>]*>.*?</select>", html[start:end], re.S))


# ---------------------------------------------------------------------------
# LLM-UI-03
# ---------------------------------------------------------------------------


def test_llm_ui_03_arbitration_bind_is_refused_when_structured_json_probe_fails(
    client, csrf, seeded
):
    """Arbitration binding 若 structured-json probe FAIL，Bind 直接拒絕。"""
    registry = seeded.registry
    _verify(registry, seeded.full_model_id, (Capability.CHAT, Capability.VISION))
    _lock_line(registry, seeded.connection_id, seeded.full_model_id)
    registry.record_verification(
        seeded.full_model_id,
        ProbeResult(
            capability=Capability.STRUCTURED_JSON, success=False,
            error_sanitized="stub: schema validation failed",
        ),
    )

    response = client.post(
        "/api/admin/llm/bindings/arbitration_agent",
        json={"model_profile_id": seeded.full_model_id},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert "structured_json" in response.get_json()["error"]
    assert registry.get_binding("arbitration_agent") is None

    # 之後 probe 通過就應該綁得上去 —— 拒絕的理由必須是能力，不是別的東西。
    registry.record_verification(
        seeded.full_model_id,
        ProbeResult(capability=Capability.STRUCTURED_JSON, success=True),
    )
    ok = client.post(
        "/api/admin/llm/bindings/arbitration_agent",
        json={"model_profile_id": seeded.full_model_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# LLM-UI-04
# ---------------------------------------------------------------------------


def test_llm_ui_04_deleting_a_bound_model_profile_returns_409_with_dependencies(
    client, csrf, seeded
):
    """仍被 task 使用的 model profile Delete -> 409，dependency 列表正確。"""
    registry = seeded.registry
    _verify(
        registry, seeded.full_model_id,
        (Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON),
    )
    _lock_line(registry, seeded.connection_id, seeded.full_model_id)
    for task_code in ("observation_agent", "physics_agent"):
        registry.set_binding(
            task_code, seeded.full_model_id, required_capabilities(task_code)
        )

    response = client.post(
        f"/api/admin/llm/models/{seeded.full_model_id}/delete",
        headers={"X-CSRF-Token": csrf},
        json={},
    )

    assert response.status_code == 409
    body = response.get_json()
    assert body["dependent_tasks"] == ["observation_agent", "physics_agent"]
    assert registry.get_model_profile(seeded.full_model_id) is not None


def test_deleting_an_unbound_model_profile_succeeds(client, csrf, seeded):
    response = client.post(
        f"/api/admin/llm/models/{seeded.embed_only_id}/delete",
        headers={"X-CSRF-Token": csrf},
        json={},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# §42 版型
# ---------------------------------------------------------------------------


def test_layout_has_exactly_the_four_specified_cards(client):
    html = client.get("/admin/llm-setup").get_data(as_text=True)
    for anchor in ("add-connection", "connections", "task-bindings", "formal-snapshot"):
        assert f'id="{anchor}"' in html, anchor
    assert html.count('class="card') == 4


def test_layout_max_width_is_within_the_specified_range():
    """§42：內容最大寬度約 1000-1200 px。"""
    from pathlib import Path

    css = (
        Path(__file__).resolve().parents[2]
        / "pcmef" / "admin" / "static" / "admin.css"
    ).read_text(encoding="utf-8")
    match = re.search(r"\.page\s*\{[^}]*max-width:\s*(\d+)px", css, re.S)
    assert match, "the .page rule declares no max-width"
    assert 1000 <= int(match.group(1)) <= 1200


def _strip_comments(source: str) -> str:
    """去掉 Jinja、HTML 與 CSS 註解。

    檔頭的維護提醒本來就必須寫得出「不得加入 React/Vue」這句話。
    連註解一起掃，唯一能通過的寫法就變成不准解釋為什麼禁 ——
    那把一條有理由的規則退化成無法傳達的迷信（與 NOTE-017 的 AST 掃描同理）。
    """
    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        source = re.sub(pattern, "", source, flags=re.S)
    return source


def test_the_comment_stripper_removes_the_prohibition_text():
    """反證：去註解後不該還留著禁令文字，否則上一條測試恆假。"""
    assert "vue" not in _strip_comments("{# 不得引入 Vue #}").lower()
    assert "vue" not in _strip_comments("/* 不得引入 Vue */").lower()
    # 但真的引入時必須留下來。
    assert "vue" in _strip_comments('<script src="vue.js"></script>').lower()


def test_layout_pulls_in_no_front_end_framework():
    """§42：第一版採 server-rendered HTML + minimal JavaScript。

    掃的是**樣板與樣式表原始碼**，不是渲染後的頁面。渲染結果含連線名稱、
    model id、錯誤訊息等執行期資料，那些是使用者輸入 —— 有人把連線命名為
    「vue 測試線路」不代表我們引入了 Vue，但會讓這條測試無故變紅。
    引入框架這件事只可能發生在樣板裡。
    """
    from pathlib import Path

    admin = Path(__file__).resolve().parents[2] / "pcmef" / "admin"
    sources = {
        path.name: _strip_comments(path.read_text(encoding="utf-8")).lower()
        for path in (
            admin / "templates" / "llm_setup.html",
            admin / "static" / "admin.css",
        )
    }
    assert sources, "no admin templates were found to scan"

    for name, source in sources.items():
        for forbidden in (
            "react", "vue", "angular", "jquery", "cdn.", "unpkg", "jsdelivr",
        ):
            assert forbidden not in source, f"{name} references {forbidden}"
        assert "<script src=" not in source, f"{name} loads an external script"


def test_the_rendered_page_runs_no_javascript(client):
    """執行期的對應檢查：頁面上不得有任何 <script> 元素。

    與上一條分工：上一條擋「引入框架」，這一條擋「渲染時混進腳本」。
    """
    html = client.get("/admin/llm-setup").get_data(as_text=True)
    assert "<script" not in html.lower()


def test_the_page_states_that_it_only_writes_the_draft_registry(client):
    html = client.get("/admin/llm-setup").get_data(as_text=True)
    assert "draft" in html
    assert "llm_runtime.lock" in html


# ---------------------------------------------------------------------------
# CSRF 與寫入邊界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/llm/connections",
        "/api/admin/llm/connections/whatever/fetch-models",
        "/api/admin/llm/models/whatever/verify",
        "/api/admin/llm/models/whatever/delete",
        "/api/admin/llm/connections/whatever/delete",
    ],
)
def test_csrf_is_required_on_every_write_endpoint(client, csrf, path):
    """csrf fixture 已建立 session 權杖；這裡刻意送出錯誤的那一個。"""
    response = client.post(path, json={}, headers={"X-CSRF-Token": "not-the-token"})
    assert response.status_code == 403
    assert "CSRF" in response.get_json()["error"]


def test_the_ui_cannot_create_a_runtime_snapshot(client, csrf):
    """§52 結語：UI 永遠不能成為繞過 freeze 的第二條設定通道。"""
    response = client.post(
        "/api/admin/llm/runtime-snapshot", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 403
    assert "CLI" in response.get_json()["error"]


def test_binding_through_the_ui_is_refused_when_the_instance_disables_it(
    make_client, seeded
):
    """§52 步驟 5：先讓 CLI 走完全流程，再接 UI Bind。"""
    disabled = make_client(bind_enabled=False)
    html = disabled.get("/admin/llm-setup").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    response = disabled.post(
        "/api/admin/llm/bindings/physics_agent",
        json={"model_profile_id": seeded.full_model_id},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 403
    assert "pcmef llm binding set" in response.get_json()["error"]


def test_listing_bindings_separates_draft_from_the_active_snapshot(client):
    """§50：active formal snapshot 必須與 draft 清楚分開。"""
    body = client.get("/api/admin/llm/bindings").get_json()
    assert set(body) == {"draft", "active_formal_snapshot"}
    assert len(body["draft"]) == 4
    assert "active_resolved_hash" in body["active_formal_snapshot"]
