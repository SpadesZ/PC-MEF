# PC-MEF Research System source maintenance contract
# 上下游: 以 pcmef.simulation.paired 的成對生成器產生樣本；
#         由 cli 的 `perception dataset` 呼叫；寫出
#         outputs/perception/<run>/ 底下的每 sample RGB+ToF、
#         dataset_manifest.json 與 preprocessing.json。
#         **不讀真實資料，不碰 FORMAL_E1_FINAL。**
# 檔案路徑: pcmef/perception/dataset.py
# 產生時間: 2026-08-31 11:20 +08:00
# 版本: v0.1.0
# 功能說明: 產生 perception 的 train/val/test synthetic dataset，以
#           scenario_id 為單位切分，並把 RGB/ToF 的正規化常數**只由 train**
#           算出後凍進 manifest。
# 模組定位: perception 的資料層。它不訓練模型，也不決定架構；它只負責
#           「哪些 sample 屬於哪個 split」與「輸入怎麼被正規化」這兩件事
#           可被重現且沒有 leakage。
# 主要責任:
#   1. physical_scene_family() 算出**不含 seed** 的場景身分，供 leakage 稽核
#   2. build_dataset() 依 scenario index 切 train/val/test 並生成成對樣本
#   3. fit_preprocessing() 只用 train split 算出 RGB/ToF 正規化常數
#   4. load_split() 依 manifest 載入張量
#   5. audit_split_leakage() 逐項回報 scenario_id 與 scene family 的重疊
# 維護提醒:
#   - 不得用 val/test 的統計量算正規化常數。那是最典型的 leakage，
#     而且它不會讓任何測試失敗，只會讓分數變好看。
#   - 不得把 clipped PNG 當正式訓練輸入。EXR 是 HDR，PNG 在 uint8 就飽和了；
#     用 PNG 等於把 VCSEL 高光那一段資訊丟掉再宣稱模型學得起來。
#   - 不得為了模型表現改動 frozen simulation resolution / spp / temporal_bins。
#   - 不得把 scenario_id 不重疊講成「沒有 leakage」。當前生成器每類只有
#     **一個**物理場景，因此 scene family 必然跨 split；那件事必須照實回報。
#   - v0.1.0 新增：首版 perception dataset。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_perception_dataset.py -v
#   - py -3.10 -m pcmef.cli perception dataset --per-class 4
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pcmef.core.constants import CLASS_ORDER, TOF_RECORDING_POINTS, TOF_SCHEMA
from pcmef.core.hash import hash_object

__all__ = [
    "SPLIT_NAMES",
    "PerceptionDatasetError",
    "physical_scene_family",
    "split_plan",
    "build_dataset",
    "fit_preprocessing",
    "apply_rgb_preprocessing",
    "apply_tof_preprocessing",
    "load_split",
    "audit_split_leakage",
]

SPLIT_NAMES = ("train", "val", "test")

#: 預設每類樣本數與切分比例。以 scenario index 切，因此同一個 index
#: 永遠落在同一個 split —— 切分是**確定性**的，不是隨機抽的。
DEFAULT_PER_CLASS = 100
DEFAULT_RATIO = {"train": 0.60, "val": 0.20, "test": 0.20}

#: 正規化尺度的退化下限，相對於該通道自己的量級。1e-8 的用意是「浮點意義下
#: 沒有變化」，不是一個可以擋下真實訊號的軟門檻。
_DEGENERATE_RELATIVE_FLOOR = 1.0e-8


class PerceptionDatasetError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


# ---------------------------------------------------------------------------
# 場景族
# ---------------------------------------------------------------------------


def physical_scene_family(scene_parameters: dict[str, Any]) -> str:
    """**不含 seed** 的場景身分。

    `scene_hash()` 涵蓋 seed，因此每個 scenario 都是唯一的；但兩個只差在
    seed 的 scenario 是**同一個物理場景的兩次 Monte-Carlo 實現**。
    要回答「train 與 test 是不是同一個場景」，必須把 seed 拿掉再比。
    """
    physical = {
        key: value
        for key, value in scene_parameters.items()
        if key not in {"seed", "outputs"}
    }
    return hash_object(physical)


def split_plan(
    per_class: int = DEFAULT_PER_CLASS, ratio: dict[str, float] | None = None
) -> dict[str, list[int]]:
    """把 scenario index 依比例切成三段。連續切而不是隨機抽：
    切分因此只由 (per_class, ratio) 決定，重跑必然得到同一組。
    """
    ratio = dict(ratio or DEFAULT_RATIO)
    if abs(sum(ratio.values()) - 1.0) > 1e-9:
        raise PerceptionDatasetError(
            "INVALID_RATIO", f"split ratio must sum to 1, got {ratio}"
        )
    n_train = int(round(per_class * ratio["train"]))
    n_val = int(round(per_class * ratio["val"]))
    indices = list(range(per_class))
    plan = {
        "train": indices[:n_train],
        "val": indices[n_train : n_train + n_val],
        "test": indices[n_train + n_val :],
    }
    empty = [name for name, ids in plan.items() if not ids]
    if empty:
        raise PerceptionDatasetError(
            "EMPTY_SPLIT",
            f"split(s) {empty} would be empty at per_class={per_class}; "
            "raise per_class or change the ratio",
        )
    return plan


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def build_dataset(
    per_class: int = DEFAULT_PER_CLASS,
    out_root: str | Path = "outputs/perception",
    run_name: str | None = None,
    ratio: dict[str, float] | None = None,
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    code_version: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """產生成對資料集並寫出 manifest。每個 sample 都經同一條成對路徑。"""
    from pcmef.simulation.paired import (
        generate_paired_sample,
        load_calibrated_simulator,
        scenario_seed,
    )

    say = progress or (lambda _m: None)
    identity, calibration = load_calibrated_simulator(freeze_dir, repo_root)
    say(f"simulator identity {identity.identity_hash()[:16]}")

    stamp = run_name or datetime.now(timezone.utc).strftime("ds_%Y%m%dT%H%M%SZ")
    run_dir = Path(out_root) / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    plan = split_plan(per_class, ratio)

    samples: list[dict[str, Any]] = []
    for class_label in CLASS_ORDER:
        for split, indices in plan.items():
            for index in indices:
                seed = scenario_seed(class_label, index)
                scenario_id = (
                    f"{class_label.lower().replace('-', '_')}_{index:04d}"
                )
                sample = generate_paired_sample(
                    identity, calibration, scenario_id, class_label, seed,
                    run_dir / split,
                )
                row = sample.to_dict()
                row["split"] = split
                row["scenario_index"] = index
                row["physical_scene_family"] = physical_scene_family(
                    sample.scene_parameters
                )
                samples.append(row)
        say(f"  {class_label}: {per_class} scenarios")

    manifest = {
        "manifest_id": "perception_dataset",
        "scientific_result": False,
        "purpose": (
            "Synthetic paired RGB-ToF dataset for the perception baselines. Every "
            "sample comes from the E1-retained calibrated simulator through the same "
            "paired path; RGB and ToF of one sample share a scenario and a seed."
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "run_dir": run_dir.as_posix(),
        "per_class": per_class,
        "split_ratio": dict(ratio or DEFAULT_RATIO),
        "split_plan_indices": plan,
        "class_order": list(CLASS_ORDER),
        "feature_order": list(TOF_SCHEMA),
        "counts": {
            split: {
                c: sum(
                    1 for s in samples if s["split"] == split and s["class_label"] == c
                )
                for c in CLASS_ORDER
            }
            for split in SPLIT_NAMES
        },
        "totals": {
            split: sum(1 for s in samples if s["split"] == split)
            for split in SPLIT_NAMES
        },
        "simulator": identity.to_artifact(),
        "samples": samples,
    }
    manifest["leakage_audit"] = audit_split_leakage(manifest)
    (run_dir / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# leakage 稽核
# ---------------------------------------------------------------------------


def audit_split_leakage(manifest: dict[str, Any]) -> dict[str, Any]:
    """逐項回報 scenario_id 與物理場景族的重疊情形。

    兩個層級刻意分開回報：scenario_id 不重疊是切分本身的正確性，
    scene family 不重疊才是「模型沒看過這個場景」。當前生成器每類
    只有一個物理場景，因此後者必然為 false —— 那不是 bug，
    是這個資料集能支持什麼結論的**上限**，必須寫在臉上。
    """
    by_split = {
        split: [s for s in manifest["samples"] if s["split"] == split]
        for split in SPLIT_NAMES
    }
    ids = {split: {s["scenario_id"] for s in rows} for split, rows in by_split.items()}
    seeds = {
        split: {s["seed_family"]["scenario_seed"] for s in rows}
        for split, rows in by_split.items()
    }
    families = {
        split: {s["physical_scene_family"] for s in rows}
        for split, rows in by_split.items()
    }

    pairs = [("train", "val"), ("train", "test"), ("val", "test")]
    id_overlap = {f"{a}|{b}": sorted(ids[a] & ids[b]) for a, b in pairs}
    seed_overlap = {f"{a}|{b}": sorted(seeds[a] & seeds[b]) for a, b in pairs}
    family_overlap = {f"{a}|{b}": sorted(families[a] & families[b]) for a, b in pairs}

    all_families = set().union(*families.values())
    families_by_class: dict[str, set[str]] = {c: set() for c in CLASS_ORDER}
    for sample in manifest["samples"]:
        families_by_class[sample["class_label"]].add(sample["physical_scene_family"])

    return {
        "scenario_id_disjoint": all(not v for v in id_overlap.values()),
        "scenario_id_overlap": id_overlap,
        "scenario_seed_disjoint": all(not v for v in seed_overlap.values()),
        "scenario_seed_overlap": {k: len(v) for k, v in seed_overlap.items()},
        "physical_scene_family_disjoint": all(
            not v for v in family_overlap.values()
        ),
        "physical_scene_family_overlap_counts": {
            k: len(v) for k, v in family_overlap.items()
        },
        "distinct_physical_families_total": len(all_families),
        "distinct_physical_families_per_class": {
            c: len(v) for c, v in families_by_class.items()
        },
        "interpretation": (
            "scenario_id and scenario_seed are disjoint across splits, so no sample "
            "appears twice. Physical scene families are NOT disjoint: the paired "
            "generator produces exactly one physical scene per class, so train, val "
            "and test are Monte-Carlo realizations of the same four scenes. Accuracy "
            "here measures separability of four fixed scenes under render noise, NOT "
            "generalisation to unseen physical configurations."
        ),
    }


# ---------------------------------------------------------------------------
# 前處理（常數只由 train 算）
# ---------------------------------------------------------------------------


def _load_exr(path: str | Path) -> np.ndarray:
    """讀 EXR，回傳 (H, W, 3) float64。**用 EXR 不用 PNG。**"""
    import mitsuba as mi

    array = np.array(mi.Bitmap(str(path)), dtype=np.float64)
    if array.ndim != 3 or array.shape[2] < 3:
        raise PerceptionDatasetError(
            "RGB_SHAPE", f"{path} has shape {array.shape}; expected (H, W, >=3)"
        )
    return array[:, :, :3]


def _rgb_tonemap(array: np.ndarray) -> np.ndarray:
    """固定且可重現的 HDR -> 有界表示。

    `log1p` 而不是 Reinhard 或 gamma：VCSEL 高光讓動態範圍跨到 1e4 以上，
    線性或 gamma 都會把絕大多數像素壓在接近 0 的一小段裡。log1p 是
    **無參數**的，因此不存在「調一個 tone-map 參數讓分數變好」這條路。
    負值先夾到 0：EXR 可能帶極小負值，log1p(<-1) 沒有定義。
    """
    return np.log1p(np.clip(array, 0.0, None))


def _tof_transform(array: np.ndarray) -> np.ndarray:
    """四個特徵跨越數個數量級（signal ~1e5、ambient ~0.05），先取 log1p。"""
    return np.log1p(np.clip(array, 0.0, None))


def fit_preprocessing(manifest: dict[str, Any]) -> dict[str, Any]:
    """**只用 train split** 算出 RGB 與 ToF 的正規化常數。"""
    train = [s for s in manifest["samples"] if s["split"] == "train"]
    if not train:
        raise PerceptionDatasetError("EMPTY_SPLIT", "the train split is empty")

    rgb_stack = np.stack([_rgb_tonemap(_load_exr(s["rgb"]["exr_path"])) for s in train])
    rgb_mean = rgb_stack.mean(axis=(0, 1, 2))
    rgb_std = rgb_stack.std(axis=(0, 1, 2))

    tof_stack = np.concatenate(
        [_tof_transform(np.load(s["tof"]["path"])) for s in train], axis=0
    )
    tof_mean = tof_stack.mean(axis=0)
    tof_std = tof_stack.std(axis=0)

    # 退化判準用**相對**下限而不是 `<= 0`。一組完全相同的值，其 np.std 常常
    # 不是精確的 0 而是 ~1e-15 的浮點殘留；`<= 0` 放它過去，接著就會拿
    # 3e-15 當除數，把輸入放大到 1e15。相對於該通道自己的尺度來判，
    # 才問得出「這個通道有沒有變化」這個問題。
    for name, std, mean in (("rgb", rgb_std, rgb_mean), ("tof", tof_std, tof_mean)):
        floor = _DEGENERATE_RELATIVE_FLOOR * np.maximum(np.abs(mean), 1.0)
        if not np.all(np.isfinite(std)) or np.any(std <= floor):
            raise PerceptionDatasetError(
                "DEGENERATE_SCALE",
                f"{name} training std is {std.tolist()!r} against a relative floor of "
                f"{floor.tolist()!r}; a channel with no variation carries no "
                "information and must not be used as a divisor",
            )

    return {
        "fitted_on": "train split only",
        "n_train_samples": len(train),
        "rgb": {
            "source": "EXR (HDR); the clipped PNG is never used as model input",
            "tonemap": "log1p(clip(x, 0, None)) — parameter-free",
            "normalization": "per-channel z-score",
            "mean": [float(v) for v in rgb_mean],
            "std": [float(v) for v in rgb_std],
            "channel_order": ["R", "G", "B"],
        },
        "tof": {
            "transform": "log1p(clip(x, 0, None)) per feature",
            "normalization": "per-feature z-score",
            "mean": [float(v) for v in tof_mean],
            "std": [float(v) for v in tof_std],
            "feature_order": list(TOF_SCHEMA),
        },
    }


def apply_rgb_preprocessing(path: str | Path, preprocessing: dict[str, Any]) -> np.ndarray:
    """回傳 (3, H, W) float32，channel-first 供 torch 使用。"""
    spec = preprocessing["rgb"]
    array = _rgb_tonemap(_load_exr(path))
    array = (array - np.asarray(spec["mean"])) / np.asarray(spec["std"])
    return np.ascontiguousarray(array.transpose(2, 0, 1), dtype=np.float32)


def apply_tof_preprocessing(path: str | Path, preprocessing: dict[str, Any]) -> np.ndarray:
    """回傳 (4, 500) float32，channel-first（特徵為 channel、時間為長度）。"""
    spec = preprocessing["tof"]
    array = _tof_transform(np.load(path))
    if array.shape != (TOF_RECORDING_POINTS, len(TOF_SCHEMA)):
        raise PerceptionDatasetError(
            "TOF_SHAPE", f"{path} has shape {array.shape}"
        )
    array = (array - np.asarray(spec["mean"])) / np.asarray(spec["std"])
    return np.ascontiguousarray(array.transpose(1, 0), dtype=np.float32)


@dataclass(frozen=True)
class SplitTensors:
    rgb: np.ndarray
    tof: np.ndarray
    labels: np.ndarray
    scenario_ids: list[str]


def load_split(
    manifest: dict[str, Any], preprocessing: dict[str, Any], split: str
) -> SplitTensors:
    """載入一個 split。RGB 與 ToF **依同一列 manifest** 取出，成對性因此保留。"""
    rows = [s for s in manifest["samples"] if s["split"] == split]
    if not rows:
        raise PerceptionDatasetError("EMPTY_SPLIT", f"split {split!r} has no samples")
    rows.sort(key=lambda s: s["scenario_id"])
    return SplitTensors(
        rgb=np.stack([apply_rgb_preprocessing(s["rgb"]["exr_path"], preprocessing) for s in rows]),
        tof=np.stack([apply_tof_preprocessing(s["tof"]["path"], preprocessing) for s in rows]),
        labels=np.asarray([s["class_index"] for s in rows], dtype=np.int64),
        scenario_ids=[s["scenario_id"] for s in rows],
    )
