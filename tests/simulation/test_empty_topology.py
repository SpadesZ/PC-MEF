# PC-MEF Research System source maintenance contract
# 上下游: 直接呼叫 pcmef.simulation.mitsuba_adapter.build_scene_dict()，
#         檢查產出的場景字典；不算圖，因此不需要 LLVM 之外的任何東西。
# 檔案路徑: tests/simulation/test_empty_topology.py
# 產生時間: 2026-08-30 11:45 +08:00
# 版本: v0.1.0
# 功能說明: 確認 Empty 場景是「玻璃殼 + 空氣 + 玻璃殼」，而不是退回 NOTE-029
#           修掉的那根實心玻璃柱；並確認只有**參與介質**是條件式的。
# 模組定位: stage 3 可辨識性論證的地基。整個 GEOMETRY_SURFACE_FOIL 階段
#           之所以能只用 Empty 一類擬合，前提是 Empty 沒有參與介質**但有
#           完整的內部幾何**。這兩件事若有一件不成立，該階段的論證就垮了，
#           因此不能靠讀程式碼的印象，要每次跑測試確認。
# 主要責任:
#   1. 四類皆建立 bottle_interior（內圓柱不是條件式的）
#   2. Empty 的內圓柱是 air-in-bk7，且沒有 participating medium
#   3. 三個有介質的類別確實掛上 participating medium
#   4. 內外半徑對應壁厚 2.0 mm，與 registry 的幾何常數一致
# 維護提醒:
#   - 不得把本檔改成只檢查 Empty；四類一起檢查才擋得住「只有 Empty 被特別
#     處理」這種寫法。
#   - 不得因為「程式碼看起來沒問題」而刪除本檔。NOTE-029 那個缺陷當初也
#     看起來沒問題，它是靠逐峰對照才被發現的。
#   - v0.1.0 新增：AMD-003 的 P1-3 稽核結論固化（NOTE-042）。
# 驗證方式:
#   - py -3.10 -m pytest tests/simulation/test_empty_topology.py -v
#   - py -3.10 -m pcmef.cli calibration preregister --validate
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.simulation.scenario import Geometry, Lighting, ScenarioConfig

pytest.importorskip("mitsuba")

MEDIA = {
    "Empty": {},
    "Water-filled": {"turbidity": {"value": 0.05, "placeholder": True}},
    "Bubbly": {"bubble_density": {"value": 0.30, "placeholder": True}},
    "Misty": {"mist_density": {"value": 0.15, "placeholder": True}},
}


@pytest.fixture(scope="module")
def scenes():
    import mitsuba as mi

    from pcmef.simulation.mitsuba_adapter import Illumination, build_scene_dict

    try:
        mi.set_variant("llvm_ad_rgb")
    except Exception as exc:  # pragma: no cover - 環境缺 LLVM-C.dll
        pytest.skip(f"llvm_ad_rgb unavailable: {exc}")

    built = {}
    for class_label, medium in MEDIA.items():
        config = ScenarioConfig(
            class_label=class_label,
            seed=1,
            geometry=Geometry(),
            lighting=Lighting(preset="nominal"),
            medium_parameters=medium,
            spp=4,
            resolution=(8, 8),
        )
        built[class_label] = build_scene_dict(mi, config, Illumination.ACTIVE_ONLY)
    return built


def test_every_class_builds_an_interior_cylinder(scenes):
    """內圓柱**不是**條件式的。

    NOTE-029 的缺陷正是 Empty 不建內圓柱，於是 bottle_wall 變成一根
    半徑 28.5 mm 的實心 bk7 圓柱，光要穿過 57 mm 玻璃。
    """
    for class_label, scene in scenes.items():
        assert "bottle_interior" in scene, f"{class_label} has no interior cylinder"
        assert scene["bottle_interior"]["type"] == "cylinder"


def test_empty_is_a_glass_shell_with_air_inside(scenes):
    interior = scenes["Empty"]["bottle_interior"]
    assert interior["bsdf"]["int_ior"] == "air"
    assert interior["bsdf"]["ext_ior"] == "bk7"
    assert "interior" not in interior, (
        "Empty must carry no participating medium; an empty bottle contains air, "
        "and that fact does not need calibrating"
    )


def test_wall_thickness_is_two_millimetres(scenes):
    for class_label, scene in scenes.items():
        outer_r = scene["bottle_wall"]["radius"]
        inner_r = scene["bottle_interior"]["radius"]
        assert outer_r == pytest.approx(0.0285), class_label
        assert inner_r == pytest.approx(0.0265), class_label
        assert (outer_r - inner_r) * 1000.0 == pytest.approx(2.0), class_label


@pytest.mark.parametrize("class_label", ["Water-filled", "Bubbly", "Misty"])
def test_the_three_medium_classes_do_carry_a_participating_medium(scenes, class_label):
    """少了這條，把介質整個關掉也會讓上面那條「Empty 沒介質」通過。"""
    interior = scenes[class_label]["bottle_interior"]
    assert "interior" in interior, f"{class_label} lost its participating medium"
    assert interior["interior"]["type"] == "homogeneous"
    assert interior["interior"]["sigma_t"]["value"] > 0.0


def test_only_the_medium_differs_between_empty_and_the_others(scenes):
    """Empty 與 Misty 的內圓柱除了介質之外必須完全相同。

    兩者的 base IOR 都是 air，因此差異應該**只有** participating medium。
    若還有別的差異，代表 Empty 走了一條自己的分支，stage 3 的
    「Empty 只是沒有介質的同一個場景」論證就不成立。
    """
    empty = dict(scenes["Empty"]["bottle_interior"])
    misty = dict(scenes["Misty"]["bottle_interior"])
    misty.pop("interior")
    assert empty == misty
