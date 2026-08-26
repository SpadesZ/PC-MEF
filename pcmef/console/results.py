# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.console.routes 呼叫；讀取某次 run 的 artifacts 目錄下的
#         simulation_smoke_manifest.json、transient*.npy、surrogate_smoke.csv
#         與 e1_gate_audit.json；輸出可直接嵌進 HTML 的 SVG 與資料表。
#         本檔只讀不寫。
# 檔案路徑: pcmef/console/results.py
# 產生時間: 2026-08-27 16:35 +08:00
# 版本: v0.1.0
# 功能說明: 把一次執行留下的檔案讀成畫面上看得懂的東西 —— 時間軸曲線、
#           四特徵表、各場景能量比較，以及 gate 燈號。
# 模組定位: Console 的呈現資料層。它「不是」計算層 ——
#           不重新計算任何科學量，只讀既有 artifact。
# 主要責任:
#   1. load_simulation() 讀 manifest 與 transient，產出逐場景曲線
#   2. load_surrogate() 讀 surrogate_smoke.csv 產出四特徵表
#   3. line_chart_svg() / bar_chart_svg() 以伺服器端 SVG 繪圖
#   4. downsample() 把 transient 壓到適合畫圖的點數
#   5. ResultBundle 彙整一次 run 可呈現的全部內容
# 維護提醒:
#   - 不得在此重算任何科學量。畫面上的數字必須與 artifact 完全一致，
#     否則「看到的」與「凍結的」會分岔，而分岔時沒有人會發現。
#   - 不得引入 matplotlib 之類的繪圖相依來畫這幾張圖。FR-020 要的
#     thesis-ready figure 由 CSV 另行產生；console 的圖是給人看趨勢的，
#     伺服器端 SVG 足夠且維持零 script。
#   - 不得把 transient 全部點數塞進 SVG。128 bins 沒問題，但 4096 bins
#     會產生數 MB 的 HTML，瀏覽器會卡住 —— 一律先 downsample。
#   - v0.1.0 新增：首版，決策見 NOTE-025。
# 驗證方式:
#   - py -3.10 -m pytest tests/console/test_results.py -v
# ------------------------------------------------------------

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

__all__ = [
    "ResultBundle",
    "ScenarioCurve",
    "load_results",
    "line_chart_svg",
    "bar_chart_svg",
    "downsample",
    "MAX_CHART_POINTS",
]

#: SVG 內單條曲線的最大點數。超過就先抽樣 —— 再多的點在畫面上分辨不出來，
#: 但會讓 HTML 膨脹到瀏覽器卡住。
MAX_CHART_POINTS = 240

_PALETTE = ("var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)")


@dataclass(frozen=True)
class ScenarioCurve:
    """單一場景的 transient 時間軸曲線。"""

    scenario_id: str
    times_ns: tuple[float, ...]
    energy: tuple[float, ...]
    total_energy: float
    peak_time_ns: float
    bin_width_s: float


@dataclass
class ResultBundle:
    """一次 run 可呈現的全部內容。"""

    run_id: str
    curves: list[ScenarioCurve] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    surrogate_rows: list[dict[str, str]] = field(default_factory=list)
    gate_report: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def has_anything(self) -> bool:
        return bool(self.curves or self.surrogate_rows or self.gate_report)


def downsample(values: Sequence[float], limit: int = MAX_CHART_POINTS) -> list[float]:
    """把序列均勻抽樣到 limit 點以內，保留頭尾。"""
    array = np.asarray(values, dtype=np.float64)
    if array.size <= limit:
        return [float(v) for v in array]
    indices = np.linspace(0, array.size - 1, limit).astype(int)
    return [float(array[i]) for i in indices]


# ---------------------------------------------------------------------------
# 讀取
# ---------------------------------------------------------------------------


def _load_curve(folder: Path) -> ScenarioCurve | None:
    transient_path = folder / "transient.npy"
    axis_path = folder / "transient_time.npy"
    if not (transient_path.exists() and axis_path.exists()):
        return None
    transient = np.load(transient_path)
    axis = np.load(axis_path)
    # transient 形狀是 (H, W, bins, channels)；對空間與通道求和後
    # 得到「每個時間 bin 收到多少能量」，那正是要畫的曲線。
    per_bin = np.asarray(transient, dtype=np.float64)
    while per_bin.ndim > 1:
        per_bin = per_bin.sum(axis=0) if per_bin.ndim > 2 else per_bin.sum(axis=-1)
    per_bin = np.asarray(per_bin, dtype=np.float64).ravel()
    times = np.asarray(axis, dtype=np.float64).ravel()[: per_bin.size]

    if per_bin.size == 0 or times.size == 0:
        return None
    peak_index = int(np.argmax(per_bin))
    bin_width = float(times[1] - times[0]) if times.size > 1 else 0.0
    return ScenarioCurve(
        scenario_id=folder.name,
        times_ns=tuple(v * 1e9 for v in downsample(times)),
        energy=tuple(downsample(per_bin)),
        total_energy=float(per_bin.sum()),
        peak_time_ns=float(times[peak_index] * 1e9),
        bin_width_s=bin_width,
    )


def load_results(artifact_dir: str | Path, run_id: str = "") -> ResultBundle:
    """讀取一次 run 的 artifacts。缺什麼就少呈現什麼，不拋例外。"""
    root = Path(artifact_dir)
    bundle = ResultBundle(run_id=run_id)
    if not root.exists():
        bundle.notes.append("尚無產物目錄")
        return bundle

    manifest_path = root / "simulation_smoke_manifest.json"
    if manifest_path.exists():
        bundle.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        curve = _load_curve(folder)
        if curve is not None:
            bundle.curves.append(curve)

    surrogate_path = root / "surrogate_smoke.csv"
    if surrogate_path.exists():
        text = surrogate_path.read_text(encoding="utf-8")
        if text.strip():
            bundle.surrogate_rows = list(csv.DictReader(text.splitlines()))

    gate_path = root / "e1_gate_audit.json"
    if gate_path.exists():
        bundle.gate_report = json.loads(gate_path.read_text(encoding="utf-8"))

    if not bundle.has_anything:
        bundle.notes.append("這次執行沒有留下可呈現的產物")
    return bundle


# ---------------------------------------------------------------------------
# SVG 繪圖
# ---------------------------------------------------------------------------


def _scale(values: Sequence[float], lo: float, hi: float, out_lo: float, out_hi: float):
    span = hi - lo
    if span <= 0:
        span = 1.0
    return [out_lo + (v - lo) / span * (out_hi - out_lo) for v in values]


def line_chart_svg(
    series: Sequence[tuple[str, Sequence[float], Sequence[float]]],
    width: int = 720,
    height: int = 260,
    x_label: str = "",
    y_label: str = "",
) -> str:
    """多條折線的 SVG。series 為 (名稱, x, y) 三元組。"""
    if not series:
        return '<p class="empty">沒有可繪製的資料</p>'

    pad_l, pad_r, pad_t, pad_b = 56, 130, 14, 34
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    all_x = [v for _, xs, _ in series for v in xs]
    all_y = [v for _, _, ys in series for v in ys]
    x_lo, x_hi = min(all_x), max(all_x)
    y_lo, y_hi = min(min(all_y), 0.0), max(all_y)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" '
        f'role="img" aria-label="{y_label} 對 {x_label}">',
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" '
        f'y2="{pad_t + plot_h}" class="axis"/>',
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" '
        'class="axis"/>',
    ]
    for index, (name, xs, ys) in enumerate(series):
        px = _scale(xs, x_lo, x_hi, pad_l, pad_l + plot_w)
        py = _scale(ys, y_lo, y_hi, pad_t + plot_h, pad_t)
        points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(px, py))
        colour = _PALETTE[index % len(_PALETTE)]
        parts.append(f'<polyline points="{points}" class="series" stroke="{colour}"/>')
        legend_y = pad_t + 14 + index * 17
        parts.append(
            f'<line x1="{pad_l + plot_w + 12}" y1="{legend_y}" '
            f'x2="{pad_l + plot_w + 30}" y2="{legend_y}" stroke="{colour}" '
            'stroke-width="2.5"/>'
        )
        parts.append(
            f'<text x="{pad_l + plot_w + 36}" y="{legend_y + 4}" '
            f'class="legend">{name}</text>'
        )

    parts.append(
        f'<text x="{pad_l + plot_w / 2}" y="{height - 6}" '
        f'class="axis-label" text-anchor="middle">{x_label}</text>'
    )
    parts.append(
        f'<text x="14" y="{pad_t + plot_h / 2}" class="axis-label" '
        f'transform="rotate(-90 14 {pad_t + plot_h / 2})" '
        f'text-anchor="middle">{y_label}</text>'
    )
    parts.append(
        f'<text x="{pad_l - 6}" y="{pad_t + 10}" class="tick" '
        f'text-anchor="end">{y_hi:.3g}</text>'
    )
    parts.append(
        f'<text x="{pad_l - 6}" y="{pad_t + plot_h}" class="tick" '
        f'text-anchor="end">{y_lo:.3g}</text>'
    )
    parts.append(
        f'<text x="{pad_l}" y="{pad_t + plot_h + 16}" class="tick">{x_lo:.3g}</text>'
    )
    parts.append(
        f'<text x="{pad_l + plot_w}" y="{pad_t + plot_h + 16}" class="tick" '
        f'text-anchor="end">{x_hi:.3g}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def bar_chart_svg(
    labels: Sequence[str],
    values: Sequence[float],
    width: int = 720,
    height: int = 200,
    y_label: str = "",
) -> str:
    """橫向比較用的長條圖。"""
    if not labels:
        return '<p class="empty">沒有可繪製的資料</p>'

    pad_l, pad_r, pad_t, pad_b = 56, 16, 14, 46
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    y_hi = max(max(values), 1e-12)
    slot = plot_w / max(len(labels), 1)
    bar_w = slot * 0.6

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" '
        f'role="img" aria-label="{y_label}">',
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" '
        f'y2="{pad_t + plot_h}" class="axis"/>',
    ]
    for index, (label, value) in enumerate(zip(labels, values)):
        bar_h = (value / y_hi) * plot_h
        x = pad_l + index * slot + (slot - bar_w) / 2
        y = pad_t + plot_h - bar_h
        colour = _PALETTE[index % len(_PALETTE)]
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" fill="{colour}" rx="2"/>'
        )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" class="tick" '
            f'text-anchor="middle">{value:.4g}</text>'
        )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{pad_t + plot_h + 16}" '
            f'class="tick" text-anchor="middle">{label}</text>'
        )
    parts.append(
        f'<text x="14" y="{pad_t + plot_h / 2}" class="axis-label" '
        f'transform="rotate(-90 14 {pad_t + plot_h / 2})" '
        f'text-anchor="middle">{y_label}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)
