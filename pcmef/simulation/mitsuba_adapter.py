# PC-MEF Research System source maintenance contract
# 上下游: 由 simulation.controller 呼叫；讀 ScenarioConfig，寫出 RGB 影像與
#         render metadata 到 artifact 目錄；產生的 scene dict 同時供
#         mitransient_adapter 重用，確保 RGB 與 transient 來自同一場景。
# 檔案路徑: pcmef/simulation/mitsuba_adapter.py
# 產生時間: 2026-08-26 06:35 +08:00
# 版本: v0.1.0
# 功能說明: 把場景設定翻成 Mitsuba 能懂的場景描述並算出一張 RGB 影像，
#           同時記下算圖當下的所有版本與參數，讓同樣的輸入日後能算出同樣的結果。
# 模組定位: Mitsuba 的封裝層。它「不是」物理校準器 —— 本批次只做 single-scenario
#           smoke，材質參數尚未校準，不得據此宣稱任何 fidelity。
# 主要責任:
#   1. require_mitsuba() 延遲載入並設定 variant，缺相依時給出可行動的訊息
#   2. build_scene_dict() 以公尺為單位組出瓶身、介質、光源與相機
#   3. MitsubaAdapter.render_rgb() 算圖並寫出 EXR/PNG
#   4. RenderResult 保存 version/variant/scene_hash/seed/spp/runtime
# 維護提醒:
#   - 不得改用毫米當場景單位；transient 的光飛行時間直接取決於場景尺度，
#     用毫米會讓時間軸整整差三個數量級。
#   - 不得在 retry 時更換 seed；SRC-SAI §28 規定 render 失敗只能以相同
#     config/seed 重試，換 seed 等於偷換實驗條件。
#   - 不得把本模組產出的影像當成 real-fidelity 證據；claim boundary 見 E1-G12。
#   - v0.1.0 新增：首版 RGB smoke adapter。
# 驗證方式:
#   - py -3.10 -m pytest tests/simulation/test_simulation.py -k "rgb or variant or scale" -v
# ------------------------------------------------------------

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pcmef.simulation.scenario import ScenarioConfig

__all__ = [
    "SimulationDependencyError",
    "RenderResult",
    "require_mitsuba",
    "build_scene_dict",
    "scene_path_bounds",
    "MitsubaAdapter",
]

# CPU 後端。cuda_ad_rgb 需要 NVIDIA GPU，本機為 Intel 內顯，因此不列為預設。
DEFAULT_VARIANT = "llvm_ad_rgb"

_MM_PER_M = 1000.0


class SimulationDependencyError(RuntimeError):
    """Mitsuba / drjit 後端不可用。訊息必須說明怎麼修，而不只是說壞了。"""


@dataclass(frozen=True)
class RenderResult:
    """一次算圖的產出與完整 provenance。"""

    scenario_hash: str
    variant: str
    mitsuba_version: str
    drjit_version: str
    seed: int
    spp: int
    resolution: tuple[int, int]
    runtime_s: float
    output_paths: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_hash": self.scenario_hash,
            "variant": self.variant,
            "mitsuba_version": self.mitsuba_version,
            "drjit_version": self.drjit_version,
            "seed": self.seed,
            "spp": self.spp,
            "resolution": list(self.resolution),
            "runtime_s": round(self.runtime_s, 4),
            "outputs": dict(self.output_paths),
            **self.extra,
        }


def require_mitsuba(variant: str = DEFAULT_VARIANT):
    """載入 mitsuba 並設定 variant。失敗時給出可行動的診斷。

    延遲載入的理由：mitsuba 是 46 MB 的原生擴充，core 與 adapters 的測試
    不該因為它沒裝就無法收集。
    """
    try:
        import mitsuba as mi
    except ImportError as error:
        raise SimulationDependencyError(
            "mitsuba is not installed. Install the simulation extra:\n"
            '  py -3.10 -m pip install -e ".[simulation]"'
        ) from error

    if variant not in mi.variants():
        raise SimulationDependencyError(
            f"variant {variant!r} is not compiled into this mitsuba build; "
            f"available: {mi.variants()}"
        )
    try:
        mi.set_variant(variant)
        # set_variant 本身可能成功，但後端要到實際配置陣列時才初始化。
        mi.load_dict({"type": "sphere"})
    except Exception as error:  # noqa: BLE001
        message = str(error)
        if "LLVM" in message:
            raise SimulationDependencyError(
                "drjit's LLVM backend could not initialise. On Windows drjit loads "
                "LLVM-C.dll at runtime and does not bundle it. Install the LLVM "
                "toolchain and make sure its bin directory is on PATH:\n"
                "  winget install --id LLVM.LLVM\n"
                f"underlying error: {message.splitlines()[0]}"
            ) from None
        if "CUDA" in message:
            raise SimulationDependencyError(
                "the CUDA backend is unavailable; this machine has no NVIDIA GPU. "
                f"Use {DEFAULT_VARIANT!r} instead.\n"
                f"underlying error: {message.splitlines()[0]}"
            ) from None
        raise SimulationDependencyError(
            f"mitsuba variant {variant!r} failed to initialise: {message}"
        ) from None
    return mi


def scene_path_bounds(config: ScenarioConfig) -> tuple[float, float]:
    """回傳 (最短光程 m, 場景跨距 m)，供 transient 時間窗推導使用。

    最短光程取「光源 -> 瓶面前緣 -> 相機」這條直達路徑；場景跨距取背景板到
    相機的距離與瓶身高度中的較大者，作為每次額外反射的路徑增量上界。
    兩者都由 build_scene_dict() 實際使用的座標推得，因此場景一改這裡就跟著改，
    不會出現「幾何改了但時間窗還停在舊尺度」的靜默錯誤。
    """
    geometry = config.geometry
    outer_r = geometry.outer_radius_mm / _MM_PER_M
    sensor_distance = geometry.sensor_to_bottle_mm / _MM_PER_M
    camera_z = -(sensor_distance + outer_r)
    light_position = (0.0, outer_r * 4.0, camera_z * 0.5)
    backdrop_z = outer_r * 6.0

    light_to_bottle = math.dist(light_position, (0.0, 0.0, -outer_r))
    shortest = light_to_bottle + sensor_distance
    extent = max(backdrop_z - camera_z, outer_r * 4.0)
    return float(shortest), float(extent)


def _look_at(mi, origin, target, up):
    """跨版本相容的 look_at。Mitsuba 3.6 前後的 Transform4f API 不同。"""
    transform = mi.ScalarTransform4f
    if hasattr(transform, "look_at"):
        try:
            return transform().look_at(origin=origin, target=target, up=up)
        except TypeError:
            return transform.look_at(origin=origin, target=target, up=up)
    raise SimulationDependencyError("this mitsuba build exposes no look_at transform")


def build_scene_dict(mi, config: ScenarioConfig) -> dict[str, Any]:
    """把 ScenarioConfig 組成 Mitsuba 場景描述。

    單位一律為公尺：transient 渲染的時間軸由光在場景中的行進距離決定，
    尺度錯了時間軸就整體錯。幾何取自 SRC-PLAN §2.1 的前研究錨點。
    """
    geometry = config.geometry
    outer_r = geometry.outer_radius_mm / _MM_PER_M
    inner_r = geometry.inner_radius_mm / _MM_PER_M
    sensor_distance = geometry.sensor_to_bottle_mm / _MM_PER_M
    lateral = geometry.lateral_offset_mm / _MM_PER_M
    height = outer_r * 4.0

    # 相機沿 -Z 看向瓶心；瓶身為沿 Y 軸的圓柱。
    camera_z = -(sensor_distance + outer_r)

    scene: dict[str, Any] = {
        "type": "scene",
        "integrator": {"type": "path", "max_depth": 12},
        "sensor": {
            "type": "perspective",
            "fov": 45.0,
            "to_world": _look_at(
                mi,
                origin=[lateral, 0.0, camera_z],
                target=[0.0, 0.0, 0.0],
                up=[0.0, 1.0, 0.0],
            ),
            "film": {
                "type": "hdrfilm",
                "width": int(config.resolution[0]),
                "height": int(config.resolution[1]),
                "rfilter": {"type": "gaussian"},
                "pixel_format": "rgb",
            },
            "sampler": {
                "type": "independent",
                "sample_count": int(config.spp),
                "seed": int(config.seed),
            },
        },
        # 瓶壁：玻璃介電質。折射率為玻璃的通用值，非校準結果。
        "bottle_wall": {
            "type": "cylinder",
            "p0": [0.0, -height / 2, 0.0],
            "p1": [0.0, height / 2, 0.0],
            "radius": outer_r,
            "bsdf": {"type": "dielectric", "int_ior": "bk7", "ext_ior": "air"},
        },
        "light": {
            "type": "rectangle",
            "to_world": _look_at(
                mi,
                origin=[0.0, height, camera_z * 0.5],
                target=[0.0, 0.0, 0.0],
                up=[0.0, 0.0, 1.0],
            ),
            "emitter": {
                "type": "area",
                "radiance": {
                    "type": "rgb",
                    "value": [config.lighting.irradiance] * 3,
                },
            },
        },
        "backdrop": {
            "type": "rectangle",
            "to_world": _look_at(
                mi,
                origin=[0.0, 0.0, outer_r * 6.0],
                target=[0.0, 0.0, 0.0],
                up=[0.0, 1.0, 0.0],
            ),
            "bsdf": {"type": "diffuse", "reflectance": {"type": "rgb", "value": 0.5}},
        },
    }

    # 內部介質。Empty 代表空氣，不放額外幾何；其餘放一個內圓柱代表液體/氣霧。
    if config.medium_preset.value != "empty":
        scene["bottle_interior"] = {
            "type": "cylinder",
            "p0": [0.0, -height / 2 * 0.98, 0.0],
            "p1": [0.0, height / 2 * 0.98, 0.0],
            "radius": inner_r,
            "bsdf": {"type": "dielectric", "int_ior": "water", "ext_ior": "bk7"},
        }
    return scene


class MitsubaAdapter:
    """RGB 算圖的封裝。"""

    def __init__(self, variant: str = DEFAULT_VARIANT) -> None:
        self.variant = variant

    def render_rgb(
        self, config: ScenarioConfig, out_dir: str | Path, stem: str = "rgb"
    ) -> RenderResult:
        """算一張 RGB 並寫出 EXR。回傳含完整 provenance 的結果。"""
        import drjit

        mi = require_mitsuba(self.variant)
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        scene_dict = build_scene_dict(mi, config)
        started = time.perf_counter()
        scene = mi.load_dict(scene_dict)
        image = mi.render(scene, spp=int(config.spp), seed=int(config.seed))
        runtime = time.perf_counter() - started

        exr_path = target_dir / f"{stem}.exr"
        mi.Bitmap(image).write(str(exr_path))

        png_path = target_dir / f"{stem}.png"
        (
            mi.Bitmap(image)
            .convert(mi.Bitmap.PixelFormat.RGB, mi.Struct.Type.UInt8, srgb_gamma=True)
            .write(str(png_path))
        )

        return RenderResult(
            scenario_hash=config.scene_hash(),
            variant=self.variant,
            mitsuba_version=mi.__version__,
            drjit_version=drjit.__version__,
            seed=int(config.seed),
            spp=int(config.spp),
            resolution=tuple(config.resolution),
            runtime_s=runtime,
            output_paths={
                "rgb_exr": exr_path.as_posix(),
                "rgb_png": png_path.as_posix(),
            },
            extra={
                "integrator": scene_dict["integrator"]["type"],
                "max_depth": scene_dict["integrator"]["max_depth"],
                "scene_units": "metre",
                "medium_preset": config.medium_preset.value,
                "uses_placeholder_medium": config.uses_placeholder_medium(),
            },
        )
