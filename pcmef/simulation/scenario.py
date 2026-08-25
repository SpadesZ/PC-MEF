# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 sim smoke 與未來的 scenario_generator 建構；讀 configs/simulation/
#         下的 scenario YAML；輸出的 ScenarioConfig 供 mitsuba_adapter 與
#         mitransient_adapter 建場景，其 scene_hash 進 initial_simulation.lock。
# 檔案路徑: pcmef/simulation/scenario.py
# 產生時間: 2026-08-26 06:10 +08:00
# 版本: v0.1.0
# 功能說明: 描述「要模擬哪一個場景」—— 瓶子的幾何尺寸、裡面裝什麼介質、
#           打什麼光、用哪個亂數種子，並把已核定的數值與尚待驗證的數值分開，
#           讓後者在 formal 模式下無法被悄悄帶進模擬。
# 模組定位: 模擬層的輸入契約。它不呼叫 Mitsuba，也不決定材質的物理參數 ——
#           那些必須來自校準結果或教授核定，不是這裡的預設值。
# 主要責任:
#   1. MediumPreset 列舉四類液態狀態對應的介質預設
#   2. Geometry 保存已由 SRC-PLAN §2.1 核定的瓶身幾何
#   3. ScenarioConfig 組合幾何、介質、光源、種子與輸出旗標
#   4. ScenarioConfig.from_mapping() 從 YAML 載入並區分 smoke 與 formal 模式
#   5. ScenarioConfig.scene_hash() 產生進 lock 的場景識別碼
# 維護提醒:
#   - 不得為 medium 的散射/吸收參數填入「看起來合理」的預設值；
#     SRC-SAI §9 的 scenario.yaml 草案把 bubble_density 標為
#     <validation-frozen range>，代表它必須由 validation 凍結後才有值。
#   - 不得在 formal 模式允許 placeholder 介質參數；smoke 模式才可以，
#     且產出的 artifact 必須帶 placeholder 標記。
#   - 不得把 scenario_id 編入軟體版本；版本放 manifest（NOTE-009 同理）。
#   - v0.1.0 新增：首版場景契約。
# 驗證方式:
#   - py -3.10 -m pytest tests/simulation/test_simulation.py -k "scenario or geometry or placeholder or scene_hash" -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pcmef.core.constants import CLASS_ORDER
from pcmef.core.hash import hash_object

__all__ = [
    "ScenarioConfigError",
    "MediumPreset",
    "Geometry",
    "Lighting",
    "ScenarioConfig",
]


class ScenarioConfigError(ValueError):
    """場景設定缺欄位、數值非法，或在 formal 模式使用 placeholder。"""


class MediumPreset(str, Enum):
    """四類液態狀態對應的介質預設名稱。

    這裡只是「名稱」，不含任何散射係數 —— 具體物理參數必須由校準決定。
    """

    EMPTY = "empty"
    WATER = "water"
    BUBBLY = "bubbly"
    MISTY = "misty"

    @classmethod
    def for_class(cls, class_label: str) -> "MediumPreset":
        mapping = {
            "Empty": cls.EMPTY,
            "Water-filled": cls.WATER,
            "Bubbly": cls.BUBBLY,
            "Misty": cls.MISTY,
        }
        if class_label not in mapping:
            raise ScenarioConfigError(
                f"unknown class_label {class_label!r}; expected one of {CLASS_ORDER}"
            )
        return mapping[class_label]


@dataclass(frozen=True)
class Geometry:
    """瓶身與感測器的幾何配置。

    預設值取自 SRC-PLAN §2.1 的前研究錨點（感測器距瓶身 5 cm、瓶徑 5.7 cm、
    壁厚 0.2 cm），這些是已記載且已由真實資料佐證的數值，不是猜測。
    """

    sensor_to_bottle_mm: float = 50.0
    bottle_diameter_mm: float = 57.0
    wall_thickness_mm: float = 2.0
    lateral_offset_mm: float = 0.0

    def __post_init__(self) -> None:
        for name in ("sensor_to_bottle_mm", "bottle_diameter_mm", "wall_thickness_mm"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ScenarioConfigError(f"geometry.{name} must be positive, got {value!r}")
        if self.wall_thickness_mm * 2 >= self.bottle_diameter_mm:
            raise ScenarioConfigError(
                f"wall thickness {self.wall_thickness_mm} mm leaves no interior in a "
                f"{self.bottle_diameter_mm} mm bottle"
            )

    @property
    def inner_radius_mm(self) -> float:
        return self.bottle_diameter_mm / 2.0 - self.wall_thickness_mm

    @property
    def outer_radius_mm(self) -> float:
        return self.bottle_diameter_mm / 2.0


@dataclass(frozen=True)
class Lighting:
    """光源配置。preset 名稱對應受控室內光的幾種設定。"""

    preset: str = "nominal"
    irradiance: float = 1.0

    def __post_init__(self) -> None:
        if not self.preset:
            raise ScenarioConfigError("lighting.preset is required")
        if not isinstance(self.irradiance, (int, float)) or self.irradiance <= 0:
            raise ScenarioConfigError(
                f"lighting.irradiance must be positive, got {self.irradiance!r}"
            )


@dataclass(frozen=True)
class ScenarioConfig:
    """單一模擬場景的完整輸入。"""

    class_label: str
    seed: int
    geometry: Geometry = field(default_factory=Geometry)
    lighting: Lighting = field(default_factory=Lighting)
    medium_parameters: dict[str, Any] = field(default_factory=dict)
    render_rgb: bool = True
    render_transient: bool = True
    spp: int = 16
    resolution: tuple[int, int] = (128, 128)
    formal: bool = False

    def __post_init__(self) -> None:
        if self.class_label not in CLASS_ORDER:
            raise ScenarioConfigError(
                f"unknown class_label {self.class_label!r}; expected one of {CLASS_ORDER}"
            )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise ScenarioConfigError(f"seed must be a non-negative int, got {self.seed!r}")
        if self.spp <= 0:
            raise ScenarioConfigError(f"spp must be positive, got {self.spp!r}")
        if len(self.resolution) != 2 or any(v <= 0 for v in self.resolution):
            raise ScenarioConfigError(
                f"resolution must be two positive ints, got {self.resolution!r}"
            )
        if not (self.render_rgb or self.render_transient):
            raise ScenarioConfigError("scenario must request at least one output")
        if self.formal and self.uses_placeholder_medium():
            raise ScenarioConfigError(
                "formal mode forbids placeholder medium parameters; the scattering "
                "parameters must come from a frozen calibration, not from a default. "
                f"placeholder keys: {sorted(self.placeholder_keys())}"
            )

    @property
    def medium_preset(self) -> MediumPreset:
        return MediumPreset.for_class(self.class_label)

    # -- placeholder 判定 ---------------------------------------------------

    def placeholder_keys(self) -> set[str]:
        """回傳被顯式標記為 placeholder 的介質參數名稱。

        標記方式是值為 dict 且含 placeholder: true。刻意要求顯式標記而不是
        「沒填就算 placeholder」—— 後者會讓遺漏欄位與暫用值長得一樣。
        """
        return {
            key
            for key, value in self.medium_parameters.items()
            if isinstance(value, dict) and value.get("placeholder") is True
        }

    def uses_placeholder_medium(self) -> bool:
        return bool(self.placeholder_keys())

    def medium_value(self, key: str) -> Any:
        """取得介質參數的實際數值。placeholder 會回傳其 value 欄位。"""
        if key not in self.medium_parameters:
            raise ScenarioConfigError(
                f"medium parameter {key!r} is not defined for class {self.class_label!r}"
            )
        value = self.medium_parameters[key]
        if isinstance(value, dict):
            if "value" not in value:
                raise ScenarioConfigError(
                    f"medium parameter {key!r} is a mapping without a 'value' field"
                )
            return value["value"]
        return value

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_label": self.class_label,
            "medium_preset": self.medium_preset.value,
            "seed": self.seed,
            "geometry": {
                "sensor_to_bottle_mm": self.geometry.sensor_to_bottle_mm,
                "bottle_diameter_mm": self.geometry.bottle_diameter_mm,
                "wall_thickness_mm": self.geometry.wall_thickness_mm,
                "lateral_offset_mm": self.geometry.lateral_offset_mm,
            },
            "lighting": {
                "preset": self.lighting.preset,
                "irradiance": self.lighting.irradiance,
            },
            "medium_parameters": self.medium_parameters,
            "outputs": {"rgb": self.render_rgb, "transient": self.render_transient},
            "spp": self.spp,
            "resolution": list(self.resolution),
        }

    def scene_hash(self) -> str:
        """場景的 canonical hash，供 initial_simulation.lock 引用。

        刻意不涵蓋 formal 旗標：同一個場景在 smoke 與 formal 模式下
        應該是同一個場景，模式差異由 manifest 另外記錄。
        """
        return hash_object(self.to_dict())

    # -- 載入 ---------------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: dict[str, Any], formal: bool = False) -> "ScenarioConfig":
        if not isinstance(data, dict):
            raise ScenarioConfigError("scenario config must be a mapping")
        scenario = data.get("scenario", data)

        geometry_raw = scenario.get("geometry", {}) or {}
        lighting_raw = scenario.get("lighting", {}) or {}
        outputs = scenario.get("outputs", {}) or {}

        for key in ("class_label", "seed"):
            if key not in scenario:
                raise ScenarioConfigError(f"scenario.{key} is required")

        return cls(
            class_label=str(scenario["class_label"]),
            seed=int(scenario["seed"]),
            geometry=Geometry(**geometry_raw) if geometry_raw else Geometry(),
            lighting=Lighting(**lighting_raw) if lighting_raw else Lighting(),
            medium_parameters=dict(scenario.get("medium", {}) or {}),
            render_rgb=bool(outputs.get("rgb", True)),
            render_transient=bool(outputs.get("transient", True)),
            spp=int(scenario.get("spp", 16)),
            resolution=tuple(scenario.get("resolution", (128, 128))),
            formal=formal,
        )
