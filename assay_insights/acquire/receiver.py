"""Stop-and-wait acquisition session: persist, fsync, ACK; validate; publish.

One ``serve`` call handles exactly one run (one ``begin`` .. ``end`` cycle):

* every accepted frame is appended to ``frames.log`` (and its CSV row to
  ``results.csv``) and fsynced before its ACK is sent;
* a byte-exact retransmission of the immediately previous frame is only
  re-ACKed; different bytes or a missing sequence are rejected;
* a CRC failure on the expected sequence is retried exactly once;
* the ``end`` frame is ACKed only after the staged run passes the existing
  ``load`` validation and is atomically published (rename within the output
  filesystem) — or, for an already published run, confirmed idempotent.

Failures keep the unpublished staging directory plus an ``error.json``
diagnostic. A later retry of the same run_id archives the old staging to
``.staging/<run_id>.retired-<n>`` and replays from ``begin``.
"""
import csv
import errno
import hashlib
import json
import os
import select
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..input import TIMED_RESULT_HEADERS, load
from .errors import AcquireError, RejectError
from . import protocol


@dataclass
class Config:
    output: Path
    calibrations: Path
    policy: Path
    calibration_sha256: str
    policy_sha256: str
    timeout: float


def _sha256_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_text_fsync(path, text):
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


class Run:
    """Filesystem state of one run: staging dir, open files, replay target."""

    def __init__(self, run_id, batch, count, staging, final):
        self.run_id = run_id
        self.batch = batch
        self.count = count
        self.staging = staging
        self.final = final
        self.published = None  # summary dict of the published run, if replaying
        self.staging_created = False
        self.frames_file = None
        self.csv_file = None
        self.csv_writer = None
        self.hasher = hashlib.sha256()

    def close(self):
        for stream in (self.frames_file, self.csv_file):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class Session:
    def __init__(self, fd, config):
        self.fd = fd
        self.config = config
        self.expected = 0
        self.last_raw = None
        self.crc_retried = False
        self.current = None

    # ---- wire I/O -----------------------------------------------------

    def _send(self, line):
        view = memoryview(line)
        try:
            while view:
                view = view[os.write(self.fd, view):]
        except OSError as exc:
            raise AcquireError(f"device write failed: {exc}") from exc

    def send_ack(self, seq):
        self._send(protocol.ack_line(seq))

    def send_rej(self, code):
        self._send(protocol.rej_line(self.expected, code))

    def send_rej_quiet(self, code):
        try:
            self.send_rej(code)
        except AcquireError:
            pass

    def read_frame(self, buffer):
        while True:
            frame = protocol.try_parse_frame(buffer)
            if frame is not None:
                return frame
            if len(buffer) > protocol.MAX_FRAME:
                raise RejectError("frame", f"incomplete frame exceeds the {protocol.MAX_FRAME}-byte bound")
            ready = select.select([self.fd], [], [], self.config.timeout)[0]
            if not ready:
                raise AcquireError(f"no data from device within {self.config.timeout} s")
            try:
                chunk = os.read(self.fd, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    chunk = b""
                else:
                    raise AcquireError(f"device read failed: {exc}") from exc
            if not chunk:
                raise AcquireError("device reached EOF before the run completed")
            buffer.extend(chunk)

    # ---- session state machine ----------------------------------------

    def run(self):
        buffer = bytearray()
        while True:
            frame = self.read_frame(buffer)
            if not frame.crc_ok:
                if self.crc_retried:
                    raise RejectError("crc", f"repeated CRC failure at expected seq {self.expected}")
                self.crc_retried = True
                self.send_rej("crc")
                continue
            self.crc_retried = False
            message = protocol.parse_message(frame.payload)
            seq = message["seq"]
            if seq == self.expected:
                pass
            elif self.expected >= 1 and seq == self.expected - 1:
                if frame.raw == self.last_raw:
                    self.send_ack(seq)  # byte-exact retransmission: re-ACK only
                    continue
                raise RejectError("conflict", f"retransmitted seq {seq} differs from the recorded frame")
            else:
                raise RejectError("sequence", f"expected seq {self.expected}, received {seq}")
            if self.expected == 0:
                if message["type"] != protocol.BEGIN:
                    raise RejectError("state", "first frame must be begin")
                self.begin_run(message)
                self.persist(frame, None)
            elif self.expected <= self.current.count:
                if message["type"] != protocol.MEASUREMENT:
                    raise RejectError("state", f"expected measurement at seq {self.expected}")
                if message["batch"] != self.current.batch:
                    raise RejectError("validation", "measurement batch differs from begin batch")
                self.current.hasher.update(frame.payload)
                self.current.hasher.update(b"\n")
                self.persist(frame, [message[header] for header in TIMED_RESULT_HEADERS])
            else:
                if message["type"] != protocol.END:
                    raise RejectError("state", f"expected end at seq {self.expected}")
                if message["count"] != self.current.count:
                    raise RejectError("validation", "end count differs from begin count")
                digest = self.current.hasher.hexdigest()
                if message["measurements_sha256"] != digest:
                    raise RejectError("validation", "measurements hash mismatch")
                self.persist(frame, None)
                summary = self.finalize(digest)
                self.send_ack(seq)
                return summary
            self.last_raw = frame.raw
            self.send_ack(self.expected)
            self.expected += 1

    # ---- persistence ----------------------------------------------------

    def persist(self, frame, row):
        run = self.current
        run.frames_file.write(frame.raw)
        run.frames_file.flush()
        os.fsync(run.frames_file.fileno())
        if row is not None:
            run.csv_writer.writerow(row)
        run.csv_file.flush()
        os.fsync(run.csv_file.fileno())

    def begin_run(self, message):
        run_id = message["run_id"]
        batch = message["batch"]
        count = message["count"]
        output = self.config.output
        run = Run(run_id, batch, count, output / ".staging" / run_id, output / run_id)
        self.current = run
        self._check_run_paths(run)
        if message["calibration_sha256"] != self.config.calibration_sha256:
            self._ensure_staging(run)
            raise RejectError("validation", "begin calibration_sha256 does not match the --calibrations file")
        published = self._read_published(run.final)
        if published is not None:
            same = (
                published.get("run_id") == run_id
                and published.get("batch") == batch
                and published.get("count") == count
                and published.get("calibration_sha256") == self.config.calibration_sha256
                and published.get("policy_sha256") == self.config.policy_sha256
            )
            if not same:
                self._ensure_staging(run)
                raise RejectError("conflict", "begin conflicts with the published run; keeping the old directory")
            run.published = published
        self._ensure_staging(run)

    def _check_run_paths(self, run):
        inputs = [self.config.calibrations.resolve(), self.config.policy.resolve()]
        for path in (run.final, run.staging):
            resolved = path.resolve()
            for source in inputs:
                if resolved == source or resolved in source.parents:
                    raise RejectError(
                        "validation",
                        f"run_id {run.run_id!r} would let output paths overlap input {source}",
                    )

    def _read_published(self, final):
        summary_path = final / "summary.json"
        if not final.is_dir() or not summary_path.is_file():
            return None
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # unreadable summary: conflicts with everything, never overwritten
        return data if isinstance(data, dict) else {}

    def _ensure_staging(self, run):
        if run.staging_created:
            return
        staging = run.staging
        if staging.is_dir():
            for attempt in range(1, 1000):
                retired = staging.with_name(f"{staging.name}.retired-{attempt}")
                if not retired.exists():
                    os.rename(staging, retired)
                    _fsync_dir(staging.parent)
                    break
            else:
                raise AcquireError(f"too many retired staging directories for run {run.run_id!r}")
        elif staging.exists() or staging.is_symlink():
            raise AcquireError(f"staging path {staging} exists and is not a directory")
        try:
            staging.mkdir()
        except OSError as exc:
            raise AcquireError(f"cannot create staging directory {staging}: {exc}") from exc
        try:
            run.frames_file = open(staging / "frames.log", "wb")
            run.csv_file = open(staging / "results.csv", "w", encoding="utf-8", newline="")
        except OSError as exc:
            run.close()
            raise AcquireError(f"cannot open staging files in {staging}: {exc}") from exc
        run.csv_writer = csv.writer(run.csv_file, lineterminator="\n")
        run.csv_writer.writerow(TIMED_RESULT_HEADERS)
        run.csv_file.flush()
        os.fsync(run.csv_file.fileno())
        run.staging_created = True

    def finalize(self, digest):
        run = self.current
        run.close()
        summary = {
            "format": protocol.SUMMARY_FORMAT,
            "run_id": run.run_id,
            "batch": run.batch,
            "count": run.count,
            "calibration_sha256": self.config.calibration_sha256,
            "policy_sha256": self.config.policy_sha256,
            "measurements_sha256": digest,
            "results_sha256": _sha256_file(run.staging / "results.csv"),
            "frames_sha256": _sha256_file(run.staging / "frames.log"),
        }
        _write_text_fsync(run.staging / "summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
        _fsync_dir(run.staging)
        try:
            load(self.config.calibrations, run.staging / "results.csv", self.config.policy)
        except (OSError, ValueError) as exc:
            raise RejectError("validation", f"results failed input validation: {exc}") from exc
        if run.published is None:
            if run.final.exists() or run.final.is_symlink():
                raise RejectError("conflict", f"publish target {run.final} already exists; keeping the old directory")
            try:
                os.rename(run.staging, run.final)  # atomic, same filesystem
            except OSError as exc:
                raise AcquireError(f"atomic publish of {run.final} failed: {exc}") from exc
            _fsync_dir(self.config.output)
            return summary
        if run.published.get("measurements_sha256") == digest:
            shutil.rmtree(run.staging)  # identical replay: keep the old directory
            _fsync_dir(run.staging.parent)
            return run.published
        raise RejectError("conflict", "replayed data conflicts with the published run; keeping the old directory")

    # ---- failure diagnostics --------------------------------------------

    def note_failure(self, exc):
        run = self.current
        if run is None or not run.staging_created:
            return
        info = {"error": str(exc), "run_id": run.run_id}
        if isinstance(exc, RejectError):
            info["code"] = exc.code
        try:
            _write_text_fsync(run.staging / "error.json", json.dumps(info, indent=2, sort_keys=True) + "\n")
        except OSError:
            pass

    def close(self):
        if self.current is not None:
            self.current.close()


def serve(fd, config):
    """Run one acquisition session on an already-configured device fd."""
    session = Session(fd, config)
    try:
        return session.run()
    except RejectError as exc:
        session.note_failure(exc)
        session.send_rej_quiet(exc.code)
        raise AcquireError(str(exc)) from exc
    except AcquireError as exc:
        session.note_failure(exc)
        session.send_rej_quiet("internal")
        raise
    except OSError as exc:
        session.note_failure(exc)
        session.send_rej_quiet("internal")
        raise AcquireError(str(exc)) from exc
    finally:
        session.close()
