# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 自動載入，供 tests/console/ 底下的測試共用。
#         不被任何 production 模組匯入。
# 檔案路徑: tests/console/conftest.py
# 產生時間: 2026-09-06 23:55 +08:00
# 版本: v0.1.0
# 功能說明: console 測試共用的啟動輔助 —— 以合法歸屬啟動一次 run。
# 模組定位: Execution/Action Layer Closure round 2 的夾具側。
#           歸屬成為啟動的必要條件之後，直接 `runner.start()` 造出來的
#           run 是**孤兒**，任何 run-scoped 端點都會正確地回 404。
#           那不是測試環境的問題，是那些夾具在模擬一個現在已經
#           不存在的啟動路徑。
# 主要責任:
#   1. start_attributed fixture 以與 console.launch 相同的順序啟動 run
# 維護提醒:
#   - **不得改成放寬 owned_by() 或 capabilities_for() 來讓舊測試變綠。**
#     那會把要驗收的規則本身拆掉，而拆掉之後測試會全綠。
#   - 不得在這裡跳過 write_attribution()。夾具要造的是一筆「合法的、
#     有主人的」執行，不是一筆「剛好能被讀到的」執行。
#   - v0.1.0 新增：首版，對應 Execution Layer Closure round 2。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/ -q
# ------------------------------------------------------------

from __future__ import annotations

import pytest


def _start_attributed(app, spec, *, project_id: str = "pcmef-thesis"):
    """以合法歸屬啟動一次 run，回傳 RunRecord。

    順序與 `pcmef.console.launch.launch_run()` 相同：先配 id、先寫歸屬，
    再啟動行程。差別只在不經 HTTP 層 —— 這些測試驗的是 run 頁與清單，
    不是端點的准駁。
    """
    from pcmef.platform.pipeline import build_definition
    from pcmef.platform.runs import RunAttribution, digest_of, write_attribution

    runner = app.config["PCMEF_CONSOLE_RUNNER"]
    run_id = runner.allocate_run_id()
    definition = build_definition(None, None)
    write_attribution(runner.run_dir(run_id), RunAttribution(
        run_id=run_id,
        project_id=project_id,
        project_name=project_id,
        pipeline_id=definition.pipeline_id,
        pipeline_digest=digest_of(definition.to_json()),
        stage_ids=definition.stage_ids,
        pipeline_snapshot=definition.to_json(),
    ))
    return runner.start(spec, run_id=run_id)


@pytest.fixture()
def start_attributed():
    """啟動一次有主人的 run。用法：`start_attributed(app, spec)`。"""
    return _start_attributed
