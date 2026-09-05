# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 `--resume` 是否真的到得了 executor ——
#         pcmef.experiments.formal_service.preflight() 的 resume 語意、
#         CLI 的旗標傳遞，以及 e2_formal 的 claim lifecycle 結構。
#         **不執行任何 case、不呼叫 provider、不觸碰 families 36-43。**
# 檔案路徑: tests/e2/test_resume_path.py
# 產生時間: 2026-09-05 20:50 +08:00
# 版本: v0.1.0
# 功能說明: `--resume` 先前是一條到不了的路。
# 模組定位: NOTE-086 的可執行防線。CLI 先跑 pre-flight，而 pre-flight 對
#           INTERRUPTED 一律 FAIL —— 於是 `reserve(resume=True)` 沒有任何
#           呼叫得到它的機會。旗標存在、文件寫了、程式碼在那裡，
#           而那條路徑永遠不會被執行。
# 主要責任:
#   1. test_preflight_blocks_a_fresh_restart_of_an_interrupted_run
#   2. test_preflight_lets_a_resume_through
#   3. test_the_cli_passes_resume_into_preflight
#   4. test_the_lifecycle_is_reserve_running_try_except_else
#   5. test_a_failure_after_the_agents_still_leaves_a_resumable_claim
# 維護提醒:
#   - 不得讓 pre-flight 自己驗 identity。那是 claim layer 的事；兩份實作
#     遲早漂移，而 pre-flight 那一份會是比較寬鬆的（SAI §3.1）。
#   - 不得把 claim 的 except 縮回「幾個記得加 try 的地方」。涵蓋範圍必須是
#     整個流程，否則統計或報告階段的失敗會讓 claim 卡在 RUNNING。
#   - v0.1.0 新增：首版，對應 P0-1 / P0-3。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_resume_path.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect

import pytest

from pcmef.experiments.e2_formal import run_artifact_root
from pcmef.experiments.formal_service import preflight
from pcmef.experiments.run_claim import (
    RunIdentity, mark_complete, mark_interrupted, mark_running, reserve,
)

IDENTITY = RunIdentity(
    freeze_dir="freeze/runs/PFC-001", lock_hashes={"gate": "a" * 64},
    base_manifest_hash="b" * 64, code_version="c" * 40,
)


def _interrupted(out):
    root = run_artifact_root(out, dry_run=False)
    claim = reserve(root, IDENTITY)
    mark_running(root, claim["claim_id"])
    mark_interrupted(root, claim["claim_id"], "provider timeout")
    return root


def _check(report, name):
    return next((c for c in report["checks"] if c["check"] == name), None)


# ---------------------------------------------------------------------------
# pre-flight 必須認得 resume
# ---------------------------------------------------------------------------


def test_preflight_blocks_a_fresh_restart_of_an_interrupted_run(tmp_path):
    """已開封的 run 不得重新開始。"""
    _interrupted(tmp_path)
    report = preflight(mode="formal", out=tmp_path, lineage_root="freeze")
    check = _check(report, "formal_run_claim_is_available")
    assert check is not None and check["passed"] is False
    assert "--resume" in check["detail"]
    assert report["allowed"] is False


def test_preflight_lets_a_resume_through(tmp_path):
    """`--resume` 必須真的到得了 executor。

    先前 pre-flight 對 INTERRUPTED 一律 FAIL，CLI 又在 pre-flight 之後才
    呼叫 executor —— 於是 `reserve(resume=True)` 是一段永遠不會被執行的
    程式碼（NOTE-086）。
    """
    _interrupted(tmp_path)
    report = preflight(
        mode="formal", out=tmp_path, lineage_root="freeze", resume=True
    )
    check = _check(report, "formal_run_claim_is_resumable")
    assert check is not None, "resume 模式要有自己具名的檢查項"
    assert check["passed"] is True
    # claim 那一項不再是 blocker（其餘八項前置條件仍然會擋，那是另一回事）。
    assert not any("claim" in b for b in report["blockers"])
    assert report["resume"] is True


def test_resume_does_not_open_a_completed_run(tmp_path):
    """COMPLETE 永久封閉，`--resume` 也打不開。"""
    root = run_artifact_root(tmp_path, dry_run=False)
    claim = reserve(root, IDENTITY)
    mark_complete(root, claim["claim_id"])

    for resume in (False, True):
        report = preflight(
            mode="formal", out=tmp_path, lineage_root="freeze", resume=resume
        )
        assert report["allowed"] is False
        assert any("claim" in b for b in report["blockers"])


def test_resume_on_a_fresh_project_is_harmless(tmp_path):
    """沒有 claim 時 `--resume` 不得把第一次執行擋掉。"""
    report = preflight(
        mode="formal", out=tmp_path, lineage_root="freeze", resume=True
    )
    check = _check(report, "formal_run_claim_is_resumable")
    assert check["passed"] is True
    assert "沒有可接續" in check["detail"]


def test_a_running_claim_is_not_resumable(tmp_path):
    """RUNNING 代表另一個請求持有它。resume 不是搶奪的手段。"""
    root = run_artifact_root(tmp_path, dry_run=False)
    claim = reserve(root, IDENTITY)
    mark_running(root, claim["claim_id"])

    report = preflight(
        mode="formal", out=tmp_path, lineage_root="freeze", resume=True
    )
    check = _check(report, "formal_run_claim_is_resumable")
    assert check["passed"] is False
    assert "reclaim" in check["detail"]


def test_preflight_does_not_re_verify_the_identity():
    """identity 由 claim layer fail-closed 驗證，pre-flight 不重複那一段。

    兩份實作遲早漂移，而漂移時 pre-flight 那一份會是比較寬鬆的
    —— 它先跑，於是它放行的東西才會走到 claim layer。
    """
    from pcmef.experiments import formal_service

    source = inspect.getsource(formal_service.preflight)
    assert "identity" not in source.split("formal_run_claim")[1][:900], (
        "pre-flight 不得自己比對 identity digest"
    )


# ---------------------------------------------------------------------------
# CLI 的傳遞
# ---------------------------------------------------------------------------


def test_the_cli_passes_resume_into_preflight():
    from pcmef import cli

    source = inspect.getsource(cli._formal_preflight)
    assert 'resume=bool(getattr(args, "resume", False))' in source


def test_both_formal_subcommands_take_resume():
    from pcmef.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["formal", "run-e2", "--resume"]).resume is True
    assert parser.parse_args(["formal", "preflight", "--resume"]).resume is True


def test_the_reclaim_command_exists():
    """卡住的 RUNNING 必須有受控的出路，不能只剩「人工刪 claim」。"""
    from pcmef.cli import build_parser

    args = build_parser().parse_args(["formal", "reclaim", "--force",
                                      "--reason", "機器斷電"])
    assert args.force is True
    assert args.reason == "機器斷電"


# ---------------------------------------------------------------------------
# lifecycle 的結構
# ---------------------------------------------------------------------------


def test_the_lifecycle_is_reserve_running_try_except_else():
    """claim 的涵蓋範圍必須是整個流程，不是幾個記得加 try 的地方。

    先前 `_fail_claim` 只裝在 stress 生成與 execute loop 兩處，於是統計、
    bootstrap、trace finalise 與 report write 階段的失敗會讓 claim 停在
    RUNNING —— 而 RUNNING 同時擋掉 fresh restart 與 resume（NOTE-085）。
    """
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    # `_execute_formal_e2(` 出現兩次：第一次在「不取 claim」的早退分支
    # （dry run 與 claim=False），第二次才是 try 內那一次。順序只看後者。
    reserve_at = source.index("reserve(root, identity, resume=resume)")
    try_at = source.index("try:", reserve_at)
    order = [
        reserve_at,
        source.index("mark_running(root"),
        try_at,
        source.index("_execute_formal_e2(", try_at),
        source.index("except BaseException"),
        source.index("mark_interrupted(root"),
        source.index("mark_complete(root"),
    ]
    assert order == sorted(order), "順序必須是 reserve → running → try → except → complete"

    # 執行本體裡不得再**呼叫**任何 claim 操作 —— 那會讓涵蓋範圍變成兩份。
    # 比對帶括號的呼叫形式：docstring 裡提到這些名字是說明，不是動作。
    body = inspect.getsource(e2_formal._execute_formal_e2)
    for forbidden in ("mark_complete(", "mark_interrupted(", "mark_running(",
                      "reserve("):
        assert forbidden not in body, forbidden


def test_the_whole_execution_is_inside_the_try():
    """`_execute_formal_e2` 的呼叫必須是 try 內**唯一**的東西。

    也就是：statistics、bootstrap、trace finalise 與 report write 全部都在
    那一次呼叫裡面，因此全部被涵蓋。
    """
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    block = source[source.index("try:"):source.index("except BaseException")]
    assert "_execute_formal_e2(" in block
    # try 內不得有別的動作。
    for line in block.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert not stripped.startswith(("mark_", "reserve", "say(")), stripped


def test_marking_interrupted_does_not_mask_the_original_error():
    """標記失敗時不得吞掉原始例外 —— 那才是使用者要看的東西。"""
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    tail = source[source.index("except BaseException"):]
    assert "raise" in tail
    assert "except Exception as nested" in tail


def test_a_dry_run_never_takes_a_claim():
    """預演可以重跑，claim 保護的是「正式結果只有一份」。"""
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    assert "if not claim or dry_run:" in source


# ---------------------------------------------------------------------------
# 報告要說得出自己屬於哪個 claim（P1-6）
# ---------------------------------------------------------------------------


def test_the_report_persists_claim_provenance():
    """報告會被複製、附進論文、寄給別人 —— 那些副本旁邊沒有 claim 檔。"""
    from pcmef.experiments import e2_formal

    body = inspect.getsource(e2_formal._execute_formal_e2)
    claim_block = body[body.index('"run_claim":'):body.index('"run_claim":') + 700]
    for field in ("claim_id", "identity_digest", "resume_count"):
        assert field in claim_block, field
    # 且它在報告落盤**之前**寫進 document。
    assert body.index('"run_claim":') < body.index("(out / filename).write_text")


def test_the_claim_file_stays_authoritative():
    """報告記的是出身，不是狀態。lifecycle 的權威仍是 RUN_CLAIM.json。"""
    from pcmef.experiments import e2_formal

    body = inspect.getsource(e2_formal._execute_formal_e2)
    assert "RUN_CLAIM.json" in body
    assert "authoritative" in body or "權威" in body
