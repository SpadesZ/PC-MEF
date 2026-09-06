# PC-MEF Research System source maintenance contract
# 上下游: 由 platform.lifecycle.providers 依 template 選用；讀
#         audit.e1_gates、experiments.final_gate 與 core.locks 的**既有判定**，
#         把它們掛到通用 lifecycle 的對應階段上。**唯讀。**
# 檔案路徑: pcmef/platform/lifecycle/pcmef.py
# 產生時間: 2026-09-07 11:45 +08:00
# 版本: v0.1.0
# 功能說明: PC-MEF 碩論專用的 lifecycle provider —— E1 十二道 gate、
#           AMD-007/008/009、llm_runtime / formal_config 重凍、
#           Golden Baseline、Final manifest 與 Formal E2 的階段歸屬。
# 模組定位: 平台化 Phase 4 的 PC-MEF adapter。SAI v0.6.0 §38「能以
#           adapter 解決就不動 stable core」的落點：
#           **這裡不重新判定任何科學狀態**，只把既有判定的結果
#           轉成 Stage / Gate 呈現。
# 主要責任:
#   1. STAGE_GATES 宣告哪些 gate 屬於哪一個階段
#   2. _e1_gates() 把 audit.e1_gates 的結果轉成 Gate
#   3. _final_gates() 把 final_gate.evaluate 的八項轉成 Gate
#   4. _lock_gates() 由 lock 是否存在推導前段階段
#   5. pcmef_lifecycle() 組出完整 LifecycleView
# 維護提醒:
#   - **不得在本檔重新實作任何 gate 判定。** 判定的可執行真相在
#     audit.e1_gates 與 experiments.final_gate；這裡再寫一份，
#     畫面與 CLI 就會對同一個研究給出兩種進度。
#   - 不得因為某個 gate 尚未通過就把它從畫面上拿掉。看不到的 blocker
#     等於沒有 blocker，而它其實還在擋。
#   - 不得把 Formal E2 標成 COMPLETE，除非 final_gate 真的全數通過
#     且正式執行已完成。這一格是整份研究最容易被誤讀的地方。
#   - v0.1.0 新增：首版，對應平台化 Phase 4。
# 驗證方式:
#   - py -3.10 -m pytest tests/platform/test_lifecycle_pcmef.py -v
# ------------------------------------------------------------

from __future__ import annotations

from pathlib import Path
from typing import Any

from pcmef.platform.lifecycle.models import (
    BLOCKED,
    COMPLETE,
    NOT_STARTED,
    PASS,
    Gate,
    LifecycleView,
    Stage,
)
from pcmef.platform.lifecycle.providers import GENERIC_STAGES
from pcmef.platform.projects.resolver import LEGACY_THESIS_PROJECT_ID

__all__ = ["pcmef_lifecycle", "PROVIDER_NAME", "STAGE_LOCKS"]

#: 這個 provider 註冊在哪個 template 之下，也是畫面上顯示的判定來源。
#:
#: 由常數導出而不是再寫一次字面值 —— 重複的字面值正是
#: test_legacy_special_case_lives_only_in_the_resolver 要擋的。
PROVIDER_NAME = LEGACY_THESIS_PROJECT_ID

#: 前段階段由「這些 lock 是否已凍結」推導。
#:
#: 用 lock 而不是自己算：lock 的存在就是那個階段完成的證據，
#: 而重新推導一次會與 freeze/ 產生第二種說法。
STAGE_LOCKS: dict[str, tuple[tuple[str, str], ...]] = {
    "sensor-data-design": (
        ("real_split_policy", "真實資料切分已凍結"),
        ("synthetic_split_policy", "合成資料切分已凍結"),
        ("training_seed_pairs", "訓練 seed 配對已凍結"),
        ("validation_pool", "驗證池已凍結"),
    ),
    "calibration": (
        ("initial_simulation", "初始模擬已凍結"),
        ("calibrated_simulation", "校準後模擬已凍結"),
        ("metric_config", "E1 度量設定已凍結"),
    ),
    "decision-validation": (
        ("reliability_final", "可靠度定義已凍結"),
        ("gate", "路由門檻已凍結"),
        ("conflict_operational", "衝突／劣化強度已凍結"),
        ("agent_schema", "Agent 證據契約已凍結"),
        ("inference_firewall", "推論防火牆已凍結"),
    ),
}


def _lock_gates(freeze_dir: Path, stage_id: str) -> tuple[Gate, ...]:
    from pcmef.core.locks import LockStore

    store = LockStore(freeze_dir)
    gates = []
    for name, label in STAGE_LOCKS.get(stage_id, ()):
        # 未註冊的 lock 名稱只讓**這一格**降級，不得讓整個 Status 消失。
        # LockStore 只認得 registry 裡的名字，而 freeze/ 底下另有
        # 一些不在 registry 的檔案（例如 heldout_partition）。
        try:
            exists = store.exists(name)
            detail = f"{name}.lock.json" + ("" if exists else "（尚未凍結）")
        except Exception as error:  # noqa: BLE001
            exists, detail = False, f"{name}：無法判定（{error}）"
        gates.append(
            Gate(
                gate_id=f"lock.{name}",
                label=label,
                status=PASS if exists else NOT_STARTED,
                detail=detail,
                why="凍結後這一項就不再隨開發改動。",
            )
        )
    return tuple(gates)


def _e1_gates(context: Any) -> tuple[Gate, ...]:
    """E1 十二道 gate。**直接取 audit.e1_gates 的結果，不重算。**"""
    summary = getattr(context, "gate_summary", None)
    if not summary:
        return ()
    mapping = {"通過": PASS, "不符": BLOCKED, "尚未產出": NOT_STARTED}
    gates = []
    for item in summary.get("gates", ()):
        gates.append(
            Gate(
                gate_id=f"E1-{item['id']}",
                label=f"E1-{item['id']}",
                status=mapping.get(item.get("status", ""), NOT_STARTED),
                detail=item.get("requirement", ""),
                why="E1 物理校準驗證的判準之一。",
            )
        )
    return tuple(gates)


def _final_gates(freeze_dir: Path) -> tuple[Gate, ...]:
    """Formal E2 的八項機器強制前置條件。"""
    from pcmef.experiments.final_gate import progress as final_progress

    report = final_progress(freeze_dir)
    if "error" in report:
        return (
            Gate(
                gate_id="final_gate.unavailable",
                label="Final E2 前置條件",
                status=NOT_STARTED,
                detail=str(report["error"])[:200],
                why="無法讀取 final gate 判定。",
            ),
        )
    gates = []
    for item in report.get("cleared_items", ()):
        gates.append(
            Gate(
                gate_id=item["check"], label=item["label"], status=PASS,
                detail=item.get("detail", "")[:220],
                why="Formal E2 進入條件之一。",
            )
        )
    for item in report.get("blockers", ()):
        gates.append(
            Gate(
                gate_id=item["check"], label=item["label"], status=BLOCKED,
                detail=item.get("detail", "")[:220],
                why=item.get("why", "Formal E2 進入條件之一。"),
            )
        )
    return tuple(gates)


def _formal_run_gate(freeze_dir: Path) -> tuple[Gate, ...]:
    """正式執行本身。

    **不得因為前置條件通過就標成 COMPLETE。** 前置條件說的是「可以跑」，
    不是「跑完了」—— 這一格是整份研究最容易被誤讀的地方。
    """
    from pcmef.experiments.final_gate import progress as final_progress

    report = final_progress(freeze_dir)
    ready = bool(report.get("ready_for_final_e2"))
    cleared = report.get("cleared", 0)
    total = report.get("total", 8)
    return (
        Gate(
            gate_id="formal_e2.run",
            label="Formal E2 正式執行",
            status=NOT_STARTED,
            detail=(
                f"前置條件 {cleared} / {total}"
                + ("；已可進入正式執行。" if ready else "；尚未達到進入條件。")
            ),
            why="一次性正式實驗，跑完即為結論。",
        ),
    )


def pcmef_lifecycle(context: Any = None) -> LifecycleView:
    """PC-MEF 的 lifecycle：通用階段 + 這個研究自己的 gate。"""
    freeze_dir = Path(getattr(context, "freeze_dir", "freeze"))
    profile = getattr(context, "profile", None)

    stages: list[Stage] = []
    for stage_id, title, summary in GENERIC_STAGES:
        gates: tuple[Gate, ...] = ()

        if stage_id == "project-definition":
            frozen = bool(getattr(profile, "is_frozen", False))
            gates = (
                Gate(
                    gate_id="design.frozen",
                    label="研究設計已定稿",
                    status=PASS if frozen else NOT_STARTED,
                    detail=(
                        f"{getattr(profile, 'source_document', '')}"
                        f"（{getattr(profile, 'state', '—')}）"
                    ),
                    why="正式實驗必須對應一份已定稿的研究設計。",
                    project_specific=False,
                ),
            )
        elif stage_id in STAGE_LOCKS:
            gates = _lock_gates(freeze_dir, stage_id)
        elif stage_id == "perception-validation":
            gates = _e1_gates(context)
        elif stage_id == "pre-final-freeze":
            gates = _final_gates(freeze_dir)
        elif stage_id == "formal-experiment":
            gates = _formal_run_gate(freeze_dir)
        elif stage_id == "analysis-publication":
            gates = (
                Gate(
                    gate_id="analysis.pending",
                    label="結果整理與發表",
                    status=NOT_STARTED,
                    detail="待正式實驗完成後進行。",
                    why="Results / Discussion 依 E1 與 E2 的主要指標建立。",
                ),
            )

        stages.append(
            Stage(stage_id=stage_id, title=title, summary=summary, gates=gates)
        )

    return LifecycleView(
        stages=tuple(stages),
        provider=PROVIDER_NAME,
        note=(
            "階段結構是平台通用的；各階段底下的判準是 PC-MEF 這個研究"
            "自己的，並且直接取自 audit.e1_gates 與 final_gate 的判定，"
            "不在此重算。"
        ),
    )


def _register() -> None:
    from pcmef.platform.lifecycle.providers import register

    register(PROVIDER_NAME, pcmef_lifecycle)


_register()
