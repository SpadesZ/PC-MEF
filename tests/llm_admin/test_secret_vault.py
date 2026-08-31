# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 tmp_path 建立臨時 vault 檔與假環境變數，
#         驗證 pcmef.secrets.vault 與 pcmef.secrets.crypto 的行為；
#         不觸碰真實的 secrets/vault.json，也不對外連線。
# 檔案路徑: tests/llm_admin/test_secret_vault.py
# 產生時間: 2026-08-26 21:50 +08:00
# 版本: v0.1.0
# 功能說明: 驗證 secret 只以參考形式流動 —— 明文 key 被擋、vault 檔內找不到原字串、
#           UI 指紋不洩漏 key 的任何片段、換 key 不會換掉參考本身、
#           以及沒有 master key 時不准持久化但仍可做一次性驗證。
# 模組定位: §46 Secret Storage 與 NFR-08 的可執行防線。它「不是」加密演算法的
#           正確性測試（那由 cryptography 套件自身負責）。
# 主要責任:
#   1. test_parse_* 驗證 SecretRef 只接受三種 scheme 並擋下明文 key
#   2. test_vault_file_never_contains_the_plaintext_secret 驗證落盤內容無明文
#   3. test_fingerprint_* 驗證指紋不含 key 片段、對不同 key 相異、對同 key 穩定
#   4. test_rotate_keeps_the_same_ref_and_bumps_version 驗證 §46 的 rotation 語意
#   5. test_persisting_without_a_master_key_is_refused 驗證 fail-closed
#   6. test_session_secret_* 驗證 ephemeral 路徑可用且不可持久化
#   7. test_resolve_registers_the_secret_for_log_redaction 驗證遮蔽自動掛上
# 維護提醒:
#   - 不得改用「指紋是 key 的後四碼」的實作來讓測試通過；§46 只允許 masked
#     fingerprint，測試斷言的正是「指紋不是 key 的任何子字串」。
#   - 不得在測試裡設定真實的 PCMEF_SECRET_MASTER_KEY 或真實 API key。
#   - v0.1.0 新增：首版，對應 NOTE-016。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_secret_vault.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json

import pytest

from pcmef.core import logging_setup
from pcmef.secrets.crypto import MASTER_KEY_ENV, crypto_available, fingerprint, new_salt
from pcmef.secrets.vault import (
    LOCAL_MASTER_KEY_FILENAME,
    SCHEME_SESSION,
    SCHEME_VAULT,
    SecretError,
    SecretRef,
    SecretVault,
    SessionSecretStore,
    ensure_local_master_key,
)

# 測試用假值。刻意做成明顯的假字串，但長度/前綴符合真實樣式，
# 這樣「像 key 的東西」的判定路徑才真的被走到。
FAKE_OPENAI_KEY = "sk-" + "T3stK3yMaterial" * 3
FAKE_GOOGLE_KEY = "AIza" + "T3stK3yMaterialZ" * 2
MASTER = "unit-test-master-key-not-a-real-secret"


def _vault(tmp_path, master_key=MASTER, environ=None):
    return SecretVault(
        path=tmp_path / "vault.json",
        master_key=master_key,
        environ=environ if environ is not None else {},
    )


# ---------------------------------------------------------------------------
# SecretRef 解析
# ---------------------------------------------------------------------------


def test_parse_accepts_env_and_vault_schemes():
    assert SecretRef.parse("env:GEMINI_API_KEY").scheme == "env"
    assert SecretRef.parse("env:GEMINI_API_KEY").identifier == "GEMINI_API_KEY"
    assert SecretRef.parse("vault:abc-123").scheme == SCHEME_VAULT


def test_parse_rejects_a_plaintext_key_pasted_as_a_reference():
    """最容易發生的錯誤：把 key 本身貼到 secret_ref 欄位。"""
    for key in (FAKE_OPENAI_KEY, FAKE_GOOGLE_KEY):
        with pytest.raises(SecretError, match="plaintext API key"):
            SecretRef.parse(key)


def test_parse_rejects_a_missing_or_unknown_scheme():
    with pytest.raises(SecretError, match="no scheme"):
        SecretRef.parse("GEMINI_API_KEY")
    with pytest.raises(SecretError, match="unknown secret_ref scheme"):
        SecretRef.parse("file:/etc/keys.txt")


def test_parse_rejects_an_env_ref_whose_identifier_is_not_a_variable_name():
    """env:<值> 的形狀擋不住 parse 的前綴檢查，所以名稱本身也要驗。"""
    with pytest.raises(SecretError, match="environment variable"):
        SecretRef.parse("env:not a var name")


def test_parse_rejects_a_key_pasted_after_the_env_scheme():
    """`env:<真的 key>` —— 實測外洩過的那條路徑。

    這種輸入躲得過兩道檢查：
      * `_looks_like_plaintext_key` 的樣式錨在字串開頭，`env:AIza…`
        因為多了前綴而不匹配；
      * API key 幾乎都是純英數，因此 `AIzaSy…` **完全符合**合法環境
        變數名的語法，`_ENV_NAME` 也放行。
    結果 key 被當成變數名存進 registry，之後 resolve 失敗的錯誤訊息
    再把整把 key 帶進 flash 與網址列。
    """
    for key in (FAKE_OPENAI_KEY, FAKE_GOOGLE_KEY):
        with pytest.raises(SecretError, match="looks like the API key itself"):
            SecretRef.parse(f"env:{key}")


def test_the_rejection_message_does_not_echo_the_key_back():
    """錯誤訊息會進 flash、log 與稽核記錄，所以它不能包含那把 key。"""
    with pytest.raises(SecretError) as caught:
        SecretRef.parse(f"env:{FAKE_GOOGLE_KEY}")
    assert FAKE_GOOGLE_KEY not in str(caught.value)


def test_session_refs_are_not_persistent():
    assert SecretRef("session", "abc").is_persistent is False
    assert SecretRef.parse("env:X").is_persistent is True
    assert SecretRef.parse("vault:x").is_persistent is True


# ---------------------------------------------------------------------------
# 落盤內容
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not crypto_available(), reason="requires cryptography")
def test_vault_file_never_contains_the_plaintext_secret(tmp_path):
    vault = _vault(tmp_path)
    ref = vault.store(FAKE_OPENAI_KEY)

    raw = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert FAKE_OPENAI_KEY not in raw
    # 連任何 12 字元的連續片段都不該出現，避免「只遮了一半」的假通過。
    for start in range(0, len(FAKE_OPENAI_KEY) - 12):
        assert FAKE_OPENAI_KEY[start : start + 12] not in raw

    assert vault.resolve(ref) == FAKE_OPENAI_KEY


@pytest.mark.skipif(not crypto_available(), reason="requires cryptography")
def test_resolve_round_trips_and_env_refs_read_the_environment(tmp_path):
    vault = _vault(tmp_path, environ={"MY_KEY": FAKE_GOOGLE_KEY})
    assert vault.resolve("env:MY_KEY") == FAKE_GOOGLE_KEY

    ref = vault.store(FAKE_OPENAI_KEY)
    assert vault.resolve(str(ref)) == FAKE_OPENAI_KEY


def test_resolving_an_unset_environment_variable_fails_fast(tmp_path):
    vault = _vault(tmp_path, environ={})
    with pytest.raises(SecretError, match="unset or empty"):
        vault.resolve("env:NOT_SET_ANYWHERE")


# ---------------------------------------------------------------------------
# 指紋
# ---------------------------------------------------------------------------


def test_fingerprint_has_the_masked_shape_and_leaks_no_key_material(tmp_path):
    vault = _vault(tmp_path, environ={"MY_KEY": FAKE_OPENAI_KEY})
    shown = vault.fingerprint("env:MY_KEY")

    assert shown.startswith("****")
    assert len(shown) == 8
    visible = shown[4:]
    # §46：不顯示 key prefix/full value。指紋的四個字元不得是 key 的任何子字串。
    assert visible not in FAKE_OPENAI_KEY
    assert not FAKE_OPENAI_KEY.startswith(visible)
    assert not FAKE_OPENAI_KEY.endswith(visible)


def test_fingerprint_is_stable_for_one_key_and_differs_across_keys(tmp_path):
    vault = _vault(tmp_path, environ={"A": FAKE_OPENAI_KEY, "B": FAKE_GOOGLE_KEY})
    assert vault.fingerprint("env:A") == vault.fingerprint("env:A")
    assert vault.fingerprint("env:A") != vault.fingerprint("env:B")


def test_fingerprint_salt_makes_the_value_installation_specific():
    """沒有 salt 的四字元雜湊會變成離線驗證預言機，因此 salt 必須真的參與計算。"""
    salt_a, salt_b = new_salt(), new_salt()
    assert fingerprint(FAKE_OPENAI_KEY, salt_a) != fingerprint(FAKE_OPENAI_KEY, salt_b)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not crypto_available(), reason="requires cryptography")
def test_rotate_keeps_the_same_ref_and_bumps_version(tmp_path):
    """§46：純換 credential 不改 identity，因此 ref 必須原封不動。"""
    vault = _vault(tmp_path)
    ref = vault.store(FAKE_OPENAI_KEY)
    assert vault.secret_version(ref) == 1

    version = vault.rotate(ref, FAKE_GOOGLE_KEY)

    assert version == 2
    assert vault.secret_version(ref) == 2
    assert vault.resolve(ref) == FAKE_GOOGLE_KEY
    # 這正是重點：lock 裡存的 secret_ref 沒有變，所以不必重新 freeze。
    entries = json.loads((tmp_path / "vault.json").read_text(encoding="utf-8"))["entries"]
    assert list(entries) == [ref.identifier]


@pytest.mark.skipif(not crypto_available(), reason="requires cryptography")
def test_rotating_an_env_ref_is_refused(tmp_path):
    vault = _vault(tmp_path, environ={"MY_KEY": FAKE_OPENAI_KEY})
    with pytest.raises(SecretError, match="only vault: refs"):
        vault.rotate(SecretRef.parse("env:MY_KEY"), FAKE_GOOGLE_KEY)


# ---------------------------------------------------------------------------
# 無 master key 的 fail-closed 與 ephemeral 路徑
# ---------------------------------------------------------------------------


def test_persisting_without_a_master_key_is_refused(tmp_path):
    vault = _vault(tmp_path, master_key=None)
    assert vault.can_persist is False
    with pytest.raises(SecretError, match=MASTER_KEY_ENV):
        vault.store(FAKE_OPENAI_KEY)


# ---------------------------------------------------------------------------
# 本機自動保管的 master key（§46 的本機主控台例外）
# ---------------------------------------------------------------------------


def test_the_library_default_still_refuses_to_invent_a_master_key(tmp_path):
    """預設值必須維持 §46 原文：不自己產生金鑰，也不留下任何檔案。

    這條與下一條是一對。例外之所以可以接受，正是因為它必須在每個組裝點
    被明確寫出來；預設值一旦翻過去，例外就從「一處可見的決定」變成
    「到處都在、沒人記得為什麼」。
    """
    vault = _vault(tmp_path, master_key=None)
    assert vault.can_persist is False
    assert vault.master_key_source == "none"
    assert not (tmp_path / LOCAL_MASTER_KEY_FILENAME).exists()


@pytest.mark.skipif(not crypto_available(), reason="需要 cryptography 才會產生金鑰")
def test_opting_in_makes_persisting_work_without_any_environment_setup(tmp_path):
    """本機主控台走的就是這條：什麼都不設，貼上 key 就存得起來。"""
    vault = SecretVault(
        path=tmp_path / "vault.json", master_key=None, environ={},
        local_master_key=True,
    )

    assert vault.can_persist is True
    assert vault.master_key_source == "local-file"
    ref = vault.store(FAKE_OPENAI_KEY)
    assert ref.scheme == SCHEME_VAULT
    assert vault.resolve(ref) == FAKE_OPENAI_KEY


@pytest.mark.skipif(not crypto_available(), reason="需要 cryptography 才會產生金鑰")
def test_the_key_survives_a_restart_so_stored_secrets_stay_readable(tmp_path):
    """金鑰每次重開都換一把的話，vault 裡的密文就全部變成解不開的垃圾。"""
    first = SecretVault(
        path=tmp_path / "vault.json", environ={}, local_master_key=True
    )
    ref = first.store(FAKE_GOOGLE_KEY)

    second = SecretVault(
        path=tmp_path / "vault.json", environ={}, local_master_key=True
    )
    assert second.resolve(ref) == FAKE_GOOGLE_KEY


def test_an_environment_master_key_still_wins_over_the_local_file(tmp_path):
    """環境變數優先，本機檔案完全不參與 —— 接 KMS 或 OS keychain 時的前提。"""
    vault = SecretVault(
        path=tmp_path / "vault.json",
        environ={MASTER_KEY_ENV: MASTER},
        local_master_key=True,
    )

    assert vault.master_key_source == "environment"
    assert not (tmp_path / LOCAL_MASTER_KEY_FILENAME).exists()


@pytest.mark.skipif(not crypto_available(), reason="需要 cryptography 才會產生金鑰")
def test_the_stored_secret_is_still_ciphertext_not_plaintext(tmp_path):
    """自動產生金鑰**不等於**退回明文保存。

    roothinks 在缺 FERNET_KEY 時會把 key 明文寫進 DB；本系統沒有那條退路，
    而 §46 的整套遮蔽與指紋設計，前提就是落盤的一定是密文。
    """
    vault = SecretVault(
        path=tmp_path / "vault.json", environ={}, local_master_key=True
    )
    vault.store(FAKE_OPENAI_KEY)

    on_disk = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert FAKE_OPENAI_KEY not in on_disk
    for start in range(0, len(FAKE_OPENAI_KEY) - 8):
        assert FAKE_OPENAI_KEY[start : start + 8] not in on_disk


def test_ensure_local_master_key_is_idempotent(tmp_path):
    first = ensure_local_master_key(tmp_path)
    second = ensure_local_master_key(tmp_path)
    assert first == second
    assert first.strip() == first and first != ""


def test_ensure_local_master_key_replaces_an_empty_file(tmp_path):
    """空檔案代表上一次寫到一半。沿用它等於得到一把空金鑰，而 derive_key
    對空字串會直接拋錯 —— 症狀是主控台起不來，原因指不到這個檔案。"""
    (tmp_path / LOCAL_MASTER_KEY_FILENAME).write_text("   \n", encoding="utf-8")
    assert ensure_local_master_key(tmp_path) != ""


def test_session_secret_verify_still_works_without_a_master_key(tmp_path):
    """§46：無 master key 時禁止 save persistent secret，但可允許 ephemeral verify。"""
    vault = _vault(tmp_path, master_key=None)
    ref = vault.session.put(FAKE_OPENAI_KEY)

    assert ref.scheme == SCHEME_SESSION
    assert vault.resolve(ref) == FAKE_OPENAI_KEY
    assert not (tmp_path / "vault.json").exists() or FAKE_OPENAI_KEY not in (
        tmp_path / "vault.json"
    ).read_text(encoding="utf-8")


def test_a_discarded_session_secret_cannot_be_resolved(tmp_path):
    vault = _vault(tmp_path, master_key=None)
    ref = vault.session.put(FAKE_OPENAI_KEY)
    vault.session.discard(ref)
    with pytest.raises(SecretError, match="do not survive"):
        vault.resolve(ref)


def test_a_session_store_is_empty_after_discarding_everything():
    store = SessionSecretStore()
    ref = store.put(FAKE_OPENAI_KEY)
    assert len(store) == 1
    store.discard(ref)
    assert len(store) == 0


# ---------------------------------------------------------------------------
# 與 log 遮蔽的銜接
# ---------------------------------------------------------------------------


def test_resolve_registers_the_secret_for_log_redaction(tmp_path):
    """解出 secret 的同時就掛上遮蔽，才不會依賴呼叫端記得做這件事。"""
    unique = "sk-" + "R3solveRegisters" * 2
    vault = _vault(tmp_path, master_key=None, environ={"MY_KEY": unique})

    vault.resolve("env:MY_KEY")

    assert unique not in logging_setup.redact(f"provider said: {unique}")
    assert "[REDACTED]" in logging_setup.redact(f"provider said: {unique}")
