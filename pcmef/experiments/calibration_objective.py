# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.experiments.calibration_identity 驗過的凍結身分、
#         經 calibration_first_access 載入的 calibration partition 數值，
#         以及 mitsuba/mitransient 的算圖結果；由 calibration_formal 的
#         每一次評估呼叫；不寫出任何 artifact。
# 檔案路徑: pcmef/experiments/calibration_objective.py
# 產生時間: 2026-08-30 19:20 +08:00
# 版本: v0.1.0
# 功能說明: 算出 Formal Calibration 的目標函數 —— 給定一組參數，重建場景、
#           算圖、過 surrogate 產生四類 recording，再對 16 個
#           (class, feature) 格子各取一次 W1 並除以凍結的 s_f。
# 模組定位: calibration 的目標函數層。它「不是」決策層 —— 收斂、平手、
#           regression guard 全部在 calibration_formal；本檔只把參數變成數字，
#           且那個數字必須是參數的**決定性**函數，否則 optimizer 追的是雜訊。
# 主要責任:
#   1. load_real_calibration() 合法重讀 calibration partition 並斷言 raw hash
#   2. scene_overrides() 以 context manager 覆寫場景常數並保證還原
#   3. RenderCache 以涵蓋全部科學輸入的鍵重用 transient cube
#   4. Simulator.recordings() 產生四類的 (500, 4) recording
#   5. objective_terms() 產生 16 項 NW 與 16 項 raw W1
# 維護提醒:
#   - 不得在 cache 鍵裡省略任何會改變結果的輸入。少一項就會有兩組不同的
#     科學輸入共用一個結果，而那種錯不會崩潰，只會安靜地給出錯的最小值。
#   - 不得為了省時間降低 spp / resolution / temporal_bins，也不得改變
#     RNG 的抽取順序；那是拿實驗解析度去遷就時程（CAL-PREREG-003
#     runtime_cost_model.forbidden_adjustment）。
#   - 不得在目標函數裡寫入任何真實 class mean 或逐類目標值；
#     real 分佈一律由執行當下載入的 partition 產生。
#   - 不得在讀值之前略過帳本。帳本是「第幾次 access」的唯一憑據，
#     先讀後記等於那份憑據是事後補的。
#   - 不得對非有限結果回傳一個大數字；一律 +inf 並完整記錄該次參數。
#   - v0.1.0 新增：首版 formal calibration 目標函數（CAL-PREREG-003）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_objective.py -v
# ------------------------------------------------------------

from __future__ import annotations

import math
from contextlib import contextmanager
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.core.hash import hash_file, hash_object
from pcmef.experiments.calibration_identity import (
    EXPECTED_RAW_CALIBRATION_DATA_HASH,
    FrozenIdentity,
    IdentityError,
    all_cells,
)

__all__ = [
    "CRN_SEEDS",
    "VERIFICATION_SEEDS",
    "MEDIUM_KEY_BY_CLASS",
    "SCENE_CONSTANT_BY_DIMENSION",
    "SURROGATE_DIMENSIONS",
    "RealCalibration",
    "load_real_calibration",
    "scene_overrides",
    "RenderCache",
    "Simulator",
    "objective_terms",
]


#: CAL-PREREG-003 optimizer.common_random_numbers。同一階段內所有評估共用。
CRN_SEEDS: dict[str, int] = {
    "Empty": 1001,
    "Water-filled": 1002,
    "Bubbly": 1042,
    "Misty": 1004,
}

#: CAL-PREREG-003 optimizer.verification_seeds。每階段**只用一次**。
VERIFICATION_SEEDS: dict[str, int] = {
    "Empty": 2001,
    "Water-filled": 2002,
    "Bubbly": 2042,
    "Misty": 2004,
}

MEDIUM_KEY_BY_CLASS: dict[str, str | None] = {
    "Empty": None,
    "Water-filled": "turbidity",
    "Bubbly": "bubble_density",
    "Misty": "mist_density",
}

#: optimizer 維度 -> mitsuba_adapter 模組常數。與 stage 0 的 DIMENSION_BINDINGS
#: 同一份事實，在此只留 formal calibration 會動到的那些。
SCENE_CONSTANT_BY_DIMENSION: dict[str, str] = {
    "sensor.fov_deg": "_SENSOR_FOV_DEG",
    "_FOIL_GAP_TO_BOTTLE_RATIO": "_FOIL_GAP_TO_BOTTLE_RATIO",
    "_FOIL_REFLECTANCE_940NM": "_FOIL_REFLECTANCE_940NM",
    "_FOIL_SURFACE_ALPHA": "_FOIL_SURFACE_ALPHA",
    "_FOIL_SIZE_TO_DIAMETER_RATIO": "_FOIL_SIZE_TO_DIAMETER_RATIO",
}

#: 作用在算圖**之後**的維度。它們不進場景，因此可以重用同一批 cube ——
#: 這是純粹的結構事實（SurrogateCalibration 進不了 build_scene_dict），
#: 不是效能猜測。
SURROGATE_DIMENSIONS: frozenset[str] = frozenset(
    {
        "ambient_energy_to_mcps",
        "ambient_jitter_relative",
        "signal_energy_to_mcps",
        "noise_relative_sigma",
        "sigma_width_to_mm",
        "sigma_snr_weight",
        "sigma_multipath_weight",
    }
)

#: 取樣間隔取自已凍結 lock 的 initial 值，不是全域常數（NOTE-011）。
FROZEN_SAMPLE_INTERVAL_SOURCE = "initial_simulation.lock:initial_parameter_values"

#: 場景常數的完整快照清單。cache 鍵必須涵蓋**全部**，不只被擬合的那些 ——
#: 少記一項就等於宣稱它永遠不會變，而那是一句沒有人會去驗證的話。
_SCENE_CONSTANT_NAMES: tuple[str, ...] = (
    "_FOIL_GAP_TO_BOTTLE_RATIO",
    "_FOIL_REFLECTANCE_940NM",
    "_FOIL_SIZE_TO_DIAMETER_RATIO",
    "_FOIL_SURFACE_ALPHA",
    "_FOIL_ORIENTATION",
    "_BOTTLE_SURFACE_ALPHA",
    "_SENSOR_FOV_DEG",
    "_LIGHT_CUTOFF_ANGLE_DEG",
    "_ROOM_LIGHT_RADIANCE",
    "_SIGMA_T_REFERENCE_PER_M",
    "_ALBEDO_BY_PRESET",
    "_INTERIOR_BASE_IOR",
    "_DENSITY_KEY_BY_PRESET",
)


# ---------------------------------------------------------------------------
# 真實側
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RealCalibration:
    """calibration partition 的真實值，逐 class x feature 攤平。

    `values[class][feature]` 是該類 98 筆 recording 在該特徵上的全部取樣點。
    攤平只發生在**度量**這一步：recording 身分在載入時完整保留，
    `recordings_per_class` 記錄它，統計單位仍是 recording/scenario。
    """

    values: dict[str, dict[str, np.ndarray]]
    raw_hash: str
    recordings_per_class: dict[str, int]
    ledger_entry: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "raw_calibration_data_hash": self.raw_hash,
            "recordings_per_class": dict(self.recordings_per_class),
            "recordings_total": sum(self.recordings_per_class.values()),
            "statistical_unit": "recording",
            "values_per_cell": {
                f"{c}|{f}": int(self.values[c][f].size)
                for c, f in all_cells()
            },
            "ledger_entry": dict(self.ledger_entry),
        }


def load_real_calibration(
    identity: FrozenIdentity,
    purpose: str,
    code_version: str,
    source_root: str | Path = "data/raw_real/edge_impulse_export",
    repo_root: str | Path = ".",
) -> RealCalibration:
    """合法地（重）讀 calibration partition。

    這**是**一次 access，因此先記帳再讀值，且讀完必須算出 canonical raw hash
    並與凍結值逐字相符。CAL-SF-001 只凍了 pooled s_f，刻意不含逐類分佈，
    所以 optimizer 需要真實分佈時只能重讀 —— 重讀是合法的，
    假裝沒讀不是。
    """
    from pcmef.experiments.calibration_first_access import (
        append_ledger_entry,
        canonical_raw_data_hash,
        load_calibration_partition,
    )

    entry = append_ledger_entry(
        purpose=purpose,
        code_version=code_version,
        repo_root=repo_root,
        extra={
            "partition": "calibration",
            "recordings": 392,
            "reads_heldout": False,
            "stage0_hash": identity.stage0_hash,
            "preregistration_hash": identity.prereg_hash,
            "sf_hash": identity.sf_hash,
            "reason": (
                "CAL-SF-001 freezes pooled s_f only and intentionally carries no "
                "per-class distribution; the 16 W1 terms need the real per-class "
                "values, which are not resident in this process."
            ),
        },
    )

    by_class = load_calibration_partition(source_root, repo_root)
    raw_hash = canonical_raw_data_hash(by_class)
    if raw_hash != EXPECTED_RAW_CALIBRATION_DATA_HASH:
        raise IdentityError(
            "RAW_CALIBRATION_HASH_MISMATCH",
            f"the calibration partition hashes to {raw_hash}, but CAL-SF-001 froze "
            f"{EXPECTED_RAW_CALIBRATION_DATA_HASH}. The data under this run is not "
            "the data the frozen scale was computed from.",
        )

    values: dict[str, dict[str, np.ndarray]] = {}
    for class_label in CLASS_ORDER:
        blocks = by_class[class_label]
        stacked = np.concatenate(blocks, axis=0)
        values[class_label] = {
            feature: np.ascontiguousarray(stacked[:, index])
            for index, feature in enumerate(TOF_SCHEMA)
        }

    return RealCalibration(
        values=values,
        raw_hash=raw_hash,
        recordings_per_class={c: len(by_class[c]) for c in CLASS_ORDER},
        ledger_entry=entry,
    )


# ---------------------------------------------------------------------------
# 場景常數覆寫
# ---------------------------------------------------------------------------


@contextmanager
def scene_overrides(values: dict[str, float]) -> Iterator[None]:
    """暫時覆寫 mitsuba_adapter 的模組常數，離開時**一定**還原。

    以 context manager 而不是直接指派：一次評估拋出例外時若沒有還原，
    後面每一次評估都會在一個沒有人宣告過的場景上進行。
    """
    from pcmef.simulation import mitsuba_adapter as scene_module

    saved: dict[str, Any] = {}
    try:
        for dimension, value in values.items():
            attribute = SCENE_CONSTANT_BY_DIMENSION.get(dimension)
            if attribute is None:
                raise IdentityError(
                    "FROZEN_PROTOCOL_DRIFT",
                    f"{dimension!r} has no scene-constant binding; an unbound scene "
                    "dimension would be silently ignored while remaining a free "
                    "parameter in the optimizer",
                )
            saved[attribute] = getattr(scene_module, attribute)
            setattr(scene_module, attribute, float(value))
        yield
    finally:
        for attribute, previous in saved.items():
            setattr(scene_module, attribute, previous)


def scene_constant_snapshot() -> dict[str, Any]:
    """全部場景常數的當下取值，供 cache 鍵使用。"""
    from pcmef.simulation import mitsuba_adapter as scene_module

    out: dict[str, Any] = {}
    for name in _SCENE_CONSTANT_NAMES:
        value = getattr(scene_module, name)
        out[name] = dict(value) if isinstance(value, dict) else value
    return out


# ---------------------------------------------------------------------------
# 算圖快取
# ---------------------------------------------------------------------------


@dataclass
class RenderCache:
    """以涵蓋全部科學輸入的鍵重用 transient cube。

    只有在鍵完全相同時才回傳既有結果，而鍵包含場景常數快照、幾何、照明、
    介質、種子、spp、解析度、bin 數、bounce budget、variant、模擬器版本
    與兩個場景建構模組的檔案雜湊。相同鍵下重算會得到逐位元相同的 cube，
    因此重用是**數學上等價**而不是近似。

    容量上限只影響時間不影響結果：被逐出的項重算後仍然逐位元相同。
    """

    capacity: int = 8
    _entries: "OrderedDict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]" = field(
        default_factory=OrderedDict, repr=False
    )
    hits: int = 0
    misses: int = 0
    evictions: int = 0

    def get(self, key: str):
        if key in self._entries:
            self.hits += 1
            self._entries.move_to_end(key)
            return self._entries[key]
        self.misses += 1
        return None

    def put(self, key: str, value) -> None:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)
            self.evictions += 1

    def stats(self) -> dict[str, int]:
        total = self.hits + self.misses
        return {
            "render_cache_lookups": total,
            "render_cache_hits": self.hits,
            "render_cache_misses": self.misses,
            "render_cache_evictions": self.evictions,
            "render_cache_capacity": self.capacity,
            "renders_executed": self.misses,
        }


# ---------------------------------------------------------------------------
# 模擬
# ---------------------------------------------------------------------------


class SimulationFailure(RuntimeError):
    """單次評估的模擬失敗。呼叫端記錄它並回傳 +inf，不中止整個階段。"""


@dataclass
class Simulator:
    """把一組參數變成四類的 (500, 4) recording。

    `scene_values` 與 `surrogate_values` 分開，因為它們作用在算圖的兩側：
    前者改變被算的場景，後者只改變算完之後的映射。這個區別決定了
    cache 能不能命中，而它是結構事實，不是最佳化猜測。
    """

    identity: FrozenIdentity
    spp: int = 16
    resolution: tuple[int, int] = (64, 64)
    temporal_bins: int = 128
    n_samples: int = 500
    cache: RenderCache = field(default_factory=RenderCache)
    _adapter: Any = field(default=None, repr=False)
    _code_identity: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        from pcmef.simulation.mitransient_adapter import MiTransientAdapter

        self._adapter = MiTransientAdapter()
        self._code_identity = hash_object(
            {
                "mitsuba_adapter": hash_file("pcmef/simulation/mitsuba_adapter.py"),
                "mitransient_adapter": hash_file(
                    "pcmef/simulation/mitransient_adapter.py"
                ),
                "single_acquisition": hash_file("pcmef/surrogate/single_acquisition.py"),
                "temporal_model": hash_file("pcmef/surrogate/temporal_model.py"),
                "scenario": hash_file("pcmef/simulation/scenario.py"),
            }
        )

    # -- 設定 --------------------------------------------------------------

    @property
    def sample_interval_s(self) -> float:
        return float(self.identity.initial_values["sample_interval_s"])

    def _config_for(self, class_label: str, seed: int):
        from pcmef.simulation.scenario import Geometry, Lighting, ScenarioConfig

        iv = self.identity.initial_values
        key = MEDIUM_KEY_BY_CLASS[class_label]
        medium = (
            {}
            if key is None
            else {key: {"value": float(iv[f"medium.{key}"]), "placeholder": True}}
        )
        return ScenarioConfig(
            class_label=class_label,
            seed=int(seed),
            geometry=Geometry(
                sensor_to_bottle_mm=float(iv["geometry.sensor_to_bottle_mm"]),
                bottle_diameter_mm=float(iv["geometry.bottle_diameter_mm"]),
                wall_thickness_mm=float(iv["geometry.wall_thickness_mm"]),
                lateral_offset_mm=float(iv["geometry.lateral_offset_mm"]),
            ),
            lighting=Lighting(
                preset="nominal", irradiance=float(iv["lighting.irradiance"])
            ),
            medium_parameters=medium,
            spp=int(self.spp),
            resolution=tuple(self.resolution),
        )

    def _render_key(self, class_label: str, seed: int) -> str:
        """涵蓋每一個會改變 cube 的科學輸入。"""
        import mitsuba as mi
        import mitransient

        iv = self.identity.initial_values
        return hash_object(
            {
                "class_label": class_label,
                "seed": int(seed),
                "spp": int(self.spp),
                "resolution": list(self.resolution),
                "temporal_bins": int(self.temporal_bins),
                "max_depth": int(iv["max_depth"]),
                "bounce_budget": float(self._adapter.bounce_budget),
                "leading_margin_bins": int(iv["_LEADING_MARGIN_BINS"]),
                "variant": self._adapter.variant,
                "geometry": {
                    k: float(iv[k])
                    for k in sorted(iv)
                    if k.startswith("geometry.")
                },
                "lighting_irradiance": float(iv["lighting.irradiance"]),
                "medium": {
                    k: float(iv[k]) for k in sorted(iv) if k.startswith("medium.")
                },
                "scene_constants": scene_constant_snapshot(),
                "mitsuba": mi.__version__,
                "mitransient": getattr(mitransient, "__version__", "unknown"),
                "code_identity": self._code_identity,
                "initial_lock_hash": self.identity.initial_lock_hash,
            }
        )

    def _render(self, class_label: str, seed: int):
        import mitsuba as mi

        from pcmef.simulation.mitransient_adapter import build_transient_scene_dict
        from pcmef.simulation.mitsuba_adapter import Illumination

        key = self._render_key(class_label, seed)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        config = self._config_for(class_label, seed)
        start, width = self._adapter.default_binning(config, self.temporal_bins)
        cubes = []
        for illumination in (Illumination.ACTIVE_ONLY, Illumination.AMBIENT_ONLY):
            scene_dict = build_transient_scene_dict(
                mi,
                config,
                self.temporal_bins,
                start,
                width,
                max_depth=int(self.identity.initial_values["max_depth"]),
                illumination=illumination,
            )
            scene = mi.load_dict(scene_dict)
            mi.render(scene, spp=int(config.spp), seed=int(config.seed))
            cube = np.array(scene.sensors()[0].film().develop_transient_())
            if not np.all(np.isfinite(cube)):
                raise SimulationFailure(
                    f"{class_label} produced a non-finite transient under "
                    f"{illumination}; the scene is already unphysical at this point"
                )
            cubes.append(cube)
        axis = (
            start + (np.arange(self.temporal_bins) + 0.5) * width
        ) / 299792458.0
        result = (cubes[0], cubes[1], axis)
        self.cache.put(key, result)
        return result

    def _surrogate(self, surrogate_values: dict[str, float]):
        from dataclasses import replace

        from pcmef.surrogate.calibration import (
            PLACEHOLDER_SMOKE_CALIBRATION,
            CalibratedScale,
        )
        from pcmef.surrogate.single_acquisition import SensorSurrogate

        calibration = PLACEHOLDER_SMOKE_CALIBRATION
        if surrogate_values:
            calibration = replace(
                calibration,
                **{
                    name: CalibratedScale(
                        value=float(value),
                        placeholder=True,
                        source="CAL-PREREG-003 formal calibration candidate",
                    )
                    for name, value in surrogate_values.items()
                },
            )
        return SensorSurrogate(calibration)

    def recordings(
        self,
        scene_values: dict[str, float],
        surrogate_values: dict[str, float],
        seeds: dict[str, int],
    ) -> dict[str, np.ndarray]:
        """四類各一筆 (n_samples, 4) recording。決定性函數。"""
        from pcmef.surrogate.temporal_model import TemporalModel

        surrogate = self._surrogate(surrogate_values)
        model = TemporalModel(surrogate)
        out: dict[str, np.ndarray] = {}
        with scene_overrides(scene_values):
            for class_label in CLASS_ORDER:
                seed = int(seeds[class_label])
                active, ambient, axis = self._render(class_label, seed)
                recording = model.generate_recording(
                    active,
                    axis,
                    sample_interval_s=self.sample_interval_s,
                    sample_interval_source=FROZEN_SAMPLE_INTERVAL_SOURCE,
                    seed=seed,
                    n_samples=int(self.n_samples),
                    ambient_transient=ambient,
                )
                out[class_label] = recording.values
        return out


# ---------------------------------------------------------------------------
# 16 項
# ---------------------------------------------------------------------------


def objective_terms(
    simulated: dict[str, np.ndarray],
    real: RealCalibration,
    s_f: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """回傳 (NW 16 項, raw W1 16 項)，鍵一律為 "class|feature"。

    raw W1 帶物理單位、是 primary evidence；NW 無單位、才可以跨特徵相加。
    兩者都保留，因為只留 NW 就沒有人看得出「差了幾 mm」。
    """
    from pcmef.stats.metrics import normalized_wasserstein, wasserstein_w1

    nw: dict[str, float] = {}
    raw: dict[str, float] = {}
    for class_label, feature in all_cells():
        index = TOF_SCHEMA.index(feature)
        simulated_values = simulated[class_label][:, index]
        w1 = wasserstein_w1(real.values[class_label][feature], simulated_values)
        key = f"{class_label}|{feature}"
        raw[key] = w1
        nw[key] = normalized_wasserstein(w1, s_f[feature])
    return nw, raw


def sum_cells(terms: dict[str, float], cells: list[tuple[str, str]]) -> float:
    """對指定格子取**等權**總和（CAL-PREREG-003 weights.scheme = UNIFORM）。

    權重是常數 1.0 且寫死在這裡，不是可傳入的參數：預註冊的
    maintenance_bound 明令「變更權重必須開 amendment，不得在執行期以參數傳入」。
    """
    total = 0.0
    for class_label, feature in cells:
        value = terms[f"{class_label}|{feature}"]
        if not math.isfinite(value):
            return math.inf
        total += 1.0 * value
    return total
