# Method figure source

`pcmef-method.svg` is the editable vector figure used by the main README. `pcmef-method.png` is its raster fallback. Both describe the method, not measured performance or final research results.

Generate both files from the repository root with the existing `figures` extra:

```powershell
python -m pip install -e ".[figures]"
python docs/assets/draw_method.py
```

The script loads no experiment data, model checkpoints, run locks, or provider credentials. Its built-in check requires ASCII-only figure labels and rejects labels outside the canvas or overlapping labels. Inspect the exported image before publishing a change.

## Visual reading guide

The figure uses English throughout. Teal marks paired sensor inputs, blue marks perception and routing, violet marks decision records, and amber marks selective arbitration. Solid arrows show the standard route; dashed amber arrows show the conditional route. Input icons are schematic, not dataset samples or screenshots. The E1 comparison and separate real-data roles remain distinct from the E2 synthetic-stress comparison. No performance values are drawn.

## Source mapping

| Figure element | Source |
|---|---|
| Simulation and paired RGB–ToF inputs | [`pipeline.py` nodes `simulation` and `paired`](../../pcmef/console/pipeline.py); [`simulation/paired.py`](../../pcmef/simulation/paired.py) |
| Perception, reliability, and routing | [`pipeline.py` nodes `perception`, `reliability`, `routing`](../../pcmef/console/pipeline.py) |
| Standard path and escalated-only arbitration | [`pipeline.py` nodes `arbitration`, `decision`](../../pcmef/console/pipeline.py); [`pcmef_orchestrator.py`](../../pcmef/perception/pcmef_orchestrator.py) |
| Class distributions, decision trace, report | [`pipeline.py` node `decision`](../../pcmef/console/pipeline.py); [`decision_trace.py`](../../pcmef/experiments/decision_trace.py); [`e2_formal.py`](../../pcmef/experiments/e2_formal.py) |
| E1 comparison and separate real-data roles | Main README E1 question; [`e1.py`](../../pcmef/experiments/e1.py); [`splits.py`](../../pcmef/core/splits.py) |
| Partial calibration and incomplete final E2 | [`STATUS.md`](../../STATUS.md), latest recorded research checks; [`pipeline.py`](../../pcmef/console/pipeline.py) calibration detail |

The layout and labels are original to this repository. [figures4papers](https://github.com/ChenLiu-1996/figures4papers), particularly its [design notes](https://github.com/ChenLiu-1996/figures4papers/blob/main/scientific-figure-making/references/design-theory.md), informed the white background, readable sans-serif text, editable vector export, and high-resolution PNG. No source code, artwork, or result data was copied.
