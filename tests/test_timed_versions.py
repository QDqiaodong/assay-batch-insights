import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from assay_insights.input import load
from assay_insights.metrics import analyze
from assay_insights.report import render, run

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data"

TIMED_CALIBRATIONS = """analyte,calibration_id,valid_from,valid_to,slope,intercept,unit
nitrate,N-OLD,2026-03-01T00:00:00Z,2026-03-01T12:00:00Z,2,1,mg/L
nitrate,N-NEW,2026-03-01T12:00:00Z,,4,1,mg/L
phosphate,P-1,2026-03-01T00:00:00Z,,4,0,mg/L
"""

# B1/S1 old version only -> reportable (old QC passes even though new QC fails
# at 75%). B1/S2 and B3/S8 mix versions with identical 10 mg/L results but are
# still rejected. B1/S3 mixes versions and also exceeds RSD. B1/S4 is new-only
# and blocked by the failed new QC; B2/S7 needs new QC that is absent even
# though old QC exists. B1/S5 is phosphate. B2/S6 exercises zero mean.
TIMED_RESULTS = """batch,sample,analyte,replicate,kind,signal,dilution,target_mg_l,measured_at
B1,S1,nitrate,1,sample,10,1,,2026-03-01T08:00:00Z
B1,S1,nitrate,2,sample,10.4,1,,2026-03-01T08:05:00.000000Z
B1,Q1,nitrate,1,QC,21,1,10,2026-03-01T09:00:00Z
B1,Q1,nitrate,2,QC,41,1,10,2026-03-01T14:00:00+01:00
B1,Q2,nitrate,1,QC,31,1,10,2026-03-01T13:30:00+01:00
B1,S2,nitrate,1,sample,41,1,,2026-03-01T13:01:00Z
B1,S2,nitrate,2,sample,21,1,,2026-03-01T02:01:00-05:00
B1,S3,nitrate,1,sample,10,1,,2026-03-01T11:59:59Z
B1,S3,nitrate,2,sample,33,1,,2026-03-01T12:00:00Z
B1,S4,nitrate,1,sample,21,1,,2026-03-01T13:00:00Z
B1,S4,nitrate,2,sample,21,1,,2026-03-01T14:00:00+01:00
B1,S5,phosphate,1,sample,8,2,,2026-03-01T08:00:00Z
B1,S5,phosphate,2,sample,8,2,,2026-03-01T08:05:00Z
B1,Q3,phosphate,1,QC,16,1,4,2026-03-01T08:10:00Z
B2,S6,nitrate,1,sample,1,1,,2026-03-01T08:00:00Z
B2,S6,nitrate,2,sample,1,1,,2026-03-01T08:10:00Z
B2,Q4,nitrate,1,QC,21,1,10,2026-03-01T08:20:00Z
B2,S7,nitrate,1,sample,21,1,,2026-03-01T13:00:00Z
B2,S7,nitrate,2,sample,21,1,,2026-03-01T13:05:00Z
B3,S8,nitrate,1,sample,21,1,,2026-03-01T08:00:00Z
B3,S8,nitrate,2,sample,41,1,,2026-03-01T13:00:00Z
B3,Q5,nitrate,1,QC,21,1,10,2026-03-01T08:30:00Z
B3,Q6,nitrate,1,QC,41,1,10,2026-03-01T13:30:00Z
"""


class TimedFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write("calibrations.csv", TIMED_CALIBRATIONS)
        self.write("results.csv", TIMED_RESULTS)
        (self.root / "policy.json").write_bytes((DATA / "policy.json").read_bytes())

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def mutate(self, name, old, new):
        text = (self.root / name).read_text()
        self.assertIn(old, text)
        self.write(name, text.replace(old, new, 1))

    def load(self, calibrations="calibrations.csv", results="results.csv"):
        return load(self.root / calibrations, self.root / results, self.root / "policy.json")

    def report(self):
        return analyze(*self.load())

    def sample(self, report, name):
        return next(s for s in report["samples"] if s["sample"] == name)

    def qc(self, report, name, calibration_id):
        return next(q for q in report["quality_controls"]
                    if q["sample"] == name and q["calibration_id"] == calibration_id)

    def scope(self, report, batch, analyte, calibration_id):
        return next(s for s in report["qc_scopes"]
                    if (s["batch"], s["analyte"], s["calibration_id"]) == (batch, analyte, calibration_id))

    def measurement(self, report, sample, replicate):
        return next(m for m in report["measurements"]
                    if m["sample"] == sample and m["replicate"] == replicate)


class TimedVersionTests(TimedFixture):
    def test_ordinary_samples_are_not_split_by_version(self):
        report = self.report()
        # S2 spans both versions but remains one sample group with both replicates.
        self.assertEqual(
            len([s for s in report["samples"]
                 if (s["batch"], s["analyte"], s["sample"]) == ("B1", "nitrate", "S2")]), 1)
        self.assertEqual(self.sample(report, "S2")["n"], 2)
        self.assertEqual(len(report["samples"]), report["summary"]["sample_groups"])

    def test_fixture_versions_counts_and_values(self):
        report = self.report()
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["summary"], {"sample_groups": 8, "reportable_groups": 2})
        self.assertEqual(self.sample(report, "S1")["mean_mg_l"], "4.600000")
        self.assertEqual(self.sample(report, "S1")["sample_sd_mg_l"], "0.141421")
        self.assertEqual(self.sample(report, "S1")["rsd_pct"], "3.074377")
        self.assertEqual(self.sample(report, "S1")["calibration_ids"], ["N-OLD"])
        self.assertEqual(self.sample(report, "S5")["mean_mg_l"], "4.000000")
        self.assertEqual(self.sample(report, "S5")["calibration_ids"], ["P-1"])
        # The version catalogue lists the intervals without requiring inference.
        catalog = {(c["analyte"], c["calibration_id"]): c for c in report["calibrations"]}
        self.assertEqual(catalog[("nitrate", "N-OLD")]["valid_to"], "2026-03-01T12:00:00.000000Z")
        self.assertIsNone(catalog[("nitrate", "N-NEW")]["valid_to"])
        self.assertEqual(catalog[("nitrate", "N-NEW")]["slope"], "4")

    def test_boundary_and_offset_normalization(self):
        report = self.report()
        # 11:59:59Z is old; exactly 12:00:00Z (left-closed) is new.
        self.assertEqual(self.measurement(report, "S3", "1")["calibration_id"], "N-OLD")
        self.assertEqual(self.measurement(report, "S3", "2")["calibration_id"], "N-NEW")
        # Different offsets denoting the same instant normalize identically.
        self.assertEqual(self.measurement(report, "S4", "1")["measured_at"], "2026-03-01T13:00:00.000000Z")
        self.assertEqual(self.measurement(report, "S4", "2")["measured_at"], "2026-03-01T13:00:00.000000Z")
        self.assertEqual(self.measurement(report, "S4", "1")["calibration_id"], "N-NEW")
        self.assertEqual(self.measurement(report, "S4", "2")["calibration_id"], "N-NEW")
        # The old valid_to instant itself is excluded.
        self.assertEqual(self.measurement(report, "S3", "2")["valid_from"], "2026-03-01T12:00:00.000000Z")

    def test_concentration_counterexample_and_mixed_rejection(self):
        report = self.report()
        # signal=21, dilution=1: old slope=2/intercept=1 -> 10; new slope=4 -> 5.
        self.assertEqual(self.measurement(report, "S2", "1")["concentration_mg_l"], "10.000000")
        self.assertEqual(self.measurement(report, "S2", "1")["calibration_id"], "N-NEW")
        self.assertEqual(self.measurement(report, "S2", "2")["concentration_mg_l"], "10.000000")
        self.assertEqual(self.measurement(report, "S2", "2")["calibration_id"], "N-OLD")
        self.assertEqual(self.measurement(report, "S4", "1")["concentration_mg_l"], "5.000000")
        # Identical concentrations and a clean RSD do not rescue a mixed sample.
        self.assertEqual(self.sample(report, "S2")["mean_mg_l"], "10.000000")
        self.assertEqual(self.sample(report, "S2")["rsd_pct"], "0.000000")
        self.assertIn("mixed_calibration_versions", self.sample(report, "S2")["reasons"])
        self.assertFalse(self.sample(report, "S2")["reportable"])
        self.assertEqual(self.sample(report, "S8")["mean_mg_l"], "10.000000")
        self.assertEqual(self.sample(report, "S8")["reasons"], ["mixed_calibration_versions"])

    def test_qc_version_isolation_and_failed_new_version(self):
        report = self.report()
        # Old QC at 100 %, new QC at 75 % for the same batch/analyte.
        self.assertTrue(self.qc(report, "Q1", "N-OLD")["passed"])
        self.assertEqual(self.qc(report, "Q2", "N-NEW")["recovery_pct"], "75.000000")
        self.assertFalse(self.qc(report, "Q2", "N-NEW")["passed"])
        self.assertFalse(self.scope(report, "B1", "nitrate", "N-NEW")["qc_passed"])
        self.assertTrue(self.scope(report, "B1", "nitrate", "N-OLD")["qc_passed"])
        # An old-version sample remains reportable despite the new QC failure.
        self.assertTrue(self.sample(report, "S1")["reportable"])
        # A new-version sample is blocked only by its own version.
        self.assertEqual(self.sample(report, "S4")["reasons"], ["qc_failed:N-NEW"])
        # Missing new QC is not covered by the old QC group.
        self.assertEqual(self.sample(report, "S7")["reasons"], ["missing_qc:N-NEW"])
        self.assertEqual(self.scope(report, "B2", "nitrate", "N-NEW")["status"], "missing")
        # A QC identity is split by version while remaining one identity.
        self.assertEqual(self.scope(report, "B1", "nitrate", "N-NEW")["failing_samples"], ["Q2"])
        # Cross-version samples keep mixed_calibration_versions in addition to
        # per-version QC reasons.
        self.assertEqual(self.sample(report, "S2")["reasons"],
                         ["mixed_calibration_versions", "qc_failed:N-NEW"])
        self.assertIn("mixed_calibration_versions", self.sample(report, "S8")["reasons"])

    def test_multiple_reasons_coexist(self):
        report = self.report()
        self.assertEqual(self.sample(report, "S3")["reasons"],
                         ["mixed_calibration_versions", "replicate_rsd_exceeded", "qc_failed:N-NEW"])
        self.assertEqual(self.sample(report, "S6")["reasons"], ["zero_mean_rsd_undefined"])
        self.assertIsNone(self.sample(report, "S6")["rsd_pct"])

    def test_qc_target_conflict_within_identity_across_versions(self):
        rows, policy = self.load()
        # Add a new-version QC replicate to Q1's identity with a different target.
        extra = dict(rows[-1])
        source = next(r for r in rows if r["sample"] == "Q1" and r["calibration_id"] == "N-NEW")
        rows.append({**source, "replicate": "3", "target": Decimal(12)})
        with self.assertRaisesRegex(ValueError, "one target"):
            analyze(rows, policy)
        # Same target across versions remains valid and split into version subgroups.
        rows2, policy2 = self.load()
        source2 = next(r for r in rows2 if r["sample"] == "Q1" and r["calibration_id"] == "N-NEW")
        rows2.append({**source2, "replicate": "3"})
        report = analyze(rows2, policy2)
        self.assertEqual(self.qc(report, "Q1", "N-NEW")["n"], 2)


class TimedContractTests(TimedFixture):

    def test_adjacent_versions_are_allowed(self):
        report = self.report()  # old valid_to == new valid_from
        self.assertTrue(any(s["calibration_id"] == "N-NEW" for s in report["qc_scopes"]))

    def test_normalized_adjacent_offsets_are_allowed(self):
        self.mutate("calibrations.csv", "nitrate,N-NEW,2026-03-01T12:00:00Z",
                    "nitrate,N-NEW,2026-03-01T07:00:00-05:00")  # 12:00Z, adjacent -> ok
        report = self.report()
        self.assertEqual(self.measurement(report, "S3", "1")["calibration_id"], "N-OLD")
        self.assertEqual(self.measurement(report, "S3", "2")["calibration_id"], "N-NEW")

    def test_normalized_overlap_rejected(self):
        self.mutate("calibrations.csv", "nitrate,N-NEW,2026-03-01T12:00:00Z",
                    "nitrate,N-NEW,2026-03-01T06:59:00-05:00")  # 11:59Z < old end
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.load()

    def test_open_ended_version_must_be_last(self):
        self.mutate("calibrations.csv",
                    "nitrate,N-OLD,2026-03-01T00:00:00Z,2026-03-01T12:00:00Z",
                    "nitrate,N-OLD,2026-03-01T00:00:00Z,")
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.load()

    def test_reversed_and_zero_width_intervals(self):
        self.mutate("calibrations.csv",
                    "nitrate,N-OLD,2026-03-01T00:00:00Z,2026-03-01T12:00:00Z,2,1,mg/L",
                    "nitrate,N-OLD,2026-03-01T00:00:00Z,2026-03-01T00:00:00Z,2,1,mg/L")
        with self.assertRaisesRegex(ValueError, "valid_to"):
            self.load()
        self.mutate("calibrations.csv",
                    "nitrate,N-OLD,2026-03-01T00:00:00Z,2026-03-01T00:00:00Z,2,1,mg/L",
                    "nitrate,N-OLD,2026-03-01T12:00:00Z,2026-03-01T00:00:00Z,2,1,mg/L")
        with self.assertRaisesRegex(ValueError, "valid_to"):
            self.load()

    def test_duplicate_calibration_id_within_analyte(self):
        text = TIMED_CALIBRATIONS.replace(
            "phosphate,P-1,2026-03-01T00:00:00Z,,4,0,mg/L",
            "phosphate,P-1,2026-03-01T00:00:00Z,,4,0,mg/L\nnitrate,N-OLD,2026-03-02T00:00:00Z,,3,1,mg/L")
        self.write("calibrations.csv", text)
        with self.assertRaisesRegex(ValueError, "duplicate calibration_id"):
            self.load()

    def test_missing_coverage_is_fatal_without_fallback(self):
        self.mutate("results.csv", "2026-03-01T13:00:00Z\nB1,S4", "2026-02-28T13:00:00Z\nB1,S4")
        with self.assertRaisesRegex(ValueError, "no calibration version covers"):
            self.load()

    def test_gap_between_versions_is_not_filled(self):
        text = TIMED_CALIBRATIONS.replace(
            "nitrate,N-OLD,2026-03-01T00:00:00Z,2026-03-01T12:00:00Z,2,1,mg/L",
            "nitrate,N-OLD,2026-03-01T00:00:00Z,2026-03-01T09:00:00Z,2,1,mg/L")
        self.write("calibrations.csv", text)
        # S3's 11:59:59Z measurement falls in the 09:00-12:00Z gap.
        with self.assertRaisesRegex(ValueError, "no calibration version covers"):
            self.load()

    def test_time_requires_offset_and_valid_rfc3339(self):
        for bad in ["2026-03-01T13:00:00", "2026-03-01 13:00:00Z",
                    "2026-03-01T13:00:00.1234567Z", "2026-02-30T13:00:00Z",
                    "2026-03-01T13:00:00+24:00"]:
            with self.subTest(bad=bad):
                self.write("results.csv", TIMED_RESULTS.replace(
                    "2026-03-01T13:00:00Z\nB1,S4", f"{bad}\nB1,S4", 1))
                with self.assertRaises(ValueError):
                    self.load()
        # Six fractional digits and lowercase z are accepted.
        self.write("results.csv", TIMED_RESULTS.replace(
            "2026-03-01T13:00:00Z\nB1,S4", "2026-03-01T13:00:00.123456z\nB1,S4", 1))
        self.load()

    def test_blank_valid_from_and_missing_measured_at(self):
        self.mutate("calibrations.csv",
                    "nitrate,N-OLD,2026-03-01T00:00:00Z,2026-03-01T12:00:00Z,2,1,mg/L",
                    "nitrate,N-OLD,,2026-03-01T12:00:00Z,2,1,mg/L")
        with self.assertRaisesRegex(ValueError, "valid_from is required"):
            self.load()
        self.write("calibrations.csv", TIMED_CALIBRATIONS)
        self.mutate("results.csv", "2026-03-01T08:05:00.000000Z", "")
        with self.assertRaisesRegex(ValueError, "measured_at is required"):
            self.load()

    def test_mixed_formats_rejected_both_directions(self):
        (self.root / "legacy_calibrations.csv").write_bytes((DATA / "calibrations.csv").read_bytes())
        (self.root / "legacy_results.csv").write_bytes((DATA / "results.csv").read_bytes())
        with self.assertRaisesRegex(ValueError, "mixed calibration/result formats"):
            self.load(calibrations="legacy_calibrations.csv")
        with self.assertRaisesRegex(ValueError, "mixed calibration/result formats"):
            self.load(results="legacy_results.csv")

    def test_duplicate_identity_cannot_gain_version_dimension(self):
        text = TIMED_RESULTS + "B1,S4,nitrate,2,sample,21,1,,2026-03-01T12:00:00Z\n"
        self.write("results.csv", text)
        with self.assertRaisesRegex(ValueError, "duplicate replicate identity"):
            self.load()

    def test_unknown_header_reported_with_file(self):
        self.mutate("results.csv", "target_mg_l,measured_at", "target_mg_l,measured")
        with self.assertRaisesRegex(ValueError, "results.csv"):
            self.load()

    def test_error_names_file_and_line(self):
        self.mutate("results.csv", "2026-03-01T08:05:00.000000Z", "2026-03-01T08:05:00")
        with self.assertRaises(ValueError) as caught:
            self.load()
        message = str(caught.exception)
        self.assertIn(str(self.root / "results.csv"), message)
        self.assertIn(":3:", message)


class TimedReportTests(TimedFixture):
    def test_json_html_traceability_escaping_and_determinism(self):
        out = self.root / "out"
        run(self.root / "calibrations.csv", self.root / "results.csv",
            self.root / "policy.json", out)
        first_json = (out / "report.json").read_bytes()
        first_html = (out / "report.html").read_bytes()
        run(self.root / "calibrations.csv", self.root / "results.csv",
            self.root / "policy.json", out)
        self.assertEqual(first_json, (out / "report.json").read_bytes())
        self.assertEqual(first_html, (out / "report.html").read_bytes())
        report = json.loads(first_json)
        self.assertEqual(len(report["inputs_sha256"]), 3)
        self.assertEqual(report["inputs_sha256"]["results"],
                         hashlib.sha256((self.root / "results.csv").read_bytes()).hexdigest())
        html_text = first_html.decode()
        for value in ["N-OLD", "N-NEW", "mixed_calibration_versions", "75.000000",
                      "qc_failed:N-NEW", "missing_qc:N-NEW"]:
            self.assertIn(value, html_text)

    def test_html_escapes_external_identifiers(self):
        self.mutate("results.csv", "B1,S1,nitrate,1,sample", "B1,<script>alert(1)</script>,nitrate,1,sample")
        report = self.report()
        html_text = render(report)
        self.assertNotIn("<script>", html_text)
        self.assertIn("&lt;script&gt;", html_text)

    def test_failed_input_leaves_existing_reports_untouched(self):
        out = self.root / "out"
        args = [self.root / "calibrations.csv", self.root / "results.csv",
                self.root / "policy.json", out]
        run(*args)
        before = {name: (out / name).read_bytes() for name in ["report.json", "report.html"]}
        self.mutate("calibrations.csv", "nitrate,N-NEW,2026-03-01T12:00:00Z",
                    "nitrate,N-NEW,2026-03-01T06:59:00-05:00")
        with self.assertRaisesRegex(ValueError, "overlap"):
            run(*args)
        for name, content in before.items():
            self.assertEqual(content, (out / name).read_bytes())

    def test_corrected_input_can_reuse_output_directory(self):
        out = self.root / "out"
        self.mutate("results.csv", "2026-03-01T13:00:00Z\nB1,S4", "2026-02-28T13:00:00Z\nB1,S4")
        with self.assertRaises(ValueError):
            run(self.root / "calibrations.csv", self.root / "results.csv",
                self.root / "policy.json", out)
        self.write("results.csv", TIMED_RESULTS)
        report = run(self.root / "calibrations.csv", self.root / "results.csv",
                     self.root / "policy.json", out)
        self.assertEqual(report["schema_version"], 2)
        self.assertTrue((out / "report.json").exists())


class CliTests(TimedFixture):
    def cli(self, *extra, calibrations=None, results=None, policy=None, output=None):
        args = [sys.executable, "-m", "assay_insights"]
        args += ["--calibrations", str(calibrations or self.root / "calibrations.csv")]
        args += ["--results", str(results or self.root / "results.csv")]
        args += ["--policy", str(policy or self.root / "policy.json")]
        args += ["--output", str(output or self.root / "out")]
        env = dict(os.environ, PYTHONPATH=str(PROJECT))
        return subprocess.run(args, cwd=PROJECT, capture_output=True, text=True, env=env)

    def test_cli_end_to_end_timed_fixture(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"reportable_groups": 2', result.stdout)
        report = json.loads((self.root / "out/report.json").read_text())
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(len(report["measurements"]), 23)
        self.assertEqual(self.sample(report, "S1")["mean_mg_l"], "4.600000")
        first = (self.root / "out/report.json").read_bytes(), (self.root / "out/report.html").read_bytes()
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(first, ((self.root / "out/report.json").read_bytes(),
                                 (self.root / "out/report.html").read_bytes()))

    def test_cli_legacy_defaults_unchanged(self):
        result = self.cli(calibrations=DATA / "calibrations.csv", results=DATA / "results.csv",
                          policy=DATA / "policy.json",
                          output=self.root / "legacy")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.root / "legacy/report.json").read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["summary"], {"sample_groups": 3, "reportable_groups": 2})
        self.assertEqual(next(s for s in report["samples"] if s["sample"] == "S1")["mean_mg_l"], "4.600000")

    def test_cli_contract_error_is_nonzero_and_keeps_output(self):
        out = self.root / "out"
        self.assertEqual(self.cli(output=out).returncode, 0)
        before = {name: (out / name).read_bytes() for name in ["report.json", "report.html"]}
        self.mutate("results.csv", "2026-03-01T13:00:00Z\nB1,S4", "2026-03-01T13:00:00\nB1,S4")
        result = self.cli(output=out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("results.csv", result.stderr)
        self.assertIn("RFC 3339", result.stderr)
        for name, content in before.items():
            self.assertEqual(content, (out / name).read_bytes())

    def test_cli_qc_failure_is_successful_analysis(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.root / "out/report.json").read_text())
        self.assertIn("qc_failed:N-NEW", self.sample(report, "S4")["reasons"])
        self.assertFalse(self.sample(report, "S4")["reportable"])

    def test_cli_mixed_formats(self):
        (self.root / "legacy_results.csv").write_bytes((DATA / "results.csv").read_bytes())
        result = self.cli(results=self.root / "legacy_results.csv")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mixed calibration/result formats", result.stderr)
        self.assertFalse((self.root / "out/report.json").exists())


if __name__ == "__main__":
    unittest.main()
