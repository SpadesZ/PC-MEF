# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 打 pcmef.console.routes，
#         執行器換成不跑 mitsuba 的短指令；不開真實埠、不連線。
# 檔案路徑: tests/console/test_console_routes.py
# 產生時間: 2026-08-27 18:10 +08:00
# 版本: v0.1.0
# 功能說明: 驗證執行台的頁面、啟動、SSE 串流與結果呈現，
#           以及所有寫入端點都受 CSRF 保護、且無法從 UI 啟動 formal。
# 模組定位: Console HTTP 層的防線。它不驗證圖表長得好不好看。
# 主要責任:
#   1. test_console_page_* 驗證三個 preset 與 gate 燈號都在
#   2. test_starting_a_run_* 驗證表單與 JSON 兩種提交
#   3. test_stream_sends_lines_then_done 驗證 SSE 的事件序列
#   4. test_csrf_is_required 驗證寫入端點受保護
#   5. test_formal_is_refused_over_http 驗證 §208 在 HTTP 層也守得住
# 維護提醒:
#   - 不得在測試裡跑真實 mitsuba；那會讓這組測試從毫秒變成分鐘，
#     而它要驗的是 HTTP 行為不是算圖。
#   - v0.1.0 新增：首版，對應 NOTE-025。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_console_routes.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import re
import sys

import pytest

from pcmef.console.runner import ConsoleRunner, RunSpec


class _FastRunner(ConsoleRunner):
    """不跑 mitsuba，只印幾行就結束。"""

    def _command(self, run_id, spec):
        lines = int(spec.params.get("lines", 3))
        return [
            sys.executable, "-u", "-c",
            f"for i in range({lines}): print('tick', i, flush=True)",
        ]


@pytest.fixture
def app(tmp_path):
    from pcmef.admin.app import create_app
    from pcmef.admin.services import AdminService
    from pcmef.audit.e1_gates import AuditPaths
    from pcmef.core.config import load_config
    from pcmef.llm.registry import LLMRegistry
    from pcmef.secrets.vault import SecretVault

    service = AdminService(
        registry=LLMRegistry(tmp_path / "llm.db"),
        vault=SecretVault(tmp_path / "vault.json", master_key="t", environ={}),
        config=load_config(["configs/base.yaml"]),
        freeze_dir=tmp_path / "freeze",
    )
    application = create_app(
        service=service, console_run_root=tmp_path / "runs",
        audit_paths=AuditPaths(
            inventory=tmp_path / "none", splits=tmp_path / "none",
            simulation=tmp_path / "none", surrogate=tmp_path / "none",
            provenance=tmp_path / "none", freeze=tmp_path / "freeze",
            tests=tmp_path / "none",
        ),
        environ={},
    )
    application.config["PCMEF_CONSOLE_RUNNER"] = _FastRunner(tmp_path / "runs")
    application.testing = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf(client):
    html = client.get("/console").get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise AssertionError("the console page rendered no csrf_token field")
    return match.group(1)


# ---------------------------------------------------------------------------
# 頁面
# ---------------------------------------------------------------------------


def test_root_redirects_to_the_console(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/console" in response.headers["Location"]


def test_console_page_shows_all_three_presets(client):
    html = client.get("/console").get_data(as_text=True)
    for label in ("快速預覽", "標準", "高品質"):
        assert label in html


def test_console_page_shows_the_gate_lights(client):
    html = client.get("/console").get_data(as_text=True)
    assert 'class="gate-grid"' in html
    for gate in ("G01", "G06", "G12"):
        assert gate in html


def test_console_page_states_that_formal_runs_go_through_the_cli(client):
    html = client.get("/console").get_data(as_text=True)
    assert "CLI" in html
    assert "lock" in html


def test_the_page_does_not_claim_it_cannot_produce_a_lock(client):
    """畫面上有一顆「凍結」按鈕，就不能同時宣稱本頁不產生 lock。

    頁面自己打自己的臉比講得含糊更糟：讀的人會以為其中一邊壞了，
    而他無從判斷是哪一邊。成立的說法是「寫不寫入由 CLI 判定」。
    """
    html = client.get("/console").get_data(as_text=True)
    assert "凍結" in html
    assert "不能產生" not in html


def test_advanced_parameters_are_collapsed_by_default(client):
    """懶人包：進階區用原生 <details>，預設收起且不需要腳本。"""
    html = client.get("/console").get_data(as_text=True)
    assert "<details" in html
    assert "open" not in re.search(r"<details[^>]*>", html).group(0)


# ---------------------------------------------------------------------------
# 啟動
# ---------------------------------------------------------------------------


def test_starting_a_run_from_the_form_redirects_to_the_run_page(client, csrf):
    response = client.post(
        "/api/console/runs",
        data={"csrf_token": csrf, "kind": "sim_smoke", "preset": "preview", "lines": "2"},
    )
    assert response.status_code == 302
    assert "/console/runs/" in response.headers["Location"]


def test_starting_a_run_over_json_returns_the_record(client, csrf):
    response = client.post(
        "/api/console/runs",
        json={"kind": "sim_smoke", "preset": "preview"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["kind"] == "sim_smoke"
    assert body["params"]["preset"] == "preview"


def test_csrf_is_required_to_start_a_run(client, csrf):
    response = client.post(
        "/api/console/runs", json={"kind": "sim_smoke"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403
    assert "CSRF" in response.get_json()["error"]


def test_formal_is_refused_over_http(client, csrf):
    """§208 在 HTTP 層也必須守得住，不能只靠執行器。"""
    response = client.post(
        "/api/console/runs",
        json={"kind": "sim_smoke", "formal": True},
        headers={"X-CSRF-Token": csrf},
    )
    # formal 不在允許的參數白名單內，因此不會被帶進 params —— 但即使被帶進，
    # runner 也會拒絕。這裡驗證的是「無論如何都不會產生 formal 指令」。
    assert response.status_code in (201, 403)
    if response.status_code == 201:
        assert "--formal" not in " ".join(response.get_json()["command"])


def test_an_unknown_kind_is_rejected(client, csrf):
    response = client.post(
        "/api/console/runs", json={"kind": "experiment_e2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 執行紀錄：搜尋、摺疊、刪除
# ---------------------------------------------------------------------------


def _finish(app, count: int, **params):
    """跑完 count 次短執行，回傳紀錄。"""
    runner = app.config["PCMEF_CONSOLE_RUNNER"]
    records = []
    for _ in range(count):
        record = runner.start(RunSpec(kind="sim_smoke", params={"lines": 1, **params}))
        records.append(runner.wait(record.run_id, timeout=30))
    return records


def test_the_history_is_searchable(app, client):
    _finish(app, 1, preset="preview")
    _finish(app, 1, preset="quality")

    html = client.get("/console?q=quality").get_data(as_text=True)
    assert "preset=quality" not in html  # 參數本身不印在畫面上
    assert "高品質" in html
    assert "快速預覽" not in html.split('id="run-history"')[1]


def test_search_reaches_runs_beyond_the_display_limit(app, client):
    """先截斷再篩選的話，limit 之外的舊紀錄永遠搜不到 ——
    而「東西太多所以要搜尋」時，要找的通常正好就是那些舊的。"""
    from pcmef.console.routes import RECENT_RUN_COUNT

    oldest = _finish(app, 1, preset="quality")[0]
    _finish(app, RECENT_RUN_COUNT + 3, preset="preview")

    plain = client.get("/console").get_data(as_text=True)
    searched = client.get("/console?q=quality").get_data(as_text=True)

    # 沒搜尋時它被推進摺疊區（"還有 N 筆" 之後）。
    assert oldest.run_id in plain
    assert plain.index("還有") < plain.index(oldest.run_id)

    # 搜尋後它必須落在攤開的那張表裡，也就是根本不需要展開摺疊。
    assert oldest.run_id in searched
    assert "還有" not in searched


def test_older_runs_are_collapsed_so_the_table_stays_short(app, client):
    from pcmef.console.routes import RECENT_RUN_COUNT

    _finish(app, RECENT_RUN_COUNT + 2)
    html = client.get("/console").get_data(as_text=True)

    assert "還有 2 筆較早的紀錄" in html


def test_a_finished_run_can_be_deleted(app, client, csrf):
    record = _finish(app, 1)[0]

    response = client.post(
        f"/api/console/runs/{record.run_id}/delete",
        json={}, headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert not (app.config["PCMEF_CONSOLE_RUNNER"].run_dir(record.run_id)).exists()


def test_a_running_run_cannot_be_deleted(app, client, csrf):
    """子行程還握著 log 的檔案句柄，而且刪掉之後 SSE 會對著不存在的紀錄重試。"""
    runner = app.config["PCMEF_CONSOLE_RUNNER"]
    record = runner.start(RunSpec(kind="sim_smoke", params={"lines": 400}))

    response = client.post(
        f"/api/console/runs/{record.run_id}/delete",
        json={}, headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert "still running" in response.get_json()["error"]
    runner.wait(record.run_id, timeout=30)


def test_csrf_is_required_to_delete_a_run(app, client):
    record = _finish(app, 1)[0]
    response = client.post(
        f"/api/console/runs/{record.run_id}/delete",
        json={}, headers={"X-CSRF-Token": "wrong"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 從主控台觸發 llm snapshot
# ---------------------------------------------------------------------------


def test_the_snapshot_button_starts_a_recorded_run(client, csrf):
    response = client.post(
        "/api/console/llm-snapshot", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201
    assert response.get_json()["kind"] == "llm_snapshot"


def test_csrf_is_required_to_trigger_a_snapshot(client):
    response = client.post(
        "/api/console/llm-snapshot", json={}, headers={"X-CSRF-Token": "wrong"}
    )
    assert response.status_code == 403


def test_the_admin_page_itself_still_refuses_to_write_a_lock(client, csrf):
    """界線沒有被放寬，只是被劃細。

    admin 頁面自己永遠不寫 lock —— 要寫就必須經過真正的 CLI 子行程，
    而那條路上前提未齊時是 CLI 自己拒絕。這兩條同時成立才是原本的意思。
    """
    html = client.get("/admin/llm-setup").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    response = client.post(
        "/api/admin/llm/runtime-snapshot", json={}, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 403
    assert "CLI" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# 串流
# ---------------------------------------------------------------------------


def test_stream_sends_lines_then_done(app, client, csrf):
    record = app.config["PCMEF_CONSOLE_RUNNER"].start(
        RunSpec(kind="sim_smoke", params={"lines": 4})
    )
    response = client.get(f"/api/console/runs/{record.run_id}/stream")
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"

    body = response.get_data(as_text=True)
    lines = [
        json.loads(chunk.split("data: ", 1)[1])["line"]
        for chunk in body.split("\n\n")
        if chunk.startswith("data: ")
    ]
    assert [l for l in lines if l.startswith("tick")] == [
        "tick 0", "tick 1", "tick 2", "tick 3"
    ]
    assert "event: done" in body


def test_stream_for_an_unknown_run_fails(client):
    assert client.get("/api/console/runs/nope/stream").status_code == 400


def test_status_endpoint_reports_the_record(app, client):
    record = app.config["PCMEF_CONSOLE_RUNNER"].start(
        RunSpec(kind="sim_smoke", params={"lines": 1})
    )
    app.config["PCMEF_CONSOLE_RUNNER"].wait(record.run_id, timeout=30)
    body = client.get(f"/api/console/runs/{record.run_id}/status").get_json()
    assert body["run_id"] == record.run_id
    assert body["status"] == "succeeded"


# ---------------------------------------------------------------------------
# 執行頁
# ---------------------------------------------------------------------------


def test_run_page_shows_the_log_and_the_exact_command(app, client):
    runner = app.config["PCMEF_CONSOLE_RUNNER"]
    record = runner.start(RunSpec(kind="sim_smoke", params={"lines": 2}))
    runner.wait(record.run_id, timeout=30)

    html = client.get(f"/console/runs/{record.run_id}").get_data(as_text=True)
    assert "tick 0" in html
    assert "succeeded" in html
    # 這次執行用了什麼，必須能完整回答。
    assert record.command[0].split("\\")[-1].split("/")[-1] in html or "python" in html


def _strip_comments(source: str) -> str:
    """去掉 Jinja、HTML、CSS 與 JavaScript 註解。

    註解裡本來就必須寫得出「不得引入 React/Vue」這句話；連註解一起掃，
    唯一能通過的寫法就變成不准解釋為什麼禁 —— 那把一條有理由的規則
    退化成無法傳達的迷信（與 NOTE-017 的 AST 掃描同理）。
    """
    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        source = re.sub(pattern, "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_run_page_uses_exactly_one_inline_script(app, client):
    """§42 允許 minimal JS；這裡量化「minimal」= 一段內嵌腳本、零外部依賴。"""
    runner = app.config["PCMEF_CONSOLE_RUNNER"]
    record = runner.start(RunSpec(kind="sim_smoke", params={"lines": 1}))
    runner.wait(record.run_id, timeout=30)

    html = client.get(f"/console/runs/{record.run_id}").get_data(as_text=True)
    assert html.count("<script") == 1
    assert "<script src=" not in html

    code = _strip_comments(html).lower()
    for framework in ("react", "vue", "angular", "jquery", "htmx", "cdn."):
        assert framework not in code, framework


def test_the_comment_stripper_catches_a_real_dependency():
    """反證：去註解不得把真的引入也一起去掉。"""
    assert "vue" not in _strip_comments("// 不得引入 Vue").lower()
    assert "vue" in _strip_comments('<script src="vue.js">').lower()


def test_the_live_log_is_appended_as_text_not_html(app, client):
    """伺服器輸出含檔名與錯誤字串；用 innerHTML 附加就是 XSS。"""
    runner = app.config["PCMEF_CONSOLE_RUNNER"]
    record = runner.start(RunSpec(kind="sim_smoke", params={"lines": 1}))
    runner.wait(record.run_id, timeout=30)

    code = _strip_comments(
        client.get(f"/console/runs/{record.run_id}").get_data(as_text=True)
    )
    assert "createTextNode" in code
    assert "innerHTML" not in code
