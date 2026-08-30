# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.perception.dataset；以合成 manifest 與 tmp_path 驗證，
#         不算圖、不讀真實資料、不碰 FORMAL_E1_FINAL。由 pytest 收集執行。
# 檔案路徑: tests/unit/test_perception_dataset.py
# 產生時間: 2026-08-31 12:40 +08:00
# 版本: v0.1.0
# 功能說明: 驗證切分是決定性的、正規化常數只由 train 算出、
#           以及 leakage 稽核如實回報 scene family 的重疊。
# 模組定位: perception 資料層的驗收。它不驗證模型表現，只驗證
#           「分數若產生了，它的資料前提是乾淨的」。
# 主要責任:
#   1. test_split_plan_is_deterministic_and_disjoint() 切分可重現且不重疊
#   2. test_preprocessing_is_fitted_on_train_only() 常數不看 val/test
#   3. test_leakage_audit_reports_family_overlap_honestly() 不粉飾
#   4. test_physical_scene_family_ignores_seed() 家族身分不含 seed
#   5. test_degenerate_scale_is_rejected() 零標準差不得被拿去除
# 維護提醒:
#   - 不得把 scenario_id 不重疊當成「沒有 leakage」而放寬本檔的斷言；
#     scene family 才是「模型沒看過這個場景」的判準。
#   - 不得讓正規化常數看到 val/test；那種 leakage 不會讓任何測試失敗，
#     只會讓分數變好看，因此必須由測試主動盯著。
#   - v0.1.0 新增：perception 資料層驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_perception_dataset.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_RECORDING_POINTS, TOF_SCHEMA
from pcmef.perception.dataset import (
    SPLIT_NAMES,
    PerceptionDatasetError,
    audit_split_leakage,
    fit_preprocessing,
    physical_scene_family,
    split_plan,
)


def test_split_plan_is_deterministic_and_disjoint():
    first, second = split_plan(100), split_plan(100)
    assert first == second
    ids = [set(v) for v in first.values()]
    assert not ids[0] & ids[1] and not ids[0] & ids[2] and not ids[1] & ids[2]
    assert sum(len(v) for v in first.values()) == 100


def test_split_plan_rejects_an_empty_split():
    with pytest.raises(PerceptionDatasetError) as excinfo:
        split_plan(2)
    assert excinfo.value.reason == "EMPTY_SPLIT"


def _scene(**overrides):
    base = {
        "class_label": "Empty",
        "medium_preset": "empty",
        "geometry": {"sensor_to_bottle_mm": 50.0, "lateral_offset_mm": 0.0},
        "lighting": {"preset": "nominal", "irradiance": 1.0},
        "medium_parameters": {},
        "scene_constants_applied": {"sensor.fov_deg": 45.0},
        "seed": 1,
        "spp": 16,
        "rgb_render_spp": 16,
        "resolution": [64, 64],
        "outputs": {"rgb": True},
    }
    base.update(overrides)
    return base


def test_physical_scene_family_ignores_numerical_settings():
    """seed 與 spp 都是數值設定，不是場景的物理狀態。

    這一條直接支撐 RGB spp 解耦：把 RGB 的取樣數調高之後，物理場景身分
    必須完全不變，否則 family split 會把同一個場景切成兩族。
    """
    base = _scene()
    for key, value in (("seed", 999), ("spp", 32), ("rgb_render_spp", 4096),
                       ("resolution", [128, 128])):
        assert physical_scene_family(_scene(**{key: value})) == physical_scene_family(base), (
            f"{key} must not change the physical family"
        )


def test_physical_scene_family_changes_with_real_physics():
    base = physical_scene_family(_scene())
    moved = physical_scene_family(
        _scene(geometry={"sensor_to_bottle_mm": 50.0, "lateral_offset_mm": 3.0})
    )
    brighter = physical_scene_family(
        _scene(lighting={"preset": "nominal", "irradiance": 4.0})
    )
    other_fov = physical_scene_family(
        _scene(scene_constants_applied={"sensor.fov_deg": 40.0})
    )
    assert len({base, moved, brighter, other_fov}) == 4


def test_physical_scene_family_rejects_missing_physical_keys():
    """白名單缺項時必須拒絕，而不是安靜地少算一個物理量。"""
    incomplete = _scene()
    del incomplete["lighting"]
    with pytest.raises(PerceptionDatasetError) as excinfo:
        physical_scene_family(incomplete)
    assert excinfo.value.reason == "SCENE_KEYS"


def test_family_split_plan_keeps_families_whole():
    from pcmef.perception.dataset import family_split_plan

    plan = family_split_plan(20, {"train": 12, "val": 4, "test": 4})
    assert [len(v) for v in plan.values()] == [12, 4, 4]
    seen = set()
    for indices in plan.values():
        assert not set(indices) & seen
        seen |= set(indices)
    assert seen == set(range(20))


def test_family_split_plan_rejects_a_mismatched_total():
    from pcmef.perception.dataset import family_split_plan

    with pytest.raises(PerceptionDatasetError) as excinfo:
        family_split_plan(20, {"train": 10, "val": 4, "test": 4})
    assert excinfo.value.reason == "INVALID_FAMILY_SPLIT"


def _manifest(tmp_path, families_per_class: int = 1):
    """合成 manifest：每類 6 個 scenario，train/val/test 各 2 個。"""
    samples = []
    for class_index, class_label in enumerate(CLASS_ORDER):
        for index in range(6):
            split = SPLIT_NAMES[index // 2]
            rgb = tmp_path / f"{class_label}_{index}.npy"
            tof = tmp_path / f"{class_label}_{index}_tof.npy"
            rng = np.random.default_rng(class_index * 100 + index)
            np.save(rgb, rng.normal(5.0, 1.0, size=(8, 8, 3)))
            np.save(tof, np.abs(rng.normal(10.0, 1.0,
                                           size=(TOF_RECORDING_POINTS, len(TOF_SCHEMA)))))
            samples.append({
                "scenario_id": f"{class_label}_{index}",
                "class_label": class_label,
                "class_index": class_index,
                "split": split,
                "scenario_index": index,
                "seed_family": {"scenario_seed": class_index * 100 + index},
                "physical_scene_family": f"{class_label}_family{index % families_per_class}",
                "rgb": {"exr_path": str(rgb)},
                "tof": {"path": str(tof)},
            })
    return {"samples": samples}


def test_leakage_audit_reports_family_overlap_honestly(tmp_path):
    """每類只有一個 family 時，稽核必須明說 family 有重疊。"""
    audit = audit_split_leakage(_manifest(tmp_path, families_per_class=1))
    assert audit["scenario_id_disjoint"] is True
    assert audit["scenario_seed_disjoint"] is True
    assert audit["physical_scene_family_disjoint"] is False
    assert audit["distinct_physical_families_total"] == len(CLASS_ORDER)
    assert all(v == 1 for v in audit["distinct_physical_families_per_class"].values())
    assert "NOT generalisation" in audit["interpretation"]


def test_leakage_audit_can_report_a_clean_family_split(tmp_path):
    """family 夠多且不跨 split 時，稽核必須回報 True —— 否則它永遠說 False，
    那樣它就不是一個檢查。"""
    manifest = _manifest(tmp_path, families_per_class=6)
    audit = audit_split_leakage(manifest)
    assert audit["physical_scene_family_disjoint"] is True
    assert audit["distinct_physical_families_total"] == len(CLASS_ORDER) * 6


def test_preprocessing_is_fitted_on_train_only(tmp_path, monkeypatch):
    """把 val/test 的值改掉，正規化常數必須完全不動。"""
    from pcmef.perception import dataset as module

    monkeypatch.setattr(module, "_load_exr", lambda p: np.load(p))
    manifest = _manifest(tmp_path)
    before = fit_preprocessing(manifest)

    for sample in manifest["samples"]:
        if sample["split"] != "train":
            np.save(sample["rgb"]["exr_path"], np.full((8, 8, 3), 1e6))
            np.save(sample["tof"]["path"],
                    np.full((TOF_RECORDING_POINTS, len(TOF_SCHEMA)), 1e6))
    after = fit_preprocessing(manifest)

    assert before["rgb"] == after["rgb"]
    assert before["tof"] == after["tof"]
    assert before["fitted_on"] == "train split only"


def test_degenerate_scale_is_rejected(tmp_path, monkeypatch):
    """標準差為 0 的通道不得被拿去做除數。"""
    from pcmef.perception import dataset as module

    monkeypatch.setattr(module, "_load_exr", lambda p: np.load(p))
    manifest = _manifest(tmp_path)
    for sample in manifest["samples"]:
        if sample["split"] == "train":
            np.save(sample["rgb"]["exr_path"], np.full((8, 8, 3), 3.0))
    with pytest.raises(PerceptionDatasetError) as excinfo:
        fit_preprocessing(manifest)
    assert excinfo.value.reason == "DEGENERATE_SCALE"


def test_preprocessing_records_that_exr_is_the_input(tmp_path, monkeypatch):
    """PNG 是 clipped 的；正式輸入必須是 EXR，而且要寫在 artifact 上。"""
    from pcmef.perception import dataset as module

    monkeypatch.setattr(module, "_load_exr", lambda p: np.load(p))
    spec = fit_preprocessing(_manifest(tmp_path))["rgb"]
    assert "EXR" in spec["source"]
    assert "PNG is never used" in spec["source"]
    assert spec["tonemap"].startswith("log1p")
