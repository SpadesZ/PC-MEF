# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.experiments.e1_final 與 pcmef.core.heldout_partition；
#         以合成資料與 tmp_path 假 repo 驗證，**不開啟 FORMAL_E1_FINAL**、
#         不算圖、不動真實 lock 或帳本。由 pytest 收集執行。
# 檔案路徑: tests/unit/test_e1_final.py
# 產生時間: 2026-08-31 00:40 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 E1 最終評估的三件事 —— 評估種子與校準用過的種子不相交、
#           per-scenario 量的是「離真實多遠」而不是原始值、
#           以及「凍結在前、開啟在後」的順序真的擋得住。
# 模組定位: E1 final 的驗收。它不驗證 E1 的結論（那要等真的跑），只驗證
#           那個結論如果產生了，它的產生條件是成立的。
# 主要責任:
#   1. test_evaluation_seeds_are_disjoint_from_calibration() 種子不相交
#   2. test_scenario_ids_are_shared_across_classes() bootstrap 查得到表
#   3. test_per_unit_is_a_discrepancy_not_a_raw_value() Delta 才有改善的意思
#   4. test_trend_is_recorded_as_not_applicable() 不得被讀成通過
#   5. test_final_partition_cannot_be_opened_before_the_lock() 順序守得住
# 維護提醒:
#   - 不得在本檔呼叫 final_ids() 於真實 repo；那會把 heldout_access_count
#     推到 1，等於用一次測試燒掉整個研究唯一的一次最終評估。
#   - 不得把「趨勢不適用」改寫成通過。沒有證據就是沒有證據。
#   - v0.1.0 新增：AMD-005 E1 final 驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_e1_final.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.experiments.calibration_objective import CRN_SEEDS, VERIFICATION_SEEDS
from pcmef.experiments.e1_final import (
    ACQUISITION_SEED_BASE,
    E1_SCENARIOS_PER_CLASS,
    OPTICAL_SEED_BASE,
    E1FinalError,
    _trend_not_applicable,
    load_final_values,
    scenario_seed_matrix,
    simulate_candidate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 評估設計
# ---------------------------------------------------------------------------


def test_scenario_seed_matrix_is_deterministic():
    assert scenario_seed_matrix() == scenario_seed_matrix()


def test_evaluation_seeds_are_disjoint_from_calibration():
    """在被擬合過的那組實現上做最終評估，量到的是記憶不是保真度。"""
    matrix = scenario_seed_matrix()
    used = set(matrix["optical_transport_seeds"].values()) | set(
        matrix["acquisition_seed_matrix"].values()
    )
    assert not used & set(CRN_SEEDS.values())
    assert not used & set(VERIFICATION_SEEDS.values())
    assert min(used) >= min(OPTICAL_SEED_BASE, ACQUISITION_SEED_BASE)


def test_scenario_ids_are_shared_across_classes():
    """id 不得含 class：E1Engine 的成對重抽在每一類上查同一個 id。"""
    matrix = scenario_seed_matrix()
    ids = matrix["base_scenario_ids"]
    assert len(ids) == E1_SCENARIOS_PER_CLASS == 28
    assert len(set(ids)) == len(ids)
    for class_label in CLASS_ORDER:
        assert not any(class_label.lower() in i.lower() for i in ids)


def test_scenario_count_matches_the_final_partition_per_class():
    """模擬側與真實側的 scenario 數相同，推論單位才有意義。"""
    from pcmef.core.heldout_partition import FINAL_PER_CLASS

    assert E1_SCENARIOS_PER_CLASS == FINAL_PER_CLASS


def test_trend_is_recorded_as_not_applicable_not_as_a_pass():
    trend = _trend_not_applicable()
    assert trend["status"] == "NOT_APPLICABLE"
    assert trend["offset_strata"] == ["baseline"]
    assert "NOT a passed trend check" in trend["must_not_be_read_as"]


# ---------------------------------------------------------------------------
# 候選觀測
# ---------------------------------------------------------------------------


class _FakeSimulator:
    """決定性的假模擬：回傳一個受參數平移的常態樣本。"""

    def __init__(self) -> None:
        self.calls = 0

    def recordings(self, scene, surrogate, seeds):
        self.calls += 1
        shift = float(surrogate.get("signal_energy_to_mcps", 1.0))
        out = {}
        for index, class_label in enumerate(CLASS_ORDER):
            rng = np.random.default_rng(int(seeds[class_label]) + index)
            out[class_label] = (
                rng.standard_normal((40, len(TOF_SCHEMA))) + 10.0 * shift
            )
        return out


def _fake_real(loc: float = 10.0):
    rng = np.random.default_rng(3)
    return {
        c: {
            f: rng.normal(loc, 1.0, size=400)
            for f in TOF_SCHEMA
        }
        for c in CLASS_ORDER
    }


def test_per_unit_is_a_discrepancy_not_a_raw_value():
    """per_unit 必須是「離真實多遠」；否則 Delta 不代表改善。

    真實分佈中心在 10。shift=1 的候選中心也在 10（貼近），shift=3 的候選
    中心在 30（很遠）。因此貼近者的 per_unit 必須明顯較小。
    """
    matrix = scenario_seed_matrix(4)
    real = _fake_real(10.0)

    close = simulate_candidate(
        "close", {"signal_energy_to_mcps": 1.0}, None, _FakeSimulator(), real, matrix
    )
    far = simulate_candidate(
        "far", {"signal_energy_to_mcps": 3.0}, None, _FakeSimulator(), real, matrix
    )

    for class_label in CLASS_ORDER:
        for feature in TOF_SCHEMA:
            near = np.mean(list(close.per_unit[class_label][feature].values()))
            distant = np.mean(list(far.per_unit[class_label][feature].values()))
            assert near < distant, (
                f"{class_label}|{feature}: the closer candidate must have the "
                "smaller per-scenario discrepancy"
            )


def test_candidates_share_scenarios_and_seed_matrix():
    """Initial 與 Calibrated 必須共用 base scenarios 與 seed matrix。"""
    matrix = scenario_seed_matrix(4)
    real = _fake_real()
    a = simulate_candidate(
        "initial", {"signal_energy_to_mcps": 1.0}, None, _FakeSimulator(), real, matrix
    )
    b = simulate_candidate(
        "calibrated", {"signal_energy_to_mcps": 2.0}, None, _FakeSimulator(), real,
        matrix,
    )
    assert a.base_scenario_ids == b.base_scenario_ids
    assert a.seed_matrix_hash == b.seed_matrix_hash
    for class_label in CLASS_ORDER:
        for feature in TOF_SCHEMA:
            assert set(a.per_unit[class_label][feature]) == set(matrix["base_scenario_ids"])


def test_every_class_and_feature_is_populated():
    matrix = scenario_seed_matrix(3)
    candidate = simulate_candidate(
        "c", {"signal_energy_to_mcps": 1.0}, None, _FakeSimulator(), _fake_real(), matrix
    )
    for class_label in CLASS_ORDER:
        for feature in TOF_SCHEMA:
            assert len(candidate.values[class_label][feature]) == 3 * 40
            assert len(candidate.per_unit[class_label][feature]) == 3


# ---------------------------------------------------------------------------
# 只開被交出來的那些檔案
# ---------------------------------------------------------------------------


def test_load_final_values_rejects_a_class_mismatch(tmp_path):
    """交出來的 ID 與其 label 對不上，就是分割與 adapter 不同步。"""
    (tmp_path / "training").mkdir()
    with pytest.raises(E1FinalError) as excinfo:
        load_final_values({"Empty": ["water-filled/measurement_1"]}, tmp_path)
    assert excinfo.value.reason == "ID_CLASS_MISMATCH"


def test_load_final_values_rejects_a_missing_file(tmp_path):
    (tmp_path / "training").mkdir()
    (tmp_path / "testing").mkdir()
    with pytest.raises(E1FinalError) as excinfo:
        load_final_values({"Empty": ["empty/measurement_999"]}, tmp_path)
    assert excinfo.value.reason == "SOURCE_FILE_AMBIGUOUS"


def test_load_final_values_reads_only_the_ids_it_was_handed(tmp_path):
    """多餘的檔案存在也不得被讀到；清單是唯一的授權。"""
    (tmp_path / "training").mkdir()
    (tmp_path / "testing").mkdir()
    for measurement in ("measurement_1", "measurement_2"):
        payload = {
            "payload": {"values": np.zeros((500, len(TOF_SCHEMA))).tolist()}
        }
        (tmp_path / "training" / f"empty.{measurement}.csv.abc.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    values = load_final_values({"Empty": ["empty/measurement_1"]}, tmp_path)
    # 只讀了一筆 500 列，不是兩筆 1000 列。
    assert values["Empty"]["distance_mm"].shape == (500,)


# ---------------------------------------------------------------------------
# 凍結在前、開啟在後
# ---------------------------------------------------------------------------


def test_final_partition_cannot_be_opened_before_the_lock(tmp_path):
    """這是整個 protected-final-test 設計的實質保護。"""
    import shutil

    from pcmef.core.heldout_partition import HeldoutPartitionError, final_ids

    root = tmp_path / "repo"
    (root / "data" / "splits").mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "data" / "splits" / "split_registry.json",
        root / "data" / "splits" / "split_registry.json",
    )
    # 假 repo 代表**開啟最終測試之前**的狀態；真實 registry 在 E1 之後是 1，
    # 直接沿用會讓這些取用測試量到的是 E1 的結果而不是它們自己的前提。
    registry_path = root / "data" / "splits" / "split_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["heldout_access_count"] = 0
    registry.pop("heldout_final_access_entries", None)
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    freeze_dir = root / "freeze"
    freeze_dir.mkdir()
    shutil.copy(
        REPO_ROOT / "freeze" / "heldout_partition.lock.json",
        freeze_dir / "heldout_partition.lock.json",
    )

    with pytest.raises(HeldoutPartitionError) as excinfo:
        final_ids("e1_final_evaluation", "whatever", "abc", freeze_dir, root)
    assert excinfo.value.reason == "CALIBRATION_NOT_FROZEN"

    registry = json.loads(
        (root / "data" / "splits" / "split_registry.json").read_text(encoding="utf-8")
    )
    assert registry["heldout_access_count"] == 0, (
        "a refused open must not leave a trace on the counter"
    )


def test_live_repository_opened_the_final_partition_at_most_once():
    """開第二次就不是最終測試了。"""
    registry = json.loads(
        (REPO_ROOT / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["heldout_access_count"] in (0, 1)
