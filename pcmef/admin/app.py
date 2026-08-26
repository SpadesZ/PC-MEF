# PC-MEF Research System source maintenance contract
# 上下游: 由 cli 的 admin serve 子指令與 tests/llm_admin 的 Flask test client 呼叫；
#         組裝 pcmef.admin.services 的 AdminService 與 routes_llm 的 blueprint，
#         啟動前經 pcmef.admin.auth 檢查網路曝露條件；不直接讀寫任何資料。
# 檔案路徑: pcmef/admin/app.py
# 產生時間: 2026-08-27 01:40 +08:00
# 版本: v0.1.0
# 功能說明: 建立管理頁面的 Flask 應用程式。預設只綁本機回環位址，
#           要開到其他位址就必須先具備管理者權杖與 TLS，否則直接拒絕啟動。
# 模組定位: admin 子系統的組裝點與啟動閘門。它「不是」formal run 的一部分 ——
#           SRC-SAI §208 規定所有 formal run 一律無 UI、走 CLI。
# 主要責任:
#   1. create_app() 組裝 service、blueprint、static/template 路徑與 session 金鑰
#   2. create_app() 以 PCMEF_ADMIN_BIND_ENABLED 控制 UI 的 Bind 是否開放
#   3. serve() 在啟動前呼叫 assert_network_policy()
#   4. _session_secret() 提供每個行程獨立的 session 簽章金鑰
# 維護提醒:
#   - 不得把 session secret 寫死或存進 repo；它每個行程重新產生，
#     代價只是重啟後要重新取得 CSRF 權杖，而那對本機單人管理頁毫無影響。
#   - 不得在 serve() 之外提供另一條略過 assert_network_policy() 的啟動路徑。
#   - 不得讓 formal runner 匯入本模組。Flask 是 admin 專用的可選相依，
#     formal run 不得因為缺 Flask 而失敗，也不得因為有 Flask 而多開一個埠。
#   - v0.1.0 新增：首版 app factory，決策見 NOTE-019。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_admin_page.py -v
#   - py -3.10 -m pytest tests/llm_admin/test_admin_security.py -v
# ------------------------------------------------------------

from __future__ import annotations

import os
import secrets as stdlib_secrets
from pathlib import Path
from typing import Mapping

from pcmef.admin.auth import ADMIN_TOKEN_ENV, DEFAULT_HOST, assert_network_policy
from pcmef.admin.routes_llm import blueprint
from pcmef.admin.services import AdminService
from pcmef.llm.registry import DEFAULT_REGISTRY_PATH, LLMRegistry
from pcmef.secrets.vault import DEFAULT_VAULT_PATH, SecretVault

__all__ = ["create_app", "serve", "DEFAULT_PORT"]

DEFAULT_PORT = 8787

_HERE = Path(__file__).resolve().parent


def _session_secret() -> bytes:
    """每個行程一把新的 session 簽章金鑰。

    刻意不持久化：這個頁面只在本機、由單一操作者短暫開啟，
    而一把寫死或存檔的金鑰是永久性的風險，換來的只是「重啟後不必重新載入頁面」。
    """
    return stdlib_secrets.token_bytes(32)


def create_app(
    service: AdminService | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    vault_path: str | Path = DEFAULT_VAULT_PATH,
    host: str = DEFAULT_HOST,
    bind_enabled: bool = False,
    environ: Mapping[str, str] | None = None,
):
    """建立 Flask app。

    bind_enabled 預設 False：§52 步驟 5 規定先讓 CLI 走完整個流程，
    再接 UI 的 Bind。旗標留在這裡而不是直接拿掉按鈕，是為了讓
    「UI 尚未開放寫入綁定」這件事在畫面上是可見的，而不是靜靜消失。
    """
    from flask import Flask

    env = environ if environ is not None else os.environ

    app = Flask(
        __name__,
        static_folder=str(_HERE / "static"),
        template_folder=str(_HERE / "templates"),
    )
    app.secret_key = _session_secret()
    app.config["PCMEF_ADMIN_SERVICE"] = service or AdminService(
        registry=LLMRegistry(registry_path),
        vault=SecretVault(vault_path),
    )
    app.config["PCMEF_ADMIN_HOST"] = host
    app.config["PCMEF_ADMIN_BIND_ENABLED"] = bool(bind_enabled)
    app.config["PCMEF_ADMIN_TOKEN_CONFIGURED"] = bool(env.get(ADMIN_TOKEN_ENV))
    app.register_blueprint(blueprint)
    return app


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    bind_enabled: bool = False,
    environ: Mapping[str, str] | None = None,
    **create_kwargs,
) -> None:
    """啟動開發用伺服器。非 loopback 綁定會在此被擋下。"""
    assert_network_policy(host, environ)
    app = create_app(host=host, bind_enabled=bind_enabled, environ=environ, **create_kwargs)
    app.run(host=host, port=port, debug=False, use_reloader=False)
