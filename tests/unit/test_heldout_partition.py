# PC-MEF Research System source maintenance contract
# 上下游: 讀 pcmef.core.heldout_partition 與 repo 內已凍結的
#         freeze/heldout_partition.lock.json；只碰 recording ID，
#         **不讀任何 heldout 值**。由 pytest 收集執行；取用測試一律在
#         tmp_path 的假 repo 上進行，不動真實帳本。
# 檔案路徑: tests/unit/test_heldout_partition.py
# 產生時間: 2026-08-30 23:35 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 AMD-005 的 heldout 二次分割是決定性的、兩側互斥、計數正確，
#           且 FORMAL_E1_FINAL 在校準模擬器凍結之前真的打不開。
# 模組定位: protected-final-test 設計的驗收。它不驗證 E1 的結論，只驗證
#           「最終測試集在被讀之前就已經固定，而且現在還沒被讀」。
# 主要責任:
#   1. test_partition_is_deterministic() 同一份 registry 永遠給同一組分割
#   2. test_probe_and_final_are_disjoint_and_cover() 互斥且合併等於原集合
#   3. test_frozen_lock_matches_a_fresh_plan() 凍結內容與重算結果相同
#   4. test_final_ids_refuses_*() 未凍結／已開過一律拒絕
#   5. test_probe_ids_logs_every_access() 每次取用都留下一筆帳
# 維護提醒:
#   - 不得在本檔讀取任何一筆 heldout recording 的值；本模組的全部意義
#     就是它先於讀值。
#   - 不得為了讓測試好寫而放寬 final_ids() 的兩道前提；那兩道前提是
#     protected-final-test 唯一的實質保護。
#   - 不得在真實 repo 上呼叫 probe_ids() 或 final_ids()；那會動到真實帳本，
#     讓一次測試變成一次 access。
#   - v0.1.0 新增：AMD-005 分割驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_heldout_partition.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcmef.core.constants import CLASS_ORDER
from pcmef.core.hash import hash_object
from pcmef.core.heldout_partition import (
    FINAL_PER_CLASS,
    FINAL_ROLE,
    PARTITION_SEED,
    PROBE_PER_CLASS,
    PROBE_ROLE,
    HeldoutPartitionError,
    freeze_partition,
    load_partition,
    plan_partition,
    probe_access_count,
    probe_ids,
    final_ids,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_repo(tmp_path):
    """帶著 split_registry 的最小假 repo，讓取用測試不碰真實帳本。"""
    root = tmp_path / "repo"
    (root / "data" / "splits").mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "data" / "splits" / "split_registry.json",
        root / "data" / "splits" / "split_registry.json",
    )
    return root


# ---------------------------------------------------------------------------
# 分割本身
# ---------------------------------------------------------------------------


def test_partition_counts_match_amd_005():
    plan = plan_partition(REPO_ROOT)
    for class_label in CLASS_ORDER:
        assert len(plan["probe"][class_label]) == PROBE_PER_CLASS == 14
        assert len(plan["final"][class_label]) == FINAL_PER_CLASS == 28
    assert sum(len(v) for v in plan["probe"].values()) == 56
    assert sum(len(v) for v in plan["final"].values()) == 112


def test_partition_is_deterministic():
    """固定種子的意思是重算一次要得到同一組 ID，不是「大概同一組」。"""
    first = plan_partition(REPO_ROOT)
    second = plan_partition(REPO_ROOT)
    assert first == second
    assert hash_object(first) == hash_object(second)


def test_probe_and_final_are_disjoint_and_cover_the_heldout_set():
    plan = plan_partition(REPO_ROOT)
    probe = set().union(*plan["probe"].values())
    final = set().union(*plan["final"].values())
    assert not probe & final
    assert len(probe) == 56 and len(final) == 112

    registry = json.loads(
        (REPO_ROOT / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    heldout = {k for k, role in registry["assignments"].items() if role == "heldout_real"}
    assert probe | final == heldout


def test_partition_never_touches_calibration_ids():
    """分割只能在 heldout_real 裡切；碰到 calibration 就是切錯了集合。"""
    plan = plan_partition(REPO_ROOT)
    registry = json.loads(
        (REPO_ROOT / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    calibration = {
        k for k, role in registry["assignments"].items() if role == "calibration"
    }
    both = set().union(*plan["probe"].values()) | set().union(*plan["final"].values())
    assert not both & calibration


def test_partition_is_stratified_by_class():
    """每一類都必須貢獻 14/28，否則最終測試的 class 分佈是抽出來的。"""
    from pcmef.adapters.edge_impulse import EI_LABEL_TO_CLASS

    plan = plan_partition(REPO_ROOT)
    for role, ids_by_class in (("probe", plan["probe"]), ("final", plan["final"])):
        for class_label, ids in ids_by_class.items():
            for key in ids:
                assert EI_LABEL_TO_CLASS[key.split("/", 1)[0]] == class_label, (
                    f"{role}/{class_label} contains {key}, which is another class"
                )


# ---------------------------------------------------------------------------
# 凍結物
# ---------------------------------------------------------------------------


def test_frozen_lock_matches_a_fresh_plan():
    """凍結的 ID 必須就是重算得到的 ID，否則 lock 記的是別的東西。"""
    document = load_partition(REPO_ROOT / "freeze", REPO_ROOT)
    payload = document["payload"]
    plan = plan_partition(REPO_ROOT)
    for class_label in CLASS_ORDER:
        assert payload["ids"][PROBE_ROLE][class_label] == plan["probe"][class_label]
        assert payload["ids"][FINAL_ROLE][class_label] == plan["final"][class_label]
    assert payload["seed"] == PARTITION_SEED
    assert payload["frozen_before_any_heldout_value_was_read"] is True
    assert payload["heldout_access_count_at_freeze"] == 0


def test_frozen_lock_verifies_its_own_hash():
    document = load_partition(REPO_ROOT / "freeze", REPO_ROOT)
    assert hash_object(document["payload"]) == document["payload_hash"]


def test_load_rejects_an_edited_lock(tmp_path):
    freeze_dir = tmp_path / "freeze"
    freeze_dir.mkdir()
    document = json.loads(
        (REPO_ROOT / "freeze" / "heldout_partition.lock.json").read_text(
            encoding="utf-8"
        )
    )
    document["payload"]["ids"][FINAL_ROLE]["Empty"].pop()
    (freeze_dir / "heldout_partition.lock.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(HeldoutPartitionError) as excinfo:
        load_partition(freeze_dir, REPO_ROOT)
    assert excinfo.value.reason == "PARTITION_DRIFT"


def test_freeze_is_refused_when_already_frozen(fake_repo):
    with pytest.raises(HeldoutPartitionError) as excinfo:
        freeze_partition(REPO_ROOT / "freeze", fake_repo)
    assert excinfo.value.reason == "ALREADY_FROZEN"


def test_freeze_is_refused_after_any_heldout_value_was_read(fake_repo, tmp_path):
    """順序反了就沒有保護可言：先讀值再切，最終測試集就是挑出來的。"""
    registry_path = fake_repo / "data" / "splits" / "split_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["heldout_access_count"] = 1
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(HeldoutPartitionError) as excinfo:
        freeze_partition(tmp_path / "empty_freeze", fake_repo)
    assert excinfo.value.reason == "HELDOUT_ALREADY_READ"


# ---------------------------------------------------------------------------
# 取用
# ---------------------------------------------------------------------------


def test_probe_ids_logs_every_access(fake_repo):
    freeze_dir = fake_repo / "freeze"
    freeze_dir.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "freeze" / "heldout_partition.lock.json",
        freeze_dir / "heldout_partition.lock.json",
    )
    assert probe_access_count(fake_repo) == 0

    ids, entry = probe_ids("first sanity check", "abc123", freeze_dir, fake_repo)
    assert sum(len(v) for v in ids.values()) == 56
    assert entry["index"] == 1
    assert entry["reads_formal_e1_final"] is False
    assert probe_access_count(fake_repo) == 1

    _, second = probe_ids("second look", "abc123", freeze_dir, fake_repo)
    assert second["index"] == 2
    assert probe_access_count(fake_repo) == 2

    ledger = json.loads(
        (fake_repo / "data" / "splits" / "heldout_probe_access_ledger.json").read_text(
            encoding="utf-8"
        )
    )
    assert [e["purpose"] for e in ledger["entries"]] == ["first sanity check", "second look"]


def test_probe_access_does_not_touch_the_final_counter(fake_repo):
    """probe 記帳不得污染 heldout_access_count，否則那個數字就沒有意義了。"""
    freeze_dir = fake_repo / "freeze"
    freeze_dir.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "freeze" / "heldout_partition.lock.json",
        freeze_dir / "heldout_partition.lock.json",
    )
    probe_ids("sanity", "abc123", freeze_dir, fake_repo)
    registry = json.loads(
        (fake_repo / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["heldout_access_count"] == 0


def test_final_ids_refuses_before_the_simulator_is_frozen(fake_repo):
    freeze_dir = fake_repo / "freeze"
    freeze_dir.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "freeze" / "heldout_partition.lock.json",
        freeze_dir / "heldout_partition.lock.json",
    )
    with pytest.raises(HeldoutPartitionError) as excinfo:
        final_ids("E1 final", "deadbeef", "abc123", freeze_dir, fake_repo)
    assert excinfo.value.reason == "CALIBRATION_NOT_FROZEN"


def test_final_ids_refuses_a_wrong_calibrated_lock_hash(fake_repo):
    freeze_dir = fake_repo / "freeze"
    freeze_dir.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "freeze" / "heldout_partition.lock.json",
        freeze_dir / "heldout_partition.lock.json",
    )
    (freeze_dir / "calibrated_simulation.lock.json").write_text(
        json.dumps({"payload_hash": "the-real-one", "payload": {}}), encoding="utf-8"
    )
    with pytest.raises(HeldoutPartitionError) as excinfo:
        final_ids("E1 final", "a-different-one", "abc123", freeze_dir, fake_repo)
    assert excinfo.value.reason == "CALIBRATION_NOT_FROZEN"


def test_final_ids_opens_exactly_once(fake_repo):
    freeze_dir = fake_repo / "freeze"
    freeze_dir.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "freeze" / "heldout_partition.lock.json",
        freeze_dir / "heldout_partition.lock.json",
    )
    (freeze_dir / "calibrated_simulation.lock.json").write_text(
        json.dumps({"payload_hash": "locked", "payload": {}}), encoding="utf-8"
    )

    ids, entry = final_ids("E1 final", "locked", "abc123", freeze_dir, fake_repo)
    assert sum(len(v) for v in ids.values()) == 112
    assert entry["index"] == 1

    registry = json.loads(
        (fake_repo / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["heldout_access_count"] == 1

    with pytest.raises(HeldoutPartitionError) as excinfo:
        final_ids("E1 final again", "locked", "abc123", freeze_dir, fake_repo)
    assert excinfo.value.reason == "FINAL_ALREADY_OPENED"


def test_the_live_repository_has_not_opened_the_final_partition():
    """這條測試是 protected-final-test 這句話在 repo 裡的實際憑據。"""
    registry = json.loads(
        (REPO_ROOT / "data" / "splits" / "split_registry.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["heldout_access_count"] == 0
