# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 console.run_view 的讀取邏輯與六個分頁的
#         HTTP 行為。用合成的 run 目錄，不啟動子行程也不呼叫 provider。
# 檔案路徑: tests/console/test_run_sections.py
# 產生時間: 2026-09-03 14:10 +08:00
# 版本: v0.1.0
# 功能說明: 確認單次 run 的六個分頁各自只回答自己的問題，
#           沒有內容時說明原因，而且 preview 端點讀不到別的檔案。
# 模組定位: P2-4 的回歸測試。這一層把「中間發生了什麼」從黑箱裡拿出來，
#           因此它的失敗模式是「顯示了錯的東西」而不是「壞掉」。
# 主要責任:
#   1. test_availability_* 依實際產物判斷分頁有無內容
#   2. test_missing_sections_explain_why 沒有內容時給原因而不是空白
#   3. test_preview_endpoint_rejects_traversal 路徑穿越必須 404
#   4. test_only_overview_loads_the_sse_script
#   5. test_case_page_shows_the_whole_chain 六段決策鏈都在
# 維護提醒:
#   - 不得讓 run_view 在讀不到檔時拋例外。一次 sim_smoke 本來就沒有 trace，
#     那是正常狀態；分頁要說明原因，不是回 500。
#   - 不得放寬 preview 端點的檔名白名單。它吃 URL 參數，
#     沒有限制就是一條讀取任意檔案的路徑。
#   - v0.1.0 新增：首版，對應 P2-4。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_run_sections.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

from pcmef.console import run_view  # noqa: E402
from pcmef.console.navigation import RUN_SECTIONS  # noqa: E402


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def bare_run(tmp_path) -> Path:
    """一次沒有 trace 也沒有報告的執行 —— 例如 sim_smoke。"""
    run = tmp_path / "runs" / "20260101T000000-aaaaaa"
    (run / "artifacts").mkdir(parents=True)
    (run / "log.txt").write_text("hello\n", encoding="utf-8")
    return run


@pytest.fixture()
def full_run(tmp_path) -> Path:
    """一次帶 trace、stress 與報告的 E2 執行。"""
    run = tmp_path / "runs" / "20260101T000000-bbbbbb"
    artifacts = run / "artifacts"
    _write(
        artifacts / "trace" / "trace_index.json",
        {
            "trace_schema_version": "decision_trace_v1",
            "run_status": "complete",
            "dry_run": True,
            "cases": [
                {"case_id": "case_0000", "row_index": 0, "condition": "clean",
                 "class_label": "Empty", "route": "fusion", "escalated": False,
                 "prediction": "Empty", "correct": True,
                 "trace_path": "cases/case_0000.json",
                 "preview_path": "previews/case_0000.png"},
                {"case_id": "case_0001", "row_index": 1, "condition": "conflict",
                 "class_label": "Misty", "route": "escalated", "escalated": True,
                 "prediction": "Bubbly", "correct": False,
                 "trace_path": "cases/case_0001.json",
                 "preview_path": "previews/case_0001.png"},
            ],
        },
    )
    _write(artifacts / "stress" / "stress_manifest.json",
           {"rows": [{}, {}], "counts": {"clean": 1, "conflict": 1}})
    _write(artifacts / "formal_e2_dry_run.json", {
        "report_id": "formal_e2_dry_run", "dry_run": True,
        "scientific_result": False, "llm_arm_evaluated": False,
        "results": {"vision_only": {"accuracy": 0.8, "macro_f1": 0.79}},
        "per_condition": {"vision_only": {"clean": {"macro_f1": 0.9},
                                          "conflict": {"macro_f1": 0.5}}},
        "worst_condition_macro_f1": {"vision_only": 0.5},
        "worst_condition_at": {"vision_only": "conflict"},
        "routing": {"counts": {"fusion": 1, "escalated": 1}, "escalated_cases": 1},
        "dataset": {"base": "x", "total_rows": 2},
    })
    (artifacts / "trace" / "previews").mkdir(parents=True, exist_ok=True)
    (artifacts / "trace" / "previews" / "case_0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return run


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------


def test_a_bare_run_still_has_overview_and_artifacts(bare_run):
    available = run_view.availability(bare_run, "sim_smoke")
    assert available["overview"] is True
    assert available["artifacts"] is True
    assert available["trace"] is False
    assert available["outputs"] is False


def test_a_full_run_has_every_section(full_run):
    available = run_view.availability(full_run, "formal_e2")
    assert all(available[section.key] for section in RUN_SECTIONS)


def test_missing_sections_explain_why_rather_than_being_empty(bare_run):
    """沒有內容不等於壞掉。分頁必須說出「這種 run 不產生那一層」。"""
    trace = run_view.trace_view(bare_run)
    assert trace["available"] is False
    assert "decision trace" in trace["reason"]
    outputs = run_view.outputs_view(bare_run)
    assert outputs["available"] is False
    assert outputs["reason"]


def test_reading_a_corrupt_json_does_not_raise(bare_run):
    """壞掉的檔案應該被當成「沒有」，而不是讓整頁 500。"""
    path = bare_run / "artifacts" / "formal_e2_report.json"
    path.write_text("{ not json", encoding="utf-8")
    assert run_view.outputs_view(bare_run)["available"] is False


# ---------------------------------------------------------------------------
# trace 篩選
# ---------------------------------------------------------------------------


def test_trace_lists_every_case_by_default(full_run):
    trace = run_view.trace_view(full_run)
    assert trace["available"] is True
    assert trace["total"] == 2
    assert trace["filtered"] == 2
    assert trace["correct"] == 1


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"condition": "clean"}, ["case_0000"]),
        ({"route": "escalated"}, ["case_0001"]),
        ({"condition": "conflict", "route": "escalated"}, ["case_0001"]),
        ({"condition": "clean", "route": "escalated"}, []),
    ],
)
def test_trace_filters_combine(full_run, kwargs, expected):
    trace = run_view.trace_view(full_run, **kwargs)
    assert [c["case_id"] for c in trace["cases"]] == expected


def test_trace_offers_the_available_filter_values(full_run):
    trace = run_view.trace_view(full_run)
    assert trace["conditions"] == ["clean", "conflict"]
    assert trace["routes"] == ["escalated", "fusion"]


# ---------------------------------------------------------------------------
# case 讀取
# ---------------------------------------------------------------------------


def test_case_neighbours_wire_up_the_ends(full_run):
    first = run_view.case_neighbours(full_run, "case_0000")
    assert first == {"previous": None, "next": "case_0001"}
    last = run_view.case_neighbours(full_run, "case_0001")
    assert last == {"previous": "case_0000", "next": None}


def test_a_case_id_cannot_escape_the_trace_directory(full_run):
    """case_id 來自 URL。它不得組成指向別處的路徑。"""
    assert run_view.case_view(full_run, "../../run") is None
    assert run_view.case_view(full_run, "nope") is None


# ---------------------------------------------------------------------------
# artifacts 摺疊
# ---------------------------------------------------------------------------


def test_a_crowded_directory_is_folded_into_one_row(tmp_path):
    """trace/previews 有幾百張 PNG；全部列出來只會變成一面檔名牆。"""
    run = tmp_path / "run"
    previews = run / "artifacts" / "trace" / "previews"
    previews.mkdir(parents=True)
    for index in range(30):
        (previews / f"case_{index:04d}.png").write_bytes(b"x")

    view = run_view.artifacts_view(run)
    folded = [e for e in view["entries"] if e["kind"] == "directory"]
    assert len(folded) == 1
    assert folded[0]["count"] == 30
    # 個別檔案不得再出現。
    assert not [e for e in view["entries"] if e["path"].endswith(".png")]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, full_run):
    from pcmef.admin.app import create_app
    from pcmef.console.runner import RunRecord

    # runner 的 run root 就是 full_run 的上層。
    app = create_app(
        registry_path=tmp_path / "r.db", vault_path=tmp_path / "v",
        console_run_root=full_run.parent,
    )
    app.config["TESTING"] = True
    record = RunRecord(
        run_id=full_run.name, kind="formal_e2", label="Formal E2 · dry-run",
        params={"mode": "dry-run"}, command=["pcmef", "formal", "run-e2"],
        status="succeeded", started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00", exit_code=0,
    )
    (full_run / "run.json").write_text(
        json.dumps(record.to_json()), encoding="utf-8"
    )
    (full_run / "log.txt").write_text("done\n", encoding="utf-8")
    return app.test_client()


@pytest.mark.parametrize("section", [s.key for s in RUN_SECTIONS])
def test_every_section_renders(client, full_run, section):
    response = client.get(f"/console/runs/{full_run.name}/{section}")
    assert response.status_code == 200


def test_an_unknown_section_is_404(client, full_run):
    assert client.get(f"/console/runs/{full_run.name}/nope").status_code == 404


def test_only_overview_loads_the_sse_script(client, full_run):
    """SSE 是為了看即時輸出而開的連線；靜態分頁不該開它。"""
    for section in (s.key for s in RUN_SECTIONS):
        body = client.get(
            f"/console/runs/{full_run.name}/{section}"
        ).get_data(as_text=True)
        expected = 1 if section == "overview" else 0
        assert body.count("<script") == expected, section


def test_preview_endpoint_serves_only_its_own_run(client, full_run):
    ok = client.get(f"/console/runs/{full_run.name}/trace/previews/case_0000.png")
    assert ok.status_code == 200
    assert ok.get_data()[:4] == b"\x89PNG"


@pytest.mark.parametrize(
    "filename",
    ["../run.json", "..%2Frun.json", "case_0000.json", "log.txt", "case_0000.png.txt"],
)
def test_preview_endpoint_rejects_anything_but_its_own_png(client, full_run, filename):
    response = client.get(
        f"/console/runs/{full_run.name}/trace/previews/{filename}"
    )
    assert response.status_code == 404, filename


def test_the_case_page_shows_the_whole_chain(client, full_run):
    """六段決策鏈都要在 —— 少一段就等於那一步仍是黑箱。"""
    _write(
        full_run / "artifacts" / "trace" / "cases" / "case_0000.json",
        {
            "trace_schema_version": "decision_trace_v1", "code_revision": "abc123",
            "case_id": "case_0000", "row_index": 0, "condition": "clean",
            "class_label": "Empty", "physical_scene_family": "Empty_f1",
            "inputs": {"rgb": {"sha256": "d" * 64, "original_shape": [64, 64, 3],
                               "preview_path": "previews/case_0000.png"},
                       "tof": {"shape": [500, 4], "channels": ["a", "b", "c", "d"]},
                       "stress_id": "x"},
            "perception": {"class_order": ["Empty", "Water-filled", "Bubbly", "Misty"],
                           "p_vision": [0.9, 0.1, 0.0, 0.0],
                           "p_tof": [0.8, 0.1, 0.1, 0.0],
                           "vision_argmax": "Empty", "tof_argmax": "Empty",
                           "temperatures": {"vision": 0.169, "tof": 0.0498}},
            "quality": {"Q_vision": 0.3, "Q_tof": 13.0, "U_vision": 0.2,
                        "U_tof": 0.3, "disagreement": 0.1, "_meaning": "m"},
            "reliability": {"q_vision": 0.8, "q_tof": 0.7, "threshold": 0.5,
                            "_meaning": "q is sensor reliability"},
            "routing": {"route": "fusion", "escalated": False, "consistent": True,
                        "implied_route": "fusion", "explanation": "兩側品質都過關",
                        "conditions": {"vision_quality_passes": True,
                                       "tof_quality_passes": True,
                                       "exceeds_disagreement_threshold": False,
                                       "disagreement_threshold": 0.4896}},
            "arms": {}, "agent_execution": {"invoked": False,
                                            "reason": "not_escalated", "artifacts": []},
            "final": {"distribution": [0.85, 0.1, 0.05, 0.0], "prediction_index": 0,
                      "prediction_label": "Empty", "truth_label": "Empty",
                      "correct": True, "s_a": None, "llm_called": False},
        },
    )
    body = client.get(
        f"/console/runs/{full_run.name}/trace/case_0000"
    ).get_data(as_text=True)
    for heading in ("1 · 輸入", "2 · 感知", "3 · 品質與可靠度",
                    "4 · 路由", "5 · 多代理仲裁", "6 · 最終決策"):
        assert heading in body, heading
    # 路由那一段必須帶理由，不能只有 route 名稱。
    assert "兩側品質都過關" in body
    # 非 escalated 要說明為什麼沒有叫 LLM。
    assert "證據夠就不叫 LLM" in body


def test_a_missing_case_is_404(client, full_run):
    assert client.get(
        f"/console/runs/{full_run.name}/trace/case_9999"
    ).status_code == 404
