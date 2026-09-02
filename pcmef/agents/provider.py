# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.llm.verification（capability probe）、pcmef.llm.registry
#         （fetch models）與未來的 agents.observation/physics/visual_semantic/
#         arbitration 呼叫；secret 由 pcmef.secrets.vault 解析後傳入；
#         對外發出 HTTPS 請求，回傳一律是本檔定義的 normalized 型別。
# 檔案路徑: pcmef/agents/provider.py
# 產生時間: 2026-08-26 21:35 +08:00
# 版本: v0.1.0
# 功能說明: 定義「本系統怎麼跟外部 LLM 說話」的統一介面。不論背後是 Google、
#           OpenAI 還是相容端點，上層拿到的都是同一組欄位；連測連線用的
#           最小 probe 內容也固定在這裡，避免各處自己編一份。
# 模組定位: provider 的唯一出入口與 anti-corruption layer。UI、Task Engine 與
#           probe 一律只依賴本檔的 normalized 型別，禁止直接碰各家 SDK 或原始欄位。
# 主要責任:
#   1. Capability / ModelDescriptor / ProbeResult / ProviderResponse 定義 normalized 契約
#   2. LLMProviderAdapter Protocol 宣告六個方法，對應 SRC-SAI 介面草案 4
#   3. HTTPProviderAdapter._request() 實作不含 URL、不含 key 的錯誤處理
#   4. GoogleAdapter 以 x-goog-api-key header 傳送金鑰，不走 query string
#   5. OpenAICompatibleAdapter 服務 openai / openrouter / custom_openai_compatible
#   6. StubOfflineAdapter 提供無網路的可測路徑，且被排除在 formal 之外
#   7. _parse_stub_catalogue() 讓離線目錄可由 PCMEF_STUB_MODELS 宣告
#   8. PROBE_* 常數固定最小 probe 內容，確保 probe 不使用任何真實 evidence
# 維護提醒:
#   - 不得呼叫 response.raise_for_status()。它產生的訊息含完整 request URL，
#     一旦有人把 key 放回 query string，key 就會被帶進例外並流入
#     llm_verification_logs 與 error artifact（NOTE-007）。
#   - 不得把 API key 放進 URL query string；一律走 request header。
#   - 不得在例外訊息中夾帶 request URL。本檔的錯誤只帶 provider、狀態碼與
#     已遮蔽的回應片段；靠 regex 事後洗 URL 是 NOTE-007 那個 bug 的成因。
#   - 不得讓 probe 使用任何真實 formal evidence（§44）；probe 內容只能是
#     本檔的 PROBE_* 常數。
#   - 不得把 StubOfflineAdapter 加進 FORMAL_ELIGIBLE_PROVIDERS；那會讓
#     一個假的 provider 有機會出現在 formal run 的 llm_runtime.lock 裡。
#   - v0.1.0 新增：首版 normalized contract，決策見 NOTE-017。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_provider_contract.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import struct
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pcmef.core.hash import hash_object
from pcmef.core.logging_setup import redact

__all__ = [
    "ProviderError",
    "Capability",
    "ConnectionProfile",
    "ModelDescriptor",
    "ProbeResult",
    "ProviderResponse",
    "LLMProviderAdapter",
    "GoogleAdapter",
    "OpenAICompatibleAdapter",
    "StubOfflineAdapter",
    "PROVIDER_ADAPTERS",
    "FORMAL_ELIGIBLE_PROVIDERS",
    "EVIDENCE_IMAGES_KEY",
    "get_adapter",
    "PROBE_CHAT_PROMPT",
    "PROBE_STRUCTURED_PROMPT",
    "PROBE_VISION_PROMPT",
    "PROBE_IMAGE_PNG",
    "PROBE_SCHEMA",
]


class ProviderError(RuntimeError):
    """provider 回應非成功狀態、無法解析，或連線失敗。

    訊息刻意只帶 provider 名稱、HTTP 狀態碼與已遮蔽的回應片段。
    不帶 request URL —— 見 NOTE-007。
    """


class Capability(str, Enum):
    """§44 的能力清單。chat / structured_json / vision 為 Thesis Core 所需。"""

    CHAT = "chat"
    VISION = "vision"
    STRUCTURED_JSON = "structured_json"
    EMBEDDING = "embedding"
    TOOL_CALL = "tool_call"
    STREAM = "stream"

    @classmethod
    def parse(cls, value: str | Capability) -> Capability:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            raise ProviderError(
                f"unknown capability {value!r}; expected one of "
                f"{[c.value for c in cls]}"
            ) from None


# ---------------------------------------------------------------------------
# 固定 probe 內容
# ---------------------------------------------------------------------------
# §44：Verification payload 必須最小化，不得把真實 formal evidence 拿來測連線。
# 因此 probe 內容固定在此，任何呼叫端都不得改成「順手拿一筆 case 來測」。

PROBE_CHAT_PROMPT = "Reply with the single word: ready"

PROBE_STRUCTURED_PROMPT = (
    "Return one JSON object with exactly these keys: "
    'probe_version (the string "v1") and ok (the boolean true). No prose.'
)

PROBE_VISION_PROMPT = "Answer with one word: how many distinct colours are in this image?"

#: structured_json probe 用的最小 schema。刻意不用研究用的三份 agent schema ——
#: 那些 schema 的欄位語意屬於研究設計，拿來測連線會讓「連線可用」與
#: 「模型能產出合格 ObservationBrief」兩件事混在一起。
PROBE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "pcmef_capability_probe_v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["probe_version", "ok"],
    "properties": {
        "probe_version": {"const": "v1"},
        "ok": {"type": "boolean"},
    },
}


def _minimal_png(width: int = 8, height: int = 8) -> bytes:
    """產生一張確定性的純色 PNG，供 vision probe 使用。

    以程式建構而非內嵌 base64 字串：一段不可讀的 blob 沒辦法被稽核，
    而這張圖的 hash 會進 capability_probe_artifact_hashes 並最終進 lock。
    """
    raw = b"".join(b"\x00" + bytes([64, 128, 192] * width) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


PROBE_IMAGE_PNG: bytes = _minimal_png()


# ---------------------------------------------------------------------------
# Normalized 型別
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionProfile:
    """一個 provider 連線的 normalized 描述。刻意不含 secret 值，只含 secret_ref。"""

    connection_id: str
    provider: str
    secret_ref: str
    base_url: str = ""
    timeout_sec: int = 30

    def __post_init__(self) -> None:
        if not self.connection_id:
            raise ProviderError("connection_id must not be empty")
        if self.timeout_sec <= 0:
            raise ProviderError(
                f"timeout_sec must be positive, got {self.timeout_sec!r}"
            )


@dataclass(frozen=True)
class ModelDescriptor:
    """一個模型的 normalized 描述。

    declared_capabilities 只來自 provider metadata，**不等於**可用能力；
    §44 要求 declared 與 probe-verified 分開，task binding 只認後者。
    """

    model_id: str
    provider: str
    display_name: str = ""
    declared_capabilities: tuple[Capability, ...] = ()
    provider_revision: str = ""

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ProviderError("model_id must not be empty")

    def to_row(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "display_name": self.display_name,
            "declared_capabilities": [c.value for c in self.declared_capabilities],
            "provider_revision": self.provider_revision,
        }


@dataclass(frozen=True)
class ProbeResult:
    """一次 capability probe 的結果。error 欄位必須已遮蔽。"""

    capability: Capability
    success: bool
    latency_ms: int = 0
    detail: str = ""
    error_sanitized: str = ""
    probed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_artifact(self) -> dict[str, Any]:
        """probe 的 flat artifact 內容。刻意不含 provider 原始回應全文。"""
        return {
            "capability": self.capability.value,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "error_sanitized": self.error_sanitized,
            "probed_at": self.probed_at,
        }

    def artifact_hash(self) -> str:
        payload = self.to_artifact()
        # probed_at 與 latency 不進 hash：同一個 probe 在不同時間跑出的結論相同時
        # 應得到相同 hash，否則 capability_probe_artifact_hashes 每跑一次就變，
        # llm_runtime.lock 會被迫跟著變而失去「identity 沒變」的意義。
        payload.pop("probed_at")
        payload.pop("latency_ms")
        return hash_object(payload)


@dataclass(frozen=True)
class ProviderResponse:
    """一次 invoke 的 normalized 回應。"""

    text: str
    model_id: str
    provider: str
    provider_revision: str = ""
    request_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: reasoning/thinking token。**與 completion 分開存但一律照 output 計費**
    #: （Google 明訂 thinking tokens 併入 output pricing）。分開存是因為
    #: 它是成本估算誤差最大的一項：prompt 大小可以從 payload 推得，
    #: thinking 只能實測。
    thoughts_tokens: int = 0
    latency_ms: int = 0

    def billable_output_tokens(self) -> int:
        """計費用的 output token = completion + thinking。"""
        return self.completion_tokens + self.thoughts_tokens

    def usage(self) -> dict[str, int]:
        return {
            "thoughts_tokens": self.thoughts_tokens,
            "billable_output_tokens": self.billable_output_tokens(),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
        }


@runtime_checkable
class LLMProviderAdapter(Protocol):
    """SRC-SAI 介面草案 4。

    與規格草案的唯一差異：每個方法都顯式接受 ConnectionProfile。
    草案寫成 verify_chat(self, model)，但 model 本身不帶端點與憑證，
    真要呼叫仍得從某處取得 connection；顯式傳入比藏在 adapter 狀態裡好稽核。
    """

    provider: str

    def list_models(self, connection: ConnectionProfile) -> list[ModelDescriptor]: ...

    def verify_chat(
        self, connection: ConnectionProfile, model: ModelDescriptor
    ) -> ProbeResult: ...

    def verify_vision(
        self, connection: ConnectionProfile, model: ModelDescriptor, image: bytes
    ) -> ProbeResult: ...

    def verify_structured_output(
        self,
        connection: ConnectionProfile,
        model: ModelDescriptor,
        schema: dict[str, Any],
    ) -> ProbeResult: ...

    def verify_embedding(
        self, connection: ConnectionProfile, model: ModelDescriptor
    ) -> ProbeResult: ...

    def invoke(
        self,
        connection: ConnectionProfile,
        model: ModelDescriptor,
        task_code: str,
        payload: dict[str, Any],
        runtime_cfg: dict[str, Any],
    ) -> ProviderResponse: ...


# ---------------------------------------------------------------------------
# HTTP 基底
# ---------------------------------------------------------------------------

_ERROR_BODY_CHARS = 200

#: 從錯誤回應裡額外抽出的**結構化**欄位。
#:
#: body 截在 200 字元是為了 LLM-SEC-01：provider 可能原樣回吐我們送出的內容，
#: 包含憑證。但那個上限也把 429 最有用的兩件事切掉了 —— 是哪一個配額爆了、
#: 該等多久。實測結果是「等 30 秒重跑」與「等 24 小時」在訊息上長得一模一樣。
#: 因此改為額外抽出少數具名欄位，且**一樣過 redact()**：
#: 上限保護的是自由文字，這些是 provider 定義的狀態欄位。
_ERROR_FACT_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("status", ("error", "status")),
    ("reason", ("error", "details", 0, "reason")),
    ("retry_after", ("error", "details", 0, "retryDelay")),
)


def _dig(node: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and isinstance(key, int) and key < len(node):
            node = node[key]
        else:
            return None
    return node


def _google_usage(body: dict[str, Any]) -> dict[str, int]:
    """從 Google 的 usageMetadata 取出計費用的 token 數。

    `thoughtsTokenCount` 必須單獨取出：Google 把 thinking token 併入
    **output** 計費，但不把它算進 `candidatesTokenCount`，因此只看
    candidates 會系統性低估成本。取不到就回 0 —— 少記一筆用量不該讓
    一次 formal run 中止。
    """
    usage = body.get("usageMetadata") or {}

    def count(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    return {
        "prompt_tokens": count("promptTokenCount"),
        "completion_tokens": count("candidatesTokenCount"),
        "thoughts_tokens": count("thoughtsTokenCount"),
    }


def _openai_usage(body: dict[str, Any]) -> dict[str, int]:
    """OpenAI 相容端點的用量。reasoning token 藏在 completion_tokens_details。"""
    usage = body.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}

    def count(node: dict[str, Any], key: str) -> int:
        value = node.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    reasoning = count(details, "reasoning_tokens")
    completion = count(usage, "completion_tokens")
    # OpenAI 的 completion_tokens **已含** reasoning，Google 的沒有。
    # 兩邊都要讓 completion + thoughts 等於實際計費 output，因此這裡扣掉。
    return {
        "prompt_tokens": count(usage, "prompt_tokens"),
        "completion_tokens": max(completion - reasoning, 0),
        "thoughts_tokens": reasoning,
    }


def _error_facts(response: Any) -> str:
    """抽出可判斷「重跑會不會有用」的結構化欄位。抽不到就回空字串。

    另外掃過 details 陣列找 RetryInfo / QuotaFailure —— Google 的錯誤把它們
    放在不固定的索引，只看 details[0] 會漏掉。
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 錯誤路徑不得再拋錯
        return ""
    facts: dict[str, str] = {}
    for name, path in _ERROR_FACT_PATHS:
        value = _dig(body, path)
        if value is not None:
            facts[name] = redact(str(value))[:80]
    for detail in (_dig(body, ("error", "details")) or []):
        if not isinstance(detail, dict):
            continue
        kind = str(detail.get("@type", "")).rsplit(".", 1)[-1]
        if kind == "RetryInfo" and detail.get("retryDelay"):
            facts["retry_after"] = redact(str(detail["retryDelay"]))[:80]
        if kind == "QuotaFailure":
            violations = detail.get("violations") or []
            if violations and isinstance(violations[0], dict):
                quota_id = violations[0].get("quotaId") or violations[0].get("subject")
                if quota_id:
                    facts["quota_id"] = redact(str(quota_id))[:80]
    if not facts:
        return ""
    return " ".join(f"{k}={v}" for k, v in sorted(facts.items())) + "; "


class HTTPProviderAdapter:
    """以 httpx 呼叫 REST 端點的 adapter 基底。

    子類別只需提供 URL 組法、header 組法與回應解析；錯誤處理一律走
    本類別的 _request()，以免每個 adapter 各自重寫一次 NOTE-007 的坑。
    """

    provider: str = ""
    default_base_url: str = ""

    def __init__(self, resolve_secret) -> None:
        """resolve_secret 是 (secret_ref) -> str，通常綁定 SecretVault.resolve。"""
        self._resolve_secret = resolve_secret

    # -- 內部 -------------------------------------------------------------

    def _base_url(self, connection: ConnectionProfile) -> str:
        return (connection.base_url or self.default_base_url).rstrip("/")

    def _request(
        self,
        connection: ConnectionProfile,
        method: str,
        path: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """送出請求並回傳 (解析後的 JSON, 耗時毫秒)。

        NOTE(NOTE-007): 這裡刻意不呼叫 response.raise_for_status()。
        該方法產生的 HTTPStatusError 訊息含完整 request URL；只要有人日後把
        key 移回 query string，key 就會被帶進例外，流入
        llm_verification_logs.error_sanitized 與 formal run 的 error artifact，
        直接讓 LLM-SEC-01 FAIL。改為自行檢查狀態碼並自組不含 URL 的訊息。
        """
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - 取決於安裝環境
            raise ProviderError(
                "the HTTP provider adapters require httpx "
                "(pip install -e \".[agents]\")"
            ) from error

        url = f"{self._base_url(connection)}{path}"
        started = time.monotonic()
        try:
            response = httpx.request(
                method, url, headers=headers, json=json_body,
                timeout=connection.timeout_sec,
            )
        except Exception as error:
            # 連線層例外也可能帶 URL，因此只保留例外類別名稱。
            raise ProviderError(
                f"{self.provider}: transport failure ({type(error).__name__}) "
                f"after {int((time.monotonic() - started) * 1000)} ms"
            ) from None
        latency_ms = int((time.monotonic() - started) * 1000)

        # provider 的回應內容由對方決定，可能原樣回吐我們送出的憑證，
        # 而這段訊息會被寫進 llm_verification_logs.error_sanitized——
        # 屬於 LLM-SEC-01 的掃描範圍。因此在組訊息時就清洗，不是等 log filter。
        excerpt = redact(response.text[:_ERROR_BODY_CHARS])

        if response.status_code >= 400:
            raise ProviderError(
                f"{self.provider}: HTTP {response.status_code}; "
                f"{_error_facts(response)}"
                f"body[:{_ERROR_BODY_CHARS}]={excerpt!r}"
            )
        try:
            return response.json(), latency_ms
        except ValueError:
            raise ProviderError(
                f"{self.provider}: HTTP {response.status_code} but the body is not "
                f"JSON; body[:{_ERROR_BODY_CHARS}]={excerpt!r}"
            ) from None

    def _probe(self, capability: Capability, run) -> ProbeResult:
        """統一的 probe 包裝：成功記 detail，失敗記已遮蔽的錯誤。"""
        started = time.monotonic()
        try:
            detail = run()
        except ProviderError as error:
            return ProbeResult(
                capability=capability,
                success=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_sanitized=str(error),
            )
        return ProbeResult(
            capability=capability,
            success=True,
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=detail,
        )


# ---------------------------------------------------------------------------
# Google / Gemini
# ---------------------------------------------------------------------------


class GoogleAdapter(HTTPProviderAdapter):
    """Google Generative Language API。"""

    provider = "google"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _headers(self, connection: ConnectionProfile) -> dict[str, str]:
        # NOTE(NOTE-007): key 走 x-goog-api-key header，不走 ?key= query string。
        # query string 會出現在 URL 上，而 URL 會出現在各種例外、log 與代理紀錄裡。
        return {
            "x-goog-api-key": self._resolve_secret(connection.secret_ref),
            "content-type": "application/json",
        }

    def list_models(self, connection: ConnectionProfile) -> list[ModelDescriptor]:
        body, _ = self._request(connection, "GET", "/models", self._headers(connection))
        models: list[ModelDescriptor] = []
        for entry in body.get("models", []):
            name = str(entry.get("name", "")).split("/")[-1]
            if not name:
                continue
            methods = set(entry.get("supportedGenerationMethods", []))
            declared: list[Capability] = []
            if "generateContent" in methods:
                declared.append(Capability.CHAT)
            if "embedContent" in methods:
                declared.append(Capability.EMBEDDING)
            models.append(
                ModelDescriptor(
                    model_id=name,
                    provider=self.provider,
                    display_name=str(entry.get("displayName", "")),
                    declared_capabilities=tuple(declared),
                    provider_revision=str(entry.get("version", "")),
                )
            )
        return models

    def _generate(
        self,
        connection: ConnectionProfile,
        model: ModelDescriptor,
        parts: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> tuple[str, int, dict[str, int]]:
        payload: dict[str, Any] = {"contents": [{"parts": parts}]}
        config: dict[str, Any] = {}
        if response_schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = response_schema
        if temperature is not None:
            # Google 沒有 OpenAI 那種 strict 旗標；responseSchema 本身就是強制的。
            # 溫度是這裡唯一能壓低取樣隨機性的旋鈕。
            config["temperature"] = float(temperature)
        if config:
            payload["generationConfig"] = config
        body, latency = self._request(
            connection, "POST", f"/models/{model.model_id}:generateContent",
            self._headers(connection), payload,
        )
        candidates = body.get("candidates") or []
        if not candidates:
            raise ProviderError(f"{self.provider}: response carried no candidates")
        text = "".join(
            str(part.get("text", ""))
            for part in candidates[0].get("content", {}).get("parts", [])
        )
        return text, latency, _google_usage(body)

    def verify_chat(self, connection, model) -> ProbeResult:
        def run() -> str:
            text, _, _ = self._generate(connection, model, [{"text": PROBE_CHAT_PROMPT}])
            if not text.strip():
                raise ProviderError(f"{self.provider}: chat probe returned empty text")
            return f"non-empty response ({len(text)} chars)"

        return self._probe(Capability.CHAT, run)

    def verify_vision(self, connection, model, image: bytes) -> ProbeResult:
        import base64

        def run() -> str:
            text, _, _ = self._generate(
                connection, model,
                [
                    {"text": PROBE_VISION_PROMPT},
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64.b64encode(image).decode("ascii"),
                        }
                    },
                ],
            )
            if not text.strip():
                raise ProviderError(f"{self.provider}: vision probe returned empty text")
            return f"accepted image+text ({len(image)} byte png)"

        return self._probe(Capability.VISION, run)

    def verify_structured_output(self, connection, model, schema) -> ProbeResult:
        def run() -> str:
            text, _, _ = self._generate(
                connection, model, [{"text": PROBE_STRUCTURED_PROMPT}],
                response_schema=_to_google_schema(schema),
            )
            return _validate_structured_probe(text, schema)

        return self._probe(Capability.STRUCTURED_JSON, run)

    def verify_embedding(self, connection, model) -> ProbeResult:
        def run() -> str:
            body, _ = self._request(
                connection, "POST", f"/models/{model.model_id}:embedContent",
                self._headers(connection),
                {"content": {"parts": [{"text": PROBE_CHAT_PROMPT}]}},
            )
            values = (body.get("embedding") or {}).get("values") or []
            return _validate_embedding(values)

        return self._probe(Capability.EMBEDDING, run)

    def invoke(self, connection, model, task_code, payload, runtime_cfg) -> ProviderResponse:
        parts = _payload_to_parts(payload)
        schema = runtime_cfg.get("response_schema")
        text, latency, usage = self._generate(
            connection, model, parts,
            response_schema=_to_google_schema(schema) if schema else None,
            temperature=runtime_cfg.get("temperature"),
        )
        return ProviderResponse(
            text=text, model_id=model.model_id, provider=self.provider, **usage,
            provider_revision=model.provider_revision, latency_ms=latency,
        )


# ---------------------------------------------------------------------------
# OpenAI 相容
# ---------------------------------------------------------------------------


class OpenAICompatibleAdapter(HTTPProviderAdapter):
    """OpenAI / OpenRouter / 任何 /v1/chat/completions 相容端點。"""

    provider = "openai"
    default_base_url = "https://api.openai.com/v1"

    def _headers(self, connection: ConnectionProfile) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._resolve_secret(connection.secret_ref)}",
            "content-type": "application/json",
        }

    def list_models(self, connection: ConnectionProfile) -> list[ModelDescriptor]:
        body, _ = self._request(connection, "GET", "/models", self._headers(connection))
        return [
            ModelDescriptor(
                model_id=str(entry.get("id", "")),
                provider=self.provider,
                display_name=str(entry.get("id", "")),
                provider_revision=str(entry.get("created", "")),
            )
            for entry in body.get("data", [])
            if entry.get("id")
        ]

    def _chat(
        self,
        connection: ConnectionProfile,
        model: ModelDescriptor,
        content: Any,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> tuple[str, int, dict[str, int]]:
        payload: dict[str, Any] = {
            "model": model.model_id,
            "messages": [{"role": "user", "content": content}],
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if temperature is not None:
            payload["temperature"] = float(temperature)
        body, latency = self._request(
            connection, "POST", "/chat/completions", self._headers(connection), payload
        )
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.provider}: response carried no choices")
        return (
            str(choices[0].get("message", {}).get("content", "")),
            latency,
            _openai_usage(body),
        )

    def verify_chat(self, connection, model) -> ProbeResult:
        def run() -> str:
            text, _, _ = self._chat(connection, model, PROBE_CHAT_PROMPT)
            if not text.strip():
                raise ProviderError(f"{self.provider}: chat probe returned empty text")
            return f"non-empty response ({len(text)} chars)"

        return self._probe(Capability.CHAT, run)

    def verify_vision(self, connection, model, image: bytes) -> ProbeResult:
        import base64

        def run() -> str:
            encoded = base64.b64encode(image).decode("ascii")
            text, _, _ = self._chat(
                connection, model,
                [
                    {"type": "text", "text": PROBE_VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            )
            if not text.strip():
                raise ProviderError(f"{self.provider}: vision probe returned empty text")
            return f"accepted image+text ({len(image)} byte png)"

        return self._probe(Capability.VISION, run)

    def verify_structured_output(self, connection, model, schema) -> ProbeResult:
        def run() -> str:
            text, _, _ = self._chat(
                connection, model, PROBE_STRUCTURED_PROMPT,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": str(schema.get("title", "probe")),
                        "schema": _to_openai_schema(schema),
                        "strict": True,
                    },
                },
            )
            return _validate_structured_probe(text, schema)

        return self._probe(Capability.STRUCTURED_JSON, run)

    def verify_embedding(self, connection, model) -> ProbeResult:
        def run() -> str:
            body, _ = self._request(
                connection, "POST", "/embeddings", self._headers(connection),
                {"model": model.model_id, "input": PROBE_CHAT_PROMPT},
            )
            data = body.get("data") or []
            values = data[0].get("embedding", []) if data else []
            return _validate_embedding(values)

        return self._probe(Capability.EMBEDDING, run)

    def invoke(self, connection, model, task_code, payload, runtime_cfg) -> ProviderResponse:
        """影像走 image_url data URI，structured 走 response_format。

        先前這裡是 `_payload_to_text(payload)` —— 影像會被 JSON 序列化成
        文字，於是 vision agent 其實從來沒有看過圖；structured 也沒有真的
        要求 schema。兩者都是「跑得動但量錯東西」的那種錯。
        """
        schema = runtime_cfg.get("response_schema")
        response_format = (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": str(task_code),
                    "schema": _to_openai_schema(schema),
                    "strict": True,
                },
            }
            if schema
            else None
        )
        text, latency, usage = self._chat(
            connection, model, _payload_to_openai_content(payload), response_format,
            temperature=runtime_cfg.get("temperature"),
        )
        return ProviderResponse(
            text=text, model_id=model.model_id, provider=self.provider,
            provider_revision=model.provider_revision, latency_ms=latency, **usage,
        )


# ---------------------------------------------------------------------------
# 離線 stub
# ---------------------------------------------------------------------------


class StubOfflineAdapter(HTTPProviderAdapter):
    """不連網的 adapter，供 admin 流程與驗收測試使用。

    它**不是** mock 框架的替代品，而是一個顯式登記、且被 formal 明文排除的
    provider：任何要求它進 llm_runtime.lock 的嘗試都會被 snapshot 拒絕。
    這樣「離線也能把整條 admin 流程走完」與「假 provider 絕不進 formal」
    兩件事可以同時成立。
    """

    provider = "stub_offline"
    default_base_url = "stub://offline"

    #: 由環境變數宣告目錄的格式：`model_a:chat+vision+structured_json,model_b:chat`
    CATALOGUE_ENV = "PCMEF_STUB_MODELS"

    def __init__(self, resolve_secret, environ=None) -> None:
        super().__init__(resolve_secret)
        # 每個實例各自持有；宣告成 class attribute 會讓一次測試設定的目錄
        # 洩漏到同一行程內的其他實例，造成互相污染的隱性耦合。
        self.catalogue: dict[str, tuple[Capability, ...]] = _parse_stub_catalogue(
            (environ if environ is not None else os.environ).get(self.CATALOGUE_ENV, "")
        )
        self.calls: list[tuple[str, str]] = []
        #: 每次 invoke 收到什麼的完整記錄，供測試斷言影像與 schema 真的到位。
        self.invocations: list[dict[str, Any]] = []

    def _supported(self, model: ModelDescriptor) -> tuple[Capability, ...]:
        return self.catalogue.get(model.model_id, model.declared_capabilities)

    def list_models(self, connection: ConnectionProfile) -> list[ModelDescriptor]:
        # 解析一次 secret：讓「connection 沒有可用憑證」在 stub 路徑上也會失敗，
        # 否則離線流程會比真實流程寬鬆，測不出憑證缺漏。
        self._resolve_secret(connection.secret_ref)
        return [
            ModelDescriptor(
                model_id=model_id, provider=self.provider, display_name=model_id,
                declared_capabilities=caps, provider_revision="stub-r1",
            )
            for model_id, caps in sorted(self.catalogue.items())
        ]

    def _stub_probe(self, connection, model, capability: Capability) -> ProbeResult:
        self._resolve_secret(connection.secret_ref)
        self.calls.append((model.model_id, capability.value))
        if capability in self._supported(model):
            return ProbeResult(
                capability=capability, success=True, latency_ms=1,
                detail=f"stub provider reports {capability.value} available",
            )
        return ProbeResult(
            capability=capability, success=False, latency_ms=1,
            error_sanitized=(
                f"{self.provider}: model {model.model_id} does not offer "
                f"{capability.value}"
            ),
        )

    def verify_chat(self, connection, model) -> ProbeResult:
        return self._stub_probe(connection, model, Capability.CHAT)

    def verify_vision(self, connection, model, image: bytes) -> ProbeResult:
        return self._stub_probe(connection, model, Capability.VISION)

    def verify_structured_output(self, connection, model, schema) -> ProbeResult:
        return self._stub_probe(connection, model, Capability.STRUCTURED_JSON)

    def verify_embedding(self, connection, model) -> ProbeResult:
        return self._stub_probe(connection, model, Capability.EMBEDDING)

    def invoke(self, connection, model, task_code, payload, runtime_cfg) -> ProviderResponse:
        """離線回應。**會記錄它到底收到了什麼**，因此測試證明得了
        「影像真的送進 adapter」與「schema 真的被要求」，而不是只證明
        呼叫沒有拋例外。

        要求 structured 時回傳一個 schema-valid 的實例，讓
        parse + jsonschema 驗證這條路在離線也走得完整。
        """
        self._resolve_secret(connection.secret_ref)
        rest, images = _split_evidence(payload)
        schema = runtime_cfg.get("response_schema")
        self.calls.append((model.model_id, f"invoke:{task_code}"))
        self.invocations.append(
            {
                "task_code": str(task_code),
                "image_count": len(images),
                "image_bytes": sum(len(i.get("data_b64", "")) for i in images),
                "schema_requested": bool(schema),
                "schema_title": (schema or {}).get("title"),
                "temperature": runtime_cfg.get("temperature"),
                # 完整記下送進來的文字部分，讓測試能斷言角色隔離
                # 真的發生在 payload 層，而不是只寫在 prompt 裡。
                "payload": rest,
            }
        )
        text = (
            json.dumps(
                _synthesise_from_schema(schema, STUB_ROLE_HINTS.get(str(task_code))),
                ensure_ascii=False, sort_keys=True,
            )
            if schema
            else hash_object(rest)[:32]
        )
        return ProviderResponse(
            text=text, model_id=model.model_id,
            provider=self.provider, provider_revision=model.provider_revision,
            request_id=f"stub-{len(self.calls)}", latency_ms=1,
        )


# ---------------------------------------------------------------------------
# 共用工具與登錄表
# ---------------------------------------------------------------------------


def _parse_stub_catalogue(raw: str) -> dict[str, tuple[Capability, ...]]:
    """解析離線 stub 的模型目錄宣告。

    讓目錄可由環境變數提供，是為了讓「沒有任何 provider 憑證」的機器也能把
    整條 admin 流程從頭跑到尾。這條路徑仍然安全，因為 stub_offline 不在
    FORMAL_ELIGIBLE_PROVIDERS 內 —— 走得完不等於凍結得了。
    """
    catalogue: dict[str, tuple[Capability, ...]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        model_id, _, caps = entry.partition(":")
        model_id = model_id.strip()
        if not model_id:
            continue
        catalogue[model_id] = tuple(
            Capability.parse(name.strip())
            for name in caps.split("+")
            if name.strip()
        )
    return catalogue


#: stub 在合成回應時要遵守的角色契約。
#: enum 的第一個值未必是該角色該給的值 —— specialist_proposal_v1 的
#: modality enum 是 [physics, visual_semantic]，照字面取第一個會讓
#: visual_semantic_agent 自稱 physics，而那是真實模型會被擋下的違約。
#: stub 要模擬的是一個**守約**的模型，不是一個剛好通過 schema 的模型。
#:
#: 同樣的道理適用於 class_support。schema 說每一類是
#: `{"type": "number", "minimum": 0}`，照字面合成會得到四個 0 —— 那份回應
#: 通過 JSON Schema，卻表示「仲裁者什麼都沒說」，是一個真實模型會被語意
#: 檢查擋下的違約（NOTE-060）。
#:
#: 這一點先前把 all-zero 的處理整段藏了起來：離線測試從來沒有跑過
#: 「仲裁者真的給出判斷」的路徑，因為 stub 每次都回全零，而 agent layer
#: 又把全零救成 25/25/25/25。
#:
#: 值刻意不對稱且總和不為 100：不對稱讓「正規化有沒有保持相對大小」驗得出來，
#: 總和 95 讓 support_sum_before_normalisation 這個稽核欄位有東西可記。
STUB_ROLE_HINTS: dict[str, dict[str, Any]] = {
    "physics_agent": {"modality": "physics"},
    "visual_semantic_agent": {"modality": "visual_semantic"},
    "arbitration_agent": {
        "class_support": {
            "Empty": 55.0, "Water-filled": 25.0, "Bubbly": 10.0, "Misty": 5.0,
        }
    },
}


def _synthesise_from_schema(
    schema: dict[str, Any], hints: dict[str, Any] | None = None
) -> Any:
    """由 JSON Schema 造出一個**滿足該 schema** 的最小實例。

    只給 stub 用。它的用途是讓離線測試能真的跑完
    「要求 schema -> 回應 -> json.loads -> jsonschema.validate」整條路 ——
    否則離線只驗到「沒有拋例外」，而 schema 驗證那一段永遠沒被執行過。
    """
    if not isinstance(schema, dict):
        return None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or list(properties)
        return {
            name: (
                (hints or {})[name]
                if name in (hints or {})
                else _synthesise_from_schema(
                    properties.get(name, {"type": "string"}), hints
                )
            )
            for name in required
        }
    if kind == "array":
        return [_synthesise_from_schema(schema.get("items") or {"type": "string"}, hints)]
    if kind == "number":
        return float(schema.get("minimum", 0.0))
    if kind == "integer":
        return int(schema.get("minimum", 0))
    if kind == "boolean":
        return False
    return str(schema.get("pattern", "stub"))


#: 兩家 provider 都不接受的 JSON Schema meta 欄位。
_SCHEMA_META_KEYS: frozenset[str] = frozenset({"$schema", "$id", "title", "examples"})

#: Google 的 responseSchema 只吃 OpenAPI 3.0 的一個子集，additionalProperties
#: 不在其中 —— 送過去會得到：
#:   400 Unknown name "additionalProperties" at 'generation_config.response_schema'
#: 而 OpenAI 的 strict json_schema 反過來**要求**它必須是 false。
#: 兩邊需求相反，因此不能共用同一份轉換結果。
_GOOGLE_UNSUPPORTED_KEYS: frozenset[str] = frozenset({"additionalProperties"})


def _json_type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _normalise_schema(node: Any, drop: frozenset[str] = frozenset()) -> Any:
    """遞迴清掉 provider 不認得的欄位，並把 const 改寫成單值 enum。

    **必須遞迴**：三份 agent schema 的 additionalProperties 有的在頂層、
    有的在巢狀 properties 裡。只剝頂層會留下巢狀那些，而 Google 的錯誤
    只會指到 'generation_config.response_schema' 這種粗略位置，
    看不出是哪一層 —— 修一次頂層會以為修好了，其實沒有。

    const 兩家都不支援（Google 的子集沒有，OpenAI strict 也沒有），但它
    語意上等於「只有一個合法值的 enum」，所以改寫而不是丟掉：丟掉會讓
    schema_version 變成任意字串，那一欄的驗證就形同虛設。
    """
    if isinstance(node, list):
        return [_normalise_schema(item, drop) for item in node]
    if not isinstance(node, dict):
        return node

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SCHEMA_META_KEYS or key in drop:
            continue
        if key == "const":
            result["enum"] = [value]
            result.setdefault("type", _json_type_of(value))
            continue
        result[key] = _normalise_schema(value, drop)
    return result


def _to_google_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Google responseSchema：額外剝掉 additionalProperties。"""
    return _normalise_schema(schema, _GOOGLE_UNSUPPORTED_KEYS)


def _to_openai_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI strict json_schema：保留 additionalProperties，strict 模式要求它。"""
    return _normalise_schema(schema)


def _validate_structured_probe(text: str, schema: dict[str, Any]) -> str:
    """§44：structured_json 必須實際 probe，且 schema parse + validator 都要 PASS。"""
    import json

    try:
        parsed = json.loads(text)
    except ValueError:
        raise ProviderError(
            "structured probe response is not parseable JSON"
        ) from None
    try:
        import jsonschema
    except ImportError as error:  # pragma: no cover - 取決於安裝環境
        raise ProviderError(
            "structured_json verification requires jsonschema "
            "(pip install -e \".[agents]\"); refusing to mark the capability "
            "verified without actually validating"
        ) from error
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as error:
        raise ProviderError(
            f"structured probe response failed schema validation: {error.message}"
        ) from None
    return "parsed and schema-validated"


def _validate_embedding(values: Any) -> str:
    import math

    if not isinstance(values, list) or not values:
        raise ProviderError("embedding probe returned an empty vector")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ProviderError("embedding probe returned non-finite values")
    return f"finite vector of length {len(values)}"


#: payload 中攜帶**影像證據**的保留鍵。值為
#: `[{"mime_type": "image/png", "data_b64": "..."}]`。
#:
#: 影像必須以 bytes 進 payload，不得以檔案路徑進 —— 路徑會洩漏
#: class/condition（NOTE-003、NOTE-004 的紅線），而且 provider 收到路徑
#: 也看不到影像，vision agent 會安靜地退化成純文字 agent。
EVIDENCE_IMAGES_KEY = "evidence_images"


def _split_evidence(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把影像證據從 payload 中拆出來。回傳 (可序列化為文字的部分, 影像清單)。"""
    images = payload.get(EVIDENCE_IMAGES_KEY) or []
    if not isinstance(images, list):
        raise ProviderError(
            f"{EVIDENCE_IMAGES_KEY} must be a list of "
            "{'mime_type': ..., 'data_b64': ...} entries"
        )
    for entry in images:
        if not isinstance(entry, dict) or "data_b64" not in entry:
            raise ProviderError(
                f"each {EVIDENCE_IMAGES_KEY} entry needs a base64 'data_b64' field; "
                "a path or a filename is not image evidence"
            )
    rest = {k: v for k, v in payload.items() if k != EVIDENCE_IMAGES_KEY}
    return rest, list(images)


def _payload_to_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Google 的 parts：文字一則，加上每張影像一則 inline_data。"""
    rest, images = _split_evidence(payload)
    parts: list[dict[str, Any]] = [{"text": _payload_to_text(rest)}]
    for image in images:
        # camelCase 與 verify_vision 的 probe 一致。Google 的 protobuf JSON
        # 兩種寫法都收，但 probe 用的那一種是已經對真實端點驗證過的形狀；
        # 兩處寫得不一樣，probe 綠燈就不再保證 invoke 也送得出去。
        parts.append(
            {
                "inlineData": {
                    "mimeType": image.get("mime_type", "image/png"),
                    "data": image["data_b64"],
                }
            }
        )
    return parts


def _payload_to_openai_content(payload: dict[str, Any]) -> Any:
    """OpenAI 相容的 content：純文字時回字串，帶影像時回 content 陣列。"""
    rest, images = _split_evidence(payload)
    text = _payload_to_text(rest)
    if not images:
        return text
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for image in images:
        mime = image.get("mime_type", "image/png")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image['data_b64']}"},
            }
        )
    return content


def _payload_to_text(payload: dict[str, Any]) -> str:
    import json

    rest, _images = _split_evidence(payload)
    return json.dumps(rest, ensure_ascii=False, sort_keys=True)


#: provider 名稱 -> adapter 類別。UI 與 CLI 只透過這張表取得 adapter，
#: 因此新增 provider 只需登記一次，不必改動任何呼叫端。
PROVIDER_ADAPTERS: dict[str, type[HTTPProviderAdapter]] = {
    GoogleAdapter.provider: GoogleAdapter,
    OpenAICompatibleAdapter.provider: OpenAICompatibleAdapter,
    "openrouter": OpenAICompatibleAdapter,
    "custom_openai_compatible": OpenAICompatibleAdapter,
    StubOfflineAdapter.provider: StubOfflineAdapter,
}

#: 可以出現在 formal llm_runtime.lock 中的 provider。
#: stub_offline 刻意不在此列 —— 它的回應不是模型產生的。
FORMAL_ELIGIBLE_PROVIDERS: frozenset[str] = frozenset(
    name for name in PROVIDER_ADAPTERS if name != StubOfflineAdapter.provider
)


def get_adapter(provider: str, resolve_secret) -> HTTPProviderAdapter:
    """依 provider 名稱建立 adapter。未登記的 provider 一律 fail-fast。"""
    try:
        adapter_class = PROVIDER_ADAPTERS[provider]
    except KeyError:
        raise ProviderError(
            f"unknown provider {provider!r}; registered providers: "
            f"{sorted(PROVIDER_ADAPTERS)}"
        ) from None
    return adapter_class(resolve_secret)
