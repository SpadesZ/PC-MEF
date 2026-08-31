# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.app 註冊為 blueprint；驅動 pcmef.console.runner
#         與 pcmef.console.results；繪製 templates/console.html 與
#         templates/console_run.html。CSRF 與權杖檢查沿用 pcmef.admin.auth。
# 檔案路徑: pcmef/console/routes.py
# 產生時間: 2026-08-27 16:55 +08:00
# 版本: v0.1.0
# 功能說明: 網頁上的執行台。按一下開始跑模擬，畫面像終端機一樣逐行更新，
#           跑完可以直接看曲線、四特徵與 gate 燈號。
# 模組定位: Console 的 HTTP 層。它「不是」formal runner ——
#           所有端點都經 runner 的 _assert_not_formal 守門。
# 主要責任:
#   1. page() 呈現執行台、歷史與系統狀態
#   2. start_run() 啟動一次探索性執行並導向該 run 的頁面
#   3. run_page() 呈現單次 run 的即時 log 與結果
#   4. stream() 以 Server-Sent Events 逐行推送新輸出
#   5. run_status() 供前端判斷是否該停止串流
# 維護提醒:
#   - 不得在此新增任何會寫 lock 或帶 --formal 的端點。§208 與 §52 結語：
#     UI 探索、CLI 凍結。runner 已有硬性檢查，這層不得繞過它。
#   - 不得把 SSE 換成把整份 log 每秒重傳一次。log 可以長到數 MB，
#     重傳會讓瀏覽器與伺服器都被自己的輸出拖垮；一律以位移增量推送。
#   - 不得讓 stream() 無上限地跑下去。run 結束後必須送出結束事件並關閉，
#     否則每開一個分頁就多一條永不釋放的連線。
#   - v0.1.0 新增：首版，決策見 NOTE-025。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_console_routes.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import time
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, redirect, request, url_for

from pcmef.admin.auth import AdminSecurityError, check_csrf
from pcmef.admin.routes_llm import ADMIN_TOKEN_HEADER, CSRF_FORM_FIELD, CSRF_SESSION_KEY
from pcmef.console.results import bar_chart_svg, line_chart_svg, load_results
from pcmef.console.runner import PRESETS, FormalRunRefused, RunnerError, RunSpec

__all__ = ["blueprint"]

blueprint = Blueprint("console", __name__)

#: SSE 的輪詢間隔與上限。上限存在是為了不讓忘記關的分頁累積連線。
_POLL_SECONDS = 0.4
_MAX_STREAM_SECONDS = 3600

#: 執行紀錄表格預設攤開幾筆，其餘收進 <details>。
#: 跑久了會累積上百筆，全部攤開時整個頁面只剩那張表。
RECENT_RUN_COUNT = 6


def _runner():
    return current_app.config["PCMEF_CONSOLE_RUNNER"]


def _guard() -> None:
    from flask import session

    supplied = request.headers.get(ADMIN_TOKEN_HEADER)
    if supplied and current_app.config.get("PCMEF_ADMIN_TOKEN_CONFIGURED"):
        return
    check_csrf(
        session.get(CSRF_SESSION_KEY),
        request.form.get(CSRF_FORM_FIELD) or request.headers.get("X-CSRF-Token"),
    )


@blueprint.errorhandler(FormalRunRefused)
def _formal_refused(error: FormalRunRefused):
    return jsonify({"error": str(error)}), 403


@blueprint.errorhandler(RunnerError)
def _runner_error(error: RunnerError):
    return jsonify({"error": str(error)}), 400


@blueprint.errorhandler(AdminSecurityError)
def _security(error: AdminSecurityError):
    return jsonify({"error": str(error)}), 403


# ---------------------------------------------------------------------------
# 頁面
# ---------------------------------------------------------------------------


@blueprint.get("/console")
def page():
    from flask import render_template, session

    from pcmef.admin.auth import new_csrf_token

    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = new_csrf_token()
        session[CSRF_SESSION_KEY] = token

    runner = _runner()
    query = request.args.get("q", "").strip()
    runs = runner.list_runs(query=query)
    # active 一律看全部，不受搜尋影響：「有東西正在跑」不該因為使用者
    # 剛好搜了別的關鍵字就從畫面上消失。
    active = next((r for r in runner.list_runs() if not r.finished), None)
    return render_template(
        "console.html",
        csrf_token=token,
        presets=PRESETS,
        runs=runs,
        query=query,
        recent_count=RECENT_RUN_COUNT,
        classes=current_app.config.get("PCMEF_CONSOLE_CLASSES", []),
        gate_summary=current_app.config["PCMEF_CONSOLE_GATE_SUMMARY"](),
        active=active,
    )


@blueprint.get("/console/runs/<run_id>")
def run_page(run_id: str):
    from flask import render_template, session

    runner = _runner()
    record = runner.get(run_id)
    bundle = load_results(runner.run_dir(run_id) / "artifacts", run_id)

    curves_svg = ""
    energy_svg = ""
    if bundle.curves:
        curves_svg = line_chart_svg(
            [(c.scenario_id, c.times_ns, c.energy) for c in bundle.curves],
            x_label="time (ns)", y_label="energy per bin",
        )
        energy_svg = bar_chart_svg(
            [c.scenario_id.replace("smoke_", "") for c in bundle.curves],
            [c.total_energy for c in bundle.curves],
            y_label="total energy",
        )

    return render_template(
        "console_run.html",
        csrf_token=session.get(CSRF_SESSION_KEY, ""),
        record=record,
        bundle=bundle,
        curves_svg=curves_svg,
        energy_svg=energy_svg,
        initial_log=runner.tail(run_id)[0],
    )


# ---------------------------------------------------------------------------
# 動作
# ---------------------------------------------------------------------------


@blueprint.post("/api/console/runs")
def start_run():
    _guard()
    form = request.form if request.form else (request.get_json(silent=True) or {})
    kind = str(form.get("kind", "sim_smoke"))

    params: dict = {"preset": str(form.get("preset", "standard"))}
    for key in (
        "resolution", "spp", "temporal_bins", "seed_offset",
    ):
        value = str(form.get(key, "")).strip()
        if value:
            params[key] = int(value)
    for key in (
        "sensor_to_bottle_mm", "bottle_diameter_mm",
        "wall_thickness_mm", "lateral_offset_mm", "irradiance",
    ):
        value = str(form.get(key, "")).strip()
        if value:
            params[key] = float(value)
    selected = request.form.getlist("classes") if request.form else form.get("classes")
    if selected:
        params["classes"] = list(selected)
    if kind == "surrogate_smoke":
        params["simulation_out"] = str(form.get("simulation_out", "")).strip()

    record = _runner().start(RunSpec(kind=kind, params=params))
    if request.form:
        return redirect(url_for("console.run_page", run_id=record.run_id))
    return jsonify(record.to_json()), 201


@blueprint.post("/api/console/runs/<run_id>/delete")
def delete_run(run_id: str):
    """刪掉一次執行紀錄。執行中的由 runner 拒絕。"""
    _guard()
    _runner().delete(run_id)
    if request.form:
        return redirect(url_for("console.page"))
    return jsonify({"deleted": run_id}), 200


@blueprint.post("/api/console/llm-snapshot")
def llm_snapshot():
    """從主控台觸發 `pcmef llm snapshot`，可選擇是否 --freeze。

    **UI 不判斷該不該凍結。** 這個端點只啟動真正的 CLI 子行程；前提未齊時
    是 CLI 自己印出缺什麼並回 exit 2。§52 結語要擋的是「UI 成為繞過 freeze
    的第二條設定通道」—— 而這裡走的是同一條通道、同一組檢查，
    並且把完整指令與輸出留成一筆可追溯的 run record。

    直接寫 lock 的那條路（`POST /api/admin/llm/runtime-snapshot`）仍然是 403：
    admin 頁面自己永遠不寫 lock，要寫就得經過這個真正的子行程。
    """
    _guard()
    form = request.form if request.form else (request.get_json(silent=True) or {})
    freeze = str(form.get("freeze", "")) in ("1", "true", "True", "on")
    record = _runner().start(
        RunSpec(
            kind="llm_snapshot",
            params={"freeze": freeze},
            label="凍結 llm_runtime.lock" if freeze else "檢查（不寫入）",
        )
    )
    if request.form:
        return redirect(url_for("console.run_page", run_id=record.run_id))
    return jsonify(record.to_json()), 201


@blueprint.get("/api/console/runs/<run_id>/status")
def run_status(run_id: str):
    return jsonify(_runner().get(run_id).to_json())


@blueprint.get("/api/console/runs/<run_id>/stream")
def stream(run_id: str):
    """以 SSE 逐行推送新輸出。

    只推送新增的部分（以位元組位移為界），而不是每次重傳整份 log ——
    一次高品質模擬的輸出可以到數 MB，重傳會讓瀏覽器與伺服器一起被拖垮。
    """
    runner = _runner()
    runner.get(run_id)  # 不存在時在建立串流之前就先失敗

    def generate():
        offset = 0
        started = time.monotonic()
        while True:
            chunk, offset = runner.tail(run_id, offset)
            if chunk:
                for line in chunk.splitlines():
                    yield f"data: {json.dumps({'line': line})}\n\n"
            record = runner.get(run_id)
            if record.finished:
                # 結束前再抓一次，避免最後幾行落在旗標翻轉與讀取之間。
                trailing, offset = runner.tail(run_id, offset)
                for line in trailing.splitlines():
                    yield f"data: {json.dumps({'line': line})}\n\n"
                yield (
                    "event: done\ndata: "
                    + json.dumps(
                        {"status": record.status, "exit_code": record.exit_code}
                    )
                    + "\n\n"
                )
                return
            if time.monotonic() - started > _MAX_STREAM_SECONDS:
                yield "event: done\ndata: " + json.dumps(
                    {"status": "timeout", "exit_code": None}
                ) + "\n\n"
                return
            time.sleep(_POLL_SECONDS)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
