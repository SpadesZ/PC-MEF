# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.experiments.final_gate 的八項判定，
#         以 tmp_path 上的合成 amendment / lock / 報告為輸入；另外對
#         repo 內實際生效的 lineage 跑一次。
#         **不生成、不讀取、不預覽、不掃描 families 36-43。**
# 檔案路徑: tests/e2/test_final_gate.py
# 產生時間: 2026-09-05 16:05 +08:00
# 版本: v0.1.0
# 功能說明: STATUS.md 列的 Final E2 八項前置條件必須是可執行的閘門。
# 模組定位: NOTE-077 的可執行防線。這八項先前只寫在 STATUS.md 的散文裡 ——
#           文件說「必須先凍結」，程式沒有一行檢查它。
# 主要責任:
#   1. test_all_eight_are_evaluated 八項一個都不能少
#   2. test_every_precondition_is_blocking 沒有一項是「參考用」
#   3. test_nothing_defaults_to_pass 缺檔一律 FAIL
#   4. test_stub_only_validation_does_not_pass real_calls=0 不算驗證過
#   5. test_provisional_snapshot_is_not_golden canonical=false 必須擋
#   6. test_the_gate_survives_unreadable_inputs 讀檔失敗不得讓閘門消失
#   7. test_the_real_lineage_reports_what_is_still_missing
# 維護提醒:
#   - 不得因為「反正還沒到那一步」而讓任何一項預設 PASS。這八項的用途正是
#     在還沒到那一步時擋住。
#   - 不得在本檔或 final_gate 讀 families 36-43 的觀測資料。第八項只看
#     manifest 的 identity 欄位。
#   - v0.1.0 新增：首版，對應 P0-3 / NOTE-077。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_final_gate.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.experiments.final_gate import (
    FINAL_E2_PRECONDITIONS, REQUIRED_AMENDMENTS, evaluate,
)

FINAL_INDICES = tuple(range(36, 44))


def _by_key(results):
    return {r["check"].split(".", 1)[1]: r for r in results}


# ---------------------------------------------------------------------------
# 形狀
# ---------------------------------------------------------------------------


def test_all_eight_are_evaluated(tmp_path):
    results = evaluate(tmp_path)
    assert len(results) == 8
    assert len(FINAL_E2_PRECONDITIONS) == 8
    assert set(_by_key(results)) == {
        "amd007_real_provider_validation", "amd007_frozen", "amd008_frozen",
        "amd009_frozen", "llm_runtime_refrozen", "formal_config_refrozen",
        "canonical_golden_baseline", "final_partition_manifest",
    }


def test_every_precondition_is_blocking(tmp_path):
    """沒有一項可以是「參考用」。八項都是 Final E2 的前提。"""
    assert all(r["blocking"] for r in evaluate(tmp_path))


def test_every_precondition_explains_what_to_do(tmp_path):
    """只寫 FAIL 的閘門會讓人不知道下一步該做什麼。"""
    for result in evaluate(tmp_path):
        assert result["why"], result["check"]
        assert result["label"], result["check"]


def test_nothing_defaults_to_pass(tmp_path):
    """什麼都不存在 = 八項全 FAIL。

    三個路徑都要明確指向空目錄。`real_validation_path` 與
    `snapshot_path` 的預設值是 repo 內的固定位置，**刻意不隨 freeze_dir
    改變** —— real-provider validation 驗的是 runtime（prompt / schema /
    provider），不是 lineage；把它綁到 lineage 反而會讓換一次 corrective
    lineage 就得重跑一次付費驗證。lineage 與 runtime 的關聯由第五項
    （llm_runtime_refrozen）負責。
    """
    results = evaluate(
        tmp_path,
        real_validation_path=tmp_path / "absent.json",
        snapshot_path=tmp_path / "absent-snapshot.json",
    )
    assert not any(r["passed"] for r in results), [
        r["check"] for r in results if r["passed"]
    ]


def test_the_validation_path_is_not_scoped_to_the_lineage():
    """第一項刻意讀固定路徑，不隨 freeze_dir 改變。

    這是設計而不是疏漏：換一次 corrective lineage 不該要求重跑一次付費的
    real-provider validation。而「lock 記的是不是通過驗證的那一組」由第五項
    負責 —— 那一項確實讀 freeze_dir。
    """
    from pcmef.experiments.final_gate import REAL_VALIDATION_PATH

    assert "freeze" not in REAL_VALIDATION_PATH.as_posix()
    assert REAL_VALIDATION_PATH.as_posix().startswith("outputs/")


def test_the_three_required_amendments_are_named():
    assert REQUIRED_AMENDMENTS == ("AMD-007", "AMD-008", "AMD-009")


# ---------------------------------------------------------------------------
# 各項判準
# ---------------------------------------------------------------------------


def _validation(tmp_path, **overrides):
    path = tmp_path / "real_agent_validation.json"
    document = {
        "READY_FOR_FINAL_E2": "YES",
        "all_checks_passed": True,
        "real_calls": 8,
        "checks": [],
        "prompt_hashes": {"observation_agent": "p1"},
        "schema_hashes": {"observation_brief_v1": "s1"},
        "runtime_config_hash": "r1",
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_stub_only_validation_does_not_pass(tmp_path):
    """`real_calls == 0` 代表整份報告是 stub 跑出來的。

    少了這一條，一次完全沒有碰過真實 provider 的驗證也會通過 ——
    而這一項的全部意義就是排除那種情況。
    """
    path = _validation(tmp_path, real_calls=0)
    result = _by_key(evaluate(tmp_path, real_validation_path=path))[
        "amd007_real_provider_validation"
    ]
    assert result["passed"] is False
    assert "real_calls=0" in result["detail"]


def test_a_not_ready_validation_does_not_pass(tmp_path):
    path = _validation(tmp_path, READY_FOR_FINAL_E2="NO")
    assert not _by_key(evaluate(tmp_path, real_validation_path=path))[
        "amd007_real_provider_validation"
    ]["passed"]


def test_a_failed_check_does_not_pass(tmp_path):
    path = _validation(
        tmp_path, all_checks_passed=False,
        checks=[{"check": "physics_receives_no_vision_evidence", "passed": False}],
    )
    result = _by_key(evaluate(tmp_path, real_validation_path=path))[
        "amd007_real_provider_validation"
    ]
    assert result["passed"] is False
    assert "physics_receives_no_vision_evidence" in result["detail"]


def test_provisional_snapshot_is_not_golden(tmp_path):
    """`canonical: false` 必須擋。provisional 不是 Golden Baseline。"""
    snapshot = tmp_path / "snap.json"
    snapshot.write_text(
        json.dumps({"canonical": False, "canonical_blockers": ["a", "b"]}),
        encoding="utf-8",
    )
    result = _by_key(evaluate(tmp_path, snapshot_path=snapshot))[
        "canonical_golden_baseline"
    ]
    assert result["passed"] is False
    assert "canonical=false" in result["detail"]

    snapshot.write_text(json.dumps({"canonical": True}), encoding="utf-8")
    assert _by_key(evaluate(tmp_path, snapshot_path=snapshot))[
        "canonical_golden_baseline"
    ]["passed"]


def test_the_final_manifest_must_match_the_locked_identity(tmp_path):
    """只比對 manifest 的 identity 欄位，不讀任何觀測資料。"""
    good = evaluate(
        tmp_path,
        generated_manifest={
            "scenario_set_hash": "abc", "family_indices": list(FINAL_INDICES),
        },
        expected_scenario_set_hash="abc",
        final_family_indices=FINAL_INDICES,
    )
    assert _by_key(good)["final_partition_manifest"]["passed"]

    for manifest in (
        {"scenario_set_hash": "wrong", "family_indices": list(FINAL_INDICES)},
        {"scenario_set_hash": "abc", "family_indices": [20, 21]},
    ):
        result = _by_key(evaluate(
            tmp_path, generated_manifest=manifest,
            expected_scenario_set_hash="abc", final_family_indices=FINAL_INDICES,
        ))["final_partition_manifest"]
        assert result["passed"] is False


def test_prompt_agreement_alone_does_not_pass_llm_runtime(tmp_path):
    """prompt/schema 相符是必要但不充分。

    timeout 與 connection topology 不在 prompt hash 裡，因此 prompt 相符
    推論不出 runtime 相符。無法證明時必須 FAIL —— 一個在該擋的時候放行的
    閘門，比沒有閘門更糟。
    """
    from pcmef.core.hash import hash_object

    freeze = tmp_path / "freeze"
    freeze.mkdir()
    payload = {
        "prompt_hashes": {"observation": "p1"},
        "schema_hashes": {"observation": "s1"},
        "runtime_config_hash": "r1",
    }
    (freeze / "llm_runtime.lock.json").write_text(
        json.dumps({
            "lock_type": "llm_runtime", "version": 1,
            "created_at": "2026-09-01T00:00:00+00:00",
            "payload": payload, "payload_hash": hash_object(payload),
        }),
        encoding="utf-8",
    )

    # validation 沒有記 runtime_config_hash → 無法比對 → FAIL
    silent = _validation(tmp_path)
    document = json.loads(silent.read_text(encoding="utf-8"))
    document.pop("runtime_config_hash")
    silent.write_text(json.dumps(document), encoding="utf-8")
    result = _by_key(evaluate(freeze, real_validation_path=silent))[
        "llm_runtime_refrozen"
    ]
    assert result["passed"] is False
    assert "runtime_config_hash" in result["detail"]

    # 有記且相符 → PASS
    matching = _validation(tmp_path / "b", runtime_config_hash="r1") \
        if (tmp_path / "b").mkdir() or True else None
    assert _by_key(evaluate(freeze, real_validation_path=matching))[
        "llm_runtime_refrozen"
    ]["passed"]


# ---------------------------------------------------------------------------
# 韌性
# ---------------------------------------------------------------------------


def test_the_gate_survives_unreadable_inputs(tmp_path):
    """讀檔失敗不得讓閘門消失，只能讓它 FAIL。"""
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    results = evaluate(tmp_path, real_validation_path=broken, snapshot_path=broken)
    assert len(results) == 8
    assert all(r["blocking"] for r in results)
    assert not any(r["passed"] for r in results)


# ---------------------------------------------------------------------------
# 對實際 lineage
# ---------------------------------------------------------------------------


def test_the_real_lineage_reports_what_is_still_missing():
    """對 repo 內實際生效的 lineage 跑一次。

    這一條刻意**不斷言全部 FAIL** —— 那會在解除某一項之後變成假警報。
    它斷言的是：三個 amendment 尚未凍結、canonical baseline 尚未確立，
    而這正是 STATUS.md 記載的現況。哪一天這條測試失敗了，代表現況變了，
    STATUS.md 也該跟著改。
    """
    from pcmef.core.active_lineage import resolve_active_lineage

    resolved = resolve_active_lineage("freeze")
    results = _by_key(evaluate(resolved.freeze_dir))

    for amendment in ("amd007_frozen", "amd008_frozen", "amd009_frozen"):
        assert results[amendment]["passed"] is False, (
            f"{amendment} 現在通過了 —— 若 amendment 真的凍了，"
            "請同步更新 STATUS.md 的前置條件表"
        )
    assert results["canonical_golden_baseline"]["passed"] is False
    # 第八項必須 FAIL：families 36-43 尚未生成，而這正是 FINAL_E2 未開封。
    assert results["final_partition_manifest"]["passed"] is False


def test_evaluating_the_real_lineage_touches_no_final_family(tmp_path, monkeypatch):
    """閘門本身不得讀 families 36-43 的任何觀測檔。"""
    import numpy as np

    def _refuse(*args, **kwargs):
        raise AssertionError("final_gate must not load any array data")

    monkeypatch.setattr(np, "load", _refuse)
    from pcmef.core.active_lineage import resolve_active_lineage

    evaluate(resolve_active_lineage("freeze").freeze_dir)
