# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 自動載入，供 tests/llm_admin/ 下所有測試使用；
#         在 tmp_path 建立臨時 SQLite registry、臨時 vault 與離線 stub adapter，
#         不觸碰真實的 registry/llm_admin.db、secrets/vault.json 或任何網路。
# 檔案路徑: tests/llm_admin/conftest.py
# 產生時間: 2026-08-26 23:05 +08:00
# 版本: v0.1.0
# 功能說明: 提供 LLM admin 測試共用的起始狀態 —— 一個乾淨的資料庫、一個假憑證，
#           以及一個能宣告「哪個模型有哪些能力」的離線 provider。
# 模組定位: 測試夾具集中處。它「不是」被測程式的一部分，也不得包含任何斷言。
# 主要責任:
#   1. fake_key / stub_provider / fake_environ 提供不落盤真實 secret 的憑證來源
#   2. vault 與 registry 建立臨時 vault 檔與臨時 SQLite 並跑完 migration
#   3. stub_adapter / adapter_factory 提供走完整程式路徑的離線 provider
#   4. seeded 建立一個 connection 與三種能力組合的 model profile
#   5. prompts_dir / decided_config 提供快照可凍結所需的前提
#   6. admin_service / make_client / csrf 提供 Flask 測試入口與 CSRF 權杖
# 維護提醒:
#   - 不得在此讀取真實環境變數或真實 vault；測試一旦依賴機器狀態就不可重現。
#   - 不得在夾具內寫斷言；夾具失敗要以例外呈現，斷言屬於測試本身。
#   - v0.1.0 新增：首版夾具。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin -q
# ------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pcmef.agents.provider import Capability, StubOfflineAdapter
from pcmef.llm.registry import LLMRegistry
from pcmef.secrets.vault import SecretVault

FAKE_KEY = "sk-" + "c0nftestKeyMaterial" * 2

STUB_PROVIDER = StubOfflineAdapter.provider

#: 三種刻意不同的能力組合，對應 §51 的 LLM-UI-02 / LLM-UI-03 情境。
STUB_CATALOGUE: dict[str, tuple[Capability, ...]] = {
    "full-model": (Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON),
    "chat-only": (Capability.CHAT,),
    "embed-only": (Capability.EMBEDDING,),
}


@pytest.fixture
def fake_key() -> str:
    """假憑證值。以 fixture 而非模組層常數對外提供：tests/ 不是 package，
    跨測試模組 import conftest 會失敗，fixture 是這個 repo 唯一可行的共用管道。"""
    return FAKE_KEY


@pytest.fixture
def stub_provider() -> str:
    return STUB_PROVIDER


@pytest.fixture
def fake_environ() -> dict[str, str]:
    return {"PCMEF_TEST_KEY": FAKE_KEY}


@pytest.fixture
def vault(tmp_path, fake_environ) -> SecretVault:
    return SecretVault(
        path=tmp_path / "vault.json",
        master_key="conftest-master-key",
        environ=fake_environ,
    )


@pytest.fixture
def registry(tmp_path) -> LLMRegistry:
    return LLMRegistry(tmp_path / "llm_admin.db")


@pytest.fixture
def stub_adapter(vault) -> StubOfflineAdapter:
    adapter = StubOfflineAdapter(vault.resolve)
    adapter.catalogue = dict(STUB_CATALOGUE)
    return adapter


@pytest.fixture
def adapter_factory(stub_adapter):
    """回傳與 get_adapter 同簽章的 factory，固定給出同一個 stub 實例。

    固定同一實例是為了讓測試能檢查 adapter.calls 累積的呼叫紀錄 ——
    LLM-CACHE-01 要斷言的正是「provider 只被呼叫一次」。
    """

    def factory(provider: str, resolve_secret):
        return stub_adapter

    return factory


@pytest.fixture
def lock_line():
    """把一條線路走完 LAVA 的 draft → fetched → connected → locked。

    測試裡需要這個 helper，是因為「只有 Locked 的線路能綁定」現在是硬規則
    （NOTE-023）。用 helper 而非在 registry 開一個測試專用的捷徑：
    捷徑會讓測試繞過的正是要被測的那條路。
    """

    def _lock(registry, connection_id: str, model_profile_id: str):
        from pcmef.llm.registry import Lifecycle

        registry.select_model(connection_id, model_profile_id)
        registry.set_lifecycle(connection_id, Lifecycle.FETCHED)
        registry.set_lifecycle(connection_id, Lifecycle.CONNECTED)
        return registry.set_lifecycle(connection_id, Lifecycle.LOCKED)

    return _lock


@pytest.fixture
def prompts_dir(tmp_path):
    """四份 agent prompt 檔。內容是佔位字串，因為 prompt 的研究內容屬 M6，
    而 snapshot 只對它取雜湊 —— 測的是「有沒有、變沒變」，不是寫得好不好。"""
    folder = tmp_path / "prompts"
    folder.mkdir()
    for name in (
        "observation_agent", "physics_agent",
        "visual_semantic_agent", "arbitration_agent",
    ):
        (folder / f"{name}.md").write_text(f"# {name} prompt v0\n", encoding="utf-8")
    return folder


@pytest.fixture
def decided_config(tmp_path):
    """把兩項 agent 待裁決值填好的設定。

    刻意疊在 configs/base.yaml 之上而不是另建一份完整設定：
    這樣「shipped base.yaml 仍然 formal-blocking」這件事不會被測試悄悄改掉，
    而測試又能驗證裁決之後 snapshot 確實變成可凍結。
    """
    from pcmef.core.config import load_config

    overlay = tmp_path / "decided.yaml"
    overlay.write_text(
        "agents:\n"
        "  representation_mode: FIXED_SUMMARY\n"
        "  retry:\n"
        "    max_attempts: 3\n",
        encoding="utf-8",
    )
    return load_config(["configs/base.yaml", overlay])


@dataclass(frozen=True)
class SeededRegistry:
    """一個已建好 connection 與三個 model profile 的 registry。"""

    registry: LLMRegistry
    connection_id: str
    full_model_id: str
    chat_only_id: str
    embed_only_id: str


@pytest.fixture
def seeded(registry, stub_adapter, vault) -> SeededRegistry:
    connection = registry.add_connection(
        name="Stub Formal",
        provider=STUB_PROVIDER,
        secret_ref="env:PCMEF_TEST_KEY",
        timeout_sec=30,
    )
    from pcmef.agents.provider import ConnectionProfile

    descriptors = stub_adapter.list_models(
        ConnectionProfile(
            connection_id=connection.connection_id,
            provider=STUB_PROVIDER,
            secret_ref=connection.secret_ref,
        )
    )
    registry.upsert_models(connection.connection_id, descriptors)
    by_model = {m.model_id: m.model_profile_id for m in registry.list_models()}
    return SeededRegistry(
        registry=registry,
        connection_id=connection.connection_id,
        full_model_id=by_model["full-model"],
        chat_only_id=by_model["chat-only"],
        embed_only_id=by_model["embed-only"],
    )


@pytest.fixture
def admin_service(seeded, vault, adapter_factory, decided_config, prompts_dir, tmp_path):
    """與 CLI 使用的完全相同的服務層實例，只是儲存位置指向 tmp_path。"""
    from pcmef.admin.services import AdminService
    from pcmef.llm.verification import CapabilityVerifier

    return AdminService(
        registry=seeded.registry,
        vault=vault,
        adapter_factory=adapter_factory,
        verifier=CapabilityVerifier(
            registry=seeded.registry, vault=vault,
            artifact_root=tmp_path / "artifacts", adapter_factory=adapter_factory,
        ),
        config=decided_config,
        freeze_dir=tmp_path / "freeze",
        prompts_dir=prompts_dir,
    )


@pytest.fixture
def make_client(admin_service):
    """建立 Flask test client。bind_enabled 由各測試自行決定。"""
    from pcmef.admin.app import create_app

    def factory(bind_enabled: bool = True, environ: dict | None = None):
        app = create_app(
            service=admin_service, bind_enabled=bind_enabled, environ=environ or {}
        )
        app.testing = True
        return app.test_client()

    return factory


@pytest.fixture
def client(make_client):
    return make_client(bind_enabled=True)


@pytest.fixture
def csrf(client):
    """取得一個與 client session 綁定的 CSRF 權杖。

    以真的載入頁面來取得，而不是直接塞 session：這樣連「頁面有沒有把權杖
    放進表單」都一併驗到了。
    """
    import re

    html = client.get("/admin/llm-setup").get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        raise AssertionError("the admin page rendered no csrf_token field")
    return match.group(1)
