# PC-MEF Research System source maintenance contract
# 上下游: 由 pytest 收集執行；在 tmp_path 造出合成的 transient.npy 與
#         manifest 餵給 pcmef.console.results；不跑 mitsuba、不連線。
# 檔案路徑: tests/console/test_results.py
# 產生時間: 2026-08-27 19:10 +08:00
# 版本: v0.1.0
# 功能說明: 驗證結果讀取層不重算任何科學量、大檔會先抽樣、
#           以及 SVG 真的帶得出可見的顏色。
# 模組定位: NOTE-025 的可執行防線。它不驗證圖好不好看。
# 主要責任:
#   1. test_curve_matches_the_artifact 驗證畫面數字與 artifact 一致
#   2. test_large_transients_are_downsampled 驗證不把 HTML 撐爆
#   3. test_chart_uses_defined_css_variables 擋下「畫得出來但透明」
#   4. test_missing_artifacts_degrade_gracefully 驗證缺檔不拋例外
# 維護提醒:
#   - 不得放寬 test_chart_uses_defined_css_variables。曲線用未定義的
#     CSS 變數上色時 stroke 會是 none —— 畫面上只剩圖例，
#     而沒有任何其他測試會失敗。
#   - v0.1.0 新增：首版，對應 NOTE-025。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_results.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from pcmef.console.results import (
    MAX_CHART_POINTS,
    bar_chart_svg,
    downsample,
    line_chart_svg,
    load_results,
)

ADMIN_CSS = Path(__file__).resolve().parents[2] / "pcmef" / "admin" / "static" / "admin.css"


def _write_scenario(root: Path, name: str, bins: int = 128, peak: int = 40) -> float:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    # (H, W, bins, channels)，在 peak 處給一個尖峰。
    cube = np.zeros((2, 2, bins, 3), dtype=np.float32)
    cube[:, :, peak, :] = 5.0
    cube[:, :, peak + 1, :] = 2.0
    np.save(folder / "transient.npy", cube)
    np.save(folder / "transient_time.npy", np.linspace(0, 1e-9, bins))
    return float(cube.sum())


def test_curve_matches_the_artifact(tmp_path):
    """畫面上的數字必須與 artifact 完全一致，不得重算。"""
    total = _write_scenario(tmp_path, "smoke_empty_0001")
    bundle = load_results(tmp_path, "run")

    assert len(bundle.curves) == 1
    curve = bundle.curves[0]
    assert curve.total_energy == pytest.approx(total)
    assert curve.scenario_id == "smoke_empty_0001"
    assert curve.peak_time_ns > 0


def test_multiple_scenarios_are_sorted(tmp_path):
    for name in ("smoke_misty_0004", "smoke_empty_0001", "smoke_bubbly_0042"):
        _write_scenario(tmp_path, name)
    ids = [c.scenario_id for c in load_results(tmp_path).curves]
    assert ids == sorted(ids)


def test_large_transients_are_downsampled(tmp_path):
    """4096 bins 全塞進 SVG 會產生數 MB 的 HTML，瀏覽器會卡住。"""
    _write_scenario(tmp_path, "smoke_empty_0001", bins=4096, peak=1000)
    curve = load_results(tmp_path).curves[0]

    assert len(curve.energy) <= MAX_CHART_POINTS
    assert len(curve.times_ns) == len(curve.energy)
    # 但總能量仍取自完整資料，不是抽樣後的和。
    assert curve.total_energy == pytest.approx(4 * 3 * (5.0 + 2.0))


def test_downsample_preserves_endpoints():
    values = list(range(1000))
    reduced = downsample(values, limit=10)
    assert len(reduced) == 10
    assert reduced[0] == 0.0
    assert reduced[-1] == 999.0


def test_downsample_leaves_short_series_alone():
    assert downsample([1.0, 2.0, 3.0], limit=10) == [1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# 圖表
# ---------------------------------------------------------------------------


def test_chart_uses_defined_css_variables():
    """曲線用未定義的變數上色時 stroke 會是 none —— 畫得出來但完全透明。

    這類缺陷不會讓任何其他測試失敗，只有打開瀏覽器才看得到，
    因此在這裡把「圖表用到的變數」與「CSS 定義的變數」對起來。
    """
    svg = line_chart_svg([("a", [0, 1], [0, 1]), ("b", [0, 1], [1, 0])])
    used = set(re.findall(r"var\((--chart-\d+)\)", svg))
    assert used, "the chart declared no palette variables"

    css = ADMIN_CSS.read_text(encoding="utf-8")
    defined = set(re.findall(r"(--chart-\d+)\s*:", css))
    assert used <= defined, f"undefined palette variables: {sorted(used - defined)}"


def test_line_chart_draws_one_polyline_per_series():
    svg = line_chart_svg([("a", [0, 1, 2], [0, 1, 0]), ("b", [0, 1, 2], [1, 0, 1])])
    assert svg.count("<polyline") == 2
    assert 'class="legend"' in svg
    assert svg.startswith("<svg")


def test_bar_chart_draws_one_rect_per_bar():
    svg = bar_chart_svg(["x", "y", "z"], [1.0, 2.0, 3.0])
    assert svg.count("<rect") == 3


def test_empty_series_render_a_message_not_a_broken_svg():
    assert "沒有可繪製" in line_chart_svg([])
    assert "沒有可繪製" in bar_chart_svg([], [])


def test_charts_carry_an_accessible_label():
    svg = line_chart_svg([("a", [0, 1], [0, 1])], x_label="time", y_label="energy")
    assert 'role="img"' in svg
    assert "aria-label" in svg


# ---------------------------------------------------------------------------
# 缺檔
# ---------------------------------------------------------------------------


def test_missing_artifacts_degrade_gracefully(tmp_path):
    bundle = load_results(tmp_path / "nothing-here", "run")
    assert bundle.curves == []
    assert bundle.notes
    assert not bundle.has_anything


def test_a_folder_without_transients_is_skipped(tmp_path):
    (tmp_path / "not-a-scenario").mkdir()
    assert load_results(tmp_path).curves == []


def test_manifest_and_surrogate_are_read_when_present(tmp_path):
    _write_scenario(tmp_path, "smoke_empty_0001")
    (tmp_path / "simulation_smoke_manifest.json").write_text(
        json.dumps({"counts": {"ok": 1, "failed": 0}}), encoding="utf-8"
    )
    (tmp_path / "surrogate_smoke.csv").write_text(
        "scenario_id,all_finite\nsmoke_empty_0001,True\n", encoding="utf-8"
    )
    bundle = load_results(tmp_path)
    assert bundle.manifest["counts"]["ok"] == 1
    assert bundle.surrogate_rows[0]["all_finite"] == "True"


def test_an_empty_surrogate_csv_is_not_a_row(tmp_path):
    _write_scenario(tmp_path, "smoke_empty_0001")
    (tmp_path / "surrogate_smoke.csv").write_text("", encoding="utf-8")
    assert load_results(tmp_path).surrogate_rows == []
