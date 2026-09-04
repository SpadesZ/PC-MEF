# PC-MEF Research System source maintenance contract
# 上下游: 由 pcmef/reporting/figures.py 呼叫。不被決策路徑 import。
# 檔案路徑: pcmef/reporting/style.py
# 產生時間: 2026-09-04 16:25 +08:00
# 版本: v0.1.0
# 功能說明: 論文圖的 rcParams 與調色盤，參考 figures4papers 的慣例。
# 模組定位: 只管外觀。任何與「數字是什麼」有關的決定都不屬於這裡。
# 主要責任:
#   1. RC_PARAMS 一份可重現的樣式
#   2. ARM_COLOURS / CONDITION_COLOURS 兩組色票
#   3. finalise() 統一收邊：去框線、tight_layout
# 維護提醒:
#   - 不得開 `text.usetex`。它需要機器上有 LaTeX，缺了會在產圖時才炸，
#     而那通常是投稿前一晚。數學符號一律走 mathtext。
#   - 不得把字型寫死成 Helvetica/Arial。那兩個在多數 Linux 與 CI 上不存在，
#     matplotlib 會靜默 fallback 並印一堆 warning；DejaVu Sans 隨 matplotlib
#     一起裝，放在 fallback stack 最後保證有字可用。
#   - 不得為了「讓差異看得出來」而截斷比例尺的起點。這一條與 figures4papers
#     的 "manual Y-limits tightened to emphasize comparative differences"
#     相反，而本專案採自己的規則：截軸能讓 0.3% 的差看起來像三倍，
#     那在論文裡是造假（見 console/results.py 同一條禁令、NOTE-069）。
#   - v0.1.0 新增：首版，對應 P2-6。
# 驗證方式:
#   - py -3.10 -m pytest tests/reporting/test_figures.py -k style -v
# ------------------------------------------------------------

from __future__ import annotations

from typing import Any

__all__ = [
    "RC_PARAMS",
    "ARM_COLOURS",
    "ARM_ORDER",
    "ARM_LABELS",
    "CONDITION_COLOURS",
    "CONDITION_ORDER",
    "apply",
    "finalise",
]

#: 論文圖的樣式。
#:
#: `svg.fonttype = "none"` 讓 SVG 的文字保持可編輯 —— 投稿前要改一個標籤時
#: 不必重跑整條 pipeline。`pdf.fonttype = 42` 內嵌 TrueType，避免部分期刊
#: 系統無法處理 Type 3。
RC_PARAMS: dict[str, Any] = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans", "sans-serif"],
    "font.size": 15,
    "axes.titlesize": 16,
    "axes.labelsize": 15,
    "xtick.labelsize": 13,
    "ytick.labelsize": 13,
    "legend.fontsize": 13,
    "axes.linewidth": 1.6,
    "axes.spines.right": False,
    "axes.spines.top": False,
    "axes.grid": False,
    "legend.frameon": False,
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "svg.fonttype": "none",
    # SVG 的 clip-path / marker id 預設由物件位址雜湊而來，同一份 report
    # 產兩次會得到不同的 id，檔案因此不是逐 byte 相同。固定 salt 之後
    # 才能用 hash 確認圖與報告對得上。
    "svg.hashsalt": "pcmef",
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    # mathtext 取代 usetex：不需要機器上有 LaTeX。
    "text.usetex": False,
    "mathtext.fontset": "dejavusans",
}

#: 五條臂的固定順序（G1→G5）。順序即論文裡的敘事順序，不得依數值重排 ——
#: 依高低排序會讓不同 run 的圖無法並排比較。
ARM_ORDER: tuple[str, ...] = (
    "vision_only", "tof_only", "fixed_fusion", "reliability_routing", "pcmef_full",
)

ARM_LABELS: dict[str, str] = {
    "vision_only": "G1\nVision only",
    "tof_only": "G2\nToF only",
    "fixed_fusion": "G3\nFixed fusion",
    "reliability_routing": "G4\nReliability routing",
    "pcmef_full": "G5\nPC-MEF full",
}

#: 四個 baseline 用中性灰，PC-MEF 用主色。
#:
#: 把自己的方法塗成最鮮豔、baseline 全部灰掉是常見的視覺取巧。這裡只讓 G5
#: 與 G4 帶顏色，因為那兩條正是論文要對比的（G4 隔離路由、G5 加上仲裁）；
#: G1–G3 是參照物，灰階足夠。
ARM_COLOURS: dict[str, str] = {
    "vision_only": "#767676",
    "tof_only": "#9E9E9E",
    "fixed_fusion": "#CFCECE",
    "reliability_routing": "#42949E",
    "pcmef_full": "#0F4D92",
}

#: 四種 condition。與 console 的 --chart-N 同一組色相，讓網頁與論文一致。
CONDITION_ORDER: tuple[str, ...] = (
    "clean", "vision_degraded", "tof_degraded", "conflict",
)

CONDITION_COLOURS: dict[str, str] = {
    "clean": "#0f4c81",
    "vision_degraded": "#0d7a8a",
    "tof_degraded": "#b5651d",
    "conflict": "#6b3fa0",
}


def apply() -> None:
    """把 RC_PARAMS 套到全域。

    在此設定 Agg backend —— 產圖經常在沒有顯示器的環境跑，預設 backend
    會嘗試開視窗然後失敗。
    """
    import logging

    import matplotlib

    matplotlib.use("Agg", force=True)
    matplotlib.rcParams.update(RC_PARAMS)
    # 寫 PDF 時 fontTools 會逐項 INFO 記錄字型子集化（"glyf pruned" 之類），
    # 五張圖就是數十行。壓到 WARNING 只是降噪，真正的字型問題仍會出現。
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)


def finalise(fig, ax=None, *, pad: float = 1.0) -> None:
    """統一收邊。"""
    for axis in ([ax] if ax is not None else fig.axes):
        if axis is None:
            continue
        axis.spines["right"].set_visible(False)
        axis.spines["top"].set_visible(False)
        axis.tick_params(width=1.4, length=5)
    fig.tight_layout(pad=pad)
