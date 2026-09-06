# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.runs 的事件寫入、讀回與
#         stage 進度推導。**全部在 tmp_path，不啟動任何 executor。**
# 檔案路徑: tests/platform/test_run_progress.py
# 產生時間: 2026-09-07 17:00 +08:00
# 版本: v0.1.0
# 功能說明: run 事件的 append-only 語意，以及由事件重播出的 stage 狀態。
# 模組定位: 平台化 Phase 5 的驗收。最重要的一條是
#           test_stages_after_a_failure_are_blocked_not_complete ——
#           一次失敗的執行不得在畫面上顯示成走完了。
# 主要責任:
#   1. 驗證事件寫入為 append-only 且可讀回
#   2. 驗證壞行只被略過並計數，不丟棄整份記錄
#   3. 驗證 NOT_STARTED → RUNNING → COMPLETE 的逐 stage 推進
#   4. 驗證失敗後的 stage 一律 BLOCKED
#   5. 驗證沒有計數就沒有百分比（不由時間推估）
# 維護提醒:
#   - 不得放寬失敗後 BLOCKED 的判定，即使事件檔裡有 completed 事件。
#   - 不得為 StageProgress 加入任何以時間推估的進度。
#   - v0.1.0 新增：首版，對應平台化 Phase 5。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_run_progress.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.platform.pipeline.registry import minimal_pipeline
from pcmef.platform.runs.events import (
    EVENT_FILENAME,
    RunEventWriter,
    read_events,
)
from pcmef.platform.runs.progress import (
    BLOCKED,
    COMPLETE,
    FAILED,
    NOT_STARTED,
    RUNNING,
    SKIPPED,
    build_progress,
)

DEFINITION = minimal_pipeline()


def _progress(run_dir):
    events, skipped = read_events(run_dir)
    return build_progress(DEFINITION, events, skipped_lines=skipped)


def _status(progress, stage_id):
    for stage in progress.stages:
        if stage.stage_id == stage_id:
            return stage.status
    raise AssertionError(f"no stage {stage_id!r}")


# ---------------------------------------------------------------------------
# 事件寫入與讀回
# ---------------------------------------------------------------------------


def test_events_are_appended_one_line_each(tmp_path):
    writer = RunEventWriter(tmp_path)
    writer.run_started()
    writer.stage_started("input", total=10)
    writer.stage_completed("input")

    lines = (tmp_path / EVENT_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_events_round_trip(tmp_path):
    writer = RunEventWriter(tmp_path)
    writer.stage_progress("classifier", current=7, total=10, detail="working")

    events, skipped = read_events(tmp_path)
    assert skipped == 0
    assert events[0].stage_id == "classifier"
    assert events[0].current == 7
    assert events[0].total == 10


def test_a_missing_event_file_is_not_an_error(tmp_path):
    assert read_events(tmp_path) == ([], 0)


def test_a_broken_line_is_skipped_and_counted(tmp_path):
    """寫到一半的行不得讓整次執行看起來沒發生過。"""
    writer = RunEventWriter(tmp_path)
    writer.stage_started("input")
    with open(tmp_path / EVENT_FILENAME, "a", encoding="utf-8") as stream:
        stream.write('{"event": "stage_st\n')
    writer.stage_completed("input")

    events, skipped = read_events(tmp_path)
    assert len(events) == 2
    assert skipped == 1


def test_an_unknown_event_type_is_refused(tmp_path):
    with open(tmp_path / EVENT_FILENAME, "w", encoding="utf-8") as stream:
        stream.write('{"event": "teleported"}\n')
    events, skipped = read_events(tmp_path)
    assert events == [] and skipped == 1


# ---------------------------------------------------------------------------
# 逐 stage 推進
# ---------------------------------------------------------------------------


def test_nothing_written_means_nothing_started(tmp_path):
    progress = _progress(tmp_path)
    assert progress.started is False
    assert all(s.status == NOT_STARTED for s in progress.stages)


def test_stages_advance_one_at_a_time(tmp_path):
    """NOT_STARTED → RUNNING → COMPLETE 必須逐 stage 發生。"""
    writer = RunEventWriter(tmp_path)
    writer.run_started()

    writer.stage_started("input", total=3)
    p = _progress(tmp_path)
    assert _status(p, "input") == RUNNING
    assert _status(p, "classifier") == NOT_STARTED
    assert _status(p, "decision") == NOT_STARTED

    writer.stage_completed("input")
    writer.stage_started("classifier", total=384)
    writer.stage_progress("classifier", current=214, total=384)
    p = _progress(tmp_path)
    assert _status(p, "input") == COMPLETE
    assert _status(p, "classifier") == RUNNING
    assert _status(p, "decision") == NOT_STARTED

    classifier = [s for s in p.stages if s.stage_id == "classifier"][0]
    assert (classifier.current, classifier.total) == (214, 384)
    assert classifier.percent == 56

    writer.stage_completed("classifier")
    writer.stage_started("decision")
    writer.stage_completed("decision")
    p = _progress(tmp_path)
    assert p.is_complete is True
    assert p.completed_count == 3


def test_the_current_stage_is_the_running_one(tmp_path):
    writer = RunEventWriter(tmp_path)
    writer.stage_completed("input")
    writer.stage_started("classifier")
    assert _progress(tmp_path).current.stage_id == "classifier"


def test_completed_without_progress_counts_as_full(tmp_path):
    writer = RunEventWriter(tmp_path)
    writer.stage_started("input", total=5)
    writer.stage_completed("input")
    stage = [s for s in _progress(tmp_path).stages if s.stage_id == "input"][0]
    assert stage.current == 5


def test_artifacts_are_carried_from_the_completion_event(tmp_path):
    writer = RunEventWriter(tmp_path)
    writer.stage_completed("input", artifacts=["a.json", "b.json"])
    stage = [s for s in _progress(tmp_path).stages if s.stage_id == "input"][0]
    assert stage.artifacts == ("a.json", "b.json")


# ---------------------------------------------------------------------------
# 失敗語意 —— 本節是 Phase 5 最重要的不變量
# ---------------------------------------------------------------------------


def test_stages_after_a_failure_are_blocked_not_complete(tmp_path):
    writer = RunEventWriter(tmp_path)
    writer.run_started()
    writer.stage_completed("input")
    writer.stage_failed("classifier", detail="model missing")

    progress = _progress(tmp_path)
    assert _status(progress, "input") == COMPLETE
    assert _status(progress, "classifier") == FAILED
    assert _status(progress, "decision") == BLOCKED
    assert progress.failed is True
    assert progress.is_complete is False


def test_a_completion_event_after_a_failure_does_not_turn_green(tmp_path):
    """殘留或錯誤的 completed 事件不得讓失敗的執行顯示成走完了。"""
    writer = RunEventWriter(tmp_path)
    writer.stage_failed("input")
    writer.stage_completed("classifier")
    writer.stage_completed("decision")

    progress = _progress(tmp_path)
    assert _status(progress, "classifier") == BLOCKED
    assert _status(progress, "decision") == BLOCKED
    assert progress.is_complete is False


def test_a_skipped_optional_stage_does_not_block_completion(tmp_path):
    writer = RunEventWriter(tmp_path)
    for sid in ("input", "classifier"):
        writer.stage_completed(sid)
    writer.stage_skipped("decision", detail="not applicable")

    progress = _progress(tmp_path)
    assert _status(progress, "decision") == SKIPPED
    assert progress.is_complete is True


# ---------------------------------------------------------------------------
# 進度必須來自 executor
# ---------------------------------------------------------------------------


def test_no_counts_means_no_percentage(tmp_path):
    """沒有計數就沒有百分比 —— **不由經過時間推估**。"""
    writer = RunEventWriter(tmp_path)
    writer.stage_started("input")
    stage = [s for s in _progress(tmp_path).stages if s.stage_id == "input"][0]
    assert stage.percent is None
    assert stage.has_counts is False


@pytest.mark.parametrize(
    "current,total,expected", [(0, 384, 0), (214, 384, 56), (384, 384, 100)]
)
def test_percentage_comes_from_the_reported_counts(tmp_path, current, total, expected):
    writer = RunEventWriter(tmp_path)
    writer.stage_progress("input", current=current, total=total)
    stage = [s for s in _progress(tmp_path).stages if s.stage_id == "input"][0]
    assert stage.percent == expected


def test_events_for_unknown_stages_are_surfaced(tmp_path):
    """定義與執行不一致必須看得見，不得靜默丟掉。"""
    writer = RunEventWriter(tmp_path)
    writer.stage_started("ghost-stage")
    assert _progress(tmp_path).unknown_stages == ("ghost-stage",)


def test_stage_descriptions_come_from_the_definition(tmp_path):
    writer = RunEventWriter(tmp_path)
    writer.stage_started("classifier")
    stage = [s for s in _progress(tmp_path).stages if s.stage_id == "classifier"][0]
    assert stage.input_desc == "樣本批次"
    assert stage.next_stage == "decision"


def test_total_stages_is_reachable_from_a_template(tmp_path):
    """Jinja 取不到的屬性會安靜地變成空字串。

    這條擋的是「3 / 個 stage 完成」那種畫面 —— 看起來像排版問題，
    其實是資料沒接上。
    """
    progress = _progress(tmp_path)
    assert progress.total_stages == 3
    assert progress.completed_count == 0


def test_the_json_shape_and_the_properties_agree(tmp_path):
    writer = RunEventWriter(tmp_path)
    writer.stage_completed("input")
    progress = _progress(tmp_path)
    data = progress.to_json()

    assert data["total_stages"] == progress.total_stages
    assert data["completed_count"] == progress.completed_count
    assert data["is_complete"] == progress.is_complete
