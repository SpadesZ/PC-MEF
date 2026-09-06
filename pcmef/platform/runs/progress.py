# PC-MEF Research System source maintenance contract
# 上下游: 由 console 的 Run 頁呼叫；讀 platform.runs.events 的事件序列，
#         配合 platform.pipeline 的 PipelineDefinition 推導每個 stage 的狀態。
#         **唯讀，不寫入任何事件。**
# 檔案路徑: pcmef/platform/runs/progress.py
# 產生時間: 2026-09-07 16:30 +08:00
# 版本: v0.1.0
# 功能說明: 把 append-only 事件重播成「每個 stage 現在什麼狀態、跑到幾筆」。
# 模組定位: 平台化 Phase 5 的推導層。畫面上的進度全部出自這裡，
#           而這裡全部出自 executor 寫下的事件 ——
#           **前端不估算任何進度**。
# 主要責任:
#   1. STAGE_STATES 定義六種 stage 執行狀態
#   2. StageProgress 承載單一 stage 的狀態、進度與 artifact
#   3. RunProgress 提供整體視圖與目前 stage
#   4. build_progress() 由 definition + events 推導，失敗後的 stage 一律 BLOCKED
# 維護提醒:
#   - **失敗之後的 stage 一律 BLOCKED，不得 COMPLETE。**
#     即使事件檔裡出現了它們的 completed 事件（例如殘留自上一次執行），
#     一次失敗的執行不該在畫面上顯示成走完了。
#   - 不得由前端時間推估進度。沒有事件就是 NOT_STARTED；
#     估算出來的百分比在最慢的那一步一定是錯的，而那正是使用者
#     最想知道發生什麼的時候。
#   - 不得把未出現在 definition 裡的 stage 事件靜默丟掉；
#     那代表定義與執行不一致，必須看得見。
#   - v0.1.0 新增：首版，對應平台化 Phase 5。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_run_progress.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from pcmef.platform.runs.events import (
    RUN_FAILED,
    RUN_STARTED,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_PROGRESS,
    STAGE_SKIPPED,
    STAGE_STARTED,
    RunEvent,
)

__all__ = [
    "STAGE_STATES",
    "BLOCKED",
    "COMPLETE",
    "FAILED",
    "NOT_STARTED",
    "RUNNING",
    "SKIPPED",
    "RunProgress",
    "StageProgress",
    "build_progress",
]

NOT_STARTED = "NOT_STARTED"
RUNNING = "RUNNING"
COMPLETE = "COMPLETE"
FAILED = "FAILED"
BLOCKED = "BLOCKED"
SKIPPED = "SKIPPED"

STAGE_STATES: tuple[str, ...] = (
    NOT_STARTED, RUNNING, COMPLETE, FAILED, BLOCKED, SKIPPED,
)


@dataclass(frozen=True)
class StageProgress:
    """單一 stage 在某一次 run 中的狀態。"""

    stage_id: str
    display_name: str
    status: str = NOT_STARTED
    current: int | None = None
    total: int | None = None
    detail: str = ""
    artifacts: tuple[str, ...] = ()
    optional: bool = False
    #: 來自 PipelineDefinition 的靜態描述，讓畫面不必再查一次定義。
    input_desc: str = ""
    process_desc: str = ""
    output_desc: str = ""
    next_stage: str = ""

    @property
    def has_counts(self) -> bool:
        return self.current is not None and self.total not in (None, 0)

    @property
    def percent(self) -> int | None:
        """只有在 executor 給了 current/total 時才有百分比。

        沒有計數就沒有百分比 —— **不由經過時間推估**。
        """
        if not self.has_counts:
            return None
        return max(0, min(100, round(self.current * 100 / self.total)))

    def to_json(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id, "display_name": self.display_name,
            "status": self.status, "current": self.current, "total": self.total,
            "percent": self.percent, "detail": self.detail,
            "artifacts": list(self.artifacts), "optional": self.optional,
            "input": self.input_desc, "process": self.process_desc,
            "output": self.output_desc, "next": self.next_stage,
        }


@dataclass(frozen=True)
class RunProgress:
    """一次 run 的完整 stage 進度。"""

    stages: tuple[StageProgress, ...] = ()
    started: bool = False
    failed: bool = False
    #: 事件檔裡出現、但流程定義沒有的 stage。定義與執行不一致的訊號。
    unknown_stages: tuple[str, ...] = ()
    skipped_lines: int = 0
    extra: Any = field(default=None)

    @property
    def current(self) -> StageProgress | None:
        for stage in self.stages:
            if stage.status == RUNNING:
                return stage
        for stage in self.stages:
            if stage.status in (NOT_STARTED, BLOCKED, FAILED):
                return stage
        return None

    @property
    def completed_count(self) -> int:
        return sum(1 for s in self.stages if s.status in (COMPLETE, SKIPPED))

    @property
    def total_stages(self) -> int:
        """stage 總數。

        以 property 提供而不是只放在 to_json()：樣板取不到的屬性在
        Jinja 裡會安靜地變成空字串，於是畫面會印出「3 / 個 stage 完成」
        —— 看起來像排版問題，其實是資料沒接上。
        """
        return len(self.stages)

    @property
    def is_complete(self) -> bool:
        return bool(self.stages) and all(
            s.status in (COMPLETE, SKIPPED) for s in self.stages
        )

    def to_json(self) -> dict[str, Any]:
        current = self.current
        return {
            "stages": [s.to_json() for s in self.stages],
            "started": self.started,
            "failed": self.failed,
            "current_stage_id": current.stage_id if current else None,
            "completed_count": self.completed_count,
            "total_stages": len(self.stages),
            "is_complete": self.is_complete,
            "unknown_stages": list(self.unknown_stages),
            "skipped_lines": self.skipped_lines,
        }


def build_progress(
    definition: Any, events: Sequence[RunEvent], *, skipped_lines: int = 0
) -> RunProgress:
    """由流程定義與事件序列推導進度。

    事件依序重播。定義決定有哪些 stage 與其順序；事件決定它們各自
    走到哪 —— 兩者的分工是刻意的：定義是靜態的，進度屬於這一次執行。
    """
    stage_ids = list(getattr(definition, "stage_ids", ()) or ())
    state: dict[str, dict[str, Any]] = {
        sid: {"status": NOT_STARTED, "current": None, "total": None,
              "detail": "", "artifacts": ()}
        for sid in stage_ids
    }

    started = False
    failed = False
    unknown: list[str] = []
    failed_at: int | None = None

    for event in events:
        if event.event == RUN_STARTED:
            started = True
            continue
        if event.event == RUN_FAILED:
            failed = True
            continue

        sid = event.stage_id
        if sid not in state:
            if sid and sid not in unknown:
                unknown.append(sid)
            continue

        entry = state[sid]
        if event.event == STAGE_STARTED:
            started = True
            entry["status"] = RUNNING
            entry["total"] = event.total if event.total is not None else entry["total"]
            entry["detail"] = event.detail or entry["detail"]
        elif event.event == STAGE_PROGRESS:
            entry["status"] = RUNNING
            entry["current"] = event.current
            if event.total is not None:
                entry["total"] = event.total
            entry["detail"] = event.detail or entry["detail"]
        elif event.event == STAGE_COMPLETED:
            entry["status"] = COMPLETE
            entry["detail"] = event.detail or entry["detail"]
            if event.artifacts:
                entry["artifacts"] = event.artifacts
            if entry["total"] is not None and entry["current"] is None:
                entry["current"] = entry["total"]
        elif event.event == STAGE_FAILED:
            entry["status"] = FAILED
            entry["detail"] = event.detail or entry["detail"]
            failed = True
            if failed_at is None:
                failed_at = stage_ids.index(sid)
        elif event.event == STAGE_SKIPPED:
            entry["status"] = SKIPPED
            entry["detail"] = event.detail or entry["detail"]

    # 失敗之後的 stage 一律 BLOCKED。即使事件檔裡有它們的 completed
    # 事件，一次失敗的執行不該在畫面上顯示成走完了。
    if failed_at is not None:
        for sid in stage_ids[failed_at + 1:]:
            state[sid]["status"] = BLOCKED
            state[sid]["detail"] = state[sid]["detail"] or "前一個 stage 失敗，未執行。"

    stages = []
    definition_stages = list(getattr(definition, "stages", ()) or ())
    for index, sid in enumerate(stage_ids):
        entry = state[sid]
        source = definition_stages[index] if index < len(definition_stages) else None
        nxt = stage_ids[index + 1] if index + 1 < len(stage_ids) else ""
        stages.append(
            StageProgress(
                stage_id=sid,
                display_name=getattr(source, "display_name", sid),
                status=entry["status"],
                current=entry["current"],
                total=entry["total"],
                detail=entry["detail"],
                artifacts=tuple(entry["artifacts"]),
                optional=bool(getattr(source, "optional", False)),
                input_desc=getattr(source, "input_desc", ""),
                process_desc=getattr(source, "process_desc", ""),
                output_desc=getattr(source, "output_desc", ""),
                next_stage=nxt,
            )
        )

    return RunProgress(
        stages=tuple(stages),
        started=started,
        failed=failed,
        unknown_stages=tuple(unknown),
        skipped_lines=skipped_lines,
    )
