# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；在 tmp_path 造出合成的證據 artifact 餵給
#         pcmef.audit.e1_gates，並以人為破壞驗證稽核器抓得到；
#         不讀真實的 data/、outputs/ 或 freeze/。
# 檔案路徑: tests/audit/test_e1_gates.py
# 產生時間: 2026-08-27 09:20 +08:00
# 版本: v0.1.0
# 功能說明: 驗證十二個 gate 的稽核器在三種情境下都給出正確結論 ——
#           證據齊全時 PASS、證據矛盾時 FAIL、證據還沒產出時 NOT_PRODUCED。
# 模組定位: 稽核器自身的反證。沒有這一檔，audit 指令可能永遠回綠而沒人發現。
# 主要責任:
#   1. test_a_bare_tree_reports_everything_as_not_produced 驗證預設狀態
#   2. test_healthy_evidence_passes 驗證齊全時 PASS
#   3. test_broken_* 以人為破壞驗證每個檢查真的擋得下來
#   4. test_not_produced_is_not_a_failure 驗證 exit code 語意
#   5. test_require_range_* 驗證 --require 的解析與判定
#   6. test_every_gate_has_a_check_function 擋下有宣告無檢查的空殼
# 維護提醒:
#   - 不得只測 PASS 路徑。一個永遠回 PASS 的稽核器與沒有稽核器等價，
#     因此每個檢查都必須有對應的「破壞後應該 FAIL」案例。
#   - v0.1.0 新增：首版，對應 NOTE-022。
# 驗證方式:
#   - py -3.10 -m pytest tests/audit/test_e1_gates.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.audit.e1_gates import (
    ALL_GATES,
    GATE_SPECS,
    AuditPaths,
    audit_e1_gates,
    parse_gate_range,
)
from pcmef.audit.result import CheckStatus


@pytest.fixture
def paths(tmp_path) -> AuditPaths:
    for name in ("inventory", "splits", "simulation", "surrogate", "provenance",
                 "freeze", "tests"):
        (tmp_path / name).mkdir()
    return AuditPaths(
        inventory=tmp_path / "inventory", splits=tmp_path / "splits",
        simulation=tmp_path / "simulation", surrogate=tmp_path / "surrogate",
        provenance=tmp_path / "provenance", freeze=tmp_path / "freeze",
        tests=tmp_path / "tests",
    )


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _healthy_g01(paths: AuditPaths, **overrides) -> None:
    (paths.inventory / "source_inventory.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    counts = {
        "nominal_logical_recordings": 560, "physical_source_files": 560,
        "canonical_recordings": 560, "valid_recordings": 560,
        "e1_eligible_recordings": 560,
    }
    counts.update(overrides.pop("counts", {}))
    payload = {"counts": counts, "exclusions_by_reason": {}}
    payload.update(overrides)
    _write(paths.inventory / "audit_report.json", payload)


def _healthy_g02(paths: AuditPaths, **overrides) -> None:
    payload = {
        "collision_count": 0,
        "assignments": {"nowater/measurement_1": "calibration",
                        "nowater/measurement_2": "heldout_real"},
        "eligible_set_hash": "e" * 64, "calibration_set_hash": "c" * 64,
        "heldout_real_set_hash": "h" * 64,
        "counts": {"Empty": {"eligible": 2}},
        "totals": {"eligible": 2},
    }
    payload.update(overrides)
    _write(paths.splits / "split_registry.json", payload)


def _healthy_g03(paths: AuditPaths, **overrides) -> None:
    payload = {"counts": {"ok": 4, "failed": 0}, "scenarios": [
        {"scenario_id": "smoke_empty_0001", "edge_fraction": 0.02}]}
    payload.update(overrides)
    _write(paths.simulation / "simulation_smoke_manifest.json", payload)


def _healthy_g04(paths: AuditPaths, all_finite: str = "True") -> None:
    (paths.surrogate / "surrogate_smoke.csv").write_text(
        f"scenario_id,all_finite\nsmoke_empty_0001,{all_finite}\n", encoding="utf-8"
    )


def _healthy_g08(paths: AuditPaths, status: str = "RESOLVED") -> None:
    _write(paths.provenance / "sigma_resolution.json", {
        "status": status, "blocking_reasons": [],
        "register": {"resolved": "0x18"}, "scaling": {"resolved_divisor": 65536.0},
    })


def _healthy_g09(paths: AuditPaths, **overrides):
    from pcmef.core.locks import LockStore

    payload = {
        "creation_phase": "AFTER_M0_BEFORE_ANY_CALIBRATION", "split_unit": "recording",
        "stratification": "class", "group_rule": "seeded_stratified_recording",
        "allocation": {"calibration": 0.7, "heldout_real": 0.3},
        "minimum_per_class": 100, "seed": 20260826,
        "eligible_set_hash": "e" * 8, "calibration_set_hash": "c" * 8,
        "heldout_real_set_hash": "h" * 8,
        "counts": {"Empty": {"eligible": 140}},
        "totals": {"calibration": 392, "heldout_real": 168},
        "heldout_access_count_at_lock": 0, "redraw_policy": "FORBIDDEN_AFTER_LOCK",
    }
    payload.update(overrides)
    LockStore(paths.freeze).write("real_split_policy", payload)


def _all_healthy(paths: AuditPaths) -> None:
    _healthy_g01(paths)
    _healthy_g02(paths)
    _healthy_g03(paths)
    _healthy_g04(paths)
    _healthy_g08(paths)
    _healthy_g09(paths)


# ---------------------------------------------------------------------------
# 三種基本情境
# ---------------------------------------------------------------------------


def test_a_bare_tree_reports_everything_as_not_produced(paths):
    """稽核器先於被稽核的產物存在，因此空樹的正確答案是「都還沒產出」。"""
    report = audit_e1_gates(paths)

    assert report.counts()["NOT_PRODUCED"] == 12
    assert report.counts()["FAIL"] == 0
    assert report.exit_code() == 0


def test_healthy_evidence_passes(paths):
    _all_healthy(paths)
    report = audit_e1_gates(paths)

    for gate in ("E1-G01", "E1-G02", "E1-G03", "E1-G04", "E1-G08", "E1-G09"):
        assert report.get(gate).status is CheckStatus.PASS, gate
    # 其餘六個屬 Batch 6/8，仍應是尚未產出。
    assert report.counts()["NOT_PRODUCED"] == 6
    assert report.exit_code() == 0


def test_not_produced_is_not_a_failure(paths):
    """把「還沒做」報成「做錯了」會讓報告從第一天起就是紅的。"""
    _all_healthy(paths)
    report = audit_e1_gates(paths)

    assert report.counts()["NOT_PRODUCED"] == 6
    assert report.exit_code() == 0
    # 但被指名要求時就不能放過。
    assert report.exit_code(["E1-G06"]) == 1


# ---------------------------------------------------------------------------
# 人為破壞：每個檢查都要擋得下來
# ---------------------------------------------------------------------------


def test_broken_g01_non_monotonic_counts(paths):
    _healthy_g01(paths, counts={"valid_recordings": 999})
    result = audit_e1_gates(paths).get("E1-G01")
    assert result.status is CheckStatus.FAIL
    assert any("單調不增" in f for f in result.findings)


def test_broken_g01_exclusion_ledger_does_not_reconcile(paths):
    """流失 60 筆卻只有 1 筆具名理由。"""
    _healthy_g01(
        paths,
        counts={"valid_recordings": 500, "e1_eligible_recordings": 500},
        exclusions_by_reason={"length_mismatch": 1},
    )
    result = audit_e1_gates(paths).get("E1-G01")
    assert result.status is CheckStatus.FAIL
    assert any("排除帳" in f for f in result.findings)


def test_broken_g01_zero_eligible(paths):
    _healthy_g01(paths, counts={"e1_eligible_recordings": 0})
    result = audit_e1_gates(paths).get("E1-G01")
    assert result.status is CheckStatus.FAIL
    assert any("E1-G08" in f for f in result.findings)


def test_broken_g02_id_collision(paths):
    _healthy_g02(paths, collision_count=3)
    result = audit_e1_gates(paths).get("E1-G02")
    assert result.status is CheckStatus.FAIL
    assert any("collision_count" in f for f in result.findings)


def test_broken_g02_missing_set_hash(paths):
    """registry 進版控而 lock 不進，這三個雜湊是唯一的對照點（NOTE-014）。"""
    _healthy_g02(paths, calibration_set_hash="")
    result = audit_e1_gates(paths).get("E1-G02")
    assert result.status is CheckStatus.FAIL
    assert any("calibration_set_hash" in f for f in result.findings)


def test_broken_g02_totals_disagree_with_assignments(paths):
    _healthy_g02(paths, totals={"eligible": 999})
    result = audit_e1_gates(paths).get("E1-G02")
    assert result.status is CheckStatus.FAIL


def test_broken_g03_failed_scenario(paths):
    _healthy_g03(paths, counts={"ok": 3, "failed": 1})
    result = audit_e1_gates(paths).get("E1-G03")
    assert result.status is CheckStatus.FAIL


def test_broken_g03_truncated_transient(paths):
    """NOTE-013：峰值貼在窗邊代表回波被截斷。"""
    _healthy_g03(paths, scenarios=[
        {"scenario_id": "smoke_empty_0001", "edge_fraction": 0.72}])
    result = audit_e1_gates(paths).get("E1-G03")
    assert result.status is CheckStatus.FAIL
    assert any("edge_fraction" in f for f in result.findings)


def test_broken_g04_non_finite(paths):
    _healthy_g04(paths, all_finite="False")
    result = audit_e1_gates(paths).get("E1-G04")
    assert result.status is CheckStatus.FAIL
    assert any("NaN" in f for f in result.findings)


def test_broken_g04_empty_file(paths):
    (paths.surrogate / "surrogate_smoke.csv").write_text("", encoding="utf-8")
    result = audit_e1_gates(paths).get("E1-G04")
    assert result.status is CheckStatus.FAIL


def test_broken_g08_unresolved_sigma(paths):
    _healthy_g08(paths, status="UNRESOLVED")
    result = audit_e1_gates(paths).get("E1-G08")
    assert result.status is CheckStatus.FAIL
    assert any("RESOLVED" in f for f in result.findings)


def test_broken_g09_heldout_already_accessed(paths):
    """SRC-PLAN §3.1：凍結時 access_count 必須為 0。"""
    _healthy_g09(paths, heldout_access_count_at_lock=1)
    result = audit_e1_gates(paths).get("E1-G09")
    assert result.status is CheckStatus.FAIL
    assert any("access_count" in f for f in result.findings)


def test_broken_g09_wrong_creation_phase(paths):
    _healthy_g09(paths, creation_phase="AFTER_CALIBRATION")
    result = audit_e1_gates(paths).get("E1-G09")
    assert result.status is CheckStatus.FAIL


def test_broken_g09_tampered_lock(paths):
    """lock 被凍結後改過，完整性檢查必須抓到。"""
    _healthy_g09(paths)
    lock_path = paths.freeze / "real_split_policy.lock.json"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace("20260826", "99999"),
        encoding="utf-8",
    )
    result = audit_e1_gates(paths).get("E1-G09")
    assert result.status is CheckStatus.FAIL
    assert any("integrity" in f for f in result.findings)


# ---------------------------------------------------------------------------
# --require 解析
# ---------------------------------------------------------------------------


def test_require_range_expands_to_all_twelve():
    assert parse_gate_range("G01:G12") == ALL_GATES
    assert parse_gate_range("E1-G01:E1-G12") == ALL_GATES


def test_require_accepts_a_comma_list():
    assert parse_gate_range("G01,G04") == ("E1-G01", "E1-G04")


def test_require_rejects_unknown_or_inverted():
    with pytest.raises(ValueError, match="unknown"):
        parse_gate_range("G99")
    with pytest.raises(ValueError, match="inverted"):
        parse_gate_range("G12:G01")


def test_requiring_a_not_produced_gate_is_a_failure(paths):
    """--require 的意思就是「你說必須通過」，尚未產出也算沒通過。"""
    _all_healthy(paths)
    report = audit_e1_gates(paths)

    assert report.exit_code(parse_gate_range("G01:G12")) == 1
    unmet = {r.identifier for r in report.unmet(parse_gate_range("G01:G12"))}
    assert unmet == {"E1-G05", "E1-G06", "E1-G07", "E1-G10", "E1-G11", "E1-G12"}
    assert report.exit_code(parse_gate_range("G01,G02,G03,G04,G08,G09")) == 0


# ---------------------------------------------------------------------------
# 稽核器自身的完整性
# ---------------------------------------------------------------------------


def test_every_gate_has_a_check_function(paths):
    """有宣告卻沒有檢查的 gate 比沒有 gate 更糟 —— 它看起來被稽核過。"""
    from pcmef.audit import e1_gates

    assert set(GATE_SPECS) == set(e1_gates._CHECKS)
    audit_e1_gates(paths)  # 不得拋例外


def test_gate_ids_match_the_specification():
    assert ALL_GATES == tuple(f"E1-G{n:02d}" for n in range(1, 13))
    for spec in GATE_SPECS.values():
        assert spec.requirement
        assert spec.evidence
        assert spec.owner


def test_a_failed_check_must_name_a_finding(paths):
    """CheckResult 在 FAIL 但沒有 findings 時應該拒絕被建立。"""
    from pcmef.audit.result import CheckResult

    with pytest.raises(ValueError, match="findings"):
        CheckResult(identifier="X", requirement="y", status=CheckStatus.FAIL)
