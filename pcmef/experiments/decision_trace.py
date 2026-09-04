# PC-MEF Research System source maintenance contract
# 上下游: 由 experiments.e2_formal 在**決策完成之後**呼叫；讀 prepared 的
#         逐列訊號與 AgentRunner.call_log；寫 <out>/trace/ 底下的
#         trace_index.json、cases/*.json 與 previews/*.png。
#         **不回傳任何值給決策路徑。**
# 檔案路徑: pcmef/experiments/decision_trace.py
# 產生時間: 2026-09-02 22:30 +08:00
# 版本: v0.1.0
# 功能說明: 把一次 run 的決策過程完整落盤 —— 感知輸出、品質訊號、可靠度、
#           路由與理由、四個 Agent 實際收到與產出的東西 —— 讓「輸入怎麼
#           一步步變成輸出」可被逐 case 檢視。
# 模組定位: **旁路觀測層**。它不參與任何決策，也不得被決策路徑讀取。
#           寫入失敗只影響 trace 本身，不影響 formal run 的結果。
# 主要責任:
#   1. CaseTrace 組出單一 case 的完整決策紀錄
#   2. TraceWriter 落盤 cases/*.json 與 previews/*.png
#   3. finalise() 原子性寫出 trace_index.json 並標記 run_status
#   4. route_explanation() 把路由條件翻成研究者讀得懂的一句話
# 維護提醒:
#   - 不得讓本模組的任何回傳值進入 decision path。trace 是觀測；
#     一旦決策依賴它，「寫 trace 失敗」就會變成「實驗失敗」（NOTE-064）。
#   - 不得由 ROLE_EVIDENCE_CONTRACT_V2 事後重建 agent 的 input_payload。
#     要證明某個角色實際收到什麼，只有記下真正送出的那一份算數。
#   - 不得把 base64 影像寫進 JSON。單張 16 KB，143 個 escalated case 會讓
#     trace 膨脹到 2 MB 以上；preview 落成 PNG，trace 只留 sha256 與路徑。
#   - 不得在 run 未完成時寫出 trace_index.json。中途失敗要留下
#     run_status=aborted 的 index，不得留下看起來像完整正式結果的東西。
#   - v0.1.0 新增：首版 decision trace，對應 NOTE-064。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_decision_trace.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_COMPLETE",
    "RUN_STATUS_ABORTED",
    "CaseTrace",
    "build_case_trace",
    "TraceWriter",
    "route_explanation",
]

#: trace 的 schema 版本。改欄位語意時必須進版，否則舊 trace 讀不回來。
TRACE_SCHEMA_VERSION = "decision_trace_v1"

#: ToF 的四個 channel。從 core.constants 匯入而不是重寫一份 ——
#: 重寫的那份哪天與 TOF_SCHEMA 不一致，trace 會標錯欄位語意。
from pcmef.core.constants import TOF_SCHEMA as TOF_CHANNELS

#: `duq_signals()` 用 "D" 當跨模態分歧的鍵，不是 "disagreement"。
#: 寫成常數而不是字面值：拼錯時 `.get()` 會安靜地回 NaN，而 NaN 在
#: 條件比較裡一律 False —— 路由說明會因此悄悄走錯分支（NOTE-064）。
DISAGREEMENT_KEY = "D"

RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETE = "complete"
RUN_STATUS_ABORTED = "aborted"

def route_explanation(
    route: str,
    q_vision: float,
    q_tof: float,
    disagreement: float,
    *,
    q_threshold: float = 0.5,
    delta: float = 0.0,
) -> dict[str, Any]:
    """把一條路由翻成研究者讀得懂的說明，並**驗證它與數值一致**。

    這一句是 trace 的重點之一：畫面上顯示 `route=escalated` 只是複述系統
    的輸出，說不出「為什麼」。

    句子由**當下的數值**推導，不是照 route 標籤挑模板。差別在哪裡：
    照標籤挑的話，一旦 route 與數值不一致（例如上游有 bug），說明會編出
    一個不成立的理由 —— 「ToF 品質過關（q_T=0.31 ≥ 0.50）」這種句子看起來
    很有說服力，卻是假的。那比沒有說明更糟。

    因此本函式先由數值算出**應該**走哪一條，再與傳入的 route 比對；
    不一致時回傳 `consistent=False` 並直說矛盾，讓畫面能標紅而不是照唸。
    """
    q_v, q_t = float(q_vision), float(q_tof)
    d, threshold, cut = float(disagreement), float(q_threshold), float(delta)

    vision_ok = q_v >= threshold
    tof_ok = q_t >= threshold
    conflicting = d > cut

    quality = (
        f"q_V={q_v:.3f}{'≥' if vision_ok else '<'}{threshold:.3f}"
        f"、q_T={q_t:.3f}{'≥' if tof_ok else '<'}{threshold:.3f}"
    )

    if vision_ok and not tof_ok:
        implied, sentence = "trust_vision", (
            f"只有 Vision 的感測品質過關（{quality}），因此採信 Vision 的分布。"
        )
    elif tof_ok and not vision_ok:
        implied, sentence = "trust_tof", (
            f"只有 ToF 的感測品質過關（{quality}），因此採信 ToF 的分布。"
        )
    elif vision_ok and tof_ok and not conflicting:
        implied, sentence = "fusion", (
            f"兩側品質都過關（{quality}），且分歧未超過門檻"
            f"（D={d:.3f} ≤ {cut:.3f}），因此以固定權重融合，不需要仲裁。"
        )
    elif vision_ok and tof_ok:
        implied, sentence = "escalated", (
            f"兩側品質都過關（{quality}），但彼此分歧超過門檻"
            f"（D={d:.3f} > {cut:.3f}），傳統證據無法自行決定，"
            "因此進入四角色證據仲裁。"
        )
    else:
        implied, sentence = "escalated", (
            f"兩側的感測品質都不過關（{quality}），傳統證據不足以支撐決定，"
            "因此進入四角色證據仲裁。"
        )

    consistent = implied == route
    if not consistent:
        sentence = (
            f"⚠ 記錄到的 route 是 {route}，但由當下數值推導應為 {implied}"
            f"（{quality}、D={d:.3f} vs δ={cut:.3f}）。"
            "兩者不一致，說明文字不可採信 —— 這是需要追查的狀況。"
        )

    return {
        "route": route,
        "implied_route": implied,
        "consistent": consistent,
        "explanation": sentence,
        "conditions": {
            "q_vision": q_v,
            "q_tof": q_t,
            "q_threshold": threshold,
            "vision_quality_passes": vision_ok,
            "tof_quality_passes": tof_ok,
            "disagreement": d,
            "disagreement_threshold": cut,
            "exceeds_disagreement_threshold": conflicting,
        },
    }


@dataclass
class CaseTrace:
    """單一 case 的完整決策紀錄。"""

    case_id: str
    row_index: int
    condition: str
    class_label: str
    physical_scene_family: str
    inputs: dict[str, Any]
    perception: dict[str, Any]
    quality: dict[str, Any]
    reliability: dict[str, Any]
    routing: dict[str, Any]
    arms: dict[str, Any]
    agent_execution: dict[str, Any]
    final: dict[str, Any]

    def to_json(self, header: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **header,
            "case_id": self.case_id,
            "row_index": self.row_index,
            "condition": self.condition,
            "class_label": self.class_label,
            "physical_scene_family": self.physical_scene_family,
            "inputs": self.inputs,
            "perception": self.perception,
            "quality": self.quality,
            "reliability": self.reliability,
            "routing": self.routing,
            "arms": self.arms,
            "agent_execution": self.agent_execution,
            "final": self.final,
        }


def build_case_trace(
    *,
    row: Mapping[str, Any],
    row_index: int,
    case_id: str,
    p_vision: Sequence[float],
    p_tof: Sequence[float],
    signals: Mapping[str, float],
    q_vision: float,
    q_tof: float,
    route: str,
    rule: Any,
    decision: Any,
    agent_calls: Sequence[Any],
    class_order: Sequence[str],
    arms: Mapping[str, Sequence[float]],
    rgb_preview: Mapping[str, Any] | None,
    tof_shape: Sequence[int] | None,
    dry_run: bool,
    cache: Mapping[str, Any] | None = None,
) -> CaseTrace:
    """把一個 case 的決策過程組成 CaseTrace。**純資料轉換，不做決策。**"""
    labels = list(class_order)
    final = [float(x) for x in decision.final]
    predicted_index = int(np.argmax(final))
    truth = str(row.get("class_label", ""))

    explanation = route_explanation(
        route, q_vision, q_tof, float(signals.get(DISAGREEMENT_KEY, float("nan"))),
        q_threshold=0.5, delta=float(getattr(rule, "disagreement_threshold", 0.0)),
    )

    escalated = route == "escalated"
    if not escalated:
        agent_execution: dict[str, Any] = {
            "invoked": False,
            "reason": "not_escalated",
            "artifacts": [],
        }
    elif dry_run:
        # 明確說明「該進仲裁但本次沒跑」，而不是留 null 讓畫面看起來像漏資料。
        agent_execution = {
            "invoked": False,
            "reason": "dry_run",
            "note": (
                "This case routed to escalation, but the run was a zero-cost "
                "rehearsal, so it stopped at the escalation boundary."
            ),
            "artifacts": [],
        }
    elif cache is not None and cache.get("hit"):
        # 命中時四次角色投影根本沒有發生，call_log 因此是空的。照原樣渲染
        # 會顯示成「escalated 但沒有呼叫過任何角色」—— 那看起來像 bug。
        # cache 依 §48 只存六份 artifact，不存 role-projected payload，
        # 所以這裡誠實地說 payload 在原本那次 run（NOTE-070）。
        agent_execution = {
            "invoked": True,
            "reason": "cache_hit",
            "cache_key": cache.get("cache_key"),
            "note": (
                "Served from the content-addressed agent cache: the same "
                "evidence, model, prompts, schema and runtime were already "
                "answered, so no provider call was made. The per-role payloads "
                "were not re-projected and therefore are not recorded here; "
                "they belong to the run that first produced this cache key."
            ),
            "artifacts": [call.to_json() for call in agent_calls],
            "n_roles": len(agent_calls),
        }
    else:
        agent_execution = {
            "invoked": True,
            "reason": "escalated",
            "artifacts": [call.to_json() for call in agent_calls],
            "n_roles": len(agent_calls),
        }
        if cache is not None:
            # 未命中也要記 key：下一次 resume 靠它認出這一筆已經問過。
            agent_execution["cache_key"] = cache.get("cache_key")
            agent_execution["cache_hit"] = False

    return CaseTrace(
        case_id=case_id,
        row_index=row_index,
        condition=str(row.get("condition", "")),
        class_label=truth,
        physical_scene_family=str(row.get("physical_scene_family", "")),
        inputs={
            "rgb": rgb_preview or {},
            "tof": {"shape": list(tof_shape or []), "channels": list(TOF_CHANNELS)},
            "stress_id": str(row.get("stress_id", "")),
            "base_scenario_id": str(row.get("base_scenario_id", "")),
        },
        perception={
            "class_order": labels,
            "p_vision": [float(x) for x in p_vision],
            "p_tof": [float(x) for x in p_tof],
            "vision_argmax": labels[int(np.argmax(p_vision))],
            "tof_argmax": labels[int(np.argmax(p_tof))],
            "temperatures": {
                "vision": float(getattr(rule, "temperature_vision", float("nan"))),
                "tof": float(getattr(rule, "temperature_tof", float("nan"))),
            },
        },
        quality={
            "Q_vision": float(signals.get("Q_vision", float("nan"))),
            "Q_tof": float(signals.get("Q_tof", float("nan"))),
            "U_vision": float(signals.get("U_vision", float("nan"))),
            "U_tof": float(signals.get("U_tof", float("nan"))),
            "disagreement": float(signals.get(DISAGREEMENT_KEY, float("nan"))),
            "_meaning": (
                "Q is raw sensing quality computed from the input itself; U is "
                "predictive entropy. Neither is modality reliability -- that is q."
            ),
        },
        reliability={
            "q_vision": float(q_vision),
            "q_tof": float(q_tof),
            "threshold": 0.5,
            "_meaning": (
                "q is sensor reliability derived from sensing quality, degradation "
                "margin and cross-modal support. Predictive confidence is NOT "
                "sensor reliability."
            ),
        },
        routing={
            "route": route,
            "escalated": escalated,
            **explanation,
        },
        arms={
            name: [float(x) for x in values] for name, values in arms.items()
        },
        agent_execution=agent_execution,
        final={
            "distribution": final,
            "prediction_index": predicted_index,
            "prediction_label": labels[predicted_index],
            "truth_label": truth,
            "correct": labels[predicted_index] == truth,
            "s_a": (
                None if decision.s_a is None
                else [float(x) for x in decision.s_a]
            ),
            "llm_called": bool(decision.llm_called),
        },
    )



class TraceWriter:
    """把 CaseTrace 落盤，並在最後原子性寫出 index。

    使用方式固定為：`write_case()` 逐筆寫，run 結束再 `finalise()`。
    中途失敗時呼叫 `abort()`，index 會標成 aborted —— 這樣目錄裡不會留下
    一份看起來像完整正式結果的東西。
    """

    def __init__(
        self,
        out_dir: str | Path,
        *,
        run_id: str,
        code_revision: str = "",
        runtime_identity: Mapping[str, Any] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.root = Path(out_dir) / "trace"
        self.cases_dir = self.root / "cases"
        self.previews_dir = self.root / "previews"
        self.cases_dir.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        self.header = {
            "trace_schema_version": TRACE_SCHEMA_VERSION,
            "run_id": run_id,
            "code_revision": code_revision,
            "runtime_identity": dict(runtime_identity or {}),
            "dry_run": bool(dry_run),
        }
        self.index_rows: list[dict[str, Any]] = []
        self._finalised = False

    # -- preview ---------------------------------------------------------

    def write_preview(self, case_id: str, rgb: np.ndarray) -> tuple[str, list[int]]:
        """把該次 run **記憶體裡**的 RGB 存成 PNG preview。

        刻意由 run 當下的陣列產生，而不是之後讓 Web 回頭重讀 dataset：
        後者會多開一條對正式資料集的存取路徑，而 UI 只需要看這次跑的是什麼。

        回傳 (相對路徑, 原始 shape)。
        """
        from pcmef.agents.pcmef_agents import rgb_to_png_bytes

        path = self.previews_dir / f"{case_id}.png"
        path.write_bytes(rgb_to_png_bytes(rgb))
        return path.relative_to(self.root).as_posix(), list(np.asarray(rgb).shape)

    # -- case ------------------------------------------------------------

    def write_case(self, trace: CaseTrace) -> str:
        path = self.cases_dir / f"{trace.case_id}.json"
        path.write_text(
            json.dumps(trace.to_json(self.header), ensure_ascii=False, indent=2,
                       sort_keys=True, default=str),
            encoding="utf-8",
        )
        # index 只放列表頁需要的輕量欄位；完整內容留在 case 檔裡。
        self.index_rows.append(
            {
                "case_id": trace.case_id,
                "row_index": trace.row_index,
                "condition": trace.condition,
                "class_label": trace.class_label,
                "route": trace.routing.get("route"),
                "escalated": bool(trace.routing.get("escalated")),
                "prediction": trace.final.get("prediction_label"),
                "correct": trace.final.get("correct"),
                "trace_path": path.relative_to(self.root).as_posix(),
                "preview_path": trace.inputs.get("rgb", {}).get("preview_path"),
            }
        )
        return path.as_posix()

    # -- index -----------------------------------------------------------

    def _write_index(self, run_status: str, note: str = "") -> Path:
        """原子性寫出 index：先寫暫存檔再 replace。

        半寫入的 index 比沒有 index 更糟：它看起來是完整的。
        """
        document = {
            **self.header,
            "run_status": run_status,
            "note": note,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "n_cases": len(self.index_rows),
            "cases": sorted(self.index_rows, key=lambda row: row["row_index"]),
        }
        target = self.root / "trace_index.json"
        staging = target.with_suffix(".json.partial")
        staging.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True,
                       default=str),
            encoding="utf-8",
        )
        os.replace(staging, target)
        return target

    def finalise(self) -> Path:
        self._finalised = True
        return self._write_index(RUN_STATUS_COMPLETE)

    def abort(self, reason: str) -> Path:
        """run 中途失敗。留下已寫的 case，但 index 明說它不完整。"""
        self._finalised = True
        return self._write_index(RUN_STATUS_ABORTED, reason[:400])
