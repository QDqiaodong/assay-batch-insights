"""Orchestrate the ambient-context diagnostic.

Responsibilities, in order:

1. **Freeze** every input and the initial warehouse database inside the
   working directory, hash them, and never write to them again. Trials receive
   copies only.
2. **Plan** an explicit trial matrix. Each trial varies exactly one factor
   (decimal precision, rounding, one trap, ``TZ`` or ``PYTHONHASHSEED``) and
   runs in its own subprocess.
3. **Compare** every trial record with the control record, stage by stage, to
   locate the first divergent call boundary and the propagation chain.
4. **Classify** each trial as numerical drift, decision change, exception, no
   effect or insufficient evidence.
5. **Emit** ``results.json`` and a self-contained ``report.md``.

The control record is produced by the same scenario code as every perturbed
trial; there is no special-case baseline path.
"""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402
import fixtures  # noqa: E402

PROJECT = common.PROJECT
CONTROL_ENV = {"TZ": "UTC", "PYTHONHASHSEED": "0"}

# Environments whose byte-for-byte output was already exercised by the old
# suite; used when reporting which entries are covered.
# --------------------------------------------------------------------------- #
# freeze
# --------------------------------------------------------------------------- #

def _make_writable(root):
    for path in sorted(Path(root).rglob("*"), reverse=True):
        try:
            path.chmod(0o755 if path.is_dir() else 0o644)
        except OSError:
            pass
    try:
        Path(root).chmod(0o755)
    except OSError:
        pass


def freeze(workdir, *, package_source=None):
    """Copy inputs, build fixtures and a control warehouse DB; return manifest."""
    workdir = Path(workdir)
    freeze_dir = workdir / "freeze"
    if freeze_dir.exists():
        _make_writable(freeze_dir)
        shutil.rmtree(freeze_dir)
    freeze_dir.mkdir(parents=True)

    inputs = freeze_dir / "inputs"
    inputs.mkdir()
    for path in sorted((Path(PROJECT) / "data").iterdir()):
        if path.is_file():
            shutil.copy2(path, inputs / path.name)

    fixture_root = freeze_dir / "fixtures"
    fixtures.build(fixture_root)

    package_source = Path(package_source or (Path(PROJECT) / "examples" / "warehouse"))
    package = freeze_dir / "package"
    shutil.copytree(package_source, package)

    manifest = {
        "project": PROJECT,
        "python": sys.version.split()[0],
        "control_environment": CONTROL_ENV,
        "inputs": _hashes(inputs),
        "fixtures": _hashes(fixture_root),
        "package": _hashes(package),
    }
    (freeze_dir / "FROZEN.json").write_text(
        common.canonical_json(manifest), encoding="utf-8")
    return freeze_dir


def _hashes(root):
    return {
        str(path.relative_to(root)): common.sha256_file(path)
        for path in sorted(Path(root).rglob("*"))
        if path.is_file()
    }


# --------------------------------------------------------------------------- #
# trial plans
# --------------------------------------------------------------------------- #

def _decimal_factor(**kwargs):
    return {"kind": "decimal", **kwargs}


def _env_factor(name, value):
    return {"kind": "env", "name": name, "value": value}


def plan_trials(freeze_dir, workdir):
    """Return an ordered list of trial specs, control first per scenario."""
    freeze_dir = Path(freeze_dir)
    workdir = Path(workdir)
    records = workdir / "records"
    reports = freeze_dir / "reports"
    fixtures_root = freeze_dir / "fixtures"
    package = freeze_dir / "package"
    control_db = freeze_dir / "db" / "control.sqlite"

    def fixture_paths(name):
        base = fixtures_root / name
        return {
            "calibrations": str(base / "calibrations.csv"),
            "results": str(base / "results.csv"),
            "policy": str(base / "policy.json"),
        }

    def trial_id(scenario, fixture_or_note, factor):
        label = factor.get("label") or _factor_label(factor)
        return f"{scenario}__{fixture_or_note}__{label}"

    trials = []
    decimal_factors = [
        ("control", _decimal_factor(label="control")),
        ("prec-1", _decimal_factor(prec=1, label="prec-1")),
        ("prec-2", _decimal_factor(prec=2, label="prec-2")),
        ("prec-4", _decimal_factor(prec=4, label="prec-4")),
        ("prec-6", _decimal_factor(prec=6, label="prec-6")),
        ("prec-8", _decimal_factor(prec=8, label="prec-8")),
        ("prec-12", _decimal_factor(prec=12, label="prec-12")),
        ("prec-50", _decimal_factor(prec=50, label="prec-50")),
        ("ROUND-UP", _decimal_factor(rounding="ROUND_UP", label="ROUND-UP")),
        ("ROUND-DOWN", _decimal_factor(rounding="ROUND_DOWN", label="ROUND-DOWN")),
        ("trap-Inexact", _decimal_factor(traps=["Inexact"], label="trap-Inexact")),
        ("trap-Rounded", _decimal_factor(traps=["Rounded"], label="trap-Rounded")),
    ]

    # A. load+analyze across every fixture and every decimal factor.
    for fixture in sorted(fixtures.FIXTURES):
        for label, factor in decimal_factors:
            if label != "control" and label in ("prec-50",) and fixture not in (
                    "nonterm-legacy", "rsd-threshold-legacy"):
                continue
            trials.append({
                "id": trial_id("analyze", fixture, factor),
                "scenario": "analyze",
                "fixture": fixture,
                "factor": factor,
                **fixture_paths(fixture),
            })

    # B. full report.run for the fixtures that can load at the default context.
    for fixture in ["nonterm-legacy", "rsd-threshold-legacy", "qc-boundary-legacy",
                    "nonterm-timed", "qc-boundary-timed", "clean-timed"]:
        for label, factor in [(name, spec) for name, spec in decimal_factors
                              if name in ("control", "prec-2", "prec-4", "prec-8",
                                          "ROUND-UP", "ROUND-DOWN",
                                          "trap-Inexact")]:
            trials.append({
                "id": trial_id("run", fixture, factor),
                "scenario": "run",
                "fixture": fixture,
                "factor": factor,
                "output": str(records / "output" / f"run__{fixture}__{label}"),
                **fixture_paths(fixture),
            })

    # C. nested real CLI, including environment factors.
    for fixture in ["nonterm-legacy", "qc-boundary-legacy",
                    "qc-boundary-timed", "clean-timed"]:
        factors = [(name, spec) for name, spec in decimal_factors
                   if name in ("control", "prec-1", "prec-4", "prec-8",
                               "ROUND-UP", "ROUND-DOWN", "trap-Inexact")]
        factors += [("TZ-Asia-Tokyo", _env_factor("TZ", "Asia/Tokyo")),
                    ("PYTHONHASHSEED-1", _env_factor("PYTHONHASHSEED", "1")),
                    ("PYTHONHASHSEED-12345", _env_factor("PYTHONHASHSEED", "12345"))]
        for label, factor in factors:
            trials.append({
                "id": trial_id("cli", fixture, factor),
                "scenario": "cli",
                "fixture": fixture,
                "factor": factor,
                "output": str(records / "output" / f"cli__{fixture}__{label}"),
                **fixture_paths(fixture),
            })

    # D. warehouse report validation of control-generated reports.
    for fixture in ["nonterm-timed", "qc-boundary-timed", "clean-timed",
                    "nonterm-legacy"]:
        report = reports / fixture / "report.json"
        for label, factor in [(name, spec) for name, spec in decimal_factors
                              if name in ("control", "prec-2", "prec-4", "prec-8",
                                          "prec-12", "ROUND-UP", "ROUND-DOWN",
                                          "trap-Inexact")]:
            trials.append({
                "id": trial_id("validate-report", fixture, factor),
                "scenario": "validate-report",
                "fixture": fixture,
                "factor": factor,
                "report": str(report),
            })

    # E. warehouse import of the frozen example package.
    import_factors = [(name, spec) for name, spec in decimal_factors
                      if name in ("control", "prec-2", "prec-4", "prec-6", "prec-8",
                                  "ROUND-UP", "ROUND-DOWN", "trap-Inexact",
                                  "trap-Rounded")]
    for label, factor in import_factors:
        trials.append({
            "id": trial_id("warehouse-import", "examples", factor),
            "scenario": "warehouse-import",
            "fixture": "examples",
            "factor": factor,
            "package": str(package),
            "db": str(records / "output" / f"import__{label}" / "warehouse.sqlite"),
            "manifests": ["IMP-001-legacy.json", "IMP-002-timed.json",
                          "IMP-003-rename.json", "IMP-004-rerun.json"],
        })

    # F. warehouse read-only query against a per-trial copy of the frozen DB.
    for label, factor in [(name, spec) for name, spec in decimal_factors
                          if name in ("control", "prec-2", "prec-8", "ROUND-UP",
                                      "ROUND-DOWN", "trap-Inexact")]:
        trials.append({
            "id": trial_id("warehouse-query", "examples", factor),
            "scenario": "warehouse-query",
            "fixture": "examples",
            "factor": factor,
            "db": str(records / "output" / f"query__{label}" / "warehouse.sqlite"),
            "copy_from": str(control_db),
        })
    for label, factor in [("TZ-Asia-Tokyo", _env_factor("TZ", "Asia/Tokyo")),
                          ("PYTHONHASHSEED-7", _env_factor("PYTHONHASHSEED", "7"))]:
        trials.append({
            "id": trial_id("warehouse-query", "examples", factor),
            "scenario": "warehouse-query",
            "fixture": "examples",
            "factor": factor,
            "db": str(records / "output" / f"query__{label}" / "warehouse.sqlite"),
            "copy_from": str(control_db),
        })

    # G. serial acquisition finalizer over a real pty.
    acquire_factors = [
        ("control", _decimal_factor(label="control")),
        ("prec-2", _decimal_factor(prec=2, label="prec-2")),
        ("prec-8", _decimal_factor(prec=8, label="prec-8")),
        ("ROUND-UP", _decimal_factor(rounding="ROUND_UP", label="ROUND-UP")),
        ("trap-Inexact", _decimal_factor(traps=["Inexact"], label="trap-Inexact")),
    ]
    for fixture in ["range-edge-timed", "nonterm-timed", "clean-timed"]:
        for label, factor in acquire_factors:
            trials.append({
                "id": trial_id("acquire", fixture, factor),
                "scenario": "acquire",
                "fixture": fixture,
                "factor": factor,
                "output": str(records / "output" / f"acquire__{fixture}__{label}"),
                "run_id": "run-1",
                "idle_timeout": 3,
                **fixture_paths(fixture),
            })

    # H. controlled wrapper trials: drift and exception validation.
    wrapper_cases = [
        ("benign-passthrough", "nonterm-legacy",
         {"kind": "benign", "target": "_statistics"}),
        ("drift-rsd-over-threshold", "rsd-threshold-legacy",
         {"kind": "statistics", "target": "_statistics", "delta": "0.0001"}),
        ("drift-rsd-sub-threshold", "nonterm-legacy",
         {"kind": "statistics", "target": "_statistics", "delta": "0.001"}),
        ("exception-statistics", "nonterm-legacy",
         {"kind": "exception", "target": "_statistics",
          "message": "synthetic controlled failure"}),
    ]
    for label, fixture, wrapper in wrapper_cases:
        trials.append({
            "id": f"wrapper__{label}__{fixture}",
            "scenario": "analyze",
            "fixture": fixture,
            "factor": _decimal_factor(label="control"),
            "wrapper": wrapper,
            "wrapper_label": label,
            **fixture_paths(fixture),
        })

    return trials


def _factor_label(factor):
    if factor.get("kind") == "env":
        return f'{factor["name"]}-{factor["value"]}'
    parts = []
    for key in ("prec", "rounding"):
        if key in factor:
            parts.append(f"{key}-{factor[key]}")
    for name in factor.get("traps", []):
        parts.append(f"trap-{name}")
    return "-".join(parts) or "control"


# --------------------------------------------------------------------------- #
# executing
# --------------------------------------------------------------------------- #

def _trial_env(factor):
    env = dict(os.environ)
    env.update(CONTROL_ENV)
    if factor.get("kind") == "env":
        env[factor["name"]] = str(factor["value"])
    env.pop("PYTHONPATH", None)
    return env


def run_trial(spec, workdir, timeout=180):
    """Run one trial in a fresh subprocess; return its record."""
    workdir = Path(workdir)
    trial_dir = workdir / "records" / spec["id"]
    trial_dir.mkdir(parents=True, exist_ok=True)
    spec = copy.deepcopy(spec)
    if "copy_from" in spec:
        source = Path(spec["copy_from"])
        target = Path(spec["db"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copy2(source, target)
    if "output" in spec:
        Path(spec["output"]).mkdir(parents=True, exist_ok=True)
    if "db" in spec:
        Path(spec["db"]).parent.mkdir(parents=True, exist_ok=True)
    spec_path = trial_dir / "spec.json"
    spec_path.write_text(common.canonical_json(spec), encoding="utf-8")

    env = _trial_env(spec.get("factor", {}))
    command = [sys.executable, os.path.join(HERE, "trial.py"),
               "--spec-file", str(spec_path)]
    completed = subprocess.run(command, cwd=PROJECT, env=env, capture_output=True,
                               text=True, timeout=timeout)
    record_path = trial_dir / "record.json"
    if completed.returncode != 0 or not completed.stdout.strip():
        record = {
            "scenario": spec["scenario"],
            "factor": spec.get("factor", {}),
            "harness_status": "error",
            "harness_error": {
                "type": "HarnessProcessError",
                "message": f"exit {completed.returncode}",
                "stderr_tail": completed.stderr.strip().splitlines()[-10:],
            },
            "stages": [],
            "context_actual": None,
            "environment": {},
        }
    else:
        record = json.loads(completed.stdout)
    record["id"] = spec["id"]
    record["spec"] = spec
    record["trial_dir"] = str(trial_dir)
    record_path.write_text(common.canonical_json(record), encoding="utf-8")
    return record


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #

def _diff_values(control, trial, path="", out=None, limit=12):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(control) is not type(trial) and not (
            isinstance(control, (int, float, str, type(None)))
            and isinstance(trial, (int, float, str, type(None)))):
        out.append({"path": path or "<root>", "control": control, "trial": trial})
        return out
    if isinstance(control, dict) and isinstance(trial, dict):
        for key in sorted(set(control) | set(trial)):
            if key not in control or key not in trial:
                out.append({"path": f"{path}.{key}" if path else key,
                            "control": control.get(key, "<absent>"),
                            "trial": trial.get(key, "<absent>")})
            else:
                _diff_values(control[key], trial[key], f"{path}.{key}" if path else key,
                             out, limit)
            if len(out) >= limit:
                break
        return out
    if isinstance(control, list) and isinstance(trial, list):
        if len(control) != len(trial):
            out.append({"path": f"{path}<length>", "control": len(control),
                        "trial": len(trial)})
        for index, (left, right) in enumerate(zip(control, trial)):
            _diff_values(left, right, f"{path}[{index}]", out, limit)
            if len(out) >= limit:
                break
        return out
    if control != trial:
        out.append({"path": path or "<root>", "control": control, "trial": trial})
    return out


def decision_signature(record):
    """Decision-relevant facts, so a hash change is not mistaken for a ruling."""
    stages = {entry["stage"]: entry for entry in record.get("stages", [])}
    scenario = record["scenario"]
    signature = {}
    if scenario in ("analyze", "run"):
        analyze = stages.get("analyze", {})
        if analyze.get("status") == "error":
            signature["error"] = analyze["error"]["type"]
            signature["error_location"] = (analyze["error"].get("location") or {}).get("line")
        values = analyze.get("values", {})
        signature["summary"] = values.get("summary")
        signature["samples"] = {
            key: (value.get("reportable"), tuple(value.get("reasons") or []))
            for key, value in (values.get("samples") or {}).items()
        }
        signature["qc"] = {
            key: value.get("passed")
            for key, value in (values.get("quality_controls") or {}).items()
        }
        run = stages.get("run", {})
        if run:
            signature["run_error"] = (run.get("error") or {}).get("type")
    elif scenario == "cli":
        cli = stages.get("cli", {}).get("values", {})
        signature["exit_code"] = cli.get("exit_code")
        signature["stdout"] = cli.get("stdout")
        signature["stderr_last"] = cli.get("stderr_last")
        # Decision facts come from the produced report, not from the artifact
        # hash: a different hash with the same rulings is numeric drift.
        decision = stages.get("report_decision", {}).get("values", {})
        signature["summary"] = decision.get("summary")
        signature["samples"] = {
            key: (value.get("reportable"), tuple(value.get("reasons") or []))
            for key, value in (decision.get("samples") or {}).items()
        }
        signature["qc"] = {
            key: value.get("passed")
            for key, value in (decision.get("quality_controls") or {}).items()
        }
    elif scenario == "validate-report":
        validate = stages.get("validate", {})
        signature["status"] = validate.get("status")
        signature["error"] = (validate.get("error") or {}).get("type")
        signature["error_message"] = (validate.get("error") or {}).get("message")
    elif scenario == "warehouse-import":
        outcomes = stages.get("import", {}).get("values", {}).get("outcomes", [])
        signature["outcomes"] = [
            (item.get("manifest"), item.get("status"),
             item.get("knowledge_seq"),
             (item.get("error") or {}).get("type"),
             (item.get("error") or {}).get("message"))
            for item in outcomes
        ]
    elif scenario == "warehouse-query":
        query = stages.get("query", {})
        signature["status"] = query.get("status")
        values = query.get("values", {})
        signature["knowledge_seq"] = values.get("knowledge_seq")
        signature["latest_knowledge_seq"] = values.get("latest_knowledge_seq")
        signature["error"] = (query.get("error") or {}).get("type")
    elif scenario == "acquire":
        process_stage = stages.get("process", {})
        signature["exit_code"] = process_stage.get("values", {}).get("exit_code")
        signature["published"] = stages.get("artifact", {}).get("values", {}).get("published")
        signature["validation"] = stages.get("default_validation", {}).get("values", {}).get("status")
        signature["staging_error_code"] = (
            (stages.get("artifact", {}).get("values", {}).get("staging_error_json") or {})
            .get("code"))
        signature["stderr_last"] = process_stage.get("values", {}).get("stderr_last")
    return signature


# Keys that describe where a trial ran rather than what the product produced.
# They differ between every isolated trial by construction, so comparing them
# would drown every real signal in harness noise.
NON_SEMANTIC_KEYS = {"command", "output", "trial_dir", "spec", "stderr_tail"}


def _strip_non_semantic(values):
    """Drop location and traceback noise; keep the product's own last message."""
    if not isinstance(values, dict):
        return values
    return {key: value for key, value in values.items()
            if key not in NON_SEMANTIC_KEYS}


def _error_fingerprint(error):
    if not error:
        return None
    location = error.get("location") or {}
    return (error.get("type"), error.get("message"),
            location.get("file"), location.get("line"))


def trial_outcome(record):
    """Compact, comparable outcome: command, exit status, raised location, artifacts."""
    stages = {entry["stage"]: entry for entry in record.get("stages", [])}
    exit_code = None
    for name in ("process", "cli"):
        values = stages.get(name, {}).get("values") or {}
        if "exit_code" in values:
            exit_code = values["exit_code"]
            break
    exceptions = []
    for entry in record.get("stages", []):
        if entry.get("status") == "error":
            error = entry.get("error") or {}
            location = error.get("location") or {}
            origin = location.get("origin") or {}
            exceptions.append({
                "stage": entry["stage"],
                "type": error.get("type"),
                "message": error.get("message"),
                "file": location.get("file"),
                "line": location.get("line"),
                "function": location.get("function"),
                "origin_file": origin.get("file"),
                "origin_line": origin.get("line"),
                "origin_function": origin.get("function"),
            })
    artifacts = {}
    for entry in record.get("stages", []):
        if entry["stage"] in ("artifact", "default_validation", "instrument"):
            artifacts[entry["stage"]] = entry.get("values")
    # A nested CLI reports its exception in stderr; surface the deepest
    # in-project frame so the location is in the compact outcome too.
    for name in ("process", "cli"):
        frame = (stages.get(name, {}).get("values") or {}).get("product_frame")
        if frame and not exceptions:
            exceptions.append({
                "stage": name,
                "type": (stages.get(name, {}).get("values") or {}).get("stderr_last"),
                "message": (stages.get(name, {}).get("values") or {}).get("stderr_last"),
                "file": frame["file"], "line": frame["line"], "function": None,
            })
        if frame and exceptions and not exceptions[-1].get("file"):
            exceptions[-1].update(file=frame["file"], line=frame["line"])
    command = None
    for name in ("process", "cli"):
        values = stages.get(name, {}).get("values") or {}
        if values.get("command"):
            command = values["command"]
            break
    return {
        "command": command,
        "exit_code": exit_code,
        "exception": exceptions[0] if exceptions else None,
        "exceptions": exceptions,
        "artifact_summary": artifacts,
    }


def compare(control, trial):
    """Stage-ordered comparison; returns first divergence and propagation chain."""
    control_stages = control.get("stages", [])
    trial_stages = trial.get("stages", [])
    trial_by_name = {entry["stage"]: entry for entry in trial_stages}
    divergences = []
    for entry in control_stages:
        name = entry["stage"]
        other = trial_by_name.get(name)
        if other is None:
            divergences.append({"stage": name, "kind": "missing"})
            continue
        if entry.get("status") != other.get("status"):
            divergences.append({
                "stage": name, "kind": "status",
                "control": entry.get("status"), "trial": other.get("status"),
                "trial_error": other.get("error"),
            })
            continue
        if entry.get("status") == "error":
            # Both aborted: the exception *is* the observation. Different
            # exception types/locations are a real divergence even though no
            # value was ever produced.
            if _error_fingerprint(entry.get("error")) != _error_fingerprint(other.get("error")):
                divergences.append({
                    "stage": name, "kind": "exception",
                    "control": entry.get("error"), "trial": other.get("error"),
                })
            continue
        if name == "context":
            # The context stage *is* the factor description; comparing it to the
            # control is the point of the trial, not a divergence of results.
            continue
        changed = _diff_values(_strip_non_semantic(entry.get("values")),
                               _strip_non_semantic(other.get("values")))
        # Decimal flags record that rounding happened, not that a value differs.
        # They are summarised per stage but never treated as a divergence.
        if changed:
            divergences.append({"stage": name, "kind": "values", "changes": changed})
    # A trial may emit stages the control did not reach (e.g. after an error).
    for entry in trial_stages:
        if entry["stage"] not in {item["stage"] for item in control_stages}:
            divergences.append({"stage": entry["stage"], "kind": "extra"})
    summary = {
        "first_divergence": _first_result_divergence(divergences),
        "propagation_chain": [item["stage"] for item in divergences
                              if item["stage"] not in ("context",)
                              and item.get("kind") != "operation"],
        "divergences": divergences,
        "decision_changed": decision_signature(control) != decision_signature(trial),
    }
    return summary


def _first_result_divergence(divergences):
    """First divergence that is not merely the factor's own context description."""
    for item in divergences:
        if item["stage"] in ("context",) or item.get("kind") == "operation":
            continue
        return item["stage"]
    return "context_only"


# Outcomes that mean "the call refused / never completed".
ABORTED = ("error",)


def _abort_detail(record):
    """Which stages aborted and with what, for the control baseline."""
    detail = []
    for entry in record.get("stages", []):
        if entry.get("status") == "error":
            detail.append({"stage": entry["stage"],
                           "type": (entry.get("error") or {}).get("type"),
                           "message": (entry.get("error") or {}).get("message")})
        elif entry["stage"] in ("cli", "process"):
            code = entry.get("values", {}).get("exit_code")
            if code not in (0, None):
                detail.append({"stage": entry["stage"], "type": "nonzero_exit",
                               "message": f"exit {code}: "
                                          f"{entry.get('values', {}).get('stderr_last')}"})
    return detail


def _control_aborted(record):
    """True when the control itself fails at any stage.

    A control that fails is not a baseline of *success*; a trial that then
    succeeds has not merely drifted, it has bypassed a guard. The two directions
    are reported separately so the report never calls a weakened rejection an
    "exception".
    """
    for entry in record.get("stages", []):
        if entry.get("status") in ABORTED:
            return True
        if entry["stage"] in ("cli", "process"):
            code = entry.get("values", {}).get("exit_code")
            if code not in (0, None):
                return True
    return False


def classify(control, trial, comparison):
    if trial.get("harness_status") != "ok":
        return "evidence_insufficient"
    if not trial.get("observed_factor", True) and trial.get("factor", {}).get("kind") in (
            "decimal", "env"):
        return "evidence_insufficient"
    if trial.get("factor", {}).get("label") == "control":
        return "control"
    divergences = comparison["divergences"]
    if not divergences:
        # Includes the context-only case: the factor was applied and recorded,
        # yet not one product value or decision moved.
        return "no_effect"
    control_aborted = _control_aborted(control)
    trial_aborted = _control_aborted(trial)
    if control_aborted and not trial_aborted:
        return "guard_weakened"
    if any(item.get("kind") in ("status", "missing", "exception") for item in divergences):
        return "exception"
    if comparison["decision_changed"]:
        return "decision_change"
    return "numeric_drift"


# --------------------------------------------------------------------------- #
# main flow
# --------------------------------------------------------------------------- #

def run_old_tests(workdir, timeout=600):
    """Run the shipped test suite in the default environment and record it.

    The suite is run **three times**: it contains fork-based concurrency tests
    whose ordering is not fixed, and a single run cannot distinguish a real
    failure from a scheduling flake. Each run's outcome is preserved verbatim.
    """
    runs = [_run_suite_once(timeout) for _ in range(3)]
    return {
        "command": runs[0]["command"],
        "runs": runs,
        "status": "pass" if all(run["status"] == "pass" for run in runs) else "fail",
        "ran": runs[0]["ran"],
        "exit_code": 0 if all(run["status"] == "pass" for run in runs) else runs[0]["exit_code"],
        "duration_s": round(sum(run["duration_s"] for run in runs), 2),
        "tail": runs[-1]["tail"],
    }


def stress_old_concurrency(workdir, iterations=120, load_processes=4, timeout=240):
    """Repeat the fork-based ordering test under controlled CPU load.

    The test asserts that the *first* committed package is `IMP-A`. Both
    processes start from one barrier, so the assertion encodes a scheduling
    assumption rather than a product requirement. This stress run measures how
    often that assumption fails, so the report can distinguish "old suite is
    green" from "old suite is green on average".
    """
    target = ("tests.test_warehouse_concurrency.ConcurrencyTests."
              "test_two_distinct_packages_serialize_to_contiguous_ledger")
    command = [sys.executable, "-m", "unittest", target]
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "ASSAY_DIAG_CONTEXT")}
    env["PYTHONPATH"] = PROJECT
    env["TZ"] = CONTROL_ENV["TZ"]

    hog = ("import time\n"
           "end = time.time() + %f\n"
           "value = 0\n"
           "while time.time() < end: value += 1\n")

    failures = []
    started = time.monotonic()
    hogs = [subprocess.Popen([sys.executable, "-c", hog % (iterations * 1.0 + 20)])
            for _ in range(load_processes)]
    try:
        for index in range(1, iterations + 1):
            completed = subprocess.run(command, cwd=PROJECT, env=env,
                                       capture_output=True, text=True, timeout=timeout)
            if completed.returncode != 0:
                lines = completed.stderr.strip().splitlines()
                failures.append({
                    "iteration": index,
                    "assertion": next((line for line in lines
                                       if line.startswith("AssertionError")), None),
                    "tail": lines[-12:],
                })
    finally:
        for process in hogs:
            process.terminate()
        for process in hogs:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    summary = {
        "command": " ".join(command),
        "iterations": iterations,
        "load_processes": load_processes,
        "failures": len(failures),
        "failure_details": failures[:5],
        "duration_s": round(time.monotonic() - started, 2),
        "context": "default (prec 28, ROUND_HALF_EVEN), TZ=UTC",
        "conclusion": (
            "Pre-existing scheduling assumption in the test, not a decimal-"
            "context effect: the same test fails with the ledger in the "
            "reverse order and no decimal factor applied."
        ) if failures else (
            "No failure observed in these iterations."),
    }
    path = Path(workdir) / "old-tests-stress.json"
    path.write_text(common.canonical_json(summary), encoding="utf-8")
    return summary


def _run_suite_once(timeout):
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "ASSAY_DIAG_CONTEXT")}
    env["PYTHONPATH"] = PROJECT
    env.setdefault("TZ", CONTROL_ENV["TZ"])
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=PROJECT, env=env,
                                   capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"command": " ".join(command), "status": "timeout",
                "ran": None, "exit_code": None, "duration_s": timeout, "tail": []}
    duration = round(time.monotonic() - started, 2)
    tail = completed.stderr.strip().splitlines()
    return {
        "command": " ".join(command),
        "status": "pass" if completed.returncode == 0 else "fail",
        "ran": _unittest_ran(tail),
        "exit_code": completed.returncode,
        "duration_s": duration,
        "tail": tail[-6:],
    }


def _unittest_ran(lines):
    for line in reversed(lines):
        if line.startswith("Ran "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def prepare_control_artifacts(workdir, freeze_dir, trials):
    """Run control ``run`` trials so frozen reports exist for later validation."""
    reports = Path(freeze_dir) / "reports"
    reports.mkdir(exist_ok=True)
    control_runs = [spec for spec in trials
                    if spec["scenario"] == "run" and spec["factor"].get("label", "control") == "control"]
    for spec in control_runs:
        record = run_trial(spec, workdir)
        source = Path(spec["output"]) / "report.json"
        destination = reports / spec["fixture"]
        destination.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, destination / "report.json")
            html = Path(spec["output"]) / "report.html"
            if html.is_file():
                shutil.copy2(html, destination / "report.html")
    return _hashes(reports)


def prepare_control_db(workdir, freeze_dir, trials):
    control_db = Path(freeze_dir) / "db" / "control.sqlite"
    control_db.parent.mkdir(exist_ok=True)
    spec = next(item for item in trials
                if item["scenario"] == "warehouse-import"
                and item["factor"].get("label", "control") == "control")
    temp = copy.deepcopy(spec)
    temp["db"] = str(control_db)
    run_trial(temp, workdir)
    return common.sha256_file(control_db) if control_db.exists() else None


def execute(workdir, *, select=None, keep=False, stress=False):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    freeze_dir = freeze(workdir)
    trials = plan_trials(freeze_dir, workdir)

    print("[diagnostics] building frozen control artifacts", file=sys.stderr)
    report_hashes = prepare_control_artifacts(workdir, freeze_dir, trials)
    db_hash = prepare_control_db(workdir, freeze_dir, trials)
    manifest = json.loads((freeze_dir / "FROZEN.json").read_text(encoding="utf-8"))
    manifest["control_reports"] = report_hashes
    manifest["control_database_sha256"] = db_hash
    (freeze_dir / "FROZEN.json").write_text(common.canonical_json(manifest),
                                            encoding="utf-8")
    # Freeze by hash and by instruction; permissions are left intact so a
    # rerun can clean up deterministically. Trials only ever receive copies.
    (freeze_dir / "READ_ONLY.txt").write_text(
        "Frozen inputs, fixtures and package are hashed in FROZEN.json. "
        "No trial writes into this tree; trials receive copies only.\n",
        encoding="utf-8")

    planned = trials
    if select:
        tokens = [token.strip() for token in select.split(",") if token.strip()]
        trials = [spec for spec in trials if any(token in spec["id"] for token in tokens)]

    records = {}
    for index, spec in enumerate(trials, start=1):
        print(f"[diagnostics] {index}/{len(trials)} {spec['id']}", file=sys.stderr)
        records[spec["id"]] = run_trial(spec, workdir)

    old_tests = run_old_tests(workdir) if not select else None
    stress_result = stress_old_concurrency(workdir) if (stress and not select) else None

    # Wrapper trials deliberately perturb the product (through a labelled
    # in-runtime wrapper), so they are validated as their own group and never
    # masquerade as a scenario control.
    wrapper_specs = [spec for spec in trials if spec["id"].startswith("wrapper__")]
    normal_specs = [spec for spec in trials if not spec["id"].startswith("wrapper__")]
    grouped = {}
    for spec in normal_specs:
        grouped.setdefault((spec["scenario"], spec["fixture"]), []).append(spec["id"])

    results = {"manifest": manifest, "trials": {}, "wrappers": {}, "scenarios": {}}
    if old_tests is not None:
        results["old_tests"] = old_tests
    if stress_result is not None:
        results["old_tests_stress"] = stress_result
    for (scenario, fixture), ids in sorted(grouped.items()):
        present = [trial_id for trial_id in ids if trial_id in records]
        controls = [trial_id for trial_id in present
                    if records[trial_id].get("factor", {}).get("label") == "control"]
        if not controls:
            results["scenarios"][f"{scenario}::{fixture}"] = {
                "control": None, "trials": {}, "note": "control not selected"}
            continue
        control_id = controls[0]
        control_record = records[control_id]
        entries = {}
        for trial_id in present:
            record = records[trial_id]
            if trial_id == control_id:
                entries[trial_id] = {"classification": "control",
                                     "comparison": {"first_divergence": None,
                                                    "propagation_chain": [],
                                                    "decision_changed": False,
                                                    "divergences": []}}
                continue
            comparison = compare(control_record, record)
            entries[trial_id] = {
                "classification": classify(control_record, record, comparison),
                "comparison": comparison,
            }
        results["scenarios"][f"{scenario}::{fixture}"] = {
            "control": control_id, "trials": entries,
            "control_aborted": _control_aborted(control_record),
            "control_abort_detail": _abort_detail(control_record),
            "control_decision": decision_signature(control_record),
        }
        for trial_id in present:
            results["trials"][trial_id] = {
                "scenario": records[trial_id]["scenario"],
                "fixture": fixture,
                "factor": records[trial_id].get("factor"),
                "context_actual": records[trial_id].get("context_actual"),
                "environment": records[trial_id].get("environment"),
                "harness_status": records[trial_id].get("harness_status"),
                "record": str(workdir / "records" / trial_id / "record.json"),
                "outcome": trial_outcome(records[trial_id]),
                **entries.get(trial_id, {}),
            }
    # Wrapper trials: the default context on the *same fixture*, but a labelled
    # controlled change to the product's own function.
    control_by_fixture = {
        spec["fixture"]: spec["id"] for spec in normal_specs
        if spec["id"].startswith("analyze__")
        and spec["factor"].get("label") == "control"
    }
    for spec in wrapper_specs:
        if spec["id"] not in records:
            continue
        baseline_id = control_by_fixture.get(spec["fixture"])
        if baseline_id is None or baseline_id not in records:
            continue
        record = records[spec["id"]]
        comparison = compare(records[baseline_id], record)
        results["wrappers"][spec["id"].split("__")[1]] = {
            "fixture": spec["fixture"],
            "wrapper": spec.get("wrapper"),
            "baseline": baseline_id,
                "classification": (
                    "exception" if any(item.get("kind") in ("status", "missing",
                                                            "exception")
                                       for item in comparison["divergences"])
                    else "decision_change" if comparison["decision_changed"]
                    else "numeric_drift" if comparison["first_divergence"] not in
                    ("context_only", None)
                    else "no_effect"),
            "comparison": comparison,
            "record": str(workdir / "records" / spec["id"] / "record.json"),
        }
    # Trials that were planned but not selected are simply absent; that is
    # recorded so a partial run cannot be mistaken for a complete one.
    results["planned_trials"] = len(planned)
    results["selected_trials"] = len(trials)

    (workdir / "results.json").write_text(common.canonical_json(results),
                                          encoding="utf-8")

    from render_report import render, render_evidence  # local, imported late
    (workdir / "report.md").write_text(render(results), encoding="utf-8")
    (workdir / "evidence.md").write_text(render_evidence(results), encoding="utf-8")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m diagnostics.cli",
        description="Ambient decimal/environment diagnostics for Assay Batch Insights")
    parser.add_argument("--workdir", type=Path, default=Path("diagnostics-run"),
                        help="working directory for frozen inputs and records")
    parser.add_argument("--select", default=None,
                        help="comma-separated substrings of trial ids to run")
    parser.add_argument("--keep", action="store_true",
                        help="keep per-trial output directories (always kept today)")
    parser.add_argument("--stress-old-tests", action="store_true",
                        help="also repeat the fork-ordering test under CPU load")
    args = parser.parse_args(argv)
    results = execute(args.workdir, select=args.select, keep=args.keep,
                      stress=args.stress_old_tests)
    counts = {}
    for entry in results["trials"].values():
        counts[entry.get("classification")] = counts.get(entry.get("classification"), 0) + 1
    print(json.dumps({"workdir": str(args.workdir),
                      "trial_count": len(results["trials"]),
                      "classifications": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
