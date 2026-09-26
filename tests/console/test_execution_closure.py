# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；對 Execution / Action Layer 的**閉合**做
#         對抗性測試。全部在 tmp_path，不觸發任何真正的科研寫入。
# 檔案路徑: tests/console/test_execution_closure.py
# 產生時間: 2026-09-06 23:10 +08:00
# 版本: v0.1.0
# 功能說明: 動作層閉合第二輪 —— 啟動交易的唯一性、行程未起跑時的
#           回滾、邊界的 fail-closed、回退情境下的拒絕、跨專案隔離、
#           歷史 run 完全由快照還原、能力受 Profile 狀態約束，
#           以及「Web 不得假造進度」。
# 模組定位: test_execution_guards.py 的續作。前者證明「有守衛」，
#           本檔證明「沒有繞過守衛的路」—— 每一條都對應一個**目前
#           仍可達**的狀態，而不是已經修好的舊缺陷。
# 主要責任:
#   1. 所有會啟動行程的端點都經同一個歸屬交易
#   2. 行程未成功 launch 時不得留下任何 run
#   3. 邊界檔壞掉或寫不進去時不得改用「現在時間」
#   4. context 回退（封存 → Thesis）時不得執行動作
#   5. 沒有能力的專案讀不到碩論的 formal report 與 E1 gate
#   6. 歷史 run 的流程完全由 pipeline_snapshot 還原
#   7. Profile 狀態參與能力判定
#   8. Web 不得寫 stage 事件，也不得在沒有事件時顯示進度
# 維護提醒:
#   - **不得把任何一條的預期改成 200 / 302 / True。** 這一層的錯誤
#     全部長得像成功。
#   - 不得為了讓測試通過而放寬 owned_by() 或 capabilities_for()。
#     fixture 該補的是合法的啟動路徑，不是更寬的判準。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_closure.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

flask = pytest.importorskip("flask")


# ---------------------------------------------------------------------------
# 共用夾具
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    """一個乾淨的 workspace，內含 legacy Thesis 與一個空專案。

    `workspace_root` 一定要給：不給的話 registry 會落在 repo 根目錄，
    測試就會在開發者真實的 projects/ 底下建立與封存專案。

    **formal 的輸出位置同樣一定要改掉。** 預設值是 repo 相對路徑
    `outputs/perception/e2_final` —— 那是 canonical Final E2 的位置。
    測試若沿用它，一個假的 report 就會落在真正的科研輸出目錄裡，
    而那個目錄不進版控，所以沒有任何東西會告訴你它被寫過。
    """
    from pcmef.admin.app import create_app

    runs = tmp_path / "runs"
    app = create_app(
        registry_path=tmp_path / "registry.db",
        vault_path=tmp_path / "vault",
        console_run_root=runs,
        workspace_root=tmp_path / "workspace",
        environ={
            "PCMEF_FORMAL_OUT": str(tmp_path / "formal_out"),
            "PCMEF_FORMAL_BASE": str(tmp_path / "formal_base"),
            "PCMEF_FORMAL_DRY_RUN_BASE": str(tmp_path / "dry_run_base"),
        },
    )
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/projects")
    client.post("/projects/create", data={
        "project_id": "tiny-dummy", "display_name": "Tiny", "template": "blank",
    }, follow_redirects=True)
    return client, runs


def _select(client, project_id: str) -> None:
    client.post("/projects/select", data={"project_id": project_id},
                follow_redirects=True)


def _token(client) -> str:
    """從 session 取 CSRF token。實作見 tests/console/conftest.py。

    沒有 executor 或沒有能力的專案不會畫出表單，因此不能從畫面上刮。
    """
    import importlib.util

    location = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_console_helpers", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.csrf_token(client)


def _run_ids(runs: Path) -> set[str]:
    return {p.name for p in runs.iterdir() if p.is_dir()} if runs.exists() else set()


class _FakeProcess:
    """假的子行程。**不執行任何東西**，但長得夠像可以被 pump。"""

    def __init__(self, *_args, **_kwargs) -> None:
        self.stdout = iter(())
        self.returncode = 0

    def wait(self) -> int:
        return 0


@pytest.fixture()
def no_subprocess(monkeypatch):
    """把 Popen 換掉。真的啟動會跑起模擬或整組實驗。"""
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen",
        lambda *a, **k: _FakeProcess(),
    )


# ---------------------------------------------------------------------------
# P0-1 每一個啟動端點都經同一個歸屬交易
# ---------------------------------------------------------------------------


def test_formal_start_stamps_attribution(env, no_subprocess):
    """`/formal/start` 先前直接 runner.start()，啟動的 run 沒有主人。"""
    client, runs = env
    _select(client, "pcmef-thesis")

    response = client.post("/formal/start", data={
        "mode": "dry-run", "csrf_token": _token(client),
    })
    assert response.status_code in (201, 302)

    run_id = next(iter(_run_ids(runs)))
    identity = runs / run_id / "run_identity.json"
    assert identity.is_file(), "a formal run must carry its own attribution"
    assert json.loads(identity.read_text(encoding="utf-8"))["project_id"] == (
        "pcmef-thesis"
    )


def test_llm_snapshot_stamps_attribution(env, no_subprocess):
    """寫 lock 的那條路同樣必須留下「是誰在什麼身分下按的」。"""
    client, runs = env
    _select(client, "pcmef-thesis")

    response = client.post("/api/console/llm-snapshot", data={
        "freeze": "1", "csrf_token": _token(client),
    })
    assert response.status_code in (201, 302)

    run_id = next(iter(_run_ids(runs)))
    identity = runs / run_id / "run_identity.json"
    assert identity.is_file(), "a freeze run must carry its own attribution"
    assert json.loads(identity.read_text(encoding="utf-8"))["project_id"] == (
        "pcmef-thesis"
    )


def test_no_route_module_starts_a_run_outside_the_launch_transaction():
    """判準只能有一份。散開的 runner.start() 遲早會漏掉一個。

    唯一允許直接呼叫 `runner.start()` 的地方是啟動交易本身；
    route 模組一律經過它。
    """
    routes = Path("pcmef/console")
    offenders = []
    for path in sorted(routes.glob("*.py")):
        if path.name == "launch.py":
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"runner\.start\(|\.start\(RunSpec", line):
                offenders.append(f"{path.as_posix()}:{number}")
    assert not offenders, (
        "these call the runner directly and therefore bypass attribution: "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# P0-2 行程沒起跑就不得留下 run
# ---------------------------------------------------------------------------


def test_a_failed_process_launch_leaves_no_run(env, monkeypatch):
    """歸屬寫完之後才失敗的那一段，先前會留下一筆沒有行程的 run。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    before = _run_ids(runs)

    def explode(*_args, **_kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr("pcmef.console.runner.subprocess.Popen", explode)
    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })

    assert response.status_code >= 400, "a run that never launched is not a success"
    assert _run_ids(runs) == before, "a run that never launched must leave nothing"


def test_a_failed_command_build_leaves_no_run(env):
    """指令組不出來也一樣：歸屬已經落盤，但沒有任何東西在跑。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    before = _run_ids(runs)

    with patch("pcmef.console.runner.ConsoleRunner._command",
               side_effect=RuntimeError("bad config")):
        response = client.post("/api/console/runs", data={
            "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
        })

    assert response.status_code >= 400
    assert _run_ids(runs) == before


def test_a_failed_formal_launch_leaves_no_run(env, monkeypatch):
    client, runs = env
    _select(client, "pcmef-thesis")
    before = _run_ids(runs)

    def explode(*_args, **_kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr("pcmef.console.runner.subprocess.Popen", explode)
    response = client.post("/formal/start", data={
        "mode": "dry-run", "csrf_token": _token(client),
    })

    assert response.status_code >= 400
    assert _run_ids(runs) == before


# ---------------------------------------------------------------------------
# P0-3 邊界 fail-closed
# ---------------------------------------------------------------------------


def test_a_corrupt_boundary_is_not_replaced_by_now(tmp_path):
    """壞掉的邊界不得當成「現在」。

    用「現在」補的邊界會讓所有既有 run 瞬間落在邊界之前 —— 於是
    刪掉歸屬檔就能把任何一筆變成碩論的，正是邊界要防的那件事。
    """
    from pcmef.platform.runs import AttributionBoundaryError, attribution_boundary

    root = tmp_path / "runs"
    root.mkdir()
    (root / ".attribution_boundary.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(AttributionBoundaryError):
        attribution_boundary(root)


def test_a_boundary_missing_its_field_is_not_replaced_by_now(tmp_path):
    from pcmef.platform.runs import AttributionBoundaryError, attribution_boundary

    root = tmp_path / "runs"
    root.mkdir()
    (root / ".attribution_boundary.json").write_text(
        json.dumps({"note": "installed_at is missing"}), encoding="utf-8"
    )

    with pytest.raises(AttributionBoundaryError):
        attribution_boundary(root)


def test_an_unwritable_boundary_is_not_replaced_by_now(tmp_path, monkeypatch):
    from pcmef.platform.runs import AttributionBoundaryError, attribution_boundary

    root = tmp_path / "runs"
    root.mkdir()

    def refuse(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("pcmef.platform.runs.attribution.os.open", refuse)
    with pytest.raises(AttributionBoundaryError):
        attribution_boundary(root)


def test_the_boundary_is_not_truncated_to_whole_seconds(tmp_path):
    """截到秒等於把邊界往前挪最多一秒。

    那一秒內建立的 run 明明在安裝前就存在，卻會落到邊界之後被判成
    孤兒 —— 差別只在毫秒，所以它只在機器忙的時候出現，看起來像
    「偶爾壞掉」而不是規則錯了。
    """
    from datetime import datetime

    from pcmef.platform.runs import attribution_boundary

    before = datetime.now().astimezone()
    installed = datetime.fromisoformat(attribution_boundary(tmp_path / "runs"))

    assert installed >= before.replace(microsecond=before.microsecond), (
        "the boundary must not predate the moment it was installed"
    )
    assert installed.microsecond or before.microsecond == 0, (
        "the boundary stamp keeps sub-second precision"
    )


def test_a_run_created_just_before_the_boundary_is_legacy(env):
    """安裝前一瞬間建立的 run 必須是 legacy，不是孤兒。"""
    import json as _json
    from datetime import datetime, timedelta

    client, runs = env
    boundary = datetime.fromisoformat(
        _json.loads(
            (runs / ".attribution_boundary.json").read_text(encoding="utf-8")
        )["installed_at"]
    )
    just_before = (boundary - timedelta(milliseconds=1)).isoformat()

    directory = runs / "just-before"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run_id": "just-before", "kind": "sim_smoke", "label": "x", "params": {},
        "command": ["x"], "status": "succeeded", "started_at": just_before,
        "finished_at": just_before, "exit_code": 0, "note": "",
    }), encoding="utf-8")
    (directory / "log.txt").write_text("x", encoding="utf-8")

    _select(client, "pcmef-thesis")
    assert client.get("/console/runs/just-before").status_code == 200


def test_a_corrupt_boundary_denies_an_unattributed_run(env):
    """邊界不可信時，沒有歸屬的 run 誰都不擁有。"""
    client, runs = env
    _select(client, "pcmef-thesis")

    legacy = runs / "old-legacy"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "run.json").write_text(json.dumps({
        "run_id": "old-legacy", "kind": "sim_smoke", "label": "legacy", "params": {},
        "command": ["x"], "status": "succeeded",
        "started_at": "2026-01-01T10:00:00+08:00",
        "finished_at": "2026-01-01T10:00:00+08:00", "exit_code": 0, "note": "",
    }), encoding="utf-8")
    (legacy / "log.txt").write_text("x", encoding="utf-8")
    assert client.get("/console/runs/old-legacy").status_code == 200

    (runs / ".attribution_boundary.json").write_text("{ broken", encoding="utf-8")
    assert client.get("/console/runs/old-legacy").status_code == 404


# ---------------------------------------------------------------------------
# P0-4 回退情境不得執行動作
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path, payload", [
    ("/api/console/runs", {"kind": "sim_smoke", "preset": "standard"}),
    ("/formal/start", {"mode": "dry-run"}),
    ("/api/console/llm-snapshot", {"freeze": "1"}),
])
def test_an_archived_project_cannot_act_as_the_thesis(env, path, payload,
                                                      no_subprocess):
    """封存 → 回退到 Thesis → 用碩論的能力執行。**這條路必須斷掉。**

    先前這幾個端點只被 CSRF 擋住，能力檢查看到的已經是回退後的
    Thesis，因此一旦帶著合法 token 就會真的跑起來。
    """
    client, runs = env
    _select(client, "tiny-dummy")
    token = _token(client)
    client.post("/projects/tiny-dummy/archive", follow_redirects=True)
    before = _run_ids(runs)

    response = client.post(path, data={**payload, "csrf_token": token})

    assert response.status_code in (403, 409), (
        "an action must not run under a project the user did not choose"
    )
    assert _run_ids(runs) == before


def test_a_fell_back_context_is_refused_even_for_a_deleted_project(env,
                                                                   no_subprocess):
    """session 指向一個已經不存在的專案時同樣不得代打。"""
    import shutil

    client, runs = env
    _select(client, "tiny-dummy")
    token = _token(client)
    workspace = Path(client.application.config["PCMEF_WORKSPACE_ROOT"])
    shutil.rmtree(workspace / "projects" / "tiny-dummy", ignore_errors=True)
    before = _run_ids(runs)

    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": token,
    })

    assert response.status_code in (403, 409)
    assert _run_ids(runs) == before


def test_an_explicitly_selected_thesis_still_runs(env, no_subprocess):
    """對照組：拒絕的是「回退」，不是「Thesis」。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    assert response.status_code in (201, 302)
    assert _run_ids(runs)


# ---------------------------------------------------------------------------
# P1-5 Results / Status 的專案隔離
# ---------------------------------------------------------------------------


def test_a_blank_project_sees_no_thesis_formal_report(env, tmp_path):
    """Blank Project 的 Results 先前讀的是全域 formal report。"""
    client, _runs = env
    out = Path(client.application.config["PCMEF_FORMAL_OUT"])
    assert not out.is_absolute() or tmp_path in out.parents or out == tmp_path / (
        "formal_out"
    ), "this test must never write into the canonical Final E2 directory"
    out.mkdir(parents=True, exist_ok=True)
    (out / "formal_e2_report.json").write_text(
        json.dumps({"experiment": "E2-FINAL", "macro_f1": 0.987654321}),
        encoding="utf-8",
    )

    _select(client, "tiny-dummy")
    body = client.get("/results").get_data(as_text=True)

    assert "0.987654321" not in body
    assert "E2-FINAL" not in body


def test_a_blank_project_sees_no_thesis_e1_gate(env):
    """E1 十二道 gate 是碩論的科研狀態，不是平台功能。"""
    client, _runs = env
    _select(client, "tiny-dummy")
    body = client.get("/status").get_data(as_text=True)

    assert "E1-" not in body
    for leaked in ("AMD-007", "PFC-001", "canonical_golden_baseline"):
        assert leaked not in body


def test_the_thesis_still_sees_its_own_status(env):
    """對照組：隔離不能只是把所有人都擋掉。"""
    client, _runs = env
    _select(client, "pcmef-thesis")
    assert client.get("/status").status_code == 200
    assert client.get("/results").status_code == 200


# ---------------------------------------------------------------------------
# P1-6 歷史 run 完全由快照還原
# ---------------------------------------------------------------------------


def test_a_historical_run_is_rebuilt_from_its_snapshot_alone(env, no_subprocess):
    """provider 之後改了文案，歷史 run 不得被重新解釋。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    run_id = next(iter(_run_ids(runs)))
    snapshot = json.loads(
        (runs / run_id / "run_identity.json").read_text(encoding="utf-8")
    )["pipeline_snapshot"]
    assert snapshot["stages"], "the snapshot must carry the full semantics"
    first_stage_name = snapshot["stages"][0]["display_name"]

    def rewritten(*_args, **_kwargs):
        raise RuntimeError("the live provider is gone")

    with patch("pcmef.platform.pipeline.build_definition", side_effect=rewritten), \
         patch("pcmef.platform.pipeline.registry.build_definition",
               side_effect=rewritten):
        body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)

    assert first_stage_name in body, (
        "a historical run must be readable without asking the live provider"
    )
    assert f"{len(snapshot['stages'])} 個 stage" in body or str(
        len(snapshot["stages"])
    ) in body


def test_the_snapshot_survives_a_pipeline_definition_round_trip():
    """快照必須能無損還原，否則「完全由快照還原」只是換個地方猜。"""
    from pcmef.platform.pipeline.models import PipelineDefinition, PipelineStage

    original = PipelineDefinition(
        pipeline_id="p", display_name="P", note="n",
        stages=(
            PipelineStage(
                stage_id="a", display_name="甲", english="A", summary="s",
                carries="c", input_desc="i", process_desc="pr", output_desc="o",
                artifact_roles=("r1",), optional=False,
            ),
            PipelineStage(
                stage_id="b", display_name="乙", depends_on=("a",), optional=True,
            ),
        ),
    )
    restored = PipelineDefinition.from_json(original.to_json())

    assert restored.to_json() == original.to_json()
    assert restored.stages[0].input_desc == "i"
    assert restored.stages[1].optional is True


# ---------------------------------------------------------------------------
# P1-7 Profile 狀態參與能力判定
# ---------------------------------------------------------------------------


def test_an_inhibited_profile_grants_no_capability():
    """RUN_INHIBITED 的字面意思就是不能啟動。"""
    from pcmef.platform.capabilities import capabilities_for
    from pcmef.platform.profiles.models import Profile
    from pcmef.platform.projects.models import Project

    project = Project(project_id="p", display_name="P", template="blank",
                      extra={"capabilities": ["formal_e2"]})
    inhibited = Profile(profile_id="f", project_id="p", display_name="F",
                        state="RUN_INHIBITED")

    assert capabilities_for(project, inhibited) == frozenset()


def test_formal_e2_needs_a_frozen_scientific_identity():
    """一次性正式實驗不得跑在還會被改動的設定上。"""
    from pcmef.platform.capabilities import FORMAL_E2, capabilities_for
    from pcmef.platform.profiles.models import Profile
    from pcmef.platform.projects.models import Project

    project = Project(project_id="p", display_name="P", template="blank",
                      extra={"capabilities": ["formal_e2"]})
    draft = Profile(profile_id="f", project_id="p", display_name="F", state="DRAFT")
    frozen = Profile(profile_id="f", project_id="p", display_name="F", state="FROZEN")

    assert FORMAL_E2 not in capabilities_for(project, draft)
    assert FORMAL_E2 in capabilities_for(project, frozen)


def test_declared_capabilities_need_a_selected_profile():
    """沒有選定 Profile 就沒有科學身分，動不了科研狀態。"""
    from pcmef.platform.capabilities import (
        FORMAL_E2, LLM_RUNTIME_FREEZE, RUN_SIMULATION, capabilities_for,
    )
    from pcmef.platform.projects.models import Project

    project = Project(project_id="p", display_name="P", template="blank",
                      extra={"capabilities": ["formal_e2", "llm_runtime_freeze"]})
    caps = capabilities_for(project, None)

    assert RUN_SIMULATION in caps
    assert FORMAL_E2 not in caps and LLM_RUNTIME_FREEZE not in caps


def test_capabilities_actually_read_the_profile_argument():
    """先前 profile 參數收下就丟掉 —— 簽章說它有用，實作說沒有。"""
    from pcmef.platform.capabilities import capabilities_for
    from pcmef.platform.profiles.models import Profile
    from pcmef.platform.projects.models import Project

    project = Project(project_id="p", display_name="P", template="blank",
                      extra={"capabilities": ["formal_e2"]})
    states = {
        state: capabilities_for(
            project,
            Profile(profile_id="f", project_id="p", display_name="F", state=state),
        )
        for state in ("DRAFT", "FROZEN", "RUN_INHIBITED")
    }
    assert len({frozenset(v) for v in states.values()}) == 3, (
        "profile state must change the answer, otherwise the argument is a lie"
    )


# ---------------------------------------------------------------------------
# P1-8 舊 project.json 的能力補寫
# ---------------------------------------------------------------------------


def test_a_pre_capability_thesis_record_is_backfilled(tmp_path):
    """平台化之前寫下的 project.json 沒有 capabilities 欄位。

    不補的話 Thesis 自己失去 formal_e2 —— 畫面顯示「這個專案沒有
    Formal E2 能力」，而它正是那篇論文。
    """
    from pcmef.platform.capabilities import FORMAL_E2, LLM_RUNTIME_FREEZE
    from pcmef.platform.projects.registry import ProjectRegistry
    from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID

    registry = ProjectRegistry(root=tmp_path)
    record = tmp_path / "projects" / LEGACY_THESIS_PROJECT_ID / "project.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({
        "schema_version": "project_v1",
        "project_id": LEGACY_THESIS_PROJECT_ID,
        "display_name": "PC-MEF Thesis",
        "template": LEGACY_THESIS_PROJECT_ID,
        "legacy_layout": True,
        "default_profile_id": "thesis-frozen",
        "extra": {},
    }, ensure_ascii=False), encoding="utf-8")

    project = registry.ensure_legacy_thesis_project()
    declared = set(project.extra.get("capabilities", ()))

    assert {FORMAL_E2, LLM_RUNTIME_FREEZE} <= declared


def test_the_backfill_is_idempotent_and_keeps_other_metadata(tmp_path):
    """補寫只補缺的那一欄。蓋掉別的欄位就是用遷移改資料。"""
    from pcmef.platform.projects.registry import ProjectRegistry
    from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID

    registry = ProjectRegistry(root=tmp_path)
    record = tmp_path / "projects" / LEGACY_THESIS_PROJECT_ID / "project.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({
        "schema_version": "project_v1",
        "project_id": LEGACY_THESIS_PROJECT_ID,
        "display_name": "My Renamed Thesis",
        "template": LEGACY_THESIS_PROJECT_ID,
        "legacy_layout": True,
        "extra": {"note": "keep me"},
    }, ensure_ascii=False), encoding="utf-8")

    first = registry.ensure_legacy_thesis_project()
    written = json.loads(record.read_text(encoding="utf-8"))
    second = registry.ensure_legacy_thesis_project()

    assert first.display_name == "My Renamed Thesis"
    assert first.extra["note"] == "keep me"
    assert second.extra == first.extra
    assert json.loads(record.read_text(encoding="utf-8")) == written


def test_the_backfill_does_not_touch_other_projects(tmp_path):
    """補寫只針對 legacy 專案。對別人補等於發能力。"""
    from pcmef.platform.capabilities import FORMAL_E2, capabilities_for
    from pcmef.platform.projects.registry import ProjectRegistry

    registry = ProjectRegistry(root=tmp_path)
    other = registry.create("other", "Other", template="blank")
    registry.ensure_legacy_thesis_project()

    assert FORMAL_E2 not in capabilities_for(registry.get(other.project_id))


# ---------------------------------------------------------------------------
# P1-9 Web 不得假造進度
# ---------------------------------------------------------------------------


def test_no_web_module_writes_stage_events():
    """executor 是事件的唯一來源。Web 能補寫，畫面就能顯示沒發生過的進度。"""
    offenders = []
    for folder in ("pcmef/console", "pcmef/admin"):
        for path in sorted(Path(folder).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if "RunEventWriter" in line or re.search(r"\bemit_\w+\(", line):
                    offenders.append(f"{path.as_posix()}:{number}")
    assert not offenders, f"web modules must not write run events: {offenders}"


def test_a_run_without_events_reports_no_progress(env, no_subprocess):
    """沒有事件就說沒有事件，不得畫一條估出來的進度條。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    run_id = next(iter(_run_ids(runs)))
    assert not (runs / run_id / "stage_events.jsonl").exists()

    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "沒有留下 stage 事件" in body
    assert "runstage-bar" not in body, "no bar may be drawn without events"
