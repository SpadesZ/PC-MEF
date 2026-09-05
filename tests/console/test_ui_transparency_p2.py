# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 dry-run 的填位值不得出現在科研主畫面
#         （P1-3）、ToF 逐通道透明度（P1-4）、以及 run_id 的碰撞防護
#         （P1-5）。以合成 trace 與 Flask test client 為輸入；
#         不啟動子行程、不呼叫 provider、不觸碰 families 36-43。
# 檔案路徑: tests/console/test_ui_transparency_p2.py
# 產生時間: 2026-09-05 21:30 +08:00
# 版本: v0.1.0
# 功能說明: 三件「畫面說了一句它證明不了的話」。
# 模組定位: NOTE-091 / NOTE-092 / NOTE-088 的可執行防線。
# 主要責任:
#   1. test_a_dry_run_escalated_case_shows_no_fake_fx
#   2. test_the_placeholder_is_only_in_advanced
#   3. test_the_trace_list_does_not_score_unexecuted_rows
#   4. test_tof_summary_carries_hash_stats_and_sparkline
#   5. test_an_illegal_run_id_is_refused_not_sanitised
#   6. test_the_auto_run_id_is_not_second_resolution_only
# 維護提醒:
#   - 不得把填位分布搬回主畫面。它恰好等於 fixed fusion，讀者會把它當成
#     PC-MEF 的結果 —— 即使旁邊有警語。
#   - 不得把 run_id 改回 sanitize。剝掉非法字元會讓 `a/b` 與 `ab` 映射到
#     同一個目錄，第二次預演靜靜覆蓋第一次。
#   - v0.1.0 新增：首版，對應 P1-3 / P1-4 / P1-5。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_ui_transparency_p2.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import numpy as np
import pytest

flask = pytest.importorskip("flask")

from pcmef.console.runner import RunRecord  # noqa: E402

CLASS_ORDER = ["Empty", "Water-filled", "Bubbly", "Misty"]
PLACEHOLDER = [0.4, 0.35, 0.15, 0.10]


# ---------------------------------------------------------------------------
# P1-3 dry-run 的填位 F(x)
# ---------------------------------------------------------------------------


def _dry_run_trace(run_root, case_id="case_0000", *, escalated=True):
    """一筆 dry-run 的 escalated case：F(x) 是填位值。"""
    from pcmef.experiments.decision_trace import _arm_outcomes

    arms = {
        "vision_only": [0.7, 0.1, 0.1, 0.1],
        "tof_only": [0.1, 0.6, 0.2, 0.1],
        "fixed_fusion": PLACEHOLDER,
        "reliability_routing": PLACEHOLDER,
        "pcmef_full": PLACEHOLDER,
    }
    artifacts = run_root / "artifacts"
    cases = artifacts / "trace" / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    document = {
        "trace_schema_version": "decision_trace_v1",
        "code_revision": "d" * 40,
        "dry_run": True,
        "case_id": case_id, "row_index": 0,
        "condition": "conflict", "class_label": "Bubbly",
        "physical_scene_family": "fam_00",
        "inputs": {
            "rgb": {"sha256": "a" * 64, "original_shape": [64, 64, 3]},
            "tof": {"shape": [500, 4], "channels": ["distance_mm"]},
            "stress_id": "s1",
        },
        "perception": {
            "class_order": CLASS_ORDER,
            "p_vision": arms["vision_only"], "p_tof": arms["tof_only"],
            "vision_argmax": "Empty", "tof_argmax": "Water-filled",
        },
        "quality": {}, "reliability": {"threshold": 0.5},
        "routing": {
            "route": "escalated" if escalated else "fusion",
            "escalated": escalated, "consistent": True,
            "explanation": "…", "conditions": {},
        },
        "arms": arms,
        "arm_outcomes": _arm_outcomes(
            arms, CLASS_ORDER, escalated=escalated, dry_run=True
        ),
        "agent_execution": {"invoked": False, "reason": "dry_run", "artifacts": []},
        "final": {
            "distribution": PLACEHOLDER, "prediction_index": 0,
            "prediction_label": "Empty", "truth_label": "Bubbly",
            "correct": False, "s_a": None, "llm_called": False,
        },
    }
    (cases / f"{case_id}.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    (artifacts / "trace" / "trace_index.json").write_text(
        json.dumps({
            "dry_run": True, "run_status": "complete", "trace_status": "complete",
            "expected_cases": 1, "written_cases": 1, "failed_trace_cases": [],
            "cases": [{
                "case_id": case_id, "row_index": 0, "condition": "conflict",
                "class_label": "Bubbly",
                "route": "escalated" if escalated else "fusion",
                "escalated": escalated, "prediction": "Empty", "correct": False,
                "trace_path": f"cases/{case_id}.json",
            }],
        }),
        encoding="utf-8",
    )
    return document


@pytest.fixture()
def client(tmp_path):
    from pcmef.admin.app import create_app

    run_root = tmp_path / "runs"
    (run_root / "r1").mkdir(parents=True)
    record = RunRecord(
        run_id="r1", kind="formal_e2", label="", params={"mode": "dry-run"},
        command=[], status="succeeded", exit_code=0,
    )
    (run_root / "r1" / "run.json").write_text(
        json.dumps(record.to_json()), encoding="utf-8"
    )
    _dry_run_trace(run_root / "r1")

    app = create_app(registry_path=tmp_path / "r.db", vault_path=tmp_path / "v",
                     console_run_root=run_root, environ={})
    app.config["TESTING"] = True
    return app.test_client()


def test_a_dry_run_escalated_case_shows_no_fake_fx(client):
    """G5 未執行時，F(x)、預測與對錯都不得顯示成結果。

    那一列的 F(x) 恰好等於 fixed fusion。印出來讀者會把它當成 PC-MEF 的
    結果 —— 即使旁邊有一行警語（NOTE-091）。
    """
    body = client.get("/console/runs/r1/trace/case_0000").get_data(as_text=True)

    assert "NOT AVAILABLE" in body
    assert "NOT EXECUTED" in body
    # 主畫面不得出現「判對 / 判錯」的結論。
    assert "判錯" not in body and "判對" not in body


def test_the_placeholder_is_only_in_advanced(client):
    """填位分布可以留給 debug，但必須在 Advanced 裡，不與正式結果並列。"""
    body = client.get("/console/runs/r1/trace/case_0000").get_data(as_text=True)

    assert "Internal · 填位分布" in body
    # 它必須在一個 <details> 裡。
    head, _, tail = body.partition("Internal · 填位分布")
    assert head.rstrip().endswith("<summary>") or "<details" in head[-400:]
    # 而且標明它不是結果。
    assert "不是結果" in body or "不是</strong>" in body


def test_an_executed_case_still_shows_its_result(client, tmp_path):
    """非 escalated 的列 F(x) 依定義等於 p_trad，那不是填位值。"""
    from pcmef.admin.app import create_app

    run_root = tmp_path / "runs2"
    (run_root / "r2").mkdir(parents=True)
    (run_root / "r2" / "run.json").write_text(
        json.dumps(RunRecord(
            run_id="r2", kind="formal_e2", label="", params={"mode": "dry-run"},
            command=[], status="succeeded", exit_code=0,
        ).to_json()),
        encoding="utf-8",
    )
    _dry_run_trace(run_root / "r2", escalated=False)

    app = create_app(registry_path=tmp_path / "r2.db", vault_path=tmp_path / "v2",
                     console_run_root=run_root, environ={})
    app.config["TESTING"] = True
    body = app.test_client().get(
        "/console/runs/r2/trace/case_0000"
    ).get_data(as_text=True)

    assert "NOT AVAILABLE" not in body
    assert "判錯" in body


def test_the_trace_list_does_not_score_unexecuted_rows(tmp_path):
    """列表頁的正確率不得把填位值算進去。"""
    from pcmef.console import run_view

    run_dir = tmp_path / "r"
    _dry_run_trace(run_dir)
    view = run_view.trace_view(run_dir)

    assert view["scored"] == 0, "escalated 的預演列不得計分"
    assert view["excluded_unexecuted"] == 1
    assert view["accuracy"] is None


# ---------------------------------------------------------------------------
# P1-4 ToF 透明度
# ---------------------------------------------------------------------------


def test_tof_summary_carries_hash_stats_and_sparkline():
    from pcmef.experiments.decision_trace import SPARKLINE_POINTS, tof_summary

    rng = np.random.default_rng(0)
    summary = tof_summary(rng.random((500, 4)))

    assert summary["shape"] == [500, 4]
    assert len(summary["channels"]) == 4
    assert len(summary["sha256"]) == 64
    assert len(summary["per_channel"]) == 4

    for channel in summary["per_channel"]:
        assert channel["name"]
        assert len(channel["sha256"]) == 64
        for stat in ("mean", "std", "p10", "median", "p90"):
            assert channel[stat] is not None, stat
        assert 1 < len(channel["sparkline"]) <= SPARKLINE_POINTS
        assert channel["n_finite"] == 500


def test_tof_summary_does_not_embed_the_raw_array():
    """原始 (500, 4) 不進 trace —— 每筆 16 KB，384 筆就是數 MB。"""
    from pcmef.experiments.decision_trace import SPARKLINE_POINTS, tof_summary

    summary = tof_summary(np.zeros((500, 4)))
    for channel in summary["per_channel"]:
        assert len(channel["sparkline"]) <= SPARKLINE_POINTS


def test_tof_summary_survives_a_shape_mismatch():
    """形狀對不上時說明原因，不得拋例外 —— trace 是旁路。"""
    from pcmef.experiments.decision_trace import tof_summary

    summary = tof_summary(np.zeros((10, 2)))
    assert summary["per_channel"] == []
    assert "對不上" in summary["note"]


def test_two_different_tof_arrays_hash_differently():
    """content hash 要能回答「這兩筆的 ToF 是不是同一份」。"""
    from pcmef.experiments.decision_trace import tof_summary

    a = tof_summary(np.zeros((500, 4)))
    b = tof_summary(np.ones((500, 4)))
    assert a["sha256"] != b["sha256"]
    assert a["per_channel"][0]["sha256"] != b["per_channel"][0]["sha256"]


def test_the_case_page_renders_the_channels(client, tmp_path):
    """UI 不得說「case 頁可看」而實際沒有。"""
    from pcmef.admin.app import create_app
    from pcmef.experiments.decision_trace import tof_summary

    run_root = tmp_path / "runs3"
    (run_root / "r3").mkdir(parents=True)
    (run_root / "r3" / "run.json").write_text(
        json.dumps(RunRecord(
            run_id="r3", kind="formal_e2", label="", params={"mode": "dry-run"},
            command=[], status="succeeded", exit_code=0,
        ).to_json()),
        encoding="utf-8",
    )
    document = _dry_run_trace(run_root / "r3", escalated=False)
    document["inputs"]["tof"] = tof_summary(
        np.random.default_rng(1).random((500, 4))
    )
    (run_root / "r3" / "artifacts" / "trace" / "cases" / "case_0000.json"
     ).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    app = create_app(registry_path=tmp_path / "r3.db", vault_path=tmp_path / "v3",
                     console_run_root=run_root, environ={})
    app.config["TESTING"] = True
    body = app.test_client().get(
        "/console/runs/r3/trace/case_0000"
    ).get_data(as_text=True)

    assert "ToF 四個通道" in body
    assert "sparkline" in body                    # mini trace 真的畫出來
    assert "<polyline" in body
    assert "ToF sha256" in body
    for name in ("distance_mm", "ambient_rate_mcps", "signal_rate_mcps",
                 "sigma_like"):
        assert name in body, name


# ---------------------------------------------------------------------------
# P1-5 run_id 碰撞
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_id", ["a/b", "..", ".", "", "   ", "a b", "x" * 65, "a\\b", "a:b"]
)
def test_an_illegal_run_id_is_refused_not_sanitised(run_id):
    """剝掉非法字元會讓 `a/b` 與 `ab` 映射到同一個目錄。

    兩次不同的預演會靜靜互相覆蓋，而呼叫端以為自己給的是兩個名字
    （NOTE-088）。拒絕比默默改名安全。
    """
    from pcmef.experiments.e2_formal import FormalE2Error, run_artifact_root

    with pytest.raises(FormalE2Error, match="illegal run_id|own run_id"):
        run_artifact_root("out", dry_run=True, run_id=run_id)


def test_two_ids_never_collapse_onto_one_directory():
    """具體那一對：`a/b` 被拒絕，因此不可能與 `ab` 撞在一起。"""
    from pcmef.experiments.e2_formal import FormalE2Error, run_artifact_root

    legal = run_artifact_root("out", dry_run=True, run_id="ab")
    with pytest.raises(FormalE2Error):
        run_artifact_root("out", dry_run=True, run_id="a/b")
    assert legal.name == "ab"


def test_the_auto_run_id_is_not_second_resolution_only():
    """同一秒啟動的兩次預演不得拿到同一個目錄。"""
    from pcmef import cli

    source = inspect_source(cli.cmd_formal_run_e2)
    assert "uuid.uuid4().hex" in source, "自動 run_id 必須帶隨機尾碼"
    assert "%Y%m%dT%H%M%S" in source     # 時間戳仍在，方便人讀


def test_the_auto_run_id_is_accepted_by_the_validator():
    """自動產生的 id 必須通過自己的白名單。"""
    import uuid
    from datetime import datetime, timezone

    from pcmef.experiments.e2_formal import run_artifact_root

    generated = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "-" + uuid.uuid4().hex[:8]
    )
    assert run_artifact_root("out", dry_run=True, run_id=generated).name == generated


def inspect_source(function) -> str:
    import inspect

    return inspect.getsource(function)
