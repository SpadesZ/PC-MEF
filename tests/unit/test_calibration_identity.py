# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.experiments.calibration_identity 與 repo 內已凍結的
#         CAL-PREREG-003 / CAL-STAGE0-001 / CAL-SF-001 / initial_simulation.lock；
#         不讀 calibration partition 的數值，也不碰 held-out。
#         由 pytest 收集執行，不寫出任何 artifact。
# 檔案路徑: tests/unit/test_calibration_identity.py
# 產生時間: 2026-08-30 21:05 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 formal calibration 的身分閘門真的擋得住漂移，且它導出的
#           搜尋空間與凍結的 stage 0 判定逐項相符。
# 模組定位: formal calibration 前提的驗收。它不驗證校準結果，只驗證
#           「這次執行宣稱依附的凍結物，真的就是它讀到的那些」。
# 主要責任:
#   1. test_live_repository_identity_verifies() 現況必須通過閘門
#   2. test_stage_dimensions_match_the_frozen_admission() 維度只能來自 stage 0
#   3. test_noise_sigma_uses_the_derived_feasible_bound() 上界取自 artifact
#   4. test_declared_cells_are_a_subset_of_the_sixteen() 宣告格子必在 16 項內
#   5. test_identity_gate_rejects_*() 各類漂移一律 fail closed
# 維護提醒:
#   - 不得把期望雜湊改成「現在算出來的值」讓測試通過；那樣測試就永遠會過，
#     而它存在的唯一理由就是會不過。
#   - 不得在本檔讀取任何 recording 數值；身分驗證的前提就是它先於讀值。
#   - v0.1.0 新增：首版身分閘門驗收（CAL-PREREG-003 formal calibration）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_identity.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.experiments.calibration_identity import (
    EXPECTED_PREREG_PAYLOAD_HASH,
    EXPECTED_RAW_CALIBRATION_DATA_HASH,
    EXPECTED_SF_PAYLOAD_HASH,
    EXPECTED_STAGE0_PAYLOAD_HASH,
    STAGE_ORDER,
    IdentityError,
    all_cells,
    load_frozen_identity,
    stage_bounds,
    stage_declared_cells,
    stage_dimensions,
    stage_held_parameters,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: stage 0 判定的七個 admitted 參數。寫在測試裡是為了讓「有人偷偷加了一個
#: 自由度」變成一條紅線，而不是一個沒有人會注意到的差異。
SEVEN_CALIBRATED = {
    "sensor.fov_deg",
    "ambient_energy_to_mcps",
    "ambient_jitter_relative",
    "signal_energy_to_mcps",
    "noise_relative_sigma",
    "sigma_width_to_mm",
    "sigma_multipath_weight",
}


@pytest.fixture(scope="module")
def identity():
    # E1 final 之後 heldout_access_count 是 1。本檔驗的是**凍結身分**，
    # 不是那道閘門；閘門另有 test_identity_gate_rejects_a_nonzero_heldout_access_count
    # 專門盯著，且它用的是一個乾淨的假 repo。
    return load_frozen_identity("freeze", REPO_ROOT, allow_opened_heldout=True)


def test_live_repository_identity_verifies(identity):
    assert identity.prereg_hash == EXPECTED_PREREG_PAYLOAD_HASH
    assert identity.stage0_hash == EXPECTED_STAGE0_PAYLOAD_HASH
    assert identity.sf_hash == EXPECTED_SF_PAYLOAD_HASH
    assert identity.raw_calibration_data_hash == EXPECTED_RAW_CALIBRATION_DATA_HASH
    # E1 final 之前是 0，之後恰為 1。大於 1 永遠是錯的。
    assert identity.heldout_access_count in (0, 1)


def test_s_f_comes_from_the_frozen_record_and_is_usable(identity):
    assert set(identity.s_f) == set(TOF_SCHEMA)
    assert all(v > 0 for v in identity.s_f.values())
    frozen = json.loads(
        (REPO_ROOT / "freeze" / "calibration" / "CAL-SF-001.sf.json").read_text(
            encoding="utf-8"
        )
    )
    assert identity.s_f == {k: float(v) for k, v in frozen["payload"]["s_f"].items()}


def test_stage_dimensions_match_the_frozen_admission(identity):
    """維度只能來自 stage 0 的 admitted，不是預註冊的候選清單。"""
    status = identity.stage0["payload"]["stage_status"]
    collected: set[str] = set()
    for stage_id in STAGE_ORDER:
        dimensions = stage_dimensions(identity, stage_id)
        assert dimensions == status[stage_id]["admitted"]
        collected.update(dimensions)
    assert collected == SEVEN_CALIBRATED


def test_zero_dimensional_stage_is_named_not_skipped(identity):
    assert stage_dimensions(identity, "SCENE_PARTICIPATING_MEDIA") == []
    assert sorted(stage_held_parameters(identity, "SCENE_PARTICIPATING_MEDIA")) == [
        "medium.bubble_density",
        "medium.mist_density",
        "medium.turbidity",
    ]
    status = identity.stage0["payload"]["stage_status"]["SCENE_PARTICIPATING_MEDIA"]
    assert status["optimizer_run"] is False
    assert status["status"] == "NO_FREE_PARAMETERS"
    assert status["physical_model_retained"] is True


def test_noise_sigma_uses_the_derived_feasible_bound(identity):
    """上界必須是 CAL-STAGE0-001 導出的那個值，不是手抄的四捨五入常數。"""
    bounds = stage_bounds(identity, stage_dimensions(identity, "MAPPING_SIGNAL"))
    derived = identity.stage0["payload"]["feasible_domains"]["noise_relative_sigma"]
    lo, hi = bounds["noise_relative_sigma"]
    assert (lo, hi) == tuple(derived["swept"])
    assert hi == derived["derivation"]["high"]["edge"]
    # 登記界線是 0.5；導出的域必為其真子集。
    registered = identity.stage0["payload"]["resolved_bounds"]["noise_relative_sigma"]
    assert hi < registered[1]


def test_every_stage_bound_matches_the_frozen_resolved_bounds(identity):
    registered = identity.stage0["payload"]["resolved_bounds"]
    for stage_id in STAGE_ORDER:
        dimensions = stage_dimensions(identity, stage_id)
        for name, (lo, hi) in stage_bounds(identity, dimensions).items():
            frozen_lo, frozen_hi = registered[name]
            assert lo >= frozen_lo and hi <= frozen_hi
            assert lo < hi


def test_declared_cells_are_a_subset_of_the_sixteen(identity):
    sixteen = set(all_cells())
    assert len(sixteen) == len(CLASS_ORDER) * len(TOF_SCHEMA) == 16
    for stage_id in STAGE_ORDER:
        cells = stage_declared_cells(identity, stage_id)
        assert cells, f"{stage_id} declares no optimised cell"
        assert set(cells) <= sixteen


def test_stage_order_is_the_frozen_order(identity):
    frozen = [str(s["id"]) for s in identity.protocol["stagewise"]]
    assert list(STAGE_ORDER) == frozen


# ---------------------------------------------------------------------------
# 漂移一律 fail closed
# ---------------------------------------------------------------------------


def _clone_freeze(tmp_path: Path) -> Path:
    target = tmp_path / "freeze"
    shutil.copytree(REPO_ROOT / "freeze", target)
    return target


def _rewrite(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "relative, mutate, reason",
    [
        pytest.param(
            "calibration/CAL-SF-001.sf.json",
            lambda d: d["payload"]["s_f"].__setitem__("distance_mm", 99.0),
            "CAL_SF_001_DRIFT",
            id="s_f-edited-in-place",
        ),
        pytest.param(
            "calibration/CAL-SF-001.sf.json",
            lambda d: d.__setitem__("payload_hash", "0" * 64),
            "CAL_SF_001_DRIFT",
            id="s_f-hash-replaced",
        ),
        pytest.param(
            "stages/CAL-STAGE0-001.stage0.json",
            lambda d: d["payload"].__setitem__("outcome", "STAGE0_ADJUDICATION_REQUIRED"),
            "FROZEN_PROTOCOL_DRIFT",
            id="stage0-not-complete",
        ),
        pytest.param(
            "preregistrations/CAL-PREREG-003.prereg.json",
            lambda d: d["payload"].__setitem__("protocol_hash", "0" * 64),
            "FROZEN_PROTOCOL_DRIFT",
            id="prereg-edited",
        ),
    ],
)
def test_identity_gate_rejects_frozen_record_drift(tmp_path, relative, mutate, reason):
    freeze_dir = _clone_freeze(tmp_path)
    _rewrite(freeze_dir / relative, mutate)
    with pytest.raises(IdentityError) as excinfo:
        load_frozen_identity(freeze_dir, REPO_ROOT)
    assert excinfo.value.reason == reason


def test_identity_gate_rejects_a_missing_sf_record(tmp_path):
    freeze_dir = _clone_freeze(tmp_path)
    (freeze_dir / "calibration" / "CAL-SF-001.sf.json").unlink()
    with pytest.raises(IdentityError) as excinfo:
        load_frozen_identity(freeze_dir, REPO_ROOT)
    assert excinfo.value.reason == "CAL_SF_001_DRIFT"


def test_identity_gate_rejects_a_nonzero_heldout_access_count(tmp_path):
    """held-out 只要被開過一次，calibration 就不得再進行。"""
    root = tmp_path / "repo"
    (root / "data" / "splits").mkdir(parents=True)
    for name in ("split_registry.json", "calibration_access_ledger.json"):
        shutil.copy(REPO_ROOT / "data" / "splits" / name, root / "data" / "splits" / name)
    # ERR-001 的證據檔以 repo_root 為基準解析，因此假 repo 也必須帶著它們。
    for relative in (
        "outputs/repro_a/simulation_smoke_manifest.json",
        "outputs/repro_b/simulation_smoke_manifest.json",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / relative, destination)
    _rewrite(
        root / "data" / "splits" / "split_registry.json",
        lambda d: d.__setitem__("heldout_access_count", 1),
    )
    with pytest.raises(IdentityError) as excinfo:
        load_frozen_identity(REPO_ROOT / "freeze", root)
    assert excinfo.value.reason == "HELDOUT_ACCESS"


def test_unknown_stage_is_rejected(identity):
    with pytest.raises(IdentityError):
        stage_dimensions(identity, "NOT_A_STAGE")
