# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 audit reproducibility 呼叫；讀兩個 sim smoke 產出目錄的
#         manifest 與 .npy；產出 reproducibility_report.json，
#         其結論為 initial_simulation.lock 的凍結前置之一。
# 檔案路徑: pcmef/audit/reproducibility.py
# 產生時間: 2026-08-28 20:10 +08:00
# 版本: v0.1.0
# 功能說明: 比對兩次獨立執行的 initial simulation —— 身分欄位逐項比對，
#           數值輸出逐檔比對，能 bitwise 相同的必須相同，不能的必須量化差異
#           並與預先訂好的容忍值比較。
# 模組定位: 凍結前的可重現性驗收。它「不是」精度驗收 ——
#           它不問結果對不對，只問「同樣的輸入是否給出同樣的輸出」。
# 主要責任:
#   1. 比對身分欄位（hash / seed / integrator / 版本）
#   2. 逐 artifact 比對 bitwise 相同性
#   3. 不同時量化差異並依 REPRODUCIBILITY_TOLERANCE 判定
# 維護提醒:
#   - 不得以「差不多」結案；非 bitwise 相同時必須寫出 max abs / max rel /
#     相對能量差三個數字，並與容忍值逐項比較。
#   - 不得為了讓某次執行通過而放寬 REPRODUCIBILITY_TOLERANCE；
#     容忍值代表 renderer 的已知非決定性上界，改它要有量測依據。
#   - 不得只比 manifest 而不比 .npy；manifest 相同而張量不同是最危險的情況。
#   - v0.1.0 新增：首版可重現性驗收（NOTE-038）。
# 驗證方式:
#   - py -3.10 -m pcmef.cli audit reproducibility --run-a <dirA> --run-b <dirB>
#   - py -3.10 -m pytest tests/audit/test_reproducibility.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from pcmef.audit.result import AuditReport, CheckResult, CheckStatus

__all__ = [
    "REPRODUCIBILITY_TOLERANCE",
    "IDENTITY_FIELDS",
    "audit_reproducibility",
]

#: 數值輸出的可重現性容忍值。
#:
#: 預設要求 **bitwise 相同**（0.0）。mitsuba/drjit 在固定 seed、固定 spp、
#: 固定 variant 與同一台機器上是決定性的，因此這裡的正確答案是「完全相同」，
#: 不是「很接近」。若日後換到會引入非決定性的後端（例如多執行緒 reduction
#: 順序不固定的 GPU variant），必須先**量測**其上界再據以放寬，
#: 並把量測寫進 NOTE —— 不得因為一次失敗就調大。
REPRODUCIBILITY_TOLERANCE: dict[str, float] = {
    "max_abs_diff": 0.0,
    "max_rel_diff": 0.0,
    "total_energy_rel_diff": 0.0,
}

#: 必須逐項相同的身分欄位。缺欄位視為 FAIL —— 一個沒有記錄 seed 的 run
#: 無法宣稱可重現，即使數值恰好相同。
IDENTITY_FIELDS: tuple[str, ...] = (
    "parameter_registry.parameter_set_hash",
    "parameter_registry.registry_version",
    # NOTE(NOTE-038): 比的是 content_hash 不是 manifest_hash —— 後者涵蓋
    # wall-clock 與輸出路徑，在原理上不可重現。
    "content_hash",
    "run_identity_hash",
    "surrogate_identity.estimator",
    "surrogate_identity.preregistration_hash",
    "surrogate_identity.calibration_hash",
    "dependencies.mitsuba",
    "dependencies.drjit",
    "dependencies.mitransient",
    "dependencies.variant",
    "dependencies.python",
)

#: 每個 scenario 內必須相同的欄位。
SCENARIO_IDENTITY_FIELDS: tuple[str, ...] = (
    "scenario_hash",
    "transient.seed",
    "transient.spp",
    "transient.integrator",
    "transient.illumination",
    # binning 在 manifest 內是巢狀的。先前這三條寫成 transient.temporal_bins
    # 等平鋪路徑，兩邊都解析成 None 而「相等」—— 檢查形式上通過，實際什麼
    # 都沒比到。缺欄位因此一律視為 FAIL，見下方的 None 判定（NOTE-038）。
    "transient.binning.temporal_bins",
    "transient.binning.start_opl_m",
    "transient.binning.bin_width_opl_m",
    "rgb.integrator",
    "rgb.max_depth",
)


def _dig(payload: Any, dotted: str) -> Any:
    node = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _compare_arrays(a: Path, b: Path) -> dict[str, Any]:
    """比對兩個 .npy。bitwise 相同時直接回報，否則量化差異。"""
    raw_a, raw_b = a.read_bytes(), b.read_bytes()
    if raw_a == raw_b:
        return {"bitwise_identical": True, "max_abs_diff": 0.0, "max_rel_diff": 0.0,
                "total_energy_rel_diff": 0.0}

    array_a = np.load(a).astype(np.float64)
    array_b = np.load(b).astype(np.float64)
    if array_a.shape != array_b.shape:
        return {
            "bitwise_identical": False,
            "shape_mismatch": [list(array_a.shape), list(array_b.shape)],
        }
    diff = np.abs(array_a - array_b)
    denominator = np.maximum(np.abs(array_a), np.abs(array_b))
    with np.errstate(divide="ignore", invalid="ignore"):
        relative = np.where(denominator > 0, diff / denominator, 0.0)
    sum_a, sum_b = float(array_a.sum()), float(array_b.sum())
    energy_rel = (
        abs(sum_a - sum_b) / max(abs(sum_a), abs(sum_b))
        if max(abs(sum_a), abs(sum_b)) > 0
        else 0.0
    )
    return {
        "bitwise_identical": False,
        "max_abs_diff": float(diff.max()),
        "max_rel_diff": float(relative.max()),
        "total_energy_rel_diff": energy_rel,
        "differing_elements": int((diff > 0).sum()),
        "total_elements": int(diff.size),
    }


def audit_reproducibility(run_a: str | Path, run_b: str | Path) -> AuditReport:
    """比對兩次獨立執行。回傳 RP-01..RP-04。"""
    dir_a, dir_b = Path(run_a), Path(run_b)
    results: list[CheckResult] = []

    manifest_a = dir_a / "simulation_smoke_manifest.json"
    manifest_b = dir_b / "simulation_smoke_manifest.json"
    if not (manifest_a.exists() and manifest_b.exists()):
        missing = [str(p) for p in (manifest_a, manifest_b) if not p.exists()]
        return AuditReport(
            name="reproducibility",
            results=(
                CheckResult(
                    identifier="RP-01",
                    requirement="both runs produced a manifest",
                    status=CheckStatus.NOT_PRODUCED,
                    detail=f"missing manifest(s): {missing}",
                ),
            ),
        )

    payload_a = json.loads(manifest_a.read_text(encoding="utf-8"))
    payload_b = json.loads(manifest_b.read_text(encoding="utf-8"))

    # -- RP-01 run 身分欄位 -------------------------------------------------
    mismatches: list[str] = []
    identity: dict[str, Any] = {}
    for field in IDENTITY_FIELDS:
        value_a, value_b = _dig(payload_a, field), _dig(payload_b, field)
        identity[field] = value_a
        if value_a is None or value_b is None:
            mismatches.append(f"{field}: missing (a={value_a!r}, b={value_b!r})")
        elif value_a != value_b:
            mismatches.append(f"{field}: {value_a!r} != {value_b!r}")
    results.append(
        CheckResult(
            identifier="RP-01",
            requirement="run identity fields are identical across the two runs",
            status=CheckStatus.PASS if not mismatches else CheckStatus.FAIL,
            detail=(
                f"{len(IDENTITY_FIELDS)} identity field(s) compared, all identical; "
                f"parameter_set_hash={identity.get('parameter_registry.parameter_set_hash')}"
                if not mismatches
                else "; ".join(mismatches)
            ),
            findings=tuple(mismatches),
        )
    )

    # -- RP-02 scenario 層身分 ---------------------------------------------
    scenarios_a = {s["scenario_id"]: s for s in payload_a.get("scenarios", [])}
    scenarios_b = {s["scenario_id"]: s for s in payload_b.get("scenarios", [])}
    scenario_issues: list[str] = []
    if set(scenarios_a) != set(scenarios_b):
        scenario_issues.append(
            f"scenario sets differ: {sorted(set(scenarios_a) ^ set(scenarios_b))}"
        )
    for name in sorted(set(scenarios_a) & set(scenarios_b)):
        for field in SCENARIO_IDENTITY_FIELDS:
            value_a = _dig(scenarios_a[name], field)
            value_b = _dig(scenarios_b[name], field)
            # 兩邊都是 None 代表欄位路徑寫錯或 manifest 少了它 —— 那不是
            # 「相等」，那是沒比到。缺欄位一律 FAIL，否則檢查會空過。
            if value_a is None or value_b is None:
                scenario_issues.append(
                    f"{name}.{field}: missing (a={value_a!r}, b={value_b!r})"
                )
            elif value_a != value_b:
                scenario_issues.append(f"{name}.{field}: {value_a!r} != {value_b!r}")
    results.append(
        CheckResult(
            identifier="RP-02",
            requirement="per-scenario seeds, integrator and binning are identical",
            status=CheckStatus.PASS if not scenario_issues else CheckStatus.FAIL,
            detail=(
                f"{len(scenarios_a)} scenario(s) x {len(SCENARIO_IDENTITY_FIELDS)} "
                "field(s) compared, all identical"
                if not scenario_issues
                else "; ".join(scenario_issues[:6])
            ),
            findings=tuple(scenario_issues),
        )
    )

    # -- RP-03 數值輸出 -----------------------------------------------------
    comparisons: dict[str, Any] = {}
    numeric_issues: list[str] = []
    for name in sorted(set(scenarios_a) & set(scenarios_b)):
        for stem in ("transient.npy", "transient_ambient.npy", "transient_time.npy"):
            path_a, path_b = dir_a / name / stem, dir_b / name / stem
            if not (path_a.exists() and path_b.exists()):
                numeric_issues.append(f"{name}/{stem}: missing in one of the runs")
                continue
            outcome = _compare_arrays(path_a, path_b)
            comparisons[f"{name}/{stem}"] = outcome
            if outcome["bitwise_identical"]:
                continue
            if "shape_mismatch" in outcome:
                numeric_issues.append(f"{name}/{stem}: shape mismatch")
                continue
            for metric, tolerance in REPRODUCIBILITY_TOLERANCE.items():
                if outcome[metric] > tolerance:
                    numeric_issues.append(
                        f"{name}/{stem}: {metric}={outcome[metric]:.3e} "
                        f"exceeds tolerance {tolerance:.3e}"
                    )
    identical = sum(1 for v in comparisons.values() if v.get("bitwise_identical"))
    results.append(
        CheckResult(
            identifier="RP-03",
            requirement="numerical outputs reproduce within the declared tolerance",
            status=CheckStatus.PASS if not numeric_issues else CheckStatus.FAIL,
            detail=(
                f"{identical}/{len(comparisons)} artifact(s) bitwise identical; "
                f"tolerance {REPRODUCIBILITY_TOLERANCE}"
            ),
            findings=tuple(numeric_issues),
        )
    )

    # -- RP-04 surrogate / estimator 身分 ----------------------------------
    # 這兩者不在 simulation manifest 內，取自程式與選定 artifact，
    # 因為它們同樣決定 initial simulation 的身分。
    from pcmef.surrogate.calibration import PLACEHOLDER_SMOKE_CALIBRATION
    from pcmef.surrogate.distance import SELECTED_ESTIMATOR
    from pcmef.surrogate.estimator_selection import preregistration_hash

    selection_path = Path("outputs/estimator_select/estimator_selection.json")
    selection_issues: list[str] = []
    selection: dict[str, Any] = {}
    if not selection_path.exists():
        selection_issues.append(f"no estimator selection artifact at {selection_path}")
    else:
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        if selection.get("outcome") != "SELECTED":
            selection_issues.append(
                f"estimator selection outcome is {selection.get('outcome')}"
            )
        if selection.get("selected") != SELECTED_ESTIMATOR.name:
            selection_issues.append(
                f"code uses {SELECTED_ESTIMATOR.name} but the selection artifact "
                f"says {selection.get('selected')}"
            )
        if selection.get("preregistration_hash") != preregistration_hash():
            selection_issues.append(
                "the selection artifact was produced under a different "
                "preregistration than the one currently on disk"
            )
    results.append(
        CheckResult(
            identifier="RP-04",
            requirement="surrogate and estimator identity are pinned and consistent",
            status=CheckStatus.PASS if not selection_issues else CheckStatus.FAIL,
            detail=(
                f"estimator={SELECTED_ESTIMATOR.name} "
                f"preregistration={preregistration_hash()[:16]} "
                f"calibration_hash={PLACEHOLDER_SMOKE_CALIBRATION.calibration_hash()[:16]}"
                if not selection_issues
                else "; ".join(selection_issues)
            ),
            findings=tuple(selection_issues),
        )
    )

    return AuditReport(name="reproducibility", results=tuple(results))
