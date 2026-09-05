# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.experiments.run_claim 的狀態機與
#         競態行為，以 tmp_path 上的 claim 檔為輸入。
#         不連線、不讀 dataset、不觸碰 families 36-43。
# 檔案路徑: tests/e2/test_run_claim.py
# 產生時間: 2026-09-05 15:40 +08:00
# 版本: v0.1.0
# 功能說明: Formal E2 的一次性是一個狀態機，不是「報告檔在不在」。
# 模組定位: NOTE-079 的可執行防線。以檔案存在推論 one-shot 有兩個洞：
#           一次跑到一半失敗的正式執行沒有報告（於是看起來還沒跑過），
#           而兩個同時發出的請求可以同時通過「檔案不存在」的檢查。
# 主要責任:
#   1. test_two_concurrent_requests_only_one_wins 競態只有一個贏
#   2. test_complete_is_permanently_closed COMPLETE 之後永久封閉
#   3. test_interrupted_refuses_fresh_restart 開封後不得重新開始
#   4. test_interrupted_resumes_only_under_the_same_identity
#   5. test_identity_ignores_run_local_noise identity 不含時間戳/pid
#   6. test_a_corrupt_claim_is_not_treated_as_free
# 維護提醒:
#   - 不得把 test_two_concurrent_requests_only_one_wins 改成序列化執行。
#     它要證明的正是「同時」那一格；序列化之後任何實作都會通過。
#   - 不得讓 COMPLETE 可以被降級或刪除。要重跑就是新的 lineage。
#   - v0.1.0 新增：首版，對應 P0-2 / NOTE-079。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_run_claim.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from pcmef.experiments.run_claim import (
    STATE_COMPLETE, STATE_INTERRUPTED, STATE_RESERVED, STATE_RUNNING,
    ClaimError, RunIdentity, claim_path, mark_complete, mark_interrupted,
    mark_running, read_claim, reserve,
)

IDENTITY = RunIdentity(
    freeze_dir="freeze/runs/PFC-001",
    lock_hashes={"gate": "a" * 64, "reliability_final": "b" * 64},
    base_manifest_hash="c" * 64,
    code_version="d" * 40,
)


# ---------------------------------------------------------------------------
# 競態
# ---------------------------------------------------------------------------


def test_two_concurrent_requests_only_one_wins(tmp_path):
    """兩個同時發出的請求，只有一個拿得到 claim。

    這是 O_EXCL 存在的全部理由。用「先檢查再建立」的話兩個都會通過檢查，
    而 formal run 是一次性的 —— 那時兩個行程會同時寫同一份正式結果。
    """
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(lambda: reserve(tmp_path, IDENTITY)) for _ in range(8)
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(("won", future.result()))
            except ClaimError as error:
                outcomes.append(("lost", str(error)))

    won = [o for o in outcomes if o[0] == "won"]
    assert len(won) == 1, f"expected exactly one winner, got {len(won)}"
    assert len(outcomes) == 8
    assert read_claim(tmp_path)["claim_id"] == won[0][1]["claim_id"]


def test_the_claim_file_is_created_exclusively(tmp_path):
    """claim 已存在時不得被覆寫，即使呼叫端沒有先讀。"""
    reserve(tmp_path, IDENTITY)
    with pytest.raises(ClaimError):
        reserve(tmp_path, IDENTITY)


# ---------------------------------------------------------------------------
# 狀態機
# ---------------------------------------------------------------------------


def test_the_happy_path_walks_reserved_running_complete(tmp_path):
    claim = reserve(tmp_path, IDENTITY)
    assert claim["state"] == STATE_RESERVED
    assert mark_running(tmp_path, claim["claim_id"])["state"] == STATE_RUNNING
    done = mark_complete(tmp_path, claim["claim_id"], "report.json")
    assert done["state"] == STATE_COMPLETE
    assert done["finished_at"]
    assert done["report_path"] == "report.json"


def test_complete_is_permanently_closed(tmp_path):
    """COMPLETE 之後任何請求都拿不到 claim —— 包括 resume。"""
    claim = reserve(tmp_path, IDENTITY)
    mark_complete(tmp_path, claim["claim_id"])
    for resume in (False, True):
        with pytest.raises(ClaimError, match="already COMPLETE"):
            reserve(tmp_path, IDENTITY, resume=resume)


def test_a_running_claim_blocks_a_second_request(tmp_path):
    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])
    with pytest.raises(ClaimError, match="already RUNNING"):
        reserve(tmp_path, IDENTITY)


def test_illegal_transitions_are_refused(tmp_path):
    claim = reserve(tmp_path, IDENTITY)
    mark_complete(tmp_path, claim["claim_id"])
    with pytest.raises(ClaimError, match="illegal transition"):
        mark_running(tmp_path, claim["claim_id"])


def test_another_runs_claim_id_cannot_advance_it(tmp_path):
    reserve(tmp_path, IDENTITY)
    with pytest.raises(ClaimError, match="claim id mismatch"):
        mark_running(tmp_path, "not-the-owner")


# ---------------------------------------------------------------------------
# 中斷與 resume
# ---------------------------------------------------------------------------


def test_interrupted_refuses_fresh_restart(tmp_path):
    """final partition 一旦開封就不得重新開始。"""
    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])
    mark_interrupted(tmp_path, claim["claim_id"], "provider timeout")

    with pytest.raises(ClaimError, match="fresh restart is refused"):
        reserve(tmp_path, IDENTITY)


def test_interrupted_resumes_under_the_same_identity(tmp_path):
    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])
    mark_interrupted(tmp_path, claim["claim_id"], "boom")

    resumed = reserve(tmp_path, IDENTITY, resume=True)
    assert resumed["state"] == STATE_RESERVED
    assert resumed["resume_count"] == 1
    # claim_id 不變：resume 是接續同一場實驗，不是開新的一場。
    assert resumed["claim_id"] == claim["claim_id"]


def test_resume_refuses_a_different_identity(tmp_path):
    """換了 lineage、資料或 code revision 就是另一場實驗，不得繼承 claim。"""
    claim = reserve(tmp_path, IDENTITY)
    mark_interrupted(tmp_path, claim["claim_id"], "boom")

    for changed in (
        RunIdentity("freeze/runs/PFC-002", IDENTITY.lock_hashes,
                    IDENTITY.base_manifest_hash, IDENTITY.code_version),
        RunIdentity(IDENTITY.freeze_dir, {"gate": "z" * 64},
                    IDENTITY.base_manifest_hash, IDENTITY.code_version),
        RunIdentity(IDENTITY.freeze_dir, IDENTITY.lock_hashes,
                    "z" * 64, IDENTITY.code_version),
        RunIdentity(IDENTITY.freeze_dir, IDENTITY.lock_hashes,
                    IDENTITY.base_manifest_hash, "z" * 40),
    ):
        with pytest.raises(ClaimError, match="different identity"):
            reserve(tmp_path, changed, resume=True)


def test_interrupt_does_not_delete_the_claim(tmp_path):
    """中斷不得把 claim 刪掉 —— 刪掉會讓下一次以為那是第一次。"""
    claim = reserve(tmp_path, IDENTITY)
    mark_interrupted(tmp_path, claim["claim_id"], "boom")
    assert claim_path(tmp_path).exists()
    assert read_claim(tmp_path)["state"] == STATE_INTERRUPTED


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_identity_ignores_run_local_noise(tmp_path):
    """identity 不得含 run_id、時間戳、pid 或主機名。

    含了的話每次 resume 都會被判成不同實驗，於是
    INTERRUPTED_RESUMABLE 這個狀態永遠無法被使用。
    """
    first = reserve(tmp_path, IDENTITY)
    recorded = first["identity"]
    assert set(recorded) == {
        "freeze_dir", "lock_hashes", "base_manifest_hash", "code_version", "digest",
    }
    # 同樣的四個要素、不同的行程 → 同一個 digest。
    assert RunIdentity(**{
        k: v for k, v in recorded.items() if k != "digest"
    }).digest() == recorded["digest"]


def test_lock_hash_order_does_not_change_the_identity():
    """lock_hashes 的插入順序不得影響 digest。"""
    a = RunIdentity("f", {"gate": "1", "reliability_final": "2"}, "b", "c")
    b = RunIdentity("f", {"reliability_final": "2", "gate": "1"}, "b", "c")
    assert a.digest() == b.digest()


# ---------------------------------------------------------------------------
# 壞掉的狀態
# ---------------------------------------------------------------------------


def test_a_corrupt_claim_is_not_treated_as_free(tmp_path):
    """讀不回來的 claim 不得被當成「沒有 claim」。

    那會讓一次壞掉的寫入變成「可以重新開始」，而 final partition 可能
    已經開封了。
    """
    claim_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    claim_path(tmp_path).write_text("{ not json", encoding="utf-8")

    assert read_claim(tmp_path)["state"] == "CORRUPT"
    with pytest.raises(ClaimError, match="unusable state"):
        reserve(tmp_path, IDENTITY)


def test_no_claim_reads_as_none(tmp_path):
    assert read_claim(tmp_path) is None


def test_advancing_without_a_claim_is_refused(tmp_path):
    with pytest.raises(ClaimError, match="no Formal E2 claim"):
        mark_running(tmp_path, "whatever")


def test_the_claim_records_who_holds_it(tmp_path):
    """畫面要說得出「是誰在什麼時候按的」，不是只說位置被佔用。"""
    claim = reserve(tmp_path, IDENTITY)
    assert claim["pid"] and claim["host"]
    assert claim["reserved_at"]
    stored = json.loads(claim_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["claim_id"] == claim["claim_id"]
