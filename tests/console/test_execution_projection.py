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
import sys
import socket
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
# 1b Tripwire：最後一段自己是別名
# ---------------------------------------------------------------------------


def _can_make_file_symlinks(where: Path) -> bool:
    """這台機器能不能做出 file symlink。

    用實際做一個來判斷，而不是看 `os.name` —— Windows 開了
    Developer Mode 就做得出來，而寫死 `os.name == "nt"` 會讓這條
    守衛在做得到的機器上也一起被跳過。
    """
    probe = where / "_probe_target"
    link = where / "_probe_link"
    try:
        probe.write_text("x", encoding="utf-8")
        os.symlink(probe, link)
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        for path in (link, probe):
            try:
                path.unlink()
            except OSError:
                pass
    return True


def test_a_write_through_a_file_symlink_never_reaches_the_protected_file(
    tmp_path,
):
    """**P0：最後一段是 symlink 的那條繞道。**

    `safe/alias.json -> protected/lock.json`：名字裡沒有任何受保護
    根目錄的字樣，父層 `safe/` 解析出來也乾乾淨淨 —— 只解析所在目錄
    的守衛因此整條放行，而 `open(alias, "w")` 會沿著連結寫進去。

    這裡用真的 file symlink，而且檢查受保護的檔案**內容沒有被動過**：
    「有沒有擋下例外」與「有沒有真的寫進去」是兩件事。
    """
    if not _can_make_file_symlinks(tmp_path):
        pytest.skip("this machine cannot create file symlinks")

    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    target = protected / "lock.json"
    target.write_text("ORIGINAL", encoding="utf-8")

    safe = tmp_path / "safe"
    safe.mkdir()
    alias = safe / "alias.json"
    os.symlink(target, alias)

    tripwire = guard.WriteTripwire([protected])
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write(str(alias))

    assert target.read_text(encoding="utf-8") == "ORIGINAL", (
        "the protected file was modified through the alias"
    )


def test_the_audit_hook_blocks_an_open_through_a_file_symlink(tmp_path):
    """端到端：真的 `open(alias, "w")`，由稽核事件擋下。"""
    if not _can_make_file_symlinks(tmp_path):
        pytest.skip("this machine cannot create file symlinks")

    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    target = protected / "lock.json"
    target.write_text("ORIGINAL", encoding="utf-8")
    safe = tmp_path / "safe"
    safe.mkdir()
    alias = safe / "alias.json"
    os.symlink(target, alias)

    tripwire = guard.WriteTripwire([protected])
    guard._install_tripwire(tripwire)

    with pytest.raises(guard.ProtectedPathWrite):
        with open(alias, "w", encoding="utf-8") as handle:
            handle.write("OVERWRITTEN")

    assert target.read_text(encoding="utf-8") == "ORIGINAL"


def test_a_junction_final_component_is_blocked_even_though_islink_says_no(
    tmp_path,
):
    """Windows junction：`os.path.islink()` 說不是連結，`resolve()` 卻跟著走。

    這是同一條繞道的 Windows 版本。用 `islink()` 單獨判定的修法會在
    這裡整個漏掉，所以這條測試刻意把 `islink()` 的答案一起斷言出來
    —— 它為 False 而守衛仍然必須擋下。
    """
    if os.name != "nt":
        pytest.skip("junctions are a Windows construct")

    import subprocess

    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()
    alias = safe / "alias"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(protected)],
        capture_output=True, text=True,
    )
    if made.returncode != 0:
        pytest.skip("this machine cannot create junctions")

    assert os.path.islink(alias) is False, (
        "if islink() ever starts reporting junctions, this test's premise "
        "changed — the guard must still not depend on it"
    )

    tripwire = guard.WriteTripwire([protected])
    with pytest.raises(guard.ProtectedPathWrite):
        tripwire.check_write(str(alias))


def test_an_ordinary_file_beside_an_alias_is_still_allowed(tmp_path):
    """對照組：同一個目錄裡的普通檔案不得被誤擋。"""
    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()

    tripwire = guard.WriteTripwire([protected])
    tripwire.check_write(str(safe / "ordinary.json"))
    assert not tripwire.violations


def test_the_alias_check_does_not_resolve_paths_that_do_not_exist(tmp_path):
    """代價要停在 lstat：還不存在的落點不得付 resolve() 的錢。

    絕大多數寫入的目標都還不存在（建立它的就是這次寫入），所以這條
    路徑上一次 `resolve()` 都不該發生。
    """
    import unittest.mock

    guard = _guard_module()
    protected = tmp_path / "freeze"
    protected.mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()
    tripwire = guard.WriteTripwire([protected])
    tripwire.check_write(str(safe / "warm.json"))   # 先把父層快取起來

    resolved = {"n": 0}
    original = Path.resolve

    def counting(self, *args, **kwargs):
        resolved["n"] += 1
        return original(self, *args, **kwargs)

    with unittest.mock.patch.object(Path, "resolve", counting):
        for index in range(50):
            tripwire.check_write(str(safe / f"new{index}.json"))

    assert resolved["n"] == 0, (
        f"{resolved['n']} resolutions for writes to files that do not exist"
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


def test_a_crash_while_formatting_the_summary_still_records_success(
    tmp_path, monkeypatch, capsys
):
    """**已經完成的科學結果不得被呈現層改判成失敗。**

    `run_formal_e2_full()` 回來的那一刻，report 已經寫在磁碟上、
    一次性 claim 也已經標成 COMPLETE。之後那二十幾行純粹是排版。

    round 7 把成功事件移到 `return 0` 前面，讓事件檔與 exit code 對上
    ——但對到的是**失敗**：磁碟上的 report 說成功，CLI 卻說失敗，而
    claim 已經 COMPLETE，重跑會被擋住。排版失敗因此改為 fail-soft。
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

    assert cli.cmd_formal_run_e2(_formal_args(tmp_path)) == 0, (
        "the science finished; a formatting error must not change the exit code"
    )
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]
    assert "warning" in capsys.readouterr().err.lower(), (
        "fail-soft must still say out loud that the summary was not rendered"
    )


def test_a_crash_in_the_per_arm_table_still_records_success(tmp_path,
                                                            monkeypatch):
    """逐臂表格那一段同樣只是排版，炸掉不改判。"""
    _stub_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        # `results` 的每一項缺 accuracy，格式化時炸開。
        lambda *a, **k: _document(results={"vision_only": {"macro_f1": 0.1}}),
    )
    from pcmef import cli

    assert cli.cmd_formal_run_e2(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


def test_a_broken_stdout_does_not_turn_a_finished_run_into_a_failure(
    tmp_path, monkeypatch
):
    """stdout 壞掉是最真實的那一種：`| head` 關掉管線就會這樣。

    連 fail-soft 的警告本身都印不出去，而科學結果仍然必須是成功。
    """
    _stub_preflight(monkeypatch, tmp_path)

    class _ClosedPipe:
        def write(self, *_a, **_k):
            raise BrokenPipeError(32, "broken pipe")

        def flush(self, *_a, **_k):
            raise BrokenPipeError(32, "broken pipe")

    def _science_then_the_pipe_closes(*_a, **_k):
        # 管線是在科學跑完之後才斷的 —— 那正是要守的那一刻。
        monkeypatch.setattr(sys, "stdout", _ClosedPipe())
        monkeypatch.setattr(sys, "stderr", _ClosedPipe())
        return _document()

    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        _science_then_the_pipe_closes,
    )
    from pcmef import cli

    assert cli.cmd_formal_run_e2(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


def test_a_malformed_document_cannot_break_the_success_event(tmp_path,
                                                             monkeypatch):
    """終局事件取值一律 .get()：它與 return 之間不得有會炸的東西。"""
    _stub_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pcmef.experiments.e2_formal.run_formal_e2_full",
        # 連 dry_run 都沒有 —— 事件仍然要發得出去。
        lambda *a, **k: {"results": {}},
    )
    from pcmef import cli

    assert cli.cmd_formal_run_e2(_formal_args(tmp_path)) == 0
    assert _terminal_events(tmp_path / "run") == ["stage_completed"]


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


def test_the_listener_guard_is_a_runtime_guard_not_a_source_scan():
    """8790 是唯一的正式 localhost UI port。

    先前這條規則是掃原始碼守的 —— 而掃原始碼守不住任何東西：
    `getattr(socket, "bind")`、包一層 helper、從 library 裡繞出去，
    三種寫法都掃不到，三種都會真的佔住一個 port。守衛因此改成在
    綁定當下攔截，這條測試確認它真的是那樣運作的。
    """
    guard = _guard_module()

    assert hasattr(guard, "ListenerTripwire"), (
        "the listener guard must exist as a runtime tripwire"
    )
    assert guard.OFFICIAL_UI_PORT == 8790
    # 真的綁下去才算數：這裡用一個獨立的 tripwire 實例，不動 session 的。
    listeners = guard.ListenerTripwire()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(guard.RealListenerOpened):
            listeners.check_bind(sock, ("127.0.0.1", 8791))
    finally:
        sock.close()
    assert listeners.violations, "a blocked bind must still be recorded"


def _session_guard(request):
    """pytest 自己載入的那一份 `tests/conftest.py`。

    `_guard_module()` 是用 importlib 另外載的一份副本，於是它的
    `RealListenerOpened` 與 session 實際掛上去的**不是同一個類別**，
    `LISTENERS` 也是另一個物件。要斷言真實綁定被擋下，必須拿到
    session 這一份。

    **不得用 `sys.modules["conftest"]` 去找。** `tests/conftest.py`
    與 `tests/console/conftest.py` 兩份都叫 `conftest`，後載入的會把
    前一份蓋掉 —— 拿到的是隔壁那個。pytest 的 plugin manager 兩份
    都留著，而且記得各自的檔案路徑。
    """
    here = Path(__file__).resolve().parents[1] / "conftest.py"
    for plugin in request.config.pluginmanager.get_plugins():
        path = getattr(plugin, "__file__", None)
        if path and Path(path).resolve() == here:
            return plugin
    raise AssertionError("the session conftest is not registered with pytest")


def test_a_bind_that_bypasses_the_literal_socket_bind_spelling_is_still_caught(
    request,
):
    """繞過字面寫法照樣被擋 —— 稽核事件看的是行為，不是拼法。

    `getattr(sock, "bind")(...)` 這一行，舊的正規式掃描器一個字都
    對不上，而它會真的佔住 8792。
    """
    def bind_via_getattr(sock, address):
        getattr(sock, "bind")(address)

    guard = _session_guard(request)
    # 這個測試**故意**去踩守衛，因此必須把自己製造的那一筆從 session
    # 的帳上抹掉 —— 否則 session 結束時的總結會把它算成真的違規。
    before = list(guard.LISTENERS.violations)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError) as caught:
            bind_via_getattr(sock, ("127.0.0.1", 8792))
    finally:
        sock.close()
        guard.LISTENERS.violations[:] = before
    assert type(caught.value).__name__ == "RealListenerOpened", (
        f"expected the listener guard to stop this, got {caught.value!r}"
    )
    assert 8792 not in guard.listening_ports(), (
        "the blocked bind must not have left 8792 listening"
    )


def test_flask_test_client_is_not_affected_by_the_listener_guard(env):
    """`test_client` 走 WSGI，一個 socket 都不碰 —— 必須照常可用。"""
    guard = _guard_module()
    before = len(guard.LISTENERS.violations)

    client, _ = env
    assert client.get("/console").status_code == 200

    assert len(guard.LISTENERS.violations) == before, (
        "the test client must not register as opening a listener"
    )
    assert not guard.LISTENERS.attempts, (
        "the WSGI test client must not bind any socket at all"
    )


def test_a_declared_temporary_listener_is_permitted_and_leaves_nothing_behind(
    request,
):
    """真的需要 listener 就要明講。宣告過就放行，收掉之後不留痕跡。"""
    guard = _session_guard(request)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with guard.LISTENERS.allowing(0):
            # port 0 讓 OS 挑一個空的，宣告過所以放行。
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            opened = sock.getsockname()[1]
            assert opened != guard.OFFICIAL_UI_PORT, (
                "a test must never take the official UI port"
            )
            # **收在宣告裡面。** 先前這一行在 `with` 之外：宣告已經結束，
            # listener 還開著。round 9 之前 `allowing(0)` 驗不到這一步，
            # 所以這條測試一直通過；現在離開時看的是 socket 物件本身。
            sock.close()
    finally:
        sock.close()

    assert opened not in guard.listening_ports(), (
        "the declared listener must be gone once the test closed it"
    )


def test_a_listener_left_open_fails_the_test_that_opened_it(request):
    """宣告了卻沒收掉 —— `allowing()` 在離開時當場失敗，**port 0 也一樣**。

    這是「測試通過但留下背景 server」唯一真正抓得到的地方。round 9
    之前 `allowing(0)` 驗不到這一步（實際 port 要綁完才知道），這條
    測試因此得先記下號碼、再用第二次宣告去撞；現在離開時看的是
    socket 物件本身。
    """
    guard = _session_guard(request)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 這一次故意不收，離開時必須被抓到。
        with pytest.raises(AssertionError) as caught:
            with guard.LISTENERS.allowing(0):
                sock.bind(("127.0.0.1", 0))
                sock.listen(1)
                port = sock.getsockname()[1]
        assert type(caught.value).__name__ == "RealListenerOpened"
        assert str(port) in str(caught.value), "the failure must name the port"
    finally:
        sock.close()


def test_the_session_never_holds_the_official_ui_port():
    """測試永遠不得佔用 8790 —— 那是正式 UI 的位置。"""
    guard = _guard_module()
    listeners = guard.ListenerTripwire()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(guard.RealListenerOpened):
            listeners.check_bind(sock, ("127.0.0.1", guard.OFFICIAL_UI_PORT))
    finally:
        sock.close()
