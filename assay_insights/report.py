"""Observable standalone HTML plus machine-readable, reproducible JSON."""
import hashlib
import html
import json
from pathlib import Path
from .input import load
from .metrics import analyze


def _cell(value):
    return "<td>"+html.escape(str(value))+"</td>"


def _render_v1(report):
    rows = []
    for s in report["samples"]:
        rows.append("<tr>"+"".join(_cell(s[k]) for k in ["batch", "sample", "analyte", "n", "mean_mg_l", "rsd_pct", "reportable"])+_cell(", ".join(s["reasons"]))+"</tr>")
    qc = []
    for q in report["quality_controls"]:
        qc.append("<tr>"+"".join(_cell(q[k]) for k in ["batch", "sample", "analyte", "recovery_pct", "passed"])+"</tr>")
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Assay batch report</title>"
            "<style>body{font:16px system-ui;margin:2rem;max-width:1100px}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:.5rem}th{background:#e7f0f5}</style>"
            "<h1>Synthetic assay batch report</h1><p>Concentration in mg/L; sample SD uses n-1. QC limits are demonstration policy, not a regulatory standard.</p>"
            "<h2>Samples</h2><table><tr><th>Batch</th><th>Sample</th><th>Analyte</th><th>n</th><th>Mean</th><th>RSD %</th><th>Reportable</th><th>Reasons</th></tr>"+"".join(rows)+"</table>"
            "<h2>Quality controls</h2><table><tr><th>Batch</th><th>Control</th><th>Analyte</th><th>Recovery %</th><th>Passed</th></tr>"+"".join(qc)+"</table></html>")


def _render_v2(report):
    def interval(item):
        return f"[{item['valid_from']}, {item['valid_to'] or 'open'})"

    catalog = []
    for c in report["calibrations"]:
        catalog.append("<tr>"+_cell(c["analyte"])+_cell(c["calibration_id"])+_cell(interval(c))+
                       _cell(c["slope"])+_cell(c["intercept"])+"</tr>")

    measurements = []
    for m in report["measurements"]:
        measurements.append("<tr>"+"".join(_cell(m[k]) for k in [
            "batch", "sample", "analyte", "replicate", "kind", "measured_at",
            "calibration_id", "slope", "intercept", "dilution", "concentration_mg_l"])+
            _cell(m.get("target_mg_l", ""))+"</tr>")

    scopes = []
    for s in report["qc_scopes"]:
        scopes.append("<tr>"+_cell(s["batch"])+_cell(s["analyte"])+_cell(s["calibration_id"])+
                      _cell(s["qc_groups"])+_cell(s["status"])+_cell(", ".join(s["failing_samples"] or []))+"</tr>")

    qc = []
    for q in report["quality_controls"]:
        qc.append("<tr>"+"".join(_cell(q[k]) for k in [
            "batch", "sample", "analyte", "calibration_id", "target_mg_l", "n",
            "mean_mg_l", "recovery_pct", "passed"])+"</tr>")

    samples = []
    for s in report["samples"]:
        checks = "; ".join(
            f"{c['calibration_id']}: {c['status']} ({c['qc_groups']} QC group(s))"
            for c in s["qc_checks"]
        )
        samples.append("<tr>"+"".join(_cell(s[k]) for k in [
            "batch", "sample", "analyte", "n", "mean_mg_l", "rsd_pct"])+
            _cell(", ".join(s["calibration_ids"]))+_cell(checks)+_cell(s["reportable"])+
            _cell(", ".join(s["reasons"]))+"</tr>")

    return ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Assay batch report</title>"
            "<style>body{font:16px system-ui;margin:2rem;max-width:1400px}table{border-collapse:collapse;margin-bottom:1.5rem}td,th{border:1px solid #aaa;padding:.4rem;vertical-align:top}th{background:#e7f0f5}</style>"
            "<h1>Synthetic assay batch report (schema v2)</h1>"
            "<p>Concentration in mg/L; sample SD uses n-1. Times are normalized to UTC. QC limits are demonstration policy, not a regulatory standard.</p>"
            "<h2>Calibration versions</h2><table><tr><th>Analyte</th><th>Calibration ID</th><th>Valid interval</th><th>Slope</th><th>Intercept</th></tr>"+"".join(catalog)+"</table>"
            "<h2>Measurements and selected versions</h2><table><tr><th>Batch</th><th>Sample</th><th>Analyte</th><th>Replicate</th><th>Kind</th><th>Measured at (UTC)</th><th>Calibration ID</th><th>Slope</th><th>Intercept</th><th>Dilution</th><th>Concentration</th><th>Target</th></tr>"+"".join(measurements)+"</table>"
            "<h2>QC eligibility by batch / analyte / calibration version</h2><table><tr><th>Batch</th><th>Analyte</th><th>Calibration ID</th><th>QC groups</th><th>Status</th><th>Failing controls</th></tr>"+"".join(scopes)+"</table>"
            "<h2>Quality controls by calibration version</h2><table><tr><th>Batch</th><th>Control</th><th>Analyte</th><th>Calibration ID</th><th>Target</th><th>n</th><th>Mean</th><th>Recovery %</th><th>Passed</th></tr>"+"".join(qc)+"</table>"
            "<h2>Samples</h2><table><tr><th>Batch</th><th>Sample</th><th>Analyte</th><th>n</th><th>Mean</th><th>RSD %</th><th>Calibration IDs</th><th>QC checks</th><th>Reportable</th><th>Reasons</th></tr>"+"".join(samples)+"</table></html>")


def render(report):
    if report["schema_version"] == 2:
        return _render_v2(report)
    return _render_v1(report)


def run(calibrations, results, policy, output):
    # Everything is validated and rendered before the output directory is touched.
    rows, limits = load(calibrations, results, policy)
    report = analyze(rows, limits)
    report["inputs_sha256"] = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in [("calibrations", calibrations), ("results", results), ("policy", policy)]}
    report["policy"] = {k: str(v) if not isinstance(v, int) else v for k,v in limits.items()}
    json_text = json.dumps(report, indent=2, sort_keys=True)+"\n"
    html_text = render(report)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output/"report.json").write_text(json_text)
    (output/"report.html").write_text(html_text, encoding="utf-8")
    return report
