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
    SCHEME_SESSION,
    SCHEME_VAULT,
    SecretError,
    SecretRef,
    SecretVault,
    SessionSecretStore,
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
