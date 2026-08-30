# PC-MEF Research System source maintenance contract
# 上下游: 讀 outputs/calibration/calibration_report.json 的校準結果、
#         freeze/ 的 initial_simulation 與 heldout_partition lock；
#         寫出 freeze/ 的 calibrated_simulation / metric_config /
#         e1_candidates / e1_evaluation_design / e1_scientific_rule /
#         claim_boundary / e1_outcome 七個 lock 與
#         outputs/e1/e1_final_report.json；由 cli 的 `e1 final` 呼叫。
# 檔案路徑: pcmef/experiments/e1_final.py
# 產生時間: 2026-08-31 00:10 +08:00
# 版本: v0.1.0
# 功能說明: 依 AMD-005 的 protected-final-test 設計跑完 E1 —— 先把校準後的
#           模擬器與全部判準凍結，**然後才**開啟 FORMAL_E1_FINAL 一次，
#           比較 Initial 與 Calibrated 對真實分佈的距離並判定 PASS/DEGRADED。
# 模組定位: E1 的執行層。判準（三條 PASS 條件、bootstrap 次數與種子）全部
#           來自 configs/base.yaml 與凍結的 lock，本檔不決定任何一條；
#           它只保證「凍結在前、開啟在後」這個順序真的成立。
# 主要責任:
#   1. scenario_seed_matrix() 產生與校準種子不相交的評估種子矩陣
#   2. simulate_candidate() 把一組參數變成 CandidateObservations
#   3. freeze_pre_heldout_locks() 在開啟最終測試之前凍結六個 lock
#   4. run_e1_final() 開啟 FORMAL_E1_FINAL 一次並產出 E1 結果
#   5. _trend_not_applicable() 具名記錄趨勢判準為何不適用
# 維護提醒:
#   - 不得在 freeze_pre_heldout_locks() 之前呼叫 final_ids()。凍結在前、
#     開啟在後是本檔存在的唯一理由；順序反了，判準就是看過答案才定的。
#   - 不得用校準的 CRN 種子（1001-1042）或 verification 種子（2001-2042）
#     當評估種子。那等於在被擬合過的那組實現上做最終評估。
#   - 不得在 E1 產出結果之後回頭改任何 lock 或重跑校準；lock 是不可變的。
#   - 不得把「趨勢不適用」當成趨勢通過。真實最終測試沒有 ±offset 分層，
#     因此該條件必須具名記錄為 NOT_APPLICABLE，不得靜默視為 PASS。
#   - 不得讓 DEGRADED 觸發重新校準；它是結論，不是「再調一次就好」。
#   - v0.1.0 新增：首版 E1 final（AMD-005 protected-final-test）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_e1_final.py -v
#   - py -3.10 -m pcmef.cli e1 final --dry-run
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.core.hash import hash_file, hash_object

__all__ = [
    "E1_SCENARIOS_PER_CLASS",
    "OPTICAL_SEED_BASE",
    "ACQUISITION_SEED_BASE",
    "E1FinalError",
    "scenario_seed_matrix",
    "simulate_candidate",
    "freeze_pre_heldout_locks",
    "run_e1_final",
]

#: 每個 base scenario 在四類上各實現一次。28 對齊 FORMAL_E1_FINAL 的
#: 每類筆數，因此模擬側與真實側的 scenario 數相同 —— bootstrap 的推論單位
#: 是 scenario，兩側數量差太多會讓那個單位失去意義。
E1_SCENARIOS_PER_CLASS = 28

#: 評估種子刻意與校準用過的兩組**不相交**：
#: 校準 CRN 是 1001/1002/1042/1004，verification 是 2001/2002/2042/2004。
#: 在被擬合過的實現上做最終評估，量到的是記憶不是保真度。
OPTICAL_SEED_BASE = 30000
ACQUISITION_SEED_BASE = 40000

_HELDOUT_PURPOSE = "e1_final_evaluation"


class E1FinalError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


# ---------------------------------------------------------------------------
# 評估設計
# ---------------------------------------------------------------------------


def scenario_seed_matrix(n: int = E1_SCENARIOS_PER_CLASS) -> dict[str, Any]:
    """base scenario 與其光學／取樣種子。

    scenario id **不含 class**：一個 base scenario 是一組場景實現，
    它在四類上各跑一次。E1Engine 的成對重抽以 scenario 為單位，且它從
    第一個 class 取 unit_ids 後在**每一類**上查同一個 id，因此 id 必須
    跨類共用 —— 把 class 編進 id 會讓那個查表在第二類就 KeyError。
    """
    scenarios = [f"s{index:02d}" for index in range(n)]
    optical = {s: OPTICAL_SEED_BASE + index for index, s in enumerate(scenarios)}
    acquisition = {s: ACQUISITION_SEED_BASE + index for index, s in enumerate(scenarios)}
    matrix = {
        "base_scenario_ids": scenarios,
        "optical_transport_seeds": optical,
        "acquisition_seed_matrix": acquisition,
    }
    matrix["seed_matrix_hash"] = hash_object(
        {"optical": optical, "acquisition": acquisition}
    )
    return matrix


def _trend_not_applicable() -> dict[str, Any]:
    """趨勢判準為何不適用，具名記錄。

    ±offset 的擾動序列來自一次**獨立採集**，而 CAL-PREREG-003
    data.forbidden_sources 明列它不得進入本研究的擬合或評估資料。
    FORMAL_E1_FINAL 只有 baseline 一個分層，因此
    `distance_trend_consistency` 沒有可比的對象。
    """
    return {
        "status": "NOT_APPLICABLE",
        "offset_strata": ["baseline"],
        "why": (
            "FORMAL_E1_FINAL carries only the baseline stratum. The +/-offset "
            "series comes from a separate acquisition that CAL-PREREG-003 "
            "data.forbidden_sources excludes from this study's fitting and "
            "evaluation data, so there is no real offset series to compare "
            "against."
        ),
        "must_not_be_read_as": (
            "This is NOT a passed trend check. The condition has no evidence "
            "either way and is recorded as NOT_APPLICABLE so that a vacuous "
            "pass cannot be mistaken for a demonstrated one."
        ),
    }


# ---------------------------------------------------------------------------
# 模擬候選
# ---------------------------------------------------------------------------


def simulate_candidate(
    name: str,
    parameter_values: dict[str, float],
    identity: Any,
    simulator: Any,
    real_values: dict[str, dict[str, np.ndarray]],
    matrix: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> Any:
    """把一組參數變成 E1 的 CandidateObservations。

    `values[class][feature]` 是全部 scenario 的樣本池（供 cell 層 W1）；
    `per_unit[class][feature][scenario]` 是**該 scenario 對真實分佈的 W1**
    （供以 scenario 為單位的成對重抽）。後者必須是「離真實多遠」而不是
    原始值 —— bootstrap 的 Delta = initial - calibrated 只有在兩邊都是
    差距時才代表改善。
    """
    from pcmef.experiments.calibration_objective import (
        SCENE_CONSTANT_BY_DIMENSION,
        SURROGATE_DIMENSIONS,
    )
    from pcmef.experiments.e1 import CandidateObservations
    from pcmef.stats.metrics import wasserstein_w1

    say = progress or (lambda _m: None)
    scene = {
        k: float(v) for k, v in parameter_values.items()
        if k in SCENE_CONSTANT_BY_DIMENSION
    }
    surrogate = {
        k: float(v) for k, v in parameter_values.items() if k in SURROGATE_DIMENSIONS
    }

    scenarios = matrix["base_scenario_ids"]
    per_scenario: dict[str, dict[str, np.ndarray]] = {}
    for index, scenario in enumerate(scenarios):
        seeds = {c: int(matrix["optical_transport_seeds"][scenario]) for c in CLASS_ORDER}
        recordings = simulator.recordings(scene, surrogate, seeds)
        per_scenario[scenario] = recordings
        if (index + 1) % 7 == 0:
            say(f"  [{name}] {index + 1}/{len(scenarios)} base scenarios")

    values: dict[str, dict[str, list[float]]] = {}
    per_unit: dict[str, dict[str, dict[str, float]]] = {}
    for class_label in CLASS_ORDER:
        values[class_label] = {}
        per_unit[class_label] = {}
        for feature_index, feature in enumerate(TOF_SCHEMA):
            pooled: list[float] = []
            unit: dict[str, float] = {}
            real = real_values[class_label][feature]
            for scenario in scenarios:
                column = per_scenario[scenario][class_label][:, feature_index]
                pooled.extend(float(v) for v in column)
                unit[scenario] = wasserstein_w1(real, column)
            values[class_label][feature] = pooled
            per_unit[class_label][feature] = unit

    return CandidateObservations(
        name=name,
        values=values,
        per_unit=per_unit,
        base_scenario_ids=tuple(scenarios),
        seed_matrix_hash=str(matrix["seed_matrix_hash"]),
    )


# ---------------------------------------------------------------------------
# 開啟最終測試之前的凍結
# ---------------------------------------------------------------------------


def freeze_pre_heldout_locks(
    report: dict[str, Any],
    matrix: dict[str, Any],
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    code_version: str = "",
) -> dict[str, str]:
    """在 FORMAL_E1_FINAL 被開啟之前凍結六個 lock。

    順序是實質的，不是形式：判準一旦晚於答案，它就不是判準。
    """
    from pcmef.core.config import load_config
    from pcmef.core.locks import LockStore

    root = Path(repo_root)
    store = LockStore(freeze_dir)
    identity = report["identity"]

    if report["overall_outcome"] not in {"CALIBRATION_COMPLETE"}:
        raise E1FinalError(
            "CALIBRATION_INCOMPLETE",
            f"calibration outcome is {report['overall_outcome']!r}; the simulator "
            "may only be frozen after all five stages reach a resolved outcome",
        )

    calibrated = report["calibrated_simulation_lock"]["calibrated_parameter_values"]
    fitted = report["seven_calibrated_parameters"]

    # 1. calibrated_simulation
    store.write(
        "calibrated_simulation",
        {
            "calibrated_scene_hash": hash_object(
                {
                    k: v for k, v in sorted(calibrated.items())
                    if not isinstance(v, (dict, list))
                }
            ),
            "calibrated_surrogate_hash": hash_object(
                {k: float(v) for k, v in sorted(fitted.items())}
            ),
            "calibration_source_hashes": {
                "preregistration": identity["preregistration_hash"],
                "protocol": identity["protocol_hash"],
                "stage0": identity["stage0_hash"],
                "s_f": identity["sf_hash"],
                "raw_calibration_data": identity["raw_calibration_data_hash"],
                "initial_simulation": identity["initial_simulation_lock_hash"],
                "bounds_resolution": identity["bounds_resolution_hash"],
                "parameter_set": identity["parameter_set_hash"],
                "amendments": _amendment_hashes(freeze_dir),
            },
            "calibrated_parameter_values": calibrated,
            "fitted_parameters": fitted,
            "stage_outcomes": report["calibrated_simulation_lock"]["stage_outcomes"],
            "inhibited_stages": report["inhibitory_control"]["inhibited_stages"],
            "inhibitory_control_claim_boundary": report["inhibitory_control"][
                "claim_boundary"
            ],
            "code_version": code_version,
        },
    )

    # 2. metric_config
    store.write(
        "metric_config",
        {
            "metric_definitions": {
                "primary": "wasserstein_w1 per class x feature on the pooled "
                           "distribution, reported raw and as NW = W1 / s_f",
                "normalization": "s_f frozen in CAL-SF-001, calibration-only pooled IQR",
                "delta": "NW_initial - NW_calibrated; positive means calibration "
                         "moved the simulator closer to the real sensor",
                "inference_unit": "base scenario (paired bootstrap), not measurement point",
                "secondary": ["mean_sd_error", "class_distance_ordering"],
            },
            "code_hash": hash_file(root / "pcmef" / "stats" / "metrics.py"),
            "s_f": identity["s_f"],
            "s_f_record": "CAL-SF-001",
            "s_f_hash": identity["sf_hash"],
        },
    )

    # 3. e1_candidates
    partition = json.loads(
        (Path(freeze_dir) / "heldout_partition.lock.json").read_text(encoding="utf-8")
    )
    store.write(
        "e1_candidates",
        {
            "initial_simulation_hash": store.load_hash("initial_simulation"),
            "calibrated_simulation_hash": store.load_hash("calibrated_simulation"),
            "metric_config_hash": store.load_hash("metric_config"),
            "heldout_set_hash": partition["payload"]["set_hashes"]["FORMAL_E1_FINAL"],
            "heldout_partition_id": partition["payload"]["partition_id"],
            "heldout_partition_hash": partition["payload_hash"],
            "heldout_subset": "FORMAL_E1_FINAL",
            "recordings_per_class": partition["payload"]["counts"]["FORMAL_E1_FINAL"],
        },
    )

    # 4. e1_evaluation_design
    store.write(
        "e1_evaluation_design",
        {
            "base_scenario_ids": matrix["base_scenario_ids"],
            "offset_strata": _trend_not_applicable()["offset_strata"],
            "optical_transport_seeds": matrix["optical_transport_seeds"],
            "acquisition_seed_matrix": matrix["acquisition_seed_matrix"],
            "matched_realization_hash": matrix["seed_matrix_hash"],
            "common_random_numbers": (
                "Initial and Calibrated evaluate the same base scenarios with the "
                "same seed matrix; the two differ only in the calibrated parameter "
                "values, so the Delta contains no difference between random "
                "realizations"
            ),
            "seed_disjointness": (
                "evaluation seeds start at 30000/40000 and share no value with the "
                "calibration CRN seeds (1001-1042) or the verification seeds "
                "(2001-2042)"
            ),
            "trend": _trend_not_applicable(),
        },
    )

    # 5. e1_scientific_rule
    # 逐鍵取值而不是抓整棵子樹：ResolvedConfig.get() 對未核定的 Required
    # 會 fail-fast，而整棵子樹拿出來之後那個保護就不會再觸發。
    config = load_config([root / "configs" / "base.yaml"])
    rule = {
        key: config.get(f"e1.scientific_rule.{key}")
        for key in (
            "macro_mean_delta_ci_lower_bound_gt",
            "per_feature_class_macro_delta_gte",
            "distance_trend_consistency",
            "bootstrap_replicates",
            "bootstrap_seed",
            "decided_by",
            "decided_on",
        )
    }
    from pcmef.core.amendments import amendment_provenance

    store.write(
        "e1_scientific_rule",
        {
            "normalization_scales": identity["s_f"],
            "aggregation": {
                "macro_mean_delta_ci_lower_bound_gt": float(
                    rule["macro_mean_delta_ci_lower_bound_gt"]
                ),
                "per_feature_class_macro_delta_gte": float(
                    rule["per_feature_class_macro_delta_gte"]
                ),
                "distance_trend_consistency": str(rule["distance_trend_consistency"]),
            },
            "improvement_threshold": float(rule["macro_mean_delta_ci_lower_bound_gt"]),
            "regression_tolerance": float(rule["per_feature_class_macro_delta_gte"]),
            "trend_rule": str(rule["distance_trend_consistency"]),
            "bootstrap_replicates": int(rule["bootstrap_replicates"]),
            "bootstrap_seed": int(rule["bootstrap_seed"]),
            "code_hash": hash_file(root / "pcmef" / "experiments" / "e1_outcome.py"),
            "decided_by": rule["decided_by"],
            "decided_on": str(rule["decided_on"]),
            "frozen_before_heldout_opened": True,
            **amendment_provenance(),
        },
    )

    # 6. claim_boundary
    store.write(
        "claim_boundary",
        {
            "e1_fidelity_scope": (
                "E1 measures the distributional distance between the calibrated "
                "simulator's four ToF features and the FORMAL_E1_FINAL real "
                "recordings, per class and feature. It says nothing about "
                "downstream classification accuracy, nothing about conditions "
                "outside the frozen scene family, and nothing about any sensor "
                "other than the one these recordings came from."
            ),
            "synthetic_rgb_statement": (
                "No RGB or vision modality was calibrated or evaluated here. "
                "Synthetic RGB remains uncalibrated and must not be described as "
                "physics-validated on the strength of this ToF result."
            ),
            "effective_parameter_boundaries": {
                "sensor.fov_deg": (
                    "CG-6: an effective collection-angle parameter of the square "
                    "perspective-film receiver surrogate, not a measured VL53L0X "
                    "system FoV"
                ),
                "medium densities": (
                    "CG-5: effective-medium extinction parameters of the calibrated "
                    "simulator, not absolute turbidity / bubble / mist concentrations"
                ),
            },
            "inhibitory_control": (
                "UPDATE_INHIBITED is a functionally inspired engineering control "
                "rule. It is not evidence for any biological neural inhibition "
                "mechanism and must not be described as such."
            ),
        },
    )

    return {
        name: store.load_hash(name)
        for name in (
            "calibrated_simulation",
            "metric_config",
            "e1_candidates",
            "e1_evaluation_design",
            "e1_scientific_rule",
            "claim_boundary",
        )
    }


def _amendment_hashes(freeze_dir: str | Path) -> dict[str, str]:
    directory = Path(freeze_dir) / "amendments"
    return {
        path.name.split(".")[0]: json.loads(path.read_text(encoding="utf-8"))[
            "payload_hash"
        ]
        for path in sorted(directory.glob("*.amendment.json"))
    }


# ---------------------------------------------------------------------------
# 開啟最終測試並評估
# ---------------------------------------------------------------------------


def load_final_values(
    ids_by_class: dict[str, list[str]],
    source_root: str | Path = "data/raw_real/edge_impulse_export",
) -> dict[str, dict[str, np.ndarray]]:
    """只開啟被交出來的那 112 個檔案。

    刻意接受**明確的 ID 清單**而不是自己去查 registry：這樣「哪些檔案被
    打開過」由 final_ids() 的帳單獨決定，本函式不可能多開一個。
    """
    from pcmef.adapters.edge_impulse import EI_LABEL_TO_CLASS

    root = Path(source_root)
    out: dict[str, dict[str, np.ndarray]] = {}
    for class_label, keys in ids_by_class.items():
        blocks: list[np.ndarray] = []
        for key in sorted(keys):
            label, measurement = key.split("/", 1)
            if EI_LABEL_TO_CLASS.get(label) != class_label:
                raise E1FinalError(
                    "ID_CLASS_MISMATCH",
                    f"{key} was handed out as {class_label} but maps to "
                    f"{EI_LABEL_TO_CLASS.get(label)!r}",
                )
            matches = sorted(
                path
                for split in ("training", "testing")
                for path in (root / split).glob(f"{label}.{measurement}.csv.*.json")
            )
            if len(matches) != 1:
                raise E1FinalError(
                    "SOURCE_FILE_AMBIGUOUS",
                    f"{key} resolved to {len(matches)} files; expected exactly one",
                )
            payload = json.loads(matches[0].read_text(encoding="utf-8"))["payload"]
            values = np.asarray(payload["values"], dtype=np.float64)
            if values.shape != (500, len(TOF_SCHEMA)):
                raise E1FinalError("SHAPE_MISMATCH", f"{key} has shape {values.shape}")
            blocks.append(values)
        stacked = np.concatenate(blocks, axis=0)
        out[class_label] = {
            feature: np.ascontiguousarray(stacked[:, index])
            for index, feature in enumerate(TOF_SCHEMA)
        }
    return out


def run_e1_final(
    calibration_report: str | Path = "outputs/calibration/calibration_report.json",
    out_root: str | Path = "outputs/e1",
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    source_root: str | Path = "data/raw_real/edge_impulse_export",
    code_version: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """凍結 -> 開啟 FORMAL_E1_FINAL 一次 -> 評估 -> 判定。順序不得調換。"""
    from pcmef.core.heldout_partition import final_ids
    from pcmef.core.locks import LockStore
    from pcmef.experiments.calibration_identity import load_frozen_identity
    from pcmef.experiments.calibration_objective import Simulator
    from pcmef.experiments.e1 import E1Engine, FeatureScales
    from pcmef.experiments.e1_outcome import (
        ScientificRule,
        evaluate_outcome,
        freeze_outcome,
    )

    say = progress or (lambda _m: None)
    root = Path(repo_root)
    out = Path(out_root)
    out.mkdir(parents=True, exist_ok=True)

    report = json.loads(Path(calibration_report).read_text(encoding="utf-8"))
    if not report.get("scientific_result", False):
        raise E1FinalError(
            "NON_SCIENTIFIC_INPUT",
            "the calibration report is marked scientific_result=false; a smoke run "
            "must never be the input to the final evaluation",
        )

    identity = load_frozen_identity(freeze_dir, root)
    matrix = scenario_seed_matrix()

    # -- 凍結在前 ----------------------------------------------------------
    say("freezing the calibrated simulator and every gate rule")
    lock_hashes = freeze_pre_heldout_locks(
        report, matrix, freeze_dir, root, code_version
    )
    for name, value in lock_hashes.items():
        say(f"  {name:26s} {value[:16]}")

    store = LockStore(freeze_dir)
    calibrated_lock_hash = store.load_hash("calibrated_simulation")

    # -- 開啟在後：整個研究只做這一次 ---------------------------------------
    say("opening FORMAL_E1_FINAL (once)")
    ids_by_class, ledger_entry = final_ids(
        purpose=_HELDOUT_PURPOSE,
        calibrated_lock_hash=calibrated_lock_hash,
        code_version=code_version,
        freeze_dir=freeze_dir,
        repo_root=root,
    )
    real_values = load_final_values(ids_by_class, source_root)
    opened = sum(len(v) for v in ids_by_class.values())
    say(f"  opened {opened} recordings; heldout_access_count is now 1")

    # -- 兩個候選 ----------------------------------------------------------
    simulator = Simulator(identity=identity)
    simulator.cache.capacity = 8
    initial_parameters = {
        name: float(identity.initial_values[name])
        for name in report["seven_calibrated_parameters"]
    }
    calibrated_parameters = {
        name: float(value)
        for name, value in report["seven_calibrated_parameters"].items()
    }

    say(f"simulating Initial over {len(matrix['base_scenario_ids'])} base scenarios")
    initial = simulate_candidate(
        "initial", initial_parameters, identity, simulator, real_values, matrix, say
    )
    say("simulating Calibrated over the same base scenarios and seed matrix")
    calibrated = simulate_candidate(
        "calibrated", calibrated_parameters, identity, simulator, real_values,
        matrix, say,
    )

    # -- 評估 --------------------------------------------------------------
    rule_payload = store.load("e1_scientific_rule")["payload"]
    scales = FeatureScales(scales=identity.s_f)
    result = E1Engine(store).evaluate(
        real_heldout=real_values,
        initial=initial,
        calibrated=calibrated,
        scales=scales,
        replicates=int(rule_payload["bootstrap_replicates"]),
        seed=int(rule_payload["bootstrap_seed"]),
        heldout_access_count=1,
    )
    decision = evaluate_outcome(result, ScientificRule.from_lock(store))
    outcome_hash = freeze_outcome(store, decision, result)

    document = {
        "report_id": "e1_final_report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "outcome": decision.outcome,
        "conditions": decision.to_artifact(),
        "condition_lines": decision.lines(),
        "e1_outcome_lock_hash": outcome_hash,
        "lock_hashes": {**lock_hashes, "e1_outcome": outcome_hash},
        "identity": identity.to_artifact(),
        "calibration": {
            "overall_outcome": report["overall_outcome"],
            "stage_outcomes": report["calibrated_simulation_lock"]["stage_outcomes"],
            "inhibited_stages": report["inhibitory_control"]["inhibited_stages"],
            "seven_calibrated_parameters": calibrated_parameters,
            "initial_parameters": initial_parameters,
        },
        "evaluation_design": {
            "base_scenarios": len(matrix["base_scenario_ids"]),
            "seed_matrix_hash": matrix["seed_matrix_hash"],
            "seed_disjoint_from_calibration": True,
            "trend": _trend_not_applicable(),
        },
        "heldout": {
            "subset": "FORMAL_E1_FINAL",
            "recordings": opened,
            "recordings_per_class": {c: len(v) for c, v in sorted(ids_by_class.items())},
            "access_entry": ledger_entry,
            "heldout_access_count": 1,
            "opened_after_every_rule_was_frozen": True,
        },
        "result": result.to_artifact(),
        "cells": result.to_rows(),
        "claim_boundary": store.load("claim_boundary")["payload"],
    }
    (out / "e1_final_report.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, default=float),
        encoding="utf-8",
    )
    return document
