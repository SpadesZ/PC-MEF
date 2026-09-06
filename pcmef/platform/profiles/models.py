# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.profiles.registry 與 console 匯入；承載一個 Project
#         底下的 versioned Research Profile。**純資料，不讀檔。**
# 檔案路徑: pcmef/platform/profiles/models.py
# 產生時間: 2026-09-06 20:40 +08:00
# 版本: v0.1.0
# 功能說明: Research Profile 的資料定義與 SAI v0.6.0 §4.1 狀態機。
# 模組定位: 平台化 Phase 3。**Project ≠ Profile**：
#           Project 是研究工作空間（namespace 邊界）；
#           Profile 是該工作空間底下一份**版本化的研究設定**。
#           一個 Project 可以同時有多個 Development Profile 與
#           一個 Frozen Thesis Profile —— 狀態機屬於 Profile，
#           不屬於 Project。
# 主要責任:
#   1. PROFILE_STATES 定義 SAI §4.1 十個狀態與其順序
#   2. Profile 定義版本化研究設定的 metadata
#   3. Profile.is_frozen 判定是否禁止原地修改 scientific fields
#   4. Profile.to_json / from_json 提供 registry 的持久化格式
# 維護提醒:
#   - **不得把狀態機搬回 Project。** Project 是工作空間，它沒有
#     「已凍結」這種性質；被凍結的是某一份 Profile。合併兩者會讓
#     「這個專案已凍結」與「這個專案還有 Development Profile」
#     同時為真而互相矛盾。
#   - 不得允許原地修改 FROZEN Profile 的 scientific fields。
#     研究性變更一律 Clone 成新的 Development Profile（SAI §23）。
#   - 不得把 Formal E2 的 claim 狀態（RESERVED / RUNNING /
#     INTERRUPTED_RESUMABLE / COMPLETE）混進本狀態機。那描述的是
#     一次性執行的佔用，與「這份設定進行到哪」是兩件事，且可同時為真。
#   - v0.1.0 新增：首版，對應平台化 Phase 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_research_design.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "PROFILE_STATES",
    "FROZEN_STATES",
    "Profile",
]

PROFILE_SCHEMA_VERSION = "research_profile_v1"

#: SAI v0.6.0 §4.1 的 Profile 狀態機。順序即進程順序。
PROFILE_STATES: tuple[str, ...] = (
    "DRAFT",
    "CONFIGURED",
    "VALIDATED",
    "PILOT_READY",
    "FREEZE_CANDIDATE",
    "FROZEN",
    "FORMAL_READY",
    "FORMAL_RUNNING",
    "FORMAL_COMPLETE",
    "RUN_INHIBITED",
)

#: 已建立 immutable scientific identity 的狀態。
#:
#: 到達 FROZEN 之後就不得原地改 scientific fields —— 後面的狀態
#: 都在 FROZEN 之上推進，因此它們同樣受這個限制。
FROZEN_STATES: frozenset[str] = frozenset(
    {"FROZEN", "FORMAL_READY", "FORMAL_RUNNING", "FORMAL_COMPLETE"}
)


@dataclass(frozen=True)
class Profile:
    """一個 Project 底下的版本化研究設定。

    scientific identity 不住在這裡：門檻、class order 與 severity 的
    可執行真相在 freeze/ 的 lock。Profile 說的是「這一份設定是誰、
    第幾版、現在什麼狀態、它的研究設計長什麼樣」。
    """

    profile_id: str
    project_id: str
    display_name: str
    version: str = "v1"
    state: str = "DRAFT"
    created_at: str = ""
    #: Clone 來源的 profile_id；None 表示不是複製而來。
    parent_profile_id: str | None = None
    #: 研究設計的來源文件，例如 PC-MEF_實驗計畫_v1.2.1。
    source_document: str = ""
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in PROFILE_STATES:
            raise ValueError(
                f"unknown profile state {self.state!r}; expected one of "
                f"{list(PROFILE_STATES)}"
            )

    @property
    def is_frozen(self) -> bool:
        """已建立 immutable scientific identity，禁止原地修改。"""
        return self.state in FROZEN_STATES

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "project_id": self.project_id,
            "display_name": self.display_name,
            "version": self.version,
            "state": self.state,
            "created_at": self.created_at,
            "parent_profile_id": self.parent_profile_id,
            "source_document": self.source_document,
            "description": self.description,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Profile:
        schema = str(data.get("schema_version", ""))
        if schema != PROFILE_SCHEMA_VERSION:
            raise ValueError(
                f"profile record declares schema_version={schema!r}; this build "
                f"understands {PROFILE_SCHEMA_VERSION!r} only"
            )
        return cls(
            profile_id=str(data["profile_id"]),
            project_id=str(data["project_id"]),
            display_name=str(data.get("display_name", data["profile_id"])),
            version=str(data.get("version", "v1")),
            state=str(data.get("state", "DRAFT")),
            created_at=str(data.get("created_at", "")),
            parent_profile_id=data.get("parent_profile_id"),
            source_document=str(data.get("source_document", "")),
            description=str(data.get("description", "")),
            extra=dict(data.get("extra", {})),
        )
