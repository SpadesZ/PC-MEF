# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.pipeline.registry 依 template 選用；沿用
#         console.pipeline 既有的七節點文案與 lock 還原細節。**唯讀。**
# 檔案路徑: pcmef/platform/pipeline/pcmef.py
# 產生時間: 2026-09-07 14:40 +08:00
# 版本: v0.1.0
# 功能說明: 把 PC-MEF 的七節點包成一份 PipelineDefinition。
# 模組定位: 平台化 Phase 6 的 PC-MEF adapter。七節點**完整保留**，
#           包含各節點由 lock 還原出的實際值；改變的只是
#           「它是 PC-MEF Profile 的流程定義」而不是「平台的流程」。
# 主要責任:
#   1. pcmef_pipeline() 由 console.pipeline 的節點與細節組出定義
#   2. 保留每個節點的 Input / Process / Output 與 lock 出處
#   3. 標出 arbitration 為 optional（只有困難案例會走）
# 維護提醒:
#   - **不得在此重寫節點文案或 lock 還原邏輯。** 那些在
#     console.pipeline，重寫一份會讓兩處對同一個流程給出兩種說法。
#   - 不得把 arbitration 標成必經。只有 reliability routing 選出的
#     困難案例才會進入仲裁；標成必經會讓 escalation rate 失去意義。
#   - v0.1.0 新增：首版，對應平台化 Phase 6。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_pipeline_pcmef.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any

from pcmef.platform.pipeline.models import PipelineDefinition, PipelineStage
from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID

__all__ = ["PROVIDER_NAME", "pcmef_pipeline"]

#: 這份定義註冊在哪個 template 之下。由常數導出，不重寫字面值。
PROVIDER_NAME = LEGACY_THESIS_PROJECT_ID

#: 每個節點產出的 artifact 角色。名稱是角色，不是路徑 ——
#: 路徑屬於某一次 run，角色屬於流程定義。
_ARTIFACT_ROLES: dict[str, tuple[str, ...]] = {
    "simulation": ("transient-response",),
    "paired": ("paired-case", "dataset-manifest"),
    "perception": ("class-distribution",),
    "reliability": ("quality-scores",),
    "routing": ("route-decision",),
    "arbitration": ("role-artifact", "evidence-support"),
    "decision": ("final-decision", "metrics", "statistics"),
}

#: 只有 reliability routing 選出的困難案例會走這一步。
_OPTIONAL_STAGES = frozenset({"arbitration"})


def pcmef_pipeline(context: Any = None) -> PipelineDefinition:
    """PC-MEF 七節點流程。節點文案與 lock 細節沿用 console.pipeline。"""
    from pcmef.console.pipeline import PIPELINE_NODES, build_pipeline

    freeze_dir = getattr(context, "freeze_dir", None)
    built = build_pipeline(freeze_dir)
    detail_by_key = {node["key"]: node.get("detail") for node in built["nodes"]}

    stages = []
    previous: tuple[str, ...] = ()
    for node in PIPELINE_NODES:
        detail = detail_by_key.get(node.key)
        stages.append(
            PipelineStage(
                stage_id=node.key,
                display_name=node.label,
                english=node.english,
                summary=node.summary,
                carries=node.carries,
                # Input / Process / Output 的實際值由 lock 還原，放在 detail；
                # 這三欄留給一句話的描述，讓沒有 lock 時畫面仍然有內容。
                input_desc=(previous[0] if previous else "場景與感測設定"),
                process_desc=node.summary,
                output_desc=node.carries,
                artifact_roles=_ARTIFACT_ROLES.get(node.key, ()),
                depends_on=previous,
                optional=node.key in _OPTIONAL_STAGES,
                detail=detail,
            )
        )
        previous = (node.key,)

    return PipelineDefinition(
        pipeline_id=PROVIDER_NAME,
        display_name="PC-MEF 七節點流程",
        stages=tuple(stages),
        note=built.get("detail_note", ""),
        extra={
            "lineage": built.get("lineage", {}),
            "detail_available": built.get("detail_available", False),
            "threshold_caveat": built.get("threshold_caveat", ""),
        },
    )


def _register() -> None:
    from pcmef.platform.pipeline.registry import register

    register(PROVIDER_NAME, pcmef_pipeline)


_register()
