# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.llm.registry、pcmef.llm.verification、pcmef.llm.snapshot 與
#         pcmef.admin.services 呼叫；讀寫 secrets/vault.json（預設，不進版控），
#         讀取環境變數；解析出的 secret 值只交給 pcmef.agents.provider 的
#         adapter，並同時註冊到 logging_setup 的遮蔽清單。
# 檔案路徑: pcmef/secrets/vault.py
# 產生時間: 2026-08-26 21:20 +08:00
# 版本: v0.1.0
# 功能說明: 把「哪一把 API key」與「key 的內容」分開。系統各處一律只傳遞
#           env:NAME / vault:<uuid> 這種指向字串，真正的值只在要呼叫 provider 的
#           那一刻才被解出來，且解出來就同時登記進 log 遮蔽清單。
# 模組定位: secret 的唯一解析入口，也是 §46 「API key 不回傳、只顯示 masked
#           fingerprint」的實作點。它「不是」加解密實作（那在 crypto.py），
#           也不決定誰有權讀（那在 admin.auth）。
# 主要責任:
#   1. SecretRef.parse() 只接受 env / vault / session 三種 scheme，並擋下明文 key
#   2. SessionSecretStore 保存只存在於行程記憶體的 ephemeral secret
#   3. SecretVault.can_persist 判定有無 master key 與加密後端
#   4. SecretVault.store() / rotate() 寫入密文，rotate 保持 ref 不變只加版號
#   5. SecretVault.resolve() 解出值並註冊到 log 遮蔽；找不到即 fail-fast
#   6. SecretVault.fingerprint() 產生 ****abcd 供 UI 顯示
#   7. ensure_local_master_key() 在本機主控台模式下自動保管一把 master key
# 維護提醒:
#   - 不得新增回傳完整 secret 的 API 給 UI 或 CLI 使用；§46 規定 server 在寫入後
#     不再回傳完整 key，唯一出口是 resolve() 交給 provider adapter。
#   - 不得把 session: ref 寫進 SQLite binding 或任何 lock。它只活在記憶體，
#     重啟後無法解析，寫進去等於製造一個必然壞掉的 formal identity。
#   - 不得在 rotate() 時換掉 vault entry 的 uuid。§46 規定純換 credential
#     不改 provider/model/base_url 時不必 invalidate scientific lock，
#     而 lock 存的正是這個 uuid；換掉它就等於偽造成 identity 變更。
#   - 不得把 vault 檔加入版控；它含密文與 salt，且屬於個別機器的操作狀態。
#     secrets/master.key 同理，且它是加密金鑰本身 —— 進版控等於 vault 檔失效。
#   - 不得把 local_master_key 的預設值改成 True。函式庫預設維持 §46 原文，
#     例外只在本機主控台與 CLI 的組裝點明列；預設值一改，這個例外就從
#     「一處可見的決定」變成「到處都在、沒人記得為什麼」。
#   - 不得在缺 master key 時退回明文保存。roothinks 有那條退路，本系統沒有：
#     §46 的整套遮蔽與指紋設計，前提就是落盤的一定是密文。
#   - v0.1.0 新增：首版 secret_ref 抽象，決策見 NOTE-016。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_secret_vault.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
import secrets as stdlib_secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pcmef.core.logging_setup import register_secret
from pcmef.secrets.crypto import (
    MASTER_KEY_ENV,
    PBKDF2_ITERATIONS,
    CryptoUnavailable,
    crypto_available,
    decrypt,
    derive_key,
    encrypt,
    fingerprint,
    new_salt,
)

__all__ = [
    "SecretError",
    "SecretRef",
    "SessionSecretStore",
    "SecretVault",
    "DEFAULT_VAULT_PATH",
    "LOCAL_MASTER_KEY_FILENAME",
    "ensure_local_master_key",
    "SCHEME_ENV",
    "SCHEME_VAULT",
    "SCHEME_SESSION",
    "PERSISTENT_SCHEMES",
]

DEFAULT_VAULT_PATH = Path("secrets/vault.json")

#: 本機自動保管的 master key 檔名，與 vault 檔同目錄。
LOCAL_MASTER_KEY_FILENAME = "master.key"

SCHEME_ENV = "env"
SCHEME_VAULT = "vault"
SCHEME_SESSION = "session"

#: 可以寫進 SQLite 與 lock 的 scheme。session 刻意不在此列。
PERSISTENT_SCHEMES: frozenset[str] = frozenset({SCHEME_ENV, SCHEME_VAULT})

_ALL_SCHEMES: frozenset[str] = PERSISTENT_SCHEMES | {SCHEME_SESSION}

# 看起來像明文 key 的樣式。用於擋下「把 key 本身當成 secret_ref 貼進來」。
_PLAINTEXT_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^sk-[A-Za-z0-9_\-]{16,}$"),
    re.compile(r"^sk-ant-[A-Za-z0-9_\-]{16,}$"),
    re.compile(r"^AIza[A-Za-z0-9_\-]{30,}$"),
    re.compile(r"^xai-[A-Za-z0-9_\-]{16,}$"),
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretError(RuntimeError):
    """secret_ref 格式非法、指向的 secret 不存在，或在不允許的情況下要求持久化。"""


def _looks_like_plaintext_key(text: str) -> bool:
    return any(pattern.match(text) for pattern in _PLAINTEXT_KEY_PATTERNS)


@dataclass(frozen=True)
class SecretRef:
    """指向一個 secret 的參考，本身不含 secret 值。"""

    scheme: str
    identifier: str

    def __post_init__(self) -> None:
        if self.scheme not in _ALL_SCHEMES:
            raise SecretError(
                f"unknown secret_ref scheme {self.scheme!r}; "
                f"expected one of {sorted(_ALL_SCHEMES)}"
            )
        if not self.identifier:
            raise SecretError(f"secret_ref {self.scheme}: has an empty identifier")
        if self.scheme == SCHEME_ENV and _looks_like_plaintext_key(self.identifier):
            # 必須排在 _ENV_NAME 之前。API key 幾乎都是純英數，因此
            # 「AIzaSy...」這種字串**完全符合**合法環境變數名的語法 ——
            # 只靠 _ENV_NAME 擋不住 `env:<真的 key>`，而那正是把 key 當成
            # 變數名存進 registry 的路徑。實測外洩過一次：key 進了 registry，
            # 之後 resolve 失敗的錯誤訊息把它一路帶進 flash 與網址列。
            #
            # 訊息刻意**不回吐 identifier** —— 它就是那把 key，而錯誤訊息
            # 會被寫進 flash、log 與稽核記錄。
            raise SecretError(
                "the env: reference contains what looks like the API key itself, "
                "not a variable name. Put the key in an environment variable and "
                "reference it by NAME, e.g. env:GEMINI_API_KEY. "
                "If this key was already submitted anywhere, treat it as "
                "compromised and rotate it (SRC-SAI §43, NFR-08)."
            )
        if self.scheme == SCHEME_ENV and not _ENV_NAME.match(self.identifier):
            raise SecretError(
                f"env secret_ref must name an environment variable, got "
                f"{self.identifier!r}. If you pasted the key itself, stop: "
                "the key must live in the environment, not in the config."
            )

    def __str__(self) -> str:
        return f"{self.scheme}:{self.identifier}"

    @property
    def is_persistent(self) -> bool:
        return self.scheme in PERSISTENT_SCHEMES

    @classmethod
    def parse(cls, text: str) -> SecretRef:
        """解析 secret_ref 字串。明文 key 一律拒絕。"""
        if not isinstance(text, str) or not text.strip():
            raise SecretError("secret_ref must be a non-empty string")
        candidate = text.strip()
        if _looks_like_plaintext_key(candidate):
            raise SecretError(
                "this looks like a plaintext API key, not a reference. "
                "secret_ref must be env:<VARNAME> or vault:<uuid>; the value "
                "itself must never be stored (SRC-SAI §43, NFR-08)."
            )
        if ":" not in candidate:
            raise SecretError(
                f"secret_ref {candidate!r} has no scheme; expected "
                "env:<VARNAME> or vault:<uuid>"
            )
        scheme, identifier = candidate.split(":", 1)
        return cls(scheme=scheme.strip(), identifier=identifier.strip())


class SessionSecretStore:
    """只存在於行程記憶體的 secret 暫存區。

    §46 允許「無 master key 時仍可做 ephemeral session verify」。這個類別就是
    那條路徑：secret 進得來、能拿去 probe，但永遠不落盤，行程結束即消失。
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, secret: str) -> SecretRef:
        if not isinstance(secret, str) or not secret:
            raise SecretError("cannot store an empty ephemeral secret")
        token = uuid.uuid4().hex
        self._values[token] = secret
        register_secret(secret)
        return SecretRef(SCHEME_SESSION, token)

    def get(self, ref: SecretRef) -> str:
        if ref.scheme != SCHEME_SESSION:
            raise SecretError(f"{ref} is not a session ref")
        try:
            return self._values[ref.identifier]
        except KeyError:
            raise SecretError(
                f"ephemeral secret {ref} is gone; session secrets do not survive "
                "a restart. Store it as env: or vault: to make it persistent."
            ) from None

    def discard(self, ref: SecretRef) -> None:
        self._values.pop(ref.identifier, None)

    def __len__(self) -> int:
        return len(self._values)


def ensure_local_master_key(directory: str | Path) -> str:
    """讀取本機 master key，不存在就產生一把並存進 `<directory>/master.key`。

    §46 原文是「master key 由 environment / OS secret 供應，不由本系統產生」。
    這個函式是那條規則在**本機單人主控台**上的明列例外，理由是：不設環境變數
    時 `can_persist` 為 False，網頁上的 API Key 欄位就是灰的，於是操作者只剩
    `env:<NAME>` 一條路；但那個環境變數在容器裡同樣沒設，結果是憑證顯示
    `(unresolvable)`、Fetch 失敗、沒有線路鎖得起來、四個 agent 全部綁不上。
    一顆沒人告訴你要設的環境變數，把整條 LAVA 流程從頭鎖到尾。

    **這不是把 secret 變成明文。** 產生的是加密用的 master key，API key 本身
    仍然只以 Fernet 密文存在 vault 檔裡，registry 與 lock 存的仍然只有
    `vault:<uuid>`。與 roothinks 的差別正在這裡 —— 它在沒有 FERNET_KEY 時
    退回明文存 DB，本系統不設那條退路。

    環境變數一旦設了就優先，本檔完全不參與；因此要接手 KMS 或 OS keychain
    時不必先移除這條路徑。
    """
    folder = Path(directory)
    path = folder / LOCAL_MASTER_KEY_FILENAME
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    folder.mkdir(parents=True, exist_ok=True)
    key = stdlib_secrets.token_urlsafe(48)
    path.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows 上 chmod 幾乎是空操作，而這把 key 的保護本來就依賴
        # 使用者家目錄的權限。失敗不該讓主控台起不來。
        pass
    return key


class SecretVault:
    """secret 的解析入口，並在有 master key 時提供本機加密保存。"""

    def __init__(
        self,
        path: str | Path = DEFAULT_VAULT_PATH,
        master_key: str | None = None,
        environ: Mapping[str, str] | None = None,
        session_store: SessionSecretStore | None = None,
        local_master_key: bool = False,
    ) -> None:
        """local_master_key 預設 False —— 函式庫的預設值維持 §46 原文。

        只有本機主控台與 CLI 明確傳 True（見 admin/app.py 與 cli.py）。
        **兩邊必須一致**：UI 用本機金鑰加密存進去的憑證，CLI 產 snapshot 時
        要解得開；只有一邊開啟的話，`pcmef llm snapshot` 會在 fingerprint
        階段就失敗，而那個錯誤看起來完全不像「兩邊的金鑰來源不同」。
        """
        self.path = Path(path)
        self._environ = environ if environ is not None else os.environ
        supplied = (
            master_key if master_key is not None else self._environ.get(MASTER_KEY_ENV)
        )
        if supplied:
            self.master_key_source = "environment" if master_key is None else "explicit"
        elif local_master_key and crypto_available():
            # crypto_available() 先擋一次：沒有 cryptography 套件時就算產生了
            # 金鑰也沒有東西能用它加密，徒然在磁碟上留一個誤導性的檔案。
            supplied = ensure_local_master_key(self.path.parent)
            self.master_key_source = "local-file"
        else:
            self.master_key_source = "none"
        self._master_key = supplied
        self.session = session_store or SessionSecretStore()

    # -- vault 檔 ---------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        """讀取 vault 檔；不存在時建立只含 salt、不含任何 secret 的骨架。

        沒有 master key 也能建立這個骨架：裡面只有兩個隨機 salt，
        而 fingerprint 需要 salt 才能穩定，包含 env: 形式的 secret 也是。
        """
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "entries" not in data:
                raise SecretError(f"vault file {self.path} is malformed")
            return data
        data = {
            "vault_version": 1,
            "kdf": {
                "algorithm": "pbkdf2_hmac_sha256",
                "iterations": PBKDF2_ITERATIONS,
                "salt": new_salt(),
            },
            "fingerprint_salt": new_salt(),
            "entries": {},
        }
        self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _key(self, data: dict[str, Any]) -> bytes:
        if not self._master_key:
            raise SecretError(
                f"no master key: set {MASTER_KEY_ENV} before storing or reading a "
                "persistent secret. Ephemeral session verification remains available."
            )
        return derive_key(self._master_key, str(data["kdf"]["salt"]))

    # -- 能力查詢 ---------------------------------------------------------

    @property
    def can_persist(self) -> bool:
        """是否能保存持久化 secret：需要同時有 master key 與加密後端。"""
        return bool(self._master_key) and crypto_available()

    def assert_can_persist(self) -> None:
        if not self._master_key:
            raise SecretError(
                f"refusing to persist a secret without {MASTER_KEY_ENV} "
                "(SRC-SAI §46). Use an ephemeral session verify instead, or set "
                "the master key and try again."
            )
        if not crypto_available():
            raise CryptoUnavailable(
                "refusing to persist a secret without the 'cryptography' package; "
                "the correct fix is to install it, not to weaken the cipher."
            )

    # -- 寫入 -------------------------------------------------------------

    def store(self, secret: str) -> SecretRef:
        """加密保存一個新 secret，回傳 vault:<uuid> 參考。"""
        if not isinstance(secret, str) or not secret:
            raise SecretError("cannot store an empty secret")
        self.assert_can_persist()
        data = self._read()
        key = self._key(data)
        identifier = str(uuid.uuid4())
        data["entries"][identifier] = {
            "ciphertext": encrypt(secret, key),
            "fingerprint": fingerprint(secret, str(data["fingerprint_salt"])),
            "secret_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rotated_at": None,
        }
        self._write(data)
        register_secret(secret)
        return SecretRef(SCHEME_VAULT, identifier)

    def rotate(self, ref: SecretRef, secret: str) -> int:
        """替換既有 entry 的 secret，回傳新的 secret_version。

        ref 本身刻意保持不變：§46 規定純 credential rotation 不改動
        provider/model/base_url/runtime identity 時，不必 invalidate scientific
        lock，而 lock 內存的正是這個 ref。換掉 ref 會讓一次單純的換 key
        看起來像 binding identity 變更，逼出一次不必要的重新 freeze。
        """
        if ref.scheme != SCHEME_VAULT:
            raise SecretError(
                f"only vault: refs can be rotated in place, got {ref}. "
                "An env: ref is rotated by changing the environment variable."
            )
        self.assert_can_persist()
        data = self._read()
        entry = data["entries"].get(ref.identifier)
        if entry is None:
            raise SecretError(f"vault entry {ref} not found in {self.path}")
        key = self._key(data)
        entry["ciphertext"] = encrypt(secret, key)
        entry["fingerprint"] = fingerprint(secret, str(data["fingerprint_salt"]))
        entry["secret_version"] = int(entry.get("secret_version", 1)) + 1
        entry["rotated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(data)
        register_secret(secret)
        return int(entry["secret_version"])

    # -- 讀取 -------------------------------------------------------------

    def resolve(self, ref: SecretRef | str) -> str:
        """解出 secret 值並登記到 log 遮蔽清單。

        這是全系統唯一取得 secret 明文的出口，所以遮蔽登記放在這裡而不是
        呼叫端 —— 呼叫端會忘記，這裡不會。
        """
        reference = SecretRef.parse(ref) if isinstance(ref, str) else ref
        if reference.scheme == SCHEME_SESSION:
            value = self.session.get(reference)
        elif reference.scheme == SCHEME_ENV:
            value = self._environ.get(reference.identifier, "")
            if not value:
                raise SecretError(
                    f"environment variable {reference.identifier!r} referenced by "
                    f"{reference} is unset or empty"
                )
        else:
            data = self._read()
            entry = data["entries"].get(reference.identifier)
            if entry is None:
                raise SecretError(f"vault entry {reference} not found in {self.path}")
            value = decrypt(str(entry["ciphertext"]), self._key(data))
        register_secret(value)
        return value

    def fingerprint(self, ref: SecretRef | str) -> str:
        """回傳 ****abcd 遮蔽指紋。vault entry 直接讀已存的值，不必解密。"""
        reference = SecretRef.parse(ref) if isinstance(ref, str) else ref
        data = self._read()
        if reference.scheme == SCHEME_VAULT:
            entry = data["entries"].get(reference.identifier)
            if entry is None:
                raise SecretError(f"vault entry {reference} not found in {self.path}")
            return str(entry["fingerprint"])
        return fingerprint(self.resolve(reference), str(data["fingerprint_salt"]))

    def secret_version(self, ref: SecretRef | str) -> int:
        """vault entry 的 secret 版本；env / session 一律視為 1。"""
        reference = SecretRef.parse(ref) if isinstance(ref, str) else ref
        if reference.scheme != SCHEME_VAULT:
            return 1
        entry = self._read()["entries"].get(reference.identifier)
        if entry is None:
            raise SecretError(f"vault entry {reference} not found in {self.path}")
        return int(entry.get("secret_version", 1))
