"""Controlled-concurrency tests for the warehouse write transaction.

Synchronization uses a multiprocessing barrier (and the SQLite write-lock
boundary), never timed sleeps. Two distinct packages must serialize into a
unique contiguous ledger; concurrent replays of one identical package must
produce at most one fact transaction with every other process observing an
idempotent replay.
"""
import json
import multiprocessing as mp
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from assay_insights.report import run
from assay_insights.warehouse import build_query, import_manifest

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data"


def _import_worker(db_path, manifest_path, barrier, outcome):
    """Child entry point: synchronize, then import, capturing the outcome."""
    try:
        barrier.wait()
        result = import_manifest(Path(db_path), Path(manifest_path))
        outcome.put(("ok", result))
    except Exception as exc:  # reported back to the parent for assertion
        outcome.put(("error", f"{type(exc).__name__}: {exc}"))


def _make_package(root, name, report_source, import_id, label):
    directory = root / name
    (directory / "payload").mkdir(parents=True)
    shutil.copy(report_source, directory / "payload" / "report.json")
    manifest = {
        "schema_version": 1,
        "import_id": import_id,
        "action": "apply",
        "reports": [{"path": "payload/report.json", "label": label}],
        "corrections": [],
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy_dir = self.root / "legacy"
        self.timed_dir = self.root / "timed"
        run(DATA / "calibrations.csv", DATA / "results.csv",
            DATA / "policy.json", self.legacy_dir)
        run(DATA / "timed_calibrations.csv", DATA / "timed_results.csv",
            DATA / "policy.json", self.timed_dir)
        self.db = self.root / "warehouse.sqlite"
        try:
            self.ctx = mp.get_context("fork")
        except ValueError:  # pragma: no cover - platforms without fork
            self.ctx = None

    def _spawn(self, target, args):
        process = self.ctx.Process(target=target, args=args)
        process.start()
        return process

    def test_two_distinct_packages_serialize_to_contiguous_ledger(self):
        if self.ctx is None:
            self.skipTest("fork start method unavailable")
        manifest_a = _make_package(
            self.root, "pkg-a", self.legacy_dir / "report.json", "IMP-A", "a"
        )
        manifest_b = _make_package(
            self.root, "pkg-b", self.timed_dir / "report.json", "IMP-B", "b"
        )
        barrier = self.ctx.Barrier(2)
        outcomes = self.ctx.Queue()
        processes = [
            self._spawn(_import_worker, (str(self.db), str(manifest_a), barrier, outcomes)),
            self._spawn(_import_worker, (str(self.db), str(manifest_b), barrier, outcomes)),
        ]
        for process in processes:
            process.join(30)
            self.assertFalse(process.is_alive(), "import process hung")
            self.assertEqual(process.exitcode, 0)

        results = [outcomes.get() for _ in range(2)]
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        seqs = sorted(payload["knowledge_seq"] for _, payload in results)
        self.assertEqual(seqs, [1, 2])

        connection = sqlite3.connect(self.db)
        ledger = connection.execute(
            "SELECT knowledge_seq, import_id FROM knowledge_ledger ORDER BY knowledge_seq"
        ).fetchall()
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM report_snapshots"
        ).fetchone()[0]
        canonical_count = connection.execute(
            "SELECT COUNT(*) FROM canonical_fact_sets"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(ledger, [(1, "IMP-A"), (2, "IMP-B")])
        self.assertEqual(snapshot_count, 2)
        self.assertEqual(canonical_count, 2)
        # Both transactions are fully visible in the latest query.
        query = build_query(self.db)
        self.assertEqual(query["knowledge_seq"], 2)
        self.assertEqual(
            sorted(r["schema_version"] for r in query["original_view"]["reports"]),
            [1, 2],
        )

    def test_concurrent_replay_of_one_package_commits_once(self):
        if self.ctx is None:
            self.skipTest("fork start method unavailable")
        manifest = _make_package(
            self.root, "pkg", self.timed_dir / "report.json", "IMP-SAME", "only"
        )
        workers = 5
        barrier = self.ctx.Barrier(workers)
        outcomes = self.ctx.Queue()
        processes = [
            self._spawn(_import_worker, (str(self.db), str(manifest), barrier, outcomes))
            for _ in range(workers)
        ]
        for process in processes:
            process.join(30)
            self.assertFalse(process.is_alive(), "import process hung")
            self.assertEqual(process.exitcode, 0)

        results = [outcomes.get() for _ in range(workers)]
        self.assertTrue(all(kind == "ok" for kind, _ in results), results)
        fact_transactions = [
            payload for _, payload in results
            if not payload["idempotent_replay"]
        ]
        replays = [
            payload for _, payload in results
            if payload["idempotent_replay"]
        ]
        self.assertEqual(len(fact_transactions), 1)
        self.assertEqual(len(replays), workers - 1)
        self.assertEqual(fact_transactions[0]["knowledge_seq"], 1)
        self.assertTrue(all(payload["knowledge_seq"] is None for payload in replays))

        connection = sqlite3.connect(self.db)
        ledger_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_ledger"
        ).fetchone()[0]
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM report_snapshots"
        ).fetchone()[0]
        source_count = connection.execute(
            "SELECT COUNT(*) FROM import_reports"
        ).fetchone()[0]
        sample_count = connection.execute(
            "SELECT COUNT(*) FROM fact_sample_groups"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(ledger_count, 1)
        self.assertEqual(snapshot_count, 1)
        self.assertEqual(source_count, 1)
        self.assertEqual(sample_count, 8)  # no fact amplification under replay


if __name__ == "__main__":
    unittest.main()
