# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.lifecycle.providers 與 pcmef 建構；console 的 Status
#         頁渲染本檔的結構。**純資料，不讀檔、不判定科學狀態。**
# 檔案路徑: pcmef/platform/lifecycle/models.py
# 產生時間: 2026-09-07 11:10 +08:00
# 版本: v0.1.0
# 功能說明: Research Lifecycle 的 Stage / Gate 資料結構與狀態推導規則。
# 模組定位: 平台化 Phase 4。Lifecycle 是 **generic** 的：
#           Project → Profile → Lifecycle → Stage → Gates。
#           哪些 gate 掛在哪一個 stage 由 provider 決定，
#           PC-MEF 專屬的判定不進本檔。
# 主要責任:
#   1. GATE_STATES 定義五種狀態與其嚴重度順序
#   2. Gate 承載單一判定與「為什麼」
#   3. Stage 由其 gates 推導狀態，不自行宣告
#   4. LifecycleView 回答「在哪一階段、下一步、被什麼擋住」
# 維護提醒:
#   - **不得讓 Stage 自行宣告狀態。** 狀態一律由 gates 推導；
#     手寫的 stage 狀態會與 gate 判定分歧，而分歧時畫面看起來仍然正常。
#   - 不得把「沒有 gate」當成 COMPLETE。沒有判定就是 NOT_STARTED ——
#     否則一個尚未定義任何 gate 的 stage 會顯示成已完成。
#   - 不得在本檔加入 PC-MEF 專屬字樣。Blank Project 也會渲染這些結構。
#   - v0.1.0 新增：首版，對應平台化 Phase 4。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_lifecycle.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "GATE_STATES",
    "BLOCKED",
    "COMPLETE",
    "IN_PROGRESS",
    "NOT_STARTED",
    "PASS",
    "Gate",
    "Stage",
    "LifecycleView",
]

NOT_STARTED = "NOT_STARTED"
IN_PROGRESS = "IN_PROGRESS"
PASS = "PASS"
BLOCKED = "BLOCKED"
COMPLETE = "COMPLETE"

#: 五種狀態。順序即「該先看哪一個」——畫面上 BLOCKED 要壓過 IN_PROGRESS。
GATE_STATES: tuple[str, ...] = (NOT_STARTED, IN_PROGRESS, BLOCKED, PASS, COMPLETE)

_SATISFIED = frozenset({PASS, COMPLETE})


@dataclass(frozen=True)
class Gate:
    """一項判定。

    `project_specific` 標出這是某個研究專案自己的判準（例如 PC-MEF 的
    E1-G08），而不是平台共有的東西 —— 使用者必須分得出「這是這個研究
    的規矩」與「這是系統的規矩」。
    """

    gate_id: str
    label: str
    status: str = NOT_STARTED
    detail: str = ""
    why: str = ""
    project_specific: bool = True

    def __post_init__(self) -> None:
        if self.status not in GATE_STATES:
            raise ValueError(
                f"unknown gate status {self.status!r}; expected one of "
                f"{list(GATE_STATES)}"
            )

    @property
    def satisfied(self) -> bool:
        return self.status in _SATISFIED

    def to_json(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id, "label": self.label, "status": self.status,
            "detail": self.detail, "why": self.why,
            "project_specific": self.project_specific,
        }


@dataclass(frozen=True)
class Stage:
    """研究階段。狀態**由 gates 推導**，不自行宣告。"""

    stage_id: str
    title: str
    summary: str = ""
    gates: tuple[Gate, ...] = ()

    @property
    def status(self) -> str:
        """由 gates 推導。

        沒有 gate 就是 NOT_STARTED，**不是 COMPLETE** —— 一個還沒定義
        任何判定的階段不該顯示成已完成。
        """
        if not self.gates:
            return NOT_STARTED
        if all(g.satisfied for g in self.gates):
            return COMPLETE
        if any(g.status == BLOCKED for g in self.gates):
            return BLOCKED
        if any(g.satisfied or g.status == IN_PROGRESS for g in self.gates):
            return IN_PROGRESS
        return NOT_STARTED

    @property
    def cleared(self) -> int:
        return sum(1 for g in self.gates if g.satisfied)

    @property
    def blockers(self) -> tuple[Gate, ...]:
        return tuple(g for g in self.gates if g.status == BLOCKED)

    def to_json(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id, "title": self.title,
            "summary": self.summary, "status": self.status,
            "cleared": self.cleared, "total": len(self.gates),
            "gates": [g.to_json() for g in self.gates],
        }


@dataclass(frozen=True)
class LifecycleView:
    """一份 Profile 的完整 lifecycle 檢視。"""

    stages: tuple[Stage, ...] = ()
    #: 這份 lifecycle 由誰產生，讓畫面能說明判定來源。
    provider: str = ""
    note: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def current(self) -> Stage | None:
        """第一個尚未完成的階段 —— 也就是「現在做到哪」。"""
        for stage in self.stages:
            if stage.status != COMPLETE:
                return stage
        return None

    @property
    def next_stage(self) -> Stage | None:
        """current 之後的下一個階段。"""
        current = self.current
        if current is None:
            return None
        seen = False
        for stage in self.stages:
            if seen:
                return stage
            if stage.stage_id == current.stage_id:
                seen = True
        return None

    @property
    def blockers(self) -> tuple[Gate, ...]:
        """目前階段擋住的判定。只看 current —— 後面階段的 gate 還沒輪到。"""
        current = self.current
        return current.blockers if current else ()

    @property
    def completed(self) -> tuple[Stage, ...]:
        return tuple(s for s in self.stages if s.status == COMPLETE)

    def to_json(self) -> dict[str, Any]:
        current = self.current
        nxt = self.next_stage
        return {
            "provider": self.provider,
            "note": self.note,
            "stages": [s.to_json() for s in self.stages],
            "current_stage_id": current.stage_id if current else None,
            "next_stage_id": nxt.stage_id if nxt else None,
            "blockers": [g.to_json() for g in self.blockers],
            "completed_count": len(self.completed),
            "total_stages": len(self.stages),
        }
