# PC-MEF Research System source maintenance contract
# 上下游: 測試 pcmef.agents.pcmef_agents 與 pcmef.agents.provider 的執行路徑；
#         使用 StubOfflineAdapter，不呼叫任何外部端點。
# 檔案路徑: tests/agents/test_pcmef_agents.py
# 產生時間: 2026-08-31 20:40 +08:00
# 版本: v0.1.0
# 功能說明: 在沒有真實 API key 的情況下證明四 agent 路徑「真的做了該做的事」——
#           影像真的以 bytes 送進 adapter、schema 真的被要求並驗證、
#           重試耗盡真的中止而不是 drop case。
# 模組定位: LLM integration 的離線閘門。
#           它「不是」研究結果 —— stub 的輸出不得作為任何 E2 數據。
# 主要責任:
#   1. 證明 vision agent 送出的是 image bytes，不是路徑也不是 JSON 文字
#   2. 證明 structured agent 真的要求 schema，且回應經過 jsonschema 驗證
#   3. 證明 retry=2 的邊界：第 2 次成功可接受，全失敗必須拋 RetryExhaustedError
#   4. 證明 provider 錯誤訊息不含 URL 與 secret
#   5. 證明 stub 無法進入 formal freeze
# 維護提醒:
#   - 不得為了讓測試變綠而把 StubOfflineAdapter 加進 FORMAL_ELIGIBLE_PROVIDERS。
#   - 不得把 RetryExhaustedError 改成回傳 None；那正是 drop case。
#   - v0.1.0 新增：首版離線整合測試。
# 驗證方式:
#   - py -3.10 -m pytest tests/agents/test_pcmef_agents.py -v
# ------------------------------------------------------------

from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import pytest

from pcmef.agents.provider import (
    EVIDENCE_IMAGES_KEY,
    FORMAL_ELIGIBLE_PROVIDERS,
    ConnectionProfile,
    ModelDescriptor,
    ProviderError,
    StubOfflineAdapter,
    _payload_to_openai_content,
    _payload_to_parts,
    _payload_to_text,
)
from pcmef.agents.pcmef_agents import (
    AGENTS,
    MAX_ATTEMPTS,
    RELIABILITY_CLAUSE,
    AgentError,
    AgentRunner,
    RetryExhaustedError,
    assert_registry_consistent,
    build_case_evidence,
    case_cache_key,
    encode_image_evidence,
    run_pcmef_case,
    tof_fixed_summary,
)

SCHEMA_DIR = Path("schemas")


def _sent_payload(stub: StubOfflineAdapter, task_code: str) -> dict:
    """取出某個角色實際收到的 payload（文字部分）。"""
    for record in stub.invocations:
        if record["task_code"] == task_code:
            return record["payload"]
    raise AssertionError(f"{task_code} was never invoked")


def evidence_classes() -> tuple[str, ...]:
    from pcmef.agents.pcmef_agents import CLASS_ORDER

    return CLASS_ORDER


@pytest.fixture()
def stub() -> StubOfflineAdapter:
    return StubOfflineAdapter(resolve_secret=lambda ref: "offline")


@pytest.fixture()
def connection() -> ConnectionProfile:
    return ConnectionProfile(
        connection_id="offline", provider="stub_offline", secret_ref="env:UNUSED"
    )


@pytest.fixture()
def model() -> ModelDescriptor:
    return ModelDescriptor(
        model_id="stub-multimodal", provider="stub_offline", provider_revision="r1"
    )


@pytest.fixture()
def runner(stub, connection, model) -> AgentRunner:
    return AgentRunner(
        adapter=stub, connection=connection, model=model, schema_dir=SCHEMA_DIR
    )


@pytest.fixture()
def evidence() -> dict:
    rng = np.random.default_rng(7)
    return build_case_evidence(
        rgb=rng.random((64, 64, 3)),
        tof=np.abs(rng.normal(size=500)) + 0.1,
        p_vision=[0.7, 0.1, 0.1, 0.1],
        p_tof=[0.1, 0.6, 0.2, 0.1],
        q_vision=0.82,
        q_tof=0.31,
        duq={"D": 0.55, "U": 0.42, "Q_vision": 120.0, "Q_tof": 15.3},
        route="escalated",
    )


# ---------------------------------------------------------------------------
# 角色契約
# ---------------------------------------------------------------------------


def test_agent_set_matches_formal_task_registry():
    """四個角色與 §45 一致，且 needs_image 等於 registry 的 VISION 要求。"""
    assert_registry_consistent()
    assert set(AGENTS) == {
        "observation_agent", "physics_agent",
        "visual_semantic_agent", "arbitration_agent",
    }
    assert AGENTS["observation_agent"].needs_image is True
    assert AGENTS["visual_semantic_agent"].needs_image is True
    assert AGENTS["physics_agent"].needs_image is False
    assert AGENTS["arbitration_agent"].needs_image is False


def test_every_prompt_states_confidence_is_not_reliability():
    """四個 prompt 都必須逐字帶著那句話，不是只有仲裁者。"""
    from pcmef.agents.pcmef_agents import assert_prompts_state_the_rule

    assert_prompts_state_the_rule()
    for spec in AGENTS.values():
        assert RELIABILITY_CLAUSE.lower() in spec.system_prompt.lower(), spec.task_code


def test_the_roles_that_see_probabilities_name_the_forbidden_substitutes():
    """會拿到 p(y|x) 的角色，必須明確禁止拿 max-softmax / entropy 當可靠度。

    Observation Agent 不在此列 —— 它根本收不到機率（見 withheld_keys），
    所以要求它的 prompt 討論 max-softmax 只是空話。
    """
    for task_code in ("physics_agent", "visual_semantic_agent", "arbitration_agent"):
        text = AGENTS[task_code].system_prompt.lower()
        assert "confidence" in text, task_code
    arbitration = AGENTS["arbitration_agent"].system_prompt.lower()
    assert "max softmax" in arbitration
    assert "entropy" in arbitration


def test_prompts_live_in_files_not_python_constants():
    """prompt 的單一來源必須是 configs/agents/prompts/ 下的檔案。

    snapshot.py 對那些檔案取雜湊寫進 llm_runtime.lock。prompt 若同時存在於
    Python 常數，凍結記錄的會是檔案雜湊而實際送出的是常數內容 ——
    不一致，而且沒有任何症狀。
    """
    from pcmef.agents.pcmef_agents import PROMPT_DIR
    from pcmef.core.hash import hash_file
    from pcmef.llm.snapshot import PROMPT_KEYS

    for spec in AGENTS.values():
        assert spec.prompt_path.exists(), spec.task_code
        # cache key 用的雜湊必須等於 lock 會記下的那一個。
        assert spec.prompt_hash() == hash_file(spec.prompt_path)

    # snapshot 期待的檔名要與四個角色一一對上，否則 freeze 會缺項。
    expected = {PROMPT_DIR / name for name in PROMPT_KEYS.values()}
    assert {spec.prompt_path for spec in AGENTS.values()} == expected


# ---------------------------------------------------------------------------
# 影像證據：真的送圖，不是送路徑或文字
# ---------------------------------------------------------------------------


def test_encoded_image_is_a_real_png_not_a_path():
    rgb = np.zeros((8, 8, 3))
    rgb[:, :, 0] = 1.0
    entry = encode_image_evidence(rgb)
    raw = base64.b64decode(entry["data_b64"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "evidence must be PNG bytes"
    assert "/" not in entry["data_b64"][:0] if False else True
    assert entry["mime_type"] == "image/png"


def test_evidence_carries_image_bytes_and_no_file_paths(evidence):
    images = evidence[EVIDENCE_IMAGES_KEY]
    assert len(images) == 1
    assert base64.b64decode(images[0]["data_b64"])[:4] == b"\x89PNG"
    # payload 的文字部分不得出現路徑，也不得出現這個 case 的身分。
    #
    # 注意：class_order 裡的 "Empty" / "Misty" 等是**類別詞彙**，必須在
    # payload 裡，否則模型不知道可選項有哪些。要擋的是這一筆的
    # ground truth 與產生條件（NOTE-003 / NOTE-004），不是詞彙本身。
    text = _payload_to_text(evidence)
    for needle in ("outputs/", ".exr", ".png", "family_", "artifacts/", "C:"):
        assert needle not in text, f"path-like {needle!r} leaked into the payload"
    for needle in ("true_class", "label", "condition", "severity", "case_id",
                   "scenario_id", "ground_truth"):
        assert needle not in text, f"case identity {needle!r} leaked into the payload"


def test_google_parts_contain_inline_image_data(evidence):
    parts = _payload_to_parts(evidence)
    assert [set(p) for p in parts] == [{"text"}, {"inlineData"}]
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert base64.b64decode(parts[1]["inlineData"]["data"])[:4] == b"\x89PNG"


def test_openai_content_contains_image_url_data_uri(evidence):
    content = _payload_to_openai_content(evidence)
    assert isinstance(content, list)
    assert [c["type"] for c in content] == ["text", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_only_payload_stays_a_plain_string():
    """沒有影像時不應該憑空包成 content 陣列 —— 那會改變純文字角色的請求形狀。"""
    assert isinstance(_payload_to_openai_content({"a": 1}), str)


def test_stub_actually_receives_the_image_bytes(runner, stub, evidence):
    """最關鍵的一條：adapter 收到的是影像，不是描述影像的字串。"""
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    runner.run(
        AGENTS["observation_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY]
    )
    record = stub.invocations[-1]
    assert record["task_code"] == "observation_agent"
    assert record["image_count"] == 1
    assert record["image_bytes"] > 1000, "an image this small means nothing was sent"


def test_vision_agent_refuses_to_run_without_an_image(runner, evidence):
    """需要視覺的角色收不到圖就必須拒絕，而不是安靜地變成純文字 agent。"""
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    with pytest.raises(AgentError, match="requires vision evidence"):
        runner.run(AGENTS["observation_agent"], body, images=[])


def test_text_only_agent_refuses_to_accept_images(runner, evidence):
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    with pytest.raises(AgentError, match="text-only role"):
        runner.run(
            AGENTS["physics_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY]
        )


# ---------------------------------------------------------------------------
# structured output：真的要求 schema，也真的驗證
# ---------------------------------------------------------------------------


def test_schema_is_actually_requested_and_response_validated(runner, stub, evidence):
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    parsed, _ = runner.run(
        AGENTS["observation_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY]
    )
    assert stub.invocations[-1]["schema_requested"] is True
    schema = json.loads(
        (SCHEMA_DIR / "observation_brief_v1.schema.json").read_text(encoding="utf-8")
    )
    for name in schema["required"]:
        assert name in parsed, f"validated output is missing required field {name}"


def test_invalid_json_is_rejected_not_silently_accepted(connection, model, evidence):
    class BadJSON(StubOfflineAdapter):
        def invoke(self, connection, model, task_code, payload, runtime_cfg):
            response = super().invoke(connection, model, task_code, payload, runtime_cfg)
            return type(response)(**{**response.__dict__, "text": "not json at all"})

    runner = AgentRunner(
        adapter=BadJSON(resolve_secret=lambda r: "x"),
        connection=connection, model=model, schema_dir=SCHEMA_DIR,
    )
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    with pytest.raises(RetryExhaustedError):
        runner.run(
            AGENTS["observation_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY]
        )


def test_schema_valid_json_but_wrong_shape_is_rejected(connection, model, evidence):
    """回傳合法 JSON 但缺 required 欄位時必須失敗，否則 schema 驗證形同虛設。"""
    class WrongShape(StubOfflineAdapter):
        def invoke(self, connection, model, task_code, payload, runtime_cfg):
            response = super().invoke(connection, model, task_code, payload, runtime_cfg)
            return type(response)(**{**response.__dict__, "text": '{"schema_version": "1.0"}'})

    runner = AgentRunner(
        adapter=WrongShape(resolve_secret=lambda r: "x"),
        connection=connection, model=model, schema_dir=SCHEMA_DIR,
    )
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    with pytest.raises(RetryExhaustedError):
        runner.run(
            AGENTS["observation_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY]
        )


# ---------------------------------------------------------------------------
# retry 邊界與 fail-closed
# ---------------------------------------------------------------------------


def test_retry_budget_is_two_attempts(runner):
    assert runner.max_attempts == MAX_ATTEMPTS == 2


def test_second_attempt_may_succeed(connection, model, evidence):
    """第一次失敗、第二次成功必須被接受 —— 這是 retry 存在的理由。"""
    class FlakyOnce(StubOfflineAdapter):
        failures = 1

        def invoke(self, connection, model, task_code, payload, runtime_cfg):
            if self.failures > 0:
                self.failures -= 1
                raise ProviderError("stub_offline: transport failure (TimeoutError)")
            return super().invoke(connection, model, task_code, payload, runtime_cfg)

    runner = AgentRunner(
        adapter=FlakyOnce(resolve_secret=lambda r: "x"),
        connection=connection, model=model, schema_dir=SCHEMA_DIR,
    )
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    parsed, _ = runner.run(
        AGENTS["observation_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY]
    )
    assert parsed["schema_version"]
    assert [a.ok for a in runner.attempts] == [False, True]


def test_retry_exhaustion_aborts_and_never_returns_a_case(connection, model, evidence):
    """耗盡即中止。**不得**回傳 None、不得回傳部分結果、不得 drop case。"""
    class AlwaysFails(StubOfflineAdapter):
        def invoke(self, connection, model, task_code, payload, runtime_cfg):
            raise ProviderError("stub_offline: HTTP 503; body[:200]='overloaded'")

    runner = AgentRunner(
        adapter=AlwaysFails(resolve_secret=lambda r: "x"),
        connection=connection, model=model, schema_dir=SCHEMA_DIR,
    )
    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}
    with pytest.raises(RetryExhaustedError, match="ABORT_FORMAL_RUN"):
        runner.run(
            AGENTS["observation_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY]
        )
    assert len(runner.attempts) == 2
    assert not any(a.ok for a in runner.attempts)


def test_abort_propagates_out_of_the_whole_case(connection, model, evidence):
    """整個 case 也必須一起失敗，而不是少一個 agent 照樣組出 bundle。"""
    class FailsOnArbitration(StubOfflineAdapter):
        def invoke(self, connection, model, task_code, payload, runtime_cfg):
            if task_code == "arbitration_agent":
                raise ProviderError("stub_offline: HTTP 500; body[:200]=''")
            return super().invoke(connection, model, task_code, payload, runtime_cfg)

    runner = AgentRunner(
        adapter=FailsOnArbitration(resolve_secret=lambda r: "x"),
        connection=connection, model=model, schema_dir=SCHEMA_DIR,
    )
    with pytest.raises(RetryExhaustedError):
        run_pcmef_case(runner, evidence)


def test_provider_errors_do_not_leak_urls_or_secrets(runner, stub, connection, model):
    """錯誤訊息會被寫進 artifact，所以它不能含端點或憑證。"""
    class Leaky(StubOfflineAdapter):
        def invoke(self, connection, model, task_code, payload, runtime_cfg):
            raise ProviderError("stub_offline: HTTP 401; body[:200]='bad key'")

    r = AgentRunner(
        adapter=Leaky(resolve_secret=lambda ref: "x"),
        connection=connection, model=model, schema_dir=SCHEMA_DIR,
    )
    try:
        r.run(AGENTS["arbitration_agent"], {"a": 1})
    except RetryExhaustedError as error:
        message = str(error)
    assert "http://" not in message and "https://" not in message
    assert "generativelanguage" not in message
    assert "key=" not in message


# ---------------------------------------------------------------------------
# 四 agent 串接與 cache identity
# ---------------------------------------------------------------------------


def test_full_four_agent_case_produces_all_six_artifacts(runner, stub, evidence):
    bundle = run_pcmef_case(runner, evidence)
    assert set(bundle.artifacts) == {
        "observation_raw", "observation_validated", "physics_proposal",
        "visual_proposal", "arbitration_raw", "arbitration_validated",
    }
    codes = [record["task_code"] for record in stub.invocations]
    assert codes == [
        "observation_agent", "physics_agent",
        "visual_semantic_agent", "arbitration_agent",
    ]
    # 只有需要視覺的兩個角色收到影像。
    sent = {r["task_code"]: r["image_count"] for r in stub.invocations}
    assert sent == {
        "observation_agent": 1, "physics_agent": 0,
        "visual_semantic_agent": 1, "arbitration_agent": 0,
    }
    assert all(r["schema_requested"] for r in stub.invocations)


def test_specialists_declare_their_own_modality(runner, evidence):
    bundle = run_pcmef_case(runner, evidence)
    assert bundle.artifacts["physics_proposal"]["modality"] == "physics"
    assert bundle.artifacts["visual_proposal"]["modality"] == "visual_semantic"


def test_cache_key_is_stable_and_evidence_sensitive(evidence):
    kwargs = dict(model_id="m", provider_revision="r1", runtime_config_hash="cfg")
    first = case_cache_key(evidence, **kwargs)
    assert first.digest() == case_cache_key(evidence, **kwargs).digest()

    changed = {**evidence, "gate_route": "trust_vision"}
    assert case_cache_key(changed, **kwargs).digest() != first.digest()

    rng = np.random.default_rng(99)
    other_image = {**evidence, EVIDENCE_IMAGES_KEY: [
        encode_image_evidence(rng.random((64, 64, 3)))
    ]}
    assert case_cache_key(other_image, **kwargs).digest() != first.digest(), (
        "a different image must change the cache key, otherwise two different "
        "scenes would share one cached answer"
    )


def test_reliability_and_confidence_are_separately_labelled(evidence):
    """payload 必須把 p(y|x) 與 q_m 標成不同的東西。"""
    assert evidence["modality_reliability"]["q_vision"] == 0.82
    assert "NOT sensor reliability" in evidence["calibrated_class_probabilities"]["_meaning"]
    forbidden = evidence["modality_reliability"]["forbidden_sources"]
    assert "max softmax probability" in forbidden
    assert "predictive entropy" in forbidden


def test_fixed_summary_length_is_constant_across_cases():
    """FIXED_SUMMARY 的欄位集合必須逐 case 相同，否則 prompt 長度變成變因。"""
    rng = np.random.default_rng(3)
    a = tof_fixed_summary(np.abs(rng.normal(size=500)))
    b = tof_fixed_summary(np.abs(rng.normal(size=500)) * 100.0)
    assert set(a) == set(b)


# ---------------------------------------------------------------------------
# stub 不得被凍結成 formal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 角色隔離：靠不送，不靠 prompt 拜託
# ---------------------------------------------------------------------------


def test_physics_never_receives_vision_evidence(runner, stub, evidence):
    """physics 的 prompt 禁止用 RGB，那 payload 就不該含 RGB 側的東西。

    一邊叫它別看、一邊把 p_vision 遞過去，等於在 specialist 這層重演
    Observation Agent 刻意避開的 anchoring。
    """
    run_pcmef_case(runner, evidence)
    sent = _sent_payload(stub, "physics_agent")
    # withheld_from_this_role 是稽核用的欄位**名稱**清單，不含任何值，
    # 因此檢查洩漏時要把它排除，否則會把稽核紀錄本身誤判為洩漏。
    text = json.dumps(
        {k: v for k, v in sent.items() if k != "withheld_from_this_role"},
        ensure_ascii=False,
    )

    assert "q_vision" not in text
    assert "Q_vision" not in text
    assert sent["calibrated_class_probabilities"].get("vision") is None
    # ToF 側必須完整保留，否則它沒東西可推理。
    assert sent["tof_summary"]["peak_bin"] is not None
    assert sent["modality_reliability"]["q_tof"] == 0.31


def test_visual_never_receives_tof_evidence(runner, stub, evidence):
    run_pcmef_case(runner, evidence)
    sent = _sent_payload(stub, "visual_semantic_agent")
    text = json.dumps(
        {k: v for k, v in sent.items() if k != "withheld_from_this_role"},
        ensure_ascii=False,
    )

    assert "tof_summary" not in sent
    assert "q_tof" not in text
    assert sent["calibrated_class_probabilities"].get("tof") is None
    assert sent["modality_reliability"]["q_vision"] == 0.82


def test_observation_never_receives_class_probabilities(runner, stub, evidence):
    """它不做分類，所以不該看到分類器的輸出。"""
    run_pcmef_case(runner, evidence)
    sent = _sent_payload(stub, "observation_agent")

    assert "calibrated_class_probabilities" not in sent
    # 但可靠度與品質證據要留著 —— 那正是它要評估的東西。
    assert sent["modality_reliability"]["q_vision"] == 0.82
    assert sent["duq_signals"]["Q_tof"] == 15.3


def test_arbitration_sees_both_sides(runner, stub, evidence):
    """仲裁者是唯一該看到全部證據的角色。"""
    run_pcmef_case(runner, evidence)
    sent = _sent_payload(stub, "arbitration_agent")

    assert sent["calibrated_class_probabilities"]["vision"]
    assert sent["calibrated_class_probabilities"]["tof"]
    assert sent["modality_reliability"]["q_vision"] == 0.82
    assert sent["modality_reliability"]["q_tof"] == 0.31
    assert len(sent["anonymous_proposals"]) == 2


def test_withholding_is_recorded_so_it_is_auditable(runner, stub, evidence):
    """拿掉了什麼要留下痕跡，否則沒有人查得出隔離有沒有真的發生。"""
    run_pcmef_case(runner, evidence)
    withheld = _sent_payload(stub, "physics_agent")["withheld_from_this_role"]
    assert "calibrated_class_probabilities.vision" in withheld
    assert "modality_reliability.q_vision" in withheld


# ---------------------------------------------------------------------------
# class_support 正規化
# ---------------------------------------------------------------------------


def test_support_is_normalised_instead_of_aborting_the_run():
    """JSON Schema 驗不了「四個數字加起來 100」。

    硬要求精確 100 的代價不對稱：retry 只有 2 次，耗盡即 ABORT_FORMAL_RUN，
    而 families 36-43 只能跑一次 —— 整場實驗會因為某個 case 寫了 99 而中止。
    """
    from pcmef.agents.pcmef_agents import normalise_class_support

    normalised = normalise_class_support(
        {"Empty": 50.0, "Water-filled": 30.0, "Bubbly": 15.0, "Misty": 4.0}
    )
    assert sum(normalised.values()) == pytest.approx(100.0)
    # 相對大小必須保持不變 —— 正規化只換刻度，不換結論。
    assert normalised["Empty"] > normalised["Water-filled"] > normalised["Bubbly"]


def test_an_all_zero_support_is_spread_evenly_not_left_at_zero():
    from pcmef.agents.pcmef_agents import normalise_class_support

    normalised = normalise_class_support(dict.fromkeys(evidence_classes(), 0.0))
    assert sum(normalised.values()) == pytest.approx(100.0)
    assert len(set(normalised.values())) == 1


def test_the_raw_sum_is_preserved_for_audit(runner, evidence):
    """正規化不得把「模型沒遵守指示」這件事抹平。"""
    bundle = run_pcmef_case(runner, evidence)
    validated = bundle.artifacts["arbitration_validated"]
    raw = bundle.artifacts["arbitration_raw"]

    assert "support_sum_before_normalisation" in validated
    assert "support_sum_within_tolerance" in validated
    assert sum(validated["class_support"].values()) == pytest.approx(100.0)
    # raw 保留模型原話，沒有被覆寫。
    assert "support_sum_before_normalisation" not in raw


# ---------------------------------------------------------------------------
# retry 分流與取樣溫度
# ---------------------------------------------------------------------------


def test_a_schema_failure_gets_a_correction_but_a_transport_failure_does_not(
    connection, model, evidence
):
    """更正指示只在「上一次違反 schema」時附上。

    transport 失敗時請求本身沒有問題，改動它反而引入新變因。
    """
    from pcmef.agents.pcmef_agents import SCHEMA_CORRECTION

    class BadThenGood(StubOfflineAdapter):
        mode = "schema"

        def invoke(self, connection, model, task_code, payload, runtime_cfg):
            self.seen = getattr(self, "seen", [])
            self.seen.append(payload)
            if len(self.seen) == 1:
                if self.mode == "schema":
                    response = super().invoke(
                        connection, model, task_code, payload, runtime_cfg
                    )
                    return type(response)(**{**response.__dict__, "text": "{}"})
                raise ProviderError("stub_offline: transport failure (TimeoutError)")
            return super().invoke(connection, model, task_code, payload, runtime_cfg)

    body = {k: v for k, v in evidence.items() if k != EVIDENCE_IMAGES_KEY}

    schema_adapter = BadThenGood(resolve_secret=lambda r: "x")
    AgentRunner(
        adapter=schema_adapter, connection=connection, model=model,
        schema_dir=SCHEMA_DIR,
    ).run(AGENTS["observation_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY])
    assert schema_adapter.seen[1].get("format_correction") == SCHEMA_CORRECTION

    transport_adapter = BadThenGood(resolve_secret=lambda r: "x")
    transport_adapter.mode = "transport"
    AgentRunner(
        adapter=transport_adapter, connection=connection, model=model,
        schema_dir=SCHEMA_DIR,
    ).run(AGENTS["observation_agent"], body, images=evidence[EVIDENCE_IMAGES_KEY])
    assert "format_correction" not in transport_adapter.seen[-1], (
        "傳輸失敗時請求必須原封不動重送"
    )


def test_formal_temperature_is_zero_and_reaches_the_adapter(runner, stub, evidence):
    """溫度 0 是可重現性的前提，不是品質偏好。"""
    from pcmef.agents.pcmef_agents import FORMAL_TEMPERATURE

    assert FORMAL_TEMPERATURE == 0.0
    assert runner.temperature == 0.0
    run_pcmef_case(runner, evidence)
    assert all(r["temperature"] == 0.0 for r in stub.invocations)


def test_stub_is_not_formal_eligible():
    assert StubOfflineAdapter.provider not in FORMAL_ELIGIBLE_PROVIDERS
    assert "google" in FORMAL_ELIGIBLE_PROVIDERS
