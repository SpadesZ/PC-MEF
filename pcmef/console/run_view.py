# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes 的 run_page 呼叫；讀單次 run 目錄下的
#         run.json、artifacts/ 與 artifacts/trace/。**唯讀，不寫入。**
# 檔案路徑: pcmef/console/run_view.py
# 產生時間: 2026-09-03 12:10 +08:00
# 版本: v0.1.0
# 功能說明: 把一次 run 的目錄整理成六個分頁各自需要的內容，
#           並判斷哪些分頁對這次 run 真的有東西。
# 模組定位: SAI v0.6.0 §20 第二層的資料層。它「不是」決策路徑的一部分 ——
#           所有內容都是事後讀檔，讀不到就說讀不到。
# 主要責任:
#   1. availability() 判斷六個分頁各自有沒有內容
#   2. inputs_view / intermediate_view / outputs_view / artifacts_view
#   3. trace_view() 讀 trace_index.json 並支援條件與路由篩選
#   4. 沒有內容時回傳「為什麼沒有」，而不是空字典
# 維護提醒:
#   - 不得在讀不到檔時拋例外。一次 sim_smoke 本來就沒有 trace，
#     那是正常狀態而不是錯誤；分頁要說明原因，不是顯示 500。
#   - 不得讓本模組寫入任何東西。它在 run 結束後才被呼叫，
#     而那些檔案是那次 run 的證據。
#   - v0.1.0 新增：首版，對應 P2-4。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_run_sections.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "availability",
    "trace_view",
    "inputs_view",
    "intermediate_view",
    "outputs_view",
    "artifacts_view",
]

#: 一次 run 可能產生的報告檔名，依「正式優先」排序。
_REPORT_NAMES = ("formal_e2_report.json", "formal_e2_dry_run.json")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _trace_index(artifacts: Path) -> dict[str, Any] | None:
    return _read_json(artifacts / "trace" / "trace_index.json")


def _report(artifacts: Path) -> tuple[dict[str, Any] | None, str]:
    for name in _REPORT_NAMES:
        document = _read_json(artifacts / name)
        if document is not None:
            return document, name
    return None, ""


def availability(run_dir: Path, kind: str) -> dict[str, bool]:
    """哪些分頁對這次 run 有內容。

    Overview 與 Artifacts 永遠有（至少有 log 與 run.json）；其餘依實際產物。
    """
    artifacts = run_dir / "artifacts"
    report, _ = _report(artifacts)
    return {
        "overview": True,
        "trace": _trace_index(artifacts) is not None,
        "inputs": True,
        "intermediate": (artifacts / "stress").is_dir() or bool(
            list(artifacts.glob("*/dataset_manifest.json"))
        ),
        "outputs": report is not None,
        "artifacts": True,
    }


# ---------------------------------------------------------------------------
# 各分頁
# ---------------------------------------------------------------------------


def trace_view(
    run_dir: Path, *, condition: str = "", route: str = "", limit: int = 200
) -> dict[str, Any]:
    """case 列表。支援依 condition 與 route 篩選。"""
    index = _trace_index(run_dir / "artifacts")
    if index is None:
        return {
            "available": False,
            "reason": (
                "這次執行沒有產生 decision trace。只有 Full PC-MEF 的 E2 執行"
                "（formal 或預演）會逐 case 記錄決策過程。"
            ),
            "cases": [],
        }

    cases = list(index.get("cases", []))
    conditions = sorted({str(c.get("condition", "")) for c in cases if c.get("condition")})
    routes = sorted({str(c.get("route", "")) for c in cases if c.get("route")})
    if condition:
        cases = [c for c in cases if str(c.get("condition")) == condition]
    if route:
        cases = [c for c in cases if str(c.get("route")) == route]

    correct = sum(1 for c in cases if c.get("correct"))
    return {
        "available": True,
        "reason": "",
        "run_status": index.get("run_status"),
        "dry_run": bool(index.get("dry_run")),
        "schema_version": index.get("trace_schema_version"),
        "note": index.get("note", ""),
        "total": len(index.get("cases", [])),
        "shown": min(len(cases), limit),
        "filtered": len(cases),
        "correct": correct,
        "accuracy": (correct / len(cases)) if cases else None,
        "conditions": conditions,
        "routes": routes,
        "condition": condition,
        "route": route,
        "cases": cases[:limit],
    }


def case_view(run_dir: Path, case_id: str) -> dict[str, Any] | None:
    """單一 case 的完整 decision trace。找不到回 None，由呼叫端決定 404。"""
    safe = case_id.replace("/", "").replace("\\", "").replace("..", "")
    return _read_json(run_dir / "artifacts" / "trace" / "cases" / f"{safe}.json")


def case_neighbours(run_dir: Path, case_id: str) -> dict[str, str | None]:
    """前後相鄰的 case，讓人可以逐筆翻閱而不必回列表。"""
    index = _trace_index(run_dir / "artifacts")
    if index is None:
        return {"previous": None, "next": None}
    ids = [str(row.get("case_id")) for row in index.get("cases", [])]
    if case_id not in ids:
        return {"previous": None, "next": None}
    position = ids.index(case_id)
    return {
        "previous": ids[position - 1] if position > 0 else None,
        "next": ids[position + 1] if position + 1 < len(ids) else None,
    }


def inputs_view(run_dir: Path, record: Any) -> dict[str, Any]:
    """餵進去的是什麼：執行參數、指令，以及資料來源。"""
    artifacts = run_dir / "artifacts"
    report, _ = _report(artifacts)
    dataset = report.get("dataset", {}) if report else {}
    return {
        "params": dict(getattr(record, "params", {}) or {}),
        "command": list(getattr(record, "command", []) or []),
        "dataset": dataset,
        "severity": (report or {}).get("severity"),
        "severity_source": (report or {}).get("severity_source"),
        "config_path": str(next(iter(run_dir.glob("config*.yaml")), "") or ""),
    }


def intermediate_view(run_dir: Path) -> dict[str, Any]:
    """中途產生了什麼：stress set 與感知階段的中間量。"""
    artifacts = run_dir / "artifacts"
    stress = _read_json(artifacts / "stress" / "stress_manifest.json")
    report, _ = _report(artifacts)
    routing = (report or {}).get("routing", {})
    return {
        "available": stress is not None or bool(routing),
        "stress": None if stress is None else {
            "n_rows": len(stress.get("rows", [])),
            "counts": stress.get("counts"),
            "code_version": stress.get("code_version"),
        },
        "routing": routing,
        "statistics_config": (report or {}).get("statistics_config", {}),
    }


def outputs_view(run_dir: Path) -> dict[str, Any]:
    """得到什麼結論。"""
    report, name = _report(run_dir / "artifacts")
    if report is None:
        return {
            "available": False,
            "reason": "這次執行沒有產生 E2 報告。",
        }
    return {
        "available": True,
        "reason": "",
        "filename": name,
        "report_id": report.get("report_id"),
        "dry_run": bool(report.get("dry_run")),
        "scientific_result": bool(report.get("scientific_result")),
        "llm_arm_evaluated": bool(report.get("llm_arm_evaluated")),
        "results": report.get("results", {}),
        "per_condition": report.get("per_condition", {}),
        "worst_condition": report.get("worst_condition_macro_f1", {}),
        "worst_condition_at": report.get("worst_condition_at", {}),
        "statistics": report.get("statistics", {}),
        "worst_condition_statistics": report.get("worst_condition_statistics", {}),
        "arm_definitions": report.get("arm_definitions", {}),
        "claim_boundary": report.get("claim_boundary"),
    }


#: Artifacts 分頁一次最多列幾個檔。trace/previews 有幾百張 PNG，
#: 全部列出來只會讓頁面變成一面檔名牆。
_MAX_ARTIFACT_ROWS = 200


def artifacts_view(run_dir: Path) -> dict[str, Any]:
    """檔案落在哪裡。目錄先摺疊成一列，避免幾百個 preview 淹沒頁面。"""
    artifacts = run_dir / "artifacts"
    if not artifacts.is_dir():
        return {"available": False, "reason": "這次執行沒有產生 artifacts 目錄。",
                "entries": []}

    entries: list[dict[str, Any]] = []
    for path in sorted(artifacts.rglob("*")):
        relative = path.relative_to(artifacts)
        # 目錄底下檔案太多時只列目錄本身與計數。
        if path.is_dir():
            children = [p for p in path.iterdir() if p.is_file()]
            if len(children) > 12:
                entries.append({
                    "path": relative.as_posix() + "/",
                    "kind": "directory",
                    "size": sum(p.stat().st_size for p in children),
                    "count": len(children),
                })
            continue
        if any(
            e["kind"] == "directory" and relative.as_posix().startswith(e["path"])
            for e in entries
        ):
            continue
        entries.append({
            "path": relative.as_posix(),
            "kind": "file",
            "size": path.stat().st_size,
            "count": None,
        })

    return {
        "available": bool(entries),
        "reason": "" if entries else "artifacts 目錄是空的。",
        "root": artifacts.as_posix(),
        "total": len(entries),
        "truncated": len(entries) > _MAX_ARTIFACT_ROWS,
        "entries": entries[:_MAX_ARTIFACT_ROWS],
    }
