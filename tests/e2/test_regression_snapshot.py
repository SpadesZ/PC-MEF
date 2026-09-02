# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.experiments.regression_snapshot 的
#         純函式部分與存檔快照的治理欄位。**不**跑完整 capture ——
#         那需要 torch checkpoint 與 stress set，屬 CLI 層的整合驗證。
# 檔案路徑: tests/e2/test_regression_snapshot.py
# 產生時間: 2026-09-02 10:55 +08:00
# 版本: v0.1.0
# 功能說明: 確認快照的比對邏輯真的抓得到差異、攤平函式處理得了巢狀結構，
#           以及存檔的快照沒有偷偷把自己升格成 Golden baseline。
# 模組定位: P0-0 的回歸測試。快照本身是拿來偵測漂移的工具，
#           工具自己也需要被釘住 —— 一個永遠回報 IDENTICAL 的比較器
#           比沒有比較器更糟。
# 主要責任:
#   1. test_flatten_numbers_* 驗證巢狀 summary 會被完整攤平
#   2. test_non_numeric_content_* 驗證數值被排除、文字被保留
#   3. test_cross_condition_invariance_* 驗證文字差異會被抓到
#   4. test_snapshot_declares_itself_provisional 擋下「偷偷升格為 Golden」
#   5. test_stored_snapshot_records_the_frozen_expectations 釘住關鍵科學性質
# 維護提醒:
#   - 不得把 test_snapshot_declares_itself_provisional 改成允許 canonical=true。
#     要升格為 Golden 必須先解除 CANONICAL_BLOCKERS 的每一條，
#     而那是 P0-B 的工作，不是改一個測試。
#   - 不得因為存檔快照被重新 capture 就放寬 test_stored_snapshot_*。
#     那幾條釘的是科學性質（image routing、gate_route 不外送、
#     只有 temporal_diff_std 對時序敏感），重新 capture 不該改變它們。
#   - v0.1.0 新增：首版，對應 P0-0。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_regression_snapshot.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcmef.experiments.regression_snapshot import (
    CANONICAL_BLOCKERS,
    COVERED_TGR,
    DEFAULT_SNAPSHOT_PATH,
    UNCOVERED_TGR,
    _cross_condition_invariance,
    _digest_array,
    _flatten_numbers,
    _non_numeric_content,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / DEFAULT_SNAPSHOT_PATH


# ---------------------------------------------------------------------------
# 攤平：巢狀 summary 不能只看最外層
# ---------------------------------------------------------------------------


def test_flatten_numbers_reaches_into_nested_channels():
    """v2 的 summary 是巢狀的；只看 top level 會漏掉全部 24 個統計量。"""
    summary = {
        "summary_version": "v2",
        "channel_order": ["a", "b"],
        "n_samples": 500,
        "channels": {
            "a": {"mean": 1.0, "std": 2.0},
            "b": {"mean": 3.0, "std": 4.0},
        },
        "derived": {"ratio": 5.0, "_meaning": "prose"},
        "_meaning": "prose",
    }
    flat = _flatten_numbers(summary)
    assert flat == {
        "n_samples": 500.0,
        "channels.a.mean": 1.0,
        "channels.a.std": 2.0,
        "channels.b.mean": 3.0,
        "channels.b.std": 4.0,
        "derived.ratio": 5.0,
    }


def test_flatten_numbers_skips_underscore_prose_and_booleans():
    """`_meaning` 是散文、bool 不是量測值，兩者都不該進數值比對。"""
    flat = _flatten_numbers({"_meaning": "x", "flag": True, "value": 1.5})
    assert flat == {"value": 1.5}


# ---------------------------------------------------------------------------
# 非數值內容：數值該被排除，文字該被留下
# ---------------------------------------------------------------------------


def test_non_numeric_content_drops_numbers_and_keeps_text():
    content = _non_numeric_content(
        {"p": 0.25, "n": 3, "note": "hello", "ok": True, "nested": {"s": "deep"}}
    )
    assert content == {
        "note": "hello",
        "ok": "True",
        "nested.s": "deep",
    }


def test_non_numeric_content_indexes_list_positions():
    """list 位置要進路徑，否則兩個順序不同的清單會被當成相同。"""
    content = _non_numeric_content({"xs": ["a", "b"]})
    assert content == {"xs[0]": "a", "xs[1]": "b"}


# ---------------------------------------------------------------------------
# 跨 condition 不變性：這是取代 token 掃描的那個檢驗
# ---------------------------------------------------------------------------


def _call(task_code: str, payload: dict) -> dict:
    return {"task_code": task_code, "payload": payload, "n_images": 0,
            "spec_needs_image": False}


def test_cross_condition_invariance_passes_when_only_numbers_differ():
    """數值本來就該不同 —— 那不是洩漏。"""
    report = _cross_condition_invariance(
        [_call("physics_agent", {"p": 0.1, "note": "same"})],
        [_call("physics_agent", {"p": 0.9, "note": "same"})],
        "clean", "conflict",
    )
    assert report["invariant"] is True
    assert report["differing_paths_by_role"] == {}


def test_cross_condition_invariance_catches_a_condition_leaking_through_prose():
    """一句隨 condition 改寫的散文就是洩漏，而 token 掃描抓不到它。"""
    report = _cross_condition_invariance(
        [_call("physics_agent", {"note": "signal looks normal"})],
        [_call("physics_agent", {"note": "signal looks degraded"})],
        "clean", "tof_degraded",
    )
    assert report["invariant"] is False
    assert report["differing_paths_by_role"] == {"physics_agent": ["note"]}


def test_cross_condition_invariance_catches_an_added_field():
    report = _cross_condition_invariance(
        [_call("arbitration_agent", {"a": "x"})],
        [_call("arbitration_agent", {"a": "x", "condition_hint": "conflict"})],
        "clean", "conflict",
    )
    assert report["invariant"] is False
    assert "condition_hint" in report["differing_paths_by_role"]["arbitration_agent"]


# ---------------------------------------------------------------------------
# 指紋：不能永遠相等
# ---------------------------------------------------------------------------


def test_digest_array_separates_values_that_actually_differ():
    numpy = pytest.importorskip("numpy")
    a = numpy.array([1.0, 2.0, 3.0])
    b = numpy.array([1.0, 2.0, 3.000001])
    assert _digest_array(a) != _digest_array(b)


def test_digest_array_ignores_signed_zero():
    """+0.0 與 -0.0 數值相同，位元不同。指紋不該因此誤報。"""
    numpy = pytest.importorskip("numpy")
    assert _digest_array(numpy.array([0.0, 1.0])) == _digest_array(
        numpy.array([-0.0, 1.0])
    )


# ---------------------------------------------------------------------------
# 治理：不得偷偷升格為 Golden
# ---------------------------------------------------------------------------


def test_the_module_still_lists_every_reason_it_is_not_canonical():
    assert len(CANONICAL_BLOCKERS) >= 4
    joined = " ".join(CANONICAL_BLOCKERS)
    assert "AMD-007" in joined
    assert "llm_runtime" in joined
    assert "paid" in joined


def test_covered_and_uncovered_tgr_do_not_overlap():
    assert not set(COVERED_TGR) & set(UNCOVERED_TGR)
    assert set(UNCOVERED_TGR) == {"TGR-09", "TGR-11"}


@pytest.mark.skipif(not SNAPSHOT_PATH.exists(), reason="snapshot not captured yet")
def test_snapshot_declares_itself_provisional():
    """存檔的快照不得宣稱自己是 canonical baseline。"""
    document = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert document["canonical"] is False
    assert document["scientific_result"] is False
    assert document["FINAL_E2_36_43_TOUCHED"] == "NO"
    assert document["canonical_blockers"]


@pytest.mark.skipif(not SNAPSHOT_PATH.exists(), reason="snapshot not captured yet")
def test_stored_snapshot_records_the_frozen_expectations():
    """釘住幾條不該因重新 capture 而改變的科學性質。"""
    items = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["items"]

    projection = items["TGR-08_role_evidence_projection"]
    assert projection["image_routing"] == {
        "observation_agent": True,
        "physics_agent": False,
        "visual_semantic_agent": True,
        "arbitration_agent": False,
    }
    assert projection["image_routing_declaration_matches_implementation"] is True
    assert projection["gate_route_withheld_from_every_role"] is True

    tof = items["TGR-04_tof_channel_semantics"]
    assert tof["no_cross_channel_waveform_statistic"] is True
    # 每個 channel 恰好一個對時序敏感的統計量，其餘一律不敏感。
    assert all(
        path.endswith(".temporal_diff_std") for path in tof["order_sensitive_paths"]
    )
    assert len(tof["order_sensitive_paths"]) == len(tof["channel_order"])
    # 擾動 ambient 只能影響明確跨 channel 的導出量。
    assert tof["paths_contaminated_by_ambient_perturbation"] == [
        "derived.signal_to_ambient_ratio"
    ]

    firewall = items["TGR-12_truth_firewall"]
    assert firewall["clean"] is True
    assert firewall["cross_condition_invariance"]["invariant"] is True

    assert items["TGR-01_class_order"]["agents_import_the_same_object"] is True
    assert items["TGR-02_scenario_identity"]["empty_f27_present"] is False
    assert items["TGR-10_statistics"]["resample_unit"] == "physical_scene_family"
    assert items["TGR-10_statistics"]["bootstrap_seed"] == 20260827
