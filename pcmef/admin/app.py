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
from pcmef.core.constants import CLASS_ORDER
from pcmef.llm.registry import DEFAULT_REGISTRY_PATH, LLMRegistry
from pcmef.secrets.vault import DEFAULT_VAULT_PATH, SecretVault

__all__ = ["create_app", "serve", "DEFAULT_PORT"]

#: 本機主控台的預設埠。
#:
#: 2026-08-31 由 8787 改為 8790：8787 被這台機器上另一個常駐服務長期佔用，
#: 而 Flask 撞埠時的錯誤訊息不會出現在瀏覽器裡 —— 瀏覽器看到的是**佔用者**
#: 回的頁面（實測是一個 503），於是症狀看起來像「PC-MEF 壞了」，
#: 實際上 PC-MEF 根本沒起來。換一個沒人用的埠比每次都重新診斷一次便宜。
DEFAULT_PORT = 8790

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
    console_run_root: str | Path = "outputs/console/runs",
    audit_paths=None,
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
    _register_console(app, console_run_root, audit_paths)
    return app


def _register_console(app, run_root, audit_paths) -> None:
    """掛上探索用執行台。

    與 LLM Setup 共用同一個 Flask app 與同一套 CSRF/權杖檢查：
    兩者都是「本機、單人、可寫入 draft」的管理介面，分成兩個服務只會
    讓安全設定有兩份，而其中一份遲早會忘記更新。
    """
    from pcmef.audit.e1_gates import AuditPaths
    from pcmef.console.routes import blueprint as console_blueprint
    from pcmef.console.runner import ConsoleRunner

    app.config["PCMEF_CONSOLE_RUNNER"] = ConsoleRunner(run_root)
    app.config["PCMEF_CONSOLE_CLASSES"] = list(CLASS_ORDER)
    resolved_paths = audit_paths or AuditPaths()

    def gate_summary():
        """讀 E1 gate 現況給首頁的燈號用。稽核器只讀不寫，隨時可呼叫。"""
        from pcmef.audit.e1_gates import audit_e1_gates

        report = audit_e1_gates(resolved_paths)
        css = {"PASS": "pass", "FAIL": "fail"}
        return {
            "counts": report.counts(),
            "gates": [
                {
                    "id": r.identifier.replace("E1-", ""),
                    "status": r.status.value.replace("NOT_PRODUCED", "尚未產出")
                              .replace("PASS", "通過").replace("FAIL", "不符"),
                    "requirement": r.requirement,
                    "css": css.get(r.status.value, "pending"),
                }
                for r in report.results
            ],
        }

    app.config["PCMEF_CONSOLE_GATE_SUMMARY"] = gate_summary
    app.register_blueprint(console_blueprint)


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
