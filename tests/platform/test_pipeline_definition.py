# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.pipeline 的 models 與 registry。
#         **純記憶體，不讀 freeze/、不啟動 app。**
# 檔案路徑: tests/platform/test_pipeline_definition.py
# 產生時間: 2026-09-07 15:10 +08:00
# 版本: v0.1.0
# 功能說明: PipelineDefinition 的驗證規則、minimal 三節點流程與
#           provider 挑選的驗收。
# 模組定位: 平台化 Phase 6 的驗收。重點是「節點數量不是固定的」——
#           minimal 有三個、PC-MEF 有七個，兩者走同一組結構。
# 主要責任:
#   1. 驗證重複 stage_id 與未知依賴會被拒絕
#   2. 驗證 minimal pipeline 為三節點且不含 PC-MEF 內容
#   3. 驗證 next_of / stage 查詢
#   4. 驗證未知 template 與失敗 provider 都退回 minimal
# 維護提醒:
#   - 不得放寬 test_the_minimal_pipeline_mentions_nothing_project_specific。
#   - 不得在測試裡假設 stage 數量為 7；那正是本階段要拆掉的假設。
#   - v0.1.0 新增：首版，對應平台化 Phase 6。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_pipeline_definition.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.platform.pipeline.models import (
    PipelineDefinition,
    PipelineDefinitionError,
    PipelineStage,
)
from pcmef.platform.pipeline.registry import (
    build_definition,
    minimal_pipeline,
    register,
)


def _stage(stage_id: str, depends: tuple[str, ...] = ()) -> PipelineStage:
    return PipelineStage(
        stage_id=stage_id, display_name=stage_id, depends_on=depends
    )


# ---------------------------------------------------------------------------
# 定義驗證
# ---------------------------------------------------------------------------


def test_a_duplicate_stage_id_is_refused():
    with pytest.raises(PipelineDefinitionError, match="duplicate stage_id"):
        PipelineDefinition(
            pipeline_id="p", display_name="p",
            stages=(_stage("a"), _stage("a")),
        )


def test_a_dependency_on_an_unknown_stage_is_refused():
    """指向不存在的 stage 會讓執行順序在某些排列下靜默錯亂。"""
    with pytest.raises(PipelineDefinitionError, match="unknown stage"):
        PipelineDefinition(
            pipeline_id="p", display_name="p",
            stages=(_stage("a"), _stage("b", depends=("ghost",))),
        )


def test_a_forward_dependency_is_allowed_if_the_stage_exists():
    """依賴只要求 stage 存在；順序由 stages 的排列決定。"""
    definition = PipelineDefinition(
        pipeline_id="p", display_name="p",
        stages=(_stage("a", depends=("b",)), _stage("b")),
    )
    assert definition.stage_ids == ("a", "b")


@pytest.mark.parametrize("count", [1, 2, 3, 5, 7, 11])
def test_any_stage_count_is_valid(count):
    """**節點數量不是固定的。** 七是 PC-MEF 的形狀，不是平台的。"""
    definition = PipelineDefinition(
        pipeline_id="p", display_name="p",
        stages=tuple(_stage(f"s{i}") for i in range(count)),
    )
    assert len(definition) == count


def test_next_of_walks_the_declared_order():
    definition = PipelineDefinition(
        pipeline_id="p", display_name="p",
        stages=(_stage("a"), _stage("b"), _stage("c")),
    )
    assert definition.next_of("a").stage_id == "b"
    assert definition.next_of("c") is None
    assert definition.next_of("nope") is None


def test_stage_lookup_returns_none_for_unknown_ids():
    definition = PipelineDefinition(
        pipeline_id="p", display_name="p", stages=(_stage("a"),)
    )
    assert definition.stage("a") is not None
    assert definition.stage("z") is None


# ---------------------------------------------------------------------------
# minimal pipeline
# ---------------------------------------------------------------------------


def test_the_minimal_pipeline_has_three_stages():
    definition = minimal_pipeline()
    assert definition.stage_ids == ("input", "classifier", "decision")


def test_the_minimal_pipeline_declares_a_linear_chain():
    definition = minimal_pipeline()
    assert definition.stage("classifier").depends_on == ("input",)
    assert definition.stage("decision").depends_on == ("classifier",)


def test_the_minimal_pipeline_mentions_nothing_project_specific():
    """不是這篇論文的專案會看到它；不得洩漏任何 PC-MEF 內容。"""
    definition = minimal_pipeline()
    blob = " ".join(
        [definition.display_name, definition.note]
        + [
            f"{s.stage_id}{s.display_name}{s.english}{s.summary}{s.carries}"
            f"{s.input_desc}{s.process_desc}{s.output_desc}"
            for s in definition.stages
        ]
    )
    for leaked in ("PC-MEF", "ToF", "RGB", "Mitsuba", "VL53L0X", "Macro-F1",
                   "Water-filled", "Sigma", "Agent", "仲裁"):
        assert leaked not in blob, f"{leaked!r} leaked into the minimal pipeline"


# ---------------------------------------------------------------------------
# provider 挑選
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["", "blank", "minimal"])
def test_templates_that_legitimately_use_minimal_are_not_flagged(template):
    assert build_definition(template).pipeline_id in ("minimal",)


def test_an_unknown_template_is_flagged_unresolved(monkeypatch):
    """七節點的研究若因綁定錯誤變成三節點，畫面必須說出來。"""
    definition = build_definition("pcmef-thesiss")
    assert definition.pipeline_id.endswith("-unresolved")
    assert "不是這個 Profile 宣告的流程" in definition.note


def test_a_failing_provider_falls_back_instead_of_raising():
    def _boom(_context):
        raise RuntimeError("pipeline provider exploded")

    register("pipeline-explodes", _boom)
    definition = build_definition("pipeline-explodes")
    assert len(definition) == 3
    assert "pipeline provider exploded" in definition.note


def test_a_registered_provider_is_used():
    marker = PipelineDefinition(
        pipeline_id="marker", display_name="marker", stages=(_stage("only"),)
    )
    register("pipeline-marker", lambda _c: marker)
    assert build_definition("pipeline-marker") is marker
