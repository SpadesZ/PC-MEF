# PC-MEF Research System source maintenance contract
# 上下游: 平台層 Profile 子套件的入口；由 console 與 platform.projects
#         匯入 Profile / ResearchDesign / ProfileRegistry。
#         **本檔只做轉出，不含邏輯。**
# 檔案路徑: pcmef/platform/profiles/__init__.py
# 產生時間: 2026-09-06 21:05 +08:00
# 版本: v0.1.0
# 功能說明: 把 models、design 與 registry 的公開名稱集中轉出。
# 模組定位: 平台化 Phase 3 的對外介面。**Project ≠ Profile**：
#           projects 子套件管工作空間與 namespace；
#           本子套件管那個工作空間底下版本化的研究設定。
# 主要責任:
#   1. 轉出 Profile、PROFILE_STATES
#   2. 轉出 ResearchDesign、DesignSection、DesignField
#   3. 轉出 ProfileRegistry 與其例外
# 維護提醒:
#   - 不得在本檔加入邏輯或預設值。轉出層一旦開始判斷，
#     「Profile 現在什麼狀態」就會有第二個答案。
#   - v0.1.0 新增：首版，對應平台化 Phase 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_research_design.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pcmef.platform.profiles.design import (
    DESIGN_SCHEMA_VERSION,
    SECTION_ORDER,
    DesignField,
    DesignSection,
    ResearchDesign,
)
from pcmef.platform.profiles.models import (
    FROZEN_STATES,
    PROFILE_SCHEMA_VERSION,
    PROFILE_STATES,
    Profile,
)
from pcmef.platform.profiles.registry import (
    DESIGN_FILENAME,
    PROFILE_FILENAME,
    THESIS_PROFILE_ID,
    FrozenProfileError,
    ProfileExistsError,
    ProfileNotFoundError,
    ProfileRegistry,
)

__all__ = [
    "DESIGN_FILENAME",
    "DESIGN_SCHEMA_VERSION",
    "FROZEN_STATES",
    "PROFILE_FILENAME",
    "PROFILE_SCHEMA_VERSION",
    "PROFILE_STATES",
    "SECTION_ORDER",
    "THESIS_PROFILE_ID",
    "DesignField",
    "DesignSection",
    "FrozenProfileError",
    "Profile",
    "ProfileExistsError",
    "ProfileNotFoundError",
    "ProfileRegistry",
    "ResearchDesign",
]
