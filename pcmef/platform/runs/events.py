# PC-MEF Research System source maintenance contract
# 上下游: 由 executor / CLI 子行程寫入，由 console 的 Run 頁讀取。
#         每個 run 一個 append-only JSONL 檔。**Web 只讀，不寫。**
# 檔案路徑: pcmef/platform/runs/events.py
# 產生時間: 2026-09-07 16:10 +08:00
# 版本: v0.1.0
# 功能說明: run 的 stage 事件記錄 —— 寫入、讀取與 schema。
# 模組定位: 平台化 Phase 5 的 source of truth。
#           **進度由 executor 寫出的事件決定**，前端只是把事件重播成畫面。
#           前端自行估算的進度會在慢的那一步說謊，而慢的那一步
#           正是使用者最想知道發生什麼的時候。
# 主要責任:
#   1. EVENT_TYPES 定義六種事件與其語意
#   2. RunEventWriter 以 append-only 方式寫入，一行一事件
#   3. read_events() 讀回事件序列，壞行略過但計數
#   4. emit_* 便利函式給 executor 使用
# 維護提醒:
#   - **不得讓 Web 寫入事件。** executor 是唯一真相來源；
#     Web 若能補寫事件，畫面就能顯示從未發生過的進度。
#   - 不得改寫或刪除既有事件行。append-only 是「這次執行到底發生什麼」
#     可被重建的前提；就地修改會讓失敗過程消失。
#   - 不得因為一行壞掉就丟棄整份記錄。壞行略過並計數，
#     否則一個寫到一半的行會讓整次執行看起來沒發生過。
#   - v0.1.0 新增：首版，對應平台化 Phase 5。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_run_progress.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "EVENT_FILENAME",
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "RUN_FAILED",
    "RUN_STARTED",
    "STAGE_COMPLETED",
    "STAGE_FAILED",
    "STAGE_PROGRESS",
    "STAGE_SKIPPED",
    "STAGE_STARTED",
    "RunEvent",
    "RunEventWriter",
    "read_events",
]

EVENT_FILENAME = "stage_events.jsonl"
EVENT_SCHEMA_VERSION = "run_stage_event_v1"

RUN_STARTED = "run_started"
STAGE_STARTED = "stage_started"
STAGE_PROGRESS = "stage_progress"
STAGE_COMPLETED = "stage_completed"
STAGE_FAILED = "stage_failed"
STAGE_SKIPPED = "stage_skipped"
RUN_FAILED = "run_failed"

#: 六種事件。刻意沒有 "run_completed" —— 一次執行是否完成由
#: 各 stage 的終局事件決定，另外寫一個總結事件會產生第二種說法。
EVENT_TYPES: tuple[str, ...] = (
    RUN_STARTED,
    STAGE_STARTED,
    STAGE_PROGRESS,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_SKIPPED,
    RUN_FAILED,
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class RunEvent:
    """一個 stage 事件。"""

    event: str
    stage_id: str = ""
    current: int | None = None
    total: int | None = None
    detail: str = ""
    artifacts: tuple[str, ...] = ()
    at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event": self.event,
            "stage_id": self.stage_id,
            "current": self.current,
            "total": self.total,
            "detail": self.detail,
            "artifacts": list(self.artifacts),
            "at": self.at or _now(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunEvent:
        event = str(data.get("event", ""))
        if event not in EVENT_TYPES:
            raise ValueError(f"unknown run event {event!r}")
        return cls(
            event=event,
            stage_id=str(data.get("stage_id", "")),
            current=data.get("current"),
            total=data.get("total"),
            detail=str(data.get("detail", "")),
            artifacts=tuple(str(a) for a in data.get("artifacts", ())),
            at=str(data.get("at", "")),
        )


class RunEventWriter:
    """把 stage 事件 append 到某個 run 的事件檔。

    **只有 executor 該持有它。** 每一次 write 都開檔、寫入、關檔：
    執行中途被中斷時，已寫出的行仍然完整，而那正是要重建
    「跑到哪裡壞掉」所需要的東西。
    """

    def __init__(self, run_dir: str | Path) -> None:
        self._path = Path(run_dir) / EVENT_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def _append(self, event: RunEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_json(), ensure_ascii=False, sort_keys=True)
        with open(self._path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            # 中斷時未 flush 的行會整行消失；那會讓最後一步看起來沒開始過。
            os.fsync(stream.fileno())

    def run_started(self, detail: str = "") -> None:
        self._append(RunEvent(RUN_STARTED, detail=detail))

    def stage_started(self, stage_id: str, total: int | None = None,
                      detail: str = "") -> None:
        self._append(
            RunEvent(STAGE_STARTED, stage_id=stage_id, total=total, detail=detail)
        )

    def stage_progress(self, stage_id: str, current: int, total: int | None = None,
                       detail: str = "") -> None:
        self._append(
            RunEvent(STAGE_PROGRESS, stage_id=stage_id, current=current,
                     total=total, detail=detail)
        )

    def stage_completed(self, stage_id: str, detail: str = "",
                        artifacts: Iterable[str] = ()) -> None:
        self._append(
            RunEvent(STAGE_COMPLETED, stage_id=stage_id, detail=detail,
                     artifacts=tuple(artifacts))
        )

    def stage_failed(self, stage_id: str, detail: str = "") -> None:
        self._append(RunEvent(STAGE_FAILED, stage_id=stage_id, detail=detail))

    def stage_skipped(self, stage_id: str, detail: str = "") -> None:
        self._append(RunEvent(STAGE_SKIPPED, stage_id=stage_id, detail=detail))

    def run_failed(self, detail: str = "") -> None:
        self._append(RunEvent(RUN_FAILED, detail=detail))


def read_events(run_dir: str | Path) -> tuple[list[RunEvent], int]:
    """讀回事件序列與「略過了幾行壞行」。

    壞行不丟棄整份記錄 —— 一個寫到一半的行（例如執行當下被中斷）
    會讓整次執行看起來沒發生過，而它其實跑了大半。
    """
    path = Path(run_dir) / EVENT_FILENAME
    if not path.is_file():
        return [], 0

    events: list[RunEvent] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(RunEvent.from_json(json.loads(line)))
        except (ValueError, json.JSONDecodeError):
            skipped += 1
    return events, skipped
