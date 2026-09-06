# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；以 Flask test client 打 POST /formal/start，
#         並直接測 ConsoleRunner 的 formal 參數白名單。
#         **不啟動任何子行程**：start() 被 monkeypatch 攔下。
# 檔案路徑: tests/console/test_formal_start.py
# 產生時間: 2026-09-02 17:55 +08:00
# 版本: v0.1.0
# 功能說明: 確認 UI 只能「觸發」formal run，不能「設定」它。
# 模組定位: AMD-008 的可執行驗收。這條線放寬的是觸發權而非設定權，
#           而兩者的差別完全落在 FORMAL_PARAM_WHITELIST 上。
# 主要責任:
#   1. test_whitelist_* 任何科學參數都被拒絕
#   2. test_formal_requires_the_confirmation_phrase 一次性不得誤觸
#   3. test_command_contains_no_scientific_flags 產生的指令本身也要乾淨
#   4. test_start_requires_csrf 寫入端點受保護
# 維護提醒:
#   - 不得擴大白名單而不同步修改 test_whitelist_refuses_scientific_parameters。
#     那條測試列的每一個鍵都是曾經或可能被誤加的。
#   - 不得讓測試真的啟動子行程。那會跑起 384 列的實驗。
#   - v0.1.0 新增：首版，對應 P0-7b / AMD-008。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_formal_start.py -v
# ------------------------------------------------------------

from __future__ import annotations

import pytest

flask = pytest.importorskip("flask")

from pcmef.console.runner import (  # noqa: E402
    FORMAL_CONFIRM_PHRASE, FORMAL_PARAM_WHITELIST, ConsoleRunner,
    FormalRunRefused, RunSpec,
)


@pytest.fixture()
def runner(tmp_path):
    return ConsoleRunner(tmp_path / "runs")


# ---------------------------------------------------------------------------
# 白名單：UI 不得設定任何科學參數
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, value",
    [
        ("severity", 2.0),
        ("vision_severity", 2.0),
        ("tof_severity", 0.05),
        ("freeze_dir", "freeze"),
        ("lineage_root", "freeze"),
        ("base", "outputs/perception/formal_e2"),
        ("ds_dir", "outputs/perception/ds_v2"),
        ("q_vision_threshold", 1.3),
        ("fusion_weight", 0.5),
        ("out", "/tmp/anywhere"),
        ("registry_dir", "registry"),
    ],
)
def test_whitelist_refuses_scientific_parameters(runner, key, value):
    """能按按鈕不等於能決定跑什麼。這是 AMD-008 的整條界線。"""
    with pytest.raises(FormalRunRefused) as error:
        runner._assert_not_formal({"mode": "dry-run", key: value}, "formal_e2")
    assert key in str(error.value)


def test_whitelist_is_exactly_two_keys():
    """白名單一旦變大就該有人重新想過。這條測試讓它不可能悄悄變大。"""
    assert FORMAL_PARAM_WHITELIST == {"mode", "confirm"}


def test_dry_run_needs_no_confirmation(runner):
    runner._assert_not_formal({"mode": "dry-run"}, "formal_e2")


def test_formal_requires_the_confirmation_phrase(runner):
    with pytest.raises(FormalRunRefused) as error:
        runner._assert_not_formal({"mode": "formal"}, "formal_e2")
    assert "one-shot" in str(error.value)

    with pytest.raises(FormalRunRefused):
        runner._assert_not_formal({"mode": "formal", "confirm": "yes"}, "formal_e2")

    runner._assert_not_formal(
        {"mode": "formal", "confirm": FORMAL_CONFIRM_PHRASE}, "formal_e2"
    )


def test_unknown_mode_is_refused(runner):
    with pytest.raises(FormalRunRefused):
        runner._assert_not_formal({"mode": "whatever"}, "formal_e2")


def test_other_run_kinds_still_refuse_formal_parameters(runner):
    """放寬只針對 formal_e2；其他 kind 沒有理由帶 formal 參數。"""
    with pytest.raises(FormalRunRefused):
        runner._assert_not_formal({"formal": True}, "sim_smoke")


# ---------------------------------------------------------------------------
# 產生的指令
# ---------------------------------------------------------------------------


def test_command_calls_the_single_formal_entry_point(runner):
    command = runner._command("r1", RunSpec(kind="formal_e2", params={"mode": "formal"}))
    assert "formal" in command and "run-e2" in command
    assert "--mode" in command and "formal" in command
    # 退役的入口不得出現。
    assert "perception" not in command


def test_command_contains_no_scientific_flags(runner):
    for mode in ("dry-run", "formal"):
        command = runner._command("r", RunSpec(kind="formal_e2", params={"mode": mode}))
        joined = " ".join(command)
        for flag in (
            "--vision-severity", "--tof-severity", "--freeze-dir",
            "--lineage-root", "--ds-dir", "--allow-dirty",
        ):
            assert flag not in joined, f"{flag} reached the command line for {mode}"


def test_dry_run_reads_a_server_configured_dataset(runner):
    """base 由伺服器端決定，不從表單來 —— 否則 UI 就能挑資料集。"""
    command = runner._command(
        "r", RunSpec(kind="formal_e2", params={"mode": "dry-run"})
    )
    assert "--base" in command
    assert str(runner.dry_run_base) in command
    # 預設必須是已開封的集合，不是 final partition。
    assert "formal_e2" not in str(runner.dry_run_base)


def test_formal_mode_does_not_pass_a_base(runner):
    """正式執行讀 CLI 的預設 base，也就是 pre-flight 會驗身分的那一個。"""
    command = runner._command(
        "r",
        RunSpec(kind="formal_e2",
                params={"mode": "formal", "confirm": FORMAL_CONFIRM_PHRASE}),
    )
    assert "--base" not in command


# ---------------------------------------------------------------------------
# HTTP 層
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from pcmef.admin.app import create_app

    started: list[RunSpec] = []

    def fake_start(self, spec, *, run_id=None):
        # 攔下來：真的啟動會跑 384 列的實驗。
        #
        # 簽章必須含 run_id：啟動一律經 console.launch 的交易，而它會
        # **先配好 id、先寫歸屬**，再把同一個 id 交給 start()。少了這個
        # 參數，攔截器會 TypeError，而 launch 把它讀成「啟動失敗」——
        # 測試於是拿到 409，看起來像端點壞了，其實是替身簽章過期。
        self._assert_not_formal(spec.params, spec.kind)
        started.append(spec)
        from pcmef.console.runner import RunRecord

        run_id = run_id or "test-run"
        return RunRecord(
            run_id=run_id, kind=spec.kind, label=spec.label,
            params=dict(spec.params), command=self._command(run_id, spec),
        )

    monkeypatch.setattr(ConsoleRunner, "start", fake_start)
    app = create_app(
        registry_path=tmp_path / "r.db", vault_path=tmp_path / "v",
        console_run_root=tmp_path / "runs",
        workspace_root=tmp_path / "workspace",
        environ={
            # formal 的輸出位置預設是 canonical Final E2 目錄。測試一律
            # 改掉：pre-flight 會去讀它，而 pointer 會去寫它旁邊。
            "PCMEF_FORMAL_OUT": str(tmp_path / "formal_out"),
            "PCMEF_FORMAL_BASE": str(tmp_path / "formal_base"),
            "PCMEF_FORMAL_DRY_RUN_BASE": str(tmp_path / "dry_run_base"),
        },
    )
    app.config["TESTING"] = True
    test_client = app.test_client()
    test_client.started = started  # type: ignore[attr-defined]
    return test_client


def _csrf(client) -> str:
    from pcmef.admin.routes_llm import CSRF_SESSION_KEY

    client.get("/formal")
    with client.session_transaction() as session:
        return session[CSRF_SESSION_KEY]


def test_start_requires_csrf(client):
    response = client.post("/formal/start", data={"mode": "dry-run"})
    assert response.status_code == 403


def test_dry_run_can_be_started(client):
    token = _csrf(client)
    response = client.post(
        "/formal/start", data={"mode": "dry-run", "csrf_token": token}
    )
    assert response.status_code in (201, 302)
    assert [s.params["mode"] for s in client.started] == ["dry-run"]


def test_formal_without_the_phrase_is_refused_over_http(client):
    token = _csrf(client)
    response = client.post(
        "/formal/start", data={"mode": "formal", "csrf_token": token}
    )
    assert response.status_code == 403
    assert not client.started


def test_extra_parameters_are_refused_over_http(client):
    """就算有人繞過表單直接打 API，白名單仍然擋得住。"""
    token = _csrf(client)
    response = client.post(
        "/formal/start",
        json={"mode": "dry-run", "vision_severity": 9.9, "csrf_token": token},
        headers={"X-CSRF-Token": token},
    )
    # 端點只取 mode 與 confirm，多的鍵根本不會被轉交 —— 但即使被轉交，
    # runner 的白名單也會擋下。兩層都不該讓 9.9 生效。
    assert response.status_code in (201, 302, 403)
    for spec in client.started:
        assert set(spec.params) <= FORMAL_PARAM_WHITELIST
