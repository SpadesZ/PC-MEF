# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；操作 core.logging_setup 的過濾器與 tmp_path 下的暫時
#         log 檔；結果為 LLM-SEC-01「manifest / report / log 全域掃描無 secret」的
#         log 路徑判定，不產生任何常駐 artifact。
# 檔案路徑: tests/secret/test_log_redaction.py
# 產生時間: 2026-08-25 22:50 +08:00
# 版本: v0.1.0
# 功能說明: 驗證各家 provider 的 API key 在寫進 log 之前確實被換成 [REDACTED]，
#           包含延遲格式化參數、巢狀物件、URL query 與授權標頭等容易漏掉的路徑。
# 模組定位: secret 遮蔽的驗收測試。它只驗證 log 這一條路徑；manifest 與 lock 的
#           secret 阻擋由 tests/unit/test_config_and_locks.py 負責。
# 主要責任:
#   1. test_registered_secret_is_redacted() 驗證已註冊的實際值被遮蔽
#   2. test_lazy_format_arguments_are_redacted() 驗證 record.args 也被處理
#   3. test_provider_key_patterns_are_redacted() 涵蓋四家 provider 的 key 樣式
#   4. test_query_string_and_auth_headers_are_redacted() 涵蓋 URL 與標頭形式
#   5. test_no_prefix_fragment_survives() 驗證不保留任何前綴片段
# 維護提醒:
#   - 不得用真實 API key 當測試資料；本檔所有 key 皆為構造值。
#   - 不得只斷言 "[REDACTED]" 出現；必須同時斷言原始 key 的任何連續片段都不存在，
#     否則部分遮蔽會被誤判為通過。
#   - 新增 provider 時要在此加一條對應樣式的案例。
#   - v0.1.0 新增：首版 log 遮蔽驗收。
# 驗證方式:
#   - py -3.10 -m pytest tests/secret/test_log_redaction.py -v
# ------------------------------------------------------------

from __future__ import annotations

import logging

import pytest

from pcmef.core.logging_setup import (
    SecretRedactionFilter,
    register_secret,
    setup_logging,
)

# 全部為構造值，不是任何真實憑證。
FAKE_KEYS = {
    "openai": "sk-proj-0123456789abcdefghijklmnop",
    "anthropic": "sk-ant-api03-0123456789abcdefghijklmnop",
    "google": "AIzaSyD0123456789abcdefghijklmnopqrstuv",
    "xai": "xai-0123456789abcdefghijklmnop",
}


def _emit(caplog, message, *args) -> str:
    """送一筆 log 通過遮蔽過濾器，回傳實際會被寫出的文字。"""
    logger = logging.getLogger("pcmef.test.redaction")
    logger.handlers.clear()
    logger.propagate = True
    redaction = SecretRedactionFilter()
    for handler in caplog.handler, :
        handler.addFilter(redaction)
    logger.addFilter(redaction)
    with caplog.at_level(logging.INFO, logger="pcmef.test.redaction"):
        logger.info(message, *args)
    return caplog.text


def _assert_fully_redacted(text: str, secret: str) -> None:
    """斷言 secret 的任何一段連續 8 字元都不在輸出中。

    只檢查完整字串是不夠的：部分遮蔽（例如只遮到第一個某字母為止）
    仍會外洩足以辨識的片段，而那正是參考實作踩過的坑（NOTE-007）。
    """
    assert secret not in text
    for start in range(0, max(1, len(secret) - 8)):
        fragment = secret[start : start + 8]
        assert fragment not in text, (
            f"secret fragment {fragment!r} survived redaction in: {text!r}"
        )


def test_registered_secret_is_redacted(caplog):
    secret = "super-secret-value-not-a-known-pattern-12345"
    register_secret(secret)
    text = _emit(caplog, f"connecting with {secret}")
    assert "[REDACTED]" in text
    _assert_fully_redacted(text, secret)


def test_lazy_format_arguments_are_redacted(caplog):
    """logger.info("key=%s", api_key) 是最容易不小心寫出來的洩漏形式。"""
    secret = FAKE_KEYS["openai"]
    text = _emit(caplog, "calling provider with key=%s", secret)
    _assert_fully_redacted(text, secret)


@pytest.mark.parametrize("provider,secret", sorted(FAKE_KEYS.items()))
def test_provider_key_patterns_are_redacted(caplog, provider, secret):
    """未經 register_secret() 註冊也必須靠樣式攔下。"""
    text = _emit(caplog, f"provider {provider} returned 401 for {secret}")
    _assert_fully_redacted(text, secret)


def test_query_string_key_is_redacted(caplog):
    """NOTE-007：Google 把 key 放 URL query 是既有實作的洩漏來源。"""
    secret = FAKE_KEYS["google"]
    text = _emit(
        caplog,
        f"HTTP 400 from https://generativelanguage.googleapis.com/v1beta/models?key={secret}",
    )
    _assert_fully_redacted(text, secret)


@pytest.mark.parametrize(
    "header",
    [
        "Authorization: Bearer {secret}",
        "x-goog-api-key: {secret}",
        "x-api-key: {secret}",
    ],
)
def test_auth_headers_are_redacted(caplog, header):
    secret = FAKE_KEYS["anthropic"]
    text = _emit(caplog, header.format(secret=secret))
    _assert_fully_redacted(text, secret)


def test_secret_inside_a_non_string_argument_is_redacted(caplog):
    """例外物件與 dict 被格式化成字串時同樣會帶出 secret。"""
    secret = FAKE_KEYS["xai"]
    error = RuntimeError(f"request failed for {secret}")
    text = _emit(caplog, "provider error: %s", error)
    _assert_fully_redacted(text, secret)


def test_short_values_are_not_registered_as_secrets():
    """過短的值若被當成 secret 會在整份 log 造成大量誤遮。"""
    register_secret("abc")
    assert SecretRedactionFilter._redact_str("abc def") == "abc def"


def test_setup_logging_attaches_the_filter_to_every_handler(tmp_path):
    secret = "another-secret-value-for-file-handler-test"
    register_secret(secret)
    log_file = tmp_path / "run.log"
    root = setup_logging(log_file=log_file)
    try:
        logging.getLogger("pcmef.test.file").info("writing %s", secret)
        for handler in root.handlers:
            handler.flush()
        _assert_fully_redacted(log_file.read_text(encoding="utf-8"), secret)
    finally:
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)


def test_setup_logging_does_not_accumulate_handlers():
    first = setup_logging()
    count_after_first = len(first.handlers)
    second = setup_logging()
    assert len(second.handlers) == count_after_first
