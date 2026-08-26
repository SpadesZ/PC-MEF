# PC-MEF Research System source maintenance contract
# 上下游: 只由 pcmef.secrets.vault 呼叫；master key 來自環境變數
#         PCMEF_SECRET_MASTER_KEY，密文回寫到 vault 檔的 entries 欄位。
#         本檔不接觸 SQLite、manifest 或 lock。
# 檔案路徑: pcmef/secrets/crypto.py
# 產生時間: 2026-08-26 21:10 +08:00
# 版本: v0.1.0
# 功能說明: 把 API key 之類的字串用 master key 加密成密文、以及把密文解回原字串。
#           另提供以雜湊產生的四字元指紋，讓 UI 能顯示 ****abcd 指出「裝的是哪一把」，
#           而這四個字元本身不透露 key 的任何片段。
# 模組定位: vault 的加解密後端，唯一與 cryptography 套件耦合之處。
#           它「不是」金鑰管理器 —— master key 的保管由作業系統/環境負責。
# 主要責任:
#   1. CryptoUnavailable 在缺少 cryptography 套件時被拋出，供上層 fail-closed
#   2. derive_key() 以 PBKDF2-HMAC-SHA256 從 master key 與 salt 導出對稱金鑰
#   3. encrypt() / decrypt() 以 Fernet（AES-CBC + HMAC，authenticated）處理密文
#   4. fingerprint() 以 HMAC-SHA256(salt, secret) 前四個 hex 產生 ****abcd
#   5. new_salt() 產生每個 vault 各自獨立的隨機 salt
# 維護提醒:
#   - 不得改用自製串流加密取代 Fernet。缺 cryptography 時正確行為是 fail-closed
#     並要求安裝，不是退回一個沒人審查過的自製構造。
#   - 不得讓 fingerprint 取 secret 的前綴或後綴字元。§46 明訂只顯示 masked
#     fingerprint、不顯示 key prefix；直接截字串等於公開部分金鑰。
#   - 不得移除 fingerprint 的 salt。無 salt 的四字元雜湊會變成離線驗證預言機：
#     任何人拿一把候選 key 就能比對是否命中。
#   - 不得降低 PBKDF2 迭代次數；它是 master key 強度不足時唯一的緩衝。
#   - v0.1.0 新增：首版，決策見 NOTE-016。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_secret_vault.py -k "crypto or fingerprint"
# ------------------------------------------------------------

from __future__ import annotations

import base64
import hashlib
import hmac
import os

__all__ = [
    "CryptoUnavailable",
    "MASTER_KEY_ENV",
    "PBKDF2_ITERATIONS",
    "derive_key",
    "encrypt",
    "decrypt",
    "fingerprint",
    "new_salt",
    "crypto_available",
]

# master key 的來源。§46：master key 由 environment / OS secret 供應，
# 不由本系統產生也不由本系統保管。
MASTER_KEY_ENV = "PCMEF_SECRET_MASTER_KEY"

PBKDF2_ITERATIONS = 480_000

_FINGERPRINT_HEX_CHARS = 4


class CryptoUnavailable(RuntimeError):
    """缺少 cryptography 套件，因此無法建立或讀取本機加密 vault。

    正確處置是安裝 `pip install -e ".[admin]"`，或改用 env: 形式的 secret_ref
    把 key 留在環境變數裡。**不是**改寫成自製加密。
    """


def crypto_available() -> bool:
    try:
        import cryptography.fernet  # noqa: F401
    except ImportError:
        return False
    return True


def _fernet(key: bytes):
    try:
        from cryptography.fernet import Fernet
    except ImportError as error:  # pragma: no cover - 取決於安裝環境
        raise CryptoUnavailable(
            "the local encrypted secret vault requires the 'cryptography' package "
            "(pip install -e \".[admin]\"). Until it is installed, use env: "
            "secret refs so the key stays in the environment and is never persisted."
        ) from error
    return Fernet(key)


def new_salt(n_bytes: int = 16) -> str:
    """產生新的隨機 salt（hex）。每個 vault 檔各自獨立。"""
    return os.urandom(n_bytes).hex()


def derive_key(master_key: str, salt_hex: str) -> bytes:
    """由 master key 與 vault 專屬 salt 導出 Fernet 金鑰。

    直接把 master key 當對稱金鑰用是不行的：使用者提供的字串長度與熵都不受控，
    而 Fernet 要求 32 bytes。PBKDF2 同時解決長度與低熵兩件事。
    """
    if not isinstance(master_key, str) or not master_key:
        raise CryptoUnavailable(
            f"no master key supplied; set {MASTER_KEY_ENV} in the environment"
        )
    raw = hashlib.pbkdf2_hmac(
        "sha256", master_key.encode("utf-8"), bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS, dklen=32,
    )
    return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: str, key: bytes) -> str:
    """加密成 URL-safe 的 token 字串。"""
    return _fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str, key: bytes) -> str:
    """解密。金鑰錯誤或密文被竄改都會拋 cryptography 的 InvalidToken。"""
    return _fernet(key).decrypt(token.encode("ascii")).decode("utf-8")


def fingerprint(secret: str, salt_hex: str) -> str:
    """回傳 ****abcd 形式的遮蔽指紋。

    刻意用 HMAC 而非截取 secret 的字元：§46 要求 UI 只顯示 masked fingerprint，
    不顯示 key prefix 或完整值。salt 讓這四個字元只在本機有意義 ——
    沒有 salt 的話，任何人拿一把候選 key 算個雜湊就能確認是否命中。
    """
    digest = hmac.new(
        bytes.fromhex(salt_hex), secret.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return "****" + digest[:_FINGERPRINT_HEX_CHARS]
