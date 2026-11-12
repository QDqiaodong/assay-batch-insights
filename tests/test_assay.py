import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from assay_insights.input import load
from assay_insights.metrics import analyze
from assay_insights.report import run, render

DATA = Path(__file__).resolve().parents[1]/"data"


class AssayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ["calibrations.csv", "results.csv", "policy.json"]:
            (self.root/name).write_bytes((DATA/name).read_bytes())

    def inputs(self):
        return load(self.root/"calibrations.csv", self.root/"results.csv", self.root/"policy.json")

    def test_calibration_dilution_sd_and_batch_qc(self):
        report = analyze(*self.inputs())
        self.assertEqual(report["summary"], {"sample_groups": 3, "reportable_groups": 2})
        by_sample = {s["sample"]:s for s in report["samples"]}
        self.assertEqual(by_sample["S1"]["mean_mg_l"], "4.600000")
        self.assertEqual(by_sample["S1"]["sample_sd_mg_l"], "0.141421")
        self.assertEqual(by_sample["S1"]["rsd_pct"], "3.074377")
        self.assertEqual(by_sample["S3"]["mean_mg_l"], "4.000000")
        self.assertEqual(by_sample["S2"]["reasons"], ["replicate_rsd_exceeded", "batch_qc_failed"])
        self.assertEqual(next(q for q in report["quality_controls"] if q["sample"]=="Q2")["recovery_pct"], "150.000000")

    def test_deterministic_reports_and_provenance(self):
        args = [self.root/"calibrations.csv",self.root/"results.csv",self.root/"policy.json",self.root/"out"]
        run(*args)
        before = (self.root/"out/report.json").read_bytes()
        run(*args)
        self.assertEqual(before, (self.root/"out/report.json").read_bytes())
        self.assertEqual(len(json.loads(before)["inputs_sha256"]), 3)
        self.assertIn("150.000000", (self.root/"out/report.html").read_text())

    def test_missing_qc_blocks_release(self):
        rows, policy = self.inputs()
        report = analyze([r for r in rows if r["kind"] != "QC"], policy)
        self.assertTrue(all(not s["reportable"] and "missing_qc" in s["reasons"] for s in report["samples"]))

    def test_single_replicate_and_zero_mean(self):
        rows, policy = self.inputs()
        one = [r for r in rows if r["replicate"]=="1"]
        self.assertIn("insufficient_replicates", analyze(one,policy)["samples"][0]["reasons"])
        for r in rows:
            if r["kind"]=="sample": r["concentration"] = Decimal(0)
        sample = analyze(rows,policy)["samples"][0]
        self.assertIsNone(sample["rsd_pct"])
        self.assertIn("zero_mean_rsd_undefined", sample["reasons"])

    def test_limits_are_inclusive_before_rounding(self):
        rows, policy = self.inputs()
        for r in rows:
            if r["sample"]=="Q1": r["concentration"] = Decimal("8")
        self.assertTrue(next(q for q in analyze(rows,policy)["quality_controls"] if q["sample"]=="Q1")["passed"])
        for r in rows:
            if r["sample"]=="Q1": r["concentration"] = Decimal("7.999999999")
        self.assertFalse(next(q for q in analyze(rows,policy)["quality_controls"] if q["sample"]=="Q1")["passed"])

    def test_duplicate_and_nonfinite_rejected(self):
        path = self.root/"results.csv"
        original = path.read_text()
        path.write_text(original+original.splitlines()[1]+"\n")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.inputs()
        for bad in ["NaN", "Infinity", "-10", "1e-10000", "1e10000"]:
            path.write_text(original.replace(",10,1,", f",{bad},1,"))
            with self.assertRaises(ValueError): self.inputs()

    def test_invalid_calibration_and_policy(self):
        c = self.root/"calibrations.csv"
        c.write_text(c.read_text().replace("nitrate,2", "nitrate,0"))
        with self.assertRaises(ValueError): self.inputs()
        c.write_bytes((DATA/"calibrations.csv").read_bytes())
        (self.root/"policy.json").write_text('{"min_replicates":1}')
        with self.assertRaises(ValueError): self.inputs()

    def test_inconsistent_qc_target_and_escaped_html(self):
        rows, policy = self.inputs()
        q = next(r for r in rows if r["sample"]=="Q1")
        rows.append({**q, "replicate":"2", "target": Decimal(12)})
        with self.assertRaisesRegex(ValueError, "one target"): analyze(rows,policy)
        report = analyze(*self.inputs())
        report["samples"][0]["sample"] = "<script>alert(1)</script>"
        self.assertNotIn("<script>", render(report))
        self.assertIn("&lt;script&gt;", render(report))


if __name__ == "__main__":
    unittest.main()
