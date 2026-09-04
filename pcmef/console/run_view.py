# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes 的 run_page 呼叫；讀單次 run 目錄下的
#         run.json、artifacts/ 與 artifacts/trace/。**唯讀，不寫入。**
# 檔案路徑: pcmef/console/run_view.py
# 產生時間: 2026-09-03 12:10 +08:00
# 版本: v0.2.0
# 功能說明: 把一次 run 的目錄整理成六個分頁各自需要的內容，
#           並判斷哪些分頁對這次 run 真的有東西。
# 模組定位: SAI v0.6.0 §20 第二層的資料層。它「不是」決策路徑的一部分 ——
#           所有內容都是事後讀檔，讀不到就說讀不到。
# 主要責任:
#   1. availability() 判斷六個分頁各自有沒有內容
#   2. inputs_view / intermediate_view / outputs_view / artifacts_view
#   3. trace_view() 讀 trace_index.json 並支援條件與路由篩選
#   4. 沒有內容時回傳「為什麼沒有」，而不是空字典
#   5. agent_isolation() 由實際 payload 算角色隔離矩陣
#   6. support_chain() 還原 s_A 的形成過程
# 維護提醒:
#   - 不得在讀不到檔時拋例外。一次 sim_smoke 本來就沒有 trace，
#     那是正常狀態而不是錯誤；分頁要說明原因，不是顯示 500。
#   - 不得讓本模組寫入任何東西。它在 run 結束後才被呼叫，
#     而那些檔案是那次 run 的證據。
#   - agent_isolation() 必須只讀 input_payload。改成查
#     ROLE_EVIDENCE_CONTRACT 就失去意義 —— 那會變成契約自我證明，
#     而契約與實作不一致正是它要抓的東西（NOTE-067）。
#   - v0.2.0 新增：agent_isolation / support_chain，對應 P2-5。
#   - v0.1.0 新增：首版，對應 P2-4。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_run_sections.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "availability",
    "trace_view",
    "case_view",
    "case_neighbours",
    "agents_view",
    "agent_isolation",
    "role_detail",
    "support_chain",
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


#: 隔離矩陣要追蹤的欄位，以及它們在畫面上的意義。
#:
#: 這張表是「角色隔離」唯一可被檢驗的形式：它由**實際送出的 payload**
#: 算出來，不是複述 ROLE_EVIDENCE_CONTRACT_V2。契約說某個角色不該看到
#: 什麼是宣告；這張表證明它真的沒看到（NOTE-067）。
_ISOLATION_ROWS: tuple[tuple[str, str, str], ...] = (
    ("image", "RGB 影像", "只有需要看圖的角色收得到"),
    ("tof_summary", "ToF 逐通道摘要", "raw ToF 證據"),
    ("observation_brief", "Observation Brief", "中性觀察摘要"),
    ("calibrated_class_probabilities", "p(y|x)", "分類器的四類分布"),
    ("modality_reliability", "q_m", "感測可靠度"),
    ("sensing_quality_cues", "Q_m", "原始感測品質"),
    ("predictive_entropy", "U_m", "預測熵"),
    ("cross_modal", "D（跨模態分歧）", "只有仲裁者需要"),
    ("anonymous_proposals", "兩份匿名意見", "仲裁的輸入"),
    ("gate_route", "route", "benchmark metadata，任何角色都不該收到"),
)

#: 模態限定欄位裡，代表「哪一側」的鍵。
_MODALITY_KEYS = ("vision", "tof", "q_vision", "q_tof", "Q_vision", "Q_tof",
                  "U_vision", "U_tof")


def _field_presence(payload: Mapping[str, Any], field: str, has_image: bool) -> str:
    """某個欄位在這份 payload 裡的狀態。

    回傳 `absent` / `present`，或模態限定時回傳實際帶了哪一側 ——
    「physics 收到 p(y|x)」與「physics 只收到 ToF 那一側的 p(y|x)」
    是兩件不同的事，混在一起就看不出隔離。
    """
    if field == "image":
        return "present" if has_image else "absent"
    block = payload.get(field)
    if block is None:
        return "absent"
    if isinstance(block, Mapping):
        sides = [k for k in _MODALITY_KEYS if k in block]
        if sides:
            return "、".join(sides)
    return "present"


def agent_isolation(artifacts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """由實際 payload 算出角色隔離矩陣。"""
    roles = [str(a.get("role", "")) for a in artifacts]
    rows = []
    for field, label, meaning in _ISOLATION_ROWS:
        cells = [
            _field_presence(
                a.get("input_payload", {}), field, bool(a.get("received_image"))
            )
            for a in artifacts
        ]
        # 全部角色都沒有的欄位仍然列出：`gate_route` 到處都是 absent
        # 正是它要證明的事。
        rows.append({
            "field": field, "label": label, "meaning": meaning, "cells": cells,
        })
    return {"roles": roles, "rows": rows}


#: payload 的信封欄位：每個角色都一樣，逐欄位列出只是四倍的雜訊。
#: 它們改成在角色標題列顯示一次。
_ENVELOPE_FIELDS = ("role", "schema_version", "class_order",
                    "evidence_contract_version", "representation_mode")


def _compact(value: Any, limit: int = 110) -> str:
    """一行 JSON，過長就截斷 —— 完整內容另外收在 <details> 裡。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def role_detail(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """單一角色實際收到什麼、送回什麼。

    `_meaning` 是 payload 內建的自述欄位，這裡當成該欄位的說明來用，
    而不是把它當雜訊濾掉 —— 它本來就是寫給讀 payload 的人看的。
    """
    payload = artifact.get("input_payload") or {}
    fields = []
    for key in sorted(payload):
        if key in _ENVELOPE_FIELDS:
            continue
        block = payload[key]
        meaning = block.get("_meaning") if isinstance(block, Mapping) else None
        shown = (
            {k: v for k, v in block.items() if k != "_meaning"}
            if isinstance(block, Mapping) else block
        )
        fields.append({
            "key": key,
            "meaning": meaning,
            "compact": _compact(shown),
            "full": json.dumps(shown, ensure_ascii=False, indent=2, sort_keys=True),
        })
    output = artifact.get("validated_output") or {}
    return {
        "role": artifact.get("role"),
        "received_image": bool(artifact.get("received_image")),
        "image_digests": artifact.get("image_digests") or [],
        "attempt_count": artifact.get("attempt_count"),
        "class_order": payload.get("class_order") or [],
        "representation_mode": payload.get("representation_mode"),
        "contract_version": payload.get("evidence_contract_version"),
        "n_fields": len(fields),
        "fields": fields,
        "output": json.dumps(
            {k: v for k, v in sorted(output.items()) if k != "schema_version"},
            ensure_ascii=False, indent=2,
        ),
    }


def support_chain(artifacts: Sequence[Mapping[str, Any]],
                  final: Mapping[str, Any]) -> dict[str, Any] | None:
    """s_A 的形成：仲裁者的原始 support → 正規化 → 加 epsilon 的機率。

    分三步顯示是因為每一步都可能是問題所在：原始總和偏離 100 是模型沒有
    遵守指示，正規化後才是 s_A 的尺度，而 core layer 的 EPS_S 只穩定
    合法的非零 support（NOTE-060）。
    """
    arbitration = next(
        (a for a in artifacts if str(a.get("role")) == "arbitration_agent"), None
    )
    if arbitration is None:
        return None
    raw = (arbitration.get("raw_output") or {}).get("class_support") or {}
    validated = arbitration.get("validated_output") or {}
    # s_a 是照 class_order 排的向量，畫面上的欄位卻依類別名排序。直接並排
    # 會得到一張欄位對不上標頭的表，所以在這裡先改成以類別名為鍵。
    class_order = (arbitration.get("input_payload") or {}).get("class_order") or []
    s_a = final.get("s_a") or []
    keyed = {
        str(name): s_a[i] for i, name in enumerate(class_order) if i < len(s_a)
    }
    normalised = validated.get("class_support") or {}
    # ε_s 的位移通常落在小數第八位，六位小數的表格上 ② 和 ③ 會長得一模一樣。
    # 直接把量到的最大位移印出來，比讓人以為兩列是同一個數字誠實。
    shift = max(
        (abs(keyed[k] - normalised[k] / 100.0) for k in keyed if k in normalised),
        default=None,
    )
    return {
        "raw": raw,
        "raw_sum": validated.get("support_sum_before_normalisation"),
        "within_tolerance": validated.get("support_sum_within_tolerance"),
        "normalised": normalised,
        "s_a": keyed,
        "eps_shift": shift,
        "conflict_tag": validated.get("conflict_tag"),
    }


def agents_view(trace: Mapping[str, Any]) -> dict[str, Any] | None:
    """一筆 case 的多代理段落。沒有真的呼叫過就回 None。

    未 escalate 與 dry-run 都沒有 artifacts。這兩種情況要由樣板說明
    「為什麼沒有」，而不是在這裡湊出一張空表。
    """
    execution = trace.get("agent_execution") or {}
    artifacts = execution.get("artifacts") or []
    if not execution.get("invoked") or not artifacts:
        return None
    return {
        "isolation": agent_isolation(artifacts),
        "roles": [role_detail(a) for a in artifacts],
        "support": support_chain(artifacts, trace.get("final") or {}),
        "total_attempts": sum(int(a.get("attempt_count") or 0) for a in artifacts),
    }


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
