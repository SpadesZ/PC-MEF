# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 取 /formal 與
#         /formal/preflight.json，並確認它們與 experiments.formal_service
#         是同一組判斷。不啟動真實伺服器、不呼叫 provider。
# 檔案路徑: tests/console/test_formal_routes.py
# 產生時間: 2026-09-02 17:10 +08:00
# 版本: v0.1.0
# 功能說明: 確認畫面上的 PASS 與 CLI 放行的判準同源，且頁面不含任何
#           科學參數欄位。
# 模組定位: P0-7a 的回歸測試。守兩件事：Web 沒有自己重算一份比較寬鬆的
#           pre-flight，以及表單只送白名單內的鍵（AMD-008 放寬的是
#           觸發權，不是設定權）。
# 主要責任:
#   1. test_the_page_has_no_scientific_input_fields
#   2. test_the_only_form_fields_are_the_whitelisted_ones
#   3. test_json_matches_the_service 逐欄位比對，確認未重算
#   4. test_non_blocking_failures_are_distinguishable 不阻擋的失敗要看得出來
# 維護提醒:
#   - 不得在表單加入任何科學參數欄位。UI 只能決定「跑不跑」與模式；
#     多一個鍵就會被 ConsoleRunner 的白名單擋下（AMD-008）。
#   - 不得讓 Web 自己算 pre-flight。畫面比 CLI 寬鬆是最難發現的那種錯。
#   - v0.1.0 新增：首版，對應 P0-7a。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_formal_routes.py -v
# ------------------------------------------------------------

from __future__ import annotations

import re

import pytest

flask = pytest.importorskip("flask")


@pytest.fixture()
def client(tmp_path):
    from pcmef.admin.app import create_app

    app = create_app(
        registry_path=tmp_path / "registry.db",
        vault_path=tmp_path / "vault",
        console_run_root=tmp_path / "runs",
    )
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# 唯讀
# ---------------------------------------------------------------------------


def test_the_page_renders(client):
    response = client.get("/formal")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Formal Research Workspace" in body


def test_the_page_has_no_scientific_input_fields(client):
    """AMD-008 放寬的是**觸發權**，不是設定權。

    頁面自 P0-7b 起有兩個 form（預演／正式執行），但表單裡不得出現任何
    科學參數欄位。這條測試列的每一個名字都是「一旦出現就代表 UI 成了
    第二條設定通道」的東西。
    """
    body = client.get("/formal").get_data(as_text=True)
    for forbidden in (
        "severity", "threshold", "freeze_dir", "freeze-dir", "ds_dir", "ds-dir",
        "lineage_root", "lineage-root", "fusion_weight", "temperature",
        'name="base"', 'name="out"',
    ):
        assert f'name="{forbidden}"' not in body and forbidden not in _form_field_names(body), (
            f"the formal page exposes a scientific input named {forbidden!r}"
        )


def _form_field_names(body: str) -> set[str]:
    """取出所有 form 欄位的 name。"""
    return set(re.findall(r'<(?:input|select|textarea)[^>]*name="([^"]+)"', body))


def test_the_only_form_fields_are_the_whitelisted_ones(client):
    from pcmef.console.runner import FORMAL_PARAM_WHITELIST

    names = _form_field_names(client.get("/formal").get_data(as_text=True))
    # csrf_token 是安全機制，不是研究參數。
    assert names - {"csrf_token"} <= FORMAL_PARAM_WHITELIST, (
        f"unexpected form fields: {sorted(names - {'csrf_token'} - FORMAL_PARAM_WHITELIST)}"
    )


def test_preflight_json_is_read_only(client):
    assert client.post("/formal/preflight.json").status_code in (404, 405)


# ---------------------------------------------------------------------------
# 與 CLI 同源
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["formal", "dry-run"])
def test_json_matches_the_shared_service(client, mode):
    """Web 不得自己重算。逐欄位比對服務層的輸出。"""
    from pcmef.experiments.formal_service import preflight

    payload = client.get(f"/formal/preflight.json?mode={mode}").get_json()
    paths = payload["paths"]
    expected = preflight(mode=mode, **paths)

    assert payload["preflight"]["allowed"] == expected["allowed"]
    assert payload["preflight"]["blockers"] == expected["blockers"]
    assert [c["check"] for c in payload["preflight"]["checks"]] == [
        c["check"] for c in expected["checks"]
    ]
    assert [c["passed"] for c in payload["preflight"]["checks"]] == [
        c["passed"] for c in expected["checks"]
    ]


def test_unknown_mode_is_rejected(client):
    response = client.get("/formal/preflight.json?mode=whatever")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 不阻擋的失敗要能與真正的失敗區分
# ---------------------------------------------------------------------------


def test_every_check_declares_whether_it_blocks(client):
    payload = client.get("/formal/preflight.json?mode=dry-run").get_json()
    for check in payload["preflight"]["checks"]:
        assert "blocking" in check, f"{check['check']} does not say whether it blocks"


def test_a_non_blocking_failure_does_not_block(tmp_path):
    """dry run 可以覆寫自己的上一份預演；那不是 FAIL。"""
    from pcmef.experiments.formal_service import preflight

    out = tmp_path / "out"
    out.mkdir()

    report = preflight(mode="dry-run", base=tmp_path / "base", out=out)
    # 預演模式下 formal 的 claim 狀態只是參考資訊，不阻擋 —— 一個已經跑完
    # 正式執行的專案裡，再跑一次預演仍然是合法的。
    informational = [c for c in report["checks"] if not c["blocking"]]
    assert informational, "預演必須有非阻擋的參考項"
    assert all(
        c["check"] not in " ".join(report["blockers"]) for c in informational
    )


def test_the_same_failure_blocks_a_formal_run(tmp_path):
    """正式模式下 claim 已被持有就必須擋下來：那是 one-shot。

    判準自 2026-09-05 起是 claim 狀態而不是「報告檔在不在」：一次跑到一半
    失敗的正式執行沒有報告，但 final partition 已經開封（NOTE-079）。
    """
    from pcmef.experiments.e2_formal import run_artifact_root
    from pcmef.experiments.formal_service import preflight
    from pcmef.experiments.run_claim import RunIdentity, mark_complete, reserve

    out = tmp_path / "out"
    out.mkdir()
    formal_root = run_artifact_root(out, dry_run=False)
    identity = RunIdentity(
        freeze_dir="freeze/runs/PFC-001", lock_hashes={"gate": "a" * 64},
        base_manifest_hash="b" * 64, code_version="c" * 40,
    )
    claim = reserve(formal_root, identity)
    mark_complete(formal_root, claim["claim_id"])

    report = preflight(mode="formal", base=tmp_path / "base", out=out)
    taken = [
        c for c in report["checks"] if c["check"] == "formal_run_claim_is_available"
    ][0]
    assert taken["passed"] is False
    assert taken["blocking"] is True
    assert report["allowed"] is False
