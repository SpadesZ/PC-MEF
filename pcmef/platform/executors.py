# PC-MEF Research System source maintenance contract
# 上下游: 由 console.launch 在啟動前查詢，由 console.routes 投影成畫面上
#         有沒有那顆按鈕。executor 的實作在 pcmef.cli，**這裡只登記
#         「哪一個 template 有哪些 executor」**。
# 檔案路徑: pcmef/platform/executors.py
# 產生時間: 2026-09-07 15:40 +08:00
# 版本: v0.2.0
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
#   1. Executor 描述一種可啟動的指令：涵蓋範圍、節點與事件歸戶名稱
#   2. register() 讓 template 宣告自己的 executor
#   3. executors_for() / supports() / stage_of() / executor_of() 供查詢
#   4. _ensure_registered() 讓查詢不依賴匯入順序
#   5. SCOPES 區分 stage / pipeline / action
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
#   - **不得把 scope 與 stage_ids 收回成單一 stage_id。** Formal E2
#     涵蓋的是整條推論鏈；記成一個節點，紀錄就會宣稱它只做了最後一步。
#   - v0.2.0 新增：scope / stage_ids / event_stage，對應 round 6 的
#     第 6 項。
#   - v0.1.0 新增：首版，對應 round 4 的第 5 項。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_resilience.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Executor",
    "SCOPES",
    "SCOPE_ACTION",
    "SCOPE_PIPELINE",
    "SCOPE_STAGE",
    "STAGE_ARBITRATION",
    "STAGE_AUDIT",
    "STAGE_DECISION",
    "STAGE_FORMAL_E2",
    "STAGE_LLM_SNAPSHOT",
    "STAGE_PERCEPTION",
    "STAGE_RELIABILITY",
    "STAGE_ROUTING",
    "STAGE_SIMULATION",
    "describe",
    "executors_for",
    "register",
    "registered_templates",
    "executor_of",
    "stage_of",
    "supports",
]

#: 一支 executor 涵蓋多大範圍。
#:
#: 少了這個欄位，紀錄只能說「這次跑了 stage X」——而 Formal E2 跑的
#: 是從感知到決策的整條推論鏈，記成單一 `decision` 會讓紀錄宣稱它
#: 只做了最後一步。llm snapshot 與稽核則根本不是流程的一步，硬塞一個
#: stage 進去同樣是說謊。
SCOPE_STAGE = "stage"        # 實作流程裡的某一個節點
SCOPE_PIPELINE = "pipeline"  # 一次跑過流程的一段（多個節點）
SCOPE_ACTION = "action"      # 不是流程的一步（凍結、稽核）

SCOPES: tuple[str, ...] = (SCOPE_STAGE, SCOPE_PIPELINE, SCOPE_ACTION)

#: 流程節點與動作的名字。**單一來源。**
#:
#: 常數放在這裡而不是各自寫在 CLI 裡：登記處與 executor 必須說同一個
#: 名字，而兩份字面值遲早會有一份被改到。改了之後不會報錯 —— 事件
#: 只是落在一個沒有人在看的名字底下，畫面照常顯示「這一步沒開始」。
#:
#: 前六個對應 PIPELINE_NODES 的 key；最後兩個沒有對應節點（凍結與
#: 稽核不是研究流程的一步），它們只是事件的歸戶名稱。
STAGE_SIMULATION = "simulation"
STAGE_PERCEPTION = "perception"
STAGE_RELIABILITY = "reliability"
STAGE_ROUTING = "routing"
STAGE_ARBITRATION = "arbitration"
STAGE_DECISION = "decision"
#: 涵蓋多步的執行用自己的名字歸戶事件，**不掛在任何一個研究節點下**。
#: 掛在 `decision` 上的話，Run 頁會顯示「決策這一步完成了」，
#: 而實際發生的是整條鏈跑完了、其餘六個節點看起來沒動過。
STAGE_FORMAL_E2 = "formal_e2"
STAGE_LLM_SNAPSHOT = "llm_snapshot"
STAGE_AUDIT = "audit"


@dataclass(frozen=True)
class Executor:
    """一種可以從 console 啟動的指令。

    `stage_ids` 是這支指令**自己宣告**它涵蓋流程裡的哪幾步。由
    executor 這一側宣告而不是由 console 指定：console 若能指定，
    它就能把任何一支指令的輸出說成任何一步的進度，而畫面上看不出
    差別。

    `scope` 說的是涵蓋的大小 —— 單一節點、流程的一段，或者根本不是
    流程的一步。少了它，Formal E2 會被記成一個 `decision` 節點，
    而它其實從感知一路跑到決策。
    """

    kind: str
    display_name: str
    scope: str
    #: 這支指令涵蓋的流程節點，依流程順序。`action` 一律為空。
    stage_ids: tuple[str, ...] = ()
    #: 事件實際寫在哪一個名字底下。
    #:
    #: 與 `stage_ids` **分開**，因為兩者回答不同問題：stage_ids 說
    #: 「這次涵蓋了流程的哪幾步」，這個說「事件檔裡的 stage_id 欄位
    #: 會是什麼」。action 沒有涵蓋任何流程節點，但它的事件仍然需要
    #: 一個名字可以歸戶；不給的話，凍結與稽核的事件會混在一起。
    #: 留空時取 stage_ids 的最後一個 —— 那是這次執行的結果所代表的
    #: 那一步。
    event_stage: str = ""
    #: 一句話說明這支指令做什麼。畫面上顯示給使用者看。
    summary: str = ""

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(
                f"executor {self.kind!r} declares unknown scope {self.scope!r}; "
                f"expected one of {list(SCOPES)}"
            )
        if self.scope == SCOPE_ACTION and self.stage_ids:
            raise ValueError(
                f"executor {self.kind!r} is an action but names stages "
                f"{list(self.stage_ids)}; an action is not a step of the pipeline"
            )
        if self.scope != SCOPE_ACTION and not self.stage_ids:
            raise ValueError(
                f"executor {self.kind!r} claims scope {self.scope!r} but names "
                "no stage"
            )
        if not self.event_stage_id:
            raise ValueError(
                f"executor {self.kind!r} has nowhere to file its events; give "
                "it an event_stage or a stage_ids entry"
            )

    @property
    def event_stage_id(self) -> str:
        """事件實際落在哪一個 stage 名下。

        涵蓋多步時仍然只有一個事件流 —— executor 回報的是整體進度，
        不是逐節點的轉換，因此預設取最後一個節點：那是這次執行的
        結果所代表的那一步。
        """
        if self.event_stage:
            return self.event_stage
        return self.stage_ids[-1] if self.stage_ids else ""


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
    """這個 executor 的事件寫在哪一個 stage 名下。沒登記就回空字串。"""
    _ensure_registered()
    executor = _BY_TEMPLATE.get(template or "", {}).get(kind)
    return executor.event_stage_id if executor else ""


def executor_of(template: str | None, kind: str):
    """取得 executor 本身。沒登記就回 None。"""
    _ensure_registered()
    return _BY_TEMPLATE.get(template or "", {}).get(kind)


def describe(template: str | None) -> tuple[Executor, ...]:
    """供畫面列出「這個專案可以跑什麼」。"""
    _ensure_registered()
    return tuple(
        sorted(_BY_TEMPLATE.get(template or "", {}).values(), key=lambda e: e.kind)
    )
