# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的所有 freeze 子指令與 experiments.e1/e2、splits、reliability、gate、
#         agents、llm.snapshot 呼叫；讀寫 freeze/<name>.lock.json；
#         各 lock 的 payload_hash 互相交叉引用，最終匯入 formal_config.lock。
# 檔案路徑: pcmef/core/locks.py
# 產生時間: 2026-08-25 22:05 +08:00
# 版本: v0.3.0
# 功能說明: 管理 22 個「凍結點」—— 每個實驗階段做完後把當時的決策與雜湊寫成一個
#           不可再改的檔案。它同時檢查該階段的前置階段是否真的完成，
#           讓「先鎖 split 再校準」這類順序不是靠人記得，而是跳步就會失敗。
# 模組定位: formal freeze 的唯一寫入與驗證通道。它不產生 lock 的內容，
#           只驗證內容齊全、順序正確、事後未被竄改。
# 主要責任:
#   1. LOCK_SPECS 登錄 22 個 lock 的必要 key 與前置 lock
#   2. LockStore.write() 檢查必要欄位、secret、前置條件後寫入，並對相同內容冪等
#   3. LockStore._assert_no_secrets() 擋下疑似 secret 值，只允許 secret_ref
#   4. LockStore.load() 重算 payload_hash 以偵測凍結後的竄改
#   5. LockStore.require() 斷言指定 lock 皆已存在且完整
#   6. LockStore.assert_frozen_before() 驗證兩個 lock 的先後時序
# 維護提醒:
#   - 不得以不同內容覆寫既有 lock；內容變了就必須開新 run_id，而不是重新 freeze。
#   - 不得把 secret 值寫進 lock payload；lock 會被檢視與流通，只能存 secret_ref。
#   - 不得把 created_at 納入 payload_hash；否則相同輸入在不同時間 freeze 會被誤判
#     為內容變更，破壞重跑的冪等性。
#   - 新增 lock 必須同時登錄必要 key 與前置 lock，否則狀態機會出現無人把關的缺口。
#   - v0.3.0 修訂：training_seed_pairs / validation_pool / reliability_final
#     三個 LockSpec 改為對齊實際 thesis execution path（NOTE-050）。
#     修訂契約只有在該 lock **尚未凍結**時才允許；三者當時皆為 pending。
#   - v0.1.0 新增：首版 22 個 lock 與相依圖。
# 驗證方式:
#   - py -3.10 -m pytest tests/unit/test_config_and_locks.py -k "lock"
#   - py -3.10 -m pcmef.cli locks status
# ------------------------------------------------------------

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.core.amendments import AMENDMENT_PROVENANCE_KEYS
from pcmef.core.hash import hash_object

__all__ = [
    "LockError",
    "LockOrderError",
    "LockSpec",
    "LOCK_SPECS",
    "LockStore",
]


class LockError(RuntimeError):
    """lock 缺失、內容不完整、完整性驗證失敗，或試圖以不同內容覆寫。"""


class LockOrderError(LockError):
    """lock 的前置條件未滿足，代表 formal run state machine 被跳步。"""


@dataclass(frozen=True)
class LockSpec:
    """單一 lock 的契約：必要內容 key 與前置 lock。"""

    name: str
    required_keys: tuple[str, ...]
    requires: tuple[str, ...] = ()
    description: str = ""


# ---------------------------------------------------------------------------
# Lock 登錄表
# ---------------------------------------------------------------------------
# 內容依 SRC-SAI Appendix G3「Formal Locks」與 §21 Formal Freeze Manifest；
# 前置關係依 SRC-SAI §23 Formal Run State Machine 與 Appendix H 的四條契約。

LOCK_SPECS: dict[str, LockSpec] = {
    spec.name: spec
    for spec in (
        # -- M0 與 real split ------------------------------------------------
        LockSpec(
            name="real_split_policy",
            required_keys=(
                "creation_phase",
                "split_unit",
                "stratification",
                "group_rule",
                "allocation",
                "minimum_per_class",
                "seed",
                "eligible_set_hash",
                "calibration_set_hash",
                "heldout_real_set_hash",
                "redraw_policy",
            ),
            description=(
                "M0 acceptance 之後、任何 calibration fit 之前建立。"
                "SRC-SAI Appendix H1：缺此 lock 時系統必須拒絕 calibration。"
            ),
        ),
        # -- E1 ---------------------------------------------------------------
        LockSpec(
            name="initial_simulation",
            required_keys=(
                "scene_hash",
                "surrogate_hash",
                "code_version",
                "parameter_ranges",
                # NOTE(NOTE-030): parameter_set_hash 是必要 key，不是附註。
                # scene_hash 只涵蓋 scenario 內容；把 27 個未校準建模常數全部
                # 換掉，scene_hash 可以一個位元都不變。少了這個欄位，lock 就
                # 無法回答「這次凍的是哪一組參數」。
                # 本 lock 仍為 pending（尚無凍結檔），因此契約現在還能改；
                # 一旦凍結就不得再動。
                "parameter_set_hash",
            ),
            requires=("real_split_policy",),
        ),
        LockSpec(
            name="calibrated_simulation",
            required_keys=(
                "calibrated_scene_hash",
                "calibrated_surrogate_hash",
                "calibration_source_hashes",
            ),
            requires=("real_split_policy", "initial_simulation"),
        ),
        LockSpec(
            name="metric_config",
            required_keys=("metric_definitions", "code_hash"),
            requires=("real_split_policy",),
        ),
        LockSpec(
            name="e1_candidates",
            required_keys=(
                "initial_simulation_hash",
                "calibrated_simulation_hash",
                "metric_config_hash",
                "heldout_set_hash",
            ),
            requires=("initial_simulation", "calibrated_simulation", "metric_config"),
        ),
        LockSpec(
            name="e1_evaluation_design",
            required_keys=(
                "base_scenario_ids",
                "offset_strata",
                "optical_transport_seeds",
                "acquisition_seed_matrix",
                "matched_realization_hash",
            ),
            requires=("e1_candidates",),
            description=(
                "Initial 與 Calibrated 必須共用 base scenarios 與 seed matrix"
                "（common random numbers），禁止各跑各的 random realization。"
            ),
        ),
        LockSpec(
            name="e1_scientific_rule",
            required_keys=(
                "normalization_scales",
                "aggregation",
                "improvement_threshold",
                "regression_tolerance",
                "trend_rule",
                "bootstrap_replicates",
                "bootstrap_seed",
                "code_hash",
                # NOTE(NOTE-028): E1 的科學判準依賴 Sigma 是否可作 primary 證據，
                # 而那由 E1-G08 決定。不記下依哪一版 G08 通過，事後就無法
                # 證明這次 run 用的是哪一套判準。
                *AMENDMENT_PROVENANCE_KEYS,
            ),
            requires=("e1_evaluation_design",),
            description=(
                "必須在 Held-out 開啟前鎖定。SRC-SAI Appendix H2："
                "scale 與 pass/degraded 決策規則皆 calibration-only。"
                "另須保存 E1-G08 的 amendment 追溯（NOTE-028）。"
            ),
        ),
        LockSpec(
            name="claim_boundary",
            required_keys=("e1_fidelity_scope", "synthetic_rgb_statement"),
            requires=("e1_candidates",),
        ),
        LockSpec(
            name="e1_outcome",
            required_keys=("outcome", "claim_mode", "result_hashes"),
            requires=("e1_scientific_rule", "claim_boundary"),
            description="PASS/DEGRADED 二選一；DEGRADED 時下游 claim_mode 強制降級。",
        ),
        # -- Post-E1 splits 與 perception -------------------------------------
        LockSpec(
            name="synthetic_split_policy",
            required_keys=(
                "parent_scene_family_rule",
                "family_hashes",
                "split_assignment_hash",
            ),
            requires=("e1_outcome",),
            description="SRC-SAI FR-P0-03：synthetic split 必須在 E1 outcome 之後才建立。",
        ),
        LockSpec(
            name="perception_condition_policy",
            required_keys=(
                "train_condition_allowlist",
                "generic_augmentation_allowlist",
                "formal_stress_denylist",
            ),
            requires=("synthetic_split_policy",),
        ),
        LockSpec(
            name="training_seed_pairs",
            # 2026-09-01（NOTE-050）：舊契約要求「至少 3 個 checkpoint pair」，
            # 那是為了讓 statistics 能對 training seed 做 aggregation 並宣稱
            # seed robustness。實際 thesis execution path 只訓練過**一組**
            # Vision+ToF checkpoint（ds_v2, seed 20260831），gate、reliability、
            # pilot、LLM validation 全部由它產出。要求補訓兩組等於為了滿足
            # 一個從未執行的設計而重跑整條下游，且那兩組不會被任何已凍結的
            # 結果使用。改為凍結唯一實際使用的 pair，並強制寫明
            # training_seed_robustness_claim = false。
            required_keys=(
                "pairs",
                "train_core_hash",
                "train_dev_hash",
                "training_seed_robustness_claim",
            ),
            requires=("perception_condition_policy",),
            description=(
                "凍結實際使用的 checkpoint pair（thesis path 為唯一一組）；"
                "pair mapping 在 formal 前固定，且不得重訓。"
                "少於 3 組時 training_seed_robustness_claim 必須為 false。"
            ),
        ),
        # -- Reliability / gate ------------------------------------------------
        LockSpec(
            name="validation_pool",
            # 2026-09-01（NOTE-050）：移除 group_fold_assignment_hash。
            # 它預設 reliability 以 grouped cross-fit 擬合，而 production 的
            # reliability mapping 沒有 fold（見下方 reliability_final）。
            # 留著它只能靠捏造一組不存在的 fold 指派來填值。
            # 改為記錄 original / effective 兩個 pool 與其間的具名 exclusion。
            required_keys=(
                "checkpoint_pair_ids",
                "original_pool_hash",
                "effective_pool_hash",
                "pooled_row_hash",
                "exclusion",
                "training_pair_id_excluded_from_features",
            ),
            requires=("training_seed_pairs",),
        ),
        LockSpec(
            name="reliability_final",
            # 2026-09-01（NOTE-050）：舊契約寫的是 logistic regression +
            # grouped cross-fit。稽核結果：程式庫裡沒有任何 logistic
            # reliability 模型，也沒有 fold 產生器。實際 production 實作是
            # perception.gate.fit_reliability_model / reliability_scores ——
            # 由 gate-validation clean 分佈定出的 sigmoid margin 乘上
            # cross-modal support，無擬合係數、無 fold。
            # required_keys 因此改為描述那個實作。
            required_keys=(
                "algorithm",
                "algorithm_version",
                "feature_schema",
                "fitted_statistics",
                "parameters",
                "effective_validation_pool_hash",
                "code_hash",
                "forbidden_predictive_features",
            ),
            requires=("validation_pool",),
        ),
        LockSpec(
            name="gate",
            # 2026-09-01（NOTE-049）：改為反映**實際 production gate**。
            # 舊的 alpha/beta/gamma 對應早期的連續 simplex gate，該設計
            # 從未有 production caller；把它們留在 required_keys 會逼出一個
            # 永遠不會被讀取的裁決，並讓 lock 記載一個不曾執行過的方法。
            required_keys=(
                "routing_policy_version",
                "decision_bridge_version",
                "q_vision_threshold",
                "q_tof_threshold",
                "disagreement_threshold",
                "fusion_weight",
                "temperature_vision",
                "temperature_tof",
                "effective_gate_validation_hash",
                "search_grid_hash",
                "objective",
                "tie_break",
                "reliability_config_hash",
            ),
            requires=("reliability_final",),
        ),
        # -- Multi-Agent -------------------------------------------------------
        LockSpec(
            name="agent_schema",
            required_keys=(
                "arbitration_schema_sha256",
                "class_order",
                "support_bridge_version",
            ),
            requires=("synthetic_split_policy",),
        ),
        LockSpec(
            name="inference_firewall",
            required_keys=(
                "payload_schema_hash",
                "opaque_id_map_hash",
                "forbidden_metadata_rules",
                "provider_payload_snapshot_policy",
            ),
            requires=("agent_schema",),
        ),
        LockSpec(
            name="llm_runtime",
            required_keys=(
                "bindings",
                "prompt_hashes",
                "schema_hashes",
                "runtime_config_hash",
                "representation_mode",
                "capability_probe_artifact_hashes",
                "secret_refs",
            ),
            requires=("agent_schema",),
            description="Formal runner 只讀此 lock，禁止查詢 live SQLite binding。",
        ),
        # -- E2 ----------------------------------------------------------------
        LockSpec(
            name="conflict_operational",
            required_keys=("delta", "operational_conflict_rule", "attainment_reporting"),
            requires=("gate",),
        ),
        LockSpec(
            name="e2_sample_size",
            required_keys=(
                "pilot_set_hash",
                "final_n_per_class",
                "class_balance",
                "severity_allocation",
                "decision_rule_version",
                "sizing_reference",
            ),
            requires=("gate", "conflict_operational"),
            description="SRC-SAI Appendix H3：sizing 不得使用 G5-G4 或 proposed-method 相對表現。",
        ),
        LockSpec(
            name="statistics_config",
            required_keys=(
                "bootstrap_seed",
                "bootstrap_replicates",
                "metric_definitions",
                "training_seed_aggregation",
                "ece_semantics",
            ),
            requires=("e2_sample_size",),
        ),
        LockSpec(
            name="formal_config",
            required_keys=(
                "resolved_config_sha256",
                "code_revision",
                "split_hashes",
                "e1_candidates_lock_hash",
                "e1_evaluation_design_hash",
                "inference_firewall_hash",
                "e2_sample_size_lock_hash",
                "scenario_set_hash",
                "model_checkpoint_hashes",
                "reliability_hash",
                "validation_pool_hash",
                "gate",
                "conflict_operational_hash",
                "perception_condition_policy_hash",
                "claim_boundary_hash",
                "agent",
                "statistics_config_hash",
                # NOTE(NOTE-028): formal run 的最終憑證必須能回答
                # 「這次 run 依哪一版判準通過」。amendment 進了版控不代表
                # run 知道自己用了哪一版。
                *AMENDMENT_PROVENANCE_KEYS,
            ),
            requires=(
                "e2_sample_size",
                "statistics_config",
                "llm_runtime",
                "inference_firewall",
            ),
            description=(
                "最後一道 freeze；鎖定後任何 config 變動都必須開新 run_id。"
                "另須保存所有已生效 protocol amendment 的追溯（NOTE-028）。"
            ),
        ),
    )
}


_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret(?!_ref)|password|token|credential|salt)", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# LockStore
# ---------------------------------------------------------------------------


class LockStore:
    """freeze/ 目錄的讀寫閘門。"""

    def __init__(self, freeze_dir: str | Path) -> None:
        self.freeze_dir = Path(freeze_dir)

    # -- 路徑 -------------------------------------------------------------

    def path_for(self, name: str) -> Path:
        if name not in LOCK_SPECS:
            raise LockError(
                f"unknown lock {name!r}; registered locks: {sorted(LOCK_SPECS)}"
            )
        return self.freeze_dir / f"{name}.lock.json"

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    # -- 檢查 -------------------------------------------------------------

    @staticmethod
    def _assert_no_secrets(payload: Any, location: str) -> None:
        """拒絕把疑似 secret 的內容寫進 lock。

        lock 檔是 formal provenance 的一部分，會被檢視與流通
        （SRC-SAI NFR-08、LLM-SEC-01）。允許 secret_ref 這種「指向」欄位，
        擋掉 api_key / token / salt 這種「值本身」欄位。
        """
        if isinstance(payload, dict):
            for key, value in payload.items():
                if _SECRET_KEY_PATTERN.search(str(key)):
                    raise LockError(
                        f"lock payload field {location}.{key} looks like a secret "
                        "value; locks may store secret_ref only, never the value"
                    )
                LockStore._assert_no_secrets(value, f"{location}.{key}")
        elif isinstance(payload, list):
            for index, item in enumerate(payload):
                LockStore._assert_no_secrets(item, f"{location}[{index}]")

    def _assert_prerequisites(self, name: str) -> None:
        spec = LOCK_SPECS[name]
        missing = [dep for dep in spec.requires if not self.exists(dep)]
        if missing:
            raise LockOrderError(
                f"cannot freeze {name!r} before {missing}; the formal run state "
                "machine forbids skipping steps"
            )

    # -- 寫入 -------------------------------------------------------------

    def write(self, name: str, payload: dict[str, Any]) -> Path:
        """寫入 lock。已存在且內容相同時視為冪等，內容不同則拒絕。

        冪等而非一律拒絕的理由：freeze 指令可能因為下游步驟失敗而被重跑，
        重跑同樣的輸入應該安全。但只要 payload 有任何差異就必須拒絕 ——
        那代表輸入已經改變，繼續沿用舊 lock 會讓 hash 與實際內容脫節。
        """
        spec = LOCK_SPECS.get(name)
        if spec is None:
            raise LockError(
                f"unknown lock {name!r}; registered locks: {sorted(LOCK_SPECS)}"
            )
        if not isinstance(payload, dict):
            raise LockError(f"lock payload for {name!r} must be a dict")

        missing_keys = [key for key in spec.required_keys if key not in payload]
        if missing_keys:
            raise LockError(
                f"lock {name!r} is missing required keys {missing_keys}; "
                f"{spec.description or 'see SRC-SAI Appendix G3'}"
            )
        self._assert_no_secrets(payload, name)
        self._assert_prerequisites(name)

        payload_hash = hash_object(payload)
        target = self.path_for(name)

        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing.get("payload_hash") == payload_hash:
                return target
            raise LockError(
                f"lock {name!r} already exists with different content "
                f"(existing {existing.get('payload_hash')}, new {payload_hash}); "
                "locks are immutable — start a new run instead of re-freezing"
            )

        record = {
            "lock_type": name,
            "version": 1,
            # created_at 刻意不進 payload_hash：同樣的輸入在不同時間 freeze
            # 應該得到相同 hash，否則 idempotent 重跑會被誤判為內容變更。
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload_hash": payload_hash,
            "payload": payload,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return target

    # -- 讀取 -------------------------------------------------------------

    def load(self, name: str) -> dict[str, Any]:
        """載入 lock 並驗證完整性。hash 不符即拒絕，不做修復。"""
        target = self.path_for(name)
        if not target.exists():
            raise LockError(f"required lock {name!r} not found at {target}")
        record = json.loads(target.read_text(encoding="utf-8"))
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise LockError(f"lock {name!r} at {target} has no payload object")
        actual = hash_object(payload)
        if actual != record.get("payload_hash"):
            raise LockError(
                f"lock {name!r} failed integrity check: stored hash "
                f"{record.get('payload_hash')} != computed {actual}. "
                "The lock file has been modified after freezing."
            )
        return payload

    def load_hash(self, name: str) -> str:
        """取得 lock 的 payload_hash，供上層 lock 交叉引用。"""
        self.load(name)
        record = json.loads(self.path_for(name).read_text(encoding="utf-8"))
        return str(record["payload_hash"])

    def created_at(self, name: str) -> datetime:
        """取得 lock 建立時間，供時序守衛使用。"""
        self.load(name)
        record = json.loads(self.path_for(name).read_text(encoding="utf-8"))
        return datetime.fromisoformat(str(record["created_at"]))

    def require(self, *names: str) -> None:
        """斷言指定 lock 皆已存在且通過完整性檢查。"""
        missing = [name for name in names if not self.exists(name)]
        if missing:
            raise LockError(f"required lock(s) not found: {missing}")
        for name in names:
            self.load(name)

    def assert_frozen_before(self, earlier: str, later: str) -> None:
        """斷言 earlier lock 的建立時間早於 later lock。

        用於 SRC-SAI Appendix B 的兩條時序規則：
        real_split_policy.lock 的時間必須早於第一次 calibration fit；
        e2_sample_size.lock 必須早於 formal scenario generation。
        """
        earlier_time = self.created_at(earlier)
        later_time = self.created_at(later)
        if earlier_time >= later_time:
            raise LockOrderError(
                f"lock {earlier!r} ({earlier_time.isoformat()}) must be frozen before "
                f"{later!r} ({later_time.isoformat()}); the ordering guarantees that "
                "the earlier decision was not made with knowledge of the later result"
            )
