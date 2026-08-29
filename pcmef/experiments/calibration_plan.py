# PC-MEF Research System source maintenance contract
# 上下游: 讀 configs/calibration_preregistration.yaml 與經 core.formal_loader
#         解析的 freeze/initial_simulation.lock.json；由 calibration_prereg 的
#         CP-03/CP-07/CP-13/CP-14 呼叫，日後由 calibration 執行器直接使用；
#         其 bounds_resolution_hash 進 AMD-003 與 CAL-PREREG-002。
# 檔案路徑: pcmef/experiments/calibration_plan.py
# 產生時間: 2026-08-30 09:30 +08:00
# 版本: v0.1.0
# 功能說明: 把已凍結的預註冊「翻譯」成 optimizer 真正會用到的東西 ——
#           每個階段有幾個搜尋維度、各維度的數值上下界、族群大小、
#           每次重啟與每個階段的評估硬上限，以及超出上限即中止的守衛。
# 模組定位: frozen protocol 與 optimizer 之間**唯一**的橋。它存在的理由是
#           讓「optimizer 用了哪組界線與多少預算」可以由凍結物唯一重建：
#           只要有兩條路徑算得出 bounds，就會有兩套 bounds，而其中一套
#           不在任何凍結物裡。
# 主要責任:
#   1. expand_dimensions() 把 registry 參數展開成 optimizer 維度
#   2. resolve_numeric_bounds() 產生唯一、可重建的數值界線
#   3. population_size() / budget_per_restart() / budget_per_stage() 精確預算
#   4. EvaluationBudget 在超出預註冊上限時中止，而不是繼續跑
#   5. bounds_resolution_hash() 讓上述結果可被凍結與比對
# 維護提醒:
#   - 不得在本模組之外另算一次 bounds；那正是本模組要防的事。
#   - 不得以 float(x) 去救字串界線。字串界線只能經預註冊逐項宣告的
#     numeric interpretation 通過，且宣告值必須與凍結字面值是同一個數。
#   - 不得讓多值參數（dict/list）隱含地佔一個維度；未宣告展開即拒絕，
#     否則 _ALBEDO_BY_PRESET 這種三類各一的量會被當成一個純量。
#   - 不得把 population_size 的公式改成「大概是這樣」；它必須與 scipy 的
#     init='sobol' 規則逐位元一致，`test_population_matches_scipy` 盯著這一點。
#   - 不得在預算用盡時取 best-so-far 並宣稱收斂；那是 NOT_CONVERGED。
#   - v0.1.0 新增：AMD-003 的預算與界線消歧義（NOTE-042）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_plan.py -v
#   - py -3.10 -m pcmef.cli calibration preregister --validate
# ------------------------------------------------------------

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from pcmef.core.hash import hash_object

__all__ = [
    "CalibrationPlanError",
    "BudgetExceeded",
    "population_size",
    "budget_per_restart",
    "budget_per_stage",
    "expand_dimensions",
    "resolve_numeric_bounds",
    "bounds_resolution_hash",
    "stage_budgets",
    "EvaluationBudget",
]


class CalibrationPlanError(ValueError):
    """界線無法唯一重建，或維度展開未被宣告。"""


class BudgetExceeded(RuntimeError):
    """評估次數超出預註冊的硬上限。"""


# ---------------------------------------------------------------------------
# 族群大小與預算
# ---------------------------------------------------------------------------


def population_size(n_dimensions: int, popsize: int = 15, init: str = "sobol") -> int:
    """scipy `differential_evolution` 的實際族群大小。

    scipy 的 `init='sobol'` 會把族群補到 2 的冪
    （`_differentialevolution.py`：`n_s = int(2 ** np.ceil(np.log2(...)))`），
    因此 `popsize` 是**乘數而非族群大小**，而實際值往往比 `popsize * N` 大。
    預算若照 `popsize` 估算會低估數倍 —— 這正是 CAL-PREREG-001 的缺陷。
    """
    if init != "sobol":
        raise CalibrationPlanError(
            f"population_size only models init='sobol'; got {init!r}. Another "
            "initialiser has a different population rule and would silently "
            "change the budget."
        )
    if n_dimensions < 1:
        raise CalibrationPlanError(f"n_dimensions must be >= 1, got {n_dimensions}")
    return int(2 ** math.ceil(math.log2(popsize * max(1, n_dimensions))))


def budget_per_restart(n_dimensions: int, popsize: int, maxiter: int) -> int:
    """單次重啟的評估硬上限。

    scipy 先評估整個初始族群，之後每一代再評估至多一個族群量，
    因此 `nfev <= P * (maxiter + 1)`。實測 N=2/3/4/7 皆恰為
    `P * (nit + 1)`，見 tests/unit/test_calibration_plan.py。
    """
    if maxiter < 1:
        raise CalibrationPlanError(f"maxiter must be >= 1, got {maxiter}")
    return population_size(n_dimensions, popsize) * (maxiter + 1)


def budget_per_stage(
    n_dimensions: int, popsize: int, maxiter: int, restarts: int
) -> int:
    if restarts < 1:
        raise CalibrationPlanError(f"restarts must be >= 1, got {restarts}")
    return restarts * budget_per_restart(n_dimensions, popsize, maxiter)


# ---------------------------------------------------------------------------
# 維度展開
# ---------------------------------------------------------------------------


def expand_dimensions(
    parameters: list[str], protocol: dict[str, Any], registry: Any
) -> list[str]:
    """把 registry 參數名展開成 optimizer 的搜尋維度名稱。

    純量參數展開為自己；多值參數（dict / list）**必須**在預註冊的
    `dimension_expansion` 明確宣告，否則拒絕 —— 一個沒宣告的 dict
    會安靜地佔掉一個維度，而它其實有三個自由度。
    """
    expansion = protocol.get("dimension_expansion") or {}
    by_name = registry.by_name()
    dimensions: list[str] = []
    for name in parameters:
        entry = by_name.get(name)
        if entry is None:
            raise CalibrationPlanError(f"{name!r} is not in the parameter registry")
        declared = expansion.get(name)
        if declared is not None:
            declared_dims = list(declared.get("dimensions") or [])
            if not declared_dims:
                raise CalibrationPlanError(
                    f"dimension_expansion for {name!r} declares no dimensions"
                )
            dimensions.extend(declared_dims)
            continue
        if isinstance(entry.value, (dict, list, tuple)):
            raise CalibrationPlanError(
                f"{name!r} holds a multi-valued initial value "
                f"({type(entry.value).__name__}) but the preregistration declares "
                "no dimension_expansion for it. An undeclared multi-valued "
                "parameter would silently occupy one search dimension while "
                "actually carrying several degrees of freedom."
            )
        dimensions.append(name)
    return dimensions


# ---------------------------------------------------------------------------
# 數值界線
# ---------------------------------------------------------------------------


def _native_floats(bound: Any) -> list[float] | None:
    """只在界線**原生就是數字**時回傳數值，否則 None。"""
    if not isinstance(bound, (list, tuple)) or len(bound) != 2:
        return None
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in bound):
        return None
    return [float(v) for v in bound]


def resolve_numeric_bounds(
    protocol: dict[str, Any], lock_payload: dict[str, Any], registry: Any
) -> dict[str, tuple[float, float]]:
    """由 frozen lock + frozen protocol 唯一重建 optimizer 的數值界線。

    只有兩種來源，且兩者都在凍結物裡：
      1. lock 的 `parameter_ranges` 原生就是數字 -> 直接使用；
      2. 該值是字串 -> 必須有預註冊宣告的 numeric interpretation，
         且 `float(凍結字面值)` 必須**恰好等於**宣告值。

    第二條的「恰好等於」是關鍵：允許宣告與字面值不同，等於讓預註冊可以
    在不改 lock 的情況下放寬界線，那就是第二套未凍結的 bounds。
    """
    frozen_ranges = lock_payload.get("parameter_ranges") or {}
    interpretations = {
        str(e["parameter"]): [float(v) for v in e["interpreted_as"]]
        for e in (protocol.get("bounds", {}).get("declared_numeric_interpretations") or [])
    }
    expansion = protocol.get("dimension_expansion") or {}

    resolved: dict[str, tuple[float, float]] = {}
    for stage in protocol.get("stagewise", []):
        for name in stage.get("parameters") or []:
            if name not in frozen_ranges:
                raise CalibrationPlanError(
                    f"{name!r} is fitted but has no frozen allowed_range in "
                    "initial_simulation.lock; calibration may only move parameters "
                    "whose search range was registered before the freeze"
                )
            frozen = frozen_ranges[name]
            numeric = _native_floats(frozen)
            if numeric is None:
                declared = interpretations.get(name)
                if declared is None:
                    raise CalibrationPlanError(
                        f"{name!r} has a non-numeric frozen bound {frozen!r} and the "
                        "preregistration declares no numeric interpretation for it"
                    )
                if not isinstance(frozen, (list, tuple)) or len(frozen) != 2:
                    raise CalibrationPlanError(
                        f"{name!r} frozen bound {frozen!r} is not a two-element range"
                    )
                for raw, value in zip(frozen, declared):
                    try:
                        literal = float(str(raw))
                    except (TypeError, ValueError) as exc:
                        raise CalibrationPlanError(
                            f"{name!r} frozen bound element {raw!r} is not a number "
                            "in any reading"
                        ) from exc
                    if literal != value:
                        raise CalibrationPlanError(
                            f"{name!r}: declared numeric interpretation {value!r} "
                            f"differs from the frozen literal {raw!r} ({literal!r}). "
                            "A declaration that changes the bound is a second, "
                            "unfrozen set of bounds."
                        )
                numeric = [float(v) for v in declared]

            lo, hi = numeric
            if not (lo < hi):
                raise CalibrationPlanError(f"{name!r} bound {numeric!r} is not lo < hi")

            declared_expansion = expansion.get(name)
            if declared_expansion is not None:
                if not declared_expansion.get("shared_bounds", False):
                    raise CalibrationPlanError(
                        f"dimension_expansion for {name!r} must declare "
                        "shared_bounds: true; per-dimension bounds would need their "
                        "own frozen ranges, which the lock does not carry"
                    )
                for dimension in declared_expansion["dimensions"]:
                    resolved[dimension] = (lo, hi)
            else:
                resolved[name] = (lo, hi)

    return dict(sorted(resolved.items()))


def bounds_resolution_hash(
    protocol: dict[str, Any], lock_payload: dict[str, Any], registry: Any
) -> str:
    """已解析界線的雜湊。凍進 AMD-003 與 CAL-PREREG-002，供事後比對。"""
    resolved = resolve_numeric_bounds(protocol, lock_payload, registry)
    return hash_object({k: list(v) for k, v in resolved.items()})


# ---------------------------------------------------------------------------
# 各階段預算
# ---------------------------------------------------------------------------


def stage_budgets(
    protocol: dict[str, Any], registry: Any
) -> dict[str, dict[str, int]]:
    """逐階段算出維度數、族群大小與兩層評估硬上限。"""
    optimizer = protocol["optimizer"]["multivariate_stages"]
    popsize = int(optimizer["options"]["popsize"])
    maxiter = int(optimizer["maxiter"])
    init = str(optimizer["options"]["init"])
    restarts = int(protocol["restart"]["count"])

    out: dict[str, dict[str, int]] = {}
    for stage in protocol.get("stagewise", []):
        dimensions = expand_dimensions(
            list(stage.get("parameters") or []), protocol, registry
        )
        n = len(dimensions)
        population = population_size(n, popsize, init)
        out[str(stage["id"])] = {
            "parameters": len(stage.get("parameters") or []),
            "dimensions": n,
            "population": population,
            "maxiter": maxiter,
            "restarts": restarts,
            "evaluations_per_restart": budget_per_restart(n, popsize, maxiter),
            "evaluations_per_stage": budget_per_stage(n, popsize, maxiter, restarts),
        }
    return out


# ---------------------------------------------------------------------------
# 預算守衛
# ---------------------------------------------------------------------------


@dataclass
class EvaluationBudget:
    """評估次數的硬上限守衛。

    它**自己數**，不採信 optimizer 回報的 `nfev`：預算的意義是
    「這個階段最多能消耗多少模擬」，而那要由真正被呼叫幾次決定，
    不是由某個函式庫事後回報的數字決定。
    """

    stage_id: str
    limit: int
    used: int = 0
    failed: int = 0
    _history: list[int] = field(default_factory=list, repr=False)

    def spend(self, count: int = 1) -> None:
        self.used += count
        if self.used > self.limit:
            raise BudgetExceeded(
                f"stage {self.stage_id}: evaluation {self.used} exceeds the "
                f"preregistered budget of {self.limit}. The stage outcome is "
                "NOT_CONVERGED; taking best-so-far and calling it converged, or "
                "raising the budget and re-running, are both forbidden "
                "(calibration preregistration, convergence.non_convergence)."
            )

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def wrap(self, objective: Callable[..., float]) -> Callable[..., float]:
        """把目標函數包成會計數的版本。非有限值計入 failed 但仍算一次評估。"""

        def counted(*args: Any, **kwargs: Any) -> float:
            self.spend()
            value = objective(*args, **kwargs)
            if not math.isfinite(value):
                self.failed += 1
            return value

        return counted

    def failure_ratio(self) -> float:
        return 0.0 if self.used == 0 else self.failed / self.used
