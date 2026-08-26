# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 evaluate e1-outcome 呼叫；輸入為 experiments.e1 產出的
#         E1Result 與 freeze/e1_scientific_rule.lock 的規則；
#         輸出 e1_outcome.lock（claim_mode 由此鎖定，下游 manifest 一律繼承）。
# 檔案路徑: pcmef/experiments/e1_outcome.py
# 產生時間: 2026-08-27 13:50 +08:00
# 版本: v0.1.0
# 功能說明: 依事先凍結的規則，把 E1 的數字判成通過或降級，並把這個判定鎖起來。
#           降級不代表工程失敗，但之後所有報告都必須改口徑 —— 這個改口徑是
#           由 lock 強制的，不是靠人記得。
# 模組定位: SRC-SAI §12.1 E1 Scientific Outcome Gate 的狀態機。
#           它「不是」計算層 —— 所有數字都由 experiments.e1 算好傳入，
#           本檔只做比較與凍結。
# 主要責任:
#   1. ScientificRule.from_lock() 從 e1_scientific_rule.lock 讀出三條判定條件
#   2. evaluate_outcome() 逐條檢查並產出 PASS / DEGRADED 與理由
#   3. CLAIM_MODE 依 outcome 決定下游 claim tag
#   4. freeze_outcome() 寫入 e1_outcome.lock，且不可覆寫
#   5. assert_claim_mode() 供下游檢查自己有沒有沿用正確的口徑
# 維護提醒:
#   - 不得在看到結果之後修改 e1_scientific_rule.lock 再重判。規則必須早於
#     held-out 開啟（Appendix H2），lock 的不可覆寫性就是這條的執行機制。
#   - 不得把 DEGRADED 當成「再調一次就好」。§12.1 明訂 DEGRADED 不解鎖
#     held-out，且不得在 held-out 上重新校準；正確處置是接受降級的 claim，
#     或以新的 run 從頭來過。
#   - 不得讓下游報告在 DEGRADED 時仍使用未限定範圍的 physics-calibrated 措辭。
#     claim_mode 寫進 lock 就是為了讓這件事可被機器檢查。
#   - 不得為了讓 PASS 好看而放寬 per-feature 條件。三條是 AND 關係：
#     macro CI 下界 > 0、每個特徵不退步、Distance 趨勢未降級。
#   - v0.1.0 新增：首版 outcome 狀態機，決策見 NOTE-024。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1/test_e1_outcome.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pcmef.core.locks import LockStore

__all__ = [
    "OutcomeError",
    "E1_SCIENTIFIC_PASS",
    "E1_SCIENTIFIC_DEGRADED",
    "CLAIM_MODE",
    "ScientificRule",
    "OutcomeDecision",
    "evaluate_outcome",
    "freeze_outcome",
    "assert_claim_mode",
]

E1_SCIENTIFIC_PASS = "E1_SCIENTIFIC_PASS"
E1_SCIENTIFIC_DEGRADED = "E1_SCIENTIFIC_DEGRADED"

#: §12.1：outcome 決定下游 claim_mode，報告層必須沿用。
CLAIM_MODE: dict[str, str] = {
    E1_SCIENTIFIC_PASS: "tof_physics_calibrated",
    E1_SCIENTIFIC_DEGRADED: "synthetic_testbed",
}


class OutcomeError(RuntimeError):
    """規則缺項、結果不完整，或試圖以不同內容重凍 outcome。"""


@dataclass(frozen=True)
class ScientificRule:
    """e1_scientific_rule.lock 裡的三條 PASS 條件（SRC-PLAN §3.2 預設）。"""

    macro_mean_delta_ci_lower_bound_gt: float
    per_feature_class_macro_delta_gte: float
    distance_trend_consistency: str
    bootstrap_replicates: int
    bootstrap_seed: int
    normalization_scales_hash: str = ""

    @classmethod
    def from_lock(cls, store: LockStore) -> ScientificRule:
        """從已凍結的 lock 讀規則。刻意不接受直接傳參數的建構路徑。

        規則只能來自 lock：這是「規則必須早於結果」的執行機制。
        若允許呼叫端直接給值，就等於允許看過結果之後臨時換一組門檻。
        """
        payload = store.load("e1_scientific_rule")
        aggregation = payload.get("aggregation")
        if not isinstance(aggregation, Mapping):
            raise OutcomeError(
                "e1_scientific_rule.lock has no aggregation mapping; the three PASS "
                "conditions must be frozen before held-out is opened"
            )
        required = (
            "macro_mean_delta_ci_lower_bound_gt",
            "per_feature_class_macro_delta_gte",
            "distance_trend_consistency",
        )
        missing = [key for key in required if key not in aggregation]
        if missing:
            raise OutcomeError(
                f"e1_scientific_rule.lock is missing PASS condition(s) {missing}"
            )
        return cls(
            macro_mean_delta_ci_lower_bound_gt=float(
                aggregation["macro_mean_delta_ci_lower_bound_gt"]
            ),
            per_feature_class_macro_delta_gte=float(
                aggregation["per_feature_class_macro_delta_gte"]
            ),
            distance_trend_consistency=str(aggregation["distance_trend_consistency"]),
            bootstrap_replicates=int(payload["bootstrap_replicates"]),
            bootstrap_seed=int(payload["bootstrap_seed"]),
            normalization_scales_hash=str(payload.get("normalization_scales", "")),
        )


@dataclass(frozen=True)
class OutcomeDecision:
    """判定結果與逐條理由。"""

    outcome: str
    claim_mode: str
    conditions: tuple[tuple[str, bool, str], ...]

    @property
    def passed(self) -> bool:
        return self.outcome == E1_SCIENTIFIC_PASS

    def failed_conditions(self) -> tuple[str, ...]:
        return tuple(name for name, ok, _ in self.conditions if not ok)

    def lines(self) -> list[str]:
        return [
            f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}"
            for name, ok, detail in self.conditions
        ]

    def to_artifact(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "claim_mode": self.claim_mode,
            "conditions": [
                {"name": name, "passed": ok, "detail": detail}
                for name, ok, detail in self.conditions
            ],
        }


def evaluate_outcome(result, rule: ScientificRule) -> OutcomeDecision:
    """依 frozen rule 判定 PASS/DEGRADED。三條是 AND 關係。

    result 是 experiments.e1.E1Result。刻意以鴨子型別接收而非 import，
    避免 outcome 層反過來相依於計算層。
    """
    conditions: list[tuple[str, bool, str]] = []

    # 1. macro-mean Delta 的 95% CI 下界必須大於門檻。
    #    用下界而非點估計：點估計為正只代表「看起來有改善」，
    #    下界為正才代表「改善不太可能是抽樣造成的」。
    lower = float(result.bootstrap.ci_lower)
    threshold = rule.macro_mean_delta_ci_lower_bound_gt
    ok_ci = lower > threshold
    conditions.append((
        "macro_mean_delta_ci_lower_bound_gt",
        ok_ci,
        f"CI lower bound {lower:.6f} vs threshold {threshold} "
        f"(point estimate {result.bootstrap.point_estimate:.6f}, "
        f"B={result.bootstrap.replicates}, seed={result.bootstrap.seed})",
    ))

    # 2. 每個特徵的 class-macro Delta 都不得低於門檻（不得退步）。
    #    整體平均變好但某個特徵變差，代表校準是在犧牲一個特徵換另一個 ——
    #    那不是 fidelity 提升。
    per_feature = result.per_feature_class_macro_delta()
    below = {
        feature: value
        for feature, value in per_feature.items()
        if value < rule.per_feature_class_macro_delta_gte
    }
    conditions.append((
        "per_feature_class_macro_delta_gte",
        not below,
        f"all features >= {rule.per_feature_class_macro_delta_gte}"
        if not below
        else f"these features regressed: "
             f"{ {k: round(v, 6) for k, v in below.items()} }",
    ))

    # 3. Distance 的 ±offset 趨勢不得降級。
    #    趨勢不適用時不算降級 —— 那代表沒有 offset 資料可比，
    #    而「沒得比」與「比了但不一致」是兩回事。
    expected = rule.distance_trend_consistency
    trend = result.trend
    ok_trend = (not trend.degraded) if expected == "non_degraded" else trend.consistent
    conditions.append((
        "distance_trend_consistency",
        ok_trend,
        f"expected {expected}; applicable={trend.applicable} "
        f"consistent={trend.consistent} degraded={trend.degraded} — {trend.detail}",
    ))

    passed = all(ok for _, ok, _ in conditions)
    outcome = E1_SCIENTIFIC_PASS if passed else E1_SCIENTIFIC_DEGRADED
    return OutcomeDecision(
        outcome=outcome, claim_mode=CLAIM_MODE[outcome], conditions=tuple(conditions)
    )


def freeze_outcome(store: LockStore, decision: OutcomeDecision, result) -> str:
    """把判定寫進 e1_outcome.lock，回傳 payload hash。

    lock 不可覆寫：因此「結果不理想就重跑一次 final」在這一步會被擋下，
    而不是靠人記得 held-out 只能開一次。
    """
    payload = {
        "outcome": decision.outcome,
        "claim_mode": decision.claim_mode,
        "result_hashes": {
            "e1_result": result.result_hash(),
            "feature_scales": result.scales.hash(),
            "design": result.design_hash,
        },
        "conditions": decision.to_artifact()["conditions"],
        "bootstrap": result.bootstrap.to_artifact(),
        "heldout_access_count": result.heldout_access_count,
    }
    store.write("e1_outcome", payload)
    return store.load_hash("e1_outcome")


def assert_claim_mode(store: LockStore, claimed: str) -> None:
    """供下游檢查自己使用的口徑與 e1_outcome.lock 一致。

    §12.1：DEGRADED 時報告層不得顯示未限定範圍的 physics-calibrated 措辭。
    把它做成可呼叫的斷言，下游就不必自己記得該用哪個字串。
    """
    payload = store.load("e1_outcome")
    locked = str(payload["claim_mode"])
    if claimed != locked:
        raise OutcomeError(
            f"downstream claims {claimed!r} but e1_outcome.lock froze "
            f"{locked!r}. When E1 degrades, every downstream manifest and report "
            "must inherit the downgraded claim (SRC-SAI §12.1)."
        )
