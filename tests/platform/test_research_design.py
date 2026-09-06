# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.profiles 的 design、models
#         與 thesis 映射。**純記憶體與 tmp_path，不碰真實 freeze/。**
# 檔案路徑: tests/platform/test_research_design.py
# 產生時間: 2026-09-06 22:10 +08:00
# 版本: v0.1.0
# 功能說明: Research Design 結構、Profile 狀態機，以及 PC-MEF_實驗計畫_v1.2.1
#           映射完整性的驗收。
# 模組定位: 平台化 Phase 3 的驗收。重點在「設計敘述不得成為科學參數的
#           第二來源」，以及「計畫書要求的每一節都在」。
# 主要責任:
#   1. 驗證 Profile 狀態機與 is_frozen 判定
#   2. 驗證 ResearchDesign 序列化往返與段落排序
#   3. 驗證 Thesis 設計涵蓋計畫書要求的所有面向
#   4. 驗證每個欄位都有 source（可稽核）
#   5. 驗證決策相關欄位標出 authoritative lock
# 維護提醒:
#   - 不得放寬 test_every_design_field_cites_its_source。沒有出處的
#     設計敘述無法被稽核，而無法稽核的敘述遲早與計畫書分歧。
#   - 不得把本檔改成比對科學數值是否「正確」。數值的可執行真相在
#     freeze/ 的 lock；這裡只驗證敘述層的結構與出處完整。
#   - v0.1.0 新增：首版，對應平台化 Phase 3。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_research_design.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.platform.profiles.design import (
    SECTION_ORDER,
    DesignField,
    DesignSection,
    ResearchDesign,
)
from pcmef.platform.profiles.models import PROFILE_STATES, Profile
from pcmef.platform.profiles.thesis import (
    SOURCE_DOCUMENT,
    THESIS_DESIGN,
    THESIS_TITLE,
)


# ---------------------------------------------------------------------------
# Profile 狀態機
# ---------------------------------------------------------------------------


def test_profile_states_follow_the_sai_state_machine():
    assert PROFILE_STATES[0] == "DRAFT"
    assert PROFILE_STATES.index("FROZEN") < PROFILE_STATES.index("FORMAL_READY")
    assert "RUN_INHIBITED" in PROFILE_STATES


def test_profile_rejects_an_unknown_state():
    with pytest.raises(ValueError, match="unknown profile state"):
        Profile(
            profile_id="p1", project_id="proj", display_name="P1", state="NOPE"
        )


@pytest.mark.parametrize(
    "state,frozen",
    [
        ("DRAFT", False),
        ("CONFIGURED", False),
        ("PILOT_READY", False),
        ("FREEZE_CANDIDATE", False),
        ("FROZEN", True),
        ("FORMAL_READY", True),
        ("FORMAL_RUNNING", True),
        ("FORMAL_COMPLETE", True),
    ],
)
def test_is_frozen_covers_every_state_after_freeze(state, frozen):
    """FROZEN 之後的狀態都在 FROZEN 之上推進，因此同樣禁止原地修改。"""
    profile = Profile(
        profile_id="p1", project_id="proj", display_name="P1", state=state
    )
    assert profile.is_frozen is frozen


def test_profile_survives_a_json_round_trip():
    original = Profile(
        profile_id="thesis-frozen",
        project_id="pcmef-thesis",
        display_name="Frozen Thesis Profile",
        version="v1.2.1",
        state="FROZEN",
        source_document=SOURCE_DOCUMENT,
    )
    assert Profile.from_json(original.to_json()) == original


# ---------------------------------------------------------------------------
# ResearchDesign 結構
# ---------------------------------------------------------------------------


def test_design_survives_a_json_round_trip():
    design = ResearchDesign(
        source_document="doc",
        title="t",
        sections=(
            DesignSection(
                key="objective", title="O", summary="s",
                fields=(DesignField("l", "v", "§1", "gate", "n"),),
            ),
        ),
    )
    assert ResearchDesign.from_json(design.to_json()) == design


def test_design_refuses_an_unknown_schema_version():
    with pytest.raises(ValueError, match="schema_version"):
        ResearchDesign.from_json({"schema_version": "research_design_v0"})


def test_unknown_sections_are_ordered_last_not_dropped():
    """丟掉未知段落會讓新增一段設計後畫面什麼都沒變。"""
    design = ResearchDesign(
        source_document="d", title="t",
        sections=(
            DesignSection(key="brand-new", title="New", summary=""),
            DesignSection(key="objective", title="O", summary=""),
        ),
    )
    assert [s.key for s in design.ordered_sections()] == ["objective", "brand-new"]


# ---------------------------------------------------------------------------
# v1.2.1 映射完整性
# ---------------------------------------------------------------------------


def test_the_thesis_design_names_its_source_document():
    assert THESIS_DESIGN.source_document == "PC-MEF_實驗計畫_v1.2.1"
    assert THESIS_DESIGN.title == THESIS_TITLE


@pytest.mark.parametrize("key", SECTION_ORDER)
def test_every_required_section_is_present(key):
    """使用者第三節列出的每一個面向都必須在設計裡。"""
    assert THESIS_DESIGN.section(key) is not None, f"missing design section {key!r}"


def test_every_design_field_cites_its_source():
    """沒有出處的設計敘述無法被稽核。"""
    missing = [
        f"{section.key}.{field.label}"
        for section in THESIS_DESIGN.sections
        for field in section.fields
        if not field.source
    ]
    assert not missing, f"design fields without a plan citation: {missing}"


def test_the_class_space_matches_the_plan():
    field = _find("class_space", "四類")
    for label in ("Empty", "Water-filled", "Bubbly", "Misty"):
        assert label in field.value


def test_the_primary_endpoint_is_worst_condition_macro_f1():
    field = _find("primary_endpoint", "Primary endpoint")
    assert "Worst-condition Macro-F1" in field.value
    assert field.authoritative == "statistics_config"


def test_the_four_benchmark_conditions_are_present():
    keys = {f.label for f in THESIS_DESIGN.section("conditions").fields}
    assert {"Clean", "Vision-degraded", "ToF-degraded", "Conflict-Stress"} <= keys


def test_the_e2_case_count_matches_the_plan():
    field = _find("experiments", "E2 案例數")
    assert "384" in field.value and "96" in field.value


def test_the_four_agent_roles_are_present():
    labels = " ".join(f.label for f in THESIS_DESIGN.section("agents").fields)
    for role in ("觀察角色", "物理角色", "視覺語意角色", "仲裁角色"):
        assert role in labels


def test_the_four_tof_channels_are_named():
    field = _find("modalities", "ToF 通道")
    for channel in ("Distance", "Signal Rate", "Ambient Rate", "Sigma-like"):
        assert channel in field.value


def test_routing_threshold_points_at_the_gate_lock():
    """畫面說 0.5，執行時讀 gate.lock —— 兩者必須看得出對應關係。"""
    field = _find("routing", "路由門檻")
    assert "0.5" in field.value
    assert field.authoritative == "gate"


def test_decision_relevant_fields_name_an_authoritative_lock():
    """routing 與 statistics 是決策層，其欄位必須指出可執行真相。"""
    for key in ("routing", "statistics"):
        unbound = [
            f.label
            for f in THESIS_DESIGN.section(key).fields
            if not f.authoritative
        ]
        assert not unbound, (
            f"{key} fields without an authoritative lock: {unbound}; a decision "
            "field that names no lock cannot be checked against what runs"
        )


def test_every_cited_lock_actually_exists_in_the_active_lineage():
    """設計頁上寫「可執行真相在 X.lock」，那個 lock 就必須真的存在。

    引用一個不存在的 lock 會讓畫面看起來很嚴謹而實際上在說謊 ——
    而且是最難發現的那一種：欄位有值、格式正確、指向空無。
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    cited = {
        field.authoritative
        for section in THESIS_DESIGN.sections
        for field in section.fields
        if field.authoritative
    }
    available = {
        path.name.replace(".lock.json", "")
        for path in (repo_root / "freeze").rglob("*.lock.json")
    }
    if not available:
        pytest.skip("no freeze/ locks in this checkout")

    missing = sorted(cited - available)
    assert not missing, (
        f"the research design cites locks that do not exist: {missing}. "
        "A citation pointing at nothing looks rigorous and says nothing."
    )


def test_the_claim_boundary_limits_e1_to_tof():
    field = _find("claim_boundary", "E1 結論範圍")
    assert "ToF" in field.value


def _find(section_key: str, label: str) -> DesignField:
    section = THESIS_DESIGN.section(section_key)
    assert section is not None, f"no section {section_key!r}"
    for field in section.fields:
        if field.label == label:
            return field
    raise AssertionError(f"no field {label!r} in section {section_key!r}")
