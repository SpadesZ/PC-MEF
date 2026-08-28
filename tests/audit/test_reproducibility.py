# PC-MEF Research System source maintenance contract
# 上下游: 測 pcmef.audit.reproducibility 的 RP-01..RP-04；
#         以合成 manifest 與 .npy 驅動，不需要算圖。
# 檔案路徑: tests/audit/test_reproducibility.py
# 產生時間: 2026-08-28 21:15 +08:00
# 版本: v0.1.0
# 功能說明: 驗證可重現性比對真的會抓到差異 —— 身分欄位不同、張量不同、
#           estimator 與選定證據分家，三種都必須 FAIL。
# 模組定位: NOTE-038 的反向驗收。它「不是」實際 renderer 決定性的測試 ——
#           那需要真的算兩次，由 cli 的 audit reproducibility 負責。
# 主要責任:
#   1. 完全相同的兩次執行必須 PASS（否則負向測試是空的）
#   2. 張量差一個位元必須 FAIL，且報出量化差異
#   3. runtime_s 與 outputs 不同**不得**造成 FAIL
# 維護提醒:
#   - 不得放寬 REPRODUCIBILITY_TOLERANCE 來讓測試過；容忍值是量出來的。
#   - 不得刪除 test_identical_runs_pass；少了它其餘測試會在
#     「永遠 FAIL」的情況下通過。
#   - v0.1.0 新增：首版（NOTE-038）。
# 驗證方式:
#   - py -3.10 -m pytest tests/audit/test_reproducibility.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import numpy as np
import pytest

from pcmef.audit.reproducibility import (
    REPRODUCIBILITY_TOLERANCE,
    audit_reproducibility,
)
from pcmef.audit.result import CheckStatus


def _manifest(run_name: str, runtime: float) -> dict:
    return {
        "content_hash": "c" * 64,
        "run_identity_hash": "r" * 64,
        "manifest_hash": f"m-{run_name}",  # 刻意每次不同，模擬真實情況
        "parameter_registry": {
            "parameter_set_hash": "p" * 64,
            "registry_version": "v0.3.0",
        },
        "surrogate_identity": {
            "estimator": "leading_edge",
            "preregistration_hash": "g" * 64,
            "calibration_hash": "k" * 64,
        },
        "dependencies": {
            "mitsuba": "3.8.0", "drjit": "1.3.1", "mitransient": "1.3.0",
            "variant": "llvm_ad_rgb", "python": "3.10.11",
        },
        "scenarios": [
            {
                "scenario_id": "smoke_empty_1001",
                "scenario_hash": "s" * 64,
                "status": "OK",
                "rgb": {"integrator": "path", "max_depth": 12, "runtime_s": runtime,
                        "outputs": {"rgb_exr": f"outputs/{run_name}/rgb.exr"}},
                "transient": {
                    "seed": 1001, "spp": 16, "integrator": "transient_path",
                    "illumination": "split_active_ambient",
                    "binning": {
                        "temporal_bins": 128, "start_opl_m": 0.0729,
                        "bin_width_opl_m": 0.0033875,
                    },
                    "runtime_s": runtime,
                    "outputs": {"optical_transient": f"outputs/{run_name}/t.npy"},
                },
            }
        ],
    }


def _build(tmp_path, run_name, runtime=0.1, tweak=None):
    root = tmp_path / run_name
    scenario = root / "smoke_empty_1001"
    scenario.mkdir(parents=True)
    (root / "simulation_smoke_manifest.json").write_text(
        json.dumps(_manifest(run_name, runtime)), encoding="utf-8"
    )
    rng = np.random.default_rng(7)
    active = rng.random((2, 2, 128, 3)).astype(np.float32)
    if tweak is not None:
        active = active.copy()
        active[0, 0, 0, 0] += tweak
    np.save(scenario / "transient.npy", active)
    np.save(scenario / "transient_ambient.npy", np.full((2, 2, 128, 3), 0.01, np.float32))
    np.save(scenario / "transient_time.npy", np.arange(128, dtype=np.float64))
    return root


@pytest.fixture
def selection(monkeypatch, tmp_path):
    """RP-04 需要一份與程式一致的 selection artifact。"""
    from pcmef.surrogate.distance import SELECTED_ESTIMATOR
    from pcmef.surrogate.estimator_selection import preregistration_hash

    path = tmp_path / "outputs" / "estimator_select"
    path.mkdir(parents=True)
    (path / "estimator_selection.json").write_text(
        json.dumps({
            "outcome": "SELECTED",
            "selected": SELECTED_ESTIMATOR.name,
            "preregistration_hash": preregistration_hash(),
        }),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return path


def _statuses(report):
    return {c.identifier: c.status for c in report.results}


def test_identical_runs_pass(tmp_path, selection):
    """沒有這一條，底下的負向測試可能只是「永遠 FAIL」。"""
    a = _build(tmp_path, "run_a", runtime=0.10)
    b = _build(tmp_path, "run_b", runtime=0.25)
    report = audit_reproducibility(a, b)
    assert _statuses(report) == {
        "RP-01": CheckStatus.PASS, "RP-02": CheckStatus.PASS,
        "RP-03": CheckStatus.PASS, "RP-04": CheckStatus.PASS,
    }


def test_runtime_and_output_paths_do_not_break_reproducibility(tmp_path, selection):
    """NOTE-038 的核心：wall-clock 與路徑不同不算不可重現。"""
    a = _build(tmp_path, "run_a", runtime=0.01)
    b = _build(tmp_path, "run_b", runtime=9.99)
    report = audit_reproducibility(a, b)
    assert _statuses(report)["RP-01"] is CheckStatus.PASS
    manifest_a = json.loads((a / "simulation_smoke_manifest.json").read_text("utf-8"))
    manifest_b = json.loads((b / "simulation_smoke_manifest.json").read_text("utf-8"))
    # 前提檢查：這兩份 manifest 的 manifest_hash 確實不同，
    # 否則本測試沒有驗到任何東西。
    assert manifest_a["manifest_hash"] != manifest_b["manifest_hash"]


def test_a_single_changed_element_fails_and_is_quantified(tmp_path, selection):
    a = _build(tmp_path, "run_a")
    b = _build(tmp_path, "run_b", tweak=1e-3)
    report = audit_reproducibility(a, b)
    checks = {c.identifier: c for c in report.results}
    assert checks["RP-03"].status is CheckStatus.FAIL
    findings = " ".join(checks["RP-03"].findings)
    assert "max_abs_diff" in findings
    assert "exceeds tolerance" in findings


def test_identity_field_mismatch_fails(tmp_path, selection):
    a = _build(tmp_path, "run_a")
    b = _build(tmp_path, "run_b")
    payload = json.loads((b / "simulation_smoke_manifest.json").read_text("utf-8"))
    payload["parameter_registry"]["parameter_set_hash"] = "different"
    (b / "simulation_smoke_manifest.json").write_text(json.dumps(payload), "utf-8")

    report = audit_reproducibility(a, b)
    checks = {c.identifier: c for c in report.results}
    assert checks["RP-01"].status is CheckStatus.FAIL
    assert any("parameter_set_hash" in f for f in checks["RP-01"].findings)


def test_a_missing_identity_field_is_not_a_silent_pass(tmp_path, selection):
    """兩邊都缺同一個欄位不是「相等」，是沒比到 —— 必須 FAIL。

    這條測試來自一個真實缺陷：SCENARIO_IDENTITY_FIELDS 原本把 binning 寫成
    平鋪路徑，兩邊都解析成 None 而「相等」，於是三個欄位形式上通過、
    實際完全沒有比對（NOTE-038）。
    """
    a = _build(tmp_path, "run_a")
    b = _build(tmp_path, "run_b")
    for run in (a, b):
        path = run / "simulation_smoke_manifest.json"
        payload = json.loads(path.read_text("utf-8"))
        del payload["scenarios"][0]["transient"]["binning"]
        path.write_text(json.dumps(payload), encoding="utf-8")

    report = audit_reproducibility(a, b)
    checks = {c.identifier: c for c in report.results}
    assert checks["RP-02"].status is CheckStatus.FAIL
    assert any("missing" in f for f in checks["RP-02"].findings)


def test_seed_mismatch_fails(tmp_path, selection):
    a = _build(tmp_path, "run_a")
    b = _build(tmp_path, "run_b")
    payload = json.loads((b / "simulation_smoke_manifest.json").read_text("utf-8"))
    payload["scenarios"][0]["transient"]["seed"] = 9999
    (b / "simulation_smoke_manifest.json").write_text(json.dumps(payload), "utf-8")

    report = audit_reproducibility(a, b)
    checks = {c.identifier: c for c in report.results}
    assert checks["RP-02"].status is CheckStatus.FAIL
    assert any("seed" in f for f in checks["RP-02"].findings)


def test_estimator_drift_from_the_selection_artifact_fails(tmp_path, selection):
    """程式改了 estimator 卻沒重跑 selection，必須被抓到。"""
    artifact = selection / "estimator_selection.json"
    payload = json.loads(artifact.read_text("utf-8"))
    payload["selected"] = "PEAK"
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    a = _build(tmp_path, "run_a")
    b = _build(tmp_path, "run_b")
    report = audit_reproducibility(a, b)
    checks = {c.identifier: c for c in report.results}
    assert checks["RP-04"].status is CheckStatus.FAIL
    assert any("PEAK" in f for f in checks["RP-04"].findings)


def test_tolerance_is_bitwise_by_default():
    """容忍值放寬必須是刻意的動作，不能悄悄發生。"""
    assert all(value == 0.0 for value in REPRODUCIBILITY_TOLERANCE.values()), (
        "the declared tolerance is bitwise equality; loosening it requires a "
        "measured upper bound on the backend's nondeterminism (NOTE-038)"
    )
