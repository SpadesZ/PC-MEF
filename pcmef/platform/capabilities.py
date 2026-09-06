# PC-MEF Research System source maintenance contract
# 上下游: 由 console 的每一個「會寫入或會啟動執行」的端點呼叫；
#         能力由 Project template 與 Profile 宣告，**不由 UI 選擇決定**。
# 檔案路徑: pcmef/platform/capabilities.py
# 產生時間: 2026-09-08 14:10 +08:00
# 版本: v0.2.0
# 功能說明: Action capability 的定義與解析 —— 哪一個 Project/Profile
#           有資格看 Formal Workspace、觸發 dry-run、或寫 llm_runtime lock。
# 模組定位: Execution/Action Layer Closure 的核心。
#           **觀察頁顯示能力 ≠ 動作端點就有權限。**
#           Formal 與 freeze 這類寫入是科研動作，必須由後端依
#           Project + Profile + capability 判定，而不是「現在選了誰」。
# 主要責任:
#   1. CAPABILITIES 定義動作能力常數
#   2. CapabilityError 為統一的拒絕型別
#   3. capabilities_for() 由 project 宣告與 Profile 狀態推導能力集合
#   4. has_capability() / require_capability() 供端點使用
# 維護提醒:
#   - **不得以 Project 顯示名稱或 current selection 判定能力。**
#     能力是宣告出來的屬性；用名字比對會讓任何叫得像的專案取得
#     寫入 freeze/ 的資格。
#   - 不得讓能力預設為「全部開放」。新增的 template 預設只有觀察與
#     探索用模擬；要動科研狀態必須明確宣告。
#   - 不得在 archived 專案上回傳任何執行類能力。封存的意義就是
#     不能再啟動；只給按鈕變灰不算。
#   - **不得讓 profile 參數變成裝飾。** 簽章收下它就必須讀它；
#     收下卻不用會讓「能力看 Profile 狀態」成為一句只寫在文件上的話。
#   - v0.2.0 變更：能力受 Profile 狀態約束，並新增 scientific_state_read，
#     對應 Execution Layer Closure round 2 的 P1-5 / P1-7。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure P0-4 / P0-5。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_guards.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any

__all__ = [
    "CAPABILITIES",
    "DECLARED_ONLY",
    "FORMAL_E2",
    "LLM_RUNTIME_FREEZE",
    "LLM_SNAPSHOT_READ",
    "RUN_SIMULATION",
    "SCIENTIFIC_STATE_READ",
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
#: 讀取這個 repo 既有的科研狀態 —— E1 十二道 gate 與 canonical
#: formal report。
#:
#: 這兩份東西住在 repo 根目錄，不在任何專案的 ProjectPaths 底下，
#: 因此每個專案的 Status / Results 都讀得到它們。那讓一個空專案的
#: 畫面看起來像是它自己跑出了碩論的結果（P1-5）。能力化之後，
#: 沒有宣告的專案看到的是「這個專案沒有這份紀錄」。
SCIENTIFIC_STATE_READ = "scientific_state_read"

CAPABILITIES: tuple[str, ...] = (
    RUN_SIMULATION,
    LLM_SNAPSHOT_READ,
    LLM_RUNTIME_FREEZE,
    FORMAL_E2,
    SCIENTIFIC_STATE_READ,
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

#: 只能由 Project 明確宣告、且必須有一份 Research Profile 才生效的能力。
#:
#: 這些動作都會讀寫既有研究的科研狀態。沒有選定 Profile 就沒有科學
#: 身分可以掛 —— 那樣寫出來的 lock 或報告事後說不出「它是哪一版設定
#: 產生的」，而那正是凍結要解決的問題。
DECLARED_ONLY: frozenset[str] = frozenset(
    {LLM_RUNTIME_FREEZE, FORMAL_E2, SCIENTIFIC_STATE_READ}
)


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

    `profile` **會被讀**。先前它收下就丟掉，於是簽章說「能力看 Profile」
    而實作說「只看 Project」—— 一個 RUN_INHIBITED 的設定照樣啟動得了，
    一份還在 DRAFT 的設定照樣觸發得了一次性正式實驗。三條規則：

      1. `RUN_INHIBITED` 的 Profile 一律沒有能力。狀態名就是結論。
      2. `DECLARED_ONLY` 的能力需要一份選定的 Profile 才生效。
      3. `FORMAL_E2` 另外要求 Profile 已凍結：一次性正式實驗不得跑在
         還會被原地修改的設定上，否則報告指向的那一版設定隔天就變了。
    """
    if project is None:
        return frozenset()
    if getattr(project, "archived", False):
        return frozenset()

    granted = set(_BASE)
    declared = (getattr(project, "extra", None) or {}).get(CAPABILITY_FIELD) or ()
    granted |= {str(item) for item in declared if str(item) in CAPABILITIES}

    if profile is None:
        return frozenset(granted - DECLARED_ONLY)

    from pcmef.platform.profiles.models import RUN_INHIBITED

    if str(getattr(profile, "state", "")) == RUN_INHIBITED:
        return frozenset()
    if not getattr(profile, "is_frozen", False):
        granted.discard(FORMAL_E2)
    return frozenset(granted)


def has_capability(project: Any, capability: str, profile: Any = None) -> bool:
    return capability in capabilities_for(project, profile)


def require_capability(project: Any, capability: str, profile: Any = None) -> None:
    """沒有能力就拒絕。**不得降級成「假裝成功」或靜默略過。**

    訊息要說出**是哪一條**擋下來的：只說「沒有權限」會讓使用者去改
    專案設定，而實際擋住他的是 Profile 的狀態。
    """
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

    from pcmef.platform.profiles.models import FROZEN_STATES, RUN_INHIBITED

    state = str(getattr(profile, "state", ""))
    if state == RUN_INHIBITED:
        raise CapabilityError(
            capability,
            f"Research Profile「{getattr(profile, 'display_name', '')}」"
            "目前是 RUN_INHIBITED，不得啟動任何執行。",
        )
    if capability in DECLARED_ONLY and profile is None:
        raise CapabilityError(
            capability,
            f"專案「{name}」尚未選定 Research Profile。"
            "這個動作會讀寫科研狀態，必須掛在一份明確的研究設定上，"
            "否則事後說不出它是哪一版設定產生的。",
        )
    if capability == FORMAL_E2 and state and state not in FROZEN_STATES:
        raise CapabilityError(
            capability,
            f"Research Profile「{getattr(profile, 'display_name', '')}」"
            f"目前是 {state}，尚未凍結。一次性正式實驗不得跑在還會被"
            "原地修改的設定上。",
        )
    raise CapabilityError(
        capability,
        f"專案「{name}」沒有 {capability} 能力。"
        "這個動作會讀寫 PC-MEF 碩論專案的科研狀態，"
        "只有該專案本身可以執行。",
    )
