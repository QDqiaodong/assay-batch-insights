"""Pseudo-terminal synthetic instrument used to exercise the acquisition CLI.

The tester is both a library (used by the test-suite) and a runnable
end-to-end demo::

    python -m assay_insights.acquire.tester --output run/acquire-demo

The demo allocates a pty pair, spawns ``python -m assay_insights.acquire`` on
the slave side, plays one full stop-and-wait run as the instrument on the
master side, and prints a JSON result.
"""
import argparse
import hashlib
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import protocol
from .serialport import configure

PROJECT = Path(__file__).resolve().parents[2]

DEMO_BATCH = "DEMO"
DEMO_ROWS = [
    {"batch": DEMO_BATCH, "sample": "S1", "analyte": "nitrate", "replicate": "1", "kind": "sample",
     "signal": "10", "dilution": "1", "target_mg_l": "", "measured_at": "2026-03-01T08:00:00Z"},
    {"batch": DEMO_BATCH, "sample": "S1", "analyte": "nitrate", "replicate": "2", "kind": "sample",
     "signal": "10.4", "dilution": "1", "target_mg_l": "", "measured_at": "2026-03-01T08:05:00Z"},
    {"batch": DEMO_BATCH, "sample": "Q1", "analyte": "nitrate", "replicate": "1", "kind": "QC",
     "signal": "21", "dilution": "1", "target_mg_l": "10", "measured_at": "2026-03-01T09:00:00Z"},
]


class InstrumentError(Exception):
    """The acquisition side misbehaved or went silent."""


def open_pty(baud=115200):
    """Return (master_fd, slave_fd, slave_path) with the slave in raw 8N1."""
    master, slave = pty.openpty()
    configure(slave, baud)
    return master, slave, os.ttyname(slave)


def wait_for_marker(stream, marker, timeout):
    """Read lines from ``stream`` until one contains ``marker``."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InstrumentError(f"timed out waiting for {marker!r}")
        if not select.select([stream], [], [], remaining)[0]:
            continue
        line = stream.readline()
        if not line:
            raise InstrumentError("process exited before signaling readiness")
        if marker in line:
            return line


class Instrument:
    """Scriptable synthetic instrument on the master side of a pty."""

    def __init__(self, master_fd, reply_timeout=10.0):
        self.master = master_fd
        self.reply_timeout = reply_timeout
        self.buffer = bytearray()

    def _write_all(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.master, view):]

    def write_raw(self, data, chunks=None, pause=0.0):
        """Write bytes, optionally split into ``chunks`` sizes (split frames)."""
        if chunks is None:
            self._write_all(data)
            return
        offset = 0
        for size in chunks:
            self._write_all(data[offset:offset + size])
            offset += size
            if pause:
                time.sleep(pause)
        if offset < len(data):
            self._write_all(data[offset:])

    def send_payload(self, payload, chunks=None, pause=0.0):
        raw = protocol.encode_frame(payload)
        self.write_raw(raw, chunks, pause)
        return raw

    def send_bad_crc(self, payload):
        """A structurally valid frame whose CRC does not match the payload."""
        raw = protocol.encode_frame(payload)
        digit = raw[-2:-1]
        bad = raw[:-2] + (b"0" if digit != b"0" else b"1") + raw[-1:]
        self.write_raw(bad)
        return bad

    def read_reply(self, timeout=None):
        deadline = time.monotonic() + (self.reply_timeout if timeout is None else timeout)
        while True:
            index = self.buffer.find(b"\n")
            if index >= 0:
                line = bytes(self.buffer[:index + 1])
                del self.buffer[:index + 1]
                try:
                    return protocol.parse_reply(line)
                except ValueError as exc:
                    raise InstrumentError(str(exc)) from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstrumentError("timed out waiting for a reply")
            if not select.select([self.master], [], [], remaining)[0]:
                raise InstrumentError("timed out waiting for a reply")
            try:
                chunk = os.read(self.master, 4096)
            except OSError as exc:
                raise InstrumentError(f"device read failed: {exc}") from exc
            if not chunk:
                raise InstrumentError("device reached EOF waiting for a reply")
            self.buffer.extend(chunk)

    def expect_ack(self, seq):
        reply = self.read_reply()
        if reply != ("ACK", seq, None):
            raise InstrumentError(f"expected ACK {seq}, got {reply}")

    def expect_rej(self, seq, code=None):
        kind, rseq, rcode = self.read_reply()
        if kind != "REJ" or rseq != seq or (code is not None and rcode != code):
            raise InstrumentError(f"expected REJ {seq} {code}, got {(kind, rseq, rcode)}")

    def run_session(self, run_id, batch, rows, calibration_sha256, chunks=None, pause=0.0):
        """Play one full stop-and-wait run; returns the raw frames sent."""
        frames = [self.send_payload(
            protocol.begin_payload(run_id, batch, len(rows), calibration_sha256), chunks, pause)]
        self.expect_ack(0)
        payloads = []
        for seq, row in enumerate(rows, start=1):
            payload = protocol.measurement_payload(seq, row)
            payloads.append(payload)
            frames.append(self.send_payload(payload, chunks, pause))
            self.expect_ack(seq)
        frames.append(self.send_payload(protocol.end_payload(
            len(rows) + 1, len(rows), protocol.measurements_digest(payloads)), chunks, pause))
        self.expect_ack(len(rows) + 1)
        return frames


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m assay_insights.acquire.tester",
        description="Play one synthetic instrument run against the acquisition CLI over a pty.",
    )
    parser.add_argument("--calibrations", type=Path, default=PROJECT / "data/timed_calibrations.csv")
    parser.add_argument("--policy", type=Path, default=PROJECT / "data/policy.json")
    parser.add_argument("--output", type=Path, default=None,
                        help="acquisition output directory (default: a fresh temp directory)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--run-id", default="demo-run")
    args = parser.parse_args(argv)

    calibration_sha256 = hashlib.sha256(args.calibrations.read_bytes()).hexdigest()
    output = args.output or Path(tempfile.mkdtemp(prefix="acquire-demo-"))
    master, slave, slave_path = open_pty(args.baud)
    env = dict(os.environ, PYTHONPATH=str(PROJECT))
    proc = subprocess.Popen(
        [sys.executable, "-m", "assay_insights.acquire",
         "--device", slave_path, "--baud", str(args.baud),
         "--calibrations", str(args.calibrations), "--policy", str(args.policy),
         "--output", str(output), "--timeout", str(args.timeout)],
        cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_for_marker(proc.stderr, "listening", 30.0)
        instrument = Instrument(master)
        instrument.run_session(args.run_id, DEMO_BATCH, DEMO_ROWS, calibration_sha256)
    finally:
        os.close(master)
        os.close(slave)
    stdout, stderr = proc.communicate(timeout=60)
    result = {
        "returncode": proc.returncode,
        "run_id": args.run_id,
        "output": str(output),
        "summary": json.loads(stdout) if proc.returncode == 0 and stdout.strip() else None,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if proc.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
