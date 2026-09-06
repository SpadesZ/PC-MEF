# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.app 註冊；呼叫 experiments.formal_service 取得
#         pre-flight 與最近一次 run 的摘要；繪製 admin/templates/formal.html。
#         寫入端點只有 POST /formal/start，且不接受任何科學參數。
# 檔案路徑: pcmef/console/formal_routes.py
# 產生時間: 2026-09-02 16:10 +08:00
# 版本: v0.2.0
# 功能說明: Formal Research Workspace 的監控頁。顯示 identity、pre-flight
#           與最近一次執行的結果，讓「正式實驗跑到哪了」不必開終端機看。
# 模組定位: SAI v0.6.0 §19 的最小實作，含 §19.9 的 Run Controls。
#           啟動端點自 AMD-008 起存在，但它**只能觸發，不能設定** ——
#           表單只送 mode 與 confirm，准駁全在子行程的 pre-flight 內。
# 主要責任:
#   1. GET /formal 繪製 identity bar、pre-flight panel、啟動控制與結果
#   2. GET /formal/preflight.json 供輪詢，回傳與 CLI 相同的判斷
#   3. POST /formal/start 觸發 ConsoleRunner 的 formal_e2 子行程
#   4. 不重算任何 pre-flight 邏輯，一律轉呼叫 formal_service
# 維護提醒:
#   - 不得在本檔重算 pre-flight。畫面顯示的 PASS 必須與 `formal run-e2`
#     實際據以放行的是同一組判斷；兩份實作漂移時，畫面通常是比較寬鬆的
#     那一份（SAI §3.1）。
#   - 不得在本檔自行判斷「這次可不可以跑」。端點只負責把 mode 與
#     confirm 交給 ConsoleRunner；白名單與 pre-flight 才是准駁所在。
#   - 不得從表單接收任何科學參數。多一個鍵就會被白名單擋下（AMD-008）。
#   - 不得把 report 整份丟給前端。traces 有幾百列，formal_service
#     已經挑好畫面需要的欄位。
#   - v0.1.0 新增：首版唯讀監控頁，對應 P0-7a。
#   - v0.2.0 新增：POST /formal/start，對應 P0-7b 與 AMD-008（NOTE-059）。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_formal_routes.py -v
#   - py -3.10 -m pytest tests/console/test_formal_start.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint, current_app, jsonify, redirect, render_template, request, session,
    url_for,
)

from pcmef.admin.auth import AdminSecurityError, check_csrf, new_csrf_token
from pcmef.admin.routes_llm import ADMIN_TOKEN_HEADER, CSRF_FORM_FIELD, CSRF_SESSION_KEY
from pcmef.console.navigation import breadcrumb, nav_context
from pcmef.console.runner import (
    FORMAL_CONFIRM_PHRASE, FormalRunRefused, RunnerError, RunSpec,
)

__all__ = ["blueprint"]


def _project_name() -> str:
    """目前專案名稱，供 breadcrumb 使用。

    取不到時回空字串：breadcrumb 會少一層，而不是顯示錯的專案名稱。
    """
    try:
        from pcmef.console.project_routes import request_context

        return request_context().display_name
    except Exception:  # noqa: BLE001 - breadcrumb 不得讓整頁 500
        return ""

blueprint = Blueprint("formal", __name__)


def _formal_capability_guard():
    """這個 Project/Profile 有沒有 PC-MEF Formal E2 能力。

    先前這三個端點是 global singleton：任何專案都看得到碩論的
    Final gate，也都能 POST /formal/start（P0-4）。Formal 是這個
    研究自己的科研動作，不是平台功能。
    """
    from pcmef.console.guards import execution_context
    from pcmef.platform.capabilities import (
        FORMAL_E2, CapabilityError, require_capability,
    )

    context, profile, _caps = execution_context()
    try:
        require_capability(context.project, FORMAL_E2, profile)
    except CapabilityError as error:
        return context, str(error)
    return context, None

#: 畫面預設看的位置。與 CLI 的預設一致，否則兩邊會盯著不同的目錄。
DEFAULT_OUT = "outputs/perception/e2_final"
DEFAULT_BASE = "outputs/perception/formal_e2"


def _paths() -> dict[str, str]:
    config = current_app.config
    return {
        "base": config.get("PCMEF_FORMAL_BASE", DEFAULT_BASE),
        "out": config.get("PCMEF_FORMAL_OUT", DEFAULT_OUT),
        "lineage_root": config.get("PCMEF_FORMAL_LINEAGE_ROOT", "freeze"),
    }


def _collect(mode: str) -> dict[str, Any]:
    from pcmef.experiments.formal_service import (
        formal_status, latest_report, preflight,
    )

    paths = _paths()
    return {
        "mode": mode,
        "paths": paths,
        "preflight": preflight(mode=mode, **paths),
        "report": latest_report(paths["out"]),
        "status": formal_status(paths["out"]),
    }


def _guard() -> None:
    """寫入端點的 CSRF／權杖檢查。與 console 同一套，不另立一份。"""
    supplied = request.headers.get(ADMIN_TOKEN_HEADER)
    if supplied and current_app.config.get("PCMEF_ADMIN_TOKEN_CONFIGURED"):
        return
    check_csrf(
        session.get(CSRF_SESSION_KEY),
        request.form.get(CSRF_FORM_FIELD) or request.headers.get("X-CSRF-Token"),
    )


@blueprint.errorhandler(FormalRunRefused)
def _refused(error: FormalRunRefused):
    return jsonify({"error": str(error)}), 403


@blueprint.errorhandler(AdminSecurityError)
def _security(error: AdminSecurityError):
    return jsonify({"error": str(error)}), 403


@blueprint.errorhandler(RunnerError)
def _runner_error(error: RunnerError):
    return jsonify({"error": str(error)}), 400


@blueprint.get("/formal")
def page():
    # 能力先行：沒有 Formal E2 能力的專案連 Final gate 都不該讀到。
    context, denied = _formal_capability_guard()
    if denied:
        return render_template(
            "formal_unavailable.html",
            **nav_context("run"),
            breadcrumb=breadcrumb(
                ("實驗 Run", url_for("console.page")),
                ("Formal Research Workspace", None),
                project_name=context.display_name,
            ),
            reason=denied,
        ), 403

    # dry-run 與 formal 的判準不同，兩者都要看得到：使用者要知道的是
    # 「現在能不能跑正式的」，而不只是「預演能不能跑」。
    formal = _collect("formal")
    dry = _collect("dry-run")

    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = new_csrf_token()
        session[CSRF_SESSION_KEY] = token

    active = next(
        (r for r in current_app.config["PCMEF_CONSOLE_RUNNER"].list_runs()
         if r.kind == "formal_e2" and not r.finished),
        None,
    )
    return render_template(
        "formal.html",
        **nav_context("run"),
        breadcrumb=breadcrumb(
            ("實驗 Run", url_for("console.page")),
            ("Formal Research Workspace", None),
            project_name=_project_name(),
        ),
        formal=formal["preflight"],
        dry=dry["preflight"],
        report=formal["report"],
        paths=formal["paths"],
        status=formal["status"],
        agent_cache=current_app.config.get("PCMEF_FORMAL_AGENT_CACHE"),
        csrf_token=token,
        confirm_phrase=FORMAL_CONFIRM_PHRASE,
        active=active,
        # 已跑過的 formal run 紀錄。畫面要能說「是誰在什麼時候按的」，
        # 而不是只說「位置被佔用了」。
        formal_records=[
            r for r in current_app.config["PCMEF_CONSOLE_RUNNER"].list_runs(limit=200)
            if r.kind == "formal_e2" and str(r.params.get("mode")) == "formal"
        ],
    )


@blueprint.post("/formal/start")
def start():
    """啟動一次 formal run。

    這個端點**不含任何科學設定**：它把 mode 與 confirm 交給
    `ConsoleRunner`，由後者的白名單把關，再由 `pcmef formal run-e2`
    子行程自己做 pre-flight。UI 沒有任何一行程式碼能決定這次該不該跑
    ——它只能決定「按了」（AMD-008）。
    """
    _guard()

    form = request.form if request.form else (request.get_json(silent=True) or {})
    params = {"mode": str(form.get("mode", "dry-run"))}
    confirm = str(form.get("confirm", "")).strip()
    if confirm:
        params["confirm"] = confirm

    # **這是啟動一次性正式實驗的端點。** 能力、歸屬與回滾全部由
    # console.launch 這一份交易處理 —— 先前這裡直接 runner.start()，
    # 於是唯一一次 Formal E2 的執行紀錄沒有任何歸屬（P0-1）。
    from pcmef.console.launch import LaunchRefused, launch_run
    from pcmef.platform.capabilities import FORMAL_E2

    runner = current_app.config["PCMEF_CONSOLE_RUNNER"]
    try:
        record = launch_run(
            runner,
            RunSpec(kind="formal_e2", params=params,
                    label=f"Formal E2 · {params['mode']}"),
            capability=FORMAL_E2,
        )
    except LaunchRefused as error:
        return jsonify({"error": str(error)}), error.status

    if request.form:
        return redirect(url_for("console.run_page", run_id=record.run_id))
    return jsonify(record.to_json()), 201


@blueprint.get("/formal/preflight.json")
def preflight_json():
    _context, denied = _formal_capability_guard()
    if denied:
        return jsonify({"error": denied}), 403

    mode = request.args.get("mode", "formal")
    if mode not in ("formal", "dry-run"):
        return jsonify({"error": f"unknown mode {mode!r}"}), 400
    return jsonify(_collect(mode))
