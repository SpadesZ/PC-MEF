# PC-MEF Research System source maintenance contract
# 上下游: 由 console 的每一個「會寫入或會啟動執行」的端點呼叫；
#         能力由 Project template 與 Profile 宣告，**不由 UI 選擇決定**。
# 檔案路徑: pcmef/platform/capabilities.py
# 產生時間: 2026-09-08 14:10 +08:00
# 版本: v0.3.0
# 功能說明: Action capability 的定義與解析 —— 哪一個 Project/Profile
#           有資格看 Formal Workspace、觸發 dry-run、或寫 llm_runtime lock。
# 模組定位: Execution/Action Layer Closure 的核心。
#           **觀察頁顯示能力 ≠ 動作端點就有權限。**
#           Formal 與 freeze 這類寫入是科研動作，必須由後端依
#           Project + Profile + capability 判定，而不是「現在選了誰」。
# 主要責任:
#   1. CAPABILITIES 定義動作能力常數，MINIMUM_STATE 定義狀態下限
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
#   - **不得把新能力放進 _BASE 就算了。** _BASE 是「每個專案預設都有」，
#     而預設有的東西沒有人會再去看它讀了什麼。
#   - v0.3.0 變更：MINIMUM_STATE 定義完整的 Profile 狀態 × 動作對照；
#     llm_snapshot_read 移出 _BASE 改為宣告制。對應 round 3 的第 4 項。
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
#: 只有一項：跑自己的探索性模擬。它寫的是這次 run 自己的 artifacts，
#: 動不到任何 lock，也讀不到別人的東西。
#:
#: `LLM_SNAPSHOT_READ` **不在這裡**。`pcmef llm snapshot` 讀的是這個
#: repo 的 LLM 綁定（provider、model、revision），那是碩論的設定，
#: 不是每個專案各自的 —— 讓空專案讀它，畫面上就會出現「這個專案的
#: LLM 設定」而內容是別人的。與 E1 gate 是同一類洩漏。
_BASE: frozenset[str] = frozenset({RUN_SIMULATION})

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
    {LLM_SNAPSHOT_READ, LLM_RUNTIME_FREEZE, FORMAL_E2, SCIENTIFIC_STATE_READ}
)

#: 每個能力**最少**要走到哪一個 Profile 狀態才生效。
#:
#: 這張表是 Profile 狀態 × 動作的完整對照。沒有列在這裡的能力不受
#: 狀態下限限制（只要 Profile 不是 RUN_INHIBITED）。用「最低狀態」
#: 而不是逐一列舉允許的狀態：`PROFILE_STATES` 是一條**進程**，
#: 逐一列舉的話，日後在中間插入一個新狀態就會有人忘記更新其中一張表，
#: 而忘記的那一張通常是比較寬鬆的。
#:
#: 兩個下限，各自的理由：
#:
#: - `LLM_RUNTIME_FREEZE` 需要 `CONFIGURED`。它寫的是不可變的
#:   `freeze/llm_runtime.lock.json`，寫下去之後要說得出「它是哪一版
#:   設定產生的」。DRAFT 的字面意思就是還沒定案，掛在它上面的那句話
#:   隔天就不成立。
#: - `FORMAL_E2` 需要 `FROZEN`。一次性正式實驗不得跑在還會被原地
#:   修改的設定上，否則報告指向的那一版設定隔天就變了。
#:
#: `RUN_SIMULATION` 刻意**沒有**下限，也沒有上限：凍結之後仍然要能
#: 跑探索性模擬。凍結的是科學身分，不是這個人；讓 console 在研究
#: 進入正式階段的那一刻變成唯讀，正好是最需要拿它試東西的時候。
MINIMUM_STATE: dict[str, str] = {
    LLM_RUNTIME_FREEZE: "CONFIGURED",
    FORMAL_E2: "FROZEN",
}

#: 被 `MINIMUM_STATE` 擋下時要對使用者說的那一句。
#:
#: 只說「狀態不夠」會讓人去改狀態；要說的是**為什麼**這個動作需要
#: 那個階段，否則下一步就是有人把 Profile 直接推到 FROZEN 好讓按鈕
#: 亮起來。
_MINIMUM_REASON: dict[str, str] = {
    LLM_RUNTIME_FREEZE: (
        "凍結 llm_runtime 寫的是不可變的 lock，事後要說得出它是哪一版"
        "設定產生的；DRAFT 表示這份設定還沒定案。"
    ),
    FORMAL_E2: (
        "一次性正式實驗不得跑在還會被原地修改的設定上，"
        "否則報告指向的那一版設定隔天就變了。"
    ),
}


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
    一份還在 DRAFT 的設定照樣觸發得了一次性正式實驗。四條規則，
    依序套用：

      1. 專案已封存 → 沒有任何能力。
      2. `RUN_INHIBITED` 的 Profile → 沒有任何能力，連探索都不行。
         狀態名就是結論。
      3. `DECLARED_ONLY` 的能力需要一份選定的 Profile 才生效。
      4. `MINIMUM_STATE` 列出的能力另外要求 Profile 已走到某個階段。

    完整對照表（未封存專案，能力已宣告）：

    ==================== ======= ========== ============ =============
    Profile 狀態          模擬    snapshot   freeze lock   Formal E2
    ==================== ======= ========== ============ =============
    （未選定）             是      否          否            否
    DRAFT                  是      是          否            否
    CONFIGURED             是      是          是            否
    VALIDATED              是      是          是            否
    PILOT_READY            是      是          是            否
    FREEZE_CANDIDATE       是      是          是            否
    FROZEN                 是      是          是            是
    FORMAL_READY           是      是          是            是
    FORMAL_RUNNING         是      是          是            是
    FORMAL_COMPLETE        是      是          是            是
    RUN_INHIBITED          否      否          否            否
    ==================== ======= ========== ============ =============
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

    from pcmef.platform.profiles.models import PROFILE_STATES, RUN_INHIBITED

    state = str(getattr(profile, "state", ""))
    if state == RUN_INHIBITED:
        return frozenset()

    order = list(PROFILE_STATES)
    reached = order.index(state) if state in order else -1
    for capability, minimum in MINIMUM_STATE.items():
        if capability not in granted:
            continue
        # 狀態認不出來時一律擋掉。認不出來的狀態不能證明它走到了哪，
        # 而這兩個能力寫的都是不可變的科學狀態。
        if reached < 0 or reached < order.index(minimum):
            granted.discard(capability)
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

    from pcmef.platform.profiles.models import RUN_INHIBITED

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
    minimum = MINIMUM_STATE.get(capability)
    if minimum and state and state != minimum:
        # 只有在能力**已宣告**時才是狀態擋的；沒宣告是另一回事，
        # 落到最後那一段。
        declared = (getattr(project, "extra", None) or {}).get(CAPABILITY_FIELD) or ()
        if capability in {str(item) for item in declared}:
            raise CapabilityError(
                capability,
                f"Research Profile「{getattr(profile, 'display_name', '')}」"
                f"目前是 {state}，尚未到達 {minimum}。"
                f"{_MINIMUM_REASON.get(capability, '')}",
            )
    raise CapabilityError(
        capability,
        f"專案「{name}」沒有 {capability} 能力。"
        "這個動作會讀寫 PC-MEF 碩論專案的科研狀態，"
        "只有該專案本身可以執行。",
    )
