# PC-MEF Research System source maintenance contract
# 上下游: 由 simulation.controller 呼叫；重用 mitsuba_adapter 的 scene dict，
#         只替換 integrator 與 film；寫出 transient.npy 與 transient_time.npy
#         到 artifact 目錄，後者即 CanonicalCase 的 optical_transient_time_axis。
# 檔案路徑: pcmef/simulation/mitransient_adapter.py
# 產生時間: 2026-08-26 07:20 +08:00
# 版本: v0.5.0
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
#   - 不得把前緣餘裕改回相對比例（如 shortest * 0.9）：在 5 公分場景那只有
#     0.73 個 bin，峰值會落在 bin 0 而量不到左側半高點（NOTE-027）。
#   - 積分器不得改由呼叫端指定：帶介質卻用 transient_path 會靜默忽略介質，
#     算得出圖但四類散射差異完全消失（NOTE-026）。
#   - v0.2.0 修正：時間窗初版在峰值抵達前就關窗（實測峰值 OPL 0.465 m，
#     初版窗尾僅 0.442 m），導致 FWHM 恆為 0；改由 scene_path_bounds 推導，
#     並新增截斷偵測，讓這類錯誤在產出當下就中斷而非等下游發現。
#   - v0.3.0 場景含參與介質時自動改用 transient_prbvolpath（NOTE-026）。
#   - v0.4.0 前緣餘裕改以 bin 數表示並解出上界 m < shortest*N/end，
#     bounce_budget 預設 7.0 → 3.0（NOTE-027）。
#   - v0.5.0 manifest 的 integrator 欄改記**實際採用**的積分器；先前硬編
#     "transient_path"，帶介質的三個場景等於記載了沒用過的積分器。
#     bounce_budget 提升為具名常數 _DEFAULT_BOUNCE_BUDGET，供 parameter
#     registry 的 formal 防線綁定（NOTE-030）。
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
    Illumination,
    SimulationDependencyError,
    build_scene_dict,
    require_mitsuba,
    scene_path_bounds,
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
    illumination: Illumination = Illumination.BOTH,
) -> dict[str, Any]:
    """在共用場景上替換成 transient integrator 與 film。

    刻意重用 build_scene_dict()：SRC-SAI FR-005 要求同一 scenario 產出的
    RGB 與 transient 必須來自同一個場景，各建一份場景會讓兩者悄悄分歧。
    """
    scene = build_scene_dict(mi, config, illumination)
    # NOTE(NOTE-026): 帶參與介質的場景必須用 volumetric 積分器。
    # transient_path 會**靜默忽略** interior medium —— 算得出圖、能量也正常，
    # 但四類的散射差異完全不見（實測：加介質前後總能量到小數點都相同）。
    # 這種「看起來成功的錯」比崩潰危險，因此依場景內容自動選擇積分器。
    has_medium = any(
        isinstance(node, dict) and "interior" in node
        for node in scene.values()
        if isinstance(node, dict)
    )
    scene["integrator"] = {
        "type": "transient_prbvolpath" if has_medium else "transient_path",
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


#: 時間窗起點保留給首個回波前緣的 bin 數（NOTE-027）。
#: 峰值落在 bin 0 時量不到左側半高點，FWHM 恆為 0，Sigma 映射整條路不可用 ——
#: 這與 NOTE-013 的窗尾截斷是同一種病，只是發生在另一端。
#: 8 個 bin 是「足以解析上升緣」的下界，不是校準值。
_LEADING_MARGIN_BINS = 8

#: 時間窗尾端 = shortest + bounce_budget * extent。由實測能量末端 0.646 m 推得
#: （NOTE-027），**未校準**；改場景後須重新量測末端能量再定。
#: 提升為具名常數是為了讓 parameter registry 的 formal 防線攔得到它 —— 先前它
#: 只是 __init__ 的預設引數，不是任何防線看得見的東西（NOTE-030）。
_DEFAULT_BOUNCE_BUDGET = 3.0


class MiTransientAdapter:
    """optical transient 算圖的封裝。"""

    def __init__(
        self, variant: str = DEFAULT_VARIANT, bounce_budget: float = _DEFAULT_BOUNCE_BUDGET
    ) -> None:
        self.variant = variant
        self.bounce_budget = float(bounce_budget)

    def default_binning(
        self, config: ScenarioConfig, temporal_bins: int = 256
    ) -> tuple[float, float]:
        """依場景尺度推導 start_opl 與 bin_width_opl。

        cornell_box 的預設值是房間尺度（start_opl=3.5 m）；本場景只有 5 公分，
        直接沿用會讓整段時間軸落在光還沒抵達的區間，算出全零的 transient。

        v0.2.0 修正：初版把最長路徑估成「相機到瓶身來回加瓶徑三倍」，
        得到 0.442 m 的窗尾。實測掃描（Empty 場景、0-3 m、10 mm bin）顯示
        首次抵達在 0.185 m、**峰值在 0.465 m**、99% 能量到 1.475 m、
        末端 1.675 m —— 也就是初版的窗在峰值抵達前就關了，波形單調上升到邊界，
        找不到右側半高點，FWHM 恆為 0，surrogate 的 Sigma 映射整條路不可用。
        改以 scene_path_bounds() 的直達路徑與場景跨距推導，並乘上反射次數預算。
        """
        shortest, extent = scene_path_bounds(config)
        end = shortest + self.bounce_budget * extent
        # NOTE(NOTE-027): 前緣餘裕以 **bin 數** 表示，不用相對比例。
        # 解 bin_width = (end - start)/N 且 start = shortest - m*bin_width，
        # 得 bin_width = (end - shortest)/(N - m)，非循環。
        #
        # 餘裕有上界：start 不得 <= 0（光還沒離開發射器）。
        # 由 m*(end-shortest)/(N-m) < shortest 解得 m < shortest*N/end。
        # bin 數太少時窗口本身就粗，餘裕跟著被壓縮 —— 這是幾何事實，
        # 不是可調參數，因此實際採用的 m 一律寫進 manifest 供事後核對。
        margin_ceiling = int(shortest * temporal_bins / end) - 1
        margin = max(1, min(_LEADING_MARGIN_BINS, margin_ceiling))
        if temporal_bins <= margin:
            raise SimulationDependencyError(
                f"temporal_bins ({temporal_bins}) must exceed the leading margin "
                f"({margin} bins); otherwise the window has no room for the echo."
            )
        bin_width = (end - shortest) / (temporal_bins - margin)
        start = shortest - margin * bin_width
        if start <= 0.0:
            raise SimulationDependencyError(
                f"derived start_opl ({start:.4f} m) is not positive; the leading "
                f"margin ({margin} bins) does not fit in {temporal_bins} bins for a "
                f"scene whose shortest path is {shortest:.4f} m."
            )
        return float(start), float(bin_width)

    @staticmethod
    def _render_pass(mi, scene_dict: dict[str, Any], config: ScenarioConfig):
        """算一個照明條件下的 transient。

        非有限值一律 fail-fast（SRC-SAI §28）：那代表積分器參數已經不合理，
        截掉它只會把問題藏起來。
        """
        scene = mi.load_dict(scene_dict)
        mi.render(scene, spp=int(config.spp), seed=int(config.seed))
        transient = np.array(scene.sensors()[0].film().develop_transient_())
        if not np.all(np.isfinite(transient)):
            raise SimulationDependencyError(
                "the transient render produced NaN/Inf values. SRC-SAI §28 requires "
                "fail-fast here: it means the integrator parameters are already "
                "unphysical, and clipping them would hide that."
            )
        return transient

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

        started = time.perf_counter()

        # NOTE(NOTE-034): 兩個 pass，同一組 binning、同一個 seed、同一份幾何，
        # 只有光源不同。
        #   active  —— VCSEL on / 室內光 off  -> Signal / Distance / Sigma-like
        #   ambient —— VCSEL off / 室內光 on  -> Ambient Rate
        # 共用 binning 是必要的：兩條波形若落在不同時間軸上就無法相加也無法比較。
        scene_dict = build_transient_scene_dict(
            mi, config, temporal_bins, start_opl_m, bin_width_opl_m,
            illumination=Illumination.ACTIVE_ONLY,
        )
        transient = self._render_pass(mi, scene_dict, config)
        ambient_dict = build_transient_scene_dict(
            mi, config, temporal_bins, start_opl_m, bin_width_opl_m,
            illumination=Illumination.AMBIENT_ONLY,
        )
        ambient_transient = self._render_pass(mi, ambient_dict, config)
        runtime = time.perf_counter() - started

        # 截斷偵測。波形若在窗邊仍在上升，代表光還在抵達時窗就關了；
        # 這種 transient 找不到右側半高點，surrogate 的 FWHM 恆為 0，
        # Sigma 映射整條路不可用。初版沒有這個檢查，直到 Batch 5 消費它才發現。
        waveform = transient.sum(axis=(0, 1, 3))
        peak_bin = int(np.argmax(waveform))
        peak_value = float(waveform[peak_bin])
        edge_fraction = (
            float(waveform[-1]) / peak_value if peak_value > 0 else 0.0
        )
        truncated = peak_bin >= temporal_bins - 2 or edge_fraction > 0.5
        if truncated:
            raise SimulationDependencyError(
                "the transient time window truncates the response: peak is at bin "
                f"{peak_bin}/{temporal_bins} and the final bin still holds "
                f"{edge_fraction:.0%} of the peak. Widen the window "
                f"(current: start={start_opl_m:.4f} m, "
                f"end={start_opl_m + bin_width_opl_m * temporal_bins:.4f} m) or raise "
                "bounce_budget. A truncated transient yields FWHM=0 and makes the "
                "Sigma mapping unusable."
            )

        transient_path = target_dir / f"{stem}.npy"
        np.save(transient_path, transient.astype(np.float32))
        ambient_path = target_dir / f"{stem}_ambient.npy"
        np.save(ambient_path, ambient_transient.astype(np.float32))

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
                "optical_transient_ambient": ambient_path.as_posix(),
                "optical_transient_time_axis": time_path.as_posix(),
            },
            extra={
                # 實際採用的積分器，不是寫死的字串。含參與介質時
                # build_transient_scene_dict() 會自動改用 transient_prbvolpath
                # （NOTE-026）；先前這裡硬編 "transient_path"，於是四個場景中
                # 有三個的 manifest 記載了它們沒有用過的積分器。manifest 是
                # provenance，記錯比不記更糟（NOTE-030）。
                "integrator": scene_dict["integrator"]["type"],
                "temporal_filter": "box",
                "scene_units": "metre",
                "total_energy": float(transient.sum()),
                # NOTE(NOTE-034): active 與 ambient 分開記錄。兩者的比值是
                # 「這個場景裡感測器自己的光佔多少」的直接證據，
                # 也是 Ambient 觀測量沒有被 active 汙染的可稽核依據。
                "illumination": "split_active_ambient",
                "active_total_energy": float(transient.sum()),
                "ambient_total_energy": float(ambient_transient.sum()),
                "nonzero_bin_ratio": float(np.mean(waveform > 0.0)),
                # 截斷診斷。peak_bin 貼在窗邊或 edge_fraction 偏高，
                # 就代表時間窗沒涵蓋完整回波（v0.2.0 新增）。
                "peak_bin": peak_bin,
                "edge_fraction": round(edge_fraction, 6),
                "bounce_budget": self.bounce_budget,
            },
        )
