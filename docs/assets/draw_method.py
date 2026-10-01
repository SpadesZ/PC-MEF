# PC-MEF Research System source maintenance contract
# 上下游: 讀者從 README 的方法圖進入；只以本檔文字與座標產圖，輸出同目錄 SVG/PNG。
# 檔案路徑: docs/assets/draw_method.py
# 產生時間: 2026-10-01 +08:00
# 版本: v0.1.0
# 功能說明: 畫出研究方法、選擇性仲裁與 E1 比較的位置，不讀任何研究資料。
# 模組定位: 文件用圖生成來源，不參與模擬、推論、校準或正式實驗。
# 主要責任:
#   1. main() 畫方法節點與分支，檢查標籤沒有超出畫布，匯出 SVG/PNG。
# 維護提醒: 不得加入未完成的研究結果或讀取保留的 final families；更新箭頭前核對 pipeline.py。
# 驗證方式:
#   - python docs/assets/draw_method.py
# ------------------------------------------------------------

from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


def main():
    # ponytail: 固定方法圖直接用座標；方法改變時重畫，不建立通用圖表框架。
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'svg.fonttype': 'none'})
    fig, ax = plt.subplots(figsize=(12, 5.2), dpi=150)
    fig.patch.set_facecolor('white')
    ax.set(xlim=(0, 12), ylim=(0, 5.2))
    ax.axis('off')
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    blue, gray, amber = '#244E70', '#4D4D4D', '#8A5A20'
    ax.text(.35, 4.88, 'PC-MEF  |  From paired sensor evidence to a traceable decision', fontsize=17, weight='bold', color=blue)
    ax.text(.35, 4.48, 'Method overview. Calibration is partial; final E2 evaluation is not complete.', fontsize=12, color=gray)

    boxes = [
        (.35, 2.82, 3.15, 1.18, 'Simulation & paired inputs', 'Scene geometry, light and media\nRGB image + ToF sensor recording', blue),
        (4.15, 2.82, 3.65, 1.18, 'Perception, reliability & routing', 'Two class distributions\nSignal quality + sensor disagreement', blue),
        (8.65, 2.82, 3.0, 1.18, 'Decision & outputs', 'Class probabilities and label\nDecision trace + evaluation report', blue),
        (4.15, .82, 3.65, 1.12, 'Evidence arbitration', 'Configured LLM roles\nEscalated cases only', amber),
        (.35, .82, 3.15, 1.12, 'E1: simulation fidelity', 'Initial vs calibrated simulation\nSeparate calibration / held-out data', gray),
    ]
    for x,y,w,h,title,body,color in boxes:
        ax.add_patch(Rectangle((x,y),w,h,facecolor='white',edgecolor=color,linewidth=1.6))
        ax.text(x+.15,y+h-.29,title,fontsize=12.2,weight='bold',color=color,va='center')
        ax.text(x+.15,y+.41,body,fontsize=11.1,color=gray,va='center',linespacing=1.5)

    arrows=[
        ((3.5,3.4),(4.15,3.4),blue),
        ((7.8,3.4),(8.65,3.4),blue),
        ((5.975,2.82),(5.975,1.94),amber),
        ((1.925,2.82),(1.925,1.94),gray),
    ]
    for start,end,color in arrows:
        ax.annotate('',xy=end,xytext=start,arrowprops={'arrowstyle':'->','color':color,'lw':1.7})
    ax.text(8.22,3.77,'Standard\npath',ha='center',fontsize=10.5,color=blue)
    ax.text(6.12,2.35,'Escalate',fontsize=10.5,color=amber)
    ax.text(2.08,2.35,'Compare\nto real data',fontsize=10.5,color=gray)
    ax.plot([7.8,10.15],[1.38,1.38],color=amber,lw=1.7)
    ax.annotate('',xy=(10.15,2.82),xytext=(10.15,1.38),arrowprops={'arrowstyle':'->','color':amber,'lw':1.7})
    ax.text(8.63,1.59,'Arbitrated evidence',fontsize=10.5,color=amber,ha='center')
    ax.text(.35,.31,'E2 compares fusion methods on paired synthetic stress cases. No experiment data or performance values are drawn here.',fontsize=10.6,color=gray)

    fig.canvas.draw()
    renderer=fig.canvas.get_renderer()
    canvas=fig.bbox
    for text in ax.texts:
        bounds=text.get_window_extent(renderer)
        assert canvas.contains(bounds.x0,bounds.y0) and canvas.contains(bounds.x1,bounds.y1), text.get_text()
    out=Path(__file__).resolve().parent
    fig.savefig(out/'pcmef-method.svg',facecolor='white',metadata={'Date':None})
    fig.savefig(out/'pcmef-method.png',dpi=300,facecolor='white')
    plt.close(fig)
    print('PASS: method figure exported as SVG and PNG; all labels fit the canvas.')


if __name__=='__main__':
    main()
