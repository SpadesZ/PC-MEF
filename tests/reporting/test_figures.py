# PC-MEF Research System source maintenance contract
# 上下游: 驗 pcmef/reporting/style.py 與 pcmef/reporting/figures.py。
# 檔案路徑: tests/reporting/test_figures.py
# 產生時間: 2026-09-04 16:55 +08:00
# 版本: v0.1.0
# 功能說明: 確認論文圖只讀 report、比例尺不截斷、輸出可逐 byte 重現。
# 模組定位: P2-6 的驗收。圖是最容易在不知不覺間說謊的產物 ——
#           截一次軸就能讓 0.02 的差看起來像兩倍，因此規則要用測試釘住。
# 主要責任:
#   1. 比例類指標的 y 軸必須是 0–1
#   2. 臂的順序固定為 G1→G5，不得依數值重排
#   3. 同一份 report 產兩次必須逐 byte 相同
#   4. report 缺欄位時跳過該張並指名缺什麼，不得整批失敗
# 維護提醒:
#   - 不得放寬 test_the_scale_is_never_truncated。截軸在論文裡是造假，
#     這一條與 figures4papers 的 "tighten Y-limits" 相反是刻意的。
#   - 不得把 test_arms_keep_their_narrative_order 改成依數值排序。
#     依高低排會讓不同 run 的圖無法並排比較。
#   - 不得移除可重現性測試。少了 svg.hashsalt，SVG 的 clip-path id 會
#     隨物件位址改變，圖與報告就無法用 hash 對應。
# 驗證方式:
#   - py -3.10 -m pytest tests/reporting/test_figures.py -v
# ------------------------------------------------------------

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

from pcmef.reporting import figures, style  # noqa: E402


@pytest.fixture(autouse=True)
def _style():
    style.apply()


@pytest.fixture()
def report() -> dict:
    """一份完整的 report，形狀與 e2_formal 實際輸出相同。"""
    arms = ("vision_only", "tof_only", "fixed_fusion",
            "reliability_routing", "pcmef_full")
    conditions = ("clean", "vision_degraded", "tof_degraded", "conflict")
    return {
        "report_id": "formal_e2_full_pcmef",
        "worst_condition_macro_f1": {
            "vision_only": 0.5344624199528143,
            "tof_only": 0.145,
            "fixed_fusion": 0.16346153846153846,
            "reliability_routing": 0.18055555555555555,
            "pcmef_full": 0.1216949152542373,
        },
        "worst_condition_at": {
            "vision_only": "conflict", "tof_only": "conflict",
            "fixed_fusion": "tof_degraded", "reliability_routing": "conflict",
            "pcmef_full": "conflict",
        },
        "per_condition": {
            arm: {
                condition: {"accuracy": 0.5, "macro_f1": 0.25 + 0.1 * index,
                            "n": 96}
                for index, condition in enumerate(conditions)
            }
            for arm in arms
        },
        "worst_condition_statistics": {
            "estimator": "cluster_bootstrap_worst_condition_delta",
            "comparisons": {
                "pcmef_full_vs_vision_only": {
                    "delta": -0.4128, "ci_lower": -0.5034,
                    "ci_upper": -0.2921, "significant_at_95": True},
                "pcmef_full_vs_tof_only": {
                    "delta": -0.0233, "ci_lower": -0.0250,
                    "ci_upper": -0.0197, "significant_at_95": True},
                "pcmef_full_vs_fixed_fusion": {
                    "delta": -0.0418, "ci_lower": -0.0642,
                    "ci_upper": -0.0250, "significant_at_95": True},
                "pcmef_full_vs_reliability_routing": {
                    "delta": -0.0589, "ci_lower": -0.0895,
                    "ci_upper": -0.0250, "significant_at_95": True},
            },
        },
        "statistics": {
            "pcmef_full_vs_fixed_fusion": {
                "macro_f1": {"delta": 0.0070, "ci_lower": -0.0443,
                             "ci_upper": 0.0503, "significant_at_95": False}},
            "pcmef_full_vs_tof_only": {
                "macro_f1": {"delta": 0.0080, "ci_lower": -0.0300,
                             "ci_upper": 0.0460, "significant_at_95": False}},
        },
        "routing": {
            "counts": {"fusion": 113, "trust_vision": 62,
                       "trust_tof": 66, "escalated": 143},
            "escalation_rate": 0.3724,
        },
    }


# ---------------------------------------------------------------------------
# 樣式
# ---------------------------------------------------------------------------


def test_style_never_requires_latex():
    """usetex 需要機器上有 LaTeX，缺了會在投稿前一晚才炸。"""
    assert style.RC_PARAMS["text.usetex"] is False


def test_style_keeps_a_guaranteed_font_last():
    """Arial/Helvetica 在多數 Linux 與 CI 上不存在。"""
    assert "DejaVu Sans" in style.RC_PARAMS["font.sans-serif"]


def test_style_pins_the_svg_hashsalt():
    """沒有它，SVG 的 clip-path id 會隨物件位址改變。"""
    assert style.RC_PARAMS["svg.hashsalt"]


def test_svg_text_stays_editable():
    """投稿前要改一個標籤時不必重跑整條 pipeline。"""
    assert style.RC_PARAMS["svg.fonttype"] == "none"


# ---------------------------------------------------------------------------
# 比例尺與順序
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "builder", [figures.worst_condition_macro_f1, figures.per_condition_macro_f1]
)
def test_the_scale_is_never_truncated(report, builder):
    """比例類指標一律 0–1。

    截軸能讓 0.02 的差看起來像兩倍。這裡的資料最大值只有 0.53，
    自動縮放會把上界拉到 0.55 左右 —— 正是要防的那種圖。
    """
    import matplotlib.pyplot as plt

    figure = builder(report)
    try:
        ax = figure.axes[0]
        assert ax.get_ylim() == (0.0, 1.0)
    finally:
        plt.close(figure)


def test_arms_keep_their_narrative_order(report):
    """G1→G5 是論文的敘事順序，不得依數值重排。

    依高低排的話，這份資料會變成 G1, G4, G3, G2, G5，
    而下一次 run 又是另一個順序，兩張圖無法並排。
    """
    import matplotlib.pyplot as plt

    figure = figures.worst_condition_macro_f1(report)
    try:
        labels = [t.get_text() for t in figure.axes[0].get_xticklabels()]
        assert [label.split("\n")[0] for label in labels] == \
            ["G1", "G2", "G3", "G4", "G5"]
    finally:
        plt.close(figure)


def test_the_worst_condition_is_named_on_each_bar(report):
    """只給數值的話，看不出 G3 最弱在 tof_degraded 而其餘在 conflict。"""
    import matplotlib.pyplot as plt

    figure = figures.worst_condition_macro_f1(report)
    try:
        texts = " ".join(t.get_text() for t in figure.axes[0].texts)
        assert "tof_degraded" in texts
        assert "conflict" in texts
        assert "0.122" in texts        # pcmef_full 的值
    finally:
        plt.close(figure)


def test_the_forest_plot_marks_zero_and_orders_by_arm(report):
    """差值圖的零線是參照。橫跨零就是不顯著。"""
    import matplotlib.pyplot as plt

    figure = figures.paired_delta_forest(report)
    try:
        ax = figure.axes[0]
        assert any(
            line.get_xdata()[0] == 0.0 and line.get_linestyle() == "--"
            for line in ax.get_lines() if len(line.get_xdata()) == 2
        )
        labels = [t.get_text() for t in ax.get_yticklabels()]
        assert [label.split()[0] for label in labels] == ["G1", "G2", "G3", "G4"]
    finally:
        plt.close(figure)


def test_the_forest_plot_flags_a_non_significant_interval(report):
    """整體 macro-F1 那一組全都橫跨零，圖上要標 n.s.。

    worst-condition 顯著而 overall 不顯著，兩者並存本身是結果的一部分。
    """
    import matplotlib.pyplot as plt

    figure = figures.paired_delta_forest(report, block="overall")
    try:
        texts = " ".join(t.get_text() for t in figure.axes[0].texts)
        assert "n.s." in texts
    finally:
        plt.close(figure)


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------


def _hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
    }


def test_the_same_report_produces_byte_identical_files(report, tmp_path):
    """否則無法用 hash 確認圖與報告對得上。"""
    first = tmp_path / "a"
    second = tmp_path / "b"
    figures.export_all(report, first)
    figures.export_all(report, second)
    assert _hashes(first) == _hashes(second)


def test_export_writes_all_three_formats(report, tmp_path):
    result = figures.export_all(report, tmp_path)
    assert not result["skipped"]
    for name in figures.FIGURES:
        for suffix in figures.FORMATS:
            assert (tmp_path / f"{name}.{suffix}").exists(), f"{name}.{suffix}"


def test_a_single_format_can_be_requested(report, tmp_path):
    figures.export_all(report, tmp_path, formats=("pdf",))
    assert not list(tmp_path.glob("*.png"))
    assert len(list(tmp_path.glob("*.pdf"))) == len(figures.FIGURES)


def test_a_missing_field_skips_one_figure_and_names_it(report, tmp_path):
    """舊報告或 dry-run 少幾個欄位是常態。

    少一張就整批不產出，只會逼人回頭手動畫。
    """
    del report["worst_condition_macro_f1"]
    del report["worst_condition_statistics"]
    result = figures.export_all(report, tmp_path)

    assert "worst_condition_macro_f1" in result["skipped"]
    assert "paired_delta_worst_condition" in result["skipped"]
    # 缺哪一個欄位要指名道姓。
    assert "worst_condition_macro_f1" in result["skipped"]["worst_condition_macro_f1"]
    # 其餘照產。
    assert (tmp_path / "per_condition_macro_f1.pdf").exists()
    assert (tmp_path / "routing_distribution.pdf").exists()


def test_a_dry_run_report_still_yields_what_it_can(tmp_path):
    """dry-run 沒有 pcmef_full，四條臂的圖仍要畫得出來。"""
    dry = {
        "report_id": "formal_e2_dry_run",
        "per_condition": {
            arm: {"clean": {"macro_f1": 0.9, "n": 96},
                  "conflict": {"macro_f1": 0.2, "n": 96}}
            for arm in ("vision_only", "tof_only", "fixed_fusion",
                        "reliability_routing")
        },
        "routing": {"counts": {"fusion": 107, "escalated": 142}},
    }
    result = figures.export_all(dry, tmp_path, formats=("png",))
    assert (tmp_path / "per_condition_macro_f1.png").exists()
    assert "worst_condition_macro_f1" in result["skipped"]


def test_figures_do_not_recompute_anything(report, tmp_path):
    """圖上的數字必須逐字來自 report。

    把 report 裡的值改掉，圖上的標註必須跟著改 —— 若圖是自己從
    per_condition 重算 worst-condition，這個測試會失敗。
    """
    import matplotlib.pyplot as plt

    report["worst_condition_macro_f1"]["pcmef_full"] = 0.4242
    report["worst_condition_at"]["pcmef_full"] = "clean"
    figure = figures.worst_condition_macro_f1(report)
    try:
        texts = " ".join(t.get_text() for t in figure.axes[0].texts)
        assert "0.424" in texts
        assert "(clean)" in texts
    finally:
        plt.close(figure)
