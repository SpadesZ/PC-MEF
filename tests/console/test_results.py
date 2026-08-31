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
# 可讀性：圖必須答得出它宣稱要回答的問題
# ---------------------------------------------------------------------------


def _tick_texts(svg: str) -> list[str]:
    return re.findall(r'class="tick"[^>]*>([^<]*)</text>', svg)


def test_both_axes_start_at_zero():
    """transient 波形的意義是「訊號從零長起來、多久之後到峰值」。

    x 軸截在第一個 bin 會讓 time-of-flight 讀不出來 —— 畫面上看得到形狀，
    卻無法把峰值位置換算成距離，而那正是 ToF 這張圖的用途。
    """
    svg = line_chart_svg(
        [("a", [3.0, 4.0, 5.0], [10.0, 40.0, 12.0])],
        x_label="time (ns)", y_label="energy",
    )
    ticks = _tick_texts(svg)
    # 兩軸各自的第一個刻度都必須是 0，即使資料從 3.0 才開始。
    assert ticks.count("0") >= 2, ticks


def test_the_line_chart_marks_the_peak():
    """「峰值有沒有貼在時間窗右緣」是這張圖唯一的科學檢查（NOTE-013）。

    沒有標記的話只能肉眼估，而肉眼估不出「差一點就被截斷」與「剛好沒被截斷」
    的差別 —— 那兩種情況一個可用一個不可用。
    """
    svg = line_chart_svg([("a", [0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 9.0, 3.0])])

    assert "peak-line" in svg
    assert any("峰值" in text for text in _tick_texts(svg))
    # 標的是真正的峰（x=2.0），不是最後一點。
    assert any("峰值 2" in text for text in _tick_texts(svg))


def test_the_peak_label_is_never_clipped_off_the_top():
    """最高的那條曲線頂點就在上緣，標籤不夾住就會被 viewBox 切掉。"""
    svg = line_chart_svg([("a", [0.0, 1.0], [0.0, 100.0])])
    label_ys = [
        float(y)
        for y in re.findall(r'<text[^>]*y="([\d.]+)"[^>]*class="tick"[^>]*>峰值', svg)
    ]
    assert label_ys and all(y >= 10.0 for y in label_ys), label_ys


def test_the_bar_chart_y_axis_is_not_truncated():
    """截斷 y 軸能讓 0.3% 的差看起來像三倍。那在論文裡是造假。

    這條測試存在，是因為「四根柱子看起來一樣高」看起來像個 bug，
    而最直覺的『修法』正好是把 y 軸起點抬高 —— 必須擋住。
    """
    svg = bar_chart_svg(["a", "b"], [1000.0, 1003.0])
    assert "0" in _tick_texts(svg)

    # 值差 0.3%，柱高就必須也只差 0.3%。
    heights = [float(h) for h in re.findall(r'<rect[^>]*height="([\d.]+)"', svg)]
    assert len(heights) == 2
    assert heights[0] / heights[1] == pytest.approx(1000.0 / 1003.0, rel=1e-3)


def test_near_identical_bars_still_report_their_difference():
    """y 軸不截斷，柱子就必然等高 —— 差異改由數字承擔，不是消失。"""
    svg = bar_chart_svg(["empty", "water", "milk", "oil"], [3908.9, 3921.4, 3895.2, 3912.7])
    ticks = _tick_texts(svg)

    assert any(t.startswith("+") and t.endswith("%") for t in ticks), ticks
    assert any(t.startswith("-") and t.endswith("%") for t in ticks), ticks
    assert any("平均" in t for t in ticks), ticks
    assert "mean-line" in svg


def test_a_single_bar_does_not_take_over_the_canvas():
    """只跑一類時，沒有上限的柱子會佔掉六成畫布，看起來像出了什麼事。"""
    svg = bar_chart_svg(["only"], [42.0])
    widths = [float(w) for w in re.findall(r'<rect[^>]*width="([\d.]+)"', svg)]
    assert widths and widths[0] <= 96.0, widths
    # 單一項目沒有「相對平均」可言，不該憑空生出一條 0% 的註記。
    assert not any("%" in t for t in _tick_texts(svg))


def test_charts_declare_only_css_classes_the_stylesheet_defines():
    """格線與峰值線用未定義的 class 時，SVG 預設 stroke 是黑色實線 ——
    格線會蓋過資料，而沒有任何測試會失敗。"""
    svg = line_chart_svg([("a", [0, 1], [0, 1])]) + bar_chart_svg(["a", "b"], [1.0, 2.0])
    used = set(re.findall(r'class="(grid-line|peak-line|mean-line|axis|series)"', svg))
    css = ADMIN_CSS.read_text(encoding="utf-8")
    for name in used:
        assert f".chart .{name}" in css, f"admin.css defines no .chart .{name}"


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
