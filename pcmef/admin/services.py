# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.routes_llm（HTTP）與 pcmef.cli 的 llm 子指令（CLI）
#         共用呼叫；內部驅動 pcmef.llm.registry、pcmef.llm.verification 與
#         pcmef.secrets.vault；輸出一律是本檔的 *View 物件，永不含 secret 值。
# 檔案路徑: pcmef/admin/services.py
# 產生時間: 2026-08-27 00:15 +08:00
# 版本: v0.1.0
# 功能說明: 把「新增連線、抓模型清單、驗證能力、改綁角色」這幾個動作寫成一份，
#           讓網頁按鈕與命令列指令跑的是同一段程式，不會有兩套行為。
# 模組定位: UI 與 CLI 的共用服務層，也是 secret 遮蔽的最後一道出口。
#           它「不是」HTTP 層（不認識 request/response），也不是儲存層。
# 主要責任:
#   1. ConnectionView / ModelView / BindingView 定義只含遮蔽資訊的輸出型別
#   2. AdminService.add_connection() 把明文 key 立刻轉成 secret_ref 後即丟棄
#   3. AdminService.fetch_models() 經 ProviderAdapter 取回 normalized descriptor
#   4. AdminService.verify_model() 委派 CapabilityVerifier 並回傳逐項結論
#   5. AdminService.bind_task() 依 §45 必要能力檢查後改綁
#   6. AdminService.binding_views() 產生 dropdown 選項，只列相容且可用的 model
#   7. AdminService.connection_views() 以 ****abcd 呈現憑證，永不回傳完整值
#   8. AdminService.snapshot_view() 唯讀比對 draft 與已凍結的 llm_runtime.lock
# 維護提醒:
#   - 不得在任何 *View 或回傳值中放入完整 API key。§46 規定 UI 提交後 server
#     不再回傳完整 key，這一層是最後能攔住的地方。
#   - 不得讓 UI 繞過本層直接操作 registry；兩套寫入路徑必然行為分歧，
#     而其中一套會忘記做能力檢查。
#   - 不得在 add_connection() 保留 api_key 參數的值到函式結束之後；
#     它只用來換取 secret_ref。
#   - 不得在此層決定網路曝露或 CSRF；那屬 admin.auth，混進來會讓 CLI 也被迫處理。
#   - v0.1.0 新增：首版共用服務層，決策見 NOTE-019。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_admin_services.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pcmef.agents.provider import Capability, ConnectionProfile, get_adapter
from pcmef.core.config import ResolvedConfig, load_config
from pcmef.core.hash import hash_object
from pcmef.core.locks import LockStore
from pcmef.llm.capabilities import (
    FORMAL_TASK_CODES,
    MINIMUM_BINDABLE_CAPABILITIES,
    TASK_REGISTRY,
    compatible_models,
    missing_capabilities,
    required_capabilities,
    roles_blocked,
    roles_servable,
)
from pcmef.core.logging_setup import redact
from pcmef.llm.registry import (
    ConnectionRow,
    ConnectionStatus,
    DependencyError,
    Lifecycle,
    LLMRegistry,
    ModelProfileRow,
)
from pcmef.llm.snapshot import build_runtime_snapshot
from pcmef.llm.verification import DEFAULT_ARTIFACT_ROOT, CapabilityVerifier
from pcmef.secrets.vault import SecretRef, SecretVault

__all__ = [
    "AdminServiceError",
    "ConnectionView",
    "ModelView",
    "BindingOption",
    "BindingView",
    "ChecklistItem",
    "SnapshotView",
    "AdminService",
]

#: 憑證無法解析時顯示的字樣。刻意不顯示成空字串 —— 空白看起來像「沒設定」，
#: 而實際情況是「設定了但解不開」，兩者的處置完全不同。
UNRESOLVABLE_FINGERPRINT = "(unresolvable)"


class AdminServiceError(RuntimeError):
    """服務層的操作前提不成立（缺憑證來源、能力不足、對象不存在）。"""


@dataclass(frozen=True)
class ModelView:
    model_profile_id: str
    model_id: str
    display_name: str
    declared: tuple[str, ...]
    verified: tuple[str, ...]
    provider_revision: str
    status: str


@dataclass(frozen=True)
class ConnectionView:
    connection_id: str
    name: str
    provider: str
    base_url: str
    secret_ref: str
    secret_fingerprint: str
    timeout_sec: int
    enabled: bool
    status: str
    notes: str
    last_verified_at: str | None
    models: tuple[ModelView, ...]
    lifecycle: str = Lifecycle.DRAFT
    selected_model_profile_id: str | None = None
    selected_model_id: str = ""
    last_error: str = ""
    #: 這條線路的選定模型目前能服務哪些 task（依 §45 的逐 role 要求）。
    servable_roles: tuple[str, ...] = ()
    can_edit: bool = True
    can_fetch: bool = True
    can_test: bool = False
    can_lock: bool = False
    can_unlock: bool = False
    #: 這一列現在該做什麼。空字串代表這條線路已經走完，可以綁定。
    #:
    #: 存在的理由是 LAVA 的四個階段各自解鎖不同的按鈕，於是畫面上同時有
    #: 五顆按鈕、其中四顆是灰的，而「為什麼是灰的」只寫在 registry 的
    #: lifecycle 規則裡。少了這一行，操作者能看到自己被卡住，但看不到卡在哪。
    next_step: str = ""

    @property
    def healthy(self) -> bool:
        return self.enabled and self.status in ("pending", "active")

    @property
    def bindable(self) -> bool:
        """只有已鎖定且健康的線路能被綁到 task（NOTE-023）。"""
        return self.healthy and self.lifecycle == Lifecycle.LOCKED


@dataclass(frozen=True)
class BindingOption:
    model_profile_id: str
    label: str


@dataclass(frozen=True)
class BindingView:
    task_code: str
    ui_name: str
    description: str
    required: tuple[str, ...]
    output_schema: str
    current_model_profile_id: str | None
    current_label: str
    binding_version: int
    status: str
    options: tuple[BindingOption, ...]
    is_locked: bool = False

    @property
    def bound(self) -> bool:
        return self.current_model_profile_id is not None


@dataclass(frozen=True)
class ChecklistItem:
    """一條「還差什麼」。what 說缺什麼，how 說去哪裡補。

    存在的理由是 blocking reason 的原文是寫給程式看的 —— 像
    `prompt file configs/agents/prompts/physics_agent.md does not exist`
    技術上完全正確，但它沒告訴操作者「所以我現在該按哪裡」，
    於是整張卡片在畫面上等同於雜訊。
    """

    what: str
    how: str


@dataclass(frozen=True)
class SnapshotView:
    """§42 區塊 D 的四個欄位，加上 draft 側的判定結果。"""

    resolved_hash: str
    created_at: str
    state: str
    badge: str
    comparison: str
    blocking: tuple[str, ...]
    draft_candidate_hash: str
    draft_freezable: bool
    #: blocking 的白話版。blocking 保留原文供 API 與稽核使用，兩者不互相取代。
    checklist: tuple[ChecklistItem, ...] = ()

    @property
    def headline(self) -> str:
        if self.draft_freezable:
            return "前置條件都齊了，可以凍結"
        n = len(self.checklist)
        return f"還差 {n} 項才能凍結" if n else "尚不可凍結"

    def to_api(self) -> dict:
        """§50：active formal snapshot 必須與 draft 清楚分開，故欄位分列。"""
        return {
            "active_resolved_hash": self.resolved_hash,
            "active_created_at": self.created_at,
            "state": self.state,
            "draft_candidate_hash": self.draft_candidate_hash,
            "draft_freezable": self.draft_freezable,
            "blocking_reasons": list(self.blocking),
        }


# --- blocking reason 的白話化 -----------------------------------------------
#
# 這些樣式**刻意寫成寬鬆比對**：對不上時退回顯示原文，而不是丟例外或吞掉。
# snapshot.py 之後新增一種 blocking reason 時，最糟的情況是那一條在畫面上
# 維持英文原樣 —— 難看，但操作者仍然看得到「有這一條」。反過來寫成嚴格比對
# 的話，一個沒跟上的樣式會讓那條前提從畫面上整個消失，而那正是最危險的
# 失敗方式：畫面顯示「都齊了」，CLI 卻凍不起來。

_RE_NO_BINDING = re.compile(r"^task (\S+) has no draft binding$")
_RE_MISSING_FILE = re.compile(r"^(prompt|schema) file (\S+) does not exist$")
_RE_LACKS_CAP = re.compile(
    r"^task (\S+) model '(.+)' lacks probe-verified capability \[(.*)\]$"
)
_RE_BAD_PROVIDER = re.compile(r"^task (\S+) is bound to provider '(.+)', which is not")
_RE_UNHEALTHY = re.compile(r"^task (\S+) is bound through connection '(.+)' whose")
_RE_NO_REVISION = re.compile(r"^task (\S+) model '(.+)' has no provider_revision")
_RE_BAD_REF = re.compile(r"^connection '(.+)' uses a (\S+): secret ref")
_RE_AWAITS = re.compile(r"^(agents\.[\w.]+) awaits advisor approval")


def _task_label(task_code: str) -> str:
    spec = TASK_REGISTRY.get(task_code)
    return spec.ui_name if spec else task_code


def _checklist(reasons: Iterable[str]) -> tuple[ChecklistItem, ...]:
    """把 blocking reason 原文譯成操作者看得懂的待辦清單。

    prompt 與 schema 檔刻意**合併成一條**。缺四個 agent 的 prompt 會產生四條
    長得幾乎一樣的訊息，那是雜訊不是資訊 —— 要做的其實只有一件事。
    """
    items: list[ChecklistItem] = []
    missing_files: dict[str, list[str]] = {"prompt": [], "schema": []}

    for reason in reasons:
        if match := _RE_MISSING_FILE.match(reason):
            missing_files[match.group(1)].append(match.group(2))
        elif match := _RE_NO_BINDING.match(reason):
            items.append(
                ChecklistItem(
                    what=f"{_task_label(match.group(1))} 還沒指定要用哪個模型",
                    how="到上面「3. 綁定 agent」那張表，替它選一條已 Locked 的線路",
                )
            )
        elif match := _RE_LACKS_CAP.match(reason):
            caps = match.group(3).replace("'", "")
            items.append(
                ChecklistItem(
                    what=f"{_task_label(match.group(1))} 綁的 {match.group(2)} 缺能力：{caps}",
                    how="回到「2. 設定線路」那一列按 Test 重跑能力驗證；仍然缺就換一個模型",
                )
            )
        elif match := _RE_BAD_PROVIDER.match(reason):
            items.append(
                ChecklistItem(
                    what=f"{_task_label(match.group(1))} 綁在 {match.group(2)}，這個 provider 不能進 formal",
                    how="stub_offline 只能走流程、不能凍結。改綁真實 provider 的線路",
                )
            )
        elif match := _RE_UNHEALTHY.match(reason):
            items.append(
                ChecklistItem(
                    what=f"{_task_label(match.group(1))} 綁的線路「{match.group(2)}」目前不健康",
                    how="到「2. 設定線路」那一列看錯誤訊息，重跑 Fetch / Test",
                )
            )
        elif match := _RE_NO_REVISION.match(reason):
            items.append(
                ChecklistItem(
                    what=f"{_task_label(match.group(1))} 綁的 {match.group(2)} 沒有可辨識的版本號",
                    how="重跑一次 Fetch，讓 provider 回報 revision；手動輸入的 model id 沒有版本號",
                )
            )
        elif match := _RE_BAD_REF.match(reason):
            items.append(
                ChecklistItem(
                    what=f"線路「{match.group(1)}」的憑證是 {match.group(2)}: 形式，formal run 解不開",
                    how="session: 憑證只活在記憶體裡。重新輸入一次 API key，或改用 env:<NAME>",
                )
            )
        elif match := _RE_AWAITS.match(reason):
            items.append(
                ChecklistItem(
                    what=f"設定值 {match.group(1)} 尚未核定",
                    how="在 configs 疊一層 overlay 填入核定值後重新載入",
                )
            )
        else:
            # 對不上的樣式原樣呈現。看得到總比消失好。
            items.append(ChecklistItem(what=reason, how=""))

    for kind, paths in missing_files.items():
        if not paths:
            continue
        label = "prompt" if kind == "prompt" else "JSON schema"
        items.insert(
            0,
            ChecklistItem(
                what=f"還有 {len(paths)} 個 {label} 檔沒建立",
                how="建立這些檔案：" + "、".join(paths),
            ),
        )
    return tuple(items)


class AdminService:
    """UI 與 CLI 共用的 LLM admin 操作。"""

    def __init__(
        self,
        registry: LLMRegistry,
        vault: SecretVault,
        adapter_factory=None,
        verifier: CapabilityVerifier | None = None,
        config: ResolvedConfig | None = None,
        freeze_dir: str | Path = "freeze",
        schemas_dir: str | Path = "schemas",
        prompts_dir: str | Path = "configs/agents/prompts",
        artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT,
    ) -> None:
        self.registry = registry
        self.vault = vault
        self._adapter_factory = adapter_factory or get_adapter
        self.verifier = verifier or CapabilityVerifier(
            registry=registry, vault=vault, artifact_root=artifact_root,
            adapter_factory=self._adapter_factory,
        )
        self.config = config if config is not None else load_config([Path("configs/base.yaml")])
        self.freeze_dir = Path(freeze_dir)
        self.schemas_dir = Path(schemas_dir)
        self.prompts_dir = Path(prompts_dir)

    # -- Formal snapshot（唯讀）--------------------------------------------

    def snapshot_view(self) -> SnapshotView:
        """§42 區塊 D：只讀地呈現 active snapshot 與 draft 的關係。

        本方法**不會**產生或凍結任何快照。§52 結語規定 UI 永遠不能成為
        繞過 freeze 的第二條設定通道，因此這裡連寫入的能力都不具備。
        """
        snapshot = build_runtime_snapshot(
            self.registry, self.config,
            schemas_dir=self.schemas_dir, prompts_dir=self.prompts_dir,
        )
        store = LockStore(self.freeze_dir)

        if not store.exists("llm_runtime"):
            return SnapshotView(
                resolved_hash="",
                created_at="",
                state="no active snapshot",
                badge="warn",
                comparison=(
                    f"draft candidate {snapshot.candidate_hash()[:16]}"
                    + ("（可凍結）" if snapshot.freezable else "（尚不可凍結）")
                ),
                blocking=snapshot.blocking_reasons,
                draft_candidate_hash=snapshot.candidate_hash(),
                draft_freezable=snapshot.freezable,
                checklist=_checklist(snapshot.blocking_reasons),
            )

        active_hash = store.load_hash("llm_runtime")
        if snapshot.freezable:
            same = hash_object(snapshot.to_lock_payload()) == active_hash
            comparison = (
                "draft 與 active 一致"
                if same
                else "draft 已與 active 分歧；既有 formal run 不受影響"
            )
        else:
            comparison = "draft 尚不可凍結，無法與 active 比對"
        return SnapshotView(
            resolved_hash=active_hash,
            created_at=store.created_at("llm_runtime").isoformat(),
            state="FROZEN (immutable)",
            badge="ok",
            comparison=comparison,
            blocking=snapshot.blocking_reasons,
            draft_candidate_hash=snapshot.candidate_hash(),
            draft_freezable=snapshot.freezable,
            checklist=_checklist(snapshot.blocking_reasons),
        )

    # -- Connections -------------------------------------------------------

    def add_connection(
        self,
        name: str,
        provider: str,
        api_key: str | None = None,
        secret_ref: str | None = None,
        base_url: str = "",
        timeout_sec: int = 30,
        notes: str = "",
        enabled: bool = True,
    ) -> ConnectionView:
        """建立連線 profile。

        api_key 與 secret_ref 二擇一：給 api_key 時立刻存進 vault 換成 ref，
        本函式結束後系統各層都只看得到 ref。§42 區塊 A：
        API key 不回傳；只建立 draft registry，不產 formal identity。
        """
        if bool(api_key) == bool(secret_ref):
            raise AdminServiceError(
                "supply exactly one of api_key or secret_ref: the key is converted "
                "to a reference immediately, or you point at an existing reference"
            )
        if api_key:
            if not self.vault.can_persist:
                # 本機主控台會自動保管一把 master key（vault.py 的
                # ensure_local_master_key），因此走到這裡幾乎只剩一種原因：
                # cryptography 套件沒裝，於是沒有東西能加密。
                raise AdminServiceError(
                    "cannot persist an API key on this machine: no encryption backend. "
                    'Install it with pip install -e ".[admin]", or put the key in an '
                    "environment variable and use secret_ref=env:<NAME>."
                )
            reference = str(self.vault.store(api_key))
        else:
            reference = str(SecretRef.parse(str(secret_ref)))

        row = self.registry.add_connection(
            name=name, provider=provider, secret_ref=reference,
            base_url=base_url, timeout_sec=timeout_sec, notes=notes, enabled=enabled,
        )
        return self._connection_view(row)

    def rotate_secret(self, connection_id: str, api_key: str) -> ConnectionView:
        """換憑證但不改 identity。

        §46：只換 credential secret、provider/model/base_url/runtime identity 不變，
        可不 invalid scientific lock。vault: ref 走就地 rotation，
        因此 lock 內記錄的 secret_ref 不變 —— 這正是「identity 未變」的證明。
        """
        row = self.registry.get_connection(connection_id)
        reference = SecretRef.parse(row.secret_ref)
        if reference.scheme != "vault":
            raise AdminServiceError(
                f"connection {row.name!r} points at {reference}; an env: reference is "
                "rotated by changing the environment variable, not through this page"
            )
        self.vault.rotate(reference, api_key)
        return self._connection_view(self.registry.get_connection(connection_id))

    def connection_views(self) -> list[ConnectionView]:
        return [self._connection_view(row) for row in self.registry.list_connections()]

    def set_enabled(self, connection_id: str, enabled: bool) -> None:
        self.registry.set_connection_enabled(connection_id, enabled)

    def delete_connection(self, connection_id: str) -> None:
        self.registry.delete_connection(connection_id)

    def delete_model(self, model_profile_id: str) -> None:
        self.registry.delete_model_profile(model_profile_id)

    # -- Models ------------------------------------------------------------

    def fetch_models(self, connection_id: str) -> list[ModelView]:
        """向 provider 取得可用模型清單（LAVA 流程的 Fetch）。

        這是唯一能區分「連線壞掉」與「模型缺能力」的地方：list_models 失敗
        代表端點或憑證有問題，因此連線層級的 error 狀態只在這裡標記。
        capability probe 失敗不會動連線狀態（見 registry.record_verification）。
        """
        row = self.registry.get_connection(connection_id)
        if not row.can_fetch:
            raise AdminServiceError(
                f"connection {row.name!r} is locked; unlock it before fetching models"
            )
        adapter = self._adapter_factory(row.provider, self.vault.resolve)
        try:
            descriptors = adapter.list_models(self._profile(row))
        except Exception as error:
            self.registry.set_connection_status(connection_id, ConnectionStatus.ERROR)
            self.registry.set_last_error(connection_id, redact(str(error))[:400])
            raise
        rows = self.registry.upsert_models(connection_id, descriptors)
        self.registry.set_connection_status(connection_id, ConnectionStatus.ACTIVE)
        self.registry.set_last_error(connection_id, "")
        self.registry.set_available_models(connection_id, [m.model_id for m in rows])

        # 取回清單本身還不構成「選好了」；只有已選定模型時才推進到 fetched。
        # 這對應 LAVA 版型中 Test 按鈕在選定模型前保持 disabled 的行為。
        refreshed = self.registry.get_connection(connection_id)
        if refreshed.selected_model_profile_id and refreshed.lifecycle == Lifecycle.DRAFT:
            self.registry.set_lifecycle(connection_id, Lifecycle.FETCHED)
        return [_model_view(model) for model in rows]

    def add_manual_model_and_select(self, connection_id: str, model_id: str) -> ModelView:
        """§42 區塊 A：不支援 list API 時允許 Manual Model ID，並直接選定它。"""
        view = self.add_manual_model(connection_id, model_id)
        self.select_model(connection_id, view.model_profile_id)
        return view

    def select_model(self, connection_id: str, model_profile_id: str) -> ConnectionView:
        """選定這條線路要用的模型，並推進到 fetched。"""
        row = self.registry.select_model(connection_id, model_profile_id)
        if row.lifecycle == Lifecycle.DRAFT:
            row = self.registry.set_lifecycle(connection_id, Lifecycle.FETCHED)
        elif row.lifecycle == Lifecycle.CONNECTED:
            # 換了模型就等於這條線路還沒被測過，退回 fetched。
            # 沿用先前的 connected 會讓 Connect 按鈕對著一個沒測過的模型亮著。
            row = self.registry.set_lifecycle(connection_id, Lifecycle.FETCHED)
        return self._connection_view(row)

    def test_connection(self, connection_id: str):
        """LAVA 流程的 Test：對選定模型跑三項 probe，但**只以最低要求為門檻**。

        roothinks 的 Test 是「送一句話看回不回 OK」。PC-MEF 跑得更完整 ——
        §44 規定這些能力不可只信 metadata，必須實際 probe。但通過與否的判定
        依 §45 的逐 role 要求表：physics_agent 與 arbitration_agent 不需要
        vision，因此 **vision 失敗不擋鎖定**，只代表這條線路不能綁
        observation_agent 與 visual_semantic_agent。

        把三項全過當成鎖定門檻是錯的：那會讓一個沒有視覺能力的純文字強模型
        永遠鎖不起來，連帶把它能勝任的兩個角色一併排除。
        """
        row = self.registry.get_connection(connection_id)
        if not row.can_test:
            raise AdminServiceError(
                f"connection {row.name!r} is {row.lifecycle}; select a model first "
                "(Fetch Models), and unlock it if it is already locked"
            )
        if not row.selected_model_profile_id:
            raise AdminServiceError(
                f"connection {row.name!r} has no selected model; pick one before testing"
            )
        outcome = self.verify_model(row.selected_model_profile_id)
        verified = set(outcome.verified)
        blocking = sorted(
            c.value for c in MINIMUM_BINDABLE_CAPABILITIES if c not in verified
        )

        if blocking:
            self.registry.set_last_error(
                connection_id,
                f"cannot serve any agent role — missing {blocking}: "
                + "; ".join(
                    f"{r.capability.value}: {r.error_sanitized}"
                    for r in outcome.results
                    if not r.success and r.capability in MINIMUM_BINDABLE_CAPABILITIES
                )[:300],
            )
            # 刻意不退回 draft：模型清單與選擇都還有效，要修的是這個模型
            # 支不支援那些能力，或換一個模型再測。
            return outcome

        # 過了最低門檻就可以鎖。缺 vision 只記錄，不阻擋 —— 那是逐 role 的事。
        optional_gaps = sorted(
            c.value
            for c in (Capability.CHAT, Capability.STRUCTURED_JSON, Capability.VISION)
            if c not in verified
        )
        self.registry.set_last_error(
            connection_id,
            (
                f"可綁 {list(roles_servable(outcome.verified))}；"
                f"缺 {optional_gaps} 故不能綁 "
                f"{sorted(roles_blocked(outcome.verified))}"
            )
            if optional_gaps
            else "",
        )
        self.registry.set_lifecycle(connection_id, Lifecycle.CONNECTED)
        return outcome

    def lock_connection(self, connection_id: str) -> ConnectionView:
        """LAVA 流程的 Connect：鎖定這條線路，之後才能被綁到 task。"""
        row = self.registry.get_connection(connection_id)
        if not row.can_lock:
            raise AdminServiceError(
                f"connection {row.name!r} is {row.lifecycle}; run Test successfully "
                "before locking it"
            )
        return self._connection_view(
            self.registry.set_lifecycle(connection_id, Lifecycle.LOCKED)
        )

    def unlock_connection(self, connection_id: str) -> ConnectionView:
        """解鎖。仍被 task 綁定時拒絕，否則會留下綁著未鎖線路的 task。"""
        dependents = self.registry.dependent_tasks_for_connection(connection_id)
        if dependents:
            raise DependencyError(
                f"connection is still bound to task(s) {list(dependents)}; "
                "rebind or unbind them before unlocking",
                dependents,
            )
        return self._connection_view(
            self.registry.set_lifecycle(connection_id, Lifecycle.CONNECTED)
        )

    def set_binding_locked(self, task_code: str, locked: bool):
        """鎖定／解鎖一個 task 的 draft binding（LAVA 的 binding lock）。"""
        return self.registry.set_binding_locked(task_code, locked)

    def test_binding(self, task_code: str):
        """LAVA 的 binding Test：對這個 task 綁到的模型，只跑它需要的能力。

        **刻意不重跑三項全部。** §45 逐 role 列出必要能力，physics_agent 與
        arbitration_agent 本來就不需要 vision；對它們跑 vision probe 只會生出
        一個與這個綁定無關的 FAIL，然後讓操作者去修一件不必修的事。

        這不是新的 provider 能力，而是 §50 的 `POST /models/{id}/verify`
        綁到 task 之後的形態 —— 走同一個 CapabilityVerifier，證據一樣落盤。

        已鎖定的 binding 也允許測：那正是這顆按鈕最主要的用途 ——
        「我當初鎖的時候是好的，它現在還好嗎」。probe 失敗會如實把該能力
        從 verified 拿掉，而那是要它揭露的事實，不是要它隱瞞的。
        """
        binding = self.registry.get_binding(task_code)
        if binding is None:
            raise AdminServiceError(
                f"task {task_code!r} is not bound to any model, so there is "
                "nothing to test; bind one first"
            )
        return self.verify_model(
            binding.model_profile_id, required_capabilities(task_code)
        )

    def add_manual_model(self, connection_id: str, model_id: str) -> ModelView:
        """§42 區塊 A：不支援 list API 時允許 Manual Model ID。

        手動加入只登記存在，declared 能力一律留空 —— 手打一個名字不構成
        任何能力宣稱，必須照樣跑 probe。
        """
        from pcmef.agents.provider import ModelDescriptor

        row = self.registry.get_connection(connection_id)
        descriptor = ModelDescriptor(
            model_id=model_id, provider=row.provider, display_name=model_id
        )
        self.registry.upsert_models(connection_id, [descriptor])
        for model in self.registry.list_models(connection_id):
            if model.model_id == model_id:
                return _model_view(model)
        raise AdminServiceError(f"manual model {model_id!r} was not persisted")

    def verify_model(
        self, model_profile_id: str, capabilities: Iterable[Capability] | None = None
    ):
        return self.verifier.verify(model_profile_id, capabilities)

    # -- Bindings ----------------------------------------------------------

    def bind_task(
        self,
        task_code: str,
        model_profile_id: str,
        actor: str = "admin-ui",
        reason: str = "",
    ):
        """改綁一個 task 的 draft binding。

        §42 區塊 C：Active formal snapshot 只讀；Bind 只更新 draft version。
        本方法因此只寫 llm_task_bindings，絕不觸碰任何 lock。
        """
        if task_code not in TASK_REGISTRY:
            raise AdminServiceError(
                f"unknown task_code {task_code!r}; the formal registry is "
                f"{list(FORMAL_TASK_CODES)}"
            )
        return self.registry.set_binding(
            task_code=task_code,
            model_profile_id=model_profile_id,
            required=required_capabilities(task_code),
            actor=actor,
            reason=reason,
        )

    def binding_views(self) -> list[BindingView]:
        models = self.registry.list_models()
        connections = {c.connection_id: c for c in self.registry.list_connections()}
        bindings = {b.task_code: b for b in self.registry.list_bindings()}
        labels = {
            m.model_profile_id: self._label(m, connections) for m in models
        }

        views: list[BindingView] = []
        for task_code in FORMAL_TASK_CODES:
            spec = TASK_REGISTRY[task_code]
            current = bindings.get(task_code)
            offered = compatible_models(task_code, models, connections)
            views.append(
                BindingView(
                    task_code=task_code,
                    ui_name=spec.ui_name,
                    description=spec.description,
                    required=tuple(c.value for c in spec.required),
                    output_schema=spec.output_schema,
                    current_model_profile_id=(
                        current.model_profile_id if current else None
                    ),
                    current_label=(
                        labels.get(current.model_profile_id, "(missing profile)")
                        if current
                        else "not bound"
                    ),
                    binding_version=current.binding_version if current else 0,
                    status=(
                        ("locked" if current.is_locked else current.status)
                        if current
                        else "unbound"
                    ),
                    is_locked=bool(current.is_locked) if current else False,
                    options=tuple(
                        BindingOption(
                            model_profile_id=m.model_profile_id,
                            label=labels[m.model_profile_id],
                        )
                        for m in offered
                    ),
                )
            )
        return views

    def why_not_bindable(self, task_code: str, model_profile_id: str) -> list[str]:
        """列出某 model 綁不上某 task 的具體理由，供 UI 顯示。"""
        model = self.registry.get_model_profile(model_profile_id)
        connection = self.registry.get_connection(model.connection_id)
        reasons: list[str] = []
        if not connection.healthy:
            reasons.append(
                f"connection is {connection.status} / enabled={connection.enabled}"
            )
        if connection.lifecycle != Lifecycle.LOCKED:
            reasons.append(f"connection is not locked ({connection.lifecycle})")
        elif connection.selected_model_profile_id != model_profile_id:
            reasons.append(
                "this model is not the one the line was locked onto"
            )
        missing = missing_capabilities(task_code, model.verified_capabilities)
        if missing:
            reasons.append(
                "capability not probe-verified: " + ", ".join(c.value for c in missing)
            )
        return reasons

    # -- 內部 -------------------------------------------------------------

    @staticmethod
    def _profile(row: ConnectionRow) -> ConnectionProfile:
        return ConnectionProfile(
            connection_id=row.connection_id, provider=row.provider,
            secret_ref=row.secret_ref, base_url=row.base_url,
            timeout_sec=row.timeout_sec,
        )

    @staticmethod
    def _label(model: ModelProfileRow, connections: dict[str, ConnectionRow]) -> str:
        connection = connections.get(model.connection_id)
        prefix = connection.name if connection else "(orphan)"
        return f"{prefix} · {model.model_id}"

    def _connection_view(self, row: ConnectionRow) -> ConnectionView:
        try:
            masked = self.vault.fingerprint(row.secret_ref)
        except Exception:
            # 刻意攔下所有例外：憑證可能因環境變數未設、vault entry 被刪或
            # master key 換掉而解不開，而這份列表正是用來發現那些狀況的。
            # 讓其中任一種把整頁弄掛，等於失去唯一能看出問題的畫面。
            masked = UNRESOLVABLE_FINGERPRINT
        models = self.registry.list_models(row.connection_id)
        selected_profile = next(
            (m for m in models if m.model_profile_id == row.selected_model_profile_id),
            None,
        )
        selected = selected_profile.model_id if selected_profile else ""
        servable = (
            roles_servable(selected_profile.verified_capabilities)
            if selected_profile
            else ()
        )
        return ConnectionView(
            connection_id=row.connection_id, name=row.name, provider=row.provider,
            base_url=row.base_url, secret_ref=row.secret_ref,
            secret_fingerprint=masked, timeout_sec=row.timeout_sec,
            enabled=row.enabled, status=row.status, notes=row.notes,
            last_verified_at=row.last_verified_at,
            models=tuple(_model_view(m) for m in models),
            lifecycle=row.lifecycle,
            selected_model_profile_id=row.selected_model_profile_id,
            selected_model_id=selected,
            last_error=row.last_error,
            servable_roles=servable,
            can_edit=row.can_edit, can_fetch=row.can_fetch, can_test=row.can_test,
            can_lock=row.can_lock, can_unlock=row.can_unlock,
            next_step=_next_step(row, masked, bool(models)),
        )


def _next_step(row: ConnectionRow, fingerprint_text: str, has_models: bool) -> str:
    """這條線路的下一個動作。順序與 registry 的 lifecycle 規則一致。

    憑證排在最前面：憑證解不開時 Fetch 一定失敗，而失敗訊息講的是 provider
    呼叫出錯，指不回真正的原因。
    """
    if fingerprint_text == UNRESOLVABLE_FINGERPRINT:
        return (
            "憑證解不開。若用的是 env:<NAME>，那個環境變數在這個行程裡沒有值；"
            "最省事的作法是刪掉這條線路、重建一條並直接貼上 API key"
        )
    if not row.enabled:
        return "這條線路是 disabled 狀態"
    if row.lifecycle == Lifecycle.LOCKED:
        return ""
    if not has_models:
        return "按 Fetch 取得這個 provider 的模型清單"
    if not row.selected_model_profile_id:
        return "在下拉選單挑一個模型，按 Set"
    if row.lifecycle == Lifecycle.CONNECTED:
        return "按 Connect 鎖定，鎖定後才能綁到 agent"
    return "按 Test 實際驗證 chat / vision / structured_json 三項能力"


def _model_view(model: ModelProfileRow) -> ModelView:
    return ModelView(
        model_profile_id=model.model_profile_id, model_id=model.model_id,
        display_name=model.display_name,
        declared=tuple(c.value for c in model.declared_capabilities),
        verified=tuple(c.value for c in model.verified_capabilities),
        provider_revision=model.provider_revision, status=model.status,
    )
