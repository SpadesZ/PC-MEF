# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.services 的 Verify 動作與 cli 的 llm model verify 呼叫；
#         經 pcmef.agents.provider 的 adapter 對外 probe，secret 由
#         pcmef.secrets.vault 解析；結果寫進 pcmef.llm.registry 的
#         llm_verification_logs，並落一份不可覆寫的 flat artifact。
# 檔案路徑: pcmef/llm/verification.py
# 產生時間: 2026-08-26 22:50 +08:00
# 版本: v0.1.0
# 功能說明: 實際去問 provider「這個模型到底能不能聊天／能不能吃圖／能不能照 schema
#           回 JSON」，把每一次詢問的結果存成證據，並據此更新該模型的可用能力。
# 模組定位: declared 能力轉成 verified 能力的唯一通道。它「不是」綁定器
#           （那在 registry.set_binding），也不決定哪些能力是必要的（那在 capabilities）。
# 主要責任:
#   1. CapabilityVerifier.verify() 依 PROBE_ORDER 逐項 probe 並彙整結果
#   2. _run_probe() 把每個 capability 對應到 adapter 的哪個方法
#   3. _write_artifact() 以 artifact_hash 命名，既有檔案內容相同即冪等、不同即拒絕
#   4. VerificationOutcome.summary() 產出人類可讀的逐項結論
#   5. probe_payload_fingerprint() 公開 probe 內容的雜湊，供稽核確認未夾帶 evidence
# 維護提醒:
#   - 不得為 verify() 增加「自訂 probe 內容」的參數。§44 規定 verification
#     payload 必須最小化且不得使用真實 formal evidence；沒有這個參數，
#     就沒有人能在趕時間時「順手拿一筆 case 來測」。
#   - 不得在 probe 失敗時仍把能力標成 verified，也不得只因 provider metadata
#     宣稱支援就跳過 probe。
#   - 不得覆寫既有的 verification artifact；內容不同就是新結果，要走新 hash 路徑。
#   - 不得把未遮蔽的 provider 回應寫進 artifact 或 DB；錯誤字串一律取自
#     ProbeResult.error_sanitized。
#   - v0.1.0 新增：首版 probe 執行器，決策見 NOTE-018。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_verification.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from pcmef.agents.provider import (
    PROBE_CHAT_PROMPT,
    PROBE_IMAGE_PNG,
    PROBE_SCHEMA,
    PROBE_STRUCTURED_PROMPT,
    PROBE_VISION_PROMPT,
    Capability,
    ConnectionProfile,
    ModelDescriptor,
    ProbeResult,
    get_adapter,
)
from pcmef.core.hash import hash_object
from pcmef.llm.capabilities import PROBE_ORDER

__all__ = [
    "VerificationOutcome",
    "CapabilityVerifier",
    "DEFAULT_ARTIFACT_ROOT",
    "probe_payload_fingerprint",
]

DEFAULT_ARTIFACT_ROOT = Path("artifacts/llm_verification")


def probe_payload_fingerprint() -> str:
    """probe 內容的 canonical hash。

    公開它是為了讓稽核能斷言「所有 probe 都用同一份固定的最小內容」——
    若哪天有人偷偷把真實 evidence 接進 probe，這個值會變。
    """
    return hash_object(
        {
            "chat_prompt": PROBE_CHAT_PROMPT,
            "structured_prompt": PROBE_STRUCTURED_PROMPT,
            "vision_prompt": PROBE_VISION_PROMPT,
            "schema": PROBE_SCHEMA,
            "image_sha256_len": len(PROBE_IMAGE_PNG),
        }
    )


@dataclass(frozen=True)
class VerificationOutcome:
    """一次 verify 的完整結果。"""

    model_profile_id: str
    model_id: str
    results: tuple[ProbeResult, ...]
    verified: tuple[Capability, ...]
    artifact_paths: tuple[Path, ...]

    def succeeded(self) -> tuple[Capability, ...]:
        return tuple(r.capability for r in self.results if r.success)

    def failed(self) -> tuple[Capability, ...]:
        return tuple(r.capability for r in self.results if not r.success)

    def summary(self) -> list[str]:
        lines = []
        for result in self.results:
            mark = "ok " if result.success else "FAIL"
            detail = result.detail if result.success else result.error_sanitized
            lines.append(f"[{mark}] {result.capability.value:<16} {detail}")
        return lines


class CapabilityVerifier:
    """對一個 model profile 執行 capability probe 並保存證據。"""

    def __init__(
        self,
        registry,
        vault,
        artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT,
        adapter_factory: Callable | None = None,
    ) -> None:
        """adapter_factory 是 (provider, resolve_secret) -> adapter。

        留這個縫是為了讓離線的 stub provider 走完全相同的程式路徑，
        而不是在測試裡另外拼一條捷徑 —— 捷徑會讓被測的東西不是被用的東西。
        """
        self.registry = registry
        self.vault = vault
        self.artifact_root = Path(artifact_root)
        self._adapter_factory = adapter_factory or get_adapter

    # -- 對外 -------------------------------------------------------------

    def verify(
        self,
        model_profile_id: str,
        capabilities: Iterable[Capability] | None = None,
    ) -> VerificationOutcome:
        """依 PROBE_ORDER 執行 probe。capabilities 為 None 時跑必要的三項。

        預設不含 embedding：§41 明訂 Thesis Core 不需要 embedding，
        每跑一次 probe 都是一次真實的 API 呼叫與費用。
        """
        model = self.registry.get_model_profile(model_profile_id)
        connection = self.registry.get_connection(model.connection_id)
        adapter = self._adapter_factory(connection.provider, self.vault.resolve)

        profile = ConnectionProfile(
            connection_id=connection.connection_id,
            provider=connection.provider,
            secret_ref=connection.secret_ref,
            base_url=connection.base_url,
            timeout_sec=connection.timeout_sec,
        )
        descriptor = ModelDescriptor(
            model_id=model.model_id,
            provider=connection.provider,
            display_name=model.display_name,
            declared_capabilities=model.declared_capabilities,
            provider_revision=model.provider_revision,
        )

        wanted = (
            tuple(Capability.parse(c) for c in capabilities)
            if capabilities is not None
            else tuple(c for c in PROBE_ORDER if c is not Capability.EMBEDDING)
        )
        ordered = tuple(c for c in PROBE_ORDER if c in set(wanted))

        results: list[ProbeResult] = []
        artifacts: list[Path] = []
        verified: tuple[Capability, ...] = model.verified_capabilities
        for capability in ordered:
            result = self._run_probe(adapter, profile, descriptor, capability)
            results.append(result)
            verified = self.registry.record_verification(model_profile_id, result)
            artifacts.append(self._write_artifact(model_profile_id, result))

        return VerificationOutcome(
            model_profile_id=model_profile_id,
            model_id=model.model_id,
            results=tuple(results),
            verified=verified,
            artifact_paths=tuple(artifacts),
        )

    # -- 內部 -------------------------------------------------------------

    @staticmethod
    def _run_probe(adapter, profile, descriptor, capability: Capability) -> ProbeResult:
        """把 capability 對應到 adapter 的方法。probe 內容一律來自固定常數。"""
        if capability is Capability.CHAT:
            return adapter.verify_chat(profile, descriptor)
        if capability is Capability.STRUCTURED_JSON:
            return adapter.verify_structured_output(profile, descriptor, PROBE_SCHEMA)
        if capability is Capability.VISION:
            return adapter.verify_vision(profile, descriptor, PROBE_IMAGE_PNG)
        if capability is Capability.EMBEDDING:
            return adapter.verify_embedding(profile, descriptor)
        return ProbeResult(
            capability=capability,
            success=False,
            error_sanitized=(
                f"{capability.value} carries no formal verification requirement "
                "(SRC-SAI §44) and has no probe implementation"
            ),
        )

    def _write_artifact(self, model_profile_id: str, result: ProbeResult) -> Path:
        """把 probe 結果落成 flat artifact。

        檔名帶 artifact_hash：內容不同必然落到不同路徑，因此「不得覆寫
        immutable artifact」（§49）在檔名層就成立，不必額外靠紀律。
        """
        digest = result.artifact_hash()
        folder = self.artifact_root / model_profile_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{result.capability.value}-{digest[:16]}.json"
        payload = {
            "model_profile_id": model_profile_id,
            "artifact_hash": digest,
            "probe_payload_fingerprint": probe_payload_fingerprint(),
            **result.to_artifact(),
        }
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("artifact_hash") == digest:
                return path
            raise RuntimeError(
                f"verification artifact {path} already exists with different content; "
                "immutable artifacts must not be overwritten (SRC-SAI §49)"
            )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path
