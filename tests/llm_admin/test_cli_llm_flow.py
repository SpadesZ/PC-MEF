# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；直接呼叫 pcmef.cli.main() 走完整條 LLM admin
#         流程，registry / vault / freeze / artifact 全部指向 tmp_path，
#         provider 使用離線 stub（PCMEF_STUB_MODELS）；不對外連線。
# 檔案路徑: tests/llm_admin/test_cli_llm_flow.py
# 產生時間: 2026-08-27 03:10 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 §52 步驟 5 要求的「CLI 先走完全流程」確實成立：
#           建連線、抓模型、驗能力、改綁、產快照五步都能以指令完成，
#           而且能力不足時擋得下來、前提未齊時凍不進去。
# 模組定位: §32 CLI 契約中 LLM Admin 段的端到端驗證。
#           它「不是」單元測試 —— 走的是使用者真正會打的那條路徑。
# 主要責任:
#   1. test_the_whole_flow_runs_from_the_command_line 五步接續成功
#   2. test_binding_is_refused_before_verification 驗證能力檢查在 CLI 也生效
#   3. test_snapshot_reports_blocking_and_exits_zero 驗證未齊備時的回報語意
#   4. test_snapshot_freeze_exits_two_when_blocked 驗證 --freeze 的拒絕碼
#   5. test_connection_add_takes_no_plaintext_key_option 驗證 CLI 不收明文 key
#   6. test_cli_errors_do_not_leak_a_traceback 驗證錯誤走 exit code 而非例外
# 維護提醒:
#   - 不得在此改用 monkeypatch 直接替換 AdminService；本檔的價值就在於
#     走的是 main() 的真實組裝路徑。
#   - v0.1.0 新增：首版，對應 NOTE-020。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_cli_llm_flow.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.cli import build_parser, main

STUB_KEY = "sk-" + "CliFlowStubKeyMat" * 2
STUB_MODELS = "stub-omni:chat+vision+structured_json,stub-chat:chat"


@pytest.fixture
def store_args(tmp_path) -> list[str]:
    return [
        "--registry-db", str(tmp_path / "llm.db"),
        "--vault", str(tmp_path / "vault.json"),
        "--freeze-dir", str(tmp_path / "freeze"),
        "--artifact-root", str(tmp_path / "artifacts"),
    ]


@pytest.fixture(autouse=True)
def offline_provider(monkeypatch):
    monkeypatch.setenv("PCMEF_TEST_KEY", STUB_KEY)
    monkeypatch.setenv("PCMEF_STUB_MODELS", STUB_MODELS)


def _connection_id(capsys, store_args) -> str:
    """先清空累積的輸出，否則讀到的第一行是上一個指令留下的。"""
    capsys.readouterr()
    assert main(["llm", "connection", "list", *store_args]) == 0
    return capsys.readouterr().out.splitlines()[0].split()[0]


# ---------------------------------------------------------------------------
# 全流程
# ---------------------------------------------------------------------------


def test_the_whole_flow_runs_from_the_command_line(capsys, store_args, tmp_path):
    """§52 步驟 5：先讓 CLI 完成全流程，再接 UI 的 Bind。"""
    assert main([
        "llm", "connection", "add", "--provider", "stub_offline",
        "--name", "Stub Formal", "--secret-ref", "env:PCMEF_TEST_KEY", *store_args,
    ]) == 0
    created = capsys.readouterr().out
    assert "****" in created
    assert STUB_KEY not in created

    connection_id = _connection_id(capsys, store_args)

    assert main([
        "llm", "connection", "fetch-models", "--connection", connection_id, *store_args,
    ]) == 0
    assert "stub-omni" in capsys.readouterr().out

    # LAVA 流程：選模型 → Test → Connect(lock)。缺任何一步都綁不上去。
    assert main([
        "llm", "connection", "select-model", "--connection", connection_id,
        "--model", "stub-omni", *store_args,
    ]) == 0
    assert "已選定 stub-omni" in capsys.readouterr().out

    assert main([
        "llm", "connection", "test", "--connection", connection_id, *store_args,
    ]) == 0
    tested = capsys.readouterr().out
    assert "lifecycle: connected" in tested

    assert main([
        "llm", "connection", "lock", "--connection", connection_id, *store_args,
    ]) == 0
    assert "locked" in capsys.readouterr().out

    for task_code in (
        "observation_agent", "physics_agent",
        "visual_semantic_agent", "arbitration_agent",
    ):
        assert main([
            "llm", "binding", "set", task_code, "--connection", connection_id,
            "--model", "stub-omni", *store_args,
        ]) == 0
    capsys.readouterr()

    assert main([
        "llm", "binding", "list", *store_args,
    ]) == 0
    listed = capsys.readouterr().out
    assert listed.count("draft") == 4

    assert main(["llm", "snapshot", "--out", str(tmp_path / "out"), *store_args]) == 0
    snapshot_out = capsys.readouterr()
    assert "bound tasks    : 4/4" in snapshot_out.out

    artifacts = list((tmp_path / "out").glob("runtime_snapshot_*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["freezable"] is False
    assert len(payload["bindings"]) == 4


def test_binding_is_refused_before_verification(capsys, store_args):
    main([
        "llm", "connection", "add", "--provider", "stub_offline", "--name", "S",
        "--secret-ref", "env:PCMEF_TEST_KEY", *store_args,
    ])
    connection_id = _connection_id(capsys, store_args)
    main(["llm", "connection", "fetch-models", "--connection", connection_id, *store_args])
    capsys.readouterr()

    assert main([
        "llm", "binding", "set", "physics_agent", "--connection", connection_id,
        "--model", "stub-omni", *store_args,
    ]) == 1
    # 未走完 LAVA 流程時，第一個該講的是「還沒鎖定」——那才是下一步。
    assert "not locked" in capsys.readouterr().err


def test_binding_audit_records_every_change(capsys, store_args):
    main([
        "llm", "connection", "add", "--provider", "stub_offline", "--name", "S",
        "--secret-ref", "env:PCMEF_TEST_KEY", *store_args,
    ])
    connection_id = _connection_id(capsys, store_args)
    main(["llm", "connection", "fetch-models", "--connection", connection_id, *store_args])
    main([
        "llm", "connection", "select-model", "--connection", connection_id,
        "--model", "stub-omni", *store_args,
    ])
    main(["llm", "connection", "test", "--connection", connection_id, *store_args])
    main(["llm", "connection", "lock", "--connection", connection_id, *store_args])
    main([
        "llm", "binding", "set", "physics_agent", "--connection", connection_id,
        "--model", "stub-omni", "--actor", "tester", "--reason", "initial", *store_args,
    ])
    capsys.readouterr()

    assert main(["llm", "binding", "audit", *store_args]) == 0
    audit = capsys.readouterr().out
    assert "physics_agent" in audit
    assert "actor=tester" in audit
    assert "reason=initial" in audit


# ---------------------------------------------------------------------------
# 快照的回報語意
# ---------------------------------------------------------------------------


def test_snapshot_reports_blocking_and_exits_zero(capsys, store_args, tmp_path):
    """只算候選不凍結時，缺前提是正常結果而非失敗 —— 這就是它要回報的事。"""
    assert main(["llm", "snapshot", "--out", str(tmp_path / "out"), *store_args]) == 0
    captured = capsys.readouterr()
    assert "NOT freezable" in captured.err
    assert "candidate hash" in captured.out


def test_snapshot_freeze_exits_two_when_blocked(capsys, store_args, tmp_path):
    """--freeze 遇到未決的教授裁決必須以 formal-blocking 的 exit code 中斷。"""
    assert main([
        "llm", "snapshot", "--out", str(tmp_path / "out"), "--freeze", *store_args,
    ]) == 2
    captured = capsys.readouterr()
    assert "refusing to freeze" in captured.err
    assert "agents.representation_mode" in captured.err
    assert not (tmp_path / "freeze" / "llm_runtime.lock.json").exists()


# ---------------------------------------------------------------------------
# CLI 的 secret 邊界
# ---------------------------------------------------------------------------


def test_connection_add_takes_no_plaintext_key_option():
    """命令列參數會留在 shell history 與 process list，因此只收參考。"""
    parser = build_parser()
    action_names = {
        option
        for action in parser._subparsers._group_actions[0]
        .choices["llm"]
        ._subparsers._group_actions[0]
        .choices["connection"]
        ._subparsers._group_actions[0]
        .choices["add"]
        ._actions
        for option in action.option_strings
    }
    assert "--secret-ref" in action_names
    assert "--api-key" not in action_names
    assert "--key" not in action_names


def test_a_plaintext_key_as_secret_ref_is_refused(capsys, store_args):
    assert main([
        "llm", "connection", "add", "--provider", "google", "--name", "Bad",
        "--secret-ref", STUB_KEY, *store_args,
    ]) == 1
    assert "plaintext API key" in capsys.readouterr().err


def test_cli_errors_do_not_leak_a_traceback(capsys, store_args):
    """錯誤要走 exit code 與一行訊息；traceback 對 CI 與批次腳本沒有意義。"""
    assert main([
        "llm", "connection", "fetch-models", "--connection", "no-such-id", *store_args,
    ]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "Traceback" not in err
