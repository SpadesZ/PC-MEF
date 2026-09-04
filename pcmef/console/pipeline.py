# PC-MEF Research System source maintenance contract
# 上下游: 由 console.workspace_routes 的 GET /pipeline 呼叫；
#         讀 freeze/ 底下由 ACTIVE_LINEAGE 解析出的 lock。**唯讀，不寫入。**
# 檔案路徑: pcmef/console/pipeline.py
# 產生時間: 2026-09-03 09:55 +08:00
# 版本: v0.2.0
# 功能說明: PC-MEF 研究流程的七個節點，各自的 Input / Process / Output
#           都由 frozen lock 的實際值填出，並標明出處。
# 模組定位: SAI v0.6.0 §20 的 Pipeline 入口。它回答「系統怎麼跑」，
#           不回答「這次跑了什麼」—— 後者屬 Results 與 case trace。
#           **唯讀，且不得成為第二條修改研究參數的通道。**
# 主要責任:
#   1. PIPELINE_NODES 定義七個節點、各自的職責與產出
#   2. build_pipeline() 讀 lock 填入實際值，每個值都帶來源
#   3. 讀不到 lock 時說明原因，不得讓整頁 500
# 維護提醒:
#   - 不得在本檔提供任何可寫入的設定。Pipeline 是說明流程的地方；
#     一個可以在這裡改的參數等於繞過 frozen configuration。
#   - 節點順序即資料流順序，不得為了畫面好看而重排。
#   - 不得把 gate.lock 的 `q_vision_threshold` / `q_tof_threshold` 呈現成
#     路由門檻。那兩個值在 Q 尺度上（1.32 / 17.0），而正式路徑走
#     `reliability_route`，比較的是 q ≥ 0.5 與 D ≤ δ。欄位名稱的 `q_`
#     是歷史遺留，照字面顯示會讓讀者拿錯數字（NOTE-068）。
#   - 不得讓本模組 import 任何會執行決策的東西。它只讀 lock 的字面值。
#   - v0.2.0 新增：各節點的實際值，對應 P2-3。
#   - v0.1.0 新增：靜態結構，對應 P2-2。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_pipeline.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["PipelineNode", "PIPELINE_NODES", "build_pipeline"]


@dataclass(frozen=True)
class PipelineNode:
    """一個流程節點。`carries` 是它**交給下一個節點**的東西。"""

    key: str
    label: str
    english: str
    summary: str
    carries: str


#: 七個節點，順序即資料流順序。
#:
#: `carries` 刻意寫「交給下一個節點什麼」而不是「這一步做了什麼」：
#: 黑箱感來自看不見中間傳遞的東西，而不是看不見每一步的名稱。
PIPELINE_NODES: tuple[PipelineNode, ...] = (
    PipelineNode(
        "simulation", "物理模擬", "Simulation",
        "以 Mitsuba 3 + mitransient 做時間解析光傳輸，把場景幾何、介質與"
        "光源條件算成 transient response。",
        "transient tensor、peak time、能量",
    ),
    PipelineNode(
        "paired", "成對 RGB-ToF", "Paired RGB-ToF",
        "同一個物理場景同步產生 RGB 影像與 500×4 的 VL53L0X-like ToF "
        "recording，四種 benchmark condition 各生成一筆。",
        "paired case：RGB 陣列 + (500, 4) ToF",
    ),
    PipelineNode(
        "perception", "感知模型", "Perception",
        "Vision small CNN 與 ToF 1D CNN 各自輸出四類完整分布，"
        "再以路由驗證集求得的溫度做 temperature scaling。",
        "p_V(y|x)、p_T(y|x)",
    ),
    PipelineNode(
        "reliability", "可靠度評估", "Reliability Assessment",
        "由影像高頻能量得 Q_V、由 signal/ambient 比得 Q_T，"
        "由兩個分布的 total variation 得 D，再轉成 0–1 的感測可靠度 q。",
        "Q_V、Q_T、D、U、q_V、q_T",
    ),
    PipelineNode(
        "routing", "選擇性路由", "Routing",
        "依 q_V、q_T 與 D 決定走 trust_vision、trust_tof、fixed fusion，"
        "或送進證據仲裁。門檻由 lock 還原，不在此重新搜尋。",
        "route，以及該 route 的成立條件",
    ),
    PipelineNode(
        "arbitration", "多代理仲裁", "Multi-Agent Arbitration",
        "只有 escalated 的案例會走這一步：Observation → Physics 與 "
        "Visual-Semantic → Arbitration，四個角色依既定契約各收到不同證據。",
        "四份 role artifact，以及正規化後的 s_A",
    ),
    PipelineNode(
        "decision", "最終決策", "Final Decision",
        "非 escalated 取傳統路徑的 p_trad，escalated 取 s_A，"
        "組成 F(x) 並與四個 baseline 一同進入統計。",
        "F(x)、預測類別、per-condition 與 worst-condition 指標",
    ),
)


def _fact(label: str, value: Any, source: str, note: str = "") -> dict[str, Any]:
    """一條事實。`source` 是它出自哪個 lock —— 沒有出處的數字沒有意義。"""
    if isinstance(value, float):
        shown = f"{value:.6g}"
    elif isinstance(value, bool):
        shown = "是" if value else "否"
    elif isinstance(value, (list, tuple)):
        shown = "、".join(str(v) for v in value)
    elif isinstance(value, dict):
        shown = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        shown = str(value)
    return {"label": label, "value": shown, "source": source, "note": note}


class _Locks:
    """把 lock 讀成 payload。缺一個不讓整頁掛掉，回空 dict。"""

    def __init__(self, freeze_dir: Path) -> None:
        self._dir = freeze_dir
        self._cache: dict[str, dict[str, Any]] = {}
        self.missing: list[str] = []

    def __call__(self, name: str) -> dict[str, Any]:
        if name not in self._cache:
            path = self._dir / f"{name}.lock.json"
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self._cache[name] = loaded.get("payload") or {}
            except (OSError, json.JSONDecodeError, AttributeError):
                self.missing.append(name)
                self._cache[name] = {}
        return self._cache[name]


def _detail(lock: _Locks) -> dict[str, dict[str, list]]:
    """七個節點各自的 Input / Process / Output。

    Process 一律列 frozen 參數的**實際值**，而不是描述它是什麼。
    「門檻由 gate.lock 還原」是說明，「D ≤ 0.489660」才是可被檢查的事實。
    """
    calib = lock("calibrated_simulation")
    sizing = lock("e2_sample_size")
    gate = lock("gate")
    rel = lock("reliability_final")
    agent = lock("agent_schema")
    runtime = lock("llm_runtime")
    stats = lock("statistics_config")
    metric = lock("metric_config")
    formal = lock("formal_config")

    values = calib.get("calibrated_parameter_values") or {}
    outcomes = calib.get("stage_outcomes") or {}
    converged = [k for k, v in outcomes.items() if v == "CONVERGED"]
    inhibited = calib.get("inhibited_stages") or []

    return {
        "simulation": {
            "inputs": [
                _fact("場景幾何與介質", "bottle / foil / participating media 預設",
                      "calibrated_simulation.lock"),
                _fact("ToF 取樣點數", values.get("TOF_RECORDING_POINTS", "—"),
                      "calibrated_simulation.lock"),
            ],
            "process": [
                _fact("已收斂的校準階段", converged or "（無）",
                      "calibrated_simulation.lock",
                      "只有 Ambient 這一段真的收斂"),
                _fact("被抑制的階段", inhibited or "（無）",
                      "calibrated_simulation.lock",
                      "更新被抑制代表該段仍使用未校準的先驗值"),
                _fact("擬合參數", list((calib.get("fitted_parameters") or {}).keys()),
                      "calibrated_simulation.lock"),
            ],
            "outputs": [
                _fact("scene hash", (calib.get("calibrated_scene_hash") or "—")[:16],
                      "calibrated_simulation.lock"),
                _fact("surrogate hash",
                      (calib.get("calibrated_surrogate_hash") or "—")[:16],
                      "calibrated_simulation.lock"),
            ],
        },
        "paired": {
            "inputs": [
                _fact("每類 base scenario", sizing.get("base_scenarios_per_class", "—"),
                      "e2_sample_size.lock"),
                _fact("family 總數", sizing.get("family_domain", "—"),
                      "e2_sample_size.lock"),
            ],
            "process": [
                _fact("四種 condition", sizing.get("conditions") or "—",
                      "e2_sample_size.lock"),
                _fact("每個 scenario 的 condition 數",
                      sizing.get("conditions_per_scenario", "—"),
                      "e2_sample_size.lock"),
                _fact("類別配置", sizing.get("class_balance", "—"),
                      "e2_sample_size.lock"),
            ],
            "outputs": [
                _fact("總列數", sizing.get("total_condition_rows", "—"),
                      "e2_sample_size.lock",
                      "96 base × 4 condition"),
                _fact("每類每 condition 列數",
                      sizing.get("rows_per_class_per_condition", "—"),
                      "e2_sample_size.lock"),
            ],
        },
        "perception": {
            "inputs": [
                _fact("RGB 影像 + (500, 4) ToF", "由上一節點成對產生", "—"),
            ],
            "process": [
                _fact("temperature (vision)", gate.get("temperature_vision", "—"),
                      "gate.lock", "在路由驗證集上求得後凍結"),
                _fact("temperature (tof)", gate.get("temperature_tof", "—"),
                      "gate.lock"),
                _fact("class_order", agent.get("class_order") or "—",
                      "agent_schema.lock",
                      "所有四類向量共用這個順序"),
                _fact("checkpoint 數",
                      len(formal.get("model_checkpoint_hashes") or {}) or "—",
                      "formal_config.lock"),
            ],
            "outputs": [
                _fact("p_V(y|x)、p_T(y|x)", "各一個長度 4 的校準後分布", "—"),
            ],
        },
        "reliability": {
            "inputs": [
                _fact("證據來源", rel.get("evidence_sources") or "—",
                      "reliability_final.lock"),
            ],
            "process": [
                _fact("公式", rel.get("formula", "—"), "reliability_final.lock"),
                _fact("演算法", rel.get("algorithm", "—"), "reliability_final.lock"),
                _fact("禁止作為特徵",
                      rel.get("forbidden_predictive_features") or "—",
                      "reliability_final.lock",
                      "q 不得由 p(y|x) 推得，否則它會變成第二個信心分數"),
            ],
            "outputs": [
                _fact("Q_V、Q_T、D、U_V、U_T、q_V、q_T",
                      "q 已 clip 到 [0, 1]", "—"),
            ],
        },
        "routing": {
            "inputs": [
                _fact("q_V、q_T、D", "由上一節點得出", "—"),
            ],
            "process": [
                # 這三條是正式路徑真正比較的東西。
                _fact("可靠門檻 q_m ≥",
                      (rel.get("parameters") or {}).get("reliable_margin", "—"),
                      "reliability_final.lock",
                      "reliability_route 比較的是 q，不是 Q"),
                _fact("分歧門檻 D ≤", gate.get("disagreement_threshold", "—"),
                      "gate.lock"),
                _fact("fusion 權重", gate.get("fusion_weight", "—"), "gate.lock"),
                _fact("路由規則", gate.get("routing_policy", "—"), "gate.lock"),
                _fact("禁止重新搜尋門檻", gate.get("re_search_forbidden", "—"),
                      "gate.lock",
                      "escalation rate 本身是研究結果，不是可調的旋鈕"),
            ],
            "outputs": [
                _fact("route", "trust_vision / trust_tof / fusion / escalated", "—"),
            ],
        },
        "arbitration": {
            "inputs": [
                _fact("證據表示法", runtime.get("representation_mode", "—"),
                      "llm_runtime.lock"),
                _fact("四個角色", "observation / physics / visual_semantic / arbitration",
                      "agent_schema.lock",
                      "各收到不同欄位，實際內容見 case trace"),
            ],
            "process": [
                _fact("support bridge",
                      agent.get("support_bridge_formula", "—"), "agent_schema.lock"),
                _fact("bridge 適用範圍", agent.get("support_bridge_scope", "—"),
                      "agent_schema.lock"),
                _fact("arbitration schema",
                      (agent.get("arbitration_schema_sha256") or "—")[:16],
                      "agent_schema.lock"),
                _fact("prompt 數", len(runtime.get("prompt_hashes") or {}) or "—",
                      "llm_runtime.lock"),
            ],
            "outputs": [
                _fact("s_A", "四類 support 正規化後的機率向量", "—"),
            ],
        },
        "decision": {
            "inputs": [
                _fact("p_trad 或 s_A", "依 route 二選一", "—"),
            ],
            "process": [
                _fact("決策橋接", gate.get("decision_bridge_formula", "—"),
                      "gate.lock"),
                _fact("主要指標", stats.get("primary_reporting", "—"),
                      "statistics_config.lock"),
                _fact("重抽單位", stats.get("resample_unit", "—"),
                      "statistics_config.lock"),
                _fact("分層", stats.get("stratification", "—"),
                      "statistics_config.lock"),
                _fact("replicates / seed",
                      f"{stats.get('bootstrap_replicates', '—')} / "
                      f"{stats.get('bootstrap_seed', '—')}",
                      "statistics_config.lock"),
                _fact("信賴水準", stats.get("confidence_level", "—"),
                      "statistics_config.lock"),
            ],
            "outputs": [
                _fact("F(x) 與預測類別", "四類機率與 argmax", "—"),
                _fact("指標定義數",
                      len(metric.get("metric_definitions") or {}) or "—",
                      "metric_config.lock"),
            ],
        },
    }


def build_pipeline(freeze_dir: str | Path | None = None) -> dict[str, Any]:
    """畫面需要的流程結構，含各節點由 lock 填出的實際值。

    lineage 解析失敗或 lock 讀不到時回傳靜態結構加上原因。Pipeline 是
    觀察頁，不是決策路徑 —— 這裡 fail-closed 只會換來一頁 500，而使用者
    連「為什麼看不到」都不知道。

    `freeze_dir` 只決定去哪裡找 `ACTIVE_LINEAGE.json`。pointer 裡的
    `active_freeze_dir` 是 repo 相對路徑，因此實際讀 lock 的位置相對於
    cwd 而不是相對於這個參數 —— 要完全隔離必須連 cwd 一起換。
    """
    detail: dict[str, dict[str, list]] = {}
    resolved: dict[str, Any] = {}
    note = ""

    try:
        from pcmef.core.active_lineage import resolve_active_lineage

        lineage = resolve_active_lineage(freeze_dir or "freeze")
        directory = Path(lineage.freeze_dir)
        resolved = {
            # 記解析出來的目錄，不是 pointer —— pointer 之後會改指別處，
            # 而這一頁描述的是現在實際生效的那一組 lock（NOTE-054）。
            "dir": str(directory),
            "pointer": str(lineage.pointer_path),
            "supersedes": list(lineage.supersedes),
            "status": lineage.status,
        }
        locks = _Locks(directory)
        detail = _detail(locks)
        if locks.missing:
            note = "讀不到的 lock：" + "、".join(sorted(set(locks.missing)))
    except Exception as error:  # noqa: BLE001 - 觀察頁不得因此 500
        note = f"無法解析 frozen lineage：{error}"

    return {
        "nodes": [
            {
                "key": node.key,
                "label": node.label,
                "english": node.english,
                "summary": node.summary,
                "carries": node.carries,
                "index": index + 1,
                "detail": detail.get(node.key),
            }
            for index, node in enumerate(PIPELINE_NODES)
        ],
        "detail_available": bool(detail),
        "detail_note": note,
        "lineage": resolved,
        # gate.lock 的 `q_vision_threshold` / `q_tof_threshold` 在 Q 尺度上，
        # 且 `gate_route()` 全庫沒有呼叫者；正式路徑走 reliability_route。
        # 不講清楚的話，讀者會拿 1.32 當成 vision 的路由門檻。
        "threshold_caveat": (
            "gate.lock 另有 q_vision_threshold 與 q_tof_threshold，"
            "但它們是 Q（原始感測品質）尺度的值，屬於 gate_route() —— "
            "該函式在正式路徑沒有呼叫者。實際路由比較的是上表的 "
            "q_m ≥ 0.5 與 D ≤ δ。"
        ),
    }
