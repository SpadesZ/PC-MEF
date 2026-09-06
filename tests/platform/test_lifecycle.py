# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.lifecycle 的 models 與 providers。
#         **純記憶體，不讀 freeze/、不啟動 app。**
# 檔案路徑: tests/platform/test_lifecycle.py
# 產生時間: 2026-09-07 12:40 +08:00
# 版本: v0.1.0
# 功能說明: Stage / Gate 狀態推導、generic lifecycle 與 provider 挑選的驗收。
# 模組定位: 平台化 Phase 4 的驗收。重點在「狀態一律由 gate 推導」
#           與「generic lifecycle 不得出現任何 PC-MEF 字樣」。
# 主要責任:
#   1. 驗證 Stage 狀態由 gates 推導的五種情況
#   2. 驗證沒有 gate 的 stage 是 NOT_STARTED 而非 COMPLETE
#   3. 驗證 current / next_stage / blockers 的推導
#   4. 驗證 generic lifecycle 不含任何專案專屬內容
#   5. 驗證 provider 失敗時退回 generic 而非 500
# 維護提醒:
#   - 不得把「沒有 gate」改判成 COMPLETE。那會讓尚未定義判準的階段
#     顯示成已完成，而那正是最容易被誤讀的一格。
#   - 不得放寬 test_the_generic_lifecycle_mentions_nothing_project_specific。
#   - v0.1.0 新增：首版，對應平台化 Phase 4。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_lifecycle.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.platform.lifecycle.models import (
    BLOCKED,
    COMPLETE,
    IN_PROGRESS,
    NOT_STARTED,
    PASS,
    Gate,
    LifecycleView,
    Stage,
)
from pcmef.platform.lifecycle.providers import (
    GENERIC_STAGES,
    build_lifecycle,
    generic_lifecycle,
    register,
)


def _stage(*statuses: str, stage_id: str = "s1") -> Stage:
    return Stage(
        stage_id=stage_id, title=stage_id,
        gates=tuple(
            Gate(gate_id=f"g{i}", label=f"g{i}", status=s)
            for i, s in enumerate(statuses)
        ),
    )


# ---------------------------------------------------------------------------
# Stage 狀態推導
# ---------------------------------------------------------------------------


def test_a_stage_with_no_gates_is_not_started(self=None):
    """**不是 COMPLETE。** 尚未定義判準的階段不該顯示成已完成。"""
    assert Stage(stage_id="s", title="S").status == NOT_STARTED


def test_all_satisfied_is_complete():
    assert _stage(PASS, COMPLETE).status == COMPLETE


def test_any_blocked_makes_the_stage_blocked():
    assert _stage(PASS, BLOCKED, NOT_STARTED).status == BLOCKED


def test_partial_progress_is_in_progress():
    assert _stage(PASS, NOT_STARTED).status == IN_PROGRESS


def test_nothing_started_is_not_started():
    assert _stage(NOT_STARTED, NOT_STARTED).status == NOT_STARTED


def test_blocked_outranks_in_progress():
    """BLOCKED 必須壓過 IN_PROGRESS —— 被擋住比有進度更該先看到。"""
    assert _stage(PASS, IN_PROGRESS, BLOCKED).status == BLOCKED


def test_an_unknown_gate_status_is_refused():
    with pytest.raises(ValueError, match="unknown gate status"):
        Gate(gate_id="g", label="g", status="MAYBE")


def test_cleared_counts_only_satisfied_gates():
    assert _stage(PASS, COMPLETE, BLOCKED, NOT_STARTED).cleared == 2


# ---------------------------------------------------------------------------
# LifecycleView 推導
# ---------------------------------------------------------------------------


def test_current_is_the_first_unfinished_stage():
    view = LifecycleView(stages=(
        _stage(PASS, stage_id="a"),
        _stage(NOT_STARTED, stage_id="b"),
        _stage(NOT_STARTED, stage_id="c"),
    ))
    assert view.current.stage_id == "b"
    assert view.next_stage.stage_id == "c"


def test_current_is_none_when_everything_is_complete():
    view = LifecycleView(stages=(_stage(PASS, stage_id="a"),))
    assert view.current is None
    assert view.next_stage is None


def test_blockers_come_from_the_current_stage_only():
    """後面階段的 gate 還沒輪到，列出來會讓人以為現在就該處理。"""
    view = LifecycleView(stages=(
        _stage(BLOCKED, stage_id="a"),
        _stage(BLOCKED, stage_id="b"),
    ))
    assert len(view.blockers) == 1


def test_completed_counts_finished_stages():
    view = LifecycleView(stages=(
        _stage(PASS, stage_id="a"),
        _stage(NOT_STARTED, stage_id="b"),
    ))
    assert len(view.completed) == 1


def test_the_view_serialises_its_derived_fields():
    view = LifecycleView(stages=(_stage(BLOCKED, stage_id="a"),), provider="p")
    data = view.to_json()
    assert data["current_stage_id"] == "a"
    assert data["total_stages"] == 1
    assert data["blockers"]


# ---------------------------------------------------------------------------
# generic provider
# ---------------------------------------------------------------------------


def test_the_generic_lifecycle_has_stages_but_no_gates():
    view = generic_lifecycle()
    assert len(view.stages) == len(GENERIC_STAGES)
    assert all(not s.gates for s in view.stages)
    assert all(s.status == NOT_STARTED for s in view.stages)


def test_the_generic_lifecycle_mentions_nothing_project_specific():
    """Blank Project 會渲染這一份，不得洩漏任何 PC-MEF 專屬字樣。"""
    view = generic_lifecycle()
    blob = " ".join(
        [view.note, view.provider]
        + [s.title + s.summary + s.stage_id for s in view.stages]
    )
    for leaked in ("PC-MEF", "E1", "AMD-", "ToF", "RGB", "Macro-F1",
                   "Water-filled", "Sigma", "PFC-"):
        assert leaked not in blob, f"{leaked!r} leaked into the generic lifecycle"


def test_an_unknown_template_falls_back_to_generic():
    view = build_lifecycle("no-such-template")
    assert view.provider == "generic"


def test_a_failing_provider_falls_back_instead_of_raising():
    """Status 是觀察頁；provider 壞掉不得讓整頁 500。"""
    def _boom(_context):
        raise RuntimeError("provider exploded")

    register("explodes", _boom)
    view = build_lifecycle("explodes")
    assert "failed" in view.provider
    assert "provider exploded" in view.note
    assert len(view.stages) == len(GENERIC_STAGES)


def test_a_registered_provider_is_used():
    marker = LifecycleView(stages=(), provider="marker")
    register("marker-template", lambda _c: marker)
    assert build_lifecycle("marker-template") is marker
