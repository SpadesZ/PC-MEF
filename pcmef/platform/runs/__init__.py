# PC-MEF Research System source maintenance contract
# 上下游: 平台層 runs 子套件的入口；executor 匯入 RunEventWriter，
#         console 的 Run 頁匯入 read_events 與 build_progress。
# 檔案路徑: pcmef/platform/runs/__init__.py
# 產生時間: 2026-09-07 16:45 +08:00
# 版本: v0.1.0
# 功能說明: 轉出 run 事件的寫入／讀取與 stage 進度推導。
# 模組定位: 平台化 Phase 5 的對外介面。
#           **寫入端只給 executor，讀取端給 Web。**
# 主要責任:
#   1. 轉出 RunEventWriter（executor 用）
#   2. 轉出 read_events / build_progress（Web 用）
#   3. 轉出 stage 執行狀態常數
# 維護提醒:
#   - 不得讓 console 匯入 RunEventWriter。Web 若能補寫事件，
#     畫面就能顯示從未發生過的進度。
#   - v0.1.0 新增：首版，對應平台化 Phase 5。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_run_progress.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pcmef.platform.runs.attribution import (
    ATTRIBUTION_FILENAME,
    BOUNDARY_FILENAME,
    AttributionExistsError,
    attribution_boundary,
    is_legacy_run,
    RunAttribution,
    digest_of,
    owned_by,
    read_attribution,
    snapshot_stage_ids,
    write_attribution,
)
from pcmef.platform.runs.events import (
    EVENT_FILENAME,
    EVENT_TYPES,
    RunEvent,
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
    STAGE_STATES,
    RunProgress,
    StageProgress,
    build_progress,
)

__all__ = [
    "ATTRIBUTION_FILENAME",
    "BOUNDARY_FILENAME",
    "attribution_boundary",
    "is_legacy_run",
    "AttributionExistsError",
    "RunAttribution",
    "digest_of",
    "owned_by",
    "read_attribution",
    "snapshot_stage_ids",
    "write_attribution",
    "BLOCKED",
    "COMPLETE",
    "EVENT_FILENAME",
    "EVENT_TYPES",
    "FAILED",
    "NOT_STARTED",
    "RUNNING",
    "SKIPPED",
    "STAGE_STATES",
    "RunEvent",
    "RunEventWriter",
    "RunProgress",
    "StageProgress",
    "build_progress",
    "read_events",
]
