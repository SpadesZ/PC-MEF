# PC-MEF Research System source maintenance contract
# 上下游: 讀 freeze/ 的 gate.lock / reliability_final.lock / statistics_config.lock，
#         outputs/perception/ds_v2 的 frozen 權重與 preprocessing，
#         以及一份 clean paired dataset manifest；
#         呼叫 perception.stress、perception.gate、perception.pcmef_orchestrator
#         與 agents.pcmef_agents；寫出 <out>/formal_e2_report.json。
#         **不重訓、不重搜門檻、不改溫度、不改 severity。**
# 檔案路徑: pcmef/experiments/e2_formal.py
# 產生時間: 2026-09-01 15:30 +08:00
# 版本: v0.8.0
# 功能說明: Full PC-MEF 的 Formal E2 執行器。完整走已凍結的決策架構 ——
#           frozen CNN -> frozen 溫度 -> D/U/Q 與獨立 q_v/q_t ->
#           reliability_route -> 四個真 agent -> arbitration_support_bridge ->
#           selective_escalation_bridge。
# 模組定位: 已凍結架構的**唯一** formal 執行入口。它「不是」gate.run_formal_e2
#           的改良版 —— 那一條走 apply_gate 加決定性替身仲裁，
#           自報 llm_arm_evaluated=False，不得用於 Full PC-MEF。
# 主要責任:
#   1. load_frozen_decision_stack() 由 lock 還原門檻、溫度與 reliability anchors
#   2. CountingAdapter 把「實際 provider 呼叫次數」與「escalated case 數」分開量
#   3. prepare_cases() 產生 stress 列並算出 p_V / p_T / D-U-Q / q_m / route
#   4. execute_full_pcmef_cases() 是**唯一**的執行迴圈，正式執行與驗證共用
#   5. run_formal_e2_full() 組裝 formal identity、呼叫上者、彙總並寫報告
#   6. G1-G5 五條臂；G4 由既有 route 與 fused 導出，零 provider 呼叫
#   7. worst_condition_macro_f1() primary endpoint 的點估計與最弱 condition
#   8. _cluster_statistics() 以 family cluster bootstrap 產出成對比較
#   9. _worst_condition_statistics() primary endpoint 的成對 CI（AMD-009）
#  10. emit_trace() 旁路寫出 decision trace，決策完成後才呼叫（NOTE-064）
#  11. artifact_cache_root 接上 content-addressed cache（§48 / FR-031）
# 維護提醒:
#   - 不得在本檔重新擬合任何東西。門檻、溫度、anchors 一律由 lock 還原；
#     refit 一次就等於 Formal E2 自己決定了自己的判準。
#   - 不得在 formal 模式讓 deterministic arbiter 頂替 LLM。那會讓
#     「PC-MEF 的 LLM 臂」在報告上成立而實際從未執行。
#   - 不得 drop 任何 case。retry 耗盡就是 ABORT_FORMAL_RUN；
#     少一個 case 就是分母悄悄變小，那正是 bounded retry 要防的事。
#   - 不得對 non-escalated case 呼叫 provider。selective escalation 的整個
#     論點就是「傳統證據足夠就不叫 LLM」，多叫一次就推翻了它。
#   - 不得讓 G4 reliability_routing 走任何會呼叫 provider 的分支。它的定義
#     是「路由但不仲裁」；一旦它叫了 LLM，G5 vs G4 就不再是仲裁的增量
#     （NOTE-061）。也不得把它從 dry run 拿掉 —— 它不依賴 LLM。
#   - 不得讓 dry run 輸出 pcmef_full 的數字。那一欄會等於 fixed_fusion，
#     讀起來像「PC-MEF 沒有比固定融合好」，而它其實從未執行（NOTE-056）。
#   - 不得只報 worst-condition 的最小值而不報它落在哪個 condition。
#     不同方法的最弱條件可能不同，那個對比本身就是結果（NOTE-062）。
#   - 不得讓 decision trace 的任何回傳值進入決策路徑。它在決策完成之後
#     才被呼叫，例外一律吞掉 —— 寫不出 trace 只損失可讀性，不該讓一次
#     formal run 失敗（NOTE-064）。
#   - 不得改用 scenario-level bootstrap。statistics_config.lock 凍的是
#     physical_scene_family cluster，且所有方法共用同一組 cluster。
#   - 不得在別處另寫一份等價的執行迴圈。execute_full_pcmef_cases() 抽出來
#     就是為了讓驗證跑到與正式執行同一段程式；再複製一份，驗到的又會是
#     「另一份很像的實作」（NOTE-055）。
#   - v0.1.0 新增：首版 Full PC-MEF formal executor，決策見 NOTE-051。
#   - v0.2.0 新增：抽出 execute_full_pcmef_cases()，freeze_dir 改必填。
#   - v0.3.0 新增：llm_mode dry run；dry run 不輸出 pcmef_full 臂（NOTE-056）。
#   - v0.4.0 新增：G4 reliability_routing 臂與其成對統計（NOTE-061）。
#   - v0.5.0 新增：worst-condition macro-F1 點估計與 primary_endpoint（NOTE-062）。
#   - v0.6.0 新增：primary endpoint 的成對 CI（NOTE-063 / AMD-009）。
#   - 不得把 agent cache 當成旁路。它**會**改變是否呼叫 provider，
#     因此讀寫失敗不得被吞掉 —— 這一點與 decision trace 正好相反
#     （NOTE-070）。dry run 不接快取：那一模式本來就不呼叫 provider。
#   - 不得在四個角色綁到不同 model/revision 時仍然啟用快取。cache key
#     只有一個 provider_model_id，命中時會安靜地服務另一個模型的答案。
#   - v0.7.0 新增：decision trace 旁路輸出（NOTE-064）。
#   - v0.8.0 新增：content-addressed agent cache 接線（NOTE-070）。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_formal_executor.py -v
#   - py -3.10 -m pytest tests/e2/test_execute_full_pcmef_cases.py -v
#   - py -3.10 -m pytest tests/e2/test_dry_run_mode.py -v
#   - py -3.10 -m pytest tests/e2/test_reliability_routing_arm.py -v
#   - py -3.10 -m pytest tests/e2/test_worst_condition_endpoint.py -v
#   - py -3.10 -m pytest tests/e2/test_worst_condition_bootstrap.py -v
#   - py -3.10 -m pytest tests/e2/test_decision_trace.py -v
#   - py -3.10 -m pytest tests/cache/test_cached_case.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from pcmef.core.constants import CLASS_ORDER

__all__ = [
    "FormalE2Error",
    "CountingAdapter",
    "load_frozen_decision_stack",
    "frozen_severity",
    "resolve_severity",
    "run_artifact_root",
    "FORMAL_SUBDIR",
    "DRY_RUN_SUBDIR",
    "prepare_cases",
    "execute_full_pcmef_cases",
    "worst_condition_macro_f1",
    "run_formal_e2_full",
]

DS_V2 = Path("outputs/perception/ds_v2")

#: Formal E2 的 severity 凍在哪裡。`stress.select_severity()` 在
#: gate-validation 上選出後寫進這份 lock 的 `severity_allocation`，
#: 且該 payload 自帶 `reselection_forbidden: true`。
SEVERITY_LOCK = "e2_sample_size"
SEVERITY_FIELD = "severity_allocation"

#: canonical out 底下，formal 與 dry-run 各自的子目錄。
#:
#: 先前兩者**共用** `<out>/trace` 與 `<out>/stress`，只有報告檔名不同。
#: 於是正式跑完之後再跑一次預演，會直接覆蓋掉正式的 trace 與 stress ——
#: 而報告還在，看起來一切正常。一次性保護了報告，卻沒保護它的證據
#: （NOTE-078）。
#:
#: 現在：formal 落在 `<out>/formal/`（整個目錄不可變）；每一次 dry-run
#: 落在 `<out>/dry_runs/<run_id>/`，彼此也不互相覆蓋。report、trace、
#: stress、preview 因此必然同屬一次 run。
FORMAL_SUBDIR = "formal"
DRY_RUN_SUBDIR = "dry_runs"


def run_artifact_root(
    out_dir: str | Path, *, dry_run: bool, run_id: str | None = None
) -> Path:
    """這一次 run 的**所有**產物落在哪裡。

    formal 與 dry-run 完全不共用任何路徑，兩次 dry-run 之間也不共用。
    這是「舊 run 頁不可讀到新 run 產物」的實作方式 —— 不是靠畫面過濾，
    而是靠它們根本不在同一個目錄裡。
    """
    out = Path(out_dir)
    if not dry_run:
        return out / FORMAL_SUBDIR
    if not run_id:
        raise FormalE2Error(
            "a dry run needs its own run_id: dry runs share the canonical out "
            "directory, and without a per-run subdirectory the second rehearsal "
            "overwrites the first one's trace and stress set"
        )
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_.")
    if not safe:
        raise FormalE2Error(f"run_id {run_id!r} has no usable characters")
    return out / DRY_RUN_SUBDIR / safe

#: 正式執行：escalated case 呼叫四個真 agent。
LLM_MODE_EXECUTE = "execute"
#: 零成本預演：資料生成、routing 與統計形狀照跑，但完全不呼叫 provider。
#: **不是**降級的正式執行 —— pcmef_full 整條臂會被排除在報告之外。
LLM_MODE_SKIP = "skip"
LLM_MODES = frozenset({LLM_MODE_EXECUTE, LLM_MODE_SKIP})


class FormalE2Error(RuntimeError):
    """Formal E2 的前提不成立。一律 fail-closed，不得降級繼續。"""


# ---------------------------------------------------------------------------
# provider 呼叫計數
# ---------------------------------------------------------------------------


@dataclass
class CountingAdapter:
    """包住 provider adapter，數**實際送出的請求**。

    escalated case 數與實際呼叫次數必須分開量：一個 case 會呼叫四個角色，
    而 retry 會讓某個角色被呼叫不只一次。把兩者混為一談的話，
    「non-escalated 不呼叫 provider」這條就無法被獨立驗證。
    """

    inner: Any
    calls: int = 0
    calls_by_task: dict[str, int] = field(default_factory=dict)
    #: 逐角色累計的實際 token 用量。thinking token 單獨記 ——
    #: 它照 output 計費卻不在 candidatesTokenCount 裡，是成本估算誤差最大的一項。
    usage_by_task: dict[str, dict[str, int]] = field(default_factory=dict)

    def invoke(self, connection, model, task_code, payload, runtime_cfg):
        self.calls += 1
        key = str(task_code)
        self.calls_by_task[key] = self.calls_by_task.get(key, 0) + 1
        response = self.inner.invoke(connection, model, task_code, payload, runtime_cfg)
        bucket = self.usage_by_task.setdefault(
            key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                  "thoughts_tokens": 0}
        )
        bucket["calls"] += 1
        bucket["prompt_tokens"] += int(getattr(response, "prompt_tokens", 0) or 0)
        bucket["completion_tokens"] += int(getattr(response, "completion_tokens", 0) or 0)
        bucket["thoughts_tokens"] += int(getattr(response, "thoughts_tokens", 0) or 0)
        return response

    def usage_totals(self) -> dict[str, int]:
        total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "thoughts_tokens": 0}
        for bucket in self.usage_by_task.values():
            for key in total:
                total[key] += bucket[key]
        total["billable_output_tokens"] = (
            total["completion_tokens"] + total["thoughts_tokens"]
        )
        return total

    def __getattr__(self, name: str) -> Any:
        # 其餘 adapter 介面原樣轉發；只有 invoke 需要被計數。
        return getattr(self.inner, name)


# ---------------------------------------------------------------------------
# 由 lock 還原決策堆疊
# ---------------------------------------------------------------------------


def load_frozen_decision_stack(freeze_dir: str | Path) -> dict[str, Any]:
    """由 lock 還原 GateRule、ReliabilityModel 與統計設定。**不重新擬合。**

    `freeze_dir` **必填，刻意沒有預設值**。原本的預設是 `"freeze"`，
    而那正是 superseded 的 parent lineage —— 兩份 lineage 的檔名與 schema
    完全相同，讀錯不會拋任何錯誤，只會安靜地用被 AMD-006 更正掉的
    anchors 與門檻跑完整場。呼叫端要嘛明確指定，要嘛先經
    `resolve_active_lineage()` 解析（NOTE-054）。
    """
    from pcmef.core.locks import LockStore
    from pcmef.perception.gate import GateRule, ReliabilityModel

    store = LockStore(freeze_dir)
    store.require("gate", "reliability_final", "statistics_config")
    gate = store.load("gate")
    reliability = store.load("reliability_final")
    statistics = store.load("statistics_config")

    rule = GateRule(
        q_vision_threshold=gate["q_vision_threshold"],
        q_tof_threshold=gate["q_tof_threshold"],
        disagreement_threshold=gate["disagreement_threshold"],
        fusion_weight=gate["fusion_weight"],
        temperature_vision=gate["temperature_vision"],
        temperature_tof=gate["temperature_tof"],
    )
    stats = reliability["fitted_statistics"]
    model = ReliabilityModel(
        q_vision_clean_median=stats["q_vision_clean_median"],
        q_tof_clean_median=stats["q_tof_clean_median"],
        q_vision_degraded_anchor=stats["q_vision_degraded_anchor"],
        q_tof_degraded_anchor=stats["q_tof_degraded_anchor"],
        q_vision_scale=stats["q_vision_scale"],
        q_tof_scale=stats["q_tof_scale"],
    )
    return {
        "rule": rule,
        "reliability_model": model,
        "bootstrap_replicates": statistics["bootstrap_replicates"],
        "bootstrap_seed": statistics["bootstrap_seed"],
        "resample_unit": statistics["resample_unit"],
        "stratification": statistics["stratification"],
        "routing_policy_version": gate["routing_policy_version"],
        "decision_bridge_version": gate["decision_bridge_version"],
        "lock_hashes": {
            name: store.load_hash(name)
            for name in ("gate", "reliability_final", "statistics_config",
                         "validation_pool", "conflict_operational",
                         "e2_sample_size", "llm_runtime", "agent_schema",
                         "inference_firewall")
            if store.exists(name)
        },
    }


# ---------------------------------------------------------------------------
# severity：由 frozen 身分還原，不由呼叫端挑
# ---------------------------------------------------------------------------


def frozen_severity(freeze_dir: str | Path) -> tuple[dict[str, float], dict[str, Any]]:
    """由 `e2_sample_size.lock` 還原 gate-validation 選出的 severity。

    回傳 `(severity, allocation)`：前者是 executor 真正要用的兩個數字，
    後者是 lock 裡那一段的原文，供報告記錄出處。

    **不接受缺欄位的 lock。** severity 是 stress set 的形狀，也就是整場
    Formal E2 吃進去的資料本身；讀不到就沒有任何預設值可以退，因為任何
    預設值都等於這個 executor 自己選了 severity —— 而 `select_severity()`
    的整套事前判準存在的理由，就是不讓執行階段做這個選擇。
    """
    from pcmef.core.locks import LockStore

    store = LockStore(freeze_dir)
    store.require(SEVERITY_LOCK)
    allocation = (store.load(SEVERITY_LOCK) or {}).get(SEVERITY_FIELD)
    if not isinstance(allocation, Mapping):
        raise FormalE2Error(
            f"{SEVERITY_LOCK}.lock has no {SEVERITY_FIELD!r}; the Formal E2 "
            "severity must come from the frozen gate-validation selection and "
            "this executor must not choose it"
        )
    try:
        severity = {
            "vision": float(allocation["vision"]),
            "tof": float(allocation["tof"]),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise FormalE2Error(
            f"{SEVERITY_LOCK}.lock {SEVERITY_FIELD} does not carry a numeric "
            f"vision/tof pair ({error}); refusing to guess the severity"
        ) from None
    return severity, dict(allocation)


def resolve_severity(
    freeze_dir: str | Path, requested: Mapping[str, float] | None
) -> tuple[dict[str, float], dict[str, Any]]:
    """Formal E2 實際要用的 severity，加上它的出處。

    `requested` 是呼叫端（實務上是 CLI 旗標）傳進來的值。**它不是設定，
    是斷言**：給定時必須與 frozen 值逐項相等，否則 fail-closed。

    保留這條旗標而不是直接刪掉，是為了讓既有腳本與文件裡的
    `--vision-severity 2.0` 仍然跑得動；但它現在的語意變成「我聲稱 frozen
    值是這個」，不相等時中止。差別在於：先前傳一個不同的數字會安靜地換掉
    整份 stress set，而報告仍然寫著 severity 來自 frozen 選擇 —— 那份報告
    因此是不實的，且不實之處沒有任何機械痕跡。
    """
    frozen, allocation = frozen_severity(freeze_dir)
    provenance: dict[str, Any] = {
        "lock": SEVERITY_LOCK,
        "field": SEVERITY_FIELD,
        "selected_on": allocation.get("selected_on"),
        "scheme": allocation.get("scheme"),
        "reselection_forbidden": bool(allocation.get("reselection_forbidden")),
        "restored_from_lock": True,
        "cli_override_supplied": requested is not None,
        "cli_override_verified_equal": None,
        "note": (
            "restored from the frozen gate-validation selection; the executor "
            "does not select it and a supplied value is verified, not applied"
        ),
    }
    if requested is None:
        return frozen, provenance

    mismatched = {
        modality: {"supplied": requested.get(modality), "frozen": frozen[modality]}
        for modality in ("vision", "tof")
        if requested.get(modality) is not None
        and float(requested[modality]) != frozen[modality]
    }
    if mismatched:
        raise FormalE2Error(
            "the supplied severity does not match the frozen gate-validation "
            f"selection: {json.dumps(mismatched, sort_keys=True)}. Severity fixes "
            "the stress set, which is the data the whole of Formal E2 consumes; "
            f"it is frozen in {SEVERITY_LOCK}.lock with reselection_forbidden. "
            "Drop the flag to run at the frozen severity."
        )
    provenance["cli_override_verified_equal"] = True
    return frozen, provenance


# ---------------------------------------------------------------------------
# case 準備
# ---------------------------------------------------------------------------


def prepare_cases(
    base_manifest_dir: str | Path,
    stack: dict[str, Any],
    out_dir: str | Path,
    ds_dir: str | Path = DS_V2,
    severity: dict[str, float] | None = None,
    code_version: str = "",
    stress_manifest: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """由 clean base manifest 產生四 condition 的 stress 列並算出決策訊號。

    `stress_manifest` 給定時改為**讀回既有的** stress set 而不是重建。
    驗證階段必須走這條：重建會換掉 stress seed，於是驗證看到的就不是
    當初擬合門檻的那一份資料。Final E2 則相反 —— 那時 stress set 尚未存在，
    必須由本函式產生。
    """
    from pcmef.perception.gate import (
        _logits, _prepare, _quality_batch, duq_signals, load_frozen_models,
        reliability_route, reliability_scores, softmax,
    )
    from pcmef.perception.stress import build_stress_dataset

    say = progress or (lambda _m: None)
    rule, model = stack["rule"], stack["reliability_model"]
    vision_model, tof_model, preprocessing = load_frozen_models(ds_dir)

    base = json.loads(
        (Path(base_manifest_dir) / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    if stress_manifest is not None:
        stress = json.loads(Path(stress_manifest).read_text(encoding="utf-8"))
        say(f"reusing the existing stress set at {stress_manifest}")
    else:
        if severity is None:
            raise FormalE2Error(
                "severity must come from the frozen gate-validation selection; "
                "this executor must not select it"
            )
        say(f"building the stress set at severity {severity}")
        stress = build_stress_dataset(
            base, severity["vision"], severity["tof"],
            Path(out_dir) / "stress", code_version, say,
        )
    rows = stress["rows"]

    rgb_x, tof_x = _prepare(rows, preprocessing)
    labels = np.asarray([r["class_index"] for r in rows])
    p_vision = softmax(_logits(vision_model, rgb_x), rule.temperature_vision)
    p_tof = softmax(_logits(tof_model, tof_x), rule.temperature_tof)
    signals = duq_signals(p_vision, p_tof, _quality_batch(rows))
    q = reliability_scores(signals, model)
    routes = reliability_route(q, signals, rule)

    say(f"  {len(rows)} rows; routes " + json.dumps(
        {str(k): int(v) for k, v in zip(*np.unique(routes, return_counts=True))}
    ))
    return {
        "rows": rows,
        "labels": labels,
        "p_vision": p_vision,
        "p_tof": p_tof,
        "signals": signals,
        "q": q,
        "routes": routes,
        "stress": stress,
        "base_manifest": base,
    }


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------


def _macro_f1(truth: np.ndarray, predicted: np.ndarray) -> float:
    from pcmef.perception.baselines import confusion_matrix, macro_f1

    return macro_f1(confusion_matrix(truth, predicted))[0]


def _accuracy(truth: np.ndarray, predicted: np.ndarray) -> float:
    return float((truth == predicted).mean())


def worst_condition_macro_f1(
    per_condition: Mapping[str, Mapping[str, Mapping[str, float]]],
) -> tuple[dict[str, float], dict[str, str]]:
    """E2 的 primary robustness endpoint：每個方法在四個 condition 中的最低
    macro-F1，以及那個 condition 是哪一個。

    它可由 per-condition 表格推導，但算出來報出去有兩個理由：讀者不必自己
    取 min，而且**哪一個 condition 是最弱的本身就是結果的一部分** ——
    不同方法的最弱條件可能不同，那件事在表格裡看得到卻很容易被略過。

    平手時取字典序最小的 condition，讓輸出不依賴 dict 的插入順序。
    """
    worst: dict[str, float] = {}
    where: dict[str, str] = {}
    for name, block in per_condition.items():
        if not block:
            raise FormalE2Error(
                f"method {name!r} has no per-condition breakdown; the worst-condition "
                "endpoint is undefined without one"
            )
        chosen = min(
            sorted(block), key=lambda condition: block[condition]["macro_f1"]
        )
        worst[name] = float(block[chosen]["macro_f1"])
        where[name] = chosen
    return worst, where


def _worst_condition_statistics(
    labels: np.ndarray,
    predictions: Mapping[str, np.ndarray],
    rows: Sequence[Mapping[str, Any]],
    stack: Mapping[str, Any],
    reference: str,
) -> dict[str, Any]:
    """Primary endpoint 的成對 CI。重抽與 `_cluster_statistics` 完全相同。

    分成兩個入口不是重複：worst-condition 必須在**每個 replicate 內**先分
    condition 再取 min，而 `cluster_bootstrap_delta` 的 metric 拿不到列的
    condition。詳見 `cluster_bootstrap_worst_condition_delta` 的說明與 AMD-009。
    """
    from pcmef.stats.bootstrap import cluster_bootstrap_worst_condition_delta

    return cluster_bootstrap_worst_condition_delta(
        labels,
        predictions,
        [r["physical_scene_family"] for r in rows],
        [r["class_label"] for r in rows],
        [r["condition"] for r in rows],
        _macro_f1,
        reference,
        replicates=stack["bootstrap_replicates"],
        seed=stack["bootstrap_seed"],
    )


def _cluster_statistics(
    labels: np.ndarray,
    predictions: dict[str, np.ndarray],
    rows: Sequence[dict[str, Any]],
    stack: dict[str, Any],
    reference: str,
) -> dict[str, Any]:
    """physical_scene_family cluster bootstrap。所有方法共用同一組 cluster。"""
    from pcmef.stats.bootstrap import cluster_bootstrap_delta

    clusters = [r["physical_scene_family"] for r in rows]
    strata = [r["class_label"] for r in rows]
    out = {}
    for metric_name, metric in (("accuracy", _accuracy), ("macro_f1", _macro_f1)):
        out[metric_name] = cluster_bootstrap_delta(
            labels, predictions, clusters, strata, metric, reference,
            replicates=stack["bootstrap_replicates"],
            seed=stack["bootstrap_seed"],
        )
    return out


# ---------------------------------------------------------------------------
# 主執行器
# ---------------------------------------------------------------------------


def execute_full_pcmef_cases(
    rows: Sequence[Mapping[str, Any]],
    p_vision: np.ndarray,
    p_tof: np.ndarray,
    q: Mapping[str, np.ndarray],
    signals: Mapping[str, np.ndarray],
    routes: np.ndarray,
    rule: Any,
    *,
    agent_runner: Any = None,
    formal: bool = True,
    llm_mode: str = LLM_MODE_EXECUTE,
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
    progress: Callable[[str], None] | None = None,
    case_arbiter: Any = None,
) -> dict[str, Any]:
    """**唯一**的 Full PC-MEF 執行迴圈。逐 case 決策並回傳 F(x) 與 trace。

    這個函式從 `run_formal_e2_full` 抽出來，目的只有一個：讓
    **正式執行與執行驗證跑的是同一段程式**。先前 validator 自己重建了一份
    等價迴圈，於是它證明的是「另一份很像的實作能跑」，而不是
    「正式入口能跑」—— 兩者之間的差異正好是最不會有症狀的那種。

    切在這裡而不是切整個 `run_formal_e2_full`，是為了讓 validator 能用
    **已開封的 effective-93** 驗證同一條執行路徑，而不必碰 families 36-43。
    final partition 一旦生成就是開封，沒有預演的餘地。

    `formal=True` 時 escalated case 必須有真正的四 agent 仲裁器；
    `decide_case` 會拒絕決定性替身。retry 耗盡一律中止整場 run，
    **不得 drop case** —— drop 會讓分母隨方法而變。

    `llm_mode="skip"` 是零成本的預演：資料生成、routing 與統計形狀全部照跑，
    但 escalated case **不呼叫 provider**。這一列的 F(x) 以傳統決策填位，
    純粹為了讓後續的 sum-to-1 與 cluster 形狀檢查仍有意義；
    **呼叫端必須據 `skipped_escalated_cases > 0` 把 pcmef_full 整條臂排除**，
    否則那一欄會等於 fixed_fusion，看起來像「PC-MEF 沒有比固定融合好」——
    一個從未執行過的方法不該有數字（見 `run_formal_e2_full` 的處理）。

    `case_arbiter` 傳入時取代預設的 `run_pcmef_case`，例如接上
    content-addressed artifact cache。預設 None 代表完全不介入。
    """
    from pcmef.agents.pcmef_agents import RetryExhaustedError, build_case_evidence
    from pcmef.perception.pcmef_orchestrator import decide_case

    def _cache_outcome(arbiter: Any) -> dict[str, Any] | None:
        """把 arbiter 這一 case 的命中結果取出來給 trace。

        用 getattr 而不是 isinstance：case_arbiter 的契約只有「同介面的
        callable」，測試也可以傳一個裸函式進來，那時就沒有 last。
        """
        outcome = getattr(arbiter, "last", None)
        if outcome is None:
            return None
        return {"cache_key": outcome.cache_key, "hit": outcome.hit}

    if llm_mode not in LLM_MODES:
        raise FormalE2Error(
            f"unknown llm_mode {llm_mode!r}; expected one of {sorted(LLM_MODES)}"
        )
    if llm_mode == LLM_MODE_SKIP and formal:
        raise FormalE2Error(
            "llm_mode='skip' cannot be combined with formal=True: skipping the "
            "arbiter is exactly the deterministic substitute that formal mode "
            "exists to refuse. A dry run is not a formal run."
        )

    say = progress or (lambda _m: None)

    counter: CountingAdapter | None = None
    if agent_runner is not None and isinstance(agent_runner.adapter, CountingAdapter):
        counter = agent_runner.adapter

    escalated_cases = 0
    llm_called_cases = 0
    skipped_escalated_cases = 0
    calls_before_first_escalation: int | None = None
    finals = np.empty_like(p_vision)
    traces: list[dict[str, Any]] = []

    say(f"deciding {len(rows)} cases" + (" (dry run: no provider calls)"
                                         if llm_mode == LLM_MODE_SKIP else ""))
    for index, row in enumerate(rows):
        route = str(routes[index])
        escalated = route == "escalated"
        skipping = escalated and llm_mode == LLM_MODE_SKIP
        evidence = None

        if escalated:
            escalated_cases += 1
            if calls_before_first_escalation is None and counter is not None:
                calls_before_first_escalation = counter.calls
        # 這個 case 開始前 call_log 的長度，供旁路 trace 取出「本 case 新增的」。
        calls_before = (
            len(agent_runner.call_log)
            if agent_runner is not None and hasattr(agent_runner, "call_log")
            else 0
        )

        if escalated and not skipping:
            # 讓 arbiter 在寫 producer trace 時說得出是哪一列。純標註 ——
            # 它不進 cache key，因為那會讓內容定址的鑰匙帶上 case 身分。
            if case_arbiter is not None and hasattr(case_arbiter, "case_index"):
                case_arbiter.case_index = index
            # 證據只在真的要送出去時才組裝：non-escalated 與 dry run 連 payload
            # 都不建，因此不可能不小心送出去。
            evidence = build_case_evidence(
                rgb=np.load(row["rgb_path"]),
                tof=np.load(row["tof_path"]),
                p_vision=p_vision[index].tolist(),
                p_tof=p_tof[index].tolist(),
                q_vision=float(q["q_vision"][index]),
                q_tof=float(q["q_tof"][index]),
                duq={k: float(v[index]) for k, v in signals.items()},
            )

        try:
            decision = decide_case(
                # dry run 把這一列當成 fusion 走，只是為了取得一個合法分布填位。
                "fusion" if skipping else route,
                p_vision[index], p_tof[index], rule.fusion_weight,
                evidence=evidence,
                agent_runner=None if skipping else agent_runner,
                formal=formal,
                case_arbiter=None if skipping else case_arbiter,
            )
        except RetryExhaustedError as error:
            # 明確不 drop：整場 run 中止。
            raise FormalE2Error(
                f"ABORT_FORMAL_RUN at row {index} ({row['stress_id']}): {error}"
            ) from None

        finals[index] = decision.final
        if decision.llm_called:
            llm_called_cases += 1

        # --- 旁路觀測。決策**已經完成**，以下任何失敗都不得回頭影響它。---
        if trace_sink is not None:
            try:
                trace_sink(
                    {
                        "row_index": index,
                        "row": row,
                        "p_vision": p_vision[index],
                        "p_tof": p_tof[index],
                        "signals": {k: float(v[index]) for k, v in signals.items()},
                        "q_vision": float(q["q_vision"][index]),
                        "q_tof": float(q["q_tof"][index]),
                        "route": route,
                        "rule": rule,
                        "decision": decision,
                        # 只取這個 case 新增的幾筆 —— call_log 是 runner 級的，
                        # 跨 case 累積；用長度差而不是清空它，避免動到 runner 狀態。
                        "agent_calls": (
                            list(agent_runner.call_log[calls_before:])
                            if agent_runner is not None
                            and hasattr(agent_runner, "call_log")
                            else []
                        ),
                        "dry_run": llm_mode == LLM_MODE_SKIP,
                        # 命中時 call_log 不會有新紀錄 —— 那些角色投影根本
                        # 沒有發生。trace 必須說是命中，而不是顯示成沒有
                        # 呼叫過 agent（NOTE-070）。
                        "cache": (
                            _cache_outcome(case_arbiter)
                            if escalated and not skipping else None
                        ),
                    }
                )
            except Exception as error:  # noqa: BLE001
                # trace 是觀測，不是決策。寫不出來只損失可讀性，
                # 不該讓一次 formal run 失敗（NOTE-064）。
                say(f"  warning: trace for row {index} failed: {type(error).__name__}")

        trace = {"stress_id": row["stress_id"], **decision.to_trace()}
        if skipping:
            skipped_escalated_cases += 1
            # trace 保留這一列**真正的** route，否則 dry run 的紀錄會看起來
            # 像是路由本身變了，而路由完全沒有改變。
            trace["route"] = route
            trace["e"] = 1
            trace["llm_skipped"] = True
            trace["final_is_placeholder"] = True
        traces.append(trace)
        if (index + 1) % 48 == 0:
            say(f"  {index + 1}/{len(rows)} cases")

    if not np.all(np.isfinite(finals)):
        raise FormalE2Error("F(x) contains non-finite entries")
    sums = finals.sum(axis=1)
    if not np.allclose(sums, 1.0, atol=1e-9):
        raise FormalE2Error(
            f"F(x) rows do not sum to 1 (min {sums.min()}, max {sums.max()})"
        )

    return {
        "finals": finals,
        "traces": traces,
        "escalated_cases": escalated_cases,
        "llm_called_cases": llm_called_cases,
        "skipped_escalated_cases": skipped_escalated_cases,
        "llm_mode": llm_mode,
        "calls_before_first_escalation": calls_before_first_escalation,
        "counter": counter,
    }


def _token_usage(counter: Any) -> dict[str, Any]:
    """逐角色與總計的 token 用量，供成本核算。

    `thoughts_tokens` **單獨列**而不是併進 completion。Google 明訂 thinking
    併入 output 計費，卻不放在 `candidatesTokenCount` 裡；只讀 candidates 會
    低估實際計費 output（實測差距達 3.59 倍）。因此另外給一個
    `billable_output_tokens = completion + thoughts` 當計價基準。
    """
    if counter is None:
        return {
            "measured": False,
            "reason": (
                "no CountingAdapter was installed, so nothing observed the "
                "provider responses; usage is unknown rather than zero"
            ),
            "by_role": {}, "totals": {},
        }
    return {
        "measured": True,
        "by_role": {role: dict(bucket)
                    for role, bucket in sorted(counter.usage_by_task.items())},
        "totals": counter.usage_totals(),
        "note": (
            "thoughts_tokens are billed as output but are absent from "
            "candidatesTokenCount; billable_output_tokens = completion + "
            "thoughts is the figure to price against."
        ),
    }


def _cache_identity_from_locks(freeze_dir: str | Path) -> Any:
    """由 llm_runtime.lock 組出 cache 鑰匙裡與 case 無關的那幾項。

    四個角色可以綁不同的 API key，但**必須指向同一個 model 與 revision**。
    cache key 只有一個 `provider_model_id` 欄位；角色之間若真的用了不同模型，
    同一把鑰匙就會同時代表兩套答案，而命中時不會有任何症狀。因此這裡
    fail-closed，不取「第一個」或「多數決」。
    """
    from pcmef.agents.cached_case import CacheIdentity
    from pcmef.core.locks import LockStore

    store = LockStore(freeze_dir)
    store.require("llm_runtime")
    runtime = store.load("llm_runtime")
    bindings = runtime.get("bindings") or {}
    if not bindings:
        raise FormalE2Error(
            "llm_runtime.lock has no bindings; the agent cache key needs the "
            "model identity and must not guess it"
        )

    identities = {
        (str(b.get("model_id", "")), str(b.get("provider_revision", "")))
        for b in bindings.values()
    }
    if len(identities) != 1:
        raise FormalE2Error(
            "the four roles are bound to different model/revision pairs "
            f"({sorted(identities)}); a single agent cache key cannot represent "
            "two different models, and a hit would silently serve the wrong one. "
            "Multiple API keys are fine; multiple models are not."
        )
    model_id, revision = identities.pop()
    return CacheIdentity(
        model_id=model_id,
        provider_revision=revision,
        runtime_config_hash=str(runtime.get("runtime_config_hash", "")),
    )


def run_formal_e2_full(
    base_manifest_dir: str | Path,
    out_dir: str | Path,
    *,
    freeze_dir: str | Path,
    ds_dir: str | Path = DS_V2,
    severity: dict[str, float] | None = None,
    agent_runner: Any = None,
    formal: bool = True,
    llm_mode: str = LLM_MODE_EXECUTE,
    code_version: str = "",
    is_final_formal_e2: bool = False,
    progress: Callable[[str], None] | None = None,
    artifact_cache_root: str | Path | None = None,
    run_id: str | None = None,
    claim: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    """Full PC-MEF 的 Formal E2。

    formal=True 時 escalated case **必須**有真正的四 agent 仲裁器；
    沒有就中止，不接受決定性替身。

    `llm_mode="skip"` 產出一份 **dry-run 報告**：三個 baseline 臂的數字完整
    有效（它們本來就不需要 LLM），而 `pcmef_full` 整條臂連同它的成對統計
    一律不輸出。理由是 dry run 的 F(x) 只是填位值；把它當成 PC-MEF 的結果
    報出去，就會得到「PC-MEF 恰好等於 fixed fusion」這個看似真實、
    實際上只反映「從未執行」的結論。

    `freeze_dir` 為 required keyword-only：見 `load_frozen_decision_stack`
    的說明，這裡不得回復預設值。

    `out_dir` 是 **canonical root**，不是這次 run 的目錄。實際產物落在
    `run_artifact_root()` 決定的子目錄：formal 一個、每次 dry-run 各一個。
    兩者不共用任何路徑（NOTE-078）。

    `claim=True` 時取得 one-shot claim 才開始跑，跑完標成 COMPLETE、
    失敗標成 INTERRUPTED_RESUMABLE。dry run 不取 claim —— 預演可以重跑，
    而 claim 保護的是「正式結果只有一份」（NOTE-079）。
    """
    say = progress or (lambda _m: None)
    dry_run = llm_mode == LLM_MODE_SKIP
    canonical_out = Path(out_dir)
    out = run_artifact_root(canonical_out, dry_run=dry_run, run_id=run_id)
    out.mkdir(parents=True, exist_ok=True)
    say(f"artifact root {out.as_posix()}")

    stack = load_frozen_decision_stack(freeze_dir)
    rule = stack["rule"]
    say(
        f"frozen decision stack: bridge={stack['decision_bridge_version']} "
        f"routing={stack['routing_policy_version']} D>{rule.disagreement_threshold:.6f}"
    )

    # severity 由 frozen 身分還原。傳進來的值是斷言而非設定：不相等就中止，
    # 因為換掉 severity 等於換掉整份 stress set，而報告仍會宣稱它來自
    # frozen 選擇（NOTE-072）。
    severity, severity_provenance = resolve_severity(freeze_dir, severity)
    severity_provenance["lock_hash"] = stack["lock_hashes"].get(SEVERITY_LOCK)
    say(
        f"frozen severity vision={severity['vision']} tof={severity['tof']} "
        f"(from {SEVERITY_LOCK}.lock)"
    )

    # --- one-shot claim（NOTE-079）----------------------------------------
    #
    # 在 prepare_cases 之前取得。prepare_cases 會生成 stress set，而生成
    # 本身就是動作 —— claim 必須在任何動作之前握住，否則兩個同時啟動的
    # 請求會各自生成一份 stress set，然後才發現彼此。
    active_claim: dict[str, Any] | None = None
    if claim and not dry_run:
        from pcmef.experiments.run_claim import RunIdentity, mark_complete
        from pcmef.experiments.run_claim import mark_interrupted, mark_running
        from pcmef.experiments.run_claim import reserve as reserve_claim

        base_manifest = json.loads(
            (Path(base_manifest_dir) / "dataset_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        identity = RunIdentity(
            freeze_dir=str(freeze_dir),
            lock_hashes=stack["lock_hashes"],
            # 只取 manifest 的**身分**欄位，不 hash 整份檔案：後者含檔案
            # 路徑與計數，換一台機器就會變，於是 resume 永遠對不上。
            base_manifest_hash=str(base_manifest.get("scenario_set_hash", "")),
            code_version=code_version,
        )
        active_claim = reserve_claim(out, identity, resume=resume)
        say(f"claim {active_claim['claim_id']} {active_claim['state']}"
            + (f" (resume #{active_claim.get('resume_count')})" if resume else ""))
        mark_running(out, active_claim["claim_id"])

    def _fail_claim(reason: str) -> None:
        """把 claim 標成可 resume。**不刪掉它** —— final partition 已經開封。"""
        if active_claim is None:
            return
        try:
            mark_interrupted(out, active_claim["claim_id"], reason)
        except Exception as error:  # noqa: BLE001
            say(f"warning: could not mark the claim interrupted: {error}")

    try:
        prepared = prepare_cases(
            base_manifest_dir, stack, out, ds_dir=ds_dir, severity=severity,
            code_version=code_version, progress=say,
        )
    except BaseException as error:
        # 這裡的失敗發生在 stress set 生成階段。claim 標成可 resume 而不是
        # 放著不管：放著會停在 RUNNING，而 RUNNING 既擋掉 fresh restart
        # 也擋掉 resume，等於把自己鎖死。
        _fail_claim(f"{type(error).__name__}: {error}")
        raise
    rows = prepared["rows"]
    labels = prepared["labels"]
    p_vision, p_tof = prepared["p_vision"], prepared["p_tof"]
    signals, q, routes = prepared["signals"], prepared["q"], prepared["routes"]

    # --- content-addressed agent cache（在決策路徑上，NOTE-070）------------
    #
    # 與 decision trace 相反：這一段**會**改變是否呼叫 provider，因此它的
    # 錯誤不得被吞掉。cache root 沒給就完全不介入，行為與接線前相同。
    arbiter = None
    cache_summary: dict[str, Any] = {"enabled": False}
    if artifact_cache_root is not None and llm_mode == LLM_MODE_EXECUTE:
        from pcmef.agents.cache import AgentArtifactCache
        from pcmef.agents.cached_case import CachedArbiter, CacheIdentity

        binding = _cache_identity_from_locks(freeze_dir)
        arbiter = CachedArbiter(
            cache=AgentArtifactCache(root=artifact_cache_root),
            identity=binding,
            # 命中時四個角色的投影不會重新發生，因此「是誰在什麼版本下產生
            # 這把鑰匙的答案」只能在未命中的那一次記下來（NOTE-074）。
            run_identity={
                "run_id": Path(out_dir).name,
                "code_version": code_version,
                "freeze_dir": str(freeze_dir),
                "base_manifest": Path(base_manifest_dir).as_posix(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        say(f"agent artifact cache at {artifact_cache_root} "
            f"(model {binding.model_id})")

    # --- decision trace（旁路觀測，NOTE-064）-------------------------------
    from pcmef.experiments.decision_trace import (
        RUN_STATUS_COMPLETE, TRACE_UNAVAILABLE, TraceWriter, build_case_trace,
    )

    fused_all = rule.fusion_weight * p_vision + (1.0 - rule.fusion_weight) * p_tof
    writer = TraceWriter(
        out, run_id=Path(out_dir).name, code_revision=code_version,
        runtime_identity=stack["lock_hashes"], dry_run=dry_run,
        # 分母。少了它，「寫出 381 筆」在畫面上看起來就是完整的。
        expected_cases=len(rows),
    )

    def emit_trace(payload: dict[str, Any]) -> None:
        """把一個 case 的決策落盤。由 executor 在決策完成後呼叫。

        失敗時**先記進 writer 再往外拋**：往外拋讓 executor 印出警告，
        記進 writer 讓 index 的分母對不上時說得出是哪幾列（NOTE-073）。
        兩者都不影響決策 —— 決策在呼叫本函式之前就已經完成。
        """
        index = payload["row_index"]
        case_id = f"case_{index:04d}"
        try:
            _write_one_trace(payload, case_id)
        except BaseException as error:
            writer.record_failure(index, case_id, error)
            raise

    def _write_one_trace(payload: dict[str, Any], case_id: str) -> None:
        index = payload["row_index"]
        row = payload["row"]

        # preview 由**這次 run 記憶體裡**的 RGB 產生，不讓 Web 之後回頭讀
        # dataset：那會多開一條對正式資料集的存取路徑。
        rgb_array = np.load(row["rgb_path"])
        preview_path, shape = writer.write_preview(case_id, rgb_array)
        from pcmef.agents.pcmef_agents import rgb_to_png_bytes
        from pcmef.core.hash import hash_object
        import base64 as _b64

        digest = hash_object(
            _b64.b64encode(rgb_to_png_bytes(rgb_array)).decode("ascii")
        )
        tof_shape = list(np.load(row["tof_path"]).shape)

        writer.write_case(
            build_case_trace(
                row=row, row_index=index, case_id=case_id,
                p_vision=payload["p_vision"], p_tof=payload["p_tof"],
                signals=payload["signals"],
                q_vision=payload["q_vision"], q_tof=payload["q_tof"],
                route=payload["route"], rule=payload["rule"],
                decision=payload["decision"], agent_calls=payload["agent_calls"],
                class_order=CLASS_ORDER,
                # 五條臂全部進 trace。先前只有三條 —— 於是 case 頁看不到
                # G4（路由但不仲裁）與 G5（完整 PC-MEF），而 G5 vs G4 正是
                # 「仲裁帶來多少」這個問題的答案（NOTE-081）。
                #
                # G4 的定義：escalated 用 fused，其餘沿用 F(x)。non-escalated
                # 時 F(x) 依定義等於 p_trad，所以這不是近似，是等式。
                arms={
                    "vision_only": p_vision[index],
                    "tof_only": p_tof[index],
                    "fixed_fusion": fused_all[index],
                    "reliability_routing": (
                        fused_all[index]
                        if payload["route"] == "escalated"
                        else payload["decision"].final
                    ),
                    "pcmef_full": payload["decision"].final,
                },
                rgb_preview={
                    "sha256": digest,
                    "original_shape": shape,
                    "preview_path": preview_path,
                },
                tof_shape=tof_shape,
                dry_run=payload["dry_run"],
                cache=payload.get("cache"),
            )
        )

    try:
        executed = execute_full_pcmef_cases(
            rows, p_vision, p_tof, q, signals, routes, rule,
            agent_runner=agent_runner, formal=formal, llm_mode=llm_mode,
            trace_sink=emit_trace, progress=say, case_arbiter=arbiter,
        )
    except BaseException as error:
        # run 中止時仍要留下已寫的 case，但 index 必須明說它不完整 ——
        # 一份看起來完整的 index 比沒有 index 更糟。
        writer.abort(f"{type(error).__name__}: {error}")
        _fail_claim(f"{type(error).__name__}: {error}")
        raise
    finals = executed["finals"]
    traces = executed["traces"]
    escalated_cases = executed["escalated_cases"]
    llm_called_cases = executed["llm_called_cases"]
    skipped_escalated = executed["skipped_escalated_cases"]
    calls_before_first_escalation = executed["calls_before_first_escalation"]
    counter = executed["counter"]

    fused = rule.fusion_weight * p_vision + (1.0 - rule.fusion_weight) * p_tof
    escalated_mask = np.asarray([str(r) == "escalated" for r in routes])

    # G4：可靠度路由，但**不呼叫 LLM**。三條傳統路徑照常輸出，
    # escalated 改用 fixed fusion。這條臂把「路由本身」與「Multi-Agent
    # 仲裁」的效果乾淨隔開：G4 vs G3 量的是路由，G5 vs G4 量的是仲裁。
    #
    # non-escalated 的列直接沿用 finals —— 那時 F(x) 依定義等於 p_trad
    # （見 decide_case 的 non-escalated 分支），所以這裡不是近似，是等式。
    # 整條 G4 的 provider 呼叫數為 0，因為它一個 case 都沒有新跑。
    reliability_routing = np.where(escalated_mask[:, None], fused, finals)

    arms = {
        "vision_only": p_vision,
        "tof_only": p_tof,
        "fixed_fusion": fused,
        "reliability_routing": reliability_routing,
    }
    if dry_run:
        # pcmef_full 刻意缺席。dry run 的 F(x) 是填位值，報出去會變成
        # 「PC-MEF 恰好等於 fixed fusion」—— 一個從未執行的方法不該有數字。
        #
        # G4 則相反，在 dry run 下**完全有效**：它的定義就是 escalated 用
        # fused，而那正是 dry run 對 escalated 列填的值。
        say(
            f"dry run: {skipped_escalated} escalated case(s) skipped; "
            "the pcmef_full arm is omitted from the report "
            "(reliability_routing remains valid: it never calls an LLM)"
        )
    else:
        arms["pcmef_full"] = finals
    predictions = {name: proba.argmax(1) for name, proba in arms.items()}

    conditions = np.array([r["condition"] for r in rows])
    results = {
        name: {
            "accuracy": _accuracy(labels, predicted),
            "macro_f1": _macro_f1(labels, predicted),
            "n": int(len(labels)),
        }
        for name, predicted in predictions.items()
    }
    per_condition = {
        name: {
            condition: {
                "n": int((conditions == condition).sum()),
                "accuracy": _accuracy(
                    labels[conditions == condition], predicted[conditions == condition]
                ),
                "macro_f1": _macro_f1(
                    labels[conditions == condition], predicted[conditions == condition]
                ),
            }
            for condition in sorted(set(conditions.tolist()))
        }
        for name, predicted in predictions.items()
    }

    worst_condition, worst_condition_at = worst_condition_macro_f1(per_condition)

    say("cluster bootstrap")
    # G5 對 G1-G4 都要有成對比較（實驗計畫 v1.2 §5）。少了 G4，
    # 「LLM 仲裁的增量」就沒有對照 —— G5 vs G3 混著路由與仲裁兩件事。
    baselines = ("vision_only", "tof_only", "fixed_fusion", "reliability_routing")
    if dry_run:
        # 成對統計全部是 pcmef_full vs baseline；沒有 pcmef_full 就沒有比較。
        # cluster 形狀仍然算 —— 驗證 12 列 / cluster 與分層是 dry run 的重點之一。
        statistics: dict[str, Any] = {}
        worst_condition_statistics: dict[str, Any] = {}
    else:
        statistics = {
            f"pcmef_full_vs_{baseline}": {
                metric: value["comparisons"][f"pcmef_full_vs_{baseline}"]
                for metric, value in _cluster_statistics(
                    labels, predictions, rows, stack, baseline
                ).items()
            }
            for baseline in baselines
        }
        # Primary endpoint 的 CI。**必須**用專用 estimator：worst-condition
        # 的 CI 不等於任何單一 condition 的 CI，因為每個 replicate 的
        # argmin condition 可能不同（NOTE-063 / AMD-009）。
        say("worst-condition bootstrap (primary endpoint)")
        worst_runs = {
            baseline: _worst_condition_statistics(
                labels, predictions, rows, stack, baseline
            )
            for baseline in baselines
        }
        worst_condition_statistics = {
            "estimator": next(iter(worst_runs.values()))["version"],
            "comparisons": {
                f"pcmef_full_vs_{baseline}":
                    run["comparisons"][f"pcmef_full_vs_{baseline}"]
                for baseline, run in worst_runs.items()
            },
            # 最弱 condition 在 replicate 之間會換人，而那正是不能用單一
            # condition 的 CI 代替的原因。數出來讓那句話有證據。
            "reference_worst_condition_counts": {
                baseline: run["reference_worst_condition_counts"]
                for baseline, run in worst_runs.items()
            },
        }
    cluster_shape = _cluster_statistics(
        labels, predictions, rows, stack, "vision_only"
    )["accuracy"]

    route_names, route_counts = np.unique(routes, return_counts=True)
    document = {
        "report_id": "formal_e2_dry_run" if dry_run else "formal_e2_full_pcmef",
        # dry run 永遠不是科學結果，即使呼叫端把 is_final_formal_e2 設成 True。
        "scientific_result": bool(is_final_formal_e2) and not dry_run,
        "is_final_formal_e2": is_final_formal_e2 and not dry_run,
        "one_shot": not dry_run,
        "llm_mode": llm_mode,
        "dry_run": dry_run,
        "dry_run_meaning": (
            None if not dry_run else
            "Data generation, routing, reliability and cluster shape were exercised "
            "with zero provider calls. The pcmef_full arm and every paired statistic "
            "are absent by construction: a method that never ran must not have a "
            "number. The other four arms -- including reliability_routing -- are "
            "complete and valid, because none of them ever calls an LLM."
        ),
        "arm_definitions": {
            "vision_only": "G1. argmax p_V.",
            "tof_only": "G2. argmax p_T.",
            "fixed_fusion": (
                f"G3. {rule.fusion_weight} p_V + {1.0 - rule.fusion_weight} p_T "
                "on every row, regardless of route."
            ),
            "reliability_routing": (
                "G4. The same q_V / q_T / D and the same route as G5, but the "
                "escalated route falls back to fixed fusion instead of calling the "
                "arbiter. Zero provider calls by construction. G4 vs G3 isolates "
                "the routing; G5 vs G4 isolates the multi-agent arbitration."
            ),
            "pcmef_full": (
                "G5. Reliability routing plus selective multi-agent arbitration; "
                "escalated rows carry s_A."
            ),
        },
        "skipped_escalated_cases": skipped_escalated,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version,
        "formal_mode": formal,
        "llm_arm_evaluated": (
            False if dry_run else (llm_called_cases > 0 or escalated_cases == 0)
        ),
        "architecture": {
            "routing_policy_version": stack["routing_policy_version"],
            "decision_bridge_version": stack["decision_bridge_version"],
            "path": (
                "RGB/ToF -> frozen CNNs -> frozen temperatures -> D/U/Q + "
                "independent q_v/q_t -> reliability_route -> "
                "{trust_vision: p_V | trust_tof: p_T | fusion: 0.5 p_V + 0.5 p_T | "
                "escalated: observation -> physics + visual_semantic -> arbitration "
                "-> arbitration_support_bridge_v1 -> s_A} -> "
                "selective_escalation_bridge_v1"
            ),
            "deterministic_substitute_used": False,
        },
        "frozen_lock_hashes": stack["lock_hashes"],
        "gate_rule": rule.to_dict(),
        "severity": severity,
        # 具名的出處而不是一句自述。先前這裡是一個固定字串，於是不論
        # `--vision-severity` 傳了什麼，報告都寫著「來自 frozen 選擇」——
        # 一句永遠為真的話證明不了任何事（NOTE-072）。
        "severity_source": severity_provenance,
        # 這次 run 的產物全部在同一個 root 底下。寫進報告，讓「報告、trace、
        # stress、preview 是不是同一次 run 的」變成可查的事實而不是推測。
        "artifact_root": out.as_posix(),
        "canonical_out": canonical_out.as_posix(),
        "run_id": run_id,
        "dataset": {
            "base": Path(base_manifest_dir).as_posix(),
            "family_indices": prepared["base_manifest"].get("family_indices"),
            "total_rows": len(rows),
            "counts": prepared["stress"]["counts"],
        },
        "routing": {
            "counts": {str(n): int(c) for n, c in zip(route_names, route_counts)},
            "escalated_cases": escalated_cases,
            "escalation_rate": float(escalated_cases / len(rows)),
            # 兩個數字刻意分開：一個 case 會呼叫四個角色，retry 還會再加。
            "llm_called_cases": llm_called_cases,
            "actual_provider_calls": None if counter is None else counter.calls,
            "actual_provider_calls_by_role": (
                None if counter is None else dict(counter.calls_by_task)
            ),
            "provider_calls_before_first_escalation": calls_before_first_escalation,
            # 命中不算 provider 呼叫，因此 actual_provider_calls 會低於
            # escalated_cases * 4。少了這一段，那個落差看起來會像漏計。
            "agent_cache": (
                arbiter.summary() if arbiter is not None else cache_summary
            ),
            "note": (
                "escalated_cases counts cases that routed to the arbiter; "
                "actual_provider_calls counts requests actually sent, which is "
                "four per escalated case plus any retry. Non-escalated cases make "
                "zero calls by construction: their evidence payload is never built."
            ),
        },
        # CountingAdapter 一直在逐角色累計用量，但先前沒有任何地方把它寫出來 ——
        # 收集了卻丟掉，等於整場正式執行結束後沒有人知道花了多少（NOTE-071）。
        "token_usage": _token_usage(counter),
        "results": results,
        "per_condition": per_condition,
        # 實驗計畫 v1.2 §5 的 primary robustness metric。
        "worst_condition_macro_f1": worst_condition,
        "worst_condition_at": worst_condition_at,
        "primary_endpoint": {
            "metric": "worst_condition_macro_f1",
            "definition": (
                "min over the four generation conditions of that method's "
                "per-condition macro-F1"
            ),
            "conditions": sorted(set(conditions.tolist())),
            "rationale": (
                "A method that lifts the average while the weakest condition "
                "still collapses has not shown robustness, which is what E2 asks "
                "about. The per-condition table stays in the report because which "
                "condition is weakest is itself a finding."
            ),
            "paired_ci_estimator": (
                "pcmef.stats.bootstrap.cluster_bootstrap_worst_condition_delta -- "
                "same clusters, strata, seed and shared-resample contract as the "
                "overall estimator, but the minimum is taken inside each replicate. "
                "The worst-condition CI is not the CI of any single condition "
                "because the argmin condition varies across replicates (AMD-009)."
            ),
        },
        "worst_condition_statistics": worst_condition_statistics,
        "statistics": statistics,
        "statistics_config": {
            "replicates": stack["bootstrap_replicates"],
            "seed": stack["bootstrap_seed"],
            "resample_unit": stack["resample_unit"],
            "stratification": stack["stratification"],
            "n_clusters": cluster_shape["n_clusters"],
            "rows_per_cluster": cluster_shape["rows_per_cluster"],
            "clusters_per_stratum": cluster_shape["clusters_per_stratum"],
            "shared_clusters_across_methods": True,
        },
        "class_order": list(CLASS_ORDER),
        "traces": traces,
        "claim_boundary": (
            "Synthetic data only. s_A is a normalized Evidence-Support Score, not a "
            "calibrated posterior: no NLL or ECE calibration claim is made for s_A "
            "or F(x). The escalation rate is a finding, not a tuning target."
        ),
    }
    # trace 的 index 在報告落盤**之前**寫，而且只在 run 真的跑完之後。
    # 中途失敗時上面的 except 已經寫過一份 aborted index。
    #
    # 順序是刻意的：完整度必須進報告本身。報告才是科研產物，而一份
    # 「381/384」的 trace 若只出現在 index 裡，讀報告的人不會知道
    # 逐 case 檢視時會缺列（NOTE-073）。
    #
    # finalise() 的失敗**不得**讓這一整場 run 變成失敗。此時統計已經算完，
    # 科學結果在任何意義上都已經成立；寫不出 index 只代表那份旁路紀錄少了
    # 目錄。先前這一行沒有包例外，於是一次磁碟寫入錯誤會讓
    # run_formal_e2_full 拋例外 —— 而呼叫端會把它報成「Formal E2 失敗」，
    # 一個已經完成的一次性實驗因此被宣告成失敗（NOTE-073）。
    trace_index_path: str | None = None
    try:
        trace_index_path = writer.finalise().as_posix()
        trace_status = writer.trace_status(RUN_STATUS_COMPLETE)
    except Exception as error:  # noqa: BLE001
        say(f"warning: the trace index could not be written: "
            f"{type(error).__name__}: {error}")
        trace_status = TRACE_UNAVAILABLE
        document["trace_index_error"] = f"{type(error).__name__}: {error}"
    document["trace"] = {
        "status": trace_status,
        "expected_cases": len(rows),
        "written_cases": len(writer.index_rows),
        "failed_cases": list(writer.failures),
        "index_path": trace_index_path,
        "note": (
            "decision trace 是旁路觀測，寫不出來不影響上面任何一個數字。"
            "status 不是 complete 時，逐 case 檢視會缺列。"
        ),
    }

    # 檔名分開：dry run 不得寫進 formal report 的位置。同名會讓一份預演
    # 在目錄裡與正式結果長得一模一樣，而唯一的差別藏在 JSON 欄位裡。
    filename = "formal_e2_dry_run.json" if dry_run else "formal_e2_report.json"
    (out / filename).write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    # report_path / trace_index_path 只給呼叫端，不進檔案：一份報告記著自己
    # 的絕對路徑，複製到別處之後那一欄就是錯的。
    document["report_path"] = (out / filename).as_posix()
    document["trace_index_path"] = trace_index_path
    document["artifact_root"] = out.as_posix()

    # 報告落盤成功才封閉 claim。順序不可對調：先標 COMPLETE 再寫報告的話，
    # 寫入失敗會留下一個「已完成但沒有結果」的永久封閉狀態（NOTE-079）。
    if active_claim is not None:
        claim_state = mark_complete(
            out, active_claim["claim_id"], (out / filename).as_posix()
        )
        say(f"claim {claim_state['claim_id']} COMPLETE — Formal E2 是一次性的，"
            "此後永久封閉")
        document["run_claim"] = {
            "claim_id": claim_state["claim_id"],
            "state": claim_state["state"],
            "identity_digest": (claim_state.get("identity") or {}).get("digest"),
            "resume_count": claim_state.get("resume_count", 0),
        }
    return document
