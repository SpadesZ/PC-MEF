# PC-MEF Research System source maintenance contract
# 上下游: 由 console 各 route、cli 與 platform.projects.__init__ 使用；
#         讀寫 projects/<id>/project.json，路徑一律向 resolver 索取。
#         **不解析路徑，也不判斷 legacy 特例。**
# 檔案路徑: pcmef/platform/projects/registry.py
# 產生時間: 2026-09-06 14:50 +08:00
# 版本: v0.1.0
# 功能說明: 專案清單、建立、開啟、封存與 Clone，以及既有 PC-MEF 的
#           自動遷移（只寫 metadata，不動任何科學資料）。
# 模組定位: 平台化 Phase 1。SAI v0.6.0 §24 Clone workflow 與 §23
#           immutable/mutable 政策在程式層的落點。
# 主要責任:
#   1. list_projects() / get() 列出與讀取專案
#   2. create() 以 O_EXCL 建立專案，競態時只有一個成功
#   3. archive() / unarchive() 切換封存狀態（**不是**科學狀態機）
#   3b. set_default_profile() 明確宣告預設 Research Profile
#   4. clone() 依 allowlist 複製設計，**拒絕複製科學結果**
#   5. ensure_legacy_thesis_project() 冪等地把既有 PC-MEF 登記為第一個專案
# 維護提醒:
#   - **Clone 不得複製 freeze、outputs、runs、artifacts 或 one-shot claim。**
#     複製 frozen evidence 會讓兩個專案共用同一份 mutable scientific
#     identity，屆時沒有人能說出某個結果屬於哪一個專案。
#   - 不得用「先檢查再建立」取代 O_EXCL。兩個請求可以同時通過檢查，
#     然後其中一個覆寫另一個剛寫好的專案。
#   - 不得讓 ensure_legacy_thesis_project() 建立或搬動任何科學資料目錄。
#     它只寫 metadata；既有 freeze/ 一個位元組都不動。
#   - v0.1.0 新增：首版，對應平台化 Phase 1。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.platform.capabilities import (
    CAPABILITY_FIELD,
    FORMAL_E2,
    LLM_RUNTIME_FREEZE,
    LLM_SNAPSHOT_READ,
    SCIENTIFIC_STATE_READ,
)
from pcmef.platform.projects.models import Project
from pcmef.platform.projects.resolver import (
    LEGACY_THESIS_PROJECT_ID,
    PROJECTS_DIRNAME,
    resolve_paths,
    validate_project_id,
    workspace_root,
)

__all__ = [
    "PROJECT_FILENAME",
    "CLONE_COPYABLE",
    "CLONE_NEVER_COPY",
    "LEGACY_THESIS_CAPABILITIES",
    "PROJECT_DATA_DIRS",
    "ProjectExistsError",
    "ProjectNotFoundError",
    "ProjectRegistry",
]

PROJECT_FILENAME = "project.json"

#: legacy Thesis 專案在遷移時取得的能力。
#:
#: 從 capabilities 模組取常數而不是寫字面字串：字面字串打錯不會報錯，
#: 只會安靜地少一項能力，而少的那一項要等到有人按下按鈕才會發現。
LEGACY_THESIS_CAPABILITIES: tuple[str, ...] = (
    FORMAL_E2,
    LLM_RUNTIME_FREEZE,
    LLM_SNAPSHOT_READ,
    SCIENTIFIC_STATE_READ,
)

#: Clone 會複製的 metadata。**設計，不是結果。**
CLONE_COPYABLE: tuple[str, ...] = (
    "research_design.json",
    "pipeline.json",
    "lifecycle.json",
)

#: Clone **永遠不碰**的目錄。這一組是科學身分所在。
#:
#: 複製它們會製造出兩個宣稱擁有同一份 frozen evidence 的專案，
#: 而 frozen evidence 的意義正建立在「只有一份」上。
CLONE_NEVER_COPY: tuple[str, ...] = (
    "freeze",
    "outputs",
    "runs",
    "artifacts",
    "datasets",
)

#: 建立新專案時要備妥的資料目錄。
#:
#: 內容目前與 CLONE_NEVER_COPY 相同，但**刻意分成兩個常數**：
#: 一個回答「建立時要開哪些目錄」，另一個回答「Clone 時不准碰哪些」。
#: 合成一個的話，日後新增一個「可以複製的資料目錄」就會同時放寬
#: Clone 的禁令，而那是靜默的。
PROJECT_DATA_DIRS: tuple[str, ...] = (
    "freeze",
    "outputs",
    "runs",
    "artifacts",
    "datasets",
)


class ProjectExistsError(RuntimeError):
    """專案已存在。**不覆寫。**"""


class ProjectNotFoundError(KeyError):
    """找不到專案。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ProjectRegistry:
    """專案登記處。所有路徑向 resolver 索取，本檔不自行組裝。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = workspace_root(root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def projects_dir(self) -> Path:
        return self._root / PROJECTS_DIRNAME

    def _record_path(self, project_id: str) -> Path:
        return resolve_paths(project_id, root=self._root).metadata_root / PROJECT_FILENAME

    # -- 讀 -----------------------------------------------------------------

    def exists(self, project_id: str) -> bool:
        return self._record_path(project_id).exists()

    def get(self, project_id: str) -> Project:
        path = self._record_path(project_id)
        if not path.exists():
            raise ProjectNotFoundError(
                f"no project {project_id!r} at {path.as_posix()}"
            )
        return Project.from_json(json.loads(path.read_text(encoding="utf-8")))

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        """依 display_name 排序的專案清單。

        壞掉的紀錄**不靜默略過**：它會以 DRAFT/`<unreadable>` 出現，
        因為一個讀不出來的專案與一個不存在的專案是兩件事。
        """
        if not self.projects_dir.is_dir():
            return []
        projects: list[Project] = []
        for entry in sorted(self.projects_dir.iterdir()):
            record = entry / PROJECT_FILENAME
            if not record.is_file():
                continue
            try:
                project = Project.from_json(
                    json.loads(record.read_text(encoding="utf-8"))
                )
            except (ValueError, json.JSONDecodeError):
                project = Project(
                    project_id=entry.name,
                    display_name=f"{entry.name} <unreadable>",
                    template="unknown",
                    description="project.json could not be parsed",
                )
            if project.archived and not include_archived:
                continue
            projects.append(project)
        return sorted(projects, key=lambda p: (p.archived, p.display_name.lower()))

    # -- 寫 -----------------------------------------------------------------

    def _write_new(self, project: Project) -> None:
        """以 O_EXCL 建立。已存在即 ProjectExistsError。

        「先檢查再建立」會讓兩個同時發出的請求都通過檢查，
        然後其中一個覆寫另一個 —— 而且沒有任何一方知道對方存在。
        """
        path = self._record_path(project.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(project.to_json(), indent=2, ensure_ascii=False)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ProjectExistsError(
                f"project {project.project_id!r} already exists at "
                f"{path.as_posix()}; refusing to overwrite it"
            ) from None
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")

    def _write_existing(self, project: Project) -> None:
        """原子覆寫：先寫暫存檔再 replace，避免半份 JSON。"""
        path = self._record_path(project.project_id)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(project.to_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)

    def create(
        self,
        project_id: str,
        display_name: str,
        *,
        template: str = "blank",
        description: str = "",
        parent_project_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Project:
        """建立新專案並備妥其目錄。"""
        validate_project_id(project_id)
        project = Project(
            project_id=project_id,
            display_name=display_name or project_id,
            template=template,
            created_at=_now(),
            parent_project_id=parent_project_id,
            legacy_layout=False,
            description=description,
            extra=extra or {},
        )
        self._write_new(project)
        paths = resolve_paths(project_id, root=self._root)
        for field in PROJECT_DATA_DIRS:
            getattr(paths, field).mkdir(parents=True, exist_ok=True)
        return project

    def set_default_profile(
        self, project_id: str, profile_id: str | None
    ) -> Project:
        """宣告這個 Project 預設顯示哪一份 Profile。

        明確 metadata，不是排序副作用 —— 新增一份名稱較前的 Profile
        不該悄悄改變畫面上顯示的研究設定。
        """
        updated = replace(self.get(project_id), default_profile_id=profile_id)
        self._write_existing(updated)
        return updated

    def archive(self, project_id: str) -> Project:
        updated = replace(self.get(project_id), archived=True)
        self._write_existing(updated)
        return updated

    def unarchive(self, project_id: str) -> Project:
        updated = replace(self.get(project_id), archived=False)
        self._write_existing(updated)
        return updated

    def clone(
        self,
        source_id: str,
        new_id: str,
        display_name: str,
        *,
        description: str = "",
    ) -> Project:
        """複製設計，**不複製任何科學結果**。

        Profile 的狀態不在 Project 上，因此這裡沒有「重設狀態」這件事；
        科學身分的繼承與否由 profiles.registry.clone() 決定，
        它一律把新的 Profile 建成 DRAFT。
        """
        source = self.get(source_id)
        created = self.create(
            new_id,
            display_name,
            template=source.template,
            description=description or source.description,
            parent_project_id=source_id,
        )

        source_paths = resolve_paths(source_id, root=self._root)
        target_paths = resolve_paths(new_id, root=self._root)
        for filename in CLONE_COPYABLE:
            origin = source_paths.metadata_root / filename
            if origin.is_file():
                (target_paths.metadata_root / filename).write_bytes(
                    origin.read_bytes()
                )
        return created

    # -- 遷移 ---------------------------------------------------------------

    def ensure_legacy_thesis_project(
        self, *, display_name: str = "PC-MEF Thesis"
    ) -> Project:
        """把既有 PC-MEF 登記為第一個專案。**冪等，且只寫 metadata。**

        既有 freeze/、outputs/、configs/ 完全不動 —— 這個方法建立的
        只有 projects/pcmef-thesis/project.json。科學資料的位置由
        resolver 指回 repo 根目錄。

        紀錄已存在時仍會**補寫缺少的能力宣告**：能力欄位是後來才加的，
        在那之前寫下的 project.json 沒有它，於是碩論專案自己失去
        formal_e2 與 llm_runtime_freeze —— 畫面會說「這個專案沒有
        Formal E2 能力」，而它正是那篇論文。
        """
        if self.exists(LEGACY_THESIS_PROJECT_ID):
            return self._backfill_capabilities(self.get(LEGACY_THESIS_PROJECT_ID))

        project = Project(
            project_id=LEGACY_THESIS_PROJECT_ID,
            display_name=display_name,
            # 論文模板與 legacy 專案共用同一個識別碼：第一個專案就是
            # 這個模板的來源。刻意不另寫一次字面值 —— 重複的字面值
            # 正是 test_legacy_special_case_lives_only_in_the_resolver 要擋的。
            # Phase 7 建立 template registry 後，此處改為引用其常數。
            template=LEGACY_THESIS_PROJECT_ID,
            created_at=_now(),
            legacy_layout=True,
            description=(
                "既有 PC-MEF 碩論研究。科學資料仍位於 repo 根目錄，"
                "平台化未搬動任何已凍結的 artifact。"
            ),
            # 能力在遷移時**明確宣告**。之後 capabilities_for() 只讀這份
            # 宣告，不再從 project id 推導 —— 用 id 推導與用名字判斷資格
            # 只差一層。
            extra={CAPABILITY_FIELD: list(LEGACY_THESIS_CAPABILITIES)},
        )
        try:
            self._write_new(project)
        except ProjectExistsError:
            # 競態：另一個行程剛建立。讀它的即可，兩份內容相同。
            return self._backfill_capabilities(self.get(LEGACY_THESIS_PROJECT_ID))
        return project

    def _backfill_capabilities(self, project: Project) -> Project:
        """把缺少的能力宣告補進既有紀錄。**冪等，且只補這一欄。**

        只在缺的時候寫檔：每次讀取都覆寫會讓 mtime 一直跳動，而且把
        「補一次遷移」變成「每次開頁都改資料」。其他欄位一律原樣留著 ——
        使用者改過的顯示名稱不該被遷移改回去。

        寫檔失敗即 **fail-closed**：回傳未修改的專案。細節見下方註解。
        """
        declared = tuple((project.extra or {}).get(CAPABILITY_FIELD) or ())
        missing = [c for c in LEGACY_THESIS_CAPABILITIES if c not in declared]
        if not missing:
            return project

        extra = dict(project.extra or {})
        extra[CAPABILITY_FIELD] = [*declared, *missing]
        updated = replace(project, extra=extra)
        try:
            self._write_existing(updated)
        except OSError:
            # **寫不進去就不算授權。**
            #
            # 先前這裡吞掉錯誤並回傳補好的物件，理由是「這一次請求該有
            # 的能力是對的」。那個理由站不住：能力是**宣告出來的資料**，
            # 而磁碟上並沒有這份宣告。結果是同一個專案這一次有
            # formal_e2、下一次沒有，取決於哪一個行程剛好試寫成功 ——
            # 而 formal_e2 開的是一次性正式實驗。
            #
            # 回傳未修改的那一份：畫面會說這個專案沒有 Formal 能力，
            # 那正是磁碟上的事實。修好權限之後下一次自然會補上。
            return project
        return updated
