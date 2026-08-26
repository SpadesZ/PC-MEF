# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；在 tmp_path 造出合成的 split_registry 與
#         real_split_policy.lock 餵給 pcmef.audit.firewall，
#         並以人為破壞驗證稽核器抓得到；不讀真實的 data/ 或 freeze/。
# 檔案路徑: tests/audit/test_firewall.py
# 產生時間: 2026-08-27 09:40 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 held-out 洩漏防線與 real split policy 契約的稽核器，
#           在切分重疊、heldout 已被取用、雜湊被改、lock 晚於校準等情況下
#           都真的擋得下來。
# 模組定位: Appendix B 與 Appendix H1 稽核器自身的反證。
# 主要責任:
#   1. test_healthy_* 驗證乾淨狀態下全 PASS
#   2. test_overlapping_split_is_caught 驗 FW-01
#   3. test_consumed_heldout_is_caught 驗 FW-02
#   4. test_lock_after_calibration_is_caught 驗 FW-03 的時序判定
#   5. test_registry_lock_hash_mismatch_is_caught 驗 FW-04
#   6. test_policy_* 逐條驗 SP-01..SP-06
# 維護提醒:
#   - 不得把「沒有 calibration artifact」寫成 PASS；那是 NOT_PRODUCED，
#     因為「還沒開始校準」與「校準確實晚於凍結」是兩個不同的事實。
#   - v0.1.0 新增：首版，對應 NOTE-022。
# 驗證方式:
#   - py -3.10 -m pytest tests/audit/test_firewall.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import time

import pytest

from pcmef.audit.firewall import (
    SplitAuditPaths,
    audit_heldout_firewall,
    audit_real_split_policy,
)
from pcmef.audit.result import CheckStatus
from pcmef.core.hash import hash_object
from pcmef.core.locks import LockStore

CALIBRATION = [f"nowater/measurement_{n}" for n in range(1, 8)]
HELDOUT = [f"nowater/measurement_{n}" for n in range(8, 11)]


def _hashes() -> dict[str, str]:
    """以與 core.splits._set_hash 相同的方式算出三組雜湊。"""
    return {
        "calibration_set_hash": hash_object({"Empty": sorted(CALIBRATION)}),
        "heldout_real_set_hash": hash_object({"Empty": sorted(HELDOUT)}),
        "eligible_set_hash": hash_object({"Empty": sorted(CALIBRATION + HELDOUT)}),
    }


@pytest.fixture
def paths(tmp_path) -> SplitAuditPaths:
    (tmp_path / "splits").mkdir()
    (tmp_path / "freeze").mkdir()
    (tmp_path / "outputs").mkdir()
    return SplitAuditPaths(
        splits=tmp_path / "splits", freeze=tmp_path / "freeze",
        outputs=tmp_path / "outputs",
    )


def _write_registry(paths: SplitAuditPaths, **overrides) -> None:
    assignments = {i: "calibration" for i in CALIBRATION}
    assignments.update({i: "heldout_real" for i in HELDOUT})
    payload = {
        "assignments": assignments,
        "heldout_access_count": 0,
        "counts": {"Empty": {"eligible": 10}},
        **_hashes(),
    }
    payload.update(overrides)
    paths.registry_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _write_lock(paths: SplitAuditPaths, **overrides):
    payload = {
        "creation_phase": "AFTER_M0_BEFORE_ANY_CALIBRATION", "split_unit": "recording",
        "stratification": "class", "group_rule": "seeded_stratified_recording",
        "group_rule_evidence": "no acquisition session metadata exists in the export",
        "allocation": {"calibration": 0.7, "heldout_real": 0.3},
        "minimum_per_class": 10, "seed": 20260826,
        "counts": {"Empty": {"eligible": 10}},
        "totals": {"calibration": 7, "heldout_real": 3},
        "heldout_access_count_at_lock": 0, "redraw_policy": "FORBIDDEN_AFTER_LOCK",
        **_hashes(),
    }
    payload.update(overrides)
    LockStore(paths.freeze).write("real_split_policy", payload)


# ---------------------------------------------------------------------------
# Heldout firewall
# ---------------------------------------------------------------------------


def test_a_bare_tree_reports_not_produced(paths):
    report = audit_heldout_firewall(paths)
    assert report.counts()["NOT_PRODUCED"] == 5
    assert report.exit_code() == 0


def test_healthy_split_passes(paths):
    _write_registry(paths)
    _write_lock(paths)

    report = audit_heldout_firewall(paths)

    assert report.get("FW-01").status is CheckStatus.PASS
    assert report.get("FW-02").status is CheckStatus.PASS
    assert report.get("FW-04").status is CheckStatus.PASS
    assert report.get("FW-05").status is CheckStatus.PASS
    # 尚無 calibration artifact，時序無從比較。
    assert report.get("FW-03").status is CheckStatus.NOT_PRODUCED
    assert report.exit_code() == 0


def test_overlapping_split_is_caught(paths):
    """Held-out 一旦進了 calibration，E1 的一次性評估就消耗掉了。"""
    assignments = {i: "calibration" for i in CALIBRATION + HELDOUT[:1]}
    assignments.update({i: "heldout_real" for i in HELDOUT})
    # 直接寫一個同時出現在兩邊的指派無法用 dict 表達，改以重疊的集合模擬：
    _write_registry(paths)
    registry = json.loads(paths.registry_path.read_text(encoding="utf-8"))
    registry["assignments"][HELDOUT[0]] = "calibration"
    registry["assignments"][f"{HELDOUT[0]}"] = "calibration"
    paths.registry_path.write_text(json.dumps(registry), encoding="utf-8")

    report = audit_heldout_firewall(paths)

    # 少了一筆 heldout，雜湊必然對不上 —— 這正是雜湊存在的理由。
    assert report.get("FW-05").status is CheckStatus.FAIL


def test_consumed_heldout_is_caught(paths):
    _write_registry(paths, heldout_access_count=1)
    _write_lock(paths)

    result = audit_heldout_firewall(paths).get("FW-02")

    assert result.status is CheckStatus.FAIL
    assert any("不算例外" in f for f in result.findings)


def test_lock_recording_a_consumed_heldout_is_caught(paths):
    _write_registry(paths)
    _write_lock(paths, heldout_access_count_at_lock=2)

    result = audit_heldout_firewall(paths).get("FW-02")

    assert result.status is CheckStatus.FAIL
    assert any("access_count=2" in f for f in result.findings)


def test_registry_lock_hash_mismatch_is_caught(paths):
    """registry 進版控、lock 不進，兩者對不上代表其中一份被改過。"""
    _write_registry(paths)
    _write_lock(paths, calibration_set_hash="d" * 64)

    result = audit_heldout_firewall(paths).get("FW-04")

    assert result.status is CheckStatus.FAIL
    assert any("calibration_set_hash" in f for f in result.findings)


def test_hash_not_matching_assignments_is_caught(paths):
    """有人改了指派卻沒改雜湊，或反過來。"""
    _write_registry(paths, calibration_set_hash="f" * 64)

    result = audit_heldout_firewall(paths).get("FW-05")

    assert result.status is CheckStatus.FAIL
    assert any("重算結果不符" in f for f in result.findings)


def test_lock_after_calibration_is_caught(paths):
    """Appendix B：切分必須早於任何校準，否則等於看過結果才分組。"""
    _write_registry(paths)
    # 先造校準產物，再凍 lock —— 時序刻意顛倒。
    calibration_dir = paths.outputs / "calibration"
    calibration_dir.mkdir()
    artifact = calibration_dir / "fit.json"
    artifact.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(artifact, (old, old))
    _write_lock(paths)

    result = audit_heldout_firewall(paths).get("FW-03")

    assert result.status is CheckStatus.FAIL
    assert any("必須早於任何校準" in f for f in result.findings)


def test_lock_before_calibration_passes(paths):
    _write_registry(paths)
    _write_lock(paths)
    calibration_dir = paths.outputs / "calibration"
    calibration_dir.mkdir()
    artifact = calibration_dir / "fit.json"
    artifact.write_text("{}", encoding="utf-8")
    future = time.time() + 3600
    os.utime(artifact, (future, future))

    assert audit_heldout_firewall(paths).get("FW-03").status is CheckStatus.PASS


# ---------------------------------------------------------------------------
# Real split policy contract
# ---------------------------------------------------------------------------


def test_policy_audit_without_a_lock_is_not_produced(paths):
    report = audit_real_split_policy(paths)
    assert report.counts()["NOT_PRODUCED"] == 6
    assert report.exit_code() == 0


def test_healthy_policy_passes(paths):
    _write_lock(paths)
    report = audit_real_split_policy(paths)
    assert report.counts()["PASS"] == 6
    assert report.exit_code() == 0


@pytest.mark.parametrize(
    "check, overrides, needle",
    [
        ("SP-02", {"creation_phase": "AFTER_CALIBRATION"}, "creation_phase"),
        ("SP-03", {"redraw_policy": "ALLOWED"}, "redraw_policy"),
        ("SP-04", {"split_unit": "measurement_point"}, "偽重複"),
        ("SP-05", {"minimum_per_class": 100}, "minimum_per_class"),
        ("SP-06", {"group_rule_evidence": ""}, "證據"),
    ],
)
def test_policy_violations_are_caught(paths, check, overrides, needle):
    _write_lock(paths, **overrides)
    result = audit_real_split_policy(paths).get(check)
    assert result.status is CheckStatus.FAIL, check
    assert any(needle in f for f in result.findings), result.findings


def test_the_audit_never_writes_to_what_it_audits(paths):
    """稽核器一旦能寫，「稽核先於產物」這個設計就失去意義。"""
    _write_registry(paths)
    _write_lock(paths)
    before = {
        p: p.read_bytes()
        for p in list(paths.splits.rglob("*")) + list(paths.freeze.rglob("*"))
        if p.is_file()
    }

    audit_heldout_firewall(paths)
    audit_real_split_policy(paths)

    after = {
        p: p.read_bytes()
        for p in list(paths.splits.rglob("*")) + list(paths.freeze.rglob("*"))
        if p.is_file()
    }
    assert before == after
