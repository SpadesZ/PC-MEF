# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.projects.registry、console 各 route 與 cli 匯入；
#         回傳 models.ProjectPaths。**本檔是全系統唯一允許判斷 legacy
#         Thesis Project 特殊路徑的地方。**
# 檔案路徑: pcmef/platform/projects/resolver.py
# 產生時間: 2026-09-06 14:25 +08:00
# 版本: v0.1.0
# 功能說明: 把 project_id 解析成該專案的七個根目錄，並集中處理
#           legacy Thesis Project「科學資料留在 repo 根目錄」這一個例外。
# 模組定位: 平台化 Phase 2 的核心。既有 PC-MEF 的 freeze/、outputs/、
#           configs/ **一個位元組都不搬**，因此必然存在一個與其他專案
#           不同形狀的路徑對映。本檔的存在理由，就是讓那個例外
#           **只有一份**。
# 主要責任:
#   1. LEGACY_THESIS_PROJECT_ID 宣告唯一的 legacy 專案識別碼
#   2. validate_project_id() 擋掉路徑穿越與非法字元
#   3. resolve_paths() 產生 ProjectPaths，內含唯一的 legacy 分支
#   4. workspace_root() 決定 workspace 根目錄（可由測試覆寫）
# 維護提醒:
#   - **不得在本檔以外的任何地方比對 LEGACY_THESIS_PROJECT_ID 來決定路徑。**
#     散落的 `if project_id == "pcmef-thesis"` 會讓「例外」變成不可數的一組
#     判斷，而新增第三種 layout 時沒有人找得齊它們。上層一律只拿
#     ProjectPaths，不自己拼路徑。
#   - 不得為 project_id 接受相對路徑片段。`..` 或分隔符號會讓一個專案
#     讀到另一個專案、甚至 workspace 之外的檔案，那正是 namespace
#     隔離要防的事。
#   - 不得為 legacy 專案把 metadata 也放回 repo 根目錄。metadata 一律住在
#     projects/<id>/：唯一該例外的是科學資料的位置，不是專案本身。
#   - v0.1.0 新增：首版，對應平台化 Phase 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_paths.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re
from pathlib import Path

from pcmef.platform.projects.models import ProjectPaths

__all__ = [
    "LEGACY_THESIS_PROJECT_ID",
    "PROJECTS_DIRNAME",
    "ProjectIdError",
    "resolve_paths",
    "validate_project_id",
    "workspace_root",
]

#: 既有 PC-MEF 碩論專案。**全系統只有這一個 legacy layout。**
LEGACY_THESIS_PROJECT_ID = "pcmef-thesis"

#: 所有專案 metadata 的家。legacy 專案的 metadata 也在這裡。
PROJECTS_DIRNAME = "projects"

#: 專案識別碼：小寫英數與連字號，開頭必須是字母。
#:
#: 刻意不接受底線與點：`.` 開頭在多數工具裡是隱藏檔，而 `..` 是路徑穿越。
#: 與其逐一排除危險字元，不如只允許明確安全的一組。
_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


class ProjectIdError(ValueError):
    """project_id 不合法。**一律拒絕，不做清洗。**

    清洗會把 `a/b` 與 `ab` 對映到同一個目錄，而那是兩個不同的專案。
    """


def validate_project_id(project_id: str) -> str:
    """檢查 project_id 是否可安全用作目錄名稱。"""
    if not _PROJECT_ID_PATTERN.match(project_id):
        raise ProjectIdError(
            f"invalid project id {project_id!r}: must match "
            f"{_PROJECT_ID_PATTERN.pattern} (lowercase letters, digits and "
            "hyphens, starting with a letter). Refusing rather than sanitising, "
            "because stripping characters maps two distinct ids onto one directory."
        )
    return project_id


def workspace_root(root: str | Path | None = None) -> Path:
    """workspace 根目錄。預設為 repo 根目錄。

    測試傳入 tmp_path 即可得到完全隔離的 workspace，不必碰真實 freeze/。
    """
    if root is not None:
        return Path(root)
    # pcmef/platform/projects/resolver.py -> repo root
    return Path(__file__).resolve().parents[3]


def resolve_paths(
    project_id: str, *, root: str | Path | None = None
) -> ProjectPaths:
    """把 project_id 解析成該專案的七個根目錄。

    metadata 一律在 `projects/<id>/`。科學資料的位置分兩種：

    - **legacy Thesis Project**：freeze/ outputs/ configs/ data/ 留在 repo
      根目錄。既有 frozen lock 與 ACTIVE_LINEAGE 不因平台化而移動 ——
      搬動它們等於為了 UI 重構去改動科學來源樹。
    - **其他專案**：全部收在 `projects/<id>/` 底下。

    這是**唯一**一處做這個判斷的地方。
    """
    validate_project_id(project_id)
    base = workspace_root(root)
    metadata_root = base / PROJECTS_DIRNAME / project_id

    if project_id == LEGACY_THESIS_PROJECT_ID:
        # ── 全系統唯一的 legacy 分支 ────────────────────────────────
        # 這些路徑對應既有 repo 佈局，不是設計出來的形狀。
        return ProjectPaths(
            project_id=project_id,
            metadata_root=metadata_root,
            freeze=base / "freeze",
            outputs=base / "outputs",
            configs=base / "configs",
            datasets=base / "data",
            runs=base / "outputs" / "console" / "runs",
            artifacts=base / "artifacts",
        )

    project_root = metadata_root
    return ProjectPaths(
        project_id=project_id,
        metadata_root=metadata_root,
        freeze=project_root / "freeze",
        outputs=project_root / "outputs",
        configs=project_root / "configs",
        datasets=project_root / "datasets",
        runs=project_root / "runs",
        artifacts=project_root / "artifacts",
    )
