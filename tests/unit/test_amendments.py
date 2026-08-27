# PC-MEF Research System source maintenance contract
# 上下游: 驗證 pcmef.core.amendments 的凍結與讀回契約；不觸及 freeze/ 正式目錄，
#         一律使用 tmp_path。
# 檔案路徑: tests/unit/test_amendments.py
# 產生時間: 2026-08-27 23:55 +08:00
# 版本: v0.1.0
# 功能說明: 確認協定修訂記錄一旦凍結就不能被覆寫或竄改 —— 這是「規則改過幾次」
#           唯一的可稽核痕跡。
# 模組定位: amendment 機制的行為契約測試。
# 主要責任:
#   1. 凍結後重複凍結必須被拒絕
#   2. payload 被改動後讀回必須拋錯
#   3. 前提證據欄位必須被完整保存
#   4. 實際的 AMD-001 必須存在、可讀回且不變式與程式碼一致
# 維護提醒:
#   - 不得改成「覆寫時發警告但仍寫入」。append-only 是這份記錄的全部意義。
#   - v0.1.0 新增：對應 NOTE-028 / AMD-001。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_amendments.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcmef.core.amendments import AmendmentError, AmendmentStore, ProtocolAmendment

REPO_ROOT = Path(__file__).resolve().parents[2]


def _amendment(amendment_id: str = "AMD-TEST") -> ProtocolAmendment:
    return ProtocolAmendment(
        amendment_id=amendment_id,
        title="test amendment",
        supersedes="previous contract",
        rationale="because the original contract conflated two facts",
        changed_contracts={"E1-GXX": {"before": "a", "after": "b"}},
        precondition_evidence={"heldout_access_count": 0},
        invariants_preserved={"canonical_recordings": 560},
        authority="researcher decision",
    )


def test_freeze_writes_a_hashed_document(tmp_path):
    store = AmendmentStore(tmp_path)
    path = store.freeze(_amendment())
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["amendment_id"] == "AMD-TEST"
    assert len(document["payload_hash"]) == 64
    assert store.list_ids() == ["AMD-TEST"]


def test_refreezing_the_same_id_is_refused(tmp_path):
    """判準改兩次就要有兩份記錄，否則「後來又改回去」不留痕跡。"""
    store = AmendmentStore(tmp_path)
    store.freeze(_amendment())
    with pytest.raises(AmendmentError, match="already frozen"):
        store.freeze(_amendment())


def test_tampering_with_the_payload_is_detected(tmp_path):
    store = AmendmentStore(tmp_path)
    path = store.freeze(_amendment())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["precondition_evidence"]["heldout_access_count"] = 7
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AmendmentError, match="payload_hash mismatch"):
        store.load("AMD-TEST")


def test_loading_an_unfrozen_amendment_raises(tmp_path):
    with pytest.raises(AmendmentError, match="not frozen"):
        AmendmentStore(tmp_path).load("AMD-NOPE")


# --- 實際的 AMD-001 ---------------------------------------------------------


def test_amd001_is_frozen_and_readable():
    payload = AmendmentStore(REPO_ROOT / "freeze").load("AMD-001")
    assert payload["changed_contracts"]["E1-G08"]["contract_version"] == "v2"


def test_amd001_records_that_heldout_was_untouched():
    """這份記錄唯一能證明「不是看到結果才改規則」的就是這幾個欄位。"""
    payload = AmendmentStore(REPO_ROOT / "freeze").load("AMD-001")
    evidence = payload["precondition_evidence"]
    assert evidence["heldout_access_count"] == 0
    assert evidence["heldout_firewall_FW02"] == "PASS"
    assert evidence["e1_outcome_lock_exists"] is False


def test_amd001_invariants_match_the_running_contract():
    """amendment 宣告的不變式必須與程式碼實際實作的一致。"""
    from pcmef.core.constants import (
        E1_G08_CONTRACT_VERSION,
        PROVENANCE_GATE_SATISFYING,
    )
    from pcmef.provenance.sigma import E1_G08_REQUIRED_FACETS

    payload = AmendmentStore(REPO_ROOT / "freeze").load("AMD-001")
    assert payload["changed_contracts"]["E1-G08"]["contract_version"] == (
        E1_G08_CONTRACT_VERSION
    )
    assert payload["invariants_preserved"]["e1_eligible_recordings"] == 560
    assert payload["invariants_preserved"]["sigma_numeric_scale"] == 65536.0
    # 位址不得混進 required facet —— amendment 說了不算，程式碼要真的這樣做。
    assert "register_address" not in E1_G08_REQUIRED_FACETS
    assert "RECONSTRUCTED" not in PROVENANCE_GATE_SATISFYING
