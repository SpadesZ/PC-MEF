# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.app 註冊；呼叫 experiments.formal_service 取得
#         pre-flight 與最近一次 run 的摘要；繪製 admin/templates/formal.html。
#         **唯讀** —— 本檔沒有任何 POST 端點。
# 檔案路徑: pcmef/console/formal_routes.py
# 產生時間: 2026-09-02 16:10 +08:00
# 版本: v0.1.0
# 功能說明: Formal Research Workspace 的監控頁。顯示 identity、pre-flight
#           與最近一次執行的結果，讓「正式實驗跑到哪了」不必開終端機看。
# 模組定位: SAI v0.6.0 §19 的最小實作，且**只做 §19.1-19.8 的顯示**。
#           §19.9 的 Run Controls（啟動）不在本檔 —— 那牽涉 §208 的紅線，
#           另行處理。本檔完全唯讀，連 CSRF 都不需要，因為沒有寫入端點。
# 主要責任:
#   1. GET /formal 繪製 identity bar、pre-flight panel 與最近一次結果
#   2. GET /formal/preflight.json 供輪詢，回傳與 CLI 相同的判斷
#   3. 不重算任何 pre-flight 邏輯，一律轉呼叫 formal_service
# 維護提醒:
#   - 不得在本檔重算 pre-flight。畫面顯示的 PASS 必須與 `formal run-e2`
#     實際據以放行的是同一組判斷；兩份實作漂移時，畫面通常是比較寬鬆的
#     那一份（SAI §3.1）。
#   - 不得在本檔加入啟動端點。那條線由 console.runner 的
#     _assert_not_formal() 守著，要改必須先有 amendment。
#   - 不得把 report 整份丟給前端。traces 有幾百列，formal_service
#     已經挑好畫面需要的欄位。
#   - v0.1.0 新增：首版唯讀監控頁，對應 P0-7a。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_formal_routes.py -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, render_template, request

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


@blueprint.get("/formal")
def page():
    # dry-run 與 formal 的判準不同，兩者都要看得到：使用者要知道的是
    # 「現在能不能跑正式的」，而不只是「預演能不能跑」。
    formal = _collect("formal")
    dry = _collect("dry-run")
    return render_template(
        "formal.html",
        formal=formal["preflight"],
        dry=dry["preflight"],
        report=formal["report"],
        paths=formal["paths"],
    )


@blueprint.get("/formal/preflight.json")
def preflight_json():
    mode = request.args.get("mode", "formal")
    if mode not in ("formal", "dry-run"):
        return jsonify({"error": f"unknown mode {mode!r}"}), 400
    return jsonify(_collect(mode))
