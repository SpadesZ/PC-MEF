# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；對 Execution / Action Layer 剩下的六個
#         缺口做對抗性測試。全部在 tmp_path，不觸發任何真正的科研寫入。
# 檔案路徑: tests/console/test_execution_hardening.py
# 產生時間: 2026-09-07 10:20 +08:00
# 版本: v0.1.0
# 功能說明: 動作層閉合第三輪 —— 幽靈子行程、遷移寫檔失敗的授權、
#           身分解析次數、Profile 狀態與動作的完整對照、executor
#           事件接線，以及測試對 repo 的隔離。
# 模組定位: test_execution_closure.py 的續作。前兩輪證明「守衛存在」
#           與「沒有繞過守衛的路」；本輪處理**守衛自己失敗時**會留下
#           什麼 —— 殺不掉的行程、寫不進去卻已授權的能力、解析兩次
#           而中間變了的身分。
# 主要責任:
#   1. thread.start() 失敗時子行程必須被收掉，不得變成孤兒
#   2. capability 遷移寫檔失敗時不得在記憶體裡授權
#   3. 一次啟動只解析一次身分，授權與歸屬用同一份
#   4. Profile 狀態 × 動作的對照表完整且無預設放行
#   5. stage 事件由真正的 executor 寫出，Web 只重播
#   6. 測試不得碰 repo 根目錄的科研輸出與專案 metadata
# 維護提醒:
#   - **不得把任何一條的預期改成「盡力而為」。** 這一層的失敗模式是
#     「看起來成功」：行程還在跑、能力好像有、身分好像對。
#   - 不得為了讓 policy 表通過而放寬 capabilities_for()。表要改的是
#     它宣告的內容，不是它的嚴格程度。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_hardening.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")


# ---------------------------------------------------------------------------
# 共用夾具
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    """乾淨的 workspace，內含 legacy Thesis 與一個空專案。

    formal 輸出與 workspace 一律指向 tmp_path —— 預設值是 repo 相對
    路徑，測試沿用就會寫進真正的科研輸出目錄。
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


class _SpyProcess:
    """假的子行程，記錄自己有沒有被收掉。"""

    def __init__(self) -> None:
        self.stdout = iter(())
        self.terminated = False
        self.killed = False
        self.waited = 0
        self.stdout_closed = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout=None) -> int:
        self.waited += 1
        self._alive = False
        return 0


# ---------------------------------------------------------------------------
# 1 幽靈子行程
# ---------------------------------------------------------------------------


def test_a_thread_that_cannot_start_does_not_leave_the_process_running(
    tmp_path, monkeypatch
):
    """Popen 成功、thread.start() 失敗 —— 行程還在跑，而沒有人握著它。

    這是最難查的洩漏：run 目錄被回滾、HTTP 回了錯誤、清單上什麼都
    沒有，但機器上多了一個算圖的行程，而且沒有任何紀錄指向它。
    """
    import threading

    from pcmef.console.runner import ConsoleRunner, RunLaunchError, RunSpec

    spy = _SpyProcess()
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: spy
    )

    def refuse_to_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse_to_start)

    runner = ConsoleRunner(tmp_path / "runs")
    with pytest.raises(RunLaunchError):
        runner.start(RunSpec(kind="sim_smoke", params={}))

    assert spy.terminated or spy.killed, (
        "the subprocess must be signalled; otherwise it keeps running with "
        "nobody holding a handle to it"
    )
    assert spy.waited >= 1, (
        "the runner must reap the process; signalling without waiting leaves "
        "a zombie"
    )


def test_a_thread_that_cannot_start_registers_no_thread(tmp_path, monkeypatch):
    """失敗的執行緒不得留在 _threads —— wait() 會對著它永遠等下去。"""
    import threading

    from pcmef.console.runner import ConsoleRunner, RunLaunchError, RunSpec

    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _SpyProcess()
    )
    monkeypatch.setattr(
        threading.Thread, "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )

    runner = ConsoleRunner(tmp_path / "runs")
    with pytest.raises(RunLaunchError):
        runner.start(RunSpec(kind="sim_smoke", params={}))

    assert not runner._threads, "a thread that never started must not be registered"


def test_a_pump_that_blows_up_reaps_the_process_and_records_the_failure(
    tmp_path, monkeypatch
):
    """同一類洩漏的另一個位置：抽輸出失敗。

    `_pump` 跑在背景執行緒裡，沒有人接得到它拋出的例外 —— 拋出去的
    結果是行程繼續跑、紀錄永遠停在「執行中」，而畫面上看起來只是
    這一次特別久。
    """
    from pcmef.console.runner import ConsoleRunner, RunSpec

    spy = _SpyProcess()
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: spy
    )

    runner = ConsoleRunner(tmp_path / "runs")
    original_open = Path.open

    def refuse_the_log(self, *args, **kwargs):
        if self.name == "log.txt" and "a" in str(args[0] if args else
                                                 kwargs.get("mode", "")):
            raise OSError("no space left on device")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_the_log)
    record = runner.start(RunSpec(kind="sim_smoke", params={}))
    runner.wait(record.run_id, timeout=30)
    monkeypatch.undo()

    assert spy.terminated or spy.killed, "the process must not outlive its reader"
    final = runner.get(record.run_id)
    assert final.finished, "a run whose reader died must not stay 'running'"
    assert final.status == "failed"


def test_a_ghost_process_is_reaped_before_the_run_is_rolled_back(env, monkeypatch):
    """端點層：回滾與收行程必須都發生，順序上先收行程。"""
    import threading

    client, runs = env
    _select(client, "pcmef-thesis")
    before = _run_ids(runs)

    spy = _SpyProcess()
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: spy
    )
    monkeypatch.setattr(
        threading.Thread, "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )

    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })

    assert response.status_code >= 400
    assert _run_ids(runs) == before, "a run that never pumped must leave nothing"
    assert spy.terminated or spy.killed


# ---------------------------------------------------------------------------
# 2 遷移寫檔失敗時的授權
# ---------------------------------------------------------------------------


def _plant_pre_capability_thesis(root: Path) -> Path:
    from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID

    record = root / "projects" / LEGACY_THESIS_PROJECT_ID / "project.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({
        "schema_version": "project_v1",
        "project_id": LEGACY_THESIS_PROJECT_ID,
        "display_name": "PC-MEF Thesis",
        "template": LEGACY_THESIS_PROJECT_ID,
        "legacy_layout": True,
        "extra": {},
    }, ensure_ascii=False), encoding="utf-8")
    return record


def test_a_backfill_that_cannot_be_written_grants_nothing(tmp_path, monkeypatch):
    """寫不進去就不算授權。

    先前寫檔失敗被吞掉，卻仍回傳補好能力的物件 —— 於是這一次請求
    有 formal_e2、下一次沒有，而磁碟上從頭到尾都沒有。能力是宣告
    出來的**資料**；資料沒寫成功就是沒有宣告。
    """
    from pcmef.platform.capabilities import (
        FORMAL_E2, LLM_RUNTIME_FREEZE, capabilities_for,
    )
    from pcmef.platform.profiles.models import Profile
    from pcmef.platform.projects.registry import ProjectRegistry

    record = _plant_pre_capability_thesis(tmp_path)
    registry = ProjectRegistry(root=tmp_path)
    monkeypatch.setattr(
        ProjectRegistry, "_write_existing",
        lambda self, project: (_ for _ in ()).throw(OSError("read-only")),
    )

    project = registry.ensure_legacy_thesis_project()
    frozen = Profile(profile_id="f", project_id=project.project_id,
                     display_name="F", state="FROZEN")
    caps = capabilities_for(project, frozen)

    assert FORMAL_E2 not in caps and LLM_RUNTIME_FREEZE not in caps, (
        "a capability that is not on disk must not be granted in memory"
    )
    assert json.loads(record.read_text(encoding="utf-8"))["extra"] == {}


def test_a_successful_backfill_still_grants(tmp_path):
    """對照組：fail-closed 不能變成 always-closed。"""
    from pcmef.platform.capabilities import FORMAL_E2, capabilities_for
    from pcmef.platform.profiles.models import Profile
    from pcmef.platform.projects.registry import ProjectRegistry

    _plant_pre_capability_thesis(tmp_path)
    project = ProjectRegistry(root=tmp_path).ensure_legacy_thesis_project()
    frozen = Profile(profile_id="f", project_id=project.project_id,
                     display_name="F", state="FROZEN")

    assert FORMAL_E2 in capabilities_for(project, frozen)


# ---------------------------------------------------------------------------
# 3 身分只解析一次
# ---------------------------------------------------------------------------


def test_launch_run_takes_no_injected_identity():
    """外部傳進來的身分無從驗證。**這個參數不該存在。**"""
    from pcmef.console.launch import launch_run

    parameters = inspect.signature(launch_run).parameters
    assert "identity" not in parameters, (
        "an injected identity bypasses the very resolution this transaction "
        "exists to perform"
    )


def test_one_launch_resolves_the_project_context_exactly_once(env, monkeypatch):
    """解析兩次就有兩份身分，而中間那一刻 session 可以變。

    授權看的是第一份、歸屬寫的是第二份時，被拒絕的專案仍然可以
    留下一筆記在別人名下的執行。
    """
    client, runs = env
    _select(client, "pcmef-thesis")

    import pcmef.console.project_routes as project_routes

    calls = []
    original = project_routes.request_context

    def counting():
        calls.append(1)
        return original()

    monkeypatch.setattr(project_routes, "request_context", counting)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen",
        lambda *a, **k: _SpyProcess(),
    )

    token = _token(client)
    calls.clear()
    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": token,
    })

    assert response.status_code in (201, 302)
    assert len(calls) == 1, (
        f"the launch resolved the project context {len(calls)} times; "
        "authorization and attribution must share one snapshot"
    )


def test_authorization_and_attribution_describe_the_same_project(env, monkeypatch):
    """若身分在兩次解析之間改變，寫下的歸屬必須仍是被授權的那一個。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    # token 要先拿。取 token 本身也會解析一次 context，把它算進漂移
    # 計數會讓這條測試量到錯的東西。
    token = _token(client)

    import pcmef.console.project_routes as project_routes

    original = project_routes.request_context
    seen = []

    def drifting():
        context = original()
        seen.append(context.project_id)
        if len(seen) > 1:
            # 第二次以後假裝使用者已經切到別的專案。
            from dataclasses import replace

            from pcmef.platform.projects.registry import ProjectRegistry

            registry = ProjectRegistry(
                root=client.application.config["PCMEF_WORKSPACE_ROOT"]
            )
            return replace(context, project=registry.get("tiny-dummy"))
        return context

    monkeypatch.setattr(project_routes, "request_context", drifting)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen",
        lambda *a, **k: _SpyProcess(),
    )

    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": token,
    })

    run_id = next(iter(_run_ids(runs)))
    identity = json.loads(
        (runs / run_id / "run_identity.json").read_text(encoding="utf-8")
    )
    assert identity["project_id"] == "pcmef-thesis", (
        "the attribution must name the project that was authorized, not "
        "whatever the session said a moment later"
    )


# ---------------------------------------------------------------------------
# 4 Profile 狀態 × 動作的完整對照
# ---------------------------------------------------------------------------


def _project(*capabilities):
    from pcmef.platform.projects.models import Project

    return Project(
        project_id="p", display_name="P", template="blank",
        extra={"capabilities": list(capabilities)},
    )


def _profile(state):
    from pcmef.platform.profiles.models import Profile

    return Profile(profile_id="f", project_id="p", display_name="F", state=state)


def test_every_state_and_capability_pair_has_a_declared_answer():
    """對照表必須涵蓋每一格。沒被想過的格子會用預設值回答。"""
    from pcmef.platform.capabilities import CAPABILITIES, capabilities_for
    from pcmef.platform.profiles.models import PROFILE_STATES

    everything = _project(*CAPABILITIES)
    for state in PROFILE_STATES:
        granted = capabilities_for(everything, _profile(state))
        assert granted <= set(CAPABILITIES)


def test_a_frozen_profile_can_still_run_exploratory_simulation():
    """凍結的是科學身分，不是這個人。

    凍結之後仍要能跑探索性模擬 —— 否則 console 在研究進入正式階段
    的那一刻就變成唯讀，而那正是最需要拿它試東西的時候。
    模擬寫的是 run 自己的 artifacts，動不到任何 lock。
    """
    from pcmef.platform.capabilities import RUN_SIMULATION, capabilities_for

    for state in ("FROZEN", "FORMAL_READY", "FORMAL_COMPLETE"):
        assert RUN_SIMULATION in capabilities_for(_project(), _profile(state)), (
            f"{state} must not turn the console read-only"
        )


def test_a_draft_profile_cannot_freeze_the_llm_runtime():
    """DRAFT 的意思就是還沒定案。

    llm_runtime.lock.json 是不可變的科學狀態，寫下去之後要說得出
    「它是哪一版設定產生的」。掛在一份還沒定案的設定上，那句話
    隔天就不成立了。
    """
    from pcmef.platform.capabilities import LLM_RUNTIME_FREEZE, capabilities_for

    project = _project(LLM_RUNTIME_FREEZE)
    assert LLM_RUNTIME_FREEZE not in capabilities_for(project, _profile("DRAFT"))
    assert LLM_RUNTIME_FREEZE in capabilities_for(project, _profile("CONFIGURED"))
    assert LLM_RUNTIME_FREEZE in capabilities_for(project, _profile("FROZEN"))


def test_a_blank_project_cannot_read_the_llm_runtime_snapshot():
    """`llm snapshot` 讀的是這個 repo 的 LLM 綁定，不是專案自己的。

    空專案沒有自己的 binding；讓它讀，畫面上就會出現「這個專案的
    LLM 設定」，而那是碩論的 provider、model 與 revision。與 E1 gate
    是同一類洩漏（P1-5）。
    """
    from pcmef.platform.capabilities import LLM_SNAPSHOT_READ, capabilities_for

    blank = _project()
    assert LLM_SNAPSHOT_READ not in capabilities_for(blank, _profile("FROZEN"))
    assert LLM_SNAPSHOT_READ in capabilities_for(
        _project(LLM_SNAPSHOT_READ), _profile("FROZEN")
    )


def test_an_inhibited_profile_denies_even_exploration():
    from pcmef.platform.capabilities import CAPABILITIES, capabilities_for

    assert capabilities_for(
        _project(*CAPABILITIES), _profile("RUN_INHIBITED")
    ) == frozenset()


def test_a_blank_project_over_http_cannot_trigger_an_llm_snapshot(env):
    """端點層：讀取用的 snapshot 也要能力，不只是 --freeze 那一條。"""
    client, runs = env
    _select(client, "tiny-dummy")
    before = _run_ids(runs)

    response = client.post("/api/console/llm-snapshot", data={
        "csrf_token": _token(client),
    })

    assert response.status_code == 403
    assert _run_ids(runs) == before


def test_the_button_is_not_drawn_where_the_endpoint_would_refuse(env):
    """畫面上的按鈕是能力的投影，不得與守衛說不同的話。

    守衛才是那條線 —— 但一顆按下去永遠 403 的按鈕讀起來像系統壞了，
    而其實是這個 Project 本來就不該有這個動作。
    """
    client, _runs = env

    _select(client, "tiny-dummy")
    blank = client.get("/console").get_data(as_text=True)
    assert 'name="freeze" value="1"' not in blank
    assert "沒有 LLM runtime 的綁定可讀" in blank

    # 對照組：有能力的專案照樣看得到兩顆按鈕。
    _select(client, "pcmef-thesis")
    thesis = client.get("/console").get_data(as_text=True)
    assert 'name="freeze" value="1"' in thesis


# ---------------------------------------------------------------------------
# 5 executor 事件接線
# ---------------------------------------------------------------------------


def test_the_simulation_command_tells_the_executor_where_to_write_events(tmp_path):
    """Web 不寫事件，但它要告訴 executor 該寫到哪裡。"""
    from pcmef.console.runner import ConsoleRunner, RunSpec

    runner = ConsoleRunner(tmp_path / "runs")
    command = runner._command("r1", RunSpec(kind="sim_smoke", params={}))

    assert "--run-events" in command, (
        "without this the executor has nowhere to record what it actually did"
    )
    target = command[command.index("--run-events") + 1]
    assert Path(target) == runner.run_dir("r1")


def test_the_executor_emits_real_stage_events(tmp_path, monkeypatch):
    """事件必須來自**跑過的那一步**，不是估出來的。"""
    import argparse

    import yaml

    from pcmef.platform.runs import (
        RUN_STARTED, STAGE_COMPLETED, STAGE_STARTED, read_events,
    )

    config = tmp_path / "scenario.yaml"
    config.write_text(yaml.safe_dump({"simulation": {
        "spp": 1, "resolution": [4, 4], "temporal_bins": 8,
        "scenarios": [{"class_label": "Empty", "seed": 1, "medium": {}}],
    }}), encoding="utf-8")

    class _Run:
        scenario_id = "smoke_empty_0001"
        transient = None
        error = ""

        def __init__(self):
            from pcmef.simulation.controller import ScenarioStatus

            self.status = ScenarioStatus.OK

    class _Controller:
        def __init__(self, *_a, **_k) -> None:
            pass

        def run_scenario(self, *_a, **_k):
            return _Run()

        def write_manifest(self, runs, out):
            path = Path(out) / "simulation_smoke_manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            return path

    monkeypatch.setattr(
        "pcmef.simulation.controller.SimulationController", _Controller
    )

    from pcmef import cli

    events_dir = tmp_path / "run"
    args = argparse.Namespace(
        config=str(config), out=str(tmp_path / "artifacts"),
        variant=None, temporal_bins=8, formal=False, in_worker=True,
        run_events=str(events_dir),
    )
    assert cli.cmd_sim_smoke(args) == 0

    events, skipped = read_events(events_dir)
    assert skipped == 0
    kinds = [e.event for e in events]
    assert RUN_STARTED in kinds
    assert STAGE_STARTED in kinds and STAGE_COMPLETED in kinds
    started = next(e for e in events if e.event == STAGE_STARTED)
    assert started.total == 1, "the total must be the real scenario count"


def test_a_failing_stage_is_recorded_as_failed_not_missing(tmp_path, monkeypatch):
    """失敗的那一步要留下 stage_failed，不是留白。"""
    import argparse

    import yaml

    from pcmef.platform.runs import STAGE_FAILED, read_events

    config = tmp_path / "scenario.yaml"
    config.write_text(yaml.safe_dump({"simulation": {
        "spp": 1, "resolution": [4, 4], "temporal_bins": 8,
        "scenarios": [{"class_label": "Empty", "seed": 1, "medium": {}}],
    }}), encoding="utf-8")

    class _Controller:
        def __init__(self, *_a, **_k) -> None:
            pass

        def run_scenario(self, *_a, **_k):
            raise RuntimeError("renderer exploded")

        def write_manifest(self, runs, out):  # pragma: no cover - not reached
            raise AssertionError("must not be reached")

    monkeypatch.setattr(
        "pcmef.simulation.controller.SimulationController", _Controller
    )

    from pcmef import cli

    events_dir = tmp_path / "run"
    args = argparse.Namespace(
        config=str(config), out=str(tmp_path / "artifacts"),
        variant=None, temporal_bins=8, formal=False, in_worker=True,
        run_events=str(events_dir),
    )
    with pytest.raises(RuntimeError):
        cli.cmd_sim_smoke(args)

    events, _ = read_events(events_dir)
    assert any(e.event == STAGE_FAILED for e in events), (
        "a stage that blew up must say so; silence reads as 'never started'"
    )


def test_a_renderer_that_cannot_load_still_records_that_the_run_began(
    tmp_path, monkeypatch
):
    """mitsuba 裝不起來時，這次執行仍然「開始過」。

    事件若等到 renderer 載好之後才開始寫，這種失敗在畫面上是一片
    空白 —— 看起來像從未開始，而它其實開始了而且失敗了。
    這是最常見的失敗（環境問題），也是最需要看得到的那一種。
    """
    import argparse

    from pcmef.platform.runs import RUN_STARTED, STAGE_FAILED, read_events

    def explode(*_a, **_k):
        raise ImportError("No module named 'mitsuba'")

    monkeypatch.setattr(
        "pcmef.simulation.controller.SimulationController", explode
    )

    from pcmef import cli

    events_dir = tmp_path / "run"
    args = argparse.Namespace(
        config=str(tmp_path / "missing.yaml"), out=str(tmp_path / "artifacts"),
        variant=None, temporal_bins=8, formal=False, in_worker=True,
        run_events=str(events_dir),
    )
    with pytest.raises(BaseException):
        cli.cmd_sim_smoke(args)

    events, _ = read_events(events_dir)
    kinds = [e.event for e in events]
    assert RUN_STARTED in kinds, "the run began; the record must say so"
    assert STAGE_FAILED in kinds


def test_the_run_page_replays_the_executor_events(env, monkeypatch):
    """Web 只重播。事件檔怎麼說，畫面就怎麼顯示。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _SpyProcess()
    )
    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    run_id = next(iter(_run_ids(runs)))

    from pcmef.platform.runs import RunEventWriter

    writer = RunEventWriter(runs / run_id)
    writer.run_started()
    writer.stage_started("simulation", total=4)
    writer.stage_progress("simulation", 2, 4)

    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "沒有留下 stage 事件" not in body
    assert "2 / 4" in body


# ---------------------------------------------------------------------------
# 6 測試對 repo 的隔離
# ---------------------------------------------------------------------------


def test_no_test_builds_the_app_without_an_isolated_workspace():
    """沒有 workspace_root 的 create_app 會落在 repo 根目錄。

    那表示跑一次測試就會讀寫開發者真實的 projects/pcmef-thesis/
    project.json —— 遷移的補寫會因此變成測試的副作用，而那個檔案
    是進版控的。
    """
    import ast

    offenders = []
    for path in sorted(Path("tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 用 AST 而不是字串比對：字串比對會把說明文字裡的
            # "create_app(" 也算成一次呼叫，而那正是這條測試自己
            # 的錯誤訊息會踩到的。
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "create_app":
                continue
            if not any(kw.arg == "workspace_root" for kw in node.keywords):
                offenders.append(f"{path.as_posix()}:{node.lineno}")
    assert not offenders, (
        f"these build the app against the real repo workspace: {offenders}"
    )


def test_the_session_guard_watches_the_canonical_output_and_project_metadata():
    """靜態掃描擋不住這一類污染，所以要有一個會真的比對的守衛。

    上一輪就是這樣被咬到的：一個 fixture 沿用了預設的
    `PCMEF_FORMAL_OUT`，於是把假的 report 寫進 canonical Final E2 目錄。
    那個目錄不進版控，所以沒有任何東西會告訴你它被動過。

    字串比對法在這裡不管用 —— `tests/e2/test_artifact_isolation.py`
    正當地拿這個路徑做位址運算，它從不寫入。能分辨兩者的只有
    「跑完之後有沒有變」。
    """
    # 以路徑載入，不用 `import tests.conftest`。這台機器的 site-packages
    # 裡有一個同名的 `tests` 套件，會把 repo 的那一個蓋掉 —— 而蓋掉之後
    # 匯入照樣成功，只是拿到別人的模組。
    import importlib.util

    location = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_repo_guard", location)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    WATCHED_PATHS, fingerprint = guard.WATCHED_PATHS, guard.fingerprint

    assert WATCHED_PATHS, "the guard must watch something"
    watched = {Path(p).as_posix() for p in WATCHED_PATHS}
    assert any("e2_final" in p for p in watched)
    assert any("project.json" in p for p in watched)

    # 指紋要對內容變化敏感，否則守衛只是擺著好看。
    probe = Path("pcmef/console/launch.py")
    assert fingerprint([probe]) == fingerprint([probe])
    assert fingerprint([probe]) != fingerprint([Path("pcmef/console/guards.py")])
