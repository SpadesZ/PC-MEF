# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.services、pcmef.llm.verification、pcmef.llm.snapshot 與
#         cli 的 llm 子指令呼叫；讀寫 registry/llm_admin.db（SQLite，不進版控）；
#         connection 的 secret 只以 secret_ref 形式存放，實際值由 secrets.vault 解析。
# 檔案路徑: pcmef/llm/registry.py
# 產生時間: 2026-08-26 22:25 +08:00
# 版本: v0.1.0
# 功能說明: 保存「有哪些 provider 連線、每個連線底下有哪些模型、哪個模型綁到哪個
#           Agent 角色」這份可變的操作紀錄，並記下每次能力驗證與每次改綁的歷程。
# 模組定位: §49 三層責任中的 operational registry / index 那一層。
#           它對 formal scientific identity **不是** source of truth ——
#           formal 只認 llm_runtime.lock，本 DB 被手改也不得穿透既有 run。
# 主要責任:
#   1. MIGRATIONS 以 user_version 逐步建表，重複執行為冪等
#   2. add_connection() / update_connection() 寫入前以 SecretRef 擋下明文 key
#   3. upsert_models() 保存 provider metadata 宣告的能力（declared，非 verified）
#   4. record_verification() 在同一交易內寫 probe log 並更新 verified 能力
#   5. verified_capabilities() 以「每個能力最近一次 probe」判定，過期結果不續命
#   6. set_binding() 檢查能力相容後改綁，並寫入 before/after hash 的 audit log
#   7. delete_model_profile() / delete_connection() 在仍被綁定時拒絕並列出相依 task
#   8. record_cache_entry() / cache_entries() 維護 content-addressed 產物的費用索引
# 維護提醒:
#   - 不得在任何欄位存放明文 API key。§49 明訂 SQLite 不可保存明文金鑰，
#     寫入路徑一律先過 SecretRef.parse()。
#   - 不得在本 DB 保存 transient / RGB / raw LLM artifact；那些屬 flat artifact 層，
#     混進來會讓 operational index 變成一份沒有 hash 保護的證據副本。
#   - 不得讓 formal runner 讀本 DB 的 binding。§47 與 Appendix J1 規定 formal
#     只解析 llm_runtime.lock；留一條 live 查詢就等於留一條繞過 freeze 的路。
#   - 不得把 verified_caps_json 當成可以手動填的欄位；它只由 record_verification()
#     依 probe 結果推導，手填等於宣稱驗證過但沒有 log 佐證。
#   - 不得因為單一 capability probe 失敗就把整個 connection 標成 degraded；
#     模型缺某項能力與連線故障是兩回事，混為一談會讓 Bind 的拒絕理由指錯方向。
#   - 不得允許 session: 形式的 secret_ref 寫進 llm_connections；那種 ref
#     重啟後必然無法解析。
#   - v0.1.0 新增：首版六張表與 migration，決策見 NOTE-018。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_registry.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from pcmef.agents.provider import Capability
from pcmef.core.hash import hash_object
from pcmef.secrets.vault import PERSISTENT_SCHEMES, SecretRef

__all__ = [
    "RegistryError",
    "DependencyError",
    "ConnectionStatus",
    "Lifecycle",
    "ConnectionRow",
    "ModelProfileRow",
    "BindingRow",
    "LLMRegistry",
    "DEFAULT_REGISTRY_PATH",
    "SCHEMA_VERSION",
]

DEFAULT_REGISTRY_PATH = Path("registry/llm_admin.db")


class RegistryError(RuntimeError):
    """registry 操作違反契約：欄位非法、對象不存在、或能力不相容。"""


class DependencyError(RegistryError):
    """刪除的對象仍被 active task binding 使用。

    §46 Delete dependency：必須回 HTTP 409 / CLI non-zero，並列出 dependent tasks。
    因此本例外帶著 tasks 清單，呼叫端不需要再查一次。
    """

    def __init__(self, message: str, tasks: tuple[str, ...]) -> None:
        super().__init__(message)
        self.tasks = tasks


class ConnectionStatus:
    """§43 的 status 列舉。以常數而非 Enum，方便直接寫入 SQLite。

    這一欄描述**連線健康度**（憑證與端點通不通），與描述**設定進度**的
    lifecycle 是兩件事，刻意分成兩欄。合成一欄會讓「已鎖定但今天端點掛了」
    無法表達。
    """

    PENDING = "pending"
    ACTIVE = "active"
    DEGRADED = "degraded"
    ERROR = "error"
    DISABLED = "disabled"

    ALL: tuple[str, ...] = (PENDING, ACTIVE, DEGRADED, ERROR, DISABLED)


class Lifecycle:
    """設定進度的四態，沿用 roothinks LAVA setup 的流程（NOTE-023）。

    draft -> fetched -> connected -> locked，每一步解鎖下一個動作：
    抓到模型清單並選定一個才能 Test，Test 過才能 Connect（鎖定），
    鎖定後 vendor/key/model 全部凍結，且**只有鎖定的線路能被綁到 task**。
    """

    DRAFT = "draft"
    FETCHED = "fetched"
    CONNECTED = "connected"
    LOCKED = "locked"

    ALL: tuple[str, ...] = (DRAFT, FETCHED, CONNECTED, LOCKED)

    #: 每個狀態允許的下一步。用資料表述而非散在 if 裡，
    #: 讓「哪些轉移合法」可以被單一測試窮舉。
    TRANSITIONS: dict[str, tuple[str, ...]] = {
        DRAFT: (FETCHED,),
        FETCHED: (FETCHED, CONNECTED),
        CONNECTED: (FETCHED, CONNECTED, LOCKED),
        LOCKED: (CONNECTED,),  # 解鎖退回 connected，不直接跳回 draft
    }


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2

_MIGRATION_1 = """
CREATE TABLE llm_connections (
    connection_id    TEXT PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    provider         TEXT NOT NULL,
    base_url         TEXT NOT NULL DEFAULT '',
    secret_ref       TEXT NOT NULL,
    timeout_sec      INTEGER NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'pending',
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    last_verified_at TEXT
);

CREATE TABLE llm_models (
    model_profile_id  TEXT PRIMARY KEY,
    connection_id     TEXT NOT NULL REFERENCES llm_connections(connection_id),
    model_id          TEXT NOT NULL,
    display_name      TEXT NOT NULL DEFAULT '',
    declared_caps_json TEXT NOT NULL DEFAULT '[]',
    verified_caps_json TEXT NOT NULL DEFAULT '[]',
    provider_revision TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    fetched_at        TEXT NOT NULL,
    UNIQUE (connection_id, model_id)
);

CREATE TABLE llm_task_bindings (
    task_code         TEXT PRIMARY KEY,
    model_profile_id  TEXT NOT NULL REFERENCES llm_models(model_profile_id),
    binding_version   INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL DEFAULT 'draft',
    updated_at        TEXT NOT NULL,
    updated_by        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE llm_verification_logs (
    verify_id        TEXT PRIMARY KEY,
    model_profile_id TEXT NOT NULL REFERENCES llm_models(model_profile_id),
    capability       TEXT NOT NULL,
    success          INTEGER NOT NULL,
    latency_ms       INTEGER NOT NULL DEFAULT 0,
    error_sanitized  TEXT NOT NULL DEFAULT '',
    artifact_hash    TEXT NOT NULL DEFAULT '',
    verified_at      TEXT NOT NULL
);

CREATE TABLE llm_binding_audit_logs (
    audit_id    TEXT PRIMARY KEY,
    task_code   TEXT NOT NULL,
    before_hash TEXT NOT NULL DEFAULT '',
    after_hash  TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    changed_at  TEXT NOT NULL
);

CREATE TABLE llm_cache_index (
    cache_key          TEXT PRIMARY KEY,
    artifact_path      TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'complete',
    provider_request_id TEXT NOT NULL DEFAULT '',
    token_usage        INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

CREATE INDEX idx_models_connection ON llm_models(connection_id);
CREATE INDEX idx_verify_model ON llm_verification_logs(model_profile_id, capability);
CREATE INDEX idx_audit_task ON llm_binding_audit_logs(task_code);
"""

# v2：導入 roothinks LAVA setup 的連線 lifecycle（NOTE-023）。
# 一條「線路」在操作者眼中是 vendor + key + 單一 model，因此把選定的 model
# 記在 connection 上；llm_models 仍保留完整的 model profile 與能力紀錄，
# 兩者不衝突 —— 前者是「這條線路現在用哪一個」，後者是「這個模型驗過什麼」。
_MIGRATION_2 = """
ALTER TABLE llm_connections ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE llm_connections ADD COLUMN selected_model_profile_id TEXT;
ALTER TABLE llm_connections ADD COLUMN available_models_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE llm_connections ADD COLUMN last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE llm_task_bindings ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0;
"""

#: user_version -> DDL。逐版套用，已套用過的版本不重跑。
MIGRATIONS: tuple[str, ...] = (_MIGRATION_1, _MIGRATION_2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 讀取用的資料列
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionRow:
    connection_id: str
    name: str
    provider: str
    base_url: str
    secret_ref: str
    timeout_sec: int
    enabled: bool
    status: str
    notes: str
    created_at: str
    last_verified_at: str | None
    lifecycle: str = Lifecycle.DRAFT
    selected_model_profile_id: str | None = None
    available_models: tuple[str, ...] = ()
    last_error: str = ""

    @property
    def healthy(self) -> bool:
        """§43：enabled=false 或 failed/inactive 狀態不得新 bind。"""
        return self.enabled and self.status in (
            ConnectionStatus.PENDING, ConnectionStatus.ACTIVE
        )

    @property
    def bindable(self) -> bool:
        """只有「已鎖定且健康」的線路可以被綁到 task。

        沿用 roothinks LAVA setup 的 `僅顯示 Locked` 規則（NOTE-023）：
        鎖定是操作者對「這條線路我確認過了」的明示，沒有它，
        一條剛貼上 key 還沒測過的線路就會出現在綁定選單裡。
        """
        return self.healthy and self.lifecycle == Lifecycle.LOCKED

    # -- lifecycle 可用動作（UI 據此決定按鈕的啟用狀態）------------------

    @property
    def can_edit(self) -> bool:
        """鎖定後 vendor / key / model 一律凍結。"""
        return self.lifecycle != Lifecycle.LOCKED

    @property
    def can_fetch(self) -> bool:
        return self.lifecycle != Lifecycle.LOCKED

    @property
    def can_test(self) -> bool:
        return self.lifecycle in (Lifecycle.FETCHED, Lifecycle.CONNECTED)

    @property
    def can_lock(self) -> bool:
        return self.lifecycle == Lifecycle.CONNECTED

    @property
    def can_unlock(self) -> bool:
        return self.lifecycle == Lifecycle.LOCKED


@dataclass(frozen=True)
class ModelProfileRow:
    model_profile_id: str
    connection_id: str
    model_id: str
    display_name: str
    declared_capabilities: tuple[Capability, ...]
    verified_capabilities: tuple[Capability, ...]
    provider_revision: str
    status: str
    fetched_at: str

    def supports(self, required: Iterable[Capability]) -> bool:
        """只認 probe-verified 能力。§44：provider metadata 只能作提示。"""
        return set(required).issubset(set(self.verified_capabilities))


@dataclass(frozen=True)
class BindingRow:
    task_code: str
    model_profile_id: str
    binding_version: int
    status: str
    updated_at: str
    updated_by: str
    is_locked: bool = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class LLMRegistry:
    """live LLM registry 的 SQLite 閘門。"""

    def __init__(self, path: str | Path = DEFAULT_REGISTRY_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    # -- 連線與 migration --------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> int:
        """套用尚未執行的 migration，回傳最終 schema 版本。

        以 SQLite 的 user_version 記錄進度而非另建一張表：少一張表就少一個
        會與實際結構脫節的地方，而 user_version 本身就是為此設計的。
        """
        with self.connect() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            for version in range(current, len(MIGRATIONS)):
                connection.executescript(MIGRATIONS[version])
                connection.execute(f"PRAGMA user_version = {version + 1}")
            return len(MIGRATIONS)

    # -- Connections -------------------------------------------------------

    @staticmethod
    def _check_secret_ref(secret_ref: str) -> str:
        """寫入前把 secret_ref 過一次解析，明文 key 與 session ref 都擋在門外。"""
        reference = SecretRef.parse(secret_ref)
        if reference.scheme not in PERSISTENT_SCHEMES:
            raise RegistryError(
                f"secret_ref {reference} cannot be stored: {reference.scheme}: refs "
                "live only in process memory and would be unresolvable after a "
                "restart. Use env: or vault:."
            )
        return str(reference)

    def add_connection(
        self,
        name: str,
        provider: str,
        secret_ref: str,
        base_url: str = "",
        timeout_sec: int = 30,
        notes: str = "",
        enabled: bool = True,
    ) -> ConnectionRow:
        if not name.strip():
            raise RegistryError("connection name must not be empty")
        if timeout_sec <= 0:
            raise RegistryError(f"timeout_sec must be positive, got {timeout_sec!r}")
        reference = self._check_secret_ref(secret_ref)
        connection_id = str(uuid.uuid4())
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO llm_connections (connection_id, name, provider, "
                    "base_url, secret_ref, timeout_sec, enabled, status, notes, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        connection_id, name.strip(), provider, base_url, reference,
                        int(timeout_sec), int(bool(enabled)),
                        ConnectionStatus.PENDING, notes, _now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise RegistryError(
                    f"a connection named {name!r} already exists"
                ) from error
        return self.get_connection(connection_id)

    def get_connection(self, connection_id: str) -> ConnectionRow:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM llm_connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
        if row is None:
            raise RegistryError(f"connection {connection_id!r} not found")
        return _connection_row(row)

    def list_connections(self) -> list[ConnectionRow]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM llm_connections ORDER BY created_at, name"
            ).fetchall()
        return [_connection_row(row) for row in rows]

    def set_connection_status(self, connection_id: str, status: str) -> None:
        if status not in ConnectionStatus.ALL:
            raise RegistryError(
                f"unknown connection status {status!r}; expected one of "
                f"{ConnectionStatus.ALL}"
            )
        with self.connect() as db:
            db.execute(
                "UPDATE llm_connections SET status = ? WHERE connection_id = ?",
                (status, connection_id),
            )

    def set_connection_enabled(self, connection_id: str, enabled: bool) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE llm_connections SET enabled = ? WHERE connection_id = ?",
                (int(bool(enabled)), connection_id),
            )

    def rotate_secret_ref(self, connection_id: str, secret_ref: str) -> None:
        """更新 connection 指向的 secret_ref。

        §46：只換 credential 而 provider/model/base_url/runtime identity 不變時，
        不必 invalidate scientific lock。真正做到「identity 不變」的方式是
        vault rotation 保持同一個 ref；本方法用於改指到另一個 ref，
        呼叫端必須自行判斷那是否構成 identity 變更。
        """
        reference = self._check_secret_ref(secret_ref)
        with self.connect() as db:
            db.execute(
                "UPDATE llm_connections SET secret_ref = ? WHERE connection_id = ?",
                (reference, connection_id),
            )

    def delete_connection(self, connection_id: str) -> None:
        dependents = self.dependent_tasks_for_connection(connection_id)
        if dependents:
            raise DependencyError(
                f"connection {connection_id!r} still backs task binding(s) "
                f"{list(dependents)}; rebind or unbind them first",
                dependents,
            )
        with self.connect() as db:
            db.execute(
                "DELETE FROM llm_models WHERE connection_id = ?", (connection_id,)
            )
            db.execute(
                "DELETE FROM llm_connections WHERE connection_id = ?", (connection_id,)
            )

    # -- Models ------------------------------------------------------------

    def upsert_models(self, connection_id: str, descriptors: Iterable) -> list[ModelProfileRow]:
        """把 fetch-models 的結果寫入。只更新 declared 能力，不碰 verified。

        刻意不在此觸碰 verified_caps_json：重新 fetch 一次 model list 不構成
        任何能力驗證，若順手覆蓋 verified 就會讓「按了 Fetch Models」
        看起來像通過了 probe。
        """
        self.get_connection(connection_id)
        with self.connect() as db:
            for descriptor in descriptors:
                declared = json.dumps(
                    [c.value for c in descriptor.declared_capabilities], sort_keys=True
                )
                existing = db.execute(
                    "SELECT model_profile_id FROM llm_models "
                    "WHERE connection_id = ? AND model_id = ?",
                    (connection_id, descriptor.model_id),
                ).fetchone()
                if existing is None:
                    db.execute(
                        "INSERT INTO llm_models (model_profile_id, connection_id, "
                        "model_id, display_name, declared_caps_json, "
                        "provider_revision, status, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            str(uuid.uuid4()), connection_id, descriptor.model_id,
                            descriptor.display_name, declared,
                            descriptor.provider_revision, "pending", _now(),
                        ),
                    )
                else:
                    db.execute(
                        "UPDATE llm_models SET display_name = ?, declared_caps_json = ?, "
                        "provider_revision = ?, fetched_at = ? WHERE model_profile_id = ?",
                        (
                            descriptor.display_name, declared,
                            descriptor.provider_revision, _now(),
                            existing["model_profile_id"],
                        ),
                    )
        return self.list_models(connection_id)

    def get_model_profile(self, model_profile_id: str) -> ModelProfileRow:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM llm_models WHERE model_profile_id = ?",
                (model_profile_id,),
            ).fetchone()
        if row is None:
            raise RegistryError(f"model profile {model_profile_id!r} not found")
        return _model_row(row)

    def list_models(self, connection_id: str | None = None) -> list[ModelProfileRow]:
        query = "SELECT * FROM llm_models"
        params: tuple[Any, ...] = ()
        if connection_id is not None:
            query += " WHERE connection_id = ?"
            params = (connection_id,)
        query += " ORDER BY model_id"
        with self.connect() as db:
            return [_model_row(row) for row in db.execute(query, params).fetchall()]

    def delete_model_profile(self, model_profile_id: str) -> None:
        dependents = self.dependent_tasks_for_model(model_profile_id)
        if dependents:
            raise DependencyError(
                f"model profile {model_profile_id!r} is still bound to task(s) "
                f"{list(dependents)}; rebind those tasks before deleting",
                dependents,
            )
        with self.connect() as db:
            db.execute(
                "DELETE FROM llm_verification_logs WHERE model_profile_id = ?",
                (model_profile_id,),
            )
            db.execute(
                "DELETE FROM llm_models WHERE model_profile_id = ?", (model_profile_id,)
            )

    # -- Verification ------------------------------------------------------

    def record_verification(self, model_profile_id: str, result) -> tuple[Capability, ...]:
        """寫入一筆 probe log，並在同一交易內重算 verified 能力。

        兩件事必須同一個交易：log 是證據、verified 是結論，
        中間斷掉就會出現「宣稱驗證過但查不到 log」或反之的狀態。
        """
        self.get_model_profile(model_profile_id)
        with self.connect() as db:
            db.execute(
                "INSERT INTO llm_verification_logs (verify_id, model_profile_id, "
                "capability, success, latency_ms, error_sanitized, artifact_hash, "
                "verified_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), model_profile_id, result.capability.value,
                    int(bool(result.success)), int(result.latency_ms),
                    result.error_sanitized, result.artifact_hash(), _now(),
                ),
            )
            verified = _derive_verified(db, model_profile_id)
            db.execute(
                "UPDATE llm_models SET verified_caps_json = ?, status = ? "
                "WHERE model_profile_id = ?",
                (
                    json.dumps([c.value for c in verified], sort_keys=True),
                    "verified" if verified else "pending",
                    model_profile_id,
                ),
            )
            connection_id = db.execute(
                "SELECT connection_id FROM llm_models WHERE model_profile_id = ?",
                (model_profile_id,),
            ).fetchone()["connection_id"]
            # 成功的 probe 證明連線可用，失敗的 probe **不**證明連線壞掉 ——
            # 一個模型不支援 structured_json 是模型的事，不是憑證或端點的事。
            # 若在此把連線標成 degraded，Bind 被拒時給出的理由會指向連線，
            # 而真正的原因是能力不足，操作者會照著錯的方向去修。
            # 連線層級的故障由 fetch_models 的 ProviderError 標記（見 services）。
            if result.success:
                db.execute(
                    "UPDATE llm_connections SET last_verified_at = ?, status = ? "
                    "WHERE connection_id = ?",
                    (_now(), ConnectionStatus.ACTIVE, connection_id),
                )
            else:
                db.execute(
                    "UPDATE llm_connections SET last_verified_at = ? "
                    "WHERE connection_id = ?",
                    (_now(), connection_id),
                )
        return verified

    def verification_logs(self, model_profile_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM llm_verification_logs"
        params: tuple[Any, ...] = ()
        if model_profile_id is not None:
            query += " WHERE model_profile_id = ?"
            params = (model_profile_id,)
        query += " ORDER BY verified_at"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def verified_capabilities(self, model_profile_id: str) -> tuple[Capability, ...]:
        return self.get_model_profile(model_profile_id).verified_capabilities

    def recompute_verified_capabilities(self, model_profile_id: str) -> tuple[Capability, ...]:
        """直接由 probe log 重算，供稽核比對 verified_caps_json 是否漂移。"""
        with self.connect() as db:
            return _derive_verified(db, model_profile_id)

    # -- Bindings ----------------------------------------------------------

    def binding_identity(self, model_profile_id: str) -> dict[str, str]:
        """一個 binding 在科學上的身分：connection + provider + model + revision。

        name / notes 這類管理用別名刻意不在內（§43：name 不得參與 scientific hash）。
        """
        model = self.get_model_profile(model_profile_id)
        connection = self.get_connection(model.connection_id)
        return {
            "connection_id": connection.connection_id,
            "provider": connection.provider,
            "model_id": model.model_id,
            "provider_revision": model.provider_revision,
        }

    def set_binding(
        self,
        task_code: str,
        model_profile_id: str,
        required: Iterable[Capability],
        actor: str = "",
        reason: str = "",
    ) -> BindingRow:
        """改綁一個 task。能力不相容、連線不可用時一律拒絕。"""
        existing_binding = self.get_binding(task_code)
        if existing_binding is not None and existing_binding.is_locked:
            raise RegistryError(
                f"binding for {task_code!r} is locked; unlock it before rebinding. "
                "The lock is the operator's confirmation that this task was checked."
            )
        model = self.get_model_profile(model_profile_id)
        connection = self.get_connection(model.connection_id)
        if not connection.healthy:
            raise RegistryError(
                f"connection {connection.name!r} is {connection.status} / "
                f"enabled={connection.enabled}; §43 forbids binding a task to a "
                "failed or inactive connection"
            )
        if connection.lifecycle != Lifecycle.LOCKED:
            raise RegistryError(
                f"connection {connection.name!r} is {connection.lifecycle}, not "
                "locked. Only a locked line can be bound to a task — fetch models, "
                "select one, Test, then Connect (NOTE-023)."
            )
        if connection.selected_model_profile_id != model_profile_id:
            raise RegistryError(
                f"connection {connection.name!r} is locked onto a different model; "
                "a locked line offers exactly the model it was checked with"
            )
        required_caps = tuple(Capability.parse(c) for c in required)
        missing = sorted(
            c.value for c in required_caps if c not in model.verified_capabilities
        )
        if missing:
            raise RegistryError(
                f"cannot bind {task_code!r} to model {model.model_id!r}: capability "
                f"{missing} not probe-verified. Provider metadata is a hint, not "
                "verification (SRC-SAI §44)."
            )

        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM llm_task_bindings WHERE task_code = ?", (task_code,)
            ).fetchone()
            before_hash = ""
            version = 1
            if existing is not None:
                before_hash = hash_object(
                    self.binding_identity(existing["model_profile_id"])
                )
                version = int(existing["binding_version"]) + 1
            after_hash = hash_object(self.binding_identity(model_profile_id))
            db.execute(
                "INSERT INTO llm_task_bindings (task_code, model_profile_id, "
                "binding_version, status, updated_at, updated_by) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(task_code) DO UPDATE SET model_profile_id = excluded."
                "model_profile_id, binding_version = excluded.binding_version, "
                "status = excluded.status, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (task_code, model_profile_id, version, "draft", _now(), actor),
            )
            db.execute(
                "INSERT INTO llm_binding_audit_logs (audit_id, task_code, before_hash, "
                "after_hash, actor, reason, changed_at) VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), task_code, before_hash, after_hash,
                    actor, reason, _now(),
                ),
            )
        return self.get_binding(task_code)

    def get_binding(self, task_code: str) -> BindingRow | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM llm_task_bindings WHERE task_code = ?", (task_code,)
            ).fetchone()
        return None if row is None else _binding_row(row)

    def list_bindings(self) -> list[BindingRow]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM llm_task_bindings ORDER BY task_code"
            ).fetchall()
        return [_binding_row(row) for row in rows]

    def binding_audit_log(self, task_code: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM llm_binding_audit_logs"
        params: tuple[Any, ...] = ()
        if task_code is not None:
            query += " WHERE task_code = ?"
            params = (task_code,)
        query += " ORDER BY changed_at"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    # -- Lifecycle（roothinks LAVA setup 流程，NOTE-023）--------------------

    def set_lifecycle(self, connection_id: str, lifecycle: str) -> ConnectionRow:
        """推進或退回連線的設定進度。非法轉移一律拒絕。

        以 TRANSITIONS 表判定而非散在各處的 if：合法轉移只有一張表，
        新增狀態時不會漏掉某一條路徑。
        """
        if lifecycle not in Lifecycle.ALL:
            raise RegistryError(
                f"unknown lifecycle {lifecycle!r}; expected one of {Lifecycle.ALL}"
            )
        current = self.get_connection(connection_id)
        allowed = Lifecycle.TRANSITIONS[current.lifecycle]
        if lifecycle not in allowed:
            raise RegistryError(
                f"connection {current.name!r} is {current.lifecycle}; it cannot move "
                f"to {lifecycle}. Allowed next states: {list(allowed)}."
            )
        with self.connect() as db:
            db.execute(
                "UPDATE llm_connections SET lifecycle = ? WHERE connection_id = ?",
                (lifecycle, connection_id),
            )
        return self.get_connection(connection_id)

    def set_available_models(
        self, connection_id: str, model_ids: Iterable[str]
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE llm_connections SET available_models_json = ? "
                "WHERE connection_id = ?",
                (json.dumps(sorted(set(model_ids)), sort_keys=True), connection_id),
            )

    def select_model(self, connection_id: str, model_profile_id: str) -> ConnectionRow:
        """選定這條線路要用的模型。鎖定後不得更換。"""
        current = self.get_connection(connection_id)
        if not current.can_edit:
            raise RegistryError(
                f"connection {current.name!r} is locked; unlock it before changing "
                "the model. Locking is the operator's statement that this line was "
                "checked, so silently swapping the model would void it."
            )
        model = self.get_model_profile(model_profile_id)
        if model.connection_id != connection_id:
            raise RegistryError(
                f"model profile {model.model_id!r} belongs to another connection"
            )
        with self.connect() as db:
            db.execute(
                "UPDATE llm_connections SET selected_model_profile_id = ? "
                "WHERE connection_id = ?",
                (model_profile_id, connection_id),
            )
        return self.get_connection(connection_id)

    def set_last_error(self, connection_id: str, message: str) -> None:
        """記下最近一次失敗的原因，供 UI 顯示。訊息必須已遮蔽。"""
        with self.connect() as db:
            db.execute(
                "UPDATE llm_connections SET last_error = ? WHERE connection_id = ?",
                (message, connection_id),
            )

    def set_binding_locked(self, task_code: str, locked: bool) -> BindingRow:
        """鎖定／解鎖一個 task 的 draft binding。

        這是 **draft 層** 的確認，與 formal 的 llm_runtime.lock 是兩件事：
        它只表示「這個綁定我確認過了，不要手滑改掉」，不產生任何 formal identity。
        """
        if self.get_binding(task_code) is None:
            raise RegistryError(f"task {task_code!r} has no binding to lock")
        with self.connect() as db:
            db.execute(
                "UPDATE llm_task_bindings SET is_locked = ? WHERE task_code = ?",
                (int(bool(locked)), task_code),
            )
        return self.get_binding(task_code)

    # -- Cache index -------------------------------------------------------

    def record_cache_entry(
        self,
        cache_key: str,
        artifact_path: str,
        provider_request_id: str = "",
        token_usage: int = 0,
        latency_ms: int = 0,
        status: str = "complete",
    ) -> None:
        """登錄一筆 content-addressed cache 產物，供費用稽核。

        以 cache_key 為主鍵且重複寫入不更新既有列：同一把鑰匙代表同一份輸入，
        真正的 provider 呼叫只發生過一次，累加或覆寫費用都會讓帳失真。
        """
        with self.connect() as db:
            db.execute(
                "INSERT INTO llm_cache_index (cache_key, artifact_path, status, "
                "provider_request_id, token_usage, latency_ms, created_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO NOTHING",
                (
                    cache_key, artifact_path, status, provider_request_id,
                    int(token_usage), int(latency_ms), _now(),
                ),
            )

    def cache_entries(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM llm_cache_index ORDER BY created_at, cache_key"
                ).fetchall()
            ]

    # -- 相依性 -----------------------------------------------------------

    def dependent_tasks_for_model(self, model_profile_id: str) -> tuple[str, ...]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT task_code FROM llm_task_bindings WHERE model_profile_id = ? "
                "ORDER BY task_code",
                (model_profile_id,),
            ).fetchall()
        return tuple(row["task_code"] for row in rows)

    def dependent_tasks_for_connection(self, connection_id: str) -> tuple[str, ...]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT b.task_code FROM llm_task_bindings b "
                "JOIN llm_models m ON m.model_profile_id = b.model_profile_id "
                "WHERE m.connection_id = ? ORDER BY b.task_code",
                (connection_id,),
            ).fetchall()
        return tuple(row["task_code"] for row in rows)


# ---------------------------------------------------------------------------
# 內部
# ---------------------------------------------------------------------------


def _derive_verified(db: sqlite3.Connection, model_profile_id: str) -> tuple[Capability, ...]:
    """以「每個能力最近一次 probe 的結果」判定 verified 能力。

    採最近一次而非「曾經成功過」：模型被下架或方案降級後 probe 會開始失敗，
    若沿用歷史成功結果，task binding 會繼續指著一個已經不能用的能力。
    """
    rows = db.execute(
        "SELECT capability, success FROM llm_verification_logs "
        "WHERE model_profile_id = ? ORDER BY verified_at, rowid",
        (model_profile_id,),
    ).fetchall()
    latest: dict[str, bool] = {}
    for row in rows:
        latest[row["capability"]] = bool(row["success"])
    return tuple(
        Capability.parse(name) for name, ok in sorted(latest.items()) if ok
    )


def _capabilities(raw: str) -> tuple[Capability, ...]:
    return tuple(Capability.parse(value) for value in json.loads(raw or "[]"))


def _connection_row(row: sqlite3.Row) -> ConnectionRow:
    return ConnectionRow(
        connection_id=row["connection_id"], name=row["name"],
        provider=row["provider"], base_url=row["base_url"],
        secret_ref=row["secret_ref"], timeout_sec=int(row["timeout_sec"]),
        enabled=bool(row["enabled"]), status=row["status"], notes=row["notes"],
        created_at=row["created_at"], last_verified_at=row["last_verified_at"],
        lifecycle=row["lifecycle"],
        selected_model_profile_id=row["selected_model_profile_id"],
        available_models=tuple(json.loads(row["available_models_json"] or "[]")),
        last_error=row["last_error"] or "",
    )


def _model_row(row: sqlite3.Row) -> ModelProfileRow:
    return ModelProfileRow(
        model_profile_id=row["model_profile_id"], connection_id=row["connection_id"],
        model_id=row["model_id"], display_name=row["display_name"],
        declared_capabilities=_capabilities(row["declared_caps_json"]),
        verified_capabilities=_capabilities(row["verified_caps_json"]),
        provider_revision=row["provider_revision"], status=row["status"],
        fetched_at=row["fetched_at"],
    )


def _binding_row(row: sqlite3.Row) -> BindingRow:
    return BindingRow(
        task_code=row["task_code"], model_profile_id=row["model_profile_id"],
        binding_version=int(row["binding_version"]), status=row["status"],
        updated_at=row["updated_at"], updated_by=row["updated_by"],
        is_locked=bool(row["is_locked"]),
    )
