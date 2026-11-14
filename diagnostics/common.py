"""Shared plumbing for the host-context diagnostic.

Nothing in this module imports the product. It exists so that both the parent
(orchestrator) and the isolated child (launcher) can describe, apply and compare
decimal contexts with one implementation.

Terminology used throughout the diagnostics:

* **factor** - the one ambient input varied by a trial (decimal precision,
  rounding, a decimal trap, ``TZ`` or ``PYTHONHASHSEED``);
* **control** - the unperturbed default context, whose record is produced by the
  same code path as every perturbed trial;
* **stage** - an ordered call boundary where the product's values can be
  observed without editing it (load, concentration, statistics, threshold,
  display, artifact).
"""
import hashlib
import json
import os
from decimal import (
    ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_DOWN, ROUND_HALF_EVEN,
    ROUND_HALF_UP, ROUND_UP, ROUND_05UP, Clamped, Decimal, DivisionByZero,
    FloatOperation, Inexact, InvalidOperation, Overflow, Rounded, Subnormal,
    Underflow, getcontext,
)

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROUNDINGS = {
    "ROUND_CEILING": ROUND_CEILING,
    "ROUND_DOWN": ROUND_DOWN,
    "ROUND_FLOOR": ROUND_FLOOR,
    "ROUND_HALF_DOWN": ROUND_HALF_DOWN,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_UP": ROUND_UP,
    "ROUND_05UP": ROUND_05UP,
}

TRAPS = {
    "Clamped": Clamped,
    "DivisionByZero": DivisionByZero,
    "FloatOperation": FloatOperation,
    "Inexact": Inexact,
    "InvalidOperation": InvalidOperation,
    "Overflow": Overflow,
    "Rounded": Rounded,
    "Subnormal": Subnormal,
    "Underflow": Underflow,
}

FLAG_NAMES = {value: name for name, value in TRAPS.items()}


def canonical_json(value):
    """Stable JSON text used for hashing and byte comparisons."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def decimal_text(value):
    """Exact, exponent-free rendering of a Decimal (or None)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def raised_flags():
    """Names of decimal flags currently raised, sorted."""
    context = getcontext()
    return sorted(name for value, name in FLAG_NAMES.items() if context.flags[value])


def clear_flags():
    getcontext().clear_flags()


def describe_context():
    """Full snapshot of the active decimal context, including traps and flags."""
    context = getcontext()
    return {
        "prec": context.prec,
        "rounding": context.rounding,
        "Emin": context.Emin,
        "Emax": context.Emax,
        "clamp": context.clamp,
        "traps": sorted(name for value, name in FLAG_NAMES.items() if context.traps[value]),
        "flags": raised_flags(),
    }


def apply_context(spec):
    """Apply a factor specification to the active context and read it back.

    ``spec`` keys: ``prec``, ``rounding``, ``emin``, ``emax``, ``traps``
    (iterable of trap names to enable) and ``clamp``. Absent keys keep the
    interpreter default. Returns the *actual* post-application context so the
    caller can prove what was in force rather than what was requested.
    """
    context = getcontext()
    if "prec" in spec:
        context.prec = int(spec["prec"])
    if "rounding" in spec:
        context.rounding = ROUNDINGS[spec["rounding"]]
    if "emin" in spec:
        context.Emin = int(spec["emin"])
    if "emax" in spec:
        context.Emax = int(spec["emax"])
    if "clamp" in spec:
        context.clamp = int(spec["clamp"])
    for name in spec.get("traps", []):
        context.traps[TRAPS[name]] = True
    context.clear_flags()
    return describe_context()


def parse_decimal(text):
    """Parse a stored decimal string, returning None when it is not numeric."""
    if text is None:
        return None
    try:
        return Decimal(text)
    except Exception:  # noqa: BLE001 - diagnostics must never crash on evidence
        return None
