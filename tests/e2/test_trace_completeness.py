# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.experiments.decision_trace 的
#         TraceWriter 完整度欄位，以及 pcmef.console.run_view.trace_view
#         把它讀成畫面資料。不連線、不讀 dataset。
# 檔案路徑: tests/e2/test_trace_completeness.py
# 產生時間: 2026-09-04 21:55 +08:00
# 版本: v0.1.0
# 功能說明: 少寫的 case trace 必須看得出來。
# 模組定位: NOTE-073 的可執行防線。先前 index 只有 n_cases，沒有分母 ——
#           384 列裡寫成功 381 筆會顯示成 run_status=complete、n_cases=381，
#           而 381 在沒有分母時看起來就是完整的。
# 主要責任:
#   1. test_a_complete_run_is_complete
#   2. test_a_failed_case_makes_the_trace_partial
#   3. test_the_failed_rows_are_named
#   4. test_a_short_index_without_failures_is_still_partial
#   5. test_no_cases_is_unavailable_not_complete
#   6. test_abort_is_aborted_regardless_of_counts
#   7. test_trace_view_surfaces_partial_to_the_screen
#   8. test_an_old_index_is_unknown_not_complete
#   9. test_finalise_failure_does_not_fail_the_run
# 維護提醒:
#   - 不得在缺欄位時預設成 complete。缺欄位代表「不知道」，而把不知道
#     畫成 complete 正是這一組測試要擋的事。
#   - 不得讓 trace 的失敗改變 scientific_result。trace 是旁路觀測；
#     決策在它之前就完成了。
#   - v0.1.0 新增：首版，對應 P1-1 / NOTE-073。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_trace_completeness.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect
import json

import pytest

from pcmef.experiments.decision_trace import (
    RUN_STATUS_ABORTED, RUN_STATUS_COMPLETE, TRACE_ABORTED, TRACE_COMPLETE,
    TRACE_PARTIAL, TRACE_UNAVAILABLE, CaseTrace, TraceWriter,
)


def _trace(index: int) -> CaseTrace:
    return CaseTrace(
        case_id=f"case_{index:04d}", row_index=index, condition="clean",
        class_label="Empty", physical_scene_family="fam_00",
        inputs={"rgb": {}}, perception={}, quality={}, reliability={},
        routing={"route": "fusion", "escalated": False}, arms={},
        agent_execution={"invoked": False, "reason": "not_escalated"},
        final={"prediction_label": "Empty", "correct": True},
    )


def _writer(tmp_path, expected=None):
    return TraceWriter(
        tmp_path, run_id="r", code_revision="deadbeef",
        runtime_identity={}, dry_run=True, expected_cases=expected,
    )


def _index(tmp_path) -> dict:
    return json.loads(
        (tmp_path / "trace" / "trace_index.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 四種狀態
# ---------------------------------------------------------------------------


def test_a_complete_run_is_complete(tmp_path):
    writer = _writer(tmp_path, expected=3)
    for i in range(3):
        writer.write_case(_trace(i))
    writer.finalise()

    document = _index(tmp_path)
    assert document["trace_status"] == TRACE_COMPLETE
    assert document["expected_cases"] == 3
    assert document["written_cases"] == 3
    assert document["failed_trace_cases"] == []
    assert document["missing_cases"] == 0


def test_a_failed_case_makes_the_trace_partial(tmp_path):
    """384 列寫成功 381 筆 —— 這是題目裡指名的情境。"""
    writer = _writer(tmp_path, expected=384)
    for i in range(381):
        writer.write_case(_trace(i))
    for i in (381, 382, 383):
        writer.record_failure(i, f"case_{i:04d}", OSError("disk full"))
    writer.finalise()

    document = _index(tmp_path)
    assert document["trace_status"] == TRACE_PARTIAL
    assert document["expected_cases"] == 384
    assert document["written_cases"] == 381
    assert document["missing_cases"] == 3
    assert len(document["failed_trace_cases"]) == 3


def test_the_failed_rows_are_named(tmp_path):
    """必須說得出是哪幾列，而不是只說「有些失敗了」。"""
    writer = _writer(tmp_path, expected=2)
    writer.write_case(_trace(0))
    writer.record_failure(1, "case_0001", ValueError("bad preview"))
    writer.finalise()

    failed = _index(tmp_path)["failed_trace_cases"][0]
    assert failed["row_index"] == 1
    assert failed["case_id"] == "case_0001"
    assert "ValueError" in failed["error"]


def test_a_short_index_without_failures_is_still_partial(tmp_path):
    """對不上分母卻沒有記錄到失敗，同樣不是 complete。

    差額來源未知比「已知少三筆」更需要被看見。
    """
    writer = _writer(tmp_path, expected=5)
    for i in range(4):
        writer.write_case(_trace(i))
    writer.finalise()
    assert _index(tmp_path)["trace_status"] == TRACE_PARTIAL


def test_no_cases_is_unavailable_not_complete(tmp_path):
    writer = _writer(tmp_path, expected=10)
    writer.finalise()
    assert _index(tmp_path)["trace_status"] == TRACE_UNAVAILABLE


def test_abort_is_aborted_regardless_of_counts(tmp_path):
    writer = _writer(tmp_path, expected=3)
    for i in range(3):
        writer.write_case(_trace(i))
    writer.abort("FormalE2Error: ABORT_FORMAL_RUN at row 3")

    document = _index(tmp_path)
    assert document["run_status"] == RUN_STATUS_ABORTED
    assert document["trace_status"] == TRACE_ABORTED


def test_the_status_carries_its_own_meaning(tmp_path):
    """狀態字要自帶一句解釋。PARTIAL 必須說出「科學結果不受影響」。"""
    writer = _writer(tmp_path, expected=2)
    writer.write_case(_trace(0))
    writer.record_failure(1, "case_0001", OSError("nope"))
    writer.finalise()
    assert "科學結果不受影響" in _index(tmp_path)["trace_status_meaning"]


# ---------------------------------------------------------------------------
# 畫面
# ---------------------------------------------------------------------------


def test_trace_view_surfaces_partial_to_the_screen(tmp_path):
    from pcmef.console import run_view

    artifacts = tmp_path / "artifacts"
    writer = TraceWriter(artifacts, run_id="r", expected_cases=384)
    for i in range(381):
        writer.write_case(_trace(i))
    for i in (381, 382, 383):
        writer.record_failure(i, f"case_{i:04d}", OSError("disk full"))
    writer.finalise()

    view = run_view.trace_view(tmp_path)
    assert view["trace_status"] == TRACE_PARTIAL
    assert view["expected_cases"] == 384
    assert view["written_cases"] == 381
    assert view["missing_cases"] == 3
    assert len(view["failed_cases"]) == 3


def test_an_old_index_is_unknown_not_complete(tmp_path):
    """2026-09-04 之前的 index 沒有分母。此時不得宣稱 complete。"""
    from pcmef.console import run_view

    folder = tmp_path / "artifacts" / "trace"
    folder.mkdir(parents=True)
    (folder / "trace_index.json").write_text(
        json.dumps({
            "run_status": "complete", "n_cases": 2,
            "cases": [
                {"case_id": "case_0000", "row_index": 0, "route": "fusion"},
                {"case_id": "case_0001", "row_index": 1, "route": "fusion"},
            ],
        }),
        encoding="utf-8",
    )
    view = run_view.trace_view(tmp_path)
    assert view["trace_status"] == "unknown"
    assert view["expected_cases"] is None


# ---------------------------------------------------------------------------
# trace 的失敗不得變成實驗的失敗
# ---------------------------------------------------------------------------


def test_finalise_failure_does_not_fail_the_run():
    """`writer.finalise()` 的例外必須被接住。

    報告在它之前就已經落盤、統計已經算完；讓一次磁碟寫入錯誤把一場已經
    完成的一次性實驗宣告成失敗，是最貴的一種誤報。
    """
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal._execute_formal_e2)
    finalise_at = source.index("writer.finalise()")
    window = source[finalise_at - 400:finalise_at + 400]
    assert "try:" in window
    assert "trace_index_error" in window


def test_emit_trace_records_the_failure_before_re_raising():
    """單筆失敗要先記進 writer 再往外拋，否則分母對不上時說不出是哪一列。"""
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal._execute_formal_e2)
    assert "writer.record_failure(index, case_id, error)" in source


def test_the_executor_still_swallows_trace_exceptions():
    """外層的吞例外必須保留：trace 是觀測，不是決策路徑的一環。"""
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.execute_full_pcmef_cases)
    sink_at = source.index("trace_sink(")
    assert "except Exception" in source[sink_at:sink_at + 1500]


def test_expected_cases_is_the_row_count():
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal._execute_formal_e2)
    assert "expected_cases=len(rows)" in source


def test_completeness_is_written_into_the_report_itself():
    """報告必須帶 trace 完整度，而不是只有 index 帶。

    報告才是科研產物。一份「381/384」的 trace 若只出現在 index 裡，
    讀報告的人不會知道逐 case 檢視時會缺列 —— 而 index 是要另外去開的檔。
    """
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal._execute_formal_e2)
    trace_block = source.index('document["trace"] = {')
    write_at = source.index('(out / filename).write_text')
    assert trace_block < write_at, (
        "trace 完整度必須在報告落盤之前寫進 document；寫在之後的話那幾個"
        "欄位只存在於回傳值，不在檔案裡"
    )


def test_writer_defaults_keep_working_without_a_denominator(tmp_path):
    """沒給 expected_cases 時不得爆掉；此時只能靠失敗清單判斷。"""
    writer = TraceWriter(tmp_path, run_id="r")
    writer.write_case(_trace(0))
    writer.finalise()
    document = _index(tmp_path)
    assert document["expected_cases"] is None
    assert document["missing_cases"] is None
    assert document["trace_status"] == TRACE_COMPLETE
    assert writer.trace_status(RUN_STATUS_COMPLETE) == TRACE_COMPLETE
