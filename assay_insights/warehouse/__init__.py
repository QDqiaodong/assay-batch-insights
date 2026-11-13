"""Longitudinal result warehouse for synthetic assay batch reports.

The package is intentionally independent of the report-generation CLI:
``assay_insights.load``/``analyze``/``run`` keep their existing contract while
the warehouse captures already-generated schema 1/2 JSON reports into SQLite.
"""
from .errors import (
    ConflictError, IdentityError, ManifestError, ReportValidationError,
    WarehouseError, WarehouseSchemaError,
)
from .importer import capture_manifest, import_manifest
from .query import build_query, render_query, write_query
from .schema import WAREHOUSE_SCHEMA_VERSION

__all__ = [
    "WAREHOUSE_SCHEMA_VERSION",
    "WarehouseError",
    "ManifestError",
    "ReportValidationError",
    "IdentityError",
    "ConflictError",
    "WarehouseSchemaError",
    "capture_manifest",
    "import_manifest",
    "build_query",
    "render_query",
    "write_query",
]
