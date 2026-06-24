# services/training/analysis — session analysis engine

The classifier + peak/trough detection + per-type chart spec for running HR sessions, distilled
from the athlete's "workout chat 3" and handed off as a self-contained artefact. Entry point:

```python
from part_a_logic import Athlete
from part_a_revisions import analyse_safe
res = analyse_safe(hr_per_second, duration_min, Athlete(), sport_label, timestamps_s=None)
```

- **Per-second HR only** — `analyse_safe` will reject pre-binned input (R1 guard). Feed the raw
  1 Hz series, NOT the downsampled dashboard trace.
- Deps: numpy (required), scipy (optional — enables the bi-exp / envelope fits).
- `part_a_logic.py` = base logic; `part_a_revisions.py` = R1–R7 fixes, use `analyse_safe`.
- Validated against the full export: reproduces the trusted recent classifications and corrects
  two that the previous first-cut got wrong (interval sessions mislabelled tempo).
- Persona / commentary context (private, drives the training agent) lives in
  `odinlake/private-data: agents/training/training-context.md`.
