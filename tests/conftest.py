# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 在 session 開始時自動載入，涵蓋 tests/ 底下每一個
#         測試。不被任何 production 模組匯入。
# 檔案路徑: tests/conftest.py
# 產生時間: 2026-09-07 11:05 +08:00
# 版本: v0.1.0
# 功能說明: 跑測試不得改到 repo 的科研輸出與專案 metadata —— 這裡在
#           session 前後各取一次指紋，變了就讓整輪失敗。
# 模組定位: Execution Layer Closure round 3 的第 6 項。
#           靜態掃描擋不住這一類污染：`tests/e2/test_artifact_isolation.py`
#           正當地拿 canonical 路徑做位址運算而從不寫入，而一個沿用
#           預設 `PCMEF_FORMAL_OUT` 的 fixture 會真的寫進去。能分辨
#           兩者的只有「跑完之後有沒有變」。
# 主要責任:
#   1. WATCHED_PATHS 列出不得被測試改動的位置
#   2. fingerprint() 以內容雜湊描述一組路徑的現況
#   3. repo_is_not_polluted 在 session 前後比對並在變動時失敗
# 維護提醒:
#   - **不得把這個守衛改成只警告。** 污染是靜默的：outputs/ 不進版控，
#     所以除了這裡沒有任何東西會告訴你它被寫過。
#   - 不得為了讓某個測試通過而把路徑從 WATCHED_PATHS 移走。要改的是
#     那個測試，讓它寫進 tmp_path。
#   - 不得改成只比對 mtime。內容相同而 mtime 變了不算污染，內容變了
#     而 mtime 沒變才是真正危險的那一種。
#   - v0.1.0 新增：首版，對應 round 3 的第 6 項。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_execution_hardening.py -v
# ------------------------------------------------------------

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 跑測試不得改動的位置。
#:
#: 兩者都被真的咬過：canonical Final E2 目錄收到過一個 fixture 寫的
#: 假 report；`projects/pcmef-thesis/project.json` 被遷移的能力補寫
#: 改過，而那是一個進版控的檔案。
WATCHED_PATHS: tuple[Path, ...] = (
    REPO_ROOT / "outputs" / "perception" / "e2_final",
    REPO_ROOT / "projects" / "pcmef-thesis" / "project.json",
    REPO_ROOT / "freeze",
)


def fingerprint(paths) -> str:
    """一組路徑的內容指紋。不存在的路徑也算進去（消失同樣是變動）。

    比對內容而不是 mtime：內容相同而 mtime 變了不算污染，內容變了
    而 mtime 沒變才是最難發現的那一種。
    """
    digest = hashlib.sha256()
    for root in sorted(Path(p) for p in paths):
        if root.is_dir():
            members = sorted(p for p in root.rglob("*") if p.is_file())
        elif root.is_file():
            members = [root]
        else:
            digest.update(f"{root.as_posix()}\0<missing>\0".encode("utf-8"))
            continue
        for member in members:
            digest.update(member.as_posix().encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(hashlib.sha256(member.read_bytes()).digest())
            except OSError as error:
                digest.update(f"<unreadable:{error.errno}>".encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


@pytest.fixture(scope="session", autouse=True)
def repo_is_not_polluted():
    """整輪測試不得改到 repo 的科研輸出與專案 metadata。

    autouse + session scope：這不是某個測試要記得啟用的東西。會忘記
    啟用的守衛，正好會在寫出污染的那個測試上被忘記。
    """
    # 逐路徑記錄，才能在失敗時指出**是哪一個**被動到。整組一個雜湊
    # 只能說「有東西變了」，而那句話幫不上任何忙。
    before = {path: fingerprint([path]) for path in WATCHED_PATHS}
    yield
    changed = [
        path.as_posix()
        for path in WATCHED_PATHS
        if fingerprint([path]) != before[path]
    ]
    if changed:
        raise AssertionError(
            "the test session modified the repository's scientific tree or "
            "project metadata. Tests must write into tmp_path only — pass "
            "workspace_root= and PCMEF_FORMAL_OUT to create_app(). "
            f"changed: {changed}"
        )
