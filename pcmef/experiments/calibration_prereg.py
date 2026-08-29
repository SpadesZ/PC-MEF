# PC-MEF Research System source maintenance contract
# 上下游: 讀 configs/calibration_preregistration.yaml、configs/parameter_registry.yaml、
#         freeze/initial_simulation.lock.json（經 core.formal_loader）、
#         freeze/real_split_policy.lock.json 與 freeze/errata/ERR-001；
#         由 cli 的 calibration preregister --validate / --freeze 呼叫；
#         寫出 freeze/preregistrations/CAL-PREREG-001.prereg.json。
# 檔案路徑: pcmef/experiments/calibration_prereg.py
# 產生時間: 2026-08-29 12:45 +08:00
# 版本: v0.1.0
# 功能說明: 驗證校準預註冊檔的完整性（CP-01..CP-12），並在通過後把它連同
#           所有它所依賴的雜湊一起凍成不可覆寫的記錄。
# 模組定位: 校準預註冊的**驗證與凍結閘門**。它不執行校準，也不讀取
#           calibration partition —— 本模組存在的全部意義，是讓「先寫完規格
#           再看資料」這件事有一份可稽核的憑據，而不是一句承諾。
# 主要責任:
#   1. validate_preregistration() 執行 CP-01..CP-12
#   2. 逐項比對 26 個 calibration_only 參數的階段覆蓋，不得遺漏或重複
#   3. 確認 optimizer 搜尋空間不含 gauge/derived/fixed/estimator 成員
#   4. 確認邊界與已凍結的 initial_simulation.lock 逐字相同
#   5. freeze_preregistration() 綁定 protocol/commit/split/registry/lock/erratum
# 維護提醒:
#   - 不得在本模組內讀取 calibration partition 的任何數值；驗證只看規格與雜湊。
#   - 不得放寬 CP-02：一旦 gauge 成員能進搜尋空間，CG-1/2/3 的裁決就等於沒做。
#   - 不得允許 CP-03 的邊界「近似相同」；lock 寫什麼就是什麼。
#   - 不得在已存在 calibration artifact 之後才凍結預註冊（CP-12）；
#     那個順序一旦反過來，預註冊就失去它唯一的功能。
#   - 不得覆寫已凍結的預註冊記錄；要改判準必須開 amendment 並新編號。
#   - v0.1.0 新增：首版校準預註冊驗證與凍結（NOTE-041）。
# 驗證方式:
#   - py -3.10 -m pcmef.cli calibration preregister --validate
#   - py -3.10 -m pytest tests/unit/test_calibration_prereg.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.audit.result import AuditReport, CheckResult, CheckStatus
from pcmef.core.hash import hash_object
from pcmef.experiments.calibration_plan import (
    CalibrationPlanError,
    bounds_resolution_hash,
    resolve_numeric_bounds,
    stage_budgets,
)

__all__ = [
    "PREREGISTRATION_PATH",
    "PREREGISTRATION_ID",
    "REQUIRED_CHECKS",
    "load_protocol",
    "protocol_hash",
    "validate_preregistration",
    "freeze_preregistration",
    "PreregistrationError",
]

PREREGISTRATION_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "calibration_preregistration.yaml"
)
PREREGISTRATION_ID = "CAL-PREREG-002"

#: 凍結前必須全部 PASS。NOT_PRODUCED 在這裡不是可接受狀態 ——
#: 預註冊的每一項都是**現在就該寫完**的，沒有「之後補」的欄位。
REQUIRED_CHECKS: tuple[str, ...] = tuple(f"CP-{n:02d}" for n in range(1, 17))

#: optimizer 在任何階段都不得觸碰的參數。前三個是 CG-1/2/3 的 gauge 固定項。
FORBIDDEN_IN_SEARCH_SPACE: frozenset[str] = frozenset(
    {
        "_SIGMA_T_REFERENCE_PER_M",
        "lighting.irradiance",
        "_ROOM_LIGHT_RADIANCE",
        "optical_path_to_distance",
    }
)

#: 掃描器會略過帶這個標記的行。它只用在**定義禁令清單本身**的那一行 ——
#: 沒有它，這份守衛會把自己的定義判成違規，然後為了通過而被整份停用。
GUARD_DEFINITION_MARKER = "REAL_MEAN_GUARD_DEFINITION"

#: 目標函數不得寫死的四類 real class mean（單位 mm）。
#: 只掃 pcmef/ 的 .py 與本預註冊檔本身 —— estimator 預註冊的
#: forbidden_selection_inputs 是**禁令清單**，把它算成違規會讓
#: 「寫下禁令」變成違規本身。
FORBIDDEN_REAL_MEAN_LITERALS: tuple[str, ...] = ("100.91", "113.87", "105.57", "79.51")  # REAL_MEAN_GUARD_DEFINITION


class PreregistrationError(RuntimeError):
    """預註冊檔缺漏、驗證失敗，或試圖覆寫已凍結的記錄。"""


def load_protocol(path: str | Path | None = None) -> dict[str, Any]:
    import yaml

    target = Path(path) if path is not None else PREREGISTRATION_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"calibration preregistration not found at {target}. The objective, "
            "the stagewise grouping and the optimizer must be frozen before the "
            "calibration partition is read; without the file there is nothing "
            "distinguishing a fit from a post-hoc rationalisation."
        )
    return yaml.safe_load(target.read_text(encoding="utf-8"))


def protocol_hash(path: str | Path | None = None) -> str:
    return hash_object(load_protocol(path))


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _ok(identifier: str, requirement: str, detail: str, evidence=()) -> CheckResult:
    return CheckResult(
        identifier=identifier,
        requirement=requirement,
        status=CheckStatus.PASS,
        detail=detail,
        evidence=tuple(evidence),
    )


def _bad(identifier: str, requirement: str, findings: list[str], detail="") -> CheckResult:
    return CheckResult(
        identifier=identifier,
        requirement=requirement,
        status=CheckStatus.FAIL,
        detail=detail or "違反預註冊契約",
        findings=tuple(findings),
    )


def _stage_parameters(protocol: dict[str, Any]) -> dict[str, list[str]]:
    """回傳 stage_id -> 該階段宣告要 fit 的參數名稱。"""
    out: dict[str, list[str]] = {}
    for stage in protocol.get("stagewise", []):
        out[str(stage.get("id"))] = list(stage.get("parameters") or [])
    return out


def _not_fitted_names(protocol: dict[str, Any]) -> list[str]:
    return [str(m["name"]) for m in protocol.get("not_fitted", {}).get("members", [])]


def _numeric_interpretations(protocol: dict[str, Any]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for entry in protocol.get("bounds", {}).get("declared_numeric_interpretations", []):
        out[str(entry["parameter"])] = [float(v) for v in entry["interpreted_as"]]
    return out


def _as_floats(bound: Any) -> list[float] | None:
    """只在邊界**原生就是數字**時回傳數值，否則 None。

    刻意不呼叫 `float(v)` 去救字串：`float("1.0e6")` 會成功，於是一個
    寫錯型別的邊界會被安靜地接受，而「邊界寫錯」與「邊界被改過」
    從此看起來一樣。字串邊界必須走 declared_numeric_interpretations
    這條要逐項具名的路。
    """
    if not isinstance(bound, list) or len(bound) != 2:
        return None
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in bound):
        return None
    return [float(v) for v in bound]


# ---------------------------------------------------------------------------
# CP-01..CP-12
# ---------------------------------------------------------------------------


def validate_preregistration(
    protocol_path: str | Path | None = None,
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    registry_path: str | Path | None = None,
) -> AuditReport:
    """執行全部預註冊檢查。**不讀取 calibration partition 的任何數值。**"""
    from pcmef.core.formal_loader import load_formal_lock
    from pcmef.core.locks import LockStore
    from pcmef.core.parameters import ParameterRegistry

    root = Path(repo_root)
    protocol = load_protocol(protocol_path)
    registry = ParameterRegistry.load(registry_path)
    results: list[CheckResult] = []

    calibration_only = sorted(
        p.name for p in registry.parameters if p.kind == "calibration_only"
    )
    stages = _stage_parameters(protocol)
    fitted = [name for names in stages.values() for name in names]
    not_fitted = _not_fitted_names(protocol)

    # -- CP-01 覆蓋：每個 calibration_only 參數恰好被歸類一次 -------------
    req = "26 個 calibration_only 參數必須各被歸入一個階段或 not_fitted，恰好一次"
    declared = fitted + not_fitted
    duplicates = sorted({n for n in declared if declared.count(n) > 1})
    missing = [n for n in calibration_only if n not in declared]
    unknown = [n for n in declared if n not in calibration_only]
    findings = []
    if missing:
        findings.append(f"未歸類的 calibration_only 參數：{missing}")
    if duplicates:
        findings.append(f"重複歸類（會被 fit 兩次或定義衝突）：{duplicates}")
    if unknown:
        findings.append(f"預註冊提到但 registry 內不是 calibration_only：{unknown}")
    results.append(
        _bad("CP-01", req, findings)
        if findings
        else _ok(
            "CP-01",
            req,
            f"{len(calibration_only)} 項全部歸類："
            f"fit {len(fitted)} / not_fitted {len(not_fitted)}",
        )
    )

    # -- CP-02 搜尋空間不得含 gauge / derived / fixed / estimator ----------
    req = "optimizer 不得觸碰 CG gauge 固定項、derived、fixed/topology 與 estimator"
    non_calibration = {
        p.name for p in registry.parameters if p.kind != "calibration_only"
    }
    findings = []
    for name in fitted:
        if name in FORBIDDEN_IN_SEARCH_SPACE:
            findings.append(f"{name} 是 gauge/derived 固定項，不得進入擬合")
        if name in non_calibration:
            findings.append(f"{name} 在 registry 內不是 calibration_only")
    for name in ("detection_threshold_sigma", "min_return_bins", "SELECTED_ESTIMATOR"):
        if name in fitted:
            findings.append(f"{name} 屬 estimator 身分，calibration 不得更動（NOTE-037）")
    # gauge 固定項也必須被明列在 forbidden_parameters 內，而不只是「剛好沒寫」。
    listed = {
        str(e["name"])
        for e in protocol.get("forbidden_parameters", {}).get("gauge_fixed", [])
    }
    for name in ("_SIGMA_T_REFERENCE_PER_M", "lighting.irradiance", "_ROOM_LIGHT_RADIANCE"):
        if name not in listed:
            findings.append(f"{name} 未被明列於 forbidden_parameters.gauge_fixed")
    results.append(
        _bad("CP-02", req, findings)
        if findings
        else _ok("CP-02", req, f"搜尋空間 {len(fitted)} 項，三個 gauge 固定項皆明列且未入列")
    )

    # -- CP-03 邊界必須與已凍結的 lock 逐字相同 ---------------------------
    req = "被 fit 的參數邊界必須與 initial_simulation.lock 的 parameter_ranges 相同"
    lock_store = LockStore(freeze_dir)
    if not lock_store.exists("initial_simulation"):
        results.append(
            CheckResult(
                "CP-03", req, CheckStatus.BLOCKED,
                "initial_simulation.lock 尚未凍結，邊界無從比對",
            )
        )
        lock_payload: dict[str, Any] = {}
    else:
        resolved = load_formal_lock("initial_simulation", freeze_dir, root)
        lock_payload = resolved.payload
        frozen_ranges = lock_payload.get("parameter_ranges", {})
        interpretations = _numeric_interpretations(protocol)
        findings = []
        for name in fitted:
            if name not in frozen_ranges:
                findings.append(f"{name} 不在 lock 的 parameter_ranges 內")
                continue
            frozen = frozen_ranges[name]
            numeric = _as_floats(frozen)
            if numeric is None:
                declared_bound = interpretations.get(name)
                if declared_bound is None:
                    findings.append(
                        f"{name} 的凍結邊界 {frozen!r} 不是數值，且預註冊未宣告其數值解讀"
                    )
                    continue
                # 宣告的解讀必須與凍結字面值表示同一個十進位數。
                for raw, declared_value in zip(frozen, declared_bound):
                    if float(str(raw)) != declared_value:
                        findings.append(
                            f"{name} 宣告的數值解讀 {declared_value} 與凍結值 {raw!r} 不符"
                        )
        results.append(
            _bad("CP-03", req, findings)
            if findings
            else _ok(
                "CP-03", req,
                f"{len(fitted)} 項邊界與 lock 相符（含 {len(interpretations)} 項宣告的數值解讀）",
            )
        )

    # -- CP-04 目標函數為 distribution-level，且未寫死 real class mean -----
    req = "objective 必須是 distribution-level 且不得 hard-code 四類 real mean"
    objective = protocol.get("objective", {})
    findings = []
    if objective.get("discrepancy", {}).get("level") != "distribution":
        findings.append("objective.discrepancy.level 不是 distribution")
    if objective.get("discrepancy", {}).get("statistic") != "wasserstein_w1":
        findings.append("objective 未使用 wasserstein_w1 作為分佈距離")
    offenders = _scan_for_real_means(root, protocol_path)
    findings.extend(offenders)
    results.append(
        _bad("CP-04", req, findings)
        if findings
        else _ok("CP-04", req, "分佈層級 W1；pcmef/ 與本預註冊檔內無 real class mean 字面值")
    )

    # -- CP-05 正規化與權重事前固定 ---------------------------------------
    req = "normalization 與 weights 必須事前固定且來自 calibration split 的真實值"
    normalization = protocol.get("normalization", {})
    weights = protocol.get("weights", {})
    findings = []
    if normalization.get("scale") != "s_f":
        findings.append("normalization.scale 不是 s_f")
    if not normalization.get("frozen"):
        findings.append("normalization.frozen 不是 true：s_f 逐階段重算會讓目標可被稀釋")
    if "計算**一次**" not in str(normalization.get("computed_when", "")):
        findings.append("normalization.computed_when 未載明只計算一次")
    if not weights.get("fixed_before_seeing_data"):
        findings.append("weights.fixed_before_seeing_data 不是 true")
    if weights.get("scheme") is None or weights.get("value") is None:
        findings.append("weights 未寫出具體方案與數值")
    results.append(
        _bad("CP-05", req, findings)
        if findings
        else _ok(
            "CP-05", req,
            f"s_f = calibration-only pooled IQR（凍結）；權重 {weights.get('scheme')} = {weights.get('value')}",
        )
    )

    # -- CP-06 每階段必須寫出可辨識性論證 ---------------------------------
    req = "每個階段必須寫明 observables 與「哪些 observable 識別哪些參數」"
    findings = []
    for stage in protocol.get("stagewise", []):
        sid = stage.get("id")
        if not stage.get("observables"):
            findings.append(f"stage {sid} 未列出 observables")
        identifiability = str(stage.get("identifiability", "")).strip()
        if len(identifiability) < 80:
            findings.append(f"stage {sid} 的 identifiability 論證缺漏或過於簡略")
        if not stage.get("parameters"):
            findings.append(f"stage {sid} 沒有參數")
    if len(protocol.get("stagewise", [])) < 5:
        findings.append("階段數少於 5；分組過粗會讓聯合擬合重新變成不可辨識")
    if protocol.get("stage_0", {}).get("reads_calibration_partition") is not False:
        findings.append("stage_0 必須宣告 reads_calibration_partition: false")
    results.append(
        _bad("CP-06", req, findings)
        if findings
        else _ok(
            "CP-06", req,
            f"{len(protocol.get('stagewise', []))} 個階段皆有 observables 與可辨識性論證，"
            "另有 stage 0 可辨識性探測",
        )
    )

    # -- CP-07 optimizer / seed / 初始化 / 收斂 / 重啟 / 平手 / 失敗 -------
    req = "optimizer、seed、初始化、收斂、評估上限、重啟、平手與失敗處置必須齊備"
    optimizer = protocol.get("optimizer", {})
    findings = []
    multivariate = optimizer.get("multivariate_stages", {})
    for key in ("method", "seed", "initialization", "maxiter"):
        if not multivariate.get(key):
            findings.append(f"optimizer.multivariate_stages.{key} 缺漏")
    # AMD-003：只能有一條求解路徑。留著一份列「參數」的 scalar_stages，
    # 會讓「stage 1 的兩個參數要不要逐一純量搜尋」永遠沒有答案。
    if not optimizer.get("method_rule"):
        findings.append("optimizer.method_rule 缺漏：求解路徑必須唯一且寫明")
    if "scalar_stages" in optimizer:
        findings.append(
            "optimizer.scalar_stages 仍存在：它列的是參數而非階段，"
            "會讓每個階段用哪一種求解器變成沒有答案（AMD-003）"
        )
    if not optimizer.get("common_random_numbers", {}).get("seeds"):
        findings.append("optimizer.common_random_numbers.seeds 缺漏：不共用種子會讓 optimizer 追雜訊")
    if not optimizer.get("verification_seeds", {}).get("seeds"):
        findings.append("optimizer.verification_seeds 缺漏：無法檢出種子專屬過擬合")
    if not optimizer.get("seeds", {}).get("seed_derivation"):
        findings.append("optimizer.seeds.seed_derivation 缺漏：重啟種子必須由公式決定")
    if multivariate.get("options", {}).get("polish") is not False:
        findings.append("differential_evolution 的 polish 必須為 false（對雜訊目標取數值梯度無意義）")
    for section, keys in (
        ("convergence", ("criterion", "non_convergence")),
        ("restart", ("policy", "count", "rule")),
        ("tie_break", ("epsilon", "rule", "rationale")),
        ("failure_handling", ("non_finite_objective", "failed_evaluation_budget")),
    ):
        block = protocol.get(section, {})
        for key in keys:
            if not block.get(key):
                findings.append(f"{section}.{key} 缺漏")
    if protocol.get("convergence", {}).get("non_convergence", {}).get(
        "is_legal_terminal_state"
    ) is not True:
        findings.append("NOT_CONVERGED 必須是合法結局，否則會誘導出放寬門檻重跑")
    results.append(
        _bad("CP-07", req, findings)
        if findings
        else _ok(
            "CP-07", req,
            f"{multivariate['method']} seed={multivariate['seed']} "
            f"maxiter={multivariate['maxiter']}；單一求解路徑；"
            "CRN、驗證種子、重啟、平手與失敗處置齊備",
        )
    )

    # -- CP-08 資料來源綁定 frozen split，且不觸及 held-out ----------------
    req = "calibration 只能用 frozen calibration split；held-out 不得出現在來源"
    data = protocol.get("data", {})
    findings = []
    if lock_store.exists("real_split_policy"):
        split = lock_store.load("real_split_policy")
        if data.get("calibration_set_hash") != split.get("calibration_set_hash"):
            findings.append(
                f"calibration_set_hash 與 real_split_policy.lock 不符："
                f"預註冊 {data.get('calibration_set_hash')} vs "
                f"lock {split.get('calibration_set_hash')}"
            )
        if data.get("eligible_set_hash") != split.get("eligible_set_hash"):
            findings.append("eligible_set_hash 與 real_split_policy.lock 不符")
    else:
        findings.append("real_split_policy.lock 不存在，calibration 一律禁止（Appendix H1）")
    if data.get("raw_data_hash") != "COMPUTED_AT_CALIBRATION_START":
        findings.append(
            "raw_data_hash 必須留待執行當下計算；現在算它就得先讀 calibration partition"
        )
    if not data.get("forbidden_sources"):
        findings.append("data.forbidden_sources 缺漏")
    results.append(
        _bad("CP-08", req, findings)
        if findings
        else _ok(
            "CP-08", req,
            f"綁定 calibration_set_hash {str(data.get('calibration_set_hash'))[:16]}…；"
            "held-out 明列為禁止來源",
        )
    )

    # -- CP-09 held-out access count 必須為 0 ------------------------------
    req = "凍結預註冊時 held-out access count 必須為 0"
    registry_file = root / "data" / "splits" / "split_registry.json"
    findings = []
    access_count: Any = None
    if registry_file.exists():
        access_count = json.loads(registry_file.read_text(encoding="utf-8")).get(
            "heldout_access_count"
        )
        if access_count != 0:
            findings.append(f"split_registry.heldout_access_count = {access_count}")
    else:
        findings.append(f"找不到 {registry_file}，無法證明 held-out 未被開啟")
    if (root / "freeze" / "e1_outcome.lock.json").exists():
        findings.append("e1_outcome.lock 已存在：E1 已有結果，預註冊失去意義")
    results.append(
        _bad("CP-09", req, findings)
        if findings
        else _ok("CP-09", req, f"heldout_access_count = {access_count}；e1_outcome.lock 不存在")
    )

    # -- CP-10 artifact 記錄規格 -------------------------------------------
    req = "必須事前訂好每次評估、每階段與最終要留下的 artifact"
    artifacts = protocol.get("artifacts", {})
    findings = []
    for section in ("per_evaluation", "per_stage", "final"):
        if not artifacts.get(section):
            findings.append(f"artifacts.{section} 缺漏")
    per_evaluation_fields = set(artifacts.get("per_evaluation", {}).get("fields") or [])
    for needed in ("parameters", "objective_total", "objective_terms", "seeds_used"):
        if needed not in per_evaluation_fields:
            findings.append(f"artifacts.per_evaluation.fields 缺少 {needed}")
    if not artifacts.get("parameter_boundary_report"):
        findings.append("缺少貼邊參數的回報規則：貼邊代表登記範圍訂錯，不得默默接受")
    results.append(
        _bad("CP-10", req, findings)
        if findings
        else _ok("CP-10", req, "逐評估／逐階段／最終三層 artifact 規格齊備")
    )

    # -- CP-11 目標函數的副作用監看 ---------------------------------------
    req = "每階段必須記錄未被最佳化的項，並訂有回歸容忍值"
    reporting = protocol.get("reporting", {})
    findings = []
    if not reporting.get("monitored_terms"):
        findings.append("reporting.monitored_terms 缺漏：只看被最佳化的項會漏掉副作用")
    guard = reporting.get("regression_guard", {})
    if guard.get("regression_tolerance") is None:
        findings.append("reporting.regression_guard.regression_tolerance 缺漏")
    results.append(
        _bad("CP-11", req, findings)
        if findings
        else _ok(
            "CP-11", req,
            f"全 16 項監看；回歸容忍值 {guard.get('regression_tolerance')}",
        )
    )

    # -- CP-12 預註冊必須早於任何 calibration artifact ---------------------
    req = "凍結預註冊時不得已存在 calibration artifact"
    from pcmef.audit.firewall import CALIBRATION_ARTIFACT_GLOBS

    outputs = root / "outputs"
    existing: list[str] = []
    for pattern in CALIBRATION_ARTIFACT_GLOBS:
        existing.extend(str(p) for p in sorted(outputs.glob(pattern)))
    results.append(
        _bad(
            "CP-12", req,
            [f"已存在 calibration artifact：{existing}"],
        )
        if existing
        else _ok("CP-12", req, "尚無 calibration artifact，預註冊確實在擬合之前")
    )

    # -- CP-13 評估預算必須與公式導出的值逐項相符（AMD-003）---------------
    req = "evaluation_budget.resolved 必須與 calibration_plan 的公式逐項相符"
    findings = []
    detail = ""
    try:
        computed = stage_budgets(protocol, registry)
        declared = protocol.get("evaluation_budget", {}).get("resolved") or {}
        if not declared:
            findings.append("evaluation_budget.resolved 缺漏：預算未被具體寫出")
        for stage_id, values in computed.items():
            entry = declared.get(stage_id)
            if entry is None:
                findings.append(f"evaluation_budget.resolved 缺少階段 {stage_id}")
                continue
            for key in ("dimensions", "population", "per_restart", "per_stage"):
                expected = values[
                    {"per_restart": "evaluations_per_restart",
                     "per_stage": "evaluations_per_stage"}.get(key, key)
                ]
                if int(entry.get(key, -1)) != int(expected):
                    findings.append(
                        f"{stage_id}.{key}：預註冊寫 {entry.get(key)!r}，"
                        f"公式導出 {expected}"
                    )
        total_declared = protocol.get("evaluation_budget", {}).get("total_evaluations")
        total_computed = sum(v["evaluations_per_stage"] for v in computed.values())
        if int(total_declared or -1) != total_computed:
            findings.append(
                f"total_evaluations：預註冊寫 {total_declared!r}，公式導出 {total_computed}"
            )
        detail = (
            f"{len(computed)} 個階段預算與公式相符；合計 {total_computed} 次評估"
        )
    except CalibrationPlanError as exc:
        findings.append(str(exc))
    results.append(
        _bad("CP-13", req, findings) if findings else _ok("CP-13", req, detail)
    )

    # -- CP-14 數值界線必須可由凍結物唯一重建（AMD-003）-------------------
    req = "optimizer 的數值界線必須由 frozen lock + frozen protocol 唯一重建"
    findings = []
    detail = ""
    if lock_payload:
        try:
            resolved_bounds = resolve_numeric_bounds(protocol, lock_payload, registry)
            digest = bounds_resolution_hash(protocol, lock_payload, registry)
            if bounds_resolution_hash(protocol, lock_payload, registry) != digest:
                findings.append("bounds_resolution_hash 不是決定性的")
            if not protocol.get("bounds", {}).get("uniqueness", {}).get("resolver"):
                findings.append("bounds.uniqueness.resolver 未指名唯一的解析路徑")
            detail = f"{len(resolved_bounds)} 個維度；bounds_resolution_hash {digest[:16]}…"
        except CalibrationPlanError as exc:
            findings.append(str(exc))
    else:
        findings.append("initial_simulation.lock 不存在，界線無從重建")
    results.append(
        _bad("CP-14", req, findings) if findings else _ok("CP-14", req, detail)
    )

    # -- CP-15 stage 0 必須是真正的 simulation-only（AMD-003）-------------
    req = "stage 0 的入場判準不得依賴任何由真實資料導出的量"
    stage0 = protocol.get("stage_0", {})
    findings = []
    normaliser = stage0.get("normaliser", {})
    if normaliser.get("symbol") != "sigma_MC":
        findings.append(
            "stage_0.normaliser.symbol 不是 sigma_MC；以 s_f 為分母會讓一個"
            "宣稱 simulation-only 的階段必須先讀 calibration partition"
        )
    if not normaliser.get("replicates") or not normaliser.get("seed_set"):
        findings.append("stage_0.normaliser 未寫出重複次數與種子集合")
    if stage0.get("reads_calibration_partition") is not False:
        findings.append("stage_0.reads_calibration_partition 必須為 false")
    if stage0.get("calibration_first_access") is not False:
        findings.append("stage_0.calibration_first_access 必須為 false")
    if stage0.get("collinearity_check", {}).get("normalised_by") != "sigma_MC":
        findings.append("collinearity_check 未以 sigma_MC 正規化：未正規化的餘弦隨單位改變")
    # 只掃**操作性**欄位，不掃說明性欄位。`normaliser.why_not_s_f` 與
    # `threshold_rationale` 的工作就是解釋為什麼不用 s_f，把它們算成違規，
    # 等於逼人刪掉理由才能過關 —— 與 CP-04 的守衛自我指涉是同一類錯誤。
    operative = {
        "method": stage0.get("method"),
        "admission_threshold.rule": stage0.get("admission_threshold", {}).get("rule"),
        "collinearity_check.rule": stage0.get("collinearity_check", {}).get("rule"),
        "normaliser.definition": normaliser.get("definition"),
    }
    for location, text in operative.items():
        if text and "s_f" in str(text):
            findings.append(
                f"stage_0.{location} 仍以 s_f 表述；stage 0 的操作性欄位不得"
                "依賴任何由真實資料導出的量"
            )
    diagnostic = protocol.get("s_f_relative_leverage_diagnostic", {})
    if diagnostic.get("gating") is not False:
        findings.append(
            "s_f_relative_leverage_diagnostic.gating 必須為 false："
            "事後依真實資料剔除參數就是資料相依的模型選擇"
        )
    results.append(
        _bad("CP-15", req, findings)
        if findings
        else _ok(
            "CP-15", req,
            f"分母為 sigma_MC（{normaliser.get('replicates')} 組種子）；"
            "s_f 相對槓桿降為不具決定權的診斷",
        )
    )

    # -- CP-16 calibration partition 尚未被讀取（AMD-003）-----------------
    req = "凍結時 calibration partition access count 必須為 0"
    ledger_path = root / "data" / "splits" / "calibration_access_ledger.json"
    findings = []
    calibration_access: Any = None
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        calibration_access = ledger.get("calibration_access_count")
        if calibration_access != 0:
            findings.append(
                f"calibration_access_count = {calibration_access}；"
                "預註冊必須早於第一次讀取"
            )
        if ledger.get("entries"):
            findings.append(f"access ledger 已有 {len(ledger['entries'])} 筆記錄")
    else:
        findings.append(f"找不到 {ledger_path}，無法證明 calibration partition 未被讀取")
    if data.get("first_access", {}).get("ledger") != "data/splits/calibration_access_ledger.json":
        findings.append("data.first_access.ledger 未指向 access ledger")
    results.append(
        _bad("CP-16", req, findings)
        if findings
        else _ok("CP-16", req, f"calibration_access_count = {calibration_access}；帳上無記錄")
    )

    return AuditReport(name="calibration_preregistration", results=tuple(results))


def _amendment_hashes(protocol: dict[str, Any], freeze_dir: str | Path) -> dict[str, str]:
    """取回協定所宣告的每一份 amendment 的 payload_hash。

    未凍結即拋錯，不回填 None —— 一份宣稱「依 AMD-003 修訂」但 AMD-003
    根本不存在的預註冊，等於沒有修訂記錄。
    """
    from pcmef.core.amendments import AmendmentStore

    store = AmendmentStore(freeze_dir)
    out: dict[str, str] = {}
    for entry in protocol.get("amendments", []) or []:
        amendment_id = str(entry["id"])
        if not store.exists(amendment_id):
            raise PreregistrationError(
                f"the protocol declares amendment {amendment_id!r} but it is not "
                f"frozen under {Path(freeze_dir) / 'amendments'}. A protocol that "
                "cites an amendment which does not exist has no amendment record."
            )
        document = json.loads(
            store.path_for(amendment_id).read_text(encoding="utf-8")
        )
        store.load(amendment_id)  # 重算雜湊，被改過即拋錯
        out[amendment_id] = str(document["payload_hash"])
    return out


def _scan_for_real_means(root: Path, protocol_path: str | Path | None) -> list[str]:
    """掃 pcmef/ 的 .py 與預註冊檔本身，找四類 real class mean 的字面值。"""
    findings: list[str] = []
    pattern = re.compile("|".join(re.escape(v) for v in FORBIDDEN_REAL_MEAN_LITERALS))
    targets = list((root / "pcmef").rglob("*.py"))
    protocol_file = Path(protocol_path) if protocol_path else PREREGISTRATION_PATH
    if protocol_file.exists():
        targets.append(protocol_file)
    for path in targets:
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if GUARD_DEFINITION_MARKER in line:
                continue
            if pattern.search(line):
                findings.append(
                    f"{path.relative_to(root)}:{number} 出現 real class mean 字面值：{line.strip()[:80]}"
                )
    return findings


# ---------------------------------------------------------------------------
# 凍結
# ---------------------------------------------------------------------------


def freeze_preregistration(
    protocol_path: str | Path | None = None,
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    code_version: str = "",
    code_dirty: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """把預註冊連同它依賴的所有雜湊凍成不可覆寫的記錄。

    保存的內容依交接裁決：protocol/hash、code commit、split hash、
    registry hash、initial lock hash、ERR-001 hash、optimizer/config/seeds。
    """
    from pcmef.core.errata import ErratumStore
    from pcmef.core.formal_loader import load_formal_lock
    from pcmef.core.locks import LockStore
    from pcmef.core.parameters import ParameterRegistry

    root = Path(repo_root)
    target_dir = Path(freeze_dir) / "preregistrations"
    target = target_dir / f"{PREREGISTRATION_ID}.prereg.json"
    if target.exists():
        raise PreregistrationError(
            f"{PREREGISTRATION_ID} is already frozen at {target}; preregistrations "
            "are append-only. Changing the protocol requires an amendment and a new id."
        )

    report = validate_preregistration(protocol_path, freeze_dir, root)
    unmet = report.unmet(REQUIRED_CHECKS)
    if unmet:
        raise PreregistrationError(
            "preregistration is not freezable; unmet checks: "
            + ", ".join(f"{c.identifier}({c.status.value})" for c in unmet)
        )

    protocol = load_protocol(protocol_path)
    registry = ParameterRegistry.load()
    lock_store = LockStore(freeze_dir)
    resolved = load_formal_lock("initial_simulation", freeze_dir, root)
    erratum_store = ErratumStore(freeze_dir)

    optimizer = protocol["optimizer"]
    payload = {
        "preregistration_id": PREREGISTRATION_ID,
        "preregistration_version": protocol["preregistration_version"],
        "protocol_path": "configs/calibration_preregistration.yaml",
        "protocol_hash": hash_object(protocol),
        "supersedes": protocol.get("supersedes"),
        "amendments": _amendment_hashes(protocol, freeze_dir),
        "code_version": code_version,
        "code_dirty_at_freeze": bool(code_dirty),
        "split": {
            "lock": "real_split_policy",
            "lock_hash": lock_store.load_hash("real_split_policy"),
            "calibration_set_hash": protocol["data"]["calibration_set_hash"],
            "eligible_set_hash": protocol["data"]["eligible_set_hash"],
            "heldout_opened": False,
            "heldout_access_count": 0,
            "calibration_access_count": 0,
            "calibration_access_ledger": protocol["data"]["first_access"]["ledger"],
        },
        "registry": {
            "version": registry.registry_version,
            "parameter_set_hash": registry.parameter_set_hash(),
            "calibration_only_count": registry.counts()["kind.calibration_only"],
        },
        "initial_simulation": {
            "lock_hash": resolved.payload_hash,
            "errata": resolved.provenance()["errata"],
        },
        "errata": {
            erratum_id: erratum_store.load_hash(erratum_id)
            for erratum_id in erratum_store.list_ids()
        },
        "optimizer": {
            "method_rule": optimizer["method_rule"],
            "multivariate": optimizer["multivariate_stages"],
            "optimizer_seed": optimizer["seeds"]["optimizer_seed"],
            "seed_derivation": optimizer["seeds"]["seed_derivation"],
            "common_random_number_seeds": optimizer["common_random_numbers"]["seeds"],
            "verification_seeds": optimizer["verification_seeds"]["seeds"],
            "restart": protocol["restart"],
            "convergence": protocol["convergence"],
            "tie_break": protocol["tie_break"],
            "failure_handling": protocol["failure_handling"],
        },
        # AMD-003：預算與界線由公式/解析器導出並在此凍結，
        # 讓「optimizer 用了哪組界線、多少預算」不必信任任何人的記憶。
        "evaluation_budget": stage_budgets(protocol, registry),
        "bounds_resolution_hash": bounds_resolution_hash(
            protocol, resolved.payload, registry
        ),
        "resolved_bounds": {
            name: list(bound)
            for name, bound in resolve_numeric_bounds(
                protocol, resolved.payload, registry
            ).items()
        },
        "stage_0": {
            "normaliser": protocol["stage_0"]["normaliser"]["symbol"],
            "replicates": protocol["stage_0"]["normaliser"]["replicates"],
            "seed_set": protocol["stage_0"]["normaliser"]["seed_set"],
            "threshold": protocol["stage_0"]["admission_threshold"]["threshold_value"],
            "borderline_band": protocol["stage_0"]["admission_threshold"][
                "borderline_band"
            ],
            "reads_calibration_partition": False,
        },
        "objective": {
            "id": protocol["objective"]["id"],
            "form": protocol["objective"]["form"],
            "statistic": protocol["objective"]["discrepancy"]["statistic"],
            "level": protocol["objective"]["discrepancy"]["level"],
            "normalization": protocol["normalization"]["scale"],
            "weights": protocol["weights"],
        },
        "stagewise": [
            {
                "stage": stage["stage"],
                "id": stage["id"],
                "parameters": stage["parameters"],
                "observables": stage["observables"],
            }
            for stage in protocol["stagewise"]
        ],
        "not_fitted": _not_fitted_names(protocol),
        "readiness": {c.identifier: c.status.value for c in report.results},
        "claim_boundary": (
            "This record freezes the calibration PROTOCOL only. No calibration has "
            "been run, no calibration partition value has been read, and held-out "
            "remains sealed (access_count = 0). Freezing it does not authorise "
            "freezing calibrated_simulation.lock, which requires its own audit."
        ),
    }

    document = {
        "preregistration_id": PREREGISTRATION_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload_hash": hash_object(payload),
        "payload": payload,
        "version": 1,
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target, document
