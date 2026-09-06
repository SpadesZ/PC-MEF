# PC-MEF Research System source maintenance contract
# 上下游: 由 console.project_routes 建立專案時使用；決定新專案綁哪一組
#         lifecycle / pipeline provider，以及要不要先建一份起始 Profile。
# 檔案路徑: pcmef/platform/templates.py
# 產生時間: 2026-09-07 19:10 +08:00
# 版本: v0.1.0
# 功能說明: Project template 的定義與套用。
# 模組定位: 平台化 Phase 7。template 決定的是**起點**，不是身分 ——
#           建立之後專案自己演化，template 不再對它有發言權。
# 主要責任:
#   1. ProjectTemplate 定義一個起點：provider 綁定與起始 Profile
#   2. TEMPLATES 列出可選的三個 template
#   3. apply_template() 建立起始 Profile 並宣告為預設
#   4. template_choices() 給建立表單用
# 維護提醒:
#   - **不得讓 template 複製任何科學結果或 frozen evidence。**
#     template 給的是結構；結果屬於執行過它的那個專案。
#   - 不得讓 blank template 帶進任何 PC-MEF 專屬內容。它是
#     「不是這篇論文」的專案的起點；洩漏進去的判準會讓空專案
#     看起來已經有研究進度。
#   - 不得把 Thesis template 用於新專案的自動建立。既有碩論是遷移
#     而來的唯一一個；再建一個同樣宣稱是這篇論文的專案會讓
#     結果歸屬無法判定。
#   - v0.1.0 新增：首版，對應平台化 Phase 7。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_templates.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pcmef.platform.pipeline.registry import MINIMAL_TEMPLATE

__all__ = [
    "BLANK",
    "BLANK_MULTIMODAL",
    "TEMPLATES",
    "ProjectTemplate",
    "apply_template",
    "get_template",
    "template_choices",
]

BLANK = "blank"
BLANK_MULTIMODAL = "blank-multimodal"


@dataclass(frozen=True)
class ProjectTemplate:
    """一個新專案的起點。"""

    template_id: str
    display_name: str
    description: str
    #: 綁哪一組 pipeline provider。空字串代表用 minimal。
    pipeline_template: str = MINIMAL_TEMPLATE
    #: 綁哪一組 lifecycle provider。空字串代表用 generic 八階段。
    lifecycle_template: str = ""
    #: 建立時要不要先開一份起始 Development Profile。
    starter_profile_id: str = ""
    starter_profile_name: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "display_name": self.display_name,
            "description": self.description,
            "pipeline_template": self.pipeline_template,
            "lifecycle_template": self.lifecycle_template,
            "starter_profile_id": self.starter_profile_id,
        }


#: 可供建立新專案的 template。
#:
#: 刻意**不包含** PC-MEF Thesis：既有碩論是遷移而來的唯一一個，
#: 再建一個同樣宣稱是這篇論文的專案，結果歸屬就無法判定。
#: 要以它為起點的人應該 Clone 它，那條路會保留 parent 關係。
TEMPLATES: tuple[ProjectTemplate, ...] = (
    ProjectTemplate(
        template_id=BLANK,
        display_name="Blank Project",
        description="最小的起點：三節點流程與通用研究階段，沒有任何預設判準。",
        pipeline_template=MINIMAL_TEMPLATE,
        starter_profile_id="draft-v1",
        starter_profile_name="Development Profile v1",
    ),
    ProjectTemplate(
        template_id=BLANK_MULTIMODAL,
        display_name="Blank Multimodal Project",
        description=(
            "多模態研究的起點：同樣是通用階段，但起始 Profile 已標明"
            "這是一個多模態設計，供之後填入模態與類別。"
        ),
        pipeline_template=MINIMAL_TEMPLATE,
        starter_profile_id="draft-v1",
        starter_profile_name="Development Profile v1",
    ),
)


def get_template(template_id: str) -> ProjectTemplate | None:
    for item in TEMPLATES:
        if item.template_id == template_id:
            return item
    return None


def template_choices() -> list[dict[str, str]]:
    """建立表單用的選項。"""
    return [
        {"id": t.template_id, "name": t.display_name, "description": t.description}
        for t in TEMPLATES
    ]


def apply_template(
    template_id: str, project_id: str, *, projects: Any, profiles: Any
) -> None:
    """把 template 的起點套到剛建立的專案上。

    只建立**結構**：一份 DRAFT 的起始 Profile，並宣告為預設。
    不複製任何結果、frozen evidence 或執行紀錄 —— 那些屬於
    執行過它的專案，不屬於一個起點。
    """
    template = get_template(template_id)
    if template is None or not template.starter_profile_id:
        return

    from pcmef.platform.profiles.registry import ProfileExistsError

    try:
        profiles.create(
            project_id,
            template.starter_profile_id,
            template.starter_profile_name,
            state="DRAFT",
            description=template.description,
        )
    except ProfileExistsError:
        return

    # 明確宣告預設，而不是靠排序取第一筆。
    projects.set_default_profile(project_id, template.starter_profile_id)
