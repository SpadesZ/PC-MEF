# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.console.runner 是否把伺服器端設定的
#         agent cache 接到 formal 子行程，以及 pcmef.admin.app 從環境變數
#         讀出它。**不啟動任何子行程。**
# 檔案路徑: tests/console/test_formal_agent_cache.py
# 產生時間: 2026-09-04 22:45 +08:00
# 版本: v0.1.0
# 功能說明: Web 啟動的 formal run 必須使用與 CLI 同一個正式 agent cache，
#           而且 cache 位置只能由伺服器端設定，不能從畫面來。
# 模組定位: P1-3 的可執行防線。CLI 早有 --agent-cache，Web 卻沒有接 ——
#           於是從畫面啟動的正式執行會對每一筆 escalated case 重新付費，
#           而畫面上沒有任何跡象顯示這件事（NOTE-076）。
# 主要責任:
#   1. test_formal_command_carries_the_configured_cache
#   2. test_no_cache_configured_means_no_flag 預設行為逐 byte 不變
#   3. test_dry_run_does_not_take_a_cache dry run 本來就不呼叫 provider
#   4. test_the_cache_root_is_not_in_the_form_whitelist UI 不得指定
#   5. test_the_app_reads_the_cache_root_from_the_environment
# 維護提醒:
#   - 不得把 cache root 加進 FORMAL_PARAM_WHITELIST。那會讓畫面決定
#     「要不要重新付費問模型」，也就是第二條科研設定通道。
#   - 不得在 app 端補一個預設 cache 路徑。沒設就是不使用快取，與接線前
#     逐 byte 相同；替使用者決定會讓一次「乾淨重跑」變成讀舊答案。
#   - v0.1.0 新增：首版，對應 P1-3 / NOTE-076。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_formal_agent_cache.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

import pytest

from pcmef.console.runner import (
    FORMAL_PARAM_WHITELIST, ConsoleRunner, FormalRunRefused, RunSpec,
)


def _command(runner: ConsoleRunner, mode: str) -> list[str]:
    return runner._command("r", RunSpec(kind="formal_e2", params={"mode": mode}))


def test_formal_command_carries_the_configured_cache(tmp_path):
    runner = ConsoleRunner(
        tmp_path / "runs", formal_out=tmp_path / "out",
        agent_cache_root=tmp_path / "agents",
    )
    command = _command(runner, "formal")
    assert "--agent-cache" in command
    assert command[command.index("--agent-cache") + 1] == str(tmp_path / "agents")


def test_no_cache_configured_means_no_flag(tmp_path):
    """沒設定就完全不使用快取。這一條保證接線前後的預設行為相同。"""
    runner = ConsoleRunner(tmp_path / "runs", formal_out=tmp_path / "out")
    assert "--agent-cache" not in _command(runner, "formal")


def test_an_empty_string_is_treated_as_unset(tmp_path):
    """空字串來自「環境變數存在但沒填」。它不是一個合法的 cache 位置。"""
    runner = ConsoleRunner(
        tmp_path / "runs", formal_out=tmp_path / "out", agent_cache_root="",
    )
    assert runner.agent_cache_root is None
    assert "--agent-cache" not in _command(runner, "formal")


def test_dry_run_does_not_take_a_cache(tmp_path):
    """dry run 一個 provider 呼叫都沒有，快取對它毫無意義。"""
    runner = ConsoleRunner(
        tmp_path / "runs", formal_out=tmp_path / "out",
        agent_cache_root=tmp_path / "agents",
    )
    assert "--agent-cache" not in _command(runner, "dry-run")


def test_the_cache_root_is_not_in_the_form_whitelist(tmp_path):
    """畫面不得指定 cache 位置。"""
    runner = ConsoleRunner(tmp_path / "runs")
    assert "agent_cache" not in FORMAL_PARAM_WHITELIST
    with pytest.raises(FormalRunRefused):
        runner._assert_not_formal(
            {"mode": "formal", "agent_cache": "/tmp/anywhere"}, "formal_e2"
        )


def test_the_app_reads_the_cache_root_from_the_environment(tmp_path):
    """伺服器端設定是唯一的來源，且 runner 與畫面看到的是同一個值。"""
    pytest.importorskip("flask")
    from pcmef.admin.app import create_app

    app = create_app(environ={"PCMEF_FORMAL_AGENT_CACHE": "artifacts/agents"},
        workspace_root=tmp_path / "workspace",
    )
    assert app.config["PCMEF_FORMAL_AGENT_CACHE"] == "artifacts/agents"
    runner = app.config["PCMEF_CONSOLE_RUNNER"]
    # 比 Path 而不是比字串：Windows 的分隔符是 `\`，比字串會在這裡失敗，
    # 而失敗的是測試的寫法，不是接線。
    assert runner.agent_cache_root == Path("artifacts/agents")


def test_the_app_defaults_to_no_cache(tmp_path):
    pytest.importorskip("flask")
    from pcmef.admin.app import create_app

    app = create_app(environ={},
        workspace_root=tmp_path / "workspace",
    )
    assert app.config["PCMEF_FORMAL_AGENT_CACHE"] is None
    assert app.config["PCMEF_CONSOLE_RUNNER"].agent_cache_root is None


def test_the_runner_and_the_preflight_share_one_out(tmp_path):
    """runner 寫入的位置與畫面 pre-flight 檢查的位置必須是同一個設定值。"""
    pytest.importorskip("flask")
    from pcmef.admin.app import create_app

    app = create_app(environ={"PCMEF_FORMAL_OUT": str(tmp_path / "canonical")},
        workspace_root=tmp_path / "workspace",
    )
    assert app.config["PCMEF_FORMAL_OUT"] == str(tmp_path / "canonical")
    runner = app.config["PCMEF_CONSOLE_RUNNER"]
    assert runner.formal_out == tmp_path / "canonical"
