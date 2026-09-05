# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef.cli 的 `figures export` 呼叫；讀 formal E2 report JSON，
#         寫 PDF / SVG / PNG。**只讀 report，不讀資料集、不重算。**
# 檔案路徑: pcmef/reporting/figures.py
# 產生時間: 2026-09-04 16:35 +08:00
# 版本: v0.1.0
# 功能說明: 由一份 formal report 產出五張論文用圖（× pdf/svg/png = 15 個檔）。
# 模組定位: FR-020 的 thesis-ready figure。它在 console 之外，因為
#           matplotlib 是重相依而 console 必須維持零繪圖相依。
# 主要責任:
#   1. worst_condition_macro_f1：主要 endpoint 的長條圖
#   2. per_condition_macro_f1：四 condition × 五臂的分組長條圖
#   3. paired_delta_forest：成對比較的 Δ 與 95% CI
#   4. routing_distribution：路由分布與 escalation rate
#   5. export_all() 逐張輸出三種格式
# 維護提醒:
#   - 不得在本檔重算任何指標。所有數字逐字取自 report；report 沒有的欄位
#     就不畫那張圖，並說明缺什麼。畫一個「順手算出來」的數字，會讓圖與
#     凍結結果分岔，而分岔時沒有人會發現。
#   - 不得截斷比例尺起點。比例類指標一律 0–1（見 style.py 的同一條）。
#     forest plot 是差值圖，零線是參照而不是截軸。
#   - 不得依數值高低重排臂。ARM_ORDER 是論文的敘事順序，依高低排會讓
#     不同 run 的圖無法並排。
#   - 不得在輸出裡寫入時間戳。同一份 report 產兩次必須逐 byte 相同，
#     否則無法用 hash 確認圖與報告對得上（NOTE-069）。
#   - v0.1.0 新增：首版，對應 P2-6。
# 驗證方式:
#   - py -3.10 -m pytest tests/reporting/test_figures.py -v
# ------------------------------------------------------------

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pcmef.reporting import style

__all__ = [
    "FIGURES",
    "FORMATS",
    "load_report",
    "export_all",
    "worst_condition_macro_f1",
    "per_condition_macro_f1",
    "paired_delta_forest",
    "routing_distribution",
]

#: 輸出格式。PDF 是投稿主格式，SVG 供手改標籤，PNG 供投影片與草稿。
FORMATS: tuple[str, ...] = ("pdf", "svg", "png")


class MissingReportFields(Exception):
    """report 缺了這張圖需要的欄位。訊息要指名缺哪一個。"""


def load_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _require(report: dict[str, Any], *fields: str) -> None:
    missing = [f for f in fields if report.get(f) in (None, {}, [])]
    if missing:
        raise MissingReportFields(
            f"report 缺少 {'、'.join(missing)} —— "
            "這通常代表它是舊版或 dry-run 產生的報告。"
        )


def _arms(report: dict[str, Any], block: str) -> list[str]:
    """report 裡實際存在的臂，依 ARM_ORDER 排。

    dry-run 沒有 pcmef_full，因此不能假設五條都在；但順序必須固定。
    """
    present = report.get(block) or {}
    return [arm for arm in style.ARM_ORDER if arm in present]


def _label(arm: str) -> str:
    return style.ARM_LABELS.get(arm, arm)


# ---------------------------------------------------------------------------
# 圖
# ---------------------------------------------------------------------------


def worst_condition_macro_f1(report: dict[str, Any]):
    """主要 endpoint：每條臂在最差 condition 下的 Macro-F1。

    每根柱子上標出它最差的是哪一個 condition —— 只給數值的話，讀者看不出
    「fixed fusion 最弱在 tof_degraded、其餘四條最弱在 conflict」這件事，
    而那正是這個 endpoint 想說的。
    """
    import matplotlib.pyplot as plt

    _require(report, "worst_condition_macro_f1", "worst_condition_at")
    worst = report["worst_condition_macro_f1"]
    where = report["worst_condition_at"]
    arms = [a for a in style.ARM_ORDER if a in worst]

    fig, ax = plt.subplots(figsize=(9, 5.2))
    values = [worst[a] for a in arms]
    bars = ax.bar(
        range(len(arms)), values,
        color=[style.ARM_COLOURS.get(a, "#767676") for a in arms],
        edgecolor="black", linewidth=1.5, width=0.62,
    )
    for index, (bar, arm) in enumerate(zip(bars, arms)):
        ax.text(
            index, bar.get_height() + 0.022,
            f"{worst[arm]:.3f}\n({where.get(arm, '—')})",
            ha="center", va="bottom", fontsize=12, linespacing=1.35,
        )

    ax.set_xticks(range(len(arms)))
    ax.set_xticklabels([_label(a) for a in arms])
    ax.set_ylabel("Worst-condition macro-F1")
    # 0–1 完整比例尺。截軸能讓 0.02 的差看起來像兩倍。
    ax.set_ylim(0.0, 1.0)
    ax.set_yticks([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    style.finalise(fig, ax)
    return fig


def per_condition_macro_f1(report: dict[str, Any]):
    """四種 condition 下各臂的 Macro-F1。

    上一張圖只給最小值；這張給出那個最小值是從哪個分布裡取出來的。
    """
    import numpy as np
    import matplotlib.pyplot as plt

    _require(report, "per_condition")
    per = report["per_condition"]
    arms = _arms(report, "per_condition")
    conditions = [
        c for c in style.CONDITION_ORDER
        if any(c in (per.get(a) or {}) for a in arms)
    ]

    fig, ax = plt.subplots(figsize=(11, 5.2))
    width = 0.8 / max(len(conditions), 1)
    base = np.arange(len(arms))
    for index, condition in enumerate(conditions):
        offsets = base - 0.4 + width * (index + 0.5)
        values = [
            ((per.get(arm) or {}).get(condition) or {}).get("macro_f1", 0.0)
            for arm in arms
        ]
        ax.bar(
            offsets, values, width=width * 0.92,
            label=condition, color=style.CONDITION_COLOURS.get(condition, "#767676"),
            edgecolor="black", linewidth=1.0,
        )

    ax.set_xticks(base)
    ax.set_xticklabels([_label(a) for a in arms])
    ax.set_ylabel("Macro-F1")
    ax.set_ylim(0.0, 1.0)
    ax.set_yticks([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ax.legend(ncol=len(conditions), loc="upper center",
              bbox_to_anchor=(0.5, 1.14), columnspacing=1.4)
    style.finalise(fig, ax)
    return fig


def paired_delta_forest(report: dict[str, Any], *, block: str = "worst"):
    """成對比較的 Δ 與 95% CI。

    `block="worst"` 用 worst-condition estimator（主要 endpoint），
    `block="overall"` 用整體 macro-F1。兩者可能給出不同結論，那本身是
    結果的一部分，因此兩張都能產。

    零線是參照而不是截軸：這是差值圖，橫跨零就是不顯著。
    """
    import matplotlib.pyplot as plt

    if block == "worst":
        _require(report, "worst_condition_statistics")
        comparisons = report["worst_condition_statistics"]["comparisons"]
        entries = {k: v for k, v in comparisons.items()}
        xlabel = r"$\Delta$ worst-condition macro-F1 (PC-MEF $-$ baseline)"
    else:
        _require(report, "statistics")
        entries = {
            k: v["macro_f1"] for k, v in report["statistics"].items()
            if isinstance(v, dict) and "macro_f1" in v
        }
        xlabel = r"$\Delta$ overall macro-F1 (PC-MEF $-$ baseline)"

    # 依 ARM_ORDER 排 —— 比較名稱形如 pcmef_full_vs_<arm>。
    def sort_key(name: str) -> int:
        arm = name.split("_vs_")[-1]
        return style.ARM_ORDER.index(arm) if arm in style.ARM_ORDER else 99

    names = sorted(entries, key=sort_key)
    if not names:
        raise MissingReportFields("report 沒有任何成對比較")

    fig, ax = plt.subplots(figsize=(9.5, 0.85 * len(names) + 2.2))
    lower = min(entries[n]["ci_lower"] for n in names)
    upper = max(entries[n]["ci_upper"] for n in names)
    span = max(upper - lower, 1e-6)
    # 註記排成固定的一欄，而不是各自跟在自己的 CI 右邊。跟著 CI 走的話，
    # 區間窄的那幾條會把文字推到零線上，讀者分不出哪個數字屬於哪一條。
    text_x = max(upper, 0.0) + 0.10 * span

    positions = range(len(names))
    for index, name in zip(positions, names):
        entry = entries[name]
        delta = entry["delta"]
        low, high = entry["ci_lower"], entry["ci_upper"]
        significant = entry.get("significant_at_95", False)
        colour = "#0F4D92" if significant else "#767676"
        ax.plot([low, high], [index, index], color=colour, linewidth=2.6,
                solid_capstyle="butt", zorder=2)
        for edge in (low, high):
            ax.plot([edge, edge], [index - 0.14, index + 0.14], color=colour,
                    linewidth=2.0, zorder=2)
        ax.scatter([delta], [index], s=95, color=colour, zorder=3,
                   edgecolor="black", linewidth=1.2)
        ax.text(
            text_x, index,
            f"{delta:+.4f}  [{low:+.4f}, {high:+.4f}]"
            + ("" if significant else "  n.s."),
            va="center", ha="left", fontsize=11.5, color=colour,
            family="monospace",
        )

    ax.axvline(0.0, color="black", linewidth=1.3, linestyle="--", zorder=1)
    ax.set_yticks(list(positions))
    ax.set_yticklabels([
        _label(n.split("_vs_")[-1]).replace("\n", " ") for n in names
    ])
    ax.invert_yaxis()
    ax.set_xlabel(xlabel)
    # 右側留白給註記那一欄，否則 tight_layout 會把它裁掉。
    ax.set_xlim(lower - 0.08 * span, text_x + 0.92 * span)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    style.finalise(fig, ax)
    return fig


def routing_distribution(report: dict[str, Any]):
    """路由分布。escalation rate 是結果，不是設定值。"""
    import matplotlib.pyplot as plt

    _require(report, "routing")
    routing = report["routing"]
    counts = routing.get("counts") or {}
    if not counts:
        raise MissingReportFields("routing.counts 不存在")

    order = ["fusion", "trust_vision", "trust_tof", "escalated"]
    names = [n for n in order if n in counts] + \
            [n for n in sorted(counts) if n not in order]
    values = [counts[n] for n in names]
    total = sum(values) or 1

    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    colours = ["#CFCECE", "#9E9E9E", "#767676", "#0F4D92"]
    bars = ax.bar(
        range(len(names)), values,
        color=[colours[i % len(colours)] for i in range(len(names))],
        edgecolor="black", linewidth=1.5, width=0.6,
    )
    for index, (bar, value) in enumerate(zip(bars, values)):
        ax.text(index, bar.get_height() + total * 0.012,
                f"{value}\n({value / total:.1%})",
                ha="center", va="bottom", fontsize=12, linespacing=1.35)

    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names)
    ax.set_ylabel("Cases")
    ax.set_ylim(0, max(values) * 1.24)
    style.finalise(fig, ax)
    return fig


#: 圖名 → builder。名稱同時是輸出檔名。
FIGURES = {
    "worst_condition_macro_f1": worst_condition_macro_f1,
    "per_condition_macro_f1": per_condition_macro_f1,
    "paired_delta_worst_condition": paired_delta_forest,
    "paired_delta_overall": lambda r: paired_delta_forest(r, block="overall"),
    "routing_distribution": routing_distribution,
}


def export_all(
    report: dict[str, Any],
    out_dir: str | Path,
    *,
    formats: tuple[str, ...] = FORMATS,
) -> dict[str, Any]:
    """產出全部圖。單張失敗不中斷其餘 —— 回報哪一張缺什麼。

    一份舊的或 dry-run 的 report 少幾個欄位是常態；因此少一張圖就整批
    不產出，只會逼人回頭手動畫。
    """
    style.apply()
    import matplotlib.pyplot as plt

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    skipped: dict[str, str] = {}

    for name, builder in FIGURES.items():
        try:
            figure = builder(report)
        except MissingReportFields as error:
            skipped[name] = str(error)
            continue
        try:
            for suffix in formats:
                target = directory / f"{name}.{suffix}"
                # metadata 清空：matplotlib 預設會把 CreationDate 寫進 PDF/SVG，
                # 同一份 report 產兩次就會得到不同的檔案。
                figure.savefig(target, format=suffix, metadata=_metadata(suffix))
                written.append(target.name)
        finally:
            plt.close(figure)

    return {"written": written, "skipped": skipped, "out_dir": str(directory)}


def _metadata(suffix: str) -> dict[str, Any]:
    """去掉時間戳，讓輸出可逐 byte 重現。

    PDF 與 SVG 各自吃不同的 metadata 鍵；PNG 走 pnginfo 而這裡不需要。
    """
    if suffix == "pdf":
        return {"CreationDate": None}
    if suffix == "svg":
        return {"Date": None}
    return {}
