# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.console.runner 產生的 formal 指令、
#         pcmef.experiments.formal_service 的 one-shot 判定，以及
#         Web 與 CLI 兩條路徑落在**同一個** canonical 輸出位置。
#         **不啟動任何子行程**：只檢查指令與檔案系統狀態。
# 檔案路徑: tests/console/test_formal_one_shot.py
# 產生時間: 2026-09-04 21:35 +08:00
# 版本: v0.1.0
# 功能說明: Formal E2 的一次性必須由 production path 保證，而不是由
#           「大家記得只跑一次」保證。
# 模組定位: P0-1 的可執行防線。先前 Web 把 --out 指到每次 run 各自的
#           artifacts 目錄，於是 pre-flight 檢查的位置永遠是空的 ——
#           阻擋看起來存在，實際上永遠不會觸發（NOTE-075）。
# 主要責任:
#   1. test_web_and_cli_write_to_the_same_canonical_location
#   2. test_second_formal_run_is_blocked_after_the_first 第二次 exit 2
#   3. test_dry_run_may_overwrite_its_own_report 預演不受一次性拘束
#   4. test_console_run_directory_holds_no_scientific_result
#   5. test_a_formal_execution_record_cannot_be_deleted
#   6. test_a_dry_run_record_can_be_deleted 只有 UI wrapper 可刪
#   7. test_formal_status_answers_the_four_questions
# 維護提醒:
#   - 不得把 --out 改回 run 目錄。那正是這一組測試存在的原因：
#     每次 run 一個新目錄 = one-shot 永遠不會觸發。
#   - 不得讓 ConsoleRunner 的 formal_out 與 formal_service.preflight()
#     檢查的 out 來自不同設定。兩者一旦分岔，畫面會顯示 PASS 而檔案
#     其實已經在別處存在。
#   - v0.1.0 新增：首版，對應 P0-1 / NOTE-075。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_formal_one_shot.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.console.runner import (
    DEFAULT_FORMAL_OUT, ConsoleRunner, RunnerError, RunRecord, RunSpec,
)
from pcmef.experiments.e2_formal import run_artifact_root
from pcmef.experiments.formal_service import (
    FORMAL_REPORT, formal_status, preflight,
)
from pcmef.experiments.run_claim import (
    STATE_COMPLETE, STATE_INTERRUPTED, RunIdentity, mark_complete,
    mark_interrupted, reserve,
)

IDENTITY = RunIdentity(
    freeze_dir="freeze/runs/PFC-001", lock_hashes={"gate": "a" * 64},
    base_manifest_hash="b" * 64, code_version="c" * 40,
)


def _command(runner: ConsoleRunner, mode: str) -> list[str]:
    """取得這個 mode 會送出的指令。不執行它。"""
    return runner._command("run-under-test", RunSpec(kind="formal_e2",
                                                     params={"mode": mode}))


@pytest.fixture()
def runner(tmp_path):
    return ConsoleRunner(
        tmp_path / "runs", formal_out=tmp_path / "e2_final",
        dry_run_base=tmp_path / "gate_validation",
    )


# ---------------------------------------------------------------------------
# Web 與 CLI 寫到同一個位置
# ---------------------------------------------------------------------------


def test_web_and_cli_write_to_the_same_canonical_location(runner, tmp_path):
    """`--out` 必須是 canonical 目錄，不是這次 run 的 artifacts 目錄。"""
    command = _command(runner, "formal")
    assert "--out" in command
    out = command[command.index("--out") + 1]
    assert str(tmp_path / "e2_final") == out
    # 反過來確認它**不是** run 目錄底下的任何東西。
    assert "runs" not in out


def test_the_default_canonical_out_matches_the_cli_default():
    """runner 的預設與 `pcmef formal run-e2 --out` 的預設必須是同一個。

    兩份預設一旦分岔，用 CLI 跑過一次之後從 Web 再跑，pre-flight 會看向
    一個空目錄並放行。
    """
    from pcmef.cli import build_parser

    args = build_parser().parse_args(["formal", "run-e2"])
    assert args.out == DEFAULT_FORMAL_OUT


def test_preflight_guards_the_root_the_runner_writes_to(runner, tmp_path):
    """pre-flight 守的 formal root 必須等於 runner 實際寫入的那一個。"""
    command = _command(runner, "formal")
    out = command[command.index("--out") + 1]
    report = preflight(mode="formal", out=out, lineage_root="freeze")
    assert report["formal_root"] == run_artifact_root(
        tmp_path / "e2_final", dry_run=False
    ).as_posix()


# ---------------------------------------------------------------------------
# 一次性
# ---------------------------------------------------------------------------


def test_second_formal_run_is_blocked_after_the_first(runner, tmp_path):
    """第一次成功之後，第二次必須 fail-closed。

    「成功」以 **claim = COMPLETE** 為準，不以報告檔存在為準：一次跑到一半
    失敗的正式執行沒有報告，但 final partition 已經開封（NOTE-079）。
    """
    out = tmp_path / "e2_final"
    out.mkdir(parents=True, exist_ok=True)
    formal_root = run_artifact_root(out, dry_run=False)

    first = preflight(mode="formal", out=out, lineage_root="freeze")
    assert "formal_run_claim_is_available" not in " ".join(first["blockers"])

    claim = reserve(formal_root, IDENTITY)
    mark_complete(formal_root, claim["claim_id"])

    second = preflight(mode="formal", out=out, lineage_root="freeze")
    assert second["allowed"] is False
    assert any("formal_run_claim_is_available" in b for b in second["blockers"])


def test_the_cli_exits_two_when_the_output_is_occupied(runner, tmp_path, capsys):
    """CLI 的退出碼必須是 2，與 Web 被擋下的是同一個判斷。"""
    from pcmef.cli import build_parser, cmd_formal_preflight

    out = tmp_path / "e2_final"
    out.mkdir(parents=True, exist_ok=True)
    (out / FORMAL_REPORT).write_text("{}", encoding="utf-8")

    args = build_parser().parse_args(
        ["formal", "preflight", "--mode", "formal", "--out", str(out)]
    )
    assert cmd_formal_preflight(args) == 2
    assert "BLOCKED" in capsys.readouterr().out


def test_a_completed_formal_run_does_not_block_a_dry_run(runner, tmp_path):
    """預演可以重跑。一次性只約束正式執行。"""
    out = tmp_path / "e2_final"
    out.mkdir(parents=True, exist_ok=True)
    formal_root = run_artifact_root(out, dry_run=False)
    claim = reserve(formal_root, IDENTITY)
    mark_complete(formal_root, claim["claim_id"])

    report = preflight(mode="dry-run", out=out, lineage_root="freeze")
    assert report["allowed"] is True
    assert not any("claim" in b for b in report["blockers"])


def test_each_dry_run_gets_its_own_root(runner, tmp_path):
    """兩次預演不得共用 trace 與 stress（NOTE-078）。"""
    out = tmp_path / "e2_final"
    first = run_artifact_root(out, dry_run=True, run_id="run-a")
    second = run_artifact_root(out, dry_run=True, run_id="run-b")
    assert first != second
    # 而且都不在 formal 的 root 底下。
    formal_root = run_artifact_root(out, dry_run=False)
    assert formal_root not in first.parents and first != formal_root
    assert formal_root not in second.parents and second != formal_root


def test_a_dry_run_without_a_run_id_is_refused(tmp_path):
    """沒有 run_id 就沒有專屬目錄，第二次預演會覆蓋第一次。"""
    from pcmef.experiments.e2_formal import FormalE2Error

    with pytest.raises(FormalE2Error, match="own run_id"):
        run_artifact_root(tmp_path, dry_run=True)


# ---------------------------------------------------------------------------
# run 目錄只放 UI 紀錄
# ---------------------------------------------------------------------------


def test_console_run_directory_holds_no_scientific_result(runner, tmp_path):
    """指令不得把**科學輸出**寫進 run 目錄；那裡只留指標與 UI 紀錄。

    逐一檢查會決定科學結果落點的旗標，而不是整條指令的字串比對：
    `--run-events` 也指向 run 目錄，但它寫的是 stage 事件 —— UI 用的
    觀測記錄，刪掉不會失去任何科學證據。把它一起擋掉，等於為了這條
    測試而放棄進度顯示；而放寬成「整條指令都不准提到 run 目錄」則會
    在下一次有人加旗標時再壞一次。
    """
    command = _command(runner, "formal")
    run_dir = str(runner.run_dir("run-under-test"))

    scientific_flags = ("--out", "--base", "--ds-dir", "--freeze-dir",
                        "--registry-dir", "--agent-cache")
    for flag in scientific_flags:
        if flag in command:
            value = command[command.index(flag) + 1]
            assert run_dir not in value, (
                f"{flag} points into the console run directory; scientific "
                "results must land in the canonical location"
            )

    # 對照：真的有一個旗標指向 run 目錄，而它是 UI 觀測記錄那一個。
    assert "--run-events" in command
    assert command[command.index("--run-events") + 1] == run_dir


def test_the_pointer_names_the_canonical_report(runner, tmp_path):
    runner._write_formal_pointer("r1", "formal")
    pointer = runner.formal_pointer("r1")
    assert pointer["one_shot"] is True
    assert pointer["report"].endswith(FORMAL_REPORT)
    assert pointer["canonical_out"] == (tmp_path / "e2_final").as_posix()


def test_the_dry_run_pointer_is_not_one_shot(runner):
    runner._write_formal_pointer("r2", "dry-run")
    pointer = runner.formal_pointer("r2")
    assert pointer["one_shot"] is False
    assert pointer["report"].endswith("formal_e2_dry_run.json")


def test_run_view_follows_the_pointer(runner, tmp_path):
    """畫面找報告時必須走指標，否則 formal run 的分頁會全部是空的。"""
    from pcmef.console import run_view

    runner.run_dir("r3").mkdir(parents=True, exist_ok=True)
    runner._write_formal_pointer("r3", "formal")
    assert run_view.artifact_root(runner.run_dir("r3")) == run_artifact_root(
        tmp_path / "e2_final", dry_run=False
    )

    # 沒有指標的一般 run 維持原本行為。
    runner.run_dir("r4").mkdir(parents=True, exist_ok=True)
    assert run_view.artifact_root(runner.run_dir("r4")) == runner.run_dir("r4") / "artifacts"


# ---------------------------------------------------------------------------
# 刪除保護
# ---------------------------------------------------------------------------


def _finished(runner, run_id, mode):
    record = RunRecord(
        run_id=run_id, kind="formal_e2", label="", params={"mode": mode},
        command=[], status="succeeded", exit_code=0,
        # 結束的紀錄一定有結束時間 —— runner 從來沒有寫出過沒有的（round 11）。
        finished_at="2026-01-01T00:01:00+00:00",
    )
    runner.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    runner._save(record)
    return record


def test_a_formal_execution_record_cannot_be_deleted(runner):
    """正式執行的逐行紀錄是 audit trail 的一半，且沒有第二份。"""
    _finished(runner, "formal-1", "formal")
    with pytest.raises(RunnerError, match="one-shot Formal E2"):
        runner.delete("formal-1")
    assert runner.run_dir("formal-1").exists()


def test_a_dry_run_record_can_be_deleted(runner):
    """預演可以重跑，紀錄也就可以丟 —— 刪的是 UI wrapper。"""
    _finished(runner, "dry-1", "dry-run")
    runner.delete("dry-1")
    assert not runner.run_dir("dry-1").exists()


def test_deleting_a_dry_run_record_leaves_the_canonical_report(runner, tmp_path):
    """關鍵性質：刪 run 紀錄不會動到 canonical 目錄裡的任何東西。"""
    out = tmp_path / "e2_final"
    out.mkdir(parents=True, exist_ok=True)
    (out / "formal_e2_dry_run.json").write_text("{}", encoding="utf-8")

    _finished(runner, "dry-2", "dry-run")
    runner._write_formal_pointer("dry-2", "dry-run")
    runner.delete("dry-2")

    assert (out / "formal_e2_dry_run.json").exists()


def test_a_failed_formal_record_is_also_protected(runner):
    """失敗的正式執行同樣消耗了一次性，紀錄同樣不可刪。"""
    record = RunRecord(
        run_id="formal-failed", kind="formal_e2", label="",
        params={"mode": "formal"}, command=[], status="failed", exit_code=2,
        # 結束的紀錄一定有結束時間。少了它，round 11 起這筆會被判為損壞 ——
        # 而損壞的拒絕訊息同樣含有 "one-shot Formal E2"，下面的 match 會因為
        # 錯的理由成立。
        finished_at="2026-01-01T00:01:00+00:00",
    )
    runner.run_dir("formal-failed").mkdir(parents=True, exist_ok=True)
    runner._save(record)
    with pytest.raises(RunnerError, match="one-shot Formal E2"):
        runner.delete("formal-failed")


# ---------------------------------------------------------------------------
# 畫面要說得出四件事
# ---------------------------------------------------------------------------


def test_formal_status_answers_the_four_questions(tmp_path):
    """是不是一次性、跑過沒有、報告在哪、為什麼被擋。"""
    out = tmp_path / "e2_final"
    out.mkdir(parents=True, exist_ok=True)

    before = formal_status(out)
    assert before["one_shot"] is True
    assert before["already_executed"] is False
    assert before["report_path"].endswith(FORMAL_REPORT)
    assert before["blocked_reason"] == ""

    (out / FORMAL_REPORT).write_text(
        json.dumps({
            "report_id": "formal_e2_full_pcmef",
            "created_at": "2026-09-05T01:00:00+00:00",
            "code_version": "abcdef0123456789",
            "scientific_result": True,
        }),
        encoding="utf-8",
    )

    after = formal_status(out)
    assert after["already_executed"] is True
    assert after["blocked_reason"]
    assert after["identity"]["report_id"] == "formal_e2_full_pcmef"
    assert after["identity"]["code_version"] == "abcdef012345"


def test_formal_status_distinguishes_records_from_the_report(tmp_path):
    """UI run record 與科學報告的差別必須寫在畫面資料裡，不是靠讀者推。"""
    status = formal_status(tmp_path / "nowhere")
    assert "執行紀錄" in status["record_vs_report"]
    assert "one-shot" in status["record_vs_report"]


def test_an_unreadable_report_is_not_reported_as_absent(tmp_path):
    """壞掉的報告不得被畫成「還沒跑過」—— 那會讓 UI 邀請你再跑一次。"""
    out = tmp_path / "e2_final"
    out.mkdir(parents=True, exist_ok=True)
    (out / FORMAL_REPORT).write_text("{ this is not json", encoding="utf-8")

    status = formal_status(out)
    assert status["already_executed"] is True
    assert "unreadable" in status["identity"]
