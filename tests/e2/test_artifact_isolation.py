# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；測 pcmef.experiments.e2_formal.run_artifact_root
#         的目錄佈局，以及 console 指標 / run_view 是否照它走。
#         不執行任何 case、不呼叫 provider、不觸碰 families 36-43。
# 檔案路徑: tests/e2/test_artifact_isolation.py
# 產生時間: 2026-09-05 16:30 +08:00
# 版本: v0.1.0
# 功能說明: formal 與 dry-run 的產物不得共用任何路徑。
# 模組定位: NOTE-078 的可執行防線。先前兩者共用 `<out>/trace` 與
#           `<out>/stress`，只有報告檔名不同 —— 正式跑完之後再跑一次預演，
#           會直接覆蓋正式的 trace 與 stress，而報告還在，看起來一切正常。
#           一次性保護了報告，卻沒保護它的證據。
# 主要責任:
#   1. test_formal_and_dry_run_share_no_path 兩者無交集
#   2. test_two_dry_runs_do_not_collide 預演之間也不互相覆蓋
#   3. test_the_executor_writes_everything_under_one_root 同屬一次 run
#   4. test_an_old_run_page_cannot_read_a_newer_run
#   5. test_the_console_pointer_names_the_run_specific_root
# 維護提醒:
#   - 不得讓 dry-run 回到「共用 canonical 目錄」。那正是這一組測試存在的原因。
#   - 不得靠畫面過濾來達成隔離。舊 run 頁讀不到新產物，是因為它們不在
#     同一個目錄裡，不是因為有人記得過濾。
#   - v0.1.0 新增：首版，對應 P0-1 / NOTE-078。
# 驗證方式:
#   - py -3.10 -m pytest tests/e2/test_artifact_isolation.py -v
# ------------------------------------------------------------

from __future__ import annotations

import inspect

import pytest

from pcmef.experiments.e2_formal import (
    DRY_RUN_SUBDIR, FORMAL_SUBDIR, FormalE2Error, run_artifact_root,
)

OUT = "outputs/perception/e2_final"


def test_formal_and_dry_run_share_no_path():
    """兩者不得有任何共用路徑 —— 包含 trace 與 stress 子目錄。"""
    formal = run_artifact_root(OUT, dry_run=False)
    dry = run_artifact_root(OUT, dry_run=True, run_id="r1")

    assert formal != dry
    assert formal not in dry.parents
    assert dry not in formal.parents
    for child in ("trace", "stress"):
        assert (formal / child) != (dry / child)


def test_two_dry_runs_do_not_collide():
    """第二次預演不得覆蓋第一次的 trace 與 stress。"""
    a = run_artifact_root(OUT, dry_run=True, run_id="20260905T010000")
    b = run_artifact_root(OUT, dry_run=True, run_id="20260905T020000")
    assert a != b
    assert (a / "trace") != (b / "trace")
    assert (a / "stress") != (b / "stress")


def test_the_layout_is_named_not_improvised():
    assert run_artifact_root(OUT, dry_run=False).name == FORMAL_SUBDIR
    assert run_artifact_root(OUT, dry_run=True, run_id="x").parent.name == (
        DRY_RUN_SUBDIR
    )


def test_a_dry_run_without_a_run_id_is_refused():
    """沒有 run_id 就沒有專屬目錄，等於回到共用狀態。"""
    with pytest.raises(FormalE2Error, match="own run_id"):
        run_artifact_root(OUT, dry_run=True)


@pytest.mark.parametrize("run_id", ["../escape", "..", "/", "\\", "   "])
def test_a_run_id_cannot_escape_the_dry_run_root(run_id):
    """run_id 來自呼叫端，不驗就是一條寫到任意目錄的路徑。"""
    root = run_artifact_root(OUT, dry_run=True, run_id="safe")
    try:
        produced = run_artifact_root(OUT, dry_run=True, run_id=run_id)
    except FormalE2Error:
        return                                  # 全部剝光即拒絕，也可接受
    assert produced.parent == root.parent, produced


def test_the_executor_writes_everything_under_one_root():
    """report、trace、stress 必須全部落在同一個 root。

    以原始碼檢查而不是實跑：實跑一次 384 列的執行只為了確認路徑，代價
    與收益不成比例，而路徑是靜態可讀的。
    """
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal._execute_formal_e2)
    # out 由 run_artifact_root 決定，而不是直接用傳進來的 out_dir。
    assert "out = run_artifact_root(canonical_out" in source

    # stress、trace 與 report 都吃那個 out。
    #
    # 用正規化過的空白比對，不綁縮排：先前這裡寫死了「12 個空格」的版本，
    # 於是把一層 try 拿掉就會失敗 —— 而那與「產物是不是落在同一個 root」
    # 完全無關。
    flat = " ".join(source.split())
    assert "prepare_cases( base_manifest_dir, stack, out," in flat
    assert "TraceWriter( out," in flat
    assert "(out / filename).write_text" in source


def test_the_report_records_its_own_root():
    """報告要說得出自己的產物在哪 —— 否則「同屬一次 run」無法被查證。"""
    from pcmef.experiments import e2_formal

    source = inspect.getsource(e2_formal._execute_formal_e2)
    assert '"artifact_root": out.as_posix()' in source
    assert '"canonical_out": canonical_out.as_posix()' in source


# ---------------------------------------------------------------------------
# 畫面：舊 run 頁不得讀到新 run 的產物
# ---------------------------------------------------------------------------


def test_the_console_pointer_names_the_run_specific_root(tmp_path):
    from pcmef.console.runner import ConsoleRunner

    runner = ConsoleRunner(tmp_path / "runs", formal_out=tmp_path / "e2_final")
    runner._write_formal_pointer("dry-1", "dry-run")
    pointer = runner.formal_pointer("dry-1")

    expected = run_artifact_root(
        tmp_path / "e2_final", dry_run=True, run_id="dry-1"
    )
    assert pointer["artifact_root"] == expected.as_posix()
    assert pointer["report"].startswith(expected.as_posix())
    assert pointer["trace_index"].startswith(expected.as_posix())


def test_an_old_run_page_cannot_read_a_newer_run(tmp_path):
    """兩次預演的 run 頁必須各自讀到自己的產物。"""
    from pcmef.console import run_view
    from pcmef.console.runner import ConsoleRunner

    runner = ConsoleRunner(tmp_path / "runs", formal_out=tmp_path / "e2_final")
    runner._write_formal_pointer("older", "dry-run")
    runner._write_formal_pointer("newer", "dry-run")

    older = run_view.artifact_root(runner.run_dir("older"))
    newer = run_view.artifact_root(runner.run_dir("newer"))
    assert older != newer

    # 在 newer 的 root 寫一份報告；older 的頁面不得看到它。
    (newer).mkdir(parents=True, exist_ok=True)
    (newer / "formal_e2_dry_run.json").write_text("{}", encoding="utf-8")
    assert not (older / "formal_e2_dry_run.json").exists()


def test_a_dry_run_pointer_never_points_into_the_formal_root(tmp_path):
    from pcmef.console.runner import ConsoleRunner

    runner = ConsoleRunner(tmp_path / "runs", formal_out=tmp_path / "e2_final")
    runner._write_formal_pointer("dry-1", "dry-run")
    formal_root = run_artifact_root(tmp_path / "e2_final", dry_run=False)
    assert formal_root.as_posix() not in runner.formal_pointer("dry-1")["report"]


def test_the_legacy_pointer_still_resolves(tmp_path):
    """2026-09-05 之前的指標只有 canonical_out，仍然要能解析。"""
    import json

    from pcmef.console import run_view

    run_dir = tmp_path / "runs" / "legacy"
    run_dir.mkdir(parents=True)
    (run_dir / "formal_output.json").write_text(
        json.dumps({"canonical_out": "outputs/perception/e2_final"}),
        encoding="utf-8",
    )
    from pathlib import Path

    assert run_view.artifact_root(run_dir) == Path("outputs/perception/e2_final")
