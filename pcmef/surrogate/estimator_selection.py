# PC-MEF Research System source maintenance contract
# 上下游: 讀 configs/estimator_preregistration.yaml；由 cli 的
#         surrogate estimator-select 在子行程呼叫（NOTE-012）；
#         產出 estimator_selection.json，其 preregistration_hash 進
#         initial_simulation.lock。
# 檔案路徑: pcmef/surrogate/estimator_selection.py
# 產生時間: 2026-08-28 15:40 +08:00
# 版本: v0.1.0
# 功能說明: 依**預先凍結**的判準逐階段淘汰候選 distance estimator，
#           並把每個候選在每一條判準上的實測數字完整留下。
# 模組定位: 預註冊判準的執行器。它「不是」判準的來源 ——
#           判準在 yaml 裡，本模組只負責照著跑，跑完不得回頭改判準。
# 主要責任:
#   1. 依 stage 1 (physics) -> stage 2 (synthetic sanity) 逐階段篩選
#   2. 每個候選逐條記錄量到的數字，PASS 與 FAIL 都留
#   3. 依 decision_rule 判定，含 NO_ESTIMATOR_SELECTED 這個合法結局
# 維護提醒:
#   - 不得在此新增判準或候選；兩者都只能來自 preregistration yaml。
#   - 不得因為「一個都沒過」就放寬門檻重跑；那是 amendment 的事（NOTE-035）。
#   - 不得讓任何 real class mean 進入本模組；stage 1/2 完全不碰真實資料。
#   - stage 3 只在候選多於一個時才執行；已定案的選擇不該再去碰真實資料。
#   - v0.1.0 新增：首版選定執行器（NOTE-035）。
# 驗證方式:
#   - py -3.10 -m pcmef.cli surrogate estimator-select --out outputs/estimator_select
#   - py -3.10 -m pytest tests/surrogate/test_ambient_and_estimator.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "PREREGISTRATION_PATH",
    "load_preregistration",
    "preregistration_hash",
    "run_estimator_selection",
]

PREREGISTRATION_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "estimator_preregistration.yaml"
)


def load_preregistration(path: str | Path | None = None) -> dict[str, Any]:
    import yaml

    target = Path(path) if path is not None else PREREGISTRATION_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"estimator preregistration not found at {target}. The candidate set and "
            "the decision rule must be frozen before any comparison is run; without "
            "the file there is nothing distinguishing a selection from a post-hoc "
            "rationalisation (NOTE-035)."
        )
    return yaml.safe_load(target.read_text(encoding="utf-8"))


def preregistration_hash(path: str | Path | None = None) -> str:
    from pcmef.core.hash import hash_object

    return hash_object(load_preregistration(path))


def run_estimator_selection(
    temporal_bins: int = 128,
    resolution: tuple[int, int] = (32, 32),
    spp: int = 16,
    preregistration: str | Path | None = None,
) -> dict[str, Any]:
    """依預註冊判準跑 stage 1+2。必須在已 set_variant 的行程內呼叫。"""
    import mitsuba as mi
    import mitransient  # noqa: F401

    from pcmef.surrogate.calibration import PLACEHOLDER_SMOKE_CALIBRATION
    from pcmef.surrogate.distance import DistanceEstimator, map_distance
    from pcmef.surrogate.features import extract_observables
    from pcmef.simulation.mitransient_adapter import (
        MiTransientAdapter,
        build_transient_scene_dict,
    )
    from pcmef.simulation.mitsuba_adapter import Illumination
    from pcmef.simulation.scenario import Geometry, Lighting, ScenarioConfig

    prereg = load_preregistration(preregistration)
    params = {p["name"]: p["default"] for p in prereg["tunable_parameters"]}
    threshold_sigma = float(params["detection_threshold_sigma"])
    min_return_bins = int(params["min_return_bins"])

    adapter = MiTransientAdapter()
    calibration = PLACEHOLDER_SMOKE_CALIBRATION

    MEDIA = {
        "Empty": ({}, 1001),
        "Water-filled": ({"turbidity": {"value": 0.05, "placeholder": True}}, 1002),
        "Bubbly": ({"bubble_density": {"value": 0.30, "placeholder": True}}, 1042),
        "Misty": ({"mist_density": {"value": 0.15, "placeholder": True}}, 1004),
    }

    def make_config(class_label, sensor_mm=50.0, irradiance=1.0):
        medium, seed = MEDIA[class_label]
        return ScenarioConfig(
            class_label=class_label,
            seed=seed,
            geometry=Geometry(sensor_to_bottle_mm=sensor_mm),
            lighting=Lighting(preset="nominal", irradiance=irradiance),
            medium_parameters=medium,
            spp=spp,
            resolution=resolution,
        )

    def observe(config):
        """算 active + ambient 兩個 pass，回傳 observables 與時間窗界限。"""
        start, width = adapter.default_binning(config, temporal_bins)
        cubes = {}
        for name, illum in (
            ("active", Illumination.ACTIVE_ONLY),
            ("ambient", Illumination.AMBIENT_ONLY),
        ):
            scene_dict = build_transient_scene_dict(
                mi, config, temporal_bins, start, width, illumination=illum
            )
            scene = mi.load_dict(scene_dict)
            mi.render(scene, spp=int(config.spp), seed=int(config.seed))
            cubes[name] = np.array(scene.sensors()[0].film().develop_transient_())
        centres = start + (np.arange(temporal_bins) + 0.5) * width
        axis = centres / 299792458.0
        obs = extract_observables(cubes["active"], axis, 8, ambient_transient=cubes["ambient"])
        bounds_mm = (start * 0.5 * 1000.0, (start + width * temporal_bins) * 0.5 * 1000.0)
        return obs, bounds_mm

    def estimate(obs, estimator):
        # rng 固定：本階段量的是 estimator 的性質，不是雜訊。
        return map_distance(
            obs,
            calibration,
            np.random.default_rng(0),
            estimator,
            threshold_sigma=threshold_sigma,
            min_return_bins=min_return_bins,
        )

    # 每個候選都用同一批算圖結果，避免「不同候選看到不同場景」。
    baseline: dict[str, Any] = {}
    for label in MEDIA:
        baseline[label] = observe(make_config(label))
    shifted_obs, _ = observe(make_config("Empty", sensor_mm=60.0))
    bright_obs, _ = observe(make_config("Empty", irradiance=4.0))

    results: list[dict[str, Any]] = []
    for candidate in prereg["candidates"]:
        estimator = DistanceEstimator(candidate["id"].lower())
        entry: dict[str, Any] = {
            "id": candidate["id"],
            "tunable_parameters": candidate["tunable_parameters"],
            "checks": {},
            "measurements": {},
        }
        failures: list[str] = []

        # -- stage 1: physics validity ------------------------------------
        try:
            obs, bounds = baseline["Empty"]
            value = estimate(obs, estimator)
            twice = estimate(obs, estimator)
            p1 = np.isfinite(value) and value > 0
            p2 = bounds[0] <= value <= bounds[1]
            p3 = value == twice
            entry["measurements"]["empty_mm"] = value
            entry["measurements"]["scene_bounds_mm"] = list(bounds)
            entry["checks"]["P1_finite_positive"] = bool(p1)
            entry["checks"]["P2_within_scene_bounds"] = bool(p2)
            entry["checks"]["P3_deterministic"] = bool(p3)
            # P4 是結構性質：map_distance 的簽章不接受 class label。
            entry["checks"]["P4_no_class_label_in_signature"] = True
            for name, ok in (
                ("P1", p1), ("P2", p2), ("P3", p3),
            ):
                if not ok:
                    failures.append(name)
        except Exception as error:  # noqa: BLE001
            entry["checks"]["P1_finite_positive"] = False
            entry["measurements"]["error"] = f"{type(error).__name__}: {error}"
            failures.append("P1")

        # -- stage 2: synthetic sanity ------------------------------------
        if not failures:
            base_mm = entry["measurements"]["empty_mm"]
            try:
                shifted_mm = estimate(shifted_obs, estimator)
                delta = shifted_mm - base_mm
                s1 = 5.0 <= delta <= 15.0
                entry["measurements"]["sensor_60mm_mm"] = shifted_mm
                entry["measurements"]["S1_delta_mm"] = delta
                entry["checks"]["S1_geometric_monotonicity"] = bool(s1)
                if not s1:
                    failures.append("S1")
            except Exception as error:  # noqa: BLE001
                entry["checks"]["S1_geometric_monotonicity"] = False
                entry["measurements"]["S1_error"] = f"{type(error).__name__}: {error}"
                failures.append("S1")

            try:
                bright_mm = estimate(bright_obs, estimator)
                rel = abs(bright_mm - base_mm) / base_mm if base_mm else float("inf")
                s2 = rel < 0.01
                entry["measurements"]["irradiance_4x_mm"] = bright_mm
                entry["measurements"]["S2_relative_change"] = rel
                entry["checks"]["S2_gain_invariance"] = bool(s2)
                if not s2:
                    failures.append("S2")
            except Exception as error:  # noqa: BLE001
                entry["checks"]["S2_gain_invariance"] = False
                entry["measurements"]["S2_error"] = f"{type(error).__name__}: {error}"
                failures.append("S2")

            per_class: dict[str, Any] = {}
            s3 = True
            for label in MEDIA:
                try:
                    per_class[label] = estimate(baseline[label][0], estimator)
                except Exception as error:  # noqa: BLE001
                    per_class[label] = f"{type(error).__name__}: {error}"
                    s3 = False
            entry["measurements"]["per_class_mm"] = per_class
            entry["checks"]["S3_all_classes_finite"] = bool(s3)
            if not s3:
                failures.append("S3")

        entry["failed_checks"] = failures
        entry["survives"] = not failures
        results.append(entry)

    survivors = [r["id"] for r in results if r["survives"]]
    if len(survivors) == 1:
        decision = survivors[0]
        outcome = "SELECTED"
        reason = (
            f"exactly one candidate survived stages 1-2; per the preregistered "
            f"decision rule stage 3 (offset anchors) is NOT run, so no real data "
            f"was consulted at any point"
        )
    elif not survivors:
        decision = None
        outcome = "NO_ESTIMATOR_SELECTED"
        reason = (
            "no candidate passed the hard gates. The preregistered rule forbids "
            "loosening the gates and re-running; this requires an amendment."
        )
    else:
        decision = None
        outcome = "TIE_BREAK_REQUIRED"
        reason = (
            f"{len(survivors)} candidates survived ({survivors}); the preregistered "
            "rule calls for stage 3 (independent offset anchors)."
        )

    return {
        "audit": "distance_estimator_selection",
        "note": "NOTE-035",
        "preregistration_hash": preregistration_hash(preregistration),
        "preregistration_version": prereg["preregistration_version"],
        "tunable_parameters_used": params,
        "settings": {
            "temporal_bins": temporal_bins,
            "resolution": list(resolution),
            "spp": spp,
        },
        "candidates": results,
        "survivors": survivors,
        "outcome": outcome,
        "selected": decision,
        "reason": reason,
        "real_data_consulted": False,
        "claim_boundary": (
            "Selection used only physics validity and synthetic sanity. No real "
            "class mean and no held-out data entered this procedure. Passing does "
            "not mean the estimator's values are close to the real sensor's."
        ),
    }
