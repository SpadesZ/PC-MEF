# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 取 /formal 與
#         /formal/preflight.json，並確認它們與 experiments.formal_service
#         是同一組判斷。不啟動真實伺服器、不呼叫 provider。
# 檔案路徑: tests/console/test_formal_routes.py
# 產生時間: 2026-09-02 17:10 +08:00
# 版本: v0.1.0
# 功能說明: 確認監控頁真的唯讀，而且畫面上的 PASS 與 CLI 放行的判準同源。
# 模組定位: P0-7a 的回歸測試。守兩件事：頁面沒有任何寫入控制項，
#           以及 Web 沒有自己重算一份比較寬鬆的 pre-flight。
# 主要責任:
#   1. test_page_has_no_write_controls 沒有 form、button、input
#   2. test_page_does_not_offer_to_start_a_run
#   3. test_json_matches_the_service 逐欄位比對，確認未重算
#   4. test_non_blocking_failures_are_distinguishable 不阻擋的失敗要看得出來
# 維護提醒:
#   - 不得在本頁加入啟動控制項而不同步修改 test_page_has_no_write_controls。
#     那條線由 §208 與 console.runner._assert_not_formal() 守著。
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


def test_page_has_no_write_controls(client):
    """唯讀不是靠說明文字，是靠頁面上沒有可送出的東西。"""
    body = client.get("/formal").get_data(as_text=True)
    for tag in ("<form", "<button", "<input", "<textarea"):
        assert tag not in body.lower(), f"the read-only monitor contains {tag}"


def test_page_does_not_offer_to_start_a_run(client):
    """連字面上的啟動端點都不該出現 —— 那會讓人以為只是壞掉了。"""
    body = client.get("/formal").get_data(as_text=True)
    assert "csrf" not in body.lower()
    # 指令字串可以出現（那是給人照著打的），但不得有指向自身的 POST 路徑。
    assert not re.search(r'action="[^"]*formal', body)


def test_there_is_no_post_endpoint_under_formal(client):
    assert client.post("/formal").status_code in (404, 405)
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
    from pcmef.experiments.formal_service import DRY_RUN_REPORT, preflight

    out = tmp_path / "out"
    out.mkdir()
    (out / DRY_RUN_REPORT).write_text("{}", encoding="utf-8")

    report = preflight(mode="dry-run", base=tmp_path / "base", out=out)
    occupied = [
        c for c in report["checks"] if c["check"] == "output_location_is_free"
    ][0]
    assert occupied["passed"] is False
    assert occupied["blocking"] is False
    assert "output_location_is_free" not in " ".join(report["blockers"])


def test_the_same_failure_blocks_a_formal_run(tmp_path):
    """同一個狀況在正式模式下必須擋下來：那是 one-shot。"""
    from pcmef.experiments.formal_service import FORMAL_REPORT, preflight

    out = tmp_path / "out"
    out.mkdir()
    (out / FORMAL_REPORT).write_text("{}", encoding="utf-8")

    report = preflight(mode="formal", base=tmp_path / "base", out=out)
    occupied = [
        c for c in report["checks"] if c["check"] == "output_location_is_free"
    ][0]
    assert occupied["blocking"] is True
    assert report["allowed"] is False
