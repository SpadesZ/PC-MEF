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


#: 這個研究模板實作了哪幾支 executor。
#:
#: 與 pipeline provider 登記在同一個地方，因為它們回答的是同一件事的
#: 兩半：pipeline 說「這個研究長什麼樣」，executor 說「它有什麼可以
#: 真的跑」。分在兩處登記，遲早會有一個模板宣告了流程卻沒有執行器，
#: 或者相反 —— 而後者正是空專案跑出碩論場景的形狀。
#:
#: stage_id 對應 PIPELINE_NODES 的 key；沒有對應節點的（凍結、稽核）
#: 用自己的名字，Run 頁會照實說「事件檔提到流程定義裡沒有的 stage」。
def _register() -> None:
    from pcmef.platform import executors as ex
    from pcmef.platform.pipeline.registry import register

    register(PROVIDER_NAME, pcmef_pipeline)
    # scope 與 stage_ids 由 executor 這一側宣告。
    #
    # `formal_e2` 涵蓋的是**整條推論鏈**：一次正式執行從感知一路跑到
    # 決策，把它記成單一 `decision` 節點，紀錄就會宣稱它只做了最後
    # 一步。`llm_snapshot` 與 `audit_gates` 則根本不是流程的一步 ——
    # 硬塞一個 stage 進去同樣是說謊，所以它們是 action，不列任何節點。
    for kind, display_name, scope, stage_ids, event_stage, summary in (
        ("sim_smoke", "物理校準模擬", ex.SCOPE_STAGE,
         (ex.STAGE_SIMULATION,), "",
         "以場景設定算出 transient 並產出 smoke manifest。"),
        ("surrogate_smoke", "代理模型四特徵", ex.SCOPE_STAGE,
         (ex.STAGE_PERCEPTION,), "",
         "由既有模擬輸出萃取四特徵。"),
        ("llm_snapshot", "LLM runtime 快照", ex.SCOPE_ACTION, (),
         ex.STAGE_LLM_SNAPSHOT,
         "解析 draft binding 成 lock candidate，可選擇凍結。"),
        ("formal_e2", "Formal E2", ex.SCOPE_PIPELINE,
         (ex.STAGE_PERCEPTION, ex.STAGE_RELIABILITY, ex.STAGE_ROUTING,
          ex.STAGE_ARBITRATION, ex.STAGE_DECISION), ex.STAGE_FORMAL_E2,
         "PC-MEF Formal E2 的預演與一次性正式執行，涵蓋感知到決策。"),
        ("audit_gates", "E1 gate 稽核", ex.SCOPE_ACTION, (), ex.STAGE_AUDIT,
         "逐項檢查 E1 開跑前的十二個 gate。"),
    ):
        ex.register(
            PROVIDER_NAME,
            ex.Executor(kind=kind, display_name=display_name, scope=scope,
                        stage_ids=stage_ids, event_stage=event_stage,
                        summary=summary),
        )


_register()
