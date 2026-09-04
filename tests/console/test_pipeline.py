# PC-MEF Research System source maintenance contract
# 上下游: 驗 pcmef/console/pipeline.py 的 build_pipeline()，
#         以及 pcmef/admin/templates/pipeline.html 的渲染。
# 檔案路徑: tests/console/test_pipeline.py
# 產生時間: 2026-09-04 15:10 +08:00
# 版本: v0.1.0
# 功能說明: 確認 Pipeline 頁顯示的是 lock 裡的實際值，而且值與出處對得上。
# 模組定位: P2-3 的驗收。這一頁的用途是讓凍結設定可被查證 ——
#           因此「值從哪裡來」與「值是多少」同等重要。
# 主要責任:
#   1. 每個節點的每一條事實都要有出處
#   2. 顯示的數字必須等於 lock 裡的字面值，不得寫死在程式裡
#   3. lock 讀不到時說明原因，不得拋例外或 500
#   4. 不得把 gate.lock 的 Q 尺度門檻呈現成路由門檻
# 維護提醒:
#   - 不得把 test_routing_shows_the_thresholds_actually_compared 改成
#     直接讀 gate.lock 的 q_vision_threshold。正式路徑走 reliability_route，
#     比較的是 q ≥ 0.5；照欄位名顯示會讓讀者拿到 1.32 這個錯的數字。
#   - 不得放寬 test_every_fact_cites_a_source。沒有出處的數字無從查證，
#     這一頁就失去存在理由。
#   - 本檔的 happy path 刻意讀真正的 freeze/ 目錄。改成假資料的話，
#     「顯示值等於 lock 值」就退化成自我比對。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_pipeline.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcmef.console.pipeline import PIPELINE_NODES, build_pipeline

REPO = Path(__file__).resolve().parents[2]
FREEZE = REPO / "freeze"


@pytest.fixture(scope="module")
def pipeline() -> dict:
    """對真正的 frozen lineage 建一次。唯讀，不寫入 freeze/。"""
    return build_pipeline(FREEZE)


def _facts(pipeline: dict, key: str, kind: str) -> list[dict]:
    node = next(n for n in pipeline["nodes"] if n["key"] == key)
    return node["detail"][kind]


def _value(pipeline: dict, key: str, kind: str, label: str) -> str:
    return next(f["value"] for f in _facts(pipeline, key, kind)
                if f["label"] == label)


def _lock(name: str) -> dict:
    resolved = json.loads((FREEZE / "ACTIVE_LINEAGE.json").read_text(encoding="utf-8"))
    directory = FREEZE.parent / resolved["resolved_freeze_dir"] \
        if "resolved_freeze_dir" in resolved else None
    for candidate in (directory, FREEZE / "runs" / "PFC-001", FREEZE):
        if candidate and (candidate / f"{name}.lock.json").exists():
            return json.loads(
                (candidate / f"{name}.lock.json").read_text(encoding="utf-8")
            )["payload"]
    raise AssertionError(f"{name}.lock.json not found")


# ---------------------------------------------------------------------------
# 結構
# ---------------------------------------------------------------------------


def test_the_seven_nodes_stay_in_data_flow_order(pipeline):
    """節點順序就是資訊本身，不得為畫面重排。"""
    assert [n["key"] for n in pipeline["nodes"]] == [
        "simulation", "paired", "perception", "reliability",
        "routing", "arbitration", "decision",
    ]
    assert len(PIPELINE_NODES) == 7


def test_every_node_has_all_three_sections(pipeline):
    assert pipeline["detail_available"] is True
    for node in pipeline["nodes"]:
        for kind in ("inputs", "process", "outputs"):
            assert node["detail"][kind], f"{node['key']}.{kind}"


def test_every_fact_cites_a_source(pipeline):
    """沒有出處的數字讀者無從查證。'—' 代表由上一步推導，也是明說。"""
    for node in pipeline["nodes"]:
        for kind in ("inputs", "process", "outputs"):
            for fact in node["detail"][kind]:
                assert fact["source"], f"{node['key']}.{kind}.{fact['label']}"


def test_the_resolved_directory_is_recorded_not_the_pointer(pipeline):
    """pointer 之後會改指別處，解析出來的目錄才是身分。"""
    assert pipeline["lineage"]["dir"].endswith("PFC-001")
    assert pipeline["lineage"]["pointer"].endswith("ACTIVE_LINEAGE.json")
    assert pipeline["lineage"]["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# 值必須等於 lock 的字面值
# ---------------------------------------------------------------------------


def test_the_numbers_come_from_the_locks(pipeline):
    """逐項比對 lock 的字面值，確認不是寫死在程式裡。"""
    sizing = _lock("e2_sample_size")
    assert _value(pipeline, "paired", "outputs", "總列數") \
        == str(sizing["total_condition_rows"])
    assert _value(pipeline, "paired", "inputs", "family 總數") \
        == str(sizing["family_domain"])

    stats = _lock("statistics_config")
    assert _value(pipeline, "decision", "process", "重抽單位") \
        == stats["resample_unit"]
    assert _value(pipeline, "decision", "process", "replicates / seed") \
        == f"{stats['bootstrap_replicates']} / {stats['bootstrap_seed']}"

    agent = _lock("agent_schema")
    assert _value(pipeline, "perception", "process", "class_order") \
        == "、".join(agent["class_order"])


def test_routing_shows_the_thresholds_actually_compared(pipeline):
    """正式路徑走 reliability_route：比 q ≥ 0.5 與 D ≤ δ。

    gate.lock 的 q_vision_threshold 是 Q 尺度的值（約 1.32），
    屬於 gate_route()，而該函式沒有呼叫者。照欄位名顯示會讓讀者
    拿到一個從未被比較過的數字。
    """
    process = _facts(pipeline, "routing", "process")
    margin = _value(pipeline, "routing", "process", "可靠門檻 q_m ≥")
    assert margin == str(_lock("reliability_final")["parameters"]["reliable_margin"])
    assert float(margin) == 0.5

    delta = _value(pipeline, "routing", "process", "分歧門檻 D ≤")
    assert float(delta) == pytest.approx(
        _lock("gate")["disagreement_threshold"], rel=1e-6
    )

    # Q 尺度的門檻不得出現在路由節點。
    gate = _lock("gate")
    forbidden = {f"{gate['q_vision_threshold']:.6g}", f"{gate['q_tof_threshold']:.6g}"}
    assert not forbidden & {f["value"] for f in process}


def test_the_page_warns_about_the_misleading_threshold_names(pipeline):
    assert "gate_route" in pipeline["threshold_caveat"]
    assert "沒有呼叫者" in pipeline["threshold_caveat"]


def test_the_partial_calibration_is_visible(pipeline):
    """E1 只有 Ambient 收斂，其餘階段的更新被抑制。

    這是整篇論文最重要的邊界之一，不能只寫在文件裡而畫面上看不到。
    """
    calib = _lock("calibrated_simulation")
    converged = _value(pipeline, "simulation", "process", "已收斂的校準階段")
    inhibited = _value(pipeline, "simulation", "process", "被抑制的階段")
    assert converged == "MAPPING_AMBIENT"
    assert inhibited == "、".join(calib["inhibited_stages"])
    assert "MAPPING_SIGNAL" in inhibited


def test_reliability_lists_what_may_not_become_a_feature(pipeline):
    """q 不得由 p(y|x) 推得，否則它只是第二個信心分數。"""
    forbidden = _value(pipeline, "reliability", "process", "禁止作為特徵")
    assert "max softmax probability" in forbidden


# ---------------------------------------------------------------------------
# 讀不到的情況
# ---------------------------------------------------------------------------


def test_an_unresolvable_lineage_explains_itself(tmp_path):
    """觀察頁不得 fail-closed 成一頁 500。

    fail-closed 屬於決策路徑。這裡 fail-closed 只換來使用者連
    「為什麼看不到」都不知道。
    """
    result = build_pipeline(tmp_path)
    assert result["detail_available"] is False
    assert result["detail_note"]
    assert len(result["nodes"]) == 7          # 靜態結構仍然要在
    assert all(n["detail"] is None for n in result["nodes"])


@pytest.fixture()
def sandboxed_freeze(tmp_path, monkeypatch) -> Path:
    """freeze/ 的可寫副本，且 cwd 也搬過去。

    ACTIVE_LINEAGE.json 的 `active_freeze_dir` 是 repo 相對路徑
    （`freeze/runs/PFC-001`），解析時相對 cwd 而不是相對傳入的 root。
    只複製目錄而不換 cwd 的話，pointer 讀的是副本、lock 讀的卻仍是本尊，
    測試會看起來通過但什麼都沒驗到。
    """
    shutil.copytree(FREEZE, tmp_path / "freeze")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "freeze"


def test_a_missing_optional_lock_is_named(sandboxed_freeze):
    """非必要的 lock 缺席時要指名道姓，不是默默顯示破折號。"""
    target = sandboxed_freeze / "runs" / "PFC-001" / "metric_config.lock.json"
    assert target.exists()
    target.unlink()

    result = build_pipeline("freeze")
    assert result["detail_available"] is True   # 其餘節點照常
    assert "metric_config" in result["detail_note"]


def test_a_corrupt_lock_does_not_raise(sandboxed_freeze):
    (sandboxed_freeze / "runs" / "PFC-001" / "metric_config.lock.json").write_text(
        "{ not json", encoding="utf-8"
    )
    result = build_pipeline("freeze")
    assert "metric_config" in result["detail_note"]


# ---------------------------------------------------------------------------
# 畫面
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    flask = pytest.importorskip("flask")  # noqa: F841
    from pcmef.admin.app import create_app

    app = create_app(registry_path=tmp_path / "r.db", vault_path=tmp_path / "v",
                     console_run_root=tmp_path / "runs")
    app.config["TESTING"] = True
    return app.test_client()


def test_the_page_renders_every_node_with_its_facts(client, pipeline):
    body = client.get("/pipeline").get_data(as_text=True)
    assert "Input / Process / Output" in body
    for node in PIPELINE_NODES:
        assert node.label in body
    # 出處標記必須出現在畫面上，不只存在於資料裡。
    for lock_name in ("gate.lock", "reliability_final.lock",
                      "statistics_config.lock", "e2_sample_size.lock"):
        assert lock_name in body


def test_the_page_has_no_controls(client):
    """Pipeline 說明流程。一個可以在這裡改的參數等於繞過 frozen config。"""
    body = client.get("/pipeline").get_data(as_text=True)
    for control in ("<form", "<button", "<input", "<select"):
        assert control not in body, control
