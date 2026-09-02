# PC-MEF Research System source maintenance contract
# 上下游: 測試 pcmef.agents.provider 的 GoogleAdapter 與 OpenAICompatibleAdapter；
#         以 monkeypatch 攔截 httpx.request，**不對外連線**。
# 檔案路徑: tests/agents/test_provider_execution_path.py
# 產生時間: 2026-08-31 21:15 +08:00
# 版本: v0.1.0
# 功能說明: 檢查兩條正式 provider 路徑真的把影像與 JSON schema 放上線路。
#           前一版的 invoke() 會把整個 payload JSON 序列化成文字，
#           vision agent 因此從來沒有看過圖，而且不會有任何症狀 ——
#           本檔就是為了讓那種失敗方式會變紅而存在。
# 模組定位: normalized provider contract 的線路層驗證。
#           它「不是」provider 行為測試 —— 回應內容是假的，只驗請求形狀。
# 主要責任:
#   1. Google 的 request body 內含 inlineData 與可解碼的 PNG
#   2. Google 在需要 structured 時帶上 responseSchema 與 responseMimeType
#   3. OpenAI 相容路徑的 content 是陣列且含 image_url data URI
#   4. OpenAI 相容路徑帶上 response_format=json_schema
#   5. 純文字角色不得憑空多送影像欄位
#   6. 錯誤訊息不得含 URL 或憑證
# 維護提醒:
#   - 不得改成斷言「呼叫沒有拋例外」。送出純文字也不會拋例外，
#     那正是這個 bug 之前躲過所有測試的原因。
#   - 不得在此檔放真實 API key 或 .env；連線只用 secret_ref: env:<NAME>。
#   - v0.1.0 新增：首版線路層驗證。
# 驗證方式:
#   - py -3.10 -m pytest tests/agents/test_provider_execution_path.py -v
# ------------------------------------------------------------

from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import pytest

from pcmef.agents.pcmef_agents import AGENTS, AgentRunner, encode_image_evidence
from pcmef.agents.provider import (
    EVIDENCE_IMAGES_KEY,
    ConnectionProfile,
    GoogleAdapter,
    ModelDescriptor,
    OpenAICompatibleAdapter,
    ProviderError,
)

SCHEMA_DIR = Path("schemas")


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _reply_for(body: dict) -> str:
    """依請求裡實際帶的 schema 合成一個合法回應。

    刻意從**請求**取 schema，而不是寫死一份觀察摘要：這樣一來
    「請求裡到底有沒有 schema」會直接決定測試能不能跑完，
    schema 沒送出去的話這裡就取不到，測試會壞在該壞的地方。
    """
    from pcmef.agents.provider import STUB_ROLE_HINTS, _synthesise_from_schema

    if "contents" in body:  # Google
        schema = (body.get("generationConfig") or {}).get("responseSchema")
        task_code = None
    else:  # OpenAI 相容
        fmt = body.get("response_format") or {}
        schema = (fmt.get("json_schema") or {}).get("schema")
        task_code = (fmt.get("json_schema") or {}).get("name")
    if not schema:
        raise AssertionError(
            "the request carried no JSON schema; a structured-output agent must "
            "actually ask for one"
        )
    hints = STUB_ROLE_HINTS.get(task_code)
    if hints is None and "class_support" in (schema.get("properties") or {}):
        # Google 的請求 body 不帶角色名（task_code 只存在於 OpenAI 的
        # json_schema.name），因此這裡改由 schema 特徵辨認仲裁者。
        # 照字面合成會給出四個 0：那份回應通過 JSON Schema，卻表示
        # 「仲裁者什麼都沒說」，是真實模型會被語意檢查擋下的違約。
        hints = STUB_ROLE_HINTS["arbitration_agent"]
    return json.dumps(_synthesise_from_schema(schema, hints))


@pytest.fixture()
def captured(monkeypatch) -> list[dict]:
    """攔截 httpx.request，把送出的 body 收集起來。"""
    import httpx

    sent: list[dict] = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        sent.append({"method": method, "url": url, "headers": headers, "body": json})
        reply = _reply_for(json or {})
        if "contents" in (json or {}):
            return _FakeResponse(
                {"candidates": [{"content": {"parts": [{"text": reply}]}}]}
            )
        return _FakeResponse({"choices": [{"message": {"content": reply}}]})

    monkeypatch.setattr(httpx, "request", fake_request)
    return sent


@pytest.fixture()
def image() -> dict:
    rng = np.random.default_rng(11)
    return encode_image_evidence(rng.random((32, 32, 3)))


@pytest.fixture()
def body() -> dict:
    return {"schema_version": "1.0", "tof_summary": {"peak_bin": 12}}


def _runner(adapter_cls, provider: str, base_url: str) -> AgentRunner:
    return AgentRunner(
        adapter=adapter_cls(resolve_secret=lambda ref: "TEST-KEY-NOT-REAL"),
        connection=ConnectionProfile(
            connection_id="c1", provider=provider,
            secret_ref="env:PCMEF_TEST_KEY", base_url=base_url,
        ),
        model=ModelDescriptor(model_id="m1", provider=provider),
    )


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


def test_google_puts_a_real_png_on_the_wire(captured, image, body):
    runner = _runner(GoogleAdapter, "google", "https://example.invalid/v1beta")
    runner.run(AGENTS["observation_agent"], body, images=[image])

    parts = captured[0]["body"]["contents"][0]["parts"]
    kinds = [set(p) for p in parts]
    assert {"inlineData"} in kinds, f"no image part was sent; parts were {kinds}"
    inline = next(p["inlineData"] for p in parts if "inlineData" in p)
    assert inline["mimeType"] == "image/png"
    assert base64.b64decode(inline["data"])[:8] == b"\x89PNG\r\n\x1a\n"


def test_google_requests_the_json_schema(captured, image, body):
    runner = _runner(GoogleAdapter, "google", "https://example.invalid/v1beta")
    runner.run(AGENTS["observation_agent"], body, images=[image])

    config = captured[0]["body"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "observed_facts" in config["responseSchema"]["properties"]


def test_google_text_only_role_sends_no_image_part(captured, body):
    runner = _runner(GoogleAdapter, "google", "https://example.invalid/v1beta")
    runner.run(
        AGENTS["arbitration_agent"],
        {**body, "anonymous_proposals": []},
    )
    parts = captured[0]["body"]["contents"][0]["parts"]
    assert all("inlineData" not in p for p in parts)
    assert EVIDENCE_IMAGES_KEY not in parts[0]["text"]


# ---------------------------------------------------------------------------
# OpenAI 相容
# ---------------------------------------------------------------------------


def test_openai_compatible_puts_a_data_uri_on_the_wire(captured, image, body):
    runner = _runner(
        OpenAICompatibleAdapter, "openai_compatible", "https://example.invalid/v1"
    )
    runner.run(AGENTS["observation_agent"], body, images=[image])

    content = captured[0]["body"]["messages"][0]["content"]
    assert isinstance(content, list), "image evidence must not collapse into a string"
    image_parts = [c for c in content if c.get("type") == "image_url"]
    assert len(image_parts) == 1
    url = image_parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1])[:4] == b"\x89PNG"


def test_openai_compatible_requests_the_json_schema(captured, image, body):
    runner = _runner(
        OpenAICompatibleAdapter, "openai_compatible", "https://example.invalid/v1"
    )
    runner.run(AGENTS["observation_agent"], body, images=[image])

    fmt = captured[0]["body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "observation_agent"
    assert "observed_facts" in fmt["json_schema"]["schema"]["properties"]


def test_openai_text_only_role_keeps_a_plain_string_content(captured, body):
    runner = _runner(
        OpenAICompatibleAdapter, "openai_compatible", "https://example.invalid/v1"
    )
    runner.run(AGENTS["arbitration_agent"], {**body, "anonymous_proposals": []})
    content = captured[0]["body"]["messages"][0]["content"]
    assert isinstance(content, str)


# ---------------------------------------------------------------------------
# 秘密與錯誤訊息
# ---------------------------------------------------------------------------


def test_the_api_key_never_appears_in_the_url(captured, image, body):
    """NOTE-007：key 一旦進 query string，就會流進錯誤訊息與 artifact。"""
    for adapter_cls, provider, base in (
        (GoogleAdapter, "google", "https://example.invalid/v1beta"),
        (OpenAICompatibleAdapter, "openai_compatible", "https://example.invalid/v1"),
    ):
        runner = _runner(adapter_cls, provider, base)
        runner.run(AGENTS["observation_agent"], body, images=[image])
    for record in captured:
        assert "TEST-KEY-NOT-REAL" not in record["url"]
        assert "key=" not in record["url"]


# ---------------------------------------------------------------------------
# schema 方言：兩家的要求相反
# ---------------------------------------------------------------------------


def _all_keys(node, found=None):
    """收集 schema 樹裡出現過的所有欄位名（遞迴）。"""
    found = found if found is not None else set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            _all_keys(value, found)
    elif isinstance(node, list):
        for item in node:
            _all_keys(item, found)
    return found


@pytest.mark.parametrize("schema_name", [
    "observation_brief_v1", "specialist_proposal_v1", "arbitration_output_v1",
])
def test_google_schema_drops_additional_properties_at_every_depth(schema_name):
    """實測踩到的 400：Unknown name "additionalProperties"。

    三份 agent schema 的 additionalProperties 有的在頂層、有的在巢狀
    properties 裡，所以只剝頂層會留下巢狀那些 —— 而 Google 的錯誤訊息
    只指到 'generation_config.response_schema'，看不出是哪一層。
    """
    from pcmef.agents.provider import _to_google_schema

    schema = json.loads(
        (SCHEMA_DIR / f"{schema_name}.schema.json").read_text(encoding="utf-8")
    )
    assert "additionalProperties" in _all_keys(schema), "前提：原始 schema 有這個欄位"

    converted = _to_google_schema(schema)
    keys = _all_keys(converted)
    for forbidden in ("additionalProperties", "$schema", "$id", "const"):
        assert forbidden not in keys, f"{forbidden} 仍留在送給 Google 的 schema 裡"


@pytest.mark.parametrize("schema_name", [
    "observation_brief_v1", "specialist_proposal_v1", "arbitration_output_v1",
])
def test_openai_schema_keeps_additional_properties(schema_name):
    """OpenAI 的 strict 模式**要求** additionalProperties: false。

    兩家需求相反，因此不能共用同一份轉換結果 —— 這個測試和上面那個
    互為對照，任何一邊被改成共用都會有一條變紅。
    """
    from pcmef.agents.provider import _to_openai_schema

    schema = json.loads(
        (SCHEMA_DIR / f"{schema_name}.schema.json").read_text(encoding="utf-8")
    )
    converted = _to_openai_schema(schema)
    assert converted["additionalProperties"] is False
    keys = _all_keys(converted)
    assert "$schema" not in keys and "$id" not in keys and "const" not in keys


def test_const_becomes_a_single_value_enum_not_dropped():
    """const 兩家都不支援，但不能直接丟掉。

    丟掉會讓 schema_version 變成任意字串，那一欄的驗證就形同虛設。
    改寫成單值 enum 才保得住原本的語意。
    """
    from pcmef.agents.provider import _to_google_schema

    schema = json.loads(
        (SCHEMA_DIR / "observation_brief_v1.schema.json").read_text(encoding="utf-8")
    )
    original = schema["properties"]["schema_version"]
    assert "const" in original, "前提：這一欄用 const 釘住版本"

    converted = _to_google_schema(schema)["properties"]["schema_version"]
    assert converted["enum"] == [original["const"]]
    assert converted["type"] == "string"


def test_the_probe_schema_survives_conversion_too():
    """probe 用的 schema 也含 additionalProperties 與 const —— 它先炸的。"""
    from pcmef.agents.provider import PROBE_SCHEMA, _to_google_schema

    keys = _all_keys(_to_google_schema(PROBE_SCHEMA))
    assert "additionalProperties" not in keys
    assert "const" not in keys


def test_converted_schema_still_validates_a_good_instance():
    """轉換不得改變 schema 接受什麼 —— 只是換一種方言表達。"""
    import jsonschema

    from pcmef.agents.provider import (
        STUB_ROLE_HINTS, _synthesise_from_schema, _to_openai_schema,
    )

    schema = json.loads(
        (SCHEMA_DIR / "specialist_proposal_v1.schema.json").read_text(encoding="utf-8")
    )
    instance = _synthesise_from_schema(schema, STUB_ROLE_HINTS["physics_agent"])
    jsonschema.validate(instance, schema)
    jsonschema.validate(instance, _to_openai_schema(schema))


def test_http_errors_are_sanitised_and_carry_no_url(monkeypatch, image, body):
    import httpx

    def failing(method, url, headers=None, json=None, timeout=None):
        return _FakeResponse({"error": "unauthorized TEST-KEY-NOT-REAL"}, 401)

    monkeypatch.setattr(httpx, "request", failing)
    adapter = GoogleAdapter(resolve_secret=lambda ref: "TEST-KEY-NOT-REAL")
    connection = ConnectionProfile(
        connection_id="c1", provider="google", secret_ref="env:PCMEF_TEST_KEY",
        base_url="https://example.invalid/v1beta",
    )
    model = ModelDescriptor(model_id="m1", provider="google")

    with pytest.raises(ProviderError) as caught:
        adapter.invoke(connection, model, "observation_agent", {"a": 1}, {})

    message = str(caught.value)
    assert "example.invalid" not in message
    assert "https://" not in message
    assert "HTTP 401" in message
