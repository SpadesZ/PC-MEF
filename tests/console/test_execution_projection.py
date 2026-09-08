# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證「畫面上看到的狀態」與「磁碟上的
#         事實」不會分岔。全部在 tmp_path，不觸發任何真正的科研寫入。
# 檔案路徑: tests/console/test_execution_projection.py
# 產生時間: 2026-09-08 09:40 +08:00
# 版本: v0.1.0
# 功能說明: 動作層閉合第七輪 —— tripwire 的 cwd 綁定、Formal 成功事件
#           的位置、marker-only 的 unreaped 投影，以及 pipeline-scope
#           run 的進度呈現。
# 模組定位: test_execution_terminal_state.py 的續作。前一輪讓紀錄不再
#           自相矛盾；這一輪處理**紀錄與畫面之間**的落差 —— 事件說
#           completed 而 run.json 說 failed、run.json 說 running 而
#           marker 說行程收不掉、七個節點全灰而其實跑的是整條鏈。
# 主要責任:
#   1. WriteTripwire 的目錄快取綁定當下 cwd
#   2. Formal 的成功事件緊貼真正的成功 return
#   3. marker 存在即投影成 terminal unreaped
#   4. pipeline-scope 的執行以整體進度呈現，不假裝成單一節點
# 維護提醒:
#   - **不得讓快取跨 cwd 沿用。** `"."` 在 chdir 之後指的是別的地方，
#     而守衛記得的是舊答案 —— 那是一條走得通的繞道。
#   - 不得把成功事件往前搬到後處理之前。後處理炸掉時，事件會說成功
#     而 exit code 說失敗，兩者都留在磁碟上。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 7。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_projection.py -v
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


class _Fake:
    stdout = iter(())

    def wait(self, timeout=None):
        return 0


def _guard_module():
    import importlib.util

    location = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location("_pcmef_repo_guard", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1 tripwire 的 cwd 綁定
# ---------------------------------------------------------------------------


def test_a_cached_relative_parent_does_not_survive_a_chdir(tmp_path, monkeypatch):
    """**`"."` 在 chdir 之後指的是別的地方。**

    先前快取的鍵是 `os.path.dirname(raw) or "."`，於是一個裸檔名永遠
    落在鍵 `"."` 上。在普通目錄底下寫過一次之後，那個鍵記住的是舊
    cwd；接著 chdir 到受保護目錄再用裸檔名寫，守衛會沿用舊答案而
    放行 —— 一條完全走得通的繞道。
    """
    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    (tmp_path / "plain").mkdir()
    tripwire = guard.WriteTripwire([protected])

    # 先在普通目錄底下寫一次，讓快取記住 "." 的解析結果。
    monkeypatch.chdir(tmp_path / "plain")
    tripwire.check_write("harmless.txt")
    assert not tripwire.violations

    # 換到受保護目錄，用裸檔名寫。
    monkeypatch.chdir(protected)
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write("lock.json")


def test_a_cached_dotdot_parent_does_not_survive_a_chdir(tmp_path, monkeypatch):
    """`../x` 同理：相對片段的意義跟著 cwd 走。"""
    guard = _guard_module()
    protected = tmp_path / "freeze"
    (protected / "inner").mkdir(parents=True)
    (tmp_path / "plain" / "inner").mkdir(parents=True)
    tripwire = guard.WriteTripwire([protected])

    monkeypatch.chdir(tmp_path / "plain" / "inner")
    tripwire.check_write("../harmless.txt")
    assert not tripwire.violations

    monkeypatch.chdir(protected / "inner")
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write("../lock.json")


def test_the_audit_hook_catches_a_bare_name_after_chdir(tmp_path, monkeypatch):
    """端到端：真的 chdir 進去再用裸檔名 open()。"""
    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    (tmp_path / "plain").mkdir()
    tripwire = guard.WriteTripwire([protected])
    guard._install_tripwire(tripwire)

    monkeypatch.chdir(tmp_path / "plain")
    with open("warmup.txt", "w") as handle:      # 讓快取先記住 "."
        handle.write("x")

    monkeypatch.chdir(protected)
    with pytest.raises(guard.ProtectedPathWrite):
        with open("sneaky.json", "w") as handle:
            handle.write("{}")
    assert not (protected / "sneaky.json").exists()


def test_the_cache_still_avoids_repeated_resolution(tmp_path, monkeypatch):
    """綁定 cwd 之後仍然不得每次寫入都 resolve()。

    快取是為了讓守衛便宜到沒有人想關掉它。修好正確性的同時把成本
    加回去，等於換一種方式失去守衛。
    """
    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    (tmp_path / "plain").mkdir()
    tripwire = guard.WriteTripwire([protected])
    monkeypatch.chdir(tmp_path / "plain")

    resolved = {"n": 0}
    original = Path.resolve

    def counting(self, *args, **kwargs):
        resolved["n"] += 1
        return original(self, *args, **kwargs)

    import unittest.mock

    with unittest.mock.patch.object(Path, "resolve", counting):
        for index in range(200):
            tripwire.check_write(f"file{index}.txt")

    assert resolved["n"] <= 2, (
        f"200 writes into one directory cost {resolved['n']} resolutions"
    )


# ---------------------------------------------------------------------------
# 2 Formal 成功事件的位置
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


def _terminal_events(events_dir: Path):
    from pcmef.platform.runs import STAGE_COMPLETED, STAGE_FAILED, read_events

    return [
        event.event for event in read_events(events_dir)[0]
        if event.event in (STAGE_COMPLETED, STAGE_FAILED)
    ]


def _stub_preflight(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pcmef.cli._formal_preflight",
        lambda args: {"checks": [], "allowed": True, "blockers": [],
                      "lineage": {"resolved_freeze_dir": str(tmp_path / "freeze")}},
    )


def _document(**overrides):
    document = {
        "report_path": "r.json", "dry_run": True, "scientific_result": "n/a",
        "llm_arm_evaluated": False, "dataset": {"total_rows": 0},
        "routing": {"counts": {}, "escalated_cases": 0, "escalation_rate": 0.0},
        "skipped_escalated_cases": 0, "results": {},
        "worst_condition_macro_f1": {}, "worst_condition_at": {},
    }
    document.update(overrides)
    return document


def test_a_crash_while_formatting_the_summary_is_not_recorded_as_success(
    tmp_path, monkeypatch
):
    """**成功事件不得早於可能失敗的後處理。**

    先前 `stage_completed()` 發在二十幾行 document 取值與格式化之前。
    那段炸掉的話：事件檔說 completed（終局唯一，後到的 failed 被丟），
    而 CLI 以非零結束、run.json 說 failed。兩份紀錄各說各話，而它們
    都在磁碟上。
    """
    _stub_preflight(monkeypatch, tmp_path)
    # 少一個鍵就會在後處理那一段 KeyError。
    broken = _document()
    broken.pop("scientific_result")
    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        lambda *a, **k: broken,
    )
    from pcmef import cli

    with pytest.raises(BaseException):
        cli.cmd_formal_run_e2(_formal_args(tmp_path))

    assert _terminal_events(tmp_path / "run") == ["stage_failed"], (
        "a run that never finished printing its summary did not succeed"
    )


def test_a_crash_in_the_per_arm_table_is_not_recorded_as_success(tmp_path,
                                                                 monkeypatch):
    """逐臂表格那一段同樣在成功事件之後。"""
    _stub_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        # `results` 的每一項缺 accuracy，格式化時炸開。
        lambda *a, **k: _document(results={"vision_only": {"macro_f1": 0.1}}),
    )
    from pcmef import cli

    with pytest.raises(BaseException):
        cli.cmd_formal_run_e2(_formal_args(tmp_path))

    assert _terminal_events(tmp_path / "run") == ["stage_failed"]


def test_a_clean_formal_run_still_records_success(tmp_path, monkeypatch):
    """對照組：真的跑完就要留下 completed。"""
    _stub_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        lambda *a, **k: _document(),
    )
    from pcmef import cli

    assert cli.cmd_formal_run_e2(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


def test_the_success_event_is_the_last_statement_before_the_return():
    """結構性守衛：成功事件與 `return 0` 之間不得有其他語句。

    中間夾任何一行，那一行炸掉時就會出現「事件說成功、exit code 說
    失敗」。這條測試讓那個距離維持在零。
    """
    import ast
    import inspect
    import textwrap

    from pcmef import cli

    tree = ast.parse(textwrap.dedent(inspect.getsource(cli._formal_run_e2_body)))
    function = tree.body[0]

    for block in ast.walk(function):
        body = getattr(block, "body", None)
        if not isinstance(body, list):
            continue
        for index, node in enumerate(body):
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
                continue
            call = node.value
            if not (isinstance(call.func, ast.Attribute)
                    and call.func.attr == "stage_completed"):
                continue
            following = body[index + 1:]
            assert following, "stage_completed must be followed by the return"
            assert isinstance(following[0], ast.Return), (
                "stage_completed is followed by "
                f"{type(following[0]).__name__} at line {following[0].lineno}; "
                "anything that can raise between the success event and the "
                "return can make the events say completed while the exit "
                "code says failed"
            )


# ---------------------------------------------------------------------------
# 3 marker-only 的 unreaped 投影
# ---------------------------------------------------------------------------


def _plant_marker_only(runs: Path, run_id: str) -> None:
    """造出「run.json 說 running，但 marker 已經在了」的狀態。

    這不是假想：`_save_or_leave_a_trace()` 可能寫不成 run.json，而
    marker 走的是另一條 `os.open` 路徑，兩者會各自成功或失敗。
    """
    directory = runs / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps({
        "run_id": run_id, "kind": "sim_smoke", "label": "x", "params": {},
        "command": ["python", "-m", "pcmef.cli", "sim", "smoke"],
        "status": "running", "started_at": "2026-09-08T00:00:00+00:00",
        "finished_at": None, "exit_code": None, "note": "",
    }), encoding="utf-8")
    (directory / "log.txt").write_text("x\n", encoding="utf-8")
    (directory / "unreaped_process.json").write_text(json.dumps({
        "run_id": run_id, "pid": 778899, "command": ["python"],
        "host": "test-host", "started_at": "2026-09-08T00:00:00+00:00",
        "reason": "RuntimeError: reader died",
    }), encoding="utf-8")


def test_a_marker_only_record_is_projected_as_unreaped(tmp_path):
    """run.json 說 running，marker 說收不掉 —— **以 marker 為準。**

    marker 存在就代表沒有人在讀那個行程了。繼續顯示 running 會讓
    清單上出現一筆永遠不會結束的執行，而它其實早就沒人管了。
    """
    from pcmef.console.runner import ConsoleRunner

    runner = ConsoleRunner(tmp_path / "runs")
    _plant_marker_only(tmp_path / "runs", "marker-only")

    record = runner.get("marker-only")
    assert record.unreaped, "the marker is the stronger signal"
    assert record.finished, "nobody is reading it; the stream must be able to stop"


def test_the_projection_also_applies_to_the_listing(tmp_path):
    """清單與單筆必須說同一句話。"""
    from pcmef.console.runner import ConsoleRunner

    runner = ConsoleRunner(tmp_path / "runs")
    _plant_marker_only(tmp_path / "runs", "marker-only")

    listed = {r.run_id: r for r in runner.list_runs()}
    assert listed["marker-only"].unreaped


def test_the_projection_does_not_rewrite_the_file(tmp_path):
    """投影是**讀取時**的解讀，不必回寫磁碟。

    run.json 寫不成功正是走到這裡的原因之一；再寫一次只會再失敗，
    而且會把「當時到底發生什麼」蓋掉。
    """
    from pcmef.console.runner import ConsoleRunner

    runner = ConsoleRunner(tmp_path / "runs")
    _plant_marker_only(tmp_path / "runs", "marker-only")
    before = (tmp_path / "runs" / "marker-only" / "run.json").read_text(
        encoding="utf-8"
    )

    runner.get("marker-only")
    runner.list_runs()

    after = (tmp_path / "runs" / "marker-only" / "run.json").read_text(
        encoding="utf-8"
    )
    assert after == before, "reading must not rewrite the record"


def test_the_status_endpoint_and_stream_agree_with_the_projection(env):
    """端點層：status 說 unreaped，SSE 收線。"""
    client, runs = env
    _select(client, "pcmef-thesis")
    _plant_marker_only(runs, "marker-only")

    status = client.get("/api/console/runs/marker-only/status").get_json()
    assert status["status"] == "unreaped"

    body = client.get(
        "/api/console/runs/marker-only/stream"
    ).get_data(as_text=True)
    assert "event: done" in body, "the stream must close on a projected terminal"


def test_the_run_page_shows_the_projected_state(env):
    client, runs = env
    _select(client, "pcmef-thesis")
    _plant_marker_only(runs, "marker-only")

    body = client.get("/console/runs/marker-only").get_data(as_text=True)
    assert "778899" in body, "the page must name the pid"
    assert "unreaped" in body


def test_a_plain_running_record_is_untouched(tmp_path):
    """對照組：沒有 marker 就照常是 running。"""
    from pcmef.console.runner import ConsoleRunner

    runner = ConsoleRunner(tmp_path / "runs")
    _plant_marker_only(tmp_path / "runs", "still-running")
    (tmp_path / "runs" / "still-running" / "unreaped_process.json").unlink()

    record = runner.get("still-running")
    assert record.status == "running"
    assert not record.finished


# ---------------------------------------------------------------------------
# 4 pipeline-scope 的進度呈現
# ---------------------------------------------------------------------------


def test_formal_events_do_not_land_on_the_decision_node():
    """整體進度不得掛在某一個研究節點名下。

    掛在 `decision` 上的話，Run 頁會顯示「決策這一步完成了」——
    而實際發生的是整條鏈跑完了，其餘六個節點看起來沒動過。
    """
    from pcmef.platform.executors import STAGE_DECISION, executor_of

    formal = executor_of("pcmef-thesis", "formal_e2")
    assert formal is not None
    assert formal.event_stage_id != STAGE_DECISION, (
        "an aggregate run must not file its events under a single node"
    )
    assert formal.scope == "pipeline"
    assert STAGE_DECISION in formal.stage_ids, (
        "it still covers decision; it just does not pretend to be it"
    )


def test_the_cli_files_formal_events_in_the_aggregate_bucket(tmp_path,
                                                             monkeypatch):
    """CLI 寫出的 stage_id 必須是那個 aggregate 名字。"""
    from pcmef.platform.executors import executor_of
    from pcmef.platform.runs import read_events

    _stub_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        lambda *a, **k: _document(),
    )
    from pcmef import cli

    cli.cmd_formal_run_e2(_formal_args(tmp_path))

    expected = executor_of("pcmef-thesis", "formal_e2").event_stage_id
    stages = {e.stage_id for e in read_events(tmp_path / "run")[0] if e.stage_id}
    assert stages == {expected}


def test_the_run_page_shows_aggregate_progress_for_a_pipeline_scope_run(
    env, monkeypatch
):
    """Run 頁要說「整體進度」，而不是列七個沒動過的節點。"""
    from pcmef.platform.runs import RunEventWriter

    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Fake()
    )
    client.post("/formal/start", data={
        "mode": "dry-run", "csrf_token": _token(client),
    })
    run_id = next(iter(_run_ids(runs)))

    from pcmef.platform.executors import executor_of

    bucket = executor_of("pcmef-thesis", "formal_e2").event_stage_id
    writer = RunEventWriter(runs / run_id)
    writer.run_started()
    writer.stage_started(bucket, total=None, detail="pre-flight")
    writer.stage_progress(bucket, 3, None, detail="scoring")

    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "整體" in body, "a pipeline-scope run reports overall progress"
    assert "事件檔提到流程定義裡沒有的 stage" not in body, (
        "the aggregate bucket is expected, not an anomaly"
    )


def test_a_single_stage_run_still_shows_its_node(env, monkeypatch):
    """對照組：單一節點的執行照常列在流程裡。"""
    from pcmef.platform.runs import RunEventWriter

    client, runs = env
    _select(client, "pcmef-thesis")
    monkeypatch.setattr(
        "pcmef.console.runner.subprocess.Popen", lambda *a, **k: _Fake()
    )
    client.post("/api/console/runs", data={
        "kind": "sim_smoke", "preset": "standard", "csrf_token": _token(client),
    })
    run_id = next(iter(_run_ids(runs)))

    writer = RunEventWriter(runs / run_id)
    writer.run_started()
    writer.stage_started("simulation", total=4)
    writer.stage_progress("simulation", 2, 4)

    body = client.get(f"/console/runs/{run_id}").get_data(as_text=True)
    assert "2 / 4" in body


# ---------------------------------------------------------------------------
# 本機環境：測試不得留下 listener
# ---------------------------------------------------------------------------


def test_no_test_opens_a_listening_socket():
    """8790 是唯一的正式 localhost UI port；測試一律走 test_client。"""
    import re

    offenders = []
    here = Path(__file__).name
    for path in sorted(Path("tests").rglob("*.py")):
        if path.name == here:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\.run\(\s*host=|socket\.bind\(|make_server\(", line):
                offenders.append(f"{path.as_posix()}:{number}")
    assert not offenders, f"these start a real listener: {offenders}"
