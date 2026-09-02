# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.cli 的 formal 子指令與 pcmef.console.formal_routes 共同呼叫；
#         讀 freeze/ACTIVE_LINEAGE.json 解析出的 lock 目錄、
#         base manifest 與既有的 run report。**不執行任何 case。**
# 檔案路徑: pcmef/experiments/formal_service.py
# 產生時間: 2026-09-02 15:50 +08:00
# 版本: v0.1.0
# 功能說明: Formal E2 的起跑前檢查與現況查詢，做成 CLI 與 Web 共用的一層。
# 模組定位: SAI v0.6.0 §3.1「Web 與 CLI 必須透過同一個 Application Service」
#           的落點。Web 不得自己重算 pre-flight —— 兩份實作必然漂移，
#           而漂移的方向通常是畫面上比較寬鬆。
# 主要責任:
#   1. preflight() 回傳逐項檢查與阻擋原因，dry-run 與 formal 判準不同
#   2. latest_report() 讀出最近一次 run 的摘要供畫面顯示
#   3. 兩者都是唯讀：不寫 lock、不寫 report、不呼叫 provider
# 維護提醒:
#   - 不得在 Web 端另寫一份 pre-flight。畫面顯示的 PASS 必須與
#     `formal run-e2` 實際據以放行的是同一組判斷。
#   - 不得讓 dry-run 的判準寬鬆到允許 families 36-43。生成即開封，
#     沒有預演的餘地（NOTE-056）。
#   - 不得在本檔執行任何 case 或呼叫 provider。它會被畫面輪詢。
#   - v0.1.0 新增：首版，對應 P0-7a。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_formal_routes.py -v
#   - py -3.10 -m pcmef.cli formal preflight --mode dry-run
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["preflight", "latest_report", "FINAL_FAMILY_INDICES"]

#: Final E2 的 family 身分。凍在 e2_sample_size.lock，這裡只是複述。
FINAL_FAMILY_INDICES: tuple[int, ...] = tuple(range(36, 44))

DRY_RUN_REPORT = "formal_e2_dry_run.json"
FORMAL_REPORT = "formal_e2_report.json"


def preflight(
    *,
    mode: str = "dry-run",
    base: str | Path = "outputs/perception/formal_e2",
    out: str | Path = "outputs/perception/e2_final",
    lineage_root: str | Path = "freeze",
) -> dict[str, Any]:
    """Final E2 的起跑前檢查。**不執行任何 case。**

    回傳 `{"checks": [...], "lineage": {...} | None, "blockers": [...],
    "allowed": bool}`。`blockers` 為空才可啟動。
    """
    from pcmef.core.active_lineage import ActiveLineageError, resolve_active_lineage

    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    def record(name: str, passed: bool, detail: str, blocking: bool = True) -> None:
        # `blocking` 必須進 payload：一條不通過但不阻擋的檢查，在畫面上
        # 若與真正的 FAIL 長得一樣，讀的人會看到「FAIL 卻 ALLOWED」而
        # 不知道該信哪一個。
        checks.append(
            {
                "check": name,
                "passed": bool(passed),
                "blocking": bool(blocking),
                "detail": detail,
            }
        )
        if blocking and not passed:
            blockers.append(f"{name}: {detail}")

    try:
        resolved = resolve_active_lineage(lineage_root)
        record(
            "active_lineage_resolves", True,
            f"{resolved.freeze_dir.as_posix()} ({len(resolved.lock_hashes)} locks)",
        )
    except ActiveLineageError as error:
        record("active_lineage_resolves", False, str(error)[:220])
        return {
            "mode": mode, "checks": checks, "lineage": None,
            "blockers": blockers, "allowed": False,
        }

    from pcmef.core.locks import LockStore

    store = LockStore(resolved.freeze_dir)
    formal_config = store.load("formal_config")
    expected_hash = str(formal_config.get("scenario_set_hash", ""))
    record(
        "formal_config_pins_the_final_scenario_set",
        bool(expected_hash),
        f"scenario_set_hash={expected_hash[:16]}",
    )

    base_path = Path(base)
    manifest = base_path / "dataset_manifest.json"
    generated: dict[str, Any] = {}
    if manifest.exists():
        generated = json.loads(manifest.read_text(encoding="utf-8"))
    indices = [int(i) for i in generated.get("family_indices", [])]

    if mode == "formal":
        if not manifest.exists():
            record(
                "final_scenario_set_generated", False,
                f"{manifest.as_posix()} does not exist; generate families 36-43 first",
            )
        else:
            actual = str(generated.get("scenario_set_hash", ""))
            record(
                "final_scenario_set_matches_formal_config",
                bool(actual) and actual == expected_hash,
                f"manifest={actual[:16] or '<absent>'} lock={expected_hash[:16]}",
            )
            record(
                "final_family_indices_are_36_to_43",
                tuple(indices) == FINAL_FAMILY_INDICES,
                f"family_indices={indices}",
            )
    else:
        # dry run 反過來檢查：final partition 生成即開封，不得預演。
        record(
            "dry_run_does_not_touch_the_sealed_partition",
            not any(index >= FINAL_FAMILY_INDICES[0] for index in indices),
            f"base={base_path.as_posix()} family_indices={indices or '<none>'}",
        )

    target = Path(out) / (DRY_RUN_REPORT if mode == "dry-run" else FORMAL_REPORT)
    occupied = target.exists()
    record(
        "output_location_is_free",
        not occupied,
        target.as_posix() + (
            "  (a dry run may overwrite its own previous report)"
            if occupied and mode == "dry-run" else ""
        ),
        # dry run 可以覆寫自己的預演結果；formal 不行，那是 one-shot。
        blocking=mode == "formal",
    )

    return {
        "mode": mode,
        "checks": checks,
        "lineage": resolved.to_manifest(),
        "blockers": blockers,
        "allowed": not blockers,
    }


def latest_report(out: str | Path = "outputs/perception/e2_final") -> dict[str, Any] | None:
    """讀出最近一次 run 的摘要。formal 優先於 dry run。

    只挑畫面需要的欄位，不整份丟給前端 —— traces 有幾百列。
    """
    directory = Path(out)
    for filename in (FORMAL_REPORT, DRY_RUN_REPORT):
        path = directory / filename
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        routing = document.get("routing", {})
        return {
            "path": path.as_posix(),
            "report_id": document.get("report_id"),
            "created_at": document.get("created_at"),
            "dry_run": bool(document.get("dry_run")),
            "scientific_result": bool(document.get("scientific_result")),
            "llm_arm_evaluated": bool(document.get("llm_arm_evaluated")),
            "code_version": document.get("code_version", "")[:12],
            "total_rows": document.get("dataset", {}).get("total_rows"),
            "family_indices": document.get("dataset", {}).get("family_indices"),
            "route_counts": routing.get("counts", {}),
            "escalated_cases": routing.get("escalated_cases"),
            "escalation_rate": routing.get("escalation_rate"),
            "llm_called_cases": routing.get("llm_called_cases"),
            "provider_calls": routing.get("actual_provider_calls"),
            "provider_calls_by_role": routing.get("actual_provider_calls_by_role"),
            "skipped_escalated_cases": document.get("skipped_escalated_cases"),
            "results": document.get("results", {}),
            "per_condition": document.get("per_condition", {}),
            "statistics": document.get("statistics", {}),
            "statistics_config": document.get("statistics_config", {}),
            "frozen_lock_hashes": document.get("frozen_lock_hashes", {}),
            "claim_boundary": document.get("claim_boundary"),
            "dry_run_meaning": document.get("dry_run_meaning"),
        }
    return None
