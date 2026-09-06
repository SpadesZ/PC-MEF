# PC-MEF Research System source maintenance contract
# 上下游: 平台層 lifecycle 子套件的入口；由 console.workspace_routes 匯入。
#         匯入本套件時會一併註冊 PC-MEF provider。
# 檔案路徑: pcmef/platform/lifecycle/__init__.py
# 產生時間: 2026-09-07 12:00 +08:00
# 版本: v0.1.0
# 功能說明: 轉出 lifecycle 的資料結構與 build_lifecycle()，並確保
#           PC-MEF provider 已完成註冊。
# 模組定位: 平台化 Phase 4 的對外介面。Status 頁只需要
#           build_lifecycle(template, context)，不必知道有幾種 provider。
# 主要責任:
#   1. 轉出 Gate / Stage / LifecycleView 與五種狀態常數
#   2. 轉出 build_lifecycle / generic_lifecycle
#   3. 匯入 pcmef 模組以觸發 provider 註冊
# 維護提醒:
#   - 不得移除對 pcmef 模組的匯入。少了它，PC-MEF 專案會靜默退回
#     generic lifecycle —— 畫面上看起來只是「這個研究還沒有判準」。
#   - v0.1.0 新增：首版，對應平台化 Phase 4。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_lifecycle.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pcmef.platform.lifecycle.models import (
    BLOCKED,
    COMPLETE,
    GATE_STATES,
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
    registered_templates,
)

# 匯入即註冊。放在最後以免與 providers 形成循環匯入。
from pcmef.platform.lifecycle import pcmef as _pcmef  # noqa: E402,F401

__all__ = [
    "BLOCKED",
    "COMPLETE",
    "GATE_STATES",
    "GENERIC_STAGES",
    "IN_PROGRESS",
    "NOT_STARTED",
    "PASS",
    "Gate",
    "LifecycleView",
    "Stage",
    "build_lifecycle",
    "generic_lifecycle",
    "register",
    "registered_templates",
]
