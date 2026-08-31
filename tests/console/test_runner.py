# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；在 tmp_path 建立 run 目錄，以短指令取代真實
#         模擬驗證 pcmef.console.runner 的串流與紀錄；不跑 mitsuba、不連線。
# 檔案路徑: tests/console/test_runner.py
# 產生時間: 2026-08-27 17:50 +08:00
# 版本: v0.1.0
# 功能說明: 驗證執行器把輸出逐行落盤、把參數完整存下來，
#           以及任何試圖從 UI 啟動 formal 的參數都會被硬性拒絕。
# 模組定位: §208「formal run 一律無 UI」在執行層的可執行防線。
# 主要責任:
#   1. test_formal_is_refused 擋下所有帶 formal 的參數
#   2. test_output_is_streamed_line_by_line 驗證邊跑邊看得到
#   3. test_tail_returns_only_new_bytes 驗證增量推送
#   4. test_config_records_the_exact_parameters 驗證可回溯
#   5. test_preset_and_overrides 驗證懶人包與進階參數的優先序
# 維護提醒:
#   - 不得把 test_formal_is_refused 改成只檢查某一個參數名；
#     它掃的是所有含 formal 的鍵，因為繞過的方式通常是換個名字。
#   - v0.1.0 新增：首版，對應 NOTE-025。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_runner.py -v
# ------------------------------------------------------------

from __future__ import annotations

import sys
import time

import pytest
import yaml

from pcmef.console.runner import (
    PRESETS,
    ConsoleRunner,
    FormalRunRefused,
    RunnerError,
    RunSpec,
)


@pytest.fixture
def runner(tmp_path) -> ConsoleRunner:
    return ConsoleRunner(tmp_path / "runs")


class _EchoRunner(ConsoleRunner):
    """用一個會逐行輸出的短 Python 程式取代真實模擬。"""

    def _command(self, run_id, spec):
        lines = int(spec.params.get("lines", 3))
        delay = float(spec.params.get("delay", 0.05))
        return [
            sys.executable, "-u", "-c",
            f"import time,sys\n"
            f"for i in range({lines}):\n"
            f"    print('line', i, flush=True)\n"
            f"    time.sleep({delay})\n"
            f"sys.exit({int(spec.params.get('exit_code', 0))})",
        ]


@pytest.fixture
def echo(tmp_path) -> _EchoRunner:
    return _EchoRunner(tmp_path / "runs")


# ---------------------------------------------------------------------------
# formal 邊界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"formal": True},
        {"is_formal": 1},
        {"run_formal_e2": "yes"},
        {"FORMAL": True},
    ],
)
def test_formal_is_refused(runner, params):
    """§208：formal run 一律無 UI、走 CLI。"""
    with pytest.raises(FormalRunRefused, match="CLI-only"):
        runner.start(RunSpec(kind="sim_smoke", params=params))


def test_a_falsy_formal_flag_is_allowed(echo):
    """formal=False 不是嘗試啟動 formal，不該被擋。"""
    record = echo.start(RunSpec(kind="sim_smoke", params={"formal": False, "lines": 1}))
    assert echo.wait(record.run_id, timeout=30).status == "succeeded"


def test_an_unknown_kind_is_refused():
    with pytest.raises(RunnerError, match="unknown run kind"):
        RunSpec(kind="experiment_e2", params={})


def test_no_generated_command_contains_formal(runner):
    record_params = {"preset": "preview"}
    command = runner._command("probe-run", RunSpec(kind="sim_smoke", params=record_params))
    assert "--formal" not in command


# ---------------------------------------------------------------------------
# 串流
# ---------------------------------------------------------------------------


def test_output_is_streamed_line_by_line(echo):
    """跑五分鐘卻沒有任何輸出的畫面，與當掉沒有分別。"""
    record = echo.start(RunSpec(kind="sim_smoke", params={"lines": 6, "delay": 0.15}))

    seen, offset, saw_partial = [], 0, False
    for _ in range(100):
        chunk, offset = echo.tail(record.run_id, offset)
        if chunk:
            seen.extend(chunk.splitlines())
            # 關鍵：在還沒結束之前就已經看得到部分輸出。
            if not echo.get(record.run_id).finished and len(seen) < 6:
                saw_partial = True
        if echo.get(record.run_id).finished and len(seen) >= 6:
            break
        time.sleep(0.05)

    assert saw_partial, "output only appeared after the run finished"
    assert len(seen) == 6


def test_tail_returns_only_new_bytes(echo):
    record = echo.start(RunSpec(kind="sim_smoke", params={"lines": 3, "delay": 0.01}))
    echo.wait(record.run_id, timeout=30)

    first, offset = echo.tail(record.run_id, 0)
    assert "line 0" in first
    second, new_offset = echo.tail(record.run_id, offset)
    assert second == ""
    assert new_offset == offset


def test_a_failing_command_is_recorded_as_failed(echo):
    record = echo.start(
        RunSpec(kind="sim_smoke", params={"lines": 1, "exit_code": 3})
    )
    final = echo.wait(record.run_id, timeout=30)
    assert final.status == "failed"
    assert final.exit_code == 3


def test_a_command_that_cannot_start_is_recorded(tmp_path):
    class _Broken(ConsoleRunner):
        def _command(self, run_id, spec):
            return ["definitely-not-an-executable-xyz"]

    runner = _Broken(tmp_path / "runs")
    record = runner.start(RunSpec(kind="sim_smoke", params={}))
    final = runner.wait(record.run_id, timeout=30)
    assert final.status == "failed"
    assert "failed to start" in runner.tail(record.run_id)[0]


# ---------------------------------------------------------------------------
# 參數與可回溯性
# ---------------------------------------------------------------------------


def test_config_records_the_exact_parameters(runner):
    path = runner.build_config(
        "abc", {"preset": "preview", "spp": 33, "classes": ["Empty"], "seed_offset": 7}
    )
    config = yaml.safe_load(path.read_text(encoding="utf-8"))["simulation"]

    assert config["spp"] == 33                       # 進階參數覆蓋 preset
    assert config["resolution"] == [32, 32]          # 未覆蓋的沿用 preset
    assert config["temporal_bins"] == 64
    assert len(config["scenarios"]) == 1
    assert config["scenarios"][0]["class_label"] == "Empty"
    assert config["scenarios"][0]["seed"] == 1001 + 7


def test_every_preset_produces_a_valid_config(runner):
    for name in PRESETS:
        config = yaml.safe_load(
            runner.build_config(f"r-{name}", {"preset": name}).read_text(encoding="utf-8")
        )["simulation"]
        assert len(config["scenarios"]) == 4
        assert config["resolution"][0] == PRESETS[name]["resolution"]


def test_placeholder_media_survive_into_the_config(runner):
    """介質參數仍是 placeholder —— formal 模式必須照樣拒絕載入。"""
    config = yaml.safe_load(
        runner.build_config("r", {"preset": "preview"}).read_text(encoding="utf-8")
    )["simulation"]
    water = next(s for s in config["scenarios"] if s["class_label"] == "Water-filled")
    assert water["medium"]["turbidity"]["placeholder"] is True


def test_invalid_parameters_are_refused(runner):
    with pytest.raises(RunnerError, match="unknown class"):
        runner.build_config("r", {"classes": ["Plasma"]})
    with pytest.raises(RunnerError, match="at least one class"):
        runner.build_config("r", {"classes": []})
    with pytest.raises(RunnerError, match="spp must be positive"):
        runner.build_config("r", {"spp": 0})


def test_the_record_keeps_the_full_command_and_params(echo):
    record = echo.start(RunSpec(kind="sim_smoke", params={"lines": 1, "preset": "preview"}))
    echo.wait(record.run_id, timeout=30)
    stored = echo.get(record.run_id)
    assert stored.command == record.command
    assert stored.params["preset"] == "preview"
    assert stored.started_at and stored.finished_at


def test_runs_are_listed_newest_first(echo):
    ids = []
    for _ in range(3):
        rec = echo.start(RunSpec(kind="sim_smoke", params={"lines": 1}))
        echo.wait(rec.run_id, timeout=30)
        ids.append(rec.run_id)
        time.sleep(1.05)          # run_id 以秒為粒度
    listed = [r.run_id for r in echo.list_runs()]
    assert listed[: len(ids)] == list(reversed(ids))


def test_getting_an_unknown_run_fails_loudly(runner):
    with pytest.raises(RunnerError, match="not found"):
        runner.get("no-such-run")


# ---------------------------------------------------------------------------
# llm snapshot：console 觸發，但准駁權在 CLI
# ---------------------------------------------------------------------------


def test_the_snapshot_command_calls_the_real_cli(runner):
    """UI 不自己算 lock，它啟動的是與終端機上同一支指令。"""
    command = runner._command("r1", RunSpec(kind="llm_snapshot", params={}))
    assert command[:4] == [runner._python, "-u", "-m", "pcmef.cli"]
    assert command[4:6] == ["llm", "snapshot"]


def test_a_plain_snapshot_never_carries_freeze(runner):
    """沒有 --freeze 就不會寫 lock —— 這是「先檢查」那顆按鈕的全部意義。"""
    command = runner._command("r1", RunSpec(kind="llm_snapshot", params={}))
    assert "--freeze" not in command


def test_the_snapshot_out_path_is_a_directory_not_a_file(runner):
    """`snapshot.write()` 自己決定檔名（runtime_snapshot_<hash>.json），
    所以 --out 收的是目錄。給它一個 .json 結尾的路徑，會建出一個叫那個
    名字的**資料夾**，而畫面上只會看到一條長得像檔案的路徑。"""
    command = runner._command("r1", RunSpec(kind="llm_snapshot", params={}))
    out = command[command.index("--out") + 1]
    assert not out.endswith(".json")
    assert out.endswith("artifacts")


def test_freeze_is_passed_through_only_when_asked(runner):
    command = runner._command(
        "r1", RunSpec(kind="llm_snapshot", params={"freeze": True})
    )
    assert "--freeze" in command


def test_the_console_still_refuses_to_run_a_formal_experiment(runner):
    """界線劃細了，但沒有被放寬：觸發 freeze 可以，跑 formal experiment 不行。"""
    with pytest.raises(FormalRunRefused):
        runner.start(RunSpec(kind="llm_snapshot", params={"formal": True}))


# ---------------------------------------------------------------------------
# 搜尋與刪除
# ---------------------------------------------------------------------------


def test_search_matches_the_run_parameters_too(echo):
    """實務上要找的往往是「那次 spp 開到 64 的」，而那個數字只在 params 裡。"""
    wanted = echo.start(RunSpec(kind="sim_smoke", params={"lines": 1, "spp": 64}))
    other = echo.start(RunSpec(kind="sim_smoke", params={"lines": 1, "spp": 8}))
    for record in (wanted, other):
        echo.wait(record.run_id, timeout=30)

    found = [r.run_id for r in echo.list_runs(query="spp=64")]
    assert found == [wanted.run_id]


def test_search_is_case_insensitive_and_ignores_padding(echo):
    record = echo.start(RunSpec(kind="sim_smoke", params={"lines": 1}))
    echo.wait(record.run_id, timeout=30)
    assert echo.list_runs(query="  SIM_SMOKE  ")


def test_an_empty_query_returns_everything(echo):
    record = echo.start(RunSpec(kind="sim_smoke", params={"lines": 1}))
    echo.wait(record.run_id, timeout=30)
    assert len(echo.list_runs(query="")) == len(echo.list_runs())


def test_deleting_a_run_removes_its_whole_directory(echo):
    record = echo.start(RunSpec(kind="sim_smoke", params={"lines": 1}))
    echo.wait(record.run_id, timeout=30)
    assert echo.run_dir(record.run_id).exists()

    echo.delete(record.run_id)

    assert not echo.run_dir(record.run_id).exists()
    with pytest.raises(RunnerError, match="not found"):
        echo.get(record.run_id)


def test_a_running_run_is_not_deletable(echo):
    record = echo.start(RunSpec(kind="sim_smoke", params={"lines": 200, "delay": 0.02}))
    with pytest.raises(RunnerError, match="still running"):
        echo.delete(record.run_id)
    echo.wait(record.run_id, timeout=30)
