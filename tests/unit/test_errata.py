# PC-MEF Research System source maintenance contract
# 上下游: 驗證 pcmef.core.errata 與 pcmef.core.formal_loader 的契約；
#         多數案例使用 tmp_path 自建 lock，另有數條針對 repo 內真實的
#         freeze/initial_simulation.lock.json 與 ERR-001。
# 檔案路徑: tests/unit/test_errata.py
# 產生時間: 2026-08-29 11:35 +08:00
# 版本: v0.1.0
# 功能說明: 確認勘誤層只能更正 metadata，且必須綁定到一份確切的 lock 與
#           一份確切的證據；任何越界、失聯或竄改都必須讓載入失敗而不是
#           退回原值繼續跑。
# 模組定位: 勘誤機制的行為契約測試。它的重點**不是**證明 ERR-001 會過，
#           而是證明「一份能改參數的勘誤」永遠過不了 —— 少了這一組，
#           勘誤層就只是一個可以繞過 lock 不可覆寫的後門。
# 主要責任:
#   1. 禁區路徑（parameter / estimator / seed / scene / config）必須 FAIL
#   2. 禁區優先於白名單：把禁區路徑加進白名單仍必須 FAIL
#   3. 綁定錯誤的 lock hash、原值不符、證據竄改、證據脫鉤皆必須 FAIL
#   4. scientific_state_changed=True 必須 FAIL
#   5. append-only：重複凍結必須 FAIL；lock 檔本身不得被改動
#   6. 真實的 ERR-001 可通過完整驗證，且 lock 身分維持原雜湊
# 維護提醒:
#   - 不得刪除 test_a_valid_metadata_erratum_is_accepted：少了它，其餘負向
#     測試會在「驗證永遠拒絕一切」的情況下全部通過，等於沒有測到東西。
#   - 不得為了讓某條通過而放寬 FORBIDDEN_PATH_PREFIXES 或 CORRECTABLE_PATHS。
#   - 不得把針對真實 lock 的測試改成讀複本；它要驗的正是磁碟上那一份。
#   - v0.1.0 新增：對應 NOTE-040 / ERR-001。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_errata.py -v
#   - py -3.10 -m pcmef.cli erratum status
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcmef.core.errata import (
    CORRECTABLE_PATHS,
    Erratum,
    ErratumError,
    ErratumScopeError,
    ErratumStore,
    EvidenceRef,
    apply_errata,
    verify_erratum,
)
from pcmef.core.formal_loader import load_formal_lock
from pcmef.core.hash import hash_object
from pcmef.core.locks import LockStore

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 合成環境：一份最小但結構真實的 initial_simulation lock 與其證據
# ---------------------------------------------------------------------------


def _manifest() -> dict:
    return {
        "content_hash": "scene-hash-aaa",
        "run_identity_hash": "run-identity-bbb",
        "dependencies": {"mitransient": "1.3.0", "mitsuba": "3.8.0"},
        "scenarios": [
            {
                "scenario_id": "smoke_empty_1001",
                "scenario_hash": "scenario-hash-ccc",
                "transient": {"mitransient_version": "1.3.0"},
            }
        ],
    }


def _lock_payload() -> dict:
    return {
        "scene_hash": "scene-hash-aaa",
        "surrogate_hash": "surrogate-hash-ddd",
        "code_version": "0" * 40,
        "parameter_ranges": {"_FOIL_REFLECTANCE_940NM": [0.3, 0.98]},
        "parameter_set_hash": "param-hash-eee",
        "estimator": {"selected": "leading_edge", "detection_threshold_sigma": 5.0},
        "scene_topology": {
            "run_identity_hash": "run-identity-bbb",
            "scenarios": {"smoke_empty_1001": {"scenario_hash": "scenario-hash-ccc"}},
        },
        "environment": {"mitransient": "unavailable", "mitsuba": "3.8.0"},
    }


@pytest.fixture()
def world(tmp_path):
    """建立 lock + 證據檔，回傳 (freeze_dir, repo_root, payload, hash)。"""
    freeze_dir = tmp_path / "freeze"
    run_dir = tmp_path / "outputs" / "repro_a"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(_manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    store = LockStore(freeze_dir)
    store.write("real_split_policy", _real_split_stub())
    payload = _lock_payload()
    store.write("initial_simulation", payload)
    return freeze_dir, tmp_path, payload, store.load_hash("initial_simulation")


def _real_split_stub() -> dict:
    """initial_simulation 的前置 lock；內容與本測試無關，只為滿足順序契約。"""
    return {
        key: "n/a"
        for key in (
            "creation_phase",
            "split_unit",
            "stratification",
            "group_rule",
            "allocation",
            "minimum_per_class",
            "seed",
            "eligible_set_hash",
            "calibration_set_hash",
            "heldout_real_set_hash",
            "redraw_policy",
        )
    }


def _evidence(repo_root: Path, pointer: str = "dependencies.mitransient") -> EvidenceRef:
    from pcmef.core.hash import hash_file

    path = "outputs/repro_a/manifest.json"
    return EvidenceRef(
        path=path,
        sha256=hash_file(repo_root / path),
        json_pointer=pointer,
        binds={"content_hash": "scene_hash"},
    )


def _erratum(repo_root: Path, lock_hash: str, **overrides) -> Erratum:
    kwargs = dict(
        erratum_id="ERR-TEST",
        target_lock="initial_simulation",
        target_payload_hash=lock_hash,
        field_path="environment.mitransient",
        recorded_value="unavailable",
        corrected_value="1.3.0",
        defect_class="FREEZE_METADATA_CAPTURE_BUG",
        rationale="the freeze process probed versions in a process without set_variant",
        evidence=(_evidence(repo_root),),
        authority="researcher decision",
        scientific_state_changed=False,
    )
    kwargs.update(overrides)
    return Erratum(**kwargs)


# ---------------------------------------------------------------------------
# 正向：少了這一條，底下所有負向測試都可能在「永遠拒絕」下假性通過
# ---------------------------------------------------------------------------


def test_a_valid_metadata_erratum_is_accepted(world):
    freeze_dir, repo_root, payload, lock_hash = world
    summary = verify_erratum(
        _erratum(repo_root, lock_hash).payload(), payload, lock_hash, repo_root
    )
    assert summary["corrected_value"] == "1.3.0"
    assert summary["evidence_verified"][0]["bindings_checked"] == ["content_hash"]


def test_apply_errata_does_not_mutate_the_original_payload(world):
    freeze_dir, repo_root, payload, lock_hash = world
    corrected, _ = apply_errata(
        payload, lock_hash, [_erratum(repo_root, lock_hash).payload()], repo_root
    )
    assert corrected["environment"]["mitransient"] == "1.3.0"
    assert payload["environment"]["mitransient"] == "unavailable"
    # lock 身分不因勘誤而改變。
    assert hash_object(payload) == lock_hash


# ---------------------------------------------------------------------------
# 範疇：勘誤不得碰科學內容
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_path,new_value",
    [
        ("parameter_ranges._FOIL_REFLECTANCE_940NM", [0.8, 0.9]),
        ("parameter_set_hash", "another-param-hash"),
        ("estimator.selected", "peak"),
        ("estimator.detection_threshold_sigma", 3.0),
        ("scene_hash", "another-scene-hash"),
        ("scene_topology.run_identity_hash", "another-run"),
        ("surrogate_hash", "another-surrogate"),
        ("code_version", "1" * 40),
    ],
)
def test_correcting_scientific_content_is_refused(world, field_path, new_value):
    """parameter / estimator / seed / scene / config 一律不得由勘誤更改。"""
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(
        repo_root,
        lock_hash,
        field_path=field_path,
        recorded_value=None,
        corrected_value=new_value,
    )
    with pytest.raises(ErratumScopeError):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_forbidden_region_beats_the_allowlist(world, monkeypatch):
    """把禁區路徑加進白名單**不足以**放行。

    兩層檢查是刻意的：放寬白名單是一行改動，那一行不該足以讓
    「改參數」偽裝成「修筆誤」。
    """
    freeze_dir, repo_root, payload, lock_hash = world
    monkeypatch.setattr(
        "pcmef.core.errata.CORRECTABLE_PATHS",
        CORRECTABLE_PATHS | {"parameter_set_hash"},
    )
    erratum = _erratum(
        repo_root,
        lock_hash,
        field_path="parameter_set_hash",
        recorded_value="param-hash-eee",
        corrected_value="param-hash-fff",
    )
    with pytest.raises(ErratumScopeError, match="forbidden region"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_unlisted_metadata_path_is_refused(world):
    """白名單之外的欄位也不行，即使它不在禁區內。"""
    freeze_dir, repo_root, payload, lock_hash = world
    payload["notes"] = "typo"
    erratum = _erratum(
        repo_root, lock_hash, field_path="notes", recorded_value="typo",
        corrected_value="fixed",
    )
    with pytest.raises(ErratumScopeError, match="CORRECTABLE_PATHS"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_scientific_state_changed_true_is_refused(world):
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(repo_root, lock_hash, scientific_state_changed=True)
    with pytest.raises(ErratumScopeError, match="not an erratum"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


# ---------------------------------------------------------------------------
# 綁定：勘誤必須指名它更正的是哪一份文件
# ---------------------------------------------------------------------------


def test_erratum_bound_to_a_different_lock_hash_is_refused(world):
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(repo_root, lock_hash, target_payload_hash="f" * 64)
    with pytest.raises(ErratumError, match="targets lock payload_hash"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_recorded_value_must_match_the_lock(world):
    """勘誤宣稱的原值若與 lock 不符，它描述的就不是這份文件。"""
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(repo_root, lock_hash, recorded_value="1.2.9")
    with pytest.raises(ErratumError, match="actually records"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_a_correction_that_changes_nothing_is_refused(world):
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(repo_root, lock_hash, corrected_value="unavailable")
    with pytest.raises(ErratumError, match="already has"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


# ---------------------------------------------------------------------------
# 證據
# ---------------------------------------------------------------------------


def test_missing_evidence_is_refused(world):
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(repo_root, lock_hash, evidence=())
    with pytest.raises(ErratumError, match="carries no evidence"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_tampered_evidence_file_is_refused(world):
    """證據檔在勘誤凍結後被改動，載入必須失敗而不是沿用舊結論。"""
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(repo_root, lock_hash)
    manifest_path = repo_root / "outputs" / "repro_a" / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["dependencies"]["mitransient"] = "9.9.9"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ErratumError, match="hashes to"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_evidence_that_does_not_state_the_corrected_value_is_refused(world):
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(
        repo_root, lock_hash, evidence=(_evidence(repo_root, "dependencies.mitsuba"),)
    )
    with pytest.raises(ErratumError, match="asserts the corrected value"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_evidence_from_a_different_run_is_refused(world):
    """binds 是「這份證據來自被凍結的那次 run」的憑據，不得省略或造假。"""
    freeze_dir, repo_root, payload, lock_hash = world
    other = repo_root / "outputs" / "other"
    other.mkdir(parents=True)
    manifest = _manifest()
    manifest["content_hash"] = "a-different-scene-hash"
    (other / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    from pcmef.core.hash import hash_file

    ref = EvidenceRef(
        path="outputs/other/manifest.json",
        sha256=hash_file(other / "manifest.json"),
        json_pointer="dependencies.mitransient",
        binds={"content_hash": "scene_hash"},
    )
    erratum = _erratum(repo_root, lock_hash, evidence=(ref,))
    with pytest.raises(ErratumError, match="does not come"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


def test_evidence_pointer_that_does_not_resolve_is_refused(world):
    freeze_dir, repo_root, payload, lock_hash = world
    erratum = _erratum(
        repo_root, lock_hash, evidence=(_evidence(repo_root, "dependencies.nonexistent"),)
    )
    with pytest.raises(ErratumError, match="does not resolve"):
        verify_erratum(erratum.payload(), payload, lock_hash, repo_root)


# ---------------------------------------------------------------------------
# append-only 與 store
# ---------------------------------------------------------------------------


def test_refreezing_the_same_erratum_id_is_refused(world):
    freeze_dir, repo_root, payload, lock_hash = world
    store = ErratumStore(freeze_dir)
    store.freeze(_erratum(repo_root, lock_hash), payload, lock_hash, repo_root)
    with pytest.raises(ErratumError, match="already frozen"):
        store.freeze(_erratum(repo_root, lock_hash), payload, lock_hash, repo_root)


def test_an_invalid_erratum_is_never_written_to_disk(world):
    """驗證失敗時不得留下一份自己都通不過的記錄。"""
    freeze_dir, repo_root, payload, lock_hash = world
    store = ErratumStore(freeze_dir)
    bad = _erratum(repo_root, lock_hash, field_path="parameter_set_hash")
    with pytest.raises(ErratumScopeError):
        store.freeze(bad, payload, lock_hash, repo_root)
    assert store.list_ids() == []


def test_tampered_erratum_record_is_refused_on_load(world):
    freeze_dir, repo_root, payload, lock_hash = world
    store = ErratumStore(freeze_dir)
    path = store.freeze(_erratum(repo_root, lock_hash), payload, lock_hash, repo_root)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["corrected_value"] = "9.9.9"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ErratumError, match="payload_hash mismatch"):
        store.load("ERR-TEST")


def test_two_errata_on_the_same_field_are_refused(world):
    """解析結果不得取決於套用順序。"""
    freeze_dir, repo_root, payload, lock_hash = world
    first = _erratum(repo_root, lock_hash, erratum_id="ERR-A").payload()
    second = _erratum(repo_root, lock_hash, erratum_id="ERR-B").payload()
    with pytest.raises(ErratumError, match="same field"):
        apply_errata(payload, lock_hash, [first, second], repo_root)


# ---------------------------------------------------------------------------
# formal loader
# ---------------------------------------------------------------------------


def test_formal_loader_applies_verified_errata(world):
    freeze_dir, repo_root, payload, lock_hash = world
    ErratumStore(freeze_dir).freeze(
        _erratum(repo_root, lock_hash), payload, lock_hash, repo_root
    )
    resolved = load_formal_lock("initial_simulation", freeze_dir, repo_root)
    assert resolved.payload["environment"]["mitransient"] == "1.3.0"
    assert resolved.original_payload["environment"]["mitransient"] == "unavailable"
    assert resolved.payload_hash == lock_hash          # 身分不變
    assert resolved.provenance()["errata"][0]["erratum_id"] == "ERR-TEST"


def test_formal_loader_fails_closed_when_evidence_is_gone(world):
    """證據消失時不得退回原值繼續跑；provenance 已自相矛盾。"""
    freeze_dir, repo_root, payload, lock_hash = world
    ErratumStore(freeze_dir).freeze(
        _erratum(repo_root, lock_hash), payload, lock_hash, repo_root
    )
    (repo_root / "outputs" / "repro_a" / "manifest.json").unlink()
    with pytest.raises(ErratumError, match="evidence file not found"):
        load_formal_lock("initial_simulation", freeze_dir, repo_root)


def test_formal_loader_detects_a_modified_lock(world):
    from pcmef.core.locks import LockError

    freeze_dir, repo_root, payload, lock_hash = world
    lock_path = freeze_dir / "initial_simulation.lock.json"
    record = json.loads(lock_path.read_text(encoding="utf-8"))
    record["payload"]["environment"]["mitransient"] = "1.3.0"
    lock_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(LockError, match="integrity check"):
        load_formal_lock("initial_simulation", freeze_dir, repo_root)


# ---------------------------------------------------------------------------
# 真實的 ERR-001
# ---------------------------------------------------------------------------


def test_err_001_is_frozen_and_verifies_against_the_real_lock():
    resolved = load_formal_lock("initial_simulation", REPO_ROOT / "freeze", REPO_ROOT)
    assert "ERR-001" in resolved.errata_hashes
    assert resolved.payload["environment"]["mitransient"] == "1.3.0"
    assert (
        resolved.payload_hash
        == "dc15c9543a3aecacafcd1cbc110c48fc5fdd08d28457d4859d2a0bf573cb8335"
    )


def test_the_real_lock_file_still_records_the_defective_value():
    """lock 不可覆寫：磁碟上那一份必須原封不動，勘誤只疊在上面。"""
    record = json.loads(
        (REPO_ROOT / "freeze" / "initial_simulation.lock.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["payload"]["environment"]["mitransient"] == "unavailable"
    assert hash_object(record["payload"]) == record["payload_hash"]


def test_err_001_declares_no_scientific_state_change():
    payload = ErratumStore(REPO_ROOT / "freeze").load("ERR-001")
    assert payload["scientific_state_changed"] is False
    assert payload["field_path"] in CORRECTABLE_PATHS
    assert len(payload["evidence"]) >= 2
