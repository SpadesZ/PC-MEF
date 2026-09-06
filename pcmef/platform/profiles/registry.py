# PC-MEF Research System source maintenance contract
# 上下游: 由 console 的 Research Design 頁與 platform.projects 的遷移流程
#         使用；讀寫 projects/<id>/profiles/<profile_id>/。
#         路徑一律向 projects.resolver 索取。
# 檔案路徑: pcmef/platform/profiles/registry.py
# 產生時間: 2026-09-06 20:55 +08:00
# 版本: v0.1.0
# 功能說明: 一個 Project 底下 Research Profile 的列出、建立、Clone、
#           狀態推進與 Research Design 讀寫，以及 Thesis Profile 的遷移。
# 模組定位: 平台化 Phase 3。**Project ≠ Profile**：本檔管的是
#           Project 底下那一層，因此每個方法都要求 project_id。
# 主要責任:
#   1. list_profiles() / get() 列出與讀取某 Project 的 Profile
#   2. create() 以 O_EXCL 建立 Profile
#   3. clone() Frozen Profile → 新的 Development Profile
#   4. read_design() / write_design() 讀寫 Research Design
#   5. ensure_thesis_profile() 冪等地建立 Frozen Thesis Profile 與其設計
# 維護提醒:
#   - **不得允許原地覆寫 FROZEN Profile 的 Research Design。**
#     研究性變更一律 Clone 成新的 Development Profile（SAI §23/§24）。
#     覆寫會讓已發表結果對應的設計悄悄變成另一份。
#   - 不得用「先檢查再建立」取代 O_EXCL；兩個請求可以同時通過檢查。
#   - 不得讓 Clone 帶走 run history、frozen evidence 或 one-shot claim。
#     那些屬於執行，不屬於設計。
#   - v0.1.0 新增：首版，對應平台化 Phase 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_profile_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.platform.profiles.design import ResearchDesign
from pcmef.platform.profiles.models import Profile
from pcmef.platform.projects.resolver import (
    LEGACY_THESIS_PROJECT_ID,
    resolve_paths,
    validate_project_id,
    workspace_root,
)

__all__ = [
    "PROFILE_FILENAME",
    "DESIGN_FILENAME",
    "THESIS_PROFILE_ID",
    "FrozenProfileError",
    "ProfileExistsError",
    "ProfileNotFoundError",
    "ProfileRegistry",
]

PROFILE_FILENAME = "profile.json"
DESIGN_FILENAME = "research_design.json"

#: 碩論的 Frozen Profile。與 Project id 分開：Project 是工作空間，
#: 這是它底下那一份已凍結的研究設定。
THESIS_PROFILE_ID = "thesis-frozen"


class ProfileExistsError(RuntimeError):
    """Profile 已存在。**不覆寫。**"""


class ProfileNotFoundError(KeyError):
    """找不到 Profile。"""


class FrozenProfileError(RuntimeError):
    """試圖原地修改已凍結的 Profile。**一律拒絕，改用 Clone。**"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _profile_id_ok(profile_id: str) -> str:
    # 與 project_id 同一組規則：可安全用作目錄名，且拒絕而非清洗。
    validate_project_id(profile_id)
    return profile_id


class ProfileRegistry:
    """某個 workspace 裡所有 Project 的 Profile 登記處。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = workspace_root(root)

    @property
    def root(self) -> Path:
        return self._root

    def profiles_dir(self, project_id: str) -> Path:
        return resolve_paths(project_id, root=self._root).metadata_root / "profiles"

    def _dir(self, project_id: str, profile_id: str) -> Path:
        _profile_id_ok(profile_id)
        return self.profiles_dir(project_id) / profile_id

    # -- 讀 -----------------------------------------------------------------

    def exists(self, project_id: str, profile_id: str) -> bool:
        return (self._dir(project_id, profile_id) / PROFILE_FILENAME).exists()

    def get(self, project_id: str, profile_id: str) -> Profile:
        path = self._dir(project_id, profile_id) / PROFILE_FILENAME
        if not path.exists():
            raise ProfileNotFoundError(
                f"no profile {profile_id!r} in project {project_id!r}"
            )
        return Profile.from_json(json.loads(path.read_text(encoding="utf-8")))

    def list_profiles(self, project_id: str) -> list[Profile]:
        """依「先 Frozen 後 Development、再依名稱」排序。

        Frozen 排前面是因為它是這個 Project 的科學身分所在；
        Development Profile 是它的衍生物。
        """
        base = self.profiles_dir(project_id)
        if not base.is_dir():
            return []
        found: list[Profile] = []
        for entry in sorted(base.iterdir()):
            record = entry / PROFILE_FILENAME
            if not record.is_file():
                continue
            try:
                found.append(
                    Profile.from_json(json.loads(record.read_text(encoding="utf-8")))
                )
            except (ValueError, json.JSONDecodeError):
                found.append(
                    Profile(
                        profile_id=entry.name,
                        project_id=project_id,
                        display_name=f"{entry.name} <unreadable>",
                        description="profile.json could not be parsed",
                    )
                )
        return sorted(
            found, key=lambda p: (not p.is_frozen, p.display_name.lower())
        )

    def active_profile(self, project_id: str) -> Profile | None:
        """這個 Project 的代表性 Profile。

        優先 Frozen —— 它是科學身分所在。沒有 Frozen 時取第一個，
        沒有任何 Profile 時回 None（而不是憑空造一個）。
        """
        profiles = self.list_profiles(project_id)
        return profiles[0] if profiles else None

    # -- 寫 -----------------------------------------------------------------

    def create(
        self,
        project_id: str,
        profile_id: str,
        display_name: str,
        *,
        version: str = "v1",
        state: str = "DRAFT",
        source_document: str = "",
        description: str = "",
        parent_profile_id: str | None = None,
    ) -> Profile:
        profile = Profile(
            profile_id=_profile_id_ok(profile_id),
            project_id=project_id,
            display_name=display_name or profile_id,
            version=version,
            state=state,
            created_at=_now(),
            parent_profile_id=parent_profile_id,
            source_document=source_document,
            description=description,
        )
        target = self._dir(project_id, profile_id)
        target.mkdir(parents=True, exist_ok=True)
        path = target / PROFILE_FILENAME
        payload = json.dumps(profile.to_json(), indent=2, ensure_ascii=False)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ProfileExistsError(
                f"profile {profile_id!r} already exists in project {project_id!r}"
            ) from None
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
        return profile

    def set_state(self, project_id: str, profile_id: str, state: str) -> Profile:
        updated = replace(self.get(project_id, profile_id), state=state)
        path = self._dir(project_id, profile_id) / PROFILE_FILENAME
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(updated.to_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
        return updated

    # -- Research Design ----------------------------------------------------

    def read_design(self, project_id: str, profile_id: str) -> ResearchDesign | None:
        path = self._dir(project_id, profile_id) / DESIGN_FILENAME
        if not path.is_file():
            return None
        return ResearchDesign.from_json(json.loads(path.read_text(encoding="utf-8")))

    def write_design(
        self, project_id: str, profile_id: str, design: ResearchDesign
    ) -> None:
        """寫入 Research Design。**FROZEN Profile 一律拒絕。**

        已凍結的設定若能被原地改寫，已發表結果所對應的設計就會悄悄
        變成另一份，而讀者手上的論文不會跟著改。
        """
        profile = self.get(project_id, profile_id)
        if profile.is_frozen:
            raise FrozenProfileError(
                f"profile {profile_id!r} is {profile.state}; its research design "
                "cannot be edited in place. Clone it into a development profile "
                "instead (SAI v0.6.0 §23/§24)."
            )
        target = self._dir(project_id, profile_id)
        target.mkdir(parents=True, exist_ok=True)
        (target / DESIGN_FILENAME).write_text(
            json.dumps(design.to_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # -- Clone --------------------------------------------------------------

    def clone(
        self,
        project_id: str,
        source_profile_id: str,
        new_profile_id: str,
        display_name: str,
        *,
        target_project_id: str | None = None,
    ) -> Profile:
        """Frozen Profile → 新的 Development Profile。

        複製研究設計，**不複製任何執行結果**。新 Profile 一律 DRAFT：
        它還沒有自己的 lock，直接繼承 FROZEN 會讓一份空設定宣稱
        自己已建立科學身分。
        """
        source = self.get(project_id, source_profile_id)
        destination = target_project_id or project_id
        created = self.create(
            destination,
            new_profile_id,
            display_name,
            version=f"{source.version}-clone",
            state="DRAFT",
            source_document=source.source_document,
            description=source.description,
            parent_profile_id=source_profile_id,
        )
        design = self.read_design(project_id, source_profile_id)
        if design is not None:
            # 走底層寫入：新 Profile 是 DRAFT，write_design 不會擋，
            # 但這裡直接寫可避免再讀一次剛建立的紀錄。
            target = self._dir(destination, new_profile_id)
            (target / DESIGN_FILENAME).write_text(
                json.dumps(design.to_json(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return created

    # -- 遷移 ---------------------------------------------------------------

    def ensure_thesis_profile(self) -> Profile:
        """把 v1.2.1 的研究設計建成 Thesis Project 的 Frozen Profile。

        **冪等。** 已存在就直接回傳，不覆寫 —— 覆寫一份 FROZEN 設計
        正是 write_design 擋下的事。
        """
        from pcmef.platform.profiles.thesis import (
            SOURCE_DOCUMENT,
            THESIS_DESIGN,
            THESIS_TITLE,
        )

        project_id = LEGACY_THESIS_PROJECT_ID
        if self.exists(project_id, THESIS_PROFILE_ID):
            return self.get(project_id, THESIS_PROFILE_ID)

        try:
            profile = self.create(
                project_id,
                THESIS_PROFILE_ID,
                "Frozen Thesis Profile",
                version="v1.2.1",
                # 設計本身出自已定稿的實驗計畫，因此建立時即為 FROZEN。
                # 這描述的是「這份研究設定已定稿」，與 Formal E2 的
                # 八項前置條件是兩回事 —— 後者由 final_gate 判定。
                state="FROZEN",
                source_document=SOURCE_DOCUMENT,
                description=THESIS_TITLE,
            )
        except ProfileExistsError:
            return self.get(project_id, THESIS_PROFILE_ID)

        # design 直接落盤：此時 profile 已是 FROZEN，write_design 會拒絕，
        # 而這一次寫入正是它凍結的那份內容本身。
        target = self._dir(project_id, THESIS_PROFILE_ID)
        (target / DESIGN_FILENAME).write_text(
            json.dumps(THESIS_DESIGN.to_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return profile
