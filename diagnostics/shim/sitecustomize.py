"""Apply a decimal context chosen by the diagnostic harness.

Place this directory first on ``PYTHONPATH`` and set ``ASSAY_DIAG_CONTEXT`` to a
JSON object such as ``{"prec": 4}`` or
``{"rounding": "ROUND_UP", "traps": ["Inexact"]}``. CPython imports
``sitecustomize`` during startup, so the product sees the perturbation without
any product file being edited.

When ``ASSAY_DIAG_CONTEXT`` is absent or empty the interpreter default is used,
so the same command line is a valid control run.
"""
import decimal
import json
import os

_raw = os.environ.get("ASSAY_DIAG_CONTEXT", "").strip()
if _raw:
    _spec = json.loads(_raw)
    _context = decimal.getcontext()
    if "prec" in _spec:
        _context.prec = int(_spec["prec"])
    if "rounding" in _spec:
        _context.rounding = _spec["rounding"]
    if "emin" in _spec:
        _context.Emin = int(_spec["emin"])
    if "emax" in _spec:
        _context.Emax = int(_spec["emax"])
    if "clamp" in _spec:
        _context.clamp = int(_spec["clamp"])
    for _name in _spec.get("traps", []):
        _context.traps[getattr(decimal, _name)] = True
