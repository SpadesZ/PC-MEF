# PC-MEF Research System source maintenance contract
# 上下游: 平台層 pipeline 子套件的入口；由 console.workspace_routes 匯入。
#         匯入本套件時會一併註冊 PC-MEF 的七節點 provider。
# 檔案路徑: pcmef/platform/pipeline/__init__.py
# 產生時間: 2026-09-07 14:50 +08:00
# 版本: v0.1.0
# 功能說明: 轉出 PipelineDefinition / PipelineStage 與 build_definition()。
# 模組定位: 平台化 Phase 6 的對外介面。Pipeline 頁只需要
#           build_definition(template, context)，不必知道有幾個節點。
# 主要責任:
#   1. 轉出 PipelineDefinition、PipelineStage 與其例外
#   2. 轉出 build_definition / minimal_pipeline
#   3. 匯入 pcmef 模組以觸發七節點 provider 註冊
# 維護提醒:
#   - 不得移除對 pcmef 模組的匯入。少了它，PC-MEF 專案會靜默退回
#     三節點的 minimal pipeline —— 而畫面看起來只是「這個流程比較短」。
#   - v0.1.0 新增：首版，對應平台化 Phase 6。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_pipeline_definition.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pcmef.platform.pipeline.models import (
    PIPELINE_SCHEMA_VERSION,
    PipelineDefinition,
    PipelineDefinitionError,
    PipelineStage,
)
from pcmef.platform.pipeline.registry import (
    MINIMAL_TEMPLATE,
    build_definition,
    minimal_pipeline,
    register,
    registered_templates,
)

# 匯入即註冊。放最後以免與 registry 形成循環匯入。
from pcmef.platform.pipeline import pcmef as _pcmef  # noqa: E402,F401

__all__ = [
    "MINIMAL_TEMPLATE",
    "PIPELINE_SCHEMA_VERSION",
    "PipelineDefinition",
    "PipelineDefinitionError",
    "PipelineStage",
    "build_definition",
    "minimal_pipeline",
    "register",
    "registered_templates",
]
