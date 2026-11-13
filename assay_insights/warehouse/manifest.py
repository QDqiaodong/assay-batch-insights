"""Manifest envelope parsing and strict contract validation.

The manifest bytes are captured exactly once by the caller; parsing and SHA256
always operate on that same byte string. Only structural validation lives here;
correction target resolution is performed inside the import transaction by
:mod:`assay_insights.warehouse.importer`.
"""
import hashlib
import json

from .errors import ManifestError
from .paths import require_identifier

SUPPORTED_OPERATIONS = {"rename_sample", "withdraw_sample"}


def _require_object(value, field):
    if not isinstance(value, dict):
        raise ManifestError(f"{field} must be a JSON object")
    return value


def parse_manifest(raw_bytes):
    """Parse and validate a manifest from its captured bytes.

    Returns ``(manifest, canonical_bytes, manifest_sha256)`` where the canonical
    bytes are a stable JSON rendering used for idempotent byte comparison. The
    original raw bytes remain the provenance record stored verbatim.
    """
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise ManifestError("manifest must be captured as raw bytes")
    try:
        manifest = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ManifestError("manifest must be UTF-8 encoded") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    _require_object(manifest, "manifest")

    allowed_top = {"schema_version", "import_id", "action", "reports", "corrections"}
    if set(manifest) != allowed_top:
        raise ManifestError(
            f"manifest keys must be exactly {sorted(allowed_top)}, found {sorted(manifest)}"
        )
    # bool is a subclass of int; reject it explicitly per the contract.
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ManifestError("manifest schema_version must be the integer 1")
    require_identifier(manifest["import_id"], "import_id")
    if manifest["action"] != "apply":
        raise ManifestError("manifest action must be \"apply\"")
    if not isinstance(manifest["reports"], list) or not manifest["reports"]:
        raise ManifestError("manifest reports must be a non-empty array")

    labels = set()
    for index, entry in enumerate(manifest["reports"]):
        _require_object(entry, f"reports[{index}]")
        if set(entry) != {"path", "label"}:
            raise ManifestError(f"reports[{index}] keys must be exactly path and label")
        require_identifier(entry["label"], f"reports[{index}].label")
        if entry["label"] in labels:
            raise ManifestError(f"duplicate report label within manifest: {entry['label']!r}")
        labels.add(entry["label"])
        if not isinstance(entry["path"], str) or not entry["path"]:
            raise ManifestError(f"reports[{index}].path must be a non-empty string")

    if not isinstance(manifest["corrections"], list):
        raise ManifestError("manifest corrections must be an array")
    correction_ids = set()
    for index, correction in enumerate(manifest["corrections"]):
        _validate_correction(correction, index, correction_ids)
    canonical_bytes = canonical_json_bytes(manifest)
    return manifest, canonical_bytes, hashlib.sha256(raw_bytes).hexdigest()


def _validate_correction(correction, index, correction_ids):
    location = f"corrections[{index}]"
    _require_object(correction, location)
    base_keys = {"correction_id", "target", "operation", "reason"}
    if set(correction) - base_keys - {"new_sample"}:
        raise ManifestError(f"{location} contains unknown fields")
    if not base_keys <= set(correction):
        raise ManifestError(f"{location} keys must include {sorted(base_keys)}")
    require_identifier(correction["correction_id"], f"{location}.correction_id")
    if correction["correction_id"] in correction_ids:
        raise ManifestError(
            f"duplicate correction_id within manifest: {correction['correction_id']!r}"
        )
    correction_ids.add(correction["correction_id"])
    if correction["operation"] not in SUPPORTED_OPERATIONS:
        raise ManifestError(
            f"{location}.operation must be one of {sorted(SUPPORTED_OPERATIONS)}"
        )
    require_identifier(correction["reason"], f"{location}.reason")
    target = correction["target"]
    _require_object(target, f"{location}.target")
    if set(target) != {"report_sha256", "batch", "sample", "analyte"}:
        raise ManifestError(
            f"{location}.target keys must be exactly report_sha256, batch, sample, analyte"
        )
    for key in ["report_sha256", "batch", "sample", "analyte"]:
        require_identifier(target[key], f"{location}.target.{key}")
    if len(target["report_sha256"]) != 64:
        raise ManifestError(f"{location}.target.report_sha256 must be a 64 hex-character SHA256")
    try:
        int(target["report_sha256"], 16)
    except ValueError as exc:
        raise ManifestError(f"{location}.target.report_sha256 is not hexadecimal") from exc
    if correction["operation"] == "rename_sample":
        if "new_sample" not in correction:
            raise ManifestError(f"{location}: rename_sample requires new_sample")
        require_identifier(correction["new_sample"], f"{location}.new_sample")
    elif "new_sample" in correction:
        raise ManifestError(f"{location}: withdraw_sample must not carry new_sample")


def canonical_json_bytes(value):
    """Stable JSON rendering: sorted keys, two-space indentation, trailing LF.

    Deterministic across processes because no time, path or row identifier is
    embedded by the warehouse itself.
    """
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
