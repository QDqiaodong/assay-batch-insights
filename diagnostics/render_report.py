"""Render a diagnostic ``results.json`` into a self-contained Markdown report.

The report is written for an engineer who needs to re-run a single claim without
the diagnostic harness. Every row names the factor, the first divergent call
boundary, the propagation chain, and whether a *decision* (reportability, QC
pass/fail, publish/validation, exit status) changed. Numeric drift is reported
separately from exceptions and from "no effect", so a difference is never
silently relabelled a defect.

Categories
----------
decision_change
    A ruling changed: reportable/reasons, QC pass/fail, import commit/reject,
    publish+validation, or the CLI exit status.
exception
    A call aborted (raised, was missing, or was rejected) where the control
    completed, or the two aborted with different exception types/locations.
numeric_drift
    Decisions are identical but an exact/displayed value differs.
no_effect
    The factor was applied and read back, yet no product value or decision moved.
evidence_insufficient
    The harness could not prove the factor took effect, or the trial itself
    failed to run. Not evidence either way.
"""
import json

CLASSIFICATIONS = [
    ("guard_weakened", "Guard weakened (control rejects, trial accepts)"),
    ("decision_change", "Decision change"),
    ("exception", "Exception / abort"),
    ("numeric_drift", "Numeric drift (no decision change)"),
    ("no_effect", "No effect"),
    ("evidence_insufficient", "Evidence insufficient"),
    ("control", "Control"),
]

ROOT_CAUSE = """\
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
"""


def _pct(value):
    return "—" if value is None else f"{value}"


def _factor_text(factor, environment):
    if not factor:
        return "default"
    if factor.get("kind") == "env":
        return f'`{factor.get("name")}={factor.get("value")}`'
    parts = []
    if "prec" in factor:
        parts.append(f"prec={factor['prec']}")
    if "rounding" in factor:
        parts.append(f"rounding={factor['rounding']}")
    for name in factor.get("traps", []):
        parts.append(f"trap {name}")
    if factor.get("label") == "control" or not parts:
        return "default (prec 28, ROUND_HALF_EVEN)"
    return ", ".join(parts)


def _first_change(entry):
    changes = []
    for divergence in entry.get("comparison", {}).get("divergences", []):
        if divergence.get("kind") == "values":
            for change in divergence.get("changes", [])[:2]:
                changes.append(f"`{change['path']}`: {_short(change['control'])} → "
                               f"{_short(change['trial'])}")
        elif divergence.get("kind") in ("status", "missing", "extra"):
            changes.append(f"stage `{divergence['stage']}`: "
                           f"{divergence.get('control', 'ok')} → "
                           f"{divergence.get('trial', 'absent')}")
        elif divergence.get("kind") == "exception":
            control = (divergence.get("control") or {}).get("message", "")
            trial = (divergence.get("trial") or {}).get("message", "")
            changes.append(f"`{divergence['stage']}`: {_short(control)} → {_short(trial)}")
    return "; ".join(changes) if changes else "—"


def _short(value, limit=72):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _table(rows, headers):
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _scenario_trials(results, scenario_name):
    return [(key, value) for key, value in results["scenarios"].items()
            if key.startswith(scenario_name + "::")]


def _ordered(entries):
    order = {name: index for index, (name, _) in enumerate(CLASSIFICATIONS)}
    return sorted(entries, key=lambda item: (order.get(item[1], 99), item[0]))


def _counts(entries):
    counts = {}
    for _, classification in entries:
        counts[classification] = counts.get(classification, 0) + 1
    return counts


def render_evidence(results):
    """Full per-trial evidence table: context, exit code, exception, artifacts."""
    lines = ["# Per-trial evidence appendix", "",
             "One row per executed trial. `Context` is read back from the live "
             "trial process, not copied from the request. `Exception` names the "
             "deepest in-project frame. `Artifact` is the digest summary the trial "
             "produced (report JSON/HTML, warehouse ledger outcome, or the "
             "acquisition summary and independent default-context validation).",
             ""]
    rows = []
    for trial_id in sorted(results["trials"]):
        entry = results["trials"][trial_id]
        context = entry.get("context_actual") or {}
        traps = ",".join(context.get("traps") or [])
        outcome = entry.get("outcome") or {}
        exception = outcome.get("exception")
        location = "—"
        if exception and exception.get("type"):
            if exception.get("file"):
                location = (f"`{exception.get('file')}:{exception.get('line')}` "
                            f"{_short(exception.get('type'), 70)}")
                if exception.get("origin_file"):
                    location += (f" (origin `{exception['origin_file']}:"
                                 f"{exception['origin_line']}`)")
            else:
                # Nested process: the location comes from its stderr traceback.
                location = _short(exception.get("type"), 90)
        artifacts = outcome.get("artifact_summary") or {}
        summary = _artifact_summary(artifacts)
        environment = entry.get("environment") or {}
        env_text = ", ".join(
            f"{key}={value}" for key, value in sorted(environment.items())
            if key != "python")
        rows.append([
            f"`{trial_id}`",
            f"prec {context.get('prec')} / {context.get('rounding')} / "
            f"[{traps or '—'}]",
            env_text or "—",
            str(outcome.get("exit_code") if outcome.get("exit_code") is not None else "—"),
            location,
            summary,
            f"`{entry.get('classification')}`",
        ])
    lines.append(_table(rows, ["Trial", "Context (actual)", "Environment",
                               "Exit", "Exception location", "Artifact summary",
                               "Class"]))
    lines.append("")
    return "\n".join(lines)


def _artifact_summary(artifacts):
    if not artifacts:
        return "—"
    parts = []
    for name, value in sorted(artifacts.items()):
        if not isinstance(value, dict):
            parts.append(f"{name}={_short(value, 40)}")
            continue
        if name == "artifact":
            published = value.get("published")
            if published is not None:
                parts.append(f"published={published}")
            for key, item in sorted(value.items()):
                if isinstance(item, dict) and item.get("sha256"):
                    parts.append(f"{key}={item['sha256'][:12]}")
            error = value.get("staging_error_json")
            if error:
                parts.append(f"staging_error={error.get('code')}")
        elif name == "default_validation":
            parts.append(f"default_validation={value.get('status')}")
        elif name == "instrument":
            parts.append(f"instrument={_short(value.get('outcome'), 44)}")
        else:
            parts.append(f"{name}={_short(value, 44)}")
    return "; ".join(parts) if parts else "—"


def render(results):
    manifest = results["manifest"]
    trials = results["trials"]
    lines = []
    add = lines.append

    add("# Host Decimal-context diagnostic — Assay Batch Insights")
    add("")
    add("Diagnostic only: the product was **not** modified, no business rule was "
        "changed, and no fix is proposed here. Every statement below is backed by "
        "a trial record under `records/<trial-id>/record.json`.")
    add("")
    add("## How the evidence was produced")
    add("")
    add(f"* Python {manifest['python']}; frozen input hashes are in "
        f"`freeze/FROZEN.json` (inputs, fixtures and warehouse example package).")
    add("* Every trial ran in a **new subprocess**. Control and perturbed runs use "
        "the same scenario code; the control is simply the trial whose factor is "
        "the interpreter default.")
    add(f"* Control environment: `TZ={manifest['control_environment']['TZ']}`, "
        f"`PYTHONHASHSEED={manifest['control_environment']['PYTHONHASHSEED']}`. "
        "`TZ` and hash seed were varied separately and never combined with a "
        "decimal change, so no causal claim mixes factors.")
    add("* Each trial changed **exactly one** factor: precision, rounding, one "
        "decimal trap, `TZ`, or `PYTHONHASHSEED`. Decimal contexts were applied in "
        "the trial process (and, for nested CLIs, through the committed "
        "`diagnostics/shim/sitecustomize.py` plus `ASSAY_DIAG_CONTEXT`); the "
        "applied context is read back and recorded, so a factor that failed to "
        "take effect is reported as *evidence insufficient*, never as *no effect*.")
    add("* Trials are isolated: each has its own output directory and (for the "
        "warehouse) its own database copy. Frozen inputs, fixtures and package are "
        "only ever copied, never written.")
    add("* For every trial the record stores: command, actual context, environment, "
        "exit status / raised exception with the deepest in-project frame, and the "
        "artifact digests. Observation is by wrapping the product's own functions "
        "at runtime (restored in `finally`), never by editing them.")
    add("")

    add(ROOT_CAUSE)

    # ---- headline -------------------------------------------------------- #
    entries = [(trial_id, entry.get("classification", "evidence_insufficient"))
               for trial_id, entry in trials.items()]
    counts = _counts(entries)
    add("## Headline")
    add("")
    rows = []
    for name, title in CLASSIFICATIONS:
        if name == "control":
            continue
        affected = sorted({entry["scenario"] for trial_id, entry in trials.items()
                           if entry.get("classification") == name})
        fixtures = sorted({entry["fixture"] for trial_id, entry in trials.items()
                           if entry.get("classification") == name and entry.get("fixture")})
        rows.append([title, str(counts.get(name, 0)),
                     ", ".join(affected) if affected else "—",
                     ", ".join(fixtures) if fixtures else "—"])
    add(_table(rows, ["Outcome", "Trials", "Scenarios affected", "Fixtures"]))
    add("")


    # ---- affected entry points / boundaries ------------------------------ #
    add(_affected_section(results))

    # ---- per-scenario detail --------------------------------------------- #
    add("## Per-scenario results")
    add("")
    for scenario in ["analyze", "run", "cli", "validate-report",
                     "warehouse-import", "warehouse-query", "acquire"]:
        blocks = _scenario_trials(results, scenario)
        if not blocks:
            continue
        add(f"### `{scenario}`")
        add("")
        for key, block in sorted(blocks):
            add(_scenario_block(key, block, trials, manifest))
            add("")

    # ---- fixtures -------------------------------------------------------- #
    add(_fixture_section(manifest))

    # ---- controlled wrappers --------------------------------------------- #
    add(_wrapper_section(results))

    # ---- conclusions ----------------------------------------------------- #
    add(_conclusions(results, counts))

    # ---- reproduction ---------------------------------------------------- #
    add(_reproduction(manifest))

    # ---- old tests ------------------------------------------------------- #
    add(_old_tests(results))
    add("")

    add("## Where the full evidence lives")
    add("")
    add("`evidence.md` is a generated appendix with one row per executed trial: "
        "the context read back from the trial process, the environment, the exit "
        "code, the deepest in-project exception frame and an artifact digest "
        "summary. `results.json` carries the same data plus the stage-by-stage "
        "comparison and classification. The raw per-trial `spec.json`/"
        "`record.json` pairs and the trial-private outputs remain under "
        "`records/`.")
    add("")

    return "\n".join(lines).rstrip() + "\n"


def _scenario_block(key, block, trials, manifest):
    control_id = block.get("control")
    if control_id is None:
        return f"**`{key}`** — control not selected in this run.\n"
    control = trials.get(control_id, {})
    rows = []
    for trial_id in sorted(block["trials"]):
        entry = block["trials"][trial_id]
        record = trials.get(trial_id, {})
        factor = record.get("factor") or {}
        rows.append([
            "control" if entry["classification"] == "control" else _factor_text(factor, {}),
            f"`{entry['classification']}`",
            f"`{(entry.get('comparison') or {}).get('first_divergence')}`",
            " ".join(f"`{stage}`" for stage in
                     (entry.get("comparison") or {}).get("propagation_chain") or []),
            "yes" if (entry.get("comparison") or {}).get("decision_changed") else "no",
            _first_change(entry) if entry["classification"] not in ("control",) else "—",
        ])
    body = _table(rows, ["Factor", "Class", "First divergence", "Chain",
                         "Decision changed", "First observed change"])
    control_context = control.get("context_actual") or {}
    header = (f"**`{key}`** — control `{control_id}` "
              f"(prec {control_context.get('prec')}, "
              f"{control_context.get('rounding')}).")
    aborted = block.get("control_aborted")
    detail = block.get("control_abort_detail") or []
    if aborted and detail:
        summary = "; ".join(
            f"`{item['stage']}` {item['type']}: {_short(item.get('message') or '', 150)}"
            for item in detail)
        header += ("\n\n> **Control baseline already aborts**, so this block's "
                   "control is a *failing* baseline: " + summary +
                   ". Rows marked `no_effect` mean the perturbation changed "
                   "nothing relative to that failure; `guard_weakened` means the "
                   "perturbation accepted what the control rejected.")
    return header + "\n\n" + body


def _fixture_section(manifest):
    lines = ["## Designed fixtures and hand calculations", "",
             "Expectations in `freeze/fixtures/*/expected.json` were derived with "
             "exact rational arithmetic (`fractions.Fraction`) and a 90-digit "
             "square root cross-checked against the exact variance. The product's "
             "arithmetic was not used to derive them, so they are an independent "
             "oracle, not a recording of current behaviour.", ""]
    for name, digest in sorted(manifest.get("fixtures", {}).items()):
        if not name.endswith("expected.json"):
            continue
        fixture = name.split("/")[0]
        lines.append(f"* `{fixture}` — see `freeze/fixtures/{fixture}/expected.json`")
    lines.append("")
    lines.append("The hand calculations, and what the product must do with them:")
    lines.append("")
    lines.append(_table([
        ["`nonterm-legacy`",
         "10/3, 11/3",
         "mean 7/2; variance 1/18; SD sqrt(2)/6; RSD 6.734350297014738…",
         "sample RSD well under 10% → reportable; QC 100% → pass",
         "control"],
        ["`rsd-threshold-legacy`",
         "535355339059, 464644660941 (slope 1)",
         "mean 5e11; variance 2499999999953702010962; "
         "RSD 9.999999999907404…%",
         "RSD is **strictly below** 10% → reportable. Display rounds to "
         "10.000000, so the report cannot show why",
         "control"],
        ["`qc-boundary-legacy`",
         "QC 7.999999999, target 10",
         "recovery exactly 79.9999999%",
         "one part in 1e7 below the inclusive 80% → **fail**, though display "
         "rounds to 80.000000",
         "control"],
        ["`qc-boundary-timed`",
         "QC 35/3, 37/3, target 10",
         "mean exactly 12; recovery exactly 120%",
         "exactly on the inclusive upper limit → **pass**; a 1e-27 sum "
         "perturbation flips it",
         "decision flip under ROUND_UP"],
        ["`range-edge-legacy` / `-timed`",
         "signal 999999999999.6, dilution 1.0000001",
         "concentration 25000002499989999999/25000000 ≈ 1.0000000999996e12",
         "above the 1e12 acceptance bound → **load must reject**",
         "guard weakened at tested prec 1/2/4/6"],
        ["`clean-timed`",
         "10, 10; QC 10, target 5",
         "all exact: mean 5, SD 0, recovery 100%",
         "every fact exact in any context → must be reportable everywhere",
         "no effect under every factor"],
    ], ["Fixture", "Raw inputs", "Exact result", "Required behaviour",
        "Observed here"]))
    lines.append("")
    lines.append("`nonterm-legacy` is the anti-false-positive control: no ruling "
                 "is near a limit, so *any* decision difference there would mean "
                 "the diagnostic — not the product — was broken. None was seen. "
                 "`qc-boundary-timed` is the sharpest counter-example: the default "
                 "decision is correct *and* the exact value is on the boundary, so "
                 "a flipped ruling can only come from the context.")
    lines.append("")
    return "\n".join(lines)


def _wrapper_section(results):
    wrappers = results.get("wrappers") or {}
    lines = ["## Controlled drift / exception wrappers (detector validation)", "",
             "These trials keep the default decimal context and instead install a "
             "labelled wrapper around the product's own `_statistics` function. "
             "They exist only to prove the detector locates the right boundary "
             "and does not invent findings:", ""]
    if not wrappers:
        lines.append("*No wrapper trials were selected in this run.*")
        return "\n".join(lines)
    rows = []
    for label, entry in sorted(wrappers.items()):
        comparison = entry.get("comparison") or {}
        rows.append([
            f"`{label}`",
            f"`{entry.get('fixture')}`",
            f"`{entry.get('classification')}`",
            f"`{comparison.get('first_divergence')}`",
            "yes" if comparison.get("decision_changed") else "no",
            _first_change(entry),
        ])
    lines.append(_table(rows, ["Wrapper", "Fixture", "Class", "First divergence",
                               "Decision changed", "Observed"]))
    lines.append("")
    lines.append("A passthrough wrapper reports **no effect**; a drift that "
                 "crosses a limit reports **decision change** at `statistics`; a "
                 "thrown exception reports **exception** at the wrapped stage. "
                 "The detector is therefore shown to respond to real changes and "
                 "to stay silent when nothing changes.")
    return "\n".join(lines)


def _affected_section(results):
    """Explicit list of what is and is not affected, with per-entry scope."""
    lines = ["## Affected entry points and their exact boundary", ""]
    rows = [
        ["`input.load` (legacy + timed)", "yes",
         "concentration, 0..1e12 admission, target parsing",
         "tested `prec` 1, 2, 4, 6 admit an exact ≈1.0000001e12 concentration; "
         "traps on non-terminating division raise raw `decimal` errors"],
        ["`metrics.analyze` (legacy + timed)", "yes",
         "mean, variance, sqrt, RSD, recovery, reportability",
         "all statistics and every `>`/`<=` comparison; display rounding in "
         "`shown()` also inherits rounding/traps"],
        ["`report.run` / `python -m assay_insights`", "yes",
         "everything above plus JSON/HTML bytes", "artifact hashes change; "
         "decision changes only on boundary values; exit code 1 on traps"],
        ["`warehouse.reports.load_report`", "yes",
         "exact equality of `concentration_mg_l`; exact equality of re-run "
         "`samples`/`quality_controls`",
         "rejects default-context reports at `prec ≤ 8`; rejects any report "
         "with a non-terminating concentration at the default context"],
        ["`warehouse.import_manifest`", "yes (via verifier)",
         "commit/reject and knowledge_seq", "an import that should commit is "
         "rejected, so the ledger never advances"],
        ["`warehouse.build_query`", "no",
         "reads stored text only", "all query trials `no_effect`, including "
         "`TZ` and `PYTHONHASHSEED`"],
        ["`acquire` pre-publish gate", "yes",
         "publish/reject, exit code, REJ line, staging diagnostics",
         "`prec 1–6` publishes a CSV the default context rejects; `Inexact` "
         "trap bypasses the documented error path"],
        ["`acquire` frame protocol / CRC / locking", "no",
         "bytes, CRC32, flock", "no decimal arithmetic involved"],
    ]
    lines.append(_table(rows, ["Entry point", "Affected?", "What moves", "Observed"]))
    lines.append("")
    lines.append("Boundary statement, from the recorded first divergences. "
                 "For a precision change the first difference is always `load` "
                 "(the concentration text), because calibration is computed "
                 "there. For a rounding change it is also `load`. For a trap it "
                 "is the first operation that is inexact under that context: "
                 "`load` when the fixture's calibration actually divides "
                 "inexactly (`nonterm-*`, `qc-boundary-timed`), and `statistics` "
                 "when the calibration is exact but the mean/`sqrt` is not "
                 "(`qc-boundary-legacy`, `rsd-threshold-legacy`, both slope 1). "
                 "A terminating fixture (`clean-timed`) never raises and is "
                 "`no_effect` under every trap tried. The propagation chain is "
                 "`load → statistics → qc_recovery → analyze → artifact`; for "
                 "the warehouse verifier it is `validate` (which internally "
                 "re-runs `analyze`), and for acquisition `process → instrument "
                 "→ artifact → default_validation`.")
    lines.append("")
    return "\n".join(lines)


def _conclusions(results, counts):
    lines = ["## Conclusions and their boundaries", ""]
    lines.append("Findings are stated as observations with their scope; they are "
                 "not a claim that the product is wrong in general.")
    lines.append("")
    lines.append("1. **Every numeric entry point inherits the host decimal "
                 "context; none pins it.** This holds for `load`/`analyze`/`run`, "
                 "the warehouse import verifier, and the serial acquirer's "
                 "pre-publish validation. Evidence: the precision/rounding/trap "
                 "rows above, plus the independent `rc`/first-divergence stages.")
    lines.append("2. **Two independent verification mechanisms reject reports "
                 "produced under another context.** (i) `_check_measurements` "
                 "recomputes each `concentration_mg_l` and requires exact equality "
                 "with the six-decimal text, so a non-terminating concentration "
                 "(10/3 → `3.333333`) is rejected even at the **default** context "
                 "— the shipped `examples/warehouse/reports/2026-03-03-evening/"
                 "report.json` contains no such value, but a report from the "
                 "`nonterm-timed` fixture legitimately does. (ii) `_validate_v2` "
                 "re-runs `metrics.analyze` and compares the `samples` section "
                 "exactly; that is what fails for the shipped example when the "
                 "importer runs at `prec ≤ 8` (verified: prec 9–28 accept, "
                 "prec 8 rejects with `report section samples is inconsistent "
                 "with its per-measurement facts`).")
    lines.append("3. **Decision flips need a value *on* a boundary — and the "
                 "boundary cases are real, not contrived.** Three independent "
                 "flips were observed with their unrounded values captured at the "
                 "call boundary: `qc-boundary-timed` recovery goes from "
                 "`120.0000000000000000000000000` to `120.0000000000000000000000001` "
                 "under `ROUND_UP` and QC flips pass→fail; `qc-boundary-legacy` "
                 "recovery `79.9999999900` rounds to `80.000000` at `prec ≤ 8` and "
                 "the sample becomes reportable; `rsd-threshold-legacy` RSD "
                 "`9.999999999907…` jumps to `11`/`10.01` at `prec 2`/`4` and the "
                 "sample becomes unreportable. Away from boundaries the identical "
                 "perturbations move only the last displayed digit.")
    lines.append("4. **Same-process leakage and cross-context verification are separable.** "
                 "(a) Leakage: `input.load` accepts a concentration whose exact "
                 "value is `25000002499989999999/25000000` mg/L "
                 "(≈1.0000000999996e12, above the 1e12 bound) at the tested "
                 "`prec` values 1, 2, 4 and 6, because the multiply rounds it to "
                 "exactly `1e12`; the identical fixture is rejected at the "
                 "default context and at `prec 8`. `metrics.analyze` and the "
                 "acquirer then consume that accepted value. (b) Cross-context: a "
                 "report produced under the default context is rejected when "
                 "validated at `prec ≤ 8` or under a different rounding mode, "
                 "although the report faithfully records the producer's rulings.")
    lines.append("5. **The serial acquirer couples host context to publication.** "
                 "Its pre-publish `load` gate runs in the acquirer process: under "
                 "`prec=2` the `range-edge-timed` run is published (exit 0) with a "
                 "CSV the default context rejects (verified independently with a "
                 "clean `PYTHONPATH`). Under `trap=Inexact` on `nonterm-timed`, "
                 "the `load` gate raises a raw `decimal.Inexact` instead of the "
                 "`ValueError` its own handler expects: the run is *not* "
                 "published, no `REJ` line is sent (the instrument times out "
                 "waiting for a reply), the CLI exits 1 rather than the documented "
                 "2, and the staging directory is left without `error.json` "
                 "(the exception type bypasses the failure-diagnostic path).")
    lines.append("6. **Read-only warehouse queries are immune.** They only render "
                 "stored text; no decimal is recomputed. All query trials, "
                 "including `TZ` and hash-seed variations, are `no_effect`.")
    lines.append("7. **`TZ` and `PYTHONHASHSEED` had no effect** in any trial. "
                 "Times are parsed to absolute UTC by `input.parse_time` and no "
                 "output depends on set/dict iteration order (all sorts are "
                 "explicit). This is an *observed in these trials* statement, not "
                 "a proof for every possible environment. Note the contrast with "
                 "the old suite: its fork-ordering flake (see the old-test "
                 "section) is scheduling-dependent, while `PYTHONHASHSEED` had no "
                 "effect in any product trial.")
    lines.append("")
    lines.append("**Boundary of the conclusions.** The observations cover the "
                 "scenarios and factors enumerated in this report, run on the "
                 "frozen inputs and hashes listed in `freeze/FROZEN.json`. "
                 "Precision values not in the matrix, other trap combinations, "
                 "other rounding modes (`ROUND_CEILING`/`FLOOR`/`05UP`/`HALF_UP`/"
                 "`HALF_DOWN`), `Emin`/`Emax`/`clamp` changes, multi-threaded "
                 "callers, and a context mutated *between* calls inside one "
                 "request were not exhaustively tested. `trap=Underflow`/`Clamped` "
                 "and `FloatOperation` are untested. The report does not decide "
                 "which entry point *should* own the context, and it does not "
                 "change any threshold or rounding rule.")
    lines.append("")
    lines.append("**Not defects (do not file as bugs based on this report).** "
                 "`qc-boundary-legacy` (recovery 79.9999999% displays as "
                 "80.000000 but correctly fails the unrounded inclusive test) and "
                 "`rsd-threshold-legacy` (exact RSD 9.9999999999…% displays as "
                 "10.000000) are display/precision gaps in the shipped decision "
                 "rule, present at the default context. They are reported as "
                 "such, not as context defects.")
    lines.append("")
    return "\n".join(lines)


def _reproduction(manifest):
    lines = ["## Minimal commands, independent of the harness", ""]
    lines.append("These use only the committed `diagnostics/` helpers. Each is a "
                 "standalone command with its evidence printed inline; none needs "
                 "`results.json`.")
    lines.append("")
    lines.append("**1. Exact fixture oracle (no harness, no product arithmetic):**")
    lines.append("")
    lines.append("```sh")
    lines.append("python3 -I diagnostics/fixtures.py --out /tmp/assay-fixtures")
    lines.append("python3 - <<'PY'")
    lines.append("import json; e=json.load(open('/tmp/assay-fixtures/index.json'))")
    lines.append("for name, spec in e.items():")
    lines.append("    print(name, spec['load']['accepted_at_default_context'],")
    lines.append("          {k: v.get('passed_at_default_context') for k, v in spec['groups'].items() if v['kind']=='QC'})")
    lines.append("PY")
    lines.append("```")
    lines.append("")
    lines.append("**2. One perturbed report run, by hand (precision 8):**")
    lines.append("")
    lines.append("```sh")
    lines.append("ASSAY_DIAG_CONTEXT='{\"prec\": 8}' \\")
    lines.append("PYTHONPATH=\"$PWD/diagnostics/shim:$PWD\" \\")
    lines.append("python3 -m assay_insights \\")
    lines.append("  --calibrations /tmp/assay-fixtures/nonterm-legacy/calibrations.csv \\")
    lines.append("  --results /tmp/assay-fixtures/nonterm-legacy/results.csv \\")
    lines.append("  --policy /tmp/assay-fixtures/nonterm-legacy/policy.json \\")
    lines.append("  --output /tmp/assay-prec8")
    lines.append("python3 -c \"import json;r=json.load(open('/tmp/assay-prec8/report.json'));"
                 "print([(s['sample'],s['mean_mg_l'],s['rsd_pct']) for s in r['samples']])\"")
    lines.append("```")
    lines.append("")
    lines.append("Compare with the same command without `ASSAY_DIAG_CONTEXT`.")
    lines.append("")
    lines.append("**3. Warehouse verifier disagreement, by hand:**")
    lines.append("")
    lines.append("```sh")
    lines.append("ASSAY_DIAG_CONTEXT='{\"prec\": 8}' \\")
    lines.append("PYTHONPATH=\"$PWD/diagnostics/shim:$PWD\" \\")
    lines.append("python3 -m assay_insights.warehouse --db /tmp/assay-wh.sqlite \\")
    lines.append("  --manifest examples/warehouse/IMP-002-timed.json")
    lines.append("# default context: exit 0; prec 8: exit 2, "
                 "'report section samples is inconsistent with its per-measurement facts'")
    lines.append("```")
    lines.append("")
    lines.append("**4. QC boundary at exact 120%, by hand:**")
    lines.append("")
    lines.append("```sh")
    lines.append("ASSAY_DIAG_CONTEXT='{\"rounding\": \"ROUND_UP\"}' \\")
    lines.append("PYTHONPATH=\"$PWD/diagnostics/shim:$PWD\" \\")
    lines.append("python3 -m assay_insights \\")
    lines.append("  --calibrations /tmp/assay-fixtures/qc-boundary-timed/calibrations.csv \\")
    lines.append("  --results /tmp/assay-fixtures/qc-boundary-timed/results.csv \\")
    lines.append("  --policy /tmp/assay-fixtures/qc-boundary-timed/policy.json \\")
    lines.append("  --output /tmp/assay-qc")
    lines.append("# QC recovery 120% passes under the default context and fails under ROUND_UP")
    lines.append("```")
    lines.append("")
    lines.append("**5. Acquisition pre-publish gate, by hand:**")
    lines.append("")
    lines.append("```sh")
    lines.append("# Control: the acquirer rejects the staged run before publishing (exit 2).")
    lines.append("python3 diagnostics/acquire_replay.py \\")
    lines.append("  --results /tmp/assay-fixtures/range-edge-timed/results.csv \\")
    lines.append("  --calibrations /tmp/assay-fixtures/range-edge-timed/calibrations.csv \\")
    lines.append("  --policy /tmp/assay-fixtures/range-edge-timed/policy.json \\")
    lines.append("  --output /tmp/assay-acq-control")
    lines.append("")
    lines.append("# prec 2: the guard is weakened; the run is published (exit 0),")
    lines.append("# while a clean default-context subprocess rejects the published CSV.")
    lines.append("python3 diagnostics/acquire_replay.py \\")
    lines.append("  --results /tmp/assay-fixtures/range-edge-timed/results.csv \\")
    lines.append("  --calibrations /tmp/assay-fixtures/range-edge-timed/calibrations.csv \\")
    lines.append("  --policy /tmp/assay-fixtures/range-edge-timed/policy.json \\")
    lines.append("  --output /tmp/assay-acq-prec2 --context '{\"prec\": 2}'")
    lines.append("")
    lines.append("# trap Inexact: the gate raises decimal.Inexact (exit 1, no REJ")
    lines.append("# line, instrument times out, staging left without error.json).")
    lines.append("python3 diagnostics/acquire_replay.py \\")
    lines.append("  --results /tmp/assay-fixtures/nonterm-timed/results.csv \\")
    lines.append("  --calibrations /tmp/assay-fixtures/nonterm-timed/calibrations.csv \\")
    lines.append("  --policy /tmp/assay-fixtures/nonterm-timed/policy.json \\")
    lines.append("  --output /tmp/assay-acq-inexact --context '{\"traps\": [\"Inexact\"]}'")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _old_tests(results):
    old = results.get("old_tests")
    lines = ["## Old test suite (real run, this environment)", ""]
    if not old:
        lines.append("Not recorded in this run.")
        return "\n".join(lines)
    lines.append(f"Command: `{old.get('command')}`")
    lines.append("")
    runs = old.get("runs") or []
    if runs:
        rows = []
        for index, run in enumerate(runs, start=1):
            rows.append([str(index), run.get("status", "?"), str(run.get("ran")),
                         str(run.get("exit_code")), str(run.get("duration_s"))])
        lines.append(_table(rows, ["Run", "Status", "Tests", "Exit", "Seconds"]))
        lines.append("")
        failures = [run for run in runs if run.get("status") != "pass"]
        for run in failures:
            lines.append(f"Run with status `{run.get('status')}` tail:")
            lines.append("")
            lines.append("```text")
            lines.extend(run.get("tail") or [])
            lines.append("```")
            lines.append("")
    else:
        lines.append(f"Result: **{old.get('status')}**, {old.get('ran')} tests, "
                     f"exit {old.get('exit_code')}, {old.get('duration_s')} s.")
        lines.append("")
    stress = results.get("old_tests_stress")
    if stress:
        lines.append("### Repeated fork-ordering test under CPU load")
        lines.append("")
        lines.append(f"`{stress['command']}` repeated **{stress['iterations']}** "
                     f"times with {stress['load_processes']} busy-loop processes "
                     f"({stress['context']}): **{stress['failures']} failures**, "
                     f"{stress['duration_s']} s.")
        lines.append("")
        for failure in stress.get("failure_details", []):
            lines.append(f"* Iteration {failure['iteration']}: "
                         f"`{_short(failure['assertion'] or '', 200)}`")
        lines.append("")
        lines.append(f"Conclusion recorded by the harness: {stress['conclusion']}")
        lines.append("")
        lines.append("This is reported as a **pre-existing test-suite defect "
                     "(an ordering assumption), not a decimal-context effect**: "
                     "no decimal factor was applied, `TZ` was fixed to UTC, and "
                     "the failure is a legitimate reversed ledger order rather "
                     "than a wrong sequence.")
        lines.append("")
    lines.append("Interpretation: the suite is a same-context regression suite. "
                 "It never changes the decimal context, `TZ` or the hash seed, so "
                 "it cannot detect the context inheritance described above. Any "
                 "failure it does show is unrelated to the factor matrix; it is "
                 "reported verbatim rather than dismissed. The concurrency tests "
                 "fork two processes and assert a scheduling-dependent ordering, "
                 "so repeated runs are recorded to distinguish a flake from a "
                 "deterministic failure.")
    return "\n".join(lines)
