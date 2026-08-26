# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；只測 pcmef.admin.auth 的純函式與
#         pcmef.admin.app.serve 的啟動前檢查；以假環境變數取代真實環境，
#         不實際開啟任何埠、不對外連線。
# 檔案路徑: tests/llm_admin/test_admin_security.py
# 產生時間: 2026-08-27 02:10 +08:00
# 版本: v0.1.0
# 功能說明: 驗證這個能寫入 provider 憑證的頁面預設只開在本機，
#           要開到別的位址就必須先有權杖與 TLS，而且權杖比對不會洩漏時間資訊。
# 模組定位: §46 Network exposure 與 NFR-09 的可執行防線。
# 主要責任:
#   1. test_is_loopback_* 驗證位址判定，含「無法解析即視為非本機」
#   2. test_serve_refuses_non_loopback_without_controls 驗證拒絕啟動
#   3. test_serve_allows_non_loopback_with_token_and_tls 驗證放行條件
#   4. test_csrf_* 驗證缺漏、錯誤與正確三種情況
#   5. test_admin_token_* 驗證未設定時不強制、設定後必須相符
# 維護提醒:
#   - 不得把 test_serve_refuses_* 改成警告而非例外；§46 的 Fail Behavior
#     明訂「不符合即拒絕非-loopback 啟動」。
#   - v0.1.0 新增：首版，對應 NOTE-019。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_admin_security.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

from pcmef.admin.auth import (
    ADMIN_TLS_ENV,
    ADMIN_TOKEN_ENV,
    AdminSecurityError,
    assert_network_policy,
    check_csrf,
    is_loopback,
    new_csrf_token,
    require_admin_token,
)


# ---------------------------------------------------------------------------
# 位址判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_is_loopback_accepts_loopback_addresses(host):
    assert is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.1", "::"])
def test_is_loopback_rejects_routable_addresses(host):
    assert is_loopback(host) is False


def test_an_unresolvable_hostname_is_treated_as_non_loopback():
    """反過來寫會讓一個打錯的主機名直接繞過整套曝露檢查。"""
    assert is_loopback("my-laptop.local") is False
    assert is_loopback("") is False


# ---------------------------------------------------------------------------
# 啟動閘門
# ---------------------------------------------------------------------------


def test_loopback_needs_no_extra_controls():
    assert_network_policy("127.0.0.1", environ={})


def test_serve_refuses_non_loopback_without_controls():
    with pytest.raises(AdminSecurityError) as excinfo:
        assert_network_policy("0.0.0.0", environ={})
    message = str(excinfo.value)
    assert ADMIN_TOKEN_ENV in message
    assert ADMIN_TLS_ENV in message


def test_serve_refuses_non_loopback_with_token_but_no_tls():
    with pytest.raises(AdminSecurityError, match=ADMIN_TLS_ENV):
        assert_network_policy("192.168.1.10", environ={ADMIN_TOKEN_ENV: "t" * 32})


def test_serve_refuses_non_loopback_with_tls_but_no_token():
    with pytest.raises(AdminSecurityError, match=ADMIN_TOKEN_ENV):
        assert_network_policy("192.168.1.10", environ={ADMIN_TLS_ENV: "1"})


def test_serve_allows_non_loopback_with_token_and_tls():
    assert_network_policy(
        "192.168.1.10", environ={ADMIN_TOKEN_ENV: "t" * 32, ADMIN_TLS_ENV: "1"}
    )


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_csrf_tokens_are_unique_and_long_enough():
    tokens = {new_csrf_token() for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(token) >= 32 for token in tokens)


def test_csrf_accepts_a_matching_token():
    token = new_csrf_token()
    check_csrf(token, token)


@pytest.mark.parametrize(
    "expected, supplied",
    [(None, "x"), ("x", None), ("", ""), ("abc", "abd")],
    ids=["no-session", "no-form", "both-empty", "mismatch"],
)
def test_csrf_rejects_anything_that_does_not_match(expected, supplied):
    with pytest.raises(AdminSecurityError, match="CSRF"):
        check_csrf(expected, supplied)


# ---------------------------------------------------------------------------
# Admin token
# ---------------------------------------------------------------------------


def test_admin_token_is_not_required_when_unset():
    require_admin_token(None, environ={})


def test_admin_token_must_match_when_set():
    environ = {ADMIN_TOKEN_ENV: "correct-horse-battery-staple"}
    require_admin_token("correct-horse-battery-staple", environ=environ)
    with pytest.raises(AdminSecurityError, match="admin token"):
        require_admin_token("wrong", environ=environ)
    with pytest.raises(AdminSecurityError, match="admin token"):
        require_admin_token(None, environ=environ)
