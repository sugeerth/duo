# Long horizons at scale · 12,000 runs · 1,375,787 steps

Generated and read in 81 s (77 s reading: 17,932 steps/s, one run in memory at a time: 16 MB peak; longest run 165 steps).

> **9,000 of 9,000 known failures caught**, 9,000 at the labelled step; **8,000 were graded a pass by the outcome**, 8,000 of them caught by the long-horizon reading; **0 of 3,000 control runs flagged**; 0 extra modes on planted runs; milestones exact on 12,000 of 12,000.

| mode | caught |
|---|---|
| `budget_exhausted` | 1000/1000 |
| `context_overflow` | 1000/1000 |
| `forgotten_constraint` | 1000/1000 |
| `out_of_order` | 1000/1000 |
| `regression` | 1000/1000 |
| `retry_stall` | 1000/1000 |
| `skipped_unit` | 1000/1000 |
| `swallowed_error` | 1000/1000 |
| `unverified_handoff` | 1000/1000 |

Reproduce: `python3 bench/scale.py --per-mode 1000 --controls 3000` (seed 29; the corpus is regenerated, not shipped).

**Caveats.** The generator and the detectors were written together, so this is a floor on these nine modes, not a claim about modes the corpus does not contain; the per-mode counts show it is not one mode carrying the total. The outcome grade is a rule (last check passed and the answer claims done), not a model judge. Values-based modes (stale value, drift, late fault) are not covered.
