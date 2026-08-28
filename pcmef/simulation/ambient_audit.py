# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 sim ambient-check 在子行程呼叫（NOTE-012）；
#         直接驅動 mitsuba_adapter / mitransient_adapter 算多個照明條件；
#         產出 ambient_observable_audit.json，供 initial_simulation readiness 引用。
# 檔案路徑: pcmef/simulation/ambient_audit.py
# 產生時間: 2026-08-28 14:10 +08:00
# 版本: v0.1.0
# 功能說明: 用四個可證偽的實測檢查，證明 Ambient 這一欄量到的真的是環境光、
#           而 Signal 量到的真的是主動照明的回波 —— 不是互相污染的同一條波形。
# 模組定位: Ambient/Signal 分離的驗收器。它「不是」校準器 ——
#           它不決定任何常數的值，只判定「這兩個量現在分得開嗎」。
# 主要責任:
#   1. AmbientCheck 保存單一檢查的判定與其量到的數字
#   2. run_ambient_audit() 依序跑四個檢查並回傳可落檔的報告
#   3. 四個檢查各自獨立算圖，不共用結果，避免一次錯誤傳染全部
# 維護提醒:
#   - 不得放寬 A4 的容忍值來讓它過關；室內光關閉後 ambient pass 若仍有能量，
#     代表 VCSEL 漏進了 ambient pass，那正是本模組要抓的病。
#   - 不得把 A2 改成「近似不變」；室內光與 VCSEL 功率解耦後它應**恰好**不變，
#     出現任何相依都代表耦合又被接回去了（NOTE-034）。
#   - 不得以本模組的通過作為 fidelity 主張；它只證明兩個量分得開，
#     不證明任何一個量接近真實值。
#   - v0.1.0 新增：首版 Ambient/Signal 分離驗收（NOTE-034）。
# 驗證方式:
#   - py -3.10 -m pcmef.cli sim ambient-check --out outputs/ambient_audit
#   - py -3.10 -m pytest tests/surrogate/test_ambient_and_estimator.py -v
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = ["AmbientCheck", "run_ambient_audit", "AMBIENT_CHECK_IDS"]

#: 四個檢查的 ID 與其判準。先寫在這裡再實作，讓「跑完再發明規則」沒有空間。
AMBIENT_CHECK_IDS: dict[str, str] = {
    "A1": "room light up -> Ambient increases monotonically",
    "A2": "VCSEL power change -> Ambient does NOT scale with it",
    "A3": "VCSEL off -> the active illumination component disappears",
    "A4": "environment off -> Ambient is ~0",
}


@dataclass
class AmbientCheck:
    """單一檢查的結果。measurements 一律留下，PASS 與 FAIL 都要能被複核。"""

    check_id: str
    description: str
    passed: bool
    detail: str
    measurements: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "description": self.description,
            "status": "PASS" if self.passed else "FAIL",
            "detail": self.detail,
            "measurements": self.measurements,
        }


def run_ambient_audit(
    class_label: str = "Empty",
    seed: int = 1001,
    temporal_bins: int = 128,
    resolution: tuple[int, int] = (32, 32),
    spp: int = 16,
) -> dict[str, Any]:
    """跑四個檢查。必須在已 set_variant 的行程內呼叫。

    解析度刻意可調低：本模組驗的是**相依關係**（單調、無關、消失、歸零），
    不是絕對數值，因此不需要 formal 等級的取樣數。
    """
    import mitsuba as mi

    # mitransient 必須在 set_variant 之後 import，且**必須** import ——
    # transient_path / transient_prbvolpath 是它註冊的 plugin，
    # 少了這行會得到 "Plugin not found" 而不是任何與 ambient 有關的訊息。
    import mitransient  # noqa: F401

    from pcmef.simulation import mitsuba_adapter as ma
    from pcmef.simulation.mitransient_adapter import (
        MiTransientAdapter,
        build_transient_scene_dict,
    )
    from pcmef.simulation.mitsuba_adapter import Illumination
    from pcmef.simulation.scenario import Geometry, Lighting, ScenarioConfig

    adapter = MiTransientAdapter()

    def make_config(irradiance: float) -> ScenarioConfig:
        return ScenarioConfig(
            class_label=class_label,
            seed=seed,
            geometry=Geometry(),
            lighting=Lighting(preset="nominal", irradiance=irradiance),
            medium_parameters={},
            spp=spp,
            resolution=resolution,
        )

    base = make_config(1.0)
    start, width = adapter.default_binning(base, temporal_bins)

    def energy(config: ScenarioConfig, illumination: Illumination) -> float:
        scene_dict = build_transient_scene_dict(
            mi, config, temporal_bins, start, width, illumination=illumination
        )
        scene = mi.load_dict(scene_dict)
        mi.render(scene, spp=int(config.spp), seed=int(config.seed))
        transient = np.array(scene.sensors()[0].film().develop_transient_())
        return float(transient.sum())

    original_radiance = ma._ROOM_LIGHT_RADIANCE
    checks: list[AmbientCheck] = []
    try:
        # -- A1: 室內光遞增 -> Ambient 單調遞增 ---------------------------
        ladder = [0.005, 0.02, 0.08, 0.32]
        ambient_by_radiance: dict[str, float] = {}
        for radiance in ladder:
            ma._ROOM_LIGHT_RADIANCE = radiance
            ambient_by_radiance[str(radiance)] = energy(base, Illumination.AMBIENT_ONLY)
        ma._ROOM_LIGHT_RADIANCE = original_radiance

        values = [ambient_by_radiance[str(r)] for r in ladder]
        monotonic = all(b > a for a, b in zip(values, values[1:]))
        # 額外量線性度：室內光是純增益，比值應貼近輸入比值。
        # 這不是判準的一部分（Monte Carlo 有雜訊），但它是 CG-3 簡併結構的證據。
        ratios = [
            (values[i + 1] / values[i]) / (ladder[i + 1] / ladder[i])
            for i in range(len(ladder) - 1)
            if values[i] > 0
        ]
        checks.append(
            AmbientCheck(
                "A1",
                AMBIENT_CHECK_IDS["A1"],
                monotonic,
                (
                    f"ambient energy over room radiance {ladder}: "
                    f"{[round(v, 4) for v in values]}"
                    + (
                        ""
                        if monotonic
                        else " -- not strictly increasing; Ambient does not track "
                        "the room light"
                    )
                ),
                {
                    "ambient_by_radiance": ambient_by_radiance,
                    "gain_linearity_ratios": [round(r, 4) for r in ratios],
                },
            )
        )

        # -- A2: 改 VCSEL 功率 -> Ambient 不應跟著變 -----------------------
        ambient_at_1 = energy(make_config(1.0), Illumination.AMBIENT_ONLY)
        ambient_at_4 = energy(make_config(4.0), Illumination.AMBIENT_ONLY)
        active_at_1 = energy(make_config(1.0), Illumination.ACTIVE_ONLY)
        active_at_4 = energy(make_config(4.0), Illumination.ACTIVE_ONLY)
        # 判準取「恰好不變」而非「近似不變」：解耦後兩者是同一個場景同一個
        # seed，差值必須是 0。任何非零都代表耦合被接了回去。
        ambient_unchanged = ambient_at_1 == ambient_at_4
        active_scaled = active_at_1 > 0 and abs(active_at_4 / active_at_1 - 4.0) < 0.05
        checks.append(
            AmbientCheck(
                "A2",
                AMBIENT_CHECK_IDS["A2"],
                ambient_unchanged and active_scaled,
                (
                    f"irradiance 1.0 -> 4.0: ambient {ambient_at_1:.6f} -> "
                    f"{ambient_at_4:.6f} (must be identical); "
                    f"active {active_at_1:.1f} -> {active_at_4:.1f} "
                    f"(x{active_at_4 / active_at_1 if active_at_1 else float('nan'):.4f}, "
                    "must be x4)"
                ),
                {
                    "ambient_at_irradiance_1": ambient_at_1,
                    "ambient_at_irradiance_4": ambient_at_4,
                    "active_at_irradiance_1": active_at_1,
                    "active_at_irradiance_4": active_at_4,
                    "ambient_unchanged": ambient_unchanged,
                    "active_scales_with_irradiance": active_scaled,
                },
            )
        )

        # -- A3: VCSEL 關閉 -> 主動照明成分消失 ---------------------------
        # 以主窗（active 峰值 ±8 bin）內的能量比對：ambient pass 在那個窗裡
        # 不該有回波，因為那個回波本來就是 VCSEL 打出去的。
        active_scene = build_transient_scene_dict(
            mi, base, temporal_bins, start, width,
            illumination=Illumination.ACTIVE_ONLY,
        )
        scene = mi.load_dict(active_scene)
        mi.render(scene, spp=int(base.spp), seed=int(base.seed))
        active_cube = np.array(scene.sensors()[0].film().develop_transient_())
        active_wave = active_cube.sum(axis=(0, 1, 3))

        ambient_scene = build_transient_scene_dict(
            mi, base, temporal_bins, start, width,
            illumination=Illumination.AMBIENT_ONLY,
        )
        scene = mi.load_dict(ambient_scene)
        mi.render(scene, spp=int(base.spp), seed=int(base.seed))
        ambient_wave = np.array(
            scene.sensors()[0].film().develop_transient_()
        ).sum(axis=(0, 1, 3))

        peak = int(np.argmax(active_wave))
        low, high = max(peak - 8, 0), min(peak + 9, active_wave.size)
        active_main = float(active_wave[low:high].sum())
        ambient_in_main = float(ambient_wave[low:high].sum())
        residual = ambient_in_main / active_main if active_main > 0 else float("inf")
        # 1e-3 的意思是「ambient pass 在主回波窗內殘留不到千分之一」。
        # 不取 0：ambient pass 的光同樣會打到同一個表面再回來，那是真的環境光
        # 貢獻，不是污染；要抓的是 VCSEL 漏進來造成的同量級回波。
        a3_pass = residual < 1e-3
        checks.append(
            AmbientCheck(
                "A3",
                AMBIENT_CHECK_IDS["A3"],
                a3_pass,
                (
                    f"main-window energy: active {active_main:.1f}, ambient "
                    f"{ambient_in_main:.4f}, residual ratio {residual:.3e} "
                    "(must be < 1e-3)"
                ),
                {
                    "active_main_window_energy": active_main,
                    "ambient_in_active_main_window": ambient_in_main,
                    "residual_ratio": residual,
                    "main_window_bins": [low, high],
                },
            )
        )

        # -- A4: 環境光關閉 -> Ambient 歸零 --------------------------------
        ma._ROOM_LIGHT_RADIANCE = 0.0
        ambient_off = energy(base, Illumination.AMBIENT_ONLY)
        ma._ROOM_LIGHT_RADIANCE = original_radiance
        ambient_on = energy(base, Illumination.AMBIENT_ONLY)
        # 這裡要求**恰為零**：ambient pass 沒有 VCSEL，室內光關掉之後場景裡
        # 沒有任何光源，能量只能是 0。非零代表有第三個光源漏進來。
        a4_pass = ambient_off == 0.0
        checks.append(
            AmbientCheck(
                "A4",
                AMBIENT_CHECK_IDS["A4"],
                a4_pass,
                (
                    f"room light off -> ambient energy {ambient_off:.6g} "
                    f"(must be exactly 0; with room light on it is {ambient_on:.4f})"
                ),
                {
                    "ambient_room_light_off": ambient_off,
                    "ambient_room_light_on": ambient_on,
                },
            )
        )
    finally:
        ma._ROOM_LIGHT_RADIANCE = original_radiance

    passed = sum(1 for c in checks if c.passed)
    return {
        "audit": "ambient_observable",
        "note": "NOTE-034",
        "class_label": class_label,
        "settings": {
            "seed": seed,
            "temporal_bins": temporal_bins,
            "resolution": list(resolution),
            "spp": spp,
        },
        "checks": [c.to_dict() for c in checks],
        "counts": {"total": len(checks), "pass": passed, "fail": len(checks) - passed},
        "claim_boundary": (
            "These checks show only that Ambient and Signal are separable "
            "observables. They make no claim that either is close to the real "
            "sensor's values; the optical constants remain uncalibrated."
        ),
    }
