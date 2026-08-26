# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；掃描 tests/ 全樹的測試函式名稱與 docstring，
#         比對 SRC-SAI §51 的十條驗收 ID；不讀寫 artifact，也不執行被測程式。
# 檔案路徑: tests/llm_admin/test_acceptance_matrix.py
# 產生時間: 2026-08-27 04:50 +08:00
# 版本: v0.1.0
# 功能說明: 檢查 §51 的十條驗收每一條都真的有對應的測試，而不是只寫在文件上。
#           少一條、或有人把某條測試刪掉改成註解，這裡就會失敗。
# 模組定位: §52 步驟 7「跑完 security/leakage/cache/resume acceptance tests 後，
#           才允許把 Admin module 標為 DoD」的可執行門檻。
#           它「不是」驗收本身 —— 它只確認驗收存在且被收集得到。
# 主要責任:
#   1. ACCEPTANCE_IDS 逐字登錄 §51 的十個 Test ID 與 PASS 條件
#   2. test_every_acceptance_id_has_a_test 驗證每條都找得到對應測試
#   3. test_no_acceptance_test_is_skipped 擋下以 skip 蒙混過關
#   4. test_the_matrix_itself_is_falsifiable 反證稽核器抓得到缺漏
# 維護提醒:
#   - 不得以在本檔加註解的方式「宣告某條已通過」；判定依據只有實際存在
#     且會被收集的測試函式。
#   - 不得為了讓某條快速過關而加上 pytest.mark.skip；本檔會擋下。
#   - 新增 §51 驗收條目時必須同步更新 ACCEPTANCE_IDS，否則新條目不會被要求。
#   - v0.1.0 新增：首版十條對照表。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_acceptance_matrix.py -v
# ------------------------------------------------------------

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parents[1]

# SRC-SAI §51 Admin UI / LLM Module Acceptance Tests，逐列抄錄。
ACCEPTANCE_IDS: dict[str, str] = {
    "LLM-UI-01": "新增 connection 後 response / HTML / logs 均找不到完整 API key",
    "LLM-UI-02": "embedding-only model 不會出現在 observation/arbitration 的可綁 dropdown",
    "LLM-UI-03": "Arbitration binding 若 structured-json probe FAIL，Bind 直接拒絕",
    "LLM-UI-04": "仍被 task 使用的 model profile Delete -> 409，dependency 列表正確",
    "LLM-UI-05": "Formal snapshot 產生後修改 live binding，不改變既有 llm_runtime.lock hash",
    "LLM-UI-06": "Formal runner 在 SQLite binding table 被手改後仍用 lock 中 model identity",
    "LLM-SEC-01": "manifest / report / DB plaintext 全域掃描無 secret",
    "LLM-CACHE-01": "相同 evidence/config 在 3 checkpoint pairs 僅產生 1 次 provider call",
    "LLM-CACHE-02": "prompt/schema/model/revision/representation/evidence 任一 hash 變 -> miss",
    "LLM-RESUME-01": "已完成 formal case resume 時只讀 frozen response，不重新 call provider",
}

#: ID 轉成函式名稱片段的規則：LLM-UI-01 -> llm_ui_01。
_SLUG = {
    identifier: identifier.lower().replace("-", "_")
    for identifier in ACCEPTANCE_IDS
}


def _test_functions() -> list[tuple[str, str, list[str]]]:
    """回傳 (檔案, 函式名, decorator 名稱) 三元組。"""
    found: list[tuple[str, str, list[str]]] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                decorators = [
                    ast.unparse(decorator) for decorator in node.decorator_list
                ]
                found.append(
                    (path.relative_to(TESTS_ROOT).as_posix(), node.name, decorators)
                )
    return found


def _covering(identifier: str) -> list[tuple[str, str, list[str]]]:
    slug = _SLUG[identifier]
    return [entry for entry in _test_functions() if slug in entry[1]]


@pytest.mark.parametrize("identifier", sorted(ACCEPTANCE_IDS))
def test_every_acceptance_id_has_a_test(identifier):
    """§52 步驟 7：跑完這十條才准把 Admin module 標為 DoD。"""
    covering = _covering(identifier)
    assert covering, (
        f"{identifier} ({ACCEPTANCE_IDS[identifier]}) has no test function whose "
        f"name contains {_SLUG[identifier]!r}. The acceptance table is not a "
        "checklist to tick off in prose — each row needs a test that can fail."
    )


@pytest.mark.parametrize("identifier", sorted(ACCEPTANCE_IDS))
def test_no_acceptance_test_is_skipped(identifier):
    """以 skip 蒙混過關與沒寫是同一件事。"""
    for path, name, decorators in _covering(identifier):
        for decorator in decorators:
            assert "skip" not in decorator, (
                f"{identifier} is covered by {path}::{name} but that test carries "
                f"{decorator!r}; a skipped acceptance test proves nothing."
            )


def test_all_ten_rows_of_section_51_are_registered():
    assert len(ACCEPTANCE_IDS) == 10
    assert sum(1 for i in ACCEPTANCE_IDS if i.startswith("LLM-UI-")) == 6
    assert "LLM-SEC-01" in ACCEPTANCE_IDS
    assert sum(1 for i in ACCEPTANCE_IDS if i.startswith("LLM-CACHE-")) == 2
    assert "LLM-RESUME-01" in ACCEPTANCE_IDS


def test_the_matrix_itself_is_falsifiable():
    """稽核器的反證：一個不存在的 ID 必須查不到覆蓋。

    沒有這一條，_covering() 若因為路徑寫錯而永遠回傳空清單，
    上面兩條會全部失敗；若它因為比對太寬鬆而永遠回傳非空，
    上面兩條會全部恆真。這條把後者釘住。
    """
    _SLUG["LLM-NOPE-99"] = "llm_nope_99"
    try:
        assert _covering("LLM-NOPE-99") == []
    finally:
        del _SLUG["LLM-NOPE-99"]
    # 而真實存在的那條必須查得到，確認比對邏輯本身可用。
    assert _covering("LLM-CACHE-01")
