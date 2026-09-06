# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.pipeline.pcmef 的七節點定義。
#         **不讀真實 freeze/ 的內容判定，只驗結構與保留性。**
# 檔案路徑: tests/platform/test_pipeline_pcmef.py
# 產生時間: 2026-09-07 15:25 +08:00
# 版本: v0.1.0
# 功能說明: PC-MEF 七節點被完整包成 PipelineDefinition 的驗收。
# 模組定位: 平台化 Phase 6 的 PC-MEF adapter 驗收。重點是
#           「七節點與其 lock 細節完整保留」，同時它只是**一份定義**。
# 主要責任:
#   1. 驗證七個節點與其順序完整保留
#   2. 驗證節點文案沿用 console.pipeline，未被改寫
#   3. 驗證 arbitration 標為 optional
#   4. 驗證每個節點都宣告 artifact 角色
#   5. 驗證鏈狀依賴正確
# 維護提醒:
#   - 不得把 arbitration 改成必經。只有困難案例才進入仲裁；
#     標成必經會讓 escalation rate 失去意義。
#   - 不得在此重寫節點文案。文案的來源是 console.pipeline。
#   - v0.1.0 新增：首版，對應平台化 Phase 6。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_pipeline_pcmef.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pcmef.console.pipeline import PIPELINE_NODES
from pcmef.platform.pipeline.pcmef import pcmef_pipeline


class _Ctx:
    def __init__(self, freeze_dir=None):
        self.freeze_dir = freeze_dir
        self.profile = None


def test_all_seven_nodes_are_preserved_in_order():
    """七節點是 PC-MEF 的科學流程，平台化不得改動它。"""
    definition = pcmef_pipeline(_Ctx())
    assert definition.stage_ids == tuple(n.key for n in PIPELINE_NODES)
    assert len(definition) == 7


def test_node_wording_is_reused_not_rewritten():
    """文案的來源是 console.pipeline；重寫一份會產生兩種說法。"""
    definition = pcmef_pipeline(_Ctx())
    for node in PIPELINE_NODES:
        stage = definition.stage(node.key)
        assert stage.display_name == node.label
        assert stage.english == node.english
        assert stage.summary == node.summary
        assert stage.carries == node.carries


def test_arbitration_is_optional():
    """只有 reliability routing 選出的困難案例會走這一步。"""
    definition = pcmef_pipeline(_Ctx())
    assert definition.stage("arbitration").optional is True


def test_every_other_stage_is_required():
    definition = pcmef_pipeline(_Ctx())
    required = [s.stage_id for s in definition.stages if not s.optional]
    assert len(required) == 6
    assert "arbitration" not in required


def test_the_stages_form_a_linear_chain():
    definition = pcmef_pipeline(_Ctx())
    assert definition.stage("simulation").depends_on == ()
    for index, stage in enumerate(definition.stages[1:], start=1):
        assert stage.depends_on == (definition.stages[index - 1].stage_id,)


def test_every_stage_declares_artifact_roles():
    """artifact 角色屬於流程定義；路徑屬於某一次 run。"""
    definition = pcmef_pipeline(_Ctx())
    missing = [s.stage_id for s in definition.stages if not s.artifact_roles]
    assert not missing, f"stages without artifact roles: {missing}"


def test_the_definition_is_identified_as_the_thesis_pipeline():
    definition = pcmef_pipeline(_Ctx())
    assert definition.pipeline_id == "pcmef-thesis"
    assert "七節點" in definition.display_name


def test_it_survives_an_unresolvable_lineage(tmp_path):
    """讀不到 lock 時仍要給出七節點結構，只是沒有還原值。

    Pipeline 是觀察頁；fail-closed 只會換來一頁 500，而使用者連
    「為什麼看不到」都不知道。
    """
    definition = pcmef_pipeline(_Ctx(tmp_path / "nonexistent"))
    assert len(definition) == 7
    assert definition.note, "the reason must be surfaced, not swallowed"
