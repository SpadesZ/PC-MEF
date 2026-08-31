# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.simulation.paired 產生的 clean 成對樣本，套上感測層的
#         劣化算子；由 cli 的 `perception stress` 呼叫；寫出
#         outputs/perception/<run>/stress_manifest.json。
#         **不讀真實資料、不碰 FORMAL_E1_FINAL、不改校準模擬器。**
# 檔案路徑: pcmef/perception/stress.py
# 產生時間: 2026-08-31 13:30 +08:00
# 版本: v0.1.0
# 功能說明: 由同一個 latent scene 派生四個可配對的 condition ——
#           Clean / Vision-degraded / ToF-degraded / Conflict ——
#           每個劣化都有物理或感測上的讀法與具名的 severity。
# 模組定位: gate 的受試資料層。它產生「兩個模態不一致」的情境，
#           讓 D/U/Q gate 有東西可以仲裁；它**不**決定 gate 規則，
#           也不知道 PC-MEF 會不會贏。
# 主要責任:
#   1. VISION_DEGRADATIONS / TOF_DEGRADATIONS 具名且有物理讀法的算子
#   2. degrade_vision() / degrade_tof() 依 severity 施加，決定性
#   3. build_stress_dataset() 由 clean base 派生四個 condition
#   4. select_severity() 依**事前宣告**的判準在 gate-validation 上選 severity
#   5. condition_summary() 回報每個 condition 實際造成的準確率變化
# 維護提醒:
#   - 不得為了讓 PC-MEF 贏而調整 severity 或加雜訊。severity 只能由
#     select_severity() 的事前判準決定，且該判準只看**單模態自己的**
#     準確率，不看任何 gate 或融合的結果。
#   - 不得在 Formal E2 上選 severity、門檻或任何規則；E2 只准算一次分數。
#   - 不得用非物理的擾動（例如直接把 logit 打亂、或對某一類專門加噪）。
#     每個算子都必須說得出它模擬的是哪一種感測失效。
#   - 不得改動 calibrated simulator 或重新算圖；劣化一律作用在
#     模擬器輸出**之後**，因此 clean 與 degraded 共用同一個 latent scene。
#   - v0.1.0 新增：首版 gate 受試資料。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_perception_gate.py -v
#   - py -3.10 -m pcmef.cli perception gate --help
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pcmef.core.constants import CLASS_ORDER, TOF_RECORDING_POINTS, TOF_SCHEMA
from pcmef.core.hash import hash_object

__all__ = [
    "CONDITIONS",
    "SEVERITY_LADDER",
    "StressError",
    "degrade_vision",
    "degrade_tof",
    "build_stress_dataset",
    "select_severity",
]

#: 四個 condition。Conflict 是兩個劣化同時施加 —— 刻意**不**另外設計一個
#: 「讓兩邊剛好指向不同類」的算子：那種構造是在雕刻答案，而不是模擬失效。
CONDITIONS = ("clean", "vision_degraded", "tof_degraded", "conflict")

#: severity 階梯。事前固定，select_severity() 只能從中挑一格。
#:
#: 低端在首次執行後往下延伸過一次：原本最細的一格是 0.25，而實測 ToF 在
#: **整條階梯上都掉到 0.250（恰為四類亂猜）**，因此事前宣告的目標帶
#: [0.30, 0.65] 在原階梯上根本不可達，select_severity() 只能回報 fallback。
#: 那不是「調鬆判準」而是「階梯的解析度不足以表達判準」。
#:
#: 往**下**延伸讓劣化更輕，也就是讓路由任務更難、讓 PC-MEF 的優勢更小 ——
#: 方向與「為了讓 PC-MEF 贏而調 severity」相反。判準本身一個字都沒有改，
#: 且延伸後的選擇一樣只看 gate-validation 上的單模態準確率。
SEVERITY_LADDER = (0.01, 0.02, 0.05, 0.1, 0.15, 0.25, 0.5, 1.0, 2.0, 3.0)

#: 劣化用的種子基底，與生成用過的全部種子不相交。
STRESS_SEED_BASE = 80000


class StressError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


# ---------------------------------------------------------------------------
# 劣化算子（每一個都說得出它模擬什麼）
# ---------------------------------------------------------------------------

VISION_DEGRADATIONS: dict[str, str] = {
    "defocus_blur": (
        "optical defocus: the lens is not focused on the bottle plane, so the "
        "image is convolved with a Gaussian point-spread function. Severity is "
        "the PSF sigma in pixels."
    ),
    "exposure_loss": (
        "short exposure / low sensor gain: the linear radiance is scaled down "
        "before quantisation, so the scene occupies fewer effective levels."
    ),
    "read_noise": (
        "sensor read noise: a Gaussian noise floor added after exposure "
        "scaling, independent of signal, as a real CMOS readout chain adds."
    ),
}

TOF_DEGRADATIONS: dict[str, str] = {
    "ambient_flood": (
        "strong ambient illumination (e.g. sunlight) floods the SPAD: the "
        "ambient rate rises and the shot-noise floor on every channel rises "
        "with it, which is how ambient actually degrades a ToF return."
    ),
    "signal_attenuation": (
        "an absorbing or fouled window attenuates the active return, lowering "
        "signal_rate_mcps without changing the scene."
    ),
    "range_walk": (
        "range walk: at low SNR a leading-edge discriminator fires later, so "
        "the reported distance is biased outward by an amount that grows as "
        "the signal falls. This is the classic ToF systematic, not added noise."
    ),
}


def degrade_vision(
    image: np.ndarray, severity: float, seed: int
) -> tuple[np.ndarray, dict[str, Any]]:
    """對線性 HDR 影像施加光學＋感測劣化。決定性（給定 seed）。

    順序是物理的：先失焦（光學，在感光面之前），再曝光衰減（感光），
    最後加讀出雜訊（讀出鏈）。倒過來做會讓讀出雜訊也被模糊掉，
    而真實的讀出雜訊發生在光學之後。
    """
    from scipy.ndimage import gaussian_filter

    if severity < 0:
        raise StressError("BAD_SEVERITY", f"severity must be >= 0, got {severity}")
    array = np.clip(np.asarray(image, dtype=np.float64), 0.0, None)

    sigma_px = 1.2 * severity
    blurred = (
        gaussian_filter(array, sigma=(sigma_px, sigma_px, 0.0), mode="nearest")
        if sigma_px > 0
        else array
    )

    exposure = 1.0 / (1.0 + 1.5 * severity)
    exposed = blurred * exposure

    rng = np.random.default_rng(seed)
    # 讀出雜訊的尺度綁在**乾淨影像自己的**平均亮度上，因此它對亮場景與
    # 暗場景是同一個相對強度，不會變成「對某些類特別不利」的擾動。
    noise_scale = 0.02 * severity * float(array.mean())
    noisy = exposed + rng.normal(0.0, noise_scale, size=exposed.shape)

    return np.clip(noisy, 0.0, None), {
        "psf_sigma_px": sigma_px,
        "exposure_scale": exposure,
        "read_noise_sd": noise_scale,
        "operators": list(VISION_DEGRADATIONS),
    }


def degrade_tof(
    recording: np.ndarray, severity: float, seed: int
) -> tuple[np.ndarray, dict[str, Any]]:
    """對 (500, 4) recording 施加感測層劣化。決定性（給定 seed）。

    三個算子彼此耦合，因為真實的失效就是耦合的：ambient 上升會抬高雜訊底線，
    訊號衰減會同時降低 SNR 並經 range walk 把距離往外推。
    """
    if severity < 0:
        raise StressError("BAD_SEVERITY", f"severity must be >= 0, got {severity}")
    array = np.array(recording, dtype=np.float64, copy=True)
    if array.shape != (TOF_RECORDING_POINTS, len(TOF_SCHEMA)):
        raise StressError("TOF_SHAPE", f"recording has shape {array.shape}")

    d = TOF_SCHEMA.index("distance_mm")
    a = TOF_SCHEMA.index("ambient_rate_mcps")
    s = TOF_SCHEMA.index("signal_rate_mcps")
    g = TOF_SCHEMA.index("sigma_like")

    rng = np.random.default_rng(seed)
    clean_ambient = float(np.median(array[:, a]))
    clean_signal = float(np.median(array[:, s]))

    # 1. ambient flood：ambient 抬高，且雜訊底線隨之上升。
    ambient_gain = 1.0 + 8.0 * severity
    array[:, a] = array[:, a] * ambient_gain

    # 2. signal attenuation：主動回波衰減。
    attenuation = 1.0 / (1.0 + 2.0 * severity)
    array[:, s] = array[:, s] * attenuation

    # 3. shot-noise 底線隨 ambient 上升、隨 signal 下降。
    snr_loss = ambient_gain / max(attenuation, 1e-12)
    relative_noise = 0.05 * severity * np.sqrt(snr_loss)
    for column, scale in ((a, clean_ambient * ambient_gain), (s, clean_signal * attenuation)):
        array[:, column] += rng.normal(0.0, relative_noise * abs(scale), size=len(array))

    # 4. range walk：SNR 掉下去時前緣判別器晚觸發，距離被往外推。
    walk_mm = 6.0 * severity * (1.0 - attenuation)
    array[:, d] = array[:, d] + walk_mm + rng.normal(
        0.0, 0.5 * severity, size=len(array)
    )

    # 5. sigma-like 隨 SNR 惡化而變寬（波形被雜訊拉胖）。
    array[:, g] = array[:, g] * (1.0 + 0.6 * severity)

    return np.clip(array, 0.0, None), {
        "ambient_gain": ambient_gain,
        "signal_attenuation": attenuation,
        "relative_noise": relative_noise,
        "range_walk_mm": walk_mm,
        "sigma_broadening": 1.0 + 0.6 * severity,
        "operators": list(TOF_DEGRADATIONS),
    }


# ---------------------------------------------------------------------------
# 由 clean base 派生四個 condition
# ---------------------------------------------------------------------------


def build_stress_dataset(
    base_manifest: dict[str, Any],
    vision_severity: float,
    tof_severity: float,
    out_dir: str | Path,
    code_version: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """由每個 clean sample 派生四個 condition，寫出 stress manifest。

    四個 condition 共用**同一個 latent scene**（同一次算圖），因此
    condition 之間的差別純粹是感測劣化，不是另一個場景。
    """
    from pcmef.perception.dataset import _load_exr

    say = progress or (lambda _m: None)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for index, sample in enumerate(base_manifest["samples"]):
        clean_rgb = _load_exr(sample["rgb"]["exr_path"])
        clean_tof = np.load(sample["tof"]["path"])
        seed = STRESS_SEED_BASE + index

        variants = {
            "clean": (clean_rgb, clean_tof, {}, {}),
        }
        v_img, v_meta = degrade_vision(clean_rgb, vision_severity, seed)
        t_rec, t_meta = degrade_tof(clean_tof, tof_severity, seed + 1)
        variants["vision_degraded"] = (v_img, clean_tof, v_meta, {})
        variants["tof_degraded"] = (clean_rgb, t_rec, {}, t_meta)
        variants["conflict"] = (v_img, t_rec, v_meta, t_meta)

        for condition, (rgb, tof, vmeta, tmeta) in variants.items():
            stem = f"{sample['scenario_id']}__{condition}"
            rgb_path = target / f"{stem}.rgb.npy"
            tof_path = target / f"{stem}.tof.npy"
            np.save(rgb_path, np.ascontiguousarray(rgb, dtype=np.float64))
            np.save(tof_path, np.ascontiguousarray(tof, dtype=np.float64))
            rows.append(
                {
                    "stress_id": stem,
                    "base_scenario_id": sample["scenario_id"],
                    "condition": condition,
                    "class_label": sample["class_label"],
                    "class_index": sample["class_index"],
                    "physical_scene_family": sample["physical_scene_family"],
                    "family_index": sample.get("family_index"),
                    "base_seed": sample["seed_family"]["scenario_seed"],
                    "stress_seed": seed,
                    "rgb_path": rgb_path.as_posix(),
                    "tof_path": tof_path.as_posix(),
                    "vision_severity": vision_severity if vmeta else 0.0,
                    "tof_severity": tof_severity if tmeta else 0.0,
                    "vision_degradation": vmeta or None,
                    "tof_degradation": tmeta or None,
                }
            )
        if (index + 1) % 24 == 0:
            say(f"  {index + 1}/{len(base_manifest['samples'])} base samples")

    manifest = {
        "manifest_id": "perception_stress",
        "scientific_result": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "out_dir": target.as_posix(),
        "conditions": list(CONDITIONS),
        "severity": {"vision": vision_severity, "tof": tof_severity},
        "degradation_catalogue": {
            "vision": dict(VISION_DEGRADATIONS),
            "tof": dict(TOF_DEGRADATIONS),
        },
        "design": (
            "All four conditions of one row derive from the SAME latent scene "
            "(one render). Differences between conditions are sensor-level "
            "degradations applied after the calibrated simulator, never a "
            "different scene and never a change to the simulator."
        ),
        "base_manifest": {
            "run_dir": base_manifest.get("run_dir"),
            "simulator_identity_hash": base_manifest["simulator"]["identity_hash"],
            "families_per_class": base_manifest.get("families_per_class"),
        },
        "counts": {
            condition: sum(1 for r in rows if r["condition"] == condition)
            for condition in CONDITIONS
        },
        "total": len(rows),
        "rows": rows,
    }
    manifest["content_hash"] = hash_object(
        {"rows": [r["stress_id"] for r in rows], "severity": manifest["severity"]}
    )
    (target / "stress_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# severity 選定（事前判準，只看單模態自己的準確率）
# ---------------------------------------------------------------------------

#: 事前宣告的目標帶。劣化後的該模態必須明顯壞掉但**不是純亂猜**
#: （0.25 是四類的亂猜水準），而另一個模態必須幾乎不受影響。
DEGRADED_TARGET_BAND = (0.30, 0.65)
UNAFFECTED_MINIMUM = 0.90


def select_severity(
    accuracy_by_severity: dict[float, dict[str, float]],
    degraded_modality: str,
) -> dict[str, Any]:
    """依事前判準挑 severity。

    判準只看**單模態自己的**準確率，完全不看 gate、融合或 PC-MEF 的結果 ——
    那是「不得為了讓 PC-MEF 贏而調 severity」這條禁令的實際執行方式。

    取第一個同時滿足兩個條件的 severity：
      * 被劣化的模態落進 DEGRADED_TARGET_BAND
      * 另一個模態仍 >= UNAFFECTED_MINIMUM
    都不滿足時取「最接近目標帶中心」的那一格，並具名記錄未達標。
    """
    other = "tof" if degraded_modality == "vision" else "vision"
    lo, hi = DEGRADED_TARGET_BAND
    rows = []
    for severity in sorted(accuracy_by_severity):
        entry = accuracy_by_severity[severity]
        in_band = lo <= entry[degraded_modality] <= hi
        other_ok = entry[other] >= UNAFFECTED_MINIMUM
        rows.append(
            {
                "severity": severity,
                f"{degraded_modality}_accuracy": entry[degraded_modality],
                f"{other}_accuracy": entry[other],
                "in_target_band": in_band,
                "other_modality_unaffected": other_ok,
                "satisfies_rule": in_band and other_ok,
            }
        )

    chosen = next((r for r in rows if r["satisfies_rule"]), None)
    if chosen is None:
        centre = 0.5 * (lo + hi)
        chosen = min(
            rows, key=lambda r: abs(r[f"{degraded_modality}_accuracy"] - centre)
        )
    return {
        "degraded_modality": degraded_modality,
        "selected_severity": chosen["severity"],
        "rule": (
            f"first severity on the fixed ladder where the degraded modality lands "
            f"in {DEGRADED_TARGET_BAND} and the other stays >= {UNAFFECTED_MINIMUM}; "
            "measured on gate-validation only, never on Formal E2, and never using "
            "any gate or fusion outcome"
        ),
        "rule_satisfied": bool(chosen["satisfies_rule"]),
        "fallback_used": not chosen["satisfies_rule"],
        "ladder": rows,
    }


def condition_summary(
    rows: list[dict[str, Any]],
    vision_correct: np.ndarray,
    tof_correct: np.ndarray,
) -> dict[str, Any]:
    """每個 condition 實際造成什麼 —— 包含三種要求的情境各出現幾次。"""
    conditions = np.array([r["condition"] for r in rows])
    out: dict[str, Any] = {}
    for condition in CONDITIONS:
        mask = conditions == condition
        if not mask.any():
            continue
        v, t = vision_correct[mask], tof_correct[mask]
        out[condition] = {
            "n": int(mask.sum()),
            "vision_accuracy": float(v.mean()),
            "tof_accuracy": float(t.mean()),
            "vision_right_tof_wrong": int((v & ~t).sum()),
            "tof_right_vision_wrong": int((t & ~v).sum()),
            "both_right": int((v & t).sum()),
            "both_wrong": int((~v & ~t).sum()),
        }
    return out
