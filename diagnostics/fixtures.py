"""Deterministic synthetic fixtures with independent, exact hand calculations.

Every fixture is intentionally tiny.  Expectations are derived with
``fractions.Fraction`` (exact rational arithmetic) for means, targets and
recoveries.  Standard deviations involve one square root; it is evaluated at
90 significant digits and cross-checked against the exact squared variance.
The shipped analyzer's arithmetic is *not* used to derive an expectation, so a
fixture expectation is an independent oracle rather than a recording of the
implementation.

Usage::

    python diagnostics/fixtures.py --out DIR

Layout::

    DIR/<name>/calibrations.csv
    DIR/<name>/results.csv
    DIR/<name>/policy.json
    DIR/<name>/expected.json
    DIR/index.json
"""
import argparse
import json
import sys
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from fractions import Fraction
from pathlib import Path

POLICY = {
    "min_replicates": 2,
    "max_rsd_pct": "10",
    "qc_low_pct": "80",
    "qc_high_pct": "120",
}

LEGACY_CAL_HEADERS = ["analyte", "slope", "intercept", "unit"]
LEGACY_RESULT_HEADERS = ["batch", "sample", "analyte", "replicate", "kind",
                         "signal", "dilution", "target_mg_l"]
TIMED_CAL_HEADERS = ["analyte", "calibration_id", "valid_from", "valid_to",
                     "slope", "intercept", "unit"]
TIMED_RESULT_HEADERS = LEGACY_RESULT_HEADERS + ["measured_at"]

_SQRT_DIGITS = 90
_WORK_DIGITS = 60


def _dec(text):
    return Decimal(str(text))


def _frac_dec(value):
    """Exact Fraction rendered as a Decimal with _WORK_DIGITS digits."""
    with localcontext() as context:
        context.prec = _WORK_DIGITS
        context.rounding = ROUND_HALF_EVEN
        return Decimal(value.numerator) / Decimal(value.denominator)


def _plain(value):
    if value is None:
        return None
    return format(value, "f")


def _quantize_six(value):
    with localcontext() as context:
        context.prec = max(_WORK_DIGITS, value.adjusted() + 10)
        context.rounding = ROUND_HALF_EVEN
        return value.quantize(Decimal("0.000001"))


def _show(value):
    return format(_quantize_six(value), "f")


def _near_six_place_tie(value):
    """True when rounding ``value`` to 6 dp is within 1e-30 of a half-way point.

    Such values make the six-decimal display sensitive to a different rounding
    mode even when the underlying value is unchanged.
    """
    scaled = abs(value) * Decimal(10) ** 6
    fraction = scaled - scaled.to_integral_value(rounding="ROUND_FLOOR")
    return abs(fraction - Decimal("0.5")) < Decimal("1e-30")


def _fraction_text(value):
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _sqrt_fraction(value):
    """sqrt of a non-negative Fraction, cross-checked against the exact value."""
    if value == 0:
        return Decimal(0)
    with localcontext() as context:
        context.prec = _SQRT_DIGITS + 30
        context.rounding = ROUND_HALF_EVEN
        ratio = Decimal(value.numerator) / Decimal(value.denominator)
        root = ratio.sqrt()
        relative_error = abs(root * root / ratio - 1)
        if relative_error > Decimal(1).scaleb(-_SQRT_DIGITS + 10):
            raise AssertionError(f"square-root check failed for {value}")
        return +root


def _statistics(concentrations):
    n = len(concentrations)
    mean = sum(concentrations, Fraction(0)) / n
    variance = sd = rsd = None
    if n > 1:
        variance = sum((x - mean) ** 2 for x in concentrations) / (n - 1)
        sd = _sqrt_fraction(variance)
        mean_decimal = _frac_dec(mean)
        if mean_decimal != 0:
            rsd = sd / mean_decimal * 100
    return n, mean, variance, sd, rsd


def _decide_sample(concentrations, min_replicates, max_rsd):
    """Independent RSD/replicate decision from exact values only."""
    n = len(concentrations)
    mean = sum(concentrations, Fraction(0)) / n
    reasons = []
    if n < min_replicates:
        reasons.append("insufficient_replicates")
    if mean == 0:
        reasons.append("zero_mean_rsd_undefined")
    elif n > 1:
        variance = sum((x - mean) ** 2 for x in concentrations) / (n - 1)
        # compare RSD**2 with max_rsd**2 exactly, so no square root is needed
        rsd_squared = (variance / mean ** 2) * 100 ** 2
        if rsd_squared > max_rsd ** 2:
            reasons.append("replicate_rsd_exceeded")
    return reasons


def _concentration(slope, intercept, signal, dilution):
    return (Fraction(signal) - intercept) / slope * Fraction(dilution)


FIXTURES = {
    "nonterm-legacy": {
        "format": "legacy",
        "calibrations": [("nitrate", "3", "0")],
        "rows": [
            ("B1", "S1", "nitrate", "1", "sample", "10", "1", ""),
            ("B1", "S1", "nitrate", "2", "sample", "11", "1", ""),
            ("B1", "Q1", "nitrate", "1", "QC", "21", "1", "7"),
        ],
        "intent": (
            "Replicates calibrate to 10/3 and 11/3, both non-terminating in base "
            "10. The mean is exactly 7/2 and the SD is sqrt(2)/6, so every "
            "statistic is repeating or irrational while all decisions sit "
            "comfortably inside the policy window."
        ),
    },
    "rsd-threshold-legacy": {
        "format": "legacy",
        "calibrations": [("nitrate", "1", "0")],
        "rows": [
            ("B1", "S1", "nitrate", "1", "sample", "535355339059", "1", ""),
            ("B1", "S1", "nitrate", "2", "sample", "464644660941", "1", ""),
            ("B1", "Q1", "nitrate", "1", "QC", "10", "1", "10"),
        ],
        "intent": (
            "Mean is exactly 5e11 and the exact RSD is 9.9999999999...%, "
            "strictly below the 10% limit yet indistinguishable from it at six "
            "decimals. The decision is a near-tie for the unrounded strict "
            "comparison and cannot be recovered from the displayed value."
        ),
    },
    "qc-boundary-legacy": {
        "format": "legacy",
        "calibrations": [("nitrate", "1", "0")],
        "rows": [
            ("B1", "S1", "nitrate", "1", "sample", "10", "1", ""),
            ("B1", "S1", "nitrate", "2", "sample", "10", "1", ""),
            ("B1", "Q1", "nitrate", "1", "QC", "7.999999999", "1", "10"),
        ],
        "intent": (
            "QC recovery is exactly 79.9999999%, one part in 1e7 below the "
            "inclusive 80% limit, so the unrounded decision is FAIL while "
            "six-decimal display rounds to 80.000000. A consumer re-deriving "
            "the decision from displayed values sees an apparent contradiction; "
            "this reports an unrounded/display gap, not a wrong decision."
        ),
    },
    "range-edge-legacy": {
        "format": "legacy",
        "calibrations": [("nitrate", "1", "0")],
        "rows": [
            ("B1", "S1", "nitrate", "1", "sample", "999999999999.6", "1.0000001", ""),
            ("B1", "S1", "nitrate", "2", "sample", "999999999999.6", "1.0000001", ""),
            ("B1", "Q1", "nitrate", "1", "QC", "10", "1", "10"),
        ],
        "intent": (
            "Exact concentration is 1.000000099999...e12 mg/L, just above the "
            "1e12 acceptance bound, so the default context must reject the "
            "input. A low-precision multiply rounds it to exactly 1e12 and a "
            "context-dependent loader silently accepts what the contract "
            "forbids."
        ),
    },
    "nonterm-timed": {
        "format": "timed",
        "calibrations": [("nitrate", "C1", "2026-01-01T00:00:00Z", "", "3", "0")],
        "rows": [
            ("B1", "S1", "nitrate", "1", "sample", "10", "1", "", "2026-03-01T08:00:00Z"),
            ("B1", "S1", "nitrate", "2", "sample", "10", "1", "", "2026-03-01T08:05:00Z"),
            ("B1", "Q1", "nitrate", "1", "QC", "9", "1", "3", "2026-03-01T09:00:00Z"),
        ],
        "intent": (
            "Schema 2 analogue: the report renders the 10/3 concentration as "
            "3.333333. The warehouse compares that claimed six-decimal value to "
            "a fresh division with exact equality, so a rounding display is "
            "treated as a factual disagreement."
        ),
    },
    "qc-boundary-timed": {
        "format": "timed",
        "calibrations": [("nitrate", "C1", "2026-01-01T00:00:00Z", "", "3", "0")],
        "rows": [
            ("B1", "S1", "nitrate", "1", "sample", "30", "1", "", "2026-03-01T08:00:00Z"),
            ("B1", "S1", "nitrate", "2", "sample", "33", "1", "", "2026-03-01T08:05:00Z"),
            ("B1", "Q1", "nitrate", "1", "QC", "35", "1", "10", "2026-03-01T09:00:00Z"),
            ("B1", "Q1", "nitrate", "2", "QC", "37", "1", "10", "2026-03-01T09:05:00Z"),
        ],
        "intent": (
            "QC replicates 35/3 and 37/3 average exactly 12, so recovery is "
            "exactly 120% and the inclusive limit PASSES. Summation rounding at "
            "28 digits can perturb the mean by 1e-27 and flip the decision."
        ),
    },
    "range-edge-timed": {
        "format": "timed",
        "calibrations": [("nitrate", "C1", "2026-01-01T00:00:00Z", "", "1", "0")],
        "rows": [
            ("B1", "S1", "nitrate", "1", "sample", "999999999999.6", "1.0000001", "", "2026-03-01T08:00:00Z"),
            ("B1", "S1", "nitrate", "2", "sample", "999999999999.6", "1.0000001", "", "2026-03-01T08:05:00Z"),
            ("B1", "Q1", "nitrate", "1", "QC", "10", "1", "10", "2026-03-01T09:00:00Z"),
        ],
        "intent": (
            "Timed analogue used against the serial acquisition finalizer, which "
            "validates the staged CSV with the same load contract before "
            "publishing."
        ),
    },
    "clean-timed": {
        "format": "timed",
        "calibrations": [("nitrate", "C1", "2026-01-01T00:00:00Z", "", "2", "0")],
        "rows": [
            ("B1", "S1", "nitrate", "1", "sample", "10", "1", "", "2026-03-01T08:00:00Z"),
            ("B1", "S1", "nitrate", "2", "sample", "10", "1", "", "2026-03-01T08:05:00Z"),
            ("B1", "Q1", "nitrate", "1", "QC", "10", "1", "5", "2026-03-01T09:00:00Z"),
        ],
        "intent": (
            "Terminating control: every fact is exact in any decimal context, so "
            "an import/analysis must succeed in every context. A failure here "
            "is a false positive of the diagnosis, not a real drift."
        ),
    },
}


def _expectations(spec):
    timed = spec["format"] == "timed"
    calibrations = {cal[0]: (Fraction(cal[-2]), Fraction(cal[-1]))
                    for cal in spec["calibrations"]}
    groups = {}
    for row in spec["rows"]:
        batch, sample, analyte, _replicate, kind, signal, dilution, target = row[:8]
        slope, intercept = calibrations[analyte]
        concentration = _concentration(slope, intercept, signal, dilution)
        key = (batch, analyte, sample)
        group = groups.setdefault(key, {
            "kind": kind,
            "concentrations": [],
            "target": Fraction(target) if kind == "QC" and target else None,
        })
        group["concentrations"].append(concentration)

    low = _dec(POLICY["qc_low_pct"])
    high = _dec(POLICY["qc_high_pct"])
    out = {"format": spec["format"], "intent": spec["intent"], "groups": {}, "load": {}}
    all_concentrations = []
    for (batch, analyte, sample), group in sorted(groups.items()):
        n, mean, variance, sd, rsd = _statistics(group["concentrations"])
        all_concentrations.extend(group["concentrations"])
        entry = {
            "kind": group["kind"],
            "n": n,
            "mean_exact": _fraction_text(mean),
            "mean_display_6dp": _show(_frac_dec(mean)),
        }
        if variance is not None:
            entry["sd_squared_exact"] = _fraction_text(variance)
            entry["sd_display_6dp"] = _show(sd)
            entry["rsd_display_6dp"] = _show(rsd)
            entry["rsd_exact_note"] = (
                "irrational; see sd_squared_exact" if variance.denominator != 1
                else _fraction_text(variance)
            )
            entry["rsd_display_is_near_half_tie"] = _near_six_place_tie(rsd)
            entry["full_50dp"] = {
                "sd": _plain(sd),
                "rsd": _plain(rsd),
            }
        if group["kind"] == "sample":
            entry["max_rsd_pct"] = POLICY["max_rsd_pct"]
            entry["reasons_at_default_context"] = _decide_sample(
                group["concentrations"], POLICY["min_replicates"], Fraction(POLICY["max_rsd_pct"]))
            entry["decision_note"] = (
                "strict > on the unrounded RSD, evaluated exactly; the "
                "six-decimal display cannot express a near-tie"
            )
        if group["kind"] == "QC":
            recovery = _frac_dec(mean) / _frac_dec(group["target"]) * 100
            recovery_exact = mean / group["target"] * 100
            displayed = _quantize_six(recovery)
            entry.update({
                "target_exact": _fraction_text(group["target"]),
                "recovery_exact": _fraction_text(recovery_exact),
                "recovery_display_6dp": _plain(displayed),
                "recovery_display_is_near_half_tie": _near_six_place_tie(recovery),
                "recovery_50dp": _plain(recovery),
                "passed_at_default_context": low <= recovery <= high,
                "display_would_suggest_passed": low <= displayed <= high,
                "decision_note": (
                    "inclusive limits applied to the unrounded recovery; "
                    "display is not used for the decision"
                ),
            })
        out["groups"][f"{batch}/{analyte}/{sample}"] = entry

    within_bounds = all(0 <= value <= 1_000_000_000_000 for value in all_concentrations)
    out["load"] = {
        "accepted_at_default_context": within_bounds,
        "concentration_max_exact": _fraction_text(max(all_concentrations)),
        "bound": "0 <= concentration <= 1000000000000 mg/L",
    }
    return out


def _write_csv(path, headers, rows):
    lines = [",".join(headers)]
    lines.extend(",".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    index = {}
    for name, spec in FIXTURES.items():
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        if spec["format"] == "legacy":
            _write_csv(directory / "calibrations.csv", LEGACY_CAL_HEADERS,
                       [(analyte, slope, intercept, "mg/L")
                        for analyte, slope, intercept in spec["calibrations"]])
            _write_csv(directory / "results.csv", LEGACY_RESULT_HEADERS, spec["rows"])
        else:
            _write_csv(directory / "calibrations.csv", TIMED_CAL_HEADERS,
                       [(analyte, calibration_id, start, end, slope, intercept, "mg/L")
                        for analyte, calibration_id, start, end, slope, intercept
                        in spec["calibrations"]])
            _write_csv(directory / "results.csv", TIMED_RESULT_HEADERS, spec["rows"])
        (directory / "policy.json").write_text(
            json.dumps(POLICY, sort_keys=True) + "\n", encoding="utf-8")
        expected = _expectations(spec)
        (directory / "expected.json").write_text(
            json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        index[name] = expected
    (root / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(description="Materialise exact synthetic fixtures")
    parser.add_argument("--out", required=True, type=Path,
                        help="directory to write the fixtures into")
    args = parser.parse_args(argv)
    index = build(args.out)
    print(json.dumps({"fixtures": sorted(index), "root": str(args.out)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
