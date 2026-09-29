# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.platform.catalog 讀 manifest 與組出內建元件；讀
#         pcmef.core.constants 的 TOF_SCHEMA / TOF_RECORDING_SHAPE 描述碩論的
#         ToF。**不寫任何東西，也不被任何 stable module 匯入。**
# 檔案路徑: pcmef/platform/sensors.py
# 產生時間: 2026-09-27 01:38 +08:00（首次提交 d1cf266 的 commit 時間）
# 版本: v0.1.1
# 功能說明: Sensor Adapter 的契約（SAI §8.3、Appendix C）：身分、能力、實作
#           指向、參數、版本化且單位明確的觀測 schema、quality signal；以及
#           碩論 RGB 與 ToF 兩個內建 adapter 的描述。
# 模組定位: Phase 3 第一片。讓「感測器」成為平台上的具名元件，而不是畫面上
#           寫死的兩個選項；碩論的 RGB / ToF 只是**被描述**，實作一行都沒動。
# 主要責任:
#   1. SENSOR_FAMILIES / LAYOUTS：封閉的家族與版面列舉（含 custom）
#   2. ObservationSchema / Channel / QualitySignal：觀測與品質訊號的形狀
#   3. SensorAdapter + parse_sensor()：契約驗證，任何不合都 ContractViolation
#   4. thesis_sensors()：碩論 RGB 與 VL53L0X ToF 的內建描述，由常數推導
#   5. compare_observations()：同為 ToF 不等於等價（UAT-03）
# 維護提醒:
#   - **不得在內建描述裡複製碩論的數值。** ToF 的通道順序與形狀一律由
#     core.constants 推導；複製一份就是兩個真相來源，而漂移的那一份不會報錯。
#   - 不得宣告沒有實作的能力：simulate / ingest 為 True 就必須指到一個
#     `module:attribute`，為 False 就不得有。反過來，**程式碼存在也不等於
#     能力成立** —— 能力只由這裡的宣告決定。
#   - 不得把 sigma_like 描述成 VL53L0X 內部的 Sigma 暫存器值（NOTE-010）。
#   - 不得讓 quality signal 與預測機率同名或與觀測通道同名：§8.3 要求兩者
#     分開，混在一起之後下游分不出哪個是品質、哪個是判斷。
#   - 不得因為 family 相同就把兩個 adapter 當成等價；比較一律看觀測 schema。
#   - **不得放寬觀測 schema 的維度一致性。** image 是 H×W×C、sequence 是
#     T×C、vector 是 C，最後一維一律是寫明的通道數；compatibility matrix
#     依這份 metadata 判斷兩個 adapter 的觀測是否一致。
#   - v0.1.1 修正：layout 與 shape 的維度數必須一致、通道維度必須寫明、
#     sampling_interval_s 必須是有限值。對應 Phase 3 第二片。
#   - v0.1.0 新增：首版，對應 SAI Phase 3 第一片。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_extension_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from pcmef.core.constants import TOF_RECORDING_SHAPE, TOF_SCHEMA
from pcmef.platform.components import (
    KIND_SENSOR,
    NOT_ASSESSED,
    ComponentIdentity,
    ContractViolation,
    Fields,
    ParameterSpec,
    Provenance,
    check_entrypoint,
    content_sha256,
    parse_parameters,
    read_identity,
    valid_version,
)

__all__ = [
    "CONTRACT",
    "LAYOUTS",
    "SENSOR_FAMILIES",
    "Channel",
    "ObservationSchema",
    "QualitySignal",
    "SensorAdapter",
    "compare_observations",
    "parse_sensor",
    "thesis_sensors",
]

CONTRACT = "pcmef.sensor_adapter/v1"

#: §8.2 Step 1 的感測類型。封閉列舉：打錯的家族名稱不得變成一個新家族。
SENSOR_FAMILIES: tuple[str, ...] = (
    "rgb_camera", "time_of_flight", "infrared_camera", "thermal_camera",
    "spectral", "ultrasonic", "custom",
)

#: 觀測的版面。image：H×W×C；sequence：T×C；vector：C。
LAYOUTS: tuple[str, ...] = ("image", "sequence", "vector")

#: 每一種版面的維度數。**最後一維一律是通道數。**
_LAYOUT_RANK: dict[str, int] = {"image": 3, "sequence": 2, "vector": 1}

#: 預測輸出的名字。quality signal 不得用它們（§8.3：品質訊號必須與
#: predictive probability 分開）。
_PREDICTIVE_NAMES = frozenset({
    "probability", "probabilities", "prediction", "predicted_class",
    "logits", "confidence", "posterior",
})

_CAPABILITIES: tuple[str, ...] = ("simulate", "ingest")


@dataclass(frozen=True)
class Channel:
    name: str
    unit: str
    description: str

    def describe(self) -> dict[str, str]:
        return {"name": self.name, "unit": self.unit, "description": self.description}


@dataclass(frozen=True)
class ObservationSchema:
    """一次觀測長什麼樣子。**版本化、每個通道都有單位、時間軸要說清楚。**"""

    schema_id: str
    version: str
    layout: str
    shape: tuple[int | None, ...]
    channels: tuple[Channel, ...]
    sampling_interval_s: float | None
    timing_note: str

    def describe(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id, "version": self.version,
            "layout": self.layout, "shape": list(self.shape),
            "channels": [c.describe() for c in self.channels],
            "sampling_interval_s": self.sampling_interval_s,
            "timing_note": self.timing_note,
        }


@dataclass(frozen=True)
class QualitySignal:
    name: str
    unit: str
    description: str

    def describe(self) -> dict[str, str]:
        return {"name": self.name, "unit": self.unit, "description": self.description}


@dataclass(frozen=True)
class SensorAdapter:
    """一個已通過契約驗證的 Sensor Adapter 描述。"""

    identity: ComponentIdentity
    display_name: str
    family: str
    device_model: str
    parameters: tuple[ParameterSpec, ...]
    simulate: str | None
    ingest: str | None
    observation: ObservationSchema
    quality_signals: tuple[QualitySignal, ...]
    dependencies: tuple[str, ...]
    known_limitations: tuple[str, ...]
    tests: tuple[str, ...]
    provenance: Provenance

    @property
    def capabilities(self) -> dict[str, bool]:
        return {"simulate": self.simulate is not None, "ingest": self.ingest is not None}

    def implementations(self) -> dict[str, str | None]:
        return {"simulate": self.simulate, "ingest": self.ingest}

    def describe(self) -> dict[str, Any]:
        return {
            **self.identity.describe(),
            "contract": CONTRACT,
            "display_name": self.display_name,
            "family": self.family,
            "device_model": self.device_model,
            "capabilities": self.capabilities,
            "implementations": self.implementations(),
            "parameters": [p.describe() for p in self.parameters],
            "observation": self.observation.describe(),
            "quality_signals": [q.describe() for q in self.quality_signals],
            "dependencies": list(self.dependencies),
            "known_limitations": list(self.known_limitations),
            "tests": list(self.tests),
            "provenance": self.provenance.describe(),
            "compatibility": dict(NOT_ASSESSED),
        }


def _parse_observation(data: Any, problems: list[str]) -> ObservationSchema | None:
    before = len(problems)
    fields = Fields(data, "observation", problems)
    schema_id = fields.text("schema_id")
    version = fields.text("version")
    layout = fields.text("layout")
    shape = fields.take("shape", (list,), default=[])
    channels_raw = fields.take("channels", (list,), default=[])
    interval = fields.take("sampling_interval_s", (int, float, type(None)))
    timing_note = fields.take("timing_note", (str,), default="")
    fields.done()

    if version and not valid_version(version):
        problems.append(f"observation.version {version!r} must be MAJOR.MINOR.PATCH")
    if layout and layout not in LAYOUTS:
        problems.append(f"observation.layout {layout!r} is not one of {list(LAYOUTS)}")
    if not shape or not all(
        d is None or (isinstance(d, int) and not isinstance(d, bool) and d > 0)
        for d in shape
    ):
        problems.append(
            "observation.shape must be a non-empty list of positive integers or "
            "null (a free dimension)"
        )
    channels = []
    for index, raw in enumerate(channels_raw):
        at = f"observation.channels[{index}]"
        channel = Fields(raw, at, problems)
        name = channel.text("name")
        unit = channel.text("unit")
        description = channel.take("description", (str,), required=False, default="")
        channel.done()
        channels.append(Channel(name, unit, description))
    if not channels:
        problems.append("observation.channels must name at least one channel")
    names = [c.name for c in channels]
    if len(set(names)) != len(names):
        problems.append(f"observation.channels repeat a name: {names}")
    if layout in _LAYOUT_RANK and shape and len(shape) != _LAYOUT_RANK[layout]:
        problems.append(
            f"observation.layout {layout!r} needs a {_LAYOUT_RANK[layout]}-dimensional "
            f"shape (the last dimension is the channel count), got {len(shape)}"
        )
    if shape and shape[-1] is None:
        # 通道數是這份 schema 自己知道的事；寫成 null 等於說「不知道有幾個
        # 通道」，而 channels 清單明明列出來了。
        problems.append(
            "observation.shape leaves the channel dimension free; the last "
            "dimension must state the channel count"
        )
    if shape and isinstance(shape[-1], int) and shape[-1] != len(channels):
        problems.append(
            f"observation.shape ends in {shape[-1]} but {len(channels)} channel(s) "
            "are declared; the schema contradicts itself"
        )
    if interval is not None and not math.isfinite(interval):
        # JSON 的 1e999 會被讀成 inf，不經過 NaN / Infinity 的檢查。
        problems.append("observation.sampling_interval_s must be a finite number")
    elif interval is not None and interval <= 0:
        problems.append("observation.sampling_interval_s must be positive or null")
    if interval is None and not timing_note:
        problems.append(
            "observation has no sampling_interval_s, so timing_note must say why "
            "(timing must be explicit or explained, never silently absent)"
        )
    if len(problems) > before:
        return None
    return ObservationSchema(schema_id, version, layout, tuple(shape), tuple(channels),
                             None if interval is None else float(interval), timing_note)


def parse_sensor(data: Mapping[str, Any], *, source: str, location: str,
                 builtin: bool = False) -> SensorAdapter:
    """驗證一份 Sensor Adapter 定義。不合契約就 ContractViolation，列出全部問題。"""
    problems: list[str] = []
    fields = Fields(data, "adapter", problems)
    contract = fields.text("contract")
    if contract and contract != CONTRACT:
        problems.append(f"contract {contract!r} is not {CONTRACT!r}")
    identity = read_identity(fields, KIND_SENSOR, builtin=builtin)
    display_name = fields.text("display_name")
    family = fields.text("family")
    device_model = fields.text("device_model")
    parameters = parse_parameters(fields.take("parameters", (dict,), default={}),
                                  "parameters", problems)
    capabilities = Fields(fields.take("capabilities", (dict,), default={}),
                          "capabilities", problems)
    claimed = {name: capabilities.take(name, (bool,), default=False)
               for name in _CAPABILITIES}
    capabilities.done()
    implementations = Fields(fields.take("implementations", (dict,), default={}),
                             "implementations", problems)
    pointers = {name: implementations.take(name, (str, type(None)), default=None)
                for name in _CAPABILITIES}
    implementations.done()
    observation = _parse_observation(fields.take("observation", (dict,), default={}),
                                     problems)
    signals_raw = fields.take("quality_signals", (list,), default=[])
    dependencies = fields.strings("dependencies")
    known_limitations = fields.strings("known_limitations")
    tests = fields.strings("tests", required=False)
    fields.done()

    if family and family not in SENSOR_FAMILIES:
        problems.append(f"family {family!r} is not one of {list(SENSOR_FAMILIES)}")
    if not any(claimed.values()):
        problems.append(
            "an adapter must be able to simulate or ingest (SAI §8.3); "
            "both capabilities are false"
        )
    for name in _CAPABILITIES:
        check_entrypoint(pointers[name], f"implementations.{name}", problems)
        if claimed[name] and pointers[name] is None:
            problems.append(
                f"capability {name!r} is claimed but implementations.{name} "
                "names no implementation"
            )
        if not claimed[name] and pointers[name] is not None:
            problems.append(
                f"implementations.{name} names code but capability {name!r} is "
                "false; code does not imply a capability"
            )
    signals = []
    channel_names = {c.name for c in observation.channels} if observation else set()
    for index, raw in enumerate(signals_raw):
        at = f"quality_signals[{index}]"
        signal = Fields(raw, at, problems)
        name = signal.text("name")
        unit = signal.text("unit")
        description = signal.take("description", (str,), required=False, default="")
        signal.done()
        if name.lower() in _PREDICTIVE_NAMES:
            problems.append(
                f"{at} is named {name!r}, which is a predictive output; quality "
                "signals must stay separate from predictions (SAI §8.3)"
            )
        if name in channel_names:
            problems.append(f"{at} {name!r} is also an observation channel")
        signals.append(QualitySignal(name, unit, description))
    signal_names = [s.name for s in signals]
    if len(set(signal_names)) != len(signal_names):
        problems.append(f"quality_signals repeat a name: {signal_names}")

    if problems or observation is None:
        raise ContractViolation(location, problems)
    return SensorAdapter(
        identity=identity, display_name=display_name, family=family,
        device_model=device_model, parameters=parameters,
        simulate=pointers["simulate"], ingest=pointers["ingest"],
        observation=observation, quality_signals=tuple(signals),
        dependencies=dependencies, known_limitations=known_limitations,
        tests=tests,
        provenance=Provenance(source, location, content_sha256(data)),
    )


# ---------------------------------------------------------------------------
# 碩論的兩個感測器：被描述，不被改寫
# ---------------------------------------------------------------------------

#: ToF 四通道的單位。鍵必須與 TOF_SCHEMA 完全一致 —— 少一個就在建立時
#: KeyError，而不是默默少一個通道。
_TOF_CHANNEL_UNITS: dict[str, tuple[str, str]] = {
    "distance_mm": ("mm", "measured range"),
    "ambient_rate_mcps": ("Mcps", "ambient photon count rate"),
    "signal_rate_mcps": ("Mcps", "return signal count rate"),
    "sigma_like": (
        "mm",
        "surrogate-built ranging uncertainty; NOT the VL53L0X internal Sigma "
        "register value (NOTE-010)",
    ),
}

#: 兩者的模擬都走同一個成對生成器：RGB 與 ToF 來自同一份場景設定。
_PAIRED_GENERATOR = "pcmef.simulation.paired:generate_paired_sample"

_THESIS_AUTHOR = "PC-MEF thesis (frozen semantics)"
_BUILTIN_LOCATION = "pcmef.platform.sensors:thesis_sensors"


def _thesis_definitions() -> tuple[dict[str, Any], dict[str, Any]]:
    rgb = {
        "contract": CONTRACT,
        "id": "pcmef.sensor.rgb-camera",
        "version": "1.0.0",
        "author": _THESIS_AUTHOR,
        "display_name": "RGB Camera",
        "family": "rgb_camera",
        "device_model": "rendered camera (Mitsuba 3)",
        "parameters": {},
        "capabilities": {"simulate": True, "ingest": False},
        "implementations": {"simulate": _PAIRED_GENERATOR, "ingest": None},
        "observation": {
            "schema_id": "pcmef.rgb.image",
            "version": "1.0.0",
            "layout": "image",
            "shape": [None, None, 3],
            "channels": [
                {"name": channel, "unit": "relative_linear_radiance",
                 "description": "linear EXR output; the PNG written beside it "
                                "is an 8-bit sRGB preview"}
                for channel in ("r", "g", "b")
            ],
            "sampling_interval_s": None,
            "timing_note": "one rendered frame per scenario; no time axis",
        },
        "quality_signals": [],
        "dependencies": ["mitsuba"],
        "known_limitations": [
            "resolution and sample count are owned by the thesis ScenarioConfig "
            "and console presets and are not re-declared here",
            "no real-data ingestion path exists for RGB",
        ],
    }
    tof = {
        "contract": CONTRACT,
        "id": "pcmef.sensor.tof-vl53l0x",
        "version": "1.0.0",
        "author": _THESIS_AUTHOR,
        "display_name": "Time-of-Flight (VL53L0X)",
        "family": "time_of_flight",
        "device_model": "VL53L0X",
        "parameters": {},
        "capabilities": {"simulate": True, "ingest": True},
        "implementations": {
            "simulate": _PAIRED_GENERATOR,
            # 目前唯一能滿足 (500, 4) 契約的真實資料來源。legacy_csv 與
            # legacy_kg 讀的是別的形狀，不是這個觀測 schema。
            "ingest": "pcmef.adapters.edge_impulse:EdgeImpulseAdapter",
        },
        "observation": {
            "schema_id": "pcmef.tof.recording",
            "version": "1.0.0",
            "layout": "sequence",
            "shape": list(TOF_RECORDING_SHAPE),
            "channels": [
                {"name": name, "unit": _TOF_CHANNEL_UNITS[name][0],
                 "description": _TOF_CHANNEL_UNITS[name][1]}
                for name in TOF_SCHEMA
            ],
            "sampling_interval_s": None,
            "timing_note": (
                "each real recording carries its own interval (Edge Impulse "
                "export); the nominal rate differs between sources — see "
                "`pcmef provenance audit-timing`"
            ),
        },
        "quality_signals": [],
        "dependencies": ["mitsuba", "mitransient"],
        "known_limitations": [
            "channel order is TOF_SCHEMA and is frozen by the thesis",
            "legacy CSV and KG readers do not produce this observation schema",
        ],
    }
    return rgb, tof


def thesis_sensors() -> tuple[SensorAdapter, ...]:
    """碩論的 RGB 與 ToF。每次呼叫都重新驗證，**沒有模組層級的快取或註冊**。"""
    return tuple(
        parse_sensor(definition, source="builtin", location=_BUILTIN_LOCATION,
                     builtin=True)
        for definition in _thesis_definitions()
    )


def compare_observations(a: SensorAdapter, b: SensorAdapter) -> tuple[str, ...]:
    """兩個 adapter 的觀測差在哪裡。空的才代表觀測形狀一致。

    UAT-03：「不把同為 ToF 當作自動等價」。family 相同只是一個分類，
    能不能沿用下游的映射，取決於通道、單位、形狀與時間軸。
    """
    oa, ob = a.observation, b.observation
    differences = []
    if a.family != b.family:
        differences.append(f"family: {a.family} vs {b.family}")
    if (oa.schema_id, oa.version) != (ob.schema_id, ob.version):
        differences.append(
            f"observation schema: {oa.schema_id} {oa.version} vs "
            f"{ob.schema_id} {ob.version}"
        )
    if oa.layout != ob.layout:
        differences.append(f"layout: {oa.layout} vs {ob.layout}")
    if oa.shape != ob.shape:
        differences.append(f"shape: {list(oa.shape)} vs {list(ob.shape)}")
    names_a = [c.name for c in oa.channels]
    names_b = [c.name for c in ob.channels]
    if names_a != names_b:
        differences.append(f"channels: {names_a} vs {names_b}")
    units_b = {c.name: c.unit for c in ob.channels}
    for channel in oa.channels:
        if channel.name in units_b and units_b[channel.name] != channel.unit:
            differences.append(
                f"unit of {channel.name}: {channel.unit} vs {units_b[channel.name]}"
            )
    if oa.sampling_interval_s != ob.sampling_interval_s:
        differences.append(
            f"sampling interval: {oa.sampling_interval_s} vs {ob.sampling_interval_s}"
        )
    return tuple(differences)
