# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes、console.formal_routes 與 admin.routes_llm 匯入；
#         提供 _layout.html 需要的導航項目與 breadcrumb 建構器。
#         **純資料，不讀檔、不查詢、不寫入。**
# 檔案路徑: pcmef/console/navigation.py
# 產生時間: 2026-09-03 09:20 +08:00
# 版本: v0.2.0
# 功能說明: 全站五個主入口的單一定義處，以及 breadcrumb 的組裝工具。
# 模組定位: SAI v0.6.0 §20 的資訊架構。導航項目集中在這裡而不是散在各
#           template：散開的話新增一頁就會漏掉某個頁面的導航，
#           而那種漏在畫面上看起來只是「這頁比較舊」。
# 主要責任:
#   1. NAV_ITEMS 定義五個主入口與各自回答的問題
#   2. nav_context() 產生 template 需要的 nav_items / nav_active
#   3. RUN_SECTIONS 定義單次 run 的六個第二層分頁
#   4. run_subnav() 產生分頁列，並標出哪些對這次 run 有內容
#   5. breadcrumb() 組出「Workspace > Project > Results > Run ID > ...」這種路徑
# 維護提醒:
#   - 不得為了新功能再加第六個頂層項目。五個入口是刻意的收斂；
#     新功能要歸進其中一個，否則首頁會重新開始堆疊（那正是要修的問題）。
#     **Project 不是第六個項目**：它是這五個入口之上的一層，
#     由版型頂端的切換器呈現，不佔用導航列。
#   - 不得把 breadcrumb 的根改回寫死的專案名稱。根寫死時，切到另一個
#     專案後麵包屑仍會說 PC-MEF，而那在畫面上看起來完全正常。
#   - 不得把「沒有內容」的分頁藏起來。藏起來看起來像系統沒有這個能力，
#     而事實是「這種 run 不產生那一層」—— 兩者必須在畫面上分得出來。
#   - 不得把 href 寫成字面路徑。一律用 url_for 的端點名，
#     改路由時才不會留下指向 404 的導航。
#   - v0.1.0 新增：首版，對應 P2-2。
#   - v0.2.0 變更：breadcrumb 根由寫死的 PC-MEF 改為
#     Workspace > Project，對應平台化 Phase 1。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_navigation.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "NavItem", "NAV_ITEMS", "nav_context", "breadcrumb",
    "RunSection", "RUN_SECTIONS", "run_subnav",
]


@dataclass(frozen=True)
class NavItem:
    """一個主入口。`hint` 寫的是它**回答什麼問題**，不是它有什麼功能。"""

    key: str
    label: str
    endpoint: str
    hint: str


#: 五個主入口。順序即操作順序：先知道系統怎麼跑，才決定跑什麼。
#:
#: 每一項的 hint 都是一個問句，那是這次資訊架構重整的判準 ——
#: 一個功能該放哪裡，看它回答的是哪一個問題，而不是它在哪個模組裡實作。
NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("run", "實驗 Run", "console.page", "我要跑什麼？"),
    NavItem("pipeline", "流程 Pipeline", "pipeline.page", "系統怎麼跑？"),
    NavItem("results", "結果 Results", "results.page", "跑出了什麼？"),
    NavItem("status", "研究狀態 Status", "status.page", "研究目前做到哪裡？"),
    NavItem("llm", "LLM 設定", "llm_admin.page", "provider / model / binding"),
)


def nav_context(active: str) -> dict[str, Any]:
    """給 template 的導航內容。`active` 是 NAV_ITEMS 的 key。"""
    from flask import url_for

    if active not in {item.key for item in NAV_ITEMS}:
        raise ValueError(
            f"unknown nav key {active!r}; expected one of "
            f"{sorted(item.key for item in NAV_ITEMS)}"
        )
    return {
        "nav_items": [
            {
                "key": item.key,
                "label": item.label,
                "href": url_for(item.endpoint),
                "hint": item.hint,
            }
            for item in NAV_ITEMS
        ],
        "nav_active": active,
    }


@dataclass(frozen=True)
class RunSection:
    """單次 run 的第二層分頁。`answers` 同樣是一個問句。"""

    key: str
    label: str
    answers: str


#: 進入一次 run 之後的七個分頁（SAI v0.6.0 §20 第二層）。
#:
#: 順序照資料流：先看整體，再看決策過程，然後才是輸入、中間值、輸出。
#: Cost 排在 Outputs 之後、Artifacts 之前 —— 它是那份輸出的代價，
#: 屬於結果的一部分，不是檔案清單。Artifacts 放最後，它回答的是
#: 「檔案在哪」而不是「發生了什麼」。
RUN_SECTIONS: tuple[RunSection, ...] = (
    RunSection("overview", "總覽 Overview", "這次執行整體發生了什麼？"),
    RunSection("trace", "流程追蹤 Trace", "每一筆是怎麼被判斷的？"),
    RunSection("inputs", "輸入 Inputs", "餵進去的是什麼？"),
    RunSection("intermediate", "中間結果 Intermediate", "中途產生了什麼？"),
    RunSection("outputs", "輸出 Outputs", "得到什麼結論？"),
    RunSection("cost", "用量與成本 Cost", "這次花了多少？"),
    RunSection("artifacts", "Artifacts", "檔案落在哪裡？"),
)


def run_subnav(run_id: str, active: str, available: Mapping[str, bool]) -> list[dict[str, Any]]:
    """單次 run 的分頁列。

    `available` 標出哪些分頁對這次 run 有內容。**沒有內容的分頁仍然顯示**，
    只是標成 disabled —— 藏起來會讓人以為系統沒有這個能力，
    而事實是「這種 run 不產生那一層」。差別在畫面上必須看得出來。
    """
    from flask import url_for

    if active not in {section.key for section in RUN_SECTIONS}:
        raise ValueError(
            f"unknown run section {active!r}; expected one of "
            f"{sorted(s.key for s in RUN_SECTIONS)}"
        )
    return [
        {
            "key": section.key,
            "label": section.label,
            "answers": section.answers,
            "href": url_for("console.run_page", run_id=run_id, section=section.key),
            "active": section.key == active,
            "available": bool(available.get(section.key, True)),
        }
        for section in RUN_SECTIONS
    ]


def breadcrumb(
    *crumbs: tuple[str, str | None],
    project_name: str | None = None,
    workspace_href: str | None = None,
) -> list[dict[str, str | None]]:
    """組出 breadcrumb。每個 crumb 是 (label, href)；最後一個不加連結。

    根從 **Workspace** 開始，其後是目前的 Project。平台化之前這裡固定寫
    `PC-MEF`，那在只有一個研究的系統裡是對的；一旦有第二個專案，
    寫死的根就會在看 B 專案時仍然說 PC-MEF —— 而那種錯誤在畫面上
    看起來完全正常。

    `project_name` 未給時只有 Workspace 一層。**不在此查詢目前專案**：
    本模組是純資料，查詢會讓它需要 request context 才能被測試。
    """
    items: list[dict[str, str | None]] = [
        {"label": "Workspace", "href": workspace_href}
    ]
    if project_name:
        items.append({"label": project_name, "href": None})
    items.extend({"label": label, "href": href} for label, href in crumbs)
    return items
