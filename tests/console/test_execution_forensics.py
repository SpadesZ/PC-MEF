# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證失敗之後**留下什麼**，以及紀錄裡
#         的每一句話是不是分得清楚。全部在 tmp_path。
# 檔案路徑: tests/console/test_execution_forensics.py
# 產生時間: 2026-09-07 20:10 +08:00
# 版本: v0.1.0
# 功能說明: 動作層閉合第五輪 —— 收不掉的行程不得被記成普通失敗、
#           手動處理要定位得到、tripwire 不得被相對路徑繞過、
#           研究流程與實際執行的動作要分開記，以及其餘 executor
#           的終局事件。
# 模組定位: test_execution_resilience.py 的續作。前一輪讓失敗不再
#           拖垮執行；這一輪處理**失敗之後的可稽核性** —— 一筆寫著
#           「failed」的紀錄，與一筆寫著「failed，而且那個行程可能
#           還在寫檔」的紀錄，對使用者的意義完全不同。
# 主要責任:
#   1. reap 失敗時不得標成普通 failed
#   2. unreaped 紀錄要含 pid / command / host / started_at
#   3. tripwire 對相對路徑、`..`、symlink 一視同仁
#   4. research pipeline 與 executed action 分開記錄
#   5. surrogate_smoke 的事件由 executor 寫
#   6. Formal E2 每一條 early return 都留下終局事件
# 維護提醒:
#   - **不得把 unreaped 併回 failed。** 兩者要採取的行動不同：
#     failed 可以直接重跑，unreaped 必須先去確認那個行程死了沒有。
#   - 不得為了讓 tripwire 便宜而只比對絕對路徑前綴。相對路徑是
#     最常見的寫法，而它正好會整個繞過那道粗篩。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 5。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_forensics.py -v
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")


# ---------------------------------------------------------------------------
# 共用夾具
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
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
    import importlib.util

    location = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_console_helpers", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.csrf_token(client)


def _run_ids(runs: Path) -> set[str]:
    return {p.name for p in runs.iterdir() if p.is_dir()} if runs.exists() else set()


class _Undead:
    """殺不掉的子行程。pid 是它在機器上唯一的名字。"""

    def __init__(self, *, ever_dies: bool = False, pid: int = 424242) -> None:
        self.stdout = iter(())
        self.pid = pid
        self._alive = True
        self._ever_dies = ever_dies

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        raise PermissionError("access denied")

    def kill(self) -> None:
        if self._ever_dies:
            self._alive = False

    def wait(self, timeout=None) -> int:
        if self._alive:
            raise TimeoutError("still running")
        return 0


# ---------------------------------------------------------------------------
# 1 收不掉的行程不得記成普通失敗
# ---------------------------------------------------------------------------

def _pump_with_a_broken_log(runner, process, monkeypatch):
    """啟動一筆 run，讓 log 寫不進去，然後親自跑一次 `_pump`。

    背景執行緒必須先擋掉：`start()` 會自己起一個 `_pump`，跑完之後
    再手動跑第二次會覆蓋掉第一次的結論 —— 那時行程已經死了，於是
    這一次看起來成功。要驗的是**第一次**收尾的行為。
    """
    import threading

    from pcmef.console.runner import RunSpec

    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    record = runner.start(RunSpec(kind="sim_smoke", params={}))

    original_open = Path.open

    def refuse_the_log(self, *args, **kwargs):
        mode = str(args[0] if args else kwargs.get("mode", "r"))
        if self.name == "log.txt" and ("a" in mode or "w" in mode):
            raise OSError("no space left on device")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_the_log)
    runner._pump(record, process)
    monkeypatch.undo()
    return record




def test_a_pump_that_cannot_reap_does_not_record_a_plain_failure(tmp_path,
                                                                 monkeypatch):
    """「失敗」與「失敗，而且那個行程可能還在寫檔」要採取的行動不同。

    前者直接重跑就好；後者必須先去機器上確認它死了沒有 —— 兩筆
    在清單上長得一樣的話，沒有人會去做第二件事。
    """
    from pcmef.console.runner import ConsoleRunner

    process = _Undead(ever_dies=False)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: process
    )
    runner = ConsoleRunner(tmp_path / "runs")
    record = _pump_with_a_broken_log(runner, process, monkeypatch)

    final = runner.get(record.run_id)
    assert final.status != "failed", (
        "a run whose process could not be reaped must not look like an "
        "ordinary failure"
    )
    assert final.unreaped, "the record must say the process may still be alive"


def test_a_pump_that_reaps_cleanly_still_records_a_plain_failure(tmp_path,
                                                                 monkeypatch):
    """對照組：確定死了就照常記成 failed，否則每個失敗都變成待辦事項。"""
    from pcmef.console.runner import ConsoleRunner

    process = _Undead(ever_dies=True)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: process
    )
    runner = ConsoleRunner(tmp_path / "runs")
    record = _pump_with_a_broken_log(runner, process, monkeypatch)

    final = runner.get(record.run_id)
    assert final.status == "failed"
    assert not final.unreaped


def test_an_unreaped_run_is_finished_so_the_stream_stops(tmp_path, monkeypatch):
    """它不是「還在跑」—— 沒有人在讀它了。SSE 必須收線。"""
    from pcmef.console.runner import ConsoleRunner

    process = _Undead(ever_dies=False)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: process
    )
    runner = ConsoleRunner(tmp_path / "runs")
    record = _pump_with_a_broken_log(runner, process, monkeypatch)

    assert runner.get(record.run_id).finished, (
        "an unreaped run is terminal from the console's point of view"
    )


def test_an_unreaped_run_refuses_deletion_while_the_marker_stands(tmp_path,
                                                                  monkeypatch):
    """刪掉它就刪掉了唯一指向那個行程的線索。"""
    from pcmef.console.runner import ConsoleRunner, RunnerError

    process = _Undead(ever_dies=False)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: process
    )
    runner = ConsoleRunner(tmp_path / "runs")
    record = _pump_with_a_broken_log(runner, process, monkeypatch)

    with pytest.raises(RunnerError, match="unreaped|still"):
        runner.delete(record.run_id)

    # 逃生口：確認過行程死了、把標記拿掉之後就可以刪。
    (runner.run_dir(record.run_id) / "unreaped_process.json").unlink()
    runner.delete(record.run_id)


# ---------------------------------------------------------------------------
# 2 unreaped 紀錄要定位得到那個行程
# ---------------------------------------------------------------------------


def _unreaped_payload(runs: Path) -> dict:
    run_id = next(iter(_run_ids(runs)))
    return json.loads(
        (runs / run_id / "unreaped_process.json").read_text(encoding="utf-8")
    )


def test_the_unreaped_marker_names_the_process(env, monkeypatch):
    """「有個行程可能還活著」沒有 pid 就是一句廢話。

    使用者要做的事是去機器上找到它並終止它。少了 pid、指令與主機名，
    這筆紀錄只是告訴他「你有麻煩了」，卻不告訴他麻煩在哪裡。
    """
    import threading

    client, runs = env
    _select(client, "pcmef-thesis")

    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen",
        lambda *a, **k: _Undead(ever_dies=False, pid=987654),
    )
    monkeypatch.setattr(
        threading.Thread, "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )
    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })

    payload = _unreaped_payload(runs)
    assert payload["pid"] == 987654
    assert payload["command"], "the command names what it is doing"
    assert payload["host"], "which machine to look on"
    assert payload["started_at"], "when it started"


def test_the_run_page_tells_the_reader_where_to_look(env, monkeypatch):
    """畫面上要說得出 pid 與主機，否則使用者不知道要去找什麼。"""
    import threading

    client, runs = env
    _select(client, "pcmef-thesis")

    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen",
        lambda *a, **k: _Undead(ever_dies=False, pid=246810),
    )
    monkeypatch.setattr(
        threading.Thread, "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )
    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    monkeypatch.undo()

    run_id = next(iter(_run_ids(runs)))
    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "246810" in body, "the page must name the pid"
    assert "未確認終止" in body


def test_the_unreaped_marker_from_the_pump_names_the_process(tmp_path,
                                                             monkeypatch):
    """兩條路徑寫的是同一份格式，否則其中一份遲早少一個欄位。"""
    from pcmef.console.runner import ConsoleRunner

    process = _Undead(ever_dies=False, pid=13579)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: process
    )
    runner = ConsoleRunner(tmp_path / "runs")
    record = _pump_with_a_broken_log(runner, process, monkeypatch)

    payload = json.loads(
        (runner.run_dir(record.run_id) / "unreaped_process.json")
        .read_text(encoding="utf-8")
    )
    for field in ("pid", "command", "host", "started_at", "run_id", "reason"):
        assert payload.get(field) not in (None, "", []), field


# ---------------------------------------------------------------------------
# 3 tripwire 不得被相對路徑繞過
# ---------------------------------------------------------------------------


def _guard_module():
    import importlib.util

    location = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_repo_guard", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_relative_path_into_a_protected_root_is_caught(tmp_path, monkeypatch):
    """**這是最常見的寫法，而它整個繞過了粗篩。**

    粗篩比對的是絕對路徑前綴；`outputs/perception/e2_final/x.json` 裡
    一個字都對不上，於是在 resolve() 之前就回 False。那段註解說
    「相對路徑在解析那一步才被攤平」—— 但根本走不到那一步。
    """
    guard = _guard_module()
    protected = tmp_path / "outputs" / "perception" / "e2_final"
    protected.mkdir(parents=True)
    tripwire = guard.WriteTripwire([protected])

    monkeypatch.chdir(tmp_path)
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write("outputs/perception/e2_final/sneaky.json")


def test_a_dotdot_path_into_a_protected_root_is_caught(tmp_path, monkeypatch):
    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    (tmp_path / "elsewhere").mkdir()
    tripwire = guard.WriteTripwire([protected])

    monkeypatch.chdir(tmp_path / "elsewhere")
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write("../freeze/lock.json")


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation needs privileges on Windows"
)
def test_a_symlink_into_a_protected_root_is_caught(tmp_path):
    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    link = tmp_path / "shortcut"
    link.symlink_to(protected, target_is_directory=True)
    tripwire = guard.WriteTripwire([protected])

    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write(link / "lock.json")


def test_the_audit_hook_itself_refuses_a_relative_write(tmp_path, monkeypatch):
    """端到端：真的用 open() 去寫，而不是只呼叫 check_write。

    前面幾條測的是判斷邏輯；這一條測的是那個判斷真的掛在 open() 上。
    兩者分開，因為它們會各自壞掉。
    """
    guard = _guard_module()
    protected = tmp_path / "outputs" / "perception" / "e2_final"
    protected.mkdir(parents=True)
    tripwire = guard.WriteTripwire([protected])
    guard._install_tripwire(tripwire)

    monkeypatch.chdir(tmp_path)
    with pytest.raises(guard.ProtectedPathWrite):
        with open("outputs/perception/e2_final/via_open.json", "w") as handle:
            handle.write("{}")

    assert not (protected / "via_open.json").exists()
    assert tripwire.violations


def test_the_tripwire_still_allows_reads_and_unrelated_writes(tmp_path):
    """收緊之後不得開始擋到正當的路徑。"""
    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    tripwire = guard.WriteTripwire([protected])

    tripwire.check_read(protected / "lock.json")
    tripwire.check_write(tmp_path / "elsewhere" / "fine.json")
    tripwire.check_write(tmp_path / "freeze_candidate" / "fine.json")
    assert not tripwire.violations


# ---------------------------------------------------------------------------
# 4 研究流程與實際執行的動作分開記
# ---------------------------------------------------------------------------


def test_the_attribution_records_what_was_actually_executed(env, monkeypatch):
    """`pipeline_snapshot` 是這個研究的流程，不是這一次跑了什麼。

    先前兩者混成同一份：一次 llm snapshot 的 run 帶著七個研究節點的
    快照，讀起來像它本來要跑完整條 pipeline 卻只做了一步。
    """
    class _Fake:
        stdout = iter(())

        def wait(self, timeout=None):
            return 0

    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Fake()
    )
    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })

    run_id = next(iter(_run_ids(runs)))
    identity = json.loads(
        (runs / run_id / "run_identity.json").read_text(encoding="utf-8")
    )

    # 研究流程仍然完整記著。
    assert identity["pipeline_snapshot"]["stages"]
    assert len(identity["stage_ids"]) == 7

    # 而這一次執行的**動作**另外記一份。
    execution = identity["execution"]
    assert execution["kind"] == "sim_smoke"
    assert execution["stage_id"] == "simulation"
    assert execution["template"] == "pcmef-thesis"
    assert execution["display_name"]


def test_an_action_outside_the_research_pipeline_says_so(env, monkeypatch):
    """凍結不是研究流程的一步，紀錄要說得出這件事。"""
    class _Fake:
        stdout = iter(())

        def wait(self, timeout=None):
            return 0

    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Fake()
    )
    client.post("/api/console/llm-snapshot", data={
        "csrf_token": _token(client),
    })

    run_id = next(iter(_run_ids(runs)))
    execution = json.loads(
        (runs / run_id / "run_identity.json").read_text(encoding="utf-8")
    )["execution"]

    assert execution["kind"] == "llm_snapshot"
    assert execution["in_research_pipeline"] is False, (
        "an llm snapshot is not a step of the research pipeline; saying it is "
        "makes the run page draw seven greyed nodes that were never expected"
    )


def test_a_historical_attribution_without_an_execution_block_still_loads(tmp_path):
    """舊 run 沒有這個欄位。讀不回來的話，歷史就消失了。"""
    from pcmef.platform.runs import RunAttribution, read_attribution

    run_dir = tmp_path / "old-run"
    run_dir.mkdir()
    (run_dir / "run_identity.json").write_text(json.dumps({
        "schema_version": "run_attribution_v1",
        "run_id": "old-run",
        "project_id": "pcmef-thesis",
        "stage_ids": ["simulation", "paired"],
        "pipeline_snapshot": {"schema_version": "pipeline_definition_v1",
                              "pipeline_id": "p", "display_name": "P",
                              "note": "", "stage_count": 0, "stages": []},
    }, ensure_ascii=False), encoding="utf-8")

    restored = read_attribution(run_dir)
    assert isinstance(restored, RunAttribution)
    assert restored.run_id == "old-run"
    assert restored.execution == {}, "absent means absent, not invented"


def test_the_run_page_says_which_action_was_executed(env, monkeypatch):
    class _Fake:
        stdout = iter(())

        def wait(self, timeout=None):
            return 0

    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Fake()
    )
    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    run_id = next(iter(_run_ids(runs)))

    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "物理校準模擬" in body, "the page must name the action that ran"


# ---------------------------------------------------------------------------
# 5 surrogate_smoke 的事件
# ---------------------------------------------------------------------------


def test_the_surrogate_command_carries_run_events(tmp_path):
    from pcmef.console.runner import ConsoleRunner, RunSpec

    runner = ConsoleRunner(tmp_path / "runs")
    command = runner._command(
        "r1", RunSpec(kind="surrogate_smoke", params={"simulation_out": "x"})
    )
    assert "--run-events" in command
    assert Path(command[command.index("--run-events") + 1]) == runner.run_dir("r1")


def test_the_surrogate_executor_emits_real_events(tmp_path, monkeypatch):
    from pcmef.platform.runs import (
        RUN_STARTED, STAGE_COMPLETED, STAGE_STARTED, read_events,
    )

    sim_out = tmp_path / "sim"
    (sim_out / "scenario_a").mkdir(parents=True)
    import numpy as np

    for name in ("transient.npy", "transient_time.npy", "transient_ambient.npy"):
        np.save(sim_out / "scenario_a" / name, np.ones((2, 2, 4, 1)))

    class _Observables:
        fwhm_s = 1.0
        snr = 2.0
        multipath_prominence = 3.0

    class _Recording:
        n_samples = 4
        duration_s = 1.0
        values = np.ones((4, 4))

    monkeypatch.setattr(
        "pcmef.surrogate.single_acquisition.SensorSurrogate",
        lambda *a, **k: type("S", (), {"observe": lambda self, *_: _Observables()})(),
    )
    monkeypatch.setattr(
        "pcmef.surrogate.temporal_model.TemporalModel",
        lambda *a, **k: type(
            "M", (), {"generate_recording": lambda self, *_a, **_k: _Recording()}
        )(),
    )

    from pcmef import cli

    events_dir = tmp_path / "run"
    args = argparse.Namespace(
        simulation_out=str(sim_out), out=str(tmp_path / "artifacts"),
        sample_interval_s=None, sample_interval_source="x", seed=1, n_samples=4,
        run_events=str(events_dir),
    )
    cli.cmd_surrogate_smoke(args)

    kinds = [e.event for e in read_events(events_dir)[0]]
    assert RUN_STARTED in kinds
    assert STAGE_STARTED in kinds and STAGE_COMPLETED in kinds


def test_a_surrogate_run_with_no_scenarios_records_the_failure(tmp_path):
    from pcmef.platform.runs import STAGE_FAILED, read_events
    from pcmef import cli

    events_dir = tmp_path / "run"
    args = argparse.Namespace(
        simulation_out=str(tmp_path / "empty"), out=str(tmp_path / "artifacts"),
        sample_interval_s=None, sample_interval_source="x", seed=1, n_samples=4,
        run_events=str(events_dir),
    )
    assert cli.cmd_surrogate_smoke(args) == 1
    assert any(e.event == STAGE_FAILED for e in read_events(events_dir)[0])


# ---------------------------------------------------------------------------
# 6 Formal E2 的每一條 early return
# ---------------------------------------------------------------------------


def _formal_args(tmp_path, **overrides):
    args = argparse.Namespace(
        mode="dry-run", base=str(tmp_path / "base"), out=str(tmp_path / "out"),
        ds_dir=str(tmp_path / "ds"), freeze_dir=str(tmp_path / "freeze"),
        lineage_root=str(tmp_path / "freeze"), registry_dir=str(tmp_path / "reg"),
        vision_severity=None, tof_severity=None, agent_cache=None,
        allow_dirty=False, confirm="", run_id=None, resume=False,
        run_events=str(tmp_path / "run"),
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_a_preflight_that_explodes_still_leaves_a_terminal_event(tmp_path,
                                                                 monkeypatch):
    """pre-flight 自己壞掉，與 pre-flight 判定不通過，是兩件事。

    但對畫面而言它們一樣：留白的話，兩者都顯示「沒有 stage 記錄」，
    讀起來像從未開始。
    """
    from pcmef.platform.runs import RUN_STARTED, STAGE_FAILED, read_events

    monkeypatch.setattr(
        "pcmef.cli._formal_preflight",
        lambda args: (_ for _ in ()).throw(RuntimeError("lineage unreadable")),
    )
    from pcmef import cli

    with pytest.raises(RuntimeError):
        cli.cmd_formal_run_e2(_formal_args(tmp_path))

    kinds = [e.event for e in read_events(tmp_path / "run")[0]]
    assert RUN_STARTED in kinds and STAGE_FAILED in kinds


def test_a_dirty_working_tree_leaves_a_terminal_event(tmp_path, monkeypatch):
    """正式執行要記錄一個 commit，工作區有未提交變更時它拒絕。

    那是一個**結局**，不是沒發生。
    """
    from pcmef.platform.runs import STAGE_FAILED, read_events

    monkeypatch.setattr(
        "pcmef.cli._formal_preflight",
        lambda args: {"checks": [], "allowed": True, "blockers": [],
                      "lineage": {"resolved_freeze_dir": str(tmp_path / "freeze")}},
    )

    class _Completed:
        stdout = "dirty-file.py\n"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Completed())

    from pcmef import cli

    assert cli.cmd_formal_run_e2(_formal_args(tmp_path, mode="formal")) == 2
    assert any(
        e.event == STAGE_FAILED for e in read_events(tmp_path / "run")[0]
    ), "a refusal is an outcome; silence reads as never started"


def _terminal_event_gaps(function_source: str) -> list[int]:
    """回傳「之前沒有發過終局事件」的 return 行號。"""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(function_source))
    function = tree.body[0]
    terminal = {"stage_failed", "stage_completed"}

    gaps = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        for block in ast.walk(function):
            body = getattr(block, "body", None)
            if not isinstance(body, list) or node not in body:
                continue
            before = body[: body.index(node)]
            emitted = any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr in terminal
                for statement in before
                for call in ast.walk(statement)
                if isinstance(call, ast.Call)
            )
            if not emitted:
                gaps.append(node.lineno)
    return gaps


def test_the_formal_body_resolves_every_name_it_was_handed(tmp_path,
                                                           monkeypatch):
    """本體是從入口拆出來的，模組層級的名字不再自動可見。

    `uuid`、`datetime`、`timezone` 只出現在 dry-run 的 run_id 那一行 ——
    拆錯的話會是一個 NameError，而它只在真的跑 formal 時才炸開，
    也就是最不能炸的時候。這條測試把那一行走一次。
    """
    from pcmef.platform.runs import STAGE_COMPLETED, read_events

    monkeypatch.setattr(
        "pcmef.cli._formal_preflight",
        lambda args: {"checks": [], "allowed": True, "blockers": [],
                      "lineage": {"resolved_freeze_dir": str(tmp_path / "freeze")}},
    )

    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        kwargs["progress"]("halfway")
        return {
            "report_path": str(tmp_path / "report.json"), "dry_run": True,
            "scientific_result": "n/a", "llm_arm_evaluated": False,
            "dataset": {"total_rows": 0},
            "routing": {"counts": {}, "escalated_cases": 0,
                        "escalation_rate": 0.0},
            "skipped_escalated_cases": 0, "results": {},
            "worst_condition_macro_f1": {}, "worst_condition_at": {},
        }

    monkeypatch.setattr("pcmef.experiments.e2_formal.run_formal_e2_full", fake_run)

    from pcmef import cli

    cli.cmd_formal_run_e2(_formal_args(tmp_path))

    assert captured["run_id"], "the dry-run run_id line must have executed"
    kinds = [e.event for e in read_events(tmp_path / "run")[0]]
    assert STAGE_COMPLETED in kinds


def test_no_guard_inspects_the_formal_entry_point_alone():
    """拆函式會讓「檢查原始碼」的守衛安靜地失效。

    這一輪就發生了三次：把 `cmd_formal_run_e2` 拆成入口與本體之後，
    三條檢查原始碼的守衛開始看著一個只剩 try/except 的函式。其中
    presence 檢查會**當場失敗**（還好），absence 檢查則變成永遠通過
    —— 它不會報錯，只是不再守任何東西。

    因此：任何取 `cmd_formal_run_e2` 原始碼的地方，都必須把本體一起取。
    """
    import re as _re

    offenders = []
    for path in sorted(Path("tests").rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if _re.search(r"getsource\(\s*cli\.cmd_formal_run_e2\s*\)", line) or \
               _re.search(r"inspect_source\(\s*cli\.cmd_formal_run_e2\s*\)", line):
                window = "\n".join(text.splitlines()[number - 1: number + 2])
                if "_formal_run_e2_body" not in window:
                    offenders.append(f"{path.as_posix()}:{number}")
    assert not offenders, (
        "these read only the entry point, so anything that moved into the "
        f"body is no longer guarded: {offenders}"
    )


def test_every_formal_return_path_is_covered_by_an_event():
    """逐條檢查原始碼：每一個 return 之前都要有終局事件。

    這一條是結構性的守衛。日後有人加一條 early return 而忘了發事件，
    畫面不會報錯 —— 它只會安靜地變回「沒有 stage 記錄」。
    """
    import inspect

    from pcmef import cli

    gaps = _terminal_event_gaps(inspect.getsource(cli._formal_run_e2_body))
    assert not gaps, (
        f"these return statements leave no terminal event: lines {gaps}"
    )


def test_the_formal_entry_point_catches_everything_the_body_raises():
    """early return 只是一半；另一半是 exception。

    本體裡的每一條 return 都發過事件，但 exception 不經過 return ——
    入口必須用一個 catch-all 把它們補上，否則 pre-flight 自己壞掉時
    畫面仍然一片空白。
    """
    import ast
    import inspect
    import textwrap

    from pcmef import cli

    tree = ast.parse(textwrap.dedent(inspect.getsource(cli.cmd_formal_run_e2)))
    handlers = [
        handler
        for node in ast.walk(tree.body[0])
        if isinstance(node, ast.Try)
        for handler in node.handlers
    ]
    assert handlers, "the entry point must wrap the body"

    catch_all = [
        handler for handler in handlers
        if isinstance(handler.type, ast.Name)
        and handler.type.id in ("BaseException", "Exception")
    ]
    assert catch_all, "an exception is an outcome too"
    assert any(
        isinstance(call.func, ast.Attribute)
        and call.func.attr in ("stage_failed", "stage_completed")
        for handler in catch_all
        for call in ast.walk(handler)
        if isinstance(call, ast.Call)
    ), "the catch-all must record the outcome before re-raising"
