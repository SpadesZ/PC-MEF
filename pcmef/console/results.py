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
#   - 不得為了「讓差異看得出來」而截斷 y 軸起點。截軸能讓 0.3% 的差看起來
#     像三倍，那在論文裡是造假。四類能量差不到 1%、柱子必然等高是事實，
#     正確處置是把差異以數字（相對平均的 ±%）補上，不是改比例尺。
#   - v0.2.0 修訂：兩軸錨 0、加格線與刻度、標出 transient 峰值、
#     長條圖加平均參考線與相對偏差。理由見各函式 docstring。
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


def _ticks(lo: float, hi: float, count: int) -> list[float]:
    """count+1 個等距刻度值，含頭尾。"""
    if hi <= lo:
        hi = lo + 1.0
    step = (hi - lo) / count
    return [lo + step * i for i in range(count + 1)]


def line_chart_svg(
    series: Sequence[tuple[str, Sequence[float], Sequence[float]]],
    width: int = 720,
    height: int = 300,
    x_label: str = "",
    y_label: str = "",
) -> str:
    """多條折線的 SVG。series 為 (名稱, x, y) 三元組。

    兩軸都錨在 0，並標出每條曲線的峰值。這兩件事對 transient 波形不是
    美化而是可讀性的前提：波形的意義是「訊號從零長起來、多久之後到達峰值」，
    x 軸截在第一個 bin 會讓 time-of-flight 讀不出來；而「峰值有沒有貼在
    時間窗邊緣」是這張圖唯一的科學檢查（NOTE-013），沒有標記就只能肉眼估。
    """
    if not series:
        return '<p class="empty">沒有可繪製的資料</p>'

    pad_l, pad_r, pad_t, pad_b = 66, 140, 18, 46
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    base_y = pad_t + plot_h

    all_x = [v for _, xs, _ in series for v in xs]
    all_y = [v for _, _, ys in series for v in ys]
    x_lo, x_hi = min(min(all_x), 0.0), max(all_x)
    y_lo, y_hi = min(min(all_y), 0.0), max(all_y)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" '
        f'role="img" aria-label="{y_label} 對 {x_label}">'
    ]

    # 格線先畫，才會在曲線底下。
    for value in _ticks(y_lo, y_hi, 4):
        y = _scale([value], y_lo, y_hi, base_y, pad_t)[0]
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" '
            f'y2="{y:.1f}" class="grid-line"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick" '
            f'text-anchor="end">{value:.3g}</text>'
        )
    for value in _ticks(x_lo, x_hi, 5):
        x = _scale([value], x_lo, x_hi, pad_l, pad_l + plot_w)[0]
        parts.append(
            f'<text x="{x:.1f}" y="{base_y + 18}" class="tick" '
            f'text-anchor="middle">{value:.3g}</text>'
        )

    parts.append(
        f'<line x1="{pad_l}" y1="{base_y}" x2="{pad_l + plot_w}" '
        f'y2="{base_y}" class="axis"/>'
    )
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{base_y}" class="axis"/>'
    )

    for index, (name, xs, ys) in enumerate(series):
        px = _scale(xs, x_lo, x_hi, pad_l, pad_l + plot_w)
        py = _scale(ys, y_lo, y_hi, base_y, pad_t)
        points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(px, py))
        colour = _PALETTE[index % len(_PALETTE)]
        parts.append(f'<polyline points="{points}" class="series" stroke="{colour}"/>')

        if ys:
            peak = max(range(len(ys)), key=lambda i: ys[i])
            parts.append(
                f'<line x1="{px[peak]:.1f}" y1="{py[peak]:.1f}" '
                f'x2="{px[peak]:.1f}" y2="{base_y}" class="peak-line" '
                f'stroke="{colour}"/>'
            )
            parts.append(
                f'<circle cx="{px[peak]:.1f}" cy="{py[peak]:.1f}" r="3.5" '
                f'fill="{colour}"/>'
            )
            # 峰值標籤壓在曲線頂點上方 8px，但最高的那條曲線頂點就在 pad_t，
            # 不夾住的話標籤會被 viewBox 上緣切掉一半。
            label_y = max(py[peak] - 8.0, 11.0)
            parts.append(
                f'<text x="{px[peak]:.1f}" y="{label_y:.1f}" class="tick" '
                f'text-anchor="middle" fill="{colour}">峰值 {xs[peak]:.3g}</text>'
            )

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
        f'<text x="{pad_l + plot_w / 2}" y="{height - 8}" '
        f'class="axis-label" text-anchor="middle">{x_label}（兩軸皆自 0 起）</text>'
    )
    parts.append(
        f'<text x="16" y="{pad_t + plot_h / 2}" class="axis-label" '
        f'transform="rotate(-90 16 {pad_t + plot_h / 2})" '
        f'text-anchor="middle">{y_label}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


#: 單根長條的最大寬度。沒有上限的話，只有一個場景時那根柱子會佔掉
#: 六成畫布，看起來像出了什麼事，而它其實只是「這次只跑了一類」。
_MAX_BAR_WIDTH = 96.0


def bar_chart_svg(
    labels: Sequence[str],
    values: Sequence[float],
    width: int = 720,
    height: int = 240,
    y_label: str = "",
) -> str:
    """橫向比較用的長條圖。y 軸自 0 起，並標出各項相對平均的偏差。

    **不截斷 y 軸**：截軸能讓 0.3% 的差看起來像三倍，那在論文裡是造假。
    但四類的總能量本來就只差不到 1%，零起點的柱子必然看起來等高 ——
    所以差異改用「相對平均 ±x.xx%」以數字呈現，並畫一條平均參考線。
    圖負責證明「量級相同」，數字負責回答「差多少」。
    """
    if not labels:
        return '<p class="empty">沒有可繪製的資料</p>'

    # 右側留白給平均參考線的標籤。放在繪圖區內的話它會與柱頂的數值標籤
    # 撞在同一個高度 —— 柱子本來就都貼著平均，那兩組文字必然重疊。
    pad_l, pad_r, pad_t, pad_b = 66, 104, 22, 52
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    base_y = pad_t + plot_h

    y_hi = max(max(values), 1e-12)
    mean = sum(values) / len(values)
    slot = plot_w / max(len(labels), 1)
    bar_w = min(slot * 0.6, _MAX_BAR_WIDTH)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" '
        f'role="img" aria-label="{y_label}">'
    ]

    for value in _ticks(0.0, y_hi, 4):
        y = base_y - (value / y_hi) * plot_h
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" '
            f'y2="{y:.1f}" class="grid-line"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick" '
            f'text-anchor="end">{value:.3g}</text>'
        )

    for index, (label, value) in enumerate(zip(labels, values)):
        bar_h = (value / y_hi) * plot_h
        x = pad_l + index * slot + (slot - bar_w) / 2
        y = base_y - bar_h
        colour = _PALETTE[index % len(_PALETTE)]
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" fill="{colour}" rx="2"/>'
        )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" class="tick" '
            f'text-anchor="middle">{value:.5g}</text>'
        )
        if len(values) > 1 and mean:
            delta = (value - mean) / mean * 100.0
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{base_y + 32}" '
                f'class="tick" text-anchor="middle">{delta:+.2f}%</text>'
            )
        parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{base_y + 16}" '
            f'class="tick" text-anchor="middle">{label}</text>'
        )

    if len(values) > 1:
        mean_y = base_y - (mean / y_hi) * plot_h
        parts.append(
            f'<line x1="{pad_l}" y1="{mean_y:.1f}" x2="{pad_l + plot_w + 8}" '
            f'y2="{mean_y:.1f}" class="mean-line"/>'
        )
        parts.append(
            f'<text x="{pad_l + plot_w + 14}" y="{mean_y + 4:.1f}" class="tick">'
            f'平均 {mean:.5g}</text>'
        )

    parts.append(
        f'<line x1="{pad_l}" y1="{base_y}" x2="{pad_l + plot_w}" '
        f'y2="{base_y}" class="axis"/>'
    )
    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{base_y}" class="axis"/>'
    )
    parts.append(
        f'<text x="16" y="{pad_t + plot_h / 2}" class="axis-label" '
        f'transform="rotate(-90 16 {pad_t + plot_h / 2})" '
        f'text-anchor="middle">{y_label}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)
