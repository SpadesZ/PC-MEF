# PC-MEF Research System source maintenance contract
# 上下游: 由 console.project_routes 與 platform.projects.context 使用；
#         讀 Flask session 與 Project.default_profile_id 決定目前 Profile。
#         **不寫入任何科學資料。**
# 檔案路徑: pcmef/platform/profiles/selection.py
# 產生時間: 2026-09-07 09:10 +08:00
# 版本: v0.1.0
# 功能說明: 「目前正在看哪一份 Research Profile」的單一解答處。
# 模組定位: 平台化 Phase 4 前置。Status / Research Design / Pipeline /
#           Run / Results 全部綁同一個 profile_id，因此那個 id 必須
#           **明確**而不是排序後的第一筆 —— 一個 Project 有兩份
#           Development Profile 時，`profiles[0]` 會隨名稱改變而變，
#           而畫面不會說它變了。
# 主要責任:
#   1. PROFILE_SESSION_KEY 記錄每個 Project 各自選了哪份 Profile
#   2. SelectedProfile 綁定 Profile 與「為什麼是這一份」
#   3. resolve_profile() 依 session → default → 無 的順序解析
#   4. select_profile() 明確切換，並拒絕不存在的 Profile
# 維護提醒:
#   - **不得用 profiles[0] 當作 fallback。** 沒有明確選擇時就是
#     「尚未選擇」，畫面必須這樣說；隨便挑一份會讓使用者以為那是
#     這個研究的正式設定。
#   - 不得在唯讀頁切換 Profile。切換是有後果的動作（Status、Pipeline、
#     Results 會全部跟著換），必須經由集中的 POST 入口。
#   - 不得把選擇存成模組級變數；那會讓兩個開著不同 Profile 的分頁互相污染。
#   - v0.1.0 新增：首版，對應平台化 Phase 4 前置。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_profile_selection.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pcmef.platform.profiles.models import Profile
from pcmef.platform.profiles.registry import ProfileNotFoundError, ProfileRegistry

__all__ = [
    "PROFILE_SESSION_KEY",
    "SelectedProfile",
    "resolve_profile",
    "select_profile",
]

#: session 中「每個 Project 各自選了哪份 Profile」的鍵。
#:
#: 用 mapping 而不是單一值：切到 B 專案再切回 A 時，A 原本看的那一份
#: 應該還在。單一值會讓切換專案順帶重設 Profile，而畫面不會說。
PROFILE_SESSION_KEY = "pcmef_selected_profiles"

#: 解析來源。畫面用它說明「為什麼現在看的是這一份」。
ORIGIN_SESSION = "session"
ORIGIN_DEFAULT = "project-default"
ORIGIN_NONE = "none"


@dataclass(frozen=True)
class SelectedProfile:
    """目前選定的 Profile，以及它是怎麼被選中的。

    `profile` 為 None 代表**尚未選擇**，不是「出錯了」也不是
    「隨便給你一份」。畫面必須把這兩者分開。
    """

    profile: Profile | None
    origin: str = ORIGIN_NONE
    reason: str = ""

    @property
    def profile_id(self) -> str | None:
        return self.profile.profile_id if self.profile else None

    @property
    def is_selected(self) -> bool:
        return self.profile is not None


def _session_map(session: Any) -> dict[str, str]:
    raw = session.get(PROFILE_SESSION_KEY) if session is not None else None
    return dict(raw) if isinstance(raw, dict) else {}


def resolve_profile(
    project: Any, session: Any, *, registry: ProfileRegistry
) -> SelectedProfile:
    """解析這個 Project 目前該顯示哪一份 Profile。

    順序：session 的明確選擇 → Project 的 default_profile_id → 尚未選擇。
    **刻意沒有「取第一筆」這條**。
    """
    project_id = project.project_id
    chosen = _session_map(session).get(project_id)

    if chosen:
        try:
            return SelectedProfile(
                registry.get(project_id, chosen), ORIGIN_SESSION,
                "由使用者在此工作階段選定。",
            )
        except (ProfileNotFoundError, ValueError):
            # 選過的 Profile 被刪掉或改名了。不靜默改用別份 ——
            # 往下走到 default，並把原因留給畫面。
            pass

    default_id = getattr(project, "default_profile_id", None)
    if default_id:
        try:
            return SelectedProfile(
                registry.get(project_id, default_id), ORIGIN_DEFAULT,
                "此專案宣告的預設 Research Profile。",
            )
        except (ProfileNotFoundError, ValueError):
            return SelectedProfile(
                None, ORIGIN_NONE,
                f"此專案宣告的預設 Profile {default_id!r} 讀不到。",
            )

    available = registry.list_profiles(project_id)
    if available:
        return SelectedProfile(
            None, ORIGIN_NONE,
            f"此專案有 {len(available)} 份 Research Profile，但尚未選擇要看哪一份。",
        )
    return SelectedProfile(None, ORIGIN_NONE, "此專案尚未建立任何 Research Profile。")


def select_profile(
    session: Any, project_id: str, profile_id: str, *, registry: ProfileRegistry
) -> Profile:
    """明確切換目前 Profile。不存在即拒絕，且不寫入 session。"""
    profile = registry.get(project_id, profile_id)
    mapping = _session_map(session)
    mapping[project_id] = profile.profile_id
    session[PROFILE_SESSION_KEY] = mapping
    return profile
