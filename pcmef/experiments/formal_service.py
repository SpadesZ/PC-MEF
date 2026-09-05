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

__all__ = [
    "preflight", "latest_report", "formal_status", "FINAL_FAMILY_INDICES",
]

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

    # severity 是 stress set 的形狀，也就是整場 Formal E2 吃進去的資料。
    # 它不可設定，因此這裡不是「檢查使用者填得對不對」，而是把 executor
    # 屆時會還原到的那兩個數字先攤在畫面上（NOTE-072）。
    severity: dict[str, float] | None = None
    try:
        from pcmef.experiments.e2_formal import frozen_severity

        severity, allocation = frozen_severity(resolved.freeze_dir)
        record(
            "severity_is_restored_from_the_frozen_selection", True,
            f"vision={severity['vision']} tof={severity['tof']} "
            f"(e2_sample_size.lock, reselection_forbidden="
            f"{bool(allocation.get('reselection_forbidden'))})",
        )
    except Exception as error:  # noqa: BLE001 - 讀不到就是阻擋理由
        record(
            "severity_is_restored_from_the_frozen_selection", False,
            str(error)[:220],
        )

    # --- one-shot：由 claim 判定，不由「報告檔在不在」推論（NOTE-079）------
    from pcmef.experiments.e2_formal import run_artifact_root
    from pcmef.experiments.run_claim import (
        STATE_COMPLETE, STATE_INTERRUPTED, STATE_RESERVED, STATE_RUNNING,
        read_claim,
    )

    formal_root = run_artifact_root(out, dry_run=False)
    claim = read_claim(formal_root)
    # 舊版面沒有 claim 檔，跑完只留報告。那份報告一樣代表 one-shot 已被
    # 消耗，因此同樣擋下第二次 —— 換機制不該讓舊資料失去保護。
    legacy_report = (
        (formal_root / FORMAL_REPORT).exists() or (Path(out) / FORMAL_REPORT).exists()
    )
    if mode == "formal":
        state = None if claim is None else str(claim.get("state", ""))
        record(
            "formal_run_claim_is_available",
            claim is None and not legacy_report,
            "no claim; the one-shot has not been taken"
            if claim is None and not legacy_report else
            "a formal report exists without a claim (pre-2026-09-05 layout); "
            "the one-shot has already been consumed" if claim is None else
            f"claim {claim.get('claim_id')} is {state}"
            + (
                " — Formal E2 已完成，永久封閉" if state == STATE_COMPLETE
                else " — 另一個請求持有它" if state in (STATE_RESERVED, STATE_RUNNING)
                else " — 已開封，只能以相同 identity resume（--resume）"
                if state == STATE_INTERRUPTED else " — 狀態不可讀，fail-closed"
            ),
        )
    else:
        # 預演不取 claim，但要看得到 formal 的現況：一個已經 COMPLETE 的
        # 專案裡，再跑預演是合法的，而畫面必須說得出正式的那一次已經跑完。
        record(
            "formal_claim_state（僅供參考，不阻擋預演）",
            True,
            "none" if claim is None else str(claim.get("state", "")),
            blocking=False,
        )

    # 預演各自有 run-specific root，因此不會互相覆蓋，也不會碰到 formal
    # 的目錄 —— 這一條先前不成立（NOTE-078）。
    if mode == "dry-run":
        record(
            "dry_run_writes_to_its_own_root",
            True,
            f"{Path(out).as_posix()}/dry_runs/<run_id>/  "
            "（report、trace、stress、preview 同屬一次 run）",
            blocking=False,
        )

    # --- Final E2 的八項前置條件（NOTE-077）--------------------------------
    #
    # 只在 formal 模式評估。預演不開封 final partition，那八項對它沒有意義，
    # 而把它們掛在預演上只會讓「預演永遠 BLOCKED」。
    final_gate_checks: list[dict[str, Any]] = []
    if mode == "formal":
        from pcmef.experiments.final_gate import evaluate as evaluate_final_gate

        final_gate_checks = evaluate_final_gate(
            resolved.freeze_dir,
            generated_manifest=generated,
            expected_scenario_set_hash=expected_hash,
            final_family_indices=FINAL_FAMILY_INDICES,
        )
        for item in final_gate_checks:
            record(item["check"], item["passed"], item["detail"])

    return {
        "mode": mode,
        "checks": checks,
        "lineage": resolved.to_manifest(),
        "blockers": blockers,
        "allowed": not blockers,
        "frozen_severity": severity,
        "claim": claim,
        "formal_root": formal_root.as_posix(),
        "final_gate": final_gate_checks,
    }


def formal_status(out: str | Path = "outputs/perception/e2_final") -> dict[str, Any]:
    """Formal E2 的 one-shot 現況。**唯讀，不執行任何 case。**

    畫面必須能回答「已經跑過了嗎、報告在哪、為什麼不能再跑」。先前這三件
    事只存在於 pre-flight 的一列 `output_location_is_free`，而那一列在
    formal 尚未跑過時顯示 PASS —— 讀起來像「這個位置沒問題」，看不出它
    正是 one-shot 的閘門。
    """
    from pcmef.experiments.e2_formal import DRY_RUN_SUBDIR, run_artifact_root
    from pcmef.experiments.run_claim import (
        STATE_COMPLETE, STATE_INTERRUPTED, STATE_RESERVED, STATE_RUNNING,
        claim_path, read_claim,
    )

    directory = Path(out)
    formal_root = run_artifact_root(directory, dry_run=False)
    formal_path = formal_root / FORMAL_REPORT
    dry_root = directory / DRY_RUN_SUBDIR
    dry_runs = sorted(
        (p.name for p in dry_root.iterdir() if (p / DRY_RUN_REPORT).exists()),
        reverse=True,
    ) if dry_root.is_dir() else []
    # 舊版面：報告直接放在 canonical 目錄底下。仍然認得它，否則
    # 2026-09-05 之前跑出來的東西會從畫面上消失。
    legacy_dry = directory / DRY_RUN_REPORT
    dry_path = (
        (dry_root / dry_runs[0] / DRY_RUN_REPORT) if dry_runs else legacy_dry
    )

    claim = read_claim(formal_root)
    claim_state = None if claim is None else str(claim.get("state", ""))
    # 「跑過了沒有」以 claim **或**報告檔為準。
    #
    # claim 是主判準：一次跑到一半失敗的正式執行沒有報告，但 final partition
    # 已經開封 —— 那時說「尚未執行」是錯的（NOTE-079）。
    #
    # 報告檔仍然算數，是為了 2026-09-05 之前的版面：那時沒有 claim 檔，
    # 跑完只留下報告。只看 claim 會把那種目錄判成「尚未執行」，於是畫面
    # 會邀請使用者再跑一次一次性的實驗。舊資料不該因為機制換新而失去保護。
    legacy_report = formal_path.exists() or (directory / FORMAL_REPORT).exists()
    executed = claim is not None or legacy_report
    completed = claim_state == STATE_COMPLETE or (claim is None and legacy_report)

    # 讀實際存在的那一份：新版面在 formal root 底下，舊版面直接在 canonical
    # 目錄。只讀新位置的話，舊資料會顯示成「已執行但沒有身分」。
    identity: dict[str, Any] = {}
    actual_report = next(
        (p for p in (formal_path, directory / FORMAL_REPORT) if p.exists()), None
    )
    if actual_report is not None:
        try:
            document = json.loads(actual_report.read_text(encoding="utf-8"))
            identity = {
                "report_id": document.get("report_id"),
                "created_at": document.get("created_at"),
                "code_version": str(document.get("code_version", ""))[:12],
                "scientific_result": bool(document.get("scientific_result")),
                "path": actual_report.as_posix(),
            }
        except (json.JSONDecodeError, OSError) as error:
            identity = {
                "unreadable": f"{type(error).__name__}: {error}",
                "path": actual_report.as_posix(),
            }

    reason = ""
    if claim_state == STATE_COMPLETE:
        reason = (
            f"claim 已 COMPLETE（{claim.get('finished_at')}）。Formal E2 是"
            "一次性的：跑完就是結論，重跑會產生第二份互相矛盾的正式結果。"
            "要再跑必須是新的 corrective lineage，那是另一個 identity。"
        )
    elif claim_state in (STATE_RESERVED, STATE_RUNNING):
        reason = (
            f"claim 目前是 {claim_state}（pid {claim.get('pid')} @ "
            f"{claim.get('host')}）。同一時間只有一個請求能持有它。"
        )
    elif claim_state == STATE_INTERRUPTED:
        reason = (
            "上一次正式執行中斷，而 final partition 已經開封。不得重新開始，"
            "只能以相同 identity resume（--resume）。"
        )
    elif claim_state:
        reason = f"claim 狀態不可讀（{claim_state}），fail-closed。"
    elif legacy_report:
        reason = (
            "找到一份正式報告，但沒有對應的 claim —— 這是 2026-09-05 之前的"
            "版面。它仍然代表 Formal E2 已經跑過，因此一樣擋下第二次。"
        )

    return {
        "one_shot": True,
        "canonical_out": directory.as_posix(),
        "formal_root": formal_root.as_posix(),
        "dry_runs_root": dry_root.as_posix(),
        "report_path": formal_path.as_posix(),
        "dry_run_report_path": dry_path.as_posix(),
        "dry_run_ids": dry_runs,
        "already_executed": executed,
        "completed": completed,
        "claim": claim,
        "claim_state": claim_state,
        "claim_path": claim_path(formal_root).as_posix(),
        "dry_run_present": dry_path.exists(),
        "identity": identity,
        "blocked_reason": reason,
        # 純文字，**不含** markdown 標記。這個字串直接進 HTML，星號不會被
        # 渲染成粗體，只會原樣印在畫面上；要強調就在樣板裡用 <strong>。
        "record_vs_report": (
            "console 的執行紀錄（run.json、log）是這次操作的痕跡，可以有很多筆；"
            "科學結果只有上面這一份，寫在 canonical 位置。"
            "刪掉一筆執行紀錄不會刪掉報告，也不會讓 one-shot 重新開放。"
        ),
    }


def report_candidates(out: str | Path) -> list[Path]:
    """依「正式優先、預演取最新」列出可讀的報告。

    formal 與每一次 dry-run 各有自己的 root（NOTE-078），因此這裡不能再
    只看 canonical 目錄底下那兩個檔名。舊版面的兩個檔案仍然列入，讓
    2026-09-05 之前跑出來的報告不會突然從畫面上消失。
    """
    from pcmef.experiments.e2_formal import DRY_RUN_SUBDIR, run_artifact_root

    directory = Path(out)
    found = [run_artifact_root(directory, dry_run=False) / FORMAL_REPORT]
    dry_root = directory / DRY_RUN_SUBDIR
    if dry_root.is_dir():
        # 最新的 dry-run 排前面。取 mtime 而非目錄名：run_id 的格式由呼叫端
        # 決定，這一層不該假設它可排序。
        found += sorted(
            (p / DRY_RUN_REPORT for p in dry_root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
            reverse=True,
        )
    # 舊版面（report 直接放 canonical 目錄底下）。
    found += [directory / FORMAL_REPORT, directory / DRY_RUN_REPORT]
    return [p for p in found if p.exists()]


def latest_report(out: str | Path = "outputs/perception/e2_final") -> dict[str, Any] | None:
    """讀出最近一次 run 的摘要。formal 優先於 dry run。

    只挑畫面需要的欄位，不整份丟給前端 —— traces 有幾百列。
    """
    for path in report_candidates(out):
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
            # Primary endpoint。少了這三個欄位，Results 首頁就只剩 overall
            # macro-F1，而那個數字回答的不是 E2 問的問題 —— 一個把平均拉高
            # 卻讓最弱條件繼續崩掉的方法沒有展示穩健性（實驗計畫 v1.2 §5）。
            "worst_condition": document.get("worst_condition_macro_f1", {}),
            "worst_condition_at": document.get("worst_condition_at", {}),
            "worst_condition_statistics": document.get(
                "worst_condition_statistics", {}
            ),
            "primary_endpoint": document.get("primary_endpoint", {}),
            "statistics": document.get("statistics", {}),
            "statistics_config": document.get("statistics_config", {}),
            "frozen_lock_hashes": document.get("frozen_lock_hashes", {}),
            "claim_boundary": document.get("claim_boundary"),
            "dry_run_meaning": document.get("dry_run_meaning"),
        }
    return None
