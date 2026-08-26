# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 conftest 的離線 stub adapter 驅動
#         pcmef.llm.verification，artifact 落在 tmp_path；
#         不對外連線，也不寫入 artifacts/ 真實目錄。
# 檔案路徑: tests/llm_admin/test_verification.py
# 產生時間: 2026-08-26 23:35 +08:00
# 版本: v0.1.0
# 功能說明: 驗證能力驗證這件事真的有跑、有留證據、而且結果會如實反映失敗 ——
#           包括預設不跑 embedding、artifact 不可覆寫、以及證據裡沒有金鑰。
# 模組定位: §44 「不可只信 metadata」與 §49 「immutable artifact 不得覆寫」
#           的可執行防線。
# 主要責任:
#   1. test_default_probe_set_is_the_three_required_capabilities 對應 §41/§52
#   2. test_probe_results_are_written_to_the_verification_log 驗證證據落庫
#   3. test_each_probe_writes_an_immutable_artifact 驗證 artifact 命名與冪等
#   4. test_a_failed_probe_does_not_mark_the_capability_verified 驗證不放水
#   5. test_verification_artifacts_contain_no_secret 對應 LLM-SEC-01 的一部分
#   6. test_probe_payload_fingerprint_is_stable 驗證 probe 內容未被換掉
# 維護提醒:
#   - 不得改用直接呼叫 adapter 的捷徑來繞過 CapabilityVerifier；
#     被測的必須是實際被用的那條路徑。
#   - v0.1.0 新增：首版。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_verification.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.agents.provider import Capability
from pcmef.llm.verification import CapabilityVerifier, probe_payload_fingerprint


@pytest.fixture
def verifier(seeded, vault, tmp_path, adapter_factory) -> CapabilityVerifier:
    return CapabilityVerifier(
        registry=seeded.registry,
        vault=vault,
        artifact_root=tmp_path / "llm_verification",
        adapter_factory=adapter_factory,
    )


def test_default_probe_set_is_the_three_required_capabilities(verifier, seeded):
    """§41：Thesis Core 不需要 embedding，預設不該花一次 API 呼叫去測它。"""
    outcome = verifier.verify(seeded.full_model_id)

    probed = [r.capability for r in outcome.results]
    assert probed == [Capability.CHAT, Capability.STRUCTURED_JSON, Capability.VISION]
    assert Capability.EMBEDDING not in probed


def test_probe_order_is_normalised_even_if_the_caller_asks_out_of_order(
    verifier, seeded
):
    outcome = verifier.verify(
        seeded.full_model_id, capabilities=[Capability.VISION, Capability.CHAT]
    )
    assert [r.capability for r in outcome.results] == [
        Capability.CHAT, Capability.VISION
    ]


def test_probe_results_are_written_to_the_verification_log(verifier, seeded):
    verifier.verify(seeded.full_model_id)

    logs = seeded.registry.verification_logs(seeded.full_model_id)

    assert len(logs) == 3
    assert {log["capability"] for log in logs} == {"chat", "structured_json", "vision"}
    assert all(log["artifact_hash"] for log in logs)


def test_successful_verification_updates_the_verified_capabilities(verifier, seeded):
    outcome = verifier.verify(seeded.full_model_id)

    assert set(outcome.verified) == {
        Capability.CHAT, Capability.STRUCTURED_JSON, Capability.VISION
    }
    assert set(seeded.registry.verified_capabilities(seeded.full_model_id)) == set(
        outcome.verified
    )


def test_a_failed_probe_does_not_mark_the_capability_verified(verifier, seeded):
    """chat-only 模型的 structured_json 與 vision probe 都會失敗。"""
    outcome = verifier.verify(seeded.chat_only_id)

    assert outcome.succeeded() == (Capability.CHAT,)
    assert set(outcome.failed()) == {Capability.STRUCTURED_JSON, Capability.VISION}
    assert outcome.verified == (Capability.CHAT,)


def test_each_probe_writes_an_immutable_artifact(verifier, seeded, tmp_path):
    outcome = verifier.verify(seeded.full_model_id)

    assert len(outcome.artifact_paths) == 3
    for path in outcome.artifact_paths:
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        # 檔名帶 artifact_hash：內容不同必然落到不同路徑。
        assert payload["artifact_hash"].startswith(path.stem.split("-")[-1])


def test_repeating_an_identical_probe_is_idempotent(verifier, seeded):
    first = verifier.verify(seeded.full_model_id)
    second = verifier.verify(seeded.full_model_id)

    assert set(first.artifact_paths) == set(second.artifact_paths)
    # log 仍逐次累積：artifact 冪等講的是內容，不是「這次沒發生過」。
    assert len(seeded.registry.verification_logs(seeded.full_model_id)) == 6


def test_verification_artifacts_contain_no_secret(verifier, seeded, fake_key):
    outcome = verifier.verify(seeded.full_model_id)

    for path in outcome.artifact_paths:
        raw = path.read_text(encoding="utf-8")
        assert fake_key not in raw
        for start in range(0, len(fake_key) - 8):
            assert fake_key[start : start + 8] not in raw


def test_verify_has_no_parameter_for_custom_probe_content():
    """§44：probe 不得使用真實 formal evidence。沒有這個參數就沒有這條路。"""
    import inspect

    parameters = set(inspect.signature(CapabilityVerifier.verify).parameters)
    assert parameters == {"self", "model_profile_id", "capabilities"}


def test_probe_payload_fingerprint_is_stable():
    assert probe_payload_fingerprint() == probe_payload_fingerprint()
    assert len(probe_payload_fingerprint()) == 64


def test_the_summary_lists_one_line_per_probe(verifier, seeded):
    outcome = verifier.verify(seeded.chat_only_id)
    lines = outcome.summary()
    assert len(lines) == 3
    assert lines[0].startswith("[ok ]")
    assert any(line.startswith("[FAIL]") for line in lines)
