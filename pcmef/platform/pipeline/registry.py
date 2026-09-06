# PC-MEF Research System source maintenance contract
# 上下游: 由 console.workspace_routes 的 Pipeline 頁與 Run 頁呼叫；
#         依 Profile 的 template 選出 provider，回傳 PipelineDefinition。
# 檔案路徑: pcmef/platform/pipeline/registry.py
# 產生時間: 2026-09-07 14:25 +08:00
# 版本: v0.1.0
# 功能說明: Pipeline provider 的註冊與挑選，以及三節點的 minimal 流程。
# 模組定位: 平台化 Phase 6。與 lifecycle 同一個模式：
#           **通用結構在這裡，PC-MEF 的七節點在 pcmef.py。**
# 主要責任:
#   1. MINIMAL_PIPELINE 提供 Input → Classifier → Decision 三節點流程
#   2. register() / build_definition() 依 template 挑 provider
#   3. 找不到 provider 時回傳 minimal 而非丟例外
# 維護提醒:
#   - **不得在 minimal pipeline 加入任何 PC-MEF 專屬內容。**
#     它是「不是這篇論文的專案」看到的東西；洩漏進去的模態或類別
#     會讓一個空專案看起來已經有研究設計。
#   - 不得讓 build_definition() 在 provider 失敗時丟例外。Pipeline 是
#     觀察頁；壞掉要顯示原因，不是 500。
#   - v0.1.0 新增：首版，對應平台化 Phase 6。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_pipeline_definition.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any, Callable

from pcmef.platform.pipeline.models import PipelineDefinition, PipelineStage

__all__ = [
    "MINIMAL_TEMPLATE",
    "build_definition",
    "minimal_pipeline",
    "register",
    "registered_templates",
]

MINIMAL_TEMPLATE = "minimal"

_PROVIDERS: dict[str, Callable[[Any], PipelineDefinition]] = {}


def register(template: str, provider: Callable[[Any], PipelineDefinition]) -> None:
    _PROVIDERS[template] = provider


def registered_templates() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def minimal_pipeline(context: Any = None) -> PipelineDefinition:
    """三個節點：Input → Classifier → Decision。

    存在的理由不只是示範：它證明 UI 與執行模型沒有把七節點寫死。
    節點數不同時畫面若會壞，這個定義會先壞給我們看。
    """
    return PipelineDefinition(
        pipeline_id=MINIMAL_TEMPLATE,
        display_name="Minimal Pipeline",
        note="三節點的最小流程。用來確認流程長度不是固定的。",
        stages=(
            PipelineStage(
                stage_id="input", display_name="輸入", english="Input",
                summary="讀入一批待判斷的樣本。",
                carries="樣本批次",
                input_desc="資料來源", process_desc="讀取與基本檢查",
                output_desc="樣本批次",
                artifact_roles=("input-manifest",),
            ),
            PipelineStage(
                stage_id="classifier", display_name="分類器", english="Classifier",
                summary="對每個樣本輸出類別分布。",
                carries="每個樣本的類別分布",
                input_desc="樣本批次", process_desc="模型推論",
                output_desc="類別分布",
                artifact_roles=("predictions",),
                depends_on=("input",),
            ),
            PipelineStage(
                stage_id="decision", display_name="決策", english="Decision",
                summary="由分布取出最終判斷並彙整指標。",
                carries="最終判斷與指標",
                input_desc="類別分布", process_desc="取 argmax 並彙整",
                output_desc="預測類別、整體指標",
                artifact_roles=("decisions", "metrics"),
                depends_on=("classifier",),
            ),
        ),
    )


def build_definition(template: str | None, context: Any = None) -> PipelineDefinition:
    """依 template 挑 provider；找不到或壞掉都退回 minimal。"""
    provider = _PROVIDERS.get(template or "")
    if provider is None:
        return minimal_pipeline(context)
    try:
        return provider(context)
    except Exception as error:  # noqa: BLE001 - 觀察頁不得因此 500
        base = minimal_pipeline(context)
        return PipelineDefinition(
            pipeline_id=base.pipeline_id,
            display_name=base.display_name,
            stages=base.stages,
            note=f"無法建立此 Profile 的流程定義：{error}",
        )
