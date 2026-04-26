# Job matrix

The job matrix is the killer view for sweep batches: a model × dataset grid
where each cell is one job. It takes a deliberately calm approach — the default state is white cells with a thin grey border;
emphasis is reserved for two cells per matrix.

## Layout

```
              dataset₁    dataset₂    dataset₃    dataset₄
            ┌─────────┬─────────┬─────────┬─────────┐
   model₁   │  0.382  │  0.412  │  0.470  │  0.521  │
            ├─────────┼─────────┼─────────┼─────────┤
   model₂   │  0.354  │  0.394  │ 🏆 0.341│  0.488  │  ← global best (green border, trophy)
            ├─────────┼─────────┼─────────┼─────────┤
   model₃   │  0.415  │  0.510  │  0.499  │ ⚠ 0.612 │  ← global worst (red border, warning)
            └─────────┴─────────┴─────────┴─────────┘
```

The unit is the **whole matrix**, not per-row or per-column. There is at
most one global best and one global worst per primary metric.

## Cell decoration

Each cell carries:

* **White background + thin grey border** by default. Calm reading.
* **A status dot in the top-right corner** (the unified 5-bucket palette
  shared with cards and badges) — *but* clean `done` cells without idle-flag
  get **no dot**, since "this run finished" doesn't need decoration.
* **Best cell** (one global): thicker green border + trophy icon.
* **Worst cell** (one global): thicker red border + warning icon.

## Best/worst eligibility

Only cells that satisfy **all** of these are eligible:

* `status === 'done'` (running / failed / stalled cells are ineligible).
* `is_idle_flagged === false` (a run that stalled mid-training cannot win).
* The primary metric value is finite (`Number.isFinite`).

Direction (`lower-is-better` / `higher-is-better`) is configured per metric.
With ≤1 eligible cell, no highlight is drawn. Ties (≥2 cells share the best
or worst value) are also skipped — celebrating any one would be misleading.

## Primary metric

The primary metric is the column the global best/worst is computed over.
By default it's the first metric a project's jobs report; set explicitly in
**Project settings → Primary metric**.

## Hyperparameter / metric columns

The toolbar **Columns** button picks which metric and hyperparameter
columns to show. Long values truncate; hover for the full string. Choices
persist per user per batch.

## CSV export

The **Export CSV** button emits the visible columns and current sort. The
result opens cleanly in pandas / Excel — the simplest path from "I ran a
sweep" to "I have a paper table".

## See also

* [Job detail](job-detail.md) — drill into a single cell.
* The implementation lives in `frontend/src/components/JobMatrix.vue` —
  the header comment explains the redesign rationale.
