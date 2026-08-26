# PC-MEF Research System source maintenance contract
# 上下游: 被 pcmef.llm.registry（binding 相容性檢查）、pcmef.llm.verification
#         （決定要跑哪些 probe）、pcmef.llm.snapshot（凍結前的完備性檢查）與
#         pcmef.admin.services（dropdown 過濾）匯入；本檔不讀寫任何檔案。
# 檔案路徑: pcmef/llm/capabilities.py
# 產生時間: 2026-08-26 22:40 +08:00
# 版本: v0.1.0
# 功能說明: 定義四個 Agent 角色各自需要哪些模型能力，以及「provider 宣稱有」與
#           「實際 probe 過」這兩種能力狀態的差別。綁定只認後者。
# 模組定位: §44 能力矩陣與 §45 task registry 的唯一宣告處。
#           它「不是」probe 執行器（那在 verification.py），也不存任何狀態。
# 主要責任:
#   1. TASK_REGISTRY 依 §45 宣告四個 task_code 的必要能力與輸出 schema
#   2. FORMAL_TASK_CODES 固定 Thesis Core 的四個角色，擋下未經核定的擴充 task
#   3. PROBE_ORDER 固定 probe 順序：chat -> structured_json -> vision -> embedding
#   4. required_capabilities() 查表並對未知 task_code fail-fast
#   5. MINIMUM_BINDABLE_CAPABILITIES 由表推導出「任一 role 的最低要求」
#   6. roles_servable() / roles_blocked() 回報一組能力能服務哪些角色
#   7. compatible_models() 過濾出能力相容且連線可用的 model profile
#   8. missing_capabilities() 回報缺哪些能力，供 UI 與 CLI 顯示拒絕理由
# 維護提醒:
#   - 不得把 embedding 列為任何 Thesis Core task 的必要能力。§41 明訂
#     PC-MEF Thesis Core 不需要 embedding，registry 支援它只為了未來擴充。
#   - 不得把 vision 當成所有線路的共同門檻。§45 是逐 role 的要求表：
#     physics_agent 與 arbitration_agent 不需要 vision，一個純文字模型
#     照樣能服務它們。把 vision 提升成鎖定條件會把這兩個角色一併鎖死。
#   - 不得手寫 MINIMUM_BINDABLE_CAPABILITIES 的內容；它必須由 TASK_REGISTRY
#     推導，否則改了能力矩陣就會留下一個對不上表格的常數。
#   - 不得新增 task_code 到 FORMAL_TASK_CODES。Appendix J3 規定正式 task 只有四個，
#     任何 RAG/embedding/content-generation task 未經研究設計核定只能列 extension。
#   - 不得以 declared_capabilities 取代 verified_capabilities 做綁定判定；
#     §44 明訂 provider metadata 只能作提示，不能取代正式 probe。
#   - 不得調整 PROBE_ORDER 讓 vision 早於 structured_json；§52 步驟 3 規定
#     先 chat + structured_json 再 vision，順序本身就是實作優先序的一部分。
#   - v0.1.0 新增：首版能力矩陣，決策見 NOTE-018。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_capabilities.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pcmef.agents.provider import Capability

__all__ = [
    "CapabilityError",
    "TaskSpec",
    "TASK_REGISTRY",
    "FORMAL_TASK_CODES",
    "PROBE_ORDER",
    "MINIMUM_BINDABLE_CAPABILITIES",
    "required_capabilities",
    "missing_capabilities",
    "roles_servable",
    "roles_blocked",
    "compatible_models",
]


class CapabilityError(ValueError):
    """未知的 task_code，或能力集合不符合契約。"""


@dataclass(frozen=True)
class TaskSpec:
    """§45 的一列。"""

    task_code: str
    ui_name: str
    required: tuple[Capability, ...]
    input_role: str
    output_schema: str
    description: str = ""


# §45 PC-MEF Task Binding Registry，逐列對應。
TASK_REGISTRY: dict[str, TaskSpec] = {
    spec.task_code: spec
    for spec in (
        TaskSpec(
            task_code="observation_agent",
            ui_name="Observation Agent",
            required=(Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON),
            input_role="RGB + ToF raw/summary + observable quality cues",
            output_schema="observation_brief_v1",
            description="產出跨模態的中性觀察摘要，不得給出類別建議。",
        ),
        TaskSpec(
            task_code="physics_agent",
            ui_name="Physics Agent",
            required=(Capability.CHAT, Capability.STRUCTURED_JSON),
            input_role="ObservationBrief + ToF/physics evidence",
            output_schema="specialist_proposal_v1",
            description="以 ToF 物理證據提出匿名專家意見。",
        ),
        TaskSpec(
            task_code="visual_semantic_agent",
            ui_name="Visual-Semantic Agent",
            required=(Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON),
            input_role="ObservationBrief + RGB/visual evidence",
            output_schema="specialist_proposal_v1",
            description="以視覺語意證據提出匿名專家意見。",
        ),
        TaskSpec(
            task_code="arbitration_agent",
            ui_name="Arbitration Agent",
            required=(Capability.CHAT, Capability.STRUCTURED_JSON),
            input_role="ObservationBrief + two anonymous proposals",
            output_schema="arbitration_output_v1",
            description="彙整兩份匿名意見產出 class_support 與 conflict_tag。",
        ),
    )
}

#: Appendix J3：Thesis Core 的唯一正式 Agent task codes。
FORMAL_TASK_CODES: tuple[str, ...] = (
    "observation_agent",
    "physics_agent",
    "visual_semantic_agent",
    "arbitration_agent",
)

#: §52 步驟 3 的 probe 順序。chat 先跑，因為它失敗時後兩者必然也失敗，
#: 先跑它可以用一次呼叫就給出可讀的失敗原因；embedding 墊底且非必要。
PROBE_ORDER: tuple[Capability, ...] = (
    Capability.CHAT,
    Capability.STRUCTURED_JSON,
    Capability.VISION,
    Capability.EMBEDDING,
)


#: 一條線路至少要具備哪些能力才值得鎖定 —— 即「任一 role 的最低要求」。
#: 由 TASK_REGISTRY 推導而非手寫：改了 §45 的能力矩陣，這個下限會自動跟著變，
#: 不會留下一個對不上表格的常數。
#:
#: 目前推導結果是 {chat, structured_json}（physics_agent 與 arbitration_agent
#: 的要求）。**刻意不含 vision** —— 一個沒有視覺能力的純文字模型仍然可以
#: 服務這兩個角色，把 vision 列為鎖定門檻等於把它們一併排除。
MINIMUM_BINDABLE_CAPABILITIES: frozenset[Capability] = frozenset(
    min((set(spec.required) for spec in TASK_REGISTRY.values()), key=len)
)


def roles_servable(verified: Iterable[Capability]) -> tuple[str, ...]:
    """這組已驗證能力可以服務哪些 task。

    綁定的守門在 task 層而不在線路層：每個 role 只需要它自己要求的能力
    （§45），因此一條只過 chat + structured_json 的線路仍可綁
    physics_agent 與 arbitration_agent。
    """
    have = set(verified)
    return tuple(
        task_code
        for task_code in FORMAL_TASK_CODES
        if set(TASK_REGISTRY[task_code].required).issubset(have)
    )


def roles_blocked(verified: Iterable[Capability]) -> dict[str, tuple[Capability, ...]]:
    """回報每個綁不上的 task 各自缺什麼，供 UI 與 CLI 顯示具體理由。"""
    servable = set(roles_servable(verified))
    return {
        task_code: missing_capabilities(task_code, verified)
        for task_code in FORMAL_TASK_CODES
        if task_code not in servable
    }


def required_capabilities(task_code: str) -> tuple[Capability, ...]:
    """查出某 task 的必要能力。未知 task_code 一律 fail-fast。"""
    try:
        return TASK_REGISTRY[task_code].required
    except KeyError:
        raise CapabilityError(
            f"unknown task_code {task_code!r}; the formal task registry is "
            f"{list(FORMAL_TASK_CODES)} (SRC-SAI §45, Appendix J3)"
        ) from None


def missing_capabilities(
    task_code: str, verified: Iterable[Capability]
) -> tuple[Capability, ...]:
    """回報某 model 對某 task 還缺哪些「已 probe 驗證」的能力。"""
    have = set(verified)
    return tuple(c for c in required_capabilities(task_code) if c not in have)


def compatible_models(task_code: str, models: Iterable, connections: dict) -> list:
    """過濾出可綁到該 task 的 model profile。

    三層條件，缺一不可：
      1. 線路已鎖定且健康（`ConnectionRow.bindable`）—— 沿用 roothinks LAVA
         setup 的「僅顯示 Locked」規則（NOTE-023）。
      2. 該 model 就是這條線路鎖定時選用的那一個。一條鎖定的線路只提供
         它被檢查過的那個模型；同一把 key 底下其他沒測過的模型不算。
      3. §42 區塊 C：capability 已 probe 驗證且與 task 相容。

    讓一個綁不上去的選項出現在下拉選單，等於把錯誤延後到按下 Bind 才爆。
    """
    result = []
    for model in models:
        connection = connections.get(model.connection_id)
        if connection is None or not connection.bindable:
            continue
        selected = getattr(connection, "selected_model_profile_id", None)
        if selected is not None and selected != model.model_profile_id:
            continue
        if missing_capabilities(task_code, model.verified_capabilities):
            continue
        result.append(model)
    return result
