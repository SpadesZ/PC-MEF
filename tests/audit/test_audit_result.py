# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；只測 pcmef.audit.result 的純資料型別，
#         不讀寫任何檔案、不呼叫任何稽核器。
# 檔案路徑: tests/audit/test_audit_result.py
# 產生時間: 2026-08-27 09:55 +08:00
# 版本: v0.1.0
# 功能說明: 驗證四種稽核狀態的語意 —— 特別是「還沒產出」不等於「做錯了」，
#           以及被指名要求時「還沒產出」同樣不算通過。
# 模組定位: NOT_PRODUCED 與 FAIL 分野的可執行定義。
# 主要責任:
#   1. test_exit_code_* 驗四種狀態組合下的回傳碼
#   2. test_a_failed_check_must_name_a_finding 驗 FAIL 必須有理由
#   3. test_unmet_lists_only_the_required_ones 驗 required 的篩選
#   4. test_to_artifact_round_trips 驗落盤內容完整
#   5. test_lines_shows_findings_under_their_check 驗人類可讀輸出
# 維護提醒:
#   - 不得為了讓 CI 好看而讓 exit_code() 在有 FAIL 時回 0；
#     FAIL 代表已產出的證據自相矛盾，任何情況下都不該被當成正常。
#   - v0.1.0 新增：首版，對應 NOTE-022。
# 驗證方式:
#   - py -3.10 -m pytest tests/audit/test_audit_result.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.audit.result import AuditReport, CheckResult, CheckStatus


def _result(identifier: str, status: CheckStatus) -> CheckResult:
    return CheckResult(
        identifier=identifier,
        requirement=f"{identifier} requirement",
        status=status,
        findings=("something is wrong",) if status is CheckStatus.FAIL else (),
    )


def _report(*pairs: tuple[str, CheckStatus]) -> AuditReport:
    return AuditReport(
        name="unit", results=tuple(_result(i, s) for i, s in pairs)
    )


# ---------------------------------------------------------------------------
# exit code 語意
# ---------------------------------------------------------------------------


def test_exit_code_is_zero_when_everything_passes():
    assert _report(("A", CheckStatus.PASS)).exit_code() == 0


def test_exit_code_ignores_not_produced_when_nothing_is_required():
    """稽核器先於產物存在，盤點時不該因為「還沒做」就變紅。"""
    report = _report(("A", CheckStatus.PASS), ("B", CheckStatus.NOT_PRODUCED))
    assert report.exit_code() == 0
    assert report.counts()["NOT_PRODUCED"] == 1


def test_exit_code_is_nonzero_for_fail_even_without_required():
    """FAIL 代表已產出的證據自相矛盾，與「還沒產出」是兩回事。"""
    assert _report(("A", CheckStatus.FAIL)).exit_code() == 1


def test_a_required_but_not_produced_check_is_not_a_pass():
    report = _report(("A", CheckStatus.NOT_PRODUCED))
    assert report.exit_code() == 0
    assert report.exit_code(["A"]) == 1


def test_a_required_blocked_check_is_not_a_pass():
    report = _report(("A", CheckStatus.BLOCKED))
    assert report.exit_code(["A"]) == 1


def test_unmet_lists_only_the_required_ones():
    report = _report(
        ("A", CheckStatus.PASS),
        ("B", CheckStatus.NOT_PRODUCED),
        ("C", CheckStatus.FAIL),
    )
    assert {r.identifier for r in report.unmet(["A", "B"])} == {"B"}
    assert {r.identifier for r in report.unmet(["A", "B", "C"])} == {"B", "C"}


# ---------------------------------------------------------------------------
# 結構約束
# ---------------------------------------------------------------------------


def test_a_failed_check_must_name_a_finding():
    with pytest.raises(ValueError, match="findings"):
        CheckResult(identifier="X", requirement="y", status=CheckStatus.FAIL)


def test_a_passing_check_needs_no_findings():
    CheckResult(identifier="X", requirement="y", status=CheckStatus.PASS)


def test_unknown_identifier_fails_loudly():
    with pytest.raises(KeyError, match="not part of"):
        _report(("A", CheckStatus.PASS)).get("Z")


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------


def test_to_artifact_round_trips():
    report = _report(("A", CheckStatus.PASS), ("B", CheckStatus.FAIL))
    artifact = report.to_artifact()

    assert artifact["audit"] == "unit"
    assert artifact["counts"]["PASS"] == 1
    assert artifact["counts"]["FAIL"] == 1
    assert [c["id"] for c in artifact["checks"]] == ["A", "B"]
    assert artifact["checks"][1]["findings"] == ["something is wrong"]


def test_counts_covers_every_status():
    counts = _report(("A", CheckStatus.PASS)).counts()
    assert set(counts) == {s.value for s in CheckStatus}


def test_lines_shows_findings_under_their_check():
    lines = _report(("A", CheckStatus.FAIL)).lines()
    assert any("FAIL" in line and "A" in line for line in lines)
    assert any("something is wrong" in line for line in lines)
