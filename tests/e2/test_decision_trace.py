# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 experiments.decision_trace 的路由說明、
#         trace 組裝與 TraceWriter 的落盤／原子性。用合成資料，
#         不需要 provider、dataset 或 frozen checkpoint。
# 檔案路徑: tests/e2/test_decision_trace.py
# 產生時間: 2026-09-02 23:40 +08:00
# 版本: v0.1.0
# 功能說明: 釘住 decision trace 的三條契約：說明由數值推導、
#           agent payload 是實際送出的那一份、index 只在 run 跑完才寫。
# 模組定位: P2-1 的回歸測試。trace 是可讀性工具，但它一旦說謊就比沒有更糟 ——
#           一個編出來的路由理由看起來非常有說服力。
# 主要責任:
#   1. test_explanation_is_derived_from_values 說明由數值推導，不照標籤挑模板
#   2. test_inconsistent_route_is_flagged 矛盾要被標出來而不是照唸
#   3. test_index_is_only_written_when_the_run_finishes 原子性與 run_status
#   4. test_aborted_index_says_so 中止時不得留下看似完整的 index
#   5. test_dry_run_escalation_is_explicit 不是 null，是「停在 escalation 邊界」
# 維護提醒:
#   - 不得把 route_explanation 改回「照 route 挑模板」。那樣在 route 與數值
#     不一致時會編出一個不成立的理由（NOTE-064）。
#   - 不得讓 trace 的例外傳播到決策路徑。它是觀測，不是決策。
#   - v0.1.0 新增：首版，對應 P2-1。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_decision_trace.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from pcmef.experiments.decision_trace import (
    DISAGREEMENT_KEY,
    RUN_STATUS_ABORTED,
    RUN_STATUS_COMPLETE,
    TRACE_SCHEMA_VERSION,
    CaseTrace,
    TraceWriter,
    build_case_trace,
    route_explanation,
)

DELTA = 0.48965981236738715


# ---------------------------------------------------------------------------
# 路由說明
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route, q_vision, q_tof, disagreement",
    [
        ("trust_vision", 0.82, 0.31, 0.42),
        ("trust_tof", 0.20, 0.71, 0.30),
        ("fusion", 0.80, 0.70, 0.20),
        ("escalated", 0.80, 0.70, 0.62),   # 品質都過關但分歧超標
        ("escalated", 0.20, 0.30, 0.10),   # 兩側都弱
    ],
)
def test_explanation_matches_the_values(route, q_vision, q_tof, disagreement):
    report = route_explanation(
        route, q_vision, q_tof, disagreement, delta=DELTA
    )
    assert report["consistent"] is True
    assert report["implied_route"] == route
    # 說明必須帶著實際數值，不能是一句通用的話。
    assert f"{q_vision:.3f}" in report["explanation"]


def test_an_inconsistent_route_is_flagged_not_narrated():
    """route 與數值矛盾時，說明必須指出矛盾，而不是照 route 編一個理由。

    照標籤挑模板的版本會輸出「ToF 品質過關（q_T=0.310 ≥ 0.500）」——
    一句看起來很有說服力但不成立的話。那比沒有說明更糟。
    """
    report = route_explanation("trust_tof", 0.82, 0.31, 0.42, delta=DELTA)
    assert report["consistent"] is False
    assert report["implied_route"] == "trust_vision"
    assert "不一致" in report["explanation"]
    # 而且不得出現那句假話。
    assert "只有 ToF 的感測品質過關" not in report["explanation"]


def test_conditions_are_reported_as_booleans_too():
    """畫面要能標紅，不能只有一段文字。"""
    conditions = route_explanation(
        "escalated", 0.2, 0.3, 0.1, delta=DELTA
    )["conditions"]
    assert conditions["vision_quality_passes"] is False
    assert conditions["tof_quality_passes"] is False
    assert conditions["exceeds_disagreement_threshold"] is False


def test_the_disagreement_key_matches_duq_signals():
    """duq_signals 用 "D"，不是 "disagreement"。

    拼錯時 .get() 安靜地回 NaN，而 NaN 在比較裡一律 False —— 路由說明
    會因此走錯分支。這條測試把兩邊的鍵綁在一起。
    """
    from pcmef.perception.gate import duq_signals

    proba = np.tile(np.array([0.7, 0.1, 0.15, 0.05]), (2, 1))
    signals = duq_signals(
        proba, proba,
        {"vision_sharpness": np.zeros(2), "tof_snr": np.zeros(2)},
    )
    assert DISAGREEMENT_KEY in signals


# ---------------------------------------------------------------------------
# trace 組裝
# ---------------------------------------------------------------------------


class _Decision:
    def __init__(self, final, s_a=None, llm_called=False):
        self.final = np.asarray(final)
        self.s_a = None if s_a is None else np.asarray(s_a)
        self.llm_called = llm_called


class _Rule:
    disagreement_threshold = DELTA
    temperature_vision = 0.169
    temperature_tof = 0.0498


def _row(condition="clean", label="Empty"):
    return {
        "stress_id": "x_f1_r0__clean",
        "base_scenario_id": "x_f1_r0",
        "condition": condition,
        "class_label": label,
        "physical_scene_family": "Empty_f1",
    }


def _build(route, dry_run=False, agent_calls=(), **kwargs):
    return build_case_trace(
        row=_row(), row_index=3, case_id="case_0003",
        p_vision=[0.7, 0.1, 0.15, 0.05], p_tof=[0.1, 0.6, 0.2, 0.1],
        signals={DISAGREEMENT_KEY: 0.42, "U_vision": 0.9, "U_tof": 1.1,
                 "Q_vision": 0.3, "Q_tof": 13.0},
        q_vision=kwargs.get("q_vision", 0.82), q_tof=kwargs.get("q_tof", 0.31),
        route=route, rule=_Rule(), decision=_Decision([0.7, 0.1, 0.15, 0.05]),
        agent_calls=list(agent_calls),
        class_order=("Empty", "Water-filled", "Bubbly", "Misty"),
        arms={"vision_only": [0.7, 0.1, 0.15, 0.05]},
        rgb_preview={"sha256": "abc", "original_shape": [64, 64, 3],
                     "preview_path": "previews/case_0003.png"},
        tof_shape=[500, 4], dry_run=dry_run,
    )


def test_a_non_escalated_case_says_why_no_agent_ran():
    trace = _build("trust_vision")
    assert trace.agent_execution == {
        "invoked": False, "reason": "not_escalated", "artifacts": [],
    }


def test_a_dry_run_escalation_is_explicit_not_null():
    """不是漏資料，是「依 routing 應進仲裁，但本次為零成本預演」。"""
    trace = _build("escalated", dry_run=True, q_vision=0.2, q_tof=0.3)
    execution = trace.agent_execution
    assert execution["invoked"] is False
    assert execution["reason"] == "dry_run"
    assert "escalation boundary" in execution["note"]


def test_the_final_block_carries_truth_and_correctness():
    trace = _build("trust_vision")
    assert trace.final["prediction_label"] == "Empty"
    assert trace.final["truth_label"] == "Empty"
    assert trace.final["correct"] is True


# ---------------------------------------------------------------------------
# 落盤與原子性
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer(tmp_path):
    return TraceWriter(
        tmp_path, run_id="test-run", code_revision="deadbeef",
        runtime_identity={"gate": "abc"}, dry_run=False,
    )


def test_index_is_absent_until_the_run_finishes(writer, tmp_path):
    writer.write_case(_build("fusion", q_vision=0.8, q_tof=0.7))
    index = tmp_path / "trace" / "trace_index.json"
    assert not index.exists(), (
        "a half-written index is worse than none: it looks complete"
    )
    writer.finalise()
    assert index.exists()


def test_a_finished_index_says_complete(writer, tmp_path):
    writer.write_case(_build("fusion", q_vision=0.8, q_tof=0.7))
    path = writer.finalise()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["run_status"] == RUN_STATUS_COMPLETE
    assert document["trace_schema_version"] == TRACE_SCHEMA_VERSION
    assert document["run_id"] == "test-run"
    assert document["code_revision"] == "deadbeef"
    assert document["n_cases"] == 1


def test_an_aborted_run_leaves_an_index_that_says_so(writer, tmp_path):
    writer.write_case(_build("fusion", q_vision=0.8, q_tof=0.7))
    path = writer.abort("FormalE2Error: ABORT_FORMAL_RUN at row 57")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["run_status"] == RUN_STATUS_ABORTED
    assert "ABORT_FORMAL_RUN" in document["note"]
    # 已寫的 case 要留著 —— 它們是失敗前的真實紀錄。
    assert document["n_cases"] == 1


def test_no_partial_file_survives(writer, tmp_path):
    writer.write_case(_build("fusion", q_vision=0.8, q_tof=0.7))
    writer.finalise()
    assert not list((tmp_path / "trace").glob("*.partial"))


def test_index_rows_are_light(writer, tmp_path):
    """index 是列表頁用的；完整內容留在 case 檔。"""
    writer.write_case(_build("fusion", q_vision=0.8, q_tof=0.7))
    document = json.loads(writer.finalise().read_text(encoding="utf-8"))
    row = document["cases"][0]
    assert set(row) == {
        "case_id", "row_index", "condition", "class_label", "route",
        "escalated", "prediction", "correct", "trace_path", "preview_path",
    }


def test_preview_uses_the_same_encoder_as_agent_evidence(writer, tmp_path):
    """preview 上宣稱「這就是 agent 看到的圖」，那必須是真的。"""
    import base64

    from pcmef.agents.pcmef_agents import encode_image_evidence, rgb_to_png_bytes

    rgb = np.random.default_rng(5).random((32, 32, 3))
    relative, shape = writer.write_preview("case_0007", rgb)
    written = (tmp_path / "trace" / relative).read_bytes()

    assert shape == [32, 32, 3]
    assert written == rgb_to_png_bytes(rgb)
    # 與送給 agent 的那一份逐位元相同。
    assert written == base64.b64decode(encode_image_evidence(rgb)["data_b64"])
