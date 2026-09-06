# PC-MEF Research System source maintenance contract
# 上下游: 由 console.workspace_routes 的 Status 頁呼叫；依 Profile 的
#         template 選出對應 provider，回傳 models.LifecycleView。
# 檔案路徑: pcmef/platform/lifecycle/providers.py
# 產生時間: 2026-09-07 11:25 +08:00
# 版本: v0.1.0
# 功能說明: Lifecycle provider 的註冊與挑選，以及不含任何專案專屬判定的
#           generic provider。
# 模組定位: 平台化 Phase 4。**generic 在這裡，PC-MEF 在 pcmef.py。**
#           Blank Project 走 generic，不會看到任何 PC-MEF 字樣；
#           PC-MEF 走自己的 provider，其 gate 完整保留。
# 主要責任:
#   1. GENERIC_STAGES 定義八個通用研究階段
#   2. generic_lifecycle() 產生無 gate 的 lifecycle（全部 NOT_STARTED）
#   3. register() / build_lifecycle() 依 template 挑 provider
#   4. GENERIC_TEMPLATES 區分「本該用 generic」與「該有 provider 卻找不到」
# 維護提醒:
#   - **不得在 generic provider 加入任何 PC-MEF 專屬 gate 或字樣。**
#     Blank Project 會渲染它；洩漏出去的 PC-MEF 判準會讓一個空專案
#     看起來有研究進度。
#   - 不得讓 build_lifecycle() 在找不到 provider 時丟例外。Status 是
#     觀察頁；找不到就退回 generic，並在 note 說明。
#   - **但也不得靜默退回。** 該有 provider 卻找不到時要標成 unresolved；
#     一個 template 打錯字的專案若顯示成「還沒定義判準」，
#     那個畫面完全正常而且完全錯誤。
#   - v0.1.0 新增：首版，對應平台化 Phase 4。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_lifecycle.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any, Callable

from pcmef.platform.lifecycle.models import LifecycleView, Stage

__all__ = [
    "GENERIC_STAGES",
    "GENERIC_TEMPLATES",
    "build_lifecycle",
    "generic_lifecycle",
    "register",
    "registered_templates",
]

#: 八個通用研究階段。名稱刻意不含任何模態、類別或指標 ——
#: 一個只有 RGB 分類器的專案也要能用同一組階段描述自己。
GENERIC_STAGES: tuple[tuple[str, str, str], ...] = (
    ("project-definition", "研究定義 Project Definition",
     "確定研究問題、範圍與可檢驗的主張。"),
    ("sensor-data-design", "感測與資料設計 Sensor / Data Design",
     "決定模態、類別、條件與資料來源，並固定切分規則。"),
    ("calibration", "校準 Calibration",
     "把模型或模擬對齊到真實量測，並留下可比對的證據。"),
    ("perception-validation", "感知驗證 Perception Validation",
     "確認感知元件在既定資料上的行為符合預期。"),
    ("decision-validation", "決策驗證 Decision Validation",
     "確認融合、路由或仲裁等決策層的行為符合設計。"),
    ("pre-final-freeze", "正式前凍結 Pre-Final Freeze",
     "把所有會影響正式結果的設定凍成不可變身分。"),
    ("formal-experiment", "正式實驗 Formal Experiment",
     "以凍結後的身分執行正式實驗。"),
    ("analysis-publication", "分析與發表 Analysis / Publication",
     "整理結果、統計與圖表，形成可發表的結論。"),
)

#: 這些 template **本來就**該用 generic lifecycle，不算「找不到 provider」。
#:
#: 有了這一組，才分得出「這個專案還沒定義判準」與「這個專案該有判準
#: 但綁錯了」。少了它，一個 template 打錯字的專案看起來會像一個
#: 全新的空專案 —— 完全正常，而且完全錯誤。
GENERIC_TEMPLATES: frozenset[str] = frozenset({"", "blank", "blank-multimodal"})

#: template → provider。provider 收 context，回傳 LifecycleView。
_PROVIDERS: dict[str, Callable[[Any], LifecycleView]] = {}


def register(template: str, provider: Callable[[Any], LifecycleView]) -> None:
    """為某個 template 註冊 lifecycle provider。"""
    _PROVIDERS[template] = provider


def registered_templates() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def generic_lifecycle(context: Any = None) -> LifecycleView:
    """通用 lifecycle：八個階段，沒有任何 gate。

    沒有 gate 的階段一律 NOT_STARTED —— 這正確描述了一個尚未定義
    自己判準的專案：它有研究階段的形狀，但還沒有任何可判定的東西。
    """
    return LifecycleView(
        stages=tuple(
            Stage(stage_id=key, title=title, summary=summary)
            for key, title, summary in GENERIC_STAGES
        ),
        provider="generic",
        note=(
            "這個 Profile 尚未定義任何階段判準（gate）。"
            "階段結構是平台通用的；判準要由該研究自己定義。"
        ),
    )


def build_lifecycle(template: str | None, context: Any = None) -> LifecycleView:
    """依 template 挑 provider；找不到就退回 generic。

    Status 是觀察頁，不得因為沒有 provider 而 500 —— 使用者連
    「為什麼看不到」都不會知道。
    """
    key = template or ""
    provider = _PROVIDERS.get(key)
    if provider is None:
        view = generic_lifecycle(context)
        if key in GENERIC_TEMPLATES:
            return view
        # 該有 provider 卻找不到：template 打錯、模組沒載入、綁定壞掉。
        # **不得靜默退回 generic** —— 那會讓畫面看起來完全正常。
        return LifecycleView(
            stages=view.stages,
            provider=f"{key} (unresolved)",
            note=(
                f"找不到 template {key!r} 的 lifecycle provider。"
                "以下顯示的是通用階段，**不是這個研究自己的判準** —— "
                "可能是 template 名稱錯誤或 provider 未註冊。"
            ),
        )
    try:
        return provider(context)
    except Exception as error:  # noqa: BLE001 - 觀察頁不得因此 500
        view = generic_lifecycle(context)
        return LifecycleView(
            stages=view.stages,
            provider=f"{template} (failed)",
            note=f"無法建立此 Profile 的 lifecycle 判定：{error}",
        )
