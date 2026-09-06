# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 P1-4~9 的六項 —— 逐臂結果、case gallery、
#         Q/q 語意、Cost/Cache 欄位、Status 的推導階段、圖表匯出入口。
#         以 Flask test client 與合成 trace 為輸入；不啟動子行程、
#         不呼叫 provider、不觸碰 families 36-43。
# 檔案路徑: tests/console/test_ui_transparency_p1.py
# 產生時間: 2026-09-05 17:20 +08:00
# 版本: v0.1.0
# 功能說明: 這六項都是「畫面說得出來 vs 說不出來」的差別，而說不出來時
#           畫面看起來完全正常 —— 因此每一項都需要一條測試。
# 模組定位: NOTE-080~083 的可執行防線。
# 主要責任:
#   1. test_dry_run_escalated_marks_g5_not_executed 不得顯示填位結果
#   2. test_all_five_arms_are_recorded G1-G5 一條都不能少
#   3. test_route_explanation_calls_q_reliability_not_quality
#   4. test_the_cost_page_shows_calls_avoided_and_provenance_errors
#   5. test_status_derives_the_stage_from_the_gate
#   6. test_the_figures_endpoint_reuses_the_shared_renderer
# 維護提醒:
#   - 不得放寬 test_dry_run_escalated_marks_g5_not_executed。dry run 的
#     escalated 列 F(x) 等於 fixed fusion；把它當成 G5 顯示出來，讀起來
#     會像「PC-MEF 沒有比較好」，而它其實從未執行。
#   - 不得讓圖表端點自己畫圖。與 CLI 共用 renderer 是「圖上的數字能在
#     report 裡逐字找到」的唯一保證。
#   - v0.1.0 新增：首版，對應 P1-4~9。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_ui_transparency_p1.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect
import json

import pytest

from pcmef.console.runner import RunSpec

flask = pytest.importorskip("flask")

CLASS_ORDER = ["Empty", "Water-filled", "Bubbly", "Misty"]


# ---------------------------------------------------------------------------
# P1-4 逐臂結果
# ---------------------------------------------------------------------------


def _outcomes(*, escalated: bool, dry_run: bool):
    from pcmef.experiments.decision_trace import _arm_outcomes

    arms = {
        "vision_only": [0.7, 0.1, 0.1, 0.1],
        "tof_only": [0.1, 0.6, 0.2, 0.1],
        "fixed_fusion": [0.4, 0.35, 0.15, 0.1],
        "reliability_routing": [0.4, 0.35, 0.15, 0.1],
        "pcmef_full": [0.05, 0.05, 0.8, 0.1],
    }
    return _arm_outcomes(arms, CLASS_ORDER, escalated=escalated, dry_run=dry_run)


def test_all_five_arms_are_recorded():
    outcomes = _outcomes(escalated=True, dry_run=False)
    assert list(outcomes) == [
        "vision_only", "tof_only", "fixed_fusion",
        "reliability_routing", "pcmef_full",
    ]
    assert [o["tag"] for o in outcomes.values()] == ["G1", "G2", "G3", "G4", "G5"]


def test_each_executed_arm_carries_a_prediction():
    outcomes = _outcomes(escalated=False, dry_run=False)
    assert outcomes["vision_only"]["prediction"] == "Empty"
    assert outcomes["tof_only"]["prediction"] == "Water-filled"
    assert outcomes["pcmef_full"]["prediction"] == "Bubbly"
    assert all(o["executed"] for o in outcomes.values())


def test_dry_run_escalated_marks_g5_not_executed():
    """dry run 的 escalated 列，G5 必須是 NOT EXECUTED 且**不帶分布**。

    那一列的 F(x) 是填位值，恰好等於 fixed fusion。顯示出來會讀成
    「PC-MEF 沒有比較好」，而它其實從未執行。
    """
    outcomes = _outcomes(escalated=True, dry_run=True)
    g5 = outcomes["pcmef_full"]
    assert g5["executed"] is False
    assert g5["distribution"] is None, "未執行的臂不得帶分布 —— 帶了就會有人畫出來"
    assert g5["prediction"] is None
    assert "填位值" in g5["reason"]

    # 其餘四條在 dry run 下**完全有效**，不得一併標成未執行。
    for key in ("vision_only", "tof_only", "fixed_fusion", "reliability_routing"):
        assert outcomes[key]["executed"] is True, key


def test_dry_run_non_escalated_keeps_g5():
    """non-escalated 的列 F(x) 依定義等於 p_trad，那不是填位值。"""
    assert _outcomes(escalated=False, dry_run=True)["pcmef_full"]["executed"] is True


def test_the_executor_records_all_five_arms():
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal._execute_formal_e2)
    for arm in ("vision_only", "tof_only", "fixed_fusion",
                "reliability_routing", "pcmef_full"):
        assert f'"{arm}":' in source, arm


# ---------------------------------------------------------------------------
# P1-6 Q / q 語意
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q_v, q_t, route",
    [(0.8, 0.2, "trust_vision"), (0.2, 0.8, "trust_tof"),
     (0.8, 0.8, "fusion"), (0.2, 0.2, "escalated")],
)
def test_route_explanation_calls_q_reliability_not_quality(q_v, q_t, route):
    """q 是可靠度，不是感測品質。

    Q 才是感測品質，而且它不在 [0,1]（實測 Q_T 到 17）。把 q 唸成
    「感測品質」會讓畫面自己與 trace 的 `_meaning` 欄位矛盾。
    """
    from pcmef.experiments.decision_trace import route_explanation

    sentence = route_explanation(route, q_v, q_t, 0.1, delta=0.49)["explanation"]
    assert "可靠度" in sentence, sentence
    assert "感測品質" not in sentence, sentence


def test_the_trace_meaning_fields_still_separate_q_and_lowercase_q():
    """trace 自帶的說明必須把兩者分開，否則畫面說得再對也沒有依據。"""
    from pcmef.experiments.decision_trace import build_case_trace

    source = inspect.getsource(build_case_trace)
    assert "Q is raw sensing quality" in source
    assert "q is sensor reliability" in source


# ---------------------------------------------------------------------------
# P1-7 Cost / Cache
# ---------------------------------------------------------------------------


def test_the_cache_summary_exposes_calls_avoided_and_provenance_errors():
    from pcmef.agents.cached_case import CacheIdentity, CachedArbiter

    arbiter = CachedArbiter(
        cache=object(),
        identity=CacheIdentity(model_id="m", provider_revision="r",
                               runtime_config_hash="h" * 64),
    )
    arbiter.hits = 3
    arbiter.misses = 1
    arbiter.provenance_errors.append("OSError: read-only filesystem")

    summary = arbiter.summary()
    assert summary["provider_calls_avoided"] == 12      # 3 命中 × 四個角色
    assert summary["provenance_errors"] == ["OSError: read-only filesystem"]


def test_the_cost_template_renders_both_fields():
    from pathlib import Path

    body = Path("pcmef/admin/templates/_run_cost.html").read_text(encoding="utf-8")
    assert "provider_calls_avoided" in body
    assert "provenance_errors" in body
    # 沒有錯誤時也要說一句，否則使用者分不出「沒有錯誤」與「沒有檢查」。
    assert "provenance 完整" in body


# ---------------------------------------------------------------------------
# P1-8 Status 的推導階段
# ---------------------------------------------------------------------------


def test_status_derives_the_stage_from_the_gate():
    """階段、下一步與後續順序必須從八項判定推導，不是手寫。"""
    from pcmef.experiments.final_gate import progress

    result = progress("freeze/runs/PFC-001")
    assert result["total"] == 8
    assert result["cleared"] + result["remaining"] == 8
    assert result["ready_for_final_e2"] is False
    # 下一步就是第一個未解除的，順序即必須解除的順序。
    assert result["next_step"]["check"] == result["blockers"][0]["check"]
    assert result["next_step"]["why"]


def test_status_progress_uses_the_same_judgement_as_preflight():
    """畫面與 CLI 不得給出不同的答案。"""
    from pcmef.experiments.final_gate import evaluate, progress

    checks = evaluate("freeze/runs/PFC-001")
    derived = progress("freeze/runs/PFC-001")
    assert derived["cleared"] == sum(1 for c in checks if c["passed"])
    assert [b["check"] for b in derived["blockers"]] == [
        c["check"] for c in checks if not c["passed"]
    ]


def test_the_status_page_does_not_500_when_the_gate_cannot_be_read():
    """Status 是觀察頁。判定失敗時說明原因，不得整頁掛掉。

    取實際註冊的 view function，不取 `workspace_routes.page` —— status、
    results 與 pipeline 三個 blueprint 的 view 都叫 `page`，module 層級的
    那個名字指向最後定義的一個（pipeline）。
    """
    from pcmef.admin.app import create_app

    view = create_app(environ={}).view_functions["status.page"]
    source = inspect.getsource(view)
    assert "final_progress" in source
    assert "except Exception" in source
    assert '"error"' in source


# ---------------------------------------------------------------------------
# P1-9 圖表匯出
# ---------------------------------------------------------------------------


def test_the_figures_endpoint_reuses_the_shared_renderer():
    """不得在 Web 端另寫一份繪圖 —— 兩份會分岔，而分岔的圖看起來很正常。"""
    from pcmef.console import routes

    source = inspect.getsource(routes.export_figures)
    assert "from pcmef.reporting import figures" in source
    assert "figures.export_all(report, out_dir)" in source
    # 不得重算指標。
    assert "run_formal_e2_full" not in source
    assert "prepare_cases" not in source


def test_the_figures_endpoint_is_registered_and_guarded(tmp_path, start_attributed):
    """兩層守衛各驗一次，不靠同一個狀態碼證明兩件事。

    歸屬先於 CSRF：不屬於目前 Project 的 run 一律 404，而且**不存在的
    run 與別人的 run 長得一樣** —— 否則不必有權限也能列舉出跑過什麼。
    CSRF 則要在一筆確實屬於自己的 run 上驗，這樣它擋下來的才確定是
    「沒有 token」，不是「沒有這筆 run」。
    """
    from pcmef.admin.app import create_app

    app = create_app(
        registry_path=tmp_path / "r.db", vault_path=tmp_path / "v",
        console_run_root=tmp_path / "runs",
        workspace_root=tmp_path / "workspace",
        environ={},
    )
    rules = {str(r): r for r in app.url_map.iter_rules()}
    rule = rules["/api/console/runs/<run_id>/figures"]
    assert "POST" in rule.methods

    app.config["TESTING"] = True
    client = app.test_client()

    # 不屬於這個 Project 的 run：404，且與不存在的無從分辨。
    assert client.post(
        "/api/console/runs/whatever/figures", json={}
    ).status_code == 404

    # 自己的 run，但沒有 token：403。寫入端點必須受 CSRF 保護。
    record = start_attributed(app, RunSpec(kind="sim_smoke", params={"lines": 1}))
    app.config["PCMEF_CONSOLE_RUNNER"].wait(record.run_id, timeout=30)
    assert client.post(
        f"/api/console/runs/{record.run_id}/figures", json={}
    ).status_code == 403


def test_the_figures_endpoint_refuses_a_run_without_a_report(tmp_path):
    """沒有報告就沒有圖。錯誤要說得出原因，不是丟一個空檔案夾。"""
    from pcmef.admin.app import create_app
    from pcmef.admin.routes_llm import CSRF_SESSION_KEY
    from pcmef.console.runner import RunRecord

    run_root = tmp_path / "runs"
    (run_root / "r1").mkdir(parents=True)
    record = RunRecord(
        run_id="r1", kind="formal_e2", label="", params={"mode": "dry-run"},
        command=[], status="succeeded", exit_code=0,
    )
    (run_root / "r1" / "run.json").write_text(
        json.dumps(record.to_json()), encoding="utf-8"
    )

    app = create_app(registry_path=tmp_path / "r.db", vault_path=tmp_path / "v",
                     console_run_root=run_root, environ={})
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session[CSRF_SESSION_KEY] = "tok"

    response = client.post(
        "/api/console/runs/r1/figures", headers={"X-CSRF-Token": "tok"}, json={}
    )
    assert response.status_code == 400
    assert "沒有可用的報告" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# P1-5 case gallery
# ---------------------------------------------------------------------------


def _write_trace(root, case_id, *, condition, family, class_label="Empty"):
    cases = root / "trace" / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    (cases / f"{case_id}.json").write_text(json.dumps({
        "case_id": case_id, "row_index": int(case_id[-4:]),
        "condition": condition, "class_label": class_label,
        "physical_scene_family": family,
        "inputs": {
            "rgb": {"sha256": "a" * 64, "original_shape": [64, 64, 3],
                    "preview_path": f"previews/{case_id}.png"},
            "tof": {"shape": [500, 4],
                    "channels": ["distance_mm", "ambient_rate_mcps",
                                 "signal_rate_mcps", "sigma_like"]},
            "stress_id": f"{case_id}__{condition}",
        },
        "quality": {"Q_vision": 0.28, "Q_tof": 13.0},
        "routing": {"route": "fusion"},
    }), encoding="utf-8")
    return {"case_id": case_id, "row_index": int(case_id[-4:]),
            "condition": condition, "route": "fusion"}


def test_the_gallery_shows_rgb_tof_and_hash(tmp_path):
    from pcmef.console import run_view

    root = tmp_path / "artifacts"
    rows = [
        _write_trace(root, "case_0000", condition="clean", family="fam_00"),
        _write_trace(root, "case_0001", condition="conflict", family="fam_01"),
    ]
    (root / "trace" / "trace_index.json").write_text(
        json.dumps({"cases": rows}), encoding="utf-8"
    )

    gallery = run_view.case_gallery(tmp_path)
    assert gallery["available"] is True
    assert gallery["total"] == 2
    first = gallery["cases"][0]
    assert first["rgb_shape"] == [64, 64, 3]
    assert first["rgb_sha256"].startswith("aaaa")
    assert first["tof_shape"] == [500, 4]
    assert len(first["tof_channels"]) == 4
    assert first["preview_path"].endswith(".png")


def test_the_gallery_filters_by_condition_and_family(tmp_path):
    from pcmef.console import run_view

    root = tmp_path / "artifacts"
    rows = [
        _write_trace(root, "case_0000", condition="clean", family="fam_00"),
        _write_trace(root, "case_0001", condition="conflict", family="fam_01"),
        _write_trace(root, "case_0002", condition="conflict", family="fam_00"),
    ]
    (root / "trace" / "trace_index.json").write_text(
        json.dumps({"cases": rows}), encoding="utf-8"
    )

    assert run_view.case_gallery(tmp_path)["matched"] == 3
    assert run_view.case_gallery(tmp_path, condition="conflict")["matched"] == 2
    assert run_view.case_gallery(tmp_path, family="fam_00")["matched"] == 2
    both = run_view.case_gallery(tmp_path, condition="conflict", family="fam_00")
    assert both["matched"] == 1
    assert both["cases"][0]["case_id"] == "case_0002"
    # 篩選選項要涵蓋全部資料，不是只涵蓋當前結果 —— 否則篩過一次就回不去。
    assert set(both["conditions"]) == {"clean", "conflict"}
    assert set(both["families"]) == {"fam_00", "fam_01"}


def test_the_gallery_says_why_it_is_empty(tmp_path):
    from pcmef.console import run_view

    gallery = run_view.case_gallery(tmp_path)
    assert gallery["available"] is False
    assert "decision trace" in gallery["reason"]


def test_the_gallery_does_not_read_the_dataset(tmp_path, monkeypatch):
    """preview 是 run 當下的 artifact；這一頁不得回頭讀原始資料集。"""
    import numpy as np

    monkeypatch.setattr(np, "load", lambda *a, **k: pytest.fail("讀了 dataset"))
    from pcmef.console import run_view

    root = tmp_path / "artifacts"
    rows = [_write_trace(root, "case_0000", condition="clean", family="fam_00")]
    (root / "trace" / "trace_index.json").write_text(
        json.dumps({"cases": rows}), encoding="utf-8"
    )
    run_view.case_gallery(tmp_path)
