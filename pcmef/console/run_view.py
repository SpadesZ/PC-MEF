# PC-MEF Research System source maintenance contract
# 上下游: 由 console.routes 的 run_page 呼叫；讀單次 run 目錄下的
#         run.json、artifacts/ 與 artifacts/trace/。**唯讀，不寫入。**
# 檔案路徑: pcmef/console/run_view.py
# 產生時間: 2026-09-03 12:10 +08:00
# 版本: v0.3.0
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
#   7. cost_view() 逐角色 token 用量與據 PRICING 換算的成本
#   8. cache_entry_view() 讀 content-addressed cache 的六份 artifact
# 維護提醒:
#   - 不得在讀不到檔時拋例外。一次 sim_smoke 本來就沒有 trace，
#     那是正常狀態而不是錯誤；分頁要說明原因，不是顯示 500。
#   - 不得讓本模組寫入任何東西。它在 run 結束後才被呼叫，
#     而那些檔案是那次 run 的證據。
#   - agent_isolation() 必須只讀 input_payload。改成查
#     ROLE_EVIDENCE_CONTRACT 就失去意義 —— 那會變成契約自我證明，
#     而契約與實作不一致正是它要抓的東西（NOTE-067）。
#   - 不得在 cost_view 內另寫一份費率。定價由 experiments.e2_cost.PRICING
#     匯入；兩份費率分岔時，畫面上的金額看起來仍然很正常（NOTE-071）。
#   - 不得把「沒有量到用量」顯示成 $0。dry run 真的沒呼叫，舊報告則是
#     量了卻沒寫出 —— 兩者都不是「花了零元」。
#   - v0.3.0 新增：cost_view / cache_entry_view，對應 P3-2 與 P3-3。
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
    "cost_view",
    "cache_entry_view",
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


def artifact_root(run_dir: Path) -> Path:
    """這次 run 的產物實際落在哪裡。

    formal run 的科學結果寫在 **canonical** 目錄而不是 run 目錄底下 ——
    否則每次 console run 都會給 formal report 一個沒人佔用的新位置，
    pre-flight 的 one-shot 檢查就永遠不會觸發（見 console.runner）。
    run 目錄裡留一份指標，這個函式就是照它走。

    指標不存在（一般探索用 run，或 2026-09-04 之前的舊紀錄）時回到
    `run_dir/artifacts`，行為與先前完全相同。
    """
    pointer = _read_json(run_dir / "formal_output.json")
    if pointer and pointer.get("canonical_out"):
        return Path(str(pointer["canonical_out"]))
    return run_dir / "artifacts"


def formal_pointer(run_dir: Path) -> dict[str, Any] | None:
    """這次 run 的 formal 指標。不是 formal run 就回 None。"""
    return _read_json(run_dir / "formal_output.json")


def _trace_index(artifacts: Path) -> dict[str, Any] | None:
    return _read_json(artifacts / "trace" / "trace_index.json")


def _report(artifacts: Path, pointer: Mapping[str, Any] | None = None
            ) -> tuple[dict[str, Any] | None, str]:
    """這次 run 的報告。

    `pointer` 存在時**只認它指名的那一個檔名**，不掃描 `_REPORT_NAMES`。
    canonical 目錄同時放得下 formal 與 dry-run 兩份報告，掃描會讓一次
    dry run 讀到別人的正式報告，或反過來。
    """
    names = (
        (Path(str(pointer.get("report", ""))).name,) if pointer else _REPORT_NAMES
    )
    for name in names:
        if not name:
            continue
        document = _read_json(artifacts / name)
        if document is not None:
            return document, name
    return None, ""


def report_attribution(
    run_dir: Path, record: Any = None
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """這次 run 的報告，以及**它是不是真的由這次 run 產生的**。

    formal run 的報告寫在 canonical 位置，而那個位置是共用的。單靠「檔案
    存在」把它算成這次 run 的產物會產生兩種假話：

      * 一次**失敗**的 formal run（exit 2，pre-flight 擋下或中途中止）
        會顯示出目錄裡既有的報告，看起來像它跑完了。
      * 兩次 dry run 之後，較早那一次的頁面會顯示較晚那一次的報告。

    因此這裡回傳 attribution：`this_run` / `not_produced` / `superseded` /
    `unknown`。判準是 run 的成敗與報告的 `created_at` 是否落在這次 run 的
    時間窗內 —— 兩者都是既有欄位，不需要新的紀錄。
    """
    pointer = formal_pointer(run_dir)
    document, name = _report(artifact_root(run_dir), pointer)

    if pointer is None:
        # 一般探索用 run：產物就在自己的目錄裡，不會有歸屬問題。
        return document, name, {"kind": "this_run", "shared_location": False}

    status = getattr(record, "status", None)
    if status == "failed" or document is None:
        return None, name, {
            "kind": "not_produced",
            "shared_location": True,
            "canonical_out": pointer.get("canonical_out"),
            "expected": pointer.get("report"),
            "reason": (
                "這次執行以 exit code 非零結束，沒有產生報告。"
                if status == "failed"
                else f"{pointer.get('report')} 不存在，這次執行沒有留下報告。"
            ),
        }

    created = str(document.get("created_at", ""))
    started = str(getattr(record, "started_at", "") or "")
    finished = str(getattr(record, "finished_at", "") or "")
    within = bool(created and started and created >= started
                  and (not finished or created <= finished))
    if created and started and not within:
        return document, name, {
            "kind": "superseded",
            "shared_location": True,
            "canonical_out": pointer.get("canonical_out"),
            "report_created_at": created,
            "run_window": f"{started[:19]} → {finished[:19] or '—'}",
            "reason": (
                "canonical 位置現在這一份報告的產生時間不在這次執行的時間窗內，"
                "代表它是另一次執行寫的。這一頁顯示的是那一份，不是這次的。"
            ),
        }
    return document, name, {
        "kind": "this_run" if within else "unknown",
        "shared_location": True,
        "canonical_out": pointer.get("canonical_out"),
        "report_created_at": created,
        "reason": "" if within else (
            "無法判斷這份報告是不是這次執行寫的：缺少可比對的時間欄位。"
        ),
    }


def availability(run_dir: Path, kind: str, record: Any = None) -> dict[str, bool]:
    """哪些分頁對這次 run 有內容。

    Overview 與 Artifacts 永遠有（至少有 log 與 run.json）；其餘依實際產物。
    """
    artifacts = artifact_root(run_dir)
    report, _, _ = report_attribution(run_dir, record)
    return {
        "overview": True,
        "trace": _trace_index(artifacts) is not None,
        "inputs": True,
        "intermediate": (artifacts / "stress").is_dir() or bool(
            list(artifacts.glob("*/dataset_manifest.json"))
        ),
        "outputs": report is not None,
        # 有報告就顯示 Cost，即使它量到的是「沒有呼叫過」——
        # 那本身就是 dry run 的重要事實，藏起來反而看不出零成本預演跑過。
        "cost": report is not None,
        "artifacts": True,
    }


# ---------------------------------------------------------------------------
# 各分頁
# ---------------------------------------------------------------------------


def trace_view(
    run_dir: Path, *, condition: str = "", route: str = "", limit: int = 200
) -> dict[str, Any]:
    """case 列表。支援依 condition 與 route 篩選。"""
    index = _trace_index(artifact_root(run_dir))
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
    written = len(index.get("cases", []))
    expected = index.get("expected_cases")
    failed = list(index.get("failed_trace_cases") or [])
    # 舊 trace 沒有這些欄位。此時不得假設 complete —— 缺欄位代表「不知道」，
    # 而把不知道畫成 complete 正是這一段要修掉的事（NOTE-073）。
    status = index.get("trace_status")
    if status is None:
        status = "unknown" if expected is None else (
            "complete" if written == expected and not failed else "partial"
        )
    return {
        "available": True,
        "reason": "",
        "run_status": index.get("run_status"),
        "trace_status": status,
        "trace_status_meaning": index.get("trace_status_meaning", ""),
        "expected_cases": expected,
        "written_cases": written,
        "failed_cases": failed,
        "missing_cases": (
            index.get("missing_cases")
            if index.get("missing_cases") is not None
            else (None if expected is None else max(int(expected) - written, 0))
        ),
        "dry_run": bool(index.get("dry_run")),
        "schema_version": index.get("trace_schema_version"),
        "note": index.get("note", ""),
        "total": written,
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
    return _read_json(artifact_root(run_dir) / "trace" / "cases" / f"{safe}.json")


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


#: §48 Cache Contract 1 的六份 artifact，順序即目錄樹的順序。
#: 從 agents.cache 匯入而不是重打一份 —— 重打的那份哪天與 §48 不一致，
#: 瀏覽器會安靜地漏掉一份 artifact 並顯示成「這個 key 不完整」。
def _artifact_names() -> tuple[str, ...]:
    from pcmef.agents.cache import AGENT_ARTIFACT_NAMES

    return AGENT_ARTIFACT_NAMES


def cache_entry_view(cache_root: Path, cache_key: str) -> dict[str, Any] | None:
    """content-addressed cache 裡某一把鑰匙的六份 artifact 與 manifest。

    `cache_key` 來自 URL，因此必須是純十六進位的 64 字元 —— 目錄名直接由它
    組成，不限制就是一條讀取任意目錄的路徑。
    """
    key = str(cache_key)
    if len(key) != 64 or not all(c in "0123456789abcdef" for c in key.lower()):
        return None
    folder = Path(cache_root) / key
    manifest = _read_json(folder / "manifest.json")
    if manifest is None:
        return None

    artifacts = []
    for name in _artifact_names():
        payload = _read_json(folder / f"{name}.json")
        artifacts.append({
            "name": name,
            "present": payload is not None,
            "body": json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                    if payload is not None else "",
        })

    # 產生這把鑰匙的那一次實際執行。命中時四個角色的投影不會重新發生，
    # 因此少了這一份，畫面就只能說「答案來自快取」而說不出當時問了什麼。
    from pcmef.agents.cache import PRODUCER_TRACE_NAME

    producer = _read_json(folder / f"{PRODUCER_TRACE_NAME}.json")
    producer_agents = None
    if producer and producer.get("artifacts"):
        records = list(producer["artifacts"])
        producer_agents = {
            "isolation": agent_isolation(records),
            "roles": [role_detail(a) for a in records],
            # s_A 的最後一步（ε_s 位移）發生在 bridge 而不是 cache 裡，
            # 因此這裡只還原到 normalised support；final 給空 dict，
            # support_chain 會據此把 s_a 那一欄留白而不是編一個值出來。
            "support": support_chain(records, {}),
            "total_attempts": producer.get("total_attempts"),
        }

    return {
        "cache_key": key,
        "folder": str(folder),
        "manifest": manifest,
        "artifacts": artifacts,
        # 六份缺一即不算命中（§48）。半套目錄要看得出來，否則使用者會以為
        # 這把鑰匙可用，而執行時卻仍然重問。
        "complete": all(a["present"] for a in artifacts),
        "producer": producer,
        "producer_agents": producer_agents,
        "producer_missing_reason": (
            "" if producer else
            "這個目錄是 2026-09-04 之前寫的，沒有 producer_trace.json。"
            "當時的 cache 只保存六份答案，四個角色實際收到的 payload 沒有"
            "被記下來，因此無法追溯。之後未命中而新寫入的鑰匙都會有。"
        ),
    }


def cost_view(run_dir: Path, kind: str, record: Any = None) -> dict[str, Any]:
    """這次執行的 token 用量與據此換算的成本。

    定價由 `experiments.e2_cost.PRICING` 匯入而不是在這裡重寫一份。
    兩份費率遲早會分岔，而分岔時畫面上的金額看起來仍然很正常。

    報告經 `report_attribution()` 取得而不是直接讀檔：canonical 位置是共用的，
    直接讀會把**別次**執行的花費算到這一次頭上。
    """
    report, _, attribution = report_attribution(run_dir, record)
    if report is None:
        return {
            "available": False,
            "reason": attribution.get("reason")
            or "這次執行沒有產生 formal report，因此沒有用量紀錄。",
            "attribution": attribution,
        }

    usage = report.get("token_usage") or {}
    if not usage.get("measured"):
        return {
            "available": False,
            # 「沒有量到」與「花了 0 元」必須分得出來。dry run 真的沒有呼叫，
            # 舊報告則是量了卻沒寫出 —— 兩者都不該顯示成 $0。
            "reason": usage.get("reason") or (
                "這份報告沒有 token_usage 欄位。dry run 不呼叫 provider，"
                "而 2026-09-04 之前的報告則是量了卻沒有寫出（NOTE-071）。"
            ),
            "dry_run": bool(report.get("dry_run")),
            "attribution": attribution,
        }

    from pcmef.experiments.e2_cost import PRICING

    totals = usage.get("totals") or {}
    billable = int(totals.get("billable_output_tokens", 0))
    prompt = int(totals.get("prompt_tokens", 0))
    completion = int(totals.get("completion_tokens", 0))
    thoughts = int(totals.get("thoughts_tokens", 0))

    input_usd = prompt / 1e6 * PRICING["input_usd_per_1m"]
    output_usd = billable / 1e6 * PRICING["output_usd_per_1m"]

    rows = []
    for role, bucket in (usage.get("by_role") or {}).items():
        role_billable = int(bucket.get("completion_tokens", 0)) + \
            int(bucket.get("thoughts_tokens", 0))
        rows.append({
            "role": role,
            "calls": int(bucket.get("calls", 0)),
            "prompt_tokens": int(bucket.get("prompt_tokens", 0)),
            "completion_tokens": int(bucket.get("completion_tokens", 0)),
            "thoughts_tokens": int(bucket.get("thoughts_tokens", 0)),
            "billable_output_tokens": role_billable,
            "usd": (int(bucket.get("prompt_tokens", 0)) / 1e6
                    * PRICING["input_usd_per_1m"]
                    + role_billable / 1e6 * PRICING["output_usd_per_1m"]),
        })
    rows.sort(key=lambda r: r["usd"], reverse=True)

    return {
        "available": True,
        "pricing": PRICING,
        "rows": rows,
        "totals": totals,
        # 只讀 candidatesTokenCount 會低估多少倍。這個比值是「不要只看
        # completion」的量化理由，比一句提醒有用。
        "thinking_share": (thoughts / billable) if billable else None,
        "underestimate_factor": (billable / completion) if completion else None,
        "input_usd": input_usd,
        "output_usd": output_usd,
        "total_usd": input_usd + output_usd,
        "agent_cache": (report.get("routing") or {}).get("agent_cache") or
                       {"enabled": False},
    }


def agents_view(
    trace: Mapping[str, Any], cache_root: Path | str | None = None
) -> dict[str, Any] | None:
    """一筆 case 的多代理段落。真的沒有發生過才回 None。

    三種來源，順序即優先序：

    1. `this_run` —— 這次 run 自己呼叫了四個角色，紀錄在 call_log 裡。
    2. `cache_producer` —— 這次命中快取，四次投影在**這一次**沒有發生，
       但在產生這把鑰匙的那一次發生過，紀錄在 producer_trace.json 裡。
       此時仍然回傳完整內容：命中省下的是一次付費，不該連帶省掉解釋能力
       （NOTE-074）。
    3. None —— 未 escalate 或 dry run，也就是**真的**沒有發生過。
       由樣板說明「為什麼沒有」，不在這裡湊一張空表。

    第 2 種先前落進第 3 種：`agents_view` 只看 artifacts，命中時 artifacts
    是空的，於是畫面顯示「未執行 cache_hit」—— 一句與事實相反的話配上一個
    沒有解釋的識別字。
    """
    execution = trace.get("agent_execution") or {}
    if not execution.get("invoked"):
        return None

    artifacts = list(execution.get("artifacts") or [])
    if artifacts:
        return _agents_block(artifacts, trace.get("final") or {}, "this_run", None)

    if execution.get("reason") != "cache_hit" or not execution.get("cache_key"):
        return None
    producer = _read_json(
        Path(cache_root or "") / str(execution["cache_key"]) / "producer_trace.json"
    ) if cache_root else None
    if not producer or not producer.get("artifacts"):
        return None
    # s_A 用**這一筆的** final：命中的是 arbitration 的答案，而 ε_s 之後的
    # 向量屬於這一次決策，兩者本來就該一起看。
    return _agents_block(
        list(producer["artifacts"]), trace.get("final") or {},
        "cache_producer", producer,
    )


def _agents_block(
    artifacts: Sequence[Mapping[str, Any]],
    final: Mapping[str, Any],
    source: str,
    producer: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "source": source,
        "producer": producer,
        "isolation": agent_isolation(artifacts),
        "roles": [role_detail(a) for a in artifacts],
        "support": support_chain(artifacts, final),
        "total_attempts": sum(int(a.get("attempt_count") or 0) for a in artifacts),
    }


def case_neighbours(run_dir: Path, case_id: str) -> dict[str, str | None]:
    """前後相鄰的 case，讓人可以逐筆翻閱而不必回列表。"""
    index = _trace_index(artifact_root(run_dir))
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
    report, _, _ = report_attribution(run_dir, record)
    dataset = report.get("dataset", {}) if report else {}
    return {
        "params": dict(getattr(record, "params", {}) or {}),
        "command": list(getattr(record, "command", []) or []),
        "dataset": dataset,
        "severity": (report or {}).get("severity"),
        "severity_source": (report or {}).get("severity_source"),
        "config_path": str(next(iter(run_dir.glob("config*.yaml")), "") or ""),
    }


def intermediate_view(run_dir: Path, record: Any = None) -> dict[str, Any]:
    """中途產生了什麼：stress set 與感知階段的中間量。"""
    artifacts = artifact_root(run_dir)
    stress = _read_json(artifacts / "stress" / "stress_manifest.json")
    report, _, _ = report_attribution(run_dir, record)
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


def outputs_view(run_dir: Path, record: Any = None) -> dict[str, Any]:
    """得到什麼結論。"""
    report, name, attribution = report_attribution(run_dir, record)
    if report is None:
        return {
            "available": False,
            "reason": attribution.get("reason") or "這次執行沒有產生 E2 報告。",
            "attribution": attribution,
        }
    return {
        "available": True,
        "reason": "",
        "filename": name,
        # 報告寫在共用的 canonical 位置時，畫面必須說得出它到底是不是
        # 這次執行寫的。單靠「檔案存在」會把別次的結果算到這一次頭上。
        "attribution": attribution,
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
    artifacts = artifact_root(run_dir)
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
