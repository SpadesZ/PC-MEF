# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.templates 與 archive 語意。
#         全部在 tmp_path，**不碰真實 projects/ 或 freeze/**。
# 檔案路徑: tests/platform/test_templates.py
# 產生時間: 2026-09-07 19:40 +08:00
# 版本: v0.1.0
# 功能說明: Project template 的套用、Clone 分層與 Archive 語意驗收。
# 模組定位: 平台化 Phase 7 的驗收。重點在「template 只給結構」
#           與「archive 不刪除任何 artifact、不改 hash」。
# 主要責任:
#   1. 驗證 template 建立起始 Profile 並宣告為預設
#   2. 驗證 blank template 不含任何 PC-MEF 專屬內容
#   3. 驗證 Thesis 不在可建立的 template 清單裡
#   4. 驗證 archive 不動 artifact 也不改 hash
#   5. 驗證 archive 後仍可唯讀稽核
# 維護提醒:
#   - 不得讓 archive 刪除或搬移任何檔案。封存改變的是「能不能被開啟
#     與執行」，不是資料是否存在。
#   - 不得把 PC-MEF Thesis 加進可建立的 template。既有碩論是遷移而來
#     的唯一一個；再建一個會讓結果歸屬無法判定。
#   - v0.1.0 新增：首版，對應平台化 Phase 7。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_templates.py -v
# ------------------------------------------------------------

from __future__ import annotations

import hashlib

import pytest

from pcmef.platform.profiles.registry import ProfileRegistry
from pcmef.platform.projects.registry import ProjectRegistry
from pcmef.platform.projects.resolver import resolve_paths
from pcmef.platform.templates import (
    BLANK,
    BLANK_MULTIMODAL,
    TEMPLATES,
    apply_template,
    get_template,
    template_choices,
)


@pytest.fixture()
def registries(tmp_path):
    return ProjectRegistry(root=tmp_path), ProfileRegistry(root=tmp_path)


# ---------------------------------------------------------------------------
# template 套用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_id", [BLANK, BLANK_MULTIMODAL])
def test_a_template_creates_a_starter_profile(registries, template_id):
    projects, profiles = registries
    projects.create("demo", "Demo", template=template_id)
    apply_template(template_id, "demo", projects=projects, profiles=profiles)

    listed = profiles.list_profiles("demo")
    assert [p.profile_id for p in listed] == ["draft-v1"]
    assert listed[0].state == "DRAFT"


def test_the_starter_profile_is_declared_as_the_default(registries):
    """明確宣告，不是靠排序取第一筆。"""
    projects, profiles = registries
    projects.create("demo", "Demo", template=BLANK)
    apply_template(BLANK, "demo", projects=projects, profiles=profiles)

    assert projects.get("demo").default_profile_id == "draft-v1"
    assert profiles.default_profile("demo").profile_id == "draft-v1"


def test_the_starter_profile_has_no_research_design(registries):
    """template 給的是起點；設計要由該研究自己填。"""
    projects, profiles = registries
    projects.create("demo", "Demo", template=BLANK)
    apply_template(BLANK, "demo", projects=projects, profiles=profiles)

    assert profiles.read_design("demo", "draft-v1") is None


def test_an_unknown_template_creates_no_profile(registries):
    projects, profiles = registries
    projects.create("demo", "Demo", template="not-a-template")
    apply_template("not-a-template", "demo", projects=projects, profiles=profiles)
    assert profiles.list_profiles("demo") == []


def test_applying_a_template_twice_is_harmless(registries):
    projects, profiles = registries
    projects.create("demo", "Demo", template=BLANK)
    apply_template(BLANK, "demo", projects=projects, profiles=profiles)
    apply_template(BLANK, "demo", projects=projects, profiles=profiles)
    assert len(profiles.list_profiles("demo")) == 1


# ---------------------------------------------------------------------------
# template 內容
# ---------------------------------------------------------------------------


def test_the_thesis_is_not_an_offered_template():
    """既有碩論是遷移而來的唯一一個；要以它為起點應該 Clone。"""
    offered = {t.template_id for t in TEMPLATES}
    assert "pcmef-thesis" not in offered


def test_no_template_mentions_anything_project_specific():
    blob = " ".join(
        f"{t.template_id}{t.display_name}{t.description}"
        f"{t.starter_profile_name}" for t in TEMPLATES
    )
    for leaked in ("PC-MEF", "ToF", "RGB", "Macro-F1", "Water-filled",
                   "Sigma", "AMD-", "E1", "VL53L0X"):
        assert leaked not in blob, f"{leaked!r} leaked into a project template"


def test_template_choices_expose_what_the_form_needs():
    for choice in template_choices():
        assert choice["id"] and choice["name"] and choice["description"]


def test_get_template_returns_none_for_unknown_ids():
    assert get_template("nope") is None


# ---------------------------------------------------------------------------
# Archive 語意
# ---------------------------------------------------------------------------


def _hash_tree(root):
    digests = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[path.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def test_archiving_changes_no_artifact_and_no_hash(registries, tmp_path):
    """封存改變的是「能不能被開啟與執行」，不是資料是否存在。"""
    projects, _ = registries
    projects.create("demo", "Demo")
    paths = resolve_paths("demo", root=tmp_path)
    (paths.outputs / "result.json").write_text('{"m": 1}', encoding="utf-8")
    (paths.freeze / "gate.lock.json").write_text('{"frozen": true}', encoding="utf-8")

    before = _hash_tree(paths.outputs) | _hash_tree(paths.freeze)
    projects.archive("demo")
    after = _hash_tree(paths.outputs) | _hash_tree(paths.freeze)

    assert before == after, "archiving must not touch artifacts or their hashes"


def test_an_archived_project_is_still_readable(registries):
    """封存不是刪除；仍要能唯讀稽核。"""
    projects, profiles = registries
    projects.create("demo", "Demo", template=BLANK)
    apply_template(BLANK, "demo", projects=projects, profiles=profiles)
    projects.archive("demo")

    assert projects.get("demo").archived is True
    assert profiles.list_profiles("demo"), "profiles must remain readable"


def test_an_archived_project_is_hidden_from_the_default_list(registries):
    projects, _ = registries
    projects.create("demo", "Demo")
    projects.archive("demo")

    assert [p.project_id for p in projects.list_projects()] == []
    assert [p.project_id for p in projects.list_projects(include_archived=True)] == ["demo"]


def test_archive_is_reversible(registries):
    """單向的封存等同於遺失。"""
    projects, _ = registries
    projects.create("demo", "Demo")
    projects.archive("demo")
    assert projects.unarchive("demo").archived is False


# ---------------------------------------------------------------------------
# Transaction 語意 —— audit P1-3
# ---------------------------------------------------------------------------


def test_a_failed_default_declaration_discards_the_starter_profile(registries):
    """半完成比乾淨的失敗更難查。

    Profile 建了、預設沒宣告成功，畫面會顯示「有 Profile 但尚未選擇」，
    而使用者從沒做過那個選擇。
    """
    from unittest.mock import patch

    projects, profiles = registries
    projects.create("rollback-demo", "Rollback Demo", template=BLANK)

    with patch.object(
        ProjectRegistry, "set_default_profile", side_effect=OSError("io")
    ):
        with pytest.raises(OSError):
            apply_template(BLANK, "rollback-demo", projects=projects, profiles=profiles)

    assert profiles.list_profiles("rollback-demo") == [], (
        "the starter profile must be discarded when the transaction did not finish"
    )


def test_reapplying_after_a_partial_failure_succeeds(registries):
    """rollback 之後重試必須乾淨 —— 否則使用者永遠卡在半完成。"""
    from unittest.mock import patch

    projects, profiles = registries
    projects.create("retry-demo", "Retry Demo", template=BLANK)

    with patch.object(ProjectRegistry, "set_default_profile", side_effect=OSError("io")):
        with pytest.raises(OSError):
            apply_template(BLANK, "retry-demo", projects=projects, profiles=profiles)

    apply_template(BLANK, "retry-demo", projects=projects, profiles=profiles)
    assert projects.get("retry-demo").default_profile_id == "draft-v1"


def test_an_existing_starter_profile_still_gets_the_default_declared(registries):
    """重複套用時不重建，但要確保預設有指到它。"""
    projects, profiles = registries
    projects.create("dup-demo", "Dup Demo", template=BLANK)
    profiles.create("dup-demo", "draft-v1", "Pre-existing")

    apply_template(BLANK, "dup-demo", projects=projects, profiles=profiles)
    assert projects.get("dup-demo").default_profile_id == "draft-v1"
