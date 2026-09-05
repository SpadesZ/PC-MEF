# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.experiments.e2_formal 的
#         frozen_severity() / resolve_severity()，以 tmp_path 上的
#         合成 e2_sample_size.lock 為輸入。不連線、不讀 dataset、
#         不觸碰 families 36-43。
# 檔案路徑: tests/e2/test_frozen_severity.py
# 產生時間: 2026-09-04 21:10 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 Formal E2 的 severity 只能由 frozen 選擇還原，
#           CLI 旗標是斷言而不是設定。
# 模組定位: NOTE-072 的可執行防線。少了它，把 severity 改回「呼叫端說了算」
#           不會有任何測試失敗，而報告仍會寫著它來自 frozen 選擇。
# 主要責任:
#   1. test_frozen_severity_is_restored_from_the_lock 正常路徑
#   2. test_absent_severity_uses_the_frozen_pair 不給旗標即還原
#   3. test_changing_the_vision_severity_is_blocked
#   4. test_changing_the_tof_severity_is_blocked
#   5. test_matching_values_are_recorded_as_verified_not_applied
#   6. test_missing_allocation_fails_closed 讀不到不得猜
#   7. test_report_records_a_named_source_not_a_slogan
#   8. test_cli_defaults_are_none 預設值不得回到 CLI
# 維護提醒:
#   - 不得把 BLOCK 改成 warning。severity 決定 stress set，也就是整場
#     Formal E2 吃進去的資料；改掉它等於換了實驗，而換了實驗的報告仍會
#     宣稱資料來自 frozen 選擇。
#   - 不得讓 CLI 旗標回到有預設值的狀態。有預設值就無法區分「沒有斷言」
#     與「斷言剛好等於預設」，而前者才是正常用法。
#   - v0.1.0 新增：首版，對應 P0-2 / NOTE-072。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_frozen_severity.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect
import json

import pytest

from pcmef.experiments.e2_formal import (
    FormalE2Error, frozen_severity, resolve_severity,
)

FROZEN = {"vision": 2.0, "tof": 0.05}


def _freeze(tmp_path, allocation=None):
    """寫一份最小的 e2_sample_size.lock。內容取自 PFC-001 的實際值。

    payload_hash 要算真的：LockStore 讀取時會驗，寫死一串 0 會在
    `frozen_severity()` 還沒開始讀欄位之前就以 LockError 失敗，
    那樣測到的是完整性檢查，不是 severity 還原。
    """
    from pcmef.core.hash import hash_object

    directory = tmp_path / "freeze"
    directory.mkdir(exist_ok=True)
    payload = {"total_condition_rows": 384}
    if allocation is not None:
        payload["severity_allocation"] = allocation
    (directory / "e2_sample_size.lock.json").write_text(
        json.dumps(
            {
                "lock_type": "e2_sample_size",
                "version": 1,
                "created_at": "2026-09-01T00:00:00+00:00",
                "payload": payload,
                "payload_hash": hash_object(payload),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture()
def freeze_dir(tmp_path):
    return _freeze(
        tmp_path,
        {
            "vision": 2.0,
            "tof": 0.05,
            "reselection_forbidden": True,
            "selected_on": "gate-validation only, by stress.select_severity()",
            "scheme": "FIXED_TWO_SEVERITY_NO_LOW_MID_HIGH",
        },
    )


# ---------------------------------------------------------------------------
# 還原
# ---------------------------------------------------------------------------


def test_frozen_severity_is_restored_from_the_lock(freeze_dir):
    severity, allocation = frozen_severity(freeze_dir)
    assert severity == FROZEN
    assert allocation["reselection_forbidden"] is True


def test_absent_severity_uses_the_frozen_pair(freeze_dir):
    """不給旗標是正常用法：executor 自己去 lock 拿。"""
    severity, provenance = resolve_severity(freeze_dir, None)
    assert severity == FROZEN
    assert provenance["restored_from_lock"] is True
    assert provenance["cli_override_supplied"] is False


def test_the_real_lineage_carries_the_expected_pair():
    """對著 repo 內實際生效的 lineage 跑一次。

    合成 lock 只證明函式會讀欄位；這一條證明它讀到的是**這個專案真正
    凍住的那一對數字**，也就是先前 CLI 預設值恰好相同的那一對。
    """
    from pcmef.core.active_lineage import resolve_active_lineage

    resolved = resolve_active_lineage("freeze")
    severity, allocation = frozen_severity(resolved.freeze_dir)
    assert severity == FROZEN
    assert allocation.get("reselection_forbidden") is True


# ---------------------------------------------------------------------------
# 斷言不符即 fail-closed
# ---------------------------------------------------------------------------


def test_changing_the_vision_severity_is_blocked(freeze_dir):
    with pytest.raises(FormalE2Error, match="does not match the frozen"):
        resolve_severity(freeze_dir, {"vision": 3.0, "tof": 0.05})


def test_changing_the_tof_severity_is_blocked(freeze_dir):
    with pytest.raises(FormalE2Error, match="does not match the frozen"):
        resolve_severity(freeze_dir, {"vision": 2.0, "tof": 0.5})


def test_the_error_names_both_values(freeze_dir):
    """錯誤訊息必須同時說出「你給的」與「凍住的」。

    只說「不相符」的話，操作者無從判斷是自己打錯還是 lock 換了。
    """
    with pytest.raises(FormalE2Error) as error:
        resolve_severity(freeze_dir, {"vision": 3.0, "tof": 0.05})
    message = str(error.value)
    assert "3.0" in message and "2.0" in message


def test_matching_values_are_recorded_as_verified_not_applied(freeze_dir):
    """相等時放行，但出處仍記成「還原自 lock」而非「來自旗標」。"""
    severity, provenance = resolve_severity(freeze_dir, dict(FROZEN))
    assert severity == FROZEN
    assert provenance["cli_override_supplied"] is True
    assert provenance["cli_override_verified_equal"] is True
    assert provenance["restored_from_lock"] is True


def test_a_partial_assertion_only_checks_what_was_given(freeze_dir):
    """只給一邊時，另一邊不算斷言，直接還原。"""
    severity, provenance = resolve_severity(
        freeze_dir, {"vision": 2.0, "tof": None}
    )
    assert severity == FROZEN
    assert provenance["cli_override_verified_equal"] is True


# ---------------------------------------------------------------------------
# 讀不到不得猜
# ---------------------------------------------------------------------------


def test_missing_allocation_fails_closed(tmp_path):
    directory = _freeze(tmp_path, allocation=None)
    with pytest.raises(FormalE2Error, match="severity_allocation"):
        frozen_severity(directory)


def test_non_numeric_allocation_fails_closed(tmp_path):
    directory = _freeze(tmp_path, {"vision": "high", "tof": 0.05})
    with pytest.raises(FormalE2Error, match="numeric"):
        frozen_severity(directory)


# ---------------------------------------------------------------------------
# 報告與 CLI 的表面
# ---------------------------------------------------------------------------


def test_report_records_a_named_source_not_a_slogan():
    """`severity_source` 必須是結構化出處，不得是永遠為真的一句話。

    先前它是固定字串「frozen gate-validation selection; not re-selected
    here」，於是不論旗標傳了什麼，報告都這樣寫 —— 一句永遠為真的話
    證明不了任何事。
    """
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    assert '"severity_source": severity_provenance' in source
    assert "resolve_severity(freeze_dir, severity)" in source


def test_cli_defaults_are_none():
    """CLI 旗標不得有預設值。

    有預設值就無法區分「沒有斷言」與「斷言剛好等於預設」，而
    cmd_formal_run_e2 正是靠 None 判斷要不要送出斷言。
    """
    import argparse

    from pcmef.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["formal", "run-e2"])
    assert args.vision_severity is None
    assert args.tof_severity is None
    assert isinstance(parser, argparse.ArgumentParser)


def test_cli_sends_none_when_no_flag_is_given():
    """沒給旗標時 CLI 必須送 None，而不是自己組一個 dict。"""
    from pcmef import cli

    source = inspect.getsource(cli.cmd_formal_run_e2)
    assert "if args.vision_severity is None and args.tof_severity is None" in source
