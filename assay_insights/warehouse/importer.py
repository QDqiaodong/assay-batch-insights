"""Transactional manifest import into the longitudinal warehouse.

The import is a single SQLite write transaction. All file capture and structural
validation happens before the transaction; identity resolution, corrections,
knowledge sequencing and every insert happen inside one ``BEGIN IMMEDIATE``
transaction and are rolled back together on any failure.
"""
import hashlib
import json
import sqlite3

from .errors import (
    ConflictError, IdentityError, ManifestError, WarehouseError,
)
from .manifest import canonical_json_bytes, parse_manifest
from .paths import safe_relative_path
from .reports import load_report
from .schema import begin_immediate, connect, next_knowledge_seq
from .state import apply_correction, assert_no_display_collision


def capture_manifest(manifest_path, *, known_report_hashes=None):
    """Read the manifest and every referenced report exactly once.

    Returns a captured package with raw bytes, parsed structures and safe
    resolved paths. Nothing in this function touches the database.
    ``known_report_hashes`` optionally supplies already-imported report hashes
    so correction targets can be validated without opening a write transaction.
    """
    known_report_hashes = set(known_report_hashes or ())
    with open(manifest_path, "rb") as stream:
        manifest_bytes = stream.read()
    manifest, manifest_canonical, manifest_sha256 = parse_manifest(manifest_bytes)
    manifest_dir = manifest_path.parent
    captured_reports = []
    seen_paths, seen_labels, seen_raw_sha = set(), set(), set()
    for ordinal, entry in enumerate(manifest["reports"]):
        if entry["path"] in seen_paths:
            raise ManifestError(f"duplicate report path in manifest: {entry['path']!r}")
        seen_paths.add(entry["path"])
        resolved, path_label = safe_relative_path(entry["path"], manifest_dir)
        with open(resolved, "rb") as stream:
            raw = stream.read()
        loaded = load_report(raw)
        if loaded["sha256"] in seen_raw_sha:
            raise ManifestError(
                f"report content is repeated within one manifest: {entry['path']!r}"
            )
        seen_raw_sha.add(loaded["sha256"])
        captured_reports.append({
            "ordinal": ordinal,
            "label": entry["label"],
            "path_label": path_label,
            "raw": raw,
            "loaded": loaded,
        })
    # Correction targets must reference either a packaged report or one already
    # imported; an unknown hash is rejected before the write transaction.
    known_hashes = {item["loaded"]["sha256"] for item in captured_reports}
    known_hashes |= known_report_hashes
    for correction in manifest["corrections"]:
        target_sha = correction["target"]["report_sha256"]
        if target_sha not in known_hashes:
            raise IdentityError(
                f"correction {correction['correction_id']} targets unknown report {target_sha}"
            )
    return {
        "manifest": manifest,
        "manifest_bytes": manifest_bytes,
        "manifest_canonical": manifest_canonical,
        "manifest_sha256": manifest_sha256,
        "reports": captured_reports,
    }


def _existing_report_hashes(db_path):
    """Return imported raw report hashes, or an empty set for a new database."""
    import os
    if not os.path.exists(db_path):
        return set()
    probe = connect(db_path, create=False)
    try:
        return {
            row["report_sha256"]
            for row in probe.execute("SELECT report_sha256 FROM report_snapshots")
        }
    finally:
        probe.close()


def import_manifest(db_path, manifest_path):
    """Import one manifest package with full idempotency and rollback semantics."""
    # A read-only look at already-imported report hashes lets capture-time
    # validation accept corrections that target earlier packages. The write
    # transaction remains the single authority for identity resolution.
    existing_hashes = _existing_report_hashes(db_path)
    package = capture_manifest(manifest_path, known_report_hashes=existing_hashes)
    connection = connect(db_path, create=True)
    try:
        begin_immediate(connection)
        try:
            result = _import_transaction(connection, package)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise WarehouseError(f"sqlite transaction failed: {exc}") from exc
    finally:
        connection.close()
    return result


def _import_transaction(connection, package):
    manifest = package["manifest"]
    import_id = manifest["import_id"]
    existing = connection.execute(
        "SELECT * FROM imports WHERE import_id = ?", (import_id,)
    ).fetchone()
    if existing is not None:
        return _replay_or_conflict(connection, package, existing)

    # A brand-new import_id may not reuse a correction_id already known.
    for correction in manifest["corrections"]:
        row = connection.execute(
            "SELECT 1 FROM corrections WHERE correction_id = ?",
            (correction["correction_id"],),
        ).fetchone()
        if row is not None:
            raise ConflictError(
                "correction_id already exists with different content: "
                f"{correction['correction_id']!r}"
            )

    knowledge_seq = next_knowledge_seq(connection)
    # The import row must exist before fact sets/snapshots can reference it.
    connection.execute(
        "INSERT INTO imports"
        "(import_id, manifest_sha256, manifest_canonical_sha256, manifest_raw, "
        "manifest_canonical, action) VALUES (?, ?, ?, ?, ?, 'apply')",
        (
            import_id, package["manifest_sha256"],
            hashlib.sha256(package["manifest_canonical"]).hexdigest(),
            package["manifest_bytes"], package["manifest_canonical"],
        ),
    )
    new_canonical, new_raw_snapshots = _insert_snapshots_and_sources(
        connection, package, import_id, knowledge_seq
    )
    _insert_identities(connection, new_raw_snapshots)
    state = _load_current_states(connection)
    correction_events = _apply_manifest_corrections(connection, package, state)

    has_new_facts = bool(new_canonical)
    has_new_corrections = bool(correction_events)
    if not has_new_facts and not has_new_corrections:
        # Only provenance/source relationships changed; still a real, ordered
        # transaction that receives a ledger sequence.
        created_kind = "source_only_transaction"
    else:
        created_kind = "fact_transaction"
    connection.execute(
        "INSERT INTO knowledge_ledger"
        "(knowledge_seq, import_id, manifest_sha256, new_facts, new_corrections, created_kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (knowledge_seq, import_id, package["manifest_sha256"],
         int(has_new_facts), int(has_new_corrections), created_kind),
    )
    _persist_corrections(connection, package, correction_events, knowledge_seq)
    return {
        "import_id": import_id,
        "knowledge_seq": knowledge_seq,
        "idempotent_replay": False,
        "new_canonical_fact_sets": sorted(new_canonical),
        "new_report_snapshots": sorted(new_raw_snapshots),
        "new_corrections": [c["correction_id"] for c in correction_events],
        "created_kind": created_kind,
    }


def _replay_or_conflict(connection, package, existing):
    manifest = package["manifest"]
    import_id = manifest["import_id"]
    if existing["manifest_sha256"] != package["manifest_sha256"]:
        raise ConflictError(
            f"import_id {import_id!r} already exists with different manifest bytes"
        )
    rows = connection.execute(
        "SELECT report_sha256, label FROM import_reports WHERE import_id = ? "
        "ORDER BY ordinal",
        (import_id,),
    ).fetchall()
    packaged = [
        (item["loaded"]["sha256"], item["label"]) for item in package["reports"]
    ]
    if [(row["report_sha256"], row["label"]) for row in rows] != packaged:
        raise ConflictError(
            f"import_id {import_id!r} already exists with different report bytes or labels"
        )
    stored_corrections = connection.execute(
        "SELECT canonical_json FROM corrections WHERE import_id = ? "
        "ORDER BY knowledge_seq, ordinal",
        (import_id,),
    ).fetchall()
    expected = [
        canonical_json_bytes(correction).decode("utf-8")
        for correction in manifest["corrections"]
    ]
    if [row["canonical_json"] for row in stored_corrections] != expected:
        raise ConflictError(
            f"import_id {import_id!r} already exists with different correction content"
        )
    for sha, _ in packaged:
        if connection.execute(
            "SELECT 1 FROM report_snapshots WHERE report_sha256 = ?", (sha,)
        ).fetchone() is None:
            raise WarehouseError("replay references a missing report snapshot")
    return {
        "import_id": import_id,
        "knowledge_seq": None,
        "idempotent_replay": True,
        "new_canonical_fact_sets": [],
        "new_report_snapshots": [],
        "new_corrections": [],
        "created_kind": "idempotent_replay",
    }


def _insert_snapshots_and_sources(connection, package, import_id, knowledge_seq):
    """Insert canonical fact sets, raw snapshots and source relationships.

    Canonical-equivalent byte streams share one fact set; only genuinely new
    raw snapshots create new sample identities.
    """
    new_canonical, new_raw_snapshots = set(), set()
    for item in package["reports"]:
        loaded = item["loaded"]
        raw_sha = loaded["sha256"]
        canonical_sha = loaded["canonical_sha256"]
        if connection.execute(
            "SELECT 1 FROM canonical_fact_sets WHERE canonical_sha256 = ?",
            (canonical_sha,),
        ).fetchone() is None:
            new_canonical.add(canonical_sha)
            connection.execute(
                "INSERT INTO canonical_fact_sets"
                "(canonical_sha256, schema_version, canonical_json, "
                "first_import_id, first_knowledge_seq) VALUES (?, ?, ?, ?, ?)",
                (
                    canonical_sha, loaded["report"]["schema_version"],
                    loaded["canonical_bytes"], import_id, knowledge_seq,
                ),
            )
            _insert_report_facts(connection, canonical_sha, loaded["report"])
        if connection.execute(
            "SELECT 1 FROM report_snapshots WHERE report_sha256 = ?", (raw_sha,)
        ).fetchone() is None:
            new_raw_snapshots.add(raw_sha)
            connection.execute(
                "INSERT INTO report_snapshots"
                "(report_sha256, canonical_sha256, schema_version, report_raw, "
                "first_import_id, first_knowledge_seq) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    raw_sha, canonical_sha,
                    loaded["report"]["schema_version"], item["raw"],
                    import_id, knowledge_seq,
                ),
            )
        connection.execute(
            "INSERT INTO import_reports"
            "(import_id, report_sha256, label, path_label, ordinal) "
            "VALUES (?, ?, ?, ?, ?)",
            (import_id, raw_sha, item["label"], item["path_label"], item["ordinal"]),
        )
    return new_canonical, new_raw_snapshots


def _insert_report_facts(connection, canonical_sha, report):
    schema_version = report["schema_version"]
    for sample in report["samples"]:
        connection.execute(
            "INSERT INTO fact_sample_groups"
            "(canonical_sha256, batch, analyte, sample, n, mean_mg_l, sample_sd_mg_l, "
            "rsd_pct, reportable, reasons_json, calibration_ids_json, qc_checks_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                canonical_sha, sample["batch"], sample["analyte"], sample["sample"],
                sample["n"], sample["mean_mg_l"], sample["sample_sd_mg_l"],
                sample["rsd_pct"], int(sample["reportable"]),
                json.dumps(sample["reasons"], sort_keys=True),
                json.dumps(sample.get("calibration_ids", []), sort_keys=True),
                json.dumps(sample.get("qc_checks", []), sort_keys=True),
            ),
        )
    for qc in report["quality_controls"]:
        connection.execute(
            "INSERT INTO fact_qc_groups"
            "(canonical_sha256, batch, analyte, qc_sample, calibration_id, n, "
            "mean_mg_l, sample_sd_mg_l, rsd_pct, recovery_pct, target_mg_l, passed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                canonical_sha, qc["batch"], qc["analyte"], qc["sample"],
                qc.get("calibration_id"), qc["n"], qc["mean_mg_l"],
                qc["sample_sd_mg_l"], qc["rsd_pct"], qc["recovery_pct"],
                qc.get("target_mg_l"), int(qc["passed"]),
            ),
        )
    if schema_version == 1:
        for batch in report["batches"]:
            connection.execute(
                "INSERT INTO fact_batches"
                "(canonical_sha256, batch, analyte, qc_groups, qc_passed) "
                "VALUES (?, ?, ?, ?, ?)",
                (canonical_sha, batch["batch"], batch["analyte"],
                 batch["qc_groups"], int(batch["qc_passed"])),
            )
        return
    for scope in report["qc_scopes"]:
        connection.execute(
            "INSERT INTO fact_qc_scopes"
            "(canonical_sha256, batch, analyte, calibration_id, qc_groups, "
            "qc_passed, status, failing_samples_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                canonical_sha, scope["batch"], scope["analyte"], scope["calibration_id"],
                scope["qc_groups"], int(scope["qc_passed"]), scope["status"],
                json.dumps(scope["failing_samples"], sort_keys=True),
            ),
        )
    for calibration in report["calibrations"]:
        connection.execute(
            "INSERT INTO fact_calibrations"
            "(canonical_sha256, analyte, calibration_id, valid_from, valid_to, "
            "slope, intercept, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                canonical_sha, calibration["analyte"], calibration["calibration_id"],
                calibration["valid_from"], calibration["valid_to"],
                calibration["slope"], calibration["intercept"], calibration["unit"],
            ),
        )
    for measurement in report["measurements"]:
        connection.execute(
            "INSERT INTO fact_measurements"
            "(canonical_sha256, batch, analyte, sample, replicate, kind, measured_at, "
            "calibration_id, valid_from, valid_to, signal, dilution, slope, "
            "intercept, concentration_mg_l, target_mg_l) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                canonical_sha, measurement["batch"], measurement["analyte"],
                measurement["sample"], measurement["replicate"], measurement["kind"],
                measurement.get("measured_at"), measurement.get("calibration_id"),
                measurement.get("valid_from"), measurement.get("valid_to"),
                measurement["signal"], measurement["dilution"],
                measurement.get("slope"), measurement.get("intercept"),
                measurement["concentration_mg_l"], measurement.get("target_mg_l"),
            ),
        )


def _insert_identities(connection, new_raw_snapshots):
    """Create immutable sample identities for genuinely new raw snapshots.

    Facts are reached through each snapshot's canonical fact set, so a
    reordering-only re-import (existing canonical set under a new raw hash)
    materializes the captured report's identities without duplicating fact rows.
    """
    if not new_raw_snapshots:
        return
    placeholders = ",".join("?" for _ in new_raw_snapshots)
    rows = connection.execute(
        "SELECT s.report_sha256, f.batch, f.analyte, f.sample "
        "FROM report_snapshots s "
        "JOIN fact_sample_groups f ON f.canonical_sha256 = s.canonical_sha256 "
        f"WHERE s.report_sha256 IN ({placeholders}) "
        "ORDER BY s.report_sha256, f.batch, f.analyte, f.sample",
        tuple(sorted(new_raw_snapshots)),
    ).fetchall()
    for row in rows:
        connection.execute(
            "INSERT INTO sample_identities"
            "(report_sha256, batch, analyte, original_sample) VALUES (?, ?, ?, ?)",
            (row["report_sha256"], row["batch"], row["analyte"], row["sample"]),
        )


def _load_current_states(connection):
    states = {}
    rows = connection.execute(
        "SELECT * FROM sample_identities ORDER BY identity_id"
    ).fetchall()
    for row in rows:
        key = (row["report_sha256"], row["batch"], row["analyte"], row["original_sample"])
        states[key] = {
            "current_sample": row["original_sample"],
            "withdrawn": False,
            "identity_id": row["identity_id"],
            "corrections": [],
        }
    corrections = connection.execute(
        "SELECT * FROM corrections ORDER BY knowledge_seq, ordinal"
    ).fetchall()
    for correction in corrections:
        key = (
            correction["report_sha256"], correction["batch"],
            correction["analyte"], correction["original_sample"],
        )
        state = states[key]
        if correction["operation"] == "rename_sample":
            state["current_sample"] = correction["new_sample"]
        else:
            state["withdrawn"] = True
        state["corrections"].append(correction)
    return states


def _apply_manifest_corrections(connection, package, states):
    events = []
    for ordinal, correction in enumerate(package["manifest"]["corrections"]):
        target = correction["target"]
        target_key = (
            target["report_sha256"], target["batch"],
            target["analyte"], target["sample"],
        )
        if target_key not in states:
            raise IdentityError(
                "correction target must identify exactly one imported ordinary sample "
                f"group (QC groups and unknown reports are not valid): {target_key}"
            )
        apply_correction(states, target_key, correction)
        assert_no_display_collision(
            states, target["report_sha256"], target["batch"], target["analyte"]
        )
        events.append({**correction, "ordinal": ordinal})
    return events


def _persist_corrections(connection, package, events, knowledge_seq):
    import_id = package["manifest"]["import_id"]
    corrections_by_id = {
        correction["correction_id"]: correction
        for correction in package["manifest"]["corrections"]
    }
    for event in events:
        correction = corrections_by_id[event["correction_id"]]
        target = event["target"]
        identity = connection.execute(
            "SELECT identity_id FROM sample_identities "
            "WHERE report_sha256 = ? AND batch = ? AND analyte = ? AND original_sample = ?",
            (target["report_sha256"], target["batch"],
             target["analyte"], target["sample"]),
        ).fetchone()
        connection.execute(
            "INSERT INTO corrections"
            "(correction_id, identity_id, report_sha256, batch, analyte, "
            "original_sample, operation, new_sample, reason, knowledge_seq, "
            "import_id, ordinal, canonical_json) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["correction_id"], identity["identity_id"],
                target["report_sha256"], target["batch"], target["analyte"],
                target["sample"], event["operation"], event.get("new_sample"),
                event["reason"], knowledge_seq, import_id, event["ordinal"],
                canonical_json_bytes(correction).decode("utf-8"),
            ),
        )
