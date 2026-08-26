# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；在 tmp_path 完整跑一次 LLM admin 流程
#         （建連線、驗能力、改綁、產快照、寫 agent cache），
#         然後逐位元組掃描該次執行產生的每一個檔案；不對外連線。
# 檔案路徑: tests/secret/test_global_secret_scan.py
# 產生時間: 2026-08-27 05:05 +08:00
# 版本: v0.1.0
# 功能說明: 執行 §51 的 LLM-SEC-01 —— 把一次真實流程產生的所有落盤內容
#           （SQLite、manifest、報告、快照、cache、log）全部掃過，
#           確認裡面找不到 API key 的任何片段。
# 模組定位: NFR-08 與 LLM-SEC-01 的最終防線。它「不是」單元測試 ——
#           價值在於掃的是整批真實產物，而不是某個函式的回傳值。
# 主要責任:
#   1. _run_full_flow() 產生一整批落盤內容
#   2. test_llm_sec_01_no_artifact_contains_the_secret 逐檔逐片段掃描
#   3. test_the_scanner_catches_a_planted_secret 反證掃描器有效
#   4. test_the_vault_is_the_only_file_that_can_decrypt_the_key 界定唯一例外
# 維護提醒:
#   - 不得把掃描範圍縮小成「只掃 JSON」；LLM-SEC-01 明訂 DB plaintext 也在內，
#     而 SQLite 檔是二進位，必須以位元組比對。
#   - 不得放寬片段長度；只比對完整字串會讓「存了一半的 key」通過。
#   - 不得把 vault 檔排除在掃描之外而不解釋；它之所以是例外，
#     是因為裡面只有密文，而本檔以「明文不得出現」為斷言，密文自然通過。
#   - v0.1.0 新增：首版全域掃描，對應 §51 LLM-SEC-01。
# 驗證方式:
#   - py -3.10 -m pytest tests/secret/test_global_secret_scan.py -v
# ------------------------------------------------------------

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pcmef.admin.services import AdminService
from pcmef.agents.cache import AGENT_ARTIFACT_NAMES, AgentArtifactCache, AgentBundle, AgentCacheKey
from pcmef.agents.provider import Capability, ModelDescriptor, ProbeResult, StubOfflineAdapter
from pcmef.core.config import load_config
from pcmef.core.logging_setup import setup_logging
from pcmef.llm.capabilities import FORMAL_TASK_CODES, required_capabilities
from pcmef.llm.registry import LLMRegistry
from pcmef.llm.snapshot import build_runtime_snapshot
from pcmef.secrets.vault import SecretVault

#: 這次流程要保護的 secret。刻意做成沒有重複子字串的長字串，
#: 讓「任意 8 字元片段」的比對不會誤中一般文字。
SECRET = "sk-Zq7Z4vLx9Wm2Kd6Bn1Rt8Yp3Hs5Jc0Ge"

FRAGMENT = 8


def _fragments(secret: str) -> list[str]:
    return [secret[i : i + FRAGMENT] for i in range(len(secret) - FRAGMENT + 1)]


def _run_full_flow(root: Path, log_path: Path) -> list[Path]:
    """完整跑一次 admin 流程，回傳這次產生的所有檔案。"""
    setup_logging(log_file=log_path)
    logging.getLogger("pcmef.test").info("about to use credential %s", SECRET)

    vault = SecretVault(
        path=root / "vault.json", master_key="global-scan-master", environ={}
    )
    registry = LLMRegistry(root / "llm_admin.db")

    def factory(provider, resolve_secret):
        adapter = StubOfflineAdapter(resolve_secret, environ={})
        adapter.catalogue = {
            "scan-model": (
                Capability.CHAT, Capability.VISION, Capability.STRUCTURED_JSON
            )
        }
        return adapter

    service = AdminService(
        registry=registry, vault=vault, adapter_factory=factory,
        config=load_config(["configs/base.yaml"]),
        freeze_dir=root / "freeze", artifact_root=root / "verification",
    )

    # 1. 以明文 key 建立連線 —— 這正是 key 唯一一次出現在記憶體中的入口。
    connection = service.add_connection(
        name="Scanned", provider="google", api_key=SECRET
    )
    logging.getLogger("pcmef.test").info(
        "created %s with %s", connection.name, connection.secret_fingerprint
    )

    # 2. 抓模型、驗能力（會寫 verification log 與 artifact）。
    registry.upsert_models(
        connection.connection_id,
        [
            ModelDescriptor(
                model_id="scan-model", provider="google",
                display_name="Scan Model", provider_revision="rev-scan",
            )
        ],
    )
    profile = registry.list_models(connection.connection_id)[0]
    service.verify_model(profile.model_profile_id)

    # 2b. 走完 LAVA 的線路生命週期；只有 locked 的線路能綁定（NOTE-023）。
    service.select_model(connection.connection_id, profile.model_profile_id)
    service.test_connection(connection.connection_id)
    service.lock_connection(connection.connection_id)

    # 3. 綁四個角色，寫 binding 與 audit log。
    for task_code in FORMAL_TASK_CODES:
        registry.set_binding(
            task_code, profile.model_profile_id, required_capabilities(task_code),
            actor="scanner", reason="global secret scan",
        )

    # 4. 產出快照候選 artifact。
    snapshot = build_runtime_snapshot(
        registry, service.config, prompts_dir=root / "no-prompts"
    )
    snapshot.write(root / "snapshots")

    # 5. 寫一份 agent cache（含 manifest 與費用索引）。
    cache = AgentArtifactCache(root=root / "agents", registry=registry)
    cache.put(
        AgentCacheKey(
            evidence_hash="e" * 64, representation_mode="FIXED_SUMMARY",
            provider_model_id="scan-model", provider_revision="rev-scan",
            prompt_hashes={"observation": "p1"}, schema_hash="s" * 64,
            runtime_config_hash="r" * 64,
        ),
        AgentBundle(
            artifacts={n: {"schema_version": "v1", "stage": n} for n in AGENT_ARTIFACT_NAMES},
            provider_request_id="req-scan", token_usage=99, latency_ms=12,
        ),
    )

    # 6. 一份人類可讀報告，模擬 report 層。
    report = root / "report.txt"
    report.write_text(
        "\n".join(
            f"{v.name} {v.provider} {v.secret_ref} {v.secret_fingerprint}"
            for v in service.connection_views()
        ),
        encoding="utf-8",
    )

    logging.getLogger().handlers.clear()
    return [p for p in sorted(root.rglob("*")) if p.is_file()]


@pytest.fixture
def produced(tmp_path) -> list[Path]:
    return _run_full_flow(tmp_path / "run", tmp_path / "run" / "pcmef.log")


# ---------------------------------------------------------------------------
# LLM-SEC-01
# ---------------------------------------------------------------------------


def test_llm_sec_01_no_artifact_contains_the_secret(produced):
    """manifest / report / DB plaintext 全域掃描無 secret。"""
    assert produced, "the flow produced no files; the scan would pass vacuously"

    needles = [fragment.encode() for fragment in _fragments(SECRET)]
    offenders: list[str] = []
    for path in produced:
        blob = path.read_bytes()
        if any(needle in blob for needle in needles):
            offenders.append(path.name)

    assert not offenders, (
        f"these artifacts contain API key material: {offenders}. "
        "NFR-08 requires that the key never reach repo, manifest or log."
    )


def test_the_scan_actually_covered_every_store(produced):
    """反證：掃描範圍必須真的涵蓋 DB、artifact、快照、cache、log 與報告。"""
    names = {path.name for path in produced}
    suffixes = {path.suffix for path in produced}

    assert "llm_admin.db" in names, "the SQLite registry was not scanned"
    assert "report.txt" in names, "the report layer was not scanned"
    assert "pcmef.log" in names, "the log was not scanned"
    assert any(path.name == "manifest.json" for path in produced), (
        "the agent cache manifest was not scanned"
    )
    assert any("runtime_snapshot_" in path.name for path in produced), (
        "the runtime snapshot candidate was not scanned"
    )
    assert ".json" in suffixes


def test_the_scanner_catches_a_planted_secret(produced, tmp_path):
    """稽核器的反證：把 key 種進其中一個 artifact，掃描必須抓到。

    沒有這一條，上面那條測試在掃描邏輯壞掉時會靜靜地永遠通過。
    """
    planted = tmp_path / "run" / "planted.json"
    planted.write_text(f'{{"api_key": "{SECRET}"}}', encoding="utf-8")

    needles = [fragment.encode() for fragment in _fragments(SECRET)]
    hits = [
        path.name
        for path in sorted((tmp_path / "run").rglob("*"))
        if path.is_file() and any(needle in path.read_bytes() for needle in needles)
    ]

    assert hits == ["planted.json"]


def test_partial_leakage_is_also_caught(tmp_path):
    """只遮到一半的 key 同樣是外洩；比對片段而非完整字串才擋得住。"""
    half = tmp_path / "half.json"
    half.write_text(f'{{"key": "{SECRET[:20]}[REDACTED]"}}', encoding="utf-8")

    needles = [fragment.encode() for fragment in _fragments(SECRET)]

    assert any(needle in half.read_bytes() for needle in needles)


def test_the_vault_holds_ciphertext_not_the_key(produced):
    """vault 是唯一保存憑證的地方，而它保存的是密文。"""
    vault_files = [p for p in produced if p.name == "vault.json"]
    assert vault_files, "the flow should have created a vault"
    raw = vault_files[0].read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "ciphertext" in raw
