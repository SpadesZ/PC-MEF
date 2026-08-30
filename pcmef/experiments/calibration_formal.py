# PC-MEF Research System source maintenance contract
# 上下游: 讀 calibration_identity 驗過的凍結身分、calibration_objective 的
#         目標函數與 calibration_journal 的檢查點；由 cli 的
#         `calibration formal` 在子行程呼叫（NOTE-012）；產出
#         outputs/calibration/ 底下的 stage_summary / evaluations.jsonl /
#         calibration_report.json 等 artifact。**不凍結**
#         calibrated_simulation.lock，那是獨立且需先過稽核的動作。
# 檔案路徑: pcmef/experiments/calibration_formal.py
# 產生時間: 2026-08-30 23:20 +08:00
# 版本: v0.2.0
# 功能說明: 依 CAL-PREREG-003（經 AMD-005 修訂）逐階段執行 differential
#           evolution，把七個 admitted 參數擬合到 16 項 NW 目標上；階段完成即
#           凍結其參數，並在每個階段邊界檢查副作用退步、種子過擬合與預算。
#           守衛破線時抑制該次更新並保留原值，而不是中止整條流程。
# 模組定位: calibration 的執行層。判準全部來自凍結物 —— 預算、收斂、平手、
#           重啟、退步容忍度都不是本檔的參數；本檔照著跑，跑完不得回頭改判準。
# 主要責任:
#   1. run_stage() 以三次重啟執行 DE，並以日誌重播支援中斷接續
#   2. select_candidate() 依凍結的 tie_break 規則挑出該階段的結論
#   3. regression guard 判定未被最佳化的項是否退步超過容忍度
#   4. verification seeds 以另一組種子確認改善不是種子專屬
#   5. AMD-005 抑制控制：任一守衛破線即拒絕更新、保留原值、繼續下一階段
#   6. run_formal_calibration() 串起五個階段並寫出全部 artifact
# 維護提醒:
#   - 不得為了讓執行變快而縮減預算、重啟次數或 maxiter。預算是上限不是目標，
#     縮它等於換一個比較容易收斂的實驗。
#   - 不得在預算用盡時取 best-so-far 並宣稱收斂；那是 NOT_CONVERGED，
#     且 calibrated_simulation 不得凍結。
#   - 不得因為 UPDATE_INHIBITED 就放寬 verification_tolerance 或
#     regression_tolerance。AMD-005 只改「破線之後怎麼辦」，判準一個位元都沒動；
#     調鬆容忍度會讓抑制永遠不觸發，那等於把守衛拆了。
#   - 不得把 UPDATE_INHIBITED 說成證明了任何生物神經抑制機制。它是功能性啟發的
#     工程控制規則，claim boundary 隨每一份 artifact 一起寫出。
#   - 不得讓後面的階段移動前面階段已凍結的參數；階段隔離是逐階段可辨識性
#     論證成立的前提。
#   - 不得開啟 FORMAL_E1_FINAL，也不得在本檔組出 heldout 的路徑。
#   - v0.2.0 變更：AMD-005 抑制控制取代 SEED_OVERFIT / SIDE_EFFECT_REGRESSION
#     的中止語意；兩者改為 flags，終止狀態只剩 NOT_CONVERGED 與
#     UNSTABLE_LANDSCAPE。
#   - v0.1.0 新增：首版 formal calibration 執行器（CAL-PREREG-003 stage 1-5）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_resume.py -v
#   - py -3.10 -m pcmef.cli calibration formal --smoke
# ------------------------------------------------------------

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from pcmef.core.hash import hash_object
from pcmef.experiments.calibration_identity import (
    STAGE_ORDER,
    FrozenIdentity,
    IdentityError,
    all_cells,
    load_frozen_identity,
    stage_bounds,
    stage_declared_cells,
    stage_dimensions,
    stage_held_parameters,
)
from pcmef.experiments.calibration_journal import (
    Checkpoint,
    EvaluationJournal,
    EvaluationRecord,
    ResumeError,
    vector_fingerprint,
)
from pcmef.experiments.calibration_objective import (
    CRN_SEEDS,
    SCENE_CONSTANT_BY_DIMENSION,
    SURROGATE_DIMENSIONS,
    VERIFICATION_SEEDS,
    RealCalibration,
    SimulationFailure,
    Simulator,
    load_real_calibration,
    objective_terms,
    sum_cells,
)
from pcmef.experiments.calibration_plan import BudgetExceeded, EvaluationBudget

__all__ = [
    "RUNNER_VERSION",
    "FormalCalibrationError",
    "StageOutcome",
    "run_formal_calibration",
]

RUNNER_VERSION = "v0.2.0"

#: 這些結局一律中止流程。它們說的是「optimizer 根本沒把事情做完」，
#: 因此在這個結果上繼續往下跑，後面每一個階段的前提都不成立。
#:
#: AMD-005 把 SEED_OVERFIT 與 SIDE_EFFECT_REGRESSION 移出這個集合：
#: 它們說的是「找到的答案應該被拒絕」，那是另一件事，處置為
#: UPDATE_INHIBITED（拒絕更新、保留原值、繼續下一階段）。
#: 抑制一次更新的前提是真的有一個成形的更新可以抑制 —— 搜尋沒收斂、
#: 或五分之一的地形算不出來時，並沒有。
_TERMINAL_OUTCOMES = frozenset({"NOT_CONVERGED", "UNSTABLE_LANDSCAPE"})


class FormalCalibrationError(RuntimeError):
    """具名的中止理由。reason 會原樣進 artifact。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


@dataclass
class StageOutcome:
    stage_id: str
    summary: dict[str, Any]
    selected: dict[str, float]

    @property
    def outcome(self) -> str:
        return str(self.summary["outcome"])


# ---------------------------------------------------------------------------
# 目標函數的一次呼叫
# ---------------------------------------------------------------------------


@dataclass
class _Evaluator:
    """把「一個參數向量」變成「一次完整記錄的評估」。

    它同時負責重播：日誌裡還有未交還的記錄時，先比對指紋再原樣交還，
    不重跑模擬。重播與真跑走同一個計數與同一份日誌順序，
    因此接續後的軌跡與未中斷的執行逐位元相同。
    """

    identity: FrozenIdentity
    simulator: Simulator
    real: RealCalibration
    stage_id: str
    dimensions: list[str]
    declared_cells: list[tuple[str, str]]
    frozen_parameters: dict[str, float]
    journal: EvaluationJournal
    budget: EvaluationBudget
    bounds: dict[str, tuple[float, float]]
    restart_index: int = 0
    seed_mode: str = "CRN"
    replay_hits: int = 0
    genuine: int = 0
    evaluation_cache: dict[str, EvaluationRecord] = field(default_factory=dict)
    cache_hits: int = 0

    @property
    def seeds(self) -> dict[str, int]:
        return dict(CRN_SEEDS if self.seed_mode == "CRN" else VERIFICATION_SEEDS)

    def _split_parameters(self, values: dict[str, float]):
        """把全部生效參數拆成「進場景的」與「作用在算圖之後的」。"""
        scene: dict[str, float] = {}
        surrogate: dict[str, float] = {}
        for name, value in values.items():
            if name in SURROGATE_DIMENSIONS:
                surrogate[name] = float(value)
            elif name in SCENE_CONSTANT_BY_DIMENSION:
                scene[name] = float(value)
            else:
                raise FormalCalibrationError(
                    "FROZEN_PROTOCOL_DRIFT",
                    f"{name!r} is neither a scene constant nor a surrogate scale; "
                    "an unclassified parameter would be silently dropped",
                )
        return scene, surrogate

    def run_identity_hash(self, values: dict[str, float]) -> str:
        """這一次模擬的完整科學身分。"""
        return hash_object(
            {
                "stage_id": self.stage_id,
                "seed_mode": self.seed_mode,
                "seeds": self.seeds,
                "parameters": {k: float(v).hex() for k, v in sorted(values.items())},
                "spp": self.simulator.spp,
                "resolution": list(self.simulator.resolution),
                "temporal_bins": self.simulator.temporal_bins,
                "n_samples": self.simulator.n_samples,
                "identity": self.identity.identity_hash(),
                "raw_calibration_data_hash": self.real.raw_hash,
            }
        )

    def evaluate(self, vector: Sequence[float]) -> float:
        """DE 看到的目標值：**該階段宣告格子**的 NW 等權總和。"""
        fingerprint = vector_fingerprint(vector)
        replayed = self.journal.replay(self.restart_index, fingerprint)
        if replayed is not None:
            self.replay_hits += 1
            self.budget.spend()
            if replayed.status != "OK":
                self.budget.failed += 1
            return replayed.stage_objective

        self.budget.spend()
        values = dict(self.frozen_parameters)
        values.update(
            {name: float(v) for name, v in zip(self.dimensions, vector)}
        )

        started = time.perf_counter()
        status, error = "OK", None
        nw: dict[str, float] = {}
        raw: dict[str, float] = {}
        stage_objective = math.inf
        total = math.inf

        out_of_bounds = [
            name
            for name, value in zip(self.dimensions, vector)
            if not (self.bounds[name][0] <= float(value) <= self.bounds[name][1])
        ]
        identity_hash = self.run_identity_hash(values)
        cached = self.evaluation_cache.get(identity_hash)

        if out_of_bounds:
            # clip 會讓最佳點落在邊界上而看不出它其實想往外走。
            status, error = "OUT_OF_BOUNDS", f"outside frozen bounds: {out_of_bounds}"
        elif cached is not None:
            # 決定性目標函數下，同一個科學身分必然給出同一個結果，
            # 因此重用是數學等價而不是近似。
            self.cache_hits += 1
            nw, raw = dict(cached.objective_terms), dict(cached.w1_raw_terms)
            stage_objective = cached.stage_objective
            total = cached.objective_total
            status, error = cached.status, cached.error
        else:
            try:
                scene, surrogate = self._split_parameters(values)
                simulated = self.simulator.recordings(scene, surrogate, self.seeds)
                nw, raw = objective_terms(simulated, self.real, self.identity.s_f)
                stage_objective = sum_cells(nw, self.declared_cells)
                total = sum_cells(nw, all_cells())
                if not math.isfinite(stage_objective) or not math.isfinite(total):
                    status, error = "NON_FINITE", "objective is not finite"
            except SimulationFailure as failure:
                status, error = "NON_FINITE", f"SimulationFailure: {failure}"
            except Exception as failure:  # noqa: BLE001
                status, error = "CRASHED", f"{type(failure).__name__}: {failure}"

        if status != "OK":
            stage_objective, total = math.inf, math.inf
            self.budget.failed += 1

        record = EvaluationRecord(
            evaluation_index=self.budget.used,
            restart_index=self.restart_index,
            parameters={name: float(v) for name, v in zip(self.dimensions, vector)},
            parameter_fingerprint=fingerprint,
            objective_total=total,
            stage_objective=stage_objective,
            objective_terms=nw,
            w1_raw_terms=raw,
            simulation_run_identity_hash=identity_hash,
            seeds_used=self.seeds,
            seed_mode=self.seed_mode,
            status=status,
            runtime_s=time.perf_counter() - started,
            error=error,
        )
        self.journal.append(record)
        if status == "OK" and identity_hash not in self.evaluation_cache:
            self.evaluation_cache[identity_hash] = record
        self.genuine += 1
        return stage_objective

    def evaluate_point(self, values: dict[str, float]) -> EvaluationRecord:
        """在**不經 optimizer** 的情況下算一個具名點（起點、驗證種子）。

        它不進日誌的重播序列，也不花該階段的搜尋預算：它不是搜尋的一步。
        """
        started = time.perf_counter()
        status, error = "OK", None
        nw: dict[str, float] = {}
        raw: dict[str, float] = {}
        stage_objective = math.inf
        total = math.inf
        try:
            scene, surrogate = self._split_parameters(values)
            simulated = self.simulator.recordings(scene, surrogate, self.seeds)
            nw, raw = objective_terms(simulated, self.real, self.identity.s_f)
            stage_objective = sum_cells(nw, self.declared_cells)
            total = sum_cells(nw, all_cells())
            if not math.isfinite(stage_objective) or not math.isfinite(total):
                status, error = "NON_FINITE", "objective is not finite"
        except SimulationFailure as failure:
            status, error = "NON_FINITE", f"SimulationFailure: {failure}"
        except Exception as failure:  # noqa: BLE001
            status, error = "CRASHED", f"{type(failure).__name__}: {failure}"
        if status != "OK":
            stage_objective, total = math.inf, math.inf
        return EvaluationRecord(
            evaluation_index=-1,
            restart_index=self.restart_index,
            parameters={k: float(v) for k, v in sorted(values.items())},
            parameter_fingerprint=vector_fingerprint(
                [values[name] for name in self.dimensions]
            ),
            objective_total=total,
            stage_objective=stage_objective,
            objective_terms=nw,
            w1_raw_terms=raw,
            simulation_run_identity_hash=self.run_identity_hash(values),
            seeds_used=self.seeds,
            seed_mode=self.seed_mode,
            status=status,
            runtime_s=time.perf_counter() - started,
            error=error,
        )


# ---------------------------------------------------------------------------
# 平手規則
# ---------------------------------------------------------------------------


def _normalised(vector: Sequence[float], bounds: list[tuple[float, float]]) -> np.ndarray:
    return np.array(
        [(float(v) - lo) / (hi - lo) for v, (lo, hi) in zip(vector, bounds)]
    )


def select_candidate(
    candidates: list[dict[str, Any]],
    initial_vector: Sequence[float],
    bounds: list[tuple[float, float]],
    epsilon: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """CAL-PREREG-003 tie_break：分不出來的時候取最小位移。

    「分不出來」的判準是目標值的**相對**差小於 epsilon；位移在正規化邊界
    空間量，因為不同維度的單位不可比。任何其他平手規則（取較大值、取中點、
    取讓某一類好看的）都是在無資訊處注入選擇。
    """
    finite = [c for c in candidates if math.isfinite(c["objective"])]
    if not finite:
        raise FormalCalibrationError(
            "NON_FINITE_OBJECTIVE",
            "every candidate for this stage produced a non-finite objective",
        )
    best = min(c["objective"] for c in finite)
    scale = abs(best) if best != 0.0 else 1.0
    tied = [c for c in finite if (c["objective"] - best) / scale < epsilon]

    u_initial = _normalised(initial_vector, bounds)
    for candidate in tied:
        candidate["displacement_l2"] = float(
            np.linalg.norm(_normalised(candidate["vector"], bounds) - u_initial)
        )
    chosen = min(tied, key=lambda c: (c["displacement_l2"], c["objective"]))
    return chosen, {
        "best_objective": best,
        "epsilon": epsilon,
        "tied_candidates": [
            {
                "label": c["label"],
                "objective": c["objective"],
                "displacement_l2": c["displacement_l2"],
            }
            for c in tied
        ],
        "applied": len(tied) > 1,
        "rule": (
            "relative objective difference below epsilon -> smallest L2 displacement "
            "from the frozen initial value in normalised bounds space"
        ),
    }


# ---------------------------------------------------------------------------
# 一個階段
# ---------------------------------------------------------------------------


def run_stage(
    identity: FrozenIdentity,
    simulator: Simulator,
    real: RealCalibration,
    stage_id: str,
    frozen_parameters: dict[str, float],
    out_root: Path,
    checkpoint: Checkpoint,
    resume: bool,
    maxiter: int,
    restarts: int,
    popsize: int,
    progress: Callable[[str], None],
) -> StageOutcome:
    """執行單一階段並回傳其結論與被選中的參數。"""
    from scipy.optimize import differential_evolution

    from pcmef.experiments.calibration_plan import budget_per_stage

    protocol = identity.protocol
    optimizer = protocol["optimizer"]["multivariate_stages"]
    options = optimizer["options"]
    optimizer_seed = int(protocol["optimizer"]["seeds"]["optimizer_seed"])
    epsilon = float(protocol["tie_break"]["epsilon"])
    regression_tolerance = float(
        protocol["reporting"]["regression_guard"]["regression_tolerance"]
    )
    verification_tolerance = float(
        protocol["optimizer"]["verification_seeds"]["verification_tolerance"]
    )
    failure_threshold = float(
        protocol["failure_handling"]["failed_evaluation_budget"]["threshold"]
    )

    dimensions = stage_dimensions(identity, stage_id)
    declared_cells = stage_declared_cells(identity, stage_id)
    held = stage_held_parameters(identity, stage_id)
    bounds_map = stage_bounds(identity, dimensions)
    bounds = [bounds_map[name] for name in dimensions]
    initial_vector = [float(identity.initial_values[name]) for name in dimensions]

    stage_dir = out_root / f"stage_{stage_id}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    journal = EvaluationJournal.open(stage_dir / "evaluations.jsonl", resume=resume)

    n = len(dimensions)
    budget_limit = (
        budget_per_stage(n, popsize, maxiter, restarts) if n else 0
    )
    budget = EvaluationBudget(stage_id=stage_id, limit=budget_limit)
    evaluator = _Evaluator(
        identity=identity,
        simulator=simulator,
        real=real,
        stage_id=stage_id,
        dimensions=dimensions,
        declared_cells=declared_cells,
        frozen_parameters=dict(frozen_parameters),
        journal=journal,
        budget=budget,
        bounds=bounds_map,
    )

    monitored_cells = [c for c in all_cells() if c not in set(declared_cells)]
    started_at = time.perf_counter()

    # -- 起點：該階段開始時的目標值，一律以 CRN 種子量 -----------------------
    progress(f"[{stage_id}] measuring the stage starting point")
    start_values = dict(frozen_parameters)
    start_values.update({name: v for name, v in zip(dimensions, initial_vector)})
    start_record = evaluator.evaluate_point(start_values)
    if start_record.status != "OK":
        journal.close()
        raise FormalCalibrationError(
            "NON_FINITE_OBJECTIVE",
            f"stage {stage_id} cannot even evaluate its own starting point: "
            f"{start_record.error}",
        )
    objective_before = start_record.stage_objective
    monitored_before = sum_cells(start_record.objective_terms, monitored_cells)

    restart_results: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = [
        {
            "label": "frozen_initial",
            "vector": list(initial_vector),
            "objective": objective_before,
        }
    ]
    stage_flags: list[str] = []
    outcome = "CONVERGED"
    non_convergence: list[int] = []

    if n == 0:
        # AMD-004 C：0 維階段是具名結局，不得靜默跳過。
        progress(f"[{stage_id}] NO_FREE_PARAMETERS — optimizer not run")
        outcome = "NO_FREE_PARAMETERS"
    else:
        for restart_index in range(restarts):
            restart_seed = optimizer_seed + 1000 * restart_index
            evaluator.restart_index = restart_index
            journal.start_replay(restart_index)
            already = len(journal.completed_for(restart_index))
            progress(
                f"[{stage_id}] restart {restart_index} seed={restart_seed} "
                f"({already} evaluation(s) replayable from journal)"
            )
            checkpoint.set_current(
                stage_id,
                restart_index,
                {
                    "restart_seed": restart_seed,
                    "dimensions": dimensions,
                    "bounds": {k: list(v) for k, v in bounds_map.items()},
                    "frozen_parameters": dict(frozen_parameters),
                    "objective_before": objective_before,
                    "evaluations_replayable": already,
                    "budget_limit": budget_limit,
                    "seeds": dict(CRN_SEEDS),
                    "journal": str((stage_dir / "evaluations.jsonl").as_posix()),
                },
            )
            try:
                result = differential_evolution(
                    evaluator.evaluate,
                    bounds,
                    strategy=str(options["strategy"]),
                    maxiter=int(maxiter),
                    popsize=int(popsize),
                    tol=float(options["tol"]),
                    mutation=tuple(float(v) for v in options["mutation"]),
                    recombination=float(options["recombination"]),
                    seed=restart_seed,
                    polish=bool(options["polish"]),
                    init=str(options["init"]),
                    updating=str(options["updating"]),
                    workers=1,
                    x0=np.array(initial_vector, dtype=np.float64),
                )
            except BudgetExceeded as exceeded:
                progress(f"[{stage_id}] restart {restart_index}: {exceeded}")
                outcome = "NOT_CONVERGED"
                non_convergence.append(restart_index)
                restart_results.append(
                    {
                        "restart_index": restart_index,
                        "seed": restart_seed,
                        "converged": False,
                        "message": str(exceeded),
                        "best_objective": None,
                        "best_parameters": None,
                    }
                )
                break

            converged = bool(result.success)
            if not converged:
                non_convergence.append(restart_index)
            restart_results.append(
                {
                    "restart_index": restart_index,
                    "seed": restart_seed,
                    "converged": converged,
                    "message": str(result.message),
                    "iterations": int(getattr(result, "nit", -1)),
                    "scipy_nfev": int(getattr(result, "nfev", -1)),
                    "runner_evaluations": budget.used,
                    "best_objective": float(result.fun),
                    "best_parameters": {
                        name: float(v) for name, v in zip(dimensions, result.x)
                    },
                }
            )
            candidates.append(
                {
                    "label": f"restart_{restart_index}",
                    "vector": [float(v) for v in result.x],
                    "objective": float(result.fun),
                }
            )
            progress(
                f"[{stage_id}] restart {restart_index} -> J={result.fun:.9g} "
                f"converged={converged} evaluations={budget.used}/{budget_limit}"
            )

        # 失敗評估比例：五分之一都算不出來的地形上的最小值不是最小值。
        if budget.used and budget.failure_ratio() > failure_threshold:
            outcome = "UNSTABLE_LANDSCAPE"
        elif non_convergence:
            outcome = "NOT_CONVERGED"

    # -- 挑出結論 ----------------------------------------------------------
    if n == 0:
        chosen = {"label": "frozen_initial", "vector": [], "objective": objective_before}
        tie_break = {"applied": False, "rule": "zero-dimensional stage: nothing to break"}
    else:
        chosen, tie_break = select_candidate(candidates, initial_vector, bounds, epsilon)

    selected = {name: float(v) for name, v in zip(dimensions, chosen["vector"])}
    selected_values = dict(frozen_parameters)
    selected_values.update(selected)

    if outcome == "CONVERGED" and chosen["label"] == "frozen_initial":
        outcome = "NO_IMPROVEMENT_REQUIRED"

    # 三次重啟的相對全距：多個近似等價的解是可辨識性的警訊。
    finite_bests = [
        r["best_objective"] for r in restart_results if r["best_objective"] is not None
    ]
    multimodal = False
    if len(finite_bests) >= 2:
        lo, hi = min(finite_bests), max(finite_bests)
        spread = (hi - lo) / abs(lo) if lo else math.inf
        multimodal = bool(spread > 0.05)
        if multimodal:
            stage_flags.append("MULTIMODAL")

    # -- 候選點的完整 16 項（CRN 種子） -------------------------------------
    progress(f"[{stage_id}] measuring the proposed candidate")
    evaluator.seed_mode = "CRN"
    candidate_record = evaluator.evaluate_point(selected_values)
    if candidate_record.status != "OK":
        journal.close()
        raise FormalCalibrationError(
            "NON_FINITE_OBJECTIVE",
            f"stage {stage_id} selected a point that does not evaluate: "
            f"{candidate_record.error}",
        )
    candidate_objective = candidate_record.stage_objective
    monitored_candidate = sum_cells(candidate_record.objective_terms, monitored_cells)

    # -- regression guard（量在候選點上） -----------------------------------
    if monitored_before > 0:
        monitored_relative = (
            monitored_candidate - monitored_before
        ) / abs(monitored_before)
    else:
        monitored_relative = 0.0 if monitored_candidate == monitored_before else math.inf
    regression = {
        "monitored_cells": [f"{c}|{f}" for c, f in monitored_cells],
        "monitored_nw_sum_before": monitored_before,
        "monitored_nw_sum_at_candidate": monitored_candidate,
        "relative_change": monitored_relative,
        "tolerance": regression_tolerance,
        "violated": bool(monitored_relative > regression_tolerance),
    }

    # -- verification seeds（每階段只用一次，量在候選點上） -------------------
    evaluator.seed_mode = "VERIFICATION"
    verification_record = evaluator.evaluate_point(selected_values)
    evaluator.seed_mode = "CRN"
    if n == 0:
        seed_check = {
            "status": "NOT_APPLICABLE_ZERO_DIMENSIONAL",
            "violated": False,
            "reason": (
                "the optimizer did not run, so there is no seed-specific improvement "
                "that could be overfitted"
            ),
        }
    elif verification_record.status != "OK":
        seed_check = {"status": "VERIFICATION_EVALUATION_FAILED",
                      "violated": True,
                      "error": verification_record.error}
    else:
        scale = abs(candidate_objective) if candidate_objective else 1.0
        degradation = (
            verification_record.stage_objective - candidate_objective
        ) / scale
        seed_check = {
            "status": "OK",
            "relative_degradation": degradation,
            "tolerance": verification_tolerance,
            "violated": bool(degradation > verification_tolerance),
        }
    seed_check.update(
        {
            "seeds": dict(VERIFICATION_SEEDS),
            "stage_objective": verification_record.stage_objective,
            "objective_total": verification_record.objective_total,
            "objective_terms": verification_record.objective_terms,
            "used_once_per_stage": True,
        }
    )

    # -- AMD-005 抑制控制 ---------------------------------------------------
    # 兩道守衛任一破線，就**拒絕這次更新並保留原值**，而不是中止整條流程。
    # 一個被獨立證據否決的提案，說的是「這個參數不該動」，不是「後面的階段
    # 也不成立」。保留凍結初值本身是一個站得住的狀態 —— 它正是 initial
    # freeze 當初凍下來的那個模型。
    #
    # UPDATE_INHIBITED 是**功能性啟發**的命名：獨立證據指出更新會傷害系統層級
    # 穩定度時，抑制該次狀態變更。它是工程控制規則，**不主張任何生物神經
    # 抑制機制**（AMD-005 changed_contracts.stage_guard_terminal_state）。
    violated_guards = [
        name
        for name, violated in (
            ("SIDE_EFFECT_REGRESSION", regression["violated"]),
            ("SEED_OVERFIT", bool(seed_check.get("violated"))),
        )
        if violated
    ]
    inhibited = bool(violated_guards) and outcome not in _TERMINAL_OUTCOMES
    rejected_candidate = dict(selected)

    if inhibited:
        # 保留前一個凍結值（本階段維度即凍結初值），並把終點量測換回起點。
        selected = {name: float(identity.initial_values[name]) for name in dimensions}
        selected_values = dict(frozen_parameters)
        selected_values.update(selected)
        after_record = start_record
        outcome = "UPDATE_INHIBITED"
        stage_flags.extend(violated_guards)
        stage_flags.append("UPDATE_INHIBITED")
        progress(
            f"[{stage_id}] UPDATE_INHIBITED by {violated_guards}; "
            f"rejecting {rejected_candidate} and retaining "
            f"{ {k: float(identity.initial_values[k]) for k in dimensions} }"
        )
    else:
        after_record = candidate_record
        stage_flags.extend(violated_guards)

    objective_after = after_record.stage_objective
    monitored_after = sum_cells(after_record.objective_terms, monitored_cells)

    inhibition = {
        "mechanism": "UPDATE_INHIBITED",
        "amendment": "AMD-005",
        "inhibited": inhibited,
        "violated_guards": violated_guards,
        "rejected_candidate": rejected_candidate if inhibited else None,
        "rejected_candidate_stage_objective": candidate_objective if inhibited else None,
        "retained_values": dict(selected),
        "retained_source": (
            "frozen initial value (initial_simulation.lock)" if inhibited else None
        ),
        "rule": (
            "if verification-seed degradation OR the regression guard exceeds its "
            "preregistered tolerance, reject the candidate parameter update, retain "
            "the previous frozen/initial value, mark UPDATE_INHIBITED and continue "
            "to the next stage"
        ),
        "claim_boundary": (
            "Functionally inspired only: a proposed state change is suppressed when "
            "independent evidence indicates the update harms system-level stability. "
            "This is an engineering control rule. It does NOT model, demonstrate or "
            "provide evidence for any biological neural inhibition mechanism."
        ),
    }

    elapsed = time.perf_counter() - started_at
    summary: dict[str, Any] = {
        "stage_id": stage_id,
        "stage_index": STAGE_ORDER.index(stage_id) + 1,
        "outcome": outcome,
        "flags": stage_flags,
        "optimizer_run": bool(n),
        "status": "HAS_FREE_PARAMETERS" if n else "NO_FREE_PARAMETERS",
        "physical_model_retained": True,
        "dimensions": dimensions,
        "bounds": {k: list(v) for k, v in bounds_map.items()},
        "held_at_frozen_initial_value": held,
        "inherited_frozen_parameters": dict(frozen_parameters),
        "fitted_parameters": {
            name: {
                "value": selected[name],
                "initial": float(identity.initial_values[name]),
                "bounds": list(bounds_map[name]),
                "at_boundary": _at_boundary(selected[name], bounds_map[name]),
                "relative_position": _relative_position(selected[name], bounds_map[name]),
                "update_inhibited": inhibited,
                "rejected_candidate": (
                    rejected_candidate[name] if inhibited else None
                ),
            }
            for name in dimensions
        },
        "inhibition": inhibition,
        "objective_before": objective_before,
        "objective_after": objective_after,
        "objective_improvement": objective_before - objective_after,
        "optimized_cells": [f"{c}|{f}" for c, f in declared_cells],
        "monitored_terms_before": start_record.objective_terms,
        "monitored_terms_after": after_record.objective_terms,
        "w1_raw_before": start_record.w1_raw_terms,
        "w1_raw_after": after_record.w1_raw_terms,
        "objective_total_before": start_record.objective_total,
        "objective_total_after": after_record.objective_total,
        "verification_seed_objective": verification_record.stage_objective,
        "verification_seed_check": seed_check,
        "restart_results": restart_results,
        "multimodal": multimodal,
        "tie_break": tie_break,
        "regression_guard": regression,
        "evaluations_used": budget.used,
        "evaluations_budget": budget_limit,
        "evaluations_failed": budget.failed,
        "failure_ratio": budget.failure_ratio(),
        "failure_ratio_threshold": failure_threshold,
        "replayed_from_journal": evaluator.replay_hits,
        "genuine_evaluations": evaluator.genuine,
        "evaluation_cache_hits": evaluator.cache_hits,
        "render_cache": simulator.cache.stats(),
        "wall_clock_s": elapsed,
        "seeds": {"common_random_numbers": dict(CRN_SEEDS),
                  "verification": dict(VERIFICATION_SEEDS),
                  "optimizer_seed": optimizer_seed,
                  "restart_seeds": [optimizer_seed + 1000 * k for k in range(restarts)]},
        "preregistration_hash": identity.prereg_hash,
        "raw_data_hash": real.raw_hash,
        "identity": identity.to_artifact(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    journal.close()
    _write_json(stage_dir / "stage_summary.json", summary)
    progress(
        f"[{stage_id}] outcome={outcome} J {objective_before:.9g} -> "
        f"{objective_after:.9g} evaluations={budget.used}/{budget_limit} "
        f"({elapsed:.1f}s)"
    )
    return StageOutcome(stage_id=stage_id, summary=summary, selected=selected)


def _at_boundary(value: float, bound: tuple[float, float]) -> bool:
    """距邊界 < 1% 範圍寬即視為貼邊（CAL-PREREG-003 parameter_boundary_report）。"""
    lo, hi = bound
    width = hi - lo
    return bool(min(value - lo, hi - value) < 0.01 * width)


def _relative_position(value: float, bound: tuple[float, float]) -> float:
    lo, hi = bound
    return float((value - lo) / (hi - lo))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default),
        encoding="utf-8",
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if value == math.inf:
        return "Infinity"
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


# ---------------------------------------------------------------------------
# 五個階段
# ---------------------------------------------------------------------------


def run_formal_calibration(
    out_root: str | Path = "outputs/calibration",
    freeze_dir: str | Path = "freeze",
    repo_root: str | Path = ".",
    source_root: str | Path = "data/raw_real/edge_impulse_export",
    code_version: str = "",
    resume: bool = False,
    scientific: bool = True,
    maxiter: int | None = None,
    restarts: int | None = None,
    render_cache_capacity: int = 8,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """依 CAL-PREREG-003 執行完整的 Formal Calibration。

    `scientific=False` 是**冒煙模式**：它用一個刻意極小的預算只驗證管線接得起來，
    產出一律標記 `scientific_result: false`，不得被引用為校準結果，也不得
    改變任何凍結的協定。它與正式執行走**同一條**程式路徑 —— 用另一條路徑
    冒煙，等於驗證了一個不會被執行的東西。
    """
    say = progress or (lambda _message: None)
    root = Path(repo_root)
    out = Path(out_root)
    out.mkdir(parents=True, exist_ok=True)
    wall_started = time.time()

    identity = load_frozen_identity(freeze_dir, root)
    say(
        "frozen identity verified: "
        f"prereg={identity.prereg_hash[:12]} stage0={identity.stage0_hash[:12]} "
        f"s_f={identity.sf_hash[:12]} lock={identity.initial_lock_hash[:12]}"
    )

    protocol = identity.protocol
    effective_maxiter = int(
        maxiter if maxiter is not None else protocol["optimizer"]["multivariate_stages"]["maxiter"]
    )
    effective_restarts = int(
        restarts if restarts is not None else protocol["restart"]["count"]
    )
    popsize = int(protocol["optimizer"]["multivariate_stages"]["options"]["popsize"])
    if scientific:
        frozen_maxiter = int(protocol["optimizer"]["multivariate_stages"]["maxiter"])
        frozen_restarts = int(protocol["restart"]["count"])
        if effective_maxiter != frozen_maxiter or effective_restarts != frozen_restarts:
            raise FormalCalibrationError(
                "FROZEN_PROTOCOL_DRIFT",
                f"a scientific run must use the frozen budget "
                f"(maxiter={frozen_maxiter}, restarts={frozen_restarts}), got "
                f"maxiter={effective_maxiter}, restarts={effective_restarts}. "
                "Reducing the budget to save time is forbidden.",
            )

    run_config = {
        "spp": int(identity.initial_values["spp"]),
        "resolution": list(identity.initial_values["resolution"]),
        "temporal_bins": int(identity.initial_values["temporal_bins"]),
        "n_samples": int(identity.initial_values["TOF_RECORDING_POINTS"]),
        "maxiter": effective_maxiter,
        "restarts": effective_restarts,
        "popsize": popsize,
        "scientific": bool(scientific),
    }

    checkpoint_path = out / "checkpoint.json"
    checkpoint = Checkpoint.load(checkpoint_path) if resume else None
    if checkpoint is not None:
        checkpoint.assert_compatible(
            identity.to_artifact(), code_version, RUNNER_VERSION, run_config
        )
        say(
            "resuming from checkpoint; completed stages: "
            f"{checkpoint.payload['stage_order_completed'] or 'none'}"
        )
    else:
        if resume:
            say("no checkpoint found; starting a fresh run")
        checkpoint = Checkpoint.create(
            checkpoint_path,
            identity.to_artifact(),
            code_version,
            RUNNER_VERSION,
            run_config,
        )

    # -- 合法（重）讀 calibration partition ---------------------------------
    purpose = (
        "CAL-PREREG-003 formal calibration: per-class real distributions for the 16 "
        "W1 terms"
        if scientific
        else "NON-SCIENTIFIC smoke: formal calibration runner mechanics only"
    )
    real = load_real_calibration(
        identity, purpose=purpose, code_version=code_version,
        source_root=source_root, repo_root=root,
    )
    checkpoint.record_ledger_entry(real.ledger_entry)
    say(
        f"calibration partition read (ledger index {real.ledger_entry['index']}); "
        f"raw hash verified {real.raw_hash[:16]}"
    )

    simulator = Simulator(
        identity=identity,
        spp=run_config["spp"],
        resolution=tuple(run_config["resolution"]),
        temporal_bins=run_config["temporal_bins"],
        n_samples=run_config["n_samples"],
    )
    simulator.cache.capacity = int(render_cache_capacity)

    frozen_parameters: dict[str, float] = dict(checkpoint.frozen_parameters)
    outcomes: list[StageOutcome] = []
    stopped_at: str | None = None
    stop_reason: str | None = None

    for stage_id in STAGE_ORDER:
        existing = checkpoint.completed(stage_id)
        if existing is not None:
            say(f"[{stage_id}] already complete in checkpoint ({existing['outcome']})")
            outcomes.append(
                StageOutcome(
                    stage_id=stage_id,
                    summary=existing,
                    selected={
                        name: float(spec["value"])
                        for name, spec in existing["fitted_parameters"].items()
                    },
                )
            )
            if existing["outcome"] in _TERMINAL_OUTCOMES:
                stopped_at, stop_reason = stage_id, existing["outcome"]
                break
            continue

        result = run_stage(
            identity=identity,
            simulator=simulator,
            real=real,
            stage_id=stage_id,
            frozen_parameters=frozen_parameters,
            out_root=out,
            checkpoint=checkpoint,
            resume=resume,
            maxiter=effective_maxiter,
            restarts=effective_restarts,
            popsize=popsize,
            progress=say,
        )
        outcomes.append(result)
        # 階段隔離：結論一旦寫進 frozen_parameters，後面的階段只能讀不能改。
        checkpoint.complete_stage(stage_id, result.summary, result.selected)
        frozen_parameters.update(result.selected)

        if result.outcome in _TERMINAL_OUTCOMES:
            stopped_at, stop_reason = stage_id, result.outcome
            say(f"STOP: stage {stage_id} ended in {result.outcome}")
            break

    wall_clock = time.time() - wall_started
    checkpoint.add_runtime(wall_clock)
    checkpoint.save()

    report = _build_report(
        identity=identity,
        real=real,
        outcomes=outcomes,
        frozen_parameters=frozen_parameters,
        simulator=simulator,
        run_config=run_config,
        code_version=code_version,
        wall_clock=wall_clock,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        scientific=scientific,
        repo_root=root,
    )

    _write_json(out / "calibration_report.json", report)
    _write_json(out / "calibration_parameter_delta.json", report["parameter_delta"])
    _write_json(out / "calibration_objective_history.json", report["objective_history"])
    _write_json(out / "calibration_runtime_report.json", report["runtime_report"])
    _write_json(out / "calibrated_simulation.lock.json", report["calibrated_simulation_lock"])
    return report


def _build_report(
    identity: FrozenIdentity,
    real: RealCalibration,
    outcomes: list[StageOutcome],
    frozen_parameters: dict[str, float],
    simulator: Simulator,
    run_config: dict[str, Any],
    code_version: str,
    wall_clock: float,
    stopped_at: str | None,
    stop_reason: str | None,
    scientific: bool,
    repo_root: Path,
) -> dict[str, Any]:
    """把五個階段的結果組成最終報告。"""
    by_stage = {o.stage_id: o.summary for o in outcomes}
    first = outcomes[0].summary if outcomes else None
    last = outcomes[-1].summary if outcomes else None

    initial_terms = dict(first["monitored_terms_before"]) if first else {}
    final_terms = dict(last["monitored_terms_after"]) if last else {}
    initial_w1 = dict(first["w1_raw_before"]) if first else {}
    final_w1 = dict(last["w1_raw_after"]) if last else {}

    calibration_terms = [
        {
            "cell": f"{c}|{f}",
            "class": c,
            "feature": f,
            "s_f": identity.s_f[f],
            "w1_initial": initial_w1.get(f"{c}|{f}"),
            "w1_calibrated": final_w1.get(f"{c}|{f}"),
            "nw_initial": initial_terms.get(f"{c}|{f}"),
            "nw_calibrated": final_terms.get(f"{c}|{f}"),
            "nw_delta": (
                initial_terms[f"{c}|{f}"] - final_terms[f"{c}|{f}"]
                if f"{c}|{f}" in initial_terms and f"{c}|{f}" in final_terms
                else None
            ),
        }
        for c, f in all_cells()
    ]

    fitted = {}
    for outcome in outcomes:
        for name, spec in outcome.summary["fitted_parameters"].items():
            fitted[name] = {**spec, "fitted_in_stage": outcome.stage_id}

    held: dict[str, Any] = {}
    for outcome in outcomes:
        for name in outcome.summary["held_at_frozen_initial_value"]:
            held[name] = {
                "value": identity.initial_values.get(name),
                "status": "GAUGE_FIXED_AT_FROZEN_INITIAL_VALUE",
                "stage": outcome.stage_id,
            }
    for name in identity.prereg["payload"]["not_fitted"]:
        held.setdefault(
            name,
            {
                "value": identity.initial_values.get(name),
                "status": "NOT_FITTED_HELD_AT_FROZEN_INITIAL_VALUE",
                "stage": None,
            },
        )

    total_used = sum(s["evaluations_used"] for s in by_stage.values())
    total_budget = sum(s["evaluations_budget"] for s in by_stage.values())
    # AMD-005：UPDATE_INHIBITED 是一個**已解決**的階段結局 —— 參數留在凍結初值，
    # 物理模型完整保留。它不阻止校準完成，也不阻止凍結。
    all_converged = all(
        s["outcome"]
        in {
            "CONVERGED",
            "NO_IMPROVEMENT_REQUIRED",
            "NO_FREE_PARAMETERS",
            "UPDATE_INHIBITED",
        }
        for s in by_stage.values()
    )
    complete = len(by_stage) == len(STAGE_ORDER) and stopped_at is None

    ledger = json.loads(
        (repo_root / "data" / "splits" / "calibration_access_ledger.json").read_text(
            encoding="utf-8"
        )
    )
    split_registry = json.loads(
        (repo_root / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )

    overall = (
        "CALIBRATION_COMPLETE"
        if complete and all_converged
        else (stop_reason or "CALIBRATION_INCOMPLETE")
    )

    parameter_delta = {
        "scientific_result": scientific,
        "parameters": [
            {
                "name": name,
                "initial": spec["initial"],
                "calibrated": spec["value"],
                "absolute_delta": spec["value"] - spec["initial"],
                "relative_delta": (
                    (spec["value"] - spec["initial"]) / abs(spec["initial"])
                    if spec["initial"]
                    else None
                ),
                "bounds": spec["bounds"],
                "at_boundary": spec["at_boundary"],
                "relative_position_in_bounds": spec["relative_position"],
                "fitted_in_stage": spec["fitted_in_stage"],
            }
            for name, spec in sorted(fitted.items())
        ],
        "boundary_report": {
            "rule": (
                "a parameter finishing within 1% of the registered range width of a "
                "bound is flagged: the data wants to push it outside the range that "
                "was registered at the initial freeze"
            ),
            "flagged": sorted(n for n, s in fitted.items() if s["at_boundary"]),
        },
    }

    objective_history = {
        "scientific_result": scientific,
        "cells": [f"{c}|{f}" for c, f in all_cells()],
        "per_stage": [
            {
                "stage_id": stage_id,
                "outcome": summary["outcome"],
                "optimized_cells": summary["optimized_cells"],
                "stage_objective_before": summary["objective_before"],
                "stage_objective_after": summary["objective_after"],
                "full_objective_before": summary["objective_total_before"],
                "full_objective_after": summary["objective_total_after"],
                "nw_before": summary["monitored_terms_before"],
                "nw_after": summary["monitored_terms_after"],
                "restart_results": summary["restart_results"],
                "regression_guard": summary["regression_guard"],
            }
            for stage_id, summary in ((o.stage_id, o.summary) for o in outcomes)
        ],
        "full_objective_initial": first["objective_total_before"] if first else None,
        "full_objective_calibrated": last["objective_total_after"] if last else None,
    }

    runtime_report = {
        "scientific_result": scientific,
        "wall_clock_s": wall_clock,
        "wall_clock_h": wall_clock / 3600.0,
        "per_stage": {
            stage_id: {
                "wall_clock_s": summary["wall_clock_s"],
                "evaluations_used": summary["evaluations_used"],
                "evaluations_budget": summary["evaluations_budget"],
                "genuine_evaluations": summary["genuine_evaluations"],
                "replayed_from_journal": summary["replayed_from_journal"],
                "evaluation_cache_hits": summary["evaluation_cache_hits"],
                "seconds_per_evaluation": (
                    summary["wall_clock_s"] / summary["evaluations_used"]
                    if summary["evaluations_used"]
                    else None
                ),
            }
            for stage_id, summary in by_stage.items()
        },
        "render_cache": simulator.cache.stats(),
        "total_evaluations_used": total_used,
        "total_evaluations_budget": total_budget,
        "cost_model_note": (
            "CAL-PREREG-003 runtime_cost_model records C_render = 60.8-84 s and is "
            "explicitly non-binding. That measurement predates the bit-identical "
            "surrogate refactor in commit 08118b1, which lifted the deterministic "
            "TransientObservables extraction out of the 500-sample loop. The "
            "evaluation budget is unchanged; only the wall clock is smaller."
        ),
    }

    calibrated_lock = {
        "record_id": "calibrated_simulation.candidate",
        "frozen": False,
        "eligible_to_freeze": bool(complete and all_converged and scientific),
        "claim_boundary": (
            "This is a CANDIDATE document, not a frozen lock. CAL-PREREG-003 "
            "after_calibration_forbidden forbids freezing calibrated_simulation.lock "
            "automatically: freezing is a separate action that requires its own "
            "audit. Nothing here authorises opening held-out or starting Formal E1."
        ),
        "scientific_result": scientific,
        "calibrated_parameter_values": {
            **{k: v for k, v in sorted(identity.initial_values.items())},
            **{k: float(v) for k, v in sorted(frozen_parameters.items())},
        },
        "fitted_parameters": {k: v["value"] for k, v in sorted(fitted.items())},
        "held_parameters": held,
        "identity": identity.to_artifact(),
        "stage_outcomes": {k: v["outcome"] for k, v in by_stage.items()},
        "overall_outcome": overall,
    }

    return {
        "report_id": "calibration_report",
        "runner": {"module": __name__, "version": RUNNER_VERSION,
                   "code_version": code_version},
        "scientific_result": scientific,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_outcome": overall,
        "complete": complete,
        "stopped_at_stage": stopped_at,
        "stop_reason": stop_reason,
        "identity": identity.to_artifact(),
        "real_calibration": real.summary(),
        "run_config": run_config,
        "stage_order": list(STAGE_ORDER),
        "stage_summaries": by_stage,
        "calibration_terms": calibration_terms,
        "seven_calibrated_parameters": {k: v["value"] for k, v in sorted(fitted.items())},
        "held_or_fixed_parameters": held,
        "per_stage_convergence": {
            stage_id: {
                "outcome": summary["outcome"],
                "flags": summary["flags"],
                "optimizer_run": summary["optimizer_run"],
                "restarts": summary["restart_results"],
                "multimodal": summary["multimodal"],
                "tie_break": summary["tie_break"],
            }
            for stage_id, summary in by_stage.items()
        },
        "verification_seed_results": {
            stage_id: summary["verification_seed_check"]
            for stage_id, summary in by_stage.items()
        },
        "side_effect_regression_checks": {
            stage_id: summary["regression_guard"]
            for stage_id, summary in by_stage.items()
        },
        "inhibitory_control": {
            "amendment": "AMD-005",
            "per_stage": {
                stage_id: summary.get("inhibition")
                for stage_id, summary in by_stage.items()
            },
            "inhibited_stages": [
                stage_id
                for stage_id, summary in by_stage.items()
                if (summary.get("inhibition") or {}).get("inhibited")
            ],
            "claim_boundary": (
                "Functionally inspired only. A proposed parameter update is "
                "suppressed when independent evidence (verification seeds or the "
                "regression guard) indicates it harms system-level stability. This "
                "is an engineering control rule and is NOT evidence for any "
                "biological neural inhibition mechanism."
            ),
        },
        "evaluation_counts": {
            "per_stage": {
                stage_id: {
                    "used": summary["evaluations_used"],
                    "budget": summary["evaluations_budget"],
                    "failed": summary["evaluations_failed"],
                    "failure_ratio": summary["failure_ratio"],
                }
                for stage_id, summary in by_stage.items()
            },
            "total_used": total_used,
            "total_budget": total_budget,
            "counted_by": "the runner itself; scipy's reported nfev is not trusted",
        },
        "parameter_delta": parameter_delta,
        "objective_history": objective_history,
        "runtime_report": runtime_report,
        "cache_stats": {
            "render_cache": simulator.cache.stats(),
            "evaluation_cache_hits": sum(
                s["evaluation_cache_hits"] for s in by_stage.values()
            ),
            "policy": (
                "reuse only where the cache key covers every scientific input, so a "
                "hit is bit-identical to a recomputation; no class-level or "
                "parameter-level heuristic caches exist"
            ),
        },
        "access_ledger": {
            "calibration_access_count": int(ledger["calibration_access_count"]),
            "entries": ledger["entries"],
            "this_run_entry_index": real.ledger_entry["index"],
        },
        "heldout_access_count": int(split_registry["heldout_access_count"]),
        "calibrated_simulation_lock": calibrated_lock,
        "after_this": (
            "STOP. Formal E1 is a separate decision and held-out stays sealed "
            "(heldout_access_count must remain 0)."
        ),
    }
