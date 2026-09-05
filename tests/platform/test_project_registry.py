# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.projects.registry。
#         全部在 tmp_path 上進行，**不碰真實 freeze/ 或 projects/**。
# 檔案路徑: tests/platform/test_project_registry.py
# 產生時間: 2026-09-06 15:05 +08:00
# 版本: v0.1.0
# 功能說明: 專案建立、清單、封存、Clone 與 legacy 遷移的行為驗收，
#           重點在「Clone 不得帶走科學結果」與「遷移不得動到科學資料」。
# 模組定位: 平台化 Phase 1 的驗收。SAI v0.6.0 §24 Clone workflow
#           與使用者第十一節「預設不要複製 frozen evidence」的守門測試。
# 主要責任:
#   1. 驗證 create() 的 O_EXCL 語意與目錄備妥
#   2. 驗證 list_projects() 的排序、封存過濾與壞紀錄處理
#   3. 驗證 clone() 複製設計但不複製 freeze/outputs/runs/artifacts
#   4. 驗證 ensure_legacy_thesis_project() 冪等且不建立科學資料目錄
#   5. 驗證 archive/unarchive 往返
# 維護提醒:
#   - 不得放寬 test_clone_never_copies_scientific_data。它擋的是
#     「兩個專案共用同一份 frozen evidence」，那會讓結果歸屬無法判定。
#   - 不得讓本檔在 repo 真實根目錄執行建立；一律傳 tmp_path 當 root。
#   - v0.1.0 新增：首版，對應平台化 Phase 1。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.platform.projects.registry import (
    CLONE_NEVER_COPY,
    PROJECT_FILENAME,
    ProjectExistsError,
    ProjectNotFoundError,
    ProjectRegistry,
)
from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID, resolve_paths


@pytest.fixture()
def registry(tmp_path):
    return ProjectRegistry(root=tmp_path)


# ---------------------------------------------------------------------------
# 建立
# ---------------------------------------------------------------------------


def test_create_writes_a_record_and_prepares_the_data_directories(registry, tmp_path):
    project = registry.create("sand-extension", "Sand Extension")

    assert project.project_id == "sand-extension"
    assert project.state == "DRAFT"
    assert project.created_at, "created_at must be stamped"

    paths = resolve_paths("sand-extension", root=tmp_path)
    assert (paths.metadata_root / PROJECT_FILENAME).is_file()
    for field in CLONE_NEVER_COPY:
        assert getattr(paths, field).is_dir(), f"{field} directory was not created"


def test_creating_the_same_project_twice_is_refused(registry):
    registry.create("duplicate-me", "First")
    with pytest.raises(ProjectExistsError, match="already exists"):
        registry.create("duplicate-me", "Second")


def test_the_second_create_does_not_overwrite_the_first(registry):
    registry.create("keep-me", "Original")
    with pytest.raises(ProjectExistsError):
        registry.create("keep-me", "Impostor")
    assert registry.get("keep-me").display_name == "Original"


def test_get_on_a_missing_project_raises(registry):
    with pytest.raises(ProjectNotFoundError):
        registry.get("never-created")


# ---------------------------------------------------------------------------
# 清單
# ---------------------------------------------------------------------------


def test_list_projects_hides_archived_by_default(registry):
    registry.create("visible-one", "Visible")
    registry.create("hidden-one", "Hidden")
    registry.archive("hidden-one")

    listed = [p.project_id for p in registry.list_projects()]
    assert listed == ["visible-one"]

    with_archived = [p.project_id for p in registry.list_projects(include_archived=True)]
    assert set(with_archived) == {"visible-one", "hidden-one"}


def test_list_projects_is_empty_before_anything_exists(registry):
    assert registry.list_projects() == []


def test_an_unreadable_record_is_surfaced_not_skipped(registry, tmp_path):
    """讀不出來的專案與不存在的專案是兩件事，畫面上必須分得出來。"""
    registry.create("broken-one", "Broken")
    record = resolve_paths("broken-one", root=tmp_path).metadata_root / PROJECT_FILENAME
    record.write_text("{ not json", encoding="utf-8")

    listed = registry.list_projects()
    assert len(listed) == 1
    assert "<unreadable>" in listed[0].display_name


def test_archive_and_unarchive_round_trip(registry):
    registry.create("toggle-me", "Toggle")
    assert registry.archive("toggle-me").archived is True
    assert registry.unarchive("toggle-me").archived is False


def test_set_state_rejects_an_unknown_state(registry):
    registry.create("state-me", "State")
    with pytest.raises(ValueError, match="unknown project state"):
        registry.set_state("state-me", "NOT_A_STATE")


# ---------------------------------------------------------------------------
# Clone —— 本節是 Phase 7 最重要的不變量
# ---------------------------------------------------------------------------


def test_clone_copies_the_design_but_not_the_results(registry, tmp_path):
    source = registry.create("origin-project", "Origin", template="blank-multimodal")
    source_paths = resolve_paths(source.project_id, root=tmp_path)

    # 設計：應被複製
    (source_paths.metadata_root / "research_design.json").write_text(
        json.dumps({"modalities": ["rgb", "tof"]}), encoding="utf-8"
    )
    # 結果：不得被複製
    (source_paths.outputs / "result.json").write_text('{"macro_f1": 0.9}', encoding="utf-8")
    (source_paths.freeze / "gate.lock.json").write_text('{"frozen": true}', encoding="utf-8")

    clone = registry.clone("origin-project", "cloned-project", "Cloned")
    clone_paths = resolve_paths(clone.project_id, root=tmp_path)

    assert (clone_paths.metadata_root / "research_design.json").is_file()
    assert json.loads(
        (clone_paths.metadata_root / "research_design.json").read_text(encoding="utf-8")
    ) == {"modalities": ["rgb", "tof"]}
    assert clone.template == "blank-multimodal"


def test_clone_never_copies_scientific_data(registry, tmp_path):
    """Clone 帶走 frozen evidence 會讓兩個專案共用同一份科學身分。"""
    registry.create("science-source", "Science Source")
    source_paths = resolve_paths("science-source", root=tmp_path)

    planted = {
        "freeze": "gate.lock.json",
        "outputs": "report.json",
        "runs": "run-001.json",
        "artifacts": "figure.png",
        "datasets": "manifest.json",
    }
    for field, filename in planted.items():
        (getattr(source_paths, field) / filename).write_text("payload", encoding="utf-8")

    registry.clone("science-source", "science-clone", "Science Clone")
    clone_paths = resolve_paths("science-clone", root=tmp_path)

    for field, filename in planted.items():
        leaked = getattr(clone_paths, field) / filename
        assert not leaked.exists(), (
            f"clone leaked {field}/{filename}; scientific results and frozen "
            "evidence must never be copied into a cloned project"
        )


def test_clone_resets_state_to_draft(registry):
    registry.create("frozen-source", "Frozen Source")
    registry.set_state("frozen-source", "FROZEN")

    clone = registry.clone("frozen-source", "fresh-clone", "Fresh Clone")
    assert clone.state == "DRAFT", (
        "a clone has no locks of its own; inheriting FROZEN would let an empty "
        "project claim a scientific identity it never established"
    )
    assert clone.parent_project_id == "frozen-source"


def test_clone_of_a_missing_source_raises(registry):
    with pytest.raises(ProjectNotFoundError):
        registry.clone("nonexistent", "target", "Target")


# ---------------------------------------------------------------------------
# legacy 遷移
# ---------------------------------------------------------------------------


def test_legacy_migration_writes_only_metadata(registry, tmp_path):
    """遷移不得建立或搬動任何科學資料目錄。"""
    project = registry.ensure_legacy_thesis_project()

    assert project.project_id == LEGACY_THESIS_PROJECT_ID
    assert project.legacy_layout is True

    paths = resolve_paths(LEGACY_THESIS_PROJECT_ID, root=tmp_path)
    assert (paths.metadata_root / PROJECT_FILENAME).is_file()
    # tmp_path 是空的 workspace：遷移不該在這裡憑空生出 freeze/ 或 outputs/
    assert not paths.freeze.exists(), "migration must not create freeze/"
    assert not paths.outputs.exists(), "migration must not create outputs/"


def test_legacy_migration_is_idempotent(registry):
    first = registry.ensure_legacy_thesis_project()
    second = registry.ensure_legacy_thesis_project()
    assert first == second


def test_legacy_migration_does_not_invent_a_scientific_state(registry):
    """科學進度由 freeze/ 的 lock 與 final_gate 推導，不寫死在 metadata。"""
    project = registry.ensure_legacy_thesis_project()
    assert project.state == "DRAFT", (
        "migration must not assert FROZEN/FORMAL_READY; that would create a "
        "second source of truth for scientific progress which will drift"
    )
