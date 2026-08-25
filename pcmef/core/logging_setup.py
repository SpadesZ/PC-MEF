# 檔案路徑: pcmef/core/logging_setup.py
# 模組定位: 全系統 logging 設定與 secret 遮蔽過濾器。
# 功能說明: 建立統一 log 格式，並在寫出前遮蔽已註冊的 secret 值與常見 API key 樣式。
# 主要責任: 讓 NFR-08「API key 不寫入 repo/manifest/log」在 log 這條路徑上有實際攔截點。
# 呼叫來源: cli 進入點、experiments runner、llm.admin 服務啟動時各呼叫一次。
# 輸入契約: register_secret() 收到的字串會被視為機密；log 訊息可為任意物件。
# 輸出契約: 遮蔽後的 log 記錄；被遮蔽處以 [REDACTED] 取代，不保留任何前綴片段。
# 安全邊界: 本過濾器是最後一道防線，不是唯一防線；呼叫端仍不得刻意把 secret 放進訊息。
# 維護提醒: 新增 provider 時同步補上其 key 的樣式 regex，並在 tests/secret 加對應案例。
# 版本: v0.1.0 / 2026-08-25
# ----------------------------------------------------------------------------------------------------

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

__all__ = ["SecretRedactionFilter", "register_secret", "setup_logging"]

_REGISTERED_SECRETS: set[str] = set()

# 常見 provider key 樣式。即使 secret 未經 register_secret() 註冊也能攔下。
# 刻意不比對過短的字串：長度門檻避免把一般英數字誤遮成 [REDACTED] 而讓 log 失去診斷價值。
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),           # OpenAI / OpenRouter
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),        # Anthropic
    re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),           # Google
    re.compile(r"xai-[A-Za-z0-9_\-]{16,}"),           # xAI
    re.compile(r"(?i)([?&]key=)[^\s'\"&]+"),          # query string 內的 key
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
    re.compile(r"(?i)(x-goog-api-key:\s*)\S+"),
    re.compile(r"(?i)(x-api-key:\s*)\S+"),
)


def register_secret(value: str) -> None:
    """註冊一個必須從 log 中遮蔽的字串。

    只註冊足夠長的值：太短的字串當成 secret 會在整份 log 造成大量誤遮，
    反而讓真正的問題難以診斷。
    """
    if isinstance(value, str) and len(value) >= 8:
        _REGISTERED_SECRETS.add(value)


class SecretRedactionFilter(logging.Filter):
    """在 log 記錄寫出前遮蔽 secret。

    同時處理 record.msg 與 record.args —— 只處理 msg 會漏掉
    logger.info("key=%s", api_key) 這種延遲格式化的寫法，
    而那正是最容易不小心寫出來的形式。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: self._redact(value) for key, value in record.args.items()
                }
            else:
                record.args = tuple(self._redact(arg) for arg in record.args)
        return True

    @staticmethod
    def _redact(value):
        if not isinstance(value, str):
            if isinstance(value, (int, float, bool)) or value is None:
                return value
            value_str = str(value)
            redacted = SecretRedactionFilter._redact_str(value_str)
            return redacted if redacted != value_str else value
        return SecretRedactionFilter._redact_str(value)

    @staticmethod
    def _redact_str(text: str) -> str:
        for secret in _REGISTERED_SECRETS:
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        for pattern in _SECRET_PATTERNS:
            if pattern.groups:
                text = pattern.sub(r"\1[REDACTED]", text)
            else:
                text = pattern.sub("[REDACTED]", text)
        return text


def setup_logging(
    level: int = logging.INFO, log_file: str | Path | None = None
) -> logging.Logger:
    """設定 root logger。重複呼叫時只會有一組 handler。"""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redaction = SecretRedactionFilter()

    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redaction)
    root.addHandler(stream_handler)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    return root
