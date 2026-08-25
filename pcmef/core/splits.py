# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 split plan-real 與 freeze real-split-policy 呼叫；
#         輸入 adapters 產出的 AlignmentReport 與 configs/base.yaml 的
#         real_split_policy 區塊；輸出 split_registry.json（E1-G02）與
#         real_split_policy.lock 的 payload（E1-G09）。
# 檔案路徑: pcmef/core/splits.py
# 產生時間: 2026-08-26 18:10 +08:00
# 版本: v0.1.1
# 功能說明: 決定每一筆真實 recording 要當校準用還是保留用，並把這個分配固定下來。
#           分配前先檢查每一類的可用筆數是否達標，分配後算出三組雜湊供凍結，
#           凍結之後就不允許重抽。
# 模組定位: 七類 split 中 real 側兩類（calibration / heldout_real）的產生器。
#           它「不是」synthetic split 的產生器 —— 那必須在 E1 outcome 之後
#           才建立（SRC-SAI FR-P0-03）。
# 主要責任:
#   1. GroupRule 判定並記錄採用 session-group 還是 seeded stratified
#   2. RealSplitPolicy 保存 allocation / minimum_per_class / seed 等決策值
#   3. plan_real_split() 執行前置檢查與分層抽樣，回傳 RealSplitPlan
#   4. RealSplitPlan.heldout_ids() 以用途守門，非 E1 final 一律拒絕
#   5. RealSplitPlan.to_lock_payload() 產生 real_split_policy.lock 的內容
#   6. RealSplitPlan.to_registry() 產生可版控且可與 lock 對照的 registry
# 維護提醒:
#   - 不得在 lock 之後重抽；SRC-SAI §7.9 明訂 redraw policy 為
#     FORBIDDEN_AFTER_LOCK，依 Held-out 結果調整分配等同讓結論自我實現。
#   - 不得在任一類別未達 minimum_per_class 時「就這樣先跑」；那是 BLOCK 條件。
#   - 不得為了湊比例而改用 recording 以外的切分單位；split_unit 固定為 recording，
#     以 500 個 measurement point 當獨立樣本是 SRC-PLAN §1 明列的偽重複。
#   - 不得在 E1 final 之外取用 heldout；heldout_ids() 會拒絕其他用途。
#   - registry 內的三組 set hash 不得移除；registry 進版控而 lock 不進，
#     那三個欄位是兩者唯一的對照點。
#   - v0.1.0 新增：首版 real split registry。
#   - v0.1.1 新增：registry 帶出三組 set hash，與 lock 交叉對照。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_splits.py -v
#   - py -3.10 -m pcmef.cli split plan-real --source-format edge-impulse --source data/raw_real/edge_impulse_export
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from pcmef.core.constants import CLASS_ORDER
from pcmef.core.hash import hash_object
from pcmef.core.schema import SplitRole

__all__ = [
    "SplitPolicyError",
    "SplitBlocked",
    "GroupRule",
    "RealSplitPolicy",
    "RealSplitPlan",
    "plan_real_split",
]

# heldout 唯一允許的用途（SRC-PLAN §3.1「E1 final evaluation only」）。
HELDOUT_PURPOSE = "e1_final_evaluation"


class SplitPolicyError(ValueError):
    """分配設定非法，或在不允許的情境下取用 heldout。"""


class SplitBlocked(SplitPolicyError):
    """前置條件未達成，正式流程必須停止。

    與一般錯誤分開的理由：BLOCK 是研究設計層級的判定
    （例如某類可用筆數不足），呼叫端不該把它當成可重試的例外吞掉。
    """


class GroupRule(str, Enum):
    """切分的分組依據。"""

    SESSION_GROUP = "session_group"
    SEEDED_STRATIFIED_RECORDING = "seeded_stratified_recording"


@dataclass(frozen=True)
class RealSplitPolicy:
    """real split 的決策值。全部由教授核定，實作端不得自行選擇。"""

    calibration_ratio: float
    heldout_ratio: float
    minimum_per_class: int
    seed: int
    group_rule: GroupRule = GroupRule.SEEDED_STRATIFIED_RECORDING
    group_rule_evidence: str = ""
    split_unit: str = "recording"
    stratification: str = "class"
    redraw_policy: str = "FORBIDDEN_AFTER_LOCK"

    def __post_init__(self) -> None:
        for name in ("calibration_ratio", "heldout_ratio"):
            value = getattr(self, name)
            if not 0.0 < value < 1.0:
                raise SplitPolicyError(f"{name} must lie in (0,1), got {value!r}")
        total = self.calibration_ratio + self.heldout_ratio
        if not np.isclose(total, 1.0, rtol=0.0, atol=1e-9):
            raise SplitPolicyError(
                f"calibration_ratio + heldout_ratio must equal 1, got {total!r}"
            )
        if self.minimum_per_class <= 0:
            raise SplitPolicyError(
                f"minimum_per_class must be positive, got {self.minimum_per_class!r}"
            )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise SplitPolicyError(f"seed must be an int, got {self.seed!r}")
        if self.split_unit != "recording":
            raise SplitPolicyError(
                "split_unit must be 'recording'; treating the 500 measurement points "
                "as independent samples is pseudoreplication (SRC-PLAN §1)"
            )
        if self.group_rule is GroupRule.SEEDED_STRATIFIED_RECORDING and not self.group_rule_evidence:
            raise SplitPolicyError(
                "falling back to seeded stratified recording requires evidence that "
                "no usable session/time grouping exists; record it explicitly"
            )


@dataclass(frozen=True)
class RealSplitPlan:
    """分配結果。尚未凍結，但內容已固定。"""

    policy: RealSplitPolicy
    calibration: dict[str, tuple[str, ...]]
    heldout: dict[str, tuple[str, ...]]
    eligible: dict[str, tuple[str, ...]]
    _heldout_access: list[str] = field(default_factory=list, repr=False)

    # -- 統計 -------------------------------------------------------------

    def counts(self) -> dict[str, dict[str, int]]:
        return {
            cls: {
                "eligible": len(self.eligible[cls]),
                "calibration": len(self.calibration[cls]),
                "heldout_real": len(self.heldout[cls]),
            }
            for cls in sorted(self.eligible)
        }

    def totals(self) -> dict[str, int]:
        return {
            "eligible": sum(len(v) for v in self.eligible.values()),
            "calibration": sum(len(v) for v in self.calibration.values()),
            "heldout_real": sum(len(v) for v in self.heldout.values()),
        }

    # -- 取用守門 ---------------------------------------------------------

    def calibration_ids(self) -> tuple[str, ...]:
        return tuple(sorted(i for ids in self.calibration.values() for i in ids))

    def heldout_ids(self, purpose: str) -> tuple[str, ...]:
        """取用 heldout。purpose 必須是 E1 final evaluation。

        SRC-PLAN §3.1：heldout 在 E1 final 前 access_count 必須為 0。
        本方法把「取用」變成需要具名理由的動作，並記錄每一次取用；
        任何其他用途一律拒絕，而不是靠呼叫端自律。
        """
        if purpose != HELDOUT_PURPOSE:
            raise SplitPolicyError(
                f"held-out data may only be accessed for {HELDOUT_PURPOSE!r}, "
                f"got {purpose!r}. Any other use — calibration, tuning, sanity "
                "checking — would consume the one-shot evaluation."
            )
        self._heldout_access.append(purpose)
        return tuple(sorted(i for ids in self.heldout.values() for i in ids))

    @property
    def heldout_access_count(self) -> int:
        return len(self._heldout_access)

    # -- 雜湊與凍結 -------------------------------------------------------

    def _set_hash(self, mapping: dict[str, tuple[str, ...]]) -> str:
        return hash_object({cls: sorted(ids) for cls, ids in sorted(mapping.items())})

    def to_registry(self) -> dict[str, Any]:
        """產生 split_registry.json（E1-G02 證據）。"""
        assignments = {}
        for cls, ids in sorted(self.calibration.items()):
            for identifier in ids:
                assignments[identifier] = SplitRole.CALIBRATION.value
        for cls, ids in sorted(self.heldout.items()):
            for identifier in ids:
                if identifier in assignments:
                    raise SplitPolicyError(
                        f"recording {identifier} assigned to two split roles; "
                        "this is an ID collision and must abort the run"
                    )
                assignments[identifier] = SplitRole.HELDOUT_REAL.value
        return {
            "gate": "E1-G02",
            "split_unit": self.policy.split_unit,
            "stratification": self.policy.stratification,
            "group_rule": self.policy.group_rule.value,
            "group_rule_evidence": self.policy.group_rule_evidence,
            "seed": self.policy.seed,
            "counts": self.counts(),
            "totals": self.totals(),
            "assignments": assignments,
            "collision_count": 0,
            "heldout_access_count": self.heldout_access_count,
            # registry 進版控、lock 不進（見 .gitignore）。這三個雜湊與
            # real_split_policy.lock 內的同名欄位由同一個 _set_hash 產生，
            # 所以有人改了 assignments 卻沒改雜湊、或改了雜湊卻對不上 lock，
            # 兩種竄改都看得出來。沒有這三個欄位，被版控的 registry 就是
            # 一份無法對照的孤兒檔。
            "eligible_set_hash": self._set_hash(self.eligible),
            "calibration_set_hash": self._set_hash(self.calibration),
            "heldout_real_set_hash": self._set_hash(self.heldout),
        }

    def to_lock_payload(self) -> dict[str, Any]:
        """產生 real_split_policy.lock 的 payload（E1-G09）。"""
        return {
            "creation_phase": "AFTER_M0_BEFORE_ANY_CALIBRATION",
            "split_unit": self.policy.split_unit,
            "stratification": self.policy.stratification,
            "group_rule": self.policy.group_rule.value,
            "group_rule_evidence": self.policy.group_rule_evidence,
            "allocation": {
                "calibration": self.policy.calibration_ratio,
                "heldout_real": self.policy.heldout_ratio,
            },
            "minimum_per_class": self.policy.minimum_per_class,
            "seed": self.policy.seed,
            "eligible_set_hash": self._set_hash(self.eligible),
            "calibration_set_hash": self._set_hash(self.calibration),
            "heldout_real_set_hash": self._set_hash(self.heldout),
            "counts": self.counts(),
            "totals": self.totals(),
            "heldout_access_count_at_lock": self.heldout_access_count,
            "redraw_policy": self.policy.redraw_policy,
        }


def plan_real_split(
    eligible_by_class: dict[str, list[str]], policy: RealSplitPolicy
) -> RealSplitPlan:
    """依政策把每類的 e1-eligible recordings 分成 calibration 與 heldout。

    分層單位是 class，切分單位是 recording。每類各自依比例分配並取最接近的
    可行整數，因此總數會自然落在整體比例附近而不需要事後調整 ——
    事後調整就是重抽的另一種說法。
    """
    missing = [cls for cls in CLASS_ORDER if cls not in eligible_by_class]
    if missing:
        raise SplitBlocked(
            f"no e1-eligible recordings for class(es) {missing}; the split cannot be "
            "planned until M0 yields every class"
        )

    short = {
        cls: len(eligible_by_class[cls])
        for cls in CLASS_ORDER
        if len(eligible_by_class[cls]) < policy.minimum_per_class
    }
    if short:
        raise SplitBlocked(
            f"minimum_per_class is {policy.minimum_per_class} e1-eligible recordings "
            f"but these classes fall short: {short}. The advisor decision specifies "
            "BLOCK in this case; the split must not proceed."
        )

    if policy.group_rule is GroupRule.SESSION_GROUP:
        raise SplitPolicyError(
            "session-group splitting is declared but not implemented; the current "
            "dataset carries no acquisition session metadata. Implement it only when "
            "such metadata actually exists, so the code path stays testable."
        )

    calibration: dict[str, tuple[str, ...]] = {}
    heldout: dict[str, tuple[str, ...]] = {}
    eligible: dict[str, tuple[str, ...]] = {}

    for cls in CLASS_ORDER:
        ids = sorted(eligible_by_class[cls])
        eligible[cls] = tuple(ids)
        # 每類獨立的 rng，且以 class 名稱參與 seeding：
        # 共用一條 rng 會讓某一類的筆數變動改變其他類的抽取結果，
        # 使得「只補了一類的資料」意外重抽了全部。
        rng = np.random.default_rng(
            [policy.seed, sum(ord(c) for c in cls), len(ids)]
        )
        permuted = list(rng.permutation(np.asarray(ids, dtype=object)))
        n_cal = int(round(len(ids) * policy.calibration_ratio))
        n_cal = max(1, min(n_cal, len(ids) - 1))
        calibration[cls] = tuple(sorted(str(i) for i in permuted[:n_cal]))
        heldout[cls] = tuple(sorted(str(i) for i in permuted[n_cal:]))

    plan = RealSplitPlan(
        policy=policy, calibration=calibration, heldout=heldout, eligible=eligible
    )
    # 立即驗證無 ID 碰撞；to_registry() 會在碰撞時拋例外。
    plan.to_registry()
    return plan
