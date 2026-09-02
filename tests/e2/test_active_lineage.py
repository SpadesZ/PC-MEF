# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.core.active_lineage 的解析與四種
#         拒絕路徑，並確認 repo 內實際的 freeze/ACTIVE_LINEAGE.json 指得對。
# 檔案路徑: tests/e2/test_active_lineage.py
# 產生時間: 2026-09-02 11:50 +08:00
# 版本: v0.1.0
# 功能說明: 確認「不知道該讀哪組 lock 的時候會拒絕」，而不是安靜地
#           回退到 superseded 的 freeze/ 根目錄。
# 模組定位: P0-1 的回歸測試，對應 SAI v0.6.0 ACC-FML-02
#           （wrong lineage -> refuse；FAIL 條件是 silent legacy fallback）。
# 主要責任:
#   1. test_resolves_* 驗證正常情況解析得出目錄與 lock 雜湊
#   2. test_refuses_* 四條：pointer 缺席／狀態非 ACTIVE／自我 supersede／
#      解析目標與呼叫端預期不符／lock 不齊
#   3. test_repo_pointer_* 驗證 repo 內的 pointer 指向 PFC-001 而非 freeze/
#   4. test_manifest_records_the_resolved_target_not_the_pointer
# 維護提醒:
#   - 不得把任一 test_refuses_* 改成「回傳預設值」。這些測試釘的就是
#     fail-closed 本身；讓它們通過的方式是修 resolver，不是放寬斷言。
#   - 不得移除 test_repo_pointer_does_not_name_the_superseded_root。
#     freeze/ 底下仍留著 parent lineage 的 22 個 lock，指錯不會有症狀。
#   - v0.1.0 新增：首版，對應 P0-1。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_active_lineage.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcmef.core.active_lineage import (
    ACTIVE_LINEAGE_FILENAME,
    ACTIVE_LINEAGE_SCHEMA,
    REQUIRED_FORMAL_LOCKS,
    ActiveLineageError,
    resolve_active_lineage,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 測試替身：一個最小但完整的 lineage
# ---------------------------------------------------------------------------


def _write_locks(directory: Path, names=REQUIRED_FORMAL_LOCKS) -> None:
    """寫出可通過 LockStore 完整性檢查的替身 lock。

    payload_hash 必須用 `hash_object` 真算：`load_hash()` 會重算並比對，
    填假值會在解析階段就被擋下 —— 那個檢查本身是對的，不該為測試繞過。
    """
    from pcmef.core.hash import hash_object

    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        payload = {"n": index}
        (directory / f"{name}.lock.json").write_text(
            json.dumps(
                {
                    "lock_type": name,
                    "version": 1,
                    "created_at": "2026-09-02T00:00:00+00:00",
                    "payload": payload,
                    "payload_hash": hash_object(payload),
                }
            ),
            encoding="utf-8",
        )


def _write_pointer(root: Path, **overrides) -> Path:
    document = {
        "schema_version": ACTIVE_LINEAGE_SCHEMA,
        "active_freeze_dir": (root / "runs" / "TEST-001").as_posix(),
        "status": "ACTIVE",
        "supersedes": [root.as_posix()],
        "reason": "test lineage",
    }
    document.update(overrides)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ACTIVE_LINEAGE_FILENAME
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture()
def lineage(tmp_path: Path) -> Path:
    root = tmp_path / "freeze"
    _write_locks(root)                       # parent lineage，仍在原地
    _write_locks(root / "runs" / "TEST-001")  # corrective lineage
    _write_pointer(root)
    return root


# ---------------------------------------------------------------------------
# 正常解析
# ---------------------------------------------------------------------------


def test_resolves_to_the_corrective_lineage_not_the_root(lineage: Path):
    resolved = resolve_active_lineage(lineage)
    assert resolved.freeze_dir == lineage / "runs" / "TEST-001"
    assert resolved.freeze_dir.as_posix() != lineage.as_posix()


def test_resolution_carries_every_required_lock_hash(lineage: Path):
    resolved = resolve_active_lineage(lineage)
    assert set(resolved.lock_hashes) == set(REQUIRED_FORMAL_LOCKS)
    assert all(resolved.lock_hashes.values())


def test_manifest_records_the_resolved_target_not_the_pointer(lineage: Path):
    """pointer 會移動，resolved 目標不會。run manifest 要記得住的是後者。"""
    manifest = resolve_active_lineage(lineage).to_manifest()
    assert manifest["resolved_freeze_dir"].endswith("runs/TEST-001")
    assert manifest["lock_hashes"]
    assert "resolver, not an identity" in manifest["note"]


def test_expected_freeze_dir_matching_the_pointer_is_accepted(lineage: Path):
    resolved = resolve_active_lineage(
        lineage, expected_freeze_dir=lineage / "runs" / "TEST-001"
    )
    assert resolved.status == "ACTIVE"


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------


def test_refuses_when_the_pointer_is_missing(tmp_path: Path):
    root = tmp_path / "freeze"
    _write_locks(root)
    with pytest.raises(ActiveLineageError) as error:
        resolve_active_lineage(root)
    assert "no active lineage declaration" in str(error.value)
    # 訊息必須說明為什麼沒有預設值，否則下一個人就會加回去。
    assert "default" in str(error.value)


def test_refuses_when_the_resolved_lineage_is_not_what_the_caller_expected(
    lineage: Path,
):
    with pytest.raises(ActiveLineageError) as error:
        resolve_active_lineage(
            lineage, expected_freeze_dir=lineage / "runs" / "PFC-999"
        )
    assert "lineage mismatch" in str(error.value)


def test_refuses_when_the_pointer_names_a_directory_it_also_supersedes(
    tmp_path: Path,
):
    """指向自己宣告已被取代的目錄 —— 兩者必有一個是錯的。"""
    root = tmp_path / "freeze"
    _write_locks(root)
    _write_pointer(
        root, active_freeze_dir=root.as_posix(), supersedes=[root.as_posix()]
    )
    with pytest.raises(ActiveLineageError) as error:
        resolve_active_lineage(root)
    assert "cannot supersede itself" in str(error.value)


def test_refuses_when_the_lineage_is_incomplete(tmp_path: Path):
    """少一個 lock 就拒絕：讀一半比完全不讀更危險。"""
    root = tmp_path / "freeze"
    _write_locks(root)
    _write_locks(root / "runs" / "TEST-001", names=REQUIRED_FORMAL_LOCKS[:-2])
    _write_pointer(root)
    with pytest.raises(ActiveLineageError) as error:
        resolve_active_lineage(root)
    assert "missing required" in str(error.value)


def test_refuses_when_status_is_not_active(lineage: Path):
    _write_pointer(
        lineage,
        active_freeze_dir=(lineage / "runs" / "TEST-001").as_posix(),
        status="RETIRED",
    )
    with pytest.raises(ActiveLineageError) as error:
        resolve_active_lineage(lineage)
    assert "status" in str(error.value)


def test_refuses_an_unknown_schema_version(lineage: Path):
    _write_pointer(
        lineage,
        active_freeze_dir=(lineage / "runs" / "TEST-001").as_posix(),
        schema_version="active_lineage_v99",
    )
    with pytest.raises(ActiveLineageError) as error:
        resolve_active_lineage(lineage)
    assert "schema_version" in str(error.value)


def test_refuses_when_the_target_directory_does_not_exist(lineage: Path):
    _write_pointer(
        lineage, active_freeze_dir=(lineage / "runs" / "GONE").as_posix()
    )
    with pytest.raises(ActiveLineageError) as error:
        resolve_active_lineage(lineage)
    assert "not a" in str(error.value)


# ---------------------------------------------------------------------------
# repo 內的實際 pointer
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (REPO_ROOT / "freeze" / ACTIVE_LINEAGE_FILENAME).exists(),
    reason="no active lineage declared in this checkout",
)
def test_repo_pointer_does_not_name_the_superseded_root():
    """freeze/ 底下仍留著 parent lineage 的 lock；指錯不會有任何症狀。"""
    resolved = resolve_active_lineage(REPO_ROOT / "freeze")
    assert resolved.freeze_dir.as_posix().endswith("runs/PFC-001")
    assert "freeze" in {Path(item).as_posix() for item in resolved.supersedes}


@pytest.mark.skipif(
    not (REPO_ROOT / "freeze" / ACTIVE_LINEAGE_FILENAME).exists(),
    reason="no active lineage declared in this checkout",
)
def test_repo_pointer_resolves_to_the_corrected_lock_hashes():
    """解析到的必須是 PFC-001 的更正值，不是 parent 的 superseded 值。"""
    hashes = resolve_active_lineage(REPO_ROOT / "freeze").lock_hashes
    # AMD-006 supersede 的三個 lock：值必須是新的那一組。
    assert hashes["reliability_final"].startswith("6e54e11b")
    assert hashes["gate"].startswith("4f043335")
    assert hashes["formal_config"].startswith("aec8e88a")
    # 明確不是 parent 的舊值。
    assert not hashes["reliability_final"].startswith("ebf4529a")
    assert not hashes["gate"].startswith("1ff2d667")
    assert not hashes["formal_config"].startswith("cefb453a")
