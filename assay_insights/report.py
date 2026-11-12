"""Observable standalone HTML plus machine-readable, reproducible JSON."""
import hashlib
import html
import json
from pathlib import Path
from .input import load
from .metrics import analyze


def render(report):
    def cell(value):
        return "<td>"+html.escape(str(value))+"</td>"
    rows = []
    for s in report["samples"]:
        rows.append("<tr>"+"".join(cell(s[k]) for k in ["batch", "sample", "analyte", "n", "mean_mg_l", "rsd_pct", "reportable"])+cell(", ".join(s["reasons"]))+"</tr>")
    qc = []
    for q in report["quality_controls"]:
        qc.append("<tr>"+"".join(cell(q[k]) for k in ["batch", "sample", "analyte", "recovery_pct", "passed"])+"</tr>")
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Assay batch report</title>"
            "<style>body{font:16px system-ui;margin:2rem;max-width:1100px}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:.5rem}th{background:#e7f0f5}</style>"
            "<h1>Synthetic assay batch report</h1><p>Concentration in mg/L; sample SD uses n-1. QC limits are demonstration policy, not a regulatory standard.</p>"
            "<h2>Samples</h2><table><tr><th>Batch</th><th>Sample</th><th>Analyte</th><th>n</th><th>Mean</th><th>RSD %</th><th>Reportable</th><th>Reasons</th></tr>"+"".join(rows)+"</table>"
            "<h2>Quality controls</h2><table><tr><th>Batch</th><th>Control</th><th>Analyte</th><th>Recovery %</th><th>Passed</th></tr>"+"".join(qc)+"</table></html>")


def run(calibrations, results, policy, output):
    rows, limits = load(calibrations, results, policy)
    report = analyze(rows, limits)
    report["inputs_sha256"] = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in [("calibrations", calibrations), ("results", results), ("policy", policy)]}
    report["policy"] = {k: str(v) if not isinstance(v, int) else v for k,v in limits.items()}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output/"report.json").write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    (output/"report.html").write_text(render(report), encoding="utf-8")
    return report
