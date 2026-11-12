# Instrument and metric contract v1 (legacy) and v2 (time-selected calibration)

All CSVs have exact ordered headers shown in data/. Format is chosen from the
complete header. Calibrations and results must use the same generation; a
legacy calibration with timed results (or the reverse) is a contract error.
Legacy inputs keep producing schema_version 1 reports with unchanged fields,
numbers and rejection reasons.

## Legacy format (v1, unchanged)

Calibrations: `analyte,slope,intercept,unit`. Results:
`batch,sample,analyte,replicate,kind,signal,dilution,target_mg_l`.

Identifiers must be nonempty. Calibration is one row per analyte:
signal = slope * concentration + intercept; slope > 0, output unit exactly
mg/L. Results are keyed by batch, sample, analyte, replicate. kind is sample
or QC, dilution > 0. Negative calibrated concentrations are rejected;
detection limits/censored values are not modeled. Decimals must be finite with
absolute input value <=1e12; nonzero input magnitude is >=1e-12. Calibrated
concentrations must remain in [0,1e12] mg/L. Samples have blank target; QC has a
positive target_mg_l, consistent within its group. QC/sample identities cannot
mix.

## Timed format (v2)

Calibrations, in this exact order:
`analyte,calibration_id,valid_from,valid_to,slope,intercept,unit`.
Results append one column to the legacy header:
`batch,sample,analyte,replicate,kind,signal,dilution,target_mg_l,measured_at`.

policy.json is the same v1 contract for both generations.

### Times

Times must be RFC 3339 with an explicit UTC offset or `Z`; a time without a
zone is rejected. Seconds are mandatory; fractional seconds may have one to six
digits (microseconds). `t`/`z` case variants are accepted. Values are
normalized to an absolute UTC instant for every comparison, so the machine's
local timezone never participates. Reports render normalized times as
`YYYY-MM-DDTHH:MM:SS.ffffffZ`.

### Calibration versions

`calibration_id` is unique within one analyte (it may repeat across analytes).
`valid_from` is required. `valid_to` may be blank, meaning no upper bound;
otherwise it must be later than the start. Intervals are left-closed,
right-open: `[valid_from, valid_to)`. Within an analyte, intervals may be
adjacent or leave gaps but must not overlap — checked after timezone
normalization. Each measurement must fall in exactly one version. Missing
coverage fails the whole input; there is no newest/latest fallback.

The measurement identity for duplicate detection is still batch/sample/analyte/
replicate. Adding a version or a timestamp does not allow a second record for
the same identity. Concentration = (signal - intercept) / slope * dilution using
the selected version. All existing numeric, unit and target validation still
applies.

### Statistics, mixed versions, QC scope

Statistics are unchanged: arithmetic mean, n-1 sample SD, RSD = SD/mean*100,
unrounded Decimal decisions, six-decimal display. Ordinary samples still
aggregate by batch/analyte/sample; calibration versions never split an ordinary
sample into several samples. A sample whose replicates used more than one
`calibration_id` keeps its descriptive statistics but is unreportable with the
stable reason `mixed_calibration_versions`, even when the computed
concentrations are identical and the RSD passes. Existing replicate-count,
zero-mean and RSD rules apply independently.

QC identity is still batch/analyte/sample, and all QC rows in that identity
must share one target even when they span versions. Within the identity, QC is
sub-grouped by `calibration_id`; each subgroup gets its own mean and recovery.
QC eligibility is scoped to batch/analyte/calibration_id: a scope qualifies
only if at least one QC subgroup exists and every subgroup passes. Recovery
limits remain inclusive and QC still ignores ordinary sample minimum
replicates/RSD. Every version an ordinary sample used is checked against its
own scope; missing or failed scopes are listed per version (`missing_qc:<id>`,
`qc_failed:<id>`), and a cross-version sample also keeps
`mixed_calibration_versions`. One version's QC cannot cover another version's
missing QC, and one version's failure cannot block samples using another
version. No QC bracketing or time-window rule is added.

### Reports

Schema 1 (legacy) is unchanged. Schema 2 additionally provides:

- `calibrations`: analyte, calibration_id, normalized interval, slope,
  intercept, unit.
- `measurements`: every original identity field, normalized `measured_at`,
  selected `calibration_id`, effective `valid_from`/`valid_to`, actual
  slope/intercept/dilution and `concentration_mg_l` (plus the QC target).
- `quality_controls`: QC subgroups with calibration_id, target, mean and
  recovery.
- `qc_scopes`: eligibility per batch/analyte/calibration_id with status
  (`passed`, `failed`, `missing`) and failing control names.
- `samples`: used `calibration_ids`, per-version `qc_checks`, statistics,
  `reportable` and the concrete `reasons`.

Reports retain SHA256 provenance for all three inputs. Re-running identical
inputs yields byte-identical JSON and HTML; no current time or other unstable
content is included. HTML shows the same version selection, QC scopes and
failure reasons and escapes external identifiers. Single-process report
generation still replaces report.json/report.html on repeat; simultaneous
writers and crash-consistent multi-file publication are outside the contract.

Contract errors (overlap, invalid time, missing coverage, mixed formats and all
prior numeric/identity errors) are validated before the output directory is
touched: the CLI exits non-zero with the input file and record reason, and any
existing valid reports remain unchanged. QC failures and mixed-version samples
are normal analysis results: the CLI succeeds and records the unreportable
reasons. After fixing bad input, rerunning into the same output directory is
supported.
