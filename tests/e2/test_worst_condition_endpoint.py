# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 experiments.e2_formal.worst_condition_macro_f1
#         與它在報告中的呈現。純函式，不需要資料集或 provider。
# 檔案路徑: tests/e2/test_worst_condition_endpoint.py
# 產生時間: 2026-09-02 20:10 +08:00
# 版本: v0.1.0
# 功能說明: 釘住 E2 的 primary robustness endpoint：四個 condition 中最低的
#           macro-F1，以及那個 condition 是哪一個。
# 模組定位: P1-3 的回歸測試。實驗計畫 v1.2 §5 把 worst-condition macro-F1
#           定為 primary robustness metric；它與 overall 的排序可能不同，
#           那正是它該當 primary 的理由。
# 主要責任:
#   1. test_it_is_the_minimum_over_conditions
#   2. test_it_names_which_condition_is_weakest
#   3. test_ties_are_resolved_deterministically 不依賴 dict 插入順序
#   4. test_an_empty_breakdown_is_refused 沒有 per-condition 就沒有定義
#   5. test_the_report_carries_it_for_every_arm
# 維護提醒:
#   - 不得只報最小值而不報它落在哪個 condition。不同方法的最弱條件可能
#     不同（實測 fixed_fusion 是 tof_degraded，其餘三條是 conflict），
#     而那件事在 per-condition 表格裡看得到卻很容易被略過。
#   - 不得把 per_condition 從報告拿掉。worst 是它的摘要，不是它的替代。
#   - v0.1.0 新增：首版，對應 P1-3。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_worst_condition_endpoint.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect

import pytest

from pcmef.experiments.e2_formal import FormalE2Error, worst_condition_macro_f1


def _breakdown(**conditions: float) -> dict[str, dict[str, float]]:
    return {name: {"macro_f1": value, "accuracy": value, "n": 96}
            for name, value in conditions.items()}


def test_it_is_the_minimum_over_conditions():
    worst, _ = worst_condition_macro_f1(
        {
            "vision_only": _breakdown(
                clean=0.95, vision_degraded=0.61, tof_degraded=0.92, conflict=0.53
            ),
            "tof_only": _breakdown(
                clean=0.88, vision_degraded=0.86, tof_degraded=0.19, conflict=0.14
            ),
        }
    )
    assert worst == {"vision_only": pytest.approx(0.53), "tof_only": pytest.approx(0.14)}


def test_it_names_which_condition_is_weakest():
    """不同方法的最弱條件可能不同 —— 那本身就是結果的一部分。"""
    _, where = worst_condition_macro_f1(
        {
            "fixed_fusion": _breakdown(
                clean=0.90, vision_degraded=0.70, tof_degraded=0.16, conflict=0.30
            ),
            "reliability_routing": _breakdown(
                clean=0.93, vision_degraded=0.75, tof_degraded=0.88, conflict=0.18
            ),
        }
    )
    assert where == {
        "fixed_fusion": "tof_degraded",
        "reliability_routing": "conflict",
    }


def test_a_higher_average_can_still_have_a_worse_floor():
    """這是把 worst-condition 當 primary 的整個理由。"""
    worst, _ = worst_condition_macro_f1(
        {
            # 平均較高，但最弱條件崩掉。
            "flashy": _breakdown(
                clean=0.99, vision_degraded=0.99, tof_degraded=0.99, conflict=0.10
            ),
            # 平均較低，但四個條件都穩。
            "steady": _breakdown(
                clean=0.70, vision_degraded=0.68, tof_degraded=0.69, conflict=0.67
            ),
        }
    )
    assert worst["steady"] > worst["flashy"]


def test_ties_are_resolved_deterministically():
    """平手時取字典序最小，輸出不得依賴 dict 的插入順序。"""
    forward = worst_condition_macro_f1(
        {"m": _breakdown(conflict=0.4, clean=0.4, tof_degraded=0.9)}
    )[1]
    reversed_order = worst_condition_macro_f1(
        {"m": _breakdown(tof_degraded=0.9, clean=0.4, conflict=0.4)}
    )[1]
    assert forward == reversed_order == {"m": "clean"}


def test_an_empty_breakdown_is_refused():
    with pytest.raises(FormalE2Error) as error:
        worst_condition_macro_f1({"m": {}})
    assert "undefined" in str(error.value)


# ---------------------------------------------------------------------------
# 報告呈現
# ---------------------------------------------------------------------------


def test_the_report_carries_the_endpoint_and_its_definition():
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal.run_formal_e2_full)
    assert '"worst_condition_macro_f1": worst_condition' in source
    assert '"worst_condition_at": worst_condition_at' in source
    assert '"primary_endpoint"' in source
    # per-condition 是它的來源，不得被取代掉。
    assert '"per_condition": per_condition' in source


def test_the_endpoint_is_derived_not_recomputed():
    """worst 必須由 per_condition 取 min，不得另外算一次。

    另外算一次就有兩個可能不一致的來源，而不一致時沒有任何症狀。
    """
    source = inspect.getsource(
        __import__("pcmef.experiments.e2_formal", fromlist=["x"]).run_formal_e2_full
    )
    assert "worst_condition_macro_f1(per_condition)" in source
