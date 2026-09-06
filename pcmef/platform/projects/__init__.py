# PC-MEF Research System source maintenance contract
# 上下游: 平台層 Project 子套件的入口；由 console、cli 與 admin 匯入
#         Project / ProjectPaths / resolve_paths 等公開名稱。
#         **本檔只做轉出，不含邏輯。**
# 檔案路徑: pcmef/platform/projects/__init__.py
# 產生時間: 2026-09-06 14:40 +08:00
# 版本: v0.1.0
# 功能說明: 把 models 與 resolver 的公開名稱集中轉出，讓上層只需
#           匯入 pcmef.platform.projects。
# 模組定位: 平台化 Phase 1/2 的對外介面。上層拿得到的是
#           ProjectPaths，拿不到「怎麼算出這些路徑」——
#           那是 resolver 的事，也只該是 resolver 的事。
# 主要責任:
#   1. 轉出 Project、ProjectPaths、PROJECT_STATES
#   2. 轉出 resolve_paths、validate_project_id、LEGACY_THESIS_PROJECT_ID
# 維護提醒:
#   - 不得在本檔加入路徑組裝或 legacy 判斷。轉出層一旦開始算東西，
#     resolver 就不再是唯一的解析處。
#   - v0.1.0 新增：首版，對應平台化 Phase 1。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_paths.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pcmef.platform.projects.models import (
    PROJECT_SCHEMA_VERSION,
    Project,
    ProjectPaths,
)
from pcmef.platform.projects.resolver import (
    LEGACY_THESIS_PROJECT_ID,
    PROJECTS_DIRNAME,
    ProjectIdError,
    resolve_paths,
    validate_project_id,
    workspace_root,
)

__all__ = [
    "LEGACY_THESIS_PROJECT_ID",
    "PROJECTS_DIRNAME",
    "PROJECT_SCHEMA_VERSION",
    "Project",
    "ProjectIdError",
    "ProjectPaths",
    "resolve_paths",
    "validate_project_id",
    "workspace_root",
]
