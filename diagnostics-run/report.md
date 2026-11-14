# Host Decimal-context diagnostic — Assay Batch Insights

Diagnostic only: the product was **not** modified, no business rule was changed, and no fix is proposed here. Every statement below is backed by a trial record under `records/<trial-id>/record.json`.

## How the evidence was produced

* Python 3.11.2; frozen input hashes are in `freeze/FROZEN.json` (inputs, fixtures and warehouse example package).
* Every trial ran in a **new subprocess**. Control and perturbed runs use the same scenario code; the control is simply the trial whose factor is the interpreter default.
* Control environment: `TZ=UTC`, `PYTHONHASHSEED=0`. `TZ` and hash seed were varied separately and never combined with a decimal change, so no causal claim mixes factors.
* Each trial changed **exactly one** factor: precision, rounding, one decimal trap, `TZ`, or `PYTHONHASHSEED`. Decimal contexts were applied in the trial process (and, for nested CLIs, through the committed `diagnostics/shim/sitecustomize.py` plus `ASSAY_DIAG_CONTEXT`); the applied context is read back and recorded, so a factor that failed to take effect is reported as *evidence insufficient*, never as *no effect*.
* Trials are isolated: each has its own output directory and (for the warehouse) its own database copy. Frozen inputs, fixtures and package are only ever copied, never written.
* For every trial the record stores: command, actual context, environment, exit status / raised exception with the deepest in-project frame, and the artifact digests. Observation is by wrapping the product's own functions at runtime (restored in `finally`), never by editing them.

## Root cause (code-level)

The product keeps every numeric fact in `decimal.Decimal` and performs all
arithmetic in CPython's **thread-local decimal context** — the module-level
`getcontext()`. That context is process-global state supplied by the *host*,
never asserted by the product:

* `assay_insights/input.py` computes `(signal - intercept) / slope * dilution`
  directly on the ambient context (`_load_legacy`, `_load_timed`), and compares
  the result with the fixed bounds `0`/`1e12` and with policy decimals.
* `assay_insights/metrics.py::_statistics` computes mean, variance, `sqrt` and
  RSD on the ambient context; `_analyze_legacy`/`_analyze_timed` compare the
  unrounded RSD/recovery with policy limits (`>` and inclusive `<=`) and then
  round for display in `shown()`. `shown()` uses `localcontext()` to raise
  *precision* only — it inherits the host's rounding mode and trap flags, so
  display text and display-time exceptions are also host-dependent.
* `assay_insights/warehouse/reports.py` re-derives each claimed
  `concentration_mg_l` with `Decimal(...) / ...` and requires **exact**
  equality (`!=`) with the report's six-decimal text, then re-runs
  `metrics.analyze` and compares the resulting `samples`/`quality_controls`
  sections for exact equality.
* `assay_insights/acquire/receiver.py::finalize` validates the staged CSV by
  calling `input.load` in the *acquirer's* process before publishing.
* `assay_insights/warehouse/query.py` reads only stored text, so it is stable.

No entry point pins the whole context: the only `localcontext()` use
(`metrics.shown`) sets `prec` and inherits everything else, and
`decimal.Context(...)`/`DefaultContext` are never asserted. The host's
precision, rounding mode and trap flags therefore flow straight into products,
QC verdicts, the warehouse's self-consistency check (which re-computes values
written under a possibly different context) and the serial publisher's
pre-publish gate.

Two distinct failure mechanisms follow, and they should not be conflated:

1. **Same-process context leakage** — one embedding process changes the
   context and then calls `run`, the warehouse importer or the acquirer. The
   computation, the ruling and (for the warehouse) the verification all move
   together.
2. **Cross-context disagreement** — a report is produced under one context and
   verified or imported under another. The verifier recomputes the same
   mathematical fact differently and reports a *contract violation* even though
   the report faithfully records what the producer decided.

## Headline

| Outcome | Trials | Scenarios affected | Fixtures |
| --- | --- | --- | --- |
| Guard weakened (control rejects, trial accepts) | 9 | acquire, analyze | range-edge-legacy, range-edge-timed |
| Decision change | 19 | acquire, analyze, cli, run | nonterm-timed, qc-boundary-legacy, qc-boundary-timed, rsd-threshold-legacy |
| Exception / abort | 29 | analyze, cli, run, validate-report, warehouse-import | examples, nonterm-legacy, nonterm-timed, qc-boundary-legacy, qc-boundary-timed, rsd-threshold-legacy |
| Numeric drift (no decision change) | 57 | analyze, cli, run | nonterm-legacy, nonterm-timed, qc-boundary-legacy, qc-boundary-timed, rsd-threshold-legacy |
| No effect | 95 | acquire, analyze, cli, run, validate-report, warehouse-query | clean-timed, examples, nonterm-legacy, nonterm-timed, qc-boundary-legacy, qc-boundary-timed, range-edge-legacy, range-edge-timed |
| Evidence insufficient | 0 | — | — |

## Affected entry points and their exact boundary

| Entry point | Affected? | What moves | Observed |
| --- | --- | --- | --- |
| `input.load` (legacy + timed) | yes | concentration, 0..1e12 admission, target parsing | tested `prec` 1, 2, 4, 6 admit an exact ≈1.0000001e12 concentration; traps on non-terminating division raise raw `decimal` errors |
| `metrics.analyze` (legacy + timed) | yes | mean, variance, sqrt, RSD, recovery, reportability | all statistics and every `>`/`<=` comparison; display rounding in `shown()` also inherits rounding/traps |
| `report.run` / `python -m assay_insights` | yes | everything above plus JSON/HTML bytes | artifact hashes change; decision changes only on boundary values; exit code 1 on traps |
| `warehouse.reports.load_report` | yes | exact equality of `concentration_mg_l`; exact equality of re-run `samples`/`quality_controls` | rejects default-context reports at `prec ≤ 8`; rejects any report with a non-terminating concentration at the default context |
| `warehouse.import_manifest` | yes (via verifier) | commit/reject and knowledge_seq | an import that should commit is rejected, so the ledger never advances |
| `warehouse.build_query` | no | reads stored text only | all query trials `no_effect`, including `TZ` and `PYTHONHASHSEED` |
| `acquire` pre-publish gate | yes | publish/reject, exit code, REJ line, staging diagnostics | `prec 1–6` publishes a CSV the default context rejects; `Inexact` trap bypasses the documented error path |
| `acquire` frame protocol / CRC / locking | no | bytes, CRC32, flock | no decimal arithmetic involved |

Boundary statement, from the recorded first divergences. For a precision change the first difference is always `load` (the concentration text), because calibration is computed there. For a rounding change it is also `load`. For a trap it is the first operation that is inexact under that context: `load` when the fixture's calibration actually divides inexactly (`nonterm-*`, `qc-boundary-timed`), and `statistics` when the calibration is exact but the mean/`sqrt` is not (`qc-boundary-legacy`, `rsd-threshold-legacy`, both slope 1). A terminating fixture (`clean-timed`) never raises and is `no_effect` under every trap tried. The propagation chain is `load → statistics → qc_recovery → analyze → artifact`; for the warehouse verifier it is `validate` (which internally re-runs `analyze`), and for acquisition `process → instrument → artifact → default_validation`.

## Per-scenario results

### `analyze`

**`analyze::clean-timed`** — control `analyze__clean-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=1 | `no_effect` | `context_only` |  | no | — |
| prec=12 | `no_effect` | `context_only` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=4 | `no_effect` | `context_only` |  | no | — |
| prec=6 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |
| trap Rounded | `no_effect` | `context_only` |  | no | — |

**`analyze::nonterm-legacy`** — control `analyze__nonterm-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[1].concentration_exact`: 3.666666666666666666666666667 → 3.666666666666666666666666666; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3.499999999999999999999999999; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 6.734350297014738327627089156; `samples.B1/nitrate/S1.mean`: 3.500000 → 3.499999 |
| rounding=ROUND_UP | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.333333333333333333333333334; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3.500000000000000000000000001; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 6.734350297014738327627089156; `samples.B1/nitrate/S1.mean`: 3.500000 → 3.500001; `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734351 |
| control | `control` | `None` |  | no | — |
| prec=1 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3; `measurements[1].concentration_exact`: 3.666666666666666666666666667 → 3; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 0; `samples.B1/nitrate/S1.mean`: 3.500000 → 3.000000; `samples.B1/nitrate/S1.rsd`: 6.734350 → 0.000000 |
| prec=12 | `numeric_drift` | `load` | `load` `statistics` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.33333333333; `measurements[1].concentration_exact`: 3.666666666666666666666666667 → 3.66666666667; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3.50000000000; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 6.73435029714 |
| prec=2 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.3; `measurements[1].concentration_exact`: 3.666666666666666666666666667 → 3.7; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3.5; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 8.0; `samples.B1/nitrate/S1.rsd`: 6.734350 → 8.000000; `samples.B1/nitrate/S1.sd`: 0.235702 → 0.280000 |
| prec=4 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.333; `measurements[1].concentration_exact`: 3.666666666666666666666666667 → 3.667; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3.500; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 6.749; `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.749000; `samples.B1/nitrate/S1.sd`: 0.235702 → 0.236200 |
| prec=50 | `numeric_drift` | `load` | `load` `statistics` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.3333333333333333333333333333333333333333333333333; `measurements[1].concentration_exact`: 3.666666666666666666666666667 → 3.6666666666666666666666666666666666666666666666667; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3.5000000000000000000000000000000000000000000000000; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 6.7343502970147383276270891629033241836651041684631 |
| prec=6 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.33333; `measurements[1].concentration_exact`: 3.666666666666666666666666667 → 3.66667; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3.50000; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 6.73449; `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734490; `samples.B1/nitrate/S1.sd`: 0.235702 → 0.235707 |
| prec=8 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.3333333; `measurements[1].concentration_exact`: 3.666666666666666666666666667 → 3.6666667; `B1/nitrate/S1/None.mean_exact`: 3.500000000000000000000000000 → 3.5000000; `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 6.7343517; `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734352 |
| trap Inexact | `exception` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: ok → error; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| trap Rounded | `exception` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: ok → error; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |

**`analyze::nonterm-timed`** — control `analyze__nonterm-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.333333333333333333333333334; `measurements[1].concentration_exact`: 3.333333333333333333333333333 → 3.333333333333333333333333334; `B1/nitrate/S1/C1.mean_exact`: 3.333333333333333333333333333 → 3.333333333333333333333333334; `samples.B1/nitrate/S1.mean`: 3.333333 → 3.333334 |
| control | `control` | `None` |  | no | — |
| prec=1 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3; `measurements[1].concentration_exact`: 3.333333333333333333333333333 → 3; `B1/nitrate/S1/C1.mean_exact`: 3.333333333333333333333333333 → 3; `samples.B1/nitrate/S1.mean`: 3.333333 → 3.000000 |
| prec=12 | `numeric_drift` | `load` | `load` `statistics` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.33333333333; `measurements[1].concentration_exact`: 3.333333333333333333333333333 → 3.33333333333; `B1/nitrate/S1/C1.mean_exact`: 3.333333333333333333333333333 → 3.33333333333 |
| prec=2 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.3; `measurements[1].concentration_exact`: 3.333333333333333333333333333 → 3.3; `B1/nitrate/S1/C1.mean_exact`: 3.333333333333333333333333333 → 3.3; `samples.B1/nitrate/S1.mean`: 3.333333 → 3.300000 |
| prec=4 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.333; `measurements[1].concentration_exact`: 3.333333333333333333333333333 → 3.333; `B1/nitrate/S1/C1.mean_exact`: 3.333333333333333333333333333 → 3.333; `samples.B1/nitrate/S1.mean`: 3.333333 → 3.333000 |
| prec=6 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.33333; `measurements[1].concentration_exact`: 3.333333333333333333333333333 → 3.33333; `B1/nitrate/S1/C1.mean_exact`: 3.333333333333333333333333333 → 3.33333; `samples.B1/nitrate/S1.mean`: 3.333333 → 3.333330 |
| prec=8 | `numeric_drift` | `load` | `load` `statistics` | no | `measurements[0].concentration_exact`: 3.333333333333333333333333333 → 3.3333333; `measurements[1].concentration_exact`: 3.333333333333333333333333333 → 3.3333333; `B1/nitrate/S1/C1.mean_exact`: 3.333333333333333333333333333 → 3.3333333 |
| trap Inexact | `exception` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: ok → error; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| trap Rounded | `exception` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: ok → error; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |

**`analyze::qc-boundary-legacy`** — control `analyze__qc-boundary-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `numeric_drift` | `analyze` | `analyze` | no | `quality_controls.B1/nitrate/Q1/None.recovery`: 80.000000 → 79.999999 |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=1 | `decision_change` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | `measurements[2].concentration_exact`: 7.999999999 → 8; `B1/nitrate/Q1/None.mean_exact`: 7.999999999 → 8; `B1/nitrate/S1/None.rsd_exact`: 0 → 0.0; `B1/nitrate/Q1/None.recovery_exact`: 79.9999999900 → 80; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0 |
| prec=12 | `no_effect` | `context_only` |  | no | — |
| prec=2 | `decision_change` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | `measurements[2].concentration_exact`: 7.999999999 → 8.0; `B1/nitrate/Q1/None.mean_exact`: 7.999999999 → 8.0; `B1/nitrate/Q1/None.recovery_exact`: 79.9999999900 → 80; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0 |
| prec=4 | `decision_change` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | `measurements[2].concentration_exact`: 7.999999999 → 8.000; `B1/nitrate/Q1/None.mean_exact`: 7.999999999 → 8.000; `B1/nitrate/Q1/None.recovery_exact`: 79.9999999900 → 80.00; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0 |
| prec=6 | `decision_change` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | `measurements[2].concentration_exact`: 7.999999999 → 8.00000; `B1/nitrate/Q1/None.mean_exact`: 7.999999999 → 8.00000; `B1/nitrate/Q1/None.recovery_exact`: 79.9999999900 → 80.0000; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0 |
| prec=8 | `decision_change` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | `measurements[2].concentration_exact`: 7.999999999 → 8.0000000; `B1/nitrate/Q1/None.mean_exact`: 7.999999999 → 8.0000000; `B1/nitrate/Q1/None.recovery_exact`: 79.9999999900 → 80.000000; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0 |
| trap Inexact | `exception` | `statistics` | `statistics` `analyze` | yes | `B1/nitrate/S1/None`: {"mean_exact": "10", "n": 2, "rsd_exact": "0", "sd_exact": "0"} → <absent>; stage `analyze`: ok → error |
| trap Rounded | `exception` | `statistics` | `statistics` `analyze` | yes | `B1/nitrate/S1/None`: {"mean_exact": "10", "n": 2, "rsd_exact": "0", "sd_exact": "0"} → <absent>; stage `analyze`: ok → error |

**`analyze::qc-boundary-timed`** — control `analyze__qc-boundary-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `numeric_drift` | `load` | `load` `statistics` `qc_recovery` `analyze` | no | `measurements[2].concentration_exact`: 11.66666666666666666666666667 → 11.66666666666666666666666666; `B1/nitrate/Q1/C1.mean_exact`: 12.00000000000000000000000000 → 11.99999999999999999999999999; `B1/nitrate/Q1/C1.rsd_exact`: 3.928371006591930691115801972 → 3.928371006591930691115802033; `B1/nitrate/Q1/C1.recovery_exact`: 120.0000000000000000000000000 → 119.9999999999999999999999999; `quality_controls.B1/nitrate/Q1/C1.recovery`: 120.000000 → 119.999999; `samples.B1/nitrate/S1.sd`: 0.707107 → 0.707106 |
| rounding=ROUND_UP | `decision_change` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | `measurements[3].concentration_exact`: 12.33333333333333333333333333 → 12.33333333333333333333333334; `B1/nitrate/Q1/C1.mean_exact`: 12.00000000000000000000000000 → 12.00000000000000000000000001; `B1/nitrate/Q1/C1.rsd_exact`: 3.928371006591930691115801972 → 3.928371006591930691115802029; `B1/nitrate/Q1/C1.recovery_exact`: 120.0000000000000000000000000 → 120.0000000000000000000000001; `quality_controls.B1/nitrate/Q1/C1.passed`: true → false; `quality_controls.B1/nitrate/Q1/C1.recovery`: 120.000000 → 120.000001 |
| control | `control` | `None` |  | no | — |
| prec=1 | `numeric_drift` | `load` | `load` `statistics` `qc_recovery` `analyze` | no | `measurements[1].concentration_exact`: 11 → 10; `measurements[2].concentration_exact`: 11.66666666666666666666666667 → 10; `B1/nitrate/Q1/C1.mean_exact`: 12.00000000000000000000000000 → 10; `B1/nitrate/Q1/C1.rsd_exact`: 3.928371006591930691115801972 → 0.0; `B1/nitrate/Q1/C1.recovery_exact`: 120.0000000000000000000000000 → 100; `quality_controls.B1/nitrate/Q1/C1.recovery`: 120.000000 → 100.000000; `samples.B1/nitrate/S1.mean`: 10.500000 → 10.000000 |
| prec=12 | `numeric_drift` | `load` | `load` `statistics` `qc_recovery` | no | `measurements[2].concentration_exact`: 11.66666666666666666666666667 → 11.6666666667; `measurements[3].concentration_exact`: 12.33333333333333333333333333 → 12.3333333333; `B1/nitrate/Q1/C1.mean_exact`: 12.00000000000000000000000000 → 12.0000000000; `B1/nitrate/Q1/C1.rsd_exact`: 3.928371006591930691115801972 → 3.92837100620; `B1/nitrate/Q1/C1.recovery_exact`: 120.0000000000000000000000000 → 120.000000000 |
| prec=2 | `numeric_drift` | `load` | `load` `statistics` `qc_recovery` `analyze` | no | `measurements[2].concentration_exact`: 11.66666666666666666666666667 → 12; `measurements[3].concentration_exact`: 12.33333333333333333333333333 → 12; `B1/nitrate/Q1/C1.mean_exact`: 12.00000000000000000000000000 → 12; `B1/nitrate/Q1/C1.rsd_exact`: 3.928371006591930691115801972 → 0; `B1/nitrate/Q1/C1.recovery_exact`: 120.0000000000000000000000000 → 120; `samples.B1/nitrate/S1.mean`: 10.500000 → 10.000000; `samples.B1/nitrate/S1.rsd`: 6.734350 → 10.000000 |
| prec=4 | `numeric_drift` | `load` | `load` `statistics` `qc_recovery` `analyze` | no | `measurements[2].concentration_exact`: 11.66666666666666666666666667 → 11.67; `measurements[3].concentration_exact`: 12.33333333333333333333333333 → 12.33; `B1/nitrate/Q1/C1.mean_exact`: 12.00000000000000000000000000 → 12.00; `B1/nitrate/Q1/C1.rsd_exact`: 3.928371006591930691115801972 → 3.889; `B1/nitrate/Q1/C1.recovery_exact`: 120.0000000000000000000000000 → 120.0; `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734000; `samples.B1/nitrate/S1.sd`: 0.707107 → 0.707100 |
| prec=6 | `numeric_drift` | `load` | `load` `statistics` `qc_recovery` | no | `measurements[2].concentration_exact`: 11.66666666666666666666666667 → 11.6667; `measurements[3].concentration_exact`: 12.33333333333333333333333333 → 12.3333; `B1/nitrate/Q1/C1.mean_exact`: 12.00000000000000000000000000 → 12.0000; `B1/nitrate/Q1/C1.rsd_exact`: 3.928371006591930691115801972 → 3.92798; `B1/nitrate/Q1/C1.recovery_exact`: 120.0000000000000000000000000 → 120.000 |
| prec=8 | `numeric_drift` | `load` | `load` `statistics` `qc_recovery` | no | `measurements[2].concentration_exact`: 11.66666666666666666666666667 → 11.666667; `measurements[3].concentration_exact`: 12.33333333333333333333333333 → 12.333333; `B1/nitrate/Q1/C1.mean_exact`: 12.00000000000000000000000000 → 12.000000; `B1/nitrate/Q1/C1.rsd_exact`: 3.928371006591930691115801972 → 3.9283671; `B1/nitrate/Q1/C1.recovery_exact`: 120.0000000000000000000000000 → 120.00000 |
| trap Inexact | `exception` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: ok → error; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| trap Rounded | `exception` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: ok → error; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |

**`analyze::range-edge-legacy`** — control `analyze__range-edge-legacy__control` (prec 28, ROUND_HALF_EVEN).

> **Control baseline already aborts**, so this block's control is a *failing* baseline: `load` builtins.ValueError: diagnostics-run/freeze/fixtures/range-edge-legacy/results.csv:2: calibrated concentration must be between 0 and 1e12 mg/L. Rows marked `no_effect` mean the perturbation changed nothing relative to that failure; `guard_weakened` means the perturbation accepted what the control rejected.

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=1 | `guard_weakened` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: error → ok; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| prec=12 | `no_effect` | `context_only` |  | no | — |
| prec=2 | `guard_weakened` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: error → ok; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| prec=4 | `guard_weakened` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: error → ok; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| prec=6 | `guard_weakened` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: error → ok; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |
| trap Rounded | `no_effect` | `context_only` |  | no | — |

**`analyze::range-edge-timed`** — control `analyze__range-edge-timed__control` (prec 28, ROUND_HALF_EVEN).

> **Control baseline already aborts**, so this block's control is a *failing* baseline: `load` builtins.ValueError: diagnostics-run/freeze/fixtures/range-edge-timed/results.csv:2: calibrated concentration must be between 0 and 1e12 mg/L. Rows marked `no_effect` mean the perturbation changed nothing relative to that failure; `guard_weakened` means the perturbation accepted what the control rejected.

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=1 | `guard_weakened` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: error → ok; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| prec=12 | `no_effect` | `context_only` |  | no | — |
| prec=2 | `guard_weakened` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: error → ok; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| prec=4 | `guard_weakened` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: error → ok; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| prec=6 | `guard_weakened` | `load` | `load` `statistics` `qc_recovery` `analyze` | yes | stage `load`: error → ok; stage `statistics`: ok → absent; stage `qc_recovery`: ok → absent; stage `analyze`: ok → absent |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |
| trap Rounded | `no_effect` | `context_only` |  | no | — |

**`analyze::rsd-threshold-legacy`** — control `analyze__rsd-threshold-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `numeric_drift` | `analyze` | `analyze` | no | `samples.B1/nitrate/S1.rsd`: 10.000000 → 9.999999 |
| rounding=ROUND_UP | `numeric_drift` | `analyze` | `analyze` | no | `samples.B1/nitrate/S1.sd`: 49999999999.537020 → 49999999999.537021 |
| control | `control` | `None` |  | no | — |
| prec=1 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 535355339059 → 500000000000; `measurements[1].concentration_exact`: 464644660941 → 500000000000; `B1/nitrate/S1/None.rsd_exact`: 9.999999999907404021923571300 → 0.00000000000; `B1/nitrate/S1/None.sd_exact`: 49999999999.53702010961785650 → 0; `samples.B1/nitrate/S1.rsd`: 10.000000 → 0.000000; `samples.B1/nitrate/S1.sd`: 49999999999.537020 → 0.000000 |
| prec=12 | `numeric_drift` | `statistics` | `statistics` `analyze` | no | `B1/nitrate/S1/None.rsd_exact`: 9.999999999907404021923571300 → 9.99999999992; `B1/nitrate/S1/None.sd_exact`: 49999999999.53702010961785650 → 49999999999.6; `samples.B1/nitrate/S1.sd`: 49999999999.537020 → 49999999999.600000 |
| prec=2 | `decision_change` | `load` | `load` `statistics` `analyze` | yes | `measurements[0].concentration_exact`: 535355339059 → 540000000000; `measurements[1].concentration_exact`: 464644660941 → 460000000000; `B1/nitrate/S1/None.rsd_exact`: 9.999999999907404021923571300 → 11; `B1/nitrate/S1/None.sd_exact`: 49999999999.53702010961785650 → 57000000000; `samples.B1/nitrate/S1.reasons<length>`: 0 → 1; `samples.B1/nitrate/S1.reportable`: true → false |
| prec=4 | `decision_change` | `load` | `load` `statistics` `analyze` | yes | `measurements[0].concentration_exact`: 535355339059 → 535400000000; `measurements[1].concentration_exact`: 464644660941 → 464600000000; `B1/nitrate/S1/None.rsd_exact`: 9.999999999907404021923571300 → 10.01; `B1/nitrate/S1/None.sd_exact`: 49999999999.53702010961785650 → 50060000000; `samples.B1/nitrate/S1.reasons<length>`: 0 → 1; `samples.B1/nitrate/S1.reportable`: true → false |
| prec=50 | `numeric_drift` | `statistics` | `statistics` | no | `B1/nitrate/S1/None.rsd_exact`: 9.999999999907404021923571300 → 9.9999999999074040219235712992422034767703141816656; `B1/nitrate/S1/None.sd_exact`: 49999999999.53702010961785650 → 49999999999.537020109617856496211017383851570908328 |
| prec=6 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 535355339059 → 535355000000; `measurements[1].concentration_exact`: 464644660941 → 464645000000; `B1/nitrate/S1/None.rsd_exact`: 9.999999999907404021923571300 → 9.99992; `B1/nitrate/S1/None.sd_exact`: 49999999999.53702010961785650 → 49999600000; `samples.B1/nitrate/S1.rsd`: 10.000000 → 9.999920; `samples.B1/nitrate/S1.sd`: 49999999999.537020 → 49999600000.000000 |
| prec=8 | `numeric_drift` | `load` | `load` `statistics` `analyze` | no | `measurements[0].concentration_exact`: 535355339059 → 535355340000; `measurements[1].concentration_exact`: 464644660941 → 464644660000; `B1/nitrate/S1/None.rsd_exact`: 9.999999999907404021923571300 → 10.000000; `B1/nitrate/S1/None.sd_exact`: 49999999999.53702010961785650 → 50000002000; `samples.B1/nitrate/S1.sd`: 49999999999.537020 → 50000002000.000000 |
| trap Inexact | `exception` | `statistics` | `statistics` `analyze` | yes | `B1/nitrate/S1/None`: {"mean_exact": "500000000000", "n": 2, "rsd_exact": "9.9999999999074040… → <absent>; stage `analyze`: ok → error |
| trap Rounded | `exception` | `statistics` | `statistics` `analyze` | yes | `B1/nitrate/S1/None`: {"mean_exact": "500000000000", "n": 2, "rsd_exact": "9.9999999999074040… → <absent>; stage `analyze`: ok → error |

### `run`

**`run::clean-timed`** — control `run__clean-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=4 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |

**`run::nonterm-legacy`** — control `run__nonterm-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.mean`: 3.500000 → 3.499999; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → a15b5dbd64bc084962d72cff474fd6dcd60d7627d3aed64a6a59632a1f582335; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → 170b0ffc7ddb473444df0561a0c59327c5e1a27944811cfdcae308bcfa89bd06 |
| rounding=ROUND_UP | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.mean`: 3.500000 → 3.500001; `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734351; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → 104dba645d85ba75a3d8f68ccaa51bf6e277562c022e772fddb60a6fbf554970; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → 4db72e5ae681345c02ebee2be5aac6748b34cd02255cf6ef2529c66d8647450a |
| control | `control` | `None` |  | no | — |
| prec=2 | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.rsd`: 6.734350 → 8.000000; `samples.B1/nitrate/S1.sd`: 0.235702 → 0.280000; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → 67cc1816bb8f4b3ca4da33ca0069f5ea3c80e6a43e6bd886e60c0cd356e0d63d; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → ea6e7bb741767db7387cc0e67d2be5292df6b83cc1ad2fc93025520e5fb26e2d |
| prec=4 | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.749000; `samples.B1/nitrate/S1.sd`: 0.235702 → 0.236200; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → 08c85e6973b142513024a44a742aff73e34c12bb04f556b5c512ca86aadb8334; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → b48246ae1d73c2bc4c53ec5f4b51a184054498e12c8dfc806b25b82523f1242a |
| prec=8 | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734352; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → 8cc27f74028e89d07c259d31b7234f7037814461a04d0235418f159023377bf5; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → f6905c4538c596b24d86aae72e472c380aa1252124d6f080093afb2a3a4536b3 |
| trap Inexact | `exception` | `run` | `run` `analyze` `artifact` | yes | stage `run`: ok → error; stage `analyze`: ok → absent; `report.html`: {"bytes": 884, "sha256": "8a32b7592596002076921e32a98a78af3faf5125b38e3… → <absent>; `report.json`: {"bytes": 1164, "sha256": "3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b20… → <absent> |

**`run::nonterm-timed`** — control `run__nonterm-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.mean`: 3.333333 → 3.333334; `report.html.sha256`: 4fabbc586f9859fbcbfa167ca51d70a6239ef4359eb6f5ddeaa7e1b60aa45640 → 484b9208d753e4a1e2316a308a6c73a26c7550fc4b19dae5418d3c3526b03af0; `report.json.sha256`: 273c6f28332c46dd03f8eacaf03e8d8eb458f84e332744f4d183f0a6adde176c → c6d668fdcfc01316774c729b9b1d5464e89cb83a2bc9f942bac630ee6e5ba410 |
| control | `control` | `None` |  | no | — |
| prec=2 | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.mean`: 3.333333 → 3.300000; `report.html.sha256`: 4fabbc586f9859fbcbfa167ca51d70a6239ef4359eb6f5ddeaa7e1b60aa45640 → 569958f099c925ee63c7b134e4424cd019fa17b349bd498ace8584b8a5573962; `report.json.sha256`: 273c6f28332c46dd03f8eacaf03e8d8eb458f84e332744f4d183f0a6adde176c → 957a4450e9754dac777bc5677c130742f0ff13da17e52def15c4ae32321beb85 |
| prec=4 | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.mean`: 3.333333 → 3.333000; `report.html.sha256`: 4fabbc586f9859fbcbfa167ca51d70a6239ef4359eb6f5ddeaa7e1b60aa45640 → e97a19e1772523a6ec1bd6380e13471ea48c2316d16a3ee1cee6f1ddc6959005; `report.json.sha256`: 273c6f28332c46dd03f8eacaf03e8d8eb458f84e332744f4d183f0a6adde176c → 42d8d818522c544bad789bc9205eb720a7bdfa52e22a2670f9e7f20666061ea4 |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `exception` | `run` | `run` `analyze` `artifact` | yes | stage `run`: ok → error; stage `analyze`: ok → absent; `report.html`: {"bytes": 2526, "sha256": "4fabbc586f9859fbcbfa167ca51d70a6239ef4359eb6… → <absent>; `report.json`: {"bytes": 3069, "sha256": "273c6f28332c46dd03f8eacaf03e8d8eb458f84e3327… → <absent> |

**`run::qc-boundary-legacy`** — control `run__qc-boundary-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `quality_controls.B1/nitrate/Q1/None.recovery`: 80.000000 → 79.999999; `report.html.sha256`: 2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598eb321172f9768e6eb4e → 0c75636d30605f07b6ea6431b1645e28bdec803e57becad996172474a07b02b8; `report.json.sha256`: 83f75842d96117dd9f883dba55aaf4c0b02015f263a1271b25f6d2a1bdcc221b → 9e48933e48c88476e6623017bc16d63f49139cae2a1d0fd6ba38e1e47072bd3d |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=2 | `decision_change` | `run` | `run` `analyze` `artifact` | yes | `summary.reportable_groups`: 0 → 1; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0; `report.html.bytes`: 901 → 884; `report.html.sha256`: 2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598eb321172f9768e6eb4e → c2a2e8d6ae313857bc5e885d107c4f04cf210d7f101f80c45688a47f703a5de9 |
| prec=4 | `decision_change` | `run` | `run` `analyze` `artifact` | yes | `summary.reportable_groups`: 0 → 1; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0; `report.html.bytes`: 901 → 884; `report.html.sha256`: 2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598eb321172f9768e6eb4e → c2a2e8d6ae313857bc5e885d107c4f04cf210d7f101f80c45688a47f703a5de9 |
| prec=8 | `decision_change` | `run` | `run` `analyze` `artifact` | yes | `summary.reportable_groups`: 0 → 1; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0; `report.html.bytes`: 901 → 884; `report.html.sha256`: 2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598eb321172f9768e6eb4e → c2a2e8d6ae313857bc5e885d107c4f04cf210d7f101f80c45688a47f703a5de9 |
| trap Inexact | `exception` | `run` | `run` `analyze` `artifact` | yes | stage `run`: ok → error; stage `analyze`: ok → absent; `report.html`: {"bytes": 901, "sha256": "2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598… → <absent>; `report.json`: {"bytes": 1200, "sha256": "83f75842d96117dd9f883dba55aaf4c0b02015f263a1… → <absent> |

**`run::qc-boundary-timed`** — control `run__qc-boundary-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `quality_controls.B1/nitrate/Q1/C1.recovery`: 120.000000 → 119.999999; `samples.B1/nitrate/S1.sd`: 0.707107 → 0.707106; `report.html.sha256`: d2251989701db40f9ea52d1a15d9371885dafc9330906efd502b973091c8e794 → 98d3f7ae3c1d20d45d81d017a8dff6ffef3832a2b9427afd781711cb89cedc03; `report.json.sha256`: ee2ab41c5b374ece49378c868953d442b8776d58c9846061e5d669f25db4a293 → 0b4e840f68237c1fd35fba7b8a473b1025a658381c294b940e2dd5198ff5d26c |
| rounding=ROUND_UP | `decision_change` | `run` | `run` `analyze` `artifact` | yes | `summary.reportable_groups`: 1 → 0; `quality_controls.B1/nitrate/Q1/C1.passed`: true → false; `quality_controls.B1/nitrate/Q1/C1.recovery`: 120.000000 → 120.000001; `report.html.bytes`: 2714 → 2730; `report.html.sha256`: d2251989701db40f9ea52d1a15d9371885dafc9330906efd502b973091c8e794 → 187db1769f122e22060e9153a2fdea60c23b8f7c9e5917a6c257e560852826a4 |
| control | `control` | `None` |  | no | — |
| prec=2 | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.mean`: 10.500000 → 10.000000; `samples.B1/nitrate/S1.rsd`: 6.734350 → 10.000000; `report.html.bytes`: 2714 → 2715; `report.html.sha256`: d2251989701db40f9ea52d1a15d9371885dafc9330906efd502b973091c8e794 → f8987295f5406a6b9d0515af4e35a3a791206bae37dcb8e914076f00b23c2ee8 |
| prec=4 | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734000; `samples.B1/nitrate/S1.sd`: 0.707107 → 0.707100; `report.html.sha256`: d2251989701db40f9ea52d1a15d9371885dafc9330906efd502b973091c8e794 → 642b13654632a085fec53ff63b60fc168c82285770283c7bcd30f914611ccfe6; `report.json.sha256`: ee2ab41c5b374ece49378c868953d442b8776d58c9846061e5d669f25db4a293 → d4e324ce4f9d61f3df0fabb32bb2916416ac1d7f1fbc6f0ec8e41d0806176f8c |
| prec=8 | `numeric_drift` | `artifact` | `artifact` | no | `report.json.sha256`: ee2ab41c5b374ece49378c868953d442b8776d58c9846061e5d669f25db4a293 → 05a0f024a6300c6d4922b47017ccc436654ec99fcacf53b8a07990c6d580643b |
| trap Inexact | `exception` | `run` | `run` `analyze` `artifact` | yes | stage `run`: ok → error; stage `analyze`: ok → absent; `report.html`: {"bytes": 2714, "sha256": "d2251989701db40f9ea52d1a15d9371885dafc933090… → <absent>; `report.json`: {"bytes": 3537, "sha256": "ee2ab41c5b374ece49378c868953d442b8776d58c984… → <absent> |

**`run::rsd-threshold-legacy`** — control `run__rsd-threshold-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.rsd`: 10.000000 → 9.999999; `report.html.bytes`: 896 → 895; `report.html.sha256`: 2d35222e9745fbd1707e9a573d05d44b83cb195c2d309fbfc23a0a7f53be631f → f773d025596893e1ecae462b24c778ca8a59e8987bef858e1b2fc5feebabe12e |
| rounding=ROUND_UP | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.sd`: 49999999999.537020 → 49999999999.537021; `report.json.sha256`: 4f82c2e7236eed032a73bc8d36903f1ff881d95f02ead09b05065f0e21e05241 → a96cba294d57946359e5cbc98c506a831cb09bfab26ef82972d6b3b47e7e4760 |
| control | `control` | `None` |  | no | — |
| prec=2 | `decision_change` | `run` | `run` `analyze` `artifact` | yes | `summary.reportable_groups`: 1 → 0; `samples.B1/nitrate/S1.reasons<length>`: 0 → 1; `samples.B1/nitrate/S1.reportable`: true → false; `report.html.bytes`: 896 → 919; `report.html.sha256`: 2d35222e9745fbd1707e9a573d05d44b83cb195c2d309fbfc23a0a7f53be631f → 7e3de30b3e7113a2bf6d1fa13c71e39b5cddaa773b2efbd72bf9015fa1143606 |
| prec=4 | `decision_change` | `run` | `run` `analyze` `artifact` | yes | `summary.reportable_groups`: 1 → 0; `samples.B1/nitrate/S1.reasons<length>`: 0 → 1; `samples.B1/nitrate/S1.reportable`: true → false; `report.html.bytes`: 896 → 919; `report.html.sha256`: 2d35222e9745fbd1707e9a573d05d44b83cb195c2d309fbfc23a0a7f53be631f → 3a446b57ba66757276bc0ad7f0ba0769c5df8d12193cf6df37351cc899ddb9ba |
| prec=8 | `numeric_drift` | `analyze` | `analyze` `artifact` | no | `samples.B1/nitrate/S1.sd`: 49999999999.537020 → 50000002000.000000; `report.json.sha256`: 4f82c2e7236eed032a73bc8d36903f1ff881d95f02ead09b05065f0e21e05241 → 6fbfcea5c80f5e025a90f141a49c012589dc667dcc4d05314b3ee871449adf68 |
| trap Inexact | `exception` | `run` | `run` `analyze` `artifact` | yes | stage `run`: ok → error; stage `analyze`: ok → absent; `report.html`: {"bytes": 896, "sha256": "2d35222e9745fbd1707e9a573d05d44b83cb195c2d309… → <absent>; `report.json`: {"bytes": 1187, "sha256": "4f82c2e7236eed032a73bc8d36903f1ff881d95f02ea… → <absent> |

### `cli`

**`cli::clean-timed`** — control `cli__clean-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| `PYTHONHASHSEED=1` | `no_effect` | `context_only` |  | no | — |
| `PYTHONHASHSEED=12345` | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| `TZ=Asia/Tokyo` | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=1 | `no_effect` | `context_only` |  | no | — |
| prec=4 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |

**`cli::nonterm-legacy`** — control `cli__nonterm-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| `PYTHONHASHSEED=1` | `no_effect` | `context_only` |  | no | — |
| `PYTHONHASHSEED=12345` | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_DOWN | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `samples.B1/nitrate/S1.mean`: 3.500000 → 3.499999; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → a15b5dbd64bc084962d72cff474fd6dcd60d7627d3aed64a6a59632a1f582335; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → 170b0ffc7ddb473444df0561a0c59327c5e1a27944811cfdcae308bcfa89bd06 |
| rounding=ROUND_UP | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `samples.B1/nitrate/S1.mean`: 3.500000 → 3.500001; `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734351; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → 104dba645d85ba75a3d8f68ccaa51bf6e277562c022e772fddb60a6fbf554970; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → 4db72e5ae681345c02ebee2be5aac6748b34cd02255cf6ef2529c66d8647450a |
| `TZ=Asia/Tokyo` | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=1 | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `samples.B1/nitrate/S1.mean`: 3.500000 → 3.000000; `samples.B1/nitrate/S1.rsd`: 6.734350 → 0.000000; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → 403e4842d10ea3448c00c7bf5609fcc52e89ceb1ae434bb5031e069059e20097; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → 2341af7d6649e1e4dd262d969bae793d3fc18195c5cd4a3e81a9a7de3287b789 |
| prec=4 | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.749000; `samples.B1/nitrate/S1.sd`: 0.235702 → 0.236200; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → 08c85e6973b142513024a44a742aff73e34c12bb04f556b5c512ca86aadb8334; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → b48246ae1d73c2bc4c53ec5f4b51a184054498e12c8dfc806b25b82523f1242a |
| prec=8 | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734352; `report.html.sha256`: 8a32b7592596002076921e32a98a78af3faf5125b38e3955053da33027095d6e → 8cc27f74028e89d07c259d31b7234f7037814461a04d0235418f159023377bf5; `report.json.sha256`: 3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b200a8923cc290770506e0b → f6905c4538c596b24d86aae72e472c380aa1252124d6f080093afb2a3a4536b3 |
| trap Inexact | `exception` | `cli` | `cli` `report_decision` `artifact` | yes | `exit_code`: 0 → 1; `product_frame`: null → {"file": "assay_insights/input.py", "line": 159}; stage `report_decision`: ok → absent; `report.html`: {"bytes": 884, "sha256": "8a32b7592596002076921e32a98a78af3faf5125b38e3… → <absent>; `report.json`: {"bytes": 1164, "sha256": "3dbbeb3db791ba7762a8a8b158bd6f745f9f71835b20… → <absent> |

**`cli::qc-boundary-legacy`** — control `cli__qc-boundary-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| `PYTHONHASHSEED=1` | `no_effect` | `context_only` |  | no | — |
| `PYTHONHASHSEED=12345` | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_DOWN | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `quality_controls.B1/nitrate/Q1/None.recovery`: 80.000000 → 79.999999; `report.html.sha256`: 2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598eb321172f9768e6eb4e → 0c75636d30605f07b6ea6431b1645e28bdec803e57becad996172474a07b02b8; `report.json.sha256`: 83f75842d96117dd9f883dba55aaf4c0b02015f263a1271b25f6d2a1bdcc221b → 9e48933e48c88476e6623017bc16d63f49139cae2a1d0fd6ba38e1e47072bd3d |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| `TZ=Asia/Tokyo` | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=1 | `decision_change` | `cli` | `cli` `report_decision` `artifact` | yes | `stdout`: {"reportable_groups": 0, "sample_groups": 1} → {"reportable_groups": 1, "sample_groups": 1}; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0; `report.html.bytes`: 901 → 884; `report.html.sha256`: 2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598eb321172f9768e6eb4e → c2a2e8d6ae313857bc5e885d107c4f04cf210d7f101f80c45688a47f703a5de9 |
| prec=4 | `decision_change` | `cli` | `cli` `report_decision` `artifact` | yes | `stdout`: {"reportable_groups": 0, "sample_groups": 1} → {"reportable_groups": 1, "sample_groups": 1}; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0; `report.html.bytes`: 901 → 884; `report.html.sha256`: 2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598eb321172f9768e6eb4e → c2a2e8d6ae313857bc5e885d107c4f04cf210d7f101f80c45688a47f703a5de9 |
| prec=8 | `decision_change` | `cli` | `cli` `report_decision` `artifact` | yes | `stdout`: {"reportable_groups": 0, "sample_groups": 1} → {"reportable_groups": 1, "sample_groups": 1}; `quality_controls.B1/nitrate/Q1/None.passed`: false → true; `samples.B1/nitrate/S1.reasons<length>`: 1 → 0; `report.html.bytes`: 901 → 884; `report.html.sha256`: 2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598eb321172f9768e6eb4e → c2a2e8d6ae313857bc5e885d107c4f04cf210d7f101f80c45688a47f703a5de9 |
| trap Inexact | `exception` | `cli` | `cli` `report_decision` `artifact` | yes | `exit_code`: 0 → 1; `product_frame`: null → {"file": "assay_insights/metrics.py", "line": 11}; stage `report_decision`: ok → absent; `report.html`: {"bytes": 901, "sha256": "2daad20fdf2bc489cd2a78ea1625d8bb2b0dc4250b598… → <absent>; `report.json`: {"bytes": 1200, "sha256": "83f75842d96117dd9f883dba55aaf4c0b02015f263a1… → <absent> |

**`cli::qc-boundary-timed`** — control `cli__qc-boundary-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| `PYTHONHASHSEED=1` | `no_effect` | `context_only` |  | no | — |
| `PYTHONHASHSEED=12345` | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_DOWN | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `quality_controls.B1/nitrate/Q1/C1.recovery`: 120.000000 → 119.999999; `samples.B1/nitrate/S1.sd`: 0.707107 → 0.707106; `report.html.sha256`: d2251989701db40f9ea52d1a15d9371885dafc9330906efd502b973091c8e794 → 98d3f7ae3c1d20d45d81d017a8dff6ffef3832a2b9427afd781711cb89cedc03; `report.json.sha256`: ee2ab41c5b374ece49378c868953d442b8776d58c9846061e5d669f25db4a293 → 0b4e840f68237c1fd35fba7b8a473b1025a658381c294b940e2dd5198ff5d26c |
| rounding=ROUND_UP | `decision_change` | `cli` | `cli` `report_decision` `artifact` | yes | `stdout`: {"reportable_groups": 1, "sample_groups": 1} → {"reportable_groups": 0, "sample_groups": 1}; `quality_controls.B1/nitrate/Q1/C1.passed`: true → false; `quality_controls.B1/nitrate/Q1/C1.recovery`: 120.000000 → 120.000001; `report.html.bytes`: 2714 → 2730; `report.html.sha256`: d2251989701db40f9ea52d1a15d9371885dafc9330906efd502b973091c8e794 → 187db1769f122e22060e9153a2fdea60c23b8f7c9e5917a6c257e560852826a4 |
| `TZ=Asia/Tokyo` | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=1 | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `quality_controls.B1/nitrate/Q1/C1.recovery`: 120.000000 → 100.000000; `samples.B1/nitrate/S1.mean`: 10.500000 → 10.000000; `report.html.sha256`: d2251989701db40f9ea52d1a15d9371885dafc9330906efd502b973091c8e794 → bccee438f137d5427af2998d93bc7c435b3997e40e62f5b9023a9de426d9fadc; `report.json.sha256`: ee2ab41c5b374ece49378c868953d442b8776d58c9846061e5d669f25db4a293 → 232805db32044c69c9c352ce6114df7a6a56b8c80521f7e97095c4a6f824de32 |
| prec=4 | `numeric_drift` | `report_decision` | `report_decision` `artifact` | no | `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.734000; `samples.B1/nitrate/S1.sd`: 0.707107 → 0.707100; `report.html.sha256`: d2251989701db40f9ea52d1a15d9371885dafc9330906efd502b973091c8e794 → 642b13654632a085fec53ff63b60fc168c82285770283c7bcd30f914611ccfe6; `report.json.sha256`: ee2ab41c5b374ece49378c868953d442b8776d58c9846061e5d669f25db4a293 → d4e324ce4f9d61f3df0fabb32bb2916416ac1d7f1fbc6f0ec8e41d0806176f8c |
| prec=8 | `numeric_drift` | `artifact` | `artifact` | no | `report.json.sha256`: ee2ab41c5b374ece49378c868953d442b8776d58c9846061e5d669f25db4a293 → 05a0f024a6300c6d4922b47017ccc436654ec99fcacf53b8a07990c6d580643b |
| trap Inexact | `exception` | `cli` | `cli` `report_decision` `artifact` | yes | `exit_code`: 0 → 1; `product_frame`: null → {"file": "assay_insights/input.py", "line": 262}; stage `report_decision`: ok → absent; `report.html`: {"bytes": 2714, "sha256": "d2251989701db40f9ea52d1a15d9371885dafc933090… → <absent>; `report.json`: {"bytes": 3537, "sha256": "ee2ab41c5b374ece49378c868953d442b8776d58c984… → <absent> |

### `validate-report`

**`validate-report::clean-timed`** — control `validate-report__clean-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=12 | `no_effect` | `context_only` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=4 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |

**`validate-report::nonterm-legacy`** — control `validate-report__nonterm-legacy__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=12 | `no_effect` | `context_only` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=4 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |

**`validate-report::nonterm-timed`** — control `validate-report__nonterm-timed__control` (prec 28, ROUND_HALF_EVEN).

> **Control baseline already aborts**, so this block's control is a *failing* baseline: `validate` assay_insights.warehouse.errors.ReportValidationError: measurements[1]: concentration_mg_l disagrees with (signal-intercept)/slope*dilution. Rows marked `no_effect` mean the perturbation changed nothing relative to that failure; `guard_weakened` means the perturbation accepted what the control rejected.

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=12 | `no_effect` | `context_only` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=4 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `exception` | `validate` | `validate` | yes | `validate`: measurements[1]: concentration_mg_l disagrees with (signal-intercept)/s… → [<class 'decimal.Inexact'>] |

**`validate-report::qc-boundary-timed`** — control `validate-report__qc-boundary-timed__control` (prec 28, ROUND_HALF_EVEN).

> **Control baseline already aborts**, so this block's control is a *failing* baseline: `validate` assay_insights.warehouse.errors.ReportValidationError: measurements[0]: concentration_mg_l disagrees with (signal-intercept)/slope*dilution. Rows marked `no_effect` mean the perturbation changed nothing relative to that failure; `guard_weakened` means the perturbation accepted what the control rejected.

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=12 | `no_effect` | `context_only` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=4 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `exception` | `validate` | `validate` | yes | `validate`: measurements[0]: concentration_mg_l disagrees with (signal-intercept)/s… → report section quality_controls is inconsistent with its per-measuremen… |
| trap Inexact | `exception` | `validate` | `validate` | yes | `validate`: measurements[0]: concentration_mg_l disagrees with (signal-intercept)/s… → [<class 'decimal.Inexact'>] |

### `warehouse-import`

**`warehouse-import::examples`** — control `warehouse-import__examples__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_DOWN | `exception` | `import:IMP-002-timed.json` | `import:IMP-002-timed.json` `import:IMP-003-rename.json` `import:IMP-004-rerun.json` `import` `query` | yes | `created_kind`: fact_transaction → <absent>; `error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; stage `import:IMP-003-rename.json`: ok → absent; stage `import:IMP-004-rerun.json`: ok → absent; `outcomes[1].created_kind`: fact_transaction → <absent>; `outcomes[1].error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; `knowledge_seq`: 4 → 1; `latest_knowledge_seq`: 4 → 1 |
| rounding=ROUND_UP | `exception` | `import:IMP-002-timed.json` | `import:IMP-002-timed.json` `import:IMP-003-rename.json` `import:IMP-004-rerun.json` `import` `query` | yes | `created_kind`: fact_transaction → <absent>; `error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; stage `import:IMP-003-rename.json`: ok → absent; stage `import:IMP-004-rerun.json`: ok → absent; `outcomes[1].created_kind`: fact_transaction → <absent>; `outcomes[1].error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; `knowledge_seq`: 4 → 1; `latest_knowledge_seq`: 4 → 1 |
| control | `control` | `None` |  | no | — |
| prec=2 | `exception` | `import:IMP-002-timed.json` | `import:IMP-002-timed.json` `import:IMP-003-rename.json` `import:IMP-004-rerun.json` `import` `query` | yes | `created_kind`: fact_transaction → <absent>; `error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; stage `import:IMP-003-rename.json`: ok → absent; stage `import:IMP-004-rerun.json`: ok → absent; `outcomes[1].created_kind`: fact_transaction → <absent>; `outcomes[1].error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; `knowledge_seq`: 4 → 1; `latest_knowledge_seq`: 4 → 1 |
| prec=4 | `exception` | `import:IMP-002-timed.json` | `import:IMP-002-timed.json` `import:IMP-003-rename.json` `import:IMP-004-rerun.json` `import` `query` | yes | `created_kind`: fact_transaction → <absent>; `error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; stage `import:IMP-003-rename.json`: ok → absent; stage `import:IMP-004-rerun.json`: ok → absent; `outcomes[1].created_kind`: fact_transaction → <absent>; `outcomes[1].error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; `knowledge_seq`: 4 → 1; `latest_knowledge_seq`: 4 → 1 |
| prec=6 | `exception` | `import:IMP-002-timed.json` | `import:IMP-002-timed.json` `import:IMP-003-rename.json` `import:IMP-004-rerun.json` `import` `query` | yes | `created_kind`: fact_transaction → <absent>; `error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; stage `import:IMP-003-rename.json`: ok → absent; stage `import:IMP-004-rerun.json`: ok → absent; `outcomes[1].created_kind`: fact_transaction → <absent>; `outcomes[1].error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; `knowledge_seq`: 4 → 1; `latest_knowledge_seq`: 4 → 1 |
| prec=8 | `exception` | `import:IMP-002-timed.json` | `import:IMP-002-timed.json` `import:IMP-003-rename.json` `import:IMP-004-rerun.json` `import` `query` | yes | `created_kind`: fact_transaction → <absent>; `error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; stage `import:IMP-003-rename.json`: ok → absent; stage `import:IMP-004-rerun.json`: ok → absent; `outcomes[1].created_kind`: fact_transaction → <absent>; `outcomes[1].error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; `knowledge_seq`: 4 → 1; `latest_knowledge_seq`: 4 → 1 |
| trap Inexact | `exception` | `import:IMP-002-timed.json` | `import:IMP-002-timed.json` `import:IMP-003-rename.json` `import:IMP-004-rerun.json` `import` `query` | yes | `created_kind`: fact_transaction → <absent>; `error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; stage `import:IMP-003-rename.json`: ok → absent; stage `import:IMP-004-rerun.json`: ok → absent; `outcomes[1].created_kind`: fact_transaction → <absent>; `outcomes[1].error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; `knowledge_seq`: 4 → 1; `latest_knowledge_seq`: 4 → 1 |
| trap Rounded | `exception` | `import:IMP-002-timed.json` | `import:IMP-002-timed.json` `import:IMP-003-rename.json` `import:IMP-004-rerun.json` `import` `query` | yes | `created_kind`: fact_transaction → <absent>; `error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; stage `import:IMP-003-rename.json`: ok → absent; stage `import:IMP-004-rerun.json`: ok → absent; `outcomes[1].created_kind`: fact_transaction → <absent>; `outcomes[1].error`: <absent> → {"frames": [{"file": "diagnostics/trial.py", "function": "scenario_ware…; `knowledge_seq`: 4 → 1; `latest_knowledge_seq`: 4 → 1 |

### `warehouse-query`

**`warehouse-query::examples`** — control `warehouse-query__examples__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| `PYTHONHASHSEED=7` | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_DOWN | `no_effect` | `context_only` |  | no | — |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| `TZ=Asia/Tokyo` | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |

### `acquire`

**`acquire::clean-timed`** — control `acquire__clean-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |

**`acquire::nonterm-timed`** — control `acquire__nonterm-timed__control` (prec 28, ROUND_HALF_EVEN).

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=2 | `no_effect` | `context_only` |  | no | — |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `decision_change` | `process` | `process` `instrument` `artifact` `default_validation` | yes | `exit_code`: 0 → 1; `product_frame`: null → {"file": "assay_insights/input.py", "line": 262}; `outcome`: acked → session: timed out waiting for a reply; `frames.log`: {"bytes": 945, "sha256": "0804295e9a1f108383a307ffc169767fcf6a472470096… → <absent>; `published`: true → false; `exit_code`: 0 → <absent>; `status`: accepted → not-published |

**`acquire::range-edge-timed`** — control `acquire__range-edge-timed__control` (prec 28, ROUND_HALF_EVEN).

> **Control baseline already aborts**, so this block's control is a *failing* baseline: `process` nonzero_exit: exit 2: acquire failed: results failed input validation: <trial-dir>/.staging/run-1/results.csv:2: calibrated concentration must be between 0 and 1e1…. Rows marked `no_effect` mean the perturbation changed nothing relative to that failure; `guard_weakened` means the perturbation accepted what the control rejected.

| Factor | Class | First divergence | Chain | Decision changed | First observed change |
| --- | --- | --- | --- | --- | --- |
| rounding=ROUND_UP | `no_effect` | `context_only` |  | no | — |
| control | `control` | `None` |  | no | — |
| prec=2 | `guard_weakened` | `process` | `process` `instrument` `artifact` `default_validation` | yes | `exit_code`: 2 → 0; `stderr_last`: acquire failed: results failed input validation: <trial-dir>/.staging/r… → null; `outcome`: session: expected ACK 4, got ('REJ', 4, 'validation') → acked; `frames.log`: <absent> → {"bytes": 987, "sha256": "3594a4a66ef7c2661b738e8edecf9d188c0c3a482f3e7…; `published`: false → true; `exit_code`: <absent> → 1; `status`: not-published → rejected |
| prec=8 | `no_effect` | `context_only` |  | no | — |
| trap Inexact | `no_effect` | `context_only` |  | no | — |

## Designed fixtures and hand calculations

Expectations in `freeze/fixtures/*/expected.json` were derived with exact rational arithmetic (`fractions.Fraction`) and a 90-digit square root cross-checked against the exact variance. The product's arithmetic was not used to derive them, so they are an independent oracle, not a recording of current behaviour.

* `clean-timed` — see `freeze/fixtures/clean-timed/expected.json`
* `nonterm-legacy` — see `freeze/fixtures/nonterm-legacy/expected.json`
* `nonterm-timed` — see `freeze/fixtures/nonterm-timed/expected.json`
* `qc-boundary-legacy` — see `freeze/fixtures/qc-boundary-legacy/expected.json`
* `qc-boundary-timed` — see `freeze/fixtures/qc-boundary-timed/expected.json`
* `range-edge-legacy` — see `freeze/fixtures/range-edge-legacy/expected.json`
* `range-edge-timed` — see `freeze/fixtures/range-edge-timed/expected.json`
* `rsd-threshold-legacy` — see `freeze/fixtures/rsd-threshold-legacy/expected.json`

The hand calculations, and what the product must do with them:

| Fixture | Raw inputs | Exact result | Required behaviour | Observed here |
| --- | --- | --- | --- | --- |
| `nonterm-legacy` | 10/3, 11/3 | mean 7/2; variance 1/18; SD sqrt(2)/6; RSD 6.734350297014738… | sample RSD well under 10% → reportable; QC 100% → pass | control |
| `rsd-threshold-legacy` | 535355339059, 464644660941 (slope 1) | mean 5e11; variance 2499999999953702010962; RSD 9.999999999907404…% | RSD is **strictly below** 10% → reportable. Display rounds to 10.000000, so the report cannot show why | control |
| `qc-boundary-legacy` | QC 7.999999999, target 10 | recovery exactly 79.9999999% | one part in 1e7 below the inclusive 80% → **fail**, though display rounds to 80.000000 | control |
| `qc-boundary-timed` | QC 35/3, 37/3, target 10 | mean exactly 12; recovery exactly 120% | exactly on the inclusive upper limit → **pass**; a 1e-27 sum perturbation flips it | decision flip under ROUND_UP |
| `range-edge-legacy` / `-timed` | signal 999999999999.6, dilution 1.0000001 | concentration 25000002499989999999/25000000 ≈ 1.0000000999996e12 | above the 1e12 acceptance bound → **load must reject** | guard weakened at tested prec 1/2/4/6 |
| `clean-timed` | 10, 10; QC 10, target 5 | all exact: mean 5, SD 0, recovery 100% | every fact exact in any context → must be reportable everywhere | no effect under every factor |

`nonterm-legacy` is the anti-false-positive control: no ruling is near a limit, so *any* decision difference there would mean the diagnostic — not the product — was broken. None was seen. `qc-boundary-timed` is the sharpest counter-example: the default decision is correct *and* the exact value is on the boundary, so a flipped ruling can only come from the context.

## Controlled drift / exception wrappers (detector validation)

These trials keep the default decimal context and instead install a labelled wrapper around the product's own `_statistics` function. They exist only to prove the detector locates the right boundary and does not invent findings:

| Wrapper | Fixture | Class | First divergence | Decision changed | Observed |
| --- | --- | --- | --- | --- | --- |
| `benign-passthrough` | `nonterm-legacy` | `no_effect` | `context_only` | no | — |
| `drift-rsd-over-threshold` | `rsd-threshold-legacy` | `decision_change` | `statistics` | yes | `B1/nitrate/S1/None.rsd_exact`: 9.999999999907404021923571300 → 10.00009999990740402192357130; `samples.B1/nitrate/S1.reasons<length>`: 0 → 1; `samples.B1/nitrate/S1.reportable`: true → false |
| `drift-rsd-sub-threshold` | `nonterm-legacy` | `numeric_drift` | `statistics` | no | `B1/nitrate/S1/None.rsd_exact`: 6.734350297014738327627089177 → 6.735350297014738327627089177; `samples.B1/nitrate/S1.rsd`: 6.734350 → 6.735350 |
| `exception-statistics` | `nonterm-legacy` | `exception` | `statistics` | yes | `B1/nitrate/Q1/None`: {"mean_exact": "7", "n": 1, "rsd_exact": null, "sd_exact": null} → <absent>; `B1/nitrate/S1/None`: {"mean_exact": "3.500000000000000000000000000", "n": 2, "rsd_exact": "6… → <absent>; `B1/nitrate/Q1/None`: {"limits": ["80", "120"], "recovery_exact": "100"} → <absent>; stage `analyze`: ok → error |

A passthrough wrapper reports **no effect**; a drift that crosses a limit reports **decision change** at `statistics`; a thrown exception reports **exception** at the wrapped stage. The detector is therefore shown to respond to real changes and to stay silent when nothing changes.
## Conclusions and their boundaries

Findings are stated as observations with their scope; they are not a claim that the product is wrong in general.

1. **Every numeric entry point inherits the host decimal context; none pins it.** This holds for `load`/`analyze`/`run`, the warehouse import verifier, and the serial acquirer's pre-publish validation. Evidence: the precision/rounding/trap rows above, plus the independent `rc`/first-divergence stages.
2. **Two independent verification mechanisms reject reports produced under another context.** (i) `_check_measurements` recomputes each `concentration_mg_l` and requires exact equality with the six-decimal text, so a non-terminating concentration (10/3 → `3.333333`) is rejected even at the **default** context — the shipped `examples/warehouse/reports/2026-03-03-evening/report.json` contains no such value, but a report from the `nonterm-timed` fixture legitimately does. (ii) `_validate_v2` re-runs `metrics.analyze` and compares the `samples` section exactly; that is what fails for the shipped example when the importer runs at `prec ≤ 8` (verified: prec 9–28 accept, prec 8 rejects with `report section samples is inconsistent with its per-measurement facts`).
3. **Decision flips need a value *on* a boundary — and the boundary cases are real, not contrived.** Three independent flips were observed with their unrounded values captured at the call boundary: `qc-boundary-timed` recovery goes from `120.0000000000000000000000000` to `120.0000000000000000000000001` under `ROUND_UP` and QC flips pass→fail; `qc-boundary-legacy` recovery `79.9999999900` rounds to `80.000000` at `prec ≤ 8` and the sample becomes reportable; `rsd-threshold-legacy` RSD `9.999999999907…` jumps to `11`/`10.01` at `prec 2`/`4` and the sample becomes unreportable. Away from boundaries the identical perturbations move only the last displayed digit.
4. **Same-process leakage and cross-context verification are separable.** (a) Leakage: `input.load` accepts a concentration whose exact value is `25000002499989999999/25000000` mg/L (≈1.0000000999996e12, above the 1e12 bound) at the tested `prec` values 1, 2, 4 and 6, because the multiply rounds it to exactly `1e12`; the identical fixture is rejected at the default context and at `prec 8`. `metrics.analyze` and the acquirer then consume that accepted value. (b) Cross-context: a report produced under the default context is rejected when validated at `prec ≤ 8` or under a different rounding mode, although the report faithfully records the producer's rulings.
5. **The serial acquirer couples host context to publication.** Its pre-publish `load` gate runs in the acquirer process: under `prec=2` the `range-edge-timed` run is published (exit 0) with a CSV the default context rejects (verified independently with a clean `PYTHONPATH`). Under `trap=Inexact` on `nonterm-timed`, the `load` gate raises a raw `decimal.Inexact` instead of the `ValueError` its own handler expects: the run is *not* published, no `REJ` line is sent (the instrument times out waiting for a reply), the CLI exits 1 rather than the documented 2, and the staging directory is left without `error.json` (the exception type bypasses the failure-diagnostic path).
6. **Read-only warehouse queries are immune.** They only render stored text; no decimal is recomputed. All query trials, including `TZ` and hash-seed variations, are `no_effect`.
7. **`TZ` and `PYTHONHASHSEED` had no effect** in any trial. Times are parsed to absolute UTC by `input.parse_time` and no output depends on set/dict iteration order (all sorts are explicit). This is an *observed in these trials* statement, not a proof for every possible environment. Note the contrast with the old suite: its fork-ordering flake (see the old-test section) is scheduling-dependent, while `PYTHONHASHSEED` had no effect in any product trial.

**Boundary of the conclusions.** The observations cover the scenarios and factors enumerated in this report, run on the frozen inputs and hashes listed in `freeze/FROZEN.json`. Precision values not in the matrix, other trap combinations, other rounding modes (`ROUND_CEILING`/`FLOOR`/`05UP`/`HALF_UP`/`HALF_DOWN`), `Emin`/`Emax`/`clamp` changes, multi-threaded callers, and a context mutated *between* calls inside one request were not exhaustively tested. `trap=Underflow`/`Clamped` and `FloatOperation` are untested. The report does not decide which entry point *should* own the context, and it does not change any threshold or rounding rule.

**Not defects (do not file as bugs based on this report).** `qc-boundary-legacy` (recovery 79.9999999% displays as 80.000000 but correctly fails the unrounded inclusive test) and `rsd-threshold-legacy` (exact RSD 9.9999999999…% displays as 10.000000) are display/precision gaps in the shipped decision rule, present at the default context. They are reported as such, not as context defects.

## Minimal commands, independent of the harness

These use only the committed `diagnostics/` helpers. Each is a standalone command with its evidence printed inline; none needs `results.json`.

**1. Exact fixture oracle (no harness, no product arithmetic):**

```sh
python3 -I diagnostics/fixtures.py --out /tmp/assay-fixtures
python3 - <<'PY'
import json; e=json.load(open('/tmp/assay-fixtures/index.json'))
for name, spec in e.items():
    print(name, spec['load']['accepted_at_default_context'],
          {k: v.get('passed_at_default_context') for k, v in spec['groups'].items() if v['kind']=='QC'})
PY
```

**2. One perturbed report run, by hand (precision 8):**

```sh
ASSAY_DIAG_CONTEXT='{"prec": 8}' \
PYTHONPATH="$PWD/diagnostics/shim:$PWD" \
python3 -m assay_insights \
  --calibrations /tmp/assay-fixtures/nonterm-legacy/calibrations.csv \
  --results /tmp/assay-fixtures/nonterm-legacy/results.csv \
  --policy /tmp/assay-fixtures/nonterm-legacy/policy.json \
  --output /tmp/assay-prec8
python3 -c "import json;r=json.load(open('/tmp/assay-prec8/report.json'));print([(s['sample'],s['mean_mg_l'],s['rsd_pct']) for s in r['samples']])"
```

Compare with the same command without `ASSAY_DIAG_CONTEXT`.

**3. Warehouse verifier disagreement, by hand:**

```sh
ASSAY_DIAG_CONTEXT='{"prec": 8}' \
PYTHONPATH="$PWD/diagnostics/shim:$PWD" \
python3 -m assay_insights.warehouse --db /tmp/assay-wh.sqlite \
  --manifest examples/warehouse/IMP-002-timed.json
# default context: exit 0; prec 8: exit 2, 'report section samples is inconsistent with its per-measurement facts'
```

**4. QC boundary at exact 120%, by hand:**

```sh
ASSAY_DIAG_CONTEXT='{"rounding": "ROUND_UP"}' \
PYTHONPATH="$PWD/diagnostics/shim:$PWD" \
python3 -m assay_insights \
  --calibrations /tmp/assay-fixtures/qc-boundary-timed/calibrations.csv \
  --results /tmp/assay-fixtures/qc-boundary-timed/results.csv \
  --policy /tmp/assay-fixtures/qc-boundary-timed/policy.json \
  --output /tmp/assay-qc
# QC recovery 120% passes under the default context and fails under ROUND_UP
```

**5. Acquisition pre-publish gate, by hand:**

```sh
# Control: the acquirer rejects the staged run before publishing (exit 2).
python3 diagnostics/acquire_replay.py \
  --results /tmp/assay-fixtures/range-edge-timed/results.csv \
  --calibrations /tmp/assay-fixtures/range-edge-timed/calibrations.csv \
  --policy /tmp/assay-fixtures/range-edge-timed/policy.json \
  --output /tmp/assay-acq-control

# prec 2: the guard is weakened; the run is published (exit 0),
# while a clean default-context subprocess rejects the published CSV.
python3 diagnostics/acquire_replay.py \
  --results /tmp/assay-fixtures/range-edge-timed/results.csv \
  --calibrations /tmp/assay-fixtures/range-edge-timed/calibrations.csv \
  --policy /tmp/assay-fixtures/range-edge-timed/policy.json \
  --output /tmp/assay-acq-prec2 --context '{"prec": 2}'

# trap Inexact: the gate raises decimal.Inexact (exit 1, no REJ
# line, instrument times out, staging left without error.json).
python3 diagnostics/acquire_replay.py \
  --results /tmp/assay-fixtures/nonterm-timed/results.csv \
  --calibrations /tmp/assay-fixtures/nonterm-timed/calibrations.csv \
  --policy /tmp/assay-fixtures/nonterm-timed/policy.json \
  --output /tmp/assay-acq-inexact --context '{"traps": ["Inexact"]}'
```

## Old test suite (real run, this environment)

Command: `/usr/bin/python3 -m unittest discover -s tests -v`

| Run | Status | Tests | Exit | Seconds |
| --- | --- | --- | --- | --- |
| 1 | pass | 120 | 0 | 4.31 |
| 2 | pass | 120 | 0 | 4.55 |
| 3 | pass | 120 | 0 | 4.19 |

### Repeated fork-ordering test under CPU load

`/usr/bin/python3 -m unittest tests.test_warehouse_concurrency.ConcurrencyTests.test_two_distinct_packages_serialize_to_contiguous_ledger` repeated **120** times with 4 busy-loop processes (default (prec 28, ROUND_HALF_EVEN), TZ=UTC): **2 failures**, 12.35 s.

* Iteration 1: `AssertionError: Lists differ: [(1, 'IMP-B'), (2, 'IMP-A')] != [(1, 'IMP-A'), (2, 'IMP-B')]`
* Iteration 56: `AssertionError: Lists differ: [(1, 'IMP-B'), (2, 'IMP-A')] != [(1, 'IMP-A'), (2, 'IMP-B')]`

Conclusion recorded by the harness: Pre-existing scheduling assumption in the test, not a decimal-context effect: the same test fails with the ledger in the reverse order and no decimal factor applied.

This is reported as a **pre-existing test-suite defect (an ordering assumption), not a decimal-context effect**: no decimal factor was applied, `TZ` was fixed to UTC, and the failure is a legitimate reversed ledger order rather than a wrong sequence.

Interpretation: the suite is a same-context regression suite. It never changes the decimal context, `TZ` or the hash seed, so it cannot detect the context inheritance described above. Any failure it does show is unrelated to the factor matrix; it is reported verbatim rather than dismissed. The concurrency tests fork two processes and assert a scheduling-dependent ordering, so repeated runs are recorded to distinguish a flake from a deterministic failure.

## Where the full evidence lives

`evidence.md` is a generated appendix with one row per executed trial: the context read back from the trial process, the environment, the exit code, the deepest in-project exception frame and an artifact digest summary. `results.json` carries the same data plus the stage-by-stage comparison and classification. The raw per-trial `spec.json`/`record.json` pairs and the trial-private outputs remain under `records/`.
