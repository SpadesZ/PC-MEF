# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 monkeypatch 攔截 httpx.request 模擬 provider 回應，
#         驗證 pcmef.agents.provider 的 normalized 契約與錯誤處理；
#         另掃描 pcmef/ 全樹確認沒有 raise_for_status。不對外連線。
# 檔案路徑: tests/llm_admin/test_provider_contract.py
# 產生時間: 2026-08-26 22:00 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 provider 這一層真的做到三件事 —— API key 走 header 不走網址、
#           錯誤訊息不會夾帶網址或金鑰、以及各家回應都被收斂成同一組欄位。
# 模組定位: NOTE-007 的可執行防線，以及 §43「UI/Task Engine 只依賴 normalized
#           descriptor/response」的結構性斷言。它不驗證真實 provider 的行為。
# 主要責任:
#   1. test_no_module_calls_raise_for_status 掃描全樹擋下 NOTE-007 的第一個坑
#   2. test_google_adapter_sends_the_key_in_a_header_not_the_url 擋下第二個坑
#   3. test_http_error_message_contains_neither_url_nor_key 驗證錯誤訊息邊界
#   4. test_error_body_excerpt_is_redacted 驗證回吐憑證的回應也被清洗
#   5. test_probe_payloads_are_fixed_minimal_content 驗證 probe 不用真實 evidence
#   6. test_stub_provider_is_excluded_from_formal 驗證假 provider 進不了 formal
#   7. test_probe_artifact_hash_ignores_timing 驗證同結論的 probe hash 穩定
# 維護提醒:
#   - 不得為了讓某個 adapter 方便而在例外訊息裡加回 request URL；那正是
#     NOTE-007 的成因，且該訊息會落進 llm_verification_logs。
#   - 不得把 StubOfflineAdapter 加進 FORMAL_ELIGIBLE_PROVIDERS 讓測試變簡單。
#   - v0.1.0 新增：首版，對應 NOTE-007 與 NOTE-017。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_provider_contract.py -v
# ------------------------------------------------------------

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pcmef.agents.provider import (
    FORMAL_ELIGIBLE_PROVIDERS,
    PROBE_CHAT_PROMPT,
    PROBE_IMAGE_PNG,
    PROBE_SCHEMA,
    PROVIDER_ADAPTERS,
    Capability,
    ConnectionProfile,
    GoogleAdapter,
    ModelDescriptor,
    ProbeResult,
    ProviderError,
    StubOfflineAdapter,
    get_adapter,
)
from pcmef.core.logging_setup import register_secret

REPO_ROOT = Path(__file__).resolve().parents[2]

FAKE_KEY = "AIza" + "N0tAR3alGoogleKey" * 2
BASE_URL = "https://generativelanguage.example/v1beta"

CONNECTION = ConnectionProfile(
    connection_id="c-1", provider="google",
    secret_ref="env:FAKE_KEY", base_url=BASE_URL, timeout_sec=5,
)
MODEL = ModelDescriptor(model_id="probe-model", provider="google")


class _FakeResponse:
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.text = body

    def json(self):
        return json.loads(self.text)


def _capture(monkeypatch, response: _FakeResponse) -> dict:
    """攔截 httpx.request，記下實際送出的 URL 與 header。"""
    seen: dict = {}

    def fake_request(method, url, headers=None, json=None, timeout=None):
        seen.update(method=method, url=url, headers=headers or {}, json=json)
        return response

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    return seen


# ---------------------------------------------------------------------------
# NOTE-007 的兩個坑
# ---------------------------------------------------------------------------


def _calls_raise_for_status(source: str) -> bool:
    """以 AST 判斷是否真的呼叫了 raise_for_status。

    刻意不用字串比對：檔頭的維護提醒與本測試的說明都必須寫得出這個名字，
    比對字串會讓「寫下禁令」本身觸發禁令，於是唯一能通過的做法變成
    不准解釋為什麼禁 —— 那把一條有理由的規則退化成無法傳達的迷信。
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr == "raise_for_status":
                return True
    return False


def test_no_module_calls_raise_for_status():
    """raise_for_status 的訊息含完整 request URL，一律禁用（NOTE-007）。"""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "pcmef").rglob("*.py"))
        if _calls_raise_for_status(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "raise_for_status() embeds the full request URL in its exception message; "
        f"found in {offenders}. Check the status code and raise ProviderError instead."
    )


def test_the_raise_for_status_scanner_actually_catches_a_call():
    """稽核器自身的反證：擋不住真的呼叫，上一條測試就只是恆真。"""
    assert _calls_raise_for_status("response.raise_for_status()")
    assert not _calls_raise_for_status('"do not call response.raise_for_status()"')


def test_google_adapter_sends_the_key_in_a_header_not_the_url(monkeypatch):
    seen = _capture(monkeypatch, _FakeResponse(200, json.dumps({"models": []})))
    adapter = GoogleAdapter(lambda ref: FAKE_KEY)

    adapter.list_models(CONNECTION)

    assert seen["headers"]["x-goog-api-key"] == FAKE_KEY
    assert FAKE_KEY not in seen["url"]
    assert "key=" not in seen["url"]


# ---------------------------------------------------------------------------
# 錯誤訊息邊界
# ---------------------------------------------------------------------------


def test_http_error_message_contains_neither_url_nor_key(monkeypatch):
    _capture(monkeypatch, _FakeResponse(401, '{"error":"unauthorised"}'))
    adapter = GoogleAdapter(lambda ref: FAKE_KEY)

    with pytest.raises(ProviderError) as excinfo:
        adapter.list_models(CONNECTION)

    message = str(excinfo.value)
    assert FAKE_KEY not in message
    assert BASE_URL not in message
    assert "generativelanguage.example" not in message
    assert "401" in message


def test_error_body_excerpt_is_redacted(monkeypatch):
    """provider 可能把我們送出的憑證原樣回吐；訊息會進 DB，必須在落盤前清洗。"""
    register_secret(FAKE_KEY)
    _capture(monkeypatch, _FakeResponse(400, f'{{"error":"bad key {FAKE_KEY}"}}'))
    adapter = GoogleAdapter(lambda ref: FAKE_KEY)

    with pytest.raises(ProviderError) as excinfo:
        adapter.list_models(CONNECTION)

    message = str(excinfo.value)
    assert FAKE_KEY not in message
    for start in range(0, len(FAKE_KEY) - 8):
        assert FAKE_KEY[start : start + 8] not in message
    assert "[REDACTED]" in message


def test_transport_failure_reports_only_the_exception_type(monkeypatch):
    import httpx

    def boom(method, url, headers=None, json=None, timeout=None):
        raise OSError(f"cannot reach {url} with key {FAKE_KEY}")

    monkeypatch.setattr(httpx, "request", boom)
    adapter = GoogleAdapter(lambda ref: FAKE_KEY)

    with pytest.raises(ProviderError) as excinfo:
        adapter.list_models(CONNECTION)

    message = str(excinfo.value)
    assert FAKE_KEY not in message
    assert BASE_URL not in message
    assert "OSError" in message


def test_a_failed_probe_records_the_sanitised_error_rather_than_raising(monkeypatch):
    _capture(monkeypatch, _FakeResponse(500, "internal error"))
    adapter = GoogleAdapter(lambda ref: FAKE_KEY)

    result = adapter.verify_chat(CONNECTION, MODEL)

    assert result.success is False
    assert result.capability is Capability.CHAT
    assert "500" in result.error_sanitized
    assert FAKE_KEY not in result.error_sanitized


# ---------------------------------------------------------------------------
# Probe 內容
# ---------------------------------------------------------------------------


def test_probe_payloads_are_fixed_minimal_content(monkeypatch):
    """§44：Verification payload 必須最小化，不得拿真實 formal evidence 測連線。"""
    seen = _capture(
        monkeypatch,
        _FakeResponse(
            200,
            json.dumps({"candidates": [{"content": {"parts": [{"text": "ready"}]}}]}),
        ),
    )
    adapter = GoogleAdapter(lambda ref: FAKE_KEY)

    result = adapter.verify_chat(CONNECTION, MODEL)

    assert result.success is True
    sent = json.dumps(seen["json"])
    assert PROBE_CHAT_PROMPT in sent
    # 一個最小 probe 不該有幾百 KB 的內容；真實 evidence 一定超過。
    assert len(sent) < 500


def test_the_probe_image_is_a_small_deterministic_png():
    assert PROBE_IMAGE_PNG.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(PROBE_IMAGE_PNG) < 1024


def test_structured_probe_rejects_a_response_that_violates_the_schema(monkeypatch):
    _capture(
        monkeypatch,
        _FakeResponse(
            200,
            json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"ok": "yes"}'}]}}]}
            ),
        ),
    )
    adapter = GoogleAdapter(lambda ref: FAKE_KEY)

    result = adapter.verify_structured_output(CONNECTION, MODEL, PROBE_SCHEMA)

    assert result.success is False
    assert "schema validation" in result.error_sanitized


def test_structured_probe_accepts_a_conforming_response(monkeypatch):
    _capture(
        monkeypatch,
        _FakeResponse(
            200,
            json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": '{"probe_version": "v1", "ok": true}'}
                                ]
                            }
                        }
                    ]
                }
            ),
        ),
    )
    adapter = GoogleAdapter(lambda ref: FAKE_KEY)

    result = adapter.verify_structured_output(CONNECTION, MODEL, PROBE_SCHEMA)

    assert result.success is True
    assert "schema-validated" in result.detail


# ---------------------------------------------------------------------------
# Normalized 契約與 stub
# ---------------------------------------------------------------------------


def test_every_registered_provider_yields_an_adapter():
    for provider in PROVIDER_ADAPTERS:
        adapter = get_adapter(provider, lambda ref: FAKE_KEY)
        for method in (
            "list_models", "verify_chat", "verify_vision",
            "verify_structured_output", "verify_embedding", "invoke",
        ):
            assert callable(getattr(adapter, method)), f"{provider}.{method}"


def test_unknown_provider_fails_fast():
    with pytest.raises(ProviderError, match="unknown provider"):
        get_adapter("definitely-not-a-provider", lambda ref: FAKE_KEY)


def test_stub_provider_is_excluded_from_formal():
    """離線 stub 必須可用於 admin 流程測試，但絕不可進 formal identity。"""
    assert StubOfflineAdapter.provider in PROVIDER_ADAPTERS
    assert StubOfflineAdapter.provider not in FORMAL_ELIGIBLE_PROVIDERS
    assert "google" in FORMAL_ELIGIBLE_PROVIDERS


def test_stub_adapter_reports_only_the_capabilities_it_was_given():
    adapter = StubOfflineAdapter(lambda ref: FAKE_KEY)
    adapter.catalogue = {
        "chat-only": (Capability.CHAT,),
        "embed-only": (Capability.EMBEDDING,),
    }
    connection = ConnectionProfile(
        connection_id="c", provider=StubOfflineAdapter.provider,
        secret_ref="env:FAKE_KEY",
    )
    chat_only = ModelDescriptor(model_id="chat-only", provider=adapter.provider)
    embed_only = ModelDescriptor(model_id="embed-only", provider=adapter.provider)

    assert adapter.verify_chat(connection, chat_only).success is True
    assert adapter.verify_chat(connection, embed_only).success is False
    assert adapter.verify_embedding(connection, embed_only).success is True


def test_probe_artifact_hash_ignores_timing():
    """同一個結論在不同時間 probe 應得到同一個 hash，否則 lock 會被迫跟著變。"""
    first = ProbeResult(Capability.CHAT, True, latency_ms=12, detail="ok")
    second = ProbeResult(Capability.CHAT, True, latency_ms=987, detail="ok")
    assert first.artifact_hash() == second.artifact_hash()

    failed = ProbeResult(Capability.CHAT, False, latency_ms=12, detail="ok")
    assert failed.artifact_hash() != first.artifact_hash()


def test_model_descriptor_requires_an_id():
    with pytest.raises(ProviderError, match="model_id"):
        ModelDescriptor(model_id="", provider="google")


def test_connection_profile_rejects_a_non_positive_timeout():
    with pytest.raises(ProviderError, match="timeout_sec"):
        ConnectionProfile(
            connection_id="c", provider="google",
            secret_ref="env:X", timeout_sec=0,
        )
