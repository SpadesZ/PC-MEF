# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.experiments.calibration_journal 與 calibration_formal 的
#         _Evaluator；不碰 calibration partition、不碰 held-out、不算圖。
#         由 pytest 收集執行，不寫出任何 repo 內的 artifact（只用 tmp_path）。
# 檔案路徑: tests/unit/test_calibration_resume.py
# 產生時間: 2026-08-30 20:20 +08:00
# 版本: v0.1.0
# 功能說明: 在一個小的合成問題上證明「中斷後接續」與「一次跑完」得到
#           **完全相同**的最終結果、相同的候選順序與相同的評估計數。
# 模組定位: formal calibration 耐久層的驗收。它不驗證校準的科學內容，
#           只驗證「接續不會偷偷變成另一個實驗」這一件事。
# 主要責任:
#   1. _synthetic_evaluator() 用假的 simulator/real 建出真正的 _Evaluator
#   2. test_resume_matches_uninterrupted_run() 中斷 vs 未中斷逐位元相同
#   3. test_replay_rejects_changed_candidate_ordering() 指紋不符即拒絕
#   4. test_checkpoint_rejects_identity_drift() 身分漂移即拒絕接續
#   5. test_vector_fingerprint_separates_adjacent_floats() 指紋不吞 ULP 差異
# 維護提醒:
#   - 不得把中斷點改成「剛好在一代邊界」讓測試變好過；中斷的意義就是它會
#     發生在任意一次評估之後。
#   - 不得放寬比對成 pytest.approx。接續若只是「差不多相同」，那就是另一次執行。
#   - 不得在本檔讀取任何真實資料；合成問題的用途正是把科學內容排除在外。
#   - v0.1.0 新增：首版接續等價性驗收（CAL-PREREG-003 formal calibration）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_calibration_resume.py -v
# ------------------------------------------------------------

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest
from scipy.optimize import differential_evolution

from pcmef.core.constants import CLASS_ORDER, TOF_SCHEMA
from pcmef.experiments.calibration_formal import _Evaluator
from pcmef.experiments.calibration_journal import (
    Checkpoint,
    EvaluationJournal,
    ResumeError,
    vector_fingerprint,
)
from pcmef.experiments.calibration_objective import RealCalibration
from pcmef.experiments.calibration_plan import EvaluationBudget

# 合成問題用真正的維度名，因為 _Evaluator 會依名稱把參數分到場景側或映射側。
DIMENSIONS = ["ambient_energy_to_mcps", "ambient_jitter_relative"]
BOUNDS = [(0.5, 4.0), (0.0, 0.5)]
CELLS = [(c, f) for c in CLASS_ORDER for f in TOF_SCHEMA]


class _StopHere(RuntimeError):
    """模擬一次崩潰／斷電。"""


@dataclass
class _FakeCache:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    capacity: int = 8

    def stats(self) -> dict[str, int]:
        return {"render_cache_hits": self.hits, "render_cache_misses": self.misses}


@dataclass
class _FakeIdentity:
    """只提供 _Evaluator 真正會讀到的欄位。"""

    s_f: dict[str, float] = field(
        default_factory=lambda: {f: 1.0 for f in TOF_SCHEMA}
    )
    initial_values: dict[str, Any] = field(
        default_factory=lambda: {"ambient_energy_to_mcps": 1.0,
                                 "ambient_jitter_relative": 0.05}
    )

    def identity_hash(self) -> str:
        return "synthetic-identity"

    def to_artifact(self) -> dict[str, Any]:
        return {"synthetic": True}


@dataclass
class _FakeSimulator:
    """決定性的假模擬：recording 只是參數的一個平滑函數。

    重點不是它像不像感測器，而是它**決定性** —— 接續等價性成立的前提就是
    同一個參數向量永遠給出同一個結果。
    """

    spp: int = 16
    resolution: tuple[int, int] = (8, 8)
    temporal_bins: int = 16
    n_samples: int = 32
    cache: _FakeCache = field(default_factory=_FakeCache)
    calls: int = 0

    def recordings(self, scene_values, surrogate_values, seeds):
        self.calls += 1
        gain = float(surrogate_values["ambient_energy_to_mcps"])
        jitter = float(surrogate_values["ambient_jitter_relative"])
        out = {}
        for index, class_label in enumerate(CLASS_ORDER):
            rng = np.random.default_rng(int(seeds[class_label]))
            base = rng.standard_normal((self.n_samples, len(TOF_SCHEMA)))
            # 目標函數的最小值落在 gain=2.0, jitter=0.2 附近，且處處有限。
            out[class_label] = (
                base * (0.1 + jitter) + gain * (1.0 + 0.05 * index)
            )
        return out


def _fake_real(n_samples: int = 32) -> RealCalibration:
    values = {}
    for index, class_label in enumerate(CLASS_ORDER):
        rng = np.random.default_rng(9000 + index)
        block = rng.standard_normal((n_samples * 4, len(TOF_SCHEMA))) * 0.3 + (
            2.0 * (1.0 + 0.05 * index)
        )
        values[class_label] = {
            feature: np.ascontiguousarray(block[:, i])
            for i, feature in enumerate(TOF_SCHEMA)
        }
    return RealCalibration(
        values=values,
        raw_hash="synthetic-raw-hash",
        recordings_per_class={c: 4 for c in CLASS_ORDER},
        ledger_entry={"index": 0, "purpose": "synthetic"},
    )


def _synthetic_evaluator(journal_path, resume: bool, budget_limit: int = 10_000):
    identity = _FakeIdentity()
    journal = EvaluationJournal.open(journal_path, resume=resume)
    budget = EvaluationBudget(stage_id="SYNTHETIC", limit=budget_limit)
    evaluator = _Evaluator(
        identity=identity,
        simulator=_FakeSimulator(),
        real=_fake_real(),
        stage_id="SYNTHETIC",
        dimensions=list(DIMENSIONS),
        declared_cells=list(CELLS),
        frozen_parameters={},
        journal=journal,
        budget=budget,
        bounds=dict(zip(DIMENSIONS, BOUNDS)),
    )
    return evaluator, journal, budget


def _solve(evaluator, maxiter: int, stop_after: int | None = None):
    """跑一次 DE；stop_after 不為 None 時在第 N 次評估後拋出模擬崩潰。"""
    calls = {"n": 0}

    def objective(x):
        if stop_after is not None and calls["n"] >= stop_after:
            raise _StopHere(f"simulated crash after {stop_after} evaluations")
        calls["n"] += 1
        return evaluator.evaluate(x)

    return differential_evolution(
        objective,
        BOUNDS,
        strategy="best1bin",
        maxiter=maxiter,
        popsize=15,
        tol=0.01,
        mutation=(0.5, 1.0),
        recombination=0.7,
        seed=20260829,
        polish=False,
        init="sobol",
        updating="deferred",
        workers=1,
        x0=np.array([1.0, 0.05]),
    )


# ---------------------------------------------------------------------------
# 核心驗收
# ---------------------------------------------------------------------------


def test_resume_matches_uninterrupted_run(tmp_path):
    """中斷 + 接續，必須與一次跑完得到完全相同的最終結果。"""
    maxiter = 12

    clean_evaluator, clean_journal, clean_budget = _synthetic_evaluator(
        tmp_path / "clean.jsonl", resume=False
    )
    clean_evaluator.journal.start_replay(0)
    clean = _solve(clean_evaluator, maxiter)
    clean_journal.close()

    # 在一個**非**世代邊界的位置中斷。
    interrupted_path = tmp_path / "interrupted.jsonl"
    evaluator, journal, _ = _synthetic_evaluator(interrupted_path, resume=False)
    journal.start_replay(0)
    with pytest.raises(_StopHere):
        _solve(evaluator, maxiter, stop_after=37)
    journal.close()
    completed = len(journal.records)
    assert completed == 37, "the journal must hold exactly the completed evaluations"

    resumed_evaluator, resumed_journal, resumed_budget = _synthetic_evaluator(
        interrupted_path, resume=True
    )
    resumed_evaluator.journal.start_replay(0)
    resumed = _solve(resumed_evaluator, maxiter)
    resumed_journal.close()

    assert resumed_evaluator.replay_hits == 37, "resume must replay, not recompute"
    assert list(resumed.x) == list(clean.x)
    assert resumed.fun == clean.fun
    assert resumed.nit == clean.nit
    assert resumed_budget.used == clean_budget.used
    assert resumed_budget.failed == clean_budget.failed

    # 逐位元比對整條軌跡：候選順序、目標值與 16 項全部相同。
    assert len(resumed_journal.records) == len(clean_journal.records)
    for left, right in zip(clean_journal.records, resumed_journal.records):
        assert left.parameter_fingerprint == right.parameter_fingerprint
        assert left.stage_objective == right.stage_objective
        assert left.objective_total == right.objective_total
        assert left.objective_terms == right.objective_terms
        assert left.simulation_run_identity_hash == right.simulation_run_identity_hash


def test_resume_does_not_rerun_completed_science(tmp_path):
    """接續時已完成的評估不得再次呼叫模擬器。"""
    path = tmp_path / "j.jsonl"
    evaluator, journal, _ = _synthetic_evaluator(path, resume=False)
    journal.start_replay(0)
    with pytest.raises(_StopHere):
        _solve(evaluator, 6, stop_after=20)
    journal.close()

    resumed, resumed_journal, _ = _synthetic_evaluator(path, resume=True)
    resumed.journal.start_replay(0)
    _solve(resumed, 6)
    resumed_journal.close()

    # 前 20 次是重播，模擬器只被真正呼叫「新的、而且不是重複候選的」那些次。
    assert resumed.replay_hits == 20
    assert resumed.simulator.calls == resumed.genuine - resumed.cache_hits
    assert resumed.simulator.calls < resumed.budget.used


def test_replay_rejects_changed_candidate_ordering(tmp_path):
    """指紋不符即 CHECKPOINT_IDENTITY_MISMATCH，不得繼續。"""
    path = tmp_path / "j.jsonl"
    evaluator, journal, _ = _synthetic_evaluator(path, resume=False)
    journal.start_replay(0)
    with pytest.raises(_StopHere):
        _solve(evaluator, 4, stop_after=10)
    journal.close()

    resumed, resumed_journal, _ = _synthetic_evaluator(path, resume=True)
    resumed.journal.start_replay(0)
    with pytest.raises(ResumeError) as excinfo:
        resumed.evaluate([3.14159, 0.271828])  # 不是原軌跡的第一個候選
    assert excinfo.value.reason == "CHECKPOINT_IDENTITY_MISMATCH"
    resumed_journal.close()


def test_vector_fingerprint_separates_adjacent_floats():
    """相差一個 ULP 的兩個向量必須有不同的指紋。"""
    a = [1.0, 0.05]
    b = [np.nextafter(1.0, 2.0), 0.05]
    assert vector_fingerprint(a) != vector_fingerprint(b)
    assert repr(a[0]) != repr(b[0]) or vector_fingerprint(a) != vector_fingerprint(b)


def test_truncated_final_line_is_dropped(tmp_path):
    """崩潰時寫了一半的最後一行不得被當成一次完成的評估。"""
    path = tmp_path / "j.jsonl"
    evaluator, journal, _ = _synthetic_evaluator(path, resume=False)
    journal.start_replay(0)
    with pytest.raises(_StopHere):
        _solve(evaluator, 4, stop_after=8)
    journal.close()

    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"evaluation_index": 9, "restart_ind')

    recovered = EvaluationJournal.open(path, resume=True)
    assert len(recovered.records) == 8
    assert path.read_text(encoding="utf-8").count("\n") == 8
    recovered.close()


# ---------------------------------------------------------------------------
# 檢查點層級
# ---------------------------------------------------------------------------


def _checkpoint(tmp_path, identity, config):
    return Checkpoint.create(tmp_path / "checkpoint.json", identity, "abc123", "v0.1.0", config)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda i, c: ({**i, "raw_calibration_data_hash": "different"}, c),
                     id="raw-data-hash"),
        pytest.param(lambda i, c: ({**i, "sf_hash": "different"}, c), id="sf-hash"),
        pytest.param(lambda i, c: (i, {**c, "spp": 32}), id="spp"),
        pytest.param(lambda i, c: (i, {**c, "maxiter": 10}), id="maxiter"),
        pytest.param(lambda i, c: (i, {**c, "restarts": 1}), id="restarts"),
    ],
)
def test_checkpoint_rejects_identity_drift(tmp_path, mutate):
    identity = {"raw_calibration_data_hash": "abc", "sf_hash": "def"}
    config = {"spp": 16, "maxiter": 100, "restarts": 3}
    checkpoint = _checkpoint(tmp_path, identity, config)

    drifted_identity, drifted_config = mutate(identity, config)
    with pytest.raises(ResumeError) as excinfo:
        checkpoint.assert_compatible(
            drifted_identity, "abc123", "v0.1.0", drifted_config
        )
    assert excinfo.value.reason == "CHECKPOINT_DRIFT"


def test_checkpoint_accepts_an_identical_run(tmp_path):
    identity = {"raw_calibration_data_hash": "abc"}
    config = {"spp": 16, "maxiter": 100, "restarts": 3}
    checkpoint = _checkpoint(tmp_path, identity, config)
    checkpoint.assert_compatible(identity, "abc123", "v0.1.0", config)

    reloaded = Checkpoint.load(tmp_path / "checkpoint.json")
    reloaded.assert_compatible(identity, "abc123", "v0.1.0", config)


def test_completed_stage_parameters_are_frozen(tmp_path):
    """階段完成後其參數進 frozen_parameters，後續階段只讀不改。"""
    checkpoint = _checkpoint(tmp_path, {"a": 1}, {"b": 2})
    checkpoint.complete_stage("STAGE_A", {"outcome": "CONVERGED"}, {"sensor.fov_deg": 33.5})
    assert checkpoint.frozen_parameters == {"sensor.fov_deg": 33.5}

    reloaded = Checkpoint.load(tmp_path / "checkpoint.json")
    assert reloaded.frozen_parameters == {"sensor.fov_deg": 33.5}
    assert reloaded.completed("STAGE_A")["outcome"] == "CONVERGED"
    assert reloaded.completed("STAGE_B") is None


def test_objective_is_deterministic_for_the_same_vector(tmp_path):
    """同一個向量必須給出同一個目標值；否則接續等價性不可能成立。"""
    evaluator, journal, _ = _synthetic_evaluator(tmp_path / "j.jsonl", resume=False)
    journal.start_replay(0)
    first = evaluator.evaluate([1.7, 0.11])
    evaluator.evaluation_cache.clear()  # 強迫真的重算，而不是命中快取
    second = evaluator.evaluate([1.7, 0.11])
    journal.close()
    assert first == second
    assert math.isfinite(first)
