# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；掃描 pcmef/、tests/、configs/ 全部原始檔與 docs/NOTES.md；
#         結果為 CI 的檔頭與決策記錄稽核判定，不寫出任何 artifact。
# 檔案路徑: tests/test_repo_integrity.py
# 產生時間: 2026-08-25 22:40 +08:00
# 版本: v0.1.0
# 功能說明: 檢查每個原始檔的十欄位檔頭齊全且順序正確，並確認程式中的
#           NOTE(NOTE-NNN) 都能在 docs/NOTES.md 找到同號、五欄齊備的條目。
# 模組定位: 把「檔頭規範」與「禁止失效 NOTE 引用」變成擋得住的 CI 規則。
#           它不檢查檔頭內容寫得好不好，只檢查結構齊全與引用有效。
# 主要責任:
#   1. iter_source_files() 列出所有受管轄的原始檔
#   2. test_every_source_file_has_the_full_header() 驗證十欄位齊全且順序正確
#   3. test_header_file_path_matches_actual_location() 驗證檔案路徑欄不是複製貼上的舊值
#   4. test_verification_commands_point_at_real_targets() 驗證「驗證方式」指向真實存在的檔案
#   5. test_every_note_reference_has_a_matching_entry() 驗證 NOTE 引用不失效
#   6. test_every_note_entry_has_all_required_sections() 驗證每則 NOTE 五欄齊備
# 維護提醒:
#   - 不得為了讓測試通過而放寬欄位檢查；欄位缺漏要補檔頭，不是改測試。
#   - 不得在 docs/NOTES.md 重用已用過的 NOTE 編號，本檔會擋下重號。
#   - v0.1.0 新增：首版稽核，涵蓋十欄位檔頭與 NOTE 五欄。
# 驗證方式:
#   - py -3.10 -m pytest tests/test_repo_integrity.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTES_PATH = REPO_ROOT / "docs" / "NOTES.md"
PROJECT_BANNER = "PC-MEF Research System source maintenance contract"

# 十欄位固定順序（SRC: code-header-comment-spec）。
HEADER_FIELDS: tuple[str, ...] = (
    "上下游",
    "檔案路徑",
    "產生時間",
    "版本",
    "功能說明",
    "模組定位",
    "主要責任",
    "維護提醒",
    "驗證方式",
)

# NOTE 每則必備欄位（SRC: code-note-decision-record-spec）。
NOTE_SECTIONS: tuple[str, ...] = ("決策日期", "適用範圍", "決策", "原因", "驗證")

_PROHIBITION_WORDS = ("不得", "不可", "禁止", "不要")

_SCAN_DIRECTORIES = ("pcmef", "tests", "configs")
_SCAN_SUFFIXES = (".py", ".yaml")


def iter_source_files() -> list[Path]:
    """列出所有受檔頭規範管轄的原始檔。"""
    files: list[Path] = []
    for directory in _SCAN_DIRECTORIES:
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix in _SCAN_SUFFIXES and "__pycache__" not in path.parts:
                files.append(path)
    return files


def _header_lines(path: Path) -> list[str]:
    """取出檔案開頭的連續註解區塊。"""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#"):
            lines.append(raw)
            continue
        if not raw.strip() and not lines:
            continue
        break
    return lines


def _field_positions(header: list[str]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, line in enumerate(header):
        match = re.match(r"^#\s*([一-鿿]+)\s*:", line)
        if match and match.group(1) in HEADER_FIELDS:
            positions.setdefault(match.group(1), index)
    return positions


def _field_body(header: list[str], field: str) -> list[str]:
    """取出某欄位的內容（含其下方縮排的續行）。"""
    positions = _field_positions(header)
    if field not in positions:
        return []
    start = positions[field]
    body = [header[start]]
    for line in header[start + 1 :]:
        if re.match(r"^#\s*[一-鿿]+\s*:", line) or set(line) <= set("#- "):
            break
        body.append(line)
    return body


SOURCE_FILES = iter_source_files()


# ---------------------------------------------------------------------------
# 檔頭結構
# ---------------------------------------------------------------------------


def test_repository_has_source_files_to_audit():
    assert SOURCE_FILES, "no source files found; the audit would pass vacuously"


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=[p.relative_to(REPO_ROOT).as_posix() for p in SOURCE_FILES]
)
def test_every_source_file_has_the_full_header(path: Path):
    header = _header_lines(path)
    assert header, f"{path.relative_to(REPO_ROOT)} has no header comment block"

    assert PROJECT_BANNER in header[0], (
        f"{path.relative_to(REPO_ROOT)} must open with '# {PROJECT_BANNER}', "
        f"got {header[0]!r}"
    )

    positions = _field_positions(header)
    missing = [field for field in HEADER_FIELDS if field not in positions]
    assert not missing, (
        f"{path.relative_to(REPO_ROOT)} header is missing field(s): {missing}"
    )

    ordered = sorted(HEADER_FIELDS, key=lambda field: positions[field])
    assert ordered == list(HEADER_FIELDS), (
        f"{path.relative_to(REPO_ROOT)} header fields are out of order.\n"
        f"  expected: {list(HEADER_FIELDS)}\n"
        f"  actual:   {ordered}"
    )


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=[p.relative_to(REPO_ROOT).as_posix() for p in SOURCE_FILES]
)
def test_header_file_path_matches_actual_location(path: Path):
    """檔案路徑欄若是複製貼上的舊值，整份檔頭的可信度就沒了。"""
    body = _field_body(_header_lines(path), "檔案路徑")
    declared = body[0].split(":", 1)[1].strip() if body else ""
    assert declared == path.relative_to(REPO_ROOT).as_posix(), (
        f"{path.relative_to(REPO_ROOT)} declares 檔案路徑 {declared!r} "
        f"but actually lives at {path.relative_to(REPO_ROOT).as_posix()!r}"
    )


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=[p.relative_to(REPO_ROOT).as_posix() for p in SOURCE_FILES]
)
def test_responsibilities_are_numbered(path: Path):
    """主要責任必須編號列舉並點到具體函式，不能寫成一句散文。"""
    body = _field_body(_header_lines(path), "主要責任")
    numbered = [line for line in body[1:] if re.match(r"^#\s+\d+\.\s+\S", line)]
    assert numbered, (
        f"{path.relative_to(REPO_ROOT)} 主要責任 must be a numbered list "
        "pointing at concrete functions or constants"
    )


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=[p.relative_to(REPO_ROOT).as_posix() for p in SOURCE_FILES]
)
def test_maintenance_notes_contain_at_least_one_prohibition(path: Path):
    """維護提醒的價值在禁令與取捨理由，不是功能複述。"""
    body = "\n".join(_field_body(_header_lines(path), "維護提醒"))
    assert any(word in body for word in _PROHIBITION_WORDS), (
        f"{path.relative_to(REPO_ROOT)} 維護提醒 must state at least one prohibition "
        f"(one of {_PROHIBITION_WORDS})"
    )


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=[p.relative_to(REPO_ROOT).as_posix() for p in SOURCE_FILES]
)
def test_verification_commands_point_at_real_targets(path: Path):
    """驗證方式是唯一可被執行、可被打臉的一欄；指向不存在的檔案即失效。"""
    body = _field_body(_header_lines(path), "驗證方式")
    commands = [line for line in body[1:] if re.match(r"^#\s+-\s+\S", line)]
    assert commands, f"{path.relative_to(REPO_ROOT)} 驗證方式 lists no runnable command"

    referenced = []
    for command in commands:
        referenced.extend(re.findall(r"(?:tests|configs|schemas)/[\w/.\-]+", command))
    for target in referenced:
        candidate = REPO_ROOT / target.split("::")[0]
        assert candidate.exists(), (
            f"{path.relative_to(REPO_ROOT)} 驗證方式 references {target!r}, "
            "which does not exist"
        )


# ---------------------------------------------------------------------------
# NOTE 決策記錄
# ---------------------------------------------------------------------------


def _note_references() -> dict[str, list[str]]:
    """收集程式中所有 NOTE(NOTE-NNN) 引用。"""
    references: dict[str, list[str]] = {}
    for path in SOURCE_FILES:
        for number in re.findall(r"NOTE\((NOTE-\d{3})\)", path.read_text(encoding="utf-8")):
            references.setdefault(number, []).append(
                path.relative_to(REPO_ROOT).as_posix()
            )
    return references


def _note_entries() -> dict[str, str]:
    """解析 docs/NOTES.md 的每則條目內容。"""
    if not NOTES_PATH.exists():
        return {}
    text = NOTES_PATH.read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    matches = list(re.finditer(r"^##\s+(NOTE-\d{3})\b(.*)$", text, re.MULTILINE))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        entries[match.group(1)] = text[match.start() : end]
    return entries


def test_notes_file_exists_at_the_specified_location():
    assert NOTES_PATH.exists(), (
        "decision records must live at docs/NOTES.md so that NOTE references "
        "resolve to a single known location"
    )


def test_every_note_reference_has_a_matching_entry():
    """禁止失效 reference：程式引用的 NOTE 必須在 docs/NOTES.md 找得到同號條目。"""
    references = _note_references()
    entries = _note_entries()
    dangling = {
        number: sites for number, sites in references.items() if number not in entries
    }
    assert not dangling, (
        "NOTE references without a matching entry in docs/NOTES.md: "
        + "; ".join(f"{number} (cited in {', '.join(sites)})" for number, sites in dangling.items())
    )


def test_every_note_entry_has_all_required_sections():
    """一行摘要不算數：每則 NOTE 必須有決策日期／適用範圍／決策／原因／驗證。"""
    problems = []
    for number, body in sorted(_note_entries().items()):
        missing = [
            section
            for section in NOTE_SECTIONS
            if not re.search(rf"^\*\*{section}\*\*", body, re.MULTILINE)
        ]
        if missing:
            problems.append(f"{number} missing {missing}")
    assert not problems, "incomplete NOTE entries: " + "; ".join(problems)


def test_note_numbers_are_never_reused():
    text = NOTES_PATH.read_text(encoding="utf-8") if NOTES_PATH.exists() else ""
    numbers = re.findall(r"^##\s+(NOTE-\d{3})\b", text, re.MULTILINE)
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicates, f"NOTE numbers must never be reused: {duplicates}"


def test_note_entries_are_reachable_from_code_or_explicitly_standalone():
    """條目可以沒有程式引用（例如環境現況記錄），但必須顯式標示適用範圍。

    這條擋的是「寫了一則 NOTE 卻忘記在程式裡標記」的反向失聯。
    """
    references = _note_references()
    for number, body in sorted(_note_entries().items()):
        if number in references:
            continue
        scope = re.search(r"^\*\*適用範圍\*\*[：:]\s*(.+)$", body, re.MULTILINE)
        assert scope, f"{number} has no code reference and no 適用範圍 to justify it"
