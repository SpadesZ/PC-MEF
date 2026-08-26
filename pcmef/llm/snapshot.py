# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 llm snapshot / llm freeze-runtime 與 pcmef.admin.services
#         的唯讀快照卡片呼叫；讀 pcmef.llm.registry 的 draft binding、
#         configs/ 的 runtime 設定與 schemas/*.schema.json；
#         寫出 outputs/llm/runtime_snapshot_<hash>.json，
#         並在前提齊備時經 core.locks 寫入 freeze/llm_runtime.lock.json。
# 檔案路徑: pcmef/llm/snapshot.py
# 產生時間: 2026-08-27 01:20 +08:00
# 版本: v0.1.0
# 功能說明: 把「現在草稿上綁的是哪些模型」解析成一份可比對的清單並算出雜湊。
#           雜湊算得出來不代表可以凍結 —— 還缺教授裁決或缺 prompt 時，
#           它會如實列出缺什麼，並拒絕寫進 lock。
# 模組定位: draft registry 與 formal identity 之間的唯一轉換點，也是
#           Appendix J1 binding resolution invariant 的實作處。
#           它「不是」設定編輯器，也永遠不從 lock 反寫回 DB。
# 主要責任:
#   1. build_runtime_snapshot() 解析 draft binding 成 RuntimeSnapshot
#   2. RuntimeSnapshot.blocking_reasons 逐條列出還不能凍結的原因
#   3. RuntimeSnapshot.candidate_hash() 對未決項以顯式標記入雜湊，不當作不存在
#   4. RuntimeSnapshot.to_lock_payload() 產生 Schema 草案 4 的七個欄位
#   5. freeze_runtime_snapshot() 在無 blocking 時才寫 lock
#   6. resolve_formal_agent_binding() 只讀 lock，禁止任何 DB 查詢（J1）
#   7. STATE_INVALIDATION 依 §47 宣告每種變更會作廢哪些下游 lock
# 維護提醒:
#   - 不得為了讓快照可以凍結而替 !required 補值。representation_mode 與
#     retry.max_attempts 未核定就是不能凍結，這正是 NOTE-005 要擋的事。
#   - 不得把 secret 值寫進 lock；secret_refs 只存參考（Schema 草案 4 明註）。
#   - 不得讓 resolve_formal_agent_binding() 讀 SQLite。Appendix J1 把
#     db.query("SELECT * FROM llm_task_bindings ...") 明列為 formal 禁止行為。
#   - 不得讓 stub_offline 這類非 formal provider 通過凍結檢查。
#   - 不得在 lock 已存在時以新內容覆寫；core.locks 會拒絕，這裡也不該繞過。
#   - v0.1.0 新增：首版 snapshot 與 state invalidation，決策見 NOTE-020。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_snapshot.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pcmef.agents.provider import FORMAL_ELIGIBLE_PROVIDERS
from pcmef.core.config import Required, ResolvedConfig
from pcmef.core.constants import CLASS_ORDER
from pcmef.core.hash import hash_file, hash_object
from pcmef.core.locks import LockStore
from pcmef.llm.capabilities import FORMAL_TASK_CODES, TASK_REGISTRY, required_capabilities
from pcmef.secrets.vault import PERSISTENT_SCHEMES, SecretRef

__all__ = [
    "SnapshotError",
    "RuntimeSnapshot",
    "build_runtime_snapshot",
    "freeze_runtime_snapshot",
    "resolve_formal_agent_binding",
    "STATE_INVALIDATION",
    "DEFAULT_SNAPSHOT_DIR",
    "SCHEMA_KEYS",
    "PROMPT_KEYS",
]

DEFAULT_SNAPSHOT_DIR = Path("outputs/llm")

#: Schema 草案 4 的 schema_hashes 欄位鍵，對應 schemas/ 下的三份檔案。
SCHEMA_KEYS: dict[str, str] = {
    "observation": "observation_brief_v1.schema.json",
    "proposal": "specialist_proposal_v1.schema.json",
    "arbitration": "arbitration_output_v1.schema.json",
}

#: Schema 草案 4 的 prompt_hashes 欄位鍵，對應 configs/agents/prompts/ 下的檔案。
PROMPT_KEYS: dict[str, str] = {
    "observation": "observation_agent.md",
    "physics": "physics_agent.md",
    "visual": "visual_semantic_agent.md",
    "arbitration": "arbitration_agent.md",
}

#: §47 Runtime Resolution、Freeze 與 State Invalidation。
#: key 是變更事件，value 是必須一併作廢的 lock。
STATE_INVALIDATION: dict[str, tuple[str, ...]] = {
    "binding_changed_before_model_gate_validated": (),
    "binding_changed_after_model_gate_validated": (
        "gate",
        "e2_sample_size",
        "statistics_config",
        "formal_config",
    ),
    "prompt_or_schema_changed_after_model_gate_validated": (
        "gate",
        "e2_sample_size",
        "statistics_config",
        "formal_config",
    ),
    "representation_mode_changed_after_model_gate_validated": (
        "gate",
        "e2_sample_size",
        "statistics_config",
        "formal_config",
    ),
    # §47：只換 credential secret 而 identity 不變時，不動任何 scientific lock。
    "credential_only_rotation": (),
    # §47：FORMAL_CONFIG_FROZEN 之後改 live draft，現有 run 不受影響。
    "draft_changed_after_formal_config_frozen": (),
}


class SnapshotError(RuntimeError):
    """快照無法解析，或在仍有 blocking 前提時試圖凍結。"""


def _peek(config: ResolvedConfig, dotted_key: str) -> tuple[Any, str]:
    """讀取設定值但不觸發 formal-blocking 例外。

    回傳 (值, 狀態)，狀態為 ok / required / missing。
    刻意做成「看得到但拿不到」：快照必須能報告缺了哪一項裁決，
    但**不得**因此取得任何替代值 —— 那就是 NOTE-005 禁止的補值。
    """
    node: Any = config.data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, "missing"
        node = node[part]
    if isinstance(node, Required):
        return None, "required"
    return node, "ok"


@dataclass(frozen=True)
class RuntimeSnapshot:
    """draft binding 解析後的 lock candidate。"""

    bindings: dict[str, dict[str, str]]
    schema_hashes: dict[str, str]
    prompt_hashes: dict[str, str]
    runtime_config: dict[str, Any]
    representation_mode: str | None
    capability_probe_artifact_hashes: tuple[str, ...]
    secret_refs: dict[str, str]
    blocking_reasons: tuple[str, ...] = ()
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # -- 雜湊 -------------------------------------------------------------

    @property
    def runtime_config_hash(self) -> str:
        return hash_object(self.runtime_config)

    def candidate_hash(self) -> str:
        """lock candidate 的雜湊。

        未決項以顯式標記進入雜湊而非被略過：一份還缺裁決的快照，
        不應該和補齊之後的快照得到相同雜湊。這與 ResolvedConfig.config_hash()
        對 Required sentinel 的處理一致。
        """
        return hash_object(
            {
                "bindings": self.bindings,
                "schema_hashes": self.schema_hashes,
                "prompt_hashes": self.prompt_hashes,
                "runtime_config_hash": self.runtime_config_hash,
                "representation_mode": self.representation_mode
                or {"__required__": "agents.representation_mode"},
                "capability_probe_artifact_hashes": list(
                    self.capability_probe_artifact_hashes
                ),
                "secret_refs": self.secret_refs,
            }
        )

    @property
    def freezable(self) -> bool:
        return not self.blocking_reasons

    # -- 輸出 -------------------------------------------------------------

    def to_lock_payload(self) -> dict[str, Any]:
        """Schema 草案 4 的七個欄位。仍有 blocking 時拒絕產生。"""
        if not self.freezable:
            raise SnapshotError(
                "refusing to build an llm_runtime lock payload while these "
                "prerequisites are unmet:\n  - "
                + "\n  - ".join(self.blocking_reasons)
            )
        return {
            "bindings": self.bindings,
            "prompt_hashes": self.prompt_hashes,
            "schema_hashes": self.schema_hashes,
            "runtime_config_hash": self.runtime_config_hash,
            "representation_mode": self.representation_mode,
            "capability_probe_artifact_hashes": list(
                self.capability_probe_artifact_hashes
            ),
            "secret_refs": self.secret_refs,
        }

    def to_artifact(self) -> dict[str, Any]:
        """可落盤的候選快照。即使不可凍結也產得出來，這正是它的用途。"""
        return {
            "artifact_type": "llm_runtime_snapshot_candidate",
            "candidate_hash": self.candidate_hash(),
            "created_at": self.created_at,
            "freezable": self.freezable,
            "blocking_reasons": list(self.blocking_reasons),
            "bindings": self.bindings,
            "prompt_hashes": self.prompt_hashes,
            "schema_hashes": self.schema_hashes,
            "runtime_config": self.runtime_config,
            "runtime_config_hash": self.runtime_config_hash,
            "representation_mode": self.representation_mode,
            "capability_probe_artifact_hashes": list(
                self.capability_probe_artifact_hashes
            ),
            "secret_refs": self.secret_refs,
        }

    def write(self, out_dir: str | Path = DEFAULT_SNAPSHOT_DIR) -> Path:
        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"runtime_snapshot_{self.candidate_hash()[:16]}.json"
        path.write_text(
            json.dumps(self.to_artifact(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path


# ---------------------------------------------------------------------------
# 建立
# ---------------------------------------------------------------------------


def build_runtime_snapshot(
    registry,
    config: ResolvedConfig,
    schemas_dir: str | Path = "schemas",
    prompts_dir: str | Path = "configs/agents/prompts",
) -> RuntimeSnapshot:
    """把 draft binding 解析成 lock candidate，並列出所有 blocking 前提。"""
    blocking: list[str] = []
    schemas_root = Path(schemas_dir)
    prompts_root = Path(prompts_dir)

    bindings: dict[str, dict[str, str]] = {}
    secret_refs: dict[str, str] = {}
    probe_hashes: set[str] = set()

    draft = {b.task_code: b for b in registry.list_bindings()}
    for task_code in FORMAL_TASK_CODES:
        binding = draft.get(task_code)
        if binding is None:
            blocking.append(f"task {task_code} has no draft binding")
            continue
        model = registry.get_model_profile(binding.model_profile_id)
        connection = registry.get_connection(model.connection_id)

        if connection.provider not in FORMAL_ELIGIBLE_PROVIDERS:
            blocking.append(
                f"task {task_code} is bound to provider {connection.provider!r}, "
                "which is not eligible for a formal run"
            )
        if not connection.bindable:
            blocking.append(
                f"task {task_code} is bound through connection {connection.name!r} "
                f"whose status is {connection.status} / enabled={connection.enabled}"
            )
        missing = [
            c.value
            for c in required_capabilities(task_code)
            if c not in model.verified_capabilities
        ]
        if missing:
            blocking.append(
                f"task {task_code} model {model.model_id!r} lacks probe-verified "
                f"capability {missing}"
            )
        if not model.provider_revision:
            blocking.append(
                f"task {task_code} model {model.model_id!r} has no provider_revision; "
                "a formal binding must pin an identifiable model revision"
            )

        reference = SecretRef.parse(connection.secret_ref)
        if reference.scheme not in PERSISTENT_SCHEMES:
            blocking.append(
                f"connection {connection.name!r} uses a {reference.scheme}: secret "
                "ref, which cannot be resolved by a formal run"
            )
        secret_refs[connection.connection_id] = str(reference)

        bindings[task_code] = {
            "connection_id": connection.connection_id,
            "provider": connection.provider,
            "model_id": model.model_id,
            "provider_revision": model.provider_revision,
        }
        probe_hashes.update(
            _probe_artifact_hashes(registry, binding.model_profile_id, task_code)
        )

    schema_hashes, schema_blocking = _hash_files(schemas_root, SCHEMA_KEYS, "schema")
    prompt_hashes, prompt_blocking = _hash_files(prompts_root, PROMPT_KEYS, "prompt")
    blocking.extend(schema_blocking)
    blocking.extend(prompt_blocking)

    representation_mode, mode_state = _peek(config, "agents.representation_mode")
    if mode_state != "ok":
        blocking.append(
            "agents.representation_mode awaits advisor approval "
            "(SRC-SAI §18); it must be frozen before llm_runtime.lock"
        )
    retry_attempts, retry_state = _peek(config, "agents.retry.max_attempts")
    if retry_state != "ok":
        blocking.append(
            "agents.retry.max_attempts awaits advisor approval (SRC-SAI §28 / FR-013)"
        )

    runtime_config: dict[str, Any] = {
        "class_order": list(CLASS_ORDER),
        "representation_mode": (
            representation_mode
            if mode_state == "ok"
            else {"__required__": "agents.representation_mode"}
        ),
        "retry_max_attempts": (
            retry_attempts
            if retry_state == "ok"
            else {"__required__": "agents.retry.max_attempts"}
        ),
        "timeout_sec": {
            connection_id: registry.get_connection(connection_id).timeout_sec
            for connection_id in sorted(secret_refs)
        },
        "task_output_schemas": {
            task_code: TASK_REGISTRY[task_code].output_schema
            for task_code in FORMAL_TASK_CODES
        },
    }

    return RuntimeSnapshot(
        bindings=bindings,
        schema_hashes=schema_hashes,
        prompt_hashes=prompt_hashes,
        runtime_config=runtime_config,
        representation_mode=representation_mode if mode_state == "ok" else None,
        capability_probe_artifact_hashes=tuple(sorted(probe_hashes)),
        secret_refs=secret_refs,
        blocking_reasons=tuple(blocking),
    )


def _hash_files(
    root: Path, keys: dict[str, str], kind: str
) -> tuple[dict[str, str], list[str]]:
    hashes: dict[str, str] = {}
    blocking: list[str] = []
    for key, filename in keys.items():
        path = root / filename
        if not path.exists():
            blocking.append(f"{kind} file {path.as_posix()} does not exist")
            continue
        hashes[key] = hash_file(path)
    return hashes, blocking


def _probe_artifact_hashes(registry, model_profile_id: str, task_code: str) -> set[str]:
    """取每個必要能力最近一次成功 probe 的 artifact hash。

    只收成功的：一份失敗的 probe 證據不該出現在宣稱「這些能力已驗證」的
    lock 欄位裡。
    """
    required = {c.value for c in required_capabilities(task_code)}
    latest: dict[str, tuple[bool, str]] = {}
    for row in registry.verification_logs(model_profile_id):
        if row["capability"] in required:
            latest[row["capability"]] = (bool(row["success"]), row["artifact_hash"])
    return {digest for success, digest in latest.values() if success and digest}


# ---------------------------------------------------------------------------
# 凍結與解析
# ---------------------------------------------------------------------------


def freeze_runtime_snapshot(store: LockStore, snapshot: RuntimeSnapshot) -> Path:
    """把快照寫入 freeze/llm_runtime.lock.json。

    仍有 blocking 時 to_lock_payload() 會先拋例外，因此這裡不需要再擋一次；
    LockStore 另會檢查前置 lock（agent_schema）與不可覆寫規則。
    """
    return store.write("llm_runtime", snapshot.to_lock_payload())


def resolve_formal_agent_binding(store: LockStore, task_code: str) -> dict[str, str]:
    """Appendix J1：formal 模式下解析 binding 的唯一合法路徑。

    只讀 llm_runtime.lock，並在讀取時驗證 payload hash。本函式刻意
    **不接受** registry 參數 —— 沒有可用的 DB 控制代碼，就沒有人能
    在這裡「順便查一下 live binding」。
    """
    payload = store.load("llm_runtime")
    try:
        binding = payload["bindings"][task_code]
    except KeyError:
        raise SnapshotError(
            f"llm_runtime.lock carries no binding for {task_code!r}; "
            f"locked tasks are {sorted(payload.get('bindings', {}))}"
        ) from None
    secret_ref = payload.get("secret_refs", {}).get(binding["connection_id"])
    if not secret_ref:
        raise SnapshotError(
            f"llm_runtime.lock has no secret_ref for connection "
            f"{binding['connection_id']!r}"
        )
    return {**binding, "secret_ref": secret_ref}
