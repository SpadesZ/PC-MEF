# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.projects.resolver 與 platform.projects.registry 匯入；
#         console 與 cli 取得的 Project / ProjectPaths 都是這裡的型別。
#         **純資料，不讀檔、不寫檔、不解析路徑。**
# 檔案路徑: pcmef/platform/projects/models.py
# 產生時間: 2026-09-06 14:15 +08:00
# 版本: v0.1.0
# 功能說明: Project（研究專案）與 ProjectPaths（專案路徑集合）的資料定義，
#           以及 SAI v0.6.0 §4.1 的 Profile 狀態機常數。
# 模組定位: 平台化後「一個研究專案是什麼」的單一定義處。
#           SAI 稱之為 Research Profile，對外顯示統一用 Project；
#           `state` 欄位仍沿用 SAI §4.1 的狀態名稱，讓既有規格可直接引用。
#           **不同時維護兩套詞彙 —— 兩套詞彙必然漂移。**
# 主要責任:
#   1. PROJECT_STATES 定義 SAI §4.1 十個狀態與其順序
#   2. ProjectPaths 定義一個專案的七個根目錄
#   3. Project 定義專案 metadata 與其序列化形狀
#   4. Project.from_json / to_json 提供 registry 的持久化格式
# 維護提醒:
#   - 不得在本檔判斷 legacy thesis 的特殊路徑。路徑解析一律由 resolver
#     負責；這裡若也判一次，特殊情況就有了第二個落點，而第二個落點
#     總是後來才被想起來要改的那一個。
#   - 不得把 scientific 欄位（class order、severity、threshold）放進 Project。
#     那些的身分來源是 freeze/ 的 lock，不是專案 metadata。
#   - v0.1.0 新增：首版，對應平台化 Phase 1。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_paths.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "PROJECT_STATES",
    "PROJECT_SCHEMA_VERSION",
    "Project",
    "ProjectPaths",
]

PROJECT_SCHEMA_VERSION = "project_v1"

#: SAI v0.6.0 §4.1 的狀態機。順序即進程順序。
#:
#: 這一組刻意與 Formal E2 的 claim 狀態（RESERVED / RUNNING /
#: INTERRUPTED_RESUMABLE / COMPLETE）**分開**：前者描述「這個研究專案的
#: 設定進行到哪」，後者描述「那一次性正式執行的佔用狀態」。
#: 合併兩者會讓「專案已凍結」與「有人正在跑」變成同一個欄位，
#: 而它們可以、也應該同時為真。
PROJECT_STATES: tuple[str, ...] = (
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


@dataclass(frozen=True)
class ProjectPaths:
    """一個專案的所有根目錄。**由 resolver 產生，不自行組裝。**

    `metadata_root` 與其他六個刻意分開：metadata 一律住在
    `projects/<id>/`，而科學資料的位置由 resolver 決定 ——
    legacy Thesis Project 的 freeze/outputs/configs 仍在 repo 根目錄，
    一個位元組都不搬。
    """

    project_id: str
    #: 專案 metadata 的位置。**所有專案都一樣**：projects/<id>/
    metadata_root: Path
    #: 以下六個是科學資料的位置。legacy 專案指向 repo 根目錄。
    freeze: Path
    outputs: Path
    configs: Path
    datasets: Path
    runs: Path
    artifacts: Path

    def as_dict(self) -> dict[str, str]:
        """給 UI 與 run manifest 用的形狀。用 posix 以免跨平台比對失敗。"""
        return {
            "project_id": self.project_id,
            "metadata_root": self.metadata_root.as_posix(),
            "freeze": self.freeze.as_posix(),
            "outputs": self.outputs.as_posix(),
            "configs": self.configs.as_posix(),
            "datasets": self.datasets.as_posix(),
            "runs": self.runs.as_posix(),
            "artifacts": self.artifacts.as_posix(),
        }


@dataclass(frozen=True)
class Project:
    """一個研究專案的 metadata。

    **這裡沒有任何科學參數。** 專案說明自己是什麼、從哪個模板來、
    現在什麼狀態；它有哪些 class、哪些門檻、哪一組 lock，
    要問 freeze/ 底下解析出來的 lineage。
    """

    project_id: str
    display_name: str
    template: str
    state: str = "DRAFT"
    created_at: str = ""
    #: Clone 來源。None 表示不是從別的專案複製而來。
    parent_project_id: str | None = None
    #: 封存的專案仍可讀，但不出現在預設清單、也不得啟動執行。
    archived: bool = False
    #: legacy 專案的科學資料不在 projects/<id>/ 底下。
    #: 這個旗標**只由 resolver 設定**，UI 用它顯示「路徑在哪」。
    legacy_layout: bool = False
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in PROJECT_STATES:
            raise ValueError(
                f"unknown project state {self.state!r}; expected one of "
                f"{list(PROJECT_STATES)}"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": self.project_id,
            "display_name": self.display_name,
            "template": self.template,
            "state": self.state,
            "created_at": self.created_at,
            "parent_project_id": self.parent_project_id,
            "archived": self.archived,
            "legacy_layout": self.legacy_layout,
            "description": self.description,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Project:
        schema = str(data.get("schema_version", ""))
        if schema != PROJECT_SCHEMA_VERSION:
            raise ValueError(
                f"project record declares schema_version={schema!r}; this build "
                f"understands {PROJECT_SCHEMA_VERSION!r} only"
            )
        return cls(
            project_id=str(data["project_id"]),
            display_name=str(data.get("display_name", data["project_id"])),
            template=str(data.get("template", "blank")),
            state=str(data.get("state", "DRAFT")),
            created_at=str(data.get("created_at", "")),
            parent_project_id=data.get("parent_project_id"),
            archived=bool(data.get("archived", False)),
            legacy_layout=bool(data.get("legacy_layout", False)),
            description=str(data.get("description", "")),
            extra=dict(data.get("extra", {})),
        )
