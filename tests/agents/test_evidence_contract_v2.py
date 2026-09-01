# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.agents.pcmef_agents 的
#         tof_fixed_summary_v2_channel_preserving() 與 project_for_role()；
#         以構造好的 (500,4) 陣列驗證，不連線、不讀 dataset、
#         不觸碰 families 36-43。
# 檔案路徑: tests/agents/test_evidence_contract_v2.py
# 產生時間: 2026-09-01 21:40 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 ToF 摘要真的逐 channel 分開（不再跨單位攤平），
#           以及四個角色實際收到的欄位與 role_evidence_contract_v2 一致。
# 模組定位: NOTE-052 兩項更正的可執行防線。少了它，把 v2 改回攤平、
#           或往 evidence bundle 加一個欄位讓四個角色全看得到，
#           都不會有測試失敗。
# 主要責任:
#   1. test_channel_order_is_fixed 欄位與順序固定
#   2. test_permutation_only_moves_temporal_statistics 打亂時間只動 temporal
#   3. test_changing_ambient_does_not_touch_distance_or_sigma channel 隔離
#   4. test_no_cross_channel_peak_or_centroid v1 的跨 channel 量已消失
#   5. test_each_role_receives_exactly_its_contract 四個角色逐一比對
#   6. test_new_evidence_fields_are_not_auto_forwarded 正向白名單性質
# 維護提醒:
#   - 不得放寬 test_changing_ambient_does_not_touch_distance_or_sigma。
#     它是「channel 不再互相污染」唯一的機械證據，也是 v1 被廢止的理由。
#   - 不得放寬 test_new_evidence_fields_are_not_auto_forwarded。
#     它是加法白名單與減法黑名單的差別所在。
#   - v0.1.0 新增：首版，對應 NOTE-052。
# 驗證方式:
#   - py -3.10 -m pytest tests/agents/test_evidence_contract_v2.py -v
# ------------------------------------------------------------

from __future__ import annotations

import numpy as np
import pytest

from pcmef.agents.pcmef_agents import (
    ROLE_EVIDENCE_CONTRACT_V2,
    TOF_CHANNEL_STATISTICS,
    AgentError,
    project_for_role,
    tof_fixed_summary_v2_channel_preserving,
)
from pcmef.core.constants import TOF_SCHEMA


def _recording(seed: int = 0, n: int = 500) -> np.ndarray:
    """四個 channel 各自有不同的量級 —— 那正是 v1 攤平時的問題來源。"""
    rng = np.random.default_rng(seed)
    return np.column_stack(
        [
            rng.normal(90.0, 2.0, n),      # distance_mm       ~90
            rng.gamma(2.0, 0.03, n),       # ambient_rate_mcps ~0.06
            rng.gamma(4.0, 3.0, n),        # signal_rate_mcps  ~12
            rng.gamma(3.0, 0.4, n),        # sigma_like        ~1.2
        ]
    )


# ---------------------------------------------------------------------------
# P0-1  ToF summary v2
# ---------------------------------------------------------------------------


def test_channel_order_is_fixed():
    summary = tof_fixed_summary_v2_channel_preserving(_recording())
    assert summary["channel_order"] == list(TOF_SCHEMA)
    assert list(summary["channels"]) == list(TOF_SCHEMA)
    for name in TOF_SCHEMA:
        assert list(summary["channels"][name]) == list(TOF_CHANNEL_STATISTICS)


def test_summary_length_is_fixed_across_cases():
    """FIXED_SUMMARY 的前提：不同 case 的欄位集合完全一樣。"""
    a = tof_fixed_summary_v2_channel_preserving(_recording(1))
    b = tof_fixed_summary_v2_channel_preserving(_recording(2))
    assert a.keys() == b.keys()
    for name in TOF_SCHEMA:
        assert a["channels"][name].keys() == b["channels"][name].keys()
    assert a["derived"].keys() == b["derived"].keys()


def test_permutation_only_moves_temporal_statistics():
    """打亂時間順序：非時間統計量必須一模一樣，只有 temporal_diff_std 變。"""
    recording = _recording(3)
    rng = np.random.default_rng(99)
    shuffled = recording[rng.permutation(len(recording))]

    before = tof_fixed_summary_v2_channel_preserving(recording)["channels"]
    after = tof_fixed_summary_v2_channel_preserving(shuffled)["channels"]

    order_free = [s for s in TOF_CHANNEL_STATISTICS if s != "temporal_diff_std"]
    for name in TOF_SCHEMA:
        for stat in order_free:
            assert before[name][stat] == after[name][stat], f"{name}.{stat} moved"
        assert before[name]["temporal_diff_std"] != after[name]["temporal_diff_std"]


def test_changing_ambient_does_not_touch_distance_or_sigma():
    """改一個 channel 只能改到它自己 —— v1 攤平時做不到這件事。"""
    recording = _recording(4)
    louder = recording.copy()
    louder[:, TOF_SCHEMA.index("ambient_rate_mcps")] *= 9.0

    before = tof_fixed_summary_v2_channel_preserving(recording)
    after = tof_fixed_summary_v2_channel_preserving(louder)

    for untouched in ("distance_mm", "sigma_like"):
        assert before["channels"][untouched] == after["channels"][untouched]
    assert before["channels"]["signal_rate_mcps"] == after["channels"]["signal_rate_mcps"]
    assert before["channels"]["ambient_rate_mcps"] != after["channels"]["ambient_rate_mcps"]
    # derived 的比值本來就該跟著動：它就是拿來讀 ambient 的。
    assert (
        after["derived"]["signal_to_ambient_ratio"]
        < before["derived"]["signal_to_ambient_ratio"]
    )


def test_no_cross_channel_peak_or_centroid():
    """v1 的跨 channel 量必須完全消失，不得以任何名字留下。"""
    summary = tof_fixed_summary_v2_channel_preserving(_recording())
    flat = str(summary)
    for banned in ("peak_bin", "centroid_bin", "temporal_spread_bins",
                   "leading_edge_bin", "peak_to_noise_ratio", "late_tail_fraction",
                   "total_return", "n_bins"):
        assert banned not in flat, f"{banned} is a v1 cross-channel quantity"


def test_flattened_input_is_refused():
    """攤平的 2000 點輸入必須被拒絕，而不是安靜地算出一個沒有意義的摘要。"""
    with pytest.raises(AgentError, match="flattened waveform"):
        tof_fixed_summary_v2_channel_preserving(_recording().ravel())


def test_summary_carries_no_benchmark_metadata():
    summary = tof_fixed_summary_v2_channel_preserving(_recording())
    flat = str(summary).lower()
    for banned in ("class", "condition", "severity", "family", "empty", "misty",
                   "bubbly", "water", "scenario", "split"):
        assert banned not in flat, f"{banned!r} leaked into the ToF summary"


def test_valid_sample_ratio_reads_distance_validity():
    recording = _recording(6)
    recording[:50, TOF_SCHEMA.index("distance_mm")] = 0.0
    summary = tof_fixed_summary_v2_channel_preserving(recording)
    assert summary["derived"]["valid_sample_ratio"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# P0-2  role_evidence_contract_v2
# ---------------------------------------------------------------------------


def _bundle() -> dict:
    """完整 evidence bundle 的最小替身，欄位名與 build_case_evidence 一致。"""
    return {
        "schema_version": "1.0",
        "representation_mode": "FIXED_SUMMARY",
        "class_order": ["Empty", "Water-filled", "Bubbly", "Misty"],
        "tof_summary": tof_fixed_summary_v2_channel_preserving(_recording()),
        "sensing_quality_cues": {
            "_meaning": "raw", "Q_vision": 1.1, "Q_tof": 15.2,
        },
        "calibrated_class_probabilities": {
            "_meaning": "p(y|x) is not reliability",
            "vision": {"Empty": 0.7, "Water-filled": 0.1, "Bubbly": 0.1, "Misty": 0.1},
            "tof": {"Empty": 0.1, "Water-filled": 0.6, "Bubbly": 0.2, "Misty": 0.1},
        },
        "modality_reliability": {
            "_meaning": "q_m", "q_vision": 0.81, "q_tof": 0.42,
        },
        "predictive_entropy": {"_meaning": "U_m", "U_vision": 0.3, "U_tof": 0.9},
        "cross_modal": {"_meaning": "D", "D": 0.55},
    }


def _flat_keys(payload: dict) -> set[str]:
    found: set[str] = set()

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                found.add(str(key))
                walk(value, f"{prefix}.{key}")
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix)

    walk(payload)
    return found


@pytest.mark.parametrize(
    "role,must_have,must_not_have",
    [
        (
            "observation_agent",
            {"tof_summary", "sensing_quality_cues", "Q_vision", "Q_tof"},
            {"calibrated_class_probabilities", "modality_reliability",
             "q_vision", "q_tof", "D", "U_vision", "U_tof", "gate_route",
             "observation_brief", "anonymous_proposals"},
        ),
        (
            "physics_agent",
            {"tof_summary", "observation_brief", "tof", "q_tof", "Q_tof", "U_tof"},
            {"vision", "q_vision", "Q_vision", "U_vision", "D", "gate_route",
             "anonymous_proposals"},
        ),
        (
            "visual_semantic_agent",
            {"observation_brief", "vision", "q_vision", "Q_vision", "U_vision"},
            {"tof_summary", "tof", "q_tof", "Q_tof", "U_tof", "D", "gate_route",
             "anonymous_proposals"},
        ),
        (
            "arbitration_agent",
            {"observation_brief", "anonymous_proposals", "vision", "tof",
             "q_vision", "q_tof", "D", "U_vision", "U_tof", "Q_vision", "Q_tof"},
            {"tof_summary", "gate_route"},
        ),
    ],
)
def test_each_role_receives_exactly_its_contract(role, must_have, must_not_have):
    extras = {
        "observation_brief": {"summary": "a brief"},
        "anonymous_proposals": [{"modality": "physics"}, {"modality": "visual_semantic"}],
    }
    payload = project_for_role(role, _bundle(), extras)
    keys = _flat_keys(payload)
    assert must_have <= keys, f"{role} missing {sorted(must_have - keys)}"
    leaked = must_not_have & keys
    assert not leaked, f"{role} received {sorted(leaked)}"


def test_image_policy_matches_the_contract():
    """Physics 與 Arbitration 不收影像；Observation 與 Visual 收。"""
    assert ROLE_EVIDENCE_CONTRACT_V2["observation_agent"]["receives_image"] is True
    assert ROLE_EVIDENCE_CONTRACT_V2["visual_semantic_agent"]["receives_image"] is True
    assert ROLE_EVIDENCE_CONTRACT_V2["physics_agent"]["receives_image"] is False
    assert ROLE_EVIDENCE_CONTRACT_V2["arbitration_agent"]["receives_image"] is False


def test_specialists_never_see_the_other_modality():
    """兩位專家的一致度要能當獨立證據，前提是他們看的東西不重疊。"""
    bundle = _bundle()
    physics = _flat_keys(project_for_role("physics_agent", bundle, {}))
    visual = _flat_keys(project_for_role("visual_semantic_agent", bundle, {}))
    assert not ({"vision", "q_vision", "Q_vision", "U_vision"} & physics)
    assert not ({"tof", "q_tof", "Q_tof", "U_tof", "tof_summary"} & visual)


def test_new_evidence_fields_are_not_auto_forwarded():
    """正向白名單的關鍵性質：新欄位預設不送給任何角色。

    減法式的 withhold 在這裡會全部放行 —— 那正是 v2 改成加法的理由。
    """
    bundle = {**_bundle(), "brand_new_leaky_field": {"ground_truth": "Empty"}}
    for role in ROLE_EVIDENCE_CONTRACT_V2:
        payload = project_for_role(role, bundle, {})
        assert "brand_new_leaky_field" not in _flat_keys(payload), role


def test_unknown_role_is_refused():
    with pytest.raises(AgentError, match="no evidence contract"):
        project_for_role("marketing_agent", _bundle(), {})


def test_meaning_prose_never_names_the_other_modality():
    """_meaning 會跟著欄位送到 specialist 手上，因此它自己也不能洩漏。

    寫「Q_vision 是影像的高頻能量」給 physics agent，等於用一句說明告訴它
    vision 側存在且怎麼讀 —— 同一種 anchoring，只是換成散文形式。
    各模態的具體讀法屬於該角色自己的 prompt。
    """
    bundle = _bundle()
    physics = project_for_role("physics_agent", bundle, {})
    visual = project_for_role("visual_semantic_agent", bundle, {})

    physics_prose = " ".join(
        str(v.get("_meaning", "")) for v in physics.values() if isinstance(v, dict)
    )
    visual_prose = " ".join(
        str(v.get("_meaning", "")) for v in visual.values() if isinstance(v, dict)
    )
    for banned in ("Q_vision", "q_vision", "U_vision", "p_vision"):
        assert banned not in physics_prose
    for banned in ("Q_tof", "q_tof", "U_tof", "p_tof"):
        assert banned not in visual_prose


def test_meaning_travels_with_the_numbers():
    """p_m 與 q_m 的語意說明必須跟著數字走，否則角色只拿到一個裸數字。"""
    payload = project_for_role("physics_agent", _bundle(), {})
    assert payload["calibrated_class_probabilities"]["_meaning"]
    assert payload["modality_reliability"]["_meaning"]
