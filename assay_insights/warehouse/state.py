"""Correction-chain semantics over immutable original business keys.

A sample identity is the immutable tuple
``(report_sha256, batch, analyte, original_sample)``. Corrections reference that
tuple forever, never a display name. The effective state at any knowledge point
is derived by replaying the ordered, successful corrections:

* ``rename_sample`` replaces the current display name;
* ``withdraw_sample`` marks the identity withdrawn and ends its chain.

No row is ever mutated or deleted, so any historical knowledge sequence can be
rebuilt exactly.
"""
from .errors import IdentityError


def apply_correction(states, target_key, correction):
    """Validate and apply one correction to an in-memory effective-state map.

    Collisions are checked by the caller after each application against the full
    map; this function enforces target existence and chain ordering.
    """
    state = states.get(target_key)
    if state is None:
        raise IdentityError(
            "correction target must identify exactly one imported ordinary sample group "
            f"(not a QC group or unknown report): {target_key}"
        )
    operation = correction["operation"]
    if state["withdrawn"]:
        raise IdentityError(
            f"withdrawn sample {target_key} cannot be renamed or withdrawn again"
        )
    if operation == "rename_sample":
        state["current_sample"] = correction["new_sample"]
    elif operation == "withdraw_sample":
        state["withdrawn"] = True
    else:  # pragma: no cover - envelope validation already rejects this
        raise IdentityError(f"unsupported correction operation: {operation}")
    state["corrections"].append(correction)


def assert_no_display_collision(states, report_sha256, batch, analyte):
    """Two live identities may not share one display name in one report scope."""
    seen = {}
    for key, state in states.items():
        if key[0] != report_sha256 or key[1] != batch or key[2] != analyte:
            continue
        if state["withdrawn"]:
            continue
        name = state["current_sample"]
        if name in seen:
            raise IdentityError(
                f"rename collision in report {report_sha256} {batch}/{analyte}: "
                f"{seen[name]} and {key[3]} both resolve to {name!r}"
            )
        seen[name] = key[3]
