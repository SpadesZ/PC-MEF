# PC-MEF Research System source maintenance contract
# 上下游: 由 console 的每一個「會寫入或會啟動執行」的端點呼叫；
#         能力由 Project template 與 Profile 宣告，**不由 UI 選擇決定**。
# 檔案路徑: pcmef/platform/capabilities.py
# 產生時間: 2026-09-08 14:10 +08:00
# 版本: v0.1.0
# 功能說明: Action capability 的定義與解析 —— 哪一個 Project/Profile
#           有資格看 Formal Workspace、觸發 dry-run、或寫 llm_runtime lock。
# 模組定位: Execution/Action Layer Closure 的核心。
#           **觀察頁顯示能力 ≠ 動作端點就有權限。**
#           Formal 與 freeze 這類寫入是科研動作，必須由後端依
#           Project + Profile + capability 判定，而不是「現在選了誰」。
# 主要責任:
#   1. CAPABILITIES 定義動作能力常數
#   2. CapabilityError 為統一的拒絕型別
#   3. capabilities_for() 由 project/profile 推導能力集合
#   4. has_capability() / require_capability() 供端點使用
# 維護提醒:
#   - **不得以 Project 顯示名稱或 current selection 判定能力。**
#     能力是宣告出來的屬性；用名字比對會讓任何叫得像的專案取得
#     寫入 freeze/ 的資格。
#   - 不得讓能力預設為「全部開放」。新增的 template 預設只有觀察與
#     探索用模擬；要動科研狀態必須明確宣告。
#   - 不得在 archived 專案上回傳任何執行類能力。封存的意義就是
#     不能再啟動；只給按鈕變灰不算。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure P0-4 / P0-5。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_guards.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any

__all__ = [
    "CAPABILITIES",
    "FORMAL_E2",
    "LLM_RUNTIME_FREEZE",
    "LLM_SNAPSHOT_READ",
    "RUN_SIMULATION",
    "CAPABILITY_FIELD",
    "CapabilityError",
    "capabilities_for",
    "has_capability",
    "require_capability",
]

#: 探索用模擬。每個未封存的專案都有。
RUN_SIMULATION = "run_simulation"
#: 讀取 LLM 綁定快照（不寫 lock）。
LLM_SNAPSHOT_READ = "llm_snapshot_read"
#: **寫入 freeze/llm_runtime.lock.json。** 科研狀態寫入。
LLM_RUNTIME_FREEZE = "llm_runtime_freeze"
#: PC-MEF Formal E2：Workspace、pre-flight、dry-run、正式觸發。
FORMAL_E2 = "formal_e2"

CAPABILITIES: tuple[str, ...] = (
    RUN_SIMULATION,
    LLM_SNAPSHOT_READ,
    LLM_RUNTIME_FREEZE,
    FORMAL_E2,
)

#: 每個未封存專案都有的基本能力。
#:
#: 刻意**不含**任何會寫入 freeze/ 或觸發 Formal 的能力：新專案的
#: 預設應該是「可以自己探索」，不是「可以動這篇論文的科研狀態」。
_BASE: frozenset[str] = frozenset({RUN_SIMULATION, LLM_SNAPSHOT_READ})

#: 專案在自己的紀錄裡宣告能力的欄位。
#:
#: 能力是**宣告出來的資料**，不是從 project id 推導的。先前這裡比對
#: legacy id，而那與「用名字判斷資格」只差一層 —— 也被
#: test_no_module_branches_on_the_legacy_project_id 直接擋下。
#: 遷移時由 ensure_legacy_thesis_project() 寫入。
CAPABILITY_FIELD = "capabilities"


class CapabilityError(PermissionError):
    """這個 Project/Profile 沒有執行該動作的能力。**fail-closed。**"""

    def __init__(self, capability: str, reason: str = "") -> None:
        self.capability = capability
        self.reason = reason
        super().__init__(reason or f"missing capability {capability!r}")


def capabilities_for(project: Any, profile: Any = None) -> frozenset[str]:
    """這個 Project/Profile 實際具備的能力。

    封存的專案**沒有任何執行類能力** —— 封存的意義就是不能再啟動，
    而只讓按鈕變灰擋不住直接 POST。
    """
    if project is None:
        return frozenset()
    if getattr(project, "archived", False):
        return frozenset()

    granted = set(_BASE)
    declared = (getattr(project, "extra", None) or {}).get(CAPABILITY_FIELD) or ()
    granted |= {str(item) for item in declared if str(item) in CAPABILITIES}
    return frozenset(granted)


def has_capability(project: Any, capability: str, profile: Any = None) -> bool:
    return capability in capabilities_for(project, profile)


def require_capability(project: Any, capability: str, profile: Any = None) -> None:
    """沒有能力就拒絕。**不得降級成「假裝成功」或靜默略過。**"""
    if has_capability(project, capability, profile):
        return
    name = getattr(project, "display_name", None) or getattr(
        project, "project_id", "<unknown>"
    )
    if getattr(project, "archived", False):
        raise CapabilityError(
            capability,
            f"專案「{name}」已封存，不能執行任何研究動作。"
            "封存不刪除資料，但也不允許再啟動。",
        )
    raise CapabilityError(
        capability,
        f"專案「{name}」沒有 {capability} 能力。"
        "這個動作會讀寫 PC-MEF 碩論專案的科研狀態，"
        "只有該專案本身可以執行。",
    )
