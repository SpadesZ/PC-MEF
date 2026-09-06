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
from pcmef.console.guards import requires_run_ownership
from pcmef.console.navigation import breadcrumb, nav_context
from pcmef.platform.capabilities import (
    LLM_RUNTIME_FREEZE,
    LLM_SNAPSHOT_READ,
    RUN_SIMULATION,
)
from pcmef.console.results import bar_chart_svg, line_chart_svg, load_results
from pcmef.console.runner import PRESETS, FormalRunRefused, RunnerError, RunSpec

#: agent cache 的預設根目錄。與 agents.cache.DEFAULT_CACHE_ROOT 同源 ——
#: 在這裡另寫一個字面路徑，改了那邊之後瀏覽器會安靜地看向空目錄。
from pcmef.agents.cache import DEFAULT_CACHE_ROOT

__all__ = ["blueprint"]


def _deny_action(message: str, status: int):
    """拒絕一個動作。**不得用 302 或空結果代替。**"""
    from flask import request as _request

    if _request.form and not _request.path.startswith("/api/"):
        return _not_found(message), status
    return jsonify({"error": message}), status


def _resolve_run_identity():
    """在任何行程啟動**之前**解析出這次執行的完整身分。

    解析失敗就不啟動。回傳的是一份快照，之後整段執行都用它 ——
    中途再解析一次 session，會讓切換 Profile 改掉已啟動 run 的歸屬。

    身分取自 `launch.action_context()`：context 回退時這裡就要失敗，
    而不是讓歸屬寫成「回退後那個專案」的（P0-4）。
    """
    from pcmef.console.launch import action_context
    from pcmef.platform.capabilities import require_capability
    from pcmef.platform.pipeline import build_definition
    from pcmef.platform.runs import digest_of

    context, profile = action_context()

    # 封存的專案不得啟動任何執行。這一條在行程起跑前就要擋（P0-2）。
    require_capability(context.project, RUN_SIMULATION, profile)

    definition = build_definition(
        getattr(profile, "extra", {}).get("pipeline_template")
        or context.project.template,
        _RunPipelineContext(context.paths.freeze, profile),
    )
    design_digest = ""
    if profile is not None:
        design = _profile_registry_for_runs().read_design(
            context.project_id, profile.profile_id
        )
        design_digest = digest_of(design.to_json() if design else None)

    return {
        "project_id": context.project_id,
        "project_name": context.display_name,
        "profile_id": getattr(profile, "profile_id", ""),
        "profile_name": getattr(profile, "display_name", ""),
        "profile_version": getattr(profile, "version", ""),
        "profile_state": getattr(profile, "state", ""),
        "design_digest": design_digest,
        "definition": definition,
        "artifact_roots": context.paths.as_dict(),
    }


def _write_attribution(run_dir, run_id: str, identity) -> None:
    """把身分落盤。**失敗必須往外拋** —— 呼叫端要據此拒絕啟動。

    連同完整的 pipeline semantics 快照一起寫：只存 stage id 的話，
    日後 provider 改了文案與 I/O 說明，歷史 run 會被新的定義重新解釋
    （P1-1）。
    """
    from pcmef.platform.runs import RunAttribution, digest_of, write_attribution

    definition = identity["definition"]
    write_attribution(run_dir, RunAttribution(
        run_id=run_id,
        project_id=identity["project_id"],
        project_name=identity["project_name"],
        profile_id=identity["profile_id"],
        profile_name=identity["profile_name"],
        profile_version=identity["profile_version"],
        profile_state=identity["profile_state"],
        design_digest=identity["design_digest"],
        pipeline_id=definition.pipeline_id,
        pipeline_digest=digest_of(definition.to_json()),
        stage_ids=definition.stage_ids,
        artifact_roots=identity["artifact_roots"],
        pipeline_snapshot=definition.to_json(),
    ))


def _profile_registry_for_runs():
    from pcmef.console.project_routes import _profile_registry

    return _profile_registry()


def _require_run_ownership(run_id: str):
    """這次 run 是否屬於目前 Project。**後端強制，不是 UI 過濾。**

    回傳 (attribution, owned)。不擁有時呼叫端一律 404 —— 直接貼 URL
    看別的專案的執行紀錄，與從清單點進去是同一件事。
    """
    from pcmef.console.project_routes import request_context
    from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID
    from pcmef.platform.runs import owned_by, read_attribution

    runner = _runner()
    attribution = read_attribution(runner.run_dir(run_id))
    context = request_context()
    # started_at 決定這筆 run 落在歸屬邊界的哪一邊。邊界之後沒有歸屬
    # 的 run 是孤兒，不得因為「沒有 attribution」就歸給碩論（P1-4）。
    started_at = ""
    try:
        started_at = runner.get(run_id).started_at
    except Exception:  # noqa: BLE001
        pass
    from pcmef.platform.runs import AttributionBoundaryError, attribution_boundary

    # 邊界壞掉時傳空字串，而不是補一個時間。空邊界讓 owned_by() 對每一筆
    # **沒有歸屬**的 run 都回 False —— 有歸屬的仍然照常判定，因為它們
    # 根本不需要邊界。無法分辨 legacy 與孤兒的時候，一律當孤兒。
    try:
        boundary = attribution_boundary(runner.run_root)
    except AttributionBoundaryError:
        boundary = ""

    return attribution, owned_by(
        attribution, context.project_id,
        legacy_project_id=LEGACY_THESIS_PROJECT_ID,
        started_at=started_at,
        boundary=boundary,
    )


def _snapshot_definition(attribution):
    """由歸屬還原這筆 run 啟動當下的流程定義。**完全不問 provider。**

    先前這裡只用 `stage_ids` 還原順序，顯示名稱卻向 `build_definition()`
    要 —— 那等於用今天的 provider 去解釋昨天的執行：改過的節點文案、
    Input/Process/Output 說明與 artifact 角色會套到歷史 run 上，而畫面
    不會說它被改寫過（P1-6）。完整快照已經存在 `pipeline_snapshot`，
    這裡只要讀它。

    快照讀不出來時回 None，由呼叫端退回「沒有可還原的流程」——
    **不得改用 live 定義補**，那正是要避免的重新解釋。
    """
    from pcmef.platform.pipeline.models import (
        PipelineDefinition, PipelineDefinitionError, PipelineStage,
    )

    if attribution is None:
        return None
    snapshot = dict(getattr(attribution, "pipeline_snapshot", None) or {})
    if snapshot:
        try:
            return PipelineDefinition.from_json(snapshot)
        except (PipelineDefinitionError, ValueError, TypeError, KeyError):
            pass
    if attribution.stage_ids:
        # 平台化早期的 run 只留了 stage_ids。用 id 當名稱顯示，
        # **不向 provider 借文案** —— 借來的文案是今天的。
        return PipelineDefinition(
            pipeline_id=attribution.pipeline_id or "snapshot",
            display_name=attribution.pipeline_id or "啟動當下的流程",
            note="這筆執行只留下 stage 順序，沒有完整的流程快照。",
            stages=tuple(
                PipelineStage(stage_id=sid, display_name=sid)
                for sid in attribution.stage_ids
            ),
        )
    return None


def _stage_progress(run_dir, attribution=None):
    """這一次 run 的 stage 進度。讀事件，不寫事件。

    **依這筆 run 自己的 pipeline 快照重播**，不是目前 UI 選中的那一份。
    用當下選擇去解讀歷史，會讓同一筆 run 在 A 專案下有七個 stage、
    切到 B 之後變成三個 —— 歷史被現在改寫（audit P0-1）。
    """
    try:
        from pcmef.platform.runs import build_progress, read_events

        definition = _snapshot_definition(attribution)
        snapshot = definition is not None
        if definition is None:
            # 沒有歸屬的 legacy run。只有這一種情況才問 provider，
            # 而且畫面會標明這不是啟動當下的定義（snapshot=False）。
            from pcmef.platform.pipeline import build_definition

            definition = build_definition(None, None)

        events, skipped = read_events(run_dir)
        if not events:
            return {
                "definition": definition, "progress": None,
                "available": False, "snapshot": snapshot,
            }
        return {
            "definition": definition,
            "progress": build_progress(definition, events, skipped_lines=skipped),
            "available": True,
            "snapshot": snapshot,
        }
    except Exception:  # noqa: BLE001 - Run 頁不得因進度讀取失敗而 500
        return {"definition": None, "progress": None,
                "available": False, "snapshot": False}


class _RunPipelineContext:
    __slots__ = ("freeze_dir", "profile")

    def __init__(self, freeze_dir, profile) -> None:
        self.freeze_dir = freeze_dir
        self.profile = profile


def _project_name() -> str:
    """目前專案名稱，供 breadcrumb 使用。

    取不到時回空字串：breadcrumb 會少一層，而不是顯示錯的專案名稱。
    """
    try:
        from pcmef.console.project_routes import request_context

        return request_context().display_name
    except Exception:  # noqa: BLE001 - breadcrumb 不得讓整頁 500
        return ""

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

    # 執行紀錄與搜尋已移到 Results（P2-2）：Run 首頁只回答「我要跑什麼」。
    # active 仍留在這裡 —— 「有東西正在跑」是決定要不要再按一次的必要資訊。
    active = next((r for r in _runner().list_runs() if not r.finished), None)
    return render_template(
        "console.html",
        **nav_context("run"),
        breadcrumb=breadcrumb(("實驗 Run", None), project_name=_project_name()),
        csrf_token=token,
        presets=PRESETS,
        classes=current_app.config.get("PCMEF_CONSOLE_CLASSES", []),
        active=active,
    )


def _not_found(what: str, run_id: str | None = None, kind: str = "run") -> str:
    """找不到的東西用一頁說明，不是一段 JSON。

    這一頁刻意**只**處理「找不到」。把伺服器錯誤也導過來的話，一次 500
    會被畫成「你可能打錯網址」，而那正好會讓真正的故障被略過。
    """
    from flask import render_template

    return render_template(
        "not_found.html",
        **nav_context("results"),
        breadcrumb=breadcrumb(("結果 Results", url_for("results.page")),
                              ("找不到", None), project_name=_project_name()),
        what=what, run_id=run_id, kind=kind,
    )


@blueprint.get("/console/runs/<run_id>")
@blueprint.get("/console/runs/<run_id>/<section>")
def run_page(run_id: str, section: str = "overview"):
    """單次 run 的六個分頁（SAI §20 第二層）。

    一個 view 分派六個 section，而不是六個 view：它們共用同一份
    record、availability 與 breadcrumb，拆開只會讓那三件事複製六份。
    """
    from flask import abort, render_template, session

    from pcmef.console import run_view
    from pcmef.console.navigation import RUN_SECTIONS, run_subnav

    if section not in {s.key for s in RUN_SECTIONS}:
        abort(404)

    runner = _runner()
    try:
        record = runner.get(run_id)
    except RunnerError:
        # 打錯網址、或紀錄被刪掉之後回到書籤，都是**正常情境**。先前這裡讓
        # RunnerError 冒到 errorhandler，於是瀏覽器上出現一段 400 JSON ——
        # 狀態碼也錯了（找不到是 404，不是請求格式錯誤）。
        return _not_found(f"執行紀錄 {run_id} 不存在。"), 404

    # 歸屬檢查在後端，不是清單過濾。直接貼別的專案的 run URL
    # 與從清單點進去是同一件事，兩者都必須被擋（audit P0-2）。
    attribution, owned = _require_run_ownership(run_id)
    if not owned:
        return _not_found(
            f"執行紀錄 {run_id} 不屬於目前的 Project。"
            "切換到它所屬的 Project 才能檢視。"
        ), 404

    run_dir = runner.run_dir(run_id)
    # record 一路傳下去：formal run 的報告寫在共用的 canonical 位置，
    # 沒有它就分不出「這次寫的」與「上一次留下的」（P0-1）。
    available = run_view.availability(run_dir, record.kind, record)

    context: dict = {
        "record": record,
        "attribution": attribution,
        "section": section,
        "subnav": run_subnav(run_id, section, available),
        "available": available,
    }

    if section == "overview":
        bundle = load_results(run_dir / "artifacts", run_id)
        curves_svg = energy_svg = ""
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
        context.update(
            bundle=bundle, curves_svg=curves_svg, energy_svg=energy_svg,
            initial_log=runner.tail(run_id)[0],
            # formal run 的科學結果不在這個目錄底下。指標存在時要說出來，
            # 否則使用者會以為刪掉這筆紀錄就等於刪掉報告（P0-1）。
            formal_pointer=run_view.formal_pointer(run_dir),
        )
    elif section == "trace":
        context["trace"] = run_view.trace_view(
            run_dir,
            condition=request.args.get("condition", "").strip(),
            route=request.args.get("route", "").strip(),
        )
    elif section == "inputs":
        context["inputs"] = run_view.inputs_view(run_dir, record)
        # 「餵進去的是什麼」先前只有參數與資料集摘要 —— 看不到任何一筆
        # 實際的資料長什麼樣。gallery 補上這一層。
        context["gallery"] = run_view.case_gallery(
            run_dir,
            condition=request.args.get("condition", "").strip(),
            family=request.args.get("family", "").strip(),
        )
    elif section == "intermediate":
        context["intermediate"] = run_view.intermediate_view(run_dir, record)
    elif section == "outputs":
        context["outputs"] = run_view.outputs_view(run_dir, record)
    elif section == "cost":
        context["cost"] = run_view.cost_view(run_dir, record.kind, record)
    else:
        context["artifacts"] = run_view.artifacts_view(run_dir)

    # Stage 進度：由 executor 寫下的事件重播而來。**前端不估算任何進度。**
    # 沒有事件檔就是沒有 stage 級記錄（舊的 run 都是這樣），
    # 畫面會說明這件事而不是顯示一條假的進度條。
    context["stage_progress"] = _stage_progress(run_dir, attribution)

    label = next(s.label for s in RUN_SECTIONS if s.key == section)
    return render_template(
        "console_run.html",
        **nav_context("results"),
        breadcrumb=breadcrumb(
            ("結果 Results", url_for("results.page")),
            (run_id, url_for("console.run_page", run_id=run_id)),
            (label, None),
            project_name=_project_name(),
        ),
        csrf_token=session.get(CSRF_SESSION_KEY, ""),
        **context,
    )


@blueprint.get("/console/runs/<run_id>/trace/<case_id>")
def case_page(run_id: str, case_id: str):
    """單一 case 的完整 decision trace（SAI §20 第三層）。**唯讀。**"""
    from flask import render_template

    from pcmef.console import run_view
    from pcmef.console.navigation import run_subnav
    from pcmef.experiments.decision_trace import ARM_LABELS

    # 直接貼 case URL 也要驗歸屬（audit P0-2）。
    _attribution, _owned = _require_run_ownership(run_id)
    if not _owned:
        return _not_found(f"執行紀錄 {run_id} 不屬於目前的 Project。"), 404

    runner = _runner()
    try:
        record = runner.get(run_id)
    except RunnerError:
        return _not_found(f"執行紀錄 {run_id} 不存在。"), 404

    # 歸屬檢查在後端，不是清單過濾。直接貼別的專案的 run URL
    # 與從清單點進去是同一件事，兩者都必須被擋（audit P0-2）。
    attribution, owned = _require_run_ownership(run_id)
    if not owned:
        return _not_found(
            f"執行紀錄 {run_id} 不屬於目前的 Project。"
            "切換到它所屬的 Project 才能檢視。"
        ), 404

    run_dir = runner.run_dir(run_id)
    trace = run_view.case_view(run_dir, case_id)
    if trace is None:
        # 缺 case 是 trace PARTIAL 的正常後果，不是系統故障。頁面要說得出
        # 「這一列的 trace 沒有寫出來」並給回流程追蹤的路。
        return _not_found(
            f"{run_id} 沒有 {case_id} 的 decision trace。",
            run_id=run_id, kind="case",
        ), 404

    return render_template(
        "case_trace.html",
        **nav_context("results"),
        breadcrumb=breadcrumb(
            ("結果 Results", url_for("results.page")),
            (run_id, url_for("console.run_page", run_id=run_id)),
            ("流程追蹤 Trace",
             url_for("console.run_page", run_id=run_id, section="trace")),
            (case_id, None),
            project_name=_project_name(),
        ),
        record=record,
        section="trace",
        subnav=run_subnav(
            run_id, "trace", run_view.availability(run_dir, record.kind, record)
        ),
        trace=trace,
        # 臂的顯示順序。trace JSON 以 sort_keys=True 落盤（為了逐 byte
        # 可重現），因此讀回來的 dict 是字母序 —— G3, G5, G4, G2, G1。
        # 順序在這裡由 ARM_LABELS 還原，而不是在樣板裡寫死一份。
        arm_order=[key for key, _tag, _definition in ARM_LABELS],
        neighbours=run_view.case_neighbours(run_dir, case_id),
        # 只有真的呼叫過 agent 才算得出隔離矩陣。未 escalate 或 dry-run
        # 的 artifacts 是空的，這時給 None 讓樣板說明原因，而不是渲染一張
        # 全部 absent 的表 —— 那看起來會像隔離失敗，而不是沒有發生。
        #
        # cache 命中是第三種：投影在這一次沒有發生，但在產生這把鑰匙的那一次
        # 發生過。給它 cache root，讓那一次的紀錄也能攤在同一頁上（NOTE-074）。
        agents=run_view.agents_view(
            trace,
            cache_root=current_app.config.get("PCMEF_FORMAL_AGENT_CACHE")
            or DEFAULT_CACHE_ROOT,
        ),
    )


@blueprint.get("/console/cache/<cache_key>")
def cache_entry_page(cache_key: str):
    """content-addressed cache 裡一把鑰匙的六份 artifact（SAI §48）。**唯讀。**

    快取以內容定址，刻意與 case 身分無關 —— 因此這一頁不掛在某個 run 底下。
    同一把鑰匙可能同時是好幾次 run 的來源，掛進單一 run 會暗示它專屬於那次。

    cache 位置取**伺服器端設定**，不再接受 `?root=`。兩個理由，都不是風格
    問題：
      1. `root` 先前完全沒有限制，而 cache_key 只被限制成 64 位十六進位 ——
         合起來就是一條「讀取任意含 manifest.json 目錄」的路徑。
      2. 正式執行的 cache 由 PCMEF_FORMAL_AGENT_CACHE 決定。畫面若能指向
         另一個 root，使用者看到的就可能不是這次 run 真正用的那一份，
         而兩者長得一模一樣。
    先前寫死 DEFAULT_CACHE_ROOT 也有第二個症狀：cache root 一經設定到別處，
    case 頁「追溯到最初那一次 Agent 執行」就是一條必然 404 的死連結。
    """
    import re

    from flask import abort, render_template

    from pcmef.console import run_view

    root = Path(
        current_app.config.get("PCMEF_FORMAL_AGENT_CACHE") or DEFAULT_CACHE_ROOT
    )
    entry = run_view.cache_entry_view(root, cache_key)
    if entry is None:
        abort(404)

    # 從哪一筆 case 過來的。**純導覽用**：不影響顯示的任何內容，只是讓這一頁
    # 不成為死路。快取以內容定址、與 case 身分無關，所以它不能掛在某個 run
    # 底下 —— 但「我剛剛是從哪裡點進來的」仍然應該回得去。
    #
    # 兩個值都經過格式白名單再交給 url_for：它們來自 query string，
    # 不驗就是一條把任意字串寫進頁面連結的路徑。
    back = None
    from_run = request.args.get("from_run", "")
    from_case = request.args.get("from_case", "")
    if re.fullmatch(r"[0-9A-Za-z._-]{1,64}", from_run) and re.fullmatch(
        r"case_[0-9]{1,8}", from_case
    ):
        back = {
            "run_id": from_run,
            "case_id": from_case,
            "url": url_for("console.case_page", run_id=from_run, case_id=from_case),
        }

    crumbs = [("結果 Results", url_for("results.page"))]
    if back:
        crumbs += [
            (back["run_id"], url_for("console.run_page", run_id=back["run_id"])),
            (back["case_id"], back["url"]),
        ]
    crumbs += [("Agent cache", None), (cache_key[:12], None)]

    return render_template(
        "cache_entry.html",
        **nav_context("results"),
        breadcrumb=breadcrumb(*crumbs),
        entry=entry,
        back=back,
    )


@blueprint.get("/console/runs/<run_id>/trace/previews/<filename>")
@requires_run_ownership
def trace_preview(run_id: str, filename: str):
    """提供某次 run 的 RGB preview PNG。

    只服務該 run 自己的 `artifacts/trace/previews/`，且檔名經過白名單 ——
    這個端點吃 URL 參數，沒有限制的話就是一條讀取任意檔案的路徑。
    preview 是 run 當下產生的 artifact，不是對正式資料集的存取。
    """
    import re

    from flask import abort, send_from_directory

    from pcmef.console import run_view

    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.png", filename):
        abort(404)
    # formal run 的 preview 落在 canonical 目錄，不在 run 目錄底下 ——
    # 走同一個解析器，否則 case 頁的圖會對著一個空目錄（P0-1）。
    folder = run_view.artifact_root(_runner().run_dir(run_id)) / "trace" / "previews"
    if not folder.is_dir():
        abort(404)
    return send_from_directory(folder.resolve(), filename)


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

    # 身分先於行程。**順序由 console.launch 統一決定**，這裡不再自己
    # 排一次 —— 三個啟動端點各排一次的結果，是其中兩個忘了寫歸屬。
    from pcmef.console.launch import LaunchRefused, launch_run

    try:
        record = launch_run(
            _runner(), RunSpec(kind=kind, params=params),
            capability=RUN_SIMULATION,
        )
    except LaunchRefused as error:
        return _deny_action(str(error), error.status)

    if request.form:
        return redirect(url_for("console.run_page", run_id=record.run_id))
    return jsonify(record.to_json()), 201


@blueprint.post("/api/console/runs/<run_id>/figures")
@requires_run_ownership
def export_figures(run_id: str):  # noqa: D401

    """由這次 run 的報告產出論文用圖。**唯讀重繪，不重算任何指標。**

    沿用 `pcmef.reporting.figures` —— 與 `pcmef figures export` 是同一個
    renderer。在這裡另寫一份繪圖程式，畫出來的數字就會與 CLI 產的那一份
    分岔，而分岔的圖看起來完全正常。

    圖落在這次 run 自己的 artifact root 底下的 `figures/`：它們是那一份
    報告的衍生物，跟著它走才不會有人拿 A 的圖配 B 的報告。
    """
    from flask import jsonify

    from pcmef.console import run_view

    _guard()
    runner = _runner()
    try:
        record = runner.get(run_id)
    except RunnerError:
        return jsonify({"error": f"run {run_id!r} not found"}), 404

    run_dir = runner.run_dir(run_id)
    report, name, attribution = run_view.report_attribution(run_dir, record)
    if report is None:
        return jsonify({
            "error": "這次執行沒有可用的報告，無法出圖。",
            "reason": attribution.get("reason", ""),
        }), 400

    try:
        from pcmef.reporting import figures
    except ImportError as error:
        return jsonify({
            "error": f"matplotlib 未安裝：{error}",
            "hint": "pip install -e '.[figures]'",
        }), 501

    out_dir = run_view.artifact_root(run_dir) / "figures"
    result = figures.export_all(report, out_dir)
    result["source_report"] = name
    result["attribution"] = attribution.get("kind")
    if request.form:
        return redirect(
            url_for("console.run_page", run_id=run_id, section="outputs")
        )
    return jsonify(result), 200


@blueprint.post("/api/console/runs/<run_id>/delete")
def delete_run(run_id: str):
    """刪掉一次執行紀錄。執行中的由 runner 拒絕。

    刪除別的專案的紀錄與檢視它一樣，都要先擁有它（audit P0-2）。
    """
    _guard()
    _attribution, _owned = _require_run_ownership(run_id)
    if not _owned:
        return jsonify({"error": "run does not belong to the current project"}), 404
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

    # 這個端點會啟動一支真的會寫 freeze/llm_runtime.lock.json 的 CLI。
    # 能不能寫由**後端能力**決定，不是由畫面上有沒有那顆按鈕決定：
    # 一個 Blank Project 先前可以直接 POST 進來動到碩論的 lock（P0-5）。
    #
    # 走與其他啟動端點同一個交易：這一筆同樣要留下「是誰、在哪一份
    # Profile 底下按的凍結」。先前它直接 runner.start()，寫出來的 lock
    # 事後說不出是哪個 Project 觸發的（P0-1）。
    from pcmef.console.launch import LaunchRefused, launch_run

    needed = LLM_RUNTIME_FREEZE if freeze else LLM_SNAPSHOT_READ
    try:
        record = launch_run(
            _runner(),
            RunSpec(
                kind="llm_snapshot",
                params={"freeze": freeze},
                label="凍結 llm_runtime.lock" if freeze else "檢查（不寫入）",
            ),
            capability=needed,
        )
    except LaunchRefused as error:
        return _deny_action(str(error), error.status)

    if request.form:
        return redirect(url_for("console.run_page", run_id=record.run_id))
    return jsonify(record.to_json()), 201


@blueprint.get("/api/console/runs/<run_id>/status")
@requires_run_ownership
def run_status(run_id: str):
    return jsonify(_runner().get(run_id).to_json())


@blueprint.get("/api/console/runs/<run_id>/stream")
@requires_run_ownership
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
