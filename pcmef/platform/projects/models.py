# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.projects.resolver 與 platform.projects.registry 匯入；
#         console 與 cli 取得的 Project / ProjectPaths 都是這裡的型別。
#         **純資料，不讀檔、不寫檔、不解析路徑。**
# 檔案路徑: pcmef/platform/projects/models.py
# 產生時間: 2026-09-06 14:15 +08:00
# 版本: v0.2.0
# 功能說明: Project（研究專案）與 ProjectPaths（專案路徑集合）的資料定義。
# 模組定位: 平台化後「一個研究專案是什麼」的單一定義處。
#           **Project ≠ Profile**：Project 是研究工作空間（namespace
#           邊界），它底下可以有多個版本化的 Research Profile。
#           SAI §4.1 的狀態機屬於 Profile，見 platform/profiles/models.py
#           —— 一個工作空間沒有「已凍結」這種性質，被凍結的是其中
#           某一份設定。
# 主要責任:
#   1. ProjectPaths 定義一個專案的七個根目錄
#   2. Project 定義專案 metadata 與其序列化形狀
#   3. Project.from_json / to_json 提供 registry 的持久化格式
# 維護提醒:
#   - 不得在本檔判斷 legacy thesis 的特殊路徑。路徑解析一律由 resolver
#     負責；這裡若也判一次，特殊情況就有了第二個落點，而第二個落點
#     總是後來才被想起來要改的那一個。
#   - 不得把 scientific 欄位（class order、severity、threshold）放進 Project。
#     那些的身分來源是 freeze/ 的 lock，不是專案 metadata。
#   - **不得把 Profile 的狀態機加回 Project。** 曾經有過一版把
#     SAI §4.1 的十個狀態放在 Project 上，那會讓「這個專案已凍結」與
#     「這個專案還有 Development Profile」同時為真而互相矛盾。
#     狀態屬於 Profile。
#   - v0.1.0 新增：首版，對應平台化 Phase 1。
#   - v0.2.0 變更：移除 Project.state 與 PROJECT_STATES，狀態機
#     改由 profiles.models 承載，對應平台化 Phase 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_paths.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "PROJECT_SCHEMA_VERSION",
    "Project",
    "ProjectPaths",
]

PROJECT_SCHEMA_VERSION = "project_v1"


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

    **這裡沒有任何科學參數，也沒有狀態機。** 專案說明自己是什麼、
    從哪個模板來、是否封存；它有哪些 class、哪些門檻、哪一組 lock，
    要問 freeze/ 底下解析出來的 lineage；它進行到哪一個階段，
    要問它底下的 Research Profile。
    """

    project_id: str
    display_name: str
    template: str
    created_at: str = ""
    #: Clone 來源。None 表示不是從別的專案複製而來。
    parent_project_id: str | None = None
    #: 封存的專案仍可讀，但不出現在預設清單、也不得啟動執行。
    archived: bool = False
    #: legacy 專案的科學資料不在 projects/<id>/ 底下。
    #: 這個旗標**只由 resolver 設定**，UI 用它顯示「路徑在哪」。
    legacy_layout: bool = False
    #: 這個 Project 預設要顯示哪一份 Research Profile。
    #:
    #: **明確宣告，不是「取第一筆」。** 沒有宣告時畫面說「尚未選擇」，
    #: 而不是隨便挑一份 —— 隨便挑的那份會被當成這個研究的正式設定。
    default_profile_id: str | None = None
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": self.project_id,
            "display_name": self.display_name,
            "template": self.template,
            "created_at": self.created_at,
            "parent_project_id": self.parent_project_id,
            "archived": self.archived,
            "legacy_layout": self.legacy_layout,
            "default_profile_id": self.default_profile_id,
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
            created_at=str(data.get("created_at", "")),
            parent_project_id=data.get("parent_project_id"),
            archived=bool(data.get("archived", False)),
            legacy_layout=bool(data.get("legacy_layout", False)),
            default_profile_id=data.get("default_profile_id"),
            description=str(data.get("description", "")),
            extra=dict(data.get("extra", {})),
        )
