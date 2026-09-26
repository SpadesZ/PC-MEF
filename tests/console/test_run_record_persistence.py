# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 ConsoleRunner 的 run.json 在更新、
#         失敗與歷史損壞三種情況下都只影響它自己。全部在 tmp_path。
# 檔案路徑: tests/console/test_run_record_persistence.py
# 產生時間: 2026-09-26 01:10 +08:00
# 版本: v0.1.1
# 功能說明: 動作層閉合第十輪 —— run.json 的原子替換、寫入失敗時保留上一份
#           完整紀錄、初始紀錄失敗留下證據，以及一筆壞紀錄不得讓 Run、
#           Results、Formal 整頁 500，也不得被當成可刪的一般紀錄。
# 模組定位: round 9 記錄、未修的 §51.4 第 2 項。實測：同時讀取 3000 次有
#           1487 次讀到半份 JSON；截斷之後寫入失敗留下 0 byte 的紀錄，之後
#           list_runs() 讓三個全域頁面永久 500。
# 主要責任:
#   1. 替換進行中的讀者只看得到舊的完整版或新的完整版（同步點，不靠時序）
#   2. 替換之前任何一步失敗，上一份完整紀錄一個 byte 都不動
#   3. 初始紀錄寫不進去：不啟動行程、不留半份 run.json、留下證據
#   4. 各種歷史損壞：單筆 409、清單照常、另列損壞區
#   5. 壞紀錄不得削弱 Formal E2 的刪除與稽核語意
#   6. 反序列化不得補欄位：缺的、矛盾的、說不出 mode 的一律損壞
# 維護提醒:
#   - **不得把同步點測試改成 sleep。** 「讀者在替換之前的那一刻讀」必須
#     是一個被安排好的時刻，而不是碰運氣；壓力測試只是補充證據。
#   - 每一條失敗注入測試都要比對 run.json 的**位元組**，不是只看有沒有
#     丟例外。
#   - v0.1.1 新增：第 7 節 —— 語法正確但不完整的紀錄（缺欄位、未知 kind、
#     生命週期矛盾、formal mode 不明）一律損壞、不可刪；真實歷史形狀與
#     runner 自己寫出的每一種狀態作為相容性對照。對應 round 11。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 10。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_run_record_persistence.py -v
# ------------------------------------------------------------

from __future__ import annotations

import errno
import json
import os
import re
import threading
from pathlib import Path

import pytest

import pcmef.console.runner as runner_module
from pcmef.console.runner import (
    RUN_CRASH_FILENAME,
    ConsoleRunner,
    RunLaunchError,
    RunnerError,
    RunRecord,
    RunRecordDamaged,
    RunSpec,
    parse_record,
)

flask = pytest.importorskip("flask")


# ---------------------------------------------------------------------------
# 共用
# ---------------------------------------------------------------------------


def _record(run_id: str, status: str = "running", note: str = "") -> RunRecord:
    return RunRecord(
        run_id=run_id, kind="sim_smoke", label="x", params={"preset": "standard"},
        command=["python", "-m", "pcmef.cli", "sim", "smoke"], status=status,
        started_at="2026-09-26T00:00:00+00:00", note=note,
        # runner 真的會寫的 exit code：succeeded 是 0、failed 不是 0、
        # unreaped 是 -1。寫錯的話 round 11 起這筆會被判為損壞，而用它當
        # 對照組的測試就會因為錯的理由通過。
        exit_code={"running": None, "succeeded": 0, "failed": 1}.get(status, -1),
        finished_at=None if status == "running" else "2026-09-26T00:01:00+00:00",
    )


@pytest.fixture()
def runner(tmp_path):
    runner = ConsoleRunner(tmp_path / "runs")
    runner.run_dir("r1").mkdir(parents=True)
    runner._save(_record("r1"))
    return runner


def _temps(runner, run_id="r1"):
    return sorted(p.name for p in runner.run_dir(run_id).glob(".run.json.*.tmp"))


class _OsWith:
    """runner 模組看到的 `os`，只換掉指定的幾個函式。**不動真的 os。**"""

    def __init__(self, **overrides) -> None:
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(os, name)


# ---------------------------------------------------------------------------
# 1 原子替換：讀者只看得到完整的一份
# ---------------------------------------------------------------------------


def test_a_reader_at_the_moment_of_replacement_sees_the_previous_record(
    runner, monkeypatch
):
    """同步點：寫入者停在「新內容已完整寫進暫存檔、還沒換上去」那一刻。

    先前的 `Path.write_text()` 在這個時刻已經把 run.json 截成 0 byte。
    """
    ready, go = threading.Event(), threading.Event()
    real_replace = runner_module._replace_file

    def held(source, target):
        ready.set()
        assert go.wait(10), "the test never released the writer"
        real_replace(source, target)

    monkeypatch.setattr(runner_module, "_replace_file", held)
    writer = threading.Thread(
        target=runner._save, args=(_record("r1", "succeeded", note="new"),)
    )
    writer.start()
    try:
        assert ready.wait(10), "the writer never reached the replacement"
        seen = runner.get("r1")
        assert seen.status == "running" and seen.note == "", (
            "before the replacement a reader must see the previous record"
        )
        pending = _temps(runner)
        assert len(pending) == 1, pending
        staged, problem = parse_record(
            (runner.run_dir("r1") / pending[0]).read_bytes(), "r1"
        )
        assert staged is not None and staged.status == "succeeded", (
            f"the staged file must already be complete: {problem}"
        )
    finally:
        go.set()
        writer.join(10)
    after = runner.get("r1")
    assert after.status == "succeeded" and after.note == "new"
    assert _temps(runner) == []


def test_every_state_transition_is_all_or_nothing(runner, monkeypatch):
    """同一筆紀錄連續更新十次，每一次都在替換前後各讀一次。"""
    real_replace = runner_module._replace_file
    observed: list[tuple[str, str]] = []

    def observing(source, target):
        observed.append(("before", runner.get("r1").note))
        real_replace(source, target)
        observed.append(("after", runner.get("r1").note))

    monkeypatch.setattr(runner_module, "_replace_file", observing)
    for index in range(10):
        runner._save(_record("r1", note=f"v{index}"))

    expected = []
    previous = ""
    for index in range(10):
        expected += [("before", previous), ("after", f"v{index}")]
        previous = f"v{index}"
    assert observed == expected


def test_concurrent_readers_never_see_a_partial_record(runner):
    """補充證據（壓力）：一個寫入者、三個讀者，零次半份。

    這條靠時序，因此只當補充；決定性的保證在上面兩條。
    """
    versions = [_record("r1", note=f"v{i:03d}" + "x" * (i % 17)) for i in range(150)]
    notes = {r.note for r in versions} | {""}
    done = threading.Event()
    failures: list[str] = []
    reads = [0]

    def read_until_done():
        while not done.is_set():
            try:
                note = runner.get("r1").note
            except Exception as error:  # noqa: BLE001
                failures.append(f"{type(error).__name__}: {error}")
                continue
            reads[0] += 1
            if note not in notes:
                failures.append(f"a record nobody wrote: {note!r}")

    readers = [threading.Thread(target=read_until_done) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        for version in versions:
            runner._save(version)
    finally:
        done.set()
        for thread in readers:
            thread.join(30)
    assert not failures, failures[:5]
    assert reads[0] > 0
    assert runner.get("r1").note == versions[-1].note


def test_a_reader_holding_the_old_file_is_not_given_half_of_the_new_one(runner,
                                                                       monkeypatch):
    """讀者正開著舊檔的那一刻。

    Windows：Python 開檔不帶 FILE_SHARE_DELETE，替換會被拒絕 —— 寫入者
    **等**，不是寫半份。POSIX：替換立刻成功，而讀者手上那個 handle 仍然
    是舊的那一份完整內容。兩者都不得出現中間狀態。
    """
    retried = threading.Event()
    real_pause = runner_module._pause

    def pause(seconds):
        retried.set()
        real_pause(seconds)

    monkeypatch.setattr(runner_module, "_pause", pause)
    path = runner.record_path("r1")
    before = path.read_bytes()
    handle = open(path, "rb")  # noqa: SIM115 - 刻意握著
    writer = threading.Thread(
        target=runner._save, args=(_record("r1", "succeeded", note="new"),)
    )
    try:
        writer.start()
        if os.name == "nt":
            assert retried.wait(10), "the replace should have waited for the reader"
            assert writer.is_alive(), "the writer must wait, not give up or corrupt"
        else:
            writer.join(10)
        assert handle.read() == before, "the open handle must hold the old record"
    finally:
        handle.close()
        writer.join(10)
    assert runner.get("r1").note == "new"
    assert _temps(runner) == []


# ---------------------------------------------------------------------------
# 2 換上去之前的任何失敗：上一份一個 byte 都不動
# ---------------------------------------------------------------------------


def _fail_on_open(real):
    def opened(path, flags, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, flags, *args, **kwargs)
    return opened


def _fail_after_half(real):
    calls = {"n": 0}

    def write(handle, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(handle, bytes(data)[: max(1, len(data) // 2)])
        raise OSError(errno.ENOSPC, "No space left on device")
    return write


def _fail(error):
    def failing(*_args, **_kwargs):
        raise error
    return failing


@pytest.mark.parametrize("where", ["open", "write", "fsync", "replace"])
def test_a_failure_before_the_replacement_leaves_the_record_untouched(
    runner, monkeypatch, where
):
    path = runner.record_path("r1")
    before = path.read_bytes()
    if where == "open":
        monkeypatch.setattr(runner_module, "os", _OsWith(open=_fail_on_open(os.open)))
    elif where == "write":
        monkeypatch.setattr(runner_module, "os", _OsWith(write=_fail_after_half(os.write)))
    elif where == "fsync":
        monkeypatch.setattr(runner_module, "os",
                            _OsWith(fsync=_fail(OSError(errno.EIO, "I/O error"))))
    else:
        monkeypatch.setattr(runner_module, "_replace_file",
                            _fail(OSError(errno.EXDEV, "cannot replace")))

    with pytest.raises(OSError):
        runner._save(_record("r1", "succeeded", note="new"))

    assert path.read_bytes() == before, "the previous record must survive byte for byte"
    assert runner.get("r1").status == "running"
    assert _temps(runner) == [], "a failed write must not leave its staging file"


def test_a_replace_that_keeps_being_refused_gives_up_in_bounded_time(runner,
                                                                    monkeypatch):
    """讀者一直不放手：重試有上限，逾時照樣拋，上一份照樣完整。"""
    pauses: list[float] = []
    monkeypatch.setattr(runner_module, "_pause", pauses.append)
    monkeypatch.setattr(runner_module, "_replace_file",
                        _fail(PermissionError(errno.EACCES, "in use")))
    before = runner.record_path("r1").read_bytes()

    with pytest.raises(PermissionError):
        runner._save(_record("r1", "succeeded"))

    expected = runner_module._REPLACE_ATTEMPTS - 1 if os.name == "nt" else 0
    assert len(pauses) == expected
    assert sum(pauses) < 5.0, "the writer is a background thread; it may not hang"
    assert runner.record_path("r1").read_bytes() == before
    assert _temps(runner) == []


def test_a_failed_terminal_update_keeps_the_last_record_and_says_so(runner,
                                                                  monkeypatch):
    """輸出執行緒的收尾寫不進去：上一份留著，run_crash.txt 說出想寫什麼。"""
    monkeypatch.setattr(runner_module, "_replace_file",
                        _fail(OSError(errno.ENOSPC, "No space left on device")))
    runner._save_or_leave_a_trace(_record("r1", "succeeded"), None)

    assert runner.get("r1").status == "running", "the previous record stays"
    trace = (runner.run_dir("r1") / RUN_CRASH_FILENAME).read_text(encoding="utf-8")
    assert "status=succeeded" in trace, "the trace names the outcome it could not record"
    assert "record_write_error=OSError" in trace
    assert "previous complete run.json left untouched" in trace


def test_the_run_page_says_when_the_last_update_failed(env, monkeypatch):
    """畫面上的是上一份紀錄 —— 它可能還寫著「執行中」。要說出來。"""
    client, runs = env
    run_id = _start(client)
    monkeypatch.setattr(runner_module, "_replace_file",
                        _fail(OSError(errno.ENOSPC, "No space left on device")))
    runner = client.application.config["PCMEF_CONSOLE_RUNNER"]
    record = runner.get(run_id)
    record.note = "outcome that could not be written"
    runner._save_or_leave_a_trace(record, None)

    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "record-update-failed" in body
    assert "outcome that could not be written" in body


# ---------------------------------------------------------------------------
# 3 初始紀錄寫不進去
# ---------------------------------------------------------------------------


def _refuse_record_writes(real):
    def write(target, payload):
        if Path(target).name == "run.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(target, payload)
    return write


def test_an_initial_record_that_cannot_be_written_starts_nothing(tmp_path,
                                                                monkeypatch):
    def no_process(*_a, **_k):
        raise AssertionError("no process may start without a record")

    monkeypatch.setattr("pcmef.console.runner.subprocess.Popen", no_process)
    monkeypatch.setattr(runner_module, "write_atomically",
                        _refuse_record_writes(runner_module.write_atomically))
    runner = ConsoleRunner(tmp_path / "runs")
    run_id = runner.allocate_run_id()

    with pytest.raises(RunLaunchError) as caught:
        runner.start(RunSpec(kind="sim_smoke", params={}), run_id=run_id)

    assert caught.value.process_reaped, "nothing was started, so nothing to reap"
    assert "initial record" in str(caught.value)
    assert not runner.record_path(run_id).exists(), "no partial run.json"
    trace = (runner.run_dir(run_id) / RUN_CRASH_FILENAME).read_text(encoding="utf-8")
    assert "no run.json was written" in trace
    assert "record_write_error=OSError" in trace
    # 證據留著的時候，這筆 run 是「損壞」，不是「不存在」。
    with pytest.raises(RunRecordDamaged):
        runner.get(run_id)
    assert [d.run_id for d in runner.damaged_runs()] == [run_id]
    assert runner.list_runs() == []


def test_a_launch_whose_initial_record_fails_is_refused_and_rolled_back(
    env, monkeypatch
):
    """經由 console.launch：round 1 的規則 —— 沒起跑的 run 什麼都不留 ——
    照舊成立；證據是拒絕訊息本身，而且畫面拿得到它。"""
    client, runs = env
    monkeypatch.setattr(runner_module, "write_atomically",
                        _refuse_record_writes(runner_module.write_atomically))
    before = set(p.name for p in runs.iterdir()) if runs.exists() else set()

    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })

    assert response.status_code == 409
    assert "initial record" in response.get_data(as_text=True)
    after = set(p.name for p in runs.iterdir()) if runs.exists() else set()
    assert after - before <= {".attribution_boundary.json"}, (
        "a run that never started must leave nothing behind"
    )
    assert not list(runs.glob("*/run.json"))


# ---------------------------------------------------------------------------
# 4 歷史損壞：只影響它自己
# ---------------------------------------------------------------------------


DAMAGED = {
    "zero_bytes": b"",
    "truncated": b'{"run_id": "bad", "kind": "sim_sm',
    "not_utf8": b'{"run_id": "\xff\xfe"}',
    "not_an_object": b"[]",
    "no_run_id": b'{"kind": "sim_smoke"}',
    "other_run": b'{"run_id": "somebody-else", "kind": "sim_smoke"}',
    "command_is_text": b'{"run_id": "bad", "kind": "sim_smoke", "command": "rm -rf"}',
    "params_is_list": b'{"run_id": "bad", "kind": "formal_e2", "params": [["mode", "formal"]]}',
    "exit_code_is_bool": b'{"run_id": "bad", "kind": "sim_smoke", "exit_code": true}',
}


@pytest.mark.parametrize("name", sorted(DAMAGED))
def test_a_damaged_record_is_reported_and_isolated(runner, name):
    bad = runner.run_dir("bad")
    bad.mkdir()
    (bad / "run.json").write_bytes(DAMAGED[name])

    with pytest.raises(RunRecordDamaged) as caught:
        runner.get("bad")
    assert caught.value.problem, "it must say what is wrong"
    assert caught.value.size == len(DAMAGED[name])
    assert isinstance(caught.value, RunnerError), "unhandled callers must not 500"

    listed = [r.run_id for r in runner.list_runs()]
    assert listed == ["r1"], "the damaged record must not take the listing down"
    damaged = runner.damaged_runs()
    assert [d.run_id for d in damaged] == ["bad"]
    assert damaged[0].record_bytes == len(DAMAGED[name])


def test_parse_record_never_invents_fields():
    """寬鬆的 from_json 會把字串拆成字元、把成對清單當成 dict —— 在壞紀錄上
    那就是在替它編內容。解析器必須拒絕，而不是補。"""
    for name, raw in DAMAGED.items():
        record, problem = parse_record(raw, "bad")
        assert record is None and problem, name


def test_a_damaged_record_is_never_deleted(runner):
    bad = runner.run_dir("bad")
    bad.mkdir()
    (bad / "run.json").write_bytes(b"")
    (bad / "log.txt").write_text("evidence", encoding="utf-8")

    with pytest.raises(RunRecordDamaged, match="Deletion is refused"):
        runner.delete("bad")
    assert (bad / "log.txt").read_text(encoding="utf-8") == "evidence"
    assert (bad / "run.json").exists()


# ---------------------------------------------------------------------------
# 5 全域頁面與 run-scoped 端點
# ---------------------------------------------------------------------------


class _NoProcess:
    stdout = iter(())
    pid = None

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from pcmef.admin.app import create_app

    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _NoProcess()
    )
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
    _select(client, "pcmef-thesis")
    return client, runs


def _select(client, project_id):
    client.post("/projects/select", data={"project_id": project_id},
                follow_redirects=True)


def _token(client) -> str:
    import importlib.util

    location = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_console_helpers", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.csrf_token(client)


def _start(client) -> str:
    response = client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    run_id = re.search(
        r"/console/runs/([\w.-]+)", response.headers["Location"]
    ).group(1)
    client.application.config["PCMEF_CONSOLE_RUNNER"].wait(run_id, timeout=30)
    return run_id


def _damage(runs: Path, run_id: str, raw: bytes = b"",
            pointer_mode: str | None = None) -> None:
    """把一筆有歸屬的 run 的 run.json 換成壞的。可選擇留下 formal 指標。"""
    (runs / run_id / "run.json").write_bytes(raw)
    if pointer_mode is not None:
        (runs / run_id / "formal_output.json").write_text(json.dumps({
            "kind": "formal_e2", "mode": pointer_mode,
            "one_shot": pointer_mode == "formal",
        }), encoding="utf-8")


@pytest.fixture()
def planted(env):
    """兩筆正常、一筆壞掉，全部屬於碩論。"""
    client, runs = env
    good = [_start(client), _start(client)]
    bad = _start(client)
    _damage(runs, bad)
    return client, runs, good, bad


@pytest.mark.parametrize("path", ["/console", "/results", "/formal"])
def test_one_damaged_record_does_not_take_down_a_global_page(planted, path):
    client, _runs, _good, bad = planted
    response = client.get(path)
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    assert bad in response.get_data(as_text=True), (
        "the damaged run must be named, not silently dropped"
    )


def test_results_keeps_the_valid_runs_and_lists_the_damaged_one_apart(planted):
    client, _runs, good, bad = planted
    body = client.get("/results").get_data(as_text=True)
    for run_id in good:
        assert run_id in body
    history, _, damaged_section = body.partition('id="damaged-runs"')
    assert bad in damaged_section and "run.json is empty" in damaged_section
    assert bad not in history, "a damaged record is not an ordinary history row"


@pytest.mark.parametrize("path, status", [
    ("/console/runs/{rid}", 409),
    ("/console/runs/{rid}/trace", 409),
    ("/console/runs/{rid}/trace/case-1", 409),
    ("/api/console/runs/{rid}/status", 409),
    ("/api/console/runs/{rid}/stream", 409),
])
def test_run_scoped_reads_explain_the_damage(planted, path, status):
    client, _runs, _good, bad = planted
    response = client.get(path.format(rid=bad))
    assert response.status_code == status
    text = response.get_data(as_text=True)
    assert "run.json is empty" in text, text[:400]


def test_the_status_endpoint_marks_the_record_as_damaged(planted):
    client, _runs, _good, bad = planted
    payload = client.get(f"/api/console/runs/{bad}/status").get_json()
    assert payload["damaged"] is True and payload["run_id"] == bad


@pytest.mark.parametrize("path", [
    "/api/console/runs/{rid}/delete", "/api/console/runs/{rid}/figures",
])
def test_run_scoped_writes_are_refused_and_change_nothing(planted, path):
    client, runs, _good, bad = planted
    before = sorted(p.name for p in (runs / bad).iterdir())
    response = client.post(path.format(rid=bad), data={"csrf_token": _token(client)})
    assert response.status_code == 409
    assert (runs / bad).is_dir()
    assert sorted(p.name for p in (runs / bad).iterdir()) == before


def test_another_project_gets_404_not_the_diagnosis(planted):
    """診斷內容屬於擁有它的 Project。別的 Project 看到的與正常紀錄相同：404。"""
    client, _runs, _good, bad = planted
    _select(client, "tiny-dummy")
    response = client.get(f"/console/runs/{bad}")
    assert response.status_code == 404
    assert "run.json is empty" not in response.get_data(as_text=True)
    assert bad not in client.get("/results").get_data(as_text=True)


def test_an_unattributed_damaged_record_is_listed_but_not_linked(env):
    """沒有歸屬檔的壞紀錄誰都不擁有 —— 但不得從畫面上消失。"""
    client, runs = env
    orphan = runs / "20260101T000000-dead00"
    orphan.mkdir(parents=True)
    (orphan / "run.json").write_bytes(b"{")

    body = client.get("/results").get_data(as_text=True)
    _, _, damaged_section = body.partition('id="damaged-runs"')
    assert orphan.name in damaged_section
    assert f'/console/runs/{orphan.name}"' not in damaged_section, (
        "no link: opening it could only 404"
    )
    assert client.get(f"/console/runs/{orphan.name}").status_code == 404


# ---------------------------------------------------------------------------
# 6 Formal E2 的刪除與稽核語意不得被壞紀錄削弱
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pointer_mode", ["formal", None, "dry-run"])
def test_no_damaged_record_is_deletable_whatever_its_pointer_says(env,
                                                                  pointer_mode):
    """指標說正式、說不知道、甚至說是預演 —— 一律不刪。

    預演本來可以刪；但一筆讀不出來的預演紀錄說不出自己跑完沒有、
    子行程收掉沒有，刪除前必須回答的問題它一個都答不出來。
    """
    client, runs = env
    run_id = _start(client)
    _damage(runs, run_id, pointer_mode=pointer_mode)

    response = client.post(f"/api/console/runs/{run_id}/delete",
                           data={"csrf_token": _token(client)})
    assert response.status_code == 409
    assert (runs / run_id / "run.json").exists()


@pytest.mark.parametrize("pointer_mode, listed", [
    ("formal", True), (None, True), ("dry-run", False),
])
def test_the_formal_workspace_keeps_accounting_for_damaged_records(
    env, pointer_mode, listed
):
    """正式執行紀錄那張表只列得出讀得到的。壞掉的、而且無法證明不是
    正式執行的，必須另外列出 —— 否則這一頁會少說一次「確實跑過」。"""
    client, runs = env
    run_id = _start(client)
    _damage(runs, run_id, raw=b'{"run_id": "', pointer_mode=pointer_mode)

    body = client.get("/formal").get_data(as_text=True)
    _, _, section = body.partition('id="damaged-formal-records"')
    assert (run_id in section) is listed
    if pointer_mode == "formal":
        assert "正式 Formal E2" in section
    if pointer_mode is None:
        assert "無法確定" in section


def test_describe_damage_reads_the_formal_pointer_and_nothing_else(env):
    client, runs = env
    run_id = _start(client)
    _damage(runs, run_id, pointer_mode="formal")
    runner = client.application.config["PCMEF_CONSOLE_RUNNER"]

    (damage,) = [d for d in runner.damaged_runs() if d.run_id == run_id]
    assert damage.formal_mode == "formal" and damage.may_be_formal
    assert damage.record_bytes == 0


def test_valid_records_keep_their_deletion_rules(env):
    """對照組：正常的正式紀錄照樣不可刪，正常的探索紀錄照樣可刪。"""
    client, runs = env
    runner = client.application.config["PCMEF_CONSOLE_RUNNER"]

    formal = _record("formal-ok", "failed")
    formal.kind, formal.params = "formal_e2", {"mode": "formal"}
    runner.run_dir("formal-ok").mkdir(parents=True)
    runner._save(formal)
    with pytest.raises(RunnerError, match="one-shot Formal E2"):
        runner.delete("formal-ok")
    assert runner.record_path("formal-ok").exists()

    explore = _start(client)
    response = client.post(f"/api/console/runs/{explore}/delete",
                           data={"csrf_token": _token(client)})
    # 表單送出的刪除成功時轉回 Run 首頁（302）。
    assert response.status_code == 302
    assert not (runs / explore).exists()


def test_a_damaged_record_never_appears_as_a_listed_record(env):
    """list_runs() 上的每一筆都會被問「是不是正式執行、能不能刪」。
    壞紀錄不得以任何補出來的形狀出現在那裡。"""
    client, runs = env
    run_id = _start(client)
    _damage(runs, run_id, raw=b'{"run_id": "%s", "kind": "sim_smoke", "params": []}'
            % run_id.encode(), pointer_mode="formal")
    runner = client.application.config["PCMEF_CONSOLE_RUNNER"]
    assert run_id not in [r.run_id for r in runner.list_runs(limit=500)]


# ---------------------------------------------------------------------------
# 7 語法正確、但說不完整的紀錄（round 11）
# ---------------------------------------------------------------------------

from pcmef.console.runner import FORMAL_CONFIRM_PHRASE, RECORD_SCHEMA  # noqa: E402

#: 真實歷史紀錄的形狀（2026-09-27 普查 outputs/console/runs 的 8 筆，逐欄照抄
#: 結構、換掉值）。它們是相容性對照組：嚴格化之後必須照樣讀得出來。
HISTORICAL_SHAPES = {
    "sim_smoke_2026_08": {
        "kind": "sim_smoke", "label": "標準",
        "params": {"classes": ["Empty"], "preset": "standard"},
        "command": ["python", "-u", "-m", "pcmef.cli", "sim", "smoke"],
        "status": "succeeded", "started_at": "2026-08-26T09:44:35+00:00",
        "finished_at": "2026-08-26T09:46:01+00:00", "exit_code": 0, "note": "",
    },
    "sim_smoke_seed_offset": {
        "kind": "sim_smoke", "label": "快速預覽",
        "params": {"classes": ["Empty"], "preset": "preview", "seed_offset": 3},
        "command": ["python", "-u", "-m", "pcmef.cli", "sim", "smoke"],
        "status": "succeeded", "started_at": "2026-09-07T05:39:32+00:00",
        "finished_at": "2026-09-07T05:40:02+00:00", "exit_code": 0, "note": "",
    },
    "llm_snapshot": {
        "kind": "llm_snapshot", "label": "檢查（不寫入）", "params": {"freeze": False},
        "command": ["python", "-u", "-m", "pcmef.cli", "llm", "snapshot"],
        "status": "succeeded", "started_at": "2026-08-31T16:11:02+00:00",
        "finished_at": "2026-08-31T16:11:09+00:00", "exit_code": 0, "note": "",
    },
    "formal_e2_dry_run": {
        "kind": "formal_e2", "label": "formal_e2", "params": {"mode": "dry-run"},
        "command": ["python", "-u", "-m", "pcmef.cli", "formal", "run-e2",
                    "--mode", "dry-run"],
        "status": "succeeded", "started_at": "2026-09-04T03:15:21+00:00",
        "finished_at": "2026-09-04T03:20:44+00:00", "exit_code": 0, "note": "",
    },
}


def _complete(run_id: str, **overrides) -> dict:
    """一筆完整、合法的 formal_e2 終局紀錄，再套上 overrides。"""
    data = {
        "run_id": run_id, "kind": "formal_e2", "label": "formal_e2",
        "params": {"mode": "formal", "confirm": FORMAL_CONFIRM_PHRASE},
        "command": ["python", "-m", "pcmef.cli", "formal", "run-e2"],
        "status": "failed", "started_at": "2026-09-26T00:00:00+00:00",
        "finished_at": "2026-09-26T00:10:00+00:00", "exit_code": 2, "note": "",
    }
    data.update(overrides)
    return data


def _write(runner, run_id: str, data) -> None:
    runner.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    runner.record_path(run_id).write_bytes(
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    )


def test_the_reviewers_example_is_damaged_not_a_dry_run(runner):
    """`{"run_id", "kind": "formal_e2", "status": "failed"}` —— 審查點名的那一筆。

    先前：缺的 params 被補成 {}，`protected` 把缺少的 mode 讀成 "dry-run"，
    status 是終局 —— 於是它是一筆「已結束的預演」，可以刪。
    """
    _write(runner, "bad", {"run_id": "bad", "kind": "formal_e2", "status": "failed"})

    with pytest.raises(RunRecordDamaged) as caught:
        runner.get("bad")
    for key in ("params", "command", "finished_at", "exit_code"):
        assert repr(key) in caught.value.problem, caught.value.problem
    assert "not filled in" in caught.value.problem

    assert "bad" not in [r.run_id for r in runner.list_runs()]
    (damaged,) = [d for d in runner.damaged_runs() if d.run_id == "bad"]
    assert damaged.may_be_formal and damaged.formal_mode is None
    with pytest.raises(RunRecordDamaged, match="Deletion is refused"):
        runner.delete("bad")
    assert runner.record_path("bad").exists()


def test_the_reviewers_example_stays_visible_and_undeletable_end_to_end(env):
    """同一筆，經由真的頁面：看得到、刪不掉、Formal 的稽核表不會少它。"""
    client, runs = env
    run_id = _start(client)
    runs.joinpath(run_id, "run.json").write_bytes(json.dumps(
        {"run_id": run_id, "kind": "formal_e2", "status": "failed"}
    ).encode("utf-8"))

    results = client.get("/results").get_data(as_text=True)
    assert run_id in results.partition('id="damaged-runs"')[2]
    formal = client.get("/formal").get_data(as_text=True)
    section = formal.partition('id="damaged-formal-records"')[2]
    assert run_id in section and "無法確定" in section
    assert client.get(f"/console/runs/{run_id}").status_code == 409
    response = client.post(f"/api/console/runs/{run_id}/delete",
                           data={"csrf_token": _token(client)})
    assert response.status_code == 409
    assert (runs / run_id / "run.json").exists()


@pytest.mark.parametrize("missing", RECORD_SCHEMA)
def test_every_field_is_required_and_none_is_invented(runner, missing):
    """十個欄位逐一拿掉一個：每一種都是損壞，而且原因點名缺的那一個。"""
    data = _complete("bad")
    del data[missing]
    record, problem = parse_record(json.dumps(data).encode("utf-8"), "bad")
    assert record is None
    assert repr(missing) in problem


INVALID = {
    "formal_without_mode": _complete("bad", params={}),
    "formal_mode_is_null": _complete("bad", params={"mode": None}),
    "formal_mode_is_unknown": _complete("bad", params={"mode": "full"}),
    "params_is_null": _complete("bad", params=None),
    "unknown_kind": _complete("bad", kind="experiment_e2", params={}),
    "unknown_status": _complete("bad", status="done"),
    "running_but_finished": _complete("bad", status="running"),
    "succeeded_with_nonzero_exit": _complete("bad", status="succeeded", exit_code=1),
    "failed_with_exit_zero": _complete("bad", status="failed", exit_code=0),
    "terminal_without_finish_time": _complete("bad", finished_at=None),
    "terminal_without_exit_code": _complete("bad", exit_code=None),
    "unreaped_without_exit_code": _complete("bad", status="unreaped", exit_code=None),
    "command_holds_numbers": _complete("bad", command=[1, 2]),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_an_invalid_but_well_formed_record_fails_closed(runner, name):
    """語法完全正確的 JSON，但說不出安全判斷需要的事 —— 一律損壞、不可刪。"""
    _write(runner, "bad", INVALID[name])
    with pytest.raises(RunRecordDamaged):
        runner.get("bad")
    assert "bad" not in [r.run_id for r in runner.list_runs()]
    with pytest.raises(RunRecordDamaged, match="Deletion is refused"):
        runner.delete("bad")
    assert runner.record_path("bad").exists()


def test_deserialisation_itself_invents_nothing():
    """最後一道：就算繞過 parse_record，from_json 也不補欄位。"""
    with pytest.raises(KeyError):
        RunRecord.from_json({"run_id": "x", "kind": "formal_e2", "status": "failed"})


@pytest.mark.parametrize("params, protected", [
    ({"mode": "formal"}, True),
    ({"mode": "dry-run"}, False),
    ({}, True),
    ({"mode": None}, True),
    ({"mode": "full"}, True),
])
def test_protection_does_not_default_a_missing_mode_to_dry_run(params, protected):
    """只有**明確**寫著 dry-run 的 formal_e2 才不受保護。"""
    record = RunRecord(run_id="x", kind="formal_e2", label="", params=params,
                       command=[], status="failed", exit_code=1,
                       finished_at="2026-09-26T00:00:00+00:00")
    assert record.protected is protected


def test_a_non_formal_kind_is_still_not_protected():
    record = RunRecord(run_id="x", kind="sim_smoke", label="", params={},
                       command=[], status="failed", exit_code=1,
                       finished_at="2026-09-26T00:00:00+00:00")
    assert record.protected is False


@pytest.mark.parametrize("params, stored", [
    ({}, "dry-run"),
    ({"mode": "dry-run"}, "dry-run"),
    ({"mode": "formal", "confirm": FORMAL_CONFIRM_PHRASE}, "formal"),
])
def test_a_new_formal_record_states_the_mode_it_actually_ran(tmp_path, monkeypatch,
                                                            params, stored):
    """寫入端：紀錄上的 mode 就是傳給 CLI 的 --mode。讀的人不必、也不得猜。"""
    monkeypatch.setattr("pcmef.console.runner.subprocess.Popen",
                        lambda *a, **k: _NoProcess())
    runner = ConsoleRunner(tmp_path / "runs", formal_out=tmp_path / "formal_out",
                           dry_run_base=tmp_path / "dry")
    record = runner.start(RunSpec(kind="formal_e2", params=params))
    runner.wait(record.run_id, timeout=30)

    on_disk = json.loads(runner.record_path(record.run_id).read_text(encoding="utf-8"))
    assert on_disk["params"]["mode"] == stored
    assert on_disk["command"][on_disk["command"].index("--mode") + 1] == stored
    assert runner.formal_pointer(record.run_id)["mode"] == stored
    assert runner.get(record.run_id).protected is (stored == "formal")


@pytest.mark.parametrize("name", sorted(HISTORICAL_SHAPES))
def test_historical_record_shapes_still_read_normally(runner, name):
    """相容性對照組：真實存在過的紀錄形狀，嚴格化之後照樣是正常紀錄。"""
    data = dict(HISTORICAL_SHAPES[name], run_id="hist")
    _write(runner, "hist", data)
    record = runner.get("hist")
    assert record.kind == data["kind"] and record.status == "succeeded"
    assert "hist" in [r.run_id for r in runner.list_runs()]
    assert not [d for d in runner.damaged_runs() if d.run_id == "hist"]


def test_historical_deletion_rules_are_unchanged(runner):
    """對照組：真實形狀的預演與探索紀錄照樣可刪；正式紀錄照樣不可刪。"""
    for name in ("sim_smoke_2026_08", "formal_e2_dry_run"):
        _write(runner, name.replace("_", "-"),
               dict(HISTORICAL_SHAPES[name], run_id=name.replace("_", "-")))
        runner.delete(name.replace("_", "-"))
        assert not runner.run_dir(name.replace("_", "-")).exists()
    formal = dict(HISTORICAL_SHAPES["formal_e2_dry_run"], run_id="formal-real",
                  params={"mode": "formal", "confirm": FORMAL_CONFIRM_PHRASE})
    _write(runner, "formal-real", formal)
    with pytest.raises(RunnerError, match="one-shot Formal E2"):
        runner.delete("formal-real")


def test_every_state_the_runner_writes_reads_back_as_a_normal_record(tmp_path,
                                                                    monkeypatch):
    """相容性對照組：runner 自己寫得出來的每一種狀態，嚴格化之後都讀得回來。

    running（啟動時）、succeeded / failed（輸出執行緒收尾）、unreaped
    （收不掉的子行程）—— 走真的程式路徑，不手寫。
    """
    class _Exits:
        def __init__(self, code):
            self.stdout, self.code, self.pid = iter(()), code, None

        def wait(self, timeout=None):
            return self.code

        def poll(self):
            return self.code

    runner = ConsoleRunner(tmp_path / "runs")
    seen = {}
    for code in (0, 3):
        monkeypatch.setattr("pcmef.console.runner.subprocess.Popen",
                            lambda *a, _c=code, **k: _Exits(_c))
        record = runner.start(RunSpec(kind="sim_smoke", params={}))
        runner.wait(record.run_id, timeout=30)
        seen[runner.get(record.run_id).status] = record.run_id

    import threading

    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    running = runner.start(RunSpec(kind="sim_smoke", params={}))
    monkeypatch.undo()
    seen[runner.get(running.run_id).status] = running.run_id

    unreaped = runner.get(running.run_id)
    unreaped.status, unreaped.exit_code = "unreaped", -1
    unreaped.finished_at = "2026-09-26T00:00:00+00:00"
    runner._save(unreaped)
    seen[runner.get(running.run_id).status] = running.run_id

    assert set(seen) == {"succeeded", "failed", "running", "unreaped"}
    assert runner.damaged_runs() == []
