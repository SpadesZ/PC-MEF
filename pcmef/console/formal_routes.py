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
from pcmef.console.runner import (
    FORMAL_CONFIRM_PHRASE, FormalRunRefused, RunnerError, RunSpec,
)

__all__ = ["blueprint"]

blueprint = Blueprint("formal", __name__)

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
    from pcmef.experiments.formal_service import latest_report, preflight

    paths = _paths()
    return {
        "mode": mode,
        "paths": paths,
        "preflight": preflight(mode=mode, **paths),
        "report": latest_report(paths["out"]),
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
        formal=formal["preflight"],
        dry=dry["preflight"],
        report=formal["report"],
        paths=formal["paths"],
        csrf_token=token,
        confirm_phrase=FORMAL_CONFIRM_PHRASE,
        active=active,
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

    runner = current_app.config["PCMEF_CONSOLE_RUNNER"]
    record = runner.start(
        RunSpec(kind="formal_e2", params=params, label=f"Formal E2 · {params['mode']}")
    )
    if request.form:
        return redirect(url_for("console.run_page", run_id=record.run_id))
    return jsonify(record.to_json()), 201


@blueprint.get("/formal/preflight.json")
def preflight_json():
    mode = request.args.get("mode", "formal")
    if mode not in ("formal", "dry-run"):
        return jsonify({"error": f"unknown mode {mode!r}"}), 400
    return jsonify(_collect(mode))
