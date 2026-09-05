# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.experiments.formal_service.preflight() 在 mode="formal"
#         時呼叫；讀 freeze/ 的 amendment 與 lock、
#         outputs/llm_validation/real_agent_validation.json、
#         regression/ 的快照，以及 final partition 的 **manifest**。
#         **不讀 families 36-43 的任何觀測資料，不執行 case，不呼叫 provider。**
# 檔案路徑: pcmef/experiments/final_gate.py
# 產生時間: 2026-09-05 14:45 +08:00
# 版本: v0.1.0
# 功能說明: STATUS.md 列的 Final E2 八項前置條件，改成可機械判定的閘門。
# 模組定位: 「開封前還缺什麼」的**唯一**判準。先前這八項只寫在 STATUS.md
#           的散文裡 —— 文件說「必須先凍結」，而程式沒有一行檢查它，
#           於是唯一的防線是操作者記得。記得不是機制（NOTE-077）。
# 主要責任:
#   1. FINAL_E2_PRECONDITIONS 定義八項，每項自帶可讀的失敗說明
#   2. evaluate() 逐項判定，回傳與 pre-flight 相同形狀的 check 列表
#   3. 每一項都只讀 metadata：amendment 檔、lock 欄位、驗證報告、manifest
# 維護提醒:
#   - 不得因為「反正還沒到那一步」而讓任何一項預設 PASS。這八項的用途正是
#     在還沒到那一步時擋住；預設 PASS 會讓閘門在最需要的時候消失。
#   - 不得在本檔讀取 families 36-43 的 RGB/ToF/npy。第八項只看 manifest 的
#     identity 欄位（hash 與 family_indices），那與生成無關。
#   - 不得把判準複製到 UI。畫面與 CLI 必須呼叫同一個 evaluate()，
#     否則兩份會漂移，而漂移的方向通常是畫面比較寬鬆（SAI §3.1）。
#   - 不得把 canonical baseline 的判準放寬成「快照存在即可」。
#     provisional 不是 Golden，那正是 CANONICAL_BLOCKERS 存在的理由。
#   - v0.1.0 新增：首版，對應 P0-3 / NOTE-077。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_final_gate.py -v
#   - py -3.10 -m pcmef.cli formal preflight --mode formal
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "FINAL_E2_PRECONDITIONS",
    "REAL_VALIDATION_PATH",
    "PROVISIONAL_SNAPSHOT_PATH",
    "REQUIRED_AMENDMENTS",
    "evaluate",
]

#: AMD-007 real-provider validation 的產物。`llm_real_validation` 寫出它。
REAL_VALIDATION_PATH = Path("outputs/llm_validation/real_agent_validation.json")

#: 目前唯一的行為快照。`canonical: false` —— 它是 Golden Baseline 的前身。
PROVISIONAL_SNAPSHOT_PATH = Path(
    "regression/provisional/thesis_regression_snapshot.json"
)

#: Final E2 之前必須凍結的 amendment。
#:
#: AMD-007 evidence-contract v2、AMD-008 §208 紅線（Web 可觸發 formal）、
#: AMD-009 worst-condition paired CI estimator。三者都已寫成 spec，
#: 而 spec 不是 freeze。
REQUIRED_AMENDMENTS: tuple[str, ...] = ("AMD-007", "AMD-008", "AMD-009")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# 八項判定。每一個回傳 (passed, detail)。
# ---------------------------------------------------------------------------


def _real_provider_validation(ctx: Mapping[str, Any]) -> tuple[bool, str]:
    """AMD-007 的 evidence-contract v2 必須對**真實 provider** 驗證過。

    判準取 `READY_FOR_FINAL_E2 == "YES"` 且 `all_checks_passed`，並要求
    `real_calls > 0` —— 少了最後一條，一份完全用 stub 跑出來的報告也會
    通過，而那正是這一項要排除的情況。
    """
    document = _read_json(Path(ctx.get("real_validation_path", REAL_VALIDATION_PATH)))
    if document is None:
        return False, (
            f"{REAL_VALIDATION_PATH.as_posix()} 不存在；四個 agent 從未對真實 "
            "provider 跑過一次完整 case"
        )
    ready = str(document.get("READY_FOR_FINAL_E2", "")).upper() == "YES"
    passed = bool(document.get("all_checks_passed"))
    calls = int(document.get("real_calls") or 0)
    failed = [
        c.get("check") for c in document.get("checks", []) if not c.get("passed")
    ]
    return (ready and passed and calls > 0), (
        f"READY_FOR_FINAL_E2={document.get('READY_FOR_FINAL_E2')} "
        f"all_checks_passed={passed} real_calls={calls}"
        + (f" failed={failed}" if failed else "")
    )


def _amendment_frozen(amendment_id: str) -> Callable[[Mapping[str, Any]], tuple[bool, str]]:
    def check(ctx: Mapping[str, Any]) -> tuple[bool, str]:
        from pcmef.core.amendments import AmendmentError, AmendmentStore

        store = AmendmentStore(ctx["freeze_dir"])
        if not store.exists(amendment_id):
            return False, (
                f"{store.path_for(amendment_id).as_posix()} 不存在；"
                "spec 已寫不等於已凍結"
            )
        try:
            store.load(amendment_id)
        except AmendmentError as error:
            return False, str(error)[:200]
        return True, f"frozen at {store.path_for(amendment_id).as_posix()}"

    return check


def _llm_runtime_refrozen(ctx: Mapping[str, Any]) -> tuple[bool, str]:
    """`llm_runtime.lock` 必須反映**通過驗證的那一組** prompt 與 schema。

    比的是 hash **值的集合**，不是 dict 相等：lock 以短名為鍵
    （`observation` / `physics` / …），validation 以 task_code 與 schema 檔名
    為鍵，兩份命名本來就不同。比值的集合讓這一項只回答它該回答的問題 ——
    「lock 記的是不是同一批 prompt 與 schema」—— 而不順便要求兩邊的鍵名一致。

    這一項是 `CANONICAL_BLOCKERS` 第二條的機械化：lock 記的是 pre-v2 身分，
    而 real-provider validation 是用 v2 跑的。兩者不同時，Final E2 會在一組
    從未被驗證過的 runtime 下執行，而且沒有任何欄位會顯示這件事。
    """
    from pcmef.core.locks import LockStore

    store = LockStore(ctx["freeze_dir"])
    if not store.exists("llm_runtime"):
        return False, "llm_runtime.lock 不存在"
    lock = store.load("llm_runtime") or {}

    document = _read_json(Path(ctx.get("real_validation_path", REAL_VALIDATION_PATH)))
    if document is None:
        return False, "沒有 real-provider validation 可供比對 runtime identity"

    mismatched = []
    for field in ("prompt_hashes", "schema_hashes"):
        validated = set((document.get(field) or {}).values())
        frozen = set((lock.get(field) or {}).values())
        if not validated:
            return False, f"validation 報告沒有記錄 {field}"
        if validated != frozen:
            mismatched.append(
                f"{field}: lock 有 {len(frozen)} 個、validated 有 {len(validated)} 個，"
                f"交集 {len(frozen & validated)}"
            )
    if mismatched:
        return False, "；".join(mismatched) + " —— lock 記的不是通過驗證的那一版"

    # prompt 與 schema 相符是**必要但不充分**。lock 的 runtime identity 還
    # 包含 timeout、role connection topology 與 runtime_config_hash，而那些
    # 都不在 prompt hash 裡 —— `CANONICAL_BLOCKERS` 第二條說的正是這一塊。
    #
    # validation 報告目前沒有記錄 runtime_config_hash，因此「lock 記的就是
    # 通過驗證的那一組 runtime 設定」**無法被機械證明**。無法證明相符時這一
    # 項必須 FAIL：一個在該擋的時候放行的閘門，比沒有閘門更糟。
    validated_hash = str(
        document.get("runtime_config_hash")
        or (document.get("runtime_identity") or {}).get("runtime_config_hash")
        or ""
    )
    frozen_hash = str(lock.get("runtime_config_hash", ""))
    if not validated_hash:
        return False, (
            f"prompt/schema 相符，但 {REAL_VALIDATION_PATH.name} 沒有記錄 "
            "runtime_config_hash —— 無法證明 lock 的 runtime 設定"
            f"（{frozen_hash[:16]}）就是通過驗證的那一組。timeout、connection "
            "topology 不在 prompt hash 裡，因此 prompt 相符不足以推論 runtime 相符"
        )
    return frozen_hash == validated_hash, (
        f"prompt/schema 相符；runtime_config_hash lock={frozen_hash[:16]} "
        f"validated={validated_hash[:16]}"
    )


def _formal_config_refrozen(ctx: Mapping[str, Any]) -> tuple[bool, str]:
    """`formal_config.lock` 引用的 llm_runtime 必須是現行那一份。

    formal_config 以 `agent.llm_runtime_hash` 交叉引用 llm_runtime。
    llm_runtime 重凍之後 formal_config 沒跟著重凍，那個引用就指向一份
    已經不存在的身分 —— 而它不會有任何症狀。
    """
    from pcmef.core.locks import LockStore

    store = LockStore(ctx["freeze_dir"])
    for name in ("formal_config", "llm_runtime"):
        if not store.exists(name):
            return False, f"{name}.lock 不存在"
    referenced = str(
        ((store.load("formal_config") or {}).get("agent") or {}).get(
            "llm_runtime_hash", ""
        )
    )
    actual = store.load_hash("llm_runtime")
    return referenced == actual, (
        f"formal_config.agent.llm_runtime_hash={referenced[:16] or '<absent>'} "
        f"llm_runtime={actual[:16]}"
    )


def _canonical_baseline(ctx: Mapping[str, Any]) -> tuple[bool, str]:
    """必須有一份 **canonical** Golden Baseline，不是 provisional 快照。

    `capture_snapshot()` 寫 `canonical: false` 並附上 `canonical_blockers`。
    把 provisional 當成 Golden，等於宣稱 canonical identity 已經確定 ——
    而那正是這八項要先確定的東西。
    """
    path = Path(ctx.get("snapshot_path", PROVISIONAL_SNAPSHOT_PATH))
    document = _read_json(path)
    if document is None:
        return False, f"{path.as_posix()} 不存在"
    if not bool(document.get("canonical")):
        blockers = document.get("canonical_blockers") or []
        return False, (
            f"{path.as_posix()} 的 canonical=false；尚有 {len(blockers)} 條 "
            "canonical_blocker 未解除"
        )
    return True, f"{path.as_posix()} canonical=true"


def _final_manifest(ctx: Mapping[str, Any]) -> tuple[bool, str]:
    """families 36-43 的 manifest 必須存在且身分與 formal_config 相符。

    **只讀 manifest 的 identity 欄位**（scenario_set_hash 與 family_indices），
    不讀任何 RGB/ToF 觀測。生成即開封，但確認「有沒有生成」本身不是開封。
    """
    generated = ctx.get("generated_manifest") or {}
    expected = str(ctx.get("expected_scenario_set_hash", ""))
    indices = [int(i) for i in generated.get("family_indices", [])]
    if not generated:
        return False, "final partition 的 dataset_manifest.json 不存在（尚未生成）"
    actual = str(generated.get("scenario_set_hash", ""))
    final_indices = tuple(ctx.get("final_family_indices") or ())
    return (
        bool(actual) and actual == expected and tuple(indices) == final_indices
    ), (
        f"manifest={actual[:16] or '<absent>'} lock={expected[:16]} "
        f"family_indices={indices}"
    )


#: 八項前置條件，順序即它們必須被解除的順序。
#:
#: `key` 進 check 名稱，`why` 是失敗時畫面上要說的那句話 —— 一個只寫
#: 「FAIL」的閘門會讓人不知道下一步該做什麼。
FINAL_E2_PRECONDITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "amd007_real_provider_validation",
        "label": "AMD-007 real-provider validation",
        "check": _real_provider_validation,
        "why": "四個 agent 必須對真實 provider 跑過一次完整 case，不能只有 stub。",
    },
    {
        "key": "amd007_frozen",
        "label": "AMD-007 已凍結",
        "check": _amendment_frozen("AMD-007"),
        "why": "evidence-contract v2 的判準變更必須先凍成 amendment。",
    },
    {
        "key": "amd008_frozen",
        "label": "AMD-008 已凍結",
        "check": _amendment_frozen("AMD-008"),
        "why": "§208 紅線變更（Web 可觸發 formal）必須先凍成 amendment。",
    },
    {
        "key": "amd009_frozen",
        "label": "AMD-009 已凍結",
        "check": _amendment_frozen("AMD-009"),
        "why": "worst-condition paired CI estimator 是 primary endpoint 的判準。",
    },
    {
        "key": "llm_runtime_refrozen",
        "label": "llm_runtime 已重凍",
        "check": _llm_runtime_refrozen,
        "why": "lock 記的 runtime identity 必須就是通過驗證的那一組。",
    },
    {
        "key": "formal_config_refrozen",
        "label": "formal_config 已重凍",
        "check": _formal_config_refrozen,
        "why": "formal_config 交叉引用 llm_runtime；後者重凍前者必須跟上。",
    },
    {
        "key": "canonical_golden_baseline",
        "label": "canonical Golden Baseline",
        "check": _canonical_baseline,
        "why": "provisional 快照不是 Golden；canonical identity 必須先確定。",
    },
    {
        "key": "final_partition_manifest",
        "label": "families 36-43 final manifest",
        "check": _final_manifest,
        "why": "final partition 必須已生成且身分與 formal_config 相符。",
    },
)


def evaluate(
    freeze_dir: str | Path,
    *,
    generated_manifest: Mapping[str, Any] | None = None,
    expected_scenario_set_hash: str = "",
    final_family_indices: tuple[int, ...] = (),
    real_validation_path: str | Path = REAL_VALIDATION_PATH,
    snapshot_path: str | Path = PROVISIONAL_SNAPSHOT_PATH,
) -> list[dict[str, Any]]:
    """逐項判定八個前置條件。**唯讀。**

    回傳與 `formal_service.preflight()` 的 check 相同形狀的列表，讓兩者能
    直接串在同一張表上 —— 使用者看到的是一份清單，不是兩份判準。

    任何一項拋例外都被接住並轉成 FAIL：這道閘門的用途是擋住，而一個因為
    讀檔失敗就整個消失的閘門比沒有閘門更糟。
    """
    context = {
        "freeze_dir": Path(freeze_dir),
        "generated_manifest": dict(generated_manifest or {}),
        "expected_scenario_set_hash": expected_scenario_set_hash,
        "final_family_indices": tuple(final_family_indices),
        "real_validation_path": Path(real_validation_path),
        "snapshot_path": Path(snapshot_path),
    }

    results: list[dict[str, Any]] = []
    for spec in FINAL_E2_PRECONDITIONS:
        try:
            passed, detail = spec["check"](context)
        except Exception as error:  # noqa: BLE001 - 閘門不得因讀檔失敗而消失
            passed, detail = False, f"{type(error).__name__}: {error}"[:220]
        results.append(
            {
                "check": f"final_gate.{spec['key']}",
                "label": spec["label"],
                "passed": bool(passed),
                "blocking": True,
                "detail": detail,
                "why": spec["why"],
            }
        )
    return results


def progress(freeze_dir: str | Path, **kwargs: Any) -> dict[str, Any]:
    """「現在在哪一步、下一步做什麼、還缺什麼」——**由判定推導，不是手寫**。

    STATUS.md 的散文會過期，而且過期時看起來完全正常。這個函式把同一件事
    從八項判定算出來：解除幾項就是進度，第一個未解除的就是下一步。

    刻意不定義 Final E2 之後的階段：那超出這道閘門知道的範圍，
    而編一個「接下來寫論文」只是把散文搬進程式裡。
    """
    checks = evaluate(freeze_dir, **kwargs)
    done = [c for c in checks if c["passed"]]
    remaining = [c for c in checks if not c["passed"]]
    nxt = remaining[0] if remaining else None
    return {
        "total": len(checks),
        "cleared": len(done),
        "remaining": len(remaining),
        "ready_for_final_e2": not remaining,
        "stage": (
            "Final E2 前置條件已全部解除；下一步是取得 one-shot claim 並執行"
            if not remaining else
            f"Final E2 前置條件解除 {len(done)} / {len(checks)}"
        ),
        "next_step": None if nxt is None else {
            "check": nxt["check"],
            "label": nxt["label"],
            "why": nxt["why"],
            "detail": nxt["detail"],
        },
        # 順序即它們必須被解除的順序，因此這份清單同時是「後續順序」。
        "blockers": [
            {"check": c["check"], "label": c["label"], "why": c["why"],
             "detail": c["detail"]}
            for c in remaining
        ],
        "cleared_items": [
            {"check": c["check"], "label": c["label"], "detail": c["detail"]}
            for c in done
        ],
    }
