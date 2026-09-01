# PC-MEF Research System source maintenance contract
# 上下游: 讀 outputs/corrective/executor_validation.json 的 measured_usage
#         與 outputs/perception/gate/stress 的實測 escalation rate；
#         寫出 outputs/corrective/final_e2_cost_estimate.json。
#         不呼叫 provider、不觸碰 families 36-43。
# 檔案路徑: pcmef/experiments/e2_cost.py
# 產生時間: 2026-09-01 20:10 +08:00
# 版本: v0.1.0
# 功能說明: 以**實測的** per-role token 用量外推 Final E2 的 provider 成本，
#           並把不確定性寫成具名的區間而不是一個假裝精確的數字。
# 模組定位: 預算估算工具。它「不是」研究結果 —— 產出的每一個數字都是
#           營運成本，與 accuracy 無關，也不得影響任何研究參數。
# 主要責任:
#   1. PRICING 記錄計價來源與有效期限
#   2. estimate() 由實測用量與 escalation rate 外推
#   3. 以 thinking token 的觀測值分開報 output 成本
#   4. 回報樣本數，讓「量了幾個 case」與「推了幾個 case」分得出來
# 維護提醒:
#   - 不得用猜的 token 數取代實測值。整個模組存在的理由就是不要用猜的；
#     沒有 measured_usage 就明說沒有，不要填一個「典型值」。
#   - 不得把本檔的輸出寫進任何 lock 或 formal artifact。成本不是研究結論。
#   - 計價會過期。PRICING 帶 effective_until，過期就必須重新查證再用。
#   - v0.1.0 新增：首版成本估算。
# 驗證方式:
#   - py -3.10 -m pcmef.cli corrective estimate-cost
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["estimate", "PRICING"]

#: Gemini 3.6 Flash 的促銷計價（USD / 1M tokens）。
#: **output 含 thinking tokens** —— Google 明訂 thinking 併入 output 計費。
PRICING: dict[str, Any] = {
    "model": "gemini-3.6-flash",
    "input_usd_per_1m": 0.75,
    "output_usd_per_1m": 3.75,
    "output_includes_thinking": True,
    "effective_until": "2026-12-31",
    "source": "Google AI for Developers pricing (promotional), supplied by the advisor",
    "note": (
        "promotional pricing with an expiry. Re-verify before quoting it after "
        "the effective_until date."
    ),
}

USD_TO_TWD = 31.67

VALIDATION = Path("outputs/corrective/executor_validation.json")
STRESS_MANIFEST = Path("outputs/perception/gate/stress/stress_manifest.json")


def estimate(
    validation_path: str | Path = VALIDATION,
    total_rows: int = 384,
    escalation_rate: float | None = None,
    out_dir: str | Path = "outputs/corrective",
    usd_to_twd: float = USD_TO_TWD,
) -> dict[str, Any]:
    """由實測用量外推 Final E2 的 provider 成本。"""
    document = json.loads(Path(validation_path).read_text(encoding="utf-8"))
    usage = document.get("measured_usage") or {}
    totals = usage.get("totals") or {}
    if not totals.get("calls"):
        raise RuntimeError(
            f"{validation_path} carries no measured token usage. Run "
            "`corrective validate-executor` with a provider that reports "
            "usageMetadata first; this module must not substitute a guess."
        )

    if escalation_rate is None:
        rows = json.loads(STRESS_MANIFEST.read_text(encoding="utf-8"))["rows"]
        escalation_rate = document["n_escalated_rows"] / len(rows)

    measured_cases = usage.get("escalated_cases_measured") or 1
    per_case = {
        "calls": totals["calls"] / measured_cases,
        "prompt_tokens": totals["prompt_tokens"] / measured_cases,
        "completion_tokens": totals["completion_tokens"] / measured_cases,
        "thoughts_tokens": totals["thoughts_tokens"] / measured_cases,
    }
    per_case["billable_output_tokens"] = (
        per_case["completion_tokens"] + per_case["thoughts_tokens"]
    )

    projected_cases = escalation_rate * total_rows
    projected = {
        key: value * projected_cases for key, value in per_case.items()
    }
    input_usd = projected["prompt_tokens"] / 1e6 * PRICING["input_usd_per_1m"]
    output_usd = (
        projected["billable_output_tokens"] / 1e6 * PRICING["output_usd_per_1m"]
    )
    total_usd = input_usd + output_usd

    # 不確定性用具名倍率表達，不假裝點估計是精確的。
    # 樣本只有幾個 case，thinking token 的變異是主要來源。
    bands = {
        f"x{factor}": {
            "usd": round(total_usd * factor, 2),
            "twd": round(total_usd * factor * usd_to_twd),
        }
        for factor in (1.0, 1.5, 2.0, 3.0)
    }

    result = {
        "report_id": "final_e2_cost_estimate",
        "scientific_result": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pricing": PRICING,
        "usd_to_twd": usd_to_twd,
        "measured": {
            "source": Path(validation_path).as_posix(),
            "escalated_cases_measured": measured_cases,
            "calls_measured": totals["calls"],
            "totals": totals,
            "by_role": usage.get("by_role", {}),
        },
        "per_escalated_case": {k: round(v, 1) for k, v in per_case.items()},
        "projection": {
            "total_condition_rows": total_rows,
            "escalation_rate": round(escalation_rate, 4),
            "escalation_rate_source": (
                "measured on the effective-93 stress set (372 rows), not assumed"
            ),
            "projected_escalated_cases": round(projected_cases),
            "projected_provider_calls": round(projected["calls"]),
            "projected_prompt_tokens": round(projected["prompt_tokens"]),
            "projected_billable_output_tokens": round(
                projected["billable_output_tokens"]
            ),
            "projected_thinking_tokens": round(projected["thoughts_tokens"]),
        },
        "cost": {
            "input_usd": round(input_usd, 2),
            "output_usd": round(output_usd, 2),
            "total_usd": round(total_usd, 2),
            "total_twd": round(total_usd * usd_to_twd),
        },
        "uncertainty_bands": bands,
        "caveats": [
            f"measured on {measured_cases} escalated case(s) only; the sample is "
            "small and thinking-token variance is the dominant unknown",
            "retries are not included: every retry is a full extra call",
            "the escalation rate on families 36-43 may differ from the "
            "effective-93 stress set",
            "promotional pricing expires " + PRICING["effective_until"],
        ],
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "final_e2_cost_estimate.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result
