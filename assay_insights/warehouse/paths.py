"""Path containment and business identifier rules for warehouse imports."""
import os
from pathlib import Path

from .errors import ManifestError


def require_identifier(value, field):
    """Validate a business identifier: non-empty text with no surrounding space.

    Every business key (import_id, label, batch, sample, analyte, ...) uses the
    same rule. Booleans and other non-string JSON values are rejected even when
    they are truthy.
    """
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ManifestError(f"{field} must not have leading or trailing whitespace")
    return value


def safe_relative_path(raw, manifest_dir):
    """Resolve a report path relative to the manifest directory.

    Absolute paths, directory traversal and symbolic links (including a
    symlinked intermediate directory) are rejected so a manifest can never point
    outside its own directory tree. The resolved path is returned together with
    the audit label (the original relative text).
    """
    if not isinstance(raw, str) or not raw:
        raise ManifestError("report path must be a non-empty relative path string")
    if raw != raw.strip():
        raise ManifestError("report path must not have leading or trailing whitespace")
    normalized = Path(raw)
    if os.path.isabs(raw):
        raise ManifestError(f"absolute report paths are not allowed: {raw!r}")
    # Pure lexical checks first; reject drives, anchors and any parent segment.
    if normalized.drive or normalized.anchor or any(part == ".." for part in normalized.parts):
        raise ManifestError(f"report path escapes the manifest directory: {raw!r}")
    base = Path(manifest_dir).resolve(strict=False)
    # Walk the un-resolved lexical components and reject every symlink, including
    # intermediate directory symlinks, before any real resolution occurs.
    current = base
    for part in normalized.parts:
        current = current / part
        if os.path.islink(str(current)):
            raise ManifestError(
                f"symbolic links are not allowed in report paths: {raw!r}"
            )
    # Only now resolve to confirm containment of the final real location.
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ManifestError(f"report path escapes the manifest directory: {raw!r}") from exc
    if not resolved.is_file():
        raise ManifestError(
            f"report file does not exist or is not a regular file: {raw!r}"
        )
    return resolved, raw
