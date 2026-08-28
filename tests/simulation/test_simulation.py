# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；scenario 契約測試不需要 mitsuba，
#         算圖測試在缺 mitsuba/LLVM 時自動 skip；產物寫在 tmp_path。
# 檔案路徑: tests/simulation/test_simulation.py
# 產生時間: 2026-08-26 09:20 +08:00
# 版本: v0.1.0
# 功能說明: 驗證場景設定會擋下未校準的介質參數進入 formal 模式，
#           以及同一場景真的能算出 RGB 與 optical transient 且兩者共用場景識別碼。
# 模組定位: Batch 4 的驗收測試。它不驗證物理正確性 ——
#           材質尚未校準，本批次只證明管線可跑通且可重現。
# 主要責任:
#   1. ScenarioConfig 的幾何、種子、輸出旗標驗證
#   2. formal 模式必須拒絕 placeholder 介質參數
#   3. scene_hash 的穩定性與敏感度
#   4. RGB 與 transient 共用 scenario_hash（FR-005）
#   5. transient 時間軸為 optical transient time 且與場景尺度相符
#   6. 缺相依時 require_mitsuba 給出可行動的訊息
# 維護提醒:
#   - 不得把算圖測試改成無條件執行；沒裝 mitsuba 的機器要能收集並通過其餘測試。
#   - 不得放寬 formal 模式拒絕 placeholder 那條；它是介質參數未校準時
#     唯一的自動防線。
#   - v0.1.0 新增：首版模擬驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/simulation/test_simulation.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

from pcmef.simulation.scenario import (
    Geometry,
    ScenarioConfig,
    ScenarioConfigError,
)

PLACEHOLDER_MEDIUM = {"bubble_density": {"value": 0.3, "placeholder": True}}


def _mitsuba_available() -> bool:
    """在子行程檢查 mitsuba 是否可用。

    刻意不在 pytest 行程內 import mitsuba：drjit 的 DLL detach 崩潰會讓
    整個 pytest 以非零 exit code 結束，即使所有測試都通過（NOTE-012）。
    """
    probe = subprocess.run(
        [
            sys.executable, "-c",
            "from pcmef.simulation.mitsuba_adapter import require_mitsuba;"
            " require_mitsuba()",
        ],
        capture_output=True,
        cwd=REPO_ROOT,
    )
    return probe.returncode == 0


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_smoke(tmp_path: Path, scenarios: list[dict], **overrides) -> dict:
    """透過 CLI 子行程跑 smoke 並回傳 manifest。

    這同時是使用者實際會走的路徑，因此測到的是真的能用的東西。
    """
    block = {
        "simulation": {
            "variant": "llvm_ad_rgb",
            "spp": 4,
            "resolution": [16, 16],
            "temporal_bins": 32,
            "geometry": {
                "sensor_to_bottle_mm": 50.0,
                "bottle_diameter_mm": 57.0,
                "wall_thickness_mm": 2.0,
            },
            "lighting": {"preset": "nominal", "irradiance": 1.0},
            "scenarios": scenarios,
            **overrides,
        }
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "smoke.yaml"
    config_path.write_text(yaml.safe_dump(block), encoding="utf-8")
    out_dir = tmp_path / "out"

    completed = subprocess.run(
        [
            sys.executable, "-m", "pcmef.cli", "sim", "smoke",
            "--config", str(config_path), "--out", str(out_dir),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    manifest_path = out_dir / "simulation_smoke_manifest.json"
    assert manifest_path.exists(), (
        f"no manifest produced.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["_parent_returncode"] = completed.returncode
    manifest["_out_dir"] = str(out_dir)
    return manifest


needs_mitsuba = pytest.mark.skipif(
    not _mitsuba_available(),
    reason="mitsuba/drjit LLVM backend unavailable on this machine",
)


# ---------------------------------------------------------------------------
# 場景契約
# ---------------------------------------------------------------------------


def test_geometry_defaults_match_the_documented_anchors():
    """幾何預設值取自 SRC-PLAN §2.1，已由真實資料佐證（NOTE-011）。"""
    geometry = Geometry()
    assert geometry.sensor_to_bottle_mm == 50.0
    assert geometry.bottle_diameter_mm == 57.0
    assert geometry.wall_thickness_mm == 2.0
    assert geometry.inner_radius_mm == pytest.approx(26.5)


def test_wall_thicker_than_the_bottle_is_rejected():
    with pytest.raises(ScenarioConfigError, match="no interior"):
        Geometry(bottle_diameter_mm=4.0, wall_thickness_mm=2.0)


def test_formal_mode_rejects_placeholder_medium_parameters():
    """介質散射參數必須來自凍結的校準，不得由預設值頂替。"""
    with pytest.raises(ScenarioConfigError, match="placeholder"):
        ScenarioConfig(
            class_label="Bubbly", seed=1, medium_parameters=PLACEHOLDER_MEDIUM,
            formal=True,
        )


def test_smoke_mode_allows_placeholder_but_flags_it():
    config = ScenarioConfig(
        class_label="Bubbly", seed=1, medium_parameters=PLACEHOLDER_MEDIUM
    )
    assert config.uses_placeholder_medium()
    assert config.placeholder_keys() == {"bubble_density"}
    assert config.medium_value("bubble_density") == 0.3


def _clean_parameter_registry(tmp_path, monkeypatch):
    """給 formal 建構一份真的乾淨 registry，以便單獨驗其他 formal 行為。

    刻意**不** mock 掉 assert_formal_ready()：mock 掉防線本身會讓
    「防線被拿掉」與「防線通過」在測試裡長得一模一樣。
    """
    import yaml

    from pcmef.core import parameters as parameters_module

    path = tmp_path / "clean_registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "registry_version": "test-clean",
                "parameters": [
                    {
                        "name": "only_parameter",
                        "source": "test",
                        "role": "test",
                        "value": 1.0,
                        "provenance_status": "CONFIRMED",
                        "kind": "fixed",
                        "class_scope": "shared",
                        "formal_blocking": True,
                    }
                ],
                "confounded_groups": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(parameters_module, "DEFAULT_REGISTRY_PATH", path)


def test_medium_without_placeholder_flag_is_not_what_blocks_formal(tmp_path, monkeypatch):
    """兩層 formal 防線必須分得開：介質層過了，參數層仍可能擋。

    NOTE-030 之後 formal 建構同時受兩道檢查管制。這一條驗的是「沒有標
    placeholder 的介質參數不會觸發**介質層**那道」—— 因此被擋下來時理由必須
    是參數集，不是 placeholder keys。理由指錯地方，下一棒就會去修錯的東西
    （NOTE-018 是同一個教訓）。
    """
    with pytest.raises(ScenarioConfigError) as error:
        ScenarioConfig(
            class_label="Bubbly", seed=1, medium_parameters={"bubble_density": 0.3},
            formal=True,
        )
    message = str(error.value)
    assert "not formal-ready" in message
    assert "placeholder medium parameters" not in message

    # 參數層滿足後，介質層確實放行。
    _clean_parameter_registry(tmp_path, monkeypatch)
    config = ScenarioConfig(
        class_label="Bubbly", seed=1, medium_parameters={"bubble_density": 0.3},
        formal=True,
    )
    assert not config.uses_placeholder_medium()

    # 而標了 placeholder 的介質參數仍必須被介質層擋下。
    with pytest.raises(ScenarioConfigError, match="placeholder medium parameters"):
        ScenarioConfig(
            class_label="Bubbly", seed=1, medium_parameters=PLACEHOLDER_MEDIUM,
            formal=True,
        )


def test_unknown_class_label_is_rejected():
    with pytest.raises(ScenarioConfigError, match="unknown class_label"):
        ScenarioConfig(class_label="Foamy", seed=1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed": -1},
        {"spp": 0},
        {"resolution": (0, 64)},
        {"render_rgb": False, "render_transient": False},
    ],
)
def test_invalid_scenario_fields_are_rejected(kwargs):
    base = {"class_label": "Empty", "seed": 1}
    base.update(kwargs)
    with pytest.raises(ScenarioConfigError):
        ScenarioConfig(**base)


def test_scene_hash_is_stable_and_sensitive():
    a = ScenarioConfig(class_label="Empty", seed=1)
    b = ScenarioConfig(class_label="Empty", seed=1)
    c = ScenarioConfig(class_label="Empty", seed=2)
    assert a.scene_hash() == b.scene_hash()
    assert a.scene_hash() != c.scene_hash()


def test_scene_hash_ignores_the_formal_flag(tmp_path, monkeypatch):
    """同一場景在 smoke 與 formal 下應是同一場景；模式差異由 manifest 記錄。

    NOTE-030 之後建構 formal config 需要參數層放行，因此先給一份乾淨 registry。
    這條驗的仍是 scene_hash 的性質，不是防線。
    """
    _clean_parameter_registry(tmp_path, monkeypatch)
    plain = ScenarioConfig(class_label="Empty", seed=1)
    formal = ScenarioConfig(class_label="Empty", seed=1, formal=True)
    assert plain.scene_hash() == formal.scene_hash()
    assert "formal" not in plain.to_dict()


def test_from_mapping_parses_the_documented_shape():
    config = ScenarioConfig.from_mapping(
        {
            "scenario": {
                "class_label": "Misty",
                "seed": 1004,
                "geometry": {"sensor_to_bottle_mm": 50.0},
                "medium": {"mist_density": 0.15},
                "outputs": {"rgb": True, "transient": False},
            }
        }
    )
    assert config.medium_preset.value == "misty"
    assert config.render_transient is False


# ---------------------------------------------------------------------------
# 相依診斷
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 實際算圖（全部經由 CLI 子行程，pytest 本身不載入 mitsuba）
# ---------------------------------------------------------------------------


@needs_mitsuba
def test_smoke_run_exits_cleanly_despite_worker_teardown_crash(tmp_path):
    """NOTE-012：父行程必須回報 0，子行程的崩潰不得污染判定。"""
    manifest = _run_smoke(tmp_path, [{"class_label": "Empty", "seed": 7, "medium": {}}])
    assert manifest["_parent_returncode"] == 0
    assert manifest["counts"] == {"total": 1, "ok": 1, "failed": 0}


@needs_mitsuba
def test_rgb_and_transient_share_one_scenario_hash(tmp_path):
    """SRC-SAI FR-005：同一 scenario 產出的 RGB 與 transient 必須來自同一場景。"""
    manifest = _run_smoke(tmp_path, [{"class_label": "Empty", "seed": 7, "medium": {}}])
    scenario = manifest["scenarios"][0]
    assert scenario["rgb"]["scenario_hash"] == scenario["transient"]["scenario_hash"]
    assert scenario["scenario_hash"] == scenario["rgb"]["scenario_hash"]


@needs_mitsuba
def test_transient_has_energy_in_a_physically_placed_window(tmp_path):
    """時間窗口必須涵蓋實際回波；全零代表 binning 設錯（例如沿用房間尺度）。"""
    manifest = _run_smoke(
        tmp_path, [{"class_label": "Bubbly", "seed": 11, "medium": {}}]
    )
    transient = manifest["scenarios"][0]["transient"]
    assert transient["shape"] == [16, 16, 32, 3]
    assert transient["total_energy"] > 0.0
    assert transient["nonzero_bin_ratio"] > 0.1


@needs_mitsuba
def test_transient_time_axis_matches_the_scene_scale(tmp_path):
    """5 公分場景的光飛行時間應在次奈秒量級；毫米/公尺單位錯置會差三個數量級。"""
    manifest = _run_smoke(tmp_path, [{"class_label": "Empty", "seed": 3, "medium": {}}])
    axis_path = manifest["scenarios"][0]["transient"]["outputs"][
        "optical_transient_time_axis"
    ]
    axis = np.load(axis_path)
    assert axis.shape == (32,)
    assert 1e-11 < axis.min() < 1e-8
    assert 1e-11 < axis.max() < 1e-8
    assert np.all(np.diff(axis) > 0)
    assert manifest["scenarios"][0]["transient"]["time_axis"] == "optical_transient_time"


@needs_mitsuba
def test_renders_are_reproducible_for_the_same_seed(tmp_path):
    """E1-G03 的核心要求：同樣的輸入必須算出同樣的結果。"""
    scenario = [{"class_label": "Misty", "seed": 99, "medium": {}}]
    first = _run_smoke(tmp_path / "a", scenario)
    second = _run_smoke(tmp_path / "b", scenario)
    assert first["scenarios"][0]["transient"]["total_energy"] == pytest.approx(
        second["scenarios"][0]["transient"]["total_energy"], rel=1e-9
    )
    # 場景識別碼與輸出路徑無關，因此跨 run 必須完全相同。
    assert (
        first["scenarios"][0]["scenario_hash"]
        == second["scenarios"][0]["scenario_hash"]
    )


@needs_mitsuba
def test_all_four_classes_render_with_distinct_energy(tmp_path):
    """四類場景必須都能算出來，且不是同一個場景被算了四次。"""
    manifest = _run_smoke(
        tmp_path,
        [
            {"class_label": "Empty", "seed": 1001, "medium": {}},
            {"class_label": "Water-filled", "seed": 1002, "medium": {}},
            {"class_label": "Bubbly", "seed": 1042, "medium": {}},
            {"class_label": "Misty", "seed": 1004, "medium": {}},
        ],
    )
    assert manifest["counts"] == {"total": 4, "ok": 4, "failed": 0}
    hashes = {s["scenario_hash"] for s in manifest["scenarios"]}
    assert len(hashes) == 4


@needs_mitsuba
def test_transient_window_is_not_truncated(tmp_path):
    """NOTE-013：峰值貼在窗邊代表光還在抵達時窗就關了，FWHM 會恆為 0。

    nonzero_bin_ratio 看不出這件事（截斷後仍有七成 bin 帶能量），
    真正的徵兆是 peak_bin 與 edge_fraction。
    """
    manifest = _run_smoke(tmp_path, [{"class_label": "Empty", "seed": 1, "medium": {}}])
    transient = manifest["scenarios"][0]["transient"]
    bins = transient["binning"]["temporal_bins"]
    assert transient["peak_bin"] < bins - 2, "peak must not sit on the window edge"
    assert transient["edge_fraction"] < 0.5


@needs_mitsuba
def test_surrogate_can_consume_the_rendered_transient(tmp_path):
    """Batch 4 與 Batch 5 的整合點：算出來的 transient 必須能餵進 surrogate。

    初版時間窗截斷回波，FWHM=0，Sigma 映射拒絕受理 —— 該缺陷是在這個
    整合點才被發現的，因此把它固定成測試。
    """
    from pcmef.surrogate.calibration import PLACEHOLDER_SMOKE_CALIBRATION
    from pcmef.surrogate.single_acquisition import SensorSurrogate
    from pcmef.surrogate.temporal_model import TemporalModel

    manifest = _run_smoke(tmp_path, [{"class_label": "Empty", "seed": 2, "medium": {}}])
    outputs = manifest["scenarios"][0]["transient"]["outputs"]
    transient = np.load(outputs["optical_transient"])
    axis = np.load(outputs["optical_transient_time_axis"])

    surrogate = SensorSurrogate(PLACEHOLDER_SMOKE_CALIBRATION)
    observables = surrogate.observe(transient, axis)
    assert observables.fwhm_s > 0, "a truncated window yields FWHM=0"

    recording = TemporalModel(surrogate).generate_recording(
        transient, axis,
        sample_interval_s=0.082,
        sample_interval_source="edge_impulse_12.19512Hz",
        seed=1042, n_samples=64,
    )
    assert recording.values.shape == (64, 4)
    assert np.all(np.isfinite(recording.values))
    assert np.all(recording.values.std(axis=0) > 0)


@needs_mitsuba
def test_manifest_carries_dependency_versions_and_claim_boundary(tmp_path):
    """E1-G03 的證據必須能追到當時用的是哪個版本。"""
    manifest = _run_smoke(tmp_path, [{"class_label": "Empty", "seed": 5, "medium": {}}])
    assert manifest["gate"] == "E1-G03"
    deps = manifest["dependencies"]
    assert deps["mitsuba"] != "unavailable"
    assert deps["mitransient"] != "unavailable"
    assert "not calibrated" in manifest["claim_boundary"]


@needs_mitsuba
def test_formal_mode_refuses_placeholder_medium_through_the_cli(tmp_path):
    """placeholder 介質參數不得經由 CLI 溜進 formal run。"""
    block = {
        "simulation": {
            "spp": 4,
            "resolution": [8, 8],
            "scenarios": [
                {
                    "class_label": "Bubbly",
                    "seed": 1,
                    "medium": {"bubble_density": {"value": 0.3, "placeholder": True}},
                }
            ],
        }
    }
    config_path = tmp_path / "formal.yaml"
    config_path.write_text(yaml.safe_dump(block), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable, "-m", "pcmef.cli", "--formal", "sim", "smoke",
            "--config", str(config_path), "--out", str(tmp_path / "out"),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert completed.returncode == 2
    assert "placeholder" in completed.stderr
