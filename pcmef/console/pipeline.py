# PC-MEF Research System source maintenance contract
# 上下游: 由 console.workspace_routes 的 GET /pipeline 呼叫；
#         本版只回傳流程的**靜態結構**，P2-3 會補上各節點的實際
#         Input / Process / Output（來自 frozen lock 與 run manifest）。
# 檔案路徑: pcmef/console/pipeline.py
# 產生時間: 2026-09-03 09:55 +08:00
# 版本: v0.1.0
# 功能說明: PC-MEF 研究流程的七個節點與它們之間傳遞什麼。
# 模組定位: SAI v0.6.0 §20 的 Pipeline 入口。它回答「系統怎麼跑」，
#           不回答「這次跑了什麼」—— 後者屬 Results 與 case trace。
#           **唯讀，且不得成為第二條修改研究參數的通道。**
# 主要責任:
#   1. PIPELINE_NODES 定義七個節點、各自的職責與產出
#   2. build_pipeline() 組出畫面需要的結構
# 維護提醒:
#   - 不得在本檔提供任何可寫入的設定。Pipeline 是說明流程的地方；
#     一個可以在這裡改的參數等於繞過 frozen configuration。
#   - 節點順序即資料流順序，不得為了畫面好看而重排。
#   - v0.1.0 新增：靜態結構，對應 P2-2。P2-3 補實際值。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_navigation.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
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
        "或送進證據仲裁。門檻由 gate.lock 還原，不在此重新搜尋。",
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


def build_pipeline() -> dict[str, Any]:
    """畫面需要的流程結構。

    本版只有靜態結構。P2-3 會為每個節點補上實際的 Input / Process / Output
    —— 那些值來自 frozen lock 與 run manifest，而不是寫死在這裡。
    """
    return {
        "nodes": [
            {
                "key": node.key,
                "label": node.label,
                "english": node.english,
                "summary": node.summary,
                "carries": node.carries,
                "index": index + 1,
            }
            for index, node in enumerate(PIPELINE_NODES)
        ],
        "detail_available": False,
        "detail_note": (
            "各節點的實際 Input / Process / Output（含這次執行用的 resolution、"
            "spp、seed、幾何與光源條件）將在下一階段接上；本頁目前呈現的是"
            "流程結構與各步驟之間傳遞的內容。"
        ),
    }
