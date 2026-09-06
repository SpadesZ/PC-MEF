# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；對 Action/Execution Layer 做對抗性測試。
#         **全部在 tmp_path；不觸發任何真正的科研寫入。**
# 檔案路徑: tests/console/test_execution_guards.py
# 產生時間: 2026-09-08 16:10 +08:00
# 版本: v0.1.0
# 功能說明: 動作層閉合的驗收 —— 身分先於行程、能力先於動作、
#           歸屬先於解讀，以及 legacy 邊界。
# 模組定位: Execution/Action Layer Closure 的守門測試。每一條都對應
#           一個**已經發生過**的缺陷：Blank Project 看得到碩論 Final
#           gate、能 POST llm freeze、能讀別的專案的 SSE。
# 主要責任:
#   1. 驗證 attribution 失敗時行程不啟動、不留孤兒
#   2. 驗證封存專案無法啟動執行
#   3. 驗證所有 run-scoped 端點的跨專案存取都被擋
#   4. 驗證 Formal 與 llm freeze 需要明確能力
#   5. 驗證 legacy 邊界之後沒有歸屬的 run 不歸碩論
# 維護提醒:
#   - **不得把任何一條的預期改成 200 或 302。** 看起來成功的拒絕
#     是這一層最難查的錯。
#   - 不得以「畫面上沒有按鈕」代替後端能力檢查。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_guards.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

flask = pytest.importorskip("flask")


@pytest.fixture()
def env(tmp_path):
    from pcmef.admin.app import create_app

    runs = tmp_path / "runs"
    app = create_app(
        registry_path=tmp_path / "registry.db",
        vault_path=tmp_path / "vault",
        console_run_root=runs,
        workspace_root=tmp_path / "workspace",
    )
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/projects")
    client.post("/projects/create", data={
        "project_id": "tiny-dummy", "display_name": "Tiny", "template": "blank",
    }, follow_redirects=True)
    return client, runs


def _select(client, project_id):
    client.post("/projects/select", data={"project_id": project_id},
                follow_redirects=True)


def _token(client) -> str:
    body = client.get("/console").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)


def _start(client) -> str:
    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    return re.search(r"/console/runs/([\w.-]+)", response.headers["Location"]).group(1)


# ---------------------------------------------------------------------------
# P0-1 身分先於行程
# ---------------------------------------------------------------------------


def test_attribution_is_written_before_the_process_starts(env):
    client, runs = env
    _select(client, "pcmef-thesis")
    run_id = _start(client)

    data = json.loads((runs / run_id / "run_identity.json").read_text(encoding="utf-8"))
    assert data["project_id"] == "pcmef-thesis"
    assert data["pipeline_snapshot"]["stages"], "the full semantics must be captured"


def test_a_failed_attribution_starts_no_process_and_leaves_nothing(env):
    """「行程已在跑、但這筆 run 沒有主人」必須是不可達狀態。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    before = {p.name for p in runs.iterdir()} if runs.exists() else set()

    with patch("pcmef.platform.runs.write_attribution", side_effect=OSError("disk full")):
        response = client.post("/api/console/runs", data={
            "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
        })

    after = {p.name for p in runs.iterdir()} if runs.exists() else set()
    assert response.status_code == 409
    assert after == before, "no run directory may survive a failed attribution"


# ---------------------------------------------------------------------------
# P0-2 封存與啟動
# ---------------------------------------------------------------------------


def test_an_archived_project_cannot_start_a_run(env):
    """封存的意義是不能再啟動。只讓按鈕變灰擋不住直接 POST。"""
    client, _runs = env
    _select(client, "tiny-dummy")
    client.post("/projects/tiny-dummy/archive", follow_redirects=True)

    response = client.post("/api/console/runs",
                           json={"kind": "sim_smoke", "preset": "standard"})
    assert response.status_code == 403


def test_an_archived_project_run_is_not_stamped_as_thesis(env):
    """封存後 fallback 到 Thesis，會讓 B 發起的執行被記成碩論的。"""
    client, runs = env
    _select(client, "tiny-dummy")
    client.post("/projects/tiny-dummy/archive", follow_redirects=True)
    before = {p.name for p in runs.iterdir()} if runs.exists() else set()

    client.post("/api/console/runs", json={"kind": "sim_smoke", "preset": "standard"})

    after = {p.name for p in runs.iterdir()} if runs.exists() else set()
    assert after == before, "a refused start must create no run at all"


# ---------------------------------------------------------------------------
# P0-3 每一個 run-scoped 端點
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/console/runs/{rid}",
    "/console/runs/{rid}/trace",
    "/console/runs/{rid}/artifacts",
    "/console/runs/{rid}/trace/previews/anything.png",
    "/api/console/runs/{rid}/status",
    "/api/console/runs/{rid}/stream",
])
def test_every_run_scoped_get_is_refused_across_projects(env, path):
    client, _runs = env
    _select(client, "pcmef-thesis")
    run_id = _start(client)
    _select(client, "tiny-dummy")

    assert client.get(path.format(rid=run_id)).status_code == 404


@pytest.mark.parametrize("path", [
    "/api/console/runs/{rid}/figures",
    "/api/console/runs/{rid}/delete",
])
def test_every_run_scoped_post_is_refused_across_projects(env, path):
    client, _runs = env
    _select(client, "pcmef-thesis")
    run_id = _start(client)
    token = _token(client)
    _select(client, "tiny-dummy")

    response = client.post(path.format(rid=run_id), data={"csrf_token": token})
    assert response.status_code == 404


def test_the_owning_project_still_reaches_its_own_run(env):
    """對照組：否則上面只證明了「什麼都打不開」。"""
    client, _runs = env
    _select(client, "pcmef-thesis")
    run_id = _start(client)

    assert client.get(f"/console/runs/{run_id}").status_code == 200
    assert client.get(f"/api/console/runs/{run_id}/status").status_code == 200


# ---------------------------------------------------------------------------
# P0-4 / P0-5 能力
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/formal", "/formal/preflight.json"])
def test_a_project_without_formal_capability_cannot_see_it(env, path):
    client, _runs = env
    _select(client, "tiny-dummy")
    assert client.get(path).status_code == 403


def test_a_project_without_formal_capability_sees_no_thesis_gate(env):
    """拒絕的訊息不得洩漏被拒絕的內容。"""
    client, _runs = env
    _select(client, "tiny-dummy")
    body = client.get("/formal").get_data(as_text=True)

    for leaked in ("AMD-007", "PFC-001", "e2_final", "canonical_golden_baseline"):
        assert leaked not in body


def test_a_project_without_formal_capability_cannot_start_formal(env):
    client, _runs = env
    _select(client, "tiny-dummy")
    response = client.post("/formal/start", data={
        "csrf_token": _token(client), "mode": "dry-run",
    })
    assert response.status_code == 403


def test_a_project_without_freeze_capability_cannot_write_the_llm_lock(env):
    """Blank Project 先前可以直接 POST 進來動到碩論的 lock。"""
    client, _runs = env
    _select(client, "tiny-dummy")
    assert client.post("/api/console/llm-snapshot",
                       json={"freeze": "1"}).status_code == 403


def test_the_thesis_project_retains_its_capabilities(env):
    """對照組：能力模型不能只是把所有人都擋掉。

    能力要連同 Thesis 的 Frozen Profile 一起問。宣告在 Project 上的
    能力**需要一份選定的 Research Profile 才生效** —— 沒有科學身分
    可掛的凍結，事後說不出它是哪一版設定產生的。
    """
    from pcmef.platform.capabilities import (
        FORMAL_E2, LLM_RUNTIME_FREEZE, capabilities_for,
    )
    from pcmef.platform.profiles.registry import ProfileRegistry
    from pcmef.platform.projects.registry import ProjectRegistry

    client, _runs = env
    _select(client, "pcmef-thesis")
    assert client.get("/formal").status_code == 200

    workspace = client.application.config["PCMEF_WORKSPACE_ROOT"]
    registry = ProjectRegistry(root=workspace)
    profile = ProfileRegistry(root=workspace).get(
        "pcmef-thesis", registry.get("pcmef-thesis").default_profile_id
    )
    caps = capabilities_for(registry.get("pcmef-thesis"), profile)
    assert FORMAL_E2 in caps and LLM_RUNTIME_FREEZE in caps


def test_a_declared_capability_does_not_survive_losing_its_profile(env):
    """對照組的另一半：宣告在 Project 上，但沒有 Profile 就不生效。"""
    from pcmef.platform.capabilities import (
        FORMAL_E2, LLM_RUNTIME_FREEZE, capabilities_for,
    )
    from pcmef.platform.projects.registry import ProjectRegistry

    client, _runs = env
    _select(client, "pcmef-thesis")
    registry = ProjectRegistry(root=client.application.config["PCMEF_WORKSPACE_ROOT"])

    caps = capabilities_for(registry.get("pcmef-thesis"), None)
    assert FORMAL_E2 not in caps and LLM_RUNTIME_FREEZE not in caps


def test_archiving_removes_every_execution_capability():
    from pcmef.platform.capabilities import capabilities_for
    from pcmef.platform.projects.models import Project

    archived = Project(project_id="p", display_name="P", template="blank",
                       archived=True)
    assert capabilities_for(archived) == frozenset()


# ---------------------------------------------------------------------------
# P1-4 legacy 邊界
# ---------------------------------------------------------------------------


def _plant(runs, run_id, started_at):
    directory = runs / run_id
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({
        "run_id": run_id, "kind": "sim", "label": run_id, "params": {},
        "command": ["x"], "status": "succeeded", "started_at": started_at,
        "finished_at": started_at, "exit_code": 0, "note": "",
    }), encoding="utf-8")
    (directory / "log.txt").write_text("x", encoding="utf-8")


def test_a_run_from_before_the_boundary_is_honoured_as_legacy(env):
    client, runs = env
    _plant(runs, "old-legacy", "2026-09-01T10:00:00+08:00")
    _select(client, "pcmef-thesis")
    assert client.get("/console/runs/old-legacy").status_code == 200


def test_a_new_run_missing_its_attribution_is_not_treated_as_legacy(env):
    """刪掉歸屬檔不得把任何 run 變成碩論的（P1-4）。"""
    client, runs = env
    _plant(runs, "new-orphan", "2026-09-09T10:00:00+08:00")
    _select(client, "pcmef-thesis")
    assert client.get("/console/runs/new-orphan").status_code == 404


def test_deleting_the_attribution_of_a_real_run_orphans_it(env):
    client, runs = env
    _select(client, "pcmef-thesis")
    run_id = _start(client)
    (runs / run_id / "run_identity.json").unlink()

    assert client.get(f"/console/runs/{run_id}").status_code == 404
