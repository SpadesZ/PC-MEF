# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 inspect 檢查 experiments.e2_formal 的簽名，
#         並用 ast 掃描 pcmef/ 全部原始檔，找出任何把 freeze_dir 填成
#         字面預設值的宣告。
# 檔案路徑: tests/e2/test_freeze_dir_is_required.py
# 產生時間: 2026-09-02 12:20 +08:00
# 版本: v0.1.0
# 功能說明: 確保「沒有明確指定 lineage 就不准跑」這件事不會被一個
#           善意的預設值悄悄還原。
# 模組定位: P0-2 的回歸測試。它守的不是某一次修改，而是**方向** ——
#           fail-open 的預設值在出錯時看起來一切正常，那正是危險所在。
# 主要責任:
#   1. test_*_has_no_default 兩個入口的 freeze_dir 必須無預設值
#   2. test_no_source_file_declares_a_literal_freeze_dir_default 以 AST 掃描
#      整個 pcmef/，擋下任何 freeze_dir="..." 的參數預設
#   3. test_calling_without_a_lineage_raises 未傳即 TypeError，不是靜默回退
# 維護提醒:
#   - 不得為了讓某個呼叫端方便而加回預設值。真正的預設 "freeze" 指向
#     superseded 的 parent lineage（NOTE-054），而兩份 lineage 的檔名與
#     schema 完全相同，讀錯不會有任何症狀。
#   - AST 掃描刻意涵蓋整個 pcmef/ 而不只 e2_formal：繞過的方式通常是
#     在別處包一層帶預設值的 wrapper。
#   - v0.1.0 新增：首版，對應 P0-2。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_freeze_dir_is_required.py -v
# ------------------------------------------------------------

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from pcmef.experiments.e2_formal import load_frozen_decision_stack, run_formal_e2_full

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "pcmef"

#: 這個名字帶預設值就是 fail-open。
GUARDED_PARAMETER = "freeze_dir"


@pytest.mark.parametrize(
    "function", [load_frozen_decision_stack, run_formal_e2_full],
    ids=["load_frozen_decision_stack", "run_formal_e2_full"],
)
def test_the_formal_entry_points_have_no_lineage_default(function):
    parameter = inspect.signature(function).parameters[GUARDED_PARAMETER]
    assert parameter.default is inspect.Parameter.empty, (
        f"{function.__name__} has a default for {GUARDED_PARAMETER}. "
        "The old default was 'freeze', which is the superseded parent lineage; "
        "a run against it raises nothing and silently uses corrected-away values."
    )


def test_run_formal_e2_full_takes_the_lineage_as_keyword_only():
    """keyword-only：位置引數容易在重排參數時對錯位置而不被發現。"""
    parameter = inspect.signature(run_formal_e2_full).parameters[GUARDED_PARAMETER]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_calling_without_a_lineage_raises_rather_than_falling_back():
    with pytest.raises(TypeError):
        load_frozen_decision_stack()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        run_formal_e2_full("base", "out")  # type: ignore[call-arg]


#: AMD-006 在 PFC-001 supersede 掉的三個 lock。
#:
#: 掃描範圍就是由這三個名字定義的，而不是「所有 freeze_dir 參數」。
#: 其餘 19 個 lock 在 parent 與 PFC-001 兩份 lineage 中逐位元相同，
#: 因此 calibration / E1 / dataset / simulation 那些模組即使
#: `freeze_dir="freeze"` 也讀到一樣的東西 —— 那些預設值不是 fail-open。
#: 會出事的只有讀到這三個之一、卻沒說清楚要讀哪份 lineage 的函式。
SUPERSEDED_LOCKS: tuple[str, ...] = ("gate", "reliability_final", "formal_config")


def _iter_python_files() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _lineage_defaults(tree: ast.AST) -> list[tuple[ast.FunctionDef, ast.Constant]]:
    """找出所有把 freeze_dir 填成字面常數的函式定義。"""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        positional = arguments.posonlyargs + arguments.args
        paired = list(
            zip(
                positional[len(positional) - len(arguments.defaults):],
                arguments.defaults,
            )
        ) + [
            (argument, default)
            for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
            if default is not None
        ]
        for argument, default in paired:
            if argument.arg == GUARDED_PARAMETER and isinstance(default, ast.Constant):
                if default.value is not None:
                    found.append((node, default))
    return found


def test_no_function_reading_a_superseded_lock_defaults_its_lineage():
    """讀 gate / reliability_final / formal_config 的函式不得有 lineage 預設值。

    掃的是**函式參數的預設值**，不是一般賦值：CLI 的
    `--freeze-dir default=...` 是使用者可見的選項，寫在指令列上，
    那沒問題。危險的是函式簽名裡的預設，因為呼叫端不寫就會靜默套用。
    """
    offenders: list[str] = []

    for path in _iter_python_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node, default in _lineage_defaults(tree):
            body = ast.get_source_segment(source, node) or ""
            touched = sorted(
                name for name in SUPERSEDED_LOCKS if f'"{name}"' in body
                or f"'{name}'" in body
            )
            if touched:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno} "
                    f"{node.name}({GUARDED_PARAMETER}={default.value!r}) "
                    f"reads {touched}"
                )

    assert not offenders, (
        "these functions read a lock that AMD-006 superseded, yet default their "
        "lineage. The default resolves to the parent freeze/, where those locks "
        "still hold the pre-correction values, and nothing raises:\n  "
        + "\n  ".join(offenders)
    )
