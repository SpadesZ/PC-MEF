# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 audit e1-gates 呼叫；讀 data/inventory、data/splits、
#         outputs/simulation、outputs/surrogate、provenance、freeze 與 tests
#         底下的證據 artifact；輸出 AuditReport 並落 e1_gate_audit.json。
#         本檔只讀不寫被稽核的產物。
# 檔案路徑: pcmef/audit/e1_gates.py
# 產生時間: 2026-08-27 08:20 +08:00
# 版本: v0.1.0
# 功能說明: 逐項檢查 E1 開跑前必須具備的十二個條件，並明確區分「檢查不通過」
#           與「證據還沒產出」。目前只有六個 gate 有產物，其餘六個回報為
#           尚未產出 —— 那是正確結果，不是失敗。
# 模組定位: SRC-SAI §12 E1 Experiment-Ready Gate 的可執行稽核器。
#           它「不是」產生證據的人 —— 刻意先於被稽核的產物存在，
#           這樣它就不會在事後被寫成剛好符合已產出的結果。
# 主要責任:
#   1. GATE_SPECS 依 §12 逐列宣告十二個 gate 的要求與證據路徑
#   2. audit_e1_gates() 逐 gate 判定並彙整成 AuditReport
#   3. _check_g01() 驗五層計數的單調性與排除帳一致
#   4. _check_g02() 驗 split registry 無 ID 碰撞且三組雜湊齊全
#   5. _check_g03() 驗 simulation smoke manifest 無失敗場景
#   6. _check_g04() 驗 surrogate 四特徵無 NaN/Inf
#   7. _check_g08() 驗 Sigma provenance 為 RESOLVED
#   8. _check_g09() 驗 real_split_policy.lock 存在且 heldout access_count=0
#   9. _lock_gate() 供尚未產出的 lock 類 gate 共用判定
# 維護提醒:
#   - 不得把尚未產出的 gate 報成 FAIL。Batch 6/8 的產物本來就還不存在，
#     報成失敗會讓這份報告從第一天起就是紅的，然後沒有人會再看它。
#   - 不得為了讓某個 gate 變綠而放寬內容檢查；gate 的用途是擋下 E1 開跑，
#     放寬它等於取消這道關卡。
#   - 不得讓本檔產生或修改任何被稽核的 artifact。稽核器一旦能寫，
#     「稽核先於產物」這個設計就失去意義。
#   - 新增 gate 必須同時補上 GATE_SPECS 條目與檢查函式，
#     否則會出現一個永遠 NOT_PRODUCED 的空殼。
#   - v0.1.0 新增：首版十二 gate 稽核，決策見 NOTE-022。
# 驗證方式:
#   - py -3.10 -m pytest tests/audit/test_e1_gates.py -v
#   - py -3.10 -m pcmef.cli audit e1-gates
# ------------------------------------------------------------

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pcmef.audit.result import AuditReport, CheckResult, CheckStatus
from pcmef.core.constants import SIGMA_STATUS_RESOLVED
from pcmef.core.locks import LockError, LockStore

__all__ = [
    "GateSpec",
    "GATE_SPECS",
    "ALL_GATES",
    "AuditPaths",
    "audit_e1_gates",
    "parse_gate_range",
]


@dataclass(frozen=True)
class GateSpec:
    """§12 的一列。"""

    gate_id: str
    requirement: str
    evidence: tuple[str, ...]
    owner: str = ""


@dataclass(frozen=True)
class AuditPaths:
    """證據所在位置。全部可覆寫，讓測試不必碰真實產物。"""

    inventory: Path = Path("data/inventory")
    splits: Path = Path("data/splits")
    simulation: Path = Path("outputs/simulation")
    surrogate: Path = Path("outputs/surrogate")
    provenance: Path = Path("provenance")
    freeze: Path = Path("freeze")
    tests: Path = Path("tests")


# SRC-SAI §12 E1 Experiment-Ready Gate，逐列抄錄。
# owner 記錄該證據由哪個 batch 產出，供報告說明「為什麼還沒有」。
GATE_SPECS: dict[str, GateSpec] = {
    spec.gate_id: spec
    for spec in (
        GateSpec("E1-G01", "real inventory / alignment audit PASS",
                 ("data/inventory/source_inventory.csv",
                  "data/inventory/audit_report.json"), "Batch 2"),
        GateSpec("E1-G02", "split registry 無 ID collision",
                 ("data/splits/split_registry.json",), "Batch 2"),
        GateSpec("E1-G03", "Mitsuba/mitransient smoke test 可重現",
                 ("outputs/simulation/simulation_smoke_manifest.json",), "Batch 4"),
        GateSpec("E1-G04", "surrogate 能輸出四特徵且無 NaN/Inf",
                 ("outputs/surrogate/surrogate_smoke.csv",), "Batch 5"),
        GateSpec("E1-G05", "±offset scenario 可批次產生",
                 ("outputs/simulation/scenario_generation_report.json",), "Batch 8"),
        GateSpec("E1-G06", "metrics unit tests 對 synthetic toy example 正確",
                 ("tests/e1_metrics.xml",), "Batch 6"),
        GateSpec("E1-G07",
                 "initial_simulation + calibrated_simulation + metric_config 已鎖",
                 ("freeze/e1_candidates.lock.json",), "Batch 6"),
        GateSpec("E1-G08", "四特徵 Primary 時 Sigma provenance status=RESOLVED",
                 ("provenance/sigma_resolution.json",), "Batch 3"),
        GateSpec("E1-G09",
                 "real_split_policy.lock 已存在且早於 calibration；heldout access_count=0",
                 ("freeze/real_split_policy.lock.json",), "Batch 2"),
        GateSpec("E1-G10", "e1_evaluation_design.lock 已鎖（matched realization）",
                 ("freeze/e1_evaluation_design.lock.json",), "Batch 6"),
        GateSpec("E1-G11", "e1_scientific_rule.lock 已鎖（pooled IQR + paired CI）",
                 ("freeze/e1_scientific_rule.lock.json",), "Batch 6"),
        GateSpec("E1-G12", "claim-boundary lock：E1 fidelity 只支撐 ToF surrogate",
                 ("freeze/claim_boundary.lock.json",), "Batch 6"),
    )
}

ALL_GATES: tuple[str, ...] = tuple(GATE_SPECS)


def parse_gate_range(text: str) -> tuple[str, ...]:
    """解析 --require 的值。支援 `G01:G12`、`E1-G01:E1-G12` 與逗號清單。"""
    raw = text.strip()
    if ":" in raw:
        start, _, end = raw.partition(":")
        start, end = _normalise(start), _normalise(end)
        if start not in GATE_SPECS or end not in GATE_SPECS:
            raise ValueError(f"unknown gate in range {text!r}; known: {ALL_GATES}")
        lo, hi = ALL_GATES.index(start), ALL_GATES.index(end)
        if lo > hi:
            raise ValueError(f"range {text!r} is inverted")
        return ALL_GATES[lo : hi + 1]
    gates = tuple(_normalise(part) for part in raw.split(",") if part.strip())
    unknown = [g for g in gates if g not in GATE_SPECS]
    if unknown:
        raise ValueError(f"unknown gate(s) {unknown}; known: {ALL_GATES}")
    return gates


def _normalise(token: str) -> str:
    token = token.strip().upper()
    return token if token.startswith("E1-") else f"E1-{token}"


# ---------------------------------------------------------------------------
# 個別 gate 的內容檢查
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_g01(paths: AuditPaths) -> tuple[CheckStatus, str, list[str]]:
    """五層計數必須單調不增，且排除數與落差相符（§7.8）。"""
    report_path = paths.inventory / "audit_report.json"
    inventory_path = paths.inventory / "source_inventory.csv"
    if not report_path.exists() or not inventory_path.exists():
        return CheckStatus.NOT_PRODUCED, "", []

    report = _read_json(report_path)
    counts = report.get("counts", {})
    ladder = [
        "nominal_logical_recordings", "physical_source_files",
        "canonical_recordings", "valid_recordings", "e1_eligible_recordings",
    ]
    missing = [key for key in ladder if key not in counts]
    if missing:
        return CheckStatus.FAIL, "", [f"audit_report.json 缺計數欄位 {missing}"]

    findings: list[str] = []
    # physical 可以大於 nominal（四個 metric 分檔存放），因此只從 canonical 起檢查。
    for earlier, later in zip(ladder[2:], ladder[3:]):
        if counts[later] > counts[earlier]:
            findings.append(
                f"{later}={counts[later]} 大於上一層 {earlier}={counts[earlier]}；"
                "五層計數必須單調不增"
            )
    if counts["e1_eligible_recordings"] == 0:
        findings.append(
            "e1_eligible_recordings 為 0；四特徵 E1 primary 被 E1-G08 擋下"
        )

    excluded = sum(report.get("exclusions_by_reason", {}).values())
    gap = counts["canonical_recordings"] - counts["valid_recordings"]
    if excluded != gap:
        findings.append(
            f"排除帳 {excluded} 筆與 canonical-valid 落差 {gap} 筆不符；"
            "每一筆流失都必須有具名理由"
        )

    detail = (
        f"{counts['e1_eligible_recordings']}/{counts['nominal_logical_recordings']} "
        f"e1-eligible，排除 {excluded} 筆"
    )
    return (CheckStatus.FAIL if findings else CheckStatus.PASS), detail, findings


def _check_g02(paths: AuditPaths) -> tuple[CheckStatus, str, list[str]]:
    """split registry 無 ID 碰撞，且三組 set hash 齊全（NOTE-014）。"""
    path = paths.splits / "split_registry.json"
    if not path.exists():
        return CheckStatus.NOT_PRODUCED, "", []

    registry = _read_json(path)
    findings: list[str] = []
    if registry.get("collision_count", -1) != 0:
        findings.append(f"collision_count={registry.get('collision_count')}，必須為 0")

    assignments = registry.get("assignments", {})
    if not assignments:
        findings.append("assignments 為空；registry 無法稽核任何切分")

    for key in ("eligible_set_hash", "calibration_set_hash", "heldout_real_set_hash"):
        if not registry.get(key):
            findings.append(
                f"缺 {key}；registry 進版控而 lock 不進，這三個雜湊是唯一的對照點"
            )

    roles = set(assignments.values())
    if assignments and not roles <= {"calibration", "heldout_real"}:
        findings.append(f"registry 出現非 real split 的角色 {sorted(roles - {'calibration', 'heldout_real'})}")

    totals = registry.get("totals", {})
    if totals and totals.get("eligible") != len(assignments):
        findings.append(
            f"totals.eligible={totals.get('eligible')} 與 assignments 筆數 "
            f"{len(assignments)} 不符"
        )

    detail = f"{len(assignments)} 筆指派，0 碰撞" if not findings else ""
    return (CheckStatus.FAIL if findings else CheckStatus.PASS), detail, findings


def _check_g03(paths: AuditPaths) -> tuple[CheckStatus, str, list[str]]:
    """simulation smoke 無失敗場景，且截斷診斷在合理範圍（NOTE-013）。"""
    path = paths.simulation / "simulation_smoke_manifest.json"
    if not path.exists():
        return CheckStatus.NOT_PRODUCED, "", []

    manifest = _read_json(path)
    counts = manifest.get("counts", {})
    findings: list[str] = []
    if int(counts.get("failed", 0)) > 0:
        findings.append(f"{counts['failed']} 個 scenario 失敗")
    if int(counts.get("ok", 0)) == 0:
        findings.append("沒有任何成功的 scenario；smoke 無法證明可重現")

    for scenario in manifest.get("scenarios", []):
        edge = scenario.get("edge_fraction")
        if edge is not None and float(edge) >= 0.5:
            findings.append(
                f"{scenario.get('scenario_id')} 的 edge_fraction={edge} >= 0.5；"
                "回波可能被時間窗截斷（NOTE-013）"
            )

    detail = f"{counts.get('ok', 0)} ok / {counts.get('failed', 0)} failed"
    return (CheckStatus.FAIL if findings else CheckStatus.PASS), detail, findings


def _check_g04(paths: AuditPaths) -> tuple[CheckStatus, str, list[str]]:
    """surrogate 四特徵無 NaN/Inf。"""
    path = paths.surrogate / "surrogate_smoke.csv"
    if not path.exists():
        return CheckStatus.NOT_PRODUCED, "", []

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return CheckStatus.FAIL, "", ["surrogate_smoke.csv 是空的；沒有任何場景被驗證"]

    rows = list(csv.DictReader(text.splitlines()))
    findings: list[str] = []
    if not rows:
        findings.append("surrogate_smoke.csv 只有表頭，沒有資料列")

    non_finite = [
        row.get("scenario_id", "?")
        for row in rows
        if str(row.get("all_finite", "")).strip().lower() not in ("true", "1")
    ]
    if non_finite:
        findings.append(f"這些場景含 NaN/Inf：{non_finite}")

    detail = f"{len(rows)} 個場景，全部有限" if not findings else ""
    return (CheckStatus.FAIL if findings else CheckStatus.PASS), detail, findings


def _check_g08(paths: AuditPaths) -> tuple[CheckStatus, str, list[str]]:
    """Sigma register/scale provenance 必須為 RESOLVED（NOTE-010）。"""
    path = paths.provenance / "sigma_resolution.json"
    if not path.exists():
        return CheckStatus.NOT_PRODUCED, "", []

    resolution = _read_json(path)
    status = str(resolution.get("status", ""))
    findings: list[str] = []
    if status != SIGMA_STATUS_RESOLVED:
        findings.append(
            f"status={status!r}，四特徵 E1 primary 需要 {SIGMA_STATUS_RESOLVED}"
        )
    for reason in resolution.get("blocking_reasons", []):
        findings.append(str(reason))

    register = (resolution.get("register") or {}).get("resolved")
    divisor = (resolution.get("scaling") or {}).get("resolved_divisor")
    detail = f"register={register} divisor=/{divisor}"
    return (CheckStatus.FAIL if findings else CheckStatus.PASS), detail, findings


def _check_g09(paths: AuditPaths) -> tuple[CheckStatus, str, list[str]]:
    """real_split_policy.lock 存在、完整，且凍結時 heldout 未被取用。"""
    store = LockStore(paths.freeze)
    if not store.exists("real_split_policy"):
        return CheckStatus.NOT_PRODUCED, "", []

    try:
        payload = store.load("real_split_policy")
    except LockError as error:
        return CheckStatus.FAIL, "", [str(error)]

    findings: list[str] = []
    access = payload.get("heldout_access_count_at_lock")
    if access != 0:
        findings.append(
            f"heldout_access_count_at_lock={access}；凍結時必須為 0，"
            "否則 held-out 已被消耗"
        )
    if payload.get("creation_phase") != "AFTER_M0_BEFORE_ANY_CALIBRATION":
        findings.append(
            f"creation_phase={payload.get('creation_phase')!r}；"
            "Appendix H1 要求在 M0 之後、任何 calibration 之前建立"
        )
    if payload.get("redraw_policy") != "FORBIDDEN_AFTER_LOCK":
        findings.append(f"redraw_policy={payload.get('redraw_policy')!r}，必須禁止重抽")

    totals = payload.get("totals", {})
    detail = (
        f"{totals.get('calibration')}/{totals.get('heldout_real')} "
        f"seed={payload.get('seed')}"
    )
    return (CheckStatus.FAIL if findings else CheckStatus.PASS), detail, findings


def _lock_gate(lock_name: str) -> Callable[[AuditPaths], tuple[CheckStatus, str, list[str]]]:
    """產生一個「該 lock 是否已凍結且完整」的檢查。"""

    def check(paths: AuditPaths) -> tuple[CheckStatus, str, list[str]]:
        store = LockStore(paths.freeze)
        if not store.exists(lock_name):
            return CheckStatus.NOT_PRODUCED, "", []
        try:
            store.load(lock_name)
        except LockError as error:
            return CheckStatus.FAIL, "", [str(error)]
        return CheckStatus.PASS, f"{lock_name} {store.load_hash(lock_name)[:16]}", []

    return check


def _file_gate(relative: str) -> Callable[[AuditPaths], tuple[CheckStatus, str, list[str]]]:
    """產生一個「該證據檔是否存在且非空」的檢查。"""

    def check(paths: AuditPaths) -> tuple[CheckStatus, str, list[str]]:
        root = {
            "outputs/simulation": paths.simulation,
            "tests": paths.tests,
        }[relative.rsplit("/", 1)[0]]
        path = root / relative.rsplit("/", 1)[1]
        if not path.exists():
            return CheckStatus.NOT_PRODUCED, "", []
        if path.stat().st_size == 0:
            return CheckStatus.FAIL, "", [f"{path} 存在但是空的"]
        return CheckStatus.PASS, f"{path.name} {path.stat().st_size} bytes", []

    return check


#: gate_id -> 檢查函式。缺項代表尚未實作對應檢查，會被 audit_e1_gates 擋下。
_CHECKS: dict[str, Callable[[AuditPaths], tuple[CheckStatus, str, list[str]]]] = {
    "E1-G01": _check_g01,
    "E1-G02": _check_g02,
    "E1-G03": _check_g03,
    "E1-G04": _check_g04,
    "E1-G05": _file_gate("outputs/simulation/scenario_generation_report.json"),
    "E1-G06": _file_gate("tests/e1_metrics.xml"),
    "E1-G07": _lock_gate("e1_candidates"),
    "E1-G08": _check_g08,
    "E1-G09": _check_g09,
    "E1-G10": _lock_gate("e1_evaluation_design"),
    "E1-G11": _lock_gate("e1_scientific_rule"),
    "E1-G12": _lock_gate("claim_boundary"),
}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def audit_e1_gates(paths: AuditPaths | None = None) -> AuditReport:
    """逐 gate 判定並彙整。尚未產出的 gate 回報 NOT_PRODUCED 而非 FAIL。"""
    resolved = paths or AuditPaths()
    missing_checks = sorted(set(GATE_SPECS) - set(_CHECKS))
    if missing_checks:
        raise RuntimeError(
            f"gates {missing_checks} are declared but have no check function; "
            "an unchecked gate is worse than no gate — it looks audited"
        )

    results: list[CheckResult] = []
    for gate_id, spec in GATE_SPECS.items():
        status, detail, findings = _CHECKS[gate_id](resolved)
        if status is CheckStatus.NOT_PRODUCED and not detail:
            detail = f"證據尚未產出（{spec.owner}）"
        results.append(
            CheckResult(
                identifier=gate_id,
                requirement=spec.requirement,
                status=status,
                detail=detail,
                evidence=spec.evidence,
                findings=tuple(findings),
            )
        )
    return AuditReport(name="e1_gates", results=tuple(results))
