# PC-MEF Research System source maintenance contract
# 上下游: 讀 configs/parameter_registry.yaml、estimator_preregistration.yaml
#         與 outputs/ 下的 ambient / estimator / simulation artifact；
#         由 cli 的 audit initial-simulation 呼叫；其結論決定
#         initial_simulation.lock 可否凍結。
# 檔案路徑: pcmef/audit/initial_simulation.py
# 產生時間: 2026-08-28 16:30 +08:00
# 版本: v0.1.0
# 功能說明: 判定「initial simulation 現在可不可以凍結」—— 檢查場景拓樸、
#           far-side 回波家族、Ambient 觀測量定義、estimator 是否已定案、
#           所有旋鈕是否被 registry 管住、未校準值是否明確標記。
# 模組定位: initial_simulation.lock 的凍結前置稽核。它「不是」formal-run 防線 ——
#           兩者判準**刻意不同**：initial freeze 發生在 calibration **之前**，
#           因此「參數仍是 placeholder」是**預期狀態**而非阻塞（NOTE-036）。
# 主要責任:
#   1. IS-01..IS-06 六項逐條判定，對應 M2 closure 的六個條件
#   2. 明確區分「未校準」（可接受）與「未受管制」（阻塞）
#   3. 產出 initial_simulation_readiness.json 供 lock 引用
# 維護提醒:
#   - 不得把 params audit 的 exit code 直接當本稽核的判準；
#     那條線問的是「可否進 formal run」，這條線問的是「可否凍結 pre-calibration
#     狀態」。混用會讓 initial freeze 永遠不可能發生（NOTE-036）。
#   - 不得加入「模擬距離是否接近真實分佈」這類條件；initial simulation
#     **不要求**貼近真實，那是 calibration 之後才談的事。
#   - 不得在 IS-04 接受「尚未選定」的 estimator；estimator 是 lock 的內容之一，
#     未定案就凍結等於凍了一個會變的東西。
#   - v0.1.0 新增：首版 initial simulation readiness（NOTE-036）。
# 驗證方式:
#   - py -3.10 -m pcmef.cli audit initial-simulation
#   - py -3.10 -m pytest tests/audit/test_initial_simulation.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pcmef.audit.result import AuditReport, CheckResult, CheckStatus

__all__ = ["INITIAL_SIMULATION_CHECKS", "audit_initial_simulation"]

INITIAL_SIMULATION_CHECKS: dict[str, str] = {
    "IS-01": "physical scene topology is defensible",
    "IS-02": "a far-side foil return family exists with tracing evidence",
    "IS-03": "the Ambient observable is defined as a dedicated ambient pass",
    "IS-04": "the distance estimator is selected and fixed",
    "IS-05": "every calibration knob is governed by the registry firewall",
    "IS-06": "uncalibrated values are explicitly placeholder / nuisance",
}


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def audit_initial_simulation(
    ambient_report: str | Path = "outputs/ambient_audit/ambient_observable_audit.json",
    estimator_report: str | Path = "outputs/estimator_select/estimator_selection.json",
    simulation_manifest: str | Path = "outputs/phaseBC_verify/simulation_smoke_manifest.json",
) -> AuditReport:
    """六項逐條判定。回傳順序固定，便於逐次比對。"""
    from pcmef.core.parameters import (
        ParameterRegistry,
        ParameterRegistryError,
        binding_coverage,
        live_value_drift,
    )

    results: list[CheckResult] = []

    try:
        registry = ParameterRegistry.load()
    except ParameterRegistryError as error:
        registry = None
        registry_error = str(error)
    else:
        registry_error = ""

    # -- IS-01 場景拓樸 ----------------------------------------------------
    # 可由程式直接驗，不依賴任何 artifact：內圓柱一律建立（空瓶是殼+空氣+殼，
    # 不是實心玻璃柱）、canonical scene 內沒有純視覺用的幾何。
    from pcmef.simulation import mitsuba_adapter as ma

    source = Path(ma.__file__).read_text(encoding="utf-8")
    has_backdrop = '"backdrop"' in source
    interior_always = 'scene["bottle_interior"] = interior' in source
    if not has_backdrop and interior_always:
        results.append(
            CheckResult(
                identifier="IS-01",
                requirement=INITIAL_SIMULATION_CHECKS["IS-01"],
                status=CheckStatus.PASS,
                detail="interior cylinder is built for every preset (empty bottle is "
                "shell+air+shell) and no RGB-only geometry sits in the ToF path",
                
            )
        )
    else:
        reasons = []
        if has_backdrop:
            reasons.append("a backdrop object is back in the canonical scene")
        if not interior_always:
            reasons.append("the interior cylinder is conditional again")
        results.append(
            CheckResult(
                identifier="IS-01",
                requirement=INITIAL_SIMULATION_CHECKS["IS-01"],
                status=CheckStatus.FAIL,
                detail="; ".join(reasons),
                findings=tuple(reasons),
            )
        )

    # -- IS-02 far-side foil family ---------------------------------------
    manifest = _load(Path(simulation_manifest))
    if manifest is None:
        results.append(
            CheckResult(
                identifier="IS-02",
                requirement=INITIAL_SIMULATION_CHECKS["IS-02"],
                status=CheckStatus.NOT_PRODUCED,
                detail=f"no simulation manifest at {simulation_manifest}; run `sim smoke`",
                
            )
        )
    else:
        foil_present = "foil" in source and "_FOIL_REFLECTANCE_940NM" in source
        scenarios = manifest.get("scenarios", [])
        energies = [
            s.get("transient", {}).get("total_energy", 0.0)
            for s in scenarios
            if s.get("status") == "OK"
        ]
        if foil_present and energies and all(e > 0 for e in energies):
            results.append(
                CheckResult(
                identifier="IS-02",
                requirement=INITIAL_SIMULATION_CHECKS["IS-02"],
                status=CheckStatus.PASS,
                detail=f"foil reflector present in the canonical scene; {len(energies)} "
                    "scenario(s) return non-zero energy (bounce lineage in NOTE-029)",
                
            )
            )
        else:
            results.append(
                CheckResult(
                identifier="IS-02",
                requirement=INITIAL_SIMULATION_CHECKS["IS-02"],
                status=CheckStatus.FAIL,
                detail="the far-side foil reflector is missing or returns no energy",
                findings=("the far-side foil reflector is missing or returns no energy",),
            )
            )

    # -- IS-03 Ambient 觀測量 ---------------------------------------------
    ambient = _load(Path(ambient_report))
    if ambient is None:
        results.append(
            CheckResult(
                identifier="IS-03",
                requirement=INITIAL_SIMULATION_CHECKS["IS-03"],
                status=CheckStatus.NOT_PRODUCED,
                detail=f"no ambient audit at {ambient_report}; run `sim ambient-check`",
                
            )
        )
    elif ambient.get("counts", {}).get("fail", 1) == 0:
        results.append(
            CheckResult(
                identifier="IS-03",
                requirement=INITIAL_SIMULATION_CHECKS["IS-03"],
                status=CheckStatus.PASS,
                detail=f"all {ambient['counts']['total']} ambient/signal separation checks "
                "pass (A1 monotonic, A2 VCSEL-independent, A3 active component "
                "vanishes, A4 environment-off is zero)",
                
            )
        )
    else:
        failed = [
            c["check_id"] for c in ambient.get("checks", []) if c["status"] != "PASS"
        ]
        results.append(
            CheckResult(
                identifier="IS-03",
                requirement=INITIAL_SIMULATION_CHECKS["IS-03"],
                status=CheckStatus.FAIL,
                detail=f"failed checks: {failed}",
                findings=(f"failed checks: {failed}",),
            )
        )

    # -- IS-04 estimator 已定案 -------------------------------------------
    estimator = _load(Path(estimator_report))
    if estimator is None:
        results.append(
            CheckResult(
                identifier="IS-04",
                requirement=INITIAL_SIMULATION_CHECKS["IS-04"],
                status=CheckStatus.NOT_PRODUCED,
                detail=f"no estimator selection at {estimator_report}; "
                "run `surrogate estimator-select`",
                
            )
        )
    elif estimator.get("outcome") == "SELECTED":
        results.append(
            CheckResult(
                identifier="IS-04",
                requirement=INITIAL_SIMULATION_CHECKS["IS-04"],
                status=CheckStatus.PASS,
                detail=f"estimator {estimator['selected']} selected under preregistration "
                f"{estimator['preregistration_hash'][:16]}",
                
            )
        )
    else:
        results.append(
            CheckResult(
                identifier="IS-04",
                requirement=INITIAL_SIMULATION_CHECKS["IS-04"],
                status=CheckStatus.FAIL,
                detail=f"outcome is {estimator.get('outcome')}, not SELECTED: "
                f"{estimator.get('reason', '')}. Freezing a lock whose estimator is "
                "still undecided would freeze something that can still change.",
                findings=(f"outcome is {estimator.get('outcome')}, not SELECTED: "
                f"{estimator.get('reason', '')}. Freezing a lock whose estimator is "
                "still undecided would freeze something that can still change.",),
            )
        )

    # -- IS-05 所有旋鈕被 registry 管住 -----------------------------------
    if registry is None:
        results.append(CheckResult(
                identifier="IS-05",
                requirement=INITIAL_SIMULATION_CHECKS["IS-05"],
                status=CheckStatus.FAIL,
                detail=registry_error,
                findings=(registry_error,),
            ))
    else:
        coverage = binding_coverage(registry)
        drift = live_value_drift(registry)
        undecided = [g.group_id for g in registry.groups if not g.decided]
        gauge_issues = registry.gauge_violations()
        problems = []
        if coverage["unbound"]:
            problems.append(f"unbound parameters: {coverage['unbound']}")
        if drift:
            problems.append(f"{len(drift)} registry/code drift(s)")
        if undecided:
            problems.append(f"undecided confounded groups: {undecided}")
        if gauge_issues:
            problems.append(f"{len(gauge_issues)} unsanctioned gauge(s)")
        if problems:
            results.append(
                CheckResult(
                    identifier="IS-05",
                    requirement=INITIAL_SIMULATION_CHECKS["IS-05"],
                    status=CheckStatus.FAIL,
                    detail="; ".join(problems),
                    findings=tuple(problems),
                )
            )
        else:
            results.append(
                CheckResult(
                identifier="IS-05",
                requirement=INITIAL_SIMULATION_CHECKS["IS-05"],
                status=CheckStatus.PASS,
                detail=f"{len(registry.parameters)} parameters registered; bindings "
                    f"code={len(coverage['code'])} config={len(coverage['config_supplied'])} "
                    f"unbound=0; drift=0; all {len(registry.groups)} confounded "
                    "groups adjudicated",
                
            )
            )

    # -- IS-06 未校準值明確標記 -------------------------------------------
    # 這一條**不要求**參數已校準 —— initial freeze 發生在 calibration 之前。
    # 它要求的是：每個未校準的值都有明確的狀態與（可校準者的）搜尋邊界。
    if registry is None:
        results.append(CheckResult(
                identifier="IS-06",
                requirement=INITIAL_SIMULATION_CHECKS["IS-06"],
                status=CheckStatus.FAIL,
                detail=registry_error,
                findings=(registry_error,),
            ))
    else:
        missing_range = [
            p.name
            for p in registry.parameters
            if p.kind == "calibration_only" and p.allowed_range is None
            and p.name != "foil_orientation"  # 類別型參數，無數值區間
        ]
        unmarked = [
            p.name
            for p in registry.parameters
            if p.kind == "calibration_only" and p.provenance_status
            not in {"PLACEHOLDER", "UNKNOWN", "RECONSTRUCTED", "CONFIRMED"}
        ]
        if missing_range or unmarked:
            problems = []
            if missing_range:
                problems.append(
                    f"calibration_only without allowed_range: {missing_range}"
                )
            if unmarked:
                problems.append(f"calibration_only with an odd status: {unmarked}")
            results.append(
                CheckResult(
                    identifier="IS-06",
                    requirement=INITIAL_SIMULATION_CHECKS["IS-06"],
                    status=CheckStatus.FAIL,
                    detail="; ".join(problems),
                    findings=tuple(problems),
                )
            )
        else:
            unresolved = registry.unresolved()
            results.append(
                CheckResult(
                identifier="IS-06",
                requirement=INITIAL_SIMULATION_CHECKS["IS-06"],
                status=CheckStatus.PASS,
                detail=f"{len(unresolved)} value(s) remain uncalibrated and every one "
                    "carries an explicit status plus a search range. This is the "
                    "expected state before calibration, not a blocker.",
                
            )
            )

    return AuditReport(name="initial_simulation", results=tuple(results))
