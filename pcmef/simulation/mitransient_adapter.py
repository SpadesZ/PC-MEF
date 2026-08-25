# PC-MEF Research System source maintenance contract
# 上下游: 由 simulation.controller 呼叫；重用 mitsuba_adapter 的 scene dict，
#         只替換 integrator 與 film；寫出 transient.npy 與 transient_time.npy
#         到 artifact 目錄，後者即 CanonicalCase 的 optical_transient_time_axis。
# 檔案路徑: pcmef/simulation/mitransient_adapter.py
# 產生時間: 2026-08-26 07:20 +08:00
# 版本: v0.1.0
# 功能說明: 算出光在場景裡隨時間傳播的過程 —— 不是一張靜態影像，而是每個時間
#           切片各一張，合起來就是單次 acquisition 內部的光飛行歷程。
#           同時把 mitransient 用的光程長換算成秒，存成獨立的時間軸檔。
# 模組定位: mitransient 的封裝層。它產出的是 optical transient time 軸，
#           與 measurement time 是兩條不同的時間軸，**禁止互相換算**（SRC-D03）。
# 主要責任:
#   1. build_transient_scene_dict() 在共用場景上替換 transient integrator 與 film
#   2. MiTransientAdapter.render_transient() 算出 (H,W,bins,C) 張量並存檔
#   3. opl_to_seconds() 把光程長換算成飛行時間
#   4. TransientResult 保存 binning/time range/integrator 參數等 provenance
# 維護提醒:
#   - 不得把 transient bins 直接當成 500 點 measurement sequence；
#     那是 SRC-D03 明列的禁止做法，兩條時間軸物理意義不同。
#   - 不得在 NaN/Inf 出現時繼續；SRC-SAI §28 規定 mitransient 的非有限值
#     一律 FAIL_FAST，因為它代表積分器參數已經不合理。
#   - 不得沿用 cornell_box 的 start_opl=3.5 / bin_width=0.02；那是房間尺度的
#     設定，本場景只有 5 公分，時間軸會整段落在有效範圍之外。
#   - v0.1.0 新增：首版 transient smoke adapter。
# 驗證方式:
#   - py -3.10 -m pytest tests/simulation/test_simulation.py -k "transient or reproducible" -v
# ------------------------------------------------------------

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from pcmef.simulation.mitsuba_adapter import (
    DEFAULT_VARIANT,
    SimulationDependencyError,
    build_scene_dict,
    require_mitsuba,
)
from pcmef.simulation.scenario import ScenarioConfig

__all__ = [
    "SPEED_OF_LIGHT_M_PER_S",
    "TransientResult",
    "opl_to_seconds",
    "build_transient_scene_dict",
    "MiTransientAdapter",
]

# 真空光速。mitransient 以光程長（公尺）參數化時間軸，換算需要它。
SPEED_OF_LIGHT_M_PER_S: float = 299_792_458.0

_MM_PER_M = 1000.0


@dataclass(frozen=True)
class TransientResult:
    """一次 transient 算圖的產出與 provenance。"""

    scenario_hash: str
    variant: str
    mitsuba_version: str
    mitransient_version: str
    seed: int
    spp: int
    temporal_bins: int
    start_opl_m: float
    bin_width_opl_m: float
    shape: tuple[int, ...]
    runtime_s: float
    output_paths: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def bin_width_s(self) -> float:
        return opl_to_seconds(self.bin_width_opl_m)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_hash": self.scenario_hash,
            "variant": self.variant,
            "mitsuba_version": self.mitsuba_version,
            "mitransient_version": self.mitransient_version,
            "seed": self.seed,
            "spp": self.spp,
            "binning": {
                "temporal_bins": self.temporal_bins,
                "start_opl_m": self.start_opl_m,
                "bin_width_opl_m": self.bin_width_opl_m,
                "bin_width_s": self.bin_width_s,
                "total_span_s": self.bin_width_s * self.temporal_bins,
            },
            "shape": list(self.shape),
            "runtime_s": round(self.runtime_s, 4),
            "outputs": dict(self.output_paths),
            "time_axis": "optical_transient_time",
            **self.extra,
        }


def opl_to_seconds(optical_path_length_m: float) -> float:
    """把光程長（公尺）換算成飛行時間（秒）。"""
    return float(optical_path_length_m) / SPEED_OF_LIGHT_M_PER_S


def build_transient_scene_dict(
    mi,
    config: ScenarioConfig,
    temporal_bins: int,
    start_opl_m: float,
    bin_width_opl_m: float,
    max_depth: int = 12,
) -> dict[str, Any]:
    """在共用場景上替換成 transient integrator 與 film。

    刻意重用 build_scene_dict()：SRC-SAI FR-005 要求同一 scenario 產出的
    RGB 與 transient 必須來自同一個場景，各建一份場景會讓兩者悄悄分歧。
    """
    scene = build_scene_dict(mi, config)
    scene["integrator"] = {
        "type": "transient_path",
        "max_depth": int(max_depth),
        "camera_unwarp": False,
        "temporal_filter": "box",
    }
    scene["sensor"]["film"] = {
        "type": "transient_hdr_film",
        "width": int(config.resolution[0]),
        "height": int(config.resolution[1]),
        "rfilter": {"type": "box"},
        "temporal_bins": int(temporal_bins),
        "start_opl": float(start_opl_m),
        "bin_width_opl": float(bin_width_opl_m),
    }
    return scene


class MiTransientAdapter:
    """optical transient 算圖的封裝。"""

    def __init__(self, variant: str = DEFAULT_VARIANT) -> None:
        self.variant = variant

    def default_binning(
        self, config: ScenarioConfig, temporal_bins: int = 256
    ) -> tuple[float, float]:
        """依場景尺度推導 start_opl 與 bin_width_opl。

        cornell_box 的預設值是房間尺度（start_opl=3.5 m）；本場景只有 5 公分，
        直接沿用會讓整段時間軸落在光還沒抵達的區間，算出全零的 transient。
        因此以幾何推導：涵蓋相機到瓶身來回再加上瓶內多次散射的餘裕。
        """
        geometry = config.geometry
        sensor_distance_m = geometry.sensor_to_bottle_mm / _MM_PER_M
        diameter_m = geometry.bottle_diameter_mm / _MM_PER_M
        # 最短路徑約為相機到瓶面來回；留 20% 餘裕避免截掉首個回波。
        shortest = 2.0 * sensor_distance_m * 0.8
        # 最長考量瓶內來回數次的散射路徑。
        longest = 2.0 * (sensor_distance_m + diameter_m * 3.0)
        span = max(longest - shortest, diameter_m)
        return float(shortest), float(span / temporal_bins)

    def render_transient(
        self,
        config: ScenarioConfig,
        out_dir: str | Path,
        temporal_bins: int = 256,
        start_opl_m: float | None = None,
        bin_width_opl_m: float | None = None,
        stem: str = "transient",
    ) -> TransientResult:
        """算出 transient 並存成 .npy，另存獨立的時間軸檔。"""
        mi = require_mitsuba(self.variant)
        import mitransient as mitr

        if start_opl_m is None or bin_width_opl_m is None:
            derived_start, derived_width = self.default_binning(config, temporal_bins)
            start_opl_m = derived_start if start_opl_m is None else start_opl_m
            bin_width_opl_m = (
                derived_width if bin_width_opl_m is None else bin_width_opl_m
            )

        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        scene_dict = build_transient_scene_dict(
            mi, config, temporal_bins, start_opl_m, bin_width_opl_m
        )
        started = time.perf_counter()
        scene = mi.load_dict(scene_dict)
        mi.render(scene, spp=int(config.spp), seed=int(config.seed))
        transient = np.array(scene.sensors()[0].film().develop_transient_())
        runtime = time.perf_counter() - started

        if not np.all(np.isfinite(transient)):
            raise SimulationDependencyError(
                "the transient render produced NaN/Inf values. SRC-SAI §28 requires "
                "fail-fast here: it means the integrator parameters are already "
                "unphysical, and clipping them would hide that."
            )

        transient_path = target_dir / f"{stem}.npy"
        np.save(transient_path, transient.astype(np.float32))

        # 時間軸取每個 bin 的中心。這是 optical transient time，
        # 與 measurement time 是兩條不同的軸（SRC-D03）。
        bin_centres_opl = start_opl_m + (np.arange(temporal_bins) + 0.5) * bin_width_opl_m
        time_axis_s = bin_centres_opl / SPEED_OF_LIGHT_M_PER_S
        time_path = target_dir / f"{stem}_time.npy"
        np.save(time_path, time_axis_s.astype(np.float64))

        return TransientResult(
            scenario_hash=config.scene_hash(),
            variant=self.variant,
            mitsuba_version=mi.__version__,
            mitransient_version=getattr(mitr, "__version__", "unknown"),
            seed=int(config.seed),
            spp=int(config.spp),
            temporal_bins=int(temporal_bins),
            start_opl_m=float(start_opl_m),
            bin_width_opl_m=float(bin_width_opl_m),
            shape=tuple(int(v) for v in transient.shape),
            runtime_s=runtime,
            output_paths={
                "optical_transient": transient_path.as_posix(),
                "optical_transient_time_axis": time_path.as_posix(),
            },
            extra={
                "integrator": "transient_path",
                "temporal_filter": "box",
                "scene_units": "metre",
                "total_energy": float(transient.sum()),
                "nonzero_bin_ratio": float(
                    np.mean(transient.sum(axis=(0, 1, 3)) > 0.0)
                ),
            },
        )
