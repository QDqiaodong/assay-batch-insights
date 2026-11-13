import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from assay_insights.acquire import protocol, tester
from assay_insights.input import TIMED_RESULT_HEADERS, load

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data"

BATCH = "B1"
ROWS = [
    {"sample": "S1", "analyte": "nitrate", "replicate": "1", "kind": "sample",
     "signal": "10", "dilution": "1", "target_mg_l": "", "measured_at": "2026-03-01T08:00:00Z"},
    {"sample": "S1", "analyte": "nitrate", "replicate": "2", "kind": "sample",
     "signal": "10.4", "dilution": "1", "target_mg_l": "", "measured_at": "2026-03-01T08:05:00Z"},
    {"sample": "Q1", "analyte": "nitrate", "replicate": "1", "kind": "QC",
     "signal": "21", "dilution": "1", "target_mg_l": "10", "measured_at": "2026-03-01T09:00:00Z"},
]


def rows_with_batch(batch=BATCH, rows=ROWS):
    return [dict(row, batch=batch) for row in rows]


def expected_csv(rows):
    lines = [",".join(TIMED_RESULT_HEADERS)]
    lines += [",".join(row[header] for header in TIMED_RESULT_HEADERS) for row in rows]
    return "\n".join(lines) + "\n"


class ProtocolTests(unittest.TestCase):
    """In-process frame codec and strict message schema checks."""

    def parse_one(self, raw):
        buffer = bytearray(raw)
        frame = protocol.try_parse_frame(buffer)
        self.assertIsNotNone(frame)
        self.assertEqual(bytes(buffer), b"")
        return frame

    def test_frame_roundtrip_and_crc_detection(self):
        payload = b'{"type":"begin"}'
        frame = self.parse_one(protocol.encode_frame(payload))
        self.assertEqual(frame.payload, payload)
        self.assertTrue(frame.crc_ok)
        raw = bytearray(protocol.encode_frame(payload))
        raw[12] ^= 0x01  # flip one payload bit, envelope stays intact
        frame = self.parse_one(bytes(raw))
        self.assertFalse(frame.crc_ok)

    def test_split_and_sticky_parsing(self):
        one = protocol.encode_frame(b'{"a":1}')
        two = protocol.encode_frame(b'{"b":2}')
        buffer = bytearray()
        self.assertIsNone(protocol.try_parse_frame(buffer))
        for byte in one:  # dribbled byte by byte
            buffer.append(byte)
        frame = protocol.try_parse_frame(buffer)
        self.assertEqual(frame.raw, one)
        buffer.extend(one + two)  # sticky: two frames in one buffer
        self.assertEqual(protocol.try_parse_frame(buffer).raw, one)
        self.assertEqual(protocol.try_parse_frame(buffer).raw, two)
        self.assertEqual(bytes(buffer), b"")

    def test_envelope_structure_errors(self):
        for raw in [b"garbage",
                    b"@zzzzzzzz:{}*00000000\n",
                    b"@00000005" + bytes([0]) + b"{}*00000000\n",
                    b"@00000002:{}+00000000\n",
                    b"@00000002:{}*zzzzzzzz\n",
                    b"@00000002:{}*00000000\r"]:
            with self.assertRaises(Exception) as caught:
                self.parse_one(raw)
            self.assertEqual(caught.exception.code, "frame", raw)

    def test_declared_length_bounded(self):
        with self.assertRaisesRegex(Exception, "exceeds"):
            self.parse_one(b"@00010001:")
        buffer = bytearray(b"@00010000:")  # exactly 65536: waits, no error
        self.assertIsNone(protocol.try_parse_frame(buffer))

    def test_strict_message_schema(self):
        begin = json.loads(protocol.begin_payload("run-1", "B1", 1, "0" * 64))
        good = protocol.canonical_json(begin)
        protocol.parse_message(good)
        bad_messages = [
            {**begin, "extra": 1},                       # extra key
            {k: v for k, v in begin.items() if k != "batch"},  # missing key
            {**begin, "seq": True},                      # bool is not int
            {**begin, "count": "1"},                     # count must be int
            {**begin, "count": 0},                       # count must be positive
            {**begin, "run_id": "../x"},                 # traversal
            {**begin, "run_id": ".staging"},             # reserved dot name
            {**begin, "run_id": "a/b"},                  # separator
            {**begin, "run_id": "x" * 129},              # too long
            {**begin, "batch": "a\nb"},                  # control character
            {**begin, "calibration_sha256": "A" * 64},   # uppercase hex
            {**begin, "calibration_sha256": "0" * 63},   # wrong length
        ]
        for message in bad_messages:
            with self.assertRaises(Exception) as caught:
                protocol.parse_message(protocol.canonical_json(message))
            self.assertEqual(caught.exception.code, "frame", message)
        with self.assertRaisesRegex(Exception, "duplicate key"):
            protocol.parse_message(b'{"type":"begin","type":"begin"}')
        with self.assertRaisesRegex(Exception, "constant"):
            protocol.parse_message(b'{"type":"begin","seq":NaN}')
        with self.assertRaisesRegex(Exception, "UTF-8"):
            protocol.parse_message(b'{"type":"begin","\xff":1}')

    def test_measurement_and_end_schema(self):
        row = rows_with_batch()[0]
        good = protocol.measurement_payload(1, row)
        message = protocol.parse_message(good)
        self.assertEqual(message["sample"], "S1")
        bad = json.loads(good)
        bad["signal"] = 10  # CSV fields must be strings
        with self.assertRaisesRegex(Exception, "signal"):
            protocol.parse_message(protocol.canonical_json(bad))
        end = json.loads(protocol.end_payload(2, 1, "0" * 64))
        protocol.parse_message(protocol.canonical_json(end))
        end["measurements_sha256"] = "zz"
        with self.assertRaisesRegex(Exception, "measurements_sha256"):
            protocol.parse_message(protocol.canonical_json(end))

    def test_reply_parse(self):
        self.assertEqual(protocol.parse_reply(b"ACK 3\n"), ("ACK", 3, None))
        self.assertEqual(protocol.parse_reply(b"REJ 0 conflict\n"), ("REJ", 0, "conflict"))
        with self.assertRaises(ValueError):
            protocol.parse_reply(b"NOPE\n")


class AcquireFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calibrations = self.root / "calibrations.csv"
        self.calibrations.write_bytes((DATA / "timed_calibrations.csv").read_bytes())
        self.policy = self.root / "policy.json"
        self.policy.write_bytes((DATA / "policy.json").read_bytes())
        self.output = self.root / "out"
        self.cal_sha = hashlib.sha256(self.calibrations.read_bytes()).hexdigest()
        self.pol_sha = hashlib.sha256(self.policy.read_bytes()).hexdigest()
        self.master = self.slave = None
        self.instrument = None
        self.proc = None
        self.open_pty()

    def open_pty(self):
        self.close_pty()
        self.master, self.slave, self.slave_path = tester.open_pty()
        self.instrument = tester.Instrument(self.master)

    def close_pty(self):
        for fd in (self.master, self.slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master = self.slave = None

    def tearDown(self):
        self.close_pty()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def start_cli(self, *extra, timeout=5.0, wait_ready=True, calibrations=None, policy=None, output=None):
        args = [sys.executable, "-m", "assay_insights.acquire",
                "--device", self.slave_path,
                "--calibrations", str(calibrations or self.calibrations),
                "--policy", str(policy or self.policy),
                "--output", str(output or self.output),
                "--timeout", str(timeout), *extra]
        env = dict(os.environ, PYTHONPATH=str(PROJECT))
        self.proc = subprocess.Popen(args, cwd=PROJECT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if wait_ready:
            tester.wait_for_marker(self.proc.stderr, "listening", 15.0)
        return self.proc

    def finish(self, timeout=30.0):
        stdout, stderr = self.proc.communicate(timeout=timeout)
        return self.proc.returncode, stdout, stderr

    def run_dir(self, run_id="run-1"):
        return self.output / run_id

    def staging(self, run_id="run-1"):
        return self.output / ".staging" / run_id


class HappyPathTests(AcquireFixture):
    def test_full_run_publishes_and_summary_verifies_independently(self):
        self.start_cli()
        rows = rows_with_batch()
        frames = self.instrument.run_session("run-1", BATCH, rows, self.cal_sha)
        rc, stdout, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)
        run_dir = self.run_dir()
        summary = json.loads((run_dir / "summary.json").read_text())
        self.assertEqual(json.loads(stdout), summary)
        results_bytes = (run_dir / "results.csv").read_bytes()
        frames_bytes = (run_dir / "frames.log").read_bytes()
        payloads = [protocol.measurement_payload(seq, row) for seq, row in enumerate(rows, start=1)]
        # independent recomputation of every claimed digest
        self.assertEqual(summary, {
            "format": "assay-insights.serial-run/1",
            "run_id": "run-1",
            "batch": BATCH,
            "count": 3,
            "calibration_sha256": self.cal_sha,
            "policy_sha256": self.pol_sha,
            "measurements_sha256": protocol.measurements_digest(payloads),
            "results_sha256": hashlib.sha256(results_bytes).hexdigest(),
            "frames_sha256": hashlib.sha256(frames_bytes).hexdigest(),
        })
        self.assertEqual(frames_bytes, b"".join(frames))
        self.assertEqual(results_bytes.decode("utf-8"), expected_csv(rows))
        loaded, _ = load(self.calibrations, run_dir / "results.csv", self.policy)
        self.assertEqual(len(loaded), 3)
        self.assertFalse(self.staging().exists())

    def test_deterministic_summary_across_runs(self):
        rows = rows_with_batch()
        summaries = {}
        frames = {}
        for name in ["out-a", "out-b"]:
            self.open_pty()
            self.start_cli(output=self.root / name)
            frames[name] = self.instrument.run_session("run-1", BATCH, rows, self.cal_sha)
            rc, _, stderr = self.finish()
            self.assertEqual(rc, 0, stderr)
            run_dir = self.root / name / "run-1"
            summaries[name] = json.loads((run_dir / "summary.json").read_text())
            frames[name] = (run_dir / "frames.log").read_bytes()
        self.assertEqual(summaries["out-a"], summaries["out-b"])
        self.assertEqual(frames["out-a"], frames["out-b"])

    def test_stop_and_wait_split_and_sticky_frames(self):
        self.start_cli()
        rows = rows_with_batch()
        begin = protocol.begin_payload("run-1", BATCH, 3, self.cal_sha)
        # split: dribble the begin frame byte by byte
        raw_begin = protocol.encode_frame(begin)
        self.instrument.write_raw(raw_begin, chunks=[1] * len(raw_begin))
        self.instrument.expect_ack(0)
        # sticky: two pipelined frames in a single write
        first = protocol.measurement_payload(1, rows[0])
        second = protocol.measurement_payload(2, rows[1])
        self.instrument.write_raw(protocol.encode_frame(first) + protocol.encode_frame(second))
        self.instrument.expect_ack(1)
        self.instrument.expect_ack(2)
        third = protocol.measurement_payload(3, rows[2])
        self.instrument.send_payload(third, chunks=[5, 1000])
        self.instrument.expect_ack(3)
        digest = protocol.measurements_digest([first, second, third])
        self.instrument.send_payload(protocol.end_payload(4, 3, digest))
        self.instrument.expect_ack(4)
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(len(load(self.calibrations, self.run_dir() / "results.csv", self.policy)[0]), 3)

    def test_max_payload_boundary_accepted(self):
        self.start_cli()
        row = rows_with_batch()[0]
        base = protocol.measurement_payload(1, {**row, "sample": ""})
        row["sample"] = "S" * (protocol.MAX_PAYLOAD - len(base))
        payload = protocol.measurement_payload(1, row)
        self.assertEqual(len(payload), protocol.MAX_PAYLOAD)
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 1, self.cal_sha))
        self.instrument.expect_ack(0)
        self.instrument.send_payload(payload)
        self.instrument.expect_ack(1)
        self.instrument.send_payload(protocol.end_payload(2, 1, protocol.measurements_digest([payload])))
        self.instrument.expect_ack(2)
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)


class RecoveryTests(AcquireFixture):
    def begin_and_first(self, run_id="run-1"):
        rows = rows_with_batch()
        self.instrument.send_payload(protocol.begin_payload(run_id, BATCH, 3, self.cal_sha))
        self.instrument.expect_ack(0)
        raw_first = self.instrument.send_payload(protocol.measurement_payload(1, rows[0]))
        self.instrument.expect_ack(1)
        return rows, raw_first

    def finish_run(self, rows, run_id="run-1"):
        payloads = [protocol.measurement_payload(seq, row) for seq, row in enumerate(rows, start=1)]
        for seq in (2, 3):
            self.instrument.send_payload(payloads[seq - 1])
            self.instrument.expect_ack(seq)
        self.instrument.send_payload(protocol.end_payload(4, 3, protocol.measurements_digest(payloads)))
        self.instrument.expect_ack(4)

    def test_byte_exact_retransmit_is_only_reacked(self):
        self.start_cli()
        rows, raw_first = self.begin_and_first()
        self.instrument.write_raw(raw_first)  # byte-exact retransmission
        self.instrument.expect_ack(1)
        self.finish_run(rows)
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)
        frames = (self.run_dir() / "frames.log").read_bytes()
        self.assertEqual(frames.count(raw_first), 1)  # recorded once
        self.assertEqual((self.run_dir() / "results.csv").read_text().count("\n"), 4)

    def test_byte_exact_retransmit_of_begin_is_reacked(self):
        self.start_cli()
        rows = rows_with_batch()
        raw_begin = self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, self.cal_sha))
        self.instrument.expect_ack(0)
        self.instrument.write_raw(raw_begin)  # lost-ACK retransmission of begin
        self.instrument.expect_ack(0)
        for seq, row in enumerate(rows, start=1):
            self.instrument.send_payload(protocol.measurement_payload(seq, row))
            self.instrument.expect_ack(seq)
        payloads = [protocol.measurement_payload(seq, row) for seq, row in enumerate(rows, start=1)]
        self.instrument.send_payload(protocol.end_payload(4, 3, protocol.measurements_digest(payloads)))
        self.instrument.expect_ack(4)
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)
        frames = (self.run_dir() / "frames.log").read_bytes()
        self.assertEqual(frames.count(raw_begin), 1)

    def test_retransmit_with_different_content_rejected(self):
        self.start_cli()
        rows, _ = self.begin_and_first()
        changed = protocol.measurement_payload(1, {**rows[0], "signal": "99"})
        self.instrument.send_payload(changed)
        self.instrument.expect_rej(2, "conflict")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertFalse(self.run_dir().exists())
        error = json.loads((self.staging() / "error.json").read_text())
        self.assertEqual(error["code"], "conflict")
        self.assertIn("differs", error["error"])

    def test_sequence_gap_rejected(self):
        self.start_cli()
        rows, _ = self.begin_and_first()
        self.instrument.send_payload(protocol.measurement_payload(3, rows[2]))
        self.instrument.expect_rej(2, "sequence")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertFalse(self.run_dir().exists())
        self.assertTrue((self.staging() / "frames.log").exists())

    def test_crc_failure_retried_exactly_once(self):
        self.start_cli()
        rows, _ = self.begin_and_first()
        second = protocol.measurement_payload(2, rows[1])
        self.instrument.send_bad_crc(second)
        self.instrument.expect_rej(2, "crc")
        self.instrument.send_payload(second)  # the single retry succeeds
        self.instrument.expect_ack(2)
        self.instrument.send_payload(protocol.measurement_payload(3, rows[2]))
        self.instrument.expect_ack(3)
        payloads = [protocol.measurement_payload(seq, row) for seq, row in enumerate(rows, start=1)]
        self.instrument.send_payload(protocol.end_payload(4, 3, protocol.measurements_digest(payloads)))
        self.instrument.expect_ack(4)
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)

    def test_crc_retry_budget_is_per_sequence(self):
        self.start_cli()
        rows, _ = self.begin_and_first()
        payloads = [protocol.measurement_payload(seq, row) for seq, row in enumerate(rows, start=1)]
        for seq in (2, 3):
            self.instrument.send_bad_crc(payloads[seq - 1])
            self.instrument.expect_rej(seq, "crc")
            self.instrument.send_payload(payloads[seq - 1])
            self.instrument.expect_ack(seq)
        self.instrument.send_payload(protocol.end_payload(4, 3, protocol.measurements_digest(payloads)))
        self.instrument.expect_ack(4)
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)

    def test_crc_failure_twice_aborts(self):
        self.start_cli()
        rows, _ = self.begin_and_first()
        second = protocol.measurement_payload(2, rows[1])
        self.instrument.send_bad_crc(second)
        self.instrument.expect_rej(2, "crc")
        self.instrument.send_bad_crc(second)
        self.instrument.expect_rej(2, "crc")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertFalse(self.run_dir().exists())
        self.assertTrue((self.staging() / "error.json").exists())

    def test_disconnect_then_restart_replays_from_begin(self):
        self.start_cli()
        self.begin_and_first()
        os.close(self.master)  # instrument disappears mid-run
        self.master = None
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertIn("EOF", stderr)
        staged_frames = (self.staging() / "frames.log").read_bytes()
        self.assertEqual(staged_frames.count(b'"type":"begin"'), 1)
        self.assertFalse(self.run_dir().exists())
        # restart: the instrument replays the whole run from begin
        self.open_pty()
        self.start_cli()
        self.instrument.run_session("run-1", BATCH, rows_with_batch(), self.cal_sha)
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)
        self.assertTrue((self.run_dir() / "summary.json").exists())
        retired = self.output / ".staging" / "run-1.retired-1"
        self.assertEqual((retired / "frames.log").read_bytes(), staged_frames)
        self.assertFalse(self.staging().exists())


class ReplayConflictTests(AcquireFixture):
    def complete_run(self, run_id="run-1", rows=None, calibrations=None, policy=None):
        rows = rows if rows is not None else rows_with_batch()
        cal_sha = hashlib.sha256(Path(calibrations or self.calibrations).read_bytes()).hexdigest()
        self.start_cli(calibrations=calibrations, policy=policy)
        self.instrument.run_session(run_id, BATCH, rows, cal_sha)
        rc, stdout, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)
        return json.loads(stdout)

    def snapshot(self, run_id="run-1"):
        return {path.name: path.read_bytes() for path in self.run_dir(run_id).iterdir()}

    def test_successful_replay_is_idempotent_and_acks_every_frame(self):
        summary = self.complete_run()
        before = self.snapshot()
        self.open_pty()
        self.start_cli()
        # run_session asserts one ACK per frame (begin, 3 measurements, end)
        self.instrument.run_session("run-1", BATCH, rows_with_batch(), self.cal_sha)
        rc, stdout, stderr = self.finish()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(json.loads(stdout), summary)
        self.assertEqual(self.snapshot(), before)  # old directory untouched
        self.assertFalse(self.staging().exists())

    def test_replay_with_different_data_keeps_old_and_rejects(self):
        self.complete_run()
        before = self.snapshot()
        changed = rows_with_batch()
        changed[1]["signal"] = "10.5"
        self.open_pty()
        self.start_cli()
        rows = changed
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, self.cal_sha))
        self.instrument.expect_ack(0)
        payloads = []
        for seq, row in enumerate(rows, start=1):
            payload = protocol.measurement_payload(seq, row)
            payloads.append(payload)
            self.instrument.send_payload(payload)
            self.instrument.expect_ack(seq)
        self.instrument.send_payload(protocol.end_payload(4, 3, protocol.measurements_digest(payloads)))
        self.instrument.expect_rej(4, "conflict")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertEqual(self.snapshot(), before)  # old directory kept
        error = json.loads((self.staging() / "error.json").read_text())
        self.assertEqual(error["code"], "conflict")
        self.assertIn("10.5", (self.staging() / "results.csv").read_text())

    def test_replay_with_different_policy_rejected_at_begin(self):
        self.complete_run()
        self.policy.write_text(self.policy.read_text().replace('"10"', '"11"'))
        self.open_pty()
        self.start_cli()
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, self.cal_sha))
        self.instrument.expect_rej(0, "conflict")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertTrue((self.run_dir() / "summary.json").exists())  # old directory kept

    def test_replay_with_different_calibration_rejected_at_begin(self):
        self.complete_run()
        before = self.snapshot()
        other = self.root / "other_calibrations.csv"
        other.write_text(self.calibrations.read_text().replace("nitrate,N-OLD", "nitrate,N-ALT"))
        other_sha = hashlib.sha256(other.read_bytes()).hexdigest()
        self.open_pty()
        self.start_cli(calibrations=other)
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, other_sha))
        self.instrument.expect_rej(0, "conflict")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertEqual(self.snapshot(), before)  # old directory kept

    def test_new_run_id_coexists_with_published(self):
        self.complete_run("run-1")
        self.open_pty()
        self.complete_run("run-2")
        self.assertTrue((self.run_dir("run-1") / "summary.json").exists())
        self.assertTrue((self.run_dir("run-2") / "summary.json").exists())


class ValidationFailureTests(AcquireFixture):
    def play_until_end_rejected(self, rows, run_id="run-1", count=3):
        self.start_cli()
        self.instrument.send_payload(protocol.begin_payload(run_id, BATCH, count, self.cal_sha))
        self.instrument.expect_ack(0)
        payloads = []
        for seq, row in enumerate(rows, start=1):
            payload = protocol.measurement_payload(seq, row)
            payloads.append(payload)
            self.instrument.send_payload(payload)
            self.instrument.expect_ack(seq)
        return payloads

    def test_load_validation_failure_keeps_staging(self):
        rows = rows_with_batch()
        rows[0]["analyte"] = "unobtainium"
        payloads = self.play_until_end_rejected(rows)
        self.instrument.send_payload(protocol.end_payload(4, 3, protocol.measurements_digest(payloads)))
        self.instrument.expect_rej(4, "validation")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertFalse(self.run_dir().exists())
        error = json.loads((self.staging() / "error.json").read_text())
        self.assertIn("unknown analyte", error["error"])
        self.assertTrue((self.staging() / "results.csv").exists())
        self.assertTrue((self.staging() / "frames.log").exists())

    def test_end_count_mismatch_rejected(self):
        payloads = self.play_until_end_rejected(rows_with_batch())
        self.instrument.send_payload(protocol.end_payload(4, 2, protocol.measurements_digest(payloads)))
        self.instrument.expect_rej(4, "validation")
        rc, _, _ = self.finish()
        self.assertEqual(rc, 2)

    def test_end_hash_mismatch_rejected(self):
        self.play_until_end_rejected(rows_with_batch())
        self.instrument.send_payload(protocol.end_payload(4, 3, "0" * 64))
        self.instrument.expect_rej(4, "validation")
        rc, _, _ = self.finish()
        self.assertEqual(rc, 2)

    def test_measurement_batch_mismatch_rejected(self):
        self.start_cli()
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, self.cal_sha))
        self.instrument.expect_ack(0)
        row = rows_with_batch(batch="B2")[0]
        self.instrument.send_payload(protocol.measurement_payload(1, row))
        self.instrument.expect_rej(1, "validation")
        rc, _, _ = self.finish()
        self.assertEqual(rc, 2)

    def test_begin_calibration_hash_mismatch_rejected(self):
        self.start_cli()
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, "0" * 64))
        self.instrument.expect_rej(0, "validation")
        rc, _, _ = self.finish()
        self.assertEqual(rc, 2)
        self.assertTrue((self.staging() / "error.json").exists())

    def test_first_frame_must_be_begin(self):
        self.start_cli()
        self.instrument.send_payload(protocol.end_payload(0, 3, "0" * 64))
        self.instrument.expect_rej(0, "state")
        rc, _, _ = self.finish()
        self.assertEqual(rc, 2)

    def test_premature_end_rejected(self):
        self.start_cli()
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, self.cal_sha))
        self.instrument.expect_ack(0)
        self.instrument.send_payload(protocol.end_payload(1, 3, "0" * 64))
        self.instrument.expect_rej(1, "state")
        rc, _, _ = self.finish()
        self.assertEqual(rc, 2)


class FailureInjectionTests(AcquireFixture):
    def test_staging_path_blocked_is_a_write_failure(self):
        (self.output / ".staging").mkdir(parents=True)
        obstacle = self.output / ".staging" / "run-1"
        obstacle.write_text("occupied")
        self.start_cli()
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, self.cal_sha))
        self.instrument.expect_rej(0, "internal")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertIn("staging", stderr)
        self.assertEqual(obstacle.read_text(), "occupied")

    def test_publish_target_blocked_keeps_staging(self):
        self.output.mkdir(parents=True)
        obstacle = self.output / "run-1"
        obstacle.write_text("occupied")
        self.start_cli()
        rows = rows_with_batch()
        self.instrument.send_payload(protocol.begin_payload("run-1", BATCH, 3, self.cal_sha))
        self.instrument.expect_ack(0)
        payloads = []
        for seq, row in enumerate(rows, start=1):
            payload = protocol.measurement_payload(seq, row)
            payloads.append(payload)
            self.instrument.send_payload(payload)
            self.instrument.expect_ack(seq)
        self.instrument.send_payload(protocol.end_payload(4, 3, protocol.measurements_digest(payloads)))
        self.instrument.expect_rej(4, "conflict")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertEqual(obstacle.read_text(), "occupied")
        self.assertTrue((self.staging() / "error.json").exists())
        self.assertTrue((self.staging() / "results.csv").exists())

    def test_output_lock_is_exclusive(self):
        self.output.mkdir(parents=True)
        lock_fd = os.open(self.output / ".acquire.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.start_cli(wait_ready=False)
            rc, _, stderr = self.finish(timeout=15.0)
            self.assertEqual(rc, 2)
            self.assertIn("lock", stderr)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def test_device_lock_is_exclusive(self):
        fcntl.flock(self.slave, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.start_cli(wait_ready=False)
            rc, _, stderr = self.finish(timeout=15.0)
            self.assertEqual(rc, 2)
            self.assertIn("busy", stderr)
        finally:
            fcntl.flock(self.slave, fcntl.LOCK_UN)

    def test_idle_timeout_is_finite(self):
        self.start_cli(timeout=1.0)
        rc, _, stderr = self.finish(timeout=20.0)
        self.assertEqual(rc, 2)
        self.assertIn("no data", stderr)

    def test_eof_before_begin(self):
        self.start_cli()
        os.close(self.master)
        self.master = None
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertIn("EOF", stderr)

    def test_oversize_declared_length_rejected(self):
        self.start_cli()
        self.instrument.write_raw(b"@" + b"00010001" + b":")
        self.instrument.expect_rej(0, "frame")
        rc, _, _ = self.finish()
        self.assertEqual(rc, 2)

    def test_garbage_rejected(self):
        self.start_cli()
        self.instrument.write_raw(b"this is not a frame at all")
        self.instrument.expect_rej(0, "frame")
        rc, _, _ = self.finish()
        self.assertEqual(rc, 2)


class CliContractTests(AcquireFixture):
    def cli_args(self, *extra, output=None, calibrations=None):
        args = [sys.executable, "-m", "assay_insights.acquire",
                "--device", self.slave_path,
                "--calibrations", str(calibrations or self.calibrations),
                "--policy", str(self.policy),
                "--output", str(output or self.output), *extra]
        env = dict(os.environ, PYTHONPATH=str(PROJECT))
        return subprocess.run(args, cwd=PROJECT, env=env, capture_output=True, text=True, timeout=30)

    def test_invalid_timeout_and_baud(self):
        for value in ["nan", "inf", "-1", "0"]:
            result = self.cli_args("--timeout", value)
            self.assertEqual(result.returncode, 2, value)
            self.assertIn("timeout", result.stderr)
        result = self.cli_args("--baud", "12345")
        self.assertEqual(result.returncode, 2)
        self.assertIn("baud", result.stderr)

    def test_legacy_calibrations_rejected(self):
        result = self.cli_args(calibrations=DATA / "calibrations.csv")
        self.assertEqual(result.returncode, 2)
        self.assertIn("timed", result.stderr)

    def test_output_must_not_be_an_input_file(self):
        result = self.cli_args(output=self.calibrations)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a directory", result.stderr)

    def test_run_id_must_not_shadow_inputs(self):
        output = self.root / "shared"
        output.mkdir()
        calibrations = output / "calibrations.csv"
        calibrations.write_bytes(self.calibrations.read_bytes())
        before = calibrations.read_bytes()
        self.start_cli(output=output, calibrations=calibrations)
        self.instrument.send_payload(protocol.begin_payload(
            "calibrations.csv", BATCH, 3, hashlib.sha256(before).hexdigest()))
        self.instrument.expect_rej(0, "validation")
        rc, _, stderr = self.finish()
        self.assertEqual(rc, 2)
        self.assertEqual(calibrations.read_bytes(), before)


class TesterDemoTests(unittest.TestCase):
    def test_tester_module_runs_a_full_demo_run(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, PYTHONPATH=str(PROJECT))
            result = subprocess.run(
                [sys.executable, "-m", "assay_insights.acquire.tester", "--output", temp],
                cwd=PROJECT, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["returncode"], 0)
            summary = json.loads((Path(temp) / "demo-run" / "summary.json").read_text())
            self.assertEqual(report["summary"], summary)
            self.assertEqual(summary["count"], 3)


if __name__ == "__main__":
    unittest.main()
