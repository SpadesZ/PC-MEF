# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；對 Execution / Action Layer 的**失敗路徑**
#         做對抗性測試。全部在 tmp_path，不觸發任何真正的科研寫入。
# 檔案路徑: tests/console/test_execution_resilience.py
# 產生時間: 2026-09-07 14:30 +08:00
# 版本: v0.1.0
# 功能說明: 動作層閉合第四輪 —— telemetry 壞掉不得拖垮實驗、行程真的
#           要死透、收尾失敗不得靜默逃走、測試碰過就算污染、Blank
#           Project 不得跑碩論的 executor，以及其餘三支的事件接線。
# 模組定位: test_execution_hardening.py 的續作。前三輪處理「守衛存在」
#           與「守衛自己失敗」；本輪處理**次要設施失敗時會不會傷到主線**
#           —— 記錄進度的檔案寫不進去，不該讓一次算了半小時的模擬失敗；
#           但反過來，收不掉的行程也不該被當成收掉了。
# 主要責任:
#   1. 事件寫入失敗只失去 telemetry，不失去執行
#   2. terminate 失敗仍要 kill；死不確定就不得刪 provenance
#   3. 收尾寫入失敗要留下可診斷狀態，不得靜默逃走
#   4. 測試**曾經**寫過受保護路徑就算污染，即使事後刪掉
#   5. 能力與 template/provider 對齊，Blank 不跑 PC-MEF executor
#   6. llm_snapshot / formal_e2 / audit 的事件同樣由 executor 寫
# 維護提醒:
#   - **不得把 fail-soft 擴大到科學結果。** telemetry 可以掉，
#     manifest、lock、report 不可以。兩者的差別是本檔每一條的前提。
#   - 不得把「收不掉行程」當成可以忽略的情況。刪掉 run 目錄之後，
#     那個行程就沒有任何紀錄指向它了。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 4。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_resilience.py -v
# ------------------------------------------------------------

from __future__ import annotations

import argparse
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
    """乾淨的 workspace，內含 legacy Thesis 與一個空專案。"""
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
    """從 session 取 CSRF token，而不是從畫面上刮。

    刮畫面的寫法在這一輪會誤導：沒有 executor 的專案根本不會畫出任何
    表單，於是「拿不到 token」看起來像頁面壞了，而它正是預期行為。
    要驗的是**端點**擋不擋得住，所以 token 必須與畫面無關地取得。
    """
    from pcmef.admin.auth import new_csrf_token
    from pcmef.admin.routes_llm import CSRF_SESSION_KEY

    client.get("/console")
    with client.session_transaction() as session:
        token = session.get(CSRF_SESSION_KEY)
        if not token:
            token = new_csrf_token()
            session[CSRF_SESSION_KEY] = token
    return token


def _run_ids(runs: Path) -> set[str]:
    return {p.name for p in runs.iterdir() if p.is_dir()} if runs.exists() else set()


class _Undead:
    """一個殺不掉的子行程。terminate 拋錯，kill 之後才真的死。"""

    def __init__(self, *, terminate_raises=True, ever_dies=True) -> None:
        self.stdout = iter(())
        self.terminate_calls = 0
        self.kill_calls = 0
        self._alive = True
        self._terminate_raises = terminate_raises
        self._ever_dies = ever_dies

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._terminate_raises:
            raise PermissionError("access denied")
        self._alive = False

    def kill(self) -> None:
        self.kill_calls += 1
        if self._ever_dies:
            self._alive = False

    def wait(self, timeout=None) -> int:
        if self._alive:
            raise TimeoutError("still running")
        return 0


# ---------------------------------------------------------------------------
# 1 telemetry 壞掉不得拖垮實驗
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", [
    "pcmef.platform.runs.events.os.fsync",
    "pcmef.platform.runs.events.open",
])
def test_an_event_sink_failure_never_reaches_the_caller(tmp_path, monkeypatch,
                                                        target):
    """事件檔寫不進去只該失去 telemetry。

    磁碟滿、handle 用盡、fsync 在網路磁碟上失敗 —— 這些都不該讓一次
    算了半小時的模擬失敗。**進度看不到**與**沒有結果**是兩件事。
    """
    from pcmef.platform.runs import RunEventWriter

    writer = RunEventWriter(tmp_path / "run")
    monkeypatch.setattr(
        target, lambda *a, **k: (_ for _ in ()).throw(OSError("no space")),
        raising=False,
    )

    # 每一個 emit 都必須吞下去。漏掉任何一個就會在那一步炸開。
    writer.run_started("x")
    writer.stage_started("s", total=3)
    writer.stage_progress("s", 1, 3)
    writer.stage_completed("s")
    writer.stage_failed("s")
    writer.stage_skipped("s")
    writer.run_failed("x")


def test_a_keyboard_interrupt_is_not_swallowed_by_the_sink(tmp_path, monkeypatch):
    """使用者要停下來，不是這一行寫不進去。**兩者不得混為一談。**

    吞掉 Ctrl-C 會讓它在恰好落在 fsync 那一瞬間時失效，而那種失效
    沒有規律，看起來只會像「有時候按了沒反應」。
    """
    from pcmef.platform.runs import RunEventWriter

    writer = RunEventWriter(tmp_path / "run")
    monkeypatch.setattr(
        "pcmef.platform.runs.events.open",
        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        raising=False,
    )
    with pytest.raises(KeyboardInterrupt):
        writer.run_started("x")
    assert writer.dropped == 0, "an interrupt is not a dropped event"


def test_an_unwritable_event_directory_never_reaches_the_caller(tmp_path,
                                                                monkeypatch):
    """連目錄都建不出來時也一樣。"""
    from pcmef.platform.runs import RunEventWriter

    monkeypatch.setattr(
        Path, "mkdir",
        lambda self, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    RunEventWriter(tmp_path / "nope" / "run").run_started("x")


def test_the_sink_reports_that_it_lost_events(tmp_path, monkeypatch):
    """吞下去不等於假裝沒事。**掉了幾筆必須說得出來。**

    靜默的 fail-soft 會讓「這次執行沒有進度」與「進度寫不進去」
    看起來一樣，而前者是執行的問題、後者是機器的問題。
    """
    from pcmef.platform.runs import RunEventWriter

    writer = RunEventWriter(tmp_path / "run")
    # raising=False：模組本身沒有 `open` 屬性（它是 builtin），
    # 設進去等於在模組命名空間裡蓋掉它。
    monkeypatch.setattr(
        "pcmef.platform.runs.events.open",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")),
        raising=False,
    )
    writer.run_started("x")
    writer.stage_started("s")

    assert writer.dropped == 2
    assert writer.last_error, "the reason must survive for the log"


def test_a_simulation_survives_a_sink_that_dies_halfway(tmp_path, monkeypatch):
    """實驗跑完，只是進度少了幾筆。"""
    import yaml

    config = tmp_path / "scenario.yaml"
    config.write_text(yaml.safe_dump({"simulation": {
        "spp": 1, "resolution": [4, 4], "temporal_bins": 8,
        "scenarios": [
            {"class_label": "Empty", "seed": 1, "medium": {}},
            {"class_label": "Empty", "seed": 2, "medium": {}},
        ],
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

    calls = {"n": 0}
    real_open = open

    def flaky(path, *args, **kwargs):
        if str(path).endswith("stage_events.jsonl"):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("no space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("pcmef.platform.runs.events.open", flaky, raising=False)

    from pcmef import cli

    args = argparse.Namespace(
        config=str(config), out=str(tmp_path / "artifacts"),
        variant=None, temporal_bins=8, formal=False, in_worker=True,
        run_events=str(tmp_path / "run"),
    )
    assert cli.cmd_sim_smoke(args) == 0, (
        "losing telemetry must not lose the experiment"
    )
    assert (tmp_path / "artifacts" / "simulation_smoke_manifest.json").exists()


def test_the_executor_says_out_loud_that_it_lost_events(tmp_path, monkeypatch,
                                                        capsys):
    """掉事件要在 log 裡看得到，否則沒有人會知道進度不完整。

    「這一步沒有進度」與「進度寫不進去」在畫面上長得一樣，而前者是
    執行的問題、後者是機器的問題 —— 處置方式相反。
    """
    from pcmef import cli

    monkeypatch.setattr(
        "pcmef.platform.runs.events.open",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")),
        raising=False,
    )
    events = cli._stage_events(str(tmp_path / "run"), "simulation")
    events.run_started("x")
    events.stage_started(total=1)

    assert "stage events are being dropped" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 2 行程要死透，死不確定就不准刪
# ---------------------------------------------------------------------------


def test_a_terminate_that_raises_still_escalates_to_kill():
    """terminate 失敗不是放棄的理由。

    先前迴圈在 terminate 拋例外時直接 break，於是 kill 永遠不會被
    嘗試 —— 而 terminate 會拋的情況（權限、行程狀態）正是最需要
    kill 的情況。
    """
    from pcmef.console.runner import ConsoleRunner

    process = _Undead(terminate_raises=True, ever_dies=True)
    assert ConsoleRunner._reap(process) is True
    assert process.terminate_calls == 1
    assert process.kill_calls == 1, "kill must still be attempted"


def test_a_process_that_never_dies_is_reported_as_unconfirmed():
    """收不掉就要說收不掉。**不得回報成功。**"""
    from pcmef.console.runner import ConsoleRunner

    process = _Undead(terminate_raises=True, ever_dies=False)
    assert ConsoleRunner._reap(process) is False
    assert process.kill_calls >= 1


def test_an_unconfirmed_kill_does_not_delete_the_run_directory(env, monkeypatch):
    """殺不死的行程仍然握著這個目錄，而且它是唯一指向那個行程的線索。

    刪掉之後，機器上有一個在寫檔的行程，而沒有任何紀錄說它是誰。
    """
    import threading

    client, runs = env
    _select(client, "pcmef-thesis")

    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen",
        lambda *a, **k: _Undead(terminate_raises=True, ever_dies=False),
    )
    monkeypatch.setattr(
        threading.Thread, "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )

    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })

    assert response.status_code >= 400
    assert _run_ids(runs), (
        "the run directory is the only thing pointing at a process we could "
        "not kill; it must survive the rollback"
    )


def test_a_confirmed_kill_still_rolls_the_run_back(env, monkeypatch):
    """對照組：確定死了就照常回滾，否則每次失敗都留垃圾。"""
    import threading

    client, runs = env
    _select(client, "pcmef-thesis")
    before = _run_ids(runs)

    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen",
        lambda *a, **k: _Undead(terminate_raises=False, ever_dies=True),
    )
    monkeypatch.setattr(
        threading.Thread, "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )

    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })

    assert _run_ids(runs) == before


# ---------------------------------------------------------------------------
# 3 收尾寫入失敗不得靜默逃走
# ---------------------------------------------------------------------------


def test_a_pump_whose_save_also_fails_leaves_a_diagnosable_trace(tmp_path,
                                                                 monkeypatch):
    """log 壞了、run.json 也寫不進去 —— 這時仍然不得什麼都不留。

    背景執行緒拋出去沒有人接得到；紀錄停在「執行中」，而畫面上
    只是看起來特別久。至少要留下一個檔案說「這裡發生過什麼」。
    """
    from pcmef.console.runner import ConsoleRunner, RunSpec

    process = _Undead(terminate_raises=False, ever_dies=True)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: process
    )

    runner = ConsoleRunner(tmp_path / "runs")
    record = runner.start(RunSpec(kind="sim_smoke", params={}))
    run_dir = runner.run_dir(record.run_id)

    original_open = Path.open

    def refuse_everything(self, *args, **kwargs):
        mode = str(args[0] if args else kwargs.get("mode", "r"))
        if self.name in ("log.txt", "run.json") and ("a" in mode or "w" in mode):
            raise OSError("no space left on device")
        return original_open(self, *args, **kwargs)

    def refuse_write_text(self, *args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "open", refuse_everything)
    monkeypatch.setattr(Path, "write_text", refuse_write_text)

    # 直接呼叫 _pump：它就是那個背景執行緒的本體。
    runner._pump(record, process)
    monkeypatch.undo()

    survivors = [p.name for p in run_dir.iterdir()]
    assert any("crash" in name for name in survivors), (
        f"nothing explains what happened; only {survivors} survived"
    )


def test_the_pump_never_raises_even_when_everything_fails(tmp_path, monkeypatch):
    """最後一道：無論如何都不得往外拋。"""
    from pcmef.console.runner import ConsoleRunner, RunSpec

    process = _Undead(terminate_raises=True, ever_dies=False)
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: process
    )
    runner = ConsoleRunner(tmp_path / "runs")
    record = runner.start(RunSpec(kind="sim_smoke", params={}))

    monkeypatch.setattr(
        Path, "open",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("gone")),
    )
    monkeypatch.setattr(
        Path, "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("gone")),
    )
    monkeypatch.setattr(
        Path, "mkdir",
        lambda self, **k: (_ for _ in ()).throw(OSError("gone")),
    )

    runner._pump(record, process)  # 不得拋


# ---------------------------------------------------------------------------
# 4 碰過就算污染
# ---------------------------------------------------------------------------


def _guard_module():
    """以路徑載入 tests/conftest.py。

    這台機器的 site-packages 裡有一個同名的 `tests` 套件會蓋掉 repo 的
    那一個，而蓋掉之後匯入照樣成功，只是拿到別人的模組。
    """
    import importlib.util

    location = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_repo_guard", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_write_that_is_undone_is_still_pollution(tmp_path):
    """寫了又刪，前後指紋一樣 —— 但它確實碰過。

    上一輪的守衛只比對 session 前後，因此一個「寫進 canonical 目錄、
    跑完自己刪掉」的 fixture 完全隱形。而在它刪掉之前，任何併行的
    讀取都會看到那份假資料。
    """
    guard = _guard_module()
    tripwire = guard.WriteTripwire([tmp_path / "protected"])

    (tmp_path / "protected").mkdir()
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write(tmp_path / "protected" / "sneaky.json")

    assert tripwire.violations, "the attempt must be recorded, not just refused"


def test_the_tripwire_allows_reads_and_unrelated_writes(tmp_path):
    """守衛只擋寫入受保護路徑。擋到別的就會有人把它關掉。"""
    guard = _guard_module()
    tripwire = guard.WriteTripwire([tmp_path / "protected"])

    tripwire.check_write(tmp_path / "elsewhere" / "fine.json")
    tripwire.check_read(tmp_path / "protected" / "lock.json")
    assert not tripwire.violations


def test_the_protected_set_covers_the_three_places_that_matter():
    guard = _guard_module()
    watched = {Path(p).as_posix() for p in guard.PROTECTED_PATHS}

    assert any(p.endswith("outputs/perception/e2_final") for p in watched)
    assert any(p.endswith("freeze") for p in watched)
    assert any(p.endswith("projects") for p in watched)


# ---------------------------------------------------------------------------
# 5 Blank Project 不得跑碩論的 executor
# ---------------------------------------------------------------------------


def test_a_blank_project_cannot_start_the_pcmef_simulation(env):
    """能力說「可以跑模擬」，不等於「可以跑這一篇論文的模擬」。

    `sim_smoke` 組出來的是四個瓶內液態類別與那支瓶子的幾何 ——
    那是 PC-MEF 的 executor，不是平台的通用核心。空專案按下去
    會跑出一份標著它自己名字、內容是碩論場景的結果。
    """
    client, runs = env
    _select(client, "tiny-dummy")
    before = _run_ids(runs)

    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })

    assert response.status_code in (403, 409)
    assert _run_ids(runs) == before


def test_a_blank_project_is_told_it_has_no_executor(env):
    """畫面要說原因，而不是給一顆按下去會 403 的按鈕。"""
    client, _runs = env
    _select(client, "tiny-dummy")
    body = client.get("/console").get_data(as_text=True)

    for leaked in ("Water-filled", "Bubbly", "Misty"):
        assert leaked not in body, (
            "a blank project must not be offered the thesis's class labels"
        )


@pytest.mark.parametrize("project", ["pcmef-thesis", "tiny-dummy"])
def test_the_run_page_stays_well_formed_in_both_branches(env, project):
    """把表單包進 {% if %} 很容易把收尾標籤留在其中一邊。

    少一個 `</div>` 不會報錯，也不會讓測試變紅 —— 它只是讓後面的
    區塊被吸進上一張卡片裡，而那在截圖上看起來只是「版面怪怪的」。
    """
    client, _runs = env
    _select(client, project)
    body = client.get("/console").get_data(as_text=True)

    for tag in ("div", "section", "form", "p"):
        opened = len(re.findall(rf"<{tag}\b", body))
        closed = body.count(f"</{tag}>")
        assert opened == closed, (
            f"{project}: <{tag}> opened {opened} times, closed {closed}"
        )


def test_the_thesis_project_still_runs_its_own_executor(env, monkeypatch):
    """對照組：邊界不能只是把所有人都擋掉。"""
    class _Fake:
        stdout = iter(())

        def wait(self, timeout=None):
            return 0

    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Fake()
    )

    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    assert response.status_code in (201, 302)
    assert _run_ids(runs)


def test_the_executor_registry_is_keyed_by_template_not_by_project_id():
    """用 project id 判斷資格與用名字判斷只差一層。"""
    import inspect

    from pcmef.platform import executors

    source = inspect.getsource(executors)
    assert "pcmef-thesis" not in source.replace("LEGACY", ""), (
        "the registry must key on template, not on the legacy project id"
    )


def test_a_generic_template_declares_no_executor():
    from pcmef.platform.executors import executors_for, supports

    assert executors_for("blank") == frozenset()
    assert not supports("blank", "sim_smoke")


# ---------------------------------------------------------------------------
# 6 其餘三支的事件接線
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind, params", [
    ("llm_snapshot", {"freeze": False}),
    ("formal_e2", {"mode": "dry-run"}),
    ("audit_gates", {}),
])
def test_every_run_kind_tells_the_executor_where_to_write_events(tmp_path, kind,
                                                                 params):
    from pcmef.console.runner import ConsoleRunner, RunSpec

    runner = ConsoleRunner(tmp_path / "runs")
    command = runner._command("r1", RunSpec(kind=kind, params=params))

    assert "--run-events" in command, f"{kind} has nowhere to record what it did"
    assert Path(command[command.index("--run-events") + 1]) == runner.run_dir("r1")


def test_the_llm_snapshot_executor_emits_real_events(tmp_path, monkeypatch):
    from pcmef.platform.runs import (
        RUN_STARTED, STAGE_COMPLETED, STAGE_STARTED, read_events,
    )

    class _Snapshot:
        runtime_config_hash = "h" * 64
        bindings: dict = {}
        freezable = True
        blocking_reasons: tuple = ()

        def candidate_hash(self):
            return "c" * 64

        def write(self, out):
            path = Path(out) / "runtime_snapshot_cccc.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            return path

    class _Service:
        registry = object()
        config = object()

    monkeypatch.setattr(
        "pcmef.llm.snapshot.build_runtime_snapshot", lambda *a, **k: _Snapshot()
    )
    monkeypatch.setattr("pcmef.cli._admin_service", lambda args: _Service())

    from pcmef import cli

    events_dir = tmp_path / "run"
    args = argparse.Namespace(
        out=str(tmp_path / "artifacts"), freeze=False,
        schemas_dir="schemas", prompts_dir="prompts",
        run_events=str(events_dir),
    )
    cli.cmd_llm_snapshot(args)

    kinds = [e.event for e in read_events(events_dir)[0]]
    assert RUN_STARTED in kinds
    assert STAGE_STARTED in kinds and STAGE_COMPLETED in kinds


def test_the_audit_executor_emits_real_events(tmp_path, monkeypatch):
    from pcmef.platform.runs import RUN_STARTED, STAGE_COMPLETED, read_events

    monkeypatch.setattr("pcmef.cli._emit_audit", lambda *a, **k: 0)
    monkeypatch.setattr(
        "pcmef.audit.e1_gates.audit_e1_gates", lambda paths: object()
    )

    from pcmef import cli

    events_dir = tmp_path / "run"
    args = argparse.Namespace(
        inventory=str(tmp_path), splits=str(tmp_path), simulation=str(tmp_path),
        surrogate=str(tmp_path), provenance=str(tmp_path),
        freeze_dir=str(tmp_path), tests=str(tmp_path),
        out=str(tmp_path / "artifacts"), require=None,
        run_events=str(events_dir),
    )
    assert cli.cmd_audit_e1_gates(args) == 0

    kinds = [e.event for e in read_events(events_dir)[0]]
    assert RUN_STARTED in kinds and STAGE_COMPLETED in kinds


def test_a_failing_audit_records_the_failure(tmp_path, monkeypatch):
    from pcmef.platform.runs import STAGE_FAILED, read_events

    monkeypatch.setattr(
        "pcmef.audit.e1_gates.audit_e1_gates",
        lambda paths: (_ for _ in ()).throw(RuntimeError("gate reader exploded")),
    )

    from pcmef import cli

    events_dir = tmp_path / "run"
    args = argparse.Namespace(
        inventory=str(tmp_path), splits=str(tmp_path), simulation=str(tmp_path),
        surrogate=str(tmp_path), provenance=str(tmp_path),
        freeze_dir=str(tmp_path), tests=str(tmp_path),
        out=str(tmp_path / "artifacts"), require=None,
        run_events=str(events_dir),
    )
    with pytest.raises(RuntimeError):
        cli.cmd_audit_e1_gates(args)

    assert any(e.event == STAGE_FAILED for e in read_events(events_dir)[0])


def test_every_console_run_kind_has_a_registered_executor():
    """console 能啟動的每一種，碩論模板都要有對應的 executor。

    少一個的話，那一種在 launch 會被 `_require_executor` 擋下 —— 對
    Blank Project 是正確行為，對碩論自己則是它失去了一項本來有的功能，
    而且是安靜地失去。
    """
    from pcmef.console.runner import RUN_KINDS
    from pcmef.platform.executors import executors_for

    registered = executors_for("pcmef-thesis")
    assert set(RUN_KINDS) <= registered, (
        f"the thesis template is missing executors for "
        f"{sorted(set(RUN_KINDS) - registered)}"
    )


def test_the_registry_and_the_cli_name_the_same_stages():
    """stage id 只能有一份。兩份字面值遲早有一份被改到，而改了不會報錯。"""
    from pcmef.platform import executors

    constants = {
        value for name, value in vars(executors).items()
        if name.startswith("STAGE_") and isinstance(value, str)
    }
    for executor in executors.describe("pcmef-thesis"):
        assert executor.stage_id in constants, (
            f"{executor.kind} names a stage id that is not one of the shared "
            f"constants: {executor.stage_id!r}"
        )


def test_the_web_still_writes_no_events():
    """接了三支 executor 之後，Web 仍然一行都不寫。"""
    offenders = []
    for folder in ("pcmef/console", "pcmef/admin"):
        for path in sorted(Path(folder).rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if line.lstrip().startswith("#"):
                    continue
                if "RunEventWriter" in line or re.search(r"\bemit_\w+\(", line):
                    offenders.append(f"{path.as_posix()}:{number}")
    assert not offenders, f"web modules must not write run events: {offenders}"
