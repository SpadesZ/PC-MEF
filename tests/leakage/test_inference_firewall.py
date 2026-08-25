# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；操作 core.inference_payload、core.opaque_ids 與
#         core.ids；opaque map 的存檔測試寫在 tmp_path，不產生常駐 artifact。
# 檔案路徑: tests/leakage/test_inference_firewall.py
# 產生時間: 2026-08-25 21:55 +08:00
# 版本: v0.1.0
# 功能說明: 驗證送給 LLM 的內容確實看不出答案 —— 代號不具語意、品質指標只限
#           allowlist、把類別或 condition 字串塞進任何欄位都會被擋下、
#           未載入的檔案路徑不得代替影像張量。
# 模組定位: truth firewall 的驗收測試。它刻意構造違規 payload，
#           這些構造僅供本檔使用，不得被其他模組 import 當成範例。
# 主要責任:
#   1. opaque id 的非語意性、run 內 deterministic 與跨 run 相異
#   2. map_hash 不涵蓋 salt，以及存檔的竄改偵測與不可覆寫
#   3. quality cue allowlist 生效，且與 reliability 特徵不相交
#   4. 未載入路徑、非有限 evidence、無 evidence 三種拒絕路徑
#   5. provider payload snapshot 只含 opaque 身分
#   6. 用合法欄位夾帶語意字串的六種變體全部被擋
# 維護提醒:
#   - 不得放寬 predictive entropy 的排除；那是 NOTE-003 的核心結論，
#     放進去會讓 s_A 與 p_rel 相關而破壞 G5 對 G4 的因果解釋。
#   - 不得把本檔的違規 payload 構造搬到其他模組重用。
#   - 新增 payload 欄位時必須同步在此加一條「該欄位不得夾帶語意」的案例。
#   - v0.1.0 新增：首版洩漏驗收，對應 NOTE-003 與 NOTE-004。
# 驗證方式:
#   - py -3.10 -m pytest tests/leakage/test_inference_firewall.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.core.constants import RELIABILITY_FEATURE_CUES
from pcmef.core.ids import real_recording_id, synthetic_scenario_id
from pcmef.core.inference_payload import (
    OBSERVABLE_QUALITY_CUES,
    InferenceFirewallViolation,
    InferencePayload,
    assert_no_forbidden_tokens,
)
from pcmef.core.opaque_ids import OpaqueIdMap

@pytest.fixture()
def opaque_map() -> OpaqueIdMap:
    return OpaqueIdMap.create(
        "e2_formal_20260825_r01",
        [
            synthetic_scenario_id("Conflict-Stress", "Bubbly", 42),
            synthetic_scenario_id("Clean", "Empty", 1),
            real_recording_id("Misty", 7),
        ],
    )


@pytest.fixture()
def opaque_id(opaque_map: OpaqueIdMap) -> str:
    return opaque_map.to_opaque(
        synthetic_scenario_id("Conflict-Stress", "Bubbly", 42)
    )


@pytest.fixture()
def tof_evidence() -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.normal(size=(500, 4))


# ---------------------------------------------------------------------------
# opaque id 契約
# ---------------------------------------------------------------------------


def test_opaque_id_is_non_semantic(opaque_id: str):
    """SRC-SAI Appendix I1：opaque_case_id 必須 random/non-semantic。"""
    assert len(opaque_id) == 32
    assert all(char in "0123456789abcdef" for char in opaque_id)
    for token in ("conflict", "bubbly", "syn", "mid", "stress"):
        assert token not in opaque_id.lower()


def test_semantic_case_id_is_rejected_as_opaque_id(tof_evidence):
    """canonical ID 具語意，直接當 opaque id 使用必須被拒。"""
    with pytest.raises(InferenceFirewallViolation):
        InferencePayload(
            opaque_case_id=synthetic_scenario_id("Conflict-Stress", "Bubbly", 42),
            tof_sequence=tof_evidence,
        )


def test_opaque_map_round_trip_is_stable_within_a_run(opaque_map: OpaqueIdMap):
    case_id = synthetic_scenario_id("Clean", "Empty", 1)
    opaque = opaque_map.to_opaque(case_id)
    assert opaque_map.to_opaque(case_id) == opaque, "must be deterministic within a run"
    assert opaque_map.to_canonical(opaque) == case_id


def test_two_runs_produce_different_opaque_ids():
    """NOTE-004：跨 run 不同，避免 opaque id 退化成半永久語意標籤。"""
    case_ids = [synthetic_scenario_id("Clean", "Empty", 1)]
    first = OpaqueIdMap.create("run_a_20260825_r01", case_ids)
    second = OpaqueIdMap.create("run_b_20260825_r01", case_ids)
    assert first.to_opaque(case_ids[0]) != second.to_opaque(case_ids[0])


def test_map_hash_excludes_the_salt(opaque_map: OpaqueIdMap):
    """salt 進 lock 等於 opaque id 可被逆推，因此 map_hash 不得涵蓋 salt。"""
    tampered = OpaqueIdMap(
        run_id=opaque_map.run_id,
        salt=b"\x00" * 32,
        forward=dict(opaque_map.forward),
        reverse=dict(opaque_map.reverse),
    )
    assert tampered.map_hash() == opaque_map.map_hash()


def test_saved_map_detects_tampering(tmp_path, opaque_map: OpaqueIdMap):
    import json

    path = tmp_path / "opaque_map.json"
    opaque_map.save(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    first_case = sorted(data["forward"])[0]
    data["forward"][first_case] = "f" * 32
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(Exception):
        OpaqueIdMap.load(path)


def test_saved_map_cannot_be_silently_overwritten(tmp_path, opaque_map: OpaqueIdMap):
    path = tmp_path / "opaque_map.json"
    opaque_map.save(path)
    with pytest.raises(Exception):
        opaque_map.save(path)


# ---------------------------------------------------------------------------
# quality cue allowlist
# ---------------------------------------------------------------------------


def test_allowlisted_quality_cues_are_accepted(opaque_id, tof_evidence):
    payload = InferencePayload(
        opaque_case_id=opaque_id,
        tof_sequence=tof_evidence,
        quality_cues={"tof_sigma_like_mean": 12.5, "tof_temporal_variability": 0.31},
    )
    assert payload.quality_cues["tof_sigma_like_mean"] == 12.5


@pytest.mark.parametrize(
    "cue_key",
    [
        "class_label",
        "condition",
        "severity",
        "corruption_target",
        "parent_scene_family",
        "tof_predictive_entropy",
        "vision_predictive_entropy",
    ],
)
def test_non_allowlisted_quality_cues_are_rejected(opaque_id, tof_evidence, cue_key):
    with pytest.raises(InferenceFirewallViolation):
        InferencePayload(
            opaque_case_id=opaque_id,
            tof_sequence=tof_evidence,
            quality_cues={cue_key: 1.0},
        )


def test_observable_cues_and_reliability_features_are_disjoint():
    """NOTE-003：agent payload 的 cue 與 numerical path 的 q_T/q_V 特徵是兩份清單。

    predictive entropy 出現在 reliability 特徵中，但絕不得出現在 agent payload，
    否則 s_A 會與 p_rel 統計相關，破壞 decision-space interpolation 的語意。
    """
    reliability_cues = set(RELIABILITY_FEATURE_CUES["tof"]) | set(
        RELIABILITY_FEATURE_CUES["vision"]
    )
    entropy_cues = {cue for cue in reliability_cues if cue.endswith("predictive_entropy")}
    assert entropy_cues, "reliability features must include predictive entropy"
    assert not (entropy_cues & OBSERVABLE_QUALITY_CUES), (
        "predictive entropy must never be exposed to the multi-agent path"
    )


def test_non_finite_quality_cue_is_rejected(opaque_id, tof_evidence):
    with pytest.raises(InferenceFirewallViolation):
        InferencePayload(
            opaque_case_id=opaque_id,
            tof_sequence=tof_evidence,
            quality_cues={"vision_blur": float("nan")},
        )


# ---------------------------------------------------------------------------
# evidence 必須已載入
# ---------------------------------------------------------------------------


def test_payload_without_evidence_is_rejected(opaque_id):
    with pytest.raises(InferenceFirewallViolation):
        InferencePayload(opaque_case_id=opaque_id)


def test_filesystem_path_instead_of_tensor_is_rejected(opaque_id):
    """SRC-PLAN §2.2.3：Orchestrator 必須先載入 bytes/tensor，不得把 source path 送進 LLM。"""
    with pytest.raises(InferenceFirewallViolation):
        InferencePayload(
            opaque_case_id=opaque_id,
            rgb_tensor="data/synthetic/syn_conflictstress_bubbly_0042/rgb.png",
        )


def test_non_finite_evidence_is_rejected(opaque_id):
    broken = np.full((500, 4), np.nan)
    with pytest.raises(InferenceFirewallViolation):
        InferencePayload(opaque_case_id=opaque_id, tof_sequence=broken)


# ---------------------------------------------------------------------------
# provider payload snapshot
# ---------------------------------------------------------------------------


def test_provider_payload_snapshot_contains_only_opaque_identity(opaque_id, tof_evidence):
    """SRC-SAI §29：provider payload snapshot 必須只含 opaque_case_id。"""
    payload = InferencePayload(
        opaque_case_id=opaque_id,
        tof_sequence=tof_evidence,
        quality_cues={"tof_signal_rate_mean": 4.2},
    )
    snapshot = payload.to_provider_dict()
    assert snapshot["opaque_case_id"] == opaque_id
    assert set(snapshot) <= {"opaque_case_id", "quality_cues", "tof_shape", "rgb_shape"}
    assert "case_id" not in snapshot
    assert "class_label" not in snapshot


@pytest.mark.parametrize(
    "leaky_summary",
    [
        {"condition": "Conflict-Stress"},
        {"note": "generated from parent_scene_family f19"},
        {"origin": "syn_conflictstress_bubbly_0042"},
        {"hint": "the true class is Bubbly"},
        {"severity_note": "Mid"},
        {"source": "data/raw_real/bubble_007.csv"},
    ],
)
def test_summary_carrying_semantic_tokens_is_rejected(
    opaque_id, tof_evidence, leaky_summary
):
    """用合法欄位夾帶語意字串也必須被擋下。"""
    with pytest.raises(InferenceFirewallViolation):
        InferencePayload(
            opaque_case_id=opaque_id,
            tof_sequence=tof_evidence,
            tof_summary=leaky_summary,
        )


def test_forbidden_token_scan_walks_nested_structures():
    with pytest.raises(InferenceFirewallViolation):
        assert_no_forbidden_tokens(
            {"outer": [{"inner": ["harmless", "Water-filled"]}]}, "payload"
        )


def test_forbidden_token_scan_allows_pure_numeric_payloads():
    assert_no_forbidden_tokens(
        {"tof_sigma_like_mean": 12.5, "counts": [1, 2, 3], "ok": True}, "payload"
    )
