"""Command-line entry point for the synthetic instrument serial acquisition."""
import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import sys
from pathlib import Path

from ..input import TIMED_CALIBRATION_HEADERS
from . import serialport
from .errors import AcquireError
from .receiver import Config, serve


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m assay_insights.acquire",
        description=(
            "Capture one synthetic instrument run from a POSIX serial device "
            "(8N1), validate it with the existing load contract, and publish "
            "results.csv, the raw frames and a deterministic summary. No analysis."
        ),
    )
    parser.add_argument("--device", required=True, type=Path,
                        help="serial device path, e.g. /dev/ttyUSB0 or a pty slave")
    parser.add_argument("--baud", type=int, default=115200,
                        help="baud rate (default: 115200)")
    parser.add_argument("--calibrations", type=Path, default=Path("data/timed_calibrations.csv"),
                        help="timed calibration CSV the run is checked against")
    parser.add_argument("--policy", type=Path, default=Path("data/policy.json"),
                        help="policy JSON used by load validation")
    parser.add_argument("--output", type=Path, default=Path("run/acquire"),
                        help="output directory; runs publish under <output>/<run_id>")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="finite idle-read timeout in seconds (default: 10)")
    return parser


def _read_header(path):
    with open(path, encoding="utf-8", newline="") as stream:
        return next(csv.reader(stream), None)


def _check_static_paths(output, inputs):
    reserved = [output.resolve(), (output / ".staging").resolve(), (output / ".acquire.lock").resolve()]
    for source in inputs:
        if source in reserved or reserved[1] in source.parents:
            raise AcquireError(f"output paths would overlap input {source}; refusing to run")


def acquire(args):
    timeout = args.timeout
    if not math.isfinite(timeout) or timeout <= 0:
        raise AcquireError("--timeout must be a finite, positive number of seconds")
    if args.baud not in serialport.BAUD_RATES:
        raise AcquireError(f"unsupported baud rate {args.baud}; supported: {sorted(serialport.BAUD_RATES)}")
    calibrations = args.calibrations
    policy = args.policy
    calibration_sha256 = hashlib.sha256(calibrations.read_bytes()).hexdigest()
    policy_sha256 = hashlib.sha256(policy.read_bytes()).hexdigest()
    if _read_header(calibrations) != TIMED_CALIBRATION_HEADERS:
        raise AcquireError(
            f"{calibrations}: acquisition requires a timed calibration CSV "
            f"with header {TIMED_CALIBRATION_HEADERS}"
        )
    output = args.output
    if output.exists() and not output.is_dir():
        raise AcquireError(f"{output}: output path exists and is not a directory")
    (output / ".staging").mkdir(parents=True, exist_ok=True)
    _check_static_paths(output, [calibrations.resolve(), policy.resolve()])

    lock_stream = open(output / ".acquire.lock", "a+b")
    try:
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AcquireError(f"{output}: another acquisition holds the output lock") from None
        fd = serialport.open_device(args.device, args.baud)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AcquireError(f"{args.device}: device is busy") from None
            except OSError:
                pass  # device does not support advisory locks; the output lock still serializes runs
            print(f"acquire: listening device={args.device} baud={args.baud} timeout={timeout}",
                  file=sys.stderr, flush=True)
            config = Config(
                output=output,
                calibrations=calibrations,
                policy=policy,
                calibration_sha256=calibration_sha256,
                policy_sha256=policy_sha256,
                timeout=timeout,
            )
            return serve(fd, config)
        finally:
            os.close(fd)
    finally:
        lock_stream.close()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = acquire(args)
    except (AcquireError, OSError) as exc:
        print(f"acquire failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
