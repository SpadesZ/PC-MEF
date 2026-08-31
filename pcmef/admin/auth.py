# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.app 於建立與啟動 Flask app 時呼叫，並由
#         pcmef.admin.routes_llm 在每個寫入端點前呼叫；讀取環境變數
#         PCMEF_ADMIN_TOKEN 與 PCMEF_ADMIN_TLS；不寫出任何檔案。
# 檔案路徑: pcmef/admin/auth.py
# 產生時間: 2026-08-27 00:00 +08:00
# 版本: v0.1.0
# 功能說明: 決定這個管理頁面允許開在哪裡、以及誰可以送出寫入請求。預設只開在
#           本機回環位址；要開到區域網路就必須同時具備管理者權杖、CSRF 防護與 TLS，
#           三者缺一就拒絕啟動。
# 模組定位: §46 Network exposure 與 NFR-09 的強制點。它「不是」使用者管理系統 ——
#           本系統只有一個管理者，權杖是門檻不是身分。
# 主要責任:
#   1. LOOPBACK_HOSTS 列出視為本機的位址
#   2. is_loopback() 以位址判定，未知主機名一律視為非本機
#   3. assert_network_policy() 在非 loopback 且條件不足時拒絕啟動
#   4. new_csrf_token() / check_csrf() 產生與比對 CSRF 權杖
#   5. require_admin_token() 比對管理者權杖，且以常數時間比較
# 維護提醒:
#   - 不得為了「方便手機看一下」而預設 bind 0.0.0.0。§46 的 Fail Behavior
#     明訂不符合即拒絕非-loopback 啟動，這個頁面可以寫入 provider 憑證。
#   - 不得把 PCMEF_ADMIN_CONFINED_FORWARD 設在 compose 以外的地方。它宣告的是
#     「上游轉發層已把入口限縮成 loopback」，只有寫得出 port mapping 的那一層
#     有資格宣告；在一般 shell 裡設它，等於在沒有任何限縮的情況下關掉檢查。
#   - 不得把該旗標的適用範圍從 wildcard 放寬到任意 host。明確綁一個可路由
#     位址是操作者刻意選了一張實體網卡，那時放行就不是限縮而是開門。
#   - 不得以 == 比對權杖；字串比較會提早返回，形成時間側通道。
#   - 不得把 CSRF 權杖放進 URL query；它會留在瀏覽器歷史與伺服器 access log。
#   - 不得把 admin token 寫進 config 或 repo；它只能來自環境變數。
#   - v0.1.0 新增：首版網路與 CSRF 邊界，決策見 NOTE-019。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_admin_security.py -v
# ------------------------------------------------------------

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets as stdlib_secrets
from typing import Mapping

__all__ = [
    "AdminSecurityError",
    "ADMIN_TOKEN_ENV",
    "ADMIN_TLS_ENV",
    "CONFINED_FORWARD_ENV",
    "WILDCARD_HOSTS",
    "is_wildcard",
    "DEFAULT_HOST",
    "LOOPBACK_HOSTS",
    "is_loopback",
    "assert_network_policy",
    "new_csrf_token",
    "check_csrf",
    "require_admin_token",
]

ADMIN_TOKEN_ENV = "PCMEF_ADMIN_TOKEN"
ADMIN_TLS_ENV = "PCMEF_ADMIN_TLS"

#: 宣告「這個行程雖然綁 0.0.0.0，但實際入口已在轉發層限縮成 loopback」。
#:
#: 唯一預期的使用者是 docker-compose 的 console 服務：容器內必須綁 0.0.0.0
#: 才收得到 Docker 的埠轉發，但主機端的 ports 只寫 127.0.0.1，因此對外
#: 其實沒有任何路由進得來。
#:
#: 做成明確旗標而不是「一律放行 0.0.0.0」，是因為這個程序**無法自己驗證**
#: 上游的 port mapping 是什麼 —— 它只看得到自己綁在哪。因此這裡放行的
#: 依據不是偵測，而是操作者的宣告，而宣告要留下痕跡。
CONFINED_FORWARD_ENV = "PCMEF_ADMIN_CONFINED_FORWARD"

DEFAULT_HOST = "127.0.0.1"

LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


class AdminSecurityError(RuntimeError):
    """網路曝露條件不符、CSRF 檢查失敗，或管理者權杖錯誤。"""


def is_loopback(host: str) -> bool:
    """判定 host 是否為本機回環位址。

    無法解析成 IP 的主機名一律視為**非**本機。反過來寫（未知就當本機）
    會讓一個打錯的主機名直接繞過整套曝露檢查。
    """
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


#: 「綁全部介面」的兩個寫法。容器情境只會是這兩個之一。
WILDCARD_HOSTS: frozenset[str] = frozenset({"0.0.0.0", "::"})


def is_wildcard(host: str) -> bool:
    """host 是否為 wildcard 綁定。

    CONFINED_FORWARD_ENV 只在 wildcard 時有效：綁 wildcard 代表「我不挑
    介面，交給上游決定入口」，那正是容器把入口交給 Docker port mapping 的
    情形。反過來，明確綁一個可路由位址（例如 192.168.1.50）是操作者
    刻意選了一張實體網卡，那時再放行就不是限縮而是開門。
    """
    return host in WILDCARD_HOSTS


def assert_network_policy(
    host: str, environ: Mapping[str, str] | None = None
) -> None:
    """非 loopback 綁定時，要求 admin token 與 TLS 同時具備。

    §46：預設 bind 127.0.0.1；如需 LAN/remote，強制 admin auth + CSRF + TLS
    /reverse proxy，不符合即拒絕非-loopback 啟動。CSRF 一律開啟，
    所以這裡只需檢查另外兩項。
    """
    env = environ if environ is not None else os.environ
    if is_loopback(host):
        return
    if is_wildcard(host) and env.get(CONFINED_FORWARD_ENV, "").lower() in (
        "1", "true", "yes"
    ):
        # 操作者已宣告入口在轉發層被限縮成 loopback（容器情境）。
        # 這裡刻意不再要求 token：token 的用途是擋住「從網路上來的人」，
        # 而在這個宣告成立時根本沒有那條路。
        #
        # 殘留風險，寫下來讓下一個人看得到：本程序驗證不了這個宣告。
        # 若有人把 compose 的 ports 從 127.0.0.1:8790:8787 改成
        # 0.0.0.0:8790:8787 而忘了拿掉這個旗標，這道防線就不再作用，
        # 且沒有任何症狀。改 ports 的人有責任一併重新評估。
        return
    missing = []
    if not env.get(ADMIN_TOKEN_ENV):
        missing.append(f"{ADMIN_TOKEN_ENV} (admin auth)")
    if env.get(ADMIN_TLS_ENV, "").lower() not in ("1", "true", "yes"):
        missing.append(f"{ADMIN_TLS_ENV}=1 (TLS or a TLS-terminating reverse proxy)")
    if missing:
        raise AdminSecurityError(
            f"refusing to bind the admin console to {host!r} without {missing}. "
            "This page can write provider credentials; SRC-SAI §46 allows "
            "non-loopback exposure only with admin auth + CSRF + TLS. "
            f"Bind {DEFAULT_HOST} instead, or supply the missing controls."
        )


def new_csrf_token() -> str:
    return stdlib_secrets.token_urlsafe(32)


def check_csrf(expected: str | None, supplied: str | None) -> None:
    """比對 CSRF 權杖。以常數時間比較，避免逐字元試探。"""
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise AdminSecurityError(
            "CSRF token missing or invalid; reload the admin page and retry"
        )


def require_admin_token(
    supplied: str | None, environ: Mapping[str, str] | None = None
) -> None:
    """在有設定 admin token 時比對它。未設定則不強制（本機單人使用）。"""
    env = environ if environ is not None else os.environ
    expected = env.get(ADMIN_TOKEN_ENV)
    if not expected:
        return
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise AdminSecurityError("admin token missing or invalid")
