# PC-MEF Research System source maintenance contract
# 上下游: 由 console.launch 在啟動前查詢，由 console.routes 投影成畫面上
#         有沒有那顆按鈕。executor 的實作在 pcmef.cli，**這裡只登記
#         「哪一個 template 有哪些 executor」**。
# 檔案路徑: pcmef/platform/executors.py
# 產生時間: 2026-09-07 15:40 +08:00
# 版本: v0.1.0
# 功能說明: Template → executor 的登記處。哪一個研究模板能跑哪幾種
#           指令，以及每一種指令會寫出哪一個 stage 的事件。
# 模組定位: Execution Layer Closure round 4 的第 5 項。
#           **PC-MEF 的 executor 不是平台的通用核心。**
#           `sim_smoke` 組出來的是四個瓶內液態類別與那支瓶子的幾何；
#           一個空專案有 RUN_SIMULATION 能力，於是按下「開始」就跑出
#           一份標著它自己名字、內容卻是碩論場景的結果。能力回答的是
#           「可不可以啟動」，這裡回答的是「這個專案有什麼可以啟動」。
#           兩個問題都要有答案，缺一個就會出現上面那種結果。
# 主要責任:
#   1. Executor 描述一種可啟動的指令與它寫出的 stage
#   2. register() 讓 template 宣告自己的 executor
#   3. executors_for() / supports() / stage_of() 供啟動與投影使用
#   4. _ensure_registered() 讓查詢不依賴匯入順序
# 維護提醒:
#   - **不得以 project id 判斷。** 資格由 template 宣告，用 id 判斷與
#     用名字判斷只差一層；test_the_executor_registry_is_keyed_by_template
#     _not_by_project_id 會直接擋下。
#   - 不得讓找不到 template 時回傳「全部」。找不到就是沒有 executor：
#     一個沒有登記過的模板不該繼承別人的執行能力。
#   - 不得在這裡放任何科學參數。這裡只說「有沒有這個 executor」，
#     指令怎麼組是 console.runner 的事，准駁是 CLI 自己的事。
#   - **不得移除查詢前的 _ensure_registered()。** 登記是「匯入即註冊」，
#     沒有它時這張表在還沒有人匯入 provider 的行程裡是空的 —— 而空表
#     的意思是「沒有任何 executor」，於是碩論自己被拒絕執行。授權結果
#     取決於匯入順序，是這一層最難重現的一種錯。
#   - v0.1.0 新增：首版，對應 round 4 的第 5 項。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_resilience.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Executor",
    "STAGE_AUDIT",
    "STAGE_DECISION",
    "STAGE_LLM_SNAPSHOT",
    "STAGE_PERCEPTION",
    "STAGE_SIMULATION",
    "describe",
    "executors_for",
    "register",
    "registered_templates",
    "stage_of",
    "supports",
]

#: 每一支 executor 寫出的 stage id。
#:
#: 常數放在這裡而不是各自寫在 CLI 裡：登記處與 executor 必須說同一個
#: 名字，而兩份字面值遲早會有一份被改到。改了之後不會報錯 —— 事件
#: 只是落在一個沒有人在看的 stage 名下，畫面照常顯示「這一步沒開始」。
#:
#: 前三個對應 PIPELINE_NODES 的 key；後兩個沒有對應節點（凍結與稽核
#: 不是研究流程的一步），Run 頁會照實說「事件檔提到流程定義裡沒有的
#: stage」——那句話是對的，不需要為了讓畫面好看而假裝它是一步。
STAGE_SIMULATION = "simulation"
STAGE_PERCEPTION = "perception"
STAGE_DECISION = "decision"
STAGE_LLM_SNAPSHOT = "llm_snapshot"
STAGE_AUDIT = "audit"


@dataclass(frozen=True)
class Executor:
    """一種可以從 console 啟動的指令。

    `stage_id` 是這支指令**自己宣告**它實作了流程裡的哪一步。由
    executor 這一側宣告而不是由 console 指定：console 若能指定，
    它就能把任何一支指令的輸出說成任何一步的進度，而畫面上看不出
    差別。
    """

    kind: str
    display_name: str
    stage_id: str
    #: 一句話說明這支指令做什麼。畫面上顯示給使用者看。
    summary: str = ""


_BY_TEMPLATE: dict[str, dict[str, Executor]] = {}

#: provider 是否已經被匯入過。
_LOADED = False


def _ensure_registered() -> None:
    """確保所有 provider 都已登記。

    **每一次查詢前都要呼叫。** 登記是「匯入即註冊」，因此在沒有人先
    匯入 provider 的情況下，這張表是空的 —— 而空表的意思是「這個
    template 沒有任何 executor」，於是碩論自己會被拒絕執行。
    授權結果取決於匯入順序，是這一層最難重現的一種錯：在網頁行程裡
    正常（route 早就匯入過 pipeline），在單元測試或 CLI 裡就不是。
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    # 匯入 pipeline 套件即觸發各 provider 的 _register()。
    # 放在函式內：pipeline.pcmef 反過來匯入本模組，模組層級匯入會成環。
    import pcmef.platform.pipeline  # noqa: F401


def register(template: str, executor: Executor) -> None:
    """登記某個 template 有這一個 executor。"""
    _BY_TEMPLATE.setdefault(template, {})[executor.kind] = executor


def registered_templates() -> tuple[str, ...]:
    _ensure_registered()
    return tuple(sorted(_BY_TEMPLATE))


def executors_for(template: str | None) -> frozenset[str]:
    """這個 template 能啟動哪幾種指令。

    沒有登記過就是**空的**，不是「全部」。一個通用模板不該繼承
    別人的執行能力 —— 那正是 Blank Project 跑出碩論場景的原因。
    """
    _ensure_registered()
    return frozenset(_BY_TEMPLATE.get(template or "", {}))


def supports(template: str | None, kind: str) -> bool:
    return kind in executors_for(template)


def stage_of(template: str | None, kind: str) -> str:
    """這個 executor 寫出的 stage id。沒登記就回空字串。"""
    _ensure_registered()
    executor = _BY_TEMPLATE.get(template or "", {}).get(kind)
    return executor.stage_id if executor else ""


def describe(template: str | None) -> tuple[Executor, ...]:
    """供畫面列出「這個專案可以跑什麼」。"""
    _ensure_registered()
    return tuple(
        sorted(_BY_TEMPLATE.get(template or "", {}).values(), key=lambda e: e.kind)
    )
