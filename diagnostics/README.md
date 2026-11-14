# Ambient-context diagnostic

This directory is a **diagnostic tool**, not product code. It answers one
question: *does the host process's ambient state — mainly the thread-local
`decimal` context, but also `TZ` and `PYTHONHASHSEED` — change what Assay Batch
Insights computes or decides?*

It never edits the product, never changes a threshold or rounding rule, and
never writes into the frozen inputs.

## Run it

```sh
python3 -m diagnostics.cli --workdir diagnostics-run --stress-old-tests
```

Outputs, all under `--workdir`:

| Path | Contents |
| --- | --- |
| `report.md` | Human-readable root-cause, per-entry-point tables, hand-checkable fixtures, minimal commands |
| `evidence.md` | Per-trial appendix: actual context, exit code, deepest in-project exception frame, artifact digest summary |
| `results.json` | Machine-readable trial matrix with stage-by-stage comparisons and classifications |
| `old-tests-stress.json` | Repeated fork-ordering test under CPU load (with `--stress-old-tests`) |
| `records/<trial-id>/spec.json` | Exactly what the trial asked for |
| `records/<trial-id>/record.json` | Command, actual context, stages, exit status, exceptions, artifact hashes |
| `freeze/FROZEN.json` | SHA-256 of every frozen input, fixture, example package and control DB |
| `freeze/fixtures/` | Designed fixtures + `expected.json` exact oracles |
| `records/output/` | Per-trial reports/databases (isolation) |

`--select token,token` restricts the run to trial ids containing a token.
A partial run is marked as such in `results.json` (`selected_trials` vs
`planned_trials`). `--stress-old-tests` additionally repeats the shipped
fork-ordering test 120 times under CPU load; it is omitted from partial runs.

## How isolation works

* Every trial is a fresh `python3 diagnostics/trial.py` subprocess.
* Exactly one factor is applied per trial: `prec`, `rounding`, one `traps`
  entry, `TZ`, or `PYTHONHASHSEED`.
* The applied context is read back from the live interpreter
  (`context_actual`). If it does not match the request, the trial is labelled
  **evidence insufficient**, never **no effect**.
* Nested CLI runs (the real `python -m assay_insights`, the real
  `python -m assay_insights.acquire`) get their context from the committed
  `diagnostics/shim/sitecustomize.py` discovered via `PYTHONPATH` and the
  `ASSAY_DIAG_CONTEXT` JSON variable. No product file is touched.
* Each trial has its own output directory and its own warehouse database copy,
  so trials cannot see each other's writes.
* Observation of unrounded values happens by wrapping the product's own helpers
  (`input.load`, `metrics._statistics`) at runtime and restoring them in
  `finally`. Values are recorded exactly (no rounding) at the call boundary.
* The pty acquisition scenario always closes both descriptor ends and reaps the
  child process group (SIGTERM then SIGKILL) in `finally`; a blocked instrument
  can never leave a stray process behind.

## Classification

| Class | Meaning |
| --- | --- |
| `guard_weakened` | Control rejects/aborts but the perturbed run accepts or publishes |
| `decision_change` | Reportability, QC pass/fail, publish/validate, or exit status changed |
| `exception` | A call aborted where the control completed (or aborted differently) |
| `numeric_drift` | Same ruling, different unrounded/displayed value |
| `no_effect` | Factor applied and read back, yet nothing moved |
| `evidence_insufficient` | Factor could not be proven applied, or the harness failed |

## Fixtures

`fixtures.py` builds small CSV inputs and an `expected.json` computed with
exact rational arithmetic (`fractions.Fraction`) and a 90-digit square root
cross-checked against the exact variance. The product's arithmetic is never used
to derive an expectation, so the oracle is independent.

```sh
python3 -I diagnostics/fixtures.py --out /tmp/assay-fixtures
```

`python3 -I` is deliberate: fixtures are pure data and must not import anything
from the working directory.

## Re-running one claim without this harness

The report's `## Minimal commands` section lists standalone commands using only
`diagnostics/shim`, `diagnostics/fixtures.py` and
`diagnostics/acquire_replay.py`. They do not need `results.json`.

```sh
python3 -I diagnostics/fixtures.py --out /tmp/assay-fixtures
ASSAY_DIAG_CONTEXT='{"prec": 8}' PYTHONPATH="$PWD/diagnostics/shim:$PWD" \
  python3 -m assay_insights \
  --calibrations /tmp/assay-fixtures/nonterm-legacy/calibrations.csv \
  --results /tmp/assay-fixtures/nonterm-legacy/results.csv \
  --policy /tmp/assay-fixtures/nonterm-legacy/policy.json \
  --output /tmp/assay-prec8
```

## Limits of what this can prove

A trial shows that a factor *did* change an outcome in *that* scenario and
fixture. It does not prove the reverse ("this factor can never matter"), and a
`no_effect` row is evidence about the tested data only. `evidence_insufficient`
is used when the factor could not be read back from the live process. See the
report's "Boundary of the conclusions" paragraph for the untested factors.
