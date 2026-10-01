# PC-MEF Research System source maintenance contract
# 檔案路徑：docs/assets/draw_method.py
# 版本：v0.4；README 方法圖的可編輯來源。
# 上下游：核對 console/pipeline.py 與 pcmef_orchestrator.py，再匯出 SVG/PNG。
# 維護提醒：不讀研究資料、不畫成果數值；E1/E2 與資料分割在 README 說明。
# 驗證方式：python docs/assets/draw_method.py；檢查字體、邊界與文字重疊。
from pathlib import Path
import re
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


def main():
    # ponytail: 單張固定方法圖直接排版；方法變更時重排，不建立通用圖稿框架。
    plt.rcParams.update({'font.family': ['DejaVu Sans', 'Arial', 'sans-serif'], 'svg.fonttype': 'none', 'svg.hashsalt': 'pcmef-method-v04'})
    fig, ax = plt.subplots(figsize=(6, 7), dpi=150)
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    ax.set(xlim=(0, 6), ylim=(0, 7)); ax.axis('off')
    ink, blue, teal, amber = '#18324B', '#225C96', '#087F83', '#995719'

    def label(x, y, text, size=18, color=ink, weight='normal'):
        return ax.text(x, y, text, fontsize=size, color=color, weight=weight, ha='center', va='center', linespacing=1.3)

    def box(x, y, w, h, text, fill='#F3F6FA', edge='#B3C5D5', color=ink):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0,rounding_size=.10', facecolor=fill, edgecolor=edge, linewidth=1.4))
        label(x+w/2, y+h/2, text, color=color)

    def arrow(a, b, color=blue, dashed=False):
        ax.add_patch(FancyArrowPatch(a, b, arrowstyle='-|>', mutation_scale=14, linewidth=1.7, color=color, linestyle='--' if dashed else '-'))

    label(3, 6.68, 'Paired sensor evidence', 22, weight='bold')
    box(.4, 5.73, 5.2, .64, 'Physics-calibrated simulation')
    arrow((1.55, 5.73), (1.55, 5.32), teal)
    arrow((4.45, 5.73), (4.45, 5.32), teal)
    box(.4, 4.43, 2.3, .87, 'RGB image\nRGB model', '#EAF3FC', blue)
    box(3.3, 4.43, 2.3, .87, 'ToF readings\nToF model', '#E7F4F1', teal)
    arrow((1.55, 4.43), (1.55, 4.12)); arrow((4.45, 4.43), (4.45, 4.12), teal)
    box(.4, 3.46, 2.3, .64, 'Class distribution', 'white')
    box(3.3, 3.46, 2.3, .64, 'Class distribution', 'white')
    arrow((1.55, 3.46), (2.2, 3.07)); arrow((4.45, 3.46), (3.8, 3.07), teal)
    box(.4, 2.42, 5.2, .64, 'Check signal quality + disagreement', '#F0F4F8')
    arrow((1.55, 2.42), (1.55, 1.71))
    arrow((4.45, 2.42), (4.45, 1.71), amber, True)
    box(.4, .85, 2.3, .85, 'Standard\nfusion', '#EAF3FC', blue)
    box(3.3, .85, 2.3, .85, 'Escalated only:\narbitration', '#FFF2DE', amber, amber)
    arrow((1.55, .85), (2.0, .59)); arrow((4.45, .85), (4.0, .59), amber, True)
    box(.4, .08, 5.2, .5, 'Predicted state + decision trace', '#E7F4F1', teal)

    fig.canvas.draw(); renderer=fig.canvas.get_renderer(); bounds=[]
    for text in ax.texts:
        assert text.get_fontsize() >= 18
        assert not re.search(r'[\u3400-\u9fff]', text.get_text())
        extent=text.get_window_extent(renderer)
        assert fig.bbox.contains(extent.x0, extent.y0) and fig.bbox.contains(extent.x1, extent.y1), text.get_text()
        for previous, other in bounds: assert not extent.overlaps(other), (previous, text.get_text())
        bounds.append((text.get_text(), extent))
    out=Path(__file__).resolve().parent
    fig.savefig(out/'pcmef-method.svg', facecolor='white', metadata={'Date': None})
    fig.savefig(out/'pcmef-method.png', dpi=300, facecolor='white')
    svg=out/'pcmef-method.svg'
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text(encoding='utf-8').splitlines())+'\n',encoding='utf-8')
    plt.close(fig)
    print('PASS: source-linked method; English labels >=18pt; no clipping or label overlap; SVG/PNG exported.')


if __name__ == '__main__':
    main()
