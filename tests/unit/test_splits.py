# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；操作 core.splits 與 core.locks；
#         全部使用合成的 recording ID，不讀取 data/raw_real/。
# 檔案路徑: tests/unit/test_splits.py
# 產生時間: 2026-08-26 19:05 +08:00
# 版本: v0.1.0
# 功能說明: 驗證真實資料的切分依教授核定的比例與種子執行、每類筆數不足時直接
#           BLOCK、heldout 只能為 E1 final 取用，且凍結後不可重抽。
# 模組定位: real split 的驗收測試。它不驗證 synthetic split（那在 E1 outcome
#           之後才建立），也不驗證切分結果的科學性，只驗證規則被強制執行。
# 主要責任:
#   1. 比例分配與每類數量
#   2. minimum_per_class 未達成時的 BLOCK 路徑
#   3. heldout 取用守門與 access count
#   4. 決定性：同 seed 同結果、不同 seed 不同結果
#   5. ID 碰撞偵測與 lock 不可重抽
# 維護提醒:
#   - 不得放寬 test_heldout_requires_the_final_evaluation_purpose；
#     heldout 是一次性的，任何其他用途取用都會消耗掉它。
#   - 不得把 BLOCK 改成警告；那是研究設計層級的判定，不是可略過的提示。
#   - v0.1.0 新增：首版 real split 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_splits.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.core.constants import CLASS_ORDER
from pcmef.core.locks import LockError, LockStore
from pcmef.core.splits import (
    GroupRule,
    RealSplitPlan,
    RealSplitPolicy,
    SplitBlocked,
    SplitPolicyError,
    plan_real_split,
)

EVIDENCE = "no acquisition session metadata in the export; ingestion timestamps only"


def _policy(**overrides) -> RealSplitPolicy:
    defaults = dict(
        calibration_ratio=0.7,
        heldout_ratio=0.3,
        minimum_per_class=100,
        seed=20260826,
        group_rule=GroupRule.SEEDED_STRATIFIED_RECORDING,
        group_rule_evidence=EVIDENCE,
    )
    defaults.update(overrides)
    return RealSplitPolicy(**defaults)


def _eligible(per_class: int = 140) -> dict[str, list[str]]:
    return {
        cls: [f"{cls}/measurement_{n}" for n in range(1, per_class + 1)]
        for cls in CLASS_ORDER
    }


# ---------------------------------------------------------------------------
# 分配
# ---------------------------------------------------------------------------


def test_advisor_allocation_yields_the_expected_counts():
    """教授裁決：70/30、每類 140 筆 -> 98 calibration + 42 heldout。"""
    plan = plan_real_split(_eligible(140), _policy())
    for cls in CLASS_ORDER:
        assert len(plan.calibration[cls]) == 98
        assert len(plan.heldout[cls]) == 42
    assert plan.totals() == {"eligible": 560, "calibration": 392, "heldout_real": 168}


def test_每類分配互斥且涵蓋全部():
    plan = plan_real_split(_eligible(140), _policy())
    for cls in CLASS_ORDER:
        cal, hel = set(plan.calibration[cls]), set(plan.heldout[cls])
        assert cal & hel == set()
        assert cal | hel == set(plan.eligible[cls])


def test_uneven_class_sizes_still_split_near_the_ratio():
    eligible = _eligible(140)
    eligible["Misty"] = eligible["Misty"][:117]
    plan = plan_real_split(eligible, _policy())
    counts = plan.counts()["Misty"]
    assert counts["calibration"] + counts["heldout_real"] == 117
    assert counts["calibration"] == round(117 * 0.7)


# ---------------------------------------------------------------------------
# BLOCK 條件
# ---------------------------------------------------------------------------


def test_class_below_minimum_blocks_the_split():
    """裁決明訂：任何 class < 100 則 BLOCK。"""
    eligible = _eligible(140)
    eligible["Bubbly"] = eligible["Bubbly"][:99]
    with pytest.raises(SplitBlocked, match="fall short"):
        plan_real_split(eligible, _policy())


def test_missing_class_blocks_the_split():
    eligible = _eligible(140)
    del eligible["Empty"]
    with pytest.raises(SplitBlocked, match="no e1-eligible"):
        plan_real_split(eligible, _policy())


def test_session_group_rule_is_refused_until_metadata_exists():
    """宣告 group-disjoint 但無 session metadata 時不得默默退回。"""
    with pytest.raises(SplitPolicyError, match="not implemented"):
        plan_real_split(
            _eligible(140),
            _policy(group_rule=GroupRule.SESSION_GROUP, group_rule_evidence="x"),
        )


def test_seeded_fallback_requires_recorded_evidence():
    with pytest.raises(SplitPolicyError, match="requires evidence"):
        _policy(group_rule_evidence="")


@pytest.mark.parametrize(
    "bad",
    [
        {"calibration_ratio": 0.7, "heldout_ratio": 0.4},
        {"calibration_ratio": 0.0, "heldout_ratio": 1.0},
        {"minimum_per_class": 0},
        {"split_unit": "measurement_point"},
    ],
)
def test_invalid_policy_is_refused(bad):
    with pytest.raises(SplitPolicyError):
        _policy(**bad)


# ---------------------------------------------------------------------------
# heldout 守門
# ---------------------------------------------------------------------------


def test_heldout_requires_the_final_evaluation_purpose():
    """SRC-PLAN §3.1：heldout 僅供 E1 final evaluation。"""
    plan = plan_real_split(_eligible(140), _policy())
    for purpose in ("calibration", "sanity_check", "tuning", ""):
        with pytest.raises(SplitPolicyError, match="may only be accessed"):
            plan.heldout_ids(purpose)
    assert plan.heldout_access_count == 0


def test_heldout_access_is_counted():
    plan = plan_real_split(_eligible(140), _policy())
    ids = plan.heldout_ids("e1_final_evaluation")
    assert len(ids) == 168
    assert plan.heldout_access_count == 1


def test_calibration_is_freely_accessible():
    plan = plan_real_split(_eligible(140), _policy())
    assert len(plan.calibration_ids()) == 392
    assert plan.heldout_access_count == 0


# ---------------------------------------------------------------------------
# 決定性
# ---------------------------------------------------------------------------


def test_same_seed_reproduces_the_split():
    a = plan_real_split(_eligible(140), _policy())
    b = plan_real_split(_eligible(140), _policy())
    assert a.calibration == b.calibration
    assert a.heldout == b.heldout


def test_different_seed_changes_the_split():
    a = plan_real_split(_eligible(140), _policy())
    b = plan_real_split(_eligible(140), _policy(seed=99999))
    assert a.calibration != b.calibration


def test_adding_data_to_one_class_does_not_reshuffle_the_others():
    """每類獨立 rng：補了一類的資料不該意外重抽其他類。"""
    base = plan_real_split(_eligible(140), _policy())
    changed_input = _eligible(140)
    changed_input["Misty"] = changed_input["Misty"] + ["Misty/measurement_141"]
    changed = plan_real_split(changed_input, _policy())
    for cls in CLASS_ORDER:
        if cls == "Misty":
            continue
        assert base.calibration[cls] == changed.calibration[cls]
    assert base.calibration["Misty"] != changed.calibration["Misty"]


# ---------------------------------------------------------------------------
# registry 與 lock
# ---------------------------------------------------------------------------


def test_registry_reports_no_collision_and_full_assignment():
    registry = plan_real_split(_eligible(140), _policy()).to_registry()
    assert registry["gate"] == "E1-G02"
    assert registry["collision_count"] == 0
    assert len(registry["assignments"]) == 560
    assert set(registry["assignments"].values()) == {"calibration", "heldout_real"}


def test_registry_and_lock_share_the_same_set_hashes():
    """registry 進版控、lock 不進，兩者必須能互相對照。"""
    plan = plan_real_split(_eligible(140), _policy())
    registry, payload = plan.to_registry(), plan.to_lock_payload()
    for key in ("eligible_set_hash", "calibration_set_hash", "heldout_real_set_hash"):
        assert registry[key] == payload[key], f"{key} diverged between the two artifacts"


def test_registry_detects_an_id_collision():
    plan = plan_real_split(_eligible(140), _policy())
    tainted = RealSplitPlan(
        policy=plan.policy,
        calibration=plan.calibration,
        # 讓一筆同時出現在兩邊。
        heldout={**plan.heldout, "Empty": plan.heldout["Empty"] + plan.calibration["Empty"][:1]},
        eligible=plan.eligible,
    )
    with pytest.raises(SplitPolicyError, match="ID collision"):
        tainted.to_registry()


def test_lock_payload_carries_the_three_set_hashes(tmp_path):
    plan = plan_real_split(_eligible(140), _policy())
    payload = plan.to_lock_payload()
    for key in ("eligible_set_hash", "calibration_set_hash", "heldout_real_set_hash"):
        assert len(payload[key]) == 64
    assert payload["creation_phase"] == "AFTER_M0_BEFORE_ANY_CALIBRATION"
    assert payload["redraw_policy"] == "FORBIDDEN_AFTER_LOCK"
    assert payload["heldout_access_count_at_lock"] == 0

    store = LockStore(tmp_path)
    store.write("real_split_policy", payload)
    assert store.load("real_split_policy")["seed"] == 20260826


def test_redraw_after_lock_is_refused(tmp_path):
    """裁決明訂：lock 後禁止 redraw。"""
    store = LockStore(tmp_path)
    store.write("real_split_policy", plan_real_split(_eligible(140), _policy()).to_lock_payload())
    redrawn = plan_real_split(_eligible(140), _policy(seed=99999)).to_lock_payload()
    with pytest.raises(LockError, match="immutable"):
        store.write("real_split_policy", redrawn)


def test_refreezing_identical_content_is_idempotent(tmp_path):
    store = LockStore(tmp_path)
    payload = plan_real_split(_eligible(140), _policy()).to_lock_payload()
    store.write("real_split_policy", payload)
    store.write("real_split_policy", payload)
    assert store.exists("real_split_policy")
