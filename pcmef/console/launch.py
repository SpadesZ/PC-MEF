# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes 與 console.formal_routes 的每一個啟動端點
#         呼叫；向 platform.capabilities 要能力、向 platform.runs 寫歸屬、
#         再交給 console.runner 啟動子行程。**唯一允許 runner.start() 的地方。**
# 檔案路徑: pcmef/console/launch.py
# 產生時間: 2026-09-06 23:40 +08:00
# 版本: v0.2.0
# 功能說明: 啟動一次 run 的完整交易 —— 解析身分、驗能力、寫歸屬、
#           起行程，任何一步失敗都回到「什麼都沒發生」。
# 模組定位: Execution/Action Layer Closure round 2 的 P0-1 / P0-2。
#           先前三個端點各自 runner.start()：只有 /api/console/runs
#           寫歸屬，另外兩個沒寫，於是 formal run 與 freeze run 都沒有
#           主人。判準散在三處時，寬鬆的那一份會先被執行到。
# 主要責任:
#   1. launch_run() 以交易方式啟動，失敗即回滾；身分只解析一次
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
#   - **不得替 launch_run() 加回 identity 參數。** 可注入的身分等於
#     可繞過整段解析，而呼叫端無從證明它傳的那一份是誰的。
#   - v0.2.0 變更：身分只解析一次，授權與歸屬共用同一份快照；
#     移除 identity 注入點。對應 round 3 的第 3 項。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_closure.py -v
# ------------------------------------------------------------

from __future__ import annotations

import shutil

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


def _require_executor(project, kind: str) -> None:
    """這個 Project 的研究模板有沒有這一支 executor。

    以 **template** 判定，不以 project id：資格是模板宣告出來的屬性，
    用 id 判斷與用名字判斷只差一層。
    """
    from pcmef.platform.executors import describe, supports

    template = getattr(project, "template", "") or ""
    if supports(template, kind):
        return

    available = describe(template)
    name = getattr(project, "display_name", None) or getattr(
        project, "project_id", "<unknown>"
    )
    if available:
        offered = "、".join(e.display_name for e in available)
        detail = f"這個專案可以執行的是：{offered}。"
    else:
        detail = (
            "這個專案的研究模板還沒有任何 executor —— 它有自己的流程"
            "定義，但還沒有能真正執行那個流程的程式。"
        )
    raise LaunchRefused(
        f"專案「{name}」不能執行 {kind}。{detail}"
        "執行器屬於研究模板，不是平台的通用功能；"
        "借用別的研究的執行器，跑出來的結果會標著這個專案的名字，"
        "內容卻是別人的場景。",
        403,
    )


def _discard(runner, run_id: str) -> None:
    """收回一筆沒有起跑的 run。**只在回滾路徑上使用。**

    刪除本身失敗不再拋出：那會蓋掉原本真正的失敗原因。

    **只有在確定沒有行程還活著時才可以呼叫。** 目錄裡的歸屬與指令是
    唯一指向那個行程的線索。
    """
    try:
        shutil.rmtree(runner.run_dir(run_id), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _mark_unreaped(runner, run_id: str, error) -> None:
    """把「行程可能還活著」寫進這筆 run。

    格式由 `runner.mark_unreaped()` 決定，兩條路徑共用同一份 ——
    啟動時 reader 掛不上去（這裡），以及執行中 reader 死掉
    （`ConsoleRunner._pump`）。各寫一份的話，其中一份遲早會少一個
    欄位，而少的通常是 pid，也就是唯一能讓人找到那個行程的東西。
    """
    process = getattr(error, "process", None)
    runner.mark_unreaped(
        run_id, process,
        reason=str(error),
        command=getattr(error, "command", None),
        started_at=getattr(error, "started_at", ""),
    )


def launch_run(runner, spec, *, capability: str):
    """啟動一次 run。**身分先於行程，行程起不來就什麼都不留。**

    順序是這裡唯一重要的事：

      1. 解析身分**一次**（回退即拒絕）
      2. 用那一份身分驗能力（沒有就拒絕，行程尚未存在）
      3. 用**同一份**身分展開歸屬快照
      4. 配 run id 與目錄
      5. 寫歸屬（寫不進去就收回目錄）
      6. 啟動子行程（起不來就連歸屬一起收回，並收掉已啟動的行程）

    「一次」是第 1 步的重點。解析兩次就有兩份身分，而兩次之間
    session 可以變 —— 授權看的是第一份、歸屬寫的是第二份時，一個
    被拒絕的專案仍然可以留下一筆記在別人名下的執行。

    這個函式**不接受外部傳進來的身分**。可注入的身分等於可繞過這整段
    解析，而呼叫端無從證明它傳的那一份是誰的。
    """
    from pcmef.console.routes import _resolve_run_identity, _write_attribution
    from pcmef.platform.capabilities import CapabilityError, require_capability

    context, profile = action_context()
    try:
        require_capability(context.project, capability, profile)
    except CapabilityError as error:
        raise LaunchRefused(str(error), 403) from None

    # 能力說「可以啟動」，executor registry 說「有什麼可以啟動」。
    # **兩個問題都要有答案。** 少了這一條，一個只有 RUN_SIMULATION 的
    # 空專案按下開始，跑的是 PC-MEF 的四個瓶內液態類別與那支瓶子的
    # 幾何 —— 結果標著它自己的名字，內容卻是別人的研究。
    _require_executor(context.project, spec.kind)

    try:
        identity = _resolve_run_identity(context, profile, spec.kind)
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
        if getattr(error, "process_reaped", True):
            _discard(runner, run_id)
            raise LaunchRefused(f"這次執行沒有啟動成功：{error}", 409) from None
        # **收不掉的行程不得連同紀錄一起消失。**
        #
        # 這個目錄裡有歸屬、有指令、有 run id —— 它是唯一指向那個
        # 仍可能在寫檔的行程的線索。刪掉之後，機器上有一個算圖的
        # 行程，而沒有任何東西說得出它是誰啟動的、在跑什麼。
        # 留著一筆狀態不明的紀錄，比留下一個查不到的行程好。
        _mark_unreaped(runner, run_id, error)
        raise LaunchRefused(
            f"這次執行沒有啟動成功，而且**子行程可能仍在執行**：{error}"
            f" 紀錄 {run_id} 已保留，供你手動確認並終止該行程。",
            409,
        ) from None
    except (FormalRunRefused, RunnerError):
        # 白名單與參數檢查的拒絕**照原樣往上拋**：它們各有自己的狀態碼
        # （403 / 400），包成 409 會把「你不准這樣跑」講成「現在跑不成」。
        # 但目錄仍然要收回 —— 被拒絕的啟動不留下任何 run。
        _discard(runner, run_id)
        raise
    except Exception as error:  # noqa: BLE001
        _discard(runner, run_id)
        raise LaunchRefused(f"這次執行沒有啟動成功：{error}", 409) from None
