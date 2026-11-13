"""Capture, validate and canonicalize project-generated schema 1/2 reports.

The warehouse stores *claimed analysis facts*, never re-reading the original
CSVs or recomputing calibration selection. A report is accepted only when it is
a complete report produced by this project and when every section is internally
self-consistent (input hashes, policy, summary, samples, QC, QC scopes and, for
schema 2, per-measurement calibration choices and final reasons).
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation

from ..input import canonical_time, parse_time
from ..metrics import analyze
from .errors import ReportValidationError
from .manifest import canonical_json_bytes

_SCHEMA_KEYS_V1 = {
    "schema_version", "unit", "batches", "quality_controls", "samples",
    "summary", "inputs_sha256", "policy",
}
_SCHEMA_KEYS_V2 = {
    "schema_version", "unit", "calibrations", "measurements", "qc_scopes",
    "quality_controls", "samples", "summary", "inputs_sha256", "policy",
}
_POLICY_KEYS = {"min_replicates", "max_rsd_pct", "qc_low_pct", "qc_high_pct"}
_INPUT_KEYS = {"calibrations", "results", "policy"}
_HEX64 = "0123456789abcdef"

_LEGACY_REASONS = {
    "insufficient_replicates", "zero_mean_rsd_undefined",
    "replicate_rsd_exceeded", "missing_qc", "batch_qc_failed",
}


def _fail(message):
    raise ReportValidationError(message)


def _is_identifier(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


def _is_decimal_text(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        number = Decimal(value)
    except InvalidOperation:
        return False
    return number.is_finite()


def _is_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in _HEX64 for char in value.lower()))


def _check_policy(policy):
    if not isinstance(policy, dict) or set(policy) != _POLICY_KEYS:
        _fail("policy must contain exactly min_replicates, max_rsd_pct, qc_low_pct, qc_high_pct")
    if type(policy["min_replicates"]) is not int or policy["min_replicates"] < 2:
        _fail("policy.min_replicates must be an integer >= 2")
    for key in ("max_rsd_pct", "qc_low_pct", "qc_high_pct"):
        if not _is_decimal_text(policy[key]) or Decimal(policy[key]) <= 0:
            _fail(f"policy.{key} must be a positive decimal string")
    if Decimal(policy["qc_low_pct"]) > Decimal(policy["qc_high_pct"]):
        _fail("policy QC range is reversed")


def _check_input_hashes(hashes):
    if not isinstance(hashes, dict) or set(hashes) != _INPUT_KEYS:
        _fail("inputs_sha256 must contain hashes for calibrations, results and policy")
    for name, digest in hashes.items():
        if not _is_sha256(digest):
            _fail(f"inputs_sha256.{name} must be a 64-character lowercase hex SHA256")


def _common_group_fields(group, kind, location):
    for key in ("batch", "analyte", "sample"):
        if not _is_identifier(group[key]):
            _fail(f"{location}.{key} must be a non-empty trimmed string")
    if type(group.get("n")) is not int or group["n"] <= 0:
        _fail(f"{location}.n must be a positive integer")
    for key in ("mean_mg_l", "sample_sd_mg_l", "rsd_pct"):
        if group[key] is not None and not _is_decimal_text(group[key]):
            _fail(f"{location}.{key} must be a decimal string or null")


def load_report(raw_bytes):
    """Parse and fully validate one captured report byte string."""
    if not isinstance(raw_bytes, (bytes, bytearray)):
        _fail("report must be captured as raw bytes")
    try:
        report = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        _fail(f"report must be UTF-8 encoded: {exc}")
    except json.JSONDecodeError as exc:
        _fail(f"report is not valid JSON: {exc}")
    if not isinstance(report, dict):
        _fail("report root must be a JSON object")
    schema_version = report.get("schema_version")
    if type(schema_version) is not int or schema_version not in (1, 2):
        _fail("report schema_version must be integer 1 or 2")
    _check_input_hashes(report.get("inputs_sha256"))
    _check_policy(report.get("policy"))
    if report.get("unit") != "mg/L":
        _fail("report unit must be mg/L")
    if schema_version == 1:
        _validate_v1(report)
    else:
        _validate_v2(report)
    report_sha256 = hashlib.sha256(bytes(raw_bytes)).hexdigest()
    canonical = canonical_report(report)
    canonical_bytes = canonical_json_bytes(canonical)
    return {
        "report": report,
        "sha256": report_sha256,
        "canonical": canonical,
        "canonical_bytes": canonical_bytes,
        "canonical_sha256": hashlib.sha256(canonical_bytes).hexdigest(),
    }


def _validate_summary(report):
    summary = report.get("summary")
    samples = report["samples"]
    if not isinstance(summary, dict) or set(summary) != {"sample_groups", "reportable_groups"}:
        _fail("summary must contain sample_groups and reportable_groups")
    if type(summary["sample_groups"]) is not int or summary["sample_groups"] != len(samples):
        _fail("summary.sample_groups does not match the number of sample groups")
    reportable = sum(1 for sample in samples if sample["reportable"])
    if type(summary["reportable_groups"]) is not int or summary["reportable_groups"] != reportable:
        _fail("summary.reportable_groups does not match reportable sample groups")


def _validate_v1(report):
    if set(report) != _SCHEMA_KEYS_V1:
        _fail(f"schema 1 report keys must be {sorted(_SCHEMA_KEYS_V1)}")
    for field in ("samples", "quality_controls", "batches"):
        if not isinstance(report[field], list):
            _fail(f"{field} must be an array")
    sample_keys, qc_keys = set(), set()
    for index, sample in enumerate(report["samples"]):
        location = f"samples[{index}]"
        if set(sample) != {
            "batch", "analyte", "sample", "n", "mean_mg_l", "sample_sd_mg_l",
            "rsd_pct", "reasons", "reportable",
        }:
            _fail(f"{location} has schema 2-only or unknown fields")
        _common_group_fields(sample, "sample", location)
        key = (sample["batch"], sample["analyte"], sample["sample"])
        if key in sample_keys:
            _fail(f"{location}: duplicate ordinary sample group {key}")
        sample_keys.add(key)
        if not isinstance(sample["reasons"], list) or any(
            not isinstance(reason, str) for reason in sample["reasons"]
        ):
            _fail(f"{location}.reasons must be an array of strings")
        if any(reason not in _LEGACY_REASONS or reason.startswith(("missing_qc:", "qc_failed:"))
               for reason in sample["reasons"]):
            _fail(f"{location}.reasons contains an unknown schema 1 reason")
        if type(sample["reportable"]) is not bool or sample["reportable"] != (not sample["reasons"]):
            _fail(f"{location}.reportable must equal emptiness of reasons")
    for index, qc in enumerate(report["quality_controls"]):
        location = f"quality_controls[{index}]"
        if set(qc) != {
            "batch", "analyte", "sample", "n", "mean_mg_l", "sample_sd_mg_l",
            "rsd_pct", "recovery_pct", "passed",
        }:
            _fail(f"{location} has unexpected fields for schema 1 QC")
        _common_group_fields(qc, "QC", location)
        key = (qc["batch"], qc["analyte"], qc["sample"])
        if key in qc_keys or key in sample_keys:
            _fail(f"{location}: QC identity overlaps another group")
        qc_keys.add(key)
        if not _is_decimal_text(qc["recovery_pct"]) or type(qc["passed"]) is not bool:
            _fail(f"{location}.recovery_pct/passed invalid")
        limits = report["policy"]
        expected_pass = (Decimal(limits["qc_low_pct"]) <= Decimal(qc["recovery_pct"])
                         <= Decimal(limits["qc_high_pct"]))
        if qc["passed"] != expected_pass:
            _fail(f"{location}.passed disagrees with policy limits and recovery_pct")
    if sample_keys & qc_keys:
        _fail("a business identity is both an ordinary sample and a QC group")
    _check_v1_batches(report, sample_keys, qc_keys)
    _validate_summary(report)


def _check_v1_batches(report, sample_keys, qc_keys):
    expected = sorted({(batch, analyte) for batch, analyte, _ in sample_keys | qc_keys})
    seen = set()
    qc_by_scope = {}
    for qc in report["quality_controls"]:
        qc_by_scope.setdefault((qc["batch"], qc["analyte"]), []).append(qc)
    sample_by_scope = {}
    for sample in report["samples"]:
        sample_by_scope.setdefault((sample["batch"], sample["analyte"]), []).append(sample)
    for index, batch in enumerate(report["batches"]):
        location = f"batches[{index}]"
        if set(batch) != {"batch", "analyte", "qc_groups", "qc_passed"}:
            _fail(f"{location} has unexpected fields")
        if not _is_identifier(batch["batch"]) or not _is_identifier(batch["analyte"]):
            _fail(f"{location} identifiers invalid")
        key = (batch["batch"], batch["analyte"])
        if key in seen:
            _fail(f"{location}: duplicate batch/analyte scope")
        seen.add(key)
        relevant = qc_by_scope.get(key, [])
        if type(batch["qc_groups"]) is not int or batch["qc_groups"] != len(relevant):
            _fail(f"{location}.qc_groups does not match QC rows")
        expected_passed = bool(relevant) and all(qc["passed"] for qc in relevant)
        if type(batch["qc_passed"]) is not bool or batch["qc_passed"] != expected_passed:
            _fail(f"{location}.qc_passed does not match QC outcomes")
    if seen != set(expected):
        _fail("batches do not cover every batch/analyte scope used by samples and QC")
    # Every sample reason must be consistent with the scope-level QC outcome.
    for sample in report["samples"]:
        key = (sample["batch"], sample["analyte"])
        relevant = qc_by_scope.get(key, [])
        reasons = sample["reasons"]
        if not relevant:
            if "missing_qc" not in reasons:
                _fail(f"sample {key + (sample['sample'],)} lacks missing_qc reason")
        elif not all(qc["passed"] for qc in relevant):
            if "batch_qc_failed" not in reasons:
                _fail(f"sample {key + (sample['sample'],)} lacks batch_qc_failed reason")
        elif "missing_qc" in reasons or "batch_qc_failed" in reasons:
            _fail(f"sample {key + (sample['sample'],)} carries a QC reason despite passing QC")


def _validate_v2(report):
    if set(report) != _SCHEMA_KEYS_V2:
        _fail(f"schema 2 report keys must be {sorted(_SCHEMA_KEYS_V2)}")
    for field in ("calibrations", "measurements", "qc_scopes", "quality_controls", "samples"):
        if not isinstance(report[field], list):
            _fail(f"{field} must be an array")
    _check_calibrations(report)
    rebuilt_rows = _check_measurements(report)
    _check_qc_and_scopes(report)
    # Re-run the project's own analysis on the claimed per-measurement facts.
    # The warehouse never re-reads CSVs; it only verifies the claimed facts are
    # mutually consistent with the shipped analysis semantics.
    policy = {
        "min_replicates": report["policy"]["min_replicates"],
        "max_rsd_pct": Decimal(report["policy"]["max_rsd_pct"]),
        "qc_low_pct": Decimal(report["policy"]["qc_low_pct"]),
        "qc_high_pct": Decimal(report["policy"]["qc_high_pct"]),
    }
    expected = analyze(rebuilt_rows, policy)
    # Compare canonical, order-independent structures: a report that reorders
    # records or JSON keys but keeps the same facts must validate identically.
    for field in ("samples", "quality_controls", "qc_scopes", "summary",
                  "calibrations", "measurements"):
        actual_section = _canonical_section(report, field)
        expected_section = _canonical_section(expected, field)
        if _stable(actual_section) != _stable(expected_section):
            _fail(f"report section {field} is inconsistent with its per-measurement facts")
    _validate_summary(report)


def _check_calibrations(report):
    seen = set()
    for index, calibration in enumerate(report["calibrations"]):
        location = f"calibrations[{index}]"
        if set(calibration) != {
            "analyte", "calibration_id", "valid_from", "valid_to",
            "slope", "intercept", "unit",
        }:
            _fail(f"{location} has unexpected fields")
        if not _is_identifier(calibration["analyte"]) or not _is_identifier(calibration["calibration_id"]):
            _fail(f"{location} identifiers invalid")
        if calibration["unit"] != "mg/L":
            _fail(f"{location}.unit must be mg/L")
        if not _is_decimal_text(calibration["slope"]) or Decimal(calibration["slope"]) <= 0:
            _fail(f"{location}.slope must be a positive decimal")
        if not _is_decimal_text(calibration["intercept"]):
            _fail(f"{location}.intercept must be a decimal string")
        key = (calibration["analyte"], calibration["calibration_id"])
        if key in seen:
            _fail(f"{location}: duplicate calibration version {key}")
        seen.add(key)
        try:
            valid_from = parse_time(calibration["valid_from"], "<report>", None, "valid_from")
        except ValueError as exc:
            _fail(f"{location}.valid_from is not a normalized RFC 3339 timestamp: {exc}")
        if calibration["valid_from"] != canonical_time(valid_from):
            _fail(f"{location}.valid_from is not canonical UTC text")
        if calibration["valid_to"] is None:
            continue
        try:
            valid_to = parse_time(calibration["valid_to"], "<report>", None, "valid_to")
        except ValueError as exc:
            _fail(f"{location}.valid_to is not a normalized RFC 3339 timestamp: {exc}")
        if calibration["valid_to"] != canonical_time(valid_to) or valid_to <= valid_from:
            _fail(f"{location}.valid_to must be canonical and later than valid_from")


_MEASUREMENT_KEYS = {
    "batch", "sample", "analyte", "replicate", "kind", "measured_at",
    "calibration_id", "valid_from", "valid_to", "signal", "dilution",
    "slope", "intercept", "concentration_mg_l",
}
_MEASUREMENT_QC_KEYS = _MEASUREMENT_KEYS | {"target_mg_l"}


def _check_measurements(report):
    catalog = {
        (c["analyte"], c["calibration_id"]): c for c in report["calibrations"]
    }
    identities = set()
    rebuilt_rows = []
    for index, measurement in enumerate(report["measurements"]):
        location = f"measurements[{index}]"
        is_qc = measurement.get("kind") == "QC"
        allowed = _MEASUREMENT_QC_KEYS if is_qc else _MEASUREMENT_KEYS
        if set(measurement) != allowed:
            _fail(f"{location} has unexpected or missing fields")
        for key in ("batch", "sample", "analyte", "replicate", "kind",
                    "measured_at", "calibration_id"):
            if not _is_identifier(measurement[key]):
                _fail(f"{location}.{key} must be a non-empty trimmed string")
        if measurement["kind"] not in ("sample", "QC"):
            _fail(f"{location}.kind must be sample or QC")
        identity = (measurement["batch"], measurement["sample"],
                    measurement["analyte"], measurement["replicate"])
        if identity in identities:
            _fail(f"{location}: duplicate measurement identity {identity}")
        identities.add(identity)
        version = catalog.get((measurement["analyte"], measurement["calibration_id"]))
        if version is None:
            _fail(f"{location}: calibration_id is absent from the calibration catalog")
        if measurement["valid_from"] != version["valid_from"] or \
                measurement["valid_to"] != version["valid_to"] or \
                measurement["slope"] != version["slope"] or \
                measurement["intercept"] != version["intercept"]:
            _fail(f"{location}: version facts disagree with the calibration catalog")
        try:
            measured_at = parse_time(measurement["measured_at"], "<report>", None, "measured_at")
        except ValueError as exc:
            _fail(f"{location}.measured_at is not a normalized RFC 3339 timestamp: {exc}")
        if measurement["measured_at"] != canonical_time(measured_at):
            _fail(f"{location}.measured_at is not canonical UTC text")
        for key in ("signal", "dilution", "slope", "intercept", "concentration_mg_l"):
            if not _is_decimal_text(measurement[key]):
                _fail(f"{location}.{key} must be a decimal string")
        if Decimal(measurement["dilution"]) <= 0 or Decimal(measurement["slope"]) <= 0:
            _fail(f"{location}: dilution and slope must be positive")
        # Verify the claimed calibration selection actually covers the instant.
        valid_from = parse_time(version["valid_from"], "<report>", None, "valid_from")
        covered = valid_from <= measured_at
        if version["valid_to"] is not None:
            valid_to = parse_time(version["valid_to"], "<report>", None, "valid_to")
            covered = covered and measured_at < valid_to
        if not covered:
            _fail(f"{location}: measured_at is outside the claimed calibration interval")
        # Verify the concentration formula from the claimed raw signal facts.
        signal = Decimal(measurement["signal"])
        slope = Decimal(measurement["slope"])
        intercept = Decimal(measurement["intercept"])
        dilution = Decimal(measurement["dilution"])
        expected_concentration = (signal - intercept) / slope * dilution
        if Decimal(measurement["concentration_mg_l"]) != expected_concentration:
            _fail(f"{location}: concentration_mg_l disagrees with (signal-intercept)/slope*dilution")
        target = None
        if is_qc:
            if not _is_decimal_text(measurement["target_mg_l"]) or \
                    Decimal(measurement["target_mg_l"]) <= 0:
                _fail(f"{location}.target_mg_l must be a positive decimal string")
            target = Decimal(measurement["target_mg_l"])
        rebuilt_rows.append({
            "batch": measurement["batch"],
            "sample": measurement["sample"],
            "analyte": measurement["analyte"],
            "replicate": measurement["replicate"],
            "kind": measurement["kind"],
            "signal": signal,
            "dilution": dilution,
            "concentration": expected_concentration,
            "target": target,
            "measured_at": measurement["measured_at"],
            "calibration_id": measurement["calibration_id"],
            "valid_from": measurement["valid_from"],
            "valid_to": measurement["valid_to"],
            "slope": slope,
            "intercept": intercept,
            "_schema_version": 2,
        })
    return rebuilt_rows


def _check_qc_and_scopes(report):
    qc_keys, sample_keys = set(), set()
    for index, sample in enumerate(report["samples"]):
        location = f"samples[{index}]"
        if set(sample) != {
            "batch", "analyte", "sample", "n", "mean_mg_l", "sample_sd_mg_l",
            "rsd_pct", "calibration_ids", "qc_checks", "reasons", "reportable",
        }:
            _fail(f"{location} has unexpected fields")
        _common_group_fields(sample, "sample", location)
        key = (sample["batch"], sample["analyte"], sample["sample"])
        if key in sample_keys:
            _fail(f"{location}: duplicate ordinary sample group")
        sample_keys.add(key)
        if not isinstance(sample["calibration_ids"], list) or not sample["calibration_ids"]:
            _fail(f"{location}.calibration_ids must be a non-empty array")
        if any(not _is_identifier(version) for version in sample["calibration_ids"]):
            _fail(f"{location}.calibration_ids must contain identifiers")
        if sample["calibration_ids"] != sorted(sample["calibration_ids"]) or \
                len(set(sample["calibration_ids"])) != len(sample["calibration_ids"]):
            _fail(f"{location}.calibration_ids must be sorted unique identifiers")
        if type(sample["reportable"]) is not bool or sample["reportable"] != (not sample["reasons"]):
            _fail(f"{location}.reportable must equal emptiness of reasons")
        if any(not isinstance(reason, str) for reason in sample["reasons"]):
            _fail(f"{location}.reasons must be strings")
    for index, qc in enumerate(report["quality_controls"]):
        location = f"quality_controls[{index}]"
        if set(qc) != {
            "batch", "analyte", "sample", "calibration_id", "target_mg_l", "n",
            "mean_mg_l", "sample_sd_mg_l", "rsd_pct", "recovery_pct", "passed",
        }:
            _fail(f"{location} has unexpected fields")
        _common_group_fields(qc, "QC", location)
        if not _is_identifier(qc["calibration_id"]):
            _fail(f"{location}.calibration_id invalid")
        if not _is_decimal_text(qc["target_mg_l"]) or Decimal(qc["target_mg_l"]) <= 0:
            _fail(f"{location}.target_mg_l invalid")
        if not _is_decimal_text(qc["recovery_pct"]) or type(qc["passed"]) is not bool:
            _fail(f"{location}.recovery_pct/passed invalid")
        key = (qc["batch"], qc["analyte"], qc["sample"])
        if key in sample_keys:
            _fail(f"{location}: QC identity overlaps an ordinary sample")
        qc_keys.add(key)
        limits = report["policy"]
        expected_pass = (Decimal(limits["qc_low_pct"]) <= Decimal(qc["recovery_pct"])
                         <= Decimal(limits["qc_high_pct"]))
        if qc["passed"] != expected_pass:
            _fail(f"{location}.passed disagrees with the policy recovery window")
    for index, scope in enumerate(report["qc_scopes"]):
        location = f"qc_scopes[{index}]"
        if set(scope) != {
            "batch", "analyte", "calibration_id", "qc_groups", "qc_passed",
            "status", "failing_samples",
        }:
            _fail(f"{location} has unexpected fields")
        if scope["status"] not in ("passed", "failed", "missing"):
            _fail(f"{location}.status invalid")
        if not isinstance(scope["failing_samples"], list):
            _fail(f"{location}.failing_samples must be an array")


def _stable(value):
    """JSON-normalized comparison helper for report sections."""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))


_SECTION_SORT_KEYS = {
    "samples": lambda item: (item["batch"], item["analyte"], item["sample"]),
    "quality_controls": lambda item: (
        item["batch"], item["analyte"],
        item.get("calibration_id") or "", item["sample"],
    ),
    "qc_scopes": lambda item: (item["batch"], item["analyte"], item["calibration_id"]),
    "calibrations": lambda item: (item["analyte"], item["calibration_id"]),
    "measurements": lambda item: (
        item["batch"], item["analyte"], item["sample"],
        item["replicate"], item["measured_at"],
    ),
}


def _canonical_section(report, field):
    section = report[field]
    if field == "summary":
        return section
    return sorted(section, key=_SECTION_SORT_KEYS[field])


def canonical_report(report):
    """Order every list by stable business keys for byte-stable normalization.

    The canonical form contains the same claimed facts as the captured report
    but never its whitespace or key ordering, so re-emitting a report with
    reordered records/keys normalizes to one fact snapshot.
    """
    schema_version = report["schema_version"]
    canonical = {
        "schema_version": schema_version,
        "unit": report["unit"],
        "policy": report["policy"],
        "inputs_sha256": report["inputs_sha256"],
        "summary": report["summary"],
        "quality_controls": None,
        "samples": None,
    }
    if schema_version == 1:
        canonical["batches"] = sorted(
            report["batches"], key=lambda item: (item["batch"], item["analyte"])
        )
        canonical["quality_controls"] = sorted(
            report["quality_controls"],
            key=lambda item: (item["batch"], item["analyte"], item["sample"]),
        )
        canonical["samples"] = sorted(
            report["samples"],
            key=lambda item: (item["batch"], item["analyte"], item["sample"]),
        )
        return canonical
    canonical["calibrations"] = sorted(
        report["calibrations"],
        key=lambda item: (item["analyte"], item["calibration_id"]),
    )
    canonical["measurements"] = sorted(
        report["measurements"],
        key=lambda item: (item["batch"], item["analyte"], item["sample"],
                          item["replicate"], item["measured_at"]),
    )
    canonical["qc_scopes"] = sorted(
        report["qc_scopes"],
        key=lambda item: (item["batch"], item["analyte"], item["calibration_id"]),
    )
    canonical["quality_controls"] = sorted(
        report["quality_controls"],
        key=lambda item: (item["batch"], item["analyte"], item["calibration_id"], item["sample"]),
    )
    canonical["samples"] = sorted(
        report["samples"],
        key=lambda item: (item["batch"], item["analyte"], item["sample"]),
    )
    return canonical
