# Instrument and metric contract v1

All CSVs have exact ordered headers shown in data/. Identifiers must be nonempty.
Calibration is one row per analyte: signal = slope * concentration + intercept;
slope > 0, output unit exactly mg/L. Results are keyed by batch, sample, analyte,
replicate. kind is sample or QC, dilution > 0. Negative calibrated concentrations
are rejected; detection limits/censored values are not modeled. Decimals must be
finite with absolute input value <=1e12; nonzero input magnitude is >=1e-12.
Calibrated concentrations must remain in [0,1e12] mg/L. Samples have blank target; QC has a
positive target_mg_l, consistent within its group. QC/sample identities cannot mix.

Concentration = (signal - intercept) / slope * dilution. Mean is arithmetic;
sample standard deviation uses n-1; RSD = SD / mean * 100. Ordinary groups require
policy.min_replicates and RSD <= policy.max_rsd_pct. Zero mean leaves RSD undefined
and makes the sample unreportable, even if all replicates are zero.
QC recovery = mean calibrated concentration / target * 100. Limits are inclusive.
QC is scoped to (batch, analyte). Every QC group in that scope must pass and at
least one must exist before ordinary samples can be reportable. A failed sample
RSD does not invalidate other samples in the batch. QC uses recovery, not the
ordinary sample replicate-count/RSD rule.

Decisions use unrounded Decimal arithmetic (Python default 28-digit precision);
JSON values render six decimal places. No uncertainty model or regulatory
compliance is claimed. policy.json is a synthetic demonstration policy, not an
external standard. No pooled statistics across batches or analytes.

JSON lists batch QC eligibility, QC recovery, every sample statistic and rejection
reason, source SHA256 hashes and applied policy. HTML displays sample and QC
results with escaped input identifiers. Single-process report generation replaces
report.json/report.html on repeat; simultaneous writers and crash-consistent
multi-file publication are outside the current contract.
