# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；驗證 platform.projects.resolver 與 models。
#         另掃描 pcmef/ 全部原始檔，確認 legacy 特例沒有外洩。
#         **不寫出任何 artifact，不碰真實 freeze/。**
# 檔案路徑: tests/platform/test_project_paths.py
# 產生時間: 2026-09-06 14:35 +08:00
# 版本: v0.1.0
# 功能說明: Project 路徑解析、namespace 隔離、project_id 驗證，
#           以及「legacy 特例只能住在 resolver」這條架構約束的守門測試。
# 模組定位: 平台化 Phase 2 的驗收。其中 test_legacy_special_case_lives_only_
#           in_the_resolver 是本輪最重要的一條：它把一個口頭約定
#           變成會失敗的測試。
# 主要責任:
#   1. 驗證 legacy Thesis Project 的科學資料仍解析到 repo 根目錄
#   2. 驗證一般專案全部收在 projects/<id>/ 底下
#   3. 驗證兩個專案不共用任何路徑
#   4. 驗證 project_id 拒絕路徑穿越與非法字元
#   5. 驗證 legacy 特例沒有散落到 resolver 以外
# 維護提醒:
#   - 不得為了讓新模組通過而放寬第 5 條。要用 legacy 身分的模組應
#     匯入 LEGACY_THESIS_PROJECT_ID，而不是自己寫字串或自己判路徑。
#   - 不得在本檔使用真實 repo 根目錄做寫入測試；一律用 tmp_path，
#     否則測試會在開發者的 freeze/ 底下留下垃圾。
#   - v0.1.0 新增：首版，對應平台化 Phase 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_project_paths.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pcmef.platform.projects.models import PROJECT_STATES, Project
from pcmef.platform.projects.resolver import (
    LEGACY_THESIS_PROJECT_ID,
    ProjectIdError,
    resolve_paths,
    validate_project_id,
)

PCMEF_ROOT = Path(__file__).resolve().parents[2] / "pcmef"
RESOLVER_RELATIVE = "pcmef/platform/projects/resolver.py"

#: ProjectPaths 上所有科學資料欄位。metadata_root 不在內：它對每個專案
#: 都是同一種形狀，legacy 也不例外。
DATA_FIELDS = ("freeze", "outputs", "configs", "datasets", "runs", "artifacts")


# ---------------------------------------------------------------------------
# legacy Thesis Project：科學資料不搬
# ---------------------------------------------------------------------------


def test_legacy_thesis_keeps_its_scientific_data_at_the_repo_root(tmp_path):
    """freeze/ outputs/ configs/ 必須仍解析到 workspace 根目錄。

    平台化不得為了整齊而搬動已凍結的 lock。
    """
    paths = resolve_paths(LEGACY_THESIS_PROJECT_ID, root=tmp_path)

    assert paths.freeze == tmp_path / "freeze"
    assert paths.outputs == tmp_path / "outputs"
    assert paths.configs == tmp_path / "configs"
    assert paths.datasets == tmp_path / "data"
    assert paths.runs == tmp_path / "outputs" / "console" / "runs"


def test_legacy_thesis_metadata_still_lives_under_projects(tmp_path):
    """唯一該例外的是科學資料的位置，不是專案 metadata 本身。"""
    paths = resolve_paths(LEGACY_THESIS_PROJECT_ID, root=tmp_path)
    assert paths.metadata_root == tmp_path / "projects" / LEGACY_THESIS_PROJECT_ID


def test_legacy_scientific_paths_are_not_under_the_projects_directory(tmp_path):
    """若 freeze 跑到 projects/ 底下，代表有人「順手整理」了來源樹。"""
    paths = resolve_paths(LEGACY_THESIS_PROJECT_ID, root=tmp_path)
    projects_dir = tmp_path / "projects"
    for field in DATA_FIELDS:
        path = getattr(paths, field)
        assert projects_dir not in path.parents, (
            f"legacy {field} resolved to {path}, which is inside projects/. "
            "The legacy project's scientific data must not be relocated."
        )


# ---------------------------------------------------------------------------
# 一般專案：全部收在自己的 namespace
# ---------------------------------------------------------------------------


def test_a_normal_project_keeps_everything_inside_its_own_directory(tmp_path):
    paths = resolve_paths("sand-extension", root=tmp_path)
    own_root = tmp_path / "projects" / "sand-extension"

    assert paths.metadata_root == own_root
    for field in DATA_FIELDS:
        path = getattr(paths, field)
        assert own_root in path.parents or path == own_root, (
            f"{field} resolved to {path}, which escapes the project directory"
        )


def test_two_projects_share_no_path_at_all(tmp_path):
    """namespace 隔離的最低要求：任何一個根目錄都不得重合。"""
    a = resolve_paths("project-alpha", root=tmp_path)
    b = resolve_paths("project-beta", root=tmp_path)

    a_paths = {getattr(a, f) for f in DATA_FIELDS} | {a.metadata_root}
    b_paths = {getattr(b, f) for f in DATA_FIELDS} | {b.metadata_root}
    assert not (a_paths & b_paths), f"projects share paths: {a_paths & b_paths}"


def test_a_normal_project_never_resolves_onto_the_legacy_freeze(tmp_path):
    """新專案讀到 PC-MEF 的 frozen lock 是本輪最該防的事。"""
    legacy = resolve_paths(LEGACY_THESIS_PROJECT_ID, root=tmp_path)
    other = resolve_paths("blank-demo", root=tmp_path)
    assert other.freeze != legacy.freeze
    assert legacy.freeze not in other.freeze.parents


# ---------------------------------------------------------------------------
# project_id 驗證
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "..",
        "../escape",
        "a/b",
        "a\\b",
        "/absolute",
        "UPPER",
        ".hidden",
        "",
        "1leading-digit",
        "-leading-hyphen",
        "with space",
        "with_underscore",
    ],
)
def test_illegal_project_ids_are_refused_not_sanitised(bad):
    with pytest.raises(ProjectIdError):
        validate_project_id(bad)


@pytest.mark.parametrize("good", ["pcmef-thesis", "sand-extension", "p1", "a-1-b"])
def test_legal_project_ids_are_accepted(good):
    assert validate_project_id(good) == good


def test_path_traversal_cannot_escape_the_workspace(tmp_path):
    """即使有人繞過驗證，解析結果也不該落在 workspace 之外。"""
    with pytest.raises(ProjectIdError):
        resolve_paths("../../etc", root=tmp_path)


# ---------------------------------------------------------------------------
# 架構約束：legacy 特例只能住在 resolver
# ---------------------------------------------------------------------------


def _python_sources() -> list[Path]:
    return [
        p
        for p in sorted(PCMEF_ROOT.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


def test_legacy_special_case_lives_only_in_the_resolver():
    """`pcmef-thesis` 這個字串只能出現在 resolver。

    其他模組要用 legacy 身分，必須匯入 LEGACY_THESIS_PROJECT_ID。
    散落的字面值會讓「例外」變成一組沒有人數得清的判斷。
    """
    repo_root = PCMEF_ROOT.parent
    offenders = []
    for path in _python_sources():
        relative = path.relative_to(repo_root).as_posix()
        if relative == RESOLVER_RELATIVE:
            continue
        if f'"{LEGACY_THESIS_PROJECT_ID}"' in path.read_text(encoding="utf-8"):
            offenders.append(relative)
    assert not offenders, (
        f"the literal {LEGACY_THESIS_PROJECT_ID!r} appears outside "
        f"{RESOLVER_RELATIVE}: {offenders}. Import LEGACY_THESIS_PROJECT_ID instead."
    )


def test_no_module_branches_on_the_legacy_project_id():
    """禁止 `== LEGACY_THESIS_PROJECT_ID` 這種散落的路徑判斷。

    比對常數比寫死字串好，但**在 resolver 之外比對**仍然是把
    「例外」複製出去。上層一律只拿 ProjectPaths。
    """
    repo_root = PCMEF_ROOT.parent
    pattern = re.compile(r"[!=]=\s*LEGACY_THESIS_PROJECT_ID|LEGACY_THESIS_PROJECT_ID\s*[!=]=")
    offenders = []
    for path in _python_sources():
        relative = path.relative_to(repo_root).as_posix()
        if relative == RESOLVER_RELATIVE:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(relative)
    assert not offenders, (
        f"modules compare against LEGACY_THESIS_PROJECT_ID outside "
        f"{RESOLVER_RELATIVE}: {offenders}. Ask the resolver for ProjectPaths instead."
    )


# ---------------------------------------------------------------------------
# Project metadata
# ---------------------------------------------------------------------------


def test_project_rejects_an_unknown_state():
    with pytest.raises(ValueError, match="unknown project state"):
        Project(project_id="p1", display_name="P1", template="blank", state="NOPE")


def test_project_survives_a_json_round_trip():
    original = Project(
        project_id="sand-extension",
        display_name="Sand Extension",
        template="blank-multimodal",
        state="CONFIGURED",
        created_at="2026-09-06T14:00:00+08:00",
        parent_project_id="pcmef-thesis",
        description="granular medium",
    )
    assert Project.from_json(original.to_json()) == original


def test_project_refuses_an_unknown_schema_version():
    with pytest.raises(ValueError, match="schema_version"):
        Project.from_json({"schema_version": "project_v0", "project_id": "p1"})


def test_project_states_follow_the_sai_state_machine():
    """狀態機順序是 SAI v0.6.0 §4.1 的，不得任意重排。"""
    assert PROJECT_STATES[0] == "DRAFT"
    assert PROJECT_STATES.index("FROZEN") < PROJECT_STATES.index("FORMAL_READY")
    assert "RUN_INHIBITED" in PROJECT_STATES
