# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes 與 console.formal_routes 的每一個啟動端點
#         呼叫；向 platform.capabilities 要能力、向 platform.runs 寫歸屬、
#         再交給 console.runner 啟動子行程。**唯一允許 runner.start() 的地方。**
# 檔案路徑: pcmef/console/launch.py
# 產生時間: 2026-09-06 23:40 +08:00
# 版本: v0.1.0
# 功能說明: 啟動一次 run 的完整交易 —— 解析身分、驗能力、寫歸屬、
#           起行程，任何一步失敗都回到「什麼都沒發生」。
# 模組定位: Execution/Action Layer Closure round 2 的 P0-1 / P0-2。
#           先前三個端點各自 runner.start()：只有 /api/console/runs
#           寫歸屬，另外兩個沒寫，於是 formal run 與 freeze run 都沒有
#           主人。判準散在三處時，寬鬆的那一份會先被執行到。
# 主要責任:
#   1. launch_run() 以交易方式啟動，失敗即回滾
#   2. action_context() 解析動作身分，回退情境一律拒絕
#   3. LaunchRefused 承載「拒絕的理由與該回的狀態碼」
#   4. _discard() 收回半途失敗的 run 目錄
# 維護提醒:
#   - **不得在別處直接呼叫 runner.start()。** 繞過本檔就等於繞過歸屬；
#     test_no_route_module_starts_a_run_outside_the_launch_transaction
#     會直接擋下。
#   - 不得把回滾改成「留著目錄但標記失敗」。留下的目錄沒有行程卻有
#     歸屬，在清單上與真的跑過的完全一樣。
#   - 不得在 context 回退時照樣執行。回退代表使用者選的專案不能用，
#     而**替他挑一個能用的來跑**是這一層最嚴重的錯：結果會被記在
#     一個他沒有選擇的專案底下。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_closure.py -v
# ------------------------------------------------------------

from __future__ import annotations

import shutil
from typing import Any

__all__ = ["LaunchRefused", "action_context", "launch_run"]


class LaunchRefused(Exception):
    """這次啟動被拒絕。`status` 是該回的 HTTP 狀態碼。

    403 是「你沒有資格」，409 是「現在的狀態下做不成」。兩者分開，
    因為使用者對它們該做的事不同：前者換專案，後者修狀態。
    """

    def __init__(self, message: str, status: int = 403) -> None:
        self.status = status
        super().__init__(message)


def action_context():
    """一個**動作**要用的身分。回退情境一律拒絕。

    觀察頁可以在回退後照樣顯示（版型會標出 fell_back），動作不行：
    session 指向的專案已封存或已消失時，`current_context()` 會回退到
    legacy Thesis —— 若動作照樣執行，使用者按下「開始」得到的是一筆
    記在碩論專案底下的執行，而他選的是別的專案（P0-4）。

    回傳 (context, profile)。
    """
    from pcmef.console.project_routes import request_context

    context = request_context()
    if getattr(context, "fell_back", False):
        raise LaunchRefused(
            (context.fallback_reason or "目前的 Project 無法使用。")
            + "動作不會在回退後的專案上執行 —— "
            "請先明確選擇一個要在其中執行的 Project。",
            409,
        )
    return context, getattr(context.selected, "profile", None)


def _discard(runner, run_id: str) -> None:
    """收回一筆沒有起跑的 run。**只在回滾路徑上使用。**

    刪除本身失敗不再拋出：那會蓋掉原本真正的失敗原因。
    """
    try:
        shutil.rmtree(runner.run_dir(run_id), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def launch_run(runner, spec, *, capability: str, identity: Any = None):
    """啟動一次 run。**身分先於行程，行程起不來就什麼都不留。**

    順序是這裡唯一重要的事：

      1. 解析身分（回退即拒絕）
      2. 驗能力（沒有就拒絕，行程尚未存在）
      3. 配 run id 與目錄
      4. 寫歸屬（寫不進去就收回目錄）
      5. 啟動子行程（起不來就連歸屬一起收回）

    第 5 步的回滾是新的。先前 Popen 在背景執行緒裡，啟動失敗只在 log
    留一行，HTTP 早就回了 201 —— 於是「有歸屬、有紀錄、從未執行」
    是一個可達狀態，而它在清單上與真的跑過的長得一樣。
    """
    from pcmef.console.routes import _resolve_run_identity, _write_attribution
    from pcmef.platform.capabilities import CapabilityError, require_capability

    context, profile = action_context()
    try:
        require_capability(context.project, capability, profile)
    except CapabilityError as error:
        raise LaunchRefused(str(error), 403) from None

    if identity is None:
        try:
            identity = _resolve_run_identity()
        except LaunchRefused:
            # 已經是一個有理由與狀態碼的拒絕。**不得再包一層** ——
            # 包起來之後畫面上顯示的是「無法確定歸屬：<真正的理由>」，
            # 而真正的理由才是使用者需要看到的那一句。
            raise
        except CapabilityError as error:
            raise LaunchRefused(str(error), 403) from None
        except Exception as error:  # noqa: BLE001
            raise LaunchRefused(f"無法確定這次執行的歸屬：{error}", 409) from None

    run_id = runner.allocate_run_id()
    try:
        _write_attribution(runner.run_dir(run_id), run_id, identity)
    except Exception as error:  # noqa: BLE001
        # 連配到的目錄一起收回。留著一個沒有歸屬的空目錄，日後任何
        # 「沒有 attribution 就當成 legacy」的判斷都會把它算成碩論的。
        _discard(runner, run_id)
        raise LaunchRefused(f"歸屬寫入失敗，未啟動任何行程：{error}", 409) from None

    from pcmef.console.runner import FormalRunRefused, RunLaunchError, RunnerError

    try:
        return runner.start(spec, run_id=run_id)
    except RunLaunchError as error:
        _discard(runner, run_id)
        raise LaunchRefused(f"這次執行沒有啟動成功：{error}", 409) from None
    except (FormalRunRefused, RunnerError):
        # 白名單與參數檢查的拒絕**照原樣往上拋**：它們各有自己的狀態碼
        # （403 / 400），包成 409 會把「你不准這樣跑」講成「現在跑不成」。
        # 但目錄仍然要收回 —— 被拒絕的啟動不留下任何 run。
        _discard(runner, run_id)
        raise
    except Exception as error:  # noqa: BLE001
        _discard(runner, run_id)
        raise LaunchRefused(f"這次執行沒有啟動成功：{error}", 409) from None
