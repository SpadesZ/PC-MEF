# PC-MEF Research System source maintenance contract
# 上下游: 測 pcmef.audit.initial_simulation 的 IS-01..IS-06；
#         以合成 artifact 驅動，不需要算圖。
# 檔案路徑: tests/audit/test_initial_simulation.py
# 產生時間: 2026-08-28 18:05 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 initial_simulation 凍結前置稽核的判準是對的 —— 特別是
#           「參數仍未校準」必須 PASS，而「estimator 未選定」必須 FAIL。
# 模組定位: NOTE-036 的反向驗收。它「不是」場景物理的測試。
# 主要責任:
#   1. 未校準值不得被當成阻塞（IS-06）
#   2. estimator 未選定必須阻塞（IS-04）
#   3. 缺 artifact 時回報 NOT_PRODUCED 而非 FAIL
# 維護提醒:
#   - 不得把 test_uncalibrated_values_do_not_block_the_initial_freeze 改成
#     期望 FAIL；那會讓 initial freeze 永遠不可能發生（NOTE-036）。
#   - v0.1.0 新增：首版（NOTE-036）。
# 驗證方式:
#   - py -3.10 -m pytest tests/audit/test_initial_simulation.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

from pcmef.audit.initial_simulation import audit_initial_simulation
from pcmef.audit.result import CheckStatus


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ambient_ok(tmp_path):
    return _write(
        tmp_path / "ambient.json",
        {"counts": {"total": 4, "pass": 4, "fail": 0}, "checks": []},
    )


def _manifest_ok(tmp_path):
    return _write(
        tmp_path / "manifest.json",
        {"scenarios": [{"status": "OK", "transient": {"total_energy": 1234.0}}]},
    )


def _estimator(tmp_path, outcome, selected=None):
    return _write(
        tmp_path / "estimator.json",
        {
            "outcome": outcome,
            "selected": selected,
            "reason": "test",
            "preregistration_hash": "a" * 64,
        },
    )


def _run(tmp_path, outcome, selected=None):
    report = audit_initial_simulation(
        ambient_report=_ambient_ok(tmp_path),
        estimator_report=_estimator(tmp_path, outcome, selected),
        simulation_manifest=_manifest_ok(tmp_path),
    )
    return {c.identifier: c for c in report.results}


def test_uncalibrated_values_do_not_block_the_initial_freeze(tmp_path):
    """initial freeze 發生在 calibration **之前**，placeholder 是預期狀態。

    這一條是本稽核與 `params audit` 的分水嶺。若它變成 FAIL，就等於要求
    「校準完才能凍結校準前的狀態」—— 循環，initial freeze 永不可能發生。
    """
    checks = _run(tmp_path, "SELECTED", "PEAK")
    assert checks["IS-06"].status is CheckStatus.PASS
    assert "expected state before calibration" in checks["IS-06"].detail

    from pcmef.core.parameters import ParameterRegistry, formal_blocking_reasons

    registry = ParameterRegistry.load()
    # 同一份 registry 在 formal-run 那條線上仍是被擋的 —— 兩條線刻意不同。
    assert formal_blocking_reasons(registry), (
        "the formal-run firewall must still block; if it does not, the two "
        "criteria have been conflated"
    )


def test_every_knob_governed_passes_while_values_remain_uncalibrated(tmp_path):
    checks = _run(tmp_path, "SELECTED", "PEAK")
    assert checks["IS-05"].status is CheckStatus.PASS
    assert "unbound=0" in checks["IS-05"].detail
    assert "drift=0" in checks["IS-05"].detail


def test_unselected_estimator_blocks_the_freeze(tmp_path):
    checks = _run(tmp_path, "TIE_BREAK_REQUIRED")
    assert checks["IS-04"].status is CheckStatus.FAIL
    assert "still undecided" in checks["IS-04"].detail
    assert checks["IS-04"].findings, "a FAIL must say which condition was not met"


def test_no_estimator_selected_also_blocks(tmp_path):
    checks = _run(tmp_path, "NO_ESTIMATOR_SELECTED")
    assert checks["IS-04"].status is CheckStatus.FAIL


def test_selected_estimator_passes(tmp_path):
    checks = _run(tmp_path, "SELECTED", "LEADING_EDGE")
    assert checks["IS-04"].status is CheckStatus.PASS
    assert "LEADING_EDGE" in checks["IS-04"].detail


def test_missing_artifacts_are_not_produced_rather_than_failures(tmp_path):
    report = audit_initial_simulation(
        ambient_report=tmp_path / "absent_ambient.json",
        estimator_report=tmp_path / "absent_estimator.json",
        simulation_manifest=tmp_path / "absent_manifest.json",
    )
    checks = {c.identifier: c for c in report.results}
    for identifier in ("IS-02", "IS-03", "IS-04"):
        assert checks[identifier].status is CheckStatus.NOT_PRODUCED, (
            "a missing artifact means 'not run yet', which is different from "
            "'ran and failed' (NOTE-022)"
        )


def test_failed_ambient_checks_block(tmp_path):
    ambient = _write(
        tmp_path / "ambient.json",
        {
            "counts": {"total": 4, "pass": 3, "fail": 1},
            "checks": [{"check_id": "A2", "status": "FAIL"}],
        },
    )
    report = audit_initial_simulation(
        ambient_report=ambient,
        estimator_report=_estimator(tmp_path, "SELECTED", "PEAK"),
        simulation_manifest=_manifest_ok(tmp_path),
    )
    checks = {c.identifier: c for c in report.results}
    assert checks["IS-03"].status is CheckStatus.FAIL
    assert "A2" in checks["IS-03"].detail


def test_scene_topology_check_reads_the_real_adapter(tmp_path):
    """IS-01 直接讀程式碼，不依賴任何 artifact。"""
    checks = _run(tmp_path, "SELECTED", "PEAK")
    assert checks["IS-01"].status is CheckStatus.PASS
    assert "shell+air+shell" in checks["IS-01"].detail
