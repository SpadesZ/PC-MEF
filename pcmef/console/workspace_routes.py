# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.app 註冊；Status 讀 audit.e1_gates 與 core.locks，
#         Results 讀 console.runner 的執行紀錄與 experiments.formal_service；
#         Pipeline 於 P2-3 填入七節點內容。**全部唯讀。**
# 檔案路徑: pcmef/console/workspace_routes.py
# 產生時間: 2026-09-03 09:40 +08:00
# 版本: v0.2.0
# 功能說明: 資訊架構重整後的三個新入口 —— Pipeline / Results / Status。
# 模組定位: SAI v0.6.0 §20。這三頁都**只讀**：Status 回答研究做到哪、
#           Results 回答跑出了什麼、Pipeline 回答系統怎麼跑。
#           要「開始跑」一律回到 Run。
# 主要責任:
#   1. GET /status  研究狀態：E1 十二道 gate、formal lock、active lineage
#   2. GET /results 執行紀錄與最近一次 formal 報告的入口
#   3. GET /pipeline 流程節點
#   4. 三頁一律經 project_routes.request_context() 取得目前專案的路徑
# 維護提醒:
#   - Status 與 Pipeline **完全唯讀**，不得加入任何 POST 端點。
#   - Results 只允許「管理執行紀錄」這一種寫入（刪掉跑過的探索紀錄，
#     那會累積上百筆）。不得在此加入任何會影響研究決策的寫入 ——
#     要啟動什麼一律回 Run，准駁由 CLI 子行程判定（AMD-008）。
#   - 不得讓 Status 重算 lock 狀態。一律經 LockStore 與 active_lineage，
#     否則畫面會出現與 CLI 不同的「研究進度」。
#   - **不得把 freeze 目錄寫死成 "freeze"。** 寫死之後，切到另一個專案
#     時 Status 仍會顯示 PC-MEF 的 lock —— 標題換了而資料沒換，
#     那在畫面上看起來完全正常。一律取自 ProjectPaths。
#   - 不得各自 new 一個 ProjectRegistry()；那會繞過 app 設定的
#     workspace root，測試因此會寫進真實的 projects/。
#   - v0.1.0 新增：首版，對應 P2-2。
#   - v0.2.0 變更：三頁改為 project-aware，freeze 目錄來自
#     ProjectPaths，對應平台化 Phase 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_navigation.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, render_template

from pcmef.console.navigation import breadcrumb, nav_context
from pcmef.console.routes import RECENT_RUN_COUNT

__all__ = ["status_blueprint", "results_blueprint", "pipeline_blueprint"]

class _LifecycleContext:
    """傳給 lifecycle provider 的唯讀輸入。

    刻意是一個小物件而不是整個 Flask request：provider 不該碰得到
    session 或 app config，否則它就有能力改變自己被判定的條件。
    """

    __slots__ = ("freeze_dir", "profile", "gate_summary")

    def __init__(self, freeze_dir, profile, gate_summary) -> None:
        self.freeze_dir = freeze_dir
        self.profile = profile
        self.gate_summary = gate_summary


def _owned_runs(runner, context, query):
    """只列出屬於目前 Project 的執行紀錄。

    **後端過濾，不是畫面過濾。** 全域 runs 根目錄本身沒有專案概念，
    因此清單必須逐筆比對歸屬；少了這一步，B 專案的 Results 會列出
    A 專案的執行紀錄（audit P0-2）。
    """
    from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID
    from pcmef.platform.runs import owned_by, read_attribution

    kept = []
    for record in runner.list_runs(query=query):
        attribution = read_attribution(runner.run_dir(record.run_id))
        if owned_by(
            attribution, context.project_id,
            legacy_project_id=LEGACY_THESIS_PROJECT_ID,
        ):
            kept.append(record)
    return kept


class _PipelineContext:
    """傳給 pipeline provider 的唯讀輸入。"""

    __slots__ = ("freeze_dir", "profile")

    def __init__(self, freeze_dir, profile) -> None:
        self.freeze_dir = freeze_dir
        self.profile = profile


status_blueprint = Blueprint("status", __name__)
results_blueprint = Blueprint("results", __name__)
pipeline_blueprint = Blueprint("pipeline", __name__)


# ---------------------------------------------------------------------------
# Status —— 研究目前做到哪裡
# ---------------------------------------------------------------------------


def _lock_summary(freeze_dir: Any) -> dict[str, Any]:
    """formal lock 的狀態。經 active lineage 解析，不自己猜目錄。

    `freeze_dir` 由目前專案的 ProjectPaths 提供 —— 這裡不再寫死
    `"freeze"`，否則切到另一個專案後，Status 仍會顯示 PC-MEF 的 lock。
    """
    from pcmef.core.active_lineage import ActiveLineageError, resolve_active_lineage

    try:
        resolved = resolve_active_lineage(freeze_dir)
    except ActiveLineageError as error:
        return {"resolved": None, "error": str(error)[:220], "locks": []}
    return {
        "resolved": resolved.to_manifest(),
        "error": None,
        "locks": [
            {"name": name, "hash": digest}
            for name, digest in sorted(resolved.lock_hashes.items())
        ],
    }


@status_blueprint.get("/status")
def page():
    from pcmef.console.project_routes import request_context

    context = request_context()
    freeze_dir = context.paths.freeze
    gate_summary = current_app.config["PCMEF_CONSOLE_GATE_SUMMARY"]()

    # 「現在在哪一步、下一步做什麼、還缺什麼」由 final_gate 的八項判定
    # **推導**，不在畫面上手寫。手寫的進度敘述會過期，而且過期時看起來
    # 完全正常 —— 那正是 STATUS.md 需要人工維護的那一段（NOTE-082）。
    #
    # freeze_dir 來自目前專案。沒有 lineage 的專案（例如剛建立的空專案）
    # 會落進下面的 except，顯示「這個專案還沒有 formal lineage」，
    # 而不是沿用 PC-MEF 的那一組。
    try:
        from pcmef.core.active_lineage import resolve_active_lineage
        from pcmef.experiments.final_gate import progress as final_progress

        progress = final_progress(
            resolve_active_lineage(freeze_dir).freeze_dir
        )
    except Exception as error:  # noqa: BLE001 - Status 是觀察頁，不得 500
        progress = {"error": f"{type(error).__name__}: {error}"}

    # Lifecycle 由 provider 依 Profile 的 template 推導 —— 階段結構通用，
    # 判準是該研究自己的。畫面上不寫死任何階段或 gate。
    from pcmef.platform.lifecycle import build_lifecycle

    profile = getattr(context.selected, "profile", None)
    lifecycle = build_lifecycle(
        getattr(profile, "extra", {}).get("lifecycle_template")
        or context.project.template,
        _LifecycleContext(freeze_dir, profile, gate_summary),
    )

    return render_template(
        "status.html",
        **nav_context("status"),
        breadcrumb=breadcrumb(
            ("研究狀態 Status", None), project_name=context.display_name
        ),
        gate_summary=gate_summary,
        lineage=_lock_summary(freeze_dir),
        progress=progress,
        lifecycle=lifecycle,
        profile=profile,
        profile_reason=getattr(context.selected, "reason", ""),
    )


# ---------------------------------------------------------------------------
# Results —— 跑出了什麼
# ---------------------------------------------------------------------------


@results_blueprint.get("/results")
def page():
    from flask import request, session

    from pcmef.admin.auth import new_csrf_token
    from pcmef.admin.routes_llm import CSRF_SESSION_KEY
    from pcmef.experiments.formal_service import latest_report

    # 刪除紀錄的表單需要 token。它保護的是「管理紀錄」這個動作，
    # 與研究決策無關 —— 那條線由 CLI 的 pre-flight 守著。
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = new_csrf_token()
        session[CSRF_SESSION_KEY] = token

    from pcmef.console.project_routes import request_context

    context = request_context()
    runner = current_app.config["PCMEF_CONSOLE_RUNNER"]
    query = request.args.get("q", "").strip()
    out_dir = current_app.config.get(
        "PCMEF_FORMAL_OUT", "outputs/perception/e2_final"
    )
    return render_template(
        "results.html",
        **nav_context("results"),
        breadcrumb=breadcrumb(
            ("結果 Results", None), project_name=context.display_name
        ),
        runs=_owned_runs(runner, context, query),
        query=query,
        recent_count=RECENT_RUN_COUNT,
        csrf_token=token,
        formal_report=latest_report(out_dir),
        formal_out=out_dir,
    )


# ---------------------------------------------------------------------------
# Pipeline —— 系統怎麼跑
# ---------------------------------------------------------------------------


@pipeline_blueprint.get("/pipeline")
def page():
    """目前 Profile 的流程定義。**節點數量由定義決定，不是固定七個。**"""
    from pcmef.console.project_routes import request_context
    from pcmef.platform.pipeline import build_definition

    context = request_context()
    profile = getattr(context.selected, "profile", None)
    definition = build_definition(
        getattr(profile, "extra", {}).get("pipeline_template")
        or context.project.template,
        _PipelineContext(context.paths.freeze, profile),
    )

    extra = dict(definition.extra)
    return render_template(
        "pipeline.html",
        **nav_context("pipeline"),
        breadcrumb=breadcrumb(
            ("流程 Pipeline", None), project_name=context.display_name
        ),
        definition=definition,
        profile=profile,
        pipeline={
            "nodes": [
                {
                    "key": stage.stage_id,
                    "label": stage.display_name,
                    "english": stage.english,
                    "summary": stage.summary,
                    "carries": stage.carries,
                    "index": index + 1,
                    "detail": stage.detail,
                    "optional": stage.optional,
                    "artifact_roles": list(stage.artifact_roles),
                    "depends_on": list(stage.depends_on),
                }
                for index, stage in enumerate(definition.stages)
            ],
            "detail_available": extra.get("detail_available", False),
            "detail_note": definition.note,
            "lineage": extra.get("lineage", {}),
            "threshold_caveat": extra.get("threshold_caveat", ""),
        },
    )
