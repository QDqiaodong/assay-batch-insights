"""Unrounded Decimal decisions; sample SD uses the n-1 denominator."""
from collections import defaultdict
from decimal import Decimal, localcontext


def shown(value):
    if value is None:
        return None
    with localcontext() as context:
        context.prec = max(28, value.adjusted()+8)
        return format(value.quantize(Decimal("0.000001")), "f")


def _statistics(records):
    values = [r["concentration"] for r in records]
    n = len(values)
    mean = sum(values)/n
    sd = (sum((x-mean)**2 for x in values)/(n-1)).sqrt() if n > 1 else None
    rsd = sd/mean*100 if sd is not None and mean != 0 else None
    return n, mean, sd, rsd


def _common(base, n, mean, sd, rsd):
    return {**base, "n": n, "mean_mg_l": shown(mean), "sample_sd_mg_l": shown(sd), "rsd_pct": shown(rsd)}


def analyze(rows, policy):
    if any(row.get("_schema_version") == 2 for row in rows):
        return _analyze_timed(rows, policy)
    return _analyze_legacy(rows, policy)


def _analyze_legacy(rows, policy):
    groups = defaultdict(list)
    kinds = {}
    for row in rows:
        key = (row["batch"], row["analyte"], row["sample"])
        if key in kinds and kinds[key] != row["kind"]:
            raise ValueError("sample identity mixes QC and ordinary rows")
        kinds[key] = row["kind"]
        groups[key].append(row)
    qcs, samples = [], []
    for (batch, analyte, sample), records in sorted(groups.items()):
        n, mean, sd, rsd = _statistics(records)
        base = {"batch": batch, "analyte": analyte, "sample": sample}
        if records[0]["kind"] == "QC":
            targets = {r["target"] for r in records}
            if len(targets) != 1:
                raise ValueError("QC group must have one target")
            recovery = mean/next(iter(targets))*100
            qcs.append({**_common(base, n, mean, sd, rsd), "recovery_pct": shown(recovery),
                        "passed": policy["qc_low_pct"] <= recovery <= policy["qc_high_pct"]})
        else:
            reasons = []
            if n < policy["min_replicates"]:
                reasons.append("insufficient_replicates")
            if mean == 0:
                reasons.append("zero_mean_rsd_undefined")
            elif rsd is not None and rsd > policy["max_rsd_pct"]:
                reasons.append("replicate_rsd_exceeded")
            samples.append({**_common(base, n, mean, sd, rsd), "reasons": reasons})
    batch_keys = sorted({(r["batch"], r["analyte"]) for r in rows})
    batches = []
    for batch, analyte in batch_keys:
        relevant = [q for q in qcs if (q["batch"], q["analyte"]) == (batch, analyte)]
        passed = bool(relevant) and all(q["passed"] for q in relevant)
        batches.append({"batch": batch, "analyte": analyte, "qc_groups": len(relevant), "qc_passed": passed})
        for sample in samples:
            if (sample["batch"], sample["analyte"]) == (batch, analyte):
                if not relevant:
                    sample["reasons"].append("missing_qc")
                elif not passed:
                    sample["reasons"].append("batch_qc_failed")
                sample["reportable"] = not sample["reasons"]
    return {"schema_version": 1, "unit": "mg/L", "batches": batches, "quality_controls": qcs, "samples": samples,
            "summary": {"sample_groups": len(samples), "reportable_groups": sum(s["reportable"] for s in samples)}}


def _analyze_timed(rows, policy):
    # QC identity keeps one target across calibration versions; subgrouping is
    # solely by calibration_id within that identity.
    identities = defaultdict(list)
    for row in rows:
        identities[(row["batch"], row["analyte"], row["sample"])].append(row)
    for records in identities.values():
        kinds = {r["kind"] for r in records}
        if len(kinds) != 1:
            raise ValueError("sample identity mixes QC and ordinary rows")
        if records[0]["kind"] == "QC" and len({r["target"] for r in records}) != 1:
            raise ValueError("QC group must have one target within its batch/analyte/sample identity")

    qc_groups = defaultdict(list)
    sample_groups = defaultdict(list)
    for row in rows:
        key = (row["batch"], row["analyte"], row["sample"])
        if row["kind"] == "QC":
            qc_groups[(row["batch"], row["analyte"], row["calibration_id"], row["sample"])].append(row)
        else:
            sample_groups[key].append(row)

    qcs = []
    for (batch, analyte, calibration_id, sample), records in sorted(qc_groups.items()):
        n, mean, sd, rsd = _statistics(records)
        target = records[0]["target"]
        recovery = mean/target*100
        qcs.append({
            "batch": batch, "analyte": analyte, "sample": sample,
            "calibration_id": calibration_id,
            "target_mg_l": shown(target),
            "n": n, "mean_mg_l": shown(mean), "sample_sd_mg_l": shown(sd), "rsd_pct": shown(rsd),
            "recovery_pct": shown(recovery),
            "passed": policy["qc_low_pct"] <= recovery <= policy["qc_high_pct"],
        })

    scope_keys = sorted({(row["batch"], row["analyte"], row["calibration_id"]) for row in rows})
    scopes = []
    for batch, analyte, calibration_id in scope_keys:
        relevant = [q for q in qcs if (q["batch"], q["analyte"], q["calibration_id"]) == (batch, analyte, calibration_id)]
        passed = bool(relevant) and all(q["passed"] for q in relevant)
        status = "passed" if passed else ("missing" if not relevant else "failed")
        scopes.append({
            "batch": batch, "analyte": analyte, "calibration_id": calibration_id,
            "qc_groups": len(relevant),
            "qc_passed": passed,
            "status": status,
            "failing_samples": sorted(q["sample"] for q in relevant if not q["passed"]),
        })
    scope_by_key = {(s["batch"], s["analyte"], s["calibration_id"]): s for s in scopes}

    samples = []
    for (batch, analyte, sample), records in sorted(sample_groups.items()):
        n, mean, sd, rsd = _statistics(records)
        versions = sorted({r["calibration_id"] for r in records})
        reasons = []
        if len(versions) > 1:
            reasons.append("mixed_calibration_versions")
        if n < policy["min_replicates"]:
            reasons.append("insufficient_replicates")
        if mean == 0:
            reasons.append("zero_mean_rsd_undefined")
        elif rsd is not None and rsd > policy["max_rsd_pct"]:
            reasons.append("replicate_rsd_exceeded")
        qc_checks = []
        for calibration_id in versions:
            scope = scope_by_key[(batch, analyte, calibration_id)]
            if not scope["qc_groups"]:
                reasons.append(f"missing_qc:{calibration_id}")
            elif not scope["qc_passed"]:
                reasons.append(f"qc_failed:{calibration_id}")
            qc_checks.append({
                "calibration_id": calibration_id,
                "qc_groups": scope["qc_groups"],
                "qc_passed": scope["qc_passed"],
                "status": scope["status"],
            })
        samples.append({
            "batch": batch, "analyte": analyte, "sample": sample,
            "n": n, "mean_mg_l": shown(mean), "sample_sd_mg_l": shown(sd), "rsd_pct": shown(rsd),
            "calibration_ids": versions,
            "qc_checks": qc_checks,
            "reasons": reasons,
            "reportable": not reasons,
        })

    measurements = []
    for row in sorted(rows, key=lambda r: (r["batch"], r["analyte"], r["sample"], r["replicate"], r["measured_at"])):
        measurements.append({
            "batch": row["batch"], "sample": row["sample"], "analyte": row["analyte"],
            "replicate": row["replicate"], "kind": row["kind"],
            "measured_at": row["measured_at"],
            "calibration_id": row["calibration_id"],
            "valid_from": row["valid_from"], "valid_to": row["valid_to"],
            "signal": str(row["signal"]), "dilution": str(row["dilution"]),
            "slope": str(row["slope"]), "intercept": str(row["intercept"]),
            "concentration_mg_l": shown(row["concentration"]),
            **({"target_mg_l": shown(row["target"])} if row["kind"] == "QC" else {}),
        })

    seen = {}
    for row in rows:
        seen[(row["analyte"], row["calibration_id"])] = row
    catalog = [
        {"analyte": analyte, "calibration_id": calibration_id,
         "valid_from": row["valid_from"], "valid_to": row["valid_to"],
         "slope": str(row["slope"]), "intercept": str(row["intercept"]), "unit": "mg/L"}
        for (analyte, calibration_id), row in sorted(seen.items())
    ]

    return {
        "schema_version": 2, "unit": "mg/L",
        "calibrations": catalog,
        "measurements": measurements,
        "qc_scopes": scopes,
        "quality_controls": qcs,
        "samples": samples,
        "summary": {"sample_groups": len(samples), "reportable_groups": sum(s["reportable"] for s in samples)},
    }
