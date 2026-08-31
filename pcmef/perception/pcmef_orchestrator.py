# PC-MEF Research System source maintenance contract
# 上下游: 由 perception.gate 的 escalated 分支與未來的 experiments.e2_formal 呼叫；
#         匯入 agents.pcmef_agents（四角色）與 core.numeric 的兩個 bridge；
#         不做任何 I/O，不讀 dataset。
# 檔案路徑: pcmef/perception/pcmef_orchestrator.py
# 產生時間: 2026-09-01 03:10 +08:00
# 版本: v0.1.0
# 功能說明: Full PC-MEF 的最終決策路徑。把「要不要問 LLM」與「問完之後怎麼算」
#           收斂成一個具名函式，公式不散落在 runner 裡。
# 模組定位: selective_escalation_bridge_v1 的唯一執行入口。
#           它「不是」路由器（那在 gate.reliability_route），也「不是」agent
#           實作（那在 agents.pcmef_agents）。
# 主要責任:
#   1. decide_case() 依 route 決定要不要呼叫 LLM，並回傳 F(x)
#   2. formal 模式拒絕 deterministic substitute，非 formal 才允許
#   3. non-escalated 保證 **零次** provider 呼叫
#   4. 回傳的 trace 記下 route、e(x)、是否呼叫 LLM、s_A 與 F
# 維護提醒:
#   - 不得在 formal 模式讓 deterministic arbiter 頂替 LLM。那會讓
#     「PC-MEF 的 LLM 臂」在報告上成立而實際從未執行（gate_rule.json 的
#     arbiter 欄位記載過這個情況，不可重演）。
#   - 不得在 non-escalated 分支呼叫 provider。selective escalation 的整個
#     論點就是「傳統證據足夠就不叫 LLM」，多叫一次就推翻了那個論點。
#   - 不得把 fusion_weight 當成 agent weight；它是 Vision<->ToF 的權重。
#   - 不得把 s_A 當 calibrated posterior 報 NLL/ECE（NOTE-049）。
#   - v0.1.0 新增：首版 orchestrator，決策見 NOTE-049。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_pcmef_orchestrator.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from pcmef.core.numeric import (
    ROUTE_ESCALATED,
    SELECTIVE_ESCALATION_BRIDGE_VERSION,
    arbitration_support_bridge,
    escalation_indicator,
    selective_escalation_bridge,
)

__all__ = [
    "FormalArbiterRequired",
    "CaseDecision",
    "decide_case",
    "SELECTIVE_ESCALATION_BRIDGE_VERSION",
]


class FormalArbiterRequired(RuntimeError):
    """formal 模式下缺少真正的 agent 仲裁器。

    這是 fail-closed：寧可中止，也不要讓 deterministic substitute 悄悄頂替
    LLM 而報告上仍寫著 Full PC-MEF。
    """


@dataclass
class CaseDecision:
    """一個 case 的最終決策與其來源。"""

    route: str
    escalated: int
    final: np.ndarray
    llm_called: bool
    s_a: np.ndarray | None = None
    p_trad: np.ndarray | None = None
    agent_bundle: Any = None
    attempts: list[bool] = field(default_factory=list)

    def to_trace(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "e": self.escalated,
            "llm_called": self.llm_called,
            "bridge_version": SELECTIVE_ESCALATION_BRIDGE_VERSION,
            "final": [float(x) for x in self.final],
            "s_a": None if self.s_a is None else [float(x) for x in self.s_a],
            "attempts": list(self.attempts),
        }


def decide_case(
    route: str,
    p_vision: np.ndarray,
    p_tof: np.ndarray,
    fusion_weight: float,
    *,
    evidence: Mapping[str, Any] | None = None,
    agent_runner: Any = None,
    formal: bool = True,
    fallback_arbiter: Callable[..., np.ndarray] | None = None,
) -> CaseDecision:
    """回傳 F(x)，並保證「非 escalated 不呼叫 LLM」。

    formal=True 時 escalated case **必須**有真正的 agent runner；沒有就中止。
    fallback_arbiter 只在 formal=False 時可用，供 pilot/診斷重現舊行為。
    """
    e = escalation_indicator(route)

    if not e:
        # 這條路徑刻意連 agent_runner 都不碰：selective escalation 的論點是
        # 「傳統證據足夠就不叫 LLM」，多叫一次就推翻了它。
        final = selective_escalation_bridge(
            route, p_vision, p_tof, fusion_weight=fusion_weight
        )
        return CaseDecision(
            route=route, escalated=0, final=final, llm_called=False, p_trad=final
        )

    if agent_runner is None:
        if formal:
            raise FormalArbiterRequired(
                f"route={ROUTE_ESCALATED} in formal mode requires the real "
                "four-agent arbiter; a deterministic substitute would let the "
                "PC-MEF LLM arm appear in the report without ever having run"
            )
        if fallback_arbiter is None:
            raise FormalArbiterRequired(
                "non-formal mode still needs either an agent runner or an "
                "explicit fallback_arbiter"
            )
        s_a = np.asarray(fallback_arbiter(), dtype=np.float64)
        bundle, attempts = None, []
    else:
        from pcmef.agents.pcmef_agents import run_pcmef_case

        if evidence is None:
            raise FormalArbiterRequired(
                "an escalated case needs its evidence payload to run the agents"
            )
        bundle = run_pcmef_case(agent_runner, evidence)
        s_a = arbitration_support_bridge(bundle.artifacts["arbitration_validated"])
        attempts = [a.ok for a in getattr(agent_runner, "attempts", [])]

    final = selective_escalation_bridge(
        route, p_vision, p_tof, s_a=s_a, fusion_weight=fusion_weight
    )
    return CaseDecision(
        route=route, escalated=1, final=final, llm_called=agent_runner is not None,
        s_a=s_a, agent_bundle=bundle, attempts=attempts,
    )
