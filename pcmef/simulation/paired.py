# PC-MEF Research System source maintenance contract
# 上下游: 讀 freeze/calibrated_simulation.lock.json 與 freeze/initial_simulation.lock.json
#         的凍結參數，經 mitsuba_adapter / mitransient_adapter 算圖；
#         由 cli 的 `sim paired-smoke` 呼叫；寫出 outputs/paired/<run>/ 底下的
#         每 sample RGB + ToF 與 paired_manifest.json。
#         **不讀任何真實資料**，也不碰 FORMAL_E1_FINAL。
# 檔案路徑: pcmef/simulation/paired.py
# 產生時間: 2026-08-31 02:10 +08:00
# 版本: v0.1.0
# 功能說明: 以 E1 最終保留狀態的模擬器，為同一個 scenario **同時**產生
#           RGB 與 500x4 ToF，並記錄足以逐位元重建該 sample 的全部身分。
# 模組定位: E1 -> E2 的成對資料橋。它不訓練模型、不做方法選擇、不碰 gate；
#           它只保證「這一張 RGB 與這一筆 ToF 來自同一個場景」這件事
#           是**結構上成立**而不是事後按 class 配對出來的。
# 主要責任:
#   1. load_calibrated_simulator() 由凍結 lock 重建 E1 最終模擬器身分
#   2. SimulatorIdentity.identity_hash() 涵蓋校準值、場景常數與程式碼身分
#   3. generate_paired_sample() 一個 ScenarioConfig 同時產出 RGB 與 ToF
#   4. run_paired_smoke() 產生四類的小型 smoke set 與 manifest
#   5. verify_manifest() 執行成對身分、形狀、欄序、有限性與判別性檢查
# 維護提醒:
#   - 不得分別產生 RGB 與 ToF 之後再依 class 配對。成對性必須來自
#     「同一個 ScenarioConfig、同一個 scene_hash」，那是可驗證的；
#     事後配對只是一個沒有人能反證的宣稱。
#   - 不得重新擬合任何參數。ambient 兩項用 E1 接受的校準值，
#     其餘五項一律用 UPDATE_INHIBITED 之後保留的凍結初值。
#   - 不得改動 spp / resolution / temporal_bins；它們是凍結的 not_fitted 項，
#     改了就不是這個被 E1 評估過的模擬器。
#   - 不得使用校準 CRN（1001-1042）、verification（2001-2042）或 E1 評估
#     （30000/40000 起）用過的種子；成對資料是新的抽樣，不是那些的重播。
#   - v0.1.0 新增：首版 E1->E2 成對資料橋。
# 驗證方式:
#   - py -3.10 -m pytest tests/simulation/test_paired.py -v
#   - py -3.10 -m pcmef.cli sim paired-smoke --per-class 1
# ------------------------------------------------------------

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pcmef.core.constants import CLASS_ORDER, TOF_RECORDING_POINTS, TOF_SCHEMA
from pcmef.core.hash import hash_array, hash_file, hash_object

__all__ = [
    "PAIRED_SEED_BASE",
    "PairedGenerationError",
    "SimulatorIdentity",
    "PairedSample",
    "load_calibrated_simulator",
    "generate_paired_sample",
    "run_paired_smoke",
    "verify_manifest",
]

#: 成對資料的種子基底。刻意與已經用過的三組**不相交**：
#: 校準 CRN 1001-1042、verification 2001-2042、E1 評估 30000/40000 起。
#: 重用那些種子會讓成對資料變成既有實現的重播，而不是新的抽樣。
PAIRED_SEED_BASE = 50000

#: 每一項的來源。E1 之後只有 ambient 兩項真的被更新；其餘一律保留。
_PROVENANCE = {
    "ambient_energy_to_mcps": "E1 calibration (MAPPING_AMBIENT CONVERGED)",
    "ambient_jitter_relative": "E1 calibration (MAPPING_AMBIENT CONVERGED)",
    "signal_energy_to_mcps": "retained frozen initial (MAPPING_SIGNAL UPDATE_INHIBITED)",
    "noise_relative_sigma": "retained frozen initial (MAPPING_SIGNAL UPDATE_INHIBITED)",
    "sigma_width_to_mm": "retained frozen initial (MAPPING_SIGMA UPDATE_INHIBITED)",
    "sigma_multipath_weight": "retained frozen initial (MAPPING_SIGMA UPDATE_INHIBITED)",
    "sigma_snr_weight": "gauge fixed at frozen initial (stage 0 borderline, S0-B1..B5)",
    "distance_offset_mm": "gauge fixed at frozen initial (CG-4)",
}

#: 進場景的校準維度。E1 之後只有 sensor.fov_deg 在這一側，且它被抑制，
#: 因此其值等於凍結初值 —— 仍然明確套用，讓 artifact 記得住它是誰。
_SCENE_DIMENSIONS = ("sensor.fov_deg",)

#: 物理變異只用**已有預註冊依據**的 nuisance 變數，不自行發明範圍。
#:
#:   lateral_offset_mm  estimator_preregistration S4 明列「橫向偏移 ±5 mm
#:                      （純幾何擾動）」，並附物理推導的容忍度；
#:                      parameter_registry 亦記其為「偏移量測試的自變數」，
#:                      provenance_status = CONFIRMED。
#:   irradiance         estimator_preregistration S3 明列 1.0 -> 4.0 的
#:                      不變性探針（純全域增益，CG-2）。
#:
#: 兩者都**不改變 class 身分**：瓶子擺得偏一點、室內亮一點，內容物不變。
#: 沒有合法範圍的量（例如 sensor_to_bottle_mm）一律不動。
NUISANCE_RANGES: dict[str, tuple[float, float]] = {
    "geometry.lateral_offset_mm": (-5.0, 5.0),
    "lighting.irradiance": (1.0, 4.0),
}

NUISANCE_PROVENANCE: dict[str, str] = {
    "geometry.lateral_offset_mm": (
        "estimator_preregistration S4: lateral offset +/-5 mm, a pure geometric "
        "perturbation with a physics-derived tolerance"
    ),
    "lighting.irradiance": (
        "estimator_preregistration S3: irradiance 1.0 -> 4.0 invariance probe "
        "(pure global gain, CG-2)"
    ),
}


@dataclass(frozen=True)
class PhysicalVariation:
    """一個 physical family 的 nuisance 取值。**不含 seed。**

    同一個 PhysicalVariation 配上不同的 seed，就是同一個物理場景的多次
    Monte-Carlo 實現；那正是 family split 要擋在同一側的東西。
    """

    lateral_offset_mm: float
    irradiance: float

    def to_dict(self) -> dict[str, float]:
        return {
            "geometry.lateral_offset_mm": float(self.lateral_offset_mm),
            "lighting.irradiance": float(self.irradiance),
        }


def family_variation(class_label: str, family_index: int, n_families: int) -> PhysicalVariation:
    """第 k 個 physical family 的 nuisance 取值。

    以**決定性**的分層網格取值而不是亂數抽樣：family 的身分因此只由
    (class, family_index, n_families) 決定，重建資料集會得到同一組場景。
    每個 class 用不同的相位偏移，避免四類的 family 在 nuisance 空間上重疊
    成同一組點 —— 那會讓「不同 class 的同號 family」意外變成同一個場景。
    """
    lo_off, hi_off = NUISANCE_RANGES["geometry.lateral_offset_mm"]
    lo_irr, hi_irr = NUISANCE_RANGES["lighting.irradiance"]
    phase = CLASS_ORDER.index(class_label) / max(len(CLASS_ORDER), 1)

    # 兩個軸用不同步長掃過網格，讓 20 個 family 覆蓋整個矩形而不是一條線。
    #
    # 分母是 n_families 而不是 n_families - 1：加了 class 相位之後
    # `(family_index + phase)` 的最大值是 n_families - 1 + phase，除以
    # n_families - 1 會得到 > 1，於是取值**跑出預註冊範圍**（實測 irradiance
    # 到 4.039，而 S3 的上界是 4.0）。除以 n_families 讓 u, v 落在 [0, 1)，
    # 取值因此嚴格落在宣告區間內。超出預註冊範圍的場景就是沒有依據的場景。
    u = ((family_index + phase) % n_families) / n_families
    v = (((family_index * 7) + phase) % n_families) / n_families
    return PhysicalVariation(
        lateral_offset_mm=lo_off + u * (hi_off - lo_off),
        irradiance=lo_irr + v * (hi_irr - lo_irr),
    )


_MEDIUM_KEY_BY_CLASS: dict[str, str | None] = {
    "Empty": None,
    "Water-filled": "turbidity",
    "Bubbly": "bubble_density",
    "Misty": "mist_density",
}


class PairedGenerationError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


# ---------------------------------------------------------------------------
# 模擬器身分
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimulatorIdentity:
    """E1 最終保留狀態的完整身分。"""

    calibrated_lock_hash: str
    initial_lock_hash: str
    parameter_values: dict[str, Any]
    fitted_parameters: dict[str, float]
    stage_outcomes: dict[str, str]
    inhibited_stages: list[str]
    scene_constants: dict[str, float]
    surrogate_hash: str
    code_hashes: dict[str, str]
    simulation_settings: dict[str, Any]

    def identity_hash(self) -> str:
        return hash_object(
            {
                "calibrated_lock_hash": self.calibrated_lock_hash,
                "initial_lock_hash": self.initial_lock_hash,
                "fitted_parameters": {
                    k: float(v).hex() for k, v in sorted(self.fitted_parameters.items())
                },
                "scene_constants": {
                    k: float(v).hex() for k, v in sorted(self.scene_constants.items())
                },
                "surrogate_hash": self.surrogate_hash,
                "code_hashes": self.code_hashes,
                "simulation_settings": self.simulation_settings,
            }
        )

    def to_artifact(self) -> dict[str, Any]:
        return {
            "identity_hash": self.identity_hash(),
            "calibrated_simulation_lock_hash": self.calibrated_lock_hash,
            "initial_simulation_lock_hash": self.initial_lock_hash,
            "fitted_parameters": dict(self.fitted_parameters),
            "parameter_provenance": dict(_PROVENANCE),
            "stage_outcomes": dict(self.stage_outcomes),
            "inhibited_stages": list(self.inhibited_stages),
            "scene_constants": dict(self.scene_constants),
            "surrogate_hash": self.surrogate_hash,
            "code_hashes": dict(self.code_hashes),
            "simulation_settings": dict(self.simulation_settings),
            "claim_boundary": (
                "This is the simulator state E1 evaluated: the two ambient mapping "
                "parameters carry E1-accepted calibrated values; sensor.fov_deg, the "
                "signal mapping and the sigma mapping carry their retained frozen "
                "initial values because their updates were inhibited. Nothing here "
                "was re-fitted."
            ),
        }


def load_calibrated_simulator(
    freeze_dir: str | Path = "freeze", repo_root: str | Path = "."
) -> tuple[SimulatorIdentity, Any]:
    """由凍結 lock 重建 E1 最終模擬器。回傳 (identity, SurrogateCalibration)。

    每一個 scale 都明確標上來源並設成非 placeholder：它們不是暫用值，
    是「E1 接受的校準值」或「更新被抑制後保留的凍結初值」，兩者都是
    有據可查的終局。留成 placeholder 會讓 assert_formal_ready() 誤判，
    也會讓 artifact 看不出它們的身分。
    """
    from pcmef.surrogate.calibration import (
        PLACEHOLDER_SMOKE_CALIBRATION,
        CalibratedScale,
    )

    root = Path(repo_root)
    fdir = Path(freeze_dir)

    lock_path = fdir / "calibrated_simulation.lock.json"
    if not lock_path.exists():
        raise PairedGenerationError(
            "CALIBRATION_NOT_FROZEN",
            f"{lock_path} does not exist; paired generation must use the frozen "
            "calibrated simulator, never an ad-hoc parameter set",
        )
    document = json.loads(lock_path.read_text(encoding="utf-8"))
    payload = document["payload"]
    if hash_object(payload) != document["payload_hash"]:
        raise PairedGenerationError(
            "CALIBRATION_LOCK_DRIFT",
            "calibrated_simulation.lock payload hash does not verify",
        )

    values = payload["calibrated_parameter_values"]
    fitted = {k: float(v) for k, v in payload["fitted_parameters"].items()}

    # 場景側：sensor.fov_deg（抑制後等於凍結初值，仍明確套用）。
    scene_constants = {name: float(values[name]) for name in _SCENE_DIMENSIONS}

    # surrogate 側：八個非 derived scale，逐項標來源。
    from dataclasses import replace

    updates = {
        name: CalibratedScale(
            value=float(values[name]), placeholder=False, source=source
        )
        for name, source in _PROVENANCE.items()
    }
    calibration = replace(PLACEHOLDER_SMOKE_CALIBRATION, **updates)
    pending = calibration.placeholder_names()
    if pending:
        raise PairedGenerationError(
            "PLACEHOLDER_REMAINS",
            f"the calibrated surrogate still carries placeholder scale(s) {pending}; "
            "every scale must name its provenance before paired data is generated",
        )

    settings = {
        "spp": int(values["spp"]),
        "resolution": [int(v) for v in values["resolution"]],
        "temporal_bins": int(values["temporal_bins"]),
        "max_depth": int(values["max_depth"]),
        "n_samples_per_recording": int(TOF_RECORDING_POINTS),
        "sample_interval_s": float(values["sample_interval_s"]),
        "sample_interval_source": "initial_simulation.lock:initial_parameter_values",
        "feature_order": list(TOF_SCHEMA),
    }
    code_hashes = {
        "mitsuba_adapter": hash_file(root / "pcmef" / "simulation" / "mitsuba_adapter.py"),
        "mitransient_adapter": hash_file(
            root / "pcmef" / "simulation" / "mitransient_adapter.py"
        ),
        "single_acquisition": hash_file(
            root / "pcmef" / "surrogate" / "single_acquisition.py"
        ),
        "temporal_model": hash_file(root / "pcmef" / "surrogate" / "temporal_model.py"),
        "paired": hash_file(root / "pcmef" / "simulation" / "paired.py"),
    }

    identity = SimulatorIdentity(
        calibrated_lock_hash=document["payload_hash"],
        initial_lock_hash=payload["calibration_source_hashes"]["initial_simulation"],
        parameter_values=values,
        fitted_parameters=fitted,
        stage_outcomes=payload["stage_outcomes"],
        inhibited_stages=payload["inhibited_stages"],
        scene_constants=scene_constants,
        surrogate_hash=calibration.calibration_hash(),
        code_hashes=code_hashes,
        simulation_settings=settings,
    )
    return identity, calibration


# ---------------------------------------------------------------------------
# 一個成對 sample
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedSample:
    """一個 scenario 同時產出的 RGB 與 ToF，外加足以重建它的全部身分。"""

    scenario_id: str
    class_label: str
    class_index: int
    scenario_hash: str
    seed_family: dict[str, int]
    rgb_exr_path: str
    rgb_exr_sha256: str
    rgb_png_path: str
    rgb_png_sha256: str
    tof_path: str
    tof_sha256: str
    tof_array_hash: str
    tof_shape: list[int]
    feature_order: list[str]
    scene_parameters: dict[str, Any]
    simulator_identity_hash: str
    calibrated_simulation_lock_hash: str
    runtime_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "class_label": self.class_label,
            "class_index": self.class_index,
            "scenario_hash": self.scenario_hash,
            "seed_family": dict(self.seed_family),
            "rgb": {
                "exr_path": self.rgb_exr_path,
                "exr_sha256": self.rgb_exr_sha256,
                "png_path": self.rgb_png_path,
                "png_sha256": self.rgb_png_sha256,
            },
            "tof": {
                "path": self.tof_path,
                "sha256": self.tof_sha256,
                "array_hash": self.tof_array_hash,
                "shape": list(self.tof_shape),
                "feature_order": list(self.feature_order),
            },
            "scene_parameters": self.scene_parameters,
            "simulator_identity_hash": self.simulator_identity_hash,
            "calibrated_simulation_lock_hash": self.calibrated_simulation_lock_hash,
            "runtime_s": self.runtime_s,
        }


def _scenario_config(
    identity: SimulatorIdentity,
    class_label: str,
    seed: int,
    variation: PhysicalVariation | None = None,
):
    """一個 scenario 的完整設定。RGB 與 ToF **共用這一份**。

    `variation` 為 None 時使用凍結初值，也就是 E1 評估過的那個場景；
    給定時只覆寫兩個有預註冊依據的 nuisance 量，其餘一律不動。
    """
    from pcmef.simulation.scenario import Geometry, Lighting, ScenarioConfig

    values = identity.parameter_values
    key = _MEDIUM_KEY_BY_CLASS[class_label]
    # 介質密度是 SCENE_PARTICIPATING_MEDIA 的 NO_FREE_PARAMETERS 結局，
    # 也就是凍結初值 —— 不是 placeholder，因此以純量寫入。
    medium = {} if key is None else {key: float(values[f"medium.{key}"])}
    lateral = float(values["geometry.lateral_offset_mm"])
    irradiance = float(values["lighting.irradiance"])
    if variation is not None:
        lateral = float(variation.lateral_offset_mm)
        irradiance = float(variation.irradiance)
    return ScenarioConfig(
        class_label=class_label,
        seed=int(seed),
        geometry=Geometry(
            sensor_to_bottle_mm=float(values["geometry.sensor_to_bottle_mm"]),
            bottle_diameter_mm=float(values["geometry.bottle_diameter_mm"]),
            wall_thickness_mm=float(values["geometry.wall_thickness_mm"]),
            lateral_offset_mm=lateral,
        ),
        lighting=Lighting(preset="nominal", irradiance=irradiance),
        medium_parameters=medium,
        render_rgb=True,
        render_transient=True,
        spp=int(identity.simulation_settings["spp"]),
        resolution=tuple(identity.simulation_settings["resolution"]),
    )


def generate_paired_sample(
    identity: SimulatorIdentity,
    calibration: Any,
    scenario_id: str,
    class_label: str,
    seed: int,
    out_root: str | Path,
    rgb_spp: int | None = None,
    variation: PhysicalVariation | None = None,
) -> PairedSample:
    """由**一個** ScenarioConfig 同時產出 RGB 與 500x4 ToF。

    成對性不是事後宣稱的：兩種模態讀的是同一份 config、同一個
    `scene_hash()`、同一個 seed family，而 `build_transient_scene_dict()`
    內部就是呼叫 `build_scene_dict()` 再換掉 integrator 與 film。
    幾何、材質、介質、照明因此逐位元相同。
    """
    import mitsuba as mi

    # `import mitransient` 是**必要的副作用**：transient_path /
    # transient_prbvolpath / transient_hdr_film 這三個 plugin 由它註冊進
    # mitsuba 的 PluginManager。少了它，build_transient_scene_dict() 組出來的
    # 場景會在 load_dict 當下報 "unknown plugin"，而錯誤訊息完全看不出
    # 真正的原因是少 import。
    import mitransient  # noqa: F401

    from pcmef.experiments.calibration_objective import scene_overrides
    from pcmef.simulation.mitransient_adapter import (
        MiTransientAdapter,
        build_transient_scene_dict,
    )
    from pcmef.simulation.mitsuba_adapter import Illumination, build_scene_dict
    from pcmef.surrogate.single_acquisition import SensorSurrogate
    from pcmef.surrogate.temporal_model import TemporalModel

    settings = identity.simulation_settings
    config = _scenario_config(identity, class_label, seed, variation)
    # spp 是**數值積分精度**，不是場景的物理狀態，因此 RGB 與 ToF 沒有理由
    # 綁在同一個值。ToF 一律用 E1 凍結的 spp（改它就不是被 E1 評估過的那個
    # 模擬器）；RGB 可以獨立提高，物理場景 scenario_hash 完全不變。
    effective_rgb_spp = int(rgb_spp if rgb_spp is not None else config.spp)
    sample_dir = Path(out_root) / scenario_id
    sample_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    adapter = MiTransientAdapter()
    temporal_bins = int(settings["temporal_bins"])
    max_depth = int(settings["max_depth"])

    with scene_overrides(identity.scene_constants):
        # -- RGB：同一個場景，BOTH 照明 --------------------------------------
        rgb_scene = mi.load_dict(build_scene_dict(mi, config))
        image = mi.render(rgb_scene, spp=effective_rgb_spp, seed=int(config.seed))
        exr_path = sample_dir / "rgb.exr"
        png_path = sample_dir / "rgb.png"
        mi.Bitmap(image).write(str(exr_path))
        (
            mi.Bitmap(image)
            .convert(mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.UInt8, srgb_gamma=True)
            .write(str(png_path))
        )

        # -- ToF：同一個場景，ACTIVE_ONLY + AMBIENT_ONLY 兩個 pass ------------
        start, width = adapter.default_binning(config, temporal_bins)
        cubes = []
        for illumination in (Illumination.ACTIVE_ONLY, Illumination.AMBIENT_ONLY):
            scene = mi.load_dict(
                build_transient_scene_dict(
                    mi, config, temporal_bins, start, width,
                    max_depth=max_depth, illumination=illumination,
                )
            )
            mi.render(scene, spp=int(config.spp), seed=int(config.seed))
            cube = np.array(scene.sensors()[0].film().develop_transient_())
            if not np.all(np.isfinite(cube)):
                raise PairedGenerationError(
                    "NON_FINITE_TRANSIENT",
                    f"{scenario_id} produced a non-finite transient under {illumination}",
                )
            cubes.append(cube)
        axis = (start + (np.arange(temporal_bins) + 0.5) * width) / 299792458.0

        recording = TemporalModel(SensorSurrogate(calibration)).generate_recording(
            cubes[0],
            axis,
            sample_interval_s=float(settings["sample_interval_s"]),
            sample_interval_source=str(settings["sample_interval_source"]),
            seed=int(config.seed),
            n_samples=int(settings["n_samples_per_recording"]),
            ambient_transient=cubes[1],
        )

    tof = np.ascontiguousarray(recording.values, dtype=np.float64)
    tof_path = sample_dir / "tof.npy"
    np.save(tof_path, tof)

    scene_parameters = {
        **config.to_dict(),
        "scene_constants_applied": dict(identity.scene_constants),
        "temporal_bins": temporal_bins,
        "max_depth": max_depth,
        "medium_values_are_frozen_initial": True,
        # `spp` 欄位是 ToF 的（凍結值）；RGB 的另記，兩者刻意分開。
        "tof_render_spp": int(config.spp),
        "rgb_render_spp": effective_rgb_spp,
        "physical_variation": (variation.to_dict() if variation else None),
        "nuisance_provenance": (dict(NUISANCE_PROVENANCE) if variation else None),
    }

    return PairedSample(
        scenario_id=scenario_id,
        class_label=class_label,
        class_index=CLASS_ORDER.index(class_label),
        scenario_hash=config.scene_hash(),
        seed_family={
            "scenario_seed": int(seed),
            "rgb_render_seed": int(seed),
            "tof_active_render_seed": int(seed),
            "tof_ambient_render_seed": int(seed),
            "tof_acquisition_seed": int(seed),
        },
        rgb_exr_path=exr_path.as_posix(),
        rgb_exr_sha256=hash_file(exr_path),
        rgb_png_path=png_path.as_posix(),
        rgb_png_sha256=hash_file(png_path),
        tof_path=tof_path.as_posix(),
        tof_sha256=hash_file(tof_path),
        tof_array_hash=hash_array(tof),
        tof_shape=list(tof.shape),
        feature_order=list(TOF_SCHEMA),
        scene_parameters=scene_parameters,
        simulator_identity_hash=identity.identity_hash(),
        calibrated_simulation_lock_hash=identity.calibrated_lock_hash,
        runtime_s=time.perf_counter() - started,
    )


# ---------------------------------------------------------------------------
# smoke set
# ---------------------------------------------------------------------------


#: family 資料集的種子基底。與 v1 診斷資料集（50000-50399）、
#: E1 評估（30000/40000 起）、校準（1001-2042）與 spp pilot（70000 起）
#: 全部不相交。
FAMILY_SEED_BASE = 60000


def family_scenario_seed(class_label: str, family_index: int, realization: int) -> int:
    """(class, family, realization) -> 唯一種子。

    family 進位 10、class 進位 1000，因此 20 個 family x 5 個 realization
    在每個 class 的區段內不會相撞，而四個 class 的區段也不重疊。
    """
    if not 0 <= realization < 10:
        raise PairedGenerationError(
            "SEED_RANGE", f"realization {realization} does not fit the seed layout"
        )
    if not 0 <= family_index < 100:
        raise PairedGenerationError(
            "SEED_RANGE", f"family_index {family_index} does not fit the seed layout"
        )
    return (
        FAMILY_SEED_BASE
        + 1000 * CLASS_ORDER.index(class_label)
        + 10 * family_index
        + realization
    )


def scenario_seed(class_label: str, index: int) -> int:
    """成對資料的種子。由 (class, index) 決定，因此可重建也可稽核。"""
    return PAIRED_SEED_BASE + 100 * CLASS_ORDER.index(class_label) + index


def run_paired_smoke(
    per_class: int = 1,
    out_root: str | Path = "outputs/paired",
    run_name: str | None = None,
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    code_version: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """產生小型成對 smoke set 並寫出 manifest。**不是 dataset。**"""
    say = progress or (lambda _m: None)
    identity, calibration = load_calibrated_simulator(freeze_dir, repo_root)
    say(f"simulator identity {identity.identity_hash()[:16]}")
    say(f"  calibrated_simulation.lock {identity.calibrated_lock_hash[:16]}")

    stamp = run_name or datetime.now(timezone.utc).strftime("smoke_%Y%m%dT%H%M%SZ")
    run_dir = Path(out_root) / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    samples: list[PairedSample] = []
    for class_label in CLASS_ORDER:
        for index in range(per_class):
            seed = scenario_seed(class_label, index)
            scenario_id = f"{class_label.lower().replace('-', '_')}_{index:02d}"
            say(f"  {scenario_id} (seed {seed})")
            samples.append(
                generate_paired_sample(
                    identity, calibration, scenario_id, class_label, seed, run_dir
                )
            )

    manifest = {
        "manifest_id": "paired_rgb_tof_smoke",
        "scientific_result": False,
        "purpose": (
            "E1 -> E2 paired-data bridge smoke set. Verifies that one scenario "
            "produces RGB and ToF together. It is NOT a dataset and must not be "
            "used to train, select or tune anything."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "run_dir": run_dir.as_posix(),
        "per_class": per_class,
        "counts": {
            "total": len(samples),
            "per_class": {
                c: sum(1 for s in samples if s.class_label == c) for c in CLASS_ORDER
            },
        },
        "class_order": list(CLASS_ORDER),
        "feature_order": list(TOF_SCHEMA),
        "seed_policy": {
            "base": PAIRED_SEED_BASE,
            "rule": "PAIRED_SEED_BASE + 100 * class_index + sample_index",
            "disjoint_from": {
                "calibration_crn": [1001, 1002, 1004, 1042],
                "calibration_verification": [2001, 2002, 2004, 2042],
                "e1_evaluation": "30000-30027 optical, 40000-40027 acquisition",
            },
        },
        "simulator": identity.to_artifact(),
        "samples": [s.to_dict() for s in samples],
    }
    (run_dir / "paired_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# 檢查
# ---------------------------------------------------------------------------


def verify_manifest(
    manifest: dict[str, Any],
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    check_determinism: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """跑完成對資料橋的全部驗收，回傳逐項結果。"""
    say = progress or (lambda _m: None)
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})
        say(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    identity, calibration = load_calibrated_simulator(freeze_dir, repo_root)
    samples = manifest["samples"]

    # -- 1. 成對身分 -------------------------------------------------------
    problems: list[str] = []
    for sample in samples:
        for label, path, digest in (
            ("rgb.exr", sample["rgb"]["exr_path"], sample["rgb"]["exr_sha256"]),
            ("rgb.png", sample["rgb"]["png_path"], sample["rgb"]["png_sha256"]),
            ("tof.npy", sample["tof"]["path"], sample["tof"]["sha256"]),
        ):
            target = Path(path)
            if not target.exists():
                problems.append(f"{sample['scenario_id']}/{label} missing")
            elif hash_file(target) != digest:
                problems.append(f"{sample['scenario_id']}/{label} hash mismatch")
        # RGB 與 ToF 必須落在同一個 scenario 目錄下，且共用同一個
        # scenario_hash 與 seed family —— 這是成對性的可驗證形式。
        if Path(sample["rgb"]["exr_path"]).parent != Path(sample["tof"]["path"]).parent:
            problems.append(f"{sample['scenario_id']}: rgb and tof are not co-located")
        family = sample["seed_family"]
        if len(set(family.values())) != 1:
            problems.append(f"{sample['scenario_id']}: seed family is not one family")
        if sample["simulator_identity_hash"] != identity.identity_hash():
            problems.append(f"{sample['scenario_id']}: simulator identity drifted")
    record(
        "paired_identity",
        not problems,
        "every sample carries a co-located RGB+ToF pair with matching file hashes, "
        f"one seed family and the same simulator identity ({len(samples)} samples)"
        if not problems
        else "; ".join(problems[:5]),
    )

    # -- 2. ToF 形狀 / 欄序 / 有限性 ---------------------------------------
    shape_problems: list[str] = []
    arrays: dict[str, np.ndarray] = {}
    for sample in samples:
        array = np.load(sample["tof"]["path"])
        arrays[sample["scenario_id"]] = array
        if array.shape != (TOF_RECORDING_POINTS, len(TOF_SCHEMA)):
            shape_problems.append(f"{sample['scenario_id']} shape {array.shape}")
        if sample["tof"]["feature_order"] != list(TOF_SCHEMA):
            shape_problems.append(f"{sample['scenario_id']} feature order")
        if not np.all(np.isfinite(array)):
            shape_problems.append(f"{sample['scenario_id']} non-finite")
    record(
        "tof_shape_order_finite",
        not shape_problems,
        f"all {len(samples)} recordings are {TOF_RECORDING_POINTS}x{len(TOF_SCHEMA)} "
        f"in {list(TOF_SCHEMA)} order and finite"
        if not shape_problems
        else "; ".join(shape_problems[:5]),
    )

    # -- 3. 四類不是全部相同 ------------------------------------------------
    by_class: dict[str, list[np.ndarray]] = {c: [] for c in CLASS_ORDER}
    for sample in samples:
        by_class[sample["class_label"]].append(arrays[sample["scenario_id"]])
    medians = {
        c: np.median(np.concatenate(v, axis=0), axis=0)
        for c, v in by_class.items()
        if v
    }
    distinct = len({tuple(np.round(v, 12)) for v in medians.values()})
    record(
        "classes_are_not_all_identical",
        distinct == len(medians),
        f"{distinct}/{len(medians)} classes have a distinct median vector; "
        + ", ".join(
            f"{c}: distance={v[0]:.4g} ambient={v[1]:.4g} signal={v[2]:.4g} sigma={v[3]:.4g}"
            for c, v in medians.items()
        ),
    )

    # -- 4. Ambient 確實用了被接受的校準映射 --------------------------------
    ambient = _check_ambient_mapping(identity, calibration, samples[0])
    record("ambient_uses_calibrated_mapping", ambient["passed"], ambient["detail"])

    # -- 5. 被抑制的參數仍為 retained state ---------------------------------
    retained = _check_retained_parameters(identity, freeze_dir, repo_root)
    record("inhibited_parameters_retained", retained["passed"], retained["detail"])

    # -- 6. 決定性 ---------------------------------------------------------
    if check_determinism:
        determinism = _check_determinism(identity, calibration, samples[0], repo_root)
        record("deterministic_reproduction", determinism["passed"], determinism["detail"])

    return {
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "simulator_identity_hash": identity.identity_hash(),
    }


def _check_ambient_mapping(
    identity: SimulatorIdentity, calibration: Any, sample: dict[str, Any]
) -> dict[str, Any]:
    """Ambient 是純增益，因此換掉增益後的比值必須恰好等於增益比。

    這比「數值看起來變小了」精確得多：`map_ambient_rate` 是
    `ambient_energy * scale` 再乘上抖動，抖動的亂數抽取不受 scale 影響，
    因此兩次執行的逐點比值必然等於兩個 scale 的比值。
    """
    from dataclasses import replace

    from pcmef.surrogate.calibration import CalibratedScale

    calibrated_gain = float(identity.fitted_parameters["ambient_energy_to_mcps"])
    initial_gain = 1.0
    if calibrated_gain == initial_gain:
        return {
            "passed": False,
            "detail": (
                "the calibrated ambient gain equals the initial value; MAPPING_AMBIENT "
                "is recorded as CONVERGED so this would mean the accepted update was "
                "not applied"
            ),
        }

    array = np.load(sample["tof"]["path"])
    ambient_index = TOF_SCHEMA.index("ambient_rate_mcps")
    calibrated_column = array[:, ambient_index]

    probe = replace(
        calibration,
        ambient_energy_to_mcps=CalibratedScale(
            value=initial_gain, placeholder=False, source="probe: frozen initial gain"
        ),
    )
    baseline = _regenerate_tof(identity, probe, sample)[:, ambient_index]

    ratio = calibrated_column / baseline
    ok = bool(np.allclose(ratio, calibrated_gain, rtol=1e-12, atol=0.0))
    return {
        "passed": ok,
        "detail": (
            f"ambient(calibrated)/ambient(initial gain 1.0) = {float(ratio[0]):.12g} "
            f"for all 500 samples, exactly the accepted gain "
            f"{calibrated_gain:.12g}; jitter {identity.fitted_parameters['ambient_jitter_relative']:.12g} "
            "also comes from the accepted update"
            if ok
            else f"ratio spread {float(ratio.min()):.6g}..{float(ratio.max()):.6g} "
            f"does not match the accepted gain {calibrated_gain:.6g}"
        ),
    }


def _check_retained_parameters(
    identity: SimulatorIdentity, freeze_dir: str | Path, repo_root: str | Path
) -> dict[str, Any]:
    """被抑制的階段其參數必須逐位元等於凍結初值。"""
    initial = json.loads(
        (Path(freeze_dir) / "initial_simulation.lock.json").read_text(encoding="utf-8")
    )["payload"]["initial_parameter_values"]

    inhibited_parameters = {
        "SCENE_GEOMETRY_SURFACE_FOIL": ["sensor.fov_deg"],
        "MAPPING_SIGNAL": ["signal_energy_to_mcps", "noise_relative_sigma"],
        "MAPPING_SIGMA": ["sigma_width_to_mm", "sigma_multipath_weight"],
    }
    drifted: list[str] = []
    checked: list[str] = []
    for stage in identity.inhibited_stages:
        for name in inhibited_parameters.get(stage, []):
            checked.append(name)
            if float(identity.fitted_parameters[name]) != float(initial[name]):
                drifted.append(
                    f"{name}={identity.fitted_parameters[name]!r} != initial "
                    f"{initial[name]!r}"
                )
    return {
        "passed": not drifted and bool(checked),
        "detail": (
            f"{len(checked)} parameter(s) from inhibited stages "
            f"{identity.inhibited_stages} are bit-identical to their frozen initial "
            f"values: {', '.join(f'{n}={initial[n]}' for n in checked)}"
            if not drifted
            else "; ".join(drifted)
        ),
    }


def _regenerate_tof(
    identity: SimulatorIdentity, calibration: Any, sample: dict[str, Any]
) -> np.ndarray:
    """以相同身分重算同一個 scenario 的 ToF（不寫檔）。"""
    import mitsuba as mi
    import mitransient  # noqa: F401  （註冊 transient plugin，見 generate_paired_sample）

    from pcmef.experiments.calibration_objective import scene_overrides
    from pcmef.simulation.mitransient_adapter import (
        MiTransientAdapter,
        build_transient_scene_dict,
    )
    from pcmef.simulation.mitsuba_adapter import Illumination
    from pcmef.surrogate.single_acquisition import SensorSurrogate
    from pcmef.surrogate.temporal_model import TemporalModel

    settings = identity.simulation_settings
    config = _scenario_config(
        identity, sample["class_label"], sample["seed_family"]["scenario_seed"]
    )
    adapter = MiTransientAdapter()
    temporal_bins = int(settings["temporal_bins"])

    with scene_overrides(identity.scene_constants):
        start, width = adapter.default_binning(config, temporal_bins)
        cubes = []
        for illumination in (Illumination.ACTIVE_ONLY, Illumination.AMBIENT_ONLY):
            scene = mi.load_dict(
                build_transient_scene_dict(
                    mi, config, temporal_bins, start, width,
                    max_depth=int(settings["max_depth"]), illumination=illumination,
                )
            )
            mi.render(scene, spp=int(config.spp), seed=int(config.seed))
            cubes.append(np.array(scene.sensors()[0].film().develop_transient_()))
        axis = (start + (np.arange(temporal_bins) + 0.5) * width) / 299792458.0
        recording = TemporalModel(SensorSurrogate(calibration)).generate_recording(
            cubes[0], axis,
            sample_interval_s=float(settings["sample_interval_s"]),
            sample_interval_source=str(settings["sample_interval_source"]),
            seed=int(config.seed),
            n_samples=int(settings["n_samples_per_recording"]),
            ambient_transient=cubes[1],
        )
    return np.ascontiguousarray(recording.values, dtype=np.float64)


def _check_determinism(
    identity: SimulatorIdentity,
    calibration: Any,
    sample: dict[str, Any],
    repo_root: str | Path,
) -> dict[str, Any]:
    """同一個身分重跑一次，ToF 必須逐位元相同。"""
    repeated = _regenerate_tof(identity, calibration, sample)
    array_hash = hash_array(repeated)
    ok = array_hash == sample["tof"]["array_hash"]
    return {
        "passed": ok,
        "detail": (
            f"{sample['scenario_id']} regenerated to the same array hash "
            f"{array_hash[:16]}"
            if ok
            else f"regenerated hash {array_hash[:16]} != recorded "
            f"{sample['tof']['array_hash'][:16]}"
        ),
    }
