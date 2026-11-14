"""Play one pty run against the real acquisition CLI, with an optional context.

This is a standalone demonstration helper, not the diagnostic orchestrator. It
exists so the acquisition finding can be reproduced without
``diagnostics/orchestrator.py``::

    # control: the run is rejected before publication
    python3 diagnostics/acquire_replay.py \
        --results /tmp/assay-fixtures/range-edge-timed/results.csv \
        --calibrations /tmp/assay-fixtures/range-edge-timed/calibrations.csv \
        --policy /tmp/assay-fixtures/range-edge-timed/policy.json \
        --output /tmp/acquire-control

    # prec 2: the same run is published
    python3 diagnostics/acquire_replay.py ... --context '{"prec": 2}' \
        --output /tmp/acquire-prec2

It prints one JSON object: the CLI exit code, whether the run was published,
the staging error (if any), and the result of validating the published CSV
under the *default* context in a clean subprocess.

Every descriptor is closed and the child process group is reaped in `finally`.
"""
import argparse
import csv
import hashlib
import json
import os
import pty
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from assay_insights.acquire import protocol, tester  # noqa: E402
from assay_insights.acquire.serialport import configure  # noqa: E402

SHIM = os.path.join(PROJECT, "diagnostics", "shim")


def _wait_for_marker(process, marker, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if line == "":
            if process.poll() is not None:
                raise RuntimeError("acquire CLI exited before readiness")
            time.sleep(0.01)
            continue
        if marker in line:
            return
    raise TimeoutError(f"no {marker!r} within {timeout}s")


def _reap(process, timeout):
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", required=True, type=Path,
                        help="timed results CSV whose rows are played as frames")
    parser.add_argument("--calibrations", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--context", default=None,
                        help='JSON decimal context, e.g. \'{"prec": 2}\'')
    parser.add_argument("--run-id", default="run-1")
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="acquirer idle-read timeout in seconds")
    parser.add_argument("--reply-timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(open(args.results, encoding="utf-8", newline="")))
    calibration_sha = hashlib.sha256(args.calibrations.read_bytes()).hexdigest()
    env = dict(os.environ)
    env["PYTHONPATH"] = SHIM + os.pathsep + PROJECT
    if args.context:
        env["ASSAY_DIAG_CONTEXT"] = args.context
    args.output.mkdir(parents=True, exist_ok=True)

    master = slave = None
    process = None
    instrument = None
    try:
        master, slave = pty.openpty()
        configure(slave, 115200)
        command = [sys.executable, "-m", "assay_insights.acquire",
                   "--device", os.ttyname(slave), "--baud", "115200",
                   "--calibrations", str(args.calibrations),
                   "--policy", str(args.policy),
                   "--output", str(args.output),
                   "--timeout", str(args.timeout)]
        process = subprocess.Popen(command, cwd=PROJECT, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        _wait_for_marker(process, "listening", 20)
        instrument = tester.Instrument(master, reply_timeout=args.reply_timeout)
        payloads = []
        batch = rows[0]["batch"]
        instrument.send_payload(protocol.begin_payload(args.run_id, batch,
                                                       len(rows), calibration_sha))
        instrument.expect_ack(0)
        for seq, row in enumerate(rows, start=1):
            payload = protocol.measurement_payload(seq, row)
            payloads.append(payload)
            instrument.send_payload(payload)
            instrument.expect_ack(seq)
        instrument.send_payload(protocol.end_payload(
            len(rows) + 1, len(rows), protocol.measurements_digest(payloads)))
        instrument.expect_ack(len(rows) + 1)
    except Exception as exc:  # noqa: BLE001
        instrument = instrument or None
        instrument_error = f"{type(exc).__name__}: {exc}"
    else:
        instrument_error = None
    finally:
        for descriptor in (master, slave):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process is not None:
            _reap(process, 20)

    stdout, stderr = process.communicate()
    run_dir = args.output / args.run_id
    staging_error = args.output / ".staging" / args.run_id / "error.json"
    published_csv = run_dir / "results.csv"
    validation = {"status": "not-published"}
    if published_csv.is_file():
        clean_env = {key: value for key, value in os.environ.items()
                     if key not in ("PYTHONPATH", "ASSAY_DIAG_CONTEXT")}
        check = subprocess.run(
            [sys.executable, "-c",
             "from assay_insights.input import load; "
             "load(%r, %r, %r); print('accepted')" % (
                 str(args.calibrations), str(published_csv), str(args.policy))],
            cwd=PROJECT, env=dict(clean_env, PYTHONPATH=PROJECT),
            capture_output=True, text=True, timeout=30)
        validation = {
            "status": "accepted" if check.returncode == 0 else "rejected",
            "exit_code": check.returncode,
            "stderr_last": (check.stderr.strip().splitlines() or [None])[-1],
        }

    print(json.dumps({
        "exit_code": process.returncode,
        "instrument": instrument_error or "acked",
        "published": published_csv.is_file(),
        "staging_error": (json.loads(staging_error.read_text(encoding="utf-8"))
                          if staging_error.is_file() else None),
        "stdout_summary": stdout.strip(),
        "stderr_last": (stderr.strip().splitlines() or [None])[-1],
        "default_context_validation": validation,
    }, indent=2, sort_keys=True))
    return 0 if instrument_error is None else 2


if __name__ == "__main__":
    sys.exit(main())
