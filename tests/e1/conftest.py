# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 自動載入，供 tests/e1/ 下的引擎與狀態機測試使用；
#         在 tmp_path 建立 lock 鏈與合成的四特徵觀測；不讀真實資料。
# 檔案路徑: tests/e1/conftest.py
# 產生時間: 2026-08-27 14:50 +08:00
# 版本: v0.1.0
# 功能說明: 造出 E1 需要的最小完整輸入 —— 真實 held-out、兩個候選、
#           特徵尺度，以及 e1_scientific_rule 之前那一整串前置 lock。
# 模組定位: 測試夾具集中處。它「不是」被測程式的一部分，也不得包含斷言。
# 主要責任:
#   1. lock_chain 依 core.locks 的相依圖凍結 e1_candidates 前後的全部 lock
#   2. make_candidate 造出可控距離的候選
#   3. scales 提供固定的 s_f，讓 NW 可手算
#   4. real_heldout 提供四類 x 四特徵的真實分佈
#   5. improving_pair / regressing_pair 對應 PASS 與 DEGRADED 兩種情境
# 維護提醒:
#   - 不得在夾具內寫斷言；夾具失敗要以例外呈現。
#   - 不得放寬 lock 鏈；那條相依鏈就是 §23 state machine 本身。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -m pytest tests/e1 -q
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.core.locks import LockStore
from pcmef.experiments.e1 import CandidateObservations, FeatureScales

SCENARIOS = tuple(f"syn_clean_{i:04d}" for i in range(12))
SEED_MATRIX = "seedmatrix-abc123"

_CHAIN = (
    ("real_split_policy", {
        "creation_phase": "AFTER_M0_BEFORE_ANY_CALIBRATION", "split_unit": "recording",
        "stratification": "class", "group_rule": "seeded_stratified_recording",
        "allocation": {"calibration": 0.7, "heldout_real": 0.3},
        "minimum_per_class": 100, "seed": 20260826,
        "eligible_set_hash": "e" * 8, "calibration_set_hash": "c" * 8,
        "heldout_real_set_hash": "h" * 8, "redraw_policy": "FORBIDDEN_AFTER_LOCK",
    }),
    ("initial_simulation", {
        "scene_hash": "s1", "surrogate_hash": "u1",
        "code_version": "v0", "parameter_ranges": {},
    }),
    ("calibrated_simulation", {
        "calibrated_scene_hash": "s2", "calibrated_surrogate_hash": "u2",
        "calibration_source_hashes": [],
    }),
    ("metric_config", {"metric_definitions": {"primary": "w1"}, "code_hash": "m1"}),
    ("e1_candidates", {
        "initial_simulation_hash": "a", "calibrated_simulation_hash": "b",
        "metric_config_hash": "c", "heldout_set_hash": "d",
    }),
    ("e1_evaluation_design", {
        "base_scenario_ids": list(SCENARIOS), "offset_strata": ["baseline"],
        "optical_transport_seeds": [1, 2], "acquisition_seed_matrix": [[1, 2]],
        "matched_realization_hash": SEED_MATRIX,
    }),
)

DEFAULT_RULE = {
    "normalization_scales": "calibration_only_pooled_iqr",
    "aggregation": {
        "macro_mean_delta_ci_lower_bound_gt": 0.0,
        "per_feature_class_macro_delta_gte": 0.0,
        "distance_trend_consistency": "non_degraded",
    },
    "improvement_threshold": 0.0,
    "regression_tolerance": 0.0,
    "trend_rule": "non_degraded",
    "bootstrap_replicates": 500,
    "bootstrap_seed": 20260826,
    "code_hash": "e1rule",
    # NOTE(NOTE-028): e1_scientific_rule 必須保存 E1-G08 的 amendment 追溯，
    # 否則 formal run 事後無法證明依哪一版判準通過。
    "amendment_id": "AMD-001",
    "amendment_payload_hash": "a" * 64,
    "g08_contract_version": "v2",
}


@pytest.fixture
def store(tmp_path) -> LockStore:
    return LockStore(tmp_path / "freeze")


@pytest.fixture
def lock_chain(store):
    for name, payload in _CHAIN:
        store.write(name, payload)
    return store


@pytest.fixture
def ready_store(lock_chain):
    lock_chain.write("e1_scientific_rule", dict(DEFAULT_RULE))
    return lock_chain


@pytest.fixture
def scales() -> FeatureScales:
    return FeatureScales(scales={f: 10.0 for f in TOF_SCHEMA})


@pytest.fixture
def real_heldout() -> dict:
    rng = np.random.default_rng(11)
    return {
        cls: {f: rng.normal(100.0, 5.0, size=200).tolist() for f in TOF_SCHEMA}
        for cls in CLASS_ORDER
    }


def _make_candidate(name, offset, jitter_seed, *, scenarios=SCENARIOS,
                    seed_matrix=SEED_MATRIX):
    rng = np.random.default_rng(jitter_seed)
    values, per_unit = {}, {}
    for cls in CLASS_ORDER:
        values[cls], per_unit[cls] = {}, {}
        for feature in TOF_SCHEMA:
            values[cls][feature] = rng.normal(100.0 + offset, 5.0, size=200).tolist()
            per_unit[cls][feature] = {
                s: float(abs(offset) + rng.normal(0.0, 0.05)) for s in scenarios
            }
    return CandidateObservations(
        name=name, values=values, per_unit=per_unit,
        base_scenario_ids=tuple(scenarios), seed_matrix_hash=seed_matrix,
    )


@pytest.fixture
def make_candidate():
    return _make_candidate


@pytest.fixture
def improving_pair():
    return (_make_candidate("initial", 8.0, 21), _make_candidate("calibrated", 1.0, 22))


@pytest.fixture
def regressing_pair():
    return (_make_candidate("initial", 1.0, 31), _make_candidate("calibrated", 8.0, 32))
