"""Unrounded Decimal decisions; sample SD uses the n-1 denominator."""
from collections import defaultdict
from decimal import Decimal, localcontext


def shown(value):
    if value is None:
        return None
    with localcontext() as context:
        context.prec = max(28, value.adjusted()+8)
        return format(value.quantize(Decimal("0.000001")), "f")


def analyze(rows, policy):
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
        values = [r["concentration"] for r in records]
        n = len(values)
        mean = sum(values)/n
        sd = (sum((x-mean)**2 for x in values)/(n-1)).sqrt() if n > 1 else None
        rsd = sd/mean*100 if sd is not None and mean != 0 else None
        base = {"batch": batch, "analyte": analyte, "sample": sample, "n": n, "mean_mg_l": shown(mean), "sample_sd_mg_l": shown(sd), "rsd_pct": shown(rsd)}
        if records[0]["kind"] == "QC":
            targets = {r["target"] for r in records}
            if len(targets) != 1:
                raise ValueError("QC group must have one target")
            recovery = mean/next(iter(targets))*100
            qcs.append({**base, "recovery_pct": shown(recovery), "passed": policy["qc_low_pct"] <= recovery <= policy["qc_high_pct"]})
        else:
            reasons = []
            if n < policy["min_replicates"]:
                reasons.append("insufficient_replicates")
            if mean == 0:
                reasons.append("zero_mean_rsd_undefined")
            elif rsd is not None and rsd > policy["max_rsd_pct"]:
                reasons.append("replicate_rsd_exceeded")
            samples.append({**base, "reasons": reasons})
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
