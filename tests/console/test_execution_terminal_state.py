# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證「一次執行的結局只有一個，而且說得
#         清楚」。全部在 tmp_path，不觸發任何真正的科研寫入。
# 檔案路徑: tests/console/test_execution_terminal_state.py
# 產生時間: 2026-09-07 23:55 +08:00
# 版本: v0.1.0
# 功能說明: 動作層閉合第六輪 —— 啟動路徑的 unreaped 狀態、刪除的
#           判準、tripwire 的 symlink 別名、終局事件的唯一性、
#           surrogate 的 catch-all，以及 execution snapshot 的範圍。
# 模組定位: test_execution_forensics.py 的續作。前一輪讓失敗留下證據；
#           這一輪處理**證據本身自相矛盾**的情況 —— 紀錄說 running 而
#           沒有人在讀、說 completed 之後又說 failed、說跑了一個
#           decision stage 而實際上跑了整條推論鏈。
# 主要責任:
#   1. 啟動路徑收不掉行程時 run.json 必須是 UNREAPED
#   2. delete() 以 record.unreaped 判定，不倚賴 marker 檔存在
#   3. tripwire 解析 symlink 別名，不被 raw name 早退繞過
#   4. 終局事件唯一：不重複 failed，不在 completed 之後 failed
#   5. surrogate 任一步例外都留下 stage_failed
#   6. execution snapshot 表達 scope 與涵蓋的 stage_ids
# 維護提醒:
#   - **不得讓「還在跑」與「沒有人在讀了」共用同一個狀態。**
#     前者會等，後者要處理；混在一起的話沒有人會去處理。
#   - 不得把終局事件的唯一性交給呼叫點自律。散落的 emit 遲早會有
#     兩個都送出，而畫面只會顯示後到的那一個。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 6。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_terminal_state.py -v
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
    """殺不掉的子行程。"""

    def __init__(self, pid: int = 555001) -> None:
        self.stdout = iter(())
        self.pid = pid

    def poll(self):
        return None

    def terminate(self) -> None:
        raise PermissionError("access denied")

    def kill(self) -> None:
        pass

    def wait(self, timeout=None) -> int:
        raise TimeoutError("still running")


def _launch_with_a_dead_reader(client, monkeypatch, pid: int = 555001):
    """啟動一次 run，讓 reader 掛不上去，且子行程收不掉。"""
    import threading

    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Undead(pid)
    )
    monkeypatch.setattr(
        threading.Thread, "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )
    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    monkeypatch.undo()
    return response


# ---------------------------------------------------------------------------
# 1 啟動路徑的 unreaped 狀態
# ---------------------------------------------------------------------------


def test_a_launch_that_cannot_reap_writes_unreaped_into_the_record(env,
                                                                   monkeypatch):
    """紀錄不得停在 running。

    `start()` 在 Popen 之前就存了一筆 status=running 的 run.json。
    reader 掛不上去、行程又收不掉時，那一筆從來沒有被改寫 —— 於是
    清單上永遠顯示「執行中」，而**沒有任何人在讀那個行程**。
    """
    client, runs = env
    _select(client, "pcmef-thesis")
    _launch_with_a_dead_reader(client, monkeypatch)

    run_id = next(iter(_run_ids(runs)))
    record = json.loads((runs / run_id / "run.json").read_text(encoding="utf-8"))
    assert record["status"] != "running", (
        "nobody is reading that process; the record must not say it is running"
    )
    assert record["status"] == "unreaped"
    assert record["finished_at"], "a terminal record needs a finish time"


def test_the_stream_stops_for_a_launch_that_could_not_reap(env, monkeypatch):
    """SSE 必須收線，否則每開一個分頁就多一條永不釋放的連線。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    _launch_with_a_dead_reader(client, monkeypatch)
    run_id = next(iter(_run_ids(runs)))

    body = client.get(
        f"/api/console/runs/{run_id}/stream"
    ).get_data(as_text=True)
    assert "event: done" in body, "the stream must close on a terminal record"


def test_the_run_page_does_not_claim_a_launch_failure_is_running(env,
                                                                 monkeypatch):
    client, runs = env
    _select(client, "pcmef-thesis")
    _launch_with_a_dead_reader(client, monkeypatch, pid=555002)
    run_id = next(iter(_run_ids(runs)))

    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "555002" in body, "the page must name the pid"
    assert "unreaped" in body


# ---------------------------------------------------------------------------
# 2 delete() 的判準
# ---------------------------------------------------------------------------


def test_delete_refuses_an_unreaped_record_even_without_the_marker(tmp_path,
                                                                   monkeypatch):
    """marker 寫失敗時仍不得失去 provenance。

    寫不出 marker 的情境正是磁碟滿 —— 也就是 reader 掛掉的同一個
    原因。只看 marker 的話，最該保留的那一筆反而變成可以刪。
    """
    from pcmef.console.runner import (
        UNREAPED, ConsoleRunner, RunnerError, RunSpec,
    )

    runner = ConsoleRunner(tmp_path / "runs")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Undead()
    )
    import threading

    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    record = runner.start(RunSpec(kind="sim_smoke", params={}))
    monkeypatch.undo()

    # 手動造出「狀態是 unreaped，但 marker 不存在」的情況。
    record.status = UNREAPED
    record.finished_at = "2026-09-07T00:00:00+00:00"
    # runner 寫 unreaped 時一律同時寫 exit_code = -1。少了它，round 11 起
    # 這筆會被判為損壞而拒絕刪除 —— 測試就會因為錯的理由通過；這裡要守的
    # 是「unreaped 狀態本身」那一道。
    record.exit_code = -1
    runner._save(record)
    marker = runner.run_dir(record.run_id) / "unreaped_process.json"
    if marker.exists():
        marker.unlink()

    with pytest.raises(RunnerError, match="unreaped|confirm"):
        runner.delete(record.run_id)


def test_delete_still_refuses_when_only_the_marker_survives(tmp_path,
                                                            monkeypatch):
    """反過來也要擋：紀錄壞了但 marker 在，同樣不能刪。"""
    from pcmef.console.runner import ConsoleRunner, RunnerError, RunSpec

    runner = ConsoleRunner(tmp_path / "runs")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Undead()
    )
    import threading

    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    record = runner.start(RunSpec(kind="sim_smoke", params={}))
    monkeypatch.undo()

    record.status = "failed"
    record.finished_at = "2026-09-07T00:00:00+00:00"
    # 紀錄本身必須完整：round 11 起缺 exit code 的紀錄會被判為損壞而拒絕
    # 刪除 —— 這條測試就會因為錯的理由通過。要守的是 marker 那一道。
    record.exit_code = 1
    runner._save(record)
    (runner.run_dir(record.run_id) / "unreaped_process.json").write_text(
        "{}", encoding="utf-8"
    )

    with pytest.raises(RunnerError):
        runner.delete(record.run_id)


def test_a_clean_failure_is_still_deletable(tmp_path, monkeypatch):
    """對照組：兩道判準都不成立時照常可刪。"""
    from pcmef.console.runner import ConsoleRunner, RunSpec

    runner = ConsoleRunner(tmp_path / "runs")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Undead()
    )
    import threading

    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    record = runner.start(RunSpec(kind="sim_smoke", params={}))
    monkeypatch.undo()

    record.status = "failed"
    record.finished_at = "2026-09-07T00:00:00+00:00"
    # runner 的每一條 failed 路徑都同時寫 exit code；少了它的紀錄說不出
    # 自己跑完沒有，round 11 起會被判為損壞而拒絕刪除。
    record.exit_code = 1
    runner._save(record)
    marker = runner.run_dir(record.run_id) / "unreaped_process.json"
    if marker.exists():
        marker.unlink()

    runner.delete(record.run_id)
    assert not runner.run_dir(record.run_id).exists()


# ---------------------------------------------------------------------------
# 3 tripwire 的 symlink 別名
# ---------------------------------------------------------------------------


def _guard_module():
    import importlib.util

    location = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_repo_guard", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _can_symlink(tmp_path) -> bool:
    probe, target = tmp_path / "_probe_link", tmp_path / "_probe_target"
    target.mkdir(exist_ok=True)
    try:
        probe.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


def test_a_symlink_alias_cannot_slip_past_the_name_screen(tmp_path):
    """**別名裡一個受保護的名字都沒有，粗篩因此直接放行。**

    `freeze` 的別名叫 `shortcut` 時，raw name 比對不到任何 needle，
    於是在 resolve() 之前就回 False —— 而 resolve() 正是唯一能看穿
    別名的那一步。這是名稱粗篩換掉絕對前綴粗篩之後留下的同一類洞。
    """
    if not _can_symlink(tmp_path):
        pytest.skip("creating a symlink needs privileges on this machine")

    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    alias = tmp_path / "shortcut"          # 名字裡沒有 "freeze"
    alias.symlink_to(protected, target_is_directory=True)
    tripwire = guard.WriteTripwire([protected])

    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write(alias / "lock.json")


def test_a_symlinked_parent_cannot_slip_past_either(tmp_path):
    """別名在中段也一樣：`alias/inner/x` 解析後仍落在保護區內。"""
    if not _can_symlink(tmp_path):
        pytest.skip("creating a symlink needs privileges on this machine")

    guard = _guard_module()
    protected = tmp_path / "outputs" / "perception" / "e2_final"
    (protected / "inner").mkdir(parents=True)
    alias = tmp_path / "elsewhere"
    alias.symlink_to(protected, target_is_directory=True)
    tripwire = guard.WriteTripwire([protected])

    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write(alias / "inner" / "x.json")


def test_the_audit_hook_refuses_a_write_through_a_symlink(tmp_path):
    """端到端：真的用 open() 經過別名寫入。"""
    if not _can_symlink(tmp_path):
        pytest.skip("creating a symlink needs privileges on this machine")

    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    alias = tmp_path / "shortcut"
    alias.symlink_to(protected, target_is_directory=True)
    tripwire = guard.WriteTripwire([protected])
    guard._install_tripwire(tripwire)

    with pytest.raises(guard.ProtectedPathWrite):
        with open(alias / "via_alias.json", "w") as handle:
            handle.write("{}")
    assert not (protected / "via_alias.json").exists()


def test_the_cost_does_not_scale_with_the_number_of_writes(tmp_path):
    """收緊不得變成「每一次 open 都 resolve」。

    看穿 symlink 別名只能靠 resolve()，所以代價無法歸零 —— 但它必須
    以**目錄**為單位，不是以寫入次數為單位。整輪測試有幾十萬次寫入、
    幾百個目錄；搞錯這件事，suite 會慢到有人把守衛關掉，而被關掉的
    守衛等於沒有。
    """
    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    (tmp_path / "plain").mkdir()
    tripwire = guard.WriteTripwire([protected])

    resolved = {"n": 0}
    original = Path.resolve

    def counting(self, *args, **kwargs):
        resolved["n"] += 1
        return original(self, *args, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(Path, "resolve", counting):
        for index in range(200):
            tripwire.check_write(tmp_path / "plain" / f"file{index}.txt")

    assert resolved["n"] <= 2, (
        f"200 writes into one directory cost {resolved['n']} resolutions; "
        "the directory answer must be remembered"
    )


# ---------------------------------------------------------------------------
# 4 終局事件唯一
# ---------------------------------------------------------------------------


def _terminal_events(events_dir: Path):
    from pcmef.platform.runs import STAGE_COMPLETED, STAGE_FAILED, read_events

    return [
        event.event for event in read_events(events_dir)[0]
        if event.event in (STAGE_COMPLETED, STAGE_FAILED)
    ]


def test_a_stage_reports_its_outcome_exactly_once(tmp_path):
    """第一個終局事件說了算。後到的一律丟掉。

    交給呼叫點自律的話，遲早有兩個都送出，而畫面只會顯示後到的
    那一個 —— 於是一次成功的執行可以在最後一行變成失敗。
    """
    from pcmef import cli

    events = cli._stage_events(str(tmp_path / "run"), "simulation")
    events.run_started("x")
    events.stage_started(total=1)
    events.stage_completed(detail="done")
    events.stage_failed("a late failure that must not overwrite the outcome")

    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


def test_a_repeated_failure_is_recorded_once(tmp_path):
    from pcmef import cli

    events = cli._stage_events(str(tmp_path / "run"), "simulation")
    events.run_started("x")
    events.stage_failed("first")
    events.stage_failed("second")

    assert _terminal_events(tmp_path / "run") == ["stage_failed"]


def test_the_formal_body_does_not_emit_a_terminal_event_on_exceptions():
    """終局事件只能有一個擁有者。

    本體與入口都對例外發事件的話，同一次崩潰會寫出兩筆 stage_failed。
    例外的擁有者是入口（它是 catch-all），本體只負責 early return。
    """
    import ast
    import inspect
    import textwrap

    from pcmef import cli

    tree = ast.parse(textwrap.dedent(inspect.getsource(cli._formal_run_e2_body)))
    offenders = []
    for node in ast.walk(tree.body[0]):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # 會往外拋的 handler 不得自己發終局事件 —— 入口會再發一次。
        reraises = any(
            isinstance(inner, ast.Raise) and inner.exc is None
            for inner in ast.walk(node)
        )
        if not reraises:
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                    and call.func.attr in ("stage_failed", "stage_completed"):
                offenders.append(node.lineno)
    assert not offenders, (
        "these handlers emit a terminal event and then re-raise, so the "
        f"entry point emits a second one: lines {offenders}"
    )


def test_a_formal_crash_records_exactly_one_terminal_event(tmp_path,
                                                           monkeypatch):
    """端到端：本體崩潰時只留下一筆 stage_failed。"""
    monkeypatch.setattr(
        "pcmef.cli._formal_preflight",
        lambda args: {"checks": [], "allowed": True, "blockers": [],
                      "lineage": {"resolved_freeze_dir": str(tmp_path / "freeze")}},
    )
    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("renderer exploded")),
    )
    from pcmef import cli

    with pytest.raises(RuntimeError):
        cli.cmd_formal_run_e2(_formal_args(tmp_path))

    assert _terminal_events(tmp_path / "run") == ["stage_failed"]


# ---------------------------------------------------------------------------
# 5 surrogate 的 catch-all
# ---------------------------------------------------------------------------


def _surrogate_args(tmp_path, sim_out):
    return argparse.Namespace(
        simulation_out=str(sim_out), out=str(tmp_path / "artifacts"),
        sample_interval_s=None, sample_interval_source="x", seed=1, n_samples=4,
        run_events=str(tmp_path / "run"),
    )


def _one_scenario(tmp_path):
    import numpy as np

    sim_out = tmp_path / "sim"
    (sim_out / "scenario_a").mkdir(parents=True)
    for name in ("transient.npy", "transient_time.npy", "transient_ambient.npy"):
        np.save(sim_out / "scenario_a" / name, np.ones((2, 2, 4, 1)))
    return sim_out


@pytest.mark.parametrize("target", [
    "numpy.load",
    "pcmef.surrogate.single_acquisition.SensorSurrogate",
    "pcmef.surrogate.temporal_model.TemporalModel",
])
def test_any_surrogate_step_that_explodes_leaves_a_failure(tmp_path,
                                                           monkeypatch, target):
    """np.load / observe / generate 任一步炸開都要留下痕跡。

    先前只有「沒有場景」與「非有限值」兩條路徑會發事件；中間任何一個
    例外都是靜默的，而畫面只會說「沒有 stage 記錄」。
    """
    from pcmef.platform.runs import STAGE_FAILED, read_events

    sim_out = _one_scenario(tmp_path)
    monkeypatch.setattr(
        target, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    from pcmef import cli

    with pytest.raises(BaseException):
        cli.cmd_surrogate_smoke(_surrogate_args(tmp_path, sim_out))

    assert any(
        e.event == STAGE_FAILED for e in read_events(tmp_path / "run")[0]
    ), f"an exception from {target} left no trace"


def test_a_surrogate_write_failure_leaves_a_failure(tmp_path, monkeypatch):
    """寫 csv 失敗同樣是一個結局。"""
    from pcmef.platform.runs import STAGE_FAILED, read_events

    sim_out = _one_scenario(tmp_path)
    monkeypatch.setattr(
        "pcmef.cli._write_csv",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")),
    )

    from pcmef import cli

    with pytest.raises(BaseException):
        cli.cmd_surrogate_smoke(_surrogate_args(tmp_path, sim_out))

    assert any(e.event == STAGE_FAILED for e in read_events(tmp_path / "run")[0])


# ---------------------------------------------------------------------------
# 6 execution snapshot 的範圍
# ---------------------------------------------------------------------------


def _execution_of(client, runs, path, payload, token):
    client.post(path, data={**payload, "csrf_token": token})
    run_id = next(iter(_run_ids(runs)))
    return json.loads(
        (runs / run_id / "run_identity.json").read_text(encoding="utf-8")
    )["execution"]


def test_a_single_stage_action_declares_stage_scope(env, monkeypatch):
    class _Fake:
        stdout = iter(())

        def wait(self, timeout=None):
            return 0

    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Fake()
    )
    execution = _execution_of(
        client, runs, "/api/console/runs",
        {"kind": "sim_smoke", "preset": "standard"}, _token(client),
    )

    assert execution["scope"] == "stage"
    assert execution["stage_ids"] == ["simulation"]


def test_formal_e2_is_not_described_as_a_single_decision_stage(env, monkeypatch):
    """**Formal E2 跑的是整條推論鏈，不是一個 decision 節點。**

    記成單一 stage 的話，紀錄會說這次執行只做了最後一步，而它其實
    從感知一路跑到決策。
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
    execution = _execution_of(
        client, runs, "/formal/start", {"mode": "dry-run"}, _token(client),
    )

    assert execution["kind"] == "formal_e2"
    assert execution["scope"] == "pipeline"
    assert len(execution["stage_ids"]) > 1, (
        "a formal run covers the whole inference chain, not one node"
    )
    assert execution["stage_ids"] != ["decision"]
    assert "decision" in execution["stage_ids"]


def test_a_non_pipeline_action_declares_action_scope(env, monkeypatch):
    """凍結不是流程的一步，也不宣稱涵蓋任何 stage。"""
    class _Fake:
        stdout = iter(())

        def wait(self, timeout=None):
            return 0

    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Fake()
    )
    execution = _execution_of(
        client, runs, "/api/console/llm-snapshot", {}, _token(client),
    )

    assert execution["scope"] == "action"
    assert execution["stage_ids"] == []
    assert execution["in_research_pipeline"] is False


def test_every_registered_executor_declares_a_known_scope():
    from pcmef.platform.executors import SCOPES, describe

    for executor in describe("pcmef-thesis"):
        assert executor.scope in SCOPES, (
            f"{executor.kind} declares an unknown scope {executor.scope!r}"
        )
        if executor.scope == "action":
            assert not executor.stage_ids
        else:
            assert executor.stage_ids, (
                f"{executor.kind} claims scope {executor.scope!r} but names "
                "no stage"
            )


def test_the_run_page_renders_a_round5_execution_block(env, monkeypatch):
    """上一輪的 execution 只有單數 `stage_id`、沒有 `scope`。

    畫面若直接對 `stage_ids` 做 `| length`，那些歷史紀錄會讓整頁 500
    —— 而它們是磁碟上真實存在的東西，不是假想情況。
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

    # 把歸屬改寫成 round 5 的形狀。write-once 擋改寫，所以直接換掉檔案
    # —— 這裡模擬的是「磁碟上本來就是舊格式」。
    identity = runs / run_id / "run_identity.json"
    data = json.loads(identity.read_text(encoding="utf-8"))
    data["execution"] = {
        "kind": "sim_smoke", "stage_id": "simulation",
        "display_name": "物理校準模擬", "template": "pcmef-thesis",
        "in_research_pipeline": True,
    }
    identity.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    response = client.get(f"/console/runs/{run_id}")
    assert response.status_code == 200, "a round-5 record must still render"
    assert "物理校準模擬" in response.get_data(as_text=True)


def test_a_historical_execution_block_without_scope_still_loads(tmp_path):
    """上一輪寫下的 execution 沒有 scope 欄位。"""
    from pcmef.platform.runs import read_attribution

    run_dir = tmp_path / "old"
    run_dir.mkdir()
    (run_dir / "run_identity.json").write_text(json.dumps({
        "schema_version": "run_attribution_v1",
        "run_id": "old", "project_id": "pcmef-thesis",
        "execution": {"kind": "sim_smoke", "stage_id": "simulation"},
    }, ensure_ascii=False), encoding="utf-8")

    restored = read_attribution(run_dir)
    assert restored.execution["kind"] == "sim_smoke"


# ---------------------------------------------------------------------------
# source-inspection guards 的總稽核
# ---------------------------------------------------------------------------


def test_no_source_inspection_guard_reads_a_split_entry_point_alone():
    """拆函式會讓「檢查原始碼」的守衛安靜地失效。

    上一輪已經被咬過一次：五條守衛讀著一個只剩 try/except 的函式，
    其中三條是 absence 檢查，因此**通過但沒守到**。這條測試把整個
    tests/ 掃一遍，任何取 `cmd_formal_run_e2` 原始碼卻沒有一併取
    本體的地方都算失敗。

    SOURCE-GUARD-SCANNER —— 本檔含掃描用的正規式字面值，掃描器需略過。
    """
    import re

    offenders = []
    for path in sorted(Path("tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        # 掃描器本身含有這個名字的正規式字面值。標記過的檔案略過，
        # 否則守衛會互相指控對方。
        if "SOURCE-GUARD-SCANNER" in text:
            continue
        lines = text.splitlines()
        for number, line in enumerate(lines, start=1):
            if not re.search(
                r"(getsource|inspect_source)\(\s*cli\.cmd_formal_run_e2\s*\)", line
            ):
                continue
            window = "\n".join(lines[max(0, number - 3): number + 3])
            if "_formal_run_e2_body" not in window:
                offenders.append(f"{path.as_posix()}:{number}")
    assert not offenders, (
        "these read only the entry point, so anything that moved into the "
        f"body is no longer guarded: {offenders}"
    )


def test_every_source_inspection_guard_still_finds_its_subject():
    """守衛必須**真的找得到**它要守的東西。

    absence 檢查（`assert x not in source`）在讀錯函式時會安靜通過。
    這裡反過來驗：每一條 presence 斷言指名的字串，確實出現在
    入口＋本體的原始碼裡。找不到就代表那條守衛已經在看空氣。
    """
    import inspect

    from pcmef import cli

    source = (inspect.getsource(cli.cmd_formal_run_e2)
              + inspect.getsource(cli._formal_run_e2_body))

    # 這幾條是既有守衛實際斷言存在的字串，逐一確認它們仍在。
    for needle in (
        "run_formal_e2_full",
        "uuid.uuid4().hex",
        "%Y%m%dT%H%M%S",
        "if args.vision_severity is None and args.tof_severity is None",
    ):
        assert needle in source, (
            f"{needle!r} is asserted present by an existing guard but no "
            "longer appears; that guard is now watching nothing"
        )


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


def test_no_test_binds_a_real_socket():
    """測試不得留下 listener。

    本輪的硬性要求：8790 是唯一的正式 localhost UI port，而測試
    一律走 Flask 的 test_client（不開 socket）。任何 `app.run(`、
    `serve(`、或直接 bind 的寫法都會在機器上留下東西。
    """
    import re

    offenders = []
    for path in sorted(Path("tests").rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\.run\(\s*host=|socket\.bind\(|cli\.cmd_admin_serve\(",
                         line):
                offenders.append(f"{path.as_posix()}:{number}")
    assert not offenders, f"these start a real listener: {offenders}"
