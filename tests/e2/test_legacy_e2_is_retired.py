# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 argparse 解析 pcmef.cli 的指令表，
#         確認退役的入口不會執行、且新入口指向正確的執行器。
#         不啟動任何子行程，也不讀資料集。
# 檔案路徑: tests/e2/test_legacy_e2_is_retired.py
# 產生時間: 2026-09-02 15:20 +08:00
# 版本: v0.1.0
# 功能說明: 確認 `perception e2` 不再能跑出任何東西，而唯一的 Formal E2
#           入口是 `formal run-e2`。
# 模組定位: P0-6 的回歸測試。退役的重點不是改名，而是**讓舊路徑跑不動** ——
#           一個名字像 Formal E2、跑起來也像、輸出也像結果的指令，
#           遲早會有人打到它。
# 主要責任:
#   1. test_perception_e2_exits_non_zero 舊入口一律失敗
#   2. test_perception_e2_does_not_call_the_deterministic_runner
#   3. test_pilot_refuses_a_non_pilot_directory
#   4. test_formal_run_e2_uses_the_full_executor 新入口指向對的函式
# 維護提醒:
#   - 不得把 `perception e2` 改回「照樣跑，只是印個警告」。警告會被略過，
#     exit code 不會。
#   - 不得放寬 pilot 的目錄守衛。它擋的是「用 pilot 指令去跑 final 資料」。
#   - v0.1.0 新增：首版，對應 P0-6。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_legacy_e2_is_retired.py -v
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import inspect

import pytest

from pcmef import cli


def _namespace(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ---------------------------------------------------------------------------
# 舊入口
# ---------------------------------------------------------------------------


def test_perception_e2_exits_non_zero(capsys):
    """退役靠 exit code，不靠警告文字。"""
    assert cli.cmd_perception_e2(_namespace()) == 2
    message = capsys.readouterr().err
    assert "DEPRECATED" in message
    # 訊息必須指出替代入口，否則使用者只知道壞了、不知道該打什麼。
    assert "formal run-e2" in message
    assert "e2-deterministic-pilot" in message


def test_perception_e2_does_not_call_the_deterministic_runner(monkeypatch):
    """舊入口不得再碰 gate.run_formal_e2 —— 連一次都不行。"""
    import pcmef.perception.gate as gate

    def explode(*args, **kwargs):  # pragma: no cover
        raise AssertionError("the retired command reached the deterministic runner")

    monkeypatch.setattr(gate, "run_formal_e2", explode)
    assert cli.cmd_perception_e2(_namespace()) == 2


def test_perception_e2_takes_no_arguments():
    """退役的指令不該還接受 --e2-dir；那會讓它看起來仍可設定。"""
    parser = cli.build_parser()
    namespace = parser.parse_args(["perception", "e2"])
    assert namespace.func is cli.cmd_perception_e2
    assert not hasattr(namespace, "e2_dir")


# ---------------------------------------------------------------------------
# pilot 入口
# ---------------------------------------------------------------------------


def test_pilot_refuses_a_non_pilot_directory(capsys):
    result = cli.cmd_perception_e2_deterministic_pilot(
        _namespace(
            ds_dir="outputs/perception/ds_v2",
            gate_rule="outputs/perception/gate/gate_rule.json",
            e2_dir="outputs/perception/formal_e2",
            out="outputs/perception/whatever",
        )
    )
    assert result == 2
    assert "refusing to run" in capsys.readouterr().err


def test_pilot_default_directory_is_the_pilot_set():
    parser = cli.build_parser()
    namespace = parser.parse_args(["perception", "e2-deterministic-pilot"])
    assert "e2_deterministic_gate_pilot" in namespace.e2_dir
    assert namespace.func is cli.cmd_perception_e2_deterministic_pilot


# ---------------------------------------------------------------------------
# 新入口
# ---------------------------------------------------------------------------


def test_formal_run_e2_is_wired_to_the_full_executor():
    """唯一的 Formal E2 入口必須呼叫 run_formal_e2_full，而不是 gate 的那條。"""
    # 入口與本體一起看：`cmd_formal_run_e2` 已拆成「入口 + 本體」，
    # 入口只負責把任何結局都寫成終局事件，實際的邏輯在本體裡。
    # 只看入口的話，這條守衛會安靜地變成永遠通過。
    source = (inspect.getsource(cli.cmd_formal_run_e2)
              + inspect.getsource(cli._formal_run_e2_body))
    assert "run_formal_e2_full" in source
    assert "gate.run_formal_e2" not in source
    assert "from pcmef.perception.gate import run_formal_e2" not in source


def test_formal_run_e2_does_not_reimplement_the_loop():
    """CLI 不得自己寫決策迴圈；它只負責 pre-flight 與交棒。"""
    # 入口與本體一起看：`cmd_formal_run_e2` 已拆成「入口 + 本體」，
    # 入口只負責把任何結局都寫成終局事件，實際的邏輯在本體裡。
    # 只看入口的話，這條守衛會安靜地變成永遠通過。
    source = (inspect.getsource(cli.cmd_formal_run_e2)
              + inspect.getsource(cli._formal_run_e2_body))
    assert "decide_case" not in source
    assert "build_case_evidence" not in source


@pytest.mark.parametrize("mode", ["dry-run", "formal"])
def test_formal_parser_accepts_both_modes(mode):
    parser = cli.build_parser()
    namespace = parser.parse_args(["formal", "run-e2", "--mode", mode])
    assert namespace.mode == mode
    assert namespace.func is cli.cmd_formal_run_e2


def test_formal_mode_defaults_to_dry_run():
    """預設是預演。忘記加參數不該直接花錢跑 one-shot。"""
    parser = cli.build_parser()
    assert parser.parse_args(["formal", "run-e2"]).mode == "dry-run"
