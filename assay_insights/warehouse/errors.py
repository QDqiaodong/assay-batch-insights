"""Domain errors raised by the longitudinal result warehouse.

All validation, identity, correction and transaction conflicts are reported as
:class:`WarehouseError` subclasses so callers can distinguish warehouse contract
failures (which must roll back) from ordinary operating-system errors.
"""


class WarehouseError(Exception):
    """Base class for every warehouse contract failure."""


class ManifestError(WarehouseError):
    """The manifest envelope is malformed or violates the import contract."""


class ReportValidationError(WarehouseError):
    """A captured report is not a self-consistent project schema 1/2 report."""


class IdentityError(WarehouseError):
    """A business key, correction target or correction chain is invalid."""


class ConflictError(WarehouseError):
    """An idempotency key already exists with different captured bytes."""


class WarehouseSchemaError(WarehouseError):
    """An existing SQLite database does not carry the warehouse schema."""
