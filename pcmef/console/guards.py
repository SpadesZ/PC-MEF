# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes、console.formal_routes 匯入；包住每一個
#         run-scoped 或會寫入科研狀態的端點。**唯一的判準來源。**
# 檔案路徑: pcmef/console/guards.py
# 產生時間: 2026-09-08 14:40 +08:00
# 版本: v0.1.0
# 功能說明: run ownership 與 action capability 的集中式 decorator。
# 模組定位: Execution/Action Layer Closure P0-3 / P0-4 / P0-5。
#           每個端點各寫一份判準，遲早會有一個漏掉 —— 而漏掉的那個
#           不會報錯，它會安靜地回 200。判準只能有一份。
# 主要責任:
#   1. requires_run_ownership() 擋下跨 Project 的 run 存取
#   2. requires_capability() 擋下沒有能力的科研動作
#   3. execution_context() 一次解析出動作要用的完整身分
#   4. _deny() 統一以 404 / 403 回應，HTML 與 JSON 各自合適
# 維護提醒:
#   - **不得在端點內另寫一份 ownership 或 capability 判斷。**
#     散開的判準會漂移，而寬鬆的那一份會先被執行到。
#   - 不得以 302 或空結果代替拒絕。看起來成功的拒絕最難查。
#   - 不得在拒絕時洩漏其他專案的內容摘要；只說「不屬於目前 Project」。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_guards.py -v
# ------------------------------------------------------------

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

__all__ = [
    "execution_context",
    "requires_capability",
    "requires_run_ownership",
]


def _wants_json() -> bool:
    from flask import request

    if request.path.startswith("/api/"):
        return True
    return not request.accept_mimetypes.accept_html


def _deny(message: str, status: int):
    """統一的拒絕回應。**不得用 302 或空結果代替。**"""
    from flask import jsonify

    if _wants_json():
        return jsonify({"error": message}), status
    from pcmef.console.routes import _not_found

    return _not_found(message), status


def execution_context():
    """動作開始前解析一次的完整身分。

    之後整段執行都用這一份 —— **中途不得再解析 session**。
    session 決定「發起時選了哪個 context」，不決定「這個已啟動的
    動作後來屬於誰」。
    """
    from pcmef.console.project_routes import request_context
    from pcmef.platform.capabilities import capabilities_for

    context = request_context()
    profile = getattr(context.selected, "profile", None)
    return context, profile, capabilities_for(context.project, profile)


def requires_run_ownership(view: Callable) -> Callable:
    """這一筆 run 必須屬於目前 Project，否則 404。

    直接貼別的專案的 run URL 與從清單點進去是同一件事。
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        from pcmef.console.routes import _require_run_ownership

        run_id = kwargs.get("run_id")
        _attribution, owned = _require_run_ownership(run_id)
        if not owned:
            return _deny(
                f"執行紀錄 {run_id} 不屬於目前的 Project。", 404
            )
        return view(*args, **kwargs)

    return wrapper


def requires_capability(capability: str) -> Callable:
    """這個動作需要 Project/Profile 明確具備的能力，否則 403。

    觀察頁顯示能力不等於動作端點就有權限 —— 這個 decorator 才是
    那條線；畫面上的按鈕只是它的投影。
    """

    def decorate(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            from pcmef.platform.capabilities import CapabilityError, require_capability

            context, profile, _caps = execution_context()
            try:
                require_capability(context.project, capability, profile)
            except CapabilityError as error:
                return _deny(str(error), 403)
            return view(*args, **kwargs)

        return wrapper

    return decorate
