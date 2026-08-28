# PC-MEF Research System source maintenance contract
# 上下游: 由 simulation.controller 呼叫；讀 ScenarioConfig，寫出 RGB 影像與
#         render metadata 到 artifact 目錄；產生的 scene dict 同時供
#         mitransient_adapter 重用，確保 RGB 與 transient 來自同一場景。
# 檔案路徑: pcmef/simulation/mitsuba_adapter.py
# 產生時間: 2026-08-26 06:35 +08:00
# 版本: v0.5.0
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
#   - 不得把純視覺需求的幾何放回 canonical physical scene。RGB 需要背景
#     就用 environment emitter，不要用會進 ToF 光路的實體（NOTE-029）。
#   - 不得因為「Empty 沒有介質」就省略內圓柱；那會讓空瓶變成實心玻璃柱。
#   - 不得把 _BOTTLE_SURFACE_ALPHA 設為 0：會退回 delta BSDF，與 delta 光源
#     之間的鏡面路徑採樣機率為零，瓶子完全不回光，且不會有任何錯誤訊息
#     （NOTE-027）。
#   - 本檔的 _ROOM_LIGHT_RATIO / _BOTTLE_SURFACE_ALPHA /
#     _SIGMA_T_REFERENCE_PER_M / _ALBEDO_BY_PRESET 皆為未校準建模常數。
#     它們現已納入 configs/parameter_registry.yaml 並由 core.parameters 的
#     formal 防線攔截；**新增任何建模常數都必須同時登記**，否則
#     tests/unit/test_parameter_registry.py 的綁定測試會失敗（NOTE-030）。
#   - 不得把 _SENSOR_FOV_DEG 逕自改成 25：本檔的 fov 是**全角**而
#     cutoff_angle 是**半角**（實測見 NOTE-033），兩者慣例不同；
#     VL53L0X 規格值的角度慣例尚無一手來源，改值前必須先取得。
#   - v0.1.0 新增：首版 RGB smoke adapter。
#   - v0.2.0 光源改為與相機共置的 spot、另加室內環境光，最短光程改由
#     共置幾何推導（NOTE-026）。
#   - v0.3.0 瓶壁由 dielectric 改為 roughdielectric，解決 SDS 導致
#     瓶子完全不回光（NOTE-027）。
#   - v0.4.0 移除無 provenance 的 backdrop、改建 far-side 鋁箔反射體；
#     內圓柱一律建立（修正 Empty 的實心玻璃柱拓樸錯誤）；
#     內部基底折射率依類別語意決定（NOTE-029）。
#   - v0.5.0 把 fov / cutoff_angle / max_depth / foil orientation 由 dict
#     literal 內的字面值提升為具名常數，讓 parameter registry 攔得到；
#     **數值一個都沒有改**（NOTE-030、NOTE-033）。
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

    # NOTE(NOTE-026): 收發同軸（monostatic），光源與相機在同一點，
    # 因此最短光程就是「相機 -> 瓶面 -> 相機」的來回，恰為單程距離的兩倍。
    # 這正是 optical_path_to_distance = 0.5 成立的幾何前提。
    shortest = 2.0 * sensor_distance
    # NOTE(NOTE-029): 場景跨距改由箔片位置推得。原本用的 backdrop 已移出
    # canonical physical scene，時間窗不得再依賴一個不存在的物件。
    extent = max(foil_center_z(outer_r) - camera_z, outer_r * 4.0)
    return float(shortest), float(extent)


#: 室內環境光相對於感測器 VCSEL 的強度比。**建模常數，尚未校準** ——
#: 真實比例必須由 E1 calibration 決定。取小值是因為 ToF 感測器的
#: signal rate 應由自己的發射器主導，ambient 只是背景。
_ROOM_LIGHT_RATIO = 0.02

#: 瓶壁表面的 GGX 粗糙度。**建模常數，尚未校準** ——
#: 真值須由 E1 calibration 決定（NOTE-027）。取小值代表「接近光滑但不是理想
#: 鏡面」，對應 PET/玻璃瓶實際的微觀表面。這個值不得為 0：
#: 0 會退化成 delta BSDF，與 delta 光源之間的鏡面路徑在 path tracing 中
#: 機率為零，瓶子會完全不回光。
_BOTTLE_SURFACE_ALPHA = 0.02

# NOTE(NOTE-029): far-side aluminum foil reflector。
# **結構已知、數值未知**，兩者必須分開對待：
#   - 反射體存在、材質為鋁箔、位於瓶身另一側 —— SRC-HANDOFF §0 把它的
#     offset/orientation/BRDF/940nm reflectance 列為「未回收」，
#     反證該物件本身存在。這部分是 provenance-supported 的**結構**。
#   - 下面四個數值全部 UNKNOWN，是 calibration-only nuisance parameters。
#     取值一律不得依類別而異，也不得為了讓 distance 貼近真實均值而挑選。
#: 箔片與瓶外壁的間距。**未校準**；取「一個瓶半徑」純粹是幾何慣例，
#: 不是量出來的，也不是為了得到任何特定距離。
_FOIL_GAP_TO_BOTTLE_RATIO = 1.0
#: 箔片邊長相對瓶徑的倍數。取 4 是為了在感測器視角下完全覆蓋瓶身輪廓，
#: 使遠側回波不會因箔片太小而被邊緣截斷。**未校準**。
_FOIL_SIZE_TO_DIAMETER_RATIO = 4.0
#: 鋁箔在 940 nm 的有效反射率。鋁在近紅外的鏡面反射率約 0.95－0.98，
#: 但實際箔片有皺褶與氧化層，有效值必然更低。**未校準**。
_FOIL_REFLECTANCE_940NM = 0.85
#: 箔片表面的 GGX 粗糙度。家用鋁箔有明顯皺褶，不是光學鏡面。**未校準**。
_FOIL_SURFACE_ALPHA = 0.15

# NOTE(NOTE-033): 以下三個量原本硬編在 build_scene_dict() 的 dict literal 裡，
# 不是具名常數，因此 parameter registry 的 formal 防線完全攔不到。這裡只做
# **參數化**，數值一個都沒有改 —— 改值需要先有 provenance，見 NOTE-031。
#
# 角度慣例在本檔內**不一致**，這是實測結果不是推測（NOTE-033 附量測）：
#   - perspective sensor 的 `fov` 是**全角**，且預設綁在 x 軸。
#     fov=45 -> 實測單邊半角 22.4959°，畫面對角 30.3562°。
#   - spot emitter 的 `cutoff_angle` 是**半角**（自軸線起算）。
#     cutoff_angle=25 -> 實測 24.9° 仍有光、25.1° 為 0，即全角 50°。
# 也就是說本檔的兩個角度雖然都寫了「25」，意思差了兩倍。
#: 接收端視角。**全角**，見上。VL53L0X 規格值的角度慣例尚未取得一手來源，
#: 因此**不得**逕自改成 25（NOTE-033）。
_SENSOR_FOV_DEG = 45.0
#: VCSEL 發散角。**半角**，故實際照明錐為 50° 全角。**未校準**。
_LIGHT_CUTOFF_ANGLE_DEG = 25.0
#: path tracer 最大反射深度。實測 far-side 箔片回波需 >= 12 才出現（NOTE-029）；
#: 設太低會靜默丟失整個 path family 且無任何錯誤訊息。**未校準**。
_MAX_DEPTH = 12
#: 箔片法線方向。SRC-HANDOFF §0 明列未回收，目前取「正對相機」。
#: 這是**建模假設**而非量測值；改成具名常數只是為了讓防線攔得到它。
_FOIL_ORIENTATION = "facing_camera"

#: 瓶內基底介質的折射率名稱，依類別語意決定（SRC-SAI FR-002 標籤定義）。
#: 這是**拓樸/材質類別**而非可調數值：霧是懸浮在空氣中的液滴，
#: 氣泡是水中的氣體，因此兩者的基底本來就不同。
#: 先前所有非 Empty 類一律用 water，對 Misty 是錯的。
_INTERIOR_BASE_IOR: dict[str, str] = {
    "empty": "air",
    "water": "water",
    "bubbly": "water",
    "misty": "air",
}


def foil_center_z(outer_r_m: float) -> float:
    """箔片中心的 z 座標（公尺）。單一來源，場景與時間窗共用。"""
    return outer_r_m * (1.0 + _FOIL_GAP_TO_BOTTLE_RATIO)


def _foil_transform(mi, outer_r_m: float):
    """箔片的位置與朝向，尺寸足以在感測器視角下覆蓋瓶身輪廓。

    朝向由 `_FOIL_ORIENTATION` 決定。目前只實作 facing_camera，其餘取值一律
    拒絕而非退回預設 —— 「未實作的朝向」與「刻意選了正對相機」必須分得開
    （NOTE-005 的同一個道理）。
    """
    if _FOIL_ORIENTATION != "facing_camera":
        raise SimulationDependencyError(
            f"foil orientation {_FOIL_ORIENTATION!r} is registered but not "
            "implemented; SRC-HANDOFF §0 lists the real orientation as not "
            "recovered, so it must not be silently approximated by the "
            "facing_camera case"
        )
    half = outer_r_m * _FOIL_SIZE_TO_DIAMETER_RATIO
    transform = _look_at(
        mi,
        origin=[0.0, 0.0, foil_center_z(outer_r_m)],
        target=[0.0, 0.0, 0.0],
        up=[0.0, 1.0, 0.0],
    )
    return transform @ mi.ScalarTransform4f().scale([half, half, 1.0])


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
        "integrator": {"type": "path", "max_depth": _MAX_DEPTH},
        "sensor": {
            "type": "perspective",
            # 全角，且預設綁 x 軸；半角為此值的一半（NOTE-033）。
            "fov": _SENSOR_FOV_DEG,
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
        # NOTE(NOTE-027): 必須是 roughdielectric 而非 dielectric。
        "bottle_wall": {
            "type": "cylinder",
            "p0": [0.0, -height / 2, 0.0],
            "p1": [0.0, height / 2, 0.0],
            "radius": outer_r,
            "bsdf": {
                "type": "roughdielectric",
                "int_ior": "bk7",
                "ext_ior": "air",
                "distribution": "ggx",
                "alpha": _BOTTLE_SURFACE_ALPHA,
            },
        },
        # NOTE(NOTE-026): 光源與相機共置（monostatic），對應 VL53L0X 的
        # VCSEL 與 SPAD 同軸配置。用 spot 而非 area rectangle 有兩個理由：
        # 其一，rectangle 是實體幾何，放在相機同一點會與相機視線重疊；
        # 其二，VCSEL 本來就是帶發散角的點狀照明，spot 更貼近實物。
        # 共置讓光程恰為單程距離的兩倍，surrogate 的
        # optical_path_to_distance = 0.5 因此由幾何成立，不必調係數去湊。
        "light": {
            "type": "spot",
            "to_world": _look_at(
                mi,
                origin=[lateral, 0.0, camera_z],
                target=[0.0, 0.0, 0.0],
                up=[0.0, 1.0, 0.0],
            ),
            # 半角：實測 24.9° 有光、25.1° 為 0，故照明錐為 50° 全角（NOTE-033）。
            "cutoff_angle": _LIGHT_CUTOFF_ANGLE_DEG,
            "intensity": {
                "type": "rgb",
                "value": [config.lighting.irradiance] * 3,
            },
        },
        # NOTE(NOTE-026): 室內環境光，與感測器自己的 spot 分開。
        # 改成 monostatic 之後場景只剩感測器發光，ambient 會恆為 0 ——
        # 而真實 VL53L0X 的 ambient rate 量的正是「不是自己打出去的光」，
        # 也就是室內光。少了它，四特徵中的 ambient 這一欄不具意義。
        # 比例為建模常數，尚未校準：真實室內光與 VCSEL 的相對強度
        # 必須由 E1 calibration 決定。
        "room_light": {
            "type": "constant",
            "radiance": {
                "type": "rgb",
                "value": [config.lighting.irradiance * _ROOM_LIGHT_RATIO] * 3,
            },
        },
        # NOTE(NOTE-029): far-side aluminum foil reflector。
        # 這裡取代了原本的 `backdrop` —— 那是一塊 reflectance 0.5 的漫反射板，
        # 純粹為了讓 RGB 有背景而加，**在 SRC-PLAN 與 SRC-HANDOFF 都找不到依據**，
        # 卻在 ToF 通道佔掉 47–55% 的能量。canonical physical scene 只放
        # 真實存在的物件；RGB 的背景改由 room_light（constant environment，
        # 無幾何、不形成有限距離回波）提供。
        "foil": {
            "type": "rectangle",
            "to_world": _foil_transform(mi, outer_r),
            "bsdf": {
                "type": "roughconductor",
                "material": "Al",
                "distribution": "ggx",
                "alpha": _FOIL_SURFACE_ALPHA,
                "specular_reflectance": {
                    "type": "rgb",
                    "value": [_FOIL_REFLECTANCE_940NM] * 3,
                },
            },
        },
    }

    # NOTE(NOTE-029): 內圓柱**一律建立，包括 Empty**。
    # 先前 Empty 不建內圓柱，於是半徑 28.5 mm 的 bottle_wall 變成一根
    # **實心 bk7 圓柱** —— 光要穿過 57 mm 玻璃，而不是「2 mm 殼 + 53 mm 空氣
    # + 2 mm 殼」。實測 Empty 的遠側成分因此落在 142 mm（實心玻璃預期 136.5），
    # 而正確殼結構應為 109.1 mm。
    #
    # 這是**幾何拓樸錯誤**，不是未校準參數：空瓶裡面是空氣這件事不需要校準。
    # 修它與「調係數去貼真實均值」是兩回事。
    #
    # NOTE(NOTE-026): 沒有參與介質時，Water / Bubbly / Misty 會是三個
    # **完全相同**的場景 —— 同一個 dielectric 內圓柱，只有 seed 不同。
    # 那會讓四類在 distance 上得到同一個值，E1 要比較的分佈差異根本不存在。
    # 本研究要區分的正是散射行為，因此散射必須真的進場景。
    preset = config.medium_preset.value
    interior: dict[str, Any] = {
        "type": "cylinder",
        "p0": [0.0, -height / 2 * 0.98, 0.0],
        "p1": [0.0, height / 2 * 0.98, 0.0],
        "radius": inner_r,
        "bsdf": {
            "type": "dielectric",
            # 基底折射率依類別語意決定：霧是空氣中的液滴，氣泡是水中的氣體。
            "int_ior": _INTERIOR_BASE_IOR.get(preset, "air"),
            "ext_ior": "bk7",
        },
    }
    medium = _medium_dict(config)
    if medium is not None:
        interior["interior"] = medium
    scene["bottle_interior"] = interior
    return scene


#: 把設定裡的無單位密度換算成散射係數 sigma_t (1/m) 的參考尺度。
#: 選 100 是為了讓瓶徑 5.7 cm 在密度 0.1–0.3 時得到光學厚度 τ≈0.6–1.7，
#: 也就是「看得見但不到全散射」的範圍 —— 這是**建模常數，尚未校準**。
#: 真正的值必須由 E1 calibration 決定；在此之前上游的密度參數仍是 placeholder，
#: formal 模式會在 ScenarioConfig 就拒絕載入，不會走到這裡。
_SIGMA_T_REFERENCE_PER_M = 100.0

#: 各類的單次散射反照率。Bubbly/Misty 以散射為主，Water 另有吸收。
#: 同樣是尚未校準的建模常數。
_ALBEDO_BY_PRESET: dict[str, float] = {
    "water": 0.60,
    "bubbly": 0.92,
    "misty": 0.88,
}

#: 各類的密度參數名稱，對應 configs/simulation/*.yaml 的 medium 欄位。
_DENSITY_KEY_BY_PRESET: dict[str, str] = {
    "water": "turbidity",
    "bubbly": "bubble_density",
    "misty": "mist_density",
}


def _medium_value(raw: Any) -> float | None:
    """取出介質參數的數值。placeholder 以 {'value': x, 'placeholder': True} 表示。"""
    if isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _medium_dict(config: ScenarioConfig) -> dict[str, Any] | None:
    """由設定的密度參數組出 homogeneous participating medium。

    參數缺失時回傳 None（退回純 dielectric），而不是自己補一個預設密度 ——
    補值會讓「忘了設定」與「刻意設成很稀」長得一樣，
    而前者應該被看見（NOTE-005 的同一個道理）。
    """
    preset = config.medium_preset.value
    key = _DENSITY_KEY_BY_PRESET.get(preset)
    if key is None:
        return None
    density = _medium_value(config.medium_parameters.get(key))
    if density is None or density <= 0.0:
        return None

    sigma_t = density * _SIGMA_T_REFERENCE_PER_M
    albedo = _ALBEDO_BY_PRESET[preset]
    return {
        "type": "homogeneous",
        # sigma_t 用 uniform 而非 rgb：rgb spectrum 會被當成反照率並限制在
        # [0,1]，而消光係數的單位是 1/m，數值遠大於 1。
        "sigma_t": {"type": "uniform", "value": sigma_t},
        "albedo": {"type": "rgb", "value": [albedo] * 3},
    }


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
