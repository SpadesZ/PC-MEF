# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.admin.app 註冊為 Flask blueprint；每個寫入端點先過
#         pcmef.admin.auth 的 CSRF 與權杖檢查，再委派 pcmef.admin.services；
#         繪製 templates/llm_setup.html。不直接接觸 registry 或 vault。
# 檔案路徑: pcmef/admin/routes_llm.py
# 產生時間: 2026-08-27 00:55 +08:00
# 版本: v0.1.0
# 功能說明: 把 §50 Admin API Contract 的八個端點接起來，並依請求型態決定回
#           JSON 還是導回頁面。仍被綁定的對象被刪除時回 409 並附相依清單。
# 模組定位: HTTP 層。它「不是」邏輯所在 —— 所有判斷都在 services，
#           本檔只負責取參數、擋 CSRF、轉換錯誤為狀態碼。
# 主要責任:
#   1. page() 繪製四張 card，並把 CSRF 權杖放進每個表單
#   2. create_connection() 對應 POST /api/admin/llm/connections
#   3. fetch_models() / verify_model() 對應 §50 的兩個 provider 動作
#   4. bind_task() 同時接受 PUT（API）與 POST（HTML form）
#   5. delete_connection() / delete_model() 在有相依 task 時回 409
#   6. rotate_secret() 對應 credential-only rotation
#   7. _respond() 依 Accept/Content-Type 在 JSON 與 redirect 之間切換
# 維護提醒:
#   - 不得在任何回應中放入完整 API key；§42 區塊 A 與 LLM-UI-01 都以此為準。
#     services 層已保證回傳值只含遮蔽指紋，本層不得另外從表單回填。
#   - 不得為了方便而讓寫入端點免除 CSRF 檢查；本頁能寫入 provider 憑證。
#   - 不得在 DependencyError 時回 200 或 500；§46 明訂 HTTP 409 並列出
#     dependent tasks，狀態碼本身是契約的一部分。
#   - 不得在此新增任何會寫 lock 的端點。UI 只寫 draft registry，
#     snapshot 只能由 CLI 產生（§52 結語）。
#   - v0.1.0 新增：首版路由，決策見 NOTE-019。
# 驗證方式:
#   - py -3.10 -m pytest tests/llm_admin/test_admin_page.py -v
# ------------------------------------------------------------

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from pcmef.admin.auth import (
    ADMIN_TOKEN_ENV,
    AdminSecurityError,
    check_csrf,
    new_csrf_token,
    require_admin_token,
)
from pcmef.admin.services import AdminServiceError
from pcmef.agents.provider import FORMAL_ELIGIBLE_PROVIDERS, PROVIDER_ADAPTERS
from pcmef.llm.registry import DependencyError, RegistryError
from pcmef.secrets.vault import SecretError

__all__ = ["blueprint", "CSRF_SESSION_KEY", "CSRF_FORM_FIELD", "ADMIN_TOKEN_HEADER"]

blueprint = Blueprint("llm_admin", __name__)

CSRF_SESSION_KEY = "pcmef_csrf"
CSRF_FORM_FIELD = "csrf_token"
ADMIN_TOKEN_HEADER = "X-PCMEF-Admin-Token"


# ---------------------------------------------------------------------------
# 共用
# ---------------------------------------------------------------------------


def _service():
    return current_app.config["PCMEF_ADMIN_SERVICE"]


def _csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = new_csrf_token()
        session[CSRF_SESSION_KEY] = token
    return token


def _guard() -> None:
    """所有寫入端點的共同前置檢查。

    帶有效 admin token 的請求免 CSRF：CSRF 防的是「瀏覽器自動附上的
    憑證被第三方站點利用」，而必須手動加上的標頭本來就不會被自動附上。
    沒有設定 admin token 時（本機單人使用）則一律要求 CSRF。
    """
    supplied_token = request.headers.get(ADMIN_TOKEN_HEADER)
    require_admin_token(supplied_token)
    if supplied_token and current_app.config.get("PCMEF_ADMIN_TOKEN_CONFIGURED"):
        return
    check_csrf(
        session.get(CSRF_SESSION_KEY),
        request.form.get(CSRF_FORM_FIELD) or request.headers.get("X-CSRF-Token"),
    )


def _wants_json() -> bool:
    if request.is_json:
        return True
    accept = request.accept_mimetypes
    return accept["application/json"] > accept["text/html"]


def _payload() -> dict:
    """表單與 JSON 兩種提交方式取值一致。"""
    if request.is_json:
        return dict(request.get_json(silent=True) or {})
    return {key: value for key, value in request.form.items()}


def _respond(body: dict, status: int = 200, message: str = "", category: str = "ok"):
    if _wants_json():
        return jsonify(body), status
    return redirect(url_for("llm_admin.page", flash=message, category=category))


# ---------------------------------------------------------------------------
# 錯誤處理
# ---------------------------------------------------------------------------


@blueprint.errorhandler(DependencyError)
def _dependency(error: DependencyError):
    """§46 Delete dependency：HTTP 409 並列出 dependent tasks。"""
    body = {"error": str(error), "dependent_tasks": list(error.tasks)}
    if _wants_json():
        return jsonify(body), 409
    return redirect(
        url_for("llm_admin.page", flash=str(error), category="error")
    ), 409


@blueprint.errorhandler(AdminSecurityError)
def _security(error: AdminSecurityError):
    return jsonify({"error": str(error)}), 403


@blueprint.errorhandler(RegistryError)
@blueprint.errorhandler(AdminServiceError)
@blueprint.errorhandler(SecretError)
def _bad_request(error: Exception):
    body = {"error": str(error)}
    if _wants_json():
        return jsonify(body), 400
    return redirect(url_for("llm_admin.page", flash=str(error), category="error")), 400


# ---------------------------------------------------------------------------
# 頁面
# ---------------------------------------------------------------------------


@blueprint.get("/")
def index():
    """本服務只有一個頁面，根路徑直接轉過去。

    留一個 404 在根路徑沒有任何好處：操作者打開 http://127.0.0.1:8787
    看到 Not Found，只會以為服務壞了。
    """
    return redirect(url_for("llm_admin.page"))


@blueprint.get("/admin/llm-setup")
def page():
    service = _service()
    flash = request.args.get("flash", "")
    category = request.args.get("category", "ok")
    return render_template(
        "llm_setup.html",
        csrf_token=_csrf_token(),
        connections=service.connection_views(),
        bindings=service.binding_views(),
        snapshot=service.snapshot_view(),
        providers=sorted(PROVIDER_ADAPTERS),
        formal_providers=sorted(FORMAL_ELIGIBLE_PROVIDERS),
        can_persist=service.vault.can_persist,
        lifecycle_badge={
            "draft": "", "fetched": "warn", "connected": "", "locked": "ok"
        },
        bind_enabled=current_app.config.get("PCMEF_ADMIN_BIND_ENABLED", False),
        bind_host=current_app.config.get("PCMEF_ADMIN_HOST", "127.0.0.1"),
        messages=[(category, flash)] if flash else [],
    )


# ---------------------------------------------------------------------------
# §50 Admin API Contract
# ---------------------------------------------------------------------------


@blueprint.post("/api/admin/llm/connections")
def create_connection():
    _guard()
    data = _payload()
    view = _service().add_connection(
        name=str(data.get("name", "")).strip(),
        provider=str(data.get("provider", "")).strip(),
        api_key=(data.get("api_key") or None),
        secret_ref=(data.get("secret_ref") or None),
        base_url=str(data.get("base_url", "")).strip(),
        timeout_sec=int(data.get("timeout_sec") or 30),
        notes=str(data.get("notes", "")),
        enabled=str(data.get("enabled", "1")) not in ("0", "false", "False"),
    )
    # 回應刻意只帶遮蔽指紋與 ref，不帶 api_key —— LLM-UI-01 的直接對象。
    return _respond(
        {
            "connection_id": view.connection_id,
            "name": view.name,
            "provider": view.provider,
            "secret_ref": view.secret_ref,
            "secret_fingerprint": view.secret_fingerprint,
            "status": view.status,
        },
        201,
        f"connection {view.name} 已建立（憑證 {view.secret_fingerprint}）",
    )


@blueprint.post("/api/admin/llm/connections/<connection_id>/fetch-models")
def fetch_models(connection_id: str):
    _guard()
    models = _service().fetch_models(connection_id)
    return _respond(
        {"models": [m.__dict__ for m in models]},
        200,
        f"取得 {len(models)} 個模型",
    )


@blueprint.post("/api/admin/llm/connections/<connection_id>/manual-model")
def add_manual_model(connection_id: str):
    _guard()
    model = _service().add_manual_model(
        connection_id, str(_payload().get("model_id", "")).strip()
    )
    return _respond({"model": model.__dict__}, 201, f"已加入 {model.model_id}")


@blueprint.post("/api/admin/llm/models/<model_profile_id>/verify")
def verify_model(model_profile_id: str):
    _guard()
    outcome = _service().verify_model(model_profile_id)
    return _respond(
        {
            "model_profile_id": outcome.model_profile_id,
            "verified": [c.value for c in outcome.verified],
            "results": [r.to_artifact() for r in outcome.results],
        },
        200,
        f"{outcome.model_id}：" + " / ".join(outcome.summary()),
    )


# --- LAVA setup 的線路生命週期（NOTE-023）--------------------------------


@blueprint.post("/api/admin/llm/connections/<connection_id>/select-model")
def select_model(connection_id: str):
    _guard()
    data = _payload()
    model_profile_id = str(data.get("model_profile_id", "")).strip()
    if not model_profile_id and data.get("model_id"):
        view = _service().add_manual_model_and_select(
            connection_id, str(data["model_id"]).strip()
        )
        return _respond(
            {"model": view.__dict__}, 200, f"已選定手動輸入的 {view.model_id}"
        )
    view = _service().select_model(connection_id, model_profile_id)
    return _respond(
        {"connection_id": view.connection_id, "lifecycle": view.lifecycle},
        200,
        f"{view.name} 已選定 {view.selected_model_id}",
    )


@blueprint.post("/api/admin/llm/connections/<connection_id>/test")
def test_connection(connection_id: str):
    """LAVA 的 Test：跑完 Thesis Core 需要的三項 probe 才算通過。"""
    _guard()
    outcome = _service().test_connection(connection_id)
    passed = not outcome.failed()
    return _respond(
        {
            "ok": passed,
            "verified": [c.value for c in outcome.verified],
            "results": [r.to_artifact() for r in outcome.results],
        },
        200,
        ("測試通過：" if passed else "測試未通過：") + " / ".join(outcome.summary()),
        "ok" if passed else "error",
    )


@blueprint.post("/api/admin/llm/connections/<connection_id>/lock")
def lock_connection(connection_id: str):
    _guard()
    view = _service().lock_connection(connection_id)
    return _respond(
        {"connection_id": view.connection_id, "lifecycle": view.lifecycle},
        200,
        f"{view.name} 已鎖定，現在可以綁定到 task",
    )


@blueprint.post("/api/admin/llm/connections/<connection_id>/unlock")
def unlock_connection(connection_id: str):
    _guard()
    view = _service().unlock_connection(connection_id)
    return _respond(
        {"connection_id": view.connection_id, "lifecycle": view.lifecycle},
        200,
        f"{view.name} 已解鎖",
    )


@blueprint.post("/api/admin/llm/bindings/<task_code>/lock")
def lock_binding(task_code: str):
    """draft 層的確認鎖，與 formal 的 llm_runtime.lock 是兩件事。"""
    _guard()
    locked = str(_payload().get("locked", "1")) not in ("0", "false", "False")
    binding = _service().set_binding_locked(task_code, locked)
    return _respond(
        {"task_code": binding.task_code, "is_locked": binding.is_locked},
        200,
        f"{task_code} draft binding {'已鎖定' if binding.is_locked else '已解鎖'}",
    )


@blueprint.route("/api/admin/llm/bindings/<task_code>", methods=["PUT", "POST"])
def bind_task(task_code: str):
    """§50 指定 PUT；HTML form 只能送 POST，因此兩者都接受。"""
    _guard()
    if not current_app.config.get("PCMEF_ADMIN_BIND_ENABLED", False):
        return (
            jsonify(
                {
                    "error": (
                        "binding through the UI is disabled on this instance; "
                        "SRC-SAI §52 step 5 requires the CLI flow to be proven "
                        "first. Use `pcmef llm binding set`."
                    )
                }
            ),
            403,
        )
    binding = _service().bind_task(
        task_code=task_code,
        model_profile_id=str(_payload().get("model_profile_id", "")),
        actor=request.headers.get("X-PCMEF-Actor", "admin-ui"),
        reason=str(_payload().get("reason", "")),
    )
    return _respond(
        {
            "task_code": binding.task_code,
            "model_profile_id": binding.model_profile_id,
            "binding_version": binding.binding_version,
            "status": binding.status,
        },
        200,
        f"{task_code} 已綁定（draft v{binding.binding_version}）",
    )


@blueprint.get("/api/admin/llm/bindings")
def list_bindings():
    """§50：active formal snapshot 必須與 draft 清楚分開。"""
    service = _service()
    return jsonify(
        {
            "draft": [
                {
                    "task_code": view.task_code,
                    "model_profile_id": view.current_model_profile_id,
                    "label": view.current_label,
                    "binding_version": view.binding_version,
                    "status": view.status,
                    "required": list(view.required),
                }
                for view in service.binding_views()
            ],
            "active_formal_snapshot": service.snapshot_view().to_api(),
        }
    )


@blueprint.post("/api/admin/llm/connections/<connection_id>/rotate-secret")
def rotate_secret(connection_id: str):
    _guard()
    view = _service().rotate_secret(
        connection_id, str(_payload().get("api_key", ""))
    )
    return _respond(
        {
            "connection_id": view.connection_id,
            "secret_ref": view.secret_ref,
            "secret_fingerprint": view.secret_fingerprint,
        },
        200,
        f"{view.name} 憑證已更新（{view.secret_fingerprint}）；model identity 未變",
    )


@blueprint.post("/api/admin/llm/connections/<connection_id>/delete")
def delete_connection(connection_id: str):
    _guard()
    _service().delete_connection(connection_id)
    return _respond({"deleted": connection_id}, 200, "connection 已刪除")


@blueprint.post("/api/admin/llm/models/<model_profile_id>/delete")
def delete_model(model_profile_id: str):
    _guard()
    _service().delete_model(model_profile_id)
    return _respond({"deleted": model_profile_id}, 200, "model profile 已刪除")


@blueprint.post("/api/admin/llm/runtime-snapshot")
def runtime_snapshot():
    """§52 結語：UI 永遠不能成為繞過 freeze 的第二條設定通道。

    因此這個端點存在、但只回報「快照必須由 CLI 產生」。保留端點而非移除，
    是為了讓照著 §50 表格找過來的人得到明確答案，而不是一個 404 之後
    自己想辦法。
    """
    return (
        jsonify(
            {
                "error": (
                    "runtime snapshots are produced by the CLI only: "
                    "`pcmef llm snapshot --out freeze/llm_runtime.lock.json`. "
                    "The admin page writes the draft registry and never a lock."
                )
            }
        ),
        403,
    )
