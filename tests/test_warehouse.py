"""Automated tests for the longitudinal SQLite result warehouse.

All fixtures are original synthetic data generated through the project's own
CLI/pipeline inside temporary directories. No real laboratory data, runtime
database or generated report is committed.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from assay_insights.report import run
from assay_insights.warehouse import (
    ConflictError, IdentityError, ManifestError, ReportValidationError,
    WarehouseError, build_query, import_manifest, render_query, write_query,
)
from assay_insights.warehouse.reports import load_report
from assay_insights.warehouse.schema import WAREHOUSE_SCHEMA_VERSION

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data"


class WarehouseFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pkg = self.root / "package"
        self.pkg.mkdir()
        self.db = self.root / "warehouse.sqlite"
        self.legacy_dir = self.root / "legacy-run"
        self.timed_dir = self.root / "timed-run"
        run(DATA / "calibrations.csv", DATA / "results.csv",
            DATA / "policy.json", self.legacy_dir)
        run(DATA / "timed_calibrations.csv", DATA / "timed_results.csv",
            DATA / "policy.json", self.timed_dir)
        self.legacy_report = self.legacy_dir / "report.json"
        self.timed_report = self.timed_dir / "report.json"
        self.legacy_sha = hashlib.sha256(self.legacy_report.read_bytes()).hexdigest()
        self.timed_sha = hashlib.sha256(self.timed_report.read_bytes()).hexdigest()

    def make_package_dir(self, name, reports):
        """Create a manifest directory with report copies and return its path."""
        directory = self.pkg / name
        directory.mkdir(parents=True, exist_ok=True)
        entries = []
        for index, (source, label) in enumerate(reports):
            sub = directory / f"report-{index}"
            sub.mkdir()
            target = sub / "report.json"
            shutil.copy(source, target)
            entries.append({"path": f"report-{index}/report.json", "label": label})
        return directory, entries

    def write_manifest(self, directory, manifest):
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return path

    def import_package(self, name, import_id, reports, corrections=None,
                       manifest_name="manifest.json"):
        directory, entries = self.make_package_dir(name, reports)
        manifest = {
            "schema_version": 1,
            "import_id": import_id,
            "action": "apply",
            "reports": entries,
            "corrections": corrections or [],
        }
        path = directory / manifest_name
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return import_manifest(self.db, path), path, directory

    def table_counts(self):
        connection = sqlite3.connect(self.db)
        try:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "knowledge_ledger", "imports", "canonical_fact_sets",
                    "report_snapshots", "import_reports", "fact_sample_groups",
                    "fact_qc_groups", "fact_qc_scopes", "fact_batches",
                    "fact_calibrations", "fact_measurements", "sample_identities",
                    "corrections",
                )
            }
        finally:
            connection.close()

    def latest_seq(self):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(
                "SELECT COALESCE(MAX(knowledge_seq), 0) FROM knowledge_ledger"
            ).fetchone()[0]
        finally:
            connection.close()


class ImportAndFactTests(WarehouseFixture):
    def test_legacy_and_timed_imports_preserve_original_decisions(self):
        self.import_package("a", "IMP-001", [(self.legacy_report, "legacy")])
        self.import_package("b", "IMP-002", [(self.timed_report, "timed")])
        query = build_query(self.db)
        reports = query["original_view"]["reports"]
        legacy = next(r for r in reports if r["schema_version"] == 1)
        timed = next(r for r in reports if r["schema_version"] == 2)
        self.assertEqual(legacy["summary"],
                         {"sample_groups": 3, "reportable_groups": 2})
        self.assertEqual(len(legacy["samples"]), 3)
        s1 = next(s for s in legacy["samples"] if s["sample"] == "S1")
        self.assertEqual(s1["mean_mg_l"], "4.600000")
        self.assertTrue(s1["reportable"])
        self.assertEqual(timed["summary"],
                         {"sample_groups": 8, "reportable_groups": 2})
        self.assertEqual(len(timed["qc_scopes"]), 7)
        s4 = next(s for s in timed["samples"] if s["sample"] == "S4")
        self.assertEqual(s4["reasons"], ["qc_failed:N-NEW"])
        self.assertFalse(s4["reportable"])
        # Legacy facts stay legacy: no fabricated time or calibration version.
        self.assertEqual(legacy["qc_scopes"], [])
        outer = query["longitudinal_changes"]
        legacy_chain = next(
            c for c in outer["chains"]
            if (c["batch"], c["analyte"], c["original_sample"])
            == ("B1", "nitrate", "S1")
        )
        self.assertEqual(legacy_chain["snapshots"][0]["report_sha256s"], [self.legacy_sha])

    def test_continuous_knowledge_seq_and_as_of_visibility(self):
        self.import_package("a", "IMP-001", [(self.legacy_report, "legacy")])
        self.import_package("b", "IMP-002", [(self.timed_report, "timed")])
        first = build_query(self.db, knowledge_seq=1)
        self.assertEqual(first["knowledge_seq"], 1)
        self.assertEqual(len(first["original_view"]["reports"]), 1)
        only_report = first["original_view"]["reports"][0]
        self.assertEqual(only_report["schema_version"], 1)
        self.assertEqual(first["sources"]["corrections"], [])
        latest = build_query(self.db)
        self.assertEqual(latest["knowledge_seq"], 2)
        self.assertEqual(
            sorted(r["schema_version"] for r in latest["original_view"]["reports"]),
            [1, 2],
        )
        seqs = [row["knowledge_seq"] for row in latest["sources"]["imports"]]
        self.assertEqual(seqs, [1, 2])
        with self.assertRaises(WarehouseError):
            build_query(self.db, knowledge_seq=3)
        with self.assertRaises(WarehouseError):
            build_query(self.db, knowledge_seq=0)

    def test_original_cli_outputs_remain_byte_stable(self):
        before_json = self.legacy_report.read_bytes()
        before_html = (self.legacy_dir / "report.html").read_bytes()
        run(DATA / "calibrations.csv", DATA / "results.csv",
            DATA / "policy.json", self.legacy_dir)
        self.assertEqual(before_json, self.legacy_report.read_bytes())
        self.assertEqual(before_html, (self.legacy_dir / "report.html").read_bytes())


class IdempotencyAndConflictTests(WarehouseFixture):
    def test_same_bytes_replay_is_idempotent(self):
        _, path, _ = self.import_package("a", "IMP-001", [(self.legacy_report, "legacy")])
        before = self.table_counts()
        result = import_manifest(self.db, path)
        self.assertTrue(result["idempotent_replay"])
        self.assertIsNone(result["knowledge_seq"])
        self.assertEqual(before, self.table_counts())
        self.assertEqual(self.latest_seq(), 1)

    def test_same_import_id_changed_label_conflicts(self):
        _, path, directory = self.import_package(
            "a", "IMP-001", [(self.legacy_report, "legacy")]
        )
        manifest = json.loads(path.read_text())
        manifest["reports"][0]["label"] = "changed"
        path.write_text(json.dumps(manifest))
        before = self.table_counts()
        with self.assertRaises(ConflictError):
            import_manifest(self.db, path)
        self.assertEqual(before, self.table_counts())

    def test_same_import_id_changed_report_bytes_conflicts(self):
        _, path, directory = self.import_package(
            "a", "IMP-001", [(self.legacy_report, "legacy")]
        )
        report_path = directory / "report-0" / "report.json"
        report = json.loads(report_path.read_text())
        report["summary"]["sample_groups"] = 99
        report_path.write_text(json.dumps(report))
        with self.assertRaises(WarehouseError):
            import_manifest(self.db, path)
        self.assertEqual(self.latest_seq(), 1)

    def test_same_import_id_changed_corrections_conflict(self):
        _, first_path, directory = self.import_package(
            "a", "IMP-001", [(self.legacy_report, "legacy")]
        )
        correction = {
            "correction_id": "COR-1",
            "target": {"report_sha256": self.legacy_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "rename_sample", "new_sample": "S1-R",
            "reason": "synthetic transcription correction",
        }
        # Same import_id, now carrying a correction: different manifest bytes.
        _, entries = self.make_package_dir("b", [(self.legacy_report, "legacy")])
        second_manifest = {
            "schema_version": 1, "import_id": "IMP-001", "action": "apply",
            "reports": entries, "corrections": [correction],
        }
        second_path = self.write_manifest(self.pkg / "b", second_manifest)
        before = self.table_counts()
        with self.assertRaises(ConflictError):
            import_manifest(self.db, second_path)
        self.assertEqual(before, self.table_counts())

    def test_different_import_reusing_report_adds_only_source_link(self):
        self.import_package("a", "IMP-001", [(self.timed_report, "timed")])
        before = self.table_counts()
        result, _, _ = self.import_package("b", "IMP-002", [(self.timed_report, "timed-again")])
        after = self.table_counts()
        self.assertEqual(result["created_kind"], "source_only_transaction")
        self.assertEqual(after["report_snapshots"], before["report_snapshots"])
        self.assertEqual(after["fact_sample_groups"], before["fact_sample_groups"])
        self.assertEqual(after["fact_measurements"], before["fact_measurements"])
        self.assertEqual(after["sample_identities"], before["sample_identities"])
        self.assertEqual(after["import_reports"], before["import_reports"] + 1)
        self.assertEqual(after["knowledge_ledger"], before["knowledge_ledger"] + 1)
        # Both source links remain queryable on the single canonical snapshot.
        query = build_query(self.db)
        snapshot = next(
            s for s in query["sources"]["report_snapshots"]
            if s["report_sha256"] == self.timed_sha
        )
        self.assertEqual(
            sorted(link["label"] for link in snapshot["source_links"]),
            ["timed", "timed-again"],
        )


class CorrectionLifecycleTests(WarehouseFixture):
    def _rename_manifest(self, name, import_id, correction_id, new_sample, reason):
        correction = {
            "correction_id": correction_id,
            "target": {"report_sha256": self.legacy_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "rename_sample", "new_sample": new_sample, "reason": reason,
        }
        return self.import_package(name, import_id,
                                   [(self.legacy_report, f"label-{import_id}")], [correction])

    def test_rename_twice_then_withdraw_keeps_full_history(self):
        self.import_package("base", "IMP-000", [(self.legacy_report, "base")])
        self._rename_manifest("r1", "IMP-001", "COR-001", "S1-R",
                              "synthetic transcription correction")
        self._rename_manifest("r2", "IMP-002", "COR-002", "S1-FINAL",
                              "second synthetic correction")
        withdraw = {
            "correction_id": "COR-003",
            "target": {"report_sha256": self.legacy_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "withdraw_sample",
            "reason": "synthetic withdrawal",
        }
        self.import_package("w", "IMP-003", [(self.legacy_report, "w-label")], [withdraw])

        q1 = build_query(self.db, knowledge_seq=1)
        s1_q1 = self._s1(q1)
        self.assertEqual(s1_q1["current_sample"], "S1")
        self.assertFalse(s1_q1["withdrawn"])

        q2 = build_query(self.db, knowledge_seq=2)
        self.assertEqual(self._s1(q2)["current_sample"], "S1-R")
        q3 = build_query(self.db, knowledge_seq=3)
        s1_q3 = self._s1(q3)
        self.assertEqual(s1_q3["current_sample"], "S1-FINAL")
        self.assertEqual(
            [h["correction_id"] for h in s1_q3["correction_history"]],
            ["COR-001", "COR-002"],
        )
        self.assertEqual(s1_q3["original_identity"]["sample"], "S1")

        q4 = build_query(self.db, knowledge_seq=4)
        s1_q4 = self._s1(q4)
        self.assertTrue(s1_q4["withdrawn"])
        # Withdrawn sample leaves the visible longitudinal summary but the
        # original chain and raw report remain intact.
        names = {
            (g["batch"], g["analyte"], g["sample"])
            for g in q4["longitudinal_summary"]["groups"]
        }
        self.assertNotIn(("B1", "nitrate", "S1-FINAL"), names)
        chain = next(
            c for c in q4["longitudinal_changes"]["chains"]
            if (c["batch"], c["analyte"], c["original_sample"])
            == ("B1", "nitrate", "S1")
        )
        self.assertEqual(chain["snapshots"][0]["report_sha256s"], [self.legacy_sha])
        raw = self._raw_snapshot(q4)
        self.assertEqual(raw["report_sha256"], self.legacy_sha)

    def _s1(self, query):
        return next(
            s for s in query["corrected_view"]["samples"]
            if s["original_identity"] == {
                "report_sha256": self.legacy_sha, "batch": "B1",
                "analyte": "nitrate", "sample": "S1",
            }
        )

    def _raw_snapshot(self, query):
        return next(
            r for r in query["original_view"]["reports"]
            if r["report_sha256"] == self.legacy_sha
        )

    def test_correction_in_same_package_as_new_report(self):
        correction = {
            "correction_id": "COR-NEW",
            "target": {"report_sha256": self.legacy_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "rename_sample", "new_sample": "S1-NEW",
            "reason": "same-package synthetic correction",
        }
        result, _, _ = self.import_package(
            "same", "IMP-SAME", [(self.legacy_report, "same")], [correction]
        )
        self.assertEqual(result["knowledge_seq"], 1)
        query = build_query(self.db)
        self.assertEqual(self._s1(query)["current_sample"], "S1-NEW")

    def test_rename_collision_rolls_back(self):
        self.import_package("base", "IMP-BASE", [(self.timed_report, "base")])
        correction = {
            "correction_id": "COR-COL",
            "target": {"report_sha256": self.timed_sha, "batch": "B1",
                       "sample": "S2", "analyte": "nitrate"},
            "operation": "rename_sample", "new_sample": "S1",
            "reason": "synthetic collision",
        }
        before = self.table_counts()
        with self.assertRaises(IdentityError):
            self.import_package("col", "IMP-COL", [(self.timed_report, "t")], [correction])
        self.assertEqual(before, self.table_counts())
        self.assertEqual(self.latest_seq(), 1)

    def test_qc_target_and_unknown_report_rejected(self):
        qc_correction = {
            "correction_id": "COR-QC",
            "target": {"report_sha256": self.legacy_sha, "batch": "B1",
                       "sample": "Q1", "analyte": "nitrate"},
            "operation": "withdraw_sample", "reason": "cannot target QC",
        }
        with self.assertRaises(IdentityError):
            self.import_package("qc", "IMP-QC", [(self.legacy_report, "l")], [qc_correction])
        unknown = dict(qc_correction, correction_id="COR-U")
        unknown["target"] = {"report_sha256": "a" * 64, "batch": "B1",
                             "sample": "S1", "analyte": "nitrate"}
        with self.assertRaises(IdentityError):
            self.import_package("u", "IMP-U", [(self.legacy_report, "l2")], [unknown])

    def test_duplicate_and_conflicting_correction_id_rejected(self):
        first = {
            "correction_id": "COR-DUP",
            "target": {"report_sha256": self.legacy_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "withdraw_sample", "reason": "first",
        }
        self.import_package("a", "IMP-A", [(self.legacy_report, "a")], [first])
        second = dict(first, reason="different content")
        second["target"] = dict(first["target"], sample="S2")
        with self.assertRaises(ConflictError):
            self.import_package("b", "IMP-B", [(self.legacy_report, "b")], [second])
        # Two identical correction_ids inside one manifest are an envelope error.
        with self.assertRaises(ManifestError):
            self.import_package(
                "c", "IMP-C", [(self.legacy_report, "c")], [first, dict(first)]
            )

    def test_rename_after_withdrawal_rejected(self):
        withdraw = {
            "correction_id": "COR-W",
            "target": {"report_sha256": self.legacy_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "withdraw_sample", "reason": "withdraw",
        }
        self.import_package("w", "IMP-W", [(self.legacy_report, "w")], [withdraw])
        rename = {
            "correction_id": "COR-RW",
            "target": {"report_sha256": self.legacy_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "rename_sample", "new_sample": "S1-Z", "reason": "late",
        }
        with self.assertRaises(IdentityError):
            self.import_package("rw", "IMP-RW", [(self.legacy_report, "rw")], [rename])


    def test_correction_can_target_a_previously_imported_report(self):
        self.import_package("base", "IMP-BASE", [(self.legacy_report, "base")])
        # A later package re-links the same report and corrects it; the target
        # hash is known from the earlier committed package.
        correction = {
            "correction_id": "COR-LATE",
            "target": {"report_sha256": self.legacy_sha, "batch": "B2",
                       "sample": "S2", "analyte": "nitrate"},
            "operation": "withdraw_sample", "reason": "late synthetic withdrawal",
        }
        result, _, _ = self.import_package(
            "late", "IMP-LATE", [(self.legacy_report, "again")], [correction]
        )
        self.assertEqual(result["new_corrections"], ["COR-LATE"])
        query = build_query(self.db)
        target = next(
            s for s in query["corrected_view"]["samples"]
            if s["original_identity"]["report_sha256"] == self.legacy_sha
            and s["original_identity"]["sample"] == "S2"
        )
        self.assertTrue(target["withdrawn"])

    def test_manifest_and_report_bytes_are_captured_once(self):
        import builtins
        import assay_insights.warehouse.importer as importer
        directory, entries = self.make_package_dir(
            "once", [(self.legacy_report, "l"), (self.timed_report, "t")]
        )
        manifest = {
            "schema_version": 1, "import_id": "IMP-ONCE", "action": "apply",
            "reports": entries, "corrections": [],
        }
        path = self.write_manifest(directory, manifest)
        opened = []
        real_open = builtins.open

        def counting_open(candidate, *args, **kwargs):
            opened.append(str(candidate))
            return real_open(candidate, *args, **kwargs)

        builtins.open = counting_open
        try:
            package = importer.capture_manifest(path)
        finally:
            builtins.open = real_open
        self.assertEqual(opened.count(str(path)), 1)
        report_paths = [
            str(directory / "report-0" / "report.json"),
            str(directory / "report-1" / "report.json"),
        ]
        for report_path in report_paths:
            self.assertEqual(opened.count(report_path), 1)
        self.assertEqual(len(package["reports"]), 2)


class ReportValidationTests(WarehouseFixture):
    def _tampered(self, mutator):
        directory = self.root / "tampered"
        directory.mkdir(exist_ok=True)
        report_path = directory / "report.json"
        shutil.copy(self.timed_report, report_path)
        report = json.loads(report_path.read_text())
        mutator(report)
        report_path.write_text(json.dumps(report, indent=2))
        return report_path

    def test_summary_mismatch_rejected(self):
        path = self._tampered(lambda r: r["summary"].__setitem__("sample_groups", 99))
        with self.assertRaises(ReportValidationError):
            self.import_package("t", "IMP-T", [(path, "t")])

    def test_qc_scope_inconsistency_rejected(self):
        def mutate(report):
            report["qc_scopes"][0]["status"] = "passed"
        with self.assertRaises(ReportValidationError):
            self.import_package("t", "IMP-T2", [(self._tampered(mutate), "t")])

    def test_measurement_trace_inconsistency_rejected(self):
        def mutate(report):
            report["measurements"][0]["calibration_id"] = "N-NEW"
        with self.assertRaises(ReportValidationError):
            self.import_package("t", "IMP-T3", [(self._tampered(mutate), "t")])

    def test_concentration_formula_tampering_rejected(self):
        def mutate(report):
            report["measurements"][0]["concentration_mg_l"] = "999.000000"
        with self.assertRaises(ReportValidationError):
            self.import_package("t", "IMP-T4", [(self._tampered(mutate), "t")])

    def test_external_summary_lookalike_rejected(self):
        lookalike = {
            "schema_version": 2,
            "summary": {"sample_groups": 1, "reportable_groups": 1},
        }
        path = self.root / "lookalike.json"
        path.write_text(json.dumps(lookalike))
        with self.assertRaises(ReportValidationError):
            load_report(path.read_bytes())

    def test_reordered_report_keeps_one_canonical_fact(self):
        import random
        report = json.loads(self.timed_report.read_text())
        shuffled = json.loads(json.dumps(report))
        random.seed(11)
        random.shuffle(shuffled["samples"])
        random.shuffle(shuffled["measurements"])
        shuffled_bytes = (json.dumps(shuffled, indent=2) + "\n").encode()
        original_loaded = load_report(self.timed_report.read_bytes())
        shuffled_loaded = load_report(shuffled_bytes)
        self.assertNotEqual(
            hashlib.sha256(self.timed_report.read_bytes()).hexdigest(),
            hashlib.sha256(shuffled_bytes).hexdigest(),
        )
        self.assertEqual(
            original_loaded["canonical_sha256"],
            shuffled_loaded["canonical_sha256"],
        )
        # Importing both byte variants must not amplify facts.
        shuffled_path = self.root / "shuffled.json"
        shuffled_path.write_bytes(shuffled_bytes)
        self.import_package("a", "IMP-A", [(self.timed_report, "ordered")])
        before = self.table_counts()
        self.import_package("b", "IMP-B", [(shuffled_path, "shuffled")])
        after = self.table_counts()
        # One new raw snapshot/source link/identity set, but the canonical fact
        # set and every fact_* row stay shared.
        self.assertEqual(after["report_snapshots"], before["report_snapshots"] + 1)
        self.assertEqual(
            after["canonical_fact_sets"], before["canonical_fact_sets"]
        )
        self.assertEqual(after["fact_sample_groups"], before["fact_sample_groups"])
        self.assertEqual(after["fact_measurements"], before["fact_measurements"])
        self.assertEqual(
            after["sample_identities"],
            before["sample_identities"] + before["fact_sample_groups"],
        )
        query = build_query(self.db)
        chain = next(
            c for c in query["longitudinal_changes"]["chains"]
            if (c["batch"], c["analyte"], c["original_sample"])
            == ("B1", "nitrate", "S1")
        )
        # Canonical-identical snapshots collapse to one longitudinal node.
        self.assertEqual(len(chain["snapshots"]), 1)
        self.assertEqual(len(chain["snapshots"][0]["report_sha256s"]), 2)


class LongitudinalCounterexampleTests(WarehouseFixture):
    def _custom_timed_report(self):
        work = self.root / "ce-inputs"
        work.mkdir(exist_ok=True)
        shutil.copy(DATA / "timed_calibrations.csv", work / "calibrations.csv")
        shutil.copy(DATA / "policy.json", work / "policy.json")
        text = (DATA / "timed_results.csv").read_text()
        text = text.replace("B1,S1,nitrate,1,sample,10,1,",
                            "B1,S1,nitrate,1,sample,9,1,")
        text = text.replace("B1,S1,nitrate,2,sample,10.4,1,",
                            "B1,S1,nitrate,2,sample,11.4,1,")
        (work / "results.csv").write_text(text)
        out = self.root / "ce-out"
        run(work / "calibrations.csv", work / "results.csv",
            work / "policy.json", out)
        return out / "report.json"

    def test_legacy_reportable_to_timed_unreportable_transition(self):
        custom = self._custom_timed_report()
        custom_report = json.loads(custom.read_text())
        s1 = next(s for s in custom_report["samples"]
                  if (s["batch"], s["sample"], s["analyte"])
                  == ("B1", "S1", "nitrate"))
        self.assertEqual(s1["mean_mg_l"], "4.600000")
        self.assertFalse(s1["reportable"])
        self.assertEqual(s1["calibration_ids"], ["N-OLD"])

        self.import_package("leg", "CE-1", [(self.legacy_report, "legacy")])
        self.import_package("tm", "CE-2", [(custom, "timed-ce")])
        query = build_query(self.db)
        chain = next(
            c for c in query["longitudinal_changes"]["chains"]
            if (c["batch"], c["analyte"], c["original_sample"])
            == ("B1", "nitrate", "S1")
        )
        transition = chain["transitions"][0]
        self.assertFalse(transition["changes"]["mean_changed"])
        self.assertTrue(transition["changes"]["reportable_changed"])
        self.assertTrue(transition["changes"]["reasons_changed"])
        self.assertTrue(transition["changes"]["calibration_ids_changed"])
        self.assertEqual(transition["from"]["mean_mg_l"], "4.600000")
        self.assertTrue(transition["from"]["reportable"])
        self.assertEqual(transition["from"]["calibration_ids"], [])
        self.assertFalse(transition["to"]["reportable"])
        self.assertEqual(transition["to"]["reasons"], ["replicate_rsd_exceeded"])
        self.assertEqual(transition["to"]["calibration_ids"], ["N-OLD"])

    def test_corrections_do_not_alter_original_change_chain(self):
        custom = self._custom_timed_report()
        custom_sha = hashlib.sha256(custom.read_bytes()).hexdigest()
        self.import_package("leg", "CE-1", [(self.legacy_report, "legacy")])
        self.import_package("tm", "CE-2", [(custom, "timed-ce")])
        rename = {
            "correction_id": "COR-CE",
            "target": {"report_sha256": custom_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "rename_sample", "new_sample": "S1-RENAMED",
            "reason": "display-only synthetic correction",
        }
        self.import_package("cor", "CE-3", [(custom, "again")], [rename])
        query = build_query(self.db)
        chain = next(
            c for c in query["longitudinal_changes"]["chains"]
            if c["original_sample"] == "S1" and c["batch"] == "B1"
            and c["analyte"] == "nitrate"
        )
        # The original chain still uses the immutable original key and facts.
        self.assertEqual(chain["original_sample"], "S1")
        self.assertEqual(len(chain["transitions"]), 1)
        corrected = next(
            s for s in query["corrected_view"]["samples"]
            if s["original_identity"]["report_sha256"] == custom_sha
            and s["original_identity"]["sample"] == "S1"
        )
        self.assertEqual(corrected["current_sample"], "S1-RENAMED")


class QueryPublicationTests(WarehouseFixture):
    def test_query_is_byte_deterministic_and_filtered(self):
        self.import_package("a", "IMP-001", [(self.legacy_report, "legacy")])
        self.import_package("b", "IMP-002", [(self.timed_report, "timed")])
        first = render_query(build_query(self.db))
        second = render_query(build_query(self.db))
        self.assertEqual(first, second)
        self.assertNotIn(str(self.root), first)
        filtered = build_query(self.db, batch="B2")
        self.assertEqual(filtered["batch_filter"], "B2")
        self.assertTrue(all(
            s["batch"] == "B2"
            for r in filtered["original_view"]["reports"]
            for s in r["samples"]
        ))

    def test_safe_replace_and_failure_keeps_existing_output(self):
        self.import_package("a", "IMP-001", [(self.legacy_report, "legacy")])
        output = self.root / "nested" / "query.json"
        output.parent.mkdir()
        write_query(build_query(self.db), output)
        before = output.read_bytes()
        write_query(build_query(self.db), output)
        self.assertEqual(before, output.read_bytes())
        leftovers = list(output.parent.glob(".warehouse-query-*.tmp"))
        self.assertEqual(leftovers, [])
        # A missing output directory must fail without touching existing files.
        with self.assertRaises(WarehouseError):
            write_query(build_query(self.db), self.root / "no-such-dir" / "q.json")
        self.assertEqual(before, output.read_bytes())

    def test_report_only_does_not_create_database(self):
        missing = self.root / "never-created.sqlite"
        self.assertFalse(missing.exists())
        with self.assertRaises(WarehouseError):
            build_query(missing)
        self.assertFalse(missing.exists())

    def test_incompatible_database_is_rejected(self):
        other = self.root / "other.sqlite"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE unrelated(x)")
        connection.commit()
        connection.close()
        with self.assertRaises(WarehouseError):
            build_query(other)

    def test_manifest_path_security(self):
        _, entries = self.make_package_dir("secure", [(self.legacy_report, "l")])
        manifest = {
            "schema_version": 1, "import_id": "IMP-SEC", "action": "apply",
            "reports": [{"path": "/etc/passwd", "label": "abs"}],
            "corrections": [],
        }
        path = self.write_manifest(self.pkg / "secure", manifest)
        with self.assertRaises(ManifestError):
            import_manifest(self.db, path)
        manifest["reports"][0]["path"] = "../escape.json"
        path = self.write_manifest(self.pkg / "secure", manifest)
        with self.assertRaises(ManifestError):
            import_manifest(self.db, path)
        link = self.pkg / "secure" / "link.json"
        os.symlink(self.legacy_report, link)
        manifest["reports"][0]["path"] = "link.json"
        path = self.write_manifest(self.pkg / "secure", manifest)
        with self.assertRaises(ManifestError):
            import_manifest(self.db, path)
        # A symlinked intermediate directory component is also rejected.
        linked_dir = self.pkg / "secure" / "linked-dir"
        os.symlink(self.pkg / "secure", linked_dir)
        manifest["reports"][0]["path"] = "linked-dir/payload/report.json"
        path = self.write_manifest(self.pkg / "secure", manifest)
        with self.assertRaises(ManifestError):
            import_manifest(self.db, path)

    def test_identifier_and_envelope_rules(self):
        for bad_schema in (True, "1", 2, None):
            directory, entries = self.make_package_dir(
                f"env-{str(bad_schema)}", [(self.legacy_report, "l")]
            )
            manifest = {
                "schema_version": bad_schema, "import_id": "IMP-X",
                "action": "apply", "reports": entries, "corrections": [],
            }
            path = self.write_manifest(directory, manifest)
            with self.assertRaises(ManifestError):
                import_manifest(self.db, path)
        directory, entries = self.make_package_dir("trim", [(self.legacy_report, "l")])
        entries[0]["label"] = " spaced "
        manifest = {"schema_version": 1, "import_id": "IMP-T", "action": "apply",
                    "reports": entries, "corrections": []}
        path = self.write_manifest(directory, manifest)
        with self.assertRaises(ManifestError):
            import_manifest(self.db, path)


class ProvenanceTests(WarehouseFixture):
    def test_database_is_self_contained_after_sources_leave(self):
        _, manifest_path, directory = self.import_package(
            "a", "IMP-001", [(self.legacy_report, "l")]
        )
        query_before = build_query(self.db)
        shutil.rmtree(directory)
        query_after = build_query(self.db)
        self.assertEqual(render_query(query_before), render_query(query_after))
        connection = sqlite3.connect(self.db)
        raw_row = connection.execute(
            "SELECT report_raw FROM report_snapshots"
        ).fetchone()
        canonical_row = connection.execute(
            "SELECT canonical_json FROM canonical_fact_sets"
        ).fetchone()
        connection.close()
        self.assertEqual(
            hashlib.sha256(raw_row[0]).hexdigest(), self.legacy_sha
        )
        canonical = json.loads(canonical_row[0].decode("utf-8"))
        self.assertEqual(canonical["summary"],
                         {"sample_groups": 3, "reportable_groups": 2})

    def test_no_absolute_paths_or_rowids_in_output(self):
        self.import_package("a", "IMP-001", [(self.legacy_report, "l")])
        text = render_query(build_query(self.db))
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("rowid", text.lower())
        payload = json.loads(text)
        self.assertEqual(payload["warehouse_schema"], WAREHOUSE_SCHEMA_VERSION)
        self.assertIn("synthetic", payload["notice"])


class WarehouseCliTests(WarehouseFixture):
    def cli(self, *extra):
        args = [sys.executable, "-m", "assay_insights.warehouse", *map(str, extra)]
        env = dict(os.environ, PYTHONPATH=str(PROJECT))
        return subprocess.run(args, cwd=PROJECT, capture_output=True, text=True, env=env)

    def _write_cli_package(self, name, report_source, import_id, label, corrections=None):
        directory = self.pkg / name
        (directory / "payload").mkdir(parents=True)
        shutil.copy(report_source, directory / "payload" / "report.json")
        manifest = {
            "schema_version": 1, "import_id": import_id, "action": "apply",
            "reports": [{"path": "payload/report.json", "label": label}],
            "corrections": corrections or [],
        }
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest_path

    def test_end_to_end_cli_generation_import_query_correction(self):
        # Generate both generations through the real analysis CLI.
        legacy_out = self.root / "cli-legacy"
        timed_out = self.root / "cli-timed"
        legacy_run = subprocess.run(
            [sys.executable, "-m", "assay_insights",
             "--calibrations", str(DATA / "calibrations.csv"),
             "--results", str(DATA / "results.csv"),
             "--policy", str(DATA / "policy.json"),
             "--output", str(legacy_out)],
            cwd=PROJECT, capture_output=True, text=True,
            env=dict(os.environ, PYTHONPATH=str(PROJECT)),
        )
        self.assertEqual(legacy_run.returncode, 0, legacy_run.stderr)
        timed_run = subprocess.run(
            [sys.executable, "-m", "assay_insights",
             "--calibrations", str(DATA / "timed_calibrations.csv"),
             "--results", str(DATA / "timed_results.csv"),
             "--policy", str(DATA / "policy.json"),
             "--output", str(timed_out)],
            cwd=PROJECT, capture_output=True, text=True,
            env=dict(os.environ, PYTHONPATH=str(PROJECT)),
        )
        self.assertEqual(timed_run.returncode, 0, timed_run.stderr)

        m1 = self._write_cli_package("c1", legacy_out / "report.json", "CLI-1", "legacy")
        m2 = self._write_cli_package("c2", timed_out / "report.json", "CLI-2", "timed")
        first = self.cli("--db", self.db, "--manifest", m1)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('"knowledge_seq": 1', first.stdout)
        second = self.cli("--db", self.db, "--manifest", m2)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('"knowledge_seq": 2', second.stdout)

        # Query the historical first knowledge sequence as a report-only run.
        old_output = self.root / "old.json"
        old_query = self.cli("--db", self.db, "--report",
                             "--knowledge-seq", "1", "--output", old_output)
        self.assertEqual(old_query.returncode, 0, old_query.stderr)
        old_payload = json.loads(old_output.read_text())
        self.assertEqual(old_payload["knowledge_seq"], 1)
        self.assertEqual(len(old_payload["original_view"]["reports"]), 1)
        self.assertEqual(
            old_payload["original_view"]["reports"][0]["schema_version"], 1
        )

        # Apply a correction in a third package, then query the latest version.
        legacy_sha = hashlib.sha256(
            (legacy_out / "report.json").read_bytes()
        ).hexdigest()
        correction = {
            "correction_id": "CLI-COR-1",
            "target": {"report_sha256": legacy_sha, "batch": "B1",
                       "sample": "S1", "analyte": "nitrate"},
            "operation": "rename_sample", "new_sample": "S1-CLI",
            "reason": "end-to-end synthetic correction",
        }
        m3 = self._write_cli_package(
            "c3", legacy_out / "report.json", "CLI-3", "legacy-again", [correction]
        )
        third = self.cli("--db", self.db, "--manifest", m3)
        self.assertEqual(third.returncode, 0, third.stderr)
        latest_output = self.root / "latest.json"
        latest_query = self.cli("--db", self.db, "--report", "--output", latest_output)
        self.assertEqual(latest_query.returncode, 0, latest_query.stderr)
        latest = json.loads(latest_output.read_text())
        corrected = next(
            s for s in latest["corrected_view"]["samples"]
            if s["original_identity"]["report_sha256"] == legacy_sha
            and s["original_identity"]["sample"] == "S1"
        )
        self.assertEqual(corrected["current_sample"], "S1-CLI")

    def test_report_only_missing_db_does_not_create_file(self):
        missing = self.root / "missing.sqlite"
        result = self.cli("--db", missing, "--report")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertFalse(missing.exists())

    def test_report_only_failure_keeps_existing_output(self):
        self._write_cli_package("c", self.legacy_report, "CLI-X", "l")
        manifest = self.pkg / "c" / "manifest.json"
        self.assertEqual(self.cli("--db", self.db, "--manifest", manifest).returncode, 0)
        output = self.root / "stable.json"
        good = self.cli("--db", self.db, "--report", "--output", output)
        self.assertEqual(good.returncode, 0, good.stderr)
        before = output.read_bytes()
        # A future knowledge sequence fails and must not overwrite the output.
        bad = self.cli("--db", self.db, "--report",
                       "--knowledge-seq", "99", "--output", output)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(before, output.read_bytes())

    def test_bad_manifest_is_nonzero_and_leaves_database_untouched(self):
        manifest = self.pkg / "bad" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"schema_version": True}))
        result = self.cli("--db", self.db, "--manifest", manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
