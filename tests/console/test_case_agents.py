# PC-MEF Research System source maintenance contract
# 上下游: 驗 pcmef/console/run_view.py 的 agents_view / agent_isolation /
#         role_detail / support_chain，以及 _case_agents.html 的渲染。
# 檔案路徑: tests/console/test_case_agents.py
# 產生時間: 2026-09-04 13:40 +08:00
# 版本: v0.1.0
# 功能說明: 確認 case 頁的多代理段落說的是「實際發生的事」。
# 模組定位: P2-5 的驗收。這一段是整個系統最像黑箱的地方 ——
#           它宣稱四個角色彼此隔離，而這裡驗證那個宣稱可被檢驗。
# 主要責任:
#   1. 隔離矩陣只由 input_payload 算出，不得回頭查契約
#   2. s_A 三列必須依類別名對齊，不是依 class_order 索引並排
#   3. 沒有真的呼叫過 agent 時要說明原因，不是渲染一張全 absent 的表
# 維護提醒:
#   - 不得把 test_isolation_reads_the_payload_not_the_contract 改成比對
#     ROLE_EVIDENCE_CONTRACT。那會變成契約自我證明，而契約與實作不一致
#     正是這張表要抓的東西。
#   - 不得把 CLASS_ORDER 改成字典序。改了之後「依索引並排」與「依名稱
#     對齊」會得到相同結果，s_A 的對齊測試就失去意義。
#   - 不得放寬 test_no_agent_call_means_no_matrix。沒發生過的呼叫湊出一張
#     全 absent 的表，看起來會像隔離失敗。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_case_agents.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

from pcmef.console import run_view  # noqa: E402

#: 刻意不是字典序：sorted() 會給 Bubbly / Empty / Misty / Water-filled，
#: 與此處的順序不同，因此「依索引並排」一定會錯位。
CLASS_ORDER = ["Empty", "Water-filled", "Bubbly", "Misty"]
S_A = [0.50, 0.30, 0.15, 0.05]


def _payload(role: str, **extra) -> dict:
    base = {
        "role": role,
        "schema_version": "1.0",
        "class_order": list(CLASS_ORDER),
        "evidence_contract_version": "role_evidence_contract_v2",
        "representation_mode": "FIXED_SUMMARY",
    }
    base.update(extra)
    return base


@pytest.fixture()
def artifacts() -> list[dict]:
    """四個角色的實際送出內容，照 ROLE_EVIDENCE_CONTRACT_V2 的形狀。"""
    return [
        {
            "role": "observation_agent",
            "input_payload": _payload(
                "observation_agent",
                tof_summary={"channel_order": ["distance_mm"]},
                sensing_quality_cues={"Q_vision": 0.3, "Q_tof": 13.0,
                                      "_meaning": "raw sensing quality"},
            ),
            "received_image": True,
            "image_digests": ["a" * 64],
            "raw_output": {"observed_facts": ["x"]},
            "validated_output": {"observed_facts": ["x"], "schema_version": "1.0"},
            "attempt_count": 1,
        },
        {
            "role": "physics_agent",
            "input_payload": _payload(
                "physics_agent",
                tof_summary={"channel_order": ["distance_mm"]},
                observation_brief={"observed_facts": ["x"]},
                calibrated_class_probabilities={"tof": [0.1, 0.2, 0.3, 0.4],
                                                "_meaning": "p(y|x)"},
                modality_reliability={"q_tof": 0.46, "_meaning": "q_m"},
                sensing_quality_cues={"Q_tof": 13.0, "_meaning": "Q_m"},
                predictive_entropy={"U_tof": 0.0, "_meaning": "U_m"},
            ),
            "received_image": False,
            "image_digests": [],
            "raw_output": {"modality": "physics"},
            "validated_output": {"modality": "physics", "schema_version": "1.0"},
            "attempt_count": 1,
        },
        {
            "role": "visual_semantic_agent",
            "input_payload": _payload(
                "visual_semantic_agent",
                observation_brief={"observed_facts": ["x"]},
                calibrated_class_probabilities={"vision": [0.4, 0.3, 0.2, 0.1],
                                                "_meaning": "p(y|x)"},
                modality_reliability={"q_vision": 0.49, "_meaning": "q_m"},
                sensing_quality_cues={"Q_vision": 0.28, "_meaning": "Q_m"},
                predictive_entropy={"U_vision": 0.1, "_meaning": "U_m"},
            ),
            "received_image": True,
            "image_digests": ["b" * 64],
            "raw_output": {"modality": "vision"},
            "validated_output": {"modality": "vision", "schema_version": "1.0"},
            "attempt_count": 2,
        },
        {
            "role": "arbitration_agent",
            "input_payload": _payload(
                "arbitration_agent",
                observation_brief={"observed_facts": ["x"]},
                calibrated_class_probabilities={"vision": [0.4, 0.3, 0.2, 0.1],
                                                "tof": [0.1, 0.2, 0.3, 0.4]},
                modality_reliability={"q_vision": 0.49, "q_tof": 0.46},
                sensing_quality_cues={"Q_vision": 0.28, "Q_tof": 13.0},
                predictive_entropy={"U_vision": 0.1, "U_tof": 0.0},
                cross_modal={"D": 0.62, "_meaning": "disagreement"},
                anonymous_proposals=[{"modality": "A"}, {"modality": "B"}],
            ),
            "received_image": False,
            "image_digests": [],
            "raw_output": {"class_support": {"Empty": 50.0, "Water-filled": 30.0,
                                             "Bubbly": 15.0, "Misty": 5.0}},
            "validated_output": {
                "class_support": {"Empty": 50.0, "Water-filled": 30.0,
                                  "Bubbly": 15.0, "Misty": 5.0},
                "support_sum_before_normalisation": 100.0,
                "support_sum_within_tolerance": True,
                "conflict_tag": "none", "schema_version": "1.0",
            },
            "attempt_count": 1,
        },
    ]


def _cells(isolation: dict, field: str) -> dict[str, str]:
    row = next(r for r in isolation["rows"] if r["field"] == field)
    return dict(zip(isolation["roles"], row["cells"]))


# ---------------------------------------------------------------------------
# 隔離矩陣
# ---------------------------------------------------------------------------


def test_isolation_reads_the_payload_not_the_contract(artifacts):
    """把某個欄位從 payload 拿掉，矩陣就必須顯示它不在。

    如果實作是查 ROLE_EVIDENCE_CONTRACT，這裡仍會顯示「有」——
    那正是這張表要抓的不一致。
    """
    before = _cells(run_view.agent_isolation(artifacts), "cross_modal")
    assert before["arbitration_agent"] == "present"

    stripped = [dict(a) for a in artifacts]
    stripped[3] = dict(stripped[3])
    stripped[3]["input_payload"] = {
        k: v for k, v in stripped[3]["input_payload"].items() if k != "cross_modal"
    }
    after = _cells(run_view.agent_isolation(stripped), "cross_modal")
    assert after["arbitration_agent"] == "absent"


def test_each_role_only_sees_its_own_side(artifacts):
    """模態限定的欄位要顯示實際帶了哪一側，而不只是「有」。

    「physics 收到 p(y|x)」與「physics 只收到 ToF 那一側」是兩件事，
    混成同一格就看不出隔離。
    """
    isolation = run_view.agent_isolation(artifacts)
    probabilities = _cells(isolation, "calibrated_class_probabilities")
    assert probabilities["physics_agent"] == "tof"
    assert probabilities["visual_semantic_agent"] == "vision"
    assert probabilities["arbitration_agent"] == "vision、tof"
    assert probabilities["observation_agent"] == "absent"


def test_the_arbiter_sees_no_raw_evidence(artifacts):
    """仲裁者只看衍生量與兩份匿名意見，不看原始證據。"""
    isolation = run_view.agent_isolation(artifacts)
    assert _cells(isolation, "tof_summary")["arbitration_agent"] == "absent"
    assert _cells(isolation, "image")["arbitration_agent"] == "absent"
    assert _cells(isolation, "anonymous_proposals")["arbitration_agent"] == "present"


def test_gate_route_reaches_nobody(artifacts):
    """route 是 benchmark metadata。任何角色收到它都是資訊洩漏。"""
    assert set(_cells(run_view.agent_isolation(artifacts), "gate_route").values()) \
        == {"absent"}


def test_a_row_stays_listed_even_when_every_cell_is_absent(artifacts):
    """全部 absent 的欄位仍要出現 —— 那一列的空白正是它要證明的事。"""
    fields = [r["field"] for r in run_view.agent_isolation(artifacts)["rows"]]
    assert "gate_route" in fields


# ---------------------------------------------------------------------------
# s_A 的形成
# ---------------------------------------------------------------------------


def test_support_chain_keys_s_a_by_class_name(artifacts):
    """s_a 是照 class_order 排的向量，必須改以類別名為鍵。

    畫面上的欄位依名稱排序。直接把向量並排會得到一張欄位對不上標頭的
    表：Bubbly 那一欄會顯示 Empty 的機率。
    """
    chain = run_view.support_chain(artifacts, {"s_a": S_A})
    assert chain["s_a"] == {"Empty": 0.50, "Water-filled": 0.30,
                            "Bubbly": 0.15, "Misty": 0.05}
    # 依索引並排會讓 Bubbly 拿到 0.50（class_order 的第一個）。
    assert chain["s_a"]["Bubbly"] != S_A[0]


def test_support_chain_reports_the_raw_sum_and_tolerance(artifacts):
    """原始總和偏離 100 是模型沒照指示配額，要看得到。"""
    chain = run_view.support_chain(artifacts, {"s_a": S_A})
    assert chain["raw_sum"] == 100.0
    assert chain["within_tolerance"] is True
    assert chain["conflict_tag"] == "none"


def test_support_chain_measures_the_epsilon_shift(artifacts):
    """ε_s 的位移要量出來，不是用文字宣稱。

    六位小數的表格上 ② 和 ③ 常常長得一模一樣；印出實際位移才能說明
    那一步真的發生過。
    """
    chain = run_view.support_chain(artifacts, {"s_a": [0.5001, 0.2999, 0.15, 0.05]})
    assert chain["eps_shift"] == pytest.approx(1e-4, rel=1e-6)


def test_support_chain_is_absent_without_an_arbiter(artifacts):
    assert run_view.support_chain(artifacts[:3], {"s_a": S_A}) is None


# ---------------------------------------------------------------------------
# 角色明細
# ---------------------------------------------------------------------------


def test_role_detail_does_not_repeat_the_envelope(artifacts):
    """role / schema_version / class_order 四個角色都一樣，
    逐欄位列出只是四倍的雜訊 —— 它們在標題列顯示一次。"""
    detail = run_view.role_detail(artifacts[1])
    keys = [f["key"] for f in detail["fields"]]
    for envelope in ("role", "schema_version", "class_order",
                     "evidence_contract_version", "representation_mode"):
        assert envelope not in keys
    assert detail["class_order"] == CLASS_ORDER
    assert detail["contract_version"] == "role_evidence_contract_v2"


def test_role_detail_uses_meaning_as_the_field_description(artifacts):
    """`_meaning` 是 payload 內建的自述欄位，當說明用，不混進值裡。"""
    field = next(f for f in run_view.role_detail(artifacts[1])["fields"]
                 if f["key"] == "modality_reliability")
    assert field["meaning"] == "q_m"
    assert "_meaning" not in field["compact"]
    assert "q_tof" in field["compact"]


# ---------------------------------------------------------------------------
# 未執行的情況
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "execution",
    [
        {"invoked": False, "reason": "not_escalated", "artifacts": []},
        {"invoked": False, "reason": "dry_run", "artifacts": []},
        {"invoked": True, "reason": None, "artifacts": []},
    ],
)
def test_no_agent_call_means_no_matrix(execution):
    """沒有真的呼叫過就回 None。

    湊一張全 absent 的表會看起來像隔離失敗，而事實是它根本沒發生。
    """
    assert run_view.agents_view({"agent_execution": execution}) is None


def test_agents_view_totals_the_attempts(artifacts):
    view = run_view.agents_view({
        "agent_execution": {"invoked": True, "artifacts": artifacts},
        "final": {"s_a": S_A},
    })
    assert view["total_attempts"] == 5  # 1 + 1 + 2 + 1，含重試
    assert len(view["roles"]) == 4


# ---------------------------------------------------------------------------
# 畫面
# ---------------------------------------------------------------------------


def _case(execution: dict, final: dict) -> dict:
    return {
        "trace_schema_version": "decision_trace_v1", "code_revision": "abc123",
        "case_id": "case_0000", "row_index": 0, "condition": "conflict",
        "class_label": "Empty", "physical_scene_family": "Empty_f1",
        "inputs": {"rgb": {"sha256": "d" * 64, "original_shape": [64, 64, 3]},
                   "tof": {"shape": [500, 4], "channels": ["a"]}, "stress_id": "x"},
        "perception": {"class_order": CLASS_ORDER, "p_vision": [0.4, 0.3, 0.2, 0.1],
                       "p_tof": [0.1, 0.2, 0.3, 0.4], "vision_argmax": "Empty",
                       "tof_argmax": "Misty", "temperatures": {}},
        "quality": {"Q_vision": 0.28, "Q_tof": 13.0, "U_vision": 0.1, "U_tof": 0.0,
                    "disagreement": 0.62},
        "reliability": {"q_vision": 0.49, "q_tof": 0.46, "threshold": 0.5},
        "routing": {"route": "escalate", "escalated": True, "consistent": True,
                    "implied_route": "escalate", "explanation": "兩側的感測品質都不過關",
                    "conditions": {}},
        "arms": {}, "agent_execution": execution, "final": final,
    }


@pytest.fixture()
def client(tmp_path):
    from pcmef.admin.app import create_app
    from pcmef.console.runner import RunRecord

    root = tmp_path / "runs"
    run = root / "20260101T000000-cccccc"
    (run / "artifacts" / "trace" / "cases").mkdir(parents=True)
    record = RunRecord(
        run_id=run.name, kind="formal_e2", label="Formal E2",
        params={"mode": "full"}, command=["pcmef", "formal", "run-e2"],
        status="succeeded", started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00", exit_code=0,
    )
    (run / "run.json").write_text(json.dumps(record.to_json()), encoding="utf-8")
    (run / "log.txt").write_text("done\n", encoding="utf-8")
    app = create_app(registry_path=tmp_path / "r.db", vault_path=tmp_path / "v",
                     console_run_root=root,
        workspace_root=tmp_path / "workspace",
    )
    app.config["TESTING"] = True
    return app.test_client(), run


def _render(client, case: dict) -> str:
    test_client, run = client
    path = run / "artifacts" / "trace" / "cases" / "case_0000.json"
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    response = test_client.get(f"/console/runs/{run.name}/trace/case_0000")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_the_page_shows_the_isolation_matrix(client, artifacts):
    body = _render(client, _case(
        {"invoked": True, "reason": None, "artifacts": artifacts},
        {"s_a": S_A, "distribution": S_A, "prediction_index": 0,
         "prediction_label": "Empty", "truth_label": "Empty", "correct": True,
         "llm_called": True},
    ))
    assert "角色隔離" in body
    assert "不是查" in body  # 說明這張表的來源是 payload
    for role in ("observation_agent", "physics_agent",
                 "visual_semantic_agent", "arbitration_agent"):
        assert role in body
    # s_A 三列都在，且 Bubbly 欄拿到的是 0.15 而不是 class_order 第一個。
    assert "0.150000" in body
    assert "① 模型原始輸出" in body and "③" in body


def test_the_page_explains_why_no_agent_ran(client):
    body = _render(client, _case(
        {"invoked": False, "reason": "dry_run", "artifacts": [],
         "note": "零成本預演"},
        {"s_a": None, "distribution": S_A, "prediction_index": 0,
         "prediction_label": "Empty", "truth_label": "Empty", "correct": True,
         "llm_called": False},
    ))
    assert "角色隔離" not in body
    assert "零成本預演" in body


def test_a_missing_number_does_not_take_down_the_page(client, artifacts):
    """缺一個數值欄位只該讓那一格顯示破折號，不是整頁 500。

    Jinja 的 `Undefined is not none` 會回 True，所以 `is not none` 擋不住
    缺席的鍵 —— 分支照樣進去，然後在 format() 裡炸掉。整條決策鏈因為
    少一個溫度就全部看不到，是最糟的失敗方式。
    """
    case = _case(
        {"invoked": True, "reason": None, "artifacts": artifacts},
        {"s_a": S_A, "distribution": S_A, "prediction_index": 0,
         "prediction_label": "Empty", "truth_label": "Empty", "correct": True,
         "llm_called": True},
    )
    case["perception"].pop("temperatures")
    case["quality"] = {}
    case["reliability"] = {}
    case["routing"]["conditions"] = {}
    body = _render(client, case)
    # 其他段落照常，多代理那一段完全不受影響。
    for heading in ("1 · 輸入", "3 · 品質與可靠度", "5 · 多代理仲裁", "6 · 最終決策"):
        assert heading in body, heading
    assert "角色隔離" in body
    assert "—" in body


def test_the_full_payload_is_reachable_without_javascript(client, artifacts):
    """完整值收在 <details> 裡。摺疊是為了可讀，不是為了藏 ——
    而 <details> 不需要 JS 就能展開。"""
    body = _render(client, _case(
        {"invoked": True, "reason": None, "artifacts": artifacts},
        {"s_a": S_A, "distribution": S_A, "prediction_index": 0,
         "prediction_label": "Empty", "truth_label": "Empty", "correct": True,
         "llm_called": True},
    ))
    assert "完整值" in body
    assert body.count("<script") == 0
