# PC-MEF Research System source maintenance contract
# 上下游: 驗證 pcmef.experiments.calibration_plan 的族群公式、評估預算守衛與
#         數值界線唯一性；族群公式以**實際呼叫 scipy** 對照，不以文件為準。
# 檔案路徑: tests/unit/test_calibration_plan.py
# 產生時間: 2026-08-30 11:20 +08:00
# 版本: v0.1.0
# 功能說明: 確認「optimizer 會用掉多少次評估、會在哪組界線上搜尋」這兩件事
#           是算得出來且擋得住的，而不是預註冊上寫的一個數字。
# 模組定位: AMD-003 兩項 P0 裁決的行為契約測試。CAL-PREREG-001 的預算之所以
#           錯了 8.6-25.7 倍，正是因為沒有任何測試把宣稱的數字與 scipy 的
#           實際行為對照過。本檔就是那個對照。
# 主要責任:
#   1. population_size() 必須與 scipy init='sobol' 的實測值逐項相同
#   2. 實際評估次數不得超過 budget_per_restart()
#   3. 超出預註冊預算時必須 FAIL（BudgetExceeded），不得繼續跑
#   4. 數值界線必須唯一可重建；字串界線缺宣告或宣告不符即拒絕
#   5. 凍結協定的預算表與 bounds hash 必須與公式/解析器一致
# 維護提醒:
#   - 不得把 test_population_matches_scipy 改成比對常數；它的價值就在於
#     真的去問 scipy。scipy 換版而規則改變時，這條必須失敗。
#   - 不得放寬 test_evaluations_never_exceed_the_preregistered_budget；
#     預算若擋不住，「用了多少評估」這個數字就不帶資訊。
#   - 不得讓 resolve_numeric_bounds 以 float() 救字串而讓本檔的負向測試變綠。
#   - v0.1.0 新增：對應 NOTE-042 / AMD-003。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_plan.py -v
#   - py -3.10 -m pcmef.cli calibration preregister --validate
# ------------------------------------------------------------

from __future__ import annotations

import copy
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import differential_evolution

from pcmef.core.formal_loader import load_formal_lock
from pcmef.core.parameters import ParameterRegistry
from pcmef.experiments.calibration_plan import (
    BudgetExceeded,
    CalibrationPlanError,
    EvaluationBudget,
    bounds_resolution_hash,
    budget_per_restart,
    budget_per_stage,
    expand_dimensions,
    population_size,
    resolve_numeric_bounds,
    stage_budgets,
)
from pcmef.experiments.calibration_prereg import load_protocol

REPO_ROOT = Path(__file__).resolve().parents[2]
FREEZE_DIR = REPO_ROOT / "freeze"

POPSIZE = 15
MAXITER_PROBE = 12


@pytest.fixture(scope="module")
def protocol() -> dict:
    return load_protocol()


@pytest.fixture(scope="module")
def registry() -> ParameterRegistry:
    return ParameterRegistry.load()


@pytest.fixture(scope="module")
def lock_payload() -> dict:
    return load_formal_lock("initial_simulation", FREEZE_DIR, REPO_ROOT).payload


def _run_de(n: int, maxiter: int) -> tuple[int, int]:
    """跑一次 DE，回傳 (實際呼叫次數, nit)。目標函數刻意平滑且便宜。"""
    calls = {"n": 0}

    def objective(x) -> float:
        calls["n"] += 1
        return float(np.sum((np.asarray(x) - 0.3) ** 2))

    result = differential_evolution(
        objective,
        [(0.0, 1.0)] * n,
        popsize=POPSIZE,
        init="sobol",
        maxiter=maxiter,
        tol=0.01,
        polish=False,
        seed=20260829,
        updating="deferred",
        mutation=(0.5, 1.0),
        recombination=0.7,
    )
    assert result.nfev == calls["n"], "scipy 的 nfev 與實際呼叫次數不一致"
    return calls["n"], result.nit


# ---------------------------------------------------------------------------
# 族群公式：對照 scipy 本人，不對照文件
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 4, 6, 7])
def test_population_matches_scipy(n):
    """P(N) = 2**ceil(log2(popsize*N))；實測 nfev 必須恰為 P*(nit+1)。

    CAL-PREREG-001 把 popsize 當成族群大小，因此預算低估了數倍。
    這條測試的存在就是為了讓那個誤解不可能再發生而沒有人察覺。
    """
    nfev, nit = _run_de(n, MAXITER_PROBE)
    expected_population = population_size(n, POPSIZE)
    assert expected_population == 2 ** math.ceil(math.log2(POPSIZE * n))
    assert nfev == expected_population * (nit + 1), (
        f"N={n}: nfev {nfev} != P {expected_population} * (nit {nit} + 1)"
    )


@pytest.mark.parametrize("n", [1, 2, 3, 4, 6, 7])
def test_evaluations_never_exceed_the_preregistered_budget(n):
    """實測評估次數必須落在 budget_per_restart 之內。"""
    nfev, _ = _run_de(n, MAXITER_PROBE)
    assert nfev <= budget_per_restart(n, POPSIZE, MAXITER_PROBE)


def test_population_is_a_multiplier_not_a_size():
    """popsize=15、N=7 的族群是 128，不是 15 也不是 105。"""
    assert population_size(7, 15) == 128
    assert population_size(2, 15) == 32
    assert population_size(6, 15) == 128


def test_budget_scales_with_restarts():
    assert budget_per_stage(7, 15, 100, 3) == 3 * budget_per_restart(7, 15, 100)


def test_non_sobol_initialiser_is_refused():
    """別的初始化有別的族群規則，靜默沿用會讓預算再次失真。"""
    with pytest.raises(CalibrationPlanError, match="sobol"):
        population_size(4, 15, init="latinhypercube")


# ---------------------------------------------------------------------------
# 預算守衛：超出必須 FAIL
# ---------------------------------------------------------------------------


def test_budget_guard_raises_on_the_first_evaluation_past_the_limit():
    budget = EvaluationBudget(stage_id="TEST", limit=3)
    for _ in range(3):
        budget.spend()
    assert budget.used == 3 and budget.remaining == 0
    with pytest.raises(BudgetExceeded, match="NOT_CONVERGED"):
        budget.spend()


def test_a_real_de_run_that_exceeds_its_budget_fails():
    """把預算設得比實際需要少，DE 必須被中止而不是跑完。

    這是本檔最重要的一條：預註冊寫了一個預算，而實際評估次數超過它時
    必須是**失敗**，不是一行警告。
    """
    n = 4
    honest_budget = budget_per_restart(n, POPSIZE, MAXITER_PROBE)
    budget = EvaluationBudget(stage_id="GEOMETRY_SURFACE_FOIL", limit=honest_budget // 4)

    def objective(x) -> float:
        return float(np.sum((np.asarray(x) - 0.3) ** 2))

    with pytest.raises(BudgetExceeded):
        differential_evolution(
            budget.wrap(objective),
            [(0.0, 1.0)] * n,
            popsize=POPSIZE,
            init="sobol",
            maxiter=MAXITER_PROBE,
            tol=0.01,
            polish=False,
            seed=20260829,
            updating="deferred",
        )
    assert budget.used > budget.limit


def test_the_preregistered_budget_is_actually_sufficient():
    """守衛不得反過來把合法的執行擋掉 —— 少了這條，上一條可以靠設 limit=0 通過。"""
    n = 3
    budget = EvaluationBudget(
        stage_id="OK", limit=budget_per_restart(n, POPSIZE, MAXITER_PROBE)
    )

    def objective(x) -> float:
        return float(np.sum((np.asarray(x) - 0.3) ** 2))

    result = differential_evolution(
        budget.wrap(objective),
        [(0.0, 1.0)] * n,
        popsize=POPSIZE,
        init="sobol",
        maxiter=MAXITER_PROBE,
        tol=0.01,
        polish=False,
        seed=20260829,
        updating="deferred",
    )
    assert result.nfev == budget.used <= budget.limit


def test_non_finite_evaluations_still_consume_budget():
    """失敗的評估一樣花了模擬時間；不計入的話預算就不是上限。"""
    budget = EvaluationBudget(stage_id="TEST", limit=10)
    wrapped = budget.wrap(lambda x: float("inf"))
    for _ in range(4):
        wrapped(0.0)
    assert budget.used == 4 and budget.failed == 4
    assert budget.failure_ratio() == 1.0


# ---------------------------------------------------------------------------
# 維度展開
# ---------------------------------------------------------------------------


def test_albedo_expands_to_three_dimensions(protocol, registry):
    dimensions = expand_dimensions(["_ALBEDO_BY_PRESET"], protocol, registry)
    assert dimensions == [
        "_ALBEDO_BY_PRESET.water",
        "_ALBEDO_BY_PRESET.bubbly",
        "_ALBEDO_BY_PRESET.misty",
    ]


def test_an_undeclared_multivalued_parameter_is_refused(protocol, registry):
    """未宣告展開的 dict 會安靜佔一個維度，而它其實有三個自由度。"""
    stripped = copy.deepcopy(protocol)
    stripped["dimension_expansion"] = {}
    with pytest.raises(CalibrationPlanError, match="dimension_expansion"):
        expand_dimensions(["_ALBEDO_BY_PRESET"], stripped, registry)


def test_participating_media_is_six_dimensions_not_four(protocol, registry):
    budgets = stage_budgets(protocol, registry)
    entry = budgets["PARTICIPATING_MEDIA"]
    assert entry["parameters"] == 4
    assert entry["dimensions"] == 6
    assert entry["population"] == 128


# ---------------------------------------------------------------------------
# 界線唯一性
# ---------------------------------------------------------------------------


def test_resolved_bounds_are_deterministic(protocol, lock_payload, registry):
    first = resolve_numeric_bounds(protocol, lock_payload, registry)
    second = resolve_numeric_bounds(protocol, lock_payload, registry)
    assert first == second
    assert bounds_resolution_hash(
        protocol, lock_payload, registry
    ) == bounds_resolution_hash(protocol, lock_payload, registry)


def test_every_fitted_dimension_has_a_numeric_bound(protocol, lock_payload, registry):
    resolved = resolve_numeric_bounds(protocol, lock_payload, registry)
    expected = sum(
        len(expand_dimensions(stage["parameters"], protocol, registry))
        for stage in protocol["stagewise"]
    )
    assert len(resolved) == expected == 20
    for name, (lo, hi) in resolved.items():
        assert isinstance(lo, float) and isinstance(hi, float), name
        assert lo < hi, name


def test_string_bound_without_declaration_is_refused(protocol, lock_payload, registry):
    stripped = copy.deepcopy(protocol)
    stripped["bounds"]["declared_numeric_interpretations"] = []
    with pytest.raises(CalibrationPlanError, match="no numeric interpretation"):
        resolve_numeric_bounds(stripped, lock_payload, registry)


def test_a_declaration_that_widens_the_bound_is_refused(
    protocol, lock_payload, registry
):
    """宣告與凍結字面值不符，等於第二套沒有被凍結的 bounds。"""
    tampered = copy.deepcopy(protocol)
    for entry in tampered["bounds"]["declared_numeric_interpretations"]:
        if entry["parameter"] == "signal_energy_to_mcps":
            entry["interpreted_as"] = [1.0e-6, 1.0e12]
    with pytest.raises(CalibrationPlanError, match="second"):
        resolve_numeric_bounds(tampered, lock_payload, registry)


def test_bounds_come_only_from_the_frozen_lock(protocol, lock_payload, registry):
    """原生數值界線必須逐字等於 lock；解析器不得自己生出一組。"""
    resolved = resolve_numeric_bounds(protocol, lock_payload, registry)
    frozen = lock_payload["parameter_ranges"]
    for name, bound in frozen.items():
        if name not in resolved:
            continue
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bound):
            assert resolved[name] == (float(bound[0]), float(bound[1])), name


def test_a_parameter_without_a_frozen_range_cannot_be_fitted(
    protocol, lock_payload, registry
):
    stripped = copy.deepcopy(lock_payload)
    stripped["parameter_ranges"].pop("_FOIL_REFLECTANCE_940NM")
    with pytest.raises(CalibrationPlanError, match="no frozen allowed_range"):
        resolve_numeric_bounds(protocol, stripped, registry)


# ---------------------------------------------------------------------------
# 凍結物一致性
# ---------------------------------------------------------------------------


def test_the_protocol_budget_table_matches_the_formula(protocol, registry):
    computed = stage_budgets(protocol, registry)
    declared = protocol["evaluation_budget"]["resolved"]
    for stage_id, values in computed.items():
        assert declared[stage_id]["dimensions"] == values["dimensions"]
        assert declared[stage_id]["population"] == values["population"]
        assert declared[stage_id]["per_restart"] == values["evaluations_per_restart"]
        assert declared[stage_id]["per_stage"] == values["evaluations_per_stage"]
    assert protocol["evaluation_budget"]["total_evaluations"] == sum(
        v["evaluations_per_stage"] for v in computed.values()
    )


def test_stage_0_never_references_the_calibration_scale(protocol):
    """stage 0 的操作性欄位不得依賴 s_f —— 那是 AMD-003 的核心。"""
    stage0 = protocol["stage_0"]
    assert stage0["reads_calibration_partition"] is False
    assert stage0["calibration_first_access"] is False
    assert stage0["normaliser"]["symbol"] == "sigma_MC"
    for text in (
        stage0["method"],
        stage0["admission_threshold"]["rule"],
        stage0["collinearity_check"]["rule"],
        stage0["normaliser"]["definition"],
    ):
        assert "s_f" not in str(text)
    assert protocol["s_f_relative_leverage_diagnostic"]["gating"] is False


def test_there_is_exactly_one_solver_path(protocol):
    assert "scalar_stages" not in protocol["optimizer"]
    assert protocol["optimizer"]["method_rule"]
    assert (
        protocol["optimizer"]["multivariate_stages"]["method"]
        == "scipy.optimize.differential_evolution"
    )


def test_calibration_partition_remains_unread():
    import json

    ledger = json.loads(
        (REPO_ROOT / "data" / "splits" / "calibration_access_ledger.json").read_text(
            encoding="utf-8"
        )
    )
    assert ledger["calibration_access_count"] == 0
    assert ledger["entries"] == []
