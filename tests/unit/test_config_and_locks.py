# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；操作 core.config 與 core.locks，讀寫 tmp_path 內
#         即時產生的 YAML 與 freeze 目錄；不觸碰 repo 內的 configs/ 或 freeze/。
# 檔案路徑: tests/unit/test_config_and_locks.py
# 產生時間: 2026-08-25 21:40 +08:00
# 版本: v0.1.0
# 功能說明: 驗證兩件事不可能發生 —— 待教授裁決的數值被一個預設值悄悄蓋過去，
#           以及某個凍結步驟在它的前置步驟還沒完成時就被寫入。
# 模組定位: 研究完整性的回歸防線。它不驗證 lock 內容的科學正確性，
#           只驗證流程順序與不可竄改性。
# 主要責任:
#   1. Required sentinel 在 get、布林與數值語境下一律中斷
#   2. get(key, default) 不得吸收待裁決值
#   3. formal 模式的全樹掃描與 CLI override 禁令
#   4. lock 的必要欄位、冪等重寫、異內容拒絕與竄改偵測
#   5. lock 前置順序（含 real_split_policy 與 synthetic_split_policy 兩條）
#   6. lock 相依圖必須完整登錄且無環
# 維護提醒:
#   - 不得放寬 default 不吸收 sentinel 那條；一旦放寬，呼叫端只要順手寫個 default
#     就能繞過整個 formal-blocking 機制。
#   - 新增 lock 時要在此補一條 required_keys 缺漏必失敗的案例。
#   - v0.1.0 新增：首版 config 與 lock 回歸測試。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_config_and_locks.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.core.config import (
    ConfigError,
    FormalBlockingError,
    Required,
    load_config,
)
from pcmef.core.locks import LOCK_SPECS, LockError, LockOrderError, LockStore


# ---------------------------------------------------------------------------
# Config：formal-blocking
# ---------------------------------------------------------------------------


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_required_sentinel_survives_load_and_blocks_read(tmp_path):
    path = _write(
        tmp_path,
        "base.yaml",
        """
splits:
  allocation: !required "advisor decision pending (SRC-PLAN 3.1)"
  seed: 1042
""",
    )
    config = load_config([path])
    assert config.get("splits.seed") == 1042
    with pytest.raises(FormalBlockingError):
        config.get("splits.allocation")


def test_default_argument_cannot_bypass_a_required_value(tmp_path):
    """NOTE-005：default 只在 key 不存在時生效，不得吸收掉待裁決的值。"""
    path = _write(tmp_path, "base.yaml", "gate:\n  delta: !required\n")
    config = load_config([path])
    with pytest.raises(FormalBlockingError):
        config.get("gate.delta", 0.5)
    assert config.get("gate.missing_key", "fallback") == "fallback"


def test_required_sentinel_raises_in_boolean_and_numeric_contexts():
    sentinel = Required(key="statistics.bootstrap_B")
    with pytest.raises(FormalBlockingError):
        bool(sentinel)
    with pytest.raises(FormalBlockingError):
        float(sentinel)
    with pytest.raises(FormalBlockingError):
        int(sentinel)


def test_formal_mode_refuses_to_start_with_unresolved_values(tmp_path):
    """SRC-SAI NFR-04：缺值必須在啟動時就失敗，不能等到數小時的 simulation 之後。"""
    path = _write(
        tmp_path,
        "experiment.yaml",
        """
e2:
  final_n_per_class: !required
statistics:
  bootstrap_replicates: !required
""",
    )
    with pytest.raises(FormalBlockingError) as excinfo:
        load_config([path], formal=True)
    message = str(excinfo.value)
    assert "e2.final_n_per_class" in message
    assert "statistics.bootstrap_replicates" in message


def test_formal_mode_accepts_a_fully_resolved_config(tmp_path):
    path = _write(
        tmp_path,
        "experiment.yaml",
        "e2:\n  final_n_per_class: 25\nstatistics:\n  bootstrap_replicates: 2000\n",
    )
    config = load_config([path], formal=True)
    assert config.unresolved() == []
    assert config.get("e2.final_n_per_class") == 25


def test_cli_overrides_are_forbidden_in_formal_mode(tmp_path):
    """SRC-SAI §33：formal 模式下 CLI 不得覆蓋會改變研究結果的參數。"""
    path = _write(tmp_path, "base.yaml", "gate:\n  alpha: 0.4\n")
    with pytest.raises(ConfigError):
        load_config([path], overrides={"gate.alpha": "0.9"}, formal=True)


def test_override_precedence_follows_declared_order(tmp_path):
    base = _write(tmp_path, "base.yaml", "sim:\n  bins: 100\n  seed: 1\n")
    module = _write(tmp_path, "module.yaml", "sim:\n  bins: 200\n")
    config = load_config([base, module], overrides={"sim.seed": "7"})
    assert config.get("sim.bins") == 200
    assert config.get("sim.seed") == 7


def test_config_hash_distinguishes_resolved_from_unresolved(tmp_path):
    pending = _write(tmp_path, "a.yaml", "gate:\n  delta: !required\n")
    resolved = _write(tmp_path, "b.yaml", "gate:\n  delta: 0.25\n")
    assert load_config([pending]).config_hash() != load_config([resolved]).config_hash()


def test_config_hash_is_stable_across_key_order(tmp_path):
    first = _write(tmp_path, "a.yaml", "gate:\n  alpha: 0.4\n  beta: 0.3\n")
    second = _write(tmp_path, "b.yaml", "gate:\n  beta: 0.3\n  alpha: 0.4\n")
    assert load_config([first]).config_hash() == load_config([second]).config_hash()


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def _split_policy_payload() -> dict:
    return {
        "creation_phase": "AFTER_M0_BEFORE_ANY_CALIBRATION",
        "split_unit": "recording",
        "stratification": "class",
        "group_rule": "seeded_stratified_recording",
        "allocation": {"calibration": 0.5, "heldout_real": 0.5},
        "minimum_per_class": 10,
        "seed": 1042,
        "eligible_set_hash": "a" * 64,
        "calibration_set_hash": "b" * 64,
        "heldout_real_set_hash": "c" * 64,
        "redraw_policy": "FORBIDDEN_AFTER_LOCK",
    }


def test_lock_write_and_load_round_trip(tmp_path):
    store = LockStore(tmp_path)
    store.write("real_split_policy", _split_policy_payload())
    loaded = store.load("real_split_policy")
    assert loaded["seed"] == 1042


def test_lock_rejects_missing_required_keys(tmp_path):
    store = LockStore(tmp_path)
    payload = _split_policy_payload()
    del payload["heldout_real_set_hash"]
    with pytest.raises(LockError, match="missing required keys"):
        store.write("real_split_policy", payload)


def test_lock_rewrite_with_identical_content_is_idempotent(tmp_path):
    store = LockStore(tmp_path)
    store.write("real_split_policy", _split_policy_payload())
    store.write("real_split_policy", _split_policy_payload())
    assert store.exists("real_split_policy")


def test_lock_rewrite_with_different_content_is_refused(tmp_path):
    """SRC-SAI §23：lock 一旦建立即不可變更，改內容必須開新 run。"""
    store = LockStore(tmp_path)
    store.write("real_split_policy", _split_policy_payload())
    changed = _split_policy_payload()
    changed["seed"] = 9999
    with pytest.raises(LockError, match="immutable"):
        store.write("real_split_policy", changed)


def test_lock_detects_post_freeze_tampering(tmp_path):
    store = LockStore(tmp_path)
    path = store.write("real_split_policy", _split_policy_payload())
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["seed"] = 4242
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(LockError, match="integrity check"):
        store.load("real_split_policy")


def test_lock_prerequisites_are_enforced(tmp_path):
    """SRC-SAI Appendix H1：缺 real_split_policy.lock 時不得進行 calibration 相關 freeze。"""
    store = LockStore(tmp_path)
    with pytest.raises(LockOrderError, match="real_split_policy"):
        store.write(
            "initial_simulation",
            {
                "scene_hash": "a" * 64,
                "surrogate_hash": "b" * 64,
                "code_version": "git:abc123",
                "parameter_ranges": {},
            },
        )


def test_synthetic_split_policy_requires_e1_outcome(tmp_path):
    """SRC-SAI FR-P0-03：synthetic split 必須在 E1 outcome 之後。"""
    store = LockStore(tmp_path)
    with pytest.raises(LockOrderError):
        store.write(
            "synthetic_split_policy",
            {
                "parent_scene_family_rule": "clean-base-scene",
                "family_hashes": [],
                "split_assignment_hash": "d" * 64,
            },
        )


def test_lock_refuses_secret_values(tmp_path):
    """SRC-SAI NFR-08 / LLM-SEC-01：lock 只能存 secret_ref，不能存 secret 值。"""
    store = LockStore(tmp_path)
    payload = _split_policy_payload()
    payload["api_key"] = "sk-not-a-real-key-0123456789"
    with pytest.raises(LockError, match="secret"):
        store.write("real_split_policy", payload)


def test_lock_allows_secret_ref(tmp_path):
    store = LockStore(tmp_path)
    payload = _split_policy_payload()
    payload["secret_ref"] = "env:GEMINI_API_KEY"
    store.write("real_split_policy", payload)
    assert store.load("real_split_policy")["secret_ref"] == "env:GEMINI_API_KEY"


def test_unknown_lock_name_is_refused(tmp_path):
    store = LockStore(tmp_path)
    with pytest.raises(LockError, match="unknown lock"):
        store.write("not_a_real_lock", {})


def test_every_lock_prerequisite_is_itself_registered():
    """相依圖不得指向未登錄的 lock，否則狀態機會有無人把關的缺口。"""
    for name, spec in LOCK_SPECS.items():
        for dependency in spec.requires:
            assert dependency in LOCK_SPECS, (
                f"lock {name!r} requires unregistered lock {dependency!r}"
            )


def test_lock_dependency_graph_is_acyclic():
    resolved: set[str] = set()

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in path:
            raise AssertionError(f"cycle detected in lock graph: {path + (name,)}")
        if name in resolved:
            return
        for dependency in LOCK_SPECS[name].requires:
            visit(dependency, path + (name,))
        resolved.add(name)

    for lock_name in LOCK_SPECS:
        visit(lock_name, ())
