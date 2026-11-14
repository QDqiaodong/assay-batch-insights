"""One isolated trial: a fresh process that perturbs exactly one host factor.

The parent orchestrator launches this file once per trial. It applies the
requested factor, executes one scenario *through the product's real entry
points*, and prints a single JSON record describing what it observed.

The file never edits product code. Where it needs a call-boundary observation
it wraps a product function at runtime, records the value, and restores it in a
``finally`` block. The wrapper is either a transparent recorder (the default) or
an explicitly labelled controlled drift/exception used only to validate the
detector.

Usage::

    python diagnostics/trial.py --spec '<json>'
    python diagnostics/trial.py --spec-file /path/to/spec.json

The JSON record is written to stdout; diagnostics go to stderr. Exit status is
0 whenever a record was produced, even if the product raised (the product error
is part of the record). A non-zero exit means the harness itself failed.
"""
import argparse
import hashlib
import json
import os
import pty
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402

PROJECT = common.PROJECT
# Factor specification keys that are metadata rather than decimal-context fields.
CONTEXT_META_KEYS = {"kind", "label"}
sys.path.insert(0, PROJECT)


# --------------------------------------------------------------------------- #
# evidence helpers
# --------------------------------------------------------------------------- #

def exception_record(exc):
    """Type, message, deepest in-project frame and a compact traceback."""
    frames = traceback.extract_tb(exc.__traceback__)
    project_frames = [frame for frame in frames
                      if os.path.abspath(frame.filename).startswith(PROJECT)]
    location = None
    if project_frames:
        frame = project_frames[-1]
        location = {
            "file": os.path.relpath(os.path.abspath(frame.filename), PROJECT),
            "line": frame.lineno,
            "function": frame.name,
            "source": (frame.line or "").strip(),
        }
        # A bare `raise X(...)` inside a `_fail` helper is the re-raise site,
        # not where the contract was actually breached. Report the caller too.
        if frame.name.startswith("_fail") or (frame.line or "").strip().startswith("raise "):
            for caller in reversed(project_frames[:-1]):
                if not caller.name.startswith("_fail"):
                    location["origin"] = {
                        "file": os.path.relpath(os.path.abspath(caller.filename), PROJECT),
                        "line": caller.lineno,
                        "function": caller.name,
                        "source": (caller.line or "").strip(),
                    }
                    break
    return {
        "type": f"{type(exc).__module__}.{type(exc).__name__}",
        "message": str(exc),
        "location": location,
        "frames": [
            {
                "file": os.path.relpath(os.path.abspath(frame.filename), PROJECT)
                if os.path.abspath(frame.filename).startswith(PROJECT)
                else frame.filename,
                "line": frame.lineno,
                "function": frame.name,
            }
            for frame in frames[-8:]
        ],
    }


def stage(name, status="ok", values=None, flags=None, error=None):
    return {
        "stage": name,
        "status": status,
        "values": values if values is not None else {},
        "flags": flags or [],
        "error": error,
    }


def _exact(value):
    if value is None:
        return None
    return common.decimal_text(value) if hasattr(value, "as_tuple") else str(value)


def _group_key(records):
    first = records[0]
    return {
        "batch": first.get("batch"),
        "analyte": first.get("analyte"),
        "sample": first.get("sample"),
        "kind": first.get("kind"),
        "calibration_id": first.get("calibration_id"),
    }


# --------------------------------------------------------------------------- #
# wrappers
# --------------------------------------------------------------------------- #

def _apply_wrapper(module, wrapper):
    """Install a labelled controlled wrapper. Returns a restore callback."""
    if not wrapper:
        return lambda: None
    kind = wrapper["kind"]
    target_name = wrapper["target"]
    if kind == "statistics":
        original = module._statistics
        delta = common.parse_decimal(wrapper["delta"]) if kind == "statistics" else None

        def drifted(records):
            n, mean, sd, rsd = original(records)
            if rsd is not None:
                rsd = rsd + common.parse_decimal(wrapper["delta"])
            return n, mean, sd, rsd

        module._statistics = drifted
        return lambda: setattr(module, "_statistics", original)
    if kind == "exception":
        original = getattr(module, target_name)

        def boom(*args, **kwargs):
            raise RuntimeError(wrapper["message"])

        setattr(module, target_name, boom)
        return lambda: setattr(module, target_name, original)
    if kind == "benign":
        original = getattr(module, target_name)

        def passthrough(*args, **kwargs):
            return original(*args, **kwargs)

        setattr(module, target_name, passthrough)
        return lambda: setattr(module, target_name, original)
    raise ValueError(f"unknown wrapper kind {kind!r}")


def observe_analysis(metrics, rows, policy):
    """Run analyze with a transparent recorder around ``_statistics``.

    Returns the product report (or the raised exception), the exact unrounded
    statistics of every group the product actually formed, and the exact QC
    recoveries. The recorder is removed in ``finally``.
    """
    original = metrics._statistics
    recorded = []

    def recorder(records):
        n, mean, sd, rsd = original(records)
        recorded.append({
            "group": _group_key(records),
            "records": len(records),
            "mean_exact": _exact(mean),
            "sd_exact": _exact(sd),
            "rsd_exact": _exact(rsd),
            "target_exact": _exact(records[0].get("target")),
        })
        return n, mean, sd, rsd

    metrics._statistics = recorder
    common.clear_flags()
    try:
        report = metrics.analyze(rows, policy)
        error = None
    except Exception as exc:  # noqa: BLE001 - an exception is evidence
        report = None
        error = exception_record(exc)
    finally:
        metrics._statistics = original
    return report, recorded, error, common.describe_context()


def summary_stages(recorded):
    statistics = {}
    recoveries = {}
    for entry in recorded:
        key = "/".join(str(entry["group"][part]) for part in
                       ("batch", "analyte", "sample", "calibration_id"))
        statistics[key] = {
            "n": entry["records"],
            "mean_exact": entry["mean_exact"],
            "sd_exact": entry["sd_exact"],
            "rsd_exact": entry["rsd_exact"],
        }
        if entry["group"]["kind"] == "QC" and entry["target_exact"] not in (None, "0"):
            target = common.parse_decimal(entry["target_exact"])
            mean = common.parse_decimal(entry["mean_exact"])
            if target and mean is not None:
                recovery = mean / target * 100
                recoveries[key] = {
                    "recovery_exact": common.decimal_text(recovery),
                    "limits": ["80", "120"],
                }
    return statistics, recoveries


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #

def scenario_analyze(spec):
    """Load + analyze with both call boundaries observed in one pass."""
    from assay_insights import input as input_module
    from assay_insights import metrics
    stages = [stage("context", values=common.describe_context())]
    common.clear_flags()
    try:
        rows, policy = input_module.load(spec["calibrations"], spec["results"], spec["policy"])
        stages.append(stage("load", values={
            "measurements": [
                {
                    "key": "/".join(str(row.get(part)) for part in
                                    ("batch", "analyte", "sample", "replicate")),
                    "concentration_exact": _exact(row.get("concentration")),
                }
                for row in rows
            ],
        }, flags=common.raised_flags()))
    except Exception as exc:  # noqa: BLE001
        stages.append(stage("load", "error", error=exception_record(exc),
                            flags=common.raised_flags()))
        return stages

    restore_wrapper = _apply_wrapper(metrics, spec.get("wrapper"))
    common.clear_flags()
    try:
        report, recorded, error, _context = observe_analysis(metrics, rows, policy)
        statistics, recoveries = summary_stages(recorded)
        stages.append(stage("statistics", values=statistics, flags=common.raised_flags()))
        stages.append(stage("qc_recovery", values=recoveries, flags=common.raised_flags()))
        if error is not None:
            stages.append(stage("analyze", "error", error=error, flags=common.raised_flags()))
            return stages
        stages.append(stage("analyze", values=_report_decision_view(report),
                            flags=common.raised_flags()))
    finally:
        restore_wrapper()
    return stages


def _report_decision_view(report):
    return {
        "summary": report.get("summary"),
        "samples": {
            "/".join(str(sample[part]) for part in ("batch", "analyte", "sample")): {
                "mean": sample.get("mean_mg_l"),
                "sd": sample.get("sample_sd_mg_l"),
                "rsd": sample.get("rsd_pct"),
                "reportable": sample.get("reportable"),
                "reasons": sample.get("reasons"),
            }
            for sample in report.get("samples", [])
        },
        "quality_controls": {
            "/".join(str(qc.get(part)) for part in
                     ("batch", "analyte", "sample", "calibration_id")): {
                "recovery": qc.get("recovery_pct"),
                "passed": qc.get("passed"),
            }
            for qc in report.get("quality_controls", [])
        },
    }


def scenario_run(spec):
    """Real ``report.run`` call, then a decision view from the same context."""
    from assay_insights import metrics, report as report_module
    stages = [stage("context", values=common.describe_context())]
    output = Path(spec["output"])
    common.clear_flags()
    try:
        result = report_module.run(spec["calibrations"], spec["results"],
                                   spec["policy"], output)
        stages.append(stage("run", values={
            "summary": result.get("summary"),
            "schema_version": result.get("schema_version"),
        }, flags=common.raised_flags()))
    except Exception as exc:  # noqa: BLE001
        stages.append(stage("run", "error", error=exception_record(exc),
                            flags=common.raised_flags()))
        return _artifact_stage(stages, output, spec)
    stages.append(stage("analyze", values=_report_decision_view(result),
                        flags=common.raised_flags()))
    return _artifact_stage(stages, output, spec)


def _artifact_stage(stages, output, spec):
    artifacts = {}
    for name in ("report.json", "report.html"):
        path = Path(output) / name
        if path.is_file():
            artifacts[name] = {"sha256": common.sha256_file(path),
                               "bytes": path.stat().st_size}
    stages.append(stage("artifact", values=artifacts))
    return stages


def scenario_cli(spec):
    """Spawn the real ``python -m assay_insights`` CLI as a nested process."""
    output = Path(spec["output"])
    command = [sys.executable, "-m", "assay_insights",
               "--calibrations", spec["calibrations"],
               "--results", spec["results"],
               "--policy", spec["policy"],
               "--output", str(output)]
    completed = subprocess.run(command, cwd=PROJECT, env=_shim_env(spec),
                               capture_output=True, text=True,
                               timeout=spec.get("timeout", 60))
    stages = [stage("context", values=_shim_context(spec)),
              stage("cli", values={
                  "command": command,
                  "exit_code": completed.returncode,
                  "stdout": completed.stdout.strip(),
                  "stderr_last": (completed.stderr.strip().splitlines() or [None])[-1],
                  "stderr_tail": completed.stderr.strip().splitlines()[-6:],
                  "product_frame": _deepest_product_frame(completed.stderr),
              })]
    report_path = output / "report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        stages.append(stage("report_decision", values=_report_decision_view(report)))
    return _artifact_stage(stages, output, spec)


def scenario_validate_report(spec):
    """Run the warehouse report validator on one captured report."""
    from assay_insights.warehouse.reports import load_report
    raw = Path(spec["report"]).read_bytes()
    stages = [stage("context", values=common.describe_context())]
    common.clear_flags()
    try:
        loaded = load_report(raw)
        stages.append(stage("validate", values={
            "report_sha256": loaded["sha256"],
            "canonical_sha256": loaded["canonical_sha256"],
            "schema_version": loaded["report"]["schema_version"],
        }, flags=common.raised_flags()))
    except Exception as exc:  # noqa: BLE001
        stages.append(stage("validate", "error", error=exception_record(exc),
                            flags=common.raised_flags()))
    return stages


def scenario_warehouse_import(spec):
    """Import frozen manifests into a trial-private database, then query it."""
    from assay_insights.warehouse.importer import import_manifest
    from assay_insights.warehouse.query import build_query, render_query
    package = Path(spec["package"])
    db = Path(spec["db"])
    stages = [stage("context", values=common.describe_context())]
    results = []
    for manifest in spec["manifests"]:
        common.clear_flags()
        results.append({"manifest": manifest, "status": "not_attempted"})
        try:
            outcome = import_manifest(db, package / manifest)
            results[-1] = {"manifest": manifest, "status": "committed",
                           "knowledge_seq": outcome["knowledge_seq"],
                           "created_kind": outcome["created_kind"]}
        except Exception as exc:  # noqa: BLE001
            results[-1] = {"manifest": manifest, "status": "error",
                           "error": exception_record(exc)}
        stages.append(stage(f"import:{manifest}", values=results[-1],
                            flags=common.raised_flags()))
        if results[-1]["status"] == "error":
            for remaining in spec["manifests"][len(results):]:
                results.append({"manifest": remaining, "status": "not_attempted"})
            break
    stages.append(stage("import", values={"outcomes": results},
                        flags=common.raised_flags()))
    # Query under the interpreter default context, proving read-side stability
    # does not depend on the write-side factor under test.
    default = common.describe_context()
    _reset_to_default()
    try:
        stage_values = {"query_context": common.describe_context(),
                        "query_sha256": None, "knowledge_seq": None}
        if db.exists():
            query = build_query(db)
            text = render_query(query)
            stage_values.update({
                "query_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "knowledge_seq": query["knowledge_seq"],
                "latest_knowledge_seq": query["latest_knowledge_seq"],
            })
        stages.append(stage("query", values=stage_values))
    except Exception as exc:  # noqa: BLE001
        stages.append(stage("query", "error", error=exception_record(exc)))
    finally:
        _restore_context(default)
    return stages


def scenario_warehouse_query(spec):
    from assay_insights.warehouse.query import build_query, render_query
    stages = [stage("context", values=common.describe_context())]
    common.clear_flags()
    try:
        query = build_query(spec["db"])
        text = render_query(query)
        stages.append(stage("query", values={
            "query_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "knowledge_seq": query["knowledge_seq"],
            "latest_knowledge_seq": query["latest_knowledge_seq"],
        }, flags=common.raised_flags()))
    except Exception as exc:  # noqa: BLE001
        stages.append(stage("query", "error", error=exception_record(exc),
                            flags=common.raised_flags()))
    return stages


def scenario_acquire(spec):
    """Play one run against the real acquisition CLI over a private pty.

    The nested CLI inherits the factor through a generated ``sitecustomize``.
    Every descriptor is closed and the child process group is reaped in
    ``finally``; a timeout never leaves a lingering process behind.
    """
    from assay_insights.acquire import protocol
    from assay_insights.acquire.serialport import configure
    import csv

    output = Path(spec["output"])
    rows = list(csv.DictReader(open(spec["results"], encoding="utf-8", newline="")))
    calibration_sha = common.sha256_file(spec["calibrations"])
    nested_env = _shim_env(spec)
    master = slave = None
    process = None
    instrument_error = None
    stages = []
    try:
        master, slave = pty.openpty()
        configure(slave, 115200)
        slave_path = os.ttyname(slave)
        command = [sys.executable, "-m", "assay_insights.acquire",
                   "--device", slave_path, "--baud", "115200",
                   "--calibrations", spec["calibrations"],
                   "--policy", spec["policy"],
                   "--output", str(output),
                   "--timeout", str(spec.get("idle_timeout", 3))]
        process = subprocess.Popen(command, cwd=PROJECT, env=nested_env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            _wait_for_marker(process, "listening", spec.get("ready_timeout", 20))
        except Exception as exc:  # noqa: BLE001
            instrument_error = f"readiness: {exc}"
        else:
            try:
                _play_session(protocol, master, spec["run_id"], rows, calibration_sha,
                              reply_timeout=spec.get("reply_timeout", 6))
            except Exception as exc:  # noqa: BLE001
                instrument_error = f"session: {exc}"
    finally:
        # Closing both pty ends gives the CLI EOF before we force-kill.
        for descriptor in (master, slave):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process is not None:
            _reap(process, spec.get("exit_timeout", 20))
    # Read the pipes *before* probing the filesystem: a probe that opened a
    # still-live stdout pipe would block until the child exited.
    stdout = stderr = ""
    if process is not None:
        stdout, stderr = process.communicate()
        stages.append(stage("process", values={
            "command": command,
            "exit_code": process.returncode,
            "stdout": stdout.strip(),
            # Only the last line carries the product's own diagnostic; earlier
            # lines are a Python traceback whose wording varies by mode.
            "stderr_last": (stderr.strip().splitlines() or [None])[-1],
            "stderr_tail": stderr.strip().splitlines()[-8:],
            "product_frame": _deepest_product_frame(stderr),
            "reaped": process.returncode is not None,
        }))
    stages.insert(0, stage("context", values=_shim_context(spec)))
    stages.append(stage("instrument", values={"outcome": instrument_error or "acked"}))
    stages.append(stage("artifact", values=_acquire_artifacts(output, spec["run_id"])))
    # Independent default-context validation of whatever got published.
    stages.append(stage("default_validation", values=_validate_published(
        spec, output / spec["run_id"] / "results.csv")))
    return stages


def _deepest_product_frame(text):
    """Last ``File "assay_insights/...`` frame in a child traceback, if any."""
    deepest = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith('File "'):
            continue
        try:
            path, rest = stripped[6:].split('", line ', 1)
        except ValueError:
            continue
        if not os.path.abspath(path).startswith(PROJECT):
            continue
        deepest = {
            "file": os.path.relpath(os.path.abspath(path), PROJECT),
            "line": int(rest.split(",", 1)[0]),
        }
    return deepest


def _play_session(protocol, master, run_id, rows, calibration_sha, reply_timeout):
    from assay_insights.acquire import tester
    instrument = tester.Instrument(master, reply_timeout=reply_timeout)
    batch = rows[0]["batch"] if rows else "B1"
    payloads = []
    instrument.send_payload(protocol.begin_payload(run_id, batch, len(rows), calibration_sha))
    instrument.expect_ack(0)
    for seq, row in enumerate(rows, start=1):
        payload = protocol.measurement_payload(seq, row)
        payloads.append(payload)
        instrument.send_payload(payload)
        instrument.expect_ack(seq)
    instrument.send_payload(protocol.end_payload(
        len(rows) + 1, len(rows), protocol.measurements_digest(payloads)))
    instrument.expect_ack(len(rows) + 1)


def _wait_for_marker(process, marker, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if line == "":
            if process.poll() is not None:
                raise RuntimeError("process exited before readiness")
            time.sleep(0.01)
            continue
        if marker in line:
            return line
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


def _acquire_artifacts(output, run_id):
    run_dir = Path(output) / run_id
    artifacts = {}
    if run_dir.is_dir():
        for name in ("results.csv", "frames.log", "summary.json"):
            path = run_dir / name
            if path.is_file():
                artifacts[name] = {"sha256": common.sha256_file(path),
                                   "bytes": path.stat().st_size}
        summary = run_dir / "summary.json"
        if summary.is_file():
            artifacts["summary"] = json.loads(summary.read_text(encoding="utf-8"))
    error = Path(output) / ".staging" / run_id / "error.json"
    artifacts["published"] = run_dir.is_dir()
    artifacts["staging_error_json"] = (
        json.loads(error.read_text(encoding="utf-8")) if error.is_file() else None
    )
    artifacts["staging_exists"] = (Path(output) / ".staging" / run_id).is_dir()
    return artifacts


def _validate_published(spec, results_csv):
    if not results_csv.is_file():
        return {"status": "not-published"}
    command = [sys.executable, "-c",
               "from assay_insights.input import load; "
               "load(%r, %r, %r); print('accepted')" % (
                   spec["calibrations"], str(results_csv), spec["policy"])]
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH",)}
    completed = subprocess.run(command, cwd=PROJECT,
                               env=dict(env, PYTHONPATH=PROJECT),
                               capture_output=True, text=True, timeout=30)
    return {
        "status": "accepted" if completed.returncode == 0 else "rejected",
        "exit_code": completed.returncode,
        "stderr_tail": completed.stderr.strip().splitlines()[-3:],
    }


def _nested_context(spec):
    """Context the nested CLI must run under: explicit spec first, else factor."""
    if spec.get("context"):
        return dict(spec["context"])
    factor = spec.get("factor", {})
    if factor.get("kind") == "decimal":
        return {key: value for key, value in factor.items()
                if key not in CONTEXT_META_KEYS}
    return {}


def _shim_env(spec):
    """Environment for a nested CLI: static shim directory + context JSON.

    The shim is a committed file (``diagnostics/shim/sitecustomize.py``), so the
    exact same PYTHONPATH/ASSAY_DIAG_CONTEXT pair can be replayed by hand.
    """
    shim_dir = os.path.join(HERE, "shim")
    env = dict(os.environ)
    env["PYTHONPATH"] = shim_dir + os.pathsep + PROJECT
    context = _nested_context(spec)
    if context:
        env["ASSAY_DIAG_CONTEXT"] = json.dumps(context, sort_keys=True)
    else:
        env.pop("ASSAY_DIAG_CONTEXT", None)
    return env


def _shim_context(spec):
    """The context as the nested process would apply it, resolved locally."""
    context = common.describe_context()
    requested = _nested_context(spec)
    effective = dict(context)
    if "prec" in requested:
        effective["prec"] = int(requested["prec"])
    if "rounding" in requested:
        effective["rounding"] = requested["rounding"]
    if "traps" in requested:
        effective["traps"] = sorted(set(context["traps"]) | set(requested["traps"]))
    effective["source"] = "nested-process sitecustomize" if requested else "interpreter default"
    return effective


def _reset_to_default():
    import decimal
    from decimal import DefaultContext
    decimal.setcontext(DefaultContext.copy())


def _restore_context(snapshot):
    import decimal
    from decimal import Context
    context = Context(prec=snapshot["prec"])
    context.rounding = snapshot["rounding"]
    context.Emin = snapshot["Emin"]
    context.Emax = snapshot["Emax"]
    context.clamp = snapshot["clamp"]
    for name in snapshot["traps"]:
        context.traps[common.TRAPS[name]] = True
    decimal.setcontext(context)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

SCENARIOS = {
    "analyze": scenario_analyze,
    "run": scenario_run,
    "cli": scenario_cli,
    "validate-report": scenario_validate_report,
    "warehouse-import": scenario_warehouse_import,
    "warehouse-query": scenario_warehouse_query,
    "acquire": scenario_acquire,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one isolated diagnostic trial")
    parser.add_argument("--spec", default=None, help="trial specification as JSON")
    parser.add_argument("--spec-file", default=None, type=Path,
                        help="path to a trial specification JSON file")
    args = parser.parse_args(argv)
    if args.spec_file is not None:
        spec = json.loads(args.spec_file.read_text(encoding="utf-8"))
    elif args.spec is not None:
        spec = json.loads(args.spec)
    else:
        parser.error("--spec or --spec-file is required")

    factor = spec.get("factor", {})
    requested = dict(spec.get("context", {}))
    if factor.get("kind") == "decimal":
        requested = {key: value for key, value in factor.items()
                     if key not in CONTEXT_META_KEYS}
    common.apply_context(requested)
    # The record must state the context actually in force, read back from the
    # live interpreter, not the context that was requested.
    actual = common.describe_context()
    record = {
        "scenario": spec["scenario"],
        "factor": factor,
        "environment": {
            "TZ": os.environ.get("TZ"),
            "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED"),
            "python": sys.version.split()[0],
        },
        "context_requested": requested,
        "context_actual": actual,
    }
    try:
        record["stages"] = SCENARIOS[spec["scenario"]](spec)
        record["harness_status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - harness failure is still a record
        record["stages"] = []
        record["harness_status"] = "error"
        record["harness_error"] = exception_record(exc)
    record["observed_factor"] = _observed_factor(factor, record)
    record = _redact_paths(record, spec)
    print(json.dumps(record, sort_keys=True))
    return 0


def _redact_paths(value, spec):
    """Replace trial-specific absolute paths so records compare fairly.

    Every isolated trial writes to its own output directory. Without redaction,
    the directory name embedded in product diagnostics would look like a
    behaviour difference. The longest path is replaced first so a trial's own
    directory collapses fully to ``<trial-dir>``; its parent becomes
    ``<trial-root>``.
    """
    replacements = []

    def add(path):
        text = str(Path(path))
        if text and (text, "<trial-dir>") not in replacements:
            replacements.append((text, "<trial-dir>"))

    for key in ("output", "db", "trial_dir", "report"):
        if key not in spec:
            continue
        raw = str(spec[key])
        add(raw)
        parent = str(Path(raw).parent)
        if parent and parent != raw:
            add(parent)
    # Longest needle first: a parent must not pre-empt its own child.
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)
    if isinstance(value, str):
        for needle, replacement in replacements:
            value = value.replace(needle, replacement)
        return value
    if isinstance(value, dict):
        return {key: _redact_paths(item, spec) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_paths(item, spec) for item in value]
    return value


def _observed_factor(factor, record):
    """Prove the factor reached the process instead of assuming that it did."""
    kind = factor.get("kind")
    actual = record.get("context_actual") or {}
    if kind == "decimal":
        keys = [key for key in factor if key not in CONTEXT_META_KEYS]
        return all(actual.get(key) == (int(factor[key]) if key == "prec" else factor[key])
                   for key in keys if key != "traps") and \
            all(name in actual.get("traps", []) for name in factor.get("traps", []))
    if kind == "env":
        key = factor.get("name")
        return record.get("environment", {}).get(key) == factor.get("value")
    return True
    if kind == "env":
        key = factor.get("name")
        return record.get("environment", {}).get(key) == factor.get("value")
    return True


if __name__ == "__main__":
    sys.exit(main())
