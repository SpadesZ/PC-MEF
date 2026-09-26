# PC-MEF Research System source maintenance contract
# 上下游: 驗 pcmef/console/run_view.py 的 cost_view / cache_entry_view，
#         以及 _run_cost.html 與 cache_entry.html 的渲染。
# 檔案路徑: tests/console/test_cost_and_cache_browser.py
# 產生時間: 2026-09-04 19:40 +08:00
# 版本: v0.1.0
# 功能說明: 確認成本頁不把「沒量到」說成 $0，且 thinking token 分開計。
# 模組定位: P3-2 與 P3-3 的驗收。
# 主要責任:
#   1. thinking token 必須計入計費 output，並標出低估倍數
#   2. 沒有 token_usage 時說明原因，不得顯示金額
#   3. cache_key 來自 URL，必須限制成 64 位十六進位
#   4. 六份缺一的目錄要顯示成不完整
# 維護提醒:
#   - 不得放寬 test_thinking_tokens_are_billed_as_output。只讀
#     candidatesTokenCount 會低估帳單，這一條是那個錯誤的防線。
#   - 不得放寬 cache_key 的白名單。目錄名直接由它組成，
#     沒有限制就是一條讀取任意目錄的路徑。
#   - 不得把「沒有量到用量」的情況改成顯示 0。dry run 沒呼叫、
#     舊報告沒寫出，兩者都不是「花了零元」。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_cost_and_cache_browser.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

from pcmef.console import run_view  # noqa: E402
from pcmef.console.navigation import RUN_SECTIONS  # noqa: E402


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


#: 實測比例：thinking 約佔計費 output 的 72%，只讀 completion 低估約 3.59 倍。
USAGE = {
    "measured": True,
    "by_role": {
        "arbitration_agent": {"calls": 4, "prompt_tokens": 3000,
                              "completion_tokens": 200, "thoughts_tokens": 900},
        "observation_agent": {"calls": 4, "prompt_tokens": 2000,
                              "completion_tokens": 150, "thoughts_tokens": 400},
    },
    "totals": {"calls": 8, "prompt_tokens": 5000, "completion_tokens": 350,
               "thoughts_tokens": 1300, "billable_output_tokens": 1650},
}


@pytest.fixture()
def run(tmp_path) -> Path:
    folder = tmp_path / "runs" / "20260101T000000-dddddd"
    (folder / "artifacts").mkdir(parents=True)
    (folder / "log.txt").write_text("done\n", encoding="utf-8")
    return folder


def _report(run: Path, **extra) -> None:
    _write(run / "artifacts" / "formal_e2_report.json",
           {"report_id": "formal_e2_full_pcmef", **extra})


# ---------------------------------------------------------------------------
# 成本
# ---------------------------------------------------------------------------


def test_thinking_tokens_are_billed_as_output(run):
    """thinking 照 output 計費卻不在 candidatesTokenCount 裡。

    只讀 completion 的話，這份用量會估成 350 而不是 1650 ——
    低估 4.71 倍。
    """
    _report(run, token_usage=USAGE)
    view = run_view.cost_view(run, "formal_e2")

    assert view["available"] is True
    assert view["totals"]["billable_output_tokens"] == 1650
    assert view["underestimate_factor"] == pytest.approx(1650 / 350)
    assert view["thinking_share"] == pytest.approx(1300 / 1650)


def test_the_price_comes_from_the_shared_pricing_table(run):
    """費率由 e2_cost.PRICING 匯入，不得在 console 另寫一份。"""
    from pcmef.experiments.e2_cost import PRICING

    _report(run, token_usage=USAGE)
    view = run_view.cost_view(run, "formal_e2")

    assert view["pricing"] is PRICING
    assert view["input_usd"] == pytest.approx(
        5000 / 1e6 * PRICING["input_usd_per_1m"]
    )
    assert view["output_usd"] == pytest.approx(
        1650 / 1e6 * PRICING["output_usd_per_1m"]
    )


def test_roles_are_sorted_by_spend(run):
    """依花費排序，不是依呼叫順序 —— 要先看到最貴的那個角色。"""
    _report(run, token_usage=USAGE)
    rows = run_view.cost_view(run, "formal_e2")["rows"]
    assert [r["role"] for r in rows] == ["arbitration_agent", "observation_agent"]
    assert rows[0]["billable_output_tokens"] == 1100


def test_unmeasured_usage_is_not_zero_dollars(run):
    """「沒有量到」與「花了 0 元」必須分得出來。"""
    _report(run, token_usage={"measured": False, "reason": "no adapter"})
    view = run_view.cost_view(run, "formal_e2")

    assert view["available"] is False
    assert "no adapter" in view["reason"]
    assert "total_usd" not in view


def test_an_older_report_without_usage_explains_itself(run):
    """2026-09-04 之前的報告量了卻沒寫出。"""
    _report(run)
    view = run_view.cost_view(run, "formal_e2")
    assert view["available"] is False
    assert view["reason"]


def test_no_report_means_no_cost_page(run):
    view = run_view.cost_view(run, "sim_smoke")
    assert view["available"] is False


def test_cost_is_a_run_section(run):
    """有報告就顯示 Cost —— dry run 的「沒有呼叫過」本身也是事實。"""
    _report(run, token_usage=USAGE)
    assert run_view.availability(run, "formal_e2")["cost"] is True
    assert "cost" in [s.key for s in RUN_SECTIONS]


# ---------------------------------------------------------------------------
# cache 瀏覽
# ---------------------------------------------------------------------------


KEY = "a" * 64


@pytest.fixture()
def cache_root(tmp_path) -> Path:
    from pcmef.agents.cache import AGENT_ARTIFACT_NAMES

    root = tmp_path / "agents"
    folder = root / KEY
    folder.mkdir(parents=True)
    _write(folder / "manifest.json",
           {"cache_key": KEY, "provider_request_id": "req-1",
            "token_usage": 1650, "latency_ms": 900})
    for name in AGENT_ARTIFACT_NAMES:
        _write(folder / f"{name}.json", {"artifact": name})
    return root


def test_the_six_artifacts_are_listed_in_contract_order(cache_root):
    from pcmef.agents.cache import AGENT_ARTIFACT_NAMES

    entry = run_view.cache_entry_view(cache_root, KEY)
    assert [a["name"] for a in entry["artifacts"]] == list(AGENT_ARTIFACT_NAMES)
    assert entry["complete"] is True


def test_a_half_written_directory_is_not_complete(cache_root):
    """§48：六份缺一即不算命中。顯示成可用會讓人以為不必再付費。"""
    (cache_root / KEY / "arbitration_validated.json").unlink()
    entry = run_view.cache_entry_view(cache_root, KEY)
    assert entry["complete"] is False
    missing = [a for a in entry["artifacts"] if not a["present"]]
    assert [a["name"] for a in missing] == ["arbitration_validated"]


@pytest.mark.parametrize(
    "key",
    ["../secrets", "a" * 63, "a" * 65, "z" * 64, "", "../../etc/passwd",
     "a" * 32 + "/" + "a" * 31],
)
def test_only_a_real_digest_is_accepted(cache_root, key):
    """cache_key 直接組成目錄名。不限制就是一條讀取任意目錄的路徑。"""
    assert run_view.cache_entry_view(cache_root, key) is None


def test_an_unknown_key_is_absent(cache_root):
    assert run_view.cache_entry_view(cache_root, "b" * 64) is None


# ---------------------------------------------------------------------------
# 畫面
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, run, cache_root):
    from pcmef.admin.app import create_app
    from pcmef.console.runner import RunRecord

    record = RunRecord(
        run_id=run.name, kind="formal_e2", label="Formal E2",
        # mode 只可能是 dry-run 或 formal：runner 自有 formal_e2 起就拒絕
        # 其他值，"full" 從來不可能被寫進一筆真的紀錄（round 11）。
        params={"mode": "formal"}, command=["pcmef", "formal", "run-e2"],
        status="succeeded", started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00", exit_code=0,
    )
    (run / "run.json").write_text(json.dumps(record.to_json()), encoding="utf-8")
    # cache root 由**伺服器端設定**提供，不再從 URL 來：`?root=` 先前完全
    # 沒有限制，配上只驗格式的 cache_key 就是一條讀取任意目錄的路徑。
    app = create_app(registry_path=tmp_path / "r.db", vault_path=tmp_path / "v",
                     console_run_root=run.parent,
                     environ={"PCMEF_FORMAL_AGENT_CACHE": str(cache_root)},
        workspace_root=tmp_path / "workspace",
    )
    app.config["TESTING"] = True
    return app.test_client()


def test_the_cost_page_names_the_thinking_penalty(client, run):
    _report(run, token_usage=USAGE)
    body = client.get(f"/console/runs/{run.name}/cost").get_data(as_text=True)
    assert "thinking" in body
    assert "只看 completion 會低估" in body
    assert "促銷價" in body            # 費率會過期，必須標明
    assert "1,650" in body             # 計費 output，不是 350


def test_the_cost_page_says_when_nothing_was_measured(client, run):
    _report(run)
    body = client.get(f"/console/runs/{run.name}/cost").get_data(as_text=True)
    assert "沒有量到用量" in body
    assert "US$" not in body


def test_the_cost_page_loads_no_script(client, run):
    """SSE 只在 Overview。靜態分頁不該開連線。"""
    _report(run, token_usage=USAGE)
    body = client.get(f"/console/runs/{run.name}/cost").get_data(as_text=True)
    assert body.count("<script") == 0


def test_the_cache_page_renders(client):
    response = client.get(f"/console/cache/{KEY}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "六份齊全" in body
    assert "observation_raw" in body


def test_the_cache_root_cannot_come_from_the_url(client, tmp_path):
    """`?root=` 必須被忽略。

    兩個理由：它是一條讀取任意含 manifest.json 目錄的路徑，而且它讓畫面
    可以指向「不是這次 run 真正用的那一份」快取 —— 兩者長得一模一樣。
    """
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / KEY).mkdir(parents=True)
    (elsewhere / KEY / "manifest.json").write_text(
        json.dumps({"cache_key": KEY, "artifacts": []}), encoding="utf-8"
    )
    body = client.get(
        f"/console/cache/{KEY}?root={elsewhere}"
    ).get_data(as_text=True)
    # 仍然讀伺服器端設定的那一份，因此六份 artifact 齊全。
    assert "六份齊全" in body
    assert str(elsewhere) not in body


def test_the_cache_page_has_no_delete_control(client):
    """快取項目是某次執行的證據，不得在瀏覽時順手清掉。"""
    body = client.get(f"/console/cache/{KEY}").get_data(as_text=True)
    for control in ("<form", "<button", "<input"):
        assert control not in body, control


def test_a_bad_cache_key_is_404(client):
    assert client.get(f"/console/cache/{'z' * 64}").status_code == 404
