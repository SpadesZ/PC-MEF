# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.experiments.run_claim 的 CAS 與
#         stale 回收，包含**跨行程**的競態。以 tmp_path 上的 claim 檔為輸入。
#         不連線、不讀 dataset、不觸碰 families 36-43。
# 檔案路徑: tests/e2/test_claim_concurrency.py
# 產生時間: 2026-09-05 20:10 +08:00
# 版本: v0.1.0
# 功能說明: resume 也必須是原子的，而且要在**真的**跨行程時成立。
# 模組定位: NOTE-084 / NOTE-085 的可執行防線。`reserve()` 建立 claim 走
#           O_EXCL，但 INTERRUPTED → RESERVED 那一段先前是 read-then-write：
#           兩個行程可以同時讀到同一份 INTERRUPTED、各自算出 resume_count+1、
#           各自覆寫，兩邊都以為自己接手了那場一次性實驗。
# 主要責任:
#   1. test_eight_concurrent_resumes_exactly_one_wins（thread）
#   2. test_two_subprocesses_racing_to_resume_only_one_wins（真行程）
#   3. test_two_subprocesses_racing_to_reserve_only_one_wins
#   4. test_every_transition_bumps_the_revision CAS 的前提
#   5. test_stale_running_can_be_reclaimed_without_deleting_the_claim
# 維護提醒:
#   - 不得把 subprocess 測試改成 thread。GIL 之下的 thread 競態比真行程弱；
#     這一組要證明的正是「兩個 python 行程同時跑」那一格。
#   - 不得為了讓測試好寫而提供「刪掉 claim」的 API。stale 回收只能推到
#     INTERRUPTED_RESUMABLE，identity 必須保留。
#   - v0.1.0 新增：首版，對應 P0-2 / P0-3。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_claim_concurrency.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pcmef.experiments.run_claim import (
    STATE_INTERRUPTED, STATE_RESERVED, STATE_RUNNING, ClaimError, RunIdentity,
    claim_path, mark_interrupted, mark_running, read_claim, reclaim_stale,
    reserve,
)

IDENTITY = RunIdentity(
    freeze_dir="freeze/runs/PFC-001",
    lock_hashes={"gate": "a" * 64},
    base_manifest_hash="b" * 64,
    code_version="c" * 40,
)

REPO = Path(__file__).resolve().parents[2]

#: 子行程要跑的東西。每個行程各自嘗試取得 claim，把結果印成一行 JSON。
_WORKER = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, {repo!r})
    from pcmef.experiments.run_claim import ClaimError, RunIdentity, reserve

    identity = RunIdentity(
        freeze_dir="freeze/runs/PFC-001",
        lock_hashes={{"gate": "a" * 64}},
        base_manifest_hash="b" * 64,
        code_version="c" * 40,
    )
    try:
        claim = reserve({root!r}, identity, resume={resume})
        print(json.dumps({{"won": True, "resume_count": claim.get("resume_count"),
                           "revision": claim.get("revision")}}))
    except ClaimError as error:
        print(json.dumps({{"won": False, "error": str(error)}}))
    """
)


def _race(root: Path, *, resume: bool, workers: int = 4) -> list[dict]:
    """同時啟動 N 個**真行程**去搶同一份 claim。"""
    script = _WORKER.format(repo=str(REPO), root=str(root), resume=resume)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO),
        )
        for _ in range(workers)
    ]
    outcomes = []
    for process in processes:
        out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
        outcomes.append(json.loads(out.strip().splitlines()[-1]))
    return outcomes


# ---------------------------------------------------------------------------
# resume 的競態
# ---------------------------------------------------------------------------


def test_eight_concurrent_resumes_exactly_one_wins(tmp_path):
    """八個同時 resume，只有一個成功。

    先前 INTERRUPTED → RESERVED 是 read-then-write：八個都會讀到同一份
    INTERRUPTED，各自算出 resume_count+1，各自覆寫 —— 八個都以為自己
    接手了那場一次性實驗（NOTE-084）。
    """
    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])
    mark_interrupted(tmp_path, claim["claim_id"], "boom")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(lambda: reserve(tmp_path, IDENTITY, resume=True))
            for _ in range(8)
        ]
        won, lost = [], []
        for future in futures:
            try:
                won.append(future.result())
            except ClaimError as error:
                lost.append(str(error))

    assert len(won) == 1, f"expected exactly one winner, got {len(won)}"
    assert len(lost) == 7
    # 而且檔案上的 resume_count 只加了一次。
    assert read_claim(tmp_path)["resume_count"] == 1


def test_two_subprocesses_racing_to_resume_only_one_wins(tmp_path):
    """跨**行程**的 resume 競態。GIL 之下的 thread 競態比真行程弱。"""
    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])
    mark_interrupted(tmp_path, claim["claim_id"], "boom")

    outcomes = _race(tmp_path, resume=True, workers=4)
    won = [o for o in outcomes if o["won"]]
    assert len(won) == 1, f"{len(won)} winners: {outcomes}"
    assert won[0]["resume_count"] == 1
    assert read_claim(tmp_path)["resume_count"] == 1


def test_two_subprocesses_racing_to_reserve_only_one_wins(tmp_path):
    """跨行程的**首次** reserve 競態。"""
    outcomes = _race(tmp_path, resume=False, workers=4)
    won = [o for o in outcomes if o["won"]]
    assert len(won) == 1, f"{len(won)} winners: {outcomes}"
    assert read_claim(tmp_path)["state"] == STATE_RESERVED


def test_a_fresh_subprocess_cannot_restart_an_interrupted_run(tmp_path):
    """跨行程也不得 fresh restart 一個已開封的 run。"""
    claim = reserve(tmp_path, IDENTITY)
    mark_interrupted(tmp_path, claim["claim_id"], "boom")

    outcomes = _race(tmp_path, resume=False, workers=2)
    assert not any(o["won"] for o in outcomes), outcomes
    assert all("fresh restart is refused" in o["error"] for o in outcomes)


# ---------------------------------------------------------------------------
# CAS 的前提
# ---------------------------------------------------------------------------


def test_every_transition_bumps_the_revision(tmp_path):
    """每一次狀態推進都必須贏得下一個 revision 的權杖。"""
    claim = reserve(tmp_path, IDENTITY)
    assert claim["revision"] == 0
    assert mark_running(tmp_path, claim["claim_id"])["revision"] == 1
    assert mark_interrupted(tmp_path, claim["claim_id"], "x")["revision"] == 2
    assert reserve(tmp_path, IDENTITY, resume=True)["revision"] == 3


def test_a_stale_read_cannot_overwrite_a_newer_claim(tmp_path):
    """拿著舊 revision 的人推不動已經被推進過的 claim。

    這是 CAS 的實質：不是「後寫的贏」，是「基於舊狀態的寫入會失敗」。
    """
    from pcmef.experiments.run_claim import _cas_advance

    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])          # revision 0 -> 1

    with pytest.raises(ClaimError, match="lost the race"):
        _cas_advance(claim_path(tmp_path), 0, {**claim, "state": "WHATEVER"})


# ---------------------------------------------------------------------------
# stale RUNNING 的回收
# ---------------------------------------------------------------------------


def test_a_live_running_claim_is_not_reclaimable(tmp_path):
    """持有者還活著就不得回收。這裡的持有者就是測試自己。"""
    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])
    with pytest.raises(ClaimError, match="refusing to reclaim"):
        reclaim_stale(tmp_path)


def test_process_liveness_distinguishes_unknown_from_dead():
    """分不出「已經死了」與「我不知道」是這一段最危險的錯誤。

    把不知道當成死了，就會把一個還在跑的正式執行判成可回收。
    """
    from pcmef.experiments.run_claim import _process_alive

    assert _process_alive(os.getpid()) is True
    # 非法 pid 無從判斷 —— 必須是 None，不是 False。
    assert _process_alive(0) is None
    assert _process_alive(-1) is None


def test_stale_running_can_be_reclaimed_without_deleting_the_claim(
    tmp_path, monkeypatch
):
    """持有者已死時自動判定 stale，且 claim **不被刪除**。

    刪 claim 是唯一絕對禁止的解法：它會讓下一次以為那是第一次，
    而 final partition 可能已經開封。

    存活判定用 monkeypatch 而不是真的製造一個死 pid：Windows 上
    `subprocess.Popen` 在 `wait()` 之後仍持有 handle，`OpenProcess` 對那個
    pid 照樣成功 —— 測試會變成在測平台的 pid 回收時機，而不是測回收邏輯。
    `_process_alive` 自己的契約由上一條測試守住。
    """
    from pcmef.experiments import run_claim

    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])
    monkeypatch.setattr(run_claim, "_process_alive", lambda _pid: False)

    after = reclaim_stale(tmp_path)
    assert after["state"] == STATE_INTERRUPTED
    assert after["reclaimed"]["automatic"] is True
    assert after["reclaimed"]["previous_state"] == STATE_RUNNING
    # claim 還在，claim_id 與 identity 都保留。
    assert claim_path(tmp_path).exists()
    assert after["claim_id"] == claim["claim_id"]
    assert after["identity"]["digest"] == IDENTITY.digest()

    # 回收之後只能以相同 identity 接續，不能重新開始。
    with pytest.raises(ClaimError, match="fresh restart is refused"):
        reserve(tmp_path, IDENTITY)
    assert reserve(tmp_path, IDENTITY, resume=True)["state"] == STATE_RESERVED


def test_forcing_a_reclaim_is_recorded(tmp_path):
    """`force` 不是後門：它留下痕跡。"""
    claim = reserve(tmp_path, IDENTITY)
    mark_running(tmp_path, claim["claim_id"])

    after = reclaim_stale(tmp_path, force=True, reason="機器斷電，已確認")
    assert after["reclaimed"]["forced"] is True
    assert after["reclaimed"]["automatic"] is False
    assert "機器斷電" in after["reclaimed"]["reason"]
    assert after["reclaimed"]["by_pid"] == os.getpid()


def test_a_complete_claim_is_not_reclaimable(tmp_path):
    from pcmef.experiments.run_claim import mark_complete

    claim = reserve(tmp_path, IDENTITY)
    mark_complete(tmp_path, claim["claim_id"])
    with pytest.raises(ClaimError, match="not a stuck run"):
        reclaim_stale(tmp_path, force=True)
