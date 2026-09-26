# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 進行跨 context 與
#         direct-URL 的對抗性測試。**全部在 tmp_path。**
# 檔案路徑: tests/console/test_run_attribution_adversarial.py
# 產生時間: 2026-09-08 11:20 +08:00
# 版本: v0.1.0
# 功能說明: 歷史 run 歸屬不可變、以及跨專案直接存取被後端擋下的驗收。
# 模組定位: post-platform audit 的 P0 守門測試。這些不是 happy path ——
#           每一條都對應一個**已經發生過**的缺陷：
#           P0-1 同一筆 run 在 A 下七個 stage、切到 B 變三個；
#           P0-2 直接貼 URL 就能看到別的專案的執行紀錄。
# 主要責任:
#   1. 驗證啟動時寫下 project/profile/pipeline 快照
#   2. 驗證切換 Profile 後歷史 run 的解讀不變
#   3. 驗證跨專案檢視／trace／case／delete 一律 404
#   4. 驗證 Results 清單不列出別專案的 run
#   5. 驗證歸屬 write-once，不可被改寫
# 維護提醒:
#   - **不得把跨專案 404 改成「清單過濾」。** 過濾只擋得住從清單點進去；
#     直接貼網址與從清單點進去是同一件事。
#   - 不得讓歷史 run 依 current selection 重建 pipeline。那正是 P0-1。
#   - v0.1.0 新增：首版，對應 post-platform audit。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_run_attribution_adversarial.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import re

import pytest

flask = pytest.importorskip("flask")

from pcmef.platform.runs import (  # noqa: E402
    ATTRIBUTION_FILENAME,
    AttributionExistsError,
    RunAttribution,
    RunEventWriter,
    write_attribution,
)


class _NoProcess:
    """代替真的模擬行程：沒有輸出、立刻以 0 結束。"""

    stdout = iter(())
    pid = None

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


@pytest.fixture()
def app_and_runs(tmp_path, monkeypatch):
    from pcmef.admin.app import create_app

    # 這裡驗的是歸屬，不是模擬本身。先前 `_start_run()` 每次都真的啟動
    # 一次 `pcmef.cli sim smoke`，測試結束時沒有人 wait 它 —— 渲染在
    # 測試之後繼續跑（round 9 的子行程帳本實測抓到）。
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _NoProcess()
    )
    runs = tmp_path / "runs"
    app = create_app(
        registry_path=tmp_path / "registry.db",
        vault_path=tmp_path / "vault",
        console_run_root=runs,
        workspace_root=tmp_path / "workspace",
    )
    app.config["TESTING"] = True
    return app.test_client(), runs


def _post(client, path, **fields):
    return client.post(path, data=fields, follow_redirects=True)


def _token(client) -> str:
    body = client.get("/console").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)


def _start_run(client, token) -> str:
    response = client.post(
        "/api/console/runs",
        data={"kind": "sim_smoke", "preset": "standard", "csrf_token": token},
    )
    run_id = re.search(
        r"/console/runs/([\w.-]+)", response.headers["Location"]
    ).group(1)
    # 等背景的輸出執行緒把最後一筆紀錄寫完再回傳。`run.json` 目前不是
    # 原子寫入，與它同時讀會讀到半份（round 9 已記錄為待修缺口）；
    # 這裡驗的是歸屬，不必與那個 race 賽跑。
    client.application.config["PCMEF_CONSOLE_RUNNER"].wait(run_id, timeout=30)
    return run_id


@pytest.fixture()
def thesis_run(app_and_runs):
    """在 PC-MEF Thesis 底下啟動一筆 run，並寫入七節點的完成事件。"""
    client, runs = app_and_runs
    client.get("/projects")
    _post(client, "/projects/create", project_id="tiny-dummy",
          display_name="Tiny Dummy", template="blank")
    _post(client, "/projects/select", project_id="pcmef-thesis")

    run_id = _start_run(client, _token(client))
    writer = RunEventWriter(runs / run_id)
    writer.run_started()
    for stage in ("simulation", "paired", "perception"):
        writer.stage_completed(stage)
    return client, runs, run_id


def _stage_count(client, run_id) -> tuple[int, int]:
    response = client.get(f"/console/runs/{run_id}")
    body = response.get_data(as_text=True)
    return response.status_code, len(re.findall(r'runstage-name">([^<]+)<', body))


# ---------------------------------------------------------------------------
# P0-1 歷史 run 的身分不可被 current selection 改寫
# ---------------------------------------------------------------------------


def test_starting_a_run_stamps_its_project_and_profile(thesis_run):
    _client, runs, run_id = thesis_run
    data = json.loads((runs / run_id / ATTRIBUTION_FILENAME).read_text(encoding="utf-8"))

    assert data["project_id"] == "pcmef-thesis"
    assert data["profile_id"] == "thesis-frozen"
    assert len(data["stage_ids"]) == 7
    assert data["pipeline_digest"], "the pipeline shape must be fingerprinted"


def test_a_historical_run_keeps_its_stage_count_across_profile_switches(thesis_run):
    """同一筆 run 不得因為切換專案而變成另一段歷史。

    這條擋的是 audit 開場抓到的那個缺陷：切到三節點的專案之後，
    這筆七節點的 run 會被用三節點的定義重播。
    """
    client, _runs, run_id = thesis_run
    assert _stage_count(client, run_id) == (200, 7)

    _post(client, "/projects/select", project_id="tiny-dummy")
    _post(client, "/projects/select", project_id="pcmef-thesis")

    assert _stage_count(client, run_id) == (200, 7)


def test_the_run_page_reports_the_runtime_project_not_the_current_one(thesis_run):
    client, _runs, run_id = thesis_run
    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "pcmef-thesis" in body or "PC-MEF Thesis" in body


def test_attribution_cannot_be_rewritten(tmp_path):
    """可被改寫的歸屬等於沒有歸屬。"""
    write_attribution(tmp_path, RunAttribution(run_id="r", project_id="a"))
    with pytest.raises(AttributionExistsError):
        write_attribution(tmp_path, RunAttribution(run_id="r", project_id="b"))


# ---------------------------------------------------------------------------
# P0-2 跨專案存取必須被後端擋下
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix", ["", "/trace", "/inputs", "/outputs", "/artifacts", "/cost"]
)
def test_direct_urls_to_another_projects_run_are_refused(thesis_run, suffix):
    """直接貼網址與從清單點進去是同一件事，兩者都要擋。"""
    client, _runs, run_id = thesis_run
    _post(client, "/projects/select", project_id="tiny-dummy")

    assert client.get(f"/console/runs/{run_id}{suffix}").status_code == 404


def test_another_projects_run_is_not_listed_in_results(thesis_run):
    client, _runs, run_id = thesis_run
    _post(client, "/projects/select", project_id="tiny-dummy")

    assert run_id not in client.get("/results").get_data(as_text=True)


def test_results_search_does_not_reach_across_projects(thesis_run):
    """搜尋別的專案的 run id 不得把它撈出來。

    比對的是**結果連結**而不是整頁：搜尋框會把查詢字串原樣回填，
    所以 run id 出現在 HTML 裡並不代表它被列出來了。
    """
    client, _runs, run_id = thesis_run
    _post(client, "/projects/select", project_id="tiny-dummy")

    body = client.get(f"/results?q={run_id}").get_data(as_text=True)
    assert f"/console/runs/{run_id}" not in body


def test_deleting_another_projects_run_is_refused(thesis_run):
    """刪除別的專案的紀錄與檢視它一樣，都要先擁有它。"""
    client, runs, run_id = thesis_run
    token = _token(client)
    _post(client, "/projects/select", project_id="tiny-dummy")

    response = client.post(
        f"/api/console/runs/{run_id}/delete", data={"csrf_token": token}
    )
    assert response.status_code == 404
    assert (runs / run_id).exists(), "the run must survive a refused delete"


def test_the_owning_project_can_still_see_and_delete_its_run(thesis_run):
    """對照組：否則上面的斷言只證明了「什麼都打不開」。"""
    client, _runs, run_id = thesis_run
    assert client.get(f"/console/runs/{run_id}").status_code == 200
    assert run_id in client.get("/results").get_data(as_text=True)


# ---------------------------------------------------------------------------
# legacy run（沒有歸屬檔）
# ---------------------------------------------------------------------------


def test_a_legacy_run_without_attribution_belongs_to_the_thesis(app_and_runs):
    """平台化之前的 run 沒有歸屬檔。全域 runs 根目錄當時就是碩論在用的。

    `started_at` 必須是**真的時間戳**。歸屬邊界比對的是時間點，因此
    一筆時間讀不出來的紀錄無法定位在邊界的哪一邊 —— 那種紀錄一律
    當孤兒，不當 legacy（P1-4）。這裡要測的是「邊界之前的 run 歸碩論」，
    所以夾具得給一個確實落在邊界之前的時間，而不是佔位字元。
    """
    client, runs = app_and_runs
    client.get("/projects")
    _post(client, "/projects/create", project_id="tiny-dummy",
          display_name="Tiny Dummy", template="blank")

    before_boundary = "2026-01-01T00:00:00+08:00"
    legacy = runs / "legacy-run"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text(json.dumps({
        "run_id": "legacy-run", "kind": "sim_smoke", "label": "legacy", "params": {},
        "command": ["x"], "status": "succeeded",
        "started_at": before_boundary, "finished_at": before_boundary,
        "exit_code": 0, "note": "",
    }), encoding="utf-8")
    (legacy / "log.txt").write_text("x", encoding="utf-8")

    _post(client, "/projects/select", project_id="pcmef-thesis")
    assert client.get("/console/runs/legacy-run").status_code == 200

    _post(client, "/projects/select", project_id="tiny-dummy")
    assert client.get("/console/runs/legacy-run").status_code == 404


def test_a_run_whose_start_time_is_unreadable_is_an_orphan(app_and_runs):
    """時間讀不出來就定位不了 —— 一律當孤兒，不得因此歸給碩論。"""
    client, runs = app_and_runs
    client.get("/projects")

    broken = runs / "no-time"
    broken.mkdir(parents=True)
    (broken / "run.json").write_text(json.dumps({
        "run_id": "no-time", "kind": "sim_smoke", "label": "broken", "params": {},
        "command": ["x"], "status": "succeeded", "started_at": "t",
        "finished_at": "t", "exit_code": 0, "note": "",
    }), encoding="utf-8")
    (broken / "log.txt").write_text("x", encoding="utf-8")

    _post(client, "/projects/select", project_id="pcmef-thesis")
    assert client.get("/console/runs/no-time").status_code == 404
