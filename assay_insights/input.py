"""Validated instrument exports and explicit calibration/quality policy."""
import csv
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path


def number(text, positive=False):
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"invalid decimal: {text}") from exc
    if not value.is_finite() or (abs(value) > Decimal("1e12") or (value != 0 and abs(value) < Decimal("1e-12"))) or (positive and value <= 0):
        raise ValueError("decimal must be finite, bounded, and positive where required")
    return value


def read_csv(path, headers):
    with open(path, encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != headers:
            raise ValueError(f"expected columns: {headers}")
        rows = list(reader)
    if not rows or any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError("empty or malformed CSV")
    return rows


def load(calibrations_path, results_path, policy_path):
    calibrations = {}
    for row in read_csv(calibrations_path, ["analyte", "slope", "intercept", "unit"]):
        if not row["analyte"] or row["analyte"] in calibrations or row["unit"] != "mg/L":
            raise ValueError("duplicate/invalid calibration or unsupported unit")
        calibrations[row["analyte"]] = (number(row["slope"], True), number(row["intercept"]))
    policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    if set(policy) != {"min_replicates", "max_rsd_pct", "qc_low_pct", "qc_high_pct"}:
        raise ValueError("policy keys differ from v1 contract")
    if type(policy["min_replicates"]) is not int or policy["min_replicates"] < 2:
        raise ValueError("at least two replicates required")
    for key in ["max_rsd_pct", "qc_low_pct", "qc_high_pct"]:
        policy[key] = number(str(policy[key]), True)
    if policy["qc_low_pct"] > policy["qc_high_pct"]:
        raise ValueError("reversed QC range")
    rows = []
    seen = set()
    headers = ["batch", "sample", "analyte", "replicate", "kind", "signal", "dilution", "target_mg_l"]
    for row in read_csv(results_path, headers):
        if any(not row[k] for k in ["batch", "sample", "analyte", "replicate"]) or row["analyte"] not in calibrations or row["kind"] not in {"sample", "QC"}:
            raise ValueError("missing identifier, unknown analyte or kind")
        key = tuple(row[k] for k in ["batch", "sample", "analyte", "replicate"])
        if key in seen:
            raise ValueError("duplicate replicate identity")
        seen.add(key)
        signal, dilution = number(row["signal"]), number(row["dilution"], True)
        slope, intercept = calibrations[row["analyte"]]
        concentration = (signal-intercept)/slope*dilution
        if not 0 <= concentration <= Decimal("1e12"):
            raise ValueError("calibrated concentration must be between 0 and 1e12 mg/L")
        target = number(row["target_mg_l"], True) if row["kind"] == "QC" else None
        if row["kind"] == "sample" and row["target_mg_l"]:
            raise ValueError("ordinary samples have no recovery target")
        rows.append({**row, "concentration": concentration, "target": target})
    return rows, policy
