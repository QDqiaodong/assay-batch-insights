"""Validated instrument exports and explicit calibration/quality policy."""
import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

LEGACY_CALIBRATION_HEADERS = ["analyte", "slope", "intercept", "unit"]
TIMED_CALIBRATION_HEADERS = ["analyte", "calibration_id", "valid_from", "valid_to", "slope", "intercept", "unit"]
LEGACY_RESULT_HEADERS = ["batch", "sample", "analyte", "replicate", "kind", "signal", "dilution", "target_mg_l"]
TIMED_RESULT_HEADERS = LEGACY_RESULT_HEADERS + ["measured_at"]

# RFC 3339 date-time with an explicit UTC offset or Z. Seconds are required;
# fractional seconds are optional and limited to six digits (microseconds).
_TIME_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[Tt](?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?"
    r"(?P<zone>[Zz]|(?P<sign>[+-])(?P<offset_hour>\d{2}):(?P<offset_minute>\d{2}))$"
)


def number(text, positive=False):
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"invalid decimal: {text}") from exc
    if not value.is_finite() or (abs(value) > Decimal("1e12") or (value != 0 and abs(value) < Decimal("1e-12"))) or (positive and value <= 0):
        raise ValueError("decimal must be finite, bounded, and positive where required")
    return value


def _number_at(path, line, field, text, positive=False):
    try:
        return number(text, positive)
    except ValueError as exc:
        raise ValueError(f"{path}:{line}: {field}: {exc}") from exc


def _fail(path, line, message):
    location = f"{path}:{line}" if line is not None else str(path)
    raise ValueError(f"{location}: {message}")


def read_csv(path, headers):
    """Read an exact-header CSV, keeping the physical line number for errors."""
    with open(path, encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != headers:
            raise ValueError(f"{path}: expected columns: {headers}; found: {reader.fieldnames}")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path}:{reader.line_num}: empty or malformed CSV")
            row["_line"] = reader.line_num
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: empty CSV")
    return rows


def _header(path):
    with open(path, encoding="utf-8", newline="") as stream:
        return next(csv.reader(stream), None)


def parse_time(text, path, line, field):
    """Parse strict RFC 3339 into an aware UTC datetime."""
    match = _TIME_RE.fullmatch(text or "")
    if not match:
        _fail(path, line, f"{field} must be an RFC 3339 timestamp with explicit UTC offset or Z: {text!r}")
    parts = match.groupdict()
    microsecond = int((parts["fraction"] or "").ljust(6, "0") or "0")
    if parts["zone"].upper() == "Z":
        zone = timezone.utc
    else:
        offset_hour = int(parts["offset_hour"])
        offset_minute = int(parts["offset_minute"])
        if offset_hour > 23 or offset_minute > 59:
            _fail(path, line, f"{field} has an invalid UTC offset: {text!r}")
        direction = 1 if parts["sign"] == "+" else -1
        zone = timezone(direction * timedelta(hours=offset_hour, minutes=offset_minute))
    try:
        value = datetime(
            int(parts["year"]), int(parts["month"]), int(parts["day"]),
            int(parts["hour"]), int(parts["minute"]), int(parts["second"]),
            microsecond, tzinfo=zone,
        )
    except ValueError as exc:
        _fail(path, line, f"{field} is not a valid RFC 3339 timestamp: {text!r} ({exc})")
    return value.astimezone(timezone.utc)


def canonical_time(value):
    """Stable UTC representation with exactly six fractional digits."""
    value = value.astimezone(timezone.utc)
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
        f".{value.microsecond:06d}Z"
    )


def _load_policy(path):
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid policy JSON: {exc}") from exc
    if not isinstance(policy, dict) or set(policy) != {"min_replicates", "max_rsd_pct", "qc_low_pct", "qc_high_pct"}:
        raise ValueError(f"{path}: policy keys differ from v1 contract")
    if type(policy["min_replicates"]) is not int or policy["min_replicates"] < 2:
        raise ValueError(f"{path}: at least two replicates required")
    for key in ["max_rsd_pct", "qc_low_pct", "qc_high_pct"]:
        try:
            policy[key] = number(str(policy[key]), True)
        except ValueError as exc:
            raise ValueError(f"{path}: policy {key}: {exc}") from exc
    if policy["qc_low_pct"] > policy["qc_high_pct"]:
        raise ValueError(f"{path}: reversed QC range")
    return policy


def _load_legacy(calibrations_path, results_path, policy_path):
    calibrations = {}
    for row in read_csv(calibrations_path, LEGACY_CALIBRATION_HEADERS):
        line = row.pop("_line")
        analyte = row["analyte"]
        if not analyte:
            _fail(calibrations_path, line, "empty analyte")
        if analyte in calibrations:
            _fail(calibrations_path, line, f"duplicate calibration for analyte {analyte!r}")
        if row["unit"] != "mg/L":
            _fail(calibrations_path, line, "unsupported unit")
        slope = _number_at(calibrations_path, line, "slope", row["slope"], True)
        intercept = _number_at(calibrations_path, line, "intercept", row["intercept"])
        calibrations[analyte] = (slope, intercept)

    policy = _load_policy(policy_path)
    rows = []
    seen = set()
    for row in read_csv(results_path, LEGACY_RESULT_HEADERS):
        line = row.pop("_line")
        identity = [row[key] for key in ["batch", "sample", "analyte", "replicate"]]
        if any(not value for value in identity):
            _fail(results_path, line, "missing identifier")
        if row["analyte"] not in calibrations:
            _fail(results_path, line, f"unknown analyte {row['analyte']!r}")
        if row["kind"] not in {"sample", "QC"}:
            _fail(results_path, line, f"unknown kind {row['kind']!r}")
        key = tuple(identity)
        if key in seen:
            _fail(results_path, line, "duplicate replicate identity")
        seen.add(key)
        signal = _number_at(results_path, line, "signal", row["signal"])
        dilution = _number_at(results_path, line, "dilution", row["dilution"], True)
        slope, intercept = calibrations[row["analyte"]]
        concentration = (signal - intercept) / slope * dilution
        if not 0 <= concentration <= Decimal("1e12"):
            _fail(results_path, line, "calibrated concentration must be between 0 and 1e12 mg/L")
        target = _number_at(results_path, line, "target_mg_l", row["target_mg_l"], True) if row["kind"] == "QC" else None
        if row["kind"] == "sample" and row["target_mg_l"]:
            _fail(results_path, line, "ordinary samples have no recovery target")
        rows.append({**row, "concentration": concentration, "target": target})
    return rows, policy


def _load_timed(calibrations_path, results_path, policy_path):
    by_analyte = defaultdict(list)
    seen_ids = {}
    for row in read_csv(calibrations_path, TIMED_CALIBRATION_HEADERS):
        line = row.pop("_line")
        analyte = row["analyte"]
        calibration_id = row["calibration_id"]
        if not analyte or not calibration_id:
            _fail(calibrations_path, line, "missing analyte or calibration_id")
        if row["unit"] != "mg/L":
            _fail(calibrations_path, line, "unsupported unit")
        if not row["valid_from"]:
            _fail(calibrations_path, line, "valid_from is required")
        valid_from = parse_time(row["valid_from"], calibrations_path, line, "valid_from")
        if row["valid_to"] == "":
            valid_to = None
            valid_to_text = None
        else:
            valid_to = parse_time(row["valid_to"], calibrations_path, line, "valid_to")
            valid_to_text = canonical_time(valid_to)
            if valid_to <= valid_from:
                _fail(calibrations_path, line, "valid_to must be later than valid_from")
        identifier_key = (analyte, calibration_id)
        if identifier_key in seen_ids:
            _fail(calibrations_path, line, f"duplicate calibration_id {calibration_id!r} within analyte {analyte!r}")
        slope = _number_at(calibrations_path, line, "slope", row["slope"], True)
        intercept = _number_at(calibrations_path, line, "intercept", row["intercept"])
        version = {
            "calibration_id": calibration_id,
            "valid_from": canonical_time(valid_from),
            "valid_to": valid_to_text,
            "valid_from_utc": valid_from,
            "valid_to_utc": valid_to,
            "slope": slope,
            "intercept": intercept,
            "_line": line,
        }
        seen_ids[identifier_key] = version
        by_analyte[analyte].append(version)

    for analyte, versions in sorted(by_analyte.items()):
        ordered = sorted(versions, key=lambda item: (item["valid_from_utc"], item["calibration_id"]))
        for previous, current in zip(ordered, ordered[1:]):
            if previous["valid_to_utc"] is None or current["valid_from_utc"] < previous["valid_to_utc"]:
                previous_end = (
                    "no upper bound"
                    if previous["valid_to_utc"] is None
                    else f"{previous['valid_to']} (calibrations line {previous['_line']})"
                )
                _fail(
                    calibrations_path, current["_line"],
                    f"calibration intervals overlap for analyte {analyte!r}: "
                    f"{current['calibration_id']!r} starts at {current['valid_from']} "
                    f"before {previous['calibration_id']!r} ends at {previous_end}",
                )

    policy = _load_policy(policy_path)
    rows = []
    seen = set()
    for row in read_csv(results_path, TIMED_RESULT_HEADERS):
        line = row.pop("_line")
        identity = [row[key] for key in ["batch", "sample", "analyte", "replicate"]]
        if any(not value for value in identity):
            _fail(results_path, line, "missing identifier")
        if row["analyte"] not in by_analyte:
            _fail(results_path, line, f"unknown analyte {row['analyte']!r}")
        if row["kind"] not in {"sample", "QC"}:
            _fail(results_path, line, f"unknown kind {row['kind']!r}")
        key = tuple(identity)
        if key in seen:
            _fail(results_path, line, "duplicate replicate identity")
        seen.add(key)
        signal = _number_at(results_path, line, "signal", row["signal"])
        dilution = _number_at(results_path, line, "dilution", row["dilution"], True)
        if not row["measured_at"]:
            _fail(results_path, line, "measured_at is required")
        measured_at = parse_time(row["measured_at"], results_path, line, "measured_at")
        measured_at_text = canonical_time(measured_at)
        matches = [
            version for version in by_analyte[row["analyte"]]
            if version["valid_from_utc"] <= measured_at
            and (version["valid_to_utc"] is None or measured_at < version["valid_to_utc"])
        ]
        if not matches:
            _fail(
                results_path, line,
                f"no calibration version covers measured_at {measured_at_text} for "
                f"analyte {row['analyte']!r} (batch={row['batch']!r}, sample={row['sample']!r}, "
                f"replicate={row['replicate']!r})",
            )
        if len(matches) > 1:
            _fail(results_path, line, "measured_at matches more than one calibration version")
        version = matches[0]
        concentration = (signal - version["intercept"]) / version["slope"] * dilution
        if not 0 <= concentration <= Decimal("1e12"):
            _fail(results_path, line, "calibrated concentration must be between 0 and 1e12 mg/L")
        target = _number_at(results_path, line, "target_mg_l", row["target_mg_l"], True) if row["kind"] == "QC" else None
        if row["kind"] == "sample" and row["target_mg_l"]:
            _fail(results_path, line, "ordinary samples have no recovery target")
        rows.append({
            **row,
            "signal": signal,
            "dilution": dilution,
            "concentration": concentration,
            "target": target,
            "measured_at": measured_at_text,
            "calibration_id": version["calibration_id"],
            "valid_from": version["valid_from"],
            "valid_to": version["valid_to"],
            "slope": version["slope"],
            "intercept": version["intercept"],
            "_schema_version": 2,
        })
    return rows, policy


def load(calibrations_path, results_path, policy_path):
    """Load either legacy or timed inputs; mixed formats are a contract error."""
    calibration_header = _header(calibrations_path)
    results_header = _header(results_path)
    if calibration_header == LEGACY_CALIBRATION_HEADERS:
        calibration_format = "legacy"
    elif calibration_header == TIMED_CALIBRATION_HEADERS:
        calibration_format = "timed"
    else:
        raise ValueError(f"{calibrations_path}: unrecognized calibration header: {calibration_header}")
    if results_header == LEGACY_RESULT_HEADERS:
        results_format = "legacy"
    elif results_header == TIMED_RESULT_HEADERS:
        results_format = "timed"
    else:
        raise ValueError(f"{results_path}: unrecognized results header: {results_header}")
    if calibration_format != results_format:
        raise ValueError(
            f"{results_path}: mixed calibration/result formats are not supported "
            f"(calibrations: {calibration_format}, results: {results_format})"
        )
    if calibration_format == "legacy":
        return _load_legacy(calibrations_path, results_path, policy_path)
    return _load_timed(calibrations_path, results_path, policy_path)
