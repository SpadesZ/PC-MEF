# PC-MEF Research System source maintenance contract
# 上下游: README 使用本檔產生的 SVG；PNG 是預覽與替代版本。
# 檔案路徑: docs/assets/draw_method.py
# 產生時間: 2026-10-01 +08:00
# 版本: v0.2.0
# 功能說明: 畫出方法步驟、選擇性仲裁與 E1 資料角色；不讀研究資料。
# 模組定位: 文件圖稿來源，不參與訓練、校準或正式研究執行。
# 主要責任:
#   1. main() 依方法排版步驟與路徑，匯出 SVG/PNG。
#   2. 檢查文字語言、畫布邊界與重疊。
# 維護提醒: 修改方法前核對 pipeline.py；不得加入未取得的成果或讀取 final families。
# 驗證方式:
#   - python docs/assets/draw_method.py
# ------------------------------------------------------------

from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle


def main():
    # ponytail: 固定方法圖，以直接座標排版；方法改變時重排，不建立通用圖表框架。
    plt.rcParams.update({'font.family': ['DejaVu Sans', 'Arial', 'sans-serif'], 'svg.fonttype': 'none', 'svg.hashsalt': 'pcmef-method-v02'})
    fig, ax = plt.subplots(figsize=(15, 8.4), dpi=150)
    fig.patch.set_facecolor('white')
    ax.set(xlim=(0, 15), ylim=(0, 8.4))
    ax.axis('off')
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    ink, muted = '#19334D', '#526475'
    blue, teal, violet, amber = '#27649B', '#117D85', '#6B56A0', '#98601D'
    pale_blue, pale_teal, pale_violet, pale_amber = '#EDF4FC', '#ECF8F5', '#F2EFF9', '#FFF4E4'

    def label(x, y, text, size=15, color=ink, weight='normal', ha='left', va='center'):
        return ax.text(x, y, text, fontsize=size, color=color, weight=weight, ha=ha, va=va, linespacing=1.55)

    def rounded(x, y, w, h, fill, edge, radius=.14, lw=1.6):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f'round,pad=0,rounding_size={radius}', facecolor=fill, edgecolor=edge, linewidth=lw))

    def arrow(start, end, color=blue, dashed=False):
        ax.add_patch(FancyArrowPatch(start, end, arrowstyle='-|>', mutation_scale=15, linewidth=2.1, color=color, linestyle=(0, (4, 3)) if dashed else 'solid'))

    label(.55, 7.78, 'PC-MEF', 27, ink, 'bold')
    label(.55, 7.22, 'Camera + depth evidence for liquid-state recognition', 19, muted)
    rounded(11.48, 7.52, 2.95, .52, '#F3F6F9', '#D6E0E8', .26, 1)
    label(12.955, 7.78, 'METHOD OVERVIEW', 12, muted, 'bold', ha='center')
    ax.plot([.55, 14.45], [6.78, 6.78], color='#DAE3EB', lw=1.2)

    stages = [(.55, '01', 'Simulation & inputs', teal), (4.00, '02', 'Perception', blue), (7.55, '03', 'Check & route', blue), (11.25, '04', 'Decision & record', violet)]
    for x, number, title, color in stages:
        label(x, 6.25, number, 14, color, 'bold')
        label(x+.42, 6.25, title, 17, ink, 'bold')

    # 輸入圖示為概念示意，不是資料集圖片或實測曲線。
    rounded(.65, 4.14, 2.50, 1.59, pale_teal, '#B1D7D3')
    ax.add_patch(Rectangle((.88, 4.90), .87, .56, facecolor='white', edgecolor=teal, linewidth=1.5))
    ax.add_patch(Polygon([[.96, 4.96], [1.23, 5.22], [1.43, 5.03], [1.66, 5.24], [1.66, 4.96]], closed=True, facecolor='#AED8D3', edgecolor='none'))
    ax.add_patch(Circle((1.55, 5.32), .055, facecolor=teal, edgecolor='none'))
    rounded(2.02, 4.90, .87, .56, 'white', teal, .05, 1.5)
    for radius in (.10, .18, .25):
        ax.add_patch(Circle((2.455, 5.18), radius, fill=False, edgecolor=teal, linewidth=1))
    label(1.315, 4.55, 'RGB', 15, teal, 'bold', ha='center')
    label(2.455, 4.55, 'ToF', 15, teal, 'bold', ha='center')
    label(1.90, 3.79, 'Scene, light, media\nand calibration settings', 14, muted, ha='center')
    arrow((3.15, 4.95), (3.88, 4.95), teal)

    rounded(3.98, 4.93, 2.68, .79, pale_blue, '#A7C2DF')
    rounded(3.98, 3.91, 2.68, .79, pale_teal, '#B1D7D3')
    label(5.32, 5.325, 'RGB model', 16, blue, 'bold', ha='center')
    label(5.32, 4.305, 'ToF model', 16, teal, 'bold', ha='center')
    ax.plot([6.66, 7.03, 7.03], [5.325, 5.325, 4.95], color=blue, lw=1.8)
    ax.plot([6.66, 7.03, 7.03], [4.305, 4.305, 4.95], color=teal, lw=1.8)
    arrow((7.03, 4.95), (7.44, 4.95))
    label(5.32, 3.46, 'Two class distributions', 14, muted, ha='center')

    ax.add_patch(Polygon([[7.47, 4.95], [8.64, 5.73], [9.81, 4.95], [8.64, 4.17]], closed=True, facecolor=pale_blue, edgecolor=blue, linewidth=1.8))
    label(8.64, 4.95, 'Route', 17, blue, 'bold', ha='center')
    label(9.09, 3.78, 'Signal quality\nDisagreement', 14, muted)
    arrow((9.81, 4.95), (11.04, 4.95))
    label(10.43, 5.26, 'Standard path', 13, blue, ha='center')

    # 紙張形狀表示輸出類型，不畫虛構機率或成果數值。
    rounded(11.46, 4.00, 2.90, 1.75, 'white', '#D3CCE6', .08, 1.2)
    rounded(11.26, 3.85, 2.90, 1.75, pale_violet, violet, .08, 1.7)
    ax.plot([11.47, 13.91], [5.24, 5.24], color='#D3CCE6', lw=1.2)
    label(11.49, 4.71, 'Class probabilities\nPredicted state\nDecision trace', 15, ink)

    # 例外路徑用虛線與暖色；一般路徑與選擇性仲裁均保留。
    arrow((8.64, 4.17), (8.64, 2.98), amber, True)
    label(9.18, 3.03, 'Escalated cases only', 13, amber)
    rounded(6.69, 1.64, 3.90, 1.22, pale_amber, amber, .18, 1.7)
    label(8.64, 2.43, 'Evidence arbitration', 16, amber, 'bold', ha='center')
    label(8.64, 1.99, 'Configured LLM roles', 14, muted, ha='center')
    ax.plot([10.59, 12.71, 12.71], [2.25, 2.25, 3.18], color=amber, lw=2.1, linestyle=(0, (4, 3)))
    arrow((12.71, 3.18), (12.71, 3.85), amber, True)
    label(11.91, 1.89, 'Arbitrated evidence', 13, amber, ha='center')

    # E1 是獨立比較用途；校準與 held-out 資料角色不合併。
    ax.plot([1.90, 1.90], [3.30, 2.87], color='#8C9CAC', lw=1.6, linestyle=(0, (3, 3)))
    rounded(.55, 1.64, 5.70, 1.22, '#F4F7FA', '#C6D2DD', .12, 1.2)
    label(.77, 2.43, 'E1 / Simulation fidelity', 16, ink, 'bold')
    label(.77, 1.99, 'Initial vs calibrated; compare with real data', 14, muted)
    label(.77, 1.16, 'Separate calibration and held-out real recordings', 14, muted)

    ax.plot([.55, 14.45], [.88, .88], color='#DAE3EB', lw=1)
    label(.55, .62, 'E2: compare fusion methods on paired synthetic stress cases.', 13, muted)
    label(.55, .27, 'Calibration is partial. Final E2 evaluation is incomplete. Icons are schematic; no measured values are shown.', 12, muted)

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bounds = []
    for text in ax.texts:
        assert text.get_text().isascii(), text.get_text()
        extent = text.get_window_extent(renderer)
        assert fig.bbox.contains(extent.x0, extent.y0) and fig.bbox.contains(extent.x1, extent.y1), text.get_text()
        for previous, other in bounds:
            assert not extent.overlaps(other), (previous, text.get_text())
        bounds.append((text.get_text(), extent))
    out = Path(__file__).resolve().parent
    fig.savefig(out/'pcmef-method.svg', facecolor='white', metadata={'Date': None})
    svg_path = out/'pcmef-method.svg'
    svg_path.write_text('\n'.join(line.rstrip() for line in svg_path.read_text(encoding='utf-8').splitlines()) + '\n', encoding='utf-8')
    fig.savefig(out/'pcmef-method.png', dpi=300, facecolor='white')
    plt.close(fig)
    print('PASS: English-only method figure; no label overlap or clipping; SVG and PNG exported.')


if __name__ == '__main__':
    main()
