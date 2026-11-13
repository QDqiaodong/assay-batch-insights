"""Read-only warehouse queries and stable JSON publication.

Queries never open the database for writing and never create a missing database
file. Every output is rebuilt from captured canonical facts, so moving or
deleting the original manifest/report files does not affect query results.
"""
import json
import os
import tempfile

from .errors import WarehouseError
from .manifest import canonical_json_bytes
from .schema import WAREHOUSE_SCHEMA_VERSION, connect

NOTICE = (
    "This warehouse stores imported analysis artifacts and their provenance. "
    "It does not verify original CSVs, instrument readings or calibration facts; "
    "all data are original synthetic demonstrations and carry no medical, "
    "regulatory or laboratory accreditation claim."
)


def build_query(db_path, knowledge_seq=None, batch=None):
    """Build a stable, fully deterministic query result dictionary."""
    connection = connect(db_path, create=False)
    try:
        connection.execute("PRAGMA query_only = ON")
        return _build(connection, knowledge_seq, batch)
    finally:
        connection.close()


def _resolve_seq(connection, knowledge_seq):
    max_seq = int(connection.execute(
        "SELECT COALESCE(MAX(knowledge_seq), 0) AS max_seq FROM knowledge_ledger"
    ).fetchone()["max_seq"])
    if max_seq == 0:
        raise WarehouseError("warehouse contains no committed knowledge versions")
    if knowledge_seq is None:
        return max_seq
    if type(knowledge_seq) is not int or knowledge_seq < 1:
        raise WarehouseError("knowledge_seq must be a positive integer")
    if knowledge_seq > max_seq:
        raise WarehouseError(
            f"knowledge_seq {knowledge_seq} does not exist; latest is {max_seq}"
        )
    if connection.execute(
        "SELECT 1 FROM knowledge_ledger WHERE knowledge_seq = ?", (knowledge_seq,)
    ).fetchone() is None:  # defensive: ledger sequences are contiguous
        raise WarehouseError(f"knowledge_seq {knowledge_seq} is missing from the ledger")
    return knowledge_seq


def _build(connection, knowledge_seq, batch):
    seq = _resolve_seq(connection, knowledge_seq)
    if batch is not None and (not isinstance(batch, str) or not batch or batch != batch.strip()):
        raise WarehouseError("batch filter must be a non-empty trimmed string")

    ledger_rows = connection.execute(
        "SELECT * FROM knowledge_ledger WHERE knowledge_seq <= ? ORDER BY knowledge_seq",
        (seq,),
    ).fetchall()
    import_ids = [row["import_id"] for row in ledger_rows]
    visible_snapshots = _visible_snapshots(connection, import_ids)
    visible_raw_shas = [row["report_sha256"] for row in visible_snapshots]
    visible_canonical_shas = sorted({row["canonical_sha256"] for row in visible_snapshots})

    sources = _build_sources(connection, import_ids, visible_snapshots)
    original_view = _build_original_view(
        connection, visible_snapshots, visible_canonical_shas, batch
    )
    corrected_view = _build_corrected_view(connection, visible_raw_shas, seq, batch)
    longitudinal_summary = _build_longitudinal_summary(
        connection, visible_snapshots, seq, batch
    )
    longitudinal_changes = _build_longitudinal_changes(
        connection, visible_snapshots, batch
    )

    return {
        "warehouse_schema": WAREHOUSE_SCHEMA_VERSION,
        "knowledge_seq": seq,
        "latest_knowledge_seq": int(connection.execute(
            "SELECT MAX(knowledge_seq) AS m FROM knowledge_ledger"
        ).fetchone()["m"]),
        "batch_filter": batch,
        "notice": NOTICE,
        "sources": sources,
        "original_view": original_view,
        "corrected_view": corrected_view,
        "longitudinal_summary": longitudinal_summary,
        "longitudinal_changes": longitudinal_changes,
    }


def _visible_snapshots(connection, import_ids):
    if not import_ids:
        return []
    placeholders = ",".join("?" for _ in import_ids)
    return connection.execute(
        "SELECT DISTINCT s.report_sha256, s.canonical_sha256, s.schema_version, "
        "s.first_knowledge_seq "
        "FROM import_reports ir JOIN report_snapshots s "
        "ON s.report_sha256 = ir.report_sha256 "
        f"WHERE ir.import_id IN ({placeholders}) "
        "ORDER BY s.first_knowledge_seq, s.report_sha256",
        import_ids,
    ).fetchall()


def _build_sources(connection, import_ids, visible_snapshots):
    imports = {
        row["import_id"]: row
        for row in connection.execute(
            "SELECT * FROM imports WHERE import_id IN (%s)"
            % ",".join("?" for _ in import_ids), import_ids,
        )
    } if import_ids else {}
    ledger = {
        row["import_id"]: row
        for row in connection.execute(
            "SELECT * FROM knowledge_ledger WHERE import_id IN (%s)"
            % ",".join("?" for _ in import_ids), import_ids,
        )
    } if import_ids else {}
    import_entries = []
    for import_id in import_ids:
        row = imports[import_id]
        reports = [
            {"report_sha256": link["report_sha256"], "label": link["label"],
             "path_label": link["path_label"]}
            for link in connection.execute(
                "SELECT * FROM import_reports WHERE import_id = ? ORDER BY ordinal",
                (import_id,),
            )
        ]
        correction_ids = [
            row["correction_id"]
            for row in connection.execute(
                "SELECT correction_id FROM corrections WHERE import_id = ? ORDER BY ordinal",
                (import_id,),
            )
        ]
        import_entries.append({
            "import_id": import_id,
            "knowledge_seq": ledger[import_id]["knowledge_seq"],
            "created_kind": ledger[import_id]["created_kind"],
            "manifest_sha256": row["manifest_sha256"],
            "manifest_canonical_sha256": row["manifest_canonical_sha256"],
            "reports": reports,
            "correction_ids": correction_ids,
        })
    snapshot_entries = []
    for snapshot in visible_snapshots:
        links = connection.execute(
            "SELECT import_id, label, path_label FROM import_reports "
            "WHERE report_sha256 = ? ORDER BY import_id, ordinal",
            (snapshot["report_sha256"],),
        ).fetchall()
        snapshot_entries.append({
            "report_sha256": snapshot["report_sha256"],
            "canonical_sha256": snapshot["canonical_sha256"],
            "schema_version": snapshot["schema_version"],
            "first_import_id": connection.execute(
                "SELECT first_import_id FROM report_snapshots WHERE report_sha256 = ?",
                (snapshot["report_sha256"],),
            ).fetchone()["first_import_id"],
            "first_knowledge_seq": snapshot["first_knowledge_seq"],
            "source_links": [
                {"import_id": link["import_id"], "label": link["label"],
                 "path_label": link["path_label"]}
                for link in links
            ],
        })
    correction_entries = [
        {
            "correction_id": row["correction_id"],
            "knowledge_seq": row["knowledge_seq"],
            "import_id": row["import_id"],
            "report_sha256": row["report_sha256"],
            "batch": row["batch"],
            "analyte": row["analyte"],
            "original_sample": row["original_sample"],
            "operation": row["operation"],
            "new_sample": row["new_sample"],
            "reason": row["reason"],
        }
        for row in connection.execute(
            "SELECT * FROM corrections ORDER BY knowledge_seq, ordinal"
        )
    ]
    return {
        "imports": import_entries,
        "report_snapshots": snapshot_entries,
        "corrections": correction_entries,
    }


def _canonical(connection, canonical_sha):
    row = connection.execute(
        "SELECT canonical_json, schema_version FROM canonical_fact_sets "
        "WHERE canonical_sha256 = ?",
        (canonical_sha,),
    ).fetchone()
    return json.loads(row["canonical_json"].decode("utf-8")), row["schema_version"]


def _build_original_view(connection, visible_snapshots, visible_canonical_shas, batch):
    reports = []
    rendered_canonical = set()
    for snapshot in visible_snapshots:
        canonical_sha = snapshot["canonical_sha256"]
        canonical, schema_version = _canonical(connection, canonical_sha)
        report_batches = {s["batch"] for s in canonical["samples"]} | \
            {q["batch"] for q in canonical["quality_controls"]}
        if batch is not None and batch not in report_batches:
            continue
        reports.append({
            "report_sha256": snapshot["report_sha256"],
            "canonical_sha256": canonical_sha,
            "schema_version": schema_version,
            "summary": canonical["summary"],
            "policy": canonical["policy"],
            "inputs_sha256": canonical["inputs_sha256"],
            "batches": [e for e in canonical.get("batches", [])
                        if batch is None or e["batch"] == batch],
            "quality_controls": [e for e in canonical["quality_controls"]
                                 if batch is None or e["batch"] == batch],
            "qc_scopes": [e for e in canonical.get("qc_scopes", [])
                          if batch is None or e["batch"] == batch],
            "samples": [e for e in canonical["samples"]
                        if batch is None or e["batch"] == batch],
        })
        rendered_canonical.add(canonical_sha)
    return {"reports": reports}


def _identity_states_asof(connection, seq, batch):
    identities = connection.execute(
        "SELECT * FROM sample_identities ORDER BY identity_id"
    ).fetchall()
    states = []
    for identity in identities:
        if batch is not None and identity["batch"] != batch:
            continue
        corrections = connection.execute(
            "SELECT * FROM corrections WHERE identity_id = ? AND knowledge_seq <= ? "
            "ORDER BY knowledge_seq, ordinal",
            (identity["identity_id"], seq),
        ).fetchall()
        current_sample = identity["original_sample"]
        withdrawn = False
        history = []
        for correction in corrections:
            if correction["operation"] == "rename_sample":
                current_sample = correction["new_sample"]
            else:
                withdrawn = True
            history.append({
                "correction_id": correction["correction_id"],
                "operation": correction["operation"],
                "new_sample": correction["new_sample"],
                "reason": correction["reason"],
                "knowledge_seq": correction["knowledge_seq"],
                "import_id": correction["import_id"],
            })
        states.append({
            "report_sha256": identity["report_sha256"],
            "batch": identity["batch"],
            "analyte": identity["analyte"],
            "original_sample": identity["original_sample"],
            "current_sample": current_sample,
            "withdrawn": withdrawn,
            "correction_history": history,
        })
    states.sort(key=lambda item: (
        item["batch"], item["analyte"], item["report_sha256"], item["original_sample"]
    ))
    return states


def _sample_fact(connection, raw_sha, batch, analyte, sample):
    return connection.execute(
        "SELECT f.* FROM fact_sample_groups f "
        "JOIN report_snapshots s ON s.canonical_sha256 = f.canonical_sha256 "
        "WHERE s.report_sha256 = ? AND f.batch = ? AND f.analyte = ? AND f.sample = ?",
        (raw_sha, batch, analyte, sample),
    ).fetchone()


def _build_corrected_view(connection, visible_raw_shas, seq, batch):
    visible_set = set(visible_raw_shas)
    states = [
        state for state in _identity_states_asof(connection, seq, batch)
        if state["report_sha256"] in visible_set
    ]
    entries = []
    for state in states:
        fact = _sample_fact(
            connection, state["report_sha256"], state["batch"],
            state["analyte"], state["original_sample"],
        )
        entries.append({
            "report_sha256": state["report_sha256"],
            "batch": state["batch"],
            "analyte": state["analyte"],
            "current_sample": state["current_sample"],
            "withdrawn": state["withdrawn"],
            "original_identity": {
                "report_sha256": state["report_sha256"],
                "batch": state["batch"],
                "analyte": state["analyte"],
                "sample": state["original_sample"],
            },
            "correction_history": state["correction_history"],
            "original_report_fact": {
                "n": fact["n"],
                "mean_mg_l": fact["mean_mg_l"],
                "sample_sd_mg_l": fact["sample_sd_mg_l"],
                "rsd_pct": fact["rsd_pct"],
                "reportable": bool(fact["reportable"]),
                "reasons": json.loads(fact["reasons_json"]),
                "calibration_ids": json.loads(fact["calibration_ids_json"]),
            },
        })
    return {"samples": entries}


def _withdrawn_keys_asof(connection, seq):
    withdrawn = set()
    for identity in connection.execute("SELECT * FROM sample_identities"):
        has_withdrawal = connection.execute(
            "SELECT 1 FROM corrections WHERE identity_id = ? AND knowledge_seq <= ? "
            "AND operation = 'withdraw_sample' LIMIT 1",
            (identity["identity_id"], seq),
        ).fetchone()
        if has_withdrawal:
            withdrawn.add((
                identity["report_sha256"], identity["batch"],
                identity["analyte"], identity["original_sample"],
            ))
    return withdrawn


def _effective_sample_name(connection, identity_id, seq):
    name = connection.execute(
        "SELECT original_sample FROM sample_identities WHERE identity_id = ?",
        (identity_id,),
    ).fetchone()["original_sample"]
    for correction in connection.execute(
        "SELECT operation, new_sample FROM corrections WHERE identity_id = ? "
        "AND knowledge_seq <= ? ORDER BY knowledge_seq, ordinal",
        (identity_id, seq),
    ):
        if correction["operation"] == "rename_sample":
            name = correction["new_sample"]
    return name


def _build_longitudinal_summary(connection, visible_snapshots, seq, batch):
    """Aggregate each current display name across unwithdrawn captured reports."""
    visible_raw_set = {s["report_sha256"] for s in visible_snapshots}
    withdrawn = _withdrawn_keys_asof(connection, seq)
    groups = {}
    for snapshot in visible_snapshots:
        fact_rows = connection.execute(
            "SELECT * FROM fact_sample_groups WHERE canonical_sha256 = ?",
            (snapshot["canonical_sha256"],),
        ).fetchall()
        for row in fact_rows:
            if batch is not None and row["batch"] != batch:
                continue
            raw_sha = snapshot["report_sha256"]
            key_tuple = (raw_sha, row["batch"], row["analyte"], row["sample"])
            if key_tuple in withdrawn:
                continue
            identity = connection.execute(
                "SELECT identity_id FROM sample_identities "
                "WHERE report_sha256 = ? AND batch = ? AND analyte = ? AND original_sample = ?",
                key_tuple,
            ).fetchone()
            current_name = _effective_sample_name(connection, identity["identity_id"], seq)
            group_key = (row["batch"], row["analyte"], current_name)
            groups.setdefault(group_key, {})[raw_sha] = (snapshot, row)
    entries = []
    for (batch_name, analyte, sample_name), by_raw in sorted(groups.items()):
        ordered_raw = sorted(
            by_raw, key=lambda sha: (by_raw[sha][0]["first_knowledge_seq"], sha)
        )
        snapshots_out = []
        for raw_sha in ordered_raw:
            snapshot, row = by_raw[raw_sha]
            snapshots_out.append({
                "report_sha256": raw_sha,
                "canonical_sha256": snapshot["canonical_sha256"],
                "first_knowledge_seq": snapshot["first_knowledge_seq"],
                "schema_version": snapshot["schema_version"],
                "n": row["n"],
                "mean_mg_l": row["mean_mg_l"],
                "sample_sd_mg_l": row["sample_sd_mg_l"],
                "rsd_pct": row["rsd_pct"],
                "calibration_ids": json.loads(row["calibration_ids_json"]),
                "reportable": bool(row["reportable"]),
                "reasons": json.loads(row["reasons_json"]),
            })
        entries.append({
            "batch": batch_name,
            "analyte": analyte,
            "sample": sample_name,
            "distinct_snapshots": len(snapshots_out),
            "snapshots": snapshots_out,
        })
    return {"groups": entries}


def _build_longitudinal_changes(connection, visible_snapshots, batch):
    """Compare original decisions of adjacent snapshots per immutable key.

    Corrections never alter this chain: it follows original business keys and
    original facts only. Canonical-identical byte streams collapse to one node,
    so reordering-only re-imports do not create phantom transitions.
    """
    chains = {}
    for snapshot in visible_snapshots:
        fact_rows = connection.execute(
            "SELECT * FROM fact_sample_groups WHERE canonical_sha256 = ?",
            (snapshot["canonical_sha256"],),
        ).fetchall()
        for row in fact_rows:
            if batch is not None and row["batch"] != batch:
                continue
            key = (row["batch"], row["analyte"], row["sample"])
            chains.setdefault(key, []).append((snapshot, row))
    result = []
    for key in sorted(chains):
        nodes_raw = sorted(
            chains[key],
            key=lambda pair: (pair[0]["first_knowledge_seq"], pair[0]["report_sha256"]),
        )
        # Collapse canonical-identical nodes while retaining every raw hash.
        deduped = []
        for snapshot, row in nodes_raw:
            canonical_sha = snapshot["canonical_sha256"]
            if deduped and deduped[-1]["canonical_sha256"] == canonical_sha:
                deduped[-1]["report_sha256s"].append(snapshot["report_sha256"])
                continue
            deduped.append({
                "first_knowledge_seq": snapshot["first_knowledge_seq"],
                "canonical_sha256": canonical_sha,
                "report_sha256s": [snapshot["report_sha256"]],
                "row": row,
            })
        transitions = []
        for previous, current in zip(deduped, deduped[1:]):
            prev_row, curr_row = previous["row"], current["row"]
            cal_prev = json.loads(prev_row["calibration_ids_json"])
            cal_curr = json.loads(curr_row["calibration_ids_json"])
            mean_changed = prev_row["mean_mg_l"] != curr_row["mean_mg_l"]
            reportable_changed = bool(prev_row["reportable"]) != bool(curr_row["reportable"])
            reasons_changed = json.loads(prev_row["reasons_json"]) != json.loads(curr_row["reasons_json"])
            calibration_changed = cal_prev != cal_curr
            transitions.append({
                "from": _change_node(previous, prev_row, cal_prev),
                "to": _change_node(current, curr_row, cal_curr),
                "changes": {
                    "mean_changed": mean_changed,
                    "reportable_changed": reportable_changed,
                    "reasons_changed": reasons_changed,
                    "calibration_ids_changed": calibration_changed,
                    "any_changed": any([
                        mean_changed, reportable_changed, reasons_changed,
                        calibration_changed,
                    ]),
                },
            })
        result.append({
            "batch": key[0],
            "analyte": key[1],
            "original_sample": key[2],
            "snapshots": [
                {
                    "first_knowledge_seq": node["first_knowledge_seq"],
                    "canonical_sha256": node["canonical_sha256"],
                    "report_sha256s": node["report_sha256s"],
                }
                for node in deduped
            ],
            "transitions": transitions,
        })
    return {"chains": result}


def _change_node(node, row, calibration_ids):
    return {
        "report_sha256s": node["report_sha256s"],
        "canonical_sha256": node["canonical_sha256"],
        "first_knowledge_seq": node["first_knowledge_seq"],
        "mean_mg_l": row["mean_mg_l"],
        "reportable": bool(row["reportable"]),
        "reasons": json.loads(row["reasons_json"]),
        "calibration_ids": calibration_ids,
    }


def render_query(result):
    """Deterministic JSON text; no time, absolute path or rowid is included."""
    return canonical_json_bytes(result).decode("utf-8")


def write_query(result, output_path):
    """Serialize fully, then atomically replace the output in its directory."""
    text = render_query(result)
    output_path = str(output_path)
    directory = os.path.dirname(os.path.abspath(output_path)) or "."
    if not os.path.isdir(directory):
        raise WarehouseError(f"output directory does not exist: {directory}")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".warehouse-query-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return output_path
