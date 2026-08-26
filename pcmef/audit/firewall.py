# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 audit heldout-firewall 與 audit real-split-policy 呼叫；
#         讀 data/splits/split_registry.json 與 freeze/real_split_policy.lock.json，
#         並掃描 outputs/ 下的 calibration artifact 判定時序；
#         輸出 AuditReport。本檔只讀不寫。
# 檔案路徑: pcmef/audit/firewall.py
# 產生時間: 2026-08-27 08:40 +08:00
# 版本: v0.1.0
# 功能說明: 檢查那批被保留下來、只能用一次的真實資料有沒有被提早動用 ——
#           calibration 與 heldout 是否真的沒有交集、lock 是否早於第一次校準、
#           以及被版控的 registry 與不進版控的 lock 是否還對得上。
# 模組定位: SRC-SAI Appendix B 前四條 leakage guard 與 Appendix H1 Real Split
#           Policy Contract 的可執行稽核器。它不產生切分，也不修復不一致。
# 主要責任:
#   1. audit_heldout_firewall() 執行 FW-01..FW-05 五項洩漏防線檢查
#   2. audit_real_split_policy() 執行 SP-01..SP-06 六項政策契約檢查
#   3. _check_disjoint() 驗 calibration ∩ heldout = empty
#   4. _check_access_count() 驗 heldout 在凍結時未被取用
#   5. _check_lock_precedes_calibration() 驗 lock 時間早於第一份校準產物
#   6. _check_registry_matches_lock() 驗三組 set hash 與 lock 相符
#   7. _recompute_set_hash() 由 registry 的 assignments 重算雜湊
# 維護提醒:
#   - 不得在此提供任何「修復」或「重抽」路徑。發現不一致的正確處置是開新 run，
#     就地修好會讓稽核變成掩蓋工具（NOTE-014）。
#   - 不得把「找不到 calibration artifact」判成 PASS。沒有校準產物時
#     時序無從比較，正確結果是 NOT_PRODUCED 而不是「通過」。
#   - 不得放寬 heldout access_count 的檢查；SRC-PLAN §3.1 規定它在 E1 final
#     之前必須為 0，calibration / tuning / sanity check 都不算例外。
#   - v0.1.0 新增：首版 firewall 與 split policy 稽核，決策見 NOTE-022。
# 驗證方式:
#   - py -3.10 -m pytest tests/audit/test_firewall.py -v
#   - py -3.10 -m pcmef.cli audit heldout-firewall
# ------------------------------------------------------------

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pcmef.audit.result import AuditReport, CheckResult, CheckStatus
from pcmef.core.hash import hash_object
from pcmef.core.locks import LockError, LockStore

__all__ = [
    "SplitAuditPaths",
    "audit_heldout_firewall",
    "audit_real_split_policy",
    "CALIBRATION_ARTIFACT_GLOBS",
]

#: 被視為「第一次 calibration fit」證據的產物。lock 必須早於其中最早的一份
#: （Appendix B：real_split_policy.lock timestamp < first calibration fit）。
CALIBRATION_ARTIFACT_GLOBS: tuple[str, ...] = (
    "calibration/*.json",
    "surrogate/calibration_*.json",
    "e1/calibrated_*.json",
)


class SplitAuditPaths:
    """證據所在位置。"""

    def __init__(
        self,
        splits: str | Path = "data/splits",
        freeze: str | Path = "freeze",
        outputs: str | Path = "outputs",
    ) -> None:
        self.splits = Path(splits)
        self.freeze = Path(freeze)
        self.outputs = Path(outputs)

    @property
    def registry_path(self) -> Path:
        return self.splits / "split_registry.json"


def _load_registry(paths: SplitAuditPaths) -> dict[str, Any] | None:
    if not paths.registry_path.exists():
        return None
    return json.loads(paths.registry_path.read_text(encoding="utf-8"))


def _load_lock(paths: SplitAuditPaths) -> dict[str, Any] | None:
    store = LockStore(paths.freeze)
    if not store.exists("real_split_policy"):
        return None
    return store.load("real_split_policy")


def _by_role(registry: dict[str, Any]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for identifier, role in registry.get("assignments", {}).items():
        grouped.setdefault(role, set()).add(identifier)
    return grouped


def _recompute_set_hash(identifiers: set[str], registry: dict[str, Any]) -> str:
    """由 assignments 重算 set hash，格式與 core.splits._set_hash 一致。

    重算而非直接讀 registry 的欄位：直接讀只能證明欄位存在，
    重算才能證明欄位與 assignments 真的對得上。
    """
    per_class: dict[str, list[str]] = {}
    for identifier in identifiers:
        # ID 形如 "<condition>/measurement_<n>"，class 由 registry.counts 的鍵推得。
        for class_label in registry.get("counts", {}):
            if identifier.split("/")[0] in _class_tokens(class_label):
                per_class.setdefault(class_label, []).append(identifier)
                break
    return hash_object({cls: sorted(ids) for cls, ids in sorted(per_class.items())})


def _class_tokens(class_label: str) -> set[str]:
    """class label 與 legacy condition 目錄名的對照。"""
    from pcmef.core.constants import LEGACY_LABEL_MAP

    tokens = {class_label, class_label.lower()}
    tokens |= {
        legacy for legacy, canonical in LEGACY_LABEL_MAP.items()
        if canonical == class_label
    }
    return tokens


# ---------------------------------------------------------------------------
# Heldout firewall
# ---------------------------------------------------------------------------


def _check_disjoint(registry: dict[str, Any]) -> tuple[CheckStatus, str, list[str]]:
    """Appendix B：Held-out Real 不可進 calibration fit；intersection=0。"""
    grouped = _by_role(registry)
    calibration = grouped.get("calibration", set())
    heldout = grouped.get("heldout_real", set())
    if not calibration or not heldout:
        return CheckStatus.FAIL, "", [
            f"registry 缺角色：calibration={len(calibration)} heldout={len(heldout)}"
        ]
    overlap = calibration & heldout
    if overlap:
        return CheckStatus.FAIL, "", [
            f"{len(overlap)} 筆同時屬於 calibration 與 heldout，例如 "
            f"{sorted(overlap)[:3]}"
        ]
    return (
        CheckStatus.PASS,
        f"calibration {len(calibration)} ∩ heldout {len(heldout)} = empty",
        [],
    )


def _check_access_count(
    registry: dict[str, Any], lock: dict[str, Any] | None
) -> tuple[CheckStatus, str, list[str]]:
    """SRC-PLAN §3.1：E1 final 之前 heldout access_count 必須為 0。"""
    findings: list[str] = []
    registry_count = registry.get("heldout_access_count")
    if registry_count != 0:
        findings.append(
            f"split_registry.heldout_access_count={registry_count}；"
            "calibration / tuning / sanity check 都不算例外"
        )
    if lock is not None:
        lock_count = lock.get("heldout_access_count_at_lock")
        if lock_count != 0:
            findings.append(f"lock 記錄凍結時 access_count={lock_count}，必須為 0")
    return (
        (CheckStatus.FAIL if findings else CheckStatus.PASS),
        "access_count = 0",
        findings,
    )


def _check_lock_precedes_calibration(
    paths: SplitAuditPaths,
) -> tuple[CheckStatus, str, list[str]]:
    """Appendix B：real_split_policy.lock timestamp < first calibration fit。"""
    store = LockStore(paths.freeze)
    if not store.exists("real_split_policy"):
        return CheckStatus.NOT_PRODUCED, "lock 尚未產出", []

    artifacts: list[Path] = []
    for pattern in CALIBRATION_ARTIFACT_GLOBS:
        artifacts.extend(sorted(paths.outputs.glob(pattern)))
    if not artifacts:
        # 沒有校準產物時時序無從比較。回報未產出而非「通過」——
        # 「還沒開始校準」與「校準確實晚於凍結」是兩個不同的事實。
        return (
            CheckStatus.NOT_PRODUCED,
            "尚無 calibration artifact，時序無從比較",
            [],
        )

    try:
        lock_time = store.created_at("real_split_policy")
    except LockError as error:
        return CheckStatus.FAIL, "", [str(error)]

    earliest = min(artifacts, key=lambda p: p.stat().st_mtime)
    artifact_time = datetime.fromtimestamp(
        earliest.stat().st_mtime, tz=lock_time.tzinfo
    )
    if lock_time >= artifact_time:
        return CheckStatus.FAIL, "", [
            f"lock 建立於 {lock_time.isoformat()}，但最早的 calibration artifact "
            f"{earliest.name} 是 {artifact_time.isoformat()}；"
            "切分必須早於任何校準，否則等於看過結果才分組"
        ]
    return (
        CheckStatus.PASS,
        f"lock 早於 {earliest.name}",
        [],
    )


def _check_registry_matches_lock(
    registry: dict[str, Any], lock: dict[str, Any] | None
) -> tuple[CheckStatus, str, list[str]]:
    """NOTE-014：registry 進版控、lock 不進，三組雜湊是唯一對照點。"""
    if lock is None:
        return CheckStatus.NOT_PRODUCED, "lock 尚未產出", []

    findings: list[str] = []
    for key in ("eligible_set_hash", "calibration_set_hash", "heldout_real_set_hash"):
        registry_value = registry.get(key)
        lock_value = lock.get(key)
        if not registry_value or not lock_value:
            findings.append(f"{key} 在 registry 或 lock 缺漏")
        elif registry_value != lock_value:
            findings.append(
                f"{key} 不符：registry {registry_value[:16]} vs "
                f"lock {lock_value[:16]}；其中一份被改過"
            )
    detail = "三組 set hash 相符" if not findings else ""
    return (CheckStatus.FAIL if findings else CheckStatus.PASS), detail, findings


def _check_hashes_match_assignments(
    registry: dict[str, Any],
) -> tuple[CheckStatus, str, list[str]]:
    """雜湊必須由 assignments 重算得出，否則欄位只是裝飾。"""
    grouped = _by_role(registry)
    findings: list[str] = []
    pairs = (
        ("calibration_set_hash", grouped.get("calibration", set())),
        ("heldout_real_set_hash", grouped.get("heldout_real", set())),
    )
    for key, identifiers in pairs:
        if not identifiers:
            continue
        recomputed = _recompute_set_hash(identifiers, registry)
        if registry.get(key) != recomputed:
            findings.append(
                f"{key} 與 assignments 重算結果不符；"
                "有人改了指派卻沒改雜湊，或反過來"
            )
    detail = "雜湊與 assignments 一致" if not findings else ""
    return (CheckStatus.FAIL if findings else CheckStatus.PASS), detail, findings


def audit_heldout_firewall(paths: SplitAuditPaths | None = None) -> AuditReport:
    """執行 Appendix B 中與 real split 相關的五條 leakage guard。"""
    resolved = paths or SplitAuditPaths()
    registry = _load_registry(resolved)
    lock = _load_lock(resolved)

    if registry is None:
        return AuditReport(
            name="heldout_firewall",
            results=tuple(
                CheckResult(
                    identifier=identifier,
                    requirement=requirement,
                    status=CheckStatus.NOT_PRODUCED,
                    detail="split_registry.json 尚未產出（Batch 2）",
                    evidence=("data/splits/split_registry.json",),
                )
                for identifier, requirement in _FIREWALL_CHECKS
            ),
        )

    outcomes = {
        "FW-01": _check_disjoint(registry),
        "FW-02": _check_access_count(registry, lock),
        "FW-03": _check_lock_precedes_calibration(resolved),
        "FW-04": _check_registry_matches_lock(registry, lock),
        "FW-05": _check_hashes_match_assignments(registry),
    }
    return AuditReport(
        name="heldout_firewall",
        results=tuple(
            CheckResult(
                identifier=identifier,
                requirement=requirement,
                status=outcomes[identifier][0],
                detail=outcomes[identifier][1],
                evidence=(
                    "data/splits/split_registry.json",
                    "freeze/real_split_policy.lock.json",
                ),
                findings=tuple(outcomes[identifier][2]),
            )
            for identifier, requirement in _FIREWALL_CHECKS
        ),
    )


_FIREWALL_CHECKS: tuple[tuple[str, str], ...] = (
    ("FW-01", "Held-out Real 不可進 calibration fit；intersection = 0"),
    ("FW-02", "heldout access_count 在 E1 final 之前必須為 0"),
    ("FW-03", "real_split_policy.lock timestamp 早於第一次 calibration fit"),
    ("FW-04", "registry 與 lock 的三組 set hash 相符"),
    ("FW-05", "set hash 由 assignments 重算可得，不是憑空欄位"),
)


# ---------------------------------------------------------------------------
# Real split policy contract
# ---------------------------------------------------------------------------

_POLICY_CHECKS: tuple[tuple[str, str], ...] = (
    ("SP-01", "lock 存在；缺此 lock 時系統必須拒絕 calibration（Appendix H1）"),
    ("SP-02", "creation_phase = AFTER_M0_BEFORE_ANY_CALIBRATION"),
    ("SP-03", "redraw_policy = FORBIDDEN_AFTER_LOCK"),
    ("SP-04", "split_unit = recording；不得以 measurement point 為單位"),
    ("SP-05", "每個 class 的 e1-eligible 數量達 minimum_per_class"),
    ("SP-06", "group_rule 若退回 seeded stratified，必須附不可用 session 的證據"),
)


def audit_real_split_policy(paths: SplitAuditPaths | None = None) -> AuditReport:
    """執行 Appendix H1 Real Split Policy Contract 的六項檢查。"""
    resolved = paths or SplitAuditPaths()
    lock = _load_lock(resolved)

    if lock is None:
        return AuditReport(
            name="real_split_policy",
            results=tuple(
                CheckResult(
                    identifier=identifier,
                    requirement=requirement,
                    status=CheckStatus.NOT_PRODUCED,
                    detail="real_split_policy.lock 尚未凍結",
                    evidence=("freeze/real_split_policy.lock.json",),
                )
                for identifier, requirement in _POLICY_CHECKS
            ),
        )

    results: list[CheckResult] = []
    for identifier, requirement in _POLICY_CHECKS:
        status, detail, findings = _policy_check(identifier, lock)
        results.append(
            CheckResult(
                identifier=identifier,
                requirement=requirement,
                status=status,
                detail=detail,
                evidence=("freeze/real_split_policy.lock.json",),
                findings=tuple(findings),
            )
        )
    return AuditReport(name="real_split_policy", results=tuple(results))


def _policy_check(
    identifier: str, lock: dict[str, Any]
) -> tuple[CheckStatus, str, list[str]]:
    if identifier == "SP-01":
        return CheckStatus.PASS, "lock 已凍結且通過完整性檢查", []

    if identifier == "SP-02":
        phase = lock.get("creation_phase")
        if phase != "AFTER_M0_BEFORE_ANY_CALIBRATION":
            return CheckStatus.FAIL, "", [f"creation_phase={phase!r}"]
        return CheckStatus.PASS, str(phase), []

    if identifier == "SP-03":
        policy = lock.get("redraw_policy")
        if policy != "FORBIDDEN_AFTER_LOCK":
            return CheckStatus.FAIL, "", [f"redraw_policy={policy!r}"]
        return CheckStatus.PASS, str(policy), []

    if identifier == "SP-04":
        unit = lock.get("split_unit")
        if unit != "recording":
            return CheckStatus.FAIL, "", [
                f"split_unit={unit!r}；以 500 個 measurement point 當獨立樣本"
                "是 SRC-PLAN §1 明列的偽重複"
            ]
        return CheckStatus.PASS, str(unit), []

    if identifier == "SP-05":
        minimum = lock.get("minimum_per_class")
        counts = lock.get("counts", {})
        if minimum is None or not counts:
            return CheckStatus.FAIL, "", ["lock 缺 minimum_per_class 或 counts"]
        short = {
            cls: values.get("eligible")
            for cls, values in counts.items()
            if int(values.get("eligible", 0)) < int(minimum)
        }
        if short:
            return CheckStatus.FAIL, "", [
                f"這些 class 未達 minimum_per_class={minimum}：{short}"
            ]
        return CheckStatus.PASS, f"每類皆 >= {minimum}", []

    if identifier == "SP-06":
        rule = str(lock.get("group_rule", ""))
        evidence = str(lock.get("group_rule_evidence", "")).strip()
        if rule == "seeded_stratified_recording" and not evidence:
            return CheckStatus.FAIL, "", [
                "退回 seeded stratified 但未附證據；"
                "§7.9 要求先證明沒有可用的 session/time grouping"
            ]
        return CheckStatus.PASS, f"{rule}（證據 {len(evidence)} 字）", []

    raise KeyError(f"unhandled policy check {identifier!r}")
