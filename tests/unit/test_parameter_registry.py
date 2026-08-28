# PC-MEF Research System source maintenance contract
# 上下游: 測 pcmef.core.parameters 與其兩個接線點（simulation.scenario 的 formal
#         建構、surrogate.calibration.assert_formal_ready）；另測 simulation
#         manifest 是否確實帶上 parameter_set_hash。
# 檔案路徑: tests/unit/test_parameter_registry.py
# 產生時間: 2026-08-28 10:20 +08:00
# 版本: v0.1.0
# 功能說明: 用「會失敗的」測試證明參數防線真的擋得住 —— registry 不見、
#           留著 placeholder、confounded group 未裁決、parameter_set_hash 對不上、
#           以及 registry 宣稱值與程式實際值不符，五種情況都必須 FAIL。
# 模組定位: 參數防線的反向驗收。它「不是」registry 內容的正確性測試 ——
#           哪個參數該標什麼狀態由 NOTE-030/031/032 裁決，本檔只驗防線行為。
# 主要責任:
#   1. 乾淨的 registry 必須 PASS（否則以下所有負向測試都是空的）
#   2. missing / placeholder / unlocked / hash mismatch / drift 五種必須 FAIL
#   3. CONVENTION 不得成為繞過防線的萬用鑰匙
#   4. derived 常數不得被 calibration 覆寫
# 維護提醒:
#   - 不得刪除 test_a_clean_registry_passes；少了它，其餘負向測試會在
#     「防線永遠拒絕一切」的情況下全部通過，等於什麼都沒驗到。
#   - 不得把負向測試改成只斷言 raises 而不檢查訊息內容；
#     擋下來的理由必須指名道姓，否則交接的人不知道要修什麼。
#   - v0.1.0 新增：首版參數防線驗收（NOTE-030）。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_parameter_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pcmef.core.parameters import (
    GAUGE_STATUS,
    ParameterRegistry,
    ParameterRegistryError,
    assert_formal_ready,
    binding_coverage,
    formal_blocking_reasons,
    live_value_drift,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_REGISTRY = REPO_ROOT / "configs" / "parameter_registry.yaml"


def _parameter(name: str, **overrides) -> dict:
    entry = {
        "name": name,
        "source": f"test/{name}",
        "role": "test parameter",
        "value": 1.0,
        "provenance_status": "CONFIRMED",
        "kind": "fixed",
        "class_scope": "shared",
        "formal_blocking": True,
    }
    entry.update(overrides)
    return entry


def _clean_registry() -> dict:
    """一組乾淨、可進 formal 的合成 registry。

    名稱刻意不與真實參數重疊，因此 live-value 綁定不會誤命中。
    """
    return {
        "registry_version": "test-v1",
        "parameters": [
            _parameter("alpha"),
            _parameter("beta"),
            _parameter(
                "gauge_scale",
                provenance_status=GAUGE_STATUS,
                formal_blocking=False,
            ),
        ],
        "confounded_groups": [
            {
                "id": "TG-1",
                "physical_quantity": "test product",
                "members": ["gauge_scale", "alpha"],
                "relation": "q = gauge_scale * alpha",
                "decision": {
                    "status": "RESOLVED",
                    "decided_on": "2026-08-28",
                    "fixed": ["gauge_scale"],
                    "calibrated": ["alpha"],
                    "rationale": "the shared scale carries the redundant degree of freedom",
                    "claim_boundary": "alpha is only meaningful relative to the fixed gauge",
                },
            }
        ],
    }


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 正向：乾淨的 registry 必須過
# ---------------------------------------------------------------------------


def test_a_clean_registry_passes(tmp_path):
    """沒有這一條，底下所有負向測試都可能是空的。"""
    registry = ParameterRegistry.load(_write(tmp_path, _clean_registry()))
    assert formal_blocking_reasons(registry, check_live=False) == []
    assert_formal_ready(registry, check_live=False)


def test_hash_is_stable_and_order_independent(tmp_path):
    payload = _clean_registry()
    first = ParameterRegistry.load(_write(tmp_path, payload)).parameter_set_hash()

    shuffled = json.loads(json.dumps(payload))
    shuffled["parameters"] = list(reversed(shuffled["parameters"]))
    second_dir = tmp_path / "shuffled"
    second_dir.mkdir()
    second = ParameterRegistry.load(_write(second_dir, shuffled)).parameter_set_hash()
    assert first == second


def test_hash_covers_the_confounded_group_decision(tmp_path):
    """同樣的數值配上不同的「固定哪一項」，是兩組不同的實驗設定。"""
    payload = _clean_registry()
    before = ParameterRegistry.load(_write(tmp_path, payload)).parameter_set_hash()

    swapped = json.loads(json.dumps(payload))
    swapped["confounded_groups"][0]["decision"]["fixed"] = ["alpha"]
    swapped["confounded_groups"][0]["decision"]["calibrated"] = ["gauge_scale"]
    after = ParameterRegistry.load(_write(tmp_path, swapped)).parameter_set_hash()
    assert before != after


# ---------------------------------------------------------------------------
# 負向一：registry 不見
# ---------------------------------------------------------------------------


def test_missing_registry_fails(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ParameterRegistryError) as error:
        ParameterRegistry.load(missing)
    message = str(error.value)
    assert "not found" in message
    # 訊息必須說明「為什麼沒有 registry 不等於零個未校準參數」，
    # 否則下一棒會以為補一個空檔案就好。
    assert "not the same as zero" in message


def test_missing_registry_blocks_via_reasons(monkeypatch, tmp_path):
    """formal_blocking_reasons 在預設路徑不存在時也必須回報，而不是拋到外面。"""
    from pcmef.core import parameters as module

    monkeypatch.setattr(module, "DEFAULT_REGISTRY_PATH", tmp_path / "absent.yaml")
    reasons = formal_blocking_reasons(check_live=False)
    assert len(reasons) == 1
    assert "not found" in reasons[0]


# ---------------------------------------------------------------------------
# 負向二：留著 placeholder / 未解決狀態
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["PLACEHOLDER", "UNKNOWN", "RECONSTRUCTED"])
def test_unresolved_status_fails(tmp_path, status):
    payload = _clean_registry()
    payload["parameters"][0]["provenance_status"] = status
    payload["parameters"][0]["kind"] = "calibration_only"
    payload["parameters"][0]["allowed_range"] = [0.0, 2.0]
    registry = ParameterRegistry.load(_write(tmp_path, payload))

    with pytest.raises(ParameterRegistryError) as error:
        assert_formal_ready(registry, check_live=False)
    message = str(error.value)
    assert "alpha" in message and status in message


def test_calibration_only_must_declare_allowed_range(tmp_path):
    payload = _clean_registry()
    payload["parameters"][0]["kind"] = "calibration_only"
    with pytest.raises(ParameterRegistryError) as error:
        ParameterRegistry.load(_write(tmp_path, payload))
    assert "allowed_range" in str(error.value)


# ---------------------------------------------------------------------------
# 負向三：confounded group 未裁決 / 裁決不完整
# ---------------------------------------------------------------------------


def test_undecided_confounded_group_fails(tmp_path):
    payload = _clean_registry()
    payload["confounded_groups"][0].pop("decision")
    payload["parameters"][2]["provenance_status"] = "CONFIRMED"  # 排除 gauge 那條理由
    registry = ParameterRegistry.load(_write(tmp_path, payload))

    with pytest.raises(ParameterRegistryError) as error:
        assert_formal_ready(registry, check_live=False)
    assert "no recorded decision" in str(error.value)
    assert "TG-1" in str(error.value)


def test_blocked_group_blocks_formal_but_is_a_valid_decision(tmp_path):
    payload = _clean_registry()
    payload["parameters"][2]["provenance_status"] = "CONFIRMED"
    payload["confounded_groups"][0]["decision"] = {
        "status": "BLOCKED",
        "decided_on": "2026-08-28",
        "rationale": "the observable it would be calibrated against is the wrong quantity",
        "blocked_reason": "observable definition error",
        "unblock_requires": ["redefine the observable"],
    }
    registry = ParameterRegistry.load(_write(tmp_path, payload))

    assert registry.groups[0].decided is True
    assert registry.groups[0].formal_ready is False
    with pytest.raises(ParameterRegistryError) as error:
        assert_formal_ready(registry, check_live=False)
    assert "BLOCKED" in str(error.value)


def test_resolved_group_without_claim_boundary_is_rejected(tmp_path):
    payload = _clean_registry()
    payload["confounded_groups"][0]["decision"].pop("claim_boundary")
    with pytest.raises(ParameterRegistryError) as error:
        ParameterRegistry.load(_write(tmp_path, payload))
    assert "claim_boundary" in str(error.value)


def test_resolved_group_must_assign_every_member(tmp_path):
    payload = _clean_registry()
    payload["confounded_groups"][0]["decision"]["calibrated"] = []
    with pytest.raises(ParameterRegistryError) as error:
        ParameterRegistry.load(_write(tmp_path, payload))
    assert "neither fixed nor calibrated" in str(error.value)


def test_resolved_group_must_fix_at_least_one_member(tmp_path):
    payload = _clean_registry()
    payload["confounded_groups"][0]["decision"]["fixed"] = []
    payload["confounded_groups"][0]["decision"]["calibrated"] = ["gauge_scale", "alpha"]
    with pytest.raises(ParameterRegistryError) as error:
        ParameterRegistry.load(_write(tmp_path, payload))
    assert "infinitely many equivalent solutions" in str(error.value)


def test_blocked_group_must_state_a_route_out(tmp_path):
    payload = _clean_registry()
    payload["confounded_groups"][0]["decision"] = {
        "status": "BLOCKED",
        "decided_on": "2026-08-28",
        "rationale": "because",
        "blocked_reason": "because",
        "unblock_requires": [],
    }
    with pytest.raises(ParameterRegistryError) as error:
        ParameterRegistry.load(_write(tmp_path, payload))
    assert "unblock_requires" in str(error.value)


# ---------------------------------------------------------------------------
# 負向四：parameter_set_hash 對不上
# ---------------------------------------------------------------------------


def test_hash_mismatch_fails(tmp_path):
    registry = ParameterRegistry.load(_write(tmp_path, _clean_registry()))
    correct = registry.parameter_set_hash()

    assert_formal_ready(registry, expected_hash=correct, check_live=False)

    with pytest.raises(ParameterRegistryError) as error:
        assert_formal_ready(registry, expected_hash="0" * 64, check_live=False)
    message = str(error.value)
    assert "parameter_set_hash mismatch" in message
    assert correct in message


def test_changing_a_value_changes_the_hash(tmp_path):
    payload = _clean_registry()
    before = ParameterRegistry.load(_write(tmp_path, payload)).parameter_set_hash()
    payload["parameters"][0]["value"] = 2.0
    after = ParameterRegistry.load(_write(tmp_path, payload)).parameter_set_hash()
    assert before != after


def test_prose_edits_do_not_change_the_hash(tmp_path):
    """修一個錯字不該讓所有既有 lock 失效。"""
    payload = _clean_registry()
    before = ParameterRegistry.load(_write(tmp_path, payload)).parameter_set_hash()
    payload["parameters"][0]["role"] = "a longer, clearer description"
    payload["parameters"][0]["note"] = "see NOTE-030"
    after = ParameterRegistry.load(_write(tmp_path, payload)).parameter_set_hash()
    assert before == after


# ---------------------------------------------------------------------------
# 負向五：CONVENTION 不得成為萬用鑰匙
# ---------------------------------------------------------------------------


def test_convention_without_a_sanctioning_group_fails(tmp_path):
    """把 PLACEHOLDER 改標 CONVENTION 不會讓它過關。"""
    payload = _clean_registry()
    payload["parameters"][1]["provenance_status"] = GAUGE_STATUS
    registry = ParameterRegistry.load(_write(tmp_path, payload))

    with pytest.raises(ParameterRegistryError) as error:
        assert_formal_ready(registry, check_live=False)
    message = str(error.value)
    assert "beta" in message
    assert "not the fixed member of any RESOLVED confounded group" in message


def test_convention_needs_the_group_to_be_resolved_not_blocked(tmp_path):
    payload = _clean_registry()
    payload["confounded_groups"][0]["decision"] = {
        "status": "BLOCKED",
        "decided_on": "2026-08-28",
        "rationale": "insufficient basis",
        "blocked_reason": "insufficient basis",
        "unblock_requires": ["get the datasheet"],
    }
    registry = ParameterRegistry.load(_write(tmp_path, payload))
    with pytest.raises(ParameterRegistryError) as error:
        assert_formal_ready(registry, check_live=False)
    assert "gauge_scale" in str(error.value)


# ---------------------------------------------------------------------------
# 負向六：registry 宣稱值與程式實際值不符
# ---------------------------------------------------------------------------


def test_live_value_drift_is_detected(monkeypatch):
    from pcmef.simulation import mitsuba_adapter as ma

    registry = ParameterRegistry.load(REAL_REGISTRY)
    assert live_value_drift(registry) == [], "the shipped registry must match the code"

    monkeypatch.setattr(ma, "_ROOM_LIGHT_RATIO", 0.99)
    drift = live_value_drift(registry)
    assert any("_ROOM_LIGHT_RATIO" in item for item in drift)

    with pytest.raises(ParameterRegistryError) as error:
        assert_formal_ready(registry)
    assert "disagree with the code actually in use" in str(error.value)


def test_an_unbound_registry_entry_is_reported_not_ignored(monkeypatch):
    """新增建模常數卻忘了登記，必須看得見。"""
    registry = ParameterRegistry.load(REAL_REGISTRY)
    coverage = binding_coverage(registry)
    assert coverage["unbound"] == [], (
        "every registry parameter must be bound either to a code constant or to a "
        f"scenario config field; unbound: {coverage['unbound']}"
    )
    assert len(coverage["code"]) + len(coverage["config_supplied"]) == len(
        registry.parameters
    )


# ---------------------------------------------------------------------------
# 真實 registry 的現況與接線點
# ---------------------------------------------------------------------------


def test_the_shipped_registry_loads_and_is_not_formal_ready():
    registry = ParameterRegistry.load(REAL_REGISTRY)
    counts = registry.counts()
    assert counts["total"] == 38
    assert counts["groups_undecided"] == 0, "every confounded group must be adjudicated"
    reasons = formal_blocking_reasons(registry)
    assert reasons, (
        "the parameter set is not calibrated yet; if this ever passes, check that "
        "it passed because the parameters were resolved and not because the "
        "firewall was loosened"
    )


def test_optical_path_to_distance_is_derived_not_calibratable():
    from pcmef.surrogate.calibration import (
        CalibrationError,
        PLACEHOLDER_SMOKE_CALIBRATION,
    )

    calibration = PLACEHOLDER_SMOKE_CALIBRATION
    assert calibration.optical_path_to_distance.derived is True
    assert calibration.optical_path_to_distance.placeholder is False
    assert "optical_path_to_distance" not in calibration.placeholder_names()
    assert "optical_path_to_distance" not in calibration.calibratable_names()

    with pytest.raises(CalibrationError) as error:
        calibration.with_calibrated(optical_path_to_distance=0.42)
    assert "derived from the scene geometry" in str(error.value)

    registry = ParameterRegistry.load(REAL_REGISTRY)
    entry = registry.by_name()["optical_path_to_distance"]
    assert entry.kind == "derived"
    assert entry.formal_blocking is False


def test_surrogate_formal_gate_consults_the_registry():
    """九個 scale 全部校準完，仍會因為場景側的常數被擋下。"""
    from pcmef.surrogate.calibration import (
        CalibrationError,
        PLACEHOLDER_SMOKE_CALIBRATION,
    )

    fully_calibrated = PLACEHOLDER_SMOKE_CALIBRATION.with_calibrated(
        **{
            name: 1.0
            for name in PLACEHOLDER_SMOKE_CALIBRATION.calibratable_names()
        }
    )
    assert fully_calibrated.placeholder_names() == []
    fully_calibrated.assert_formal_ready(check_registry=False)

    with pytest.raises(CalibrationError) as error:
        fully_calibrated.assert_formal_ready()
    assert "not formal-ready" in str(error.value)


def test_scenario_formal_gate_consults_the_registry():
    """介質參數沒有 placeholder，仍不得建立 formal scenario。"""
    from pcmef.simulation.scenario import ScenarioConfig, ScenarioConfigError

    with pytest.raises(ScenarioConfigError) as error:
        ScenarioConfig(
            class_label="Water-filled",
            seed=1,
            medium_parameters={"turbidity": 0.05},  # 沒有 placeholder 標記
            formal=True,
        )
    assert "not formal-ready" in str(error.value)


def test_simulation_manifest_records_the_parameter_set(tmp_path):
    from pcmef.simulation.controller import SimulationController

    controller = SimulationController()
    path = controller.write_manifest([], tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))

    block = manifest["parameter_registry"]
    assert block["available"] is True
    registry = ParameterRegistry.load(REAL_REGISTRY)
    assert block["parameter_set_hash"] == registry.parameter_set_hash()
    assert block["confounded_groups"]["CG-3_ambient"]["status"] == "BLOCKED"
    assert manifest["run_identity_hash"] != manifest["manifest_hash"]


def test_run_identity_hash_moves_when_the_parameter_set_moves(tmp_path, monkeypatch):
    """換掉建模常數但 scenario 內容不變時，run 身分必須改變。"""
    from pcmef.simulation import controller as controller_module
    from pcmef.simulation.controller import SimulationController

    controller = SimulationController()
    first = json.loads(
        controller.write_manifest([], tmp_path / "a").read_text(encoding="utf-8")
    )

    real = ParameterRegistry.load(REAL_REGISTRY)
    shifted = ParameterRegistry(
        registry_version=real.registry_version + "-shifted",
        parameters=real.parameters,
        groups=real.groups,
    )
    monkeypatch.setattr(
        controller_module,
        "_parameter_registry_block",
        lambda: {**shifted.summary(), "available": True},
    )
    second = json.loads(
        controller.write_manifest([], tmp_path / "b").read_text(encoding="utf-8")
    )

    assert first["manifest_hash"] == second["manifest_hash"]
    assert first["run_identity_hash"] != second["run_identity_hash"]
