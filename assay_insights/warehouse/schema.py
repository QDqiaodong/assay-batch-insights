"""SQLite schema and connection management for the result warehouse.

The schema models four layers:

1. Provenance (``imports``, ``import_reports``): captured manifest/report
   bytes, SHA256 digests and audit labels. Paths are labels only.
2. Canonical fact sets (``canonical_fact_sets`` plus the ``fact_*`` tables):
   one normalized fact set per distinct *canonical* report content. Two byte
   streams that differ only in record/JSON key order share one canonical fact
   set, so repeated imports never amplify measurements or sample counts.
3. Raw report snapshots (``report_snapshots``): one row per distinct captured
   byte stream, pointing at its canonical fact set while retaining the exact
   original bytes and their own SHA256.
4. Corrections and the knowledge ledger (``sample_identities``,
   ``corrections``, ``knowledge_ledger``): immutable original business keys and
   the ordered, replayable correction chain.
"""
import os
import sqlite3

from .errors import WarehouseSchemaError

WAREHOUSE_SCHEMA_VERSION = 1


def _split_ddl(script):
    """Split the schema script into individual DDL statements.

    SQL line comments are dropped first, then the remaining text is split on
    semicolons. Executing each statement separately preserves the explicit
    surrounding transaction (unlike ``executescript``, which commits first).
    """
    cleaned_lines = [
        line for line in script.splitlines()
        if not line.strip().startswith("--")
    ]
    return [
        statement.strip()
        for statement in "\n".join(cleaned_lines).split(";")
        if statement.strip()
    ]


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS warehouse_meta (
    meta_key TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_ledger (
    knowledge_seq INTEGER PRIMARY KEY,
    import_id TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL,
    new_facts INTEGER NOT NULL,
    new_corrections INTEGER NOT NULL,
    created_kind TEXT NOT NULL CHECK (created_kind IN ('fact_transaction', 'source_only_transaction'))
);

CREATE TABLE IF NOT EXISTS imports (
    import_id TEXT PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL,
    manifest_canonical_sha256 TEXT NOT NULL,
    manifest_raw BLOB NOT NULL,
    manifest_canonical BLOB NOT NULL,
    action TEXT NOT NULL CHECK (action = 'apply')
);

-- One normalized fact set per distinct canonical report content.
CREATE TABLE IF NOT EXISTS canonical_fact_sets (
    canonical_sha256 TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2)),
    canonical_json BLOB NOT NULL,
    first_import_id TEXT NOT NULL,
    first_knowledge_seq INTEGER NOT NULL,
    FOREIGN KEY (first_import_id) REFERENCES imports(import_id)
);

-- One row per distinct captured byte stream; several snapshots may share a
-- canonical fact set when only ordering/whitespace differs.
CREATE TABLE IF NOT EXISTS report_snapshots (
    report_sha256 TEXT PRIMARY KEY,
    canonical_sha256 TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2)),
    report_raw BLOB NOT NULL,
    first_import_id TEXT NOT NULL,
    first_knowledge_seq INTEGER NOT NULL,
    FOREIGN KEY (canonical_sha256) REFERENCES canonical_fact_sets(canonical_sha256),
    FOREIGN KEY (first_import_id) REFERENCES imports(import_id)
);

CREATE TABLE IF NOT EXISTS import_reports (
    import_id TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    label TEXT NOT NULL,
    path_label TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (import_id, report_sha256),
    UNIQUE (import_id, label),
    FOREIGN KEY (import_id) REFERENCES imports(import_id),
    FOREIGN KEY (report_sha256) REFERENCES report_snapshots(report_sha256)
);

CREATE TABLE IF NOT EXISTS fact_sample_groups (
    canonical_sha256 TEXT NOT NULL,
    batch TEXT NOT NULL,
    analyte TEXT NOT NULL,
    sample TEXT NOT NULL,
    n INTEGER NOT NULL,
    mean_mg_l TEXT NOT NULL,
    sample_sd_mg_l TEXT,
    rsd_pct TEXT,
    reportable INTEGER NOT NULL CHECK (reportable IN (0, 1)),
    reasons_json TEXT NOT NULL,
    calibration_ids_json TEXT NOT NULL,
    qc_checks_json TEXT NOT NULL,
    PRIMARY KEY (canonical_sha256, batch, analyte, sample),
    FOREIGN KEY (canonical_sha256) REFERENCES canonical_fact_sets(canonical_sha256)
);

CREATE TABLE IF NOT EXISTS fact_qc_groups (
    canonical_sha256 TEXT NOT NULL,
    batch TEXT NOT NULL,
    analyte TEXT NOT NULL,
    qc_sample TEXT NOT NULL,
    calibration_id TEXT,
    n INTEGER NOT NULL,
    mean_mg_l TEXT NOT NULL,
    sample_sd_mg_l TEXT,
    rsd_pct TEXT,
    recovery_pct TEXT NOT NULL,
    target_mg_l TEXT,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    PRIMARY KEY (canonical_sha256, batch, analyte, qc_sample, calibration_id),
    FOREIGN KEY (canonical_sha256) REFERENCES canonical_fact_sets(canonical_sha256)
);

CREATE TABLE IF NOT EXISTS fact_qc_scopes (
    canonical_sha256 TEXT NOT NULL,
    batch TEXT NOT NULL,
    analyte TEXT NOT NULL,
    calibration_id TEXT NOT NULL,
    qc_groups INTEGER NOT NULL,
    qc_passed INTEGER NOT NULL CHECK (qc_passed IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'missing')),
    failing_samples_json TEXT NOT NULL,
    PRIMARY KEY (canonical_sha256, batch, analyte, calibration_id),
    FOREIGN KEY (canonical_sha256) REFERENCES canonical_fact_sets(canonical_sha256)
);

CREATE TABLE IF NOT EXISTS fact_batches (
    canonical_sha256 TEXT NOT NULL,
    batch TEXT NOT NULL,
    analyte TEXT NOT NULL,
    qc_groups INTEGER NOT NULL,
    qc_passed INTEGER NOT NULL CHECK (qc_passed IN (0, 1)),
    PRIMARY KEY (canonical_sha256, batch, analyte),
    FOREIGN KEY (canonical_sha256) REFERENCES canonical_fact_sets(canonical_sha256)
);

CREATE TABLE IF NOT EXISTS fact_calibrations (
    canonical_sha256 TEXT NOT NULL,
    analyte TEXT NOT NULL,
    calibration_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    slope TEXT NOT NULL,
    intercept TEXT NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (canonical_sha256, analyte, calibration_id),
    FOREIGN KEY (canonical_sha256) REFERENCES canonical_fact_sets(canonical_sha256)
);

CREATE TABLE IF NOT EXISTS fact_measurements (
    canonical_sha256 TEXT NOT NULL,
    batch TEXT NOT NULL,
    analyte TEXT NOT NULL,
    sample TEXT NOT NULL,
    replicate TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('sample', 'QC')),
    measured_at TEXT,
    calibration_id TEXT,
    valid_from TEXT,
    valid_to TEXT,
    signal TEXT NOT NULL,
    dilution TEXT NOT NULL,
    slope TEXT,
    intercept TEXT,
    concentration_mg_l TEXT NOT NULL,
    target_mg_l TEXT,
    PRIMARY KEY (canonical_sha256, batch, analyte, sample, replicate),
    FOREIGN KEY (canonical_sha256) REFERENCES canonical_fact_sets(canonical_sha256)
);

-- Identities and corrections are anchored to the immutable raw report hash,
-- never to a display name, so reordering-only re-imports never perturb them.
CREATE TABLE IF NOT EXISTS sample_identities (
    identity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_sha256 TEXT NOT NULL,
    batch TEXT NOT NULL,
    analyte TEXT NOT NULL,
    original_sample TEXT NOT NULL,
    UNIQUE (report_sha256, batch, analyte, original_sample),
    FOREIGN KEY (report_sha256) REFERENCES report_snapshots(report_sha256)
);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    identity_id INTEGER NOT NULL,
    report_sha256 TEXT NOT NULL,
    batch TEXT NOT NULL,
    analyte TEXT NOT NULL,
    original_sample TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('rename_sample', 'withdraw_sample')),
    new_sample TEXT,
    reason TEXT NOT NULL,
    knowledge_seq INTEGER NOT NULL,
    import_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    canonical_json TEXT NOT NULL,
    UNIQUE (knowledge_seq, ordinal),
    FOREIGN KEY (identity_id) REFERENCES sample_identities(identity_id),
    FOREIGN KEY (knowledge_seq) REFERENCES knowledge_ledger(knowledge_seq),
    FOREIGN KEY (import_id) REFERENCES imports(import_id)
);

CREATE INDEX IF NOT EXISTS idx_corrections_identity ON corrections(identity_id, knowledge_seq, ordinal);
CREATE INDEX IF NOT EXISTS idx_import_reports_sha ON import_reports(report_sha256);
CREATE INDEX IF NOT EXISTS idx_snapshots_canonical ON report_snapshots(canonical_sha256);
CREATE INDEX IF NOT EXISTS idx_fact_samples_key ON fact_sample_groups(batch, analyte, sample);
CREATE INDEX IF NOT EXISTS idx_fact_measurements_key ON fact_measurements(batch, analyte, sample);
"""


def connect(db_path, *, create=True, busy_timeout_ms=30000):
    """Open a warehouse database.

    With ``create=False`` (report-only mode) a missing database is reported
    rather than created. An existing database missing the warehouse schema is a
    hard error; the file is never silently rebuilt.
    """
    path = str(db_path)
    if not create and not os.path.exists(path):
        raise WarehouseSchemaError(f"warehouse database does not exist: {path}")
    connection = sqlite3.connect(
        path, timeout=busy_timeout_ms / 1000.0, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    connection.execute("PRAGMA foreign_keys = ON")
    _initialize(connection)
    return connection


def _initialize(connection):
    # Schema creation must be serialized when several processes race on a
    # missing database. BEGIN IMMEDIATE takes the write lock; competing
    # processes block on busy_timeout rather than colliding on the meta insert.
    # In autocommit mode (isolation_level=None) DDL takes an implicit schema
    # lock and COMMIT releases it.
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='warehouse_meta'"
        ).fetchone()
        if existing is None:
            other_tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if other_tables:
                raise WarehouseSchemaError(
                    "database contains unrelated tables but no warehouse schema; "
                    "refusing to initialize"
                )
            # Statement-by-statement execution keeps the explicit transaction
            # open; executescript would force an implicit COMMIT first.
            for ddl in _split_ddl(SCHEMA_SQL):
                connection.execute(ddl)
            connection.execute(
                "INSERT INTO warehouse_meta(meta_key, meta_value) VALUES (?, ?)",
                ("schema_version", str(WAREHOUSE_SCHEMA_VERSION)),
            )
        else:
            row = connection.execute(
                "SELECT meta_value FROM warehouse_meta WHERE meta_key = 'schema_version'"
            ).fetchone()
            if row is None or int(row["meta_value"]) != WAREHOUSE_SCHEMA_VERSION:
                raise WarehouseSchemaError(
                    "incompatible warehouse schema version: "
                    f"{row['meta_value'] if row else 'missing'}"
                )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def begin_immediate(connection):
    """Acquire the SQLite single-writer lock before any write transaction."""
    connection.execute("BEGIN IMMEDIATE")


def next_knowledge_seq(connection):
    row = connection.execute(
        "SELECT COALESCE(MAX(knowledge_seq), 0) + 1 AS next_seq FROM knowledge_ledger"
    ).fetchone()
    return int(row["next_seq"])
