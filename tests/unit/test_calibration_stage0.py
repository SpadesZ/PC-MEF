# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；只測 pcmef.experiments.calibration_stage0 與
#         calibration_stage0_freeze 的**判定邏輯**，evaluate() 由測試注入，
#         因此不需要 mitsuba，也不寫出任何 artifact 到 repo。
# 檔案路徑: tests/unit/test_calibration_stage0.py
# 產生時間: 2026-08-30 15:50 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 stage 0 的門檻判定、sigma_MC 退化處理、共線性計算與封存前置
#           條件；重點在「不該通過的一定不通過」。
# 模組定位: CAL-PREREG-002 stage 0 的負向測試。它不驗證物理，
#           驗證的是這一階段有沒有辦法被繞過。
# 主要責任:
#   1. DIMENSION_BINDINGS 與已凍結 bounds 逐項相符
#   2. sigma_MC = 0 時 leverage_of 回 None 而非 inf
#   3. classify_leverage 對 borderline band 不做四捨五入
#   4. cosine_similarity 對零響應向量回 None 而非 0
#   5. run_stage0 在 borderline / undetermined 時要求裁決
#   6. freeze_stage0 拒絕凍結需要裁決的結果與重複凍結
# 維護提醒:
#   - 不得把 sigma_MC = 0 的期望值改成 inf 或某個大數來讓流程往前走；
#     這幾條測試存在的唯一理由就是擋住那個捷徑。
#   - 不得刪掉 test_a_clean_probe_completes；少了它，其餘負向測試會在
#     「判定永遠要求裁決」的情況下全部通過。
#   - v0.1.0 新增：首版 stage 0 判定測試（NOTE-043）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_stage0.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from pcmef.experiments.calibration_stage0 import (
    DIMENSION_BINDINGS,
    Stage0Error,
    classify_leverage,
    cosine_similarity,
    leverage_of,
    observable_names,
    run_stage0,
    summarise_recording,
)
from pcmef.experiments.calibration_stage0_freeze import (
    Stage0FreezeError,
    freeze_stage0,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FROZEN_PREREG = (
    REPO_ROOT / "freeze" / "preregistrations" / "CAL-PREREG-002.prereg.json"
)

NAMES = observable_names()
SETTINGS = {
    "threshold": 10.0,
    "borderline_band": [5.0, 20.0],
    "replicates": 8,
    "seed_set": [3001, 3002, 3003, 3004, 3005, 3006, 3007, 3008],
}


# ---------------------------------------------------------------------------
# 綁定表
# ---------------------------------------------------------------------------


def test_binding_table_matches_the_frozen_bounds_exactly():
    """一個沒有綁定的維度會被安靜跳過，但在 optimizer 裡仍是自由度。"""
    frozen = json.loads(FROZEN_PREREG.read_text(encoding="utf-8"))
    bounds = set(frozen["payload"]["resolved_bounds"])
    bindings = {b.dimension for b in DIMENSION_BINDINGS}
    assert bindings == bounds


def test_every_binding_declares_where_it_acts():
    for binding in DIMENSION_BINDINGS:
        assert binding.target in {
            "scene_constant", "scene_albedo", "medium", "surrogate"
        }
        assert binding.attribute
        # surrogate 常數作用在算圖之後，因此不需要重算場景；其餘一律需要。
        assert binding.requires_render == (binding.target != "surrogate")


def test_observables_carry_both_location_and_dispersion():
    """只用 median 會讓「只改離散度」的參數在建構上槓桿為 0。"""
    assert len(NAMES) == 32
    assert any(name.endswith("|median") for name in NAMES)
    assert any(name.endswith("|iqr") for name in NAMES)
    assert sum(name.endswith("|iqr") for name in NAMES) == 16


def test_summarise_recording_reports_median_and_iqr():
    values = np.array([[1.0, 2.0, 3.0, 4.0]] * 4 + [[5.0, 6.0, 7.0, 8.0]] * 4)
    summary = summarise_recording(values, "Empty")
    assert summary["Empty|distance_mm|median"] == pytest.approx(3.0)
    assert summary["Empty|distance_mm|iqr"] == pytest.approx(4.0)


def test_a_recording_with_the_wrong_width_is_rejected():
    with pytest.raises(Stage0Error, match="TOF_SCHEMA order"):
        summarise_recording(np.zeros((10, 3)), "Empty")


# ---------------------------------------------------------------------------
# sigma_MC 退化：本檔最重要的一組
# ---------------------------------------------------------------------------


def test_leverage_never_returns_infinity_for_any_input():
    """除零後當成無限大，等於讓沒有雜訊參考的量自動拿到入場券。"""
    for sigma in (0.0, -1.0, float("nan"), 1e-30):
        value, _branch = leverage_of(delta=1.0, sigma_mc=sigma, reference_value=1.0)
        assert value != float("inf")
        assert value is None or np.isfinite(value)


def test_leverage_is_a_plain_ratio_when_the_noise_reference_exists():
    value, branch = leverage_of(delta=-30.0, sigma_mc=2.0, reference_value=100.0)
    assert value == pytest.approx(15.0)
    assert branch == "RATIO"


def test_a_noiseless_observable_that_does_not_move_is_inert_not_undefined():
    """既沒有雜訊也沒有反應 -> 這個 observable 對該參數沒有資訊，槓桿恰為 0。"""
    value, branch = leverage_of(delta=0.0, sigma_mc=0.0, reference_value=100.0)
    assert branch == "INERT"
    assert value == 0.0


def test_a_noiseless_observable_that_moves_is_a_named_branch_not_an_invented_number():
    """無雜訊卻會動 -> 訊噪比發散。必須具名，不得寫成 inf 或某個大數。"""
    value, branch = leverage_of(delta=5.0, sigma_mc=0.0, reference_value=100.0)
    assert branch == "DETERMINISTIC_RESPONSE"
    assert value is None


def test_inert_and_deterministic_response_are_not_conflated():
    """兩者先前都落在同一個 None，於是「沒有資訊」與「完美證據」長得一樣。"""
    _, inert = leverage_of(delta=0.0, sigma_mc=0.0, reference_value=1.0)
    _, moving = leverage_of(delta=1.0, sigma_mc=0.0, reference_value=1.0)
    assert inert != moving


def test_an_undetermined_leverage_classifies_as_undetermined():
    assert classify_leverage(None, 10.0, (5.0, 20.0)) == "UNDETERMINED"


# ---------------------------------------------------------------------------
# 可行域推導
# ---------------------------------------------------------------------------


def test_a_feasible_bound_is_returned_unchanged():
    from pcmef.experiments.calibration_stage0 import derive_feasible_edge

    result = derive_feasible_edge(0.01, 0.5, lambda v: True)
    assert result["edge"] == 0.5
    assert result["status"] == "BOUND_IS_FEASIBLE"


def test_an_infeasible_bound_is_bisected_towards_the_initial_value():
    from pcmef.experiments.calibration_stage0 import derive_feasible_edge

    result = derive_feasible_edge(0.01, 0.5, lambda v: v <= 0.2)
    assert result["status"] == "DERIVED_BY_BISECTION"
    # 一律取可行側，因此導出的邊必定仍可行且不超過真實界線。
    assert result["edge"] <= 0.2
    assert result["edge"] == pytest.approx(0.2, abs=1e-3)


def test_the_derived_domain_is_always_a_subset_of_the_registered_range():
    from pcmef.experiments.calibration_stage0 import derive_feasible_edge

    result = derive_feasible_edge(0.01, 0.5, lambda v: v <= 0.2)
    assert 0.01 <= result["edge"] <= 0.5


def test_an_infeasible_initial_value_is_reported_rather_than_bisected():
    from pcmef.experiments.calibration_stage0 import derive_feasible_edge

    result = derive_feasible_edge(0.01, 0.5, lambda v: False)
    assert result["edge"] is None
    assert result["status"] == "INITIAL_VALUE_INFEASIBLE"


@pytest.mark.parametrize(
    "leverage,expected",
    [
        (4.99, "GAUGE_FIXED"),
        (5.0, "BORDERLINE"),
        (10.0, "BORDERLINE"),
        (20.0, "BORDERLINE"),
        (20.01, "ADMITTED"),
        (0.0, "GAUGE_FIXED"),
    ],
)
def test_the_borderline_band_is_not_rounded_to_either_side(leverage, expected):
    """K = 10 落在 band 內：達到門檻**還不夠**，必須離開 [5, 20] 才算判定。"""
    assert classify_leverage(leverage, 10.0, (5.0, 20.0)) == expected


def test_a_zero_response_vector_has_no_defined_angle():
    """記成 0（正交、可辨識）會讓完全看不見的參數看起來與別人無關。"""
    assert cosine_similarity(np.zeros(4), np.array([1.0, 0.0, 0.0, 0.0])) is None
    assert cosine_similarity(np.array([1.0, 1.0]), np.array([2.0, 2.0])) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# run_stage0 端到端（evaluate 由測試注入）
# ---------------------------------------------------------------------------


def _own_channel(dimension: str) -> str:
    """該維度**自己階段宣告**的第一個 observable。

    AMD-004 之後入場判定只看這一組通道，因此測試的效應必須落在這裡；
    落在別的通道上會（正確地）被判為 GAUGE_FIXED。
    """
    from pcmef.experiments.calibration_stage0 import declared_observables

    stage = next(s for s, members in STAGES.items() if dimension in members)
    return declared_observables(stage)[0]


def _make_evaluate(effects: dict[str, float], noise: float = 1.0):
    """假的 evaluate：每個維度的效應落在它自己階段宣告的第一個 observable。"""

    def evaluate(override, seed):
        row = {name: 100.0 for name in NAMES}
        if seed >= 0:
            # 種子間抖動 -> sigma_MC。用固定序列讓測試是決定性的。
            for name in NAMES:
                row[name] = 100.0 + noise * ((seed % 8) - 3.5)
        if override is not None:
            dimension = override["binding"].dimension
            span = effects.get(dimension, 0.0)
            lo, hi = BOUNDS[dimension]
            fraction = (override["value"] - lo) / (hi - lo)
            row[_own_channel(dimension)] = 100.0 + span * fraction
        return row

    return evaluate


BOUNDS = {"ambient_energy_to_mcps": (0.0, 1.0), "signal_energy_to_mcps": (0.0, 1.0)}
STAGES = {
    "AMBIENT": ["ambient_energy_to_mcps"],
    "SIGNAL_SCALE": ["signal_energy_to_mcps"],
}
INITIAL = {"ambient_energy_to_mcps": 0.5, "signal_energy_to_mcps": 0.5}


def _run(effects, noise=1.0):
    return run_stage0(
        resolved_bounds=dict(BOUNDS),
        initial_values=dict(INITIAL),
        stage_parameters=dict(STAGES),
        stage0_settings=dict(SETTINGS),
        evaluate=_make_evaluate(effects, noise),
    )


def _outcome(result, dimension):
    return next(p["outcome"] for p in result["parameters"] if p["dimension"] == dimension)


def test_declared_channels_cover_every_stage_the_protocol_defines():
    """漏掉一個階段會讓該階段的參數靜默失去診斷欄位。"""
    import yaml

    from pcmef.experiments.calibration_stage0 import (
        DECLARED_CHANNELS,
        declared_observables,
    )

    protocol = yaml.safe_load(
        (REPO_ROOT / "configs" / "calibration_preregistration.yaml").read_text(
            encoding="utf-8"
        )
    )
    stages = {str(s["id"]) for s in protocol["stagewise"]}
    assert set(DECLARED_CHANNELS) == stages
    # 每個階段宣告的 observable 都必須是那 32 個之一，不得憑空造名。
    for stage in stages:
        assert set(declared_observables(stage)) <= set(NAMES)
        assert declared_observables(stage)
    # 只有 stage 1 把 ambient 列為要最佳化的通道。
    ambient_stages = {
        s for s in stages
        if any("|ambient_rate_mcps|" in n for n in declared_observables(s))
    }
    assert ambient_stages == {"AMBIENT"}


def _run_with_channels(effects_by_observable, noise=1.0):
    """effects_by_observable: {dimension: {observable: span}}。"""

    def evaluate(override, seed):
        row = {name: 100.0 for name in NAMES}
        if seed >= 0:
            for name in NAMES:
                row[name] = 100.0 + noise * ((seed % 8) - 3.5)
        if override is not None:
            dimension = override["binding"].dimension
            lo, hi = BOUNDS[dimension]
            fraction = (override["value"] - lo) / (hi - lo)
            for observable, span in effects_by_observable.get(dimension, {}).items():
                row[observable] = 100.0 + span * fraction
        return row

    return run_stage0(
        resolved_bounds=dict(BOUNDS),
        initial_values=dict(INITIAL),
        stage_parameters=dict(STAGES),
        stage0_settings=dict(SETTINGS),
        evaluate=evaluate,
    )


def test_a_parameter_loud_elsewhere_but_silent_in_its_own_stage_is_not_admitted():
    """AMD-004 P0-1：入場判定必須用**該階段自己宣告**的 observable。

    這一條複製 stage 0 首跑實測到的情況：
    `_FOIL_SIZE_TO_DIAMETER_RATIO` 在 Water-filled 的 ambient 上槓桿 101.7
    而被 ADMITTED，但它在自己階段宣告的通道上只有 0.0006。
    入場的意思是「optimizer 看得見它」，而 optimizer 只看得見該階段目標
    函數裡的項 —— 靠別的階段才會最佳化的通道拿入場券是無效的。
    """
    from pcmef.experiments.calibration_stage0 import declared_observables

    own = declared_observables("AMBIENT")[0]           # ambient 通道，屬 stage AMBIENT
    other = declared_observables("SIGNAL_SCALE")[0]    # signal 通道，別的階段才最佳化

    # sigma_MC 對這組序列恰為 2.4495；span 245 -> 槓桿約 100，span 0.0024 -> 約 0.001
    result = _run_with_channels(
        {"ambient_energy_to_mcps": {other: 245.0, own: 0.0024}}
    )
    entry = next(
        p for p in result["parameters"] if p["dimension"] == "ambient_energy_to_mcps"
    )

    # 全域上很大（遠高於門檻 10），自己階段的通道上幾乎為零（遠低於 band 下緣 5）。
    assert entry["max_leverage"] > 50.0
    assert entry["max_leverage_observable"] == other
    assert entry["max_leverage_declared"] < 0.01
    assert entry["max_leverage_declared_observable"] == own

    # 這是本測試的重點：判定必須是 GAUGE_FIXED，不是 ADMITTED。
    assert entry["outcome"] == "GAUGE_FIXED"
    # 舊規則的判定仍留作診斷，因此「改判準之後結論變了哪些」看得見。
    assert entry["outcome_under_global_scope"] == "ADMITTED"
    assert result["parameters"] and any(
        w["dimension"] == "ambient_energy_to_mcps"
        for w in _would_have_passed(result)
    )


def _would_have_passed(result):
    return [
        p for p in result["parameters"]
        if str(p.get("outcome_under_global_scope", "")).startswith("ADMITTED")
        and not p["outcome"].startswith("ADMITTED")
    ]


def test_admission_uses_own_stage_even_when_that_makes_it_pass():
    """成對：自己階段的通道夠大時就必須 ADMIT，否則上一條會在
    「判定永遠拒絕」的情況下通過。"""
    from pcmef.experiments.calibration_stage0 import declared_observables

    own = declared_observables("AMBIENT")[0]
    result = _run_with_channels({"ambient_energy_to_mcps": {own: 245.0}})
    entry = next(
        p for p in result["parameters"] if p["dimension"] == "ambient_energy_to_mcps"
    )
    assert entry["max_leverage_declared"] == pytest.approx(100.0, rel=1e-3)
    assert entry["outcome"] == "ADMITTED"


def test_a_stage_without_declared_observables_is_refused_not_defaulted():
    """未知/未宣告的 stage 不得靜默落入某個預設範圍。"""
    with pytest.raises(Stage0Error, match="declares no observables"):
        run_stage0(
            resolved_bounds={"ambient_energy_to_mcps": (0.0, 1.0)},
            initial_values=dict(INITIAL),
            stage_parameters={"NOT_A_REAL_STAGE": ["ambient_energy_to_mcps"]},
            stage0_settings=dict(SETTINGS),
            evaluate=_make_evaluate({}),
        )


def test_a_clean_probe_completes():
    """沒有這一條，其餘負向測試會在「永遠要求裁決」的情況下全部通過。"""
    result = _run({"ambient_energy_to_mcps": 1000.0, "signal_energy_to_mcps": 1000.0})
    assert _outcome(result, "ambient_energy_to_mcps") == "ADMITTED"
    assert _outcome(result, "signal_energy_to_mcps") == "ADMITTED"


def test_a_parameter_the_optimizer_cannot_see_is_gauge_fixed():
    result = _run({"ambient_energy_to_mcps": 0.0, "signal_energy_to_mcps": 1000.0})
    assert _outcome(result, "ambient_energy_to_mcps") == "GAUGE_FIXED"


def test_a_parameter_inside_the_band_stops_the_flow():
    # sigma_MC 的樣本 SD 對 seed%8-3.5 序列恰為 2.4495；乘 10 落在 band 內。
    result = _run({"ambient_energy_to_mcps": 24.495, "signal_energy_to_mcps": 1000.0})
    assert _outcome(result, "ambient_energy_to_mcps") == "BORDERLINE"


def test_a_moving_observable_without_a_noise_reference_is_admitted_deterministically():
    """sigma_MC = 0 但該 observable 確實動了 -> 無雜訊卻會動，是最強的可辨識證據。

    重點在於它是**具名分支**而不是一個假造的 inf：artifact 裡看得到
    `DETERMINISTIC_RESPONSE`，也看得到 max_leverage 仍然是 None。
    """
    result = _run({"ambient_energy_to_mcps": 500.0}, noise=0.0)
    assert result["sigma_mc_degenerate"] == sorted(NAMES)
    assert _outcome(result, "ambient_energy_to_mcps") == "ADMITTED_DETERMINISTIC"
    entry = next(
        p for p in result["parameters"] if p["dimension"] == "ambient_energy_to_mcps"
    )
    moved = _own_channel("ambient_energy_to_mcps")
    assert entry["deterministic_response_observables"] == [moved]
    # 而且那個無雜訊卻會動的 observable 必須落在**自己階段宣告**的通道上，
    # 否則它不該讓參數入場（P0-1）。
    assert entry["deterministic_response_declared"] == [moved]
    assert entry["leverage_branch"][moved] == "DETERMINISTIC_RESPONSE"
    # 會動的那一個沒有數值槓桿（不得是 inf），其餘 observable 是 INERT，
    # 槓桿恰為 0 —— 因此 max 只會是 0.0，判定完全由具名分支決定。
    assert entry["leverage"][moved] is None
    assert entry["max_leverage"] == 0.0
    inert = next(n for n in NAMES if n != moved)
    assert entry["leverage_branch"][inert] == "INERT"
    assert entry["leverage"][inert] == 0.0


def test_a_parameter_nothing_responds_to_is_gauge_fixed_even_without_noise():
    """全部 observable 都 INERT -> 槓桿全為 0 -> GAUGE_FIXED，不是未定。"""
    result = _run({"ambient_energy_to_mcps": 0.0}, noise=0.0)
    assert _outcome(result, "ambient_energy_to_mcps") == "GAUGE_FIXED"


def test_an_infeasible_bound_yields_a_derived_domain_not_a_narrowed_range():
    """AMD-004：不可行的登記界線改由預註冊二分法導出可行子域。

    凍結的 registered range 必須原封不動地留在 artifact 裡 —— 導出的域是
    額外欄位，不是把界線改掉。
    """
    calls = {"n": 0}

    def evaluate(override, seed):
        calls["n"] += 1
        if override is not None and override["value"] > 0.6:
            raise RuntimeError("distance mapped to a negative value")
        base = 100.0 + (seed % 8)
        row = {name: base for name in NAMES}
        if override is not None:
            row[NAMES[0]] = base + 500.0 * override["value"]
        return row

    result = run_stage0(
        resolved_bounds=dict(BOUNDS),
        initial_values=dict(INITIAL),
        stage_parameters=dict(STAGES),
        stage0_settings=dict(SETTINGS),
        evaluate=evaluate,
    )
    for entry in result["parameters"]:
        assert entry["evaluation_status"] == "INFEASIBLE_BOUND"
        assert entry["infeasible_edges"] == ["high"]
        assert entry["low_status"] == "OK"
        assert entry["high_status"] == "FAILED"
        assert "negative value" in entry["error"]
        # 登記範圍原封不動。
        assert (entry["low"], entry["high"]) == BOUNDS[entry["dimension"]]
        # 導出的上緣落在可行側，且仍在登記範圍內。
        edge = entry["feasible_domain"]["high"]["edge"]
        assert 0.5 < edge <= 0.6
        assert entry["swept_high"] == edge
        # 導出之後參數仍然可以被正常分類，而不是卡在 UNDETERMINED。
        assert entry["outcome"] in {"ADMITTED", "BORDERLINE", "GAUGE_FIXED"}

    assert set(result["feasible_domains"]) == set(BOUNDS)


def test_an_infeasible_initial_value_stays_undetermined():
    """導不出可行域時必須維持未定，不得退回某個「差不多」的值。"""
    def evaluate(override, seed):
        if override is not None:
            raise RuntimeError("infeasible everywhere")
        return {name: 100.0 + (seed % 8) for name in NAMES}

    result = run_stage0(
        resolved_bounds=dict(BOUNDS),
        initial_values=dict(INITIAL),
        stage_parameters=dict(STAGES),
        stage0_settings=dict(SETTINGS),
        evaluate=evaluate,
    )
    for entry in result["parameters"]:
        assert entry["outcome"] == "UNDETERMINED"


def test_the_frozen_seed_set_may_not_be_substituted():
    settings = dict(SETTINGS)
    settings["seed_set"] = [1, 2, 3]
    with pytest.raises(Stage0Error, match="frozen seed set"):
        run_stage0(
            resolved_bounds=dict(BOUNDS),
            initial_values=dict(INITIAL),
            stage_parameters=dict(STAGES),
            stage0_settings=settings,
            evaluate=_make_evaluate({}),
        )


def test_an_unbound_dimension_is_refused_rather_than_skipped():
    with pytest.raises(Stage0Error, match="no binding for optimizer dimension"):
        run_stage0(
            resolved_bounds={"not_a_real_dimension": (0.0, 1.0)},
            initial_values={},
            stage_parameters={},
            stage0_settings=dict(SETTINGS),
            evaluate=_make_evaluate({}),
        )


# ---------------------------------------------------------------------------
# 封存前置
# ---------------------------------------------------------------------------


def test_freezing_a_result_that_needs_adjudication_is_refused(tmp_path):
    report = {
        "outcome": "STAGE0_ADJUDICATION_REQUIRED",
        "adjudication_blockers": ["2 dimension(s) landed inside the borderline band"],
    }
    with pytest.raises(Stage0FreezeError, match="STAGE0_ADJUDICATION_REQUIRED"):
        freeze_stage0(report, freeze_dir=tmp_path / "freeze", repo_root=REPO_ROOT)


def test_stage0_refuses_to_run_without_the_preregistration_it_implements(tmp_path):
    """程式實作 AMD-004 語意；沒有 CAL-PREREG-003 就不得產出 artifact。

    閘門必須在 import mitsuba **之前**觸發，否則在沒有 LLVM 的機器上，
    使用者看到的會是一則關於 drjit 後端的錯誤，而真正的原因是協定沒凍結。
    """
    from pcmef.experiments.calibration_stage0 import (
        IMPLEMENTS_PREREGISTRATION,
        execute_stage0,
    )

    (tmp_path / "preregistrations").mkdir(parents=True)
    (tmp_path / "preregistrations" / "CAL-PREREG-002.prereg.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(Stage0Error) as error:
        execute_stage0(freeze_dir=tmp_path, repo_root=REPO_ROOT)
    message = str(error.value)
    assert IMPLEMENTS_PREREGISTRATION in message
    assert "CAL-PREREG-002.prereg.json" in message
    # 不得變成 mitsuba/LLVM 的錯誤訊息。
    assert "mitsuba" not in message.lower()
    assert "llvm" not in message.lower()


def test_the_runner_names_the_preregistration_it_implements():
    """規則改了卻沒改這個常數，就會用新規則產出宣稱舊協定的 artifact。"""
    from pcmef.experiments.calibration_stage0 import IMPLEMENTS_PREREGISTRATION

    assert IMPLEMENTS_PREREGISTRATION.startswith("CAL-PREREG-")


def test_an_existing_stage0_record_is_never_overwritten(tmp_path):
    target = tmp_path / "freeze" / "stages" / "CAL-STAGE0-001.stage0.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(Stage0FreezeError, match="already frozen"):
        freeze_stage0(
            {"outcome": "STAGE0_COMPLETE"},
            freeze_dir=tmp_path / "freeze",
            repo_root=REPO_ROOT,
        )
