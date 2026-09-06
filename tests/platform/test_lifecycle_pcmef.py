# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.lifecycle.pcmef 的階段歸屬。
#         以 tmp_path 造假 freeze/，**不讀真實 freeze/，不碰 families 36-43。**
# 檔案路徑: tests/platform/test_lifecycle_pcmef.py
# 產生時間: 2026-09-07 12:55 +08:00
# 版本: v0.1.0
# 功能說明: PC-MEF gate 掛在正確 lifecycle stage 的驗收。
# 模組定位: 平台化 Phase 4 的 PC-MEF adapter 驗收。重點在
#           「既有 gate 不得消失」與「Formal E2 不得被誤標成完成」。
# 主要責任:
#   1. 驗證 E1 十二道 gate 掛在 perception-validation
#   2. 驗證 final gate 八項掛在 pre-final-freeze
#   3. 驗證 Formal E2 執行本身不因前置條件通過而變 COMPLETE
#   4. 驗證未知 lock 名稱只降級單一 gate
#   5. 驗證 gate 標成 project_specific
# 維護提醒:
#   - 不得放寬 test_formal_execution_is_not_complete_just_because_gates_pass。
#     前置條件說的是「可以跑」，不是「跑完了」。
#   - 不得在本檔讀真實 freeze/ 或 final partition。
#   - v0.1.0 新增：首版，對應平台化 Phase 4。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_lifecycle_pcmef.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.platform.lifecycle.models import BLOCKED, COMPLETE, NOT_STARTED, PASS
from pcmef.platform.lifecycle.pcmef import STAGE_LOCKS, pcmef_lifecycle


class _Ctx:
    def __init__(self, freeze_dir, profile=None, gate_summary=None):
        self.freeze_dir = freeze_dir
        self.profile = profile
        self.gate_summary = gate_summary


class _Profile:
    def __init__(self, frozen=True):
        self.is_frozen = frozen
        self.state = "FROZEN" if frozen else "DRAFT"
        self.source_document = "PC-MEF_實驗計畫_v1.2.1"
        self.display_name = "Frozen Thesis Profile"


def _stage(view, stage_id):
    for stage in view.stages:
        if stage.stage_id == stage_id:
            return stage
    raise AssertionError(f"no stage {stage_id!r}")


@pytest.fixture()
def empty_freeze(tmp_path):
    d = tmp_path / "freeze"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# 階段歸屬
# ---------------------------------------------------------------------------


def test_the_lifecycle_uses_the_generic_stage_structure(empty_freeze):
    view = pcmef_lifecycle(_Ctx(empty_freeze))
    assert view.provider == "pcmef-thesis"
    assert [s.stage_id for s in view.stages][:3] == [
        "project-definition", "sensor-data-design", "calibration",
    ]


def test_the_research_design_gate_reflects_the_profile(empty_freeze):
    frozen = pcmef_lifecycle(_Ctx(empty_freeze, _Profile(True)))
    draft = pcmef_lifecycle(_Ctx(empty_freeze, _Profile(False)))

    assert _stage(frozen, "project-definition").status == COMPLETE
    assert _stage(draft, "project-definition").status == NOT_STARTED


def test_e1_gates_land_in_perception_validation(empty_freeze):
    summary = {"gates": [
        {"id": "G01", "status": "通過", "requirement": "r1"},
        {"id": "G02", "status": "不符", "requirement": "r2"},
        {"id": "G03", "status": "尚未產出", "requirement": "r3"},
    ]}
    view = pcmef_lifecycle(_Ctx(empty_freeze, gate_summary=summary))
    stage = _stage(view, "perception-validation")

    assert [g.gate_id for g in stage.gates] == ["E1-G01", "E1-G02", "E1-G03"]
    assert [g.status for g in stage.gates] == [PASS, BLOCKED, NOT_STARTED]
    assert stage.status == BLOCKED


def test_e1_gates_are_marked_project_specific(empty_freeze):
    """E1-G08 是 PC-MEF 自己的判準，不是平台共有的規矩。"""
    summary = {"gates": [{"id": "G08", "status": "通過", "requirement": "r"}]}
    view = pcmef_lifecycle(_Ctx(empty_freeze, gate_summary=summary))
    assert all(g.project_specific for g in _stage(view, "perception-validation").gates)


def test_final_gates_land_in_pre_final_freeze(empty_freeze):
    view = pcmef_lifecycle(_Ctx(empty_freeze))
    stage = _stage(view, "pre-final-freeze")
    assert stage.gates, "the eight Final E2 preconditions must be visible"


def test_formal_execution_is_not_complete_just_because_gates_pass(empty_freeze):
    """前置條件說的是「可以跑」，不是「跑完了」。

    這一格若因為 8/8 就變綠，讀者會以為正式實驗已經有結果。
    """
    stage = _stage(pcmef_lifecycle(_Ctx(empty_freeze)), "formal-experiment")
    assert stage.status == NOT_STARTED
    assert all(g.status == NOT_STARTED for g in stage.gates)


def test_lock_backed_stages_pass_when_the_locks_exist(tmp_path):
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    for name, _label in STAGE_LOCKS["decision-validation"]:
        (freeze / f"{name}.lock.json").write_text("{}", encoding="utf-8")

    view = pcmef_lifecycle(_Ctx(freeze))
    assert _stage(view, "decision-validation").status == COMPLETE


def test_missing_locks_leave_the_stage_not_started(empty_freeze):
    view = pcmef_lifecycle(_Ctx(empty_freeze))
    assert _stage(view, "decision-validation").status == NOT_STARTED


def test_an_unknown_lock_name_degrades_only_its_own_gate(tmp_path, monkeypatch):
    """未註冊的 lock 名稱不得讓整個 Status 消失。"""
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    monkeypatch.setitem(
        STAGE_LOCKS, "calibration", (("not_a_real_lock", "不存在的 lock"),)
    )
    view = pcmef_lifecycle(_Ctx(freeze))
    stage = _stage(view, "calibration")

    assert len(view.stages) == 8, "the whole lifecycle must still render"
    assert stage.gates[0].status == NOT_STARTED
    assert "無法判定" in stage.gates[0].detail


def test_every_stage_is_present_even_when_nothing_is_frozen(empty_freeze):
    """看不到的階段等於不存在的階段。"""
    view = pcmef_lifecycle(_Ctx(empty_freeze))
    assert len(view.stages) == 8
